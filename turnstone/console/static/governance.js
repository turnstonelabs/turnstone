/* Governance tabs — roles, policies, skills, usage, audit */

// ---------------------------------------------------------------------------
// Module state
// ---------------------------------------------------------------------------
let _govRoles = [];
let _govPolicies = [];
let _govSkills = [];
let _govUsageRange = "7d";
let _govUsageGroupBy = "day";
let _govAuditEvents = [];
let _govAuditTotal = 0;
let _govAuditOffset = 0;
let _skillCurrentView = "installed";
let _skillDiscoverResults = [];
let _skillDiscoverQuery = "";
let _pendingResources = [];
let _giTrapHandler = null;
let _giTriggerEl = null;

// Trap handler refs for modals
let _crTrapHandler = null; // create role
let _erTrapHandler = null; // edit role
let _urTrapHandler = null; // user roles
let _cpTrapHandler = null; // create policy
let _epTrapHandler = null; // edit policy
let _ctmTrapHandler = null; // create template
let _etmTrapHandler = null; // edit template

// Trigger element refs for focus restoration
let _crTriggerEl = null;
let _erTriggerEl = null;
let _urTriggerEl = null;
let _cpTriggerEl = null;
let _epTriggerEl = null;
let _ctmTriggerEl = null;
let _etmTriggerEl = null;

// ---------------------------------------------------------------------------
// Roles
// ---------------------------------------------------------------------------

function loadGovRoles() {
  authFetch("/v1/api/admin/roles")
    .then(function (r) {
      if (!r.ok) throw new Error("Failed");
      return r.json();
    })
    .then(function (data) {
      _govRoles = data.roles || [];
      _renderGovRoles(_govRoles);
    })
    .catch(function () {
      setSafeHtml(
        document.getElementById("admin-roles-table"),
        '<div class="dashboard-empty">Failed to load roles</div>',
      );
    });
}

function _renderGovRoles(items) {
  const el = document.getElementById("admin-roles-table");
  if (!items.length) {
    setSafeHtml(el, '<div class="dashboard-empty">No roles defined</div>');
    return;
  }
  let html = "";
  for (let i = 0; i < items.length; i++) {
    const r = items[i];
    // Render permissions as badges
    const perms = (r.permissions || "").split(",");
    let badges = "";
    for (let j = 0; j < perms.length; j++) {
      const p = perms[j].trim();
      if (!p) continue;
      let cls = "scope-badge";
      if (p === "approve" || p.indexOf("admin.") === 0) cls += " scope-approve";
      else if (p === "write" || p.indexOf("workstreams.") === 0)
        cls += " scope-write";
      badges += '<span class="' + cls + '">' + escapeHtml(p) + "</span>";
    }
    const typeLabel = r.builtin
      ? '<span class="scope-badge scope-channel">builtin</span>'
      : "";
    const actions = r.builtin
      ? ""
      : '<button class="admin-btn-action" data-edit-role="' +
        escapeHtml(r.role_id) +
        '">edit</button>' +
        '<button class="admin-btn-danger" data-delete-role="' +
        escapeHtml(r.role_id) +
        '" data-role-name="' +
        escapeHtml(r.name) +
        '">delete</button>';
    html +=
      '<div class="admin-row" role="listitem">' +
      '<span class="admin-col admin-col-rname">' +
      escapeHtml(r.display_name) +
      " " +
      typeLabel +
      "</span>" +
      '<span class="admin-col admin-col-rperms">' +
      badges +
      "</span>" +
      '<span class="admin-col admin-col-actions">' +
      actions +
      "</span></div>";
  }
  setSafeHtml(el, html);
  // Bind edit
  const editBtns = el.querySelectorAll("[data-edit-role]");
  for (let k = 0; k < editBtns.length; k++) {
    editBtns[k].addEventListener("click", function () {
      showEditRoleModal(this.getAttribute("data-edit-role"));
    });
  }
  // Bind delete
  const delBtns = el.querySelectorAll("[data-delete-role]");
  for (let k = 0; k < delBtns.length; k++) {
    delBtns[k].addEventListener("click", function () {
      const rid = this.getAttribute("data-delete-role");
      const rname = this.getAttribute("data-role-name");
      showConfirmModal(
        "Delete Role",
        'Delete role "' +
          rname +
          '"? Users with this role will lose its permissions.',
        "Delete",
        function () {
          authFetch("/v1/api/admin/roles/" + rid, { method: "DELETE" })
            .then(function (r) {
              if (!r.ok) throw new Error();
              return r.json();
            })
            .then(function () {
              showToast("Role deleted");
              loadGovRoles();
            })
            .catch(function () {
              showToast("Failed to delete role");
            });
        },
      );
    });
  }
}

// Permission inventory grouped by namespace so the role modal can
// render each section under its own heading.  Sectioning prevents the
// row-flow grid from slicing a namespace mid-column (e.g. half of
// ``admin.*`` ending up in column 1, the rest in column 2) and lets
// readers who don't yet know the permission taxonomy scan by
// concept.  Each section's permissions render as a 2-column grid;
// the ``Scopes`` and ``Workstreams & Tools`` sections are short
// enough to fit one row, ``Admin`` carries the bulk.
const _PERMISSION_SECTIONS = [
  {
    label: "Scopes",
    permissions: ["read", "write", "approve"],
  },
  {
    label: "Admin",
    permissions: [
      "admin.users",
      "admin.roles",
      "admin.orgs",
      "admin.policies",
      "admin.skills",
      "admin.audit",
      "admin.usage",
      "admin.schedules",
      "admin.watches",
      "admin.judge",
      "admin.memories",
      "admin.settings",
      "admin.mcp",
    ],
  },
  {
    label: "Workstreams & Tools",
    permissions: ["workstreams.create", "workstreams.close", "tools.approve"],
  },
  {
    label: "Model",
    permissions: ["model.skills.write"],
  },
];

// Flat list — kept for any caller that wants the full permission
// inventory without caring about sectioning.
const _ALL_PERMISSIONS = (function () {
  let flat = [];
  for (let i = 0; i < _PERMISSION_SECTIONS.length; i++) {
    flat = flat.concat(_PERMISSION_SECTIONS[i].permissions);
  }
  return flat;
})();

function _buildPermCheckboxes(prefix, selected) {
  // Emits the toggle-switch component used elsewhere in the admin
  // modals so each permission reads as a deliberate on/off rather
  // than a generic checkbox.  Sections are wrapped in a
  // ``.perm-section`` block with a caps-styled heading so the
  // typographic system inside the role modal stays consistent (the
  // surrounding label cadence is also caps + 0.08em letter-spacing).
  // The underlying ``<input type="checkbox" name="{prefix}-perm">``
  // shape is preserved so ``_collectPermCheckboxes`` still picks
  // them up regardless of section.
  let html = "";
  for (let s = 0; s < _PERMISSION_SECTIONS.length; s++) {
    const section = _PERMISSION_SECTIONS[s];
    html +=
      '<div class="perm-section">' +
      '<div class="perm-section-label">' +
      escapeHtml(section.label) +
      "</div>" +
      '<div class="perm-grid">';
    for (let i = 0; i < section.permissions.length; i++) {
      const p = section.permissions[i];
      const checked = selected && selected.indexOf(p) >= 0 ? " checked" : "";
      html +=
        '<label class="toggle-switch perm-toggle">' +
        '<input type="checkbox" value="' +
        p +
        '" name="' +
        prefix +
        '-perm"' +
        checked +
        ">" +
        '<span class="toggle-track" aria-hidden="true"></span>' +
        '<span class="toggle-label">' +
        escapeHtml(p) +
        "</span></label>";
    }
    html += "</div></div>";
  }
  return html;
}

function _collectPermCheckboxes(prefix) {
  const boxes = document.querySelectorAll(
    'input[name="' + prefix + '-perm"]:checked',
  );
  const perms = [];
  for (let i = 0; i < boxes.length; i++) perms.push(boxes[i].value);
  return perms.join(",");
}

function showCreateRoleModal() {
  _crTriggerEl = document.activeElement;
  const ov = document.getElementById("create-role-overlay");
  ov.style.display = "flex";
  document.getElementById("cr-name").value = "";
  document.getElementById("cr-displayname").value = "";
  setSafeHtml(
    document.getElementById("cr-perms-container"),
    _buildPermCheckboxes("cr", []),
  );
  document.getElementById("create-role-error").classList.remove("is-visible");
  document.getElementById("cr-name").focus();
  _crTrapHandler = _installTrap("create-role-overlay", "create-role-box");
}

function hideCreateRoleModal() {
  document.getElementById("create-role-overlay").style.display = "none";
  _crTrapHandler = _removeTrap(_crTrapHandler);
  if (_crTriggerEl && _crTriggerEl.focus) {
    _crTriggerEl.focus();
  }
  _crTriggerEl = null;
}

function submitCreateRole() {
  const name = document.getElementById("cr-name").value.trim();
  let dname = document.getElementById("cr-displayname").value.trim();
  const perms = _collectPermCheckboxes("cr");
  if (!name) {
    const e = document.getElementById("create-role-error");
    e.textContent = "Name is required";
    e.classList.add("is-visible");
    return;
  }
  if (!dname) dname = name;
  document.getElementById("cr-submit").disabled = true;
  authFetch("/v1/api/admin/roles", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      name: name,
      display_name: dname,
      permissions: perms,
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
      hideCreateRoleModal();
      showToast("Role created");
      loadGovRoles();
    })
    .catch(function (e) {
      const el = document.getElementById("create-role-error");
      el.textContent = e.message;
      el.classList.add("is-visible");
    })
    .finally(function () {
      document.getElementById("cr-submit").disabled = false;
    });
}

function showEditRoleModal(roleId) {
  _erTriggerEl = document.activeElement;
  let role = null;
  for (let i = 0; i < _govRoles.length; i++) {
    if (_govRoles[i].role_id === roleId) {
      role = _govRoles[i];
      break;
    }
  }
  if (!role) return;
  const ov = document.getElementById("edit-role-overlay");
  ov.style.display = "flex";
  document.getElementById("er-id").value = roleId;
  document.getElementById("er-name").value = role.display_name;
  const selected = (role.permissions || "").split(",");
  setSafeHtml(
    document.getElementById("er-perms-container"),
    _buildPermCheckboxes("er", selected),
  );
  document.getElementById("edit-role-error").classList.remove("is-visible");
  _erTrapHandler = _installTrap("edit-role-overlay", "edit-role-box");
}

function hideEditRoleModal() {
  document.getElementById("edit-role-overlay").style.display = "none";
  _erTrapHandler = _removeTrap(_erTrapHandler);
  if (_erTriggerEl && _erTriggerEl.focus) {
    _erTriggerEl.focus();
  }
  _erTriggerEl = null;
}

function submitEditRole() {
  const roleId = document.getElementById("er-id").value;
  const dname = document.getElementById("er-name").value.trim();
  const perms = _collectPermCheckboxes("er");
  document.getElementById("er-submit").disabled = true;
  authFetch("/v1/api/admin/roles/" + roleId, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ display_name: dname, permissions: perms }),
  })
    .then(function (r) {
      if (!r.ok)
        return r.json().then(function (d) {
          throw new Error(d.error || "Failed");
        });
      return r.json();
    })
    .then(function () {
      hideEditRoleModal();
      showToast("Role updated");
      loadGovRoles();
    })
    .catch(function (e) {
      const el = document.getElementById("edit-role-error");
      el.textContent = e.message;
      el.classList.add("is-visible");
    })
    .finally(function () {
      document.getElementById("er-submit").disabled = false;
    });
}

// User roles modal (launched from Users tab)
function showUserRolesModal(userId) {
  _urTriggerEl = document.activeElement;
  const ov = document.getElementById("user-roles-overlay");
  ov.style.display = "flex";
  document.getElementById("ur-user-id").value = userId;
  const container = document.getElementById("ur-roles-container");
  setSafeHtml(container, '<div class="dashboard-empty">Loading...</div>');
  _urTrapHandler = _installTrap("user-roles-overlay", "user-roles-box");
  // Fetch all roles and user's current roles
  Promise.all([
    authFetch("/v1/api/admin/roles").then(function (r) {
      return r.json();
    }),
    authFetch("/v1/api/admin/users/" + userId + "/roles").then(function (r) {
      return r.json();
    }),
  ])
    .then(function (results) {
      const allRoles = results[0].roles || [];
      const userRoles = results[1].roles || [];
      const assigned = {};
      for (let i = 0; i < userRoles.length; i++)
        assigned[userRoles[i].role_id] = true;
      // Role-assignment rows reuse the toggle-switch component for
      // consistency with the rest of the admin UX.  Role display names
      // are human-readable text, so no monospace override is needed.
      let html = '<div class="user-roles-list">';
      for (let j = 0; j < allRoles.length; j++) {
        const r = allRoles[j];
        const checked = assigned[r.role_id] ? " checked" : "";
        html +=
          '<label class="toggle-switch user-role-toggle">' +
          '<input type="checkbox" value="' +
          escapeHtml(r.role_id) +
          '" name="ur-role"' +
          checked +
          ">" +
          '<span class="toggle-track" aria-hidden="true"></span>' +
          '<span class="toggle-label">' +
          escapeHtml(r.display_name) +
          "</span></label>";
      }
      html += "</div>";
      setSafeHtml(container, html);
    })
    .catch(function () {
      setSafeHtml(
        container,
        '<div class="dashboard-empty">Failed to load roles</div>',
      );
    });
}

function hideUserRolesModal() {
  document.getElementById("user-roles-overlay").style.display = "none";
  _urTrapHandler = _removeTrap(_urTrapHandler);
  if (_urTriggerEl && _urTriggerEl.focus) {
    _urTriggerEl.focus();
  }
  _urTriggerEl = null;
}

function submitUserRoles() {
  const userId = document.getElementById("ur-user-id").value;
  const boxes = document.querySelectorAll('input[name="ur-role"]');
  const selected = [];
  for (let i = 0; i < boxes.length; i++) {
    if (boxes[i].checked) selected.push(boxes[i].value);
  }
  // Get current user roles to diff
  authFetch("/v1/api/admin/users/" + userId + "/roles")
    .then(function (r) {
      return r.json();
    })
    .then(function (data) {
      const current = {};
      const roles = data.roles || [];
      for (let i = 0; i < roles.length; i++) current[roles[i].role_id] = true;
      const promises = [];
      // Assign new
      for (let j = 0; j < selected.length; j++) {
        if (!current[selected[j]]) {
          promises.push(
            authFetch("/v1/api/admin/users/" + userId + "/roles", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ role_id: selected[j] }),
            }),
          );
        }
      }
      // Unassign removed
      const selMap = {};
      for (let k = 0; k < selected.length; k++) selMap[selected[k]] = true;
      for (let rid in current) {
        if (!selMap[rid]) {
          promises.push(
            authFetch("/v1/api/admin/users/" + userId + "/roles/" + rid, {
              method: "DELETE",
            }),
          );
        }
      }
      return Promise.all(promises);
    })
    .then(function () {
      hideUserRolesModal();
      showToast("Roles updated");
    })
    .catch(function () {
      showToast("Failed to update roles");
    });
}

// ---------------------------------------------------------------------------
// Tool Policies
// ---------------------------------------------------------------------------

function loadGovPolicies() {
  authFetch("/v1/api/admin/policies")
    .then(function (r) {
      if (!r.ok) throw new Error("Failed");
      return r.json();
    })
    .then(function (data) {
      _govPolicies = data.policies || [];
      _renderGovPolicies(_govPolicies);
    })
    .catch(function () {
      setSafeHtml(
        document.getElementById("admin-policies-table"),
        '<div class="dashboard-empty">Failed to load policies</div>',
      );
    });
}

function _renderGovPolicies(items) {
  const el = document.getElementById("admin-policies-table");
  if (!items.length) {
    setSafeHtml(
      el,
      '<div class="dashboard-empty">No tool policies defined</div>',
    );
    return;
  }
  let html = "";
  for (let i = 0; i < items.length; i++) {
    const p = items[i];
    const actionCls = "policy-badge policy-" + p.action;
    const statusDot = p.enabled
      ? '<span class="watch-active" title="Enabled">\u25CF active</span>'
      : '<span class="watch-completed" title="Disabled">\u25CB disabled</span>';
    html +=
      '<div class="admin-row" role="listitem">' +
      '<span class="admin-col admin-col-pname">' +
      escapeHtml(p.name) +
      "</span>" +
      '<span class="admin-col admin-col-ppattern"><code>' +
      escapeHtml(p.tool_pattern) +
      "</code></span>" +
      '<span class="admin-col admin-col-paction"><span class="' +
      actionCls +
      '">' +
      escapeHtml(p.action) +
      "</span></span>" +
      '<span class="admin-col admin-col-ppriority">' +
      p.priority +
      "</span>" +
      '<span class="admin-col admin-col-pstatus">' +
      statusDot +
      "</span>" +
      '<span class="admin-col admin-col-actions">' +
      '<button class="admin-btn-action" data-edit-policy="' +
      escapeHtml(p.policy_id) +
      '">edit</button>' +
      '<button class="admin-btn-danger" data-delete-policy="' +
      escapeHtml(p.policy_id) +
      '" data-policy-name="' +
      escapeHtml(p.name) +
      '">delete</button>' +
      "</span></div>";
  }
  setSafeHtml(el, html);
  el.querySelectorAll("[data-edit-policy]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      showEditPolicyModal(this.getAttribute("data-edit-policy"));
    });
  });
  el.querySelectorAll("[data-delete-policy]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      const pid = this.getAttribute("data-delete-policy");
      const pname = this.getAttribute("data-policy-name");
      showConfirmModal(
        "Delete Policy",
        'Delete policy "' + pname + '"?',
        "Delete",
        function () {
          authFetch("/v1/api/admin/policies/" + pid, { method: "DELETE" })
            .then(function (r) {
              if (!r.ok) throw new Error();
              return r.json();
            })
            .then(function () {
              showToast("Policy deleted");
              loadGovPolicies();
            })
            .catch(function () {
              showToast("Failed to delete policy");
            });
        },
      );
    });
  });
}

function showCreatePolicyModal() {
  _cpTriggerEl = document.activeElement;
  const ov = document.getElementById("create-policy-overlay");
  ov.style.display = "flex";
  document.getElementById("cp-name").value = "";
  document.getElementById("cp-pattern").value = "";
  document.getElementById("cp-action").value = "ask";
  document.getElementById("cp-priority").value = "0";
  document.getElementById("create-policy-error").classList.remove("is-visible");
  document.getElementById("cp-name").focus();
  _cpTrapHandler = _installTrap("create-policy-overlay", "create-policy-box");
}

function hideCreatePolicyModal() {
  document.getElementById("create-policy-overlay").style.display = "none";
  _cpTrapHandler = _removeTrap(_cpTrapHandler);
  if (_cpTriggerEl && _cpTriggerEl.focus) {
    _cpTriggerEl.focus();
  }
  _cpTriggerEl = null;
}

