// --- Shared hooks ---
window.onLoginSuccess = function () {
  connectSSE();
  // Refresh permission-gated home-landing UI (admin.coordinator etc).
  if (typeof _refreshHomeComposerVisibility === "function") {
    _refreshHomeComposerVisibility();
  }
  // Re-populate the home-composer skill dropdown now that auth has
  // landed.  The initial page-load pass runs before login completes,
  // so /v1/api/skills 401s; without this re-run the dropdown stays
  // empty.
  if (typeof _populateHomeSkillDropdown === "function") {
    _populateHomeSkillDropdown();
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
  if (sseReconnectTimer) {
    clearTimeout(sseReconnectTimer);
    sseReconnectTimer = null;
  }
  if (evtSource) {
    evtSource.close();
    evtSource = null;
  }
  if (typeof _refreshHomeComposerVisibility === "function") {
    _refreshHomeComposerVisibility();
  }
};
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
  // Persist to server so admin settings and node UIs see the change
  const themeValue = next === "light" ? "light" : "dark";
  authFetch("/v1/api/admin/settings/interface.theme", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ value: themeValue }),
  }).catch(function () {});
};
// Set initial theme button text and aria
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

// --- State ---
let currentView = "home"; // "home" | "overview" | "filtered" | "admin"
let currentFilter = { state: null, node: null, page: 1, per_page: 50 };
let evtSource = null;
let retryDelay = 1000;
// Pending reconnect handle — tracked so logout (and a fresh connectSSE) can
// cancel it; an untracked timer fired post-logout and opened a new
// EventSource that 401s and re-probes in a loop.
let sseReconnectTimer = null;
let clusterState = null;
let _navigatingFromPopstate = false;

