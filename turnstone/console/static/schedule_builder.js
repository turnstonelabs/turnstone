/* Schedule "When" builder — the timing control shared by the admin schedule
   shelf and the dashboard launcher's Scheduled kind.

   The cron DSL is compiled, not typed: the segmented Runs control (Daily /
   Weekly / Monthly / Interval / Once / Cron) builds schedule_type /
   cron_expr / at_time, and the NEXT RUNS read-out previews the result
   through the server's croniter (POST /v1/api/admin/schedules/preview) as
   the user edits.  Cron mode is the raw escape hatch with the same live
   read-out.  Storage is unchanged: a saved expression is reverse-parsed
   back into the friendly mode when its shape matches (cronToScheduleMode),
   else the editor opens in Cron mode.

   Markup comes from <template id="schedule-when-template"> — one source for
   every consumer.  Each instance clones it and prefixes every id (plus the
   label / aria references to them, via scopedId) so two builders can share
   a page.  The pure helpers (compileSchedule, describeSchedule,
   cronToScheduleMode, stateFromSaved, scopedId, the time formatters) take
   and return plain values so they run under node without a DOM.

   Classic script: exposes window.TurnstoneScheduleBuilder. */
(function () {
  "use strict";

  const DAY_NAMES = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  const MODES = ["daily", "weekly", "monthly", "interval", "once", "cron"];
  const TEMPLATE_ID = "schedule-when-template";
  const PREVIEW_URL = "/v1/api/admin/schedules/preview";
  const PREVIEW_DEBOUNCE_MS = 250;

  function pad2(n) {
    return n < 10 ? "0" + n : "" + n;
  }

  function byNumber(a, b) {
    return a - b;
  }

  // "HH:MM" -> [h, m].  A blank or malformed value reads as 06:00 (the
  // builder's default) so a cleared <input type=time> never compiles to
  // NaN fields.
  function timeParts(value) {
    const parts = (value || "06:00").split(":");
    return [parseInt(parts[0], 10) || 0, parseInt(parts[1], 10) || 0];
  }

  function hhmm(value) {
    const p = timeParts(value);
    return pad2(p[0]) + ":" + pad2(p[1]);
  }

  // datetime-local gives "YYYY-MM-DDTHH:MM" in browser local time; the
  // server wants an offset-bearing ISO timestamp.  "" when unparseable.
  function localToUtcIso(localDatetimeStr) {
    const d = new Date(localDatetimeStr);
    if (isNaN(d.getTime())) return "";
    return d.toISOString().replace(/\.\d{3}Z$/, "+00:00");
  }

  // The server emits "YYYY-MM-DDTHH:MM:SS" (UTC, no suffix) for next_run and
  // "+00:00" ISO for preview rows and at_time.  A suffix-less value is UTC,
  // never browser-local, so pin it before parsing.
  function parseServerTime(iso) {
    const hasOffset = /(?:Z|[+-]\d{2}:?\d{2})$/.test(iso);
    return new Date(hasOffset ? iso : iso + "Z");
  }

  // Server time -> datetime-local ("YYYY-MM-DDTHH:MM", browser local).
  function utcToLocalDatetime(utcStr) {
    if (!utcStr) return "";
    const d = parseServerTime(utcStr);
    if (isNaN(d.getTime())) return utcStr.slice(0, 16);
    return (
      d.getFullYear() +
      "-" +
      pad2(d.getMonth() + 1) +
      "-" +
      pad2(d.getDate()) +
      "T" +
      pad2(d.getHours()) +
      ":" +
      pad2(d.getMinutes())
    );
  }

  // The browser zone's short name ("EDT", "UTC", "GMT+2"); "" when the
  // platform cannot say.
  function localZoneName(d) {
    try {
      const parts = new Intl.DateTimeFormat(undefined, {
        timeZoneName: "short",
      }).formatToParts(d);
      const part = parts.find(function (p) {
        return p.type === "timeZoneName";
      });
      return part ? part.value : "";
    } catch (_e) {
      return "";
    }
  }

  // A server instant in the operator's local time: "Tue 2026-09-08 02:00
  // EDT".  The raw input when it does not parse.  The cron itself stays in
  // UTC (the scheduler has no zone), so the read-out is where an operator
  // sees when a run actually lands for them.
  function formatLocal(iso) {
    const d = parseServerTime(iso);
    if (isNaN(d.getTime())) return iso;
    const zone = localZoneName(d);
    return (
      DAY_NAMES[d.getDay()] +
      " " +
      d.getFullYear() +
      "-" +
      pad2(d.getMonth() + 1) +
      "-" +
      pad2(d.getDate()) +
      " " +
      pad2(d.getHours()) +
      ":" +
      pad2(d.getMinutes()) +
      (zone ? " " + zone : "")
    );
  }

  // "in 3d" / "in 2h" / "in 15m" / "now"; "" when it does not parse.
  // `nowMs` overrides the clock (tests); Date.now() otherwise.
  function relativeToNow(iso, nowMs) {
    const d = parseServerTime(iso);
    if (isNaN(d.getTime())) return "";
    const now = nowMs == null ? Date.now() : nowMs;
    const mins = Math.round((d.getTime() - now) / 60000);
    if (mins < 1) return "now";
    if (mins < 60) return "in " + mins + "m";
    if (mins < 48 * 60) return "in " + Math.round(mins / 60) + "h";
    return "in " + Math.round(mins / (24 * 60)) + "d";
  }

  // A cron step field cannot exceed its range: "*/90" in the minute field
  // matches minute 0 only (hourly), "0 */30" in the hour field matches hour
  // 0 only (daily) — croniter accepts both.  Every place that decides
  // whether a step is a builder-representable interval goes through
  // intervalStepOk: compileSchedule refuses to CREATE one out of range,
  // cronToScheduleMode refuses to READ one back as Interval (it opens in
  // Cron mode instead, so a saved expression stays editable), and
  // describeSchedule only claims a restart cadence for a step inside it.
  // Saved out-of-range expressions are preserved verbatim, never rewritten
  // or refused — refusing them stranded them.  Uneven steps ("*/7") are
  // cron's own semantics and stay accepted; the read-out says how they
  // restart.  Out-of-range LITERAL fields ("70 99 * * *") need no gate:
  // croniter rejects them, so they can never be stored.
  const INTERVAL_MAX = { minutes: 59, hours: 23 };
  const INTERVAL_FIELD = { minutes: 60, hours: 24 };

  function intervalStepOk(n, unit) {
    return Number.isInteger(n) && n >= 1 && n <= INTERVAL_MAX[unit];
  }

  // A whole number from a numeric input's raw value, else NaN: compile and
  // the read-out parse through here, so a "4.5" or "1e1" the input reports
  // is refused rather than truncated to a cadence the header never named.
  function wholeNumber(value) {
    const n = Number(value);
    return Number.isInteger(n) ? n : NaN;
  }

  // Builder state — plain values mirroring the template's controls.  Every
  // mode keeps its own fields so switching modes never loses an edit.
  // monthlyDom and intervalEvery are numbers from defaultState() /
  // stateFromSaved() and the raw input strings from state(); consumers
  // parse them (compileSchedule does).
  function defaultState() {
    return {
      mode: "daily",
      dailyTime: "06:00",
      weeklyTime: "06:00",
      weeklyDays: [],
      monthlyTime: "06:00",
      monthlyDom: 1,
      intervalEvery: 4,
      intervalUnit: "hours",
      atLocal: "",
      cron: "",
    };
  }

  function cronResult(expr) {
    return { schedule_type: "cron", cron_expr: expr, at_time: "" };
  }

  // Compile builder state down to the wire fields.  Returns
  // {schedule_type, cron_expr, at_time} or {error}.
  function compileSchedule(state) {
    const mode = state.mode;
    let h, m;
    if (mode === "once") {
      if (!state.atLocal) return { error: "Pick a date and time" };
      const at = localToUtcIso(state.atLocal);
      if (!at) return { error: "Pick a date and time" };
      return { schedule_type: "at", cron_expr: "", at_time: at };
    }
    if (mode === "cron") {
      const expr = (state.cron || "").trim();
      if (!expr) return { error: "Cron expression is required" };
      return cronResult(expr);
    }
    if (mode === "daily") {
      [h, m] = timeParts(state.dailyTime);
      return cronResult(m + " " + h + " * * *");
    }
    if (mode === "weekly") {
      const days = (state.weeklyDays || []).slice().sort(byNumber);
      if (!days.length) return { error: "Select at least one day" };
      [h, m] = timeParts(state.weeklyTime);
      return cronResult(m + " " + h + " * * " + days.join(","));
    }
    if (mode === "monthly") {
      const dom = wholeNumber(state.monthlyDom);
      if (!dom || dom < 1 || dom > 31)
        return { error: "Day of month must be 1-31" };
      [h, m] = timeParts(state.monthlyTime);
      return cronResult(m + " " + h + " " + dom + " * *");
    }
    if (mode === "interval") {
      const n = wholeNumber(state.intervalEvery);
      if (!n || n < 1)
        return { error: "Interval must be a whole number, at least 1" };
      const unit = state.intervalUnit === "hours" ? "hours" : "minutes";
      if (!intervalStepOk(n, unit))
        return {
          error:
            (unit === "hours" ? "Hours" : "Minutes") +
            " interval must be 1-" +
            INTERVAL_MAX[unit] +
            (unit === "hours" ? " (Daily runs once a day)" : ""),
        };
      return cronResult(
        unit === "hours" ? "0 */" + n + " * * *" : "*/" + n + " * * * *",
      );
    }
    return { error: "Unknown schedule mode" };
  }

  // Plain-words summary of the state for the read-out header.
  function describeSchedule(state) {
    switch (state.mode) {
      case "daily":
        return "every day at " + hhmm(state.dailyTime) + " UTC";
      case "weekly": {
        const names = (state.weeklyDays || [])
          .slice()
          .sort(byNumber)
          .map(function (d) {
            return DAY_NAMES[d];
          });
        return names.length
          ? "every " +
              names.join(", ") +
              " at " +
              hhmm(state.weeklyTime) +
              " UTC"
          : "no days selected";
      }
      case "monthly":
        return (
          "monthly on day " +
          (wholeNumber(state.monthlyDom) >= 1
            ? wholeNumber(state.monthlyDom)
            : "?") +
          " at " +
          hhmm(state.monthlyTime) +
          " UTC"
        );
      case "interval": {
        const n = wholeNumber(state.intervalEvery);
        const unit = state.intervalUnit === "hours" ? "hours" : "minutes";
        // The phrase names the step compile will use — never the raw text.
        const base = "every " + (n >= 1 ? n : "?") + " " + unit;
        // A step that does not divide its field restarts at the field
        // boundary ("*/7" hours fires 00, 07, 14, 21, then 00): say so.
        if (!intervalStepOk(n, unit) || INTERVAL_FIELD[unit] % n === 0)
          return base;
        return (
          base +
          (unit === "hours"
            ? ", restarting at midnight"
            : ", restarting each hour")
        );
      }
      case "once": {
        const at = state.atLocal ? localToUtcIso(state.atLocal) : "";
        return at ? "once at " + formatLocal(at) : "one time";
      }
      default:
        return "custom cron";
    }
  }

  // Reverse-parse a saved expression into builder state.  Only the exact
  // shapes the builder emits round-trip; anything else opens in Cron mode.
  function cronToScheduleMode(expr) {
    let m = /^(\d{1,2}) (\d{1,2}) \* \* \*$/.exec(expr);
    if (m) return { mode: "daily", h: +m[2], min: +m[1] };
    m = /^(\d{1,2}) (\d{1,2}) \* \* ([0-7](?:,[0-7])*)$/.exec(expr);
    if (m)
      return {
        mode: "weekly",
        h: +m[2],
        min: +m[1],
        days: m[3].split(",").map(function (d) {
          return +d % 7; // cron allows 7 for Sunday
        }),
      };
    m = /^(\d{1,2}) (\d{1,2}) (\d{1,2}) \* \*$/.exec(expr);
    if (m) return { mode: "monthly", h: +m[2], min: +m[1], dom: +m[3] };
    m = /^0 \*\/(\d+) \* \* \*$/.exec(expr);
    if (m && intervalStepOk(+m[1], "hours"))
      return { mode: "interval", every: +m[1], unit: "hours" };
    m = /^\*\/(\d+) \* \* \* \*$/.exec(expr);
    if (m && intervalStepOk(+m[1], "minutes"))
      return { mode: "interval", every: +m[1], unit: "minutes" };
    return { mode: "cron" };
  }

  // Saved timing -> the state a builder shows for it (edit mode).
  function stateFromSaved(scheduleType, cronExpr, atTime) {
    const s = defaultState();
    if (scheduleType === "at") {
      s.mode = "once";
      s.atLocal = utcToLocalDatetime(atTime);
      return s;
    }
    // The raw expression is always carried, whatever mode opens: switching
    // to Cron never shows an empty box for a saved schedule.
    s.cron = cronExpr || "";
    const parsed = cronToScheduleMode(cronExpr || "");
    s.mode = parsed.mode;
    if (parsed.mode === "daily") {
      s.dailyTime = pad2(parsed.h) + ":" + pad2(parsed.min);
    } else if (parsed.mode === "weekly") {
      s.weeklyTime = pad2(parsed.h) + ":" + pad2(parsed.min);
      s.weeklyDays = parsed.days;
    } else if (parsed.mode === "monthly") {
      s.monthlyTime = pad2(parsed.h) + ":" + pad2(parsed.min);
      s.monthlyDom = parsed.dom;
    } else if (parsed.mode === "interval") {
      s.intervalEvery = parsed.every;
      s.intervalUnit = parsed.unit;
    }
    return s;
  }

  // How an unmet compile shows in the read-out: a neutral hint while the
  // mode is untouched (it still needs input), an error once it has been
  // edited or a submit was refused on it; null when it compiles.
  function runsMessageKind(compiled, dirty) {
    if (!compiled.error) return null;
    return dirty ? "err" : "hint";
  }

  // Instance-scope a template id: "when-seg" -> "when1-seg" for the prefix
  // "when1-".  Ids outside the template's vocabulary pass through.
  function scopedId(id, prefix) {
    return id.replace(/^when-/, prefix);
  }

  // ---------------------------------------------------------------------
  // DOM instance
  // ---------------------------------------------------------------------

  let _instances = 0;

  /**
   * Mount a builder into `mount`.  `opts.onChange(compiled)` fires with the
   * compiled wire fields ({schedule_type, cron_expr, at_time} or {error})
   * on every edit and on preview().
   */
  function ScheduleBuilder(mount, opts) {
    if (!(this instanceof ScheduleBuilder))
      return new ScheduleBuilder(mount, opts);
    if (!mount) throw new Error("ScheduleBuilder: mount element required");
    const tpl = document.getElementById(TEMPLATE_ID);
    if (!tpl || !tpl.content)
      throw new Error("ScheduleBuilder: #" + TEMPLATE_ID + " missing");
    this._opts = opts || {};
    this._els = {};
    this._previewTimer = null;
    this._previewSeq = 0;
    this._previewAbort = null; // AbortController of the in-flight request
    // False until the current mode is edited: an untouched mode that does
    // not compile yet (Weekly with no days, Once with no date, an empty
    // Cron) reads as a hint, not an error.  setMode() clears it.
    this._dirty = false;

    // Instance-scope every id (and the label / aria references to them):
    // the template's "when-seg" becomes "when1-seg", "when2-seg", …  The
    // lookups below keep using the template's own names.
    const prefix = "when" + ++_instances + "-";
    const root = document.createElement("div");
    root.className = "when-builder";
    root.appendChild(tpl.content.cloneNode(true));
    const els = this._els;
    root.querySelectorAll("[id]").forEach(function (el) {
      els[el.id] = el;
      el.id = scopedId(el.id, prefix);
    });
    root.querySelectorAll("[for]").forEach(function (el) {
      el.setAttribute("for", scopedId(el.getAttribute("for"), prefix));
    });
    root.querySelectorAll("[aria-labelledby]").forEach(function (el) {
      el.setAttribute(
        "aria-labelledby",
        scopedId(el.getAttribute("aria-labelledby"), prefix),
      );
    });
    mount.appendChild(root);
    this.root = root;
    this._wire();
    this.reset();
  }

  ScheduleBuilder.prototype._wire = function () {
    const self = this;
    const els = this._els;
    els["when-seg"].addEventListener("click", function (e) {
      const b = e.target.closest("button[data-mode]");
      if (!b) return;
      self.setMode(b.getAttribute("data-mode"));
      self.preview();
    });
    els["when-days"].addEventListener("click", function (e) {
      const b = e.target.closest("button[data-day]");
      if (!b) return;
      b.setAttribute(
        "aria-pressed",
        b.getAttribute("aria-pressed") === "true" ? "false" : "true",
      );
      self._edited();
    });
    // Every field the template carries — wired from the clone, not from a
    // list that a new control could be left off.
    this.root.querySelectorAll("input, select").forEach(function (el) {
      el.addEventListener("input", function () {
        self._edited();
      });
      el.addEventListener("change", function () {
        self._edited();
      });
    });
    els["when-unit"].addEventListener("change", function () {
      self._syncIntervalMax();
    });
  };

  // The step input's max follows the unit (the same bound compileSchedule
  // enforces), so the browser's own validation hints before the read-out.
  ScheduleBuilder.prototype._syncIntervalMax = function () {
    const unit = this._els["when-unit"].value === "hours" ? "hours" : "minutes";
    this._els["when-every"].max = INTERVAL_MAX[unit];
  };

  ScheduleBuilder.prototype._edited = function () {
    this._dirty = true;
    this.preview();
  };

  /** Read the controls into a plain state object. */
  ScheduleBuilder.prototype.state = function () {
    const els = this._els;
    const on = els["when-seg"].querySelector('[aria-pressed="true"]');
    const days = [];
    els["when-days"]
      .querySelectorAll('button[aria-pressed="true"]')
      .forEach(function (b) {
        days.push(parseInt(b.getAttribute("data-day"), 10));
      });
    return {
      mode: on ? on.getAttribute("data-mode") : "daily",
      dailyTime: els["when-time-daily"].value,
      weeklyTime: els["when-time-weekly"].value,
      weeklyDays: days,
      monthlyTime: els["when-time-monthly"].value,
      monthlyDom: els["when-dom"].value,
      intervalEvery: els["when-every"].value,
      intervalUnit: els["when-unit"].value,
      atLocal: els["when-at"].value,
      cron: els["when-cron"].value,
    };
  };

  /** Write a plain state object into the controls (no preview). */
  ScheduleBuilder.prototype.setState = function (state) {
    const els = this._els;
    els["when-time-daily"].value = state.dailyTime;
    els["when-time-weekly"].value = state.weeklyTime;
    const days = state.weeklyDays || [];
    els["when-days"].querySelectorAll("button[data-day]").forEach(function (b) {
      const d = parseInt(b.getAttribute("data-day"), 10);
      b.setAttribute("aria-pressed", days.indexOf(d) >= 0 ? "true" : "false");
    });
    els["when-time-monthly"].value = state.monthlyTime;
    els["when-dom"].value = state.monthlyDom;
    els["when-every"].value = state.intervalEvery;
    els["when-unit"].value = state.intervalUnit;
    this._syncIntervalMax();
    els["when-at"].value = state.atLocal;
    els["when-cron"].value = state.cron;
    this.setMode(state.mode);
  };

  ScheduleBuilder.prototype.setMode = function (mode) {
    if (MODES.indexOf(mode) === -1) mode = "cron";
    this._els["when-seg"]
      .querySelectorAll("button[data-mode]")
      .forEach(function (b) {
        b.setAttribute(
          "aria-pressed",
          b.getAttribute("data-mode") === mode ? "true" : "false",
        );
      });
    this.root.querySelectorAll("[data-when-pane]").forEach(function (pane) {
      pane.hidden = pane.getAttribute("data-when-pane") !== mode;
    });
    this._dirty = false;
  };

  /** Back to the defaults (Daily 06:00) with an empty read-out. */
  ScheduleBuilder.prototype.reset = function () {
    this.setState(defaultState());
    this.cancelPreview();
    this._els["when-desc-out"].textContent = "";
    this._renderRuns([]);
  };

  /** Show a saved schedule's timing (edit mode). */
  ScheduleBuilder.prototype.apply = function (scheduleType, cronExpr, atTime) {
    this.setState(stateFromSaved(scheduleType, cronExpr, atTime));
  };

  ScheduleBuilder.prototype.compile = function () {
    return compileSchedule(this.state());
  };

  /**
   * A submit was refused on this builder's state: show the compile error
   * as an error even if the mode was never touched.
   */
  ScheduleBuilder.prototype.showErrors = function () {
    this._dirty = true;
    this.preview();
  };

  /**
   * Void any pending or in-flight preview (the host is hiding the builder,
   * or its state moved on): the sequence bump makes a late response a
   * no-op.
   */
  ScheduleBuilder.prototype.cancelPreview = function () {
    if (this._previewTimer) clearTimeout(this._previewTimer);
    this._previewTimer = null;
    if (this._previewAbort) this._previewAbort.abort();
    this._previewAbort = null;
    this._previewSeq++;
  };

  /**
   * Refresh the read-out: the description and the compiled fields render
   * at once (and reach onChange); the next-runs list arrives from the
   * server preview after a debounce.
   */
  ScheduleBuilder.prototype.preview = function () {
    const self = this;
    const state = this.state();
    const descEl = this._els["when-desc-out"];
    const desc = describeSchedule(state);
    if (descEl.textContent !== desc) descEl.textContent = desc;
    const compiled = compileSchedule(state);
    if (typeof this._opts.onChange === "function")
      this._opts.onChange(compiled);
    this.cancelPreview();
    const seq = this._previewSeq;
    const kind = runsMessageKind(compiled, this._dirty);
    if (kind) {
      this._renderRuns([], compiled.error, kind === "hint");
      return;
    }
    this._previewTimer = setTimeout(function () {
      self._previewTimer = null;
      const fetchFn = window.authFetch;
      if (typeof fetchFn !== "function") {
        self._renderRuns([], "Preview unavailable");
        return;
      }
      const abort =
        typeof AbortController === "function" ? new AbortController() : null;
      self._previewAbort = abort;
      fetchFn(PREVIEW_URL, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(compiled),
        signal: abort ? abort.signal : undefined,
      })
        .then(function (r) {
          return r.json();
        })
        .then(function (data) {
          if (seq !== self._previewSeq) return; // a newer edit superseded us
          self._previewAbort = null;
          if (!data.valid)
            self._renderRuns([], data.error || "Invalid schedule");
          else self._renderRuns(data.next || []);
        })
        .catch(function () {
          // An aborted request was superseded (seq moved on) — silent.
          if (seq === self._previewSeq) {
            self._previewAbort = null;
            self._renderRuns([], "Preview unavailable");
          }
        });
    }, PREVIEW_DEBOUNCE_MS);
  };

  // `asHint` renders the message in the neutral hint style (an untouched
  // mode that still needs input) rather than as an error.
  ScheduleBuilder.prototype._renderRuns = function (runs, errText, asHint) {
    const rows = this._els["when-runs-out"];
    while (rows.firstChild) rows.removeChild(rows.firstChild);
    if (errText) {
      const err = document.createElement("div");
      err.className = asHint ? "hint" : "err";
      err.textContent = errText;
      rows.appendChild(err);
      return;
    }
    runs.forEach(function (iso) {
      const row = document.createElement("div");
      row.className = "readout-row";
      const when = document.createElement("span");
      when.textContent = formatLocal(iso);
      const rel = document.createElement("span");
      rel.className = "rel";
      rel.textContent = relativeToNow(iso);
      row.appendChild(when);
      row.appendChild(rel);
      rows.appendChild(row);
    });
  };

  window.TurnstoneScheduleBuilder = {
    ScheduleBuilder: ScheduleBuilder,
    compileSchedule: compileSchedule,
    describeSchedule: describeSchedule,
    cronToScheduleMode: cronToScheduleMode,
    stateFromSaved: stateFromSaved,
    defaultState: defaultState,
    scopedId: scopedId,
    runsMessageKind: runsMessageKind,
    utcToLocalDatetime: utcToLocalDatetime,
    formatLocal: formatLocal,
    relativeToNow: relativeToNow,
  };
})();
