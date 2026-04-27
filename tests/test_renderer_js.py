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
from typing import Any

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


# ---------------------------------------------------------------------------
# Edge cases — Copilot review on PR #425
# ---------------------------------------------------------------------------


def test_display_math_inside_inline_code_stays_literal() -> None:
    r"""``$$...$$`` inside backticks must NOT trigger display-math
    extraction — otherwise the math sentinel ends up wrapped inside
    the <code> placeholder and leaks into rendered HTML as a raw
    null-byte sentinel string.

    Pre-#425 ordering ran display-math before inline code, which
    caused this leak. The reordering makes inline code seal first.
    """
    out = _render(r"Use `$$x$$` for display math.")
    assert "<code>$$x$$</code>" in out
    assert '<span class="katex">' not in out
    assert "\x00" not in out  # no leaked sentinel


def test_latex_display_math_inside_inline_code_stays_literal() -> None:
    r"""Same as above, but for the LaTeX-style \[...\] delimiter."""
    out = _render(r"Use `\[x\]` for display math.")
    assert r"<code>\[x\]</code>" in out
    assert '<span class="katex">' not in out
    assert "\x00" not in out


def test_inline_latex_math_does_not_span_paragraphs() -> None:
    r"""An unterminated \(...\) on one line must not eat the
    following paragraph until it finds a closing \) — that would
    consume large chunks of text under streaming markdown where
    the closer hasn't arrived yet. Mirrors the $...$ behavior."""
    src = "Open \\(unterminated\n\nNext paragraph with \\(x\\) here."
    out = _render(src)
    # The bare \( on line 1 should NOT match; the well-formed \(x\)
    # on the second paragraph should render normally.
    assert out.count('<span class="katex">') == 1
    assert "[KATEX:x:inline]" in out
    # The "unterminated" stays as raw text.
    assert "unterminated" in out


def test_inline_tex_math_does_not_span_newlines() -> None:
    """Existing $...$ behavior — regression guard."""
    src = "Open $unterminated\n\nNext paragraph $x$ here."
    out = _render(src)
    assert out.count('<span class="katex">') == 1
    assert "[KATEX:x:inline]" in out


# ---------------------------------------------------------------------------
# Mermaid progressive rendering — source-keyed SVG cache
# ---------------------------------------------------------------------------


