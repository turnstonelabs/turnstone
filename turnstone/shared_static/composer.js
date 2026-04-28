/* composer.js — shared message-input widget.
 *
 * Used by:
 *   - turnstone/ui/static/app.js     (interactive workstream pane)
 *   - turnstone/console/static/coordinator/coordinator.js  (coordinator session)
 *
 * Owns: textarea, send button, optional stop button, optional attach
 * button + hidden file input + chip container, optional drag/drop and
 * paste-image handling.  Caller decides which features to enable via
 * the constructor options; disabled features render no DOM at all.
 *
 * Event surface (caller-provided callbacks):
 *   onSend(text)               — Enter / Send-button — required
 *   onStop()                   — Stop button click   — required iff stopBtn=true
 *   onAttach(file)             — file picker change OR paste image OR drop
 *
 * Imperative API on the returned instance:
 *   value           — current textarea value (getter / setter)
 *   focus()         — focus the textarea
 *   clear()         — empty the textarea + reset auto-resize
 *   setBusy(b)      — toggle send/queue label, show/hide stop button,
 *                     swap placeholder if `busyPlaceholder` was set
 *   destroy()       — detach event listeners and remove DOM
 *   chipsEl         — the chips container (caller appends attachment
 *                     chips into this).  null when attachments disabled.
 *   inputEl         — the textarea — supported surface for callers that
 *                     need direct access (focus management, value reads,
 *                     extra listeners).
 *   sendBtn / stopBtn — supported surface for the same reason; pages
 *                     mutate textContent / aria-label on cancel.
 *
 * Lifecycle: the constructor mounts DOM into `mount` (an HTMLElement).
 * Drag/drop, when enabled, listens on `dragDrop.targetEl` (typically the
 * whole pane) — broader than the composer itself so users can drop
 * anywhere onto the pane to attach.
 */
