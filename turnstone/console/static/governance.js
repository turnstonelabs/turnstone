/* Governance tabs — roles, policies, templates, usage, audit */

// ---------------------------------------------------------------------------
// Module state
// ---------------------------------------------------------------------------
var _govRoles = [];
var _govPolicies = [];
var _govTemplates = [];
var _govWsTemplates = [];
var _govUsageRange = "7d";
var _govUsageGroupBy = "day";
var _govAuditEvents = [];
var _govAuditTotal = 0;
var _govAuditOffset = 0;

// Trap handler refs for modals
var _crTrapHandler = null; // create role
var _erTrapHandler = null; // edit role
var _urTrapHandler = null; // user roles
var _cpTrapHandler = null; // create policy
var _epTrapHandler = null; // edit policy
var _ctmTrapHandler = null; // create template
var _etmTrapHandler = null; // edit template
var _cwstTrapHandler = null; // create ws template
var _ewstTrapHandler = null; // edit ws template

// Trigger element refs for focus restoration
var _crTriggerEl = null;
var _erTriggerEl = null;
var _urTriggerEl = null;
var _cpTriggerEl = null;
var _epTriggerEl = null;
var _ctmTriggerEl = null;
var _etmTriggerEl = null;
var _cwstTriggerEl = null;
var _ewstTriggerEl = null;

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
      document.getElementById("admin-roles-table").innerHTML =
        '<div class="dashboard-empty">Failed to load roles</div>';
    });
}

