/* ==========================================================================
   L-shell bootstrap — builds the unified rail + tab-bar + pane-host frame and
   hands off to the legacy app boot.

   The shared substrate (utils/toast/auth/composer/renderer/…) is ES modules
   like this file; only theme.js (FOUC), the vendored libs, and the legacy
   app/admin/governance bundles remain classic.  Being `type="module"` this
   file is deferred and last in document order, so it runs AFTER every classic
   script — including app.js, which defines `window.TS_APP.boot` without
   auto-running it — and after the substrate modules have installed their
   transitional window bridges.  So the order is: classic scripts define the
   legacy globals → substrate modules evaluate → this module builds the shell
   and reparents the existing DOM → it calls `TS_APP.boot()` to start login +
   the Tier-1 cluster stream under the shell.

   Re-point without rewiring: the cluster stream writes its connection status via
   getElementById("status-bar"); we MOVE that element (id preserved) into the
   rail, so connectSSE keeps writing to it.  Cluster health itself renders in the
   rail (rail.js) from the Tier-1 seam app.js exposes on window.TS_APP.

   This is the one module that legitimately reaches the existing document by id —
   it is the orchestrator wiring the shell onto the page, not pane-internal code
   (pane code stays root-scoped).
   ========================================================================== */

import { PaneManager, ShellPane, openPopupMenu } from "./pane.js";
import { mountRail, mountManage, glyph, setRowBadge } from "./rail.js";
import { authFetch } from "./auth.js";
// The interactive pane is a real ES module beside us in /shared (step 5a) — the
// shell imports it directly, and it exists in every deployment.  The coordinator
// pane lives at an absolute /static path that only the CONSOLE serves, so it is
// imported LAZILY in mountShell, gated on the orchestration capability: a
// standalone turnstone-server has no /static/coordinator/* and a static import
// would 404 and abort the whole shell module.
import { createInteractivePane } from "./interactive.js";
import { createPreviewPane } from "./preview.js";

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
  rail.id = "shell-rail"; // aria-controls target for the collapse toggle

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
  // Collapse toggle — shrinks the rail to a glyph-only strip (desktop only; on
  // mobile the rail is an off-canvas drawer and this button is hidden).  Label,
  // glyph and aria state are kept in sync by the shell's setRailCollapsed.
  const collapseBtn = make("button", "rail-collapse");
  collapseBtn.type = "button";
  collapseBtn.setAttribute("aria-controls", "shell-rail");
  brand.append(collapseBtn);
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
  // Drawer toggle — first tab-bar item, shown only at the mobile breakpoint
  // (the rail leaves the grid and overlays off-canvas there).  State + focus
  // hand-off are wired by mountShell's setDrawer.
  const burger = make("button", "rail-burger", "☰");
  burger.type = "button";
  burger.setAttribute("aria-label", "Open navigation");
  burger.setAttribute("aria-controls", "shell-rail");
  burger.setAttribute("aria-expanded", "false");
  // The tab strip is its OWN element so PaneManager's role="tablist" wraps
  // ONLY the tabs — the burger and the [+] tail are non-tab focusables and
  // don't belong inside a tablist's accessibility tree.  It is also the
  // horizontal scroller on mobile, so burger + [+] stay pinned while tabs
  // scroll.
  const tabstrip = make("div", "tabstrip");
  const tail = make("div", "tabbar-right"); // right-floated tab-bar chrome (the [+])
  tabbar.append(burger, tabstrip, tail);
  const panes = make("div", "panes");
  content.append(tabbar, panes);

  // Backdrop scrim for the mobile drawer — fixed overlay between content and
  // the off-canvas rail; decorative (the burger/Escape carry the semantics).
  const scrim = make("div", "rail-scrim");
  scrim.setAttribute("aria-hidden", "true");

  app.append(rail, content, scrim);
  return {
    app,
    rail,
    collapseBtn,
    burger,
    scrim,
    scroll,
    connSlot,
    foot,
    tabbar,
    tabstrip,
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

// Resolve which node an interactive pane's session lives on, ENSURING the
// session is loaded there before the pane streams.  This is the one seam that
// makes a node-proxied session survive a reload: the node /events stream 404s
// on a ws that isn't loaded on that node, so a freshly-rehydrated pane must
// (re)open the session on a node first.  Resolves to {nodeId} or {error}.
//   - Standalone (no cluster): every session is LOCAL → base "" (nodeId null).
//     The first-activate path skips the /open round-trip (the resume flows
//     POST /open before opening the pane); the REVIVE path passes `openFirst`
//     because a closed / post-restart session 404s its /events until reopened.
//   - Console: the route + proxy + open work is the console's — delegate to the
//     TS_APP seam (origin-first POST /open with a rendezvous fallback; it always
//     opens, so `openFirst` is implicit).  Without the seam (unexpected on a
//     cluster console) fall back to the open-time hint or the live Tier-1
//     snapshot, accepting the pre-reload behaviour.
function ensureInteractiveNode(caps, wsId, hint, openFirst) {
  if (!caps.cluster) {
    if (!openFirst) return Promise.resolve({ nodeId: null });
    return authFetch(
      "/v1/api/workstreams/" + encodeURIComponent(wsId) + "/open",
      { method: "POST" },
    )
      .then((r) =>
        r.ok
          ? { nodeId: null }
          : { error: "Could not reopen this session (" + r.status + ")." },
      )
      .catch(() => ({ error: "Could not reopen this session." }));
  }
  if (
    window.TS_APP &&
    typeof window.TS_APP.resolveInteractiveNode === "function"
  ) {
    return window.TS_APP.resolveInteractiveNode(wsId, hint || null);
  }
  return Promise.resolve({ nodeId: hint || nodeForWs(wsId) || null });
}

// Repaint conversational tabs from Tier-1 in ONE pass: a single findWs per
// stateful tab feeds BOTH the live state glyph (one Tier-1 writer; the pane's
// Tier-2 stream drives its body, not the tab) and the live workstream name.
// The title only ever UPGRADES to a real name — never flickers a known name
// back to the id-slice if the ws blips out of a frame; the glyph always
// reflects the live state.
function paintConvTabs(pm) {
  for (const t of pm.statefulTabs()) {
    const f = findWs(t.rawId, false);
    pm.setTabGlyph(t.id, glyph(f ? f.ws.state || "idle" : "idle"));
    const name = f && (f.ws.name || f.ws.title);
    if (name) pm.setTabTitle(t.id, name);
  }
}

// POST a workstream verb against a pane's OWN transport base — the node proxy
// for a console interactive pane, "" locally.  The base-aware fallback lane for
// deployments without the classic verb globals (see convTabMenu).
function postWsVerb(base, wsId, verb, body) {
  return authFetch(
    base + "/v1/api/workstreams/" + encodeURIComponent(wsId) + "/" + verb,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    },
  );
}

