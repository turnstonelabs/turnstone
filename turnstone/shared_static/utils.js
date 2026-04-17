/* Shared utility functions — turnstone design system */

function escapeHtml(text) {
  var el = document.createElement("span");
  el.textContent = text;
  return el.innerHTML.replace(/'/g, "&#39;").replace(/"/g, "&quot;");
}

function formatTokens(n) {
  if (n >= 1000000) return (n / 1000000).toFixed(1) + "M";
  if (n >= 1000) return (n / 1000).toFixed(1) + "k";
  return String(n || 0);
}

function ctxClass(ratio) {
  if (ratio <= 0) return "ctx-idle";
  var pct = ratio * 100;
  if (pct < 30) return "ctx-low";
  if (pct < 50) return "ctx-mid";
  if (pct < 80) return "ctx-high";
  return "ctx-danger";
}

function formatUptime(seconds) {
  if (!seconds) return "";
  if (seconds < 60) return seconds + "s";
  var min = Math.floor(seconds / 60);
  if (min < 60) return min + "m";
  var hr = Math.floor(min / 60);
  return hr + "h " + (min % 60) + "m";
}

function formatCount(n) {
  if (n >= 1000) return (n / 1000).toFixed(1) + "k";
  return String(n);
}

// Safe CSS attribute-selector escape.  CSS.escape is universally
// supported in modern browsers, but we keep a minimal polyfill so
// selector-construction never throws on an older browser or a
// sandboxed runtime where CSS is undefined.  Unlike CSS.escape
// (which is spec-exact), this fallback handles the characters that
// actually appear in our id formats — hex ws_ids, alphanumeric
// node_ids — and escapes the characters a CSS attribute selector
// treats specially.
function cssEscape(s) {
  var str = String(s == null ? "" : s);
  if (typeof CSS !== "undefined" && typeof CSS.escape === "function") {
    return CSS.escape(str);
  }
  return str.replace(/["\\]/g, "\\$&");
}
