/* Shared auth system — turnstone design system
   Configure: window.TURNSTONE_AUTH_TITLE (default "turnstone")
   Hooks: window.onLoginSuccess() and window.onLogout()

   Flows:
   1. Check /v1/api/auth/status — detect if setup is needed
   2. If setup_required → show first-time setup wizard (create admin user)
   3. If auth_enabled + has_users → show login (username:password)
   4. Legacy: token-based login still supported via toggle */

var _AUTH_TITLE = window.TURNSTONE_AUTH_TITLE || "turnstone";
var _loginTrapHandler = null;
var _loginBusy = false;
var _authMode = "login"; // "login", "setup", "token"
var _authUpgradeReload = false;

// Cross-tab auth sync — when one tab logs in/out, others follow.
var _authChannel =
  typeof BroadcastChannel !== "undefined"
    ? new BroadcastChannel("turnstone_auth")
    : null;
if (_authChannel) {
  _authChannel.onmessage = function (e) {
    if (e.data === "login") {
      hideLogin();
      if (typeof window.onLoginSuccess === "function") window.onLoginSuccess();
      _scheduleRefreshFromWhoami();
    } else if (e.data === "logout") {
      _cancelRefreshTimer();
      showLogin();
    } else if (e.data === "refresh") {
      // Another tab refreshed; reschedule based on the new exp so we
      // don't redundantly hit /refresh ourselves.
      _scheduleRefreshFromWhoami();
    }
  };
}

async function authFetch(url, opts) {
  var maxRetries = 2;
  for (var attempt = 0; attempt <= maxRetries; attempt++) {
    var r = await fetch(url, opts);
    if (r.status === 401) {
      try {
        var body = await r.clone().json();
        if (body && body.code === "version_mismatch") {
          _authUpgradeReload = true;
          showLogin("upgrade");
          throw new Error("auth");
        }
      } catch (e) {
        if (e.message === "auth") throw e;
      }
      // Reactive refresh — try once before falling through to login.
      // Covers cases where the proactive refresh timer didn't fire
      // (tab restored from disk-cache after expiry, system clock jump,
      // page first-load with a stale cookie).
      if (attempt === 0 && (await _tryRefresh())) {
        continue; // retry the original request with the new cookie
      }
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
    // Successful auth — ensure logout button and SSE connection
    var _lb = document.getElementById("logout-btn");
    if (_lb) _lb.style.display = "";
    if (typeof _ensureSSE === "function") _ensureSSE();
    return r;
  }
}

// ---------------------------------------------------------------------------
// Cookie refresh — proactive timer + reactive on-401 fallback
// ---------------------------------------------------------------------------

// Pre-emptive refresh fires at REFRESH_AT_FRACTION of remaining cookie life
// (90% by default).  Keeps the cookie fresh ahead of expiry without
// hammering the server for every authFetch.  The reactive _tryRefresh()
// path above covers cases where the timer didn't fire (tab restored from
// disk cache after expiry, system clock jump, etc).
var _REFRESH_AT_FRACTION = 0.9;
// Floor so we don't spin on tiny lifetimes; ceil so very long-lived
// cookies still refresh once a day for permission re-resolution.
var _REFRESH_MIN_DELAY_MS = 30 * 1000;
var _REFRESH_MAX_DELAY_MS = 24 * 60 * 60 * 1000;
var _refreshTimer = null;
var _refreshInFlight = null;
// Logout race guard: a refresh (or whoami) in flight when the user
// clicks Logout can land AFTER /logout and re-populate state, silently
// undoing the logout.  _loggedOut is set synchronously in logout() and
// every fetch's .then bails on its post-fetch effects when it sees the
// flag.  _refreshAbort / _whoamiAbort are the AbortControllers for any
// in-flight /refresh and /whoami respectively.
var _loggedOut = false;
var _refreshAbort = null;
var _whoamiAbort = null;

// Permissions-ready: one-shot promise resolved after the initial whoami
// completes (success OR failure).  Lets permission-gated UI await the
// "have we tried to populate sessionStorage yet?" signal instead of
// guessing a setTimeout duration.  Subsequent logins/logouts refresh
// permissions through the existing onLoginSuccess / onLogout hooks, so
// one-shot is sufficient for the page-load gate problem.
var _permissionsReadyResolve = null;
var _permissionsReady = new Promise(function (resolve) {
  _permissionsReadyResolve = resolve;
});
function _markPermissionsReady() {
  if (_permissionsReadyResolve) {
    var r = _permissionsReadyResolve;
    _permissionsReadyResolve = null;
    r();
  }
}
if (typeof window !== "undefined") {
  window.permissionsReady = _permissionsReady;
}

async function _tryRefresh() {
  // Don't start a refresh if logout already won the race.
  if (_loggedOut) return false;
  // De-dupe concurrent callers — many parallel authFetch'es hitting
  // 401 at once should still only fire one /refresh request.
  if (_refreshInFlight) {
    try {
      return await _refreshInFlight;
    } catch (_e) {
      return false;
    }
  }
  _refreshAbort =
    typeof AbortController !== "undefined" ? new AbortController() : null;
  _refreshInFlight = (async function () {
    try {
      var r = await fetch("/v1/api/auth/refresh", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        signal: _refreshAbort ? _refreshAbort.signal : undefined,
      });
      if (!r.ok) return false;
      var data = null;
      try {
        data = await r.json();
      } catch (_e) {
        /* refresh succeeded but body unreadable — fall back to whoami */
      }
      // If logout fired while the refresh was in flight, drop all
      // post-fetch effects: don't store perms (already cleared), don't
      // reschedule a timer, don't broadcast (would re-arm sibling tabs).
      // The new cookie is harmless — logout's clear-cookie response
      // already overwrote it on the way back from this fetch.
      if (_loggedOut) return false;
      // Consume the refresh response inline.  /refresh returns the same
      // permissions + exp shape as /whoami (auth.py:handle_auth_refresh),
      // so we can populate sessionStorage and reschedule the next refresh
      // off this single round-trip — no follow-up whoami needed.  The
      // caller (authFetch retry loop, refresh timer) sees fresh
      // sessionStorage immediately on resume, so a permission re-resolve
      // server-side is reflected in the UI on the very next
      // permission-gated check.
      if (data) {
        _storePermissions(data);
        if (typeof data.exp === "number") {
          _scheduleRefreshAt(data.exp);
        } else {
          // Server didn't surface exp (older deployment) — fall back to
          // the whoami round-trip so we still reschedule.
          _scheduleRefreshFromWhoami();
        }
      } else {
        _scheduleRefreshFromWhoami();
      }
      if (_authChannel) _authChannel.postMessage("refresh");
      return true;
    } catch (_e) {
      return false;
    } finally {
      _refreshInFlight = null;
      _refreshAbort = null;
    }
  })();
  return await _refreshInFlight;
}

