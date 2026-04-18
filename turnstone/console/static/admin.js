/* Admin panel — user & token management for turnstone console */

var _adminTab = "users";
var _adminUsers = [];
var _adminTokenUserId = "";
var _adminChannelUserId = "";
var _lastCreatedToken = "";
var _cuTrapHandler = null;
var _ctTrapHandler = null;
var _tcTrapHandler = null;
var _ccTrapHandler = null;
var _cfTrapHandler = null;
var _adminWatches = [];
var _confirmCallbackFn = null;
var _confirmTriggerEl = null;
var _mobileSidebarOpen = false;

// Settings whose choices are populated dynamically from the live model
// alias list, and whose empty-string option renders as "(server default)".
var ALIAS_SETTING_KEYS = [
  "model.default_alias",
  "model.plan_alias",
  "model.task_alias",
  "channels.default_model_alias",
];

// Settings whose empty option means "inherit from a fallback chain", as
// opposed to "no value" — distinct from the literal "none" choice (e.g.
// reasoning_effort="none" actually disables reasoning, very different
// from leaving it unset).
var INHERIT_EMPTY_LABEL_KEYS = ["model.plan_effort", "model.task_effort"];

// ---------------------------------------------------------------------------
// View switching (called from app.js showOverview/drillDown pattern)
// ---------------------------------------------------------------------------

function showAdmin() {
  /* global currentView, showHome */
  // Toggle: if already in admin view, go back to the home landing
  if (currentView === "admin") {
    var adminBtn = document.getElementById("admin-btn");
    if (adminBtn) {
      adminBtn.classList.remove("active");
      adminBtn.setAttribute("aria-expanded", "false");
    }
    showHome();
    return;
  }

  currentView = "admin";
  if (typeof _stopActiveCoordsPolling === "function") {
    _stopActiveCoordsPolling();
  }
  var homeView = document.getElementById("view-home");
  if (homeView) homeView.style.display = "none";
  document.getElementById("view-node").style.display = "none";
  document.getElementById("view-filtered").style.display = "none";
  document.getElementById("view-admin").style.display = "";
  document.getElementById("breadcrumb").style.display = "";
  document.getElementById("breadcrumb-label").textContent = "Admin";
  document.getElementById("main").scrollTop = 0;

  // Highlight admin button as active
  var adminBtn = document.getElementById("admin-btn");
  if (adminBtn) {
    adminBtn.classList.add("active");
    adminBtn.setAttribute("aria-expanded", "true");
  }
  history.pushState({ view: "admin" }, "");

  // Permission gating: hide nav items the user cannot access
  var perms = sessionStorage.getItem("turnstone_permissions") || "";
  var tabPerms = {
    users: "admin.users",
    tokens: "admin.users",
    channels: "admin.users",
    schedules: "admin.schedules",
    watches: "admin.watches",
    roles: "admin.roles",
    policies: "admin.policies",
    "prompt-policies": "admin.prompt_policies",
    judge: "admin.judge",
    skills: "admin.skills",
    usage: "admin.usage",
    audit: "admin.audit",
    memories: "admin.memories",
    settings: "admin.settings",
    tls: "admin.settings",
    mcp: "admin.mcp",
    models: "admin.models",
  };
  if (perms) {
    var permSet = perms.split(",");
    var navItems = document.querySelectorAll(".admin-nav");
    for (var i = 0; i < navItems.length; i++) {
      var tabName = navItems[i].getAttribute("data-tab");
      var needed = tabPerms[tabName];
      if (needed && permSet.indexOf(needed) < 0) {
        navItems[i].style.display = "none";
      } else {
        navItems[i].style.display = "";
      }
    }
  }

  // Hide groups where all children are permission-hidden
  var groups = document.querySelectorAll(".admin-sidebar-group");
  for (var g = 0; g < groups.length; g++) {
    var visibleInGroup = groups[g].querySelectorAll(
      '.admin-nav:not([style*="display: none"])',
    );
    groups[g].style.display = visibleInGroup.length > 0 ? "" : "none";
  }

  // Mobile: ensure sidebar starts hidden + inert; desktop: ensure it's accessible
  var sidebar = document.getElementById("admin-sidebar");

  // Inject close header for mobile drawer (once)
  if (!document.getElementById("admin-sidebar-close")) {
    var closeHeader = document.createElement("div");
    closeHeader.id = "admin-sidebar-close";
    closeHeader.className = "admin-sidebar-close";
    var label = document.createElement("span");
    label.textContent = "Navigation";
    var closeBtn = document.createElement("button");
    closeBtn.setAttribute("aria-label", "Close navigation");
    closeBtn.textContent = "\u00d7";
    closeBtn.addEventListener("click", function () {
      if (_mobileSidebarOpen) {
        _toggleMobileSidebar();
        var mt = document.getElementById("admin-mobile-toggle");
        if (mt) mt.focus();
      }
    });
    closeHeader.appendChild(label);
    closeHeader.appendChild(closeBtn);
    sidebar.insertBefore(closeHeader, sidebar.firstChild);
  }

  if (window.innerWidth <= 700) {
    _mobileSidebarOpen = false;
    sidebar.classList.add("collapsed");
    sidebar.classList.remove("open");
    sidebar.setAttribute("aria-hidden", "true");
    sidebar.setAttribute("inert", "");
  } else {
    sidebar.removeAttribute("aria-hidden");
    sidebar.removeAttribute("inert");
  }

  // Mobile backdrop listener (idempotent)
  var backdrop = document.getElementById("admin-sidebar-backdrop");
  if (backdrop && !backdrop._listenerAttached) {
    backdrop.addEventListener("click", function () {
      if (_mobileSidebarOpen) {
        _toggleMobileSidebar();
        var mt = document.getElementById("admin-mobile-toggle");
        if (mt) mt.focus();
      }
    });
    backdrop._listenerAttached = true;
  }

  // Switch to the first visible nav item
  var visibleNavs = document.querySelectorAll(
    '.admin-nav:not([style*="display: none"])',
  );
  if (visibleNavs.length > 0) {
    switchAdminTab(visibleNavs[0].getAttribute("data-tab"));
  } else {
    // No tabs visible — show empty state
    var panels = document.querySelectorAll(".admin-panel");
    for (var j = 0; j < panels.length; j++) panels[j].style.display = "none";
    var empty = document.getElementById("admin-no-permissions");
    if (!empty) {
      empty = document.createElement("div");
      empty.id = "admin-no-permissions";
      empty.className = "dashboard-empty";
      empty.textContent = "You do not have permissions to view any admin tabs.";
      document.getElementById("admin-content").appendChild(empty);
    }
    empty.style.display = "";
  }
}

function _injectMobileToggle(tab) {
  var toggle = document.getElementById("admin-mobile-toggle");
  if (!toggle) {
    toggle = document.createElement("button");
    toggle.id = "admin-mobile-toggle";
    toggle.className = "admin-mobile-toggle";
    toggle.setAttribute("aria-label", "Open navigation");
    toggle.setAttribute("aria-expanded", "false");
    toggle.onclick = function () {
      _toggleMobileSidebar();
    };
  }
  var panel = document.getElementById("admin-" + tab);
  if (!panel) return;
  var toolbar = panel.querySelector(".admin-toolbar");
  if (toolbar) {
    if (!toolbar.contains(toggle))
      toolbar.insertBefore(toggle, toolbar.firstChild);
  } else {
    // Panel has no toolbar — prepend toggle directly so it remains accessible
    if (!panel.contains(toggle)) panel.insertBefore(toggle, panel.firstChild);
  }
}

function _toggleMobileSidebar() {
  _mobileSidebarOpen = !_mobileSidebarOpen;
  var sidebar = document.getElementById("admin-sidebar");
  sidebar.classList.toggle("open", _mobileSidebarOpen);
  sidebar.classList.toggle("collapsed", !_mobileSidebarOpen);
  sidebar.setAttribute("aria-hidden", _mobileSidebarOpen ? "false" : "true");
  if (_mobileSidebarOpen) sidebar.removeAttribute("inert");
  else sidebar.setAttribute("inert", "");
  var backdrop = document.getElementById("admin-sidebar-backdrop");
  if (backdrop) backdrop.classList.toggle("visible", _mobileSidebarOpen);
  // Update hamburger aria-label to reflect current state
  var mt = document.getElementById("admin-mobile-toggle");
  if (mt) {
    mt.setAttribute(
      "aria-label",
      _mobileSidebarOpen ? "Close navigation" : "Open navigation",
    );
    mt.setAttribute("aria-expanded", _mobileSidebarOpen ? "true" : "false");
  }
  // Move focus into drawer on open; callers handle focus-return on close
  if (_mobileSidebarOpen) {
    var closeBtn = sidebar.querySelector(".admin-sidebar-close button");
    if (closeBtn) closeBtn.focus();
  }
}

function switchAdminTab(tab) {
  _adminTab = tab;
  // Hide no-permissions empty state if it was showing
  var noPerms = document.getElementById("admin-no-permissions");
  if (noPerms) noPerms.style.display = "none";
  var navItems = document.querySelectorAll(".admin-nav");
  for (var i = 0; i < navItems.length; i++) {
    var isActive = navItems[i].getAttribute("data-tab") === tab;
    navItems[i].classList.toggle("active", isActive);
    navItems[i].setAttribute("aria-selected", isActive ? "true" : "false");
    navItems[i].setAttribute("tabindex", isActive ? "0" : "-1");
  }
  var panels = [
    "users",
    "tokens",
    "channels",
    "schedules",
    "watches",
    "roles",
    "policies",
    "skills",
    "usage",
    "audit",
    "memories",
    "models",
    "node-metadata",
    "settings",
    "tls",
    "mcp",
    "prompt-policies",
    "judge",
  ];
  for (var p = 0; p < panels.length; p++) {
    var el = document.getElementById("admin-" + panels[p]);
    if (el) el.style.display = panels[p] === tab ? "" : "none";
  }

  if (tab === "users") loadAdminUsers();
  if (tab === "tokens") _populateTokenUserSelect();
  if (tab === "channels") _populateChannelUserSelect();
  if (tab === "schedules") loadAdminSchedules();
  if (tab === "watches") loadAdminWatches();
  if (tab === "roles") loadGovRoles();
  if (tab === "policies") loadGovPolicies();
  if (tab === "skills") loadGovSkills();
  if (tab === "usage") loadGovUsage();
  if (tab === "audit") {
    _populateAuditUserFilter();
    loadGovAudit();
  }
  if (tab === "memories") loadAdminMemories();
  if (tab === "models") loadAdminModels();
  if (tab === "node-metadata") loadAdminNodeMetadata();
  if (tab === "settings") loadSettings();
  if (tab === "tls") loadTlsCerts();
  if (tab === "mcp") loadAdminMcp();
  if (tab === "prompt-policies") loadPromptPolicies();
  if (tab === "judge") loadJudgeTab();

  // Update breadcrumb with active tab label
  var activeNav = document.querySelector('.admin-nav[data-tab="' + tab + '"]');
  var label = activeNav ? activeNav.textContent : tab;
  var bcLabel = document.getElementById("breadcrumb-label");
  if (bcLabel) bcLabel.textContent = "Admin / " + label;

  // Inject mobile hamburger toggle into active panel's toolbar
  _injectMobileToggle(tab);

  // On mobile, auto-close sidebar after tab selection
  if (window.innerWidth <= 700 && _mobileSidebarOpen) {
    _toggleMobileSidebar();
    // Move focus to the newly active panel instead of leaving it in the inert sidebar
    var panel = document.getElementById("admin-" + tab);
    var focusTarget =
      panel &&
      panel.querySelector("h2, .section-header, button:not([disabled])");
    if (focusTarget) {
      focusTarget.setAttribute("tabindex", "-1");
      focusTarget.focus();
    }
  }
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
      '<div class="admin-row" role="listitem" data-expandable data-user-id="' +
      escapeHtml(u.user_id) +
      '" data-username="' +
      escapeHtml(u.username) +
      '" tabindex="0" aria-expanded="false">' +
      '<span class="admin-col admin-col-username">' +
      '<span class="admin-expand-indicator" aria-hidden="true">\u25b8</span>' +
      escapeHtml(u.username) +
      "</span>" +
      '<span class="admin-col admin-col-name">' +
      escapeHtml(u.display_name) +
      "</span>" +
      '<span class="admin-col admin-col-created">' +
      escapeHtml(u.created || "").slice(0, 10) +
      "</span>" +
      '<span class="admin-col admin-col-actions">' +
      '<button class="admin-btn-action" data-user-roles="' +
      escapeHtml(u.user_id) +
      '" title="Manage roles">roles</button>' +
      '<button class="admin-btn-danger" data-delete-user="' +
      escapeHtml(u.user_id) +
      '" data-username="' +
      escapeHtml(u.username) +
      '" title="Delete user">delete</button>' +
      "</span>" +
      "</div>";
  }
  container.innerHTML = html;
  // Bind roles buttons
  var roleBtns = container.querySelectorAll("[data-user-roles]");
  for (var rj = 0; rj < roleBtns.length; rj++) {
    roleBtns[rj].addEventListener("click", function () {
      showUserRolesModal(this.getAttribute("data-user-roles"));
    });
  }
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
  // Bind expandable row click + keyboard handlers for OIDC detail panel
  var rows = container.querySelectorAll(".admin-row[data-expandable]");
  for (var k = 0; k < rows.length; k++) {
    (function (row) {
      var _expand = function () {
        var uid = row.getAttribute("data-user-id");
        var uname = row.getAttribute("data-username");
        _toggleOidcPanel(uid, uname, row);
      };
      row.addEventListener("click", function (e) {
        if (
          e.target.closest(".admin-btn-danger") ||
          e.target.closest(".admin-btn-action")
        )
          return;
        _expand();
      });
      row.addEventListener("keydown", function (e) {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          _expand();
        }
      });
    })(rows[k]);
  }
}

function confirmDeleteUser(userId, username) {
  showConfirmModal(
    "Delete User",
    "Delete user \u2018" +
      username +
      "\u2019 and all their tokens and channel links? This cannot be undone.",
    "Delete",
    function () {
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
    },
  );
}

// ---------------------------------------------------------------------------
// OIDC identity expansion in Users tab
// ---------------------------------------------------------------------------

function _toggleOidcPanel(userId, username, rowEl) {
  var existing = rowEl.nextElementSibling;
  if (existing && existing.classList.contains("oidc-detail-panel")) {
    // Collapse
    existing.style.maxHeight = "0";
    var indicator = rowEl.querySelector(".admin-expand-indicator");
    if (indicator) indicator.classList.remove("expanded");
    rowEl.setAttribute("aria-expanded", "false");
    setTimeout(function () {
      if (existing.parentNode) existing.remove();
    }, 160);
    return;
  }
  // Collapse any other open panel first
  var openPanels = document.querySelectorAll(
    "#admin-users-table .oidc-detail-panel",
  );
  for (var i = 0; i < openPanels.length; i++) {
    openPanels[i].style.maxHeight = "0";
    var prevRow = openPanels[i].previousElementSibling;
    if (prevRow) {
      var ind = prevRow.querySelector(".admin-expand-indicator");
      if (ind) ind.classList.remove("expanded");
      prevRow.setAttribute("aria-expanded", "false");
    }
    (function (panel) {
      setTimeout(function () {
        if (panel.parentNode) panel.remove();
      }, 160);
    })(openPanels[i]);
  }
  // Mark expanded
  var indicator = rowEl.querySelector(".admin-expand-indicator");
  if (indicator) indicator.classList.add("expanded");
  rowEl.setAttribute("aria-expanded", "true");
  // Create panel (role="none" so it doesn't break the parent role="list")
  var panel = document.createElement("div");
  panel.className = "oidc-detail-panel";
  panel.setAttribute("role", "none");
  panel.innerHTML =
    '<div class="oidc-detail-inner">' +
    '<div class="oidc-detail-header">OIDC Identities</div>' +
    '<div class="oidc-detail-body"><span class="oidc-detail-empty">Loading\u2026</span></div>' +
    "</div>";
  rowEl.after(panel);
  // Animate open
  requestAnimationFrame(function () {
    panel.style.maxHeight = panel.scrollHeight + "px";
  });
  // Fetch identities
  authFetch(
    "/v1/api/admin/users/" + encodeURIComponent(userId) + "/oidc-identities",
  )
    .then(function (r) {
      if (!r.ok) throw new Error("Failed");
      return r.json();
    })
    .then(function (data) {
      _renderOidcDetail(panel, data.oidc_identities || [], userId, username);
    })
    .catch(function () {
      var body = panel.querySelector(".oidc-detail-body");
      if (body)
        body.innerHTML =
          '<span class="oidc-detail-empty">Failed to load</span>';
    });
}

function _renderOidcDetail(panel, identities, userId, username) {
  var body = panel.querySelector(".oidc-detail-body");
  if (!body) return;
  if (!identities.length) {
    body.innerHTML =
      '<span class="oidc-detail-empty">No OIDC identities linked</span>';
    panel.style.maxHeight = panel.scrollHeight + "px";
    return;
  }
  var html = "";
  for (var i = 0; i < identities.length; i++) {
    var oid = identities[i];
    var shortIssuer = _issuerShortName(oid.issuer || "");
    var shortSubject =
      (oid.subject || "").length > 12
        ? (oid.subject || "").slice(0, 12) + "\u2026"
        : oid.subject || "";
    var lastLogin = oid.last_login ? _relativeTime(oid.last_login) : "never";
    html +=
      '<div class="oidc-identity-row">' +
      '<span class="oidc-identity-issuer"><span class="scope-badge">' +
      escapeHtml(shortIssuer) +
      "</span></span>" +
      '<span class="oidc-identity-subject" title="' +
      escapeHtml(oid.subject || "") +
      '">' +
      escapeHtml(shortSubject) +
      "</span>" +
      '<span class="oidc-identity-email" title="' +
      escapeHtml(oid.email || "") +
      '">' +
      escapeHtml(oid.email || "\u2014") +
      "</span>" +
      '<span class="oidc-identity-time">' +
      escapeHtml(lastLogin) +
      "</span>" +
      '<span class="oidc-identity-actions">' +
      '<button class="admin-btn-danger" aria-label="Unlink ' +
      escapeHtml(shortIssuer) +
      " identity " +
      escapeHtml(shortSubject) +
      '" data-oidc-issuer="' +
      escapeHtml(oid.issuer || "") +
      '" data-oidc-subject="' +
      escapeHtml(oid.subject || "") +
      '" data-oidc-username="' +
      escapeHtml(username) +
      '" data-oidc-user-id="' +
      escapeHtml(userId) +
      '">unlink</button>' +
      "</span></div>";
  }
  body.innerHTML = html;
  // Update panel height for animation
  panel.style.maxHeight = panel.scrollHeight + "px";
  // Bind unlink buttons
  var btns = body.querySelectorAll("[data-oidc-issuer]");
  for (var j = 0; j < btns.length; j++) {
    btns[j].addEventListener("click", function (e) {
      e.stopPropagation();
      var issuer = this.getAttribute("data-oidc-issuer");
      var subject = this.getAttribute("data-oidc-subject");
      var uname = this.getAttribute("data-oidc-username");
      var uid = this.getAttribute("data-oidc-user-id");
      _confirmUnlinkOidc(issuer, subject, uname, uid);
    });
  }
}

function _confirmUnlinkOidc(issuer, subject, username, userId) {
  var shortIssuer = _issuerShortName(issuer);
  var shortSubject =
    subject.length > 16 ? subject.slice(0, 16) + "\u2026" : subject;
  showConfirmModal(
    "Unlink OIDC Identity",
    "Unlink " +
      shortIssuer +
      " identity \u2018" +
      shortSubject +
      "\u2019 from user " +
      username +
      "?\n\nThe user will need to log in via OIDC again to re-link.",
    "Unlink",
    function () {
      authFetch(
        "/v1/api/admin/oidc-identities?issuer=" +
          encodeURIComponent(issuer) +
          "&subject=" +
          encodeURIComponent(subject),
        { method: "DELETE" },
      )
        .then(function (r) {
          if (!r.ok) throw new Error("Unlink failed");
          showToast("OIDC identity unlinked");
          // Refresh the panel content in place (no close/reopen flicker)
          var allRows = document.querySelectorAll(
            "#admin-users-table .admin-row[data-expandable]",
          );
          var targetRow = null;
          for (var ri = 0; ri < allRows.length; ri++) {
            if (allRows[ri].getAttribute("data-user-id") === userId) {
              targetRow = allRows[ri];
              break;
            }
          }
          if (targetRow) {
            var panel = targetRow.nextElementSibling;
            if (panel && panel.classList.contains("oidc-detail-panel")) {
              var body = panel.querySelector(".oidc-detail-body");
              if (body)
                body.innerHTML =
                  '<span class="oidc-detail-empty">Loading\u2026</span>';
              authFetch(
                "/v1/api/admin/users/" +
                  encodeURIComponent(userId) +
                  "/oidc-identities",
              )
                .then(function (r2) {
                  if (!r2.ok) throw new Error("Failed");
                  return r2.json();
                })
                .then(function (data) {
                  _renderOidcDetail(
                    panel,
                    data.oidc_identities || [],
                    userId,
                    username,
                  );
                })
                .catch(function () {
                  if (body)
                    body.innerHTML =
                      '<span class="oidc-detail-empty">Failed to load</span>';
                });
            }
          }
        })
        .catch(function () {
          showToast("Failed to unlink OIDC identity");
        });
    },
  );
}

