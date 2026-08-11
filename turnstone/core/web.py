"""Web utilities — HTML stripping, SSRF protection, and the guarded fetch."""

import dataclasses
import re
from html import unescape as _html_unescape
from urllib.parse import urlparse

import httpx

from turnstone.core.ip_classify import (
    BLOCKED_HOSTNAMES,
    AddressLane,
    ResolutionError,
    describe_address,
    resolve_and_classify,
)

_RE_INVISIBLE = re.compile(
    r"<(script|style|template|noscript)\b[^>]*>.*?</\1\s*>",
    re.DOTALL | re.IGNORECASE,
)
# Tags whose boundary should become a newline: block-level elements plus <br>, so
# paragraphs, headings, list items, and table cells don't glue together once the
# tags are removed (e.g. "<p>a</p><p>b</p>" -> "a\n\nb", not "ab").
_NEWLINE_TAGS = frozenset(
    {
        "address",
        "article",
        "aside",
        "blockquote",
        "br",
        "dd",
        "div",
        "dl",
        "dt",
        "fieldset",
        "figcaption",
        "figure",
        "footer",
        "form",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "li",
        "main",
        "nav",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "tbody",
        "td",
        "tfoot",
        "th",
        "thead",
        "tr",
        "ul",
    }
)
# Single linear tag scan. The possessive quantifier ([^>]++) cannot backtrack, so
# untrusted HTML cannot trigger catastrophic backtracking (ReDoS) here.
_RE_TAG = re.compile(r"<[^>]++>")
_RE_TAG_NAME = re.compile(r"</?\s*([a-zA-Z][a-zA-Z0-9]*)")
_RE_WS = re.compile(r"[ \t]+")
_RE_LINE_WS = re.compile(r" *\n *")
_RE_BLANKLINES = re.compile(r"\n{3,}")


def _tag_replacement(match: re.Match[str]) -> str:
    """Map one HTML tag to a newline (block boundary / <br>) or to nothing (inline)."""
    name = _RE_TAG_NAME.match(match.group())
    if name is not None and name.group(1).lower() in _NEWLINE_TAGS:
        return "\n"
    return ""


def strip_html(html: str) -> str:
    """Convert HTML to plain text, preserving block structure as line breaks.

    Block-level boundaries (and ``<br>``) become newlines while inline tags are
    dropped, so paragraphs, headings, list items, and table cells stay separated
    rather than concatenating into a structureless run of text. A single linear tag
    scan is used so untrusted input cannot trigger catastrophic regex backtracking.
    """
    # Remove elements whose content should never appear as text
    text = _RE_INVISIBLE.sub("", html)
    # One pass over tags: block/<br> boundaries -> newline, inline tags -> removed
    text = _RE_TAG.sub(_tag_replacement, text)
    text = _html_unescape(text)
    text = _RE_WS.sub(" ", text)
    text = _RE_LINE_WS.sub("\n", text)
    text = _RE_BLANKLINES.sub("\n\n", text)
    return text.strip()


@dataclasses.dataclass(frozen=True)
class UrlScreen:
    """The verdict on one URL: which lane, why, and whether it is wholly private."""

    lane: AddressLane
    error: str | None
    all_private: bool
    """True when EVERY resolved address is private.

    ``lane`` is the worst address, which is the right basis for refusing. It is
    the wrong basis for treating a chain as "inside the operator's network": a
    hostname with both a private and a public A record folds to PRIVATE, but the
    connection may land on the public record, so the approval the operator gave
    does not describe where the fetch actually goes.
    """


def screen_url(url: str) -> UrlScreen:
    """Classify every address *url* resolves to and return the worst lane.

    The test is "globally routable", not "not in a private range". Those are
    not complements: CGNAT (100.64.0.0/10, RFC 6598 shared address space —
    where overlay VPNs commonly assign internal hosts) is neither private nor
    global, so a denylist let it through. IPv6 transition addresses are judged
    by the IPv4 they route to rather than by the wrapper (see
    :mod:`turnstone.core.ip_classify`).

    Every resolved address is classified and the *worst* lane wins. Returning
    on the first offending address would let a hostname whose first A record is
    merely private mask a second record that is link-local: the caller would
    see the approvable lane, and under the operator opt-in the whole hostname —
    including the record it never looked at — would be fetched.

    Fails closed. A resolution failure is a refusal, not a pass: the fetch that
    follows resolves again, so an authority that answers the guard's query with
    SERVFAIL and the fetch's query with an internal address would otherwise
    turn the guard off for that hop. Malformed URLs are refusals too — every
    exception path returns a verdict rather than raising, because the callers
    screen model-supplied URLs and one of them prepares tools outside any
    ``try``.
    """
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname
        # Touched, not used: ``urlsplit.port`` parses lazily and raises for an
        # out-of-range value, which must become a refusal rather than escape.
        # It is not passed to resolution — a numeric service does not change
        # which addresses come back, and classification looks only at those.
        _ = parsed.port
    except ValueError:
        return UrlScreen(AddressLane.NEVER, f"Blocked: malformed URL ({url})", False)
    if not hostname:
        return UrlScreen(AddressLane.NEVER, "Invalid URL: no hostname", False)
    if hostname.lower() in BLOCKED_HOSTNAMES:
        return UrlScreen(
            AddressLane.NEVER, f"Blocked: URL names a metadata host ({hostname})", False
        )

    try:
        classified = resolve_and_classify(hostname)
    except ResolutionError as exc:
        return UrlScreen(AddressLane.NEVER, f"Blocked: {exc}", False)

    worst, offender = max(classified, key=lambda item: item[0])
    all_private = all(lane is AddressLane.PRIVATE for lane, _ in classified)

    if worst is AddressLane.PUBLIC:
        return UrlScreen(AddressLane.PUBLIC, None, False)
    if worst is AddressLane.NEVER:
        return UrlScreen(
            worst,
            "Blocked: URL resolves to a link-local/multicast/unspecified/"
            f"reserved/metadata address ({describe_address(offender)})",
            all_private,
        )
    return UrlScreen(
        worst,
        f"Blocked: URL resolves to private/internal address ({describe_address(offender)})",
        all_private,
    )


