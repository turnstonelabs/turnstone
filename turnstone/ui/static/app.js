// ===========================================================================
//  turnstone server UI — app.js
//  Split-pane layout that hosts per-workstream Pane instances (binary layout
//  tree, tab bar, global stream, dashboard, new-workstream modal).
// ===========================================================================

// ===========================================================================
//  1. Pane class — moved to shared_static/interactive.js
//
//  The per-workstream conversational Pane now lives in the shared module
//  (window.InteractivePane), so the console L-shell can mount the same pane
//  over a node-proxied transport.  This shell constructs it via createPane()
//  below with the standalone host adapter (section "Layout tree").
// ===========================================================================

// ===========================================================================
//  2. Layout tree + rendering
// ===========================================================================

const panes = {};
let focusedPaneId = null;
let splitRoot = null;
const MAX_PANES = 6;

function getFocusedPane() {
  return panes[focusedPaneId] || null;
}

function setFocusedPane(paneId) {
  if (focusedPaneId === paneId) return;
  // Remove focused class from old pane
  if (focusedPaneId && panes[focusedPaneId]) {
    panes[focusedPaneId].el.classList.remove("focused");
  }
  focusedPaneId = paneId;
  if (panes[paneId]) {
    panes[paneId].el.classList.add("focused");
    currentWsId = panes[paneId].wsId;
    renderTabBar();
  }
}

// The interactive Pane now lives in the shared module (window.InteractivePane).
// This split-pane shell supplies the host adapter for the seams only it knows:
// workstream display names, which pane is focused (focus-steal guard), the
// stream-error recovery policy (refetch the ws list + reassign across panes),
// and the target for the --skip-permissions banner.
const STANDALONE_HOST = {
  getWsName(wsId) {
    return (workstreams[wsId] && workstreams[wsId].name) || null;
  },
  isFocused(pane) {
    return pane.id === focusedPaneId;
  },
  onStreamError(pane) {
    if (pane.id === focusedPaneId) refetchWorkstreamsAndReassign(pane);
  },
  warningTarget() {
    return document.getElementById("ui-header");
  },
  onConsentDetected(server) {
    _onConsentDetected(server);
  },
};

function refetchWorkstreamsAndReassign(focusedPane) {
  // Lifted from the pre-PR-D ``onerror`` body.  Triggered when the
  // focused pane sees its EventSource enter the error state — pulls
  // the authoritative workstream list and reassigns stale wsIds.
  // Survives the onerror refactor as a separate concern from the
  // SSE reconnect mechanics: native EventSource handles the same-
  // workstream reconnect; this handles the workstream-evicted-
  // during-disconnect recovery.
  fetch("/v1/api/workstreams")
    .then((r) => {
      if (r.status === 401) {
        showLogin();
        return;
      }
      return r.json().then((data) => {
        workstreams = {};
        (data.workstreams || []).forEach((ws) => {
          workstreams[ws.ws_id] = { name: ws.name, state: ws.state };
        });
        renderTabBar();
        // Two passes: (1) reassign stale panes, (2) reconnect any
        // that ended up in CLOSED state.  Native reconnect covers
        // CONNECTING -> OPEN transitions transparently.
        const remaining = Object.keys(workstreams);
        if (!remaining.length) {
          showDashboard();
          return;
        }
        const usedWsIds = {};
        for (let pid in panes) {
          if (panes[pid].wsId && workstreams[panes[pid].wsId])
            usedWsIds[panes[pid].wsId] = true;
        }
        for (let pid2 in panes) {
          const p2 = panes[pid2];
          if (p2.wsId && !workstreams[p2.wsId]) {
            let newWsId = null;
            for (let ri = 0; ri < remaining.length; ri++) {
              if (!usedWsIds[remaining[ri]]) {
                newWsId = remaining[ri];
                break;
              }
            }
            if (newWsId) {
              p2.disconnectSSE();
              // Different workstream → drop saved id; replay is
              // per-ws so an id from ws-A is meaningless on ws-B.
              p2._lastEventId = null;
              p2.wsId = newWsId;
              usedWsIds[newWsId] = true;
              while (p2.messagesEl.firstChild)
                p2.messagesEl.removeChild(p2.messagesEl.firstChild);
              p2.showEmptyState();
              p2.updateWsName();
            }
            // else: more panes than workstreams — leave pane stale,
            // connectSSE below picks it up or stays disconnected.
          }
        }
        // Pass 2: reconnect any pane whose EventSource ended up
        // truly CLOSED (not just transient — native reconnect
        // handles CONNECTING / OPEN).
        for (let pid3 in panes) {
          const p3 = panes[pid3];
          if (pid3 === focusedPaneId) currentWsId = p3.wsId;
          const dead =
            !p3.evtSource || p3.evtSource.readyState === EventSource.CLOSED;
          if (dead && p3.wsId && workstreams[p3.wsId]) {
            setTimeout(
              ((pp) => {
                return () => {
                  pp._loadHistoryThenConnect(pp.wsId);
                };
              })(p3),
              focusedPane.retryDelay,
            );
          }
        }
        focusedPane.retryDelay = Math.min(focusedPane.retryDelay * 2, 30000);
      });
    })
    .catch(() => {
      // Fetch failed (network) — schedule a same-pane reconnect
      // fallback in case the EventSource is genuinely dead.
      setTimeout(() => {
        if (
          !focusedPane.evtSource ||
          focusedPane.evtSource.readyState === EventSource.CLOSED
        ) {
          focusedPane.connectSSE(focusedPane.wsId);
        }
      }, focusedPane.retryDelay);
      focusedPane.retryDelay = Math.min(focusedPane.retryDelay * 2, 30000);
    });
}

function createPane(wsId) {
  const p = new window.InteractivePane(wsId, { host: STANDALONE_HOST });
  panes[p.id] = p;
  return p;
}

function updatePaneHeaders() {
  const root = document.getElementById("split-root");
  const leafCount = countLeaves(splitRoot);
  if (leafCount > 1) {
    root.classList.add("multi-pane");
  } else {
    root.classList.remove("multi-pane");
  }
  // Hide tab-bar split button when already in multi-pane mode
  const splitBtn = document.getElementById("split-btn");
  if (splitBtn) {
    if (leafCount > 1) {
      splitBtn.classList.add("hidden");
    } else {
      splitBtn.classList.remove("hidden");
    }
  }
}

function splitFocusedPane() {
  if (focusedPaneId) splitPane(focusedPaneId, "horizontal");
}

// --- Tree helpers ---

function findLeafAndParent(node, paneId, parent, childIndex) {
  if (!node) return null;
  if (node.type === "leaf") {
    if (node.pane.id === paneId) {
      return { node: node, parent: parent, childIndex: childIndex };
    }
    return null;
  }
  // split
  for (let i = 0; i < node.children.length; i++) {
    const result = findLeafAndParent(node.children[i], paneId, node, i);
    if (result) return result;
  }
  return null;
}

function countLeaves(node) {
  if (!node) return 0;
  if (node.type === "leaf") return 1;
  let count = 0;
  for (let i = 0; i < node.children.length; i++) {
    count += countLeaves(node.children[i]);
  }
  return count;
}

function getFirstLeaf(node) {
  if (!node) return null;
  if (node.type === "leaf") return node.pane;
  return getFirstLeaf(node.children[0]);
}

function replaceNode(tree, target, replacement) {
  if (tree === target) return replacement;
  if (tree.type === "split") {
    for (let i = 0; i < tree.children.length; i++) {
      if (tree.children[i] === target) {
        tree.children[i] = replacement;
        return tree;
      }
      const result = replaceNode(tree.children[i], target, replacement);
      if (result !== tree.children[i]) {
        tree.children[i] = result;
        return tree;
      }
    }
  }
  return tree;
}

function splitPane(paneId, direction) {
  if (countLeaves(splitRoot) >= MAX_PANES) return;
  // Guard: viewport too narrow/short to fit another pane
  const root = document.getElementById("split-root");
  const minDim = direction === "horizontal" ? 200 : 150;
  const available =
    direction === "horizontal" ? root.clientWidth : root.clientHeight;
  if (available < minDim * 2 + 4) {
    showToast("Not enough space to split");
    return;
  }
  const found = findLeafAndParent(splitRoot, paneId, null, -1);
  if (!found) return;

  // Find a workstream not already shown in any pane
  const wsIds = Object.keys(workstreams);
  let newWsId = null;
  for (let i = 0; i < wsIds.length; i++) {
    let inUse = false;
    for (let pid in panes) {
      if (panes[pid].wsId === wsIds[i]) {
        inUse = true;
        break;
      }
    }
    if (!inUse) {
      newWsId = wsIds[i];
      break;
    }
  }
  if (!newWsId) {
    showToast("No unused workstreams \u2014 create one first");
    return;
  }

  const newPane = createPane(newWsId);
  const newLeaf = { type: "leaf", pane: newPane };
  const newSplit = {
    type: "split",
    direction: direction,
    children: [found.node, newLeaf],
    ratio: 0.5,
  };

  splitRoot = replaceNode(splitRoot, found.node, newSplit);
  renderLayout();
  setFocusedPane(newPane.id);
  newPane.showEmptyState();
  newPane._loadHistoryThenConnect(newWsId);
}