function _scheduleRefreshAt(epochSeconds) {
  if (_refreshTimer) {
    clearTimeout(_refreshTimer);
    _refreshTimer = null;
  }
  if (typeof epochSeconds !== "number" || !isFinite(epochSeconds)) return;
  var nowMs = Date.now();
  var expMs = epochSeconds * 1000;
  var remaining = expMs - nowMs;
  if (remaining <= 0) return; // already expired; reactive path handles it
  var delay = Math.floor(remaining * _REFRESH_AT_FRACTION);
  if (delay < _REFRESH_MIN_DELAY_MS) delay = _REFRESH_MIN_DELAY_MS;
  if (delay > _REFRESH_MAX_DELAY_MS) delay = _REFRESH_MAX_DELAY_MS;
  _refreshTimer = setTimeout(function () {
    _refreshTimer = null;
    _tryRefresh();
  }, delay);
}

function _scheduleRefreshFromWhoami() {
  // Best-effort — failure here just means no proactive refresh; the
  // reactive on-401 path still works.  Uses fetch (not authFetch) to
  // avoid recursion through the on-401 trap.
  //
  // Also rehydrates sessionStorage permissions when the cookie outlives
  // the tab session: an existing valid cookie means the user is still
  // authenticated, but sessionStorage was cleared on tab close, so
  // permission-gated UI (e.g. the coordinator composer) hides until the
  // user manually logs out + back in.  Populating from whoami here keeps
  // the gate consistent with the actual auth state.
  //
  // Logout race: if logout() fires while this fetch is in flight, the
  // .then must NOT call _storePermissions (would re-populate after
  // logout cleared) or _scheduleRefreshAt (would re-arm the timer).
  // _loggedOut guards the post-fetch effects; _whoamiAbort lets logout
  // cancel the in-flight request directly so the network roundtrip
  // doesn't even complete.
  //
  // Same-call superseding: this function is invoked from several places
  // (initial page load, _onSuccess, BroadcastChannel "login"/"refresh",
  // _tryRefresh fallback).  Two of those firing in quick succession —
  // e.g. an old slow whoami still in flight when login completes and
  // schedules a fresh one — could let the older response land last and
  // clobber the newer one's effects (clearing permissions right after a
  // successful login, or rescheduling off a stale exp).  We abort the
  // prior in-flight whoami before starting a new one AND guard the
  // post-fetch effects with `_whoamiAbort === ctrl` so a late arrival
  // from a superseded call is fully neutralised.
  var prior = _whoamiAbort;
  if (prior) {
    try {
      prior.abort();
    } catch (_e) {
      /* AbortController not available; the equality check below covers it */
    }
  }
  var ctrl =
    typeof AbortController !== "undefined" ? new AbortController() : null;
  _whoamiAbort = ctrl;
  fetch("/v1/api/auth/whoami", {
    credentials: "same-origin",
    signal: ctrl ? ctrl.signal : undefined,
  })
    .then(function (r) {
      return r.ok ? r.json() : null;
    })
    .then(function (data) {
      if (_loggedOut) return;
      // Bail if a newer call has superseded us — its eventual effects
      // are the authoritative ones; ours would only clobber.
      if (_whoamiAbort !== ctrl) return;
      // Treat a non-OK whoami (data === null) as an explicit clear.
      // _storePermissions(null) removes the key so stale UI gating
      // disappears when the server-side identity is gone (user
      // deleted, role stripped, token revoked between tab close and
      // restore).  Per the documented threat model these permissions
      // are cosmetic — but a stale "you're an admin" badge after
      // server-side revocation is exactly the kind of thing that
      // surprises an operator at the wrong moment.
      _storePermissions(data);
      if (data && typeof data.exp === "number") {
        _scheduleRefreshAt(data.exp);
      }
    })
    .catch(function () {
      /* silent — proactive refresh is optional, abort is expected */
    })
    .finally(function () {
      // Only clear if still ours — a newer call may have replaced it.
      if (_whoamiAbort === ctrl) _whoamiAbort = null;
      _markPermissionsReady();
    });
}

