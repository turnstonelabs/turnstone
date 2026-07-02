/* Shared toast notification — turnstone design system
   Configure timeout via window.TURNSTONE_TOAST_TIMEOUT (default 3000ms).
   ES module, dependency-free; window bridge below for classic consumers. */

const _toastQueue = [];
let _toastTimer = null;
let _toastShowing = false;
const _TOAST_TIMEOUT = window.TURNSTONE_TOAST_TIMEOUT || 3000;

export function showToast(message, type) {
  const el = document.getElementById("toast");
  if (!el) return;
  if (_toastShowing) {
    // Coalesce + cap: the queue drains at one toast per ~3.3s, so any
    // sustained source (verdict toasts during an auto-approved tool storm)
    // would otherwise grow it for the rest of the session and keep
    // surfacing hours-stale notices.  Identical consecutive messages
    // collapse; beyond the cap the OLDEST queued toast drops (newest wins —
    // it reflects current state).
    const last = _toastQueue[_toastQueue.length - 1];
    if (last && last.message === message && last.type === type) return;
    if (_toastQueue.length >= 5) _toastQueue.shift();
    _toastQueue.push({ message: message, type: type });
    return;
  }
  _displayToast(el, message, type);
}

function _displayToast(el, message, type) {
  el.textContent = message;
  el.classList.remove("toast-error");
  if (type === "error") el.classList.add("toast-error");
  // A document-modal <dialog> owns the top layer, which stacks above every
  // z-index — a toast fired while one is open (e.g. "Token copied" over the
  // token-created dialog) would render underneath. Promote to a manual
  // popover ONLY for that case: popovers join the top layer above the open
  // dialog, while the everyday path keeps the fade transition (a persistent
  // popover attribute would impose UA display:none and kill it).
  if ("showPopover" in el && document.querySelector("dialog:modal")) {
    el.popover = "manual";
    try {
      el.showPopover();
    } catch (e) {
      /* already showing */
    }
  }
  el.classList.add("show");
  _toastShowing = true;
  if (_toastTimer) clearTimeout(_toastTimer);
  _toastTimer = setTimeout(function () {
    el.classList.remove("show");
    if (el.popover) {
      try {
        el.hidePopover();
      } catch (e) {
        /* already hidden */
      }
      el.removeAttribute("popover"); // restore the classic fade path
    }
    _toastShowing = false;
    _toastTimer = null;
    if (_toastQueue.length) {
      setTimeout(function () {
        const item = _toastQueue.shift();
        _displayToast(el, item.message, item.type);
      }, 300);
    }
  }, _TOAST_TIMEOUT);
}

// --- Legacy window bridge ---------------------------------------------------
// Still-classic consumers reach this as a global at event/boot time (after
// this deferred module evaluated).  New module code imports instead.
window.showToast = showToast;
