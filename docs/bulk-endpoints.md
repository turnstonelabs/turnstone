# Bulk endpoint shape contract

Turnstone exposes several endpoints and tool calls that take multiple
ids and return a per-id outcome.  Over the last few phases two
**distinct** response shapes have settled, one per semantic category.
This doc codifies both so a future endpoint author can pick the right
shape by semantics instead of by coin-flip.

Existing bulk endpoints at time of writing:

| Endpoint / tool                                         | Category                 | Response shape                          |
|---------------------------------------------------------|--------------------------|------------------------------------------|
| `GET  /v1/api/cluster/ws/live?ids=a,b,c`                | bulk read                | `{results, denied, truncated}`           |
| model tool `spawn_batch`                                 | bulk create (per-item)   | `{results, denied}`                      |
| `POST /v1/api/workstreams/{ws_id}/stop_cascade`          | cascade mutation         | `{cancelled, failed, skipped}`           |
| `POST /v1/api/workstreams/{ws_id}/close_all_children`    | cascade mutation         | `{closed, failed, skipped}`              |

---

## Why two shapes

The ask-to-outcome mapping is fundamentally different between the
two categories, and a one-size-fits-all envelope ends up papering
over distinctions the caller genuinely needs to branch on.

**Bulk read / bulk create-with-payload.**  Each input id (or batch
index) carries a *request-side* concept — "give me the live block
for this ws_id" or "spawn a child with this spec" — and each
successful output carries a *payload* — the live block, or the new
workstream's identifying triple.  The interesting distinction on
failure is *ownership / validation* (caller can't see that id, spec
was malformed) — independent of the storage state.

**Cascade mutation.**  The action is uniform across every id (cancel
this subtree, close this child).  The interesting distinctions on
outcome are *did it reach the terminal state?* (succeeded / already
was there / the dispatch itself failed) — driven by the storage
state plus transport reliability, not by the caller's input.

Trying to unify these forces either:

- a stateless `denied` bucket that has to carry "already gone"
  *and* "you don't have permission" *and* "transport failed" with a
  separate reason string — reviewers end up string-matching to branch.
- or a per-item-payload map for cascade mutations where every
  successful value is the same sentinel — carrier with no payload.

So: two shapes, one per category.  The rest of this doc spells out
each.

---

## Shape A — bulk read / bulk create-with-payload

```json
{
  "results":  { "<key>": <value-or-null>, ... },
  "denied":   [ "<key>", ... ],
  "truncated": false
}
```

**`results`** is a key-indexed map of the positive-path payload.
The key is the input id for read endpoints (`cluster/ws/live` uses
the ws_id), or the input-array index (stringified) for create
endpoints that want ordering preserved (`spawn_batch` uses `"0"`,
`"1"`, ...).  The value is whatever the endpoint produces per
success — a live block, a `{ws_id, name, node_id, status}` triple,
etc.  A `null` value (read endpoints only) means "the id existed and
you own it, but the live block wasn't available" — distinct from
"denied".

**`denied`** is the negative-path list.  For read endpoints it's a
flat list of ids (preserves input order so callers can re-zip
against their input).  For create endpoints with per-item payloads
it's a list of `{idx, reason}` objects (`spawn_batch`'s validation
and spawn-error rows; also the operator-reject surface when per-item
selective-deny ships).  Include every reason that's *not* the
positive path — authz, ownership, validation, already-consumed,
spawn failure — so callers don't branch on status codes.

**`truncated`** is a boolean set to `true` when the server's
per-endpoint input cap was exceeded and the tail was dropped.  The
endpoint docs each spell out the cap (50 for `cluster/ws/live`).
`spawn_batch` hard-errors on overflow instead of silently
truncating — it omits the field entirely rather than carry a
permanently-false flag.

### Example — `cluster/ws/live`

```http
GET /v1/api/cluster/ws/live?ids=a1b2,c3d4,nonexistent,foreign HTTP/1.1
```

```json
{
  "results": {
    "a1b2": {"state": "running", "tokens": 12843, "activity": "..."},
    "c3d4": null
  },
  "denied": ["nonexistent", "foreign"],
  "truncated": false
}
```

Callers that need ordered output zip their original id list against
this map; ids in `denied` drop out of the zip cleanly.  A live-block
`null` doesn't route to `denied` — the row exists and the caller
owns it; the node is just currently unreachable.

### Example — `spawn_batch`

