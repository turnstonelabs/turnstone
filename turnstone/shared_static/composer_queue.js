/* composer_queue.js — shared optimistic-queue UI for the chat composer.
 *
 * Used by both:
 *   - turnstone/ui/static/app.js (interactive Pane)
 *   - turnstone/console/static/coordinator/coordinator.js (coord IIFE)
 *
 * What this owns:
 *   - The queued-message bubble's DOM shape (msg-queued / queued-badge
 *     / queued-dismiss) and its dismiss-while-in-flight state machine.
 *   - The on-idle sweep that strips queued styling once the worker
 *     drains (caller invokes onIdleEdge() on the busy → idle edge).
 *
 * What this does NOT own:
 *   - Sending. Caller renders the bubble before the POST, then later
 *     calls bind(el, msgId) when the server's response carries the id,
 *     promote(el) when the response settled the message another way
 *     (e.g. an "ok"/unknown status), or remove(el) on a reject path.
 *   - Busy state. The shape is "addQueuedMessage on send, settle on
 *     response / promote on idle"; caller orchestrates around its busy flag.
 *
 * Dismiss contract: a queued card is NEVER removed before the server
 * confirms the cancel. Clicking × marks the card "dismissing" (aria-disabled
 * × + aria-busy); the bubble only leaves the DOM on a confirmed `removed`.
 * On `not_found` (already drained) it is promoted to a normal sent bubble
 * with an "already sent" notice; on any error/timeout the × is re-enabled
 * so the user can retry. This avoids the trust-eroding divergence of a
 * card that shows "cancelled" while the message is still delivered.
 *
 * Caller options:
 *   messagesEl: HTMLElement — chat log container.
 *   getWsId:    () => string — current ws id (function so the
 *               interactive pane can swap tabs without re-instantiating).
 *   getBase:    optional () => string — node-proxy URL prefix ("" local,
 *               "/node/{id}" proxied). Applied to the dequeue DELETE so it
 *               reaches the node that owns the session. Default "".
 *   wrapInBody: bool — when true (coord), wrap the queued content in a
 *               .msg-body div to match the surrounding .msg shape; when
 *               false (interactive), append children directly to the
 *               .msg element. Default false to match the historical
 *               interactive shape.
 *   authFetch:  optional override (default window.authFetch).
 *   onAfterDequeue: optional () => void — re-sync the composer's staged
 *               attachment chips (interactive and coord both wire it to
 *               attachments.rehydrate). Fires only on a confirmed `removed`
 *               — the one verdict that mutated server-side queue state;
 *               `not_found`/error change nothing, so re-fetching would be
 *               wasted. Queued messages are text-only, so this isn't undoing
 *               an attachment reservation — there is none.
 *   onNotice:   optional (msg) => void — surfaces a user-facing notice
 *               (e.g. a toast): "already sent" when a dismiss lost the race
 *               to delivery, or "couldn't remove" when the DELETE failed.
 *   onIdle:     optional () => void — fires inside onIdleEdge() after
 *               the bubble sweep, so the consumer can run its own
 *               edge-only cleanup (e.g. clearing cancel/force-stop
 *               timers) without re-implementing edge detection.
 *
 * Returned controller surface:
 *   addQueuedMessage(text, priority) -> el
 *       priority: "important" | anything-else (treated as "notice")
 *   bind(el, msgId)
 *       Server returned status:queued + msg_id. Stamps msgId so the × can
 *       dequeue; if the user already clicked × (pre-bind) runs the
 *       confirming delete now; if the idle sweep already promoted the
 *       bubble (already delivered), leaves it untouched.
 *   promote(el)
 *       Settle an optimistic bubble as a normal sent message — used by the
 *       consumer when the send response wasn't "queued" (e.g. an "ok"
 *       stale-busy race) so a pre-bind × can't strand the card.
 *   remove(el)
 *       Drop the bubble (busy / queue_full / connection-error path).
 *   onIdleEdge()
 *       Caller invokes once per busy → idle transition. Promotes every
 *       not-in-flight queued bubble and then fires the onIdle hook.
 */
