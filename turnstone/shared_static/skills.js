/* skills.js — client-side skill-list data layer.
 *
 * Backs the skill picker on the standalone new-ws modal and the dashboard
 * quick-create (the console launcher has its own skill picker too).  Before
 * this each open re-fetched /v1/api/skills inline, flashing an empty
 * dropdown for the round-trip; this warms a shared cache the composers read
 * synchronously.
 *
 * RAW ROWS, PER-APP LABEL.  The skill label diverges between apps — the ui
 * pickers append a " [MCP]" suffix for MCP-origin skills, the console
 * launcher does not — so this cache exposes the raw rows via
 * {@link getSkills} (`{name, is_default, origin}`) and each populate helper
 * formats its own label.  A single pre-formatted `text` here would silently
 * change one app's labels.
 *
 * House style mirrors projects.js: coalescing / fail-open refresh /
 * change-detection / bridge come from the shared `makeListCache` core;
 * installs a `window.TurnstoneSkills` bridge for the classic app.js bundles.
 *
 * NOTE: unlike models there is no `skills_changed` SSE event, so the cache
 * only re-warms on a picker-open refresh / onLoginSuccess — a skill created
 * elsewhere appears on the next open (same as before this cache existed).
 */

import { makeListCache } from "./list_cache.js";

const _core = makeListCache({
  url: "/v1/api/skills",
  dataKey: "skills",
  name: "skills",
  fpRow: function (s) {
    return [s.name, s.is_default, s.origin];
  },
});

/**
 * Fetch /v1/api/skills into the cache.  Resolves to the row list and NEVER
 * rejects — a failed/forbidden fetch keeps the prior cache (a picker never
 * blanks).  Failures are warned and fail open (see list_cache.js) rather than
 * masqueraded as "no skills".  Pass `{force:true}` to force a fresh fetch that converges to the
 * latest even mid-flight (onLoginSuccess uses this to recover a failed pre-auth
 * warm — skills has no *_changed event to recover otherwise).
 */
export function refreshSkills(callOpts) {
  return _core.refresh(callOpts);
}

/** Cached skill rows (`{name, is_default, origin, ...}`; empty until the
 *  first refresh resolves).  Raw so each app formats its own label. */
export function getSkills() {
  return _core.get();
}

// No onChange subscription is exposed (unlike projects.js / personas.js): the
// skills cache has no live-render consumer — the composers repaint on open.
// The loaded/error readers are omitted for the same no-consumer reason;
// projects.js/personas.js keep theirs as pre-existing public surface.
// Omitted rather than exposed-and-unused.

// Classic (non-module) app.js bundles reach the data layer through this bridge.
window.TurnstoneSkills = {
  refreshSkills: refreshSkills,
  getSkills: getSkills,
};