function closePane(paneId) {
  if (countLeaves(splitRoot) <= 1) return;
  let found = findLeafAndParent(splitRoot, paneId, null, -1);
  if (!found || !found.parent) {
    // paneId is the root leaf — shouldn't happen if count > 1
    // but handle: root must be a split
    if (splitRoot.type === "split") {
      // Find which child contains our pane
      for (let ci = 0; ci < splitRoot.children.length; ci++) {
        const childFound = findLeafAndParent(
          splitRoot.children[ci],
          paneId,
          splitRoot,
          ci,
        );
        if (childFound) {
          found = childFound;
          break;
        }
      }
    }
    if (!found || !found.parent) return;
  }

  // Sibling is the other child
  const siblingIdx = found.childIndex === 0 ? 1 : 0;
  const sibling = found.parent.children[siblingIdx];

  // Replace parent split with sibling
  splitRoot = replaceNode(splitRoot, found.parent, sibling);

  // Cleanup the closed pane
  const closedPane = panes[paneId];
  if (closedPane) {
    closedPane.disconnectSSE();
    delete panes[paneId];
  }

  // If focused pane was closed, focus first available
  if (focusedPaneId === paneId) {
    const first = getFirstLeaf(splitRoot);
    if (first) {
      focusedPaneId = null; // reset so setFocusedPane triggers
      setFocusedPane(first.id);
    }
  }

  renderLayout();
}

function renderLayout() {
  const root = document.getElementById("split-root");

  // Save scroll positions before clearing
  const scrollPositions = {};
  for (let pid in panes) {
    scrollPositions[pid] = panes[pid].messagesEl.scrollTop;
  }

  // Clear and rebuild
  root.replaceChildren();
  if (splitRoot) {
    _renderLayoutNode(splitRoot, root);
  }

  // Restore scroll positions
  for (let pid2 in panes) {
    if (scrollPositions[pid2] !== undefined) {
      panes[pid2].messagesEl.scrollTop = scrollPositions[pid2];
    }
  }

  updatePaneHeaders();
  saveLayout();
}

function _renderLayoutNode(node, container) {
  if (node.type === "leaf") {
    container.appendChild(node.pane.el);
    return;
  }

  // split node
  const splitContainer = document.createElement("div");
  splitContainer.className = "split-container split-" + node.direction;

  const child0 = document.createElement("div");
  child0.className = "split-child";
  child0.style.flex = String(node.ratio);
  _renderLayoutNode(node.children[0], child0);
  splitContainer.appendChild(child0);

  const handle = document.createElement("div");
  handle.className = "split-handle";
  handle.setAttribute("role", "separator");
  handle.setAttribute("tabindex", "0");
  handle.setAttribute(
    "aria-orientation",
    node.direction === "horizontal" ? "vertical" : "horizontal",
  );
  handle.setAttribute("aria-valuenow", Math.round(node.ratio * 100));
  handle.setAttribute("aria-valuemin", "10");
  handle.setAttribute("aria-valuemax", "90");
  handle.setAttribute(
    "aria-label",
    node.direction === "horizontal"
      ? "Resize panes horizontally"
      : "Resize panes vertically",
  );
  splitContainer.appendChild(handle);

  const child1 = document.createElement("div");
  child1.className = "split-child";
  child1.style.flex = String(1 - node.ratio);
  _renderLayoutNode(node.children[1], child1);
  splitContainer.appendChild(child1);

  container.appendChild(splitContainer);
  setupDragHandle(handle, node, [child0, child1]);
}

function _dragBounds(node, handle) {
  // Compute min/max ratio from container size and CSS min dimensions
  const container = handle.parentElement;
  const totalSize =
    node.direction === "horizontal"
      ? container.clientWidth
      : container.clientHeight;
  const minPx = node.direction === "horizontal" ? 200 : 150; // match CSS min-width/min-height
  const handlePx = 4;
  const usable = totalSize - handlePx;
  const minRatio = usable > 0 ? Math.max(0.05, minPx / usable) : 0.1;
  const maxRatio = usable > 0 ? Math.min(0.95, 1 - minPx / usable) : 0.9;
  return { minRatio: minRatio, maxRatio: maxRatio, totalSize: totalSize };
}

function _applyRatio(node, children, handle, ratio) {
  node.ratio = ratio;
  children[0].style.flex = String(ratio);
  children[1].style.flex = String(1 - ratio);
  if (handle) {
    handle.setAttribute("aria-valuenow", Math.round(ratio * 100));
  }
}

function setupDragHandle(handle, node, children) {
  handle.addEventListener("pointerdown", function (e) {
    if (e.button !== 0 && e.pointerType === "mouse") return;
    e.preventDefault();
    handle.setPointerCapture(e.pointerId);
    handle.classList.add("dragging");
    const startRatio = node.ratio;
    const bounds = _dragBounds(node, handle);
    const startPos = node.direction === "horizontal" ? e.clientX : e.clientY;
    document.body.style.cursor =
      node.direction === "horizontal" ? "col-resize" : "row-resize";
    document.body.style.userSelect = "none";

    const onMove = function (e2) {
      const delta =
        (node.direction === "horizontal" ? e2.clientX : e2.clientY) - startPos;
      const newRatio = Math.max(
        bounds.minRatio,
        Math.min(bounds.maxRatio, startRatio + delta / bounds.totalSize),
      );
      _applyRatio(node, children, handle, newRatio);
    };
    const onUp = function () {
      handle.classList.remove("dragging");
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
      handle.removeEventListener("pointermove", onMove);
      handle.removeEventListener("pointerup", onUp);
      handle.removeEventListener("pointercancel", onUp);
      saveLayout();
    };
    handle.addEventListener("pointermove", onMove);
    handle.addEventListener("pointerup", onUp);
    handle.addEventListener("pointercancel", onUp);
  });

  // Keyboard resizing (arrow keys)
  handle.addEventListener("keydown", function (e) {
    const bounds = _dragBounds(node, handle);
    const step = e.shiftKey ? 0.1 : 0.02;
    let delta = 0;
    if (e.key === "ArrowRight" || e.key === "ArrowDown") delta = step;
    else if (e.key === "ArrowLeft" || e.key === "ArrowUp") delta = -step;
    else if (e.key === "Home") delta = -(node.ratio - bounds.minRatio);
    else if (e.key === "End") delta = bounds.maxRatio - node.ratio;
    else return;
    e.preventDefault();
    const newRatio = Math.max(
      bounds.minRatio,
      Math.min(bounds.maxRatio, node.ratio + delta),
    );
    _applyRatio(node, children, handle, newRatio);
    saveLayout();
  });
}

// ===========================================================================
//  3. Layout persistence
// ===========================================================================

function serializeLayout(node) {
  if (!node) return null;
  if (node.type === "leaf") {
    return { type: "leaf", wsId: node.pane.wsId };
  }
  return {
    type: "split",
    direction: node.direction,
    ratio: node.ratio,
    children: [
      serializeLayout(node.children[0]),
      serializeLayout(node.children[1]),
    ],
  };
}

function deserializeLayout(data, _seen) {
  if (!_seen) _seen = {};
  if (!data) return null;
  if (data.type === "leaf") {
    if (!data.wsId || !workstreams[data.wsId] || _seen[data.wsId]) return null;
    if (Object.keys(panes).length >= MAX_PANES) return null;
    _seen[data.wsId] = true;
    const p = createPane(data.wsId);
    return { type: "leaf", pane: p };
  }
  if (data.type === "split") {
    const left = deserializeLayout(data.children[0], _seen);
    const right = deserializeLayout(data.children[1], _seen);
    if (!left && !right) return null;
    if (!left) return right;
    if (!right) return left;
    return {
      type: "split",
      direction: data.direction || "horizontal",
      ratio: data.ratio || 0.5,
      children: [left, right],
    };
  }
  return null;
}

function saveLayout() {
  try {
    const data = serializeLayout(splitRoot);
    if (data) {
      localStorage.setItem("turnstone_split_layout", JSON.stringify(data));
    }
  } catch (e) {
    // localStorage may be unavailable
  }
}

function restoreLayout() {
  try {
    const raw = localStorage.getItem("turnstone_split_layout");
    if (!raw) return false;
    const data = JSON.parse(raw);
    const tree = deserializeLayout(data);
    if (!tree) return false;
    splitRoot = tree;
    const first = getFirstLeaf(splitRoot);
    if (first) {
      setFocusedPane(first.id);
    }
    return true;
  } catch (e) {
    return false;
  }
}

// ===========================================================================
//  3b. Pane context menu
// ===========================================================================

let _ctxMenu = null;
let _ctxCloseHandler = null;
let _ctxTriggerElement = null;

let _tabDropdown = null;
let _tabDropdownCloseHandler = null;
let _tabDropdownTrigger = null;

