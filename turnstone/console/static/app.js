// --- Shared hooks ---
window.onLoginSuccess = function () {
  connectSSE();
  // Refresh permission-gated home-landing UI (admin.coordinator etc).
  if (typeof _refreshHomeComposerVisibility === "function") {
    _refreshHomeComposerVisibility();
  }
  // Re-populate the home-composer skill dropdown and re-probe the
  // coordinator subsystem now that auth has landed.  The initial
  // page-load pass runs before login completes, so /v1/api/skills
  // and /v1/api/workstreams both 401; without this re-run the
  // dropdown stays empty and the 503 banner never flips correctly.
  if (typeof _populateHomeSkillDropdown === "function") {
    _populateHomeSkillDropdown();
  }
  if (typeof _probeCoordSubsystem === "function") {
    _probeCoordSubsystem();
  }
  // Active-coordinators list is SSE-driven via the console pseudo-node
  // (#9) — no poller to restart after login.  The home-view renderer
  // reads from clusterState.nodes["console"].workstreams on every SSE
  // patch, so authenticating just unblocks the normal event stream.
  if (typeof loadSavedCoordinators === "function") {
    loadSavedCoordinators();
  }
};
window.onLogout = function () {
  if (evtSource) {
    evtSource.close();
    evtSource = null;
  }
  if (typeof _refreshHomeComposerVisibility === "function") {
    _refreshHomeComposerVisibility();
  }
};
window.onThemeChange = function (next) {
  var btn = document.getElementById("theme-toggle");
  if (btn) {
    var isLight = next === "light";
    btn.textContent = isLight ? "\u2600" : "\u263E";
    btn.title = isLight ? "Switch to dark theme" : "Switch to light theme";
    btn.setAttribute(
      "aria-label",
      isLight ? "Switch to dark theme" : "Switch to light theme",
    );
  }
  // Persist to server so admin settings and node UIs see the change
  var themeValue = next === "light" ? "light" : "dark";
  authFetch("/v1/api/admin/settings/interface.theme", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ value: themeValue }),
  }).catch(function () {});
};
// Set initial theme button text and aria
(function () {
  var btn = document.getElementById("theme-toggle");
  if (btn) {
    var isLight = document.documentElement.dataset.theme === "light";
    btn.textContent = isLight ? "\u2600" : "\u263E";
    btn.title = isLight ? "Switch to dark theme" : "Switch to light theme";
    btn.setAttribute(
      "aria-label",
      isLight ? "Switch to dark theme" : "Switch to light theme",
    );
  }
})();

// --- State ---
var currentView = "home"; // "home" | "overview" | "node" | "filtered" | "admin"
var currentNodeId = null;
var currentServerUrl = "";
var currentFilter = { state: null, node: null, page: 1, per_page: 50 };
var expandedGroups = {};
var _lastOverviewJson = "";
var _lastNodesJson = "";
var evtSource = null;
var retryDelay = 1000;
var clusterState = null;
var _navigatingFromPopstate = false;

// --- Constants ---
var STATE_DISPLAY = {
  running: { symbol: "\u25b8", label: "run" },
  thinking: { symbol: "\u25cc", label: "think" },
  attention: { symbol: "\u25c6", label: "attn" },
  idle: { symbol: "\u00b7", label: "idle" },
  error: { symbol: "\u2716", label: "err" },
};
var STATE_ORDER = ["running", "thinking", "attention", "error", "idle"];

// --- Cluster State Model ---
function applySnapshot(data) {
  clusterState = {
    nodes: {},
    overview: data.overview || {},
    timestamp: data.timestamp || 0,
  };
  (data.nodes || []).forEach(function (n) {
    clusterState.nodes[n.node_id] = n;
  });
  renderFromState();
}

function patchClusterState(data) {
  if (!clusterState) return;
  var t = data.type;
  if (t === "cluster_state") {
    var node = clusterState.nodes[data.node_id];
    if (node) {
      (node.workstreams || []).forEach(function (ws) {
        if (ws.id === data.ws_id) {
          if ("state" in data) ws.state = data.state;
          if ("tokens" in data) ws.tokens = data.tokens;
          if ("context_ratio" in data) ws.context_ratio = data.context_ratio;
          if ("activity" in data) ws.activity = data.activity;
          if ("activity_state" in data) ws.activity_state = data.activity_state;
        }
      });
    }
  } else if (t === "ws_created") {
    var targetNode = clusterState.nodes[data.node_id];
    if (targetNode) {
      targetNode.workstreams = targetNode.workstreams || [];
      targetNode.workstreams.push({
        id: data.ws_id,
        name: data.name || "",
        state: "idle",
        node: data.node_id,
        server_url: targetNode.server_url || "",
        title: data.title || "",
        tokens: 0,
        context_ratio: 0.0,
        activity: "",
        activity_state: "",
        tool_calls: 0,
        // ws_created SSE events carry kind / parent_ws_id / user_id;
        // preserve them on the in-memory ws so the home-landing
        // active-coordinators list and the tree grouping both pick up
        // newly-created rows without needing a snapshot refetch.
        kind: data.kind || "interactive",
        parent_ws_id: data.parent_ws_id || null,
        user_id: data.user_id || null,
      });
    }
  } else if (t === "ws_closed") {
    // Peek BEFORE the filter so we can tell whether the closed ws was a
    // coordinator (lives on the console pseudo-node, kind="coordinator")
    // and only then refetch the saved list.  ws_closed payloads from
    // real-node interactive closes don't carry kind on the wire, but
    // they're already typed in clusterState from the matching ws_created
    // event.  Skipping interactive closes avoids per-close fan-out into
    // /v1/api/workstreams/saved on busy clusters.
    var wasCoordinator = false;
    Object.keys(clusterState.nodes).forEach(function (nid) {
      (clusterState.nodes[nid].workstreams || []).forEach(function (ws) {
        if (ws.id === data.ws_id && ws.kind === "coordinator") {
          wasCoordinator = true;
        }
      });
    });
    Object.keys(clusterState.nodes).forEach(function (nid) {
      var n = clusterState.nodes[nid];
      n.workstreams = (n.workstreams || []).filter(function (ws) {
        return ws.id !== data.ws_id;
      });
    });
    if (wasCoordinator && typeof loadSavedCoordinators === "function") {
      loadSavedCoordinators();
    }
  } else if (t === "ws_rename") {
    Object.keys(clusterState.nodes).forEach(function (nid) {
      (clusterState.nodes[nid].workstreams || []).forEach(function (ws) {
        if (ws.id === data.ws_id) ws.name = data.name || "";
      });
    });
  } else if (t === "node_joined") {
    if (!clusterState.nodes[data.node_id]) {
      clusterState.nodes[data.node_id] = {
        node_id: data.node_id,
        server_url: "",
        max_ws: 10,
        reachable: true,
        version: "",
        health: {},
        aggregate: {},
        workstreams: [],
      };
    }
  } else if (t === "node_lost") {
    delete clusterState.nodes[data.node_id];
  } else {
    return;
  }
  scheduleRender();
}

var _renderTimer = null;
function scheduleRender() {
  if (_renderTimer) return;
  _renderTimer = requestAnimationFrame(function () {
    _renderTimer = null;
    recomputeOverview();
    renderFromState();
  });
}

function recomputeOverview() {
  if (!clusterState) return;
  var states = { running: 0, thinking: 0, attention: 0, idle: 0, error: 0 };
  var totalTokens = 0,
    totalToolCalls = 0,
    totalWs = 0;
  var mcpServers = 0,
    mcpResources = 0,
    mcpPrompts = 0;
  var versions = {};
  Object.keys(clusterState.nodes).forEach(function (nid) {
    // Skip the "console" pseudo-node — coordinators aren't compute-
    // node workstreams, and counting them here would inflate the
    // cluster-summary totals the home view renders.  The
    // active-coordinators list surfaces them separately.
    if (nid === "console") return;
    var node = clusterState.nodes[nid];
    var nodeWsTokens = 0;
    (node.workstreams || []).forEach(function (ws) {
      var s = ws.state || "idle";
      states[s] = (states[s] || 0) + 1;
      totalWs++;
      nodeWsTokens += ws.tokens || 0;
    });
    var aggTokens = (node.aggregate || {}).total_tokens || 0;
    totalTokens += aggTokens || nodeWsTokens;
    totalToolCalls += (node.aggregate || {}).total_tool_calls || 0;
    if (node.version) versions[node.version] = true;
    var mcp = (node.health || {}).mcp || {};
    mcpServers += mcp.servers || 0;
    mcpResources += mcp.resources || 0;
    mcpPrompts += mcp.prompts || 0;
  });
  var versionList = Object.keys(versions).sort();
  // Count only real compute nodes for the cluster summary — the
  // "console" pseudo-node hosts coordinators, which are surfaced
  // separately by the active-coordinators list.
  var realNodeCount = Object.keys(clusterState.nodes).filter(function (nid) {
    return nid !== "console";
  }).length;
  clusterState.overview = {
    nodes: realNodeCount,
    workstreams: totalWs,
    states: states,
    aggregate: {
      total_tokens: totalTokens,
      total_tool_calls: totalToolCalls,
    },
    version_drift: versionList.length > 1,
    versions: versionList,
  };
  if (mcpServers > 0) {
    clusterState.overview.mcp_servers = mcpServers;
    clusterState.overview.mcp_resources = mcpResources;
    clusterState.overview.mcp_prompts = mcpPrompts;
  }
}

function buildNodeInfoFromSnapshot(node) {
  var states = { running: 0, thinking: 0, attention: 0, idle: 0, error: 0 };
  var ws = node.workstreams || [];
  ws.forEach(function (w) {
    var s = w.state || "idle";
    states[s] = (states[s] || 0) + 1;
  });
  var aggTokens = (node.aggregate || {}).total_tokens || 0;
  if (!aggTokens) {
    ws.forEach(function (w) {
      aggTokens += w.tokens || 0;
    });
  }
  return {
    node_id: node.node_id,
    server_url: node.server_url || "",
    ws_total: ws.length,
    ws_running: states.running,
    ws_thinking: states.thinking,
    ws_attention: states.attention,
    ws_idle: states.idle,
    ws_error: states.error,
    total_tokens: aggTokens,
    ws_tokens: aggTokens,
    max_ws: node.max_ws || 10,
    started: node.started || 0,
    reachable: node.reachable !== false,
    reachable_reason: node.reachable_reason || "",
    health: node.health || {},
    version: node.version || "",
  };
}