function _renderGovRoles(items) {
  var el = document.getElementById("admin-roles-table");
  if (!items.length) {
    el.innerHTML = '<div class="dashboard-empty">No roles defined</div>';
    return;
  }
  var html = "";
  for (var i = 0; i < items.length; i++) {
    var r = items[i];
    // Render permissions as badges
    var perms = (r.permissions || "").split(",");
    var badges = "";
    for (var j = 0; j < perms.length; j++) {
      var p = perms[j].trim();
      if (!p) continue;
      var cls = "scope-badge";
      if (p === "approve" || p.indexOf("admin.") === 0) cls += " scope-approve";
      else if (p === "write" || p.indexOf("workstreams.") === 0)
        cls += " scope-write";
      badges += '<span class="' + cls + '">' + escapeHtml(p) + "</span>";
    }
    var typeLabel = r.builtin
      ? '<span class="scope-badge scope-channel">builtin</span>'
      : "";
    var actions = r.builtin
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
  el.innerHTML = html;
  // Bind edit
  var editBtns = el.querySelectorAll("[data-edit-role]");
  for (var k = 0; k < editBtns.length; k++) {
    editBtns[k].addEventListener("click", function () {
      showEditRoleModal(this.getAttribute("data-edit-role"));
    });
  }
  // Bind delete
  var delBtns = el.querySelectorAll("[data-delete-role]");
  for (var k = 0; k < delBtns.length; k++) {
    delBtns[k].addEventListener("click", function () {
      var rid = this.getAttribute("data-delete-role");
      var rname = this.getAttribute("data-role-name");
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

// All permission names for the checkbox UI
var _ALL_PERMISSIONS = [
  "read",
  "write",
  "approve",
  "admin.users",
  "admin.roles",
  "admin.orgs",
  "admin.policies",
  "admin.templates",
  "admin.audit",
  "admin.usage",
  "admin.schedules",
  "admin.watches",
  "tools.approve",
  "workstreams.create",
  "workstreams.close",
];

function _buildPermCheckboxes(prefix, selected) {
  var html = '<div class="perm-grid">';
  for (var i = 0; i < _ALL_PERMISSIONS.length; i++) {
    var p = _ALL_PERMISSIONS[i];
    var checked = selected && selected.indexOf(p) >= 0 ? " checked" : "";
    html +=
      '<label class="perm-checkbox"><input type="checkbox" value="' +
      p +
      '" name="' +
      prefix +
      '-perm"' +
      checked +
      "> " +
      escapeHtml(p) +
      "</label>";
  }
  html += "</div>";
  return html;
}

function _collectPermCheckboxes(prefix) {
  var boxes = document.querySelectorAll(
    'input[name="' + prefix + '-perm"]:checked',
  );
  var perms = [];
  for (var i = 0; i < boxes.length; i++) perms.push(boxes[i].value);
  return perms.join(",");
}

function showCreateRoleModal() {
  _crTriggerEl = document.activeElement;
  var ov = document.getElementById("create-role-overlay");
  ov.style.display = "flex";
  document.getElementById("cr-name").value = "";
  document.getElementById("cr-displayname").value = "";
  document.getElementById("cr-perms-container").innerHTML =
    _buildPermCheckboxes("cr", []);
  document.getElementById("create-role-error").style.display = "none";
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
  var name = document.getElementById("cr-name").value.trim();
  var dname = document.getElementById("cr-displayname").value.trim();
  var perms = _collectPermCheckboxes("cr");
  if (!name) {
    var e = document.getElementById("create-role-error");
    e.textContent = "Name is required";
    e.style.display = "";
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
      var el = document.getElementById("create-role-error");
      el.textContent = e.message;
      el.style.display = "";
    })
    .finally(function () {
      document.getElementById("cr-submit").disabled = false;
    });
}

function showEditRoleModal(roleId) {
  _erTriggerEl = document.activeElement;
  var role = null;
  for (var i = 0; i < _govRoles.length; i++) {
    if (_govRoles[i].role_id === roleId) {
      role = _govRoles[i];
      break;
    }
  }
  if (!role) return;
  var ov = document.getElementById("edit-role-overlay");
  ov.style.display = "flex";
  document.getElementById("er-id").value = roleId;
  document.getElementById("er-name").value = role.display_name;
  var selected = (role.permissions || "").split(",");
  document.getElementById("er-perms-container").innerHTML =
    _buildPermCheckboxes("er", selected);
  document.getElementById("edit-role-error").style.display = "none";
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
  var roleId = document.getElementById("er-id").value;
  var dname = document.getElementById("er-name").value.trim();
  var perms = _collectPermCheckboxes("er");
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
      var el = document.getElementById("edit-role-error");
      el.textContent = e.message;
      el.style.display = "";
    })
    .finally(function () {
      document.getElementById("er-submit").disabled = false;
    });
}

// User roles modal (launched from Users tab)
function showUserRolesModal(userId) {
  _urTriggerEl = document.activeElement;
  var ov = document.getElementById("user-roles-overlay");
  ov.style.display = "flex";
  document.getElementById("ur-user-id").value = userId;
  var container = document.getElementById("ur-roles-container");
  container.innerHTML = '<div class="dashboard-empty">Loading...</div>';
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
      var allRoles = results[0].roles || [];
      var userRoles = results[1].roles || [];
      var assigned = {};
      for (var i = 0; i < userRoles.length; i++)
        assigned[userRoles[i].role_id] = true;
      var html = "";
      for (var j = 0; j < allRoles.length; j++) {
        var r = allRoles[j];
        var checked = assigned[r.role_id] ? " checked" : "";
        html +=
          '<label class="perm-checkbox"><input type="checkbox" value="' +
          escapeHtml(r.role_id) +
          '" name="ur-role"' +
          checked +
          "> " +
          escapeHtml(r.display_name) +
          "</label>";
      }
      container.innerHTML = html;
    })
    .catch(function () {
      container.innerHTML =
        '<div class="dashboard-empty">Failed to load roles</div>';
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
  var userId = document.getElementById("ur-user-id").value;
  var boxes = document.querySelectorAll('input[name="ur-role"]');
  var selected = [];
  for (var i = 0; i < boxes.length; i++) {
    if (boxes[i].checked) selected.push(boxes[i].value);
  }
  // Get current user roles to diff
  authFetch("/v1/api/admin/users/" + userId + "/roles")
    .then(function (r) {
      return r.json();
    })
    .then(function (data) {
      var current = {};
      var roles = data.roles || [];
      for (var i = 0; i < roles.length; i++) current[roles[i].role_id] = true;
      var promises = [];
      // Assign new
      for (var j = 0; j < selected.length; j++) {
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
      var selMap = {};
      for (var k = 0; k < selected.length; k++) selMap[selected[k]] = true;
      for (var rid in current) {
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
      document.getElementById("admin-policies-table").innerHTML =
        '<div class="dashboard-empty">Failed to load policies</div>';
    });
}

function _renderGovPolicies(items) {
  var el = document.getElementById("admin-policies-table");
  if (!items.length) {
    el.innerHTML =
      '<div class="dashboard-empty">No tool policies defined</div>';
    return;
  }
  var html = "";
  for (var i = 0; i < items.length; i++) {
    var p = items[i];
    var actionCls = "policy-badge policy-" + p.action;
    var statusDot = p.enabled
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
  el.innerHTML = html;
  el.querySelectorAll("[data-edit-policy]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      showEditPolicyModal(this.getAttribute("data-edit-policy"));
    });
  });
  el.querySelectorAll("[data-delete-policy]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var pid = this.getAttribute("data-delete-policy");
      var pname = this.getAttribute("data-policy-name");
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
  var ov = document.getElementById("create-policy-overlay");
  ov.style.display = "flex";
  document.getElementById("cp-name").value = "";
  document.getElementById("cp-pattern").value = "";
  document.getElementById("cp-action").value = "ask";
  document.getElementById("cp-priority").value = "0";
  document.getElementById("create-policy-error").style.display = "none";
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
  var name = document.getElementById("cp-name").value.trim();
  var pattern = document.getElementById("cp-pattern").value.trim();
  var action = document.getElementById("cp-action").value;
  var priority =
    parseInt(document.getElementById("cp-priority").value, 10) || 0;
  if (!name || !pattern) {
    var e = document.getElementById("create-policy-error");
    e.textContent = "Name and pattern are required";
    e.style.display = "";
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
      var el = document.getElementById("create-policy-error");
      el.textContent = e.message;
      el.style.display = "";
    })
    .finally(function () {
      document.getElementById("cp-submit").disabled = false;
    });
}

function showEditPolicyModal(policyId) {
  _epTriggerEl = document.activeElement;
  var policy = null;
  for (var i = 0; i < _govPolicies.length; i++) {
    if (_govPolicies[i].policy_id === policyId) {
      policy = _govPolicies[i];
      break;
    }
  }
  if (!policy) return;
  var ov = document.getElementById("edit-policy-overlay");
  ov.style.display = "flex";
  document.getElementById("ep-id").value = policyId;
  document.getElementById("ep-name").value = policy.name;
  document.getElementById("ep-pattern").value = policy.tool_pattern;
  document.getElementById("ep-action").value = policy.action;
  document.getElementById("ep-priority").value = policy.priority;
  document.getElementById("ep-enabled").checked = policy.enabled;
  document.getElementById("edit-policy-error").style.display = "none";
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
  var id = document.getElementById("ep-id").value;
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
      var el = document.getElementById("edit-policy-error");
      el.textContent = e.message;
      el.style.display = "";
    })
    .finally(function () {
      document.getElementById("ep-submit").disabled = false;
    });
}

