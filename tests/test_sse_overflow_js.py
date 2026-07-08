"""Static + runtime guards for the shared SSE overflow-recovery helper.

``turnstone/shared_static/sse_overflow.js`` is the client half of the SSE
overflow recovery — the storm-guard threshold, the cooldown-ladder constants,
and the two pure helpers (``overflowWindowTripped`` / ``degradedCooldownStep``)
— extracted so BOTH the interactive pane (``shared_static/interactive.js``) and
the coordinator pane (``console/static/coordinator/coordinator.js``) share one
source of truth for the trip math instead of drifting copies.  The panes keep
their own transport/DOM glue; only the pure core lives here.

Like the rest of the WebUI the module has no JS test framework, so these are
Python-side string-presence assertions plus two ``node`` runtime probes that
execute the extracted pure functions — the storm-guard math is the part the
design review marked UNCONFIRMED, so it gets run, not just string-pinned.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_SSE_OVERFLOW = _ROOT / "turnstone/shared_static/sse_overflow.js"


def test_module_exports_constants_and_pure_helpers() -> None:
    """The single source of truth exports the five tuning constants and the two
    pure helpers.  Both panes import these by name (pinned in their own suites),
    so a rename here is a breaking change that must surface loudly."""
    body = _SSE_OVERFLOW.read_text(encoding="utf-8")
    for const, value in (
        ("OVERFLOW_TRIP_COUNT", "3"),
        ("OVERFLOW_TRIP_WINDOW_MS", "60000"),
        ("DEGRADED_COOLDOWN_BASE_MS", "15000"),
        ("DEGRADED_COOLDOWN_MAX_MS", "120000"),
        ("DEGRADED_COOLDOWN_RESET_MS", "300000"),
    ):
        assert f"export const {const} = {value};" in body, f"missing export const {const}"
    assert "export function overflowWindowTripped(" in body
    assert "export function degradedCooldownStep(" in body


def test_overflow_window_tripped_runtime() -> None:
    """Runtime probe for the limiter's rolling-window helper — the storm-guard
    math is the part of Fix A the design review marked UNCONFIRMED, so it gets
    executed, not just string-pinned: prunes stale entries in place, trips at
    exactly K-in-window, and does not trip for closes spread wider than the
    window."""
    body = _SSE_OVERFLOW.read_text(encoding="utf-8")
    m = re.search(
        r"^export function overflowWindowTripped\(times, nowMs, count, windowMs\) \{.*?^\}",
        body,
        re.S | re.M,
    )
    assert m is not None, "overflowWindowTripped not found (keep it a module-level export)"
    harness = (
        m.group(0)
        + "\n"
        + "// trips at exactly count-in-window\n"
        + "let t = [1000, 2000, 3000];\n"
        + "if (!overflowWindowTripped(t, 3000, 3, 60000)) throw new Error('K-in-window must trip');\n"
        + "// stale entries prune in place and prevent the trip\n"
        + "t = [1000, 2000, 70000];\n"
        + "if (overflowWindowTripped(t, 70000, 3, 60000)) throw new Error('stale entries must not trip');\n"
        + "if (JSON.stringify(t) !== '[70000]') throw new Error('prune in place failed: ' + JSON.stringify(t));\n"
        + "// boundary: an entry exactly windowMs old is still counted\n"
        + "t = [10000, 70000];\n"
        + "if (!overflowWindowTripped(t, 70000, 2, 60000)) throw new Error('boundary entry must count');\n"
        + "// below threshold never trips\n"
        + "t = [];\n"
        + "if (overflowWindowTripped(t, 1, 1, 60000) !== false) throw new Error('empty must not trip');\n"
    )
    with tempfile.NamedTemporaryFile(mode="w", suffix=".mjs", delete=False) as f:
        f.write(harness)
        tmp = f.name
    try:
        proc = subprocess.run(["node", tmp], capture_output=True, text=True, timeout=15)
    except FileNotFoundError:
        pytest.skip("node binary not available on PATH")
    finally:
        os.unlink(tmp)
    assert proc.returncode == 0, (
        f"overflowWindowTripped runtime probe failed. stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )


def test_degraded_cooldown_ladder_escalates_and_resets_runtime() -> None:
    """Review finding [0] regression: the degraded-catchup cooldown ladder must
    actually ESCALATE across consecutive trips (15→30→60→120s, capped) and reset
    to base only after a genuine quiet gap.  The original bug cleared the
    overflow-window array in the trip handler, so the empty-window check reset
    the cooldown to base on every storm's first overflow and the doubling never
    took effect.  The fix keys the ladder off a last-trip timestamp via the pure
    degradedCooldownStep helper, exercised here directly."""
    body = _SSE_OVERFLOW.read_text(encoding="utf-8")
    m = re.search(
        r"^export function degradedCooldownStep\(.*?\) \{.*?^\}",
        body,
        re.S | re.M,
    )
    assert m is not None, "degradedCooldownStep not found (keep it a module-level export)"
    harness = (
        m.group(0)
        + "\n"
        + "const BASE=15000, MAX=120000, RESET=300000;\n"
        + "function assert(c,msg){ if(!c) throw new Error(msg); }\n"
        + "// First trip: gap since lastTrip(0) exceeds RESET -> base, next doubles.\n"
        + "let s = degradedCooldownStep(BASE, 0, 1000000, BASE, MAX, RESET);\n"
        + "assert(s.cooldown===15000, 'first trip cooldown '+s.cooldown);\n"
        + "assert(s.nextCooldownMs===30000, 'first next '+s.nextCooldownMs);\n"
        + "// Second trip recurs within RESET -> escalates (uses the doubled prev).\n"
        + "s = degradedCooldownStep(30000, 1000000, 1030000, BASE, MAX, RESET);\n"
        + "assert(s.cooldown===30000, 'second trip must ESCALATE not reset, got '+s.cooldown);\n"
        + "assert(s.nextCooldownMs===60000, 'second next '+s.nextCooldownMs);\n"
        + "// Third + fourth keep escalating and cap at MAX.\n"
        + "s = degradedCooldownStep(60000, 1030000, 1060000, BASE, MAX, RESET);\n"
        + "assert(s.cooldown===60000 && s.nextCooldownMs===120000, 'third '+JSON.stringify(s));\n"
        + "s = degradedCooldownStep(120000, 1060000, 1090000, BASE, MAX, RESET);\n"
        + "assert(s.cooldown===120000 && s.nextCooldownMs===120000, 'fourth must cap at MAX '+JSON.stringify(s));\n"
        + "// A quiet gap longer than RESET resets the ladder to base.\n"
        + "s = degradedCooldownStep(120000, 1090000, 1090000+RESET+1, BASE, MAX, RESET);\n"
        + "assert(s.cooldown===15000, 'quiet gap must reset to base, got '+s.cooldown);\n"
    )
    with tempfile.NamedTemporaryFile(mode="w", suffix=".mjs", delete=False) as f:
        f.write(harness)
        tmp = f.name
    try:
        proc = subprocess.run(["node", tmp], capture_output=True, text=True, timeout=15)
    except FileNotFoundError:
        pytest.skip("node binary not available on PATH")
    finally:
        os.unlink(tmp)
    assert proc.returncode == 0, (
        f"degradedCooldownStep escalation probe failed. stderr={proc.stderr!r}"
    )
