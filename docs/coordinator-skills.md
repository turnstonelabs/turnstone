# Writing a coordinator-specific skill

Skills are prompt-level personas that steer a Turnstone session
toward a narrow task.  Most skills target **interactive** sessions —
the single-workstream "do this thing" surface where the model wields
`bash`, `edit_file`, `web_fetch`, and the rest of the maker toolset.

A **coordinator skill** is different.  It runs on a session whose job
is to orchestrate other sessions.  The toolset is smaller and
narrower, the persona is an orchestrator instead of a maker, and the
success metric is "did the plan resolve" instead of "did the code
compile".  This doc covers the differences a skill author has to
care about.

---

## The two-surface model

A row in `prompt_templates` carries a `kind` column (see
[`turnstone/core/skill_kind.py`](../turnstone/core/skill_kind.py);
migration 044 added the column).  Three values:

| `SkillKind` enum     | Stored as                   | Visible in                                                                |
|----------------------|-----------------------------|---------------------------------------------------------------------------|
| `SkillKind.INTERACTIVE` | `"interactive"`          | Only the interactive-session activation path.  `list_skills` on a coord won't show it. |
| `SkillKind.COORDINATOR` | `"coordinator"`          | Only the coordinator's `list_skills` tool.  Hidden from interactive activation pickers. |
| `SkillKind.ANY`         | `"any"`                  | Both surfaces.  Default for legacy rows predating the classifier.        |

The `kind` field is a `StrEnum` — drop-in ``str`` compatible — so
DB rows, JSON payloads, and `==` comparisons all work without
translation at the edge.

When a coordinator calls `list_skills`, the SQL filter narrows to
`kind IN ('coordinator', 'any')`.  When an interactive session picks
a skill at activation, the filter narrows to
`kind IN ('interactive', 'any')`.  A skill author tags once at
creation; the two surfaces stay partitioned without any
per-call filtering on the LLM side.

**Tagging a new skill as coordinator-only** — set `kind` to
`SkillKind.COORDINATOR` (or the literal string `"coordinator"`) when
you POST to `/v1/api/admin/skills`.  Existing rows default to
`SkillKind.ANY`; bump them to `COORDINATOR` if you've rewritten the
prompt around the orchestrator toolset.

---

## Tool surface differences

Coordinator sessions receive a **fixed** tool set, defined in
`turnstone/core/tools.py` as `COORDINATOR_TOOLS`.  Nothing a skill
or MCP config can do adds to it.  Current members:

| Tool                      | Category        | Notes                                                               |
|---------------------------|-----------------|---------------------------------------------------------------------|
| `spawn_workstream`        | delegate        | Create one child.  Requires approval.                               |
| `spawn_batch`              | delegate        | Create up to 10 children in one approval.  Partial-success shape.   |
| `inspect_workstream`      | observe         | Read state + tail of one child.  Auto-approved (no mutation).       |
| `list_workstreams`        | observe         | List the direct children (same shape as `/children` endpoint).      |
| `wait_for_workstream`     | block           | Block until one/all listed children hit a terminal state.           |
| `send_to_workstream`      | steer           | Queue a follow-up message to a running child.                        |
| `close_workstream`        | wind-down       | Soft-close one child.  Requires approval.                           |
| `close_all_children`      | wind-down       | Soft-close every direct child in one approval.  Partial-success shape. |
| `cancel_workstream`       | wind-down       | Drop the in-flight generation; leaves child idle for a fresh send.  |
| `delete_workstream`       | wind-down       | Hard-delete one child.  Requires approval.                          |
| `list_nodes`              | discover        | Enumerate live cluster nodes + capabilities.                        |
| `list_skills`             | discover        | Coordinator-visible skills only (SkillKind filter above).           |
| `tasks`                   | plan            | Orchestrator-only scratchpad.  Children don't see it.               |

Explicitly **not** in the coordinator set:

- `bash` / `edit_file` / `write_file` / `append_file` / `diff_file` — no local FS.
- `read_file` / `search` — no local FS reads.
- `web_fetch` / `web_search` — no direct web access.
- `task_agent` / `plan_agent` — sub-agent tools are zeroed on coord sessions.
- `memory` / `recall` / `notify` / `watch` / `read_resource` / `use_prompt` / `skill` — the orchestrator's "memory" is its children's outputs; these UX / persistence tools belong to interactive sessions.

If your skill needs a coordinator to "run a command" or "read a
file", write the delegate pattern instead: spawn a child with an
appropriate skill, `wait_for_workstream`, then `inspect_workstream`
for the output.  The coordinator stays the orchestrator.

---

## Persona differences

Interactive skills compose on top of `base_interactive.md` — a
"maker" persona: get the work done, use the tools, edit the code,
close the loop.

