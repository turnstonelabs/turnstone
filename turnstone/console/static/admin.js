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

// ---------------------------------------------------------------------------
// View switching (called from app.js showOverview/drillDown pattern)
// ---------------------------------------------------------------------------

function showAdmin() {
  /* global currentView, showOverview */
  // Toggle: if already in admin view, go back to overview
  if (currentView === "admin") {
    var adminBtn = document.getElementById("admin-btn");
    if (adminBtn) {
      adminBtn.classList.remove("active");
      adminBtn.setAttribute("aria-expanded", "false");
    }
    showOverview();
    return;
  }

  currentView = "admin";
  document.getElementById("view-overview").style.display = "none";
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
    templates: "admin.templates",
    "ws-templates": "admin.ws_templates",
    usage: "admin.usage",
    audit: "admin.audit",
    settings: "admin.users",
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
    toggle.onclick = function () {
      _mobileSidebarOpen = false;
      _toggleMobileSidebar();
    };
  }
  var panel = document.getElementById("admin-" + tab);
  if (panel) {
    var toolbar = panel.querySelector(".admin-toolbar");
    if (toolbar) toolbar.insertBefore(toggle, toolbar.firstChild);
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
    "templates",
    "ws-templates",
    "usage",
    "audit",
    "settings",
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
  if (tab === "templates") loadGovTemplates();
  if (tab === "ws-templates") loadGovWsTemplates();
  if (tab === "usage") loadGovUsage();
  if (tab === "audit") {
    _populateAuditUserFilter();
    loadGovAudit();
  }
  if (tab === "settings") loadSettings();

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
    html +=
      '<div class="admin-row" role="listitem">' +
      '<span class="admin-col admin-col-chtype"><span class="scope-badge scope-channel">' +
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

function _populateWsTemplateSelect(selectId) {
  var sel = document.getElementById(selectId);
  sel.innerHTML = '<option value="">None</option>';
  return authFetch("/v1/api/ws-templates")
    .then(function (r) {
      return r.json();
    })
    .then(function (data) {
      (data.ws_templates || []).forEach(function (t) {
        var opt = document.createElement("option");
        opt.value = t.name;
        var label = t.name;
        if (t.model) label += " (" + t.model + ")";
        opt.textContent = label;
        sel.appendChild(opt);
      });
    })
    .catch(function () {
      /* ignore — dropdown stays with "None" */
    });
}

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
        : (s.at_time || "").slice(0, 16).replace("T", " ");
    var target = s.target_mode;
    var nextRun = s.next_run
      ? escapeHtml(s.next_run).slice(0, 16).replace("T", " ")
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
  document.getElementById("cs-model").value = "";
  document.getElementById("cs-template").value = "";
  _populateWsTemplateSelect("cs-ws-template");
  document.getElementById("cs-message").value = "";
  document.getElementById("cs-autoapprove").checked = false;
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
  var template = (document.getElementById("cs-template").value || "").trim();
  var wsTemplate = document.getElementById("cs-ws-template").value;
  var autoApprove = document.getElementById("cs-autoapprove").checked;
  var errEl = document.getElementById("create-schedule-error");

  if (!name) return _showModalError(errEl, "Name is required");
  if (!message) return _showModalError(errEl, "Initial message is required");
  if (schedType === "cron" && !cronExpr)
    return _showModalError(errEl, "Cron expression is required");
  if (schedType === "at" && !atTime)
    return _showModalError(errEl, "Run time is required");

  // Normalize datetime-local to "YYYY-MM-DDTHH:MM:SS+00:00" (UTC)
  if (schedType === "at" && atTime) {
    if (atTime.length === 16) atTime += ":00";
    else if (atTime.length > 19) atTime = atTime.slice(0, 19);
    atTime += "+00:00";
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
      template: template,
      ws_template: wsTemplate,
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
      document.getElementById("es-at").value = (s.at_time || "").slice(0, 16);
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
      document.getElementById("es-model").value = s.model || "";
      document.getElementById("es-template").value = s.template || "";
      var _wsTemplateVal = s.ws_template || "";
      _populateWsTemplateSelect("es-ws-template").then(function () {
        document.getElementById("es-ws-template").value = _wsTemplateVal;
      });
      document.getElementById("es-message").value = s.initial_message || "";
      document.getElementById("es-autoapprove").checked = !!s.auto_approve;
      document.getElementById("es-enabled").checked = !!s.enabled;
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
  if (atTime) {
    if (atTime.length === 16) atTime += ":00";
    else if (atTime.length > 19) atTime = atTime.slice(0, 19);
    atTime += "+00:00";
  }

  var errEl = document.getElementById("edit-schedule-error");

  if (!name) return _showModalError(errEl, "Name is required");
  if (!message) return _showModalError(errEl, "Initial message is required");
  if (schedType === "cron" && !cronExpr)
    return _showModalError(errEl, "Cron expression is required");
  if (schedType === "at" && !atTime)
    return _showModalError(errEl, "Run time is required");

  var btn = document.getElementById("es-submit");
  btn.disabled = true;
  btn.textContent = "Saving\u2026";

  authFetch("/v1/api/admin/schedules/" + encodeURIComponent(taskId), {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      name: (document.getElementById("es-name").value || "").trim(),
      description: (document.getElementById("es-desc").value || "").trim(),
      schedule_type: document.getElementById("es-type").value,
      cron_expr: (document.getElementById("es-cron").value || "").trim(),
      at_time: atTime,
      target_mode: targetMode,
      model: (document.getElementById("es-model").value || "").trim(),
      template: (document.getElementById("es-template").value || "").trim(),
      ws_template: document.getElementById("es-ws-template").value,
      initial_message: (
        document.getElementById("es-message").value || ""
      ).trim(),
      auto_approve: document.getElementById("es-autoapprove").checked,
      enabled: document.getElementById("es-enabled").checked,
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
  document.getElementById("cc-type").value = "discord";
  document.getElementById("cc-uid").value = "";
  document.getElementById("cc-submit").disabled = false;
  document.getElementById("cc-submit").textContent = "Link";
  _ccTrapHandler = _installTrap("create-channel-overlay", "create-channel-box");
  setTimeout(function () {
    document.getElementById("cc-uid").focus();
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
        else if (overlayId === "create-wst-overlay")
          hideCreateWsTemplateModal();
        else if (overlayId === "edit-wst-overlay") hideEditWsTemplateModal();
        else if (overlayId === "wst-history-overlay") hideWstHistoryModal();
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
    ["create-wst-overlay", hideCreateWsTemplateModal],
    ["edit-wst-overlay", hideEditWsTemplateModal],
    ["wst-history-overlay", hideWstHistoryModal],
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
      if (isMobile && !_mobileSidebarOpen) {
        sidebar.setAttribute("aria-hidden", "true");
        sidebar.setAttribute("inert", "");
        sidebar.classList.add("collapsed");
        sidebar.classList.remove("open");
        if (backdrop) backdrop.classList.remove("visible");
      } else if (!isMobile) {
        sidebar.removeAttribute("aria-hidden");
        sidebar.removeAttribute("inert");
        sidebar.classList.remove("collapsed", "open");
        if (backdrop) backdrop.classList.remove("visible");
        _mobileSidebarOpen = false;
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
// Settings (stub — full implementation is a separate project)
// ---------------------------------------------------------------------------

function loadSettings() {
  var el = document.getElementById("admin-settings-content");
  if (el)
    el.innerHTML = '<div class="dashboard-empty">Settings coming soon</div>';
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function _showModalError(el, msg) {
  el.textContent = msg;
  el.style.display = "block";
}
