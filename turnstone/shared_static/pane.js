/* ==========================================================================
   PaneManager — the one new abstraction of the L-shell.

   Owns the tab bar + the pane host (`.panes`).  A pane is a typed window onto
   any turnstone surface (dashboard, admin, coordinator, interactive, …); the
   host is generic and every surface is a registered factory.  This is the spine
   the rest of the renovation hangs off — step 1 exercises it with a single
   `dashboard` pane that adopts the legacy `#main`; richer pane types arrive in
   steps 2-5.

   House style: ES module, programmatic DOM (createElement / textContent /
   append), NO innerHTML.  Panes scope all queries to their own `bodyEl` so they
   stay multi-instantiable — never `document.getElementById` from pane code.
   ========================================================================== */

/** Make a pane id safe for use in an element id / `aria-*` reference (keyed
 *  pane ids contain `:`, which is invalid in an unescaped id). */
function cssId(s) {
  return String(s).replace(/[^a-zA-Z0-9_-]/g, "-");
}

/**
 * A pane: a typed window with its own scoped root.  Subtypes (or callers that
 * patch the lifecycle hooks) build content into `bodyEl` on first mount and
 * own any per-pane resources (e.g. a Tier-2 EventSource) across activate/close.
 */
export class ShellPane {
  constructor(opts) {
    opts = opts || {};
    this.type = opts.type || null;
    this.rawId = opts.id != null ? opts.id : null; // ws_id for keyed panes, null for singletons
    this.id = this.rawId == null ? this.type : this.type + ":" + this.rawId;
    this.title = opts.title || this.type || "";
    this.glyph = opts.glyph || null; // a single static char shown in the tab (e.g. "◇"); stateful panes use a live .ui-glyph-* instead
    this.stateful = opts.stateful || false; // conversational panes: tab glyph tracks live Tier-1 state (set via setTabGlyph)
    this.closable = opts.closable !== false; // dashboard is not closable
    // DOM — created and owned by the PaneManager on mount:
    this.el = null; // <section class="pane">
    this.bodyEl = null; // <div class="pane-body"> — pane content host
    this.tabEl = null; // the managed tab in the tab bar
    this._mounted = false;
  }

  /** Build content into `this.bodyEl`.  Runs once, on first mount. */
  onMount() {}
  /** Pane became the visible tab.  (Conversational panes open Tier-2 here.) */
  onActivate() {}
  /** Pane left the visible tab — keep-or-teardown is per-type. */
  onDeactivate() {}
  /** Pane is being destroyed — release resources (close streams, timers). */
  onClose() {}
}

/**
 * Owns the tab bar + pane host.  `openPane(type, id?)` is create-or-focus;
 * singletons are keyed by `type`, multi-instance panes by `type:id`.
 */
export class PaneManager {
  constructor(opts) {
    opts = opts || {};
    this.tabbarEl = opts.tabbarEl;
    this.panesEl = opts.panesEl;
    // managed tabs are inserted before this element (the right-floated region /
    // add-tab affordance), so non-tab tabbar chrome keeps its position.
    this.tailEl = opts.tailEl || null;
    this.caps = opts.caps || {};
    this.storageKey = opts.storageKey || "ts.shell.panes";
    this._types = new Map(); // type -> factory(id) => ShellPane
    this._gates = new Map(); // type -> { canOpen, onDeny } create-time auth gate
    this._panes = new Map(); // paneId -> ShellPane
    this._order = []; // paneId[] — tab order
    this._activeId = null;
    this._activeSubs = []; // active-pane-change listeners (e.g. the rail marker)
    this._openMenu = null; // the currently-open tab-action dropdown, if any
    // The tab bar is a WAI-ARIA tablist; arrow keys rove focus across the open
    // tabs (delegated, so it survives tab reconciliation).
    if (this.tabbarEl) {
      this.tabbarEl.setAttribute("role", "tablist");
      this.tabbarEl.setAttribute("aria-label", "Open panes");
      this.tabbarEl.addEventListener("keydown", (e) =>
        this._onTablistKeydown(e),
      );
    }
  }

  /** Register a pane type with a factory; `factory(id)` returns a ShellPane. */
  registerType(type, factory) {
    this._types.set(type, factory);
  }

  /** Attach a create-time auth gate to a registered type: `opts.canOpen(id)` —
   *  a falsy return denies a NEW pane (focusing an already-open one is never
   *  re-gated) — and `opts.onDeny(id)`, the deny feedback (e.g. a toast).  Kept
   *  separate from registerType so the existing 2-arg registrations stay
   *  untouched, and so PaneManager owns no scope knowledge (the shell supplies
   *  the predicate). */
  setAuthGate(type, opts) {
    if (opts) this._gates.set(type, opts);
  }