function _cancelRefreshTimer() {
  if (_refreshTimer) {
    clearTimeout(_refreshTimer);
    _refreshTimer = null;
  }
}

function initLogin() {
  var overlay = document.createElement("div");
  overlay.id = "login-overlay";
  overlay.style.display = "none";
  overlay.setAttribute("role", "dialog");
  overlay.setAttribute("aria-modal", "true");
  overlay.setAttribute("aria-labelledby", "login-title");
  overlay.innerHTML = _buildLoginHTML();
  document.body.appendChild(overlay);
  _bindLoginEvents();

  // OIDC callback: detect success or error from URL params
  var _oidcParams = new URLSearchParams(window.location.search);
  var _oidcError = _oidcParams.get("oidc_error");
  if (_oidcError) {
    showLogin();
    history.replaceState({}, "", window.location.pathname);
    // Defer: showLogin() triggers async status fetch → _switchMode() → _clearError().
    // Display after that settles.
    var _pendingOidcError = _oidcError;
    setTimeout(function () {
      _showError(_pendingOidcError);
    }, 300);
  } else if (_oidcParams.get("oidc_success")) {
    history.replaceState({}, "", window.location.pathname);
    // Fetch permissions before completing login (cookie is already set)
    fetch("/v1/api/auth/whoami")
      .then(function (r) {
        return r.ok ? r.json() : {};
      })
      .then(function (data) {
        _storePermissions(data);
        _onSuccess();
      })
      .catch(function () {
        _onSuccess(); // Proceed even if permissions fetch fails
      });
  }
}

