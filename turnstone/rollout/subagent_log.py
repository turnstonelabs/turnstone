"""The tool calls of a rollout's sub-agents, for the record.

A sub-agent is a tool for the training sample: the call that starts it is the
session's own, and its conversation is not trained on.  Its tool calls still
act in the episode's container, and a reward that counts what a rollout did
(a command it should not have run, a file it should not have read) has to see
them, or a model could delegate what it does not want counted.

The session's own tool log does not hold them: it lists the session's calls.
:class:`SubAgentLog` keeps them apart.  The session hands each sub-agent's
turn list to :meth:`SubAgentLog.watch` before the sub-agent runs; every
assistant turn the sub-agent's loop adds to that list brings its tool calls,
and every tool turn the result of one.  That sees every call the sub-agent's
model issued, also one the loop answered without running anything (a tool the
sub-agent is not offered, an attempt to start a sub-agent of its own).

Nothing here can fail the sub-agent: a turn that cannot be read is logged and
left out.
"""

from __future__ import annotations

import json
import threading
from typing import TYPE_CHECKING, Any

from turnstone.core.log import get_logger
from turnstone.core.tools import apply_null_spellings
from turnstone.core.trajectory import Role, TextBlock

if TYPE_CHECKING:
    from collections.abc import Callable

log = get_logger(__name__)

# Characters of a result kept per call, as in the session's own tool log.
_RESULT_CHARS = 500


class _WatchedTurns(list[Any]):
    """A sub-agent's turns; each one added or put in place is shown to a watcher."""

    def __init__(self, turns: list[Any], note: Callable[[Any], None]) -> None:
        super().__init__(turns)
        self._note = note

    def append(self, turn: Any) -> None:
        super().append(turn)
        self._note(turn)

    def __setitem__(self, index: Any, turn: Any) -> None:
        super().__setitem__(index, turn)
        if not isinstance(index, slice):
            # A result put in the place its call's placeholder held.
            self._note(turn)


class SubAgentLog:
    """Every tool call the sub-agents of one rollout issued, in the order each
    sub-agent issued its own.

    *tool_definition* gives the definition of a tool by name, so that the
    arguments are logged as the tool got them, like the session's.
    """

    def __init__(self, tool_definition: Callable[[str], dict[str, Any] | None]) -> None:
        self._tool_definition = tool_definition
        self._lock = threading.Lock()
        # One per sub-agent, in the order they were started: the id of the
        # session's call that started it, and its calls.
        self._agents: list[dict[str, Any]] = []

    def watch(
        self,
        turns: list[Any],
        parent_call_id: str | None,
        parent_turn: int | None = None,
        parent_place: int | None = None,
    ) -> list[Any]:
        """A list to run a sub-agent on in place of *turns*: the same turns,
        with what the sub-agent adds noted here.

        *parent_call_id* is the id of the session's call that starts the
        sub-agent, *parent_turn* the session's turn that made it and
        *parent_place* which of that turn's calls it is."""
        with self._lock:
            agent: dict[str, Any] = {
                "agent": len(self._agents),
                "parent_call_id": parent_call_id,
                "parent_turn": parent_turn,
                "parent_place": parent_place,
                "turn": -1,
                "calls": [],
                "by_id": {},
            }
            self._agents.append(agent)
        return _WatchedTurns(turns, lambda turn: self._note(agent, turn))

    def _note(self, agent: dict[str, Any], turn: Any) -> None:
        try:
            with self._lock:
                if turn.role is Role.ASSISTANT:
                    agent["turn"] += 1
                    for call in turn.tool_calls:
                        self._call(agent, call)
                elif turn.role is Role.TOOL and turn.tool_call_id in agent["by_id"]:
                    entry = agent["by_id"][turn.tool_call_id]
                    entry["ok"] = not turn.is_error
                    entry["result"] = _text(turn)[:_RESULT_CHARS]
        except Exception as error:  # the record may miss a call; the sub-agent goes on
            log.warning("rollout.subagent_log_failed", error=f"{type(error).__name__}: {error}")

    def _call(self, agent: dict[str, Any], call: Any) -> None:
        name = str(call.name or "").strip()
        arguments: Any
        try:
            arguments = json.loads(call.arguments or "{}")
        except (ValueError, RecursionError):
            arguments = {"_raw": call.arguments}
        if isinstance(arguments, dict):
            apply_null_spellings(arguments, self._tool_definition(name))
        else:
            arguments = {"_raw": arguments}
        entry = {
            "agent": agent["agent"],
            "turn": agent["turn"],
            "call_id": call.id,
            "tool": name,
            "args": arguments,
            # Until its result is in: a call the run ended on has none.
            "result": None,
            "ok": None,
        }
        agent["calls"].append(entry)
        agent["by_id"][call.id] = entry

    def agents(self) -> list[dict[str, Any]]:
        """For each sub-agent started, in the order they were started, the
        session's call that started it: its id (``call_id``), the turn that
        made it (``turn``) and which of that turn's calls it is (``place``)."""
        with self._lock:
            return [
                {
                    "call_id": agent["parent_call_id"],
                    "turn": agent["parent_turn"],
                    "place": agent["parent_place"],
                }
                for agent in self._agents
            ]

    def entries(self) -> list[dict[str, Any]]:
        """The calls so far, sub-agent by sub-agent, as JSON-safe copies."""
        with self._lock:
            calls = [dict(entry) for agent in self._agents for entry in agent["calls"]]
        plain: list[dict[str, Any]] = json.loads(json.dumps(calls, default=str))
        return plain


def _text(turn: Any) -> str:
    """The text of a tool turn; content that is no text is named, not read."""
    blocks = [block for block in turn.content if isinstance(block, TextBlock)]
    parts = [block.text for block in blocks if isinstance(block.text, str)]
    if not parts and turn.content:
        return "[non-text result]"
    return chr(10).join(parts)
