"""Store a default model for existing setups that never set one.

A registry load no longer picks another model when the default's definition is
left out of it: a default that isn't loaded fails the creates that rely on it
(#1359). From this release the console also stores the first chat model added
(or enabled) as ``model.default_alias`` when none is stored, so the default is
a setting rather than a pick that a later model, whose alias sorts first,
could take over.

Existing setups that never set ``model.default_alias`` get one stored here:
among the enabled definitions that can hold a chat, the one named ``default``,
else the first in alias order (the storage list order). A reranker or speech
model is never picked: the setting outranks every node's config.toml default,
so a setup whose chat models live in config.toml and whose Models tab holds
only a reranker must not route its chats there. Nothing is written when a
global ``model.default_alias`` row already exists, cleared or not, or when no
enabled chat definition exists.

A migration cannot read config.toml. A host whose config.toml picks its
default itself (``[model] default``, or a ``[models.default]`` entry) while
the database holds model rows now follows the stored setting instead, which
outranks config.toml; clearing the setting restores the config.toml default
(CHANGELOG).

Downgrade leaves the row in place: it cannot tell this migration's row from
one an operator wrote, and the older loader reads it the same way.

Revision ID: 082
Revises: 081
Create Date: 2026-10-13
"""

import json
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision = "082"
down_revision = "081"
branch_labels = None
depends_on = None

_KEY = "model.default_alias"

# A frozen copy of the console's chat test (``serves_chat``), so this migration
# does not change with the live code: rerankers and dedicated speech models are
# not chat models; a model that takes audio in chat is.
_SPEECH_FLAGS: dict[str, tuple[str, ...]] = {
    "supports_transcription": ("transcribe", "whisper", "-asr"),
    "supports_speech_synthesis": ("tts-", "-tts"),
}


def _serves_chat(capabilities: str | None, model: str | None) -> bool:
    try:
        caps = json.loads(capabilities or "{}")
    except (ValueError, TypeError, RecursionError):
        caps = {}
    if not isinstance(caps, dict):
        caps = {}
    if caps.get("supports_rerank"):
        return False
    if caps.get("supports_audio_input"):
        return True
    name = (model or "").strip().lower()
    for flag, hints in _SPEECH_FLAGS.items():
        if flag in caps:
            if caps.get(flag):
                return False
        elif name and any(hint in name for hint in hints):
            return False
    return True


def upgrade() -> None:
    conn = op.get_bind()
    already_set = conn.execute(
        sa.text("SELECT 1 FROM system_settings WHERE key = :key AND node_id = ''"),
        {"key": _KEY},
    ).first()
    if already_set is not None:
        return
    aliases = [
        row[0]
        for row in conn.execute(
            sa.text(
                "SELECT alias, model, capabilities FROM model_definitions "
                "WHERE enabled = 1 ORDER BY alias"
            )
        ).fetchall()
        if _serves_chat(row[2], row[1])
    ]
    if not aliases:
        return
    pick = "default" if "default" in aliases else aliases[0]
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
    conn.execute(
        sa.text(
            "INSERT INTO system_settings "
            "(key, value, node_id, is_secret, changed_by, created, updated) "
            "VALUES (:key, :value, '', 0, '', :now, :now)"
        ),
        {"key": _KEY, "value": json.dumps(pick), "now": now},
    )


def downgrade() -> None:
    pass
