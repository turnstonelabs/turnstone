"""Tests for alembic migration 060 (un-wrap legacy tool-output envelopes).

Drives ``command.upgrade`` from a programmatic Alembic config against an
isolated SQLite database per test, then asserts:

* a wrapped ``<tool_output>`` envelope row is rewritten to the bare tool
  output, dropping the embedded ``<system-reminder>`` advisory blocks;
* only ``&amp;`` → ``&`` is reversed — wrapper-tag entities stay escaped so a
  previously-defanged injection is not re-activated (sec-2);
* the tightened structural guard requires the *full* envelope signature, so a
  bare row that merely starts with ``<tool_output>`` — or even one with a
  matching ``</tool_output>`` close but no advisory — is left untouched (the
  known-issue #1 false positive);
* the dead ``_reminders`` side-channel column is dropped outright (not nulled
  and carried forward as a writable foot-gun);
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
# ``<tool_output>`` block, then ``"\n".join`` with a part that itself begins
# with ``\n<system-reminder>`` — yielding the ``</tool_output>\n\n<system-
# reminder>`` double-newline join the tightened guard requires.
_WRAPPED = (
    "<tool_output>\nclean tool output\n</tool_output>\n\n"
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

    def test_ampersand_decoded_but_wrapper_tags_left_escaped(self, tmp_path: Path) -> None:
        """The un-wrap reverses only ``&amp;`` → ``&``.  Wrapper-tag entities are
        left escaped on purpose: re-activating ``&lt;system-reminder&gt;`` into a
        live tag would un-defang injection the old escape had neutralised."""
        db_path = tmp_path / "060-decode.db"
        cfg = _alembic_cfg(db_path)
        command.upgrade(cfg, "059")

        # escape_wrapper_tags(``see <tool_output> & <system-reminder>``) →
        # ``&amp;`` first, then the tag escapes.
        inner_escaped = "see &lt;tool_output&gt; &amp; &lt;system-reminder&gt;"
        wrapped = (
            f"<tool_output>\n{inner_escaped}\n</tool_output>\n\n"
            "<system-reminder>\nThe user sent a message. User message: x\n</system-reminder>"
        )

        engine = sa.create_engine(f"sqlite:///{db_path}")
        try:
            with engine.begin() as conn:
                _seed_row(conn, content=wrapped, tool_call_id="call_b")

            command.upgrade(cfg, "060")

            with engine.connect() as conn:
                content = conn.execute(
                    sa.text("SELECT content FROM conversations WHERE tool_call_id = 'call_b'")
                ).scalar_one()
            # ``&amp;`` → ``&`` only; the wrapper-tag entities stay escaped.
            assert content == "see &lt;tool_output&gt; & &lt;system-reminder&gt;"
            assert "<system-reminder>" not in content
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

    def test_tool_output_open_close_without_advisory_untouched(self, tmp_path: Path) -> None:
        """The known-issue #1 false positive: a bare tool output that genuinely
        starts with ``<tool_output>`` AND has a matching ``</tool_output>`` close
        but NO trailing ``<system-reminder>`` advisory is NOT a legacy envelope
        (those were only emitted with advisories).  The tightened guard leaves
        it byte-for-byte untouched — the loose open+close guard would have
        irreversibly mis-rewritten it to its inner text."""
        db_path = tmp_path / "060-noadvisory.db"
        cfg = _alembic_cfg(db_path)
        command.upgrade(cfg, "059")

        # e.g. a tool that printed XML, or this project's own source/docs.
        bare = "<tool_output>\nls -la output here\n</tool_output>\nplus a trailing line"

        engine = sa.create_engine(f"sqlite:///{db_path}")
        try:
            with engine.begin() as conn:
                _seed_row(conn, content=bare, tool_call_id="call_g")

            command.upgrade(cfg, "060")

            with engine.connect() as conn:
                content = conn.execute(
                    sa.text("SELECT content FROM conversations WHERE tool_call_id = 'call_g'")
                ).scalar_one()
            assert content == bare
        finally:
            engine.dispose()

    def test_missing_trailing_system_reminder_close_untouched(self, tmp_path: Path) -> None:
        """An open + join that lacks the trailing ``</system-reminder>`` close is
        not a complete envelope — left untouched rather than half-rewritten."""
        db_path = tmp_path / "060-notail.db"
        cfg = _alembic_cfg(db_path)
        command.upgrade(cfg, "059")

        truncated = "<tool_output>\nx\n</tool_output>\n\n<system-reminder>\nno close here"

        engine = sa.create_engine(f"sqlite:///{db_path}")
        try:
            with engine.begin() as conn:
                _seed_row(conn, content=truncated, tool_call_id="call_h")

            command.upgrade(cfg, "060")

            with engine.connect() as conn:
                content = conn.execute(
                    sa.text("SELECT content FROM conversations WHERE tool_call_id = 'call_h'")
                ).scalar_one()
            assert content == truncated
        finally:
            engine.dispose()

    def test_drops_reminders_column(self, tmp_path: Path) -> None:
        db_path = tmp_path / "060-reminders.db"
        cfg = _alembic_cfg(db_path)
        command.upgrade(cfg, "059")

        engine = sa.create_engine(f"sqlite:///{db_path}")
        try:
            with engine.begin() as conn:
                # The column still exists at 059, so a legacy value can be seeded.
                _seed_row(
                    conn,
                    role="user",
                    content="hello",
                    tool_call_id="call_d",
                    _reminders='[{"type":"correction","text":"watch it"}]',
                )
            # At 059 the column is present.
            assert "_reminders" in {
                c["name"] for c in sa.inspect(engine).get_columns("conversations")
            }

            command.upgrade(cfg, "060")

            # 060 drops it outright (no dead column carried forward); the row
            # itself survives.
            cols = {c["name"] for c in sa.inspect(engine).get_columns("conversations")}
            assert "_reminders" not in cols
            assert "_source" in cols  # the live sibling stays
            with engine.connect() as conn:
                content = conn.execute(
                    sa.text("SELECT content FROM conversations WHERE tool_call_id = 'call_d'")
                ).scalar_one()
            assert content == "hello"
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