function renderFromState() {
  if (!clusterState) return;
  renderStatusBar(clusterState.overview);
  if (currentView === "home") {
    _renderHomeView();
    // Home view also hosts the inline node-list (cluster details);
    // render it so expanding the cluster-summary reveals the current
    // state without waiting for the next SSE tick.
    var nodesList = Object.keys(clusterState.nodes)
      .filter(function (nid) {
        // Exclude the "console" pseudo-node from the nodes list — it's
        // a synthetic carrier for coordinators, not a compute node.
        return nid !== "console";
      })
      .map(function (nid) {
        return buildNodeInfoFromSnapshot(clusterState.nodes[nid]);
      });
    nodesList.sort(function (a, b) {
      var d = b.ws_running + b.ws_attention - (a.ws_running + a.ws_attention);
      return d !== 0 ? d : a.node_id.localeCompare(b.node_id);
    });
    renderNodeGroups(nodesList, nodesList.length);
  } else if (currentView === "node" && currentNodeId) {
    var snapNode = clusterState.nodes[currentNodeId];
    if (snapNode) {
      var wsList = snapNode.workstreams || [];
      var active = wsList.filter(function (w) {
        return w.state !== "idle";
      }).length;
      document.getElementById("node-ws-summary").textContent =
        active + " active \u00b7 " + wsList.length + " total";
      var mcpSumEl = document.getElementById("node-mcp-summary");
      if (mcpSumEl) {
        var mcpInfo = snapNode.health && snapNode.health.mcp;
        if (mcpInfo && mcpInfo.servers > 0) {
          mcpSumEl.textContent =
            mcpInfo.servers +
            " MCP server" +
            (mcpInfo.servers !== 1 ? "s" : "") +
            " \u00b7 " +
            mcpInfo.resources +
            " resources \u00b7 " +
            mcpInfo.prompts +
            " prompts";
        } else {
          mcpSumEl.textContent = "";
        }
      }
      renderWsTable(document.getElementById("node-ws-table"), wsList);
    }
  } else if (currentView === "filtered") {
    var allWs = [];
    Object.keys(clusterState.nodes).forEach(function (nid) {
      (clusterState.nodes[nid].workstreams || []).forEach(function (ws) {
        allWs.push(ws);
      });
    });
    if (currentFilter.state) {
      allWs = allWs.filter(function (ws) {
        return ws.state === currentFilter.state;
      });
    }
    if (currentFilter.node) {
      allWs = allWs.filter(function (ws) {
        return ws.node === currentFilter.node;
      });
    }
    var stateOrder = {
      running: 0,
      thinking: 1,
      attention: 2,
      error: 3,
      idle: 4,
    };
    allWs.sort(function (a, b) {
      return (stateOrder[a.state] || 9) - (stateOrder[b.state] || 9);
    });
    var total = allWs.length;
    var perPage = currentFilter.per_page || 50;
    var pages = Math.max(1, Math.ceil(total / perPage));
    var page = Math.min(currentFilter.page || 1, pages);
    var start = (page - 1) * perPage;
    var pageWs = allWs.slice(start, start + perPage);
    document.getElementById("filtered-summary").textContent =
      "Page " + page + " of " + pages + " (" + total + " total)";
    renderWsTable(document.getElementById("filtered-ws-table"), pageWs);
    renderPagination(
      document.getElementById("filtered-pagination"),
      page,
      pages,
    );
  }
}

// --- SSE Connection ---
function connectSSE() {
  if (evtSource) {
    evtSource.close();
    evtSource = null;
  }
  evtSource = new EventSource("/v1/api/cluster/events");
  var statusBar = document.getElementById("status-bar");
  evtSource.onopen = function () {
    retryDelay = 1000;
    statusBar.classList.remove("disconnected");
    statusBar.textContent = "";
    var csb = document.getElementById("cluster-status-bar");
    if (csb) csb.classList.remove("stale");
  };
  evtSource.onmessage = function (e) {
    try {
      var data = JSON.parse(e.data);
      handleClusterEvent(data);
    } catch (err) {
      /* ignore malformed SSE */
    }
  };
  evtSource.onerror = function () {
    evtSource.close();
    evtSource = null;
    // Don't show reconnecting state if login overlay is visible
    var loginOverlay = document.getElementById("login-overlay");
    if (loginOverlay && loginOverlay.style.display !== "none") return;
    statusBar.textContent = "Reconnecting\u2026";
    statusBar.classList.add("disconnected");
    var csb = document.getElementById("cluster-status-bar");
    if (csb) csb.classList.add("stale");
    // Raw fetch (not authFetch) — need to inspect status before throwing
    fetch("/v1/api/cluster/overview")
      .then(function (r) {
        if (r.status === 401) {
          showLogin();
          return;
        }
        setTimeout(connectSSE, retryDelay);
        retryDelay = Math.min(retryDelay * 2, 30000);
      })
      .catch(function () {
        setTimeout(connectSSE, retryDelay);
        retryDelay = Math.min(retryDelay * 2, 30000);
      });
  };
}

function handleClusterEvent(data) {
  if (data.type === "snapshot") {
    applySnapshot(data);
    return;
  }
  if (
    data.type === "cluster_state" ||
    data.type === "ws_created" ||
    data.type === "ws_closed" ||
    data.type === "ws_rename" ||
    data.type === "node_joined" ||
    data.type === "node_lost"
  ) {
    patchClusterState(data);
  }
  if (data.type === "ws_closed" && data.reason === "evicted") {
    showToast("Evicted" + (data.name ? ": " + data.name : "") + " (capacity)");
  }
}

// --- Home View ---
//
// Coordinator-first landing: composer + active-coordinators list +
// compact cluster summary.  The legacy #view-overview stays reachable
// via the summary's expand button and the existing popstate / deep-
// link wiring so `?view=overview` etc. still land on the node list.
function showHome() {
  currentView = "home";
  currentNodeId = null;
  currentServerUrl = "";
  currentFilter = { state: null, node: null, page: 1, per_page: 50 };
  _setLandingView("home");
  _setClusterDetailsExpanded(false);
  var adminView = document.getElementById("view-admin");
  if (adminView) adminView.style.display = "none";
  var adminBtn = document.getElementById("admin-btn");
  if (adminBtn) {
    adminBtn.classList.remove("active");
    adminBtn.setAttribute("aria-expanded", "false");
  }
  document.getElementById("breadcrumb").style.display = "none";
  document.getElementById("main").scrollTop = 0;
  if (clusterState) renderFromState();
  else loadOverview();
  _ensureHomeComposerInit();
  if (!_navigatingFromPopstate) history.pushState({ view: "home" }, "");
}

function _setLandingView(which) {
  // Toggle the three top-level landing panes.  The "overview" (node
  // list) is no longer its own pane — it's an expandable section
  // inside #view-home, toggled by toggleClusterDetails() — so every
  // view transition only needs to choose between home / node /
  // filtered.
  var views = ["home", "node", "filtered"];
  views.forEach(function (name) {
    var el = document.getElementById("view-" + name);
    if (!el) return;
    el.style.display = name === which ? "" : "none";
  });
}

// Toggle the inline cluster-details (node list) panel inside
// #view-home.  Replaces the old "swap to #view-overview" navigation so
// operators never leave the landing page to see cluster state.
function _setClusterDetailsExpanded(expanded) {
  var details = document.getElementById("view-overview");
  var btn = document.getElementById("cluster-summary-expand");
  var caret = document.querySelector(".home-cluster-summary-caret");
  if (!details) return;
  if (expanded) {
    details.removeAttribute("hidden");
    if (btn) btn.setAttribute("aria-expanded", "true");
    if (caret) caret.textContent = "\u25BE"; // ▾
  } else {
    details.setAttribute("hidden", "");
    if (btn) btn.setAttribute("aria-expanded", "false");
    if (caret) caret.textContent = "\u25B8"; // ▸
  }
}

function toggleClusterDetails() {
  var details = document.getElementById("view-overview");
  if (!details) return;
  _setClusterDetailsExpanded(details.hasAttribute("hidden"));
}

// --- Overview (alias: expanded cluster-details on the home view) ---
// Preserved so breadcrumb "Cluster" links, popstate {view:"overview"},
// and ?view=overview deep-links still land on a meaningful state.
// Semantically equivalent to showHome() with the cluster-details
// section forced open.
function showOverview() {
  currentView = "home";
  currentNodeId = null;
  currentServerUrl = "";
  currentFilter = { state: null, node: null, page: 1, per_page: 50 };
  _setLandingView("home");
  _setClusterDetailsExpanded(true);
  var adminView = document.getElementById("view-admin");
  if (adminView) adminView.style.display = "none";
  var adminBtn = document.getElementById("admin-btn");
  if (adminBtn) {
    adminBtn.classList.remove("active");
    adminBtn.setAttribute("aria-expanded", "false");
  }
  document.getElementById("breadcrumb").style.display = "none";
  if (clusterState) renderFromState();
  else loadOverview();
  if (!_navigatingFromPopstate) history.pushState({ view: "home" }, "");
  // Scroll the details section into view so the click produces a
  // visible reaction — the user expands the summary expecting to see
  // the node list, not wonder whether the click worked.
  var details = document.getElementById("view-overview");
  if (details && typeof details.scrollIntoView === "function") {
    details.scrollIntoView({ block: "start", behavior: "smooth" });
  }
}

function loadOverview() {
  authFetch("/v1/api/cluster/snapshot")
    .then(function (r) {
      return r.json();
    })
    .then(function (data) {
      applySnapshot(data);
    })
    .catch(function () {
      document.getElementById("node-table").innerHTML =
        '<div class="dashboard-empty">Failed to load</div>';
    });
}