function _issuerShortName(issuer) {
  try {
    var host = new URL(issuer).hostname;
    if (host.includes("google")) return "google";
    if (host.includes("microsoftonline") || host.includes("azure"))
      return "azure";
    if (host.includes("okta")) return "okta";
    if (host.includes("auth0")) return "auth0";
    if (host.includes("keycloak")) return "keycloak";
    return host.replace(/^(login|accounts|auth|id|sso)\./, "");
  } catch (e) {
    return issuer || "unknown";
  }
}

function _relativeTime(isoStr) {
  try {
    var then = new Date(
      isoStr + (isoStr.includes("Z") || isoStr.includes("+") ? "" : "Z"),
    );
    var diff = (Date.now() - then.getTime()) / 1000;
    if (diff < 60) return "just now";
    if (diff < 3600) return Math.floor(diff / 60) + "m ago";
    if (diff < 86400) return Math.floor(diff / 3600) + "h ago";
    if (diff < 2592000) return Math.floor(diff / 86400) + "d ago";
    return isoStr.slice(0, 10);
  } catch (e) {
    return isoStr || "unknown";
  }
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
  showConfirmModal(
    "Revoke Token",
    "Revoke this API token? Existing JWTs issued from it will remain valid until they expire (max 24h). This cannot be undone.",
    "Revoke",
    function () {
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
    },
  );
}

// ---------------------------------------------------------------------------
// Channels
// ---------------------------------------------------------------------------

function _populateChannelUserSelect() {
  var sel = document.getElementById("admin-channel-user");
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

function loadAdminChannels() {
  var userId = document.getElementById("admin-channel-user").value;
  _adminChannelUserId = userId;
  if (!userId) {
    document.getElementById("admin-channels-table").innerHTML =
      '<div class="dashboard-empty">Select a user to view channel links</div>';
    return;
  }
  document.getElementById("admin-channels-table").innerHTML =
    '<div class="dashboard-empty">Loading channel links...</div>';
  authFetch("/v1/api/admin/users/" + encodeURIComponent(userId) + "/channels")
    .then(function (r) {
      if (!r.ok) throw new Error("Failed to load channels");
      return r.json();
    })
    .then(function (data) {
      _renderChannels(data.channels || []);
    })
    .catch(function () {
      document.getElementById("admin-channels-table").innerHTML =
        '<div class="dashboard-empty">Failed to load channel links</div>';
    });
}

function _renderChannels(channels) {
  var container = document.getElementById("admin-channels-table");
  if (!channels.length) {
    container.innerHTML =
      '<div class="dashboard-empty">No channel links for this user</div>';
    return;
  }
  var html = "";
  for (var i = 0; i < channels.length; i++) {
    var c = channels[i];
    // Per-platform badge class (scope-discord / scope-slack) so different
    // adapters render with their own color.  Falls back to the generic
    // scope-channel for unknown platforms; the per-platform class wins
    // by being the only class set, not by source order.
    var ctSlug = (c.channel_type || "").toLowerCase().replace(/[^a-z0-9]/g, "");
    var ctClass =
      ctSlug && (ctSlug === "discord" || ctSlug === "slack")
        ? "scope-badge scope-" + ctSlug
        : "scope-badge scope-channel";
    html +=
      '<div class="admin-row" role="listitem">' +
      '<span class="admin-col admin-col-chtype"><span class="' +
      ctClass +
      '">' +
      escapeHtml(c.channel_type) +
      "</span></span>" +
      '<span class="admin-col admin-col-chuid"><code>' +
      escapeHtml(c.channel_user_id) +
      "</code></span>" +
      '<span class="admin-col admin-col-created">' +
      escapeHtml(c.created || "").slice(0, 10) +
      "</span>" +
      '<span class="admin-col admin-col-actions">' +
      '<button class="admin-btn-danger" data-unlink-type="' +
      escapeHtml(c.channel_type) +
      '" data-unlink-uid="' +
      escapeHtml(c.channel_user_id) +
      '" title="Unlink channel account">unlink</button>' +
      "</span>" +
      "</div>";
  }
  container.innerHTML = html;
  var btns = container.querySelectorAll("[data-unlink-type]");
  for (var j = 0; j < btns.length; j++) {
    btns[j].addEventListener("click", function () {
      confirmUnlinkChannel(
        this.getAttribute("data-unlink-type"),
        this.getAttribute("data-unlink-uid"),
      );
    });
  }
}

function confirmUnlinkChannel(channelType, channelUserId) {
  showConfirmModal(
    "Unlink Channel",
    "Unlink " +
      channelType +
      " account \u2018" +
      channelUserId +
      "\u2019? The user will need to re-link via /link to interact with the bot.",
    "Unlink",
    function () {
      authFetch(
        "/v1/api/admin/channels/" +
          encodeURIComponent(channelType) +
          "/" +
          encodeURIComponent(channelUserId),
        { method: "DELETE" },
      )
        .then(function (r) {
          if (!r.ok) throw new Error("Unlink failed");
          showToast("Channel account unlinked");
          loadAdminChannels();
        })
        .catch(function () {
          showToast("Failed to unlink channel account");
        });
    },
  );
}

// ---------------------------------------------------------------------------
// Schedules
// ---------------------------------------------------------------------------

var _csTrapHandler = null;
var _esTrapHandler = null;
var _srTrapHandler = null;
var _editScheduleTriggerEl = null;
var _runsScheduleTriggerEl = null;

function loadAdminSchedules() {
  authFetch("/v1/api/admin/schedules")
    .then(function (r) {
      if (!r.ok) throw new Error("Failed to load schedules");
      return r.json();
    })
    .then(function (data) {
      _renderSchedules(data.schedules || []);
    })
    .catch(function () {
      document.getElementById("admin-schedules-table").innerHTML =
        '<div class="dashboard-empty">Failed to load schedules</div>';
    });
}

function _renderSchedules(schedules) {
  var container = document.getElementById("admin-schedules-table");
  if (!schedules.length) {
    container.innerHTML =
      '<div class="dashboard-empty">No scheduled tasks. Create one to get started.</div>';
    return;
  }
  var html = "";
  for (var i = 0; i < schedules.length; i++) {
    var s = schedules[i];
    var typeLabel = s.schedule_type === "cron" ? "cron" : "at";
    var typeCls = s.schedule_type === "cron" ? "scope-write" : "scope-approve";
    var schedule =
      s.schedule_type === "cron"
        ? s.cron_expr
        : _utcToLocalDatetime(s.at_time).replace("T", " ");
    var target = s.target_mode;
    var nextRun = s.next_run
      ? _utcToLocalDatetime(s.next_run).replace("T", " ")
      : "\u2014";
    var enabled = s.enabled;
    var statusCls = enabled ? "sched-active" : "sched-disabled";
    var statusLabel = enabled ? "active" : "disabled";
    var statusDot = enabled ? "\u25cf " : "\u25cb ";
    if (s.schedule_type === "at" && !enabled && s.last_run) {
      statusCls = "sched-expired";
      statusLabel = "completed";
      statusDot = "\u25c9 ";
    }
    html +=
      '<div class="admin-row" role="listitem">' +
      '<span class="admin-col admin-col-sname">' +
      escapeHtml(s.name) +
      "</span>" +
      '<span class="admin-col admin-col-stype"><span class="scope-badge ' +
      typeCls +
      '">' +
      typeLabel +
      "</span></span>" +
      '<span class="admin-col admin-col-sschedule"><code>' +
      escapeHtml(schedule) +
      "</code></span>" +
      '<span class="admin-col admin-col-starget">' +
      escapeHtml(target) +
      "</span>" +
      '<span class="admin-col admin-col-snext">' +
      nextRun +
      "</span>" +
      '<span class="admin-col admin-col-sstatus"><span class="' +
      statusCls +
      '">' +
      statusDot +
      statusLabel +
      "</span></span>" +
      '<span class="admin-col admin-col-actions">' +
      '<button class="admin-btn-action" data-edit-sched="' +
      escapeHtml(s.task_id) +
      '" title="Edit">edit</button>' +
      '<button class="admin-btn-action" data-runs-sched="' +
      escapeHtml(s.task_id) +
      '" title="Run history">runs</button>' +
      '<button class="admin-btn-action" data-toggle-sched="' +
      escapeHtml(s.task_id) +
      '" data-enabled="' +
      (enabled ? "1" : "0") +
      '" title="' +
      (enabled ? "Disable" : "Enable") +
      '">' +
      (enabled ? "disable" : "enable") +
      "</button>" +
      '<button class="admin-btn-danger" data-delete-sched="' +
      escapeHtml(s.task_id) +
      '" data-sname="' +
      escapeHtml(s.name) +
      '" title="Delete">delete</button>' +
      "</span></div>";
  }
  container.innerHTML = html;
  // Bind buttons
  var editBtns = container.querySelectorAll("[data-edit-sched]");
  for (var j = 0; j < editBtns.length; j++) {
    editBtns[j].addEventListener("click", function () {
      showEditScheduleModal(this.getAttribute("data-edit-sched"));
    });
  }
  var runsBtns = container.querySelectorAll("[data-runs-sched]");
  for (var k = 0; k < runsBtns.length; k++) {
    runsBtns[k].addEventListener("click", function () {
      showScheduleRuns(this.getAttribute("data-runs-sched"));
    });
  }
  var toggleBtns = container.querySelectorAll("[data-toggle-sched]");
  for (var m = 0; m < toggleBtns.length; m++) {
    toggleBtns[m].addEventListener("click", function () {
      toggleSchedule(
        this.getAttribute("data-toggle-sched"),
        this.getAttribute("data-enabled") === "1",
      );
    });
  }
  var delBtns = container.querySelectorAll("[data-delete-sched]");
  for (var n = 0; n < delBtns.length; n++) {
    delBtns[n].addEventListener("click", function () {
      confirmDeleteSchedule(
        this.getAttribute("data-delete-sched"),
        this.getAttribute("data-sname"),
      );
    });
  }
}

function toggleSchedule(taskId, currentlyEnabled) {
  authFetch("/v1/api/admin/schedules/" + encodeURIComponent(taskId), {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled: !currentlyEnabled }),
  })
    .then(function (r) {
      if (!r.ok) throw new Error("Toggle failed");
      showToast(currentlyEnabled ? "Schedule disabled" : "Schedule enabled");
      loadAdminSchedules();
    })
    .catch(function () {
      showToast("Failed to toggle schedule");
    });
}

function confirmDeleteSchedule(taskId, name) {
  showConfirmModal(
    "Delete Schedule",
    "Delete schedule \u2018" +
      name +
      "\u2019 and its run history? This cannot be undone.",
    "Delete",
    function () {
      authFetch("/v1/api/admin/schedules/" + encodeURIComponent(taskId), {
        method: "DELETE",
      })
        .then(function (r) {
          if (!r.ok) throw new Error("Delete failed");
          showToast("Schedule deleted");
          loadAdminSchedules();
        })
        .catch(function () {
          showToast("Failed to delete schedule");
        });
    },
  );
}

// --- Schedule helpers: dropdowns, notify rows, timezone ---

function _populateScheduleSelect(selectId, url, labelKey, valueKey, opts) {
  var sel = document.getElementById(selectId);
  // Keep the first option (placeholder) and remove the rest
  while (sel.options.length > 1) sel.remove(1);
  // Add temporary option for pre-selected value so form is correct before fetch completes
  if (opts && opts.selected) {
    var tmp = document.createElement("option");
    tmp.value = opts.selected;
    tmp.textContent = opts.selected;
    tmp.dataset.temporary = "1";
    sel.appendChild(tmp);
    sel.value = opts.selected;
  }
  authFetch(url)
    .then(function (r) {
      return r.json();
    })
    .then(function (data) {
      var temp = sel.querySelector("[data-temporary]");
      if (temp) temp.remove();
      var items = opts && opts.listKey ? data[opts.listKey] : data;
      if (!Array.isArray(items)) return;
      items.forEach(function (item) {
        var opt = document.createElement("option");
        opt.value = item[valueKey];
        opt.textContent =
          opts && opts.display ? opts.display(item) : item[labelKey];
        sel.appendChild(opt);
      });
      if (opts && opts.selected) sel.value = opts.selected;
    })
    .catch(function () {
      /* dropdown stays with placeholder or temporary option */
    });
}

// Channel platforms shown in admin notify-target rows.  Mirror server-side
// channel adapters; expand here when a new adapter ships (Discord / Slack
// today, MS Teams / etc. later).
var _NOTIFY_CHANNEL_TYPES = [
  {
    value: "discord",
    label: "Discord",
    id_hint: "Discord ID (e.g. 123456789012345678)",
  },
  {
    value: "slack",
    label: "Slack",
    id_hint: "Slack ID (e.g. C01234567 or U01234567)",
  },
];

function _notifyIdPlaceholder(channelType) {
  for (var i = 0; i < _NOTIFY_CHANNEL_TYPES.length; i++) {
    if (_NOTIFY_CHANNEL_TYPES[i].value === channelType) {
      return _NOTIFY_CHANNEL_TYPES[i].id_hint;
    }
  }
  return "ID";
}

function _addNotifyRow(prefix, targetType, targetId, channelType) {
  var container = document.getElementById(prefix + "-notify-rows");
  var row = document.createElement("div");
  row.className = "notify-row";

  var ctSel = document.createElement("select");
  ctSel.setAttribute("aria-label", "Channel platform");
  ctSel.className = "notify-row-ct";
  for (var i = 0; i < _NOTIFY_CHANNEL_TYPES.length; i++) {
    var ctOpt = document.createElement("option");
    ctOpt.value = _NOTIFY_CHANNEL_TYPES[i].value;
    ctOpt.textContent = _NOTIFY_CHANNEL_TYPES[i].label;
    ctSel.appendChild(ctOpt);
  }
  ctSel.value = channelType || "discord";

  var typeSel = document.createElement("select");
  typeSel.setAttribute("aria-label", "Target type");
  typeSel.className = "notify-row-target";
  var optCh = document.createElement("option");
  optCh.value = "channel_id";
  optCh.textContent = "Channel";
  var optUsr = document.createElement("option");
  optUsr.value = "user_id";
  optUsr.textContent = "User DM";
  typeSel.appendChild(optCh);
  typeSel.appendChild(optUsr);
  if (targetType) typeSel.value = targetType;

  var idInput = document.createElement("input");
  idInput.type = "text";
  idInput.className = "notify-row-id";
  idInput.placeholder = _notifyIdPlaceholder(ctSel.value);
  idInput.setAttribute("aria-label", "Channel/user ID");
  idInput.spellcheck = false;
  if (targetId) idInput.value = targetId;

  // Re-hint the ID input when the platform changes — e.g. Discord
  // snowflakes vs Slack C…/U… ids.
  ctSel.addEventListener("change", function () {
    idInput.placeholder = _notifyIdPlaceholder(ctSel.value);
  });

  var removeBtn = document.createElement("button");
  removeBtn.type = "button";
  removeBtn.className = "notify-row-remove";
  removeBtn.setAttribute("aria-label", "Remove target");
  removeBtn.textContent = "\u00d7";
  removeBtn.onclick = function () {
    row.remove();
  };

  row.appendChild(ctSel);
  row.appendChild(typeSel);
  row.appendChild(idInput);
  row.appendChild(removeBtn);
  container.appendChild(row);
  idInput.focus();
}

function _collectNotifyTargets(prefix) {
  var rows = document
    .getElementById(prefix + "-notify-rows")
    .querySelectorAll(".notify-row");
  var targets = [];
  for (var i = 0; i < rows.length; i++) {
    var ct = (rows[i].querySelector(".notify-row-ct") || {}).value || "discord";
    var type =
      (rows[i].querySelector(".notify-row-target") || {}).value || "channel_id";
    var idEl = rows[i].querySelector(".notify-row-id");
    var id = ((idEl && idEl.value) || "").trim();
    if (!id) continue;
    var t = { channel_type: ct };
    t[type] = id;
    targets.push(t);
  }
  return targets;
}

function _populateNotifyRows(prefix, targets) {
  var container = document.getElementById(prefix + "-notify-rows");
  while (container.firstChild) container.removeChild(container.firstChild);
  if (!Array.isArray(targets)) return;
  targets.forEach(function (t) {
    var targetType = "channel_id" in t ? "channel_id" : "user_id";
    var targetId = t[targetType] || "";
    _addNotifyRow(prefix, targetType, targetId, t.channel_type || "discord");
  });
}

function _localToUtcIso(localDatetimeStr) {
  // datetime-local gives "YYYY-MM-DDTHH:MM" in browser local time
  // Convert to UTC ISO string for the server
  var d = new Date(localDatetimeStr);
  if (isNaN(d.getTime())) return "";
  return d.toISOString().replace(/\.\d{3}Z$/, "+00:00");
}

function _utcToLocalDatetime(utcStr) {
  // Convert UTC ISO string to datetime-local format in browser local time
  if (!utcStr) return "";
  var d = new Date(utcStr);
  if (isNaN(d.getTime())) return utcStr.slice(0, 16);
  var pad = function (n) {
    return n < 10 ? "0" + n : "" + n;
  };
  return (
    d.getFullYear() +
    "-" +
    pad(d.getMonth() + 1) +
    "-" +
    pad(d.getDate()) +
    "T" +
    pad(d.getHours()) +
    ":" +
    pad(d.getMinutes())
  );
}

// --- Create Schedule Modal ---

function toggleScheduleTypeFields() {
  var t = document.getElementById("cs-type").value;
  document.getElementById("cs-cron-group").style.display =
    t === "cron" ? "" : "none";
  document.getElementById("cs-at-group").style.display =
    t === "at" ? "" : "none";
  if (t === "cron") document.getElementById("cs-cron").focus();
  else document.getElementById("cs-at").focus();
}

function toggleScheduleNodeField() {
  var v = document.getElementById("cs-target").value;
  document.getElementById("cs-node-group").style.display =
    v === "node" ? "" : "none";
  if (v === "node") document.getElementById("cs-node").focus();
}

function showCreateScheduleModal() {
  var overlay = document.getElementById("create-schedule-overlay");
  overlay.style.display = "flex";
  document.getElementById("create-schedule-error").style.display = "none";
  document.getElementById("cs-name").value = "";
  document.getElementById("cs-desc").value = "";
  document.getElementById("cs-type").value = "cron";
  document.getElementById("cs-cron").value = "";
  document.getElementById("cs-at").value = "";
  document.getElementById("cs-target").value = "auto";
  document.getElementById("cs-node").value = "";
  document.getElementById("cs-message").value = "";
  document.getElementById("cs-autoapprove").checked = false;
  _populateNotifyRows("cs", []);
  // Populate model dropdown
  _populateScheduleSelect("cs-model", "/v1/api/models", "alias", "alias", {
    listKey: "models",
    display: function (m) {
      return m.alias === m.model ? m.alias : m.alias + " (" + m.model + ")";
    },
  });
  // Populate skill dropdown
  _populateScheduleSelect(
    "cs-template",
    "/v1/api/admin/skills",
    "name",
    "name",
    {
      listKey: "skills",
      display: function (s) {
        return s.name;
      },
    },
  );
  toggleScheduleTypeFields();
  toggleScheduleNodeField();
  document.getElementById("cs-submit").disabled = false;
  document.getElementById("cs-submit").textContent = "Create";
  _csTrapHandler = _installTrap(
    "create-schedule-overlay",
    "create-schedule-box",
  );
  setTimeout(function () {
    document.getElementById("cs-name").focus();
  }, 50);
}

function hideCreateScheduleModal() {
  document.getElementById("create-schedule-overlay").style.display = "none";
  _csTrapHandler = _removeTrap(_csTrapHandler);
  var trigger = document.querySelector("#admin-schedules .admin-action-btn");
  if (trigger) trigger.focus();
}