function submitCreatePolicy() {
  const name = document.getElementById("cp-name").value.trim();
  const pattern = document.getElementById("cp-pattern").value.trim();
  const action = document.getElementById("cp-action").value;
  const priority =
    parseInt(document.getElementById("cp-priority").value, 10) || 0;
  if (!name || !pattern) {
    const e = document.getElementById("create-policy-error");
    e.textContent = "Name and pattern are required";
    e.classList.add("is-visible");
    return;
  }
  document.getElementById("cp-submit").disabled = true;
  authFetch("/v1/api/admin/policies", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      name: name,
      tool_pattern: pattern,
      action: action,
      priority: priority,
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
      hideCreatePolicyModal();
      showToast("Policy created");
      loadGovPolicies();
    })
    .catch(function (e) {
      const el = document.getElementById("create-policy-error");
      el.textContent = e.message;
      el.classList.add("is-visible");
    })
    .finally(function () {
      document.getElementById("cp-submit").disabled = false;
    });
}

function showEditPolicyModal(policyId) {
  _epTriggerEl = document.activeElement;
  let policy = null;
  for (let i = 0; i < _govPolicies.length; i++) {
    if (_govPolicies[i].policy_id === policyId) {
      policy = _govPolicies[i];
      break;
    }
  }
  if (!policy) return;
  const ov = document.getElementById("edit-policy-overlay");
  ov.style.display = "flex";
  document.getElementById("ep-id").value = policyId;
  document.getElementById("ep-name").value = policy.name;
  document.getElementById("ep-pattern").value = policy.tool_pattern;
  document.getElementById("ep-action").value = policy.action;
  document.getElementById("ep-priority").value = policy.priority;
  document.getElementById("ep-enabled").checked = policy.enabled;
  document.getElementById("edit-policy-error").classList.remove("is-visible");
  _epTrapHandler = _installTrap("edit-policy-overlay", "edit-policy-box");
}

function hideEditPolicyModal() {
  document.getElementById("edit-policy-overlay").style.display = "none";
  _epTrapHandler = _removeTrap(_epTrapHandler);
  if (_epTriggerEl && _epTriggerEl.focus) {
    _epTriggerEl.focus();
  }
  _epTriggerEl = null;
}

function submitEditPolicy() {
  const id = document.getElementById("ep-id").value;
  document.getElementById("ep-submit").disabled = true;
  authFetch("/v1/api/admin/policies/" + id, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      name: document.getElementById("ep-name").value.trim(),
      tool_pattern: document.getElementById("ep-pattern").value.trim(),
      action: document.getElementById("ep-action").value,
      priority: parseInt(document.getElementById("ep-priority").value, 10) || 0,
      enabled: document.getElementById("ep-enabled").checked,
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
      hideEditPolicyModal();
      showToast("Policy updated");
      loadGovPolicies();
    })
    .catch(function (e) {
      const el = document.getElementById("edit-policy-error");
      el.textContent = e.message;
      el.classList.add("is-visible");
    })
    .finally(function () {
      document.getElementById("ep-submit").disabled = false;
    });
}

// ---------------------------------------------------------------------------
// Skills (prompt templates)
// ---------------------------------------------------------------------------

function loadGovSkills() {
  return authFetch("/v1/api/admin/skills")
    .then(function (r) {
      if (!r.ok) throw new Error("Failed");
      return r.json();
    })
    .then(function (data) {
      _govSkills = data.skills || [];
      _renderGovSkills(_govSkills);
    })
    .catch(function () {
      setSafeHtml(
        document.getElementById("admin-skills-table"),
        '<div class="dashboard-empty">Failed to load skills</div>',
      );
    });
}

function _renderGovSkills(items) {
  const el = document.getElementById("admin-skills-table");
  if (!items.length) {
    setSafeHtml(el, '<div class="dashboard-empty">No skills configured</div>');
    return;
  }
  let html = "";
  for (let i = 0; i < items.length; i++) {
    const t = items[i];
    let activationBadge = "";
    const activation = t.activation || "named";
    if (activation === "default") {
      activationBadge =
        '<span class="scope-badge scope-approve">default</span>';
    } else if (activation === "search") {
      activationBadge = '<span class="scope-badge">search</span>';
    }
    const defBadge =
      t.is_default && activation !== "default"
        ? '<span class="scope-badge scope-approve">default</span>'
        : "";
    const originBadge =
      t.origin === "mcp"
        ? ' <span class="scope-badge scope-mcp">mcp:' +
          escapeHtml(t.mcp_server) +
          "</span>"
        : "";
    const catBadge =
      '<span class="scope-badge">' + escapeHtml(t.category) + "</span>";
    // Build risk column content with tooltip
    let riskCell = "";
    if (t.risk_level) {
      const scanClass =
        {
          safe: "scope-scan-safe",
          low: "scope-scan-low",
          medium: "scope-scan-medium",
          high: "scope-scan-high",
          critical: "scope-scan-critical",
        }[t.risk_level] || "";
      const scanIcon =
        {
          safe: "\u2713 ",
          low: "",
          medium: "\u25B2 ",
          high: "\u25C6 ",
          critical: "\u26A0 ",
        }[t.risk_level] || "";
      const tipParts = [];
      try {
        const report = JSON.parse(t.scan_report || "{}");
        if (report.composite != null) {
          tipParts.push("Score: " + report.composite.toFixed(2));
        }
        const axes = ["content", "supply_chain", "vulnerability", "capability"];
        for (let ai = 0; ai < axes.length; ai++) {
          const d = (report.details || {})[axes[ai]] || {};
          if (d.flags && d.flags.length) {
            tipParts.push(
              axes[ai].replace(/_/g, " ") + ": " + d.flags.join(", "),
            );
          }
        }
      } catch (e) {}
      const tipText = tipParts.length ? tipParts.join("\n") : t.risk_level;
      riskCell =
        '<span class="scope-badge ' +
        scanClass +
        '" tabindex="0" role="button" aria-label="Risk: ' +
        escapeHtml(t.risk_level) +
        (tipParts.length ? ". " + escapeHtml(tipParts.join(". ")) : "") +
        '" title="' +
        escapeHtml(tipText) +
        '">' +
        escapeHtml(scanIcon + t.risk_level) +
        "</span>";
    } else {
      riskCell =
        '<span class="scope-badge" style="opacity:0.4" title="Not scanned">\u2014</span>';
    }
    let resBadge = "";
    if (t.resource_count > 0) {
      resBadge =
        ' <span class="scope-badge" title="' +
        t.resource_count +
        ' bundled resource(s)">' +
        t.resource_count +
        " res</span>";
    }
    const editLabel = t.readonly ? "view" : "edit";
    const deleteDisabled = "";
    html +=
      '<div class="admin-row" role="listitem">' +
      '<span class="admin-col admin-col-tmcat">' +
      catBadge +
      "</span>" +
      '<span class="admin-col admin-col-tmname">' +
      escapeHtml(t.name) +
      " " +
      activationBadge +
      defBadge +
      originBadge +
      resBadge +
      (t.description
        ? '<br><span class="admin-col-subtitle">' +
          escapeHtml(t.description) +
          "</span>"
        : "") +
      "</span>" +
      '<span class="admin-col admin-col-tmrisk">' +
      riskCell +
      "</span>" +
      '<span class="admin-col admin-col-actions">' +
      '<button class="admin-btn-action" data-edit-tmpl="' +
      escapeHtml(t.template_id) +
      '">' +
      editLabel +
      "</button>" +
      '<button class="admin-btn-danger" data-delete-tmpl="' +
      escapeHtml(t.template_id) +
      '" data-tmpl-name="' +
      escapeHtml(t.name) +
      '"' +
      deleteDisabled +
      ">delete</button>" +
      "</span></div>";
  }
  setSafeHtml(el, html);
  el.querySelectorAll("[data-edit-tmpl]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      showEditTemplateModal(this.getAttribute("data-edit-tmpl"));
    });
  });
  el.querySelectorAll("[data-delete-tmpl]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      const tid = this.getAttribute("data-delete-tmpl");
      const tname = this.getAttribute("data-tmpl-name");
      showConfirmModal(
        "Delete Skill",
        'Delete skill "' + tname + '"?',
        "Delete",
        function () {
          authFetch("/v1/api/admin/skills/" + tid, { method: "DELETE" })
            .then(function (r) {
              if (!r.ok) throw new Error();
              return r.json();
            })
            .then(function () {
              showToast("Skill deleted");
              loadGovSkills();
            })
            .catch(function () {
              showToast("Failed to delete skill");
            });
        },
      );
    });
  });
}

// ---------------------------------------------------------------------------
// SKILL.md paste auto-fill — sniff frontmatter on paste, hit the parse
// endpoint, and populate the form so users don't have to retype name /
// description / tags / etc. when importing a SKILL.md-style skill.
// ---------------------------------------------------------------------------

// Trigger on any paste whose first non-whitespace bytes look like an opening
// YAML frontmatter delimiter — restrictive enough to ignore normal markdown
// pastes, permissive enough to catch CRLF and trailing-space variants.
const _SKILL_FRONTMATTER_RE = /^---\s*\r?\n/;

const _SKILL_FIELD_MAP = {
  name: "ctm-name",
  description: "skill-description",
  tags: "skill-tags",
  author: "skill-author",
  version: "skill-version",
  license: "skill-license",
  compatibility: "skill-compatibility",
  allowed_tools: "csk-allowed-tools",
  paths: "skill-paths",
  arguments: "skill-arguments",
  argument_hint: "skill-argument-hint",
};

// Inflight paste-parse fetch — referenced from hideCreateTemplateModal so a
// modal close cancels the request, and from _handleSkillContentPaste so a
// fresh paste supersedes the previous one.  Acts as a generation token: any
// callback that observes _ctmPasteController != its captured controller knows
// the modal moved on and must not touch the DOM.
let _ctmPasteController = null;

// Returns "filled" if we set the value, "skipped" if the field was already
// non-empty (we don't clobber user input), or "absent" if we couldn't find or
// match the option.  Tracking this lets the caller report what actually
// happened so the user knows whether their pre-typed values survived.
function _setSkillFormField(id, value) {
  const el = document.getElementById(id);
  if (!el) return "absent";
  if (el.value && String(el.value).trim()) return "skipped";
  if (el.tagName === "SELECT") {
    // License is a fixed option list — only set the value if it matches an
    // option.  Custom licenses fall through to the default "— not specified —"
    // and the user can edit manually.
    for (let i = 0; i < el.options.length; i++) {
      if (el.options[i].value === value) {
        el.value = value;
        return "filled";
      }
    }
    return "absent";
  }
  el.value = value;
  return "filled";
}

function _applyParsedSkill(parsed, contentTextarea, fieldMap) {
  // The textarea is the explicit paste target — replacing its full content
  // matches the user's mental model ("I pasted a SKILL.md, the body should
  // become the content").  Side metadata fields use the non-destructive
  // _setSkillFormField rule below so half-typed values aren't lost.
  contentTextarea.value = parsed.content || "";
  contentTextarea.dispatchEvent(new Event("input", { bubbles: true }));

  let filled = 0;
  let skipped = 0;
  function _apply(id, value) {
    const outcome = _setSkillFormField(id, value);
    if (outcome === "filled") filled++;
    else if (outcome === "skipped") skipped++;
  }

  if (parsed.name) _apply(fieldMap.name, parsed.name);
  if (parsed.description) _apply(fieldMap.description, parsed.description);
  if (parsed.tags && parsed.tags.length)
    _apply(fieldMap.tags, parsed.tags.join(", "));
  if (parsed.author) _apply(fieldMap.author, parsed.author);
  if (parsed.version) _apply(fieldMap.version, parsed.version);
  if (parsed.license) _apply(fieldMap.license, parsed.license);
  if (parsed.compatibility)
    _apply(fieldMap.compatibility, parsed.compatibility);
  if (parsed.allowed_tools && parsed.allowed_tools.length)
    _apply(fieldMap.allowed_tools, parsed.allowed_tools.join(", "));
  if (parsed.paths && parsed.paths.length)
    _apply(fieldMap.paths, parsed.paths.join(", "));
  // ``user-invocable: false`` from the parsed SKILL.md flips the
  // hidden-from-menu checkbox.  Boolean coercion — the parse response
  // can return either ``true``/``false`` or omit the field.  Mirrors
  // the apply rule for other fields: only fill, never override an
  // existing user choice (a checkbox the admin already ticked stays
  // ticked even if the parsed value disagrees).
  if (parsed.user_invocable === false) {
    const hidden = document.getElementById("skill-hidden-from-menu");
    if (hidden && !hidden.checked) {
      hidden.checked = true;
      filled++;
    }
  }
  if (parsed.arguments && parsed.arguments.length)
    _apply(fieldMap.arguments, parsed.arguments.join(", "));
  if (parsed.argument_hint)
    _apply(fieldMap.argument_hint, parsed.argument_hint);

  return { filled: filled, skipped: skipped };
}

function _setSkillPasteHintBusy(busy) {
  const hint = document.getElementById("ctm-paste-hint");
  if (!hint) return;
  const rest = hint.querySelector(".skill-paste-hint-rest");
  const busyEl = hint.querySelector(".skill-paste-hint-busy");
  if (rest) rest.style.display = busy ? "none" : "";
  if (busyEl) busyEl.style.display = busy ? "" : "none";
}

function _handleSkillContentPaste(event, fieldMap) {
  const clipboard = event.clipboardData || window.clipboardData;
  if (!clipboard) return;
  const text = clipboard.getData("text/plain");
  if (!text || !_SKILL_FRONTMATTER_RE.test(text)) return;

  event.preventDefault();
  const textarea = event.target;

  // Cancel any prior paste fetch — a fresh paste supersedes whatever was in
  // flight.  The previous handler's callbacks see _ctmPasteController !=
  // their captured controller and bail before touching the DOM.
  if (_ctmPasteController) _ctmPasteController.abort();
  const controller = new AbortController();
  _ctmPasteController = controller;

  // Optimistic paint — drop the raw text into the textarea immediately so the
  // user sees their paste landed, then disable the field and flip the hint
  // line into a "Parsing..." state.  On a slow network the round-trip can
  // stretch past 400ms; without a visible state the user thinks nothing
  // happened and re-pastes (or hits Create with empty fields).
  textarea.value = text;
  textarea.disabled = true;
  textarea.setAttribute("aria-busy", "true");
  textarea.dispatchEvent(new Event("input", { bubbles: true }));
  _setSkillPasteHintBusy(true);

  function _isCurrent() {
    return _ctmPasteController === controller;
  }

  authFetch("/v1/api/admin/skills/parse", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ raw: text }),
    signal: controller.signal,
  })
    .then(function (r) {
      return r.json().then(function (d) {
        return { ok: r.ok, data: d };
      });
    })
    .then(function (res) {
      if (!_isCurrent()) return;
      if (res.ok) {
        const counts = _applyParsedSkill(res.data, textarea, fieldMap);
        let msg = "Populated from SKILL.md";
        if (counts.skipped) {
          msg += " (" + counts.filled + " set, " + counts.skipped + " kept)";
        }
        showToast(msg);
      } else {
        // Frontmatter looked plausible but the parser rejected it (missing
        // name, malformed YAML beyond the retry, etc).  The raw text is
        // already in the textarea from the optimistic paint above so the
        // user can fix the YAML in place.
        showToast(
          "Couldn't parse SKILL.md: " +
            ((res.data && res.data.error) || "unknown error"),
          "error",
        );
      }
    })
    .catch(function (err) {
      if (!_isCurrent()) return;
      // AbortError fires when the modal closed or a fresher paste superseded
      // this one — silent, the new lifecycle owns the UI.
      if (err && err.name === "AbortError") return;
      showToast("Network error — pasted as plain text", "error");
    })
    .finally(function () {
      if (!_isCurrent()) return;
      _ctmPasteController = null;
      textarea.disabled = false;
      textarea.removeAttribute("aria-busy");
      _setSkillPasteHintBusy(false);
      textarea.focus();
    });
}

function _detectTemplateVars(content) {
  const matches = content.match(/\{\{(\w+)\}\}/g) || [];
  const seen = {};
  const result = [];
  for (let i = 0; i < matches.length; i++) {
    const v = matches[i].replace(/[{}]/g, "");
    if (!seen[v]) {
      seen[v] = true;
      result.push(v);
    }
  }
  return result;
}

function _updateVarsDisplay(contentId, displayId) {
  const content = document.getElementById(contentId).value || "";
  const vars = _detectTemplateVars(content);
  document.getElementById(displayId).textContent = vars.length
    ? vars.join(", ")
    : "(none)";
}

function showCreateTemplateModal() {
  _ctmTriggerEl = document.activeElement;
  const ov = document.getElementById("create-template-overlay");
  ov.style.display = "flex";
  document.getElementById("ctm-name").value = "";
  document.getElementById("ctm-category").value = "general";
  document.getElementById("skill-description").value = "";
  document.getElementById("skill-tags").value = "";
  document.getElementById("skill-author").value = "";
  document.getElementById("skill-version").value = "";
  document.getElementById("skill-license").value = "";
  document.getElementById("skill-compatibility").value = "";
  document.getElementById("skill-paths").value = "";
  document.getElementById("skill-hidden-from-menu").checked = false;
  document.getElementById("skill-arguments").value = "";
  document.getElementById("skill-argument-hint").value = "";
  document.getElementById("skill-activation").value = "named";
  const ctmContent = document.getElementById("ctm-content");
  ctmContent.value = "";
  document.getElementById("ctm-variables").textContent = "(none)";
  ctmContent.oninput = function () {
    _updateVarsDisplay("ctm-content", "ctm-variables");
  };
  ctmContent.onpaste = function (event) {
    _handleSkillContentPaste(event, _SKILL_FIELD_MAP);
  };
  document.getElementById("ctm-default").checked = false;
  // Session config fields
  document.getElementById("csk-model").value = "";
  document.getElementById("csk-temperature").value = "";
  document.getElementById("csk-reasoning-effort").value = "";
  document.getElementById("csk-max-tokens").value = "";
  document.getElementById("csk-token-budget").value = "";
  document.getElementById("csk-agent-max-turns").value = "";
  document.getElementById("csk-auto-approve").checked = false;
  document.getElementById("csk-allowed-tools").value = "";
  document.getElementById("csk-allowed-tools").disabled = false;
  document.getElementById("csk-notify-on-complete").value = "";
  document.getElementById("csk-enabled").checked = true;
  document.getElementById("csk-auto-approve").onchange = function () {
    document.getElementById("csk-allowed-tools").disabled = this.checked;
  };
  document
    .getElementById("create-template-error")
    .classList.remove("is-visible");
  // Clear resource list
  _pendingResources = [];
  _renderPendingResources();
  document.getElementById("ctm-name").focus();
  _ctmTrapHandler = _installTrap(
    "create-template-overlay",
    "create-template-box",
  );
}