function showPaneContextMenu(x, y, paneId) {
  closeTabDropdown();
  closePaneContextMenu();
  _ctxTriggerElement = document.activeElement;

  const menu = document.createElement("div");
  menu.className = "pane-ctx-menu";
  menu.setAttribute("role", "menu");
  menu.setAttribute("aria-label", "Pane actions");
  menu.addEventListener("contextmenu", function (e) {
    e.preventDefault();
  });

  const canClose = splitRoot && countLeaves(splitRoot) > 1;
  // Can split only if under pane limit AND there's an unused workstream
  const usedWs = {};
  for (let pid in panes) usedWs[panes[pid].wsId] = true;
  const hasUnused = Object.keys(workstreams).some(function (id) {
    return !usedWs[id];
  });
  const canSplit = countLeaves(splitRoot) < MAX_PANES && hasUnused;

  const items = [
    {
      label: "Split Right",
      key: "Ctrl+\\",
      disabled: !canSplit,
      action: function () {
        splitPane(paneId, "horizontal");
      },
    },
    {
      label: "Split Down",
      key: "Ctrl+Shift+\\",
      disabled: !canSplit,
      action: function () {
        splitPane(paneId, "vertical");
      },
    },
    { separator: true },
    {
      label: "Close Pane",
      key: "Ctrl+Shift+W",
      disabled: !canClose,
      action: function () {
        closePane(paneId);
      },
    },
  ];

  items.forEach(function (item) {
    if (item.separator) {
      const sep = document.createElement("div");
      sep.className = "pane-ctx-sep";
      sep.setAttribute("role", "separator");
      menu.appendChild(sep);
      return;
    }
    const btn = document.createElement("button");
    btn.className = "pane-ctx-item";
    btn.setAttribute("role", "menuitem");
    btn.setAttribute("tabindex", "-1");
    btn.disabled = !!item.disabled;
    const labelSpan = document.createElement("span");
    labelSpan.className = "pane-ctx-label";
    labelSpan.textContent = item.label;
    btn.appendChild(labelSpan);
    if (item.key) {
      const keySpan = document.createElement("span");
      keySpan.className = "pane-ctx-key";
      keySpan.textContent = item.key;
      btn.appendChild(keySpan);
    }
    btn.onclick = function () {
      closePaneContextMenu();
      item.action();
    };
    menu.appendChild(btn);
  });

  // Position: ensure menu stays within viewport
  document.body.appendChild(menu);
  const rect = menu.getBoundingClientRect();
  let mx = x;
  let my = y;
  if (mx + rect.width > window.innerWidth)
    mx = window.innerWidth - rect.width - 4;
  if (my + rect.height > window.innerHeight)
    my = window.innerHeight - rect.height - 4;
  if (mx < 0) mx = 4;
  if (my < 0) my = 4;
  menu.style.left = mx + "px";
  menu.style.top = my + "px";
  _ctxMenu = menu;

  // Close on click outside, Escape, Tab; arrow key navigation
  _ctxCloseHandler = function (e) {
    if (e.type === "keydown") {
      if (e.key === "Escape" || e.key === "Tab") {
        e.preventDefault();
        closePaneContextMenu();
      } else if (
        e.key === "ArrowDown" ||
        e.key === "ArrowUp" ||
        e.key === "Home" ||
        e.key === "End"
      ) {
        e.preventDefault();
        const btns = Array.from(
          menu.querySelectorAll(".pane-ctx-item:not(:disabled)"),
        );
        if (!btns.length) return;
        const idx = btns.indexOf(document.activeElement);
        if (e.key === "ArrowDown") btns[(idx + 1) % btns.length].focus();
        else if (e.key === "ArrowUp")
          btns[(idx - 1 + btns.length) % btns.length].focus();
        else if (e.key === "Home") btns[0].focus();
        else if (e.key === "End") btns[btns.length - 1].focus();
      }
    } else if (e.type === "mousedown" && !menu.contains(e.target)) {
      closePaneContextMenu();
    }
  };
  setTimeout(function () {
    document.addEventListener("mousedown", _ctxCloseHandler);
    document.addEventListener("keydown", _ctxCloseHandler);
    // Focus first enabled item
    const first = menu.querySelector(".pane-ctx-item:not(:disabled)");
    if (first) first.focus();
  }, 0);
}

function closePaneContextMenu() {
  if (_ctxMenu) {
    _ctxMenu.remove();
    _ctxMenu = null;
  }
  if (_ctxCloseHandler) {
    document.removeEventListener("mousedown", _ctxCloseHandler);
    document.removeEventListener("keydown", _ctxCloseHandler);
    _ctxCloseHandler = null;
  }
  if (_ctxTriggerElement && document.contains(_ctxTriggerElement)) {
    _ctxTriggerElement.focus();
    _ctxTriggerElement = null;
  }
}

// ---------------------------------------------------------------------------
//  3c. Tab dropdown menu (per-tab workstream actions)
// ---------------------------------------------------------------------------

function showTabDropdown(chevronEl, wsId) {
  closePaneContextMenu();
  closeTabDropdown();
  _tabDropdownTrigger = chevronEl;
  chevronEl.setAttribute("aria-expanded", "true");

  const menu = document.createElement("div");
  menu.className = "ws-tab-dropdown";
  menu.setAttribute("role", "menu");
  menu.setAttribute("aria-label", "Workstream actions");
  menu.addEventListener("contextmenu", function (e) {
    e.preventDefault();
  });

  const isLastWs = Object.keys(workstreams).length <= 1;
  const items = [
    {
      label: "Refresh title",
      cls: "mobile-hide",
      action: function () {
        refreshWorkstreamTitle(wsId);
      },
    },
    {
      label: "Edit title",
      key: "Ctrl+Shift+E",
      action: function () {
        editWorkstreamTitle(wsId);
      },
    },
    {
      label: "Fork",
      key: "Ctrl+Shift+F",
      action: function () {
        forkWorkstream(wsId);
      },
    },
    {
      label: "Export conversation",
      action: function () {
        exportWorkstreamDownload(wsId);
      },
    },
    {
      label: "Close",
      key: "Ctrl+W",
      disabled: isLastWs,
      action: function () {
        closeWorkstream(wsId);
      },
    },
    { separator: true },
    {
      label: "Delete",
      key: "Ctrl+Shift+X",
      cls: "destructive",
      disabled: isLastWs,
      action: function () {
        confirmDeleteWorkstream(wsId);
      },
    },
  ];

  items.forEach(function (item) {
    if (item.separator) {
      const sep = document.createElement("div");
      sep.className = "ws-tab-dropdown-sep";
      sep.setAttribute("role", "separator");
      menu.appendChild(sep);
      return;
    }
    const btn = document.createElement("button");
    btn.className = "ws-tab-dropdown-item" + (item.cls ? " " + item.cls : "");
    btn.setAttribute("role", "menuitem");
    btn.setAttribute("tabindex", "-1");
    if (item.disabled) {
      btn.setAttribute("aria-disabled", "true");
      btn.setAttribute(
        "title",
        "Cannot " + item.label.toLowerCase() + " the last workstream",
      );
    }
    const labelSpan = document.createElement("span");
    labelSpan.className = "ws-tab-dropdown-label";
    labelSpan.textContent = item.label;
    btn.appendChild(labelSpan);
    if (item.key) {
      const keySpan = document.createElement("span");
      keySpan.className = "ws-tab-dropdown-key";
      keySpan.textContent = item.key;
      keySpan.setAttribute("aria-hidden", "true");
      btn.appendChild(keySpan);
    }
    btn.onclick = function () {
      if (this.getAttribute("aria-disabled") === "true") return;
      closeTabDropdown();
      item.action();
    };
    menu.appendChild(btn);
  });

  document.body.appendChild(menu);

  // Position below chevron, right-aligned
  const cr = chevronEl.getBoundingClientRect();
  const mr = menu.getBoundingClientRect();
  let mx = cr.right - mr.width;
  let my = cr.bottom + 2;
  if (mx < 0) mx = 4;
  if (my + mr.height > window.innerHeight) my = cr.top - mr.height - 2;
  if (mx + mr.width > window.innerWidth) mx = window.innerWidth - mr.width - 4;
  menu.style.left = mx + "px";
  menu.style.top = my + "px";
  _tabDropdown = menu;

  // Keyboard handler is mirrored by the console node-picker shim in
  // turnstone/console/server.py (search for closeHandler in _JS_PROXY_SHIM).
  // If you change the keys or filter selector here, change them there.
  _tabDropdownCloseHandler = function (e) {
    if (e.type === "keydown") {
      if (e.key === "Escape" || e.key === "Tab") {
        e.preventDefault();
        closeTabDropdown();
      } else if (
        e.key === "ArrowDown" ||
        e.key === "ArrowUp" ||
        e.key === "Home" ||
        e.key === "End"
      ) {
        e.preventDefault();
        const btns = Array.from(menu.querySelectorAll(".ws-tab-dropdown-item"));
        if (!btns.length) return;
        const idx = btns.indexOf(document.activeElement);
        if (e.key === "ArrowDown") btns[(idx + 1) % btns.length].focus();
        // idx <= 0 covers both "first item" (wrap to last) and "no
        // current focus" (idx === -1, which would otherwise yield
        // len-2 via the modulo).  Same shape as openSettingsMenu and
        // the proxy node-picker (turnstone/console/server.py:275).
        else if (e.key === "ArrowUp")
          btns[idx <= 0 ? btns.length - 1 : idx - 1].focus();
        else if (e.key === "Home") btns[0].focus();
        else if (e.key === "End") btns[btns.length - 1].focus();
      }
    } else if (
      e.type === "mousedown" &&
      !menu.contains(e.target) &&
      e.target !== chevronEl
    ) {
      closeTabDropdown();
    }
  };
  const closeHandler = _tabDropdownCloseHandler;
  const activeMenu = menu;
  setTimeout(function () {
    if (_tabDropdown !== activeMenu || !closeHandler) return;
    document.addEventListener("mousedown", closeHandler);
    document.addEventListener("keydown", closeHandler);
    const first = activeMenu.querySelector(".ws-tab-dropdown-item");
    if (first) first.focus();
  }, 0);
}

function closeTabDropdown() {
  if (_tabDropdown) {
    _tabDropdown.remove();
    _tabDropdown = null;
  }
  if (_tabDropdownCloseHandler) {
    document.removeEventListener("mousedown", _tabDropdownCloseHandler);
    document.removeEventListener("keydown", _tabDropdownCloseHandler);
    _tabDropdownCloseHandler = null;
  }
  if (_tabDropdownTrigger) {
    _tabDropdownTrigger.setAttribute("aria-expanded", "false");
    if (document.contains(_tabDropdownTrigger)) {
      _tabDropdownTrigger.focus();
    }
    _tabDropdownTrigger = null;
  }
}

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
  for (let id in panes) {
    panes[id].disconnectSSE();
    delete panes[id];
  }
  splitRoot = null;
  focusedPaneId = null;
  workstreams = {};
  currentWsId = null;
  document.getElementById("split-root").replaceChildren();
  if (globalEvtSource) {
    globalEvtSource.close();
    globalEvtSource = null;
  }
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
//  8. Tab bar
// ===========================================================================

const tabBar = document.getElementById("tab-bar");
const tabList = document.getElementById("tab-list");
const newTabBtn = document.getElementById("new-tab-btn");