function submitCreateSchedule() {
  var name = (document.getElementById("cs-name").value || "").trim();
  var desc = (document.getElementById("cs-desc").value || "").trim();
  var schedType = document.getElementById("cs-type").value;
  var cronExpr = (document.getElementById("cs-cron").value || "").trim();
  var atTime = document.getElementById("cs-at").value || "";
  var targetMode = document.getElementById("cs-target").value;
  var nodeId = (document.getElementById("cs-node").value || "").trim();
  var model = (document.getElementById("cs-model").value || "").trim();
  var message = (document.getElementById("cs-message").value || "").trim();
  var skill = (document.getElementById("cs-template").value || "").trim();
  var autoApprove = document.getElementById("cs-autoapprove").checked;
  var notifyTargets = _collectNotifyTargets("cs");
  var errEl = document.getElementById("create-schedule-error");

  if (!name) return _showModalError(errEl, "Name is required");
  if (!message) return _showModalError(errEl, "Initial message is required");
  if (schedType === "cron" && !cronExpr)
    return _showModalError(errEl, "Cron expression is required");
  if (schedType === "at" && !atTime)
    return _showModalError(errEl, "Run time is required");

  // Convert browser local time to UTC for the server
  if (schedType === "at" && atTime) {
    atTime = _localToUtcIso(atTime);
  }

  if (targetMode === "node") targetMode = nodeId;

  var btn = document.getElementById("cs-submit");
  btn.disabled = true;
  btn.textContent = "Creating\u2026";

  authFetch("/v1/api/admin/schedules", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      name: name,
      description: desc,
      schedule_type: schedType,
      cron_expr: cronExpr,
      at_time: atTime,
      target_mode: targetMode,
      model: model,
      initial_message: message,
      auto_approve: autoApprove,
      skill: skill,
      notify_targets: notifyTargets,
    }),
  })
    .then(function (r) {
      if (!r.ok)
        return r.json().then(function (d) {
          throw new Error(d.error || "Failed");
        });
      return r.json();
    })
    .then(function () {
      hideCreateScheduleModal();
      showToast("Schedule '" + name + "' created");
      loadAdminSchedules();
    })
    .catch(function (err) {
      btn.disabled = false;
      btn.textContent = "Create";
      _showModalError(errEl, err.message || "Failed to create schedule");
    });
}

// --- Edit Schedule Modal ---

function toggleEditScheduleTypeFields() {
  var t = document.getElementById("es-type").value;
  document.getElementById("es-cron-group").style.display =
    t === "cron" ? "" : "none";
  document.getElementById("es-at-group").style.display =
    t === "at" ? "" : "none";
  if (t === "cron") document.getElementById("es-cron").focus();
  else document.getElementById("es-at").focus();
}

function toggleEditScheduleNodeField() {
  var v = document.getElementById("es-target").value;
  document.getElementById("es-node-group").style.display =
    v === "node" ? "" : "none";
  if (v === "node") document.getElementById("es-node").focus();
}

function showEditScheduleModal(taskId) {
  _editScheduleTriggerEl = document.activeElement;
  authFetch("/v1/api/admin/schedules/" + encodeURIComponent(taskId))
    .then(function (r) {
      if (!r.ok) throw new Error("Not found");
      return r.json();
    })
    .then(function (s) {
      document.getElementById("es-id").value = s.task_id;
      document.getElementById("es-name").value = s.name || "";
      document.getElementById("es-desc").value = s.description || "";
      document.getElementById("es-type").value = s.schedule_type;
      document.getElementById("es-cron").value = s.cron_expr || "";
      document.getElementById("es-at").value = _utcToLocalDatetime(s.at_time);
      var isSpecificNode =
        s.target_mode &&
        s.target_mode !== "auto" &&
        s.target_mode !== "pool" &&
        s.target_mode !== "all";
      document.getElementById("es-target").value = isSpecificNode
        ? "node"
        : s.target_mode;
      document.getElementById("es-node").value = isSpecificNode
        ? s.target_mode
        : "";
      // Populate model dropdown with current value pre-selected
      _populateScheduleSelect("es-model", "/v1/api/models", "alias", "alias", {
        listKey: "models",
        selected: s.model || "",
        display: function (m) {
          return m.alias === m.model ? m.alias : m.alias + " (" + m.model + ")";
        },
      });
      // Populate skill dropdown with current value pre-selected
      _populateScheduleSelect(
        "es-template",
        "/v1/api/admin/skills",
        "name",
        "name",
        {
          listKey: "skills",
          selected: s.skill || "",
          display: function (sk) {
            return sk.name;
          },
        },
      );
      document.getElementById("es-message").value = s.initial_message || "";
      document.getElementById("es-autoapprove").checked = !!s.auto_approve;
      document.getElementById("es-enabled").checked = !!s.enabled;
      _populateNotifyRows("es", s.notify_targets || []);
      toggleEditScheduleTypeFields();
      toggleEditScheduleNodeField();
      document.getElementById("edit-schedule-error").style.display = "none";
      document.getElementById("es-submit").disabled = false;
      document.getElementById("es-submit").textContent = "Save";
      var overlay = document.getElementById("edit-schedule-overlay");
      overlay.style.display = "flex";
      _esTrapHandler = _installTrap(
        "edit-schedule-overlay",
        "edit-schedule-box",
      );
      setTimeout(function () {
        document.getElementById("es-name").focus();
      }, 50);
    })
    .catch(function () {
      showToast("Failed to load schedule");
    });
}

function hideEditScheduleModal() {
  document.getElementById("edit-schedule-overlay").style.display = "none";
  _esTrapHandler = _removeTrap(_esTrapHandler);
  if (_editScheduleTriggerEl && _editScheduleTriggerEl.isConnected) {
    _editScheduleTriggerEl.focus();
  }
  _editScheduleTriggerEl = null;
}

function submitEditSchedule() {
  var taskId = document.getElementById("es-id").value;
  var name = (document.getElementById("es-name").value || "").trim();
  var message = (document.getElementById("es-message").value || "").trim();
  var schedType = document.getElementById("es-type").value;
  var cronExpr = (document.getElementById("es-cron").value || "").trim();
  var targetMode = document.getElementById("es-target").value;
  if (targetMode === "node")
    targetMode = (document.getElementById("es-node").value || "").trim();
  var atTime = document.getElementById("es-at").value || "";

  var editNotifyTargets = _collectNotifyTargets("es");
  var errEl = document.getElementById("edit-schedule-error");

  if (!name) return _showModalError(errEl, "Name is required");
  if (!message) return _showModalError(errEl, "Initial message is required");
  if (schedType === "cron" && !cronExpr)
    return _showModalError(errEl, "Cron expression is required");
  if (schedType === "at" && !atTime)
    return _showModalError(errEl, "Run time is required");

  // Convert browser local time to UTC for the server
  if (schedType === "at" && atTime) {
    atTime = _localToUtcIso(atTime);
  }

  var btn = document.getElementById("es-submit");
  btn.disabled = true;
  btn.textContent = "Saving\u2026";

  authFetch("/v1/api/admin/schedules/" + encodeURIComponent(taskId), {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      name: name,
      description: (document.getElementById("es-desc").value || "").trim(),
      schedule_type: schedType,
      cron_expr: cronExpr,
      at_time: atTime,
      target_mode: targetMode,
      model: (document.getElementById("es-model").value || "").trim(),
      skill: (document.getElementById("es-template").value || "").trim(),
      initial_message: message,
      auto_approve: document.getElementById("es-autoapprove").checked,
      enabled: document.getElementById("es-enabled").checked,
      notify_targets: editNotifyTargets,
    }),
  })
    .then(function (r) {
      if (!r.ok)
        return r.json().then(function (d) {
          throw new Error(d.error || "Failed");
        });
      return r.json();
    })
    .then(function () {
      hideEditScheduleModal();
      showToast("Schedule updated");
      loadAdminSchedules();
    })
    .catch(function (err) {
      btn.disabled = false;
      btn.textContent = "Save";
      _showModalError(errEl, err.message || "Failed to update schedule");
    });
}

// --- Schedule Runs Modal ---

function showScheduleRuns(taskId) {
  _runsScheduleTriggerEl = document.activeElement;
  authFetch(
    "/v1/api/admin/schedules/" + encodeURIComponent(taskId) + "/runs?limit=50",
  )
    .then(function (r) {
      if (!r.ok) throw new Error("Not found");
      return r.json();
    })
    .then(function (data) {
      var runs = data.runs || [];
      var container = document.getElementById("schedule-runs-table");
      if (!runs.length) {
        container.innerHTML = '<div class="dashboard-empty">No runs yet</div>';
      } else {
        var html =
          '<div class="admin-colheaders sched-runs-grid" aria-hidden="true">' +
          '<span class="admin-col">STARTED</span>' +
          '<span class="admin-col">NODE</span>' +
          '<span class="admin-col">STATUS</span>' +
          '<span class="admin-col">ERROR</span></div>';
        for (var i = 0; i < runs.length; i++) {
          var r = runs[i];
          var statusCls =
            r.status === "dispatched"
              ? "sched-active"
              : r.status === "failed"
                ? "sched-expired"
                : "";
          html +=
            '<div class="admin-row sched-runs-grid">' +
            '<span class="admin-col">' +
            escapeHtml(r.started || "")
              .slice(0, 19)
              .replace("T", " ") +
            "</span>" +
            '<span class="admin-col">' +
            escapeHtml(r.node_id || "\u2014") +
            "</span>" +
            '<span class="admin-col"><span class="' +
            statusCls +
            '">' +
            escapeHtml(r.status) +
            "</span></span>" +
            '<span class="admin-col">' +
            escapeHtml(r.error || "\u2014") +
            "</span></div>";
        }
        container.innerHTML = html;
      }
      var overlay = document.getElementById("schedule-runs-overlay");
      overlay.style.display = "flex";
      _srTrapHandler = _installTrap(
        "schedule-runs-overlay",
        "schedule-runs-box",
      );
    })
    .catch(function () {
      showToast("Failed to load run history");
    });
}

function hideScheduleRunsModal() {
  document.getElementById("schedule-runs-overlay").style.display = "none";
  _srTrapHandler = _removeTrap(_srTrapHandler);
  if (_runsScheduleTriggerEl && _runsScheduleTriggerEl.isConnected) {
    _runsScheduleTriggerEl.focus();
  }
  _runsScheduleTriggerEl = null;
}

// ---------------------------------------------------------------------------
// Watches
// ---------------------------------------------------------------------------

function _populateWatchNodeSelect() {
  var sel = document.getElementById("admin-watch-node");
  var current = sel.value;
  var seen = {};
  sel.innerHTML = '<option value="">All nodes</option>';
  for (var i = 0; i < _adminWatches.length; i++) {
    var nid = _adminWatches[i].node_id || "";
    if (nid && !seen[nid]) {
      seen[nid] = true;
      var opt = document.createElement("option");
      opt.value = nid;
      opt.textContent = nid;
      sel.appendChild(opt);
    }
  }
  if (current) sel.value = current;
}

function loadAdminWatches() {
  authFetch("/v1/api/admin/watches")
    .then(function (r) {
      if (!r.ok) throw new Error("Failed to load watches");
      return r.json();
    })
    .then(function (data) {
      _adminWatches = data.watches || [];
      _populateWatchNodeSelect();
      var nodeFilter = document.getElementById("admin-watch-node").value;
      var filtered = _adminWatches;
      if (nodeFilter) {
        filtered = _adminWatches.filter(function (w) {
          return w.node_id === nodeFilter;
        });
      }
      _renderWatches(filtered);
    })
    .catch(function () {
      document.getElementById("admin-watches-table").innerHTML =
        '<div class="dashboard-empty">Failed to load watches</div>';
    });
}

function _formatInterval(secs) {
  if (!secs || secs <= 0) return "\u2014";
  if (secs >= 3600) return Math.round(secs / 3600) + "h";
  if (secs >= 60) return Math.round(secs / 60) + "m";
  return secs + "s";
}

function _renderWatches(watches) {
  var container = document.getElementById("admin-watches-table");
  if (!watches.length) {
    container.innerHTML =
      '<div class="dashboard-empty">No active watches. Watches are created when workstreams use the watch tool.</div>';
    return;
  }
  var html = "";
  for (var i = 0; i < watches.length; i++) {
    var w = watches[i];
    var name = w.name || w.watch_id || "\u2014";
    var nodeShort = (w.node_id || "").slice(0, 8);
    var cmd = w.command || "";
    var cmdTrunc = cmd.length > 40 ? cmd.slice(0, 40) + "\u2026" : cmd;
    var interval = _formatInterval(w.interval_secs);
    var pollMax = w.max_polls ? w.max_polls : "\u221e";
    var pollLabel = (w.poll_count || 0) + "/" + pollMax;
    var cond = w.stop_on || "on change";
    var condTrunc = cond.length > 30 ? cond.slice(0, 30) + "\u2026" : cond;
    var active = w.active;
    var statusCls = active ? "watch-active" : "watch-completed";
    var statusLabel = active ? "active" : "done";
    var statusDot = active ? "\u25cf " : "\u25cb ";
    var cancelBtn = active
      ? '<button class="admin-btn-danger" data-cancel-watch="' +
        escapeHtml(w.watch_id) +
        '" data-watch-node="' +
        escapeHtml(w.node_id || "") +
        '" data-watch-name="' +
        escapeHtml(name) +
        '" title="Cancel watch">cancel</button>'
      : "";
    html +=
      '<div class="admin-row" role="listitem">' +
      '<span class="admin-col admin-col-wname">' +
      escapeHtml(name) +
      "</span>" +
      '<span class="admin-col admin-col-wnode" title="' +
      escapeHtml(w.node_id || "") +
      '"><code>' +
      escapeHtml(nodeShort) +
      "</code></span>" +
      '<span class="admin-col admin-col-wcmd" title="' +
      escapeHtml(cmd) +
      '"><code>' +
      escapeHtml(cmdTrunc) +
      "</code></span>" +
      '<span class="admin-col admin-col-winterval">' +
      escapeHtml(interval) +
      "</span>" +
      '<span class="admin-col admin-col-wpoll"><code>' +
      escapeHtml(pollLabel) +
      "</code></span>" +
      '<span class="admin-col admin-col-wcond" title="' +
      escapeHtml(cond) +
      '">' +
      escapeHtml(condTrunc) +
      "</span>" +
      '<span class="admin-col admin-col-wstatus"><span class="' +
      statusCls +
      '">' +
      statusDot +
      statusLabel +
      "</span></span>" +
      '<span class="admin-col admin-col-actions">' +
      cancelBtn +
      "</span></div>";
  }
  container.innerHTML = html;
  // Bind cancel buttons
  var btns = container.querySelectorAll("[data-cancel-watch]");
  for (var j = 0; j < btns.length; j++) {
    btns[j].addEventListener("click", function () {
      _cancelWatch(
        this.getAttribute("data-cancel-watch"),
        this.getAttribute("data-watch-node"),
        this.getAttribute("data-watch-name"),
      );
    });
  }
}

function _cancelWatch(watchId, nodeId, name) {
  showConfirmModal(
    "Cancel Watch",
    "Cancel watch \u2018" + name + "\u2019? This will stop future polling.",
    "Cancel watch",
    function () {
      authFetch(
        "/v1/api/admin/watches/" + encodeURIComponent(watchId) + "/cancel",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ node_id: nodeId }),
        },
      )
        .then(function (r) {
          if (!r.ok) throw new Error("Cancel failed");
          showToast("Watch '" + name + "' cancelled");
          loadAdminWatches();
        })
        .catch(function () {
          showToast("Failed to cancel watch");
        });
    },
  );
}

// ---------------------------------------------------------------------------
// Create Channel Link Modal
// ---------------------------------------------------------------------------

function showCreateChannelModal() {
  if (!_adminChannelUserId) {
    showToast("Select a user first");
    return;
  }
  var overlay = document.getElementById("create-channel-overlay");
  overlay.style.display = "flex";
  document.getElementById("create-channel-error").style.display = "none";
  var ctSel = document.getElementById("cc-type");
  var uidInput = document.getElementById("cc-uid");
  ctSel.value = "discord";
  uidInput.value = "";
  uidInput.placeholder = _notifyIdPlaceholder(ctSel.value);
  ctSel.onchange = function () {
    uidInput.placeholder = _notifyIdPlaceholder(ctSel.value);
  };
  document.getElementById("cc-submit").disabled = false;
  document.getElementById("cc-submit").textContent = "Link";
  _ccTrapHandler = _installTrap("create-channel-overlay", "create-channel-box");
  setTimeout(function () {
    uidInput.focus();
  }, 50);
}

function hideCreateChannelModal() {
  document.getElementById("create-channel-overlay").style.display = "none";
  _ccTrapHandler = _removeTrap(_ccTrapHandler);
  var trigger = document.querySelector("#admin-channels .admin-action-btn");
  if (trigger) trigger.focus();
}

function submitCreateChannel() {
  var channelType = document.getElementById("cc-type").value;
  var channelUserId = (document.getElementById("cc-uid").value || "").trim();
  var errEl = document.getElementById("create-channel-error");

  if (!channelUserId)
    return _showModalError(errEl, "External user ID is required");

  var btn = document.getElementById("cc-submit");
  btn.disabled = true;
  btn.textContent = "Linking\u2026";

  authFetch(
    "/v1/api/admin/users/" +
      encodeURIComponent(_adminChannelUserId) +
      "/channels",
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        channel_type: channelType,
        channel_user_id: channelUserId,
      }),
    },
  )
    .then(function (r) {
      if (r.status === 409)
        return r.json().then(function (d) {
          throw new Error(d.error || "Already linked");
        });
      if (!r.ok)
        return r.json().then(function (d) {
          throw new Error(d.error || "Failed");
        });
      return r.json();
    })
    .then(function () {
      hideCreateChannelModal();
      showToast("Channel account linked");
      loadAdminChannels();
    })
    .catch(function (err) {
      btn.disabled = false;
      btn.textContent = "Link";
      _showModalError(errEl, err.message || "Failed to link channel account");
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
  var trigger = document.querySelector("#admin-users .admin-action-btn");
  if (trigger) trigger.focus();
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
  var trigger = document.querySelector("#admin-tokens .admin-action-btn");
  if (trigger) trigger.focus();
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
  var trigger = document.querySelector("#admin-tokens .admin-action-btn");
  if (trigger) trigger.focus();
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
        "input:not([disabled]):not([type='hidden']), select:not([disabled]), textarea:not([disabled]), button:not([disabled])",
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
        else if (overlayId === "create-channel-overlay")
          hideCreateChannelModal();
        else if (overlayId === "create-schedule-overlay")
          hideCreateScheduleModal();
        else if (overlayId === "edit-schedule-overlay") hideEditScheduleModal();
        else if (overlayId === "schedule-runs-overlay") hideScheduleRunsModal();
        else if (overlayId === "confirm-overlay") hideConfirmModal();
        else if (overlayId === "create-role-overlay") hideCreateRoleModal();
        else if (overlayId === "edit-role-overlay") hideEditRoleModal();
        else if (overlayId === "user-roles-overlay") hideUserRolesModal();
        else if (overlayId === "create-policy-overlay") hideCreatePolicyModal();
        else if (overlayId === "edit-policy-overlay") hideEditPolicyModal();
        else if (overlayId === "create-template-overlay")
          hideCreateTemplateModal();
        else if (overlayId === "edit-template-overlay") hideEditTemplateModal();
        else if (overlayId === "memory-detail-overlay") hideMemoryDetailModal();
        else if (overlayId === "mcp-create-overlay") hideCreateMcpModal();
        else if (overlayId === "mcp-import-overlay") hideImportMcpModal();
        else if (overlayId === "mcp-detail-overlay") hideMcpDetailModal();
        else if (overlayId === "mcp-install-overlay") hideInstallMcpModal();
        else if (overlayId === "github-import-overlay") hideGitHubImportModal();
        else if (overlayId === "model-create-overlay") hideCreateModelModal();
        else if (overlayId === "create-ppolicy-overlay")
          hideCreatePromptPolicyModal();
        else if (overlayId === "edit-ppolicy-overlay")
          hideEditPromptPolicyModal();
        else if (overlayId === "create-hr-overlay") hideCreateHRModal();
        else if (overlayId === "edit-hr-overlay") hideEditHRModal();
        else if (overlayId === "create-ogp-overlay") hideCreateOGPModal();
        else if (overlayId === "edit-ogp-overlay") hideEditOGPModal();
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
  // Close any open settings help popover first
  var openHelp = document.querySelector('.settings-help-popover[style=""]');
  if (openHelp) {
    e.preventDefault();
    _closeAllSettingsHelp();
    return;
  }
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
  var cc = document.getElementById("create-channel-overlay");
  if (cc && cc.style.display !== "none") {
    e.preventDefault();
    hideCreateChannelModal();
    return;
  }
  var cso = document.getElementById("create-schedule-overlay");
  if (cso && cso.style.display !== "none") {
    e.preventDefault();
    hideCreateScheduleModal();
    return;
  }
  var eso = document.getElementById("edit-schedule-overlay");
  if (eso && eso.style.display !== "none") {
    e.preventDefault();
    hideEditScheduleModal();
    return;
  }
  var sro = document.getElementById("schedule-runs-overlay");
  if (sro && sro.style.display !== "none") {
    e.preventDefault();
    hideScheduleRunsModal();
    return;
  }
  var cf = document.getElementById("confirm-overlay");
  if (cf && cf.style.display !== "none") {
    e.preventDefault();
    hideConfirmModal();
    return;
  }
  // Governance modals
  var govOverlays = [
    ["create-role-overlay", hideCreateRoleModal],
    ["edit-role-overlay", hideEditRoleModal],
    ["user-roles-overlay", hideUserRolesModal],
    ["create-policy-overlay", hideCreatePolicyModal],
    ["edit-policy-overlay", hideEditPolicyModal],
    ["create-template-overlay", hideCreateTemplateModal],
    ["edit-template-overlay", hideEditTemplateModal],
    ["memory-detail-overlay", hideMemoryDetailModal],
    ["mcp-install-overlay", hideInstallMcpModal],
    ["mcp-detail-overlay", hideMcpDetailModal],
    ["mcp-import-overlay", hideImportMcpModal],
    ["mcp-create-overlay", hideCreateMcpModal],
    ["github-import-overlay", hideGitHubImportModal],
    ["model-create-overlay", hideCreateModelModal],
    ["create-ppolicy-overlay", hideCreatePromptPolicyModal],
    ["edit-ppolicy-overlay", hideEditPromptPolicyModal],
    ["create-hr-overlay", hideCreateHRModal],
    ["edit-hr-overlay", hideEditHRModal],
    ["create-ogp-overlay", hideCreateOGPModal],
    ["edit-ogp-overlay", hideEditOGPModal],
  ];
  for (var gi = 0; gi < govOverlays.length; gi++) {
    var govEl = document.getElementById(govOverlays[gi][0]);
    if (govEl && govEl.style.display !== "none") {
      e.preventDefault();
      govOverlays[gi][1]();
      return;
    }
  }
  // Close mobile sidebar drawer on Escape
  if (_mobileSidebarOpen && window.innerWidth <= 700) {
    e.preventDefault();
    _toggleMobileSidebar();
    var mt = document.getElementById("admin-mobile-toggle");
    if (mt) mt.focus();
    return;
  }
});

