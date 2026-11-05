"""Web utilities — HTML to text, SSRF protection, and the guarded fetch."""

import dataclasses
import re
import socket
from contextlib import ExitStack
from html import unescape as _html_unescape
from html.entities import html5 as _html5_entities
from ipaddress import ip_address
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

# Elements whose content never appears as text: it runs to the first end tag of the same
# name, whatever markup it holds. A browser renders no iframe or noembed content either.
# noframes content stays text: on a frameset page it is the only text and link there are.
# A template that declares a shadow root is rendered instead (see _shadow_root).
_INVISIBLE_TAGS = frozenset({"iframe", "noembed", "noscript", "script", "style", "template"})
# Elements whose content is text, never markup, up to their end tag. Title and textarea
# decode character references in it; xmp shows it as written.
_TEXT_TAGS = {"textarea": True, "title": True, "xmp": False}
_RE_CONTENT_END = {
    name: re.compile(rf"</{name}[\t\n\f\r />]", re.IGNORECASE | re.ASCII)
    for name in (*_INVISIBLE_TAGS, *_TEXT_TAGS)
}
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
# The scan moves forward from one piece of markup to the next. Each pattern below matches
# at the position it is given, or searches forward from it for a fixed-width string, and
# markup still open at the end of the document ends the scan, so no character is read more
# than a few times however the page is built. Patterns that searched again from every "<"
# made unclosed tags and scripts cost time growing with the square of the page.
_RE_MARKUP_START = re.compile(r"<[a-zA-Z/!?]")
_RE_TAG_START = re.compile(r"<(/?)([a-zA-Z][^\t\n\f\r />]*+)")
_RE_COMMENT_END = re.compile(r"--!?>")
# Attributes as the HTML tokenizer reads them. A name runs to whitespace, "/", ">" or "=",
# only a name takes "=" and a value, and a quote opens the value only right after that "=",
# so a ">" between the quotes does not end the tag. A quote that never closes is read as
# part of an unquoted value, which ends at whitespace or ">". A browser would read the rest
# of the page as that value; here one stray quote does not hide the page.
_ATTR_NAME = r"[^\t\n\f\r />][^\t\n\f\r />=]*+"
_ATTR_VALUE = r"""(?:"[^"]*+"|'[^']*+'|[^\t\n\f\r >]*+)"""
_RE_TAG_REST = re.compile(
    rf"(?:[\t\n\f\r /]++|{_ATTR_NAME}(?:[\t\n\f\r ]*+=[\t\n\f\r ]*+{_ATTR_VALUE})?)*+>"
)
_RE_ATTRIBUTE = re.compile(rf"({_ATTR_NAME})(?:[\t\n\f\r ]*+=[\t\n\f\r ]*+({_ATTR_VALUE}))?")
_RE_CHARREF = re.compile(r"&(?:#[0-9]+;?|#[xX][0-9a-fA-F]+;?|[a-zA-Z][a-zA-Z0-9]*+;?)")
_RE_DECIMAL_REF = re.compile(r"&#0*([0-9]+)(;?)")
_REPLACEMENT_CHARACTER = chr(0xFFFD)
# Whitespace runs that change when collapsed: spaces and tabs other than one space, and
# spaces beside a line break. Matching only those keeps re.sub from building a piece per
# word.
_RE_WS = re.compile(r"(?: [ \t]|\t)[ \t]*+")
_RE_LINE_WS = re.compile(r" +\n *+| *\n +")
_RE_LINE_BREAKS = re.compile(r"\n{2,}")
_RE_BLANKLINES = re.compile(r"\n{3,}")

