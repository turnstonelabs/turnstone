/* ==========================================================================
   Rail live sections — Cluster health + Workspaces session tree.

   Step 2 of the L-shell: the rail's `Cluster` and `Workspaces` sections (step-1
   stub labels) become live, fed by the Tier-1 `clusterState` that app.js owns
   and exposes via a small `window.TS_APP` seam (getClusterState + onRender +
   the nav actions).  This module never touches `clusterState` directly — it
   reads through the seam and re-renders whenever app.js fires `onRender`.

   House style: ES module, programmatic DOM (createElement / textContent /
   append), NO innerHTML.  State = shape + colour via `ui-base.css .ui-glyph-*`.
   ========================================================================== */

const GLYPH = {
  running: "●",
  thinking: "◐",
  attention: "⚠",
  error: "✗",
  idle: "○",
};
const STATE_LABEL = {
  running: "running",
  thinking: "thinking",
  attention: "attention",
  error: "error",
  idle: "idle",
};

/** A decorative shape+colour state glyph (the row/pill also carries the state
 *  in text/aria, so the glyph itself is aria-hidden). */
function glyph(state) {
  const s = GLYPH[state] ? state : "idle";
  const el = document.createElement("span");
  el.className = "ui-glyph ui-glyph-" + s;
  el.textContent = GLYPH[s];
  el.setAttribute("aria-hidden", "true");
  return el;
}

/** Derive a node's overall state glyph from its workstream mix + health. */
function nodeState(info) {
  if (!info.reachable) return "error";
  if ((info.health || {}).status === "degraded") return "attention";
  if (info.ws_running > 0) return "running";
  if (info.ws_thinking > 0) return "thinking";
  if (info.ws_attention > 0) return "attention";
  return "idle";
}

function tagFor(kind) {
  const tag = document.createElement("span");
  const coord = kind === "coordinator";
  tag.className = "tag" + (coord ? " coord" : "");
  tag.textContent = coord ? "COORD" : "INT";
  return tag;
}

// ---- Cluster section -------------------------------------------------------

function renderCluster(root, cs, TS) {
  root.replaceChildren();
  const card = document.createElement("div");
  card.className = "cluster";

  const overview = (cs && cs.overview) || {};
  const states = overview.states || {};

  // Health pills — always show run/think/idle; attention/error only when present.
  const row = document.createElement("div");
  row.className = "cluster-row";
  const pillStates = ["running", "thinking", "idle"];
  for (const st of ["attention", "error"]) {
    if ((states[st] || 0) > 0) pillStates.splice(2, 0, st);
  }
  for (const st of pillStates) {
    const n = states[st] || 0;
    const pill = document.createElement("button");
    pill.type = "button";
    pill.className = "cpill";
    pill.setAttribute("aria-label", n + " " + STATE_LABEL[st] + ", filter");
    pill.append(glyph(st));
    const b = document.createElement("b");
    b.textContent = String(n);
    pill.append(b, document.createTextNode(" " + STATE_LABEL[st]));
    pill.addEventListener(
      "click",
      () => TS.drillDownByState && TS.drillDownByState(st),
    );
    row.append(pill);
  }
  card.append(row);

  // Node list (real compute nodes only — the "console" pseudo-node hosts
  // coordinators, which live in Workspaces, not the node list).
  const nodeIds = cs
    ? Object.keys(cs.nodes || {}).filter((n) => n !== "console")
    : [];
  if (nodeIds.length) {
    const nodes = nodeIds.map((n) => TS.buildNodeInfo(cs.nodes[n]));
    // The cluster's most-common version — only nodes that DIFFER from it get the
    // amber drift treatment, so the highlight marks the outlier, not every row.
    const verCounts = {};
    for (const n of nodes) {
      if (n.version) verCounts[n.version] = (verCounts[n.version] || 0) + 1;
    }
    const majorityVer = Object.keys(verCounts).sort(
      (a, b) => verCounts[b] - verCounts[a],
    )[0];
    const wrap = document.createElement("div");
    wrap.className = "cluster-nodes";

    const label = document.createElement("div");
    label.className = "nlabel";
    label.append(document.createTextNode("Nodes · " + nodes.length));
    const driftCount = (overview.versions || []).length;
    if (overview.version_drift && driftCount > 1) {
      const drift = document.createElement("span");
      drift.className = "drift";
      drift.append(glyph("attention"), document.createTextNode(" drift"));
      drift.title = (overview.versions || []).join(", ");
      label.append(drift);
    }
    wrap.append(label);

    for (const info of nodes) {
      const item = document.createElement("button");
      item.type = "button";
      item.className = "node-row";
      const st = nodeState(info);
      item.setAttribute(
        "aria-label",
        info.node_id + ", " + st + ", " + info.ws_total + " workstreams",
      );
      item.append(glyph(st));
      const nn = document.createElement("span");
      nn.className = "nn";
      nn.textContent = info.node_id;
      const drifted =
        overview.version_drift && info.version && info.version !== majorityVer;
      const ver = document.createElement("span");
      ver.className = "ver" + (drifted ? " drift" : "");
      if (drifted) ver.title = "drift: cluster majority is " + majorityVer;
      ver.textContent = info.version || "—";
      const nws = document.createElement("span");
      nws.className = "nws";
      nws.textContent = info.ws_total ? info.ws_total + " ws" : "idle";
      item.append(nn, ver, nws);
      item.addEventListener("click", () => {
        window.location.href =
          "/node/" + encodeURIComponent(info.node_id) + "/";
      });
      wrap.append(item);
    }
    card.append(wrap);
  }

  root.append(card);
}

