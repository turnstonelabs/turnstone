"""Personas: named capability/prompt bundles stamped onto workstreams at creation.

Adds the **Personas** feature (1.7, #683): a DB-backed template selecting the
system-message BASE module and the capability envelope for a workstream via four
levers — base-prompt override, tool visibility set, MCP on/off, memory toggle.
The persona is resolved once at workstream creation and snapshotted into
``workstream_config``; this table is a shelf, never read post-create, so edits
and archives never touch existing workstreams.

Prompt source is explicit in storage — never inferred in application logic:

- ``base_prompt_file`` — a repo file under ``prompts/personas/`` (e.g.
  ``scribe.md``).  Set only for built-ins (code-owned, PR-reviewed, drift-proof)
  and only by the migration/code — the admin API never exposes it for write.
  ``base_prompt_file IS NOT NULL`` ⟺ built-in ⟺ undeletable.
- ``base_prompt`` — inline prose, set by an operator (their own persona, or an
  override layered on a built-in row).

A CHECK enforces that at least one is set: a persona always names its source,
so resolution is a two-term coalesce (``base_prompt ?? load(base_prompt_file)``)
with no NULL/NULL fallthrough.  "Inherit the kind default" is not a persona
state — it is expressed at workstream creation by stamping the ``is_default``
persona for the kind.

Schema:

- ``personas`` — the template shelf.  ``tool_allowlist`` is tri-state JSON
  (NULL = unrestricted, ``[]`` = hard empty, ``[names]`` = exact set);
  ``is_default`` marks the per-kind resolution target for an empty ``persona=``
  (exactly one per kind); ``enabled=0`` = archived (no hard delete).
- ``workstreams.persona`` — nullable SLUG carrier for row projections
  (``personas.name``, not display_name — clients resolve the label; mirrors
  062's ``project_id`` shape); the full snapshot lives in ``workstream_config``.
- ``persona.{create,read,write}`` granted to ``builtin-admin`` (admin-default;
  opt others in via ``role_permission_overrides``), following the 062 pattern.
  No ``persona.delete`` — archive only.

Data: six file-backed seed personas.  ``engineer`` (interactive default) and
``orchestrator`` (coordinator default) carry the stock kind bases; ``writer``
replaces the removed ``/creative`` REPL toggle; ``scribe``/``researcher``/
``executive`` are curated restricted envelopes.

Backfill: every existing workstream is stamped with the resolved (frozen) base
prompt of a kind-appropriate persona — creative-mode rows become ``writer``,
the rest become their kind default — so no workstream is left personaless and
the ``snapshot is None`` path retires.

Revision ID: 063
Revises: 062
Create Date: 2026-07-02
"""

from __future__ import annotations

import datetime

import sqlalchemy as sa
from alembic import op

revision = "063"
down_revision = "062"
branch_labels = None
depends_on = None

_PERSONA_PERMS = ("persona.create", "persona.read", "persona.write")

# Frozen backfill prompts.  The backfill stamps the RESOLVED base prompt of the
# kind-default / writer personas onto existing workstreams, and that text must be
# reproducible and self-contained: a migration is immutable history, so it must
# NOT read the live prompts/personas/*.md files (which are the living source for
# NEW workstreams and may be renamed or edited after this migration ships — a
# run-time read would then crash `alembic upgrade` on a fresh DB, or freeze
# different text on two DBs migrated at different times).  These are a
# point-in-time snapshot of engineer.md / orchestrator.md / writer.md as of 063.
_BACKFILL_ENGINEER = """\
You are a software engineer working on this project. You know the codebase, the tools, and their limits.

You do real work: investigating bugs, implementing features, reviewing security, writing code that ships. You have access to the project's files and the tools your environment provides. You don't have access to everything — some tools require approval, some paths are restricted, and that's by design. You work within those boundaries.

You think before you act. You read before you edit. You verify before you commit. When something breaks, you diagnose before you retry. When you're uncertain, you say so. When a request is ambiguous, you make a reasonable call and note what you assumed — you don't stall asking for permission on every judgment call.

When you disagree with a direction, you push back with reasoning — then defer to the user's call.

The code you write will run. The files you edit are real. The commits you make go to a shared repository. Act accordingly.
"""

