"""Tests for alembic migration 060 (un-wrap legacy tool-output envelopes).

Drives ``command.upgrade`` from a programmatic Alembic config against an
isolated SQLite database per test, then asserts:

* a wrapped ``<tool_output>`` envelope row is rewritten to the bare
  (entity-decoded) tool output, dropping the embedded ``<system-reminder>``
  advisory blocks;
* a row whose content merely *starts with* a literal ``<tool_output>`` line
  but has no matching close is left untouched (structural-match guard);
* the ``_reminders`` side-channel column is nulled;
* the migration is idempotent (a second run is a no-op);
* a plain non-envelope row is untouched.
"""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config

_MIGRATIONS_DIR = str(
    Path(__file__).resolve().parent.parent / "turnstone" / "core" / "storage" / "migrations"
)


def _alembic_cfg(db_path: Path) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", _MIGRATIONS_DIR)
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def _seed_row(conn: sa.Connection, **cols: object) -> None:
    defaults: dict[str, object] = {
        "ws_id": "ws1",
        "timestamp": "2026-06-01T00:00:00",
        "role": "tool",
        "content": None,
        "tool_name": None,
        "tool_call_id": None,
        "provider_data": None,
        "tool_calls": None,
        "_source": None,
        "_reminders": None,
    }
    defaults.update(cols)
    keys = ", ".join(defaults)
    binds = ", ".join(f":{k}" for k in defaults)
    conn.execute(sa.text(f"INSERT INTO conversations ({keys}) VALUES ({binds})"), defaults)


# A wrapped envelope exactly as ``wrap_tool_result`` produced it: the
# ``<tool_output>`` block followed by one ``<system-reminder>`` advisory.
_WRAPPED = (
    "<tool_output>\nclean tool output\n</tool_output>\n"
    "<system-reminder>\nThe user sent a message. User message: check logs\n</system-reminder>"
)


