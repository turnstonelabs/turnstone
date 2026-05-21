/* Shared keyboard shortcuts overlay — turnstone design system
   Configure: window.TURNSTONE_KB_SHORTCUTS = [{title, keys: [{desc, badge}]}] */

let _kbPreviousFocus = null;

function showKbHelp() {
  _kbPreviousFocus = document.activeElement;
  const existing = document.getElementById("kb-overlay");
  if (existing) existing.remove();
  const shortcuts = window.TURNSTONE_KB_SHORTCUTS || [];
  let html =
    '<div id="kb-box" role="dialog" aria-modal="true" aria-label="Keyboard shortcuts" tabindex="-1">' +
    "<h2>Keyboard shortcuts</h2>";
  shortcuts.forEach(function (section) {
    html += '<div class="kb-section">' + escapeHtml(section.title) + "</div>";
    section.keys.forEach(function (k) {
      html +=
        '<div class="kb-row"><span class="kb-desc">' +
        escapeHtml(k.desc) +
        "</span>" +
        k.badge +
        "</div>";
    });
  });
  html +=
    '<div class="kb-hint">Press <span class="kb-key">Esc</span> to close</div>' +
    "</div>";
  const overlay = document.createElement("div");
  overlay.id = "kb-overlay";
  setSafeHtml(overlay, html);
  overlay.onclick = function (e) {
    if (e.target === overlay) hideKbHelp();
  };
  document.body.appendChild(overlay);
  document.getElementById("kb-box").focus();
}

function hideKbHelp() {
  const el = document.getElementById("kb-overlay");
  if (el) el.remove();
  if (_kbPreviousFocus && _kbPreviousFocus.focus) {
    _kbPreviousFocus.focus();
    _kbPreviousFocus = null;
  }
}

document.addEventListener("keydown", function (e) {
  if (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA") return;
  const login = document.getElementById("login-overlay");
  if (login && login.style.display !== "none") return;
  if (e.key === "?" && !e.ctrlKey && !e.metaKey) {
    e.preventDefault();
    showKbHelp();
    return;
  }
  if (e.key === "Escape") {
    const kb = document.getElementById("kb-overlay");
    if (kb) {
      e.preventDefault();
      hideKbHelp();
      return;
    }
  }
});
