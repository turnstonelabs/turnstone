/* ==========================================================================
   PaneManager — the one new abstraction of the L-shell.

   Owns the tab bar + the pane host (`.panes`).  A pane is a typed window onto
   any turnstone surface (dashboard, admin, coordinator, interactive, …); the
   host is generic and every surface is a registered factory.  This is the spine
   the rest of the renovation hangs off — step 1 exercises it with a single
   `dashboard` pane that adopts the legacy `#main`; richer pane types arrive in
   steps 2-5.  The split-view section lets the host show several open panes at
   once (the revived split-pane feature — see "Split view" below); without an
   active split tree the manager is strictly one-pane-per-tab.

   House style: ES module, programmatic DOM (createElement / textContent /
   append), NO innerHTML.  Panes scope all queries to their own `bodyEl` so they
   stay multi-instantiable — never `document.getElementById` from pane code.
   ========================================================================== */

/** Make a pane id safe for use in an element id / `aria-*` reference (keyed
 *  pane ids contain `:`, which is invalid in an unescaped id). */
function cssId(s) {
  return String(s).replace(/[^a-zA-Z0-9_-]/g, "-");
}

/* ----- Split view limits (the revived split-pane feature) -----
   A split cell below ~200×150 can't render a usable conversation (the composer
   alone needs ~150px of width headroom); 6 cells is the old ui/static ceiling,
   kept — past it the cells fall under the minimums on any sane viewport. */
const SPLIT_MAX_CELLS = 6;
const SPLIT_MIN_W = 200;
const SPLIT_MIN_H = 150;
const SPLIT_HANDLE_PX = 7; // keep in sync with shell.css .split-handle--row/--col

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
    // Ephemeral: a pane with no standalone background-tab life.  Dismissing its
    // split cell (the ✕ chip, or unsplit) CLOSES it — destroy, not the default
    // hide-and-keep-the-tab — because there is no meaningful re-open-from-tab;
    // its reopen affordance lives elsewhere (the transcript preview chip).  The
    // preview singleton sets this; conversational panes do not.
    this.ephemeral = opts.ephemeral || false;
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
  /** An openPane() call targeted this ALREADY-OPEN pane — explicit user intent
   *  (saved-list resume, rail row, child link), distinct from onActivate which
   *  also fires on plain tab switches and only on a pane CHANGE.  Conversational
   *  panes use this to revive a dead session even when the pane is already the
   *  active tab.  `extra` is the caller's open-time hint (e.g. `{nodeId}`). */
  onReopen(extra) {}
  /** Pane is being destroyed — release resources (close streams, timers). */
  onClose() {}
}

/**
 * Generic popup menu on the shared `.tab-menu` chrome — items, positioning,
 * dismissal (Escape/Tab, outside-mousedown), and arrow-key roving in one
 * place.  Used by the PaneManager's tab-action dropdown and the shell's
 * footer user menu (one menu vocabulary, one behaviour).
 *
 * `items` are `{label, key?, cls?, separator?, action}`.  `opts`:
 *   - label:         the menu's aria-label.
 *   - cls:           extra class(es) on the `.tab-menu` element.
 *   - prefer:        "down" (default) opens under the anchor, flipping up on
 *                    overflow; "up" the reverse (e.g. a viewport-bottom chip).
 *   - align:         "end" (default) right-aligns to the anchor; "start" left.
 *   - expandEl:      element whose aria-expanded mirrors the menu (often the
 *                    anchor's host button).
 *   - returnFocusEl: focus target when the menu closes via keyboard.
 *   - ignoreEl:      outside-mousedown ignore region (defaults to the anchor).
 *   - onClose:       cleanup notification (fires exactly once).
 *
 * Returns `{ menu, close }`; `close()` is idempotent.
 */
