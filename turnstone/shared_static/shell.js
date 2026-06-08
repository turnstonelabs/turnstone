/* ==========================================================================
   L-shell bootstrap — builds the unified rail + tab-bar + pane-host frame and
   hands off to the legacy app boot.

   The FIRST ES-module citizen in shared_static (the rest are classic scripts).
   Being `type="module"` it is deferred, so it runs AFTER every classic script —
   including app.js, which now defines `window.TS_APP.boot` without auto-running
   it.  So the order is: classic scripts define globals → this module builds the
   shell and reparents the existing DOM → it calls `TS_APP.boot()` to start
   login + the Tier-1 cluster stream under the shell.

   Re-point without rewiring: the cluster stream writes its connection status via
   getElementById("status-bar"); we MOVE that element (id preserved) into the
   rail, so connectSSE keeps writing to it.  Cluster health itself renders in the
   rail (rail.js) from the Tier-1 seam app.js exposes on window.TS_APP.

   This is the one module that legitimately reaches the existing document by id —
   it is the orchestrator wiring the shell onto the page, not pane-internal code
   (pane code stays root-scoped).
   ========================================================================== */

import { PaneManager, ShellPane } from "./pane.js";
import { mountRail, mountManage, glyph } from "./rail.js";
// The interactive pane is a real ES module beside us in /shared (step 5a) — the
// shell imports it directly, and it exists in every deployment.  The coordinator
// pane lives at an absolute /static path that only the CONSOLE serves, so it is
// imported LAZILY in mountShell, gated on the orchestration capability: a
// standalone turnstone-server has no /static/coordinator/* and a static import
// would 404 and abort the whole shell module.
import { createInteractivePane } from "./interactive.js";

