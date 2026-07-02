# Personas

A **persona** is a named, reusable bundle attached to a workstream **at
creation** that controls how its system message is composed and what
capability envelope it runs with. Personas answer a recurring operational
complaint: the default composition primes every session for heavy tool use,
and there was no per-workstream dial to launch a "just write prose" or
"read-only research" session.

A persona is exactly four levers — no more:

| Lever | What it does |
|---|---|
| **Base prompt** | Replaces the BASE module of the composed system message (`base.md` / `base_coordinator.md`). *Only* BASE: ENV, CONTEXT, TOOLS, and POLICIES keep composing, so mandatory [prompt policies](governance.md) ride on top of every persona. Empty = the kind's stock base. |
| **Tool visibility** | Which tools the session advertises. Tri-state: *unrestricted* (tracks tool growth and MCP catalogs), *no tools* (the TOOLS prompt block self-suppresses and zero definitions go on the wire), or an *exact set* of names. Including `tool_search` in a set makes it **soft** — tools the model discovers through search join the visible set; omitting it makes the set **hard** (the search pathway is disabled entirely). On commercial providers a soft set costs one prompt-cache re-prime per `tool_search` expansion, since each expansion rewrites the wire tool set and recomposes the prompt. |
| **MCP** | Whether the workstream talks to MCP at all. **Session-wide**: off means no MCP tools for the persona's own hands *or* for in-process task agents, no resource/prompt catalogs, and no listener registrations. This lever expresses infrastructure intent, not behavior shaping. |
| **Memory** | Whether the persona's **own hands** get memory: recalled-memory injection into the prompt, memory-directed metacognitive nudges, and the `memory` tool. Task agents keep their own envelope, and compaction spill/markers are session mechanics that are never persona-gated. An exact tool set that hides `memory` also mutes those nudges, and the compaction-resume pointer follows `recall`'s visibility. |

Visibility is behavior shaping, **not** a security boundary: any tool call
that does reach the wire still clears the same approval, judge, and policy
machinery as always. RBAC and tool policies remain the enforcement layers.

## Snapshot semantics — resolve once, stamp forever

The persona is resolved **once**, at workstream creation, and stamped into
`workstream_config` as five keys (`persona`, `persona_prompt`,
`persona_tools`, `persona_mcp`, `persona_memory`). From then on the session
reads only the stamp:

- **Editing or archiving a persona never changes an existing workstream.**
  Rehydrate, resume, and post-compaction resume all run from the stamp.
  A mid-session REPL `/resume` adopts the target workstream's stamp for
  prompt, tools, and memory; for the MCP lever it can only narrow in
  place — adopting an MCP-off stamp drops the live MCP surface, while
  adopting an MCP-on stamp into a session whose persona dropped MCP at
  construction is refused with an error telling you to reopen the
  workstream fresh.
- A workstream outlives its persona — an archived persona keeps labelling
  the workstreams stamped with it.
- A partial or unparseable stamp is treated as corruption: session
  construction fails loudly rather than silently falling back to a default
  envelope the operator never chose.
- Workstreams created before personas existed carry no stamp and keep
  legacy behavior, byte-identical to the `engineer` / `orchestrator`
  defaults below — with one exception: pre-1.7 workstreams that had
  `creative_mode` set are converted by migration `063` into full
  `writer` stamps, so they resume as writing sessions rather than as
  legacy defaults.
- Forking (`resume_ws` on create) resumes the source's stamped persona; the
  fork does not re-resolve.

## Seed personas

Migration `063` seeds six personas. The two per-kind **defaults** carry no
overrides at all, so a zero-touch launch behaves exactly as it did before
personas existed:

| Persona | Kind | Base prompt | Tools | MCP | Memory |
|---|---|---|---|---|---|
| `engineer` *(default)* | interactive | stock | unrestricted | on | on |
| `orchestrator` *(default)* | coordinator | stock | unrestricted | on | on |
| `scribe` | interactive | custom (faithful structuring of given material) | none | off | off |
| `researcher` | interactive | custom (evidence-first, read-only) | `read_file`, `search`, `web_fetch`, `web_search`, `recall`, `memory` (hard) | off | on |
| `writer` | interactive | custom (creative writing partner — replaces the removed `/creative`) | none | off | on |
| `executive` | coordinator | custom (delegate, interrogate plans, judge outcomes) | spawn/inspect/lifecycle tools plus `memory`: `spawn_workstream`, `spawn_batch`, `send_to_workstream`, `wait_for_workstream`, `inspect_workstream`, `list_workstreams`, `list_nodes`, `close_workstream`, `cancel_workstream`, `memory` (hard) | off | on |

Notes:

- `scribe` turns memory off deliberately: recalled memories would
  contaminate faithful summarization with unrelated context.
- `researcher`'s set is hard (no `tool_search`) — including the escape
  hatch would let the model load write tools and break the read-only
  promise.
- Coordinator sessions do not merge MCP today, so the MCP lever on
  coordinator personas is forward-compatible bookkeeping; it bites on
  interactive workstreams.

## Choosing a persona

Every creation surface takes an optional persona; empty always means the
kind's default (or plain legacy behavior on a database with no personas
seeded):

- **Web/console**: the persona select on the console launcher, the server
  webui's new-workstream dialog, and the dashboard composer. Selecting a
  persona requires **no** `persona.*` permission — the picker feed
  (`GET /v1/api/personas`) is authenticated-only and returns display fields.
- **API/SDK**: `CreateWorkstreamRequest.persona` (Python:
  `create_workstream(persona=...)`; TypeScript: `{ persona: ... }`).
- **CLI**: `turnstone --persona <name>`. Unknown or disabled names error at
  startup. `--resume` ignores `--persona` and adopts the resumed
  workstream's stamp.
- **Coordinator spawn**: `spawn_workstream` / `spawn_batch` take a
  `persona` argument, validated when the coordinator prepares the spawn
  and re-checked by the node that creates the child (children are always
  interactive-kind). Omitted means the interactive **default** — a child
  never inherits its parent coordinator's persona. Sub-agents spawned via
  `task_agent` have no persona parameter at all; they keep their own
  identity and envelope.

## Authoring (console)

Personas are managed in the console's **Manage → Governance → Personas**
tab. The admin shelf exposes exactly the four levers plus the kind
list, the default marker, and archive. Rules:

- `name` is an immutable lowercase slug; edit `display_name` instead.
- Exactly one default per kind, storage-enforced: flipping the flag on a
  successor demotes the incumbent atomically, defaults are single-kind,
  and a default cannot be archived.
- **Archive only** — there is no delete verb, so every stamped
  workstream's provenance stays explicable.

RBAC: `persona.create` / `persona.read` / `persona.write` gate the admin
CRUD (`/v1/api/admin/personas`); all three are granted to `builtin-admin`
by migration `063`, and other roles opt in via role permission overrides.