LINK_TARGET_CHAR_CAP = 2048
"""The longest link target the page text shows; a longer one keeps only its link text.

Every relative link writes out the address it resolves against, so without a cap a page of
short links under a long ``<base href>`` or served URL would repeat that address per link.
"""
_RESOLVE_BUDGET_CHARS = 2_000_000
"""The most characters of href, and of the base a relative one joins, one page's links are
resolved over.

Each resolution parses them, and a link refused afterwards (the page itself, a target over
the cap) adds no text, so the text budget alone would not bound this work. An href is
resolved once per base; past the budget, a link not resolved earlier keeps only its text,
and the first one to do so is preceded by a note saying why.
"""
_BUDGET_NOTE = (
    "\n\n[From here on, a link not seen earlier on the page shows only its text: the page has"
    " more links than one fetch resolves.]\n\n"
)
# The link targets a model can use. Script, data and phone targets keep only their text.
_LINK_SCHEMES = frozenset({"http", "https", "mailto"})
_RE_SCHEME = re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]*):")
# What a URL parser drops from an href before reading it: C0 controls and spaces at either
# end, and tabs and newlines anywhere.
_HREF_EDGE = "".join(map(chr, range(0x21)))
_HREF_TAB_NEWLINE = dict.fromkeys(map(ord, "\t\n\r"))
# hrefs that httpx2 resolves differently from a browser in a web page: a backslash before
# the query or fragment (a browser reads it as "/"), a run of three slashes, a leading ":",
# and an http or https scheme not followed by "//" and a host.
_RE_DIVERGENT_HREF = re.compile(r"[^?#\\]*+\\|/{3}|:|https?:(?!//[^/])", re.IGNORECASE | re.ASCII)
# The characters that end a [text](url) link, percent-encoded after a target's host, so a
# target cannot read as the end of its own link and the start of another.
_LINK_DELIMITERS = str.maketrans({"(": "%28", ")": "%29", "[": "%5B", "]": "%5D"})


def _unescape_text(text: str) -> str:
    """Decode the character references in page text as ``html.unescape`` does.

    A decimal reference past seven digits names no character, and one past 4,300 digits
    would make Python's integer conversion raise, so each becomes the replacement
    character first.
    """
    if "&#" in text:
        text = _RE_DECIMAL_REF.sub(_bounded_decimal_ref, text)
    return _html_unescape(text)


def _bounded_decimal_ref(match: re.Match[str]) -> str:
    digits = match.group(1)
    return _REPLACEMENT_CHARACTER if len(digits) > 7 else f"&#{digits}{match.group(2)}"


def _attribute(attributes: str, name: str) -> str | None:
    """Return the decoded value of a tag's first *name* attribute, or None without one.

    A value whose opening quote never closes counts as none.
    """
    for match in _RE_ATTRIBUTE.finditer(attributes):
        if match.group(1).lower() == name:
            value = match.group(2) or ""
            if value[:1] in ("'", '"'):
                if len(value) < 2 or value[-1] != value[0]:
                    return None
                value = value[1:-1]
            return _RE_CHARREF.sub(_attribute_charref, value)
    return None


def _attribute_charref(match: re.Match[str]) -> str:
    """Decode one character reference as HTML decodes it inside an attribute value.

    ``html.unescape`` decodes text, where "&region=us" begins with the registered sign. In
    an attribute, a named reference without its ";" stays literal when "=" or a letter or
    digit follows it, so a link's query string keeps its "&region".
    """
    ref = match.group()
    if ref.startswith("&#"):
        return _unescape_text(ref)
    name = ref[1:]
    if name in _html5_entities and (
        name.endswith(";") or match.string[match.end() : match.end() + 1] != "="
    ):
        return _html5_entities[name]
    return ref


def _clean_href(href: str) -> str:
    """The href as a URL parser reads it: C0 controls and spaces trimmed, tabs and newlines
    dropped."""
    return href.strip(_HREF_EDGE).translate(_HREF_TAB_NEWLINE)


def _join(base: httpx2.URL | None, href: str) -> httpx2.URL | None:
    """Resolve a cleaned *href* against *base*, or on its own when *base* is None.

    None covers an href that is no URL (a malformed IPv6 zone and a lone surrogate
    included) and one that httpx2 would resolve somewhere else (``_RE_DIVERGENT_HREF``).
    """
    if _RE_DIVERGENT_HREF.match(href):
        return None
    try:
        return httpx2.URL(href) if base is None else base.join(href)
    except (httpx2.InvalidURL, ValueError):  # UnicodeError is a ValueError
        return None


def _refused_authority(url: httpx2.URL) -> bool:
    """Whether a browser would refuse this http or https URL's host or port.

    httpx2 accepts an empty host and a port outside 0-65535. The host is read as its
    encoded bytes: ``URL.host`` decodes a host holding "xn--" on every read, and raises when
    that fails (a host over 253 characters, or another label its decoder rejects).
    """
    host, port = url.raw_host, url.port
    return not host or (port is not None and not 0 <= port <= 65535)


