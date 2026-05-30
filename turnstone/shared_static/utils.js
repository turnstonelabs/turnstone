/* Shared utility functions — turnstone design system */

function escapeHtml(text) {
  const el = document.createElement("span");
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
  const pct = ratio * 100;
  if (pct < 30) return "ctx-low";
  if (pct < 50) return "ctx-mid";
  if (pct < 80) return "ctx-high";
  return "ctx-danger";
}

function formatUptime(seconds) {
  if (!seconds) return "";
  if (seconds < 60) return seconds + "s";
  const min = Math.floor(seconds / 60);
  if (min < 60) return min + "m";
  const hr = Math.floor(min / 60);
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
  let s = String(iso).replace(" ", "T");
  if (!s.endsWith("Z") && !s.includes("+")) s += "Z";
  const d = new Date(s);
  if (isNaN(d)) return "";
  const ms = new Date() - d;
  const min = Math.floor(ms / 60000);
  if (min < 1) return "just now";
  if (min < 60) return min + "m ago";
  const hr = Math.floor(min / 60);
  if (hr < 24) return hr + "h ago";
  const day = Math.floor(hr / 24);
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
  const str = String(s == null ? "" : s);
  if (typeof CSS !== "undefined" && typeof CSS.escape === "function") {
    return CSS.escape(str);
  }
  return str.replace(/["\\]/g, "\\$&");
}

// Build a fragment that renders the "keyboard hint + label" pattern
// used on approval, deny, always, and plan amend/reject buttons.  The
// hint glyph (e.g. "y", "Esc") gets the .key class so CSS draws the
// outlined keycap; the trailing label is a plain text node.  Returning
// a DocumentFragment lets callers use either append() or
// replaceChildren() depending on whether the button is fresh or
// rebuilt in place.
function makeKeyLabel(hint, label) {
  const span = document.createElement("span");
  span.className = "key";
  span.textContent = hint;
  const frag = document.createDocumentFragment();
  frag.append(span, " " + label);
  return frag;
}

// Build a placeholder card with the .dashboard-empty class, used for
// "Loading…", "Failed to load", and "No active workstreams" states
// across the dashboard surfaces.  Callers typically pass the result to
// el.replaceChildren(...) so the empty card replaces existing content.
function makeEmptyState(text) {
  const div = document.createElement("div");
  div.className = "dashboard-empty";
  div.textContent = text;
  return div;
}

// Parse a *trusted* HTML string into DOM nodes and install them as
// the new children of ``el``.  Callers must guarantee the HTML was
// produced by an escaping / sanitising pipeline (escapeHtml,
// renderMarkdown, or static template literals with no caller-supplied
// interpolation) — DOMParser will faithfully parse whatever it is
// given.  The DOMParser path keeps the unsafe sink off the call site
// without requiring every caller to construct DOM elements by hand.
function setSafeHtml(el, html) {
  const parsed = new DOMParser().parseFromString(html, "text/html");
  el.replaceChildren(...Array.from(parsed.body.childNodes));
}

// Render markdown content into an element.  renderMarkdown produces
// fully-escaped HTML (see renderer.js — every input runs through
// escapeHtml before any markdown ops; URLs are gated by an allow-list
// regex), so routing the result through setSafeHtml is safe as long
// as renderer.js is trusted.  postRenderMarkdown finishes the job —
// hljs highlighting + mermaid SVG rendering for any code blocks the
// markdown emitted.
function setMarkdown(el, content) {
  setSafeHtml(el, renderMarkdown(content));
  postRenderMarkdown(el);
}

// Replay the ``advisories`` array attached to a tool history message.
// Queued user messages spliced into the last tool-result envelope of
// a batch (Seam 1) persist on the tool DB row as a wrapped
// ``<tool_output>`` envelope; the wire layer projects them onto
// ``msg.advisories``.  Both interactive's ``replayHistory`` and the
// coord history loop walk the array, filter on ``user_interjection``,
// and route ``adv.text`` through their own renderer (interactive uses
// ``addUserMessage``; coord uses ``appendUserMessageWithAttachments``).
// This shared helper centralises the walk + filter so a future
// advisory shape change lands once.
function replayAdvisoriesAfterTool(advisories, renderUserText) {
  if (!Array.isArray(advisories) || !advisories.length) return;
  for (let ai = 0; ai < advisories.length; ai++) {
    const adv = advisories[ai];
    if (!adv || adv.type !== "user_interjection") continue;
    renderUserText(adv.text || "");
  }
}

// Download a workstream's conversation as OpenAI-shaped JSON.  Hits
// GET /v1/api/workstreams/{ws_id}/export, which streams a
// ``{"messages":[...]}`` body with a Content-Disposition attachment
// filename.  Shared by the interactive appbar (app.js) and the
// coordinator appbar (coordinator.js) so both export buttons behave
// identically.  authFetch already handles the 401 (shows login) and
// 429 (retry) paths and returns the raw Response, so we read .blob()
// directly and synthesise an anchor click to trigger the browser save.
async function exportWorkstreamDownload(wsId, btn) {
  if (!wsId) {
    showToast("No conversation to export", "error");
    return;
  }
  // Re-entrancy guard: a double-click (or Enter+Enter) must not fire two
  // concurrent exports / two downloads.  The optional triggering button
  // is disabled for the duration as the in-progress affordance, matching
  // the send/stop buttons' disable-during-async pattern.
  if (exportWorkstreamDownload._busy) return;
  exportWorkstreamDownload._busy = true;
  if (btn) {
    btn.disabled = true;
    btn.setAttribute("aria-busy", "true");
  }
  try {
    const url = "/v1/api/workstreams/" + encodeURIComponent(wsId) + "/export";
    let r;
    try {
      r = await authFetch(url);
    } catch (e) {
      // authFetch throws Error("auth") on 401 after showing the login
      // modal — nothing more to do here.
      return;
    }
    if (!r || !r.ok) {
      showToast("Export failed", "error");
      return;
    }
    let filename = wsId + ".json";
    const cd = r.headers.get("Content-Disposition");
    if (cd) {
      const m = cd.match(/filename="([^"]+)"/);
      if (m) filename = m[1];
    }
    const blob = await r.blob();
    const objUrl = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = objUrl;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(objUrl);
    showToast("Exported " + filename);
  } finally {
    exportWorkstreamDownload._busy = false;
    if (btn) {
      btn.disabled = false;
      btn.removeAttribute("aria-busy");
    }
  }
}