function hideCreateTemplateModal() {
  // Cancel any inflight paste-parse so a late response can't reach into a
  // closed (or freshly reopened) modal and clobber state.  AbortController
  // also short-circuits the .then chain — see _handleSkillContentPaste.
  // After abort, the handler's .catch/.finally bail via _isCurrent() before
  // resetting the textarea, so we proactively restore the paste-induced
  // visible state here.  Otherwise reopening would land on a disabled
  // textarea stuck on "Parsing…".
  if (_ctmPasteController) {
    _ctmPasteController.abort();
    _ctmPasteController = null;
    const ctmContent = document.getElementById("ctm-content");
    if (ctmContent) {
      ctmContent.disabled = false;
      ctmContent.removeAttribute("aria-busy");
    }
    _setSkillPasteHintBusy(false);
  }
  document.getElementById("create-template-overlay").style.display = "none";
  _ctmTrapHandler = _removeTrap(_ctmTrapHandler);
  if (_ctmTriggerEl && _ctmTriggerEl.focus) {
    _ctmTriggerEl.focus();
  }
  _ctmTriggerEl = null;
}

function submitCreateTemplate() {
  // Clear any prior error before re-validating — a successful submit shouldn't
  // leave stale red text on-screen, and the in-flight PUT period shouldn't
  // either. Cheaper than reasoning about every catch path remembering to
  // clear on success.
  const prevErr = document.getElementById("create-template-error");
  if (prevErr) {
    prevErr.classList.remove("is-visible");
    prevErr.textContent = "";
  }
  const name = document.getElementById("ctm-name").value.trim();
  const content = document.getElementById("ctm-content").value;
  if (!name || !content) {
    const e = document.getElementById("create-template-error");
    e.textContent = "Name and content are required";
    e.classList.add("is-visible");
    return;
  }
  const varList = _detectTemplateVars(content);
  const tagsRaw = (document.getElementById("skill-tags").value || "").trim();
  const tagsArray = tagsRaw
    ? tagsRaw
        .split(",")
        .map(function (t) {
          return t.trim();
        })
        .filter(Boolean)
    : [];
  // Session config fields
  const csTemp = document.getElementById("csk-temperature").value.trim();
  const csMaxTok = document.getElementById("csk-max-tokens").value.trim();
  const csBudget = document.getElementById("csk-token-budget").value.trim();
  const csMaxTurns = document
    .getElementById("csk-agent-max-turns")
    .value.trim();
  const csAllowed = (
    document.getElementById("csk-allowed-tools").value || ""
  ).trim();
  const csAllowedArr = csAllowed
    ? csAllowed
        .split(",")
        .map(function (t) {
          return t.trim();
        })
        .filter(Boolean)
    : [];
  const csNotifyRaw = (
    document.getElementById("csk-notify-on-complete").value || ""
  ).trim();
  let csNotifyVal = "[]";
  if (csNotifyRaw) {
    try {
      const csNotifyParsed = JSON.parse(csNotifyRaw);
      if (!Array.isArray(csNotifyParsed))
        throw new Error("must be a JSON array");
      csNotifyVal = JSON.stringify(csNotifyParsed);
    } catch (ne) {
      const ne2 = document.getElementById("create-template-error");
      ne2.textContent = "Notify on completion: " + ne.message;
      ne2.classList.add("is-visible");
      return;
    }
  }
  document.getElementById("ctm-submit").disabled = true;
  const csVersion = (
    document.getElementById("skill-version").value || ""
  ).trim();
  const csPathsArr = (document.getElementById("skill-paths").value || "")
    .split(",")
    .map(function (p) {
      return p.trim();
    })
    .filter(Boolean);
  const csArgsArr = (document.getElementById("skill-arguments").value || "")
    .split(",")
    .map(function (a) {
      return a.trim();
    })
    .filter(Boolean);
  const csArgumentHint = (
    document.getElementById("skill-argument-hint").value || ""
  ).trim();
  const createBody = {
    name: name,
    category: document.getElementById("ctm-category").value,
    description: (
      document.getElementById("skill-description").value || ""
    ).trim(),
    tags: JSON.stringify(tagsArray),
    author: (document.getElementById("skill-author").value || "").trim(),
    license: (document.getElementById("skill-license").value || "").trim(),
    compatibility: (
      document.getElementById("skill-compatibility").value || ""
    ).trim(),
    activation: document.getElementById("skill-activation").value,
    content: content,
    variables: JSON.stringify(varList),
    is_default: document.getElementById("ctm-default").checked,
    model: document.getElementById("csk-model").value.trim(),
    auto_approve: document.getElementById("csk-auto-approve").checked,
    temperature: csTemp ? parseFloat(csTemp) : null,
    reasoning_effort: document.getElementById("csk-reasoning-effort").value,
    max_tokens: csMaxTok ? parseInt(csMaxTok, 10) : null,
    token_budget: csBudget ? parseInt(csBudget, 10) : 0,
    agent_max_turns: csMaxTurns ? parseInt(csMaxTurns, 10) : null,
    allowed_tools: JSON.stringify(csAllowedArr),
    paths: JSON.stringify(csPathsArr),
    hidden_from_menu: document.getElementById("skill-hidden-from-menu").checked,
    arguments: JSON.stringify(csArgsArr),
    argument_hint: csArgumentHint,
    notify_on_complete: csNotifyVal,
    enabled: document.getElementById("csk-enabled").checked,
  };
  if (csVersion) createBody.version = csVersion;
  authFetch("/v1/api/admin/skills", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(createBody),
  })
    .then(function (r) {
      if (!r.ok)
        return r.json().then(function (d) {
          throw new Error(d.error || "Failed");
        });
      return r.json();
    })
    .then(function (data) {
      if (_pendingResources.length && data && data.template_id) {
        const promises = _pendingResources.map(function (res) {
          return authFetch(
            "/v1/api/admin/skills/" + data.template_id + "/resources",
            {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify(res),
            },
          ).then(function (r) {
            if (!r.ok) throw new Error("Upload failed for " + res.path);
            return r.json();
          });
        });
        Promise.all(promises)
          .then(function () {
            hideCreateTemplateModal();
            showToast(
              "Skill created with " + _pendingResources.length + " resource(s)",
            );
            loadGovSkills();
          })
          .catch(function () {
            hideCreateTemplateModal();
            showToast("Skill created (some resources failed)");
            loadGovSkills();
          });
      } else {
        hideCreateTemplateModal();
        showToast("Skill created");
        loadGovSkills();
      }
    })
    .catch(function (e) {
      const el = document.getElementById("create-template-error");
      el.textContent = e.message;
      el.classList.add("is-visible");
    })
    .finally(function () {
      document.getElementById("ctm-submit").disabled = false;
    });
}

function showEditTemplateModal(tmplId) {
  const ov = document.getElementById("edit-template-overlay");
  // When called against an already-open modal (e.g. mutate-in-place after
  // unlock), preserve the original trigger so focus restores to the row
  // launcher on close, and don't reinstall the focus trap.
  const alreadyOpen = ov && ov.style.display === "flex";
  if (!alreadyOpen) {
    _etmTriggerEl = document.activeElement;
  }
  let tmpl = null;
  for (let i = 0; i < _govSkills.length; i++) {
    if (_govSkills[i].template_id === tmplId) {
      tmpl = _govSkills[i];
      break;
    }
  }
  if (!tmpl) return;
  ov.style.display = "flex";
  document.getElementById("etm-id").value = tmplId;
  document.getElementById("etm-name").value = tmpl.name;
  document.getElementById("etm-category").value = tmpl.category;
  document.getElementById("etm-description").value = tmpl.description || "";
  // Parse tags from JSON array to comma-separated display
  let tagsDisplay = "";
  try {
    const tagsList = JSON.parse(tmpl.tags || "[]");
    tagsDisplay = tagsList.join(", ");
  } catch (e) {
    tagsDisplay = tmpl.tags || "";
  }
  document.getElementById("etm-tags").value = tagsDisplay;
  document.getElementById("etm-author").value = tmpl.author || "";
  document.getElementById("etm-version").value = tmpl.version || "";
  document.getElementById("etm-license").value = tmpl.license || "";
  document.getElementById("etm-compatibility").value = tmpl.compatibility || "";
  let pathsDisplay = "";
  try {
    const pathsList = JSON.parse(tmpl.paths || "[]");
    pathsDisplay = pathsList.join(", ");
  } catch (e) {
    pathsDisplay = tmpl.paths || "";
  }
  document.getElementById("etm-paths").value = pathsDisplay;
  document.getElementById("etm-hidden-from-menu").checked = Boolean(
    tmpl.hidden_from_menu,
  );
  let argumentsDisplay = "";
  try {
    const argumentsList = JSON.parse(tmpl.arguments || "[]");
    argumentsDisplay = argumentsList.join(", ");
  } catch (e) {
    argumentsDisplay = tmpl.arguments || "";
  }
  document.getElementById("etm-arguments").value = argumentsDisplay;
  document.getElementById("etm-argument-hint").value = tmpl.argument_hint || "";
  document.getElementById("etm-activation").value = tmpl.activation || "named";
  document.getElementById("etm-content").value = tmpl.content;
  _updateVarsDisplay("etm-content", "etm-variables");
  document.getElementById("etm-content").oninput = function () {
    _updateVarsDisplay("etm-content", "etm-variables");
  };
  document.getElementById("etm-default").checked = tmpl.is_default;
  // Session config fields
  document.getElementById("esk-model").value = tmpl.model || "";
  document.getElementById("esk-temperature").value =
    tmpl.temperature != null ? tmpl.temperature : "";
  document.getElementById("esk-reasoning-effort").value =
    tmpl.reasoning_effort || "";
  document.getElementById("esk-max-tokens").value =
    tmpl.max_tokens != null ? tmpl.max_tokens : "";
  document.getElementById("esk-token-budget").value = tmpl.token_budget
    ? tmpl.token_budget
    : "";
  document.getElementById("esk-agent-max-turns").value =
    tmpl.agent_max_turns != null ? tmpl.agent_max_turns : "";
  document.getElementById("esk-auto-approve").checked =
    tmpl.auto_approve || false;
  // allowed_tools: parse JSON array to comma-separated display
  let allowedDisplay = "";
  try {
    const allowed = JSON.parse(tmpl.allowed_tools || "[]");
    allowedDisplay = allowed.join(", ");
  } catch (e) {
    allowedDisplay = tmpl.allowed_tools || "";
  }
  document.getElementById("esk-allowed-tools").value = allowedDisplay;
  document.getElementById("esk-allowed-tools").disabled =
    tmpl.auto_approve || false;
  document.getElementById("esk-enabled").checked = tmpl.enabled !== false;
  const notifyVal = tmpl.notify_on_complete || "[]";
  document.getElementById("esk-notify-on-complete").value =
    notifyVal && notifyVal !== "[]" ? notifyVal : "";
  document.getElementById("esk-auto-approve").onchange = function () {
    document.getElementById("esk-allowed-tools").disabled = this.checked;
  };
  // The CSS contract for .admin-modal [role="alert"] is hide-by-default,
  // .is-visible to show — so toggling style.display does nothing here.
  document.getElementById("edit-template-error").classList.remove("is-visible");
  // Scan report section
  const scanSection = document.getElementById("etm-scan-section");
  if (scanSection) {
    if (tmpl.risk_level) {
      scanSection.style.display = "";
      const scanClassMap = {
        safe: "scope-scan-safe",
        low: "scope-scan-low",
        medium: "scope-scan-medium",
        high: "scope-scan-high",
        critical: "scope-scan-critical",
      };
      let report = {};
      try {
        report = JSON.parse(tmpl.scan_report || "{}");
      } catch (e) {}
      let scanHtml =
        '<span class="scope-badge ' +
        (scanClassMap[tmpl.risk_level] || "") +
        '">' +
        escapeHtml(tmpl.risk_level) +
        "</span>";
      if (report.composite != null) {
        scanHtml +=
          ' <span class="scan-composite">Score: ' +
          report.composite.toFixed(2) +
          "</span>";
      }
      if (tmpl.scan_version) {
        scanHtml +=
          ' <span class="scan-version">v' +
          escapeHtml(tmpl.scan_version) +
          "</span>";
      }
      const axes = ["content", "supply_chain", "vulnerability", "capability"];
      for (let ai = 0; ai < axes.length; ai++) {
        const axis = axes[ai];
        const d = (report.details || {})[axis] || {};
        scanHtml +=
          '<div class="scan-axis"><span class="scan-axis-name">' +
          escapeHtml(axis.replace(/_/g, " ")) +
          '</span> <span class="scan-axis-score">' +
          (d.score != null ? d.score.toFixed(1) : "0.0") +
          "/4.0</span>";
        if (d.flags && d.flags.length) {
          scanHtml +=
            ' <span class="scan-axis-flags">' +
            d.flags.map(escapeHtml).join(", ") +
            "</span>";
        }
        scanHtml += "</div>";
      }
      setSafeHtml(document.getElementById("etm-scan-report"), scanHtml);
    } else {
      scanSection.style.display = "none";
    }
  }
  const rescanBtn = document.getElementById("etm-rescan-btn");
  if (rescanBtn) {
    rescanBtn.onclick = function () {
      rescanBtn.disabled = true;
      rescanBtn.textContent = "Scanning...";
      authFetch("/v1/api/admin/skills/" + tmplId + "/rescan", {
        method: "POST",
      })
        .then(function (r) {
          if (!r.ok) throw new Error("Failed");
          return r.json();
        })
        .then(function (data) {
          showToast("Scan complete: " + (data.risk_level || "unknown"));
          // Refresh the modal by re-loading skills and re-opening
          loadGovSkills();
          // Update current tmpl in memory
          tmpl.risk_level = data.risk_level;
          tmpl.scan_report = data.scan_report;
          tmpl.scan_version = data.scan_version;
          showEditTemplateModal(tmplId);
        })
        .catch(function () {
          showToast("Re-scan failed");
        })
        .finally(function () {
          rescanBtn.disabled = false;
          rescanBtn.textContent = "Re-scan";
        });
    };
  }
  // Reset collapsible state before applying readonly rules (prevents state leak
  // when switching between readonly and editable skills in the same session)
  const allDetails = document.querySelectorAll(
    "#edit-template-box .admin-details",
  );
  for (let d = 0; d < allDetails.length; d++) allDetails[d].open = false;

  // --- Readonly mode for imported skills ---
  const isReadonly = tmpl.readonly || false;
  // An unlocked install retains origin="source" — combined with !readonly it
  // means the operator detached the skill from upstream and may have edits.
  const isUnlockedInstall =
    !isReadonly && tmpl.origin && tmpl.origin === "source";
  const editTitle = document.getElementById("edit-template-title");
  if (editTitle)
    editTitle.textContent = isReadonly ? "View Skill" : "Edit Skill";
  // Origin badge — show provenance for installed skills
  const originBadge = document.getElementById("etm-origin-badge");
  if (originBadge) {
    if (isReadonly && tmpl.source_url) {
      originBadge.textContent = "Installed from \u00a0" + tmpl.source_url;
      originBadge.style.display = "inline-flex";
    } else if (isReadonly && tmpl.origin && tmpl.origin !== "manual") {
      originBadge.textContent = "Installed skill";
      originBadge.style.display = "inline-flex";
    } else if (isUnlockedInstall && tmpl.source_url) {
      originBadge.textContent = "Customized from \u00a0" + tmpl.source_url;
      originBadge.style.display = "inline-flex";
    } else if (isUnlockedInstall) {
      originBadge.textContent = "Customized from upstream";
      originBadge.style.display = "inline-flex";
    } else {
      originBadge.style.display = "none";
    }
  }
  const lockBtn = document.getElementById("etm-lock-btn");
  if (lockBtn) {
    // Inline-flex (not "") so display: none doesn't bleed into a
    // browser-default block reflow on re-show.
    lockBtn.style.display = isReadonly ? "inline-flex" : "none";
    lockBtn.dataset.skillId = tmplId;
    lockBtn.dataset.skillName = tmpl.name || "";
  }
  const submitBtn = document.getElementById("etm-submit");
  if (submitBtn) {
    submitBtn.style.display = "";
    submitBtn.textContent = isReadonly ? "Save Config" : "Save";
    // Always reset to enabled — submitEditTemplate disables this on click
    // and re-enables in .finally, but a stale disabled=true survives a
    // mutate-in-place re-render (e.g. after unlock) and would leave the
    // button non-functional otherwise.
    submitBtn.disabled = false;
  }
  // Spec/content fields: locked for installed skills (preserve source fidelity).
  // Point screen readers at the origin badge so the "why is this disabled?"
  // affordance sighted users see is also announced.
  [
    "etm-name",
    "etm-category",
    "etm-description",
    "etm-tags",
    "etm-author",
    "etm-version",
    "etm-license",
    "etm-compatibility",
    "etm-paths",
    "etm-arguments",
    "etm-argument-hint",
    "etm-activation",
    "etm-content",
    "etm-default",
  ].forEach(function (id) {
    const el = document.getElementById(id);
    if (!el) return;
    el.disabled = isReadonly;
    if (isReadonly) {
      el.setAttribute("aria-describedby", "etm-origin-badge");
    } else {
      el.removeAttribute("aria-describedby");
    }
  });
  // Runtime config fields: always editable, even for readonly skills.
  // Admins should be able to override these local-UX-ish values
  // (model, effort, hidden-from-menu, ...) on installed skills
  // without unlocking the row.  Must match SKILL_RUNTIME_CONFIG_FIELDS
  // in turnstone/core/skill_field_validation.py.
  [
    "esk-model",
    "esk-temperature",
    "esk-reasoning-effort",
    "esk-max-tokens",
    "esk-token-budget",
    "esk-agent-max-turns",
    "esk-auto-approve",
    "esk-enabled",
    "etm-hidden-from-menu",
  ].forEach(function (id) {
    const el = document.getElementById(id);
    if (el) el.disabled = false;
  });
  // esk-allowed-tools follows auto_approve state, not readonly state
  const allowedToolsEl = document.getElementById("esk-allowed-tools");
  if (allowedToolsEl) allowedToolsEl.disabled = tmpl.auto_approve || false;
  const cancelBtn = document.querySelector("#edit-template-box .modal-cancel");
  if (cancelBtn) cancelBtn.textContent = isReadonly ? "Close" : "Cancel";
  // Auto-expand Runtime Config collapsible for installed skills so config is visible
  if (isReadonly) {
    const details = document.querySelectorAll(
      "#edit-template-box .admin-details",
    );
    for (let d = 0; d < details.length; d++) details[d].open = true;
  }
  // --- Skill Resources ---
  const resSection = document.getElementById("etm-resources-section");
  if (resSection) {
    _loadSkillResources(tmplId, isReadonly);
  }
  if (!alreadyOpen) {
    _etmTrapHandler = _installTrap(
      "edit-template-overlay",
      "edit-template-box",
    );
    // Focus management — only on the initial open. Re-renders preserve
    // wherever focus was so a screen reader doesn't get a transition.
    // For readonly skills, prefer the lock button so keyboard users land
    // on the unlock affordance instead of having to Tab past every
    // disabled spec field to reach it. Cancel is still one Shift-Tab away.
    if (isReadonly) {
      if (lockBtn && lockBtn.style.display !== "none") {
        lockBtn.focus();
      } else if (cancelBtn) {
        cancelBtn.focus();
      }
    } else {
      document.getElementById("etm-name").focus();
    }
  }
}

