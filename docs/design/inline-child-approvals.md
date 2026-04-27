# Inline Child Approvals — Coordinator Tree UI

**Status**: Proposal — no implementation has started.
**Author / owner**: TBD.
**Frozen against**: `main` at `b8e51fa` (1.5.0aN). Line refs below are at this snapshot; re-verify before implementation.

## Problem

A coordinator with N children-in-`state=attention` produces an unworkable
UX today: the operator must click into each child workstream individually
to learn what tool is awaiting approval and resolve it. The motivating
screenshot showed 10 `uptime-node-N` children, each `state=attention
tokens=~6320 ~ approval`, all wanting to approve the same `bash` call
the coord just dispatched. Inline approve/deny + a judge-verdict pill
on each row would fix this.

## Architectural verdict on the original scoping

The original recommendation was:

1. New `child_approval_pending` event on the **cluster bus**.
2. Cache the payload in the **collector** alongside `node.workstreams[ws_id]`.
3. Surface inline on `GET /v1/api/workstreams/{ws_id}/children`.
4. New `child_approval_resolved` event so siblings re-render without re-fetch.

After mapping the surfaces, **layers 1–2 are wrong** and **layer 4 is
unnecessary**. The right architecture is materially smaller:

- **Don't put approval payloads on the cluster bus.** `/v1/api/cluster/events`
  is `read`-scope (any logged-in user). Commit `a46dab1` explicitly chose
  scope-level subscription gating *over* per-event tenant filtering
  ("trusted-team posture") — see `coordinator_adapter.py:88-89, 663-667`.
  Putting approval `tool_name`/`preview` on the cluster bus would
  contradict that decision and would either need a new auth gate (raising
  the whole stream's scope) or per-event filtering (the rejected pattern).
- **Don't materialize the field on the collector row.** Every console-only
  field hung off `node.workstreams[ws_id]` is wiped by `_reconcile_node`
  (`collector.py:447-507`) on every node SSE reconnect, because the
  reconcile assigns `node.workstreams = new_ws` from the snapshot wire,
  which only carries `_CLUSTER_WS_LIVE_KEYS` (`server.py:599-610`). The
  existing pattern is to derive at the read boundary in `_fetch_live_block`
  (`server.py:735-821`, line 819 already derives `pending_approval` from
  `activity_state`).
- **No new SSE events are required.** `child_ws_state` events already
  carry `activity_state` and already arrive on the per-coord SSE
  (`coordinator_adapter._dispatch_child_event`, `coord.js:858-869`,
  handler `handleChildState` at `coord.js:1432-1489`). When `activity_state`
  flips to `"approval"` the JS already paints the existing
  `.badge-attention` pill (`coord.js:1096-1101`); we just need to
  *trigger an immediate live-bulk fetch* on that transition so the row
  picks up the rich payload, and *clear* on the transition out.

The actual missing pieces are:

1. **Data path**: the node's `/v1/api/dashboard` does not currently expose
   `WebUI._pending_approval.items` or the cached LLM judge verdict for
   the pending `call_id`. This is the central server-side change.
2. **Live-bulk passthrough**: `_fetch_live_block` reads dashboard but
   only derives a boolean `pending_approval`. It needs to pass the rich
   payload through.
3. **Trigger latency**: live-bulk runs on a ~5s coalesce. We want
   sub-second from "child enters attention" to "buttons appear", which
   requires a one-line "schedule urgent fetch on `activity_state="approval"`"
   in the JS.
4. **UI**: render the verdict pill + approve/deny buttons in
   `renderChildRow` (`coord.js:1043`); parameterize the existing
   `coordApprove` POST to accept a child ws_id rather than the coord's
   own.
5. **Coord-self parity** (stretch): the same approach works for the
   coord's own pending approvals via `_coordinator_live_snapshot`
   (`server.py:696-732`).

## Answers to the four scoping questions

### Q1. Where does `intent_verdict` actually get cached today?

- `_pending_approval` is a `dict | None` on each `SessionUIBase`
  instance (`session_ui_base.py:76`). Set in `WebUI.approve_tools`
  (`server.py:377-382`) and `ConsoleCoordinatorUI.approve_tools`
  (`coordinator_ui.py:133-138`); cleared after the approval event
  resolves (`server.py:388`, `coordinator_ui.py:142`).
- Per-call_id LLM verdicts are cached separately in
  `SessionUIBase._llm_verdicts: dict[str, dict] = {}` (`session_ui_base.py:130`),
  FIFO-bounded at `_LLM_VERDICT_CACHE_MAX = 50` (`session_ui_base.py:256`),
  inserts at `:271-276` under `_ws_lock`. Persisted via
  `storage.create_intent_verdict` (`session_ui_base.py:296-320`); schema
  at `core/storage/_schema.py:522-545`.
- **`approve_request` does not carry the LLM judge verdict.** It carries
  the *heuristic-tier* verdict inline on each item (optional, only when
  one fired) — `WebUI.approve_tools` at `server.py:241-242` adds
  `entry["verdict"] = item["_heuristic_verdict"]`. The coord build path
  doesn't even attach heuristic verdicts (`coordinator_ui.py:133-138`).
  The LLM judge runs asynchronously on a daemon thread
  (`session.py:3359-3362`) and fires *separately* as an `intent_verdict`
  event via `SessionUIBase.on_intent_verdict` (`session_ui_base.py:277`).
- **No synthesis exists today.** A consumer wanting "approval items + LLM
  judge verdict" must merge by `call_id` themselves. The plan must do
  this synthesis somewhere — proposed location: server-side, inside the
  `/dashboard` projection, so it crosses the wire pre-merged.

### Q2. Is `child_approval_pending` a clean addition to the cluster-bus vocabulary?

Clean in *naming* (no overlap) but **wrong in *layer***. Existing
cluster-bus types (`sdk/events.py:242-398`): `node_joined`, `node_lost`,
`cluster_state`, `ws_created`, `ws_closed`, `ws_rename`, `snapshot`,
`node_snapshot`, `health_changed`, `aggregate`. There is no central
enum — the strings are literals shared between `collector.py` producers
and `events.py` consumer dataclasses, and a new entry would land in
`_CLUSTER_REGISTRY` (`events.py:384-398`).

But the cluster bus is the wrong layer (see "Architectural verdict"
above). If a new event type were strictly required, the right home is
the **per-coord re-emission family** — `child_ws_*` types in
`coordinator_adapter._dispatch_child_event` (`coordinator_adapter.py:635-720`).
Those flow on `/v1/api/workstreams/{coord_ws_id}/events`, gated by
`admin.coordinator`, and parallel the existing `child_ws_created` /
`child_ws_state` / `child_ws_closed` / `child_ws_rename` types
(`coord.js:858-869`).

For this proposal we don't need a new event type at all. `child_ws_state`
with `activity_state="approval"` is the trigger we already have.

### Q3. What does the SSE auth-gate change actually look like?

**It doesn't, under this plan.** We are not putting approval payloads on
`/v1/api/cluster/events`. The data flows on:

- **`/v1/api/cluster/ws/live`** (`server.py:938`, `cluster_ws_live_bulk`),
  already gated by `admin.cluster.inspect`. The bulk live block is the
  natural carrier for `pending_approval_detail`.
- **`/v1/api/workstreams/{coord_ws_id}/events`** (the per-coord SSE),
  already gated by `admin.coordinator`. Existing `child_ws_state` events
  carry `activity_state` and serve as the refresh trigger.
- **`GET /v1/api/workstreams/{coord_ws_id}/children`** (`server.py:2762`),
  already gated by `admin.coordinator`. Optional enrichment site if we
  want richer rows on initial load (decision: **leave to JS**, keep the
  children handler as the durable storage projection it already is).

If we ever did want this on `/cluster/events`, the right move per
codebase precedent is **subscription-level gating** — raise the whole
stream's scope to `admin.cluster.inspect` (matching `cluster_ws_detail`
at `server.py:824`) — *not* per-event filtering. Per-event filtering at
fan-out was the path explicitly rejected by `a46dab1`.

