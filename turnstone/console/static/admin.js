/* Admin panel — user & token management for turnstone console */

let _adminTab = "users";
let _adminUsers = [];
let _adminTokenUserId = "";
let _adminChannelUserId = "";
let _lastCreatedToken = "";
let _adminWatches = [];
let _confirmCallbackFn = null;

// Settings whose choices are populated dynamically from the live model
// alias list, and whose empty-string option renders as "(server default)".
const ALIAS_SETTING_KEYS = [
  "model.default_alias",
  "model.task_alias",
  "channels.default_model_alias",
  "audio.stt_model_alias",
  "audio.tts_model_alias",
];

// Settings whose empty option means "inherit from a fallback chain", as
// opposed to "no value" — distinct from the literal "none" choice (e.g.
// reasoning_effort="none" actually disables reasoning, very different
// from leaving it unset).
const INHERIT_EMPTY_LABEL_KEYS = [
  "model.task_effort",
  "model.reasoning_effort",
  "coordinator.reasoning_effort",
];

// ---------------------------------------------------------------------------
// Admin information architecture — the single source of truth for the rail's
// Manage groups (rail.js builds from this) AND in-pane tab activation.  Each
// tab carries the permission scope that gates it, so the rail can filter
// without reaching into admin internals.  `perm: null` = ungated (always
// shown — mirrors the legacy gate, which left node-metadata uncovered).
// ---------------------------------------------------------------------------
const ADMIN_IA = [
  {
    group: "Identity",
    tabs: [
      { tab: "users", label: "Users", perm: "admin.users" },
      { tab: "tokens", label: "API Tokens", perm: "admin.users" },
      { tab: "channels", label: "Channels", perm: "admin.users" },
    ],
  },
  {
    group: "Automation",
    tabs: [
      { tab: "schedules", label: "Schedules", perm: "admin.schedules" },
      { tab: "watches", label: "Watches", perm: "admin.watches" },
    ],
  },
  {
    group: "Governance",
    tabs: [
      { tab: "projects", label: "Projects", perm: "project.read" },
      { tab: "personas", label: "Personas", perm: "persona.read" },
      { tab: "roles", label: "Roles", perm: "admin.roles" },
      { tab: "policies", label: "Policies", perm: "admin.policies" },
      {
        tab: "prompt-policies",
        label: "Prompts",
        perm: "admin.prompt_policies",
      },
      { tab: "judge", label: "Judge", perm: "admin.judge" },
    ],
  },
  {
    group: "Extensions",
    tabs: [
      { tab: "skills", label: "Skills", perm: "admin.skills" },
      { tab: "mcp", label: "MCP Servers", perm: "admin.mcp" },
    ],
  },
  {
    group: "Observe",
    tabs: [
      { tab: "usage", label: "Usage", perm: "admin.usage" },
      { tab: "audit", label: "Audit", perm: "admin.audit" },
      { tab: "memories", label: "Memories", perm: "admin.memories" },
    ],
  },
  {
    group: "System",
    tabs: [
      { tab: "models", label: "Models", perm: "admin.models" },
      { tab: "node-metadata", label: "Nodes", perm: null },
      { tab: "settings", label: "Settings", perm: "admin.settings" },
      { tab: "tls", label: "TLS", perm: "admin.settings" },
    ],
  },
];

function _adminTabMeta(tab) {
  for (const grp of ADMIN_IA) {
    for (const t of grp.tabs) if (t.tab === tab) return t;
  }
  return null;
}

// Mirror the legacy showAdmin gate exactly: only gate when a permission string
// is present (unknown perms → show everything); ungated tabs (perm: null) and
// tabs whose scope the user holds are allowed.
function adminTabAllowed(tab) {
  const raw = sessionStorage.getItem("turnstone_permissions");
  if (!raw) return true;
  const meta = _adminTabMeta(tab);
  const needed = meta && meta.perm;
  if (!needed) return true;
  return raw.split(",").indexOf(needed) >= 0;
}

function _firstAllowedAdminTab() {
  for (const grp of ADMIN_IA) {
    for (const t of grp.tabs) if (adminTabAllowed(t.tab)) return t.tab;
  }
  return null;
}

// No-permissions empty state in the admin content host (ported from the legacy
// sidebar gate — still reachable when a user holds no admin scope at all).
function _showAdminNoPermissions() {
  const panels = document.querySelectorAll(".admin-panel");
  for (let j = 0; j < panels.length; j++) panels[j].style.display = "none";
  let empty = document.getElementById("admin-no-permissions");
  if (!empty) {
    empty = document.createElement("div");
    empty.id = "admin-no-permissions";
    empty.className = "dashboard-empty";
    empty.textContent = "You do not have permissions to view any admin tabs.";
    const content = document.getElementById("admin-content");
    if (content) content.appendChild(empty);
  }
  empty.style.display = "";
}

// The rail's Manage groups subscribe here to mirror the active admin tab.
const _adminTabSubs = [];
function _notifyAdminTab(tab) {
  for (const cb of _adminTabSubs) {
    try {
      cb(tab);
    } catch (e) {
      /* a faulty subscriber must not break tab switching */
    }
  }
}

// Seam consumed by rail.js (the Manage section): the admin IA, its permission
// gate, the active-tab subscription, the current tab, and the open-on-tab entry
// point (a row click opens/focuses the Admin pane on that tab).
window.TS_ADMIN = window.TS_ADMIN || {};
window.TS_ADMIN.ia = ADMIN_IA;
window.TS_ADMIN.isTabAllowed = adminTabAllowed;
window.TS_ADMIN.onTabChange = function (cb) {
  if (typeof cb === "function") _adminTabSubs.push(cb);
};
window.TS_ADMIN.getActiveTab = function () {
  return _adminTab;
};
window.TS_ADMIN.openTab = function (tab) {
  showAdmin(tab);
};

// ---------------------------------------------------------------------------
// View switching (called from app.js showHome/drillDown pattern + the rail)
// ---------------------------------------------------------------------------

function showAdmin(tab) {
  // The admin surface is a singleton pane now — the L-shell PaneManager owns
  // show/hide and the rail's Manage groups are its navigation.  Open/focus the
  // pane (its onMount adopts #view-admin), then activate a tab the user may see.
  const pm = window.TS_SHELL && window.TS_SHELL.panes;
  if (pm && pm.hasType("admin")) pm.openPane("admin");

  const target =
    tab ||
    (_adminTab && adminTabAllowed(_adminTab)
      ? _adminTab
      : _firstAllowedAdminTab());
  if (target) switchAdminTab(target);
  else _showAdminNoPermissions();
}

function switchAdminTab(tab) {
  const tabChanged = tab !== _adminTab;
  _adminTab = tab;
  // Hide no-permissions empty state if it was showing
  const noPerms = document.getElementById("admin-no-permissions");
  if (noPerms) noPerms.style.display = "none";
  const panels = [
    "users",
    "tokens",
    "channels",
    "schedules",
    "watches",
    "projects",
    "personas",
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
  for (let p = 0; p < panels.length; p++) {
    const el = document.getElementById("admin-" + panels[p]);
    if (el) el.style.display = panels[p] === tab ? "" : "none";
  }

  // One #admin-content scroller serves every panel — a leftover offset from
  // a tall tab must not carry into the next (the app.js #main reset is the
  // precedent for pane-local navigation).  Same-tab re-entry (showAdmin on
  // pane focus) keeps the user's place.
  if (tabChanged) {
    const contentEl = document.getElementById("admin-content");
    if (contentEl) contentEl.scrollTop = 0;
  }

  if (tab === "users") loadAdminUsers();
  if (tab === "tokens") _populateTokenUserSelect();
  if (tab === "channels") _populateChannelUserSelect();
  if (tab === "schedules") loadAdminSchedules();
  if (tab === "watches") loadAdminWatches();
  if (tab === "projects") loadAdminProjects();
  if (tab === "personas") loadAdminPersonas();
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

  // Mirror the active tab into the rail's Manage groups (the in-pane sidebar
  // that used to carry the active state is retired — the rail navigates now).
  _notifyAdminTab(tab);
}

// ---------------------------------------------------------------------------
// Row action overflow menu (kebab)
// ---------------------------------------------------------------------------
// Shared affordance for every admin table's ACTIONS column.  _kebabMenu()
// returns the markup string; _kebabMenuEl() the equivalent DOM node for the
// few tables that build rows with createElement.  Each menu item carries the
// SAME data-* attribute its inline-button predecessor had, so every existing
// click binding (querySelectorAll('[data-...]')) keeps working untouched —
// only the markup generation changes.  _initKebabMenus() wires open/close,
// outside-click, Escape, scroll/resize dismissal, viewport-aware flip-up, and
// arrow-key navigation once, via document-level delegation, so it covers rows
// rendered by both admin.js and governance.js.

let _kebabInit = false;
// Tracks whether any menu is open, so the high-frequency scroll/resize
// dismiss listeners can skip the DOM query in the common (nothing-open) case.
let _anyKebabOpen = false;

function _kebabBtnClass(kind) {
  if (kind === "danger") return "admin-btn-danger";
  if (kind === "caution") return "admin-btn-caution";
  return "admin-btn-action";
}

function _kebabAttrString(attrs) {
  let out = "";
  if (attrs) {
    const keys = Object.keys(attrs);
    for (let k = 0; k < keys.length; k++) {
      out += " " + keys[k] + '="' + escapeHtml(String(attrs[keys[k]])) + '"';
    }
  }
  return out;
}

function _kebabMenu(items) {
  // items: array of { label, kind?: "danger" | "caution", title?, attrs? }.
  // Falsy entries are dropped so callers can inline conditionals; an empty
  // list yields "" so a row with no applicable actions renders no kebab.
  const real = (items || []).filter(Boolean);
  if (!real.length) return "";
  if (real.length === 1) {
    // A lone action doesn't earn the open-the-menu click — render it as a
    // direct inline button.  Keeps the same data-* attrs (so existing
    // bindings still match) and the familiar bordered button look.
    const it = real[0];
    const title = it.title ? ' title="' + escapeHtml(it.title) + '"' : "";
    return (
      '<button type="button" class="' +
      _kebabBtnClass(it.kind) +
      '"' +
      _kebabAttrString(it.attrs) +
      title +
      ">" +
      escapeHtml(it.label) +
      "</button>"
    );
  }
  let inner = "";
  for (let i = 0; i < real.length; i++) {
    const it = real[i];
    let cls = "admin-kebab-item";
    if (it.kind === "danger") cls += " admin-kebab-item--danger";
    else if (it.kind === "caution") cls += " admin-kebab-item--caution";
    const title = it.title ? ' title="' + escapeHtml(it.title) + '"' : "";
    inner +=
      '<button type="button" class="' +
      cls +
      '" role="menuitem"' +
      _kebabAttrString(it.attrs) +
      title +
      ">" +
      escapeHtml(it.label) +
      "</button>";
  }
  return (
    '<div class="admin-kebab">' +
    '<button type="button" class="admin-kebab-btn" aria-haspopup="true" ' +
    'aria-expanded="false" aria-label="Row actions" title="Row actions">' +
    "⋯" +
    "</button>" +
    '<div class="admin-kebab-menu" role="menu">' +
    inner +
    "</div>" +
    "</div>"
  );
}

function _kebabMenuEl(items) {
  // DOM-node variant for createElement-built rows.  Parsed via DOMParser
  // (same as setSafeHtml) rather than innerHTML; _kebabMenu() has already
  // escaped every interpolated value.
  const html = _kebabMenu(items);
  if (!html) return null;
  const parsed = new DOMParser().parseFromString(html, "text/html");
  return parsed.body.firstElementChild;
}

function _closeAllKebabs() {
  if (!_anyKebabOpen) return;
  _anyKebabOpen = false;
  const open = document.querySelectorAll(".admin-kebab.is-open");
  for (let i = 0; i < open.length; i++) {
    open[i].classList.remove("is-open");
    const menu = open[i].querySelector(".admin-kebab-menu");
    if (menu) menu.classList.remove("flip-up", "flip-left");
    const btn = open[i].querySelector(".admin-kebab-btn");
    if (btn) btn.setAttribute("aria-expanded", "false");
  }
}

function _openKebab(kebab) {
  _closeAllKebabs();
  kebab.classList.add("is-open");
  _anyKebabOpen = true;
  const btn = kebab.querySelector(".admin-kebab-btn");
  const menu = kebab.querySelector(".admin-kebab-menu");
  if (btn) btn.setAttribute("aria-expanded", "true");
  if (!menu) return;
  // Steer the menu back into the viewport: flip above the trigger if a
  // downward menu would pass the bottom edge, and open rightward if a
  // right-anchored menu would run off the left edge (narrow viewports where
  // the actions column isn't flush against the right of the screen).
  menu.classList.remove("flip-up", "flip-left");
  const rect = menu.getBoundingClientRect();
  if (rect.bottom > window.innerHeight - 8) menu.classList.add("flip-up");
  if (rect.left < 8) menu.classList.add("flip-left");
  // Move focus into the menu (WAI-ARIA menu-button pattern).  preventScroll
  // stops the scroll-dismiss listener from firing as the menu opens.
  const first = menu.querySelector(".admin-kebab-item");
  if (first) first.focus({ preventScroll: true });
}

function _initKebabMenus() {
  if (_kebabInit) return;
  _kebabInit = true;

  document.addEventListener("click", function (e) {
    const trigger = e.target.closest(".admin-kebab-btn");
    if (trigger) {
      const kebab = trigger.closest(".admin-kebab");
      if (kebab.classList.contains("is-open")) _closeAllKebabs();
      else _openKebab(kebab);
      return;
    }
    // A menu item's own data-* handler runs first (target phase); we just
    // dismiss the menu afterwards as the click bubbles to the document.
    if (e.target.closest(".admin-kebab-item")) {
      _closeAllKebabs();
      return;
    }
    // Any other click closes an open menu.
    _closeAllKebabs();
  });

  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") {
      const open = document.querySelector(".admin-kebab.is-open");
      if (open) {
        const btn = open.querySelector(".admin-kebab-btn");
        _closeAllKebabs();
        if (btn) btn.focus();
      }
      return;
    }
    const trigger = e.target.closest(".admin-kebab-btn");
    if (trigger && e.key === "ArrowDown") {
      e.preventDefault();
      _openKebab(trigger.closest(".admin-kebab"));
      return;
    }
    const menu = e.target.closest(".admin-kebab-menu");
    if (!menu) return;
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      const items = Array.prototype.slice.call(
        menu.querySelectorAll(".admin-kebab-item"),
      );
      const cur = items.indexOf(document.activeElement);
      const delta = e.key === "ArrowDown" ? 1 : -1;
      const next = items[(cur + delta + items.length) % items.length];
      if (next) next.focus();
    } else if (e.key === "Tab") {
      // Don't let Tab walk focus into the page behind an open menu.
      _closeAllKebabs();
    }
  });

  // The menu is anchored to its cell and rides document scroll, but an
  // independent page/ancestor scroll or a resize can leave it mispositioned —
  // dismiss.  Capture phase catches descendant scrolls too, so skip scrolls
  // originating INSIDE the menu (it can overflow-y:auto at high zoom / short
  // viewports) — those must scroll the menu, not close it.
  window.addEventListener(
    "scroll",
    function (e) {
      const t = e.target;
      if (t && t.closest && t.closest(".admin-kebab-menu")) return;
      _closeAllKebabs();
    },
    true,
  );
  window.addEventListener("resize", _closeAllKebabs);
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
      setSafeHtml(
        document.getElementById("admin-users-table"),
        '<div class="dashboard-empty">Failed to load users</div>',
      );
    });
}

