/* Admin panel — user & token management for turnstone console */

var _adminTab = "users";
var _adminUsers = [];
var _adminTokenUserId = "";
var _lastCreatedToken = "";
var _cuTrapHandler = null;
var _ctTrapHandler = null;
var _tcTrapHandler = null;

// ---------------------------------------------------------------------------
// View switching (called from app.js showOverview/drillDown pattern)
// ---------------------------------------------------------------------------

function showAdmin() {
  /* global currentView */
  currentView = "admin";
  document.getElementById("view-overview").style.display = "none";
  document.getElementById("view-node").style.display = "none";
  document.getElementById("view-filtered").style.display = "none";
  document.getElementById("view-admin").style.display = "";
  document.getElementById("breadcrumb").style.display = "";
  document.getElementById("breadcrumb-label").textContent = "Admin";
  document.getElementById("main").scrollTop = 0;
  history.pushState({ view: "admin" }, "");
  loadAdminUsers();
}

function switchAdminTab(tab) {
  _adminTab = tab;
  var tabs = document.querySelectorAll(".admin-tab");
  for (var i = 0; i < tabs.length; i++) {
    var isActive = tabs[i].getAttribute("data-tab") === tab;
    tabs[i].classList.toggle("active", isActive);
    tabs[i].setAttribute("aria-selected", isActive ? "true" : "false");
    tabs[i].setAttribute("tabindex", isActive ? "0" : "-1");
  }
  document.getElementById("admin-users").style.display =
    tab === "users" ? "" : "none";
  document.getElementById("admin-tokens").style.display =
    tab === "tokens" ? "" : "none";

  if (tab === "users") loadAdminUsers();
  if (tab === "tokens") _populateTokenUserSelect();
}

// ---------------------------------------------------------------------------
// Users
// ---------------------------------------------------------------------------

function loadAdminUsers() {
  authFetch("/v1/api/admin/users")
    .then(function (r) {
      if (!r.ok) throw new Error("Failed to load users");
      return r.json();
    })
    .then(function (data) {
      _adminUsers = data.users || [];
      _renderUsers(_adminUsers);
      _populateTokenUserSelect();
    })
    .catch(function () {
      document.getElementById("admin-users-table").innerHTML =
        '<div class="dashboard-empty">Failed to load users</div>';
    });
}

function _renderUsers(users) {
  var container = document.getElementById("admin-users-table");
  if (!users.length) {
    container.innerHTML =
      '<div class="dashboard-empty">No users yet. Create one to get started.</div>';
    return;
  }
  var html = "";
  for (var i = 0; i < users.length; i++) {
    var u = users[i];
    html +=
      '<div class="admin-row" role="listitem">' +
      '<span class="admin-col admin-col-username">' +
      escapeHtml(u.username) +
      "</span>" +
      '<span class="admin-col admin-col-name">' +
      escapeHtml(u.display_name) +
      "</span>" +
      '<span class="admin-col admin-col-created">' +
      escapeHtml(u.created || "").slice(0, 10) +
      "</span>" +
      '<span class="admin-col admin-col-actions">' +
      '<button class="admin-btn-danger" data-delete-user="' +
      escapeHtml(u.user_id) +
      '" data-username="' +
      escapeHtml(u.username) +
      '" title="Delete user">delete</button>' +
      "</span>" +
      "</div>";
  }
  container.innerHTML = html;
  // Bind delete buttons via delegation (avoids inline JS injection)
  var btns = container.querySelectorAll("[data-delete-user]");
  for (var j = 0; j < btns.length; j++) {
    btns[j].addEventListener("click", function () {
      confirmDeleteUser(
        this.getAttribute("data-delete-user"),
        this.getAttribute("data-username"),
      );
    });
  }
}

function confirmDeleteUser(userId, username) {
  if (!confirm("Delete user '" + username + "' and all their tokens?")) return;
  authFetch("/v1/api/admin/users/" + encodeURIComponent(userId), {
    method: "DELETE",
  })
    .then(function (r) {
      if (!r.ok) throw new Error("Delete failed");
      showToast("User '" + username + "' deleted");
      loadAdminUsers();
    })
    .catch(function () {
      showToast("Failed to delete user");
    });
}

