/* Shared saved-list primitives — used by ui/static (Saved Workstreams) and
   console/static (Saved Coordinators).  Single source so the two surfaces
   don't drift on row shape, ARIA, keyboard handling, filter/sort, or the
   delete affordance:

     - renderSessionRow(sess, opts)  — one .dash-row from a column spec
     - SavedColumns                  — shared column descriptors
     - createSavedTable(opts)        — fetch + page + render, wrapping the
                                       multi-select delete controller
     - createSavedCardsController    — the delete-mode controller (below)

   ES module (imports utils/toast/auth; window bridge below for the
   still-classic app.js consumers).

   Built with safe DOM APIs (createElement + textContent), never innerHTML,
   so user-supplied alias/title/name/skill fields never reach the DOM as
   HTML.  Depends on formatRelativeTime and cssEscape (from /shared/utils.js).
*/

import { cssEscape, formatRelativeTime } from "./utils.js";
import { showToast } from "./toast.js";
import { authFetch } from "./auth.js";

/* ==========================================================================
   Saved-list TABLE primitives — the row builder (renderSessionRow) plus a
   shared fetch / page / render orchestrator (createSavedTable).  Both the
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
/* Resolve a project_id to its display name via the shared projects data
   layer (window bridge — cards.js also loads in the classic bundles).
   Unknown / inaccessible ids render empty so callers can show "—". */
function _projectName(projectId) {
  if (!projectId) return "";
  var tp = window.TurnstoneProjects;
  if (!tp || typeof tp.projectName !== "function") return "";
  return tp.projectName(projectId) || "";
}

function _personaLabel(name) {
  if (!name) return "";
  var tp = window.TurnstonePersonas;
  // The raw slug still labels rows when the data layer hasn't loaded —
  // an archived persona keeps labelling the workstreams stamped with it.
  if (!tp || typeof tp.personaLabel !== "function") return name;
  return tp.personaLabel(name) || name;
}

/* The skill chip is visual, and its title reaches only a pointer, so a
   row's accessible name carries the skill too. */
function _skillNote(sess) {
  return sess.launch_skill ? " (skill: " + sess.launch_skill + ")" : "";
}

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
    /* A long name can cut the chip's label short; hovering names the skill. */
    chip.title = sess.launch_skill;
    var g = document.createElement("span");
    g.className = "skill-chip-g";
    g.setAttribute("aria-hidden", "true");
    g.textContent = "◆";
    chip.appendChild(g);
    var label = document.createElement("span");
    label.className = "skill-chip-label";
    label.textContent = sess.launch_skill;
    chip.appendChild(label);
    wrap.appendChild(chip);
  }
  return wrap;
}

/* Column factory — shared descriptors.  Each: {key, label, width, align,
   cell(sess)->Node|string}.  `key` doubles as the column's sort key on
   GET /v1/api/workstreams/saved (the server sorts every page), and `order`
   "asc" makes a column's first click sort A to Z (the rest start highest or
   newest first).  The only difference between the two surfaces is
   count("message_count","MSGS") vs count("child_count","CHILDREN").  A
   column with a `drop` rank leaves when the table is too narrow for NAME,
   lowest rank first, and one with `nameMin` leaves whenever NAME would be
   narrower than that (see createSavedTable's visibleColumns); NAME, CTX
   and LAST never do. */