function make(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

function buildShell(caps) {
  const app = make("div", "app");

  // ----- Rail (the | of the L) -----
  const rail = make("aside", "rail");

  const brand = make("div", "rail-brand");
  // The brand doubles as "go home" — a real <button> so it is keyboard-
  // focusable + operable (parity with the header's #header-home-link <a>).
  const home = make("button", "brand-home");
  home.type = "button";
  home.setAttribute("aria-label", "Home (cluster landing)");
  home.append(make("div", "brand-mark"));
  home.append(make("span", "brand-name", "turnstone"));
  home.append(make("span", "brand-sub", caps.brandSub || "console"));
  home.addEventListener("click", () => {
    if (typeof window.showHome === "function") window.showHome();
  });
  brand.append(home);
  rail.append(brand);

  const scroll = make("div", "rail-scroll");
  // connection indicator — the relocated #status-bar is inserted here at wire time.
  const connSlot = make("div", "rail-conn-slot");
  scroll.append(connSlot);
  // IA sections — each a label + a render target.  Cluster is capability-gated
  // (hidden on a single-node standalone deployment); rail.js fills Cluster +
  // Workspaces from Tier-1 and the Manage groups from the admin IA (step 3).
  function section(title) {
    scroll.append(make("div", "sec-label", title));
    const body = make("div", "rail-section");
    scroll.append(body);
    return body;
  }
  const clusterSec = caps.cluster ? section("Cluster") : null;
  const workspacesSec = section("Workspaces");
  const manageSec = section("Manage");
  rail.append(scroll);

  const foot = make("div", "rail-foot");
  rail.append(foot);

  // ----- Content (the — of the L): tab bar + pane host -----
  const content = make("main", "content");
  const tabbar = make("div", "tabbar");
  const tail = make("div", "tabbar-right"); // right-floated tab-bar chrome (empty for now)
  tabbar.append(tail);
  const panes = make("div", "panes");
  content.append(tabbar, panes);

  app.append(rail, content);
  return {
    app,
    rail,
    scroll,
    connSlot,
    foot,
    tabbar,
    tail,
    panes,
    clusterSec,
    workspacesSec,
    manageSec,
  };
}

// Scan the Tier-1 snapshot for a workstream — the shared spine of the three thin
// wrappers below.  `skipConsole` excludes the `console` pseudo-node (coordinators
// live there and must NOT be node-proxied — only nodeForWs wants that).  Returns
// { nodeId, ws } or null (snapshot not ready / no match).
function findWs(wsId, skipConsole) {
  try {
    const cs =
      window.TS_APP &&
      window.TS_APP.getClusterState &&
      window.TS_APP.getClusterState();
    if (cs && cs.nodes) {
      for (const nid in cs.nodes) {
        if (skipConsole && nid === "console") continue;
        for (const ws of cs.nodes[nid].workstreams || []) {
          if (ws.id === wsId) return { nodeId: nid, ws: ws };
        }
      }
    }
  } catch (e) {
    /* snapshot not ready */
  }
  return null;
}

// Tab title for a session pane — the ws name from the Tier-1 snapshot, else a
// short ws_id (a restored pane may open before the first snapshot arrives).
function wsTitle(wsId) {
  const f = findWs(wsId, false);
  if (f) return f.ws.name || f.ws.title || String(wsId).slice(0, 8);
  return wsId ? String(wsId).slice(0, 8) : "session";
}

// The cluster node hosting an interactive ws — the node-proxy transport target
// for its pane (Tier-1 carries every ws under its owning node).  Derived from
// the live snapshot so a rehydrated pane needs no persisted node_id; an
// explicit open-time hint (rail click / coordinator child link) skips the scan.
function nodeForWs(wsId) {
  const f = findWs(wsId, true);
  return f ? f.nodeId : null;
}

// The live state of a workstream from the Tier-1 snapshot (the SAME source the
// rail reads) — so a conversational tab's state glyph stays consistent with its
// rail row and updates live rather than sitting at an open-time placeholder.
function stateForWs(wsId) {
  const f = findWs(wsId, false);
  return f ? f.ws.state || "idle" : "idle";
}

// Repaint every stateful tab's glyph from Tier-1.  Subscribed to the render
// signal (one Tier-1 writer for the tab glyph; the pane's Tier-2 stream drives
// its body, not the tab) and called on activate for the initial paint.
function paintConvTabGlyphs(pm) {
  for (const t of pm.statefulTabs())
    pm.setTabGlyph(t.id, glyph(stateForWs(t.rawId)));
}

// Tab-action menu items for a conversational pane — the three-verb close plus
// the per-persona verbs.  Pane-type-derived AND deployment-aware: a verb appears
// only when its handler exists here, so the SAME shell yields the full menu in
// the standalone (whose interactive verbs are classic globals in ui/static's
// app.js) and a reduced menu in the console — the capability-derived-affordances
// thesis applied to the tab menu.  `opts`: titleVerbs (Refresh/Edit/Fork title),
// deleteVerb (the destructive Delete), closeSession (stop the workstream itself).
function convTabMenu(pane, pm, wsId, opts) {
  opts = opts || {};
  const G = window;
  const items = [];
  if (opts.titleVerbs) {
    if (typeof G.refreshWorkstreamTitle === "function")
      items.push({
        label: "Refresh title",
        action: () => G.refreshWorkstreamTitle(wsId),
      });
    if (typeof G.editWorkstreamTitle === "function")
      items.push({
        label: "Edit title",
        key: "Ctrl+Shift+E",
        action: () => G.editWorkstreamTitle(wsId),
      });
    if (typeof G.forkWorkstream === "function")
      items.push({
        label: "Fork",
        key: "Ctrl+Shift+F",
        action: () => G.forkWorkstream(wsId),
      });
  }
  if (typeof G.exportWorkstreamDownload === "function")
    items.push({
      label: "Export conversation",
      action: () => G.exportWorkstreamDownload(wsId),
    });
  items.push({ separator: true });
  // Close pane — drop the tab, leave the session running (PaneManager-level).
  items.push({
    label: "Close pane",
    key: "Ctrl+W",
    action: () => pm.close(pane.id),
  });
  // Close workstream — stop the session itself (distinct from closing the tab).
  if (opts.closeSession)
    items.push({ label: "Close workstream", action: opts.closeSession });
  // Delete — destroy + unsave (interactive standalone only; confirms itself).
  if (opts.deleteVerb && typeof G.confirmDeleteWorkstream === "function")
    items.push({
      label: "Delete",
      key: "Ctrl+Shift+X",
      cls: "destructive",
      action: () => G.confirmDeleteWorkstream(wsId),
    });
  return items;
}

async function mountShell() {
  const caps = window.TURNSTONE_SHELL_CAPS || {};

  // Capture the existing page structure before we reorganise it.
  const headerEl = document.getElementById("header");
  const statusBarEl = document.getElementById("status-bar");
  const breadcrumbEl = document.getElementById("breadcrumb");
  const mainEl = document.getElementById("main");
  const viewAdminEl = document.getElementById("view-admin");

  const shell = buildShell(caps);
  // Insert the shell as the first body child so it owns the viewport; portals
  // (toast, modals, the login overlay appended later by auth.js) stay siblings.
  document.body.insertBefore(shell.app, document.body.firstChild);

  // Relocate the connection indicator into the rail (id preserved → connectSSE
  // keeps writing to it; just styled as a rail line now).
  if (statusBarEl) {
    statusBarEl.classList.add("rail-conn");
    shell.connSlot.replaceWith(statusBarEl);
  }

  // Relocate the header controls into the rail footer (onclick + ids preserved),
  // then retire the now-empty header.  Order: theme · admin · logout · user.
  for (const id of ["theme-toggle", "admin-btn", "logout-btn"]) {
    const btn = document.getElementById(id);
    if (btn) shell.foot.append(btn);
  }
  const userChip = make("span", "user-chip");
  userChip.append(make("span", "avatar", initialsFor(caps)));
  const nameEl = make("span", null, displayNameFor(caps));
  userChip.append(nameEl);
  shell.foot.append(userChip);
  if (headerEl) headerEl.style.display = "none";

  // ----- PaneManager: one new spine -----
  const pm = new PaneManager({
    tabbarEl: shell.tabbar,
    panesEl: shell.panes,
    tailEl: shell.tail,
    caps,
  });

  // [+] new-tab (step 7): a shortcut to the persona launcher.  The Dashboard pane
  // hosts the unified coordinator/interactive launcher (a new session needs a task
  // prompt, so it composes there) — "new session" focuses it.  showHome is exposed
  // by both deployments; openPane is the fallback.  Lives in the right-floated tail
  // slot per the brief.  (Auth is the launcher's own concern — it gates each
  // persona option; focusing it is always safe.)
  const addTab = make("button", "tab-add");
  addTab.type = "button";
  addTab.setAttribute("aria-label", "New session");
  addTab.title = "New session";
  addTab.textContent = "+";
  addTab.addEventListener("click", () => {
    if (typeof window.showHome === "function") window.showHome();
    else pm.openPane("dashboard");
    // Land in the launcher composer so "new session" is immediately typeable —
    // showHome on the already-active Dashboard is otherwise a no-op.
    if (window.TS_APP && typeof window.TS_APP.focusLauncher === "function")
      window.TS_APP.focusLauncher();
  });
  shell.tail.append(addTab);

  // Dashboard pane (step 1): a singleton that ADOPTS the legacy #main so the
  // console renders unchanged inside the new shell.  Real pane types (admin,
  // coordinator, interactive) register in steps 2-5.
  pm.registerType("dashboard", () => {
    const pane = new ShellPane({
      type: "dashboard",
      title: "Dashboard",
      glyph: "◇",
      closable: false,
    });
    pane.onMount = function () {
      if (breadcrumbEl) this.bodyEl.append(breadcrumbEl);
      if (mainEl) this.bodyEl.append(mainEl);
    };
    return pane;
  });

  // Admin pane (step 3): a singleton that ADOPTS #view-admin (the 18 admin
  // tabpanels).  The rail's Manage groups are its navigation — the in-pane
  // sidebar is retired.  Lazy-mounts on first openPane('admin'); by then the
  // dashboard pane already hosts #main, so #view-admin moves out of it here.
  pm.registerType("admin", () => {
    const pane = new ShellPane({ type: "admin", title: "Admin", glyph: "⚙" });
    pane.tabMenu = () => [
      { label: "Close pane", key: "Ctrl+W", action: () => pm.close(pane.id) },
    ];
    pane.onMount = function () {
      if (viewAdminEl) {
        viewAdminEl.style.display = ""; // clear the inline display:none guard
        this.bodyEl.append(viewAdminEl);
      }
    };
    return pane;
  });

  // Interactive pane (step 5): a ws_id-keyed conversational pane whose session
  // lives on a cluster NODE, so its Tier-2 SSE is node-PROXIED (the LOCALITY
  // invariant).  The owning node is resolved from the Tier-1 snapshot (or a
  // `{nodeId}` open-time hint from a rail click / coordinator child link), so a
  // rehydrated pane needs no persisted node_id.  No children/tasks sidebar —
  // an interactive persona has no spawn affordance.  onActivate connects once +
  // re-marks focus; onDeactivate stops focus-stealing while the stream stays
  // live; onClose tears down the stream + timers and detaches the DOM.
  pm.registerType("interactive", (id, extra) => {
    const pane = new ShellPane({
      type: "interactive",
      title: wsTitle(id),
      stateful: true, // tab shows live Tier-1 state (no static placeholder)
    });
    pane.tabMenu = () =>
      convTabMenu(pane, pm, id, {
        titleVerbs: true,
        deleteVerb: true,
        closeSession:
          typeof window.closeWorkstream === "function"
            ? () => window.closeWorkstream(id)
            : null,
      });
    pane.onMount = function () {
      // Node-proxy transport only exists in a cluster deployment (the console).
      // On a single-node standalone (caps.cluster=false) every session is LOCAL,
      // so nodeId stays null → the pane uses base="" (no /node/<id> hop), even
      // though the synthesized one-node clusterState would otherwise name a node.
      const nodeId = caps.cluster
        ? (extra && extra.nodeId) || nodeForWs(id)
        : null;
      this._ctl = createInteractivePane(this.bodyEl, id, {
        nodeId,
        onClose: () => pm.close(pane.id),
      });
    };
    pane.onActivate = function () {
      pm.setTabGlyph(pane.id, glyph(stateForWs(id))); // live Tier-1 state glyph
      if (!this._ctl) return;
      this._ctl.connect(); // idempotent — opens the stream once, re-marks focus
      if (!this._loginArmed && window.TS_LOGIN && this._ctl.onLogin) {
        this._loginArmed = true;
        window.TS_LOGIN.subscribe(this._ctl.onLogin);
      }
    };
    pane.onDeactivate = function () {
      if (this._ctl && this._ctl.deactivate) this._ctl.deactivate();
    };
    pane.onClose = function () {
      if (this._ctl) {
        if (window.TS_LOGIN && this._ctl.onLogin)
          window.TS_LOGIN.unsubscribe(this._ctl.onLogin);
        this._ctl.destroy();
      }
    };
    return pane;
  });

  // Coordinator pane (step 4): a ws_id-keyed conversational pane — but ONLY where
  // the deployment has the orchestration capability (the console).  The factory
  // lives at an absolute /static path the standalone server does not serve, so it
  // is imported lazily here, before rehydrate, and skipped entirely otherwise (a
  // persisted coordinator pane then degrades to a skip — rehydrate only re-opens
  // registered types).  onMount builds the coordinator chrome + controller (a
  // console-local persona, no node-proxy transport); onActivate opens its per-pane
  // Tier-2 SSE once + subscribes to login re-arm; onClose tears the controller
  // (stream + timers + observer) down.
  if (caps.orchestration) {
    try {
      const { createCoordinatorPane } =
        await import("/static/coordinator/coordinator.js");
      pm.registerType("coordinator", (id) => {
        const pane = new ShellPane({
          type: "coordinator",
          title: wsTitle(id),
          stateful: true, // tab shows live Tier-1 state (no static placeholder)
        });
        pane.tabMenu = () =>
          convTabMenu(pane, pm, id, {
            closeSession: () => {
              if (pane._ctl && pane._ctl.closeSession) pane._ctl.closeSession();
            },
          });
        pane.onMount = function () {
          this._ctl = createCoordinatorPane(this.bodyEl, id, {
            onClose: () => pm.close(pane.id),
          });
        };
        pane.onActivate = function () {
          pm.setTabGlyph(pane.id, glyph(stateForWs(id))); // live Tier-1 state glyph
          if (this._ctl && !this._connected) {
            this._connected = true;
            this._ctl.connect();
            if (window.TS_LOGIN && this._ctl.onLogin) {
              window.TS_LOGIN.subscribe(this._ctl.onLogin);
            }
          }
        };
        pane.onClose = function () {
          if (this._ctl) {
            if (window.TS_LOGIN && this._ctl.onLogin)
              window.TS_LOGIN.unsubscribe(this._ctl.onLogin);
            this._ctl.destroy();
          }
        };
        return pane;
      });
      // Coordinator panes need the admin.coordinator scope (the SAME gate the
      // launcher + saved-list use).  Deny -> no pane; the rail / child-link /
      // rehydrate open paths all route through openPane, so this gates them all at
      // once.  The backend enforces the scope too — this just avoids opening a
      // doomed pane.  Fail-open if the helper is somehow absent (the backend still
      // catches it); a present helper returning false is the real deny.
      pm.setAuthGate("coordinator", {
        canOpen: () =>
          typeof window._hasCoordPermission !== "function" ||
          window._hasCoordPermission(),
        onDeny: () =>
          window.showToast &&
          window.showToast("admin.coordinator permission required", "warning"),
      });
    } catch (e) {
      console.error("L-shell: coordinator pane unavailable", e);
    }
  }

  window.TS_SHELL = { panes: pm, caps };

  // Login fan-out: app.js owns the single window.onLoginSuccess (the Tier-1
  // reconnect, set at load).  Wrap it in a tiny registry so EVERY conversational
  // pane can re-arm its own Tier-2 stream on re-auth, not just the last writer.
  // MUST be set up BEFORE rehydrate() below: rehydrate activates the restored
  // panes, whose onActivate subscribes here — if TS_LOGIN were defined after,
  // a rehydrated pane would silently skip its re-auth reconnect (and onActivate
  // never re-fires).  `unsubscribe` lets a closed pane drop its closure (else the
  // detached controller leaks across open/close/re-login).
  const _loginSubs = [];
  if (typeof window.onLoginSuccess === "function")
    _loginSubs.push(window.onLoginSuccess);
  window.TS_LOGIN = {
    subscribe(cb) {
      if (typeof cb === "function" && _loginSubs.indexOf(cb) < 0)
        _loginSubs.push(cb);
    },
    unsubscribe(cb) {
      const i = _loginSubs.indexOf(cb);
      if (i >= 0) _loginSubs.splice(i, 1);
    },
  };
  window.onLoginSuccess = function () {
    for (const cb of _loginSubs) {
      try {
        cb();
      } catch (e) {
        console.error("L-shell: onLogin subscriber failed", e);
      }
    }
  };

  // Restore the persisted working set, else open the default Dashboard pane.
  if (!pm.rehydrate()) pm.openPane("dashboard");

  // Wire the rail's live Cluster + Workspaces sections to the Tier-1 render
  // signal (subscribe before boot so the first snapshot render is caught).
  mountRail(
    {
      cluster: shell.clusterSec,
      workspaces: shell.workspacesSec,
      paneManager: pm,
    },
    caps,
  );

  // Manage section — the admin IA as collapsible discovery groups; a row click
  // routes through the TS_ADMIN seam (opens/focuses the singleton Admin pane).
  // `pm` lets the rail seed its active marker when a restored Admin pane is open.
  mountManage(shell.manageSec, pm);

  // Live tab state-glyphs (step 7): repaint conversational tabs' state glyphs on
  // every Tier-1 render — the SAME source + builder the rail uses, so tab and
  // rail agree and the glyph never sits stale at an open-time placeholder.  One
  // Tier-1 writer for the tab glyph; the pane's Tier-2 stream drives its body.
  if (window.TS_APP && typeof window.TS_APP.onRender === "function") {
    window.TS_APP.onRender(() => paintConvTabGlyphs(pm));
  }

  // Hand off to the legacy boot (login + Tier-1 stream) now that the shell and
  // its status DOM exist.
  if (window.TS_APP && typeof window.TS_APP.boot === "function") {
    window.TS_APP.boot();
  } else {
    console.error(
      "L-shell: window.TS_APP.boot is missing — app.js did not load",
    );
  }
}

function displayNameFor(caps) {
  // sessionStorage is populated by auth.js after whoami; fall back gracefully.
  try {
    return (
      caps.userName ||
      sessionStorage.getItem("ts.username") ||
      sessionStorage.getItem("username") ||
      "account"
    );
  } catch (e) {
    return "account";
  }
}

function initialsFor(caps) {
  const name = displayNameFor(caps);
  const parts = String(name)
    .trim()
    .split(/[\s._-]+/)
    .filter(Boolean);
  if (!parts.length) return "·";
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
  return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
}

mountShell();
