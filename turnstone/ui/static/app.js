// ===========================================================================
//  turnstone server UI — app.js
//  Standalone L-shell provider.  Owns the single-node Tier-1 feed (a flat
//  workstream roster over /v1/api/events/global) and exposes the window.TS_APP
//  / TS_ADMIN seams the shared shell (shell.js) reads: getClusterState (a
//  synthesized one-node cluster), boot, onRender, showHome, and a one-tab
//  Manage IA (MCP connections).  Sessions open as interactive panes; the
//  binary split-pane machinery is retired in favour of PaneManager.
// ===========================================================================

// ===========================================================================
//  4. Global state
// ===========================================================================

let workstreams = {};
let currentWsId = null;
let globalEvtSource = null;
let globalRetryDelay = 1000;
// Saved high-water mark for the manual-reconnect path (the
// EventSource constructor can't set custom headers, so the
// browser-native ``Last-Event-ID`` header is unavailable on
// reconnect — we thread it via ``?last_event_id=N`` instead).  Updated
// from ``globalEvtSource.lastEventId`` on every onmessage; native
// auto-reconnect uses the header directly on the same source object.
let globalLastEventId = null;
let dashboardVisible = false;
let _historyNavigation = false;
let _lastHealth = null;

const STATE_DISPLAY = {
  running: { symbol: "\u25b8", label: "run" },
  thinking: { symbol: "\u25cc", label: "think" },
  attention: { symbol: "\u25c6", label: "attn" },
  idle: { symbol: "\u00b7", label: "idle" },
  error: { symbol: "\u2716", label: "err" },
};

// ===========================================================================
//  5. Health polling
// ===========================================================================

function pollHealth() {
  authFetch("/health")
    .then(function (r) {
      return r.json();
    })
    .then(function (data) {
      pollHealth._failCount = 0;
      _lastHealth = data;
      const mcpEl = document.getElementById("mcp-status");
      if (mcpEl) {
        if (data.mcp && data.mcp.servers > 0) {
          mcpEl.textContent =
            "MCP: " +
            data.mcp.servers +
            " server" +
            (data.mcp.servers !== 1 ? "s" : "");
          mcpEl.title =
            data.mcp.resources +
            " resources \u00b7 " +
            data.mcp.prompts +
            " prompts";
          mcpEl.style.opacity = "1";
        } else {
          mcpEl.textContent = "";
          mcpEl.title = "";
          mcpEl.style.opacity = "0";
        }
      }
      const el = document.getElementById("health-indicator");
      if (!el) return;
      if (data.status === "degraded") {
        el.textContent = "backend degraded";
        el.className = "health-degraded";
        el.title =
          "Backend: " + ((data.backend && data.backend.status) || "unknown");
        el.setAttribute(
          "aria-label",
          "Backend degraded: " +
            ((data.backend && data.backend.status) || "unknown"),
        );
      } else {
        el.textContent = "";
        el.className = "health-ok";
        el.title = "";
        el.removeAttribute("aria-label");
      }
    })
    .catch(function () {
      if (!pollHealth._failCount) pollHealth._failCount = 0;
      pollHealth._failCount++;
      if (pollHealth._failCount >= 2) {
        const el = document.getElementById("health-indicator");
        if (!el) return;
        el.textContent = "health unknown";
        el.className = "health-degraded";
        el.title = "Health endpoint unreachable";
      }
    });
}
setInterval(pollHealth, 30000);

// ===========================================================================
//  6. Auth hooks
// ===========================================================================

window.onLoginSuccess = function () {
  initWorkstreams();
};

window.onLogout = function () {
  // The shell + PaneManager own the panes; just drop the roster + Tier-1 feed.
  workstreams = {};
  currentWsId = null;
  if (globalEvtSource) {
    globalEvtSource.close();
    globalEvtSource = null;
  }
  fireRender();
};

// ===========================================================================
//  7. Theme toggle
// ===========================================================================

window.onThemeChange = function (next) {
  const btn = document.getElementById("theme-toggle");
  if (btn) {
    const isLight = next === "light";
    btn.textContent = isLight ? "\u2600" : "\u263E";
    btn.title = isLight ? "Switch to dark theme" : "Switch to light theme";
    btn.setAttribute(
      "aria-label",
      isLight ? "Switch to dark theme" : "Switch to light theme",
    );
  }
  reRenderAllMermaid();
  // Persist theme to server settings so it propagates to other clients
  const themeValue = next === "light" ? "light" : "dark";
  authFetch("/v1/api/admin/settings/interface.theme", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ value: themeValue }),
  }).catch(function () {});
};
(function () {
  const btn = document.getElementById("theme-toggle");
  if (btn) {
    const isLight = document.documentElement.dataset.theme === "light";
    btn.textContent = isLight ? "\u2600" : "\u263E";
    btn.title = isLight ? "Switch to dark theme" : "Switch to light theme";
    btn.setAttribute(
      "aria-label",
      isLight ? "Switch to dark theme" : "Switch to light theme",
    );
  }
})();

// ===========================================================================
//  9. New workstream modal
// ===========================================================================

let _forkFromWsId = "";

// Staged files for the new-workstream modal.  Distinct from the pane's
// chip strip: there's no ws_id yet, so we hold File objects in memory
// and ship them all in one multipart create request on submit.
let _newWsStagedFiles = [];

// Per-kind size caps (mirrored from turnstone/core/attachments.py so the
// browser can fail fast before uploading).  Keep in sync.
const _NEW_WS_IMAGE_CAP = 4 * 1024 * 1024;
const _NEW_WS_TEXT_CAP = 512 * 1024;
const _NEW_WS_MAX_FILES = 10;

function _newWsRenderChips() {
  const chipsEl = document.getElementById("new-ws-attach-chips");
  if (!chipsEl) return;
  chipsEl.textContent = "";
  for (let i = 0; i < _newWsStagedFiles.length; i++) {
    (function (idx) {
      const f = _newWsStagedFiles[idx];
      const chip = document.createElement("span");
      chip.className = "new-ws-attach-chip";
      chip.setAttribute("role", "listitem");
      const label = document.createElement("span");
      label.className = "new-ws-attach-chip-name";
      label.textContent = f.name;
      label.title = f.name + " (" + f.size + " bytes)";
      chip.appendChild(label);
      const size = document.createElement("span");
      size.className = "new-ws-attach-chip-size";
      size.textContent = _formatAttachSize(f.size);
      chip.appendChild(size);
      const rm = document.createElement("button");
      rm.type = "button";
      rm.className = "new-ws-attach-chip-remove";
      rm.setAttribute("aria-label", "Remove " + f.name);
      rm.textContent = "\u00d7";
      rm.onclick = function () {
        _newWsStagedFiles.splice(idx, 1);
        _newWsRenderChips();
      };
      chip.appendChild(rm);
      chipsEl.appendChild(chip);
    })(i);
  }
}

// Mirrors turnstone/server.py classifier — magic-byte image allowlist plus
// text/* MIMEs, allowlisted application/* MIMEs, and known text extensions.
// Surfaces unsupported types client-side so the user sees a clear error
// instead of a generic create failure after the server rejects.
const _ATTACH_IMAGE_MIMES = [
  "image/png",
  "image/jpeg",
  "image/gif",
  "image/webp",
];
const _ATTACH_TEXT_APP_MIMES = [
  "application/json",
  "application/xml",
  "application/x-yaml",
  "application/yaml",
  "application/toml",
];
const _ATTACH_TEXT_EXTENSIONS = [
  ".c",
  ".conf",
  ".cpp",
  ".css",
  ".go",
  ".h",
  ".hpp",
  ".html",
  ".ini",
  ".java",
  ".js",
  ".json",
  ".jsx",
  ".md",
  ".py",
  ".rs",
  ".sh",
  ".sql",
  ".toml",
  ".ts",
  ".tsx",
  ".txt",
  ".xml",
  ".yaml",
  ".yml",
];

function _isAttachmentAllowed(file) {
  const mime = (file.type || "").toLowerCase();
  if (_ATTACH_IMAGE_MIMES.indexOf(mime) !== -1) return true;
  if (mime.indexOf("text/") === 0) return true;
  if (_ATTACH_TEXT_APP_MIMES.indexOf(mime) !== -1) return true;
  const name = (file.name || "").toLowerCase();
  const dot = name.lastIndexOf(".");
  if (dot >= 0 && _ATTACH_TEXT_EXTENSIONS.indexOf(name.substr(dot)) !== -1) {
    return true;
  }
  return false;
}

// In-dialog error strip (sh-alert).  Empty message clears + hides; a set
// message also scrolls into view — the alert sits at the top of the
// scrollable body while the submit lives in the pinned foot.
function _newWsError(msg) {
  const el = document.getElementById("new-ws-error");
  el.textContent = msg || "";
  if (msg) {
    el.classList.add("is-visible");
    if (el.scrollIntoView) el.scrollIntoView({ block: "nearest" });
  } else {
    el.classList.remove("is-visible");
  }
}

function _newWsAddFiles(files) {
  for (let i = 0; i < files.length; i++) {
    const f = files[i];
    if (_newWsStagedFiles.length >= _NEW_WS_MAX_FILES) {
      _newWsError(
        "At most " + _NEW_WS_MAX_FILES + " attachments per workstream",
      );
      return;
    }
    if (!_isAttachmentAllowed(f)) {
      _newWsError(
        "Unsupported file type: " +
          f.name +
          " (allowed: png/jpeg/gif/webp images, text)",
      );
      return;
    }
    const isImage = (f.type || "").indexOf("image/") === 0;
    const cap = isImage ? _NEW_WS_IMAGE_CAP : _NEW_WS_TEXT_CAP;
    if (f.size > cap) {
      _newWsError(f.name + " exceeds the " + _formatAttachSize(cap) + " cap");
      return;
    }
    _newWsStagedFiles.push(f);
  }
  _newWsError("");
  _newWsRenderChips();
}

function newWorkstream() {
  showNewWsModal();
}