(function (root) {
  "use strict";

  var ATTACH_DEFAULT_ACCEPT =
    "image/png,image/jpeg,image/gif,image/webp,text/*," +
    ".md,.py,.js,.ts,.tsx,.jsx,.json,.yaml,.yml,.toml,.html,.css,.sh," +
    ".rs,.go,.java,.c,.cpp,.h,.hpp,.sql,.xml,.ini,.conf";

  var AUTO_RESIZE_MAX_PX = 200;

  function isTouchDevice() {
    return window.matchMedia("(hover: none) and (pointer: coarse)").matches;
  }

  function makeButton(opts) {
    var b = document.createElement("button");
    b.type = "button";
    if (opts.className) b.className = opts.className;
    if (opts.text) b.textContent = opts.text;
    if (opts.ariaLabel) b.setAttribute("aria-label", opts.ariaLabel);
    if (opts.title) b.setAttribute("title", opts.title);
    return b;
  }

  /**
   * @param {HTMLElement} mount — container to receive the composer DOM
   * @param {Object} opts — configuration:
   *   onSend: (text) => void
   *   onStop: () => void
   *   attachments: { onAttach: (file) => void, accept?: string } | null
   *   stopBtn: boolean (default false)
   *   queueWhileBusy: boolean (default false) — busy state shows "Queue"
   *     instead of disabling the send button
   *   autoResize: boolean (default true)
   *   placeholder: string | null (default touch-aware "Type a message…")
   *   busyPlaceholder: string | null — shown on the textarea whenever
   *     setBusy(true) is called.  Defaults to placeholder (no visible
   *     swap) when omitted.
   *   ariaLabel: string (default "Message input")
   *   pasteFiles: boolean (default true if attachments enabled)
   *   dragDrop: { targetEl: HTMLElement, dropClass: string } | null
   *     — dropClass is the CSS class applied to targetEl during a
   *       drag-over; the page must define the corresponding rule.
   *   sendLabel: string (default "Send")
   *   busyLabel: string — label shown on the send button whenever
   *     setBusy(true) is called.  Default is context-sensitive:
   *     "Queue" when queueWhileBusy=true, sendLabel otherwise (no
   *     rotation for non-queue consumers that don't opt in).
   *   stopLabel: string (default "■ Stop")
   *   touchEnterSends: boolean (default false) — when true, Enter sends
   *     even on touch devices (default makes Enter insert a newline on
   *     touch so the on-screen keyboard's Return key behaves as the OS
   *     expects).
   *   rows: integer (default 1) — initial textarea row count; still
   *     auto-resizes upward based on content.  Set to 3 for creation
   *     forms where the user is expected to type a multi-line prompt.
   *   layout: "inline" | "stacked" (default "inline") — "inline" puts
   *     attach/input/send/stop on one row (chat-style composers).
   *     "stacked" puts the textarea on its own row with the attach +
   *     options + send row below (creation-form composers where the
   *     input area is visually dominant).
   *   options: {
   *     fields: [{id, label, type, placeholder?, autocomplete?,
   *               choices?, initial?}],
   *     toggleLabel?: string (default "Options"),
   *     summary?: (values) => string | null,
   *     onChange?: (values) => void,
   *     storageKey?: string,
   *   } | null
   *     — renders a collapsible "Options ▾" panel.  Fields support
   *       type:"input" (text) and type:"select" (dropdown with
   *       {choices: [{value, text}]}).  Any other type is rendered
   *       as a text input.  toggleLabel sets the button text before
   *       the caret; onChange fires whenever a field's change event
   *       fires; summary returns an inline summary string shown beside
   *       the toggle (return "" / null to hide).  getOptionValues()
   *       returns the current {id: value} map; setOptionChoices(id,
   *       choices) populates a select later (for async-loaded lists).
   *       When storageKey is provided the open/closed state persists
   *       to localStorage under that key.
   *   externalDisable: boolean (default false) — when true, setBusy()
   *     still rotates the send button's label / aria / placeholder
   *     + toggles the stop button, but does NOT write sendBtn.disabled.
   *     The caller owns the disabled flag and combines busy state
   *     with any other disable inputs (e.g. a subsystem-down probe)
   *     via its own reconciler function.
   */
  function Composer(mount, opts) {
    if (!(this instanceof Composer)) return new Composer(mount, opts);
    if (!mount) throw new Error("Composer: mount element required");
    opts = opts || {};
    this._opts = opts;
    this._mount = mount;
    this._busy = false;
    this._destroyed = false;
    this._listeners = []; // [{el, type, fn}, ...] for clean detach
    this._autoResizeEnabled = opts.autoResize !== false;

    this._isTouch = isTouchDevice();
    this._buildDom();
    this._wireEvents();
  }

  Composer.prototype._buildDom = function () {
    var opts = this._opts;

    var root = document.createElement("div");
    root.className = "composer";
    var layout = opts.layout === "stacked" ? "stacked" : "inline";
    if (layout === "stacked") root.classList.add("composer--stacked");
    this.el = root;

    // Attachment chips row — always present in the DOM when attachments
    // are enabled (caller may append/remove chips at any time); hidden
    // by CSS until populated.
    if (opts.attachments) {
      this.chipsEl = document.createElement("div");
      this.chipsEl.className = "composer-chips";
      this.chipsEl.setAttribute("role", "list");
      this.chipsEl.setAttribute("aria-label", "Pending attachments");
      root.appendChild(this.chipsEl);
    } else {
      this.chipsEl = null;
    }

    // Stacked layout: textarea on its own row, action row below.
    // Inline layout: textarea shares a row with buttons.
    var textRow;
    var actionRow;
    if (layout === "stacked") {
      textRow = document.createElement("div");
      textRow.className = "composer-text";
      root.appendChild(textRow);
      actionRow = document.createElement("div");
      actionRow.className = "composer-row";
      root.appendChild(actionRow);
    } else {
      actionRow = document.createElement("div");
      actionRow.className = "composer-row";
      root.appendChild(actionRow);
      textRow = actionRow;
    }

    this._buildAttachButton(actionRow, opts);
    this._buildInput(textRow, opts);
    this._buildOptionsToggle(actionRow, opts);
    this._buildSendButton(actionRow, opts);
    this._buildStopButton(actionRow, opts);
    this._buildOptionsPanel(root, opts);

    this._mount.appendChild(root);
  };

  Composer.prototype._buildAttachButton = function (row, opts) {
    if (!opts.attachments) {
      this.attachBtn = null;
      this.attachInput = null;
      return;
    }
    this.attachBtn = makeButton({
      className: "composer-attach",
      text: "\ud83d\udcce",
      ariaLabel: "Attach files",
      title: "Attach files",
    });
    row.appendChild(this.attachBtn);

    this.attachInput = document.createElement("input");
    this.attachInput.type = "file";
    this.attachInput.multiple = true;
    this.attachInput.style.display = "none";
    this.attachInput.accept = opts.attachments.accept || ATTACH_DEFAULT_ACCEPT;
    row.appendChild(this.attachInput);
  };

  Composer.prototype._buildInput = function (row, opts) {
    this.inputEl = document.createElement("textarea");
    this.inputEl.className = "composer-input";
    this.inputEl.rows = opts.rows && opts.rows > 0 ? opts.rows : 1;
    var defaultPlaceholder = this._isTouch
      ? "Type a message\u2026"
      : "Type a message\u2026 (Shift+Enter for newline)";
    this._idlePlaceholder =
      opts.placeholder != null ? opts.placeholder : defaultPlaceholder;
    this._busyPlaceholder = opts.busyPlaceholder || this._idlePlaceholder;
    this.inputEl.placeholder = this._idlePlaceholder;
    this.inputEl.setAttribute("aria-label", opts.ariaLabel || "Message input");
    row.appendChild(this.inputEl);
  };

  Composer.prototype._buildSendButton = function (row, opts) {
    this._sendLabel = opts.sendLabel || "Send";
    // busyLabel default is context-sensitive: "Queue" only makes sense
    // when queueWhileBusy=true (the send button stays clickable during
    // busy to accept queued messages).  Non-queue consumers that omit
    // busyLabel fall back to sendLabel — no label rotation on busy —
    // so the disabled button doesn't misleadingly read "Queue" when
    // queueing isn't actually supported by the caller.
    this._busyLabel =
      opts.busyLabel != null
        ? opts.busyLabel
        : opts.queueWhileBusy
          ? "Queue"
          : this._sendLabel;
    this.sendBtn = makeButton({
      className: "composer-send",
      text: this._sendLabel,
      ariaLabel: this._sendLabel + " message",
    });
    row.appendChild(this.sendBtn);
  };

  Composer.prototype._buildStopButton = function (row, opts) {
    if (!opts.stopBtn) {
      this.stopBtn = null;
      return;
    }
    this._stopLabel = opts.stopLabel || "\u25a0 Stop";
    this.stopBtn = makeButton({
      className: "composer-stop",
      text: this._stopLabel,
      ariaLabel: "Stop generation",
    });
    this.stopBtn.style.display = "none";
    this.stopBtn.disabled = true;
    row.appendChild(this.stopBtn);
  };

  Composer.prototype._buildOptionsToggle = function (row, opts) {
    if (!opts.options) {
      this.optionsBtn = null;
      this.optionsSummaryEl = null;
      return;
    }
    this.optionsBtn = document.createElement("button");
    this.optionsBtn.type = "button";
    this.optionsBtn.className = "composer-options-btn";
    this.optionsBtn.setAttribute("aria-label", "Toggle options");
    this.optionsBtn.setAttribute("title", "Options");
    this.optionsBtn.setAttribute("aria-expanded", "false");
    var labelSpan = document.createElement("span");
    labelSpan.className = "composer-options-btn-label";
    labelSpan.textContent = opts.options.toggleLabel || "Options";
    this.optionsBtn.appendChild(labelSpan);
    var caret = document.createElement("span");
    caret.className = "composer-options-caret";
    caret.setAttribute("aria-hidden", "true");
    caret.textContent = "\u25be"; // ▾
    this.optionsBtn.appendChild(caret);
    row.appendChild(this.optionsBtn);

    this.optionsSummaryEl = document.createElement("span");
    this.optionsSummaryEl.className = "composer-options-summary";
    this.optionsSummaryEl.setAttribute("aria-live", "polite");
    this.optionsSummaryEl.hidden = true;
    row.appendChild(this.optionsSummaryEl);
  };

  Composer.prototype._buildOptionsPanel = function (root, opts) {
    if (!opts.options) {
      this.optionsPanel = null;
      this._optionFields = {};
      return;
    }
    this.optionsPanel = document.createElement("div");
    this.optionsPanel.className = "composer-options-panel";
    this.optionsPanel.hidden = true;
    var panelId = "composer-options-" + Math.random().toString(36).slice(2, 10);
    this.optionsPanel.id = panelId;
    this.optionsBtn.setAttribute("aria-controls", panelId);

    var fields = (opts.options && opts.options.fields) || [];
    this._optionFields = {};
    for (var i = 0; i < fields.length; i++) {
      this._buildOptionField(fields[i], i, panelId);
    }
    root.appendChild(this.optionsPanel);
  };

  Composer.prototype._buildOptionField = function (field, idx, panelId) {
    var rowEl = document.createElement("div");
    rowEl.className = "composer-options-field";

    var lbl = document.createElement("label");
    lbl.textContent = field.label || field.id || "";
    var ctrlId =
      panelId + "-" + (field.id || "f" + idx).replace(/[^a-zA-Z0-9_-]/g, "");
    lbl.htmlFor = ctrlId;
    rowEl.appendChild(lbl);

    var ctrl;
    if (field.type === "select") {
      ctrl = document.createElement("select");
      (field.choices || []).forEach(function (c) {
        var opt = document.createElement("option");
        opt.value = c.value == null ? "" : String(c.value);
        opt.textContent = c.text == null ? opt.value : String(c.text);
        ctrl.appendChild(opt);
      });
    } else {
      ctrl = document.createElement("input");
      ctrl.type = "text";
      if (field.placeholder) ctrl.placeholder = field.placeholder;
      if (field.autocomplete) ctrl.autocomplete = field.autocomplete;
    }
    ctrl.id = ctrlId;
    ctrl.className = "composer-options-control";
    if (field.initial != null) ctrl.value = String(field.initial);
    rowEl.appendChild(ctrl);

    this.optionsPanel.appendChild(rowEl);
    this._optionFields[field.id] = ctrl;
  };

  Composer.prototype._on = function (el, type, fn) {
    el.addEventListener(type, fn);
    this._listeners.push({ el: el, type: type, fn: fn });
  };

  Composer.prototype._wireEvents = function () {
    var self = this;
    var opts = this._opts;
    var pasteFiles =
      opts.pasteFiles != null ? opts.pasteFiles : !!opts.attachments;
    var enterSends = opts.touchEnterSends || !this._isTouch;

    this._on(this.inputEl, "input", function () {
      if (self._autoResizeEnabled) self.autoResize();
    });

    this._on(this.inputEl, "keydown", function (e) {
      // Default: desktop sends on Enter, touch inserts a newline (the
      // on-screen Return key should behave as the OS expects).
      // touchEnterSends=true overrides for callers that want Enter to
      // send on touch too.  isComposing guards against eating Enter
      // mid-IME (CJK input).  The sendBtn.disabled check mirrors the
      // click-path semantics: when the send button is disabled (busy
      // non-queue / subsystem-down), Enter also must not submit, or
      // a rapid double-press before the first POST resolves would
      // create duplicate requests.
      if (
        e.key === "Enter" &&
        !e.shiftKey &&
        !e.isComposing &&
        enterSends &&
        !self.sendBtn.disabled
      ) {
        e.preventDefault();
        self._fireSend();
      }
    });

    if (pasteFiles && opts.attachments) {
      this._on(this.inputEl, "paste", function (e) {
        var items = (e.clipboardData && e.clipboardData.items) || [];
        var uploaded = 0;
        for (var i = 0; i < items.length; i++) {
          var it = items[i];
          if (it.kind === "file") {
            var f = it.getAsFile();
            if (f) {
              opts.attachments.onAttach(f);
              uploaded += 1;
            }
          }
        }
        if (uploaded > 0) e.preventDefault();
      });
    }

    if (this.attachBtn) {
      this._on(this.attachBtn, "click", function () {
        self.attachInput.click();
      });
    }
    if (this.attachInput) {
      this._on(this.attachInput, "change", function (e) {
        var files = Array.from(e.target.files || []);
        files.forEach(function (f) {
          opts.attachments.onAttach(f);
        });
        // Reset so re-selecting the same file still fires change.
        self.attachInput.value = "";
      });
    }

    this._on(this.sendBtn, "click", function () {
      self._fireSend();
    });

    if (this.stopBtn) {
      this._on(this.stopBtn, "click", function () {
        if (typeof opts.onStop === "function") opts.onStop();
      });
    }

    if (this.optionsBtn) {
      this._on(this.optionsBtn, "click", function () {
        self.toggleOptions();
      });
      // Hook change events on each field to refresh the summary + fire
      // caller callback.
      Object.keys(this._optionFields).forEach(function (id) {
        var ctrl = self._optionFields[id];
        self._on(ctrl, "change", function () {
          self._refreshOptionsSummary();
          if (typeof opts.options.onChange === "function") {
            opts.options.onChange(self.getOptionValues());
          }
        });
      });
      // Restore persisted open/closed state if storageKey is set.
      this._restoreOptionsState();
      this._refreshOptionsSummary();
    }

    if (opts.dragDrop) {
      var target = opts.dragDrop.targetEl;
      var dropClass = opts.dragDrop.dropClass;
      if (!dropClass) {
        throw new Error("Composer: dragDrop.dropClass is required");
      }
      var onAttach = opts.attachments && opts.attachments.onAttach;
      if (target && typeof onAttach === "function") {
        this._on(target, "dragover", function (e) {
          if (
            e.dataTransfer &&
            Array.from(e.dataTransfer.types || []).indexOf("Files") !== -1
          ) {
            e.preventDefault();
            target.classList.add(dropClass);
          }
        });
        this._on(target, "dragleave", function (e) {
          // Clear the hover state only when leaving the target entirely;
          // dragleave fires for every child the cursor crosses.  Using
          // relatedTarget lets us check whether the new element is still
          // inside the drop zone.
          var related = e.relatedTarget;
          if (!related || !target.contains(related)) {
            target.classList.remove(dropClass);
          }
        });
        this._on(target, "dragend", function () {
          target.classList.remove(dropClass);
        });
        this._on(target, "drop", function (e) {
          target.classList.remove(dropClass);
          var files = Array.from(
            (e.dataTransfer && e.dataTransfer.files) || [],
          );
          if (files.length > 0) {
            e.preventDefault();
            files.forEach(function (f) {
              onAttach(f);
            });
          }
        });
      }
    }
  };

  Composer.prototype._fireSend = function () {
    var opts = this._opts;
    if (typeof opts.onSend === "function") opts.onSend(this.inputEl.value);
  };

  Composer.prototype.autoResize = function () {
    if (!this._autoResizeEnabled) return;
    this.inputEl.style.height = "auto";
    this.inputEl.style.height =
      Math.min(this.inputEl.scrollHeight, AUTO_RESIZE_MAX_PX) + "px";
  };

  Object.defineProperty(Composer.prototype, "value", {
    get: function () {
      return this.inputEl.value;
    },
    set: function (v) {
      this.inputEl.value = v == null ? "" : String(v);
      this.autoResize();
    },
  });

  Composer.prototype.focus = function () {
    this.inputEl.focus();
  };

  Composer.prototype.clear = function () {
    this.inputEl.value = "";
    this.autoResize();
  };

  // setBusy rotates the send button label between sendLabel and
  // busyLabel, swaps the busy placeholder, and (when stopBtn is
  // configured) toggles the stop button visibility + resets its label
  // to the configured stopLabel on every transition so a
  // cancelGeneration-set "Cancelling…" text doesn't persist into the
  // next busy cycle.
  //
  // The send button's disabled attribute follows this matrix:
  //   externalDisable=true  → composer never writes sendBtn.disabled
  //                           (caller manages it via its own reconciler).
  //   queueWhileBusy=true   → sendBtn.disabled = false (queue stays
  //                           clickable during busy to accept queued sends).
  //   otherwise             → sendBtn.disabled = !!b (disable on busy to
  //                           prevent duplicate-click submits).
  Composer.prototype.setBusy = function (b) {
    this._busy = !!b;
    var opts = this._opts;

    // Label + aria-label rotation — universal so callers can supply a
    // busyLabel regardless of queueWhileBusy mode.
    this.sendBtn.textContent = b ? this._busyLabel : this._sendLabel;
    var queueAriaLabel = b
      ? "Queue message for delivery after current execution"
      : "Send message";
    this.sendBtn.setAttribute(
      "aria-label",
      opts.queueWhileBusy
        ? queueAriaLabel
        : (b ? this._busyLabel : this._sendLabel) + " message",
    );

    // --queue class is queue-specific (the colour flip signals "this
    // button now queues rather than sends").
    this.sendBtn.classList.toggle(
      "composer-send--queue",
      !!(b && opts.queueWhileBusy),
    );
    // Placeholder swap applies whenever busy, not only in queue mode —
    // callers that don't set busyPlaceholder see no visible change
    // because it defaults to idlePlaceholder.
    this.inputEl.placeholder = b
      ? this._busyPlaceholder
      : this._idlePlaceholder;

    // Disabled state — composer owns it unless caller opted out.
    if (!opts.externalDisable) {
      this.sendBtn.disabled = !!b && !opts.queueWhileBusy;
    }

    // Stop button visibility + label reset — reset every transition so
    // cancelGeneration's transient "Cancelling…" label doesn't stick.
    if (this.stopBtn) {
      this.stopBtn.style.display = b ? "" : "none";
      this.stopBtn.disabled = !b;
      this.stopBtn.textContent = this._stopLabel;
      this.stopBtn.setAttribute("aria-label", "Stop generation");
      delete this.stopBtn.dataset.forceCancel;
    }
  };

  // ---------------------------------------------------------------------
  // Options panel — public API.
  //
  // Naming convention: panel-level operations use plural "options"
  // (toggleOptions, setOptionsOpen, getOptionValues), per-field
  // operations use singular "option" (setOptionValue, setOptionChoices,
  // getOptionValue).
  // ---------------------------------------------------------------------

  Composer.prototype.setOptionsOpen = function (open) {
    if (!this.optionsPanel || !this.optionsBtn) return;
    this.optionsPanel.hidden = !open;
    this.optionsBtn.setAttribute("aria-expanded", open ? "true" : "false");
    var storageKey = this._opts.options && this._opts.options.storageKey;
    if (storageKey) {
      try {
        localStorage.setItem(storageKey, open ? "1" : "0");
      } catch (_) {
        /* localStorage unavailable — state persists for this page only. */
      }
    }
  };

  Composer.prototype.toggleOptions = function () {
    if (!this.optionsPanel) return;
    this.setOptionsOpen(this.optionsPanel.hidden);
  };

  Composer.prototype._restoreOptionsState = function () {
    var storageKey = this._opts.options && this._opts.options.storageKey;
    if (!storageKey) return;
    var saved = null;
    try {
      saved = localStorage.getItem(storageKey);
    } catch (_) {
      return;
    }
    if (saved === "1") this.setOptionsOpen(true);
    else if (saved === "0") this.setOptionsOpen(false);
  };

  Composer.prototype.getOptionValues = function () {
    var out = {};
    if (!this._optionFields) return out;
    var keys = Object.keys(this._optionFields);
    for (var i = 0; i < keys.length; i++) {
      var ctrl = this._optionFields[keys[i]];
      out[keys[i]] = ctrl ? ctrl.value : "";
    }
    return out;
  };

  Composer.prototype.getOptionValue = function (id) {
    var ctrl = this._optionFields && this._optionFields[id];
    return ctrl ? ctrl.value : "";
  };

  Composer.prototype.setOptionValue = function (id, value) {
    var ctrl = this._optionFields && this._optionFields[id];
    if (!ctrl) return;
    ctrl.value = value == null ? "" : String(value);
    this._refreshOptionsSummary();
  };

  // Replace a select field's <option> list, preserving the first
  // <option> when one already exists (conventionally the "Default /
  // Use defaults" placeholder the caller seeded at construction time).
  // When the field was constructed without any choices, nothing is
  // preserved — callers get exactly the list they passed in.
  // Used by callers that populate choices asynchronously — e.g. the
  // coordinator creation panel fetches /v1/api/skills and feeds the
  // result here.
  Composer.prototype.setOptionChoices = function (id, choices) {
    var ctrl = this._optionFields && this._optionFields[id];
    if (!ctrl || ctrl.tagName !== "SELECT") return;
    var keep = ctrl.options.length > 0 ? ctrl.options[0] : null;
    if (keep) {
      ctrl.replaceChildren(keep);
    } else {
      ctrl.replaceChildren();
    }
    (choices || []).forEach(function (c) {
      var opt = document.createElement("option");
      opt.value = c.value == null ? "" : String(c.value);
      opt.textContent = c.text == null ? opt.value : String(c.text);
      ctrl.appendChild(opt);
    });
    this._refreshOptionsSummary();
  };

  Composer.prototype._refreshOptionsSummary = function () {
    if (!this.optionsSummaryEl) return;
    var summaryFn = this._opts.options && this._opts.options.summary;
    if (typeof summaryFn !== "function") {
      this.optionsSummaryEl.hidden = true;
      this.optionsSummaryEl.textContent = "";
      return;
    }
    var text = summaryFn(this.getOptionValues());
    if (text) {
      this.optionsSummaryEl.textContent = text;
      this.optionsSummaryEl.hidden = false;
    } else {
      this.optionsSummaryEl.textContent = "";
      this.optionsSummaryEl.hidden = true;
    }
  };

  Composer.prototype.destroy = function () {
    if (this._destroyed) return;
    this._destroyed = true;
    for (var i = 0; i < this._listeners.length; i++) {
      var l = this._listeners[i];
      l.el.removeEventListener(l.type, l.fn);
    }
    this._listeners = [];
    if (this.el && this.el.parentNode) {
      this.el.parentNode.removeChild(this.el);
    }
    // Null out DOM back-references so post-destroy access fails
    // loudly rather than silently mutating detached nodes.
    this.inputEl = null;
    this.sendBtn = null;
    this.stopBtn = null;
    this.chipsEl = null;
    this.attachBtn = null;
    this.attachInput = null;
    this.optionsBtn = null;
    this.optionsPanel = null;
    this.optionsSummaryEl = null;
    this._optionFields = {};
    this.el = null;
  };

  root.Composer = Composer;
})(typeof window !== "undefined" ? window : globalThis);