def _differs_in_a_browser(target: httpx2.URL) -> bool:
    """Whether a browser would refuse or rewrite this http or https target.

    Beyond a host or port it refuses (``_refused_authority``), httpx2 keeps a percent-encoded
    host as written, where a browser decodes it, and a path segment of encoded dots, which a
    browser reads as "." or "..", and it leaves "(" and ")" raw in the host and userinfo,
    where one would end the rendered link early.
    """
    return (
        _refused_authority(target)
        or any(mark in target.raw_host for mark in b"%()")
        or any(mark in target.userinfo for mark in b"()")
        or any(segment in (".", "..") for segment in target.path.split("/"))
    )


def _page_address(page_url: str) -> httpx2.URL | None:
    """The page's address without credentials or fragment, or None if it does not parse.

    Relative links resolve against it, and credentials in it would reach every one of them.
    The fragment goes before parsing: it plays no part in resolving a link, and a long one
    would push the address past httpx2's length limit.
    """
    try:
        return httpx2.URL(page_url.partition("#")[0]).copy_with(userinfo=b"")
    except (httpx2.InvalidURL, ValueError):
        return None


def _encode_delimiters(target: str, scheme: str) -> str:
    """Percent-encode ``( ) [ ]`` in *target* after an http or https host, keeping IPv6
    brackets, or everywhere for any other *scheme*, which has no host."""
    if scheme not in ("http", "https"):
        return target.translate(_LINK_DELIMITERS)
    head, _, rest = target.partition("://")
    host_end = min((i for i in map(rest.find, "/?#") if i >= 0), default=len(rest))
    return f"{head}://{rest[:host_end]}{rest[host_end:].translate(_LINK_DELIMITERS)}"