function showNewWsModal(forkFromWsId) {
  _forkFromWsId = forkFromWsId || "";
  const dlg = document.getElementById("new-ws-dialog");

  // Update title, plate and button text based on mode
  const titleEl = document.getElementById("new-ws-title");
  const tagEl = document.getElementById("new-ws-tag");
  const submitBtn = document.getElementById("new-ws-submit");
  if (_forkFromWsId) {
    titleEl.textContent = "Fork workstream";
    tagEl.textContent = "WS-FORK";
    submitBtn.textContent = "Fork";
  } else {
    titleEl.textContent = "New workstream";
    tagEl.textContent = "WS-NEW";
    submitBtn.textContent = "Create";
  }

  // Hide skill dropdown when forking (not relevant — fork copies history)
  const skillLabel = document.querySelector('label[for="new-ws-skill"]');
  const skillSelect = document.getElementById("new-ws-skill");
  if (skillLabel) skillLabel.hidden = !!_forkFromWsId;
  if (skillSelect) skillSelect.hidden = !!_forkFromWsId;

  // Populate model dropdown
  const modelSelect = document.getElementById("new-ws-model");
  const judgeSelect = document.getElementById("new-ws-judge-model");
  const curModel = ""; // (focused-pane model prefill was PaneManager-only; dead in the L-shell)
  modelSelect.textContent = "";
  judgeSelect.textContent = "";
  const defaultOpt = document.createElement("option");
  defaultOpt.value = "";
  defaultOpt.textContent = curModel
    ? "Default (" + curModel + ")"
    : "Default model";
  modelSelect.appendChild(defaultOpt);
  const defJudgeOpt = document.createElement("option");
  defJudgeOpt.value = "";
  defJudgeOpt.textContent = "Default (agent model)";
  judgeSelect.appendChild(defJudgeOpt);
  authFetch("/v1/api/models")
    .then(function (r) {
      return r.json();
    })
    .then(function (data) {
      (data.models || []).forEach(function (m) {
        const opt = document.createElement("option");
        opt.value = m.alias;
        opt.textContent =
          m.alias === m.model ? m.alias : m.alias + " (" + m.model + ")";
        modelSelect.appendChild(opt);

        const judgeOpt = document.createElement("option");
        judgeOpt.value = m.alias;
        judgeOpt.textContent = opt.textContent;
        judgeSelect.appendChild(judgeOpt);
      });
    })
    .catch(function () {
      /* ignore — default model still works */
    });

  const tplSelect = document.getElementById("new-ws-skill");
  const tplDefaultOpt = document.createElement("option");
  tplDefaultOpt.value = "";
  tplDefaultOpt.textContent = "Use defaults";
  tplSelect.replaceChildren(tplDefaultOpt);
  authFetch("/v1/api/skills")
    .then(function (r) {
      return r.json();
    })
    .then(function (data) {
      (data.skills || []).forEach(function (t) {
        const opt = document.createElement("option");
        opt.value = t.name;
        let label = t.name;
        if (t.is_default) label += " (default)";
        if (t.origin === "mcp") label += " [MCP]";
        opt.textContent = label;
        tplSelect.appendChild(opt);
      });
    })
    .catch(function () {
      /* ignore */
    });

  // Project picker — populated from the shared projects cache, refreshed on
  // open so a project created elsewhere appears.  Hidden when forking: a fork
  // inherits its parent's project.
  const projLabel = document.querySelector('label[for="new-ws-project"]');
  const projSelect = document.getElementById("new-ws-project");
  if (projLabel) projLabel.hidden = !!_forkFromWsId;
  if (projSelect) projSelect.hidden = !!_forkFromWsId;
  if (projSelect && !_forkFromWsId && window.TurnstoneProjects) {
    window.TurnstoneProjects.refreshProjects().then(function () {
      _populateProjectSelect(projSelect);
    });
  }

  document.getElementById("new-ws-name").value = "";
  const initEl = document.getElementById("new-ws-initial-message");
  if (initEl) initEl.value = "";
  _newWsError("");

  // Reset attachment staging.  Forks don't carry attachments —
  // disable the attach UI in that case (the fork inherits its
  // parent's history; new attachments go on the next manual send).
  _newWsStagedFiles = [];
  const attachRow = document.getElementById("new-ws-attach-row");
  const attachInput = document.getElementById("new-ws-attach-input");
  const attachBtn = document.getElementById("new-ws-attach-btn");
  if (attachRow) attachRow.hidden = !!_forkFromWsId;
  if (attachInput) attachInput.value = "";
  _newWsRenderChips();
  if (attachBtn && attachInput) {
    attachBtn.onclick = function () {
      attachInput.click();
    };
    attachInput.onchange = function () {
      if (attachInput.files && attachInput.files.length) {
        _newWsAddFiles(attachInput.files);
      }
      attachInput.value = "";
    };
  }

  submitBtn.onclick = submitNewWs;
  window.TurnstoneHatch.openDialog(dlg, {
    onClose: function () {
      _forkFromWsId = "";
    },
  });
}

function hideNewWsModal() {
  const d = document.getElementById("new-ws-dialog");
  if (d.open) d.close();
}

// Sentinel option that reveals the inline project creator (project_creator.js).
const _PROJECT_NEW = "__new__";

// Mount the inline "+ New project…" creator once per <select>: it sits right
// after the select; picking the sentinel resets the select and opens it; on
// Save it repopulates + selects the new project.
function _ensureStandaloneProjectCreator(sel) {
  if (!sel || sel._projCreatorWired || !window.TurnstoneProjectCreator) return;
  sel._projCreatorWired = true;
  const creator = window.TurnstoneProjectCreator.make({
    onCreated: function (proj) {
      _populateProjectSelect(sel);
      sel.value = proj.project_id;
    },
    onClose: function () {
      if (sel.value === _PROJECT_NEW) sel.value = "";
    },
  });
  if (sel.parentNode) sel.parentNode.insertBefore(creator.el, sel.nextSibling);
  sel.addEventListener("change", function () {
    if (sel.value === _PROJECT_NEW) {
      sel.value = ""; // reset before opening so the sentinel can't stick
      creator.open();
    }
  });
}

// Fill a project <select> from the shared projects cache, preserving its first
// <option> (the "No project" placeholder) and the current selection, and append
// the "+ New project…" sentinel.  No-op when the projects bridge is absent
// (project.read denied / still loading).
function _populateProjectSelect(sel) {
  if (!sel || !window.TurnstoneProjects) return;
  _ensureStandaloneProjectCreator(sel);
  const previous = sel.value;
  const placeholder = sel.options.length ? sel.options[0] : null;
  sel.replaceChildren();
  if (placeholder) sel.appendChild(placeholder);
  window.TurnstoneProjects.projectChoices().forEach(function (c) {
    const opt = document.createElement("option");
    opt.value = c.value;
    opt.textContent = c.text;
    sel.appendChild(opt);
  });
  const newOpt = document.createElement("option");
  newOpt.value = _PROJECT_NEW;
  newOpt.textContent = "+ New project…";
  sel.appendChild(newOpt);
  if (previous && previous !== _PROJECT_NEW) sel.value = previous;
}

function submitNewWs() {
  const dlg = document.getElementById("new-ws-dialog");
  const body = {};
  const name = document.getElementById("new-ws-name").value.trim();
  const model = document.getElementById("new-ws-model").value.trim();
  const judge_model = document
    .getElementById("new-ws-judge-model")
    .value.trim();
  const skill = document.getElementById("new-ws-skill").value;
  const projectEl = document.getElementById("new-ws-project");
  const project_id = projectEl ? projectEl.value : "";
  const initEl = document.getElementById("new-ws-initial-message");
  const initial_message = initEl ? initEl.value.trim() : "";
  if (name) body.name = name;
  if (model) body.model = model;
  if (judge_model) body.judge_model = judge_model;
  if (skill && !_forkFromWsId) body.skill = skill;
  // A fork inherits its parent's project (the picker is hidden); only a fresh
  // create carries an explicit project_id.
  if (project_id && !_forkFromWsId) body.project_id = project_id;
  if (_forkFromWsId) body.resume_ws = _forkFromWsId;
  if (initial_message) body.initial_message = initial_message;

  _newWsError("");
  window.TurnstoneHatch.setBusy(dlg, true);

  let fetchOpts;
  const staged = _forkFromWsId ? [] : _newWsStagedFiles.slice();
  if (staged.length > 0) {
    const form = new FormData();
    form.append("meta", JSON.stringify(body));
    for (let i = 0; i < staged.length; i++) {
      form.append("file", staged[i], staged[i].name);
    }
    // Don't set Content-Type — the browser adds the correct boundary.
    fetchOpts = { method: "POST", body: form };
  } else {
    fetchOpts = {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    };
  }

  authFetch("/v1/api/workstreams/new", fetchOpts)
    .then(function (r) {
      return r.json();
    })
    .then(function (data) {
      window.TurnstoneHatch.setBusy(dlg, false);
      if (data.error) {
        _newWsError(data.error);
        return;
      }
      if (data.ws_id) {
        // Seed project_id from what we sent so the rail groups it immediately
        // (the ws_created SSE re-affirms it shortly after).
        workstreams[data.ws_id] = {
          name: data.name,
          state: "idle",
          project_id: body.project_id || null,
        };
        _newWsStagedFiles = [];
        hideNewWsModal();
        switchTab(data.ws_id);
      }
    })
    .catch(function () {
      window.TurnstoneHatch.setBusy(dlg, false);
      _newWsError(
        _forkFromWsId
          ? "Failed to fork workstream"
          : "Failed to create workstream",
      );
    });
}

function closeWorkstream(wsId) {
  authFetch("/v1/api/workstreams/" + encodeURIComponent(wsId) + "/close", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: "{}",
  })
    .then(function (r) {
      return r.json();
    })
    .then(function (data) {
      if (data.status === "ok") {
        delete workstreams[wsId];
        closeSessionPane(wsId);
        fireRender();
        if (!Object.keys(workstreams).length) showDashboard();
      } else if (data.error) {
        showToast(data.error, "warning");
      }
    });
}

// ===========================================================================
//  10. Dashboard
// ===========================================================================

// The dashboard is a first-class L-shell pane (#main).  "Show" focuses that
// pane and refreshes its lists; "hide" is a no-op (activating another tab is
// how you leave it).  boot() populates the lists once on first load.
function showDashboard() {
  if (window.TS_SHELL && window.TS_SHELL.panes)
    window.TS_SHELL.panes.openPane("dashboard");
  loadDashboard();
  _loadDashboardOptionsLists();
  _restoreDashboardOptionsState();
  _refreshDashboardOptionsSummary();
  _refreshDashboardSubmitLabel();
}

function hideDashboard() {
  /* no-op: the dashboard is a pane, not an overlay (PaneManager owns focus). */
}

function toggleDashboard() {
  showDashboard();
}

// Paint a transient message (loading / error) into the saved-workstreams
// area.  Clears any cards AND hides the pagination control \u2014 it's a sibling
// of the cards container, so a bare replaceChildren on the cards alone would
// leave stale Prev/Next visible and still wired to the previous list cache.
// A successful load re-shows it (and the footer) via _wsTable.setItems.
function _setSavedWsMessage(text) {
  document
    .getElementById("dashboard-saved-cards")
    .replaceChildren(makeEmptyState(text));
  const pag = document.getElementById("ws-pagination");
  if (pag) pag.style.display = "none";
  const footer = document.getElementById("ws-saved-footer");
  if (footer) footer.textContent = "";
}

