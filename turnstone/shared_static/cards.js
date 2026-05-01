/* Shared session card primitive — used by ui/static (Saved Workstreams)
   and console/static (Saved Coordinators).  Single source so the two
   surfaces don't drift on field cascade, ARIA roles, keyboard handling,
   or DOM shape.

   Card structure (also see /shared/cards.css for styling):

     .dashboard-card               role="button" tabindex="0"
       .card-title                 sess.alias || title || name || ws_id[:12]
       .card-meta                  "X msgs · Y ago "
         .card-wsid                ws_id[:7]

   Built with safe DOM APIs (createElement + textContent) — never
   innerHTML — so user-supplied alias/title/name fields don't reach the
   DOM as HTML.

   Caller passes:
     sess          — {ws_id, alias?, title?, name?, message_count?, updated?}
     opts.onActivate(sess)  — fired on click + Enter/Space
     opts.ariaLabel(sess)?  — optional aria-label override; default
                              "Resume: {label}"
     opts.busy?             — boolean; adds `is-busy` class (visual dim
                              + cursor: progress) and suppresses re-entry
                              into onActivate.

   Returns the card DOM node.  Caller appends it.

   Depends on: formatRelativeTime (from /shared/utils.js).
*/

function renderSessionCard(sess, opts) {
  opts = opts || {};
  var card = document.createElement("div");
  card.className = "dashboard-card" + (opts.busy ? " is-busy" : "");
  card.dataset.wsId = sess.ws_id;
  var label = sess.alias || sess.title || sess.name || sess.ws_id;
  card.setAttribute("role", "button");
  card.setAttribute("tabindex", "0");
  card.setAttribute(
    "aria-label",
    typeof opts.ariaLabel === "function"
      ? opts.ariaLabel(sess)
      : "Resume: " + label,
  );

  var activate = function () {
    if (card.classList.contains("is-busy")) return;
    if (typeof opts.onActivate === "function") opts.onActivate(sess, card);
  };
  card.onclick = activate;
  card.onkeydown = function (e) {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      activate();
    }
  };

  var title =
    sess.alias || sess.title || sess.name || sess.ws_id.substring(0, 12);
  var titleEl = document.createElement("div");
  titleEl.className = "card-title";
  titleEl.textContent = title;

  var metaEl = document.createElement("div");
  metaEl.className = "card-meta";
  var metaText = (sess.message_count || 0) + " msgs";
  if (sess.updated && typeof formatRelativeTime === "function") {
    metaText += " · " + formatRelativeTime(sess.updated);
  }
  metaEl.appendChild(document.createTextNode(metaText + " "));
  var wsidEl = document.createElement("span");
  wsidEl.className = "card-wsid";
  wsidEl.textContent = sess.ws_id.substring(0, 7);
  metaEl.appendChild(wsidEl);

  card.appendChild(titleEl);
  card.appendChild(metaEl);
  return card;
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

  /* Decorate an already-rendered .dashboard-card with the checkbox +
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