// ---------------------------------------------------------------------------
// Prompt Templates
// ---------------------------------------------------------------------------

function loadGovTemplates() {
  authFetch("/v1/api/admin/templates")
    .then(function (r) {
      if (!r.ok) throw new Error("Failed");
      return r.json();
    })
    .then(function (data) {
      _govTemplates = data.templates || [];
      _renderGovTemplates(_govTemplates);
    })
    .catch(function () {
      document.getElementById("admin-templates-table").innerHTML =
        '<div class="dashboard-empty">Failed to load templates</div>';
    });
}

function _renderGovTemplates(items) {
  var el = document.getElementById("admin-templates-table");
  if (!items.length) {
    el.innerHTML =
      '<div class="dashboard-empty">No prompt templates defined</div>';
    return;
  }
  var html = "";
  for (var i = 0; i < items.length; i++) {
    var t = items[i];
    var vars = "";
    try {
      var vlist = JSON.parse(t.variables || "[]");
      vars = vlist.join(", ");
    } catch (e) {
      vars = t.variables;
    }
    var defBadge = t.is_default
      ? '<span class="scope-badge scope-approve">default</span>'
      : "";
    var originBadge =
      t.origin === "mcp"
        ? ' <span class="scope-badge scope-deny">mcp:' +
          escapeHtml(t.mcp_server) +
          "</span>"
        : "";
    var catBadge =
      '<span class="scope-badge">' + escapeHtml(t.category) + "</span>";
    var editDisabled = t.readonly ? " disabled" : "";
    var deleteDisabled = t.readonly ? " disabled" : "";
    html +=
      '<div class="admin-row" role="listitem">' +
      '<span class="admin-col admin-col-tmname">' +
      escapeHtml(t.name) +
      " " +
      defBadge +
      originBadge +
      "</span>" +
      '<span class="admin-col admin-col-tmcat">' +
      catBadge +
      "</span>" +
      '<span class="admin-col admin-col-tmvars"><code>' +
      escapeHtml(vars || "\u2014") +
      "</code></span>" +
      '<span class="admin-col admin-col-actions">' +
      '<button class="admin-btn-action" data-edit-tmpl="' +
      escapeHtml(t.template_id) +
      '"' +
      editDisabled +
      ">edit</button>" +
      '<button class="admin-btn-danger" data-delete-tmpl="' +
      escapeHtml(t.template_id) +
      '" data-tmpl-name="' +
      escapeHtml(t.name) +
      '"' +
      deleteDisabled +
      ">delete</button>" +
      "</span></div>";
  }
  el.innerHTML = html;
  el.querySelectorAll("[data-edit-tmpl]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      showEditTemplateModal(this.getAttribute("data-edit-tmpl"));
    });
  });
  el.querySelectorAll("[data-delete-tmpl]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var tid = this.getAttribute("data-delete-tmpl");
      var tname = this.getAttribute("data-tmpl-name");
      showConfirmModal(
        "Delete Template",
        'Delete template "' + tname + '"?',
        "Delete",
        function () {
          authFetch("/v1/api/admin/templates/" + tid, { method: "DELETE" })
            .then(function (r) {
              if (!r.ok) throw new Error();
              return r.json();
            })
            .then(function () {
              showToast("Template deleted");
              loadGovTemplates();
            })
            .catch(function () {
              showToast("Failed to delete template");
            });
        },
      );
    });
  });
}

function _detectTemplateVars(content) {
  var matches = content.match(/\{\{(\w+)\}\}/g) || [];
  var seen = {};
  var result = [];
  for (var i = 0; i < matches.length; i++) {
    var v = matches[i].replace(/[{}]/g, "");
    if (!seen[v]) {
      seen[v] = true;
      result.push(v);
    }
  }
  return result;
}

function _updateVarsDisplay(contentId, displayId) {
  var content = document.getElementById(contentId).value || "";
  var vars = _detectTemplateVars(content);
  document.getElementById(displayId).textContent = vars.length
    ? vars.join(", ")
    : "(none)";
}

function showCreateTemplateModal() {
  _ctmTriggerEl = document.activeElement;
  var ov = document.getElementById("create-template-overlay");
  ov.style.display = "flex";
  document.getElementById("ctm-name").value = "";
  document.getElementById("ctm-category").value = "general";
  document.getElementById("ctm-content").value = "";
  document.getElementById("ctm-variables").textContent = "(none)";
  document.getElementById("ctm-content").oninput = function () {
    _updateVarsDisplay("ctm-content", "ctm-variables");
  };
  document.getElementById("ctm-default").checked = false;
  document.getElementById("create-template-error").style.display = "none";
  document.getElementById("ctm-name").focus();
  _ctmTrapHandler = _installTrap(
    "create-template-overlay",
    "create-template-box",
  );
}

function hideCreateTemplateModal() {
  document.getElementById("create-template-overlay").style.display = "none";
  _ctmTrapHandler = _removeTrap(_ctmTrapHandler);
  if (_ctmTriggerEl && _ctmTriggerEl.focus) {
    _ctmTriggerEl.focus();
  }
  _ctmTriggerEl = null;
}

