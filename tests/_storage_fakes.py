"""Scripted-PostgreSQL fakes shared by the storage race-test modules.

One implementation of the scripted connection/result pair and the keyed-save
three-way dispatch, so a backend statement-sequence or signature change is
updated once. The two hand-rolled twins had already diverged before the
round-4 review folded them here: the truncation copy grew a ``SET LOCAL``
arm and ``fetchall``/``scalar`` the prune copy lacked.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from sqlalchemy.dialects import postgresql

from turnstone.core.storage import AttachmentWrite


def make_attachment(
    attachment_id: str,
    content: bytes,
    *,
    filename: str | None = None,
    mime_type: str = "text/plain",
    kind: str = "text",
) -> AttachmentWrite:
    return AttachmentWrite(
        attachment_id=attachment_id,
        filename=filename or f"{attachment_id[0]}.txt",
        mime_type=mime_type,
        size_bytes=len(content),
        kind=kind,
        content=content,
    )


def save_keyed(
    backend: Any,
    ws_id: str,
    kind: str,
    *,
    content: str,
    commit_key: str,
    attachments: list[AttachmentWrite] | None = None,
    tool_content: str | None = None,
    tool_name: str = "read_file",
    tool_call_id: str = "call-keyed",
) -> int:
    """Three-way plain/user/tool keyed-save dispatch.

    The per-module literals (content, commit keys, attachment multiplicity)
    stay at the call sites — this owns only the method dispatch, so a
    signature change on the three save entry points is threaded once.
    """
    if kind == "plain":
        return int(backend.save_message(ws_id, "assistant", content, commit_key=commit_key))
    if kind == "user":
        return int(
            backend.save_user_message_with_attachments(
                ws_id,
                content,
                attachments or [],
                commit_key=commit_key,
            )
        )
    return int(
        backend.save_tool_message_with_attachments(
            ws_id,
            tool_content if tool_content is not None else content,
            tool_name,
            tool_call_id,
            attachments or [],
            commit_key=commit_key,
        )
    )


class ScriptedPostgresResult:
    def __init__(
        self,
        *,
        row: Any | None = None,
        rows: list[Any] | None = None,
        scalar_value: Any | None = None,
    ) -> None:
        self._row = row
        self._rows = rows or []
        self._scalar_value = scalar_value

    def fetchone(self) -> Any | None:
        return self._row

    def fetchall(self) -> list[Any]:
        return self._rows

    def scalar(self) -> Any | None:
        return self._scalar_value

    def scalar_one_or_none(self) -> Any | None:
        return self._scalar_value


def lease_row(ws_id: str, *, state: str = "idle", token: str = "") -> Any:
    """A parent-row lock result for an unleased workstream.

    Mirrors the columns ``_lease.lock_workstream_lease_row`` and
    ``_lease.admit_offline_writes_on_connection`` read, so scripted
    transactions take the unfenced, unleased admission path.
    """
    return SimpleNamespace(
        ws_id=ws_id,
        state=state,
        lease_holder=None,
        lease_node_id=None,
        lease_epoch=0,
        lease_expires_ms=None,
        incarnation_token=token,
        now_ms=0,
        lease_live=False,
    )


def expire_lease(backend: Any, ws_id: str, *, seconds_ago: float | None = None) -> None:
    """Move a workstream's owner-lease expiry into the past on the database clock.

    By default it expired long ago. *seconds_ago* expires it only that recently:
    a holder that just stopped renewing, as in a renewal outage.
    """
    import time

    import sqlalchemy as sa

    from turnstone.core.storage._schema import workstreams

    expires_ms = 1 if seconds_ago is None else int((time.time() - seconds_ago) * 1000)
    with backend._engine.connect() as conn:
        conn.execute(
            sa.update(workstreams)
            .where(workstreams.c.ws_id == ws_id)
            .values(lease_expires_ms=expires_ms)
        )
        conn.commit()


def stamp_updated_newest_first(backend: Any, ws_ids: list[str]) -> None:
    """Stamp ``updated`` so *ws_ids* sort newest-first in list order, one second apart."""
    from datetime import datetime, timedelta

    import sqlalchemy as sa

    from turnstone.core.storage._schema import workstreams

    newest = datetime(2026, 1, 1, 12, 0, 0)
    with backend._engine.begin() as conn:
        for i, ws_id in enumerate(ws_ids):
            stamp = (newest - timedelta(seconds=i)).strftime("%Y-%m-%dT%H:%M:%S")
            conn.execute(
                sa.update(workstreams).where(workstreams.c.ws_id == ws_id).values(updated=stamp)
            )


def acquire_lease(
    backend: Any,
    ws_id: str,
    *,
    holder: str = "node-a/1",
    token: str = "",
    allow_creating: bool = False,
) -> Any:
    """Take a real backend's owner lease on ``ws_id`` (token ``tok-<id>`` by default)."""
    grant = backend.acquire_workstream_lease(
        ws_id,
        incarnation_token=token or f"tok-{ws_id}",
        holder=holder,
        node_id=holder.split("/", 1)[0],
        ttl_seconds=30.0,
        allow_creating=allow_creating,
    )
    assert grant is not None
    return grant.fence


class ScriptedPostgresConnection:
    dialect = postgresql.dialect()

    def __init__(self, results: list[ScriptedPostgresResult]) -> None:
        self._results = results
        self.statements: list[Any] = []
        self.commits = 0
        self.rollbacks = 0

    def execute(self, statement: Any, *_args: Any, **_kwargs: Any) -> ScriptedPostgresResult:
        self.statements.append(statement)
        # Session-scoped tuning (the truncation lock_timeout bound) is not part
        # of the scripted result sequence; record it and return an empty result.
        if str(statement).startswith("SET LOCAL "):
            return ScriptedPostgresResult()
        if not self._results:
            raise AssertionError("unexpected PostgreSQL statement")
        return self._results.pop(0)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def assert_consumed(self) -> None:
        assert not self._results, f"unconsumed scripted results: {len(self._results)}"
