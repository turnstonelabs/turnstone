/* Admin panel — user & token management for turnstone console */

let _adminTab = "users";
let _adminUsers = [];
let _adminTokenUserId = "";
let _adminChannelUserId = "";
let _lastCreatedToken = "";
let _cuTrapHandler = null;
let _ctTrapHandler = null;
let _tcTrapHandler = null;
let _ccTrapHandler = null;
let _cfTrapHandler = null;
let _adminWatches = [];
let _confirmCallbackFn = null;
let _confirmTriggerEl = null;

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
const INHERIT_EMPTY_LABEL_KEYS = ["model.task_effort"];

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

let _csTrapHandler = null;
let _esTrapHandler = null;
let _srTrapHandler = null;
let _editScheduleTriggerEl = null;
let _runsScheduleTriggerEl = null;

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
      const items = opts && opts.listKey ? data[opts.listKey] : data;
      if (!Array.isArray(items)) return;
      items.forEach(function (item) {
        const opt = document.createElement("option");
        opt.value = item[valueKey];
        opt.textContent =
          opts && opts.display ? opts.display(item) : item[labelKey];
        sel.appendChild(opt);
      });
      if (opts && opts.selected) sel.value = opts.selected;
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

// --- Create Schedule Modal ---

function toggleScheduleTypeFields() {
  const t = document.getElementById("cs-type").value;
  document.getElementById("cs-cron-group").style.display =
    t === "cron" ? "" : "none";
  document.getElementById("cs-at-group").style.display =
    t === "at" ? "" : "none";
  if (t === "cron") document.getElementById("cs-cron").focus();
  else document.getElementById("cs-at").focus();
}

function toggleScheduleNodeField() {
  const v = document.getElementById("cs-target").value;
  document.getElementById("cs-node-group").style.display =
    v === "node" ? "" : "none";
  if (v === "node") document.getElementById("cs-node").focus();
}

function showCreateScheduleModal() {
  const overlay = document.getElementById("create-schedule-overlay");
  overlay.style.display = "flex";
  document
    .getElementById("create-schedule-error")
    .classList.remove("is-visible");
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
    afterPopulate: _decorateScheduleModelPlaceholder,
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
  const trigger = document.querySelector("#admin-schedules .admin-action-btn");
  if (trigger) trigger.focus();
}

function submitCreateSchedule() {
  const name = (document.getElementById("cs-name").value || "").trim();
  const desc = (document.getElementById("cs-desc").value || "").trim();
  const schedType = document.getElementById("cs-type").value;
  const cronExpr = (document.getElementById("cs-cron").value || "").trim();
  let atTime = document.getElementById("cs-at").value || "";
  let targetMode = document.getElementById("cs-target").value;
  const nodeId = (document.getElementById("cs-node").value || "").trim();
  const model = (document.getElementById("cs-model").value || "").trim();
  const message = (document.getElementById("cs-message").value || "").trim();
  const skill = (document.getElementById("cs-template").value || "").trim();
  const autoApprove = document.getElementById("cs-autoapprove").checked;
  const notifyTargets = _collectNotifyTargets("cs");
  const errEl = document.getElementById("create-schedule-error");

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

  const btn = document.getElementById("cs-submit");
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
  const t = document.getElementById("es-type").value;
  document.getElementById("es-cron-group").style.display =
    t === "cron" ? "" : "none";
  document.getElementById("es-at-group").style.display =
    t === "at" ? "" : "none";
  if (t === "cron") document.getElementById("es-cron").focus();
  else document.getElementById("es-at").focus();
}

