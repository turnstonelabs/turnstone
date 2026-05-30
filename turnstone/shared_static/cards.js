/* Shared saved-list primitives — used by ui/static (Saved Workstreams) and
   console/static (Saved Coordinators).  Single source so the two surfaces
   don't drift on row shape, ARIA, keyboard handling, filter/sort, or the
   delete affordance:

     - renderSessionRow(sess, opts)  — one .dash-row from a column spec
     - SavedColumns                  — shared column descriptors
     - createSavedTable(opts)        — filter + sort + render, wrapping the
                                       multi-select delete controller
     - createSavedCardsController    — the delete-mode controller (below)

   Built with safe DOM APIs (createElement + textContent), never innerHTML,
   so user-supplied alias/title/name/skill fields never reach the DOM as
   HTML.  Depends on formatRelativeTime (from /shared/utils.js).
*/

/* ==========================================================================
   Saved-list TABLE primitives — the row builder (renderSessionRow) plus a
   shared filter / sort / render orchestrator (createSavedTable).  Both the
   server UI (Saved Workstreams) and the console (Saved Coordinators) build
   their saved list from these so the two surfaces can't drift.  The only
   per-surface input is the column spec (MSGS vs CHILDREN), the DOM refs,
   and the delete-request shape — everything generic lives here.
   ========================================================================== */

/* Map a 0..1 context-occupancy ratio to a coloured CTX cell using the
   active table's bands (base.css .dash-cell-ctx.ctx-*).  0 / unknown
   renders as a dim em-dash, not "0%": a saved row with no recorded usage
   (or a model whose window isn't in model_definitions) has no occupancy to
   report.  The value is a frozen snapshot from the last turn, not live. */
function _ctxCell(sess) {
  var ratio = typeof sess.context_ratio === "number" ? sess.context_ratio : 0;
  var span = document.createElement("span");
  span.className = "dash-cell-ctx";
  if (ratio <= 0) {
    span.classList.add("ctx-idle");
    span.textContent = "—";
    return span;
  }
  var level =
    ratio > 0.95
      ? "ctx-danger"
      : ratio > 0.8
        ? "ctx-high"
        : ratio > 0.5
          ? "ctx-mid"
          : "ctx-low";
  span.classList.add(level);
  span.textContent = Math.round(ratio * 100) + "%";
  return span;
}

/* NAME cell: ellipsised title + an optional skill chip when the workstream
   launched with a non-default skill (empty for "Use defaults"). */
function _nameCell(sess) {
  var wrap = document.createElement("div");
  wrap.className = "scell-name";
  var nm = document.createElement("span");
  nm.className = "scell-nm";
  nm.textContent =
    sess.alias || sess.title || sess.name || sess.ws_id.substring(0, 12);
  wrap.appendChild(nm);
  if (sess.launch_skill) {
    var chip = document.createElement("span");
    chip.className = "skill-chip";
    var g = document.createElement("span");
    g.className = "skill-chip-g";
    g.setAttribute("aria-hidden", "true");
    g.textContent = "◆";
    chip.appendChild(g);
    chip.appendChild(document.createTextNode(sess.launch_skill));
    wrap.appendChild(chip);
  }
  return wrap;
}

/* Column factory — shared descriptors.  Each: {key, label, width, align,
   cell(sess)->Node|string, sort(sess)->comparable}.  The only difference
   between the two surfaces is count("message_count","MSGS") vs
   count("child_count","CHILDREN"). */
