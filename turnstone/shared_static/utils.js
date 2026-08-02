/* Shared utility functions — turnstone design system

   ES module at the BOTTOM of the shared module graph: imports nothing, so
   renderer.js / auth.js / kb.js / cards.js can import from here without
   cycles.  The one helper that calls upward (exportWorkstreamDownload →
   toast/auth) late-binds through window at CALL time instead — importing
   here would close an import cycle.

   The window bridge at the bottom keeps the still-classic consumers
   (console app.js / admin.js / governance.js, ui app.js, inline onclick=)
   working; modules should import instead. */

export function escapeHtml(text) {
  const el = document.createElement("span");
  el.textContent = text;
  return el.innerHTML.replace(/'/g, "&#39;").replace(/"/g, "&quot;");
}

export function formatTokens(n) {
  if (n >= 1000000) return (n / 1000000).toFixed(1) + "M";
  if (n >= 1000) return (n / 1000).toFixed(1) + "k";
  return String(n || 0);
}

export function ctxClass(ratio) {
  if (ratio <= 0) return "ctx-idle";
  const pct = ratio * 100;
  if (pct < 30) return "ctx-low";
  if (pct < 50) return "ctx-mid";
  if (pct < 80) return "ctx-high";
  return "ctx-danger";
}

export function formatUptime(seconds) {
  if (!seconds) return "";
  if (seconds < 60) return seconds + "s";
  const min = Math.floor(seconds / 60);
  if (min < 60) return min + "m";
  const hr = Math.floor(min / 60);
  return hr + "h " + (min % 60) + "m";
}

export function formatCount(n) {
  if (n >= 1000) return (n / 1000).toFixed(1) + "k";
  return String(n);
}

// Friendly display label for an operator-context system turn's `_source`.
// The metacognition nudge types (metacognition._NUDGE_MAP, mirrored in
// tool_advisory.SYSTEM_TURN_SOURCES) collapse to one "metacognition" category;
// the other generic-bubble sources are humanized.
//
// The map must cover EVERY member of tool_advisory.SYSTEM_TURN_SOURCES,
// carded or not, because a missing entry falls through to the raw
// `_source` ("operator · idle_children") — the regression
// test_app_js.test_operator_nudge_labels_use_shared_helper guards.
//
// Two ways a source reaches this label:
//   * uncarded kinds (compaction_pending / background_shell_exit /
//     participant_joined) have no dispatch branch at all, so they reach
//     it on EVERY render;
//   * carded kinds reach it when a replayed turn's persisted
//     `_source_meta` is absent or unparseable, because every card
//     dispatch is guarded on the turn carrying `meta`.
// `compaction` is the one exception — handled first and unguarded in
// both panes, so it never arrives here.
const OPERATOR_SOURCE_LABELS = {
  correction: "metacognition",
  denial: "metacognition",
  resume: "metacognition",
  completion: "metacognition",
  start: "metacognition",
  repeat: "metacognition",
  tool_error: "tool error",
  skill_hint: "skill hint",
  idle_children: "idle children",
  idle_tasks: "open tasks",
  watch_triggered: "watch",
  output_guard: "output guard",
  user_interjection: "queued message",
  compaction_pending: "context budget",
  background_shell_exit: "background shell",
  participant_joined: "participant",
};
export function operatorSourceLabel(source) {
  return OPERATOR_SOURCE_LABELS[source] || source || "operator";
}

// Naive ISO-8601 → "Nm ago" / "Nh ago" / "Nd ago" / locale date.
// Tolerates space-as-separator (SQLite default) and missing TZ marker
// (assumes UTC, matching the storage layer's stamp).
export function formatRelativeTime(iso) {
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
export function cssEscape(s) {
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
export function makeKeyLabel(hint, label) {
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
export function makeEmptyState(text) {
  const div = document.createElement("div");
  div.className = "dashboard-empty";
  div.textContent = text;
  return div;
}

// Build a screen-reader live-region announcer and return its announce
// function.  The sr-only region is appended to document.body EAGERLY, at
// factory time: assistive tech only announces changes to a region that
// was ALREADY in the accessibility tree, so a region born lazily with
// its first message is silent exactly once.  Announcing is clear-then-
// set on a short timer so repeated identical messages re-announce.
// Callers keep one announcer per concern (voice status, tool early
// paint, copy outcomes) so two announcements never clobber each other
// inside one region.
export function makeAnnouncer() {
  const region = document.createElement("span");
  region.className = "sr-only";
  region.setAttribute("role", "status");
  region.setAttribute("aria-live", "polite");
  document.body.appendChild(region);
  return function announce(text) {
    region.textContent = "";
    window.setTimeout(function () {
      region.textContent = text;
    }, 30);
  };
}

// Copy text to the system clipboard; resolves true on success.  The
// async Clipboard API exists only in secure contexts (HTTPS or
// localhost), and cluster nodes reached over plain HTTP on a LAN have
// no `navigator.clipboard` at all — those fall back to the legacy
// hidden-textarea + execCommand("copy") path.  execCommand copies the
// textarea's selection, so the user's own selection and focus are
// captured first and restored after.
export async function copyTextToClipboard(text) {
  const value = String(text == null ? "" : text);
  if (window.isSecureContext && navigator.clipboard) {
    try {
      await navigator.clipboard.writeText(value);
      return true;
    } catch (e) {
      /* permission denied — the legacy path below still has a shot */
    }
  }
  const prevFocus = document.activeElement;
  const sel = document.getSelection();
  const prevRanges = [];
  if (sel) {
    // cloneRange: getRangeAt returns LIVE ranges, and moving the
    // selection into the shim textarea below can collapse them in
    // place — a live ref would "restore" the collapsed range.
    for (let i = 0; i < sel.rangeCount; i++) {
      prevRanges.push(sel.getRangeAt(i).cloneRange());
    }
  }
  const ta = document.createElement("textarea");
  ta.value = value;
  ta.setAttribute("readonly", "");
  ta.setAttribute("aria-hidden", "true");
  // This off-screen node briefly becomes the focused / hit-tested
  // element mid-copy; delegated UI listeners (the copy affordance's
  // pointer-dismissal rule) must be able to recognize and ignore the
  // shim, or the copy gesture dismisses its own button mid-copy.
  ta.setAttribute("data-clipboard-shim", "");
  ta.style.position = "fixed";
  ta.style.top = "0";
  ta.style.left = "0";
  ta.style.width = "1px";
  ta.style.height = "1px";
  ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.select();
  let ok = false;
  try {
    ok = document.execCommand("copy");
  } catch (e) {
    ok = false;
  }
  ta.remove();
  if (sel) {
    sel.removeAllRanges();
    for (let i = 0; i < prevRanges.length; i++) sel.addRange(prevRanges[i]);
  }
  if (prevFocus && typeof prevFocus.focus === "function") {
    try {
      prevFocus.focus({ preventScroll: true });
    } catch (e) {
      /* focus restoration is best-effort */
    }
  }
  return ok;
}

// Parse a *trusted* HTML string into DOM nodes and install them as
// the new children of ``el``.  Callers must guarantee the HTML was
// produced by an escaping / sanitising pipeline (escapeHtml,
// renderMarkdown, or static template literals with no caller-supplied
// interpolation) — DOMParser will faithfully parse whatever it is
// given.  The DOMParser path keeps the unsafe sink off the call site
// without requiring every caller to construct DOM elements by hand.
export function setSafeHtml(el, html) {
  const parsed = new DOMParser().parseFromString(html, "text/html");
  el.replaceChildren(...Array.from(parsed.body.childNodes));
}

// Download a workstream's conversation as OpenAI-shaped JSON.  Hits
// GET {base}/v1/api/workstreams/{ws_id}/export, which streams a
// ``{"messages":[...]}`` body with a Content-Disposition attachment
// filename.  Shared by the interactive appbar (app.js) and the
// coordinator appbar (coordinator.js) so both export buttons behave
// identically.  ``base`` is the session's transport prefix — "" for a
// local / console-homed session (the default), "/node/{id}" when the
// console proxies a node-hosted interactive session (the export must
// come from the node that owns the conversation, not the console).
// authFetch already handles the 401 (shows login) and
// 429 (retry) paths and returns the raw Response, so we read .blob()
// directly and synthesise an anchor click to trigger the browser save.
export async function exportWorkstreamDownload(wsId, btn, base) {
  if (!wsId) {
    window.showToast("No conversation to export", "error");
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
    const url =
      (base || "") +
      "/v1/api/workstreams/" +
      encodeURIComponent(wsId) +
      "/export";
    let r;
    try {
      r = await window.authFetch(url);
    } catch (e) {
      // authFetch throws Error("auth") on 401 after showing the login
      // modal — nothing more to do here.
      return;
    }
    if (!r || !r.ok) {
      window.showToast("Export failed", "error");
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
    window.showToast("Exported " + filename);
  } finally {
    exportWorkstreamDownload._busy = false;
    if (btn) {
      btn.disabled = false;
      btn.removeAttribute("aria-busy");
    }
  }
}

// --- Legacy window bridge ---------------------------------------------------
// Classic consumers (console app.js / admin.js / governance.js, ui app.js,
// inline onclick=) still reach these as globals; they only do so at event /
// boot time, well after this deferred module has evaluated.  New module code
// imports instead.  Drop entries as the classic bundles migrate.
Object.assign(window, {
  escapeHtml,
  formatTokens,
  ctxClass,
  formatUptime,
  formatCount,
  operatorSourceLabel,
  formatRelativeTime,
  cssEscape,
  makeKeyLabel,
  makeEmptyState,
  copyTextToClipboard,
  setSafeHtml,
  exportWorkstreamDownload,
});