function renderTabBar() {
  closeTabDropdown();
  tabList.querySelectorAll(".ws-tab").forEach(function (t) {
    t.remove();
  });

  const wsIds = Object.keys(workstreams);
  wsIds.forEach(function (wsId) {
    const ws = workstreams[wsId];
    const tab = document.createElement("div");
    tab.className = "ws-tab" + (wsId === currentWsId ? " active" : "");
    tab.dataset.wsId = wsId;
    tab.setAttribute("role", "tab");
    tab.setAttribute("tabindex", "0");
    tab.setAttribute("aria-selected", wsId === currentWsId ? "true" : "false");
    tab.onclick = function (e) {
      if (e.target.classList.contains("tab-chevron")) return;
      switchTab(wsId);
    };
    tab.onkeydown = function (e) {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        switchTab(wsId);
      }
    };

    const indicator = document.createElement("span");
    indicator.className = "tab-indicator";
    indicator.dataset.state = ws.state || "idle";
    indicator.setAttribute("aria-label", ws.state || "idle");
    tab.appendChild(indicator);

    const name = document.createElement("span");
    name.className = "tab-name";
    name.textContent = ws.name || wsId.substring(0, 6);
    tab.appendChild(name);

    const wsidBadge = document.createElement("span");
    wsidBadge.className = "tab-wsid";
    wsidBadge.textContent = wsId.substring(0, 7);
    tab.appendChild(wsidBadge);

    const chevron = document.createElement("button");
    chevron.className = "tab-chevron";
    chevron.textContent = "\u25BE";
    chevron.title = "Workstream actions";
    chevron.setAttribute(
      "aria-label",
      "Actions for " + (ws.name || wsId.substring(0, 6)),
    );
    chevron.setAttribute("aria-haspopup", "menu");
    chevron.setAttribute("aria-expanded", "false");
    chevron.onclick = function (e) {
      e.stopPropagation();
      if (_tabDropdown && _tabDropdownTrigger === chevron) {
        closeTabDropdown();
      } else {
        showTabDropdown(chevron, wsId);
      }
    };
    tab.appendChild(chevron);

    tabList.appendChild(tab);
  });
}

function updateTabIndicator(wsId, state, extra) {
  workstreams[wsId] = workstreams[wsId] || {};
  workstreams[wsId].state = state;
  const tab = tabBar.querySelector('.ws-tab[data-ws-id="' + wsId + '"]');
  if (tab) {
    const ind = tab.querySelector(".tab-indicator");
    if (ind) ind.dataset.state = state;
  }
  const row = document.querySelector(
    '#dash-ws-table .dash-row[data-ws-id="' + wsId + '"]',
  );
  if (row) {
    const sd = STATE_DISPLAY[state] || STATE_DISPLAY.idle;
    row.dataset.state = state;
    const dot = row.querySelector(".dash-state-dot");
    if (dot) dot.dataset.state = state;
    const label = row.querySelector(".dash-state-label");
    if (label) {
      label.dataset.state = state;
      label.textContent = sd.symbol + " " + sd.label;
    }
    if (extra) {
      if (extra.tokens !== undefined) {
        const tokEl = row.querySelector(".dash-cell-tokens");
        if (tokEl) tokEl.textContent = formatTokens(extra.tokens);
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
  }
}

function switchTab(wsId) {
  closeTabDropdown();
  let pane = getFocusedPane();
  if (!pane) {
    // Bootstrap the first pane on a fresh-loaded page that had no
    // workstreams to render at init time. Without this, creating
    // or opening a workstream from the dashboard left switchTab
    // with nowhere to attach: it early-returned, no SSE connected,
    // the chat UI showed nothing, and only a refresh fixed it
    // (initWorkstreams creates the pane on a now-populated
    // workstreams list). Mirrors the bootstrap block in
    // initWorkstreams; renderLayout fires once so the pane DOM is
    // attached before the rest of switchTab connects SSE.
    pane = createPane(wsId);
    splitRoot = { type: "leaf", pane: pane };
    setFocusedPane(pane.id);
    renderLayout();
  }
  if (wsId === pane.wsId && !dashboardVisible) return;

  // Track last active for close_tab_action
  if (pane.wsId && workstreams[pane.wsId]) {
    _lastActiveWsId = pane.wsId;
  }

  // In multi-pane mode, focus an existing pane showing this ws
  if (splitRoot && countLeaves(splitRoot) > 1) {
    for (let pid in panes) {
      if (panes[pid].wsId === wsId && pid !== focusedPaneId) {
        setFocusedPane(pid);
        return;
      }
    }
  }

  pane.disconnectSSE();
  pane.reset();
  pane.wsId = wsId;
  currentWsId = wsId;
  while (pane.messagesEl.firstChild)
    pane.messagesEl.removeChild(pane.messagesEl.firstChild);
  pane.showEmptyState();
  pane.updateWsName();
  renderTabBar();
  pane._loadHistoryThenConnect(wsId);

  if (!_historyNavigation) {
    history.pushState({ turnstone: "workstream", wsId: wsId }, "");
  }
}

// ===========================================================================
//  9. New workstream modal
// ===========================================================================

let _newWsTrapHandler = null;
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

function _newWsAddFiles(files) {
  const errEl = document.getElementById("new-ws-error");
  for (let i = 0; i < files.length; i++) {
    const f = files[i];
    if (_newWsStagedFiles.length >= _NEW_WS_MAX_FILES) {
      errEl.textContent =
        "At most " + _NEW_WS_MAX_FILES + " attachments per workstream";
      errEl.style.display = "block";
      return;
    }
    if (!_isAttachmentAllowed(f)) {
      errEl.textContent =
        "Unsupported file type: " +
        f.name +
        " (allowed: png/jpeg/gif/webp images, text)";
      errEl.style.display = "block";
      return;
    }
    const isImage = (f.type || "").indexOf("image/") === 0;
    const cap = isImage ? _NEW_WS_IMAGE_CAP : _NEW_WS_TEXT_CAP;
    if (f.size > cap) {
      errEl.textContent =
        f.name + " exceeds the " + _formatAttachSize(cap) + " cap";
      errEl.style.display = "block";
      return;
    }
    _newWsStagedFiles.push(f);
  }
  errEl.style.display = "none";
  _newWsRenderChips();
}

function newWorkstream() {
  showNewWsModal();
}

function showNewWsModal(forkFromWsId) {
  _forkFromWsId = forkFromWsId || "";
  const overlay = document.getElementById("new-ws-overlay");
  overlay.style.display = "flex";
  document.body.style.overflow = "hidden";

  // Update title and button text based on mode
  const titleEl = document.getElementById("new-ws-title");
  const submitBtn = document.getElementById("new-ws-submit");
  if (_forkFromWsId) {
    titleEl.textContent = "Fork Workstream";
    submitBtn.textContent = "Fork";
  } else {
    titleEl.textContent = "New Workstream";
    submitBtn.textContent = "Create";
  }

  // Hide skill dropdown when forking (not relevant — fork copies history)
  const skillLabel = document.querySelector('label[for="new-ws-skill"]');
  const skillSelect = document.getElementById("new-ws-skill");
  if (_forkFromWsId) {
    if (skillLabel) skillLabel.style.display = "none";
    if (skillSelect) skillSelect.style.display = "none";
  } else {
    if (skillLabel) skillLabel.style.display = "";
    if (skillSelect) skillSelect.style.display = "";
  }

  overlay.onclick = function (e) {
    if (e.target === overlay) hideNewWsModal();
  };

  // Populate model dropdown
  const modelSelect = document.getElementById("new-ws-model");
  const judgeSelect = document.getElementById("new-ws-judge-model");
  const fp = getFocusedPane();
  const curModel = fp ? fp.modelAlias || fp.model || "" : "";
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

  document.getElementById("new-ws-name").value = "";
  const initEl = document.getElementById("new-ws-initial-message");
  if (initEl) initEl.value = "";
  const errEl = document.getElementById("new-ws-error");
  errEl.style.display = "none";
  errEl.textContent = "";
  submitBtn.disabled = false;

  // Reset attachment staging.  Forks don't carry attachments —
  // disable the attach UI in that case (the fork inherits its
  // parent's history; new attachments go on the next manual send).
  _newWsStagedFiles = [];
  const attachRow = document.getElementById("new-ws-attach-row");
  const attachInput = document.getElementById("new-ws-attach-input");
  const attachBtn = document.getElementById("new-ws-attach-btn");
  if (attachRow) attachRow.style.display = _forkFromWsId ? "none" : "";
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

  document.getElementById("new-ws-cancel").onclick = hideNewWsModal;
  submitBtn.onclick = submitNewWs;

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
    const box = document.getElementById("new-ws-box");
    const focusable = box.querySelectorAll(
      'input, select, button, [tabindex]:not([tabindex="-1"])',
    );
    if (!focusable.length) return;
    const first = focusable[0],
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
  _forkFromWsId = "";
  document.getElementById("new-ws-overlay").style.display = "none";
  document.body.style.overflow = "";
  if (_newWsTrapHandler) {
    document.removeEventListener("keydown", _newWsTrapHandler);
    _newWsTrapHandler = null;
  }
  document.getElementById("new-tab-btn").focus();
}

function submitNewWs() {
  const submitBtn = document.getElementById("new-ws-submit");
  if (submitBtn.disabled) return;
  submitBtn.disabled = true;
  submitBtn.textContent = _forkFromWsId ? "Forking\u2026" : "Creating\u2026";

  const body = {};
  const name = document.getElementById("new-ws-name").value.trim();
  const model = document.getElementById("new-ws-model").value.trim();
  const judge_model = document
    .getElementById("new-ws-judge-model")
    .value.trim();
  const skill = document.getElementById("new-ws-skill").value;
  const initEl = document.getElementById("new-ws-initial-message");
  const initial_message = initEl ? initEl.value.trim() : "";
  if (name) body.name = name;
  if (model) body.model = model;
  if (judge_model) body.judge_model = judge_model;
  if (skill && !_forkFromWsId) body.skill = skill;
  if (_forkFromWsId) body.resume_ws = _forkFromWsId;
  if (initial_message) body.initial_message = initial_message;

  const errEl = document.getElementById("new-ws-error");
  errEl.style.display = "none";

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
      if (data.error) {
        errEl.textContent = data.error;
        errEl.style.display = "block";
        submitBtn.disabled = false;
        submitBtn.textContent = _forkFromWsId ? "Fork" : "Create";
        return;
      }
      if (data.ws_id) {
        workstreams[data.ws_id] = { name: data.name, state: "idle" };
        _newWsStagedFiles = [];
        hideNewWsModal();
        switchTab(data.ws_id);
      }
    })
    .catch(function () {
      errEl.textContent = _forkFromWsId
        ? "Failed to fork workstream"
        : "Failed to create workstream";
      errEl.style.display = "block";
      submitBtn.disabled = false;
      submitBtn.textContent = _forkFromWsId ? "Fork" : "Create";
    });
}