var SavedColumns = {
  name: function () {
    return {
      key: "name",
      label: "NAME",
      width: "minmax(0,1fr)",
      cell: _nameCell,
      sort: function (s) {
        return (s.alias || s.title || s.name || s.ws_id).toLowerCase();
      },
    };
  },
  model: function () {
    return {
      key: "model",
      label: "MODEL",
      width: "150px",
      cls: "scell-model",
      hideBelow: true,
      cell: function (s) {
        return s.model_alias || "—";
      },
      sort: function (s) {
        return (s.model_alias || "").toLowerCase();
      },
    };
  },
  count: function (field, label, width) {
    return {
      key: field,
      label: label,
      width: width || "72px",
      align: "right",
      cell: function (s) {
        return String(s[field] != null ? s[field] : 0);
      },
      sort: function (s) {
        return s[field] != null ? s[field] : 0;
      },
    };
  },
  ctx: function () {
    return {
      key: "context_ratio",
      label: "CTX",
      width: "56px",
      align: "right",
      title: "Context window used as of last activity",
      cell: _ctxCell,
      sort: function (s) {
        return typeof s.context_ratio === "number" ? s.context_ratio : 0;
      },
    };
  },
  last: function () {
    return {
      key: "updated",
      label: "LAST",
      width: "62px",
      align: "right",
      cell: function (s) {
        return typeof formatRelativeTime === "function"
          ? formatRelativeTime(s.updated)
          : s.updated || "";
      },
      sort: function (s) {
        return s.updated || "";
      },
    };
  },
  id: function () {
    return {
      key: "ws_id",
      label: "ID",
      width: "76px",
      align: "right",
      cls: "scell-id",
      hideBelow: true,
      cell: function (s) {
        return s.ws_id.substring(0, 7);
      },
      sort: function (s) {
        return s.ws_id;
      },
    };
  },
};

/* Builds one saved-list .dash-row from a column spec.
   Saved rows reuse the dash-table chrome but opt OUT of the active table's
   live-state styling — only an `error` state is carried (for the red
   left-edge); idle/running/etc. are not, so a terminal, mostly-idle saved
   list isn't dimmed by base.css's `[data-state="idle"]` rule.  The grid
   template comes from the `--saved-grid` CSS var that createSavedTable sets
   once per render (not rebuilt per row). */
function renderSessionRow(sess, opts) {
  opts = opts || {};
  var columns = opts.columns || [];
  var row = document.createElement("div");
  row.className = "dash-row saved-row" + (opts.busy ? " is-busy" : "");
  row.dataset.wsId = sess.ws_id;
  if (sess.state === "error") row.dataset.state = "error";
  row.setAttribute("role", "button");
  row.setAttribute("tabindex", "0");
  row.setAttribute(
    "aria-label",
    typeof opts.ariaLabel === "function"
      ? opts.ariaLabel(sess)
      : "Resume: " + (sess.alias || sess.title || sess.name || sess.ws_id),
  );
  var main = document.createElement("div");
  main.className = "dash-row-main";
  columns.forEach(function (col) {
    var cell = document.createElement("div");
    cell.className = "scell" + (col.align === "right" ? " scell-r" : "");
    if (col.cls) cell.classList.add(col.cls);
    var content = col.cell(sess);
    if (content instanceof Node) cell.appendChild(content);
    else cell.textContent = content;
    main.appendChild(cell);
  });
  row.appendChild(main);
  var activate = function () {
    if (row.classList.contains("is-busy")) return;
    if (typeof opts.onActivate === "function") opts.onActivate(sess, row);
  };
  row.onclick = activate;
  row.onkeydown = function (e) {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      activate();
    }
  };
  return row;
}

/* Shared saved-list table: owns client-side filter + sort + render and
   wraps the existing multi-select delete controller.  Apps pass DOM refs +
   a column spec + the delete-request shape; the per-app delete-bar HTML
   keeps wiring its inline onclick thunks to `table.controller.*`.

   opts:
     headerEl, bodyEl  — the .dash-colheaders + .dash-table elements
     filterEl          — optional <input> for the client-side name filter
     footerEl          — optional element for the count line
     paginationEl      — optional .pagination container; the table fills it
                         with Prev / “page X / Y” / Next and hides it when
                         the list fits on one page or delete mode is active
     pageSize          — rows per page (default 20)
     columns           — array from SavedColumns
     noun              — "workstream" / "coordinator"
     onActivate        — sess => void (resume); gated by delete mode
     activateLabel     — optional sess => string (aria when not deleting)
     emptyText         — empty-state copy
     delete            — {idPrefix, buttonId, buildDeleteRequest, onClose}
   returns { setItems(items), render(), controller }. */