// --- Status Bar ---
function renderStatusBar(overview) {
  var cacheKey =
    JSON.stringify(overview) +
    "|" +
    currentView +
    "|" +
    (currentFilter.state || "");
  if (cacheKey === _lastOverviewJson) return;
  _lastOverviewJson = cacheKey;

  var states = overview.states || {};
  var agg = overview.aggregate || {};

  var statesContainer = document.getElementById("csb-states");
  statesContainer.innerHTML = "";
  STATE_ORDER.forEach(function (state) {
    var count = states[state] || 0;
    var sd = STATE_DISPLAY[state] || STATE_DISPLAY.idle;
    var pill = document.createElement("button");
    pill.className = "csb-state";
    if (currentView === "filtered" && currentFilter.state === state) {
      pill.classList.add("active");
    }
    pill.setAttribute("aria-label", sd.label + ": " + count + " workstreams");
    pill.innerHTML =
      '<span class="csb-state-dot" data-state="' +
      escapeHtml(state) +
      '" aria-hidden="true"></span>' +
      '<span class="csb-state-count' +
      (count === 0 ? " zero" : "") +
      '">' +
      formatCount(count) +
      "</span>" +
      '<span class="csb-state-label">' +
      sd.label +
      "</span>";
    pill.onclick = function () {
      drillDownByState(state);
    };
    statesContainer.appendChild(pill);
  });

  var metricsContainer = document.getElementById("csb-metrics");
  metricsContainer.innerHTML = "";
  var metrics = [
    { value: overview.nodes || 0, label: "nodes", format: formatCount },
    { value: overview.workstreams || 0, label: "ws", format: formatCount },
    { value: agg.total_tokens || 0, label: "tokens", format: formatTokens },
    { value: agg.total_tool_calls || 0, label: "calls", format: formatCount },
  ];
  metrics.forEach(function (m) {
    if (m.value === 0 && m.label !== "nodes" && m.label !== "ws") return;
    var el = document.createElement("span");
    el.className = "csb-metric";
    var valSpan = document.createElement("span");
    valSpan.className = "csb-metric-value";
    valSpan.textContent = m.format(m.value);
    var labelSpan = document.createElement("span");
    labelSpan.className = "csb-metric-label";
    labelSpan.textContent = m.label;
    el.appendChild(valSpan);
    el.appendChild(labelSpan);
    metricsContainer.appendChild(el);
  });
  if (
    overview.version_drift &&
    overview.versions &&
    overview.versions.length > 1
  ) {
    var driftEl = document.createElement("span");
    driftEl.className = "csb-metric csb-version-drift";
    driftEl.title = "Versions detected: " + overview.versions.join(", ");
    var warnSpan = document.createElement("span");
    warnSpan.className = "csb-metric-value drift-warn";
    warnSpan.textContent = "DRIFT";
    var verLabel = document.createElement("span");
    verLabel.className = "csb-metric-label";
    verLabel.textContent = overview.versions.join(" / ");
    driftEl.appendChild(warnSpan);
    driftEl.appendChild(verLabel);
    metricsContainer.appendChild(driftEl);
  } else if (overview.versions && overview.versions.length === 1) {
    var verEl = document.createElement("span");
    verEl.className = "csb-metric";
    var valSpan = document.createElement("span");
    valSpan.className = "csb-metric-value";
    valSpan.textContent = overview.versions[0];
    var verLbl = document.createElement("span");
    verLbl.className = "csb-metric-label";
    verLbl.textContent = "ver";
    verEl.appendChild(valSpan);
    verEl.appendChild(verLbl);
    metricsContainer.appendChild(verEl);
  }
  // MCP aggregate metrics
  if (overview.mcp_servers && overview.mcp_servers > 0) {
    var mcpDivider = document.createElement("span");
    mcpDivider.className = "csb-divider";
    mcpDivider.setAttribute("aria-hidden", "true");
    metricsContainer.appendChild(mcpDivider);
    var mcpTitles = {
      mcp: "MCP servers",
      rsrc: "MCP resources",
      pmpt: "MCP prompts",
    };
    var mcpMetrics = [
      { value: overview.mcp_servers, label: "mcp" },
      { value: overview.mcp_resources, label: "rsrc" },
      { value: overview.mcp_prompts, label: "pmpt" },
    ];
    mcpMetrics.forEach(function (m) {
      var el = document.createElement("span");
      el.className = "csb-metric";
      el.title = mcpTitles[m.label] || "";
      if (m.label === "mcp") {
        var dot = document.createElement("span");
        dot.className = "csb-mcp-dot";
        dot.setAttribute("aria-hidden", "true");
        el.appendChild(dot);
      }
      var valSpan = document.createElement("span");
      valSpan.className = "csb-metric-value";
      valSpan.textContent = formatCount(m.value);
      var labelSpan = document.createElement("span");
      labelSpan.className = "csb-metric-label";
      labelSpan.textContent = m.label;
      el.appendChild(valSpan);
      el.appendChild(labelSpan);
      metricsContainer.appendChild(el);
    });
  }
}

// --- Node Grouping ---
function extractNodePrefix(nodeId) {
  var stripped = nodeId.replace(/[-_][a-z0-9]*\d[a-z0-9]*$/i, "");
  if (!stripped || stripped === nodeId) {
    stripped = nodeId.replace(/[-_]?\d+$/, "");
  }
  // Clean trailing separators (e.g., FQDN-style "node.prod.01" → "node.prod")
  stripped = stripped.replace(/[-_.]$/, "");
  return stripped || nodeId;
}

function groupNodes(nodes) {
  var groupMap = {};
  var groupOrder = [];
  nodes.forEach(function (node) {
    var prefix = extractNodePrefix(node.node_id);
    if (!groupMap[prefix]) {
      groupMap[prefix] = {
        prefix: prefix,
        nodes: [],
        ws_total: 0,
        ws_running: 0,
        ws_thinking: 0,
        ws_attention: 0,
        ws_error: 0,
        ws_idle: 0,
        total_tokens: 0,
        all_reachable: true,
        any_degraded: false,
        versions: new Set(),
      };
      groupOrder.push(prefix);
    }
    var g = groupMap[prefix];
    g.nodes.push(node);
    g.ws_total += node.ws_total || 0;
    g.ws_running += node.ws_running || 0;
    g.ws_thinking += node.ws_thinking || 0;
    g.ws_attention += node.ws_attention || 0;
    g.ws_error += node.ws_error || 0;
    g.ws_idle += node.ws_idle || 0;
    g.total_tokens += node.total_tokens || 0;
    if (!node.reachable) g.all_reachable = false;
    if (node.health && node.health.status === "degraded") g.any_degraded = true;
    var nodeVer = node.version || "";
    if (nodeVer) g.versions.add(nodeVer);
  });
  groupOrder.forEach(function (prefix) {
    groupMap[prefix].nodes.sort(function (a, b) {
      var d = b.ws_running + b.ws_attention - (a.ws_running + a.ws_attention);
      return d !== 0 ? d : a.node_id.localeCompare(b.node_id);
    });
  });
  var groups = groupOrder.map(function (p) {
    return groupMap[p];
  });
  groups.sort(function (a, b) {
    var aAct = a.ws_running + a.ws_attention;
    var bAct = b.ws_running + b.ws_attention;
    if (bAct !== aAct) return bAct - aAct;
    return a.prefix.localeCompare(b.prefix);
  });
  return groups;
}

function buildNodeRow(node) {
  var row = document.createElement("div");
  row.className = "node-row";
  if (node.ws_attention > 0) row.classList.add("has-attention");
  else if (node.ws_running > 0) row.classList.add("has-running");
  else if (node.ws_thinking > 0) row.classList.add("has-thinking");
  else if (node.ws_error > 0) row.classList.add("has-error");
  row.setAttribute("role", "button");
  row.setAttribute("tabindex", "0");
  row.setAttribute(
    "aria-label",
    node.node_id +
      ": " +
      node.ws_total +
      " workstreams, " +
      node.ws_running +
      " running, " +
      node.ws_attention +
      " attention, " +
      formatTokens(node.total_tokens) +
      " tokens" +
      (node.version ? ", version " + node.version : ""),
  );

  var isDegraded = node.health && node.health.status === "degraded";
  var dotClass = node.reachable
    ? isDegraded
      ? "node-dot degraded"
      : "node-dot"
    : "node-dot unreachable";
  var displayTokens = node.total_tokens || node.ws_tokens || 0;
  var maxWs = node.max_ws || 10;
  var healthPct =
    maxWs > 0 ? Math.min(Math.round((node.ws_total / maxWs) * 100), 100) : 0;
  var healthFillClass =
    healthPct < 50 ? "low" : healthPct < 80 ? "mid" : "high";
  var healthFillHtml =
    healthPct > 0
      ? '<span class="health-bar-fill ' +
        healthFillClass +
        '" style="width:' +
        healthPct +
        '%"></span>'
      : "";

  var healthTitle = "";
  if (node.health && node.health.backend) {
    healthTitle = "backend: " + node.health.backend.status;
  }
  var degradedBadge = isDegraded
    ? '<span class="node-degraded-badge" title="' +
      escapeHtml(healthTitle) +
      '" aria-label="' +
      escapeHtml(healthTitle) +
      '">degraded</span>'
    : "";

  row.innerHTML =
    '<span class="node-cell node-cell-name"' +
    (healthTitle ? ' title="' + escapeHtml(healthTitle) + '"' : "") +
    '><span class="' +
    dotClass +
    '"></span>' +
    escapeHtml(node.node_id) +
    degradedBadge +
    "</span>" +
    '<span class="node-cell node-cell-num' +
    (node.ws_total > 0 ? " has-value" : "") +
    '">' +
    node.ws_total +
    "</span>" +
    '<span class="node-cell node-cell-num' +
    (node.ws_running > 0 ? " has-value" : "") +
    '">' +
    node.ws_running +
    "</span>" +
    '<span class="node-cell node-cell-num' +
    (node.ws_attention > 0 ? " has-value" : "") +
    '">' +
    node.ws_attention +
    "</span>" +
    '<span class="node-cell node-cell-num">' +
    formatTokens(displayTokens) +
    "</span>" +
    '<span class="node-cell node-cell-version">' +
    escapeHtml(node.version || "") +
    "</span>" +
    '<span class="node-cell node-cell-health"><span class="health-bar">' +
    healthFillHtml +
    "</span> " +
    healthPct +
    "%</span>";

  row.onclick = function () {
    drillDownToNode(node.node_id, node.server_url);
  };
  row.onkeydown = function (e) {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      drillDownToNode(node.node_id, node.server_url);
    }
  };
  return row;
}

