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
import { mountRail, mountManage } from "./rail.js";

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

function mountShell() {
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
    pane.onMount = function () {
      if (viewAdminEl) {
        viewAdminEl.style.display = ""; // clear the inline display:none guard
        this.bodyEl.append(viewAdminEl);
      }
    };
    return pane;
  });

  // Restore the persisted working set, else open the default Dashboard pane.
  if (!pm.rehydrate()) pm.openPane("dashboard");

  window.TS_SHELL = { panes: pm, caps };

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
  mountManage(shell.manageSec);

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
