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
    _toastQueue.push({ message: message, type: type });
    return;
  }
  _displayToast(el, message, type);
}

function _displayToast(el, message, type) {
  el.textContent = message;
  el.classList.remove("toast-error");
  if (type === "error") el.classList.add("toast-error");
  el.classList.add("show");
  _toastShowing = true;
  if (_toastTimer) clearTimeout(_toastTimer);
  _toastTimer = setTimeout(function () {
    el.classList.remove("show");
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
