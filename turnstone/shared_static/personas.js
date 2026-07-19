/* personas.js — client-side personas data layer.
 *
 * Shared by the creation pickers (the console launcher composer + the
 * standalone new-ws dialog + dashboard quick-create), the saved-list /
 * rail labels, and the Service-Hatch manage shelf.  One fetch path and
 * one name→row map so every creation surface agrees on the same list.
 *
 * The picker feed (GET /v1/api/personas) returns display fields only —
 * name, display_name, description, applies_to_kinds, is_default — and is
 * authenticated but gated by NO persona.* permission: selecting a persona
 * at creation is a user action, authoring lives behind the console admin
 * CRUD.
 *
 * House style mirrors projects.js: the coalescing, fail-open refresh,
 * change-detection fan-out, and bridge machinery come from the shared
 * `makeListCache` core (list_cache.js); this module adds the persona-specific
 * readers (kind-filtered choices, the kind default, name→label resolution).
 * An ES module for module consumers that ALSO installs a
 * `window.TurnstonePersonas` bridge for the classic (non-module) app.js
 * bundles.  All fetches ride the shared cookie-auth `authFetch`.
 */

import { makeListCache } from "./list_cache.js";

const _core = makeListCache({
  url: "/v1/api/personas",
  dataKey: "personas",
  name: "personas",
  keyField: "name",
  fpRow: function (p) {
    return [p.name, p.display_name, p.applies_to_kinds, p.is_default];
  },
});

/**
 * Fetch /v1/api/personas into the cache.  Resolves to the row list and
 * NEVER rejects — a failed fetch leaves the prior cache in place and
 * resolves to it, so a transient error can't blank a half-open picker.
 * Failures are recorded (see {@link personasError}) and warned, rather
 * than silently masqueraded as "no personas".
 */
export function refreshPersonas(callOpts) {
  return _core.refresh(callOpts);
}

/** Cached persona rows (empty array until the first refresh resolves). */
export function getPersonas() {
  return _core.get();
}

/** Whether the first refresh has resolved — lets a reader distinguish
 *  "no personas" (pre-seed database) from "not loaded yet". */
export function personasLoaded() {
  return _core.loaded();
}

/** Status of the last refresh failure — HTTP status, 0 for network/parse
 *  error, null when the last refresh succeeded. */
export function personasError() {
  return _core.error();
}

/** Display label for a persona name, or the raw name when unknown (a
 *  since-archived persona keeps labelling the workstreams stamped with
 *  it), or "" for an empty/pre-persona value. */
export function personaLabel(name) {
  if (!name) return "";
  const p = _core.getByKey(name);
  return (p && p.display_name) || name;
}

/** The default persona row for a workstream kind, or null (pre-seed DB). */
export function defaultPersona(kind) {
  for (const p of _core.get()) {
    if (p.is_default && (p.applies_to_kinds || []).indexOf(kind) >= 0) return p;
  }
  return null;
}

/** `{value, text}` choices for a <select>, filtered to a workstream kind
 *  ("interactive" | "coordinator").  The kind's default persona sorts
 *  first so a zero-touch composer preselects today's behavior. */
export function personaChoices(kind) {
  const rows = _core.get().filter(function (p) {
    return (p.applies_to_kinds || []).indexOf(kind) >= 0;
  });
  rows.sort(function (a, b) {
    if (a.is_default !== b.is_default) return a.is_default ? -1 : 1;
    return (a.display_name || a.name).localeCompare(b.display_name || b.name);
  });
  return rows.map(function (p) {
    return { value: p.name, text: p.display_name || p.name };
  });
}

/** Subscribe to post-refresh changes (picker repopulate, label refresh).
 *  Idempotent — the same callback is registered at most once. */
export function onPersonasChange(cb) {
  _core.onChange(cb);
}

// Classic (non-module) app.js bundles reach the data layer through this
// global bridge (mirrors window.TurnstoneProjects).
window.TurnstonePersonas = {
  refreshPersonas: refreshPersonas,
  getPersonas: getPersonas,
  personasLoaded: personasLoaded,
  personasError: personasError,
  personaLabel: personaLabel,
  defaultPersona: defaultPersona,
  personaChoices: personaChoices,
  onPersonasChange: onPersonasChange,
};
