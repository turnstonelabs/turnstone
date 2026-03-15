// renderer.js — Markdown + LaTeX rendering (no external deps except KaTeX)

// ---------------------------------------------------------------------------
//  Inline formatting
// ---------------------------------------------------------------------------
function inlineMarkdown(text) {
  // Escape HTML first so only tags we generate are real
  text = escapeHtml(text);
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
  // Links (block javascript: scheme)
  text = text.replace(/\[([^\]]+)\]\(([^)]+)\)/g, function (m, label, url) {
    if (/^\s*javascript:/i.test(url)) return m;
    return (
      '<a href="' +
      url +
      '" target="_blank" rel="noopener noreferrer">' +
      label +
      "</a>"
    );
  });
  return text;
}

// Attach click-to-load listener for image placeholders (delegated)
document.addEventListener("click", function (e) {
  var ph = e.target.closest(".img-placeholder");
  if (!ph) return;
  var img = document.createElement("img");
  img.src = ph.getAttribute("data-src");
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
//  Main markdown renderer
// ---------------------------------------------------------------------------
function renderMarkdown(text) {
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
        bqBlocks.push(
          "<blockquote>" + renderMarkdown(inner.join("\n")) + "</blockquote>",
        );
        result.push("\x00BQ" + (bqBlocks.length - 1) + "\x00");
      } else {
        result.push(blines[i]);
        i++;
      }
    }
    text = result.join("\n");
  })();

  // Protect code blocks
  var codeBlocks = [];
  text = text.replace(/```(\w*)\n([\s\S]*?)```/g, function (m, lang, code) {
    codeBlocks.push(
      '<pre><code class="lang-' +
        escapeHtml(lang) +
        '">' +
        escapeHtml(code.replace(/\n$/, "")) +
        "</code></pre>",
    );
    return "\x00CB" + (codeBlocks.length - 1) + "\x00";
  });

  // Protect display math ($$...$$) — must come before inline code/math
  var mathBlocks = [];
  text = text.replace(/\$\$([\s\S]+?)\$\$/g, function (m, tex) {
    mathBlocks.push(renderLatex(tex.trim(), true));
    return "\x00MB" + (mathBlocks.length - 1) + "\x00";
  });

  // Protect inline code
  var inlineCodes = [];
  text = text.replace(/`([^`\n]+)`/g, function (m, code) {
    inlineCodes.push("<code>" + escapeHtml(code) + "</code>");
    return "\x00IC" + (inlineCodes.length - 1) + "\x00";
  });

  // Protect inline math ($...$) — after inline code so `$x$` in code is safe
  var inlineMaths = [];
  text = text.replace(/\$([^\$\n]+?)\$/g, function (m, tex) {
    inlineMaths.push(renderLatex(tex, false));
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

    // Paragraph / plain text
    if (line.trim() === "") {
      out.push("");
    } else {
      out.push("<p>" + inlineMarkdown(line) + "</p>");
    }
  }

  var result = out.join("\n");

  // Restore protected blocks
  result = result.replace(/\x00CB(\d+)\x00/g, function (m, idx) {
    return codeBlocks[parseInt(idx)];
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

  return result;
}
