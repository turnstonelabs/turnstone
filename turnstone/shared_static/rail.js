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
      const ver = document.createElement("span");
      ver.className =
        "ver" +
        (overview.version_drift && (overview.versions || []).length > 1
          ? " drift"
          : "");
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