  hasType(type) {
    return this._types.has(type);
  }

  /** Is a pane currently open (mounted)?  Omit id for a singleton (keyed by type). */
  hasPane(type, id) {
    const paneId = id == null ? type : type + ":" + id;
    return this._panes.has(paneId);
  }

  /** The active pane's identity ({type, rawId}), or null — lets the rail mark
   *  the workspace row that mirrors the active tab. */
  getActive() {
    const p = this._activeId ? this._panes.get(this._activeId) : null;
    return p ? { type: p.type, rawId: p.rawId } : null;
  }

  /** Subscribe to active-pane changes (the rail re-renders its open-marker). */
  onActiveChange(cb) {
    if (typeof cb === "function" && this._activeSubs.indexOf(cb) < 0)
      this._activeSubs.push(cb);
  }

  /** Replace a tab's leading state glyph with `el` (a built glyph span).  The
   *  shell drives this from Tier-1 so a conversational tab shows live shape+colour
   *  state — generic here (PaneManager owns no glyph vocabulary; the shell passes
   *  the element, reusing the rail's builder so tab and rail render identically). */
  setTabGlyph(paneId, el) {
    const pane = this._panes.get(paneId);
    if (!pane || !pane.tabEl || !el) return;
    el.classList.add("tab-glyph"); // tab spacing (margin); colour stays the el's own
    if (pane._glyphEl && pane._glyphEl.parentNode === pane.tabEl)
      pane._glyphEl.replaceWith(el);
    else pane.tabEl.insertBefore(el, pane.tabEl.firstChild);
    pane._glyphEl = el;
  }

  /** Update a pane's persisted open-time meta (a small serializable hint, e.g.
   *  the interactive pane's resolved `{nodeId}`) and re-persist immediately, so
   *  the next reload re-opens the pane with the same hint as `extra`.  Generic:
   *  PaneManager treats meta as opaque; the pane type owns its shape. */
  setPaneMeta(paneId, meta) {
    const pane = this._panes.get(paneId);
    if (!pane) return;
    pane.meta = meta;
    this._persist();
  }

  /** Update a tab's label text in place.  The shell drives this from Tier-1 so a
   *  conversational tab tracks its workstream's live NAME instead of freezing at
   *  the open-time id; pane.title is updated too so a later tab rebuild keeps it. */
  setTabTitle(paneId, text) {
    const pane = this._panes.get(paneId);
    if (!pane || text == null || text === "") return;
    pane.title = text;
    if (pane._titleNode) pane._titleNode.textContent = text;
  }

  /** Open panes whose tab shows live Tier-1 state — `{id, rawId}[]` (the shell
   *  repaints these on every Tier-1 render). */
  statefulTabs() {
    const out = [];
    for (const p of this._panes.values())
      if (p.stateful && p.rawId != null) out.push({ id: p.id, rawId: p.rawId });
    return out;
  }

  /** Create the pane if absent, then focus it.  Creation is auth-gated by the
   *  type's registered `canOpen` (deny -> no pane); focusing an already-open pane
   *  is never re-gated.  `extra` is an optional open-time hint passed straight to
   *  the factory (e.g. the interactive pane's `{nodeId}` from a rail click).  A
   *  factory may copy a SERIALIZABLE hint onto `pane.meta` (and refresh it later
   *  via `setPaneMeta`) to have it persisted and handed back as `extra` on
   *  rehydrate — the interactive pane does this with its resolved nodeId so a
   *  reload restores the pane onto the SAME node (no re-route / duplicate load). */
  openPane(type, id, extra) {
    if (!this._types.has(type)) {
      console.warn("PaneManager: unknown pane type", type);
      return null;
    }
    const paneId = id == null ? type : type + ":" + id;
    let pane = this._panes.get(paneId);
    if (!pane) {
      // Auth gate runs only on CREATE — a denied pane is never built (the backend
      // enforces the scope too; this just avoids opening a doomed pane).
      const gate = this._gates.get(type);
      if (gate && gate.canOpen && !gate.canOpen(id)) {
        if (gate.onDeny) gate.onDeny(id);
        return null;
      }
      pane = this._types.get(type)(id, extra);
      // normalise identity in case the factory left it unset
      pane.type = type;
      pane.rawId = id != null ? id : null;
      pane.id = paneId;
      this._panes.set(paneId, pane);
      this._order.push(paneId);
      this._mount(pane);
    }
    this.activate(paneId);
    return pane;
  }

