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

Both files are ES modules now; ``_demodulize`` strips the module syntax
so the script-semantics harness keeps working.  That is DELIBERATE, not
a shortcut: the mermaid harness pokes renderer-internal state
(``_mermaidState``) that script evaluation exposes as a context global
but a real module would encapsulate.  Module semantics themselves are
covered elsewhere (``test_shell_js`` parses every shared module with
``node --check`` as ``.mjs``); these tests pin renderer BEHAVIOR.
"""

from __future__ import annotations

import json
import re
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


def _demodulize(path: Path) -> str:
    """Strip ES-module syntax so ``vm.runInThisContext`` (script semantics)
    can evaluate the file: imports drop (the harness loads the whole
    dependency set into one shared context, so cross-file bindings resolve
    as context globals, exactly like the pre-module classic scripts), and
    ``export`` keywords peel off their declarations."""
    src = path.read_text(encoding="utf-8")
    src = re.sub(r"^import\s+\{[\s\S]*?\}\s+from\s+\"[^\"]+\";\s*$", "", src, flags=re.M)
    src = re.sub(r"^import\s+[^;\n]+;\s*$", "", src, flags=re.M)
    src = re.sub(
        r"^export\s+(?=(?:async\s+)?(?:function|const|let|var|class)\b)", "", src, flags=re.M
    )
    src = re.sub(r"^export\s*\{[^}]*\};\s*$", "", src, flags=re.M)
    return src


_HARNESS_TEMPLATE = """
const vm = require('vm');
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
vm.runInThisContext(%(utils_src)s);
vm.runInThisContext(%(renderer_src)s);
const input = %(input)s;
process.stdout.write(renderMarkdown(input));
"""


def _render(markdown: str) -> str:
    """Render ``markdown`` through renderer.js + return the HTML."""
    harness = _HARNESS_TEMPLATE % {
        "utils_src": json.dumps(_demodulize(_UTILS_JS)),
        "renderer_src": json.dumps(_demodulize(_RENDERER_JS)),
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


def test_single_dollar_inline_math_is_not_supported() -> None:
    """Single-$ inline math is intentionally disabled — $ collides
    with currency, env vars, and shell prompts in prose. Inline math
    must use the unambiguous \\(...\\) form. This test pins the
    behavior so a regex regression doesn't quietly resurrect it."""
    out = _render("The formula $E = mc^2$ is famous.")
    assert '<span class="katex">' not in out
    assert "$E = mc^2$" in out  # raw delimiters preserved


def test_dollar_currency_does_not_trigger_math() -> None:
    """The actual bug single-$ removal fixes: prose mentioning
    multiple currency amounts on one line used to get the span
    between two dollar signs eaten as a math expression."""
    out = _render("It costs $5 and the other is $10 each.")
    assert '<span class="katex">' not in out
    assert "$5" in out and "$10" in out


def test_dollar_env_vars_do_not_trigger_math() -> None:
    """Same class of bug as currency, with shell-style variables."""
    out = _render("Set $HOME and $PATH before running.")
    assert '<span class="katex">' not in out
    assert "$HOME" in out and "$PATH" in out


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
    """Only \\(...\\) renders; the $...$ form is left as raw prose
    (see test_single_dollar_inline_math_is_not_supported)."""
    out = _render(r"Here $x$ then \(y\) end.")
    assert out.count('<span class="katex">') == 1
    assert "[KATEX:y:inline]" in out
    assert "$x$" in out  # untouched


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


def test_dollar_signs_never_render_as_math_across_paragraphs() -> None:
    """Pre-removal regression covered the cross-paragraph eating bug
    for $...$. With single-$ inline math gone, the stronger guarantee
    is simply that no arrangement of $ signs ever produces math."""
    src = "Open $unterminated\n\nNext paragraph $x$ here."
    out = _render(src)
    assert '<span class="katex">' not in out
    assert "$unterminated" in out
    assert "$x$" in out


# ---------------------------------------------------------------------------
# Mermaid progressive rendering — source-keyed SVG cache
# ---------------------------------------------------------------------------


_MERMAID_HARNESS_TEMPLATE = """
const vm = require('vm');

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
      // Real DOMTokenList is array-like (length + indexed access) AND
      // exposes add/remove/contains. The hljs language-extraction
      // loop reads .length + [j], so we return a fresh Array snapshot
      // each get + bolt the mutator methods on. add/remove operate on
      // the live _classes set so subsequent reads see updates.
      const self = this;
      const arr = Array.from(self._classes);
      arr.add = (...c) => c.forEach((x) => self._classes.add(x));
      arr.remove = (...c) => c.forEach((x) => self._classes.delete(x));
      arr.contains = (c) => self._classes.has(c);
      return arr;
    },
    get className() { return Array.from(this._classes).join(' '); },
    set className(v) {
      this._classes = new Set(String(v).split(/\\s+/).filter(Boolean));
    },
    get textContent() {
      return this._textContent || this.children.map(c => c.textContent || '').join('');
    },
    set textContent(v) {
      // Real DOM: assigning textContent ALSO replaces innerHTML with
      // an entity-escaped representation of the same text. escapeHtml
      // (utils.js) round-trips via this side effect — without it,
      // every escapeHtml() call returns '' and renderMarkdown emits
      // empty <p> tags.
      this._textContent = v;
      this.children = [];
      this._innerHTML = String(v)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;');
    },
    get innerHTML() { return this._innerHTML; },
    set innerHTML(v) {
      // Real DOM invalidates the previous textContent when innerHTML
      // is replaced — leaving _textContent intact would return stale
      // data from subsequent textContent reads and mask bugs that
      // depend on innerHTML/textContent consistency. We don't HTML-
      // parse here, so the cheap correct behavior is to clear
      // _textContent and let the children-derived fallback in the
      // textContent getter (which is empty after this children = [])
      // take over.
      this._innerHTML = v;
      this.children = [];
      this._textContent = '';
    },
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
      // Supports the two selectors the post-render passes use:
      //   "pre code.language-mermaid"      (postRenderMermaid)
      //   "pre code[class*='language-']"   (postRenderHljs)
      const out = [];
      const wantsMermaid = selector === "pre code.language-mermaid";
      function matchesLangAttr(el) {
        for (const cls of el._classes) {
          if (cls.startsWith('language-')) return true;
        }
        return false;
      }
      function walk(node) {
        for (const c of (node.children || [])) {
          const isCodeInPre =
            c.tagName === 'CODE' &&
            c.parent && c.parent.tagName === 'PRE';
          if (isCodeInPre) {
            if (wantsMermaid) {
              if (c._classes.has('language-mermaid')) out.push(c);
            } else if (matchesLangAttr(c)) {
              out.push(c);
            }
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

// hljs stub. highlightElement mutates the element in place: replaces
// innerHTML with a deterministic synthetic span keyed by the source,
// and adds the hljs class — same surface postRenderHljs depends on.
// hljsHighlightCallCount lets tests assert "ran N times" semantics.
let hljsHighlightCallCount = 0;
global.hljs = {
  configure: () => {},
  highlightElement: (el) => {
    hljsHighlightCallCount++;
    el._classes.add('hljs');
    el._innerHTML = '<span class="hljs-tok">' + el._textContent + '</span>';
  },
};

vm.runInThisContext(%(utils_src)s);
vm.runInThisContext(%(renderer_src)s);

// Mermaid is normally lazy-loaded via _loadMermaid which fetches a
// script tag. Force-mark it ready so postRenderMermaid invokes the
// render path synchronously without trying to inject a script.  (This
// poke is WHY the harness script-evaluates the demodulized source: a
// real module would encapsulate _mermaidState.)
_mermaidState = 'ready';

%(scenario)s
"""