function _reassignPanesForClosedWs(closedWsId, tabIdsBeforeClose) {
  const remaining = Object.keys(workstreams);
  // Collect panes showing the closed ws
  const affected = [];
  for (let pid in panes) {
    if (panes[pid].wsId === closedWsId) affected.push(pid);
  }
  if (!affected.length) return;

  // Determine target ws based on close_tab_action setting
  let action = "last_used";
  try {
    action =
      localStorage.getItem("turnstone_interface.close_tab_action") ||
      "last_used";
  } catch (_) {}

  if (action === "dashboard" && remaining.length > 0) {
    // Show dashboard, but still need to reassign panes to valid ws
    for (let di = 0; di < affected.length; di++) {
      const dp = panes[affected[di]];
      dp.disconnectSSE();
      if (remaining.length) {
        dp.wsId = remaining[0];
        dp.messagesEl.replaceChildren();
        dp.showEmptyState();
        dp.updateWsName();
        dp._loadHistoryThenConnect(remaining[0]);
      }
    }
    if (focusedPaneId && panes[focusedPaneId]) {
      currentWsId = panes[focusedPaneId].wsId;
    }
    renderTabBar();
    showDashboard();
    loadDashboard();
    return;
  }

  // Determine preferred target ws_id
  let preferredWsId = null;
  if (action === "last_used") {
    if (
      _lastActiveWsId &&
      _lastActiveWsId !== closedWsId &&
      workstreams[_lastActiveWsId]
    ) {
      preferredWsId = _lastActiveWsId;
    }
  } else if (action === "nearest_left" || action === "nearest_right") {
    const idx = tabIdsBeforeClose ? tabIdsBeforeClose.indexOf(closedWsId) : -1;
    if (idx >= 0) {
      if (action === "nearest_left") {
        // Walk left, then right
        for (let li = idx - 1; li >= 0; li--) {
          if (workstreams[tabIdsBeforeClose[li]]) {
            preferredWsId = tabIdsBeforeClose[li];
            break;
          }
        }
        if (!preferredWsId) {
          for (let ri = idx + 1; ri < tabIdsBeforeClose.length; ri++) {
            if (workstreams[tabIdsBeforeClose[ri]]) {
              preferredWsId = tabIdsBeforeClose[ri];
              break;
            }
          }
        }
      } else {
        // Walk right, then left
        for (let ri2 = idx + 1; ri2 < tabIdsBeforeClose.length; ri2++) {
          if (workstreams[tabIdsBeforeClose[ri2]]) {
            preferredWsId = tabIdsBeforeClose[ri2];
            break;
          }
        }
        if (!preferredWsId) {
          for (let li2 = idx - 1; li2 >= 0; li2--) {
            if (workstreams[tabIdsBeforeClose[li2]]) {
              preferredWsId = tabIdsBeforeClose[li2];
              break;
            }
          }
        }
      }
    }
  }

  // Build set of ws_ids already shown by non-affected panes
  const usedWsIds = {};
  for (let pid2 in panes) {
    if (affected.indexOf(pid2) === -1) usedWsIds[panes[pid2].wsId] = true;
  }

  for (let i = 0; i < affected.length; i++) {
    const p = panes[affected[i]];
    // Try the preferred ws first, then fall back to first unused
    let newWsId = null;
    if (preferredWsId && !usedWsIds[preferredWsId]) {
      newWsId = preferredWsId;
    } else {
      for (let j = 0; j < remaining.length; j++) {
        if (!usedWsIds[remaining[j]]) {
          newWsId = remaining[j];
          break;
        }
      }
    }
    if (newWsId) {
      // Reassign pane to the target workstream
      p.disconnectSSE();
      p.wsId = newWsId;
      p.messagesEl.replaceChildren();
      p.showEmptyState();
      p.updateWsName();
      p._loadHistoryThenConnect(newWsId);
      usedWsIds[newWsId] = true;
    } else if (countLeaves(splitRoot) > 1) {
      // No unused workstream available — close redundant pane
      closePane(affected[i]);
    } else {
      // Last pane — reassign to first remaining ws (will duplicate, but no choice)
      p.disconnectSSE();
      if (remaining.length) {
        p.wsId = remaining[0];
        p.messagesEl.replaceChildren();
        p.showEmptyState();
        p.updateWsName();
        p._loadHistoryThenConnect(remaining[0]);
      }
    }
  }
  if (focusedPaneId && panes[focusedPaneId]) {
    currentWsId = panes[focusedPaneId].wsId;
  }
  renderTabBar();
  if (currentWsId && workstreams[currentWsId]) {
    switchTab(currentWsId);
  }
}

function closeWorkstream(wsId) {
  // Capture tab order from DOM (visual order) before deletion for close_tab_action=nearest_left/right
  const tabIdsBeforeClose = Array.from(
    document.querySelectorAll("#tab-list .ws-tab"),
  ).map(function (tab) {
    return tab.dataset.wsId;
  });

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
        renderTabBar();
        _reassignPanesForClosedWs(wsId, tabIdsBeforeClose);
        const remaining = Object.keys(workstreams);
        if (remaining.length === 0) {
          loadDashboard();
          showDashboard();
        }
      } else if (data.error) {
        showToast(data.error, "warning");
      }
    });
}

// ===========================================================================
//  10. Dashboard
// ===========================================================================

function showDashboard() {
  dashboardVisible = true;
  document.getElementById("dashboard").classList.add("active");
  // ui-header stays interactive while the dashboard is open so the
  // theme toggle, settings menu, and the console proxy's node-picker
  // pill remain reachable.  See .dashboard-overlay { top: 48px } in
  // style.css for the matching layout offset.
  document.getElementById("tab-bar").inert = true;
  document.getElementById("split-root").inert = true;
  loadDashboard();
  _loadDashboardOptionsLists();
  _restoreDashboardOptionsState();
  _refreshDashboardOptionsSummary();
  _refreshDashboardSubmitLabel();
  setTimeout(function () {
    document.getElementById("dashboard-input").focus();
  }, 50);
}

function hideDashboard() {
  dashboardVisible = false;
  document.getElementById("dashboard").classList.remove("active");
  document.getElementById("tab-bar").inert = false;
  document.getElementById("split-root").inert = false;
  document.getElementById("dashboard-input").value = "";
  _dashboardStagedFiles = [];
  _renderDashboardChips();
  _refreshDashboardSubmitLabel();
  const pane = getFocusedPane();
  if (pane) pane.inputEl.focus();
}

