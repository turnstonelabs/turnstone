const messagesEl = document.getElementById("messages");
const inputEl = document.getElementById("input");
const sendBtn = document.getElementById("send-btn");
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
    '<h2 id="login-title">turnstone</h2>' +
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
      // Re-initialize: fetch workstreams, connect SSE
      authFetch("/api/workstreams")
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
            connectContentSSE(currentWsId);
          }
          connectGlobalSSE();
          showDashboard();
        });
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
    if (contentEvtSource) {
      contentEvtSource.close();
      contentEvtSource = null;
    }
    if (globalEvtSource) {
      globalEvtSource.close();
      globalEvtSource = null;
    }
    showLogin();
  });
}

// --- Dashboard helpers ---
var STATE_DISPLAY = {
  running: { symbol: "\u25b8", label: "run" },
  thinking: { symbol: "\u25cc", label: "think" },
  attention: { symbol: "\u25c6", label: "attn" },
  idle: { symbol: "\u00b7", label: "idle" },
  error: { symbol: "\u2716", label: "err" },
};
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
  if (seconds < 60) return seconds + "s";
  var min = Math.floor(seconds / 60);
  if (min < 60) return min + "m";
  var hr = Math.floor(min / 60);
  return hr + "h " + (min % 60) + "m";
}

// --- Theme ---
function toggleTheme() {
  var current = document.documentElement.dataset.theme;
  var next = current === "light" ? "" : "light";
  document.documentElement.dataset.theme = next;
  localStorage.setItem("turnstone-theme", next || "dark");
  updateThemeMenuItem();
}
function updateThemeMenuItem() {
  var isLight = document.documentElement.dataset.theme === "light";
  // Show the target state icon+label (what you will switch to)
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
(function () {
  if (localStorage.getItem("turnstone-theme") === "light")
    document.documentElement.dataset.theme = "light";
  updateThemeMenuItem();
})();

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

// --- Markdown rendering (basic regex, no external libs) ---
function renderMarkdown(text) {
  // Protect code blocks first
  const codeBlocks = [];
  text = text.replace(/```(\w*)\n([\s\S]*?)```/g, function (m, lang, code) {
    codeBlocks.push(
      '<pre><code class="lang-' +
        escapeHtml(lang) +
        '">' +
        escapeHtml(code.replace(/\n$/, "")) +
        "</code></pre>",
    );
    return "\x00CB" + (codeBlocks.length - 1) + "\x00";
  });

  // Protect inline code
  const inlineCodes = [];
  text = text.replace(/`([^`\n]+)`/g, function (m, code) {
    inlineCodes.push("<code>" + escapeHtml(code) + "</code>");
    return "\x00IC" + (inlineCodes.length - 1) + "\x00";
  });

  // Process block-level elements per line
  const lines = text.split("\n");
  const out = [];
  let inList = false;
  let listType = "";

  for (let i = 0; i < lines.length; i++) {
    let line = lines[i];

    // Horizontal rule
    if (/^(\*{3,}|-{3,}|_{3,})\s*$/.test(line)) {
      if (inList) {
        out.push(listType === "ul" ? "</ul>" : "</ol>");
        inList = false;
      }
      out.push("<hr>");
      continue;
    }

    // Headers
    const hm = line.match(/^(#{1,6})\s+(.+)/);
    if (hm) {
      if (inList) {
        out.push(listType === "ul" ? "</ul>" : "</ol>");
        inList = false;
      }
      const level = hm[1].length;
      out.push(
        "<h" + level + ">" + inlineMarkdown(hm[2]) + "</h" + level + ">",
      );
      continue;
    }

    // Blockquote
    if (line.startsWith("> ")) {
      if (inList) {
        out.push(listType === "ul" ? "</ul>" : "</ol>");
        inList = false;
      }
      out.push(
        "<blockquote>" + inlineMarkdown(line.slice(2)) + "</blockquote>",
      );
      continue;
    }

    // Unordered list
    const ulm = line.match(/^(\s*)[-*+]\s+(.+)/);
    if (ulm) {
      if (!inList || listType !== "ul") {
        if (inList) out.push(listType === "ul" ? "</ul>" : "</ol>");
        out.push("<ul>");
        inList = true;
        listType = "ul";
      }
      out.push("<li>" + inlineMarkdown(ulm[2]) + "</li>");
      continue;
    }

    // Ordered list
    const olm = line.match(/^(\s*)\d+[.)]\s+(.+)/);
    if (olm) {
      if (!inList || listType !== "ol") {
        if (inList) out.push(listType === "ul" ? "</ul>" : "</ol>");
        out.push("<ol>");
        inList = true;
        listType = "ol";
      }
      out.push("<li>" + inlineMarkdown(olm[2]) + "</li>");
      continue;
    }

    // Close list if we hit a non-list line
    if (inList && line.trim() === "") {
      out.push(listType === "ul" ? "</ul>" : "</ol>");
      inList = false;
    }

    // Paragraph / plain text
    if (line.trim() === "") {
      out.push("");
    } else {
      out.push("<p>" + inlineMarkdown(line) + "</p>");
    }
  }
  if (inList) out.push(listType === "ul" ? "</ul>" : "</ol>");

  let result = out.join("\n");

  // Restore code blocks and inline code
  result = result.replace(/\x00CB(\d+)\x00/g, function (m, idx) {
    return codeBlocks[parseInt(idx)];
  });
  result = result.replace(/\x00IC(\d+)\x00/g, function (m, idx) {
    return inlineCodes[parseInt(idx)];
  });

  return result;
}

function inlineMarkdown(text) {
  // Bold
  text = text.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
  text = text.replace(/__(.+?)__/g, "<strong>$1</strong>");
  // Italic
  text = text.replace(/\*(.+?)\*/g, "<em>$1</em>");
  text = text.replace(/_(.+?)_/g, "<em>$1</em>");
  // Strikethrough
  text = text.replace(/~~(.+?)~~/g, "<del>$1</del>");
  // Links
  text = text.replace(
    /\[([^\]]+)\]\(([^)]+)\)/g,
    '<a href="$2" target="_blank">$1</a>',
  );
  return text;
}

function escapeHtml(text) {
  const d = document.createElement("div");
  d.textContent = text;
  return d.innerHTML;
}

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
  busy = false;
  pendingApproval = false;
  approvalBlockEl = null;
  sendBtn.disabled = false;
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

function newWorkstream() {
  authFetch("/api/workstreams/new", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: "{}",
  })
    .then(function (r) {
      return r.json();
    })
    .then(function (data) {
      if (data.ws_id) {
        workstreams[data.ws_id] = { name: data.name, state: "idle" };
        switchTab(data.ws_id);
      }
    });
}

function closeWorkstream(wsId) {
  authFetch("/api/workstreams/close", {
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
    "/api/events?ws_id=" + encodeURIComponent(wsId),
  );
  contentEvtSource.onmessage = function (e) {
    contentRetryDelay = 1000;
    statusBar.classList.remove("disconnected");
    var data = JSON.parse(e.data);
    handleEvent(data);
  };
  contentEvtSource.onerror = function () {
    contentEvtSource.close();
    contentEvtSource = null;
    statusBar.textContent = "Reconnecting\u2026";
    statusBar.classList.add("disconnected");
    // Raw fetch (not authFetch) — need to inspect status before throwing
    fetch("/api/workstreams")
      .then(function (r) {
        if (r.status === 401) {
          showLogin();
          return;
        }
        setTimeout(function () {
          connectContentSSE(currentWsId);
        }, contentRetryDelay);
        contentRetryDelay = Math.min(contentRetryDelay * 2, 30000);
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
  document.getElementById("dashboard-session-cards").innerHTML =
    '<div class="dashboard-empty">Loading\u2026</div>';
  var dashP = authFetch("/api/dashboard").then(function (r) {
    return r.json();
  });
  var sessP = authFetch("/api/sessions").then(function (r) {
    return r.json();
  });
  Promise.all([dashP, sessP])
    .then(function (res) {
      var dashData = res[0];
      var wsList = dashData.workstreams || [];
      var agg = dashData.aggregate || {};
      renderDashboardTable(wsList, agg);
      // Collect active session IDs for dedup
      var activeSessionIds = {};
      wsList.forEach(function (ws) {
        if (ws.session_id) activeSessionIds[ws.session_id] = true;
      });
      var sessList = (res[1].sessions || []).filter(function (s) {
        return !activeSessionIds[s.session_id];
      });
      renderDashboardSessions(sessList);
    })
    .catch(function () {
      tableEl.innerHTML = '<div class="dashboard-empty">Failed to load</div>';
      document.getElementById("dashboard-session-cards").innerHTML =
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

    // NODE cell
    var nodeCell = document.createElement("span");
    nodeCell.className = "dash-cell-node";
    nodeCell.textContent = ws.node || "local";
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
}
function renderDashboardSessions(sessions) {
  var c = document.getElementById("dashboard-session-cards");
  c.innerHTML = "";
  if (!sessions.length) {
    c.innerHTML = '<div class="dashboard-empty">No saved sessions</div>';
    return;
  }
  sessions.forEach(function (sess) {
    var card = document.createElement("div");
    card.className = "dashboard-card";
    card.setAttribute("role", "button");
    card.setAttribute("tabindex", "0");
    var label = sess.alias || sess.title || sess.session_id;
    card.setAttribute("aria-label", "Resume: " + label);
    card.onclick = function () {
      dashboardResumeSession(sess.session_id);
    };
    card.onkeydown = function (e) {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        dashboardResumeSession(sess.session_id);
      }
    };
    var title = sess.alias || sess.title || sess.session_id.substring(0, 12);
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
function dashboardResumeSession(sessionId) {
  authFetch("/api/workstreams/new", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: "{}",
  })
    .then(function (r) {
      return r.json();
    })
    .then(function (data) {
      if (!data.ws_id) return;
      workstreams[data.ws_id] = { name: data.name, state: "idle" };
      switchTab(data.ws_id);
      hideDashboard();
      authFetch("/api/command", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          ws_id: data.ws_id,
          command: "/resume " + sessionId,
        }),
      }).catch(function (err) {
        addErrorMessage("Failed to resume: " + err.message);
      });
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
  authFetch("/api/workstreams/new", {
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
      busy = true;
      sendBtn.disabled = true;
      addUserMessage(text);
      authFetch("/api/send", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: text, ws_id: data.ws_id }),
      }).catch(function (err) {
        addErrorMessage("Connection error: " + err.message);
        busy = false;
        sendBtn.disabled = false;
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
  globalEvtSource = new EventSource("/api/events/global");
  globalEvtSource.onmessage = function (e) {
    globalRetryDelay = 1000;
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
    fetch("/api/workstreams")
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
      }
      currentAssistantEl = null;
      currentReasoningEl = null;
      contentBuffer = "";
      busy = false;
      sendBtn.disabled = false;
      inputEl.focus();
      scrollToBottom(true);
      break;

    case "tool_info":
      showInlineToolBlock(evt.items, true);
      break;

    case "approve_request":
      showInlineToolBlock(evt.items, false);
      break;

    case "tool_output_chunk":
      if (evt.call_id && evt.chunk) {
        appendToolOutputChunk(evt.call_id, evt.chunk);
      }
      break;

    case "tool_result":
      appendToolOutput(evt.name, evt.output);
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
      busy = false;
      sendBtn.disabled = false;
      break;

    case "busy_error":
      addErrorMessage(evt.message);
      busy = false;
      sendBtn.disabled = false;
      break;

    case "connected":
      modelName.textContent = evt.model || "";
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
          var block = document.createElement("div");
          block.className = "msg approval-block approved";
          msg.tool_calls.forEach(function (tc) {
            var div = document.createElement("div");
            div.className = "approval-tool";
            div.dataset.funcName = tc.name;
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
          badge.className = "approval-badge badge-approved";
          badge.textContent = "\u2713 approved";
          block.appendChild(badge);
          messagesEl.appendChild(block);
          lastToolBlock = block;
        }
      }
      if (msg.content) {
        var el = document.createElement("div");
        el.className = "msg msg-assistant";
        el.innerHTML = renderMarkdown(msg.content);
        messagesEl.appendChild(el);
        lastToolBlock = null;
      }
    } else if (msg.role === "tool") {
      if (lastToolBlock) {
        var stripped = stripAnsi(msg.content || "").trim();
        if (stripped) {
          var out = document.createElement("div");
          out.className = "tool-output";
          out.textContent = stripped;
          if (stripped.split("\\n").length > 10) {
            out.classList.add("collapsed");
            out.addEventListener("click", function () {
              this.classList.remove("collapsed");
            });
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

function showInlineToolBlock(items, autoApproved) {
  const block = document.createElement("div");
  block.className = "msg approval-block" + (autoApproved ? " approved" : "");
  if (!autoApproved) {
    block.setAttribute("role", "alertdialog");
    block.setAttribute("aria-label", "Tool approval required");
  }

  items.forEach(function (item) {
    block.appendChild(buildToolDiv(item));
  });

  if (autoApproved) {
    const badge = document.createElement("div");
    badge.className = "approval-badge badge-approved";
    badge.textContent = "\u2713 auto-approved";
    block.appendChild(badge);
  } else {
    const prompt = document.createElement("div");
    prompt.className = "approval-prompt";

    const actions = document.createElement("div");
    actions.className = "approval-actions";
    actions.innerHTML =
      '<button class="approval-btn btn-approve" onclick="resolveInlineApproval(true,false,getFeedback())"><span class="key">y</span> Approve</button>' +
      '<button class="approval-btn btn-deny" onclick="resolveInlineApproval(false,false,getFeedback())"><span class="key">n</span> Deny</button>' +
      '<button class="approval-btn btn-always" onclick="resolveInlineApproval(true,true,getFeedback())"><span class="key">a</span> Always</button>';
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

function resolveInlineApproval(approved, always, feedback) {
  if (!approvalBlockEl) return;
  pendingApproval = false;

  // Remove prompt
  const prompt = approvalBlockEl.querySelector(".approval-prompt");
  if (prompt) prompt.remove();

  // Add badge
  const badge = document.createElement("div");
  if (approved) {
    badge.className = "approval-badge badge-approved";
    var label = always ? "\u2713 always approve" : "\u2713 approved";
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

  // POST to server with ws_id
  authFetch("/api/approve", {
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

  scrollToBottom();
}

function appendToolOutputChunk(callId, chunk) {
  if (!chunk) return;
  var stripped = stripAnsi(chunk);
  if (!stripped) return;

  // Find or create a streaming output element keyed by call_id
  var el = messagesEl.querySelector(
    '.tool-output-stream[data-call-id="' + callId + '"]',
  );
  if (!el) {
    // Find the last approval-block and last bash tool div inside it
    var blocks = messagesEl.querySelectorAll(".approval-block");
    if (!blocks.length) return;
    var block = blocks[blocks.length - 1];
    var tools = block.querySelectorAll('.approval-tool[data-func-name="bash"]');
    var target = tools.length ? tools[tools.length - 1] : null;
    if (!target) {
      // Fallback to last tool div
      var allTools = block.querySelectorAll(".approval-tool");
      target = allTools.length ? allTools[allTools.length - 1] : null;
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

function appendToolOutput(name, output) {
  // Find the last approval-block in messages
  const blocks = messagesEl.querySelectorAll(".approval-block");
  if (!blocks.length) return;
  const block = blocks[blocks.length - 1];

  // Find the matching tool div or use the last one
  let target = null;
  const tools = block.querySelectorAll(".approval-tool");
  for (let i = tools.length - 1; i >= 0; i--) {
    if (tools[i].dataset.funcName === name) {
      target = tools[i];
      break;
    }
  }
  if (!target && tools.length) target = tools[tools.length - 1];
  if (!target) return;

  // Remove the streaming output element adjacent to this tool
  var streamEl = target.nextElementSibling;
  if (streamEl && streamEl.classList.contains("tool-output-stream")) {
    streamEl.remove();
  }

  const stripped = stripAnsi(output || "").trim();
  if (!stripped) return;

  const out = document.createElement("div");
  out.className = "tool-output";
  out.textContent = stripped;

  // Auto-collapse long output (keyboard-accessible)
  const lineCount = stripped.split("\n").length;
  if (lineCount > 10) {
    out.classList.add("collapsed");
    out.setAttribute("tabindex", "0");
    out.setAttribute("role", "button");
    out.setAttribute(
      "aria-label",
      "Tool output (collapsed). Activate to expand.",
    );
    var expandHandler = function () {
      this.classList.remove("collapsed");
      this.removeAttribute("tabindex");
      this.removeAttribute("role");
      this.removeAttribute("aria-label");
    };
    out.addEventListener("click", expandHandler);
    out.addEventListener("keydown", function (e) {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        expandHandler.call(this);
      }
    });
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
function showPlanDialog(content) {
  document.getElementById("plan-content").textContent = content;
  document.getElementById("plan-feedback").value = "";
  document.getElementById("plan-overlay").classList.add("active");
  setTimeout(function () {
    document.getElementById("plan-feedback").focus();
  }, 50);
}

function resolvePlan(defaultFeedback) {
  let feedback = document.getElementById("plan-feedback").value.trim();
  if (!feedback && defaultFeedback) feedback = defaultFeedback;
  document.getElementById("plan-overlay").classList.remove("active");
  inputEl.focus();
  authFetch("/api/plan", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ feedback: feedback, ws_id: currentWsId }),
  }).catch(function (err) {
    addErrorMessage("Connection error: " + err.message);
  });
}

// --- Send message ---
function sendMessage() {
  const text = inputEl.value.trim();
  if (!text || busy) return;

  if (text.startsWith("/")) {
    // Slash command
    authFetch("/api/command", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ command: text, ws_id: currentWsId }),
    });
    addUserMessage(text);
    inputEl.value = "";
    autoResize();
    return;
  }

  busy = true;
  sendBtn.disabled = true;
  addUserMessage(text);
  inputEl.value = "";
  autoResize();

  authFetch("/api/send", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message: text, ws_id: currentWsId }),
  }).catch(function (err) {
    addErrorMessage("Connection error: " + err.message);
    busy = false;
    sendBtn.disabled = false;
  });
}

// --- Textarea auto-resize and keyboard shortcuts ---
function autoResize() {
  inputEl.style.height = "auto";
  inputEl.style.height = Math.min(inputEl.scrollHeight, 200) + "px";
}

inputEl.addEventListener("input", autoResize);
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
      resolvePlan("reject");
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
    '<div class="kb-section">Workstreams</div>' +
    '<div class="kb-row"><span class="kb-desc">Toggle dashboard</span><span class="kb-key">Ctrl+D</span></div>' +
    '<div class="kb-row"><span class="kb-desc">New workstream</span><span class="kb-key">Ctrl+T</span></div>' +
    '<div class="kb-row"><span class="kb-desc">Close workstream</span><span class="kb-key">Ctrl+W</span></div>' +
    '<div class="kb-row"><span class="kb-desc">Switch to tab 1\u20139</span><span class="kb-key">Ctrl+1</span>\u2026<span class="kb-key">9</span></div>' +
    '<div class="kb-section">Tool approval</div>' +
    '<div class="kb-row"><span class="kb-desc">Approve</span><span class="kb-key">y</span> / <span class="kb-key">Enter</span></div>' +
    '<div class="kb-row"><span class="kb-desc">Deny</span><span class="kb-key">n</span> / <span class="kb-key">Esc</span></div>' +
    '<div class="kb-row"><span class="kb-desc">Always approve</span><span class="kb-key">a</span></div>' +
    '<div class="kb-section">Chat</div>' +
    '<div class="kb-row"><span class="kb-desc">Send message</span><span class="kb-key">Enter</span></div>' +
    '<div class="kb-row"><span class="kb-desc">New line</span><span class="kb-key">Shift+Enter</span></div>' +
    '<div class="kb-section">Navigation</div>' +
    '<div class="kb-row"><span class="kb-desc">Navigate table rows</span><span class="kb-key">\u2191</span> <span class="kb-key">\u2193</span></div>' +
    '<div class="kb-row"><span class="kb-desc">Close dashboard / menu</span><span class="kb-key">Esc</span></div>' +
    '<div class="kb-section">General</div>' +
    '<div class="kb-row"><span class="kb-desc">Show this help</span><span class="kb-key">?</span></div>' +
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
  if (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA") return;
  var login = document.getElementById("login-overlay");
  if (login && login.style.display !== "none") return;
  if (e.key === "?" && !e.ctrlKey && !e.metaKey) {
    e.preventDefault();
    showKbHelp();
    return;
  }
  if (e.key === "Escape") {
    var kb = document.getElementById("kb-overlay");
    if (kb) {
      e.preventDefault();
      hideKbHelp();
      return;
    }
  }
});

// --- Init: fetch workstream list, then connect ---
initLogin();
authFetch("/api/workstreams")
  .then(function (r) {
    return r.json();
  })
  .then(function (data) {
    data.workstreams.forEach(function (ws) {
      workstreams[ws.id] = { name: ws.name, state: ws.state };
    });
    // Default to first workstream
    var wsIds = Object.keys(workstreams);
    if (wsIds.length) {
      currentWsId = wsIds[0];
      renderTabBar();
      connectContentSSE(currentWsId);
    }
    connectGlobalSSE();
    // Seed the history stack so back-from-workstream returns here.
    history.replaceState({ turnstone: "dashboard" }, "");
    showDashboard();
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
