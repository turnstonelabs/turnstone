"""Bounded previews of a reply body for log lines and error text, with every character that isn't
printable replaced."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import httpx2


def _printable(text: str) -> str:
    """*text* with each character ``str.isprintable`` refuses replaced by a space: C0 and C1
    controls, line and paragraph separators, and format characters such as a bidi override. Any of
    them could break a log line, or change how a terminal or log viewer shows it."""
    return "".join(ch if ch.isprintable() else " " for ch in text)


async def bounded_stream_preview(response: httpx2.Response, cap: int = 200) -> str:
    """Streaming counterpart of :func:`bounded_body_preview`.

    Reads at most ``int(cap * 1.3) + 1`` bytes of ``response.aiter_bytes``, enough for ``cap``
    characters of mostly-ASCII text, so a compromised / oversized upstream can't force the caller
    to buffer an arbitrary error page just to populate a preview.  Shares the scrub with the
    non-streaming helper so the output shape is identical across sites.
    """
    preview_chunks: list[bytes] = []
    read = 0
    byte_cap = int(cap * 1.3) + 1
    try:
        async for chunk in response.aiter_bytes():
            if not chunk:
                continue
            remaining = byte_cap - read
            if remaining <= 0:
                break
            preview_chunks.append(chunk[:remaining])
            read += len(preview_chunks[-1])
            if read >= byte_cap:
                break
    except Exception:
        return "<unreadable>"
    decoded = b"".join(preview_chunks).decode("utf-8", "replace")
    return _printable(decoded[:cap])


def bounded_body_preview(text: str | bytes, cap: int = 200) -> str:
    """Return a body preview for 4xx logs capped at ``cap`` chars.

    Body is already in memory on a non-streaming httpx2 response; this
    helper exists so every call-site produces the same shape.

    Characters that aren't printable (CR, LF, NUL, C1 controls, line separators, bidi overrides,
    ...) are replaced with spaces: the preview flows into both ``log.warning`` records and the
    operator-facing 503 ``collector_scope_error`` body, and upstream-controlled newlines in either
    surface would let a compromised node forge additional log lines or masquerade as embedded
    remediation text.
    """
    if not text:
        return ""
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    return _printable(text[:cap])