def _run_mermaid_scenario(scenario_js: str) -> dict[str, Any]:
    """Run a JS snippet against the mermaid-aware harness, return JSON output."""
    harness = _MERMAID_HARNESS_TEMPLATE % {
        "utils_src": json.dumps(_demodulize(_UTILS_JS)),
        "renderer_src": json.dumps(_demodulize(_RENDERER_JS)),
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
  _cacheFifoEntry(_mermaidSvgCache, 'src-' + i, {svg: 'svg-' + i, bindFunctions: null}, cap);
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
  _cacheFifoEntry(_mermaidSvgCache, 'src-' + i, {svg: 'svg-' + i, bindFunctions: null}, cap);
}
// Overwrite an existing entry — must not evict src-0.
_cacheFifoEntry(_mermaidSvgCache, 'src-5', {svg: 'svg-updated', bindFunctions: null}, cap);
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
_cacheFifoEntry(_mermaidSvgCache, 'src-1', {svg: 'old', bindFunctions: null}, _MERMAID_CACHE_MAX);
_cacheFifoEntry(_mermaidErrorCache, 'src-bad', 'old error', _MERMAID_CACHE_MAX);
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


# ---------------------------------------------------------------------------
#  _normalizeMermaidSource — autoquote labels with bare shape-delimiter
#  chars. Mermaid rejects unquoted ( ) [ ] { } inside other labels with
#  a "got 'PS'" parse error (paren-start in shape context). The two
#  diagrams in the screenshot regression case are encoded here verbatim.
# ---------------------------------------------------------------------------


def _run_normalize(source: str) -> str:
    """Drive _normalizeMermaidSource against the JS harness and return
    its output. The function is pure, so no container / mermaid stub
    setup is required."""
    scenario = f"""
const input = {json.dumps(source)};
const output = _normalizeMermaidSource(input);
process.stdout.write(JSON.stringify({{ output: output }}));
"""
    out = _run_mermaid_scenario(scenario)
    return str(out["output"])


# Diagram 1 from the screenshot regression — unquoted edge labels with
# parens and <br/> markers. Mermaid rejects both edge labels with
# "got 'PS'"; quoting them resolves it.
_SCREENSHOT_DIAGRAM_1_IN = (
    "flowchart LR\n"
    '    A["vllm-openai:nightly<br/>commit 5536fc0c0<br/>2026-05-11 11:59"]'
    " -->|22 upstream<br/>main commits<br/>(10 csrc, but<br/>no new bindings)|"
    ' B["fork merge_base<br/>7863fff6e5<br/>2026-05-12 00:27"]\n'
    "    B -->|13 jasl patches<br/>(Python only:<br/>tunings, kernels,"
    "<br/>warmup, etc.)|"
    ' C["ds4-sm120-preview-dev<br/>acc3455b1e"]'
)
_SCREENSHOT_DIAGRAM_1_OUT = (
    "flowchart LR\n"
    '    A["vllm-openai:nightly<br/>commit 5536fc0c0<br/>2026-05-11 11:59"]'
    ' -->|"22 upstream<br/>main commits<br/>(10 csrc, but<br/>no new bindings)"|'
    ' B["fork merge_base<br/>7863fff6e5<br/>2026-05-12 00:27"]\n'
    '    B -->|"13 jasl patches<br/>(Python only:<br/>tunings, kernels,'
    '<br/>warmup, etc.)"|'
    ' C["ds4-sm120-preview-dev<br/>acc3455b1e"]'
)

# Diagram 2 from the screenshot regression — unquoted RECTANGLE node
# label `D[untouched<br/>(.so, _version.py,<br/>install-vendored)]`.
# Same parser failure mode; quoting the bracket label fixes it.
_SCREENSHOT_DIAGRAM_2_IN = (
    "flowchart LR\n"
    "    A[nightly's vllm/<br/>installed package] --> B{tar -xf<br/>fork-vllm.tar}\n"
    "    B -->|in archive| C[overwritten with<br/>fork's version]\n"
    "    B -->|not in archive| D[untouched<br/>(.so, _version.py,"
    "<br/>install-vendored)]\n"
    "    E[explicit rm of 1 file<br/>deleted upstream] --> B"
)
_SCREENSHOT_DIAGRAM_2_OUT = (
    "flowchart LR\n"
    "    A[nightly's vllm/<br/>installed package] --> B{tar -xf<br/>fork-vllm.tar}\n"
    "    B -->|in archive| C[overwritten with<br/>fork's version]\n"
    '    B -->|not in archive| D["untouched<br/>(.so, _version.py,'
    '<br/>install-vendored)"]\n'
    "    E[explicit rm of 1 file<br/>deleted upstream] --> B"
)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (_SCREENSHOT_DIAGRAM_1_IN, _SCREENSHOT_DIAGRAM_1_OUT),
        (_SCREENSHOT_DIAGRAM_2_IN, _SCREENSHOT_DIAGRAM_2_OUT),
    ],
)
def test_mermaid_autoquote_fixes_screenshot_diagrams(source: str, expected: str) -> None:
    """The two exact diagrams from the screenshot regression. If
    these stop being rewritten with quoted labels, mermaid will
    again reject them with `Expecting ... got 'PS'` during live
    streaming."""
    assert _run_normalize(source) == expected


@pytest.mark.parametrize(
    "source",
    [
        # Clean diagram — no shape delimiters in any label.
        "graph TD\n  A[foo] --> B[bar]",
        # Edge label with no special chars.
        "A --> B\nA -->|plain text| B",
        # Already-correctly-quoted node label.
        'A["already (quoted)"] --> B',
        # Already-correctly-quoted edge label.
        'A -->|"already (quoted)"| B',
        # Cylinder shape — inner () is part of the shape syntax.
        "A[(database)] --> B",
        # Subroutine shape — inner [] is part of the shape syntax.
        "A[[subroutine]] --> B",
        # Trapezoid shape — inner / is part of the shape syntax.
        "A[/trapezoid/] --> B",
        # Reverse trapezoid.
        "A[\\trap\\] --> B",
        # Mermaid directive — braces here are config, not a label.
        '%%{init: {"theme": "dark"}}%%\ngraph TD\n  A --> B',
        # <br/> tags on their own don't trip quoting.
        "A[line1<br/>line2] --> B",
        # Sequence diagram — different grammar; we only target labels
        # in shape/edge syntax that match the regex anchors.
        "sequenceDiagram\n  A->>B: hello",
    ],
)
def test_mermaid_autoquote_leaves_valid_source_alone(source: str) -> None:
    """The autoquoter must not rewrite syntactically valid Mermaid —
    a false positive here would break a working diagram. Each case
    covers a syntax form whose delimiters are intentional and must
    not be wrapped."""
    assert _run_normalize(source) == source


def test_mermaid_autoquote_edge_label_with_parens() -> None:
    """Bare-parens edge label gets wrapped. The bare `(` would
    otherwise re-enter Mermaid's shape parser."""
    src = "A -->|note (with parens)| B"
    assert _run_normalize(src) == 'A -->|"note (with parens)"| B'


def test_mermaid_autoquote_node_label_with_parens() -> None:
    """Bare-parens node label gets wrapped."""
    src = "D[label (foo, bar)]"
    assert _run_normalize(src) == 'D["label (foo, bar)"]'


def test_mermaid_autoquote_node_label_with_braces() -> None:
    """Bare-braces in a rectangle label get wrapped. (Diamond {}
    shapes are left alone — only single-bracket [] labels are
    rewritten.)"""
    src = "A[config {key: value}]"
    assert _run_normalize(src) == 'A["config {key: value}"]'


def test_mermaid_autoquote_preserves_br_tag_with_parens() -> None:
    """`<br/>` inside a label that also has parens stays — only the
    quoting needs to be added around the whole label."""
    src = "A[line1<br/>(line2)] --> B"
    assert _run_normalize(src) == 'A["line1<br/>(line2)"] --> B'


