const messagesEl = document.getElementById("messages");
const inputEl = document.getElementById("input");
const sendBtn = document.getElementById("send-btn");
const stopBtn = document.getElementById("stop-btn");
const statusBar = document.getElementById("status-bar");
const modelName = document.getElementById("model-name");
const tabBar = document.getElementById("tab-bar");
const newTabBtn = document.getElementById("new-tab-btn");

let currentAssistantEl = null;
let currentReasoningEl = null;
let isThinking = false;
let busy = false;
let pendingApproval = false;
let approvalBlockEl = null;

function setBusy(b) {
  busy = b;
  sendBtn.disabled = b;
  sendBtn.style.display = b ? "none" : "";
  stopBtn.style.display = b ? "" : "none";
  stopBtn.disabled = !b;
}

// --- Workstream state ---
let workstreams = {}; // ws_id -> {name, state}
let currentWsId = null;
let contentEvtSource = null;
let globalEvtSource = null;
let contentBuffer = "";
let contentRetryDelay = 1000;
let globalRetryDelay = 1000;
let dashboardVisible = false;
let _historyNavigation = false; // true while popstate is driving navigation
let _lastHealth = null;

function pollHealth() {
  authFetch("/health")
    .then(function (r) {
      return r.json();
    })
    .then(function (data) {
      pollHealth._failCount = 0;
      _lastHealth = data;
      var mcpEl = document.getElementById("mcp-status");
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
      var el = document.getElementById("health-indicator");
      if (!el) return;
      if (data.status === "degraded") {
        el.textContent = "backend down";
        el.className = "health-degraded";
        el.title =
          "Circuit: " +
          ((data.backend && data.backend.circuit_state) || "unknown");
        el.setAttribute(
          "aria-label",
          "Backend degraded. Circuit: " +
            ((data.backend && data.backend.circuit_state) || "unknown"),
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
        var el = document.getElementById("health-indicator");
        if (!el) return;
        el.textContent = "health unknown";
        el.className = "health-degraded";
        el.title = "Health endpoint unreachable";
      }
    });
}
setInterval(pollHealth, 30000);

// --- Shared hooks ---
window.onLoginSuccess = function () {
  authFetch("/v1/api/workstreams")
    .then(function (r) {
      return r.json();
    })
    .then(function (data) {
      data.workstreams.forEach(function (ws) {
        workstreams[ws.id] = { name: ws.name, state: ws.state };
      });
      var wsIds = Object.keys(workstreams);
      if (wsIds.length) {
        currentWsId = wsIds[0];
        renderTabBar();
      }
      connectGlobalSSE();
      var params = new URLSearchParams(location.search);
      var targetWs = params.get("ws_id");
      if (targetWs && workstreams[targetWs]) {
        history.replaceState(
          { turnstone: "workstream", wsId: targetWs },
          "",
          location.pathname,
        );
        _historyNavigation = true;
        try {
          switchTab(targetWs);
        } finally {
          _historyNavigation = false;
        }
      } else {
        if (currentWsId) connectContentSSE(currentWsId);
        history.replaceState({ turnstone: "dashboard" }, "", location.pathname);
        showDashboard();
      }
    });
};
window.onLogout = function () {
  if (contentEvtSource) {
    contentEvtSource.close();
    contentEvtSource = null;
  }
  if (globalEvtSource) {
    globalEvtSource.close();
    globalEvtSource = null;
  }
};

// --- Dashboard helpers ---
var STATE_DISPLAY = {
  running: { symbol: "\u25b8", label: "run" },
  thinking: { symbol: "\u25cc", label: "think" },
  attention: { symbol: "\u25c6", label: "attn" },
  idle: { symbol: "\u00b7", label: "idle" },
  error: { symbol: "\u2716", label: "err" },
};
// --- Theme hooks ---
function updateThemeMenuItem() {
  var isLight = document.documentElement.dataset.theme === "light";
  document.getElementById("theme-menu-icon").textContent = isLight
    ? "\u263E"
    : "\u2600";
  document.getElementById("theme-menu-label").textContent = isLight
    ? "Dark mode"
    : "Light mode";
  document
    .getElementById("theme-menu-item")
    .setAttribute(
      "aria-label",
      isLight
        ? "Switch to dark mode (currently light)"
        : "Switch to light mode (currently dark)",
    );
}
window.onThemeChange = function () {
  updateThemeMenuItem();
  reRenderAllMermaid();
};
updateThemeMenuItem();

// --- Hamburger menu ---
function toggleHamburger() {
  var menu = document.getElementById("hamburger-menu");
  var btn = document.getElementById("hamburger-btn");
  var open = menu.classList.toggle("open");
  btn.setAttribute("aria-expanded", open ? "true" : "false");
  if (open) {
    updateThemeMenuItem();
    // Focus first item
    var first = menu.querySelector(".hmenu-item");
    if (first) first.focus();
  }
}
function closeHamburger() {
  document.getElementById("hamburger-menu").classList.remove("open");
  document
    .getElementById("hamburger-btn")
    .setAttribute("aria-expanded", "false");
}
function hamburgerDashboard() {
  closeHamburger();
  toggleDashboard();
}
function hamburgerTheme() {
  toggleTheme();
  closeHamburger();
}
// Close on outside click
document.addEventListener("click", function (e) {
  var wrap = document.getElementById("hamburger-wrap");
  if (wrap && !wrap.contains(e.target)) closeHamburger();
});
// Keyboard nav within menu
document
  .getElementById("hamburger-menu")
  .addEventListener("keydown", function (e) {
    var items = Array.from(this.querySelectorAll(".hmenu-item"));
    var idx = items.indexOf(document.activeElement);
    if (e.key === "ArrowDown") {
      e.preventDefault();
      items[(idx + 1) % items.length].focus();
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      items[(idx - 1 + items.length) % items.length].focus();
    } else if (e.key === "Home") {
      e.preventDefault();
      items[0].focus();
    } else if (e.key === "End") {
      e.preventDefault();
      items[items.length - 1].focus();
    } else if (e.key === "Escape") {
      e.preventDefault();
      closeHamburger();
      document.getElementById("hamburger-btn").focus();
    } else if (e.key === "Tab") {
      closeHamburger();
    }
  });
// Escape when focus is on the button itself
document
  .getElementById("hamburger-btn")
  .addEventListener("keydown", function (e) {
    if (
      e.key === "Escape" &&
      document.getElementById("hamburger-menu").classList.contains("open")
    ) {
      e.preventDefault();
      closeHamburger();
    }
  });

// === Tab / Workstream management ===

function renderTabBar() {
  // Remove existing tabs (keep the + button)
  tabBar.querySelectorAll(".ws-tab").forEach(function (t) {
    t.remove();
  });

  var wsIds = Object.keys(workstreams);
  wsIds.forEach(function (wsId) {
    var ws = workstreams[wsId];
    var tab = document.createElement("div");
    tab.className = "ws-tab" + (wsId === currentWsId ? " active" : "");
    tab.dataset.wsId = wsId;
    tab.setAttribute("role", "tab");
    tab.setAttribute("tabindex", "0");
    tab.setAttribute("aria-selected", wsId === currentWsId ? "true" : "false");
    tab.onclick = function (e) {
      if (e.target.classList.contains("tab-close")) return;
      switchTab(wsId);
    };
    tab.onkeydown = function (e) {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        switchTab(wsId);
      }
    };

    var indicator = document.createElement("span");
    indicator.className = "tab-indicator";
    indicator.dataset.state = ws.state || "idle";
    indicator.setAttribute("aria-label", ws.state || "idle");
    tab.appendChild(indicator);

    var name = document.createElement("span");
    name.className = "tab-name";
    name.textContent = ws.name || wsId.substring(0, 6);
    tab.appendChild(name);

    // Close button (only if more than one tab)
    if (wsIds.length > 1) {
      var close = document.createElement("button");
      close.className = "tab-close";
      close.innerHTML = "&times;";
      close.title = "Close workstream";
      close.onclick = function (e) {
        e.stopPropagation();
        closeWorkstream(wsId);
      };
      tab.appendChild(close);
    }

    tabBar.insertBefore(tab, newTabBtn);
  });
}