function toggleGroup(prefix) {
  expandedGroups[prefix] = !expandedGroups[prefix];
  var body = document.querySelector(
    '.node-group-body[data-prefix="' +
      prefix.replace(/\\/g, "\\\\").replace(/"/g, '\\"') +
      '"]',
  );
  if (!body) return;
  var isExpanded = expandedGroups[prefix];
  if (isExpanded) body.classList.remove("collapsed");
  else body.classList.add("collapsed");
  var groupEl = body.parentElement;
  if (groupEl) groupEl.setAttribute("aria-expanded", String(isExpanded));
  var chevron = groupEl ? groupEl.querySelector(".node-group-chevron") : null;
  if (chevron) {
    if (isExpanded) chevron.classList.add("expanded");
    else chevron.classList.remove("expanded");
  }
}

function renderNodeGroups(nodes, total) {
  var json = JSON.stringify(nodes);
  if (json === _lastNodesJson) return;
  _lastNodesJson = json;

  var table = document.getElementById("node-table");
  table.innerHTML = "";
  if (!nodes.length) {
    table.innerHTML = '<div class="dashboard-empty">No nodes discovered</div>';
    return;
  }

  var topHeaders = document.createElement("div");
  topHeaders.className = "node-colheaders";
  topHeaders.setAttribute("aria-hidden", "true");
  topHeaders.innerHTML =
    '<span class="ncol ncol-node">NODE</span>' +
    '<span class="ncol ncol-ws">WS</span>' +
    '<span class="ncol ncol-run">RUN</span>' +
    '<span class="ncol ncol-attn">ATTN</span>' +
    '<span class="ncol ncol-tokens">TOKENS</span>' +
    '<span class="ncol ncol-version">VER</span>' +
    '<span class="ncol ncol-health">LOAD</span>';
  table.appendChild(topHeaders);

  var groups = groupNodes(nodes);

  groups.forEach(function (group) {
    // Single-node group — render as plain row
    if (group.nodes.length === 1) {
      var wrapper = document.createElement("div");
      wrapper.className = "node-group node-group-single";
      wrapper.appendChild(buildNodeRow(group.nodes[0]));
      table.appendChild(wrapper);
      return;
    }

    var groupEl = document.createElement("div");
    groupEl.className = "node-group";
    var isExpanded = !!expandedGroups[group.prefix];
    groupEl.setAttribute("role", "listitem");
    groupEl.setAttribute("aria-expanded", String(isExpanded));

    // Group header
    var header = document.createElement("div");
    header.className = "node-group-header";
    if (group.ws_attention > 0) header.classList.add("has-attention");
    else if (group.ws_running > 0) header.classList.add("has-running");
    else if (group.ws_thinking > 0) header.classList.add("has-thinking");
    else if (group.ws_error > 0) header.classList.add("has-error");
    header.setAttribute("role", "button");
    header.setAttribute("tabindex", "0");
    header.setAttribute(
      "aria-label",
      group.prefix +
        " group: " +
        group.nodes.length +
        " nodes, " +
        group.ws_total +
        " workstreams, " +
        group.ws_running +
        " running, " +
        group.ws_attention +
        " attention, " +
        formatTokens(group.total_tokens) +
        " tokens" +
        (group.versions.size > 1 ? ", version drift detected" : ""),
    );

    var chevronClass = "node-group-chevron" + (isExpanded ? " expanded" : "");
    var totalMaxWs = 0;
    group.nodes.forEach(function (n) {
      totalMaxWs += n.max_ws || 10;
    });
    var healthPct =
      totalMaxWs > 0
        ? Math.min(Math.round((group.ws_total / totalMaxWs) * 100), 100)
        : 0;
    var healthFillClass =
      healthPct < 50 ? "low" : healthPct < 80 ? "mid" : "high";
    var healthFillHtml =
      healthPct > 0
        ? '<span class="health-bar-fill ' +
          healthFillClass +
          '" style="width:' +
          healthPct +
          '%"></span>'
        : "";

    var groupDegradedBadge = group.any_degraded
      ? '<span class="node-degraded-badge">degraded</span>'
      : "";
    var groupVersionText = "";
    var groupVersionDrift = false;
    if (group.versions.size === 1) {
      groupVersionText = Array.from(group.versions)[0];
    } else if (group.versions.size > 1) {
      groupVersionText = "mixed";
      groupVersionDrift = true;
    }
    var versionDriftBadge = groupVersionDrift
      ? '<span class="node-version-drift-badge">drift</span>'
      : "";

    header.innerHTML =
      '<span class="node-group-name">' +
      '<span class="' +
      chevronClass +
      '" aria-hidden="true">&#x25b8;</span>' +
      escapeHtml(group.prefix) +
      '<span class="node-group-badge">' +
      group.nodes.length +
      " nodes</span>" +
      groupDegradedBadge +
      "</span>" +
      '<span class="node-group-cell num' +
      (group.ws_total > 0 ? " has-value" : "") +
      '">' +
      group.ws_total +
      "</span>" +
      '<span class="node-group-cell num' +
      (group.ws_running > 0 ? " has-value" : "") +
      '">' +
      group.ws_running +
      "</span>" +
      '<span class="node-group-cell num' +
      (group.ws_attention > 0 ? " has-value" : "") +
      '">' +
      group.ws_attention +
      "</span>" +
      '<span class="node-group-cell num">' +
      formatTokens(group.total_tokens) +
      "</span>" +
      '<span class="node-group-cell node-cell-version' +
      (groupVersionDrift ? " drift" : "") +
      '">' +
      escapeHtml(groupVersionText) +
      versionDriftBadge +
      "</span>" +
      '<span class="node-group-cell node-cell-health"><span class="health-bar">' +
      healthFillHtml +
      "</span> " +
      healthPct +
      "%</span>";

    var prefix = group.prefix;
    header.onclick = function () {
      toggleGroup(prefix);
    };
    header.onkeydown = function (e) {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        toggleGroup(prefix);
      }
    };
    groupEl.appendChild(header);

    // Group body
    var body = document.createElement("div");
    body.className = "node-group-body" + (isExpanded ? "" : " collapsed");
    body.dataset.prefix = group.prefix;

    var colHeaders = document.createElement("div");
    colHeaders.className = "node-colheaders";
    colHeaders.setAttribute("aria-hidden", "true");
    colHeaders.innerHTML =
      '<span class="ncol ncol-node">NODE</span>' +
      '<span class="ncol ncol-ws">WS</span>' +
      '<span class="ncol ncol-run">RUN</span>' +
      '<span class="ncol ncol-attn">ATTN</span>' +
      '<span class="ncol ncol-tokens">TOKENS</span>' +
      '<span class="ncol ncol-version">VER</span>' +
      '<span class="ncol ncol-health">LOAD</span>';
    body.appendChild(colHeaders);

    group.nodes.forEach(function (node) {
      body.appendChild(buildNodeRow(node));
    });

    groupEl.appendChild(body);
    table.appendChild(groupEl);
  });
}

// --- Drill-down: Node ---
function drillDownToNode(nodeId, serverUrl) {
  currentView = "node";
  currentNodeId = nodeId;
  currentServerUrl = serverUrl || "";
  _setLandingView("node");
  var adminView = document.getElementById("view-admin");
  if (adminView) adminView.style.display = "none";
  var adminBtn = document.getElementById("admin-btn");
  if (adminBtn) {
    adminBtn.classList.remove("active");
    adminBtn.setAttribute("aria-expanded", "false");
  }
  document.getElementById("breadcrumb").style.display = "";
  document.getElementById("breadcrumb-label").textContent = nodeId;
  var link = document.getElementById("node-link");
  // Use proxy path so users don't need direct server access
  link.href = "/node/" + encodeURIComponent(nodeId) + "/";
  link.style.display = "";
  document.getElementById("main").scrollTop = 0;
  if (clusterState && clusterState.nodes[nodeId]) {
    renderFromState();
  } else {
    document.getElementById("node-ws-table").innerHTML =
      '<div class="dashboard-empty">Loading workstreams...</div>';
    loadNodeDetail(nodeId);
  }
  _loadNodeMetadataPanel(nodeId);
  document.getElementById("breadcrumb-home").focus();
  if (!_navigatingFromPopstate)
    history.pushState(
      { view: "node", nodeId: nodeId, serverUrl: serverUrl },
      "",
    );
}

function loadNodeDetail(nodeId) {
  authFetch("/v1/api/cluster/snapshot")
    .then(function (r) {
      return r.json();
    })
    .then(function (data) {
      applySnapshot(data);
      if (!clusterState || !clusterState.nodes[nodeId]) {
        document.getElementById("node-ws-table").innerHTML =
          '<div class="dashboard-empty">Node not found</div>';
      }
    })
    .catch(function () {
      document.getElementById("node-ws-table").innerHTML =
        '<div class="dashboard-empty">Failed to load</div>';
    });
}

// --- Drill-down: Filtered ---
function drillDownByState(state) {
  currentView = "filtered";
  currentFilter = { state: state, node: null, page: 1, per_page: 50 };
  _setLandingView("filtered");
  var adminView = document.getElementById("view-admin");
  if (adminView) adminView.style.display = "none";
  var adminBtn = document.getElementById("admin-btn");
  if (adminBtn) {
    adminBtn.classList.remove("active");
    adminBtn.setAttribute("aria-expanded", "false");
  }
  document.getElementById("breadcrumb").style.display = "";
  var sd = STATE_DISPLAY[state] || STATE_DISPLAY.idle;
  document.getElementById("breadcrumb-label").textContent =
    sd.symbol + " " + sd.label;
  document.getElementById("filtered-title").textContent =
    "WORKSTREAMS — " + sd.label.toUpperCase();
  document.getElementById("main").scrollTop = 0;
  if (clusterState) renderFromState();
  else loadFilteredWorkstreams();
  document.getElementById("breadcrumb-home").focus();
  if (!_navigatingFromPopstate)
    history.pushState({ view: "filtered", filter: currentFilter }, "");
}

function drillDownByNode(nodeId) {
  currentView = "filtered";
  currentFilter = { state: null, node: nodeId, page: 1, per_page: 50 };
  _setLandingView("filtered");
  var adminView = document.getElementById("view-admin");
  if (adminView) adminView.style.display = "none";
  var adminBtn = document.getElementById("admin-btn");
  if (adminBtn) {
    adminBtn.classList.remove("active");
    adminBtn.setAttribute("aria-expanded", "false");
  }
  document.getElementById("breadcrumb").style.display = "";
  document.getElementById("breadcrumb-label").textContent = nodeId;
  document.getElementById("filtered-title").textContent =
    "WORKSTREAMS — " + nodeId;
  document.getElementById("main").scrollTop = 0;
  if (clusterState) renderFromState();
  else loadFilteredWorkstreams();
  document.getElementById("breadcrumb-home").focus();
  if (!_navigatingFromPopstate)
    history.pushState({ view: "filtered", filter: currentFilter }, "");
}

