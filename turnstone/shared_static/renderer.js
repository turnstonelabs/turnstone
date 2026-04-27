// renderer.js — Markdown + LaTeX rendering (no external deps except KaTeX)

// ---------------------------------------------------------------------------
//  Inline formatting
// ---------------------------------------------------------------------------
// Safe inline HTML tags allowed through escapeHtml (no attributes — XSS safe)
// Block-level tags (details, summary, hr) handled by their own protection passes
var _SAFE_TAGS =
  /&lt;(\/?(?:br|kbd|mark|sub|sup|ins|wbr|abbr|small|u|s))(?:\s*\/?)&gt;/gi;

function inlineMarkdown(text) {
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
  // Images (must come before links — render as click-to-load placeholder)
  text = text.replace(/!\[([^\]]*)\]\(([^)]+)\)/g, function (m, alt, url) {
    if (!/^\s*(https?:\/\/|data:image\/)/i.test(url)) return m;
    var safeAlt = alt || "Image";
    var domain = "";
    try {
      domain = escapeHtml(new URL(url).hostname);
    } catch (e) {
      domain = url.length > 40 ? url.slice(0, 40) + "…" : url;
    }
    return (
      '<span class="img-placeholder" tabindex="0" role="button" ' +
      'aria-label="Load image: ' +
      safeAlt +
      '" ' +
      'data-src="' +
      url +
      '" data-alt="' +
      safeAlt +
      '">' +
      '<span class="img-placeholder-icon">&#x1F5BC;</span> ' +
      '<span class="img-placeholder-label">' +
      safeAlt +
      "</span>" +
      '<span class="img-placeholder-domain">' +
      domain +
      "</span>" +
      "</span>"
    );
  });
  // Links (allow http, https, and same-origin relative URLs only)
  text = text.replace(/\[([^\]]+)\]\(([^)]+)\)/g, function (m, label, url) {
    if (!/^\s*(https?:\/\/|\/(?!\/))/i.test(url)) return m;
    return (
      '<a href="' +
      url +
      '" target="_blank" rel="noopener noreferrer">' +
      label +
      "</a>"
    );
  });
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
function renderMarkdown(text) {
  // Scope footnote IDs per top-level render call (prevents collisions across messages)
  if (_fnDepth === 0) _fnScopeId++;
  _fnDepth++;

  // Pre-pass: extract blockquote blocks and recursively render.
  // Must run FIRST (before code/math protection) so the recursive call
  // processes raw markdown, not text with outer-scope placeholders.
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

  // Protect code blocks.  The opening-run length is captured and
  // required on the close via backreference so a 4-backtick outer
  // fence wrapping a 3-backtick inner (common when embedding
  // markdown-about-markdown or lang-tagged snippets inside another
  // code block) is tokenised as one outer block with the inner
  // triple-backticks preserved verbatim — the prior `` ```...``` ``
  // regex treated the outer-open and inner-open as a single fence
  // pair, stranding the rest of the content with visible
  // \x00CB{n}\x00 sentinels.
  var codeBlocks = [];
  text = text.replace(
    /(```+)([^\s`]*)\n([\s\S]*?)\1/g,
    function (m, _open, lang, code) {
      var cssLang = _langToCssClass(lang);
      codeBlocks.push(
        "<pre><code" +
          (cssLang ? ' class="language-' + escapeHtml(cssLang) + '"' : "") +
          ">" +
          escapeHtml(code.replace(/\n$/, "")) +
          "</code></pre>",
      );
      return "\x00CB" + (codeBlocks.length - 1) + "\x00";
    },
  );

  // Protect <details> blocks (safe HTML — attribute-free only)
  var detailsBlocks = [];
  text = text.replace(
    /<details>\s*\n?([\s\S]*?)<\/details>/gi,
    function (m, inner) {
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

  // Protect inline math — both delimiter styles ($...$ and \(...\)).
  // Both regexes explicitly forbid newlines inside the captured
  // group: an unterminated \(...\) (or $...$) on one line would
  // otherwise eat the next paragraph until it found a closing
  // delimiter, which is jarring on streaming markdown where the
  // closer hasn't arrived yet. Display math (\[...\] / $$...$$)
  // is the multi-line form by design.
  var inlineMaths = [];
  text = text.replace(/\$([^\$\n]+?)\$/g, function (m, tex) {
    inlineMaths.push(renderLatex(tex, false));
    return "\x00IM" + (inlineMaths.length - 1) + "\x00";
  });
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

  // Append footnote section if any definitions were collected
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

  // Restore protected blocks
  result = result.replace(/\x00CB(\d+)\x00/g, function (m, idx) {
    return codeBlocks[parseInt(idx)];
  });
  result = result.replace(/<p>\x00DT(\d+)\x00<\/p>/g, function (m, idx) {
    return detailsBlocks[parseInt(idx)];
  });
  result = result.replace(/\x00DT(\d+)\x00/g, function (m, idx) {
    return detailsBlocks[parseInt(idx)];
  });
  result = result.replace(/<p>\x00BQ(\d+)\x00<\/p>/g, function (m, idx) {
    return bqBlocks[parseInt(idx)];
  });
  result = result.replace(/\x00BQ(\d+)\x00/g, function (m, idx) {
    return bqBlocks[parseInt(idx)];
  });
  result = result.replace(/<p>\x00MB(\d+)\x00<\/p>/g, function (m, idx) {
    return mathBlocks[parseInt(idx)];
  });
  result = result.replace(/\x00MB(\d+)\x00/g, function (m, idx) {
    return mathBlocks[parseInt(idx)];
  });
  result = result.replace(/<p>\x00TB(\d+)\x00<\/p>/g, function (m, idx) {
    return tableBlocks[parseInt(idx)];
  });
  result = result.replace(/\x00TB(\d+)\x00/g, function (m, idx) {
    return tableBlocks[parseInt(idx)];
  });
  result = result.replace(/\x00IC(\d+)\x00/g, function (m, idx) {
    return inlineCodes[parseInt(idx)];
  });
  result = result.replace(/\x00IM(\d+)\x00/g, function (m, idx) {
    return inlineMaths[parseInt(idx)];
  });

  _fnDepth--;
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

function postRenderMarkdown(containerEl) {
  // Syntax highlighting (skip if highlight.js unavailable)
  if (typeof hljs !== "undefined") {
    if (!_hljsConfigured) {
      hljs.configure({ ignoreUnescapedHTML: true });
      _hljsConfigured = true;
    }
    var codeEls = containerEl.querySelectorAll("pre code[class*='language-']");
    for (var i = 0; i < codeEls.length; i++) {
      var el = codeEls[i];
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
      // Apply highlighting
      hljs.highlightElement(el);
      // Add terminal styling class for shell languages
      if (_TERMINAL_LANGS[langClass]) {
        el.closest("pre").classList.add("code-terminal");
      }
    }
  }
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
  script.src = "/shared/mermaid-11.14.0/mermaid.min.js";
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

function _cacheMermaidEntry(cache, source, value) {
  // Only evict the oldest when inserting a new key — overwriting an
  // existing source is an in-place update and should not pay the
  // eviction cost (which would drop an unrelated cached entry).
  if (!cache.has(source) && cache.size >= _MERMAID_CACHE_MAX) {
    var firstKey = cache.keys().next().value;
    cache.delete(firstKey);
  }
  cache.set(source, value);
}

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
  _mermaidRenderChain = _mermaidRenderChain.then(function () {
    var pending = _mermaidPending.get(source) || [];
    _mermaidPending.delete(source);
    var id = "mermaid-" + ++_mermaidIdCounter;
    return mermaid.render(id, source).then(
      function (result) {
        _cacheMermaidEntry(_mermaidSvgCache, source, {
          svg: result.svg,
          bindFunctions: result.bindFunctions,
        });
        for (var i = 0; i < pending.length; i++) {
          var c = pending[i];
          if (c.isConnected) {
            _applyMermaidSvg(c, result.svg, result.bindFunctions);
          }
        }
      },
      function (err) {
        var orphan = document.getElementById(id);
        if (orphan) orphan.remove();
        var msg = err && err.message ? err.message : "Diagram error";
        _cacheMermaidEntry(_mermaidErrorCache, source, msg);
        for (var i = 0; i < pending.length; i++) {
          var c = pending[i];
          if (c.isConnected) _applyMermaidError(c, source, msg);
        }
      },
    );
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
    var source = codeEls[i].textContent;
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

function reRenderAllMermaid() {
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
//  renders (SSE retries / resumes).  hljs syntax highlighting stays
//  deferred to streamingRenderFinalize, but mermaid runs inline on
//  every render so closed diagram fences appear progressively as they
//  complete (the source-keyed SVG cache makes re-renders cheap; only
//  the first encounter with a given source pays mermaid.render).
//  renderMarkdown escapes HTML internally (see escapeHtml in
//  utils.js); it is the trust boundary for the markup written to el
//  below.
// ---------------------------------------------------------------------------
function _streamingRenderApply(el, buffer) {
  if (el._lastRenderedBuffer === buffer) return;
  el._lastRenderedBuffer = buffer;
  var html = renderMarkdown(buffer);
  el.innerHTML = html;
  // Progressive mermaid render — see comment above. postRenderMermaid
  // is no-op when the element has no language-mermaid code blocks,
  // and the source-keyed cache avoids re-invoking mermaid.render
  // for blocks we've already rendered. Subsequent rAF ticks that
  // re-extract the same closed fence hit the cache synchronously.
  if (typeof postRenderMermaid === "function") {
    postRenderMermaid(el);
  }
}

function streamingRender(el, buffer) {
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

function streamingRenderFinalize(el, buffer) {
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