`/cluster/events` traffic shape: ~3-10 events per LLM turn per active
workstream (state transitions; `ws_activity` is filtered out at the
collector boundary at `collector.py:578`); `health_changed` and
`aggregate` are absorbed in-memory and not fanned. At 100 active
workstreams turning ~6s/turn that's ~150-500 events/sec across the bus.
Subscriber queues are `maxsize=2000` (`server.py:1101`); `_fanout`
swallows `queue.Full` (`collector.py:163`).

### Q4. Where is the coord tree UI? Row-template extension cost?

**Path correction.** The user's hypothesis was
`turnstone/console/static/coordinator.js`; actual path is
`turnstone/console/static/coordinator/coordinator.js` (one level deeper).
Host shell at `turnstone/console/static/coordinator/index.html`. No
files under `turnstone/web/static/` or `turnstone/console/static/admin/`.

Row template: `renderChildRow(child)` at `coord.js:1043-1105`. Pure
DOM construction (no `innerHTML` — explicit zero-XSS-surface comment
at `:1041-1042`). Already has per-row state binding via
`row.dataset.wsId` and `.closed`/`.highlight` classes; already paints
the `.badge-attention` pill from `live.pending_approval`
(`:1096-1101`). CSS at `coordinator/index.html:123-179`. Verdict-chip
classes already in scope: `.rec-approve` / `.rec-review` / `.rec-deny`
(`coordinator/index.html:266-280`, used by `applyJudgeVerdictToRow` at
`coord.js:440-466` for the dock chip), and `.risk.{low|med|high|crit}`
from `shared_static/design/primitives/pills.css:179-196`. Auth helpers
already wrapped: `postJSON` / `getJSON` (`coord.js:593-610`) call
`authFetch` (`shared_static/auth.js:39`).