function loadFilteredWorkstreams() {
  authFetch("/v1/api/cluster/snapshot")
    .then(function (r) {
      return r.json();
    })
    .then(function (data) {
      applySnapshot(data);
    })
    .catch(function () {
      document.getElementById("filtered-ws-table").innerHTML =
        '<div class="dashboard-empty">Failed to load</div>';
    });
}

function renderPagination(container, page, pages) {
  container.innerHTML = "";
  if (pages <= 1) return;
  var prev = document.createElement("button");
  prev.textContent = "\u25c4 Prev";
  prev.disabled = page <= 1;
  prev.onclick = function () {
    currentFilter.page--;
    if (clusterState) renderFromState();
    else loadFilteredWorkstreams();
  };
  container.appendChild(prev);
  var info = document.createElement("span");
  info.textContent = page + " / " + pages;
  container.appendChild(info);
  var next = document.createElement("button");
  next.textContent = "Next \u25ba";
  next.disabled = page >= pages;
  next.onclick = function () {
    currentFilter.page++;
    if (clusterState) renderFromState();
    else loadFilteredWorkstreams();
  };
  container.appendChild(next);
}

// --- Workstream table renderer (shared) ---
//
// Group rows by parent_ws_id so coordinator workstreams render with
// their spawned children nested beneath (tree grouping).  A
// coordinator row gets an expand/collapse caret; its children render as
// indented sub-rows when expanded.  Orphaned children (parent missing
// from the pool) fall through to the top level with an "orphan" badge.
//
// Expansion state persists in localStorage keyed by coordinator ws_id so
// the browser remembers the operator's preferred layout across reloads.
var _DASH_EXPAND_KEY_PREFIX = "coord-dashboard-expanded-";

function _isExpanded(coordWsId) {
  if (!coordWsId) return false;
  try {
    var v = localStorage.getItem(_DASH_EXPAND_KEY_PREFIX + coordWsId);
    return v === "1";
  } catch (_) {
    return false;
  }
}

function _setExpanded(coordWsId, expanded) {
  if (!coordWsId) return;
  try {
    localStorage.setItem(
      _DASH_EXPAND_KEY_PREFIX + coordWsId,
      expanded ? "1" : "0",
    );
  } catch (_) {
    /* storage quota / private mode — silently drop */
  }
}

function _bucketByParent(wsList) {
  var byId = {};
  wsList.forEach(function (ws) {
    if (ws.id) byId[ws.id] = ws;
  });
  var childrenMap = {};
  var roots = [];
  var orphans = [];
  wsList.forEach(function (ws) {
    var parent = ws.parent_ws_id || null;
    if (parent && byId[parent]) {
      (childrenMap[parent] = childrenMap[parent] || []).push(ws);
    } else if (parent) {
      orphans.push(ws);
    } else {
      roots.push(ws);
    }
  });
  return { roots: roots, childrenMap: childrenMap, orphans: orphans };
}

function renderWsTable(container, wsList) {
  container.replaceChildren();
  if (!wsList.length) {
    var empty = document.createElement("div");
    empty.className = "dashboard-empty";
    empty.textContent = "No workstreams";
    container.appendChild(empty);
    return;
  }
  var groups = _bucketByParent(wsList);

  function appendRow(ws, opts) {
    opts = opts || {};
    var row = _renderWsRow(ws, opts, container);
    container.appendChild(row);
    // Render children ALWAYS — expand/collapse is a CSS display toggle
    // on the child rows.  This lets the caret swap a class instead of
    // rebuilding the table, which preserves focus, avoids SR re-
    // announcement, and stays cheap regardless of row count.
    if (opts.childCount != null) {
      var kids = groups.childrenMap[ws.id] || [];
      kids.forEach(function (child) {
        var childRow = _renderWsRow(
          child,
          { isChild: true, parentWsId: ws.id, collapsed: !opts.expanded },
          container,
        );
        container.appendChild(childRow);
      });
    }
  }

  groups.roots.forEach(function (ws) {
    var kids = groups.childrenMap[ws.id] || [];
    var isCoord = ws.kind === "coordinator" || kids.length > 0;
    if (isCoord) {
      appendRow(ws, {
        isCoordinator: true,
        childCount: kids.length,
        expanded: _isExpanded(ws.id),
      });
    } else {
      appendRow(ws, {});
    }
  });
  groups.orphans.forEach(function (ws) {
    appendRow(ws, { isOrphan: true });
  });
}

function _renderWsRow(ws, opts, container) {
  opts = opts || {};
  var state = ws.state || "idle";
  var sd = STATE_DISPLAY[state] || STATE_DISPLAY.idle;

  var row = document.createElement("div");
  row.className = "dash-row";
  if (opts.isCoordinator) row.classList.add("dash-row--coordinator");
  if (opts.isChild) {
    row.classList.add("dash-row--child");
    if (opts.parentWsId) row.dataset.parentWsId = opts.parentWsId;
    // Child rows render in the collapsed state by default when the
    // parent coordinator was last left collapsed.  Caret toggle
    // flips this class in place — no table rebuild.
    if (opts.collapsed) row.classList.add("dash-row--collapsed");
  }
  if (opts.isOrphan) row.classList.add("dash-row--orphan");
  row.dataset.wsId = ws.id || "";
  row.dataset.state = state;
  row.setAttribute("tabindex", "0");
  row.setAttribute("role", "button");
  var ariaLabel = sd.label + ": " + (ws.name || ws.id || "unnamed");
  if (ws.model_alias || ws.model)
    ariaLabel += ", model: " + (ws.model_alias || ws.model);
  if (ws.node) ariaLabel += " on " + ws.node;
  if (ws.title) ariaLabel += ", task: " + ws.title;
  if (ws.tokens) ariaLabel += ", " + formatTokens(ws.tokens) + " tokens";
  if (ws.context_ratio > 0)
    ariaLabel += ", " + Math.round(ws.context_ratio * 100) + "% context";
  if (opts.isCoordinator && opts.childCount != null)
    ariaLabel += ", " + opts.childCount + " children";
  if (opts.isOrphan) ariaLabel += ", orphan";
  row.setAttribute("aria-label", ariaLabel);

  var main = document.createElement("div");
  main.className = "dash-row-main";

  // Expand / collapse caret — coordinator rows only.  The caret is a
  // button so it's keyboard-reachable; clicking it toggles without
  // bubbling to the row-level deep link handler.
  if (opts.isCoordinator && opts.childCount != null && opts.childCount > 0) {
    var caret = document.createElement("button");
    caret.type = "button";
    caret.className = "dash-caret";
    caret.setAttribute("aria-expanded", opts.expanded ? "true" : "false");
    // aria-controls intentionally omitted — children render as sibling
    // rows in the same flat list, not a nested container, so there's
    // no stable id to target.  aria-expanded alone is a valid SR
    // affordance per WAI-ARIA 1.1 when the controlled relationship
    // isn't strict (mirrors the admin sidebar carets elsewhere here).
    caret.setAttribute(
      "aria-label",
      (opts.expanded ? "Collapse" : "Expand") + " children",
    );
    caret.textContent = opts.expanded ? "\u25BE" : "\u25B8"; // ▾ / ▸
    caret.onclick = function (e) {
      e.stopPropagation();
      var coordWsId = ws.id;
      var nowExpanded = caret.getAttribute("aria-expanded") !== "true";
      _setExpanded(coordWsId, nowExpanded);
      // Toggle CSS class on child rows — no table rebuild, so focus
      // stays on the caret and screen readers don't re-announce the
      // list.  See .dash-row--collapsed in style.css for the hide
      // rule.
      caret.setAttribute("aria-expanded", nowExpanded ? "true" : "false");
      caret.setAttribute(
        "aria-label",
        (nowExpanded ? "Collapse" : "Expand") + " children",
      );
      caret.textContent = nowExpanded ? "\u25BE" : "\u25B8";
      row.dataset.expanded = nowExpanded ? "true" : "false";
      if (container) {
        var selector =
          '.dash-row--child[data-parent-ws-id="' + cssEscape(coordWsId) + '"]';
        var kids = container.querySelectorAll(selector);
        kids.forEach(function (k) {
          k.classList.toggle("dash-row--collapsed", !nowExpanded);
        });
      }
    };
    // Tag the parent row so CSS can key off expansion state
    // (e.g. hide the "(N children)" summary when expanded).
    row.dataset.expanded = opts.expanded ? "true" : "false";
    main.appendChild(caret);
  } else if (opts.isChild) {
    // Indentation placeholder so child rows align visually with their
    // parent's post-caret content.  Not a caret — nested coordinators
    // aren't supported in v1.
    var indent = document.createElement("span");
    indent.className = "dash-caret-placeholder";
    indent.setAttribute("aria-hidden", "true");
    main.appendChild(indent);
  }

  // STATE
  var stateCell = document.createElement("span");
  stateCell.className = "dash-cell-state";
  var dot = document.createElement("span");
  dot.className = "dash-state-dot";
  dot.dataset.state = state;
  dot.setAttribute("aria-hidden", "true");
  stateCell.appendChild(dot);
  var stateLabel = document.createElement("span");
  stateLabel.className = "dash-state-label";
  stateLabel.dataset.state = state;
  stateLabel.textContent = sd.symbol + " " + sd.label;
  stateCell.appendChild(stateLabel);
  main.appendChild(stateCell);

  // NAME (with optional child-count summary for collapsed coordinators)
  var nameCell = document.createElement("span");
  nameCell.className = "dash-cell-name";
  var nameText = ws.name || ws.title || ws.id || "";
  nameCell.textContent = nameText;
  if (opts.isCoordinator && opts.childCount != null && opts.childCount > 0) {
    // Render the "(N children)" summary only when there actually are
    // children — the home view feeds a coordinator-only pool into
    // renderWsTable so _bucketByParent sees no children and would
    // otherwise always print "(0 children)".  CSS hides it when the
    // row is expanded (see [data-expanded="true"] .dash-child-count
    // in style.css).
    var summary = document.createElement("span");
    summary.className = "dash-child-count";
    summary.textContent =
      " (" +
      opts.childCount +
      (opts.childCount === 1 ? " child)" : " children)");
    nameCell.appendChild(summary);
  }
  if (opts.isOrphan) {
    var orphanBadge = document.createElement("span");
    orphanBadge.className = "dash-orphan-badge";
    orphanBadge.textContent = " orphan";
    nameCell.appendChild(orphanBadge);
  }
  main.appendChild(nameCell);

  // MODEL
  var modelCell = document.createElement("span");
  modelCell.className = "dash-cell-model";
  modelCell.textContent = ws.model_alias || ws.model || "";
  if (ws.model) modelCell.title = ws.model;
  main.appendChild(modelCell);

  // NODE (clickable)
  var nodeCell = document.createElement("span");
  nodeCell.className = "dash-cell-node";
  nodeCell.textContent = ws.node || "";
  nodeCell.onclick = function (e) {
    e.stopPropagation();
    if (ws.node) drillDownByNode(ws.node);
  };
  main.appendChild(nodeCell);

  // TASK
  var taskCell = document.createElement("span");
  taskCell.className = "dash-cell-task";
  taskCell.textContent = ws.title || "";
  main.appendChild(taskCell);

  // TOKENS
  var tokensCell = document.createElement("span");
  tokensCell.className = "dash-cell-tokens";
  tokensCell.textContent = ws.tokens ? formatTokens(ws.tokens) : "";
  main.appendChild(tokensCell);

  // CTX
  var ctxCell = document.createElement("span");
  ctxCell.className = "dash-cell-ctx " + ctxClass(ws.context_ratio || 0);
  ctxCell.textContent =
    ws.context_ratio > 0 ? Math.round(ws.context_ratio * 100) + "%" : "";
  main.appendChild(ctxCell);

  row.appendChild(main);

  // Sub-line
  var sub = document.createElement("div");
  sub.className = "dash-row-sub";
  if (ws.activity_state === "approval") sub.classList.add("sub-attention");
  sub.textContent = ws.activity || "";
  row.appendChild(sub);

  // Deep link: click opens proxied server UI at this workstream.
  // Coordinator rows route to /coordinator/{ws_id}; node-backed
  // workstreams route to the proxied /node/{node_id}/?ws_id=X UI.
  var wsNodeId = ws.node;
  if (opts.isCoordinator || ws.kind === "coordinator") {
    row.classList.add("has-link");
    (function (wsId) {
      row.onclick = function () {
        if (wsId)
          window.location.href = "/coordinator/" + encodeURIComponent(wsId);
      };
      row.onkeydown = function (e) {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          row.onclick();
        }
      };
    })(ws.id);
  } else if (wsNodeId) {
    row.classList.add("has-link");
    (function (nodeId, wsId) {
      row.onclick = function () {
        window.location.href =
          "/node/" +
          encodeURIComponent(nodeId) +
          "/?ws_id=" +
          encodeURIComponent(wsId);
      };
      row.onkeydown = function (e) {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          row.onclick();
        }
      };
    })(wsNodeId, ws.id);
  } else {
    row.removeAttribute("role");
    row.removeAttribute("tabindex");
  }

  return row;
}

