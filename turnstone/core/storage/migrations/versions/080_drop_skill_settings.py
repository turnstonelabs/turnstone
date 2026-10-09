"""Drop the session settings skills no longer carry.

A skill no longer chooses the model alias, sets temperature, reasoning effort or
max tokens, or caps task-agent turns for the workstreams it starts: the alias the
user or operator chose supplies the first four, and the operator's
``tools.agent_max_turns`` setting the turn cap (#1292).  Migration 021 added these
columns when workstream templates merged into ``prompt_templates``.

Skills also lose their activation mode (``activation``, mirrored in
``is_default``): no skill applies to every session, and none is listed in the
system prompt, so a skill applies only when a workstream names it.  The skill
token budget goes too, with the saved per-workstream ``token_budget`` keys that
carried it.  The dropped values are discarded.

Two data fixes ride along.  A workstream switched to another skill before this
release kept the skill it was created with in its saved stamp
(``applied_skill_content``), which a reopen now renders; where the saved skill
name names an existing skill other than the stamped one, and the stamped skill
still exists, the stamp is cleared, so the workstream reopens with the skill it
switched to.  The data cannot tell two rarer cases from those: a skill renamed
whose old name a newer skill took looks like a switch and is cleared too, and a
switch whose original skill was deleted, or whose target skill was later deleted
or renamed, keeps its stamp.  A skill's ``notify_on_complete`` that fails the
check workstream create applies (create ignored such a list or could not deliver
it, so it never fired) is reset to ``[]``, since skill create and update now
refuse it.

Revision ID: 080
Revises: 079
Create Date: 2026-10-07
"""

import json

import sqlalchemy as sa
from alembic import op

revision = "080"
down_revision = "079"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("prompt_templates") as batch_op:
        batch_op.drop_column("model")
        batch_op.drop_column("temperature")
        batch_op.drop_column("reasoning_effort")
        batch_op.drop_column("max_tokens")
        batch_op.drop_column("agent_max_turns")
        batch_op.drop_column("activation")
        batch_op.drop_column("is_default")
        batch_op.drop_column("token_budget")
    op.execute("DELETE FROM workstream_config WHERE key = 'token_budget'")
    # Stamps a pre-release skill switch left behind: the saved name is another existing skill.
    op.execute(
        "UPDATE workstream_config SET value = '' "
        "WHERE key = 'applied_skill_content' AND value <> '' AND ws_id IN ("
        " SELECT s.ws_id FROM workstream_config AS s"
        " JOIN workstream_config AS i ON i.ws_id = s.ws_id AND i.key = 'applied_skill_id'"
        " JOIN prompt_templates AS named ON named.name = s.value"
        " JOIN prompt_templates AS stamped ON stamped.template_id = i.value"
        " WHERE s.key = 'skill' AND s.value <> '' AND named.template_id <> stamped.template_id"
        ")"
    )
    conn = op.get_bind()
    rows = conn.execute(
        sa.text("SELECT template_id, notify_on_complete FROM prompt_templates")
    ).fetchall()
    for template_id, raw in rows:
        if (raw or "").strip() in ("", "[]", "{}") or _notify_targets_valid(raw):
            continue
        conn.execute(
            sa.text("UPDATE prompt_templates SET notify_on_complete = '[]' WHERE template_id = :t"),
            {"t": template_id},
        )


def _notify_targets_valid(raw: str) -> bool:
    """Whether workstream create accepts *raw* as notify targets.

    A frozen copy of ``turnstone.core.notify_targets.validate_notify_targets`` as of #1292, so a
    later change to the live check cannot change what this migration did.
    """
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return False
    if not isinstance(parsed, list) or len(parsed) > 10:
        return False
    for target in parsed:
        if not isinstance(target, dict) or target.get("channel_type") is None:
            return False
        if (target.get("channel_id") is None) == (target.get("user_id") is None):
            return False
        for key in ("channel_type", "channel_id", "user_id"):
            val = target.get(key)
            if val is None:
                continue
            if not isinstance(val, str) or not val.strip() or len(val.strip()) > 256:
                return False
    return True


def downgrade() -> None:
    # The deleted workstream_config rows are not restored: an older release reads a
    # missing ``token_budget`` key as no budget.  Neither data fix is undone: an older
    # release loads a cleared stamp's workstream by its skill name, which is what the
    # switch meant, and reads ``[]`` as no notify targets.
    with op.batch_alter_table("prompt_templates") as batch_op:
        batch_op.add_column(sa.Column("model", sa.Text, nullable=False, server_default=""))
        batch_op.add_column(sa.Column("temperature", sa.Float, nullable=True))
        batch_op.add_column(
            sa.Column("reasoning_effort", sa.Text, nullable=False, server_default="")
        )
        batch_op.add_column(sa.Column("max_tokens", sa.Integer, nullable=True))
        batch_op.add_column(sa.Column("agent_max_turns", sa.Integer, nullable=True))
        batch_op.add_column(
            sa.Column("activation", sa.Text, nullable=False, server_default="named")
        )
        batch_op.add_column(sa.Column("is_default", sa.Integer, nullable=False, server_default="0"))
        batch_op.add_column(
            sa.Column("token_budget", sa.Integer, nullable=False, server_default="0")
        )