// Sidebar arrow key navigation (vertical)
(function () {
  var sidebar = document.getElementById("admin-sidebar");
  if (!sidebar) return;
  sidebar.addEventListener("keydown", function (e) {
    if (e.key !== "ArrowUp" && e.key !== "ArrowDown") return;
    e.preventDefault();
    var allNavs = document.querySelectorAll(
      '.admin-nav:not([style*="display: none"])',
    );
    var navOrder = [];
    for (var ni = 0; ni < allNavs.length; ni++) {
      navOrder.push(allNavs[ni].getAttribute("data-tab"));
    }
    if (navOrder.length === 0) return;
    var idx = navOrder.indexOf(_adminTab);
    if (e.key === "ArrowDown") idx = (idx + 1) % navOrder.length;
    else idx = (idx - 1 + navOrder.length) % navOrder.length;
    switchAdminTab(navOrder[idx]);
    var btn = document.querySelector(
      '.admin-nav[data-tab="' + navOrder[idx] + '"]',
    );
    if (btn) btn.focus();
  });
})();

// Sync sidebar aria-hidden/inert when crossing mobile/desktop breakpoint
(function () {
  var resizeTimer;
  window.addEventListener("resize", function () {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(function () {
      if (typeof currentView === "undefined" || currentView !== "admin") return;
      var sidebar = document.getElementById("admin-sidebar");
      if (!sidebar) return;
      var isMobile = window.innerWidth <= 700;
      var backdrop = document.getElementById("admin-sidebar-backdrop");
      if (!isMobile) {
        // Crossed into desktop: close drawer cleanly if it was open
        if (_mobileSidebarOpen) _toggleMobileSidebar();
        sidebar.removeAttribute("aria-hidden");
        sidebar.removeAttribute("inert");
        sidebar.classList.remove("collapsed", "open");
        if (backdrop) backdrop.classList.remove("visible");
      } else if (!_mobileSidebarOpen) {
        // Mobile with drawer closed: ensure collapsed state
        sidebar.setAttribute("aria-hidden", "true");
        sidebar.setAttribute("inert", "");
        sidebar.classList.add("collapsed");
        sidebar.classList.remove("open");
        if (backdrop) backdrop.classList.remove("visible");
      }
    }, 150);
  });
})();

// ---------------------------------------------------------------------------
// Confirm Modal (reusable styled replacement for confirm())
// ---------------------------------------------------------------------------

function showConfirmModal(title, message, actionLabel, callback) {
  _confirmCallbackFn = callback;
  _confirmTriggerEl = document.activeElement;
  document.getElementById("confirm-title").textContent = title;
  document.getElementById("confirm-message").textContent = message;
  var btn = document.getElementById("confirm-submit");
  btn.textContent = actionLabel;
  btn.disabled = false;
  var overlay = document.getElementById("confirm-overlay");
  overlay.style.display = "flex";
  _cfTrapHandler = _installTrap("confirm-overlay", "confirm-box");
  setTimeout(function () {
    btn.focus();
  }, 50);
}

function hideConfirmModal() {
  document.getElementById("confirm-overlay").style.display = "none";
  _cfTrapHandler = _removeTrap(_cfTrapHandler);
  if (
    _confirmTriggerEl &&
    _confirmTriggerEl.focus &&
    _confirmTriggerEl.isConnected
  ) {
    _confirmTriggerEl.focus();
  }
  _confirmCallbackFn = null;
  _confirmTriggerEl = null;
}

function _confirmCallback() {
  var fn = _confirmCallbackFn;
  _confirmCallbackFn = null;
  var btn = document.getElementById("confirm-submit");
  if (btn) btn.disabled = true;
  if (fn) fn();
  hideConfirmModal();
}

// ---------------------------------------------------------------------------
// Settings — form-based editor grouped by section
// ---------------------------------------------------------------------------

var _settingsOriginal = {}; // original values for dirty detection

// Section display order
var _settingsSectionOrder = [
  "model",
  "session",
  "tools",
  "server",
  "cluster",
  "channels",
  "mcp",
  "ratelimit",
  "health",
  "judge",
  "skills",
  "memory",
];

function _settingsSectionLabel(section) {
  var labels = {
    model: "Model",
    session: "Session",
    tools: "Tools",
    server: "Server",
    cluster: "Cluster",
    channels: "Channels",
    mcp: "MCP",
    ratelimit: "Rate Limiting",
    health: "Health",
    judge: "Judge",
    skills: "Skills",
    memory: "Memory",
  };
  return labels[section] || section;
}

// ---------------------------------------------------------------------------
// TLS tab
// ---------------------------------------------------------------------------

function loadTlsCerts() {
  var statusEl = document.getElementById("tls-ca-status");
  var listEl = document.getElementById("tls-cert-list");
  if (!statusEl || !listEl) return;

  // Fetch CA status and cert list in parallel
  Promise.all([
    authFetch("/v1/api/admin/tls/ca").then(function (r) {
      if (!r.ok) throw new Error("Failed");
      return r.json();
    }),
    authFetch("/v1/api/admin/tls/certs").then(function (r) {
      if (!r.ok) return { certs: [] };
      return r.json();
    }),
  ])
    .then(function (results) {
      var data = results[0];
      var certData = results[1];
      while (statusEl.firstChild) statusEl.removeChild(statusEl.firstChild);
      while (listEl.firstChild) listEl.removeChild(listEl.firstChild);

      if (!data.enabled) {
        var msg = document.createElement("div");
        msg.className = "dashboard-empty";
        msg.textContent =
          "TLS is not enabled. Set tls.enabled = true in Settings.";
        statusEl.appendChild(msg);
        return;
      }

      // CA status bar
      var bar = document.createElement("div");
      bar.className = "tls-ca-bar";
      var caLabel = document.createElement("span");
      caLabel.textContent = "CA: " + data.ca_cn;
      var countLabel = document.createElement("span");
      countLabel.textContent = "Certificates: " + data.cert_count;
      bar.appendChild(caLabel);
      bar.appendChild(countLabel);
      statusEl.appendChild(bar);

      var certs = certData.certs || [];
      if (certs.length === 0) {
        var empty = document.createElement("div");
        empty.className = "dashboard-empty";
        empty.textContent = "No certificates issued yet.";
        listEl.appendChild(empty);
        return;
      }

      // Cert rows
      certs.forEach(function (c) {
        var row = document.createElement("div");
        row.className = "admin-row";
        row.setAttribute("role", "listitem");

        var colDomain = document.createElement("span");
        colDomain.className = "admin-col";
        colDomain.textContent = c.domain;

        var colSans = document.createElement("span");
        colSans.className = "admin-col";
        colSans.textContent = (c.domains || [c.domain]).join(", ");

        var colIssued = document.createElement("span");
        colIssued.className = "admin-col";
        colIssued.textContent = (c.issued_at || "")
          .slice(0, 16)
          .replace("T", " ");

        var colExpires = document.createElement("span");
        colExpires.className = "admin-col";
        var expires = new Date(c.expires_at);
        var isExpired = expires < new Date();
        colExpires.textContent =
          (isExpired ? "EXPIRED " : "") +
          (c.expires_at || "").slice(0, 16).replace("T", " ");
        if (isExpired) colExpires.style.color = "var(--red)";

        var colActions = document.createElement("span");
        colActions.className = "admin-col admin-col-actions";
        var renewBtn = document.createElement("button");
        renewBtn.className = "admin-btn-action";
        renewBtn.textContent = "Renew";
        renewBtn.setAttribute(
          "aria-label",
          "Renew certificate for " + c.domain,
        );
        renewBtn.onclick = function () {
          tlsRenewCert(c.domain);
        };
        var deleteBtn = document.createElement("button");
        deleteBtn.className = "admin-btn-danger";
        deleteBtn.textContent = "Delete";
        deleteBtn.setAttribute(
          "aria-label",
          "Delete certificate for " + c.domain,
        );
        deleteBtn.onclick = function () {
          tlsDeleteCert(c.domain);
        };
        colActions.appendChild(renewBtn);
        colActions.appendChild(deleteBtn);

        row.appendChild(colDomain);
        row.appendChild(colSans);
        row.appendChild(colIssued);
        row.appendChild(colExpires);
        row.appendChild(colActions);
        listEl.appendChild(row);
      });
    })
    .catch(function () {
      while (statusEl.firstChild) statusEl.removeChild(statusEl.firstChild);
      while (listEl.firstChild) listEl.removeChild(listEl.firstChild);
      var errMsg = document.createElement("div");
      errMsg.className = "dashboard-empty";
      errMsg.textContent = "Failed to load TLS status";
      statusEl.appendChild(errMsg);
    });
}

function tlsRenewCert(domain) {
  showConfirmModal(
    "Renew Certificate",
    "Force renew certificate for \u2018" + domain + "\u2019?",
    "Renew",
    function () {
      authFetch(
        "/v1/api/admin/tls/certs/" + encodeURIComponent(domain) + "/renew",
        { method: "POST" },
      )
        .then(function (r) {
          if (!r.ok) throw new Error("Renew failed");
          showToast("Certificate renewed for " + domain);
          loadTlsCerts();
        })
        .catch(function () {
          showToast("Failed to renew certificate", "error");
        });
    },
  );
}

function tlsDeleteCert(domain) {
  showConfirmModal(
    "Delete Certificate",
    "Delete certificate for \u2018" + domain + "\u2019? This cannot be undone.",
    "Delete",
    function () {
      authFetch("/v1/api/admin/tls/certs/" + encodeURIComponent(domain), {
        method: "DELETE",
      })
        .then(function (r) {
          if (!r.ok) throw new Error("Delete failed");
          showToast("Certificate deleted for " + domain);
          loadTlsCerts();
        })
        .catch(function () {
          showToast("Failed to delete certificate", "error");
        });
    },
  );
}

// ---------------------------------------------------------------------------
// Settings tab
// ---------------------------------------------------------------------------

function loadSettings() {
  var el = document.getElementById("admin-settings-content");
  if (!el) return;

  Promise.all([
    authFetch("/v1/api/admin/settings").then(function (r) {
      if (!r.ok) throw new Error("Failed to load settings");
      return r.json();
    }),
    authFetch("/v1/api/admin/settings/schema").then(function (r) {
      if (!r.ok) throw new Error("Failed to load schema");
      return r.json();
    }),
    authFetch("/v1/api/admin/model-definitions").then(function (r) {
      if (!r.ok) return { models: [] };
      return r.json();
    }),
  ])
    .then(function (results) {
      var valuesArr = results[0].settings || [];
      var schemaArr = results[1].schema || [];
      var modelDefs = results[2].models || [];

      // Build schema lookup
      var schemaMap = {};
      for (var i = 0; i < schemaArr.length; i++) {
        schemaMap[schemaArr[i].key] = schemaArr[i];
      }

      // Merge values + schema
      var merged = {};
      for (var j = 0; j < valuesArr.length; j++) {
        var v = valuesArr[j];
        if (v.key.startsWith("judge.")) continue;
        var s = schemaMap[v.key] || {};
        merged[v.key] = {
          key: v.key,
          value: v.value,
          source: v.source,
          type: v.type || s.type || "str",
          default_value: s.default !== undefined ? s.default : "",
          description: v.description || s.description || "",
          section: v.section || s.section || "",
          is_secret: v.is_secret || false,
          min_value: s.min_value,
          max_value: s.max_value,
          choices: s.choices || null,
          restart_required: v.restart_required || false,
          changed_by: v.changed_by || "",
          updated: v.updated || "",
          help: s.help || "",
          reference_url: s.reference_url || "",
        };
      }

      // Inject dynamic choices for model alias settings from model definitions.
      var enabledAliases = [""];
      for (var m = 0; m < modelDefs.length; m++) {
        if (modelDefs[m].enabled) enabledAliases.push(modelDefs[m].alias);
      }
      if (enabledAliases.length > 1) {
        for (var ak = 0; ak < ALIAS_SETTING_KEYS.length; ak++) {
          var aliasKey = ALIAS_SETTING_KEYS[ak];
          if (merged[aliasKey]) merged[aliasKey].choices = enabledAliases;
        }
      }

      _settingsOriginal = {};

      // Group by section
      var grouped = {};
      var keys = Object.keys(merged);
      for (var k = 0; k < keys.length; k++) {
        var item = merged[keys[k]];
        var sec = item.section || "other";
        if (!grouped[sec]) grouped[sec] = [];
        grouped[sec].push(item);
      }

      _renderSettings(el, grouped);
    })
    .catch(function (err) {
      // NOTE: escapeHtml sanitises err.message before insertion.
      el.innerHTML =
        '<div class="dashboard-empty">Failed to load settings: ' +
        escapeHtml(err.message || String(err)) +
        "</div>";
    });
}

function _renderSettings(container, grouped) {
  var html = "";

  for (var i = 0; i < _settingsSectionOrder.length; i++) {
    var sec = _settingsSectionOrder[i];
    var items = grouped[sec];
    if (!items || items.length === 0) continue;

    html +=
      '<div class="settings-section" data-section="' +
      sec +
      '" data-collapsed>';
    html +=
      '<div class="settings-section-header" onclick="_toggleSettingsSection(this)" onkeydown="_onSettingsHeaderKey(event,this)" role="button" tabindex="0" aria-expanded="false" aria-controls="settings-body-' +
      sec +
      '">';
    html += "<span>" + _settingsSectionLabel(sec) + "</span>";
    html += "</div>";
    html +=
      '<div class="settings-section-body" id="settings-body-' + sec + '">';

    for (var j = 0; j < items.length; j++) {
      html += _renderSettingRow(items[j]);
    }

    html += "</div></div>";
  }

  // Render any sections not in the explicit order
  var allSections = Object.keys(grouped);
  for (var s = 0; s < allSections.length; s++) {
    if (_settingsSectionOrder.indexOf(allSections[s]) === -1) {
      var extra = grouped[allSections[s]];
      html +=
        '<div class="settings-section" data-section="' +
        allSections[s] +
        '" data-collapsed>';
      html +=
        '<div class="settings-section-header" onclick="_toggleSettingsSection(this)" onkeydown="_onSettingsHeaderKey(event,this)" role="button" tabindex="0" aria-expanded="false" aria-controls="settings-body-' +
        allSections[s] +
        '">';
      html += "<span>" + _settingsSectionLabel(allSections[s]) + "</span>";
      html += "</div>";
      html +=
        '<div class="settings-section-body" id="settings-body-' +
        allSections[s] +
        '">';
      for (var x = 0; x < extra.length; x++) {
        html += _renderSettingRow(extra[x]);
      }
      html += "</div></div>";
    }
  }

  container.innerHTML = html;

  // Store original values for dirty detection
  var inputs = container.querySelectorAll("[data-setting-key]");
  for (var n = 0; n < inputs.length; n++) {
    var inp = inputs[n];
    var key = inp.getAttribute("data-setting-key");
    if (inp.type === "checkbox") {
      _settingsOriginal[key] = inp.checked;
    } else {
      _settingsOriginal[key] = inp.value;
    }
  }
}

function _renderSettingRow(item) {
  var shortKey =
    item.key.indexOf(".") !== -1
      ? item.key.substring(item.key.indexOf(".") + 1)
      : item.key;
  var escapedKey = escapeHtml(item.key);
  var escapedShort = escapeHtml(shortKey);
  var escapedDesc = escapeHtml(item.description);

  var html = '<div class="settings-row" data-row-key="' + escapedKey + '">';

  // Label column
  html += '<div class="settings-label-col">';
  html += '<div class="settings-label">';
  html += escapeHtml(shortKey);
  if (item.help) {
    html +=
      ' <button class="settings-help-btn" onclick="_toggleSettingsHelp(event, this)" ' +
      'aria-label="Help for ' +
      escapedShort +
      '" aria-expanded="false" title="More info">?</button>';
  }
  html += "</div>";
  if (item.description) {
    html += '<div class="settings-desc">' + escapedDesc + "</div>";
  }
  if (item.help) {
    html += '<div class="settings-help-popover" style="display:none">';
    html +=
      '<span class="settings-help-text">' + escapeHtml(item.help) + "</span>";
    if (item.reference_url) {
      html +=
        ' <a href="' +
        escapeHtml(item.reference_url) +
        '" target="_blank" rel="noopener" class="settings-help-ref">learn more</a>';
    }
    html += "</div>";
  }
  html += "</div>";

  // Input column
  html += '<div class="settings-input">';
  if (item.is_secret) {
    html +=
      '<input type="password" data-setting-key="' +
      escapedKey +
      '" aria-label="Secret value for ' +
      escapedShort +
      '" autocomplete="off" value="" placeholder="' +
      (item.source === "storage" ? "***" : "not set") +
      '" oninput="_onSettingChange(\'' +
      escapedKey +
      "')\">";
  } else if (item.type === "bool") {
    var checked =
      item.value === true || item.value === "true" ? " checked" : "";
    html +=
      '<label class="settings-toggle"><input type="checkbox" data-setting-key="' +
      escapedKey +
      '" aria-label="' +
      escapedShort +
      '"' +
      checked +
      " onchange=\"_onSettingChange('" +
      escapedKey +
      '\')"><span class="settings-toggle-slider"></span></label>';
  } else if (item.choices && item.choices.length > 0) {
    html +=
      '<select data-setting-key="' +
      escapedKey +
      '" aria-label="' +
      escapedShort +
      '" onchange="_onSettingChange(\'' +
      escapedKey +
      "')\">";
    for (var c = 0; c < item.choices.length; c++) {
      var sel = item.choices[c] === String(item.value) ? " selected" : "";
      var label;
      if (item.choices[c] !== "") {
        label = escapeHtml(item.choices[c]);
      } else if (ALIAS_SETTING_KEYS.indexOf(item.key) !== -1) {
        label = "(server default)";
      } else if (INHERIT_EMPTY_LABEL_KEYS.indexOf(item.key) !== -1) {
        label = "(inherit)";
      } else {
        label = "(none)";
      }
      html +=
        '<option value="' +
        escapeHtml(item.choices[c]) +
        '"' +
        sel +
        ">" +
        label +
        "</option>";
    }
    html += "</select>";
  } else if (item.type === "int" || item.type === "float") {
    var step = item.type === "float" ? "0.01" : "1";
    var minAttr =
      item.min_value !== null && item.min_value !== undefined
        ? ' min="' + item.min_value + '"'
        : "";
    var maxAttr =
      item.max_value !== null && item.max_value !== undefined
        ? ' max="' + item.max_value + '"'
        : "";
    html +=
      '<input type="number" data-setting-key="' +
      escapedKey +
      '" aria-label="' +
      escapedShort +
      '" value="' +
      escapeHtml(String(item.value != null ? item.value : "")) +
      '" step="' +
      step +
      '"' +
      minAttr +
      maxAttr +
      " oninput=\"_onSettingChange('" +
      escapedKey +
      "')\">";
  } else {
    // str
    html +=
      '<input type="text" data-setting-key="' +
      escapedKey +
      '" aria-label="' +
      escapedShort +
      '" value="' +
      escapeHtml(String(item.value != null ? item.value : "")) +
      '" oninput="_onSettingChange(\'' +
      escapedKey +
      "')\">";
  }
  html += "</div>";

  // Actions column
  html += '<div class="settings-actions">';

  // Restart badge (left of source badge, hidden until dirty or post-save)
  if (item.restart_required) {
    html +=
      '<span class="settings-restart-badge" data-restart-key="' +
      escapedKey +
      '">restart</span>';
  }

  // Source badge
  if (item.source === "storage") {
    html += '<span class="scope-badge scope-write">storage</span>';
  } else {
    html += '<span class="scope-badge settings-badge-default">default</span>';
  }

  // Save button (hidden until value changes)
  html +=
    '<button class="settings-save-btn" data-save-key="' +
    escapedKey +
    '" onclick="_saveSettingValue(\'' +
    escapedKey +
    "')\">save</button>";

  // Reset link (when stored — including secrets, to clear legacy overrides)
  if (item.source === "storage") {
    html +=
      '<button class="settings-reset-btn" data-reset-key="' +
      escapedKey +
      '" onclick="_resetSetting(\'' +
      escapedKey +
      "')\">reset</button>";
  }

  html += "</div>";
  html += "</div>";
  return html;
}

