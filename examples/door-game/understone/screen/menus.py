"""Classic door-game menu rendering for location interiors.

A menu is a boxed title, a block of flavour/body lines, an option line of
the ``(B)uy  (S)ell  (L)eave`` form, and a footer status line.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from understone.screen.box import BL, BR, H, V, border_with_title

if TYPE_CHECKING:
    from collections.abc import Sequence


def render_menu(
    title: str,
    lines: Sequence[str],
    options: Sequence[str],
    status: str,
) -> str:
    """Render a boxed location menu.

    *title* is centred in the top border, *lines* form the body (each
    left-padded inside the box), *options* are joined with two spaces into
    an option line, and *status* prints under the box.
    """
    body = list(lines)
    option_line = "  ".join(options)
    if option_line:
        body.append("")
        body.append(option_line)
    inner = _inner_width(title, body)
    out = [border_with_title(inner, title)]
    for line in body:
        out.append(V + " " + line.ljust(inner - 1) + V)
    out.append(BL + (H * inner) + BR)
    out.append(status)
    return "\n".join(out)


def _inner_width(title: str, body: Sequence[str]) -> int:
    """Choose an inner width that fits the title and the widest body line."""
    title_need = len(title.strip()) + 4
    body_need = max((len(line) + 2 for line in body), default=0)
    return max(title_need, body_need, 24)