function updateTabIndicator(wsId, state, extra) {
  workstreams[wsId] = workstreams[wsId] || {};
  workstreams[wsId].state = state;
  // Update tab bar indicator
  var tab = tabBar.querySelector('.ws-tab[data-ws-id="' + wsId + '"]');
  if (tab) {
    var ind = tab.querySelector(".tab-indicator");
    if (ind) ind.dataset.state = state;
  }
  // Update dashboard table row if dashboard is open
  var row = document.querySelector(
    '#dash-ws-table .dash-row[data-ws-id="' + wsId + '"]',
  );
  if (row) {
    var sd = STATE_DISPLAY[state] || STATE_DISPLAY.idle;
    row.dataset.state = state;
    var dot = row.querySelector(".dash-state-dot");
    if (dot) dot.dataset.state = state;
    var label = row.querySelector(".dash-state-label");
    if (label) {
      label.dataset.state = state;
      label.textContent = sd.symbol + " " + sd.label;
    }
    if (extra) {
      if (extra.tokens !== undefined) {
        var tokEl = row.querySelector(".dash-cell-tokens");
        if (tokEl) tokEl.textContent = formatTokens(extra.tokens);
      }
      if (extra.context_ratio !== undefined) {
        var ctxEl = row.querySelector(".dash-cell-ctx");
        if (ctxEl) {
          ctxEl.className = "dash-cell-ctx " + ctxClass(extra.context_ratio);
          ctxEl.textContent =
            extra.context_ratio > 0
              ? Math.round(extra.context_ratio * 100) + "%"
              : "";
        }
      }
      if (extra.activity !== undefined) {
        var sub = row.querySelector(".dash-row-sub");
        if (sub) {
          sub.textContent = extra.activity || "";
          if (extra.activity_state === "approval")
            sub.classList.add("sub-attention");
          else sub.classList.remove("sub-attention");
        }
      }
    }
  }
}

function switchTab(wsId) {
  if (wsId === currentWsId && !dashboardVisible) return;

  // Reset current tab state
  currentAssistantEl = null;
  currentReasoningEl = null;
  contentBuffer = "";
  setBusy(false);
  pendingApproval = false;
  approvalBlockEl = null;
  inputEl.disabled = false;

  currentWsId = wsId;
  messagesEl.innerHTML = "";
  showEmptyState();
  renderTabBar();
  connectContentSSE(wsId);

  // Push history entry so back button can retrace tab navigation.
  if (!_historyNavigation) {
    history.pushState({ turnstone: "workstream", wsId: wsId }, "");
  }
}

// ---------------------------------------------------------------------------
// New workstream modal
// ---------------------------------------------------------------------------
var _newWsTrapHandler = null;

function newWorkstream() {
  showNewWsModal();
}

function showNewWsModal() {
  var overlay = document.getElementById("new-ws-overlay");
  overlay.style.display = "flex";
  document.body.style.overflow = "hidden";

  // Backdrop click to dismiss
  overlay.onclick = function (e) {
    if (e.target === overlay) hideNewWsModal();
  };

  // Populate model placeholder from header
  var curModel = document.getElementById("model-name").textContent;
  var modelInput = document.getElementById("new-ws-model");
  modelInput.placeholder = curModel || "Default model";
  modelInput.value = "";

  // Populate skill dropdown
  var tplSelect = document.getElementById("new-ws-skill");
  tplSelect.innerHTML = '<option value="">Use defaults</option>';
  authFetch("/v1/api/skills")
    .then(function (r) {
      return r.json();
    })
    .then(function (data) {
      (data.skills || []).forEach(function (t) {
        var opt = document.createElement("option");
        opt.value = t.name;
        var label = t.name;
        if (t.is_default) label += " (default)";
        if (t.origin === "mcp") label += " [MCP]";
        opt.textContent = label;
        tplSelect.appendChild(opt);
      });
    })
    .catch(function () {
      /* ignore — defaults still work */
    });

  // Reset form
  document.getElementById("new-ws-name").value = "";
  var errEl = document.getElementById("new-ws-error");
  errEl.style.display = "none";
  errEl.textContent = "";
  var submitBtn = document.getElementById("new-ws-submit");
  submitBtn.disabled = false;
  submitBtn.textContent = "Create";

  // Wire buttons
  document.getElementById("new-ws-cancel").onclick = hideNewWsModal;
  submitBtn.onclick = submitNewWs;

  // Focus trap
  _newWsTrapHandler = function (e) {
    if (e.key === "Escape") {
      e.preventDefault();
      hideNewWsModal();
      return;
    }
    if (
      e.key === "Enter" &&
      e.target.tagName !== "TEXTAREA" &&
      e.target.tagName !== "SELECT"
    ) {
      e.preventDefault();
      submitNewWs();
      return;
    }
    if (e.key !== "Tab") return;
    var box = document.getElementById("new-ws-box");
    var focusable = box.querySelectorAll(
      'input, select, button, [tabindex]:not([tabindex="-1"])',
    );
    if (!focusable.length) return;
    var first = focusable[0],
      last = focusable[focusable.length - 1];
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
  };
  document.addEventListener("keydown", _newWsTrapHandler);
  setTimeout(function () {
    document.getElementById("new-ws-name").focus();
  }, 50);
}

function hideNewWsModal() {
  document.getElementById("new-ws-overlay").style.display = "none";
  document.body.style.overflow = "";
  if (_newWsTrapHandler) {
    document.removeEventListener("keydown", _newWsTrapHandler);
    _newWsTrapHandler = null;
  }
  document.getElementById("new-tab-btn").focus();
}

function submitNewWs() {
  var submitBtn = document.getElementById("new-ws-submit");
  if (submitBtn.disabled) return;
  submitBtn.disabled = true;
  submitBtn.textContent = "Creating\u2026";

  var body = {};
  var name = document.getElementById("new-ws-name").value.trim();
  var model = document.getElementById("new-ws-model").value.trim();
  var skill = document.getElementById("new-ws-skill").value;
  if (name) body.name = name;
  if (model) body.model = model;
  if (skill) body.skill = skill;

  var errEl = document.getElementById("new-ws-error");
  errEl.style.display = "none";

  authFetch("/v1/api/workstreams/new", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  })
    .then(function (r) {
      return r.json();
    })
    .then(function (data) {
      if (data.error) {
        errEl.textContent = data.error;
        errEl.style.display = "block";
        submitBtn.disabled = false;
        submitBtn.textContent = "Create";
        return;
      }
      if (data.ws_id) {
        workstreams[data.ws_id] = { name: data.name, state: "idle" };
        hideNewWsModal();
        switchTab(data.ws_id);
      }
    })
    .catch(function () {
      errEl.textContent = "Failed to create workstream";
      errEl.style.display = "block";
      submitBtn.disabled = false;
      submitBtn.textContent = "Create";
    });
}

function closeWorkstream(wsId) {
  authFetch("/v1/api/workstreams/close", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ws_id: wsId }),
  })
    .then(function (r) {
      return r.json();
    })
    .then(function (data) {
      if (data.status === "ok") {
        delete workstreams[wsId];
        if (wsId === currentWsId) {
          var remaining = Object.keys(workstreams);
          if (remaining.length) switchTab(remaining[0]);
        } else {
          renderTabBar();
        }
      }
    });
}

// === SSE connections ===

function connectContentSSE(wsId) {
  if (contentEvtSource) {
    contentEvtSource.close();
    contentEvtSource = null;
  }
  contentEvtSource = new EventSource(
    "/v1/api/events?ws_id=" + encodeURIComponent(wsId),
  );
  contentEvtSource.onopen = function () {
    contentRetryDelay = 1000;
    statusBar.classList.remove("disconnected");
    statusBar.textContent = "";
  };
  contentEvtSource.onmessage = function (e) {
    var data = JSON.parse(e.data);
    handleEvent(data);
  };
  contentEvtSource.onerror = function () {
    contentEvtSource.close();
    contentEvtSource = null;
    var loginOverlay = document.getElementById("login-overlay");
    if (loginOverlay && loginOverlay.style.display !== "none") return;
    statusBar.textContent = "Reconnecting\u2026";
    statusBar.classList.add("disconnected");
    // Raw fetch (not authFetch) — need to inspect status before throwing
    fetch("/v1/api/workstreams")
      .then(function (r) {
        if (r.status === 401) {
          showLogin();
          return;
        }
        return r.json().then(function (data) {
          // Replace workstream list — IDs may have changed after restart
          var freshIds = {};
          workstreams = {};
          (data.workstreams || []).forEach(function (ws) {
            workstreams[ws.id] = { name: ws.name, state: ws.state };
            freshIds[ws.id] = true;
          });
          // Always re-render tabs since workstreams map was replaced
          renderTabBar();
          // If current ws_id is stale, switch to first available
          if (currentWsId && !freshIds[currentWsId]) {
            var ids = Object.keys(freshIds);
            if (ids.length) {
              switchTab(ids[0]);
            } else {
              showDashboard();
            }
            return; // switchTab/showDashboard handles SSE connection
          }
          setTimeout(function () {
            connectContentSSE(currentWsId);
          }, contentRetryDelay);
          contentRetryDelay = Math.min(contentRetryDelay * 2, 30000);
        });
      })
      .catch(function () {
        setTimeout(function () {
          connectContentSSE(currentWsId);
        }, contentRetryDelay);
        contentRetryDelay = Math.min(contentRetryDelay * 2, 30000);
      });
  };
}