function loadDashboard() {
  const tableEl = document.getElementById("dash-ws-table");
  tableEl.replaceChildren(makeEmptyState("Loading\u2026"));
  _setSavedWsMessage("Loading\u2026");
  const dashP = authFetch("/v1/api/dashboard").then(function (r) {
    return r.json();
  });
  const sessP = authFetch("/v1/api/workstreams/saved").then(function (r) {
    return r.json();
  });
  // Refresh the projects cache alongside the table so the per-row project pills
  // resolve on first paint (deduped with the rail's fetch; never rejects).
  const projP = window.TurnstoneProjects
    ? window.TurnstoneProjects.refreshProjects()
    : Promise.resolve();
  Promise.all([dashP, sessP, projP])
    .then(function (res) {
      const dashData = res[0];
      const wsList = dashData.workstreams || [];
      const agg = dashData.aggregate || {};
      renderDashboardTable(wsList, agg);
      const activeWsIds = {};
      wsList.forEach(function (ws) {
        activeWsIds[ws.ws_id] = true;
      });
      const savedList = (res[1].workstreams || []).filter(function (s) {
        return !activeWsIds[s.ws_id];
      });
      _wsTable.setItems(savedList);
    })
    .catch(function () {
      tableEl.replaceChildren(makeEmptyState("Failed to load"));
      _setSavedWsMessage("Failed to load");
    });
}

function renderDashboardTable(wsList, agg) {
  const activeCount = wsList.filter(function (w) {
    return w.state !== "idle";
  }).length;
  document.getElementById("dash-summary").textContent =
    activeCount + " active \u00b7 " + wsList.length + " total";
  const table = document.getElementById("dash-ws-table");
  table.replaceChildren();
  if (!wsList.length) {
    table.replaceChildren(makeEmptyState("No active workstreams"));
    updateDashFooter(agg);
    return;
  }
  wsList.forEach(function (ws) {
    const liveState =
      (workstreams[ws.ws_id] && workstreams[ws.ws_id].state) ||
      ws.state ||
      "idle";
    const liveName =
      (workstreams[ws.ws_id] && workstreams[ws.ws_id].name) ||
      ws.name ||
      ws.ws_id;
    const sd = STATE_DISPLAY[liveState] || STATE_DISPLAY.idle;

    const row = document.createElement("div");
    row.className = "dash-row";
    row.dataset.wsId = ws.ws_id;
    row.dataset.state = liveState;
    row.setAttribute("role", "button");
    row.setAttribute("tabindex", "0");
    let ariaLabel = liveName + " \u2014 " + sd.label;
    if (ws.model_alias || ws.model)
      ariaLabel += ", model: " + (ws.model_alias || ws.model);
    if (ws.title) ariaLabel += ", task: " + ws.title;
    if (ws.tokens) ariaLabel += ", " + formatTokens(ws.tokens) + " tokens";
    if (ws.context_ratio > 0)
      ariaLabel += ", " + Math.round(ws.context_ratio * 100) + "% context";
    row.setAttribute("aria-label", ariaLabel);

    const main = document.createElement("div");
    main.className = "dash-row-main";

    const stateCell = document.createElement("span");
    stateCell.className = "dash-cell-state";
    const stateDot = document.createElement("span");
    stateDot.className = "dash-state-dot";
    stateDot.setAttribute("data-state", liveState);
    stateDot.setAttribute("aria-hidden", "true");
    const stateLabel = document.createElement("span");
    stateLabel.className = "dash-state-label";
    stateLabel.setAttribute("data-state", liveState);
    stateLabel.textContent = sd.symbol + " " + sd.label;
    stateCell.append(stateDot, stateLabel);
    main.appendChild(stateCell);

    const nameCell = document.createElement("span");
    nameCell.className = "dash-cell-name";
    // The name truncates in its own element so the project marker (a flex:none
    // sibling) stays visible instead of being clipped by the fixed-width cell.
    const nameTextEl = document.createElement("span");
    nameTextEl.className = "dash-cell-name-text";
    nameTextEl.textContent = liveName;
    nameCell.appendChild(nameTextEl);
    // A glyph-only project marker (the table grid is a fixed 6 columns, so no
    // 7th cell; the rail group + composer chip carry the full name) when the ws
    // is attached to a project the viewer can name.
    const projName =
      ws.project_id && window.TurnstoneProjects
        ? window.TurnstoneProjects.projectName(ws.project_id)
        : "";
    if (projName) {
      const pill = document.createElement("span");
      pill.className = "dash-project-pill";
      pill.textContent = "▣";
      pill.title = "Project: " + projName;
      pill.setAttribute("role", "img");
      pill.setAttribute("aria-label", "Project: " + projName);
      nameCell.appendChild(pill);
    }
    main.appendChild(nameCell);

    const modelCell = document.createElement("span");
    modelCell.className = "dash-cell-model";
    modelCell.textContent = ws.model_alias || ws.model || "";
    if (ws.model) modelCell.title = ws.model;
    main.appendChild(modelCell);

    // No NODE cell on standalone: this server is single-node (caps.cluster=false),
    // so the multi-node NODE column is dropped (the workstreams table is gated to
    // 6 columns in style.css to match the header).

    const taskCell = document.createElement("span");
    taskCell.className = "dash-cell-task";
    taskCell.textContent = ws.title || "";
    main.appendChild(taskCell);

    const tokensCell = document.createElement("span");
    tokensCell.className = "dash-cell-tokens";
    tokensCell.textContent = ws.tokens ? formatTokens(ws.tokens) : "";
    main.appendChild(tokensCell);

    const ctxCell = document.createElement("span");
    ctxCell.className = "dash-cell-ctx " + ctxClass(ws.context_ratio);
    ctxCell.textContent =
      ws.context_ratio > 0 ? Math.round(ws.context_ratio * 100) + "%" : "";
    main.appendChild(ctxCell);

    row.appendChild(main);

    const sub = document.createElement("div");
    sub.className = "dash-row-sub";
    if (ws.activity_state === "approval") sub.classList.add("sub-attention");
    sub.textContent = ws.activity || "";
    row.appendChild(sub);

    row.onclick = function () {
      dashboardSwitchWorkstream(ws.ws_id);
    };
    row.onkeydown = function (e) {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        dashboardSwitchWorkstream(ws.ws_id);
      }
    };

    table.appendChild(row);
  });
  updateDashFooter(agg);
  table.onkeydown = function (e) {
    if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
    e.preventDefault();
    const rows = Array.from(table.querySelectorAll(".dash-row"));
    const idx = rows.indexOf(document.activeElement);
    if (idx === -1) return;
    if (e.key === "ArrowDown" && idx < rows.length - 1) rows[idx + 1].focus();
    if (e.key === "ArrowUp" && idx > 0) rows[idx - 1].focus();
  };
}

function updateDashFooter(agg) {
  if (!agg) return;
  const nodesEl = document.getElementById("dash-footer-nodes");
  const statsEl = document.getElementById("dash-footer-stats");
  const footerDot = document.createElement("span");
  footerDot.className = "dash-footer-node-dot";
  nodesEl.replaceChildren(
    footerDot,
    " " + (agg.node || "local") + " (" + (agg.total_count || 0) + " ws)",
  );
  const parts = [];
  if (agg.total_tokens) parts.push(formatTokens(agg.total_tokens) + " tokens");
  if (agg.total_tool_calls) parts.push(agg.total_tool_calls + " tool calls");
  if (agg.uptime_seconds)
    parts.push(formatUptime(agg.uptime_seconds) + " uptime");
  statsEl.textContent = parts.join(" \u00b7 ");
  if (_lastHealth && _lastHealth.status === "degraded") {
    statsEl.textContent += " \u00b7 backend degraded";
  }
}

// Saved Workstreams table.  The shared createSavedTable (/shared/cards.js)
// owns filter + sort + render and wraps the multi-select delete controller;
// the per-app inputs are the column spec, the DOM refs, and the path-keyed
// delete request.  Coordinators (console/static) use the same helper with a
// CHILDREN column instead of MSGS.
let _wsTable = null;

// Built at boot, not parse: the saved-table substrate (/shared/cards.js) is a
// deferred ES module now, so its bridged globals (SavedColumns,
// createSavedTable) don't exist yet while this classic file parses.
function _initSavedWsTable() {
  const WS_COLUMNS = [
    SavedColumns.name(),
    SavedColumns.project(),
    SavedColumns.model(),
    SavedColumns.count("message_count", "MSGS"),
    SavedColumns.ctx(),
    SavedColumns.last(),
    SavedColumns.id(),
  ];
  _wsTable = createSavedTable({
    headerEl: document.getElementById("ws-saved-colheaders"),
    bodyEl: document.getElementById("dashboard-saved-cards"),
    filterEl: document.getElementById("ws-filter"),
    footerEl: document.getElementById("ws-saved-footer"),
    paginationEl: document.getElementById("ws-pagination"),
    columns: WS_COLUMNS,
    noun: "workstream",
    emptyText: "No saved workstreams",
    activateLabel: function (s) {
      return "Resume: " + (s.alias || s.title || s.ws_id);
    },
    onActivate: function (s) {
      dashboardResumeSession(s.ws_id);
    },
    delete: {
      idPrefix: "ws-delete",
      buttonId: "ws-delete-btn",
      buildDeleteRequest: function (wsId) {
        return {
          url: "/v1/api/workstreams/" + encodeURIComponent(wsId) + "/delete",
          options: { method: "POST" },
        };
      },
      onClose: function () {
        loadDashboard();
      },
    },
  });
  // The PROJECT column resolves names from the shared projects cache,
  // which fills asynchronously — re-render once names arrive.
  if (window.TurnstoneProjects) {
    window.TurnstoneProjects.onProjectsChange(function () {
      if (_wsTable) _wsTable.render();
    });
  }
}

// HTML inline-onclick wrappers — keep the global names the existing markup
// binds to (`onclick="startWsDeleteMode()"` etc.) and forward to the shared
// table's delete controller.
function startWsDeleteMode() {
  _wsTable.controller.start();
}
function cancelWsDeleteMode() {
  _wsTable.controller.cancel();
}
function toggleSelectAll() {
  _wsTable.controller.toggleAll();
}
function confirmWsDeleteSelection() {
  _wsTable.controller.confirmSelection();
}

// --- Workstream title management ---

let _lastActiveWsId = null;

function refreshWorkstreamTitle(optWsId) {
  const wsId = optWsId || getCurrentWsId();
  if (!wsId) return;

  const url =
    "/v1/api/workstreams/" + encodeURIComponent(wsId) + "/refresh-title";

  authFetch(url, { method: "POST" })
    .then(function (r) {
      if (!r.ok)
        throw new Error("Failed to refresh title (HTTP " + r.status + ")");
      return r.json();
    })
    .then(function (data) {
      showToast("Title regeneration started…", "info");
    })
    .catch(function (err) {
      showToast(err.message || "Failed to refresh title", "error");
    });
}

let _editTitleWsId = null;

function editWorkstreamTitle(optWsId) {
  const wsId = optWsId || getCurrentWsId();
  if (!wsId) return;
  const ws = workstreams[wsId];
  const currentTitle = ws && ws.name ? ws.name : "";

  // Pin the target: submit must rename THIS workstream, not whichever
  // pane is active by then (menu-rename on a background tab).
  _editTitleWsId = wsId;
  const dlg = document.getElementById("edit-title-dialog");
  const input = document.getElementById("edit-title-input");
  input.value = currentTitle;
  // A rename is a styled prompt(): Enter submits.  Escape is the native
  // dialog cancel; hatch.js owns the trap and the data-close buttons.
  input.onkeydown = function (e) {
    if (e.key === "Enter") {
      e.preventDefault();
      submitEditTitle();
    }
  };
  document.getElementById("edit-title-save").onclick = submitEditTitle;
  window.TurnstoneHatch.openDialog(dlg, {
    onClose: function () {
      _editTitleWsId = null;
    },
  });
  // select() sets the selection but does NOT move focus (per spec) — without
  // this, focus stays on the header ✕ and Enter closes instead of submitting.
  input.focus();
  input.select();
}