function _buildLoginHTML() {
  return (
    '<form id="login-box" aria-describedby="login-subtitle">' +
    '<h2 id="login-title">' +
    escapeHtml(_AUTH_TITLE) +
    "</h2>" +
    '<div id="login-subtitle" class="login-subtitle"></div>' +
    '<div id="login-error" role="alert" aria-live="assertive"></div>' +
    // --- OIDC SSO button ---
    '<div id="oidc-section" style="display:none">' +
    '<button id="oidc-btn" class="oidc-btn" type="button">Continue with SSO</button>' +
    '<div id="oidc-divider" class="oidc-divider"><span>or</span></div>' +
    "</div>" +
    // --- Setup mode fields ---
    '<div id="setup-fields" style="display:none">' +
    '<label for="setup-username" class="login-label">Username</label>' +
    '<input id="setup-username" name="username" type="text" placeholder="admin" autocomplete="username" spellcheck="false">' +
    '<label for="setup-displayname" class="login-label">Display name</label>' +
    '<input id="setup-displayname" name="display_name" type="text" placeholder="Administrator" autocomplete="name">' +
    '<label for="setup-password" class="login-label">Password</label>' +
    '<input id="setup-password" name="password" type="password" placeholder="Choose a strong password" autocomplete="new-password">' +
    '<label for="setup-confirm" class="login-label">Confirm password</label>' +
    '<input id="setup-confirm" name="confirm" type="password" placeholder="Confirm password" autocomplete="new-password">' +
    "</div>" +
    // --- Login mode fields ---
    '<div id="login-fields">' +
    '<label for="login-username" class="login-label">Username</label>' +
    '<input id="login-username" name="username" type="text" placeholder="Username" autocomplete="username" spellcheck="false">' +
    '<label for="login-password" class="login-label">Password</label>' +
    '<input id="login-password" name="password" type="password" placeholder="Password" autocomplete="current-password">' +
    "</div>" +
    // --- Token mode fields ---
    '<div id="token-fields" style="display:none">' +
    '<label for="login-token" class="login-label">Auth token</label>' +
    '<input id="login-token" name="token" type="password" placeholder="Enter auth token" autocomplete="off">' +
    "</div>" +
    '<button id="login-submit" type="submit">Sign in</button>' +
    // --- Mode toggle ---
    '<div id="login-toggle" class="login-toggle">' +
    '<button id="toggle-token" class="login-link" type="button">Use token instead</button>' +
    "</div>" +
    "</form>"
  );
}

function _bindLoginEvents() {
  // Handle form submission (button click, Enter key, and password manager fill)
  document.getElementById("login-box").addEventListener("submit", function (e) {
    e.preventDefault();
    _handleSubmit();
  });

  // Escape key clears errors
  var inputs = document.querySelectorAll("#login-box input");
  for (var i = 0; i < inputs.length; i++) {
    inputs[i].addEventListener("keydown", function (e) {
      if (e.key === "Escape") _clearError();
    });
  }

  // Mode toggle
  document.getElementById("toggle-token").onclick = function () {
    if (_authMode === "login") {
      _switchMode("token");
    } else if (_authMode === "token") {
      _switchMode("login");
    }
  };
}

function _switchMode(mode) {
  _authMode = mode;
  var setupFields = document.getElementById("setup-fields");
  var loginFields = document.getElementById("login-fields");
  var tokenFields = document.getElementById("token-fields");
  var toggleDiv = document.getElementById("login-toggle");
  var toggleBtn = document.getElementById("toggle-token");
  var subtitle = document.getElementById("login-subtitle");
  var btn = document.getElementById("login-submit");

  setupFields.style.display = "none";
  loginFields.style.display = "none";
  tokenFields.style.display = "none";
  _clearError();

  if (mode === "setup") {
    setupFields.style.display = "";
    toggleDiv.style.display = "none";
    subtitle.textContent = "Create the first admin account";
    btn.textContent = "Create account";
    setTimeout(function () {
      document.getElementById("setup-username").focus();
    }, 50);
  } else if (mode === "login") {
    loginFields.style.display = "";
    toggleDiv.style.display = "";
    toggleBtn.textContent = "Use token instead";
    subtitle.textContent = "";
    btn.textContent = "Sign in";
    setTimeout(function () {
      document.getElementById("login-username").focus();
    }, 50);
  } else if (mode === "token") {
    tokenFields.style.display = "";
    toggleDiv.style.display = "";
    toggleBtn.textContent = "Use password instead";
    subtitle.textContent = "";
    btn.textContent = "Sign in";
    setTimeout(function () {
      document.getElementById("login-token").focus();
    }, 50);
  }
}

