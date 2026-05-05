"""Tests for alembic migration 049 (OAuth-MCP schema).

Drives ``command.upgrade`` from a programmatic Alembic config against
an isolated SQLite database per test, then asserts:

* the two new tables (``mcp_user_tokens``, ``mcp_oauth_pending``) exist,
* the eight new ``mcp_servers`` columns exist,
* the post-upgrade ``UPDATE mcp_servers`` normalization rewrites rows
  with empty / missing headers to ``auth_type='none'`` while leaving
  rows with non-empty headers at ``auth_type='static'``.
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


class TestMigration049:
    def test_creates_new_tables_and_columns(self, tmp_path: Path) -> None:
        db_path = tmp_path / "049.db"
        cfg = _alembic_cfg(db_path)

        # Walk forward through 048 first, then explicitly to 049 so we
        # exercise the *upgrade* function (not just the schema's `head`).
        command.upgrade(cfg, "048")
        command.upgrade(cfg, "049")

        engine = sa.create_engine(f"sqlite:///{db_path}")
        try:
            inspector = sa.inspect(engine)
            tables = set(inspector.get_table_names())
            assert "mcp_user_tokens" in tables
            assert "mcp_oauth_pending" in tables

            mcp_cols = {c["name"] for c in inspector.get_columns("mcp_servers")}
            new_cols = {
                "auth_type",
                "oauth_client_id",
                "oauth_client_secret_ct",
                "oauth_scopes",
                "oauth_audience",
                "oauth_registration_mode",
                "oauth_authorization_server_url",
                "oauth_as_issuer_cached",
            }
            assert new_cols.issubset(mcp_cols), new_cols - mcp_cols

            # Index check on mcp_oauth_pending.
            indexes = {ix["name"] for ix in inspector.get_indexes("mcp_oauth_pending")}
            assert "idx_mcp_pending_created" in indexes
        finally:
            engine.dispose()

    def test_normalizes_empty_headers_to_none(self, tmp_path: Path) -> None:
        """Streamable-http rows with NULL / '' / '{}' headers become
        auth_type='none'; rows with non-empty headers stay 'static'.
        Stdio rows always stay 'static' regardless of headers — the
        column value is opaque when there is no HTTP transport."""
        db_path = tmp_path / "049-norm.db"
        cfg = _alembic_cfg(db_path)

        # Apply everything up to 048, seed rows, then apply 049.
        command.upgrade(cfg, "048")

        engine = sa.create_engine(f"sqlite:///{db_path}")
        try:
            with engine.begin() as conn:
                conn.execute(
                    sa.text(
                        """
                        INSERT INTO mcp_servers (
                            server_id, name, transport, command, args, url,
                            headers, env, auto_approve, enabled, created_by,
                            registry_name, registry_version, registry_meta,
                            created, updated
                        ) VALUES (
                            :sid, :name, :transport, '', '[]',
                            'https://x', :headers, '{}', 0, 1, '', NULL, '',
                            '{}', '2026-05-04T11:00:00', '2026-05-04T11:00:00'
                        )
                        """
                    ),
                    [
                        {
                            "sid": "s-empty-str",
                            "name": "empty-str",
                            "transport": "streamable-http",
                            "headers": "",
                        },
                        {
                            "sid": "s-empty-obj",
                            "name": "empty-obj",
                            "transport": "streamable-http",
                            "headers": "{}",
                        },
                        {
                            "sid": "s-with-headers",
                            "name": "with-headers",
                            "transport": "streamable-http",
                            "headers": '{"Authorization":"Bearer x"}',
                        },
                        # Stdio rows must keep the 'static' default, even
                        # though their headers are empty — auth_type is
                        # opaque for stdio.
                        {
                            "sid": "s-stdio-empty",
                            "name": "stdio-empty",
                            "transport": "stdio",
                            "headers": "{}",
                        },
                        {
                            "sid": "s-stdio-null",
                            "name": "stdio-null",
                            "transport": "stdio",
                            "headers": "",
                        },
                    ],
                )

            command.upgrade(cfg, "049")

            with engine.connect() as conn:
                rows = dict(conn.execute(sa.text("SELECT name, auth_type FROM mcp_servers")).all())
            assert rows["empty-str"] == "none"
            assert rows["empty-obj"] == "none"
            assert rows["with-headers"] == "static"
            # Stdio rows must remain at the 'static' column default even
            # when headers are empty — the migration only touches HTTP
            # rows where auth_type is semantically meaningful.
            assert rows["stdio-empty"] == "static"
            assert rows["stdio-null"] == "static"
        finally:
            engine.dispose()

    def test_full_chain_to_head(self, tmp_path: Path) -> None:
        """Sanity: running ``upgrade head`` on a fresh DB yields the
        same end-state column set as ``_schema.metadata``."""
        db_path = tmp_path / "049-head.db"
        cfg = _alembic_cfg(db_path)
        command.upgrade(cfg, "head")

        engine = sa.create_engine(f"sqlite:///{db_path}")
        try:
            from turnstone.core.storage._schema import mcp_servers

            inspector = sa.inspect(engine)
            actual = {c["name"] for c in inspector.get_columns("mcp_servers")}
            expected = {c.name for c in mcp_servers.columns}
            assert expected.issubset(actual), expected - actual
        finally:
            engine.dispose()
