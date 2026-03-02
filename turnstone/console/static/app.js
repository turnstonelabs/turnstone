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
  var nodesP = authFetch("/api/cluster/nodes?sort=activity&limit=50").then(
    function (r) {
      return r.json();
    },
  );
  Promise.all([overviewP, nodesP])
    .then(function (res) {
      renderStateCards(res[0].states);
      renderAggregateBar(res[0]);
      renderNodeTable(res[1].nodes, res[1].total);
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

function renderStateCards(states) {
  var container = document.getElementById("state-cards");
  container.innerHTML = "";
  STATE_ORDER.forEach(function (state) {
    var count = states[state] || 0;
    var sd = STATE_DISPLAY[state] || STATE_DISPLAY.idle;
    var card = document.createElement("div");
    card.className = "state-card";
    card.dataset.state = state;
    card.setAttribute("role", "button");
    card.setAttribute("tabindex", "0");
    card.setAttribute("aria-label", sd.label + ": " + count + " workstreams");
    card.innerHTML =
      '<div class="state-card-count">' +
      formatCount(count) +
      "</div>" +
      '<div class="state-card-label">' +
      sd.symbol +
      " " +
      sd.label +
      "</div>";
    card.onclick = function () {
      drillDownByState(state);
    };
    card.onkeydown = function (e) {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        drillDownByState(state);
      }
    };
    container.appendChild(card);
  });
}

function renderAggregateBar(overview) {
  var agg = overview.aggregate || {};
  var parts = [];
  if (agg.total_tokens) parts.push(formatTokens(agg.total_tokens) + " tokens");
  if (agg.total_tool_calls)
    parts.push(formatCount(agg.total_tool_calls) + " tool calls");
  document.getElementById("aggregate-bar").textContent = parts.join(" \u00b7 ");
}

function renderNodeTable(nodes, total) {
  var table = document.getElementById("node-table");
  table.innerHTML = "";
  if (!nodes.length) {
    table.innerHTML = '<div class="dashboard-empty">No nodes discovered</div>';
    return;
  }
  nodes.forEach(function (node) {
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

    // Use aggregate tokens, fall back to summed workstream tokens
    var displayTokens = node.total_tokens || node.ws_tokens || 0;

    // Load = workstream count / max capacity
    var maxWs = node.max_ws || 10;
    var healthPct = Math.round((node.ws_total / maxWs) * 100);
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
      '<span class="node-cell node-cell-health">' +
      '<span class="health-bar">' +
      healthFillHtml +
      "</span>" +
      " " +
      healthPct +
      "%" +
      "</span>";

    row.onclick = function () {
      drillDownToNode(node.node_id, node.server_url);
    };
    row.onkeydown = function (e) {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        drillDownToNode(node.node_id, node.server_url);
      }
    };
    table.appendChild(row);
  });

  // Pagination hint
  var pag = document.getElementById("node-pagination");
  pag.innerHTML = "";
  if (total > nodes.length) {
    pag.textContent = "Showing " + nodes.length + " of " + total + " nodes";
  }
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
  loadNodeDetail(nodeId);
  document.getElementById("breadcrumb-home").focus();
  history.pushState({ view: "node", nodeId: nodeId, serverUrl: serverUrl }, "");
}

function loadNodeDetail(nodeId) {
  authFetch("/api/cluster/node/" + encodeURIComponent(nodeId))
    .then(function (r) {
      return r.json();
    })
    .then(function (data) {
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
  authFetch("/api/cluster/workstreams?" + params)
    .then(function (r) {
      return r.json();
    })
    .then(function (data) {
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