function _toggleSettingsHelp(e, btn) {
  e.stopPropagation();
  var popover = btn
    .closest(".settings-label-col")
    .querySelector(".settings-help-popover");
  if (!popover) return;
  var isVisible = popover.style.display !== "none";
  // Close any other open popovers and reset their buttons
  _closeAllSettingsHelp(popover);
  popover.style.display = isVisible ? "none" : "";
  btn.setAttribute("aria-expanded", isVisible ? "false" : "true");
}

function _closeAllSettingsHelp(except) {
  var allOpen = document.querySelectorAll('.settings-help-popover[style=""]');
  for (var i = 0; i < allOpen.length; i++) {
    if (allOpen[i] !== except) {
      allOpen[i].style.display = "none";
      var col = allOpen[i].closest(".settings-label-col");
      if (col) {
        var helpBtn = col.querySelector(".settings-help-btn");
        if (helpBtn) helpBtn.setAttribute("aria-expanded", "false");
      }
    }
  }
}

function _onSettingsHeaderKey(e, el) {
  if ((e.key === "Enter" || e.key === " ") && !e.repeat) {
    e.preventDefault();
    _toggleSettingsSection(el);
  }
}

function _toggleSettingsSection(headerEl) {
  var section = headerEl.parentElement;
  if (section.hasAttribute("data-collapsed")) {
    section.removeAttribute("data-collapsed");
    headerEl.setAttribute("aria-expanded", "true");
  } else {
    section.setAttribute("data-collapsed", "");
    headerEl.setAttribute("aria-expanded", "false");
  }
}

function _onSettingChange(key) {
  var inp = document.querySelector('[data-setting-key="' + key + '"]');
  var saveBtn = document.querySelector('[data-save-key="' + key + '"]');
  if (!inp || !saveBtn) return;

  var current;
  if (inp.type === "checkbox") {
    current = inp.checked;
  } else {
    current = inp.value;
  }

  var orig = _settingsOriginal[key];
  var dirty;
  if (inp.type === "checkbox") {
    dirty = current !== orig;
  } else if (inp.type === "number" && current !== "" && orig !== "") {
    // Compare numerically to avoid false positives (0.1 vs 0.10)
    dirty = Number(current) !== Number(orig);
  } else {
    dirty = String(current) !== String(orig);
  }

  // Disable save for empty number fields (server will reject)
  var emptyNumber = inp.type === "number" && current === "";
  if (dirty && !emptyNumber) {
    saveBtn.classList.add("visible");
  } else {
    saveBtn.classList.remove("visible");
  }

  // Show/hide restart badge alongside dirty state (but keep it if already saved)
  var restartBadge = document.querySelector('[data-restart-key="' + key + '"]');
  if (restartBadge && !restartBadge.classList.contains("saved")) {
    restartBadge.classList.toggle("visible", dirty);
  }
}

function _saveSettingValue(key) {
  var inp = document.querySelector('[data-setting-key="' + key + '"]');
  var saveBtn = document.querySelector('[data-save-key="' + key + '"]');
  if (!inp) return;

  var value;
  if (inp.type === "checkbox") {
    value = inp.checked;
  } else if (inp.type === "number") {
    if (inp.value === "") {
      showToast("Value is required");
      return;
    }
    value = Number(inp.value);
  } else if (inp.type === "password") {
    if (inp.value === "") {
      // Nothing to save — user didn't enter a value.
      if (saveBtn) saveBtn.classList.remove("visible");
      return;
    }
    value = inp.value;
  } else {
    value = inp.value;
  }

  if (saveBtn) {
    saveBtn.textContent = "saving\u2026";
    saveBtn.disabled = true;
  }

  authFetch("/v1/api/admin/settings/" + encodeURIComponent(key), {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ value: value }),
  })
    .then(function (r) {
      if (!r.ok)
        return r.json().then(function (d) {
          throw new Error(d.error || "Save failed");
        });
      return r.json();
    })
    .then(function () {
      // Update original so dirty detection resets
      if (inp.type === "checkbox") {
        _settingsOriginal[key] = inp.checked;
      } else if (inp.type === "password") {
        // Clear the field after save; show "***" placeholder.
        inp.value = "";
        inp.placeholder = "***";
        _settingsOriginal[key] = "";
      } else {
        _settingsOriginal[key] = inp.value;
      }
      if (saveBtn) {
        saveBtn.textContent = "save";
        saveBtn.disabled = false;
        saveBtn.classList.remove("visible");
      }

      // Update source badge to "storage"
      var row = document.querySelector('[data-row-key="' + key + '"]');
      if (row) {
        var badge = row.querySelector(".scope-badge");
        if (badge) {
          badge.className = "scope-badge scope-write";
          badge.textContent = "storage";
        }
        // Add reset button if not present
        if (!row.querySelector('[data-reset-key="' + key + '"]')) {
          var actions = row.querySelector(".settings-actions");
          if (actions) {
            var resetBtn = document.createElement("button");
            resetBtn.className = "settings-reset-btn";
            resetBtn.setAttribute("data-reset-key", key);
            resetBtn.textContent = "reset";
            resetBtn.onclick = function () {
              _resetSetting(key);
            };
            actions.appendChild(resetBtn);
          }
        }
      }

      // Show restart badge post-save (stays until page reload = restart)
      var restartBadge = document.querySelector(
        '[data-restart-key="' + key + '"]',
      );
      if (restartBadge) {
        restartBadge.classList.add("visible");
        restartBadge.classList.add("saved");
      }

      // Brief row flash for visual feedback
      if (
        row &&
        !window.matchMedia("(prefers-reduced-motion: reduce)").matches
      ) {
        row.style.background = "var(--accent-glow)";
        setTimeout(function () {
          row.style.background = "";
        }, 600);
      }

      showToast(
        "Saved " + key + (restartBadge ? " \u2014 restart required" : ""),
      );

      // If this is a theme setting, apply it immediately.  Don't call
      // onThemeChange — it would fire a redundant PUT since the settings
      // save above already persisted the value.
      if (key === "interface.theme") {
        var isLight = value === "light";
        document.documentElement.dataset.theme = isLight ? "light" : "";
        localStorage.setItem(
          "turnstone_interface.theme",
          isLight ? "light" : "dark",
        );
        var themeBtn = document.getElementById("theme-toggle");
        if (themeBtn) {
          themeBtn.textContent = isLight ? "\u2600" : "\u263E";
          themeBtn.title = isLight
            ? "Switch to dark theme"
            : "Switch to light theme";
          themeBtn.setAttribute(
            "aria-label",
            isLight ? "Switch to dark theme" : "Switch to light theme",
          );
        }
      }
    })
    .catch(function (err) {
      if (saveBtn) {
        saveBtn.textContent = "save";
        saveBtn.disabled = false;
      }
      showToast("Error: " + (err.message || err));
    });
}

function _resetSetting(key) {
  showConfirmModal(
    "Reset Setting",
    "Reset \u2018" +
      key +
      "\u2019 to its default value? The stored override will be removed.",
    "Reset",
    function () {
      authFetch("/v1/api/admin/settings/" + encodeURIComponent(key), {
        method: "DELETE",
      })
        .then(function (r) {
          if (!r.ok)
            return r.json().then(function (d) {
              throw new Error(d.error || "Reset failed");
            });
          showToast("Reset " + key + " to default");
          loadSettings();
        })
        .catch(function (err) {
          showToast("Error: " + (err.message || err));
        });
    },
  );
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function _showModalError(el, msg) {
  el.textContent = msg;
  el.style.display = "block";
}

/* ── MCP Servers tab ─────────────────────────────────────────────────────── */

var _mcpServers = [];
var _mcpCreateTrap = null;
var _mcpCreateTrigger = null;
var _mcpImportTrap = null;
var _mcpImportTrigger = null;
var _mcpDetailTrap = null;
var _mcpDetailTrigger = null;
var _mcpInstallTrap = null;
var _mcpInstallTrigger = null;
var _mcpInstallServer = null;
var _mcpCurrentView = "servers";
var _registryResults = [];
var _registryCursor = null;
var _registryQuery = "";

function loadAdminMcp() {
  authFetch("/v1/api/admin/mcp-servers")
    .then(function (r) {
      if (!r.ok) throw new Error("Failed");
      return r.json();
    })
    .then(function (data) {
      _mcpServers = data.servers || [];
      _renderMcpServers(_mcpServers);
    })
    .catch(function () {
      document.getElementById("admin-mcp-table").innerHTML =
        '<div class="dashboard-empty">Failed to load MCP servers</div>';
    });
}

function _renderMcpServers(items) {
  var el = document.getElementById("admin-mcp-table");
  if (!items.length) {
    el.innerHTML =
      '<div class="dashboard-empty">No MCP servers configured</div>';
    return;
  }
  var html = "";
  for (var i = 0; i < items.length; i++) {
    var s = items[i];
    var statusEntries = s.status || {};
    var nodeIds = Object.keys(statusEntries);
    var anyConnected = false;
    var anyError = false;
    var firstError = "";
    var totalTools = 0,
      totalRes = 0,
      totalPrompts = 0;
    for (var j = 0; j < nodeIds.length; j++) {
      var ns = statusEntries[nodeIds[j]];
      if (ns.connected) {
        anyConnected = true;
        totalTools += ns.tools || 0;
        totalRes += ns.resources || 0;
        totalPrompts += ns.prompts || 0;
      }
      if (ns.error) {
        anyError = true;
        if (!firstError) firstError = ns.error;
      }
    }

    var dotClass = "mcp-status-dot disabled";
    var rowClass = "mcp-row-disabled";
    var statusText = "disabled";
    if (!s.enabled) {
      statusText = "disabled";
    } else if (anyConnected) {
      dotClass = "mcp-status-dot connected";
      rowClass = "mcp-row-connected";
      statusText = "connected";
    } else if (anyError) {
      dotClass = "mcp-status-dot error";
      rowClass = "mcp-row-error";
      statusText = "error";
    } else if (s.enabled && s.source !== "config" && nodeIds.length === 0) {
      dotClass = "mcp-status-dot connecting";
      rowClass = "mcp-row-disabled";
      statusText = "connecting";
    } else {
      dotClass = "mcp-status-dot disabled";
      rowClass = "mcp-row-disabled";
      statusText = "idle";
    }

    var transportCls =
      s.transport === "stdio" ? "mcp-transport-stdio" : "mcp-transport-http";
    var toolsVal = anyConnected
      ? totalTools
      : '<span class="mcp-count-dim">--</span>';
    var resVal = anyConnected
      ? totalRes
      : '<span class="mcp-count-dim">--</span>';
    var promptsVal = anyConnected
      ? totalPrompts
      : '<span class="mcp-count-dim">--</span>';

    var isConfig = s.source === "config";
    var isRegistry = !!s.registry_name;
    var nameBadge = isConfig
      ? ' <span class="scope-badge scope-config">config</span>'
      : isRegistry
        ? ' <span class="scope-badge scope-registry">registry</span>'
        : ' <span class="scope-badge scope-manual">manual</span>';
    var detailAttr = isConfig
      ? 'data-mcp-detail-name="' + escapeHtml(s.name) + '"'
      : 'data-mcp-detail="' + escapeHtml(s.server_id) + '"';
    var actions = isConfig
      ? ""
      : '<button class="admin-btn-action" data-mcp-edit="' +
        escapeHtml(s.server_id) +
        '">edit</button>' +
        '<button class="admin-btn-danger" data-mcp-delete="' +
        escapeHtml(s.server_id) +
        '" data-mcp-name="' +
        escapeHtml(s.name) +
        '">del</button>';

    html +=
      '<div class="admin-row mcp-grid ' +
      rowClass +
      '" role="listitem">' +
      '<span class="admin-col admin-col-mname"><a href="#" ' +
      detailAttr +
      ">" +
      escapeHtml(s.name) +
      "</a>" +
      nameBadge +
      "</span>" +
      '<span class="admin-col admin-col-mtransport"><span class="mcp-transport-badge ' +
      transportCls +
      '">' +
      (s.transport === "streamable-http" ? "remote" : escapeHtml(s.transport)) +
      "</span></span>" +
      '<span class="admin-col admin-col-mtools">' +
      toolsVal +
      "</span>" +
      '<span class="admin-col admin-col-mres">' +
      resVal +
      "</span>" +
      '<span class="admin-col admin-col-mprompts">' +
      promptsVal +
      "</span>" +
      '<span class="admin-col admin-col-mstatus"' +
      (firstError ? ' title="' + escapeHtml(firstError) + '"' : "") +
      '><span class="' +
      dotClass +
      '" aria-hidden="true"></span>' +
      escapeHtml(statusText) +
      "</span>" +
      '<span class="admin-col admin-col-mactions">' +
      actions +
      "</span></div>";
  }
  el.innerHTML = html;

  // Bind event handlers
  el.querySelectorAll("[data-mcp-detail]").forEach(function (a) {
    a.addEventListener("click", function (e) {
      e.preventDefault();
      showMcpDetailModal(this.getAttribute("data-mcp-detail"));
    });
  });
  el.querySelectorAll("[data-mcp-detail-name]").forEach(function (a) {
    a.addEventListener("click", function (e) {
      e.preventDefault();
      showMcpDetailByName(this.getAttribute("data-mcp-detail-name"));
    });
  });
  el.querySelectorAll("[data-mcp-edit]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      showEditMcpModal(this.getAttribute("data-mcp-edit"));
    });
  });
  el.querySelectorAll("[data-mcp-delete]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var sid = this.getAttribute("data-mcp-delete");
      var sname = this.getAttribute("data-mcp-name");
      showConfirmModal(
        "Delete MCP Server",
        'Delete server "' + sname + '"?',
        "Delete",
        function () {
          authFetch("/v1/api/admin/mcp-servers/" + sid, { method: "DELETE" })
            .then(function (r) {
              if (!r.ok) throw new Error();
              return r.json();
            })
            .then(function () {
              showToast("Server deleted");
              _flagMcpSyncPending();
              loadAdminMcp();
            })
            .catch(function () {
              showToast("Failed to delete server");
            });
        },
      );
    });
  });
}

function toggleMcpTransport() {
  var v = document.getElementById("mcp-transport").value;
  document.getElementById("mcp-stdio-fields").style.display =
    v === "stdio" ? "" : "none";
  document.getElementById("mcp-http-fields").style.display =
    v === "streamable-http" ? "" : "none";
}

function showCreateMcpModal() {
  _mcpCreateTrigger = document.activeElement;
  var ov = document.getElementById("mcp-create-overlay");
  ov.style.display = "flex";
  document.getElementById("mcp-edit-id").value = "";
  document.getElementById("mcp-create-title").textContent = "Add MCP Server";
  document.getElementById("mcp-create-submit").textContent = "Create";
  document.getElementById("mcp-name").value = "";
  document.getElementById("mcp-transport").value = "stdio";
  document.getElementById("mcp-command").value = "";
  document.getElementById("mcp-args").value = "";
  document.getElementById("mcp-env").value = "";
  document.getElementById("mcp-url").value = "";
  document.getElementById("mcp-headers").value = "";
  document.getElementById("mcp-auto-approve").checked = false;
  document.getElementById("mcp-enabled").checked = true;
  document.getElementById("mcp-create-error").style.display = "none";
  toggleMcpTransport();
  document.getElementById("mcp-name").focus();
  _mcpCreateTrap = _installTrap("mcp-create-overlay", "mcp-create-box");
}

function showEditMcpModal(serverId) {
  // Fetch with reveal=true to get actual secret values for editing
  authFetch("/v1/api/admin/mcp-servers/" + serverId + "?reveal=true")
    .then(function (r) {
      if (!r.ok) throw new Error("Failed to load server");
      return r.json();
    })
    .then(function (s) {
      showCreateMcpModal();
      document.getElementById("mcp-edit-id").value = serverId;
      document.getElementById("mcp-create-title").textContent =
        "Edit MCP Server";
      document.getElementById("mcp-create-submit").textContent = "Save";
      document.getElementById("mcp-name").value = s.name;
      document.getElementById("mcp-transport").value = s.transport;
      document.getElementById("mcp-command").value = s.command || "";
      try {
        var argsList = JSON.parse(s.args || "[]");
        document.getElementById("mcp-args").value = argsList.join("\n");
      } catch (e) {
        document.getElementById("mcp-args").value = "";
      }
      try {
        var envObj = JSON.parse(s.env || "{}");
        document.getElementById("mcp-env").value = Object.keys(envObj)
          .map(function (k) {
            return k + "=" + envObj[k];
          })
          .join("\n");
      } catch (e) {
        document.getElementById("mcp-env").value = "";
      }
      document.getElementById("mcp-url").value = s.url || "";
      try {
        var hdrObj = JSON.parse(s.headers || "{}");
        document.getElementById("mcp-headers").value = Object.keys(hdrObj)
          .map(function (k) {
            return k + ": " + hdrObj[k];
          })
          .join("\n");
      } catch (e) {
        document.getElementById("mcp-headers").value = "";
      }
      document.getElementById("mcp-auto-approve").checked =
        s.auto_approve || false;
      document.getElementById("mcp-enabled").checked = s.enabled !== false;
      toggleMcpTransport();
    })
    .catch(function () {
      showToast("Failed to load server details");
    });
}

function hideCreateMcpModal() {
  document.getElementById("mcp-create-overlay").style.display = "none";
  _mcpCreateTrap = _removeTrap(_mcpCreateTrap);
  if (_mcpCreateTrigger && _mcpCreateTrigger.focus) _mcpCreateTrigger.focus();
  _mcpCreateTrigger = null;
}

function _parseMcpForm() {
  var name = document.getElementById("mcp-name").value.trim();
  var transport = document.getElementById("mcp-transport").value;
  if (!name) return { error: "Name is required" };
  if (!/^[a-zA-Z0-9._-]+$/.test(name))
    return { error: "Name must match [a-zA-Z0-9._-]+" };
  if (name.indexOf("__") >= 0) return { error: "Name must not contain '__'" };

  var payload = {
    name: name,
    transport: transport,
    auto_approve: document.getElementById("mcp-auto-approve").checked,
    enabled: document.getElementById("mcp-enabled").checked,
  };

  if (transport === "stdio") {
    payload.command = document.getElementById("mcp-command").value.trim();
    var argsText = document.getElementById("mcp-args").value.trim();
    payload.args = argsText
      ? argsText
          .split("\n")
          .map(function (l) {
            return l.trim();
          })
          .filter(Boolean)
      : [];
    var envText = document.getElementById("mcp-env").value.trim();
    var envObj = {};
    if (envText) {
      envText.split("\n").forEach(function (line) {
        var eq = line.indexOf("=");
        if (eq > 0)
          envObj[line.substring(0, eq).trim()] = line.substring(eq + 1).trim();
      });
    }
    payload.env = envObj;
  } else {
    payload.url = document.getElementById("mcp-url").value.trim();
    var hdrText = document.getElementById("mcp-headers").value.trim();
    var hdrObj = {};
    if (hdrText) {
      hdrText.split("\n").forEach(function (line) {
        var colon = line.indexOf(":");
        if (colon > 0)
          hdrObj[line.substring(0, colon).trim()] = line
            .substring(colon + 1)
            .trim();
      });
    }
    payload.headers = hdrObj;
  }
  return payload;
}

function submitCreateMcp() {
  var form = _parseMcpForm();
  if (form.error) {
    var e = document.getElementById("mcp-create-error");
    e.textContent = form.error;
    e.style.display = "";
    return;
  }
  var editId = document.getElementById("mcp-edit-id").value;
  var method = editId ? "PUT" : "POST";
  var url = editId
    ? "/v1/api/admin/mcp-servers/" + editId
    : "/v1/api/admin/mcp-servers";

  document.getElementById("mcp-create-submit").disabled = true;
  authFetch(url, {
    method: method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(form),
  })
    .then(function (r) {
      if (!r.ok)
        return r.json().then(function (d) {
          throw new Error(d.error || "Failed");
        });
      return r.json();
    })
    .then(function () {
      hideCreateMcpModal();
      showToast(editId ? "Server updated" : "Server created");
      _flagMcpSyncPending();
      loadAdminMcp();
    })
    .catch(function (e) {
      var el = document.getElementById("mcp-create-error");
      el.textContent = e.message;
      el.style.display = "";
    })
    .finally(function () {
      document.getElementById("mcp-create-submit").disabled = false;
    });
}

function _flagMcpSyncPending() {
  var btn = document.getElementById("mcp-sync-btn");
  if (btn) btn.classList.add("mcp-sync-pending");
}

function _clearMcpSyncPending() {
  var btn = document.getElementById("mcp-sync-btn");
  if (btn) btn.classList.remove("mcp-sync-pending");
}