// --- Navigation ---
window.addEventListener("popstate", function (e) {
  var overlay = document.getElementById("login-overlay");
  if (overlay && overlay.style.display !== "none") return;
  _navigatingFromPopstate = true;
  try {
    if (!e.state) {
      showHome();
      return;
    }
    if (e.state.view === "home") showHome();
    else if (e.state.view === "overview") showOverview();
    else if (e.state.view === "admin" && typeof showAdmin === "function")
      showAdmin();
    else if (e.state.view === "node" && e.state.nodeId)
      drillDownToNode(e.state.nodeId, e.state.serverUrl);
    else if (e.state.view === "filtered" && e.state.filter) {
      currentFilter = e.state.filter;
      if (currentFilter.state) drillDownByState(currentFilter.state);
      else if (currentFilter.node) drillDownByNode(currentFilter.node);
    }
  } finally {
    _navigatingFromPopstate = false;
  }
});

// --- New Workstream Modal ---
var _newWsTrapHandler = null;

function showNewWsModal() {
  // Don't open if login overlay is active
  var login = document.getElementById("login-overlay");
  if (login && login.style.display !== "none") return;

  var overlay = document.getElementById("new-ws-overlay");
  overlay.style.display = "flex";
  document.body.style.overflow = "hidden";

  // Backdrop click to dismiss
  overlay.onclick = function (e) {
    if (e.target === overlay) hideNewWsModal();
  };

  var select = document.getElementById("new-ws-node");
  select.innerHTML =
    '<option value="">Auto (best node by capacity)</option>' +
    '<option value="pool">General pool (next available)</option>';
  authFetch("/v1/api/cluster/nodes?sort=activity&limit=100")
    .then(function (r) {
      return r.json();
    })
    .then(function (data) {
      (data.nodes || []).forEach(function (n) {
        if (!n.reachable) return;
        var opt = document.createElement("option");
        opt.value = n.node_id;
        opt.textContent =
          n.node_id +
          " (" +
          (n.ws_total || 0) +
          "/" +
          (n.max_ws || 10) +
          " ws)";
        select.appendChild(opt);
      });
    })
    .catch(function () {
      /* ignore — auto is always available */
    });
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
  // Populate model dropdown
  var modelSelect = document.getElementById("new-ws-model");
  var judgeSelect = document.getElementById("new-ws-judge");
  modelSelect.textContent = "";
  judgeSelect.textContent = "";

  var defaultOpt = document.createElement("option");
  defaultOpt.value = "";
  defaultOpt.textContent = "Default model";
  modelSelect.appendChild(defaultOpt);

  var defaultJudgeOpt = document.createElement("option");
  defaultJudgeOpt.value = "";
  defaultJudgeOpt.textContent = "Default (agent model)";
  judgeSelect.appendChild(defaultJudgeOpt);

  authFetch("/v1/api/models")
    .then(function (r) {
      return r.json();
    })
    .then(function (data) {
      (data.models || []).forEach(function (m) {
        var opt = document.createElement("option");
        opt.value = m.alias;
        opt.textContent =
          m.alias === m.model ? m.alias : m.alias + " (" + m.model + ")";
        modelSelect.appendChild(opt);

        var jOpt = document.createElement("option");
        jOpt.value = m.alias;
        jOpt.textContent = opt.textContent;
        judgeSelect.appendChild(jOpt);
      });
    })
    .catch(function () {
      /* ignore — default model still works */
    });
  document.getElementById("new-ws-name").value = "";
  modelSelect.value = "";
  judgeSelect.value = "";
  var taskEl = document.getElementById("new-ws-task");
  taskEl.value = "";
  var mod =
    navigator.platform && navigator.platform.indexOf("Mac") > -1
      ? "\u2318"
      : "Ctrl";
  taskEl.placeholder =
    "What should this workstream work on? (" + mod + "+Enter to create)";
  var errEl = document.getElementById("new-ws-error");
  errEl.style.display = "none";
  errEl.textContent = "";
  var btn = document.getElementById("new-ws-submit");
  btn.disabled = false;
  btn.textContent = "Create";

  // Focus trap (same pattern as login overlay)
  if (_newWsTrapHandler)
    document.removeEventListener("keydown", _newWsTrapHandler);
  _newWsTrapHandler = function (e) {
    if (e.key === "Escape") {
      e.preventDefault();
      hideNewWsModal();
      return;
    }
    if (e.key === "Tab") {
      var box = document.getElementById("new-ws-box");
      var focusable = box.querySelectorAll("select, input, textarea, button");
      var first = focusable[0];
      var last = focusable[focusable.length - 1];
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
  document.addEventListener("keydown", _newWsTrapHandler);

  setTimeout(function () {
    document.getElementById("new-ws-task").focus();
  }, 50);
}

function hideNewWsModal() {
  document.getElementById("new-ws-overlay").style.display = "none";
  document.body.style.overflow = "";
  if (_newWsTrapHandler) {
    document.removeEventListener("keydown", _newWsTrapHandler);
    _newWsTrapHandler = null;
  }
  var triggerBtn = document.getElementById("new-ws-btn");
  if (triggerBtn) triggerBtn.focus();
}

function submitNewWs() {
  var nodeId = document.getElementById("new-ws-node").value;
  var name = document.getElementById("new-ws-name").value.trim();
  var model = document.getElementById("new-ws-model").value.trim();
  var judgeModel = document.getElementById("new-ws-judge").value.trim();
  var skill = document.getElementById("new-ws-skill").value;
  var task = document.getElementById("new-ws-task").value.trim();
  var errEl = document.getElementById("new-ws-error");
  var btn = document.getElementById("new-ws-submit");

  btn.disabled = true;
  btn.textContent = "Creating\u2026";
  errEl.style.display = "none";

  var body = {};
  if (nodeId) body.node_id = nodeId;
  if (name) body.name = name;
  if (model) body.model = model;
  if (judgeModel) body.judge_model = judgeModel;
  if (task) body.initial_message = task;
  if (skill) body.skill = skill;

  authFetch("/v1/api/cluster/workstreams/new", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  })
    .then(function (r) {
      return r.json();
    })
    .then(function (data) {
      btn.disabled = false;
      btn.textContent = "Create";
      if (data.error) {
        errEl.textContent = data.error;
        errEl.style.display = "block";
        return;
      }
      hideNewWsModal();
      var label =
        data.target_node === "pool"
          ? "general pool"
          : data.target_node || "auto";
      showToast("Workstream created on " + label);
    })
    .catch(function () {
      btn.disabled = false;
      btn.textContent = "Create";
      errEl.textContent = "Request failed";
      errEl.style.display = "block";
    });
}

// Escape closes the new-ws modal; Enter submits
document.addEventListener("keydown", function (e) {
  var overlay = document.getElementById("new-ws-overlay");
  if (!overlay || overlay.style.display === "none") return;
  if (e.key === "Escape") {
    e.preventDefault();
    hideNewWsModal();
  }
  if (e.key === "Enter") {
    if (e.target.tagName === "SELECT") return;
    if (e.target.tagName === "BUTTON") return; // let native click fire
    if (e.target.tagName === "TEXTAREA" && !(e.ctrlKey || e.metaKey)) return;
    e.preventDefault();
    var btn = document.getElementById("new-ws-submit");
    if (btn && !btn.disabled) submitNewWs();
  }
});

// ---------------------------------------------------------------------------
// Coordinator session creation — used by the home-landing composer.
// Permission check lives in _hasCoordPermission (admin.coordinator);
// _createCoordinator does the POST + redirect.
// ---------------------------------------------------------------------------

function _hasCoordPermission() {
  var perms = sessionStorage.getItem("turnstone_permissions") || "";
  return perms.split(",").indexOf("admin.coordinator") !== -1;
}

// POST /v1/api/workstreams/new.  Accepts the three request fields
// directly + an errEl / setBusy callback so the caller owns the
// loading-state UX (button label swap, composer disabled flag, etc.).
// On success redirects to /coordinator/{ws_id}; on 503 invokes on503
// so the caller can surface the "subsystem not configured" banner.
function _createCoordinator(opts) {
  var name = (opts.name || "").trim();
  var skill = opts.skill || "";
  var task = (opts.task || "").trim();
  var errEl = opts.errEl;
  var setBusy = opts.setBusy || function () {};
  var on503 = opts.on503 || function () {};
  var onSuccess = opts.onSuccess || function () {};

  errEl.style.display = "none";
  errEl.textContent = "";
  setBusy(true);

  var body = {};
  if (name) body.name = name;
  if (skill) body.skill = skill;
  if (task) body.initial_message = task;

  authFetch("/v1/api/workstreams/new", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  })
    .then(function (r) {
      return r.json().then(function (data) {
        return { ok: r.ok, status: r.status, data: data };
      });
    })
    .then(function (res) {
      setBusy(false);
      if (res.status === 503) {
        on503(res);
        return;
      }
      if (!res.ok || !res.data || !res.data.ws_id) {
        errEl.textContent =
          (res.data && res.data.error) || "HTTP " + res.status;
        errEl.style.display = "block";
        return;
      }
      onSuccess(res);
      window.location.href =
        "/coordinator/" + encodeURIComponent(res.data.ws_id);
    })
    .catch(function () {
      setBusy(false);
      errEl.textContent = "Request failed";
      errEl.style.display = "block";
    });
}