_BACKFILL_ORCHESTRATOR = """\
You are a coordinator.  Your role is to orchestrate work across the cluster: you decompose a user's request into tasks, spawn child workstreams on appropriate nodes with the right skills, monitor their progress, synthesise their results, and surface the outcome back to the user.

You do not edit files, run shells, or browse the web — children do. You pick the right child, give a well-formed brief, and keep the plan coherent while multiple children run.

You think in plans: enumerate the independent units of work, spawn one child per unit, run them in parallel by default. Sequential only when one child's output feeds the next. When a child reports back, you decide whether the goal is met, then close it out, push a follow-up, or spawn another child to cover the gap.

You are precise about what you delegate.  A child gets the minimum context it needs — skill, initial_message, maybe a node_id.  You don't paste whole files into its prompt; children have their own tools for that.

When a request is ambiguous, you make a reasonable call and note what you assumed.  When you disagree with a direction, you push back with reasoning — then defer to the user's call.  When something breaks, you diagnose before you retry: inspect the child, read the failure, pick a better skill or a better message, then re-delegate.

The children you spawn run real tools against real files.  Act accordingly.
"""

_BACKFILL_WRITER = """\
You are a creative writing partner. Think through structure, voice, and intent before you draft.

Craft principles:
- Ground scenes in concrete sensory detail — what is seen, heard, felt.
- Vary rhythm. Short sentences hit hard. Longer ones carry the reader through texture and nuance, building toward something.
- Dialogue should do at least two things: reveal character AND advance plot or tension. Cut anything that's just exchanging information.
- Earn your abstractions. Don't say 'she felt sad' — show the thing that makes the reader feel it.
- Trust subtext. Leave room for the reader.

Match the user's genre and tone. If they want literary fiction, write literary fiction. If they want pulp, write pulp with conviction. Never condescend to the form.

Treat revision as the real work: when the user pushes back on a draft, dig into what isn't landing — pacing, stakes, voice — rather than defending the words. Offer options where taste diverges; commit fully once a direction is chosen.
"""


def _append_permission(conn: sa.engine.Connection, perm: str) -> None:
    conn.execute(
        sa.text(
            "UPDATE roles SET permissions = permissions || :sep "
            "WHERE role_id = 'builtin-admin' "
            "AND permissions NOT LIKE :needle"
        ),
        {"sep": "," + perm, "needle": "%" + perm + "%"},
    )


def _remove_permission(conn: sa.engine.Connection, perm: str) -> None:
    conn.execute(
        sa.text(
            "UPDATE roles SET permissions = REPLACE(permissions, :needle, '') "
            "WHERE role_id = 'builtin-admin'"
        ),
        {"needle": "," + perm},
    )


def _backfill_config(
    conn: sa.engine.Connection,
    source: str,
    stamp: dict[str, str],
    kind: str | None = None,
) -> None:
    """Set-based backfill: write each of the five persona-snapshot keys onto
    every ``ws_id`` in the ``source`` temp table (optionally filtered to one
    ``kind``), one ``INSERT … SELECT`` per key — not a per-row loop, so the
    statement count is O(1) regardless of how many workstreams match.  ``source``
    is a migration-controlled temp-table name (never user input)."""
    where = " WHERE kind = :kind" if kind else ""
    for key, value in stamp.items():
        params: dict[str, str] = {"key": key, "val": value}
        if kind:
            params["kind"] = kind
        conn.execute(
            sa.text(
                f"INSERT INTO workstream_config (ws_id, key, value) "  # noqa: S608
                f"SELECT ws_id, :key, :val FROM {source}{where}"
            ),
            params,
        )