function _updateOIDCUI(data) {
  var section = document.getElementById("oidc-section");
  var btn = document.getElementById("oidc-btn");
  var divider = document.getElementById("oidc-divider");
  if (!section) return;

  if (!data.oidc_enabled || _authMode === "setup") {
    section.style.display = "none";
    return;
  }

  section.style.display = "";
  btn.textContent = "Continue with " + (data.oidc_provider_name || "SSO");
  btn.onclick = function () {
    window.location.href = "/v1/api/auth/oidc/authorize";
  };

  if (data.password_enabled === false) {
    document.getElementById("login-fields").style.display = "none";
    document.getElementById("login-toggle").style.display = "none";
    document.getElementById("login-submit").style.display = "none";
    divider.style.display = "none";
  }
}

function _clearError() {
  var errEl = document.getElementById("login-error");
  if (errEl && errEl.style.display !== "none") {
    errEl.style.display = "none";
    errEl.textContent = "";
  }
}

function _showError(msg) {
  var errEl = document.getElementById("login-error");
  if (errEl) {
    errEl.textContent = msg;
    errEl.style.display = "block";
  }
}

function showLogin(reason) {
  var overlay = document.getElementById("login-overlay");
  if (!overlay) return;
  overlay.style.display = "flex";
  document.body.style.overflow = "hidden";
  var logoutBtn = document.getElementById("logout-btn");
  if (logoutBtn) logoutBtn.style.display = "none";
  _clearError();

  // Check auth status to determine mode
  var _loginReason = reason;
  fetch("/v1/api/auth/status")
    .then(function (r) {
      return r.json();
    })
    .then(function (data) {
      if (data.setup_required) {
        _switchMode("setup");
      } else {
        _switchMode("login");
        if (_loginReason === "upgrade") {
          var subtitle = document.getElementById("login-subtitle");
          if (subtitle)
            subtitle.textContent =
              "The server was updated \u2014 please sign in again";
        }
      }
      _updateOIDCUI(data);
    })
    .catch(function () {
      // Fallback to login mode
      _switchMode("login");
    });

  // Keyboard trap
  if (_loginTrapHandler)
    document.removeEventListener("keydown", _loginTrapHandler);
  _loginTrapHandler = function (e) {
    if (e.key === "Tab") {
      var box = document.getElementById("login-box");
      var focusable = box.querySelectorAll(
        'input:not([style*="display: none"]):not([style*="display:none"]), button:not([style*="display: none"]):not([style*="display:none"])',
      );
      // Filter to visible elements
      var visible = [];
      for (var i = 0; i < focusable.length; i++) {
        if (focusable[i].offsetParent !== null) visible.push(focusable[i]);
      }
      if (visible.length === 0) return;
      var first = visible[0];
      var last = visible[visible.length - 1];
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

function _handleSubmit() {
  if (_loginBusy) return;
  if (_authMode === "setup") return _submitSetup();
  if (_authMode === "token") return _submitToken();
  return _submitLogin();
}

function _submitLogin() {
  var username = (document.getElementById("login-username").value || "").trim();
  var password = document.getElementById("login-password").value || "";

  if (!username) {
    _showError("Username is required");
    return;
  }
  if (!password) {
    _showError("Password is required");
    return;
  }

  _setBusy(true);
  fetch("/v1/api/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username: username, password: password }),
  })
    .then(function (r) {
      if (r.status === 401 || r.status === 403) throw new Error("invalid");
      if (!r.ok) throw new Error("server");
      return r.json();
    })
    .then(function (data) {
      _setBusy(false);
      _storePermissions(data);
      _onSuccess();
    })
    .catch(function (err) {
      _setBusy(false);
      _showError(
        err.message === "invalid"
          ? "Invalid username or password"
          : "Connection failed \u2014 try again",
      );
    });
}

function _submitToken() {
  var token = (document.getElementById("login-token").value || "").trim();
  if (!token) {
    _showError("Token is required");
    return;
  }

  _setBusy(true);
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
    .then(function (data) {
      _setBusy(false);
      _storePermissions(data);
      _onSuccess();
    })
    .catch(function (err) {
      _setBusy(false);
      _showError(
        err.message === "invalid"
          ? "Invalid token"
          : "Connection failed \u2014 try again",
      );
    });
}

function _submitSetup() {
  var username = (document.getElementById("setup-username").value || "").trim();
  var displayName = (
    document.getElementById("setup-displayname").value || ""
  ).trim();
  var password = document.getElementById("setup-password").value || "";
  var confirm = document.getElementById("setup-confirm").value || "";

  if (!username) {
    _showError("Username is required");
    return;
  }
  if (!displayName) {
    _showError("Display name is required");
    return;
  }
  if (!password) {
    _showError("Password is required");
    return;
  }
  if (password.length < 8) {
    _showError("Password must be at least 8 characters");
    return;
  }
  if (password !== confirm) {
    _showError("Passwords do not match");
    return;
  }

  _setBusy(true, "Creating account\u2026");

  // Use the public setup endpoint (creates user + returns JWT in one step)
  fetch("/v1/api/auth/setup", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      username: username,
      display_name: displayName,
      password: password,
    }),
  })
    .then(function (r) {
      if (r.status === 409) throw new Error("Setup already completed");
      if (!r.ok)
        return r.json().then(function (d) {
          throw new Error(d.error || "Failed to create account");
        });
      return r.json();
    })
    .then(function (data) {
      _setBusy(false);
      _storePermissions(data);
      _onSuccess();
    })
    .catch(function (err) {
      _setBusy(false);
      _showError(err.message || "Setup failed \u2014 try again");
    });
}