class _PageText:
    """The page's text as the scan writes it, with the link open in it, if any."""

    def __init__(self, page_url: str) -> None:
        self.pieces: list[str] = []
        self.size = 0
        self.foreign = 0  # open svg and math elements, inside which "/>" closes any element
        self._page = _page_address(page_url)
        self._page_text = "" if self._page is None else str(self._page)
        # None: relative hrefs have nothing to resolve against and keep only their text.
        self._base = self._page
        self._base_text = self._page_text
        self._base_seen = False
        self._resolved: dict[str, str | None] = {}  # each parsed href's target under this base
        self._budget = _RESOLVE_BUDGET_CHARS
        self._budget_noted = False
        self._link_start = -1  # where the open link's text begins in pieces; -1 if none
        self._link_size = 0
        self._target = ""

    def add(self, piece: str) -> None:
        self.pieces.append(piece)
        self.size += len(piece)

    def set_base(self, attributes: str) -> None:
        """Resolve the links after the page's first ``<base href>`` against it.

        As in a browser, a base that does not parse (a host or port a browser refuses
        included), or is a data: or javascript: URL, leaves the page's address in place;
        any other scheme becomes the base, so relative links under an ftp: base lead nowhere
        a model can use and keep only their text. A browser resolves every link in the page
        against that base, those before it too; the scan has written those already.
        """
        if self._base_seen:
            return
        href = _attribute(attributes, "href")
        if href is None:
            return
        self._base_seen = True
        base = _join(self._page, _clean_href(href))
        if (
            base is None
            or base.scheme in ("", "data", "javascript")
            or (base.scheme in ("http", "https") and _refused_authority(base))
        ):
            return
        self._base = base
        self._base_text = str(base)
        self._resolved.clear()

    def open(self, attributes: str) -> None:
        """Start a link. An ``<a>`` inside an open one closes it first, as in a browser."""
        self.close()
        href = _attribute(attributes, "href")
        target = None if href is None else self._target_for(href)
        if target is not None:
            self._link_start = len(self.pieces)
            self._link_size = self.size
            self._target = target

    def _target_for(self, href: str) -> str | None:
        """The target to show for *href*, or None when the link keeps only its text.

        An href parsed before keeps its result. One settled without a parse is not kept,
        so the memo holds no more entries than the budget lets the scan parse.
        """
        if href in self._resolved:
            return self._resolved[href]
        cleaned = _clean_href(href)
        scheme = _RE_SCHEME.match(cleaned)
        if scheme is not None and scheme.group(1).lower() not in _LINK_SCHEMES:
            return None  # a script, data or phone link
        if scheme is None and self._base is None:
            return None
        if self._budget <= 0:
            if not self._budget_noted:
                self._budget_noted = True
                self.add(_BUDGET_NOTE)
            return None
        # An absolute href resolves on its own, whatever the base, so it costs only its
        # own length; a relative one also parses the base it joins.
        self._budget -= len(cleaned) + (0 if scheme else len(self._base_text))
        shown = self._resolve(cleaned, absolute=scheme is not None)
        self._resolved[href] = shown
        return shown

    def _resolve(self, href: str, *, absolute: bool) -> str | None:
        """Parse a cleaned *href* into the target to show, or None to keep only its text."""
        target = _join(None if absolute else self._base, href)
        if target is None or target.scheme not in _LINK_SCHEMES:
            return None
        if target.scheme != "mailto" and _differs_in_a_browser(target):
            return None
        resolved = str(target)
        # The first "#" in a URL begins its fragment. A link to a named place in this page
        # shows just "#fragment", which resolves against the page's address without
        # repeating it; a link to the page itself keeps only its text.
        address, _, fragment = resolved.partition("#")
        if address == self._page_text:
            shown = f"#{fragment.translate(_LINK_DELIMITERS)}" if fragment else ""
        else:
            shown = _encode_delimiters(resolved, target.scheme)
        return shown if 0 < len(shown) <= LINK_TARGET_CHAR_CAP else None

    def close(self) -> None:
        """Replace the open link's text with the link itself."""
        if self._link_start < 0:
            return
        raw = "".join(self.pieces[self._link_start :])
        del self.pieces[self._link_start :]
        self.size = self._link_size
        self._link_start = -1
        # The text keeps its line breaks, one between each pair of its lines.
        text = _RE_LINE_BREAKS.sub("\n", _RE_LINE_WS.sub("\n", _RE_WS.sub(" ", raw))).strip()
        link = f"[{text}]({self._target})" if text and text != self._target else f"<{self._target}>"
        # The text's outer whitespace stays outside the link, keeping it apart from what
        # surrounds it: a space from the words around it, a newline from the blocks.
        lead = raw[: len(raw) - len(raw.lstrip())]
        trail = raw[len(raw.rstrip()) :]
        self.add(f"{lead}{link}{trail}")


def _comment_end(html: str, start: int) -> int:
    """Return where the comment opened at *start* ends: at "-->" or "--!>", as in a browser.

    A "--!>" that overlaps the opening "<!--", as in "<!--!>" and "<!---!>", leaves the
    comment open, and a comment still open at the end of the document hides the rest.
    """
    close = _RE_COMMENT_END.search(html, start + 2)
    while close is not None and close.group() == "--!>" and close.start() < start + 4:
        close = _RE_COMMENT_END.search(html, close.start() + 1)
    return len(html) if close is None else close.end()


def _text_content(html: str, start: int, name: str, page: _PageText) -> int:
    """Add a title, textarea or xmp element's content as text; return where it ends.

    The content is text up to the element's end tag, never markup. Without an end tag, the
    rest of the document is that text, as a browser reads it.
    """
    close_tag = _RE_CONTENT_END[name].search(html, start)
    end = len(html) if close_tag is None else close_tag.start()
    content = html[start:end]
    page.add(_unescape_text(content) if _TEXT_TAGS[name] else content)
    return end


def _shadow_root(name: str, attributes: str) -> bool:
    """Whether a start tag is a template that declares a shadow root a browser renders."""
    if name != "template":
        return False
    mode = _attribute(attributes, "shadowrootmode")
    return mode is not None and mode.lower() in ("open", "closed")