function toggleDashboard() {
  if (dashboardVisible) hideDashboard();
  else showDashboard();
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
  Promise.all([dashP, sessP])
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
    nameCell.textContent = liveName;
    main.appendChild(nameCell);

    const modelCell = document.createElement("span");
    modelCell.className = "dash-cell-model";
    modelCell.textContent = ws.model_alias || ws.model || "";
    if (ws.model) modelCell.title = ws.model;
    main.appendChild(modelCell);

    const nodeCell = document.createElement("span");
    nodeCell.className = "dash-cell-node";
    nodeCell.textContent = ws.node || "local";
    if (ws.node) nodeCell.title = ws.node;
    main.appendChild(nodeCell);

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
const WS_COLUMNS = [
  SavedColumns.name(),
  SavedColumns.model(),
  SavedColumns.count("message_count", "MSGS"),
  SavedColumns.ctx(),
  SavedColumns.last(),
  SavedColumns.id(),
];
const _wsTable = createSavedTable({
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
function cancelWsDelete() {
  _wsTable.controller.closeModal();
}
function confirmWsDelete() {
  _wsTable.controller.confirm();
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

let _editTitleTrap = null;

function editWorkstreamTitle(optWsId) {
  const wsId = optWsId || getCurrentWsId();
  if (!wsId) return;
  let currentTitle = "";
  const tabEl = document.querySelector(
    '.ws-tab[data-ws-id="' + wsId + '"] .tab-name',
  );
  if (tabEl) currentTitle = tabEl.textContent.trim();

  const overlay = document.getElementById("edit-title-overlay");
  const input = document.getElementById("edit-title-input");
  input.value = currentTitle;
  overlay.style.display = "flex";
  overlay.onclick = function (e) {
    if (e.target === overlay) cancelEditTitle();
  };

  // Focus trap + Escape
  if (_editTitleTrap) document.removeEventListener("keydown", _editTitleTrap);
  _editTitleTrap = function (e) {
    if (e.key === "Escape") {
      e.preventDefault();
      cancelEditTitle();
      return;
    }
    if (e.key === "Tab") {
      const box = document.getElementById("edit-title-box");
      const focusable = box.querySelectorAll("input, button");
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    }
  };
  document.addEventListener("keydown", _editTitleTrap);

  setTimeout(function () {
    input.focus();
    input.select();
  }, 50);
}

function cancelEditTitle() {
  document.getElementById("edit-title-overlay").style.display = "none";
  if (_editTitleTrap) {
    document.removeEventListener("keydown", _editTitleTrap);
    _editTitleTrap = null;
  }
  const chevron = document.querySelector(".ws-tab.active .tab-chevron");
  if (chevron) chevron.focus();
}

function submitEditTitle() {
  const wsId = getCurrentWsId();
  if (!wsId) return;
  const input = document.getElementById("edit-title-input");
  const newTitle = input.value.trim();
  if (!newTitle) {
    showToast("Title cannot be empty", "warning");
    return;
  }

  const url = "/v1/api/workstreams/" + encodeURIComponent(wsId) + "/title";

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
      showToast(err.message || "Failed to set title", "error");
    });
}

// --- Workstream deletion ---

let _pendingDeleteWsId = null;
let _deleteWsTrap = null;

function confirmDeleteWorkstream(optWsId) {
  const wsId = optWsId || getCurrentWsId();
  if (!wsId) return;
  if (Object.keys(workstreams).length <= 1) return;
  const tabEl = document.querySelector(
    '.ws-tab[data-ws-id="' + wsId + '"] .tab-name',
  );
  const name = tabEl ? tabEl.textContent.trim() : wsId.substring(0, 12);

  _pendingDeleteWsId = wsId;
  const overlay = document.getElementById("delete-ws-overlay");
  const msg = document.getElementById("delete-ws-message");
  msg.textContent = 'Delete "' + name + '"? This cannot be undone.';
  overlay.style.display = "flex";

  // Focus trap + Escape
  if (_deleteWsTrap) document.removeEventListener("keydown", _deleteWsTrap);
  _deleteWsTrap = function (e) {
    if (e.key === "Escape") {
      e.preventDefault();
      cancelDeleteWs();
      return;
    }
    if (e.key === "Tab") {
      const box = document.getElementById("delete-ws-box");
      const focusable = box.querySelectorAll("button");
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    }
  };
  document.addEventListener("keydown", _deleteWsTrap);

  const cancelBtn = overlay.querySelector("button");
  if (cancelBtn) cancelBtn.focus();
}

function cancelDeleteWs() {
  _pendingDeleteWsId = null;
  document.getElementById("delete-ws-overlay").style.display = "none";
  if (_deleteWsTrap) {
    document.removeEventListener("keydown", _deleteWsTrap);
    _deleteWsTrap = null;
  }
  const chevron = document.querySelector(".ws-tab.active .tab-chevron");
  if (chevron) {
    chevron.focus();
  } else {
    const fallback = document.getElementById("new-tab-btn");
    if (fallback) fallback.focus();
  }
}

function executeDeleteWs() {
  const wsId = _pendingDeleteWsId;
  if (!wsId) return;
  cancelDeleteWs();

  const url = "/v1/api/workstreams/" + encodeURIComponent(wsId) + "/delete";

  authFetch(url, { method: "POST" })
    .then(function (r) {
      if (!r.ok)
        throw new Error("Failed to delete workstream (HTTP " + r.status + ")");
      return r.json();
    })
    .then(function () {
      // Update local state directly — don't call closeWorkstream which
      // would send a redundant POST to /close for an already-deleted ws.
      delete workstreams[wsId];
      renderTabBar();
      _reassignPanesForClosedWs(wsId, []);
      if (!Object.keys(workstreams).length) {
        loadDashboard();
        showDashboard();
      }
      showToast("Workstream deleted", "success");
    })
    .catch(function (err) {
      showToast(err.message || "Failed to delete workstream", "error");
    });
}

function getCurrentWsId() {
  const activeTab = document.querySelector(".ws-tab.active");
  if (activeTab) return activeTab.dataset.wsId || "";
  return "";
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
  if (model) body.model = model;
  if (judge) body.judge_model = judge;
  if (skill) body.skill = skill;
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
      workstreams[data.ws_id] = { name: data.name, state: "idle" };
      switchTab(data.ws_id);
      hideDashboard();
      // If we sent an initial_message, the server's worker thread already
      // dispatched it. Echo into the pane so the user sees their own text
      // immediately rather than waiting for SSE to backfill.
      if (text) {
        const pane = getFocusedPane();
        if (pane) {
          pane.setBusy(true);
          pane.addUserMessage(text);
        }
      }
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
    const data = JSON.parse(e.data);
    if (data.type === "ws_state") {
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
      // Update ALL matching tab elements (not just first one)
      const nameEls = document.querySelectorAll(
        '[data-ws-id="' + data.ws_id + '"] .tab-name',
      );
      nameEls.forEach(function (el) {
        el.textContent = data.name;
      });
      // Update all panes showing this workstream
      for (let id in panes) {
        if (panes[id].wsId === data.ws_id) panes[id].updateWsName();
      }
    } else if (data.type === "ws_created") {
      workstreams[data.ws_id] = workstreams[data.ws_id] || {};
      workstreams[data.ws_id].name = data.name || data.ws_id.slice(0, 6);
      workstreams[data.ws_id].state = "idle";
      renderTabBar();
    } else if (data.type === "ws_closed") {
      const wsId = data.ws_id;
      // Capture tab order from DOM (visual order) before deletion for close_tab_action=nearest_left/right
      const sseTabIds = Array.from(
        document.querySelectorAll("#tab-list .ws-tab"),
      ).map(function (tab) {
        return tab.dataset.wsId;
      });
      // Disconnect per-ws SSE on affected panes immediately so stale
      // events from the dying workstream don't leak into reassigned panes.
      for (let cid in panes) {
        if (panes[cid].wsId === wsId) panes[cid].disconnectSSE();
      }
      delete workstreams[wsId];
      renderTabBar();
      if (data.reason === "evicted") {
        showToast(
          "Evicted" + (data.name ? ": " + data.name : "") + " (capacity)",
        );
      }
      _reassignPanesForClosedWs(wsId, sseTabIds);
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
//  12. MCP consent badge (standalone settings-gear pending-consent indicator)
//
//  The tool-output / media / MCP-error / verdict renderers that used to live in
//  this section moved to shared_static/interactive.js with the Pane.  What
//  stays here is the standalone consent-badge subsystem: the gear badge lives
//  in this shell's header, so the pane only notifies it (host.onConsentDetected
//  -> _onConsentDetected) and the dashboard hydrates it via loadPendingConsents.
// ===========================================================================

// Module-level set of servers with an unresolved consent prompt; drives the
// gear-icon badge so the user has a stable signal that re-consent is pending
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

function _refreshConsentBadge() {
  const btn = document.getElementById("settings-btn");
  if (!btn) return;
  let existing = btn.querySelector(".settings-consent-badge");
  const n = _pendingConsentServers.size;
  // Keep the visible badge and the accessible name in lockstep so screen-
  // reader users get the same pending-consent signal that sighted users
  // get from the red dot. The badge itself stays aria-hidden because the
  // count is already reflected in the button's aria-label/title.
  if (n === 0) {
    if (existing) existing.remove();
    btn.setAttribute("aria-label", "Settings");
    btn.setAttribute("title", "Settings");
    return;
  }
  if (!existing) {
    existing = document.createElement("span");
    existing.className = "settings-consent-badge";
    existing.setAttribute("aria-hidden", "true");
    btn.appendChild(existing);
  }
  existing.textContent = String(n);
  const label =
    "Settings (" + n + " MCP consent" + (n === 1 ? "" : "s") + " pending)";
  btn.setAttribute("aria-label", label);
  btn.setAttribute("title", label);
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
// ---------------------------------------------------------------------------
//  HLS lazy-loader (follows the mermaid.js lazy-load pattern in
//  /shared/renderer.js)
// ---------------------------------------------------------------------------
let _hlsState = "idle";
let _hlsQueue = [];

function _loadHls(callback) {
  if (_hlsState === "ready") {
    callback();
    return;
  }
  _hlsQueue.push(callback);
  if (_hlsState === "loading") return;
  _hlsState = "loading";
  const script = document.createElement("script");
  script.src = "/shared/hls-1.6.16/hls.min.js";
  script.onload = function () {
    _hlsState = "ready";
    const q = _hlsQueue;
    _hlsQueue = [];
    for (let i = 0; i < q.length; i++) q[i]();
  };
  script.onerror = function () {
    _hlsState = "idle";
    const q = _hlsQueue;
    _hlsQueue = [];
    // Fall through — _activatePlayer will use stream_url since Hls is undefined
    for (let i = 0; i < q.length; i++) q[i]();
  };
  document.head.appendChild(script);
}

function _isHlsUrl(url) {
  return typeof url === "string" && /\.m3u8(\?|$)/i.test(url);
}

// ---------------------------------------------------------------------------
//  Click-to-play delegated handler (follows img-placeholder pattern)
// ---------------------------------------------------------------------------
function _activatePlayer(btn) {
  const url = btn.dataset.streamUrl;
  const hlsUrl = btn.dataset.hlsUrl;
  const isAudio = btn.dataset.audioOnly === "true";
  const directStream = btn.dataset.directStream === "true";

  const player = document.createElement(isAudio ? "audio" : "video");
  player.controls = true;
  player.autoplay = true;
  player.className = "media-player";

  // Prefer direct stream when the source supports it; fall back to HLS
  // only when transcoding is needed.
  if (directStream && url) {
    player.src = url;
  } else if (
    hlsUrl &&
    !isAudio &&
    typeof Hls !== "undefined" &&
    Hls.isSupported()
  ) {
    const hls = new Hls();
    hls.loadSource(hlsUrl);
    hls.attachMedia(player);
  } else if (
    hlsUrl &&
    !isAudio &&
    player.canPlayType("application/vnd.apple.mpegurl")
  ) {
    player.src = hlsUrl;
  } else {
    player.src = url;
  }

  player.addEventListener("error", function () {
    const card = player.closest(".media-embed");
    const titleEl = card ? card.querySelector(".media-card-title") : null;
    const label = titleEl ? ": " + titleEl.textContent : "";

    const err = document.createElement("div");
    err.className = "media-player-error";
    err.setAttribute("role", "alert");
    err.textContent = "Failed to load stream" + label;

    const retry = document.createElement("button");
    retry.className = "media-play-btn";
    retry.type = "button";
    retry.dataset.streamUrl = url;
    retry.dataset.hlsUrl = hlsUrl || "";
    retry.dataset.audioOnly = String(isAudio);
    retry.dataset.directStream = String(directStream);
    retry.setAttribute("aria-label", "Retry" + label);
    retry.appendChild(document.createTextNode("\u25b6 Retry"));

    const container = document.createElement("div");
    container.appendChild(err);
    container.appendChild(retry);
    player.replaceWith(container);
  });

  btn.replaceWith(player);
}

document.addEventListener("click", function (e) {
  const btn = e.target.closest(".media-play-btn");
  if (!btn) return;
  e.preventDefault();
  btn.disabled = true;
  const labelEl = btn.querySelector("span:last-child");
  if (labelEl) {
    labelEl.textContent = "Loading\u2026";
  } else {
    btn.textContent = "\u25b6 Loading\u2026";
  }

  const hlsUrl = btn.dataset.hlsUrl;
  const isAudio = btn.dataset.audioOnly === "true";

  // If HLS URL present and not audio, ensure hls.js is loaded first
  if (hlsUrl && !isAudio && _isHlsUrl(hlsUrl)) {
    _loadHls(function () {
      _activatePlayer(btn);
    });
  } else {
    _activatePlayer(btn);
  }
});

document.addEventListener("keydown", function (e) {
  if (e.key !== "Enter") return;
  const btn = e.target.closest(".media-play-btn");
  if (!btn) return;
  btn.click();
});

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
let _settingsTrap = null;
let _revokeMcpTrap = null;
let _settingsReturnFocus = null;

function openSettingsPanel() {
  const overlay = document.getElementById("settings-overlay");
  if (!overlay) return;
  _settingsReturnFocus = document.activeElement;
  overlay.style.display = "flex";

  if (_settingsTrap) document.removeEventListener("keydown", _settingsTrap);
  _settingsTrap = function (e) {
    if (e.key === "Escape") {
      // If the nested revoke confirmation is open, let its own trap
      // handle Escape — closing inner-first matches the delete-ws flow.
      const inner = document.getElementById("revoke-mcp-overlay");
      if (inner && inner.style.display !== "none") return;
      e.preventDefault();
      closeSettingsPanel();
      return;
    }
    if (e.key === "Tab") {
      const box = document.getElementById("settings-box");
      if (!box) return;
      const focusable = box.querySelectorAll(
        "button, [href], input, select, textarea, [tabindex]:not([tabindex='-1'])",
      );
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    }
  };
  document.addEventListener("keydown", _settingsTrap);

  loadMcpConnections();

  const closeBtn = document.getElementById("settings-close-btn");
  if (closeBtn) closeBtn.focus();
}

function closeSettingsPanel() {
  // If the nested revoke confirmation is still up, tear it down first
  // — otherwise hiding the parent panel would leave an orphan modal
  // overlay floating with its own keydown trap still attached. The
  // Escape-key path inside the parent's keydown trap defers to the
  // inner trap; this branch is the close-button path that doesn't go
  // through that trap.
  const inner = document.getElementById("revoke-mcp-overlay");
  if (inner && inner.style.display !== "none") {
    cancelRevokeMcp();
  }
  const overlay = document.getElementById("settings-overlay");
  if (overlay) overlay.style.display = "none";
  if (_settingsTrap) {
    document.removeEventListener("keydown", _settingsTrap);
    _settingsTrap = null;
  }
  if (
    _settingsReturnFocus &&
    typeof _settingsReturnFocus.focus === "function"
  ) {
    try {
      _settingsReturnFocus.focus();
    } catch (_) {}
  }
  _settingsReturnFocus = null;
}

// ---------------------------------------------------------------------------
//  Settings menu (gear icon dropdown — MCP connections + Logout)
// ---------------------------------------------------------------------------
//
// Reuses the .ws-tab-dropdown shell for visual + behavioural consistency
// with the workstream tab dropdown and the console proxy's node-picker.
// Keyboard handling matches the proxy node-picker (the APG-correct
// reference): Tab closes the menu WITHOUT preventDefault so focus
// moves naturally to the next focusable; Escape closes + refocuses
// the trigger.  showTabDropdown collapses Tab and Escape into a
// single preventDefault branch — that's a pre-existing divergence,
// tracked as a follow-up to align showTabDropdown to APG.  ArrowUp
// uses an `idx <= 0` guard (not modulo) so the no-focus case wraps
// to the last item rather than the second-to-last — same shape as
// showTabDropdown and the proxy node-picker.

let _settingsMenu = null;
let _settingsMenuCloseHandler = null;
// Cached at open time so closeSettingsMenu can reset ARIA without
// re-querying by id, and so the menu-item click path can refocus
// the trigger BEFORE close — that way openSettingsPanel captures
// the gear (not <body>) as _settingsReturnFocus.
let _settingsMenuTrigger = null;

function toggleSettingsMenu(triggerEl) {
  if (_settingsMenu) closeSettingsMenu();
  else openSettingsMenu(triggerEl);
}

function openSettingsMenu(triggerEl) {
  if (_settingsMenu) return;
  _settingsMenuTrigger = triggerEl;
  triggerEl.setAttribute("aria-expanded", "true");
  triggerEl.setAttribute("aria-controls", "settings-menu");

  const menu = document.createElement("div");
  menu.id = "settings-menu";
  menu.className = "ws-tab-dropdown";
  menu.setAttribute("role", "menu");
  menu.setAttribute("aria-label", "Settings");
  menu.addEventListener("contextmenu", function (e) {
    e.preventDefault();
  });

  const pendingCount = _pendingConsentServers.size;
  const items = [
    {
      label:
        "MCP connections" + (pendingCount ? " (" + pendingCount + ")" : ""),
      action: function () {
        openSettingsPanel();
      },
    },
    { separator: true },
    {
      label: "Logout",
      // Destructive styling matches Delete in the workstream tab dropdown.
      // Logout doesn't lose data, but it interrupts the session and the red
      // hover/focus tint reduces misclick risk on a dense menu.
      cls: "destructive",
      action: function () {
        logout();
      },
    },
  ];

  items.forEach(function (item) {
    if (item.separator) {
      const sep = document.createElement("div");
      sep.className = "ws-tab-dropdown-sep";
      sep.setAttribute("role", "separator");
      menu.appendChild(sep);
      return;
    }
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "ws-tab-dropdown-item" + (item.cls ? " " + item.cls : "");
    btn.setAttribute("role", "menuitem");
    btn.setAttribute("tabindex", "-1");
    const labelSpan = document.createElement("span");
    labelSpan.className = "ws-tab-dropdown-label";
    labelSpan.textContent = item.label;
    btn.appendChild(labelSpan);
    btn.onclick = function () {
      // Refocus the trigger BEFORE close — closeSettingsMenu removes
      // the menu DOM (including this button), and item.action() may
      // call openSettingsPanel which captures document.activeElement
      // as the eventual return-focus target.  Without this refocus,
      // activeElement falls back to <body> and focus restoration
      // sends the user nowhere when the panel later closes.
      if (_settingsMenuTrigger) _settingsMenuTrigger.focus();
      closeSettingsMenu();
      item.action();
    };
    menu.appendChild(btn);
  });

  document.body.appendChild(menu);

  // Right-align under the gear so the menu hangs off the right edge of
  // the appbar without overflowing the viewport.  Right-edge override
  // runs BEFORE the left-edge floor so a menu wider than the viewport
  // still gets clamped to mx=4 instead of going negative — matches the
  // proxy node-picker order in turnstone/console/server.py:307-309.
  const tr = triggerEl.getBoundingClientRect();
  const mr = menu.getBoundingClientRect();
  let mx = tr.right - mr.width;
  let my = tr.bottom + 4;
  if (my + mr.height > window.innerHeight) my = tr.top - mr.height - 4;
  if (mx + mr.width > window.innerWidth) mx = window.innerWidth - mr.width - 4;
  if (mx < 4) mx = 4;
  menu.style.left = mx + "px";
  menu.style.top = my + "px";
  _settingsMenu = menu;

  _settingsMenuCloseHandler = function (e) {
    if (e.type === "keydown") {
      if (e.key === "Escape") {
        e.preventDefault();
        closeSettingsMenu();
        triggerEl.focus();
      } else if (e.key === "Tab") {
        // Per WAI-ARIA APG menu pattern: Tab closes the menu AND lets
        // focus move naturally to the next focusable element — don't
        // preventDefault, otherwise Tab is a dead key inside the menu.
        closeSettingsMenu();
      } else if (
        e.key === "ArrowDown" ||
        e.key === "ArrowUp" ||
        e.key === "Home" ||
        e.key === "End"
      ) {
        e.preventDefault();
        const btns = Array.from(menu.querySelectorAll(".ws-tab-dropdown-item"));
        if (!btns.length) return;
        const idx = btns.indexOf(document.activeElement);
        if (e.key === "ArrowDown") btns[(idx + 1) % btns.length].focus();
        // idx <= 0 covers both "first item" (wrap to last) and "no
        // current focus" (idx === -1, which would otherwise yield
        // len-2 via the modulo).  Matches showTabDropdown and the
        // proxy node-picker (turnstone/console/server.py:275).
        else if (e.key === "ArrowUp")
          btns[idx <= 0 ? btns.length - 1 : idx - 1].focus();
        else if (e.key === "Home") btns[0].focus();
        else if (e.key === "End") btns[btns.length - 1].focus();
      }
    } else if (
      e.type === "mousedown" &&
      !menu.contains(e.target) &&
      e.target !== triggerEl &&
      !triggerEl.contains(e.target)
    ) {
      closeSettingsMenu();
    }
  };

  // Attach the keydown listener synchronously so an Escape press
  // queued behind the opening click isn't silently dropped: the
  // global keydown handler at the bottom of this file returns early
  // when _settingsMenu is set (the dashboard-Escape-wipes-composer
  // guard), so without a synchronous menu-side listener there's a
  // brief window where Escape has no handler at all.  Mousedown +
  // initial focus stay deferred — mousedown to avoid the click that
  // opened the menu firing its own outside-click close, initial
  // focus because the menu DOM needs a tick to settle layout before
  // we call focus() on its first item.
  document.addEventListener("keydown", _settingsMenuCloseHandler);
  const activeMenu = menu;
  const closeHandler = _settingsMenuCloseHandler;
  setTimeout(function () {
    if (_settingsMenu !== activeMenu || !closeHandler) return;
    document.addEventListener("mousedown", closeHandler);
    const first = activeMenu.querySelector(".ws-tab-dropdown-item");
    if (first) first.focus();
  }, 0);
}

function closeSettingsMenu() {
  if (_settingsMenu) {
    _settingsMenu.remove();
    _settingsMenu = null;
  }
  if (_settingsMenuCloseHandler) {
    document.removeEventListener("mousedown", _settingsMenuCloseHandler);
    document.removeEventListener("keydown", _settingsMenuCloseHandler);
    _settingsMenuCloseHandler = null;
  }
  if (_settingsMenuTrigger) {
    _settingsMenuTrigger.setAttribute("aria-expanded", "false");
    _settingsMenuTrigger.removeAttribute("aria-controls");
    _settingsMenuTrigger = null;
  }
}

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
  const overlay = document.getElementById("revoke-mcp-overlay");
  if (msg) {
    msg.textContent =
      "Disconnect " +
      server +
      "? Tools that need this server will require re-consent.";
  }
  if (overlay) overlay.style.display = "flex";

  if (_revokeMcpTrap) document.removeEventListener("keydown", _revokeMcpTrap);
  _revokeMcpTrap = function (e) {
    if (e.key === "Escape") {
      e.preventDefault();
      cancelRevokeMcp();
      return;
    }
    if (e.key === "Tab") {
      const box = document.getElementById("revoke-mcp-box");
      if (!box) return;
      const focusable = box.querySelectorAll("button");
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    }
  };
  document.addEventListener("keydown", _revokeMcpTrap);

  const cancelBtn = overlay
    ? overlay.querySelector("button:not(.danger)")
    : null;
  if (cancelBtn) cancelBtn.focus();
}

function cancelRevokeMcp() {
  _pendingRevokeServer = null;
  const overlay = document.getElementById("revoke-mcp-overlay");
  if (overlay) overlay.style.display = "none";
  if (_revokeMcpTrap) {
    document.removeEventListener("keydown", _revokeMcpTrap);
    _revokeMcpTrap = null;
  }
}

function confirmRevokeMcp() {
  const server = _pendingRevokeServer;
  if (!server) {
    cancelRevokeMcp();
    return;
  }
  authFetch("/v1/api/mcp/oauth/connections/" + encodeURIComponent(server), {
    method: "DELETE",
  })
    .then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      cancelRevokeMcp();
      showToast("Disconnected " + server);
      loadMcpConnections();
    })
    .catch(function (err) {
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
  // Defer to modal's own keydown handler when any modal is open
  const modalIds = [
    "new-ws-overlay",
    "edit-title-overlay",
    "delete-ws-overlay",
    "ws-delete-overlay",
    "settings-overlay",
    "revoke-mcp-overlay",
  ];
  for (let mi = 0; mi < modalIds.length; mi++) {
    const modal = document.getElementById(modalIds[mi]);
    if (modal && modal.style.display !== "none") return;
  }
  // Settings menu is a transient dropdown, not a modal overlay, but
  // the global Escape handler must not reach hideDashboard() while
  // it's open — that would wipe the composer out from under the user
  // (hideDashboard clears dashboard-input.value and _dashboardStagedFiles).
  // The menu's own keydown handler (registered async via setTimeout(0)
  // in openSettingsMenu) handles Escape and Tab.
  if (_settingsMenu) return;

  if (e.key === "Escape" && dashboardVisible) {
    e.preventDefault();
    hideDashboard();
    return;
  }

  // Get focused pane for approval / busy checks
  const pane = getFocusedPane();

  // Escape: cancel generation when busy
  if (e.key === "Escape" && pane && pane.busy && !pane.pendingApproval) {
    e.preventDefault();
    pane.cancelGeneration();
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
  // Ctrl+Shift+W: close pane (must come before Ctrl+W)
  if (e.ctrlKey && e.shiftKey && e.key.toLowerCase() === "w") {
    if (splitRoot && countLeaves(splitRoot) > 1) {
      e.preventDefault();
      closePane(focusedPaneId);
    }
    return;
  }
  // Workstream action shortcuts — only preventDefault when a workstream
  // is active, so native browser shortcuts (e.g. Ctrl+Shift+R hard reload)
  // still work when no workstream is focused.
  if (e.ctrlKey && e.shiftKey) {
    closeTabDropdown();
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
    closeTabDropdown();
    if (Object.keys(workstreams).length > 1) {
      e.preventDefault();
      closeWorkstream(currentWsId);
    }
    return;
  }

  // Ctrl+Alt+Arrow: cycle pane focus
  if (
    e.ctrlKey &&
    e.altKey &&
    (e.key === "ArrowLeft" || e.key === "ArrowRight")
  ) {
    e.preventDefault();
    const paneIds = [];
    (function collectIds(n) {
      if (!n) return;
      if (n.type === "leaf") {
        paneIds.push(n.pane.id);
      } else {
        collectIds(n.children[0]);
        collectIds(n.children[1]);
      }
    })(splitRoot);
    if (paneIds.length > 1) {
      let ci = paneIds.indexOf(focusedPaneId);
      if (e.key === "ArrowRight") ci = (ci + 1) % paneIds.length;
      else ci = (ci - 1 + paneIds.length) % paneIds.length;
      setFocusedPane(paneIds[ci]);
      panes[paneIds[ci]].inputEl.focus();
    }
    return;
  }

  // Ctrl+\: split pane
  if (e.ctrlKey && e.code === "Backslash") {
    e.preventDefault();
    if (e.shiftKey) splitPane(focusedPaneId, "vertical");
    else splitPane(focusedPaneId, "horizontal");
    return;
  }

  // Inline approval keybindings
  if (pane && pane.pendingApproval) {
    const fbInput =
      pane.approvalBlockEl &&
      pane.approvalBlockEl.querySelector(".ts-approval-feedback");
    if (fbInput && document.activeElement === fbInput) {
      if (e.key === "Enter") {
        e.preventDefault();
        pane.resolveApproval(true, false, pane.getFeedback());
      } else if (e.key === "Escape") {
        e.preventDefault();
        pane.resolveApproval(false, false, pane.getFeedback());
      }
      return;
    }
    // Not in feedback input — intercept shortcut keys
    e.preventDefault();
    e.stopPropagation();
    if (e.key === "y" || e.key === "Enter") {
      pane.resolveApproval(true, false, pane.getFeedback());
    } else if (e.key === "n" || e.key === "Escape") {
      pane.resolveApproval(false, false, pane.getFeedback());
    } else if (e.key === "a") {
      pane.resolveApproval(true, true, pane.getFeedback());
    } else if (e.key === "d") {
      const details = pane.approvalBlockEl
        ? pane.approvalBlockEl.querySelectorAll(".verdict-detail")
        : [];
      details.forEach(function (d) {
        const isHidden = d.style.display === "none";
        d.style.display = isHidden ? "block" : "none";
        const btn2 = d.previousElementSibling
          ? d.previousElementSibling.querySelector(".verdict-expand")
          : null;
        if (btn2) btn2.textContent = isHidden ? "hide" : "details";
      });
    }
    return;
  }
});

// ===========================================================================
//  16. Init
// ===========================================================================

function initWorkstreams() {
  authFetch("/v1/api/workstreams")
    .then(function (r) {
      return r.json();
    })
    .then(function (data) {
      data.workstreams.forEach(function (ws) {
        workstreams[ws.ws_id] = { name: ws.name, state: ws.state };
      });
      connectGlobalSSE();
      const wsIds = Object.keys(workstreams);
      if (!wsIds.length) {
        renderTabBar();
        showDashboard();
        return;
      }
      if (!Object.keys(panes).length) {
        if (!restoreLayout()) {
          const p = createPane(wsIds[0]);
          splitRoot = { type: "leaf", pane: p };
          setFocusedPane(p.id);
        }
        renderLayout();
      }
      renderTabBar();
      for (let id in panes) {
        if (!panes[id].evtSource) {
          panes[id].showEmptyState();
          panes[id]._loadHistoryThenConnect(panes[id].wsId);
        }
      }
      const params = new URLSearchParams(location.search);
      const targetWs = params.get("ws_id");
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
        history.replaceState({ turnstone: "dashboard" }, "", location.pathname);
        showDashboard();
      }
    });
}

initLogin();
pollHealth();
loadInterfaceSettings();
initWorkstreams();
loadPendingConsents();

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
    for (const id in panes) {
      if (panes[id]) panes[id].disconnectSSE();
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
  for (const id in panes) {
    const p = panes[id];
    if (!p || !p.wsId) continue;
    const live =
      p.evtSource &&
      (p.evtSource.readyState === EventSource.OPEN ||
        p.evtSource.readyState === EventSource.CONNECTING);
    if (!live) p.connectSSE(p.wsId);
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