# (name, display_name, description, base_prompt_file, tool_allowlist JSON or None,
#  mcp, memory, kinds JSON, is_default).  Every built-in is file-backed:
#  base_prompt is seeded NULL and the prose lives in prompts/personas/<file>.
#  An operator override (base_prompt on a built-in row) is added later via the
#  API, never seeded.
_SEEDS = [
    (
        "scribe",
        "Scribe",
        "Turns raw material into clean, faithful, structured text. No tools, no memory.",
        "scribe.md",
        "[]",
        0,
        0,
        '["interactive"]',
        0,
    ),
    (
        "researcher",
        "Researcher",
        "Answers questions with evidence — reads and cites, loads tools to verify when needed.",
        "researcher.md",
        '["read_file", "search", "web_fetch", "web_search", "recall", "memory", "tool_search"]',
        0,
        1,
        '["interactive"]',
        0,
    ),
    (
        "writer",
        "Writer",
        "Creative writing partner. No tools; craft over machinery.",
        "writer.md",
        "[]",
        0,
        1,
        '["interactive"]',
        0,
    ),
    (
        "engineer",
        "Engineer",
        "The stock interactive workstream: full tools, MCP, and memory.",
        "engineer.md",
        None,
        1,
        1,
        '["interactive"]',
        1,
    ),
    (
        "orchestrator",
        "Manager / Orchestrator",
        "The stock coordinator: decomposes, delegates, monitors, synthesizes.",
        "orchestrator.md",
        None,
        1,
        1,
        '["coordinator"]',
        1,
    ),
    (
        "executive",
        "Executive",
        "Delegates and judges at altitude: status, decisions, outcomes.",
        "executive.md",
        '["spawn_workstream", "spawn_batch", "send_to_workstream", "wait_for_workstream", '
        '"inspect_workstream", "list_workstreams", "list_nodes", "close_workstream", '
        '"cancel_workstream", "memory"]',
        0,
        1,
        '["coordinator"]',
        0,
    ),
]