  _mount(pane) {
    const section = document.createElement("section");
    section.className = "pane";
    section.id = "pane-" + cssId(pane.id);
    section.hidden = true;
    section.setAttribute("role", "tabpanel");
    section.setAttribute("aria-labelledby", "tab-" + cssId(pane.id));
    section.tabIndex = 0; // a scrollable tabpanel is itself focusable
    const body = document.createElement("div");
    body.className = "pane-body";
    section.append(body);
    pane.el = section;
    pane.bodyEl = body;
    this.panesEl.append(section);
    try {
      pane.onMount();
    } catch (e) {
      console.error("PaneManager: pane onMount failed", pane.id, e);
    }
    pane._mounted = true;
    this._renderTabs();
  }

  /** Show one pane, hide the rest, fire deactivate/activate hooks. */
  activate(paneId) {
    // Re-activating the already-active pane is a cheap no-op (it still re-renders
    // tabs / re-persists / re-notifies below, just no onDeactivate/onActivate).
    if (!this._panes.has(paneId)) return;
    const prev = this._activeId ? this._panes.get(this._activeId) : null;
    if (prev && prev.id !== paneId) {
      prev.el.hidden = true;
      try {
        prev.onDeactivate();
      } catch (e) {
        console.error("PaneManager: onDeactivate failed", prev.id, e);
      }
    }
    const next = this._panes.get(paneId);
    next.el.hidden = false;
    const changed = this._activeId !== paneId;
    this._activeId = paneId;
    if (changed) {
      try {
        next.onActivate();
      } catch (e) {
        console.error("PaneManager: onActivate failed", paneId, e);
      }
    }
    this._renderTabs();
    this._persist();
    this._notifyActive();
  }

  _notifyActive() {
    for (const cb of this._activeSubs) {
      try {
        cb();
      } catch (e) {
        console.error("PaneManager: active-change subscriber failed", e);
      }
    }
  }

  /** Drop a pane (tab + content), release it, focus a neighbour. */
  close(paneId) {
    const pane = this._panes.get(paneId);
    if (!pane || pane.closable === false) return;
    this._closeTabMenu(); // a dropdown anchored on the closing tab must not strand
    try {
      pane.onClose();
    } catch (e) {
      console.error("PaneManager: onClose failed", paneId, e);
    }
    if (pane.el && pane.el.parentNode) pane.el.parentNode.removeChild(pane.el);
    this._panes.delete(paneId);
    this._order = this._order.filter((p) => p !== paneId);
    if (this._activeId === paneId) {
      this._activeId = null;
      const fallback = this._order[this._order.length - 1];
      if (fallback)
        this.activate(fallback); // fires _notifyActive itself
      else this._notifyActive(); // last pane closed — clear the marker
    }
    this._renderTabs();
    this._persist();
  }

  _renderTabs() {
    // Reconcile the managed tabs IN PLACE — never destroy + recreate.  Keeps
    // keyboard focus and click/keydown listeners stable across activate / open /
    // close, leaves the tail chrome untouched, and is the seam every later pane
    // type renders its tab through.
    const anchor =
      this.tailEl && this.tailEl.parentNode === this.tabbarEl
        ? this.tailEl
        : null;
    for (const paneId of this._order) {
      const pane = this._panes.get(paneId);
      const tab = pane.tabEl || this._buildTab(pane);
      this._refreshTab(tab, pane);
      this.tabbarEl.insertBefore(tab, anchor); // idempotent reorder
    }
    // Drop tabs whose pane is gone.
    for (const t of Array.from(
      this.tabbarEl.querySelectorAll('[role="tab"]'),
    )) {
      if (!this._panes.has(t.dataset.paneId)) t.remove();
    }
  }

