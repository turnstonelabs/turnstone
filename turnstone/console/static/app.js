// --- Shared hooks ---
window.onLoginSuccess = function () {
  connectSSE();
};
window.onLogout = function () {
  if (evtSource) {
    evtSource.close();
    evtSource = null;
  }
};
window.onThemeChange = function (next) {
  var btn = document.getElementById("theme-toggle");
  if (btn) btn.textContent = next === "light" ? "\u2600" : "\u263E";
};
// Set initial theme button text
(function () {
  var btn = document.getElementById("theme-toggle");
  if (btn)
    btn.textContent =
      document.documentElement.dataset.theme === "light" ? "\u2600" : "\u263E";
})();

// --- State ---
var currentView = "overview"; // "overview" | "node" | "filtered"
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
      });
    }
  } else if (t === "ws_closed") {
    Object.keys(clusterState.nodes).forEach(function (nid) {
      var n = clusterState.nodes[nid];
      n.workstreams = (n.workstreams || []).filter(function (ws) {
        return ws.id !== data.ws_id;
      });
    });
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
  clusterState.overview = {
    nodes: Object.keys(clusterState.nodes).length,
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
    health: node.health || {},
    version: node.version || "",
  };
}

function renderFromState() {
  if (!clusterState) return;
  renderStatusBar(clusterState.overview);
  if (currentView === "overview") {
    var nodesList = Object.keys(clusterState.nodes).map(function (nid) {
      return buildNodeInfoFromSnapshot(clusterState.nodes[nid]);
    });
    nodesList.sort(function (a, b) {
      var d = b.ws_running + b.ws_attention - (a.ws_running + a.ws_attention);
      return d !== 0 ? d : a.node_id.localeCompare(b.node_id);
    });
    renderNodeGroups(nodesList, nodesList.length);
    document.getElementById("cluster-summary").textContent =
      clusterState.overview.nodes +
      " nodes \u00b7 " +
      formatCount(clusterState.overview.workstreams) +
      " workstreams";
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

// --- Overview View ---
function showOverview() {
  currentView = "overview";
  currentNodeId = null;
  currentServerUrl = "";
  currentFilter = { state: null, node: null, page: 1, per_page: 50 };
  document.getElementById("view-overview").style.display = "";
  document.getElementById("view-node").style.display = "none";
  document.getElementById("view-filtered").style.display = "none";
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
  if (!_navigatingFromPopstate) history.pushState({ view: "overview" }, "");
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

  var circuitTitle = "";
  if (node.health && node.health.backend) {
    circuitTitle =
      "backend: " +
      node.health.backend.status +
      ", circuit: " +
      node.health.backend.circuit_state;
  }
  var degradedBadge = isDegraded
    ? '<span class="node-degraded-badge" title="' +
      escapeHtml(circuitTitle) +
      '" aria-label="' +
      escapeHtml(circuitTitle) +
      '">degraded</span>'
    : "";

  row.innerHTML =
    '<span class="node-cell node-cell-name"' +
    (circuitTitle ? ' title="' + escapeHtml(circuitTitle) + '"' : "") +
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
    '.node-group-body[data-prefix="' + prefix.replace(/"/g, '\\"') + '"]',
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
  document.getElementById("view-overview").style.display = "none";
  document.getElementById("view-node").style.display = "";
  document.getElementById("view-filtered").style.display = "none";
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
  document.getElementById("view-overview").style.display = "none";
  document.getElementById("view-node").style.display = "none";
  document.getElementById("view-filtered").style.display = "";
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
  document.getElementById("view-overview").style.display = "none";
  document.getElementById("view-node").style.display = "none";
  document.getElementById("view-filtered").style.display = "";
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
function renderWsTable(container, wsList) {
  container.innerHTML = "";
  if (!wsList.length) {
    container.innerHTML = '<div class="dashboard-empty">No workstreams</div>';
    return;
  }
  wsList.forEach(function (ws) {
    var state = ws.state || "idle";
    var sd = STATE_DISPLAY[state] || STATE_DISPLAY.idle;

    var row = document.createElement("div");
    row.className = "dash-row";
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
    row.setAttribute("aria-label", ariaLabel);

    var main = document.createElement("div");
    main.className = "dash-row-main";

    // STATE
    var stateCell = document.createElement("span");
    stateCell.className = "dash-cell-state";
    stateCell.innerHTML =
      '<span class="dash-state-dot" data-state="' +
      escapeHtml(state) +
      '" aria-hidden="true"></span>' +
      '<span class="dash-state-label" data-state="' +
      escapeHtml(state) +
      '">' +
      sd.symbol +
      " " +
      sd.label +
      "</span>";
    main.appendChild(stateCell);

    // NAME
    var nameCell = document.createElement("span");
    nameCell.className = "dash-cell-name";
    nameCell.textContent = ws.name || ws.id || "";
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

    // Deep link: click opens proxied server UI at this workstream
    var wsNodeId = ws.node;
    if (wsNodeId) {
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

    container.appendChild(row);
  });
}

// --- Navigation ---
window.addEventListener("popstate", function (e) {
  var overlay = document.getElementById("login-overlay");
  if (overlay && overlay.style.display !== "none") return;
  _navigatingFromPopstate = true;
  try {
    if (!e.state) {
      showOverview();
      return;
    }
    if (e.state.view === "overview") showOverview();
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
  modelSelect.textContent = "";
  var defaultOpt = document.createElement("option");
  defaultOpt.value = "";
  defaultOpt.textContent = "Default model";
  modelSelect.appendChild(defaultOpt);
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
      });
    })
    .catch(function () {
      /* ignore — default model still works */
    });
  document.getElementById("new-ws-name").value = "";
  modelSelect.value = "";
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
history.replaceState({ view: "overview" }, "");
initLogin();
loadOverview();