// ---------------------------------------------------------------------------
// Tokens
// ---------------------------------------------------------------------------

function _populateTokenUserSelect() {
  var sel = document.getElementById("admin-token-user");
  var current = sel.value;
  sel.innerHTML = '<option value="">Select user...</option>';
  for (var i = 0; i < _adminUsers.length; i++) {
    var u = _adminUsers[i];
    var opt = document.createElement("option");
    opt.value = u.user_id;
    opt.textContent = u.username + " (" + u.display_name + ")";
    sel.appendChild(opt);
  }
  if (current) sel.value = current;
}

function loadAdminTokens() {
  var userId = document.getElementById("admin-token-user").value;
  _adminTokenUserId = userId;
  if (!userId) {
    document.getElementById("admin-tokens-table").innerHTML =
      '<div class="dashboard-empty">Select a user to view tokens</div>';
    return;
  }
  authFetch("/v1/api/admin/users/" + encodeURIComponent(userId) + "/tokens")
    .then(function (r) {
      if (!r.ok) throw new Error("Failed to load tokens");
      return r.json();
    })
    .then(function (data) {
      _renderTokens(data.tokens || []);
    })
    .catch(function () {
      document.getElementById("admin-tokens-table").innerHTML =
        '<div class="dashboard-empty">Failed to load tokens</div>';
    });
}

function _renderTokens(tokens) {
  var container = document.getElementById("admin-tokens-table");
  if (!tokens.length) {
    container.innerHTML =
      '<div class="dashboard-empty">No tokens for this user</div>';
    return;
  }
  var html = "";
  for (var i = 0; i < tokens.length; i++) {
    var t = tokens[i];
    var expires = t.expires ? escapeHtml(t.expires).slice(0, 10) : "\u2014";
    html +=
      '<div class="admin-row" role="listitem">' +
      '<span class="admin-col admin-col-prefix"><code>' +
      escapeHtml(t.token_prefix) +
      "\u2026</code></span>" +
      '<span class="admin-col admin-col-tname">' +
      escapeHtml(t.name || "\u2014") +
      "</span>" +
      '<span class="admin-col admin-col-scopes">' +
      _renderScopeBadges(t.scopes) +
      "</span>" +
      '<span class="admin-col admin-col-created">' +
      escapeHtml(t.created || "").slice(0, 10) +
      "</span>" +
      '<span class="admin-col admin-col-expires">' +
      expires +
      "</span>" +
      '<span class="admin-col admin-col-actions">' +
      '<button class="admin-btn-danger" data-revoke-token="' +
      escapeHtml(t.token_id) +
      '" title="Revoke token">revoke</button>' +
      "</span>" +
      "</div>";
  }
  container.innerHTML = html;
  // Bind revoke buttons via delegation (avoids inline JS injection)
  var rbtns = container.querySelectorAll("[data-revoke-token]");
  for (var j = 0; j < rbtns.length; j++) {
    rbtns[j].addEventListener("click", function () {
      confirmRevokeToken(this.getAttribute("data-revoke-token"));
    });
  }
}

function _renderScopeBadges(scopes) {
  if (!scopes) return "";
  var parts = scopes.split(",");
  var html = "";
  for (var i = 0; i < parts.length; i++) {
    var s = parts[i].trim();
    if (!s) continue;
    var cls = "scope-badge";
    if (s === "approve") cls += " scope-approve";
    else if (s === "write") cls += " scope-write";
    html += '<span class="' + cls + '">' + escapeHtml(s) + "</span>";
  }
  return html;
}

function confirmRevokeToken(tokenId) {
  if (!confirm("Revoke this token? This cannot be undone.")) return;
  authFetch("/v1/api/admin/tokens/" + encodeURIComponent(tokenId), {
    method: "DELETE",
  })
    .then(function (r) {
      if (!r.ok) throw new Error("Revoke failed");
      showToast("Token revoked");
      loadAdminTokens();
    })
    .catch(function () {
      showToast("Failed to revoke token");
    });
}