// --- Pane accelerators -----------------------------------------------------
// ONE source of truth for the pane keyboard shortcuts, shared by BOTH the
// tab-menu key badges (below) and the keydown handler (in mountShell), so a
// badge can never advertise a chord the handler doesn't actually listen for.
//
// The modifier is chosen per platform: Ctrl on macOS (the browser owns Cmd and
// leaves Ctrl free) and Alt on Windows/Linux (there Ctrl IS the browser's own
// new-tab / close-tab / switch-tab accelerator and never reaches the page).
const IS_MAC =
  (navigator.platform && navigator.platform.indexOf("Mac") > -1) || false;
const PANE_MOD_LABEL = IS_MAC ? "Ctrl" : "Alt";

// The per-pane menu actions that also carry a shortcut, keyed by a stable id
// (the menu item's `accel`).  `letter` is matched case-insensitively; `shift`
// gates the Shift-family.  Close-pane is the one non-Shift chord.
const PANE_MENU_ACCELS = {
  "close-pane": { letter: "w", shift: false },
  "edit-title": { letter: "e", shift: true },
  "refresh-title": { letter: "r", shift: true },
  fork: { letter: "f", shift: true },
  delete: { letter: "x", shift: true },
};

// The badge string for a menu accel, e.g. "Alt+Shift+E" — platform-correct.
function paneAccelBadge(id) {
  const a = PANE_MENU_ACCELS[id];
  if (!a) return "";
  return (
    PANE_MOD_LABEL + (a.shift ? "+Shift" : "") + "+" + a.letter.toUpperCase()
  );
}

// True when `e` carries the pane modifier and no other primary modifier — on
// Windows/Linux AltGr surfaces as Ctrl+Alt, so this keeps accented-character
// entry (and the browser's own Ctrl chords) from firing pane shortcuts.
function paneModDown(e) {
  return IS_MAC
    ? e.ctrlKey && !e.altKey && !e.metaKey
    : e.altKey && !e.ctrlKey && !e.metaKey;
}

// Which per-pane menu accel (if any) a keydown triggers, else null.
function paneAccelFor(e) {
  if (!paneModDown(e)) return null;
  const k = e.key.toLowerCase();
  for (const id in PANE_MENU_ACCELS) {
    const a = PANE_MENU_ACCELS[id];
    if (!!e.shiftKey === a.shift && k === a.letter) return id;
  }
  return null;
}