function toggleEditScheduleNodeField() {
  const v = document.getElementById("es-target").value;
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
      const isSpecificNode =
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
        afterPopulate: _decorateScheduleModelPlaceholder,
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
      document
        .getElementById("edit-schedule-error")
        .classList.remove("is-visible");
      document.getElementById("es-submit").disabled = false;
      document.getElementById("es-submit").textContent = "Save";
      const overlay = document.getElementById("edit-schedule-overlay");
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
  const taskId = document.getElementById("es-id").value;
  const name = (document.getElementById("es-name").value || "").trim();
  const message = (document.getElementById("es-message").value || "").trim();
  const schedType = document.getElementById("es-type").value;
  const cronExpr = (document.getElementById("es-cron").value || "").trim();
  let targetMode = document.getElementById("es-target").value;
  if (targetMode === "node")
    targetMode = (document.getElementById("es-node").value || "").trim();
  let atTime = document.getElementById("es-at").value || "";

  const editNotifyTargets = _collectNotifyTargets("es");
  const errEl = document.getElementById("edit-schedule-error");

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

  const btn = document.getElementById("es-submit");
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
      const overlay = document.getElementById("schedule-runs-overlay");
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
  const overlay = document.getElementById("create-channel-overlay");
  overlay.style.display = "flex";
  document
    .getElementById("create-channel-error")
    .classList.remove("is-visible");
  const ctSel = document.getElementById("cc-type");
  const uidInput = document.getElementById("cc-uid");
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
  const trigger = document.querySelector("#admin-channels .admin-action-btn");
  if (trigger) trigger.focus();
}

function submitCreateChannel() {
  const channelType = document.getElementById("cc-type").value;
  const channelUserId = (document.getElementById("cc-uid").value || "").trim();
  const errEl = document.getElementById("create-channel-error");

  if (!channelUserId)
    return _showModalError(errEl, "External user ID is required");

  const btn = document.getElementById("cc-submit");
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
  const overlay = document.getElementById("create-user-overlay");
  overlay.style.display = "flex";
  document.getElementById("create-user-error").classList.remove("is-visible");
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
  const trigger = document.querySelector("#admin-users .admin-action-btn");
  if (trigger) trigger.focus();
}

function submitCreateUser() {
  const username = (document.getElementById("cu-username").value || "").trim();
  const displayName = (
    document.getElementById("cu-displayname").value || ""
  ).trim();
  const password = document.getElementById("cu-password").value || "";
  const confirm = document.getElementById("cu-confirm").value || "";
  const errEl = document.getElementById("create-user-error");

  if (!username) return _showModalError(errEl, "Username is required");
  if (!displayName) return _showModalError(errEl, "Display name is required");
  if (!password) return _showModalError(errEl, "Password is required");
  if (password.length < 8)
    return _showModalError(errEl, "Password must be at least 8 characters");
  if (password !== confirm)
    return _showModalError(errEl, "Passwords do not match");

  const btn = document.getElementById("cu-submit");
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
  const overlay = document.getElementById("create-token-overlay");
  overlay.style.display = "flex";
  document.getElementById("create-token-error").classList.remove("is-visible");
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
  const trigger = document.querySelector("#admin-tokens .admin-action-btn");
  if (trigger) trigger.focus();
}