function reloadMcpNodes() {
  authFetch("/v1/api/admin/mcp-servers/reload", { method: "POST" })
    .then(function (r) {
      if (!r.ok) throw new Error();
      return r.json();
    })
    .then(function (data) {
      var results = data.results || {};
      var nodeIds = Object.keys(results);
      var totalAdded = 0,
        totalRemoved = 0;
      for (var i = 0; i < nodeIds.length; i++) {
        var nr = results[nodeIds[i]];
        totalAdded += (nr.added || []).length;
        totalRemoved += (nr.removed || []).length;
      }
      var msg = "Reload sent to " + nodeIds.length + " node(s)";
      if (totalAdded) msg += ", +" + totalAdded + " added";
      if (totalRemoved) msg += ", -" + totalRemoved + " removed";
      showToast(msg);
      _clearMcpSyncPending();
      setTimeout(loadAdminMcp, 1500);
    })
    .catch(function () {
      showToast("Failed to reload nodes");
    });
}

function showMcpDetailByName(name) {
  for (var i = 0; i < _mcpServers.length; i++) {
    if (_mcpServers[i].name === name) {
      return _openMcpDetail(_mcpServers[i]);
    }
  }
}

function showMcpDetailModal(serverId) {
  for (var i = 0; i < _mcpServers.length; i++) {
    if (_mcpServers[i].server_id === serverId) {
      return _openMcpDetail(_mcpServers[i]);
    }
  }
}

function _openMcpDetail(s) {
  if (!s) return;
  _mcpDetailTrigger = document.activeElement;

  var html = '<div class="modal-columns">';
  html += '<div class="modal-col">';
  html += '<div class="mcp-detail-section"><h3>Configuration</h3>';
  html +=
    '<p style="font-size:12px;color:var(--fg-dim)">Transport: <span class="mcp-transport-badge ' +
    (s.transport === "stdio" ? "mcp-transport-stdio" : "mcp-transport-http") +
    '">' +
    escapeHtml(s.transport) +
    "</span></p>";
  if (s.transport === "stdio") {
    html +=
      '<p style="font-size:12px;color:var(--fg-dim)">Command: <code>' +
      escapeHtml(s.command || "") +
      "</code></p>";
    try {
      var a = JSON.parse(s.args || "[]");
      if (a.length)
        html +=
          '<p style="font-size:12px;color:var(--fg-dim)">Args: <code>' +
          escapeHtml(a.join(" ")) +
          "</code></p>";
    } catch (e) {}
  } else {
    html +=
      '<p style="font-size:12px;color:var(--fg-dim)">URL: <code>' +
      escapeHtml(s.url || "") +
      "</code></p>";
  }
  html += "</div>";
  if (s.registry_name) {
    html += '<div class="mcp-detail-section"><h3>Registry</h3>';
    html +=
      '<p style="font-size:12px;color:var(--fg-dim)">Name: <code>' +
      escapeHtml(s.registry_name) +
      "</code></p>";
    if (s.registry_version) {
      html +=
        '<p style="font-size:12px;color:var(--fg-dim)">Version: <code>' +
        escapeHtml(s.registry_version) +
        "</code></p>";
    }
    try {
      var meta =
        typeof s.registry_meta === "string"
          ? JSON.parse(s.registry_meta)
          : s.registry_meta || {};
      if (meta.description) {
        html +=
          '<p style="font-size:12px;color:var(--fg-dim)">' +
          escapeHtml(meta.description) +
          "</p>";
      }
      if (meta.website_url && /^https?:\/\//i.test(meta.website_url)) {
        html +=
          '<p style="font-size:12px"><a href="' +
          escapeHtml(meta.website_url) +
          '" target="_blank" rel="noopener noreferrer" style="color:var(--magenta)">' +
          escapeHtml(meta.website_url) +
          "</a></p>";
      }
    } catch (e) {}
    html += "</div>";
  }
  html += "</div>";

  html += '<div class="modal-col">';
  var statusEntries = s.status || {};
  var nodeIds = Object.keys(statusEntries);
  html += '<div class="mcp-detail-section"><h3>Node Status</h3>';
  if (nodeIds.length === 0) {
    html +=
      '<p style="font-size:12px;color:var(--fg-dim)">Not connected on any node</p>';
  } else {
    html += '<ul class="mcp-detail-list">';
    for (var j = 0; j < nodeIds.length; j++) {
      var ns = statusEntries[nodeIds[j]];
      var dot = ns.connected
        ? '<span class="mcp-status-dot connected"></span>'
        : '<span class="mcp-status-dot error"></span>';
      var nodeInfo =
        escapeHtml(nodeIds[j]) +
        " — " +
        (ns.tools || 0) +
        " tools, " +
        (ns.resources || 0) +
        " resources, " +
        (ns.prompts || 0) +
        " prompts";
      if (ns.error) {
        nodeInfo +=
          '<br><span style="color:var(--red);font-size:11px">' +
          escapeHtml(ns.error) +
          "</span>";
      }
      html += "<li>" + dot + nodeInfo + "</li>";
    }
    html += "</ul>";
  }
  html += "</div></div></div>";

  document.getElementById("mcp-detail-title").textContent = s.name;
  document.getElementById("mcp-detail-content").innerHTML = html;
  document.getElementById("mcp-detail-overlay").style.display = "flex";
  _mcpDetailTrap = _installTrap("mcp-detail-overlay", "mcp-detail-box");
}

function hideMcpDetailModal() {
  document.getElementById("mcp-detail-overlay").style.display = "none";
  _mcpDetailTrap = _removeTrap(_mcpDetailTrap);
  if (_mcpDetailTrigger && _mcpDetailTrigger.focus) _mcpDetailTrigger.focus();
  _mcpDetailTrigger = null;
}

function showImportMcpModal() {
  _mcpImportTrigger = document.activeElement;
  document.getElementById("mcp-import-overlay").style.display = "flex";
  document.getElementById("mcp-import-json").value = "";
  document.getElementById("mcp-import-error").style.display = "none";
  document.getElementById("mcp-import-json").focus();
  _mcpImportTrap = _installTrap("mcp-import-overlay", "mcp-import-box");
}

function hideImportMcpModal() {
  document.getElementById("mcp-import-overlay").style.display = "none";
  _mcpImportTrap = _removeTrap(_mcpImportTrap);
  if (_mcpImportTrigger && _mcpImportTrigger.focus) _mcpImportTrigger.focus();
  _mcpImportTrigger = null;
}

function submitImportMcp() {
  var raw = document.getElementById("mcp-import-json").value.trim();
  if (!raw) {
    var e = document.getElementById("mcp-import-error");
    e.textContent = "Paste a JSON config";
    e.style.display = "";
    return;
  }
  var parsed;
  try {
    parsed = JSON.parse(raw);
  } catch (ex) {
    var e2 = document.getElementById("mcp-import-error");
    e2.textContent = "Invalid JSON: " + ex.message;
    e2.style.display = "";
    return;
  }
  if (!parsed.mcpServers || typeof parsed.mcpServers !== "object") {
    var e3 = document.getElementById("mcp-import-error");
    e3.textContent = 'No "mcpServers" key found in JSON';
    e3.style.display = "";
    return;
  }
  document.getElementById("mcp-import-submit").disabled = true;
  authFetch("/v1/api/admin/mcp-servers/import", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ config: parsed }),
  })
    .then(function (r) {
      if (!r.ok)
        return r.json().then(function (d) {
          throw new Error(d.error || "Failed");
        });
      return r.json();
    })
    .then(function (data) {
      hideImportMcpModal();
      var msg = "Imported " + (data.imported || []).length;
      if ((data.skipped || []).length)
        msg += ", skipped " + data.skipped.length;
      if ((data.errors || []).length)
        msg += ", " + data.errors.length + " error(s)";
      showToast(msg);
      if ((data.imported || []).length) _flagMcpSyncPending();
      loadAdminMcp();
    })
    .catch(function (e) {
      var el = document.getElementById("mcp-import-error");
      el.textContent = e.message;
      el.style.display = "";
    })
    .finally(function () {
      document.getElementById("mcp-import-submit").disabled = false;
    });
}

/* ── MCP Registry ────────────────────────────────────────────────────────── */

function switchMcpView(view) {
  _mcpCurrentView = view;
  var btns = document.querySelectorAll("#admin-mcp .mcp-view-btn");
  for (var i = 0; i < btns.length; i++) {
    var isActive = btns[i].getAttribute("data-mcp-view") === view;
    btns[i].classList.toggle("active", isActive);
    btns[i].setAttribute("aria-selected", isActive ? "true" : "false");
    btns[i].setAttribute("tabindex", isActive ? "0" : "-1");
  }
  document.getElementById("mcp-view-servers").style.display =
    view === "servers" ? "" : "none";
  document.getElementById("mcp-view-registry").style.display =
    view === "registry" ? "" : "none";
  document.getElementById("mcp-servers-toolbar").style.display =
    view === "servers" ? "" : "none";
  if (view === "servers") loadAdminMcp();
  if (view === "registry") {
    var q = document.getElementById("mcp-registry-q");
    if (q) q.focus();
    if (!_registryResults.length) searchMcpRegistry();
  }
}

function searchMcpRegistry(append) {
  var q = document.getElementById("mcp-registry-q").value.trim();
  if (!append) {
    _registryResults = [];
    _registryCursor = null;
    _registryQuery = q;
    var filterEl = document.getElementById("mcp-registry-filter");
    if (filterEl) filterEl.value = "";
  }
  var url = "/v1/api/admin/mcp-registry/search?limit=20";
  if (_registryQuery) url += "&search=" + encodeURIComponent(_registryQuery);
  if (append && _registryCursor)
    url += "&cursor=" + encodeURIComponent(_registryCursor);

  var resultsEl = document.getElementById("mcp-registry-results");
  if (!append) {
    resultsEl.innerHTML = '<div class="dashboard-empty">Searching…</div>';
  }
  var searchBtn = document.getElementById("mcp-registry-search-btn");
  var moreBtn = document.getElementById("mcp-registry-more");
  if (searchBtn) searchBtn.disabled = true;
  if (moreBtn) moreBtn.disabled = true;

  authFetch(url)
    .then(function (r) {
      if (!r.ok)
        return r.json().then(function (d) {
          throw new Error(d.error || "Search failed");
        });
      return r.json();
    })
    .then(function (data) {
      _registryResults = append
        ? _registryResults.concat(data.servers || [])
        : data.servers || [];
      _registryCursor = data.next_cursor || null;
      _renderRegistryResults();
    })
    .catch(function (e) {
      if (!append) {
        resultsEl.innerHTML =
          '<div class="dashboard-empty">' + escapeHtml(e.message) + "</div>";
      }
    })
    .finally(function () {
      if (searchBtn) searchBtn.disabled = false;
      if (moreBtn) moreBtn.disabled = false;
    });
}

function loadMoreRegistry() {
  if (_registryCursor) searchMcpRegistry(true);
}

function _applyRegistryFilter() {
  _renderRegistryResults();
}

function _renderRegistryResults() {
  var el = document.getElementById("mcp-registry-results");
  if (!_registryResults.length) {
    el.innerHTML = '<div class="dashboard-empty">No servers found</div>';
    document.getElementById("mcp-registry-pagination").style.display = "none";
    return;
  }

  // Client-side type filter
  var filterEl = document.getElementById("mcp-registry-filter");
  var typeFilter = filterEl ? filterEl.value : "";

  var html = "";
  var visibleCount = 0;
  for (var i = 0; i < _registryResults.length; i++) {
    var srv = _registryResults[i];
    var hasRemote = srv.remotes && srv.remotes.length > 0;
    var pkgTypes = (srv.packages || []).map(function (p) {
      return p.registry_type;
    });

    // Apply type filter
    if (typeFilter === "remote" && !hasRemote) continue;
    if (typeFilter === "npm" && pkgTypes.indexOf("npm") === -1) continue;
    if (typeFilter === "pypi" && pkgTypes.indexOf("pypi") === -1) continue;
    visibleCount++;

    // Action button
    var srvLabel = escapeHtml(srv.title || srv.name);
    var actionHtml = "";
    if (srv.installed && srv.update_available) {
      actionHtml =
        '<button class="mcp-install-btn mcp-update-btn" data-reg-install="' +
        i +
        '" aria-label="Update ' +
        srvLabel +
        '">Update</button>';
    } else if (srv.installed) {
      actionHtml = '<span class="mcp-installed-badge">Installed</span>';
    } else {
      actionHtml =
        '<button class="mcp-install-btn" data-reg-install="' +
        i +
        '" aria-label="Install ' +
        srvLabel +
        '">Install</button>';
    }

    // Source type badges
    var sourceBadges = "";
    if (hasRemote) {
      sourceBadges +=
        '<span class="scope-badge mcp-transport-http">remote</span>';
    }
    for (var p = 0; p < (srv.packages || []).length; p++) {
      sourceBadges +=
        '<span class="scope-badge mcp-transport-stdio">' +
        escapeHtml(srv.packages[p].registry_type) +
        "</span>";
    }

    // Repo link for trust signal
    var repoLink = "";
    var repoUrl = (srv.repository || {}).url || "";
    if (repoUrl && /^https?:\/\//i.test(repoUrl)) {
      repoLink =
        ' <a href="' +
        escapeHtml(repoUrl) +
        '" target="_blank" rel="noopener noreferrer" class="mcp-reg-card-repo"' +
        ' aria-label="Source repository for ' +
        srvLabel +
        '"><span aria-hidden="true">\u2197</span></a>';
    }

    html +=
      '<div class="mcp-reg-card" role="listitem">' +
      '<div class="mcp-reg-card-info">' +
      '<div class="mcp-reg-card-name">' +
      escapeHtml(srv.title || srv.name) +
      repoLink +
      "</div>" +
      (srv.description
        ? '<div class="mcp-reg-card-desc">' +
          escapeHtml(srv.description) +
          "</div>"
        : "") +
      '<div class="mcp-reg-card-meta">' +
      sourceBadges +
      "</div></div>" +
      '<div class="mcp-reg-card-actions">' +
      (srv.version
        ? '<span class="mcp-reg-card-version">v' +
          escapeHtml(srv.version) +
          "</span>"
        : "") +
      actionHtml +
      "</div></div>";
  }

  if (!visibleCount && _registryResults.length) {
    el.innerHTML =
      '<div class="dashboard-empty">No servers match the selected filter</div>';
  } else {
    el.innerHTML = html;
  }

  // Pagination
  var pagEl = document.getElementById("mcp-registry-pagination");
  var moreBtn = document.getElementById("mcp-registry-more");
  var countEl = document.getElementById("mcp-registry-count");
  var isFiltered = typeFilter && visibleCount < _registryResults.length;
  if (_registryCursor) {
    pagEl.style.display = "";
    moreBtn.style.display = "";
    countEl.textContent = isFiltered
      ? visibleCount +
        " of " +
        _registryResults.length +
        " loaded (more available)"
      : "Showing " + visibleCount + " results";
  } else {
    pagEl.style.display = visibleCount > 0 ? "" : "none";
    if (moreBtn) moreBtn.style.display = "none";
    countEl.textContent = isFiltered
      ? visibleCount + " of " + _registryResults.length + " match filter"
      : visibleCount + " result" + (visibleCount !== 1 ? "s" : "");
  }

  // Bind install buttons
  el.querySelectorAll("[data-reg-install]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var idx = parseInt(this.getAttribute("data-reg-install"), 10);
      _initiateRegistryInstall(_registryResults[idx]);
    });
  });
}

/* ── Registry Install Flow ───────────────────────────────────────────────── */

function _initiateRegistryInstall(srv) {
  _mcpInstallServer = srv;
  var hasRemote = srv.remotes && srv.remotes.length > 0;
  var hasPackage = srv.packages && srv.packages.length > 0;

  // Check if remote needs configuration
  var remoteNeedsConfig = false;
  if (hasRemote) {
    var remote = srv.remotes[0];
    for (var hi = 0; hi < (remote.headers || []).length; hi++) {
      if (remote.headers[hi].is_required) {
        remoteNeedsConfig = true;
        break;
      }
    }
    var varKeys = Object.keys(remote.variables || {});
    for (var vi = 0; vi < varKeys.length; vi++) {
      if (remote.variables[varKeys[vi]].is_required) {
        remoteNeedsConfig = true;
        break;
      }
    }
  }

  // One-click: remote with no config needed and no package alternative
  if (hasRemote && !remoteNeedsConfig && !hasPackage) {
    // Disable the clicked Install button for loading feedback
    var cardBtns = document.querySelectorAll("[data-reg-install]");
    for (var bi = 0; bi < cardBtns.length; bi++) {
      var idx = parseInt(cardBtns[bi].getAttribute("data-reg-install"), 10);
      if (_registryResults[idx] && _registryResults[idx].name === srv.name) {
        cardBtns[bi].disabled = true;
        cardBtns[bi].textContent = "Installing\u2026";
        break;
      }
    }
    _doRegistryInstall(srv.name, "remote", 0, {}, {}, {});
    return;
  }

  // Otherwise show the install modal
  _showInstallMcpModal(srv, hasRemote, hasPackage);
}

function _showInstallMcpModal(srv, hasRemote, hasPackage) {
  _mcpInstallTrigger = document.activeElement;
  var ov = document.getElementById("mcp-install-overlay");
  ov.style.display = "flex";
  document.getElementById("mcp-install-error").style.display = "none";

  // Summary
  document.getElementById("mcp-install-summary").innerHTML =
    '<div class="mcp-install-summary-name">' +
    escapeHtml(srv.title || srv.name) +
    "</div>" +
    (srv.description
      ? '<div class="mcp-install-summary-desc">' +
        escapeHtml(srv.description) +
        "</div>"
      : "");

  // Source selector (only if both remote AND package)
  var srcEl = document.getElementById("mcp-install-source-select");
  if (hasRemote && hasPackage) {
    var srcHtml = '<div class="mcp-install-source-group">';
    srcHtml +=
      '<label class="mcp-install-source-label">' +
      '<input type="radio" name="mcp-install-src" value="remote" checked ' +
      'onchange="_updateInstallFields()"> ' +
      'Remote <span class="mcp-install-source-type">streamable-http</span>' +
      "</label>";
    for (var pi = 0; pi < srv.packages.length; pi++) {
      srcHtml +=
        '<label class="mcp-install-source-label">' +
        '<input type="radio" name="mcp-install-src" value="package-' +
        pi +
        '" onchange="_updateInstallFields()"> ' +
        'Package <span class="mcp-install-source-type">' +
        escapeHtml(srv.packages[pi].registry_type) +
        " / " +
        escapeHtml(srv.packages[pi].identifier) +
        "</span></label>";
    }
    srcHtml += "</div>";
    srcEl.innerHTML = srcHtml;
  } else {
    srcEl.innerHTML = "";
  }

  _updateInstallFields();
  _mcpInstallTrap = _installTrap("mcp-install-overlay", "mcp-install-box");
}