```json
{
  "results": {
    "0": {"ws_id": "d4e5f6...", "name": "csrf-audit", "node_id": "gpu-3", "status": 200},
    "2": {"ws_id": "f1a2b3...", "name": "xss-audit",  "node_id": "gpu-1", "status": 200}
  },
  "denied": [
    {"idx": 1, "reason": "skill not found: nonexistent-skill"}
  ]
}
```

Indexes are stringified to keep the envelope JSON-safe and
consistently-typed across the read and create cases.

---

## Shape B — cascade mutation

```json
{
  "status":       "ok",
  "<bucket>":     [ "<ws_id>", ... ],
  "failed":       [ "<ws_id>", ... ],
  "skipped":      [ "<ws_id>", ... ]
}
```

Where `<bucket>` is the endpoint-specific name for "succeeded" —
`cancelled` for `stop_cascade`, `closed` for `close_all_children`.
The three buckets partition the input set exactly once:

| Bucket        | Meaning                                                                       |
|---------------|-------------------------------------------------------------------------------|
| `<bucket>`    | Action dispatch accepted; target reached the intended terminal state.         |
| `failed`      | Dispatch returned a non-404 error (transport issue, upstream 5xx, exception). |
| `skipped`     | Upstream 404 — stale registry entry, row already deleted, or peer gone.       |

The split between `failed` and `skipped` is load-bearing.  `failed`
is actionable — the operator may want to retry, or the cascade may
be partial.  `skipped` is pre-resolved — the target is already in
the terminal state the cascade was aiming at, so it's neither a
win to report nor a fault to fix.

### Example — `stop_cascade`

```json
{
  "status": "ok",
  "cancelled": ["child-1", "child-3"],
  "failed":    [],
  "skipped":   ["child-2"]
}
```

A subsequent retry would target only `failed` ids, not `skipped`
ones — the latter are already done.

### Example — `close_all_children`

```json
{
  "status": "ok",
  "closed":  ["child-1", "child-3"],
  "failed":  ["child-2"],
  "skipped": []
}
```

Same partition, different success-bucket name.  When `coord_client`
is unavailable (session loaded but no HTTP client attached — a
construction bug) every id goes to `failed` so the operator notices
rather than getting a silent all-skipped response.

---

## Guidance for future bulk endpoints

1. **Pick by semantics, not by "what shape is nearby."**
   - Mutation that's uniform across ids + terminal-state outcome?  →
     **Shape B** (cascade mutation).
   - Read or create where the input id carries payload, or where the
     denial axis is independent of storage state?  →  **Shape A**
     (bulk read / bulk create-with-payload).

2. **Cap the input.**  Both shapes assume a bounded input — the
   server rejects or silently truncates past the cap.  Document the
   cap in the endpoint's OpenAPI description.  Shape A uses
   `truncated: true` on quiet truncation; Shape B hard-errors on
   overflow.

3. **Match existing bucket names for the same semantic.**  Use
   `failed` and `skipped` verbatim in Shape B — the per-endpoint
   success bucket is the only slot that varies.  Use `results` and
   `denied` verbatim in Shape A; the per-endpoint `<key>` /
   `<value>` types vary.

4. **Audit the verbose shape.**  Both endpoints emit a corresponding
   audit event with the full before/after bucket lists — the SSE
   stream and the in-process response give live feedback, but a
   postmortem operator will read the audit row.  Use
   `_emit_coord_audit` (coordinator-scoped) or `record_audit`
   directly; don't inline.

5. **Don't mix shapes within one endpoint.**  If a bulk endpoint
   wants both partial-success creation AND per-item failure reasons
   (like `spawn_batch` with its `{idx, reason}` denial rows), that's
   Shape A with a richer denial element — not a blend with Shape B.

---

## History

- **Phase 6** shipped `cluster/ws/live` as the first Shape A endpoint
  (`{results, denied, truncated}`).
- **Phase 7** shipped `stop_cascade` as the first Shape B endpoint
  (`{cancelled, failed, skipped}`).
- **Phase 8 PR A** shipped `spawn_batch` (Shape A, keyed by idx) and
  `close_all_children` (Shape B, twin of `stop_cascade`), which
  crystallised the two-shape-per-semantic-category policy codified
  here.

Before adding a third shape, read this doc and argue for why the
new surface doesn't fit either A or B.  Two idioms in the cluster
API is a finite operator tax; three is one too many.