def _render_markup(html: str, start: int, page: _PageText, xml: bool) -> int:
    """Render the markup at *start* into *page*; return where the text after it begins.

    Markup still open at the end of the document hides the rest of it, as in a browser,
    and the scan stops there rather than searching again from the next "<".
    """
    if html.startswith("<!--", start):
        return _comment_end(html, start)
    tag = _RE_TAG_START.match(html, start)
    if tag is None:
        # A doctype, a processing instruction or a malformed end tag shows nothing.
        close = html.find(">", start + 2)
        return len(html) if close < 0 else close + 1
    rest = _RE_TAG_REST.match(html, tag.end())
    if rest is None:
        return len(html)
    end = rest.end()
    is_end, name = bool(tag.group(1)), tag.group(2).lower()
    self_closed = html[end - 2] == "/"
    if name in ("svg", "math"):
        if is_end:
            page.foreign = max(0, page.foreign - 1)
        elif not self_closed:
            page.foreign += 1
    # In XHTML, and inside svg or math on any page, a start tag closed by "/>" is an empty
    # element, a script's or a style's included, as a browser reads it.
    empty = self_closed and (xml or page.foreign > 0)
    if name in _NEWLINE_TAGS:
        page.add("\n")
    elif name == "a":
        if is_end:
            page.close()
        else:
            page.open(html[tag.end() : end - 1])
            if empty:
                page.close()
    elif is_end:
        pass
    elif name == "base":
        page.set_base(html[tag.end() : end - 1])
    elif empty:
        pass
    elif name in _TEXT_TAGS:
        return _text_content(html, end, name, page)
    elif name in _INVISIBLE_TAGS and not _shadow_root(name, html[tag.end() : end - 1]):
        close_tag = _RE_CONTENT_END[name].search(html, end)
        return len(html) if close_tag is None else close_tag.start()
    return end


def strip_html(
    html: str,
    *,
    page_url: str,
    stop_after_chars: int,
    xml: bool = False,
) -> tuple[str, bool]:
    """Convert HTML to plain text that keeps its block structure and its links' targets.

    Block-level boundaries (and ``<br>``) become newlines while other tags are dropped, so
    paragraphs, headings, list items and table cells stay apart instead of running
    together. A link becomes ``[text](url)``, or ``<url>`` when it has no text of its own,
    so a model asked for a link can copy its URL rather than guess one; its text keeps its
    line breaks. A target is resolved against *page_url*, the address the page was served
    from, without its credentials, or against the page's first ``<base href>``. A link to
    a named place in the page shows just ``#fragment``. After a target's host, ``( ) [ ]``
    are percent-encoded.

    A link keeps only its text wherever its target could mislead: when it leads to the page
    itself, to anything but an http, https or mailto URL, or to a target longer than
    ``LINK_TARGET_CHAR_CAP``; when a browser would refuse, rewrite or resolve its href
    differently; and once the page's links have used up the work one conversion spends
    resolving them (``_RESOLVE_BUDGET_CHARS``), unless the same href was resolved before,
    after a note saying so.

    Comments and the content of script, style, template, noscript, iframe and noembed
    elements are dropped, and title, textarea and xmp content is text. With *xml*, for an
    XHTML page, a start tag closed by "/>" is an empty element, as it is inside svg and
    math on any page.

    The scan stops once the text passes *stop_after_chars*, which a single long run of text
    can carry it well past; the caller cuts the text to its own limit. The second value
    says whether the text covers the whole page.
    """
    page = _PageText(page_url)
    pos = 0
    while page.size <= stop_after_chars:
        markup = _RE_MARKUP_START.search(html, pos)
        if markup is None:
            page.add(_unescape_text(html[pos:]))
            pos = len(html)
            break
        start = markup.start()
        if start > pos:
            page.add(_unescape_text(html[pos:start]))
        pos = _render_markup(html, start, page, xml)
    page.close()
    text = "".join(page.pieces)
    text = _RE_WS.sub(" ", text)
    text = _RE_LINE_WS.sub("\n", text)
    text = _RE_BLANKLINES.sub("\n\n", text)
    return text.strip(), pos >= len(html)


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
_REQUEST_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
"""Headers every hop of a guarded fetch sends alongside its User-Agent.

``Accept`` prefers HTML while still accepting any other type, and
``Accept-Language`` prefers English. URL targets of open_preview send them
too: the tool mostly shows the model's own files, and the docs note that an
image host may answer this ``Accept`` with its HTML page.
"""
_FETCH_METADATA = {
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
}
"""Navigation metadata for hops to potentially trustworthy URLs.

It describes a top-level navigation, a compatibility choice for a server-side
fetcher; the User-Agent still identifies Turnstone. As in browsers, each hop is
judged by its own URL, so an https page redirecting to plain http sends none on
the http hop. ``Sec-Fetch-User`` stays unset: ``?1`` asserts a user-activated
navigation, and this fetcher cannot tell whether a person approved the call
(task agents and auto-approval run it unattended). ``Sec-Fetch-Site: none``
says no site initiated the request, which holds for a tool call, and
resource-isolation policies, as usually written, admit a request without
Fetch Metadata as well, so the header grants nothing that omitting it would
not.
"""


