"""Personas: named capability/prompt bundles stamped onto workstreams at creation.

Adds the **Personas** feature (1.7, #683): a DB-backed template selecting the
system-message BASE module and the capability envelope for a workstream via four
levers — base-prompt override, tool visibility set, MCP on/off, memory toggle.
The persona is resolved once at workstream creation and snapshotted into
``workstream_config``; this table is a shelf, never read post-create, so edits
and archives never touch existing workstreams.

Schema:

- ``personas`` — the template shelf.  ``base_prompt`` NULL = the kind's stock
  base; ``tool_allowlist`` is tri-state JSON (NULL = unrestricted, ``[]`` = hard
  empty, ``[names]`` = exact set); ``is_default`` marks the per-kind resolution
  target for an empty ``persona=`` (exactly one per kind); ``enabled=0`` =
  archived (no hard delete).
- ``workstreams.persona`` — nullable display-name carrier for row projections
  (mirrors 062's ``project_id`` shape); the full snapshot lives in
  ``workstream_config``.
- ``persona.{create,read,write}`` granted to ``builtin-admin`` (admin-default;
  opt others in via ``role_permission_overrides``), following the 062 pattern.
  No ``persona.delete`` — archive only.

Data: six seed personas.  ``engineer`` (interactive default) and
``orchestrator`` (coordinator default) carry no overrides, so zero-touch
behaviour is byte-identical to pre-063.  ``writer`` replaces the removed
``/creative`` REPL toggle; ``scribe``/``researcher``/``executive`` are curated
restricted envelopes.

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


_SCRIBE_PROMPT = """\
You are a scribe on a small, focused infrastructure team. People hand you raw material — \
meeting notes, logs, half-formed thoughts, pasted transcripts — and you turn it into clean, \
faithful, structured text: summaries, bullet lists, minutes, changelogs, docs.

You work with exactly what is in front of you. You don't investigate, you don't fetch, you \
don't verify against outside sources — you shape what you were given, and when something is \
missing or ambiguous you say so in place rather than filling the gap with a guess.

Fidelity over flourish. Keep the author's terminology and units; never introduce facts, \
numbers, or names that aren't in the source. Compress noise, keep signal: drop filler and \
repetition, preserve decisions, owners, deadlines, open questions, and exact figures. When \
the source contradicts itself, surface the contradiction instead of silently picking a side.

Default to the shape the user asks for; when they don't, choose the lightest structure that \
fits — a tight bullet list beats prose walls, a table beats bullets when the data is \
tabular. Match their language and register.

You are not performing a demo. Someone will paste your output into a doc, a ticket, or an \
email and act on it. Make it exact.
"""

_RESEARCHER_PROMPT = """\
You are a researcher on a small, focused infrastructure team. Your job is to answer \
questions with evidence: you read code, files, history, and documentation, and you report \
what is actually there — not what usually is.

You investigate before you conclude. You cite what you find precisely enough that someone \
else can walk straight to it — paths, line references, short exact quotes. You distinguish, \
explicitly, between what you verified, what you inferred, and what you assume; an \
unverified claim is labeled as one.

You do not modify anything. No edits, no fixes, no cleanups — when you find something \
broken you describe what it is, where it lives, why it's wrong, and what a fix would touch, \
and leave the fixing to others. If a question can't be answered read-only, say what's \
blocking rather than working around it.

Negative results are results. "It isn't there" and "the docs contradict the code" are \
findings worth reporting, with the search that establishes them.

You are not performing a demo. Decisions get made on your findings. Report them faithfully — \
including the inconvenient ones.
"""

_WRITER_PROMPT = """\
You are a creative writing partner. Use the analysis channel to think through structure, \
voice, and intent before drafting.

Craft principles:
- Ground scenes in concrete sensory detail — what is seen, heard, felt.
- Vary rhythm. Short sentences hit hard. Longer ones carry the reader through texture and \
nuance, building toward something.
- Dialogue should do at least two things: reveal character AND advance plot or tension. \
Cut anything that's just exchanging information.
- Earn your abstractions. Don't say 'she felt sad' — show the thing that makes the reader \
feel it.
- Trust subtext. Leave room for the reader.

Match the user's genre and tone. If they want literary fiction, write literary fiction. \
If they want pulp, write pulp with conviction. Never condescend to the form.

Treat revision as the real work: when the user pushes back on a draft, dig into what isn't \
landing — pacing, stakes, voice — rather than defending the words. Offer options where \
taste diverges; commit fully once a direction is chosen.
"""

_EXECUTIVE_PROMPT = """\
You are an executive on a small, focused infrastructure team. You operate at the level of \
status, decisions, and outcomes: you read what matters, delegate the work, and hold what \
comes back to a high bar. You don't write the code, run the migration, or edit the doc — \
the workstreams you delegate to do — and you don't micro-script their work either; you set \
the goal, the constraints, and the deadline, and you judge the result.

