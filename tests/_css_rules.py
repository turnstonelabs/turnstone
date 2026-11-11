"""Read style rules out of a stylesheet, for tests that pin where a rule lives.

Text-level on purpose: the tests that use it ask "which sheet declares this",
which needs no cascade.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


def css_rules(css: str) -> list[tuple[list[str], str]]:
    """Every style rule as (selectors, declarations), @media bodies included."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    return [
        ([part.strip() for part in m[1].split(",")], m[2])
        for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", css)
    ]


def css_selectors(path: Path) -> list[str]:
    """Every selector in the sheet, one entry per comma-separated part."""
    return [s for selectors, _ in css_rules(path.read_text(encoding="utf-8")) for s in selectors]


def css_declarations(path: Path, selector: str) -> str:
    """The declarations of every rule that lists exactly this selector, joined."""
    return ";".join(
        declarations
        for selectors, declarations in css_rules(path.read_text(encoding="utf-8"))
        if selector in selectors
    )


def class_names(selector: str) -> list[str]:
    """Class names a selector mentions."""
    return re.findall(r"\.(-?[A-Za-z_][\w-]*)", selector)
