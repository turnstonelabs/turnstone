"""What one rollout spends, and whose each second of it is.

A rollout's time limit can be used up by both sides: by the model, with long
replies and slow commands, and by the server, by being slow.  With the time
limit as the only limit the two cannot be told apart, so a slow server ends
rollouts as the model's own timeout, and under load that falls on the long
episodes.

So a task can give the model budgets of its own, in units the server's speed
does not change:

- ``max_generated_tokens``: the tokens the session and its sub-agents may
  generate, together;
- ``max_command_seconds``: the time the model's commands may run.

Running out of either is the model's outcome.  The time limit then stops being
the model's budget and becomes a backstop for the machinery: a run that
reaches it with budget left was held up by the server or by the harness.

Whether it was is measured, not assumed, because one thing the model asks for
is in neither budget: the server's work on prompts.  Every instant of a run
is one of three things, in this order: a model call is under way (the
server's time), or else a tool call is (the commands' time), or else neither
(the harness's own).  At the backstop the server's time is compared with what
it would take at a configured minimum speed to process the prompt tokens it
was sent and generate the tokens it produced.  A server that took longer was
too slow, and so was a harness that took more than a small allowance: the
record is then not trainable.  A server that kept the minimum speed did what
it was asked in the time that takes, and what was asked is the model's doing:
the run ends as the model's ``timeout``.

:class:`Meter` does the counting.  The recording client asks it before every
request (:meth:`Meter.admit`), which is where the token budget is kept: each
request's reply limit is cut to what is left, counting what requests still
under way may yet generate, so sub-agents running side by side cannot
overshoot together; a request with nothing left is refused.
"""

from __future__ import annotations

import contextlib
import math
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from turnstone.rollout.recording import CallRecord

# Seconds of the server's time one request is allowed besides its tokens: the
# request's way there and back.
_PER_CALL_SECONDS = 1.0
# The harness's own time between calls: a fixed part and a part per model
# call and per batch of tool calls.
_HARNESS_SECONDS = 30.0
_HARNESS_SECONDS_PER_STEP = 1.0
# A server processes a prompt this many times faster than it generates, where
# only its generation speed is configured.  A cautious ratio: servers measured
# with this harness were at two hundred and more.
_PROMPT_SPEED_FACTOR = 100.0
# A request that failed this soon without the server having given anything
# never reached it: a client that was already closed, a connection that could
# not be made.
_UNSENT_SECONDS = 0.1


class TokenBudgetError(RuntimeError):
    """The episode's budget of generated tokens is used up: no further model
    request is made."""


@dataclass(frozen=True)
class Budgets:
    """The budgets one rollout runs under.

    With ``max_generated_tokens`` the model has budgets of its own and
    ``wall_seconds`` is a backstop for the machinery; without it
    ``wall_seconds`` is the model's time.
    """

    wall_seconds: int
    max_generated_tokens: int | None = None
    max_command_seconds: float | None = None
    # Tokens a second the server must at least generate, and process of a prompt.
    min_tokens_per_second: float | None = None
    min_prompt_tokens_per_second: float | None = None
    context_window: int = 0

    @property
    def backstop(self) -> bool:
        """Whether the wall clock is a backstop for the machinery."""
        return self.max_generated_tokens is not None

    @property
    def prompt_speed(self) -> float | None:
        """The minimum speed for prompts: the configured one, else a multiple
        of the minimum generation speed."""
        if self.min_prompt_tokens_per_second is not None:
            return self.min_prompt_tokens_per_second
        if self.min_tokens_per_second is not None:
            return self.min_tokens_per_second * _PROMPT_SPEED_FACTOR
        return None

    def describe(self) -> dict[str, Any]:
        return {
            "wall_seconds": self.wall_seconds,
            "wall_clock": "backstop for the machinery" if self.backstop else "the model's time",
            "max_generated_tokens": self.max_generated_tokens,
            "max_command_seconds": self.max_command_seconds,
            "min_tokens_per_second": self.min_tokens_per_second,
            "min_prompt_tokens_per_second": self.prompt_speed,
        }


def backstop_seconds(
    *,
    max_generated_tokens: int,
    min_tokens_per_second: float,
    max_command_seconds: float = 0.0,
    requests: int = 1,
    context_window: int = 0,
    min_prompt_tokens_per_second: float | None = None,
    slack: float = 60.0,
) -> int:
    """A wall-clock backstop that a server keeping its minimum speed does not
    reach before the model's budgets are used up.

    The time to generate the whole token budget at the minimum speed, the
    whole command time, and for *requests* requests the time to process a
    prompt as long as the context window, plus *slack*.  A model that makes
    more requests than that with long prompts can still reach it; that is
    then its own ``timeout``, because the server's speed is measured against
    what it was asked to do (:meth:`Meter.at_the_backstop`).
    """
    if min_tokens_per_second <= 0:
        raise ValueError("the minimum server speed must be above zero")
    prompt_speed = min_prompt_tokens_per_second or min_tokens_per_second * _PROMPT_SPEED_FACTOR
    per_request = context_window / prompt_speed + _PER_CALL_SECONDS + _HARNESS_SECONDS_PER_STEP
    return math.ceil(
        max_generated_tokens / min_tokens_per_second
        + max_command_seconds
        + requests * per_request
        + _HARNESS_SECONDS
        + slack
    )


