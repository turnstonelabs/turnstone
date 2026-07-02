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
 * House style mirrors projects.js: an ES module for module consumers that
 * ALSO installs a `window.TurnstonePersonas` bridge for the classic
 * (non-module) app.js bundles.  All fetches ride the shared cookie-auth
 * `authFetch`.
 */

import { authFetch } from "./auth.js";

let _cache = []; // last-fetched persona rows (enabled only)
let _byName = {}; // name -> row, for O(1) label/default resolution
let _loaded = false; // has the first refresh attempt completed (ok or failed)?
let _lastError = null; // last failure: HTTP status, 0 for network/parse, null when ok
let _inflight = null; // shared pending refresh so concurrent callers coalesce
let _fingerprint = null; // last fired list signature, for change-detection
const _subs = []; // () => void, fired after each CHANGED refresh

function _fp(rows) {
  // Cheap signature of the fields subscribers render, so a refresh
  // returning identical data skips the fan-out (same policy as
  // projects.js — JSON.stringify keeps the file text-safe for git).
  return JSON.stringify(
    rows.map(function (p) {
      return [p.name, p.display_name, p.applies_to_kinds, p.is_default];
    }),
  );
}

function _setCache(rows) {
  _cache = Array.isArray(rows) ? rows : [];
  _byName = {};
  for (const p of _cache) if (p && p.name) _byName[p.name] = p;
  const firstLoad = !_loaded;
  _loaded = true;
  const fp = _fp(_cache);
  if (firstLoad || fp !== _fingerprint) {
    _fingerprint = fp;
    _subs.forEach(function (cb) {
      try {
        cb(_cache);
      } catch (_) {
        /* a subscriber throwing must not abort the rest of the fan-out */
      }
    });
  }
}

/**
 * Fetch /v1/api/personas into the cache.  Resolves to the row list and
 * NEVER rejects — a failed fetch leaves the prior cache in place and
 * resolves to it, so a transient error can't blank a half-open picker.
 * Failures are recorded (see {@link personasError}) and warned, rather
 * than silently masqueraded as "no personas".
 */
export function refreshPersonas() {
  if (_inflight) return _inflight;
  _inflight = authFetch("/v1/api/personas")
    .then(function (r) {
      if (r.ok) return r.json();
      _lastError = r.status;
      console.warn("personas: GET /v1/api/personas -> " + r.status);
      return null;
    })
    .then(function (data) {
      if (data) {
        _lastError = null;
        _setCache(data.personas || []);
      }
      return _cache;
    })
    .catch(function (e) {
      _lastError = 0;
      console.warn("personas: refresh failed", e);
      return _cache;
    })
    .finally(function () {
      _loaded = true;
      _inflight = null;
    });
  return _inflight;
}

/** Cached persona rows (empty array until the first refresh resolves). */
export function getPersonas() {
  return _cache;
}

/** Whether the first refresh has resolved — lets a reader distinguish
 *  "no personas" (pre-seed database) from "not loaded yet". */
export function personasLoaded() {
  return _loaded;
}

/** Status of the last refresh failure — HTTP status, 0 for network/parse
 *  error, null when the last refresh succeeded. */
export function personasError() {
  return _lastError;
}

/** Display label for a persona name, or the raw name when unknown (a
 *  since-archived persona keeps labelling the workstreams stamped with
 *  it), or "" for an empty/pre-persona value. */
export function personaLabel(name) {
  if (!name) return "";
  const p = _byName[name];
  return (p && p.display_name) || name;
}

/** The default persona row for a workstream kind, or null (pre-seed DB). */
export function defaultPersona(kind) {
  for (const p of _cache) {
    if (p.is_default && (p.applies_to_kinds || []).indexOf(kind) >= 0) return p;
  }
  return null;
}

/** `{value, text}` choices for a <select>, filtered to a workstream kind
 *  ("interactive" | "coordinator").  The kind's default persona sorts
 *  first so a zero-touch composer preselects today's behavior. */
export function personaChoices(kind) {
  const rows = _cache.filter(function (p) {
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
  if (typeof cb === "function" && _subs.indexOf(cb) < 0) _subs.push(cb);
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