// --- Constants ---
const STATE_DISPLAY = {
  running: { symbol: "\u25b8", label: "run" },
  thinking: { symbol: "\u25cc", label: "think" },
  attention: { symbol: "\u25c6", label: "attn" },
  idle: { symbol: "\u00b7", label: "idle" },
  error: { symbol: "\u2716", label: "err" },
};

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
  const t = data.type;
  if (t === "cluster_state") {
    const node = clusterState.nodes[data.node_id];
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
    const targetNode = clusterState.nodes[data.node_id];
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
        // ws_created SSE events carry kind / parent_ws_id / user_id /
        // project_id; preserve them on the in-memory ws so the home-landing
        // active-coordinators list and the tree grouping both pick up
        // newly-created rows without needing a snapshot refetch.
        kind: data.kind || "interactive",
        parent_ws_id: data.parent_ws_id || null,
        user_id: data.user_id || null,
        project_id: data.project_id || null,
      });
    }
  } else if (t === "ws_closed") {
    Object.keys(clusterState.nodes).forEach(function (nid) {
      const n = clusterState.nodes[nid];
      n.workstreams = (n.workstreams || []).filter(function (ws) {
        return ws.id !== data.ws_id;
      });
    });
    // The saved list spans BOTH kinds now, so refresh on any close — the
    // in-flight coalescing guard in loadSavedCoordinators (single fetch +
    // one catch-up) prevents per-close fan-out into the saved endpoint.
    if (typeof loadSavedCoordinators === "function") {
      loadSavedCoordinators();
    }
    // An open pane on this session closes with it — the coordinator just
    // closed its child (or the session ended elsewhere); a lingering tab
    // would only reconnect-poll a stream that is gone (the shell owns the
    // pane lifecycle; this is the Tier-1 → pane seam).
    if (
      window.TS_SHELL &&
      typeof window.TS_SHELL.notifySessionClosed === "function"
    ) {
      window.TS_SHELL.notifySessionClosed(data.ws_id);
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

let _renderTimer = null;
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
  const states = { running: 0, thinking: 0, attention: 0, idle: 0, error: 0 };
  let totalTokens = 0,
    totalToolCalls = 0,
    totalWs = 0;
  let mcpServers = 0,
    mcpResources = 0,
    mcpPrompts = 0;
  const versions = {};
  Object.keys(clusterState.nodes).forEach(function (nid) {
    // Skip the "console" pseudo-node — coordinators aren't compute-
    // node workstreams, and counting them here would inflate the
    // cluster totals.  The active-coordinators list surfaces them
    // separately.
    if (nid === "console") return;
    const node = clusterState.nodes[nid];
    let nodeWsTokens = 0;
    (node.workstreams || []).forEach(function (ws) {
      const s = ws.state || "idle";
      states[s] = (states[s] || 0) + 1;
      totalWs++;
      nodeWsTokens += ws.tokens || 0;
    });
    const aggTokens = (node.aggregate || {}).total_tokens || 0;
    totalTokens += aggTokens || nodeWsTokens;
    totalToolCalls += (node.aggregate || {}).total_tool_calls || 0;
    if (node.version) versions[node.version] = true;
    const mcp = (node.health || {}).mcp || {};
    mcpServers += mcp.servers || 0;
    mcpResources += mcp.resources || 0;
    mcpPrompts += mcp.prompts || 0;
  });
  const versionList = Object.keys(versions).sort();
  // Count only real compute nodes for the cluster summary — the
  // "console" pseudo-node hosts coordinators, which are surfaced
  // separately by the active-coordinators list.
  const realNodeCount = Object.keys(clusterState.nodes).filter(function (nid) {
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
  const states = { running: 0, thinking: 0, attention: 0, idle: 0, error: 0 };
  const ws = node.workstreams || [];
  ws.forEach(function (w) {
    const s = w.state || "idle";
    states[s] = (states[s] || 0) + 1;
  });
  let aggTokens = (node.aggregate || {}).total_tokens || 0;
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
  if (currentView === "filtered") {
    let allWs = [];
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
    const stateOrder = {
      running: 0,
      thinking: 1,
      attention: 2,
      error: 3,
      idle: 4,
    };
    allWs.sort(function (a, b) {
      return (stateOrder[a.state] || 9) - (stateOrder[b.state] || 9);
    });
    const total = allWs.length;
    const perPage = currentFilter.per_page || 50;
    const pages = Math.max(1, Math.ceil(total / perPage));
    const page = Math.min(currentFilter.page || 1, pages);
    const start = (page - 1) * perPage;
    const pageWs = allWs.slice(start, start + perPage);
    document.getElementById("filtered-summary").textContent =
      "Page " + page + " of " + pages + " (" + total + " total)";
    renderWsTable(document.getElementById("filtered-ws-table"), pageWs);
    renderPagination(
      document.getElementById("filtered-pagination"),
      page,
      pages,
    );
  }
  _fireRenderSubs();
}

// Notify the L-shell rail (rail.js) that cluster state was (re)rendered.
function _fireRenderSubs() {
  const subs = (window.TS_APP && window.TS_APP._renderSubs) || [];
  for (const cb of subs) {
    try {
      cb(clusterState);
    } catch (e) {
      console.error("onRender subscriber failed", e);
    }
  }
}

// --- SSE Connection ---
function connectSSE() {
  if (sseReconnectTimer) {
    clearTimeout(sseReconnectTimer);
    sseReconnectTimer = null;
  }
  if (evtSource) {
    evtSource.close();
    evtSource = null;
  }
  evtSource = new EventSource("/v1/api/cluster/events");
  const statusBar = document.getElementById("status-bar");
  evtSource.onopen = function () {
    retryDelay = 1000;
    statusBar.classList.remove("disconnected");
    statusBar.textContent = "";
  };
  evtSource.onmessage = function (e) {
    try {
      const data = JSON.parse(e.data);
      handleClusterEvent(data);
    } catch (err) {
      /* ignore malformed SSE */
    }
  };
  evtSource.onerror = function () {
    evtSource.close();
    evtSource = null;
    // Don't show reconnecting state if login overlay is visible
    const loginOverlay = document.getElementById("login-overlay");
    if (loginOverlay && loginOverlay.style.display !== "none") return;
    statusBar.textContent = "Reconnecting\u2026";
    statusBar.classList.add("disconnected");
    // Raw fetch (not authFetch) — need to inspect status before throwing
    fetch("/v1/api/cluster/overview")
      .then(function (r) {
        if (r.status === 401) {
          showLogin();
          return;
        }
        sseReconnectTimer = setTimeout(connectSSE, retryDelay);
        retryDelay = Math.min(retryDelay * 2, 30000);
      })
      .catch(function () {
        sseReconnectTimer = setTimeout(connectSSE, retryDelay);
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
  if (data.type === "models_changed") {
    // Server emits this when a model definition or a role-assignment
    // setting (model.default_alias, judge.model, coordinator.model_alias,
    // coordinator.reasoning_effort) changes.  Refresh anything that
    // renders model aliases so labels stay accurate without a reload.
    if (typeof _populateHomeModelDropdowns === "function") {
      _populateHomeModelDropdowns();
    }
    if (
      typeof _adminTab !== "undefined" &&
      _adminTab === "models" &&
      typeof loadAdminModels === "function"
    ) {
      loadAdminModels();
    }
  }
}

// --- Home View ---
//
// Coordinator-first landing: composer + active-coordinators list +
// inline node list.  The node list is self-collapsing (consecutive
// same-prefix nodes group into a single row) so it stays visible
// without dominating the page.
function showHome() {
  currentView = "home";
  currentFilter = { state: null, node: null, page: 1, per_page: 50 };
  _setLandingView("home");
  // #view-admin is its own pane now; re-focus the Dashboard pane (home/filtered
  // sub-views live inside it) instead of toggling the old in-#main admin view.
  const pm = window.TS_SHELL && window.TS_SHELL.panes;
  if (pm) pm.openPane("dashboard");
  const adminBtn = document.getElementById("admin-btn");
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
  // Toggle the two top-level landing panes.  The node list lives inside
  // #view-home as a sibling section, and clicking a node navigates
  // straight to /node/<id>/ rather than swapping in a detail pane.
  const views = ["home", "filtered"];
  views.forEach(function (name) {
    const el = document.getElementById("view-" + name);
    if (!el) return;
    el.style.display = name === which ? "" : "none";
  });
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
      showToast("Failed to load cluster data");
    });
}

// --- Drill-down: Filtered ---
function drillDownByState(state) {
  currentView = "filtered";
  currentFilter = { state: state, node: null, page: 1, per_page: 50 };
  _setLandingView("filtered");
  // #view-admin is its own pane now; re-focus the Dashboard pane (home/filtered
  // sub-views live inside it) instead of toggling the old in-#main admin view.
  const pm = window.TS_SHELL && window.TS_SHELL.panes;
  if (pm) pm.openPane("dashboard");
  const adminBtn = document.getElementById("admin-btn");
  if (adminBtn) {
    adminBtn.classList.remove("active");
    adminBtn.setAttribute("aria-expanded", "false");
  }
  document.getElementById("breadcrumb").style.display = "";
  const sd = STATE_DISPLAY[state] || STATE_DISPLAY.idle;
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
  // #view-admin is its own pane now; re-focus the Dashboard pane (home/filtered
  // sub-views live inside it) instead of toggling the old in-#main admin view.
  const pm = window.TS_SHELL && window.TS_SHELL.panes;
  if (pm) pm.openPane("dashboard");
  const adminBtn = document.getElementById("admin-btn");
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
      document
        .getElementById("filtered-ws-table")
        .replaceChildren(makeEmptyState("Failed to load"));
    });
}

function renderPagination(container, page, pages) {
  container.replaceChildren();
  if (pages <= 1) return;
  const prev = document.createElement("button");
  prev.textContent = "\u25c4 Prev";
  prev.disabled = page <= 1;
  prev.onclick = function () {
    currentFilter.page--;
    if (clusterState) renderFromState();
    else loadFilteredWorkstreams();
  };
  container.appendChild(prev);
  const info = document.createElement("span");
  info.textContent = page + " / " + pages;
  container.appendChild(info);
  const next = document.createElement("button");
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
const _DASH_EXPAND_KEY_PREFIX = "coord-dashboard-expanded-";

function _isExpanded(coordWsId) {
  if (!coordWsId) return false;
  try {
    const v = localStorage.getItem(_DASH_EXPAND_KEY_PREFIX + coordWsId);
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
  const byId = {};
  wsList.forEach(function (ws) {
    if (ws.id) byId[ws.id] = ws;
  });
  const childrenMap = {};
  const roots = [];
  const orphans = [];
  wsList.forEach(function (ws) {
    const parent = ws.parent_ws_id || null;
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
    const empty = document.createElement("div");
    empty.className = "dashboard-empty";
    empty.textContent = "No workstreams";
    container.appendChild(empty);
    return;
  }
  const groups = _bucketByParent(wsList);

  function appendRow(ws, opts) {
    opts = opts || {};
    const row = _renderWsRow(ws, opts, container);
    container.appendChild(row);
    // Render children ALWAYS — expand/collapse is a CSS display toggle
    // on the child rows.  This lets the caret swap a class instead of
    // rebuilding the table, which preserves focus, avoids SR re-
    // announcement, and stays cheap regardless of row count.
    if (opts.childCount != null) {
      const kids = groups.childrenMap[ws.id] || [];
      kids.forEach(function (child) {
        const childRow = _renderWsRow(
          child,
          { isChild: true, parentWsId: ws.id, collapsed: !opts.expanded },
          container,
        );
        container.appendChild(childRow);
      });
    }
  }

  groups.roots.forEach(function (ws) {
    const kids = groups.childrenMap[ws.id] || [];
    const isCoord = ws.kind === "coordinator" || kids.length > 0;
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
  const state = ws.state || "idle";
  const sd = STATE_DISPLAY[state] || STATE_DISPLAY.idle;

  const row = document.createElement("div");
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
  let ariaLabel = sd.label + ": " + (ws.name || ws.id || "unnamed");
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

  const main = document.createElement("div");
  main.className = "dash-row-main";

  // Expand / collapse caret — coordinator rows only.  The caret is a
  // button so it's keyboard-reachable; clicking it toggles without
  // bubbling to the row-level deep link handler.
  if (opts.isCoordinator && opts.childCount != null && opts.childCount > 0) {
    const caret = document.createElement("button");
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
      const coordWsId = ws.id;
      const nowExpanded = caret.getAttribute("aria-expanded") !== "true";
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
        const selector =
          '.dash-row--child[data-parent-ws-id="' + cssEscape(coordWsId) + '"]';
        const kids = container.querySelectorAll(selector);
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
    const indent = document.createElement("span");
    indent.className = "dash-caret-placeholder";
    indent.setAttribute("aria-hidden", "true");
    main.appendChild(indent);
  }

  // STATE
  const stateCell = document.createElement("span");
  stateCell.className = "dash-cell-state";
  const dot = document.createElement("span");
  dot.className = "dash-state-dot";
  dot.dataset.state = state;
  dot.setAttribute("aria-hidden", "true");
  stateCell.appendChild(dot);
  const stateLabel = document.createElement("span");
  stateLabel.className = "dash-state-label";
  stateLabel.dataset.state = state;
  stateLabel.textContent = sd.symbol + " " + sd.label;
  stateCell.appendChild(stateLabel);
  main.appendChild(stateCell);

  // NAME (with optional child-count summary for collapsed coordinators)
  const nameCell = document.createElement("span");
  nameCell.className = "dash-cell-name";
  const nameText = ws.name || ws.title || ws.id || "";
  // The name truncates in its own element so the trailing markers (child-count,
  // orphan, project) stay visible instead of being clipped by the fixed cell.
  const nameTextEl = document.createElement("span");
  nameTextEl.className = "dash-cell-name-text";
  nameTextEl.textContent = nameText;
  nameCell.appendChild(nameTextEl);
  if (opts.isCoordinator && opts.childCount != null && opts.childCount > 0) {
    // Render the "(N children)" summary only when there actually are
    // children — the home view feeds a coordinator-only pool into
    // renderWsTable so _bucketByParent sees no children and would
    // otherwise always print "(0 children)".  CSS hides it when the
    // row is expanded (see [data-expanded="true"] .dash-child-count
    // in style.css).
    const summary = document.createElement("span");
    summary.className = "dash-child-count";
    summary.textContent =
      " (" +
      opts.childCount +
      (opts.childCount === 1 ? " child)" : " children)");
    nameCell.appendChild(summary);
  }
  if (opts.isOrphan) {
    const orphanBadge = document.createElement("span");
    orphanBadge.className = "dash-orphan-badge";
    orphanBadge.textContent = " orphan";
    nameCell.appendChild(orphanBadge);
  }
  // Project pill (shared .dash-project-pill) when the ws is attached to a
  // project the viewer can name — rides inside the name cell like the others.
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

  // MODEL
  const modelCell = document.createElement("span");
  modelCell.className = "dash-cell-model";
  modelCell.textContent = ws.model_alias || ws.model || "";
  if (ws.model) modelCell.title = ws.model;
  main.appendChild(modelCell);

  // NODE (clickable)
  const nodeCell = document.createElement("span");
  nodeCell.className = "dash-cell-node";
  nodeCell.textContent = ws.node || "";
  nodeCell.onclick = function (e) {
    e.stopPropagation();
    if (ws.node) drillDownByNode(ws.node);
  };
  main.appendChild(nodeCell);

  // TASK
  const taskCell = document.createElement("span");
  taskCell.className = "dash-cell-task";
  taskCell.textContent = ws.title || "";
  main.appendChild(taskCell);

  // TOKENS
  const tokensCell = document.createElement("span");
  tokensCell.className = "dash-cell-tokens";
  tokensCell.textContent = ws.tokens ? formatTokens(ws.tokens) : "";
  main.appendChild(tokensCell);

  // CTX
  const ctxCell = document.createElement("span");
  ctxCell.className = "dash-cell-ctx " + ctxClass(ws.context_ratio || 0);
  ctxCell.textContent =
    ws.context_ratio > 0 ? Math.round(ws.context_ratio * 100) + "%" : "";
  main.appendChild(ctxCell);

  row.appendChild(main);

  // Sub-line
  const sub = document.createElement("div");
  sub.className = "dash-row-sub";
  if (ws.activity_state === "approval") sub.classList.add("sub-attention");
  sub.textContent = ws.activity || "";
  row.appendChild(sub);

  // Deep link: click opens proxied server UI at this workstream.
  // Row clicks open an L-shell PANE (coordinator → coordinator pane; node-backed
  // → node-proxied interactive pane); full-page nav is the shell-absent fallback.
  const wsNodeId = ws.node;
  if (opts.isCoordinator || ws.kind === "coordinator") {
    row.classList.add("has-link");
    (function (wsId) {
      row.onclick = function () {
        if (!wsId) return;
        const pm = window.TS_SHELL && window.TS_SHELL.panes;
        if (pm) pm.openPane("coordinator", wsId);
        else window.location.href = "/coordinator/" + encodeURIComponent(wsId);
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
        const pm = window.TS_SHELL && window.TS_SHELL.panes;
        if (pm) pm.openPane("interactive", wsId, { nodeId: nodeId });
        else
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
  const overlay = document.getElementById("login-overlay");
  if (overlay && overlay.style.display !== "none") return;
  _navigatingFromPopstate = true;
  try {
    if (!e.state) {
      showHome();
      return;
    }
    if (e.state.view === "home" || e.state.view === "overview") showHome();
    else if (e.state.view === "filtered" && e.state.filter) {
      currentFilter = e.state.filter;
      if (currentFilter.state) drillDownByState(currentFilter.state);
      else if (currentFilter.node) drillDownByNode(currentFilter.node);
    } else showHome();
  } finally {
    _navigatingFromPopstate = false;
  }
});

// ---------------------------------------------------------------------------
// Coordinator session creation — used by the home-landing composer.
// Permission check lives in _hasCoordPermission (admin.coordinator);
// _createCoordinator does the POST + redirect.
// ---------------------------------------------------------------------------

function _hasCoordPermission() {
  const perms = sessionStorage.getItem("turnstone_permissions") || "";
  return perms.split(",").indexOf("admin.coordinator") !== -1;
}

// POST /v1/api/workstreams/new.  Accepts the three request fields
// directly + an errEl / setBusy callback so the caller owns the
// loading-state UX (button label swap, composer disabled flag, etc.).
// On success redirects to /coordinator/{ws_id}; on failure surfaces
// the server's error text inline through errEl.
// Build the fetch options for a workstream-create POST: multipart (a `meta`
// JSON field + one `file` part per staged attachment) when files are present,
// else plain JSON.  Shared by the coordinator and interactive launchers so the
// create wire shape lives in one place.
function _createWorkstreamFetchOpts(body, files) {
  if (files.length > 0) {
    const form = new FormData();
    form.append("meta", JSON.stringify(body));
    for (let i = 0; i < files.length; i++) {
      form.append("file", files[i], files[i].name);
    }
    // Don't set Content-Type — the browser adds the correct multipart boundary.
    return { method: "POST", body: form };
  }
  return {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
}

function _createCoordinator(opts) {
  const name = (opts.name || "").trim();
  const skill = opts.skill || "";
  const model = (opts.model || "").trim();
  const judgeModel = (opts.judge_model || "").trim();
  const project = (opts.project_id || "").trim();
  const task = (opts.task || "").trim();
  const errEl = opts.errEl;
  const setBusy = opts.setBusy || function () {};
  const onSuccess = opts.onSuccess || function () {};

  // Error region is always rendered with reserved min-height (see
  // .home-composer-error in style.css) so toggling validation messages
  // doesn't reflow the active-coordinators list below — clear the
  // textContent only, no display toggle.
  errEl.textContent = "";
  setBusy(true);

  const body = {};
  if (name) body.name = name;
  if (skill) body.skill = skill;
  if (model) body.model = model;
  if (judgeModel) body.judge_model = judgeModel;
  if (project) body.project_id = project;
  if (task) body.initial_message = task;

  // Multipart when files are staged (meta JSON + file parts); the coord create
  // endpoint reserves the attachments for the very first turn.  Plain JSON
  // otherwise.
  const files = Array.isArray(opts.files) ? opts.files : [];
  const fetchOpts = _createWorkstreamFetchOpts(body, files);

  authFetch("/v1/api/workstreams/new", fetchOpts)
    .then(function (r) {
      return r.json().then(function (data) {
        return { ok: r.ok, status: r.status, data: data };
      });
    })
    .then(function (res) {
      setBusy(false);
      if (!res.ok || !res.data || !res.data.ws_id) {
        errEl.textContent =
          (res.data && res.data.error) || "HTTP " + res.status;
        return;
      }
      onSuccess(res);
      // Open the new coordinator as an L-shell PANE (the renovation: sessions
      // open as tabs, not full-page nav).  Full-page nav stays as the
      // shell-absent fallback.  The session was just created + is live in the
      // console, so the coordinator pane connects straight away (no /open hop).
      const pm = window.TS_SHELL && window.TS_SHELL.panes;
      if (pm) pm.openPane("coordinator", res.data.ws_id);
      else
        window.location.href =
          "/coordinator/" + encodeURIComponent(res.data.ws_id);
    })
    .catch(function () {
      setBusy(false);
      errEl.textContent = "Request failed";
    });
}

function _hasInteractivePermission() {
  const perms = sessionStorage.getItem("turnstone_permissions") || "";
  return perms.split(",").indexOf("workstreams.create") !== -1;
}

// Which persona the launcher creates: "coordinator" (console-local) or
// "interactive" (proxied to a compute node).  The composer is shared; only the
// submit endpoint + redirect differ.
let _launcherKind = "coordinator";

// Sentinel value for the project picker's "+ New project…" row — selecting it
// prompts for a name, creates, then re-selects the new project (see
// _applyLauncherFields' onChange branch).
const _PROJECT_NEW = "__new__";

function _setLauncherKind(kind, focus) {
  _launcherKind = kind;
  const map = {
    "persona-coordinator": "coordinator",
    "persona-interactive": "interactive",
  };
  Object.keys(map).forEach(function (id) {
    const btn = document.getElementById(id);
    if (!btn) return;
    const on = map[id] === kind;
    btn.classList.toggle("active", on);
    btn.setAttribute("aria-checked", on ? "true" : "false");
    btn.tabIndex = on ? 0 : -1; // roving tabindex — the radiogroup is one tab stop
    if (on && focus) btn.focus();
  });
  _applyLauncherFields();
}

// Reflect the active persona in the shared launcher composer: the task-prompt
// hint, and which option fields are relevant.  The node picker is
// interactive-only (coordinators run in the console, not on a compute node);
// its node list appears only under the "Specific node" strategy.
function _applyLauncherFields() {
  if (!_homeCoordComposer) return;
  const interactive = _launcherKind === "interactive";
  _homeCoordComposer.setPlaceholder(
    interactive
      ? "What do you want to work on?"
      : "What should this coordinator orchestrate?",
  );
  _homeCoordComposer.setOptionFieldVisible("node_strategy", interactive);
  const specific =
    interactive &&
    _homeCoordComposer.getOptionValue("node_strategy") === "node";
  _homeCoordComposer.setOptionFieldVisible("node_id", specific);
  if (specific) _populateLauncherNodes();

  // "+ New project…" — reset the field FIRST so the sentinel can't stick (or
  // loop on re-entry), then reveal the inline creator beneath the picker.
  if (_homeCoordComposer.getOptionValue("project") === _PROJECT_NEW) {
    _homeCoordComposer.setOptionValue("project", "");
    if (_homeProjectCreator) _homeProjectCreator.open();
  }
}

// Populate the launcher's "Specific node" picker from the live Tier-1 snapshot
// (the SAME clusterState the rail node list reads), excluding the `console`
// pseudo-node (interactive sessions run on compute nodes) and unreachable
// nodes.  Re-read on each reveal so a node that just (dis)appeared is current.
function _populateLauncherNodes() {
  if (!_homeCoordComposer) return;
  // Preserve the operator's current pick across the rebuild: selecting a node
  // fires the composer `change` event → onChange → _applyLauncherFields → here,
  // and setOptionChoices() resets the <select> to its placeholder.  Without this
  // the selection is wiped the instant it's made, so a Specific-node session can
  // never be launched.  Restored only if the node still exists (else it falls
  // back to the placeholder, which is correct — the picked node disappeared).
  const previous = _homeCoordComposer.getOptionValue("node_id");
  const choices = [];
  if (clusterState && clusterState.nodes) {
    Object.keys(clusterState.nodes)
      .filter(function (nid) {
        return nid !== "console";
      })
      .sort()
      .forEach(function (nid) {
        const n = clusterState.nodes[nid];
        if (n && n.reachable === false) return;
        const count = n && n.workstreams ? n.workstreams.length : 0;
        choices.push({ value: nid, text: nid + " (" + count + " ws)" });
      });
  }
  _homeCoordComposer.setOptionChoices("node_id", choices);
  if (previous) _homeCoordComposer.setOptionValue("node_id", previous);
}

function _wireLauncherToggle() {
  const group = document.getElementById("launcher-personas");
  const coordBtn = document.getElementById("persona-coordinator");
  const intBtn = document.getElementById("persona-interactive");
  if (coordBtn) {
    coordBtn.addEventListener("click", function () {
      _setLauncherKind("coordinator");
    });
  }
  if (intBtn) {
    intBtn.addEventListener("click", function () {
      _setLauncherKind("interactive");
    });
  }
  if (group) {
    // WAI-ARIA radiogroup contract: arrow keys move the selection (+ focus).
    group.addEventListener("keydown", function (e) {
      if (
        ["ArrowLeft", "ArrowUp", "ArrowRight", "ArrowDown"].indexOf(e.key) ===
        -1
      ) {
        return;
      }
      e.preventDefault();
      const back = e.key === "ArrowLeft" || e.key === "ArrowUp";
      const next = back
        ? _launcherKind === "interactive"
          ? "coordinator"
          : "interactive"
        : _launcherKind === "coordinator"
          ? "interactive"
          : "coordinator";
      _setLauncherKind(next, true);
    });
  }
  _setLauncherKind(_launcherKind); // seed the initial roving-tabindex state
}

// POST /v1/api/cluster/workstreams/new — the console picks a node and PROXIES
// the create; interactive sessions run on compute nodes (only coordinators
// live in the console).  On success redirects to the node's session view.
function _createInteractive(opts) {
  const name = (opts.name || "").trim();
  const skill = opts.skill || "";
  const model = (opts.model || "").trim();
  const judgeModel = (opts.judge_model || "").trim();
  const project = (opts.project_id || "").trim();
  const task = (opts.task || "").trim();
  const errEl = opts.errEl;
  const setBusy = opts.setBusy || function () {};
  const onSuccess = opts.onSuccess || function () {};

  // Node placement (launcher node-strategy picker): "node" pins to the chosen
  // node; anything else lets the console pick the least-loaded node ("auto").
  // Validate the specific-node choice BEFORE flipping busy so a missing pick
  // surfaces inline without a stuck spinner.
  let placement = "auto";
  if (opts.node_strategy === "node") {
    placement = (opts.node_id || "").trim();
    if (!placement) {
      errEl.textContent = "Choose a node, or switch to Least loaded.";
      return;
    }
  }

  errEl.textContent = "";
  setBusy(true);

  const body = { node_id: placement };
  if (name) body.name = name;
  if (skill) body.skill = skill;
  if (model) body.model = model;
  if (judgeModel) body.judge_model = judgeModel;
  if (project) body.project_id = project;
  if (task) body.initial_message = task;

  // Multipart when files are staged (meta JSON + file parts); the cluster proxy
  // picks the node (node_id in meta) and forwards the files to its create
  // endpoint.  Plain JSON otherwise.
  const files = Array.isArray(opts.files) ? opts.files : [];
  const fetchOpts = _createWorkstreamFetchOpts(body, files);

  authFetch("/v1/api/cluster/workstreams/new", fetchOpts)
    .then(function (r) {
      return r.json().then(function (data) {
        return { ok: r.ok, status: r.status, data: data };
      });
    })
    .then(function (res) {
      setBusy(false);
      const data = res.data || {};
      const wsId = data.correlation_id || data.ws_id;
      const node = data.target_node;
      if (!res.ok || !wsId) {
        errEl.textContent = data.error || "HTTP " + res.status;
        return;
      }
      onSuccess(res);
      // Open the new session as an L-shell PANE (interactive sessions are
      // node-proxied; the pane resolves + opens on its node before streaming).
      // Full-page nav stays as the shell-absent fallback.
      const pm = window.TS_SHELL && window.TS_SHELL.panes;
      if (pm) {
        // target_node is the just-created live node; pass it as the open-time
        // hint so the pane pins there (origin-first) instead of re-routing.  A
        // missing node (server-contract drift) still recovers via the pane's
        // rendezvous fallback.
        pm.openPane("interactive", wsId, { nodeId: node || null });
      } else if (node) {
        window.location.href =
          "/node/" +
          encodeURIComponent(node) +
          "/?ws_id=" +
          encodeURIComponent(wsId);
      } else {
        // Defensive: a 200 without target_node (a server-contract drift, e.g. an
        // async-dispatch response) would otherwise strand the user silently.
        errEl.textContent = "Session created but no target node was reported";
      }
    })
    .catch(function () {
      setBusy(false);
      errEl.textContent = "Request failed";
    });
}

// ---------------------------------------------------------------------------
// Home-landing session launcher (coordinator or interactive) + saved sessions.
// The composer renders as a persistent panel on the home view.  The
// active list and
// cluster summary render from clusterState, so every SSE-driven patch
// picks them up automatically via scheduleRender → renderFromState.
// ---------------------------------------------------------------------------

let _homeComposerInit = false;
let _homeCoordComposer = null; // shared Composer instance
let _homeCoordBusy = false;

// Attachment staging for the home coord composer.  The coord ws_id
// doesn't exist until the create POST resolves, so we hold File
// objects in memory and ship them as multipart parts on submit (same
// pattern interactive uses for its new-ws modal + dashboard composer).
let _homeStagedFiles = [];

// Per-kind size caps + allowlist mirrored from turnstone/core/attachments.py
// so the browser can fail fast.  Keep in sync with the interactive
// UI's _ATTACH_* constants in turnstone/ui/static/app.js.
const _HOME_IMAGE_CAP = 4 * 1024 * 1024;
const _HOME_TEXT_CAP = 512 * 1024;
const _HOME_MAX_FILES = 10;
const _HOME_IMAGE_MIMES = [
  "image/png",
  "image/jpeg",
  "image/gif",
  "image/webp",
];
const _HOME_TEXT_APP_MIMES = [
  "application/json",
  "application/xml",
  "application/x-yaml",
  "application/yaml",
  "application/toml",
];
const _HOME_TEXT_EXTENSIONS = [
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

function _homeFormatSize(n) {
  if (n < 1024) return n + " B";
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
  return (n / (1024 * 1024)).toFixed(1) + " MB";
}

function _homeIsAttachmentAllowed(file) {
  const mime = (file.type || "").toLowerCase();
  if (_HOME_IMAGE_MIMES.indexOf(mime) !== -1) return true;
  if (mime.indexOf("text/") === 0) return true;
  if (_HOME_TEXT_APP_MIMES.indexOf(mime) !== -1) return true;
  const name = (file.name || "").toLowerCase();
  const dot = name.lastIndexOf(".");
  if (dot >= 0 && _HOME_TEXT_EXTENSIONS.indexOf(name.substr(dot)) !== -1) {
    return true;
  }
  return false;
}

function _homeShowError(msg) {
  const errEl = document.getElementById("home-coord-error");
  if (!errEl) return;
  // Element is always rendered (min-height reserves the row); just
  // toggle the message text so layout doesn't shift on validation.
  errEl.textContent = msg || "";
}

function _homeRenderChips() {
  if (!_homeCoordComposer || !_homeCoordComposer.chipsEl) return;
  const chipsEl = _homeCoordComposer.chipsEl;
  chipsEl.textContent = "";
  for (let i = 0; i < _homeStagedFiles.length; i++) {
    (function (idx) {
      const f = _homeStagedFiles[idx];
      const isImage = (f.type || "").indexOf("image/") === 0;
      const chip = document.createElement("span");
      chip.className =
        "composer-chip composer-chip-" + (isImage ? "image" : "text");
      chip.setAttribute("role", "listitem");

      const icon = document.createElement("span");
      icon.className = "composer-chip-icon";
      icon.setAttribute("aria-hidden", "true");
      icon.textContent = isImage ? "🖼" : "📄";
      chip.appendChild(icon);

      const name = document.createElement("span");
      name.className = "composer-chip-name";
      name.textContent = f.name;
      name.title = f.name + " (" + f.size + " bytes)";
      chip.appendChild(name);

      const size = document.createElement("span");
      size.className = "composer-chip-size";
      size.textContent = _homeFormatSize(f.size);
      chip.appendChild(size);

      const rm = document.createElement("button");
      rm.type = "button";
      rm.className = "composer-chip-remove";
      rm.setAttribute("aria-label", "Remove " + f.name);
      rm.title = "Remove";
      rm.textContent = "×";
      rm.onclick = function () {
        _homeStagedFiles.splice(idx, 1);
        _homeRenderChips();
      };
      chip.appendChild(rm);
      chipsEl.appendChild(chip);
    })(i);
  }
}

function _homeStageFile(file) {
  if (!file) return;
  if (_homeStagedFiles.length >= _HOME_MAX_FILES) {
    _homeShowError(
      "At most " + _HOME_MAX_FILES + " attachments per coordinator",
    );
    return;
  }
  if (!_homeIsAttachmentAllowed(file)) {
    _homeShowError(
      "Unsupported file type: " +
        file.name +
        " (allowed: png/jpeg/gif/webp images, text)",
    );
    return;
  }
  const isImage = (file.type || "").indexOf("image/") === 0;
  const cap = isImage ? _HOME_IMAGE_CAP : _HOME_TEXT_CAP;
  if (file.size > cap) {
    _homeShowError(file.name + " exceeds the " + _homeFormatSize(cap) + " cap");
    return;
  }
  _homeShowError("");
  _homeStagedFiles.push(file);
  _homeRenderChips();
}

function _homeClearStagedFiles() {
  _homeStagedFiles = [];
  _homeRenderChips();
}

// Sole owner of sendBtn.disabled: disables while a submit is in flight.
function _refreshHomeCoordSubmitEnabled() {
  if (!_homeCoordComposer) return;
  _homeCoordComposer.sendBtn.disabled = _homeCoordBusy;
}

function _ensureHomeComposerInit() {
  if (_homeComposerInit) return;
  _homeComposerInit = true;
  _mountHomeCoordComposer();
  _wireLauncherToggle();
  _populateHomeSkillDropdown();
  _populateHomeModelDropdowns();
  _refreshAndPopulateProjects();
  _ensureHomeProjectCreator();
  _refreshHomeComposerVisibility();
}

// Refresh the shared projects cache (window.TurnstoneProjects — also feeds the
// rail's group-by-project) then repaint the launcher's Project picker.  Safe
// when the bridge is absent (project.read denied / module still loading): the
// picker simply keeps its "No project" placeholder.
function _refreshAndPopulateProjects() {
  const TP = window.TurnstoneProjects;
  if (!TP) return;
  TP.refreshProjects().then(_populateHomeProjectDropdown);
}

// Populate the launcher's Project picker from the shared cache, preserving the
// current pick across the rebuild (same reason as _populateLauncherNodes) and
// always appending the "+ New project…" sentinel after the live list.
function _populateHomeProjectDropdown() {
  if (!_homeCoordComposer) return;
  const TP = window.TurnstoneProjects;
  if (!TP) return;
  const previous = _homeCoordComposer.getOptionValue("project");
  const choices = TP.projectChoices();
  choices.push({ value: _PROJECT_NEW, text: "+ New project…" });
  _homeCoordComposer.setOptionChoices("project", choices);
  if (previous && previous !== _PROJECT_NEW)
    _homeCoordComposer.setOptionValue("project", previous);
}

// The inline "+ New project…" creator (project_creator.js), mounted once into
// the composer's options panel right beneath the Project picker.  On Save it
// refreshes the shared cache; we then repopulate the dropdown + select the new
// project.  Replaces the old native window.prompt.  Full management — rename /
// visibility / members — still lives in the manage shelf.
let _homeProjectCreator = null;

function _ensureHomeProjectCreator() {
  if (_homeProjectCreator || !_homeCoordComposer) return;
  const PC = window.TurnstoneProjectCreator;
  if (!PC) return;
  _homeProjectCreator = PC.make({
    onCreated: function (proj) {
      _populateHomeProjectDropdown();
      _homeCoordComposer.setOptionValue("project", proj.project_id);
    },
  });
  _homeCoordComposer.addOptionsRowAfter("project", _homeProjectCreator.el);
}

function _mountHomeCoordComposer() {
  const mount = document.getElementById("home-coord-composer-mount");
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
        const bits = [];
        if (v.name) bits.push(v.name);
        if (v.skill) bits.push(v.skill);
        if (v.model) bits.push(v.model);
        if (v.judge_model) bits.push("judge: " + v.judge_model);
        if (v.project && v.project !== _PROJECT_NEW) {
          const pn = window.TurnstoneProjects
            ? window.TurnstoneProjects.projectName(v.project)
            : "";
          bits.push("project: " + (pn || v.project));
        }
        // Node placement is interactive-only; surface it only when a specific
        // node is pinned (the "Least loaded" default needs no summary line).
        if (
          _launcherKind === "interactive" &&
          v.node_strategy === "node" &&
          v.node_id
        )
          bits.push("node: " + v.node_id);
        return bits.join(" \u00b7 ");
      },
      // Re-evaluate the interactive node-picker visibility whenever a field
      // changes (the node list appears only under the "Specific node" strategy).
      onChange: function () {
        _applyLauncherFields();
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
        {
          id: "model",
          label: "Model",
          type: "select",
          choices: [{ value: "", text: "Default model" }],
        },
        {
          id: "judge_model",
          label: "Judge Model",
          type: "select",
          // Initial placeholder; _populateHomeModelDropdowns rewrites this
          // to "Default model (<alias>)" once /v1/api/models reports the
          // resolved judge alias (judge.model when set, otherwise the
          // session model — see IntentJudge.__init__).
          choices: [{ value: "", text: "Default model" }],
        },
        {
          // Attached project — scopes the session's `project` memory + groups
          // it in the rail.  Placeholder "No project" is preserved by
          // setOptionChoices; _populateHomeProjectDropdown appends the live
          // list + a "+ New project…" sentinel (handled in onChange).
          id: "project",
          label: "Project",
          type: "select",
          choices: [{ value: "", text: "No project" }],
        },
        // Node placement — INTERACTIVE persona only (coordinators run in the
        // console, not on a compute node).  _applyLauncherFields shows/hides
        // these per persona.  "auto" → the console picks the least-loaded node;
        // "node" → reveal the live node picker below + pin to the chosen node.
        {
          id: "node_strategy",
          label: "Node",
          type: "select",
          choices: [
            { value: "auto", text: "Least loaded" },
            { value: "node", text: "Specific node…" },
          ],
        },
        {
          id: "node_id",
          label: "Pick node",
          type: "select",
          // First option is the placeholder; _populateLauncherNodes appends the
          // live reachable compute nodes (excluding the console pseudo-node).
          choices: [{ value: "", text: "Select a node" }],
        },
      ],
    },
    attachments: {
      onAttach: function (file) {
        _homeStageFile(file);
      },
    },
    dragDrop: { targetEl: mount, dropClass: "home-coord-drop" },
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
      const choices = (data.skills || []).map(function (t) {
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

// Format a resolved alias with its model suffix the same way as the
// dropdown rows ("alias (model)", or just "alias" when they coincide).
// Returns "" when alias is empty or unknown so callers can fall back
// to a neutral placeholder.
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

// Populate Model + Judge Model dropdowns from /v1/api/models — same
// list the interactive new-ws modal uses.  Empty/default option stays
// at the top so submitting without a choice falls back to the
// ConfigStore-configured coordinator.model_alias / judge.model.
function _populateHomeModelDropdowns() {
  if (!_homeCoordComposer) return;
  authFetch("/v1/api/models")
    .then(function (r) {
      return r.ok ? r.json() : { models: [] };
    })
    .then(function (data) {
      const choices = (data.models || []).map(function (m) {
        const label =
          m.alias === m.model ? m.alias : m.alias + " (" + m.model + ")";
        return { value: m.alias, text: label };
      });
      _homeCoordComposer.setOptionChoices("model", choices);
      _homeCoordComposer.setOptionChoices("judge_model", choices);
      // Both placeholders use the same "Default — alias (model)"
      // template — the field-row labels (MODEL / JUDGE MODEL) already
      // carry the role context, so an asymmetric "Default judge model"
      // reads awkwardly alongside the plain "Default model" line above
      // it.  Em-dash separator (rather than nested parens) keeps the
      // alias's "(model)" suffix legible and matches the
      // ``(default — alias (model))`` pattern used in the admin Roles
      // tab.
      const coordDefault = _resolveModelLabel(
        data.coordinator_default_alias || "",
        data.models || [],
      );
      const judgeDefault = _resolveModelLabel(
        data.judge_default_alias || "",
        data.models || [],
      );
      _homeCoordComposer.setOptionPlaceholder(
        "model",
        coordDefault ? "Default — " + coordDefault : "Default model",
      );
      _homeCoordComposer.setOptionPlaceholder(
        "judge_model",
        judgeDefault ? "Default — " + judgeDefault : "Default model",
      );
    })
    .catch(function () {
      /* defaults still work even without the dropdown populated */
    });
}

function _refreshHomeComposerVisibility() {
  const panel = document.getElementById("coord-composer-panel");
  if (!panel) return;
  const canCoord = _hasCoordPermission();
  const canInt = _hasInteractivePermission();
  panel.style.display = canCoord || canInt ? "" : "none";
  const coordBtn = document.getElementById("persona-coordinator");
  const intBtn = document.getElementById("persona-interactive");
  if (coordBtn) coordBtn.style.display = canCoord ? "" : "none";
  if (intBtn) intBtn.style.display = canInt ? "" : "none";
  // Hide the toggle when only one persona is available.
  const personas = document.getElementById("launcher-personas");
  if (personas) personas.style.display = canCoord && canInt ? "" : "none";
  // Default to a persona the user can actually create.
  if (_launcherKind === "coordinator" && !canCoord && canInt) {
    _setLauncherKind("interactive");
  } else if (_launcherKind === "interactive" && !canInt && canCoord) {
    _setLauncherKind("coordinator");
  }
}

function submitHomeCoord(textFromComposer) {
  const kind = _launcherKind;
  if (kind === "coordinator" && !_hasCoordPermission()) {
    showToast("admin.coordinator permission required");
    return;
  }
  if (kind === "interactive" && !_hasInteractivePermission()) {
    showToast("workstreams.create permission required");
    return;
  }
  if (!_homeCoordComposer) return;
  // text arg is passed when the Composer's Enter-key handler fires;
  // direct callers (Ctrl/Cmd+Enter) invoke with no argument and we
  // read from the composer.
  const task =
    textFromComposer != null ? textFromComposer : _homeCoordComposer.value;
  const opts = _homeCoordComposer.getOptionValues();
  // Snapshot at submit time so a chip remove mid-request can't race
  // the multipart payload (the actual reset only fires on the success
  // branch, after the response lands).
  const files = _homeStagedFiles.slice();
  // Files-without-text would upload pending attachment rows but the
  // server's _coord_create_post_install only reserves+dispatches when
  // initial_message is non-empty — uploaded files would orphan as
  // pending storage rows until the GC sweep.  Require text whenever
  // attachments are staged so the first turn always picks them up.
  if (files.length > 0 && !(task || "").trim()) {
    _homeShowError(
      "Add a task message — attachments need an initial turn to dispatch on.",
    );
    return;
  }
  const shared = {
    name: opts.name || "",
    skill: opts.skill || "",
    model: opts.model || "",
    judge_model: opts.judge_model || "",
    // A pending "+ New project…" sentinel never reaches submit (it's reset in
    // onChange); guard anyway so it can't leak onto the wire as a project_id.
    project_id:
      opts.project && opts.project !== _PROJECT_NEW ? opts.project : "",
    task: task,
    errEl: document.getElementById("home-coord-error"),
    setBusy: function (b) {
      _homeCoordBusy = b;
      if (_homeCoordComposer) _homeCoordComposer.setBusy(b);
      _refreshHomeCoordSubmitEnabled();
    },
    onSuccess: function () {
      _homeClearStagedFiles();
    },
  };
  if (kind === "interactive") {
    // Create-with-attachments rides multipart through the cluster proxy to the
    // node (see _createInteractive); the files-need-a-task guard above already
    // ensures an initial turn to dispatch them on.
    shared.files = files;
    // Node placement from the launcher's node-strategy picker (interactive-only).
    shared.node_strategy = opts.node_strategy || "auto";
    shared.node_id = (opts.node_id || "").trim();
    _createInteractive(shared);
  } else {
    shared.files = files;
    _createCoordinator(shared);
  }
}

// Ctrl/Cmd+Enter anywhere on the home page submits the coordinator
// composer — consistent with the modal's keyboard-shortcut convention.
// The Composer's own Enter handler already fires submitHomeCoord when
// focus is in the textarea; this covers the case when focus sits on
// the attach / options buttons.
document.addEventListener("keydown", function (e) {
  if (e.key !== "Enter" || !(e.ctrlKey || e.metaKey)) return;
  if (!_homeCoordComposer) return;
  const mount = document.getElementById("home-coord-composer-mount");
  if (!mount || !mount.contains(e.target)) return;
  e.preventDefault();
  if (!_homeCoordComposer.sendBtn.disabled) submitHomeCoord();
});

// ---------------------------------------------------------------------------
// Saved coordinators — closed sessions persisted on disk.  Mirrors the
// interactive UI's "Saved Workstreams" table (same /shared/cards.js
// createSavedTable + /shared/cards.css, same response item shape from
// /v1/api/workstreams/saved), differing only in the CHILDREN column and
// the body-keyed delete.  Click a row → POST /open then
// /coordinator/{ws_id}; the lifted detail factory lazily rehydrates from
// storage on the GET miss.
// ---------------------------------------------------------------------------

// In-flight de-dup for loadSavedCoordinators.  ws_closed events can
// arrive in bursts on a busy cluster; without this guard each one
// triggers a parallel fetch.  Single boolean is enough because the
// renderer reads from the latest response — a coalesced re-fetch right
// after the in-flight one resolves catches any state change.
let _savedCoordsInFlight = false;
let _savedCoordsRetry = false;

function loadSavedCoordinators() {
  // Saved sessions is an operator surface (admin.coordinator) — the unified
  // list spans BOTH kinds for operators (who already have cluster-wide reach).
  // Interactive-only users use the launcher, not this list, so the gate stays
  // admin.coordinator (matching the server gate; a looser gate here would 403).
  if (!_hasCoordPermission()) return;
  // Freeze the list while the user is multi-selecting — re-rendering
  // mid-mode would shuffle the visible page out from under them.  The
  // delete-mode wrapper drains the retry flag on cancel/onClose.
  if (_coordTable && _coordTable.controller.inMode()) {
    _savedCoordsRetry = true;
    return;
  }
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
      // Belt-and-braces: if the user entered delete mode while this
      // fetch was already in flight, defer the render — re-rendering
      // mid-selection would shuffle visible cards and reshape selections.
      if (_coordTable && _coordTable.controller.inMode()) {
        _savedCoordsRetry = true;
        return;
      }
      const saved = data.workstreams || [];
      const sec = document.getElementById("saved-coordinators");
      if (sec) sec.style.display = saved.length ? "" : "none";
      _coordTable.setItems(saved);
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

// Saved Coordinators table — same shared createSavedTable as the server UI
// (/shared/cards.js), with a CHILDREN column instead of MSGS and the
// body-keyed (router-proxied) delete.  Activation POSTs /open before
// navigating so capacity limits surface as a toast, not a broken page.
let _coordTable = null;

// Built at boot, not parse: the saved-table substrate (/shared/cards.js) is a
// deferred ES module now, so its bridged globals (SavedColumns,
// createSavedTable) don't exist yet while this classic file parses.
function _initSavedCoordTable() {
  const COORD_COLUMNS = [
    SavedColumns.name(),
    {
      key: "kind",
      label: "KIND",
      width: "62px",
      cell: function (s) {
        const tag = document.createElement("span");
        const coord = s.kind === "coordinator";
        tag.className = "persona-tag" + (coord ? " coord" : " int");
        tag.textContent = coord ? "COORD" : "INT";
        return tag;
      },
      sort: function (s) {
        return s.kind || "";
      },
    },
    SavedColumns.project(),
    SavedColumns.model(),
    SavedColumns.count("child_count", "CHILDREN", "92px"),
    SavedColumns.ctx(),
    SavedColumns.last(),
    SavedColumns.id(),
  ];
  _coordTable = createSavedTable({
    headerEl: document.getElementById("coord-saved-colheaders"),
    bodyEl: document.getElementById("saved-coord-cards"),
    filterEl: document.getElementById("coord-filter"),
    footerEl: document.getElementById("coord-saved-footer"),
    paginationEl: document.getElementById("coord-pagination"),
    columns: COORD_COLUMNS,
    noun: "session",
    emptyText: "No saved sessions",
    activateLabel: function (s) {
      return (
        "Resume " +
        (s.kind === "coordinator" ? "coordinator" : "session") +
        ": " +
        (s.alias || s.title || s.name || s.ws_id)
      );
    },
    onActivate: function (s, rowEl) {
      // Open the session as an L-shell PANE (the renovation: rail + saved-list
      // clicks open tabs, not full-page nav).  Fall back to full-page nav only if
      // the shell isn't present.
      const pm = window.TS_SHELL && window.TS_SHELL.panes;
      // Interactive sessions live on a compute node and, unlike coordinators,
      // have no warm pool: a dormant one must be routed to a live node and
      // rehydrated there before its pane can stream.  The interactive pane does
      // exactly that on first activate (resolveInteractiveNode: origin-first
      // POST /open with a rendezvous fallback), so the saved-row click just opens
      // the pane with the origin node as the hint.  Shell-absent falls back to a
      // best-effort full-page nav to the origin node, whose detail page
      // rehydrates lazily.
      if (s.kind !== "coordinator") {
        if (pm) {
          pm.openPane("interactive", s.ws_id, { nodeId: s.node_id || null });
        } else if (s.node_id) {
          window.location.href =
            "/node/" +
            encodeURIComponent(s.node_id) +
            "/?ws_id=" +
            encodeURIComponent(s.ws_id);
        } else {
          showToast("Session node unknown");
        }
        return;
      }
      // POST /open BEFORE navigating so capacity issues surface as a toast
      // instead of a broken-looking detail page.
      if (rowEl) rowEl.classList.add("is-busy");
      authFetch(
        "/v1/api/workstreams/" + encodeURIComponent(s.ws_id) + "/open",
        {
          method: "POST",
        },
      )
        .then(function (r) {
          if (r.ok) {
            if (rowEl) rowEl.classList.remove("is-busy");
            if (pm) pm.openPane("coordinator", s.ws_id);
            else
              window.location.href =
                "/coordinator/" + encodeURIComponent(s.ws_id);
            return;
          }
          if (rowEl) rowEl.classList.remove("is-busy");
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
          if (rowEl) rowEl.classList.remove("is-busy");
          showToast("Failed to restore coordinator");
        });
    },
    delete: {
      idPrefix: "coord-delete",
      buttonId: "coord-delete-btn",
      // Coordinators live on whichever node owns the ws_id; the router proxy
      // reads ws_id from the body, resolves the owning node via rendezvous
      // hashing, and forwards to that node's POST workstreams/{ws_id}/delete.
      buildDeleteRequest: function (wsId) {
        return {
          url: "/v1/api/route/workstreams/delete",
          options: {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ ws_id: wsId }),
          },
        };
      },
      onClose: function () {
        // Drain queued retries before the explicit reload (see the freeze
        // gate in loadSavedCoordinators) so .finally() doesn't double-fetch.
        _savedCoordsRetry = false;
        loadSavedCoordinators();
      },
    },
  });
  // The PROJECT column resolves names from the shared projects cache,
  // which fills asynchronously — re-render once names arrive.
  if (window.TurnstoneProjects) {
    window.TurnstoneProjects.onProjectsChange(function () {
      if (_coordTable) _coordTable.render();
    });
  }
}

// HTML inline-onclick wrappers — keep the global names the markup binds
// to and forward to the shared controller.
function startCoordDeleteMode() {
  _coordTable.controller.start();
}
function cancelCoordDeleteMode() {
  _coordTable.controller.cancel();
  // The freeze gate (see loadSavedCoordinators) may have queued retries
  // while we were multi-selecting; drain them now that we're idle again.
  if (_savedCoordsRetry) {
    _savedCoordsRetry = false;
    loadSavedCoordinators();
  }
}
function toggleCoordSelectAll() {
  _coordTable.controller.toggleAll();
}
function confirmCoordDeleteSelection() {
  _coordTable.controller.confirmSelection();
}

// --- Init ---
// SSE connects after auth is confirmed — either via onLoginSuccess after
// login, or after the first successful data load (page refresh with valid cookie).
let _sseStarted = false;
function _ensureSSE() {
  if (!_sseStarted) {
    _sseStarted = true;
    connectSSE();
  }
}
// --- Boot ---
// The L-shell (shared_static/shell.js, an ES module) is deferred and runs after
// this classic script, so it drives boot() once the rail + status DOM exist —
// init no longer auto-runs at parse time.  window.onLoginSuccess (top of file)
// still starts the Tier-1 stream on both fresh login and refresh-with-cookie,
// so that path is unchanged.
window.TS_APP = window.TS_APP || {};
// --- Rail seam (Tier-1) ---
// The shell's rail.js reads cluster state + nav actions through this surface and
// re-renders whenever renderFromState fires onRender.  app.js keeps owning
// clusterState (no physical extraction); these closures see the live binding.
window.TS_APP._renderSubs = window.TS_APP._renderSubs || [];
window.TS_APP.getClusterState = function () {
  return clusterState;
};
window.TS_APP.onRender = function (cb) {
  window.TS_APP._renderSubs.push(cb);
};
window.TS_APP.drillDownByState = function (s) {
  drillDownByState(s);
};
window.TS_APP.bucketByParent = function (list) {
  return _bucketByParent(list);
};
window.TS_APP.buildNodeInfo = function (node) {
  return buildNodeInfoFromSnapshot(node);
};
// Resolve the cluster node that should host an interactive pane's session AND
// ensure the session is loaded there before the pane streams — the node /events
// stream 404s on a ws not loaded on that node, and /history alone won't load it.
// The shell's interactive pane factory calls this on first activate (fresh open,
// saved-row resume, AND reload-rehydrate all funnel through here).
//
// Origin-FIRST: POST /open on the hint node (the live / origin / just-created
// node) — this REUSES a session already loaded there instead of loading a
// duplicate copy elsewhere, and keeps node affinity (the pane talks directly to
// /node/{id} for every verb, so load-node == pane-node).  Only when the hint
// node is gone (404 = not in the registry / 502 = unreachable) or there is no
// hint do we re-home onto a live rendezvous node via GET /v1/api/route (the
// router skips dead nodes; persistence is shared ws_id-keyed Postgres, so any
// live node is state-safe).  Capacity (429) / permission (403) surface as an
// error — NOT a silent re-home.  Resolves to {nodeId} on success, else {error}.
window.TS_APP.resolveInteractiveNode = function (wsId, hintNodeId) {
  const openOn = function (nodeId) {
    return authFetch(
      "/node/" +
        encodeURIComponent(nodeId) +
        "/v1/api/workstreams/" +
        encodeURIComponent(wsId) +
        "/open",
      { method: "POST" },
    ).then(function (r) {
      return r.ok ? { nodeId: nodeId } : { status: r.status };
    });
  };
  // Single error surface: the failure is returned as {error} and the interactive
  // pane writes it into its .pane-status line (the pane is always the active tab
  // when it resolves).  No toast — one source of truth, no wording drift.
  const failResult = function (status) {
    if (status === 429)
      return { error: "Node at capacity — close a session and retry." };
    if (status === 403)
      return { error: "You don’t have permission to open this session." };
    return { error: "Could not open this session (" + status + ")." };
  };
  const routeFallback = function () {
    return authFetch("/v1/api/route?ws_id=" + encodeURIComponent(wsId))
      .then(function (r) {
        if (!r.ok) {
          return {
            error:
              r.status === 503
                ? "No nodes available to open this session."
                : "Could not locate the session node (" + r.status + ").",
          };
        }
        return r.json();
      })
      .then(function (route) {
        if (!route || route.error)
          return route || { error: "Could not locate the session node." };
        if (!route.node_id)
          return { error: "Could not locate the session node." };
        return openOn(route.node_id).then(function (res) {
          return res.nodeId ? res : failResult(res.status);
        });
      });
  };
  const flow = hintNodeId
    ? openOn(hintNodeId).then(function (res) {
        if (res.nodeId) return res;
        // Origin gone -> rendezvous re-home; capacity/permission surface as-is.
        if (res.status === 404 || res.status === 502) return routeFallback();
        return failResult(res.status);
      })
    : routeFallback();
  return flow.catch(function () {
    return { error: "Failed to open this session." };
  });
};
window.TS_APP.boot = function () {
  history.replaceState({ view: "home" }, "");
  _initSavedCoordTable(); // substrate modules have evaluated by boot time
  initLogin();
  // loadOverview fetches the cluster snapshot — both the node list AND
  // the active-coordinators list come from the same snapshot + SSE patch
  // pipeline (#9); the console pseudo-node carries coordinator
  // ws_created / ws_closed / cluster_state events.
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
};
