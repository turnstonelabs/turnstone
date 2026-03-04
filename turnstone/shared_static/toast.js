/* Shared toast notification — turnstone design system
   Configure timeout via window.TURNSTONE_TOAST_TIMEOUT (default 3000ms) */

var _toastQueue = [];
var _toastTimer = null;
var _toastShowing = false;
var _TOAST_TIMEOUT = window.TURNSTONE_TOAST_TIMEOUT || 3000;

function showToast(message) {
  var el = document.getElementById("toast");
  if (!el) return;
  if (_toastShowing) {
    _toastQueue.push(message);
    return;
  }
  _displayToast(el, message);
}

function _displayToast(el, message) {
  el.textContent = message;
  el.classList.add("show");
  _toastShowing = true;
  if (_toastTimer) clearTimeout(_toastTimer);
  _toastTimer = setTimeout(function () {
    el.classList.remove("show");
    _toastShowing = false;
    _toastTimer = null;
    if (_toastQueue.length) {
      setTimeout(function () {
        _displayToast(el, _toastQueue.shift());
      }, 300);
    }
  }, _TOAST_TIMEOUT);
}
