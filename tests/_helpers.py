"""Shared test helpers — kept out of conftest.py since these are factories,
not fixtures, and several test files want to import them directly."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

if TYPE_CHECKING:
    from collections.abc import Callable


def opaque_png() -> bytes:
    """A valid 1x1 PNG with no transparency, which an upload stores byte for byte.

    A transparent upload is stored flattened (#1445), so a test of an upload's bytes or
    content-addressed id needs an image with nothing to flatten.
    """
    from io import BytesIO

    from PIL import Image

    buf = BytesIO()
    Image.new("RGB", (1, 1), (255, 0, 0)).save(buf, format="PNG")
    return buf.getvalue()


def transparent_png() -> bytes:
    """A PNG of white content on a transparent background, which an upload stores flattened."""
    from io import BytesIO

    from PIL import Image

    img = Image.new("RGBA", (40, 20), (255, 255, 255, 0))
    img.paste((255, 255, 255, 255), (10, 5, 30, 15))
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def keyed_png(bit_depth: int, colour_type: int, rows: list[bytes], trns: bytes) -> bytes:
    """An 8-pixel-wide PNG written byte by byte, for sample depths Pillow can't save with a key."""
    import struct
    import zlib

    def chunk(kind: bytes, body: bytes) -> bytes:
        return (
            struct.pack(">L", len(body)) + kind + body + struct.pack(">L", zlib.crc32(kind + body))
        )

    width = 8
    header = struct.pack(">LLBBBBB", width, len(rows), bit_depth, colour_type, 0, 0, 0)
    raw = b"".join(bytes([0]) + row for row in rows)
    return (
        bytes([0x89])
        + b"PNG"
        + bytes([13, 10, 26, 10])
        + chunk(b"IHDR", header)
        + chunk(b"tRNS", trns)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def wait_until(cond: Callable[[], bool], timeout: float = 5.0) -> None:
    """Poll ``cond`` to True within ``timeout`` or fail the test.

    The worker/wake tests can't join threads by identity:
    ``session_worker.send`` assigns ``ws.worker_thread`` under the lock
    BEFORE ``t.start()``, so the instant a dispatching call returns, a
    fast worker may already have run its exit backstop and installed the
    (not-yet-started) wake thread — joining whatever ``ws.worker_thread``
    points at races ``RuntimeError: cannot join thread before it is
    started``.  Poll outcomes instead.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return
        time.sleep(0.005)
    if cond():
        # Final re-check: the condition can become true during the last
        # sleep (or a CI descheduling stall past the deadline) — failing
        # without re-looking makes the helper itself a flake source.
        return
    raise AssertionError("condition not met within timeout")


def make_chat_session(**overrides: Any) -> Any:
    """Build a minimal ``ChatSession`` with sane test defaults.

    Caller passes any constructor arg as a kwarg to override the default —
    e.g. ``make_chat_session(memory_config=MemoryConfig(relevance_k=5))``.
    """
    from turnstone.core.session import ChatSession

    defaults: dict[str, Any] = {
        "client": MagicMock(),
        "model": "test-model",
        "ui": MagicMock(),
        "instructions": None,
        "temperature": 0.5,
        "max_tokens": 4096,
        "tool_timeout": 30,
    }
    defaults.update(overrides)
    return ChatSession(**defaults)


def patch_session_storage(
    monkeypatch: Any,
    *,
    active: bool = True,
    raise_on_is_active: bool = False,
) -> list[str]:
    """Patch session storage for watch predicate tests."""
    from turnstone.core import session as session_mod

    calls: list[str] = []

    class _Stub:
        def get_workstream(self, ws_id: str) -> None:
            return None

        def is_watch_active(self, watch_id: str) -> bool:
            calls.append(watch_id)
            if raise_on_is_active:
                raise RuntimeError("storage down")
            return active

    monkeypatch.setattr(session_mod, "get_storage", lambda: _Stub())
    return calls