**Extension cost is low.** ~30 lines added to `renderChildRow`, ~10
lines to parameterize `coordApprove` for a non-coord ws_id, ~10 lines
in `handleChildState` to nudge an urgent live-bulk fetch on
`activity_state="approval"`. No new CSS files, no new auth wiring.

The non-trivial gap (flagged in §Q1): the existing
`POST /v1/api/workstreams/{ws_id}/approve` requires a `call_id` and
the `coordApprove` helper at `coord.js:509-530` always sends one. The
plan must therefore make `_pending_approval.items[].call_id` reachable
from the coord — that's exactly what the `pending_approval_detail`
field accomplishes.

## Architecture (revised)

```
WebUI._pending_approval (per-ws-instance, in memory)         server-side, child node
WebUI._llm_verdicts[call_id]   (per-ws, in memory, FIFO 50)  server-side, child node
   │
   ├─[A] pre-merged into /v1/api/dashboard projection         (NEW)
   │           │
   │           ▼
   │    console._fetch_live_block (2s TTL cache)              (PASSTHROUGH change)
   │           │
   │           ▼
   │    /v1/api/cluster/ws/live (admin.cluster.inspect)       (returns enriched live block)
   │           │
   │           ▼
   │    coord.js liveBadgeCache + scheduleLiveFetch           (TRIGGER on activity_state="approval")
   │           │
   │           ▼
   │    renderChildRow → verdict pill + approve/deny buttons  (UI extension)
   │           │
   │           ▼
   │    POST /v1/api/workstreams/{child_ws_id}/approve        (REUSE existing endpoint)
   │
   └─[B] state churn already flows via child_ws_state events
        on per-coord SSE; activity_state="approval" is the trigger,
        activity_state→"" on resolution clears the row.
```

No new event types. No collector schema change. No cluster-bus
auth-gate change. Server changes confined to `/dashboard` projection
and `_fetch_live_block` passthrough.

## Frontend visual design

### Row anatomy

```
┌─ ch-row.attention ─────────────────────────────────────────────────────────────┐
│ bash  [CRIT 0.93]  ws_91a4bb · api-east-03 · ⚖ llm:gpt-5                       │
│                                                                                │
│   Replace live OAuth proxy config and reload Nginx on a production edge.       │
│   Rollback path is not staged.                                                 │
│                                                                                │
│   ↳ judge: sudo install replaces live config with no atomic rollback;          │
│     Nginx reload risks dropping in-flight TLS sessions.    ▸ more              │
│                                                                                │
│   $ sudo install -m 640 oauth.conf /etc/nginx/conf.d/oauth.conf && \           │
│     systemctl reload nginx                                                     │
│                                                              [Deny] [Approve]  │
└────────────────────────────────────────────────────────────────────────────────┘
```

Three vertically stacked blocks inside `.ch-row.attention`:

1. **Header line** — `func_name` (bold) · `[risk_level confidence]` (one
   pill, no separator) · `ws_id` · `node_id` · `tier:judge_model`
   (icon-prefixed `⚖` for `llm`, `⚙` for `heuristic`).
2. **Body** — `intent_summary` (full), then a dimmed `↳ judge:` line
   carrying first ~2 lines of `reasoning` and a `▸ more` toggle, then
   the `preview` (monospace, the func_args being approved).
3. **Action row** — `[Deny]` `[Approve]`, right-aligned. Buttons use
   the existing `.k-btn` primitive plus `.k-btn-deny` (red outline)
   and `.k-btn-approve` (green fill). Disabled with spinner during the
   in-flight POST.

