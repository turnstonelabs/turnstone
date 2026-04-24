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

// Naive ISO-8601 → "Nm ago" / "Nh ago" / "Nd ago" / locale date.
// Tolerates space-as-separator (SQLite default) and missing TZ marker
// (assumes UTC, matching the storage layer's stamp).
function formatRelativeTime(iso) {
  if (!iso) return "";
  var s = String(iso).replace(" ", "T");
  if (!s.endsWith("Z") && !s.includes("+")) s += "Z";
  var d = new Date(s);
  if (isNaN(d)) return "";
  var ms = new Date() - d;
  var min = Math.floor(ms / 60000);
  if (min < 1) return "just now";
  if (min < 60) return min + "m ago";
  var hr = Math.floor(min / 60);
  if (hr < 24) return hr + "h ago";
  var day = Math.floor(hr / 24);
  if (day < 30) return day + "d ago";
  return d.toLocaleDateString();
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