function cancelEditTitle() {
  const d = document.getElementById("edit-title-dialog");
  if (d.open) d.close();
}

function submitEditTitle() {
  const wsId = _editTitleWsId;
  if (!wsId) return;
  const dlg = document.getElementById("edit-title-dialog");
  // Enter arrives straight from the input's keydown — the busy capture
  // guard only swallows clicks, so re-submits are refused here.
  if (dlg.hasAttribute("data-busy")) return;
  const input = document.getElementById("edit-title-input");
  const newTitle = input.value.trim();
  if (!newTitle) {
    showToast("Title cannot be empty", "warning");
    return;
  }

  const url = "/v1/api/workstreams/" + encodeURIComponent(wsId) + "/title";

  window.TurnstoneHatch.setBusy(dlg, true);
  authFetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title: newTitle }),
  })
    .then(function (r) {
      if (!r.ok) throw new Error("Failed to set title (HTTP " + r.status + ")");
      return r.json();
    })
    .then(function (data) {
      window.TurnstoneHatch.setBusy(dlg, false);
      cancelEditTitle();
      // Optimistic update — SSE ws_rename will confirm
      const nameEls = document.querySelectorAll(
        '[data-ws-id="' + wsId + '"] .tab-name',
      );
      nameEls.forEach(function (el) {
        el.textContent = newTitle;
      });
      if (workstreams[wsId]) workstreams[wsId].name = newTitle;
      showToast("Title updated", "success");
    })
    .catch(function (err) {
      window.TurnstoneHatch.setBusy(dlg, false);
      showToast(err.message || "Failed to set title", "error");
    });
}

// --- Workstream deletion ---

let _pendingDeleteWsId = null;

function confirmDeleteWorkstream(optWsId) {
  const wsId = optWsId || getCurrentWsId();
  if (!wsId) return;
  if (Object.keys(workstreams).length <= 1) return;
  const ws = workstreams[wsId];
  const name = ws && ws.name ? ws.name : wsId.substring(0, 12);

  _pendingDeleteWsId = wsId;
  document.getElementById("delete-ws-message").textContent =
    'Delete "' + name + '"? This cannot be undone.';
  document.getElementById("delete-ws-confirm").onclick = executeDeleteWs;
  // Cancel carries the autofocus — Enter on a freshly-opened destructive
  // confirm must not fire the action (the console confirm-dialog rule).
  window.TurnstoneHatch.openDialog(
    document.getElementById("delete-ws-dialog"),
    {
      onClose: function () {
        _pendingDeleteWsId = null;
      },
    },
  );
}

function cancelDeleteWs() {
  const d = document.getElementById("delete-ws-dialog");
  if (d.open) d.close();
}

function executeDeleteWs() {
  const wsId = _pendingDeleteWsId;
  if (!wsId) return;
  const dlg = document.getElementById("delete-ws-dialog");
  // Hold the dialog open under the busy lock until the request resolves —
  // the revoke confirm's pattern. On failure the user keeps their context
  // (retry or cancel) instead of a toast over an already-closed dialog.
  window.TurnstoneHatch.setBusy(dlg, true);

  const url = "/v1/api/workstreams/" + encodeURIComponent(wsId) + "/delete";

  authFetch(url, { method: "POST" })
    .then(function (r) {
      if (!r.ok)
        throw new Error("Failed to delete workstream (HTTP " + r.status + ")");
      return r.json();
    })
    .then(function () {
      window.TurnstoneHatch.setBusy(dlg, false);
      cancelDeleteWs();
      // Update local state directly — don't call closeWorkstream which
      // would send a redundant POST to /close for an already-deleted ws.
      delete workstreams[wsId];
      closeSessionPane(wsId);
      fireRender();
      if (!Object.keys(workstreams).length) {
        loadDashboard();
        showDashboard();
      }
      showToast("Workstream deleted", "success");
    })
    .catch(function (err) {
      window.TurnstoneHatch.setBusy(dlg, false);
      showToast(err.message || "Failed to delete workstream", "error");
    });
}

function getCurrentWsId() {
  const pm = window.TS_SHELL && window.TS_SHELL.panes;
  const a = pm && pm.getActive ? pm.getActive() : null;
  return a && a.type === "interactive" ? a.rawId : "";
}

// Human-readable byte size for attachment chips + over-cap errors (mirrors
// composer_attachments.js's IIFE-local formatSize, which isn't a global).  The
// four call sites referenced this with no definition in scope — a ReferenceError
// that broke file staging (pre-existing, not introduced by the L-shell work).
function _formatAttachSize(n) {
  if (n < 1024) return n + " B";
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
  return (n / (1024 * 1024)).toFixed(1) + " MB";
}

function forkWorkstream(optWsId) {
  const wsId = optWsId || getCurrentWsId();
  if (!wsId) return;
  showNewWsModal(wsId);
}

// formatRelativeTime moved to /shared/utils.js so both surfaces share it.

function dashboardSwitchWorkstream(wsId) {
  if (workstreams[wsId]) {
    hideDashboard();
    switchTab(wsId);
  } else loadDashboard();
}

function dashboardResumeSession(wsId) {
  authFetch("/v1/api/workstreams/" + encodeURIComponent(wsId) + "/open", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
  })
    .then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    })
    .then(function (data) {
      if (!data.ws_id) return;
      workstreams[data.ws_id] = { name: data.name, state: "idle" };
      switchTab(data.ws_id);
      hideDashboard();
    })
    .catch(function (err) {
      showToast("Failed to open workstream", "error");
    });
}

// Staged files for the dashboard composer. Reuses the same file-list pattern
// as the new-workstream modal but lives independently so the two flows don't
// stomp on each other's state.
let _dashboardStagedFiles = [];

// Per-kind size caps mirrored from turnstone/core/attachments.py — keep in sync.
const _DASH_IMAGE_CAP = 4 * 1024 * 1024;
const _DASH_TEXT_CAP = 512 * 1024;
const _DASH_MAX_FILES = 10;

function _renderDashboardChips() {
  const chipsEl = document.getElementById("dashboard-attach-chips");
  if (!chipsEl) return;
  chipsEl.textContent = "";
  for (let i = 0; i < _dashboardStagedFiles.length; i++) {
    (function (idx) {
      const f = _dashboardStagedFiles[idx];
      const chip = document.createElement("span");
      chip.className = "new-ws-attach-chip";
      chip.setAttribute("role", "listitem");
      const label = document.createElement("span");
      label.className = "new-ws-attach-chip-name";
      label.textContent = f.name;
      label.title = f.name + " (" + f.size + " bytes)";
      chip.appendChild(label);
      const size = document.createElement("span");
      size.className = "new-ws-attach-chip-size";
      size.textContent = _formatAttachSize(f.size);
      chip.appendChild(size);
      const rm = document.createElement("button");
      rm.type = "button";
      rm.className = "new-ws-attach-chip-remove";
      rm.setAttribute("aria-label", "Remove " + f.name);
      rm.textContent = "\u00d7";
      rm.onclick = function () {
        _dashboardStagedFiles.splice(idx, 1);
        _renderDashboardChips();
        _refreshDashboardSubmitLabel();
      };
      chip.appendChild(rm);
      chipsEl.appendChild(chip);
    })(i);
  }
}

function _addDashboardFiles(files) {
  for (let i = 0; i < files.length; i++) {
    const f = files[i];
    if (_dashboardStagedFiles.length >= _DASH_MAX_FILES) {
      _dashboardError(
        "At most " + _DASH_MAX_FILES + " attachments per workstream",
      );
      return;
    }
    // Drag-drop bypasses the <input accept="..."> filter, so re-check
    // against the server's allowlist before the upload roundtrip.
    if (!_isAttachmentAllowed(f)) {
      _dashboardError(
        "Unsupported file type: " +
          f.name +
          " (allowed: png/jpeg/gif/webp images, text)",
      );
      return;
    }
    const isImage = (f.type || "").indexOf("image/") === 0;
    const cap = isImage ? _DASH_IMAGE_CAP : _DASH_TEXT_CAP;
    if (f.size > cap) {
      _dashboardError(
        f.name + " exceeds the " + _formatAttachSize(cap) + " cap",
      );
      return;
    }
    _dashboardStagedFiles.push(f);
  }
  _renderDashboardChips();
  _refreshDashboardSubmitLabel();
}

let _dashboardErrorTimer = null;

function _dashboardError(msg) {
  // Live-region message + outline.  title= alone is invisible to screen
  // readers and on touch devices, so we surface the message visibly
  // beneath the textarea via aria-live="polite".
  const input = document.getElementById("dashboard-input");
  const errEl = document.getElementById("dashboard-error");
  if (errEl) {
    errEl.textContent = msg;
  }
  if (input) {
    input.classList.add("dashboard-input-error");
  }
  if (_dashboardErrorTimer) clearTimeout(_dashboardErrorTimer);
  _dashboardErrorTimer = setTimeout(function () {
    if (input) input.classList.remove("dashboard-input-error");
    if (errEl) errEl.textContent = "";
    _dashboardErrorTimer = null;
  }, 5000);
}

function _refreshDashboardSubmitLabel() {
  const btn = document.getElementById("dashboard-submit-btn");
  if (!btn) return;
  const input = document.getElementById("dashboard-input");
  const hasText = input && input.value.trim().length > 0;
  const hasFiles = _dashboardStagedFiles.length > 0;
  btn.textContent = hasText || hasFiles ? "Send" : "Create";
}

// Format a resolved alias with its model suffix the same way as the
// dropdown rows ("alias (model)", or just "alias" when they coincide).
// Returns "" when alias is empty or unknown so callers fall back to a
// neutral placeholder.
function _resolveModelLabel(alias, models) {
  if (!alias) return "";
  for (let i = 0; i < (models || []).length; i++) {
    const m = models[i];
    if (m.alias === alias) {
      return m.alias === m.model ? m.alias : m.alias + " (" + m.model + ")";
    }
  }
  return "";
}