// ---------------------------------------------------------------------------
// Home-landing composer + active-coordinators list + cluster summary.
// The composer renders as a persistent panel on the home view.  The
// active list and
// cluster summary render from clusterState, so every SSE-driven patch
// picks them up automatically via scheduleRender → renderFromState.
// ---------------------------------------------------------------------------

var _homeComposerInit = false;
var _homeCoordReady = null; // tri-state: null = unknown, true = ready, false = 503
var _homeCoordComposer = null; // shared Composer instance
var _homeCoordBusy = false;

// Single owner for sendBtn.disabled: disabled if EITHER busy OR the
// subsystem probe flipped to 503.  Every setter for _homeCoordBusy /
// _homeCoordReady ends with a call here so the two inputs can't drift
// out of sync (and a probe resolving mid-submit can't re-enable the
// button under an in-flight request).
function _refreshHomeCoordSubmitEnabled() {
  if (!_homeCoordComposer) return;
  _homeCoordComposer.sendBtn.disabled =
    _homeCoordBusy || _homeCoordReady === false;
}

function _ensureHomeComposerInit() {
  if (_homeComposerInit) return;
  _homeComposerInit = true;
  _mountHomeCoordComposer();
  _populateHomeSkillDropdown();
  _probeCoordSubsystem();
  _refreshHomeComposerVisibility();
}

function _mountHomeCoordComposer() {
  var mount = document.getElementById("home-coord-composer-mount");
  if (!mount || _homeCoordComposer) return;
  _homeCoordComposer = new Composer(mount, {
    layout: "stacked",
    rows: 3,
    placeholder: "What should this coordinator orchestrate?",
    ariaLabel: "Initial task",
    sendLabel: "Start",
    busyLabel: "Starting\u2026",
    // The submit button's disabled flag is owned by
    // _refreshHomeCoordSubmitEnabled, which combines busy state with
    // the subsystem-ready probe.  Tell the composer to skip writing
    // sendBtn.disabled so the reconciler has a single owner.
    externalDisable: true,
    // Ctrl/Cmd+Enter submit stays in the document keydown handler
    // below — it wants to also work when focus is outside the
    // composer (e.g. just after the admin banner dismisses).
    options: {
      storageKey: "turnstone.console.home_coord.options_open",
      summary: function (v) {
        var bits = [];
        if (v.name) bits.push(v.name);
        if (v.skill) bits.push(v.skill);
        return bits.join(" \u00b7 ");
      },
      fields: [
        {
          id: "name",
          label: "Name",
          type: "input",
          placeholder: "Auto-generated if empty",
          autocomplete: "off",
        },
        {
          id: "skill",
          label: "Skill",
          type: "select",
          choices: [{ value: "", text: "Use defaults" }],
        },
      ],
    },
    onSend: function (text) {
      submitHomeCoord(text);
    },
  });
}

function _populateHomeSkillDropdown() {
  if (!_homeCoordComposer) return;
  authFetch("/v1/api/skills")
    .then(function (r) {
      return r.ok ? r.json() : { skills: [] };
    })
    .then(function (data) {
      var choices = (data.skills || []).map(function (t) {
        return {
          value: t.name,
          text: t.is_default ? t.name + " (default)" : t.name,
        };
      });
      _homeCoordComposer.setOptionChoices("skill", choices);
    })
    .catch(function () {
      /* defaults still work even without the dropdown populated */
    });
}

// Probe GET /v1/api/workstreams — 200 = subsystem ready; 503 = no model
// alias resolvable, show remediation banner.  4xx (auth / permission) is
// treated as "unknown, don't flip the banner" because the probe cannot
// actually tell us anything about subsystem readiness in that case —
// the caller is expected to re-invoke this after login lands so a real
// answer can arrive.  Leaving the submit button enabled on unknown
// keeps first-paint usable; a subsequent 503 from the actual submit
// flips the banner via _createCoordinator's on503 hook.
//
// Skip the probe entirely for users without admin.coordinator — they
// can't see the composer anyway (see _refreshHomeComposerVisibility),
// and the endpoint returns 403 for them, producing a useless network
// round-trip on every login.
function _probeCoordSubsystem() {
  if (!_hasCoordPermission()) return;
  authFetch("/v1/api/workstreams")
    .then(function (r) {
      if (r.status === 503) {
        _homeCoordReady = false;
      } else if (r.ok) {
        _homeCoordReady = true;
      } else {
        _homeCoordReady = null;
        return;
      }
      var banner = document.getElementById("coord-composer-503");
      if (banner) banner.style.display = _homeCoordReady ? "none" : "";
      _refreshHomeCoordSubmitEnabled();
    })
    .catch(function () {
      /* network error — leave banner hidden; submit will surface a retryable error */
    });
}

function _refreshHomeComposerVisibility() {
  var panel = document.getElementById("coord-composer-panel");
  if (!panel) return;
  panel.style.display = _hasCoordPermission() ? "" : "none";
}

function submitHomeCoord(textFromComposer) {
  if (!_hasCoordPermission()) {
    showToast("admin.coordinator permission required");
    return;
  }
  if (!_homeCoordComposer) return;
  // text arg is passed when the Composer's Enter-key handler fires;
  // direct callers (Ctrl/Cmd+Enter) invoke with no argument and we
  // read from the composer.
  var task =
    textFromComposer != null ? textFromComposer : _homeCoordComposer.value;
  var opts = _homeCoordComposer.getOptionValues();
  _createCoordinator({
    name: opts.name || "",
    skill: opts.skill || "",
    task: task,
    errEl: document.getElementById("home-coord-error"),
    setBusy: function (b) {
      _homeCoordBusy = b;
      if (_homeCoordComposer) _homeCoordComposer.setBusy(b);
      _refreshHomeCoordSubmitEnabled();
    },
    on503: function () {
      _homeCoordReady = false;
      var banner = document.getElementById("coord-composer-503");
      if (banner) banner.style.display = "";
      _refreshHomeCoordSubmitEnabled();
    },
  });
}

// Ctrl/Cmd+Enter anywhere on the home page submits the coordinator
// composer — consistent with the modal's keyboard-shortcut convention.
// The Composer's own Enter handler already fires submitHomeCoord when
// focus is in the textarea; this covers the case when focus sits on
// the attach / options buttons.
document.addEventListener("keydown", function (e) {
  if (e.key !== "Enter" || !(e.ctrlKey || e.metaKey)) return;
  if (!_homeCoordComposer) return;
  var mount = document.getElementById("home-coord-composer-mount");
  if (!mount || !mount.contains(e.target)) return;
  e.preventDefault();
  // sendBtn.disabled is the single reconciler of busy + 503-ready —
  // checking it here is enough to avoid double-submits or submits
  // while the subsystem is down.
  if (!_homeCoordComposer.sendBtn.disabled) submitHomeCoord();
});

// Fingerprints of the last home-view render — skip DOM rebuilds when
// nothing visible in either region has changed.  renderFromState fires
// on every SSE patch (state_change, ws_created, ws_closed, cluster
// aggregate update...) and most of those don't affect the coord list or
// the summary line — short-circuiting here avoids a replaceChildren +
// tree-group rebuild on every activity tick.
var _homeCoordsFingerprint = "";
var _homeSummaryFingerprint = "";

// Active-coordinators list is SSE-driven — the console collector
// registers a "console" pseudo-node and the coordinator manager fans
// out ws_created / ws_closed / cluster_state / ws_rename events when
// coordinators come, go, or change state.  The browser's
// patchClusterState handler routes those events into
// clusterState.nodes["console"].workstreams, so every home-view render
// reads a live mirror without polling.

function _activeCoordsFromClusterState() {
  if (!clusterState) return [];
  var node = clusterState.nodes && clusterState.nodes["console"];
  if (!node) return [];
  return (node.workstreams || []).filter(function (ws) {
    return ws && ws.kind === "coordinator";
  });
}

