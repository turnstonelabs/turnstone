/* Shared keyboard shortcuts overlay — turnstone design system
   Configure: window.TURNSTONE_KB_SHORTCUTS = [{title, keys: [{desc, badge}]}] */

var _kbPreviousFocus = null;

function showKbHelp() {
  _kbPreviousFocus = document.activeElement;
  var existing = document.getElementById("kb-overlay");
  if (existing) existing.remove();
  var shortcuts = window.TURNSTONE_KB_SHORTCUTS || [];
  var html =
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
  var overlay = document.createElement("div");
  overlay.id = "kb-overlay";
  overlay.innerHTML = html;
  overlay.onclick = function (e) {
    if (e.target === overlay) hideKbHelp();
  };
  document.body.appendChild(overlay);
  document.getElementById("kb-box").focus();
}

function hideKbHelp() {
  var el = document.getElementById("kb-overlay");
  if (el) el.remove();
  if (_kbPreviousFocus && _kbPreviousFocus.focus) {
    _kbPreviousFocus.focus();
    _kbPreviousFocus = null;
  }
}

document.addEventListener("keydown", function (e) {
  if (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA") return;
  var login = document.getElementById("login-overlay");
  if (login && login.style.display !== "none") return;
  if (e.key === "?" && !e.ctrlKey && !e.metaKey) {
    e.preventDefault();
    showKbHelp();
    return;
  }
  if (e.key === "Escape") {
    var kb = document.getElementById("kb-overlay");
    if (kb) {
      e.preventDefault();
      hideKbHelp();
      return;
    }
  }
});