def test_mermaid_autoquote_skips_label_with_internal_quote() -> None:
    """If a label contains a literal `"`, wrapping would produce
    nested unescaped quotes. The autoquoter must punt — leaving the
    parse error to surface, rather than silently producing a worse
    one."""
    src = 'A[he said "hi" (lol)]'
    assert _run_normalize(src) == src


def test_mermaid_autoquote_multiple_edges_on_one_line() -> None:
    """Both edge labels on a single line get rewritten independently."""
    src = "A -->|first (paren)| B -->|second (paren)| C"
    expected = 'A -->|"first (paren)"| B -->|"second (paren)"| C'
    assert _run_normalize(src) == expected


def test_mermaid_autoquote_normalized_source_hits_cache() -> None:
    """The SVG cache keys on the normalized source — same malformed
    input that the LLM streamed earlier still hits the cache on
    re-render rather than re-invoking mermaid.render every tick."""
    bad = "A[label (with parens)] --> B"
    scenario = (
        _build_mermaid_container_js([bad])
        + _MERMAID_DRAIN_JS
        + """
postRenderMermaid(container);
setTimeout(() => setTimeout(() => {
  const container2 = buildContainer(sources);
  postRenderMermaid(container2);
  setTimeout(() => {
    process.stdout.write(JSON.stringify({
      renderCalls: renderCallCount,
      normalized: container.children[0]._attrs['data-mermaid-source'],
    }));
  }, 0);
}, 0), 0);
"""
    )
    out = _run_mermaid_scenario(scenario)
    assert out["renderCalls"] == 1, "second render bypassed the cache"
    assert out["normalized"] == 'A["label (with parens)"] --> B'


def test_mermaid_normalize_memo_populates_on_first_call() -> None:
    """First postRenderMermaid call populates _mermaidNormalizeCache
    with a raw→normalized entry. A second call on identical raw
    textContent then hits the memo (size stays at 1, no second
    normalize call), which is the perf-1 fix — avoids re-running
    split + per-line regex per rAF tick when the diagram hasn't
    changed."""
    bad = "A[label (with parens)] --> B"
    scenario = (
        _build_mermaid_container_js([bad])
        + _MERMAID_DRAIN_JS
        + """
postRenderMermaid(container);
const sizeAfterFirst = _mermaidNormalizeCache.size;
const cachedNorm = _mermaidNormalizeCache.get(sources[0]);
// Re-render on a fresh container with the same source.
const container2 = buildContainer(sources);
postRenderMermaid(container2);
setTimeout(() => setTimeout(() => {
  process.stdout.write(JSON.stringify({
    sizeAfterFirst: sizeAfterFirst,
    cachedNorm: cachedNorm,
    sizeAfterSecond: _mermaidNormalizeCache.size,
  }));
}, 0), 0);
"""
    )
    out = _run_mermaid_scenario(scenario)
    assert out["sizeAfterFirst"] == 1, "first call didn't populate normalize memo"
    assert out["cachedNorm"] == 'A["label (with parens)"] --> B'
    assert out["sizeAfterSecond"] == 1, (
        "second call added a new entry — memo missed on identical source"
    )


def test_mermaid_normalize_memo_is_consulted_before_normalize() -> None:
    """Pre-seed _mermaidNormalizeCache with a sentinel value for a
    raw source. postRenderMermaid must use the sentinel rather than
    re-running _normalizeMermaidSource. Catches a regression where
    the memo gets populated but the lookup path is skipped."""
    bad = "A[label (with parens)] --> B"
    sentinel = "SENTINEL_FROM_MEMO --> X"
    raw_js = json.dumps(bad)
    sentinel_js = json.dumps(sentinel)
    scenario = (
        _build_mermaid_container_js([bad])
        + _MERMAID_DRAIN_JS
        + f"""
_mermaidNormalizeCache.set({raw_js}, {sentinel_js});
postRenderMermaid(container);
setTimeout(() => setTimeout(() => {{
  process.stdout.write(JSON.stringify({{
    sourceAttr: container.children[0]._attrs['data-mermaid-source'],
  }}));
}}, 0), 0);
"""
    )
    out = _run_mermaid_scenario(scenario)
    assert out["sourceAttr"] == sentinel, (
        "postRenderMermaid bypassed the normalize memo and re-ran normalize"
    )


def test_mermaid_normalize_memo_distinct_sources_cache_separately() -> None:
    """Two distinct raw sources produce two memo entries. Confirms
    the memo keys on raw textContent, not on something coarser like
    container identity."""
    bad1 = "A[label (with parens)] --> B"
    bad2 = "C[other (label)] --> D"
    scenario = (
        _build_mermaid_container_js([bad1, bad2])
        + _MERMAID_DRAIN_JS
        + """
postRenderMermaid(container);
setTimeout(() => setTimeout(() => {
  process.stdout.write(JSON.stringify({
    size: _mermaidNormalizeCache.size,
    hasBad1: _mermaidNormalizeCache.has(sources[0]),
    hasBad2: _mermaidNormalizeCache.has(sources[1]),
  }));
}, 0), 0);
"""
    )
    out = _run_mermaid_scenario(scenario)
    assert out["size"] == 2
    assert out["hasBad1"] is True
    assert out["hasBad2"] is True


# ---------------------------------------------------------------------------
#  Code-fence pairing — close requires \n / EOS, content can't cross
#  another close-pattern. Repros the streaming bug where ```mermaid +
#  later ```python were paired by the regex, handing mermaid a
#  truncated source.
# ---------------------------------------------------------------------------


def _render_md(source: str) -> str:
    """Drive renderMarkdown against the JS harness and return the
    rendered HTML. The function is a pure string transform; no DOM
    container scaffolding is required."""
    scenario = f"""
const input = {json.dumps(source)};
const output = renderMarkdown(input);
process.stdout.write(JSON.stringify({{ output: output }}));
"""
    out = _run_mermaid_scenario(scenario)
    return str(out["output"])


_FENCE = "```"


def test_fence_partial_open_emits_no_code_block() -> None:
    """While a fence is still open and there's no other ``` later in
    the buffer, no <code> block is emitted — the open fence stays as
    plain markdown text until the real close arrives."""
    src = "Intro\n" + _FENCE + 'mermaid\nA["x"] -->|note (with parens)| B["y"]\nstill streaming'
    html = _render_md(src)
    assert "<code" not in html, f"open fence should not emit <code> mid-stream: {html!r}"


def test_fence_partial_with_later_open_does_not_pair_wrongly() -> None:
    """Before the fence-pair fix: an unclosed ```mermaid followed by
    a ```python (also unclosed) would have paired up as
    <code class=mermaid>...</code>python..., handing mermaid a
    truncated source. With the new regex, neither fence emits a
    block until its OWN closing line arrives."""
    src = "Intro\n" + _FENCE + "mermaid\nA --> B\n" + _FENCE + 'python\nprint("hi")'
    html = _render_md(src)
    assert 'class="language-mermaid"' not in html, (
        f"mermaid fence should not emit while open: {html!r}"
    )
    assert 'class="language-python"' not in html, (
        f"python fence should not emit while open: {html!r}"
    )


def test_fence_close_paired_with_next_open_is_rejected() -> None:
    """Repro of the live-streaming failure: mermaid fence open, then
    ```python opens and ``` closes the python block. Without the
    fix, the regex paired mermaid's open with python's *open* (or
    backtracked all the way to python's close), producing
    <code class=mermaid>truncated</code>. With the fix mermaid stays
    open (content can't cross another \\1 run; close must be at line
    boundary) and only python's pair matches."""
    src = (
        "Intro\n"
        + _FENCE
        + 'mermaid\nA["x"] -->|note (with parens)| B["y"]\n'
        + _FENCE
        + 'python\nprint("hi")\n'
        + _FENCE
    )
    html = _render_md(src)
    assert 'class="language-mermaid"' not in html, f"mermaid fence misparing reintroduced: {html!r}"
    assert 'class="language-python"' in html, f"python fence on its own should match: {html!r}"


