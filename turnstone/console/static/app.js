// --- Theme ---
function toggleTheme() {
  var next = document.documentElement.dataset.theme === "light" ? "" : "light";
  document.documentElement.dataset.theme = next;
  localStorage.setItem("turnstone-theme", next || "dark");
  var btn = document.getElementById("theme-toggle");
  if (btn) btn.textContent = next === "light" ? "\u2600" : "\u263E";
}
(function initTheme() {
  var stored = localStorage.getItem("turnstone-theme");
  if (stored === "light") {
    document.documentElement.dataset.theme = "light";
  } else if (
    !stored &&
    window.matchMedia &&
    window.matchMedia("(prefers-color-scheme: light)").matches
  ) {
    document.documentElement.dataset.theme = "light";
  }
  var btn = document.getElementById("theme-toggle");
  if (btn)
    btn.textContent =
      document.documentElement.dataset.theme === "light" ? "\u2600" : "\u263E";
})();

/* Auth-aware fetch — shows login overlay on 401 */
function authFetch(url, opts) {
  return fetch(url, opts).then(function (r) {
    if (r.status === 401) {
      showLogin();
      throw new Error("auth");
    }
    return r;
  });
}

// --- State ---
var currentView = "overview"; // "overview" | "node" | "filtered"
var currentNodeId = null;
var currentFilter = { state: null, node: null, page: 1, per_page: 50 };
var expandedGroups = {};
var _lastOverviewJson = "";
var _lastNodesJson = "";
var evtSource = null;
var retryDelay = 1000;

// --- Constants ---
var STATE_DISPLAY = {
  running: { symbol: "\u25b8", label: "run" },
  thinking: { symbol: "\u25cc", label: "think" },
  attention: { symbol: "\u25c6", label: "attn" },
  idle: { symbol: "\u00b7", label: "idle" },
  error: { symbol: "\u2716", label: "err" },
};
var STATE_ORDER = ["running", "thinking", "attention", "error", "idle"];

// --- Helpers ---
function escapeHtml(s) {
  var el = document.createElement("span");
  el.textContent = s;
  return el.innerHTML;
}
function formatTokens(n) {
  if (n >= 1000000) return (n / 1000000).toFixed(1) + "M";
  if (n >= 1000) return (n / 1000).toFixed(1) + "k";
  return String(n || 0);
}
function ctxClass(ratio) {
  if (ratio <= 0) return "ctx-idle";
  var pct = ratio * 100;
  if (pct < 30) return "ctx-low";
  if (pct < 50) return "ctx-mid";
  if (pct < 80) return "ctx-high";
  return "ctx-danger";
}
function formatUptime(seconds) {
  if (!seconds) return "";
  if (seconds < 60) return seconds + "s";
  var min = Math.floor(seconds / 60);
  if (min < 60) return min + "m";
  var hr = Math.floor(min / 60);
  return hr + "h " + (min % 60) + "m";
}
function formatCount(n) {
  if (n >= 1000) return (n / 1000).toFixed(1) + "k";
  return String(n);
}

