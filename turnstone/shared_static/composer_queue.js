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
 *     or remove(el) on a queue_full / busy reject path.
 *   - Busy state. The shape is "addQueuedMessage on send, promote on
 *     idle"; caller orchestrates both around its own busy flag.
 *
 * Caller options:
 *   messagesEl: HTMLElement — chat log container.
 *   getWsId:    () => string — current ws id (function so the
 *               interactive pane can swap tabs without re-instantiating).
 *   wrapInBody: bool — when true (coord), wrap the queued content in a
 *               .msg-body div to match the surrounding .msg shape; when
 *               false (interactive), append children directly to the
 *               .msg element. Default false to match the historical
 *               interactive shape.
 *   authFetch:  optional override (default window.authFetch).
 *   onAfterDequeue: optional () => void — interactive hooks attachment
 *               re-fetch here. Coord deliberately omits.
 *   onIdle:     optional () => void — fires inside onIdleEdge() after
 *               the bubble sweep, so the consumer can run its own
 *               edge-only cleanup (e.g. clearing cancel/force-stop
 *               timers) without re-implementing edge detection.
 *
 * Returned controller surface:
 *   addQueuedMessage(text, priority) -> el
 *       priority: "important" | anything-else (treated as "notice")
 *   bind(el, msgId)
 *       Server returned a queued msg_id. Stamps msgId onto the bubble,
 *       or releases the slot server-side when the bubble can no longer
 *       be dequeued (user dismissed pre-bind, or the promote sweep
 *       raced ahead). Caller need only invoke.
 *   remove(el)
 *       Drop the bubble (busy / queue_full / connection-error path).
 *   onIdleEdge()
 *       Caller invokes once per busy → idle transition. Strips queued
 *       styling from every bubble and then fires the onIdle hook.
 */
(function (root) {
  "use strict";

  function createQueueController(opts) {
    if (!opts || !opts.messagesEl)
      throw new Error("createQueueController: messagesEl required");
    if (typeof opts.getWsId !== "function")
      throw new Error("createQueueController: getWsId must be a function");
    var messagesEl = opts.messagesEl;
    var getWsId = opts.getWsId;
    var wrapInBody = !!opts.wrapInBody;
    var onAfterDequeue =
      typeof opts.onAfterDequeue === "function" ? opts.onAfterDequeue : null;
    var onIdle = typeof opts.onIdle === "function" ? opts.onIdle : null;
    // Lazy authFetch lookup — see composer_attachments.js for the
    // rationale; same load-order robustness applies here.
    function _authFetch(url, init) {
      var fn = opts.authFetch || root.authFetch;
      return fn(url, init);
    }

    function _scrollIntoView() {
      messagesEl.scrollTop = messagesEl.scrollHeight;
    }

    function _deleteRequest(msgId) {
      var wsId = getWsId();
      if (!wsId || !msgId) return null;
      return _authFetch(
        "/v1/api/workstreams/" + encodeURIComponent(wsId) + "/send",
        {
          method: "DELETE",
          credentials: "include",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ msg_id: msgId }),
        },
      );
    }

    // Fire-and-forget DELETE — used by bind() on a raced-away bubble
    // where the caller has no DOM follow-up. Still invokes
    // onAfterDequeue on success: the server-side reservation release
    // freed any attachments the queued message held, and the caller
    // typically rehydrates its chip pile so the user can reuse them.
    function _sendDelete(msgId) {
      var p = _deleteRequest(msgId);
      if (!p) return;
      p.then(function () {
        if (onAfterDequeue) onAfterDequeue();
      }).catch(function () {
        /* network error — promote loop strips queued styling on idle */
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
      _scrollIntoView();
      return el;
    }

    // Dismiss flow:
    // - msg_id known → DELETE /send, optimistically remove on success.
    // - msg_id not yet bound → mark pendingDismiss; bind() picks it up
    //   when the send response arrives.
    function dequeue(el) {
      var msgId = el.dataset.msgId;
      if (!msgId) {
        el.dataset.pendingDismiss = "true";
        el.remove();
        return;
      }
      var p = _deleteRequest(msgId);
      if (!p) return;
      p.then(function (r) {
        return r.json();
      })
        .then(function (data) {
          if (data && data.status === "removed") el.remove();
          if (onAfterDequeue) onAfterDequeue();
        })
        .catch(function () {
          /* network error — promote loop strips queued styling on idle */
        });
    }

    // Server returned status:queued + msg_id. Stamps msgId onto the
    // bubble, OR releases the slot server-side when the bubble can no
    // longer be dequeued from the UI (user dismissed pre-bind, or the
    // promote sweep raced ahead and stripped .msg-queued / its dismiss
    // button). Caller need only invoke; the controller handles all
    // three races without further callbacks.
    function bind(el, msgId) {
      if (!el || !msgId) return;
      var racedAway =
        el.dataset.pendingDismiss || !el.classList.contains("msg-queued");
      if (racedAway) {
        _sendDelete(msgId);
        return;
      }
      el.dataset.msgId = msgId;
    }

    function remove(el) {
      if (el && el.parentNode) el.remove();
    }

    // Caller invokes onIdleEdge() exactly once per busy → idle
    // transition. The controller strips queued styling from every
    // bubble (so optimistic queues render as normal user messages
    // once the worker has drained them) and then fires the optional
    // onIdle hook so the consumer can run its own edge-only cleanup
    // (e.g. clearing the cancel/force-stop timers) without each
    // consumer re-implementing the same edge-detection logic.
    function onIdleEdge() {
      var queued = messagesEl.querySelectorAll(".msg-queued");
      queued.forEach(function (el) {
        el.classList.remove("msg-queued", "msg-queued-important");
        delete el.dataset.msgId;
        el.removeAttribute("role");
        el.removeAttribute("aria-label");
        var badge = el.querySelector(".queued-badge");
        if (badge) badge.remove();
        var dismiss = el.querySelector(".queued-dismiss");
        if (dismiss) dismiss.remove();
      });
      if (onIdle) onIdle();
    }

    return {
      addQueuedMessage: addQueuedMessage,
      bind: bind,
      remove: remove,
      onIdleEdge: onIdleEdge,
    };
  }

  root.createQueueController = createQueueController;
})(typeof window !== "undefined" ? window : globalThis);