class TestMigration060:
    def test_unwraps_envelope_and_drops_advisories(self, tmp_path: Path) -> None:
        db_path = tmp_path / "060-unwrap.db"
        cfg = _alembic_cfg(db_path)
        command.upgrade(cfg, "059")

        engine = sa.create_engine(f"sqlite:///{db_path}")
        try:
            with engine.begin() as conn:
                _seed_row(conn, content=_WRAPPED, tool_call_id="call_a")

            command.upgrade(cfg, "060")

            with engine.connect() as conn:
                content = conn.execute(
                    sa.text("SELECT content FROM conversations WHERE tool_call_id = 'call_a'")
                ).scalar_one()
            # Envelope stripped to the bare inner output; the advisory block
            # is gone (cosmetic loss accepted by the design).
            assert content == "clean tool output"
            assert "<tool_output>" not in content
            assert "<system-reminder>" not in content
        finally:
            engine.dispose()

    def test_entity_decode_round_trips_literal_wrapper_text(self, tmp_path: Path) -> None:
        """A tool output documenting the wrapper format escaped to entities
        on the way in; the un-wrap decodes it back to the literal tags."""
        db_path = tmp_path / "060-decode.db"
        cfg = _alembic_cfg(db_path)
        command.upgrade(cfg, "059")

        # escape_wrapper_tags(``see <tool_output> & <system-reminder>``) →
        # ``&amp;`` first, then the tag escapes.
        inner_escaped = "see &lt;tool_output&gt; &amp; &lt;system-reminder&gt;"
        wrapped = f"<tool_output>\n{inner_escaped}\n</tool_output>"

        engine = sa.create_engine(f"sqlite:///{db_path}")
        try:
            with engine.begin() as conn:
                _seed_row(conn, content=wrapped, tool_call_id="call_b")

            command.upgrade(cfg, "060")

            with engine.connect() as conn:
                content = conn.execute(
                    sa.text("SELECT content FROM conversations WHERE tool_call_id = 'call_b'")
                ).scalar_one()
            assert content == "see <tool_output> & <system-reminder>"
        finally:
            engine.dispose()

    def test_literal_prefix_without_close_untouched(self, tmp_path: Path) -> None:
        """A tool output that merely STARTS with a literal ``<tool_output>``
        line but has no matching close is not an envelope — left untouched."""
        db_path = tmp_path / "060-prefix.db"
        cfg = _alembic_cfg(db_path)
        command.upgrade(cfg, "059")

        unmatched = "<tool_output>\nthis tool printed the open tag but never closed it"

        engine = sa.create_engine(f"sqlite:///{db_path}")
        try:
            with engine.begin() as conn:
                _seed_row(conn, content=unmatched, tool_call_id="call_c")

            command.upgrade(cfg, "060")

            with engine.connect() as conn:
                content = conn.execute(
                    sa.text("SELECT content FROM conversations WHERE tool_call_id = 'call_c'")
                ).scalar_one()
            assert content == unmatched
        finally:
            engine.dispose()

    def test_nulls_reminders_column(self, tmp_path: Path) -> None:
        db_path = tmp_path / "060-reminders.db"
        cfg = _alembic_cfg(db_path)
        command.upgrade(cfg, "059")

        engine = sa.create_engine(f"sqlite:///{db_path}")
        try:
            with engine.begin() as conn:
                _seed_row(
                    conn,
                    role="user",
                    content="hello",
                    tool_call_id="call_d",
                    _reminders='[{"type":"correction","text":"watch it"}]',
                )

            command.upgrade(cfg, "060")

            with engine.connect() as conn:
                reminders = conn.execute(
                    sa.text("SELECT _reminders FROM conversations WHERE tool_call_id = 'call_d'")
                ).scalar_one()
            assert reminders is None
        finally:
            engine.dispose()

    def test_non_envelope_row_untouched(self, tmp_path: Path) -> None:
        db_path = tmp_path / "060-plain.db"
        cfg = _alembic_cfg(db_path)
        command.upgrade(cfg, "059")

        engine = sa.create_engine(f"sqlite:///{db_path}")
        try:
            with engine.begin() as conn:
                _seed_row(conn, content="just a normal tool result", tool_call_id="call_e")

            command.upgrade(cfg, "060")

            with engine.connect() as conn:
                content = conn.execute(
                    sa.text("SELECT content FROM conversations WHERE tool_call_id = 'call_e'")
                ).scalar_one()
            assert content == "just a normal tool result"
        finally:
            engine.dispose()

    def test_idempotent(self, tmp_path: Path) -> None:
        """A second pass over the now-clean rows is a no-op.

        Alembic won't re-run a stamped revision, so the rewrite's
        stability is asserted directly on the migration's ``_unwrap_envelope``
        guard: a bare (already-unwrapped) output is not an envelope, so a
        second pass leaves it alone.
        """
        db_path = tmp_path / "060-idem.db"
        cfg = _alembic_cfg(db_path)
        command.upgrade(cfg, "059")

        engine = sa.create_engine(f"sqlite:///{db_path}")
        try:
            with engine.begin() as conn:
                _seed_row(conn, content=_WRAPPED, tool_call_id="call_f")

            command.upgrade(cfg, "060")
            with engine.connect() as conn:
                first = conn.execute(
                    sa.text("SELECT content FROM conversations WHERE tool_call_id = 'call_f'")
                ).scalar_one()
        finally:
            engine.dispose()

        # The migration module's filename (``060_...``) isn't a valid import
        # identifier, so load it by path to reuse its guard.
        import importlib.util

        mig_path = (
            Path(__file__).resolve().parent.parent
            / "turnstone"
            / "core"
            / "storage"
            / "migrations"
            / "versions"
            / "060_unwrap_tool_envelopes.py"
        )
        spec = importlib.util.spec_from_file_location("_mig_060", mig_path)
        assert spec is not None and spec.loader is not None
        mig = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mig)
        # Already-clean content is not an envelope → second pass is a no-op.
        assert mig._unwrap_envelope(first) is None
