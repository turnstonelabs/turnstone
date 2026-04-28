/* composer_attachments.js — shared paperclip / chip / upload pipeline.
 *
 * Used by both:
 *   - turnstone/ui/static/app.js (interactive Pane)
 *   - turnstone/console/static/coordinator/coordinator.js (coord IIFE)
 *
 * Owns: the in-flight `pendingAttachments` Map (insertion-ordered, so
 * the send-time iteration order reflects user selection rather than
 * upload-completion order) and the chips DOM container. Caller wires
 * the controller's `upload()` into the Composer's `attachments.onAttach`
 * callback.
 *
 * Server contract: POST /v1/api/workstreams/{ws_id}/attachments returns
 * `{attachment_id, filename, size_bytes, mime_type, kind}`. DELETE
 * /v1/api/workstreams/{ws_id}/attachments/{attachment_id} releases the
 * pending row. GET returns `{attachments: [...]}` for rehydrate.
 *
 * Returned controller surface:
 *   upload(file)            — POST a File; renders a placeholder chip
 *                             that swaps to the real id on success.
 *   remove(attachmentId)    — DELETE the chip + server-side row.
 *   clearChips()            — drop all chips + map entries (no DELETE).
 *   rehydrate()             — pull the server-side pending list (page
 *                             reload / tab switch).
 *   snapshot()              — {attachments, attachment_ids} of stable
 *                             chips only (skips in-flight placeholders),
 *                             ready to feed into a /send body.
 *   consume(attached_ids,
 *           dropped_ids?)    — strip chips for ids the server reserved;
 *                             surface a toast if any were dropped.
 *   isEmpty()                — true when no chips are pending.
 */