function showEmptyState() {
  if (!messagesEl.querySelector(".empty-state")) {
    var el = document.createElement("div");
    el.className = "empty-state";
    el.textContent = "Type a message to start";
    messagesEl.appendChild(el);
  }
}
function removeEmptyState() {
  var el = messagesEl.querySelector(".empty-state");
  if (el) el.remove();
}

// --- Dashboard ---
function showDashboard() {
  dashboardVisible = true;
  closeHamburger();
  document.getElementById("dashboard").classList.add("active");
  document.getElementById("header").inert = true;
  document.getElementById("tab-bar").inert = true;
  document.getElementById("messages").inert = true;
  document.getElementById("input-area").inert = true;
  loadDashboard();
  setTimeout(function () {
    document.getElementById("dashboard-input").focus();
  }, 50);
}
function hideDashboard() {
  dashboardVisible = false;
  document.getElementById("dashboard").classList.remove("active");
  document.getElementById("header").inert = false;
  document.getElementById("tab-bar").inert = false;
  document.getElementById("messages").inert = false;
  document.getElementById("input-area").inert = false;
  document.getElementById("dashboard-input").value = "";
  inputEl.focus();
}
function toggleDashboard() {
  if (dashboardVisible) hideDashboard();
  else showDashboard();
}
function loadDashboard() {
  var tableEl = document.getElementById("dash-ws-table");
  tableEl.innerHTML = '<div class="dashboard-empty">Loading\u2026</div>';
  document.getElementById("dashboard-saved-cards").innerHTML =
    '<div class="dashboard-empty">Loading\u2026</div>';
  var dashP = authFetch("/v1/api/dashboard").then(function (r) {
    return r.json();
  });
  var sessP = authFetch("/v1/api/workstreams/saved").then(function (r) {
    return r.json();
  });
  Promise.all([dashP, sessP])
    .then(function (res) {
      var dashData = res[0];
      var wsList = dashData.workstreams || [];
      var agg = dashData.aggregate || {};
      renderDashboardTable(wsList, agg);
      // Collect active ws IDs for dedup
      var activeWsIds = {};
      wsList.forEach(function (ws) {
        activeWsIds[ws.id] = true;
      });
      var savedList = (res[1].workstreams || []).filter(function (s) {
        return !activeWsIds[s.ws_id];
      });
      renderSavedWorkstreams(savedList);
    })
    .catch(function () {
      tableEl.innerHTML = '<div class="dashboard-empty">Failed to load</div>';
      document.getElementById("dashboard-saved-cards").innerHTML =
        '<div class="dashboard-empty">Failed to load</div>';
    });
}
function renderDashboardTable(wsList, agg) {
  // Update header summary
  var activeCount = wsList.filter(function (w) {
    return w.state !== "idle";
  }).length;
  document.getElementById("dash-summary").textContent =
    activeCount + " active \u00b7 " + wsList.length + " total";
  // Render rows
  var table = document.getElementById("dash-ws-table");
  table.innerHTML = "";
  if (!wsList.length) {
    table.innerHTML =
      '<div class="dashboard-empty">No active workstreams</div>';
    updateDashFooter(agg);
    return;
  }
  wsList.forEach(function (ws) {
    var liveState =
      (workstreams[ws.id] && workstreams[ws.id].state) || ws.state || "idle";
    var liveName =
      (workstreams[ws.id] && workstreams[ws.id].name) || ws.name || ws.id;
    var sd = STATE_DISPLAY[liveState] || STATE_DISPLAY.idle;

    var row = document.createElement("div");
    row.className = "dash-row";
    row.dataset.wsId = ws.id;
    row.dataset.state = liveState;
    row.setAttribute("role", "button");
    row.setAttribute("tabindex", "0");
    var ariaLabel = liveName + " \u2014 " + sd.label;
    if (ws.model_alias || ws.model)
      ariaLabel += ", model: " + (ws.model_alias || ws.model);
    if (ws.title) ariaLabel += ", task: " + ws.title;
    if (ws.tokens) ariaLabel += ", " + formatTokens(ws.tokens) + " tokens";
    if (ws.context_ratio > 0)
      ariaLabel += ", " + Math.round(ws.context_ratio * 100) + "% context";
    row.setAttribute("aria-label", ariaLabel);

    // Main line
    var main = document.createElement("div");
    main.className = "dash-row-main";

    // STATE cell
    var stateCell = document.createElement("span");
    stateCell.className = "dash-cell-state";
    stateCell.innerHTML =
      '<span class="dash-state-dot" data-state="' +
      escapeHtml(liveState) +
      '" aria-hidden="true"></span>' +
      '<span class="dash-state-label" data-state="' +
      escapeHtml(liveState) +
      '">' +
      sd.symbol +
      " " +
      sd.label +
      "</span>";
    main.appendChild(stateCell);

    // NAME cell
    var nameCell = document.createElement("span");
    nameCell.className = "dash-cell-name";
    nameCell.textContent = liveName;
    main.appendChild(nameCell);

    // MODEL cell
    var modelCell = document.createElement("span");
    modelCell.className = "dash-cell-model";
    modelCell.textContent = ws.model_alias || ws.model || "";
    if (ws.model) modelCell.title = ws.model;
    main.appendChild(modelCell);

    // NODE cell
    var nodeCell = document.createElement("span");
    nodeCell.className = "dash-cell-node";
    nodeCell.textContent = ws.node || "local";
    if (ws.node) nodeCell.title = ws.node;
    main.appendChild(nodeCell);

    // TASK cell
    var taskCell = document.createElement("span");
    taskCell.className = "dash-cell-task";
    taskCell.textContent = ws.title || "";
    main.appendChild(taskCell);

    // TOKENS cell
    var tokensCell = document.createElement("span");
    tokensCell.className = "dash-cell-tokens";
    tokensCell.textContent = ws.tokens ? formatTokens(ws.tokens) : "";
    main.appendChild(tokensCell);

    // CTX cell
    var ctxCell = document.createElement("span");
    ctxCell.className = "dash-cell-ctx " + ctxClass(ws.context_ratio);
    ctxCell.textContent =
      ws.context_ratio > 0 ? Math.round(ws.context_ratio * 100) + "%" : "";
    main.appendChild(ctxCell);

    row.appendChild(main);

    // Sub-line (activity)
    var sub = document.createElement("div");
    sub.className = "dash-row-sub";
    if (ws.activity_state === "approval") sub.classList.add("sub-attention");
    sub.textContent = ws.activity || "";
    row.appendChild(sub);

    // Click handler
    row.onclick = function () {
      dashboardSwitchWorkstream(ws.id);
    };
    row.onkeydown = function (e) {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        dashboardSwitchWorkstream(ws.id);
      }
    };

    table.appendChild(row);
  });
  updateDashFooter(agg);
  // Arrow key navigation between rows
  table.onkeydown = function (e) {
    if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
    e.preventDefault();
    var rows = Array.from(table.querySelectorAll(".dash-row"));
    var idx = rows.indexOf(document.activeElement);
    if (idx === -1) return;
    if (e.key === "ArrowDown" && idx < rows.length - 1) rows[idx + 1].focus();
    if (e.key === "ArrowUp" && idx > 0) rows[idx - 1].focus();
  };
}
function updateDashFooter(agg) {
  if (!agg) return;
  var nodesEl = document.getElementById("dash-footer-nodes");
  var statsEl = document.getElementById("dash-footer-stats");
  nodesEl.innerHTML =
    '<span class="dash-footer-node-dot"></span> ' +
    escapeHtml((agg.node || "local") + " (" + (agg.total_count || 0) + " ws)");
  var parts = [];
  if (agg.total_tokens) parts.push(formatTokens(agg.total_tokens) + " tokens");
  if (agg.total_tool_calls) parts.push(agg.total_tool_calls + " tool calls");
  if (agg.uptime_seconds)
    parts.push(formatUptime(agg.uptime_seconds) + " uptime");
  statsEl.textContent = parts.join(" \u00b7 ");
  if (_lastHealth && _lastHealth.status === "degraded") {
    statsEl.textContent +=
      " \u00b7 backend down (circuit " +
      (_lastHealth.backend && _lastHealth.backend.circuit_state
        ? _lastHealth.backend.circuit_state
        : "unknown") +
      ")";
  }
}
function renderSavedWorkstreams(items) {
  var c = document.getElementById("dashboard-saved-cards");
  c.innerHTML = "";
  if (!items.length) {
    c.innerHTML = '<div class="dashboard-empty">No saved workstreams</div>';
    return;
  }
  items.forEach(function (sess) {
    var card = document.createElement("div");
    card.className = "dashboard-card";
    card.setAttribute("role", "button");
    card.setAttribute("tabindex", "0");
    var label = sess.alias || sess.title || sess.ws_id;
    card.setAttribute("aria-label", "Resume: " + label);
    card.onclick = function () {
      dashboardResumeSession(sess.ws_id);
    };
    card.onkeydown = function (e) {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        dashboardResumeSession(sess.ws_id);
      }
    };
    var title = sess.alias || sess.title || sess.ws_id.substring(0, 12);
    var meta = sess.message_count + " msgs";
    if (sess.updated) meta += " \u00b7 " + formatRelativeTime(sess.updated);
    card.innerHTML =
      '<div class="card-title">' +
      escapeHtml(title) +
      "</div>" +
      '<div class="card-meta">' +
      escapeHtml(meta) +
      "</div>";
    c.appendChild(card);
  });
}
function formatRelativeTime(iso) {
  if (!iso) return "";
  // SQLite datetime('now') produces "YYYY-MM-DD HH:MM:SS" (UTC, no timezone marker).
  // Without a Z suffix JS parses it as local time, breaking relative time for western offsets.
  var s = iso.replace(" ", "T");
  if (!s.endsWith("Z") && !s.includes("+")) s += "Z";
  var d = new Date(s);
  if (isNaN(d)) return "";
  var now = new Date();
  var ms = now - d;
  var min = Math.floor(ms / 60000);
  if (min < 1) return "just now";
  if (min < 60) return min + "m ago";
  var hr = Math.floor(min / 60);
  if (hr < 24) return hr + "h ago";
  var day = Math.floor(hr / 24);
  if (day < 30) return day + "d ago";
  return d.toLocaleDateString();
}
function dashboardSwitchWorkstream(wsId) {
  if (workstreams[wsId]) {
    hideDashboard();
    switchTab(wsId);
  } else loadDashboard();
}
function dashboardResumeSession(wsId) {
  authFetch("/v1/api/workstreams/new", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ resume_ws: wsId }),
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
      // Resume handled atomically by server — history arrives via SSE.
    })
    .catch(function (err) {
      showToast("Failed to resume workstream", "error");
    });
}
function dashboardNewChat() {
  hideDashboard();
  newWorkstream();
}
function dashboardSendMessage() {
  var input = document.getElementById("dashboard-input");
  var text = input.value.trim();
  if (!text) return;
  input.disabled = true;
  var btn = document.querySelector(".dashboard-new-btn");
  btn.disabled = true;
  authFetch("/v1/api/workstreams/new", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: "{}",
  })
    .then(function (r) {
      return r.json();
    })
    .then(function (data) {
      if (!data.ws_id) {
        input.disabled = false;
        btn.disabled = false;
        return;
      }
      workstreams[data.ws_id] = { name: data.name, state: "idle" };
      switchTab(data.ws_id);
      hideDashboard();
      input.disabled = false;
      btn.disabled = false;
      setBusy(true);
      addUserMessage(text);
      authFetch("/v1/api/send", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: text, ws_id: data.ws_id }),
      }).catch(function (err) {
        addErrorMessage("Connection error: " + err.message);
        setBusy(false);
      });
    })
    .catch(function () {
      input.disabled = false;
      btn.disabled = false;
    });
}