function _renderHomeView() {
  // Active coordinators are sourced live from clusterState.nodes["console"]
  // — the coordinator manager fans out ws_created / ws_closed /
  // cluster_state via the collector's pseudo-node so the home view
  // stays in sync without polling.
  var coords = _activeCoordsFromClusterState();
  coords.sort(function (a, b) {
    // Most-recently-active first.  updated is absent on freshly-created
    // rows; fall back to id so the ordering is stable either way.
    var au = a.updated || 0,
      bu = b.updated || 0;
    if (au !== bu) return bu - au;
    return (a.id || "").localeCompare(b.id || "");
  });

  // Fingerprint: every field _renderWsRow actually consumes, so a
  // cluster_state tick that only bumps tokens / context_ratio /
  // activity (patchClusterState mutates those without touching
  // ws.updated) doesn't leave the TOKENS / CTX / activity cells
  // frozen.  Bucketing tokens by hundreds keeps the fingerprint
  // stable enough that unrelated sub-hundred drift doesn't trigger
  // a full rebuild on every SSE tick; the rendered value still
  // re-renders when the bucket changes.
  var coordsFp = coords.length + "|";
  for (var i = 0; i < coords.length; i++) {
    var c = coords[i];
    coordsFp +=
      (c.id || "") +
      ":" +
      (c.state || "") +
      ":" +
      (c.updated || 0) +
      ":" +
      (c.name || "") +
      ":" +
      Math.floor((c.tokens || 0) / 100) +
      ":" +
      Math.round((c.context_ratio || 0) * 100) +
      ":" +
      (c.activity_state || "") +
      ":" +
      (c.model_alias || c.model || "") +
      ":" +
      (c.node || "") +
      ":" +
      (c.title || "") +
      ";";
  }
  if (coordsFp !== _homeCoordsFingerprint) {
    _homeCoordsFingerprint = coordsFp;
    var countEl = document.getElementById("active-coord-count");
    if (countEl) {
      countEl.textContent = coords.length ? "(" + coords.length + ")" : "";
    }
    var listEl = document.getElementById("active-coord-list");
    if (listEl) {
      listEl.replaceChildren();
      if (!coords.length) {
        var empty = document.createElement("div");
        empty.className = "dashboard-empty";
        empty.textContent = "No active coordinator sessions. Start one above.";
        listEl.appendChild(empty);
      } else {
        // Reuse the shared tree-grouped renderer so coordinators on
        // the landing page get the same glyphs, child-count badges,
        // and row treatment as the legacy dashboard.
        renderWsTable(listEl, coords);
      }
    }
  }

  // Cluster summary — one-line aggregate.  Mirrors the existing
  // #cluster-summary header span (kept unchanged for deep-link callers)
  // but with state counts inlined so operators don't have to expand
  // to see the cluster's posture.
  //
  // The #cluster-summary header element is ALSO written by the overview
  // branch of renderFromState with a different format ("1 nodes" vs
  // "1 node").  Always rewrite it here so navigating overview → home
  // doesn't leave the stale overview text behind when the cluster
  // aggregate fingerprint hasn't actually changed.
  var ovr = clusterState.overview || {};
  var states = ovr.states || {};
  var aggTokens = (ovr.aggregate || {}).total_tokens || 0;
  var headerSum = document.getElementById("cluster-summary");
  if (headerSum) {
    headerSum.textContent =
      (ovr.nodes || 0) +
      " node" +
      ((ovr.nodes || 0) === 1 ? "" : "s") +
      " \u00b7 " +
      formatCount(ovr.workstreams || 0) +
      " workstreams";
  }

  var summaryFp =
    (ovr.nodes || 0) +
    "|" +
    (ovr.workstreams || 0) +
    "|" +
    (states.running || 0) +
    "|" +
    (states.attention || 0) +
    "|" +
    (states.error || 0) +
    "|" +
    aggTokens;
  if (summaryFp === _homeSummaryFingerprint) return;
  _homeSummaryFingerprint = summaryFp;

  var parts = [
    (ovr.nodes || 0) + " node" + ((ovr.nodes || 0) === 1 ? "" : "s"),
    formatCount(ovr.workstreams || 0) + " workstreams",
  ];
  if (states.running) parts.push(states.running + " running");
  if (states.attention) parts.push(states.attention + " attention");
  if (states.error) parts.push(states.error + " error");
  if (aggTokens) parts.push(formatTokens(aggTokens) + " tokens");
  var summaryText = parts.join(" \u00b7 ");
  var line = document.getElementById("cluster-summary-line");
  if (line) line.textContent = summaryText;
}

// ---------------------------------------------------------------------------
// Saved coordinators — closed sessions persisted on disk.  Mirrors the
// interactive UI's "Saved Workstreams" card grid (same /shared/cards.css
// primitives, same /shared/cards.js renderSessionCard helper, same
// response item shape from /v1/api/workstreams/saved).  Click a card →
// POST /open then /coordinator/{ws_id}; the lifted detail factory
// lazily rehydrates from storage on the GET miss.
// ---------------------------------------------------------------------------

// In-flight de-dup for loadSavedCoordinators.  ws_closed events can
// arrive in bursts on a busy cluster; without this guard each one
// triggers a parallel fetch.  Single boolean is enough because the
// renderer reads from the latest response — a coalesced re-fetch right
// after the in-flight one resolves catches any state change.
var _savedCoordsInFlight = false;
var _savedCoordsRetry = false;

function loadSavedCoordinators() {
  if (!_hasCoordPermission()) return;
  if (_savedCoordsInFlight) {
    _savedCoordsRetry = true;
    return;
  }
  _savedCoordsInFlight = true;
  authFetch("/v1/api/workstreams/saved")
    .then(function (r) {
      return r.ok ? r.json() : { workstreams: [] };
    })
    .then(function (data) {
      renderSavedCoordinators(data.workstreams || []);
    })
    .catch(function () {
      /* silent — saved list is informational, not load-bearing */
    })
    .finally(function () {
      _savedCoordsInFlight = false;
      // If at least one call arrived while we were in flight, fire one
      // catch-up fetch (not N) so the UI reflects the latest state
      // without a per-event fan-out.
      if (_savedCoordsRetry) {
        _savedCoordsRetry = false;
        loadSavedCoordinators();
      }
    });
}

function renderSavedCoordinators(items) {
  var section = document.getElementById("saved-coordinators");
  var cards = document.getElementById("saved-coord-cards");
  var countEl = document.getElementById("saved-coord-count");
  if (!section || !cards) return;
  if (!items.length) {
    section.style.display = "none";
    cards.replaceChildren();
    if (countEl) countEl.textContent = "";
    return;
  }
  section.style.display = "";
  if (countEl) countEl.textContent = "(" + items.length + ")";
  cards.replaceChildren();
  items.forEach(function (sess) {
    var card = renderSessionCard(sess, {
      ariaLabel: function (s) {
        return (
          "Resume coordinator: " + (s.alias || s.title || s.name || s.ws_id)
        );
      },
      onActivate: function (s, cardEl) {
        // POST /open BEFORE navigating so capacity issues surface as a
        // toast instead of a broken-looking detail page.  The /open
        // endpoint calls the same lazy-rehydrate path the GET would,
        // but we get the status code synchronously so the user learns
        // "all slots in use" instead of staring at a 404.
        cardEl.classList.add("is-busy");
        authFetch(
          "/v1/api/workstreams/" + encodeURIComponent(s.ws_id) + "/open",
          { method: "POST" },
        )
          .then(function (r) {
            if (r.ok) {
              window.location.href =
                "/coordinator/" + encodeURIComponent(s.ws_id);
              return;
            }
            cardEl.classList.remove("is-busy");
            if (r.status === 429) {
              showToast(
                "All coordinator slots are active — close one first to restore this session",
              );
            } else if (r.status === 404) {
              showToast("Coordinator no longer available");
              loadSavedCoordinators();
            } else if (r.status === 503) {
              showToast("Coordinator subsystem not configured");
            } else {
              showToast("Failed to restore coordinator (" + r.status + ")");
            }
          })
          .catch(function () {
            cardEl.classList.remove("is-busy");
            showToast("Failed to restore coordinator");
          });
      },
    });
    cards.appendChild(card);
  });
}

// --- Init ---
// SSE connects after auth is confirmed — either via onLoginSuccess after
// login, or after the first successful data load (page refresh with valid cookie).
var _sseStarted = false;
function _ensureSSE() {
  if (!_sseStarted) {
    _sseStarted = true;
    connectSSE();
  }
}
history.replaceState({ view: "home" }, "");
initLogin();
// loadOverview fetches the cluster snapshot — both the cluster-summary
// aggregates AND the active-coordinators list come from the same
// snapshot + SSE patch pipeline (#9); the console pseudo-node carries
// coordinator ws_created / ws_closed / cluster_state events.
loadOverview();
_ensureHomeComposerInit();
// Refresh the coord button visibility once auth.js has populated
// sessionStorage from the initial whoami.  window.permissionsReady
// resolves after that completes (success or failure); fall back to a
// short timeout if the promise isn't available (older auth.js).
//
// NOTE: permissionsReady is one-shot — it fires exactly once per page
// load (see auth.js).  Subsequent re-logins are caught by the
// onLoginSuccess hook above which calls loadSavedCoordinators() again.
if (
  window.permissionsReady &&
  typeof window.permissionsReady.then === "function"
) {
  window.permissionsReady.then(function () {
    _refreshHomeComposerVisibility();
    loadSavedCoordinators();
  });
} else {
  setTimeout(function () {
    _refreshHomeComposerVisibility();
    loadSavedCoordinators();
  }, 500);
}

// --- Node Metadata Panel (read-only in node detail view) ---
function _loadNodeMetadataPanel(nodeId) {
  var section = document.getElementById("node-metadata-section");
  var table = document.getElementById("node-metadata-table");
  if (!section || !table) return;
  section.style.display = "none";
  table.textContent = "";
  authFetch("/v1/api/cluster/node/" + encodeURIComponent(nodeId))
    .then(function (r) {
      return r.ok ? r.json() : null;
    })
    .then(function (data) {
      if (!data || !data.metadata || !data.metadata.length) return;
      section.style.display = "";
      var tbl = document.createElement("table");
      tbl.className = "nm-table";
      var thead = document.createElement("thead");
      var hr = document.createElement("tr");
      ["Key", "Value", "Source"].forEach(function (h) {
        var th = document.createElement("th");
        th.setAttribute("scope", "col");
        th.textContent = h;
        hr.appendChild(th);
      });
      thead.appendChild(hr);
      tbl.appendChild(thead);
      var tbody = document.createElement("tbody");
      data.metadata.forEach(function (m) {
        var tr = document.createElement("tr");
        var tdKey = document.createElement("td");
        tdKey.className = "nm-key";
        tdKey.textContent = m.key;
        tr.appendChild(tdKey);
        var tdVal = document.createElement("td");
        tdVal.className = "nm-val";
        tdVal.textContent =
          typeof m.value === "object"
            ? JSON.stringify(m.value)
            : String(m.value);
        tdVal.title = tdVal.textContent;
        tr.appendChild(tdVal);
        var tdSrc = document.createElement("td");
        var badge = document.createElement("span");
        badge.className = "nm-source-badge nm-source-" + m.source;
        badge.textContent = m.source;
        tdSrc.appendChild(badge);
        tr.appendChild(tdSrc);
        tbody.appendChild(tr);
      });
      tbl.appendChild(tbody);
      table.appendChild(tbl);
    })
    .catch(function () {
      /* silent — metadata is supplementary */
    });
}