Coordinator skills compose on top of
[`base_coordinator.md`](../turnstone/prompts/base_coordinator.md) —
an "orchestrator" persona: decompose, delegate, monitor, synthesise.
The base text is short but sets the tone every coordinator skill
inherits:

> You are a coordinator on a small, focused infrastructure team.
> Your role is to orchestrate work across the cluster...  You do
> not edit files, run shell commands, browse the web, or manipulate
> the codebase directly.  Children do that.

Write your skill's system prompt to *add* task-specific orchestration
hints on top — don't re-explain the role, don't paste tool JSON,
don't try to override the "no direct action" contract.  Keep the
additions to: (a) the specific kind of work this skill delegates;
(b) the preferred skill tags for children; (c) the synthesis shape
the skill should end on.

---

## `tasks` integration

`tasks` is the coordinator's scratchpad — a persisted, ordered
list of rows with fields `{id, title, status, child_ws_id, created,
updated}` that only this coordinator sees.  Children don't see it;
the user does via the sidebar.  Five actions: `add`, `update`,
`remove`, `reorder`, `list` (only `list` is auto-approved; the
mutators go through the approval flow).

The input schema refers to rows by `task_id`; the persisted row
object exposes the same id as `id`.  The `child_ws_id` field is a
free-form label the skill sets to link a task to a spawned
workstream — it is NOT validated against the workstreams table, so
a skill can set it to a placeholder before `spawn_workstream`
returns or keep it pointing at a closed child for later audit.

A skill's initial prompt can seed the task list by calling
`tasks(action="add", title=...)` as its very first tool calls —
the user gets a visible plan before any child is spawned, and the
coordinator's future self has something concrete to iterate on.
Status transitions (`pending` → `in_progress` → `done` / `blocked`)
are the skill's main feedback loop: mutate the task when the child
covering it finishes, not when the child starts.  Use
`tasks(action="update", task_id=..., child_ws_id=<ws_id>)` to
link a task to the child that owns it once spawn returns.

A final gotcha: parallel tool dispatch does NOT serialise reads
after writes in the same batch.  If a skill issues an `update` and
a `list` in one parallel tool batch, the `list` response may reflect
the pre-update state.  Dispatch mutate and list serially (one
tool_use turn each) when the list must observe the mutation.

Keep the tasks coarse-grained — one per child, roughly.  A 20-task
list for a 3-child fan-out is noise; a 1-task list for a 5-child
fan-out loses the plan.  The sidebar renders tasks as the operator's
mental model of "what the coord thinks it's doing".

---

## Referencing children by `ws_id`

Every ws_id returned by `spawn_workstream` / `spawn_batch` is a
**full 32-char hex string**.  The skill's system prompt must not
invent ws_ids — a model that hallucinates `"child-1"` or `"ws-abc"`
hits the tenant guard in `CoordinatorClient._is_own_subtree`, which
validates ws_id against `parent_ws_id=coord_ws_id` AND
`user_id=owner` in storage.  The rejection shape varies by tool:

- **Mutating ops** (`send_to_workstream`, `close_workstream`,
  `cancel_workstream`, `delete_workstream`) return
  `{"error": "workstream not in coordinator subtree: <ws_id>", "status": 404}`
  — the skill should treat this as a tool error, not an empty result.
- **`inspect_workstream`** returns `{"error": "workstream not found", "ws_id": "<ws_id>"}`
  (same shape as a genuinely missing row, so the guard can't be
  used as an existence oracle).
- **`wait_for_workstream`** reports the offending id with
  `state="denied"` in its `results` dict; `mode="any"` won't
  satisfy on a pure-denied list, so a hallucinated id won't trick
  the wait into reporting "complete".

Pattern: capture each spawn result in the next tool call's input.
The JSON tool-result carries `{"ws_id": "...", "name": "...",
"node_id": "...", "routing_strategy": "..."}`; the model should
extract the ws_id and pass it to `inspect_workstream` /
`wait_for_workstream` / `send_to_workstream` / `close_workstream`
verbatim.

A UI that wants human-readable identifiers should render the `name`
field and keep the ws_id as the click-through key.

---

## `wait_for_workstream` vs `inspect_workstream`

Two distinct semantics, different cost profiles:

- **`wait_for_workstream(ws_ids=[...], timeout=60, mode="any")`** —
  blocks inside a single tool call until one (or all, for `mode="all"`)
  of the listed children reaches a terminal state (`idle`, `error`,
  `closed`, `deleted`).  The worker thread blocks up to `timeout`
  seconds; the assistant turn remains a single round-trip regardless
  of how long the wait actually takes.  Prefer this for "the plan
  needs child X to finish before the next step."
- **`inspect_workstream(ws_id=...)`** — single read of the child's
  state + tail.  Costs a full assistant turn (judge, tokens, stream).
  Prefer this for "what does the final message say?" after the child
  has already resolved (via `wait_for_workstream` or a known
  transition).