function connectGlobalSSE() {
  if (globalEvtSource) {
    globalEvtSource.close();
    globalEvtSource = null;
  }
  globalEvtSource = new EventSource("/v1/api/events/global");
  globalEvtSource.onopen = function () {
    globalRetryDelay = 1000;
  };
  globalEvtSource.onmessage = function (e) {
    var data = JSON.parse(e.data);
    if (data.type === "ws_state") {
      updateTabIndicator(data.ws_id, data.state, {
        tokens: data.tokens,
        context_ratio: data.context_ratio,
        activity: data.activity,
        activity_state: data.activity_state,
      });
    } else if (data.type === "ws_activity") {
      // Live-update dashboard row sub-line
      var row = document.querySelector(
        '#dash-ws-table .dash-row[data-ws-id="' + data.ws_id + '"]',
      );
      if (row) {
        var sub = row.querySelector(".dash-row-sub");
        if (sub) {
          sub.textContent = data.activity || "";
          if (data.activity_state === "approval")
            sub.classList.add("sub-attention");
          else sub.classList.remove("sub-attention");
        }
      }
    } else if (data.type === "ws_rename") {
      if (workstreams[data.ws_id]) workstreams[data.ws_id].name = data.name;
      var nameEl = document.querySelector(
        '[data-ws-id="' + data.ws_id + '"] .tab-name',
      );
      if (nameEl) nameEl.textContent = data.name;
    } else if (data.type === "ws_closed") {
      var wsId = data.ws_id;
      delete workstreams[wsId];
      renderTabBar();
      if (data.reason === "evicted") {
        showToast(
          "Evicted" + (data.name ? ": " + data.name : "") + " (capacity)",
        );
      }
      if (wsId === currentWsId) {
        var remaining = Object.keys(workstreams);
        if (remaining.length) switchTab(remaining[0]);
        else showDashboard();
      }
    }
  };
  globalEvtSource.onerror = function () {
    globalEvtSource.close();
    globalEvtSource = null;
    // Raw fetch (not authFetch) — need to inspect status before throwing
    fetch("/v1/api/workstreams")
      .then(function (r) {
        if (r.status === 401) {
          showLogin();
          return;
        }
        setTimeout(connectGlobalSSE, globalRetryDelay);
        globalRetryDelay = Math.min(globalRetryDelay * 2, 30000);
      })
      .catch(function () {
        setTimeout(connectGlobalSSE, globalRetryDelay);
        globalRetryDelay = Math.min(globalRetryDelay * 2, 30000);
      });
  };
}

function handleEvent(evt) {
  switch (evt.type) {
    case "thinking_start":
      isThinking = true;
      setBusy(true);
      removeEmptyState();
      addThinkingIndicator();
      break;

    case "thinking_stop":
      isThinking = false;
      removeThinkingIndicator();
      break;

    case "reasoning":
      removeThinkingIndicator();
      if (!currentReasoningEl) {
        currentReasoningEl = document.createElement("div");
        currentReasoningEl.className = "msg msg-assistant reasoning";
        messagesEl.appendChild(currentReasoningEl);
      }
      currentReasoningEl.textContent += evt.text;
      scrollToBottom();
      break;

    case "content":
      removeThinkingIndicator();
      if (currentReasoningEl) {
        currentReasoningEl = null;
      }
      if (!currentAssistantEl) {
        currentAssistantEl = document.createElement("div");
        currentAssistantEl.className = "msg msg-assistant";
        messagesEl.appendChild(currentAssistantEl);
      }
      contentBuffer += evt.text;
      currentAssistantEl.innerHTML = renderMarkdown(contentBuffer);
      scrollToBottom();
      break;

    case "stream_end":
      if (currentAssistantEl && contentBuffer) {
        currentAssistantEl.innerHTML = renderMarkdown(contentBuffer);
        postRenderMarkdown(currentAssistantEl);
      }
      currentAssistantEl = null;
      currentReasoningEl = null;
      contentBuffer = "";
      setBusy(false);
      inputEl.focus();
      scrollToBottom(true);
      break;

    case "tool_info":
      showInlineToolBlock(evt.items, true);
      break;

    case "approve_request":
      showInlineToolBlock(evt.items, false, evt.judge_pending);
      break;

    case "intent_verdict":
      updateVerdictBadge(evt);
      break;

    case "output_warning":
      showOutputWarning(evt);
      break;

    case "approval_resolved":
      resolveInlineApproval(evt.approved, false, evt.feedback, true);
      break;

    case "tool_output_chunk":
      appendToolOutputChunk(evt.call_id || "", evt.chunk);
      break;

    case "tool_result":
      appendToolOutput(evt.call_id || "", evt.name, evt.output);
      break;

    case "status":
      updateStatus(evt);
      break;

    case "plan_review":
      showPlanDialog(evt.content);
      break;

    case "info":
      addInfoMessage(evt.message);
      break;

    case "error":
      addErrorMessage(evt.message);
      setBusy(false);
      break;

    case "busy_error":
      addErrorMessage(evt.message);
      setBusy(false);
      break;

    case "cancelled":
      currentAssistantEl = null;
      currentReasoningEl = null;
      contentBuffer = "";
      setBusy(false);
      inputEl.focus();
      scrollToBottom(true);
      break;

    case "connected":
      modelName.textContent = evt.model_alias || evt.model || "";
      modelName.title = evt.model || "";
      if (evt.skip_permissions) {
        var existing = document.querySelector(".skip-permissions-warning");
        if (!existing) {
          var warn = document.createElement("div");
          warn.className = "skip-permissions-warning";
          warn.textContent =
            "\u26a0 Running with --skip-permissions: all tool calls are auto-approved";
          document.getElementById("header").appendChild(warn);
        }
      }
      break;

    case "history":
      replayHistory(evt.messages);
      break;

    case "clear_ui":
      messagesEl.innerHTML = "";
      break;
  }
}