_MERMAID_HARNESS_TEMPLATE = """
const vm = require('vm');
const fs = require('fs');

// Minimal DOM fake — enough surface for postRenderMermaid + the
// mermaid render path. Each created element tracks its attributes,
// classList, children, and parent so replaceWith works.
function makeEl(tag) {
  const el = {
    tagName: tag.toUpperCase(),
    _attrs: {},
    _classes: new Set(),
    children: [],
    parent: null,
    _innerHTML: '',
    _textContent: '',
    setAttribute(k, v) { this._attrs[k] = v; },
    getAttribute(k) { return this._attrs[k] !== undefined ? this._attrs[k] : null; },
    get classList() {
      const self = this;
      return {
        add(...c) { c.forEach(x => self._classes.add(x)); },
        remove(...c) { c.forEach(x => self._classes.delete(x)); },
        contains(c) { return self._classes.has(c); },
      };
    },
    get className() { return Array.from(this._classes).join(' '); },
    set className(v) {
      this._classes = new Set(String(v).split(/\\s+/).filter(Boolean));
    },
    get textContent() {
      return this._textContent || this.children.map(c => c.textContent || '').join('');
    },
    set textContent(v) { this._textContent = v; this.children = []; },
    get innerHTML() { return this._innerHTML; },
    set innerHTML(v) { this._innerHTML = v; this.children = []; },
    get isConnected() {
      // In real DOM this checks attachment to the document; for the
      // test harness we approximate via the parent chain. After
      // replaceWith, the displaced element's parent is nulled so
      // its isConnected goes false — which is exactly the
      // detached-during-streaming case the production guard
      // protects against.
      return !!this.parent;
    },
    appendChild(c) {
      c.parent = this;
      this.children.push(c);
      return c;
    },
    closest(selector) {
      const t = selector.toUpperCase();
      let cur = this;
      while (cur) {
        if (cur.tagName === t) return cur;
        cur = cur.parent;
      }
      return null;
    },
    replaceWith(other) {
      if (!this.parent) return;
      const idx = this.parent.children.indexOf(this);
      if (idx === -1) return;
      this.parent.children[idx] = other;
      other.parent = this.parent;
      this.parent = null;
    },
    querySelectorAll(selector) {
      // Only supports the literal "pre code.language-mermaid"
      // selector that postRenderMermaid uses.
      const out = [];
      function walk(node) {
        for (const c of (node.children || [])) {
          if (
            c.tagName === 'CODE' &&
            c.parent && c.parent.tagName === 'PRE' &&
            c._classes.has('language-mermaid')
          ) {
            out.push(c);
          }
          walk(c);
        }
      }
      walk(this);
      return out;
    },
  };
  return el;
}

global.document = {
  createElement: makeEl,
  addEventListener: () => {},
  getElementById: () => null,
  head: { appendChild: () => {} },
  documentElement: {},
};
global.window = global;
global.getComputedStyle = () => ({ getPropertyValue: () => '' });

let renderCallCount = 0;
let renderShouldFail = false;
global.mermaid = {
  initialize: () => {},
  render: (id, source) => {
    renderCallCount++;
    if (renderShouldFail) {
      return Promise.reject(new Error('bad diagram: ' + source));
    }
    return Promise.resolve({
      svg: '<svg data-source="' + source + '">rendered</svg>',
      bindFunctions: null,
    });
  },
};

vm.runInThisContext(fs.readFileSync(%(utils)s, 'utf8'));
vm.runInThisContext(fs.readFileSync(%(renderer)s, 'utf8'));

// Mermaid is normally lazy-loaded via _loadMermaid which fetches a
// script tag. Force-mark it ready so postRenderMermaid invokes the
// render path synchronously without trying to inject a script.
_mermaidState = 'ready';

%(scenario)s
"""


