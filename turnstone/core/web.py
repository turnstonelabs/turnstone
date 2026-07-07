"""Web utilities — HTML stripping, SSRF protection, and the guarded fetch."""

import ipaddress
import re
import socket
from html import unescape as _html_unescape
from urllib.parse import urlparse

import httpx

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


def check_ssrf(url: str) -> str | None:
    """Return error string if URL resolves to a private/link-local address, else None.

    Checks both IPv4 and IPv6 addresses via getaddrinfo to prevent bypasses
    using IPv6 loopback (``::1``), link-local (``fe80::``), or unique-local
    (``fd00::``/``fc00::``) addresses.
    """
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname
        if not hostname:
            return "Invalid URL: no hostname"
        # Resolve all address families (IPv4 + IPv6)
        results = socket.getaddrinfo(hostname, parsed.port or 80, proto=socket.IPPROTO_TCP)
        for _family, _type, _proto, _canonname, sockaddr in results:
            addr = str(sockaddr[0])
            # Strip IPv6 zone/scope identifier (e.g. "fe80::1%lo0")
            addr_clean = addr.split("%", 1)[0] if "%" in addr else addr
            try:
                ip = ipaddress.ip_address(addr_clean)
            except ValueError:
                return f"Blocked: unable to parse resolved address ({addr})"
            # Normalize IPv4-mapped IPv6 (e.g. ::ffff:127.0.0.1)
            if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
                ip = ip.ipv4_mapped
            if ip.is_private or ip.is_loopback or ip.is_link_local:
                return f"Blocked: URL resolves to private/internal address ({addr})"
    except (socket.gaierror, OSError):
        pass  # DNS failure — let the actual fetch handle it
    return None


def fetch_with_ssrf_guard(
    url: str,
    *,
    timeout: float,
    user_agent: str = "turnstone/1.0",
    max_redirects: int = 5,
) -> httpx.Response:
    """GET *url* following redirects manually, SSRF-screening EVERY hop.

    ``httpx.get(follow_redirects=True)`` checks nothing between hops — a
    public URL that 302s into private address space (cloud metadata, an
    internal admin endpoint) would be fetched before any post-hoc check runs,
    executing the private-network request even if the response is later
    discarded.  Here each hop's URL is screened BEFORE its request is issued.

    Raises ``ValueError`` for a blocked hop or a redirect chain past
    *max_redirects* (callers already route ``ValueError`` to their
    fetch-failed lane), and lets ``httpx`` transport errors propagate
    unchanged.  ``resp.raise_for_status()`` stays the caller's call.
    """
    current = url
    with httpx.Client(
        headers={"User-Agent": user_agent},
        timeout=timeout,
        follow_redirects=False,
    ) as client:
        for _hop in range(max_redirects + 1):
            ssrf_err = check_ssrf(current)
            if ssrf_err:
                raise ValueError(ssrf_err)
            resp = client.get(current)
            if resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get("location")
                if location:
                    current = str(httpx.URL(current).join(location))
                    continue
            return resp
    raise ValueError(f"Blocked: more than {max_redirects} redirects")