// ---------------------------------------------------------------------------
// Create User Modal
// ---------------------------------------------------------------------------

function showCreateUserModal() {
  var overlay = document.getElementById("create-user-overlay");
  overlay.style.display = "flex";
  document.getElementById("create-user-error").style.display = "none";
  document.getElementById("cu-username").value = "";
  document.getElementById("cu-displayname").value = "";
  document.getElementById("cu-password").value = "";
  document.getElementById("cu-confirm").value = "";
  document.getElementById("cu-submit").disabled = false;
  document.getElementById("cu-submit").textContent = "Create";
  _cuTrapHandler = _installTrap("create-user-overlay", "create-user-box");
  setTimeout(function () {
    document.getElementById("cu-username").focus();
  }, 50);
}

function hideCreateUserModal() {
  document.getElementById("create-user-overlay").style.display = "none";
  _cuTrapHandler = _removeTrap(_cuTrapHandler);
}

function submitCreateUser() {
  var username = (document.getElementById("cu-username").value || "").trim();
  var displayName = (
    document.getElementById("cu-displayname").value || ""
  ).trim();
  var password = document.getElementById("cu-password").value || "";
  var confirm = document.getElementById("cu-confirm").value || "";
  var errEl = document.getElementById("create-user-error");

  if (!username) return _showModalError(errEl, "Username is required");
  if (!displayName) return _showModalError(errEl, "Display name is required");
  if (!password) return _showModalError(errEl, "Password is required");
  if (password.length < 8)
    return _showModalError(errEl, "Password must be at least 8 characters");
  if (password !== confirm)
    return _showModalError(errEl, "Passwords do not match");

  var btn = document.getElementById("cu-submit");
  btn.disabled = true;
  btn.textContent = "Creating\u2026";

  authFetch("/v1/api/admin/users", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      username: username,
      display_name: displayName,
      password: password,
    }),
  })
    .then(function (r) {
      if (r.status === 409) throw new Error("Username already taken");
      if (!r.ok)
        return r.json().then(function (d) {
          throw new Error(d.error || "Failed");
        });
      return r.json();
    })
    .then(function () {
      hideCreateUserModal();
      showToast("User '" + username + "' created");
      loadAdminUsers();
    })
    .catch(function (err) {
      btn.disabled = false;
      btn.textContent = "Create";
      _showModalError(errEl, err.message || "Failed to create user");
    });
}

// ---------------------------------------------------------------------------
// Create Token Modal
// ---------------------------------------------------------------------------

function showCreateTokenModal() {
  if (!_adminTokenUserId) {
    showToast("Select a user first");
    return;
  }
  var overlay = document.getElementById("create-token-overlay");
  overlay.style.display = "flex";
  document.getElementById("create-token-error").style.display = "none";
  document.getElementById("ct-name").value = "";
  document.getElementById("ct-scopes").value = "read,write,approve";
  document.getElementById("ct-expires").value = "";
  document.getElementById("ct-submit").disabled = false;
  document.getElementById("ct-submit").textContent = "Create";
  _ctTrapHandler = _installTrap("create-token-overlay", "create-token-box");
  setTimeout(function () {
    document.getElementById("ct-name").focus();
  }, 50);
}

function hideCreateTokenModal() {
  document.getElementById("create-token-overlay").style.display = "none";
  _ctTrapHandler = _removeTrap(_ctTrapHandler);
}

function submitCreateToken() {
  var name = (document.getElementById("ct-name").value || "").trim();
  var scopes = document.getElementById("ct-scopes").value;
  var expiresDays = document.getElementById("ct-expires").value;
  var errEl = document.getElementById("create-token-error");

  var btn = document.getElementById("ct-submit");
  btn.disabled = true;
  btn.textContent = "Creating\u2026";

  var body = { name: name, scopes: scopes };
  if (expiresDays) body.expires_days = parseInt(expiresDays, 10);

  authFetch(
    "/v1/api/admin/users/" + encodeURIComponent(_adminTokenUserId) + "/tokens",
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    },
  )
    .then(function (r) {
      if (!r.ok)
        return r.json().then(function (d) {
          throw new Error(d.error || "Failed");
        });
      return r.json();
    })
    .then(function (data) {
      hideCreateTokenModal();
      _lastCreatedToken = data.token;
      showTokenCreatedModal(data.token);
      loadAdminTokens();
    })
    .catch(function (err) {
      btn.disabled = false;
      btn.textContent = "Create";
      _showModalError(errEl, err.message || "Failed to create token");
    });
}