export function createQueueController(opts) {
  if (!opts || !opts.messagesEl)
    throw new Error("createQueueController: messagesEl required");
  if (typeof opts.getWsId !== "function")
    throw new Error("createQueueController: getWsId must be a function");
  var messagesEl = opts.messagesEl;
  var getWsId = opts.getWsId;
  var wrapInBody = !!opts.wrapInBody;
  // Node-proxy URL prefix for the active tab ("" local, "/node/{id}"
  // proxied). Mirrors composer_attachments's getBase so the dequeue
  // DELETE lands on the node that owns the ChatSession, not the console
  // root — without it, x-delete on a proxied interactive workstream
  // 404s and the queued message is never removed (it gets delivered).
  var getBase =
    typeof opts.getBase === "function"
      ? opts.getBase
      : function () {
          return "";
        };
  var onAfterDequeue =
    typeof opts.onAfterDequeue === "function" ? opts.onAfterDequeue : null;
  var onIdle = typeof opts.onIdle === "function" ? opts.onIdle : null;
  var onNotice = typeof opts.onNotice === "function" ? opts.onNotice : null;
  // Live queued bubbles — the idle sweep iterates this instead of querying
  // the whole messages container (see onIdleEdge).
  var _liveQueued = new Set();
  // Upper bound on the dequeue DELETE so a wedged proxied node (the exact
  // case this flow targets) can't leave a card stuck "dismissing" forever.
  var DELETE_TIMEOUT_MS = 15000;
  // Lazy authFetch lookup — see composer_attachments.js for the
  // rationale; same load-order robustness applies here.
  function _authFetch(url, init) {
    var fn = opts.authFetch || window.authFetch;
    return fn(url, init);
  }

  function _scrollIntoView() {
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  function _deleteRequest(msgId) {
    var wsId = getWsId();
    if (!wsId || !msgId) return null;
    var base = getBase() || "";
    var init = {
      method: "DELETE",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ msg_id: msgId }),
    };
    // Abort on timeout so the promise always settles — without this a hung
    // request leaves the × disabled + aria-busy with no recovery path.
    var ctrl =
      typeof AbortController === "function" ? new AbortController() : null;
    var timer = null;
    if (ctrl) {
      init.signal = ctrl.signal;
      timer = setTimeout(function () {
        ctrl.abort();
      }, DELETE_TIMEOUT_MS);
    }
    var p = _authFetch(
      base + "/v1/api/workstreams/" + encodeURIComponent(wsId) + "/send",
      init,
    );
    if (timer) {
      return p.finally(function () {
        clearTimeout(timer);
      });
    }
    // No AbortController (old runtime): can't cancel the fetch, but still
    // bound the promise — race a rejecting timeout so a hung request settles
    // into _confirmDequeue's catch (re-enable + notice). The fetch keeps
    // running; its result is then ignored.
    var fbTimer = null;
    return Promise.race([
      p,
      new Promise(function (_resolve, reject) {
        fbTimer = setTimeout(function () {
          reject(new Error("dequeue_timeout"));
        }, DELETE_TIMEOUT_MS);
      }),
    ]).finally(function () {
      clearTimeout(fbTimer);
    });
  }

  function addQueuedMessage(text, priority) {
    var el = document.createElement("div");
    el.className = "msg user msg-queued";
    el.setAttribute("role", "status");
    var important = priority === "important";
    if (important) {
      el.classList.add("msg-queued-important");
      el.setAttribute("aria-label", "Important message queued: " + text);
    } else {
      el.setAttribute("aria-label", "Message queued: " + text);
    }

    var badge = document.createElement("span");
    badge.className = "queued-badge";
    badge.setAttribute("aria-hidden", "true");
    badge.textContent = important ? "queued (!!!) " : "queued ";

    var textNode = document.createTextNode(text);

    var dismiss = document.createElement("button");
    dismiss.type = "button";
    dismiss.className = "queued-dismiss";
    dismiss.title = "Remove from queue";
    dismiss.setAttribute("aria-label", "Remove queued message");
    dismiss.textContent = "×";
    dismiss.addEventListener("click", function (e) {
      e.stopPropagation();
      dequeue(el);
    });

    var host;
    if (wrapInBody) {
      host = document.createElement("div");
      host.className = "msg-body";
      el.appendChild(host);
    } else {
      host = el;
    }
    host.appendChild(badge);
    host.appendChild(textNode);
    host.appendChild(dismiss);

    messagesEl.appendChild(el);
    _liveQueued.add(el);
    _scrollIntoView();
    return el;
  }

  // Toggle a queued bubble's in-flight "dismissing" state: mark the row
  // aria-busy and the × aria-disabled so a click gives immediate feedback
  // without removing the card before the server confirms. We use
  // aria-disabled rather than the real `disabled` attribute so disabling the
  // focused × doesn't drop keyboard/AT focus to <body> (WCAG 2.4.3); the
  // dequeue() guard makes the in-flight × inert. Reversible — a failed or
  // declined delete clears it (and the dismiss flag) so the user can retry.
  function _setDismissing(el, on) {
    var dismiss = el.querySelector(".queued-dismiss");
    if (on) {
      el.setAttribute("aria-busy", "true");
      if (dismiss) dismiss.setAttribute("aria-disabled", "true");
    } else {
      el.removeAttribute("aria-busy");
      if (dismiss) dismiss.removeAttribute("aria-disabled");
      // Clear the dismiss-intent flag on the re-enable off-ramp so a later
      // idle-sweep _promote can't fire a contradictory "already sent" after
      // we've told the user the removal didn't take.
      delete el.dataset.dismissAttempted;
    }
  }

  // Issue the DELETE and reconcile the card with the server's verdict:
  //   removed   → drop the card (the message never reached the assistant)
  //   not_found → already drained; _promote() to a sent bubble (+ notice)
  //   404 gone  → workstream reaped/closed; drop the card with a terminal
  //               "no longer available" notice (a retry would just 404 again)
  //   else / non-2xx / transport / timeout → keep the card, re-enable the
  //     × and tell the user it didn't take so they can retry.
  function _confirmDequeue(el, msgId) {
    var p = _deleteRequest(msgId);
    if (!p) {
      _setDismissing(el, false);
      return;
    }
    p.then(function (r) {
      // 404 = reaped/closed workstream (distinct from a 200 not_found): the
      // message is gone and a retry would just 404 again. Terminal — drop the
      // card rather than stranding it un-dismissable in the retry loop.
      if (r.status === 404) {
        el.remove();
        if (onNotice)
          onNotice(
            "This conversation is no longer available — the message wasn't delivered.",
          );
        return null;
      }
      // Other non-2xx (400 "No session" / 5xx) carry no {status}; route them
      // through the catch so the user is told, not a silent re-enable that
      // reads as "delete is broken".
      if (!r.ok) throw new Error("dequeue_http_" + r.status);
      return r.json();
    })
      .then(function (data) {
        if (data === null) return; // 404 handled above
        var status = data && data.status;
        if (status === "removed") {
          el.remove();
          // `removed` is the only verdict that mutated server-side queue
          // state, so it's the only one worth re-syncing composer state for.
          if (onAfterDequeue) onAfterDequeue();
        } else if (status === "not_found") {
          // Already drained — present it as the sent message it now is;
          // _promote fires the "already sent" notice (dismissAttempted).
          _promote(el);
        } else {
          // Unexpected 2xx shape — keep the card cancellable.
          _setDismissing(el, false);
        }
      })
      .catch(function () {
        _setDismissing(el, false);
        if (onNotice)
          onNotice("Couldn't remove the message — please try again.");
      });
  }

  // × handler. Marks the card dismissing and either confirms now (msg_id
  // known) or defers to bind() (pre-bind). We never remove optimistically:
  // if the deferred delete failed, the message would still be delivered
  // while the card showed "cancelled". dismissAttempted lets _promote fire
  // the "already sent" notice if the message turns out to have been
  // delivered before we could cancel it, and signals bind() to confirm.
  function dequeue(el) {
    // Ignore re-clicks while a dismiss is in flight — the × stays
    // aria-disabled (not real `disabled`, so focus isn't lost), which means
    // the click still fires; this guard is what makes the in-flight × inert.
    if (el.getAttribute("aria-busy") === "true") return;
    el.dataset.dismissAttempted = "1";
    _setDismissing(el, true);
    var msgId = el.dataset.msgId;
    if (!msgId) {
      // Pre-bind: bind() confirms once the server returns the msg_id (it
      // reads dismissAttempted).
      return;
    }
    _confirmDequeue(el, msgId);
  }

  // Server returned status:queued + msg_id.
  function bind(el, msgId) {
    if (!el || !msgId) return;
    // Idle sweep already promoted the bubble (worker drained mid-POST): the
    // message is on its way, so leave it as a sent bubble. We deliberately do
    // NOT delete here — idle can be emitted before the worker actually drains,
    // so a delete could cancel a still-queued message; and a dismissed card
    // never reaches this branch (onIdleEdge skips aria-busy cards), so doing
    // nothing is the safe action.
    if (!el.classList.contains("msg-queued")) return;
    el.dataset.msgId = msgId;
    // User clicked × before the id arrived → confirm the delete now
    // (removes only on a confirmed `removed`; promotes on not_found).
    if (el.dataset.dismissAttempted) _confirmDequeue(el, msgId);
  }

  function remove(el) {
    _liveQueued.delete(el);
    if (el && el.parentNode) el.remove();
  }

  // Strip the queued affordances so a bubble renders as a normal
  // (delivered) user message. Shared by the idle sweep, the not_found
  // dequeue path, and the consumer's "ok"/unknown send-response path: a
  // message the worker already drained is on its way and can't be
  // cancelled, so present it as sent. If the user had clicked × first
  // (dismissAttempted), tell them it was too late.
  function _promote(el) {
    _liveQueued.delete(el);
    var attempted = el.dataset.dismissAttempted;
    el.classList.remove("msg-queued", "msg-queued-important");
    delete el.dataset.msgId;
    delete el.dataset.dismissAttempted;
    el.removeAttribute("role");
    el.removeAttribute("aria-label");
    el.removeAttribute("aria-busy");
    var badge = el.querySelector(".queued-badge");
    if (badge) badge.remove();
    var dismiss = el.querySelector(".queued-dismiss");
    if (dismiss) dismiss.remove();
    if (attempted && onNotice)
      onNotice("Already sent — too late to remove from the queue.");
  }

  // Caller invokes onIdleEdge() exactly once per busy → idle transition.
  // Promotes every queued bubble that isn't mid-dequeue (a [aria-busy] card
  // has a DELETE in flight — let _confirmDequeue settle it so the sweep
  // can't promote a card the delete is about to remove) and then fires the
  // onIdle hook so the consumer can run edge-only cleanup (e.g. clearing
  // cancel/force-stop timers).
  function onIdleEdge() {
    // Sweep the controller-local live set, not the DOM: the old
    // ".msg-queued:not([aria-busy])" query walked every element under the
    // messages container (O(transcript) per busy→idle edge) to find the
    // handful of queued bubbles that always sit in the tail.  Bubbles wiped
    // by a full re-render prune lazily via the isConnected check.
    _liveQueued.forEach(function (el) {
      if (!el.isConnected) {
        _liveQueued.delete(el);
        return;
      }
      if (el.hasAttribute("aria-busy")) return; // mid-dequeue — let it settle
      _promote(el);
    });
    if (onIdle) onIdle();
  }

  return {
    addQueuedMessage: addQueuedMessage,
    bind: bind,
    promote: _promote,
    remove: remove,
    onIdleEdge: onIdleEdge,
  };
}

// --- Legacy window bridge ---------------------------------------------------
// Still-classic consumers reach this as a global at event/boot time (after
// this deferred module evaluated).  New module code imports instead.
window.createQueueController = createQueueController;