// ---- Workspaces section ----------------------------------------------------

function sessionRow(ws, childCount, isChild, TS) {
  const li = document.createElement("li");
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "row" + (isChild ? " row-child" : "");
  const kind = ws.kind || (ws.parent_ws_id ? "interactive" : "interactive");
  btn.setAttribute(
    "aria-label",
    (ws.name || ws.title || ws.id) + ", " + (ws.state || "idle") + ", " + kind,
  );
  btn.append(glyph(ws.state || "idle"));
  const nm = document.createElement("span");
  nm.className = "nm";
  nm.textContent = ws.name || ws.title || ws.id || "session";
  btn.append(nm);
  if (childCount) {
    const c = document.createElement("span");
    c.className = "rcount";
    c.textContent = String(childCount);
    btn.append(c);
  }
  btn.append(tagFor(kind));
  // Interim navigation until the coordinator/interactive panes exist (steps 4-5):
  // keep today's full-page behaviour.
  btn.addEventListener("click", () => {
    if (kind === "coordinator") {
      window.location.href = "/coordinator/" + encodeURIComponent(ws.id) + "/";
    } else if (ws.node && ws.node !== "console") {
      window.location.href =
        "/node/" +
        encodeURIComponent(ws.node) +
        "/?ws_id=" +
        encodeURIComponent(ws.id);
    }
  });
  li.append(btn);
  return li;
}

function renderWorkspaces(root, cs, TS, paneManager) {
  root.replaceChildren();
  const nav = document.createElement("ul");
  nav.className = "nav";

  // Dashboard entry — activates the Dashboard pane (the one pane in step 2).
  const dashLi = document.createElement("li");
  const dash = document.createElement("button");
  dash.type = "button";
  dash.className = "row open";
  const g = document.createElement("span");
  g.className = "glyph";
  g.setAttribute("aria-hidden", "true");
  g.textContent = "◇";
  dash.append(g);
  const dnm = document.createElement("span");
  dnm.className = "nm";
  dnm.textContent = "Dashboard";
  dash.append(dnm);
  dash.addEventListener(
    "click",
    () => paneManager && paneManager.openPane("dashboard"),
  );
  dashLi.append(dash);
  nav.append(dashLi);

  // Session tree: gather all workstreams (console coordinators + node
  // interactives), bucket by parent so children nest under their coordinator.
  const all = [];
  if (cs) {
    for (const nid of Object.keys(cs.nodes || {})) {
      for (const ws of cs.nodes[nid].workstreams || []) all.push(ws);
    }
  }
  const groups = TS.bucketByParent(all);
  for (const ws of groups.roots) {
    const kids = groups.childrenMap[ws.id] || [];
    const li = sessionRow(ws, kids.length, false, TS);
    if (kids.length) {
      const childUl = document.createElement("ul");
      childUl.className = "children";
      for (const k of kids) childUl.append(sessionRow(k, 0, true, TS));
      li.append(childUl);
    }
    nav.append(li);
  }
  for (const ws of groups.orphans) nav.append(sessionRow(ws, 0, false, TS));

  root.append(nav);
}

