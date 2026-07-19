/* models.js — client-side model-list data layer.
 *
 * Backs the model + judge-model pickers on every creation surface (the
 * standalone new-ws modal, the dashboard quick-create, and the console
 * launcher composer).  Before this the pickers each re-fetched
 * /v1/api/models inline on every open, flashing an empty dropdown for the
 * round-trip; this warms a shared cache the composers read synchronously.
 *
 * TWO SERVER SCHEMAS, ONE CACHE.  /v1/api/models is served by BOTH the node
 * server (ui app) and the console server, with DIFFERENT default-alias
 * fields: the node sends `default_alias` (+ `judge_default_alias`), the
 * console sends `coordinator_default_alias` (+ `judge_default_alias`).  So
 * the cache exposes the raw default-alias fields via {@link modelDefaults}
 * (each "" when the local server didn't send it) and each app reads its own
 * — it must NOT normalize to a single field name.  The per-row alias/model
 * shape is common, so {@link modelChoices} formats one app-agnostic label.
 *
 * House style mirrors projects.js: coalescing / fail-open refresh /
 * change-detection / bridge come from the shared `makeListCache` core;
 * installs a `window.TurnstoneModels` bridge for the classic app.js bundles.
 *
 * NOTE (parallel readers, intentionally NOT unified here): the console
 * governance panel (_sklcModelsPromise), the per-node voice-role fetch, and
 * the admin schedule picker are independent /v1/api/models readers with
 * their own (or no) caching.  This cache is a fourth path scoped to the
 * composer pickers; unifying the others is a separate follow-up.
 */

import { makeListCache } from "./list_cache.js";

const _core = makeListCache({
  url: "/v1/api/models",
  dataKey: "models",
  name: "models",
  // alias is the unique <select> key — index by it so modelLabel() is an O(1)
  // getByKey rather than a per-paint scan.
  keyField: "alias",
  fpRow: function (m) {
    return [m.alias, m.model];
  },
  captureExtra: function (data) {
    return {
      default_alias: data.default_alias || "",
      judge_default_alias: data.judge_default_alias || "",
      coordinator_default_alias: data.coordinator_default_alias || "",
    };
  },
  extraDefaults: {
    default_alias: "",
    judge_default_alias: "",
    coordinator_default_alias: "",
  },
  // The default aliases are a COSMETIC placeholder annotation ("Default — gpt-5"),
  // not a UI-gating advisory, so keep the last-known value through a transient
  // refresh failure instead of blanking it back to an un-annotated "Default
  // model".  A first-load failure still shows all-"" (the seed above).
  resetExtraOnError: false,
});

/**
 * Fetch /v1/api/models into the cache.  Resolves to the row list and NEVER
 * rejects — a failed/forbidden fetch keeps the prior cache (a picker never
 * blanks) AND keeps the last-known default aliases; a first-load failure still
 * shows all-"".  Failures are warned and fail open (see list_cache.js) rather
 * than masqueraded as "no models".  Pass `{force:true}` to force a fresh fetch
 * that converges to the latest server state even mid-flight (the
 * `models_changed` SSE path uses this).
 */
export function refreshModels(callOpts) {
  return _core.refresh(callOpts);
}

/** Cached model rows (empty until the first refresh resolves).  Raw rows so
 *  a caller can resolve a default alias to its "alias (model)" label. */
export function getModels() {
  return _core.get();
}

// The one place the "alias (model)" label (or just "alias" when they coincide)
// is formatted — modelChoices, modelLabel, and every composer placeholder
// annotation resolve through here so the option labels and the "Default — …"
// placeholder can never disagree.
function _fmtLabel(m) {
  return m.alias === m.model ? m.alias : m.alias + " (" + m.model + ")";
}

/** `{value, text}` choices for a model <select> — the label is identical across
 *  all three creation surfaces, so it is centralized here.  Callers seed their
 *  own static "Default …" placeholder as option 0. */
export function modelChoices() {
  return _core.get().map(function (m) {
    return { value: m.alias, text: _fmtLabel(m) };
  });
}

/** The "alias (model)" label for a model alias, or "" when the alias is empty or
 *  unknown — lets a composer annotate its "Default — <resolved>" placeholder
 *  without re-implementing the format.  Resolves via the keyField:"alias" index
 *  (getByKey), O(1). */
export function modelLabel(alias) {
  if (!alias) return "";
  const row = _core.getByKey(alias);
  return row ? _fmtLabel(row) : "";
}

/** The resolved default aliases the local server reported:
 *  `{default_alias, judge_default_alias, coordinator_default_alias}` (each ""
 *  when not sent).  The ui dashboard reads default_alias, the console launcher
 *  reads coordinator_default_alias; both read judge_default_alias.  Keeps the
 *  last-known values on a refresh error (a cosmetic annotation, not a UI gate);
 *  all-"" only before the first successful load. */
export function modelDefaults() {
  return _core.extra();
}

// No onChange subscription is exposed (unlike projects.js / personas.js, whose
// onChange is consumed by rail.js): the models cache has no live-render consumer
// — the console repaints on `models_changed` via its own direct handler, the ui
// composers repaint on open.  The loaded/error readers are omitted for the same
// no-consumer reason; projects.js/personas.js keep theirs as pre-existing
// public surface.  Omitted rather than exposed-and-unused.

// Classic (non-module) app.js bundles reach the data layer through this bridge.
window.TurnstoneModels = {
  refreshModels: refreshModels,
  getModels: getModels,
  modelChoices: modelChoices,
  modelLabel: modelLabel,
  modelDefaults: modelDefaults,
};