// --- SSE Connection ---
function connectSSE() {
  if (evtSource) {
    evtSource.close();
    evtSource = null;
  }
  evtSource = new EventSource("/api/cluster/events");
  var statusBar = document.getElementById("status-bar");
  evtSource.onmessage = function (e) {
    retryDelay = 1000;
    statusBar.classList.remove("disconnected");
    statusBar.textContent = "";
    var csb = document.getElementById("cluster-status-bar");
    if (csb) csb.classList.remove("stale");
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
    statusBar.textContent = "Reconnecting\u2026";
    statusBar.classList.add("disconnected");
    var csb = document.getElementById("cluster-status-bar");
    if (csb) csb.classList.add("stale");
    // Raw fetch (not authFetch) — need to inspect status before throwing
    fetch("/api/cluster/overview")
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

var _refreshTimer = null;
function scheduleRefresh() {
  if (_refreshTimer) return;
  _refreshTimer = setTimeout(function () {
    _refreshTimer = null;
    if (currentView === "overview") loadOverview();
    else if (currentView === "node" && currentNodeId)
      loadNodeDetail(currentNodeId);
    else if (currentView === "filtered") loadFilteredWorkstreams();
  }, 250);
}

function handleClusterEvent(data) {
  if (
    data.type === "cluster_state" ||
    data.type === "ws_created" ||
    data.type === "ws_closed" ||
    data.type === "ws_rename" ||
    data.type === "node_joined" ||
    data.type === "node_lost"
  ) {
    scheduleRefresh();
  }
}

// --- Overview View ---
function showOverview() {
  currentView = "overview";
  currentNodeId = null;
  currentFilter = { state: null, node: null, page: 1, per_page: 50 };
  document.getElementById("view-overview").style.display = "";
  document.getElementById("view-node").style.display = "none";
  document.getElementById("view-filtered").style.display = "none";
  document.getElementById("breadcrumb").style.display = "none";
  document.getElementById("main").scrollTop = 0;
  loadOverview();
  history.pushState({ view: "overview" }, "");
}

function loadOverview() {
  var overviewP = authFetch("/api/cluster/overview").then(function (r) {
    return r.json();
  });
  var nodesP = authFetch("/api/cluster/nodes?sort=activity&limit=1000").then(
    function (r) {
      return r.json();
    },
  );
  Promise.all([overviewP, nodesP])
    .then(function (res) {
      renderStatusBar(res[0]);
      renderNodeGroups(res[1].nodes, res[1].total);
      document.getElementById("cluster-summary").textContent =
        res[0].nodes +
        " nodes \u00b7 " +
        formatCount(res[0].workstreams) +
        " workstreams";
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
  });
  groupOrder.forEach(function (prefix) {
    groupMap[prefix].nodes.sort(function (a, b) {
      return b.ws_running + b.ws_attention - (a.ws_running + a.ws_attention);
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
      " tokens",
  );

  var dotClass = node.reachable ? "node-dot" : "node-dot unreachable";
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

  row.innerHTML =
    '<span class="node-cell node-cell-name"><span class="' +
    dotClass +
    '"></span>' +
    escapeHtml(node.node_id) +
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
        " tokens",
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

    header.innerHTML =
      '<span class="node-group-name">' +
      '<span class="' +
      chevronClass +
      '" aria-hidden="true">&#x25b8;</span>' +
      escapeHtml(group.prefix) +
      '<span class="node-group-badge">' +
      group.nodes.length +
      " nodes</span>" +
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
  document.getElementById("view-overview").style.display = "none";
  document.getElementById("view-node").style.display = "";
  document.getElementById("view-filtered").style.display = "none";
  document.getElementById("breadcrumb").style.display = "";
  document.getElementById("breadcrumb-label").textContent = nodeId;
  if (serverUrl) {
    var link = document.getElementById("node-link");
    link.href = serverUrl;
    link.style.display = "";
  }
  document.getElementById("main").scrollTop = 0;
  document.getElementById("node-ws-table").innerHTML =
    '<div class="dashboard-empty">Loading workstreams...</div>';
  loadNodeDetail(nodeId);
  document.getElementById("breadcrumb-home").focus();
  history.pushState({ view: "node", nodeId: nodeId, serverUrl: serverUrl }, "");
}

function loadNodeDetail(nodeId) {
  var detailP = authFetch(
    "/api/cluster/node/" + encodeURIComponent(nodeId),
  ).then(function (r) {
    return r.json();
  });
  var overviewP = authFetch("/api/cluster/overview").then(function (r) {
    return r.json();
  });
  Promise.all([detailP, overviewP]).then(function (res) {
    var data = res[0];
    renderStatusBar(res[1]);
    if (data.error) {
      document.getElementById("node-ws-table").innerHTML =
        '<div class="dashboard-empty">' + escapeHtml(data.error) + "</div>";
      return;
    }
    var ws = data.workstreams || [];
    var active = ws.filter(function (w) {
      return w.state !== "idle";
    }).length;
    document.getElementById("node-ws-summary").textContent =
      active + " active \u00b7 " + ws.length + " total";
    renderWsTable(document.getElementById("node-ws-table"), ws);
  });
}

// --- Drill-down: Filtered ---
function drillDownByState(state) {
  currentView = "filtered";
  currentFilter = { state: state, node: null, page: 1, per_page: 50 };
  document.getElementById("view-overview").style.display = "none";
  document.getElementById("view-node").style.display = "none";
  document.getElementById("view-filtered").style.display = "";
  document.getElementById("breadcrumb").style.display = "";
  var sd = STATE_DISPLAY[state] || STATE_DISPLAY.idle;
  document.getElementById("breadcrumb-label").textContent =
    sd.symbol + " " + sd.label;
  document.getElementById("filtered-title").textContent =
    "WORKSTREAMS — " + sd.label.toUpperCase();
  document.getElementById("main").scrollTop = 0;
  loadFilteredWorkstreams();
  document.getElementById("breadcrumb-home").focus();
  history.pushState({ view: "filtered", filter: currentFilter }, "");
}

function drillDownByNode(nodeId) {
  currentView = "filtered";
  currentFilter = { state: null, node: nodeId, page: 1, per_page: 50 };
  document.getElementById("view-overview").style.display = "none";
  document.getElementById("view-node").style.display = "none";
  document.getElementById("view-filtered").style.display = "";
  document.getElementById("breadcrumb").style.display = "";
  document.getElementById("breadcrumb-label").textContent = nodeId;
  document.getElementById("filtered-title").textContent =
    "WORKSTREAMS — " + nodeId;
  document.getElementById("main").scrollTop = 0;
  loadFilteredWorkstreams();
  document.getElementById("breadcrumb-home").focus();
  history.pushState({ view: "filtered", filter: currentFilter }, "");
}

function loadFilteredWorkstreams() {
  var params =
    "page=" + currentFilter.page + "&per_page=" + currentFilter.per_page;
  if (currentFilter.state)
    params += "&state=" + encodeURIComponent(currentFilter.state);
  if (currentFilter.node)
    params += "&node=" + encodeURIComponent(currentFilter.node);
  var wsP = authFetch("/api/cluster/workstreams?" + params).then(function (r) {
    return r.json();
  });
  var overviewP = authFetch("/api/cluster/overview").then(function (r) {
    return r.json();
  });
  Promise.all([wsP, overviewP])
    .then(function (res) {
      var data = res[0];
      renderStatusBar(res[1]);
      document.getElementById("main").scrollTop = 0;
      document.getElementById("filtered-summary").textContent =
        "Page " +
        data.page +
        " of " +
        data.pages +
        " (" +
        data.total +
        " total)";
      renderWsTable(
        document.getElementById("filtered-ws-table"),
        data.workstreams,
      );
      renderPagination(
        document.getElementById("filtered-pagination"),
        data.page,
        data.pages,
      );
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
    loadFilteredWorkstreams();
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
    loadFilteredWorkstreams();
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

    container.appendChild(row);
  });
}

// --- Login Overlay ---
var _loginTrapHandler = null;
var _loginBusy = false;

function initLogin() {
  var overlay = document.createElement("div");
  overlay.id = "login-overlay";
  overlay.style.display = "none";
  overlay.setAttribute("role", "dialog");
  overlay.setAttribute("aria-modal", "true");
  overlay.setAttribute("aria-labelledby", "login-title");
  overlay.innerHTML =
    '<div id="login-box">' +
    '<h2 id="login-title">turnstone console</h2>' +
    '<div id="login-error" role="alert" aria-live="assertive"></div>' +
    '<label for="login-token" class="sr-only">Auth token</label>' +
    '<input id="login-token" type="password" placeholder="Enter auth token" autocomplete="off">' +
    '<button id="login-submit">Sign in</button>' +
    "</div>";
  document.body.appendChild(overlay);
  document.getElementById("login-submit").onclick = submitLogin;
  document
    .getElementById("login-token")
    .addEventListener("keydown", function (e) {
      if (e.key === "Enter") submitLogin();
      if (e.key === "Escape") {
        var errEl = document.getElementById("login-error");
        if (errEl && errEl.style.display !== "none") {
          errEl.style.display = "none";
          errEl.textContent = "";
        }
      }
    });
}

function showLogin() {
  var overlay = document.getElementById("login-overlay");
  if (!overlay) return;
  overlay.style.display = "flex";
  document.body.style.overflow = "hidden";
  var logoutBtn = document.getElementById("logout-btn");
  if (logoutBtn) logoutBtn.style.display = "none";
  var errEl = document.getElementById("login-error");
  if (errEl) {
    errEl.style.display = "none";
    errEl.textContent = "";
  }
  setTimeout(function () {
    var inp = document.getElementById("login-token");
    if (inp) {
      inp.value = "";
      inp.focus();
    }
  }, 50);
  // Focus trap
  if (_loginTrapHandler)
    document.removeEventListener("keydown", _loginTrapHandler);
  _loginTrapHandler = function (e) {
    if (e.key === "Tab") {
      var box = document.getElementById("login-box");
      var focusable = box.querySelectorAll("input, button");
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
  document.addEventListener("keydown", _loginTrapHandler);
}

function hideLogin() {
  var overlay = document.getElementById("login-overlay");
  if (overlay) overlay.style.display = "none";
  document.body.style.overflow = "";
  if (_loginTrapHandler) {
    document.removeEventListener("keydown", _loginTrapHandler);
    _loginTrapHandler = null;
  }
}

function submitLogin() {
  if (_loginBusy) return;
  var token = (document.getElementById("login-token").value || "").trim();
  if (!token) {
    var errEl = document.getElementById("login-error");
    if (errEl) {
      errEl.textContent = "Token is required";
      errEl.style.display = "block";
    }
    document.getElementById("login-token").focus();
    return;
  }

  _loginBusy = true;
  var btn = document.getElementById("login-submit");
  var inp = document.getElementById("login-token");
  btn.disabled = true;
  btn.textContent = "Signing in\u2026";
  inp.disabled = true;

  fetch("/api/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ token: token }),
  })
    .then(function (r) {
      if (r.status === 401 || r.status === 403) throw new Error("invalid");
      if (!r.ok) throw new Error("server");
      return r.json();
    })
    .then(function () {
      _loginBusy = false;
      btn.disabled = false;
      btn.textContent = "Sign in";
      inp.disabled = false;
      hideLogin();
      document.getElementById("logout-btn").style.display = "";
      connectSSE();
      if (currentView === "overview") loadOverview();
      else if (currentView === "node") drillDownToNode(currentNodeId);
      else if (currentView === "filtered") loadFilteredWorkstreams();
    })
    .catch(function (err) {
      _loginBusy = false;
      btn.disabled = false;
      btn.textContent = "Sign in";
      inp.disabled = false;
      var errEl = document.getElementById("login-error");
      if (errEl) {
        errEl.textContent =
          err.message === "invalid"
            ? "Invalid token"
            : "Connection failed \u2014 try again";
        errEl.style.display = "block";
      }
    });
}

function logout() {
  fetch("/api/auth/logout", { method: "POST" }).then(function () {
    if (evtSource) {
      evtSource.close();
      evtSource = null;
    }
    showLogin();
  });
}

// --- Navigation ---
window.addEventListener("popstate", function (e) {
  var overlay = document.getElementById("login-overlay");
  if (overlay && overlay.style.display !== "none") return;
  if (!e.state) {
    showOverview();
    return;
  }
  if (e.state.view === "overview") showOverview();
  else if (e.state.view === "node" && e.state.nodeId)
    drillDownToNode(e.state.nodeId, e.state.serverUrl);
  else if (e.state.view === "filtered" && e.state.filter) {
    currentFilter = e.state.filter;
    if (currentFilter.state) drillDownByState(currentFilter.state);
    else if (currentFilter.node) drillDownByNode(currentFilter.node);
  }
});

// --- Keyboard shortcuts help ---
function showKbHelp() {
  var existing = document.getElementById("kb-overlay");
  if (existing) {
    existing.remove();
  }
  var overlay = document.createElement("div");
  overlay.id = "kb-overlay";
  overlay.innerHTML =
    '<div id="kb-box" role="dialog" aria-modal="true" aria-label="Keyboard shortcuts">' +
    "<h2>Keyboard shortcuts</h2>" +
    '<div class="kb-section">Navigation</div>' +
    '<div class="kb-row"><span class="kb-desc">Activate card / row</span><span class="kb-key">Enter</span></div>' +
    '<div class="kb-row"><span class="kb-desc">Activate card / row</span><span class="kb-key">Space</span></div>' +
    '<div class="kb-row"><span class="kb-desc">Navigate rows</span><span class="kb-key">\u2191</span> <span class="kb-key">\u2193</span></div>' +
    '<div class="kb-section">General</div>' +
    '<div class="kb-row"><span class="kb-desc">Show this help</span><span class="kb-key">?</span></div>' +
    '<div class="kb-row"><span class="kb-desc">Close overlay</span><span class="kb-key">Esc</span></div>' +
    '<div class="kb-hint">Press <span class="kb-key">Esc</span> to close</div>' +
    "</div>";
  overlay.onclick = function (e) {
    if (e.target === overlay) hideKbHelp();
  };
  document.body.appendChild(overlay);
  document.getElementById("kb-box").focus();
}
function hideKbHelp() {
  var el = document.getElementById("kb-overlay");
  if (el) el.remove();
}
document.addEventListener("keydown", function (e) {
  // Don't trigger when typing in inputs or when login overlay is open
  if (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA") return;
  var login = document.getElementById("login-overlay");
  if (login && login.style.display !== "none") return;
  if (e.key === "?" && !e.ctrlKey && !e.metaKey) {
    e.preventDefault();
    showKbHelp();
  }
  if (e.key === "Escape") {
    var kb = document.getElementById("kb-overlay");
    if (kb) {
      e.preventDefault();
      hideKbHelp();
    }
  }
});

// --- Init ---
history.replaceState({ view: "overview" }, "");
initLogin();
connectSSE();
loadOverview();
// Try loading — if auth required, login overlay will show
