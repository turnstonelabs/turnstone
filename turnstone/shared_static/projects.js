/* projects.js — client-side projects data layer.
 *
 * Shared by the creation pickers (the console launcher composer + the
 * standalone new-ws dialog + dashboard quick-create), the rail's
 * group-by-project, and the Service-Hatch manage shelf.  One fetch path
 * and one id→name map, so the rail resolves project names without a
 * per-snapshot backend join and every creation surface agrees on the
 * same list.
 *
 * House style mirrors personas.js / models.js / skills.js: the coalescing,
 * fail-open refresh, change-detection fan-out, and bridge machinery come
 * from the shared `makeListCache` core (list_cache.js); this module adds the
 * projects-specific readers (the require_project advisory, id→name
 * resolution, {value,text} choices) and the `createProject` POST helper.
 * An ES module for module consumers (rail.js, project_creator.js) that ALSO
 * installs a `window.TurnstoneProjects` bridge for the classic (non-module)
 * app.js bundles.  All fetches ride the shared cookie-auth `authFetch`.
 */

import { authFetch } from "./auth.js";
import { makeListCache } from "./list_cache.js";

// The require_project advisory rides the same /v1/api/projects response as an
// extra top-level field.  It fails OPEN to false: a stale-true value would make
// the composer hide options on a transient error, so `extraDefaults` resets it
// on every refresh failure and it seeds false before the first refresh.  It is
// deliberately NOT in the fingerprint (no fpExtra) — a require_project-only
// toggle with an unchanged project list should not force a rail rebuild; the
// composers re-read it synchronously on their next open.
const _core = makeListCache({
  url: "/v1/api/projects",
  dataKey: "projects",
  name: "projects",
  keyField: "project_id",
  fpRow: function (p) {
    return [p.project_id, p.name, p.visibility, p.state];
  },
  captureExtra: function (data) {
    return { requireProject: !!data.require_project };
  },
  extraDefaults: { requireProject: false },
  // require_project GATES the picker (strict mode hides the projectless option),
  // so it must fail OPEN: a failed refresh resets it to false rather than letting
  // a stale-true value hide options.  (Default, but explicit for the contrast
  // with models.js, whose cosmetic extra opts out of the reset.)
  resetExtraOnError: true,
});

/**
 * Fetch /v1/api/projects into the cache.  Resolves to the row list and
 * NEVER rejects — a failed or forbidden fetch leaves the prior cache in
 * place and resolves to it, so a transient error can't blank the rail or
 * a half-open picker.  A failure is recorded (see {@link projectsError})
 * and warned, rather than silently masqueraded as an empty project list.
 */
export function refreshProjects() {
  return _core.refresh();
}

/** Cached project rows (empty array until the first refresh resolves). */
export function getProjects() {
  return _core.get();
}

/** Whether the first refresh has resolved — lets a reader distinguish
 *  "no projects" from "not loaded yet" (the rail falls back to the bare
 *  id only when loaded-but-unknown, i.e. a stale/again-removed project). */
export function projectsLoaded() {
  return _core.loaded();
}

/** Status of the last refresh failure — the HTTP status (e.g. 403 when the
 *  caller lacks the project.read grant) or 0 for a network/parse error; null
 *  when the last refresh succeeded.  Lets a reader tell "loaded, no projects"
 *  apart from "couldn't load projects" without re-fetching. */
export function projectsError() {
  return _core.error();
}

/** Whether this deployment requires new chats to be filed under a project
 *  (server.require_project). ADVISORY only — the create endpoint on the
 *  enforcing node is authoritative. Reads the current cached value
 *  SYNCHRONOUSLY: before the first {@link refreshProjects} resolves it is the
 *  fail-open default false, so a synchronous read is safe — a composer can seed
 *  UI from the warm cache immediately and re-read after a refresh to catch a
 *  change. Fails OPEN to false on any refresh failure too, so the composer never
 *  hides options on a stale-true value. */
export function requireProject() {
  return _core.extra().requireProject;
}

/** Display name for a project_id, or "" when unknown (not yet loaded, no
 *  access, or since-removed). */
export function projectName(id) {
  const p = _core.getByKey(id);
  return p ? p.name || "" : "";
}

/** `{value, text}` choices for a <select> — real projects only.  Callers
 *  seed their own "No project" placeholder (preserved by the composer's
 *  setOptionChoices) and any "+ New project…" sentinel. */
export function projectChoices() {
  return _core.get().map(function (p) {
    return { value: p.project_id, text: p.name };
  });
}

/** Subscribe to post-refresh changes (rail re-render, picker repopulate).
 *  Idempotent — the same callback is registered at most once. */
export function onProjectsChange(cb) {
  _core.onChange(cb);
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