function submitCreateToken() {
  const name = (document.getElementById("ct-name").value || "").trim();
  const scopes = document.getElementById("ct-scopes").value;
  const expiresDays = document.getElementById("ct-expires").value;
  const errEl = document.getElementById("create-token-error");

  const btn = document.getElementById("ct-submit");
  btn.disabled = true;
  btn.textContent = "Creating\u2026";

  const body = { name: name, scopes: scopes };
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
  const trigger = document.querySelector("#admin-tokens .admin-action-btn");
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
    const el = document.getElementById("token-created-value");
    const range = document.createRange();
    range.selectNodeContents(el);
    const sel = window.getSelection();
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
      const box = document.getElementById(boxId);
      if (!box) return;
      const focusable = box.querySelectorAll(
        "input:not([disabled]):not([type='hidden']), select:not([disabled]), textarea:not([disabled]), button:not([disabled])",
      );
      const visible = [];
      for (let i = 0; i < focusable.length; i++) {
        if (focusable[i].offsetParent !== null) visible.push(focusable[i]);
      }
      if (visible.length === 0) return;
      const first = visible[0];
      const last = visible[visible.length - 1];
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
  const overlay = document.getElementById(overlayId);
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
  const handler = _modalFocusTrap(boxId);
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
  const openHelp = document.querySelector('.settings-help-popover[style=""]');
  if (openHelp) {
    e.preventDefault();
    _closeAllSettingsHelp();
    return;
  }
  const cu = document.getElementById("create-user-overlay");
  if (cu && cu.style.display !== "none") {
    e.preventDefault();
    hideCreateUserModal();
    return;
  }
  const ct = document.getElementById("create-token-overlay");
  if (ct && ct.style.display !== "none") {
    e.preventDefault();
    hideCreateTokenModal();
    return;
  }
  const tc = document.getElementById("token-created-overlay");
  if (tc && tc.style.display !== "none") {
    e.preventDefault();
    hideTokenCreatedModal();
    return;
  }
  const cc = document.getElementById("create-channel-overlay");
  if (cc && cc.style.display !== "none") {
    e.preventDefault();
    hideCreateChannelModal();
    return;
  }
  const cso = document.getElementById("create-schedule-overlay");
  if (cso && cso.style.display !== "none") {
    e.preventDefault();
    hideCreateScheduleModal();
    return;
  }
  const eso = document.getElementById("edit-schedule-overlay");
  if (eso && eso.style.display !== "none") {
    e.preventDefault();
    hideEditScheduleModal();
    return;
  }
  const sro = document.getElementById("schedule-runs-overlay");
  if (sro && sro.style.display !== "none") {
    e.preventDefault();
    hideScheduleRunsModal();
    return;
  }
  const cf = document.getElementById("confirm-overlay");
  if (cf && cf.style.display !== "none") {
    e.preventDefault();
    hideConfirmModal();
    return;
  }
  // Governance modals
  const govOverlays = [
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
  for (let gi = 0; gi < govOverlays.length; gi++) {
    const govEl = document.getElementById(govOverlays[gi][0]);
    if (govEl && govEl.style.display !== "none") {
      e.preventDefault();
      govOverlays[gi][1]();
      return;
    }
  }
});

// ---------------------------------------------------------------------------
// Confirm Modal (reusable styled replacement for confirm())
// ---------------------------------------------------------------------------

function showConfirmModal(title, message, actionLabel, callback) {
  _confirmCallbackFn = callback;
  _confirmTriggerEl = document.activeElement;
  document.getElementById("confirm-title").textContent = title;
  document.getElementById("confirm-message").textContent = message;
  const btn = document.getElementById("confirm-submit");
  btn.textContent = actionLabel;
  btn.disabled = false;
  const overlay = document.getElementById("confirm-overlay");
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
  const fn = _confirmCallbackFn;
  _confirmCallbackFn = null;
  const btn = document.getElementById("confirm-submit");
  if (btn) btn.disabled = true;
  if (fn) fn();
  hideConfirmModal();
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

      // Merge values + schema.  Skip role-assignment settings owned by
      // the Models → Roles sub-tab (judge.* settings still live on the
      // Judge tab; the model-tab roles render only there).
      const merged = {};
      const roleKeys = {
        "coordinator.model_alias": 1,
        "coordinator.reasoning_effort": 1,
        "model.task_alias": 1,
        "model.task_effort": 1,
        "channels.default_model_alias": 1,
      };
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

  // Disable save for empty number fields (server will reject)
  const emptyNumber = inp.type === "number" && current === "";
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

// Show an error message in a modal's [role="alert"] element. The .is-visible
// class is the canonical toggle — see `.admin-modal [role="alert"]` in
// style.css. Do NOT use `el.style.display = "block"` here; the CSS rule
// `.admin-modal [role="alert"] { display: none }` is selector-equivalent and
// will hide the element again as soon as the inline style is cleared. Hide
// side is `el.classList.remove("is-visible")`.
function _showModalError(el, msg) {
  el.textContent = msg;
  el.classList.add("is-visible");
}

/* ── MCP Servers tab ─────────────────────────────────────────────────────── */

let _mcpServers = [];
let _mcpCreateTrap = null;
let _mcpCreateTrigger = null;
let _mcpImportTrap = null;
let _mcpImportTrigger = null;
let _mcpDetailTrap = null;
let _mcpDetailTrigger = null;
let _mcpInstallTrap = null;
let _mcpInstallTrigger = null;
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
    const isOauth = s.auth_type === "oauth_user";
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
  el.querySelectorAll("[data-mcp-bulk-revoke]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      const name = this.getAttribute("data-mcp-bulk-revoke");
      const count = this.getAttribute("data-mcp-consent-count") || "?";
      showConfirmModal(
        "Bulk-revoke MCP consents",
        "Drop all " +
          count +
          ' user consents for server "' +
          name +
          '"? Users will need to re-consent on next use. Upstream revoke is not attempted in bulk; tokens at the authorization server will expire naturally.',
        "Bulk-revoke",
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
              showToast(
                "Bulk-revoked " + (j.rows_deleted || 0) + " row(s) for " + name,
              );
              loadAdminMcp();
            })
            .catch(function () {
              showToast("Failed to bulk-revoke " + name);
            });
        },
      );
    });
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
  document.getElementById("mcp-stdio-fields").style.display =
    v === "stdio" ? "" : "none";
  document.getElementById("mcp-http-fields").style.display =
    v === "streamable-http" ? "" : "none";
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
  const oauthDiv = document.getElementById("mcp-oauth-fields");
  if (oauthDiv) {
    oauthDiv.style.display = authType === "oauth_user" ? "" : "none";
  }
  // The "Headers" textarea (inside mcp-http-fields) is only meaningful
  // for static auth; hide it for 'none' / 'oauth_user' so operators
  // don't accidentally configure stale credentials.
  const headersInput = document.getElementById("mcp-headers");
  if (headersInput) {
    const headersLabel = document.querySelector('label[for="mcp-headers"]');
    const show = authType === "static";
    headersInput.style.display = show ? "" : "none";
    if (headersLabel) headersLabel.style.display = show ? "" : "none";
  }
}