def test_fence_closed_emits_code_block() -> None:
    """Baseline: a properly closed fence with its close on its own
    line emits the <code> block as expected — the anchor doesn't
    break the normal case."""
    src = "Intro\n" + _FENCE + "python\nimport os\n" + _FENCE + "\nAfter"
    html = _render_md(src)
    assert 'class="language-python"' in html
    assert "import os" in html


def test_fence_close_at_end_of_buffer_emits() -> None:
    """A fence that closes at the very end of the buffer (no trailing
    newline) still emits — the anchor accepts end-of-string as a
    valid line boundary, so the rehydration / static-render path
    where the buffer ends cleanly at ``` still works."""
    src = "Intro\n" + _FENCE + "python\nimport os\n" + _FENCE
    html = _render_md(src)
    assert 'class="language-python"' in html
    assert "import os" in html


def test_fence_close_with_trailing_whitespace_emits() -> None:
    """A close followed only by spaces / tabs before \\n still counts
    — CommonMark allows trailing whitespace on the close line."""
    src = "Intro\n" + _FENCE + "python\nimport os\n" + _FENCE + "   \nAfter"
    html = _render_md(src)
    assert 'class="language-python"' in html


# ---------------------------------------------------------------------------
#  postRenderHljs — progressive syntax highlighting + source-keyed cache
# ---------------------------------------------------------------------------


def _build_hljs_container_js(blocks: list[tuple[str, str]]) -> str:
    """Build a container with <pre><code class="language-LANG"> blocks.

    ``blocks`` is a list of ``(language, source)`` tuples — the language
    becomes the ``language-X`` class, the source becomes textContent."""
    arr = "[" + ", ".join(f"[{json.dumps(lang)}, {json.dumps(src)}]" for lang, src in blocks) + "]"
    return f"""
function buildHljsContainer(blocks) {{
  const container = document.createElement('div');
  for (const [lang, src] of blocks) {{
    const pre = document.createElement('pre');
    const code = document.createElement('code');
    code.classList.add('language-' + lang);
    code.textContent = src;
    pre.appendChild(code);
    container.appendChild(pre);
  }}
  return container;
}}
const blocks = {arr};
const container = buildHljsContainer(blocks);
"""


def test_hljs_cache_hit_skips_highlight_call() -> None:
    """Two postRenderHljs calls on identical source must invoke
    hljs.highlightElement exactly once — the second call hits the
    cache and applies the stored markup synchronously. Mirrors the
    mermaid SVG-cache invariant that lets streamingRender fire on
    every rAF tick without re-tokenizing every code block."""
    scenario = (
        _build_hljs_container_js([("python", "import os")])
        + """
postRenderHljs(container);
const container2 = buildHljsContainer(blocks);
postRenderHljs(container2);
process.stdout.write(JSON.stringify({
  highlightCalls: hljsHighlightCallCount,
  cacheSize: _hljsCache.size,
  firstHtml: container.children[0].children[0]._innerHTML,
  secondHtml: container2.children[0].children[0]._innerHTML,
  secondHasHljsClass: container2.children[0].children[0]._classes.has('hljs'),
}));
"""
    )
    out = _run_mermaid_scenario(scenario)
    assert out["highlightCalls"] == 1, (
        "second postRenderHljs call invoked highlightElement — cache miss"
    )
    assert out["cacheSize"] == 1
    assert out["firstHtml"] == out["secondHtml"]
    assert out["secondHasHljsClass"] is True


def test_hljs_distinct_sources_highlight_independently() -> None:
    """Distinct sources each trigger one highlight and cache one entry.
    Cache key includes the source string, not e.g. just the language."""
    scenario = (
        _build_hljs_container_js([("python", "import os"), ("python", "print('hi')")])
        + """
postRenderHljs(container);
process.stdout.write(JSON.stringify({
  highlightCalls: hljsHighlightCallCount,
  cacheSize: _hljsCache.size,
}));
"""
    )
    out = _run_mermaid_scenario(scenario)
    assert out["highlightCalls"] == 2
    assert out["cacheSize"] == 2


def test_hljs_cache_separates_by_language() -> None:
    """Same source text under different language fences must NOT
    collide in the cache — language is part of the key. Otherwise a
    `python` block of `foo` and a `ruby` block of `foo` would share
    a single (wrongly-highlighted) cache entry."""
    scenario = (
        _build_hljs_container_js([("python", "foo"), ("ruby", "foo")])
        + """
postRenderHljs(container);
process.stdout.write(JSON.stringify({
  highlightCalls: hljsHighlightCallCount,
  cacheSize: _hljsCache.size,
}));
"""
    )
    out = _run_mermaid_scenario(scenario)
    assert out["highlightCalls"] == 2
    assert out["cacheSize"] == 2


def test_hljs_skips_no_highlight_langs() -> None:
    """language-mermaid / language-text / language-plaintext etc. must
    get the `nohighlight` class without invoking hljs.highlightElement.
    Highlighting plaintext or mermaid source would be both wasteful
    and ugly."""
    scenario = (
        _build_hljs_container_js(
            [("mermaid", "graph TD\\nA-->B"), ("text", "plain"), ("plaintext", "p")]
        )
        + """
postRenderHljs(container);
process.stdout.write(JSON.stringify({
  highlightCalls: hljsHighlightCallCount,
  cacheSize: _hljsCache.size,
  mermaidNoHighlight: container.children[0].children[0]._classes.has('nohighlight'),
  textNoHighlight: container.children[1].children[0]._classes.has('nohighlight'),
  plaintextNoHighlight: container.children[2].children[0]._classes.has('nohighlight'),
}));
"""
    )
    out = _run_mermaid_scenario(scenario)
    assert out["highlightCalls"] == 0
    assert out["cacheSize"] == 0
    assert out["mermaidNoHighlight"] is True
    assert out["textNoHighlight"] is True
    assert out["plaintextNoHighlight"] is True


def test_hljs_terminal_lang_marks_pre_for_terminal_styling() -> None:
    """Shell-family languages (bash / sh / zsh / console / terminal)
    must add the `code-terminal` class to the parent <pre>, so the
    stylesheet can give them the terminal look-and-feel."""
    scenario = (
        _build_hljs_container_js([("bash", "echo hi")])
        + """
postRenderHljs(container);
process.stdout.write(JSON.stringify({
  highlightCalls: hljsHighlightCallCount,
  preHasTerminalClass: container.children[0]._classes.has('code-terminal'),
}));
"""
    )
    out = _run_mermaid_scenario(scenario)
    assert out["highlightCalls"] == 1
    assert out["preHasTerminalClass"] is True


def test_hljs_cache_evicts_oldest_at_cap() -> None:
    """FIFO eviction at _HLJS_CACHE_MAX. Mirrors the mermaid cache —
    prevents unbounded growth on long sessions with many distinct
    code blocks."""
    scenario = """
const cap = _HLJS_CACHE_MAX;
for (let i = 0; i < cap + 5; i++) {
  _cacheFifoEntry(_hljsCache, 'key-' + i, 'val-' + i, cap);
}
process.stdout.write(JSON.stringify({
  size: _hljsCache.size,
  hasOldest: _hljsCache.has('key-0'),
  hasNewest: _hljsCache.has('key-' + (cap + 4)),
}));
"""
    out = _run_mermaid_scenario(scenario)
    assert out["size"] == 64
    assert out["hasOldest"] is False
    assert out["hasNewest"] is True