function _loadDashboardOptionsLists() {
  // Models
  const modelSel = document.getElementById("dashboard-model");
  const judgeSel = document.getElementById("dashboard-judge-model");
  if (modelSel && modelSel.options.length <= 1) {
    authFetch("/v1/api/models")
      .then(function (r) {
        return r.json();
      })
      .then(function (data) {
        (data.models || []).forEach(function (m) {
          const opt = document.createElement("option");
          opt.value = m.alias;
          opt.textContent =
            m.alias === m.model ? m.alias : m.alias + " (" + m.model + ")";
          modelSel.appendChild(opt);
          if (judgeSel) {
            const jOpt = document.createElement("option");
            jOpt.value = m.alias;
            jOpt.textContent = opt.textContent;
            judgeSel.appendChild(jOpt);
          }
        });
        // Surface the resolved defaults in the placeholder rows so the
        // panel shows which model actually runs when left untouched —
        // mirrors the coordinator launcher.  The judge tracks the
        // per-workstream agent model unless judge.model is explicitly
        // configured, so keep the "(agent model)" wording in that case
        // rather than advertising a fixed alias the judge won't use.
        const modelDefault = _resolveModelLabel(
          data.default_alias || "",
          data.models || [],
        );
        modelSel.options[0].textContent = modelDefault
          ? "Default — " + modelDefault
          : "Default model";
        if (judgeSel) {
          const judgeDefault = _resolveModelLabel(
            data.judge_default_alias || "",
            data.models || [],
          );
          judgeSel.options[0].textContent = judgeDefault
            ? "Default — " + judgeDefault
            : "Default (agent model)";
        }
      })
      .catch(function () {
        /* default model still works */
      });
  }
  // Skills
  const skillSel = document.getElementById("dashboard-skill");
  if (skillSel && skillSel.options.length <= 1) {
    authFetch("/v1/api/skills")
      .then(function (r) {
        return r.json();
      })
      .then(function (data) {
        (data.skills || []).forEach(function (t) {
          const opt = document.createElement("option");
          opt.value = t.name;
          let label = t.name;
          if (t.is_default) label += " (default)";
          if (t.origin === "mcp") label += " [MCP]";
          opt.textContent = label;
          skillSel.appendChild(opt);
        });
      })
      .catch(function () {
        /* ignore */
      });
  }

  // Project picker — refresh the shared cache then repaint (also feeds the
  // rail's group-by-project).  Re-fetched each time the options open so a
  // project created elsewhere appears without a page reload.
  const projSel = document.getElementById("dashboard-project");
  if (projSel && window.TurnstoneProjects) {
    window.TurnstoneProjects.refreshProjects().then(function () {
      _populateProjectSelect(projSel);
    });
  }
}

// localStorage key for the dashboard composer's Options-panel disclosure
// state — power users who set non-default model/skill repeatedly want the
// panel to stay open across reloads instead of clicking it every time.
const _DASH_OPTIONS_LS_KEY = "turnstone.dashboard.options_open";
// In-memory fallback for environments where localStorage throws (private
// mode, storage quota, embedded WebViews).  null means "no preference
// recorded this session yet — use the closed default".
let _dashOptionsOpenSession = null;

function _setDashboardOptionsOpen(open) {
  const panel = document.getElementById("dashboard-options");
  const btn = document.getElementById("dashboard-options-btn");
  if (!panel || !btn) return;
  if (open) {
    panel.removeAttribute("hidden");
    btn.setAttribute("aria-expanded", "true");
  } else {
    panel.setAttribute("hidden", "");
    btn.setAttribute("aria-expanded", "false");
  }
}

function _toggleDashboardOptions() {
  const panel = document.getElementById("dashboard-options");
  if (!panel) return;
  const nextOpen = panel.hasAttribute("hidden");
  _setDashboardOptionsOpen(nextOpen);
  _dashOptionsOpenSession = nextOpen;
  try {
    localStorage.setItem(_DASH_OPTIONS_LS_KEY, nextOpen ? "1" : "0");
  } catch (_) {
    /* localStorage unavailable — _dashOptionsOpenSession above keeps the
       state for this session so a hide/show cycle preserves the choice. */
  }
}

function _restoreDashboardOptionsState() {
  // Read order: localStorage (cross-session) → in-memory session value
  // → closed default.  Only override based on a genuinely-successful
  // localStorage read; on throw, fall back to the session value so the
  // panel stays where the user last put it within the same tab.
  let saved = null;
  let lsAvailable = true;
  try {
    saved = localStorage.getItem(_DASH_OPTIONS_LS_KEY);
  } catch (_) {
    lsAvailable = false;
  }
  let open;
  if (lsAvailable && saved !== null) {
    open = saved === "1";
  } else if (_dashOptionsOpenSession !== null) {
    open = _dashOptionsOpenSession;
  } else {
    open = false;
  }
  _setDashboardOptionsOpen(open);
}

// Update the inline summary chip beside the Options button when any of
// model / judge_model / skill is non-default.  Helps users see at a
// glance that they've overridden defaults — without having to expand
// the panel.  Hidden when everything is default.
function _refreshDashboardOptionsSummary() {
  const summary = document.getElementById("dashboard-options-summary");
  if (!summary) return;
  const bits = [];
  const modelSel = document.getElementById("dashboard-model");
  const judgeSel = document.getElementById("dashboard-judge-model");
  const skillSel = document.getElementById("dashboard-skill");
  if (modelSel && modelSel.value) bits.push(modelSel.value);
  if (judgeSel && judgeSel.value) bits.push("judge: " + judgeSel.value);
  if (skillSel && skillSel.value) bits.push(skillSel.value);
  if (bits.length === 0) {
    summary.textContent = "";
    summary.setAttribute("hidden", "");
    return;
  }
  summary.textContent = bits.join(" · ");
  summary.removeAttribute("hidden");
}

// Unified dashboard submit. Replaces the old "click button → modal" +
// "press Enter → quick-send-empty-config" split. One path: build the
// create payload from text + attachments + options, send it, switch.
function dashboardSubmit() {
  const input = document.getElementById("dashboard-input");
  const btn = document.getElementById("dashboard-submit-btn");
  const text = input.value.trim();
  const staged = _dashboardStagedFiles.slice();

  const body = {};
  const model = document.getElementById("dashboard-model").value.trim();
  const judge = document.getElementById("dashboard-judge-model").value.trim();
  const skill = document.getElementById("dashboard-skill").value;
  const projEl = document.getElementById("dashboard-project");
  const project_id = projEl ? projEl.value : "";
  if (model) body.model = model;
  if (judge) body.judge_model = judge;
  if (skill) body.skill = skill;
  if (project_id) body.project_id = project_id;
  if (text) body.initial_message = text;

  input.disabled = true;
  btn.disabled = true;

  let fetchOpts;
  if (staged.length > 0) {
    const form = new FormData();
    form.append("meta", JSON.stringify(body));
    for (let i = 0; i < staged.length; i++) {
      form.append("file", staged[i], staged[i].name);
    }
    fetchOpts = { method: "POST", body: form };
  } else {
    fetchOpts = {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    };
  }

  authFetch("/v1/api/workstreams/new", fetchOpts)
    .then(function (r) {
      return r.json();
    })
    .then(function (data) {
      input.disabled = false;
      btn.disabled = false;
      if (data.error || !data.ws_id) {
        _dashboardError(data.error || "Failed to create workstream");
        return;
      }
      workstreams[data.ws_id] = {
        name: data.name,
        state: "idle",
        project_id: body.project_id || null,
      };
      switchTab(data.ws_id);
      hideDashboard();
    })
    .catch(function (err) {
      input.disabled = false;
      btn.disabled = false;
      // authFetch throws Error("auth") when the user is signed out and the
      // login modal has already been surfaced; suppress the redundant
      // error toast in that case.  Otherwise fall back to a generic
      // string so we never render "Connection error: undefined".
      if (err && err.message === "auth") return;
      const detail = (err && err.message) || "Unable to reach the server";
      _dashboardError("Connection error: " + detail);
    });
}

// ===========================================================================
//  11. Global SSE
// ===========================================================================

function connectGlobalSSE() {
  if (globalEvtSource) {
    globalEvtSource.close();
    globalEvtSource = null;
  }
  // Manual-reconnect path threads ``?last_event_id=N`` because the
  // EventSource constructor can't set headers; native auto-reconnect
  // on the same source uses the header directly.
  let globalUrl = "/v1/api/events/global";
  if (globalLastEventId) {
    globalUrl += "?last_event_id=" + encodeURIComponent(globalLastEventId);
  }
  globalEvtSource = new EventSource(globalUrl);
  globalEvtSource.onopen = function () {
    globalRetryDelay = 1000;
  };
  globalEvtSource.onmessage = function (e) {
    // Capture lastEventId BEFORE JSON.parse (see Pane.connectSSE
    // onmessage for full rationale).
    if (globalEvtSource && globalEvtSource.lastEventId) {
      globalLastEventId = globalEvtSource.lastEventId;
    }
    // Guarded parse: the cursor above has already advanced past this frame,
    // so a parse failure is a permanently-lost roster mutation — resync the
    // roster from REST instead of silently drifting (a dropped ws_created
    // renders as a conversation that never appears; a dropped ws_closed as
    // a ghost row forever).
    let data = null;
    try {
      data = JSON.parse(e.data);
    } catch (err) {
      console.warn("global SSE: malformed frame — resyncing roster", err);
      resyncRoster();
      return;
    }
    if (data.type === "node_snapshot") {
      // Recovery floor: the server emits this when our resume cursor
      // predates its ring buffer (fresh connect, or a truncated gap after
      // hidden-tab/sleep).  The snapshot carries the FULL workstream
      // inventory — rebuild the roster wholesale; per-ws panes re-sync
      // through their own Tier-2 streams.  Eviction is safe here (and only
      // here): the snapshot is serialized with ws_created/ws_closed on the
      // stream itself.
      applyRosterSnapshot(data.workstreams || [], { evict: true });
    } else if (data.type === "replay_truncated") {
      // Events between our cursor and the buffer head are gone for good.
      // The node_snapshot that follows rebuilds the roster; refetch too so
      // recovery doesn't depend on event ordering.
      resyncRoster();
    } else if (data.type === "ws_state") {
      updateTabIndicator(data.ws_id, data.state, {
        tokens: data.tokens,
        context_ratio: data.context_ratio,
        activity: data.activity,
        activity_state: data.activity_state,
      });
    } else if (data.type === "ws_activity") {
      const row = document.querySelector(
        '#dash-ws-table .dash-row[data-ws-id="' + data.ws_id + '"]',
      );
      if (row) {
        const sub = row.querySelector(".dash-row-sub");
        if (sub) {
          sub.textContent = data.activity || "";
          if (data.activity_state === "approval")
            sub.classList.add("sub-attention");
          else sub.classList.remove("sub-attention");
        }
      }
    } else if (data.type === "ws_rename") {
      if (workstreams[data.ws_id]) workstreams[data.ws_id].name = data.name;
      // The open pane keeps its own name via its Tier-2 stream; the rail + tab
      // title refresh from the roster.
      fireRender();
    } else if (data.type === "ws_created") {
      workstreams[data.ws_id] = workstreams[data.ws_id] || {};
      workstreams[data.ws_id].name = data.name || data.ws_id.slice(0, 6);
      workstreams[data.ws_id].state = "idle";
      // Carry the attached project so the rail groups the new session
      // without waiting for a roster refetch (null = unattached).
      workstreams[data.ws_id].project_id = data.project_id || null;
      renderTabBar();
    } else if (data.type === "ws_closed") {
      const wsId = data.ws_id;
      delete workstreams[wsId];
      closeSessionPane(wsId); // PaneManager tears down its Tier-2 stream
      fireRender();
      if (data.reason === "evicted") {
        showToast(
          "Evicted" + (data.name ? ": " + data.name : "") + " (capacity)",
        );
      }
      if (!Object.keys(workstreams).length) showDashboard();
    } else if (data.type === "settings_changed") {
      // Re-load interface settings and apply immediately
      loadInterfaceSettings();
    }
  };
  globalEvtSource.onerror = function () {
    // Do NOT close globalEvtSource for transient errors — native
    // EventSource auto-reconnect handles them with the
    // ``Last-Event-ID`` header automatically (now that the global
    // SSE handler emits ``id:`` on every buffered event).  Closing
    // here would defeat native reconnect.  See PR-D briefing § 3.3
    // and the per-pane handler above for the same pattern.
    //
    // The 401 probe stays — an authentication failure is a terminal
    // condition (the user must log in) and merits an explicit
    // close + showLogin.  ``_reconnectDeadSSEs`` (visibilitychange /
    // focus listener) covers the truly-CLOSED case.
    fetch("/v1/api/workstreams").then(function (r) {
      if (r.status === 401) {
        if (globalEvtSource) {
          globalEvtSource.close();
          globalEvtSource = null;
        }
        showLogin();
      }
    });
  };
}