function _wireMcpAudienceAutofill() {
  // Idempotent — only attach the listener once per page lifetime.
  const urlInput = document.getElementById("mcp-url");
  if (!urlInput || urlInput.dataset.audAutofill === "1") return;
  urlInput.dataset.audAutofill = "1";
  urlInput.addEventListener("blur", function () {
    const aud = document.getElementById("mcp-oauth-audience");
    if (aud && !aud.value.trim()) aud.value = urlInput.value.trim();
  });
}

function showCreateMcpModal() {
  _mcpCreateTrigger = document.activeElement;
  const ov = document.getElementById("mcp-create-overlay");
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
  // Reset auth radios + OAuth subfields to the 'static' default.
  document.getElementById("mcp-auth-static").checked = true;
  document.getElementById("mcp-auth-none").checked = false;
  document.getElementById("mcp-auth-oauth").checked = false;
  document.getElementById("mcp-oauth-as-url").value = "";
  document.getElementById("mcp-oauth-registration").value = "preregistered";
  document.getElementById("mcp-oauth-client-id").value = "";
  document.getElementById("mcp-oauth-client-secret").value = "";
  document.getElementById("mcp-oauth-scopes").value = "";
  document.getElementById("mcp-oauth-audience").value = "";
  document.getElementById("mcp-create-error").classList.remove("is-visible");
  toggleMcpTransport();
  toggleMcpAuthFields();
  _wireMcpAudienceAutofill();
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
      // 'none' / 'oauth_user' — clear server-side static headers state.
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
  }

  return payload;
}

