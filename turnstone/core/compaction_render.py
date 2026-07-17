"""Render compaction lifecycle events as classic info-channel text lines.

One implementation, two consumers:

* :class:`turnstone.cli.TerminalUI` — the terminal's native rendering of
  ``on_compaction`` payloads (the threshold notice, ``part k/N``
  progress, and the token-delta + boxed-summary result it printed before
  these became structured events).
* :meth:`ChatSession._compaction_event`'s duck-typed fallback — a
  SessionUI implementation that predates the ``on_compaction`` hook gets
  these same lines through its ``on_info`` instead of silence (an
  auto-compaction that swaps history with zero announcement).

Display policy: failed ends consult ``payload["notice"]`` — stamped by
the emitter (:meth:`ChatSession._compaction_event`, the single policy
site) — never re-derive suppression from reason/trigger/superseded here.
Superseded OK ends still render: the history swap really committed, and
suppressing its announcement is exactly the silent-swap failure this
module exists to prevent.
"""

from collections.abc import Callable
from typing import Any


def render_compaction_event_as_info(
    payload: dict[str, Any], on_info: Callable[[str], None]
) -> None:
    """Print one compaction lifecycle event through ``on_info``."""
    phase = payload.get("phase")
    if phase == "start":
        # The threshold notice prints only when a threshold actually
        # fired (pct present — _do_auto_compact).  The overflow-retry
        # path compacts with auto=True but prints its own "[Context
        # overflow — auto-compacting and retrying]" line; claiming a
        # percentage there would fabricate the trigger.  Manual starts
        # print nothing — the caller's own activity display covers it.
        pct = payload.get("pct")
        if payload.get("trigger") == "auto" and pct is not None:
            where = payload.get("where") or ""
            qualifier = f" {where}" if where else ""
            on_info(f"\n[Auto-compacting{qualifier}: prompt exceeds {pct}% of context window]")
    elif phase == "progress":
        if payload.get("warning") == "summary_truncated":
            on_info("[Warning: compaction summary was truncated]")
        elif payload.get("retry_in") is not None:
            on_info(f"[Compact retrying in {payload['retry_in']:.0f}s: {payload.get('error', '')}]")
        else:
            on_info(f"[compacting part {payload.get('part')}/{payload.get('total')}…]")
    elif phase == "end":
        if payload.get("ok"):
            before = payload.get("before_tokens", 0)
            after = payload.get("after_tokens", 0)
            on_info(f"[compacted: ~{before:,} -> ~{after:,} tokens]")
            separator = "─" * 60
            lines = [separator]
            for line in str(payload.get("summary") or "").splitlines():
                lines.append(f"  {line}")
            lines.append(separator)
            on_info("\n".join(lines))
        elif payload.get("notice"):
            # The emitter stamps ``notice`` on failed ends (suppressing
            # error-reason ends — already printed red through on_error —
            # plus superseded and cancelled-auto ends).  A payload without
            # the field (an event replayed from an older node) stays
            # silent: these notices are informational, and re-deriving the
            # suppression here is the cross-runtime drift trap the stamp
            # exists to kill.
            on_info(str(payload.get("message") or ""))