Rule of thumb: wait once for a fan-out, then inspect once per
child for the content.  A loop of inspect-every-few-seconds is a
token-burning antipattern — on 3+ children it rounds to a 10×
efficiency hit over a wait+inspect pair.

---

## Common coordinator patterns

Three patterns cover most coordinator skills.  Pick the one that
matches the task, or combine them deliberately.

### Pattern 1 — delegate-and-summarise

One specialist child, one focused brief, one synthesis message back
to the user.  Appropriate when the user's request is "run the thing
and tell me what happened" and the work fits in one workstream.

```
tasks(action='add', title='audit /auth for CSRF')
spawn_workstream(skill='engineer', initial_message='audit /auth ...')
wait_for_workstream(ws_ids=[<child>], timeout=300)
inspect_workstream(ws_id=<child>)
→ synthesise the final message into a user-facing response
tasks(action='update', task_id='t_01', status='done')
close_workstream(ws_id=<child>, reason='audit complete')
```

### Pattern 2 — fan-out-and-synthesise

N children running in parallel, each with a distinct brief, all
waited-on together, then synthesised.  Appropriate when the user's
request naturally decomposes into independent subtasks.

```
tasks seeds:
  t_01 benchmark Anthropic 4.7 latency on summarisation
  t_02 benchmark OpenAI GPT-5.2 latency on summarisation
  t_03 benchmark Gemini 2.5 latency on summarisation
spawn_batch(children=[...3 briefs...])
wait_for_workstream(ws_ids=[c1, c2, c3], mode='all', timeout=600)
inspect_workstream(ws_id=c1); ...(c2); ...(c3)
→ synthesise head-to-head comparison
tasks → all done
close_all_children(reason='benchmark complete')
```

Prefer `spawn_batch` over 3 individual `spawn_workstream` calls —
one approval instead of three, one audit trail, deterministic
sibling ordering.  Pair with `wait_for_workstream(mode='all')` and
`close_all_children(reason=...)` to wind the fan-out down in one
approval each.

### Pattern 3 — plan-then-delegate

The coordinator first uses its own reasoning to carve the plan,
records it in `tasks`, then spawns children that each own one
task.  Appropriate when the user's request is "figure out how to X"
and the coordinator's planning step is itself valuable.

```
→ coord reasons about the shape of the work
tasks(action='add', title='...') × N   # the plan, visible in the sidebar
for task in tasks:
    spawn_workstream(skill=..., initial_message=task.brief)
    tasks(action='update', task_id=task.id, notes='ws=<child_ws_id>')
wait_for_workstream(ws_ids=[...], mode='all', timeout=...)
for child in children:
    inspect_workstream(ws_id=child)
    tasks(action='update', task_id=..., status='done', notes='result summary')
→ synthesise
```

The key distinction from Pattern 2: the plan is an artifact the user
can see and interact with (via the sidebar).  If the coordinator's
reasoning-pass was wrong about the decomposition, the user can
course-correct before any child runs.

---

## Testing a coordinator skill

Coordinator sessions are hosted on the console, not on a node.
Integration tests that drive a real coord session live under
`tests/test_coordinator_end_to_end.py` — they spin a console with
an in-memory SQLite backend and a fake upstream node, then drive
the session through its HTTP surface.

For a new coordinator skill:

1. Write the skill prompt as a string and pass it to the
   `coord_session` fixture's `skill=` kwarg (see
   `tests/test_coordinator_tools.py` for the pattern).
2. Build a small fake cluster: one node + two children via
   the `_seed_children` helper in `tests/_coord_test_helpers.py`
   (``_seed_children(mgr._adapter, coord.id, ["child-1", "child-2"])``).
3. Drive the session with seeded tool_call dicts matching the
   provider layer's shape.  The unit-level tests in
   `tests/test_coordinator_tools.py` show the helper (`_tc(name,
   args, call_id)`).
4. Assert the skill's decision shape — which tools fire in what
   order, what the tasks looks like at the end, which
   `_error` reasons appear on the denied-path.

A full end-to-end test isn't required for every skill; a
prepare-step unit test that asserts "given this initial message, the
first tool call is X with Y args" is usually sufficient to catch
persona drift without a real LLM in the loop.

---

## Further reading

- [coordinator-api-tour.md](coordinator-api-tour.md) — the HTTP
  surface every coordinator skill indirectly drives.
- [bulk-endpoints.md](bulk-endpoints.md) — the response shape
  `spawn_batch` and `close_all_children` use, so your skill can
  parse results / denied arrays correctly.
- [governance.md](governance.md) — the broader governance surface
  (`/trust`, `/restrict`, `/stop_cascade`, role-based permissions)
  that wraps every coord session.
- [settings.md](settings.md) — `coordinator.model_alias` and
  `coordinator.reasoning_effort` settings that gate which LLM runs
  the coordinator session at all.