// ===========================================================================
//  12. MCP consent badge (standalone pending-consent indicator)
//
//  The tool-output / media / MCP-error / verdict renderers that used to live in
//  this section moved to shared_static/interactive.js with the Pane.  What
//  stays here is the standalone consent-badge subsystem: it owns the pending set
//  and drives the rail's Manage > Connections row badge (via the TS_SHELL bridge
//  — `setRowBadge`).  An interactive pane only NOTIFIES it (the shared host
//  bridges `onConsentDetected` to the TS_APP seam below); `loadPendingConsents`
//  hydrates it on boot.  The settings-gear it used to hang on is retired.
// ===========================================================================

// Module-level set of servers with an unresolved consent prompt; drives the
// Manage-row badge so the user has a stable signal that re-consent is pending
// after the inline card scrolls out of view.
const _pendingConsentServers = new Set();

function _onConsentDetected(server) {
  if (typeof server === "string" && server) {
    _pendingConsentServers.add(server);
    _refreshConsentBadge();
  }
}

function _clearConsentBadge() {
  _pendingConsentServers.clear();
  _refreshConsentBadge();
}

// Hydrate the pending-consent badge from the Phase 9 persistence endpoint
// on dashboard load.  Closes the gap that pre-Phase-9 left open: a
// scheduled / channel-driven run that hit ``mcp_consent_required`` while
// the user wasn't online produced an in-flight SSE event that nobody saw.
// The endpoint short-circuits to ``{pending: 0}`` on installs with no
// ``auth_type=oauth_user`` MCP servers, so the call is cheap on local-
// auth deployments.  Failures are silent — the badge will be re-driven
// by the next in-flight tool error if any.
function loadPendingConsents() {
  authFetch("/v1/api/mcp/oauth/pending")
    .then(function (r) {
      if (!r.ok) return null;
      return r.json();
    })
    .then(function (data) {
      if (!data || !Array.isArray(data.servers)) return;
      for (let i = 0; i < data.servers.length; i++) {
        const row = data.servers[i];
        if (row && typeof row.server_name === "string") {
          _pendingConsentServers.add(row.server_name);
        }
      }
      _refreshConsentBadge();
    })
    .catch(function () {
      // Endpoint failures must not block dashboard init.
    });
}

// The Manage tab the pending-consent badge rides on.  The standalone's Manage IA
// (TS_ADMIN.ia, below) is a single Extensions > Connections tab where MCP server
// connections live; the badge surfaces there (and, when that group is collapsed,
// on its head — the rail handles that).  The retired settings-gear it used to
// hang on is gone with the L-shell renovation.
const _CONSENT_BADGE_TAB = "connections";

function _refreshConsentBadge() {
  const n = _pendingConsentServers.size;
  // Drive the rail's generic Manage-row badge through the shell bridge (classic
  // app.js can't import the ESM rail module).  The chip's own ⚠ glyph + count
  // carry the signal; `label` keeps the accessible name in lockstep.  A no-op
  // before the rail mounts — `loadPendingConsents` re-drives it after boot.
  const shell = window.TS_SHELL;
  if (!shell || typeof shell.setRowBadge !== "function") return;
  const label =
    n === 0
      ? ""
      : n + " MCP server" + (n === 1 ? "" : "s") + " awaiting consent";
  shell.setRowBadge(_CONSENT_BADGE_TAB, n, label);
}

/**
 * Detect a structured MCP error envelope.  Returns the inner ``error``
 * object on shape match, null otherwise.  Recognised codes:
 *   - mcp_consent_required (carries optional consent_url + scopes_required)
 *   - mcp_insufficient_scope (carries consent_url + scopes_required)
 *   - mcp_tool_call_forbidden / mcp_resource_read_forbidden / mcp_prompt_get_forbidden
 *   - mcp_token_undecryptable_key_unknown (operator action)
 *   - mcp_oauth_url_insecure (operator action)
 */
/**
 * Render the action card for an MCP error envelope.  Mirrors the
 * media-embed pattern: visible card on top, collapsible raw JSON below.
 */
function _announce(text) {
  const el = document.getElementById("toast");
  if (!el) return;
  // Re-set textContent in two ticks so screen readers re-announce even
  // when the message is identical to the previous one.
  el.textContent = "";
  setTimeout(function () {
    el.textContent = text;
  }, 50);
}

// ===========================================================================
//  14. Keyboard shortcuts
// ===========================================================================

// Dashboard composer wiring — Enter (no shift) submits, input refreshes the
// button label, paperclip + drag-drop + paste-image stage files, options
// toggle expands the dropdown panel.
(function () {
  const input = document.getElementById("dashboard-input");
  const attachBtn = document.getElementById("dashboard-attach-btn");
  const attachInput = document.getElementById("dashboard-attach-input");
  const optionsBtn = document.getElementById("dashboard-options-btn");
  const composer = document.getElementById("dashboard-composer");
  if (!input) return;

  input.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey && !e.altKey) {
      e.preventDefault();
      dashboardSubmit();
    }
  });
  input.addEventListener("input", _refreshDashboardSubmitLabel);
  input.addEventListener("paste", function (e) {
    if (!e.clipboardData) return;
    const items = e.clipboardData.items || [];
    const pasted = [];
    for (let i = 0; i < items.length; i++) {
      if (items[i].kind === "file") {
        const f = items[i].getAsFile();
        if (f) pasted.push(f);
      }
    }
    if (pasted.length) {
      e.preventDefault();
      _addDashboardFiles(pasted);
    }
  });

  if (attachBtn && attachInput) {
    attachBtn.addEventListener("click", function () {
      attachInput.click();
    });
    attachInput.addEventListener("change", function () {
      if (attachInput.files && attachInput.files.length) {
        _addDashboardFiles(attachInput.files);
      }
      attachInput.value = "";
    });
  }
  if (optionsBtn) {
    optionsBtn.addEventListener("click", _toggleDashboardOptions);
  }
  // Keep the inline summary chip in sync with whichever non-default
  // model / judge / skill is selected.  Listening on the options panel
  // catches all three selects with one handler.
  const optionsPanel = document.getElementById("dashboard-options");
  if (optionsPanel) {
    optionsPanel.addEventListener("change", _refreshDashboardOptionsSummary);
  }
  if (composer) {
    composer.addEventListener("dragover", function (e) {
      if (
        e.dataTransfer &&
        Array.from(e.dataTransfer.types || []).includes("Files")
      ) {
        e.preventDefault();
        composer.classList.add("dashboard-composer-drop");
      }
    });
    composer.addEventListener("dragleave", function (e) {
      if (e.target === composer)
        composer.classList.remove("dashboard-composer-drop");
    });
    composer.addEventListener("drop", function (e) {
      composer.classList.remove("dashboard-composer-drop");
      if (
        e.dataTransfer &&
        e.dataTransfer.files &&
        e.dataTransfer.files.length
      ) {
        e.preventDefault();
        _addDashboardFiles(e.dataTransfer.files);
      }
    });
  }
})();

// ===========================================================================
//  15. MCP server connections settings panel
// ===========================================================================

let _pendingRevokeServer = null;

function openSettingsPanel() {
  // MCP connections render in the Admin pane's Connections panel (#view-admin),
  // which the shell shows when the Manage > Connections row opens it.  This is a
  // pane, not a modal — so there is no overlay to show and no focus-trap; just
  // (re)load the table into the panel's existing #settings-mcp-* nodes.
  loadMcpConnections();
}

function closeSettingsPanel() {
  // If the nested revoke confirmation is still up, close it first so
  // hiding the parent panel doesn't strand an open dialog.
  const inner = document.getElementById("revoke-mcp-dialog");
  if (inner && inner.open) {
    cancelRevokeMcp();
  }
}

// ---------------------------------------------------------------------------
//  MCP connections panel (Manage -> Connections via the rail; the old gear
//  dropdown that fronted it was retired with the split-pane tab bar)
// ---------------------------------------------------------------------------

function loadMcpConnections() {
  const loadingEl = document.getElementById("settings-mcp-loading");
  const emptyEl = document.getElementById("settings-mcp-empty");
  const tableEl = document.getElementById("settings-mcp-table");
  const errorEl = document.getElementById("settings-mcp-error");
  if (!loadingEl || !emptyEl || !tableEl || !errorEl) return;
  loadingEl.style.display = "";
  emptyEl.style.display = "none";
  tableEl.style.display = "none";
  errorEl.style.display = "none";

  authFetch("/v1/api/mcp/oauth/connections")
    .then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    })
    .then(function (data) {
      loadingEl.style.display = "none";
      const connections =
        data && Array.isArray(data.connections) ? data.connections : [];
      renderMcpConnections(connections);
      // Clear AFTER the table renders so the badge reflects "user has
      // seen current state" rather than "user opened the panel" — a
      // failed fetch keeps the pending-consent signal until the user
      // gets confirmation that consents are in fact reachable.
      _clearConsentBadge();
      // Phase 9: re-hydrate the badge from the persistent pending-
      // consent table.  Phase 8 cleared in-memory state on settings-
      // panel open (signal-acknowledged); Phase 9 records are
      // DB-backed, so we re-pull them now to keep the badge in sync
      // with what's actually pending across page lifetimes.
      loadPendingConsents();
    })
    .catch(function (err) {
      loadingEl.style.display = "none";
      errorEl.style.display = "";
      errorEl.textContent = "Failed to load connections: " + err.message;
    });
}

function _clearChildren(node) {
  while (node && node.firstChild) node.removeChild(node.firstChild);
}

