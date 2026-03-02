"""Web utilities — HTML stripping and SSRF protection."""

import ipaddress
import re
import socket
from html import unescape as _html_unescape
from urllib.parse import urlparse

_RE_TAGS = re.compile(r"<[^>]+>")
_RE_WS = re.compile(r"[ \t]+")
_RE_BLANKLINES = re.compile(r"\n{3,}")


def strip_html(html: str) -> str:
    """Convert HTML to plain text: strip tags, decode entities, collapse whitespace."""
    text = _RE_TAGS.sub("", html)
    text = _html_unescape(text)
    text = _RE_WS.sub(" ", text)
    text = _RE_BLANKLINES.sub("\n\n", text)
    return text.strip()


def check_ssrf(url: str) -> str | None:
    """Return error string if URL resolves to a private/link-local address, else None."""
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname
        if not hostname:
            return "Invalid URL: no hostname"
        addr = socket.gethostbyname(hostname)
        ip = ipaddress.ip_address(addr)
        if ip.is_private or ip.is_loopback or ip.is_link_local:
            return f"Blocked: URL resolves to private/internal address ({addr})"
    except (socket.gaierror, ValueError):
        pass  # DNS failure or invalid IP — let the actual fetch handle it
    return None