function hideEditTemplateModal() {
  document.getElementById("edit-template-overlay").style.display = "none";
  _etmTrapHandler = _removeTrap(_etmTrapHandler);
  if (_etmTriggerEl && _etmTriggerEl.focus) {
    _etmTriggerEl.focus();
  }
  _etmTriggerEl = null;
}

function unlockSkill() {
  const btn = document.getElementById("etm-lock-btn");
  if (!btn) return;
  const skillId = btn.dataset.skillId;
  const skillName = btn.dataset.skillName || "this skill";
  if (!skillId) return;
  showConfirmModal(
    "Customize " + skillName + "?",
    "This detaches the skill from its upstream source so you can edit " +
      "content, description, and resources locally. The current version is " +
      "saved to History — you can revert from there. Future updates from " +
      "the upstream source will not be applied.",
    "Customize",
    function () {
      _performUnlockSkill(skillId);
    },
  );
}

function _performUnlockSkill(skillId) {
  const btn = document.getElementById("etm-lock-btn");
  if (btn) btn.disabled = true;
  authFetch("/v1/api/admin/skills/" + skillId + "/unlock", {
    method: "POST",
  })
    .then(function (r) {
      if (!r.ok) {
        return r.json().then(function (data) {
          throw new Error((data && data.error) || "Unlock failed");
        });
      }
      return r.json();
    })
    .then(function () {
      showToast("Skill unlocked — fields are now editable");
      // Refresh the cached list, then re-render the open modal in place from
      // the fresh row. No close/reopen → no flicker, no focus bounce.
      return loadGovSkills().then(function () {
        showEditTemplateModal(skillId);
      });
    })
    .catch(function (e) {
      showToast(e.message || "Unlock failed");
    })
    .finally(function () {
      // Re-enable in case the re-render didn't happen (error path); the
      // success path hides the button anyway via showEditTemplateModal.
      if (btn) btn.disabled = false;
    });
}

// ---------------------------------------------------------------------------
// Skill Resources
// ---------------------------------------------------------------------------

function _loadSkillResources(skillId, readonly) {
  const container = document.getElementById("etm-resources-list");
  const addBtn = document.getElementById("etm-add-resource-btn");
  const addForm = document.getElementById("etm-add-resource-form");
  if (!container) return;
  setSafeHtml(container, '<div class="dashboard-empty">Loading...</div>');
  if (addBtn) addBtn.style.display = readonly ? "none" : "";
  if (addForm) addForm.style.display = "none";

  authFetch("/v1/api/admin/skills/" + skillId + "/resources")
    .then(function (r) {
      if (!r.ok) throw new Error("Failed");
      return r.json();
    })
    .then(function (data) {
      const resources = data.resources || [];
      if (!resources.length) {
        setSafeHtml(
          container,
          '<div class="dashboard-empty">No resource files</div>',
        );
        return;
      }
      let html = "";
      for (let i = 0; i < resources.length; i++) {
        const res = resources[i];
        const sizeStr =
          res.size > 1024
            ? (res.size / 1024).toFixed(1) + " KB"
            : res.size + " B";
        html +=
          '<div role="listitem" style="display:flex;align-items:center;padding:4px 0;gap:8px">' +
          '<span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"><code>' +
          escapeHtml(res.path) +
          "</code></span>" +
          '<span style="width:80px;text-align:right;opacity:0.6">' +
          sizeStr +
          "</span>" +
          '<span style="width:60px;text-align:right">' +
          (readonly
            ? ""
            : '<button class="admin-btn-danger" data-del-res="' +
              escapeHtml(res.path) +
              '" style="font-size:0.85em" aria-label="Delete resource ' +
              escapeHtml(res.path) +
              '">delete</button>') +
          "</span></div>";
      }
      setSafeHtml(container, html);
      if (!readonly) {
        container.querySelectorAll("[data-del-res]").forEach(function (btn) {
          btn.addEventListener("click", function () {
            const path = this.getAttribute("data-del-res");
            showConfirmModal(
              "Delete Resource",
              'Delete "' + path + '"?',
              "Delete",
              function () {
                authFetch(
                  "/v1/api/admin/skills/" +
                    skillId +
                    "/resources/" +
                    path.split("/").map(encodeURIComponent).join("/"),
                  { method: "DELETE" },
                )
                  .then(function (r) {
                    if (!r.ok) throw new Error();
                    return r.json();
                  })
                  .then(function () {
                    showToast("Resource deleted");
                    _loadSkillResources(skillId, readonly);
                    loadGovSkills();
                    const addBtn = document.getElementById(
                      "etm-add-resource-btn",
                    );
                    if (addBtn) addBtn.focus();
                  })
                  .catch(function () {
                    showToast("Failed to delete resource");
                  });
              },
            );
          });
        });
      }
    })
    .catch(function () {
      setSafeHtml(
        container,
        '<div class="dashboard-empty">Failed to load resources</div>',
      );
    });
}

function _showAddResourceForm(skillId) {
  const form = document.getElementById("etm-add-resource-form");
  if (!form) return;
  form.style.display = "";
  document.getElementById("etm-res-path").value = "";
  document.getElementById("etm-res-content").value = "";
  document.getElementById("etm-res-content-type").value = "text/plain";
  document.getElementById("etm-res-submit").onclick = function () {
    const path = (document.getElementById("etm-res-path").value || "").trim();
    const content = document.getElementById("etm-res-content").value || "";
    const contentType = document.getElementById("etm-res-content-type").value;
    if (!path || !content) {
      showToast("Path and content are required");
      return;
    }
    if (
      !path.startsWith("scripts/") &&
      !path.startsWith("references/") &&
      !path.startsWith("assets/")
    ) {
      showToast("Path must start with scripts/, references/, or assets/");
      return;
    }
    this.disabled = true;
    this.textContent = "Uploading\u2026";
    authFetch("/v1/api/admin/skills/" + skillId + "/resources", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        path: path,
        content: content,
        content_type: contentType,
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
        showToast("Resource added");
        form.style.display = "none";
        _loadSkillResources(skillId, false);
        loadGovSkills();
      })
      .catch(function (e) {
        showToast(e.message || "Failed to add resource");
      })
      .finally(function () {
        const btn = document.getElementById("etm-res-submit");
        if (btn) {
          btn.disabled = false;
          btn.textContent = "Upload";
        }
      });
  };
}

// ---------------------------------------------------------------------------
// Pending resources (create modal)
// ---------------------------------------------------------------------------

function _renderPendingResources() {
  const container = document.getElementById("ctm-resources-list");
  if (!container) return;
  if (!_pendingResources.length) {
    setSafeHtml(
      container,
      '<div class="dashboard-empty">No resource files yet</div>',
    );
    return;
  }
  let html = "";
  for (let i = 0; i < _pendingResources.length; i++) {
    const r = _pendingResources[i];
    const sizeStr =
      r.content.length > 1024
        ? (r.content.length / 1024).toFixed(1) + " KB"
        : r.content.length + " B";
    html +=
      '<div role="listitem" style="display:flex;align-items:center;padding:4px 0;gap:8px">' +
      '<span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"><code>' +
      escapeHtml(r.path) +
      "</code></span>" +
      '<span style="width:80px;text-align:right;opacity:0.6">' +
      sizeStr +
      "</span>" +
      '<span style="width:60px;text-align:right">' +
      '<button class="admin-btn-danger" data-remove-res="' +
      i +
      '" style="font-size:0.85em" aria-label="Remove resource ' +
      escapeHtml(r.path) +
      '">remove</button>' +
      "</span></div>";
  }
  setSafeHtml(container, html);
  container.querySelectorAll("[data-remove-res]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      const idx = parseInt(this.getAttribute("data-remove-res"), 10);
      _pendingResources.splice(idx, 1);
      _renderPendingResources();
    });
  });
}

function _addPendingResource() {
  const path = (document.getElementById("ctm-res-path").value || "").trim();
  const content = document.getElementById("ctm-res-content").value || "";
  const contentType = document.getElementById("ctm-res-content-type").value;
  if (!path || !content) {
    showToast("Path and content are required");
    return;
  }
  if (
    !path.startsWith("scripts/") &&
    !path.startsWith("references/") &&
    !path.startsWith("assets/")
  ) {
    showToast("Path must start with scripts/, references/, or assets/");
    return;
  }
  if (
    _pendingResources.some(function (r) {
      return r.path === path;
    })
  ) {
    showToast("Resource path already added");
    return;
  }
  if (_pendingResources.length >= 10) {
    showToast("Maximum 10 resources per skill");
    return;
  }
  _pendingResources.push({
    path: path,
    content: content,
    content_type: contentType,
  });
  document.getElementById("ctm-res-path").value = "";
  document.getElementById("ctm-res-content").value = "";
  _renderPendingResources();
  document.getElementById("ctm-res-path").focus();
}

function submitEditTemplate() {
  // Clear any prior error before re-validating — a successful submit shouldn't
  // leave stale red text visible during the in-flight PUT, and a successful
  // PUT shouldn't either. Cheaper than reasoning about every catch path
  // remembering to clear on success.
  const prevErr = document.getElementById("edit-template-error");
  if (prevErr) {
    prevErr.classList.remove("is-visible");
    prevErr.textContent = "";
  }
  const id = document.getElementById("etm-id").value;
  const content = document.getElementById("etm-content").value;
  const varList = _detectTemplateVars(content);
  const tagsRaw = (document.getElementById("etm-tags").value || "").trim();
  const tagsArray = tagsRaw
    ? tagsRaw
        .split(",")
        .map(function (t) {
          return t.trim();
        })
        .filter(Boolean)
    : [];
  // Session config fields
  const esTemp = document.getElementById("esk-temperature").value.trim();
  const esMaxTok = document.getElementById("esk-max-tokens").value.trim();
  const esBudget = document.getElementById("esk-token-budget").value.trim();
  const esMaxTurns = document
    .getElementById("esk-agent-max-turns")
    .value.trim();
  const esAllowed = (
    document.getElementById("esk-allowed-tools").value || ""
  ).trim();
  const esAllowedArr = esAllowed
    ? esAllowed
        .split(",")
        .map(function (t) {
          return t.trim();
        })
        .filter(Boolean)
    : [];
  const esNotifyRaw = (
    document.getElementById("esk-notify-on-complete").value || ""
  ).trim();
  let esNotifyVal = "[]";
  if (esNotifyRaw) {
    try {
      const esNotifyParsed = JSON.parse(esNotifyRaw);
      if (!Array.isArray(esNotifyParsed))
        throw new Error("must be a JSON array");
      esNotifyVal = JSON.stringify(esNotifyParsed);
    } catch (ne) {
      const ne3 = document.getElementById("edit-template-error");
      ne3.textContent = "Notify on completion: " + ne.message;
      ne3.classList.add("is-visible");
      return;
    }
  }
  document.getElementById("etm-submit").disabled = true;
  const esVersion = (document.getElementById("etm-version").value || "").trim();
  const esPathsArr = (document.getElementById("etm-paths").value || "")
    .split(",")
    .map(function (p) {
      return p.trim();
    })
    .filter(Boolean);
  const esArgsArr = (document.getElementById("etm-arguments").value || "")
    .split(",")
    .map(function (a) {
      return a.trim();
    })
    .filter(Boolean);
  const esArgumentHint = (
    document.getElementById("etm-argument-hint").value || ""
  ).trim();
  const updateBody = {
    name: document.getElementById("etm-name").value.trim(),
    category: document.getElementById("etm-category").value,
    description: (
      document.getElementById("etm-description").value || ""
    ).trim(),
    tags: JSON.stringify(tagsArray),
    author: (document.getElementById("etm-author").value || "").trim(),
    license: (document.getElementById("etm-license").value || "").trim(),
    compatibility: (
      document.getElementById("etm-compatibility").value || ""
    ).trim(),
    paths: JSON.stringify(esPathsArr),
    hidden_from_menu: document.getElementById("etm-hidden-from-menu").checked,
    arguments: JSON.stringify(esArgsArr),
    argument_hint: esArgumentHint,
    activation: document.getElementById("etm-activation").value,
    content: content,
    variables: JSON.stringify(varList),
    is_default: document.getElementById("etm-default").checked,
    model: document.getElementById("esk-model").value.trim(),
    auto_approve: document.getElementById("esk-auto-approve").checked,
    temperature: esTemp ? parseFloat(esTemp) : null,
    reasoning_effort: document.getElementById("esk-reasoning-effort").value,
    max_tokens: esMaxTok ? parseInt(esMaxTok, 10) : null,
    token_budget: esBudget ? parseInt(esBudget, 10) : 0,
    agent_max_turns: esMaxTurns ? parseInt(esMaxTurns, 10) : null,
    allowed_tools: JSON.stringify(esAllowedArr),
    notify_on_complete: esNotifyVal,
    enabled: document.getElementById("esk-enabled").checked,
  };
  if (esVersion) updateBody.version = esVersion;
  authFetch("/v1/api/admin/skills/" + id, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(updateBody),
  })
    .then(function (r) {
      if (!r.ok)
        return r.json().then(function (d) {
          throw new Error(d.error || "Failed");
        });
      return r.json();
    })
    .then(function () {
      hideEditTemplateModal();
      showToast("Skill updated");
      loadGovSkills();
    })
    .catch(function (e) {
      const el = document.getElementById("edit-template-error");
      el.textContent = e.message;
      el.classList.add("is-visible");
    })
    .finally(function () {
      document.getElementById("etm-submit").disabled = false;
    });
}

// ---------------------------------------------------------------------------
// Usage
// ---------------------------------------------------------------------------

function loadGovUsage() {
  const now = new Date();
  let since;
  if (_govUsageRange === "24h") since = new Date(now - 24 * 60 * 60 * 1000);
  else if (_govUsageRange === "30d")
    since = new Date(now - 30 * 24 * 60 * 60 * 1000);
  else since = new Date(now - 7 * 24 * 60 * 60 * 1000);
  const sinceStr = since.toISOString().slice(0, 19);

  // Fetch summary + breakdown in parallel
  const summaryUrl =
    "/v1/api/admin/usage?since=" + encodeURIComponent(sinceStr);
  const breakdownUrl = summaryUrl + "&group_by=" + _govUsageGroupBy;

  Promise.all([
    authFetch(summaryUrl).then(function (r) {
      return r.json();
    }),
    authFetch(breakdownUrl).then(function (r) {
      return r.json();
    }),
  ])
    .then(function (results) {
      _renderGovUsage(results[0], results[1]);
    })
    .catch(function () {
      setSafeHtml(
        document.getElementById("admin-usage-content"),
        '<div class="dashboard-empty">Failed to load usage data</div>',
      );
    });
}

function _renderGovUsage(summary, breakdown) {
  const container = document.getElementById("admin-usage-content");
  const s = (summary.breakdown && summary.breakdown[0]) || {};
  const prompt = s.prompt_tokens || 0;
  const completion = s.completion_tokens || 0;
  const total = prompt + completion;
  const tools = s.tool_calls_count || 0;
  const cacheWrite = s.cache_creation_tokens || 0;
  const cacheRead = s.cache_read_tokens || 0;

  const cacheZero = cacheWrite === 0 && cacheRead === 0;
  const cacheCls =
    "usage-readout usage-readout-secondary" +
    (cacheZero ? " usage-readout-zero" : "");

  let html =
    '<div class="usage-summary">' +
    '<div class="usage-readout"><span class="usage-readout-value">' +
    formatTokens(total) +
    '</span><span class="usage-readout-label">total tokens</span></div>' +
    '<div class="usage-readout"><span class="usage-readout-value">' +
    formatTokens(prompt) +
    '</span><span class="usage-readout-label">prompt</span></div>' +
    '<div class="usage-readout"><span class="usage-readout-value">' +
    formatTokens(completion) +
    '</span><span class="usage-readout-label">completion</span></div>' +
    '<div class="usage-readout"><span class="usage-readout-value">' +
    formatCount(tools) +
    '</span><span class="usage-readout-label">tool calls</span></div>' +
    '<div class="usage-summary-divider"></div>' +
    '<div class="' +
    cacheCls +
    '"><span class="usage-readout-value">' +
    formatTokens(cacheWrite) +
    '</span><span class="usage-readout-label">cache write</span></div>' +
    '<div class="' +
    cacheCls +
    '"><span class="usage-readout-value">' +
    formatTokens(cacheRead) +
    '</span><span class="usage-readout-label">cache read</span></div>' +
    "</div>";

  // Bar chart breakdown
  const items = breakdown.breakdown || [];
  if (items.length) {
    let maxVal = 0;
    for (let i = 0; i < items.length; i++) {
      const v =
        (items[i].prompt_tokens || 0) + (items[i].completion_tokens || 0);
      if (v > maxVal) maxVal = v;
    }
    html += '<div class="usage-chart">';
    for (let j = 0; j < items.length; j++) {
      const item = items[j];
      const val = (item.prompt_tokens || 0) + (item.completion_tokens || 0);
      const pct = maxVal > 0 ? Math.round((val / maxVal) * 100) : 0;
      const label = item.key || "\u2014";
      html +=
        '<div class="usage-bar-row">' +
        '<span class="usage-bar-label">' +
        escapeHtml(label) +
        "</span>" +
        '<div class="usage-bar-track"><div class="usage-bar-fill" style="width:' +
        pct +
        '%"></div></div>' +
        '<span class="usage-bar-value">' +
        formatTokens(val) +
        "</span>" +
        "</div>";
    }
    html += "</div>";
  } else {
    html += '<div class="dashboard-empty">No usage data for this period</div>';
  }

  setSafeHtml(container, html);
}

function setUsageRange(range) {
  _govUsageRange = range;
  // Update button states
  const btns = document.querySelectorAll(".usage-range-btn");
  for (let i = 0; i < btns.length; i++) {
    btns[i].classList.toggle(
      "active",
      btns[i].getAttribute("data-range") === range,
    );
    btns[i].setAttribute(
      "aria-pressed",
      btns[i].classList.contains("active") ? "true" : "false",
    );
  }
  loadGovUsage();
}