function createSavedTable(opts) {
  var state = {
    items: [],
    filter: "",
    sortKey: "updated",
    sortDir: -1,
    compact: false,
    page: 0,
  };
  /* Client-side page size — the list is fetched whole and sliced here, so
     the visible page (and therefore the delete controller's Select-All
     fan-out) is capped at this many rows. */
  var pageSize = opts.pageSize || 20;

  var controller = createSavedCardsController({
    idPrefix: opts.delete.idPrefix,
    buttonId: opts.delete.buttonId,
    noun: opts.noun,
    activateLabel:
      opts.activateLabel ||
      function (s) {
        return "Resume: " + (s.alias || s.title || s.name || s.ws_id);
      },
    buildDeleteRequest: opts.delete.buildDeleteRequest,
    render: function () {
      render();
    },
    onClose: opts.delete.onClose,
  });

  function matches(sess) {
    if (!state.filter) return true;
    var hay = (
      (sess.alias || "") +
      " " +
      (sess.title || "") +
      " " +
      (sess.name || "") +
      " " +
      sess.ws_id
    ).toLowerCase();
    return hay.indexOf(state.filter) !== -1;
  }

  function column(key) {
    for (var i = 0; i < opts.columns.length; i++) {
      if (opts.columns[i].key === key) return opts.columns[i];
    }
    return null;
  }

  /* On narrow viewports drop the lower-value columns (those flagged
     hideBelow — model, id) so NAME, the column this redesign exists to keep
     readable, never collapses to zero. */
  function visibleColumns() {
    return opts.columns.filter(function (c) {
      return !(state.compact && c.hideBelow);
    });
  }

  function gridTemplate(cols) {
    return cols
      .map(function (c) {
        return c.width;
      })
      .join(" ");
  }

  function sorted() {
    var col = column(state.sortKey) || column("updated");
    var out = state.items.filter(matches);
    if (col) {
      out.sort(function (a, b) {
        var av = col.sort(a);
        var bv = col.sort(b);
        if (av < bv) return -state.sortDir;
        if (av > bv) return state.sortDir;
        return 0;
      });
    }
    return out;
  }

  function renderHeaders(cols) {
    if (!opts.headerEl) return;
    opts.headerEl.style.gridTemplateColumns = gridTemplate(cols);
    /* Shift the headers in lockstep with the rows' checkbox gutter so the
       columns stay registered while multi-selecting. */
    opts.headerEl.classList.toggle("saved-cols-delete", controller.inMode());
    opts.headerEl.replaceChildren();
    cols.forEach(function (col) {
      var active = col.key === state.sortKey;
      var h = document.createElement("span");
      h.className =
        "scol" +
        (col.align === "right" ? " scell-r" : "") +
        (active ? " sorted" : "");
      h.setAttribute("role", "button");
      h.setAttribute("tabindex", "0");
      h.setAttribute("aria-label", "Sort by " + col.label);
      h.setAttribute(
        "aria-sort",
        active ? (state.sortDir < 0 ? "descending" : "ascending") : "none",
      );
      if (col.title) h.title = col.title;
      h.appendChild(document.createTextNode(col.label));
      /* Every sortable header carries a caret so the affordance is
         discoverable at rest — inactive ones faint, the active one
         directional. */
      var car = document.createElement("span");
      car.className = "caret" + (active ? "" : " caret-idle");
      car.setAttribute("aria-hidden", "true");
      car.textContent = active ? (state.sortDir < 0 ? "▼" : "▲") : "↕";
      h.appendChild(car);
      function doSort() {
        if (state.sortKey === col.key) {
          state.sortDir = -state.sortDir;
        } else {
          state.sortKey = col.key;
          /* text columns default A→Z, everything else newest/highest-first */
          state.sortDir = col.key === "name" || col.key === "model" ? 1 : -1;
        }
        /* Re-sorting reshuffles which rows land on which page; jump back to
           the first page so the user isn't stranded mid-list. */
        state.page = 0;
        render();
      }
      h.onclick = doSort;
      h.onkeydown = function (e) {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          doSort();
        }
      };
      opts.headerEl.appendChild(h);
    });
  }

  /* footer copy:
       filteredCount — rows after the name filter (the paginated population)
       start         — index of the first visible row within filteredCount
       shown         — rows actually painted this page
       pages         — total page count for filteredCount
     When the list spans more than one page the footer leads with the
     visible range so the Prev/Next control reads as intentional paging, not
     a silent truncation. */
  function renderFooter(filteredCount, start, shown, pages) {
    if (!opts.footerEl) return;
    var total = state.items.length;
    var noun = opts.noun + (total === 1 ? "" : "s");
    if (pages > 1 && filteredCount > 0) {
      var rangeNoun = opts.noun + (filteredCount === 1 ? "" : "s");
      var range =
        "Showing " +
        (start + 1) +
        "–" +
        (start + shown) +
        " of " +
        filteredCount +
        " " +
        rangeNoun;
      opts.footerEl.textContent = state.filter
        ? range + " matching “" + state.filter + "”"
        : range;
      return;
    }
    /* Single page: the empty/filtered body message owns the "no match" copy,
       so the footer stays a plain total — the two don't say the same thing
       twice. */
    if (state.filter && filteredCount > 0 && filteredCount !== total) {
      opts.footerEl.textContent =
        filteredCount +
        " of " +
        total +
        " " +
        noun +
        " match “" +
        state.filter +
        "”";
    } else {
      opts.footerEl.textContent = total + " " + noun;
    }
  }

  /* Fill the optional .pagination container with Prev / “page X / Y” / Next.
     Hidden when the list fits on one page or while multi-selecting: paging
     in delete mode would orphan the user's checkbox selections, which live
     on the visible page only.  Buttons are rebuilt each render so their
     onclick closures always page over the current filtered/sorted list.
     The page label is intentionally NOT a live region — the footer (already
     aria-live) announces the resulting "Showing X–Y of Z" range. */
  function renderPagination(pages) {
    if (!opts.paginationEl) return;
    var pag = opts.paginationEl;
    if (pages <= 1 || controller.inMode()) {
      pag.style.display = "none";
      pag.replaceChildren();
      /* Don't leave an empty navigation landmark (or a stale label) behind
         when the pager isn't shown — the visible branch re-applies both. */
      pag.removeAttribute("role");
      pag.removeAttribute("aria-label");
      return;
    }
    pag.style.display = "";
    pag.setAttribute("role", "navigation");
    /* state.page is 0-based here; the legacy console pager
       (console/static/app.js renderPagination) is 1-based.  The rendered
       "X / Y" is identical — only the internal index differs — so don't
       assume a shared base if the two are ever unified. */
    var prev = document.createElement("button");
    prev.type = "button";
    prev.setAttribute("aria-label", "Previous page");
    prev.textContent = "◄ Prev";
    prev.disabled = state.page <= 0;
    prev.onclick = function () {
      if (state.page > 0) {
        state.page--;
        render();
      }
    };
    var label = document.createElement("span");
    label.textContent = state.page + 1 + " / " + pages;
    var next = document.createElement("button");
    next.type = "button";
    next.setAttribute("aria-label", "Next page");
    next.textContent = "Next ►";
    next.disabled = state.page >= pages - 1;
    next.onclick = function () {
      if (state.page < pages - 1) {
        state.page++;
        render();
      }
    };
    pag.replaceChildren(prev, label, next);
    pag.setAttribute(
      "aria-label",
      "Saved " + opts.noun + "s — page " + (state.page + 1) + " of " + pages,
    );
  }

  function render() {
    var cols = visibleColumns();
    var all = sorted();
    var filteredCount = all.length;
    /* Clamp the page after a delete / filter / upstream churn shrinks the
       list, then slice to it.  The delete controller only ever sees the
       visible page, so its Select-All / count can't reach off-page rows. */
    var pages = Math.max(1, Math.ceil(filteredCount / pageSize));
    if (state.page > pages - 1) state.page = pages - 1;
    if (state.page < 0) state.page = 0;
    var start = state.page * pageSize;
    var rows = all.slice(start, start + pageSize);
    controller.setItems(rows);
    /* One grid write per render: rows read it from the inherited CSS var. */
    if (opts.bodyEl) {
      opts.bodyEl.style.setProperty("--saved-grid", gridTemplate(cols));
      opts.bodyEl.replaceChildren();
    }
    if (!filteredCount) {
      /* Empty state owns the space — hide the column headers so it doesn't
         read as a broken grid. */
      if (opts.headerEl) opts.headerEl.style.display = "none";
      var empty = document.createElement("div");
      empty.className = "dashboard-empty";
      empty.textContent = state.filter
        ? "No " + opts.noun + "s match “" + state.filter + "”"
        : opts.emptyText || "No saved items";
      if (opts.bodyEl) opts.bodyEl.appendChild(empty);
    } else {
      if (opts.headerEl) opts.headerEl.style.display = "";
      renderHeaders(cols);
      rows.forEach(function (sess) {
        var row = renderSessionRow(sess, {
          columns: cols,
          ariaLabel: controller.ariaLabel,
          onActivate: function (s, el) {
            if (controller.blockActivate()) return;
            if (typeof opts.onActivate === "function") opts.onActivate(s, el);
          },
        });
        controller.decorateCard(row, sess);
        opts.bodyEl.appendChild(row);
      });
    }
    if (controller.inMode()) controller.refreshBar();
    renderPagination(pages);
    renderFooter(filteredCount, start, rows.length, pages);
  }

  /* Debounce only the filter keystrokes; setItems / sort / delete render
     immediately. */
  var filterTimer = null;
  if (opts.filterEl) {
    opts.filterEl.addEventListener("input", function () {
      if (filterTimer) clearTimeout(filterTimer);
      filterTimer = setTimeout(function () {
        state.filter = opts.filterEl.value.trim().toLowerCase();
        /* A narrower filter usually means fewer pages; restart at page 1 so
           the user lands on matches rather than an out-of-range page. */
        state.page = 0;
        render();
      }, 120);
    });
  }

  /* Saved table owns its responsive layout: below the breakpoint the
     hideBelow columns drop and NAME reclaims the width. */
  if (typeof window !== "undefined" && window.matchMedia) {
    var mq = window.matchMedia("(max-width: 760px)");
    state.compact = mq.matches;
    var onMq = function (e) {
      state.compact = e.matches;
      render();
    };
    if (mq.addEventListener) mq.addEventListener("change", onMq);
    else if (mq.addListener) mq.addListener(onMq);
  }

  return {
    setItems: function (items) {
      state.items = items || [];
      render();
    },
    render: render,
    controller: controller,
  };
}