### Always shown vs disclosure

| Field | Always shown | Behind ▸ more |
|---|---|---|
| `func_name`, `risk_level`, `confidence`, `ws_id`, `node_id`, `tier`, `judge_model` | ✓ | |
| `intent_summary` | ✓ (full) | |
| `reasoning` first ~2 lines | ✓ (dimmed `↳ judge:`) | |
| `reasoning` remainder | | ✓ |
| `evidence` (bullets) | | ✓ |
| `preview` (func_args) | ✓ (monospace, max 4 lines) | overflow → ▸ more |
| `verdict_id`, `latency_ms` | | ✓ (footer line) |
| approve/deny buttons | ✓ | |

### Auto-expand rule

`▸ more` is open by default when **any** of:

- `risk_level ∈ {high, crit}`
- `recommendation == "deny"`
- `preview` exceeds 4 lines (truncation tail)

Otherwise default-collapsed. Operator's local toggle is sticky for the
session but does not persist across reload (no localStorage).

### Edge-case matrix

| State | Header pill | Reasoning line | Buttons |
|---|---|---|---|
| LLM judge complete, `recommendation=approve` | `[LOW conf]` green | `↳ judge: …` | both enabled |
| LLM judge complete, `recommendation=review` | `[MED conf]` amber | `↳ judge: …` (auto-expanded) | both enabled |
| LLM judge complete, `recommendation=deny` | `[HIGH conf]` or `[CRIT conf]` red | `↳ judge: …` (auto-expanded) | Deny visually emphasized |
| LLM judge running (`judge_pending=true`) | `⏳ judge running…` | placeholder | both enabled but de-emphasized |
| Heuristic-only (no LLM tier) | `[risk_level]` neutral | none (heuristic carries no `reasoning`) | both enabled |
| LLM judge failed / unavailable | `(judge unavailable)` neutral | none | both enabled |
| Multi-item `_pending_approval` (N>1) | header summarizes (`bash + 2 more` or comma-list ≤2 names) and pill takes **max severity across items** | first item inline; `▸ N more tools` disclosure stacks items 2..N each with their own intent_summary + preview + tier badge | **one envelope-level pair** — clicking resolves the whole envelope per server semantics. No per-item buttons (would be a false affordance). |
| Tool policy `deny` (item.error set, item.needs_approval=False) | `[POLICY-BLOCKED]` red | `policy: '{name}' blocked` | buttons hidden — child will fail this call regardless |
| Tool policy `allow` | item silently dropped from items[] | n/a | n/a |
| Post-resolution race (server cleared `_pending_approval` between live-bulk fetches) | last-known | dimmed | buttons disabled with "(resolved elsewhere)" tooltip |

### Layout details and small push-backs on the mock

- **Group `confidence` with the risk pill**, not floating right. Combined
  pill `[CRIT 0.93]` keeps the trust signal in one place and frees the
  right edge for the action buttons alone (less eye-jump on a row with
  10+ siblings).
- **Tier as trust badge, not metadata.** `⚖ llm:gpt-5` reads as "judged
  by gpt-5" rather than `llm · gpt-5` which reads as a pair of tags.
- **Preview is always func_args**, not evidence. The bullet-list shape
  in the mock's second row is judge `evidence` — promote it to the
  `▸ more` panel and keep the preview block as the literal call args
  (e.g., `{"soql": "SELECT *…", "scope": "all"}` for a SOQL call).
  Rationale: the question "what am I approving?" must never be
  confused with "why did the judge flag it?".
- **Risk pill colors** reuse the design-system primitives at
  `shared_static/design/primitives/pills.css:179-196` (`.risk.low /
  .risk.med / .risk.high / .risk.crit`). Recommendation chips
  (`.rec-approve / .rec-review / .rec-deny`) are reserved for the
  recommendation footer in `▸ more`, not the header pill.

### Mobile (<700px)

The row already collapses to flex-column inside the accordion sidebar
(`coordinator/index.html:351-367`). On narrow widths:

- Header line wraps to two rows (func_name + pill on row 1; ws_id +
  node_id + tier on row 2).
- Buttons stack horizontally as a third top-level child, full-width
  with min-height 44px (touch target).
- `▸ more` content slides down full-width below the preview.

### Out of scope for v1

- **Bulk approve / "approve all matching"** — even with inline buttons,
  10 identical bash calls is N clicks. A "select all → approve"
  affordance is a separate proposal.