function setUsageGroupBy(groupBy) {
  _govUsageGroupBy = groupBy;
  const btns = document.querySelectorAll(".usage-group-btn");
  for (let i = 0; i < btns.length; i++) {
    btns[i].classList.toggle(
      "active",
      btns[i].getAttribute("data-group") === groupBy,
    );
    btns[i].setAttribute(
      "aria-pressed",
      btns[i].classList.contains("active") ? "true" : "false",
    );
  }
  loadGovUsage();
}

// ---------------------------------------------------------------------------
// Audit
// ---------------------------------------------------------------------------

function loadGovAudit(append) {
  if (!append) {
    _govAuditOffset = 0;
    _govAuditEvents = [];
  }
  let url = "/v1/api/admin/audit?limit=50&offset=" + _govAuditOffset;
  const actionFilter = document.getElementById("audit-action-filter");
  const userFilter = document.getElementById("audit-user-filter");
  if (actionFilter && actionFilter.value)
    url += "&action=" + encodeURIComponent(actionFilter.value);
  if (userFilter && userFilter.value)
    url += "&user_id=" + encodeURIComponent(userFilter.value);

  authFetch(url)
    .then(function (r) {
      if (!r.ok) throw new Error("Failed");
      return r.json();
    })
    .then(function (data) {
      _govAuditTotal = data.total || 0;
      const events = data.events || [];
      _govAuditEvents = _govAuditEvents.concat(events);
      _renderGovAudit(_govAuditEvents, _govAuditTotal);
    })
    .catch(function () {
      setSafeHtml(
        document.getElementById("admin-audit-table"),
        '<div class="dashboard-empty">Failed to load audit events</div>',
      );
    });
}

function _relativeTime(isoStr) {
  const now = Date.now();
  const then = new Date(isoStr + "Z").getTime();
  const diff = Math.max(0, Math.floor((now - then) / 1000));
  if (diff < 60) return diff + "s ago";
  if (diff < 3600) return Math.floor(diff / 60) + "m ago";
  if (diff < 86400) return Math.floor(diff / 3600) + "h ago";
  return Math.floor(diff / 86400) + "d ago";
}

function _renderGovAudit(events, total) {
  const el = document.getElementById("admin-audit-table");
  if (!events.length) {
    setSafeHtml(el, '<div class="dashboard-empty">No audit events</div>');
    return;
  }
  let html = "";
  for (let i = 0; i < events.length; i++) {
    const ev = events[i];
    let detail = "";
    try {
      const d = JSON.parse(ev.detail || "{}");
      const keys = Object.keys(d);
      if (keys.length) {
        const parts = [];
        for (let k = 0; k < Math.min(keys.length, 3); k++) {
          parts.push(keys[k] + "=" + String(d[keys[k]]).slice(0, 30));
        }
        detail = parts.join(", ");
      }
    } catch (e) {
      detail = ev.detail;
    }

    let actionCls = "audit-badge";
    if (ev.action.indexOf("delete") >= 0 || ev.action.indexOf("revoke") >= 0)
      actionCls += " audit-danger";
    else if (
      ev.action.indexOf("create") >= 0 ||
      ev.action.indexOf("assign") >= 0
    )
      actionCls += " audit-success";

    html +=
      '<div class="admin-row" role="listitem">' +
      '<span class="admin-col admin-col-atime" title="' +
      escapeHtml(ev.timestamp) +
      '">' +
      _relativeTime(ev.timestamp) +
      "</span>" +
      '<span class="admin-col admin-col-auser">' +
      escapeHtml(
        ev.username || (ev.user_id ? ev.user_id.slice(0, 8) : "\u2014"),
      ) +
      "</span>" +
      '<span class="admin-col admin-col-aaction"><span class="' +
      actionCls +
      '">' +
      escapeHtml(ev.action) +
      "</span></span>" +
      '<span class="admin-col admin-col-aresource">' +
      escapeHtml(
        ev.resource_type
          ? ev.resource_type + "/" + (ev.resource_id || "").slice(0, 8)
          : "\u2014",
      ) +
      "</span>" +
      '<span class="admin-col admin-col-adetail" title="' +
      escapeHtml(ev.detail) +
      '">' +
      escapeHtml(detail || "\u2014") +
      "</span>" +
      "</div>";
  }
  // Pagination
  if (events.length < total) {
    html +=
      '<div class="pagination"><button class="audit-load-more">Load more (' +
      events.length +
      " of " +
      total +
      ")</button></div>";
  }
  setSafeHtml(el, html);

  const loadMoreBtn = el.querySelector(".audit-load-more");
  if (loadMoreBtn) loadMoreBtn.addEventListener("click", loadMoreAudit);
}

function loadMoreAudit() {
  _govAuditOffset = _govAuditEvents.length;
  loadGovAudit(true);
}

// Populate audit user filter from admin users list
function _populateAuditUserFilter() {
  const sel = document.getElementById("audit-user-filter");
  if (!sel) return;
  let html = '<option value="">All users</option>';
  for (let i = 0; i < _adminUsers.length; i++) {
    html +=
      '<option value="' +
      escapeHtml(_adminUsers[i].user_id) +
      '">' +
      escapeHtml(_adminUsers[i].username) +
      "</option>";
  }
  setSafeHtml(sel, html);
}

// ---------------------------------------------------------------------------
// Memories tab
// ---------------------------------------------------------------------------

let _adminMemories = [];
let _memDetailTrap = null;
let _memDetailTrigger = null;
let _memSearchTimer = null;
let _memSearchBound = false;

function loadAdminMemories() {
  clearTimeout(_memSearchTimer);
  // Bind search debounce on first load
  if (!_memSearchBound) {
    const searchEl = document.getElementById("mem-search");
    if (searchEl) {
      searchEl.addEventListener("input", function () {
        clearTimeout(_memSearchTimer);
        _memSearchTimer = setTimeout(loadAdminMemories, 300);
      });
    }
    _memSearchBound = true;
  }

  const memType = document.getElementById("mem-filter-type").value;
  const scope = document.getElementById("mem-filter-scope").value;
  const query = (document.getElementById("mem-search").value || "").trim();

  let url;
  if (query) {
    url =
      "/v1/api/admin/memories/search?q=" +
      encodeURIComponent(query) +
      (memType ? "&type=" + encodeURIComponent(memType) : "") +
      (scope ? "&scope=" + encodeURIComponent(scope) : "");
  } else {
    url =
      "/v1/api/admin/memories?limit=200" +
      (memType ? "&type=" + encodeURIComponent(memType) : "") +
      (scope ? "&scope=" + encodeURIComponent(scope) : "");
  }

  authFetch(url)
    .then(function (r) {
      if (!r.ok) throw new Error("Failed to load memories");
      return r.json();
    })
    .then(function (data) {
      _adminMemories = data.memories || [];
      _renderAdminMemories(_adminMemories, data.total || _adminMemories.length);
    })
    .catch(function () {
      setSafeHtml(
        document.getElementById("admin-memories-table"),
        '<div class="dashboard-empty">Failed to load memories</div>',
      );
    });
}

function _renderAdminMemories(items, total) {
  const el = document.getElementById("admin-memories-table");
  if (!items.length) {
    setSafeHtml(el, '<div class="dashboard-empty">No memories found</div>');
    return;
  }

  let html = "";
  for (let i = 0; i < items.length; i++) {
    const m = items[i];

    // Type badge
    const typeCls = "scope-badge mem-type-" + escapeHtml(m.type);
    const typeBadge =
      '<span class="' + typeCls + '">' + escapeHtml(m.type) + "</span>";

    // Scope badge
    let scopeLabel = m.scope;
    if (m.scope_id) scopeLabel += ":" + m.scope_id;
    const scopeCls = "scope-badge mem-scope-" + escapeHtml(m.scope);
    const scopeBadge =
      '<span class="' + scopeCls + '">' + escapeHtml(scopeLabel) + "</span>";

    // Description (truncated)
    let desc = m.description || "";
    if (desc.length > 60) desc = desc.substring(0, 57) + "…";

    html +=
      '<div class="admin-row" role="listitem">' +
      '<span class="admin-col admin-col-mname">' +
      escapeHtml(m.name) +
      "</span>" +
      '<span class="admin-col admin-col-mtype">' +
      typeBadge +
      "</span>" +
      '<span class="admin-col admin-col-mscope">' +
      scopeBadge +
      "</span>" +
      '<span class="admin-col admin-col-mdesc">' +
      escapeHtml(desc) +
      "</span>" +
      '<span class="admin-col admin-col-mupdated">' +
      _relativeTime(m.updated) +
      "</span>" +
      '<span class="admin-col admin-col-actions">' +
      '<button class="admin-btn-action" data-view-memory="' +
      escapeHtml(m.memory_id) +
      '">view</button>' +
      '<button class="admin-btn-danger" data-delete-memory="' +
      escapeHtml(m.memory_id) +
      '" data-delete-name="' +
      escapeHtml(m.name) +
      '">delete</button>' +
      "</span>" +
      "</div>";
  }

  setSafeHtml(el, html);

  // Bind view buttons
  const viewBtns = el.querySelectorAll("[data-view-memory]");
  for (let v = 0; v < viewBtns.length; v++) {
    viewBtns[v].addEventListener("click", function () {
      showMemoryDetailModal(this.getAttribute("data-view-memory"));
    });
  }

  // Bind delete buttons
  const delBtns = el.querySelectorAll("[data-delete-memory]");
  for (let d = 0; d < delBtns.length; d++) {
    delBtns[d].addEventListener("click", function () {
      const mid = this.getAttribute("data-delete-memory");
      const mname = this.getAttribute("data-delete-name");
      deleteAdminMemory(mid, mname);
    });
  }
}

function showMemoryDetailModal(memoryId) {
  _memDetailTrigger = document.activeElement;
  const ov = document.getElementById("memory-detail-overlay");
  ov.style.display = "flex";
  setSafeHtml(
    document.getElementById("memory-detail-body"),
    '<div class="dashboard-empty">Loading…</div>',
  );

  // Disable delete button and clear stale handler while loading
  const delBtn = document.getElementById("mem-detail-delete");
  delBtn.disabled = true;
  delBtn.onclick = null;

  // Focus close button for keyboard accessibility
  const closeBtn = ov.querySelector(".modal-cancel");
  if (closeBtn) closeBtn.focus();

  authFetch("/v1/api/admin/memories/" + encodeURIComponent(memoryId))
    .then(function (r) {
      if (!r.ok) throw new Error("Not found");
      return r.json();
    })
    .then(function (m) {
      let scopeLabel = m.scope;
      if (m.scope_id) scopeLabel += ":" + m.scope_id;

      const html =
        '<div class="mem-detail-grid">' +
        '<div class="mem-detail-field"><span class="mem-detail-label">Name</span>' +
        escapeHtml(m.name) +
        "</div>" +
        '<div class="mem-detail-field"><span class="mem-detail-label">Type</span>' +
        '<span class="scope-badge mem-type-' +
        escapeHtml(m.type) +
        '">' +
        escapeHtml(m.type) +
        "</span></div>" +
        '<div class="mem-detail-field"><span class="mem-detail-label">Scope</span>' +
        '<span class="scope-badge mem-scope-' +
        escapeHtml(m.scope) +
        '">' +
        escapeHtml(scopeLabel) +
        "</span></div>" +
        '<div class="mem-detail-field"><span class="mem-detail-label">Created</span>' +
        _relativeTime(m.created) +
        "</div>" +
        '<div class="mem-detail-field"><span class="mem-detail-label">Updated</span>' +
        _relativeTime(m.updated) +
        "</div>" +
        '<div class="mem-detail-field"><span class="mem-detail-label">Accessed</span>' +
        (m.access_count || 0) +
        " times</div>" +
        "</div>" +
        '<div class="mem-detail-label" style="margin-top:12px">Description</div>' +
        '<div class="mem-detail-desc">' +
        escapeHtml(m.description || "(none)") +
        "</div>" +
        '<div class="mem-detail-label" style="margin-top:12px">Content</div>' +
        '<pre class="memory-content-block">' +
        escapeHtml(m.content) +
        "</pre>";

      setSafeHtml(document.getElementById("memory-detail-body"), html);

      // Wire delete button now that data is loaded
      delBtn.disabled = false;
      delBtn.onclick = function () {
        deleteAdminMemory(m.memory_id, m.name);
      };
    })
    .catch(function () {
      setSafeHtml(
        document.getElementById("memory-detail-body"),
        '<div class="dashboard-empty">Failed to load memory</div>',
      );
    });

  _memDetailTrap = _installTrap("memory-detail-overlay", "memory-detail-box");
}

function hideMemoryDetailModal() {
  document.getElementById("memory-detail-overlay").style.display = "none";
  _memDetailTrap = _removeTrap(_memDetailTrap);
  if (_memDetailTrigger && _memDetailTrigger.focus) _memDetailTrigger.focus();
  _memDetailTrigger = null;
}

function deleteAdminMemory(memoryId, memoryName) {
  if (!confirm("Delete memory '" + memoryName + "'?")) return;

  authFetch("/v1/api/admin/memories/" + encodeURIComponent(memoryId), {
    method: "DELETE",
  })
    .then(function (r) {
      if (!r.ok)
        return r.json().then(function (d) {
          throw new Error(d.error || "Failed");
        });
      return r.json();
    })
    .then(function () {
      showToast("Memory deleted");
      // Close detail modal if open
      if (
        document.getElementById("memory-detail-overlay").style.display !==
        "none"
      ) {
        hideMemoryDetailModal();
      }
      loadAdminMemories();
    })
    .catch(function (e) {
      showToast("Error: " + e.message);
    });
}

// ---------------------------------------------------------------------------
// Skill Discovery
// ---------------------------------------------------------------------------

function switchSkillView(view) {
  _skillCurrentView = view;
  const btns = document.querySelectorAll("#admin-skills [data-skill-view]");
  for (let i = 0; i < btns.length; i++) {
    const isActive = btns[i].getAttribute("data-skill-view") === view;
    btns[i].classList.toggle("active", isActive);
    btns[i].setAttribute("aria-selected", isActive ? "true" : "false");
    btns[i].setAttribute("tabindex", isActive ? "0" : "-1");
  }
  document.getElementById("skill-view-installed").style.display =
    view === "installed" ? "" : "none";
  document.getElementById("skill-view-discover").style.display =
    view === "discover" ? "" : "none";
  const toolbar = document.getElementById("skill-installed-toolbar");
  if (toolbar) toolbar.style.display = view === "installed" ? "" : "none";

  if (view === "installed") {
    loadGovSkills();
  } else {
    const q = document.getElementById("skill-discover-q");
    if (q) q.focus();
  }
}

function searchSkillDiscover() {
  const q = (document.getElementById("skill-discover-q").value || "").trim();
  if (!q) {
    showToast("Enter a search query");
    return;
  }
  _skillDiscoverResults = [];
  _skillDiscoverQuery = q;

  const el = document.getElementById("skill-discover-results");
  setSafeHtml(el, '<div class="dashboard-empty">Searching\u2026</div>');

  const searchBtn = document.getElementById("skill-discover-search-btn");
  if (searchBtn) searchBtn.disabled = true;

  const url =
    "/v1/api/admin/skills/discover?limit=20&q=" + encodeURIComponent(q);

  authFetch(url)
    .then(function (r) {
      if (!r.ok)
        return r.json().then(function (d) {
          throw new Error(d.error || "Search failed");
        });
      return r.json();
    })
    .then(function (data) {
      _skillDiscoverResults = data.skills || [];
      _renderSkillDiscoverResults();
    })
    .catch(function (e) {
      setSafeHtml(
        el,
        '<div class="dashboard-empty">' + escapeHtml(e.message) + "</div>",
      );
    })
    .finally(function () {
      if (searchBtn) searchBtn.disabled = false;
    });
}

function _renderSkillDiscoverResults() {
  const el = document.getElementById("skill-discover-results");
  if (!_skillDiscoverResults.length) {
    setSafeHtml(el, '<div class="dashboard-empty">No skills found</div>');
    return;
  }

  let html = "";
  for (let i = 0; i < _skillDiscoverResults.length; i++) {
    const s = _skillDiscoverResults[i];
    const nameLabel = escapeHtml(s.name || "");
    let actionHtml;
    if (s.installed) {
      let scanBadgeHtml = "";
      if (s.risk_level) {
        const scanCls =
          {
            safe: "scope-scan-safe",
            low: "scope-scan-low",
            medium: "scope-scan-medium",
            high: "scope-scan-high",
            critical: "scope-scan-critical",
          }[s.risk_level] || "";
        scanBadgeHtml =
          '<span class="scope-badge ' +
          scanCls +
          '" style="margin-right:4px">' +
          escapeHtml(s.risk_level) +
          "</span>";
      }
      actionHtml =
        scanBadgeHtml + '<span class="mcp-installed-badge">Installed</span>';
    } else {
      actionHtml =
        '<button class="mcp-install-btn" data-skill-install="' +
        i +
        '" aria-label="Install ' +
        nameLabel +
        '">Install</button>';
    }

    // Tags
    let tagHtml = "";
    const tags = s.tags || [];
    for (let t = 0; t < tags.length && t < 4; t++) {
      tagHtml += '<span class="scope-badge">' + escapeHtml(tags[t]) + "</span>";
    }

    // Source + install count badge
    let metaHtml = "";
    if (s.source) {
      metaHtml +=
        '<span class="scope-badge mcp-transport-http">' +
        escapeHtml(s.source) +
        "</span>";
    }
    if (s.install_count > 0) {
      metaHtml +=
        '<span class="mcp-reg-card-version">' +
        s.install_count.toLocaleString() +
        " installs</span>";
    }

    html +=
      '<div class="mcp-reg-card" role="listitem">' +
      '<div class="mcp-reg-card-info">' +
      '<div class="mcp-reg-card-name">' +
      nameLabel +
      (s.author
        ? ' <span class="mcp-reg-card-version">by ' +
          escapeHtml(s.author) +
          "</span>"
        : "") +
      "</div>" +
      (s.description
        ? '<div class="mcp-reg-card-desc">' +
          escapeHtml(s.description) +
          "</div>"
        : "") +
      '<div class="mcp-reg-card-meta">' +
      tagHtml +
      metaHtml +
      "</div></div>" +
      '<div class="mcp-reg-card-actions">' +
      actionHtml +
      "</div></div>";
  }

  setSafeHtml(el, html);

  // Bind install handlers
  el.querySelectorAll("[data-skill-install]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      const idx = parseInt(this.getAttribute("data-skill-install"), 10);
      installDiscoveredSkill(_skillDiscoverResults[idx]);
    });
  });
}

