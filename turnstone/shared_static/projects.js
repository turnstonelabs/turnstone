/* projects.js — client-side projects data layer.
 *
 * Shared by the creation pickers (the console launcher composer + the
 * standalone new-ws dialog + dashboard quick-create), the rail's
 * group-by-project, and the Service-Hatch manage shelf.  One fetch path
 * and one id→name map, so the rail resolves project names without a
 * per-snapshot backend join and every creation surface agrees on the
 * same list.
 *
 * House style mirrors composer.js / hatch.js: an ES module for the
 * module consumers (rail.js, shell-side) that ALSO installs a
 * `window.TurnstoneProjects` bridge for the classic (non-module)
 * app.js bundles.  All fetches ride the shared cookie-auth `authFetch`.
 */

import { authFetch } from "./auth.js";

let _cache = []; // last-fetched project rows (active, caller-visible)
let _byId = {}; // project_id -> row, for O(1) rail name resolution
let _loaded = false; // has the first refresh attempt completed (ok or failed)?
let _lastError = null; // last failure: HTTP status, 0 for network/parse, null when ok
let _inflight = null; // shared pending refresh so concurrent callers coalesce
let _fingerprint = null; // last fired map signature, for change-detection
let _requireProject = false; // server.require_project advisory; fail-open false
const _subs = []; // () => void, fired after each CHANGED refresh

function _fp(rows) {
  // Cheap signature of the fields subscribers render (id + name + visibility +
  // state) so a refresh returning identical data skips the fan-out (and the
  // rail's O(workstreams) rebuild).  JSON.stringify is collision-proof (it
  // escapes anything inside a project name) and needs no separator chars; an
  // earlier version joined on raw control bytes, which made git see this whole
  // file as binary.
  return JSON.stringify(
    rows.map(function (p) {
      return [p.project_id, p.name, p.visibility, p.state];
    }),
  );
}

function _setCache(rows) {
  _cache = Array.isArray(rows) ? rows : [];
  _byId = {};
  for (const p of _cache) if (p && p.project_id) _byId[p.project_id] = p;
  const firstLoad = !_loaded;
  _loaded = true;
  const fp = _fp(_cache);
  // Fire only when the map actually changed (or on first load) — a redundant
  // refresh (picker re-open, a mutation that no-ops the visible set) shouldn't
  // force every subscriber to rebuild.
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
 * Fetch /v1/api/projects into the cache.  Resolves to the row list and
 * NEVER rejects — a failed or forbidden fetch leaves the prior cache in
 * place and resolves to it, so a transient error can't blank the rail or
 * a half-open picker.  A failure is recorded (see {@link projectsError})
 * and warned, rather than silently masqueraded as an empty project list.
 */
export function refreshProjects() {
  // Coalesce concurrent callers (on console load both mountRail and the launcher
  // init fire this) onto one in-flight request — they share the same promise and
  // _setCache runs once.
  if (_inflight) return _inflight;
  _inflight = authFetch("/v1/api/projects")
    .then(function (r) {
      if (r.ok) return r.json();
      // A non-OK status (403 when the caller lacks the project.read grant, a
      // 5xx, ...) is NOT "you have zero projects": keep the prior cache rather
      // than blanking it, and record the status so the failure is visible
      // instead of looking like an empty list.
      _lastError = r.status;
      // The require_project advisory fails OPEN: a stale-true value would make
      // the composer hide options on a transient error, so reset it to false.
      _requireProject = false;
      console.warn("projects: GET /v1/api/projects -> " + r.status);
      return null;
    })
    .then(function (data) {
      if (data) {
        _lastError = null;
        _requireProject = !!data.require_project;
        _setCache(data.projects || []);
      }
      return _cache;
    })
    .catch(function (e) {
      // Network drop or a non-JSON body — same policy as a non-OK status:
      // preserve the last-known cache, never reject (callers chain a bare
      // .then), and surface the failure.
      _lastError = 0;
      _requireProject = false; // fail-open (see the non-OK branch above)
      console.warn("projects: refresh failed", e);
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

/** Cached project rows (empty array until the first refresh resolves). */
export function getProjects() {
  return _cache;
}

/** Whether the first refresh has resolved — lets a reader distinguish
 *  "no projects" from "not loaded yet" (the rail falls back to the bare
 *  id only when loaded-but-unknown, i.e. a stale/again-removed project). */
export function projectsLoaded() {
  return _loaded;
}

/** Status of the last refresh failure — the HTTP status (e.g. 403 when the
 *  caller lacks the project.read grant) or 0 for a network/parse error; null
 *  when the last refresh succeeded.  Lets a reader tell "loaded, no projects"
 *  apart from "couldn't load projects" without re-fetching. */
export function projectsError() {
  return _lastError;
}

/** Whether this deployment requires new chats to be filed under a project
 *  (server.require_project). ADVISORY only — the create endpoint on the
 *  enforcing node is authoritative. Fails OPEN to false on any refresh failure
 *  so the composer never hides options on a stale-true value; read it only
 *  AFTER {@link refreshProjects} resolves. */
export function requireProject() {
  return _requireProject;
}

/** Display name for a project_id, or "" when unknown (not yet loaded, no
 *  access, or since-removed). */
export function projectName(id) {
  const p = id && _byId[id];
  return p ? p.name || "" : "";
}

/** `{value, text}` choices for a <select> — real projects only.  Callers
 *  seed their own "No project" placeholder (preserved by the composer's
 *  setOptionChoices) and any "+ New project…" sentinel. */
export function projectChoices() {
  return _cache.map(function (p) {
    return { value: p.project_id, text: p.name };
  });
}

/** Subscribe to post-refresh changes (rail re-render, picker repopulate).
 *  Idempotent — the same callback is registered at most once. */
export function onProjectsChange(cb) {
  if (typeof cb === "function" && _subs.indexOf(cb) < 0) _subs.push(cb);
}

/** Create a project owned by the caller.  Resolves `{ok, status, data}`
 *  (the picker's "+ New project…" path and the manage shelf both use it). */
export function createProject(name, visibility) {
  return authFetch("/v1/api/projects", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name: name, visibility: visibility || "private" }),
  }).then(function (r) {
    return r.json().then(function (data) {
      return { ok: r.ok, status: r.status, data: data };
    });
  });
}

// Classic (non-module) app.js bundles reach the data layer through this
// global bridge (mirrors window.Composer / window.TurnstoneHatch).
window.TurnstoneProjects = {
  refreshProjects: refreshProjects,
  getProjects: getProjects,
  projectsLoaded: projectsLoaded,
  projectsError: projectsError,
  requireProject: requireProject,
  projectName: projectName,
  projectChoices: projectChoices,
  onProjectsChange: onProjectsChange,
  createProject: createProject,
};