function _updateInstallFields() {
  var srv = _mcpInstallServer;
  if (!srv) return;
  var fieldsEl = document.getElementById("mcp-install-fields");
  var srcRadio = document.querySelector(
    'input[name="mcp-install-src"]:checked',
  );
  var srcVal = srcRadio ? srcRadio.value : "";

  var source = "remote";
  var pkgIndex = 0;
  if (srcVal.startsWith("package-")) {
    source = "package";
    pkgIndex = parseInt(srcVal.replace("package-", ""), 10);
  } else if (!srv.remotes || !srv.remotes.length) {
    source = "package";
  }

  var html = "";
  if (source === "package") {
    var pkg = srv.packages && srv.packages[pkgIndex];
    var pkgId = pkg ? pkg.identifier : "";
    var pkgType = pkg ? pkg.registry_type : "";
    var runner =
      pkgType === "npm" ? "npx" : pkgType === "pypi" ? "uvx" : pkgType;
    html +=
      '<div class="mcp-registry-notice" role="alert" style="margin-bottom:14px">' +
      '<span class="mcp-registry-notice-icon" aria-hidden="true">&#9888;</span>' +
      "This will download and execute <code>" +
      escapeHtml(pkgId) +
      "</code> via <code>" +
      escapeHtml(runner) +
      "</code> on all cluster nodes. " +
      "Verify the package source before proceeding.</div>";
  }
  if (source === "remote" && srv.remotes && srv.remotes.length > 0) {
    var remote = srv.remotes[0];
    // URL variables
    var varKeys = Object.keys(remote.variables || {});
    for (var vi = 0; vi < varKeys.length; vi++) {
      var v = remote.variables[varKeys[vi]];
      html +=
        '<label for="mcp-inst-var-' +
        vi +
        '">' +
        escapeHtml(varKeys[vi]) +
        (v.is_required
          ? ' <span style="color:var(--red)">*</span>'
          : ' <span class="label-hint">optional</span>') +
        "</label>";
      if (v.choices && v.choices.length) {
        html +=
          '<select id="mcp-inst-var-' +
          vi +
          '" data-var-name="' +
          escapeHtml(varKeys[vi]) +
          '">';
        if (!v.is_required) html += '<option value="">--</option>';
        for (var ci = 0; ci < v.choices.length; ci++) {
          var sel = v.choices[ci] === (v["default"] || "") ? " selected" : "";
          html +=
            '<option value="' +
            escapeHtml(v.choices[ci]) +
            '"' +
            sel +
            ">" +
            escapeHtml(v.choices[ci]) +
            "</option>";
        }
        html += "</select>";
      } else {
        html +=
          '<input type="text" id="mcp-inst-var-' +
          vi +
          '" data-var-name="' +
          escapeHtml(varKeys[vi]) +
          '" placeholder="' +
          escapeHtml(v.description || "") +
          '" value="' +
          escapeHtml(v["default"] || "") +
          '">';
      }
    }
    // Required headers
    for (var hi = 0; hi < (remote.headers || []).length; hi++) {
      var h = remote.headers[hi];
      html +=
        '<label for="mcp-inst-hdr-' +
        hi +
        '">' +
        escapeHtml(h.name) +
        (h.is_required
          ? ' <span style="color:var(--red)">*</span>'
          : ' <span class="label-hint">optional</span>') +
        "</label>";
      html +=
        '<input type="' +
        (h.is_secret ? "password" : "text") +
        '" id="mcp-inst-hdr-' +
        hi +
        '" data-hdr-name="' +
        escapeHtml(h.name) +
        '" placeholder="' +
        escapeHtml(h.description || "") +
        '">';
    }
  } else if (source === "package" && srv.packages && srv.packages[pkgIndex]) {
    var pkg = srv.packages[pkgIndex];
    var evs = pkg.environment_variables || [];
    for (var ei = 0; ei < evs.length; ei++) {
      var ev = evs[ei];
      html +=
        '<label for="mcp-inst-env-' +
        ei +
        '">' +
        escapeHtml(ev.name) +
        (ev.is_required
          ? ' <span style="color:var(--red)">*</span>'
          : ' <span class="label-hint">optional</span>') +
        "</label>";
      html +=
        '<input type="' +
        (ev.is_secret ? "password" : "text") +
        '" id="mcp-inst-env-' +
        ei +
        '" data-env-name="' +
        escapeHtml(ev.name) +
        '" placeholder="' +
        escapeHtml(ev.description || "") +
        '" value="' +
        escapeHtml(ev["default"] || "") +
        '">';
    }
  }

  if (!html) {
    html =
      '<p style="font-size:12px;color:var(--fg-dim);margin:8px 0">' +
      "No configuration required — click Install to proceed.</p>";
  }
  fieldsEl.innerHTML = html;
  fieldsEl.setAttribute("data-source", source);
  fieldsEl.setAttribute("data-pkg-index", String(pkgIndex));
}

function hideInstallMcpModal() {
  document.getElementById("mcp-install-overlay").style.display = "none";
  _mcpInstallTrap = _removeTrap(_mcpInstallTrap);
  if (_mcpInstallTrigger && _mcpInstallTrigger.focus)
    _mcpInstallTrigger.focus();
  _mcpInstallTrigger = null;
  _mcpInstallServer = null;
}

function submitInstallMcp() {
  var srv = _mcpInstallServer;
  if (!srv) return;
  var fieldsEl = document.getElementById("mcp-install-fields");
  var source = fieldsEl.getAttribute("data-source") || "remote";
  var pkgIndex = parseInt(fieldsEl.getAttribute("data-pkg-index") || "0", 10);
  var index = source === "remote" ? 0 : pkgIndex;

  var variables = {};
  fieldsEl.querySelectorAll("[data-var-name]").forEach(function (el) {
    variables[el.getAttribute("data-var-name")] = el.value;
  });
  var headers = {};
  fieldsEl.querySelectorAll("[data-hdr-name]").forEach(function (el) {
    if (el.value) headers[el.getAttribute("data-hdr-name")] = el.value;
  });
  var env = {};
  fieldsEl.querySelectorAll("[data-env-name]").forEach(function (el) {
    if (el.value) env[el.getAttribute("data-env-name")] = el.value;
  });

  _doRegistryInstall(srv.name, source, index, variables, env, headers);
}

function _doRegistryInstall(
  registryName,
  source,
  index,
  variables,
  env,
  headers,
) {
  var submitBtn = document.getElementById("mcp-install-submit");
  if (submitBtn) submitBtn.disabled = true;

  authFetch("/v1/api/admin/mcp-registry/install", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      registry_name: registryName,
      source: source,
      index: index,
      variables: variables,
      env: env,
      headers: headers,
    }),
  })
    .then(function (r) {
      if (!r.ok)
        return r.json().then(function (d) {
          throw new Error(d.error || "Install failed");
        });
      return r.json();
    })
    .then(function (data) {
      var overlay = document.getElementById("mcp-install-overlay");
      if (overlay && overlay.style.display !== "none") {
        hideInstallMcpModal();
      }
      var serverName = data.name || registryName;
      showToast("Installed " + serverName + " — connecting to nodes\u2026");
      // Re-search to update installed status
      if (_mcpCurrentView === "registry" && _registryResults.length) {
        searchMcpRegistry(false);
      }
      // Poll for connection status after a delay
      if (data.server_id) {
        _pollInstallStatus(data.server_id, serverName, 0);
      }
    })
    .catch(function (e) {
      var overlay = document.getElementById("mcp-install-overlay");
      var errEl = document.getElementById("mcp-install-error");
      if (errEl && overlay && overlay.style.display !== "none") {
        errEl.textContent = e.message;
        errEl.style.display = "";
      } else {
        showToast("Install failed: " + e.message);
        // Re-render to reset card button states
        _renderRegistryResults();
      }
    })
    .finally(function () {
      if (submitBtn) submitBtn.disabled = false;
    });
}

function _pollInstallStatus(serverId, serverName, attempt) {
  if (attempt >= 3) return; // give up after ~9s
  setTimeout(function () {
    authFetch("/v1/api/admin/mcp-servers/" + encodeURIComponent(serverId))
      .then(function (r) {
        return r.ok ? r.json() : null;
      })
      .then(function (data) {
        if (!data) return;
        var status = data.status || {};
        var nodeIds = Object.keys(status);
        var anyConnected = false;
        var errors = [];
        for (var i = 0; i < nodeIds.length; i++) {
          var ns = status[nodeIds[i]];
          if (ns.connected) anyConnected = true;
          if (ns.error) errors.push(ns.error);
        }
        if (anyConnected) {
          var tools = 0;
          for (var j = 0; j < nodeIds.length; j++) {
            if (status[nodeIds[j]].connected) {
              tools = status[nodeIds[j]].tools || 0;
              break;
            }
          }
          var msg = serverName + " connected";
          if (tools)
            msg += " (" + tools + " tool" + (tools !== 1 ? "s" : "") + ")";
          if (errors.length)
            msg +=
              ", " +
              errors.length +
              " node error" +
              (errors.length !== 1 ? "s" : "");
          showToast(msg);
          if (_mcpCurrentView === "servers") loadAdminMcp();
        } else if (errors.length) {
          showToast(serverName + ": " + errors[0]);
          if (_mcpCurrentView === "servers") loadAdminMcp();
        } else {
          _pollInstallStatus(serverId, serverName, attempt + 1);
        }
      })
      .catch(function () {});
  }, 3000);
}

// ---------------------------------------------------------------------------
// Models tab
// ---------------------------------------------------------------------------

var _modelDefs = [];
var _modelDefaultAlias = "";
var _modelCreateTrap = null;
var _modelCreateTrigger = null;

function loadAdminModels() {
  authFetch("/v1/api/admin/model-definitions")
    .then(function (r) {
      if (!r.ok) throw new Error("Failed");
      return r.json();
    })
    .then(function (data) {
      _modelDefs = data.models || [];
      _modelDefaultAlias = data.default_alias || "";
      _renderModels(_modelDefs);
    })
    .catch(function () {
      var el = document.getElementById("admin-models-table");
      el.textContent = "";
      var d = document.createElement("div");
      d.className = "dashboard-empty";
      d.textContent = "Failed to load models";
      el.appendChild(d);
    });
}

function _renderModels(items) {
  var el = document.getElementById("admin-models-table");
  // Clear previous content
  el.textContent = "";
  if (!items.length) {
    var empty = document.createElement("div");
    empty.className = "dashboard-empty";
    empty.textContent = "No model definitions configured";
    el.appendChild(empty);
    return;
  }
  for (var i = 0; i < items.length; i++) {
    var m = items[i];
    var isConfig = m.source === "config";

    // Status
    var dotClass = m.enabled
      ? "model-status-dot enabled"
      : "model-status-dot disabled";
    var rowClass = m.enabled ? "model-row-enabled" : "model-row-disabled";
    var statusText = m.enabled ? "enabled" : "disabled";

    // Context window formatting (0 = auto-detect)
    var ctxText = m.context_window
      ? m.context_window >= 1000
        ? Math.round(m.context_window / 1000) + "k"
        : String(m.context_window)
      : "auto";

    // Provider badge class
    var providerCls =
      m.provider === "anthropic"
        ? "model-provider-anthropic"
        : m.provider === "google"
          ? "model-provider-google"
          : m.provider === "openai-compatible"
            ? "model-provider-compat"
            : "model-provider-openai";

    // Build row via DOM
    var row = document.createElement("div");
    row.className = "admin-row models-grid " + rowClass;
    row.setAttribute("role", "listitem");

    // Alias + source badge + default badge
    var isDefault = m.alias === _modelDefaultAlias;
    var colAlias = document.createElement("span");
    colAlias.className = "admin-col";
    colAlias.textContent = m.alias;
    var badge = document.createElement("span");
    badge.className = isConfig
      ? "scope-badge scope-config"
      : "scope-badge scope-db";
    badge.textContent = isConfig ? "config" : "db";
    colAlias.appendChild(document.createTextNode(" "));
    colAlias.appendChild(badge);
    if (isDefault) {
      var defBadge = document.createElement("span");
      defBadge.className = "scope-badge scope-default";
      defBadge.textContent = "default";
      colAlias.appendChild(document.createTextNode(" "));
      colAlias.appendChild(defBadge);
    }
    // Per-model sampling override indicators
    var overrides = [];
    if (m.temperature != null) overrides.push("temp=" + m.temperature);
    if (m.max_tokens != null) overrides.push("max_tok=" + m.max_tokens);
    if (m.reasoning_effort != null)
      overrides.push("effort=" + m.reasoning_effort);
    if (overrides.length) {
      var ovrSpan = document.createElement("span");
      ovrSpan.className = "model-overrides-hint";
      ovrSpan.textContent = overrides.join(", ");
      ovrSpan.title = "Per-model overrides (override global defaults)";
      ovrSpan.setAttribute(
        "aria-label",
        "Per-model overrides: " + overrides.join(", "),
      );
      colAlias.appendChild(document.createElement("br"));
      colAlias.appendChild(ovrSpan);
    }
    row.appendChild(colAlias);

    // Model ID
    var colModel = document.createElement("span");
    colModel.className = "admin-col";
    var code = document.createElement("code");
    code.textContent = m.model;
    colModel.appendChild(code);
    row.appendChild(colModel);

    // Provider
    var colProvider = document.createElement("span");
    colProvider.className = "admin-col";
    var provBadge = document.createElement("span");
    provBadge.className = "model-provider-badge " + providerCls;
    provBadge.textContent = m.provider;
    colProvider.appendChild(provBadge);
    row.appendChild(colProvider);

    // Context window
    var colCtx = document.createElement("span");
    colCtx.className = "admin-col";
    colCtx.textContent = ctxText;
    row.appendChild(colCtx);

    // Status
    var colStatus = document.createElement("span");
    colStatus.className = "admin-col";
    var dot = document.createElement("span");
    dot.className = dotClass;
    dot.setAttribute("aria-hidden", "true");
    colStatus.appendChild(dot);
    colStatus.appendChild(document.createTextNode(statusText));
    row.appendChild(colStatus);

    // Actions
    var colActions = document.createElement("span");
    colActions.className = "admin-col";
    if (!isDefault && m.enabled) {
      var defBtn = document.createElement("button");
      defBtn.className = "admin-btn-action";
      defBtn.textContent = "set default";
      defBtn.setAttribute("data-model-set-default", m.alias);
      defBtn.setAttribute("aria-label", "Set " + m.alias + " as default model");
      defBtn.setAttribute("title", "Set " + m.alias + " as default model");
      colActions.appendChild(defBtn);
    }
    if (!isConfig) {
      var editBtn = document.createElement("button");
      editBtn.className = "admin-btn-action";
      editBtn.textContent = "edit";
      editBtn.setAttribute("data-model-edit", m.definition_id);
      editBtn.setAttribute("title", "Edit " + m.alias);
      colActions.appendChild(editBtn);

      var delBtn = document.createElement("button");
      delBtn.className = "admin-btn-danger";
      delBtn.textContent = "del";
      delBtn.setAttribute("data-model-delete", m.definition_id);
      delBtn.setAttribute("data-model-alias", m.alias);
      delBtn.setAttribute("title", "Delete " + m.alias);
      colActions.appendChild(delBtn);
    }
    row.appendChild(colActions);

    el.appendChild(row);
  }

  // Bind event handlers
  el.querySelectorAll("[data-model-set-default]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var alias = this.getAttribute("data-model-set-default");
      var self = this;
      self.disabled = true;
      self.textContent = "setting\u2026";
      authFetch("/v1/api/admin/settings/model.default_alias", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ value: alias }),
      })
        .then(function (r) {
          if (!r.ok) throw new Error();
          showToast("Default model set to " + alias);
          _flagModelSyncPending();
          loadAdminModels();
        })
        .catch(function () {
          showToast("Failed to set default model");
          self.disabled = false;
          self.textContent = "set default";
        });
    });
  });
  el.querySelectorAll("[data-model-edit]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      showEditModelModal(this.getAttribute("data-model-edit"));
    });
  });
  el.querySelectorAll("[data-model-delete]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var did = this.getAttribute("data-model-delete");
      var dalias = this.getAttribute("data-model-alias");
      showConfirmModal(
        "Delete Model",
        'Delete model "' + dalias + '"?',
        "Delete",
        function () {
          authFetch(
            "/v1/api/admin/model-definitions/" + encodeURIComponent(did),
            {
              method: "DELETE",
            },
          )
            .then(function (r) {
              if (!r.ok) throw new Error();
              return r.json();
            })
            .then(function () {
              showToast("Model deleted");
              _flagModelSyncPending();
              loadAdminModels();
            })
            .catch(function () {
              showToast("Failed to delete model");
            });
        },
      );
    });
  });
}

function _isPlainObject(v) {
  return v !== null && typeof v === "object" && !Array.isArray(v);
}

function _toggleThinkingParam() {
  var mode = document.getElementById("model-thinking-mode").value;
  var row = document.getElementById("model-thinking-param-row");
  row.style.display = mode ? "" : "none";
  // Set default when first enabling
  var paramEl = document.getElementById("model-thinking-param");
  if (mode && !paramEl.value) paramEl.value = "enable_thinking";
}

function showCreateModelModal() {
  _modelCreateTrigger = document.activeElement;
  var ov = document.getElementById("model-create-overlay");
  ov.style.display = "flex";
  document.getElementById("model-edit-id").value = "";
  document.getElementById("model-create-title").textContent = "Add Model";
  document.getElementById("model-create-submit").textContent = "Create";
  document.getElementById("model-create-error").classList.remove("is-visible");
  document.getElementById("model-alias").value = "";
  document.getElementById("model-name").value = "";
  document.getElementById("model-provider").value = "openai";
  document.getElementById("model-base-url").value = "";
  document.getElementById("model-api-key").value = "";
  document.getElementById("model-api-key").placeholder = "sk-...";
  document.getElementById("model-ctx-window").value = "0";
  document.getElementById("model-temperature").value = "";
  document.getElementById("model-max-tokens").value = "";
  document.getElementById("model-reasoning-effort").value = "";
  document.getElementById("model-server-type").value = "";
  document.getElementById("model-thinking-mode").value = "";
  document.getElementById("model-thinking-param").value = "";
  document.getElementById("model-thinking-param-row").style.display = "none";
  document.getElementById("model-extra-body").value = "";
  document.getElementById("model-capabilities").value = "";
  // Clear validation error styling from prior submit attempts
  ["model-extra-body", "model-capabilities"].forEach(function (id) {
    var el = document.getElementById(id);
    el.removeAttribute("aria-invalid");
    el.style.borderColor = "";
  });
  document.getElementById("model-enabled").checked = true;
  document.getElementById("model-detect-result").style.display = "none";
  document.getElementById("model-detect-btn").disabled = false;
  document.getElementById("model-detect-btn").textContent = "Detect";
  _refreshModelSuggestions();
  _applyProviderDefaults();
  document.getElementById("model-alias").focus();
  _modelCreateTrap = _installTrap("model-create-overlay", "model-create-box");
}

function showEditModelModal(definitionId) {
  authFetch(
    "/v1/api/admin/model-definitions/" + encodeURIComponent(definitionId),
  )
    .then(function (r) {
      if (!r.ok) throw new Error("Failed");
      return r.json();
    })
    .then(function (m) {
      showCreateModelModal();
      document.getElementById("model-edit-id").value = definitionId;
      document.getElementById("model-create-title").textContent = "Edit Model";
      document.getElementById("model-create-submit").textContent = "Save";
      document.getElementById("model-alias").value = m.alias || "";
      document.getElementById("model-name").value = m.model || "";
      document.getElementById("model-provider").value = m.provider || "openai";
      document.getElementById("model-base-url").value = m.base_url || "";
      document.getElementById("model-api-key").value = "";
      document.getElementById("model-api-key").placeholder =
        "\u2022\u2022\u2022 (leave blank to keep existing)";
      document.getElementById("model-ctx-window").value =
        m.context_window != null ? m.context_window : 0;
      document.getElementById("model-temperature").value =
        m.temperature != null ? m.temperature : "";
      document.getElementById("model-max-tokens").value =
        m.max_tokens != null ? m.max_tokens : "";
      document.getElementById("model-reasoning-effort").value =
        m.reasoning_effort != null ? m.reasoning_effort : "";
      // Parse capabilities JSON and extract server_compat for structured fields
      var capsObj = {};
      try {
        capsObj = JSON.parse(m.capabilities || "{}");
      } catch (e) {
        /* keep empty */
      }
      // Defend against null/array/primitive values in the DB
      if (!_isPlainObject(capsObj)) capsObj = {};
      var sc = _isPlainObject(capsObj.server_compat)
        ? capsObj.server_compat
        : {};
      // Only extract thinking_mode into the dropdown when the UI can
      // represent it ("manual" or "").  Values like "adaptive" (Anthropic-
      // only) stay in the raw capabilities JSON so they aren't silently
      // lost on save.
      var tmVal = capsObj.thinking_mode || "";
      var tmRepresentable = tmVal === "" || tmVal === "manual";
      if (tmRepresentable) {
        document.getElementById("model-thinking-mode").value = tmVal;
        document.getElementById("model-thinking-param").value =
          capsObj.thinking_param || "";
      } else {
        document.getElementById("model-thinking-mode").value = "";
        document.getElementById("model-thinking-param").value = "";
      }
      _toggleThinkingParam();
      // Server compat: server_type and extra_body workarounds
      document.getElementById("model-server-type").value = sc.server_type || "";
      var eb = sc.extra_body || {};
      var ebText = JSON.stringify(eb, null, 2);
      document.getElementById("model-extra-body").value =
        ebText === "{}" ? "" : ebText;
      // Remove structured fields from capabilities display — only delete
      // thinking_mode/thinking_param when the UI successfully captured them.
      delete capsObj.server_compat;
      if (tmRepresentable) {
        delete capsObj.thinking_mode;
        delete capsObj.thinking_param;
      }
      var capsText = JSON.stringify(capsObj, null, 2);
      document.getElementById("model-capabilities").value =
        capsText === "{}" ? "" : capsText;
      document.getElementById("model-enabled").checked = m.enabled !== false;
      _applyProviderDefaults();
    })
    .catch(function () {
      showToast("Failed to load model details");
    });
}

function hideCreateModelModal() {
  document.getElementById("model-create-overlay").style.display = "none";
  _modelCreateTrap = _removeTrap(_modelCreateTrap);
  if (_modelCreateTrigger && _modelCreateTrigger.focus)
    _modelCreateTrigger.focus();
  _modelCreateTrigger = null;
}