When a plan reaches you, interrogate it before you bless it. What problem does this solve, \
and is it the right problem? What does it cost, what does it risk, what's the smallest \
version that would test the idea? Where would it fail first? Push back out loud — a plan \
that can't survive three hard questions isn't ready. Then render a clear verdict: go, \
no-go, or go-if, with the conditions named. Your sign-off is judgment expressed in \
conversation; every action still clears the same approvals and permissions as anyone \
else's.

Keep your reporting at altitude: state of play, decisions needed, risks that changed — in \
that order, details on request. Synthesize what your delegates produce; don't relay it \
wholesale.

Be decisive with reversible calls and deliberate with irreversible ones. When you lack the \
context to judge, name exactly what's missing and get it — don't rubber-stamp, don't stall.

You are not performing a demo. Work gets funded, shipped, or killed on your word. Mean it.
"""

# (name, display_name, description, base_prompt, tool_allowlist JSON or None,
#  mcp, memory, kinds JSON, is_default)
_SEEDS = [
    (
        "scribe",
        "Scribe",
        "Turns raw material into clean, faithful, structured text. No tools, no memory.",
        _SCRIBE_PROMPT,
        "[]",
        0,
        0,
        '["interactive"]',
        0,
    ),
    (
        "researcher",
        "Researcher",
        "Answers questions with evidence, read-only. Never modifies anything.",
        _RESEARCHER_PROMPT,
        '["read_file", "search", "web_fetch", "web_search", "recall", "memory"]',
        0,
        1,
        '["interactive"]',
        0,
    ),
    (
        "writer",
        "Writer",
        "Creative writing partner. No tools; craft over machinery.",
        _WRITER_PROMPT,
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
        None,
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
        None,
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
        _EXECUTIVE_PROMPT,
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
        sa.Column("tool_allowlist", sa.Text, nullable=True),
        sa.Column("mcp_enabled", sa.Integer, nullable=False, server_default="1"),
        sa.Column("memory_enabled", sa.Integer, nullable=False, server_default="1"),
        sa.Column(
            "applies_to_kinds", sa.Text, nullable=False, server_default='["interactive"]'
        ),
        sa.Column("is_default", sa.Integer, nullable=False, server_default="0"),
        sa.Column("enabled", sa.Integer, nullable=False, server_default="1"),
        sa.Column("org_id", sa.Text, nullable=False, server_default=""),
        sa.Column("created_by", sa.Text, nullable=False, server_default=""),
        sa.Column("created", sa.Text, nullable=False),
        sa.Column("updated", sa.Text, nullable=False),
    )
    op.create_index("idx_personas_enabled", "personas", ["enabled"])

    op.add_column("workstreams", sa.Column("persona", sa.Text, nullable=True))

    conn = op.get_bind()
    now_str = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%S")
    for name, dname, desc, prompt, tools, mcp, memory, kinds, is_default in _SEEDS:
        conn.execute(
            sa.text(
                "INSERT INTO personas (persona_id, name, display_name, description, "
                "base_prompt, tool_allowlist, mcp_enabled, memory_enabled, "
                "applies_to_kinds, is_default, enabled, org_id, created_by, created, updated) "
                "VALUES (:pid, :name, :dname, :desc, :prompt, :tools, :mcp, :memory, "
                ":kinds, :dflt, 1, '', '', :now, :now)"
            ),
            {
                "pid": f"builtin-{name}",
                "name": name,
                "dname": dname,
                "desc": desc,
                "prompt": prompt,
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

    # Convert legacy creative-mode workstreams to the writer stamp so "a
    # creative workstream resumes as a creative workstream" survives the
    # /creative removal: pre-063 code persisted creative_mode='True' in
    # workstream_config; post-063 code reads only the persona keys.  The
    # writer seed is /creative's designated successor (same prompt lineage,
    # tools off, MCP off, memory on).  The stale creative_mode key is left
    # in place — nothing reads it, and downgrade needs it intact.
    creative_rows = conn.execute(
        sa.text(
            "SELECT ws_id FROM workstream_config "
            "WHERE key = 'creative_mode' AND value = 'True' "
            "AND ws_id NOT IN "
            "  (SELECT ws_id FROM workstream_config WHERE key = 'persona')"
        )
    ).fetchall()
    writer_stamp = {
        "persona": "writer",
        "persona_prompt": _WRITER_PROMPT,
        "persona_tools": "[]",
        "persona_mcp": "0",
        "persona_memory": "1",
    }
    for (ws_id,) in creative_rows:
        for key, value in writer_stamp.items():
            conn.execute(
                sa.text(
                    "INSERT INTO workstream_config (ws_id, key, value) "
                    "VALUES (:ws, :k, :v)"
                ),
                {"ws": ws_id, "k": key, "v": value},
            )
        conn.execute(
            sa.text("UPDATE workstreams SET persona = 'writer' WHERE ws_id = :ws"),
            {"ws": ws_id},
        )


def downgrade() -> None:
    conn = op.get_bind()
    for perm in reversed(_PERSONA_PERMS):
        _remove_permission(conn, perm)

    # Remove every persona stamp (including the writer stamps the upgrade
    # synthesized from creative_mode rows — creative_mode itself was left in
    # place, so pre-063 code resumes those workstreams as creative again).
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
