"""Inputs and parser failures for JSON decoder limits.

Python 3.13 raises ``RecursionError`` for JSON nested about 10,000 levels deep, and 3.14 only when
the thread's C stack runs out, so a 3.14 run parses real deep input on its main thread. A test seeds
an otherwise valid value instead and makes ``json.loads`` raise for exactly that value: a test whose
patch stops applying sees the value parse, and fails.

Oversized integers use real input beyond the active interpreter's conversion limit.
"""

from __future__ import annotations

import json
import sys
from typing import Any

import pytest


def too_deep_to_parse(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    """``json.loads`` raises ``RecursionError`` for *value*, as text or as UTF-8 bytes."""
    real_loads = json.loads
    seeded = (value, value.encode())

    def loads(s: Any, *args: Any, **kwargs: Any) -> Any:
        if s in seeded:
            raise RecursionError("maximum recursion depth exceeded while decoding a JSON object")
        return real_loads(s, *args, **kwargs)

    monkeypatch.setattr(json, "loads", loads)


def overlong_integer() -> str:
    """An integer literal beyond this interpreter's configured conversion limit."""
    limit = sys.get_int_max_str_digits()
    if limit == 0:
        pytest.skip("integer string conversion limit is disabled")
    return "9" * (limit + 1)
