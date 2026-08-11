"""Scripted-PostgreSQL fakes shared by the storage race-test modules.

One implementation of the scripted connection/result pair and the keyed-save
three-way dispatch, so a backend statement-sequence or signature change is
updated once. The two hand-rolled twins had already diverged before the
round-4 review folded them here: the truncation copy grew a ``SET LOCAL``
arm and ``fetchall``/``scalar`` the prune copy lacked.
"""

from __future__ import annotations

from typing import Any

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


class ScriptedPostgresConnection:
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
