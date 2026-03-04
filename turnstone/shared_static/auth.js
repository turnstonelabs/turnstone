/* Shared auth system — turnstone design system
   Configure: window.TURNSTONE_AUTH_TITLE (default "turnstone")
   Hooks: window.onLoginSuccess() and window.onLogout() */

var _AUTH_TITLE = window.TURNSTONE_AUTH_TITLE || "turnstone";
var _loginTrapHandler = null;
var _loginBusy = false;

async function authFetch(url, opts) {
  var maxRetries = 2;
  for (var attempt = 0; attempt <= maxRetries; attempt++) {
    var r = await fetch(url, opts);
    if (r.status === 401) {
      showLogin();
      throw new Error("auth");
    }
    if (r.status === 429 && attempt < maxRetries) {
      var retryAfter = parseInt(r.headers.get("Retry-After") || "1", 10);
      showToast("Rate limited \u2014 retrying in " + retryAfter + "s");
      await new Promise(function (resolve) {
        setTimeout(resolve, retryAfter * 1000);
      });
      continue;
    }
    return r;
  }
}

function initLogin() {
  var overlay = document.createElement("div");
  overlay.id = "login-overlay";
  overlay.style.display = "none";
  overlay.setAttribute("role", "dialog");
  overlay.setAttribute("aria-modal", "true");
  overlay.setAttribute("aria-labelledby", "login-title");
  overlay.innerHTML =
    '<div id="login-box">' +
    '<h2 id="login-title">' +
    escapeHtml(_AUTH_TITLE) +
    "</h2>" +
    '<div id="login-error" role="alert" aria-live="assertive"></div>' +
    '<label for="login-token" class="sr-only">Auth token</label>' +
    '<input id="login-token" type="password" placeholder="Enter auth token" autocomplete="off">' +
    '<button id="login-submit">Sign in</button>' +
    "</div>";
  document.body.appendChild(overlay);
  document.getElementById("login-submit").onclick = submitLogin;
  document
    .getElementById("login-token")
    .addEventListener("keydown", function (e) {
      if (e.key === "Enter") submitLogin();
      if (e.key === "Escape") {
        var errEl = document.getElementById("login-error");
        if (errEl && errEl.style.display !== "none") {
          errEl.style.display = "none";
          errEl.textContent = "";
        }
      }
    });
}

function showLogin() {
  var overlay = document.getElementById("login-overlay");
  if (!overlay) return;
  overlay.style.display = "flex";
  document.body.style.overflow = "hidden";
  var logoutBtn = document.getElementById("logout-btn");
  if (logoutBtn) logoutBtn.style.display = "none";
  var errEl = document.getElementById("login-error");
  if (errEl) {
    errEl.style.display = "none";
    errEl.textContent = "";
  }
  setTimeout(function () {
    var inp = document.getElementById("login-token");
    if (inp) {
      inp.value = "";
      inp.focus();
    }
  }, 50);
  if (_loginTrapHandler)
    document.removeEventListener("keydown", _loginTrapHandler);
  _loginTrapHandler = function (e) {
    if (e.key === "Tab") {
      var box = document.getElementById("login-box");
      var focusable = box.querySelectorAll("input, button");
      var first = focusable[0];
      var last = focusable[focusable.length - 1];
      if (e.shiftKey) {
        if (document.activeElement === first) {
          e.preventDefault();
          last.focus();
        }
      } else {
        if (document.activeElement === last) {
          e.preventDefault();
          first.focus();
        }
      }
    }
  };
  document.addEventListener("keydown", _loginTrapHandler);
}

function hideLogin() {
  var overlay = document.getElementById("login-overlay");
  if (overlay) overlay.style.display = "none";
  document.body.style.overflow = "";
  if (_loginTrapHandler) {
    document.removeEventListener("keydown", _loginTrapHandler);
    _loginTrapHandler = null;
  }
}

function submitLogin() {
  if (_loginBusy) return;
  var token = (document.getElementById("login-token").value || "").trim();
  if (!token) {
    var errEl = document.getElementById("login-error");
    if (errEl) {
      errEl.textContent = "Token is required";
      errEl.style.display = "block";
    }
    document.getElementById("login-token").focus();
    return;
  }

  _loginBusy = true;
  var btn = document.getElementById("login-submit");
  var inp = document.getElementById("login-token");
  btn.disabled = true;
  btn.textContent = "Signing in\u2026";
  inp.disabled = true;

  fetch("/v1/api/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ token: token }),
  })
    .then(function (r) {
      if (r.status === 401 || r.status === 403) throw new Error("invalid");
      if (!r.ok) throw new Error("server");
      return r.json();
    })
    .then(function () {
      _loginBusy = false;
      btn.disabled = false;
      btn.textContent = "Sign in";
      inp.disabled = false;
      hideLogin();
      var logoutBtn = document.getElementById("logout-btn");
      if (logoutBtn) logoutBtn.style.display = "";
      if (typeof window.onLoginSuccess === "function") window.onLoginSuccess();
    })
    .catch(function (err) {
      _loginBusy = false;
      btn.disabled = false;
      btn.textContent = "Sign in";
      inp.disabled = false;
      var errEl = document.getElementById("login-error");
      if (errEl) {
        errEl.textContent =
          err.message === "invalid"
            ? "Invalid token"
            : "Connection failed \u2014 try again";
        errEl.style.display = "block";
      }
    });
}

function logout() {
  fetch("/v1/api/auth/logout", { method: "POST" }).then(function () {
    if (typeof window.onLogout === "function") window.onLogout();
    showLogin();
  });
}