function submitCreateTemplate() {
  var name = document.getElementById("ctm-name").value.trim();
  var content = document.getElementById("ctm-content").value;
  if (!name || !content) {
    var e = document.getElementById("create-template-error");
    e.textContent = "Name and content are required";
    e.style.display = "";
    return;
  }
  var varList = _detectTemplateVars(content);
  document.getElementById("ctm-submit").disabled = true;
  authFetch("/v1/api/admin/templates", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      name: name,
      category: document.getElementById("ctm-category").value,
      content: content,
      variables: JSON.stringify(varList),
      is_default: document.getElementById("ctm-default").checked,
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
      hideCreateTemplateModal();
      showToast("Template created");
      loadGovTemplates();
    })
    .catch(function (e) {
      var el = document.getElementById("create-template-error");
      el.textContent = e.message;
      el.style.display = "";
    })
    .finally(function () {
      document.getElementById("ctm-submit").disabled = false;
    });
}

function showEditTemplateModal(tmplId) {
  _etmTriggerEl = document.activeElement;
  var tmpl = null;
  for (var i = 0; i < _govTemplates.length; i++) {
    if (_govTemplates[i].template_id === tmplId) {
      tmpl = _govTemplates[i];
      break;
    }
  }
  if (!tmpl) return;
  var ov = document.getElementById("edit-template-overlay");
  ov.style.display = "flex";
  document.getElementById("etm-id").value = tmplId;
  document.getElementById("etm-name").value = tmpl.name;
  document.getElementById("etm-category").value = tmpl.category;
  document.getElementById("etm-content").value = tmpl.content;
  _updateVarsDisplay("etm-content", "etm-variables");
  document.getElementById("etm-content").oninput = function () {
    _updateVarsDisplay("etm-content", "etm-variables");
  };
  document.getElementById("etm-default").checked = tmpl.is_default;
  document.getElementById("edit-template-error").style.display = "none";
  _etmTrapHandler = _installTrap("edit-template-overlay", "edit-template-box");
}

function hideEditTemplateModal() {
  document.getElementById("edit-template-overlay").style.display = "none";
  _etmTrapHandler = _removeTrap(_etmTrapHandler);
  if (_etmTriggerEl && _etmTriggerEl.focus) {
    _etmTriggerEl.focus();
  }
  _etmTriggerEl = null;
}

function submitEditTemplate() {
  var id = document.getElementById("etm-id").value;
  var content = document.getElementById("etm-content").value;
  var varList = _detectTemplateVars(content);
  document.getElementById("etm-submit").disabled = true;
  authFetch("/v1/api/admin/templates/" + id, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      name: document.getElementById("etm-name").value.trim(),
      category: document.getElementById("etm-category").value,
      content: content,
      variables: JSON.stringify(varList),
      is_default: document.getElementById("etm-default").checked,
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
      hideEditTemplateModal();
      showToast("Template updated");
      loadGovTemplates();
    })
    .catch(function (e) {
      var el = document.getElementById("edit-template-error");
      el.textContent = e.message;
      el.style.display = "";
    })
    .finally(function () {
      document.getElementById("etm-submit").disabled = false;
    });
}

// ---------------------------------------------------------------------------
// WS Templates
// ---------------------------------------------------------------------------

function loadGovWsTemplates() {
  authFetch("/v1/api/admin/ws-templates")
    .then(function (r) {
      if (!r.ok) throw new Error("Failed");
      return r.json();
    })
    .then(function (data) {
      _govWsTemplates = data.ws_templates || [];
      _renderGovWsTemplates(_govWsTemplates);
    })
    .catch(function () {
      document.getElementById("admin-ws-templates-table").innerHTML =
        '<div class="dashboard-empty">Failed to load WS templates</div>';
    });
}

function _renderGovWsTemplates(items) {
  var el = document.getElementById("admin-ws-templates-table");
  if (!items.length) {
    el.innerHTML =
      '<div class="dashboard-empty">No workstream templates defined</div>';
    return;
  }
  var html = "";
  for (var i = 0; i < items.length; i++) {
    var t = items[i];
    var modelBadge = t.model
      ? '<span class="scope-badge">' + escapeHtml(t.model) + "</span>"
      : '<span class="scope-badge">default</span>';
    var approveBadge = t.auto_approve
      ? '<span class="scope-badge scope-approve">auto</span>'
      : "";
    var budgetBadge =
      t.token_budget > 0
        ? '<span class="scope-badge scope-deny">' +
          t.token_budget.toLocaleString() +
          "</span>"
        : "";
    var enabledBadge = !t.enabled
      ? ' <span class="scope-badge scope-deny">disabled</span>'
      : "";
    html +=
      '<div class="admin-row" role="listitem">' +
      '<span class="admin-col admin-col-tmname">' +
      escapeHtml(t.name) +
      enabledBadge +
      "</span>" +
      '<span class="admin-col admin-col-tmcat">' +
      modelBadge +
      "</span>" +
      '<span class="admin-col admin-col-tmvars">' +
      approveBadge +
      " " +
      budgetBadge +
      "</span>" +
      '<span class="admin-col admin-col-actions">' +
      "v" +
      t.version +
      " " +
      '<button class="admin-btn-action" data-history-wst="' +
      escapeHtml(t.ws_template_id) +
      '">history</button> ' +
      '<button class="admin-btn-action" data-edit-wst="' +
      escapeHtml(t.ws_template_id) +
      '">edit</button>' +
      '<button class="admin-btn-danger" data-delete-wst="' +
      escapeHtml(t.ws_template_id) +
      '" data-wst-name="' +
      escapeHtml(t.name) +
      '">delete</button>' +
      "</span></div>";
  }
  el.innerHTML = html;
  el.querySelectorAll("[data-edit-wst]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      showEditWsTemplateModal(this.getAttribute("data-edit-wst"));
    });
  });
  el.querySelectorAll("[data-delete-wst]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var tid = this.getAttribute("data-delete-wst");
      var tname = this.getAttribute("data-wst-name");
      showConfirmModal(
        "Delete WS Template",
        'Delete workstream template "' + tname + '"?',
        "Delete",
        function () {
          authFetch("/v1/api/admin/ws-templates/" + tid, {
            method: "DELETE",
          })
            .then(function (r) {
              if (!r.ok) throw new Error();
              return r.json();
            })
            .then(function () {
              showToast("WS template deleted");
              loadGovWsTemplates();
            })
            .catch(function () {
              showToast("Failed to delete WS template");
            });
        },
      );
    });
  });
  el.querySelectorAll("[data-history-wst]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      showWstHistoryModal(this.getAttribute("data-history-wst"));
    });
  });
}

