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
    this.glyph = opts.glyph || null; // a single char shown in the tab (e.g. "◇"); state glyphs use ui-base .ui-glyph-*
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
    this._panes = new Map(); // paneId -> ShellPane
    this._order = []; // paneId[] — tab order
    this._activeId = null;
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

  hasType(type) {
    return this._types.has(type);
  }

  /** Create the pane if absent, then focus it.  Auth/cap gating is the caller's. */
  openPane(type, id) {
    if (!this._types.has(type)) {
      console.warn("PaneManager: unknown pane type", type);
      return null;
    }
    const paneId = id == null ? type : type + ":" + id;
    let pane = this._panes.get(paneId);
    if (!pane) {
      pane = this._types.get(type)(id);
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
    if (this._activeId === paneId || !this._panes.has(paneId)) {
      if (!this._panes.has(paneId)) return;
    }
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
  }

  /** Drop a pane (tab + content), release it, focus a neighbour. */
  close(paneId) {
    const pane = this._panes.get(paneId);
    if (!pane || pane.closable === false) return;
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
      if (fallback) this.activate(fallback);
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
      const g = document.createElement("span");
      g.className = "glyph";
      g.setAttribute("aria-hidden", "true"); // decorative glyph, not read as content
      g.textContent = pane.glyph;
      tab.append(g);
    }
    tab.append(document.createTextNode(pane.title));
    tab.addEventListener("click", () => this.activate(pane.id));
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

  _persist() {
    try {
      const order = this._order.map((paneId) => {
        const p = this._panes.get(paneId);
        return { type: p.type, id: p.rawId };
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
        this.openPane(item.type, item.id);
        restored = true;
      }
    }
    if (state.active && this._panes.has(state.active)) {
      this.activate(state.active);
    }
    return restored;
  }
}