function renderMcpConnections(list) {
  const emptyEl = document.getElementById("settings-mcp-empty");
  const tableEl = document.getElementById("settings-mcp-table");
  const tbody = document.getElementById("settings-mcp-tbody");
  if (!emptyEl || !tableEl || !tbody) return;
  if (!list.length) {
    tableEl.style.display = "none";
    emptyEl.style.display = "";
    return;
  }
  emptyEl.style.display = "none";
  tableEl.style.display = "";
  _clearChildren(tbody);
  for (let i = 0; i < list.length; i++) {
    const conn = list[i];
    const tr = document.createElement("tr");

    const serverTd = document.createElement("td");
    serverTd.textContent = conn.server_name || "";
    tr.appendChild(serverTd);

    const scopesTd = document.createElement("td");
    scopesTd.textContent = conn.scopes || "(none)";
    tr.appendChild(scopesTd);

    const createdTd = document.createElement("td");
    createdTd.textContent = _formatRelativeTimestamp(conn.created);
    createdTd.title = conn.created || "";
    tr.appendChild(createdTd);

    const refreshedTd = document.createElement("td");
    if (conn.last_refreshed) {
      refreshedTd.textContent = _formatRelativeTimestamp(conn.last_refreshed);
      refreshedTd.title = conn.last_refreshed;
    } else {
      refreshedTd.textContent = "—";
    }
    tr.appendChild(refreshedTd);

    const actionTd = document.createElement("td");
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "settings-revoke-btn";
    btn.textContent = "Revoke";
    const serverNameForRevoke = conn.server_name || "";
    btn.setAttribute(
      "aria-label",
      "Revoke connection to " + serverNameForRevoke,
    );
    (function (name) {
      btn.addEventListener("click", function () {
        promptRevokeMcp(name);
      });
    })(serverNameForRevoke);
    actionTd.appendChild(btn);
    tr.appendChild(actionTd);

    tbody.appendChild(tr);
  }
}

function promptRevokeMcp(server) {
  if (!server) return;
  _pendingRevokeServer = server;
  const msg = document.getElementById("revoke-mcp-message");
  if (msg) {
    msg.textContent =
      "Revoke the connection to " +
      server +
      "? Tools that need this server will require re-consent.";
  }
  document.getElementById("revoke-mcp-confirm").onclick = confirmRevokeMcp;
  // Cancel carries the autofocus (the console confirm-dialog rule).
  window.TurnstoneHatch.openDialog(
    document.getElementById("revoke-mcp-dialog"),
    {
      onClose: function () {
        _pendingRevokeServer = null;
      },
    },
  );
}

function cancelRevokeMcp() {
  const d = document.getElementById("revoke-mcp-dialog");
  if (d && d.open) d.close();
}

function confirmRevokeMcp() {
  const server = _pendingRevokeServer;
  if (!server) {
    cancelRevokeMcp();
    return;
  }
  const dlg = document.getElementById("revoke-mcp-dialog");
  window.TurnstoneHatch.setBusy(dlg, true);
  authFetch("/v1/api/mcp/oauth/connections/" + encodeURIComponent(server), {
    method: "DELETE",
  })
    .then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      window.TurnstoneHatch.setBusy(dlg, false);
      cancelRevokeMcp();
      showToast("Revoked connection to " + server);
      loadMcpConnections();
    })
    .catch(function (err) {
      window.TurnstoneHatch.setBusy(dlg, false);
      cancelRevokeMcp();
      showToast("Failed to revoke: " + err.message);
    });
}

function _formatRelativeTimestamp(iso) {
  if (!iso) return "—";
  try {
    const d = new Date(iso);
    if (isNaN(d.getTime())) return iso;
    const now = new Date();
    const diffMs = now.getTime() - d.getTime();
    const sec = Math.round(diffMs / 1000);
    if (sec < 60) return "just now";
    if (sec < 3600) return Math.round(sec / 60) + "m ago";
    if (sec < 86400) return Math.round(sec / 3600) + "h ago";
    return Math.round(sec / 86400) + "d ago";
  } catch (e) {
    return iso;
  }
}

document.addEventListener("keydown", function (e) {
  // Defer while a document-modal hatch dialog is open — native dialogs own
  // their Escape, and global shortcuts must not fire under the top layer.
  if (document.querySelector("dialog:modal")) return;
  if (e.key === "Escape" && dashboardVisible) {
    e.preventDefault();
    hideDashboard();
    return;
  }

  // Ctrl+D: toggle dashboard
  if (e.ctrlKey && e.key === "d") {
    e.preventDefault();
    toggleDashboard();
    return;
  }
  // Ctrl+T: new tab
  if (e.ctrlKey && e.key === "t") {
    e.preventDefault();
    newWorkstream();
    return;
  }
  // Ctrl+1..9: switch tabs
  if (e.ctrlKey && e.key >= "1" && e.key <= "9") {
    e.preventDefault();
    const idx = parseInt(e.key) - 1;
    const wsIds = Object.keys(workstreams);
    if (idx < wsIds.length) switchTab(wsIds[idx]);
    return;
  }
  // Workstream action shortcuts — only preventDefault when a workstream
  // is active, so native browser shortcuts (e.g. Ctrl+Shift+R hard reload)
  // still work when no workstream is focused.
  if (e.ctrlKey && e.shiftKey) {
    const wsActionKey = e.key.toLowerCase();
    const activeWsId = !dashboardVisible && getCurrentWsId();
    if (wsActionKey === "e" && activeWsId) {
      e.preventDefault();
      editWorkstreamTitle();
      return;
    }
    if (wsActionKey === "f" && activeWsId) {
      e.preventDefault();
      forkWorkstream();
      return;
    }
    // X not D — D conflicts with Chrome DevTools
    if (
      wsActionKey === "x" &&
      activeWsId &&
      Object.keys(workstreams).length > 1
    ) {
      e.preventDefault();
      confirmDeleteWorkstream();
      return;
    }
  }
  // Ctrl+W: close current workstream tab
  if (e.ctrlKey && !e.shiftKey && e.key === "w") {
    if (Object.keys(workstreams).length > 1) {
      e.preventDefault();
      closeWorkstream(getCurrentWsId());
    }
    return;
  }
});

// ===========================================================================
//  16. Init
// ===========================================================================

// Rebuild the roster from a node_snapshot payload (workstream items keyed by
// ``id`` — the snapshot mirrors the console-collector projection, not the
// REST list's ``ws_id``).  ``opts.evict``: remove roster entries missing
// from the list and close their panes.  Eviction is ONLY safe for the
// in-stream node_snapshot — it is serialized with ws_created/ws_closed on
// the SSE stream, so it can't race a roster mutation.  An out-of-band REST
// snapshot (resyncRoster) can be built server-side BEFORE a create whose
// ws_created the client already consumed; evicting from it would close a
// live, freshly-opened conversation.  REST resyncs therefore merge only;
// missed-ws_closed ghosts heal on the next in-stream snapshot.
function applyRosterSnapshot(list, opts) {
  const evict = !!(opts && opts.evict);
  const seen = {};
  (list || []).forEach(function (ws) {
    if (!ws || !ws.id) return;
    seen[ws.id] = true;
    const cur = workstreams[ws.id] || {};
    cur.name = ws.name || cur.name || ws.id.slice(0, 6);
    cur.state = ws.state || cur.state || "idle";
    cur.parent_ws_id = ws.parent_ws_id || null;
    cur.project_id = ws.project_id || null;
    workstreams[ws.id] = cur;
  });
  if (evict) {
    const pm = window.TS_SHELL && window.TS_SHELL.panes;
    for (const id in workstreams) {
      if (!seen[id]) {
        // Gap recovery can retire a session the user is LOOKING at — the
        // live ws_closed (and its eviction toast) is exactly what was missed
        // during the gap — so closing the pane wordlessly would yank it
        // mid-read.  Toast only when an open pane goes away; mass ghost-row
        // cleanup in the rail stays quiet.
        const wasOpen = !!(pm && pm.hasPane("interactive", id));
        const name =
          (workstreams[id] && workstreams[id].name) || id.slice(0, 6);
        delete workstreams[id];
        closeSessionPane(id);
        if (wasOpen) showToast("Session ended: " + name);
      }
    }
  }
  fireRender();
}

// REST fallback for the same recovery (replay_truncated / a malformed frame
// whose cursor already advanced).  MERGE-ONLY (see applyRosterSnapshot) and
// gated on r.ok — a 503 during a node restart parses as a JSON error body
// with no ``workstreams``, which must not read as an authoritative empty
// roster.  In-flight latch: one resync at a time — repeated triggers during
// an outage must not stack fetches.
let _rosterResyncInflight = null;
function resyncRoster() {
  if (_rosterResyncInflight) return _rosterResyncInflight;
  _rosterResyncInflight = authFetch("/v1/api/workstreams")
    .then(function (r) {
      if (!r.ok) return null;
      return r.json();
    })
    .then(function (data) {
      if (!data || !data.workstreams) return;
      applyRosterSnapshot(
        data.workstreams.map(function (ws) {
          return {
            id: ws.ws_id,
            name: ws.name,
            state: ws.state,
            parent_ws_id: ws.parent_ws_id,
            project_id: ws.project_id,
          };
        }),
      );
    })
    .catch(function () {
      /* transient — the next snapshot or reconnect heals the roster */
    })
    .finally(function () {
      _rosterResyncInflight = null;
    });
  return _rosterResyncInflight;
}

function initWorkstreams() {
  return authFetch("/v1/api/workstreams")
    .then(function (r) {
      return r.json();
    })
    .then(function (data) {
      (data.workstreams || []).forEach(function (ws) {
        workstreams[ws.ws_id] = { name: ws.name, state: ws.state };
      });
      connectGlobalSSE();
      fireRender();
      // Deep-link: /?ws_id=… opens that session as an interactive pane (the
      // PaneManager already restored any persisted working set on its own).
      const targetWs = new URLSearchParams(location.search).get("ws_id");
      if (targetWs && workstreams[targetWs]) openSessionPane(targetWs);
    });
}

// boot() — the shell calls this (window.TS_APP.boot) once it has built the
// rail + tab bar and opened the Dashboard pane.  It starts login, the Tier-1
// roster + stream, the dashboard lists, health polling, and pending MCP
// consents.  It does NOT auto-run at parse time (the shell sequences it).
function boot() {
  _initSavedWsTable(); // substrate modules have evaluated by boot time
  initLogin();
  pollHealth();
  loadInterfaceSettings();
  initWorkstreams();
  loadPendingConsents();
  // The Dashboard pane (#main) is already mounted — populate its lists.
  loadDashboard();
  _loadDashboardOptionsLists();
  _restoreDashboardOptionsState();
  _refreshDashboardOptionsSummary();
  _refreshDashboardSubmitLabel();
}

// Free the HTTP/1.1 6-connection-per-host budget before the refresh
// document fetch starts.  Each pane holds a long-lived per-ws SSE +
// the global SSE; at 5–6 panes the cap is hit and the new document
// load queues behind the existing connections.  Chrome leaves the
// document fetch in (pending) indefinitely; Firefox surfaces
// "interrupted while page was loading" and leaves the new page
// stuck on "Loading…".  Best-effort close on unload frees the slots.
//
// Per-pane teardown goes through `disconnectSSE()` instead of a bare
// `evtSource.close()` so the pane's `_cancelTimeout` / `_forceTimeout`
// timers also get cleared.  Otherwise — in the edge case where
// beforeunload fires but navigation is then cancelled (see the
// defensive-reconnect block below) — those timers can still fire on
// a now-disconnected pane and mutate UI state.
//
// Tactical only — the canonical fix is console-side SSE fan-in
// tracked at https://github.com/turnstonelabs/turnstone/issues/540.
window.addEventListener("beforeunload", function () {
  try {
    if (globalEvtSource) {
      globalEvtSource.close();
      globalEvtSource = null;
    }
  } catch (_e) {
    /* best-effort — never block unload */
  }
});