def test_hljs_overwrite_does_not_evict() -> None:
    """Overwriting an existing key is an in-place update, not a new
    insertion — must not evict the oldest unrelated entry. Same
    invariant as the mermaid cache."""
    scenario = """
const cap = _HLJS_CACHE_MAX;
for (let i = 0; i < cap; i++) {
  _cacheFifoEntry(_hljsCache, 'key-' + i, 'val-' + i, cap);
}
_cacheFifoEntry(_hljsCache, 'key-5', 'val-updated', cap);
process.stdout.write(JSON.stringify({
  size: _hljsCache.size,
  hasOldest: _hljsCache.has('key-0'),
  updated: _hljsCache.get('key-5'),
}));
"""
    out = _run_mermaid_scenario(scenario)
    assert out["size"] == 64
    assert out["hasOldest"] is True, "overwrite evicted oldest unnecessarily"
    assert out["updated"] == "val-updated"


def test_post_render_markdown_invokes_hljs() -> None:
    """postRenderMarkdown is the public end-of-stream entry point and
    must still run syntax highlighting after the postRenderHljs
    refactor — regression guard for the public API surface that
    app.js / coordinator code already call."""
    scenario = (
        _build_hljs_container_js([("python", "import os")])
        + """
postRenderMarkdown(container);
process.stdout.write(JSON.stringify({
  highlightCalls: hljsHighlightCallCount,
  hasHljsClass: container.children[0].children[0]._classes.has('hljs'),
}));
"""
    )
    out = _run_mermaid_scenario(scenario)
    assert out["highlightCalls"] == 1
    assert out["hasHljsClass"] is True


def test_streaming_render_invokes_hljs() -> None:
    """_streamingRenderApply must call postRenderHljs so closed code
    fences appear progressively (syntax-highlighted) during streaming,
    not only at stream_end via streamingRenderFinalize. The cache
    keeps the per-tick cost down to a synchronous lookup."""
    body = _RENDERER_JS.read_text(encoding="utf-8")
    start = body.index("function _streamingRenderApply")
    hljs_call = body.find("postRenderHljs(el)", start, start + 4000)
    assert hljs_call != -1, (
        "_streamingRenderApply must call postRenderHljs for progressive "
        "syntax highlighting during streaming"
    )


# ---------------------------------------------------------------------------
# Attribute-context interpolation lint + pin tests
# ---------------------------------------------------------------------------

# The JS source uses `'...attr="' + var + '"...'` — so the literal text
# between `=` and `+` is `"` (the HTML-attribute opener inside the
# JS string) followed by `'` (the JS-string closer). Match that pair,
# then optional whitespace + `+` + whitespace + an identifier.
_RENDERER_ATTR_INTERP_RE = re.compile(
    r"=[\"'][\"']\s*\+\s*(?!escapeHtml\b)([a-zA-Z_][a-zA-Z0-9_]*)"
)

# Identifiers exempted from the lint. Each entry is reviewer-approved
# as known-safe; adding a new one requires a comment explaining why.
_RENDERER_KNOWN_SAFE_IDENTIFIERS = {
    # CALLOUT_TYPES enum lookup ({label, icon} of fixed strings — Note,
    # Tip, Important, Warning, Caution). `alertType` matched by regex
    # /(NOTE|TIP|IMPORTANT|WARNING|CAUTION)/, so .toLowerCase() output
    # is also a fixed set; flows through `info`.
    "info",
}


def test_renderer_attribute_context_interpolation_is_safe() -> None:
    """Pin: every `attr="' + var` string-concat interpolation in
    renderer.js must use one of:

    * `escapeHtml(...)` at the call site (allowed by the negative
      lookahead in the regex),
    * an identifier matching `safe[A-Z]…` (camelCase convention: the
      value is pre-escaped at assignment), or
    * an identifier in :data:`_RENDERER_KNOWN_SAFE_IDENTIFIERS`
      (reviewer-approved enum lookups / counters).

    Defence-in-depth lint per issue #553. The current call sites are
    already safe today via ``inlineMarkdown``'s leading ``escapeHtml``
    pass, but that invariant is non-local — a refactor moving image
    or link rendering out of ``inlineMarkdown`` would silently
    regress it. The lint locks in the local-escape posture so the
    safety property is structural rather than emergent.
    """
    body = _RENDERER_JS.read_text(encoding="utf-8")
    lines = body.splitlines()
    offenders: list[tuple[int, str, str]] = []
    for m in _RENDERER_ATTR_INTERP_RE.finditer(body):
        ident = m.group(1)
        if len(ident) > 4 and ident.startswith("safe") and ident[4].isupper():
            continue
        if ident in _RENDERER_KNOWN_SAFE_IDENTIFIERS:
            continue
        line_no = body.count("\n", 0, m.start()) + 1
        offenders.append((line_no, ident, lines[line_no - 1].rstrip()))
    assert not offenders, (
        f"Found {len(offenders)} unsafe attribute-context "
        f"interpolation(s) in renderer.js:\n"
        + "\n".join(
            f"  line {n}: {ident!r}  in  {line.strip()[:100]}" for n, ident, line in offenders[:10]
        )
        + "\nEither wrap with escapeHtml() at the call site, rename "
        "the variable to safeXxx (after verifying it is pre-escaped "
        "at assignment), or add the identifier to "
        "_RENDERER_KNOWN_SAFE_IDENTIFIERS with a comment explaining "
        "why it is known-safe (e.g. enum lookup, integer counter)."
    )


_HANDLER_ATTRS = frozenset(
    {
        "onerror",
        "onload",
        "onmouseover",
        "onclick",
        "onmouseout",
        "onfocus",
        "onblur",
        "onchange",
        "onsubmit",
        "onkeydown",
        "onkeyup",
        "onkeypress",
    }
)