- **Keyboard shortcuts** (j/k navigation, a/d approve/deny) — UX win
  but ortho­gonal.
- **Confirm-on-deny modal** — operator might want a "type the
  func_name to confirm" interstitial for `risk_level=crit` denials of
  long-running jobs. Defer.
- **Always-trust toggle inline.** The existing `coordApprove(approved,
  always, call_id)` carries an `always` flag (whitelists the tool for
  the rest of the session — see `core/session_routes.py:702-714`).
  Surface this as a checkbox inside the `▸ more` panel, not on the
  primary button. v1 hardcodes `always=false`; expose toggle in v1.5
  if asked for.

## Implementation plan

The work splits cleanly into six chunks, each independently
mergeable. Chunks 1–4 are required; 5–6 are stretch.

### Chunk 1 — Server: extend `/v1/api/dashboard` projection (server-side, child node)

Files: `turnstone/server.py` (the `dashboard` endpoint body around
line 1114). Deliberately **not** touching `_build_node_snapshot`
(`server.py:990-1041`) — that projection feeds `/v1/api/events/global`,
which the cluster collector consumes onto the cluster bus. Putting
the rich approval payload there would land it on the read-scoped
`/v1/api/cluster/events` stream, contradicting the §Q3 decision to
keep it admin-cluster-inspect gated.

Add a new optional field on each per-ws row in the dashboard response:

```python
"pending_approval_detail": {
    "call_id": str,                        # the primary call_id from _pending_approval
    "judge_pending": bool,
    "items": [                             # serialized from _pending_approval["items"]
        {
            "call_id": str,
            "header": str,
            "preview": str,
            "func_name": str,
            "approval_label": str,
            "needs_approval": bool,
            "heuristic_verdict": dict | None,  # the existing inline heuristic, renamed for clarity
            "judge_verdict": dict | None,      # NEW: looked up from _llm_verdicts.get(call_id)
        },
    ],
} | None
```

Where `judge_verdict` is the dict shape from
`SessionUIBase._llm_verdicts[call_id]` (matches
`sdk/events.py:174-190` `IntentVerdictEvent` shape — `risk_level`,
`confidence`, `recommendation`, `reasoning`, `tier`, `judge_model`,
`verdict_id`, `latency_ms`).

Implementation notes:

- `_pending_approval` is read under no explicit lock today
  (assignments are atomic; reads are best-effort by design — see
  `server.py:1422` cancel-forensics for prior art). Read it the same
  way here.
- `_llm_verdicts` reads should take `_ws_lock` (see write path at
  `session_ui_base.py:267-276`). Keep the lock window tiny — copy the
  one entry by `call_id`, drop the lock.
- Field defaults to `None` when no approval is pending. `_NodeDashboardCache`
  (`console/server.py:613-693`, 2s TTL) caches the response unchanged;
  no changes needed there.
- Do **not** mirror this onto the cluster bus's `cluster_state` payload
  — it already carries assistant `content`, so adding more sensitive
  data widens the read-scope leak. Dashboard is fetched by
  `admin.cluster.inspect`-gated callers only.

Tests:

- `tests/test_dashboard.py` (or equivalent) — add a test that sets
  `_pending_approval` + `_llm_verdicts[call_id]` on a `WebUI` and asserts
  `pending_approval_detail` in the response; clears it after
  `resolve_approval(approved=True, "")`.
- Cover the no-approval (default-None) case explicitly.
- Cover the judge-pending-no-verdict case (`judge_pending=True`,
  `items[].judge_verdict is None`).

### Chunk 2 — Console: pass through `pending_approval_detail` in live block

Files: `turnstone/console/server.py` — `_fetch_live_block` at
`735-821`, `_CLUSTER_WS_LIVE_KEYS` at `599-610`,
`_coordinator_live_snapshot` at `696-732`.

- Add `"pending_approval_detail"` to `_CLUSTER_WS_LIVE_KEYS` (the
  projection allowlist).
- In `_fetch_live_block`, after the existing
  `live["pending_approval"] = live.get("activity_state") == "approval"`
  derivation (line 819), pass through the dashboard's
  `pending_approval_detail` (defaulting to `None`):
  `live["pending_approval_detail"] = upstream.get("pending_approval_detail")`.