// ---------------------------------------------------------------------------
// Token Created Modal (show-once)
// ---------------------------------------------------------------------------

function showTokenCreatedModal(token) {
  document.getElementById("token-created-value").textContent = token;
  document.getElementById("token-created-overlay").style.display = "flex";
  _tcTrapHandler = _installTrap("token-created-overlay", "token-created-box");
}

function hideTokenCreatedModal() {
  document.getElementById("token-created-overlay").style.display = "none";
  _tcTrapHandler = _removeTrap(_tcTrapHandler);
  _lastCreatedToken = "";
}

function copyCreatedToken() {
  if (!_lastCreatedToken) return;
  if (navigator.clipboard) {
    navigator.clipboard.writeText(_lastCreatedToken).then(function () {
      showToast("Token copied to clipboard");
    });
  } else {
    // Fallback: select the text
    var el = document.getElementById("token-created-value");
    var range = document.createRange();
    range.selectNodeContents(el);
    var sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
    showToast("Select and copy the token");
  }
}

// ---------------------------------------------------------------------------
// Modal focus trap + keyboard
// ---------------------------------------------------------------------------

function _modalFocusTrap(boxId) {
  return function (e) {
    if (e.key === "Tab") {
      var box = document.getElementById(boxId);
      if (!box) return;
      var focusable = box.querySelectorAll(
        "input:not([disabled]), select:not([disabled]), button:not([disabled])",
      );
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
}

function _installTrap(overlayId, boxId, trapRef) {
  var overlay = document.getElementById(overlayId);
  if (overlay) {
    overlay.onclick = function (e) {
      if (e.target === overlay) {
        if (overlayId === "create-user-overlay") hideCreateUserModal();
        else if (overlayId === "create-token-overlay") hideCreateTokenModal();
        else if (overlayId === "token-created-overlay") hideTokenCreatedModal();
      }
    };
  }
  document.body.style.overflow = "hidden";
  var handler = _modalFocusTrap(boxId);
  document.addEventListener("keydown", handler);
  return handler;
}

function _removeTrap(handler) {
  if (handler) document.removeEventListener("keydown", handler);
  document.body.style.overflow = "";
  return null;
}

// Global Escape key for admin modals
document.addEventListener("keydown", function (e) {
  if (e.key !== "Escape") return;
  var cu = document.getElementById("create-user-overlay");
  if (cu && cu.style.display !== "none") {
    e.preventDefault();
    hideCreateUserModal();
    return;
  }
  var ct = document.getElementById("create-token-overlay");
  if (ct && ct.style.display !== "none") {
    e.preventDefault();
    hideCreateTokenModal();
    return;
  }
  var tc = document.getElementById("token-created-overlay");
  if (tc && tc.style.display !== "none") {
    e.preventDefault();
    hideTokenCreatedModal();
    return;
  }
});

// Tab arrow key navigation
(function () {
  var tablist = document.querySelector(".admin-tabs");
  if (!tablist) return;
  tablist.addEventListener("keydown", function (e) {
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
    var tabOrder = ["users", "tokens"];
    var idx = tabOrder.indexOf(_adminTab);
    if (e.key === "ArrowRight") idx = (idx + 1) % tabOrder.length;
    else idx = (idx - 1 + tabOrder.length) % tabOrder.length;
    switchAdminTab(tabOrder[idx]);
    var btn = document.querySelector(
      '.admin-tab[data-tab="' + tabOrder[idx] + '"]',
    );
    if (btn) btn.focus();
  });
})();

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function _showModalError(el, msg) {
  el.textContent = msg;
  el.style.display = "block";
}