  _buildTab(pane) {
    const tab = document.createElement("button");
    tab.type = "button";
    tab.className = "tab";
    tab.id = "tab-" + cssId(pane.id);
    tab.dataset.paneId = pane.id;
    tab.setAttribute("role", "tab");
    tab.setAttribute("aria-controls", "pane-" + cssId(pane.id));
    if (pane.glyph) {
      // Static decorative char (Dashboard ◇ / Admin ⚙).  Stateful panes get NO
      // static glyph — the shell paints a live .ui-glyph onto the slot via
      // setTabGlyph (on activate + every Tier-1 render).
      const g = document.createElement("span");
      g.className = "glyph tab-glyph";
      g.setAttribute("aria-hidden", "true"); // decorative glyph, not read as content
      g.textContent = pane.glyph;
      tab.append(g);
      pane._glyphEl = g;
    }
    const titleNode = document.createTextNode(pane.title);
    tab.append(titleNode);
    pane._titleNode = titleNode; // setTabTitle repaints this from Tier-1
    // Tab-action menu (step 7): a pane that exposes `tabMenu()` gets a caret to
    // the right of its label that opens the action dropdown (the three-verb close
    // + per-persona verbs).  The Dashboard home tab exposes none, so it gets no
    // caret.  The caret is a <span>, NOT a nested <button> (invalid inside the
    // tab <button>); a click is routed to the menu vs activation by its target.
    if (typeof pane.tabMenu === "function") {
      const caret = document.createElement("span");
      caret.className = "tab-caret";
      caret.setAttribute("aria-hidden", "true");
      caret.textContent = "▾"; // down-caret menu affordance
      tab.append(caret);
      tab.setAttribute("aria-haspopup", "menu");
      tab.setAttribute("aria-expanded", "false");
    }
    tab.addEventListener("click", (e) => {
      if (typeof pane.tabMenu === "function" && e.target.closest(".tab-caret"))
        this._openTabMenu(tab, pane);
      else this.activate(pane.id);
    });
    // Right-click / long-press parity with the caret.
    tab.addEventListener("contextmenu", (e) => {
      if (typeof pane.tabMenu !== "function") return;
      e.preventDefault();
      this._openTabMenu(tab, pane);
    });
    pane.tabEl = tab;
    return tab;
  }

  /** Refresh a tab's selection state: roving tabindex (only the selected tab is
   *  in the Tab order) + aria-selected + the `.active` style hook. */
  _refreshTab(tab, pane) {
    const active = pane.id === this._activeId;
    tab.classList.toggle("active", active);
    tab.setAttribute("aria-selected", active ? "true" : "false");
    tab.tabIndex = active ? 0 : -1;
  }

  /** Roving arrow-key navigation across the open tabs.  Manual activation —
   *  moving focus does NOT switch panes; Enter/Space/click on the focused tab
   *  does, so arrow-scrubbing never thrashes a pane's Tier-2 stream. */
  _onTablistKeydown(e) {
    const tabs = Array.from(this.tabbarEl.querySelectorAll('[role="tab"]'));
    const i = tabs.indexOf(document.activeElement);
    if (i < 0) return;
    // ContextMenu key / Shift+F10 opens the focused tab's action menu — keyboard
    // parity with the caret click (the caret itself is a decorative span).
    if (e.key === "ContextMenu" || (e.shiftKey && e.key === "F10")) {
      const pane = this._panes.get(tabs[i].dataset.paneId);
      if (pane && typeof pane.tabMenu === "function") {
        e.preventDefault();
        this._openTabMenu(tabs[i], pane);
      }
      return;
    }
    let j = i;
    if (e.key === "ArrowRight" || e.key === "ArrowDown")
      j = (i + 1) % tabs.length;
    else if (e.key === "ArrowLeft" || e.key === "ArrowUp")
      j = (i - 1 + tabs.length) % tabs.length;
    else if (e.key === "Home") j = 0;
    else if (e.key === "End") j = tabs.length - 1;
    else return;
    e.preventDefault();
    tabs[j].focus();
  }