// ---- Mount -----------------------------------------------------------------

/**
 * Wire the rail's live sections to the Tier-1 render signal.
 * `sections` = { cluster?: HTMLElement, workspaces: HTMLElement, paneManager }.
 */
export function mountRail(sections, caps) {
  const TS = window.TS_APP || {};
  function render() {
    const cs = TS.getClusterState ? TS.getClusterState() : null;
    if (sections.cluster && caps.cluster)
      renderCluster(sections.cluster, cs, TS);
    if (sections.workspaces)
      renderWorkspaces(sections.workspaces, cs, TS, sections.paneManager);
  }
  if (TS.onRender) TS.onRender(render);
  render(); // initial paint (empty until the first snapshot arrives)
}

// ---- Manage section (admin IA → collapsible discovery groups) --------------

/**
 * Build the rail's Manage groups from the admin IA seam (admin.js exposes
 * `window.TS_ADMIN`).  Each group is a collapsible `.grp` whose head toggles
 * its `.grp-items`; items are the admin tabs the user is permitted to see.  A
 * row click opens/focuses the singleton Admin pane on that tab via the seam's
 * `openTab`.  The IA is static, so this builds once; only the active-row marker
 * re-renders, driven by the admin tab-change subscription (single writer).
 *
 * House style: programmatic DOM, NO innerHTML; reuses the mock's
 * `.grp`/`.grp-head`/`.grp-items` vocabulary (shell.css).
 */
export function mountManage(root) {
  if (!root) return;
  const TS = window.TS_ADMIN || {};
  const ia = TS.ia || [];
  const allowed = TS.isTabAllowed || (() => true);
  root.replaceChildren();

  const rowByTab = new Map(); // tab -> its row <button>, for active-state sync

  ia.forEach((group, gi) => {
    const tabs = group.tabs.filter((t) => allowed(t.tab));
    if (!tabs.length) return; // every tab in the group is gated away → drop it

    const grp = document.createElement("div");
    grp.className = "grp";
    if (gi === 0) grp.classList.add("open"); // first group expanded (mock)

    const itemsId = "manage-grp-" + group.group.toLowerCase();
    const head = document.createElement("button");
    head.type = "button";
    head.className = "grp-head";
    head.setAttribute("aria-expanded", gi === 0 ? "true" : "false");
    head.setAttribute("aria-controls", itemsId);
    const chev = document.createElement("span");
    chev.className = "chev";
    chev.setAttribute("aria-hidden", "true");
    chev.textContent = gi === 0 ? "▾" : "▸";
    const name = document.createElement("span");
    name.className = "gname";
    name.textContent = group.group;
    const count = document.createElement("span");
    count.className = "gcount";
    count.textContent = String(tabs.length);
    head.append(chev, name, count);

    const items = document.createElement("div");
    items.className = "grp-items";
    items.id = itemsId;
    for (const t of tabs) {
      const row = document.createElement("button");
      row.type = "button";
      row.className = "row";
      row.dataset.tab = t.tab;
      const nm = document.createElement("span");
      nm.className = "nm";
      nm.textContent = t.label;
      row.append(nm);
      row.addEventListener("click", () => {
        if (TS.openTab) TS.openTab(t.tab);
      });
      rowByTab.set(t.tab, row);
      items.append(row);
    }

    head.addEventListener("click", () => {
      const open = grp.classList.toggle("open");
      head.setAttribute("aria-expanded", open ? "true" : "false");
      chev.textContent = open ? "▾" : "▸";
    });

    grp.append(head, items);
    root.append(grp);
  });

  // Single writer for the Manage active-row: the row for the current admin tab
  // carries `.active`.  admin.js notifies on every switchAdminTab; nothing is
  // marked until the user actually navigates the admin pane.
  function markActive(tab) {
    for (const [t, row] of rowByTab) row.classList.toggle("active", t === tab);
  }
  if (TS.onTabChange) TS.onTabChange(markActive);
}