/* createSavedCardsController — shared multi-select-delete behaviour for
   the dashboard / home "saved cards" surfaces.  ui/static (Saved
   Workstreams) and console/static (Saved Coordinators) both instantiate
   one of these; the controller owns:

     - delete-mode state (active flag + selected ws_id set)
     - card decoration (checkbox + key/click overrides)
     - the bottom toolbar wiring (count, Select All, Delete Selected)
     - the confirmation modal (focus trap, batch fan-out, results view)

   It does NOT own how cards get fetched or rendered — the caller's
   render() is invoked when the controller needs the list redrawn (mode
   transitions, Select-All toggles).

   Required opts:
     idPrefix          — DOM-id prefix shared by the toolbar + modal
                         (e.g. "ws-delete" / "coord-delete").  The DOM
                         must already contain `${idPrefix}-bar`,
                         `${idPrefix}-bar-count`, `${idPrefix}-bar-delete`,
                         `${idPrefix}-bar-select-all`, `${idPrefix}-overlay`,
                         `${idPrefix}-box`, `${idPrefix}-error`,
                         `${idPrefix}-count`, `${idPrefix}-list`,
                         `${idPrefix}-confirm-btn`, `${idPrefix}-cancel-btn`.
     buttonId          — id of the section's start/cancel toggle button.
     noun              — singular display word for the item kind, e.g.
                         "workstream" / "coordinator".  Used in toast +
                         modal copy.
     activateLabel     — sess => string; aria-label for the card when NOT
                         in delete mode (e.g. "Resume: foo").
     buildDeleteRequest — wsId => { url, options }; what authFetch should
                         send to delete one item.
     render            — () => void; redraw the visible cards.  Called by
                         the controller on mode start/cancel and Select-
                         All toggle.  Caller is responsible for calling
                         setItems(items) + decorateCard() inside it.
     onClose           — optional () => void; called once after the user
                         closes the post-delete results modal.  Typical
                         use: re-fetch the saved list.
*/
function createSavedCardsController(opts) {
  var state = { mode: false, selected: {}, items: [] };
  var batchTrap = null;
  /* Element that owned focus when the modal opened — restored in
     closeModal() so keyboard users land back on the toggle button (or
     wherever they came from) instead of <body>.  WCAG 2.4.3. */
  var prevFocus = null;

  function $(id) {
    return document.getElementById(opts.idPrefix + "-" + id);
  }

  /* Replace the toggle button's content with a glyph + label, keeping
     the glyph in an aria-hidden span so screen readers only read the
     label.  Built from DOM nodes (no innerHTML) — same shape as the
     section-header markup the JS replaces. */
  function setIconButton(btn, glyph, label) {
    btn.replaceChildren();
    var span = document.createElement("span");
    span.setAttribute("aria-hidden", "true");
    span.textContent = glyph;
    btn.appendChild(span);
    btn.appendChild(document.createTextNode(" " + label));
  }

  function setItems(items) {
    state.items = items;
    /* Drop any selections whose ws_id is no longer on the visible page —
       SSE-driven re-renders or pagination jumps shouldn't leave ghost
       entries inflating the count and 404-ing on confirm. */
    if (state.mode) {
      var byId = {};
      items.forEach(function (s) {
        byId[s.ws_id] = true;
      });
      Object.keys(state.selected).forEach(function (id) {
        if (!byId[id]) delete state.selected[id];
      });
    }
  }

  function inMode() {
    return state.mode;
  }

  function blockActivate() {
    return state.mode;
  }

  function isSelected(wsId) {
    return !!state.selected[wsId];
  }

  function ariaLabel(sess) {
    var label = sess.alias || sess.title || sess.name || sess.ws_id;
    if (state.mode) return "Select " + opts.noun + ": " + label;
    return typeof opts.activateLabel === "function"
      ? opts.activateLabel(sess)
      : "Activate: " + label;
  }

  /* Decorate an already-rendered saved row (.dash-row) with the checkbox +
     event overrides used in delete mode.  Idempotent guard: only acts
     when the controller is active. */
  function decorateCard(card, sess) {
    if (!state.mode) return;
    card.classList.add("ws-delete-mode");
    card.removeAttribute("role");
    var chk = document.createElement("input");
    chk.type = "checkbox";
    chk.className = "ws-card-check";
    chk.checked = !!state.selected[sess.ws_id];
    var label = sess.alias || sess.title || sess.name || sess.ws_id;
    chk.setAttribute("aria-label", "Select " + label + " for deletion");
    chk.onclick = function (e) {
      e.stopPropagation();
      if (chk.checked) state.selected[sess.ws_id] = true;
      else delete state.selected[sess.ws_id];
      card.classList.toggle("ws-selected", chk.checked);
      refreshBar();
    };
    card.insertBefore(chk, card.firstChild);
    card.onclick = function (e) {
      if (e.target === chk) return;
      chk.checked = !chk.checked;
      chk.onclick(e);
    };
    card.onkeydown = function (e) {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        chk.checked = !chk.checked;
        chk.onclick(e);
      }
    };
    if (state.selected[sess.ws_id]) card.classList.add("ws-selected");
  }

  function refreshBar() {
    var count = Object.keys(state.selected).length;
    var label = $("bar-count");
    if (label) label.textContent = count + " selected";
    var delBtn = $("bar-delete");
    if (delBtn) delBtn.disabled = count === 0;
    var selBtn = $("bar-select-all");
    if (selBtn) {
      var allSelected = count === state.items.length && state.items.length > 0;
      selBtn.textContent = allSelected ? "Deselect All" : "Select All";
    }
  }

  function start() {
    if (!state.items.length) {
      if (typeof showToast === "function") {
        showToast("No saved " + opts.noun + "s to delete");
      }
      return;
    }
    state.mode = true;
    state.selected = {};
    opts.render();
    var btn = document.getElementById(opts.buttonId);
    if (btn) {
      setIconButton(btn, "✕", "Cancel");
      btn.onclick = cancel;
    }
    var bar = $("bar");
    if (bar) bar.classList.add("visible");
    refreshBar();
  }

  function cancel() {
    state.mode = false;
    state.selected = {};
    opts.render();
    var btn = document.getElementById(opts.buttonId);
    if (btn) {
      setIconButton(btn, "\u{1f5d1}", "Delete");
      btn.onclick = start;
    }
    var bar = $("bar");
    if (bar) bar.classList.remove("visible");
  }

  function toggleAll() {
    var allSelected =
      Object.keys(state.selected).length === state.items.length &&
      state.items.length > 0;
    if (allSelected) {
      state.selected = {};
    } else {
      state.items.forEach(function (s) {
        state.selected[s.ws_id] = true;
      });
    }
    opts.render();
    refreshBar();
  }

  function _byId() {
    /* Single-pass index over the visible items so the modal + fan-out
       paths don't repeat O(N) `find` calls per selection. */
    var map = {};
    state.items.forEach(function (s) {
      map[s.ws_id] = s;
    });
    return map;
  }

  function confirmSelection() {
    var selected = Object.keys(state.selected);
    if (!selected.length) {
      if (typeof showToast === "function") {
        showToast("No " + opts.noun + "s selected");
      }
      return;
    }
    var byId = _byId();
    var overlay = $("overlay");
    var countEl = $("count");
    var listEl = $("list");
    var errorEl = $("error");
    if (errorEl) errorEl.textContent = "";
    if (countEl) {
      countEl.textContent =
        selected.length + " " + opts.noun + "(s) will be permanently deleted:";
    }
    if (listEl) {
      listEl.replaceChildren();
      selected.forEach(function (wsId) {
        var item = byId[wsId];
        var name = item ? item.alias || item.title || item.name || wsId : wsId;
        var div = document.createElement("div");
        div.className = "ws-delete-item";
        div.textContent = name;
        listEl.appendChild(div);
      });
    }
    var delBtn = $("confirm-btn");
    if (delBtn) {
      delBtn.textContent = "Delete";
      delBtn.disabled = false;
      delBtn.classList.remove("ws-delete-close");
      delBtn.classList.add("ws-delete-confirm");
      delBtn.onclick = confirm;
    }
    var cancelBtn = $("cancel-btn");
    if (cancelBtn) cancelBtn.disabled = false;
    if (overlay) overlay.style.display = "flex";

    if (batchTrap) document.removeEventListener("keydown", batchTrap);
    batchTrap = function (e) {
      if (e.key === "Escape") {
        e.preventDefault();
        closeModal();
        return;
      }
      if (e.key === "Tab") {
        var box = $("box");
        if (!box) return;
        var focusable = box.querySelectorAll("button:not(:disabled)");
        if (!focusable.length) return;
        var first = focusable[0];
        var last = focusable[focusable.length - 1];
        if (e.shiftKey && document.activeElement === first) {
          e.preventDefault();
          last.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault();
          first.focus();
        }
      }
    };
    document.addEventListener("keydown", batchTrap);
    /* Snapshot the pre-modal focus owner so closeModal() can return to
       it.  Captured before we move focus into the dialog so the
       restore-target is the caller, not the dialog itself. */
    prevFocus = document.activeElement;
    if (cancelBtn) cancelBtn.focus();
  }

  function closeModal() {
    var overlay = $("overlay");
    if (overlay) overlay.style.display = "none";
    if (batchTrap) {
      document.removeEventListener("keydown", batchTrap);
      batchTrap = null;
    }
    /* Pick the most useful focus target:
         1. prevFocus (where the user came from), if it's still in the
            DOM and visible.  Esc / Cancel paths land here — the bar is
            still on screen, so focus returns to "Delete Selected".
         2. The section toggle button — always present, semantic exit
            point for the flow.  Used when prevFocus has been hidden by
            cancel() (post-delete Close path: cancel() ran first and
            put `.ws-delete-bar` at display:none, so the bar's button
            is no longer focusable). */
    var target = prevFocus;
    if (!target || target.offsetParent === null) {
      target = document.getElementById(opts.buttonId);
    }
    if (target && typeof target.focus === "function") {
      try {
        target.focus();
      } catch (_) {
        /* node detached between open and close — give up silently */
      }
    }
    prevFocus = null;
  }

  function confirm() {
    var selected = Object.keys(state.selected);
    if (!selected.length) return;
    var byId = _byId();
    var errorEl = $("error");
    var listEl = $("list");
    var countEl = $("count");
    var delBtn = $("confirm-btn");
    var cancelBtn = $("cancel-btn");
    if (errorEl) errorEl.textContent = "";
    if (delBtn) {
      delBtn.disabled = true;
      delBtn.textContent = "Deleting...";
    }
    if (cancelBtn) cancelBtn.disabled = true;

    var results = [];
    var promises = selected.map(function (wsId) {
      var shortId = wsId.substring(0, 8);
      var item = byId[wsId];
      var name = item ? item.alias || item.title || item.name || wsId : wsId;
      var req = opts.buildDeleteRequest(wsId);
      return authFetch(req.url, req.options)
        .then(function (r) {
          var status = r.status;
          var contentType = r.headers.get("content-type") || "";
          if (r.ok) {
            results.push({ name: name, shortId: shortId, ok: true });
            return;
          }
          return r.text().then(function (body) {
            var errMsg = shortId + ": HTTP " + status;
            if (contentType.includes("json")) {
              try {
                var j = JSON.parse(body);
                if (j.error) errMsg = shortId + ": " + j.error;
              } catch (_) {
                /* fall through */
              }
            } else if (body) {
              errMsg = shortId + ": " + body.substring(0, 200);
            }
            results.push({
              name: name,
              shortId: shortId,
              ok: false,
              error: errMsg,
            });
          });
        })
        .catch(function (err) {
          results.push({
            name: name,
            shortId: shortId,
            ok: false,
            error: shortId + ": " + err.message,
          });
        });
    });

    Promise.all(promises).then(function () {
      if (listEl) {
        listEl.replaceChildren();
        results.forEach(function (r) {
          var div = document.createElement("div");
          div.className = "ws-delete-item" + (r.ok ? "" : " ws-delete-error");
          div.textContent =
            (r.ok ? "✓ " : "✗ ") + r.name + (r.error ? " — " + r.error : "");
          listEl.appendChild(div);
        });
      }
      var okCount = results.filter(function (r) {
        return r.ok;
      }).length;
      var failCount = results.filter(function (r) {
        return !r.ok;
      }).length;
      if (countEl) {
        countEl.textContent = okCount + " deleted, " + failCount + " failed";
      }
      if (delBtn) {
        delBtn.disabled = false;
        delBtn.textContent = "Close";
        /* Swap modifier classes so styling is intent-driven instead of
           cascade-positional: the Close button picks up the default
           ".ws-delete-modal-buttons button" rule once .ws-delete-confirm
           is removed. */
        delBtn.classList.remove("ws-delete-confirm");
        delBtn.classList.add("ws-delete-close");
        delBtn.onclick = function () {
          /* Order matters: cancel() reshapes the toggle button via
             setIconButton(), which preserves the element identity but
             swaps its subtree.  closeModal() then focuses prevFocus —
             which IS that toggle button — landing on a freshly rebuilt
             "Delete" affordance instead of <body>. */
          cancel();
          closeModal();
          if (typeof opts.onClose === "function") opts.onClose();
        };
      }
      if (cancelBtn) cancelBtn.disabled = false;
    });
  }

  return {
    setItems: setItems,
    inMode: inMode,
    blockActivate: blockActivate,
    isSelected: isSelected,
    ariaLabel: ariaLabel,
    decorateCard: decorateCard,
    refreshBar: refreshBar,
    start: start,
    cancel: cancel,
    toggleAll: toggleAll,
    confirmSelection: confirmSelection,
    closeModal: closeModal,
    confirm: confirm,
  };
}