- In `_coordinator_live_snapshot` (the path for coord-self rows on the
  console pseudo-node), read the coord UI's `_pending_approval` directly
  and synthesize `pending_approval_detail` the same way (since coord
  rows don't go through `/dashboard`). This is the coord-self parity
  step, optional for v1.

Tests:

- Extend the existing `cluster_ws_live_bulk` integration test (look in
  `tests/test_console_endpoints.py` or `tests/test_cluster_collector.py`)
  to assert the new field round-trips.

### Chunk 3 — Frontend: render verdict pill + approve/deny buttons

Files: `turnstone/console/static/coordinator/coordinator.js`
(`renderChildRow` at `1043-1105`, `coordApprove` at `509-530`,
`scheduleLiveFetch` at `1329-1410`, `handleChildState` at
`1432-1489`); `turnstone/console/static/coordinator/index.html` for
any new minor row-layout CSS.

3a. Parameterize the approve POST.

```js
// coord.js around 509-530 — current
async function coordApprove(approved, always, call_id) {
  await postJSON(`/v1/api/workstreams/${WS_ID}/approve`, {approved, always, call_id});
}

// proposed: split into a generic helper + a coord-self wrapper
async function approveWorkstream(targetWsId, {approved, always, call_id}) {
  await postJSON(`/v1/api/workstreams/${targetWsId}/approve`, {approved, always, call_id});
}
async function coordApprove(approved, always, call_id) {
  await approveWorkstream(WS_ID, {approved, always, call_id});
}
```

The new buttons call `approveWorkstream(child.ws_id, {...})`.

3b. Extend `renderChildRow` to paint:
- a verdict pill when `child.live.pending_approval_detail.items[0].judge_verdict`
  is set, using the existing `.rec-approve|review|deny` chip classes
  (mirror `applyJudgeVerdictToRow` at `coord.js:440-466`);
- approve and deny buttons when `child.live.pending_approval_detail` is
  set, posting via `approveWorkstream(child.ws_id, ...)`. Use
  `.k-btn` (existing primitive) for styling. Disable both buttons
  during the in-flight fetch and re-enable on success/failure.

3c. Trigger urgent live-bulk fetch on `activity_state="approval"`.
In `handleChildState` (`coord.js:1432-1489`), after the existing
state-merge logic, if the new state's `activity_state === "approval"`
and the previous wasn't, call `scheduleLiveFetch(child.ws_id, {urgent: true})`.
Add an `urgent` option to `scheduleLiveFetch` (`coord.js:1329`) that
flushes the batch immediately instead of waiting for the rAF coalesce.
Symmetric clear: when `activity_state` transitions away from
`"approval"`, drop the cached `pending_approval_detail` from the row's
`childrenState` snapshot so the buttons disappear without waiting for
the next live-bulk poll.

3d. Mobile: row already collapses to flex-column at <700px
(`coordinator/index.html:351-367`). Place the buttons as a third
top-level child under `.meta` so they wrap naturally.

Tests:

- `tests/test_coordinator_static.py` (if exists; otherwise add via
  `tests/test_static_smoke.py`) — assert the new helper exists, parses,
  and is reachable from `renderChildRow`.
- Manual: 10-children-pending-bash repro from the motivating
  screenshot. Verify buttons appear ≤500ms after the children enter
  attention, single-row approve clears that row, sibling rows remain
  attentive, and a denied call's row clears with no orphan UI.
- `coord.js` does not have a JS test framework today — add a minimal
  jsdom-based smoke test if the file gains enough surface to warrant
  it; otherwise rely on the existing `tests/test_static_*` pattern of
  asserting strings exist.

### Chunk 4 — Reconnect / replay parity

Files: `turnstone/console/server.py` (`_coord_events_replay` at
`2483-2506`), `coord.js` (the SSE reconnect handler around `654-674`).

- The coord per-ws SSE replay already re-yields `_pending_approval` on
  reconnect for the *coord's own* approvals. For *children*, on SSE
  reconnect the JS already calls `loadChildren({replace: true})`
  (`coord.js:661`), which re-fetches the children list. After that
  fetch, every row not in `state="closed"` should re-trigger a
  live-bulk fetch (it already does this lazily via the
  IntersectionObserver at `coord.js:1137`). **No extra plumbing
  required** for reconnect, but verify the lazy-fetch behavior fires
  promptly enough on reconnect (or force-flush all visible rows once).
- Document the trade: pending-approval-after-reconnect surfaces on the
  next live-bulk poll (target ≤1s once `urgent` is wired into the
  reconnect path), not on the SSE replay itself. This matches the
  existing pattern (live-bulk is the post-reconnect refresh path).

Tests:

- Extend any existing reconnect tests to assert `pending_approval_detail`
  populates after a forced SSE drop+reconnect sequence.

### Chunk 5 — Stretch: coord-self parity

If we want the coord's own pending approvals (when the coord itself
is awaiting input on a tool call, distinct from children) to also
render in this style, mirror the change:

- `_coordinator_live_snapshot` (`server.py:696-732`) reads the coord
  UI's `_pending_approval` directly; have it synthesize
  `pending_approval_detail` the same way Chunk 1 does for interactive
  rows. The coord pre-lift wires no LLM judge today
  (`coordinator_ui.py:138`), so `judge_verdict` will always be `None`
  for coord-self rows; `judge_pending=False`.
- The dock UI (`applyJudgeVerdictToRow` at `coord.js:440-466`) already
  renders coord-self verdicts; this is purely about the children-tree
  view also showing coord-self if such rows ever appear there.

Treat as v2; not in scope for the initial cut.

### Chunk 6 — Stretch: judge-verdict push latency

If the ~5s live-bulk cadence proves too laggy for verdict appearance
(LLM judge fires *after* approval was already shown, and the verdict
takes the next poll cycle to surface):

- Emit a node-level signal when `on_intent_verdict` fires (e.g., reuse
  `_broadcast_state` adjacent path or a one-line addition to
  `_apply_delta`'s `cluster_state` payload — `intent_verdict_call_id`
  field on the activity tick) so the JS can fire `urgent: true` on
  arrival.
- Or relay `intent_verdict` events through `/v1/api/events/global` and
  add a `_apply_delta` handler that produces a coord-side
  `child_intent_verdict` re-emit (the heavyweight path the deep-dives
  describe). This is the only path with sub-second judge-verdict
  appearance and crosses three layers.

Defer until UX feels sluggish.

## File-touch list

Required:

- `turnstone/server.py` — extend `/v1/api/dashboard` projection (Chunk 1).
- `turnstone/console/server.py` — extend `_CLUSTER_WS_LIVE_KEYS` and
  `_fetch_live_block` (Chunk 2). Optionally `_coordinator_live_snapshot`
  for coord-self (Chunk 5).
- `turnstone/console/static/coordinator/coordinator.js` —
  `renderChildRow`, `coordApprove`, `scheduleLiveFetch`,
  `handleChildState` (Chunk 3).
- `turnstone/console/static/coordinator/index.html` — minor row-layout
  CSS for buttons.

Tests:

- `tests/test_dashboard*.py` — pending-approval-detail assertions.
- `tests/test_cluster_collector*.py` or
  `tests/test_console_endpoints*.py` — live-bulk passthrough.
- `tests/test_static_*.py` — coord.js helper smoke tests.
- New: child-approval-flow integration test — spawn a child, force
  it into `state=attention` with a known `call_id`, assert the live
  block exposes `pending_approval_detail.items[0].call_id`, post
  approve, assert resolution.

No SDK changes. No new event-type registry entries. No migrations.

## Pre-implementation verifications

Items below were either confirmed during scoping or remain open.
Confirm the open ones before the chunk that depends on them lands.