function installDiscoveredSkill(skill) {
  if (!skill) return;

  // Disable the button
  const btns = document.querySelectorAll("[data-skill-install]");
  for (let i = 0; i < btns.length; i++) {
    const idx = parseInt(btns[i].getAttribute("data-skill-install"), 10);
    if (
      _skillDiscoverResults[idx] &&
      _skillDiscoverResults[idx].id === skill.id
    ) {
      btns[i].disabled = true;
      btns[i].textContent = "Installing\u2026";
      break;
    }
  }

  let body;
  if (skill.source === "github") {
    body = { source: "github", url: skill.source_url };
  } else {
    body = { source: "skills.sh", skill_id: skill.id };
  }

  authFetch("/v1/api/admin/skills/install", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  })
    .then(function (r) {
      if (!r.ok)
        return r.json().then(function (d) {
          throw new Error(d.error || "Install failed");
        });
      return r.json();
    })
    .then(function (data) {
      const first = (data.installed && data.installed[0]) || {};
      const tierMsg = first.risk_level ? " [" + first.risk_level + "]" : "";
      showToast("Skill installed: " + (skill.name || skill.id) + tierMsg);
      // Mark as installed in results with scan data
      for (let j = 0; j < _skillDiscoverResults.length; j++) {
        if (_skillDiscoverResults[j].id === skill.id) {
          _skillDiscoverResults[j].installed = true;
          _skillDiscoverResults[j].risk_level = first.risk_level || "";
          break;
        }
      }
      _renderSkillDiscoverResults();
    })
    .catch(function (e) {
      showToast("Error: " + e.message);
      _renderSkillDiscoverResults();
    });
}

function showGitHubImportModal() {
  _giTriggerEl = document.activeElement;
  document.getElementById("github-import-overlay").style.display = "";
  const urlInput = document.getElementById("gi-url");
  urlInput.value = "";
  const errEl = document.getElementById("github-import-error");
  errEl.textContent = "";
  errEl.classList.remove("is-visible");
  _giTrapHandler = _installTrap("github-import-overlay", "github-import-box");
  urlInput.focus();
}

function hideGitHubImportModal() {
  document.getElementById("github-import-overlay").style.display = "none";
  _giTrapHandler = _removeTrap(_giTrapHandler);
  if (_giTriggerEl) {
    _giTriggerEl.focus();
    _giTriggerEl = null;
  }
}

function submitGitHubImport() {
  const url = (document.getElementById("gi-url").value || "").trim();
  const errEl = document.getElementById("github-import-error");
  if (!url) {
    errEl.textContent = "URL is required";
    errEl.classList.add("is-visible");
    return;
  }
  if (!/^https?:\/\/github\.com\//i.test(url)) {
    errEl.textContent = "Must be a GitHub URL";
    errEl.classList.add("is-visible");
    return;
  }

  const submitBtn = document.getElementById("gi-submit");
  submitBtn.disabled = true;
  submitBtn.textContent = "Installing\u2026";
  errEl.classList.remove("is-visible");

  authFetch("/v1/api/admin/skills/install", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ source: "github", url: url }),
  })
    .then(function (r) {
      if (!r.ok)
        return r.json().then(function (d) {
          throw new Error(d.error || "Install failed");
        });
      return r.json();
    })
    .then(function (data) {
      hideGitHubImportModal();
      const count = data.installed.length;
      const skipCount = (data.skipped || []).length;
      let msg;
      if (count === 1 && !skipCount) {
        const name = data.installed[0].name || "";
        const tierMsg = data.installed[0].risk_level
          ? " [" + data.installed[0].risk_level + "]"
          : "";
        msg = "Skill installed: " + name + tierMsg;
      } else if (count === 0 && skipCount) {
        msg =
          "All " +
          skipCount +
          " skill" +
          (skipCount !== 1 ? "s" : "") +
          " already installed";
      } else {
        msg = count + " skill" + (count !== 1 ? "s" : "") + " installed";
        if (skipCount) msg += " (" + skipCount + " already installed)";
      }
      showToast(msg);
      loadGovSkills();
    })
    .catch(function (e) {
      errEl.textContent = e.message;
      errEl.classList.add("is-visible");
    })
    .finally(function () {
      submitBtn.disabled = false;
      submitBtn.textContent = "Install";
    });
}

// ---------------------------------------------------------------------------
// Prompt Policies (system message composition)
// ---------------------------------------------------------------------------

let _promptPolicies = [];
let _cppTrapHandler = null;
let _cppTriggerEl = null;
let _eppTrapHandler = null;
let _eppTriggerEl = null;

function loadPromptPolicies() {
  authFetch("/v1/api/admin/prompt-policies")
    .then(function (r) {
      if (!r.ok) throw new Error("Failed");
      return r.json();
    })
    .then(function (data) {
      _promptPolicies = data.policies || [];
      _renderPromptPolicies(_promptPolicies);
    })
    .catch(function () {
      const el = document.getElementById("admin-prompt-policies-table");
      el.textContent = "";
      const empty = document.createElement("div");
      empty.className = "dashboard-empty";
      empty.textContent = "Failed to load prompts";
      el.appendChild(empty);
    });
}

function _renderPromptPolicies(items) {
  const el = document.getElementById("admin-prompt-policies-table");
  el.textContent = "";
  if (!items.length) {
    const empty = document.createElement("div");
    empty.className = "dashboard-empty";
    empty.textContent = "No prompts defined";
    el.appendChild(empty);
    return;
  }
  for (let i = 0; i < items.length; i++) {
    const p = items[i];
    const row = document.createElement("div");
    row.className = "admin-row";
    row.setAttribute("role", "listitem");

    const colName = document.createElement("span");
    colName.className = "admin-col admin-col-pname";
    colName.textContent = p.name;
    row.appendChild(colName);

    const colGate = document.createElement("span");
    colGate.className = "admin-col admin-col-ppattern";
    if (p.tool_gate) {
      const code = document.createElement("code");
      code.textContent = p.tool_gate;
      colGate.appendChild(code);
    } else {
      const em = document.createElement("em");
      em.textContent = "unconditional";
      colGate.appendChild(em);
    }
    row.appendChild(colGate);

    const colPri = document.createElement("span");
    colPri.className = "admin-col admin-col-ppriority";
    colPri.textContent = String(p.priority);
    row.appendChild(colPri);

    const colStatus = document.createElement("span");
    colStatus.className = "admin-col admin-col-pstatus";
    const dot = document.createElement("span");
    dot.className = p.enabled ? "watch-active" : "watch-completed";
    dot.title = p.enabled ? "Enabled" : "Disabled";
    dot.textContent = p.enabled ? "\u25CF active" : "\u25CB disabled";
    colStatus.appendChild(dot);
    row.appendChild(colStatus);

    const colActions = document.createElement("span");
    colActions.className = "admin-col admin-col-actions";
    const editBtn = document.createElement("button");
    editBtn.className = "admin-btn-action";
    editBtn.textContent = "edit";
    editBtn.setAttribute("data-edit-ppolicy", p.policy_id);
    editBtn.setAttribute("aria-label", "Edit prompt " + p.name);
    colActions.appendChild(editBtn);
    const delBtn = document.createElement("button");
    delBtn.className = "admin-btn-danger";
    delBtn.textContent = "delete";
    delBtn.setAttribute("data-delete-ppolicy", p.policy_id);
    delBtn.setAttribute("data-ppolicy-name", p.name);
    delBtn.setAttribute("aria-label", "Delete prompt " + p.name);
    colActions.appendChild(delBtn);
    row.appendChild(colActions);

    el.appendChild(row);
  }
  el.querySelectorAll("[data-edit-ppolicy]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      showEditPromptPolicyModal(this.getAttribute("data-edit-ppolicy"));
    });
  });
  el.querySelectorAll("[data-delete-ppolicy]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      const pid = this.getAttribute("data-delete-ppolicy");
      const pname = this.getAttribute("data-ppolicy-name");
      showConfirmModal(
        "Delete Prompt",
        'Delete prompt "' + pname + '"?',
        "Delete",
        function () {
          authFetch("/v1/api/admin/prompt-policies/" + pid, {
            method: "DELETE",
          })
            .then(function (r) {
              if (!r.ok) throw new Error();
              return r.json();
            })
            .then(function () {
              showToast("Prompt deleted");
              loadPromptPolicies();
            })
            .catch(function () {
              showToast("Failed to delete prompt");
            });
        },
      );
    });
  });
}

function showCreatePromptPolicyModal() {
  _cppTriggerEl = document.activeElement;
  const ov = document.getElementById("create-ppolicy-overlay");
  ov.style.display = "flex";
  document.getElementById("cpp-name").value = "";
  document.getElementById("cpp-gate").value = "";
  document.getElementById("cpp-content").value = "";
  document.getElementById("cpp-priority").value = "0";
  document.getElementById("cpp-error").classList.remove("is-visible");
  document.getElementById("cpp-name").focus();
  _cppTrapHandler = _installTrap(
    "create-ppolicy-overlay",
    "create-ppolicy-box",
  );
}

function hideCreatePromptPolicyModal() {
  document.getElementById("create-ppolicy-overlay").style.display = "none";
  _cppTrapHandler = _removeTrap(_cppTrapHandler);
  if (_cppTriggerEl && _cppTriggerEl.focus) {
    _cppTriggerEl.focus();
  }
  _cppTriggerEl = null;
}

function submitCreatePromptPolicy() {
  const errEl = document.getElementById("cpp-error");
  const name = document.getElementById("cpp-name").value.trim();
  const content = document.getElementById("cpp-content").value.trim();
  if (!name || !content) {
    errEl.textContent = "Name and content are required";
    errEl.classList.add("is-visible");
    return;
  }
  errEl.classList.remove("is-visible");
  const submitBtn = document.getElementById("cpp-submit");
  submitBtn.disabled = true;
  authFetch("/v1/api/admin/prompt-policies", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      name: name,
      content: content,
      tool_gate: document.getElementById("cpp-gate").value.trim(),
      priority:
        parseInt(document.getElementById("cpp-priority").value, 10) || 0,
      enabled: true,
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
      hideCreatePromptPolicyModal();
      showToast("Prompt created");
      loadPromptPolicies();
    })
    .catch(function (e) {
      errEl.textContent = e.message;
      errEl.classList.add("is-visible");
    })
    .finally(function () {
      submitBtn.disabled = false;
    });
}

function showEditPromptPolicyModal(policyId) {
  _eppTriggerEl = document.activeElement;
  let p = null;
  for (let i = 0; i < _promptPolicies.length; i++) {
    if (_promptPolicies[i].policy_id === policyId) {
      p = _promptPolicies[i];
      break;
    }
  }
  if (!p) return;
  document.getElementById("epp-id").value = p.policy_id;
  document.getElementById("epp-name").value = p.name;
  document.getElementById("epp-gate").value = p.tool_gate || "";
  document.getElementById("epp-content").value = p.content || "";
  document.getElementById("epp-priority").value = p.priority || 0;
  document.getElementById("epp-enabled").checked = p.enabled;
  document.getElementById("epp-error").classList.remove("is-visible");
  const ov = document.getElementById("edit-ppolicy-overlay");
  ov.style.display = "flex";
  document.getElementById("epp-name").focus();
  _eppTrapHandler = _installTrap("edit-ppolicy-overlay", "edit-ppolicy-box");
}

function hideEditPromptPolicyModal() {
  document.getElementById("edit-ppolicy-overlay").style.display = "none";
  _eppTrapHandler = _removeTrap(_eppTrapHandler);
  if (_eppTriggerEl && _eppTriggerEl.focus) {
    _eppTriggerEl.focus();
  }
  _eppTriggerEl = null;
}

function submitEditPromptPolicy() {
  const errEl = document.getElementById("epp-error");
  const policyId = document.getElementById("epp-id").value;
  const name = document.getElementById("epp-name").value.trim();
  const content = document.getElementById("epp-content").value.trim();
  if (!name || !content) {
    errEl.textContent = "Name and content are required";
    errEl.classList.add("is-visible");
    return;
  }
  errEl.classList.remove("is-visible");
  const submitBtn = document.getElementById("epp-submit");
  submitBtn.disabled = true;
  authFetch("/v1/api/admin/prompt-policies/" + policyId, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      name: name,
      content: content,
      tool_gate: document.getElementById("epp-gate").value.trim(),
      priority:
        parseInt(document.getElementById("epp-priority").value, 10) || 0,
      enabled: document.getElementById("epp-enabled").checked,
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
      hideEditPromptPolicyModal();
      showToast("Prompt updated");
      loadPromptPolicies();
    })
    .catch(function (e) {
      errEl.textContent = e.message;
      errEl.classList.add("is-visible");
    })
    .finally(function () {
      submitBtn.disabled = false;
    });
}

// ---------------------------------------------------------------------------
// Judge tab — settings, heuristic rules, output guard patterns
// ---------------------------------------------------------------------------

let _judgeSettings = [];
let _judgeHeuristicRules = [];
let _judgeOGPatterns = [];
let _judgeModelDefs = [];
let _chrTrapHandler = null; // create heuristic rule
let _cogpTrapHandler = null; // create output guard pattern
let _chrTriggerEl = null;
let _cogpTriggerEl = null;
let _ehrTrapHandler = null; // edit heuristic rule
let _eogpTrapHandler = null; // edit output guard pattern
let _ehrTriggerEl = null;
let _eogpTriggerEl = null;

// -- Sub-section switcher ---------------------------------------------------

function switchJudgeSection(section) {
  const sections = document.querySelectorAll(".judge-section");
  for (let i = 0; i < sections.length; i++) sections[i].style.display = "none";
  const switcher = document.querySelector(
    "#admin-judge .admin-subtab-switcher",
  );
  const btns = switcher ? switcher.querySelectorAll(".admin-subtab-btn") : [];
  for (let i = 0; i < btns.length; i++) {
    const isActive = btns[i].getAttribute("data-section") === section;
    btns[i].classList.toggle("active", isActive);
    btns[i].setAttribute("aria-selected", isActive ? "true" : "false");
    btns[i].setAttribute("tabindex", isActive ? "0" : "-1");
  }
  const target = document.getElementById(section + "-section");
  if (target) target.style.display = "";
}

// Arrow key navigation for judge sub-section tabs
(function () {
  const switcher = document.querySelector(
    "#admin-judge .admin-subtab-switcher",
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
    switchJudgeSection(secs[idx]);
    btns[idx].focus();
  });
})();

// -- Load all judge data ----------------------------------------------------

function loadJudgeTab() {
  loadJudgeHeuristicRules();
  loadJudgeOGPatterns();
  // Load model definitions before settings (settings render needs the model list)
  authFetch("/v1/api/admin/model-definitions")
    .then(function (r) {
      if (!r.ok) throw new Error("Failed");
      return r.json();
    })
    .then(function (d) {
      _judgeModelDefs = d.models || [];
    })
    .catch(function () {
      _judgeModelDefs = [];
    })
    .finally(function () {
      loadJudgeSettings();
    });
}

// -- Settings section -------------------------------------------------------

function loadJudgeSettings() {
  authFetch("/v1/api/admin/judge/settings")
    .then(function (r) {
      if (!r.ok) throw new Error("Failed");
      return r.json();
    })
    .then(function (d) {
      _judgeSettings = d.settings || [];
      renderJudgeSettings();
    })
    .catch(function () {
      setSafeHtml(
        document.getElementById("judge-settings-container"),
        '<div class="dashboard-empty">Failed to load settings</div>',
      );
    });
}

function renderJudgeSettings() {
  const c = document.getElementById("judge-settings-container");
  if (!_judgeSettings.length) {
    setSafeHtml(
      c,
      '<div class="dashboard-empty">No judge settings found</div>',
    );
    return;
  }
  let html = "";
  for (let i = 0; i < _judgeSettings.length; i++) {
    const s = _judgeSettings[i];
    if (s.key === "judge.model") continue;
    const shortKey = s.key.replace("judge.", "");
    let inputHtml = "";
    const currentVal = s.value;
    const isDefault = s.source === "default";

    const eKey = escapeHtml(s.key);
    if (s.type === "bool") {
      // Toggle switch — same component used by the admin modals.
      // The delegated change handler (wired after setSafeHtml below)
      // reads ``data-judge-key`` and writes via saveJudgeSetting.  The
      // ``.toggle-label`` is a static "Enabled" because the slider
      // position is the truth — flipping the caption text on save
      // round-tripped through reload, so the slider moved instantly
      // while the caption lagged 50-300ms and looked broken.
      // ``aria-label`` carries the setting name so screen readers get
      // the row context inline.
      inputHtml =
        '<label class="toggle-switch toggle--flush">' +
        '<input type="checkbox" data-judge-key="' +
        eKey +
        '" aria-label="' +
        escapeHtml(shortKey) +
        '" ' +
        (currentVal ? "checked" : "") +
        ">" +
        '<span class="toggle-track" aria-hidden="true"></span>' +
        '<span class="toggle-label">Enabled</span></label>';
    } else if (s.type === "float") {
      // currentVal/min/max are expected to be numeric, but
      // ``admin_list_judge_settings`` can fall back to returning the
      // raw stored string when deserialization fails — escape +
      // stringify defensively so a non-numeric fallback can't break
      // out of the value/min/max attribute boundary.
      inputHtml =
        '<div style="display:flex;gap:8px;align-items:center">' +
        '<input type="number" step="0.01" data-judge-key="' +
        eKey +
        '" value="' +
        escapeHtml(String(currentVal != null ? currentVal : "")) +
        '"' +
        (s.min_value != null
          ? ' min="' + escapeHtml(String(s.min_value)) + '"'
          : "") +
        (s.max_value != null
          ? ' max="' + escapeHtml(String(s.max_value)) + '"'
          : "") +
        ' style="width:100px;padding:4px 8px;background:var(--bg);border:1px solid var(--border-strong);color:var(--fg);border-radius:3px">' +
        '<button class="admin-action-btn" data-judge-save-key="' +
        eKey +
        '">Save</button></div>';
    } else if (s.is_secret) {
      inputHtml =
        '<div style="display:flex;gap:8px;align-items:center">' +
        '<input type="password" data-judge-key="' +
        eKey +
        '" value="' +
        escapeHtml(currentVal || "") +
        '" placeholder="(not set)"' +
        ' style="width:240px;padding:4px 8px;background:var(--bg);border:1px solid var(--border-strong);color:var(--fg);border-radius:3px">' +
        '<button class="admin-action-btn" data-judge-save-key="' +
        eKey +
        '">Save</button></div>';
    } else {
      inputHtml =
        '<div style="display:flex;gap:8px;align-items:center">' +
        '<input type="text" data-judge-key="' +
        eKey +
        '" value="' +
        escapeHtml(currentVal || "") +
        '"' +
        ' style="width:240px;padding:4px 8px;background:var(--bg);border:1px solid var(--border-strong);color:var(--fg);border-radius:3px">' +
        '<button class="admin-action-btn" data-judge-save-key="' +
        eKey +
        '">Save</button></div>';
    }

    const resetBtn = !isDefault
      ? ' <button class="admin-action-btn" style="font-size:11px;padding:2px 6px" data-judge-reset-key="' +
        eKey +
        '">Reset</button>'
      : "";

    html +=
      '<div style="margin-bottom:12px;padding-bottom:10px;border-bottom:1px solid var(--border-strong)">' +
      '<div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">' +
      '<code style="font-size:12px;font-weight:600;color:var(--fg)">' +
      shortKey +
      "</code>" +
      (isDefault
        ? '<span style="font-size:11px;color:var(--fg-dim)">default</span>'
        : '<span style="font-size:11px;color:var(--green)">customized</span>') +
      resetBtn +
      "</div>" +
      '<div style="font-size:11px;color:var(--fg-dim);margin-bottom:5px">' +
      escapeHtml(s.help || s.description || "") +
      "</div>" +
      inputHtml +
      "</div>";
  }
  setSafeHtml(c, html);

  // Wire delegated handlers via data-* attributes — keys flow through
  // setAttribute (escapeHtml'd in the HTML builder) and are read back
  // via getAttribute, avoiding the prior ``onclick="..._('KEY')"``
  // pattern that embedded a JS-string inside an HTML-attribute and
  // would break out if a key ever contained an apostrophe.  Bool
  // checkboxes auto-save on change; text/number/password inputs are
  // paired with an explicit Save button (no per-keystroke save).
  const inputs = c.querySelectorAll("[data-judge-key]");
  for (let ii = 0; ii < inputs.length; ii++) {
    const inp = inputs[ii];
    if (inp.type === "checkbox") {
      inp.addEventListener("change", function () {
        saveJudgeSetting(this.getAttribute("data-judge-key"), this.checked);
      });
    }
    // text/number/password: no auto-save listener — the adjacent Save
    // button (data-judge-save-key) is the commit gesture.
  }
  const saveBtns = c.querySelectorAll("[data-judge-save-key]");
  for (let sb = 0; sb < saveBtns.length; sb++) {
    saveBtns[sb].addEventListener("click", function () {
      saveJudgeSettingFromInput(this.getAttribute("data-judge-save-key"));
    });
  }
  const resetBtns = c.querySelectorAll("[data-judge-reset-key]");
  for (let rb = 0; rb < resetBtns.length; rb++) {
    resetBtns[rb].addEventListener("click", function () {
      resetJudgeSetting(this.getAttribute("data-judge-reset-key"));
    });
  }
}