// True when focus is in an editable element.  On macOS Ctrl+T / Ctrl+D are the
// Cocoa "transpose" / "delete-forward" text bindings, so each surface's
// creation and dashboard chords must yield to text editing while a field is
// focused.  Exposed on TS_SHELL so both app.js surfaces share ONE definition.
function inEditable(el) {
  if (!el) return false;
  const tag = el.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || el.isContentEditable;
}

// Tab-action menu items for a conversational pane — the three-verb close plus
// the per-persona verbs.  Pane-type-derived AND deployment-aware, in two lanes:
// the classic verb GLOBALS where they exist (the standalone's ui/static app.js,
// whose verbs also manage its local roster), else a base-aware fallback that
// POSTs the verb straight to the pane's own transport base (`opts.base()` — the
// console's node proxy).  A node-verb is omitted only when no base is resolvable
// yet (a never-activated pane with no node hint) — never aimed at the wrong
// origin.  `opts`: titleVerbs (Refresh/Edit/Fork title), deleteVerb (the
// destructive Delete), closeSession (stop the workstream itself), base (a
// () => string|null transport-base getter; omitted = "" local).
function convTabMenu(pane, pm, wsId, opts) {
  opts = opts || {};
  const G = window;
  const items = [];
  const base = typeof opts.base === "function" ? opts.base() : "";
  const toast = (msg, kind) => {
    if (typeof G.showToast === "function") G.showToast(msg, kind);
  };
  if (opts.titleVerbs) {
    if (typeof G.refreshWorkstreamTitle === "function")
      items.push({
        label: "Refresh title",
        accel: "refresh-title",
        key: paneAccelBadge("refresh-title"),
        action: () => G.refreshWorkstreamTitle(wsId),
      });
    else if (base != null)
      items.push({
        label: "Refresh title",
        accel: "refresh-title",
        key: paneAccelBadge("refresh-title"),
        action: () =>
          postWsVerb(base, wsId, "refresh-title")
            .then((r) =>
              r.ok
                ? toast("Title regeneration started…", "info")
                : toast("Failed to refresh title", "error"),
            )
            .catch(() => toast("Failed to refresh title", "error")),
      });
    if (typeof G.editWorkstreamTitle === "function")
      items.push({
        label: "Edit title",
        accel: "edit-title",
        key: paneAccelBadge("edit-title"),
        action: () => G.editWorkstreamTitle(wsId),
      });
    else if (base != null)
      items.push({
        label: "Edit title",
        accel: "edit-title",
        key: paneAccelBadge("edit-title"),
        action: () => {
          const f = findWs(wsId, false);
          const cur = (f && (f.ws.name || f.ws.title)) || "";
          const next = window.prompt("Session title", cur);
          if (next == null) return; // cancelled
          const title = next.trim();
          if (!title || title === cur) return;
          postWsVerb(base, wsId, "title", { title })
            .then((r) =>
              r.ok
                ? toast("Title updated", "success")
                : toast("Failed to set title", "error"),
            )
            .catch(() => toast("Failed to set title", "error"));
        },
      });
    // Fork stays global-only: it needs the standalone's seeded new-session
    // modal; the console has no interactive fork surface (yet).
    if (typeof G.forkWorkstream === "function")
      items.push({
        label: "Fork",
        accel: "fork",
        key: paneAccelBadge("fork"),
        action: () => G.forkWorkstream(wsId),
      });
  }
  // Export is base-aware everywhere (a proxied pane must export from its node,
  // not the console origin) — omitted while the node is unresolved.
  if (typeof G.exportWorkstreamDownload === "function" && base != null)
    items.push({
      label: "Export conversation",
      action: () => G.exportWorkstreamDownload(wsId, null, base),
    });
  if (items.length) items.push({ separator: true });
  // Close pane — drop the tab, leave the session running (PaneManager-level).
  items.push({
    label: "Close pane",
    accel: "close-pane",
    key: paneAccelBadge("close-pane"),
    action: () => pm.close(pane.id),
  });
  // Close workstream — stop the session itself (distinct from closing the tab).
  if (opts.closeSession)
    items.push({ label: "Close workstream", action: opts.closeSession });
  // Delete — destroy + unsave.  Standalone delegates to its modal-confirming
  // global; the console fallback confirms inline and deletes on the node.
  if (opts.deleteVerb) {
    if (typeof G.confirmDeleteWorkstream === "function")
      items.push({
        label: "Delete",
        accel: "delete",
        key: paneAccelBadge("delete"),
        cls: "destructive",
        action: () => G.confirmDeleteWorkstream(wsId),
      });
    else if (base != null)
      items.push({
        label: "Delete",
        accel: "delete",
        key: paneAccelBadge("delete"),
        cls: "destructive",
        action: () => {
          if (!window.confirm("Delete this session? This cannot be undone."))
            return;
          postWsVerb(base, wsId, "delete")
            .then((r) => {
              // 404 = no row left to delete (already deleted elsewhere) — the
              // intent is satisfied either way; drop the tab.
              if (!r.ok && r.status !== 404) {
                toast("Failed to delete session", "error");
                return;
              }
              pm.close(pane.id);
              toast("Session deleted", "success");
              // The saved list holds the deleted row — refresh it if present.
              if (typeof G.loadSavedCoordinators === "function")
                G.loadSavedCoordinators();
            })
            .catch(() => toast("Failed to delete session", "error"));
        },
      });
  }
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

  // ----- Rail collapse (desktop) -----
  // Collapsed = a glyph-only strip: live state glyphs stay (sessions, cluster
  // counts), text labels hide, Manage becomes a single ⚙ row (rail.js).  A UI
  // preference, so it persists across sessions in localStorage (same home as
  // the theme), unlike the per-tab working set in sessionStorage.  CSS scopes
  // the collapsed layout to desktop; the mobile drawer always shows the full
  // rail, so the preference simply lies dormant there.
  const RAIL_COLLAPSE_KEY = "turnstone_interface.rail";
  const setRailCollapsed = (collapsed, persist) => {
    shell.app.classList.toggle("rail-collapsed", collapsed);
    shell.collapseBtn.textContent = collapsed ? "»" : "«"; // » / «
    const label = collapsed ? "Expand navigation" : "Collapse navigation";
    shell.collapseBtn.setAttribute("aria-label", label);
    shell.collapseBtn.title = label;
    shell.collapseBtn.setAttribute(
      "aria-expanded",
      collapsed ? "false" : "true",
    );
    if (persist) {
      try {
        localStorage.setItem(
          RAIL_COLLAPSE_KEY,
          collapsed ? "collapsed" : "expanded",
        );
      } catch (e) {
        /* localStorage unavailable (private mode) — the toggle still works */
      }
    }
  };
  let railCollapsed = false;
  try {
    railCollapsed = localStorage.getItem(RAIL_COLLAPSE_KEY) === "collapsed";
  } catch (e) {
    /* unreadable preference — default expanded */
  }
  setRailCollapsed(railCollapsed, false);
  shell.collapseBtn.addEventListener("click", () =>
    setRailCollapsed(!shell.app.classList.contains("rail-collapsed"), true),
  );

  // Relocate the connection indicator into the rail (id preserved → connectSSE
  // keeps writing to it; just styled as a rail line now).
  if (statusBarEl) {
    statusBarEl.classList.add("rail-conn");
    shell.connSlot.replaceWith(statusBarEl);
  }

  // Relocate ONLY the theme toggle into the rail footer (id + onclick
  // preserved), then retire the now-empty header.  The Admin button is dropped
  // — Manage already surfaces every admin tab, so a separate footer button is
  // redundant.  Logout moves into the user menu (the #logout-btn stays in the
  // hidden header for its wired onclick + auth.js race-guards; the menu clicks it).
  const themeBtn = document.getElementById("theme-toggle");
  if (themeBtn) shell.foot.append(themeBtn);

  // User chip = a menu button: click opens a small popup with Log out.  Name +
  // avatar come from the whoami identity; the chip is built before whoami lands,
  // so it starts at the "account" placeholder and refreshUser() repaints it once
  // the real username arrives (see the Tier-1 hook below).
  const userChip = make("button", "user-chip");
  userChip.type = "button";
  userChip.setAttribute("aria-haspopup", "menu");
  userChip.setAttribute("aria-expanded", "false");
  const avatarEl = make("span", "avatar", initialsFor(caps));
  const userNameEl = make("span", "user-name", displayNameFor(caps));
  userChip.append(avatarEl, userNameEl);
  userChip.addEventListener("click", (e) => {
    e.stopPropagation();
    toggleUserMenu(userChip);
  });
  shell.foot.append(userChip);
  if (headerEl) headerEl.style.display = "none";
  const refreshUser = () => {
    const nm = displayNameFor(caps);
    if (userNameEl.textContent !== nm) {
      userNameEl.textContent = nm;
      avatarEl.textContent = initialsFor(caps);
    }
  };

  // ----- PaneManager: one new spine -----
  // It owns the tabstrip (role=tablist), NOT the whole tab bar: the burger and
  // the [+] tail live outside the strip so the tablist holds only tabs.  No
  // tailEl — the strip has no non-tab chrome to anchor before.
  const pm = new PaneManager({
    tabbarEl: shell.tabstrip,
    panesEl: shell.panes,
    caps,
  });

  // ----- Mobile drawer (the rail off-canvas below the breakpoint) -----
  // Open moves focus into the rail (its buttons are unreachable while
  // off-canvas — the closed drawer is visibility:hidden); close returns it to
  // the burger only for Escape, the keyboard path.  Any pane activation closes
  // the drawer: a rail tap that opened/focused a pane has done its job, and
  // the stale-open drawer would cover the very pane it opened.  Desktop is
  // untouched — the classes exist but the media query ignores them.
  const drawerOpen = () => shell.app.classList.contains("rail-open");
  const setDrawer = (open) => {
    if (open === drawerOpen()) return;
    shell.app.classList.toggle("rail-open", open);
    shell.burger.setAttribute("aria-expanded", open ? "true" : "false");
    if (open) {
      const first = shell.rail.querySelector("button");
      if (first) first.focus();
    }
  };
  shell.burger.addEventListener("click", () => setDrawer(!drawerOpen()));
  shell.scrim.addEventListener("mousedown", () => setDrawer(false));
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && drawerOpen()) {
      setDrawer(false);
      shell.burger.focus();
    }
  });
  pm.onActiveChange(() => setDrawer(false));

  // Pane keyboard accelerators (shared by every surface): the per-pane tab-menu
  // actions — close pane, edit/refresh title, fork, delete — driven off the
  // ACTIVE conversational pane's OWN menu, so the chord runs the exact action
  // its badge advertises and each surface contributes only the items it
  // supports (the console omits Fork; a non-conversational pane has no tabMenu
  // and is skipped).  The global accels — new, switch, dashboard — live in each
  // surface's app.js.  None of these chords overlap in-field text editing
  // (close is Mod+W; the rest are Mod+Shift+…), so no typing guard is needed.
  document.addEventListener("keydown", (e) => {
    if (document.querySelector("dialog:modal")) return;
    const accel = paneAccelFor(e);
    if (!accel) return;
    const active = pm.getActive();
    if (!active) return;
    const pane = pm.getPane(active.type, active.rawId);
    if (!pane || typeof pane.tabMenu !== "function") return;
    let item;
    try {
      item = (pane.tabMenu() || []).find((it) => it.accel === accel);
    } catch (err) {
      return; // a pane whose menu throws simply has no accelerators
    }
    if (!item || typeof item.action !== "function") return;
    e.preventDefault();
    item.action();
  });

  // Split controls (the revived split-view): they act on the FOCUSED pane.
  // Split right / split down open a second cell beside/below it, filled with
  // the most-recently-used backgrounded tab; Unsplit returns to one pane and
  // only shows while split.  They replaced the old [+] new-session button —
  // the permanent Dashboard tab IS the launcher, so [+] duplicated one click.
  // Deliberately NO contextmenu override anywhere (the pre-L-shell split UI
  // hijacked right-click): these buttons are the whole affordance surface.
  const tbBtn = (cls, glyph, label) => {
    const b = make("button", cls);
    b.type = "button";
    b.setAttribute("aria-label", label);
    b.title = label;
    const g = make("span", "tb-glyph", glyph);
    g.setAttribute("aria-hidden", "true"); // the button's aria-label speaks
    b.append(g);
    return b;
  };
  // Denials surface as a toast (space / pane-limit / nothing to show) — the
  // manager stays chrome-free and just returns the reason.
  const splitFeedback = (r) => {
    if (r && !r.ok && r.reason && typeof window.showToast === "function")
      window.showToast(r.reason, "warning");
  };
  const splitRightBtn = tbBtn("tb-split", "◫", "Split right");
  splitRightBtn.addEventListener("click", () =>
    splitFeedback(pm.splitFocused("right")),
  );
  const splitDownBtn = tbBtn("tb-split tb-split--down", "◫", "Split down");
  splitDownBtn.addEventListener("click", () =>
    splitFeedback(pm.splitFocused("down")),
  );
  const unsplitBtn = tbBtn("tb-split", "□", "Unsplit — keep the focused pane");
  unsplitBtn.addEventListener("click", () => pm.unsplit());
  const syncSplitControls = () => {
    unsplitBtn.hidden = !pm.isSplit();
  };
  pm.onActiveChange(syncSplitControls);
  syncSplitControls();
  shell.tail.append(splitRightBtn, splitDownBtn, unsplitBtn);

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
      {
        label: "Close pane",
        accel: "close-pane",
        key: paneAccelBadge("close-pane"),
        action: () => pm.close(pane.id),
      },
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
    // The pane's CURRENT transport base for tab-menu verbs: the LIVE
    // controller's (exact), else the persisted node hint, else the live Tier-1
    // node; null = unresolved (node-verbs are omitted until the pane connects).
    // Standalone is always local ("").  A DEAD controller is the exception: its
    // base — and the persisted hint that mirrors it — is stale once its node has
    // lost or RE-HOMED the ws, so trusting it would let the close/delete
    // 404-as-success lanes silently drop a tab whose session is alive on the
    // node it re-homed to.  When dead we therefore mirror the revive path (the
    // live Tier-1 node leads), falling back to the stale base only if the ws is
    // gone cluster-wide, where its 404 correctly reads as "already closed".
    const menuBase = () => {
      if (pane._ctl && pane._ctl.isDead && pane._ctl.isDead()) {
        const live = caps.cluster ? nodeForWs(id) : null;
        return live ? "/node/" + encodeURIComponent(live) : pane._ctl.base;
      }
      if (pane._ctl && pane._ctl.base != null) return pane._ctl.base;
      if (pane.meta && pane.meta.nodeId)
        return "/node/" + encodeURIComponent(pane.meta.nodeId);
      if (!caps.cluster) return "";
      const live = nodeForWs(id);
      return live ? "/node/" + encodeURIComponent(live) : null;
    };
    pane.tabMenu = () => {
      // Close workstream: the standalone's roster-managing global where it
      // exists, else end the session on its own node (confirm-first, like the
      // coordinator's End session) and drop the tab.  Hidden while the node is
      // unresolved — same omit-don't-misaim rule as the other node-verbs.
      const closeBase = menuBase();
      const closeSession =
        typeof window.closeWorkstream === "function"
          ? () => window.closeWorkstream(id)
          : closeBase == null
            ? null
            : () => {
                if (
                  !window.confirm(
                    "End this session? The server will terminate it.",
                  )
                )
                  return;
                const failToast = () => {
                  if (typeof window.showToast === "function")
                    window.showToast("Could not end session", "error");
                };
                postWsVerb(closeBase, id, "close")
                  .then((r) => {
                    // 404 = nothing left to stop (closed under us / node lost
                    // it) — the user's intent is satisfied; drop the tab.
                    if (r.ok || r.status === 404) pm.close(pane.id);
                    else failToast();
                  })
                  .catch(failToast);
              };
      return convTabMenu(pane, pm, id, {
        titleVerbs: true,
        deleteVerb: true,
        base: menuBase,
        closeSession: closeSession,
      });
    };
    // Persist the open-time node hint so a reload re-opens on the SAME node
    // (origin-first; avoids a re-route + duplicate load).  Updated to the
    // resolved node after ensureInteractiveNode settles, below.
    if (extra && extra.nodeId) pane.meta = { nodeId: extra.nodeId };
    pane.onMount = function () {
      // The controller is built LAZILY on first activate (see beginConnect) —
      // a node-proxied session may need its node resolved + (re)opened first.
      // onMount only reserves a status line so a not-yet-connected pane isn't a
      // blank box.
      this._statusEl = make("div", "pane-status", "Connecting…");
      this.bodyEl.append(this._statusEl);
    };
    // Build the controller on a resolved node, persist that node for the next
    // reload, and open the stream.  nodeId null = standalone-local (base="").
    const buildController = (nodeId) => {
      if (pane._closed) return; // resolved after the tab was closed
      // Persist the node so a reload restores onto the SAME node — but skip the
      // write when it already matches (no redundant sessionStorage round-trip).
      if (nodeId && (!pane.meta || pane.meta.nodeId !== nodeId)) {
        pane.meta = { nodeId };
        pm.setPaneMeta(pane.id, pane.meta);
      }
      if (pane._statusEl && pane._statusEl.parentNode) pane._statusEl.remove();
      pane._statusEl = null;
      pane._ctl = createInteractivePane(pane.bodyEl, id, {
        nodeId,
        onClose: () => pm.close(pane.id),
        onDead: showDeadBanner,
      });
      pane._ctl.connect();
      if (window.TS_LOGIN && pane._ctl.onLogin) {
        pane._loginArmed = true;
        window.TS_LOGIN.subscribe(pane._ctl.onLogin);
      }
    };
    // Terminal dead session (the controller exhausted its reconnects, or Tier-1
    // said ws_closed): keep the conversation readable, but surface ONE
    // actionable affordance.  Reviving is never automatic — a deliberately
    // closed session must not resurrect on a timer; the user (or an explicit
    // reopen gesture) decides.
    const showDeadBanner = () => {
      if (pane._closed || pane._deadBanner) return;
      const b = document.createElement("button");
      b.type = "button";
      b.className = "pane-status pane-status--retry pane-dead-banner";
      b.textContent = "Session disconnected — click to reconnect.";
      b.addEventListener("click", () => revive());
      pane.bodyEl.prepend(b);
      pane._deadBanner = b;
    };
    // Tear down the dead controller and re-run the resolve + connect path.
    // forceResolve: the session may need (re)opening on its node (POST /open)
    // or may have re-homed — never trust the dead controller's base.  An
    // explicit fresh hint (a saved-row click carries the roster's node_id)
    // supersedes the stale persisted one.
    const revive = (freshNodeId) => {
      if (pane._closed || pane._resolving || !pane._ctl) return;
      if (pane._deadBanner) {
        pane._deadBanner.remove();
        pane._deadBanner = null;
      }
      if (window.TS_LOGIN && pane._ctl.onLogin)
        window.TS_LOGIN.unsubscribe(pane._ctl.onLogin);
      pane._ctl.destroy();
      pane._ctl = null;
      if (freshNodeId && (!pane.meta || pane.meta.nodeId !== freshNodeId)) {
        pane.meta = { nodeId: freshNodeId };
        pm.setPaneMeta(pane.id, pane.meta);
      }
      pane._statusEl = make("div", "pane-status", "Reconnecting…");
      pane.bodyEl.append(pane._statusEl);
      beginConnect(true);
    };
    // Errored resolve (capacity / no node free): show it in the status line and
    // offer a one-click retry — re-clicking the tab won't re-fire onActivate
    // (PaneManager fires it only on a pane CHANGE), so without this a transient
    // failure would strand the pane until the user closed + reopened it.
    const showResolveError = (msg, forceResolve) => {
      const el = pane._statusEl;
      if (!el) return;
      el.className = "pane-status pane-status--retry msg error";
      el.textContent = msg || "Could not connect to this session.";
      el.title = "Click to retry";
      el.onclick = () => {
        if (pane._ctl || pane._resolving) return;
        el.className = "pane-status";
        el.title = "";
        el.onclick = null;
        el.textContent = "Connecting…";
        beginConnect(forceResolve);
      };
    };
    // First-activate connect.  A LIVE session (Tier-1 already names its node, so
    // it is loaded there) connects DIRECTLY — no /open round-trip; this is the
    // hot rail / active-row / just-created path.  Standalone runs locally.  Only
    // the dormant / reload case (the snapshot has no node for this ws) resolves
    // the node + (re)opens the session, whose /events would otherwise 404.
    // `forceResolve` (the revive path) skips BOTH fast paths so the resolve
    // POSTs /open — the give-up fired because /events 404'd, so the session
    // needs (re)loading even when a stale Tier-1 row still names a node.  The
    // live node (when one exists) stays the HINT, so the origin-first /open
    // reuses a genuinely-live session in place rather than loading a second
    // copy on the old meta node.
    const beginConnect = (forceResolve) => {
      const liveNode = caps.cluster ? nodeForWs(id) : null;
      if (!forceResolve && (liveNode || !caps.cluster)) {
        buildController(liveNode || null);
        return;
      }
      pane._resolving = true;
      const hint =
        liveNode || (pane.meta && pane.meta.nodeId) || (extra && extra.nodeId);
      ensureInteractiveNode(caps, id, hint, forceResolve).then((res) => {
        pane._resolving = false;
        if (pane._closed) return; // closed mid-resolve — don't build into a detached body
        if (!res || res.error) {
          showResolveError(res && res.error, forceResolve);
          return;
        }
        buildController(res.nodeId);
      });
    };
    pane.onActivate = function () {
      pm.setTabGlyph(pane.id, glyph(stateForWs(id))); // live Tier-1 state glyph
      if (this._ctl) {
        if (this._ctl.isDead && this._ctl.isDead()) {
          showDeadBanner(); // visible terminal state; reviving is the user's call
          return;
        }
        this._ctl.connect(); // built — idempotent re-mark focus
        return;
      }
      if (this._resolving) return; // first-activate resolve already in flight
      beginConnect();
    };
    // Explicit re-open (saved-list resume, rail row, child link) targeted this
    // already-open pane.  A healthy pane needs nothing (activate re-marked
    // focus); a DEAD one revives — this is the "resume with a pre-existing tab"
    // path, which previously focused the dead pane and reconnected nothing.
    pane.onReopen = function (reExtra) {
      if (this._ctl && this._ctl.isDead && this._ctl.isDead())
        revive(reExtra && reExtra.nodeId);
    };
    pane.onDeactivate = function () {
      if (this._ctl && this._ctl.deactivate) this._ctl.deactivate();
    };
    pane.onClose = function () {
      this._closed = true; // a pending resolve must not build after close
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
            // Coordinators carry titles like interactive workstreams now:
            // surface Refresh/Edit title. The default base ("") targets the
            // console origin, where the coord refresh-title / title routes
            // are mounted (same base coordinator.js posts every verb to).
            titleVerbs: true,
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
        // Explicit re-open (saved-list resume with this pane already open): the
        // resume already POSTed /open, so a coordinator whose stream went dead
        // (session was closed, console restarted) just needs a fresh connect —
        // reconnect() no-ops on a healthy OPEN stream.
        pane.onReopen = function () {
          if (this._connected && this._ctl && this._ctl.reconnect)
            this._ctl.reconnect();
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

  // Preview pane: rich rendering of tool-selected content (the open_preview
  // tool) — a singleton that opens BESIDE the conversation that produced it.
  // Surface-agnostic: the descriptor arrives on a conversational pane's
  // Tier-2 stream and reaches here through the TS_SHELL.openPreview seam.
  // `extra` is the rehydrate hint (last-viewed descriptor + transport ctx)
  // the pane keeps current via setPaneMeta, so a reload restores the view.
  pm.registerType("preview", (id, extra) => {
    const pane = createPreviewPane(extra, {
      persistMeta: (meta) => pm.setPaneMeta("preview", meta),
      setTitle: (text) => pm.setTabTitle("preview", text),
    });
    pane.tabMenu = () => [
      {
        label: "Close pane",
        accel: "close-pane",
        key: paneAccelBadge("close-pane"),
        action: () => pm.close(pane.id),
      },
    ];
    return pane;
  });
  // Create-or-focus the preview pane BESIDE the focused cell (the
  // conversation stays visible; a denied split degrades to a tab swap
  // inside openPaneBeside), then hand it the descriptor.  `ctx` is the
  // originating pane's transport context ({base, wsId}) — blob fetches ride
  // the same node proxy the session streams from.
  const openPreview = (descriptor, ctx) => {
    if (!descriptor) return;
    const pane = pm.openPaneBeside("preview");
    if (pane && typeof pane.showPreview === "function") {
      pane.showPreview(descriptor, ctx || null);
    }
  };

  // Tier-1 lifecycle → pane signal.  The console's ws_closed handler calls
  // this so an open pane on a CLOSED session closes outright — tab gone, a
  // split cell collapses onto its sibling.  This is the coordinator-closes-
  // its-child flow (and matches the standalone's pane-auto-close); the dead-
  // BANNER lane stays for streams that die WITHOUT a ws_closed (node crash,
  // network) where the session may still be revivable.
  const notifySessionClosed = (wsId) => {
    const p = pm.getPane("interactive", wsId);
    if (p) pm.close(p.id);
  };
  // `setRowBadge` lets a classic-script subsystem (the standalone consent badge
  // in ui/static/app.js) stamp a count chip on a Manage row without importing the
  // ESM rail module — the shell is its module bridge.  Generic: the rail owns the
  // chip mechanism, the caller owns what the count means.
  window.TS_SHELL = {
    panes: pm,
    caps,
    notifySessionClosed,
    setRowBadge,
    inEditable,
    openPreview,
  };

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
    window.TS_APP.onRender(() => {
      paintConvTabs(pm);
      refreshUser();
    });
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

// --- Footer user menu (Log out lives here) ---------------------------------
// A small popup anchored to the rail-footer user chip, riding the shared
// openPopupMenu chrome (pane.js — same vocabulary + keyboard behaviour as the
// tab-action dropdown).  The one item clicks the hidden #logout-btn so auth.js
// stays the single owner of logout (incl. its in-flight-refresh race guards).
let _userMenu = null;

function toggleUserMenu(chip) {
  if (_userMenu) {
    _userMenu.close();
    return;
  }
  _userMenu = openPopupMenu(
    chip,
    [
      {
        label: "Log out",
        cls: "destructive",
        action: () => {
          const lb = document.getElementById("logout-btn");
          if (lb) lb.click();
        },
      },
    ],
    {
      cls: "user-menu",
      label: "Account",
      prefer: "up", // the footer chip sits at the viewport bottom
      align: "start",
      expandEl: chip,
      returnFocusEl: chip,
      onClose: () => {
        _userMenu = null;
      },
    },
  );
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