function toggleWstPromptSource() {
  var inline = document.getElementById("cwst-src-inline").checked;
  document.getElementById("cwst-inline-section").style.display = inline
    ? ""
    : "none";
  document.getElementById("cwst-ref-section").style.display = inline
    ? "none"
    : "";
}

function toggleEditWstPromptSource() {
  var inline = document.getElementById("ewst-src-inline").checked;
  document.getElementById("ewst-inline-section").style.display = inline
    ? ""
    : "none";
  document.getElementById("ewst-ref-section").style.display = inline
    ? "none"
    : "";
}

function _populateWstPromptTemplates(selectId) {
  var sel = document.getElementById(selectId);
  sel.innerHTML = '<option value="">None</option>';
  return authFetch("/v1/api/admin/templates")
    .then(function (r) {
      return r.json();
    })
    .then(function (data) {
      (data.templates || []).forEach(function (t) {
        var opt = document.createElement("option");
        opt.value = t.name;
        opt.textContent = t.name;
        sel.appendChild(opt);
      });
    })
    .catch(function () {
      /* ignore */
    });
}

function showCreateWsTemplateModal() {
  _cwstTriggerEl = document.activeElement;
  var ov = document.getElementById("create-wst-overlay");
  ov.style.display = "flex";
  document.getElementById("cwst-name").value = "";
  document.getElementById("cwst-description").value = "";
  document.getElementById("cwst-system-prompt").value = "";
  document.getElementById("cwst-src-inline").checked = true;
  toggleWstPromptSource();
  _populateWstPromptTemplates("cwst-prompt-template");
  document.getElementById("cwst-model").value = "";
  document.getElementById("cwst-auto-approve").checked = false;
  document.getElementById("cwst-auto-approve-tools").value = "";
  document.getElementById("cwst-token-budget").value = "0";
  document.getElementById("cwst-temperature").value = "";
  document.getElementById("cwst-reasoning-effort").value = "";
  document.getElementById("cwst-max-tokens").value = "";
  document.getElementById("cwst-agent-max-turns").value = "";
  document.getElementById("cwst-enabled").checked = true;
  document.getElementById("create-wst-error").style.display = "none";
  document.getElementById("cwst-name").focus();
  _cwstTrapHandler = _installTrap("create-wst-overlay", "create-wst-box");
}

function hideCreateWsTemplateModal() {
  document.getElementById("create-wst-overlay").style.display = "none";
  _cwstTrapHandler = _removeTrap(_cwstTrapHandler);
  if (_cwstTriggerEl && _cwstTriggerEl.focus) _cwstTriggerEl.focus();
  _cwstTriggerEl = null;
}

function submitCreateWsTemplate() {
  var name = document.getElementById("cwst-name").value.trim();
  if (!name) {
    var e = document.getElementById("create-wst-error");
    e.textContent = "Name is required";
    e.style.display = "";
    return;
  }
  var isInline = document.getElementById("cwst-src-inline").checked;
  document.getElementById("cwst-submit").disabled = true;
  authFetch("/v1/api/admin/ws-templates", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      name: name,
      description: document.getElementById("cwst-description").value,
      system_prompt: isInline
        ? document.getElementById("cwst-system-prompt").value
        : "",
      prompt_template: isInline
        ? ""
        : document.getElementById("cwst-prompt-template").value,
      model: document.getElementById("cwst-model").value.trim(),
      auto_approve: document.getElementById("cwst-auto-approve").checked,
      auto_approve_tools: document
        .getElementById("cwst-auto-approve-tools")
        .value.trim(),
      token_budget: parseInt(
        document.getElementById("cwst-token-budget").value || "0",
        10,
      ),
      temperature: document.getElementById("cwst-temperature").value
        ? parseFloat(document.getElementById("cwst-temperature").value)
        : null,
      reasoning_effort: document.getElementById("cwst-reasoning-effort").value,
      max_tokens: document.getElementById("cwst-max-tokens").value
        ? parseInt(document.getElementById("cwst-max-tokens").value, 10)
        : null,
      agent_max_turns: document.getElementById("cwst-agent-max-turns").value
        ? parseInt(document.getElementById("cwst-agent-max-turns").value, 10)
        : null,
      enabled: document.getElementById("cwst-enabled").checked,
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
      hideCreateWsTemplateModal();
      showToast("WS template created");
      loadGovWsTemplates();
    })
    .catch(function (e) {
      var el = document.getElementById("create-wst-error");
      el.textContent = e.message;
      el.style.display = "";
    })
    .finally(function () {
      document.getElementById("cwst-submit").disabled = false;
    });
}