| # | Item | Status | Affects |
|---|---|---|---|
| V1 | `make_approve_handler` (`core/session_routes.py:686-716`) does NOT validate body `call_id` against `_pending_approval`. `resolve_approval(approved, feedback)` is called positionally; `call_id` only feeds the `auto_approve_tools` set update. | **CONFIRMED** | risk #1 below — design must guard at the UI |
| V2 | `WebUI.approve_tools` (`server.py:225-298`) can mutate `items[]` mid-call: tool policies flip `denied=true` / `needs_approval=false`, and the serialized list now carries `error` populated with `denial_msg`. | **CONFIRMED** | edge-case matrix row "Tool policy `deny`" |
| V3 | Dashboard projection at `server.py:1114-1173` is built inside one `with ui._ws_lock:` block per ws (`:1128-1133`). The natural extension point: add `_pending_approval`/`_llm_verdicts` reads inside that same lock window. Helper `_serialize_pending_approval_detail(ui)` keeps the lock-held block short. | **CONFIRMED** | Chunk 1 implementation shape |
| V4 | `scheduleLiveFetch` (`coord.js:1329-1359`) uses one `setTimeout(LIVE_BADGE_BULK_FLUSH_MS)` for batching; no urgent path today. Adding `urgent: true` = `clearTimeout(liveBadgeFlushTimer); flushLiveFetches();` after the cache/state/visibility guards. | **CONFIRMED** | Chunk 3c |
| V5 | `LIVE_BADGE_BULK_FLUSH_MS` actual value. | **CLOSED** — SLA not load-bearing for v1; urgent-flush change still applies. |  |
| V6 | `cfg.tenant_check` cross-user behavior. | **CLOSED** — single-user trusted-team posture; coord-operator approving any child is fine. | drops risk |
| V7 | Audit attribution distinguishing inline-coord vs direct-child approve. | **CLOSED** — both paths attribute to the same operator user_id; no need to differentiate. | drops risk |
| V8 | Existing per-child UI multi-item rendering — purely a parity check. Design call for the new row is already made (envelope-level buttons + `▸ N more tools` disclosure, see edge-case matrix). | **OPEN — non-blocking** | visual consistency only; do quick grep during Chunk 3 |
| V9 | `applyJudgeVerdictToRow` (`coord.js:440-466`) — how does it handle multiple verdicts arriving for the same call_id (re-judge on retry)? Same call_id-keyed dedupe needed in the inline pill. | **OPEN** | Chunk 3b |
| V10 | Coord registry retains closed children forever (`coordinator_adapter.py:64-65`). On `child_ws_closed`, does the JS clear the row's `live` cache eagerly, or does the closed row continue to show stale `pending_approval_detail` until its `liveBadgeCache` entry expires? | **OPEN** | risk #6 below |

## Risks and open questions

1. **Stale call_id race — confirmed real.** V1 above: the approve
   endpoint doesn't validate `call_id`, so an operator who clicks
   approve on a row showing call A while the child has rolled over
   to call B will silently approve B. **Action**: guard at the UI
   layer. The new approve POST should include the `call_id` the row
   was rendered with, and the handler should be extended to compare
   against `_pending_approval.get("call_id")`; if they differ, return
   409 with the current call_id so the row re-renders. This is a
   small, isolated server-side change in
   `make_approve_handler`. Treat as part of Chunk 1.

2. **Heuristic vs LLM verdict precedence.** The `items[].verdict`
   field today carries the heuristic verdict; we propose adding a
   sibling `judge_verdict`. If both are set, which does the row
   render? Proposal: prefer LLM if present; fall back to heuristic;
   show neither pill if neither (just buttons). Style: same `.rec-*`
   classes regardless of source, plus a small `tier:llm` /
   `tier:heuristic` micro-label so the operator can tell.

3. **Truncation on dashboard cache.** `_NodeDashboardCache` is 2s TTL
   (`console/server.py:613-693`). If approval transitions are faster
   than 2s (rare in practice), the live-bulk could see stale state.
   Acceptable for v1 — `child_ws_state` events provide the trigger
   layer with sub-second freshness; live-bulk is just the rich-payload
   carrier.

4. **Frontend test coverage gap.** `coord.js` has limited JS
   test coverage today (mostly Python-side string assertions). The
   plan should not block on this gap, but a follow-up to add jsdom
   smoke coverage on the row helper would pay back over time.

5. **Coord judge wiring.** `ConsoleCoordinatorUI.approve_tools`
   hardcodes `judge_pending=False` and the coord LLM judge isn't
   wired (`coordinator_ui.py:138`). Coord-self rows will always show
   buttons-without-pill until the coord judge ships separately.

6. **Closed-children flicker.** The coord registry keeps closed
   children "grayed out" forever (`coordinator_adapter.py:64-65`). A
   row that closes mid-approval should clear its
   `pending_approval_detail` immediately on `child_ws_closed`; verify
   the JS clear-on-close path covers this without explicit logic
   (V10).

7. **Multi-item rendering precedent (non-blocking).** V8 — design call
   is made (envelope-level buttons + `▸ N more tools` disclosure, max
   severity across items) but the existing per-child UI may render
   the same envelope differently. Quick grep during Chunk 3 to
   confirm we're not creating two divergent visual languages for the
   same data.

## Out of scope

- New cluster-bus event types (`child_approval_pending`,
  `child_approval_resolved`).
- Collector schema change to materialize fields on `node.workstreams[ws_id]`.
- Children-handler enrichment with live data (the JS already
  enriches via live-bulk; doubling up on the handler is unnecessary).
- SSE auth-gate changes to `/v1/api/cluster/events`.
- Per-event tenant filtering at fan-out time (codebase has explicitly
  rejected this pattern).
- Bulk approve-all UI affordance — separate proposal; this plan only
  adds *per-row* approve/deny.
