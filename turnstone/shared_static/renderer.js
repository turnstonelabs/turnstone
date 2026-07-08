// renderer.js — Markdown + LaTeX rendering (no external deps except KaTeX)
//
// ES module (imports utils; vendor hljs/katex/mermaid stay lazy typeof-guarded
// globals).  Window bridge at the bottom for the still-classic consumers.

import { escapeHtml } from "./utils.js";

// ---------------------------------------------------------------------------
//  Inline formatting
// ---------------------------------------------------------------------------
// Safe inline HTML tags allowed through escapeHtml (no attributes — XSS safe)
// Block-level tags (details, summary, hr) handled by their own protection passes
var _SAFE_TAGS =
  /&lt;(\/?(?:br|kbd|mark|sub|sup|ins|wbr|abbr|small|u|s))(?:\s*\/?)&gt;/gi;

export function inlineMarkdown(text) {
  // Escape HTML first so only tags we generate are real
  text = escapeHtml(text);
  // Restore safe HTML tags (attribute-free only — already escaped so no XSS)
  text = text.replace(_SAFE_TAGS, function (m, tag) {
    return "<" + tag + ">";
  });
  // Bold (asterisks only — underscores cause false positives on snake_case)
  text = text.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
  // Italic (asterisks only)
  text = text.replace(
    /(?<!\*)\*([^\s*](?:.*?[^\s*])?)\*(?!\*)/g,
    "<em>$1</em>",
  );
  // Strikethrough
  text = text.replace(/~~(.+?)~~/g, "<del>$1</del>");
  // Images (must come before links — render as click-to-load placeholder).
  // Captured groups inherit inlineMarkdown's leading escapeHtml(text) —
  // they are already entity-encoded by the time this regex runs. Don't
  // re-escape them: double-encoding turns `&` into `&amp;amp;`, which
  // breaks query-string URLs after browser parse + getAttribute. The
  // `safe*` rename signals the pre-escape invariant; the attribute-
  // context lint in tests/test_renderer_js.py enforces that future
  // attribute-context concat sites maintain the convention or call
  // escapeHtml explicitly.
  text = text.replace(
    /!\[([^\]]*)\]\(([^)]+)\)/g,
    function (m, safeAlt, safeUrl) {
      if (!/^\s*(https?:\/\/|data:image\/)/i.test(safeUrl)) return m;
      if (!safeAlt) safeAlt = "Image";
      var safeDomain;
      try {
        // hostname is freshly extracted, not from the regex capture,
        // so it does NOT carry the upstream escape — escape locally.
        safeDomain = escapeHtml(new URL(safeUrl).hostname);
      } catch (e) {
        safeDomain = safeUrl.length > 40 ? safeUrl.slice(0, 40) + "…" : safeUrl;
      }
      return (
        '<span class="img-placeholder" tabindex="0" role="button" ' +
        'aria-label="Load image: ' +
        safeAlt +
        '" ' +
        'data-src="' +
        safeUrl +
        '" data-alt="' +
        safeAlt +
        '">' +
        '<span class="img-placeholder-icon">&#x1F5BC;</span> ' +
        '<span class="img-placeholder-label">' +
        safeAlt +
        "</span>" +
        '<span class="img-placeholder-domain">' +
        safeDomain +
        "</span>" +
        "</span>"
      );
    },
  );
  // Links — same upstream-escape invariant as images. `safeLabel` and
  // `safeUrl` are entity-encoded via inlineMarkdown's leading pass.
  text = text.replace(
    /\[([^\]]+)\]\(([^)]+)\)/g,
    function (m, safeLabel, safeUrl) {
      if (!/^\s*(https?:\/\/|\/(?!\/))/i.test(safeUrl)) return m;
      return (
        '<a href="' +
        safeUrl +
        '" target="_blank" rel="noopener noreferrer">' +
        safeLabel +
        "</a>"
      );
    },
  );
  // Footnote references [^id] — after links (link regex requires (url), so no conflict)
  text = text.replace(/\[\^([^\]]+)\]/g, function (m, fnId) {
    var safeFnId = escapeHtml(fnId);
    return (
      '<sup class="fn-ref" id="fn-' +
      _fnScopeId +
      "-ref-" +
      safeFnId +
      '">' +
      '<a href="#fn-' +
      _fnScopeId +
      "-def-" +
      safeFnId +
      '" role="doc-noteref">[' +
      safeFnId +
      "]</a></sup>"
    );
  });
  return text;
}

// Attach click-to-load listener for image placeholders (delegated)
document.addEventListener("click", function (e) {
  var ph = e.target.closest(".img-placeholder");
  if (!ph) return;
  var raw = ph.getAttribute("data-src") || "";
  if (!/^(https?:\/\/|data:image\/)/i.test(raw)) return;
  var src;
  try {
    src = new URL(raw).href;
  } catch (_e) {
    return;
  }
  var img = document.createElement("img");
  img.src = src;
  img.alt = ph.getAttribute("data-alt");
  img.loading = "lazy";
  ph.replaceWith(img);
});
document.addEventListener("keydown", function (e) {
  if (e.key !== "Enter") return;
  var ph = e.target.closest(".img-placeholder");
  if (!ph) return;
  ph.click();
});

// Smooth-scroll footnote navigation (native fragment jumps don't work in scrollable containers)
document.addEventListener("click", function (e) {
  var a = e.target.closest(".fn-ref a, .fn-backref");
  if (!a) return;
  var href = a.getAttribute("href");
  if (!href || href[0] !== "#") return;
  var target = document.getElementById(href.slice(1));
  if (!target) return;
  e.preventDefault();
  target.scrollIntoView({ behavior: "smooth", block: "center" });
});

// ---------------------------------------------------------------------------
//  List rendering (nested + task lists)
// ---------------------------------------------------------------------------
function renderListBlock(items) {
  if (items.length === 0) return "";
  var minIndent = items[0].indent;
  for (var i = 1; i < items.length; i++) {
    if (items[i].indent < minIndent) minIndent = items[i].indent;
  }
  // Split into separate lists when marker type changes at top indent level
  var segments = [];
  var cur = [items[0]];
  for (var i = 1; i < items.length; i++) {
    if (items[i].indent <= minIndent && items[i].ordered !== cur[0].ordered) {
      segments.push(cur);
      cur = [items[i]];
    } else {
      cur.push(items[i]);
    }
  }
  segments.push(cur);
  if (segments.length > 1) {
    return segments.map(renderListBlock).join("\n");
  }
  var type = items[0].ordered ? "ol" : "ul";
  var html = "<" + type + ">";
  var i = 0;
  while (i < items.length) {
    var item = items[i];
    if (item.indent <= minIndent) {
      var content = item.content;
      // Task list checkboxes
      var taskMatch = content.match(/^\[([ xX])\]\s*(.*)/);
      if (taskMatch) {
        var checked = taskMatch[1] !== " ";
        content =
          '<input type="checkbox" disabled' +
          (checked ? " checked" : "") +
          ' aria-label="' +
          escapeHtml(taskMatch[2]) +
          '"> ' +
          inlineMarkdown(taskMatch[2]);
      } else {
        content = inlineMarkdown(content);
      }
      // Collect children (deeper indent items following this one)
      var children = [];
      var j = i + 1;
      while (j < items.length && items[j].indent > minIndent) {
        children.push(items[j]);
        j++;
      }
      if (children.length > 0) {
        html += "<li>" + content + renderListBlock(children) + "</li>";
      } else {
        html += "<li>" + content + "</li>";
      }
      i = j;
    } else {
      i++;
    }
  }
  html += "</" + type + ">";
  return html;
}

