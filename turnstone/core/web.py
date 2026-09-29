"""Web utilities — HTML stripping, SSRF protection, and the guarded fetch."""

import dataclasses
import re
from contextlib import ExitStack
from html import unescape as _html_unescape
from urllib.parse import urlparse

import httpx2

from turnstone.core._web_transport import PinnedTransport, proxy_required
from turnstone.core.ip_classify import (
    BLOCKED_HOSTNAMES,
    AddressLane,
    IPAddress,
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
    addresses: tuple[IPAddress, ...] = ()
    """The classified addresses, in resolver order, retained for the connection."""
    hostname: str = ""
    """The encoded hostname these addresses were screened for."""


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

    Fails closed: a resolution failure leaves no addresses the caller can
    safely contact. Malformed URLs are refusals too — every exception path
    returns a verdict rather than raising, because the callers screen
    model-supplied URLs and one of them prepares tools outside any ``try``.
    """
    hostname = _screened_hostname(url)
    if isinstance(hostname, UrlScreen):
        return hostname
    try:
        classified = resolve_and_classify(hostname)
    except ResolutionError as exc:
        return UrlScreen(AddressLane.NEVER, f"Blocked: {exc}", False)
    return _worst_lane(classified, hostname)


def screen_url_offline(url: str) -> UrlScreen | None:
    """Screen *url* as far as it can be without a DNS lookup, else ``None``.

    A lookup is itself outbound traffic: the query carries the hostname to the
    resolver and on to whoever serves that name, so a check that runs before a
    request is approved must not make one. What is decidable locally gets the
    verdict :func:`screen_url` gives it: a malformed URL, a metadata hostname,
    an IP literal. A hostname gets ``None`` rather than a PUBLIC verdict, since
    its lane is unknown until :func:`fetch_with_ssrf_guard` resolves it for the
    approved request.
    """
    hostname = _screened_hostname(url)
    if isinstance(hostname, UrlScreen):
        return hostname
    try:
        classified = resolve_and_classify(hostname, numeric_only=True)
    except ResolutionError:
        return None
    return _worst_lane(classified, hostname)


def _screened_hostname(url: str) -> str | UrlScreen:
    """Return the hostname *url* names, or the verdict refusing it unresolved."""
    try:
        parsed = urlparse(url)
        hostname = httpx2.URL(url).raw_host.decode("ascii")
        # Touched, not used: ``urlsplit.port`` parses lazily and raises for an
        # out-of-range value, which must become a refusal rather than escape.
        # It is not passed to resolution — a numeric service does not change
        # which addresses come back, and classification looks only at those.
        _ = parsed.port
    except (ValueError, httpx2.InvalidURL):
        return UrlScreen(AddressLane.NEVER, f"Blocked: malformed URL ({url})", False)
    if not hostname:
        return UrlScreen(AddressLane.NEVER, "Invalid URL: no hostname", False)
    if hostname.lower() in BLOCKED_HOSTNAMES:
        return UrlScreen(
            AddressLane.NEVER, f"Blocked: URL names a metadata host ({hostname})", False
        )
    return hostname


def _worst_lane(classified: list[tuple[AddressLane, IPAddress]], hostname: str) -> UrlScreen:
    """Fold classified addresses into one verdict: the worst lane and its refusal."""
    worst, offender = max(classified, key=lambda item: item[0])
    all_private = all(lane is AddressLane.PRIVATE for lane, _ in classified)
    addresses = tuple(dict.fromkeys(addr for _lane, addr in classified))

    if worst is AddressLane.PUBLIC:
        return UrlScreen(AddressLane.PUBLIC, None, False, addresses, hostname)
    if worst is AddressLane.NEVER:
        return UrlScreen(
            worst,
            "Blocked: URL resolves to a link-local/multicast/unspecified/"
            f"reserved/metadata address ({describe_address(offender)})",
            all_private,
            addresses,
            hostname,
        )
    return UrlScreen(
        worst,
        f"Blocked: URL resolves to private/internal address ({describe_address(offender)})",
        all_private,
        addresses,
        hostname,
    )


FETCH_BYTE_CEILING = 32 * 1024 * 1024
"""Anti-OOM backstop on a guarded fetch's decoded body (see fetch_with_ssrf_guard)."""

_REDIRECT_STATUSES = (301, 302, 303, 307, 308)
_STALE_FRAMING_HEADERS = frozenset({"content-encoding", "content-length", "transfer-encoding"})


class UrlBlockedError(ValueError):
    """A guarded fetch refused a hop before requesting it.

    Still a ``ValueError``, so a caller that routes blocked hops to its
    fetch-failed lane keeps working. ``screen`` is the verdict and ``hop`` its
    place in the chain: 0 is the URL the caller asked for, 1 and up are
    redirect targets. A caller can then word a refusal of its own target the
    way it would have before approval, and a redirect's as a failed fetch.
    """

    def __init__(self, screen: UrlScreen, hop: int) -> None:
        super().__init__(screen.error)
        self.screen = screen
        self.hop = hop


def fetch_with_ssrf_guard(
    url: str,
    *,
    timeout: float,
    user_agent: str = "turnstone/1.0",
    max_redirects: int = 5,
    allow_private_origin: bool = False,
    max_bytes: int = FETCH_BYTE_CEILING,
    first_hop_screen: UrlScreen | None = None,
) -> httpx2.Response:
    """GET *url* following redirects manually, SSRF-screening EVERY hop.

    ``httpx2.get(follow_redirects=True)`` checks nothing between hops — a
    public URL that 302s into private address space (cloud metadata, an
    internal admin endpoint) would be fetched before any post-hoc check runs,
    executing the private-network request even if the response is later
    discarded.  Here each hop's URL is screened BEFORE its request is issued.

    EVERY hop is screened, in every mode, the first included: a screen run
    before approval must not resolve a hostname (see
    :func:`screen_url_offline`), so this is where a hostname target is first
    looked up.  ``allow_private_origin`` widens which lanes are acceptable,
    it does not turn screening off: the caller sets it only when the operator
    opted in AND the approval prompt marked the original target as a
    private-network request, so the approval gate saw and approved that
    private URL.

    ``first_hop_screen`` may supply the caller's execution-time verdict on
    this exact URL, so a private-grant recheck and hop 0 use the same answer.
    Redirects are always screened afresh. The transport connects only to the
    screened addresses, retaining the original hostname for HTTP routing and
    TLS verification. It never resolves that hostname again to connect.
    Proxy-routed requests are refused because a proxy could resolve the
    destination independently; ``NO_PROXY`` exclusions can connect directly.

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
    (``iter_bytes`` runs after content-decoding, which yields bounded pieces),
    so a small compressed body cannot expand past it, and redirect-hop bodies
    are never read at all.  Callers keep their own tighter product caps; this
    ceiling only bounds a hostile or runaway response.  The realized response
    drops the wire-framing headers (content-encoding / content-length /
    transfer-encoding) that no longer describe the decoded content it carries.

    Raises :class:`UrlBlockedError` (a ``ValueError``) for a blocked hop and
    ``ValueError`` for an over-budget body or a redirect chain past
    *max_redirects* (callers already route ``ValueError`` to their
    fetch-failed lane), and lets ``httpx2`` transport errors propagate
    unchanged.  ``resp.raise_for_status()`` stays the caller's call.
    """
    current = url
    private_allowed = allow_private_origin
    with ExitStack() as stack:
        client = None
        transport = None
        for hop in range(max_redirects + 1):
            try:
                parsed = httpx2.URL(current)
            except httpx2.InvalidURL:
                screen = UrlScreen(AddressLane.NEVER, f"Blocked: malformed URL ({current})", False)
                raise UrlBlockedError(screen, hop) from None
            if proxy_required(parsed):
                raise ValueError(
                    "Blocked: URL fetches cannot use a proxy because the connection must use"
                    " the screened addresses. Configure NO_PROXY for this host to connect directly."
                )
            screen = (
                first_hop_screen
                if hop == 0 and first_hop_screen is not None
                else screen_url(current)
            )
            if screen.lane is AddressLane.NEVER:
                raise UrlBlockedError(screen, hop)
            if screen.lane is AddressLane.PRIVATE and not private_allowed:
                raise UrlBlockedError(screen, hop)
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
            if client is None:
                transport = PinnedTransport()
                client = stack.enter_context(
                    httpx2.Client(
                        headers={"User-Agent": user_agent},
                        timeout=timeout,
                        follow_redirects=False,
                        transport=transport,
                        trust_env=False,
                    )
                )
            assert transport is not None
            transport.pin(current, screen.hostname, screen.addresses)
            with client.stream("GET", current) as resp:
                if resp.status_code in _REDIRECT_STATUSES:
                    location = resp.headers.get("location")
                    if location:
                        current = str(httpx2.URL(current).join(location))
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
                return httpx2.Response(
                    status_code=resp.status_code,
                    headers=headers,
                    content=b"".join(chunks),
                    request=httpx2.Request("GET", current),
                )
    raise ValueError(f"Blocked: more than {max_redirects} redirects")