function submitCreateMcp() {
  const form = _parseMcpForm();
  if (form.error) {
    const e = document.getElementById("mcp-create-error");
    e.textContent = form.error;
    e.classList.add("is-visible");
    return;
  }
  const editId = document.getElementById("mcp-edit-id").value;
  const method = editId ? "PUT" : "POST";
  const url = editId
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
      const el = document.getElementById("mcp-create-error");
      el.textContent = e.message;
      el.classList.add("is-visible");
    })
    .finally(function () {
      document.getElementById("mcp-create-submit").disabled = false;
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
  _mcpDetailTrigger = document.activeElement;

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
      html += "<li>" + dot + nodeInfo + "</li>";
    }
    html += "</ul>";
  }
  html += "</div></div></div>";

  document.getElementById("mcp-detail-title").textContent = s.name;
  setSafeHtml(document.getElementById("mcp-detail-content"), html);
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
  document.getElementById("mcp-import-error").classList.remove("is-visible");
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
      const el = document.getElementById("mcp-import-error");
      el.textContent = e.message;
      el.classList.add("is-visible");
    })
    .finally(function () {
      document.getElementById("mcp-import-submit").disabled = false;
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

function _showInstallMcpModal(srv, hasRemote, hasPackage) {
  _mcpInstallTrigger = document.activeElement;
  const ov = document.getElementById("mcp-install-overlay");
  ov.style.display = "flex";
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
  _mcpInstallTrap = _installTrap("mcp-install-overlay", "mcp-install-box");
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
          '">';
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
  setSafeHtml(fieldsEl, html);
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
  const submitBtn = document.getElementById("mcp-install-submit");
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
      const overlay = document.getElementById("mcp-install-overlay");
      if (overlay && overlay.style.display !== "none") {
        hideInstallMcpModal();
      }
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
      const overlay = document.getElementById("mcp-install-overlay");
      const errEl = document.getElementById("mcp-install-error");
      if (errEl && overlay && overlay.style.display !== "none") {
        errEl.textContent = e.message;
        errEl.classList.add("is-visible");
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
let _modelCreateTrap = null;
let _modelCreateTrigger = null;
// Reranker calibration fields extracted out of the capabilities textarea in the
// edit modal (like server_compat), held here so they survive an unrelated edit
// and are re-merged on save. Reset per modal open.
let _rerankCalFields = {};

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
      "Transcribes microphone audio in the workstream composer (voice input). Empty disables the mic affordance — there is no audio-capable session fallback.",
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
    label: "Reranker",
    description:
      "Reranks web_search results. Point at a model whose base_url is a Cohere/Jina-compatible /rerank endpoint and whose capabilities include supports_rerank. Empty disables reranking. Enabling a reranker sends web_search results AND BM25 retrieval candidates (tool/skill descriptions and memory content) to this endpoint; self-hosted endpoints keep it on your infrastructure.",
    aliasKey: "tools.reranker_alias",
    fallbackKind: "disabled",
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
      blank.textContent = "(disabled — voice off)";
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
          : m.provider === "openai-compatible"
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
  row.style.display = mode ? "" : "none";
  // Set default when first enabling
  const paramEl = document.getElementById("model-thinking-param");
  if (mode && !paramEl.value) paramEl.value = "enable_thinking";
}

function showCreateModelModal() {
  _modelCreateTrigger = document.activeElement;
  const ov = document.getElementById("model-create-overlay");
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
  document.getElementById("model-api-surface").value = "";
  document.getElementById("model-thinking-mode").value = "";
  document.getElementById("model-thinking-param").value = "";
  document.getElementById("model-thinking-param-row").style.display = "none";
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
  document.getElementById("model-detect-result").style.display = "none";
  document.getElementById("model-detect-btn").disabled = false;
  document.getElementById("model-detect-btn").textContent = "Detect";
  // Reranker calibration: reset extracted fields and hide the chip + the
  // Re-calibrate button (edit mode re-shows them after loading the def).
  _rerankCalFields = {};
  const _calChip = document.getElementById("model-calibration-chip");
  if (_calChip) _calChip.style.display = "none";
  const _recalBtn = document.getElementById("model-recalibrate-btn");
  if (_recalBtn) _recalBtn.style.display = "none";
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
      // represent it ("manual" or "").  Values like "adaptive" (Anthropic-
      // only) stay in the raw capabilities JSON so they aren't silently
      // lost on save.
      const tmVal = capsObj.thinking_mode || "";
      const tmRepresentable = tmVal === "" || tmVal === "manual";
      if (tmRepresentable) {
        document.getElementById("model-thinking-mode").value = tmVal;
        document.getElementById("model-thinking-param").value =
          capsObj.thinking_param || "";
      } else {
        document.getElementById("model-thinking-mode").value = "";
        document.getElementById("model-thinking-param").value = "";
      }
      _toggleThinkingParam();
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
      // Remove structured fields from capabilities display — only delete
      // thinking_mode/thinking_param when the UI successfully captured them.
      delete capsObj.server_compat;
      if (tmRepresentable) {
        delete capsObj.thinking_mode;
        delete capsObj.thinking_param;
      }
      const capsText = JSON.stringify(capsObj, null, 2);
      document.getElementById("model-capabilities").value =
        capsText === "{}" ? "" : capsText;
      // Calibration chip + Re-calibrate button: only for rerankers with an id.
      _paintCalibrationChip(
        Object.assign({ supports_rerank: isReranker }, _rerankCalFields),
      );
      const recalBtn = document.getElementById("model-recalibrate-btn");
      if (recalBtn)
        recalBtn.style.display = isReranker ? "inline-block" : "none";
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
  document.getElementById("model-create-overlay").style.display = "none";
  _modelCreateTrap = _removeTrap(_modelCreateTrap);
  if (_modelCreateTrigger && _modelCreateTrigger.focus)
    _modelCreateTrigger.focus();
  _modelCreateTrigger = null;
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

  // Thinking mode → capabilities (provider uses this to inject
  // the correct chat_template_kwargs param automatically).
  const thinkingMode = document.getElementById("model-thinking-mode").value;
  if (thinkingMode) {
    caps.thinking_mode = thinkingMode;
    // Preserve thinking_param so Granite/DeepSeek "thinking" key
    // isn't silently reverted to the default "enable_thinking".
    const savedParam = document.getElementById("model-thinking-param").value;
    if (savedParam) caps.thinking_param = savedParam;
  }

  // Build server_compat from structured fields.  Only meaningful for
  // openai-compatible aliases — for other providers the section is hidden
  // but the form values can linger after a provider switch, so gate the
  // whole block on the active provider to keep persisted state honest.
  const serverCompat = {};
  const providerVal = document.getElementById("model-provider").value;
  const ebEl = document.getElementById("model-extra-body");
  ebEl.removeAttribute("aria-invalid");
  ebEl.style.borderColor = "";
  if (providerVal === "openai-compatible") {
    const serverType = document.getElementById("model-server-type").value;
    if (serverType) serverCompat.server_type = serverType;
    const apiSurface = document.getElementById("model-api-surface").value;
    if (apiSurface) serverCompat.api_surface = apiSurface;
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
    rd.style.display = "none";
    rd.textContent = "";
    rd.style.borderColor = "";
  }
}

// Best-effort: is the model being edited a reranker? Reads supports_rerank from
// the capabilities textarea (the chip/calibration flow keeps it there).
function _editingReranker() {
  try {
    const c = JSON.parse(
      document.getElementById("model-capabilities").value.trim() || "{}",
    );
    return _isPlainObject(c) && !!c.supports_rerank;
  } catch (e) {
    return false;
  }
}

function detectModel() {
  const btn = document.getElementById("model-detect-btn");
  const resultDiv = document.getElementById("model-detect-result");
  const isReranker = _editingReranker();
  btn.disabled = true;
  btn.setAttribute("aria-busy", "true");
  // Calibrate-on-detect adds a ~20s endpoint probe, so signal it.
  btn.textContent = isReranker ? "Calibrating\u2026" : "Detecting\u2026";
  resultDiv.style.display = "none";
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
      resultDiv.style.display = "block";
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
      resultDiv.style.display = "block";
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
      resultDiv.style.display = "block";
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
   changes the provider, look up static capabilities and pre-fill
   context_window and the capabilities textarea. */
let _capsTimer = null;
function _onModelFieldChange() {
  clearTimeout(_capsTimer);
  _capsTimer = setTimeout(function () {
    const overlay = document.getElementById("model-create-overlay");
    if (!overlay || overlay.style.display === "none") return;
    const provider = document.getElementById("model-provider").value;
    const modelName = document.getElementById("model-name").value.trim();
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
        const ctxInput = document.getElementById("model-ctx-window");
        if (
          parseInt(ctxInput.value, 10) === 0 &&
          d.capabilities.context_window
        ) {
          ctxInput.value = d.capabilities.context_window;
        }
        const capsInput = document.getElementById("model-capabilities");
        if (!capsInput.value.trim()) {
          const caps = Object.assign({}, d.capabilities);
          delete caps.context_window;
          delete caps.max_output_tokens;
          delete caps.token_param;
          delete caps.supports_streaming;
          delete caps.supports_tools;
          const text = JSON.stringify(caps, null, 2);
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
    scSection.style.display = provider === "openai-compatible" ? "" : "none";
  }
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
  const nameEl = document.getElementById("model-name");
  const provEl = document.getElementById("model-provider");
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