function addThinkingIndicator() {
  if (document.getElementById("thinking")) return;
  const el = document.createElement("div");
  el.id = "thinking";
  el.className = "thinking-indicator";
  el.textContent = "Thinking";
  messagesEl.appendChild(el);
  scrollToBottom();
}

function removeThinkingIndicator() {
  const el = document.getElementById("thinking");
  if (el) el.remove();
}

function addUserMessage(text) {
  removeEmptyState();
  const el = document.createElement("div");
  el.className = "msg msg-user";
  el.textContent = text;
  messagesEl.appendChild(el);
  scrollToBottom(true);
}

// --- History replay ---

function replayHistory(messages) {
  messagesEl.innerHTML = "";
  if (!messages.length) {
    showEmptyState();
    return;
  }
  var lastToolBlock = null;
  for (var i = 0; i < messages.length; i++) {
    var msg = messages[i];
    if (msg.role === "user") {
      addUserMessage(msg.content || "");
      lastToolBlock = null;
    } else if (msg.role === "assistant") {
      if (msg.tool_calls && msg.tool_calls.length) {
        if (msg.pending) {
          // Approval still outstanding — skip the approved block here.
          // The server re-sends approve_request right after history, which
          // will create the live approval UI.
          lastToolBlock = null;
        } else {
          var wasDenied = !!msg.denied;
          var block = document.createElement("div");
          block.className =
            "msg approval-block " + (wasDenied ? "denied" : "approved");
          msg.tool_calls.forEach(function (tc) {
            var div = document.createElement("div");
            div.className = "approval-tool";
            div.dataset.funcName = tc.name;
            div.dataset.callId = tc.id || "";
            var nameEl = document.createElement("div");
            nameEl.className = "tool-name";
            nameEl.textContent = tc.name;
            div.appendChild(nameEl);
            var cmd = document.createElement("div");
            cmd.className = "tool-cmd";
            try {
              var args = JSON.parse(tc.arguments);
              var preview = Object.values(args)[0] || "";
              if (tc.name === "bash") {
                cmd.innerHTML =
                  '<span class="dollar">$ </span>' +
                  escapeHtml(String(preview));
              } else {
                cmd.textContent = String(preview).substring(0, 200);
              }
            } catch (e) {
              cmd.textContent = tc.arguments.substring(0, 100);
            }
            div.appendChild(cmd);
            block.appendChild(div);
          });
          var badge = document.createElement("div");
          badge.setAttribute("role", "status");
          if (wasDenied) {
            badge.className = "approval-badge badge-denied";
            badge.textContent = "\u2717 denied";
          } else {
            badge.className = "approval-badge badge-approved";
            badge.textContent = "\u2713 approved";
          }
          block.appendChild(badge);
          messagesEl.appendChild(block);
          lastToolBlock = block;
        }
      }
      if (msg.content) {
        var el = document.createElement("div");
        el.className = "msg msg-assistant";
        el.innerHTML = renderMarkdown(msg.content);
        postRenderMarkdown(el);
        messagesEl.appendChild(el);
        lastToolBlock = null;
      }
    } else if (msg.role === "tool") {
      if (lastToolBlock) {
        var stripped = stripAnsi(msg.content || "").trim();
        // Skip displaying denied/blocked messages as tool output
        var isDenied =
          msg.denied ||
          /^Denied by user/.test(stripped) ||
          /^Blocked/.test(stripped);
        if (stripped && !isDenied) {
          var out = document.createElement("div");
          out.className = "tool-output";
          out.textContent = stripped;
          if (stripped.split("\n").length > 10) {
            makeCollapsible(out);
          }
          var bdg = lastToolBlock.querySelector(".approval-badge");
          if (bdg) lastToolBlock.insertBefore(out, bdg);
          else lastToolBlock.appendChild(out);
        }
      }
    }
  }
  scrollToBottom();
}

function makeCollapsible(el) {
  el.classList.add("collapsed");
  el.setAttribute("tabindex", "0");
  el.setAttribute("role", "button");
  el.setAttribute("aria-label", "Tool output (collapsed). Activate to expand.");
  var handler = function () {
    this.classList.remove("collapsed");
    this.removeAttribute("tabindex");
    this.removeAttribute("role");
    this.removeAttribute("aria-label");
  };
  el.addEventListener("click", handler);
  el.addEventListener("keydown", function (e) {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      handler.call(this);
    }
  });
}

// --- Inline tool/approval blocks ---