def _parse_renderer_html(html: str) -> tuple[list[str], list[tuple[str, str]]]:
    """Parse ``html`` and return ``(start_tags, (tag, attr_name) pairs)``.

    Two return values because:

    * ``start_tags`` records every start tag regardless of whether it
      carries attributes, so a bare ``<script>`` injection (no attrs)
      cannot slip past a tag-presence check.
    * ``attr_pairs`` records every attribute-bearing tag for the
      event-handler-attribute assertion.

    Substring checks on the raw output are too noisy: the literal text
    ``onerror=&amp;quot;`` is safe when it sits inside a parsed
    attribute value, but the substring still matches."""
    from html.parser import HTMLParser

    class _Collector(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.tags: list[str] = []
            self.attrs: list[tuple[str, str]] = []

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            self.tags.append(tag)
            for name, _value in attrs:
                self.attrs.append((tag, name))

    p = _Collector()
    p.feed(html)
    return p.tags, p.attrs


def _assert_no_handler_attrs(html: str) -> None:
    _tags, attrs = _parse_renderer_html(html)
    handlers = [(tag, name) for tag, name in attrs if name in _HANDLER_ATTRS]
    assert not handlers, (
        f"Renderer output materialized event-handler attribute(s) "
        f"{handlers!r} — attribute-boundary escape regression. "
        f"Full output:\n{html}"
    )


def test_attacker_image_url_with_quote_does_not_break_attribute() -> None:
    """Pin: an image URL containing embedded double-quote characters
    must NOT escape the ``data-src``/``data-alt`` attribute boundary.
    The injected text remains inside the attribute value; no extra
    attributes (``onerror``, etc.) materialize on the rendered span."""
    out = _render('![alt](https://x/y.png" onerror="alert(1))')
    _assert_no_handler_attrs(out)


def test_attacker_image_alt_with_quote_does_not_break_attribute() -> None:
    """Pin: an image alt text containing embedded double-quote
    characters must not break the ``data-alt`` / ``aria-label``
    attribute boundaries."""
    out = _render('![alt" onerror="alert(1)](https://x/y.png)')
    _assert_no_handler_attrs(out)


def test_attacker_link_url_with_quote_does_not_break_attribute() -> None:
    """Pin: a link URL containing embedded double-quote characters
    must not escape the ``href`` attribute boundary."""
    out = _render('[click](https://x/y" onmouseover="alert(1))')
    _assert_no_handler_attrs(out)


def test_attacker_link_label_with_quote_renders_as_text() -> None:
    """Pin: a link label containing embedded ``<`` characters must
    render as escaped text inside the anchor, not as a real tag.

    Uses :func:`_parse_renderer_html` (not the attr-pairs accessor)
    because a bare ``<script>`` injection has no attributes and would
    be invisible to a (tag, attr) pair listing."""
    out = _render("[<script>alert(1)</script>](https://x/y)")
    tags, _attrs = _parse_renderer_html(out)
    assert "script" not in tags, "Link label leaked a real <script> element:\n" + out


def test_image_url_with_ampersand_not_double_escaped() -> None:
    """Pin: a query-string URL must not double-escape ``&``.

    inlineMarkdown's leading ``escapeHtml(text)`` turns ``&`` into
    ``&amp;`` once. Any local re-escape on the captured ``url`` would
    produce ``&amp;amp;`` in the attribute — which decodes to literal
    ``&amp;`` at attribute-parse time, breaks ``getAttribute`` +
    ``new URL`` round-trip, and silently corrupts query strings."""
    out = _render("![alt](https://x/y?a=1&b=2)")
    assert 'data-src="https://x/y?a=1&amp;b=2"' in out, (
        "Expected single &amp; encoding for `&`; got:\n" + out
    )
    assert "&amp;amp;" not in out, (
        "URL was double-escaped (`&` → `&amp;amp;`); breaks getAttribute "
        "+ new URL round-trip. Full output:\n" + out
    )


def test_link_url_with_ampersand_not_double_escaped() -> None:
    """Same as the image case, for link ``href``."""
    out = _render("[docs](https://example.com/p?a=1&b=2)")
    assert 'href="https://example.com/p?a=1&amp;b=2"' in out, (
        "Expected single &amp; encoding for `&`; got:\n" + out
    )
    assert "&amp;amp;" not in out


def test_render_markdown_depth_capped_and_throw_safe() -> None:
    """Perf-audit P0: ``renderMarkdown`` recurses for blockquote/callout
    bodies, and a few KB of nested ``"> "`` used to overflow the call stack
    mid-render.  The exported wrapper depth-caps the recursion (bailing to
    escaped text) and keeps the ``_fnDepth`` accounting in a try/finally so a
    body throw can't strand it elevated (which froze ``_fnScopeId`` and
    collided footnote ids for every later message)."""
    body = _RENDERER_JS.read_text(encoding="utf-8")
    assert "var _MD_MAX_DEPTH" in body
    assert "_fnDepth >= _MD_MAX_DEPTH" in body
    wrapper = body.index("export function renderMarkdown(text)")
    seg = body[wrapper : body.index("function _renderMarkdownBody(text)")]
    assert "try {" in seg and "finally {" in seg and "_fnDepth--;" in seg, (
        "depth accounting must ride a try/finally in the wrapper"
    )


def test_streaming_apply_marks_buffer_only_on_success() -> None:
    """Perf-audit P0: ``_streamingRenderApply`` must set
    ``el._lastRenderedBuffer`` only AFTER a successful render, with a
    plain-text fallback on throw.  Marking before the render made an errored
    frame look done — the finalize short-circuit then pinned the broken DOM
    forever.  The mermaid chain must also be rejection-proof (a sync throw in
    a settle handler used to leave every later diagram stuck at 'Loading
    diagram…')."""
    body = _RENDERER_JS.read_text(encoding="utf-8")
    apply_at = body.index("function _streamingRenderApply")
    seg = body[apply_at : apply_at + 2000]
    render_at = seg.index("renderMarkdown(buffer)")
    mark_at = seg.index("el._lastRenderedBuffer = buffer;")
    assert render_at < mark_at, "buffer must be marked rendered only on success"
    assert "el.textContent = buffer;" in seg
    chain_at = body.index("_mermaidRenderChain = _mermaidRenderChain")
    assert ".catch(function (e) {" in body[chain_at : chain_at + 3500], (
        "every mermaid chain link must settle back to fulfilled"
    )


# ---------------------------------------------------------------------------
# Renderer containment escapes (frontend-render-containment-brief)
#
# The renderer protects structural blocks with in-band NUL-framed sentinels
# (NUL + two-letter-tag + index + NUL, e.g. code-block 0 -> chr(0)+"CB0"+chr(0)).
# escapeHtml preserves U+0000, so model/tool text carrying such a sequence used
# to FORGE a sentinel: the shared restore pass rewrote every match, duplicating
# or relocating a protected block (B1), printing literal "undefined" for an
# out-of-range index (B2), or injecting a restored span across a container (B3).
# Fix 1 strips U+0000 (NUL) at the TOP-LEVEL render entry only, so no forged
# NUL survives to frame a sentinel while generated (recursive-frame) sentinels
# are left intact.  Only NUL is stripped — every other control byte survives so
# code fences show pasted source verbatim.  Inputs build NUL via chr(0) (never
# a literal escape) per the brief.
# ---------------------------------------------------------------------------

_NUL = chr(0)


def test_forged_code_block_sentinel_does_not_duplicate_block() -> None:
    """B1: prose carrying a forged ``chr(0)+CB0+chr(0)`` used to make the
    shared restore pass emit the protected code block a SECOND time (content
    spoofing / relocation).  Stripping NUL at the entry neutralises the
    forgery: exactly one code block, no leaked sentinel."""
    md = "```python\nprint('hi')\n```\n\nprose " + _NUL + "CB0" + _NUL + " end"
    out = _render(md)
    assert out.count("<pre>") == 1, "forged CB sentinel duplicated the block:\n" + out
    assert out.count("print(") == 1
    assert _NUL not in out, "raw NUL / forged sentinel leaked into output"


def test_forged_out_of_range_sentinel_does_not_print_undefined() -> None:
    """B2: ``chr(0)+IC7+chr(0)`` with no inline codes used to restore
    ``inlineCodes[7]`` -> literal ``undefined`` in the rendered text.  After
    the entry strip the forged framing is gone, so no ``undefined`` appears."""
    out = _render("text " + _NUL + "IC7" + _NUL + " tail")
    assert "undefined" not in out, "out-of-range forged sentinel printed 'undefined':\n" + out
    assert _NUL not in out


def test_control_strip_preserves_legit_fence_and_inline() -> None:
    """Fix 1 must not disturb legitimately generated sentinels: a normal
    fence and inline-code span still render after the entry strip (the strip
    only removes caller-supplied control chars, which are never valid data)."""
    out = _render("Here is `inline` and a block:\n\n```py\nx = 1\n```")
    assert "<code>inline</code>" in out
    assert "<pre><code" in out
    assert "x = 1" in out
    assert _NUL not in out


def test_strip_removes_only_nul_preserving_other_control_bytes() -> None:
    """The entry strip removes ONLY NUL (the sentinel-framing byte), so a code
    fence still shows pasted control bytes (terminal output, ANSI escapes)
    verbatim.  Stripping the whole C0/DEL range would silently corrupt code
    samples; only NUL can forge a sentinel."""
    esc = chr(27)  # ANSI escape — legitimate in pasted terminal output
    out = _render("```\nbefore " + esc + "[0m after " + _NUL + " end\n```")
    assert esc in out, "ESC (0x1b) must survive inside a code fence:\n" + repr(out)
    assert _NUL not in out, "NUL must still be stripped (sentinel-framing byte)"
    assert "before " in out and " end" in out


def test_forged_inline_sentinel_not_injected_inside_fence() -> None:
    """B3: a forged ``chr(0)+IC0+chr(0)`` placed inside a real code fence
    used to be substituted AFTER the fence was restored (CB restores before
    IC), injecting a real ``<code>`` span into the ``<pre>``.  With a genuine
    inline-code span present (so inlineCodes[0] exists), the forged reference
    must NOT clone it into the code block."""
    md = "`real`\n\n```text\nbefore " + _NUL + "IC0" + _NUL + " after\n```"
    out = _render(md)
    assert out.count("<code>real</code>") == 1, "forged IC sentinel injected into <pre>:\n" + out
    assert _NUL not in out
    assert "before IC0 after" in out, "fence body should show the inert forged tag as text"


def test_nul_strip_scoped_to_top_level_call() -> None:
    """Structural pin for the PLAUSIBLE placement refinement: the NUL strip
    lives inside the ``_fnDepth === 0`` guard of the exported wrapper, NOT in
    ``_renderMarkdownBody`` (which runs at every recursion depth).  An
    unconditional strip would shred the generated sentinels that recursive
    ``<details>``/footnote frames legitimately carry — foreclosing the
    recursive-frame fix.  Recursion must reach raw text with its sentinels."""
    body = _RENDERER_JS.read_text(encoding="utf-8")
    assert "_NUL_STRIP_RE" in body
    wrapper = body.index("export function renderMarkdown(text)")
    body_fn = body.index("function _renderMarkdownBody(text)")
    seg = body[wrapper:body_fn]
    guard_at = seg.index("_fnDepth === 0")
    strip_at = seg.index("_NUL_STRIP_RE", guard_at)
    incr_at = seg.index("_fnDepth++")
    assert guard_at < strip_at < incr_at, (
        "the NUL strip must run inside the top-level (_fnDepth === 0) "
        "guard, before the depth increment"
    )
    assert "_NUL_STRIP_RE" not in body[body_fn:], (
        "strip must not live in _renderMarkdownBody (would run at every depth)"
    )


def test_recursive_frame_degrades_without_literal_undefined() -> None:
    """Fix 2 floor for the NEW-1 residual: a recursive render frame
    (``<details>`` body, footnote definition) whose fresh block arrays cannot
    resolve an outer-scope sentinel must NOT print the literal word
    ``undefined``.  The restore callbacks return the (inert) matched sentinel
    instead.  (This asserts only the ``undefined`` floor — Fix 5 is what makes
    the body actually render; the raw sentinel that the node harness preserves
    here is dropped by a real browser's tokenizer.)"""
    details = _render("<details>\n<summary>x</summary>\n\n```py\nsecret_code()\n```\n\n</details>")
    assert "undefined" not in details, "code-in-<details> printed 'undefined':\n" + details
    footnote = _render("See[^1].\n\n[^1]: a `snippet` ok")
    assert "undefined" not in footnote, "inline-code-in-footnote printed 'undefined':\n" + footnote


def test_standalone_code_block_not_wrapped_in_paragraph() -> None:
    """Fix 6 (NEW-3): code blocks need the ``<p>SENTINEL</p>`` unwrap variant
    that DT/BQ/MB/TB already have.  Without it a lone fenced block emits
    ``<p><pre>…</pre></p>``, which a real browser splits into a stray empty
    ``<p>`` before the ``<pre>``.  The unwrap removes the wrapping paragraph."""
    out = _render("```py\nx = 1\n```")
    assert "<pre><code" in out
    assert "<p><pre>" not in out, "code block still wrapped in a paragraph:\n" + out
    assert out.strip().startswith("<pre>"), "code block should not be paragraph-wrapped:\n" + out


# ---------------------------------------------------------------------------
# Fix 3 — blockquote-in-fence (B4): fence protection must run before (and
# mask) the line-based blockquote pass, with the fence open anchored to line
# start so a blockquoted fence (`> ```) is NOT matched at column > 0.
# ---------------------------------------------------------------------------


def test_blockquote_inside_fence_not_extracted() -> None:
    """B4 (the common one, no special chars): ``> `` lines INSIDE a code
    fence used to be scooped out by the blockquote pre-pass (which ran first)
    and rendered as a real ``<blockquote>`` nested in ``<pre><code>`` — a
    shell transcript or quoted-email code block would sprout a headline.  The
    fence pass now runs first and masks the region."""
    out = _render("```text\nplain\n> quoted\nafter\n```")
    assert "<blockquote>" not in out, "blockquote extracted from inside a fence:\n" + out
    assert "<pre><code" in out
    assert "&gt; quoted" in out, "the quoted line must stay literal (escaped) code:\n" + out


def test_blockquoted_fence_renders_as_code() -> None:
    """A fence nested inside a blockquote (``> ```` ``) must still render as a
    code block WITHIN the ``<blockquote>``.  Anchoring the fence open to line
    start means it is not matched at column > 0, so the blockquote pass
    extracts the ``> `` run and its recursive render handles the fence.  (Pins
    that we did not over-correct by simply hoisting the fence pass — which
    would have swallowed the blockquoted fence as ``undefined``.)"""
    out = _render("> ```\n> code\n> ```")
    assert "<blockquote>" in out
    assert "<pre><code>code</code></pre>" in out, "blockquoted fence lost its code:\n" + out
    assert "undefined" not in out
    assert _NUL not in out


def test_indented_fence_still_renders_as_code() -> None:
    """The open anchor allows arbitrary leading indent, so a legitimately
    indented fence (e.g. under a list item) still renders as code rather than a
    paragraph of literal backticks.  (A bare ``^`` anchor would drop it; the
    deeper 4-space-indent case is pinned separately.)"""
    out = _render("  ```py\n  x = 1\n  ```")
    assert "<pre><code" in out, "indented fence dropped (not rendered as code):\n" + out
    assert "x = 1" in out


def test_indented_fence_close_leaves_no_trailing_whitespace_line() -> None:
    """An indented closing line's leading spaces must NOT survive as a trailing
    whitespace-only line inside the code block: the content strip removes a
    trailing newline PLUS any indent the close dragged into the capture (a
    `` ``` `` closed at column 0 is unaffected).  Copilot review, PR #804."""
    out = _render("  ```py\n  x = 1\n  ```")
    m = re.search(r"<code[^>]*>(.*?)</code>", out, re.S)
    assert m, "no <code> block:\n" + out
    assert m.group(1) == "  x = 1", "indented fence close left a trailing whitespace line: " + repr(
        m.group(1)
    )


# ---------------------------------------------------------------------------
# Fix 4 — <details> open anchored to line start (B5). The details pass ran
# with an unanchored open, so a `<details>` mentioned mid-line inside inline
# code matched across the backtick spans and swallowed the DT sentinel /
# lost the content between them.
# ---------------------------------------------------------------------------


def test_inline_code_details_tag_not_consumed_by_details_pass() -> None:
    """B5: ``Use `<details>` then `</details>` to fold`` must render two
    inline-code spans of the literal tags — NOT a real <details> element with
    the text between the spans swallowed."""
    out = _render("Use `<details>` then `</details>` to fold.")
    assert "&lt;details&gt;" in out, "opening <details> tag not shown as literal code:\n" + out
    assert "&lt;/details&gt;" in out, "closing </details> tag not shown as literal code:\n" + out
    assert "<details>" not in out, "a real <details> element was wrongly created:\n" + out
    assert out.count("<code>") == 2, "expected two inline-code spans:\n" + out


def test_block_details_still_renders() -> None:
    """No-regression: a genuine multi-line <details> block (at line start)
    still renders as a real disclosure element."""
    out = _render("<details>\n<summary>More</summary>\n\nBody text here.\n\n</details>")
    assert "<details><summary>More</summary>" in out
    assert "Body text here." in out


def test_oneline_details_still_renders() -> None:
    """No-regression: the common one-line form must survive the open anchor
    (anchoring the CLOSE too would break this — do not)."""
    out = _render("<details><summary>x</summary>y</details>")
    assert "<details><summary>x</summary>" in out
    assert "y" in out and out.rstrip().endswith("</details>")


def test_details_inside_fence_stays_literal() -> None:
    """Lock the behavior Fix 5a must preserve: a <details> shown INSIDE a code
    fence is masked by the (earlier) fence pass and must stay literal escaped
    code, never extracted into a real element."""
    out = _render("```html\n<details><summary>s</summary>x</details>\n```")
    assert "<pre><code" in out
    assert "&lt;details&gt;" in out, "details-in-fence should be literal code:\n" + out
    assert "<details>" not in out, "details inside a fence was wrongly extracted:\n" + out


# ---------------------------------------------------------------------------
# Fix 5 (NEW-1) — recursive-frame content loss.  renderMarkdown recurses for
# <details> bodies and footnote definitions.  When those bodies were extracted
# AFTER the fence/inline-code/math passes, they carried outer-scope sentinels
# that the recursive call — with fresh, empty block arrays — could not resolve,
# so a code block / inline code / math inside them rendered as `undefined` (or,
# after the Fix 2 floor, an inert `CB0`/`IC0` sentinel) — silent content loss.
# The structural fix extracts <details> from RAW markdown (before fence/inline
# protection, fence-aware) and collects footnote definitions before the inline
# passes, so each recursion sees raw content.
# ---------------------------------------------------------------------------


def test_code_block_in_details_renders_code() -> None:
    """NEW-1 (a), the headline case: a fenced code block inside <details> must
    render the CODE, not `undefined` and not an inert `CB0` sentinel."""
    out = _render("<details>\n<summary>x</summary>\n\n```py\nsecret_code()\n```\n\n</details>")
    assert "secret_code()" in out, "code inside <details> was lost:\n" + out
    assert "<pre><code" in out and 'class="language-py"' in out
    assert "undefined" not in out
    assert _NUL not in out, "a raw sentinel leaked (recursion did not see raw markdown):\n" + out


def test_blockquote_in_details_renders() -> None:
    """NEW-1 generalises to any recursive block: a blockquote inside <details>
    must render as a real <blockquote>, not a lost/inert sentinel."""
    out = _render("<details>\n<summary>x</summary>\n\n> quoted\n\n</details>")
    assert "<blockquote>" in out, "blockquote inside <details> was lost:\n" + out
    assert "quoted" in out
    assert _NUL not in out


def test_inline_code_in_footnote_renders() -> None:
    """NEW-1 (b): inline code in a footnote definition must render as a real
    <code> span in the footnote section, not `undefined`/`IC0`."""
    out = _render("See[^1].\n\n[^1]: uses `code` here")
    assert "<code>code</code>" in out, "inline code in footnote def was lost:\n" + out
    assert "undefined" not in out
    assert _NUL not in out


def test_math_in_footnote_renders() -> None:
    r"""NEW-1 (b), math variant: display/inline math in a footnote definition
    must reach KaTeX, not restore to `undefined`/`MB0`."""
    out = _render("See[^1].\n\n[^1]: with \\(x^2\\) inline")
    assert '<span class="katex">' in out, "math in footnote def was lost:\n" + out
    assert "undefined" not in out
    assert _NUL not in out


# ---------------------------------------------------------------------------
# Review round-1 regression pins: the details pass runs AFTER fence protection
# (fence-masking, not offset math, provides fence-awareness), and both the
# fence and details opens allow arbitrary leading indent.
# ---------------------------------------------------------------------------


def test_details_close_tag_shown_in_fenced_example_does_not_close_block() -> None:
    """A `</details>` shown as example code inside a fence must NOT close the
    real disclosure early.  Because the fence pass runs first and masks the
    example as a sentinel, the details close matches only the real trailing
    tag; the fenced example renders as literal code inside the block."""
    md = "<details>\n<summary>s</summary>\n\n```html\n</details>\n```\n\n</details>"
    out = _render(md)
    assert '<pre><code class="language-html">' in out, "fenced example was swallowed:\n" + out
    assert "&lt;/details&gt;" in out, "example </details> should be literal code:\n" + out
    assert out.strip().startswith("<details><summary>s</summary>"), out
    assert out.rstrip().endswith("</details>"), "real block closed early / stray text:\n" + out
    assert _NUL not in out


def test_deeply_indented_fence_renders_as_code() -> None:
    """A fence indented 4+ spaces (as when nested under a list item) still
    tokenises as a code block — the open anchor allows arbitrary indent, so we
    don't regress deeply-nested code samples to literal backticks."""
    out = _render("    ```py\n    x = 1\n    ```")
    assert "<pre><code" in out, "deeply-indented fence dropped:\n" + out
    assert "x = 1" in out


def test_fence_on_list_marker_line_renders_as_code() -> None:
    """A code fence that OPENS on the same line as a list marker (`- ```py`)
    still tokenises as a code block inside the list item.  The open matches
    after an optional list marker, which is re-emitted before the sentinel so
    the list pass still sees the item.  Regression guard: a bare `^[ \\t]*`
    anchor (no list-marker allowance) destroyed the block and leaked the raw
    backticks + language tag as text."""
    for src in ["- ```py\n  print(1)\n  ```", "1. ```py\n   print(1)\n   ```"]:
        out = _render(src)
        assert "<pre><code" in out, "list-marker-line fence dropped:\n" + repr(src) + "\n" + out
        assert "print(1)" in out
        assert "```py" not in out, "raw fence backticks leaked as text:\n" + out
        assert "<li>" in out, "list structure lost:\n" + out


def test_nested_list_fence_stays_nested() -> None:
    """A fenced code block as a NESTED sub-item keeps its nesting level: the
    fence pass re-emits the leading indent before the sentinel, so the list
    pass still reads the sub-item's indentation.  Regression guard: dropping
    the indent flattened the code block to a top-level sibling of the parent."""
    out = _render("- parent\n  - ```py\n    code\n    ```")
    assert "parent" in out
    assert "<pre><code" in out and "```py" not in out
    assert out.count("<ul>") == 2, "nested list fence flattened to a sibling:\n" + out


def test_big_ordered_marker_fence_is_protected() -> None:
    r"""A fence opening on a 10+ digit ordered-list marker line is still
    protected — the marker alternation uses ``\d+``, matching the list pass,
    not a capped ``\d{1,9}`` that would leave the fence unprotected."""
    out = _render("1234567890. ```py\ncode\n```")
    assert "<pre><code" in out, "big ordered-marker fence leaked as text:\n" + out
    assert "```py" not in out


def test_fenced_block_in_footnote_renders_in_footnote() -> None:
    """A fenced code block continuing a footnote definition renders INSIDE the
    footnote section (the fence pass re-emits the 2-space indent the
    continuation scan needs; the restore round-trip then resolves it there)."""
    out = _render("See[^1].\n\n[^1]: note\n  ```py\n  x=1\n  ```")
    assert 'class="footnotes"' in out
    assert out.find("<pre") > out.find('class="footnotes"'), (
        "fenced code in a footnote rendered outside the footnote section:\n" + out
    )
    assert "x=1" in out


def test_indented_details_is_extracted() -> None:
    """An indented `<details>` (e.g. under a list item) is still extracted into
    a real disclosure element — the open anchor allows leading whitespace,
    while a mid-line `<details>` inside inline code still is not (B5)."""
    out = _render("  <details><summary>x</summary>y</details>")
    assert "<details><summary>x</summary>" in out, "indented <details> not extracted:\n" + out
    assert "y" in out
