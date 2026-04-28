#!/usr/bin/env python3
"""CSS specificity audit for cascade-flip regressions after #431.

Scans a fixed list of stylesheets, computes the specificity of every selector,
finds the static HTML files referenced by the project, and flags every place
where an element matched by an ID-scoped (or tag-chained, or body-class scoped)
rule is *also* matched by a bare-class rule and the higher-specificity rule sets
a property in conflict with the bare class.

This was prompted by PR #431 which stripped a class-tier of specificity from ~400
rules; two cascade flips (`#header` outranking `.appbar`, `#header h1`
outranking `.appbar-title`) were caught visually and fixed in review.  This
audit looks for any others lurking in the merged tree.

Scope:
- **Per-page stylesheet manifest** (`PAGE_STYLESHEETS`): each HTML file only
  loads a specific subset of CSS files; the audit honours that to avoid
  cross-page false positives.
- **JS literal scanner**: regex-scrapes runtime-built `<tag id="..." class="..."`
  literals from JS files; conflicts on shared-JS elements only fire when EVERY
  page that hosts the element has the conflict (intersection of stylesheets).
- **State-pseudo gating**: a hover-state rule overriding a resting-state base
  rule is intentional, not a flip.  The audit only flags conflicts where the
  winner's state-set is a subset of the bare class's state-set (winner applies
  in every state where bare applies).
- **!important** is honoured: an !important author rule beats a non-important
  one regardless of specificity; both at !important fall back to specificity.
- **Shorthand expansion**: `font` / `padding` / `margin` / `border` / `background`
  shorthands get virtual-expanded into the longhands they imply, so a rule with
  `font: inherit` correctly conflicts with another rule's `font-family`.
- `WATCH_PROPERTIES` filters declarations to layout/visual properties whose
  accidental overrides produce user-visible bugs; pass `--all-properties` to
  scan every declaration.

Usage:
    python scripts/css_specificity_audit.py [--quiet] [--all-tiers] [--all-properties]

`--all-tiers`: also flag class-chain / tag-chain / multi-class wins (default:
ID tier only).  `--all-properties`: scan every declaration, not just the watch
list.  Exit status: 0 if clean, 1 if any findings are reported.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from collections.abc import Iterable

# -- file lists -----------------------------------------------------------------

REPO = Path(__file__).resolve().parents[1]

CSS_FILES = [
    "turnstone/shared_static/base.css",
    "turnstone/shared_static/ui-base.css",
    "turnstone/shared_static/chat.css",
    "turnstone/shared_static/cards.css",
    "turnstone/console/static/style.css",
    "turnstone/console/static/coordinator/coordinator.css",
    "turnstone/ui/static/style.css",
]

# Per-page stylesheet manifest — only these CSS files apply to elements found
# in this HTML file and the JS files it loads.  Mirrors the <link rel> tags in
# each index.html.  Without this, the audit pools rules across pages and
# reports false positives (e.g., a rule from console/static/style.css matching
# an element on the UI page where that sheet is never loaded).
PAGE_STYLESHEETS: dict[str, list[str]] = {
    "turnstone/console/static/index.html": [
        "turnstone/shared_static/base.css",
        "turnstone/shared_static/ui-base.css",
        "turnstone/shared_static/chat.css",
        "turnstone/shared_static/cards.css",
        "turnstone/console/static/style.css",
    ],
    "turnstone/console/static/coordinator/index.html": [
        "turnstone/shared_static/base.css",
        "turnstone/shared_static/ui-base.css",
        "turnstone/shared_static/chat.css",
        "turnstone/console/static/style.css",
        "turnstone/console/static/coordinator/coordinator.css",
    ],
    "turnstone/ui/static/index.html": [
        "turnstone/shared_static/base.css",
        "turnstone/shared_static/ui-base.css",
        "turnstone/shared_static/chat.css",
        "turnstone/shared_static/cards.css",
        "turnstone/ui/static/style.css",
    ],
}

# JS-emitted elements: which page do they belong to?  Map JS file → owning
# HTML file.  Shared JS (auth/kb/renderer/etc.) appears on every page; we
# default those to the "worst case" smallest-stylesheet-set page so a finding
# is only reported when EVERY page that includes the JS has the conflict.
JS_FILES: dict[str, list[str]] = {
    # Owned-per-page JS — definite stylesheet set.
    "turnstone/console/static/admin.js": [
        "turnstone/console/static/index.html",
    ],
    "turnstone/console/static/app.js": [
        "turnstone/console/static/index.html",
    ],
    "turnstone/console/static/governance.js": [
        "turnstone/console/static/index.html",
    ],
    "turnstone/console/static/coordinator/coordinator.js": [
        "turnstone/console/static/coordinator/index.html",
    ],
    "turnstone/ui/static/app.js": [
        "turnstone/ui/static/index.html",
    ],
    # Shared JS — emit elements on multiple pages; check against every page
    # that loads it (intersection of conflicts).
    "turnstone/shared_static/auth.js": list(PAGE_STYLESHEETS),
    "turnstone/shared_static/cards.js": list(PAGE_STYLESHEETS),
    "turnstone/shared_static/composer.js": list(PAGE_STYLESHEETS),
    "turnstone/shared_static/kb.js": list(PAGE_STYLESHEETS),
    "turnstone/shared_static/renderer.js": list(PAGE_STYLESHEETS),
    "turnstone/shared_static/toast.js": list(PAGE_STYLESHEETS),
}

# Properties whose conflicting values are "important" — visual/layout properties
# whose accidental override produces user-visible bugs.  We don't flag a conflict
# on `color` if both rules set the same colour token, etc.; we flag conflicts on
# any of these where the values DIFFER.
WATCH_PROPERTIES: set[str] = {
    "background",
    "background-color",
    "background-image",
    "color",
    "border",
    "border-radius",
    "border-color",
    "border-width",
    "border-style",
    "border-bottom",
    "border-top",
    "border-left",
    "border-right",
    "border-bottom-color",
    "border-top-color",
    "padding",
    "padding-top",
    "padding-right",
    "padding-bottom",
    "padding-left",
    "margin",
    "margin-top",
    "margin-right",
    "margin-bottom",
    "margin-left",
    "display",
    "position",
    "top",
    "right",
    "bottom",
    "left",
    "width",
    "height",
    "min-width",
    "min-height",
    "max-width",
    "max-height",
    "font",
    "font-family",
    "font-size",
    "font-weight",
    "font-style",
    "line-height",
    "letter-spacing",
    "text-align",
    "text-decoration",
    "text-transform",
    "flex",
    "flex-direction",
    "justify-content",
    "align-items",
    "gap",
    "grid-template-columns",
    "grid-template-rows",
    "grid-area",
    "opacity",
    "visibility",
    "z-index",
    "box-shadow",
    "box-sizing",
    "outline",
    "transform",
    "transition",
}


# -- CSS parser -----------------------------------------------------------------


@dataclass
class CSSRule:
    selectors: list[str]
    declarations: dict[str, tuple[str, bool]]  # property -> (value, important)
    source_file: str
    line_no: int
    media: str | None = None

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"CSSRule({self.selectors!r}, file={self.source_file}:{self.line_no})"


def _strip_comments(text: str) -> str:
    """Strip CSS comments while preserving newlines so line numbers stay
    accurate."""

    def repl(m: re.Match[str]) -> str:
        return "\n" * m.group(0).count("\n")

    return re.sub(r"/\*.*?\*/", repl, text, flags=re.DOTALL)


def _split_top_level_commas(s: str) -> list[str]:
    out: list[str] = []
    cur: list[str] = []
    depth = 0
    for ch in s:
        if ch == "(":
            depth += 1
            cur.append(ch)
        elif ch == ")":
            depth -= 1
            cur.append(ch)
        elif ch == "," and depth == 0:
            out.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    last = "".join(cur).strip()
    if last:
        out.append(last)
    return out


_IMPORTANT_RE = re.compile(r"\s*!\s*important\s*$", re.IGNORECASE)


def _parse_declarations(block: str) -> dict[str, tuple[str, bool]]:
    """Parse the contents of `{ ... }` into a property->(value, important) dict.

    Multiple instances of the same property keep the *last* value (CSS
    cascade-within-rule winner).  Whitespace normalised.  `!important` is
    extracted and reflected in the boolean.
    """
    decls: dict[str, tuple[str, bool]] = {}
    for raw in re.split(r";(?![^()]*\))", block):
        raw = raw.strip()
        if not raw or ":" not in raw:
            continue
        prop, _, value = raw.partition(":")
        prop = prop.strip().lower()
        value = re.sub(r"\s+", " ", value.strip())
        if not prop or prop.startswith("--"):
            continue
        important = False
        m = _IMPORTANT_RE.search(value)
        if m:
            important = True
            value = value[: m.start()].strip()
        # Delete-then-insert so the last occurrence of `prop` lands at the
        # *end* of the dict, not in its original slot.  Downstream
        # `_expanded_declarations` iterates in dict order to mirror CSS
        # within-rule cascade; without this re-ordering, a sequence like
        # `font-size: 13px; font: inherit; font-size: 12px;` would iterate
        # as (font-size, font) and resolve to font-size: inherit instead
        # of the correct 12px.
        if prop in decls:
            del decls[prop]
        decls[prop] = (value, important)
    return decls


def parse_css(text: str, source_file: str) -> list[CSSRule]:
    text = _strip_comments(text)
    rules: list[CSSRule] = []
    _parse_block(text, 0, len(text), source_file, None, 1, rules)
    return rules


# Shorthand → longhand expansion table.  When a rule sets the shorthand, the
# audit synthesises a longhand decl with the same value/!important so that
# bare-class rules setting the longhand directly are detectable.  This is
# intentionally lossy — we don't try to parse `font: 1.65 11.5px` into its
# four longhand fields; we just replicate the whole value into every longhand
# slot.  That's enough to detect the shadowing pattern (`font: inherit`
# vs `font-family: var(--mono)`) where the audit needs to flag a conflict;
# false positives on partial-value parses are filtered by `values_conflict`.
_SHORTHAND_LONGHANDS: dict[str, tuple[str, ...]] = {
    "font": ("font-family", "font-size", "font-weight", "font-style", "line-height"),
    "padding": ("padding-top", "padding-right", "padding-bottom", "padding-left"),
    "margin": ("margin-top", "margin-right", "margin-bottom", "margin-left"),
    "border": ("border-color", "border-width", "border-style"),
    "background": ("background-color", "background-image"),
}


def _expanded_declarations(
    decls: dict[str, tuple[str, bool]],
) -> dict[str, tuple[str, bool]]:
    """Return a new dict with shorthand keys expanded into their longhands.

    Source order matters: `font: inherit; font-size: 13px;` resolves to
    `font-size: 13px`, while `font-size: 13px; font: inherit;` resolves to
    `font-size: inherit` (the shorthand resets all font longhands).  We walk
    `decls` in iteration order (which `_parse_declarations` preserves from
    source) and overwrite, mirroring CSS within-rule cascade.
    """
    out: dict[str, tuple[str, bool]] = {}
    for prop, val in decls.items():
        out[prop] = val
        for lh in _SHORTHAND_LONGHANDS.get(prop, ()):
            out[lh] = val
    return out


def _line_at(text: str, idx: int) -> int:
    return text.count("\n", 0, idx) + 1


def _find_matching_brace(text: str, start: int) -> int:
    depth = 0
    for j in range(start, len(text)):
        c = text[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return j
    return -1


def _parse_block(
    text: str,
    start: int,
    end: int,
    source_file: str,
    media: str | None,
    line_offset: int,
    out: list[CSSRule],
) -> None:
    i = start
    while i < end:
        # skip whitespace
        while i < end and text[i].isspace():
            i += 1
        if i >= end:
            break

        if text[i] == "}":
            i += 1
            continue

        if text[i] == "@":
            # Find either ; (declaration at-rule) or { (block at-rule).
            j = i
            depth = 0
            semi = -1
            brace = -1
            while j < end:
                c = text[j]
                if c == "(":
                    depth += 1
                elif c == ")":
                    depth -= 1
                elif c == ";" and depth == 0:
                    semi = j
                    break
                elif c == "{" and depth == 0:
                    brace = j
                    break
                j += 1
            if semi != -1 and (brace == -1 or semi < brace):
                # @import / @charset / @namespace — skip
                i = semi + 1
                continue
            if brace == -1:
                break

            head = text[i:brace].strip()
            close = _find_matching_brace(text, brace)
            if close == -1:
                break

            name_match = re.match(r"@(\w+)", head)
            at_name = name_match.group(1) if name_match else ""

            if at_name in {"media", "supports", "container", "layer"}:
                # nest media context if already inside one
                effective = head if media is None or at_name != "media" else f"{media} && {head}"
                _parse_block(text, brace + 1, close, source_file, effective, line_offset, out)
                i = close + 1
                continue

            # @keyframes / @font-face / @page / unknown — skip body
            i = close + 1
            continue

        # regular rule: read selector until '{'
        depth = 0
        j = i
        while j < end:
            c = text[j]
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
            elif c == "{" and depth == 0:
                break
            j += 1
        if j >= end:
            break
        sel_text = text[i:j].strip()
        close = _find_matching_brace(text, j)
        if close == -1:
            break
        body = text[j + 1 : close]
        if sel_text:
            decls = _parse_declarations(body)
            if decls:
                line = _line_at(text, i) + line_offset - 1
                selectors = _split_top_level_commas(sel_text)
                out.append(CSSRule(selectors, decls, source_file, line, media))
        i = close + 1


# -- selector parsing & specificity --------------------------------------------


# A "compound" is a sequence of simple selectors with no combinator between
# them: `a.b#c[x][y]:hover`.  A "selector" is a list of compounds joined by
# combinators (`>`, `+`, `~`, ` `).  The "subject compound" is the rightmost
# compound — the element actually styled by the rule.

COMBINATOR_RE = re.compile(r"\s*([>+~])\s*|\s+")


@dataclass
class Compound:
    raw: str
    ids: list[str] = field(default_factory=list)
    classes: list[str] = field(default_factory=list)
    attrs: list[str] = field(default_factory=list)
    pseudo_classes: list[str] = field(default_factory=list)
    pseudo_elements: list[str] = field(default_factory=list)
    type: str | None = None  # element name or None for universal/missing
    is_universal: bool = False
    specificity: tuple[int, int, int] = (0, 0, 0)


@dataclass
class Selector:
    raw: str
    compounds: list[Compound]
    combinators: list[str]  # length len(compounds)-1; descendant ' ', '>', '+', '~'
    specificity: tuple[int, int, int]

    @property
    def subject(self) -> Compound:
        return self.compounds[-1]


def _split_compounds(sel: str) -> tuple[list[str], list[str]]:
    """Split a selector into compound tokens and combinators.

    Combinators: ' ' (descendant), '>' (child), '+' (adj), '~' (sibling).
    """
    s = sel.strip()
    parts: list[str] = []
    combs: list[str] = []
    i = 0
    cur: list[str] = []
    paren = 0
    while i < len(s):
        c = s[i]
        if c == "(":
            paren += 1
            cur.append(c)
            i += 1
        elif c == ")":
            paren -= 1
            cur.append(c)
            i += 1
        elif paren > 0:
            cur.append(c)
            i += 1
        elif c in ">+~":
            if cur and cur[-1] == " ":
                cur.pop()
            parts.append("".join(cur).strip())
            combs.append(c)
            cur = []
            i += 1
            while i < len(s) and s[i].isspace():
                i += 1
        elif c.isspace():
            if cur and not cur[-1].isspace():
                cur.append(" ")
            i += 1
        else:
            cur.append(c)
            i += 1
    last = "".join(cur).strip()
    if last:
        parts.append(last)

    # collapse split compounds: if a part contains internal spaces (descendant),
    # split it now into multiple compounds with descendant combinators.
    final_parts: list[str] = []
    final_combs: list[str] = []
    for k, p in enumerate(parts):
        bits = [b for b in re.split(r"\s+(?![^()]*\))", p) if b]
        for m, b in enumerate(bits):
            final_parts.append(b)
            if m < len(bits) - 1:
                final_combs.append(" ")
        if k < len(combs):
            final_combs.append(combs[k])
    return final_parts, final_combs


def _parse_compound(raw: str) -> Compound:
    c = Compound(raw=raw)
    s = raw
    if not s:
        return c
    if s == "*":
        c.is_universal = True
        return c
    # Split by simple-selector boundary: # . [ : (but :: as group)
    # Use regex to scan tokens.
    i = 0
    # leading element / universal
    m = re.match(r"\*|[a-zA-Z][\w-]*", s)
    if m:
        token = m.group()
        if token == "*":
            c.is_universal = True
        else:
            c.type = token.lower()
        i = m.end()
    while i < len(s):
        ch = s[i]
        if ch == "#":
            m = re.match(r"#([\w-]+)", s[i:])
            if not m:
                i += 1
                continue
            c.ids.append(m.group(1))
            i += m.end()
        elif ch == ".":
            m = re.match(r"\.([\w-]+)", s[i:])
            if not m:
                i += 1
                continue
            c.classes.append(m.group(1))
            i += m.end()
        elif ch == "[":
            depth = 1
            j = i + 1
            while j < len(s) and depth > 0:
                if s[j] == "[":
                    depth += 1
                elif s[j] == "]":
                    depth -= 1
                j += 1
            c.attrs.append(s[i:j])
            i = j
        elif ch == ":":
            # pseudo-element ::
            if s[i : i + 2] == "::":
                m = re.match(r"::([\w-]+)", s[i:])
                if m:
                    c.pseudo_elements.append(m.group(1).lower())
                    i += m.end()
                else:
                    i += 2
            else:
                # pseudo-class :name or :name(...)
                m = re.match(r":([\w-]+)(\(([^()]|\([^()]*\))*\))?", s[i:])
                if not m:
                    i += 1
                    continue
                name = m.group(1).lower()
                arg = m.group(2) or ""
                # Treat single-colon legacy pseudo-elements as pseudo-elements.
                if name in {"before", "after", "first-line", "first-letter"}:
                    c.pseudo_elements.append(name)
                else:
                    c.pseudo_classes.append(name + arg)
                i += m.end()
        else:
            # whitespace or unknown — break
            i += 1

    # specificity: (#ids, #classes+#attrs+#pseudo-classes (with :not/:is/:has special), #elements + #pseudo-elements)
    a = len(c.ids)
    b = len(c.classes) + len(c.attrs)
    d = len(c.pseudo_elements) + (0 if c.type is None else 1)
    # pseudo-classes: each adds 1 to b; :not/:is/:has add the highest of the
    # selectors inside; :where adds 0.
    for pc in c.pseudo_classes:
        m = re.match(r"([\w-]+)(?:\((.*)\))?$", pc)
        if not m:
            b += 1
            continue
        pname = m.group(1).lower()
        parg = m.group(2)
        if pname == "where":
            continue
        if pname in {"not", "is", "has"} and parg:
            best = (0, 0, 0)
            for inner in _split_top_level_commas(parg):
                sel = parse_selector(inner)
                if sel.specificity > best:
                    best = sel.specificity
            a += best[0]
            b += best[1]
            d += best[2]
            continue
        b += 1
    c.specificity = (a, b, d)
    return c


def parse_selector(raw: str) -> Selector:
    parts, combs = _split_compounds(raw)
    compounds = [_parse_compound(p) for p in parts]
    spec = (0, 0, 0)
    for cp in compounds:
        spec = (
            spec[0] + cp.specificity[0],
            spec[1] + cp.specificity[1],
            spec[2] + cp.specificity[2],
        )
    return Selector(raw=raw.strip(), compounds=compounds, combinators=combs, specificity=spec)


# -- HTML parser ---------------------------------------------------------------


@dataclass
class HTMLElement:
    tag: str
    id: str | None
    classes: list[str]
    parent_chain: list[HTMLElement]  # ancestors, root first
    source_file: str
    line: int
    attrs: dict[str, str] = field(default_factory=dict)
    stylesheets: tuple[str, ...] = ()  # CSS files that apply to this element

    def has_class(self, c: str) -> bool:
        return c in self.classes


class _HTMLCollector(HTMLParser):
    def __init__(self, source_file: str) -> None:
        super().__init__(convert_charrefs=True)
        self.source_file = source_file
        self.stack: list[HTMLElement] = []
        self.elements: list[HTMLElement] = []
        # Tags whose end-tag is omitted in HTML5 (or void elements).
        self._void = {
            "area",
            "base",
            "br",
            "col",
            "embed",
            "hr",
            "img",
            "input",
            "link",
            "meta",
            "param",
            "source",
            "track",
            "wbr",
        }

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_dict: dict[str, str] = {k: (v or "") for k, v in attrs}
        elem_id = attr_dict.get("id")
        cls_attr = attr_dict.get("class") or ""
        classes = cls_attr.split() if cls_attr else []
        line, _col = self.getpos()
        elem = HTMLElement(
            tag=tag.lower(),
            id=elem_id,
            classes=classes,
            parent_chain=list(self.stack),
            source_file=self.source_file,
            line=line,
            attrs=attr_dict,
        )
        self.elements.append(elem)
        if tag.lower() not in self._void:
            self.stack.append(elem)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # Self-closing: handle as start without push.
        attr_dict: dict[str, str] = {k: (v or "") for k, v in attrs}
        elem_id = attr_dict.get("id")
        cls_attr = attr_dict.get("class") or ""
        classes = cls_attr.split() if cls_attr else []
        line, _col = self.getpos()
        self.elements.append(
            HTMLElement(
                tag=tag.lower(),
                id=elem_id,
                classes=classes,
                parent_chain=list(self.stack),
                source_file=self.source_file,
                line=line,
                attrs=attr_dict,
            )
        )

    def handle_endtag(self, tag: str) -> None:
        for k in range(len(self.stack) - 1, -1, -1):
            if self.stack[k].tag == tag.lower():
                del self.stack[k:]
                return


def parse_html(path: Path, stylesheets: tuple[str, ...]) -> list[HTMLElement]:
    coll = _HTMLCollector(str(path))
    coll.feed(path.read_text(encoding="utf-8"))
    for el in coll.elements:
        el.stylesheets = stylesheets
    return coll.elements


# -- JS-built element scanner --------------------------------------------------


# Match a literal HTML start-tag with an `id` attribute.  Used to find runtime-
# built elements with both id+class.  A hand rolled scan is good enough — we
# don't need to be perfect, only catch the obvious cases.
_HTML_LITERAL_RE = re.compile(r"""<(?P<tag>[a-zA-Z][\w-]*)\b(?P<attrs>[^>]*?)/?>""", re.DOTALL)
_ID_ATTR_RE = re.compile(r"""\bid\s*=\s*(?P<q>['"])(?P<v>[^'"]+)(?P=q)""")
_CLASS_ATTR_RE = re.compile(r"""\bclass\s*=\s*(?P<q>['"])(?P<v>[^'"]+)(?P=q)""")


def parse_js_for_elements(path: Path, stylesheets: tuple[str, ...]) -> list[HTMLElement]:
    text = path.read_text(encoding="utf-8")
    out: list[HTMLElement] = []
    for m in _HTML_LITERAL_RE.finditer(text):
        attrs = m.group("attrs")
        id_m = _ID_ATTR_RE.search(attrs)
        cls_m = _CLASS_ATTR_RE.search(attrs)
        if not (id_m and cls_m):
            continue
        line = text.count("\n", 0, m.start()) + 1
        out.append(
            HTMLElement(
                tag=m.group("tag").lower(),
                id=id_m.group("v"),
                classes=cls_m.group("v").split(),
                parent_chain=[],
                source_file=str(path),
                line=line,
                stylesheets=stylesheets,
            )
        )
    return out


# -- matching: does a selector apply (subject-only) to an element? ------------


_ATTR_RE = re.compile(
    r"\[\s*(?P<name>[\w-]+)\s*(?:(?P<op>[~|^$*]?=)\s*(?P<q>['\"]?)(?P<val>[^'\"\]]*)(?P=q))?\s*\]"
)


def _attr_matches(attr_sel: str, attrs: dict[str, str]) -> bool:
    """Match a CSS attribute selector like `[role="alert"]` against an
    element's attributes."""
    m = _ATTR_RE.match(attr_sel)
    if not m:
        return True  # unparseable → pretend it matches (conservative)
    name = m.group("name").lower()
    op = m.group("op")
    val = m.group("val") or ""
    actual = attrs.get(name)
    if actual is None:
        return False
    if op is None:
        return True  # `[name]` — just presence
    if op == "=":
        return actual == val
    if op == "~=":
        return val in actual.split()
    if op == "|=":
        return actual == val or actual.startswith(val + "-")
    if op == "^=":
        return actual.startswith(val)
    if op == "$=":
        return actual.endswith(val)
    if op == "*=":
        return val in actual
    return True


def selector_subject_matches(sel: Selector, elem: HTMLElement) -> bool:
    """Match the SUBJECT compound of `sel` against `elem` (id/classes/tag).

    We do NOT walk up the descendant/sibling chain — we only check whether the
    rightmost compound could match this element.  That's deliberate: we want to
    flag any rule that could apply, not only ones that definitely apply.

    Returns True if every simple selector in the subject compound matches.
    """
    sub = sel.subject
    if sub.type and elem.tag != sub.type:
        return False
    for c in sub.classes:
        if c not in elem.classes:
            return False
    for i in sub.ids:
        if elem.id != i:
            return False
    if not all(_attr_matches(a, elem.attrs) for a in sub.attrs):
        return False
    # If pseudo-element, we conservatively say it does not match a regular
    # element (the rule styles a generated box, not the element itself).
    return not sub.pseudo_elements


def selector_chain_could_match(sel: Selector, elem: HTMLElement) -> bool:
    """Return True if the full selector (including ancestor compounds) could
    match `elem`, given its parent_chain.  Greedy descendant matching."""
    if not selector_subject_matches(sel, elem):
        return False
    if len(sel.compounds) == 1:
        return True
    # Walk left-ward through compounds + combinators.
    chain = list(elem.parent_chain)  # root first ... immediate parent last
    # process compounds[:-1] right to left, checking against ancestors right to left
    idx_anc = len(chain) - 1
    for k in range(len(sel.compounds) - 2, -1, -1):
        comb = sel.combinators[k]
        cp = sel.compounds[k]
        if comb == ">":
            if idx_anc < 0:
                return False
            if not _compound_matches_elem(cp, chain[idx_anc]):
                return False
            idx_anc -= 1
        elif comb == " ":
            # find any ancestor matching cp
            found = -1
            while idx_anc >= 0:
                if _compound_matches_elem(cp, chain[idx_anc]):
                    found = idx_anc
                    break
                idx_anc -= 1
            if found < 0:
                return False
            idx_anc = found - 1
        else:
            # +/~ siblings — we don't track sibling order in the simple parser.
            # Be conservative: assume could match.
            return True
    return True


def _compound_matches_elem(cp: Compound, elem: HTMLElement) -> bool:
    if cp.type and elem.tag != cp.type:
        return False
    for c in cp.classes:
        if c not in elem.classes:
            return False
    for i in cp.ids:
        if elem.id != i:
            return False
    return all(_attr_matches(a, elem.attrs) for a in cp.attrs)


# -- finding -------------------------------------------------------------------


_STATE_PSEUDOS = {
    "hover",
    "focus",
    "focus-visible",
    "focus-within",
    "active",
    "disabled",
    "checked",
    "visited",
    "target",
    "placeholder-shown",
    "valid",
    "invalid",
    "required",
    "optional",
    "read-only",
    "read-write",
    "indeterminate",
    "default",
    "empty",
    "blank",
}


def _state_set(sel: Selector) -> frozenset[str]:
    """Return the set of state pseudo-classes referenced anywhere in `sel`.

    Used to gate "real" conflict detection: two rules only really conflict on
    a property when they apply in the SAME state.  `.foo:hover` setting `bg`
    does not conflict with `.foo` setting `bg` in the resting state, even
    though both rules touch `bg`.  But in hover state both apply, so the more
    specific wins — that IS a real conflict if both rules want to set `bg`
    in hover state.

    To reflect this: define a rule's "state" as its set of state pseudo-class
    names.  Two rules can only conflict if one's state is a SUBSET of the
    other's state — meaning both apply in the more-state-y rule's state.  The
    less-state-y rule applies in every state including the more-state-y one.
    """
    s: set[str] = set()
    for cp in sel.compounds:
        for pc in cp.pseudo_classes:
            name = pc.split("(", 1)[0]
            if name in _STATE_PSEUDOS:
                s.add(name)
    return frozenset(s)


def _winner_overrides_bare_state(winner: frozenset[str], bare: frozenset[str]) -> bool:
    """Return True iff `winner` rule should be considered an override of
    `bare` in `bare`'s intended state.

    Specifically: winner's state-set must be a (non-strict) subset of bare's.
    That way, in any state where bare applies, winner also applies — so
    if winner is more specific, it overrides bare.

    Counter-example: `.foo:hover` (winner) vs `.foo` (bare).  winner state
    {hover} is NOT ⊆ ∅, so we don't flag.  That's correct — hover-state
    styling overriding a resting-state base rule is a normal pattern, not
    a cascade flip.
    """
    return winner <= bare


@dataclass
class Finding:
    elem: HTMLElement
    prop: str
    high_rule: CSSRule
    high_selector: Selector
    high_value: str
    high_important: bool
    low_rule: CSSRule
    low_selector: Selector
    low_value: str
    low_important: bool

    def category(self) -> str:
        # id: any compound has an ID
        # class-chain: multiple class-tier compounds (e.g., `.parent .C`, `.X.C`)
        # tag-chain: tag in subject or ancestor compound (`h1.C`, `div .C`)
        # pseudo: state pseudo-class only differentiates
        sel = self.high_selector
        if any(cp.ids for cp in sel.compounds):
            return "id"
        # count class-tier compounds vs subject's bare-class
        if len(sel.compounds) > 1:
            return "ancestor-chain"
        sub = sel.subject
        if sub.type:
            return "tag-chain"
        if len(sub.classes) >= 2:
            return "multi-class"
        if sub.attrs:
            return "attr"
        if any(p not in _STATE_PSEUDOS for p in (s.split("(", 1)[0] for s in sub.pseudo_classes)):
            return "pseudo-class"
        return "state-pseudo"

    def severity(self) -> str:
        if self.category() == "id":
            return "high"
        return "medium"


def values_conflict(a: str, b: str) -> bool:
    """Are two CSS property values different enough to call a real conflict?

    Normalise whitespace and lowercase, then compare.  This catches `red` vs
    `blue` but treats `red` and ` red ` as equal.
    """
    return _normalise_value(a) != _normalise_value(b)


def _normalise_value(v: str) -> str:
    return re.sub(r"\s+", " ", v.strip().lower()).rstrip(";")


def is_unscoped_legacy(sel: Selector) -> bool:
    """A "legacy ID/tag-chain rule" — selector has at least one ID anywhere
    (so it carries an ID-tier specificity).
    """
    return any(cp.ids for cp in sel.compounds)


def collect_rules(rules: Iterable[CSSRule]) -> list[tuple[CSSRule, Selector]]:
    """Flatten rule -> per-selector pairs.  Selector-parse failures emit a
    warning to stderr so the developer notices coverage gaps; silent skip
    would corrupt the "audit clean" verdict."""
    out: list[tuple[CSSRule, Selector]] = []
    for r in rules:
        for s in r.selectors:
            try:
                sel = parse_selector(s)
            except (IndexError, AttributeError, ValueError) as exc:
                print(
                    f"warn: failed to parse selector at {r.source_file}:{r.line_no}: "
                    f"{s!r} ({exc.__class__.__name__}: {exc})",
                    file=sys.stderr,
                )
                continue
            out.append((r, sel))
    return out


def _is_bare_class_subject(sel: Selector) -> bool:
    """A "bare class" rule has a single compound containing exactly one class
    and no IDs/types/attrs/non-state-pseudo-classes — the design primitive.
    """
    if len(sel.compounds) != 1:
        return False
    sub = sel.subject
    if sub.ids or sub.attrs or sub.type or sub.is_universal:
        return False
    if len(sub.classes) != 1:
        return False
    if sub.pseudo_elements:
        return False
    for pc in sub.pseudo_classes:
        name = pc.split("(", 1)[0]
        if name not in _STATE_PSEUDOS:
            return False
    return True


class Hit(NamedTuple):
    """One (rule, selector, value, important) tuple matched against an element.

    Used inside `audit()` to keep the cascade-resolution predicates readable —
    `h.specificity > b.specificity and h.important == b.important` reads better
    than `h[1].specificity > b[1].specificity and h[3] == b[3]`.
    """

    rule: CSSRule
    selector: Selector
    value: str
    important: bool

    @property
    def specificity(self) -> tuple[int, int, int]:
        return self.selector.specificity


def _cascade_beats(winner: Hit, bare: Hit) -> bool:
    """Does `winner` beat `bare` in CSS cascade resolution?

    Winner is more specific at the same `!important` level, OR winner has
    `!important` and bare doesn't.  (Two `!important` rules at the same spec
    fall through to source order, handled by the caller.)
    """
    if winner.important and not bare.important:
        return True
    return winner.important == bare.important and winner.specificity > bare.specificity


def audit(
    rules: list[CSSRule],
    elements: list[HTMLElement],
    *,
    only_id_tier: bool = True,
    watch_only: bool = True,
) -> list[Finding]:
    pairs = collect_rules(rules)
    findings: list[Finding] = []
    for elem in elements:
        if not elem.classes:
            continue
        # Find every (rule, selector) whose subject compound could match elem,
        # restricted to the stylesheets actually loaded by the page that owns
        # this element.
        applicable: list[tuple[CSSRule, Selector]] = []
        for r, s in pairs:
            if elem.stylesheets and r.source_file not in elem.stylesheets:
                continue
            if selector_chain_could_match(s, elem):
                applicable.append((r, s))
        if len(applicable) < 2:
            continue
        # Cascade source order is per-page, not per-file: stylesheets are
        # `<link>`-loaded in the order PAGE_STYLESHEETS lists them, so a rule
        # at line 1000 of base.css comes BEFORE a rule at line 50 of style.css
        # when both load on the same page.  Build a (file_index, line_no)
        # ordering keyed off the element's stylesheet manifest.
        sheet_index = {s: i for i, s in enumerate(elem.stylesheets)}
        sheet_count = len(sheet_index)

        # Group by property — expand watched-shorthand declarations into
        # their longhands so e.g. `font: inherit` shadows `font-family`.
        by_prop: dict[str, list[Hit]] = {}
        for r, s in applicable:
            for p, (v, imp) in _expanded_declarations(r.declarations).items():
                if watch_only and p not in WATCH_PROPERTIES:
                    continue
                by_prop.setdefault(p, []).append(Hit(r, s, v, imp))
        for p, hits in by_prop.items():
            if len(hits) < 2:
                continue
            # bare-class hits: design primitives where the only thing between
            # the rule and the element is the class.  These are the things
            # that should "win" if the design works correctly.
            bare = [h for h in hits if _is_bare_class_subject(h.selector)]
            if not bare:
                continue
            for b in bare:
                bare_state = _state_set(b.selector)
                candidates = [
                    h
                    for h in hits
                    if _winner_overrides_bare_state(_state_set(h.selector), bare_state)
                    and _cascade_beats(h, b)
                ]
                if not candidates:
                    continue
                # Pick the actual cascade winner: !important first, then spec,
                # then source-later (across the per-page stylesheet load order).
                candidates.sort(
                    key=lambda h: (
                        h.important,
                        h.specificity,
                        sheet_index.get(h.rule.source_file, sheet_count),
                        h.rule.line_no,
                    ),
                    reverse=True,
                )
                winner = candidates[0]
                if not values_conflict(winner.value, b.value):
                    continue
                if winner.rule is b.rule and winner.selector is b.selector:
                    continue
                f = Finding(
                    elem=elem,
                    prop=p,
                    high_rule=winner.rule,
                    high_selector=winner.selector,
                    high_value=winner.value,
                    high_important=winner.important,
                    low_rule=b.rule,
                    low_selector=b.selector,
                    low_value=b.value,
                    low_important=b.important,
                )
                if only_id_tier and f.category() != "id":
                    continue
                findings.append(f)
    return findings


# -- driver --------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quiet", action="store_true", help="exit code only")
    parser.add_argument(
        "--all-tiers",
        action="store_true",
        help="also flag class-tier and tag-tier wins (default: ID tier only)",
    )
    parser.add_argument(
        "--all-properties",
        action="store_true",
        help="check every property (default: visual/layout properties only)",
    )
    args = parser.parse_args()

    rules: list[CSSRule] = []
    for rel in CSS_FILES:
        path = REPO / rel
        if not path.exists():
            print(f"warn: {rel} not found", file=sys.stderr)
            continue
        rules.extend(parse_css(path.read_text(encoding="utf-8"), rel))

    elements: list[HTMLElement] = []
    for html_rel, page_sheets in PAGE_STYLESHEETS.items():
        html_path = REPO / html_rel
        if html_path.exists():
            elements.extend(parse_html(html_path, tuple(page_sheets)))
    # JS-emitted elements: tag with the intersection of stylesheets across
    # every page that loads the JS file.  An element built by shared JS only
    # has a flagged conflict if EVERY page that hosts it has the conflict.
    for js_rel, owner_pages in JS_FILES.items():
        js_path = REPO / js_rel
        if not js_path.exists():
            continue
        sheet_sets = [set(PAGE_STYLESHEETS[p]) for p in owner_pages if p in PAGE_STYLESHEETS]
        if not sheet_sets:
            continue
        common: set[str] = sheet_sets[0]
        for s in sheet_sets[1:]:
            common = common & s
        if not common:
            continue
        js_sheets: tuple[str, ...] = tuple(s for s in CSS_FILES if s in common)
        elements.extend(parse_js_for_elements(js_path, js_sheets))

    findings = audit(
        rules,
        elements,
        only_id_tier=not args.all_tiers,
        watch_only=not args.all_properties,
    )

    # de-dup: same elem.id + prop + winner-selector + bare-selector tuple is one finding.
    seen: set[tuple[str, str | None, str, str, str]] = set()
    unique: list[Finding] = []
    for f in findings:
        key = (
            f.elem.source_file or "",
            f.elem.id,
            f.prop,
            f.high_selector.raw,
            f.low_selector.raw,
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(f)

    unique.sort(
        key=lambda f: (
            f.elem.source_file or "",
            f.elem.id or "",
            f.high_selector.raw,
            f.prop,
        )
    )

    if args.quiet:
        return 0 if not unique else 1

    if not unique:
        print(f"audit clean: {len(rules)} rules, {len(elements)} elements scanned, 0 findings")
        return 0

    print("# CSS specificity audit\n")
    print(
        f"Scanned {len(rules)} rules across {len(CSS_FILES)} stylesheets, "
        f"{len(elements)} elements across {len(PAGE_STYLESHEETS)} HTML files + JS literals.\n"
    )
    by_cat: dict[str, list[Finding]] = {}
    for f in unique:
        by_cat.setdefault(f.category(), []).append(f)

    print(f"## Findings ({len(unique)})\n")
    for cat in (
        "id",
        "ancestor-chain",
        "tag-chain",
        "multi-class",
        "attr",
        "pseudo-class",
        "state-pseudo",
    ):
        if cat not in by_cat:
            continue
        print(f"### {cat} ({len(by_cat[cat])})\n")
        for f in by_cat[cat]:
            src = f.elem.source_file or "(synthetic)"
            try:
                rel_path = Path(src).resolve().relative_to(REPO)
                elem_loc = f"{rel_path}:{f.elem.line}"
            except (ValueError, OSError):
                elem_loc = f"{src}:{f.elem.line}"
            elem_desc = f"<{f.elem.tag}"
            if f.elem.id:
                elem_desc += f' id="{f.elem.id}"'
            if f.elem.classes:
                elem_desc += f' class="{" ".join(f.elem.classes)}"'
            elem_desc += ">"

            print(f"#### `{elem_desc}` at `{elem_loc}` — `{f.prop}`")
            print()
            print(
                f"- **winner** `{f.high_selector.raw}` "
                f"(spec {f.high_selector.specificity}{', !important' if f.high_important else ''}) at "
                f"`{f.high_rule.source_file}:{f.high_rule.line_no}` → `{f.high_value}`"
            )
            print(
                f"- **bare class** `{f.low_selector.raw}` "
                f"(spec {f.low_selector.specificity}{', !important' if f.low_important else ''}) at "
                f"`{f.low_rule.source_file}:{f.low_rule.line_no}` → `{f.low_value}`"
            )
            print()

    return 1


if __name__ == "__main__":
    sys.exit(main())