function stripAnsi(s) {
  // Strip CSI sequences, OSC sequences, and two-byte escapes
  return s.replace(
    /\x1b(?:\[[0-9;?]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)?|[()#][A-Za-z0-9]|.)/g,
    "",
  );
}

function buildToolDiv(item) {
  const div = document.createElement("div");
  div.className = "approval-tool";
  div.dataset.funcName = item.func_name || "";
  div.dataset.callId = item.call_id || "";

  const name = document.createElement("div");
  name.className = "tool-name";
  name.textContent = item.func_name || "";
  if (item.error) name.style.color = "var(--red)";
  div.appendChild(name);

  // Command/header preview
  const cmd = document.createElement("div");
  cmd.className = "tool-cmd";
  const headerText = stripAnsi(item.header || "");
  // Strip the leading icon + tool name prefix to show just the command
  const cleaned = headerText.replace(/^[^\s]+\s+\w+:\s*/, "");
  if (item.func_name === "bash" && cleaned) {
    cmd.innerHTML = '<span class="dollar">$ </span>' + escapeHtml(cleaned);
  } else {
    cmd.textContent = cleaned || headerText;
  }
  div.appendChild(cmd);

  // Diff preview for edit_file / write_file
  if (item.preview) {
    const diff = document.createElement("div");
    diff.className = "tool-diff";
    const lines = stripAnsi(item.preview).split("\n");
    diff.innerHTML = lines
      .map(function (line) {
        const trimmed = line.trim();
        if (trimmed.startsWith("-"))
          return '<span class="diff-del">' + escapeHtml(line) + "</span>";
        if (trimmed.startsWith("+"))
          return '<span class="diff-add">' + escapeHtml(line) + "</span>";
        if (trimmed.startsWith("Warning:"))
          return '<span class="diff-warn">' + escapeHtml(line) + "</span>";
        return escapeHtml(line);
      })
      .join("\n");
    div.appendChild(diff);
  }

  return div;
}

function getFeedback() {
  if (!approvalBlockEl) return null;
  var inp = approvalBlockEl.querySelector(".approval-feedback-input");
  return inp && inp.value.trim() ? inp.value.trim() : null;
}

// --- Verdict badge helpers ---

function renderVerdictBadge(verdict, judgePending) {
  if (!verdict) return "";
  var risk = verdict.risk_level || "medium";
  var rec = verdict.recommendation || "review";
  var conf = Math.round((verdict.confidence || 0) * 100);
  var summary = verdict.intent_summary || "";
  var spinnerHtml = "";
  if (judgePending) {
    spinnerHtml =
      '<span class="verdict-judge-spinner">' +
      '<span class="judge-spinner-dot"></span> judge analyzing\u2026</span>';
  }
  var callId = escapeHtml(verdict.call_id || "");
  return (
    '<div class="verdict-badge verdict-' +
    escapeHtml(risk) +
    '" data-call-id="' +
    callId +
    '">' +
    '<span class="verdict-risk">' +
    escapeHtml(risk.toUpperCase()) +
    "</span>" +
    '<span class="verdict-rec">' +
    escapeHtml(rec) +
    "</span>" +
    '<span class="verdict-conf">' +
    conf +
    "%</span>" +
    spinnerHtml +
    '<button class="verdict-expand" onclick="toggleVerdictDetail(this)">details</button>' +
    "</div>" +
    '<div class="verdict-detail" style="display:none">' +
    '<div class="verdict-summary">' +
    escapeHtml(summary) +
    "</div>" +
    '<div class="verdict-reasoning">' +
    escapeHtml(verdict.reasoning || "") +
    "</div>" +
    ((verdict.evidence || []).length
      ? '<div class="verdict-evidence">' +
        (verdict.evidence || [])
          .map(function (e) {
            return "<div>\u2022 " + escapeHtml(e) + "</div>";
          })
          .join("") +
        "</div>"
      : "") +
    '<div class="verdict-tier">' +
    escapeHtml(verdict.tier || "heuristic") +
    " tier" +
    (verdict.judge_model ? " | " + escapeHtml(verdict.judge_model) : "") +
    "</div>" +
    "</div>"
  );
}

function toggleVerdictDetail(btn) {
  var badge = btn.closest(".verdict-badge");
  var detail = badge ? badge.nextElementSibling : null;
  if (detail && detail.classList.contains("verdict-detail")) {
    var isHidden = detail.style.display === "none";
    detail.style.display = isHidden ? "block" : "none";
    btn.textContent = isHidden ? "hide" : "details";
  }
}

function updateVerdictBadge(verdict) {
  if (!verdict || !verdict.call_id) return;
  var escapedId = CSS.escape(verdict.call_id);
  var badge = document.querySelector(
    '.verdict-badge[data-call-id="' + escapedId + '"]',
  );
  if (!badge) return;

  // Update risk level class
  var risk = verdict.risk_level || "medium";
  badge.className = "verdict-badge verdict-" + risk;

  // Update content spans
  var riskEl = badge.querySelector(".verdict-risk");
  var recEl = badge.querySelector(".verdict-rec");
  var confEl = badge.querySelector(".verdict-conf");
  if (riskEl) riskEl.textContent = risk.toUpperCase();
  if (recEl) recEl.textContent = verdict.recommendation || "review";
  if (confEl)
    confEl.textContent = Math.round((verdict.confidence || 0) * 100) + "%";

  // Remove spinner
  var spinner = badge.querySelector(".verdict-judge-spinner");
  if (spinner) spinner.remove();

  // Update detail section
  var detail = badge.nextElementSibling;
  if (detail && detail.classList.contains("verdict-detail")) {
    var summaryEl = detail.querySelector(".verdict-summary");
    var reasonEl = detail.querySelector(".verdict-reasoning");
    var tierEl = detail.querySelector(".verdict-tier");
    if (summaryEl) summaryEl.textContent = verdict.intent_summary || "";
    if (reasonEl) reasonEl.textContent = verdict.reasoning || "";
    if (tierEl)
      tierEl.textContent =
        (verdict.tier || "llm") +
        " tier" +
        (verdict.judge_model ? " | " + verdict.judge_model : "");
    // Update evidence
    var evidenceEl = detail.querySelector(".verdict-evidence");
    if (verdict.evidence && verdict.evidence.length) {
      if (!evidenceEl) {
        evidenceEl = document.createElement("div");
        evidenceEl.className = "verdict-evidence";
        var tierDiv = detail.querySelector(".verdict-tier");
        if (tierDiv) detail.insertBefore(evidenceEl, tierDiv);
        else detail.appendChild(evidenceEl);
      }
      evidenceEl.innerHTML = verdict.evidence
        .map(function (e) {
          return "<div>\u2022 " + escapeHtml(e) + "</div>";
        })
        .join("");
    } else if (evidenceEl) {
      evidenceEl.remove();
    }
  }

  // Update glow on approval buttons
  updateVerdictGlow(verdict.recommendation);
}

function updateVerdictGlow(recommendation) {
  var prompt = document.querySelector(".approval-prompt");
  if (!prompt) return;
  prompt.classList.remove(
    "verdict-glow-approve",
    "verdict-glow-deny",
    "verdict-glow-review",
  );
  if (recommendation === "approve")
    prompt.classList.add("verdict-glow-approve");
  else if (recommendation === "deny") prompt.classList.add("verdict-glow-deny");
  else prompt.classList.add("verdict-glow-review");
}

function showInlineToolBlock(items, autoApproved, judgePending) {
  const block = document.createElement("div");
  block.className = "msg approval-block" + (autoApproved ? " approved" : "");
  if (!autoApproved) {
    block.setAttribute("role", "alertdialog");
    block.setAttribute("aria-label", "Tool approval required");
  }

  // Track the highest-priority recommendation for glow
  var glowRec = null;

  items.forEach(function (item) {
    block.appendChild(buildToolDiv(item));
    // Render verdict badge if present
    if (item.verdict) {
      block.insertAdjacentHTML(
        "beforeend",
        renderVerdictBadge(item.verdict, judgePending),
      );
      // Track recommendation for glow (deny > review > approve)
      var rec = item.verdict.recommendation || "review";
      if (
        !glowRec ||
        rec === "deny" ||
        (rec === "review" && glowRec === "approve")
      ) {
        glowRec = rec;
      }
    }
  });

  if (autoApproved) {
    const badge = document.createElement("div");
    badge.setAttribute("role", "status");
    badge.className = "approval-badge badge-approved";
    badge.textContent = "\u2713 auto-approved";
    block.appendChild(badge);
  } else {
    const prompt = document.createElement("div");
    prompt.className = "approval-prompt";

    // Apply verdict glow on initial heuristic verdict
    if (glowRec) {
      if (glowRec === "approve") prompt.classList.add("verdict-glow-approve");
      else if (glowRec === "deny") prompt.classList.add("verdict-glow-deny");
      else prompt.classList.add("verdict-glow-review");
    }

    var alwaysNames = items
      .filter(function (it) {
        return (
          it.needs_approval &&
          it.func_name &&
          it.func_name !== "__budget_override__" &&
          !it.error
        );
      })
      .map(function (it) {
        return it.approval_label || it.func_name;
      });
    block.dataset.alwaysNames = JSON.stringify(alwaysNames);
    var alwaysTitle = alwaysNames.length
      ? "Always approve " + alwaysNames.join(", ")
      : "Always approve this tool type";
    const actions = document.createElement("div");
    actions.className = "approval-actions";
    actions.innerHTML =
      '<button class="approval-btn btn-approve" onclick="resolveInlineApproval(true,false,getFeedback())"><span class="key">y</span> Approve</button>' +
      '<button class="approval-btn btn-deny" onclick="resolveInlineApproval(false,false,getFeedback())"><span class="key">n</span> Deny</button>' +
      (alwaysNames.length
        ? '<button class="approval-btn btn-always" title="' +
          escapeHtml(alwaysTitle) +
          '" aria-label="' +
          escapeHtml(alwaysTitle) +
          '" onclick="resolveInlineApproval(true,true,getFeedback())"><span class="key">a</span> Always</button>'
        : "");
    prompt.appendChild(actions);

    const fbInput = document.createElement("input");
    fbInput.type = "text";
    fbInput.className = "approval-feedback-input";
    fbInput.placeholder = "feedback (optional)";
    prompt.appendChild(fbInput);

    block.appendChild(prompt);
    pendingApproval = true;
    approvalBlockEl = block;
    inputEl.disabled = true;
    sendBtn.disabled = true;
    requestAnimationFrame(function () {
      fbInput.focus();
    });
  }

  messagesEl.appendChild(block);
  scrollToBottom();
}

function resolveInlineApproval(approved, always, feedback, skipPost) {
  if (!approvalBlockEl) return;
  pendingApproval = false;

  // Remove prompt
  const prompt = approvalBlockEl.querySelector(".approval-prompt");
  if (prompt) prompt.remove();

  // Add badge
  const badge = document.createElement("div");
  badge.setAttribute("role", "status");
  if (approved) {
    badge.className = "approval-badge badge-approved";
    var label = "\u2713 approved";
    if (always) {
      var raw = approvalBlockEl.dataset.alwaysNames;
      var names = raw ? JSON.parse(raw) : [];
      label = names.length
        ? "\u2713 always approve " + names.join(", ")
        : "\u2713 always approve";
    }
    badge.textContent = feedback ? label + ": " + feedback : label;
    approvalBlockEl.classList.add("approved");
  } else {
    badge.className = "approval-badge badge-denied";
    badge.textContent = "\u2717 denied" + (feedback ? ": " + feedback : "");
    approvalBlockEl.classList.add("denied");
  }
  approvalBlockEl.appendChild(badge);
  approvalBlockEl = null;

  // Re-enable input
  inputEl.disabled = false;
  sendBtn.disabled = busy;
  inputEl.focus();

  // POST to server with ws_id (skip when server already resolved, e.g. timeout)
  if (!skipPost) {
    authFetch("/v1/api/approve", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        approved: approved,
        feedback: feedback || null,
        always: !!always,
        ws_id: currentWsId,
      }),
    }).catch(function (err) {
      addErrorMessage("Connection error: " + err.message);
    });
  }

  scrollToBottom();
}