def upgrade() -> None:
    op.create_table(
        "personas",
        sa.Column("persona_id", sa.Text, primary_key=True),
        sa.Column("name", sa.Text, nullable=False, unique=True),
        sa.Column("display_name", sa.Text, nullable=False, server_default=""),
        sa.Column("description", sa.Text, nullable=False, server_default=""),
        sa.Column("base_prompt", sa.Text, nullable=True),
        sa.Column("base_prompt_file", sa.Text, nullable=True),
        sa.Column("tool_allowlist", sa.Text, nullable=True),
        sa.Column("mcp_enabled", sa.Integer, nullable=False, server_default="1"),
        sa.Column("memory_enabled", sa.Integer, nullable=False, server_default="1"),
        sa.Column("applies_to_kinds", sa.Text, nullable=False, server_default='["interactive"]'),
        sa.Column("is_default", sa.Integer, nullable=False, server_default="0"),
        sa.Column("enabled", sa.Integer, nullable=False, server_default="1"),
        sa.Column("org_id", sa.Text, nullable=False, server_default=""),
        sa.Column("created_by", sa.Text, nullable=False, server_default=""),
        sa.Column("created", sa.Text, nullable=False),
        sa.Column("updated", sa.Text, nullable=False),
        # A persona must name a prompt source: a repo file (built-in) or inline
        # text (operator), or both (operator override on a built-in) — never
        # neither.  Resolution is base_prompt ?? load(base_prompt_file), so the
        # forbidden NULL/NULL state has no meaning to encode in app logic.
        sa.CheckConstraint(
            "base_prompt IS NOT NULL OR base_prompt_file IS NOT NULL",
            name="ck_personas_prompt_source",
        ),
    )
    op.create_index("idx_personas_enabled", "personas", ["enabled"])

    conn = op.get_bind()
    now_str = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%S")
    for name, dname, desc, pfile, tools, mcp, memory, kinds, is_default in _SEEDS:
        conn.execute(
            sa.text(
                "INSERT INTO personas (persona_id, name, display_name, description, "
                "base_prompt, base_prompt_file, tool_allowlist, mcp_enabled, memory_enabled, "
                "applies_to_kinds, is_default, enabled, org_id, created_by, created, updated) "
                "VALUES (:pid, :name, :dname, :desc, NULL, :pfile, :tools, :mcp, :memory, "
                ":kinds, :dflt, 1, '', '', :now, :now)"
            ),
            {
                "pid": f"builtin-{name}",
                "name": name,
                "dname": dname,
                "desc": desc,
                "pfile": pfile,
                "tools": tools,
                "mcp": mcp,
                "memory": memory,
                "kinds": kinds,
                "dflt": is_default,
                "now": now_str,
            },
        )

    for perm in _PERSONA_PERMS:
        _append_permission(conn, perm)

    # -- Backfill existing workstreams with a frozen persona stamp ----------
    # Every workstream carries an explicit stamp; "no persona" is not a state
    # resolved in app logic.  Each stamp freezes the persona's RESOLVED base
    # prompt (the frozen `_BACKFILL_*` snapshots above).  Set-based — INSERT … SELECT
    # per key against a captured temp table — so the statement count is O(1) in
    # the number of workstreams, not six-per-row.  Two passes, ordered so the
    # second skips what the first stamped:
    #
    #   1. creative-mode -> writer.  Pre-063 persisted creative_mode='True' in
    #      workstream_config; writer is /creative's designated successor (same
    #      prompt lineage, tools off, MCP off, memory on).  The stale
    #      creative_mode key is left in place — nothing reads it, and downgrade
    #      needs it intact to resume those rows as creative again.
    #   2. everything else -> the kind default (engineer / orchestrator),
    #      unrestricted tools, MCP + memory on — byte-identical envelope to
    #      pre-063 zero-touch behaviour, now made explicit.
    #
    # Targets are captured into temp tables first, so the five per-target inserts
    # don't race the evolving 'persona' guard AND so workstreams.persona (its
    # ACCESS EXCLUSIVE lock) can be added AFTER the bulk config writes rather
    # than held across them.
    conn.execute(
        sa.text(
            "CREATE TEMPORARY TABLE _persona_creative AS "
            "SELECT ws_id FROM workstream_config "
            "WHERE key = 'creative_mode' AND value = 'True' "
            "AND ws_id NOT IN (SELECT ws_id FROM workstream_config WHERE key = 'persona')"
        )
    )
    _backfill_config(
        conn,
        "_persona_creative",
        {
            "persona": "writer",
            "persona_prompt": _BACKFILL_WRITER,
            "persona_tools": "[]",
            "persona_mcp": "0",
            "persona_memory": "1",
        },
    )

    conn.execute(
        sa.text(
            "CREATE TEMPORARY TABLE _persona_default AS "
            "SELECT ws_id, kind FROM workstreams "
            "WHERE ws_id NOT IN (SELECT ws_id FROM workstream_config WHERE key = 'persona')"
        )
    )
    _backfill_config(
        conn,
        "_persona_default",
        {
            "persona": "engineer",
            "persona_prompt": _BACKFILL_ENGINEER,
            "persona_tools": "null",
            "persona_mcp": "1",
            "persona_memory": "1",
        },
        kind="interactive",
    )
    _backfill_config(
        conn,
        "_persona_default",
        {
            "persona": "orchestrator",
            "persona_prompt": _BACKFILL_ORCHESTRATOR,
            "persona_tools": "null",
            "persona_mcp": "1",
            "persona_memory": "1",
        },
        kind="coordinator",
    )

    op.add_column("workstreams", sa.Column("persona", sa.Text, nullable=True))
    conn.execute(
        sa.text(
            "UPDATE workstreams SET persona = 'writer' "
            "WHERE ws_id IN (SELECT ws_id FROM _persona_creative)"
        )
    )
    conn.execute(
        sa.text(
            "UPDATE workstreams SET persona = 'engineer' "
            "WHERE ws_id IN (SELECT ws_id FROM _persona_default WHERE kind = 'interactive')"
        )
    )
    conn.execute(
        sa.text(
            "UPDATE workstreams SET persona = 'orchestrator' "
            "WHERE ws_id IN (SELECT ws_id FROM _persona_default WHERE kind = 'coordinator')"
        )
    )
    conn.execute(sa.text("DROP TABLE _persona_creative"))
    conn.execute(sa.text("DROP TABLE _persona_default"))


def downgrade() -> None:
    conn = op.get_bind()
    for perm in reversed(_PERSONA_PERMS):
        _remove_permission(conn, perm)

    # Remove every persona stamp — the seeds' backfill (kind defaults + writer)
    # and any created at runtime alike.  creative_mode keys were left intact, so
    # pre-063 code resumes those workstreams as creative again.
    # NOTE: this WIDENS restricted workstreams — a scribe-stamped session
    # (tools [], MCP off) resumes under pre-063 code with the full legacy
    # tool/MCP surface, since pre-063 code has no stamp to read.  The kind-
    # default (engineer/orchestrator) stamps were already unrestricted, so
    # dropping those is a no-op envelope-wise.  Widening is inherent to
    # downgrading past the feature; it is operator-initiated and called out
    # here rather than guarded.
    conn.execute(
        sa.text(
            "DELETE FROM workstream_config WHERE key IN "
            "('persona', 'persona_prompt', 'persona_tools', "
            "'persona_mcp', 'persona_memory')"
        )
    )

    op.drop_column("workstreams", "persona")

    op.drop_index("idx_personas_enabled", table_name="personas")
    op.drop_table("personas")