function saveJudgeSetting(key, value) {
  authFetch("/v1/api/admin/judge/settings/" + encodeURIComponent(key), {
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
      showToast("Setting saved");
      loadJudgeSettings();
    })
    .catch(function (e) {
      showToast("Error: " + e.message);
    });
}

function saveJudgeSettingFromInput(key) {
  // ``cssEscape`` (shared/utils.js) defends against keys that contain
  // CSS attribute-selector metacharacters (``"``, ``\``).  Keys today
  // come from a static server-side registry without those chars, but
  // the helper is cheap and makes the selector future-proof.
  const input = document.querySelector(
    '[data-judge-key="' + cssEscape(key) + '"]',
  );
  if (!input) return;
  saveJudgeSetting(key, input.value);
}

function resetJudgeSetting(key) {
  authFetch("/v1/api/admin/judge/settings/" + encodeURIComponent(key), {
    method: "DELETE",
  })
    .then(function (r) {
      if (!r.ok)
        return r.json().then(function (d) {
          throw new Error(d.error || "Failed");
        });
      return r.json();
    })
    .then(function () {
      showToast("Reset to default");
      loadJudgeSettings();
    })
    .catch(function (e) {
      showToast("Error: " + e.message);
    });
}

// -- Heuristic Rules section ------------------------------------------------

function loadJudgeHeuristicRules() {
  authFetch("/v1/api/admin/judge/heuristic-rules")
    .then(function (r) {
      if (!r.ok) throw new Error("Failed");
      return r.json();
    })
    .then(function (d) {
      _judgeHeuristicRules = d.rules || [];
      renderHeuristicRules();
    })
    .catch(function () {
      setSafeHtml(
        document.getElementById("judge-heuristic-table-container"),
        '<div class="dashboard-empty">Failed to load rules</div>',
      );
    });
}

function renderHeuristicRules() {
  const c = document.getElementById("judge-heuristic-table-container");
  if (!_judgeHeuristicRules.length) {
    setSafeHtml(c, '<div class="dashboard-empty">No rules found</div>');
    return;
  }
  let html = "";
  for (let i = 0; i < _judgeHeuristicRules.length; i++) {
    const r = _judgeHeuristicRules[i];
    const sourceBadge =
      r.source === "builtin"
        ? '<span class="scope-badge">built-in</span>'
        : r.source === "builtin-overridden"
          ? '<span class="scope-badge scope-channel">modified</span>'
          : r.source === "builtin-disabled"
            ? '<span class="scope-badge">built-in</span>'
            : '<span class="scope-badge scope-write">custom</span>';
    const statusBadge = r.enabled
      ? '<span class="scope-badge scope-scan-safe">active</span>'
      : '<span class="scope-badge scope-deny">disabled</span>';
    let actions = "";
    const eName = escapeHtml(r.name);
    if (!r.rule_id) {
      // Pure built-in: Disable + Edit
      actions =
        '<button class="admin-btn-action" data-disable-builtin-hr="' +
        eName +
        '" aria-label="Disable ' +
        eName +
        '">Disable</button> ' +
        '<button class="admin-btn-action" data-edit-hr-builtin="' +
        eName +
        '" aria-label="Edit ' +
        eName +
        '">Edit</button>';
    } else if (r.builtin) {
      // Overridden or disabled built-in: Enable/Disable + Edit + Reset
      actions =
        '<button class="admin-btn-action" data-toggle-hr="' +
        r.rule_id +
        '" data-enabled="' +
        !r.enabled +
        '" aria-label="' +
        (r.enabled ? "Disable" : "Enable") +
        " " +
        eName +
        '">' +
        (r.enabled ? "Disable" : "Enable") +
        "</button> " +
        '<button class="admin-btn-action" data-edit-hr="' +
        r.rule_id +
        '" aria-label="Edit ' +
        eName +
        '">Edit</button> ' +
        '<button class="admin-btn-caution" data-reset-hr="' +
        r.rule_id +
        '" aria-label="Reset ' +
        eName +
        '">Reset</button>';
    } else {
      // Custom rule: Enable/Disable + Edit + Delete
      actions =
        '<button class="admin-btn-action" data-toggle-hr="' +
        r.rule_id +
        '" data-enabled="' +
        !r.enabled +
        '" aria-label="' +
        (r.enabled ? "Disable" : "Enable") +
        " " +
        eName +
        '">' +
        (r.enabled ? "Disable" : "Enable") +
        "</button> " +
        '<button class="admin-btn-action" data-edit-hr="' +
        r.rule_id +
        '" aria-label="Edit ' +
        eName +
        '">Edit</button> ' +
        '<button class="admin-btn-danger" data-delete-hr="' +
        r.rule_id +
        '" aria-label="Delete ' +
        eName +
        '">Delete</button>';
    }
    html +=
      '<div class="admin-row" role="listitem">' +
      '<span class="admin-col"><code>' +
      escapeHtml(r.name) +
      "</code></span>" +
      '<span class="admin-col admin-col-htier">' +
      escapeHtml(r.tier || r.risk_level) +
      "</span>" +
      '<span class="admin-col admin-col-hrisk">' +
      escapeHtml(r.risk_level) +
      "</span>" +
      '<span class="admin-col"><code>' +
      escapeHtml(r.tool_pattern) +
      "</code></span>" +
      '<span class="admin-col admin-col-hrec">' +
      escapeHtml(r.recommendation) +
      "</span>" +
      '<span class="admin-col">' +
      sourceBadge +
      "</span>" +
      '<span class="admin-col">' +
      statusBadge +
      "</span>" +
      '<span class="admin-col">' +
      actions +
      "</span></div>";
  }
  setSafeHtml(c, html);
  // Bind data-attribute event handlers
  c.querySelectorAll("[data-disable-builtin-hr]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      disableBuiltinHeuristicRule(this.getAttribute("data-disable-builtin-hr"));
    });
  });
  c.querySelectorAll("[data-toggle-hr]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      toggleHeuristicRule(
        this.getAttribute("data-toggle-hr"),
        this.getAttribute("data-enabled") === "true",
      );
    });
  });
  c.querySelectorAll("[data-edit-hr-builtin]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      showEditBuiltinHeuristicRuleModal(
        this.getAttribute("data-edit-hr-builtin"),
      );
    });
  });
  c.querySelectorAll("[data-edit-hr]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      showEditHeuristicRuleModal(this.getAttribute("data-edit-hr"));
    });
  });
  c.querySelectorAll("[data-reset-hr]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      resetHeuristicRule(this.getAttribute("data-reset-hr"));
    });
  });
  c.querySelectorAll("[data-delete-hr]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      deleteHeuristicRule(this.getAttribute("data-delete-hr"));
    });
  });
}

function toggleHeuristicRule(ruleId, enabled) {
  authFetch("/v1/api/admin/judge/heuristic-rules/" + ruleId, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled: enabled }),
  })
    .then(function (r) {
      if (!r.ok)
        return r.json().then(function (d) {
          throw new Error(d.error || "Failed");
        });
      return r.json();
    })
    .then(function () {
      showToast(enabled ? "Rule enabled" : "Rule disabled");
      loadJudgeHeuristicRules();
    })
    .catch(function (e) {
      showToast("Error: " + e.message);
    });
}

function deleteHeuristicRule(ruleId) {
  let ruleName = "";
  for (let j = 0; j < _judgeHeuristicRules.length; j++) {
    if (_judgeHeuristicRules[j].rule_id === ruleId) {
      ruleName = _judgeHeuristicRules[j].name;
      break;
    }
  }
  showConfirmModal(
    "Delete Rule",
    'Delete custom rule "' + ruleName + '"? This action cannot be undone.',
    "Delete",
    function () {
      authFetch("/v1/api/admin/judge/heuristic-rules/" + ruleId, {
        method: "DELETE",
      })
        .then(function (r) {
          if (!r.ok)
            return r.json().then(function (d) {
              throw new Error(d.error || "Failed");
            });
          return r.json();
        })
        .then(function () {
          showToast("Rule deleted");
          loadJudgeHeuristicRules();
        })
        .catch(function (e) {
          showToast("Error: " + e.message);
        });
    },
  );
}

function showCreateHeuristicRuleModal() {
  _chrTriggerEl = document.activeElement;
  const ov = document.getElementById("create-hr-overlay");
  ov.style.display = "flex";
  document.getElementById("hr-name").value = "";
  document.getElementById("hr-tier").value = "medium";
  document.getElementById("hr-risk").value = "medium";
  document.getElementById("hr-rec").value = "review";
  document.getElementById("hr-tool").value = "bash";
  document.getElementById("hr-args").value = "";
  document.getElementById("hr-conf").value = "0.8";
  document.getElementById("hr-intent").value = "";
  document.getElementById("hr-reason").value = "";
  document.getElementById("create-hr-error").classList.remove("is-visible");
  document.getElementById("hr-submit").disabled = false;
  document.getElementById("hr-name").focus();
  _chrTrapHandler = _installTrap("create-hr-overlay", "create-hr-box");
}

function hideCreateHRModal() {
  document.getElementById("create-hr-overlay").style.display = "none";
  _chrTrapHandler = _removeTrap(_chrTrapHandler);
  if (_chrTriggerEl && _chrTriggerEl.focus) _chrTriggerEl.focus();
  _chrTriggerEl = null;
}

function submitCreateHeuristicRule() {
  const errEl = document.getElementById("create-hr-error");
  errEl.classList.remove("is-visible");
  const argsText = document.getElementById("hr-args").value.trim();
  const argPatterns = argsText
    ? argsText.split("\n").filter(function (l) {
        return l.trim();
      })
    : [];
  const payload = {
    name: document.getElementById("hr-name").value.trim(),
    tier: document.getElementById("hr-tier").value,
    risk_level: document.getElementById("hr-risk").value,
    recommendation: document.getElementById("hr-rec").value,
    tool_pattern: document.getElementById("hr-tool").value.trim(),
    arg_patterns: argPatterns,
    confidence: parseFloat(document.getElementById("hr-conf").value) || 0.8,
    intent_template: document.getElementById("hr-intent").value.trim(),
    reasoning_template: document.getElementById("hr-reason").value.trim(),
    enabled: true,
  };
  const btn = document.getElementById("hr-submit");
  btn.disabled = true;
  authFetch("/v1/api/admin/judge/heuristic-rules", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  })
    .then(function (r) {
      if (!r.ok)
        return r.json().then(function (d) {
          throw new Error(d.error || "Failed");
        });
      return r.json();
    })
    .then(function () {
      hideCreateHRModal();
      showToast("Rule created");
      loadJudgeHeuristicRules();
    })
    .catch(function (e) {
      errEl.textContent = e.message;
      errEl.classList.add("is-visible");
    })
    .finally(function () {
      btn.disabled = false;
    });
}

// -- Heuristic Rule: disable / edit / reset ---------------------------------

function disableBuiltinHeuristicRule(name) {
  let rule = null;
  for (let i = 0; i < _judgeHeuristicRules.length; i++) {
    if (_judgeHeuristicRules[i].name === name) {
      rule = _judgeHeuristicRules[i];
      break;
    }
  }
  if (!rule) return;
  const payload = {
    name: rule.name,
    risk_level: rule.risk_level,
    confidence: rule.confidence,
    recommendation: rule.recommendation,
    tool_pattern: rule.tool_pattern,
    arg_patterns: rule.arg_patterns,
    intent_template: rule.intent_template || "",
    reasoning_template: rule.reasoning_template || "",
    tier: rule.tier || rule.risk_level,
    priority: rule.priority || 0,
    builtin: true,
    enabled: false,
  };
  authFetch("/v1/api/admin/judge/heuristic-rules", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  })
    .then(function (r) {
      if (!r.ok)
        return r.json().then(function (d) {
          throw new Error(d.error || "Failed");
        });
      return r.json();
    })
    .then(function () {
      showToast("Built-in rule disabled \u2014 Reset to restore defaults");
      loadJudgeHeuristicRules();
    })
    .catch(function (e) {
      showToast("Error: " + e.message);
    });
}

function resetHeuristicRule(ruleId) {
  let ruleName = "";
  for (let j = 0; j < _judgeHeuristicRules.length; j++) {
    if (_judgeHeuristicRules[j].rule_id === ruleId) {
      ruleName = _judgeHeuristicRules[j].name;
      break;
    }
  }
  showConfirmModal(
    "Reset to Built-in",
    'Reset "' +
      ruleName +
      '" to its built-in defaults? Your customizations will be removed.',
    "Reset",
    function () {
      authFetch("/v1/api/admin/judge/heuristic-rules/" + ruleId, {
        method: "DELETE",
      })
        .then(function (r) {
          if (!r.ok)
            return r.json().then(function (d) {
              throw new Error(d.error || "Failed");
            });
          return r.json();
        })
        .then(function () {
          showToast("Rule reset to built-in defaults");
          loadJudgeHeuristicRules();
        })
        .catch(function (e) {
          showToast("Error: " + e.message);
        });
    },
  );
}

function _populateEditHRModal(rule, isBuiltin) {
  document.getElementById("ehr-id").value = rule.rule_id || "";
  document.getElementById("ehr-builtin").value = isBuiltin ? "true" : "false";
  document.getElementById("ehr-priority").value = rule.priority || 0;
  document.getElementById("ehr-name").value = rule.name;
  document.getElementById("ehr-name").disabled = isBuiltin;
  document.getElementById("ehr-tier").value = rule.tier || rule.risk_level;
  document.getElementById("ehr-risk").value = rule.risk_level;
  document.getElementById("ehr-rec").value = rule.recommendation;
  document.getElementById("ehr-tool").value = rule.tool_pattern;
  // arg_patterns comes as JSON string from API
  let args = rule.arg_patterns || "[]";
  if (typeof args === "string") {
    try {
      args = JSON.parse(args);
    } catch (e) {
      args = [];
    }
  }
  document.getElementById("ehr-args").value = args.join("\n");
  document.getElementById("ehr-conf").value = rule.confidence;
  document.getElementById("ehr-intent").value = rule.intent_template || "";
  document.getElementById("ehr-reason").value = rule.reasoning_template || "";
  document.getElementById("edit-hr-error").classList.remove("is-visible");
  document.getElementById("ehr-submit").disabled = false;
}

function showEditHeuristicRuleModal(ruleId) {
  _ehrTriggerEl = document.activeElement;
  let rule = null;
  for (let i = 0; i < _judgeHeuristicRules.length; i++) {
    if (_judgeHeuristicRules[i].rule_id === ruleId) {
      rule = _judgeHeuristicRules[i];
      break;
    }
  }
  if (!rule) return;
  _populateEditHRModal(rule, !!rule.builtin);
  const ov = document.getElementById("edit-hr-overlay");
  ov.style.display = "flex";
  document.getElementById("ehr-tier").focus();
  _ehrTrapHandler = _installTrap("edit-hr-overlay", "edit-hr-box");
}

function showEditBuiltinHeuristicRuleModal(name) {
  _ehrTriggerEl = document.activeElement;
  let rule = null;
  for (let i = 0; i < _judgeHeuristicRules.length; i++) {
    if (
      _judgeHeuristicRules[i].name === name &&
      !_judgeHeuristicRules[i].rule_id
    ) {
      rule = _judgeHeuristicRules[i];
      break;
    }
  }
  if (!rule) return;
  _populateEditHRModal(rule, true);
  const ov = document.getElementById("edit-hr-overlay");
  ov.style.display = "flex";
  document.getElementById("ehr-tier").focus();
  _ehrTrapHandler = _installTrap("edit-hr-overlay", "edit-hr-box");
}

function hideEditHRModal() {
  document.getElementById("edit-hr-overlay").style.display = "none";
  _ehrTrapHandler = _removeTrap(_ehrTrapHandler);
  if (_ehrTriggerEl && _ehrTriggerEl.focus) _ehrTriggerEl.focus();
  _ehrTriggerEl = null;
}