function appendToolOutputChunk(callId, chunk) {
  if (!chunk) return;
  var stripped = stripAnsi(chunk);
  if (!stripped) return;

  // Find or create a streaming output element keyed by call_id
  var escapedId = callId ? CSS.escape(callId) : "";
  var el = escapedId
    ? messagesEl.querySelector(
        '.tool-output-stream[data-call-id="' + escapedId + '"]',
      )
    : null;
  if (!el) {
    // Primary: find the tool div matching this call_id
    var target = escapedId
      ? messagesEl.querySelector(
          '.approval-tool[data-call-id="' + escapedId + '"]',
        )
      : null;
    if (!target) {
      // Fallback: last bash tool in last approval-block
      var blocks = messagesEl.querySelectorAll(".approval-block");
      if (!blocks.length) return;
      var block = blocks[blocks.length - 1];
      var tools = block.querySelectorAll(
        '.approval-tool[data-func-name="bash"]',
      );
      target = tools.length ? tools[tools.length - 1] : null;
      if (!target) {
        var allTools = block.querySelectorAll(".approval-tool");
        target = allTools.length ? allTools[allTools.length - 1] : null;
      }
    }
    if (!target) return;

    el = document.createElement("pre");
    el.className = "tool-output tool-output-stream";
    el.dataset.callId = callId;
    el.setAttribute("aria-label", "Streaming command output");
    el.setAttribute("aria-live", "off");
    el.textContent = "";
    target.after(el);
  }

  el.appendChild(document.createTextNode(stripped));
  el.scrollTop = el.scrollHeight;
  scrollToBottom();
}

function showOutputWarning(evt) {
  if (!evt.call_id || evt.risk_level === "none") return;
  var escapedId = CSS.escape(evt.call_id);
  var toolDiv = messagesEl.querySelector(
    '.approval-tool[data-call-id="' + escapedId + '"]',
  );
  if (!toolDiv) return;
  var risk = evt.risk_level || "medium";
  var flags = evt.flags || [];
  var warning = document.createElement("div");
  warning.className = "output-warning output-warning-" + risk;
  warning.setAttribute("role", "alert");
  warning.innerHTML =
    '<span class="output-warning-label">\u26a0 ' +
    escapeHtml(risk.toUpperCase()) +
    "</span> " +
    flags.map(escapeHtml).join(", ");
  if (evt.redacted) {
    warning.innerHTML +=
      ' <span class="output-warning-redacted">(credentials redacted)</span>';
  }
  // Insert after tool output if it already exists, otherwise after tool div
  var nextEl = toolDiv.nextElementSibling;
  if (nextEl && nextEl.classList.contains("tool-output")) {
    nextEl.insertAdjacentElement("afterend", warning);
  } else {
    toolDiv.insertAdjacentElement("afterend", warning);
  }
}

function appendToolOutput(callId, name, output) {
  var escapedId = callId ? CSS.escape(callId) : "";
  // Primary: find tool div by call_id
  var target = escapedId
    ? messagesEl.querySelector(
        '.approval-tool[data-call-id="' + escapedId + '"]',
      )
    : null;
  // Fallback: last block, match by func_name
  if (!target) {
    const blocks = messagesEl.querySelectorAll(".approval-block");
    if (!blocks.length) return;
    const block = blocks[blocks.length - 1];
    const tools = block.querySelectorAll(".approval-tool");
    for (let i = tools.length - 1; i >= 0; i--) {
      if (tools[i].dataset.funcName === name) {
        target = tools[i];
        break;
      }
    }
    if (!target && tools.length) target = tools[tools.length - 1];
  }
  if (!target) return;

  // Remove the streaming output element for this tool
  var streamEl = null;
  if (escapedId) {
    streamEl = messagesEl.querySelector(
      '.tool-output-stream[data-call-id="' + escapedId + '"]',
    );
  } else {
    var next = target.nextElementSibling;
    if (next && next.classList.contains("tool-output-stream")) {
      streamEl = next;
    }
  }
  if (streamEl) streamEl.remove();

  const stripped = stripAnsi(output || "").trim();
  if (!stripped) return;

  const out = document.createElement("div");
  out.className = "tool-output";
  out.textContent = stripped;

  // Auto-collapse long output (keyboard-accessible)
  if (stripped.split("\n").length > 10) {
    makeCollapsible(out);
  }

  // Insert after the target tool div
  target.after(out);
  scrollToBottom();
}

function addInfoMessage(text) {
  const el = document.createElement("div");
  el.className = "msg msg-info";
  el.textContent = stripAnsi(text);
  messagesEl.appendChild(el);
  scrollToBottom();
}

function addErrorMessage(text) {
  const el = document.createElement("div");
  el.className = "msg msg-error";
  el.setAttribute("role", "alert");
  el.textContent = stripAnsi(text);
  messagesEl.appendChild(el);
  scrollToBottom();
}

function updateStatus(evt) {
  let parts = [
    evt.total_tokens.toLocaleString() +
      " / " +
      evt.context_window.toLocaleString() +
      " tokens (" +
      evt.pct +
      "%)",
  ];
  if (evt.effort !== "medium") parts.push("reasoning: " + evt.effort);
  statusBar.textContent = parts.join(" \u00b7 ");
}

function isNearBottom() {
  return (
    messagesEl.scrollHeight - messagesEl.scrollTop - messagesEl.clientHeight <
    80
  );
}
function scrollToBottom(force) {
  if (force || isNearBottom()) {
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }
}

// --- Plan review dialog ---
var _planContent = "";
function showPlanDialog(content) {
  _planContent = content;
  document.getElementById("plan-content").textContent = content;
  var feedbackEl = document.getElementById("plan-feedback");
  feedbackEl.value = "";
  _updatePlanRejectBtn();
  inputEl.disabled = true;
  sendBtn.disabled = true;
  document.getElementById("plan-overlay").classList.add("active");
  setTimeout(function () {
    feedbackEl.focus();
  }, 50);
}

function _updatePlanRejectBtn() {
  var btn = document.getElementById("btn-plan-reject");
  var hasFeedback =
    document.getElementById("plan-feedback").value.trim().length > 0;
  btn.innerHTML = hasFeedback
    ? '<span class="key">Esc</span> Amend'
    : '<span class="key">Esc</span> Reject';
  btn.style.background = hasFeedback ? "var(--accent)" : "";
  btn.style.color = hasFeedback ? "var(--on-color)" : "";
  btn.onclick = function () {
    resolvePlan(hasFeedback ? "" : "reject");
  };
}

function resolvePlan(defaultFeedback) {
  let feedback = document.getElementById("plan-feedback").value.trim();
  if (!feedback && defaultFeedback) feedback = defaultFeedback;
  document.getElementById("plan-overlay").classList.remove("active");
  inputEl.disabled = false;
  sendBtn.disabled = false;
  inputEl.focus();

  // Critical: fire the API call first — this unblocks the server.
  // The inline rendering below is cosmetic and must never prevent it.
  authFetch("/v1/api/plan", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ feedback: feedback, ws_id: currentWsId }),
  }).catch(function (err) {
    addErrorMessage("Connection error: " + err.message);
  });

  // Render plan inline in the chat (best-effort)
  try {
    var isReject = feedback === "reject";
    var isAmend = feedback && !isReject;
    var action = isReject ? "rejected" : isAmend ? "amending" : "approved";
    _addInlinePlan(_planContent, action, feedback);
  } catch (err) {
    console.error("Failed to render inline plan:", err);
    addInfoMessage("Plan " + action);
  }

  // Show spinner while the model processes the plan result
  setBusy(true);
  addThinkingIndicator();
}