function _storePermissions(data) {
  if (data && data.permissions) {
    sessionStorage.setItem("turnstone_permissions", data.permissions);
  } else {
    sessionStorage.removeItem("turnstone_permissions");
  }
}

function _setBusy(busy, label) {
  _loginBusy = busy;
  var btn = document.getElementById("login-submit");
  var inputs = document.querySelectorAll("#login-box input");
  btn.disabled = busy;
  if (busy) {
    btn.textContent = label || "Signing in\u2026";
  } else {
    btn.textContent = _authMode === "setup" ? "Create account" : "Sign in";
  }
  for (var i = 0; i < inputs.length; i++) {
    inputs[i].disabled = busy;
  }
}

function _onSuccess() {
  // After a version-triggered re-auth, reload the page to pick up fresh
  // JS/CSS via the updated ?v= query strings in the new HTML.
  if (_authUpgradeReload) {
    _authUpgradeReload = false;
    window.location.reload();
    return;
  }
  // Successful re-login clears the logged-out latch so subsequent
  // refreshes work again.
  _loggedOut = false;
  hideLogin();
  var logoutBtn = document.getElementById("logout-btn");
  if (logoutBtn) logoutBtn.style.display = "";
  if (_authChannel) _authChannel.postMessage("login");
  if (typeof window.onLoginSuccess === "function") window.onLoginSuccess();
  // Schedule pre-emptive cookie refresh based on the new token's exp.
  _scheduleRefreshFromWhoami();
}

function logout() {
  // Set flag + abort in-flight fetches BEFORE the network call so any
  // concurrent _tryRefresh() / _scheduleRefreshFromWhoami() bails its
  // post-fetch effects (see _loggedOut handling above).  Without this,
  // a refresh or whoami already in flight can land after /logout and
  // re-set the cookie or re-populate sessionStorage permissions.
  _loggedOut = true;
  _cancelRefreshTimer();
  [_refreshAbort, _whoamiAbort].forEach(function (ctrl) {
    if (!ctrl) return;
    try {
      ctrl.abort();
    } catch (_e) {
      /* AbortController not available; the _loggedOut flag handles it */
    }
  });
  fetch("/v1/api/auth/logout", { method: "POST" }).then(function () {
    sessionStorage.removeItem("turnstone_permissions");
    if (_authChannel) _authChannel.postMessage("logout");
    if (typeof window.onLogout === "function") window.onLogout();
    showLogin();
  });
}

// Schedule on initial load if already authenticated.  The whoami
// request silently fails if the cookie is missing or expired, which
// is the right behaviour — the first authFetch will trigger the
// login overlay via the existing on-401 path.
if (typeof window !== "undefined") {
  _scheduleRefreshFromWhoami();
}