function submitEditHeuristicRule() {
  const errEl = document.getElementById("edit-hr-error");
  errEl.classList.remove("is-visible");
  const argsText = document.getElementById("ehr-args").value.trim();
  const argPatterns = argsText
    ? argsText.split("\n").filter(function (l) {
        return l.trim();
      })
    : [];
  const ruleId = document.getElementById("ehr-id").value;
  const payload = {
    name: document.getElementById("ehr-name").value.trim(),
    tier: document.getElementById("ehr-tier").value,
    risk_level: document.getElementById("ehr-risk").value,
    recommendation: document.getElementById("ehr-rec").value,
    tool_pattern: document.getElementById("ehr-tool").value.trim(),
    arg_patterns: argPatterns,
    confidence: parseFloat(document.getElementById("ehr-conf").value) || 0.8,
    intent_template: document.getElementById("ehr-intent").value.trim(),
    reasoning_template: document.getElementById("ehr-reason").value.trim(),
    priority: parseInt(document.getElementById("ehr-priority").value, 10) || 0,
  };
  const btn = document.getElementById("ehr-submit");
  btn.disabled = true;

  let url, method;
  if (ruleId) {
    // Existing DB row — update in place
    url = "/v1/api/admin/judge/heuristic-rules/" + ruleId;
    method = "PUT";
  } else {
    // Pure built-in first edit — create override
    url = "/v1/api/admin/judge/heuristic-rules";
    method = "POST";
    payload.builtin = true;
    payload.enabled = true;
  }
  authFetch(url, {
    method: method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  })
    .then(function (r) {
      if (!r.ok)
        return r.json().then(function (d) {
          throw new Error(d.error || "Failed");
        });
      return r.json();
    })
    .then(function () {
      hideEditHRModal();
      showToast(ruleId ? "Rule updated" : "Rule overridden");
      loadJudgeHeuristicRules();
    })
    .catch(function (e) {
      errEl.textContent = e.message;
      errEl.classList.add("is-visible");
    })
    .finally(function () {
      btn.disabled = false;
    });
}

// -- Output Guard Patterns section ------------------------------------------

function loadJudgeOGPatterns() {
  authFetch("/v1/api/admin/judge/output-guard-patterns")
    .then(function (r) {
      if (!r.ok) throw new Error("Failed");
      return r.json();
    })
    .then(function (d) {
      _judgeOGPatterns = d.patterns || [];
      renderOGPatterns();
    })
    .catch(function () {
      setSafeHtml(
        document.getElementById("judge-og-table-container"),
        '<div class="dashboard-empty">Failed to load patterns</div>',
      );
    });
}

function renderOGPatterns() {
  const c = document.getElementById("judge-og-table-container");
  if (!_judgeOGPatterns.length) {
    setSafeHtml(c, '<div class="dashboard-empty">No patterns found</div>');
    return;
  }
  let html = "";
  for (let i = 0; i < _judgeOGPatterns.length; i++) {
    const p = _judgeOGPatterns[i];
    const sourceBadge =
      p.source === "builtin"
        ? '<span class="scope-badge">built-in</span>'
        : p.source === "builtin-overridden"
          ? '<span class="scope-badge scope-channel">modified</span>'
          : p.source === "builtin-disabled"
            ? '<span class="scope-badge">built-in</span>'
            : '<span class="scope-badge scope-write">custom</span>';
    const statusBadge = p.enabled
      ? '<span class="scope-badge scope-scan-safe">active</span>'
      : '<span class="scope-badge scope-deny">disabled</span>';
    let actions = "";
    const eName = escapeHtml(p.name);
    if (!p.pattern_id) {
      // Pure built-in: Disable + Edit
      actions =
        '<button class="admin-btn-action" data-disable-builtin-ogp="' +
        eName +
        '" aria-label="Disable ' +
        eName +
        '">Disable</button> ' +
        '<button class="admin-btn-action" data-edit-ogp-builtin="' +
        eName +
        '" aria-label="Edit ' +
        eName +
        '">Edit</button>';
    } else if (p.builtin) {
      // Overridden or disabled built-in: Enable/Disable + Edit + Reset
      actions =
        '<button class="admin-btn-action" data-toggle-ogp="' +
        p.pattern_id +
        '" data-enabled="' +
        !p.enabled +
        '" aria-label="' +
        (p.enabled ? "Disable" : "Enable") +
        " " +
        eName +
        '">' +
        (p.enabled ? "Disable" : "Enable") +
        "</button> " +
        '<button class="admin-btn-action" data-edit-ogp="' +
        p.pattern_id +
        '" aria-label="Edit ' +
        eName +
        '">Edit</button> ' +
        '<button class="admin-btn-caution" data-reset-ogp="' +
        p.pattern_id +
        '" aria-label="Reset ' +
        eName +
        '">Reset</button>';
    } else {
      // Custom rule: Enable/Disable + Edit + Delete
      actions =
        '<button class="admin-btn-action" data-toggle-ogp="' +
        p.pattern_id +
        '" data-enabled="' +
        !p.enabled +
        '" aria-label="' +
        (p.enabled ? "Disable" : "Enable") +
        " " +
        eName +
        '">' +
        (p.enabled ? "Disable" : "Enable") +
        "</button> " +
        '<button class="admin-btn-action" data-edit-ogp="' +
        p.pattern_id +
        '" aria-label="Edit ' +
        eName +
        '">Edit</button> ' +
        '<button class="admin-btn-danger" data-delete-ogp="' +
        p.pattern_id +
        '" aria-label="Delete ' +
        eName +
        '">Delete</button>';
    }
    html +=
      '<div class="admin-row" role="listitem">' +
      '<span class="admin-col"><code>' +
      escapeHtml(p.name) +
      "</code></span>" +
      '<span class="admin-col">' +
      escapeHtml(p.category) +
      "</span>" +
      '<span class="admin-col admin-col-ogrisk">' +
      escapeHtml(p.risk_level) +
      "</span>" +
      '<span class="admin-col admin-col-ogflag"><code>' +
      escapeHtml(p.flag_name) +
      "</code></span>" +
      '<span class="admin-col">' +
      sourceBadge +
      "</span>" +
      '<span class="admin-col">' +
      statusBadge +
      "</span>" +
      '<span class="admin-col">' +
      actions +
      "</span></div>";
  }
  setSafeHtml(c, html);
  // Bind data-attribute event handlers
  c.querySelectorAll("[data-disable-builtin-ogp]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      disableBuiltinOGPattern(this.getAttribute("data-disable-builtin-ogp"));
    });
  });
  c.querySelectorAll("[data-toggle-ogp]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      toggleOGPattern(
        this.getAttribute("data-toggle-ogp"),
        this.getAttribute("data-enabled") === "true",
      );
    });
  });
  c.querySelectorAll("[data-edit-ogp-builtin]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      showEditBuiltinOGPatternModal(this.getAttribute("data-edit-ogp-builtin"));
    });
  });
  c.querySelectorAll("[data-edit-ogp]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      showEditOGPatternModal(this.getAttribute("data-edit-ogp"));
    });
  });
  c.querySelectorAll("[data-reset-ogp]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      resetOGPattern(this.getAttribute("data-reset-ogp"));
    });
  });
  c.querySelectorAll("[data-delete-ogp]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      deleteOGPattern(this.getAttribute("data-delete-ogp"));
    });
  });
}

function toggleOGPattern(patternId, enabled) {
  authFetch("/v1/api/admin/judge/output-guard-patterns/" + patternId, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled: enabled }),
  })
    .then(function (r) {
      if (!r.ok)
        return r.json().then(function (d) {
          throw new Error(d.error || "Failed");
        });
      return r.json();
    })
    .then(function () {
      showToast(enabled ? "Pattern enabled" : "Pattern disabled");
      loadJudgeOGPatterns();
    })
    .catch(function (e) {
      showToast("Error: " + e.message);
    });
}

function deleteOGPattern(patternId) {
  let patName = "";
  for (let j = 0; j < _judgeOGPatterns.length; j++) {
    if (_judgeOGPatterns[j].pattern_id === patternId) {
      patName = _judgeOGPatterns[j].name;
      break;
    }
  }
  showConfirmModal(
    "Delete Pattern",
    'Delete custom pattern "' + patName + '"? This action cannot be undone.',
    "Delete",
    function () {
      authFetch("/v1/api/admin/judge/output-guard-patterns/" + patternId, {
        method: "DELETE",
      })
        .then(function (r) {
          if (!r.ok)
            return r.json().then(function (d) {
              throw new Error(d.error || "Failed");
            });
          return r.json();
        })
        .then(function () {
          showToast("Pattern deleted");
          loadJudgeOGPatterns();
        })
        .catch(function (e) {
          showToast("Error: " + e.message);
        });
    },
  );
}

function showCreateOutputGuardPatternModal() {
  _cogpTriggerEl = document.activeElement;
  const ov = document.getElementById("create-ogp-overlay");
  ov.style.display = "flex";
  document.getElementById("ogp-name").value = "";
  document.getElementById("ogp-cat").value = "prompt_injection";
  document.getElementById("ogp-risk").value = "medium";
  document.getElementById("ogp-pattern").value = "";
  document.getElementById("ogp-flag").value = "";
  document.getElementById("ogp-ann").value = "";
  document.getElementById("ogp-flags").value = "";
  document.getElementById("ogp-cred").checked = false;
  document.getElementById("ogp-redact").value = "";
  document.getElementById("ogp-regex-result").textContent = "";
  document.getElementById("create-ogp-error").classList.remove("is-visible");
  document.getElementById("ogp-submit").disabled = false;
  document.getElementById("ogp-name").focus();
  _cogpTrapHandler = _installTrap("create-ogp-overlay", "create-ogp-box");
}

function hideCreateOGPModal() {
  document.getElementById("create-ogp-overlay").style.display = "none";
  _cogpTrapHandler = _removeTrap(_cogpTrapHandler);
  if (_cogpTriggerEl && _cogpTriggerEl.focus) _cogpTriggerEl.focus();
  _cogpTriggerEl = null;
}

function validateOGRegex() {
  const pattern = document.getElementById("ogp-pattern").value;
  const resultEl = document.getElementById("ogp-regex-result");
  if (!pattern) {
    resultEl.textContent = "";
    return;
  }
  authFetch("/v1/api/admin/judge/validate-regex", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ pattern: pattern }),
  })
    .then(function (r) {
      if (!r.ok) throw new Error("Validation failed");
      return r.json();
    })
    .then(function (d) {
      if (d.valid) {
        resultEl.textContent = "Valid";
        resultEl.style.color = "var(--green)";
      } else {
        resultEl.textContent = d.error || "Invalid";
        resultEl.style.color = "var(--red)";
      }
    })
    .catch(function () {
      resultEl.textContent = "Validation failed";
      resultEl.style.color = "var(--red)";
    });
}

function submitCreateOGPattern() {
  const errEl = document.getElementById("create-ogp-error");
  errEl.classList.remove("is-visible");
  const payload = {
    name: document.getElementById("ogp-name").value.trim(),
    category: document.getElementById("ogp-cat").value,
    risk_level: document.getElementById("ogp-risk").value,
    pattern: document.getElementById("ogp-pattern").value,
    flag_name: document.getElementById("ogp-flag").value.trim(),
    annotation: document.getElementById("ogp-ann").value.trim(),
    pattern_flags: document.getElementById("ogp-flags").value.trim(),
    is_credential: document.getElementById("ogp-cred").checked,
    redact_label: document.getElementById("ogp-redact").value.trim(),
    enabled: true,
  };
  const btn = document.getElementById("ogp-submit");
  btn.disabled = true;
  authFetch("/v1/api/admin/judge/output-guard-patterns", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  })
    .then(function (r) {
      if (!r.ok)
        return r.json().then(function (d) {
          throw new Error(d.error || "Failed");
        });
      return r.json();
    })
    .then(function () {
      hideCreateOGPModal();
      showToast("Pattern created");
      loadJudgeOGPatterns();
    })
    .catch(function (e) {
      errEl.textContent = e.message;
      errEl.classList.add("is-visible");
    })
    .finally(function () {
      btn.disabled = false;
    });
}

// -- Output Guard Pattern: disable / edit / reset ---------------------------

function disableBuiltinOGPattern(name) {
  let pat = null;
  for (let i = 0; i < _judgeOGPatterns.length; i++) {
    if (_judgeOGPatterns[i].name === name) {
      pat = _judgeOGPatterns[i];
      break;
    }
  }
  if (!pat) return;
  const payload = {
    name: pat.name,
    category: pat.category,
    risk_level: pat.risk_level,
    pattern: pat.pattern || "",
    flag_name: pat.flag_name,
    annotation: pat.annotation || "",
    pattern_flags: pat.pattern_flags || "",
    is_credential: pat.is_credential || false,
    redact_label: pat.redact_label || "",
    priority: pat.priority || 0,
    builtin: true,
    enabled: false,
  };
  authFetch("/v1/api/admin/judge/output-guard-patterns", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  })
    .then(function (r) {
      if (!r.ok)
        return r.json().then(function (d) {
          throw new Error(d.error || "Failed");
        });
      return r.json();
    })
    .then(function () {
      showToast("Built-in pattern disabled \u2014 Reset to restore defaults");
      loadJudgeOGPatterns();
    })
    .catch(function (e) {
      showToast("Error: " + e.message);
    });
}

function resetOGPattern(patternId) {
  let patName = "";
  for (let j = 0; j < _judgeOGPatterns.length; j++) {
    if (_judgeOGPatterns[j].pattern_id === patternId) {
      patName = _judgeOGPatterns[j].name;
      break;
    }
  }
  showConfirmModal(
    "Reset to Built-in",
    'Reset "' +
      patName +
      '" to its built-in defaults? Your customizations will be removed.',
    "Reset",
    function () {
      authFetch("/v1/api/admin/judge/output-guard-patterns/" + patternId, {
        method: "DELETE",
      })
        .then(function (r) {
          if (!r.ok)
            return r.json().then(function (d) {
              throw new Error(d.error || "Failed");
            });
          return r.json();
        })
        .then(function () {
          showToast("Pattern reset to built-in defaults");
          loadJudgeOGPatterns();
        })
        .catch(function (e) {
          showToast("Error: " + e.message);
        });
    },
  );
}

function _populateEditOGPModal(pat, isBuiltin) {
  document.getElementById("eogp-id").value = pat.pattern_id || "";
  document.getElementById("eogp-builtin").value = isBuiltin ? "true" : "false";
  document.getElementById("eogp-priority").value = pat.priority || 0;
  document.getElementById("eogp-name").value = pat.name;
  document.getElementById("eogp-name").disabled = isBuiltin;
  document.getElementById("eogp-cat").value = pat.category;
  document.getElementById("eogp-risk").value = pat.risk_level;
  document.getElementById("eogp-pattern").value = pat.pattern || "";
  document.getElementById("eogp-flag").value = pat.flag_name || "";
  document.getElementById("eogp-flag").disabled = isBuiltin;
  document.getElementById("eogp-ann").value = pat.annotation || "";
  document.getElementById("eogp-flags").value = pat.pattern_flags || "";
  document.getElementById("eogp-cred").checked = !!pat.is_credential;
  document.getElementById("eogp-redact").value = pat.redact_label || "";
  document.getElementById("eogp-regex-result").textContent = "";
  document.getElementById("edit-ogp-error").classList.remove("is-visible");
  document.getElementById("eogp-submit").disabled = false;
}

function showEditOGPatternModal(patternId) {
  _eogpTriggerEl = document.activeElement;
  let pat = null;
  for (let i = 0; i < _judgeOGPatterns.length; i++) {
    if (_judgeOGPatterns[i].pattern_id === patternId) {
      pat = _judgeOGPatterns[i];
      break;
    }
  }
  if (!pat) return;
  _populateEditOGPModal(pat, !!pat.builtin);
  const ov = document.getElementById("edit-ogp-overlay");
  ov.style.display = "flex";
  document.getElementById("eogp-cat").focus();
  _eogpTrapHandler = _installTrap("edit-ogp-overlay", "edit-ogp-box");
}

function showEditBuiltinOGPatternModal(name) {
  _eogpTriggerEl = document.activeElement;
  let pat = null;
  for (let i = 0; i < _judgeOGPatterns.length; i++) {
    if (_judgeOGPatterns[i].name === name && !_judgeOGPatterns[i].pattern_id) {
      pat = _judgeOGPatterns[i];
      break;
    }
  }
  if (!pat) return;
  _populateEditOGPModal(pat, true);
  const ov = document.getElementById("edit-ogp-overlay");
  ov.style.display = "flex";
  document.getElementById("eogp-cat").focus();
  _eogpTrapHandler = _installTrap("edit-ogp-overlay", "edit-ogp-box");
}

function hideEditOGPModal() {
  document.getElementById("edit-ogp-overlay").style.display = "none";
  _eogpTrapHandler = _removeTrap(_eogpTrapHandler);
  if (_eogpTriggerEl && _eogpTriggerEl.focus) _eogpTriggerEl.focus();
  _eogpTriggerEl = null;
}

function validateEditOGRegex() {
  const pattern = document.getElementById("eogp-pattern").value;
  const resultEl = document.getElementById("eogp-regex-result");
  if (!pattern) {
    resultEl.textContent = "";
    return;
  }
  authFetch("/v1/api/admin/judge/validate-regex", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ pattern: pattern }),
  })
    .then(function (r) {
      if (!r.ok) throw new Error("Validation failed");
      return r.json();
    })
    .then(function (d) {
      if (d.valid) {
        resultEl.textContent = "Valid";
        resultEl.style.color = "var(--green)";
      } else {
        resultEl.textContent = d.error || "Invalid";
        resultEl.style.color = "var(--red)";
      }
    })
    .catch(function () {
      resultEl.textContent = "Validation failed";
      resultEl.style.color = "var(--red)";
    });
}

function submitEditOGPattern() {
  const errEl = document.getElementById("edit-ogp-error");
  errEl.classList.remove("is-visible");
  const patternId = document.getElementById("eogp-id").value;
  const payload = {
    name: document.getElementById("eogp-name").value.trim(),
    category: document.getElementById("eogp-cat").value,
    risk_level: document.getElementById("eogp-risk").value,
    pattern: document.getElementById("eogp-pattern").value,
    flag_name: document.getElementById("eogp-flag").value.trim(),
    annotation: document.getElementById("eogp-ann").value.trim(),
    pattern_flags: document.getElementById("eogp-flags").value.trim(),
    is_credential: document.getElementById("eogp-cred").checked,
    redact_label: document.getElementById("eogp-redact").value.trim(),
    priority: parseInt(document.getElementById("eogp-priority").value, 10) || 0,
  };
  const btn = document.getElementById("eogp-submit");
  btn.disabled = true;

  let url, method;
  if (patternId) {
    url = "/v1/api/admin/judge/output-guard-patterns/" + patternId;
    method = "PUT";
  } else {
    url = "/v1/api/admin/judge/output-guard-patterns";
    method = "POST";
    payload.builtin = true;
    payload.enabled = true;
  }
  authFetch(url, {
    method: method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  })
    .then(function (r) {
      if (!r.ok)
        return r.json().then(function (d) {
          throw new Error(d.error || "Failed");
        });
      return r.json();
    })
    .then(function () {
      hideEditOGPModal();
      showToast(patternId ? "Pattern updated" : "Pattern overridden");
      loadJudgeOGPatterns();
    })
    .catch(function (e) {
      errEl.textContent = e.message;
      errEl.classList.add("is-visible");
    })
    .finally(function () {
      btn.disabled = false;
    });
}