  /** Open a pane's tab-action dropdown, anchored under its caret.  Generic: the
   *  item set comes from `pane.tabMenu()` (wired per type in the shell); the
   *  PaneManager owns only the chrome + keyboard + positioning.  Singleton menu —
   *  opening one closes any other.  Items are
   *  `{label, key?, cls?, separator?, action}`. */
  _openTabMenu(tab, pane) {
    this._closeTabMenu();
    let items;
    try {
      items = pane.tabMenu() || [];
    } catch (e) {
      console.error("PaneManager: tabMenu() failed", pane.id, e);
      return;
    }
    if (!items.length) return;

    const menu = document.createElement("div");
    menu.className = "tab-menu";
    menu.setAttribute("role", "menu");
    menu.setAttribute("aria-label", (pane.title || "Pane") + " actions");
    for (const item of items) {
      if (item.separator) {
        const sep = document.createElement("div");
        sep.className = "tab-menu-sep";
        sep.setAttribute("role", "separator");
        menu.append(sep);
        continue;
      }
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "tab-menu-item" + (item.cls ? " " + item.cls : "");
      btn.setAttribute("role", "menuitem");
      btn.tabIndex = -1;
      const label = document.createElement("span");
      label.className = "tab-menu-label";
      label.textContent = item.label;
      btn.append(label);
      if (item.key) {
        const key = document.createElement("span");
        key.className = "tab-menu-key";
        key.setAttribute("aria-hidden", "true"); // a visual hint, not the action
        key.textContent = item.key;
        btn.append(key);
      }
      btn.addEventListener("click", () => {
        this._closeTabMenu();
        try {
          item.action();
        } catch (e) {
          console.error("PaneManager: tab action failed", item.label, e);
        }
      });
      menu.append(btn);
    }
    document.body.append(menu);

    // Position fixed, right-aligned under the caret; flip up / clamp on overflow.
    const anchor = tab.querySelector(".tab-caret") || tab;
    const ar = anchor.getBoundingClientRect();
    const mr = menu.getBoundingClientRect();
    let x = ar.right - mr.width;
    let y = ar.bottom + 2;
    if (x < 4) x = 4;
    if (x + mr.width > window.innerWidth) x = window.innerWidth - mr.width - 4;
    if (y + mr.height > window.innerHeight) y = ar.top - mr.height - 2;
    if (y < 4) y = 4; // never strand the menu above the viewport (short window)
    menu.style.left = x + "px";
    menu.style.top = y + "px";
    tab.setAttribute("aria-expanded", "true");

    const onKey = (e) => {
      const btns = Array.from(menu.querySelectorAll(".tab-menu-item"));
      if (e.key === "Escape" || e.key === "Tab") {
        e.preventDefault();
        this._closeTabMenu();
        tab.focus();
      } else if (
        e.key === "ArrowDown" ||
        e.key === "ArrowUp" ||
        e.key === "Home" ||
        e.key === "End"
      ) {
        e.preventDefault();
        if (!btns.length) return;
        const idx = btns.indexOf(document.activeElement);
        if (e.key === "ArrowDown") btns[(idx + 1) % btns.length].focus();
        else if (e.key === "ArrowUp")
          btns[(idx - 1 + btns.length) % btns.length].focus();
        else if (e.key === "Home") btns[0].focus();
        else btns[btns.length - 1].focus();
      }
    };
    const onDown = (e) => {
      if (!menu.contains(e.target) && !tab.contains(e.target))
        this._closeTabMenu();
    };
    this._openMenu = { menu, tab, onKey, onDown };
    document.addEventListener("keydown", onKey);
    // Defer the outside-mousedown attach a tick so the opening click that
    // bubbled to document doesn't immediately re-close the menu.
    setTimeout(() => {
      if (this._openMenu && this._openMenu.menu === menu)
        document.addEventListener("mousedown", onDown);
    }, 0);
    const first = menu.querySelector(".tab-menu-item");
    if (first) first.focus();
  }

  /** Tear down the open tab-action dropdown (if any) + its document listeners. */
  _closeTabMenu() {
    const m = this._openMenu;
    if (!m) return;
    this._openMenu = null;
    document.removeEventListener("keydown", m.onKey);
    document.removeEventListener("mousedown", m.onDown);
    if (m.menu.parentNode) m.menu.parentNode.removeChild(m.menu);
    if (m.tab && m.tab.isConnected)
      m.tab.setAttribute("aria-expanded", "false");
  }

  _persist() {
    try {
      const order = this._order.map((paneId) => {
        const p = this._panes.get(paneId);
        const entry = { type: p.type, id: p.rawId };
        // A pane's serializable open-time hint (e.g. interactive nodeId) rides
        // along so rehydrate hands it back as `extra` — only when present, so
        // hint-less panes (dashboard/admin/coordinator) persist unchanged.
        if (p.meta != null) entry.meta = p.meta;
        return entry;
      });
      sessionStorage.setItem(
        this.storageKey,
        JSON.stringify({ order, active: this._activeId }),
      );
    } catch (e) {
      /* sessionStorage may be unavailable (private mode / disabled) — non-fatal */
    }
  }

  /**
   * Re-open the persisted pane set + active tab.  Only re-opens types that are
   * registered now, so a future-session pane type degrades to a skip rather
   * than an error.  Returns true if anything was restored.
   */
  rehydrate() {
    let state = null;
    try {
      state = JSON.parse(sessionStorage.getItem(this.storageKey) || "null");
    } catch (e) {
      state = null;
    }
    if (!state || !Array.isArray(state.order)) return false;
    let restored = false;
    for (const item of state.order) {
      if (item && this._types.has(item.type)) {
        // Only count it restored if the pane was actually created — an auth-gated
        // type (a coordinator pane without scope) returns null, and must not
        // suppress the Dashboard fallback into a blank shell.  `item.meta` (e.g.
        // the interactive pane's resolved nodeId) is handed back as `extra`.
        if (this.openPane(item.type, item.id, item.meta)) restored = true;
      }
    }
    if (state.active && this._panes.has(state.active)) {
      this.activate(state.active);
    }
    return restored;
  }
}