// ---------------------------------------------------------------------------
//  LaTeX rendering via KaTeX
// ---------------------------------------------------------------------------
function renderLatex(tex, displayMode) {
  if (typeof katex === "undefined") return escapeHtml(tex);
  try {
    return katex.renderToString(tex, {
      displayMode: displayMode,
      throwOnError: false,
      errorColor: "#f87171",
      output: "html",
    });
  } catch (e) {
    return '<code class="katex-error">' + escapeHtml(tex) + "</code>";
  }
}

// ---------------------------------------------------------------------------
//  Footnote scope — prevents ID collisions across multiple renderMarkdown calls
// ---------------------------------------------------------------------------
var _fnScopeId = 0;
var _fnDepth = 0;

// ---------------------------------------------------------------------------
//  GFM callout types (alerts)
// ---------------------------------------------------------------------------
var CALLOUT_TYPES = {
  NOTE: { icon: "\u2139", label: "Note" },
  TIP: { icon: "\u{1F4A1}", label: "Tip" },
  IMPORTANT: { icon: "\u2757", label: "Important" },
  WARNING: { icon: "\u26A0", label: "Warning" },
  CAUTION: { icon: "\u{1F6D1}", label: "Caution" },
};

// ---------------------------------------------------------------------------
//  Code fence language → CSS class normalization
// ---------------------------------------------------------------------------
var _LANG_ALIASES = { "c++": "cpp", "c#": "csharp", "f#": "fsharp" };

function _langToCssClass(lang) {
  if (!lang) return "";
  var lower = lang.toLowerCase();
  if (_LANG_ALIASES[lower]) return _LANG_ALIASES[lower];
  // Strip chars invalid in CSS class names (keep alphanumeric + hyphen)
  return lower.replace(/[^a-z0-9-]/g, "");
}

// ---------------------------------------------------------------------------
//  Main markdown renderer
// ---------------------------------------------------------------------------
// Hard cap on renderMarkdown re-entrancy.  Blockquote/callout/list bodies
// recurse through renderMarkdown; a pathological input (a few KB of nested
// "> " prefixes) would otherwise overflow the call stack mid-render — an
// exception the streaming callers can only partially recover from.  Beyond
// the cap the nested body renders as escaped plain text: degraded, visible.
var _MD_MAX_DEPTH = 100;

// U+0000 (NUL) only.  Structural sentinels are NUL-framed (chr(0)+tag+idx+
// chr(0)) and renderer.js is the sole NUL producer, so stripping NUL at the
// top-level entry closes every forgery path — while leaving every OTHER
// control byte intact, because a code fence must show pasted source verbatim
// (terminal output legitimately carries ESC/FF/VT/DEL, none of which can forge
// a sentinel).  Authored as the literal \x00 escape (only \uXXXX decodes to a
// raw byte in this toolchain), matching the file's \x00 sentinel convention.
var _NUL_STRIP_RE = /\x00/g;

export function renderMarkdown(text) {
  if (_fnDepth >= _MD_MAX_DEPTH) {
    return "<p>" + escapeHtml(String(text == null ? "" : text)) + "</p>";
  }
  // Scope footnote IDs per top-level render call (prevents collisions across
  // messages).  Depth accounting rides a try/finally: a throw anywhere in the
  // body used to strand _fnDepth elevated, freezing _fnScopeId so footnote
  // anchor ids collided across every later message.
  //
  // Strip caller-supplied NUL, but at the TOP-LEVEL call ONLY.  The renderer
  // frames structural blocks with in-band NUL sentinels (NUL+tag+index+NUL)
  // and escapeHtml preserves U+0000, so model/tool text carrying that shape
  // could forge a sentinel and duplicate/relocate a block or print "undefined"
  // (B1/B2/B3).  renderer.js is the sole NUL producer and every restore regex
  // is NUL-framed, so removing NUL here erases every forgery path — and ONLY
  // NUL, so a fenced code block still shows pasted control bytes (ESC/FF/VT/
  // DEL) verbatim.  Depth 0 only: recursive frames (blockquote/details/
  // footnote bodies) legitimately carry generated sentinels and an
  // unconditional strip would shred them.  NUL is never valid content.
  if (_fnDepth === 0) {
    text = String(text == null ? "" : text).replace(_NUL_STRIP_RE, "");
    _fnScopeId++;
  }
  _fnDepth++;
  try {
    return _renderMarkdownBody(text);
  } finally {
    _fnDepth--;
  }
}

// Restore-pass callback factory: one guarded closure per protected-block
// array.  Returns the matched sentinel `m` when the index is out of range (an
// inert placeholder the tokenizer strips the NUL from) instead of the array's
// `undefined`.  Factored so the ~12 restore passes share one implementation
// and can't drift (e.g. a callback reading the wrong array after a copy-paste).
function _restorer(arr) {
  return function (m, idx) {
    var v = arr[parseInt(idx)];
    return v === undefined ? m : v;
  };
}

