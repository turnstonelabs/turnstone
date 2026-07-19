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
  // Deferred (follow-up): a cold in-place login leaves the dashboard composer
  // caches (models/skills/projects/personas) warmed pre-auth as empty (401 ->
  // fail-open) until the Dashboard pane is re-focused (loadDashboard ->
  // _loadDashboardOptionsLists) or the page reloads. The CONSOLE force-re-warms
  // all four in its onLoginSuccess; the ui relies on the re-focus repaint and is
  // NOT force-warmed here (out of the composer-cache scope) — revisit if it bites.
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

  // Model + judge pickers — HIDDEN for a fork: a fork inherits its source's
  // model + judge (like skill/persona/project), and submitNewWs gates both on
  // !_forkFromWsId.  For a fresh create, paint from the warm cache with the
  // resolved-default annotation ("Default — gpt-5"), FRESH on open (the reused
  // dialog has no prior pick to carry over), then refresh-and-repaint.
  const modelLabel = document.querySelector('label[for="new-ws-model"]');
  const judgeLabel = document.querySelector('label[for="new-ws-judge-model"]');
  const modelSelect = document.getElementById("new-ws-model");
  const judgeSelect = document.getElementById("new-ws-judge-model");
  if (modelLabel) modelLabel.hidden = !!_forkFromWsId;
  if (judgeLabel) judgeLabel.hidden = !!_forkFromWsId;
  if (modelSelect) modelSelect.hidden = !!_forkFromWsId;
  if (judgeSelect) judgeSelect.hidden = !!_forkFromWsId;
  if (!_forkFromWsId) {
    _paintModelSelects(modelSelect, judgeSelect, { freshOnOpen: true });
  }

  // Skill picker — hidden for a fork (inherited + submit-gated), so skip its
  // paint too (no wasted /v1/api/skills fetch + hidden-select rebuild); a fresh
  // create paints fresh-on-open from the warm cache, then refreshes.
  const tplSelect = document.getElementById("new-ws-skill");
  if (!_forkFromWsId) {
    _paintSkillSelect(tplSelect, { freshOnOpen: true });
  }

  // Project picker — populated from the shared projects cache, refreshed on
  // open. Fresh creates SHOW it; forks HIDE it — a fork's project is its
  // source's, enforced server-side (an explicit body project_id is discarded for
  // a fork), so the frontend never sends a project for a fork.
  //
  // By design there is NO in-modal project picker for a fork: under
  // require_project a fork of a PROJECTLESS source is refused by the node with a
  // 400 and no project field (matches the setting help "forking a chat that has
  // no project is refused just like starting a fresh chat without one") — the
  // operator files the source under a project first. Do NOT "restore" a fork
  // project picker here without re-opening the cross-project re-file hole it caused.
  const projLabel = document.querySelector('label[for="new-ws-project"]');
  const projSelect = document.getElementById("new-ws-project");
  const projHint = projLabel ? projLabel.querySelector(".label-hint") : null;
  if (projLabel) projLabel.hidden = !!_forkFromWsId;
  if (projSelect) projSelect.hidden = !!_forkFromWsId;
  // Paint the picker + its required/optional hint from the warm cache, then
  // refresh-and-repaint — shared with the dashboard via _paintProjectPicker so
  // the two can't drift; skips for a fork (its picker is hidden above,
  // inheritance is server-enforced).
  _paintProjectPicker(projSelect, projHint, {
    fork: !!_forkFromWsId,
    freshOnOpen: true,
  });

  // Persona picker — hidden when forking (a fork resumes the source's
  // stamped persona; the create handler skips resolution on resume_ws).
  const personaLabel = document.querySelector('label[for="new-ws-persona"]');
  const personaSelect = document.getElementById("new-ws-persona");
  if (personaLabel) personaLabel.hidden = !!_forkFromWsId;
  if (personaSelect) personaSelect.hidden = !!_forkFromWsId;
  // Fresh-on-open (the reused dialog has no prior pick); the async repaint
  // preserves a mid-window pick and re-applies the kind default only when nothing
  // valid is selected.  Shared with the dashboard via _paintPersonaSelect.
  if (!_forkFromWsId) {
    _paintPersonaSelect(personaSelect, { freshOnOpen: true });
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
      // Under require_project a fresh picker must not be left blank after a
      // cancelled "+ New project…" — snap back to a valid project.
      _reconcileRequiredProjectSelection(sel);
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

// Fill the persona <select> from the shared personas cache (interactive kind
// — this dialog only creates interactive workstreams), preselecting the kind
// default so a zero-touch create behaves exactly like today.  No-op when the
// personas bridge is absent (module still loading / pre-seed database).
function _populatePersonaSelect(sel, opts) {
  if (!sel || !window.TurnstonePersonas) return;
  // A fresh modal open has no prior selection to preserve — render the kind
  // default; a repaint (fresh=false) keeps a mid-window pick.
  const fresh = !!(opts && opts.fresh);
  const previous = fresh ? "" : sel.value;
  const placeholder = sel.options.length ? sel.options[0] : null;
  sel.replaceChildren();
  if (placeholder) sel.appendChild(placeholder);
  const choices = window.TurnstonePersonas.personaChoices("interactive");
  choices.forEach(function (c) {
    _appendOption(sel, c.value, c.text, false);
  });
  const stillValid = choices.some(function (c) {
    return c.value === previous;
  });
  if (previous && stillValid) {
    sel.value = previous;
  } else {
    const dflt = window.TurnstonePersonas.defaultPersona("interactive");
    if (dflt) sel.value = dflt.name;
  }
}

// Add one <option> to a project <select>.
function _appendOption(sel, value, text, disabled) {
  const opt = document.createElement("option");
  opt.value = value;
  opt.textContent = text;
  if (disabled) opt.disabled = true;
  sel.appendChild(opt);
}

// True when *val* is currently an <option> value on *sel*.
function _optionExists(sel, val) {
  for (let i = 0; i < sel.options.length; i++) {
    if (sel.options[i].value === val) return true;
  }
  return false;
}

// Paint a FRESH-create project picker (+ its required/optional label hint) from
// the warm cache SYNCHRONOUSLY, then refresh-and-repaint.  Shared by the new-ws
// modal and the dashboard composer so the two paths can't drift and silently
// re-introduce the empty-dropdown / mislabel FOUC this exists to prevent.  A fork
// skips entirely (a fork inherits its source's project server-side; the modal
// hides the picker for forks).  Both paints reuse _populateProjectSelect, so the
// #867 strict-picker invariant (never auto-select a real project) holds on each.
// The sync-then-refresh tail routes through _paintFromCache (the fork skip and
// the hint stay bespoke here); returns its async-repaint promise.
function _paintProjectPicker(sel, hint, opts) {
  if ((opts && opts.fork) || !sel || !window.TurnstoneProjects)
    return Promise.resolve();
  // paint(fresh): a fresh open renders the actual default (no prior selection);
  // the async repaint MUST pass fresh=false, or it clobbers a project the user
  // picked during the refresh round-trip — under require_project that reconciles
  // the select back to "" and submit then sends no project -> a 400.
  const paint = function (fresh) {
    const strict = !!window.TurnstoneProjects.requireProject();
    if (hint) hint.textContent = strict ? "required" : "optional";
    _populateProjectSelect(sel, { requireProject: strict, fresh: fresh });
  };
  return _paintFromCache(window.TurnstoneProjects.refreshProjects, paint, opts);
}

// One chokepoint for the composer paint discipline, shared by the three
// cache-backed wrappers below and _paintProjectPicker's tail: sync-paint NOW
// from the warm cache, then refresh-and-repaint.  The sync paint mirrors the
// caller's freshOnOpen (a reused-dialog open renders the actual defaults); the
// async repaint is ALWAYS fresh:false — it must preserve a pick made during
// the fetch window, never re-blank.  Returns the async-repaint promise
// (already-resolved when there is nothing to refresh) so a caller can act
// after the repaint lands — the dashboard chains its Options-chip recompute.
//
// `refresh` may be absent (bridge missing): the sync populate no-ops and the
// refresh is skipped.  If the module graph itself failed to load (a failed
// /shared/models.js, skills.js, personas.js, projects.js, or list_cache.js
// fetch at page load), that picker stays empty until a reload — an ACCEPTED
// tradeoff of the shared-cache architecture, the same class the projects/
// personas pickers + rail have carried since #867/#868.  Do NOT add a
// per-picker retry (a cache-busted dynamic re-import creates a second cache
// instance with split-brain state) or an inline-fetch fallback (it resurrects
// the dual-path this refactor deleted); surfacing a failed bridge, if ever
// wanted, belongs in the app shell for all bridges at once.
function _paintFromCache(refresh, repaint, opts) {
  repaint(!!(opts && opts.freshOnOpen));
  if (!refresh) return Promise.resolve();
  return refresh().then(function () {
    repaint(false);
  });
}

// Paint the model + judge pickers from the warm cache then refresh-and-repaint,
// collapsing the identical sync-then-refresh dance the modal and dashboard both
// need.
function _paintModelSelects(modelSel, judgeSel, opts) {
  return _paintFromCache(
    window.TurnstoneModels && window.TurnstoneModels.refreshModels,
    function (fresh) {
      _populateModelSelect(modelSel, judgeSel, { fresh: fresh });
    },
    opts,
  );
}

// Skill twin of _paintModelSelects.
function _paintSkillSelect(sel, opts) {
  return _paintFromCache(
    window.TurnstoneSkills && window.TurnstoneSkills.refreshSkills,
    function (fresh) {
      _populateSkillSelect(sel, { fresh: fresh });
    },
    opts,
  );
}

// Persona twin of _paintModelSelects.  _populatePersonaSelect keeps the kind
// default when nothing valid is selected, so the fresh:false repaint can't
// clobber a mid-window pick.
function _paintPersonaSelect(sel, opts) {
  return _paintFromCache(
    window.TurnstonePersonas && window.TurnstonePersonas.refreshPersonas,
    function (fresh) {
      _populatePersonaSelect(sel, { fresh: fresh });
    },
    opts,
  );
}

// Fill the model + judge-model <select>s from the shared models cache.  One list
// feeds BOTH selects with the same "alias (model)" labels; each placeholder is
// annotated with the server-resolved default alias ("Default — gpt-5"), resolved
// once via the cache's modelLabel.  A fresh open renders the default (no prior
// selection); a repaint preserves each select's mid-window pick independently.
// No-op when the models bridge is absent (still loading) — the async refresh then
// fills it, exactly as before this cache existed.
function _populateModelSelect(modelSel, judgeSel, opts) {
  if (!modelSel || !window.TurnstoneModels) return;
  const fresh = !!(opts && opts.fresh);
  const M = window.TurnstoneModels;
  const choices = M.modelChoices();
  const defaults = M.modelDefaults();
  const prevModel = fresh ? "" : modelSel.value;
  const prevJudge = fresh || !judgeSel ? "" : judgeSel.value;
  const modelDefault = M.modelLabel(defaults.default_alias || "");
  const judgeDefault = M.modelLabel(defaults.judge_default_alias || "");
  modelSel.replaceChildren();
  _appendOption(
    modelSel,
    "",
    modelDefault ? "Default — " + modelDefault : "Default model",
    false,
  );
  if (judgeSel) {
    judgeSel.replaceChildren();
    _appendOption(
      judgeSel,
      "",
      judgeDefault ? "Default — " + judgeDefault : "Default (agent model)",
      false,
    );
  }
  choices.forEach(function (c) {
    _appendOption(modelSel, c.value, c.text, false);
    if (judgeSel) _appendOption(judgeSel, c.value, c.text, false);
  });
  // Preserve a mid-window pick on EACH select independently so the async repaint
  // can't clobber a fast user's choice (a fresh open zeroed both above).
  if (prevModel && _optionExists(modelSel, prevModel)) {
    modelSel.value = prevModel;
  }
  if (judgeSel && prevJudge && _optionExists(judgeSel, prevJudge)) {
    judgeSel.value = prevJudge;
  }
}

// Fill a skill <select> from the shared skills cache, keeping the static
// "Use defaults" placeholder (option 0) and preserving a mid-window pick.  The
// ui label appends " [MCP]" for MCP-origin skills (the console launcher does
// not — which is why the cache returns raw rows).  No-op when the bridge is
// absent.
function _populateSkillSelect(sel, opts) {
  if (!sel || !window.TurnstoneSkills) return;
  const fresh = !!(opts && opts.fresh);
  const previous = fresh ? "" : sel.value;
  sel.replaceChildren();
  _appendOption(sel, "", "Use defaults", false);
  window.TurnstoneSkills.getSkills().forEach(function (t) {
    let label = t.name;
    if (t.is_default) label += " (default)";
    if (t.origin === "mcp") label += " [MCP]";
    _appendOption(sel, t.name, label, false);
  });
  if (previous && _optionExists(sel, previous)) sel.value = previous;
}

// Fill a project <select> from the shared projects cache.  The picker MODE is
// passed EXPLICITLY by each caller as {requireProject}; a re-populate from the
// inline "+ New project…" creator passes nothing and reuses the mode stamped on
// the element at open time.  This helper NEVER reads the _forkFromWsId module
// global — the dashboard picker shares it.  No-op when the projects bridge is
// absent (project.read denied / still loading).  Forks don't reach this helper:
// their picker is hidden and their project is inherited server-side.
//
// Option 0 (placeholder) by mode:
//   - gate off:            "No project" (value "" -> a projectless create).
//   - gate on + projects:  "Select a project…" (value "") — the user must
//                          consciously pick; we never auto-select a project they
//                          didn't choose (it could silently file the chat under a
//                          shared one).
//   - gate on + none:      a DISABLED "No projects available" notice (value "").
function _populateProjectSelect(sel, opts) {
  if (!sel || !window.TurnstoneProjects) return;
  const mode = opts || sel._projMode || {};
  // Stamp ONLY the picker mode onto the element (the inline "+ New project…"
  // creator's no-opts repaint reuses it); `fresh` is read from the LIVE opts
  // per-call and deliberately NOT stamped, so a fresh:true modal open can't
  // persist into a later preserve-repaint.
  sel._projMode = { requireProject: !!mode.requireProject };
  const strict = !!mode.requireProject;
  const fresh = !!(opts && opts.fresh);
  _ensureStandaloneProjectCreator(sel);
  const previous = fresh ? "" : sel.value;
  const choices = window.TurnstoneProjects.projectChoices();
  sel.replaceChildren();
  if (!strict) {
    _appendOption(sel, "", "No project", false);
  } else if (choices.length === 0) {
    _appendOption(sel, "", "No projects available", true);
  } else {
    // strict + has projects: a "Select a project…" prompt (value "") so the user
    // must consciously pick — never auto-file under an unchosen (possibly shared)
    // project. Zero-touch submit sends no project and the gate returns the 400.
    _appendOption(sel, "", "Select a project…", false);
  }
  choices.forEach(function (c) {
    _appendOption(sel, c.value, c.text, false);
  });
  _appendOption(sel, _PROJECT_NEW, "+ New project…", false);
  if (previous && previous !== _PROJECT_NEW && _optionExists(sel, previous)) {
    sel.value = previous;
  }
  // Reuse the choices we already built — reconcile needs the same real-project
  // list and would otherwise recompute projectChoices() a second time.
  _reconcileRequiredProjectSelection(sel, choices);
}

// Under require_project the picker keeps a VALID selection — an already-chosen
// real project, else the "Select a project…" prompt (value "", installed by
// populate), else (no projects) the disabled "No projects available" notice. It
// must NOT auto-select a real project the user didn't choose — that silently
// files a required chat under a possibly-shared project. No-op when the gate is
// off.  Idempotent — only sets the SELECTION, never adds options.
function _reconcileRequiredProjectSelection(sel, choices) {
  if (!sel) return;
  const mode = sel._projMode || {};
  if (!mode.requireProject) return;
  // _populateProjectSelect threads in the projectChoices() list it already built;
  // the inline "+ New project…" creator's onClose caller passes none, so recompute
  // from the cache in that case.
  const real =
    choices ||
    (window.TurnstoneProjects ? window.TurnstoneProjects.projectChoices() : []);
  if (real.length === 0) {
    sel.value = ""; // the disabled "No projects available" notice
    return;
  }
  const isReal = real.some(function (c) {
    return c.value === sel.value;
  });
  // Keep a real pick; otherwise rest on the "Select a project…" prompt (value
  // ""), never auto-selecting an unchosen project.
  if (!isReal) sel.value = "";
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
  const personaEl = document.getElementById("new-ws-persona");
  const persona = personaEl ? personaEl.value : "";
  const initEl = document.getElementById("new-ws-initial-message");
  const initial_message = initEl ? initEl.value.trim() : "";
  if (name) body.name = name;
  // Forks DELIBERATELY inherit their source's model + judge: the selects are
  // hidden for a fork (showNewWsModal) and never sent here — matching the
  // skill/persona/project fork guards. A fork resumes the source session
  // (resume_ws), which already carries its model, so there is intentionally no
  // fork model/judge override. Do NOT drop these !_forkFromWsId guards without
  // also un-hiding the selects (a bare gate-removal ships a hidden-select's stale
  // value).
  if (model && !_forkFromWsId) body.model = model;
  if (judge_model && !_forkFromWsId) body.judge_model = judge_model;
  if (skill && !_forkFromWsId) body.skill = skill;
  // Only a FRESH create sends a project_id; a fork's project is its source's,
  // enforced server-side (the picker is hidden for forks, and any explicit pid is
  // discarded for a fork on the node). No _PROJECT_NEW guard: the select's change
  // handler resets the sentinel to "" before opening the creator, so it can't
  // reach submit (the dashboard quick-create path likewise doesn't filter it).
  if (project_id && !_forkFromWsId) {
    body.project_id = project_id;
  }
  // Persona likewise — a fork resumes the source's stamped persona.
  if (persona && !_forkFromWsId) body.persona = persona;
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
        // A projectless-source fork can't satisfy require_project in this modal
        // (no picker, and a pick would be discarded server-side), so the generic
        // "choose a project" text points at an impossible remedy — surface the
        // real one (file the source under a project first) instead.
        if (_forkFromWsId && data.code === "require_project") {
          _newWsError(
            "This chat isn't filed under a project, so it can't be forked while " +
              "this deployment requires one. File the source chat under a project " +
              "first, then fork it.",
          );
        } else {
          _newWsError(data.error);
        }
        return;
      }
      if (data.ws_id) {
        // Seed project_id from what we sent so the rail groups it immediately
        // (the ws_created SSE re-affirms it shortly after).
        workstreams[data.ws_id] = {
          name: data.name,
          state: "idle",
          project_id: body.project_id || null,
          persona: body.persona || "",
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
  // Same for the personas cache — the PERSONA column falls back to raw
  // slugs until display names arrive.
  const persP = window.TurnstonePersonas
    ? window.TurnstonePersonas.refreshPersonas()
    : Promise.resolve();
  Promise.all([dashP, sessP, projP, persP])
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
    SavedColumns.persona(),
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
  // The PROJECT and PERSONA columns resolve names from the shared caches,
  // which fill asynchronously — re-render once names arrive.
  if (window.TurnstoneProjects) {
    window.TurnstoneProjects.onProjectsChange(function () {
      if (_wsTable) _wsTable.render();
    });
  }
  if (window.TurnstonePersonas) {
    window.TurnstonePersonas.onPersonasChange(function () {
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

function _loadDashboardOptionsLists() {
  // Models + skills — paint from the warm cache (model placeholders annotated
  // with the server-resolved default) then refresh-and-repaint, via the shared
  // wrappers (same paths the modal uses).  The dashboard composer is persistent
  // (not a reused dialog), so freshOnOpen:false — it preserves a pick across a
  // repaint.  Previously fetch-once (guarded on options.length); now
  // refresh-on-open via the coalesced caches, matching the project/persona pickers.
  //
  // EVERY paint chains a recompute of the collapsed Options chip on its async
  // repaint: a repaint can drop a server-removed pick (the select falls back to
  // its placeholder) or revert the persona to its kind default WITHOUT firing
  // 'change' — the optionsPanel change listener covers user edits only — and
  // the chip must always name what submit will send.  The project paint is
  // chained for symmetry/future chip coverage; _refreshDashboardOptionsSummary
  // reads persona/model/judge/skill only today.
  const modelSel = document.getElementById("dashboard-model");
  const judgeSel = document.getElementById("dashboard-judge-model");
  _paintModelSelects(modelSel, judgeSel, { freshOnOpen: false }).then(
    _refreshDashboardOptionsSummary,
  );
  const skillSel = document.getElementById("dashboard-skill");
  _paintSkillSelect(skillSel, { freshOnOpen: false }).then(
    _refreshDashboardOptionsSummary,
  );

  // Project picker — paint from the warm cache then refresh-and-repaint (also
  // feeds the rail's group-by-project). Dashboard quick-create is ALWAYS a fresh
  // create (never a fork); shared with the modal via _paintProjectPicker.
  const projSel = document.getElementById("dashboard-project");
  const projLabel = document.querySelector('label[for="dashboard-project"]');
  const projHint = projLabel ? projLabel.querySelector(".label-hint") : null;
  _paintProjectPicker(projSel, projHint, { fork: false }).then(
    _refreshDashboardOptionsSummary,
  );

  // Persona picker — via the shared wrapper; the dashboard is a persistent panel
  // so freshOnOpen:false (preserve a pick across a repaint), kind default when
  // nothing valid is selected.
  const personaSel = document.getElementById("dashboard-persona");
  _paintPersonaSelect(personaSel, { freshOnOpen: false }).then(
    _refreshDashboardOptionsSummary,
  );
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
  const personaSel = document.getElementById("dashboard-persona");
  const modelSel = document.getElementById("dashboard-model");
  const judgeSel = document.getElementById("dashboard-judge-model");
  const skillSel = document.getElementById("dashboard-skill");
  // Persona surfaces only when it's a non-default pick — the kind default
  // is the zero-touch state and needs no summary line.
  if (personaSel && personaSel.value && window.TurnstonePersonas) {
    const dflt = window.TurnstonePersonas.defaultPersona("interactive");
    if (!dflt || dflt.name !== personaSel.value)
      bits.push(window.TurnstonePersonas.personaLabel(personaSel.value));
  }
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
  const personaSel = document.getElementById("dashboard-persona");
  const persona = personaSel ? personaSel.value : "";
  if (model) body.model = model;
  if (judge) body.judge_model = judge;
  if (skill) body.skill = skill;
  if (project_id) body.project_id = project_id;
  if (persona) body.persona = persona;
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
        persona: body.persona || "",
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
      workstreams[data.ws_id].persona = data.persona || "";
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

// The GLOBAL pane accelerators — new workstream, switch, and dashboard.  The
// per-pane tab-menu actions (close pane, edit/refresh title, fork, delete) are
// bound once in shell.js off the active pane's own menu, so they stay identical
// across the standalone and the console; only these roster/shell-level chords
// live here.
//
// The modifier is chosen per platform: Ctrl on macOS (the browser owns Cmd and
// leaves Ctrl free) and Alt on Windows/Linux (there Ctrl IS the browser's own
// new-tab / switch-tab accelerator and never reaches the page).
const IS_MAC =
  (navigator.platform && navigator.platform.indexOf("Mac") > -1) || false;

// The typing guard (`inEditable`) lives on TS_SHELL — shell.js is the single
// source of truth for these keyboard helpers, shared by both surfaces.
document.addEventListener("keydown", function (e) {
  // Defer while a document-modal hatch dialog is open — native dialogs own
  // their Escape, and global shortcuts must not fire under the top layer.
  if (document.querySelector("dialog:modal")) return;
  if (e.key === "Escape" && dashboardVisible) {
    e.preventDefault();
    hideDashboard();
    return;
  }

  // Ctrl+D: toggle dashboard.  Left on Ctrl for every platform — it is
  // cancelable everywhere (the Cmd+D / Ctrl+D bookmark dialog), whereas Alt+D
  // is the browser's "focus the address bar" and can't be reclaimed.  Yields to
  // text editing (macOS delete-forward) while a field is focused.
  if (e.ctrlKey && !e.altKey && !e.metaKey && !e.shiftKey && e.key === "d") {
    if (window.TS_SHELL && window.TS_SHELL.inEditable(e.target)) return;
    e.preventDefault();
    toggleDashboard();
    return;
  }

  // The pane modifier: Ctrl on macOS, Alt elsewhere.  Require it WITHOUT the
  // other primary modifier — on Windows/Linux AltGr surfaces as Ctrl+Alt, and
  // this guard keeps accented-character entry from firing Alt shortcuts.
  const paneMod = IS_MAC
    ? e.ctrlKey && !e.altKey && !e.metaKey
    : e.altKey && !e.ctrlKey && !e.metaKey;
  if (!paneMod || e.shiftKey) return;

  // <mod>+T: new workstream.  Yields to text editing (macOS transpose) while a
  // field is focused.
  if (e.key.toLowerCase() === "t") {
    if (window.TS_SHELL && window.TS_SHELL.inEditable(e.target)) return;
    e.preventDefault();
    newWorkstream();
    return;
  }
  // <mod>+1..9: switch workstreams.  No text-binding overlap, so it works even
  // while composing — mirroring a browser's own Ctrl+1..9.
  if (e.key >= "1" && e.key <= "9") {
    e.preventDefault();
    const idx = parseInt(e.key, 10) - 1;
    const wsIds = Object.keys(workstreams);
    if (idx < wsIds.length) switchTab(wsIds[idx]);
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
  // Null-prototype membership map: a ws id that happened to collide with an
  // Object.prototype property name would read as always-seen on a plain
  // object and dodge eviction.
  const seen = Object.create(null);
  (list || []).forEach(function (ws) {
    if (!ws || !ws.id) return;
    seen[ws.id] = true;
    const cur = workstreams[ws.id] || {};
    cur.name = ws.name || cur.name || ws.id.slice(0, 6);
    cur.state = ws.state || cur.state || "idle";
    cur.parent_ws_id = ws.parent_ws_id || null;
    cur.project_id = ws.project_id || null;
    // Preserve-on-ABSENT (unlike project_id's hard overwrite): roster
    // snapshots from pre-persona nodes omit the field during a rolling
    // upgrade, and persona is immutable post-create — keeping the known
    // value beats flapping labels to the slug fallback.  Key-presence,
    // not truthiness: an upgraded node sends persona:"" for unstamped
    // workstreams, and that authoritative empty must not be masked by a
    // stale in-memory value.
    cur.persona = "persona" in ws ? ws.persona || "" : cur.persona || "";
    workstreams[ws.id] = cur;
  });
  if (evict) {
    const pm = window.TS_SHELL && window.TS_SHELL.panes;
    // Stable key snapshot: mutating the roster mid-walk is well-defined for
    // the currently-visited key, but the snapshot makes the eviction loop
    // self-evidently order-safe and skips inherited keys.
    for (const id of Object.keys(workstreams)) {
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
            persona: ws.persona || "",
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
// Per-pane visibility-based DISCONNECT (close-on-hidden) lives in the
// shared Pane itself (interactive.js visibilitychange handler), which
// also reconnects its own stream with the saved Last-Event-ID on show —
// this helper only revives the GLOBAL list stream.
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
      persona: w.persona || "",
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