function _addInlinePlan(content, action, feedback) {
  if (!content) return;
  var wrapper = document.createElement("div");
  wrapper.className = "plan-inline";

  var header = document.createElement("div");
  header.className = "plan-inline-header";
  var label =
    action === "rejected"
      ? "Plan rejected"
      : action === "amending"
        ? "Plan — amending"
        : "Plan approved";
  header.innerHTML =
    '<span class="plan-inline-label plan-' + action + '">' + label + "</span>";
  wrapper.appendChild(header);

  var body = document.createElement("div");
  body.className = "plan-inline-body";
  try {
    body.innerHTML = renderMarkdown(content);
    postRenderMarkdown(body);
  } catch (e) {
    body.textContent = content;
  }
  if (content.split("\n").length > 12) {
    makeCollapsible(body);
    body.setAttribute(
      "aria-label",
      "Plan content (collapsed). Activate to expand.",
    );
  }
  wrapper.appendChild(body);

  if (feedback && action === "amending") {
    var fb = document.createElement("div");
    fb.className = "plan-inline-feedback";
    fb.textContent = "Feedback: " + feedback;
    wrapper.appendChild(fb);
  }

  messagesEl.appendChild(wrapper);
  scrollToBottom();
}

// --- Send message ---
function sendMessage() {
  const text = inputEl.value.trim();
  if (!text || busy) return;

  if (text.startsWith("/")) {
    // Slash command
    authFetch("/v1/api/command", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ command: text, ws_id: currentWsId }),
    });
    addUserMessage(text);
    inputEl.value = "";
    autoResize();
    return;
  }

  setBusy(true);
  addUserMessage(text);
  inputEl.value = "";
  autoResize();

  authFetch("/v1/api/send", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message: text, ws_id: currentWsId }),
  }).catch(function (err) {
    addErrorMessage("Connection error: " + err.message);
    setBusy(false);
  });
}

function cancelGeneration() {
  if (!busy || !currentWsId) return;
  stopBtn.disabled = true;
  authFetch("/v1/api/cancel", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ws_id: currentWsId }),
  }).catch(function (err) {
    addErrorMessage("Cancel error: " + err.message);
    stopBtn.disabled = false; // Re-enable only on error so user can retry
  });
  // On success, button stays disabled until setBusy(false) hides it
}

// --- Textarea auto-resize and keyboard shortcuts ---
function autoResize() {
  inputEl.style.height = "auto";
  inputEl.style.height = Math.min(inputEl.scrollHeight, 200) + "px";
}

inputEl.addEventListener("input", autoResize);
document
  .getElementById("plan-feedback")
  .addEventListener("input", _updatePlanRejectBtn);
inputEl.addEventListener("keydown", function (e) {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    sendMessage();
  }
});
document
  .getElementById("dashboard-input")
  .addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      dashboardSendMessage();
    }
  });

// Keyboard shortcuts for inline approval + plan dialog + tabs
document.addEventListener("keydown", function (e) {
  // Defer to modal's own keydown handler when new-ws modal is open
  var nwsOverlay = document.getElementById("new-ws-overlay");
  if (nwsOverlay && nwsOverlay.style.display !== "none") return;
  // Escape: close hamburger first, then dashboard
  if (
    e.key === "Escape" &&
    document.getElementById("hamburger-menu").classList.contains("open")
  ) {
    e.preventDefault();
    closeHamburger();
    document.getElementById("hamburger-btn").focus();
    return;
  }
  if (e.key === "Escape" && dashboardVisible) {
    e.preventDefault();
    hideDashboard();
    return;
  }
  // Escape: cancel generation when busy
  if (e.key === "Escape" && busy && !pendingApproval) {
    e.preventDefault();
    cancelGeneration();
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
    var idx = parseInt(e.key) - 1;
    var wsIds = Object.keys(workstreams);
    if (idx < wsIds.length) switchTab(wsIds[idx]);
    return;
  }
  // Ctrl+W: close current tab
  if (e.ctrlKey && e.key === "w") {
    if (Object.keys(workstreams).length > 1) {
      e.preventDefault();
      closeWorkstream(currentWsId);
    }
    return;
  }

  // Inline approval keybindings
  if (pendingApproval) {
    // If typing in the feedback input, let keys through except Enter/Escape
    var fbInput =
      approvalBlockEl &&
      approvalBlockEl.querySelector(".approval-feedback-input");
    if (fbInput && document.activeElement === fbInput) {
      if (e.key === "Enter") {
        e.preventDefault();
        resolveInlineApproval(true, false, getFeedback());
      } else if (e.key === "Escape") {
        e.preventDefault();
        resolveInlineApproval(false, false, getFeedback());
      }
      return; // let normal typing pass through
    }
    // Not in feedback input — intercept shortcut keys
    e.preventDefault();
    e.stopPropagation();
    if (e.key === "y" || e.key === "Enter") {
      resolveInlineApproval(true, false, getFeedback());
    } else if (e.key === "n" || e.key === "Escape") {
      resolveInlineApproval(false, false, getFeedback());
    } else if (e.key === "a") {
      resolveInlineApproval(true, true, getFeedback());
    } else if (e.key === "d") {
      // Toggle verdict details panel
      var details = approvalBlockEl
        ? approvalBlockEl.querySelectorAll(".verdict-detail")
        : [];
      details.forEach(function (d) {
        var isHidden = d.style.display === "none";
        d.style.display = isHidden ? "block" : "none";
        var btn = d.previousElementSibling
          ? d.previousElementSibling.querySelector(".verdict-expand")
          : null;
        if (btn) btn.textContent = isHidden ? "hide" : "details";
      });
    }
    return;
  }
  // Plan dialog
  if (document.getElementById("plan-overlay").classList.contains("active")) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      resolvePlan("");
    } else if (e.key === "Escape") {
      e.preventDefault();
      var hasFb =
        document.getElementById("plan-feedback").value.trim().length > 0;
      resolvePlan(hasFb ? "" : "reject");
    } else if (e.key === "Tab") {
      var focusable = document.querySelectorAll(
        "#plan-dialog input, #plan-dialog button",
      );
      var first = focusable[0],
        last = focusable[focusable.length - 1];
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    }
  }
});

// --- Init: fetch workstream list, then connect ---
initLogin();
pollHealth();
authFetch("/v1/api/workstreams")
  .then(function (r) {
    return r.json();
  })
  .then(function (data) {
    data.workstreams.forEach(function (ws) {
      workstreams[ws.id] = { name: ws.name, state: ws.state };
    });
    var wsIds = Object.keys(workstreams);
    if (wsIds.length) {
      currentWsId = wsIds[0];
      renderTabBar();
    }
    connectGlobalSSE();
    // Deep linking: check for ?ws_id= query parameter
    var params = new URLSearchParams(location.search);
    var targetWs = params.get("ws_id");
    if (targetWs && workstreams[targetWs]) {
      history.replaceState(
        { turnstone: "workstream", wsId: targetWs },
        "",
        location.pathname,
      );
      // Force switch even if targetWs is already currentWsId — on init
      // the SSE connection hasn't been established yet.
      currentWsId = targetWs;
      messagesEl.innerHTML = "";
      showEmptyState();
      renderTabBar();
      connectContentSSE(targetWs);
    } else {
      if (currentWsId) connectContentSSE(currentWsId);
      history.replaceState({ turnstone: "dashboard" }, "", location.pathname);
      showDashboard();
    }
  });

// Back/forward button: retrace dashboard → tab1 → tab2 navigation.
window.addEventListener("popstate", function (e) {
  _historyNavigation = true;
  try {
    if (e.state && e.state.turnstone === "workstream") {
      // Navigating to a workstream state (forward, or back between tabs).
      if (dashboardVisible) hideDashboard();
      if (e.state.wsId && workstreams[e.state.wsId]) switchTab(e.state.wsId);
    } else {
      // Navigating to the dashboard state (back from any workstream).
      if (!dashboardVisible) showDashboard();
    }
  } finally {
    _historyNavigation = false;
  }
});