function showEditWsTemplateModal(wstId) {
  _ewstTriggerEl = document.activeElement;
  var tpl = null;
  for (var i = 0; i < _govWsTemplates.length; i++) {
    if (_govWsTemplates[i].ws_template_id === wstId) {
      tpl = _govWsTemplates[i];
      break;
    }
  }
  if (!tpl) return;
  var ov = document.getElementById("edit-wst-overlay");
  ov.style.display = "flex";
  document.getElementById("ewst-id").value = wstId;
  document.getElementById("ewst-name").value = tpl.name;
  document.getElementById("ewst-description").value = tpl.description || "";
  document.getElementById("ewst-system-prompt").value = tpl.system_prompt || "";
  // Set radio based on which field has content
  if (tpl.prompt_template && !tpl.system_prompt) {
    document.getElementById("ewst-src-ref").checked = true;
  } else {
    document.getElementById("ewst-src-inline").checked = true;
  }
  toggleEditWstPromptSource();
  _populateWstPromptTemplates("ewst-prompt-template").then(function () {
    if (tpl.prompt_template) {
      document.getElementById("ewst-prompt-template").value =
        tpl.prompt_template;
    }
  });
  document.getElementById("ewst-model").value = tpl.model || "";
  document.getElementById("ewst-auto-approve").checked = tpl.auto_approve;
  document.getElementById("ewst-auto-approve-tools").value =
    tpl.auto_approve_tools || "";
  document.getElementById("ewst-token-budget").value = tpl.token_budget || 0;
  document.getElementById("ewst-temperature").value =
    tpl.temperature != null ? tpl.temperature : "";
  document.getElementById("ewst-reasoning-effort").value =
    tpl.reasoning_effort || "";
  document.getElementById("ewst-max-tokens").value =
    tpl.max_tokens != null ? tpl.max_tokens : "";
  document.getElementById("ewst-agent-max-turns").value =
    tpl.agent_max_turns != null ? tpl.agent_max_turns : "";
  document.getElementById("ewst-enabled").checked = tpl.enabled;
  document.getElementById("edit-wst-error").style.display = "none";
  _ewstTrapHandler = _installTrap("edit-wst-overlay", "edit-wst-box");
}

function hideEditWsTemplateModal() {
  document.getElementById("edit-wst-overlay").style.display = "none";
  _ewstTrapHandler = _removeTrap(_ewstTrapHandler);
  if (_ewstTriggerEl && _ewstTriggerEl.focus) _ewstTriggerEl.focus();
  _ewstTriggerEl = null;
}

function submitEditWsTemplate() {
  var id = document.getElementById("ewst-id").value;
  var name = document.getElementById("ewst-name").value.trim();
  if (!name) {
    var e = document.getElementById("edit-wst-error");
    e.textContent = "Name is required";
    e.style.display = "";
    return;
  }
  var isInline = document.getElementById("ewst-src-inline").checked;
  document.getElementById("ewst-submit").disabled = true;
  authFetch("/v1/api/admin/ws-templates/" + id, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      name: document.getElementById("ewst-name").value.trim(),
      description: document.getElementById("ewst-description").value,
      system_prompt: isInline
        ? document.getElementById("ewst-system-prompt").value
        : "",
      prompt_template: isInline
        ? ""
        : document.getElementById("ewst-prompt-template").value,
      model: document.getElementById("ewst-model").value.trim(),
      auto_approve: document.getElementById("ewst-auto-approve").checked,
      auto_approve_tools: document
        .getElementById("ewst-auto-approve-tools")
        .value.trim(),
      token_budget: parseInt(
        document.getElementById("ewst-token-budget").value || "0",
        10,
      ),
      temperature: document.getElementById("ewst-temperature").value
        ? parseFloat(document.getElementById("ewst-temperature").value)
        : null,
      reasoning_effort: document.getElementById("ewst-reasoning-effort").value,
      max_tokens: document.getElementById("ewst-max-tokens").value
        ? parseInt(document.getElementById("ewst-max-tokens").value, 10)
        : null,
      agent_max_turns: document.getElementById("ewst-agent-max-turns").value
        ? parseInt(document.getElementById("ewst-agent-max-turns").value, 10)
        : null,
      enabled: document.getElementById("ewst-enabled").checked,
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
      hideEditWsTemplateModal();
      showToast("WS template updated");
      loadGovWsTemplates();
    })
    .catch(function (e) {
      var el = document.getElementById("edit-wst-error");
      el.textContent = e.message;
      el.style.display = "";
    })
    .finally(function () {
      document.getElementById("ewst-submit").disabled = false;
    });
}

// ---------------------------------------------------------------------------
// WS Template Version History
// ---------------------------------------------------------------------------

var _whTrapHandler = null;
var _whTriggerEl = null;