function submitCreateModel() {
  var alias = document.getElementById("model-alias").value.trim();
  var modelName = document.getElementById("model-name").value.trim();
  if (!alias) {
    _showModelError("Alias is required");
    return;
  }
  if (!modelName) {
    _showModelError("Model ID is required");
    return;
  }
  if (!/^[a-zA-Z0-9._-]+$/.test(alias)) {
    _showModelError("Alias must be alphanumeric (with . _ -)");
    return;
  }

  var capsEl = document.getElementById("model-capabilities");
  var capsText = capsEl.value.trim();
  var caps = {};
  capsEl.removeAttribute("aria-invalid");
  capsEl.style.borderColor = "";
  if (capsText) {
    try {
      caps = JSON.parse(capsText);
    } catch (e) {
      capsEl.setAttribute("aria-invalid", "true");
      capsEl.style.borderColor = "var(--red)";
      _showModelError("Invalid JSON in capabilities");
      return;
    }
    if (!_isPlainObject(caps)) {
      capsEl.setAttribute("aria-invalid", "true");
      capsEl.style.borderColor = "var(--red)";
      _showModelError(
        "Capabilities must be a JSON object (not array or primitive)",
      );
      return;
    }
  }

  // Thinking mode → capabilities (provider uses this to inject
  // the correct chat_template_kwargs param automatically).
  var thinkingMode = document.getElementById("model-thinking-mode").value;
  if (thinkingMode) {
    caps.thinking_mode = thinkingMode;
    // Preserve thinking_param so Granite/DeepSeek "thinking" key
    // isn't silently reverted to the default "enable_thinking".
    var savedParam = document.getElementById("model-thinking-param").value;
    if (savedParam) caps.thinking_param = savedParam;
  }

  // Build server_compat from structured fields
  var serverCompat = {};
  var serverType = document.getElementById("model-server-type").value;
  if (serverType) serverCompat.server_type = serverType;
  var ebEl = document.getElementById("model-extra-body");
  var ebText = ebEl.value.trim();
  ebEl.removeAttribute("aria-invalid");
  ebEl.style.borderColor = "";
  if (ebText) {
    try {
      var ebParsed = JSON.parse(ebText);
      if (!_isPlainObject(ebParsed)) {
        throw new Error("not an object");
      }
      serverCompat.extra_body = ebParsed;
    } catch (e) {
      ebEl.setAttribute("aria-invalid", "true");
      ebEl.style.borderColor = "var(--red)";
      _showModelError("Extra body params must be a JSON object");
      return;
    }
  }
  if (Object.keys(serverCompat).length > 0) {
    caps.server_compat = serverCompat;
  }

  var form = {
    alias: alias,
    model: modelName,
    provider: document.getElementById("model-provider").value,
    base_url: document.getElementById("model-base-url").value.trim(),
    context_window:
      parseInt(document.getElementById("model-ctx-window").value, 10) || 0,
    capabilities: caps,
    enabled: document.getElementById("model-enabled").checked,
  };

  // Per-model sampling overrides — null when empty (use global default)
  var tempVal = document.getElementById("model-temperature").value.trim();
  if (tempVal !== "") {
    var t = parseFloat(tempVal);
    if (isNaN(t) || t < 0 || t > 2) {
      _showModelError("Temperature must be between 0 and 2");
      return;
    }
    form.temperature = t;
  } else {
    form.temperature = null;
  }
  var mtVal = document.getElementById("model-max-tokens").value.trim();
  if (mtVal !== "") {
    var mt = parseInt(mtVal, 10);
    if (isNaN(mt) || mt < 1) {
      _showModelError("Max tokens must be at least 1");
      return;
    }
    form.max_tokens = mt;
  } else {
    form.max_tokens = null;
  }
  var reVal = document.getElementById("model-reasoning-effort").value;
  if (reVal !== "") {
    form.reasoning_effort = reVal;
  } else {
    form.reasoning_effort = null;
  }

  var apiKey = document.getElementById("model-api-key").value;
  if (apiKey) form.api_key = apiKey;

  var editId = document.getElementById("model-edit-id").value;
  var method = editId ? "PUT" : "POST";
  var url = editId
    ? "/v1/api/admin/model-definitions/" + encodeURIComponent(editId)
    : "/v1/api/admin/model-definitions";

  document.getElementById("model-create-submit").disabled = true;
  authFetch(url, {
    method: method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(form),
  })
    .then(function (r) {
      if (!r.ok)
        return r.json().then(function (d) {
          throw new Error(d.error || "Failed");
        });
      return r.json();
    })
    .then(function () {
      hideCreateModelModal();
      showToast(editId ? "Model updated" : "Model created");
      _flagModelSyncPending();
      loadAdminModels();
    })
    .catch(function (e) {
      _showModelError(e.message);
    })
    .finally(function () {
      document.getElementById("model-create-submit").disabled = false;
    });
}

function _showModelError(msg) {
  var e = document.getElementById("model-create-error");
  e.textContent = msg;
  e.classList.add("is-visible");
}

function _detectResultLine(text, color) {
  var div = document.createElement("div");
  div.style.marginTop = "3px";
  if (color) div.style.color = "var(--" + color + ")";
  div.textContent = text;
  return div;
}

function _clearDetectResult() {
  var rd = document.getElementById("model-detect-result");
  if (rd) {
    rd.style.display = "none";
    rd.textContent = "";
    rd.style.borderColor = "";
  }
}

function detectModel() {
  var btn = document.getElementById("model-detect-btn");
  var resultDiv = document.getElementById("model-detect-result");
  btn.disabled = true;
  btn.setAttribute("aria-busy", "true");
  btn.textContent = "Detecting\u2026";
  resultDiv.style.display = "none";
  resultDiv.textContent = "";

  var form = {
    provider: document.getElementById("model-provider").value,
    base_url: document.getElementById("model-base-url").value.trim(),
    model: document.getElementById("model-name").value.trim(),
  };
  var apiKey = document.getElementById("model-api-key").value;
  if (apiKey) form.api_key = apiKey;
  var editId = document.getElementById("model-edit-id").value;
  if (editId) form.definition_id = editId;

  authFetch("/v1/api/admin/model-definitions/detect", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(form),
  })
    .then(function (r) {
      if (!r.ok)
        return r.json().then(function (d) {
          throw new Error(d.error || "Detect failed");
        });
      return r.json();
    })
    .then(function (d) {
      resultDiv.style.display = "block";
      resultDiv.textContent = "";
      if (d.error && !d.reachable) {
        resultDiv.appendChild(
          _detectResultLine("\u2717 Failed: " + d.error, "red"),
        );
        resultDiv.style.borderColor = "var(--red)";
        return;
      }
      var line1 = "\u2713 Connected";
      if (d.available_models && d.available_models.length) {
        line1 += " \u2014 " + d.available_models.length + " model(s) available";
      }
      resultDiv.appendChild(_detectResultLine(line1, "green"));

      if (d.model_found === false) {
        var models = d.available_models || [];
        var msg =
          '\u26A0 Model "' +
          form.model +
          '" not found in ' +
          models.length +
          " available model(s)";
        if (models.length > 0) {
          var shown = models.slice(0, 8);
          msg += ": " + shown.join(", ");
          if (models.length > 8)
            msg += ", \u2026 +" + (models.length - 8) + " more";
        }
        resultDiv.appendChild(_detectResultLine(msg, "yellow"));
      }

      if (d.available_models && d.available_models.length > 0) {
        var dl = document.getElementById("model-name-suggestions");
        if (dl) {
          dl.textContent = "";
          d.available_models.forEach(function (m) {
            var opt = document.createElement("option");
            opt.value = m;
            dl.appendChild(opt);
          });
        }
      }
      if (d.context_window) {
        resultDiv.appendChild(
          _detectResultLine(
            "Context window: " + d.context_window.toLocaleString() + " tokens",
          ),
        );
        var ctxInput = document.getElementById("model-ctx-window");
        if (parseInt(ctxInput.value, 10) === 0) {
          ctxInput.value = d.context_window;
        }
      }
      if (d.server_type) {
        resultDiv.appendChild(
          _detectResultLine("Server type: " + d.server_type),
        );
        // Auto-fill server type if not already set and value is a known option
        var stEl = document.getElementById("model-server-type");
        var stOpts = Array.from(stEl.options).map(function (o) {
          return o.value;
        });
        if (!stEl.value && stOpts.indexOf(d.server_type) !== -1)
          stEl.value = d.server_type;
      }
      // Auto-fill capabilities from suggested profile
      if (d.suggested_capabilities) {
        var sc2 = d.suggested_capabilities;
        var tmEl = document.getElementById("model-thinking-mode");
        if (!tmEl.value && sc2.thinking_mode) {
          tmEl.value = sc2.thinking_mode;
        }
        if (sc2.thinking_param) {
          var tpEl = document.getElementById("model-thinking-param");
          if (!tpEl.value) tpEl.value = sc2.thinking_param;
        }
        _toggleThinkingParam();
      }
      // Auto-fill server compat from suggested profile
      if (d.suggested_server_compat) {
        var ssc = d.suggested_server_compat;
        var stEl2 = document.getElementById("model-server-type");
        var stOpts2 = Array.from(stEl2.options).map(function (o) {
          return o.value;
        });
        if (
          !stEl2.value &&
          ssc.server_type &&
          stOpts2.indexOf(ssc.server_type) !== -1
        )
          stEl2.value = ssc.server_type;
        if (ssc.extra_body) {
          var ebEl2 = document.getElementById("model-extra-body");
          if (!ebEl2.value.trim()) {
            var ebJson = JSON.stringify(ssc.extra_body, null, 2);
            if (ebJson !== "{}") ebEl2.value = ebJson;
          }
        }
      }
      if (d.suggested_capabilities || d.suggested_server_compat) {
        resultDiv.appendChild(
          _detectResultLine("\u2713 Compatibility profile suggested", "green"),
        );
      }
      resultDiv.style.borderColor = "var(--green)";
    })
    .catch(function (e) {
      if (e.message === "auth") return;
      resultDiv.style.display = "block";
      resultDiv.textContent = "";
      resultDiv.appendChild(_detectResultLine("\u2717 " + e.message, "red"));
      resultDiv.style.borderColor = "var(--red)";
    })
    .finally(function () {
      btn.disabled = false;
      btn.removeAttribute("aria-busy");
      btn.textContent = "Detect";
    });
}

/* Capability auto-fill: when the user types a known model name or
   changes the provider, look up static capabilities and pre-fill
   context_window and the capabilities textarea. */
var _capsTimer = null;
function _onModelFieldChange() {
  clearTimeout(_capsTimer);
  _capsTimer = setTimeout(function () {
    var overlay = document.getElementById("model-create-overlay");
    if (!overlay || overlay.style.display === "none") return;
    var provider = document.getElementById("model-provider").value;
    var modelName = document.getElementById("model-name").value.trim();
    if (!modelName) return;
    authFetch(
      "/v1/api/admin/model-capabilities?provider=" +
        encodeURIComponent(provider) +
        "&model=" +
        encodeURIComponent(modelName),
    )
      .then(function (r) {
        return r.json();
      })
      .then(function (d) {
        if (!d.known || !d.capabilities) return;
        var ctxInput = document.getElementById("model-ctx-window");
        if (
          parseInt(ctxInput.value, 10) === 0 &&
          d.capabilities.context_window
        ) {
          ctxInput.value = d.capabilities.context_window;
        }
        var capsInput = document.getElementById("model-capabilities");
        if (!capsInput.value.trim()) {
          var caps = Object.assign({}, d.capabilities);
          delete caps.context_window;
          delete caps.max_output_tokens;
          delete caps.token_param;
          delete caps.supports_streaming;
          delete caps.supports_tools;
          var text = JSON.stringify(caps, null, 2);
          if (text !== "{}") capsInput.value = text;
        }
      })
      .catch(function () {
        /* silent */
      });
  }, 500);
}
/* Provider-specific placeholder hints for base_url and model ID fields.
   Keep URLs in sync with _PROVIDER_DEFAULT_URLS in console/server.py
   and GOOGLE_DEFAULT_BASE_URL in core/providers/_google.py. */
var _providerDefaults = {
  openai: {
    urlPlaceholder: "https://api.openai.com/v1",
    modelPlaceholder: "gpt-5",
  },
  anthropic: {
    urlPlaceholder: "https://api.anthropic.com",
    modelPlaceholder: "claude-",
  },
  google: {
    urlPlaceholder: "https://generativelanguage.googleapis.com/v1beta/openai/",
    modelPlaceholder: "gemini-",
  },
  "openai-compatible": {
    urlPlaceholder: "e.g. https://your-provider.com/v1",
    modelPlaceholder: "GLM5",
  },
};

/* Update placeholders when provider changes. */
function _applyProviderDefaults() {
  var provider = document.getElementById("model-provider").value;
  var def = _providerDefaults[provider];
  if (!def) return;
  document.getElementById("model-base-url").placeholder = def.urlPlaceholder;
  document.getElementById("model-name").placeholder = def.modelPlaceholder;
  // Server compat section only applies to local model servers
  var scSection = document.getElementById("model-server-compat-section");
  if (scSection) {
    scSection.style.display = provider === "openai-compatible" ? "" : "none";
  }
}

/* Populate the model name datalist with known model prefixes for the
   selected provider.  Called on page load and provider change. */
function _refreshModelSuggestions() {
  var dl = document.getElementById("model-name-suggestions");
  if (!dl) return;
  var provider = document.getElementById("model-provider").value;
  authFetch(
    "/v1/api/admin/model-capabilities/known?provider=" +
      encodeURIComponent(provider),
  )
    .then(function (r) {
      return r.json();
    })
    .then(function (d) {
      dl.textContent = "";
      (d.models || []).forEach(function (m) {
        var opt = document.createElement("option");
        opt.value = m;
        dl.appendChild(opt);
      });
    })
    .catch(function () {
      dl.textContent = "";
    });
}

/* Register listeners once at page load */
(function () {
  var nameEl = document.getElementById("model-name");
  var provEl = document.getElementById("model-provider");
  if (nameEl) nameEl.addEventListener("input", _onModelFieldChange);
  if (provEl) {
    provEl.addEventListener("change", _onModelFieldChange);
    provEl.addEventListener("change", _refreshModelSuggestions);
    provEl.addEventListener("change", _clearDetectResult);
    provEl.addEventListener("change", _applyProviderDefaults);
  }
  /* Clear stale detect results when probe-relevant inputs change */
  ["model-base-url", "model-api-key"].forEach(function (id) {
    var el = document.getElementById(id);
    if (el) el.addEventListener("input", _clearDetectResult);
  });
})();

function _flagModelSyncPending() {
  var btn = document.getElementById("model-sync-btn");
  if (btn) btn.classList.add("model-sync-pending");
}
function _clearModelSyncPending() {
  var btn = document.getElementById("model-sync-btn");
  if (btn) btn.classList.remove("model-sync-pending");
}

function reloadModelNodes() {
  var btn = document.getElementById("model-sync-btn");
  btn.disabled = true;
  btn.textContent = "Syncing...";
  authFetch("/v1/api/admin/model-definitions/reload", { method: "POST" })
    .then(function (r) {
      if (!r.ok) throw new Error();
      return r.json();
    })
    .then(function () {
      showToast("Model reload dispatched");
      _clearModelSyncPending();
      loadAdminModels();
    })
    .catch(function () {
      showToast("Failed to sync models");
    })
    .finally(function () {
      btn.disabled = false;
      btn.textContent = "Sync to Nodes";
    });
}

// ---------------------------------------------------------------------------
// Node Metadata tab
// ---------------------------------------------------------------------------

var _nodeMetaCache = {};

function loadAdminNodeMetadata() {
  var container = document.getElementById("admin-node-metadata-content");
  if (!container) return;
  container.innerHTML = '<div class="dashboard-empty">Loading\u2026</div>';

  // Single bulk fetch for all node metadata
  authFetch("/v1/api/admin/node-metadata")
    .then(function (r) {
      if (!r.ok) throw new Error("Failed");
      return r.json();
    })
    .then(function (data) {
      _nodeMetaCache = data.nodes || {};
      _renderNodeMetadata();
    })
    .catch(function () {
      container.innerHTML =
        '<div class="dashboard-empty">Failed to load node metadata</div>';
    });
}

function _renderNodeMetadata() {
  var container = document.getElementById("admin-node-metadata-content");
  if (!container) return;
  var nodeIds = Object.keys(_nodeMetaCache).sort();
  if (!nodeIds.length) {
    container.innerHTML =
      '<div class="dashboard-empty">No nodes registered</div>';
    return;
  }

  var html = "";
  nodeIds.forEach(function (nid) {
    var meta = _nodeMetaCache[nid] || [];
    html +=
      '<div class="settings-section" data-section="nm-' +
      escapeHtml(nid) +
      '" data-collapsed>';
    html +=
      '<div class="settings-section-header" onclick="_toggleSettingsSection(this)" ';
    html += 'onkeydown="_onSettingsHeaderKey(event,this)" ';
    html += 'role="button" tabindex="0" aria-expanded="false" ';
    html += 'aria-controls="nm-body-' + escapeHtml(nid) + '">';
    html +=
      "<span>" +
      escapeHtml(nid) +
      " <small>(" +
      meta.length +
      " keys)</small></span>";
    html += "</div>";
    html +=
      '<div class="settings-section-body" id="nm-body-' +
      escapeHtml(nid) +
      '">';

    // Table of metadata — all values passed through escapeHtml()
    if (meta.length) {
      html += '<table class="nm-table">';
      html +=
        '<caption class="sr-only">Metadata for node ' +
        escapeHtml(nid) +
        "</caption>";
      html += '<thead><tr><th scope="col">Key</th>';
      html += '<th scope="col">Value</th>';
      html += '<th scope="col">Source</th>';
      html +=
        '<th scope="col"><span class="sr-only">Actions</span></th></tr></thead><tbody>';
      meta.forEach(function (m) {
        var valStr =
          typeof m.value === "object"
            ? JSON.stringify(m.value)
            : String(m.value);
        var isAuto = m.source === "auto";
        html += "<tr>";
        html += '<td class="nm-key">' + escapeHtml(m.key) + "</td>";
        html +=
          '<td class="nm-val" title="' +
          escapeHtml(valStr) +
          '">' +
          escapeHtml(valStr) +
          "</td>";
        html +=
          '<td><span class="nm-source-badge nm-source-' +
          escapeHtml(m.source) +
          '">' +
          escapeHtml(m.source) +
          "</span></td>";
        html += "<td>";
        if (!isAuto) {
          html +=
            '<button class="admin-btn-danger nm-del-btn" aria-label="Delete ' +
            escapeHtml(m.key) +
            '" data-node="' +
            escapeHtml(nid) +
            '" data-key="' +
            escapeHtml(m.key) +
            '">Del</button>';
        }
        html += "</td></tr>";
      });
      html += "</tbody></table>";
    } else {
      html +=
        '<div class="dashboard-empty" style="padding:8px">No metadata</div>';
    }

    // Add metadata form
    html += '<div class="nm-add-row">';
    html +=
      '<input id="nm-key-' +
      escapeHtml(nid) +
      '" type="text" placeholder="key" aria-label="Metadata key">';
    html +=
      '<input id="nm-val-' +
      escapeHtml(nid) +
      '" type="text" placeholder="value (JSON or string)" aria-label="Metadata value">';
    html +=
      '<button class="admin-btn-action nm-add-btn" data-node="' +
      escapeHtml(nid) +
      '" style="white-space:nowrap">Add</button>';
    html += "</div>";

    html += "</div></div>";
  });
  container.innerHTML = html;

  // Bind button handlers (data-* attrs carry node/key context)
  var delBtns = container.querySelectorAll(".nm-del-btn");
  for (var d = 0; d < delBtns.length; d++) {
    delBtns[d].addEventListener("click", function () {
      _deleteNodeMeta(
        this.getAttribute("data-node"),
        this.getAttribute("data-key"),
      );
    });
  }
  var addBtns = container.querySelectorAll(".nm-add-btn");
  for (var a = 0; a < addBtns.length; a++) {
    addBtns[a].addEventListener("click", function () {
      _addNodeMeta(this.getAttribute("data-node"));
    });
  }
}

function _addNodeMeta(nodeId) {
  var keyEl = document.getElementById("nm-key-" + nodeId);
  var valEl = document.getElementById("nm-val-" + nodeId);
  if (!keyEl || !valEl) return;
  var key = keyEl.value.trim();
  var rawVal = valEl.value.trim();
  if (!key) {
    showToast("Key is required", "error");
    return;
  }

  var value;
  try {
    value = JSON.parse(rawVal);
  } catch (e) {
    value = rawVal;
  }

  authFetch(
    "/v1/api/admin/nodes/" +
      encodeURIComponent(nodeId) +
      "/metadata/" +
      encodeURIComponent(key),
    {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ value: value }),
    },
  )
    .then(function (r) {
      if (!r.ok)
        return r
          .json()
          .catch(function () {
            return {};
          })
          .then(function (d) {
            throw new Error(d.error || "Failed");
          });
      showToast("Metadata set");
      loadAdminNodeMetadata();
    })
    .catch(function (e) {
      showToast(e.message, "error");
    });
}

function _deleteNodeMeta(nodeId, key) {
  showConfirmModal(
    "Delete Metadata",
    'Delete key "' + key + '" from node ' + nodeId + "?",
    "Delete",
    function () {
      authFetch(
        "/v1/api/admin/nodes/" +
          encodeURIComponent(nodeId) +
          "/metadata/" +
          encodeURIComponent(key),
        {
          method: "DELETE",
        },
      )
        .then(function (r) {
          if (!r.ok)
            return r
              .json()
              .catch(function () {
                return {};
              })
              .then(function (d) {
                throw new Error(d.error || "Failed");
              });
          showToast("Metadata deleted");
          loadAdminNodeMetadata();
        })
        .catch(function (e) {
          showToast(e.message, "error");
        });
    },
  );
}