FETCH_BYTE_CEILING = 32 * 1024 * 1024
"""Anti-OOM backstop on a guarded fetch's decoded body (see fetch_with_ssrf_guard)."""

_REDIRECT_STATUSES = (301, 302, 303, 307, 308)
_STALE_FRAMING_HEADERS = frozenset({"content-encoding", "content-length", "transfer-encoding"})


def fetch_with_ssrf_guard(
    url: str,
    *,
    timeout: float,
    user_agent: str = "turnstone/1.0",
    max_redirects: int = 5,
    allow_private_origin: bool = False,
    max_bytes: int = FETCH_BYTE_CEILING,
) -> httpx.Response:
    """GET *url* following redirects manually, SSRF-screening EVERY hop.

    ``httpx.get(follow_redirects=True)`` checks nothing between hops — a
    public URL that 302s into private address space (cloud metadata, an
    internal admin endpoint) would be fetched before any post-hoc check runs,
    executing the private-network request even if the response is later
    discarded.  Here each hop's URL is screened BEFORE its request is issued.

    EVERY hop is screened, in every mode.  ``allow_private_origin`` widens
    which lanes are acceptable, it does not turn screening off: the caller
    sets it only when the operator opted in AND the original target itself
    named a private address, so the approval gate saw and approved that
    private URL.

    The permission is also revoked the moment the chain leaves that network.
    Once any hop resolves PUBLIC, private hops are refused for the rest of the
    chain — the operator approved their own hosts, not whatever a public site
    picks next, so ``private -> public -> private`` cannot be used to steer the
    fetcher into internal endpoints of an attacker's choosing.  The NEVER lane
    is absolute throughout: approving a private origin says "this is my
    network", which is not a claim about the cloud metadata endpoint.

    The body is streamed under a *max_bytes* budget rather than buffered
    blind — ``client.get()`` would read an unbounded body into memory before
    any caller-side size cap could run.  The budget counts DECODED bytes
    (``iter_bytes`` runs after content-decoding), so a small gzip body cannot
    expand past it, and redirect-hop bodies are never read at all.  Callers
    keep their own tighter product caps; this ceiling only bounds a hostile
    or runaway response.  The realized response drops the wire-framing
    headers (content-encoding / content-length / transfer-encoding) that no
    longer describe the decoded content it carries.

    Raises ``ValueError`` for a blocked hop, an over-budget body, or a
    redirect chain past *max_redirects* (callers already route
    ``ValueError`` to their fetch-failed lane), and lets ``httpx``
    transport errors propagate unchanged.  ``resp.raise_for_status()``
    stays the caller's call.
    """
    current = url
    private_allowed = allow_private_origin
    with httpx.Client(
        headers={"User-Agent": user_agent},
        timeout=timeout,
        follow_redirects=False,
    ) as client:
        for _hop in range(max_redirects + 1):
            screen = screen_url(current)
            if screen.lane is AddressLane.NEVER:
                raise ValueError(screen.error)
            if screen.lane is AddressLane.PRIVATE and not private_allowed:
                raise ValueError(screen.error)
            if not screen.all_private:
                # The chain can no longer be shown to be inside the operator's
                # network, so private hops stop being allowed from here on.
                # There is deliberately no exemption for the origin host: an
                # earlier attempt to keep one let a public hop steer the fetcher
                # back into the approved host at a path of its choosing, and
                # made the grant re-entrant across same-host redirects with
                # fresh DNS each time. The caller refuses a mixed-record origin
                # outright instead, so a chain that gets here wholly private
                # stays that way or ends.
                private_allowed = False
            with client.stream("GET", current) as resp:
                if resp.status_code in _REDIRECT_STATUSES:
                    location = resp.headers.get("location")
                    if location:
                        current = str(httpx.URL(current).join(location))
                        continue  # leaves the with-block: hop body never read
                chunks: list[bytes] = []
                total = 0
                for chunk in resp.iter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        raise ValueError(
                            f"Blocked: response body exceeded the {max_bytes:,}-byte fetch limit"
                        )
                    chunks.append(chunk)
                headers = [
                    (k, v)
                    for k, v in resp.headers.items()
                    if k.lower() not in _STALE_FRAMING_HEADERS
                ]
                return httpx.Response(
                    status_code=resp.status_code,
                    headers=headers,
                    content=b"".join(chunks),
                    request=httpx.Request("GET", current),
                )
    raise ValueError(f"Blocked: more than {max_redirects} redirects")