// Defensive reconnect: covers the edge case where beforeunload fires but
// navigation is then cancelled (e.g. another beforeunload listener — present
// or future — sets returnValue and the user picks "Stay" in the dialog).
// In that path, our handler already disconnected the SSEs but the page is
// still alive with no automatic reconnect.  Both events are registered
// because they catch different cancellation shapes: visibilitychange fires
// on hide/show; focus fires when the window regains focus from a modal /
// browser-UI / OS-level interruption.  Idempotent — when SSEs are alive
// the check is a no-op, so this is also safe on every tab return.
//
// Reconnect condition handles both shapes the beforeunload handler can
// leave behind: `disconnectSSE()` nulls `evtSource`; older non-handler
// close paths may leave it non-null in CLOSED state.  Either way means
// "not actively streaming for a pane that has a workstream attached".
//
// Out of scope here: visibility-based DISCONNECT (close-on-hidden to
// support many tabs).  That belongs to the fan-in design — issue #540.
function _reconnectDeadSSEs() {
  if (!globalEvtSource || globalEvtSource.readyState === EventSource.CLOSED) {
    connectGlobalSSE();
  }
}
document.addEventListener("visibilitychange", function () {
  if (document.visibilityState === "visible") _reconnectDeadSSEs();
});
window.addEventListener("focus", _reconnectDeadSSEs);

function loadInterfaceSettings() {
  authFetch("/v1/api/admin/settings")
    .then(function (r) {
      return r.json();
    })
    .then(function (data) {
      const settings = data.settings || [];
      for (let i = 0; i < settings.length; i++) {
        const s = settings[i];
        if (s.key && s.key.indexOf("interface.") === 0) {
          const lsKey = "turnstone_" + s.key;
          try {
            // Only write server value if no local value exists — this
            // preserves the user's theme choice when switching between
            // nodes via the console proxy (each node may return a
            // different default).
            if (!localStorage.getItem(lsKey) && s.source === "storage") {
              localStorage.setItem(lsKey, s.value);
            }
          } catch (_) {}
        }
      }
      // Apply theme from localStorage (set by theme.js initTheme or
      // a previous toggle) — don't let a node's default override it.
      const theme = localStorage.getItem("turnstone_interface.theme");
      const currentTheme = document.documentElement.dataset.theme;
      if (theme) {
        const effectiveTheme = theme === "light" ? "light" : "";
        if (effectiveTheme !== currentTheme) {
          document.documentElement.dataset.theme = effectiveTheme;
          const btn = document.getElementById("theme-toggle");
          if (btn) {
            btn.textContent = theme === "light" ? "\u2600" : "\u263E";
            btn.title =
              theme === "light"
                ? "Switch to dark theme"
                : "Switch to light theme";
          }
          reRenderAllMermaid();
        }
      }
    })
    .catch(function (err) {
      // Silently ignore — settings are optional on load
    });
}

// Back/forward button: retrace dashboard -> tab navigation.
window.addEventListener("popstate", function (e) {
  _historyNavigation = true;
  try {
    if (e.state && e.state.turnstone === "workstream") {
      if (dashboardVisible) hideDashboard();
      if (e.state.wsId && workstreams[e.state.wsId]) switchTab(e.state.wsId);
    } else {
      if (!dashboardVisible) showDashboard();
    }
  } finally {
    _historyNavigation = false;
  }
});

// ===========================================================================
//  17. L-shell seams — TS_APP (single-node Tier-1) + TS_ADMIN (Manage) + showHome
//
//  The shared shell (shell.js) reads window.TS_APP for the rail's live data and
//  window.TS_ADMIN for the Manage groups.  A standalone turnstone-server is a
//  single node, so getClusterState() synthesizes a one-node cluster from the
//  flat `workstreams` roster this file maintains over /v1/api/events/global.
// ===========================================================================

// Rail re-render fan-out — the rail subscribes via TS_APP.onRender; every
// roster mutation calls fireRender() so the Workspaces section stays live.
// rAF-coalesced: the server emits ws_state at least twice per tool round for
// EVERY workstream on the node, and each subscriber repaint rebuilds the
// whole rail (replaceChildren + a listener per row) — uncoalesced, a busy
// session drove thousands of full rebuilds per hour, O(#workstreams) each.
// All subscribers are snapshot-driven repaints, so batching to one repaint
// per frame is lossless.
const _renderSubs = [];
let _renderScheduled = false;
function fireRender() {
  if (_renderScheduled) return;
  _renderScheduled = true;
  requestAnimationFrame(function () {
    _renderScheduled = false;
    for (const cb of _renderSubs) {
      try {
        cb();
      } catch (e) {
        console.error("TS_APP render subscriber failed", e);
      }
    }
  });
}

// Open / focus an interactive session as a pane (base="" local transport — the
// standalone has no node proxy; shell.js gates nodeId off caps.cluster).
function openSessionPane(wsId) {
  const pm = window.TS_SHELL && window.TS_SHELL.panes;
  if (pm) pm.openPane("interactive", wsId);
}
function closeSessionPane(wsId) {
  const pm = window.TS_SHELL && window.TS_SHELL.panes;
  if (pm) pm.close("interactive:" + wsId);
}

// Lean shims for the retired tab-bar verbs the keep-code still calls — the tab
// bar is PaneManager's now, so these re-express onto panes + the rail.
function switchTab(wsId) {
  openSessionPane(wsId);
}
function renderTabBar() {
  fireRender();
}
function updateTabIndicator(wsId, state, extra) {
  if (workstreams[wsId]) workstreams[wsId].state = state;
  fireRender(); // rail glyph (the only surface fireRender repaints)
  // Patch the Dashboard row in place — fireRender fans out to the rail, NOT the
  // #dash-ws-table cells, so without this a watched row's STATE/TOKENS/CTX go
  // stale on every ws_state tick until a full loadDashboard().  (Ported from main;
  // the .ws-tab indicator it also patched is retired with the split-pane bar.)
  const row = document.querySelector(
    '#dash-ws-table .dash-row[data-ws-id="' + wsId + '"]',
  );
  if (!row) return;
  const sd = STATE_DISPLAY[state] || STATE_DISPLAY.idle;
  row.dataset.state = state;
  const dot = row.querySelector(".dash-state-dot");
  if (dot) dot.dataset.state = state;
  const label = row.querySelector(".dash-state-label");
  if (label) {
    label.dataset.state = state;
    label.textContent = sd.symbol + " " + sd.label;
  }
  if (!extra) return;
  if (extra.tokens !== undefined) {
    const tokEl = row.querySelector(".dash-cell-tokens");
    if (tokEl)
      tokEl.textContent = extra.tokens ? formatTokens(extra.tokens) : "";
  }
  if (extra.context_ratio !== undefined) {
    const ctxEl = row.querySelector(".dash-cell-ctx");
    if (ctxEl) {
      ctxEl.className = "dash-cell-ctx " + ctxClass(extra.context_ratio);
      ctxEl.textContent =
        extra.context_ratio > 0
          ? Math.round(extra.context_ratio * 100) + "%"
          : "";
    }
  }
  if (extra.activity !== undefined) {
    const sub = row.querySelector(".dash-row-sub");
    if (sub) {
      sub.textContent = extra.activity || "";
      if (extra.activity_state === "approval")
        sub.classList.add("sub-attention");
      else sub.classList.remove("sub-attention");
    }
  }
}

// Synthesize the one-node clusterState shape the rail consumes
// (cs.nodes[nid].workstreams[]).  caps.cluster is false, so the rail's Cluster
// section is never built; only Workspaces + wsTitle read this.
function getClusterState() {
  const list = [];
  for (const id in workstreams) {
    const w = workstreams[id] || {};
    list.push({
      id: id,
      name: w.name,
      state: w.state || "idle",
      kind: "interactive",
      parent_ws_id: w.parent_ws_id || null,
      project_id: w.project_id || null,
    });
  }
  return { nodes: { local: { workstreams: list } }, overview: {} };
}

// Bucket a flat ws list by parent (standalone sessions are flat → all roots);
// shape mirrors the console seam the rail's renderWorkspaces expects.
function bucketByParent(list) {
  const byId = {};
  for (const w of list) byId[w.id] = w;
  const roots = [];
  const orphans = [];
  const childrenMap = {};
  for (const w of list) {
    const p = w.parent_ws_id;
    if (!p) roots.push(w);
    else if (byId[p]) (childrenMap[p] = childrenMap[p] || []).push(w);
    else orphans.push(w);
  }
  return { roots: roots, orphans: orphans, childrenMap: childrenMap };
}

window.showHome = function () {
  if (window.TS_SHELL && window.TS_SHELL.panes)
    window.TS_SHELL.panes.openPane("dashboard");
};

window.TS_APP = window.TS_APP || {};
window.TS_APP.getClusterState = getClusterState;
window.TS_APP.onRender = function (cb) {
  if (typeof cb === "function" && _renderSubs.indexOf(cb) < 0)
    _renderSubs.push(cb);
};
window.TS_APP.bucketByParent = bucketByParent;
window.TS_APP.boot = boot;
// Live MCP-consent notifications from an interactive pane (the shared pane host
// bridges its `onConsentDetected` here when this seam exists; the console leaves
// it undefined, so the pane no-ops there).  Adds the server to the pending set
// and re-paints the Manage-row badge.
window.TS_APP.onConsentDetected = _onConsentDetected;

// --- Manage seam: one Connections tab (MCP server connections) -------------
const _CONN_IA = [
  {
    group: "Extensions",
    tabs: [{ tab: "connections", label: "Connections", perm: null }],
  },
];
let _activeAdminTab = null;
const _adminTabSubs = [];
window.TS_ADMIN = window.TS_ADMIN || {};
window.TS_ADMIN.ia = _CONN_IA;
window.TS_ADMIN.isTabAllowed = function () {
  return true;
};
window.TS_ADMIN.getActiveTab = function () {
  return _activeAdminTab;
};
window.TS_ADMIN.onTabChange = function (cb) {
  if (typeof cb === "function") _adminTabSubs.push(cb);
};
window.TS_ADMIN.openTab = function (tab) {
  const pm = window.TS_SHELL && window.TS_SHELL.panes;
  if (pm) pm.openPane("admin");
  _activeAdminTab = tab;
  for (const cb of _adminTabSubs) {
    try {
      cb(tab);
    } catch (e) {
      console.error("TS_ADMIN tab subscriber failed", e);
    }
  }
  if (tab === "connections") openSettingsPanel();
};