function showWstHistoryModal(wstId) {
  _whTriggerEl = document.activeElement;
  var ov = document.getElementById("wst-history-overlay");
  ov.style.display = "flex";
  document.getElementById("wst-history-content").innerHTML =
    '<div class="dashboard-empty">Loading...</div>';
  _whTrapHandler = _installTrap("wst-history-overlay", "wst-history-box");
  authFetch("/v1/api/admin/ws-templates/" + wstId + "/versions")
    .then(function (r) {
      if (!r.ok) throw new Error("Failed");
      return r.json();
    })
    .then(function (data) {
      var versions = data.versions || [];
      if (!versions.length) {
        document.getElementById("wst-history-content").innerHTML =
          '<div class="dashboard-empty">No version history yet</div>';
        return;
      }
      var html = "";
      for (var i = 0; i < versions.length; i++) {
        var v = versions[i];
        var snapshot = "{}";
        try {
          snapshot = JSON.stringify(JSON.parse(v.snapshot), null, 2);
        } catch (e) {
          snapshot = v.snapshot;
        }
        html +=
          '<div class="admin-row" style="flex-direction:column;align-items:stretch">' +
          '<div style="display:flex;justify-content:space-between;margin-bottom:4px">' +
          "<strong>v" +
          v.version +
          "</strong>" +
          '<span class="label-hint">' +
          escapeHtml(v.changed_by || "unknown") +
          " &mdash; " +
          escapeHtml(v.created) +
          "</span></div>" +
          '<pre style="margin:0;padding:8px;background:var(--bg-elevated,#1a1a2e);border-radius:4px;overflow-x:auto;font-size:0.85em;max-height:200px;overflow-y:auto">' +
          escapeHtml(snapshot) +
          "</pre></div>";
      }
      document.getElementById("wst-history-content").innerHTML = html;
    })
    .catch(function () {
      document.getElementById("wst-history-content").innerHTML =
        '<div class="dashboard-empty">Failed to load version history</div>';
    });
}

function hideWstHistoryModal() {
  document.getElementById("wst-history-overlay").style.display = "none";
  _whTrapHandler = _removeTrap(_whTrapHandler);
  if (_whTriggerEl && _whTriggerEl.focus) _whTriggerEl.focus();
  _whTriggerEl = null;
}

// ---------------------------------------------------------------------------
// Usage
// ---------------------------------------------------------------------------

function loadGovUsage() {
  var now = new Date();
  var since;
  if (_govUsageRange === "24h") since = new Date(now - 24 * 60 * 60 * 1000);
  else if (_govUsageRange === "30d")
    since = new Date(now - 30 * 24 * 60 * 60 * 1000);
  else since = new Date(now - 7 * 24 * 60 * 60 * 1000);
  var sinceStr = since.toISOString().slice(0, 19);

  // Fetch summary + breakdown in parallel
  var summaryUrl = "/v1/api/admin/usage?since=" + encodeURIComponent(sinceStr);
  var breakdownUrl = summaryUrl + "&group_by=" + _govUsageGroupBy;

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
      document.getElementById("admin-usage-content").innerHTML =
        '<div class="dashboard-empty">Failed to load usage data</div>';
    });
}

function _renderGovUsage(summary, breakdown) {
  var container = document.getElementById("admin-usage-content");
  var s = (summary.breakdown && summary.breakdown[0]) || {};
  var prompt = s.prompt_tokens || 0;
  var completion = s.completion_tokens || 0;
  var total = prompt + completion;
  var tools = s.tool_calls_count || 0;

  var html =
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
    "</div>";

  // Bar chart breakdown
  var items = breakdown.breakdown || [];
  if (items.length) {
    var maxVal = 0;
    for (var i = 0; i < items.length; i++) {
      var v = (items[i].prompt_tokens || 0) + (items[i].completion_tokens || 0);
      if (v > maxVal) maxVal = v;
    }
    html += '<div class="usage-chart">';
    for (var j = 0; j < items.length; j++) {
      var item = items[j];
      var val = (item.prompt_tokens || 0) + (item.completion_tokens || 0);
      var pct = maxVal > 0 ? Math.round((val / maxVal) * 100) : 0;
      var label = item.key || "\u2014";
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

  container.innerHTML = html;
}

function setUsageRange(range) {
  _govUsageRange = range;
  // Update button states
  var btns = document.querySelectorAll(".usage-range-btn");
  for (var i = 0; i < btns.length; i++) {
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
  var btns = document.querySelectorAll(".usage-group-btn");
  for (var i = 0; i < btns.length; i++) {
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
  var url = "/v1/api/admin/audit?limit=50&offset=" + _govAuditOffset;
  var actionFilter = document.getElementById("audit-action-filter");
  var userFilter = document.getElementById("audit-user-filter");
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
      var events = data.events || [];
      _govAuditEvents = _govAuditEvents.concat(events);
      _renderGovAudit(_govAuditEvents, _govAuditTotal);
    })
    .catch(function () {
      document.getElementById("admin-audit-table").innerHTML =
        '<div class="dashboard-empty">Failed to load audit events</div>';
    });
}

function _relativeTime(isoStr) {
  var now = Date.now();
  var then = new Date(isoStr + "Z").getTime();
  var diff = Math.max(0, Math.floor((now - then) / 1000));
  if (diff < 60) return diff + "s ago";
  if (diff < 3600) return Math.floor(diff / 60) + "m ago";
  if (diff < 86400) return Math.floor(diff / 3600) + "h ago";
  return Math.floor(diff / 86400) + "d ago";
}

function _renderGovAudit(events, total) {
  var el = document.getElementById("admin-audit-table");
  if (!events.length) {
    el.innerHTML = '<div class="dashboard-empty">No audit events</div>';
    return;
  }
  var html = "";
  for (var i = 0; i < events.length; i++) {
    var ev = events[i];
    var detail = "";
    try {
      var d = JSON.parse(ev.detail || "{}");
      var keys = Object.keys(d);
      if (keys.length) {
        var parts = [];
        for (var k = 0; k < Math.min(keys.length, 3); k++) {
          parts.push(keys[k] + "=" + String(d[keys[k]]).slice(0, 30));
        }
        detail = parts.join(", ");
      }
    } catch (e) {
      detail = ev.detail;
    }

    var actionCls = "audit-badge";
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
      escapeHtml(ev.user_id ? ev.user_id.slice(0, 8) : "\u2014") +
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
      '<div class="pagination"><button class="audit-load-more" onclick="loadMoreAudit()">Load more (' +
      events.length +
      " of " +
      total +
      ")</button></div>";
  }
  el.innerHTML = html;
}

function loadMoreAudit() {
  _govAuditOffset = _govAuditEvents.length;
  loadGovAudit(true);
}

// Populate audit user filter from admin users list
function _populateAuditUserFilter() {
  var sel = document.getElementById("audit-user-filter");
  if (!sel) return;
  var html = '<option value="">All users</option>';
  for (var i = 0; i < _adminUsers.length; i++) {
    html +=
      '<option value="' +
      escapeHtml(_adminUsers[i].user_id) +
      '">' +
      escapeHtml(_adminUsers[i].username) +
      "</option>";
  }
  sel.innerHTML = html;
}

// ---------------------------------------------------------------------------
// Memories tab
// ---------------------------------------------------------------------------

var _adminMemories = [];
var _memDetailTrap = null;
var _memDetailTrigger = null;
var _memSearchTimer = null;
var _memSearchBound = false;

function loadAdminMemories() {
  clearTimeout(_memSearchTimer);
  // Bind search debounce on first load
  if (!_memSearchBound) {
    var searchEl = document.getElementById("mem-search");
    if (searchEl) {
      searchEl.addEventListener("input", function () {
        clearTimeout(_memSearchTimer);
        _memSearchTimer = setTimeout(loadAdminMemories, 300);
      });
    }
    _memSearchBound = true;
  }

  var memType = document.getElementById("mem-filter-type").value;
  var scope = document.getElementById("mem-filter-scope").value;
  var query = (document.getElementById("mem-search").value || "").trim();

  var url;
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
      document.getElementById("admin-memories-table").innerHTML =
        '<div class="dashboard-empty">Failed to load memories</div>';
    });
}