def _run_mermaid_scenario(scenario_js: str) -> dict[str, Any]:
    """Run a JS snippet against the mermaid-aware harness, return JSON output."""
    harness = _MERMAID_HARNESS_TEMPLATE % {
        "utils": json.dumps(str(_UTILS_JS)),
        "renderer": json.dumps(str(_RENDERER_JS)),
        "scenario": scenario_js,
    }
    result = subprocess.run(
        ["node", "-e", harness],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    parsed: dict[str, Any] = json.loads(result.stdout)
    return parsed


def _build_mermaid_container_js(sources: list[str]) -> str:
    """JS expression that builds a container with ``<pre><code language-mermaid>`` blocks."""
    src_array = "[" + ", ".join(json.dumps(s) for s in sources) + "]"
    return f"""
function buildContainer(sources) {{
  const container = document.createElement('div');
  for (const src of sources) {{
    const pre = document.createElement('pre');
    const code = document.createElement('code');
    code.classList.add('language-mermaid');
    code.textContent = src;
    pre.appendChild(code);
    container.appendChild(pre);
  }}
  return container;
}}
const sources = {src_array};
const container = buildContainer(sources);
"""


# Drain microtasks + global mermaid render chain. Wraps the async
# work in a setTimeout(0) hop so all queued microtasks (including
# the per-source pending list draining via _mermaidRenderChain)
# flush before the assertion script reads cache state.
_MERMAID_DRAIN_JS = """
function drainAndReport(report) {
  // Two setTimeout hops give the global chain time to resolve
  // mermaid.render's promise + the .then handlers that populate
  // the cache and call _applyMermaidSvg.
  setTimeout(() => setTimeout(() => {
    process.stdout.write(JSON.stringify(report()));
  }, 0), 0);
}
"""


def test_mermaid_cache_hit_skips_render_call() -> None:
    """Identical source on a second postRenderMermaid call must serve
    from the cache — mermaid.render runs exactly once across both
    invocations. This is the core invariant that lets streamingRender
    fire postRenderMermaid on every rAF tick without thrashing."""
    scenario = (
        _build_mermaid_container_js(["graph TD\n  A --> B"])
        + _MERMAID_DRAIN_JS
        + """
postRenderMermaid(container);
setTimeout(() => setTimeout(() => {
  // Second invocation — fresh container, same source. Should NOT
  // call mermaid.render again because the cache holds the SVG.
  const container2 = buildContainer(sources);
  postRenderMermaid(container2);
  setTimeout(() => {
    process.stdout.write(JSON.stringify({
      renderCalls: renderCallCount,
      cacheSize: _mermaidSvgCache.size,
      firstClass: container.children[0].className,
      secondClass: container2.children[0].className,
    }));
  }, 0);
}, 0), 0);
"""
    )
    out = _run_mermaid_scenario(scenario)
    assert out["renderCalls"] == 1, "second postRenderMermaid call invoked render — cache miss"
    assert out["cacheSize"] == 1
    # Both containers end up with the rendered class — second from cache.
    assert "mermaid-rendered" in out["firstClass"]
    assert "mermaid-rendered" in out["secondClass"]


def test_mermaid_distinct_sources_render_independently() -> None:
    """Two distinct sources each trigger mermaid.render once and are
    cached separately. Verifies the cache key is the source string,
    not e.g. a positional index."""
    scenario = (
        _build_mermaid_container_js(["graph TD\n  A --> B", "sequenceDiagram\n  A->>B: hi"])
        + """
postRenderMermaid(container);
// Drain twice — across-source serialization means the second
// render starts only after the first lands.
setTimeout(() => setTimeout(() => setTimeout(() => {
  process.stdout.write(JSON.stringify({
    renderCalls: renderCallCount,
    cacheSize: _mermaidSvgCache.size,
  }));
}, 0), 0), 0);
"""
    )
    out = _run_mermaid_scenario(scenario)
    assert out["renderCalls"] == 2
    assert out["cacheSize"] == 2


def test_mermaid_error_cached_to_avoid_thrash() -> None:
    """A mermaid render failure caches the error message keyed by
    source, so subsequent postRenderMermaid calls on the same source
    don't re-invoke mermaid.render only to re-fail."""
    scenario = (
        _build_mermaid_container_js(["bogus diagram"])
        + """
renderShouldFail = true;
postRenderMermaid(container);
setTimeout(() => setTimeout(() => {
  // Re-run with same source — should hit error cache.
  const container2 = buildContainer(sources);
  postRenderMermaid(container2);
  setTimeout(() => {
    process.stdout.write(JSON.stringify({
      renderCalls: renderCallCount,
      errorCacheSize: _mermaidErrorCache.size,
      svgCacheSize: _mermaidSvgCache.size,
      secondClass: container2.children[0].className,
    }));
  }, 0);
}, 0), 0);
"""
    )
    out = _run_mermaid_scenario(scenario)
    assert out["renderCalls"] == 1, "errored source re-invoked mermaid.render — error cache miss"
    assert out["errorCacheSize"] == 1
    assert out["svgCacheSize"] == 0
    # Second container shows the error class without re-rendering.
    assert "mermaid-error" in out["secondClass"]


def test_mermaid_cache_evicts_oldest_at_cap() -> None:
    """FIFO eviction at _MERMAID_CACHE_MAX prevents unbounded growth
    on long sessions emitting many distinct diagrams."""
    scenario = """
const cap = _MERMAID_CACHE_MAX;
for (let i = 0; i < cap + 5; i++) {
  _cacheMermaidEntry(_mermaidSvgCache, 'src-' + i, {svg: 'svg-' + i, bindFunctions: null});
}
process.stdout.write(JSON.stringify({
  size: _mermaidSvgCache.size,
  hasOldest: _mermaidSvgCache.has('src-0'),
  hasNewest: _mermaidSvgCache.has('src-' + (cap + 4)),
}));
"""
    out = _run_mermaid_scenario(scenario)
    assert out["size"] == 64
    assert out["hasOldest"] is False
    assert out["hasNewest"] is True


def test_mermaid_overwrite_does_not_evict() -> None:
    """Overwriting an existing key is an in-place update, not a new
    insertion — should not evict the oldest entry. Pre-fix, an
    update at cap would unnecessarily drop an unrelated cached SVG."""
    scenario = """
const cap = _MERMAID_CACHE_MAX;
// Fill exactly to cap.
for (let i = 0; i < cap; i++) {
  _cacheMermaidEntry(_mermaidSvgCache, 'src-' + i, {svg: 'svg-' + i, bindFunctions: null});
}
// Overwrite an existing entry — must not evict src-0.
_cacheMermaidEntry(_mermaidSvgCache, 'src-5', {svg: 'svg-updated', bindFunctions: null});
process.stdout.write(JSON.stringify({
  size: _mermaidSvgCache.size,
  hasOldest: _mermaidSvgCache.has('src-0'),
  updated: _mermaidSvgCache.get('src-5').svg,
}));
"""
    out = _run_mermaid_scenario(scenario)
    assert out["size"] == 64
    assert out["hasOldest"] is True, "overwrite evicted oldest unnecessarily"
    assert out["updated"] == "svg-updated"


def test_mermaid_cache_cleared_on_init() -> None:
    """_initMermaid must clear both caches so a theme change via
    reRenderAllMermaid doesn't serve stale SVG keyed by source-only
    — the rendered output depends on themeVariables which change
    on init."""
    scenario = """
_cacheMermaidEntry(_mermaidSvgCache, 'src-1', {svg: 'old', bindFunctions: null});
_cacheMermaidEntry(_mermaidErrorCache, 'src-bad', 'old error');
_initMermaid();
process.stdout.write(JSON.stringify({
  svgSize: _mermaidSvgCache.size,
  errorSize: _mermaidErrorCache.size,
}));
"""
    out = _run_mermaid_scenario(scenario)
    assert out["svgSize"] == 0
    assert out["errorSize"] == 0


def test_mermaid_cache_hit_reapplies_bind_functions() -> None:
    """bindFunctions returned by mermaid.render attach link/click
    handlers to the rendered SVG. Cache hits must re-invoke this
    on the new container instance — pre-fix, only the first render
    got bindings; subsequent cache hits via innerHTML left the SVG
    inert."""
    scenario = (
        _build_mermaid_container_js(["graph TD\n  A --> B"])
        + """
let bindCallCount = 0;
const origRender = mermaid.render;
mermaid.render = (id, source) => {
  return Promise.resolve({
    svg: '<svg>render</svg>',
    bindFunctions: () => { bindCallCount++; },
  });
};
postRenderMermaid(container);
setTimeout(() => setTimeout(() => {
  // Second invocation — cache hit, should still call
  // bindFunctions on the new container.
  const container2 = buildContainer(sources);
  postRenderMermaid(container2);
  setTimeout(() => {
    process.stdout.write(JSON.stringify({
      bindCallCount: bindCallCount,
    }));
  }, 0);
}, 0), 0);
"""
    )
    out = _run_mermaid_scenario(scenario)
    # First render binds; cache hit on second container also binds.
    assert out["bindCallCount"] == 2, (
        "bindFunctions was not re-applied on cache hit — interactive "
        "diagram features (links, callbacks) would silently break"
    )


def test_streaming_render_invokes_mermaid_post_render() -> None:
    """_streamingRenderApply must call postRenderMermaid so closed
    mermaid fences appear progressively during streaming, not only
    at stream_end via streamingRenderFinalize."""
    body = _RENDERER_JS.read_text(encoding="utf-8")
    # Bound the search to a window after the function declaration —
    # avoids the brittleness of stopping at the first inner-block
    # closing brace.
    start = body.index("function _streamingRenderApply")
    mermaid_call = body.find("postRenderMermaid(el)", start, start + 4000)
    assert mermaid_call != -1, (
        "_streamingRenderApply must call postRenderMermaid for "
        "progressive diagram rendering during streaming"
    )