function _renderUsers(users) {
  const container = document.getElementById("admin-users-table");
  if (!users.length) {
    setSafeHtml(
      container,
      '<div class="dashboard-empty">No users yet. Create one to get started.</div>',
    );
    return;
  }
  let html = "";
  for (let i = 0; i < users.length; i++) {
    const u = users[i];
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
      _kebabMenu([
        {
          label: "roles",
          title: "Manage roles",
          attrs: { "data-user-roles": u.user_id },
        },
        {
          label: "delete",
          kind: "danger",
          title: "Delete user",
          attrs: { "data-delete-user": u.user_id, "data-username": u.username },
        },
      ]) +
      "</span>" +
      "</div>";
  }
  setSafeHtml(container, html);
  // Bind roles buttons
  const roleBtns = container.querySelectorAll("[data-user-roles]");
  for (let rj = 0; rj < roleBtns.length; rj++) {
    roleBtns[rj].addEventListener("click", function () {
      showUserRolesModal(this.getAttribute("data-user-roles"));
    });
  }
  // Bind delete buttons via delegation (avoids inline JS injection)
  const btns = container.querySelectorAll("[data-delete-user]");
  for (let j = 0; j < btns.length; j++) {
    btns[j].addEventListener("click", function () {
      confirmDeleteUser(
        this.getAttribute("data-delete-user"),
        this.getAttribute("data-username"),
      );
    });
  }
  // Bind expandable row click + keyboard handlers for OIDC detail panel
  const rows = container.querySelectorAll(".admin-row[data-expandable]");
  for (let k = 0; k < rows.length; k++) {
    (function (row) {
      const _expand = function () {
        const uid = row.getAttribute("data-user-id");
        const uname = row.getAttribute("data-username");
        _toggleOidcPanel(uid, uname, row);
      };
      row.addEventListener("click", function (e) {
        // Clicks on the row's action menu (kebab trigger or any item) must
        // not also toggle the OIDC detail panel.
        if (
          e.target.closest(".admin-kebab") ||
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
  const existing = rowEl.nextElementSibling;
  if (existing && existing.classList.contains("oidc-detail-panel")) {
    // Collapse
    existing.style.maxHeight = "0";
    const indicator = rowEl.querySelector(".admin-expand-indicator");
    if (indicator) indicator.classList.remove("expanded");
    rowEl.setAttribute("aria-expanded", "false");
    setTimeout(function () {
      if (existing.parentNode) existing.remove();
    }, 160);
    return;
  }
  // Collapse any other open panel first
  const openPanels = document.querySelectorAll(
    "#admin-users-table .oidc-detail-panel",
  );
  for (let i = 0; i < openPanels.length; i++) {
    openPanels[i].style.maxHeight = "0";
    const prevRow = openPanels[i].previousElementSibling;
    if (prevRow) {
      const ind = prevRow.querySelector(".admin-expand-indicator");
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
  const indicator = rowEl.querySelector(".admin-expand-indicator");
  if (indicator) indicator.classList.add("expanded");
  rowEl.setAttribute("aria-expanded", "true");
  // Create panel (role="none" so it doesn't break the parent role="list")
  const panel = document.createElement("div");
  panel.className = "oidc-detail-panel";
  panel.setAttribute("role", "none");
  setSafeHtml(
    panel,
    '<div class="oidc-detail-inner">' +
      '<div class="oidc-detail-header">OIDC Identities</div>' +
      '<div class="oidc-detail-body"><span class="oidc-detail-empty">Loading\u2026</span></div>' +
      "</div>",
  );
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
      const body = panel.querySelector(".oidc-detail-body");
      if (body)
        setSafeHtml(
          body,
          '<span class="oidc-detail-empty">Failed to load</span>',
        );
    });
}

function _buildOidcRow(oid, userId, username) {
  const shortIssuer = _issuerShortName(oid.issuer || "");
  const shortSubject =
    (oid.subject || "").length > 12
      ? (oid.subject || "").slice(0, 12) + "\u2026"
      : oid.subject || "";
  const lastLogin = oid.last_login ? _relativeTime(oid.last_login) : "never";
  return (
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
    "</span></div>"
  );
}

function _renderOidcDetail(panel, identities, userId, username) {
  const body = panel.querySelector(".oidc-detail-body");
  if (!body) return;
  if (!identities.length) {
    setSafeHtml(
      body,
      '<span class="oidc-detail-empty">No OIDC identities linked</span>',
    );
    panel.style.maxHeight = panel.scrollHeight + "px";
    return;
  }
  let html = "";
  for (let i = 0; i < identities.length; i++) {
    html += _buildOidcRow(identities[i], userId, username);
  }
  setSafeHtml(body, html);
  // Update panel height for animation
  panel.style.maxHeight = panel.scrollHeight + "px";
  // Bind unlink buttons
  const btns = body.querySelectorAll("[data-oidc-issuer]");
  for (let j = 0; j < btns.length; j++) {
    btns[j].addEventListener("click", function (e) {
      e.stopPropagation();
      const issuer = this.getAttribute("data-oidc-issuer");
      const subject = this.getAttribute("data-oidc-subject");
      const uname = this.getAttribute("data-oidc-username");
      const uid = this.getAttribute("data-oidc-user-id");
      _confirmUnlinkOidc(issuer, subject, uname, uid);
    });
  }
}

function _confirmUnlinkOidc(issuer, subject, username, userId) {
  const shortIssuer = _issuerShortName(issuer);
  const shortSubject =
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
          const allRows = document.querySelectorAll(
            "#admin-users-table .admin-row[data-expandable]",
          );
          let targetRow = null;
          for (let ri = 0; ri < allRows.length; ri++) {
            if (allRows[ri].getAttribute("data-user-id") === userId) {
              targetRow = allRows[ri];
              break;
            }
          }
          if (targetRow) {
            const panel = targetRow.nextElementSibling;
            if (panel && panel.classList.contains("oidc-detail-panel")) {
              const body = panel.querySelector(".oidc-detail-body");
              if (body)
                setSafeHtml(
                  body,
                  '<span class="oidc-detail-empty">Loading\u2026</span>',
                );
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
                    setSafeHtml(
                      body,
                      '<span class="oidc-detail-empty">Failed to load</span>',
                    );
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
    const host = new URL(issuer).hostname;
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
    const then = new Date(
      isoStr + (isoStr.includes("Z") || isoStr.includes("+") ? "" : "Z"),
    );
    const diff = (Date.now() - then.getTime()) / 1000;
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
  const sel = document.getElementById("admin-token-user");
  const current = sel.value;
  setSafeHtml(sel, '<option value="">Select user...</option>');
  for (let i = 0; i < _adminUsers.length; i++) {
    const u = _adminUsers[i];
    const opt = document.createElement("option");
    opt.value = u.user_id;
    opt.textContent = u.username + " (" + u.display_name + ")";
    sel.appendChild(opt);
  }
  if (current) sel.value = current;
}

function loadAdminTokens() {
  const userId = document.getElementById("admin-token-user").value;
  _adminTokenUserId = userId;
  if (!userId) {
    setSafeHtml(
      document.getElementById("admin-tokens-table"),
      '<div class="dashboard-empty">Select a user to view tokens</div>',
    );
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
      setSafeHtml(
        document.getElementById("admin-tokens-table"),
        '<div class="dashboard-empty">Failed to load tokens</div>',
      );
    });
}

function _renderTokens(tokens) {
  const container = document.getElementById("admin-tokens-table");
  if (!tokens.length) {
    setSafeHtml(
      container,
      '<div class="dashboard-empty">No tokens for this user</div>',
    );
    return;
  }
  let html = "";
  for (let i = 0; i < tokens.length; i++) {
    const t = tokens[i];
    const expires = t.expires ? escapeHtml(t.expires).slice(0, 10) : "\u2014";
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
      _kebabMenu([
        {
          label: "revoke",
          kind: "danger",
          title: "Revoke token",
          attrs: { "data-revoke-token": t.token_id },
        },
      ]) +
      "</span>" +
      "</div>";
  }
  setSafeHtml(container, html);
  // Bind revoke buttons via delegation (avoids inline JS injection)
  const rbtns = container.querySelectorAll("[data-revoke-token]");
  for (let j = 0; j < rbtns.length; j++) {
    rbtns[j].addEventListener("click", function () {
      confirmRevokeToken(this.getAttribute("data-revoke-token"));
    });
  }
}

function _renderScopeBadges(scopes) {
  if (!scopes) return "";
  const parts = scopes.split(",");
  let html = "";
  for (let i = 0; i < parts.length; i++) {
    const s = parts[i].trim();
    if (!s) continue;
    let cls = "scope-badge";
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
  const sel = document.getElementById("admin-channel-user");
  const current = sel.value;
  setSafeHtml(sel, '<option value="">Select user...</option>');
  for (let i = 0; i < _adminUsers.length; i++) {
    const u = _adminUsers[i];
    const opt = document.createElement("option");
    opt.value = u.user_id;
    opt.textContent = u.username + " (" + u.display_name + ")";
    sel.appendChild(opt);
  }
  if (current) sel.value = current;
}

function loadAdminChannels() {
  const userId = document.getElementById("admin-channel-user").value;
  _adminChannelUserId = userId;
  if (!userId) {
    setSafeHtml(
      document.getElementById("admin-channels-table"),
      '<div class="dashboard-empty">Select a user to view channel links</div>',
    );
    return;
  }
  setSafeHtml(
    document.getElementById("admin-channels-table"),
    '<div class="dashboard-empty">Loading channel links...</div>',
  );
  authFetch("/v1/api/admin/users/" + encodeURIComponent(userId) + "/channels")
    .then(function (r) {
      if (!r.ok) throw new Error("Failed to load channels");
      return r.json();
    })
    .then(function (data) {
      _renderChannels(data.channels || []);
    })
    .catch(function () {
      setSafeHtml(
        document.getElementById("admin-channels-table"),
        '<div class="dashboard-empty">Failed to load channel links</div>',
      );
    });
}

function _renderChannels(channels) {
  const container = document.getElementById("admin-channels-table");
  if (!channels.length) {
    setSafeHtml(
      container,
      '<div class="dashboard-empty">No channel links for this user</div>',
    );
    return;
  }
  let html = "";
  for (let i = 0; i < channels.length; i++) {
    const c = channels[i];
    // Per-platform badge class (scope-discord / scope-slack) so different
    // adapters render with their own color.  Falls back to the generic
    // scope-channel for unknown platforms; the per-platform class wins
    // by being the only class set, not by source order.
    const ctSlug = (c.channel_type || "")
      .toLowerCase()
      .replace(/[^a-z0-9]/g, "");
    const ctClass =
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
      _kebabMenu([
        {
          label: "unlink",
          kind: "danger",
          title: "Unlink channel account",
          attrs: {
            "data-unlink-type": c.channel_type,
            "data-unlink-uid": c.channel_user_id,
          },
        },
      ]) +
      "</span>" +
      "</div>";
  }
  setSafeHtml(container, html);
  const btns = container.querySelectorAll("[data-unlink-type]");
  for (let j = 0; j < btns.length; j++) {
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
      setSafeHtml(
        document.getElementById("admin-schedules-table"),
        '<div class="dashboard-empty">Failed to load schedules</div>',
      );
    });
}

function _renderSchedules(schedules) {
  const container = document.getElementById("admin-schedules-table");
  if (!schedules.length) {
    setSafeHtml(
      container,
      '<div class="dashboard-empty">No scheduled tasks. Create one to get started.</div>',
    );
    return;
  }
  let html = "";
  for (let i = 0; i < schedules.length; i++) {
    const s = schedules[i];
    const typeLabel = s.schedule_type === "cron" ? "cron" : "at";
    const typeCls =
      s.schedule_type === "cron" ? "scope-write" : "scope-approve";
    const schedule =
      s.schedule_type === "cron"
        ? s.cron_expr
        : _utcToLocalDatetime(s.at_time).replace("T", " ");
    const target = s.target_mode;
    const nextRun = s.next_run
      ? _utcToLocalDatetime(s.next_run).replace("T", " ")
      : "\u2014";
    const enabled = s.enabled;
    let statusCls = enabled ? "sched-active" : "sched-disabled";
    let statusLabel = enabled ? "active" : "disabled";
    let statusDot = enabled ? "\u25cf " : "\u25cb ";
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
      _kebabMenu([
        {
          label: "edit",
          title: "Edit",
          attrs: { "data-edit-sched": s.task_id },
        },
        {
          label: "runs",
          title: "Run history",
          attrs: { "data-runs-sched": s.task_id },
        },
        {
          label: enabled ? "disable" : "enable",
          title: enabled ? "Disable" : "Enable",
          attrs: {
            "data-toggle-sched": s.task_id,
            "data-enabled": enabled ? "1" : "0",
          },
        },
        {
          label: "delete",
          kind: "danger",
          title: "Delete",
          attrs: { "data-delete-sched": s.task_id, "data-sname": s.name },
        },
      ]) +
      "</span></div>";
  }
  setSafeHtml(container, html);
  // Bind buttons
  const editBtns = container.querySelectorAll("[data-edit-sched]");
  for (let j = 0; j < editBtns.length; j++) {
    editBtns[j].addEventListener("click", function () {
      showEditScheduleModal(this.getAttribute("data-edit-sched"));
    });
  }
  const runsBtns = container.querySelectorAll("[data-runs-sched]");
  for (let k = 0; k < runsBtns.length; k++) {
    runsBtns[k].addEventListener("click", function () {
      showScheduleRuns(this.getAttribute("data-runs-sched"));
    });
  }
  const toggleBtns = container.querySelectorAll("[data-toggle-sched]");
  for (let m = 0; m < toggleBtns.length; m++) {
    toggleBtns[m].addEventListener("click", function () {
      toggleSchedule(
        this.getAttribute("data-toggle-sched"),
        this.getAttribute("data-enabled") === "1",
      );
    });
  }
  const delBtns = container.querySelectorAll("[data-delete-sched]");
  for (let n = 0; n < delBtns.length; n++) {
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
  const sel = document.getElementById(selectId);
  // Keep the first option (placeholder) and remove the rest
  while (sel.options.length > 1) sel.remove(1);
  // Add temporary option for pre-selected value so form is correct before fetch completes
  if (opts && opts.selected) {
    const tmp = document.createElement("option");
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
      const temp = sel.querySelector("[data-temporary]");
      if (temp) temp.remove();
      let items = opts && opts.listKey ? data[opts.listKey] : data;
      if (!Array.isArray(items)) return;
      if (opts && typeof opts.filter === "function")
        items = items.filter(opts.filter);
      items.forEach(function (item) {
        const opt = document.createElement("option");
        opt.value = item[valueKey];
        opt.textContent =
          opts && opts.display ? opts.display(item) : item[labelKey];
        sel.appendChild(opt);
      });
      if (opts && opts.selected) {
        sel.value = opts.selected;
        if (sel.value !== opts.selected) {
          // The current value isn't in this list — it was filtered out
          // (a disabled/wrong-kind persona) or is outside the caller-scoped
          // feed (a private/archived project the editing admin can't see).
          // Re-add it as a "(current)" option so it round-trips; without this
          // the select falls back to the placeholder and saving an unrelated
          // field would silently CLEAR the setting.
          const keep = document.createElement("option");
          keep.value = opts.selected;
          keep.textContent = opts.selected + " (current)";
          sel.appendChild(keep);
          sel.value = opts.selected;
        }
      }
      // Caller hook for placeholder annotation / other post-load tweaks.
      // Used by the schedule modals to rewrite the bare "Default model"
      // placeholder with the resolved alias so the label matches the
      // home composer (see app.js _populateHomeModelDropdowns).
      if (opts && typeof opts.afterPopulate === "function") {
        try {
          opts.afterPopulate(sel, data, items);
        } catch (_e) {
          /* hook errors must not break the dropdown */
        }
      }
    })
    .catch(function () {
      /* dropdown stays with placeholder or temporary option */
    });
}

// Update the schedule-model placeholder option (first <option>) to
// "Default — alias (model)" using /v1/api/models's resolved
// default_alias, mirroring the home composer.  Schedules don't carry
// a coordinator/judge split, so they consume the workstream-creation
// default rather than coordinator_default_alias / judge_default_alias.
// Em-dash separator (rather than nested parens) keeps the alias's
// "(model)" suffix legible.
function _decorateScheduleModelPlaceholder(sel, data) {
  if (!sel || sel.options.length === 0) return;
  const alias = (data && data.default_alias) || "";
  if (!alias) return;
  let match = null;
  const models = (data && data.models) || [];
  for (let i = 0; i < models.length; i++) {
    if (models[i].alias === alias) {
      match = models[i];
      break;
    }
  }
  let label;
  if (match) {
    label =
      match.alias === match.model
        ? match.alias
        : match.alias + " (" + match.model + ")";
  } else {
    label = alias;
  }
  sel.options[0].textContent = "Default — " + label;
}

// Channel platforms shown in admin notify-target rows.  Mirror server-side
// channel adapters; expand here when a new adapter ships (Discord / Slack
// today, MS Teams / etc. later).
const _NOTIFY_CHANNEL_TYPES = [
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
  for (let i = 0; i < _NOTIFY_CHANNEL_TYPES.length; i++) {
    if (_NOTIFY_CHANNEL_TYPES[i].value === channelType) {
      return _NOTIFY_CHANNEL_TYPES[i].id_hint;
    }
  }
  return "ID";
}

function _addNotifyRow(prefix, targetType, targetId, channelType) {
  const container = document.getElementById(prefix + "-notify-rows");
  const row = document.createElement("div");
  row.className = "notify-row";

  const ctSel = document.createElement("select");
  ctSel.setAttribute("aria-label", "Channel platform");
  ctSel.className = "notify-row-ct";
  for (let i = 0; i < _NOTIFY_CHANNEL_TYPES.length; i++) {
    const ctOpt = document.createElement("option");
    ctOpt.value = _NOTIFY_CHANNEL_TYPES[i].value;
    ctOpt.textContent = _NOTIFY_CHANNEL_TYPES[i].label;
    ctSel.appendChild(ctOpt);
  }
  ctSel.value = channelType || "discord";

  const typeSel = document.createElement("select");
  typeSel.setAttribute("aria-label", "Target type");
  typeSel.className = "notify-row-target";
  const optCh = document.createElement("option");
  optCh.value = "channel_id";
  optCh.textContent = "Channel";
  const optUsr = document.createElement("option");
  optUsr.value = "user_id";
  optUsr.textContent = "User DM";
  typeSel.appendChild(optCh);
  typeSel.appendChild(optUsr);
  if (targetType) typeSel.value = targetType;

  const idInput = document.createElement("input");
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

  const removeBtn = document.createElement("button");
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
  const rows = document
    .getElementById(prefix + "-notify-rows")
    .querySelectorAll(".notify-row");
  const targets = [];
  for (let i = 0; i < rows.length; i++) {
    const ct =
      (rows[i].querySelector(".notify-row-ct") || {}).value || "discord";
    const type =
      (rows[i].querySelector(".notify-row-target") || {}).value || "channel_id";
    const idEl = rows[i].querySelector(".notify-row-id");
    const id = ((idEl && idEl.value) || "").trim();
    if (!id) continue;
    const t = { channel_type: ct };
    t[type] = id;
    targets.push(t);
  }
  return targets;
}

function _populateNotifyRows(prefix, targets) {
  const container = document.getElementById(prefix + "-notify-rows");
  while (container.firstChild) container.removeChild(container.firstChild);
  if (!Array.isArray(targets)) return;
  targets.forEach(function (t) {
    const targetType = "channel_id" in t ? "channel_id" : "user_id";
    const targetId = t[targetType] || "";
    _addNotifyRow(prefix, targetType, targetId, t.channel_type || "discord");
  });
}

function _localToUtcIso(localDatetimeStr) {
  // datetime-local gives "YYYY-MM-DDTHH:MM" in browser local time
  // Convert to UTC ISO string for the server
  const d = new Date(localDatetimeStr);
  if (isNaN(d.getTime())) return "";
  return d.toISOString().replace(/\.\d{3}Z$/, "+00:00");
}

function _utcToLocalDatetime(utcStr) {
  // Convert UTC ISO string to datetime-local format in browser local time
  if (!utcStr) return "";
  const d = new Date(utcStr);
  if (isNaN(d.getTime())) return utcStr.slice(0, 16);
  const pad = function (n) {
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

// --- Schedule shelf (create + edit) ---
// The cron DSL is compiled, not typed: the segmented Runs control builds
// schedule_type/cron_expr/at_time, and the NEXT RUNS read-out previews the
// result through the server's croniter (POST /v1/api/admin/schedules/preview)
// as the user edits.  Cron mode is the raw escape hatch with the same live
// read-out.  Storage is unchanged: on edit the saved expression is
// reverse-parsed back into the friendly mode when its shape matches
// (_cronToScheduleMode), else the editor opens in Cron mode.

let _schWired = false;
let _schPreviewTimer = null;
let _schPreviewSeq = 0;
let _schShelfHandle = null;

function _schMode() {
  const seg = document.getElementById("sch-seg");
  const on = seg.querySelector('[aria-pressed="true"]');
  return on ? on.getAttribute("data-mode") : "daily";
}

function _schSetMode(mode) {
  const seg = document.getElementById("sch-seg");
  seg.querySelectorAll("button[data-mode]").forEach(function (b) {
    b.setAttribute(
      "aria-pressed",
      b.getAttribute("data-mode") === mode ? "true" : "false",
    );
  });
  document
    .querySelectorAll("#schedule-shelf [data-sch-pane]")
    .forEach(function (pane) {
      pane.hidden = pane.getAttribute("data-sch-pane") !== mode;
    });
}

function _schSelectedDays() {
  const out = [];
  document
    .querySelectorAll('#sch-days button[aria-pressed="true"]')
    .forEach(function (b) {
      out.push(parseInt(b.getAttribute("data-day"), 10));
    });
  return out.sort();
}

function _schSetDays(days) {
  document.querySelectorAll("#sch-days button").forEach(function (b) {
    const d = parseInt(b.getAttribute("data-day"), 10);
    b.setAttribute("aria-pressed", days.indexOf(d) >= 0 ? "true" : "false");
  });
}

function _schTimeParts(id) {
  const v = document.getElementById(id).value || "06:00";
  const parts = v.split(":");
  return [parseInt(parts[0], 10) || 0, parseInt(parts[1], 10) || 0];
}

// Compile the builder state down to the wire fields.  Returns
// {schedule_type, cron_expr, at_time} or {error}.
function _schCompile() {
  const mode = _schMode();
  if (mode === "once") {
    const local = document.getElementById("sch-at").value || "";
    if (!local) return { error: "Pick a date and time" };
    return {
      schedule_type: "at",
      cron_expr: "",
      at_time: _localToUtcIso(local),
    };
  }
  if (mode === "cron") {
    const expr = (document.getElementById("sch-cron").value || "").trim();
    if (!expr) return { error: "Cron expression is required" };
    return { schedule_type: "cron", cron_expr: expr, at_time: "" };
  }
  let h, m;
  if (mode === "daily") {
    [h, m] = _schTimeParts("sch-time-daily");
    return {
      schedule_type: "cron",
      cron_expr: m + " " + h + " * * *",
      at_time: "",
    };
  }
  if (mode === "weekly") {
    const days = _schSelectedDays();
    if (!days.length) return { error: "Select at least one day" };
    [h, m] = _schTimeParts("sch-time-weekly");
    return {
      schedule_type: "cron",
      cron_expr: m + " " + h + " * * " + days.join(","),
      at_time: "",
    };
  }
  if (mode === "monthly") {
    const dom = parseInt(document.getElementById("sch-dom").value, 10);
    if (!dom || dom < 1 || dom > 31)
      return { error: "Day of month must be 1-31" };
    [h, m] = _schTimeParts("sch-time-monthly");
    return {
      schedule_type: "cron",
      cron_expr: m + " " + h + " " + dom + " * *",
      at_time: "",
    };
  }
  // interval
  const n = parseInt(document.getElementById("sch-every").value, 10);
  if (!n || n < 1) return { error: "Interval must be at least 1" };
  const unit = document.getElementById("sch-unit").value;
  const expr = unit === "hours" ? "0 */" + n + " * * *" : "*/" + n + " * * * *";
  return { schedule_type: "cron", cron_expr: expr, at_time: "" };
}

const _SCH_DAY_NAMES = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

function _schDescribe() {
  const mode = _schMode();
  const t = function (id) {
    const parts = _schTimeParts(id);
    const pad = function (n) {
      return n < 10 ? "0" + n : "" + n;
    };
    return pad(parts[0]) + ":" + pad(parts[1]);
  };
  if (mode === "daily") return "every day at " + t("sch-time-daily") + " UTC";
  if (mode === "weekly") {
    const names = _schSelectedDays().map(function (d) {
      return _SCH_DAY_NAMES[d];
    });
    return names.length
      ? "every " + names.join(", ") + " at " + t("sch-time-weekly") + " UTC"
      : "no days selected";
  }
  if (mode === "monthly")
    return (
      "monthly on day " +
      (document.getElementById("sch-dom").value || "?") +
      " at " +
      t("sch-time-monthly") +
      " UTC"
    );
  if (mode === "interval")
    return (
      "every " +
      (document.getElementById("sch-every").value || "?") +
      " " +
      document.getElementById("sch-unit").value
    );
  if (mode === "once") return "one time";
  return "custom cron";
}

// Reverse-parse a saved expression into builder state.  Only the exact
// shapes the builder emits round-trip; anything else opens in Cron mode.
function _cronToScheduleMode(expr) {
  let m = /^(\d{1,2}) (\d{1,2}) \* \* \*$/.exec(expr);
  if (m) return { mode: "daily", h: +m[2], min: +m[1] };
  m = /^(\d{1,2}) (\d{1,2}) \* \* ([0-7](?:,[0-7])*)$/.exec(expr);
  if (m)
    return {
      mode: "weekly",
      h: +m[2],
      min: +m[1],
      days: m[3].split(",").map(function (d) {
        return +d % 7; // cron allows 7 for Sunday
      }),
    };
  m = /^(\d{1,2}) (\d{1,2}) (\d{1,2}) \* \*$/.exec(expr);
  if (m) return { mode: "monthly", h: +m[2], min: +m[1], dom: +m[3] };
  m = /^0 \*\/(\d+) \* \* \*$/.exec(expr);
  if (m) return { mode: "interval", every: +m[1], unit: "hours" };
  m = /^\*\/(\d+) \* \* \* \*$/.exec(expr);
  if (m) return { mode: "interval", every: +m[1], unit: "minutes" };
  return { mode: "cron" };
}

function _schSetTime(id, h, min) {
  const pad = function (n) {
    return n < 10 ? "0" + n : "" + n;
  };
  document.getElementById(id).value = pad(h) + ":" + pad(min);
}

// Apply a saved schedule's timing to the builder (edit mode).
function _schApplyTiming(scheduleType, cronExpr, atTime) {
  if (scheduleType === "at") {
    document.getElementById("sch-at").value = _utcToLocalDatetime(atTime);
    _schSetMode("once");
    return;
  }
  const parsed = _cronToScheduleMode(cronExpr || "");
  if (parsed.mode === "daily")
    _schSetTime("sch-time-daily", parsed.h, parsed.min);
  else if (parsed.mode === "weekly") {
    _schSetTime("sch-time-weekly", parsed.h, parsed.min);
    _schSetDays(parsed.days);
  } else if (parsed.mode === "monthly") {
    _schSetTime("sch-time-monthly", parsed.h, parsed.min);
    document.getElementById("sch-dom").value = parsed.dom;
  } else if (parsed.mode === "interval") {
    document.getElementById("sch-every").value = parsed.every;
    document.getElementById("sch-unit").value = parsed.unit;
  } else {
    document.getElementById("sch-cron").value = cronExpr || "";
  }
  _schSetMode(parsed.mode);
}

function _schFmtUtc(iso) {
  // Server emits "YYYY-MM-DDTHH:MM:SS" (UTC, no suffix) or "+00:00" ISO.
  const d = new Date(/[Z+]/.test(iso) ? iso : iso + "Z");
  if (isNaN(d.getTime())) return iso;
  const pad = function (n) {
    return n < 10 ? "0" + n : "" + n;
  };
  return (
    _SCH_DAY_NAMES[d.getUTCDay()] +
    " " +
    d.getUTCFullYear() +
    "-" +
    pad(d.getUTCMonth() + 1) +
    "-" +
    pad(d.getUTCDate()) +
    " " +
    pad(d.getUTCHours()) +
    ":" +
    pad(d.getUTCMinutes()) +
    " UTC"
  );
}

function _schRel(iso) {
  const d = new Date(/[Z+]/.test(iso) ? iso : iso + "Z");
  if (isNaN(d.getTime())) return "";
  const mins = Math.round((d.getTime() - Date.now()) / 60000);
  if (mins < 1) return "now";
  if (mins < 60) return "in " + mins + "m";
  if (mins < 48 * 60) return "in " + Math.round(mins / 60) + "h";
  return "in " + Math.round(mins / (24 * 60)) + "d";
}

function _schRenderRuns(runs, errText) {
  const rows = document.getElementById("sch-runs-out");
  while (rows.firstChild) rows.removeChild(rows.firstChild);
  if (errText) {
    const err = document.createElement("div");
    err.className = "err";
    err.textContent = errText;
    rows.appendChild(err);
    return;
  }
  runs.forEach(function (iso) {
    const row = document.createElement("div");
    row.className = "readout-row";
    const when = document.createElement("span");
    when.textContent = _schFmtUtc(iso);
    const rel = document.createElement("span");
    rel.className = "rel";
    rel.textContent = _schRel(iso);
    row.appendChild(when);
    row.appendChild(rel);
    rows.appendChild(row);
  });
}

function _schRenderCompiled(compiled) {
  const meta = document.getElementById("sch-compiled-out");
  while (meta.firstChild) meta.removeChild(meta.firstChild);
  if (compiled.error) return;
  if (compiled.schedule_type === "at") {
    meta.appendChild(document.createTextNode("runs once at "));
    const span = document.createElement("span");
    span.className = "mono";
    span.textContent = compiled.at_time;
    meta.appendChild(span);
  } else {
    meta.appendChild(document.createTextNode("compiles to "));
    const span = document.createElement("span");
    span.className = "mono";
    span.textContent = compiled.cron_expr;
    meta.appendChild(span);
  }
}

// Debounced read-out refresh: description + compiled text render instantly,
// the next-runs list arrives from the croniter preview endpoint.
function _schPreview() {
  document.getElementById("sch-desc-out").textContent = _schDescribe();
  const compiled = _schCompile();
  _schRenderCompiled(compiled);
  if (compiled.error) {
    _schRenderRuns([], compiled.error);
    return;
  }
  if (_schPreviewTimer) clearTimeout(_schPreviewTimer);
  const seq = ++_schPreviewSeq;
  _schPreviewTimer = setTimeout(function () {
    authFetch("/v1/api/admin/schedules/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(compiled),
    })
      .then(function (r) {
        return r.json();
      })
      .then(function (data) {
        if (seq !== _schPreviewSeq) return; // a newer edit superseded us
        if (!data.valid) _schRenderRuns([], data.error || "Invalid schedule");
        else _schRenderRuns(data.next || []);
      })
      .catch(function () {
        if (seq === _schPreviewSeq) _schRenderRuns([], "Preview unavailable");
      });
  }, 250);
}

function _schWire() {
  if (_schWired) return;
  _schWired = true;
  document.getElementById("sch-seg").addEventListener("click", function (e) {
    const b = e.target.closest("button[data-mode]");
    if (!b) return;
    _schSetMode(b.getAttribute("data-mode"));
    _schPreview();
  });
  document.getElementById("sch-days").addEventListener("click", function (e) {
    const b = e.target.closest("button[data-day]");
    if (!b) return;
    b.setAttribute(
      "aria-pressed",
      b.getAttribute("aria-pressed") === "true" ? "false" : "true",
    );
    _schPreview();
  });
  [
    "sch-time-daily",
    "sch-time-weekly",
    "sch-time-monthly",
    "sch-dom",
    "sch-every",
    "sch-unit",
    "sch-at",
    "sch-cron",
  ].forEach(function (id) {
    const el = document.getElementById(id);
    el.addEventListener("input", _schPreview);
    el.addEventListener("change", _schPreview);
  });
  document.getElementById("sch-target").addEventListener("change", function () {
    const isNode = this.value === "node";
    document.getElementById("sch-node-group").hidden = !isNode;
    if (isNode) document.getElementById("sch-node").focus();
  });
  document
    .getElementById("sch-notify-add")
    .addEventListener("click", function () {
      _addNotifyRow("sch");
    });
  document
    .getElementById("sch-submit")
    .addEventListener("click", _submitScheduleShelf);
}

// Shared open path: reset to a clean create state, then edit overwrites.
function _schResetForm() {
  document
    .getElementById("schedule-shelf-error")
    .classList.remove("is-visible");
  document.getElementById("sch-id").value = "";
  document.getElementById("sch-name").value = "";
  document.getElementById("sch-desc").value = "";
  document.getElementById("sch-time-daily").value = "06:00";
  document.getElementById("sch-time-weekly").value = "06:00";
  document.getElementById("sch-time-monthly").value = "06:00";
  _schSetDays([]);
  document.getElementById("sch-dom").value = "1";
  document.getElementById("sch-every").value = "4";
  document.getElementById("sch-unit").value = "hours";
  document.getElementById("sch-at").value = "";
  document.getElementById("sch-cron").value = "";
  document.getElementById("sch-target").value = "auto";
  document.getElementById("sch-node").value = "";
  document.getElementById("sch-node-group").hidden = true;
  document.getElementById("sch-message").value = "";
  document.getElementById("sch-autoapprove").checked = false;
  document.getElementById("sch-enabled").checked = true;
  _populateNotifyRows("sch", []);
  _schSetMode("daily");
}

function _schPopulateSelects(
  selectedModel,
  selectedSkill,
  selectedPersona,
  selectedProject,
) {
  _populateScheduleSelect("sch-model", "/v1/api/models", "alias", "alias", {
    listKey: "models",
    selected: selectedModel || "",
    display: function (m) {
      return m.alias === m.model ? m.alias : m.alias + " (" + m.model + ")";
    },
    afterPopulate: _decorateScheduleModelPlaceholder,
  });
  _populateScheduleSelect(
    "sch-template",
    "/v1/api/admin/skills",
    "name",
    "name",
    {
      listKey: "skills",
      selected: selectedSkill || "",
      display: function (s) {
        return s.name;
      },
    },
  );
  // Schedules dispatch interactive workstreams, so only offer personas
  // eligible for that kind; the label matches the home/create picker
  // (display name, falling back to the slug).
  _populateScheduleSelect("sch-persona", "/v1/api/personas", "name", "name", {
    listKey: "personas",
    selected: selectedPersona || "",
    filter: function (p) {
      return (p.applies_to_kinds || []).indexOf("interactive") !== -1;
    },
    display: function (p) {
      return p.display_name || p.name;
    },
  });
  _populateScheduleSelect(
    "sch-project",
    "/v1/api/projects",
    "name",
    "project_id",
    {
      listKey: "projects",
      selected: selectedProject || "",
      display: function (p) {
        return p.name;
      },
    },
  );
}

function _schOpen(title, tag, kind, submitLabel) {
  const shelf = document.getElementById("schedule-shelf");
  document.getElementById("schedule-shelf-title").textContent = title;
  document.getElementById("schedule-shelf-tag").textContent = tag;
  shelf.setAttribute("data-kind", kind);
  document.getElementById("sch-submit").textContent = submitLabel;
  _schShelfHandle = window.TurnstoneHatch.openShelf(shelf);
  document.getElementById("sch-name").focus();
  _schPreview();
}

function showCreateScheduleModal() {
  _schWire();
  _schResetForm();
  document.getElementById("sch-enabled-row").hidden = true;
  _schPopulateSelects("", "", "", "");
  _schOpen("New schedule", "SCH-NEW", "create", "Create");
}

function showEditScheduleModal(taskId) {
  _schWire();
  authFetch("/v1/api/admin/schedules/" + encodeURIComponent(taskId))
    .then(function (r) {
      if (!r.ok) throw new Error("Not found");
      return r.json();
    })
    .then(function (s) {
      _schResetForm();
      document.getElementById("sch-id").value = s.task_id;
      document.getElementById("sch-name").value = s.name || "";
      document.getElementById("sch-desc").value = s.description || "";
      _schApplyTiming(s.schedule_type, s.cron_expr, s.at_time);
      const isSpecificNode =
        s.target_mode &&
        s.target_mode !== "auto" &&
        s.target_mode !== "pool" &&
        s.target_mode !== "all";
      document.getElementById("sch-target").value = isSpecificNode
        ? "node"
        : s.target_mode;
      document.getElementById("sch-node").value = isSpecificNode
        ? s.target_mode
        : "";
      document.getElementById("sch-node-group").hidden = !isSpecificNode;
      _schPopulateSelects(
        s.model || "",
        s.skill || "",
        s.persona || "",
        s.project_id || "",
      );
      document.getElementById("sch-message").value = s.initial_message || "";
      document.getElementById("sch-autoapprove").checked = !!s.auto_approve;
      document.getElementById("sch-enabled").checked = !!s.enabled;
      document.getElementById("sch-enabled-row").hidden = false;
      _populateNotifyRows("sch", s.notify_targets || []);
      _schOpen(
        "Edit schedule — " + (s.name || s.task_id),
        "SCH-EDIT",
        "edit",
        "Save",
      );
    })
    .catch(function () {
      showToast("Failed to load schedule");
    });
}

function _submitScheduleShelf() {
  const shelf = document.getElementById("schedule-shelf");
  const errEl = document.getElementById("schedule-shelf-error");
  const taskId = document.getElementById("sch-id").value;
  const name = (document.getElementById("sch-name").value || "").trim();
  const message = (document.getElementById("sch-message").value || "").trim();

  if (!name) return _showModalError(errEl, "Name is required");
  if (!message) return _showModalError(errEl, "Initial message is required");
  const compiled = _schCompile();
  if (compiled.error) return _showModalError(errEl, compiled.error);

  let targetMode = document.getElementById("sch-target").value;
  if (targetMode === "node") {
    targetMode = (document.getElementById("sch-node").value || "").trim();
    if (!targetMode) return _showModalError(errEl, "Node ID is required");
  }

  const body = {
    name: name,
    description: (document.getElementById("sch-desc").value || "").trim(),
    schedule_type: compiled.schedule_type,
    cron_expr: compiled.cron_expr,
    at_time: compiled.at_time,
    target_mode: targetMode,
    model: (document.getElementById("sch-model").value || "").trim(),
    skill: (document.getElementById("sch-template").value || "").trim(),
    persona: (document.getElementById("sch-persona").value || "").trim(),
    project_id: (document.getElementById("sch-project").value || "").trim(),
    initial_message: message,
    auto_approve: document.getElementById("sch-autoapprove").checked,
    notify_targets: _collectNotifyTargets("sch"),
  };
  if (taskId) body.enabled = document.getElementById("sch-enabled").checked;

  errEl.classList.remove("is-visible");
  window.TurnstoneHatch.setBusy(shelf, true);
  authFetch(
    taskId
      ? "/v1/api/admin/schedules/" + encodeURIComponent(taskId)
      : "/v1/api/admin/schedules",
    {
      method: taskId ? "PUT" : "POST",
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
    .then(function () {
      window.TurnstoneHatch.setBusy(shelf, false);
      if (_schShelfHandle) _schShelfHandle.close();
      showToast(
        taskId ? "Schedule updated" : "Schedule '" + name + "' created",
      );
      loadAdminSchedules();
    })
    .catch(function (err) {
      window.TurnstoneHatch.setBusy(shelf, false);
      _showModalError(errEl, err.message || "Failed to save schedule");
    });
}

// --- Schedule Runs Modal ---

function showScheduleRuns(taskId) {
  authFetch(
    "/v1/api/admin/schedules/" + encodeURIComponent(taskId) + "/runs?limit=50",
  )
    .then(function (r) {
      if (!r.ok) throw new Error("Not found");
      return r.json();
    })
    .then(function (data) {
      const runs = data.runs || [];
      const container = document.getElementById("schedule-runs-table");
      if (!runs.length) {
        setSafeHtml(
          container,
          '<div class="dashboard-empty">No runs yet</div>',
        );
      } else {
        let html =
          '<div class="admin-colheaders sched-runs-grid" aria-hidden="true">' +
          '<span class="admin-col">STARTED</span>' +
          '<span class="admin-col">NODE</span>' +
          '<span class="admin-col">STATUS</span>' +
          '<span class="admin-col">ERROR</span></div>';
        for (let i = 0; i < runs.length; i++) {
          const r = runs[i];
          const statusCls =
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
        setSafeHtml(container, html);
      }
      window.TurnstoneHatch.openShelf(
        document.getElementById("schedule-runs-shelf"),
      );
    })
    .catch(function () {
      showToast("Failed to load run history");
    });
}

function hideScheduleRunsModal() {
  window.TurnstoneHatch.closeShelf(
    document.getElementById("schedule-runs-shelf"),
  );
}

// ---------------------------------------------------------------------------
// Watches
// ---------------------------------------------------------------------------

function _populateWatchNodeSelect() {
  const sel = document.getElementById("admin-watch-node");
  const current = sel.value;
  const seen = {};
  setSafeHtml(sel, '<option value="">All nodes</option>');
  for (let i = 0; i < _adminWatches.length; i++) {
    const nid = _adminWatches[i].node_id || "";
    if (nid && !seen[nid]) {
      seen[nid] = true;
      const opt = document.createElement("option");
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
      const nodeFilter = document.getElementById("admin-watch-node").value;
      let filtered = _adminWatches;
      if (nodeFilter) {
        filtered = _adminWatches.filter(function (w) {
          return w.node_id === nodeFilter;
        });
      }
      _renderWatches(filtered);
    })
    .catch(function () {
      setSafeHtml(
        document.getElementById("admin-watches-table"),
        '<div class="dashboard-empty">Failed to load watches</div>',
      );
    });
}

function _formatInterval(secs) {
  if (!secs || secs <= 0) return "\u2014";
  if (secs >= 3600) return Math.round(secs / 3600) + "h";
  if (secs >= 60) return Math.round(secs / 60) + "m";
  return secs + "s";
}

function _renderWatches(watches) {
  const container = document.getElementById("admin-watches-table");
  if (!watches.length) {
    setSafeHtml(
      container,
      '<div class="dashboard-empty">No active watches. Watches are created when workstreams use the watch tool.</div>',
    );
    return;
  }
  let html = "";
  for (let i = 0; i < watches.length; i++) {
    const w = watches[i];
    const name = w.name || w.watch_id || "\u2014";
    const nodeShort = (w.node_id || "").slice(0, 8);
    const cmd = w.command || "";
    const cmdTrunc = cmd.length > 40 ? cmd.slice(0, 40) + "\u2026" : cmd;
    const interval = _formatInterval(w.interval_secs);
    const pollMax = w.max_polls ? w.max_polls : "\u221e";
    const pollLabel = (w.poll_count || 0) + "/" + pollMax;
    const cond = w.stop_on || "on change";
    const condTrunc = cond.length > 30 ? cond.slice(0, 30) + "\u2026" : cond;
    const active = w.active;
    const statusCls = active ? "watch-active" : "watch-completed";
    const statusLabel = active ? "active" : "done";
    const statusDot = active ? "\u25cf " : "\u25cb ";
    // Only active watches can be cancelled; completed rows render no menu.
    const cancelBtn = _kebabMenu([
      active
        ? {
            label: "cancel",
            kind: "danger",
            title: "Cancel watch",
            attrs: {
              "data-cancel-watch": w.watch_id,
              "data-watch-node": w.node_id || "",
              "data-watch-name": name,
            },
          }
        : null,
    ]);
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
  setSafeHtml(container, html);
  // Bind cancel buttons
  const btns = container.querySelectorAll("[data-cancel-watch]");
  for (let j = 0; j < btns.length; j++) {
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
    "Stop watch",
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

let _channelWired = false;

function _channelWire() {
  if (_channelWired) return;
  _channelWired = true;
  const ctSel = document.getElementById("cc-type");
  ctSel.addEventListener("change", function () {
    document.getElementById("cc-uid").placeholder = _notifyIdPlaceholder(
      ctSel.value,
    );
  });
  document
    .getElementById("cc-submit")
    .addEventListener("click", submitCreateChannel);
}

function showCreateChannelModal() {
  if (!_adminChannelUserId) {
    showToast("Select a user first");
    return;
  }
  _channelWire();
  const shelf = document.getElementById("channel-shelf");
  document.getElementById("channel-shelf-error").classList.remove("is-visible");
  const ctSel = document.getElementById("cc-type");
  const uidInput = document.getElementById("cc-uid");
  ctSel.value = "discord";
  uidInput.value = "";
  uidInput.placeholder = _notifyIdPlaceholder(ctSel.value);
  window.TurnstoneHatch.openShelf(shelf);
  ctSel.focus();
}

function hideCreateChannelModal() {
  window.TurnstoneHatch.closeShelf(document.getElementById("channel-shelf"));
}

function submitCreateChannel() {
  const shelf = document.getElementById("channel-shelf");
  const channelType = document.getElementById("cc-type").value;
  const channelUserId = (document.getElementById("cc-uid").value || "").trim();
  const errEl = document.getElementById("channel-shelf-error");

  if (!channelUserId)
    return _showModalError(errEl, "External user ID is required");

  errEl.classList.remove("is-visible");
  window.TurnstoneHatch.setBusy(shelf, true);
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
      window.TurnstoneHatch.setBusy(shelf, false);
      hideCreateChannelModal();
      showToast("Channel account linked");
      loadAdminChannels();
    })
    .catch(function (err) {
      window.TurnstoneHatch.setBusy(shelf, false);
      _showModalError(errEl, err.message || "Failed to link channel account");
    });
}

// ---------------------------------------------------------------------------
// Create User Modal
// ---------------------------------------------------------------------------

let _userWired = false;

function _userWire() {
  if (_userWired) return;
  _userWired = true;
  document
    .getElementById("cu-submit")
    .addEventListener("click", submitCreateUser);
}

function showCreateUserModal() {
  _userWire();
  const shelf = document.getElementById("user-shelf");
  document.getElementById("user-shelf-error").classList.remove("is-visible");
  document.getElementById("cu-username").value = "";
  document.getElementById("cu-displayname").value = "";
  document.getElementById("cu-password").value = "";
  document.getElementById("cu-confirm").value = "";
  window.TurnstoneHatch.openShelf(shelf);
  document.getElementById("cu-username").focus();
}

function hideCreateUserModal() {
  window.TurnstoneHatch.closeShelf(document.getElementById("user-shelf"));
}

function submitCreateUser() {
  const shelf = document.getElementById("user-shelf");
  const username = (document.getElementById("cu-username").value || "").trim();
  const displayName = (
    document.getElementById("cu-displayname").value || ""
  ).trim();
  const password = document.getElementById("cu-password").value || "";
  const confirm = document.getElementById("cu-confirm").value || "";
  const errEl = document.getElementById("user-shelf-error");

  if (!username) return _showModalError(errEl, "Username is required");
  if (!displayName) return _showModalError(errEl, "Display name is required");
  if (!password) return _showModalError(errEl, "Password is required");
  if (password.length < 8)
    return _showModalError(errEl, "Password must be at least 8 characters");
  if (password !== confirm)
    return _showModalError(errEl, "Passwords do not match");

  errEl.classList.remove("is-visible");
  window.TurnstoneHatch.setBusy(shelf, true);
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
      window.TurnstoneHatch.setBusy(shelf, false);
      hideCreateUserModal();
      showToast("User '" + username + "' created");
      loadAdminUsers();
    })
    .catch(function (err) {
      window.TurnstoneHatch.setBusy(shelf, false);
      _showModalError(errEl, err.message || "Failed to create user");
    });
}

// ---------------------------------------------------------------------------
// Projects (resource containers) — list + create/edit + member whitelist.
// Clones the Users tab (loadAdminUsers / showCreateUserModal / _renderUsers).
// The tab gates on project.read; the server re-gates each mutation on
// project.{create,write,delete} + per-project ownership, so a read-only viewer
// sees the list but every action 403s (surfaced inline / as a toast).
// ---------------------------------------------------------------------------
let _adminProjects = [];
let _projectShelfWired = false;
let _projectMembersWired = false;

function loadAdminProjects() {
  authFetch("/v1/api/projects?include_archived=1")
    .then(function (r) {
      if (!r.ok) throw new Error("Failed to load projects");
      return r.json();
    })
    .then(function (data) {
      _adminProjects = data.projects || [];
      _renderProjects(_adminProjects);
    })
    .catch(function () {
      setSafeHtml(
        document.getElementById("admin-projects-table"),
        '<div class="dashboard-empty">Failed to load projects</div>',
      );
    });
}

function _renderProjects(projects) {
  const container = document.getElementById("admin-projects-table");
  if (!projects.length) {
    setSafeHtml(
      container,
      '<div class="dashboard-empty">No projects yet. Create one to get started.</div>',
    );
    return;
  }
  let html = "";
  for (let i = 0; i < projects.length; i++) {
    const p = projects[i];
    const archived = p.state === "archived";
    html +=
      '<div class="admin-row" role="listitem" data-expandable data-project-id="' +
      escapeHtml(p.project_id) +
      '" tabindex="0" aria-expanded="false">' +
      '<span class="admin-col admin-col-username">' +
      '<span class="admin-expand-indicator" aria-hidden="true">▸</span>' +
      escapeHtml(p.name) +
      "</span>" +
      '<span class="admin-col admin-col-name">' +
      (p.visibility === "public" ? "Public" : "Private") +
      "</span>" +
      '<span class="admin-col admin-col-created">' +
      (archived ? "Archived" : "Active") +
      "</span>" +
      '<span class="admin-col admin-col-actions">' +
      _kebabMenu([
        {
          label: "edit",
          title: "Rename / visibility",
          attrs: { "data-edit-project": p.project_id },
        },
        {
          label: "members",
          title: "Manage members",
          attrs: { "data-project-members": p.project_id },
        },
        {
          label: archived ? "unarchive" : "archive",
          title: archived ? "Reactivate project" : "Archive project",
          attrs: {
            "data-archive-project": p.project_id,
            "data-archive-state": archived ? "active" : "archived",
          },
        },
        {
          label: "delete",
          kind: "danger",
          title: "Delete project",
          attrs: {
            "data-delete-project": p.project_id,
            "data-project-name": p.name,
          },
        },
      ]) +
      "</span>" +
      "</div>";
  }
  setSafeHtml(container, html);
  _bindProjectRowActions(container);
}

function _bindProjectRowActions(container) {
  container.querySelectorAll("[data-edit-project]").forEach(function (b) {
    b.addEventListener("click", function () {
      showEditProjectModal(this.getAttribute("data-edit-project"));
    });
  });
  container.querySelectorAll("[data-project-members]").forEach(function (b) {
    b.addEventListener("click", function () {
      showProjectMembersModal(this.getAttribute("data-project-members"));
    });
  });
  container.querySelectorAll("[data-archive-project]").forEach(function (b) {
    b.addEventListener("click", function () {
      _setProjectState(
        this.getAttribute("data-archive-project"),
        this.getAttribute("data-archive-state"),
      );
    });
  });
  container.querySelectorAll("[data-delete-project]").forEach(function (b) {
    b.addEventListener("click", function () {
      confirmDeleteProject(
        this.getAttribute("data-delete-project"),
        this.getAttribute("data-project-name"),
      );
    });
  });
  // Expandable per-project resources panel (workstreams / attachments /
  // memory) — same interaction contract as the Users tab's OIDC panel.
  container
    .querySelectorAll(".admin-row[data-expandable]")
    .forEach(function (row) {
      const _expand = function () {
        _toggleProjectPanel(row.getAttribute("data-project-id"), row);
      };
      row.addEventListener("click", function (e) {
        // Clicks on the row's kebab menu must not also toggle the panel.
        if (
          e.target.closest(".admin-kebab") ||
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
    });
}

function _toggleProjectPanel(projectId, rowEl) {
  const existing = rowEl.nextElementSibling;
  if (existing && existing.classList.contains("proj-detail-panel")) {
    // Collapse
    existing.style.maxHeight = "0";
    const indicator = rowEl.querySelector(".admin-expand-indicator");
    if (indicator) indicator.classList.remove("expanded");
    rowEl.setAttribute("aria-expanded", "false");
    setTimeout(function () {
      if (existing.parentNode) existing.remove();
    }, 160);
    return;
  }
  // Collapse any other open panel first (single-open accordion).
  const openPanels = document.querySelectorAll(
    "#admin-projects-table .proj-detail-panel",
  );
  for (let i = 0; i < openPanels.length; i++) {
    openPanels[i].style.maxHeight = "0";
    const prevRow = openPanels[i].previousElementSibling;
    if (prevRow) {
      const ind = prevRow.querySelector(".admin-expand-indicator");
      if (ind) ind.classList.remove("expanded");
      prevRow.setAttribute("aria-expanded", "false");
    }
    (function (panel) {
      setTimeout(function () {
        if (panel.parentNode) panel.remove();
      }, 160);
    })(openPanels[i]);
  }
  const indicator = rowEl.querySelector(".admin-expand-indicator");
  if (indicator) indicator.classList.add("expanded");
  rowEl.setAttribute("aria-expanded", "true");
  const panel = document.createElement("div");
  panel.className = "proj-detail-panel";
  panel.setAttribute("role", "none");
  setSafeHtml(
    panel,
    '<div class="proj-detail-inner">' +
      '<div class="proj-detail-body"><span class="proj-detail-empty">Loading…</span></div>' +
      "</div>",
  );
  rowEl.after(panel);
  requestAnimationFrame(function () {
    panel.style.maxHeight = panel.scrollHeight + "px";
  });
  authFetch("/v1/api/projects/" + encodeURIComponent(projectId) + "/resources")
    .then(function (r) {
      if (!r.ok) throw new Error("Failed");
      return r.json();
    })
    .then(function (data) {
      _renderProjectResources(panel, data);
    })
    .catch(function () {
      const body = panel.querySelector(".proj-detail-body");
      if (body)
        setSafeHtml(
          body,
          '<span class="proj-detail-empty">Failed to load</span>',
        );
    });
}

const _PROJ_ATT_ICONS = { image: "\u{1f5bc}", audio: "\u{1f3b5}" };

function _projAttachmentHref(att) {
  // Content serving is ws-scoped and node-local: interactive workstreams
  // route through the console's transparent node proxy; coordinator
  // workstreams (no node_id recorded on the attachment's ws row here)
  // serve from the console's own coord attachment routes.
  const tail =
    "v1/api/workstreams/" +
    encodeURIComponent(att.ws_id) +
    "/attachments/" +
    encodeURIComponent(att.attachment_id) +
    "/content";
  return att.node_id
    ? "/node/" + encodeURIComponent(att.node_id) + "/" + tail
    : "/" + tail;
}

function _projFmtSize(n) {
  if (typeof n !== "number" || n < 0) return "";
  if (n < 1024) return n + " B";
  if (n < 1048576) return (n / 1024).toFixed(1) + " KB";
  return (n / 1048576).toFixed(1) + " MB";
}

function _renderProjectResources(panel, data) {
  const body = panel.querySelector(".proj-detail-body");
  if (!body) return;
  const wss = data.workstreams || [];
  // node_id lives on the workstream rows; the attachment rows carry only
  // their first-referencing ws_id — join here for download URLs.
  const nodeByWs = {};
  for (let i = 0; i < wss.length; i++) nodeByWs[wss[i].ws_id] = wss[i].node_id;
  let html =
    '<div class="proj-detail-header">Workstreams (' + wss.length + ")</div>";
  if (!wss.length) {
    html += '<span class="proj-detail-empty">No workstreams</span>';
  } else {
    for (let i = 0; i < wss.length; i++) {
      const w = wss[i];
      html +=
        '<div class="proj-detail-row">' +
        '<span class="proj-detail-main">' +
        escapeHtml(w.title || w.name || w.ws_id.substring(0, 12)) +
        "</span>" +
        '<span class="proj-detail-dim">' +
        escapeHtml(String(w.kind || "")) +
        " · " +
        escapeHtml(String(w.state || "")) +
        " · " +
        escapeHtml(String(w.updated || "").slice(0, 10)) +
        " · " +
        escapeHtml(w.ws_id.substring(0, 7)) +
        "</span>" +
        "</div>";
    }
  }
  const atts = data.attachments || [];
  html +=
    '<div class="proj-detail-header">Attachments (' + atts.length + ")</div>";
  if (!atts.length) {
    html += '<span class="proj-detail-empty">No attachments</span>';
  } else {
    for (let i = 0; i < atts.length; i++) {
      const a = atts[i];
      const icon = _PROJ_ATT_ICONS[a.kind] || "\u{1f4c4}";
      a.node_id = nodeByWs[a.ws_id] || "";
      html +=
        '<div class="proj-detail-row">' +
        '<span class="proj-detail-main">' +
        '<span aria-hidden="true">' +
        icon +
        "</span> " +
        '<a class="proj-detail-link" target="_blank" rel="noopener" href="' +
        escapeHtml(_projAttachmentHref(a)) +
        '">' +
        escapeHtml(a.filename || a.attachment_id.substring(0, 12)) +
        "</a>" +
        "</span>" +
        '<span class="proj-detail-dim">' +
        escapeHtml(_projFmtSize(a.size_bytes)) +
        " · " +
        escapeHtml(String(a.created || "").slice(0, 10)) +
        "</span>" +
        "</div>";
    }
  }
  html +=
    '<div class="proj-detail-header">Memory</div>' +
    '<div class="proj-detail-row"><span class="proj-detail-main">' +
    String(data.memory_count || 0) +
    " project-scoped memor" +
    (data.memory_count === 1 ? "y" : "ies") +
    "</span></div>";
  setSafeHtml(body, html);
  // Re-measure after content lands so the animated max-height fits.
  requestAnimationFrame(function () {
    panel.style.maxHeight = panel.scrollHeight + "px";
  });
}

function _projectById(pid) {
  for (let i = 0; i < _adminProjects.length; i++)
    if (_adminProjects[i].project_id === pid) return _adminProjects[i];
  return null;
}

// Refresh the shared picker/rail cache after any project mutation so the
// launcher dropdowns + the rail's group-by-project pick up the change.
function _afterProjectMutation() {
  loadAdminProjects();
  if (window.TurnstoneProjects) window.TurnstoneProjects.refreshProjects();
}

function _projectShelfWire() {
  if (_projectShelfWired) return;
  _projectShelfWired = true;
  document
    .getElementById("cp-submit")
    .addEventListener("click", submitProjectShelf);
}

function showCreateProjectModal() {
  _projectShelfWire();
  const shelf = document.getElementById("project-shelf");
  document.getElementById("project-shelf-error").classList.remove("is-visible");
  document.getElementById("cp-project-id").value = "";
  document.getElementById("cp-name").value = "";
  document.getElementById("cp-visibility").value = "private";
  document.getElementById("project-shelf-title").textContent = "New project";
  document.getElementById("cp-submit").textContent = "Create";
  window.TurnstoneHatch.openShelf(shelf);
  document.getElementById("cp-name").focus();
}

function showEditProjectModal(pid) {
  const p = _projectById(pid);
  if (!p) return;
  _projectShelfWire();
  const shelf = document.getElementById("project-shelf");
  document.getElementById("project-shelf-error").classList.remove("is-visible");
  document.getElementById("cp-project-id").value = p.project_id;
  document.getElementById("cp-name").value = p.name;
  document.getElementById("cp-visibility").value = p.visibility || "private";
  document.getElementById("project-shelf-title").textContent = "Edit project";
  document.getElementById("cp-submit").textContent = "Save";
  window.TurnstoneHatch.openShelf(shelf);
  document.getElementById("cp-name").focus();
}

function submitProjectShelf() {
  const shelf = document.getElementById("project-shelf");
  const pid = document.getElementById("cp-project-id").value;
  const name = (document.getElementById("cp-name").value || "").trim();
  const visibility = document.getElementById("cp-visibility").value;
  const errEl = document.getElementById("project-shelf-error");
  if (!name) return _showModalError(errEl, "Name is required");

  errEl.classList.remove("is-visible");
  window.TurnstoneHatch.setBusy(shelf, true);
  const editing = !!pid;
  const url = editing
    ? "/v1/api/projects/" + encodeURIComponent(pid)
    : "/v1/api/projects";
  authFetch(url, {
    method: editing ? "PATCH" : "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name: name, visibility: visibility }),
  })
    .then(function (r) {
      if (!r.ok)
        return r.json().then(function (d) {
          throw new Error(d.error || "Failed");
        });
      return r.json();
    })
    .then(function () {
      window.TurnstoneHatch.setBusy(shelf, false);
      window.TurnstoneHatch.closeShelf(shelf);
      showToast(editing ? "Project updated" : "Project '" + name + "' created");
      _afterProjectMutation();
    })
    .catch(function (err) {
      window.TurnstoneHatch.setBusy(shelf, false);
      _showModalError(errEl, err.message || "Failed to save project");
    });
}

function _setProjectState(pid, state) {
  authFetch("/v1/api/projects/" + encodeURIComponent(pid), {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ state: state }),
  })
    .then(function (r) {
      if (!r.ok) throw new Error("Failed");
      showToast(state === "archived" ? "Project archived" : "Project restored");
      _afterProjectMutation();
    })
    .catch(function () {
      showToast("Failed to update project");
    });
}

function confirmDeleteProject(pid, name) {
  showConfirmModal(
    "Delete project",
    "Delete project ‘" +
      name +
      "’ and its scoped memory? Conversations stay but lose their project link. This cannot be undone.",
    "Delete",
    function () {
      authFetch("/v1/api/projects/" + encodeURIComponent(pid), {
        method: "DELETE",
      })
        .then(function (r) {
          if (!r.ok) throw new Error("Failed");
          showToast("Project deleted");
          _afterProjectMutation();
        })
        .catch(function () {
          showToast("Failed to delete project");
        });
    },
  );
}

// ===========================================================================
//  Personas — workstream capability/prompt templates (Service Hatch shelf).
//  Archive-only lifecycle (PATCH enabled=false); no DELETE — a workstream's
//  stamped provenance stays explicable forever.  Edits never touch existing
//  workstreams: they run on the snapshot stamped at creation.
// ===========================================================================

let _adminPersonas = [];
let _personaShelfWired = false;

// Builtin tool inventories per kind for the visibility checklist ride the
// GET /v1/api/admin/personas response (tool_inventory, derived server-side
// from core/tools.py) — deliberately NO hand-mirrored fallback constant
// here, which would silently drift every time a tool ships.  Until the
// first list response lands the checklist renders empty; the free-text
// "extra tools" input still accepts any name in that window.
let _personaToolInventory = null; // {interactive: [...], coordinator: [...]}

function loadAdminPersonas() {
  authFetch("/v1/api/admin/personas")
    .then(function (r) {
      if (!r.ok) throw new Error("Failed to load personas");
      return r.json();
    })
    .then(function (data) {
      _adminPersonas = data.personas || [];
      if (data.tool_inventory) _personaToolInventory = data.tool_inventory;
      _renderPersonas(_adminPersonas);
    })
    .catch(function () {
      setSafeHtml(
        document.getElementById("admin-personas-table"),
        '<div class="dashboard-empty">Failed to load personas</div>',
      );
    });
}

// One-line envelope summary for the list: prompt/tools/MCP/memory levers.
function _personaEnvelope(p) {
  const bits = [];
  bits.push(p.base_prompt ? "custom prompt" : "stock prompt");
  if (p.tool_allowlist === null || p.tool_allowlist === undefined) {
    bits.push("all tools");
  } else if (!p.tool_allowlist.length) {
    bits.push("no tools");
  } else {
    bits.push(p.tool_allowlist.length + " tools");
  }
  if (!p.mcp_enabled) bits.push("no MCP");
  if (!p.memory_enabled) bits.push("no memory");
  return bits.join(", ");
}

function _renderPersonas(personas) {
  const container = document.getElementById("admin-personas-table");
  if (!personas.length) {
    setSafeHtml(
      container,
      '<div class="dashboard-empty">No personas. Run migrations to seed the builtin set, or create one.</div>',
    );
    return;
  }
  let html = "";
  for (let i = 0; i < personas.length; i++) {
    const p = personas[i];
    const archived = !p.enabled;
    const label = p.display_name || p.name;
    html +=
      '<div class="admin-row" role="listitem" tabindex="0">' +
      '<span class="admin-col admin-col-username" title="' +
      escapeHtml(p.description || "") +
      '">' +
      escapeHtml(label) +
      (p.is_default
        ? ' <span class="scope-badge scope-default">default</span>'
        : "") +
      "</span>" +
      '<span class="admin-col admin-col-name">' +
      escapeHtml((p.applies_to_kinds || []).join(", ")) +
      "</span>" +
      '<span class="admin-col admin-col-created">' +
      escapeHtml(_personaEnvelope(p)) +
      "</span>" +
      '<span class="admin-col admin-col-created">' +
      (archived ? "Archived" : "Active") +
      "</span>" +
      '<span class="admin-col admin-col-actions">' +
      _kebabMenu(
        [
          {
            label: "edit",
            title: "Edit levers (existing workstreams keep their stamp)",
            attrs: { "data-edit-persona": p.persona_id },
          },
        ]
          .concat(
            p.is_default || archived
              ? []
              : [
                  {
                    label: "set default",
                    title: "Set as the kind default (demotes the incumbent)",
                    attrs: { "data-default-persona": p.persona_id },
                  },
                ],
          )
          .concat(
            p.is_default
              ? [] // the default is un-archivable — flip the flag elsewhere first
              : [
                  {
                    label: archived ? "unarchive" : "archive",
                    title: archived
                      ? "Reactivate persona"
                      : "Archive persona (existing workstreams unaffected)",
                    attrs: {
                      "data-archive-persona": p.persona_id,
                      "data-archive-enabled": archived ? "1" : "0",
                    },
                  },
                ],
          ),
      ) +
      "</span>" +
      "</div>";
  }
  setSafeHtml(container, html);
  _bindPersonaRowActions(container);
}

function _bindPersonaRowActions(container) {
  container.querySelectorAll("[data-edit-persona]").forEach(function (b) {
    b.addEventListener("click", function () {
      showEditPersonaModal(this.getAttribute("data-edit-persona"));
    });
  });
  container.querySelectorAll("[data-default-persona]").forEach(function (b) {
    b.addEventListener("click", function () {
      _patchPersona(
        this.getAttribute("data-default-persona"),
        { is_default: true },
        "Default persona updated",
      );
    });
  });
  container.querySelectorAll("[data-archive-persona]").forEach(function (b) {
    b.addEventListener("click", function () {
      const enable = this.getAttribute("data-archive-enabled") === "1";
      _patchPersona(
        this.getAttribute("data-archive-persona"),
        { enabled: enable },
        enable ? "Persona restored" : "Persona archived",
      );
    });
  });
}

function _personaById(pid) {
  for (let i = 0; i < _adminPersonas.length; i++)
    if (_adminPersonas[i].persona_id === pid) return _adminPersonas[i];
  return null;
}

// Refresh the shared picker cache after any persona mutation so the launcher
// dropdowns + the saved-table labels pick up the change.
function _afterPersonaMutation() {
  loadAdminPersonas();
  if (window.TurnstonePersonas) window.TurnstonePersonas.refreshPersonas();
}

function _patchPersona(pid, body, okToast) {
  authFetch("/v1/api/admin/personas/" + encodeURIComponent(pid), {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  })
    .then(function (r) {
      if (!r.ok)
        return r.json().then(function (d) {
          throw new Error(d.error || "Failed");
        });
      showToast(okToast);
      _afterPersonaMutation();
    })
    .catch(function (err) {
      showToast(err.message || "Failed to update persona");
    });
}

function _personaShelfWire() {
  if (_personaShelfWired) return;
  _personaShelfWired = true;
  document
    .getElementById("pr-submit")
    .addEventListener("click", submitPersonaShelf);
  document
    .getElementById("pr-tools-mode")
    .addEventListener("change", _personaToolsModeChanged);
  document.getElementById("pr-kinds").addEventListener("change", function () {
    // Re-render the ACTIVE kind's inventory carrying the checked names
    // over, so dual-kind tools (memory, notify, skills, tool_search)
    // survive the flip; checked names outside the new kind's inventory
    // migrate to the extra field instead of silently dropping from the
    // allowlist the operator is editing.
    const kept = [];
    document
      .querySelectorAll("#pr-tools-checklist [data-persona-tool]")
      .forEach(function (input) {
        if (input.checked) kept.push(input.value);
      });
    _renderPersonaToolChecklist(kept);
    const kind = document.getElementById("pr-kinds").value || "interactive";
    const known = (_personaToolInventory || {})[kind] || [];
    const extra = document.getElementById("pr-tools-extra");
    const extras = (extra.value || "")
      .split(",")
      .map(function (s) {
        return s.trim();
      })
      .filter(Boolean);
    kept.forEach(function (n) {
      if (known.indexOf(n) < 0 && extras.indexOf(n) < 0) extras.push(n);
    });
    extra.value = extras.join(", ");
  });
}

function _personaToolsModeChanged() {
  const mode = document.getElementById("pr-tools-mode").value;
  document.getElementById("pr-tools-picker").hidden = mode !== "list";
}

function _renderPersonaToolChecklist(checked) {
  const kind = document.getElementById("pr-kinds").value || "interactive";
  const host = document.getElementById("pr-tools-checklist");
  const inventory = _personaToolInventory || {};
  const names = inventory[kind] || [];
  host.replaceChildren();
  names.forEach(function (name) {
    const label = document.createElement("label");
    label.className = "toggle-switch";
    const input = document.createElement("input");
    input.type = "checkbox";
    input.value = name;
    input.checked = checked.indexOf(name) >= 0;
    input.setAttribute("data-persona-tool", "");
    const track = document.createElement("span");
    track.className = "toggle-track";
    track.setAttribute("aria-hidden", "true");
    const text = document.createElement("span");
    text.className = "toggle-label";
    text.textContent = name;
    label.append(input, track, text);
    host.append(label);
  });
}

function _personaToolsFromForm() {
  const mode = document.getElementById("pr-tools-mode").value;
  if (mode === "all") return null;
  if (mode === "none") return [];
  const names = [];
  document
    .querySelectorAll("#pr-tools-checklist [data-persona-tool]")
    .forEach(function (input) {
      if (input.checked) names.push(input.value);
    });
  (document.getElementById("pr-tools-extra").value || "")
    .split(",")
    .map(function (s) {
      return s.trim();
    })
    .filter(Boolean)
    .forEach(function (name) {
      if (names.indexOf(name) < 0) names.push(name);
    });
  return names;
}

function _personaFillToolsForm(allowlist) {
  const modeSel = document.getElementById("pr-tools-mode");
  const extra = document.getElementById("pr-tools-extra");
  if (allowlist === null || allowlist === undefined) {
    modeSel.value = "all";
    _renderPersonaToolChecklist([]);
    extra.value = "";
  } else if (!allowlist.length) {
    modeSel.value = "none";
    _renderPersonaToolChecklist([]);
    extra.value = "";
  } else {
    modeSel.value = "list";
    const kind = document.getElementById("pr-kinds").value || "interactive";
    const known = (_personaToolInventory || {})[kind] || [];
    _renderPersonaToolChecklist(allowlist);
    extra.value = allowlist
      .filter(function (n) {
        return known.indexOf(n) < 0;
      })
      .join(", ");
  }
  _personaToolsModeChanged();
}

function showCreatePersonaModal() {
  _personaShelfWire();
  const shelf = document.getElementById("persona-shelf");
  document.getElementById("persona-shelf-error").classList.remove("is-visible");
  document.getElementById("pr-persona-id").value = "";
  document.getElementById("pr-name").value = "";
  document.getElementById("pr-name").disabled = false;
  document.getElementById("pr-display-name").value = "";
  document.getElementById("pr-description").value = "";
  document.getElementById("pr-kinds").value = "interactive";
  document.getElementById("pr-base-prompt").value = "";
  document.getElementById("pr-base-prompt").placeholder =
    "Required — the base system prompt for this persona";
  document.getElementById("pr-mcp").checked = true;
  document.getElementById("pr-memory").checked = true;
  _personaFillToolsForm(null);
  document.getElementById("persona-shelf-title").textContent = "New persona";
  document.getElementById("pr-submit").textContent = "Create";
  window.TurnstoneHatch.openShelf(shelf);
  document.getElementById("pr-name").focus();
}

function showEditPersonaModal(pid) {
  const p = _personaById(pid);
  if (!p) return;
  _personaShelfWire();
  const shelf = document.getElementById("persona-shelf");
  document.getElementById("persona-shelf-error").classList.remove("is-visible");
  document.getElementById("pr-persona-id").value = p.persona_id;
  // The slug is immutable — shown for context, not editable.
  document.getElementById("pr-name").value = p.name;
  document.getElementById("pr-name").disabled = true;
  document.getElementById("pr-display-name").value = p.display_name || "";
  document.getElementById("pr-description").value = p.description || "";
  document.getElementById("pr-kinds").value =
    (p.applies_to_kinds || ["interactive"])[0] || "interactive";
  document.getElementById("pr-base-prompt").value = p.base_prompt || "";
  document.getElementById("pr-base-prompt").placeholder =
    "Blank keeps this built-in's shipped prompt";
  document.getElementById("pr-mcp").checked = !!p.mcp_enabled;
  document.getElementById("pr-memory").checked = !!p.memory_enabled;
  _personaFillToolsForm(
    p.tool_allowlist === undefined ? null : p.tool_allowlist,
  );
  document.getElementById("persona-shelf-title").textContent = "Edit persona";
  document.getElementById("pr-submit").textContent = "Save";
  window.TurnstoneHatch.openShelf(shelf);
  document.getElementById("pr-display-name").focus();
}

function submitPersonaShelf() {
  const shelf = document.getElementById("persona-shelf");
  const pid = document.getElementById("pr-persona-id").value;
  const name = (document.getElementById("pr-name").value || "").trim();
  const errEl = document.getElementById("persona-shelf-error");
  const editing = !!pid;
  if (!editing && !name) return _showModalError(errEl, "Name is required");

  const prompt = document.getElementById("pr-base-prompt").value;
  if (!editing && !prompt.trim())
    return _showModalError(errEl, "Base prompt is required");
  const original = editing ? _personaById(pid) || {} : {};
  const wasDefault = editing && !!original.is_default;
  const body = {
    display_name: (
      document.getElementById("pr-display-name").value || ""
    ).trim(),
    description: (document.getElementById("pr-description").value || "").trim(),
    base_prompt: prompt.trim() ? prompt : null,
    tool_allowlist: _personaToolsFromForm(),
    mcp_enabled: document.getElementById("pr-mcp").checked,
    memory_enabled: document.getElementById("pr-memory").checked,
    applies_to_kinds: [document.getElementById("pr-kinds").value],
  };
  if (!editing) body.name = name;
  if (editing) {
    const originalKinds = original.applies_to_kinds || [];
    if (wasDefault) {
      // Storage forbids changing a default persona's kinds — don't send it.
      delete body.applies_to_kinds;
    } else if (
      originalKinds.length !== 1 &&
      body.applies_to_kinds[0] === originalKinds[0]
    ) {
      // The single-value select can only show one kind; on a multi-kind
      // persona an unrelated edit must not silently strip the others.
      // Send kinds only when the operator actually changed the selection.
      delete body.applies_to_kinds;
    }
  }

  errEl.classList.remove("is-visible");
  window.TurnstoneHatch.setBusy(shelf, true);
  const url = editing
    ? "/v1/api/admin/personas/" + encodeURIComponent(pid)
    : "/v1/api/admin/personas";
  authFetch(url, {
    method: editing ? "PATCH" : "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  })
    .then(function (r) {
      if (!r.ok)
        return r.json().then(function (d) {
          throw new Error(d.error || "Failed");
        });
      return r.json();
    })
    .then(function () {
      window.TurnstoneHatch.setBusy(shelf, false);
      window.TurnstoneHatch.closeShelf(shelf);
      showToast(editing ? "Persona updated" : "Persona '" + name + "' created");
      _afterPersonaMutation();
    })
    .catch(function (err) {
      window.TurnstoneHatch.setBusy(shelf, false);
      _showModalError(errEl, err.message || "Failed to save persona");
    });
}

// --- Members (whitelist users for read+write; "public" visibility above grants
//     read to any project.read holder — the "* all users" lever). ------------
function _projectMembersWire() {
  if (_projectMembersWired) return;
  _projectMembersWired = true;
  document
    .getElementById("pm-add-btn")
    .addEventListener("click", _addProjectMember);
}

function showProjectMembersModal(pid) {
  _projectMembersWire();
  const shelf = document.getElementById("project-members-shelf");
  document
    .getElementById("project-members-shelf-error")
    .classList.remove("is-visible");
  document.getElementById("pm-project-id").value = pid;
  setSafeHtml(
    document.getElementById("pm-members-container"),
    '<div class="dashboard-empty">Loading…</div>',
  );
  _populateMemberUserSelect();
  _loadProjectMembers(pid);
  window.TurnstoneHatch.openShelf(shelf);
  // Move focus into the shelf (the create/edit shelf focuses its name input;
  // mirror that here so keyboard users land on the add-member control).
  document.getElementById("pm-add-user").focus();
}

function _populateMemberUserSelect() {
  const sel = document.getElementById("pm-add-user");
  function fill(users) {
    let html = '<option value="">Select a user…</option>';
    for (let i = 0; i < users.length; i++) {
      html +=
        '<option value="' +
        escapeHtml(users[i].user_id) +
        '">' +
        escapeHtml(users[i].username) +
        "</option>";
    }
    setSafeHtml(sel, html);
  }
  // Reuse the Users tab's already-loaded list when present; else fetch it (an
  // admin managing projects normally also holds admin.users — if not, the
  // fetch 403s and the picker stays empty, the documented v1 limitation).
  if (_adminUsers && _adminUsers.length) {
    fill(_adminUsers);
    return;
  }
  authFetch("/v1/api/admin/users")
    .then(function (r) {
      return r.ok ? r.json() : { users: [] };
    })
    .then(function (data) {
      _adminUsers = data.users || [];
      fill(_adminUsers);
    })
    .catch(function () {
      fill([]);
    });
}

function _loadProjectMembers(pid) {
  authFetch("/v1/api/projects/" + encodeURIComponent(pid) + "/members")
    .then(function (r) {
      return r.ok ? r.json() : { members: [] };
    })
    .then(function (data) {
      _renderProjectMembers(data.members || []);
    })
    .catch(function () {
      setSafeHtml(
        document.getElementById("pm-members-container"),
        '<div class="dashboard-empty">Failed to load members</div>',
      );
    });
}

function _userNameFor(uid) {
  if (_adminUsers)
    for (let i = 0; i < _adminUsers.length; i++)
      if (_adminUsers[i].user_id === uid) return _adminUsers[i].username;
  return uid;
}

function _renderProjectMembers(members) {
  const container = document.getElementById("pm-members-container");
  if (!members.length) {
    setSafeHtml(
      container,
      '<div class="dashboard-empty">No members yet. Add users for write access, or set the project Public for read access.</div>',
    );
    return;
  }
  let html = "";
  for (let i = 0; i < members.length; i++) {
    const uid = members[i];
    html +=
      '<div class="admin-row" role="listitem">' +
      '<span class="admin-col admin-col-username">' +
      escapeHtml(_userNameFor(uid)) +
      "</span>" +
      '<span class="admin-col admin-col-actions">' +
      '<button class="admin-btn-danger" type="button" data-remove-member="' +
      escapeHtml(uid) +
      '" aria-label="Remove ' +
      escapeHtml(_userNameFor(uid)) +
      '">Remove</button>' +
      "</span></div>";
  }
  setSafeHtml(container, html);
  container.querySelectorAll("[data-remove-member]").forEach(function (b) {
    b.addEventListener("click", function () {
      _removeProjectMember(this.getAttribute("data-remove-member"));
    });
  });
}

function _addProjectMember() {
  const pid = document.getElementById("pm-project-id").value;
  const uid = document.getElementById("pm-add-user").value;
  const errEl = document.getElementById("project-members-shelf-error");
  if (!uid) return _showModalError(errEl, "Select a user to add");
  errEl.classList.remove("is-visible");
  authFetch("/v1/api/projects/" + encodeURIComponent(pid) + "/members", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ user_id: uid }),
  })
    .then(function (r) {
      if (!r.ok)
        return r.json().then(function (d) {
          throw new Error(d.error || "Failed");
        });
      return r.json();
    })
    .then(function (data) {
      document.getElementById("pm-add-user").value = "";
      _renderProjectMembers(data.members || []);
    })
    .catch(function (err) {
      _showModalError(errEl, err.message || "Failed to add member");
    });
}

function _removeProjectMember(uid) {
  const pid = document.getElementById("pm-project-id").value;
  authFetch(
    "/v1/api/projects/" +
      encodeURIComponent(pid) +
      "/members/" +
      encodeURIComponent(uid),
    { method: "DELETE" },
  )
    .then(function (r) {
      if (!r.ok)
        return r.json().then(function (d) {
          throw new Error(d.error || "Failed");
        });
      return r.json();
    })
    .then(function (data) {
      _renderProjectMembers(data.members || []);
    })
    .catch(function () {
      showToast("Failed to remove member");
    });
}

// ---------------------------------------------------------------------------
// Create Token Modal
// ---------------------------------------------------------------------------

let _tokenWired = false;

function _tokenWire() {
  if (_tokenWired) return;
  _tokenWired = true;
  document
    .getElementById("ct-submit")
    .addEventListener("click", submitCreateToken);
}

function showCreateTokenModal() {
  if (!_adminTokenUserId) {
    showToast("Select a user first");
    return;
  }
  _tokenWire();
  const shelf = document.getElementById("token-shelf");
  document.getElementById("token-shelf-error").classList.remove("is-visible");
  document.getElementById("ct-name").value = "";
  document.getElementById("ct-scopes").value = "read,write,approve";
  document.getElementById("ct-expires").value = "";
  window.TurnstoneHatch.openShelf(shelf);
  document.getElementById("ct-name").focus();
}

function hideCreateTokenModal() {
  window.TurnstoneHatch.closeShelf(document.getElementById("token-shelf"));
}

function submitCreateToken() {
  const shelf = document.getElementById("token-shelf");
  const name = (document.getElementById("ct-name").value || "").trim();
  const scopes = document.getElementById("ct-scopes").value;
  const expiresDays = document.getElementById("ct-expires").value;
  const errEl = document.getElementById("token-shelf-error");

  const body = { name: name, scopes: scopes };
  if (expiresDays) body.expires_days = parseInt(expiresDays, 10);

  errEl.classList.remove("is-visible");
  window.TurnstoneHatch.setBusy(shelf, true);
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
      window.TurnstoneHatch.setBusy(shelf, false);
      hideCreateTokenModal();
      _lastCreatedToken = data.token;
      showTokenCreatedModal(data.token);
      loadAdminTokens();
    })
    .catch(function (err) {
      window.TurnstoneHatch.setBusy(shelf, false);
      _showModalError(errEl, err.message || "Failed to create token");
    });
}

// ---------------------------------------------------------------------------
// Token Created Modal (show-once)
// ---------------------------------------------------------------------------

function showTokenCreatedModal(token) {
  document.getElementById("token-created-value").textContent = token;
  window.TurnstoneHatch.openDialog(
    document.getElementById("token-created-dialog"),
    {
      onClose: function () {
        _lastCreatedToken = "";
      },
    },
  );
}

function hideTokenCreatedModal() {
  const d = document.getElementById("token-created-dialog");
  if (d.open) d.close();
}

function copyCreatedToken() {
  if (!_lastCreatedToken) return;
  if (navigator.clipboard) {
    navigator.clipboard.writeText(_lastCreatedToken).then(function () {
      showToast("Token copied to clipboard");
    });
  } else {
    // Fallback: select the text
    const el = document.getElementById("token-created-value");
    const range = document.createRange();
    range.selectNodeContents(el);
    const sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
    showToast("Select and copy the token");
  }
}

// Escape closes any open settings help popover (the settings panels and the
// shelf form help buttons share the same popover component).
document.addEventListener("keydown", function (e) {
  if (e.key !== "Escape") return;
  const openHelp = document.querySelector('.settings-help-popover[style=""]');
  if (openHelp) {
    e.preventDefault();
    // Layered dismissal: a popover inside an open shelf consumes this
    // Escape entirely — hatch.js's document listener (registered later, on
    // first openShelf) must not also close the shelf underneath it.
    e.stopImmediatePropagation();
    _closeAllSettingsHelp();
  }
});

// ---------------------------------------------------------------------------
// Confirm Modal (reusable styled replacement for confirm())
// ---------------------------------------------------------------------------

function showConfirmModal(title, message, actionLabel, callback) {
  _confirmCallbackFn = callback;
  document.getElementById("confirm-title").textContent = title;
  document.getElementById("confirm-message").textContent = message;
  const btn = document.getElementById("confirm-submit");
  btn.textContent = actionLabel;
  btn.disabled = false;
  // Cancel carries the autofocus: Enter on a freshly-opened destructive
  // confirm must not fire the action (deliberate change from the legacy
  // action-focused behavior).
  window.TurnstoneHatch.openDialog(document.getElementById("confirm-dialog"), {
    onClose: function () {
      _confirmCallbackFn = null;
    },
  });
}

function hideConfirmModal() {
  const d = document.getElementById("confirm-dialog");
  if (d.open) d.close();
}

function _confirmCallback() {
  const fn = _confirmCallbackFn;
  _confirmCallbackFn = null;
  hideConfirmModal();
  if (fn) fn();
}

// ---------------------------------------------------------------------------
// Settings — form-based editor grouped by section
// ---------------------------------------------------------------------------

let _settingsOriginal = {}; // original values for dirty detection

// Section display order
const _settingsSectionOrder = [
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
  const labels = {
    model: "Model",
    session: "Session",
    tools: "Tools",
    server: "Server",
    cluster: "Cluster",
    channels: "Channels",
    audio: "Voice",
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
  const statusEl = document.getElementById("tls-ca-status");
  const listEl = document.getElementById("tls-cert-list");
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
      const data = results[0];
      const certData = results[1];
      while (statusEl.firstChild) statusEl.removeChild(statusEl.firstChild);
      while (listEl.firstChild) listEl.removeChild(listEl.firstChild);

      if (!data.enabled) {
        const msg = document.createElement("div");
        msg.className = "dashboard-empty";
        msg.textContent =
          "TLS is not enabled. Set tls.enabled = true in Settings.";
        statusEl.appendChild(msg);
        return;
      }

      // CA status bar
      const bar = document.createElement("div");
      bar.className = "tls-ca-bar";
      const caLabel = document.createElement("span");
      caLabel.textContent = "CA: " + data.ca_cn;
      const countLabel = document.createElement("span");
      countLabel.textContent = "Certificates: " + data.cert_count;
      bar.appendChild(caLabel);
      bar.appendChild(countLabel);
      statusEl.appendChild(bar);

      const certs = certData.certs || [];
      if (certs.length === 0) {
        const empty = document.createElement("div");
        empty.className = "dashboard-empty";
        empty.textContent = "No certificates issued yet.";
        listEl.appendChild(empty);
        return;
      }

      // Cert rows
      certs.forEach(function (c) {
        const row = document.createElement("div");
        row.className = "admin-row";
        row.setAttribute("role", "listitem");

        const colDomain = document.createElement("span");
        colDomain.className = "admin-col";
        colDomain.textContent = c.domain;

        const colSans = document.createElement("span");
        colSans.className = "admin-col";
        colSans.textContent = (c.domains || [c.domain]).join(", ");

        const colIssued = document.createElement("span");
        colIssued.className = "admin-col";
        colIssued.textContent = (c.issued_at || "")
          .slice(0, 16)
          .replace("T", " ");

        const colExpires = document.createElement("span");
        colExpires.className = "admin-col";
        const expires = new Date(c.expires_at);
        const isExpired = expires < new Date();
        colExpires.textContent =
          (isExpired ? "EXPIRED " : "") +
          (c.expires_at || "").slice(0, 16).replace("T", " ");
        if (isExpired) colExpires.style.color = "var(--red)";

        const colActions = document.createElement("span");
        colActions.className = "admin-col admin-col-actions";
        const kebab = _kebabMenuEl([
          {
            label: "Renew",
            attrs: {
              "data-tls-renew": c.domain,
              "aria-label": "Renew certificate for " + c.domain,
            },
          },
          {
            label: "Delete",
            kind: "danger",
            attrs: {
              "data-tls-delete": c.domain,
              "aria-label": "Delete certificate for " + c.domain,
            },
          },
        ]);
        colActions.appendChild(kebab);

        row.appendChild(colDomain);
        row.appendChild(colSans);
        row.appendChild(colIssued);
        row.appendChild(colExpires);
        row.appendChild(colActions);
        listEl.appendChild(row);
      });
      // Bind by attribute (matching the Models call site) rather than by
      // positional index into the kebab items, so this survives any future
      // single-item degrade (where _kebabMenu returns a bare button).
      listEl.querySelectorAll("[data-tls-renew]").forEach(function (btn) {
        btn.addEventListener("click", function () {
          tlsRenewCert(this.getAttribute("data-tls-renew"));
        });
      });
      listEl.querySelectorAll("[data-tls-delete]").forEach(function (btn) {
        btn.addEventListener("click", function () {
          tlsDeleteCert(this.getAttribute("data-tls-delete"));
        });
      });
    })
    .catch(function () {
      while (statusEl.firstChild) statusEl.removeChild(statusEl.firstChild);
      while (listEl.firstChild) listEl.removeChild(listEl.firstChild);
      const errMsg = document.createElement("div");
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
  const el = document.getElementById("admin-settings-content");
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
      const valuesArr = results[0].settings || [];
      const schemaArr = results[1].schema || [];
      const modelDefs = results[2].models || [];

      // Build schema lookup
      const schemaMap = {};
      for (let i = 0; i < schemaArr.length; i++) {
        schemaMap[schemaArr[i].key] = schemaArr[i];
      }

      // Merge values + schema.  Skip role-assignment settings owned by the
      // Models → Roles sub-tab.  Derive the skip-set straight from MODEL_ROLES
      // (alias + optional effort key) so a newly-added role can't drift back
      // into this list — perception was exactly that miss, and stt/tts/reranker
      // had quietly leaked the same way.  judge.* keeps its own prefix skip
      // below: it covers more than the two judge role aliases (the output-guard
      // toggle, thresholds, ...), all of which live on the Judge tab.
      const merged = {};
      const roleKeys = {};
      for (let ri = 0; ri < MODEL_ROLES.length; ri++) {
        roleKeys[MODEL_ROLES[ri].aliasKey] = 1;
        if (MODEL_ROLES[ri].effortKey) roleKeys[MODEL_ROLES[ri].effortKey] = 1;
      }
      for (let j = 0; j < valuesArr.length; j++) {
        const v = valuesArr[j];
        if (v.key.startsWith("judge.")) continue;
        if (roleKeys[v.key]) continue;
        const s = schemaMap[v.key] || {};
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
      const enabledAliases = [""];
      for (let m = 0; m < modelDefs.length; m++) {
        if (modelDefs[m].enabled) enabledAliases.push(modelDefs[m].alias);
      }
      if (enabledAliases.length > 1) {
        for (let ak = 0; ak < ALIAS_SETTING_KEYS.length; ak++) {
          const aliasKey = ALIAS_SETTING_KEYS[ak];
          if (merged[aliasKey]) merged[aliasKey].choices = enabledAliases;
        }
      }

      _settingsOriginal = {};

      // Group by section
      const grouped = {};
      const keys = Object.keys(merged);
      for (let k = 0; k < keys.length; k++) {
        const item = merged[keys[k]];
        const sec = item.section || "other";
        if (!grouped[sec]) grouped[sec] = [];
        grouped[sec].push(item);
      }

      _renderSettings(el, grouped);
    })
    .catch(function (err) {
      // NOTE: escapeHtml sanitises err.message before insertion.
      setSafeHtml(
        el,
        '<div class="dashboard-empty">Failed to load settings: ' +
          escapeHtml(err.message || String(err)) +
          "</div>",
      );
    });
}

function _renderSettings(container, grouped) {
  let html = "";

  for (let i = 0; i < _settingsSectionOrder.length; i++) {
    const sec = _settingsSectionOrder[i];
    const items = grouped[sec];
    if (!items || items.length === 0) continue;

    html +=
      '<div class="settings-section" data-section="' +
      sec +
      '" data-collapsed>';
    html +=
      '<div class="settings-section-header" role="button" tabindex="0" aria-expanded="false" aria-controls="settings-body-' +
      sec +
      '">';
    html += "<span>" + _settingsSectionLabel(sec) + "</span>";
    html += "</div>";
    html +=
      '<div class="settings-section-body" id="settings-body-' + sec + '">';

    for (let j = 0; j < items.length; j++) {
      html += _renderSettingRow(items[j]);
    }

    html += "</div></div>";
  }

  // Render any sections not in the explicit order
  const allSections = Object.keys(grouped);
  for (let s = 0; s < allSections.length; s++) {
    if (_settingsSectionOrder.indexOf(allSections[s]) === -1) {
      const extra = grouped[allSections[s]];
      html +=
        '<div class="settings-section" data-section="' +
        allSections[s] +
        '" data-collapsed>';
      html +=
        '<div class="settings-section-header" role="button" tabindex="0" aria-expanded="false" aria-controls="settings-body-' +
        allSections[s] +
        '">';
      html += "<span>" + _settingsSectionLabel(allSections[s]) + "</span>";
      html += "</div>";
      html +=
        '<div class="settings-section-body" id="settings-body-' +
        allSections[s] +
        '">';
      for (let x = 0; x < extra.length; x++) {
        html += _renderSettingRow(extra[x]);
      }
      html += "</div></div>";
    }
  }

  setSafeHtml(container, html);

  // Bind handlers via data-* attributes — a key with an apostrophe
  // would otherwise break out of the JS string when the HTML parser
  // decodes &#39; before JS evaluation.
  const sectionHeaders = container.querySelectorAll(".settings-section-header");
  for (let sh = 0; sh < sectionHeaders.length; sh++) {
    sectionHeaders[sh].addEventListener("click", function () {
      _toggleSettingsSection(this);
    });
    sectionHeaders[sh].addEventListener("keydown", function (e) {
      _onSettingsHeaderKey(e, this);
    });
  }
  // Help-button clicks are handled by the document-delegated listener
  // below; no per-button binding is needed here (and re-binding on each
  // render would leak listeners onto reused DOM).

  // Store original values for dirty detection + wire per-key change
  // handlers off data-setting-key (no key interpolation into JS).
  const inputs = container.querySelectorAll("[data-setting-key]");
  for (let n = 0; n < inputs.length; n++) {
    const inp = inputs[n];
    const key = inp.getAttribute("data-setting-key");
    if (inp.type === "checkbox") {
      _settingsOriginal[key] = inp.checked;
    } else {
      _settingsOriginal[key] = inp.value;
    }
    const evtName =
      inp.type === "checkbox" || inp.tagName === "SELECT" ? "change" : "input";
    inp.addEventListener(evtName, function () {
      _onSettingChange(this);
    });
  }
  const saveBtns = container.querySelectorAll("[data-save-key]");
  for (let sb = 0; sb < saveBtns.length; sb++) {
    saveBtns[sb].addEventListener("click", function () {
      _saveSettingValue(this.getAttribute("data-save-key"));
    });
  }
  const resetBtns = container.querySelectorAll("[data-reset-key]");
  for (let rb = 0; rb < resetBtns.length; rb++) {
    resetBtns[rb].addEventListener("click", function () {
      _resetSetting(this.getAttribute("data-reset-key"));
    });
  }
}

function _renderSettingRow(item) {
  const shortKey =
    item.key.indexOf(".") !== -1
      ? item.key.substring(item.key.indexOf(".") + 1)
      : item.key;
  const escapedKey = escapeHtml(item.key);
  const escapedShort = escapeHtml(shortKey);
  const escapedDesc = escapeHtml(item.description);

  // Description (short TLDR) renders inline below the key.  The ? button
  // gates the long-form help paragraph + any reference_url link.
  const hasHelp = !!(item.help || item.reference_url);
  const helpId = escapedKey + "-help";

  let html = '<div class="settings-row" data-row-key="' + escapedKey + '">';

  // Label column
  html += '<div class="settings-label-col">';
  html += '<div class="settings-label">';
  html += escapeHtml(shortKey);
  if (hasHelp) {
    html +=
      ' <button type="button" class="settings-help-btn" ' +
      'data-help-target="' +
      helpId +
      '" aria-label="Help for ' +
      escapedShort +
      '" aria-expanded="false" title="More info"></button>';
  }
  html += "</div>";
  if (item.description) {
    html += '<div class="settings-desc">' + escapedDesc + "</div>";
  }
  if (hasHelp) {
    html +=
      '<div id="' +
      helpId +
      '" class="settings-help-popover" style="display:none">';
    if (item.help) {
      html +=
        '<span class="settings-help-text">' + escapeHtml(item.help) + "</span>";
    }
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
      '">';
  } else if (item.type === "bool") {
    const checked =
      item.value === true || item.value === "true" ? " checked" : "";
    html +=
      '<label class="settings-toggle"><input type="checkbox" data-setting-key="' +
      escapedKey +
      '" aria-label="' +
      escapedShort +
      '"' +
      checked +
      '><span class="settings-toggle-slider"></span></label>';
  } else if (item.choices && item.choices.length > 0) {
    html +=
      '<select data-setting-key="' +
      escapedKey +
      '" aria-label="' +
      escapedShort +
      '">';
    for (let c = 0; c < item.choices.length; c++) {
      const sel = item.choices[c] === String(item.value) ? " selected" : "";
      let label;
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
    const step = item.type === "float" ? "0.01" : "1";
    const minAttr =
      item.min_value !== null && item.min_value !== undefined
        ? ' min="' + item.min_value + '"'
        : "";
    const maxAttr =
      item.max_value !== null && item.max_value !== undefined
        ? ' max="' + item.max_value + '"'
        : "";
    // A null registry default means "unset = inherit" (e.g.
    // model.temperature): blank is a saveable state, not a validation
    // error — the save handler maps it to reset-to-default.
    const nullableAttr =
      item.default_value === null
        ? ' data-nullable="1" placeholder="(inherit model default)"'
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
      nullableAttr +
      ">";
  } else {
    // str
    html +=
      '<input type="text" data-setting-key="' +
      escapedKey +
      '" aria-label="' +
      escapedShort +
      '" value="' +
      escapeHtml(String(item.value != null ? item.value : "")) +
      '">';
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
    '">save</button>';

  // Reset link (when stored — including secrets, to clear legacy overrides)
  if (item.source === "storage") {
    html +=
      '<button class="settings-reset-btn" data-reset-key="' +
      escapedKey +
      '">reset</button>';
  }

  html += "</div>";
  html += "</div>";
  return html;
}

function _toggleSettingsHelp(e, btn) {
  // Stop both flavours of cascade: bubble to label/checkbox parents (would
  // toggle the form control behind the button) AND any default action on
  // the button click itself (irrelevant for type="button" but cheap insurance).
  e.stopPropagation();
  e.preventDefault();
  const targetId = btn.getAttribute("data-help-target");
  if (!targetId) return;
  const popover = document.getElementById(targetId);
  if (!popover) return;
  const isVisible = popover.style.display !== "none";
  _closeAllSettingsHelp(popover);
  popover.style.display = isVisible ? "none" : "";
  btn.setAttribute("aria-expanded", isVisible ? "false" : "true");
}

// Single document-delegated handler for every .settings-help-btn — both the
// settings-tab buttons emitted by _renderSettingRow and the static skill-modal
// buttons in index.html.  Bound once at module load.
document.addEventListener("click", function (e) {
  const btn = e.target.closest(".settings-help-btn[data-help-target]");
  if (btn) _toggleSettingsHelp(e, btn);
});

function _closeAllSettingsHelp(except) {
  const allOpen = document.querySelectorAll('.settings-help-popover[style=""]');
  for (let i = 0; i < allOpen.length; i++) {
    if (allOpen[i] === except) continue;
    const popover = allOpen[i];
    popover.style.display = "none";
    if (!popover.id) continue;
    const helpBtn = document.querySelector(
      '.settings-help-btn[data-help-target="' + popover.id + '"]',
    );
    if (helpBtn) helpBtn.setAttribute("aria-expanded", "false");
  }
}

function _onSettingsHeaderKey(e, el) {
  if ((e.key === "Enter" || e.key === " ") && !e.repeat) {
    e.preventDefault();
    _toggleSettingsSection(el);
  }
}

function _toggleSettingsSection(headerEl) {
  const section = headerEl.parentElement;
  if (section.hasAttribute("data-collapsed")) {
    section.removeAttribute("data-collapsed");
    headerEl.setAttribute("aria-expanded", "true");
  } else {
    section.setAttribute("data-collapsed", "");
    headerEl.setAttribute("aria-expanded", "false");
  }
}

function _onSettingChange(inp) {
  // ``inp`` is the input element the listener fired on — passed in by
  // the per-input event-listener callback so we avoid a document-wide
  // selector per keystroke.  Save button + restart badge for this key
  // live inside the same ``.settings-row`` (which carries the unique
  // ``data-row-key``).
  const row = inp.closest(".settings-row");
  if (!row) return;
  const saveBtn = row.querySelector("[data-save-key]");
  if (!saveBtn) return;
  const key = inp.getAttribute("data-setting-key");

  let current;
  if (inp.type === "checkbox") {
    current = inp.checked;
  } else {
    current = inp.value;
  }

  const orig = _settingsOriginal[key];
  let dirty;
  if (inp.type === "checkbox") {
    dirty = current !== orig;
  } else if (inp.type === "number" && current !== "" && orig !== "") {
    // Compare numerically to avoid false positives (0.1 vs 0.10)
    dirty = Number(current) !== Number(orig);
  } else {
    dirty = String(current) !== String(orig);
  }

  // Disable save for empty number fields (server will reject) — EXCEPT
  // nullable-default settings, where blank is a saveable state meaning
  // "inherit" (the save handler maps it to reset-to-default).
  const emptyNumber =
    inp.type === "number" &&
    current === "" &&
    inp.getAttribute("data-nullable") !== "1";
  if (dirty && !emptyNumber) {
    saveBtn.classList.add("visible");
  } else {
    saveBtn.classList.remove("visible");
  }

  // Show/hide restart badge alongside dirty state (but keep it if already saved)
  const restartBadge = row.querySelector("[data-restart-key]");
  if (restartBadge && !restartBadge.classList.contains("saved")) {
    restartBadge.classList.toggle("visible", dirty);
  }
}

function _saveSettingValue(key) {
  const inp = document.querySelector('[data-setting-key="' + key + '"]');
  const saveBtn = document.querySelector('[data-save-key="' + key + '"]');
  if (!inp) return;

  let value;
  if (inp.type === "checkbox") {
    value = inp.checked;
  } else if (inp.type === "number") {
    if (inp.value === "") {
      if (inp.getAttribute("data-nullable") === "1") {
        // Blank on a nullable-default setting means "inherit": clear any
        // stored override (reset), or nothing to do if already default.
        if (document.querySelector('[data-reset-key="' + key + '"]')) {
          _resetSetting(key);
        } else if (saveBtn) {
          saveBtn.classList.remove("visible");
        }
        return;
      }
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
      const row = document.querySelector('[data-row-key="' + key + '"]');
      if (row) {
        const badge = row.querySelector(".scope-badge");
        if (badge) {
          badge.className = "scope-badge scope-write";
          badge.textContent = "storage";
        }
        // Add reset button if not present
        if (!row.querySelector('[data-reset-key="' + key + '"]')) {
          const actions = row.querySelector(".settings-actions");
          if (actions) {
            const resetBtn = document.createElement("button");
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
      const restartBadge = document.querySelector(
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
        const isLight = value === "light";
        document.documentElement.dataset.theme = isLight ? "light" : "";
        localStorage.setItem(
          "turnstone_interface.theme",
          isLight ? "light" : "dark",
        );
        const themeBtn = document.getElementById("theme-toggle");
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

// Show an error message in a shelf's sh-alert element. The .is-visible class
// is the canonical toggle — see `.sh-alert` in hatch.css. Do NOT use
// `el.style.display = "block"` here; the CSS rule `.sh-alert { display: none }`
// is selector-equivalent and will hide the element again as soon as the
// inline style is cleared. Hide side is `el.classList.remove("is-visible")`.
function _showModalError(el, msg) {
  el.textContent = msg;
  el.classList.add("is-visible");
  // The alert sits at the top of the scrollable body; the submit button in
  // the sticky foot. At scroll-bottom a failed submit would otherwise paint
  // nothing in view.
  if (el.scrollIntoView) el.scrollIntoView({ block: "nearest" });
}

/* ── MCP Servers tab ─────────────────────────────────────────────────────── */

let _mcpServers = [];
let _mcpWired = false;
let _mcpImportWired = false;
let _mcpInstallWired = false;
let _mcpInstallServer = null;
let _mcpCurrentView = "servers";
let _registryResults = [];
let _registryCursor = null;
let _registryQuery = "";

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
      setSafeHtml(
        document.getElementById("admin-mcp-table"),
        '<div class="dashboard-empty">Failed to load MCP servers</div>',
      );
    });
}

function _wireMcpTokenDropButtons(el, attr, opts) {
  // Shared binder for the two per-server token-drop list actions —
  // "Bulk-revoke" (oauth_user consents) and "Flush cache" (oauth_obo minted
  // tokens). Both post to the same bulk-revoke endpoint; only the operator-
  // facing copy differs, so the confirm/fetch/toast/reload flow lives once.
  el.querySelectorAll("[" + attr + "]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      const name = this.getAttribute(attr);
      const count = this.getAttribute("data-mcp-consent-count") || "?";
      showConfirmModal(
        opts.title,
        opts.message(name, count),
        opts.confirmLabel,
        function () {
          authFetch(
            "/v1/api/admin/mcp-servers/" +
              encodeURIComponent(name) +
              "/bulk-revoke",
            { method: "POST" },
          )
            .then(function (r) {
              if (!r.ok) throw new Error();
              return r.json();
            })
            .then(function (j) {
              showToast(opts.successToast(name, j.rows_deleted || 0));
              loadAdminMcp();
            })
            .catch(function () {
              showToast(opts.failToast(name));
            });
        },
      );
    });
  });
}

function _renderMcpServers(items) {
  const el = document.getElementById("admin-mcp-table");
  if (!items.length) {
    setSafeHtml(
      el,
      '<div class="dashboard-empty">No MCP servers configured</div>',
    );
    return;
  }
  let html = "";
  for (let i = 0; i < items.length; i++) {
    const s = items[i];
    const statusEntries = s.status || {};
    const nodeIds = Object.keys(statusEntries);
    let anyConnected = false;
    let anyError = false;
    let firstError = "";
    let totalTools = 0,
      totalRes = 0,
      totalPrompts = 0;
    // Phase 9: aggregate the most-recent refresh entry across nodes
    // so the admin pill reflects "the freshest known state" rather
    // than picking an arbitrary node.
    let newestRefreshAt = null;
    let newestRefreshOutcome = null;
    for (let j = 0; j < nodeIds.length; j++) {
      const ns = statusEntries[nodeIds[j]];
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
      if (
        typeof ns.last_refresh_at === "number" &&
        (newestRefreshAt === null || ns.last_refresh_at > newestRefreshAt)
      ) {
        newestRefreshAt = ns.last_refresh_at;
        newestRefreshOutcome = ns.last_refresh_outcome || null;
      }
    }

    let dotClass = "mcp-status-dot disabled";
    let rowClass = "mcp-row-disabled";
    let statusText = "disabled";
    // Pool-backed (oauth_user/oauth_obo) servers hold NO cluster-level session —
    // they connect per-user on demand — so "connecting"/"idle" reads as broken
    // when the resting state (zero warm users) is normal.
    const isPool = s.auth_type === "oauth_user" || s.auth_type === "oauth_obo";
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
    } else if (isPool) {
      dotClass = "mcp-status-dot disabled";
      rowClass = "mcp-row-disabled";
      statusText = "per-user";
    } else if (s.enabled && s.source !== "config" && nodeIds.length === 0) {
      dotClass = "mcp-status-dot connecting";
      rowClass = "mcp-row-disabled";
      statusText = "connecting";
    } else {
      dotClass = "mcp-status-dot disabled";
      rowClass = "mcp-row-disabled";
      statusText = "idle";
    }

    // Phase 9: refresh pill shows the short-relative age (e.g. "12m") with
    // outcome-tinted color (ok vs err) and the full ISO timestamp + outcome
    // in the tooltip.  Pill is omitted (and the cell stays unchanged from
    // its pre-Phase-9 shape) when no node has yet recorded a refresh
    // outcome for this server.
    let refreshPill = "";
    if (newestRefreshAt !== null) {
      const ageSeconds = Math.max(
        0,
        Math.floor(Date.now() / 1000 - newestRefreshAt),
      );
      let ageShort;
      if (ageSeconds < 60) ageShort = ageSeconds + "s";
      else if (ageSeconds < 3600) ageShort = Math.floor(ageSeconds / 60) + "m";
      else if (ageSeconds < 86400)
        ageShort = Math.floor(ageSeconds / 3600) + "h";
      else ageShort = Math.floor(ageSeconds / 86400) + "d";
      const outcomeText = newestRefreshOutcome || "unknown";
      const pillCls =
        outcomeText === "ok" ? "mcp-refresh-pill-ok" : "mcp-refresh-pill-err";
      const pillTitle =
        "Last refresh " +
        new Date(newestRefreshAt * 1000).toISOString() +
        " (" +
        outcomeText +
        ")";
      refreshPill =
        ' <span class="mcp-refresh-pill ' +
        pillCls +
        '" title="' +
        escapeHtml(pillTitle) +
        '">' +
        escapeHtml(ageShort) +
        "</span>";
    }

    const transportCls =
      s.transport === "stdio" ? "mcp-transport-stdio" : "mcp-transport-http";
    const toolsVal = anyConnected
      ? totalTools
      : '<span class="mcp-count-dim">--</span>';
    const resVal = anyConnected
      ? totalRes
      : '<span class="mcp-count-dim">--</span>';
    const promptsVal = anyConnected
      ? totalPrompts
      : '<span class="mcp-count-dim">--</span>';

    const isConfig = s.source === "config";
    const isRegistry = !!s.registry_name;
    const nameBadge = isConfig
      ? ' <span class="scope-badge scope-config">config</span>'
      : isRegistry
        ? ' <span class="scope-badge scope-registry">registry</span>'
        : ' <span class="scope-badge scope-manual">manual</span>';
    const detailAttr = isConfig
      ? 'data-mcp-detail-name="' + escapeHtml(s.name) + '"'
      : 'data-mcp-detail="' + escapeHtml(s.server_id) + '"';
    // Phase 9: surface the connect/bulk-revoke affordances only for
    // user-OAuth servers, and bulk-revoke only once a user has consented.
    // oauth_obo has NO per-server connect (it uses the org sign-in); its
    // "flush cache" drops minted tokens so users re-mint (honest label — it is
    // not a durable revoke; that is governed by the identity provider).
    const isOauth = s.auth_type === "oauth_user";
    const isObo = s.auth_type === "oauth_obo";
    const consentCount =
      typeof s.consented_users_count === "number" ? s.consented_users_count : 0;
    const actions = _kebabMenu([
      { label: "refresh", attrs: { "data-mcp-refresh": s.name } },
      { label: "reconnect", attrs: { "data-mcp-reconnect": s.name } },
      isOauth
        ? { label: "connect", attrs: { "data-mcp-oauth-connect": s.name } }
        : null,
      isOauth && consentCount > 0
        ? {
            label: "bulk-revoke (" + consentCount + ")",
            kind: "danger",
            title:
              "Drop all " + consentCount + " user consents for this server",
            attrs: {
              "data-mcp-bulk-revoke": s.name,
              "data-mcp-consent-count": consentCount,
            },
          }
        : null,
      isObo && consentCount > 0
        ? {
            label: "flush cache (" + consentCount + ")",
            kind: "danger",
            title:
              "Drop " +
              consentCount +
              " users' minted tokens; they re-mint on next use unless access is removed at your identity provider",
            attrs: {
              "data-mcp-cache-flush": s.name,
              "data-mcp-consent-count": consentCount,
            },
          }
        : null,
      // Config-sourced servers are read-only here (edited via config.toml).
      isConfig
        ? null
        : { label: "edit", attrs: { "data-mcp-edit": s.server_id } },
      isConfig
        ? null
        : {
            label: "delete",
            kind: "danger",
            attrs: {
              "data-mcp-delete": s.server_id,
              "data-mcp-name": s.name,
            },
          },
    ]);

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
      refreshPill +
      "</span>" +
      '<span class="admin-col admin-col-mactions">' +
      actions +
      "</span></div>";
  }
  setSafeHtml(el, html);

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
  el.querySelectorAll("[data-mcp-refresh]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      const name = this.getAttribute("data-mcp-refresh");
      authFetch(
        "/v1/api/admin/mcp-servers/" + encodeURIComponent(name) + "/refresh",
        { method: "POST" },
      )
        .then(function (r) {
          if (!r.ok) throw new Error();
          return r.json();
        })
        .then(function () {
          showToast("Refreshed " + name);
          loadAdminMcp();
        })
        .catch(function () {
          showToast("Failed to refresh " + name);
        });
    });
  });
  el.querySelectorAll("[data-mcp-reconnect]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      const name = this.getAttribute("data-mcp-reconnect");
      authFetch(
        "/v1/api/admin/mcp-servers/" + encodeURIComponent(name) + "/reconnect",
        { method: "POST" },
      )
        .then(function (r) {
          if (!r.ok) throw new Error();
          return r.json();
        })
        .then(function () {
          showToast("Reconnected " + name);
          loadAdminMcp();
        })
        .catch(function () {
          showToast("Failed to reconnect " + name);
        });
    });
  });
  el.querySelectorAll("[data-mcp-oauth-connect]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      const name = this.getAttribute("data-mcp-oauth-connect");
      // Open the OAuth /start endpoint in a new window so the redirect
      // chain (AS → callback → return_url) doesn't displace the admin UI.
      const url =
        "/v1/api/mcp/oauth/start?server=" +
        encodeURIComponent(name) +
        "&return_url=" +
        encodeURIComponent(window.location.href);
      window.open(url, "_blank", "noopener");
    });
  });
  _wireMcpTokenDropButtons(el, "data-mcp-bulk-revoke", {
    title: "Bulk-revoke MCP consents",
    message: function (name, count) {
      return (
        "Drop all " +
        count +
        ' user consents for server "' +
        name +
        '"? Users will need to re-consent on next use. Upstream revoke is not attempted in bulk; tokens at the authorization server will expire naturally.'
      );
    },
    confirmLabel: "Bulk-revoke",
    successToast: function (name, n) {
      return "Bulk-revoked " + n + " row(s) for " + name;
    },
    failToast: function (name) {
      return "Failed to bulk-revoke " + name;
    },
  });
  _wireMcpTokenDropButtons(el, "data-mcp-cache-flush", {
    title: "Flush minted tokens",
    message: function (name, count) {
      return (
        "Drop " +
        count +
        ' users’ minted tokens for server "' +
        name +
        '"? This forces a fresh mint on next use (e.g. after changing the audience). It does NOT cut off access — users re-mint from their org sign-in; to revoke a user, remove their access at your identity provider or unlink their identity.'
      );
    },
    confirmLabel: "Flush cache",
    successToast: function (name, n) {
      return "Flushed " + n + " token(s) for " + name;
    },
    failToast: function (name) {
      return "Failed to flush cache for " + name;
    },
  });
  el.querySelectorAll("[data-mcp-delete]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      const sid = this.getAttribute("data-mcp-delete");
      const sname = this.getAttribute("data-mcp-name");
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
  const v = document.getElementById("mcp-transport").value;
  document.getElementById("mcp-stdio-fields").hidden = v !== "stdio";
  document.getElementById("mcp-http-fields").hidden = v !== "streamable-http";
  // Re-evaluate auth-field visibility because the headers row lives
  // inside mcp-http-fields and gets toggled there.
  toggleMcpAuthFields();
}

function _selectedMcpAuthType() {
  const radios = document.getElementsByName("mcp-auth-type");
  for (let i = 0; i < radios.length; i++) {
    if (radios[i].checked) return radios[i].value;
  }
  return "static";
}

function toggleMcpAuthFields() {
  const authType = _selectedMcpAuthType();
  const isOauthUser = authType === "oauth_user";
  const isObo = authType === "oauth_obo";
  // The OAuth fields block is shared by oauth_user and oauth_obo; obo shows
  // only the audience (+ scopes for the rfc8693 profile), hiding the
  // oauth_user-only client/registration inputs it does not use.
  const oauthDiv = document.getElementById("mcp-oauth-fields");
  if (oauthDiv) oauthDiv.hidden = !(isOauthUser || isObo);
  const userOnly = document.getElementById("mcp-oauth-user-only");
  if (userOnly) userOnly.hidden = !isOauthUser;
  const oboNote = document.getElementById("mcp-obo-note");
  if (oboNote) oboNote.hidden = !isObo;
  // For obo the audience is required (the mint engine hard-requires it) and
  // scopes apply only under the rfc8693 grant profile; retitle the hints so
  // the operator isn't misled (no impl vocabulary in the copy).
  const audHint = document.getElementById("mcp-oauth-audience-hint");
  if (audHint)
    audHint.textContent = isObo ? "required" : "auto-populated from URL";
  const scopesHint = document.getElementById("mcp-oauth-scopes-hint");
  if (scopesHint)
    scopesHint.textContent = isObo
      ? "only used by the rfc8693 sign-in profile"
      : "space-separated";
  // The "Headers" textarea (inside mcp-http-fields) is only meaningful
  // for static auth; hide it for 'none' / 'oauth_user' / 'oauth_obo' so
  // operators don't accidentally configure stale credentials.
  const headersInput = document.getElementById("mcp-headers");
  if (headersInput) {
    const headersLabel = document.querySelector('label[for="mcp-headers"]');
    const show = authType === "static";
    headersInput.hidden = !show;
    if (headersLabel) headersLabel.hidden = !show;
  }
}

function _wireMcpAudienceAutofill() {
  // Idempotent — only attach the listener once per page lifetime.
  const urlInput = document.getElementById("mcp-url");
  if (!urlInput || urlInput.dataset.audAutofill === "1") return;
  urlInput.dataset.audAutofill = "1";
  urlInput.addEventListener("blur", function () {
    // oauth_user only: there the audience IS the server URL (resource
    // indicator). For sign-in passthrough the audience is the identity-
    // provider-side application identifier — prefilling the MCP URL there
    // passes every validation layer and then fails every token mint, so
    // the autofill stays off.
    if (_selectedMcpAuthType() === "oauth_obo") return;
    const aud = document.getElementById("mcp-oauth-audience");
    if (aud && !aud.value.trim()) aud.value = urlInput.value.trim();
  });
}

function _onMcpAuthTypeChange() {
  // Audience and Scopes are auth-type-specific: for oauth_user the audience is
  // an RFC 8707 resource indicator (~ the MCP URL) and scopes are AS-consent
  // scopes; for sign-in passthrough the audience is the identity-provider-side
  // application identifier and scopes (rfc8693 only) are the token-exchange
  // scope. Carrying one type's value into the other passes validation and then
  // fails every mint/consent, so clear both when the auth type changes — the
  // operator re-enters the correct values for the new mode (the backend
  // likewise refuses to carry these columns across a flip). A same-type edit
  // never fires this (the radio didn't change), so pre-filled values are kept.
  document.getElementById("mcp-oauth-audience").value = "";
  document.getElementById("mcp-oauth-scopes").value = "";
  toggleMcpAuthFields();
}

function _mcpWire() {
  if (_mcpWired) return;
  _mcpWired = true;
  document
    .getElementById("mcp-transport")
    .addEventListener("change", toggleMcpTransport);
  const authRadios = document.getElementsByName("mcp-auth-type");
  for (let i = 0; i < authRadios.length; i++) {
    authRadios[i].addEventListener("change", _onMcpAuthTypeChange);
  }
  document
    .getElementById("mcp-create-submit")
    .addEventListener("click", submitCreateMcp);
  _wireMcpAudienceAutofill();
}

function _mcpOpen(title, tag, kind, submitLabel) {
  const shelf = document.getElementById("mcp-shelf");
  document.getElementById("mcp-shelf-title").textContent = title;
  document.getElementById("mcp-shelf-tag").textContent = tag;
  shelf.setAttribute("data-kind", kind);
  document.getElementById("mcp-create-submit").textContent = submitLabel;
  window.TurnstoneHatch.openShelf(shelf);
  document.getElementById("mcp-name").focus();
}

function _mcpResetForm() {
  document.getElementById("mcp-edit-id").value = "";
  document.getElementById("mcp-name").value = "";
  document.getElementById("mcp-transport").value = "stdio";
  document.getElementById("mcp-command").value = "";
  document.getElementById("mcp-args").value = "";
  document.getElementById("mcp-env").value = "";
  document.getElementById("mcp-url").value = "";
  document.getElementById("mcp-headers").value = "";
  document.getElementById("mcp-auto-approve").checked = false;
  document.getElementById("mcp-enabled").checked = true;
  // Reset auth radios + OAuth subfields to the 'static' default.
  document.getElementById("mcp-auth-static").checked = true;
  document.getElementById("mcp-auth-none").checked = false;
  document.getElementById("mcp-auth-oauth").checked = false;
  document.getElementById("mcp-auth-obo").checked = false;
  document.getElementById("mcp-oauth-as-url").value = "";
  document.getElementById("mcp-oauth-registration").value = "preregistered";
  document.getElementById("mcp-oauth-client-id").value = "";
  document.getElementById("mcp-oauth-client-secret").value = "";
  document.getElementById("mcp-oauth-scopes").value = "";
  document.getElementById("mcp-oauth-audience").value = "";
  document.getElementById("mcp-create-error").classList.remove("is-visible");
  toggleMcpTransport();
  toggleMcpAuthFields();
}

function showCreateMcpModal() {
  _mcpWire();
  _mcpResetForm();
  _mcpOpen("New MCP server", "MCP-NEW", "create", "Create");
}

function showEditMcpModal(serverId) {
  _mcpWire();
  // Fetch with reveal=true to get actual secret values for editing
  authFetch("/v1/api/admin/mcp-servers/" + serverId + "?reveal=true")
    .then(function (r) {
      if (!r.ok) throw new Error("Failed to load server");
      return r.json();
    })
    .then(function (s) {
      _mcpResetForm();
      document.getElementById("mcp-edit-id").value = serverId;
      document.getElementById("mcp-name").value = s.name;
      document.getElementById("mcp-transport").value = s.transport;
      document.getElementById("mcp-command").value = s.command || "";
      try {
        const argsList = JSON.parse(s.args || "[]");
        document.getElementById("mcp-args").value = argsList.join("\n");
      } catch (e) {
        document.getElementById("mcp-args").value = "";
      }
      try {
        const envObj = JSON.parse(s.env || "{}");
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
        const hdrObj = JSON.parse(s.headers || "{}");
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
      const authType = s.auth_type || "static";
      document.getElementById("mcp-auth-none").checked = authType === "none";
      document.getElementById("mcp-auth-static").checked =
        authType === "static";
      document.getElementById("mcp-auth-oauth").checked =
        authType === "oauth_user";
      document.getElementById("mcp-auth-obo").checked =
        authType === "oauth_obo";
      document.getElementById("mcp-oauth-as-url").value =
        s.oauth_authorization_server_url || "";
      document.getElementById("mcp-oauth-registration").value =
        s.oauth_registration_mode || "preregistered";
      document.getElementById("mcp-oauth-client-id").value =
        s.oauth_client_id || "";
      // Secret field always blank — write-only, never read back.
      document.getElementById("mcp-oauth-client-secret").value = "";
      document.getElementById("mcp-oauth-scopes").value = s.oauth_scopes || "";
      document.getElementById("mcp-oauth-audience").value =
        s.oauth_audience || "";
      toggleMcpTransport();
      toggleMcpAuthFields();
      _mcpOpen("Edit MCP server — " + s.name, "MCP-EDIT", "edit", "Save");
    })
    .catch(function () {
      showToast("Failed to load server details");
    });
}

function hideCreateMcpModal() {
  window.TurnstoneHatch.closeShelf(document.getElementById("mcp-shelf"));
}

function _parseMcpForm() {
  const name = document.getElementById("mcp-name").value.trim();
  const transport = document.getElementById("mcp-transport").value;
  if (!name) return { error: "Name is required" };
  if (!/^[a-zA-Z0-9._-]+$/.test(name))
    return { error: "Name must match [a-zA-Z0-9._-]+" };
  if (name.indexOf("__") >= 0) return { error: "Name must not contain '__'" };

  const authType = _selectedMcpAuthType();
  const payload = {
    name: name,
    transport: transport,
    auto_approve: document.getElementById("mcp-auto-approve").checked,
    enabled: document.getElementById("mcp-enabled").checked,
    auth_type: authType,
  };

  if (transport === "stdio") {
    payload.command = document.getElementById("mcp-command").value.trim();
    const argsText = document.getElementById("mcp-args").value.trim();
    payload.args = argsText
      ? argsText
          .split("\n")
          .map(function (l) {
            return l.trim();
          })
          .filter(Boolean)
      : [];
    const envText = document.getElementById("mcp-env").value.trim();
    const envObj = {};
    if (envText) {
      envText.split("\n").forEach(function (line) {
        const eq = line.indexOf("=");
        if (eq > 0)
          envObj[line.substring(0, eq).trim()] = line.substring(eq + 1).trim();
      });
    }
    payload.env = envObj;
  } else {
    payload.url = document.getElementById("mcp-url").value.trim();
    if (authType === "static") {
      const hdrText = document.getElementById("mcp-headers").value.trim();
      const hdrObj = {};
      if (hdrText) {
        hdrText.split("\n").forEach(function (line) {
          const colon = line.indexOf(":");
          if (colon > 0)
            hdrObj[line.substring(0, colon).trim()] = line
              .substring(colon + 1)
              .trim();
        });
      }
      payload.headers = hdrObj;
    } else {
      // 'none' / 'oauth_user' / 'oauth_obo' — clear static headers state.
      payload.headers = {};
    }
  }

  if (authType === "oauth_user") {
    payload.oauth_authorization_server_url = document
      .getElementById("mcp-oauth-as-url")
      .value.trim();
    payload.oauth_registration_mode = document.getElementById(
      "mcp-oauth-registration",
    ).value;
    payload.oauth_client_id = document
      .getElementById("mcp-oauth-client-id")
      .value.trim();
    payload.oauth_scopes = document
      .getElementById("mcp-oauth-scopes")
      .value.trim();
    payload.oauth_audience = document
      .getElementById("mcp-oauth-audience")
      .value.trim();
    const secret = document.getElementById("mcp-oauth-client-secret").value;
    // Submit only when the operator typed a value; redacted in audit log.
    if (secret) payload.oauth_client_secret = secret;
  } else if (authType === "oauth_obo") {
    // Sign-in passthrough uses only the audience (+ optional rfc8693 scopes);
    // the client/registration/secret columns are oauth_user-only and cleared
    // server-side. Audience is required — catch it here for an inline error
    // rather than a round-trip 400.
    const audience = document.getElementById("mcp-oauth-audience").value.trim();
    if (!audience)
      return { error: "Audience is required for sign-in passthrough servers" };
    payload.oauth_audience = audience;
    // Always send the visible Scopes value — the backend distinguishes a
    // same-type no-op re-send (dropped) from a genuine change / flip on its
    // side, so the form doesn't need omit-when-unchanged logic (which used to
    // collide with the backend's flip handling and silently drop scopes).
    payload.oauth_scopes = document
      .getElementById("mcp-oauth-scopes")
      .value.trim();
  }

  return payload;
}

function submitCreateMcp() {
  const shelf = document.getElementById("mcp-shelf");
  const errEl = document.getElementById("mcp-create-error");
  const form = _parseMcpForm();
  if (form.error) {
    errEl.textContent = form.error;
    errEl.classList.add("is-visible");
    return;
  }
  const editId = document.getElementById("mcp-edit-id").value;
  const method = editId ? "PUT" : "POST";
  const url = editId
    ? "/v1/api/admin/mcp-servers/" + editId
    : "/v1/api/admin/mcp-servers";

  errEl.classList.remove("is-visible");
  window.TurnstoneHatch.setBusy(shelf, true);
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
      window.TurnstoneHatch.setBusy(shelf, false);
      hideCreateMcpModal();
      showToast(editId ? "Server updated" : "Server created");
      _flagMcpSyncPending();
      loadAdminMcp();
    })
    .catch(function (e) {
      window.TurnstoneHatch.setBusy(shelf, false);
      errEl.textContent = e.message;
      errEl.classList.add("is-visible");
    });
}

function _flagMcpSyncPending() {
  const btn = document.getElementById("mcp-sync-btn");
  if (btn) btn.classList.add("mcp-sync-pending");
}

function _clearMcpSyncPending() {
  const btn = document.getElementById("mcp-sync-btn");
  if (btn) btn.classList.remove("mcp-sync-pending");
}

function reloadMcpNodes() {
  authFetch("/v1/api/admin/mcp-servers/reload", { method: "POST" })
    .then(function (r) {
      if (!r.ok) throw new Error();
      return r.json();
    })
    .then(function (data) {
      const results = data.results || {};
      const nodeIds = Object.keys(results);
      let totalAdded = 0,
        totalRemoved = 0;
      for (let i = 0; i < nodeIds.length; i++) {
        const nr = results[nodeIds[i]];
        totalAdded += (nr.added || []).length;
        totalRemoved += (nr.removed || []).length;
      }
      let msg = "Reload sent to " + nodeIds.length + " node(s)";
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
  for (let i = 0; i < _mcpServers.length; i++) {
    if (_mcpServers[i].name === name) {
      return _openMcpDetail(_mcpServers[i]);
    }
  }
}

function showMcpDetailModal(serverId) {
  for (let i = 0; i < _mcpServers.length; i++) {
    if (_mcpServers[i].server_id === serverId) {
      return _openMcpDetail(_mcpServers[i]);
    }
  }
}

function _openMcpDetail(s) {
  if (!s) return;

  let html = '<div class="modal-columns">';
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
      const a = JSON.parse(s.args || "[]");
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
      const meta =
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
  const statusEntries = s.status || {};
  const nodeIds = Object.keys(statusEntries);
  html += '<div class="mcp-detail-section"><h3>Node Status</h3>';
  if (nodeIds.length === 0) {
    html +=
      '<p style="font-size:12px;color:var(--fg-dim)">Not connected on any node</p>';
  } else {
    html += '<ul class="mcp-detail-list">';
    for (let j = 0; j < nodeIds.length; j++) {
      const ns = statusEntries[nodeIds[j]];
      const dot = ns.connected
        ? '<span class="mcp-status-dot connected"></span>'
        : '<span class="mcp-status-dot error"></span>';
      let nodeInfo =
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
      // The dot alone would make the healthy state color-only — pair it
      // with a text token, mirroring the error-text sibling above.
      if (ns.connected) {
        nodeInfo +=
          '<br><span style="color:var(--fg-dim);font-size:11px">connected</span>';
      }
      html += "<li>" + dot + nodeInfo + "</li>";
    }
    html += "</ul>";
  }
  html += "</div></div></div>";

  document.getElementById("mcp-detail-title").textContent = s.name;
  setSafeHtml(document.getElementById("mcp-detail-content"), html);
  window.TurnstoneHatch.openShelf(document.getElementById("mcp-detail-shelf"));
}

function hideMcpDetailModal() {
  window.TurnstoneHatch.closeShelf(document.getElementById("mcp-detail-shelf"));
}

function _mcpImportWire() {
  if (_mcpImportWired) return;
  _mcpImportWired = true;
  document
    .getElementById("mcp-import-submit")
    .addEventListener("click", submitImportMcp);
}

function showImportMcpModal() {
  _mcpImportWire();
  const shelf = document.getElementById("mcp-import-shelf");
  document.getElementById("mcp-import-json").value = "";
  document.getElementById("mcp-import-error").classList.remove("is-visible");
  window.TurnstoneHatch.openShelf(shelf);
  document.getElementById("mcp-import-json").focus();
}

function hideImportMcpModal() {
  window.TurnstoneHatch.closeShelf(document.getElementById("mcp-import-shelf"));
}

function submitImportMcp() {
  const raw = document.getElementById("mcp-import-json").value.trim();
  if (!raw) {
    const e = document.getElementById("mcp-import-error");
    e.textContent = "Paste a JSON config";
    e.classList.add("is-visible");
    return;
  }
  let parsed;
  try {
    parsed = JSON.parse(raw);
  } catch (ex) {
    const e2 = document.getElementById("mcp-import-error");
    e2.textContent = "Invalid JSON: " + ex.message;
    e2.classList.add("is-visible");
    return;
  }
  if (!parsed.mcpServers || typeof parsed.mcpServers !== "object") {
    const e3 = document.getElementById("mcp-import-error");
    e3.textContent = 'No "mcpServers" key found in JSON';
    e3.classList.add("is-visible");
    return;
  }
  const shelf = document.getElementById("mcp-import-shelf");
  const errEl = document.getElementById("mcp-import-error");
  errEl.classList.remove("is-visible");
  window.TurnstoneHatch.setBusy(shelf, true);
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
      window.TurnstoneHatch.setBusy(shelf, false);
      hideImportMcpModal();
      let msg = "Imported " + (data.imported || []).length;
      if ((data.skipped || []).length)
        msg += ", skipped " + data.skipped.length;
      if ((data.errors || []).length)
        msg += ", " + data.errors.length + " error(s)";
      showToast(msg);
      if ((data.imported || []).length) _flagMcpSyncPending();
      loadAdminMcp();
    })
    .catch(function (e) {
      window.TurnstoneHatch.setBusy(shelf, false);
      errEl.textContent = e.message;
      errEl.classList.add("is-visible");
    });
}

/* ── MCP Registry ────────────────────────────────────────────────────────── */

function switchMcpView(view) {
  _mcpCurrentView = view;
  const btns = document.querySelectorAll("#admin-mcp .mcp-view-btn");
  for (let i = 0; i < btns.length; i++) {
    const isActive = btns[i].getAttribute("data-mcp-view") === view;
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
    const q = document.getElementById("mcp-registry-q");
    if (q) q.focus();
    if (!_registryResults.length) searchMcpRegistry();
  }
}

function searchMcpRegistry(append) {
  const q = document.getElementById("mcp-registry-q").value.trim();
  if (!append) {
    _registryResults = [];
    _registryCursor = null;
    _registryQuery = q;
    const filterEl = document.getElementById("mcp-registry-filter");
    if (filterEl) filterEl.value = "";
  }
  let url = "/v1/api/admin/mcp-registry/search?limit=20";
  if (_registryQuery) url += "&search=" + encodeURIComponent(_registryQuery);
  if (append && _registryCursor)
    url += "&cursor=" + encodeURIComponent(_registryCursor);

  const resultsEl = document.getElementById("mcp-registry-results");
  if (!append) {
    setSafeHtml(resultsEl, '<div class="dashboard-empty">Searching…</div>');
  }
  const searchBtn = document.getElementById("mcp-registry-search-btn");
  const moreBtn = document.getElementById("mcp-registry-more");
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
        setSafeHtml(
          resultsEl,
          '<div class="dashboard-empty">' + escapeHtml(e.message) + "</div>",
        );
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
  const el = document.getElementById("mcp-registry-results");
  if (!_registryResults.length) {
    setSafeHtml(el, '<div class="dashboard-empty">No servers found</div>');
    document.getElementById("mcp-registry-pagination").style.display = "none";
    return;
  }

  // Client-side type filter
  const filterEl = document.getElementById("mcp-registry-filter");
  const typeFilter = filterEl ? filterEl.value : "";

  let html = "";
  let visibleCount = 0;
  for (let i = 0; i < _registryResults.length; i++) {
    const srv = _registryResults[i];
    const hasRemote = srv.remotes && srv.remotes.length > 0;
    const pkgTypes = (srv.packages || []).map(function (p) {
      return p.registry_type;
    });

    // Apply type filter
    if (typeFilter === "remote" && !hasRemote) continue;
    if (typeFilter === "npm" && pkgTypes.indexOf("npm") === -1) continue;
    if (typeFilter === "pypi" && pkgTypes.indexOf("pypi") === -1) continue;
    visibleCount++;

    // Action button
    const srvLabel = escapeHtml(srv.title || srv.name);
    let actionHtml = "";
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
    let sourceBadges = "";
    if (hasRemote) {
      sourceBadges +=
        '<span class="scope-badge mcp-transport-http">remote</span>';
    }
    for (let p = 0; p < (srv.packages || []).length; p++) {
      sourceBadges +=
        '<span class="scope-badge mcp-transport-stdio">' +
        escapeHtml(srv.packages[p].registry_type) +
        "</span>";
    }

    // Repo link for trust signal
    let repoLink = "";
    const repoUrl = (srv.repository || {}).url || "";
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
    setSafeHtml(
      el,
      '<div class="dashboard-empty">No servers match the selected filter</div>',
    );
  } else {
    setSafeHtml(el, html);
  }

  // Pagination
  const pagEl = document.getElementById("mcp-registry-pagination");
  const moreBtn = document.getElementById("mcp-registry-more");
  const countEl = document.getElementById("mcp-registry-count");
  const isFiltered = typeFilter && visibleCount < _registryResults.length;
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
      const idx = parseInt(this.getAttribute("data-reg-install"), 10);
      _initiateRegistryInstall(_registryResults[idx]);
    });
  });
}