(function (root) {
  "use strict";

  function formatSize(n) {
    if (n < 1024) return n + " B";
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
    return (n / (1024 * 1024)).toFixed(1) + " MB";
  }

  function _toastError(msg) {
    if (typeof root.toast !== "undefined" && root.toast.error) {
      root.toast.error(msg);
    }
  }

  /**
   * @param {Object} opts
   *   chipsEl: HTMLElement — chips render target (composer.chipsEl).
   *   getWsId: () => string — current workstream id (function so the
   *     interactive pane can swap tabs without re-instantiating).
   *   authFetch: optional override (default window.authFetch).
   *   onError: optional (msg, err) => void — replaces the default toast
   *     for upload failures.
   */
  function createAttachmentController(opts) {
    if (!opts || !opts.chipsEl)
      throw new Error("createAttachmentController: chipsEl required");
    if (typeof opts.getWsId !== "function")
      throw new Error("createAttachmentController: getWsId must be a function");
    var chipsEl = opts.chipsEl;
    var getWsId = opts.getWsId;
    var onError = opts.onError || _toastError;
    // Lazy authFetch lookup — shared/auth.js is loaded before this
    // module in production, but being lazy avoids surprising
    // construction-order failures and keeps the test stub (which
    // defines window.authFetch later) working.
    function _authFetch(url, init) {
      var fn = opts.authFetch || root.authFetch;
      return fn(url, init);
    }
    var pending = new Map();

    function renderChip(info) {
      var chip = document.createElement("span");
      chip.className = "composer-chip composer-chip-" + (info.kind || "other");
      chip.setAttribute("role", "listitem");
      chip.dataset.attachmentId = info.attachment_id;

      var icon = document.createElement("span");
      icon.className = "composer-chip-icon";
      icon.setAttribute("aria-hidden", "true");
      icon.textContent = info.kind === "image" ? "🖼" : "📄";
      chip.appendChild(icon);

      var name = document.createElement("span");
      name.className = "composer-chip-name";
      name.textContent = info.filename || "(unnamed)";
      name.title = info.filename || "";
      chip.appendChild(name);

      var size = document.createElement("span");
      size.className = "composer-chip-size";
      size.textContent = formatSize(info.size_bytes || 0);
      chip.appendChild(size);

      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "composer-chip-remove";
      btn.setAttribute(
        "aria-label",
        "Remove attachment " + (info.filename || ""),
      );
      btn.title = "Remove";
      btn.textContent = "×";
      btn.addEventListener("click", function () {
        remove(info.attachment_id);
      });
      chip.appendChild(btn);

      chipsEl.appendChild(chip);
      return chip;
    }

    function _findChip(id) {
      return chipsEl.querySelector('[data-attachment-id="' + id + '"]');
    }

    function _removeChipDom(id) {
      var chip = _findChip(id);
      if (chip) chip.remove();
    }

    // Replace one Map key with another in place, preserving insertion
    // order. JS Map iteration is insertion-ordered, so naïve
    // `delete + set` would push the entry to the end of the order —
    // breaking the contract that send() iterates chips in user-
    // selection order. Localised here so callers (and the swap path
    // below) don't restate the rationale.
    function _replaceMapKey(map, oldKey, newKey, newVal) {
      var rebuilt = new Map();
      map.forEach(function (val, key) {
        if (key === oldKey) rebuilt.set(newKey, newVal);
        else rebuilt.set(key, val);
      });
      map.clear();
      rebuilt.forEach(function (val, key) {
        map.set(key, val);
      });
    }

    function _swapPlaceholder(placeholderId, info) {
      // If the user removed the placeholder mid-upload (chip + map
      // entry both gone), drop the response — resurrecting a chip the
      // user dismissed would attach an untracked element (not in the
      // map, so coordSend wouldn't include it) and confuse them.
      if (!pending.has(placeholderId)) return;
      _replaceMapKey(pending, placeholderId, info.attachment_id, info);

      var chip = _findChip(placeholderId);
      if (chip) {
        chip.dataset.attachmentId = info.attachment_id;
        var name = chip.querySelector(".composer-chip-name");
        if (name) {
          name.textContent = info.filename || "(unnamed)";
          name.title = info.filename || "";
        }
        var size = chip.querySelector(".composer-chip-size");
        if (size) size.textContent = formatSize(info.size_bytes || 0);
      } else {
        renderChip(info);
      }
    }

    function upload(file) {
      var wsId = getWsId();
      if (!wsId || !file) return;
      var fd = new FormData();
      fd.append("file", file, file.name);

      var placeholderId = "__uploading_" + Date.now() + "_" + Math.random();
      var placeholder = {
        attachment_id: placeholderId,
        filename: file.name,
        size_bytes: file.size,
        mime_type: file.type || "",
        kind: (file.type || "").indexOf("image/") === 0 ? "image" : "text",
        uploading: true,
      };
      pending.set(placeholderId, placeholder);
      renderChip(placeholder);

      _authFetch(
        "/v1/api/workstreams/" + encodeURIComponent(wsId) + "/attachments",
        { method: "POST", credentials: "include", body: fd },
      )
        .then(function (r) {
          return r.json().then(function (body) {
            return { ok: r.ok, status: r.status, body: body };
          });
        })
        .then(function (res) {
          if (!res.ok) {
            _removeChipDom(placeholderId);
            pending.delete(placeholderId);
            onError((res.body && res.body.error) || "Upload failed");
            return;
          }
          _swapPlaceholder(placeholderId, res.body);
        })
        .catch(function (e) {
          _removeChipDom(placeholderId);
          pending.delete(placeholderId);
          if (e && e.message !== "auth") onError("Upload failed", e);
        });
    }

    function remove(attachmentId) {
      var info = pending.get(attachmentId);
      if (!info) return;
      _removeChipDom(attachmentId);
      pending.delete(attachmentId);
      if (info.uploading) return; // no server-side row yet
      var wsId = getWsId();
      if (!wsId) return;
      _authFetch(
        "/v1/api/workstreams/" +
          encodeURIComponent(wsId) +
          "/attachments/" +
          encodeURIComponent(attachmentId),
        { method: "DELETE", credentials: "include" },
      ).catch(function () {
        // Non-fatal — chip is gone client-side; the row will be
        // garbage-collected by the attachment GC sweep.
      });
    }

    function clearChips() {
      pending.clear();
      chipsEl.textContent = "";
    }

    function rehydrate() {
      var wsId = getWsId();
      if (!wsId) return Promise.resolve();
      return _authFetch(
        "/v1/api/workstreams/" + encodeURIComponent(wsId) + "/attachments",
        { method: "GET", credentials: "include" },
      )
        .then(function (r) {
          return r.ok ? r.json() : null;
        })
        .then(function (body) {
          if (!body) return;
          // Tab swap mid-fetch: a stale response would clobber the new
          // tab's chips with the old tab's data. Re-check the live
          // wsId before mutating any DOM.
          if (getWsId() !== wsId) return;
          clearChips();
          (body.attachments || []).forEach(function (a) {
            pending.set(a.attachment_id, a);
            renderChip(a);
          });
        })
        .catch(function () {
          /* non-fatal */
        });
    }

    function snapshot() {
      var attachments = [];
      var ids = [];
      pending.forEach(function (info, id) {
        if (info && !info.uploading) {
          attachments.push(info);
          ids.push(id);
        }
      });
      return { attachments: attachments, attachment_ids: ids };
    }

    function consume(attachedIds, droppedIds) {
      if (Array.isArray(attachedIds)) {
        attachedIds.forEach(function (id) {
          _removeChipDom(id);
          pending.delete(id);
        });
        if (Array.isArray(droppedIds) && droppedIds.length) {
          onError(
            "Some attachments couldn’t be included (" +
              droppedIds.length +
              ") — they’re still in your composer.",
          );
        }
      } else {
        clearChips();
      }
    }

    function isEmpty() {
      return pending.size === 0;
    }

    return {
      upload: upload,
      remove: remove,
      clearChips: clearChips,
      rehydrate: rehydrate,
      snapshot: snapshot,
      consume: consume,
      isEmpty: isEmpty,
    };
  }

  root.createAttachmentController = createAttachmentController;
})(typeof window !== "undefined" ? window : globalThis);