def _covered(intervals: list[tuple[float, float]]) -> float:
    """The length of time covered by at least one of *intervals*."""
    total = 0.0
    reach = -math.inf
    for start, end in sorted(intervals):
        if end <= reach:
            continue
        total += end - max(start, reach)
        reach = end
    return total


class Meter:
    """Counts what one rollout spends: generated tokens, and time by whose it is.

    *calls* returns every model call recorded so far, the session's own and
    its sub-agents', those still under way included.
    """

    def __init__(
        self,
        budgets: Budgets,
        calls: Callable[[], list[CallRecord]],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.budgets = budgets
        self._calls = calls
        self._clock = clock
        self._lock = threading.Lock()
        # Tool batches of the session as [start, end]; end is None while one runs.
        self._batches: list[list[float | None]] = []
        # The finish state a used-up budget gave the run, and when.
        self.ended: str | None = None
        self.ended_at: float | None = None

    # -- tokens --------------------------------------------------------------

    @staticmethod
    def _generated_by(call: CallRecord) -> int:
        counted = (call.usage or {}).get("completion_tokens") or 0
        return max(len(call.token_ids), counted)

    def generated(self) -> int:
        """Tokens generated so far, by the session and its sub-agents."""
        return sum(self._generated_by(call) for call in self._calls())

    def _spoken_for(self, but: CallRecord | None = None) -> int:
        """Tokens generated, plus what calls still under way may yet generate."""
        total = 0
        for call in self._calls():
            if call is but:
                continue
            made = self._generated_by(call)
            under_way = call.ended_at is None and call.token_grant is not None
            total += max(made, call.token_grant or 0) if under_way else made
        return total

    def left(self) -> int | None:
        """Tokens of the budget that nothing has generated or may yet generate;
        ``None`` without a token budget."""
        budget = self.budgets.max_generated_tokens
        return None if budget is None else max(budget - self._spoken_for(), 0)

    def admit(self, call: CallRecord, request: dict[str, Any]) -> None:
        """Cut *request*'s reply limit to what the token budget has left, and
        note the grant on *call*.  Raises :class:`TokenBudgetError` when
        nothing is left."""
        budget = self.budgets.max_generated_tokens
        if budget is None:
            return
        keys = [k for k in ("max_tokens", "max_completion_tokens") if k in request]
        asked = [request[k] for k in keys if isinstance(request[k], int)]
        with self._lock:
            left = budget - self._spoken_for(but=call)
            if left <= 0:
                # Refused, not the end of the run: calls still under way may
                # leave some of what they were granted.  The session ends the
                # run when it finds nothing left for a request of its own.
                raise TokenBudgetError(
                    f"the episode's budget of {budget:,} generated tokens is used up"
                )
            grant = min([left, *asked])
            call.token_grant = grant
            # Less than the request would have had without the budget.
            call.token_grant_cut = not asked or grant < min(asked)
        for key in keys or ["max_tokens"]:
            request[key] = grant

    def _end(self, state: str) -> None:
        if self.ended is None:
            self.ended, self.ended_at = state, self._clock()

    def end(self, state: str) -> None:
        """Note that a used-up budget ended the run as *state*."""
        with self._lock:
            self._end(state)

    # -- time ----------------------------------------------------------------

    @contextlib.contextmanager
    def tool_batch(self) -> Iterator[None]:
        """Around one batch of the session's tool calls."""
        entry: list[float | None] = [self._clock(), None]
        with self._lock:
            self._batches.append(entry)
        try:
            yield
        finally:
            entry[1] = self._clock()

    def split(self, since: float, now: float) -> dict[str, float]:
        """Where the time from *since* to *now* went, in seconds: ``model``
        while a model call was under way, ``commands`` while a tool call was
        and no model call, ``harness`` for the rest."""
        calls = [
            (max(c.started_at, since), min(c.ended_at if c.ended_at is not None else now, now))
            for c in self._calls()
            if c.started_at
        ]
        with self._lock:
            batches = [
                (max(b[0] or since, since), min(b[1] if b[1] is not None else now, now))
                for b in self._batches
            ]
        model = _covered([i for i in calls if i[1] > i[0]])
        either = _covered([i for i in calls + batches if i[1] > i[0]])
        return {
            "model": model,
            "commands": either - model,
            "harness": max(now - since - either, 0.0),
        }

    def command_seconds(self, since: float, now: float) -> float:
        return self.split(since, now)["commands"]

    def over_command_time(self, since: float, now: float) -> bool:
        """Whether the model's commands have run for longer than they may."""
        limit = self.budgets.max_command_seconds
        return limit is not None and self.command_seconds(since, now) >= limit

    # -- the server's speed ---------------------------------------------------

    def _prompt_tokens(self, call: CallRecord) -> int:
        """The tokens of a call's prompt: as the server counted them, or the
        most they can be while it has not said."""
        if call.prompt_token_ids is not None:
            return len(call.prompt_token_ids)
        counted = (call.usage or {}).get("prompt_tokens")
        if counted:
            return counted
        window = self.budgets.context_window
        return min(call.prompt_bytes, window) if window else call.prompt_bytes

    @staticmethod
    def _asked(call: CallRecord) -> bool:
        """Whether *call* asked anything of the server.

        Every call has a record, also one that never went out: a request the
        token budget refused or whose prompt could not be put together has no
        request to show, and one made on a client that was already closed, as
        the session's next try after the time limit is, failed at once without
        the server having given anything.  Neither is work of the server's.  A
        call still under way counts, and so does one that failed after it had
        gone out, however little came back.
        """
        if not call.started_at:
            return False
        if call.ended_at is None:
            return True
        if not call.request:
            return False
        heard = call.chunks > 0 or call.last_heard_at > call.started_at
        unsent = call.ended_at - call.started_at < _UNSENT_SECONDS
        return not (call.error and not heard and unsent)

    def work(self, now: float) -> dict[str, Any]:
        """What the server was asked to do and how long it took over it."""
        prompt_tokens = generated = calls = 0
        prompt_seconds = generation_seconds = 0.0
        timed_prompt_tokens = timed_generated = 0
        for call in self._calls():
            if not self._asked(call):
                continue
            calls += 1
            tokens, made = self._prompt_tokens(call), self._generated_by(call)
            prompt_tokens += tokens
            generated += made
            if call.first_token_at is not None:
                end = call.ended_at if call.ended_at is not None else now
                prompt_seconds += call.first_token_at - call.started_at
                generation_seconds += max(end - call.first_token_at, 0.0)
                timed_prompt_tokens += tokens
                timed_generated += made
        return {
            "model_calls": calls,
            "prompt_tokens": prompt_tokens,
            "generated_tokens": generated,
            "prompt_seconds": round(prompt_seconds, 2),
            "generation_seconds": round(generation_seconds, 2),
            # Measured: over the calls that got as far as a first token.
            "generation_tokens_per_second": (
                round(timed_generated / generation_seconds, 2) if generation_seconds > 0 else None
            ),
            "prompt_tokens_per_second": (
                round(timed_prompt_tokens / prompt_seconds, 2) if prompt_seconds > 0 else None
            ),
        }

    def allowed_model_seconds(self, now: float) -> float | None:
        """The time a server at its minimum speeds takes for what it was asked
        to do so far; ``None`` when no minimum speed is configured."""
        generation, prompt = self.budgets.min_tokens_per_second, self.budgets.prompt_speed
        if generation is None or prompt is None:
            return None
        work = self.work(now)
        return float(
            work["prompt_tokens"] / prompt
            + work["generated_tokens"] / generation
            + work["model_calls"] * _PER_CALL_SECONDS
        )

    def at_the_backstop(self, since: float, now: float) -> tuple[str, dict[str, Any]]:
        """Whose doing it is that the wall clock was reached with budget left.

        ``("backstop", ...)``: the server was slower than its configured
        minimum, or the harness took more than its allowance: the machinery's
        failure.  ``("timeout", ...)``: the server did what it was asked at
        its minimum speed or faster, so what took the time is what the model
        asked for: its own outcome.
        """
        split = self.split(since, now)
        allowed = self.allowed_model_seconds(now)
        with self._lock:
            steps = len(self._batches)
        work = self.work(now)
        harness_allowed = _HARNESS_SECONDS + _HARNESS_SECONDS_PER_STEP * (
            steps + work["model_calls"]
        )
        detail: dict[str, Any] = {
            "model_seconds": round(split["model"], 2),
            "model_seconds_at_minimum_speed": None if allowed is None else round(allowed, 2),
            "harness_seconds": round(split["harness"], 2),
            "harness_seconds_allowed": round(harness_allowed, 2),
        }
        if allowed is None or split["model"] > allowed:
            return "backstop", {
                **detail,
                "waiting_on": "model",
                "cause": "the model server was slower than its configured minimum speed",
            }
        if split["harness"] > harness_allowed:
            return "backstop", {
                **detail,
                "waiting_on": "harness",
                "cause": "the harness itself took the time",
            }
        return "timeout", {
            **detail,
            "waiting_on": "model",
            "cause": "the server kept its minimum speed: what took the time is what the "
            "model asked of it",
        }