/* ── Registry Install Flow ───────────────────────────────────────────────── */

function _initiateRegistryInstall(srv) {
  _mcpInstallServer = srv;
  const hasRemote = srv.remotes && srv.remotes.length > 0;
  const hasPackage = srv.packages && srv.packages.length > 0;

  // Check if remote needs configuration
  let remoteNeedsConfig = false;
  if (hasRemote) {
    const remote = srv.remotes[0];
    for (let hi = 0; hi < (remote.headers || []).length; hi++) {
      if (remote.headers[hi].is_required) {
        remoteNeedsConfig = true;
        break;
      }
    }
    const varKeys = Object.keys(remote.variables || {});
    for (let vi = 0; vi < varKeys.length; vi++) {
      if (remote.variables[varKeys[vi]].is_required) {
        remoteNeedsConfig = true;
        break;
      }
    }
  }

  // One-click: remote with no config needed and no package alternative
  if (hasRemote && !remoteNeedsConfig && !hasPackage) {
    // Disable the clicked Install button for loading feedback
    const cardBtns = document.querySelectorAll("[data-reg-install]");
    for (let bi = 0; bi < cardBtns.length; bi++) {
      const idx = parseInt(cardBtns[bi].getAttribute("data-reg-install"), 10);
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

function _mcpInstallWire() {
  if (_mcpInstallWired) return;
  _mcpInstallWired = true;
  document
    .getElementById("mcp-install-submit")
    .addEventListener("click", submitInstallMcp);
}

function _showInstallMcpModal(srv, hasRemote, hasPackage) {
  _mcpInstallWire();
  document.getElementById("mcp-install-error").classList.remove("is-visible");

  // Summary
  setSafeHtml(
    document.getElementById("mcp-install-summary"),
    '<div class="mcp-install-summary-name">' +
      escapeHtml(srv.title || srv.name) +
      "</div>" +
      (srv.description
        ? '<div class="mcp-install-summary-desc">' +
          escapeHtml(srv.description) +
          "</div>"
        : ""),
  );

  // Source selector (only if both remote AND package)
  const srcEl = document.getElementById("mcp-install-source-select");
  if (hasRemote && hasPackage) {
    let srcHtml = '<div class="mcp-install-source-group">';
    srcHtml +=
      '<label class="mcp-install-source-label">' +
      '<input type="radio" name="mcp-install-src" value="remote" checked> ' +
      'Remote <span class="mcp-install-source-type">streamable-http</span>' +
      "</label>";
    for (let pi = 0; pi < srv.packages.length; pi++) {
      srcHtml +=
        '<label class="mcp-install-source-label">' +
        '<input type="radio" name="mcp-install-src" value="package-' +
        pi +
        '"> ' +
        'Package <span class="mcp-install-source-type">' +
        escapeHtml(srv.packages[pi].registry_type) +
        " / " +
        escapeHtml(srv.packages[pi].identifier) +
        "</span></label>";
    }
    srcHtml += "</div>";
    setSafeHtml(srcEl, srcHtml);
    const srcRadios = srcEl.querySelectorAll('input[name="mcp-install-src"]');
    for (let sr = 0; sr < srcRadios.length; sr++) {
      srcRadios[sr].addEventListener("change", _updateInstallFields);
    }
  } else {
    srcEl.replaceChildren();
  }

  _updateInstallFields();
  window.TurnstoneHatch.openDialog(
    document.getElementById("mcp-install-dialog"),
    {
      onClose: function () {
        _mcpInstallServer = null;
      },
    },
  );
}

function _updateInstallFields() {
  const srv = _mcpInstallServer;
  if (!srv) return;
  const fieldsEl = document.getElementById("mcp-install-fields");
  const srcRadio = document.querySelector(
    'input[name="mcp-install-src"]:checked',
  );
  const srcVal = srcRadio ? srcRadio.value : "";

  let source = "remote";
  let pkgIndex = 0;
  if (srcVal.startsWith("package-")) {
    source = "package";
    pkgIndex = parseInt(srcVal.replace("package-", ""), 10);
  } else if (!srv.remotes || !srv.remotes.length) {
    source = "package";
  }

  let html = "";
  if (source === "package") {
    const pkg = srv.packages && srv.packages[pkgIndex];
    const pkgId = pkg ? pkg.identifier : "";
    const pkgType = pkg ? pkg.registry_type : "";
    const runner =
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
    const remote = srv.remotes[0];
    // URL variables
    const varKeys = Object.keys(remote.variables || {});
    for (let vi = 0; vi < varKeys.length; vi++) {
      const v = remote.variables[varKeys[vi]];
      html +=
        '<label for="mcp-inst-var-' +
        vi +
        '">' +
        escapeHtml(varKeys[vi]) +
        (v.is_required
          ? ' <span style="color:var(--red)" aria-hidden="true">*</span>'
          : ' <span class="label-hint">optional</span>') +
        "</label>";
      if (v.choices && v.choices.length) {
        html +=
          '<select id="mcp-inst-var-' +
          vi +
          '" data-var-name="' +
          escapeHtml(varKeys[vi]) +
          '"' +
          (v.is_required ? " required" : "") +
          ">";
        if (!v.is_required) html += '<option value="">--</option>';
        for (let ci = 0; ci < v.choices.length; ci++) {
          const sel = v.choices[ci] === (v["default"] || "") ? " selected" : "";
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
          '"' +
          (v.is_required ? " required" : "") +
          ">";
      }
    }
    // Required headers
    for (let hi = 0; hi < (remote.headers || []).length; hi++) {
      const h = remote.headers[hi];
      html +=
        '<label for="mcp-inst-hdr-' +
        hi +
        '">' +
        escapeHtml(h.name) +
        (h.is_required
          ? ' <span style="color:var(--red)" aria-hidden="true">*</span>'
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
        '"' +
        (h.is_required ? " required" : "") +
        ">";
    }
  } else if (source === "package" && srv.packages && srv.packages[pkgIndex]) {
    const pkg = srv.packages[pkgIndex];
    const evs = pkg.environment_variables || [];
    for (let ei = 0; ei < evs.length; ei++) {
      const ev = evs[ei];
      html +=
        '<label for="mcp-inst-env-' +
        ei +
        '">' +
        escapeHtml(ev.name) +
        (ev.is_required
          ? ' <span style="color:var(--red)" aria-hidden="true">*</span>'
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
        '"' +
        (ev.is_required ? " required" : "") +
        ">";
    }
  }

  if (!html) {
    html =
      '<p style="font-size:12px;color:var(--fg-dim);margin:8px 0">' +
      "No configuration required — click Install to proceed.</p>";
  }
  setSafeHtml(fieldsEl, html);
  fieldsEl.setAttribute("data-source", source);
  fieldsEl.setAttribute("data-pkg-index", String(pkgIndex));
}

function hideInstallMcpModal() {
  const d = document.getElementById("mcp-install-dialog");
  if (d.open) d.close();
}

function submitInstallMcp() {
  const srv = _mcpInstallServer;
  if (!srv) return;
  const fieldsEl = document.getElementById("mcp-install-fields");
  const source = fieldsEl.getAttribute("data-source") || "remote";
  const pkgIndex = parseInt(fieldsEl.getAttribute("data-pkg-index") || "0", 10);
  const index = source === "remote" ? 0 : pkgIndex;

  const variables = {};
  fieldsEl.querySelectorAll("[data-var-name]").forEach(function (el) {
    variables[el.getAttribute("data-var-name")] = el.value;
  });
  const headers = {};
  fieldsEl.querySelectorAll("[data-hdr-name]").forEach(function (el) {
    if (el.value) headers[el.getAttribute("data-hdr-name")] = el.value;
  });
  const env = {};
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
  // Two entry points share this: the one-click card path (no dialog) and the
  // install dialog's submit. The dialog's busy lock only applies when it's open.
  const dialog = document.getElementById("mcp-install-dialog");
  if (dialog.open) window.TurnstoneHatch.setBusy(dialog, true);

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
      window.TurnstoneHatch.setBusy(dialog, false);
      if (dialog.open) hideInstallMcpModal();
      const serverName = data.name || registryName;
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
      window.TurnstoneHatch.setBusy(dialog, false);
      const errEl = document.getElementById("mcp-install-error");
      if (errEl && dialog.open) {
        errEl.textContent = e.message;
        errEl.classList.add("is-visible");
      } else {
        showToast("Install failed: " + e.message);
        // Re-render to reset card button states
        _renderRegistryResults();
      }
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
        const status = data.status || {};
        const nodeIds = Object.keys(status);
        let anyConnected = false;
        const errors = [];
        for (let i = 0; i < nodeIds.length; i++) {
          const ns = status[nodeIds[i]];
          if (ns.connected) anyConnected = true;
          if (ns.error) errors.push(ns.error);
        }
        if (anyConnected) {
          let tools = 0;
          for (let j = 0; j < nodeIds.length; j++) {
            if (status[nodeIds[j]].connected) {
              tools = status[nodeIds[j]].tools || 0;
              break;
            }
          }
          let msg = serverName + " connected";
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

let _modelDefs = [];
let _modelDefaultAlias = "";
// Reranker calibration fields extracted out of the capabilities textarea in the
// edit modal (like server_compat), held here so they survive an unrelated edit
// and are re-merged on save. Reset per modal open.
let _rerankCalFields = {};

// Capability tile matrix — sparse-override semantics. The tiles display
// merge(dataclass defaults, known-model table baseline, explicit overrides);
// only EXPLICIT keys persist (saved keys + tiles the user toggled), so a
// known model keeps tracking future table updates instead of being pinned.
const _MODEL_CAP_KEYS = [
  "supports_tools",
  "supports_streaming",
  "supports_vision",
  "supports_pdf",
  "supports_web_search",
  "supports_temperature",
  "supports_effort",
  "supports_verbosity",
  "supports_pro_mode",
  "supports_transcription",
  "supports_speech_synthesis",
  "supports_audio_input",
  "supports_rerank",
];
const _MODEL_CAP_DEFAULTS = {
  supports_tools: true,
  supports_streaming: true,
  supports_vision: false,
  supports_pdf: false,
  supports_web_search: false,
  supports_temperature: true,
  supports_effort: false,
  supports_verbosity: false,
  supports_pro_mode: false,
  supports_transcription: false,
  supports_speech_synthesis: false,
  supports_audio_input: false,
  supports_rerank: false,
};
let _modelCapsBaseline = {}; // known-model table values (display + delta base)
let _modelCapsExplicit = {}; // keys that persist: saved-in-JSON + user-toggled

function _modelTileEl(key) {
  return document.querySelector('#model-capgrid input[data-cap="' + key + '"]');
}
function _modelGetTile(key) {
  const el = _modelTileEl(key);
  return el ? el.checked : _MODEL_CAP_DEFAULTS[key];
}
function _modelRenderTiles() {
  _MODEL_CAP_KEYS.forEach(function (k) {
    const el = _modelTileEl(k);
    if (!el) return;
    if (k in _modelCapsExplicit) el.checked = !!_modelCapsExplicit[k];
    else if (k in _modelCapsBaseline) el.checked = !!_modelCapsBaseline[k];
    else el.checked = _MODEL_CAP_DEFAULTS[k];
  });
  _updateModelResponseControls();
}

const _MODEL_RESPONSE_CONTROLS = [
  {
    key: "verbosity",
    supportKey: "supports_verbosity",
    elementId: "model-output-verbosity",
    fieldId: "model-output-verbosity-field",
    values: ["low", "medium", "high"],
  },
  {
    key: "reasoning_mode",
    supportKey: "supports_pro_mode",
    elementId: "model-reasoning-mode",
    fieldId: "model-reasoning-mode-field",
    values: ["standard", "pro"],
  },
];
let _modelResponseInitialIdentity = "";
let _modelResponseCurrentIdentity = "";
let _modelResponseCaptured = {};
let _modelResponseDirty = {};

function _modelIdentity() {
  const provider = document.getElementById("model-provider").value;
  const model = document.getElementById("model-name").value.trim();
  const surface =
    provider === "openai-compatible"
      ? document.getElementById("model-api-surface").value
      : "";
  return provider + "\n" + model + "\n" + surface;
}

function _modelUsesResponsesSurface() {
  const provider = document.getElementById("model-provider").value;
  if (provider === "openai") return true;
  return (
    provider === "openai-compatible" &&
    document.getElementById("model-api-surface").value === "responses"
  );
}

function _modelResponseValueValid(spec, value) {
  return typeof value === "string" && spec.values.indexOf(value) !== -1;
}

function _updateModelResponseControls() {
  const group = document.getElementById("model-response-controls");
  if (!group) return;
  const responseSurface = _modelUsesResponsesSurface();
  const sameIdentity =
    _modelResponseInitialIdentity &&
    _modelIdentity() === _modelResponseInitialIdentity;
  let anyVisible = false;
  _MODEL_RESPONSE_CONTROLS.forEach(function (spec) {
    const field = document.getElementById(spec.fieldId);
    const select = document.getElementById(spec.elementId);
    if (!field || !select) return;
    // A value _captureModelResponseControls lifted out of the row's JSON
    // stays visible (and re-saveable) while the identity still matches the
    // row being edited, deliberately NOT consulting the capability
    // baseline: the baseline arrives async (or never, on the compat lane),
    // and yielding to it would hide the pinned value and silently drop it
    // on save — the same lift-then-restore contract as server_compat,
    // rerank calibration, and thinking_param. Wire safety is server-side:
    // emission gates on the merged supports_* flag, so a pinned value on
    // an unsupported model is inert; "Provider default" explicitly clears
    // it. An explicit tile override (either polarity) supersedes the
    // fallback — unchecking the tile is the operator's way to retire it.
    const capturedFallback =
      sameIdentity &&
      !(spec.supportKey in _modelCapsExplicit) &&
      _modelResponseValueValid(spec, select.value);
    const visible =
      responseSurface && (_modelGetTile(spec.supportKey) || capturedFallback);
    field.hidden = !visible;
    anyVisible = anyVisible || visible;
  });
  group.hidden = !anyVisible;
}

function _resetModelResponseControls() {
  _MODEL_RESPONSE_CONTROLS.forEach(function (spec) {
    const select = document.getElementById(spec.elementId);
    if (select) select.value = "";
  });
  _modelResponseInitialIdentity = "";
  _modelResponseCurrentIdentity = _modelIdentity();
  _modelResponseCaptured = {};
  _modelResponseDirty = {};
  _updateModelResponseControls();
}

function _captureModelResponseControls(capsObj) {
  if (!_modelUsesResponsesSurface()) return;
  _MODEL_RESPONSE_CONTROLS.forEach(function (spec) {
    const select = document.getElementById(spec.elementId);
    if (!select) return;
    const explicitlyUnsupported =
      spec.supportKey in _modelCapsExplicit &&
      !_modelCapsExplicit[spec.supportKey];
    const value = capsObj[spec.key];
    if (!explicitlyUnsupported && _modelResponseValueValid(spec, value)) {
      select.value = value;
      _modelResponseCaptured[spec.key] = value;
      delete capsObj[spec.key];
    }
  });
}

function _mergeModelResponseControls(caps) {
  if (!_modelUsesResponsesSurface()) return;
  const sameIdentity =
    _modelResponseInitialIdentity &&
    _modelIdentity() === _modelResponseInitialIdentity;
  _MODEL_RESPONSE_CONTROLS.forEach(function (spec) {
    // Dirty (select touched this session) lets the select override a
    // stale JSON key, but only for the identity that made it dirty —
    // after a model/provider/surface change the flag describes the OLD
    // row, and honoring it would delete a key hand-typed into the
    // Advanced JSON for the new one.
    if (_modelResponseDirty[spec.key] && sameIdentity) delete caps[spec.key];
    else if (spec.key in caps) return; // Advanced JSON wins.
    const select = document.getElementById(spec.elementId);
    if (!select || !_modelResponseValueValid(spec, select.value)) return;
    // Same capturedFallback contract as _updateModelResponseControls
    // (rationale there): a lifted same-identity value must re-save, or an
    // unrelated edit silently drops it from the row.
    const capturedFallback =
      sameIdentity && !(spec.supportKey in _modelCapsExplicit);
    if (_modelGetTile(spec.supportKey) || capturedFallback) {
      caps[spec.key] = select.value;
    }
  });
}

function _rememberModelResponseControl(spec) {
  _modelResponseDirty[spec.key] = true;
  if (_modelIdentity() !== _modelResponseInitialIdentity) return;
  const select = document.getElementById(spec.elementId);
  if (select && _modelResponseValueValid(spec, select.value)) {
    _modelResponseCaptured[spec.key] = select.value;
  } else {
    delete _modelResponseCaptured[spec.key];
  }
}

// Roles surfaced in the Models → Roles sub-tab.  Each entry maps a
// settings-registry key onto a UX label.  ``effortKey`` is optional —
// roles whose registry entry has a paired ``*.reasoning_effort``
// setting render a second selector inline.  Adding a new role (e.g.
// ``perception.audio.model``) is purely additive: drop a row here once
// the SettingDef lands in turnstone/core/settings_registry.py.
// ``fallbackKind`` controls how the empty/blank option in the alias
// dropdown is labelled.  Coordinator and Judge fall back to a single
// well-defined alias (model.default_alias / coordinator alias) so we
// surface that concrete model in the placeholder.  The Task agent
// cascades through ``[model].task_model → [model].agent_model →
// session model`` per turnstone/core/settings_registry.py — there's
// no single "default" to advertise, so the blank reads "(inherit)"
// to match the vocabulary of the reasoning-effort dropdowns.
const MODEL_ROLES = [
  {
    label: "Coordinator",
    description:
      "Console-hosted coordinator sessions that drive child workstreams.",
    aliasKey: "coordinator.model_alias",
    effortKey: "coordinator.reasoning_effort",
    fallbackKind: "default",
  },
  {
    label: "Judge",
    description:
      "Intent-validation judge that scores tool calls before approval.",
    aliasKey: "judge.model",
    fallbackKind: "default",
  },
  {
    label: "Output guard judge",
    description:
      "Output-guard judge that semantically evaluates tool results for camouflaged prompt injection (active when judge.output_guard_llm is enabled).",
    aliasKey: "judge.output_guard_model",
    fallbackKind: "default",
  },
  {
    label: "Task agent",
    description:
      "task_agent sub-agent — runs autonomous subtasks dispatched by the parent.",
    aliasKey: "model.task_alias",
    effortKey: "model.task_effort",
    fallbackKind: "inherit",
  },
  {
    label: "Channel adapter",
    description:
      "Workstreams created by channel adapters (Discord, Slack) when no model is specified at creation time.",
    aliasKey: "channels.default_model_alias",
    fallbackKind: "default",
  },
  {
    label: "Speech-to-text",
    description:
      "Transcribes microphone audio in the workstream composer (voice input), and is the preferred transcript for audio attachments. Point at a transcription model (Whisper-style) or an audio-capable omni model — the omni path transcribes via chat. Empty disables the mic affordance; audio attachments then fall back to the Perception model if one is configured.",
    aliasKey: "audio.stt_model_alias",
    fallbackKind: "disabled",
    mediaCapability: "supports_transcription",
    mediaRole: "stt",
  },
  {
    label: "Text-to-speech",
    description:
      "Synthesizes assistant replies for playback (voice output). Empty disables the play affordance.",
    aliasKey: "audio.tts_model_alias",
    fallbackKind: "disabled",
    mediaCapability: "supports_speech_synthesis",
    mediaRole: "tts",
  },
  {
    label: "Perception",
    description:
      "Bottom-tier fallback for attachments the primary model can't ingest natively (image / PDF / audio): the perception model perceives the attachment and its description is passed to the primary as text. Point at a vision-capable or omni model and enable the matching capabilities on it — supports_vision for image/PDF, supports_audio_input for audio. Empty disables the fallback; native handling and the speech-to-text role still take precedence.",
    aliasKey: "perception.model_alias",
    fallbackKind: "disabled",
    disabledLabel: "(disabled — no fallback)",
  },
  {
    label: "Reranker",
    description:
      "Reranks web_search results. Point at a model whose base_url is a Cohere/Jina-compatible /rerank endpoint and whose capabilities include supports_rerank. Empty disables reranking. Enabling a reranker sends web_search results AND BM25 retrieval candidates (tool/skill descriptions and memory content) to this endpoint; self-hosted endpoints keep it on your infrastructure.",
    aliasKey: "tools.reranker_alias",
    fallbackKind: "disabled",
    disabledLabel: "(disabled — reranking off)",
    mediaCapability: "supports_rerank",
    mediaRole: "rerank",
  },
];

// Whether a model definition is eligible for an audio role.  Mirrors
// turnstone/core/audio.py model_supports_role: an explicit capability flag
// wins; otherwise infer from well-known OpenAI audio model names so a stock
// gpt-4o-mini-transcribe / -tts / whisper alias shows up without hand-ticking.
// Known-model-name hints, mirrored VERBATIM from _AUDIO_MODEL_HINTS in
// turnstone/core/audio.py (the canonical source). A substring match marks
// eligibility. Keep these two lists in sync — the Python side is pinned by
// tests/test_audio.py so any change there is deliberate.
const AUDIO_MODEL_HINTS = {
  stt: ["transcribe", "whisper", "-asr"],
  tts: ["tts-", "-tts"],
};

// Providers whose client speaks the OpenAI-SDK audio surface. Mirror
// _AUDIO_SDK_PROVIDERS / _provider_carries_audio in turnstone/core/audio.py:
// anthropic(-compatible) has no audio content block, so it can't serve the
// voice roles regardless of capability flags.
function _providerCarriesAudio(provider) {
  return (
    provider === "openai" ||
    provider === "openai-compatible" ||
    provider === "google" ||
    provider === "xai"
  );
}

function _audioModelEligible(md, capFlag, mediaRole) {
  let caps = md && md.capabilities;
  if (typeof caps === "string") {
    try {
      caps = JSON.parse(caps || "{}");
    } catch (e) {
      caps = {};
    }
  }
  if (!caps || typeof caps !== "object") caps = {};
  // Voice roles (stt/tts) ride the OpenAI-SDK audio surface; an Anthropic
  // (-compatible) provider can't serve them even with a capability flag set.
  // A blank/unset provider defaults to "openai" — matching the backend's
  // _provider_carries_audio (ModelConfig.provider defaults to "openai") so a
  // provider-less model isn't wrongly excluded. Reranker is NOT an audio role
  // (it hits a /rerank endpoint), so it is not provider-gated here.
  if (
    (mediaRole === "stt" || mediaRole === "tts") &&
    !_providerCarriesAudio((md && md.provider) || "openai")
  ) {
    return false;
  }
  // An omni model (chat audio input) can serve STT via the chat transcription
  // path — mirror model_supports_role in turnstone/core/audio.py.
  if (mediaRole === "stt" && caps.supports_audio_input) return true;
  if (Object.prototype.hasOwnProperty.call(caps, capFlag))
    return !!caps[capFlag];
  const name = ((md && md.model) || "").toLowerCase();
  const hints = AUDIO_MODEL_HINTS[mediaRole] || [];
  return hints.some(function (h) {
    return name.indexOf(h) !== -1;
  });
}

// Roles sub-tab reads/writes via ``/v1/api/admin/settings`` which
// requires ``admin.settings`` — different from the ``admin.models``
// permission gating the Models tab itself.  When the user has Models
// access but not Settings, hide the sub-tab button + force the
// Definitions panel visible so they don't see a perpetual 403 loader.
function _modelRolesAccessible() {
  const perms = sessionStorage.getItem("turnstone_permissions") || "";
  return perms.split(",").indexOf("admin.settings") !== -1;
}

function _applyModelRolesPermission() {
  const btn = document.getElementById("models-tab-roles");
  if (!btn) return;
  if (_modelRolesAccessible()) {
    btn.style.display = "";
    return;
  }
  btn.style.display = "none";
  // If Roles was the active sub-tab, snap back to Definitions so the
  // user isn't staring at a hidden panel.
  if (btn.classList.contains("active")) {
    switchModelsSection("models-list");
  }
}

function loadAdminModels() {
  _applyModelRolesPermission();
  authFetch("/v1/api/admin/model-definitions")
    .then(function (r) {
      if (!r.ok) throw new Error("Failed");
      return r.json();
    })
    .then(function (data) {
      _modelDefs = data.models || [];
      _modelDefaultAlias = data.default_alias || "";
      _renderModels(_modelDefs);
      // Roles sub-tab piggybacks on the model list; skip it when the
      // user has no settings permission since the underlying API will
      // 403 anyway.
      if (_modelRolesAccessible()) {
        loadAdminModelRoles();
      }
    })
    .catch(function () {
      const el = document.getElementById("admin-models-table");
      el.textContent = "";
      const d = document.createElement("div");
      d.className = "dashboard-empty";
      d.textContent = "Failed to load models";
      el.appendChild(d);
    });
}

function switchModelsSection(section) {
  const sections = document.querySelectorAll("#admin-models .models-section");
  for (let i = 0; i < sections.length; i++) sections[i].style.display = "none";
  const switcher = document.querySelector(
    "#admin-models .admin-subtab-switcher",
  );
  const btns = switcher ? switcher.querySelectorAll(".admin-subtab-btn") : [];
  for (let k = 0; k < btns.length; k++) {
    const isActive = btns[k].getAttribute("data-section") === section;
    btns[k].classList.toggle("active", isActive);
    btns[k].setAttribute("aria-selected", isActive ? "true" : "false");
    btns[k].setAttribute("tabindex", isActive ? "0" : "-1");
  }
  const target = document.getElementById(section + "-section");
  if (target) target.style.display = "";
}

// Arrow key navigation for Models sub-tabs (matches the Judge tab).
(function () {
  const switcher = document.querySelector(
    "#admin-models .admin-subtab-switcher",
  );
  if (!switcher) return;
  switcher.addEventListener("keydown", function (e) {
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
    const btns = switcher.querySelectorAll(".admin-subtab-btn");
    const secs = [];
    for (let i = 0; i < btns.length; i++)
      secs.push(btns[i].getAttribute("data-section"));
    const current = switcher.querySelector(".admin-subtab-btn.active");
    let idx = secs.indexOf(current ? current.getAttribute("data-section") : "");
    if (e.key === "ArrowRight") idx = (idx + 1) % secs.length;
    else idx = (idx - 1 + secs.length) % secs.length;
    e.preventDefault();
    switchModelsSection(secs[idx]);
    btns[idx].focus();
  });
})();

function _modelRolesError(container, msg) {
  while (container.firstChild) container.removeChild(container.firstChild);
  const d = document.createElement("div");
  d.className = "dashboard-empty";
  d.textContent = msg;
  container.appendChild(d);
}

function loadAdminModelRoles() {
  const c = document.getElementById("admin-models-roles-container");
  if (!c) return;
  // Reads ``_modelDefs`` / ``_modelDefaultAlias`` populated by the most
  // recent ``loadAdminModels`` — both entry points into the Models tab
  // (initial open + ``models_changed`` SSE refresh) go through
  // ``loadAdminModels`` first, so the cached snapshot is fresh.  Role
  // saves don't change model definitions, so the snapshot stays
  // accurate after ``_saveModelRole`` chains back here.
  Promise.all([
    authFetch("/v1/api/admin/settings").then(function (r) {
      if (!r.ok) throw new Error("settings " + r.status);
      return r.json();
    }),
    authFetch("/v1/api/admin/settings/schema").then(function (r) {
      if (!r.ok) throw new Error("schema " + r.status);
      return r.json();
    }),
  ])
    .then(function (results) {
      const values = {};
      const arr = results[0].settings || [];
      for (let i = 0; i < arr.length; i++) values[arr[i].key] = arr[i];
      const schema = {};
      const sa = results[1].schema || [];
      for (let j = 0; j < sa.length; j++) schema[sa[j].key] = sa[j];
      _renderModelRoles(c, values, schema);
    })
    .catch(function () {
      _modelRolesError(c, "Failed to load roles");
    });
}

function _renderModelRoles(container, values, schema) {
  const enabledAliases = [];
  for (let i = 0; i < _modelDefs.length; i++) {
    if (_modelDefs[i].enabled) enabledAliases.push(_modelDefs[i]);
  }
  container.textContent = "";
  for (let r = 0; r < MODEL_ROLES.length; r++) {
    const role = MODEL_ROLES[r];
    const aliasInfo = values[role.aliasKey];
    if (!aliasInfo) continue; // setting not registered (e.g. older server)

    const row = document.createElement("div");
    row.className = "model-role-row";

    // The dropdown's selected-option text is the single source of
    // truth for default vs override — when nothing is set it shows
    // "(default — <alias>)", otherwise it shows the chosen alias.  No
    // separate badge: redundant with the select, and prone to
    // confusing color contrasts on freshly-rendered rows.
    const head = document.createElement("div");
    head.className = "model-role-head";
    const nameEl = document.createElement("span");
    nameEl.className = "model-role-label";
    nameEl.textContent = role.label;
    head.appendChild(nameEl);
    row.appendChild(head);

    if (role.description) {
      const desc = document.createElement("div");
      desc.className = "model-role-desc";
      desc.textContent = role.description;
      row.appendChild(desc);
    }

    const controls = document.createElement("div");
    controls.className = "model-role-controls";

    // Alias dropdown
    const aliasWrap = document.createElement("label");
    aliasWrap.className = "model-role-control";
    const aliasLabel = document.createElement("span");
    aliasLabel.className = "model-role-control-label";
    aliasLabel.textContent = "Model";
    aliasWrap.appendChild(aliasLabel);
    const aliasSel = document.createElement("select");
    aliasSel.setAttribute("data-role-key", role.aliasKey);
    aliasSel.setAttribute(
      "aria-label",
      role.label + " model (empty = default)",
    );
    // Format the blank/inherit option in the same "alias (model)" shape
    // as the other rows so the dropdown reads consistently — without
    // this the empty row was bare "(default — flatspark)" while every
    // other row carried a "(/models/...)" suffix.  Plan/Task agent
    // fall back through a multi-step chain (config.toml → agent_model
    // → session) that has no single concrete "default", so they get
    // a plain "(inherit)" instead of the misleading
    // "(default — <coordinator-alias>)".
    const blank = document.createElement("option");
    blank.value = "";
    if (role.fallbackKind === "disabled") {
      blank.textContent = role.disabledLabel || "(disabled — voice off)";
    } else if (role.fallbackKind === "inherit") {
      blank.textContent = "(inherit)";
    } else {
      let defaultDef = null;
      if (_modelDefaultAlias) {
        for (let dm = 0; dm < enabledAliases.length; dm++) {
          if (enabledAliases[dm].alias === _modelDefaultAlias) {
            defaultDef = enabledAliases[dm];
            break;
          }
        }
      }
      if (defaultDef) {
        const defLabel =
          defaultDef.alias === defaultDef.model
            ? defaultDef.alias
            : defaultDef.alias + " (" + defaultDef.model + ")";
        blank.textContent = "(default — " + defLabel + ")";
      } else if (_modelDefaultAlias) {
        blank.textContent = "(default — " + _modelDefaultAlias + ")";
      } else {
        blank.textContent = "(default)";
      }
    }
    aliasSel.appendChild(blank);
    // Audio roles only list aliases capable of the role (capability flag or
    // known-model inference); other roles list every enabled alias.
    let roleAliases = enabledAliases;
    if (role.mediaCapability) {
      roleAliases = enabledAliases.filter(function (md) {
        return _audioModelEligible(md, role.mediaCapability, role.mediaRole);
      });
    }
    const currentAlias = aliasInfo.value || "";
    let matched = false;
    for (let m = 0; m < roleAliases.length; m++) {
      const md = roleAliases[m];
      const opt = document.createElement("option");
      opt.value = md.alias;
      opt.textContent =
        md.alias === md.model ? md.alias : md.alias + " (" + md.model + ")";
      if (currentAlias && currentAlias === md.alias) {
        opt.selected = true;
        matched = true;
      }
      aliasSel.appendChild(opt);
    }
    if (currentAlias && !matched) {
      const manual = document.createElement("option");
      manual.value = currentAlias;
      manual.textContent = currentAlias + " (manual)";
      manual.selected = true;
      aliasSel.appendChild(manual);
    }
    aliasSel.addEventListener("change", function () {
      _saveModelRole(this.getAttribute("data-role-key"), this.value);
    });
    aliasWrap.appendChild(aliasSel);
    controls.appendChild(aliasWrap);

    // Optional reasoning effort dropdown
    if (role.effortKey && values[role.effortKey] && schema[role.effortKey]) {
      const effortWrap = document.createElement("label");
      effortWrap.className = "model-role-control";
      const effortLabel = document.createElement("span");
      effortLabel.className = "model-role-control-label";
      effortLabel.textContent = "Reasoning effort";
      effortWrap.appendChild(effortLabel);
      const effortSel = document.createElement("select");
      effortSel.setAttribute("data-role-key", role.effortKey);
      effortSel.setAttribute("aria-label", role.label + " reasoning effort");
      const choices = schema[role.effortKey].choices || [];
      const currentEffort = values[role.effortKey].value;
      for (let c2 = 0; c2 < choices.length; c2++) {
        const eo = document.createElement("option");
        eo.value = choices[c2];
        eo.textContent = choices[c2] === "" ? "(inherit)" : choices[c2];
        if (currentEffort === choices[c2]) eo.selected = true;
        effortSel.appendChild(eo);
      }
      effortSel.addEventListener("change", function () {
        _saveModelRole(this.getAttribute("data-role-key"), this.value);
      });
      effortWrap.appendChild(effortSel);
      controls.appendChild(effortWrap);
    }

    row.appendChild(controls);
    container.appendChild(row);
  }

  if (!container.children.length) {
    _modelRolesError(container, "No model roles configured");
  }
}

function _saveModelRole(key, value) {
  authFetch("/v1/api/admin/settings/" + encodeURIComponent(key), {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ value: value }),
  })
    .then(function (r) {
      if (!r.ok)
        return r.json().then(function (d) {
          throw new Error(d.error || "Failed");
        });
      return r.json();
    })
    .then(function () {
      showToast("Saved");
      loadAdminModelRoles();
    })
    .catch(function (e) {
      showToast("Error: " + (e && e.message ? e.message : "save failed"));
    });
}

function _renderModels(items) {
  const el = document.getElementById("admin-models-table");
  // Clear previous content
  el.textContent = "";
  if (!items.length) {
    const empty = document.createElement("div");
    empty.className = "dashboard-empty";
    empty.textContent = "No model definitions configured";
    el.appendChild(empty);
    return;
  }
  for (let i = 0; i < items.length; i++) {
    const m = items[i];
    const isConfig = m.source === "config";

    // Status
    const dotClass = m.enabled
      ? "model-status-dot enabled"
      : "model-status-dot disabled";
    const rowClass = m.enabled ? "model-row-enabled" : "model-row-disabled";
    const statusText = m.enabled ? "enabled" : "disabled";

    // Context window formatting (0 = auto-detect)
    const ctxText = m.context_window
      ? m.context_window >= 1000
        ? Math.round(m.context_window / 1000) + "k"
        : String(m.context_window)
      : "auto";

    // Provider badge class
    const providerCls =
      m.provider === "anthropic"
        ? "model-provider-anthropic"
        : m.provider === "google"
          ? "model-provider-google"
          : m.provider === "openai-compatible" ||
              m.provider === "anthropic-compatible"
            ? "model-provider-compat"
            : "model-provider-openai";

    // Build row via DOM
    const row = document.createElement("div");
    row.className = "admin-row models-grid " + rowClass;
    row.setAttribute("role", "listitem");

    // Alias + source badge + default badge
    const isDefault = m.alias === _modelDefaultAlias;
    const colAlias = document.createElement("span");
    colAlias.className = "admin-col";
    colAlias.textContent = m.alias;
    const badge = document.createElement("span");
    badge.className = isConfig
      ? "scope-badge scope-config"
      : "scope-badge scope-db";
    badge.textContent = isConfig ? "config" : "db";
    colAlias.appendChild(document.createTextNode(" "));
    colAlias.appendChild(badge);
    if (isDefault) {
      const defBadge = document.createElement("span");
      defBadge.className = "scope-badge scope-default";
      defBadge.textContent = "default";
      colAlias.appendChild(document.createTextNode(" "));
      colAlias.appendChild(defBadge);
    }
    // Per-model sampling override indicators
    const overrides = [];
    if (m.temperature != null) overrides.push("temp=" + m.temperature);
    if (m.max_tokens != null) overrides.push("max_tok=" + m.max_tokens);
    if (m.reasoning_effort != null)
      overrides.push("effort=" + m.reasoning_effort);
    let displayCaps = m.capabilities;
    if (typeof displayCaps === "string") {
      try {
        displayCaps = JSON.parse(displayCaps || "{}");
      } catch (e) {
        displayCaps = {};
      }
    }
    if (!_isPlainObject(displayCaps)) displayCaps = {};
    if (
      displayCaps.supports_verbosity !== false &&
      ["low", "medium", "high"].indexOf(displayCaps.verbosity) !== -1
    )
      overrides.push("verbosity=" + displayCaps.verbosity);
    if (
      displayCaps.supports_pro_mode !== false &&
      ["standard", "pro"].indexOf(displayCaps.reasoning_mode) !== -1
    )
      overrides.push("mode=" + displayCaps.reasoning_mode);
    // Reasoning persistence flags surface only when non-default
    // (persist=False is the operator opt-out; replay=True is the
    // operator opt-in). Default values are silent.
    if (m.surface_persisted_reasoning === false) overrides.push("surface=off");
    if (m.replay_reasoning_to_model === true) overrides.push("replay=on");
    if (overrides.length) {
      const ovrSpan = document.createElement("span");
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
    const colModel = document.createElement("span");
    colModel.className = "admin-col";
    const code = document.createElement("code");
    code.textContent = m.model;
    colModel.appendChild(code);
    row.appendChild(colModel);

    // Provider
    const colProvider = document.createElement("span");
    colProvider.className = "admin-col";
    const provBadge = document.createElement("span");
    provBadge.className = "model-provider-badge " + providerCls;
    provBadge.textContent = m.provider;
    colProvider.appendChild(provBadge);
    row.appendChild(colProvider);

    // Context window
    const colCtx = document.createElement("span");
    colCtx.className = "admin-col";
    colCtx.textContent = ctxText;
    row.appendChild(colCtx);

    // Status
    const colStatus = document.createElement("span");
    colStatus.className = "admin-col";
    const dot = document.createElement("span");
    dot.className = dotClass;
    dot.setAttribute("aria-hidden", "true");
    colStatus.appendChild(dot);
    colStatus.appendChild(document.createTextNode(statusText));
    row.appendChild(colStatus);

    // Actions
    const colActions = document.createElement("span");
    colActions.className = "admin-col admin-col-actions";
    const modelKebab = _kebabMenuEl([
      // Config-sourced models are read-only here; only the live default may
      // be changed.  Hide "set default" on the row that already is default.
      !isDefault && m.enabled
        ? {
            label: "set default",
            title: "Set " + m.alias + " as default model",
            attrs: {
              "data-model-set-default": m.alias,
              "aria-label": "Set " + m.alias + " as default model",
            },
          }
        : null,
      isConfig
        ? null
        : {
            label: "edit",
            title: "Edit " + m.alias,
            attrs: { "data-model-edit": m.definition_id },
          },
      isConfig
        ? null
        : {
            label: "delete",
            kind: "danger",
            title: "Delete " + m.alias,
            attrs: {
              "data-model-delete": m.definition_id,
              "data-model-alias": m.alias,
            },
          },
    ]);
    if (modelKebab) colActions.appendChild(modelKebab);
    row.appendChild(colActions);

    el.appendChild(row);
  }

  // Bind event handlers
  el.querySelectorAll("[data-model-set-default]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      const alias = this.getAttribute("data-model-set-default");
      const self = this;
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
      const did = this.getAttribute("data-model-delete");
      const dalias = this.getAttribute("data-model-alias");
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
  const mode = document.getElementById("model-thinking-mode").value;
  const row = document.getElementById("model-thinking-param-row");
  row.hidden = !mode;
  // Set default when first enabling
  const paramEl = document.getElementById("model-thinking-param");
  if (mode && !paramEl.value) paramEl.value = "enable_thinking";
}

function showCreateModelModal() {
  document.getElementById("model-edit-id").value = "";
  document.getElementById("model-create-title").textContent = "New model";
  document.getElementById("model-shelf-tag").textContent = "MDL-NEW";
  document.getElementById("model-shelf").setAttribute("data-kind", "create");
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
  document.getElementById("model-api-surface").value = "";
  document.getElementById("model-thinking-mode").value = "";
  document.getElementById("model-thinking-param").value = "";
  document.getElementById("model-thinking-param-row").hidden = true;
  document.getElementById("model-effort-param").value = "";
  _annotateEffortSelect(
    document.getElementById("model-reasoning-effort"),
    null,
  );
  document.getElementById("model-extra-body").value = "";
  document.getElementById("model-capabilities").value = "";
  // Clear validation error styling from prior submit attempts
  ["model-extra-body", "model-capabilities"].forEach(function (id) {
    const el = document.getElementById(id);
    el.removeAttribute("aria-invalid");
    el.style.borderColor = "";
  });
  document.getElementById("model-enabled").checked = true;
  document.getElementById("model-surface-persisted-reasoning").checked = true;
  document.getElementById("model-replay-reasoning").checked = false;
  document.getElementById("model-detect-result").hidden = true;
  document.getElementById("model-detect-btn").disabled = false;
  document.getElementById("model-detect-btn").textContent = "Detect";
  // Reranker calibration: reset extracted fields and hide the chip + the
  // Re-calibrate button (edit mode re-shows them after loading the def).
  _rerankCalFields = {};
  const _calChip = document.getElementById("model-calibration-chip");
  if (_calChip) _calChip.style.display = "none";
  const _recalBtn = document.getElementById("model-recalibrate-btn");
  if (_recalBtn) _recalBtn.hidden = true;
  _modelCapsSeq++; // invalidate lookups from a prior shelf lifecycle
  _modelCapsBaseline = {};
  _modelCapsExplicit = {};
  _resetModelResponseControls();
  _modelRenderTiles();
  document.getElementById("model-autofill").hidden = true;
  _refreshModelSuggestions();
  _applyProviderDefaults();
  window.TurnstoneHatch.openShelf(document.getElementById("model-shelf"));
  document.getElementById("model-alias").focus();
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
      document.getElementById("model-create-title").textContent =
        "Edit model — " + (m.alias || definitionId);
      document.getElementById("model-shelf-tag").textContent = "MDL-EDIT";
      document.getElementById("model-shelf").setAttribute("data-kind", "edit");
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
      let capsObj = {};
      try {
        capsObj = JSON.parse(m.capabilities || "{}");
      } catch (e) {
        /* keep empty */
      }
      // Defend against null/array/primitive values in the DB
      if (!_isPlainObject(capsObj)) capsObj = {};
      const sc = _isPlainObject(capsObj.server_compat)
        ? capsObj.server_compat
        : {};
      // Only extract thinking_mode into the dropdown when the UI can
      // represent it ("", "manual" = effort-knob controlled, "adaptive" =
      // always on).  Unrepresentable/garbage values keep thinking_mode in
      // the raw capabilities JSON so they aren't silently lost on save.
      // Both compat lanes round-trip the dropdown: it drives the
      // effort-knob → chat_template_kwargs mapping
      // (merge_reasoning_template_kwargs).
      const tmVal = capsObj.thinking_mode || "";
      const tmCaptured =
        tmVal === "" || tmVal === "manual" || tmVal === "adaptive";
      if (tmCaptured) {
        document.getElementById("model-thinking-mode").value = tmVal;
        document.getElementById("model-thinking-param").value =
          capsObj.thinking_param || "";
      } else {
        document.getElementById("model-thinking-mode").value = "";
        document.getElementById("model-thinking-param").value = "";
      }
      _toggleThinkingParam();
      // effort_param is a plain string — always representable, so it lifts
      // into its field unconditionally (stripped from the raw JSON below).
      document.getElementById("model-effort-param").value =
        capsObj.effort_param || "";
      // Server compat: server_type, api_surface, and extra_body workarounds
      document.getElementById("model-server-type").value = sc.server_type || "";
      document.getElementById("model-api-surface").value = sc.api_surface || "";
      const eb = sc.extra_body || {};
      const ebText = JSON.stringify(eb, null, 2);
      document.getElementById("model-extra-body").value =
        ebText === "{}" ? "" : ebText;
      // Extract reranker calibration fields out of the displayed capabilities
      // (like server_compat) so an unrelated edit doesn't drop them — held in
      // _rerankCalFields and re-merged on save. Capture supports_rerank for the
      // chip/button before it stays in the visible JSON.
      const isReranker = !!capsObj.supports_rerank;
      _rerankCalFields = {};
      ["rerank_threshold", "rerank_scale", "rerank_separated"].forEach(
        function (k) {
          if (k in capsObj) {
            _rerankCalFields[k] = capsObj[k];
            delete capsObj[k];
          }
        },
      );
      // Lift the capability keys out of the JSON into the tiles — they are
      // the row's explicit overrides and the textarea holds the remainder.
      _modelCapsExplicit = {};
      _MODEL_CAP_KEYS.forEach(function (k) {
        if (k in capsObj) {
          _modelCapsExplicit[k] = !!capsObj[k];
          delete capsObj[k];
        }
      });
      _modelResponseInitialIdentity = _modelIdentity();
      _modelResponseCurrentIdentity = _modelResponseInitialIdentity;
      _captureModelResponseControls(capsObj);
      _modelRenderTiles();
      _modelCapsRefreshBaseline();
      _scheduleEffortLadder();
      // Remove structured fields from capabilities display — only delete
      // thinking_mode/thinking_param when the UI successfully captured them.
      delete capsObj.server_compat;
      if (tmCaptured) {
        delete capsObj.thinking_mode;
        // Strip thinking_param only when a mode value round-trips through
        // the dropdown. With mode unset the save path writes neither key,
        // so a stored thinking_param must stay in the raw JSON — deleting
        // it here would silently drop it on the next unrelated edit-save.
        if (tmVal) delete capsObj.thinking_param;
      }
      // Strip effort_param only for the lanes whose save path re-adds it
      // (the same provider gate) — on other providers the field is
      // hidden and the save path never writes it, so deleting it here
      // would silently drop a stored key on an unrelated edit-save.
      if (
        m.provider === "openai-compatible" ||
        m.provider === "anthropic-compatible"
      ) {
        delete capsObj.effort_param;
      }
      const capsText = JSON.stringify(capsObj, null, 2);
      document.getElementById("model-capabilities").value =
        capsText === "{}" ? "" : capsText;
      // Calibration chip + Re-calibrate button: only for rerankers with an id.
      _paintCalibrationChip(
        Object.assign({ supports_rerank: isReranker }, _rerankCalFields),
      );
      const recalBtn = document.getElementById("model-recalibrate-btn");
      if (recalBtn) recalBtn.hidden = !isReranker;
      document.getElementById("model-enabled").checked = m.enabled !== false;
      // Reasoning persistence flags — defaults match the dataclass
      // defaults (persist=true, replay=false) when the API returns
      // them as undefined (legacy / pre-052 row).
      document.getElementById("model-surface-persisted-reasoning").checked =
        m.surface_persisted_reasoning !== false;
      document.getElementById("model-replay-reasoning").checked =
        m.replay_reasoning_to_model === true;
      _applyProviderDefaults();
    })
    .catch(function () {
      showToast("Failed to load model details");
    });
}

function hideCreateModelModal() {
  window.TurnstoneHatch.closeShelf(document.getElementById("model-shelf"));
}

function submitCreateModel() {
  const alias = document.getElementById("model-alias").value.trim();
  const modelName = document.getElementById("model-name").value.trim();
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

  const capsEl = document.getElementById("model-capabilities");
  const capsText = capsEl.value.trim();
  let caps = {};
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

  const providerVal = document.getElementById("model-provider").value;

  // Thinking mode → capabilities.  thinking_mode round-trips through the
  // dropdown for every provider, including anthropic-compatible (where
  // manual mode maps the session effort knob onto the template's
  // thinking toggle via chat_template_kwargs —
  // merge_reasoning_template_kwargs).
  const thinkingMode = document.getElementById("model-thinking-mode").value;
  if (thinkingMode) {
    caps.thinking_mode = thinkingMode;
    // Preserve thinking_param so Granite/DeepSeek "thinking" key
    // isn't silently reverted to the default "enable_thinking".
    const savedParam = document.getElementById("model-thinking-param").value;
    if (savedParam) caps.thinking_param = savedParam;
  }
  // Effort param (graded chat-template effort key, e.g. gpt-oss
  // "reasoning_effort") round-trips like thinking_param: lifted out of
  // the raw JSON on edit-load, re-added here when the field is set.
  // Gated on the local-server lanes (like the server_compat block
  // below): the field is hidden for other providers, so a lingering
  // value from a provider switch must never persist.
  if (
    providerVal === "openai-compatible" ||
    providerVal === "anthropic-compatible"
  ) {
    const effortParam = document
      .getElementById("model-effort-param")
      .value.trim();
    if (effortParam) caps.effort_param = effortParam;
  }

  // Build server_compat from structured fields.  Only meaningful for the
  // compat lanes (openai-compatible: all fields; anthropic-compatible: the
  // extra-body JSON only) — for other providers the section is hidden
  // but the form values can linger after a provider switch, so gate the
  // whole block on the active provider to keep persisted state honest.
  const serverCompat = {};
  const ebEl = document.getElementById("model-extra-body");
  ebEl.removeAttribute("aria-invalid");
  ebEl.style.borderColor = "";
  if (
    providerVal === "openai-compatible" ||
    providerVal === "anthropic-compatible"
  ) {
    if (providerVal === "openai-compatible") {
      const serverType = document.getElementById("model-server-type").value;
      if (serverType) serverCompat.server_type = serverType;
      const apiSurface = document.getElementById("model-api-surface").value;
      if (apiSurface) serverCompat.api_surface = apiSurface;
    }
    const ebText = ebEl.value.trim();
    if (ebText) {
      try {
        const ebParsed = JSON.parse(ebText);
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
  }
  if (Object.keys(serverCompat).length > 0) {
    caps.server_compat = serverCompat;
  }

  // Capability tiles: persist exactly the explicit keys (saved-in-JSON +
  // user-toggled). A key typed directly into the raw JSON wins (operator
  // override) — same philosophy as the calibration re-merge below.
  Object.keys(_modelCapsExplicit).forEach(function (k) {
    if (!(k in caps)) caps[k] = _modelGetTile(k);
  });
  _mergeModelResponseControls(caps);

  // Re-merge reranker calibration fields extracted on edit so an unrelated edit
  // doesn't silently drop the calibration. A field typed directly into the
  // capabilities JSON wins (operator override).
  Object.keys(_rerankCalFields).forEach(function (k) {
    if (!(k in caps)) caps[k] = _rerankCalFields[k];
  });

  const form = {
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
  const tempVal = document.getElementById("model-temperature").value.trim();
  if (tempVal !== "") {
    const t = parseFloat(tempVal);
    if (isNaN(t) || t < 0 || t > 2) {
      _showModelError("Temperature must be between 0 and 2");
      return;
    }
    form.temperature = t;
  } else {
    form.temperature = null;
  }
  const mtVal = document.getElementById("model-max-tokens").value.trim();
  if (mtVal !== "") {
    const mt = parseInt(mtVal, 10);
    if (isNaN(mt) || mt < 1) {
      _showModelError("Max tokens must be at least 1");
      return;
    }
    form.max_tokens = mt;
  } else {
    form.max_tokens = null;
  }
  const reVal = document.getElementById("model-reasoning-effort").value;
  if (reVal !== "") {
    form.reasoning_effort = reVal;
  } else {
    form.reasoning_effort = null;
  }

  // Reasoning persistence flags — always serialize so a flip from
  // default takes effect on PUT (the server's update path keys off
  // "field present in body").
  form.surface_persisted_reasoning = document.getElementById(
    "model-surface-persisted-reasoning",
  ).checked;
  form.replay_reasoning_to_model = document.getElementById(
    "model-replay-reasoning",
  ).checked;

  const apiKey = document.getElementById("model-api-key").value;
  if (apiKey) form.api_key = apiKey;

  const editId = document.getElementById("model-edit-id").value;
  const method = editId ? "PUT" : "POST";
  const url = editId
    ? "/v1/api/admin/model-definitions/" + encodeURIComponent(editId)
    : "/v1/api/admin/model-definitions";

  window.TurnstoneHatch.setBusy(document.getElementById("model-shelf"), true);
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
      window.TurnstoneHatch.setBusy(
        document.getElementById("model-shelf"),
        false,
      );
    });
}

function _showModelError(msg) {
  const e = document.getElementById("model-create-error");
  e.textContent = msg;
  e.classList.add("is-visible");
}

function _detectResultLine(text, color) {
  const div = document.createElement("div");
  div.style.marginTop = "3px";
  if (color) div.style.color = "var(--" + color + ")";
  div.textContent = text;
  return div;
}

// Pure: map a capabilities object to the reranker-calibration verdict chip.
// rerank_scale is the "has been calibrated" marker; with rerank_separated the
// per-model floor applies, without it the serving is suspect (no clean split).
function renderCalibrationVerdict(caps) {
  const c = _isPlainObject(caps) ? caps : {};
  if (c.rerank_scale && c.rerank_separated) {
    return {
      text: "✓ calibrated · floor " + Number(c.rerank_threshold).toFixed(2),
      cls: "ok",
    };
  }
  if (c.rerank_scale) {
    return {
      text: "⚠ no clean separation — check serving (--chat-template?)",
      cls: "warn",
    };
  }
  return { text: "not calibrated", cls: "muted" };
}

const _CALIBRATION_CHIP_COLORS = {
  ok: "var(--green)",
  warn: "var(--yellow)",
  muted: "var(--fg-dim)",
};

// Paint (or hide) the calibration chip from a capabilities object. Hidden when
// the model is not a reranker (no supports_rerank) so non-rerank models are
// unaffected.
function _paintCalibrationChip(caps) {
  const chip = document.getElementById("model-calibration-chip");
  if (!chip) return;
  const c = _isPlainObject(caps) ? caps : {};
  if (!c.supports_rerank) {
    chip.style.display = "none";
    chip.textContent = "";
    return;
  }
  const v = renderCalibrationVerdict(c);
  chip.textContent = v.text;
  chip.style.color = _CALIBRATION_CHIP_COLORS[v.cls] || "var(--fg-dim)";
  chip.style.borderColor =
    v.cls === "muted" ? "var(--border)" : _CALIBRATION_CHIP_COLORS[v.cls];
  chip.style.display = "inline-block";
}

function _clearDetectResult() {
  const rd = document.getElementById("model-detect-result");
  if (rd) {
    rd.hidden = true;
    rd.textContent = "";
    rd.style.borderColor = "";
  }
}

// Best-effort: is the model being edited a reranker? Reads supports_rerank from
// the capabilities textarea (the chip/calibration flow keeps it there).
function _editingReranker() {
  return _modelGetTile("supports_rerank");
}

function detectModel() {
  const btn = document.getElementById("model-detect-btn");
  const resultDiv = document.getElementById("model-detect-result");
  const isReranker = _editingReranker();
  btn.disabled = true;
  btn.setAttribute("aria-busy", "true");
  // Calibrate-on-detect adds a ~20s endpoint probe, so signal it.
  btn.textContent = isReranker ? "Calibrating\u2026" : "Detecting\u2026";
  resultDiv.hidden = true;
  resultDiv.textContent = "";

  const form = {
    provider: document.getElementById("model-provider").value,
    base_url: document.getElementById("model-base-url").value.trim(),
    model: document.getElementById("model-name").value.trim(),
  };
  const apiKey = document.getElementById("model-api-key").value;
  if (apiKey) form.api_key = apiKey;
  const editId = document.getElementById("model-edit-id").value;
  if (editId) form.definition_id = editId;
  // Tell the server to also calibrate when this is a reranker.
  if (isReranker) form.supports_rerank = true;

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
      resultDiv.hidden = false;
      resultDiv.textContent = "";
      if (d.error && !d.reachable) {
        resultDiv.appendChild(
          _detectResultLine("\u2717 Failed: " + d.error, "red"),
        );
        resultDiv.style.borderColor = "var(--red)";
        return;
      }
      let line1 = "\u2713 Connected";
      if (d.available_models && d.available_models.length) {
        line1 += " \u2014 " + d.available_models.length + " model(s) available";
      }
      resultDiv.appendChild(_detectResultLine(line1, "green"));

      if (d.model_found === false) {
        const models = d.available_models || [];
        let msg =
          '\u26A0 Model "' +
          form.model +
          '" not found in ' +
          models.length +
          " available model(s)";
        if (models.length > 0) {
          const shown = models.slice(0, 8);
          msg += ": " + shown.join(", ");
          if (models.length > 8)
            msg += ", \u2026 +" + (models.length - 8) + " more";
        }
        resultDiv.appendChild(_detectResultLine(msg, "yellow"));
      }

      if (d.available_models && d.available_models.length > 0) {
        const dl = document.getElementById("model-name-suggestions");
        if (dl) {
          dl.textContent = "";
          d.available_models.forEach(function (m) {
            const opt = document.createElement("option");
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
        const ctxInput = document.getElementById("model-ctx-window");
        if (parseInt(ctxInput.value, 10) === 0) {
          ctxInput.value = d.context_window;
        }
      }
      if (d.server_type) {
        resultDiv.appendChild(
          _detectResultLine("Server type: " + d.server_type),
        );
        // Auto-fill server type if not already set and value is a known option
        const stEl = document.getElementById("model-server-type");
        const stOpts = Array.from(stEl.options).map(function (o) {
          return o.value;
        });
        if (!stEl.value && stOpts.indexOf(d.server_type) !== -1)
          stEl.value = d.server_type;
      }
      // Auto-fill capabilities from suggested profile
      if (d.suggested_capabilities) {
        const sc2 = d.suggested_capabilities;
        const tmEl = document.getElementById("model-thinking-mode");
        if (!tmEl.value && sc2.thinking_mode) {
          tmEl.value = sc2.thinking_mode;
        }
        if (sc2.thinking_param) {
          const tpEl = document.getElementById("model-thinking-param");
          if (!tpEl.value) tpEl.value = sc2.thinking_param;
        }
        _toggleThinkingParam();
      }
      // Auto-fill server compat from suggested profile
      if (d.suggested_server_compat) {
        const ssc = d.suggested_server_compat;
        const stEl2 = document.getElementById("model-server-type");
        const stOpts2 = Array.from(stEl2.options).map(function (o) {
          return o.value;
        });
        if (
          !stEl2.value &&
          ssc.server_type &&
          stOpts2.indexOf(ssc.server_type) !== -1
        )
          stEl2.value = ssc.server_type;
        // Restrict to the known set so a hostile detect response can't
        // smuggle a non-listed value into the form.
        const _SURFACE_SUGGESTABLE = { chat: 1, responses: 1 };
        if (ssc.api_surface && _SURFACE_SUGGESTABLE[ssc.api_surface]) {
          const asEl = document.getElementById("model-api-surface");
          if (!asEl.value) asEl.value = ssc.api_surface;
        }
        if (ssc.extra_body) {
          const ebEl2 = document.getElementById("model-extra-body");
          if (!ebEl2.value.trim()) {
            const ebJson = JSON.stringify(ssc.extra_body, null, 2);
            if (ebJson !== "{}") ebEl2.value = ebJson;
          }
        }
      }
      if (d.suggested_capabilities || d.suggested_server_compat) {
        resultDiv.appendChild(
          _detectResultLine("\u2713 Compatibility profile suggested", "green"),
        );
      }
      // Calibrate-on-detect result: merge the returned calibration fields so a
      // subsequent save persists them, and paint the chip. The note covers the
      // graceful "could not calibrate" case (endpoint unreachable as a rerank).
      if (isReranker) {
        if (_isPlainObject(d.capabilities)) {
          ["rerank_threshold", "rerank_scale", "rerank_separated"].forEach(
            function (k) {
              if (k in d.capabilities) _rerankCalFields[k] = d.capabilities[k];
            },
          );
        }
        _paintCalibrationChip(
          Object.assign({ supports_rerank: true }, _rerankCalFields),
        );
        if (d.rerank_calibration_note) {
          resultDiv.appendChild(
            _detectResultLine("\u26a0 " + d.rerank_calibration_note, "yellow"),
          );
        }
      }
      resultDiv.style.borderColor = "var(--green)";
    })
    .catch(function (e) {
      if (e.message === "auth") return;
      resultDiv.hidden = false;
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

// Re-calibrate a saved reranker: POST to its calibrate endpoint (persists the
// floor server-side), then re-render the chip from the verdict.
function recalibrateModel() {
  const editId = document.getElementById("model-edit-id").value;
  if (!editId) return;
  const btn = document.getElementById("model-recalibrate-btn");
  const resultDiv = document.getElementById("model-detect-result");
  btn.disabled = true;
  btn.setAttribute("aria-busy", "true");
  btn.textContent = "Calibrating…";
  authFetch(
    "/v1/api/admin/model-definitions/" +
      encodeURIComponent(editId) +
      "/calibrate",
    { method: "POST", headers: { "Content-Type": "application/json" } },
  )
    .then(function (r) {
      if (!r.ok)
        return r.json().then(function (d) {
          throw new Error(d.error || "Calibrate failed");
        });
      return r.json();
    })
    .then(function (d) {
      resultDiv.hidden = false;
      resultDiv.textContent = "";
      if (d.error) {
        resultDiv.appendChild(_detectResultLine("✗ " + d.error, "red"));
        resultDiv.style.borderColor = "var(--red)";
        // Nothing was persisted — keep the prior (still-valid) verdict on the
        // chip by repainting from the retained fields rather than wiping it.
        _paintCalibrationChip(
          Object.assign({ supports_rerank: true }, _rerankCalFields),
        );
        return;
      }
      // Persisted server-side; mirror into _rerankCalFields + the chip.
      _rerankCalFields.rerank_scale = d.raw_scale;
      _rerankCalFields.rerank_separated = d.separated;
      _rerankCalFields.rerank_threshold =
        d.suggested_threshold != null ? d.suggested_threshold : 0;
      _paintCalibrationChip(
        Object.assign({ supports_rerank: true }, _rerankCalFields),
      );
      resultDiv.appendChild(
        _detectResultLine(
          d.separated
            ? "✓ Calibrated · floor " +
                Number(_rerankCalFields.rerank_threshold).toFixed(2)
            : "⚠ No clean separation — check serving",
          d.separated ? "green" : "yellow",
        ),
      );
      resultDiv.style.borderColor = d.separated
        ? "var(--green)"
        : "var(--yellow)";
    })
    .catch(function (e) {
      if (e.message === "auth") return;
      resultDiv.hidden = false;
      resultDiv.textContent = "";
      resultDiv.appendChild(_detectResultLine("✗ " + e.message, "red"));
      resultDiv.style.borderColor = "var(--red)";
    })
    .finally(function () {
      btn.disabled = false;
      btn.removeAttribute("aria-busy");
      btn.textContent = "Re-calibrate";
    });
}

/* Capability auto-fill: when the user types a known model name or
   changes the provider, look up static capabilities and refresh the
   context window, capability tiles, and conditional response controls. */
let _capsTimer = null;
let _modelCapsSeq = 0;
function _onModelFieldChange() {
  clearTimeout(_capsTimer);
  const nextIdentity = _modelIdentity();
  if (
    _modelResponseCurrentIdentity &&
    nextIdentity !== _modelResponseCurrentIdentity
  ) {
    _MODEL_RESPONSE_CONTROLS.forEach(function (spec) {
      const select = document.getElementById(spec.elementId);
      if (!select) return;
      const captured = _modelResponseCaptured[spec.key];
      select.value =
        nextIdentity === _modelResponseInitialIdentity &&
        _modelResponseValueValid(spec, captured)
          ? captured
          : "";
    });
  }
  _modelResponseCurrentIdentity = nextIdentity;
  _modelCapsSeq++; // invalidate any capability lookup already in flight
  _modelCapsBaseline = {};
  const banner = document.getElementById("model-autofill");
  if (banner) banner.hidden = true;
  _modelRenderTiles();
  _capsTimer = setTimeout(_modelCapsRefreshBaseline, 500);
  _scheduleEffortLadder();
}

/* Effort-ladder annotation: each knob position states, in plain words,
   what the request will carry — from the server-computed projection
   (providers/effort_ladder.py — equal "effective" tokens ⇒ identical
   requests).  A position whose delivered level matches its name stays
   plain ("Max"); a snapped position says so ("Low — sends high"); the
   adaptive none position warns "thinking stays on"; budget detail
   lives in the tooltip.  Never label a position after a sibling that
   shares its token — that rendered "Max (= minimal)", implying a
   downgrade the wire doesn't contain.  Defined here and shared as a
   page global with governance.js (skill launch config), which loads
   after this file. */
function _annotateEffortSelect(sel, ladder) {
  if (!sel) return;
  const byVal = {};
  (ladder || []).forEach(function (row) {
    byVal[row.value] = row.effective;
  });
  Array.from(sel.options).forEach(function (opt) {
    if (!opt.value) return; // "" = inherit-the-default option
    if (!opt.dataset.baseLabel) opt.dataset.baseLabel = opt.textContent;
    let label = opt.dataset.baseLabel;
    let title = "";
    const eff = byVal[opt.value];
    if (eff !== undefined) {
      title = "sends: " + eff;
      if (eff === "default") {
        label += " — model default";
      } else if (eff === "on") {
        // Local adaptive lane, none position: we send only the thinking
        // toggle (no effort grade exists), so the knob can't turn
        // thinking off — that is the whole story here.
        label += " — thinking stays on";
      } else if (eff === "adaptive") {
        // Native Anthropic adaptive: thinking is ALWAYS on and an effort
        // level rides output_config on every graded position, so
        // "thinking stays on" is true everywhere and not what sets the
        // none position apart.  What "none" uniquely means is that no
        // effort is pinned — the model self-regulates it.
        label += " — model sets effort";
      } else if (eff !== "off") {
        // "off" needs no echo on the none position.  Otherwise strip
        // the toggle prefix and budget qualifier ("on+high" → "high",
        // "high·budget:16384" → "high", bare "budget:4096" → "" = no
        // suffix); the tooltip keeps the full token.
        const wire = eff.replace(/^on\+/, "").replace(/(^|·)budget:\d+$/, "");
        if (wire && wire !== opt.value) label += " — sends " + wire;
      }
    }
    opt.textContent = label;
    opt.title = title;
  });
}

let _effortLadderTimer = null;
let _effortLadderSeq = 0;
function _scheduleEffortLadder() {
  clearTimeout(_effortLadderTimer);
  _effortLadderTimer = setTimeout(_refreshModelEffortLadder, 500);
}
function _refreshModelEffortLadder() {
  const shelf = document.getElementById("model-shelf");
  const sel = document.getElementById("model-reasoning-effort");
  if (!shelf || !shelf.open || !sel) return;
  const provider = document.getElementById("model-provider").value;
  const modelName = document.getElementById("model-name").value.trim();
  if (!modelName) {
    _effortLadderSeq++; // invalidate any in-flight response
    _annotateEffortSelect(sel, null);
    return;
  }
  // Assemble the same capabilities the save path would persist: raw
  // JSON base, structured thinking/effort fields overlaid.  Mid-edit
  // invalid JSON annotates from the structured fields alone.
  let caps = {};
  const rawText = document.getElementById("model-capabilities").value.trim();
  if (rawText) {
    try {
      const parsed = JSON.parse(rawText);
      if (_isPlainObject(parsed)) caps = parsed;
    } catch (e) {
      /* fall through */
    }
  }
  const tm = document.getElementById("model-thinking-mode").value;
  if (tm) {
    caps.thinking_mode = tm;
    const tp = document.getElementById("model-thinking-param").value;
    if (tp) caps.thinking_param = tp;
  }
  const ep = document.getElementById("model-effort-param").value.trim();
  if (ep) caps.effort_param = ep;
  const seq = ++_effortLadderSeq;
  authFetch("/v1/api/admin/models/effort-ladder", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      provider: provider,
      model: modelName,
      capabilities: caps,
      api_surface: document.getElementById("model-api-surface").value || "",
    }),
  })
    .then(function (r) {
      return r.ok ? r.json() : null;
    })
    .then(function (d) {
      if (seq !== _effortLadderSeq) return; // superseded by a newer edit
      _annotateEffortSelect(sel, d && d.ladder);
    })
    .catch(function () {
      /* silent — annotation only */
    });
}

/* Known-model lookup feeding the tile matrix: the table becomes the display
   BASELINE (with the provenance banner), explicit overrides stay on top, and
   nothing is written into the raw JSON — known models keep tracking the
   table at runtime instead of being pinned at save time. */
function _modelCapsRefreshBaseline() {
  const shelf = document.getElementById("model-shelf");
  if (!shelf || !shelf.open) return;
  const provider = document.getElementById("model-provider").value;
  const modelName = document.getElementById("model-name").value.trim();
  const banner = document.getElementById("model-autofill");
  const seq = ++_modelCapsSeq;
  if (
    !modelName ||
    provider === "openai-compatible" ||
    provider === "anthropic-compatible"
  ) {
    _modelCapsBaseline = {};
    banner.hidden = true;
    _modelRenderTiles();
    return;
  }
  // Two type-then-pause cycles can have both fetches in flight; a reordered
  // older response must not clobber the tiles (the _schPreviewSeq pattern).
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
      if (seq !== _modelCapsSeq) return; // superseded by a newer edit
      if (!d.known || !d.capabilities) {
        _modelCapsBaseline = {};
        banner.hidden = true;
        _modelRenderTiles();
        return;
      }
      const ctxInput = document.getElementById("model-ctx-window");
      if (parseInt(ctxInput.value, 10) === 0 && d.capabilities.context_window) {
        ctxInput.value = d.capabilities.context_window;
      }
      _modelCapsBaseline = {};
      _MODEL_CAP_KEYS.forEach(function (k) {
        if (k in d.capabilities) _modelCapsBaseline[k] = !!d.capabilities[k];
      });
      document.getElementById("model-autofill-name").textContent = modelName;
      banner.hidden = false;
      _modelRenderTiles();
    })
    .catch(function () {
      /* silent */
    });
}
/* Provider-specific placeholder hints for base_url and model ID fields.
   Keep URLs in sync with _PROVIDER_DEFAULT_URLS in console/server.py
   and GOOGLE_DEFAULT_BASE_URL in core/providers/_google.py. */
const _providerDefaults = {
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
  "anthropic-compatible": {
    urlPlaceholder: "e.g. http://your-vllm-host:8000",
    modelPlaceholder: "deepseek-ai/DeepSeek-V4-Flash",
  },
};

/* Update placeholders when provider changes. */
function _applyProviderDefaults() {
  const provider = document.getElementById("model-provider").value;
  const def = _providerDefaults[provider];
  if (!def) return;
  document.getElementById("model-base-url").placeholder = def.urlPlaceholder;
  document.getElementById("model-name").placeholder = def.modelPlaceholder;
  // Server compat section only applies to local model servers
  const scSection = document.getElementById("model-server-compat-section");
  if (scSection) {
    // hidden attr, not style.display — `.hatch [hidden]` is !important and
    // an inline display can never un-hide it.
    scSection.hidden =
      provider !== "openai-compatible" && provider !== "anthropic-compatible";
  }
  // Within the section, server type / API surface are openai-compatible
  // knobs (Detect heuristics + chat-vs-responses surface pick) and stay
  // hidden on the anthropic-compatible lane.  Thinking mode applies to
  // BOTH compat lanes: on anthropic-compatible it opts the model into
  // the effort-knob → chat_template_kwargs mapping.
  const serverFieldsRow = document.getElementById("model-server-fields-row");
  if (serverFieldsRow) {
    serverFieldsRow.hidden = provider === "anthropic-compatible";
  }
  _updateModelResponseControls();
}

/* Populate the model name datalist with known model prefixes for the
   selected provider.  Called on page load and provider change. */
function _refreshModelSuggestions() {
  const dl = document.getElementById("model-name-suggestions");
  if (!dl) return;
  const provider = document.getElementById("model-provider").value;
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
        const opt = document.createElement("option");
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
  const confirmBtn = document.getElementById("confirm-submit");
  if (confirmBtn) confirmBtn.addEventListener("click", _confirmCallback);
  const tcCopy = document.getElementById("tc-copy");
  if (tcCopy) tcCopy.addEventListener("click", copyCreatedToken);
  const nameEl = document.getElementById("model-name");
  const provEl = document.getElementById("model-provider");
  const tmEl = document.getElementById("model-thinking-mode");
  if (tmEl) tmEl.addEventListener("change", _toggleThinkingParam);
  if (tmEl) tmEl.addEventListener("change", _scheduleEffortLadder);
  _MODEL_RESPONSE_CONTROLS.forEach(function (spec) {
    const select = document.getElementById(spec.elementId);
    if (select)
      select.addEventListener("change", function () {
        _rememberModelResponseControl(spec);
      });
  });
  ["model-thinking-param", "model-effort-param", "model-capabilities"].forEach(
    function (id) {
      const el = document.getElementById(id);
      if (el) el.addEventListener("input", _scheduleEffortLadder);
    },
  );
  const rawCapsEl = document.getElementById("model-capabilities");
  if (rawCapsEl)
    rawCapsEl.addEventListener("input", function () {
      _modelResponseDirty = {};
    });
  const apiSurfEl = document.getElementById("model-api-surface");
  if (apiSurfEl) apiSurfEl.addEventListener("change", _onModelFieldChange);
  const grid = document.getElementById("model-capgrid");
  if (grid) {
    grid.addEventListener("change", function (e) {
      const cap = e.target && e.target.getAttribute("data-cap");
      if (!cap) return;
      // a toggle IS the override decision — the key persists from here on
      _modelCapsExplicit[cap] = e.target.checked;
      if (cap === "supports_verbosity" || cap === "supports_pro_mode") {
        const spec = _MODEL_RESPONSE_CONTROLS.find(function (item) {
          return item.supportKey === cap;
        });
        if (spec && !e.target.checked) {
          const select = document.getElementById(spec.elementId);
          if (select) select.value = "";
          delete _modelResponseCaptured[spec.key];
        }
        _updateModelResponseControls();
      }
      if (cap === "supports_rerank") {
        const recalBtn = document.getElementById("model-recalibrate-btn");
        if (recalBtn)
          recalBtn.hidden = !(
            e.target.checked && document.getElementById("model-edit-id").value
          );
        _paintCalibrationChip(
          Object.assign(
            { supports_rerank: e.target.checked },
            _rerankCalFields,
          ),
        );
      }
    });
  }
  const modelSubmit = document.getElementById("model-create-submit");
  if (modelSubmit) modelSubmit.addEventListener("click", submitCreateModel);
  const detectBtn = document.getElementById("model-detect-btn");
  if (detectBtn) detectBtn.addEventListener("click", detectModel);
  const recalBtn2 = document.getElementById("model-recalibrate-btn");
  if (recalBtn2) recalBtn2.addEventListener("click", recalibrateModel);
  if (nameEl) nameEl.addEventListener("input", _onModelFieldChange);
  if (provEl) {
    provEl.addEventListener("change", _onModelFieldChange);
    provEl.addEventListener("change", _refreshModelSuggestions);
    provEl.addEventListener("change", _clearDetectResult);
    provEl.addEventListener("change", _applyProviderDefaults);
  }
  /* Clear stale detect results when probe-relevant inputs change */
  ["model-base-url", "model-api-key"].forEach(function (id) {
    const el = document.getElementById(id);
    if (el) el.addEventListener("input", _clearDetectResult);
  });
})();

function _flagModelSyncPending() {
  const btn = document.getElementById("model-sync-btn");
  if (btn) btn.classList.add("model-sync-pending");
}
function _clearModelSyncPending() {
  const btn = document.getElementById("model-sync-btn");
  if (btn) btn.classList.remove("model-sync-pending");
}

function reloadModelNodes() {
  const btn = document.getElementById("model-sync-btn");
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

let _nodeMetaCache = {};

function loadAdminNodeMetadata() {
  const container = document.getElementById("admin-node-metadata-content");
  if (!container) return;
  setSafeHtml(container, '<div class="dashboard-empty">Loading\u2026</div>');

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
      setSafeHtml(
        container,
        '<div class="dashboard-empty">Failed to load node metadata</div>',
      );
    });
}

function _renderNodeMetadata() {
  const container = document.getElementById("admin-node-metadata-content");
  if (!container) return;
  const nodeIds = Object.keys(_nodeMetaCache).sort();
  if (!nodeIds.length) {
    setSafeHtml(
      container,
      '<div class="dashboard-empty">No nodes registered</div>',
    );
    return;
  }

  let html = "";
  nodeIds.forEach(function (nid) {
    const meta = _nodeMetaCache[nid] || [];
    html +=
      '<div class="settings-section" data-section="nm-' +
      escapeHtml(nid) +
      '" data-collapsed>';
    html +=
      '<div class="settings-section-header" role="button" tabindex="0" aria-expanded="false" ';
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
        const valStr =
          typeof m.value === "object"
            ? JSON.stringify(m.value)
            : String(m.value);
        const isAuto = m.source === "auto";
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
  setSafeHtml(container, html);

  // Bind section-header click/keydown (no inline handlers — keys come
  // from operator-controlled node IDs and would be a footgun in a
  // ``onclick="..._('NID')"`` attribute).
  const nmHeaders = container.querySelectorAll(".settings-section-header");
  for (let nh = 0; nh < nmHeaders.length; nh++) {
    nmHeaders[nh].addEventListener("click", function () {
      _toggleSettingsSection(this);
    });
    nmHeaders[nh].addEventListener("keydown", function (e) {
      _onSettingsHeaderKey(e, this);
    });
  }

  // Bind button handlers (data-* attrs carry node/key context)
  const delBtns = container.querySelectorAll(".nm-del-btn");
  for (let d = 0; d < delBtns.length; d++) {
    delBtns[d].addEventListener("click", function () {
      _deleteNodeMeta(
        this.getAttribute("data-node"),
        this.getAttribute("data-key"),
      );
    });
  }
  const addBtns = container.querySelectorAll(".nm-add-btn");
  for (let a = 0; a < addBtns.length; a++) {
    addBtns[a].addEventListener("click", function () {
      _addNodeMeta(this.getAttribute("data-node"));
    });
  }
}

function _addNodeMeta(nodeId) {
  const keyEl = document.getElementById("nm-key-" + nodeId);
  const valEl = document.getElementById("nm-val-" + nodeId);
  if (!keyEl || !valEl) return;
  const key = keyEl.value.trim();
  const rawVal = valEl.value.trim();
  if (!key) {
    showToast("Key is required", "error");
    return;
  }

  let value;
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

// Wire the shared row-action menu once.  admin.js loads at the end of the
// console <body>, so the document-level delegation below covers every admin
// table (including those rendered by governance.js, which loads after).
_initKebabMenus();
