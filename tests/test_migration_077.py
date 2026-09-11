"""OAuth storage rename preserves populated databases in both directions."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from tests.conftest import make_mcp_token_cipher

_MIGRATIONS = str(Path(__file__).resolve().parents[1] / "turnstone/core/storage/migrations")
_ISSUER = "https://idp.example.com/tenant"
_CREATED = "2026-01-02T03:04:05"


def _rows(engine: sa.Engine, name: str) -> list[dict[str, Any]]:
    table = sa.Table(name, sa.MetaData(), autoload_with=engine)
    with engine.connect() as conn:
        return [
            dict(row)
            for row in conn.execute(sa.select(table).order_by(*table.primary_key)).mappings()
        ]


def _assert_layout(engine: sa.Engine, *, renamed: bool) -> None:
    table = "oauth_tokens" if renamed else "mcp_user_tokens"
    key = "token_key" if renamed else "server_name"
    index = "idx_oauth_tokens_key" if renamed else "idx_mcp_user_tokens_server"
    absent_table = "mcp_user_tokens" if renamed else "oauth_tokens"
    inspector = sa.inspect(engine)
    assert table in inspector.get_table_names()
    assert absent_table not in inspector.get_table_names()
    assert {column["name"] for column in inspector.get_columns(table)} == {
        "user_id",
        key,
        "access_token_ct",
        "refresh_token_ct",
        "expires_at",
        "scopes",
        "as_issuer",
        "audience",
        "created",
        "last_refreshed",
    }
    pk = inspector.get_pk_constraint(table)
    assert pk["constrained_columns"] == ["user_id", key]
    assert pk["name"] == (f"{table}_pkey" if engine.dialect.name == "postgresql" else None)
    secondary = [i for i in inspector.get_indexes(table) if not i.get("duplicates_constraint")]
    assert [(i["name"], i["column_names"], bool(i["unique"])) for i in secondary] == [
        (index, [key, "expires_at"], False)
    ]
    with engine.connect() as conn:
        if engine.dialect.name == "postgresql":
            names = set(
                conn.execute(
                    sa.text(
                        "SELECT indexname FROM pg_indexes WHERE schemaname = current_schema() "
                        "AND tablename = :table"
                    ),
                    {"table": table},
                ).scalars()
            )
            assert names == {f"{table}_pkey", index}
        else:
            names = {row[1] for row in conn.execute(sa.text(f'PRAGMA index_list("{table}")'))}
            assert names == {f"sqlite_autoindex_{table}_1", index}


def _roundtrip(url: sa.URL) -> None:
    cfg = Config()
    cfg.set_main_option("script_location", _MIGRATIONS)
    cfg.set_main_option(
        "sqlalchemy.url", url.render_as_string(hide_password=False).replace("%", "%%")
    )
    # Starting empty also exercises the complete historical migration chain.
    command.upgrade(cfg, "076")
    engine = sa.create_engine(url)
    cipher = make_mcp_token_cipher()
    identities = [
        ("u1", "mcp-custodial"),
        ("u2", "mcp-custodial"),
        ("u1", "mcp-obo"),
        ("u1", "__model_obo__:gateway"),
        ("u2", "__model_obo__:gateway"),
        ("__app__", "__model_app__:gateway"),
        ("u1", "__model_obo__:api://legacy-audience"),
        ("__app__", "__model_app__:api://legacy-audience"),
        ("u1", "__model_obo__:legacy\x1fsha256:opaque"),
    ]
    expected = [
        {
            "user_id": user,
            "server_name": key,
            "access_token_ct": cipher.encrypt(f"access-{i}".encode()),
            "refresh_token_ct": cipher.encrypt(f"refresh-{i}".encode()) if i < 2 else None,
            "expires_at": None if i == 0 else "2026-02-03T04:05:06",
            "scopes": None if i == 0 else ("" if i == 1 else "openid aud-gateway"),
            "as_issuer": _ISSUER,
            "audience": f"api://audience-{i}",
            "created": _CREATED,
            "last_refreshed": None if i % 2 else "2026-01-03T04:05:06",
        }
        for i, (user, key) in enumerate(identities)
    ]
    try:
        tables = sa.MetaData()
        tables.reflect(
            engine,
            only=[
                "mcp_user_tokens",
                "oidc_user_credentials",
                "mcp_oauth_pending",
                "mcp_pending_consent",
            ],
        )
        with engine.begin() as conn:
            conn.execute(tables.tables["mcp_user_tokens"].insert(), expected)
            conn.execute(
                tables.tables["oidc_user_credentials"].insert(),
                [
                    {
                        "user_id": user,
                        "issuer": _ISSUER,
                        "refresh_token_ct": cipher.encrypt(f"credential-{user}".encode()),
                        "created": _CREATED,
                        "last_refreshed": "2026-01-03T04:05:06",
                    }
                    for user in ("u1", "u2")
                ],
            )
            conn.execute(
                tables.tables["mcp_oauth_pending"].insert(),
                {
                    "state": "auth-state",
                    "user_id": "u1",
                    "server_name": "mcp-custodial",
                    "code_verifier": "verifier",
                    "return_url": "/settings",
                    "created_at": _CREATED,
                },
            )
            conn.execute(
                tables.tables["mcp_pending_consent"].insert(),
                {
                    "user_id": "u2",
                    "server_name": "mcp-custodial",
                    "error_code": "needs_auth",
                    "scopes_required": "scope",
                    "first_seen_at": _CREATED,
                    "last_seen_at": _CREATED,
                    "occurrence_count": 4,
                },
            )
        expected = _rows(engine, "mcp_user_tokens")
        untouched = {
            name: _rows(engine, name)
            for name in ("oidc_user_credentials", "mcp_oauth_pending", "mcp_pending_consent")
        }
        _assert_layout(engine, renamed=False)

        # Round-trip twice: inverse names must leave the next upgrade usable.
        for _ in range(2):
            command.upgrade(cfg, "077")
            _assert_layout(engine, renamed=True)
            actual = _rows(engine, "oauth_tokens")
            assert [
                {
                    ("server_name" if key == "token_key" else key): value
                    for key, value in row.items()
                }
                for row in actual
            ] == expected
            for row in actual:
                assert cipher.decrypt(row["access_token_ct"]).startswith(b"access-")
            assert {name: _rows(engine, name) for name in untouched} == untouched

            command.downgrade(cfg, "076")
            _assert_layout(engine, renamed=False)
            assert _rows(engine, "mcp_user_tokens") == expected
            assert {name: _rows(engine, name) for name in untouched} == untouched
    finally:
        engine.dispose()


def test_oauth_token_rename_sqlite(tmp_path: Path) -> None:
    _roundtrip(sa.URL.create("sqlite", database=str(tmp_path / "oauth.db")))


def test_oauth_token_rename_postgresql(fresh_pg_url: sa.URL) -> None:
    _roundtrip(fresh_pg_url)