export function openPopupMenu(anchor, items, opts) {
  opts = opts || {};
  const menu = document.createElement("div");
  menu.className = "tab-menu" + (opts.cls ? " " + opts.cls : "");
  menu.setAttribute("role", "menu");
  if (opts.label) menu.setAttribute("aria-label", opts.label);

  let closed = false;
  const close = () => {
    if (closed) return;
    closed = true;
    document.removeEventListener("keydown", onKey);
    document.removeEventListener("mousedown", onDown);
    if (menu.parentNode) menu.parentNode.removeChild(menu);
    if (opts.expandEl && opts.expandEl.isConnected)
      opts.expandEl.setAttribute("aria-expanded", "false");
    if (opts.onClose) opts.onClose();
  };

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
      close();
      // close() just removed the focused item from the DOM — without a
      // handoff, focus falls to <body>, and a dialog opened by the action
      // captures body as its opener (so its close-restore no-ops). Hand
      // focus to the menu's return target before the action runs.
      const back = opts.returnFocusEl || anchor;
      if (back && back.isConnected) back.focus();
      try {
        item.action();
      } catch (e) {
        console.error("popup menu: action failed", item.label, e);
      }
    });
    menu.append(btn);
  }
  document.body.append(menu);

  // Position fixed against the anchor; flip on overflow, clamp to viewport.
  const ar = anchor.getBoundingClientRect();
  const mr = menu.getBoundingClientRect();
  let x = opts.align === "start" ? ar.left : ar.right - mr.width;
  if (x < 4) x = 4;
  if (x + mr.width > window.innerWidth) x = window.innerWidth - mr.width - 4;
  let y;
  if (opts.prefer === "up") {
    y = ar.top - mr.height - 4;
    if (y < 4) y = ar.bottom + 4;
  } else {
    y = ar.bottom + 2;
    if (y + mr.height > window.innerHeight) y = ar.top - mr.height - 2;
    if (y < 4) y = 4; // never strand the menu above the viewport (short window)
  }
  menu.style.left = x + "px";
  menu.style.top = y + "px";
  if (opts.expandEl) opts.expandEl.setAttribute("aria-expanded", "true");

  const onKey = (e) => {
    const btns = Array.from(menu.querySelectorAll(".tab-menu-item"));
    if (e.key === "Escape" || e.key === "Tab") {
      e.preventDefault();
      close();
      if (opts.returnFocusEl) opts.returnFocusEl.focus();
    } else if (
      e.key === "ArrowDown" ||
      e.key === "ArrowUp" ||
      e.key === "Home" ||
      e.key === "End"
    ) {
      e.preventDefault();
      if (!btns.length) return;
      // idx -1 = no item focused (a click on a separator / the menu surface
      // moves focus off the items without closing).  ArrowDown's modulo
      // already enters at the top then; ArrowUp must enter at the BOTTOM —
      // unguarded, (-1 - 1 + n) % n lands on the second-to-last item.
      const idx = btns.indexOf(document.activeElement);
      if (e.key === "ArrowDown") btns[(idx + 1) % btns.length].focus();
      else if (e.key === "ArrowUp")
        btns[
          idx < 0 ? btns.length - 1 : (idx - 1 + btns.length) % btns.length
        ].focus();
      else if (e.key === "Home") btns[0].focus();
      else btns[btns.length - 1].focus();
    }
  };
  const ignoreEl = opts.ignoreEl || anchor;
  const onDown = (e) => {
    if (!menu.contains(e.target) && !ignoreEl.contains(e.target)) close();
  };
  document.addEventListener("keydown", onKey);
  // Defer the outside-mousedown attach a tick so the opening click that
  // bubbled to document doesn't immediately re-close the menu.
  setTimeout(() => {
    if (!closed) document.addEventListener("mousedown", onDown);
  }, 0);
  const first = menu.querySelector(".tab-menu-item");
  if (first) first.focus();
  return { menu, close };
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
    // Split view: a binary layout tree ({type:"leaf",paneId} | {type:"split",
    // dir:"row"|"col", ratio, children:[2]}), or null — null is single-pane
    // mode, where every code path below behaves exactly as before the feature.
    // Visible panes are positioned by inline % insets (no reparenting: a pane's
    // live SSE DOM and media elements are never detached).
    this._layout = null;
    this._handleEls = []; // live .split-handle separators (rebuilt per layout change)
    this._mru = []; // paneId[], most-recently-focused first (split auto-fill order)
    // Clicking anywhere inside a visible-but-unfocused pane focuses its cell
    // (capture phase — pane content may stopPropagation on bubbled events).
    if (this.panesEl) {
      this.panesEl.addEventListener(
        "pointerdown",
        (e) => this._onPanesPointerdown(e),
        true,
      );
    }
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

  /** The open pane for (type, id), or null — lets the shell reach a pane for
   *  cross-cutting lifecycle signals (e.g. Tier-1 ws_closed → mark its session
   *  controller dead).  Omit id for a singleton. */
  getPane(type, id) {
    const paneId = id == null ? type : type + ":" + id;
    return this._panes.get(paneId) || null;
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
  openPane(type, id, extra, _beside) {
    if (!this._types.has(type)) {
      console.warn("PaneManager: unknown pane type", type);
      return null;
    }
    const paneId = id == null ? type : type + ":" + id;
    let pane = this._panes.get(paneId);
    const existed = !!pane;
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
    if (_beside && this._activeId !== paneId && !this._leafFor(paneId)) {
      // Open BESIDE the focused cell (see openPaneBeside) — a denied split
      // (cap / space) degrades to the plain focused-cell placement below.
      const r = this.splitFocused("right", paneId);
      if (!r.ok) this.activate(paneId);
    } else {
      this.activate(paneId);
    }
    // Explicit-reopen signal: openPane on an existing pane is a user saying
    // "open this AGAIN" (saved-list resume, rail row, child link) — activate()
    // alone can't carry that (it no-ops hooks on the already-active pane, and
    // onActivate also fires on plain tab switches).  Fired AFTER activate so
    // the pane is visible when it reacts (e.g. revives a dead session).
    if (existed) {
      try {
        pane.onReopen(extra);
      } catch (e) {
        console.error("PaneManager: onReopen failed", paneId, e);
      }
    }
    return pane;
  }

  /** openPane, but a pane that was not already on screen lands in a fresh
   *  cell to the RIGHT of the focused one instead of replacing it — the
   *  coordinator child-link gesture (the parent stays visible; you are
   *  usually cross-checking the child against the tree that spawned it).
   *  An already-visible pane is just focused; a denied split (cell cap /
   *  not enough space) degrades to the plain focused-cell swap.  Everything
   *  else (auth gate, factory hint, onReopen) is openPane's. */
  openPaneBeside(type, id, extra) {
    return this.openPane(type, id, extra, true);
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

  /** Show one pane (single-pane mode: hide the rest) or focus it (split mode),
   *  firing deactivate/activate hooks.  In split mode "active" means FOCUSED:
   *  a pane already in a cell keeps every cell as-is (pure focus move); a
   *  backgrounded pane swaps into the focused cell, parking that cell's
   *  current pane.  onDeactivate therefore means "lost focus", not necessarily
   *  "hidden" — which matches the panes' contract (interactive panes only stop
   *  focus-stealing and keep streaming: exactly what a visible-but-unfocused
   *  cell wants). */
  activate(paneId) {
    // Re-activating the already-active pane is a cheap no-op (it still re-renders
    // tabs / re-persists / re-notifies below, just no onDeactivate/onActivate).
    if (!this._panes.has(paneId)) return;
    const prev = this._activeId ? this._panes.get(this._activeId) : null;
    const next = this._panes.get(paneId);
    const changed = this._activeId !== paneId;
    if (this._layout) {
      if (changed && !this._leafFor(paneId)) {
        // Swap the backgrounded pane into the focused cell.
        const target = this._leafFor(this._activeId) || this._firstLeaf();
        const old = target ? this._panes.get(target.paneId) : null;
        if (target) target.paneId = paneId;
        if (old && old !== next) {
          old.el.hidden = true;
          this._clearCellStyle(old);
        }
      }
    } else if (prev && prev.id !== paneId) {
      prev.el.hidden = true;
    }
    if (changed && prev) {
      try {
        prev.onDeactivate();
      } catch (e) {
        console.error("PaneManager: onDeactivate failed", prev.id, e);
      }
    }
    next.el.hidden = false;
    this._activeId = paneId;
    if (changed) {
      // Most-recently-focused order — the split auto-fill source.
      this._mru = [paneId].concat(this._mru.filter((p) => p !== paneId));
      try {
        next.onActivate();
      } catch (e) {
        console.error("PaneManager: onActivate failed", paneId, e);
      }
    }
    if (this._layout) this._applyLayout();
    this._refreshCellChips();
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

  /** Drop a pane (tab + content), release it, focus a neighbour.  In split
   *  mode a visible pane's cell collapses first (its sibling takes the space),
   *  and the sibling is preferred as the fallback focus target so closing a
   *  cell lands you on the pane that absorbed it. */
  close(paneId) {
    const pane = this._panes.get(paneId);
    if (!pane || pane.closable === false) return;
    this._closeTabMenu(); // a dropdown anchored on the closing tab must not strand
    let preferFallback = null;
    if (this._layout) {
      const leaf = this._leafFor(paneId);
      if (leaf) preferFallback = this._collapseLeaf(leaf);
    }
    try {
      pane.onClose();
    } catch (e) {
      console.error("PaneManager: onClose failed", paneId, e);
    }
    if (pane.el && pane.el.parentNode) pane.el.parentNode.removeChild(pane.el);
    this._panes.delete(paneId);
    this._order = this._order.filter((p) => p !== paneId);
    this._mru = this._mru.filter((p) => p !== paneId);
    if (this._activeId === paneId) {
      this._activeId = null;
      const fallback = preferFallback || this._order[this._order.length - 1];
      if (fallback)
        this.activate(fallback); // fires _notifyActive itself
      else this._notifyActive(); // last pane closed — clear the marker
    } else {
      this._notifyActive(); // split state may have changed (a cell collapsed)
    }
    this._renderTabs();
    this._persist();
  }

  /* ===== Split view ============================================================
     The layout tree shows MORE than one open pane at once.  Tabs stay global:
     the active tab is the FOCUSED cell, clicking a backgrounded tab swaps that
     pane into the focused cell, clicking inside a visible pane focuses its
     cell.  Cells are rendered as inline % insets on the pane elements
     themselves — a pane is NEVER reparented or detached, so its live stream
     DOM, scroll positions and media elements are untouched by layout changes.
     With `_layout === null` (the default) every path above behaves exactly as
     it did before this feature existed.
     ========================================================================== */

  /** Is the pane host currently showing more than one cell? */
  isSplit() {
    return !!this._layout;
  }

  /** Split the focused pane's cell — `dir` "right" puts the new cell beside
   *  it, "down" below it.  The new cell shows `fillId` when given (the
   *  openPaneBeside path), else the most-recently-focused backgrounded pane.
   *  Splitting never duplicates a pane (panes are keyed singletons — two
   *  mounts of one session would race its stream).  Returns `{ok:true}` or
   *  `{ok:false, reason}`; the CALLER owns user feedback (the shell toasts
   *  the reason — PaneManager stays chrome-free). */
  splitFocused(dir, fillId) {
    const activeId = this._activeId;
    if (!activeId || !this._panes.has(activeId))
      return { ok: false, reason: "Nothing to split" };
    if (this._leafCount() >= SPLIT_MAX_CELLS)
      return {
        ok: false,
        reason: "Pane limit reached (" + SPLIT_MAX_CELLS + ")",
      };
    if (fillId != null) {
      // An explicit fill must be an open, backgrounded, non-focused pane —
      // anything else is the caller's bug; deny so it can degrade cleanly.
      if (
        !this._panes.has(fillId) ||
        fillId === activeId ||
        this._leafFor(fillId)
      )
        return { ok: false, reason: "Not splittable" };
    } else {
      fillId = this._nextBackgroundPane();
      if (!fillId)
        return {
          ok: false,
          reason: "Every open tab is already visible — open another one first",
        };
    }
    // Space guard: the focused cell must fit two cells plus the divider.
    const rect = this._cellRect(activeId);
    const need =
      (dir === "down" ? SPLIT_MIN_H : SPLIT_MIN_W) * 2 + SPLIT_HANDLE_PX;
    if ((dir === "down" ? rect.h : rect.w) < need)
      return { ok: false, reason: "Not enough space to split" };
    const leaf = this._layout ? this._leafFor(activeId) : null;
    const node = {
      type: "split",
      dir: dir === "down" ? "col" : "row",
      ratio: 0.5,
      children: [
        leaf || { type: "leaf", paneId: activeId },
        { type: "leaf", paneId: fillId },
      ],
    };
    if (!leaf) {
      this._layout = node;
    } else {
      const found = this._findParent(this._layout, leaf);
      if (found) found.parent.children[found.index] = node;
      else this._layout = node; // the leaf was the root (defensive — see _collapseLeaf)
    }
    this.panesEl.classList.add("panes--split");
    this._applyLayout(true);
    this.activate(fillId); // focus the new cell (already a leaf → pure focus move)
    return { ok: true };
  }

  /** Collapse back to a single pane — the focused one.  Other panes stay open
   *  as tabs (they just stop being visible) — EXCEPT ephemeral ones (e.g. the
   *  preview), which have no background-tab life and close outright rather than
   *  linger as orphan tabs.  The focused survivor is spared even if ephemeral. */
  unsplit() {
    if (!this._layout) return;
    const keep = this._activeId;
    const doomed = this._leaves()
      .map((l) => l.paneId)
      .filter((id) => {
        const p = this._panes.get(id);
        return id !== keep && p && p.ephemeral;
      });
    for (const id of doomed) this.close(id); // collapses its cell, then destroys
    if (this._layout) this._exitLayout(keep); // a close() may have already exited
    this._renderTabs();
    this._persist();
    this._notifyActive();
  }

  /** Remove ONE cell from the split — the pane stays open as a (now hidden)
   *  tab and its sibling absorbs the space.  The per-cell ✕ chip calls this;
   *  distinct from close() (destroys the pane) and unsplit() (collapses every
   *  cell but the focused one). */
  closeCell(paneId) {
    if (!this._layout) return;
    const leaf = this._leafFor(paneId);
    if (!leaf) return;
    const sibling = this._collapseLeaf(leaf); // hides the pane + may exit split mode
    if (this._activeId === paneId && sibling) {
      this.activate(sibling); // fires the focus hooks + renders + persists + notifies
    } else {
      this._renderTabs();
      this._persist();
      this._notifyActive();
    }
  }

  // ----- tree helpers -----

  _leaves(node, out) {
    out = out || [];
    node = node || this._layout;
    if (!node) return out;
    if (node.type === "leaf") out.push(node);
    else {
      this._leaves(node.children[0], out);
      this._leaves(node.children[1], out);
    }
    return out;
  }

  _leafCount() {
    return this._layout ? this._leaves().length : 1;
  }

  _leafFor(paneId) {
    if (paneId == null || !this._layout) return null;
    return this._leaves().find((l) => l.paneId === paneId) || null;
  }

  _firstLeaf(node) {
    node = node || this._layout;
    if (!node) return null;
    return node.type === "leaf" ? node : this._firstLeaf(node.children[0]);
  }

  _findParent(node, target) {
    if (!node || node.type === "leaf") return null;
    for (let i = 0; i < 2; i++) {
      if (node.children[i] === target) return { parent: node, index: i };
      const found = this._findParent(node.children[i], target);
      if (found) return found;
    }
    return null;
  }

  /** Remove a leaf: its sibling subtree takes the parent's place.  Exits split
   *  mode when one cell remains.  Returns the sibling's first pane id — the
   *  natural focus target for a close() that emptied the focused cell. */
  _collapseLeaf(leaf) {
    const found = this._findParent(this._layout, leaf);
    if (!found) {
      // The leaf IS the root — a tree this small should already have exited
      // split mode; recover rather than strand a stale layout.
      this._exitLayout(null);
      return null;
    }
    const sibling = found.parent.children[found.index === 0 ? 1 : 0];
    const grand = this._findParent(this._layout, found.parent);
    if (grand) grand.parent.children[grand.index] = sibling;
    else this._layout = sibling;
    const first = this._firstLeaf(sibling);
    if (this._layout.type === "leaf") this._exitLayout(this._layout.paneId);
    else this._applyLayout(true);
    return first ? first.paneId : null;
  }

  /** The most-recently-focused open pane that is not currently visible —
   *  what a fresh split cell shows.  Null when every open pane is visible. */
  _nextBackgroundPane() {
    const visible = new Set(
      this._layout
        ? this._leaves().map((l) => l.paneId)
        : [this._activeId].filter(Boolean),
    );
    for (const pid of this._mru) {
      if (this._panes.has(pid) && !visible.has(pid)) return pid;
    }
    for (const pid of this._order) {
      if (!visible.has(pid)) return pid;
    }
    return null;
  }

  // ----- geometry + rendering -----

  /** The px rect of a pane's current cell (the whole host when unsplit). */
  _cellRect(paneId) {
    const W = this.panesEl.clientWidth;
    const H = this.panesEl.clientHeight;
    if (!this._layout) return { x: 0, y: 0, w: W, h: H };
    let hit = null;
    const walk = (node, x, y, w, h) => {
      if (hit) return;
      if (node.type === "leaf") {
        if (node.paneId === paneId) hit = { x, y, w, h };
        return;
      }
      const r = node.ratio;
      if (node.dir === "row") {
        walk(node.children[0], x, y, w * r, h);
        walk(node.children[1], x + w * r, y, w * (1 - r), h);
      } else {
        walk(node.children[0], x, y, w, h * r);
        walk(node.children[1], x, y + h * r, w, h * (1 - r));
      }
    };
    walk(this._layout, 0, 0, W, H);
    return hit || { x: 0, y: 0, w: W, h: H };
  }

  /** Lay the visible panes out as % insets and place the separators.
   *  `rebuild` re-creates the handle ELEMENTS (tree structure changed);
   *  without it only styles update, so a mid-drag handle keeps its pointer
   *  capture.  % insets make window resizes free — no JS resize listener. */
  _applyLayout(rebuild) {
    if (!this._layout) return;
    const rects = new Map();
    const handles = [];
    const walk = (node, x, y, w, h) => {
      if (node.type === "leaf") {
        rects.set(node.paneId, { x, y, w, h });
        return;
      }
      const r = node.ratio;
      if (node.dir === "row") {
        walk(node.children[0], x, y, w * r, h);
        handles.push({ node, x: x + w * r, y, span: h });
        walk(node.children[1], x + w * r, y, w * (1 - r), h);
      } else {
        walk(node.children[0], x, y, w, h * r);
        handles.push({ node, x, y: y + h * r, span: w });
        walk(node.children[1], x, y + h * r, w, h * (1 - r));
      }
    };
    walk(this._layout, 0, 0, 1, 1);
    const multi = rects.size > 1;
    for (const p of this._panes.values()) {
      const r = rects.get(p.id);
      if (r) {
        p.el.hidden = false;
        p.el.style.left = r.x * 100 + "%";
        p.el.style.top = r.y * 100 + "%";
        p.el.style.width = r.w * 100 + "%";
        p.el.style.height = r.h * 100 + "%";
        // The focus ring only means something with 2+ cells on screen.
        p.el.classList.toggle(
          "split-focused",
          multi && p.id === this._activeId,
        );
      } else {
        p.el.hidden = true;
        this._clearCellStyle(p);
      }
    }
    if (rebuild) {
      for (const h of this._handleEls) h.remove();
      this._handleEls = handles.map((h) => this._buildHandle(h.node));
    }
    // Position fresh AND surviving handles from the same walk (identical
    // traversal order, so index pairing is stable while the tree shape is).
    for (let i = 0; i < handles.length && i < this._handleEls.length; i++) {
      const h = handles[i];
      const el = this._handleEls[i];
      el.style.left = h.x * 100 + "%";
      el.style.top = h.y * 100 + "%";
      if (h.node.dir === "row") el.style.height = h.span * 100 + "%";
      else el.style.width = h.span * 100 + "%";
      // The ARIA range is the REAL clamp (_ratioBounds: the cell minimums
      // against this split's OWN px region — nested splits sit tighter than
      // any constant; the old hard-coded 10–90 misreported it to AT).  It
      // refreshes with every drag/keyboard/structure pass through here; a
      // bare window resize can stale it until the next interaction (no
      // resize listener by design — % insets make resizes free), which is
      // still strictly truer than a constant.  The max>=min guard covers a
      // host shrunk below two minimums, where the bounds legitimately cross.
      const b = this._ratioBounds(h.node);
      const lo = Math.round(b.min * 100);
      el.setAttribute("aria-valuemin", String(lo));
      el.setAttribute(
        "aria-valuemax",
        String(Math.max(lo, Math.round(b.max * 100))),
      );
      el.setAttribute("aria-valuenow", String(Math.round(h.node.ratio * 100)));
    }
  }

  /** Leave split mode.  `keepId` (usually the focused pane) stays visible and
   *  the other panes hide; null leaves visibility to the caller (the close()
   *  fallback re-activates).  No extra deactivate hooks fire: a pane hidden
   *  here already lost focus — and with it its onDeactivate — earlier. */
  _exitLayout(keepId) {
    this._layout = null;
    this.panesEl.classList.remove("panes--split");
    for (const h of this._handleEls) h.remove();
    this._handleEls = [];
    for (const p of this._panes.values()) {
      this._clearCellStyle(p);
      if (keepId) p.el.hidden = p.id !== keepId;
    }
    this._refreshCellChips(); // the survivor's ✕ flips to close-pane mode
  }

  _clearCellStyle(pane) {
    pane.el.style.left = "";
    pane.el.style.top = "";
    pane.el.style.width = "";
    pane.el.style.height = "";
    pane.el.classList.remove("split-focused");
    this._removeCellChip(pane);
  }

  /** The per-pane ✕ chip, top-right of every VISIBLE pane.  Mode-dependent:
   *  in a multi-cell split it hides that cell (closeCell — the tab stays);
   *  single-pane it closes the pane outright (tab and all), so it is withheld
   *  from non-closable panes (the Dashboard) there.  The click decides at
   *  CLICK time, the label tracks the mode.  Injected by the MANAGER into the
   *  pane's section (not bodyEl — pane content is never touched). */
  _refreshCellChips() {
    const multi = !!this._layout && this._leaves().length > 1;
    for (const p of this._panes.values()) {
      const want =
        !p.el.hidden && (multi ? !!this._leafFor(p.id) : p.closable !== false);
      if (want) this._ensureCellChip(p, multi);
      else this._removeCellChip(p);
    }
  }

  _ensureCellChip(pane, multi) {
    let b = pane._cellChip;
    if (!b || !b.isConnected) {
      b = document.createElement("button");
      b.type = "button";
      b.className = "cell-unsplit";
      b.addEventListener("click", () => {
        // In a multi-cell split the chip HIDES this cell (the tab stays) —
        // except an ephemeral pane (e.g. the preview), which has no background-
        // tab life and so closes outright, exactly as it does single-pane.
        if (this._layout && this._leafFor(pane.id) && !pane.ephemeral)
          this.closeCell(pane.id);
        else this.close(pane.id);
      });
      pane.el.append(b);
      pane._cellChip = b;
    }
    // Mode-DISTINCT glyphs — an identical signifier at an identical locus with
    // divergent outcomes is a mode-error trap (split-mode muscle memory would
    // fire the destructive close): − hides the cell (reversible — the tab
    // stays), ✕ closes the pane.  The chip DESTROYS whenever the click cannot be
    // a reversible cell-hide: single-pane always, and an ephemeral pane even in
    // a split.  Close mode also wears a danger hover (shell.css .cell-unsplit--close).
    const destroys = !multi || pane.ephemeral;
    b.textContent = destroys ? "✕" : "−";
    b.classList.toggle("cell-unsplit--close", destroys);
    const label = destroys
      ? "Close pane"
      : "Hide from split — the tab stays open";
    b.title = label;
    b.setAttribute("aria-label", label);
  }

  _removeCellChip(pane) {
    if (pane._cellChip) {
      pane._cellChip.remove();
      pane._cellChip = null;
    }
  }

  /** Focus follows the pointer between cells: a click anywhere inside a
   *  visible-but-unfocused pane focuses it (capture phase — pane content may
   *  stop propagation of bubbled events). */
  _onPanesPointerdown(e) {
    if (!this._layout) return;
    // The ✕ chip collapses its cell — focusing that cell first would fire a
    // spurious onActivate on the very pane about to leave the screen.
    if (e.target.closest && e.target.closest(".cell-unsplit")) return;
    let el = e.target;
    // The ShellPane <section> is the DIRECT child of the host (the interactive
    // pane's inner <div> also carries .pane — walking to the direct child
    // disambiguates without knowing any pane-type internals).
    while (el && el.parentElement !== this.panesEl) el = el.parentElement;
    if (!el || !el.classList || !el.classList.contains("pane")) return;
    for (const p of this._panes.values()) {
      if (p.el === el) {
        if (p.id !== this._activeId && this._leafFor(p.id)) this.activate(p.id);
        return;
      }
    }
  }

  // ----- separators (drag + keyboard resize) -----

  _buildHandle(node) {
    const el = document.createElement("div");
    el.className = "split-handle split-handle--" + node.dir;
    el.setAttribute("role", "separator");
    el.tabIndex = 0;
    el.setAttribute(
      "aria-orientation",
      node.dir === "row" ? "vertical" : "horizontal",
    );
    // aria-valuenow/min/max are written by _applyLayout's handle loop (the
    // single writer) — the range comes from _ratioBounds, not a constant.
    el.setAttribute(
      "aria-label",
      node.dir === "row"
        ? "Resize panes horizontally"
        : "Resize panes vertically",
    );
    this._wireHandle(el, node);
    this.panesEl.append(el);
    return el;
  }

  /** Ratio bounds that keep both children of a split above the cell minimums,
   *  derived from the split node's CURRENT px region (so nested splits clamp
   *  against their own space, not the whole host). */
  _ratioBounds(node) {
    const W = this.panesEl.clientWidth;
    const H = this.panesEl.clientHeight;
    let region = null;
    const walk = (n, x, y, w, h) => {
      if (region) return;
      if (n === node) {
        region = { w, h };
        return;
      }
      if (n.type === "leaf") return;
      const r = n.ratio;
      if (n.dir === "row") {
        walk(n.children[0], x, y, w * r, h);
        walk(n.children[1], x + w * r, y, w * (1 - r), h);
      } else {
        walk(n.children[0], x, y, w, h * r);
        walk(n.children[1], x, y + h * r, w, h * (1 - r));
      }
    };
    walk(this._layout, 0, 0, W, H);
    const px = region ? (node.dir === "row" ? region.w : region.h) : 0;
    const minPx = node.dir === "row" ? SPLIT_MIN_W : SPLIT_MIN_H;
    return {
      min: px > 0 ? Math.max(0.05, minPx / px) : 0.1,
      max: px > 0 ? Math.min(0.95, 1 - minPx / px) : 0.9,
      px: px || 1,
    };
  }

  _wireHandle(el, node) {
    el.addEventListener("pointerdown", (e) => {
      // Touch/pen carry no primary-button semantics — only filter MOUSE
      // non-primary buttons (a bare `e.button !== 0` would break touch).
      if (e.button !== 0 && e.pointerType === "mouse") return;
      e.preventDefault();
      el.setPointerCapture(e.pointerId);
      el.classList.add("dragging");
      const bounds = this._ratioBounds(node);
      const startRatio = node.ratio;
      const horiz = node.dir === "row";
      const startPos = horiz ? e.clientX : e.clientY;
      document.body.style.cursor = horiz ? "col-resize" : "row-resize";
      document.body.style.userSelect = "none";
      const onMove = (e2) => {
        const delta = (horiz ? e2.clientX : e2.clientY) - startPos;
        node.ratio = Math.max(
          bounds.min,
          Math.min(bounds.max, startRatio + delta / bounds.px),
        );
        this._applyLayout();
      };
      const onUp = () => {
        el.classList.remove("dragging");
        document.body.style.cursor = "";
        document.body.style.userSelect = "";
        document.removeEventListener("pointermove", onMove);
        document.removeEventListener("pointerup", onUp);
        document.removeEventListener("pointercancel", onUp);
        this._persist(); // the settled ratio is part of the working set
      };
      // The move/up listeners live on DOCUMENT, not the handle: a layout
      // rebuild can remove the handle MID-DRAG (e.g. a server-side ws_closed
      // collapses a cell), and handle-bound listeners would die with it,
      // stranding the body-wide cursor/user-select overrides.  Capture loss on
      // removal just redirects the events to the hit-test chain — document
      // still hears them and onUp always runs.
      document.addEventListener("pointermove", onMove);
      document.addEventListener("pointerup", onUp);
      document.addEventListener("pointercancel", onUp);
    });
    // Keyboard resize: arrows nudge (Shift = coarse), Home/End to the bounds.
    el.addEventListener("keydown", (e) => {
      const bounds = this._ratioBounds(node);
      const step = e.shiftKey ? 0.1 : 0.02;
      let delta = 0;
      if (e.key === "ArrowRight" || e.key === "ArrowDown") delta = step;
      else if (e.key === "ArrowLeft" || e.key === "ArrowUp") delta = -step;
      else if (e.key === "Home") delta = bounds.min - node.ratio;
      else if (e.key === "End") delta = bounds.max - node.ratio;
      else return;
      e.preventDefault();
      node.ratio = Math.max(
        bounds.min,
        Math.min(bounds.max, node.ratio + delta),
      );
      this._applyLayout();
      this._persist();
    });
  }

  // ----- persistence -----

  _serializeLayout(node) {
    if (node.type === "leaf") return { type: "leaf", paneId: node.paneId };
    return {
      type: "split",
      dir: node.dir,
      ratio: node.ratio,
      children: [
        this._serializeLayout(node.children[0]),
        this._serializeLayout(node.children[1]),
      ],
    };
  }

  /** Re-apply a persisted layout after rehydrate re-opened the panes.  Leaves
   *  whose pane did not restore (skipped type, auth-denied, duplicate) prune
   *  away and their sibling absorbs the space — the same degrade-don't-error
   *  stance rehydrate takes on pane types. */
  _restoreLayout(data) {
    const seen = new Set();
    const prune = (d) => {
      if (!d || typeof d !== "object") return null;
      if (d.type === "leaf") {
        if (!this._panes.has(d.paneId) || seen.has(d.paneId)) return null;
        seen.add(d.paneId);
        return { type: "leaf", paneId: d.paneId };
      }
      if (d.type !== "split" || !Array.isArray(d.children)) return null;
      const a = prune(d.children[0]);
      const b = prune(d.children[1]);
      if (!a || !b) return a || b;
      return {
        type: "split",
        dir: d.dir === "col" ? "col" : "row",
        ratio:
          typeof d.ratio === "number" && d.ratio >= 0.05 && d.ratio <= 0.95
            ? d.ratio
            : 0.5,
        children: [a, b],
      };
    };
    const tree = prune(data);
    if (!tree || tree.type === "leaf") return; // 0-1 cells — stay single-pane
    this._layout = tree;
    this.panesEl.classList.add("panes--split");
    this._applyLayout(true);
    // The persisted active pane may have failed to restore — focus the first
    // cell rather than leaving a hidden pane active.
    if (!this._leafFor(this._activeId)) {
      const first = this._firstLeaf(tree);
      if (first) this.activate(first.paneId);
    }
    this._refreshCellChips();
    this._renderTabs(); // pick up the .shown markers
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
    // The title is a span (not a bare text node) so CSS can ellipsize it —
    // tabs cap their width (tighter on mobile) instead of growing unbounded
    // with a long workstream name.
    const titleNode = document.createElement("span");
    titleNode.className = "tab-title";
    titleNode.textContent = pane.title;
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
   *  in the Tab order) + aria-selected + the `.active` style hook.  `.shown`
   *  marks a pane that is VISIBLE in a split cell without being the focused
   *  one (aria-selected stays single — selection means focus). */
  _refreshTab(tab, pane) {
    const active = pane.id === this._activeId;
    tab.classList.toggle("active", active);
    tab.classList.toggle("shown", !active && !!this._leafFor(pane.id));
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
    // j is assigned in every branch below before tabs[j] is read (the no-match
    // case returns first), so no initial value is needed.
    let j;
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
   *  chrome + keyboard + positioning live in the shared openPopupMenu helper.
   *  Singleton menu — opening one closes any other.  Items are
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
    const handle = openPopupMenu(
      tab.querySelector(".tab-caret") || tab,
      items,
      {
        label: (pane.title || "Pane") + " actions",
        expandEl: tab,
        returnFocusEl: tab,
        ignoreEl: tab, // a click elsewhere on the tab is menu-adjacent, not "outside"
        onClose: () => {
          if (this._openMenu && this._openMenu.handle === handle)
            this._openMenu = null;
        },
      },
    );
    this._openMenu = { handle };
  }

  /** Tear down the open tab-action dropdown (if any) + its document listeners. */
  _closeTabMenu() {
    if (this._openMenu) this._openMenu.handle.close(); // onClose clears the ref
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
      const state = { order, active: this._activeId };
      // The split tree rides along (leaves reference pane ids, which are
      // deterministic type[:id] strings — rehydrate recomputes the same ones).
      if (this._layout) state.layout = this._serializeLayout(this._layout);
      sessionStorage.setItem(this.storageKey, JSON.stringify(state));
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
    // Layout LAST: every pane it references is open (or pruned), and the
    // active pane is settled — the restore just re-applies the cells.
    if (restored && state.layout) this._restoreLayout(state.layout);
    return restored;
  }
}