export var SavedColumns = {
  name: function () {
    return {
      key: "name",
      label: "NAME",
      width: "minmax(0,1fr)",
      order: "asc",
      cell: _nameCell,
    };
  },
  model: function () {
    return {
      key: "model",
      label: "MODEL",
      width: "150px",
      cls: "scell-model",
      order: "asc",
      drop: 5,
      cell: function (s) {
        return s.model_alias || "—";
      },
    };
  },
  project: function () {
    return {
      key: "project",
      label: "PROJECT",
      width: "120px",
      order: "asc",
      drop: 4,
      cell: function (s) {
        return (
          _projectName(s.project_id) || (s.project_id ? "Unavailable" : "—")
        );
      },
    };
  },
  persona: function () {
    return {
      key: "persona",
      label: "PERSONA",
      width: "110px",
      order: "asc",
      drop: 3,
      cell: function (s) {
        return _personaLabel(s.persona) || "—";
      },
    };
  },
  count: function (field, label, width) {
    return {
      key: field,
      label: label,
      width: width || "72px",
      align: "right",
      drop: 2,
      cell: function (s) {
        return String(s[field] != null ? s[field] : 0);
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
    };
  },
  id: function () {
    return {
      key: "ws_id",
      label: "ID",
      width: "76px",
      align: "right",
      cls: "scell-id",
      /* Few people use the id, so it goes first and early. */
      drop: 1,
      nameMin: 280,
      cell: function (s) {
        return s.ws_id.substring(0, 7);
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
export function renderSessionRow(sess, opts) {
  opts = opts || {};
  var columns = opts.columns || [];
  var row = document.createElement("div");
  row.className = "dash-row saved-row" + (opts.busy ? " is-busy" : "");
  row.dataset.wsId = sess.ws_id;
  if (sess.state === "error") row.dataset.state = "error";
  var canActivate = opts.canActivate !== false;
  row.setAttribute("role", canActivate ? "button" : "group");
  if (canActivate) row.setAttribute("tabindex", "0");
  row.setAttribute(
    "aria-label",
    (!canActivate
      ? sess.alias || sess.title || sess.name || sess.ws_id
      : typeof opts.ariaLabel === "function"
        ? opts.ariaLabel(sess)
        : "Resume: " + (sess.alias || sess.title || sess.name || sess.ws_id)) +
      _skillNote(sess),
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
    if (!canActivate) return;
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

/* URL for one page of GET /v1/api/workstreams/saved — the query the table
   asks for.  Sort and order always go along, so the server's defaults never
   have to match the table's. */
export function savedListUrl(query) {
  var params = new URLSearchParams();
  params.set("limit", String(query.limit));
  if (query.offset) params.set("offset", String(query.offset));
  if (query.q) params.set("q", query.q);
  params.set("sort", query.sort);
  params.set("order", query.order);
  return "/v1/api/workstreams/saved?" + params.toString();
}

/* Rows per saved-list page. */
var PAGE_SIZE = 20;

/* Shared saved-list table: the server pages, searches and sorts
   (GET /v1/api/workstreams/saved), so the table holds only the query — page,
   filter text, sort column — and fetches the page it shows.  It wraps the
   existing multi-select delete controller.  Apps pass DOM refs + a column
   spec + the delete-request shape; the per-app delete-bar HTML keeps wiring
   its inline onclick thunks to `table.controller.*`.

   One request is out at a time.  load() is the app's refresh (at boot,
   after events, from Retry): a call while a request is out asks once more
   after it lands, however many calls came.  Paging, sorting and searching
   ask for their own query; a click made while a request is out waits for
   it, and an answer for a query the user has since left is dropped for one
   that asks for the newest.  reset() drops every answer still out.  While
   rows are selected for deletion nothing is fetched: the table asks again
   once delete mode ends, however it ends.  Pages, sorts and searches mark
   the rows pending until their answer arrives.

   opts:
     headerEl, bodyEl  — the .dash-colheaders + .dash-table elements
     filterEl          — optional <input> for the search box
     footerEl          — optional element for the count line
     paginationEl      — optional .pagination container; the table fills it
                         with Prev / “page X / Y” / Next and hides it when
                         the list fits on one page or delete mode is active
     errorEl, errorTextEl — optional load-error block and its text; while it
                         shows, the rows and the footer hide.  Its Retry
                         calls the app's loader.
     columns           — array from SavedColumns
     noun              — "workstream" / "session"
     onLoad            — optional ({total, filter, failed}) => void, once a
                         page lands or fails to load
     onActivate        — sess => void (resume); gated by delete mode
     canActivate       — optional sess => boolean (default true)
     canDelete         — optional sess => boolean (default true)
     canDeleteAny      — () => boolean: whether the viewer may delete saved
                         items at all.  The Delete button follows it and the
                         list, not the rows on the current page.
     activateLabel     — optional sess => string (aria when not deleting)
     emptyText         — empty-state copy
     delete            — {idPrefix, buttonId, buildDeleteRequest}

   returns { load(ready), render(), reset(), controller }.  load()'s
   optional `ready` promise holds the first page until it settles (the node
   waits for the project and persona names). */
export function createSavedTable(opts) {
  var state = {
    rows: [],
    total: 0,
    filter: "",
    sortKey: "updated",
    sortDir: -1,
    /* The table's measured width in px; 0 until it has one. */
    width: 0,
    page: 0,
    /* The page and filter that produced `rows`.  The pager and footer
       describe these until the next page arrives, not the newer query a
       click has already asked for. */
    shownPage: 0,
    shownFilter: "",
    /* Whether a page, or a failure, has landed since the last reset.  Until
       then the table says it is loading. */
    loaded: false,
  };

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
    canDelete: opts.canDelete,
    render: function () {
      render();
    },
    /* The deleted rows leave the page at once, and the list is fetched again
       as delete mode ends, which the controller does next (render() replays
       the fetch). */
    onDeleted: function (ids) {
      var gone = {};
      ids.forEach(function (id) {
        gone[id] = true;
      });
      var kept = state.rows.filter(function (s) {
        return !gone[s.ws_id];
      });
      state.total = Math.max(
        0,
        state.total - (state.rows.length - kept.length),
      );
      state.rows = kept;
      /* Deleting a whole last page leaves it past the end: the fetch asks
         for the last page that still exists instead, and the pager never
         names a page past it. */
      var last = Math.max(0, Math.ceil(state.total / PAGE_SIZE) - 1);
      if (!kept.length && state.page > last) state.page = last;
      if (state.shownPage > last) state.shownPage = last;
      deferredFetch = true;
    },
  });

  function query() {
    return {
      limit: PAGE_SIZE,
      offset: state.page * PAGE_SIZE,
      q: state.filter,
      sort: state.sortKey,
      order: state.sortDir < 0 ? "desc" : "asc",
    };
  }

  function queryKey(q) {
    return JSON.stringify([q.limit, q.offset, q.q, q.sort, q.order]);
  }

  function noMatchText(filter) {
    return "No " + opts.noun + "s match “" + filter + "”";
  }

  /* While rows are selected for deletion, searching or sorting would fetch
     another page and drop the selections; the search box and the column
     headers say so. */
  var pauseHint = "Search and sort pause while you select rows to delete";

  /* Set while a fetch waits for delete mode to end: a new page would drop
     the selections on this one.  render() asks again once the mode ends. */
  var deferredFetch = false;

  /* The request out: its query key (null when none), whether the list may
     have changed since it was sent, and the reset() generation it belongs
     to. */
  var inFlight = null;
  var changed = false;
  var generation = 0;

  /* Ask for the current query, or note that the request out should be
     followed by another.  `fresh` (load()) means the list itself may have
     changed, so even the query already out is asked for again once it
     lands; a page, sort or search only needs its own query. */
  function fetchPage(fresh, ready) {
    if (controller.inMode()) {
      deferredFetch = true;
      return;
    }
    if (inFlight !== null) {
      if (fresh) changed = true;
      return;
    }
    var q = query();
    var key = queryKey(q);
    var mine = generation;
    inFlight = key;
    changed = false;
    setRetrying(true);
    var page = authFetch(savedListUrl(q)).then(function (r) {
      if (!r.ok) {
        throw new Error(
          "Could not load saved " + opts.noun + "s (" + r.status + ").",
        );
      }
      return r.json();
    });
    Promise.all([page, ready]).then(
      function (res) {
        if (mine === generation) landed(key, res[0], null);
      },
      function (error) {
        if (mine === generation) landed(key, null, error);
      },
    );
  }

  function landed(key, data, error) {
    inFlight = null;
    setRetrying(false);
    /* Rows being selected stay as they are; the table asks again once
       delete mode ends. */
    if (controller.inMode()) {
      deferredFetch = true;
      return;
    }
    /* The user paged, sorted or searched while it was out. */
    if (key !== queryKey(query())) {
      fetchPage(false);
      return;
    }
    if (error) {
      /* Drop the rows as well, so nothing re-rendered behind the error can
         pass for the page that failed to load. */
      state.rows = [];
      state.total = 0;
      state.loaded = true;
      setError(
        (error && error.message) || "Could not load saved " + opts.noun + "s.",
      );
      setPending(false);
      notifyLoad(true);
      render();
    } else {
      var rows = data.workstreams || [];
      var total = data.total > 0 ? data.total : 0;
      var lastPage = Math.max(0, Math.ceil(total / PAGE_SIZE) - 1);
      /* A page past the end — rows deleted, or other sessions loaded since —
         steps back to the last page that exists and asks for it.  Only ever
         back, so a page the count says exists but that came back empty is
         shown empty instead of being asked for again. */
      if (!rows.length && state.page > lastPage) {
        state.page = lastPage;
        fetchPage(false);
        return;
      }
      setError("");
      state.rows = rows;
      /* Never report fewer rows than this page already holds. */
      state.total = Math.max(total, state.page * PAGE_SIZE + rows.length);
      state.shownPage = state.page;
      state.shownFilter = state.filter;
      state.loaded = true;
      setPending(false);
      notifyLoad(false);
      render();
    }
    if (changed) fetchPage(true);
  }

  /* Tell the app before the page is drawn: a section it shows for the page
     is laid out by the time render() measures the table, so the page lands
     in the columns that fit. */
  function notifyLoad(failed) {
    if (typeof opts.onLoad === "function") {
      opts.onLoad({
        total: state.total,
        filter: state.shownFilter,
        failed: failed,
      });
    }
  }

  /* While a request is out behind the load error, its Retry says so. */
  function setRetrying(on) {
    if (!opts.errorEl || (on && opts.errorEl.hidden)) return;
    var retry = opts.errorEl.querySelector("button");
    if (retry) retry.textContent = on ? "Retrying…" : "Retry";
    if (on) opts.errorEl.setAttribute("aria-busy", "true");
    else opts.errorEl.removeAttribute("aria-busy");
  }

  /* Show or clear the load error.  The rows and footer hide while it shows;
     the table, emptied first, hides its own headers and pager. */
  function setError(message) {
    if (opts.errorEl) opts.errorEl.hidden = !message;
    if (opts.errorTextEl) opts.errorTextEl.textContent = message;
    [opts.bodyEl, opts.footerEl].forEach(function (el) {
      if (el) el.style.display = message ? "none" : "";
    });
  }

  /* The user asked for another page, sort or search. */
  function requestPage() {
    if (controller.inMode()) {
      deferredFetch = true;
      return;
    }
    setPending(true);
    fetchPage(false);
  }

  /* The rows on screen are about to give way to a page the user asked for.
     The CSS dims them after a short delay, so a fast answer never flickers,
     and aria-busy tells assistive technology to wait for it. */
  function setPending(on) {
    if (!opts.bodyEl) return;
    if (on && !opts.bodyEl.classList.contains("is-pending")) {
      /* Rows drawn in this same task have no style yet; working it out now
         gives them the delayed dimming too, instead of an instant one. */
      void opts.bodyEl.offsetWidth;
    }
    opts.bodyEl.classList.toggle("is-pending", on);
    if (on) opts.bodyEl.setAttribute("aria-busy", "true");
    else opts.bodyEl.removeAttribute("aria-busy");
  }

  /* Re-rendering replaces the headers, pager buttons and rows, which would
     drop keyboard focus to the page.  focusedKey() names the focused control
     inside `container` by its data-focus-key, which its replacement carries
     too; refocus() then focuses the first of `keys` that is there and
     enabled. */
  function focusedKey(container) {
    var key = null;
    for (var el = document.activeElement; el; el = el.parentNode) {
      if (el === container) return key;
      if (!key && el.dataset && el.dataset.focusKey) key = el.dataset.focusKey;
    }
    return null;
  }

  function refocus(container, keys) {
    for (var i = 0; i < keys.length; i++) {
      if (!keys[i]) continue;
      var el = container.querySelector(
        '[data-focus-key="' + cssEscape(keys[i]) + '"]',
      );
      if (el && !el.disabled) {
        el.focus({ preventScroll: true });
        return;
      }
    }
  }

  /* When the table is too narrow, drop the lower-value columns one at a time,
     lowest `drop` rank first (id, the counts, persona, project, model, the
     console's KIND), until NAME, the column this redesign exists to keep
     readable, has at least NAME_MIN: room for a long name beside a skill
     chip.  A column with `nameMin` (ID) leaves whenever NAME would get less
     than that.  It goes by the table's own width, not the window's: a side
     rail or a split pane narrows it as much as a phone does. */
  var NAME_MIN = 200;
  /* Besides its columns a row takes 16px of padding on each side and a 3px
     left border, and delete mode's checkbox gutter takes 18px more.  The
     gutter is always reserved, so entering delete mode never squeezes NAME
     or changes the columns. */
  var ROW_OVERHEAD = 53;
  var dropOrder = opts.columns
    .filter(function (c) {
      return c.drop;
    })
    .sort(function (a, b) {
      return a.drop - b.drop;
    });

  function fixedWidth(cols) {
    return cols.reduce(function (sum, c) {
      /* "150px" → 150; NAME's flexible track adds nothing. */
      var px = parseFloat(c.width);
      return c.key === "name" || isNaN(px) ? sum : sum + px;
    }, 0);
  }

  function visibleColumns() {
    var cols = opts.columns;
    /* Unmeasured (hidden, or no layout at all): every column. */
    if (!state.width) return cols;
    /* The sorted column goes last, so its caret shows as long as it can. */
    var order = dropOrder
      .filter(function (c) {
        return c.key !== state.sortKey;
      })
      .concat(
        dropOrder.filter(function (c) {
          return c.key === state.sortKey;
        }),
      );
    for (var i = 0; i < order.length; i++) {
      var room = state.width - ROW_OVERHEAD - fixedWidth(cols);
      if (room >= (order[i].nameMin || NAME_MIN)) break;
      cols = cols.filter(function (c) {
        return c !== order[i];
      });
    }
    return cols;
  }

  function sameColumns(a, b) {
    return (
      a.length === b.length &&
      a.every(function (c, i) {
        return c === b[i];
      })
    );
  }

  function gridTemplate(cols) {
    return cols
      .map(function (c) {
        return c.width;
      })
      .join(" ");
  }

  function renderHeaders(cols) {
    if (!opts.headerEl) return;
    opts.headerEl.style.gridTemplateColumns = gridTemplate(cols);
    /* Shift the headers in lockstep with the rows' checkbox gutter so the
       columns stay registered while multi-selecting. */
    var deleting = controller.inMode();
    opts.headerEl.classList.toggle("saved-cols-delete", deleting);
    var focused = focusedKey(opts.headerEl);
    opts.headerEl.replaceChildren();
    cols.forEach(function (col) {
      var active = col.key === state.sortKey;
      var h = document.createElement("span");
      h.className =
        "scol" +
        (col.align === "right" ? " scell-r" : "") +
        (active ? " sorted" : "");
      h.dataset.focusKey = "sort:" + col.key;
      h.setAttribute("role", "button");
      h.setAttribute("tabindex", deleting ? "-1" : "0");
      /* Sorting fetches a different page, which would drop the selections
         on this one — the same reason the pager hides in delete mode. */
      if (deleting) h.setAttribute("aria-disabled", "true");
      /* aria-sort is not exposed on a button, so the name carries the
         order; it keeps the visible label for speech input. */
      h.setAttribute(
        "aria-label",
        "Sort by " +
          col.label +
          (active
            ? state.sortDir < 0
              ? ", sorted descending"
              : ", sorted ascending"
            : ""),
      );
      if (deleting) h.title = pauseHint;
      else if (col.title) h.title = col.title;
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
        if (controller.inMode()) return;
        var before = visibleColumns();
        if (state.sortKey === col.key) {
          state.sortDir = -state.sortDir;
        } else {
          state.sortKey = col.key;
          state.sortDir = col.order === "asc" ? 1 : -1;
        }
        /* A new order starts from its first page.  The sorted column drops
           last, so a new sort can change which columns fit and redraw the
           rows; otherwise only the headers change before the page lands. */
        state.page = 0;
        var cols = visibleColumns();
        if (sameColumns(before, cols)) renderHeaders(cols);
        else render();
        requestPage();
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
    refocus(opts.headerEl, [focused]);
  }

  /* footer copy, for the page on screen:
       start  — index of its first row within the total
       shown  — rows painted on it
       pages  — total page count
       note   — appended to the count (a sort whose column is hidden)
     When the list spans more than one page the footer leads with the
     visible range so the Prev/Next control reads as intentional paging, not
     a silent truncation.  The total is the server's count of every matching
     row. */
  function renderFooter(start, shown, pages, note) {
    if (!opts.footerEl) return;
    /* Nothing to count before the first page lands. */
    if (!state.loaded) {
      opts.footerEl.textContent = "";
      return;
    }
    var total = state.total;
    var filter = state.shownFilter;
    var noun = opts.noun + (total === 1 ? "" : "s");
    if (!total) {
      opts.footerEl.textContent = filter ? "" : "0 " + noun;
      if (filter) {
        /* The body already shows that nothing matches; the footer is the
           live region, so it says so too, to screen readers only. */
        var note = document.createElement("span");
        note.className = "sr-only";
        note.textContent = noMatchText(filter);
        opts.footerEl.appendChild(note);
      }
      return;
    }
    if (pages > 1 && shown) {
      var range =
        "Showing " +
        (start + 1) +
        "–" +
        (start + shown) +
        " of " +
        total +
        " " +
        noun;
      opts.footerEl.textContent =
        (filter ? range + " matching “" + filter + "”" : range) + note;
      return;
    }
    opts.footerEl.textContent =
      (filter
        ? total +
          " " +
          noun +
          (total === 1 ? " matches" : " match") +
          " “" +
          filter +
          "”"
        : total + " " + noun) + note;
  }

  /* The sorted column drops last, but it can drop: the footer then says what
     order the rows are in. */
  function hiddenSortNote(cols) {
    var shown = cols.some(function (c) {
      return c.key === state.sortKey;
    });
    var col = opts.columns.find(function (c) {
      return c.key === state.sortKey;
    });
    if (shown || !col) return "";
    return (
      " · sorted by " +
      col.label.toLowerCase() +
      (state.sortDir < 0 ? ", descending" : ", ascending")
    );
  }

  /* Fill the optional .pagination container with Prev / “page X / Y” / Next.
     Hidden when the list fits on one page or while multi-selecting: paging
     in delete mode would orphan the user's checkbox selections, which live
     on the visible page only.  Buttons are rebuilt each render, and the one
     that had focus passes it on.  The page label is intentionally NOT a
     live region — the footer (already aria-live) announces the resulting
     "Showing X–Y of Z" range. */
  function renderPagination(pages) {
    if (!opts.paginationEl) return;
    var pag = opts.paginationEl;
    var focused = focusedKey(pag);
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
    var current = state.shownPage;
    /* Prev and Next step from the page on screen, so a request still out,
       or one that failed, never changes where they lead. */
    var prev = document.createElement("button");
    prev.type = "button";
    prev.dataset.focusKey = "pager:prev";
    prev.setAttribute("aria-label", "Previous page");
    prev.textContent = "◄ Prev";
    prev.disabled = current <= 0;
    prev.onclick = function () {
      if (current > 0) {
        state.page = current - 1;
        requestPage();
      }
    };
    var label = document.createElement("span");
    label.textContent = current + 1 + " / " + pages;
    var next = document.createElement("button");
    next.type = "button";
    next.dataset.focusKey = "pager:next";
    next.setAttribute("aria-label", "Next page");
    next.textContent = "Next ►";
    next.disabled = current >= pages - 1;
    next.onclick = function () {
      if (current < pages - 1) {
        state.page = current + 1;
        requestPage();
      }
    };
    pag.replaceChildren(prev, label, next);
    pag.setAttribute(
      "aria-label",
      "Saved " + opts.noun + "s — page " + (current + 1) + " of " + pages,
    );
    /* Reaching the first or last page disables the button just used; focus
       moves to the other one. */
    if (focused) {
      refocus(pag, [
        focused,
        focused === "pager:next" ? "pager:prev" : "pager:next",
      ]);
    }
  }

  function render() {
    /* Measured here as well, so a page lands in the columns that fit. */
    var measured = opts.bodyEl && opts.bodyEl.clientWidth;
    if (measured) state.width = measured;
    var cols = visibleColumns();
    var rows = state.rows;
    var pages = Math.max(1, Math.ceil(state.total / PAGE_SIZE));
    controller.setItems(rows);
    var deleting = controller.inMode();
    /* Rows being selected stay as they are; their fetch waits (see
       requestPage). */
    if (deleting) setPending(false);
    /* The fetch that render() replays below, once delete mode has ended. */
    var replaying = deferredFetch && !deleting;
    var deleteButton = document.getElementById(opts.delete.buttonId);
    if (deleteButton) {
      /* Its place in the toolbar follows the viewer, not this page or
         search, so the search box never shifts under someone typing in it:
         taken whenever the viewer may delete, shown while the list has
         anything (a search that matches nothing included). */
      deleteButton.style.display = opts.canDeleteAny() ? "" : "none";
      deleteButton.style.visibility =
        state.total > 0 || state.shownFilter ? "" : "hidden";
    }
    /* Searching fetches a different page too (see renderHeaders). */
    if (opts.filterEl) {
      opts.filterEl.disabled = deleting;
      opts.filterEl.title = deleting ? pauseHint : "";
    }
    var focused = focusedKey(opts.bodyEl);
    /* One grid write per render: rows read it from the inherited CSS var. */
    if (opts.bodyEl) {
      opts.bodyEl.style.setProperty("--saved-grid", gridTemplate(cols));
      opts.bodyEl.replaceChildren();
    }
    if (!rows.length) {
      /* Empty state owns the space — hide the column headers so it doesn't
         read as a broken grid. */
      if (opts.headerEl) opts.headerEl.style.display = "none";
      var empty = document.createElement("div");
      empty.className = "dashboard-empty";
      /* A total above zero means the list goes on, only not on this page:
         rows left between the count and the page query, or were deleted,
         in which case the page is already being fetched again. */
      empty.textContent =
        replaying || !state.loaded
          ? "Loading…"
          : state.total > 0
            ? "No " + opts.noun + "s on this page"
            : state.shownFilter
              ? noMatchText(state.shownFilter)
              : opts.emptyText || "No saved items";
      if (opts.bodyEl) opts.bodyEl.appendChild(empty);
    } else {
      if (opts.headerEl) opts.headerEl.style.display = "";
      renderHeaders(cols);
      rows.forEach(function (sess) {
        var row = renderSessionRow(sess, {
          columns: cols,
          canActivate: !opts.canActivate || opts.canActivate(sess),
          ariaLabel: controller.ariaLabel,
          onActivate: function (s, el) {
            if (controller.blockActivate()) return;
            if (opts.canActivate && !opts.canActivate(s)) return;
            if (typeof opts.onActivate === "function") opts.onActivate(s, el);
          },
        });
        row.dataset.focusKey = "row:" + sess.ws_id;
        controller.decorateCard(row, sess);
        opts.bodyEl.appendChild(row);
      });
      refocus(opts.bodyEl, [focused]);
    }
    if (deleting) controller.refreshBar();
    renderPagination(pages);
    renderFooter(
      state.shownPage * PAGE_SIZE,
      rows.length,
      pages,
      hiddenSortNote(cols),
    );
    /* Delete mode just ended (the controller re-renders on every mode
       change): fetch what was asked for meanwhile. */
    if (deferredFetch && !controller.inMode()) {
      deferredFetch = false;
      requestPage();
    }
  }

  /* Debounce the search keystrokes — each settled value is one request.
     Text an input method is still composing is not a search yet: the
     compositionend that commits it schedules one. */
  var filterTimer = null;
  function scheduleSearch() {
    if (filterTimer) clearTimeout(filterTimer);
    filterTimer = setTimeout(function () {
      var value = opts.filterEl.value.trim();
      if (value === state.filter) return;
      state.filter = value;
      /* A new search starts from its first page. */
      state.page = 0;
      requestPage();
    }, 250);
  }
  if (opts.filterEl) {
    opts.filterEl.addEventListener("input", function (e) {
      if (e && e.isComposing) return;
      scheduleSearch();
    });
    opts.filterEl.addEventListener("compositionend", scheduleSearch);
  }

  /* Saved table owns its responsive layout (see visibleColumns): it watches
     its own width and re-renders only when that changes which columns fit.
     A hidden table measures 0 and keeps its last width. */
  if (typeof ResizeObserver === "function" && opts.bodyEl) {
    var refit = false;
    new ResizeObserver(function (entries) {
      var width = Math.round(entries[0].contentRect.width);
      if (!width || width === state.width) return;
      var before = visibleColumns().length;
      state.width = width;
      /* An empty table shows no columns. */
      if (!state.rows.length || visibleColumns().length === before) return;
      /* Redraw on the next frame, not inside the observer: new rows change
         the body's height, which the browser would otherwise report as a
         resize loop. */
      if (refit) return;
      refit = true;
      requestAnimationFrame(function () {
        refit = false;
        render();
      });
    }).observe(opts.bodyEl);
  }

  return {
    /* Fetch the current query: the app's refresh. */
    load: function (ready) {
      /* Until a page lands the table says it is loading. */
      if (!state.loaded) render();
      fetchPage(true, ready);
    },
    reset: function () {
      if (filterTimer) clearTimeout(filterTimer);
      /* Answers still out belong to the identity this forgets. */
      generation++;
      inFlight = null;
      changed = false;
      deferredFetch = false;
      setPending(false);
      setRetrying(false);
      setError("");
      state.rows = [];
      state.total = 0;
      state.filter = "";
      state.page = 0;
      state.shownPage = 0;
      state.shownFilter = "";
      state.loaded = false;
      if (opts.filterEl) opts.filterEl.value = "";
      controller.reset();
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
     - the confirmation dialog (hatch dialog tier: batch fan-out + results
       view; focus trap / Escape / busy lock belong to hatch.js)

   It does NOT own how cards get fetched or rendered — the caller's
   render() is invoked when the controller needs the list redrawn (mode
   transitions, Select-All toggles).

   Required opts:
     idPrefix          — DOM-id prefix shared by the toolbar + dialog
                         (e.g. "ws-delete" / "coord-delete").  The DOM
                         must already contain `${idPrefix}-bar`,
                         `${idPrefix}-bar-count`, `${idPrefix}-bar-delete`,
                         `${idPrefix}-bar-select-all`, `${idPrefix}-dialog`
                         (a `dialog.hatch.hatch--dialog`), `${idPrefix}-error`,
                         `${idPrefix}-count`, `${idPrefix}-list`,
                         `${idPrefix}-meta`, `${idPrefix}-confirm-btn`.
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
     onDeleted         — optional ids => void; called once when the user
                         closes the post-delete results modal, with the
                         ids that were deleted, just before delete mode
                         ends (render() runs next).  Typical use: drop
                         those rows and re-fetch the saved list.
*/
export function createSavedCardsController(opts) {
  var state = { mode: false, selected: {}, items: [] };
  var generation = 0;

  function canDelete(sess) {
    return !opts.canDelete || opts.canDelete(sess);
  }

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
    state.items = items.filter(canDelete);
    /* Drop any selections whose ws_id is no longer on the visible page —
       SSE-driven re-renders or pagination jumps shouldn't leave ghost
       entries inflating the count and 404-ing on confirm. */
    if (state.mode) {
      var byId = {};
      state.items.forEach(function (s) {
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
    if (!state.mode || !canDelete(sess)) return;
    card.classList.add("ws-delete-mode");
    card.removeAttribute("role");
    var chk = document.createElement("input");
    chk.type = "checkbox";
    chk.className = "ws-card-check";
    chk.checked = !!state.selected[sess.ws_id];
    var label = sess.alias || sess.title || sess.name || sess.ws_id;
    chk.setAttribute(
      "aria-label",
      "Select " + label + _skillNote(sess) + " for deletion",
    );
    chk.dataset.focusKey = "check:" + sess.ws_id;
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
    /* The page may hold rows this viewer cannot delete (the list's other
       pages may not). */
    if (!state.items.length) {
      if (typeof showToast === "function") {
        showToast("Nothing on this page you can delete");
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
    /* Single-pass index over the visible items so the dialog + fan-out
       paths don't repeat O(N) `find` calls per selection. */
    var map = {};
    state.items.forEach(function (s) {
      map[s.ws_id] = s;
    });
    return map;
  }

  function _deleteLabel(count) {
    return (
      "Delete " + count + " " + (count === 1 ? opts.noun : opts.noun + "s")
    );
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
    var dlg = $("dialog");
    var countEl = $("count");
    var listEl = $("list");
    var errorEl = $("error");
    var metaEl = $("meta");
    if (errorEl) {
      errorEl.textContent = "";
      errorEl.classList.remove("is-visible");
    }
    /* The results view hides Cancel (Close-only foot) and may have
       flipped the chrome to the success kind — restore both. */
    var cancelBtn = dlg.querySelector(".sh-foot [data-close]");
    if (cancelBtn) cancelBtn.hidden = false;
    dlg.setAttribute("data-kind", "danger");
    if (metaEl) metaEl.textContent = selected.length + " selected";
    if (countEl) {
      countEl.textContent =
        (selected.length === 1
          ? "This " + opts.noun
          : "These " + opts.noun + "s") + " will be permanently deleted:";
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
      delBtn.textContent = _deleteLabel(selected.length);
      delBtn.classList.add("sh-btn--danger");
      delBtn.onclick = confirm;
    }
    /* Hatch owns the rest: focus trap, Escape, backdrop click, the busy
       lock, and focus restore to the opener.  Cancel carries the markup
       autofocus (destructive-confirm rule).  The onClose runs on EVERY
       dismissal path (footer Close, header ✕, Escape, backdrop) — once
       results are showing, any of them must exit delete mode and refresh
       the now-stale list, not just the footer button. */
    state.resultsShown = false;
    state.deleted = [];
    window.TurnstoneHatch.openDialog(dlg, {
      onClose: function () {
        if (!state.resultsShown) return; // pre-delete cancel keeps the mode
        state.resultsShown = false;
        /* Still in delete mode: the caller's refetch waits for the mode
           change that follows, so there is one fetch, not two. */
        if (typeof opts.onDeleted === "function") opts.onDeleted(state.deleted);
        cancel();
        var t = document.getElementById(opts.buttonId);
        if (t && typeof t.focus === "function") t.focus();
      },
    });
  }

  function closeModal() {
    var dlg = $("dialog");
    if (dlg && dlg.open) dlg.close();
  }

  function confirm() {
    var currentGeneration = generation;
    var selected = Object.keys(state.selected);
    if (!selected.length) return;
    var byId = _byId();
    var dlg = $("dialog");
    var errorEl = $("error");
    var listEl = $("list");
    var countEl = $("count");
    var metaEl = $("meta");
    var delBtn = $("confirm-btn");
    if (errorEl) {
      errorEl.textContent = "";
      errorEl.classList.remove("is-visible");
    }
    /* LED pulses, actions lock, dismissal refused while the fan-out runs. */
    window.TurnstoneHatch.setBusy(dlg, true);

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
            results.push({
              wsId: wsId,
              name: name,
              shortId: shortId,
              ok: true,
            });
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
              // Non-JSON failures are often whole HTML error pages (proxy
              // 502s, gateway timeouts) — strip markup before display.
              var plain = body
                .replace(/<(style|script)[\s\S]*?<\/\1>/gi, " ")
                .replace(/<[^>]+>/g, " ")
                .replace(/\s+/g, " ")
                .trim();
              errMsg =
                shortId + ": " + (plain.substring(0, 120) || "HTTP " + status);
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
      if (currentGeneration !== generation) return;
      window.TurnstoneHatch.setBusy(dlg, false);
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
      // Failures land in the live alert region (the summary prose is
      // polite-live for the all-good case); a clean run flips the chrome
      // to the success kind — red head over "3 deleted, 0 failed" would
      // disagree with the de-dangered foot.
      if (errorEl && failCount > 0) {
        errorEl.textContent =
          failCount + " of " + results.length + " deletions failed";
        errorEl.classList.add("is-visible");
      }
      if (dlg)
        dlg.setAttribute("data-kind", failCount === 0 ? "success" : "danger");
      if (metaEl) metaEl.textContent = "";
      /* Close-only foot: a Cancel beside a Close would be the redundant
         dismissal pair the foot grammar forbids. */
      var cancelBtn = dlg ? dlg.querySelector(".sh-foot [data-close]") : null;
      if (cancelBtn) cancelBtn.hidden = true;
      state.deleted = results
        .filter(function (r) {
          return r.ok;
        })
        .map(function (r) {
          return r.wsId;
        });
      state.resultsShown = true;
      if (delBtn) {
        delBtn.textContent = "Close";
        /* The results view's action is no longer destructive — drop the
           danger fill (confirmSelection restores it on the next open). */
        delBtn.classList.remove("sh-btn--danger");
        /* Teardown (exit delete mode, refresh the stale list, focus the
           rebuilt section toggle) lives on the dialog's onClose so the
           header ✕ / Escape / backdrop run it too — Close just closes. */
        delBtn.onclick = closeModal;
        // The state just changed under the user — land focus somewhere
        // predictable (the only remaining action).
        delBtn.focus();
      }
    });
  }

  return {
    reset: function () {
      generation++;
      state.resultsShown = false;
      var dlg = $("dialog");
      if (dlg && dlg.open) {
        window.TurnstoneHatch.setBusy(dlg, false);
        dlg.close();
      }
      ["list", "count", "meta", "error"].forEach(function (id) {
        var element = $(id);
        if (element) element.textContent = "";
      });
      cancel();
    },
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

// --- Legacy window bridge ---------------------------------------------------
// Still-classic consumers reach these as globals at event/boot time (after
// this deferred module evaluated).  New module code imports instead.
Object.assign(window, {
  SavedColumns,
  createSavedTable,
});