function _renderMarkdownBody(text) {
  // Protect code blocks before the line-based passes (blockquote,
  // table) that would otherwise scoop a `> ` / `|` line out of a fenced body
  // and render it as real markdown nested in <pre><code> (B4: shell
  // transcripts, quoted email, markdown-about-markdown).  The open matches at
  // line start after optional indent (group 1) AND an optional list marker
  // (group 2: `- `, `1. `), so a fence opening on a list-item's marker line
  // (`- ```py`) still tokenises; both the indent and the marker are re-emitted
  // before the sentinel so the fence keeps its document position (see the
  // callback).  A blockquoted fence (`> ```) is NOT matched — `>` is not indent
  // or a list marker — so the blockquote pass below extracts that `> ` run and
  // its recursive render handles it.
  //
  // The opening-run length is captured and required on the close via
  // backreference so a 4-backtick outer fence wrapping a 3-backtick inner
  // (common when embedding markdown-about-markdown or lang-tagged snippets
  // inside another code block) is tokenised as one outer block with the inner
  // triple-backticks preserved verbatim — the prior `` ```...``` `` regex
  // treated the outer-open and inner-open as a single fence pair, stranding
  // the rest of the content with visible \x00CB{n}\x00 sentinels.
  //
  // Two constraints below close the gap that mid-stream buffers expose:
  //
  //   1. Content can't contain its own close pattern — `(?!\3)` (group 3 is
  //      the backtick run; groups 1-2 are the indent and optional list marker)
  //      inside the content quantifier blocks the lazy matcher from extending
  //      across another N-backtick run. Without this, a buffer like
  //      ```mermaid\n<partial>\n```python\n<partial>\n```  would extend
  //      mermaid's content all the way to the FINAL ```, swallowing python
  //      and handing mermaid a wrong (and incomplete-looking) source. With
  //      the lookahead, content stops at the first matching run and the open
  //      simply doesn't match anything until a true close arrives. Inner
  //      backticks of a SMALLER count (e.g. 3-backtick inner inside a
  //      4-backtick outer) still pass since `\3` is the OPEN count.
  //
  //   2. The close must live at a line boundary — `[ \t]*(?=\n|$)` after
  //      `\3` forbids the close from being immediately followed by a language
  //      tag, so ```python opening another fence can't masquerade as the
  //      previous fence's close.
  //
  // Together these mean an unclosed fence stays as plain markdown until its
  // true close arrives — no intermediate parse errors flash through mermaid /
  // hljs while a stream is in flight.
  // codeBlockRaw keeps each fence's RAW source next to its rendered HTML, so
  // the <details> pre-pass below can restore a fenced body to raw markdown for
  // its recursive render (NEW-1) rather than an unresolvable outer sentinel.
  // Populated only when the text contains a <details> tag — its sole reader —
  // so the common no-<details> render skips the per-fence slice.  (When there
  // is no <details>, the details .replace matches nothing and its CB->raw
  // restore never runs, so the empty array is never read.)
  var codeBlocks = [];
  var codeBlockRaw = [];
  var needFenceRaw = /<details>/i.test(text);
  text = text.replace(
    /^([ \t]*)((?:[-*+]|\d+[.)])[ \t]+)?(```+)([^\s`]*)\n((?:(?!\3)[\s\S])*?)\3[ \t]*(?=\n|$)/gm,
    function (m, indent, marker, _open, lang, code) {
      var cssLang = _langToCssClass(lang);
      codeBlocks.push(
        "<pre><code" +
          (cssLang ? ' class="language-' + escapeHtml(cssLang) + '"' : "") +
          ">" +
          // Strip the trailing newline plus any whitespace an indented close
          // dragged into the content (a `  ```` close would otherwise leave a
          // whitespace-only last line in the code block).
          escapeHtml(code.replace(/\n[ \t]*$/, "")) +
          "</code></pre>",
      );
      // Store the fence source WITHOUT the re-emitted indent/marker prefix so
      // the <details> CB->raw restore substitutes the fence alone (no double);
      // only when a <details> is present to read it (kept index-aligned with
      // codeBlocks because every fence pushes to both in that case).
      if (needFenceRaw) {
        codeBlockRaw.push(
          m.slice(indent.length + (marker ? marker.length : 0)),
        );
      }
      // Re-emit the leading indent AND any list marker so the fence keeps its
      // place in the document: a nested list item (`  - ```py`) stays nested,
      // `- ```py` stays a list item, and a fence continuing a footnote def
      // keeps the 2-space indent the continuation scan needs.  The CB <p>-unwrap
      // (restore pass) tolerates that preserved indent, so an own-line indented
      // fence still leaves no stray <p> (NEW-3).  A blockquoted fence (`> ```)
      // stays excluded: `>` is neither indent nor a list marker.
      return (
        indent + (marker || "") + "\x00CB" + (codeBlocks.length - 1) + "\x00"
      );
    },
  );

  // Extract <details> blocks and recursively render — AFTER fence protection,
  // BEFORE the blockquote/inline/math passes.  Running after fence means a
  // fenced code block inside <details> is already a \x00CB\x00 sentinel; it is
  // restored to its RAW source (codeBlockRaw) before the recursive
  // renderMarkdown, so the code renders in-frame instead of restoring to
  // `undefined` / an inert sentinel against the recursion's empty arrays
  // (NEW-1, silent content loss).  Fence-first also masks a `</details>` shown
  // as example code (so it can't close the block early) and a whole <details>
  // shown inside a fence (so it stays literal) — no separate fence-awareness
  // needed.  The open is anchored to line start (`^[ \t]*`: allows indentation,
  // e.g. under a list, but not a mid-line `<details>` inside inline code — B5);
  // the close stays unanchored so the one-line
  // <details><summary>x</summary>y</details> form still matches.
  var detailsBlocks = [];
  text = text.replace(
    /^[ \t]*<details>\s*\n?([\s\S]*?)<\/details>/gim,
    function (m, inner) {
      // Restore any fenced bodies to raw markdown so the recursion re-renders
      // them in its own frame (the outer codeBlocks entry is then unused).
      inner = inner.replace(/\x00CB(\d+)\x00/g, _restorer(codeBlockRaw));
      var sumMatch = inner.match(
        /^\s*<summary>([\s\S]*?)<\/summary>\s*\n?([\s\S]*)/i,
      );
      var html;
      if (sumMatch) {
        html =
          "<details><summary>" +
          inlineMarkdown(sumMatch[1].trim()) +
          "</summary>" +
          renderMarkdown(sumMatch[2]) +
          "</details>";
      } else {
        html = "<details>" + renderMarkdown(inner) + "</details>";
      }
      detailsBlocks.push(html);
      return "\x00DT" + (detailsBlocks.length - 1) + "\x00";
    },
  );

  // Pre-pass: extract blockquote blocks and recursively render.  Runs after
  // fence protection (a fenced `> ` line is already masked as a \x00CB\x00
  // sentinel, so it is not scooped here — B4) but before the remaining
  // code/math protection, so the recursive call still processes raw markdown,
  // not text with outer-scope placeholders.
  var bqBlocks = [];
  (function () {
    var blines = text.split("\n");
    var result = [];
    var i = 0;
    while (i < blines.length) {
      if (blines[i].startsWith("> ") || blines[i] === ">") {
        var inner = [];
        while (
          i < blines.length &&
          (blines[i].startsWith("> ") || blines[i] === ">")
        ) {
          inner.push(blines[i] === ">" ? "" : blines[i].slice(2));
          i++;
        }
        // Check for GFM alert/callout syntax: > [!NOTE], > [!TIP], etc.
        var alertMatch =
          inner.length > 0 &&
          inner[0].match(/^\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\]\s*$/i);
        if (alertMatch) {
          var alertType = alertMatch[1].toUpperCase();
          var info = CALLOUT_TYPES[alertType];
          var bodyLines = inner.slice(1);
          if (bodyLines.length > 0 && bodyLines[0].trim() === "") {
            bodyLines = bodyLines.slice(1);
          }
          var bodyHtml =
            bodyLines.length > 0 ? renderMarkdown(bodyLines.join("\n")) : "";
          bqBlocks.push(
            '<div class="callout callout-' +
              alertType.toLowerCase() +
              '" role="note" aria-label="' +
              info.label +
              '">' +
              '<div class="callout-title">' +
              '<span class="callout-icon" aria-hidden="true">' +
              info.icon +
              "</span> " +
              '<span class="callout-label">' +
              info.label +
              "</span>" +
              "</div>" +
              (bodyHtml
                ? '<div class="callout-body">' + bodyHtml + "</div>"
                : "") +
              "</div>",
          );
        } else {
          bqBlocks.push(
            "<blockquote>" + renderMarkdown(inner.join("\n")) + "</blockquote>",
          );
        }
        result.push("\x00BQ" + (bqBlocks.length - 1) + "\x00");
      } else {
        result.push(blines[i]);
        i++;
      }
    }
    text = result.join("\n");
  })();

  // Protect inline code FIRST so backtick spans containing math
  // delimiters (e.g. `` `$$x$$` `` or `` `\[x\]` ``) stay literal.
  // Display math used to run first, but that lets the math regex
  // consume delimiters inside backticks and replace them with
  // \x00MB…\x00 sentinels — sentinels then captured by the inline
  // code grab end up restored INSIDE the <code>, leaking the
  // null-byte placeholder into rendered output. Code first means
  // backticks seal their content before any math regex sees it.
  // The reverse edge case (math containing backticks, e.g.
  // ``$$ \verb|`x`| $$``) is much rarer and KaTeX would reject
  // the verbatim syntax anyway.
  var inlineCodes = [];
  text = text.replace(/`([^`\n]+)`/g, function (m, code) {
    inlineCodes.push("<code>" + escapeHtml(code) + "</code>");
    return "\x00IC" + (inlineCodes.length - 1) + "\x00";
  });

  // Protect display math — both TeX ($$...$$) and LaTeX (\[...\])
  // delimiter styles. Most models emit one or the other depending
  // on system-prompt style; GPT-5 / o-series and Claude with
  // reasoning effort tend to emit the LaTeX form. Without both,
  // math nested in a markdown paragraph silently passes through
  // as raw \[...\] text.
  var mathBlocks = [];
  text = text.replace(/\$\$([\s\S]+?)\$\$/g, function (m, tex) {
    mathBlocks.push(renderLatex(tex.trim(), true));
    return "\x00MB" + (mathBlocks.length - 1) + "\x00";
  });
  text = text.replace(/\\\[([\s\S]+?)\\\]/g, function (m, tex) {
    mathBlocks.push(renderLatex(tex.trim(), true));
    return "\x00MB" + (mathBlocks.length - 1) + "\x00";
  });

  // Protect inline math — LaTeX-style \(...\) only.
  //
  // Single-$ delimiters were dropped because $ is overloaded in
  // prose: currency ("$5 and $10"), env vars ("$HOME and $PATH"),
  // and shell prompts all produced false-positive math spans.
  // \(...\) is unambiguous and is what GPT-5 / o-series / Claude
  // with reasoning effort emit by default anyway. Display math
  // ($$...$$ and \[...\]) is unchanged; it has enough delimiter
  // mass that ambiguity is not a practical problem.
  //
  // Newlines inside the captured group are forbidden: an
  // unterminated \(...\) on one line would otherwise eat the next
  // paragraph until it found a closing \), which is jarring on
  // streaming markdown where the closer hasn't arrived yet.
  var inlineMaths = [];
  text = text.replace(/\\\(([^\n]+?)\\\)/g, function (m, tex) {
    inlineMaths.push(renderLatex(tex.trim(), false));
    return "\x00IM" + (inlineMaths.length - 1) + "\x00";
  });

  // Protect markdown tables (extract before line-by-line processing)
  var tableBlocks = [];
  (function () {
    var tlines = text.split("\n");
    var sepRe = /^\|?(\s*:?-{1,}:?\s*\|)+\s*:?-{1,}:?\s*\|?\s*$/;
    var result = [];
    var i = 0;
    while (i < tlines.length) {
      if (
        i + 1 < tlines.length &&
        tlines[i].includes("|") &&
        sepRe.test(tlines[i + 1])
      ) {
        var headerLine = tlines[i];
        var sepLine = tlines[i + 1];
        var sepCells = sepLine
          .replace(/^\|/, "")
          .replace(/\|?\s*$/, "")
          .split("|");
        var aligns = sepCells.map(function (c) {
          c = c.trim();
          if (c.startsWith(":") && c.endsWith(":")) return "center";
          if (c.endsWith(":")) return "right";
          return "left";
        });
        var hdrCells = headerLine
          .replace(/^\|/, "")
          .replace(/\|?\s*$/, "")
          .split("|")
          .map(function (c) {
            return c.trim();
          });
        var dataRows = [];
        var j = i + 2;
        while (
          j < tlines.length &&
          tlines[j].includes("|") &&
          tlines[j].trim() !== ""
        ) {
          var row = tlines[j]
            .replace(/^\|/, "")
            .replace(/\|?\s*$/, "")
            .split("|")
            .map(function (c) {
              return c.trim();
            });
          dataRows.push(row);
          j++;
        }
        var html =
          '<div class="table-wrap" tabindex="0" role="region" aria-label="Data table"><table>';
        html += "<thead><tr>";
        for (var k = 0; k < hdrCells.length; k++) {
          var align = aligns[k] || "left";
          html +=
            '<th scope="col" class="align-' +
            align +
            '">' +
            inlineMarkdown(hdrCells[k]) +
            "</th>";
        }
        html += "</tr></thead><tbody>";
        for (var r = 0; r < dataRows.length; r++) {
          html += "<tr>";
          for (var k = 0; k < hdrCells.length; k++) {
            var align = aligns[k] || "left";
            var cell = dataRows[r][k] || "";
            html +=
              '<td class="align-' +
              align +
              '">' +
              inlineMarkdown(cell) +
              "</td>";
          }
          html += "</tr>";
        }
        html += "</tbody></table></div>";
        tableBlocks.push(html);
        result.push("\x00TB" + (tableBlocks.length - 1) + "\x00");
        i = j;
      } else {
        result.push(tlines[i]);
        i++;
      }
    }
    text = result.join("\n");
  })();

  // Collect footnote definitions ([^id]: content)
  var footnoteDefs = {};
  (function () {
    var flines = text.split("\n");
    var result = [];
    var i = 0;
    while (i < flines.length) {
      var fnm = flines[i].match(/^\[\^([^\]]+)\]:\s*(.*)/);
      if (fnm) {
        var fnId = fnm[1];
        var fnContent = fnm[2];
        // Collect continuation lines (indented by 2+ spaces)
        var j = i + 1;
        while (j < flines.length && /^ {2}/.test(flines[j])) {
          fnContent += "\n" + flines[j].slice(2);
          j++;
        }
        footnoteDefs[fnId] = fnContent;
        i = j;
      } else {
        result.push(flines[i]);
        i++;
      }
    }
    text = result.join("\n");
  })();

  // Process block-level elements per line
  var lines = text.split("\n");
  var out = [];

  for (var i = 0; i < lines.length; i++) {
    var line = lines[i];

    // Horizontal rule
    if (/^(\*{3,}|-{3,}|_{3,})\s*$/.test(line)) {
      out.push("<hr>");
      continue;
    }

    // Headers
    var hm = line.match(/^(#{1,6})\s+(.+)/);
    if (hm) {
      var level = hm[1].length;
      out.push(
        "<h" + level + ">" + inlineMarkdown(hm[2]) + "</h" + level + ">",
      );
      continue;
    }

    // Lists — collect consecutive list lines, then render with nesting
    var ulm = line.match(/^(\s*)[-*+]\s+(.*)/);
    var olm = !ulm ? line.match(/^(\s*)\d+[.)]\s+(.*)/) : null;
    if (ulm || olm) {
      var listItems = [];
      while (i < lines.length) {
        var um = lines[i].match(/^(\s*)[-*+]\s+(.*)/);
        var om = !um ? lines[i].match(/^(\s*)\d+[.)]\s+(.*)/) : null;
        if (um || om) {
          var lm = um || om;
          listItems.push({
            indent: lm[1].length,
            ordered: !!om,
            content: lm[2],
          });
          i++;
        } else {
          break;
        }
      }
      i--; // for-loop will increment
      out.push(renderListBlock(listItems));
      continue;
    }

    // Definition list — term followed by `: definition` lines
    if (
      line.trim() !== "" &&
      i + 1 < lines.length &&
      /^:\s+/.test(lines[i + 1])
    ) {
      var dlItems = [];
      while (i < lines.length) {
        if (lines[i].trim() === "" || /^:\s+/.test(lines[i])) break;
        var dlTerm = lines[i];
        var dlDefs = [];
        i++;
        while (i < lines.length && /^:\s+/.test(lines[i])) {
          dlDefs.push(lines[i].match(/^:\s+(.*)/)[1]);
          i++;
        }
        if (dlDefs.length > 0) {
          dlItems.push({ term: dlTerm, defs: dlDefs });
        } else {
          break;
        }
        // Skip blank lines between entries
        while (i < lines.length && lines[i].trim() === "") i++;
        // Check if another term+definition follows
        if (
          i < lines.length &&
          lines[i].trim() !== "" &&
          !/^:\s+/.test(lines[i]) &&
          i + 1 < lines.length &&
          /^:\s+/.test(lines[i + 1])
        ) {
          continue;
        }
        break;
      }
      if (dlItems.length > 0) {
        var dlHtml = "<dl>";
        for (var di = 0; di < dlItems.length; di++) {
          dlHtml += "<dt>" + inlineMarkdown(dlItems[di].term) + "</dt>";
          for (var dd = 0; dd < dlItems[di].defs.length; dd++) {
            dlHtml += "<dd>" + inlineMarkdown(dlItems[di].defs[dd]) + "</dd>";
          }
        }
        dlHtml += "</dl>";
        out.push(dlHtml);
        i--; // for-loop will increment
        continue;
      }
    }

    // Paragraph / plain text
    if (line.trim() === "") {
      out.push("");
    } else {
      out.push("<p>" + inlineMarkdown(line) + "</p>");
    }
  }

  var result = out.join("\n");

  // Append footnote section if any definitions were collected.
  //
  // Each definition body is rendered by a recursive renderMarkdown call.  The
  // body was collected AFTER the inline-code/math passes, so it may carry
  // outer-scope sentinels (e.g. `code` in a footnote -> a \x00IC\x00 sentinel).
  // The recursion can't resolve those against its own fresh, empty arrays, but
  // the restore guard (Fix 2) leaves the sentinel intact instead of emitting
  // "undefined"; because this section is appended to `result` BEFORE the
  // restore passes below — whose inlineCodes/mathBlocks are still populated —
  // the OUTER restore resolves it, so inline code / math in a footnote renders
  // correctly.  A FENCED block continuing a footnote definition works the same
  // way: the fence pass re-emits its 2-space indent before the sentinel, so
  // the continuation scan still collects it and the round-trip restores the
  // code inside the footnote item.
  var fnKeys = Object.keys(footnoteDefs);
  if (fnKeys.length > 0) {
    var fnHtml =
      '<section class="footnotes" role="doc-endnotes">' +
      '<hr class="footnotes-sep"><ol class="footnotes-list">';
    for (var fi = 0; fi < fnKeys.length; fi++) {
      var fid = fnKeys[fi];
      var safeFid = escapeHtml(fid);
      fnHtml +=
        '<li class="footnote-item" id="fn-' +
        _fnScopeId +
        "-def-" +
        safeFid +
        '">' +
        renderMarkdown(footnoteDefs[fid]) +
        ' <a href="#fn-' +
        _fnScopeId +
        "-ref-" +
        safeFid +
        '" class="fn-backref" ' +
        'role="doc-backlink" aria-label="Back to reference">\u21A9</a></li>';
    }
    fnHtml += "</ol></section>";
    result += fnHtml;
  }

  // Restore protected blocks through the _restorer factory (out-of-range
  // indices — reachable only via a recursive frame whose fresh array can't
  // resolve an outer-scope sentinel, the NEW-1 residual — leave the inert
  // sentinel rather than the literal "undefined").  Each block type unwraps a
  // `<p>SENTINEL</p>` paragraph first (the line pass wraps a lone sentinel in
  // <p>) so the browser doesn't split a stray empty <p> off the block; the
  // bare form follows.  The CB unwrap also tolerates surrounding whitespace
  // (`<p>  \x00CB0\x00</p>`) because the fence pass re-emits an own-line
  // fence's leading indent before the sentinel — the other block types emit
  // their sentinel at column 0, so they don't need it.
  result = result.replace(
    /<p>[ \t]*\x00CB(\d+)\x00[ \t]*<\/p>/g,
    _restorer(codeBlocks),
  );
  result = result.replace(/\x00CB(\d+)\x00/g, _restorer(codeBlocks));
  result = result.replace(/<p>\x00DT(\d+)\x00<\/p>/g, _restorer(detailsBlocks));
  result = result.replace(/\x00DT(\d+)\x00/g, _restorer(detailsBlocks));
  result = result.replace(/<p>\x00BQ(\d+)\x00<\/p>/g, _restorer(bqBlocks));
  result = result.replace(/\x00BQ(\d+)\x00/g, _restorer(bqBlocks));
  result = result.replace(/<p>\x00MB(\d+)\x00<\/p>/g, _restorer(mathBlocks));
  result = result.replace(/\x00MB(\d+)\x00/g, _restorer(mathBlocks));
  result = result.replace(/<p>\x00TB(\d+)\x00<\/p>/g, _restorer(tableBlocks));
  result = result.replace(/\x00TB(\d+)\x00/g, _restorer(tableBlocks));
  result = result.replace(/\x00IC(\d+)\x00/g, _restorer(inlineCodes));
  result = result.replace(/\x00IM(\d+)\x00/g, _restorer(inlineMaths));

  return result;
}

// ---------------------------------------------------------------------------
//  Post-render hook — syntax highlighting via highlight.js
// ---------------------------------------------------------------------------
var _NO_HIGHLIGHT_LANGS = {
  ascii: true,
  text: true,
  plaintext: true,
  plain: true,
  nohighlight: true,
  mermaid: true,
  plantuml: true,
};
var _TERMINAL_LANGS = {
  bash: true,
  shell: true,
  sh: true,
  zsh: true,
  console: true,
  terminal: true,
};
var _hljsConfigured = false;

// Source-keyed highlight cache. Mirrors _mermaidSvgCache: streamingRender
// replaces innerHTML wholesale on every rAF tick, so the <code> elements
// inside come up FRESH each tick — they don't carry the hljs class, and
// nothing on them carries forward. Without a cache, running hljs per
// tick would re-tokenize every code block every paint cycle on long
// streamed responses with many fences. With the cache, identical
// (language, source) pairs reuse the highlighted innerHTML synchronously.
//
// Cache miss runs hljs.highlightElement(el) (which mutates the element
// in place: replaces its innerHTML with highlighted span markup and
// adds the hljs class) and stores the resulting markup. Cache hit
// assigns that stored markup to el.innerHTML and re-adds the hljs
// class manually — semantically equivalent to a fresh highlightElement
// call without paying for re-tokenization.
//
// The cached value is the structured span markup that hljs itself
// produced from already-escaped text content, so re-assigning it to
// innerHTML doesn't widen the XSS surface beyond what hljs.highlight
// Element already does.
//
// FIFO-bounded so a long session with many distinct code blocks can't
// grow unbounded.
var _hljsCache = new Map();
var _HLJS_CACHE_MAX = 64;

// Shared FIFO eviction helper for the source-keyed caches in this
// file (_hljsCache, _mermaidSvgCache, _mermaidErrorCache, plus the
// raw→normalized mermaid memo). Only evicts the oldest when inserting
// a NEW key — overwriting an existing key is an in-place update and
// must not pay the eviction cost (which would drop an unrelated
// cached entry). The `cache_overwrite_does_not_evict` tests pin this
// invariant per cache.
function _cacheFifoEntry(cache, key, value, max) {
  if (!cache.has(key) && cache.size >= max) {
    var firstKey = cache.keys().next().value;
    cache.delete(firstKey);
  }
  cache.set(key, value);
}

function _applyCachedHljs(el, cachedHtml) {
  el.innerHTML = cachedHtml;
  el.classList.add("hljs");
}

export function postRenderHljs(containerEl) {
  if (typeof hljs === "undefined") return;
  if (!_hljsConfigured) {
    hljs.configure({ ignoreUnescapedHTML: true });
    _hljsConfigured = true;
  }
  var codeEls = containerEl.querySelectorAll("pre code[class*='language-']");
  for (var i = 0; i < codeEls.length; i++) {
    var el = codeEls[i];
    // Already-highlighted element (e.g. postRenderMarkdown called twice
    // on the same DOM with no intervening innerHTML replace). The
    // streaming path replaces innerHTML wholesale per tick, so this
    // guard primarily protects the non-streaming render path.
    if (el.classList.contains("hljs")) continue;
    // Extract language name from class
    var langClass = "";
    for (var j = 0; j < el.classList.length; j++) {
      if (el.classList[j].startsWith("language-")) {
        langClass = el.classList[j].substring(9);
        break;
      }
    }
    // Skip plaintext variants
    if (_NO_HIGHLIGHT_LANGS[langClass]) {
      el.classList.add("nohighlight");
      continue;
    }
    // Cache key: language + separator + source. ":" isn't part of a
    // language identifier so the prefix is unambiguous across keys.
    var source = el.textContent;
    var cacheKey = langClass + ":" + source;
    if (_hljsCache.has(cacheKey)) {
      _applyCachedHljs(el, _hljsCache.get(cacheKey));
    } else {
      hljs.highlightElement(el);
      _cacheFifoEntry(_hljsCache, cacheKey, el.innerHTML, _HLJS_CACHE_MAX);
    }
    // Add terminal styling class for shell languages
    if (_TERMINAL_LANGS[langClass]) {
      var pre = el.closest("pre");
      if (pre) pre.classList.add("code-terminal");
    }
  }
}

export function postRenderMarkdown(containerEl) {
  postRenderHljs(containerEl);
  // Render mermaid diagrams (lazy-loads mermaid.js on first use)
  postRenderMermaid(containerEl);
}

// ---------------------------------------------------------------------------
//  Mermaid diagram rendering (lazy-loaded)
// ---------------------------------------------------------------------------
var _mermaidState = "idle"; // idle | loading | ready
var _mermaidQueue = []; // callbacks queued while loading
var _mermaidIdCounter = 0;

function _initMermaid() {
  // Clear caches on (re-)init so a theme change via reRenderAllMermaid
  // doesn't serve stale SVG keyed by source-only — the rendered output
  // depends on themeVariables which we just changed.
  if (typeof _mermaidSvgCache !== "undefined") _mermaidSvgCache.clear();
  if (typeof _mermaidErrorCache !== "undefined") _mermaidErrorCache.clear();
  mermaid.initialize({
    startOnLoad: false,
    securityLevel: "strict",
    theme: "base",
    themeVariables: _getMermaidTheme(),
  });
}

function _loadMermaid(callback) {
  if (_mermaidState === "ready") {
    callback();
    return;
  }
  _mermaidQueue.push(callback);
  if (_mermaidState === "loading") return;
  _mermaidState = "loading";
  var script = document.createElement("script");
  script.src = "/shared/mermaid-11.16.0/mermaid.min.js";
  script.onload = function () {
    _initMermaid();
    _mermaidState = "ready";
    var q = _mermaidQueue;
    _mermaidQueue = [];
    for (var i = 0; i < q.length; i++) q[i]();
  };
  script.onerror = function () {
    _mermaidState = "idle";
    _mermaidQueue = [];
    var els = document.querySelectorAll(".mermaid-loading");
    for (var i = 0; i < els.length; i++) {
      els[i].classList.remove("mermaid-loading");
      els[i].classList.add("mermaid-error");
      els[i].textContent = "Failed to load diagram renderer";
    }
  };
  document.head.appendChild(script);
}

function _getMermaidTheme() {
  var s = getComputedStyle(document.documentElement);
  return {
    primaryColor: s.getPropertyValue("--bg-surface").trim(),
    primaryTextColor: s.getPropertyValue("--fg").trim(),
    primaryBorderColor:
      s.getPropertyValue("--border-strong").trim() || "rgba(255,255,255,0.1)",
    lineColor: s.getPropertyValue("--fg-dim").trim(),
    secondaryColor: s.getPropertyValue("--bg-highlight").trim(),
    tertiaryColor: s.getPropertyValue("--bg").trim(),
    noteBkgColor: s.getPropertyValue("--bg-surface").trim(),
    noteTextColor: s.getPropertyValue("--fg").trim(),
    noteBorderColor: s.getPropertyValue("--accent").trim(),
    actorTextColor: s.getPropertyValue("--fg-bright").trim(),
    actorBkg: s.getPropertyValue("--bg-surface").trim(),
    actorBorder: s.getPropertyValue("--accent").trim(),
    signalColor: s.getPropertyValue("--fg").trim(),
    signalTextColor: s.getPropertyValue("--fg").trim(),
  };
}

// Mermaid label autoquoter.
//
// Mermaid's flowchart parser treats ( ) [ ] { } as shape delimiters
// EVERYWHERE — including inside other labels — unless the label is
// wrapped in "...". LLM-emitted diagrams routinely produce things
// like  A["x"] -->|note (with parens)| B  or  D[label (foo, bar)]
// and Mermaid then rejects them with "Parse error, got PS" (paren-
// start in shape context — the parser entered a nested shape parse
// at the bare `(` and ran out of expected closing tokens).
//
// We can't fix every malformed diagram, but the two patterns above
// are easy to spot syntactically and quote:
//
//   1. Edge labels:  |content|  →  |"content"|
//   2. Plain rectangle node labels:  ID[content]  →  ID["content"]
//
// Shapes whose syntax already nests delimiters — cylinders [(...)],
// subroutines [[...]], trapezoids [/.../] [\...\], circles ((...)),
// double circles (((...))), hexagons {{...}}, diamonds {...} — are
// intentionally left alone. The inner delimiters are part of the
// shape, and our regex would corrupt valid syntax. Authors using
// those shapes must quote the label manually.
function _normalizeMermaidSource(source) {
  if (!source) return source;
  // Fast path: no shape delimiters anywhere → nothing to quote.
  if (
    source.indexOf("(") === -1 &&
    source.indexOf("[") === -1 &&
    source.indexOf("{") === -1
  ) {
    return source;
  }
  var lines = source.split("\n");
  for (var i = 0; i < lines.length; i++) {
    var line = lines[i];
    // %% directives and comments — never rewrite. The %%{init:...}%%
    // form contains braces that would otherwise look like a label.
    if (/^\s*%%/.test(line)) continue;
    line = _quoteMermaidNodeLabels(line);
    line = _quoteMermaidEdgeLabels(line);
    lines[i] = line;
  }
  return lines.join("\n");
}

function _quoteMermaidNodeLabels(line) {
  // ID[content]  →  ID["content"]  when content needs quoting.
  //
  // The first character of content is restricted to NOT be [ ( / \
  // so we skip [[subroutine]], [(cylinder)], [/trap/], [\trap\].
  // The rest of content is restricted to NOT contain [ ] so the
  // regex can't run away past a legitimate ].
  return line.replace(
    /([A-Za-z_][\w-]*)\[([^[(/\\\n][^[\]\n]*?)\]/g,
    function (m, id, content) {
      if (_mermaidLabelNeedsQuoting(content)) {
        return id + '["' + content + '"]';
      }
      return m;
    },
  );
}

function _quoteMermaidEdgeLabels(line) {
  // |content|  →  |"content"|  when content needs quoting.
  // Edge labels can't contain a literal | (it's the delimiter), so
  // [^|\n] is exhaustive.
  return line.replace(/\|([^|\n]+)\|/g, function (m, content) {
    if (_mermaidLabelNeedsQuoting(content)) {
      return '|"' + content + '"|';
    }
    return m;
  });
}

function _mermaidLabelNeedsQuoting(content) {
  // Any literal " in content would produce nested unescaped quotes
  // when we wrap. Punt to manual fix. This also short-circuits the
  // already-correctly-quoted "..." case (which has " at the bounds).
  if (content.indexOf('"') !== -1) return false;
  // <br/> and <br> are part of Mermaid's allowed HTML in labels and
  // don't on their own require quoting.
  var stripped = content.replace(/<br\s*\/?>/gi, "");
  return /[()[\]{}]/.test(stripped);
}

// Source-keyed SVG cache. Identical mermaid source produces identical
// SVG, so we can swap in cached output synchronously without re-running
// mermaid.render. Crucial for streaming markdown: streamingRender does
// `el.innerHTML = html` wholesale on every rAF tick, which destroys
// rendered SVG nodes — without the cache, a closed mermaid block would
// re-trigger an async render on every subsequent token. With the cache,
// each unique source pays mermaid.render exactly once per session.
//
// Errored sources are cached too (as the message string) so a
// syntactically-broken diagram doesn't re-thrash mermaid on every
// render. The user can fix the diagram and the new source string
// misses the cache, triggering a fresh render.
//
// FIFO bounded so a long session that emits many distinct diagrams
// can't grow unbounded.
var _mermaidSvgCache = new Map();
var _mermaidErrorCache = new Map();
var _MERMAID_CACHE_MAX = 64;

// Raw-textContent → normalized memo. _normalizeMermaidSource splits +
// regex-replaces line by line; on a 50-line flowchart that's ~57 µs.
// The SVG cache short-circuits mermaid.render once we have the
// normalized key, but the *normalize step itself* runs on every rAF
// tick (postRenderMermaid always calls it before the SVG-cache
// lookup, since the normalized output IS the lookup key). Memoizing
// raw → normalized avoids repeating the split + regex for diagrams
// whose source hasn't changed between ticks.
//
// Bounded by _MERMAID_CACHE_MAX so its memory footprint stays in the
// same order of magnitude as the SVG cache it feeds, but the two
// queues evict INDEPENDENTLY: this memo keys on raw textContent while
// the SVG cache keys on normalized source, so a single diagram can
// occupy one slot in each with no positional coupling. The memo also
// deliberately survives `_initMermaid` (which clears the SVG / error
// caches on theme change) — normalization output is purely a function
// of input text, independent of mermaid theme / config.
var _mermaidNormalizeCache = new Map();

function _applyMermaidSvg(container, svg, bindFunctions) {
  container.innerHTML = svg;
  container.classList.remove("mermaid-loading", "mermaid-error");
  container.classList.add("mermaid-rendered");
  if (bindFunctions) bindFunctions(container);
}

function _applyMermaidError(container, source, message) {
  container.classList.remove("mermaid-loading");
  container.classList.add("mermaid-error");
  container.innerHTML =
    '<div class="mermaid-error-msg">' +
    escapeHtml(message || "Diagram error") +
    "</div>" +
    "<pre><code>" +
    escapeHtml(source) +
    "</code></pre>";
}

// Global render queue + per-source pending-container map.
//
// mermaid.render() uses module-level state internally — concurrent
// calls clobber that state. With postRenderMermaid now firing on
// every streaming rAF tick (not just on stream_end), two ticks
// could each find a fresh container (the prior tick's container
// is detached after innerHTML replace) for the SAME unfinished
// source, or for DIFFERENT sources, and both would queue
// mermaid.render() concurrently. Two layers of serialization fix
// this:
//
//   1. _mermaidPending: per-source. While a render is in flight
//      for source X, additional containers asking for source X
//      are queued; the single render result fans out to all
//      pending containers when it lands.
//   2. _mermaidRenderChain: across-source. Promises chain so
//      mermaid.render() runs at most one at a time globally.
//
// Detached containers (no longer in the DOM by the time the
// render completes) are skipped — innerHTML replace during
// streaming detaches them and a later rAF tick's render is
// already taking care of the live container.
var _mermaidPending = new Map();
var _mermaidRenderChain = Promise.resolve();

function _renderMermaidBlock(container, callback) {
  var source = container.getAttribute("data-mermaid-source");
  if (!source) {
    if (callback) callback();
    return;
  }
  // Same source already in flight — append to pending list.
  // Caller's callback fires as if the render started; the actual
  // SVG application happens when the in-flight render lands.
  if (_mermaidPending.has(source)) {
    _mermaidPending.get(source).push(container);
    if (callback) callback();
    return;
  }
  _mermaidPending.set(source, [container]);
  // ``linkPending`` is hoisted to the link's closure so the rejection-proof
  // .catch below can paint the error on the containers THIS link captured —
  // by the time it runs, the link already removed them from _mermaidPending,
  // so without the hoist they'd sit at "Loading diagram…" forever.
  var linkPending = null;
  _mermaidRenderChain = _mermaidRenderChain
    .then(function () {
      var pending = _mermaidPending.get(source) || [];
      linkPending = pending;
      _mermaidPending.delete(source);
      var id = "mermaid-" + ++_mermaidIdCounter;
      return mermaid.render(id, source).then(
        function (result) {
          _cacheFifoEntry(
            _mermaidSvgCache,
            source,
            { svg: result.svg, bindFunctions: result.bindFunctions },
            _MERMAID_CACHE_MAX,
          );
          for (var i = 0; i < pending.length; i++) {
            var c = pending[i];
            if (c.isConnected) {
              // Per-container guard: one bad apply (a bindFunctions throw)
              // must not skip the remaining containers for this source.
              try {
                _applyMermaidSvg(c, result.svg, result.bindFunctions);
              } catch (e) {
                _applyMermaidError(c, source, "diagram apply failed");
              }
            }
          }
        },
        function (err) {
          var orphan = document.getElementById(id);
          if (orphan) orphan.remove();
          var msg = err && err.message ? err.message : "Diagram error";
          _cacheFifoEntry(_mermaidErrorCache, source, msg, _MERMAID_CACHE_MAX);
          for (var i = 0; i < pending.length; i++) {
            var c = pending[i];
            if (c.isConnected) _applyMermaidError(c, source, msg);
          }
        },
      );
    })
    .catch(function (e) {
      // Rejection-proof every link: a sync throw escaping the link body
      // (e.g. mermaid.render throwing on malformed input before returning a
      // promise) would otherwise reject the shared chain, and every later
      // diagram would silently sit at "Loading diagram…" forever.  Settle
      // back to fulfilled and paint the error on the containers this link
      // had already claimed.  Deliberately NO _mermaidPending.delete(source)
      // here: the link deleted its own entry up top, and any entry present
      // NOW belongs to a newer re-entry for the same source — deleting it
      // would orphan THAT link's containers.
      var msg = (e && e.message) || "Diagram error";
      _cacheFifoEntry(_mermaidErrorCache, source, msg, _MERMAID_CACHE_MAX);
      (linkPending || []).forEach(function (c) {
        if (!c.isConnected) return;
        try {
          _applyMermaidError(c, source, msg);
        } catch (_) {
          /* container-level failure — nothing left to degrade to */
        }
      });
      console.warn("renderer: mermaid render chain error", e);
    });
  if (callback) callback();
}

// Render mermaid blocks via the global chain. Calls return
// immediately; serialization happens inside _renderMermaidBlock.
function _renderMermaidSequence(containers, idx) {
  for (var i = 0; i < containers.length; i++) {
    _renderMermaidBlock(containers[i]);
  }
}

function postRenderMermaid(containerEl) {
  var codeEls = containerEl.querySelectorAll("pre code.language-mermaid");
  if (codeEls.length === 0) return;
  var pendingContainers = [];
  for (var i = 0; i < codeEls.length; i++) {
    var pre = codeEls[i].closest("pre");
    if (!pre) continue;
    // Autoquote labels with bare shape-delimiter chars before
    // caching / rendering. Identical malformed input maps to identical
    // normalized output, so the SVG cache still hits on repeated
    // streams of the same diagram. _mermaidNormalizeCache skips the
    // split + per-line regex when the raw textContent hasn't changed
    // between ticks — only on a fresh source does normalization run.
    var raw = codeEls[i].textContent;
    var source;
    if (_mermaidNormalizeCache.has(raw)) {
      source = _mermaidNormalizeCache.get(raw);
    } else {
      source = _normalizeMermaidSource(raw);
      _cacheFifoEntry(_mermaidNormalizeCache, raw, source, _MERMAID_CACHE_MAX);
    }
    var div = document.createElement("div");
    div.setAttribute("data-mermaid-source", source);
    // Use cache.has (not truthiness) so a future cached value of
    // empty string / falsy SVG doesn't masquerade as a miss.
    if (_mermaidSvgCache.has(source)) {
      // Cache hit — sync swap, no loading flash, no async work.
      // Identical source produces identical SVG (mermaid is
      // deterministic for a given init), so reusing the rendered
      // result is safe across streamingRender's wholesale
      // innerHTML replaces. Re-apply via the same helper used
      // by fresh renders so mermaid's bindFunctions (link/click
      // bindings) attach to each new container instance.
      var cached = _mermaidSvgCache.get(source);
      div.className = "mermaid-container mermaid-rendered";
      _applyMermaidSvg(div, cached.svg, cached.bindFunctions);
      pre.replaceWith(div);
    } else if (_mermaidErrorCache.has(source)) {
      // Errored source — keep showing the error without thrashing
      // mermaid.render on every streaming tick.
      div.className = "mermaid-container mermaid-error";
      _applyMermaidError(div, source, _mermaidErrorCache.get(source));
      pre.replaceWith(div);
    } else {
      // Cache miss — show loading state, queue async render.
      div.className = "mermaid-container mermaid-loading";
      div.textContent = "Loading diagram\u2026";
      pre.replaceWith(div);
      pendingContainers.push(div);
    }
  }
  if (pendingContainers.length === 0) return;
  _loadMermaid(function () {
    _renderMermaidSequence(pendingContainers, 0);
  });
}

export function reRenderAllMermaid() {
  if (_mermaidState !== "ready") return;
  _initMermaid();
  var els = document.querySelectorAll(
    ".mermaid-container[data-mermaid-source]",
  );
  var arr = [];
  for (var i = 0; i < els.length; i++) {
    els[i].classList.add("mermaid-loading");
    els[i].classList.remove("mermaid-rendered", "mermaid-error");
    arr.push(els[i]);
  }
  _renderMermaidSequence(arr, 0);
}

// ---------------------------------------------------------------------------
//  Streaming helpers — shared between the server-node UI and coordinator.
//  Re-render the full buffer on each streamed token, but coalesce through
//  requestAnimationFrame so fast producers (10-100 tokens/sec) don't
//  trigger renderMarkdown + DOM replacement more than once per paint
//  cycle.  renderMarkdown tolerates mid-stream partial fences / lists
//  (they render as literal text and resolve once the closing tokens
//  arrive), and the per-element buffer cache skips identical redundant
//  renders (SSE retries / resumes).  Both hljs syntax highlighting
//  and mermaid run inline on every render so closed code / diagram
//  fences appear progressively as they complete; their source-keyed
//  caches (_hljsCache, _mermaidSvgCache) make subsequent rAF ticks
//  that re-extract the same closed fence hit synchronously without
//  re-invoking hljs.highlightElement / mermaid.render.
//  renderMarkdown escapes HTML internally (see escapeHtml in
//  utils.js); it is the trust boundary for the markup written to el
//  below.
// ---------------------------------------------------------------------------
function _streamingRenderApply(el, buffer) {
  if (el._lastRenderedBuffer === buffer) return;
  try {
    el.innerHTML = renderMarkdown(buffer);
  } catch (e) {
    // A render failure must not wedge the stream: show THIS frame as plain
    // text and leave the buffer UN-marked, so the next delta / the finalize
    // pass re-attempts a full render (partial-input throws heal themselves
    // once the closing tokens arrive).  Marking before the render used to
    // make an errored frame look done — the finalize short-circuit then
    // pinned the stale DOM forever.
    console.warn("renderer: streaming render failed; plain-text frame", e);
    el.textContent = buffer;
    return;
  }
  el._lastRenderedBuffer = buffer;
  // Progressive hljs + mermaid render — see comment above. Both are
  // no-ops when the element has no matching code blocks, and their
  // source-keyed caches avoid re-tokenizing / re-rendering for
  // sources we've already processed. Subsequent rAF ticks that
  // re-extract the same closed fence hit the cache synchronously.
  // Guarded: decoration failures degrade to undecorated markup, never to
  // a broken segment state upstream.
  try {
    postRenderHljs(el);
    postRenderMermaid(el);
  } catch (e) {
    console.warn("renderer: post-render decoration failed", e);
  }
}

export function streamingRender(el, buffer) {
  if (!el) return;
  // Short-circuit identical-buffer calls (e.g. SSE retry / resume).
  // V8's string === length-compares internally so the explicit check is
  // redundant.
  if (el._lastRenderedBuffer === buffer) return;
  el._pendingBuffer = buffer;
  if (el._rafHandle) return; // one render per animation frame
  el._rafHandle = requestAnimationFrame(function () {
    el._rafHandle = 0;
    // If the element has been removed from the tree between schedule
    // and flush (pane cleared, message deleted, view swapped) skip the
    // render so we don't mutate a detached node + keep its buffer
    // strings alive until GC.
    if (!el.isConnected) return;
    _streamingRenderApply(el, el._pendingBuffer);
  });
}

export function streamingRenderFinalize(el, buffer) {
  if (!el) return;
  // Flush any pending rAF-scheduled render so the finalize sees the
  // final buffer exactly once, then run the expensive post-render
  // (hljs / mermaid / KaTeX) on the settled DOM.
  if (el._rafHandle) {
    cancelAnimationFrame(el._rafHandle);
    el._rafHandle = 0;
  }
  _streamingRenderApply(el, buffer);
  if (typeof postRenderMarkdown === "function") {
    postRenderMarkdown(el);
  }
}

// --- Legacy window bridge ---------------------------------------------------
// Still-classic consumers reach these as globals at event/boot time (after
// this deferred module evaluated).  New module code imports instead.
Object.assign(window, {
  renderMarkdown,
  inlineMarkdown,
  postRenderHljs,
  postRenderMarkdown,
  reRenderAllMermaid,
  streamingRender,
  streamingRenderFinalize,
});
