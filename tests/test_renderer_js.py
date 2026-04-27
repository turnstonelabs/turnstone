"""Smoke tests for ``turnstone/shared_static/renderer.js``.

The renderer is browser-only JS with no test framework on the project
side. These tests drive it through ``node`` against a minimal browser-
shim harness so a regression on the markdown / KaTeX wiring surfaces
in CI rather than at runtime in the operator's browser.

Each test invokes ``node -e`` with a small wrapper that loads
``utils.js`` + ``renderer.js`` via ``vm.runInThisContext``, stubs
``document`` / ``katex`` enough for the renderer to run, then prints
the rendered HTML for a sample input. The assertions check the
resulting markup contains the expected ``<span class="katex">…</span>``
placeholder and not the raw delimiter.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_UTILS_JS = _REPO_ROOT / "turnstone/shared_static/utils.js"
_RENDERER_JS = _REPO_ROOT / "turnstone/shared_static/renderer.js"


def _has_node() -> bool:
    return shutil.which("node") is not None


pytestmark = pytest.mark.skipif(not _has_node(), reason="node not available")


_HARNESS_TEMPLATE = """
const vm = require('vm');
const fs = require('fs');
global.document = {
  createElement: () => {
    let t = '';
    return {
      get textContent() { return t; },
      set textContent(v) { t = v; },
      get innerHTML() {
        return t.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
      },
    };
  },
  addEventListener: () => {},
};
global.katex = {
  renderToString: (tex, opts) =>
    '<span class="katex">[KATEX:' +
    tex.replace(/\\n/g, '\\\\n') +
    (opts.displayMode ? ':display' : ':inline') +
    ']</span>',
};
global.window = global;
vm.runInThisContext(fs.readFileSync(%(utils)s, 'utf8'));
vm.runInThisContext(fs.readFileSync(%(renderer)s, 'utf8'));
const input = %(input)s;
process.stdout.write(renderMarkdown(input));
"""


def _render(markdown: str) -> str:
    """Render ``markdown`` through renderer.js + return the HTML."""
    harness = _HARNESS_TEMPLATE % {
        "utils": json.dumps(str(_UTILS_JS)),
        "renderer": json.dumps(str(_RENDERER_JS)),
        "input": json.dumps(markdown),
    }
    result = subprocess.run(
        ["node", "-e", harness],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    return result.stdout


# ---------------------------------------------------------------------------
# KaTeX delimiter handling — both TeX and LaTeX styles
# ---------------------------------------------------------------------------


def test_tex_inline_math_renders() -> None:
    out = _render("The formula $E = mc^2$ is famous.")
    assert '<span class="katex">' in out
    assert "[KATEX:E = mc^2:inline]" in out
    assert "$E = mc^2$" not in out  # raw delimiters consumed


def test_tex_display_math_renders() -> None:
    out = _render("$$\nE = mc^2\n$$")
    assert '<span class="katex">' in out
    assert ":display]" in out


def test_latex_inline_math_renders() -> None:
    r"""LaTeX-style \(...\) inline math. GPT-5 / o-series / Claude
    with reasoning effort emit this style by default; without
    explicit support the model output passed through as raw \(x\)
    text in coord + interactive UIs."""
    out = _render(r"The formula \(E = mc^2\) is famous.")
    assert '<span class="katex">' in out
    assert "[KATEX:E = mc^2:inline]" in out
    assert r"\(E = mc^2\)" not in out


def test_latex_display_math_renders() -> None:
    r"""LaTeX-style \[...\] display math."""
    out = _render("Intro\n\n\\[\nE = mc^2\n\\]\n\nMore")
    assert '<span class="katex">' in out
    assert ":display]" in out
    assert "\\[" not in out
    assert "\\]" not in out


def test_latex_math_in_list_item_renders() -> None:
    """Nested-in-markdown-block — the original bug report. The list
    item is processed via line-by-line + inlineMarkdown; the math
    placeholder must survive that path."""
    out = _render(r"- Item with \(E = mc^2\) math")
    assert "<li>" in out
    assert '<span class="katex">' in out
    assert "[KATEX:E = mc^2:inline]" in out


def test_latex_math_in_blockquote_renders() -> None:
    out = _render(r"> Note: \(x^2\) is squared.")
    assert "<blockquote>" in out
    assert '<span class="katex">' in out


def test_latex_math_in_bold_renders() -> None:
    out = _render(r"Then **\(x^2\)** end.")
    assert "<strong>" in out
    assert '<span class="katex">' in out


def test_mixed_tex_and_latex_styles() -> None:
    out = _render(r"Here $x$ then \(y\) end.")
    assert out.count('<span class="katex">') == 2
    assert "[KATEX:x:inline]" in out
    assert "[KATEX:y:inline]" in out


def test_latex_math_inside_inline_code_preserved() -> None:
    r"""\(...\) inside inline code must NOT render as math —
    code is escaped + left literal."""
    out = _render(r"Code: `\(x\)` raw.")
    assert r"<code>\(x\)</code>" in out
    assert '<span class="katex">' not in out


def test_latex_math_inside_fenced_code_preserved() -> None:
    r"""\(...\) inside a fenced block must stay literal."""
    out = _render("```\nA \\(x\\) sample\n```")
    assert "<pre><code>" in out
    assert '<span class="katex">' not in out


def test_solo_escaped_bracket_does_not_render_as_math() -> None:
    r"""A lone \[ with no matching \] is not math — it's a markdown
    bracket escape. Don't hijack it."""
    out = _render(r"No math: \[ alone.")
    assert '<span class="katex">' not in out


def test_markdown_link_unaffected_by_math_protection() -> None:
    r"""Math regex uses \[ / \] (escaped brackets), not bare [...].
    Markdown links must still render."""
    out = _render("See [docs](https://example.com).")
    assert '<a href="https://example.com"' in out
    assert ">docs</a>" in out