function _renderAdminMemories(items, total) {
  var el = document.getElementById("admin-memories-table");
  if (!items.length) {
    el.innerHTML = '<div class="dashboard-empty">No memories found</div>';
    return;
  }

  var html = "";
  for (var i = 0; i < items.length; i++) {
    var m = items[i];

    // Type badge
    var typeCls = "scope-badge mem-type-" + escapeHtml(m.type);
    var typeBadge =
      '<span class="' + typeCls + '">' + escapeHtml(m.type) + "</span>";

    // Scope badge
    var scopeLabel = m.scope;
    if (m.scope_id) scopeLabel += ":" + m.scope_id;
    var scopeCls = "scope-badge mem-scope-" + escapeHtml(m.scope);
    var scopeBadge =
      '<span class="' + scopeCls + '">' + escapeHtml(scopeLabel) + "</span>";

    // Description (truncated)
    var desc = m.description || "";
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

  el.innerHTML = html;

  // Bind view buttons
  var viewBtns = el.querySelectorAll("[data-view-memory]");
  for (var v = 0; v < viewBtns.length; v++) {
    viewBtns[v].addEventListener("click", function () {
      showMemoryDetailModal(this.getAttribute("data-view-memory"));
    });
  }

  // Bind delete buttons
  var delBtns = el.querySelectorAll("[data-delete-memory]");
  for (var d = 0; d < delBtns.length; d++) {
    delBtns[d].addEventListener("click", function () {
      var mid = this.getAttribute("data-delete-memory");
      var mname = this.getAttribute("data-delete-name");
      deleteAdminMemory(mid, mname);
    });
  }
}

function showMemoryDetailModal(memoryId) {
  _memDetailTrigger = document.activeElement;
  var ov = document.getElementById("memory-detail-overlay");
  ov.style.display = "flex";
  document.getElementById("memory-detail-body").innerHTML =
    '<div class="dashboard-empty">Loading…</div>';

  // Disable delete button and clear stale handler while loading
  var delBtn = document.getElementById("mem-detail-delete");
  delBtn.disabled = true;
  delBtn.onclick = null;

  // Focus close button for keyboard accessibility
  var closeBtn = ov.querySelector(".modal-cancel");
  if (closeBtn) closeBtn.focus();

  authFetch("/v1/api/admin/memories/" + encodeURIComponent(memoryId))
    .then(function (r) {
      if (!r.ok) throw new Error("Not found");
      return r.json();
    })
    .then(function (m) {
      var scopeLabel = m.scope;
      if (m.scope_id) scopeLabel += ":" + m.scope_id;

      var html =
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

      document.getElementById("memory-detail-body").innerHTML = html;

      // Wire delete button now that data is loaded
      delBtn.disabled = false;
      delBtn.onclick = function () {
        deleteAdminMemory(m.memory_id, m.name);
      };
    })
    .catch(function () {
      document.getElementById("memory-detail-body").innerHTML =
        '<div class="dashboard-empty">Failed to load memory</div>';
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
