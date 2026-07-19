/* list_cache.js — shared client-side list-cache core.
 *
 * The four creation-surface data layers (projects.js, personas.js,
 * models.js, skills.js) all fetch a single list from a `/v1/api/*`
 * endpoint into a warm client cache that the composers read
 * SYNCHRONOUSLY to paint their pickers without a network round-trip,
 * then refresh-and-repaint.  They shared ~70% of their body verbatim —
 * the coalescing, the fail-open refresh, the change-detection fan-out,
 * and the `window.Turnstone*` bridge — so that machinery lives here once
 * and each module layers its own bespoke readers (kind-filtered choices,
 * the require_project advisory, default-alias resolution, ...) on top.
 *
 * This is an INTERNAL helper module — it installs no window bridge and is
 * imported (never `<script>`-tagged) by the four data layers.  All fetches
 * ride the shared cookie-auth `authFetch`.
 */

import { authFetch } from "./auth.js";

/**
 * Build a coalesced, fail-open list cache over `opts.url`.
 *
 * @param {object} opts
 * @param {string}  opts.url            endpoint to GET (e.g. "/v1/api/projects")
 * @param {string}  opts.dataKey        response field holding the row array
 *                                      (e.g. "projects" -> `data.projects`)
 * @param {string}  opts.name           short label for console.warn on failure
 * @param {string} [opts.keyField]      row field to index by for O(1) lookup
 *                                      (`getByKey`); omit for no index map
 * @param {(row:object)=>any} opts.fpRow  per-row fingerprint tuple — the fields
 *                                      subscribers render, so a refresh that
 *                                      returns identical data skips the fan-out
 * @param {(extra:object)=>any} [opts.fpExtra]  extra top-level fields to fold
 *                                      into the fingerprint (e.g. models' default
 *                                      aliases, so a default-only change still
 *                                      fires onChange).  Omit to fingerprint rows
 *                                      only (a `captureExtra` value that changes
 *                                      without a row change then does NOT fire).
 * @param {(data:object)=>object} [opts.captureExtra]  pull extra top-level
 *                                      response fields into cache state on a
 *                                      successful refresh (returns the new
 *                                      `extra` object, replacing the prior one)
 * @param {object} [opts.extraDefaults] the fail-open `extra` value — installed
 *                                      at init AND restored on every refresh
 *                                      failure, so a stale-true advisory can't
 *                                      survive a transient error
 * @returns {{refresh:Function, get:Function, getByKey:Function,
 *            loaded:Function, error:Function, extra:Function, onChange:Function}}
 */
export function makeListCache(opts) {
  const url = opts.url;
  const dataKey = opts.dataKey;
  const name = opts.name;
  const keyField = opts.keyField || null;
  const fpRow = opts.fpRow;
  const fpExtra = opts.fpExtra || null;
  const captureExtra = opts.captureExtra || null;
  const extraDefaults = opts.extraDefaults || null;

  let _cache = []; // last-fetched rows (caller-visible)
  let _byKey = {}; // keyField value -> row, for O(1) lookup (when keyField set)
  let _loaded = false; // has the first refresh attempt completed (ok or failed)?
  let _lastError = null; // last failure: HTTP status, 0 for network/parse, null when ok
  let _inflight = null; // shared pending refresh so concurrent callers coalesce
  let _fingerprint = null; // last fired signature, for change-detection
  let _extra = extraDefaults ? Object.assign({}, extraDefaults) : {};
  const _subs = []; // () => void, fired after each CHANGED refresh

  function _fp() {
    // Cheap signature of what subscribers render, so a refresh returning
    // identical data skips the fan-out.  JSON.stringify is collision-proof
    // (it escapes anything inside a string field) and needs no separator
    // chars; an earlier per-module version joined on raw control bytes,
    // which made git see the whole file as binary.
    return JSON.stringify([
      _cache.map(fpRow),
      fpExtra ? fpExtra(_extra) : null,
    ]);
  }

  function _setCache(rows) {
    _cache = Array.isArray(rows) ? rows : [];
    if (keyField) {
      _byKey = {};
      for (const r of _cache) if (r && r[keyField]) _byKey[r[keyField]] = r;
    }
    const firstLoad = !_loaded;
    _loaded = true;
    const fp = _fp();
    // Fire only when the signature actually changed (or on first load) — a
    // redundant refresh (picker re-open, a mutation that no-ops the visible
    // set) shouldn't force every subscriber to rebuild.
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

  function refresh() {
    // Coalesce concurrent callers (startup warm + a picker open can both fire
    // this) onto one in-flight request — they share the promise and _setCache
    // runs once.
    if (_inflight) return _inflight;
    _inflight = authFetch(url)
      .then(function (r) {
        if (r.ok) return r.json();
        // A non-OK status (403 when a grant is missing, a 5xx, ...) is NOT "you
        // have zero rows": keep the prior cache rather than blanking it, record
        // the status so the failure is visible, and reset the advisory extra to
        // its fail-open default (a stale-true value must not survive an error).
        _lastError = r.status;
        if (extraDefaults) _extra = Object.assign({}, extraDefaults);
        console.warn(name + ": GET " + url + " -> " + r.status);
        return null;
      })
      .then(function (data) {
        if (data) {
          _lastError = null;
          if (captureExtra) _extra = captureExtra(data);
          _setCache(data[dataKey] || []);
        }
        return _cache;
      })
      .catch(function (e) {
        // Network drop or a non-JSON body — same policy as a non-OK status:
        // preserve the last-known cache, never reject (callers chain a bare
        // .then), surface the failure, fail-open the extra.
        _lastError = 0;
        if (extraDefaults) _extra = Object.assign({}, extraDefaults);
        console.warn(name + ": refresh failed", e);
        return _cache;
      })
      .finally(function () {
        // One attempt has completed (ok or not) — lets a reader tell "still
        // loading" from "loaded, genuinely empty" / "load failed".
        _loaded = true;
        _inflight = null;
      });
    return _inflight;
  }

  return {
    refresh: refresh,
    /** Cached rows (empty array until the first refresh resolves). */
    get: function () {
      return _cache;
    },
    /** Row indexed by keyField value, or null (unknown / no keyField). */
    getByKey: function (k) {
      return (k && _byKey[k]) || null;
    },
    /** Whether the first refresh has resolved — distinguishes "empty" from
     *  "not loaded yet". */
    loaded: function () {
      return _loaded;
    },
    /** Last refresh failure status (HTTP status, 0 for network/parse), or null
     *  when the last refresh succeeded. */
    error: function () {
      return _lastError;
    },
    /** The captured extra top-level fields (fail-open defaults until a
     *  successful refresh; reset to those defaults on any failure). */
    extra: function () {
      return _extra;
    },
    /** Subscribe to post-refresh CHANGES.  Idempotent — the same callback is
     *  registered at most once. */
    onChange: function (cb) {
      if (typeof cb === "function" && _subs.indexOf(cb) < 0) _subs.push(cb);
    },
  };
}
