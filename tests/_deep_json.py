"""Make the JSON parser fail on one value as it fails on JSON nested too deeply.

Python 3.13 raises ``RecursionError`` for JSON nested about 10,000 levels deep, and 3.14 only when
the thread's C stack runs out, so a 3.14 run parses real deep input on its main thread. A test seeds
an otherwise valid value instead and makes ``json.loads`` raise for exactly that value: a test whose
patch stops applying sees the value parse, and fails.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
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