def _potentially_trustworthy(url: httpx2.URL) -> bool:
    """Return whether browsers would send Fetch Metadata to *url*.

    That is https, or a loopback host: a ``localhost`` name (a trailing dot
    included) or a loopback address. IPv4 shorthand such as ``127.1`` counts as
    the address it resolves to, the form a browser's URL parser would have
    produced. Other schemes never reach the guarded fetch's client.
    """
    if url.scheme == "https":
        return True
    host = url.host.lower().rstrip(".")
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        address = ip_address(host)
    except ValueError:
        pass
    else:
        # The spec counts 127.0.0.0/8 and ::1 as loopback. Python also counts an
        # IPv4-mapped address such as ::ffff:127.0.0.1, which falls outside both.
        return address.is_loopback and not getattr(address, "ipv4_mapped", None)
    try:
        return ip_address(socket.inet_aton(host)).is_loopback
    except OSError:
        return False


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

    EVERY hop is screened, in every mode, the first included: a screen run before approval must not
    resolve a hostname (see :func:`screen_url_offline`), so this is where a hostname target is first
    looked up.  ``allow_private_origin`` widens which lanes are acceptable, it does not turn
    screening off: the URL tools set it only when the operator opted in AND the approval prompt
    marked the original target as a private-network request, so the approval gate saw and approved
    that private URL, and the doctor sets it for every health probe, since reaching the operator's
    own nodes is its job.

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
    ``ValueError`` for an over-budget body, a redirect chain past
    *max_redirects* or a redirect ``Location`` that cannot be resolved
    (callers already route ``ValueError`` to their fetch-failed lane), and
    lets ``httpx2`` transport errors propagate unchanged.
    ``resp.raise_for_status()`` stays the caller's call.
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
                # The chain can no longer be shown to be inside the operator's network, so private
                # hops stop being allowed from here on. There is deliberately no exemption for the
                # origin host: an earlier attempt to keep one let a public hop steer the fetcher
                # back into the approved host at a path of its choosing, and made the grant
                # re-entrant across same-host redirects with fresh DNS each time. The URL tools
                # refuse a mixed-record origin outright instead, so a chain that gets here wholly
                # private stays that way or ends; the doctor accepts one, and this latch then
                # refuses every private hop after it.
                private_allowed = False
            if client is None:
                transport = PinnedTransport()
                client = stack.enter_context(
                    httpx2.Client(
                        headers={**_REQUEST_HEADERS, "User-Agent": user_agent},
                        timeout=timeout,
                        follow_redirects=False,
                        transport=transport,
                        trust_env=False,
                    )
                )
            assert transport is not None
            transport.pin(current, screen.hostname, screen.addresses)
            metadata = _FETCH_METADATA if _potentially_trustworthy(parsed) else None
            try:
                with client.stream("GET", current, headers=metadata) as resp:
                    if resp.status_code in _REDIRECT_STATUSES:
                        location = resp.headers.get("location")
                        if location:
                            current = str(parsed.join(location))
                            continue  # leaves the with-block: hop body never read
                    chunks: list[bytes] = []
                    total = 0
                    for chunk in resp.iter_bytes():
                        total += len(chunk)
                        if total > max_bytes:
                            raise ValueError(
                                "Blocked: response body exceeded the "
                                f"{max_bytes:,}-byte fetch limit"
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
            except httpx2.InvalidURL as exc:
                # httpx2 resolves every redirect's Location while building the
                # response, even one it will not follow, and a Location such as
                # "http:foo" fails there with InvalidURL, which is not a RequestError.
                raise ValueError(f"Invalid URL in location header: {exc}.") from None
    raise ValueError(f"Blocked: more than {max_redirects} redirects")
