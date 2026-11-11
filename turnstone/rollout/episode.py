"""Run one task as one rollout and return what a trainer needs.

:func:`run_episode` drives the headless session loop under the eval harness's
per-run lifecycle, with what a rollout needs switched on:

- one attempt (a second attempt would sample the model again and repeat the
  first attempt's actions), and the task's time limit bounding the run;
- ``stable_prefix`` with the arguments of each tool call sent back as the text
  the model wrote, so every request extends the previous one down to the
  tokens;
- an execution environment that is one container for this run, so the model's
  shell and file tools act there and nowhere else;
- a recording client, so every model call's prompt token IDs, generated token
  IDs and log-probabilities are kept.

The episode record holds the task, the tool-call log, the final messages, the
finish state and the token record (:mod:`turnstone.rollout.tokens`), and says
two separate things about itself: ``trainable`` (every model turn's tokens and
log-probabilities are sound and were sampled the way the record says) and
``one_sequence`` (that, and every prompt extended the previous one).

A sub-agent is a tool like any other for the sample: the call that spawned it
is the session's own and is recorded, its result is text from the environment,
and its conversation is not part of the token record.  It is kept as a side
record marked not for training.

One episode at a time per process: the session's storage is a process-wide
singleton that the lifecycle initialises and resets around each run.  Run
episodes in parallel as separate processes.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import threading
import time
from dataclasses import asdict, dataclass, field, replace
from typing import TYPE_CHECKING, Any, Protocol

from openai import APIStatusError, DefaultHttpxClient, OpenAI

from turnstone.core.completion_recovery import EmptyCompletionError, is_context_overflow
from turnstone.core.log import get_logger
from turnstone.core.model_turn import ModelContextLimitError
from turnstone.core.output_guard import ControllerText
from turnstone.core.providers._http_client import shutdown_on_close
from turnstone.core.session import GenerationCancelled, inside_task_agent
from turnstone.core.storage import init_storage, reset_storage
from turnstone.core.tools import apply_null_spellings, salvage_tool_arguments
from turnstone.core.trajectory import final_assistant_text
from turnstone.eval.core import HeadlessSession, run_with_lifecycle
from turnstone.rollout.budget import Budgets, Meter, TokenBudgetError, backstop_seconds
from turnstone.rollout.container import (
    Container,
    ContainerEnvironment,
    ContainerError,
    ContainerLostError,
    ContainerSpec,
)
from turnstone.rollout.preparation import BeforeRunCommand, before_run_failure, run_before
from turnstone.rollout.probe import SETTINGS as PROBED_SETTINGS
from turnstone.rollout.probe import all_settings, sampler_logprobs_reported
from turnstone.rollout.rawargs import RecordedArguments
from turnstone.rollout.recording import PARENT, SUB_AGENT, RecordingClient
from turnstone.rollout.rules import RuleSet, refusal_text
from turnstone.rollout.settings import (
    Limit,
    Setting,
    SetupError,
    resolve_limits,
    resolve_sampling,
)
from turnstone.rollout.sidecar import RunningSidecar, Sidecar, SidecarError
from turnstone.rollout.tokens import build_token_record, describe_break, verify_record
from turnstone.rollout.toolset import ROLLOUT_TOOL_NAMES
from turnstone.rollout.verification import (
    AfterRunCommand,
    VerificationContext,
    build_archive,
    run_after,
    run_verifier,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from turnstone.core.exec_env import ExecutionEnvironment

log = get_logger(__name__)

DEFAULT_SYSTEM_PROMPT = (
    "You are an agent working in a Linux sandbox. Use the tools to do what the user asks, "
    "then reply with a short summary of what you did."
)

# A container is removed from outside by a keeper of its own when the runner
# is gone.  The keeper's time is set for the step under way: as long as that
# step may take, and this many seconds on top.
_KEEP_MARGIN = 600
# How long bringing a sandbox up may take: the container, its checks and the
# task's files.  A sidecar's own time limits come on top.
_STARTUP_SECONDS = 900
# What one after-run command takes besides its own time limit, when every part
# takes as long as it may: waiting for the ends of commands still under way
# and for a look of the size watch, ending what is left running, removing the
# helper, letting the container run again, and reading out the files it names.
_AFTER_RUN_OVERHEAD = 330
# What one before-run command takes besides its own time limit: reaching the
# container through the runtime.
_BEFORE_RUN_OVERHEAD = 30

# Seconds a session is given to stop once it was told to.
_CANCEL_SECONDS = 10.0
# Tokens a second a server is taken to process of a prompt where nothing says.
_DEFAULT_PROMPT_SPEED = 1000.0

# Request-body keys that are not sampler settings.
_NOT_SAMPLER_KEYS = frozenset(
    {"chat_template_kwargs", "return_token_ids", "return_tokens_as_token_ids"}
)


@dataclass
class Task:
    """One task: what the user asks, what the sandbox starts with, and the limits."""

    prompt: str
    key: str = ""
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    # Files written into the sandbox's working directory before the first turn:
    # text, and any content as bytes.
    setup_files: dict[str, str] = field(default_factory=dict)
    files: dict[str, bytes] = field(default_factory=dict)
    max_turns: int = 15
    # Wall-clock limit in seconds for the whole session run, sub-agents
    # included.  Bringing the sandbox up comes before it and removing it after.
    # Without a token budget it is the model's time.  With one it is a backstop
    # for the machinery, and ``None`` then derives it from the budgets and the
    # server's minimum speed (:func:`turnstone.rollout.budget.backstop_seconds`).
    timeout: int | None = 300
    # The model's own budgets (:mod:`turnstone.rollout.budget`), in units a slow
    # server does not use up: the tokens the session and its sub-agents may
    # generate together, and the seconds the model's commands may run.  Running
    # out of either is the model's outcome.
    max_generated_tokens: int | None = None
    max_command_seconds: float | None = None
    # The task's container: image, working directory, the user its commands run
    # as, what is writable, capabilities, environment, limits.
    container: ContainerSpec = field(default_factory=ContainerSpec)
    # Commands run in the container once the task's files are in place and
    # before the first model call, as root unless one says otherwise
    # (:mod:`turnstone.rollout.preparation`).  The model sees nothing of them;
    # their time is start-up time; one that fails means the run does not start.
    before_run: list[BeforeRunCommand] = field(default_factory=list)
    # Commands run in the container once the model has finished, before the
    # container is stopped; their results go in the record's ``verification``.
    # See :mod:`turnstone.rollout.verification` for when that is enough.
    after_run: list[AfterRunCommand] = field(default_factory=list)
    # A second container with the task's own servers, reached as MCP tools
    # (:mod:`turnstone.rollout.sidecar`).
    sidecar: Sidecar | None = None


@dataclass
class RolloutConfig:
    """The model endpoint, the sampling settings and the sandbox limits.

    What the framework already has a setting for is ``None`` here and is then
    taken from it (:mod:`turnstone.rollout.settings`): the model's definition,
    the configured setting, the registered default.  A value given here is an
    explicit override.  The record lists the effective values and where each
    came from.
    """

    base_url: str
    model: str
    api_key: str = "none"
    # ``None``: the framework's for this model (its definition, then
    # ``[model] temperature``), and 1 when nothing names one.
    temperature: float | None = None
    # Reply limit per model call; ``None``: the framework's for this model.
    # Never more than a quarter of the context window.
    max_tokens: int | None = None
    # ``None``: the framework's for this model, which may be none at all.
    reasoning_effort: str | None = None
    # ``None``: the model's definition, else what its endpoint reports.  A
    # window nothing names is refused, not guessed.
    context_window: int | None = None
    # Merged into every request's body: sampler settings the SDK has no keyword
    # for (``top_k``, ``top_p``) and chat-template arguments.  The model
    # definition's own extra request parameters go under these.
    extra_body: dict[str, Any] = field(default_factory=dict)
    # Sampler settings a trainer needs to reproduce the sampling distribution.
    # One the requests leave to the server's default makes the rollout not
    # trainable, because the record cannot say what it was.
    required_sampling: tuple[str, ...] = ("temperature", "top_k", "top_p")
    # Whether the server reports log-probabilities of the sampling distribution
    # (``True``) or the model's own (``False``), or that per sampler setting
    # (the ``reflects`` of a probe).  ``None`` asks the server with a probe
    # before the run; pass the verdict of one probe to a whole batch.
    sampler_logprobs: bool | dict[str, bool | None] | None = None
    # A call whose log-probabilities the server did not name by token ID was
    # checked by counts only.  ``False`` accepts such calls; the record says so.
    require_token_id_check: bool = True
    # How the server writes tool calls in the generated text, for sending each
    # argument back as the model wrote it: ``"parameter_blocks"`` or ``"none"``.
    argument_text: str = "parameter_blocks"
    # Seconds one tool call may run when the model names no limit of its own;
    # ``None``: the framework's ``tools.timeout``.
    tool_timeout: int | None = None
    # Tools to offer; ``None`` offers the tools that act inside the sandbox and
    # the sub-agent tool.
    tool_names: frozenset[str] | None = None
    # Sub-agents: the most one rollout may start (a compute backstop, not a
    # budget the model is told about), and the turns one may take (``None``:
    # the framework's ``tools.agent_max_turns``, where -1 is no limit).
    max_subagents: int = 16
    subagent_max_turns: int | None = None
    # Characters of a command's output the model gets (head and tail kept);
    # ``None``: the framework's ``tools.truncation``, where 0 is its automatic
    # share of the remaining context.
    max_output_chars: int | None = None
    # The pattern rules enforced on tool calls (:mod:`turnstone.rollout.rules`):
    # ``"shipped"`` for the rollout package's own set, a rule set of the run's,
    # or ``None`` for none.
    rules: RuleSet | str | None = "shipped"
    # A run that reaches its time limit while a model request has had nothing
    # from the server for this long ended on the server, not on the model.
    # A request whose prompt the server is still working on is given the time
    # that takes at the minimum prompt speed on top.
    server_stall_seconds: float = 60.0
    # Tokens a second the server must at least generate, and process of a
    # prompt (``None``: a hundred times the generation speed, or 1000 where
    # neither is named).  With a token budget the time limit is a backstop for
    # the machinery, and these say when the machinery was too slow.
    min_tokens_per_second: float | None = None
    min_prompt_tokens_per_second: float | None = None
    # Largest file a file tool reads whole, and the longest text an edit may make.
    max_read_bytes: int = 256 * 1024
    # Keep the generated text of every call in the record.
    keep_text: bool = False


class HostExecutionError(ValueError):
    """A rollout was given an execution environment that acts on the machine it
    runs on, and its caller did not say that this is meant."""


_HOST_REFUSAL = (
    "a rollout's tools must act in a sandbox, and this execution environment acts on the "
    "machine the rollout runs on: every command the model writes would run here. Pass "
    "allow_host_execution=True only where every command is the caller's own"
)


def _acts_on_host(environment: Any) -> bool:
    """Whether *environment* acts on this machine; one that does not say does."""
    return environment is None or bool(getattr(environment, "acts_on_host", True))


class ContextFullError(Exception):
    """The next request could not fit the model's context window.

    The conversation filled the window: what the model generated and what it
    asked its tools for.  The rollout ends there, as the model's outcome.
    """


class Sandbox(Protocol):
    """Where one rollout's tools act.  :class:`ContainerSandbox` is the real one."""

    @property
    def lost(self) -> str | None:
        """Why the sandbox stopped while the run needed it, ``None`` while it is fine."""
        ...

    @property
    def lost_cause(self) -> str | None:
        """Whose doing a lost sandbox is: ``"model"`` when only something done
        inside it explains the stop, ``"machinery"`` otherwise."""
        ...

    def notify_lost(self, callback: Callable[[], None]) -> None:
        """Have *callback* called once, when the sandbox is first found gone."""
        ...

    def start(self) -> ExecutionEnvironment: ...

    def put_files(self, files: dict[str, str | bytes]) -> None: ...

    def close(self) -> bool:
        """Remove the sandbox; ``True`` once its absence is confirmed.  Never raises."""
        ...

    def describe(self) -> dict[str, Any]: ...


class ContainerSandbox:
    """One container for one rollout (:mod:`turnstone.rollout.container`)."""

    def __init__(self, task: Task, config: RolloutConfig) -> None:
        self._config = config
        # Kept from outside for as long as bringing the sandbox up may take;
        # ``keep_for`` sets the time again for each step that follows.
        lifetime = _STARTUP_SECONDS + _KEEP_MARGIN
        if task.sidecar is not None:
            lifetime += task.sidecar.setup_timeout + task.sidecar.wait_timeout
        self.container = Container(task.container, max_lifetime=lifetime)
        self.sidecar: RunningSidecar | None = None
        if task.sidecar is not None:
            self.sidecar = RunningSidecar(task.sidecar, max_lifetime=lifetime)
        self.environment: ContainerEnvironment | None = None

    def keep_for(self, seconds: float) -> None:
        """Have the containers removed from outside *seconds* from now if the
        runner is gone by then: the step now starting may take that long."""
        self.container.keep_for(seconds)
        if self.sidecar is not None:
            self.sidecar.keep_for(seconds)

    @property
    def lost(self) -> str | None:
        return self.container.lost

    @property
    def lost_cause(self) -> str | None:
        return self.container.lost_cause

    def notify_lost(self, callback: Callable[[], None]) -> None:
        self.container.on_lost = callback

    def start(self) -> ExecutionEnvironment:
        self.container.start()
        self.environment = ContainerEnvironment(
            self.container,
            op_timeout=min(self._config.tool_timeout or 120, 60),
            max_file_bytes=self._config.max_read_bytes,
        )
        if self.sidecar is not None:
            self.sidecar.start()
        return self.environment

    @property
    def mcp_client(self) -> Any:
        """The MCP client for this rollout's sidecar, ``None`` without one."""
        return self.sidecar.mcp_client if self.sidecar is not None else None

    def sidecar_lost(self) -> str | None:
        return self.sidecar.lost() if self.sidecar is not None else None

    def put_files(self, files: dict[str, str | bytes]) -> None:
        assert self.environment is not None
        text = {path: content for path, content in files.items() if isinstance(content, str)}
        self.environment.put_files(text)
        data = {path: content for path, content in files.items() if isinstance(content, bytes)}
        if data:
            # Unpacked in the working directory as the model's user, who owns them.
            done = self.container.exec(
                ["tar", "-x", "-f", "-"], stdin_data=build_archive(data), timeout=300
            )
            if done.returncode != 0:
                raise ContainerError(
                    f"the task's files could not be put in: {done.stderr_text[:300]}"
                )

    @property
    def name(self) -> str:
        return self.container.name

    def before_run(self, command: BeforeRunCommand) -> dict[str, Any]:
        """Run one before-run command in the container, before the model starts."""
        return run_before(self.container, command)

    def after_run(self, command: AfterRunCommand) -> dict[str, Any]:
        """Run one after-run command in the still running container.

        The end of a command that ran past its time is a command in the
        container too.  One that is still being started when the container is
        held still, to end what was left running, keeps the runtime from
        listing the container's processes; so each is waited for first, for as
        long as one may take.
        """
        if self.environment is not None:
            self.environment.settle(self.environment.kill_timeout + 2.0)
        return run_after(self.container, command)

    def stop(self) -> bool:
        """End everything in the container, and in the sidecar, and keep both
        to be read from."""
        if self.environment is not None:
            self.environment.settle(5.0)
        stopped = self.container.stop()
        if self.sidecar is not None:
            stopped = self.sidecar.stop() and stopped
        return stopped

    def state(self) -> dict[str, Any]:
        return self.container.state()

    def confirm_running(self) -> bool:
        """Ask the runtime whether the container is running now.  One found
        gone is marked lost, as by a command that finds it gone."""
        try:
            self.container.ensure_running()
        except ContainerLostError:
            return False
        return True

    def close(self) -> bool:
        if self.environment is not None:
            self.environment.settle(5.0)
        removed = self.container.close()
        if self.sidecar is not None:
            removed = self.sidecar.close() and removed
        return removed

    def describe(self) -> dict[str, Any]:
        described = self.container.describe()
        if self.sidecar is not None:
            described["sidecar"] = self.sidecar.describe()
            # ``removed`` speaks for both.
            described["removed"] = described["removed"] and described["sidecar"]["removed"]
        return described


# What a sub-agent can do to itself.  Any other exception out of a sub-agent is
# the machinery's: a failed request, a harness error.
_SUBAGENT_OWN_DOING = (EmptyCompletionError, ModelContextLimitError, TokenBudgetError)

# The statuses a server answers with when it refuses a request for what the
# request holds: a conversation its template will not render, a body it cannot
# read.  Every other failed request says something about the server or the way
# to it (an error of its own, too many requests, no such model, no right to
# ask, no answer in time), and is the machinery's.
_REFUSED_FOR_CONTENT = frozenset({400, 422})
# Characters of a server's own words kept in a tool result and in the record.
_REFUSAL_CHARS = 300


def _causes(error: BaseException) -> list[BaseException]:
    """*error* and what caused it, link by link, also where a wrapper keeps its
    cause under another name.

    The session wraps what a sub-agent raises once it has earlier text to hand
    back, so the outermost exception says nothing about the kind of failure.
    """
    chain: list[BaseException] = []
    link: BaseException | None = error
    while link is not None and len(chain) < 16:
        chain.append(link)
        kept = getattr(link, "error", None)
        # An attribute of that name that is no exception is not a cause.
        link = link.__cause__ or (kept if isinstance(kept, BaseException) else None)
    return chain


def _sub_agents_own_doing(error: BaseException) -> bool:
    """Whether what a sub-agent raised is something it did to itself: its model
    returned nothing, it filled its own context window, or the episode's token
    budget was used up when it asked for more.

    Read from the whole chain of causes.
    """
    for link in _causes(error):
        if isinstance(link, _SUBAGENT_OWN_DOING):
            return True
        # A server's refusal of a prompt that is too long, told by its text.
        # Not asked of the session's own wrapper, whose text quotes the model.
        if type(link) is not RuntimeError and is_context_overflow(link):
            return True
    return False


def _refusal(error: BaseException) -> APIStatusError | None:
    """The server's refusal of a request for what the request holds, where that
    is what *error* is or was caused by; ``None`` for every other failure.

    Told by the kind of the exception and its status, anywhere in the chain of
    causes, never by a text: a text can quote the model.
    """
    for link in _causes(error):
        if isinstance(link, APIStatusError) and link.status_code in _REFUSED_FOR_CONTENT:
            return link
    return None


def _server_words(error: APIStatusError) -> str:
    """What the server said when it refused, on one line and bounded."""
    body = error.body
    message = body.get("message") if isinstance(body, dict) else body
    if isinstance(message, str) and message.strip():
        said = message
    else:
        # No message of its own: what it answered with, as it came.
        try:
            said = json.dumps(body) if body is not None else str(error)
        except (TypeError, ValueError, RecursionError):
            said = str(error)
    said = " ".join(said.split())
    return said if len(said) <= _REFUSAL_CHARS else said[:_REFUSAL_CHARS] + "..."


class SubAgentRefusedError(RuntimeError):
    """The server refused a sub-agent's request for what the request holds.

    The sub-agent's conversation begins with the prompt the model wrote for it
    and goes on with what its own model generated, so a conversation the server
    will not take is the model's doing.  The session hands the model this
    error's text as the failed result of its delegation.
    """


def _refused_text(whose: str, refusal: APIStatusError) -> str:
    """The one wording for a conversation the server refused, in a tool result
    and in the record: whose it was, the status, and the server's words."""
    return (
        f"the model server refused {whose} conversation (HTTP {refusal.status_code}): "
        f"{_server_words(refusal)}"
    )


class RolloutSession(HeadlessSession):
    """The headless session of a rollout: sub-agents counted and watched, and
    the context window kept.

    A sub-agent is the session's own tool.  What this class adds is what a
    rollout has to know about it:

    - how many were spawned, and a refusal with a plain result once the run's
      limit is reached (a compute backstop);
    - whether one failed for a reason that is not the model's doing.  The
      session reports any sub-agent failure to the model as a failed tool
      result.  One the sub-agent brought on itself (its model returned
      nothing, it filled its context, the server would not take its
      conversation) is exactly that, an ordinary result.  A request that
      failed for the server's own reasons or a harness error is not: a rollout
      must not score it as the model's failure, so such a run is marked and
      ended.

    It also ends the run, as the model's own outcome, before a request that
    could not fit the context window (:class:`ContextFullError`): without that, the
    server's refusal of an overlong prompt would look like a server failure.

    It refuses an execution environment that acts on the machine the session
    runs on (:class:`HostExecutionError`) unless ``allow_host_execution`` says
    that this is meant: the commands are the model's.
    """

    def __init__(
        self,
        *,
        max_subagents: int,
        last_call_tokens: Callable[[], int | None],
        rules: RuleSet | None = None,
        allow_host_execution: bool = False,
        meter: Meter | None = None,
        before_acting: Callable[[], None] | None = None,
        **kwargs: Any,
    ) -> None:
        if kwargs.get("judge_config") is not None:
            # Its model-based tier would make model calls of its own, on a
            # client the recorder does not see, and read this machine's files.
            raise ValueError("a rollout does not run the intent judge; give it rules instead")
        if _acts_on_host(kwargs.get("exec_env")) and not allow_host_execution:
            # The model is not a caller to be trusted with this machine, and
            # the pattern rules are not what keeps it off.
            raise HostExecutionError(_HOST_REFUSAL)
        # The tools of the task's own servers, by name.  Set before the base
        # class builds its tool lists, which asks ``_apply_persona_visibility``.
        own_servers = kwargs.get("mcp_client")
        self._own_server_tools: frozenset[str] = frozenset(
            t["function"]["name"] for t in (own_servers.get_tools() if own_servers else [])
        )
        super().__init__(**kwargs)
        # Called once, before the first of the model's tool calls runs.
        self._before_acting = before_acting
        # Counts the run's tokens and time (:mod:`turnstone.rollout.budget`).
        self._meter = meter
        self._rules = rules
        # Every call a rule matched: which rule, what it said, whose call.
        self.rule_events: list[dict[str, Any]] = []
        self._max_subagents = max_subagents
        self._last_call_tokens = last_call_tokens
        self._delegation_lock = threading.Lock()
        self._agent_call = threading.local()
        # Sub-agent calls admitted, calls refused at the limit, and what went
        # wrong with a sub-agent besides its work, in the order it happened:
        # ``whose`` is ``"machinery"`` for a failure of the machinery, which
        # ends the run, and ``"model"`` for a conversation the server refused,
        # which the model gets as a failed tool result.
        self.subagents_admitted = 0
        self.subagents_refused = 0
        self.subagent_failures: list[dict[str, str]] = []
        # Whether it was a failure of the machinery around a sub-agent that
        # told the session to stop, and not something that came before it.
        self._stopped_by_failure = False

    # -- context window ------------------------------------------------------

    def _check_stable_prefix(self, wire: list[dict[str, Any]], lane: Any) -> None:
        super()._check_stable_prefix(wire, lane)
        if self._meter is not None and self._meter.left() == 0:
            # Nothing of the episode's token budget is left for a reply.
            self._meter.end("token_budget")
            raise TokenBudgetError(
                "the episode's budget of generated tokens is used up: "
                f"{self._meter.generated():,} generated by the session and its sub-agents"
            )
        used = self._last_call_tokens()
        if used is None:
            return  # the first request: what the task itself asks for
        # What this request adds to the previous call's prompt and reply: the
        # messages after the last assistant turn.  A token is at least one
        # byte, so their size in bytes bounds their tokens.
        added = 0
        for message in reversed(wire):
            if message.get("role") == "assistant":
                break
            added += len(json.dumps(message, ensure_ascii=False).encode("utf-8", "replace")) + 16
        if used + added + self.max_tokens > self.context_window:
            raise ContextFullError(
                f"the conversation holds {used:,} tokens and up to {added:,} more were added; "
                f"with a reply of up to {self.max_tokens:,} that passes the context window of "
                f"{self.context_window:,}"
            )

    # -- pattern rules -------------------------------------------------------

    def _ensure_judge(self) -> None:
        """A rollout has no intent judge, whatever its configuration says."""
        return None

    def _call_as_it_runs(
        self, tc: dict[str, Any], item: dict[str, Any]
    ) -> tuple[str, dict[str, Any] | None]:
        """The tool and the arguments of a call as it will run, which is not
        always what the model wrote: ``None`` for arguments nothing runs on.

        Between the model's words and the tool the session strips white space
        off the name, makes arguments of text that is no JSON, reads a null
        spelled as text as no value, and writes plain dashes and quotes for
        typographic ones in a command.  Each step is the session's own function
        or what its prepared call holds, so nothing here can drift from it.
        """
        function = tc.get("function") or {}
        name = str(function.get("name") or "").strip()
        raw = function.get("arguments")
        if raw is None or raw == "":
            raw = "{}"
        if not isinstance(raw, str):
            return name, None
        arguments: Any
        try:
            arguments = json.loads(raw)
        except json.JSONDecodeError:
            arguments = salvage_tool_arguments(name, raw)
        except (ValueError, RecursionError):
            return name, None
        if not isinstance(arguments, dict):
            return name, None
        apply_null_spellings(arguments, self._tool_definition(name))
        command = item.get("command")
        if isinstance(command, str):
            # The command the prepared call starts.
            arguments["command"] = command
        return name, arguments

    def _prepare_tool_item(self, tc: dict[str, Any]) -> dict[str, Any]:
        """Ask the run's rules before a call runs, the session's own or a sub-agent's.

        The rules judge the call that will run, not the model's spelling of it:
        a command with typographic dashes runs with plain ones, a tool name
        with white space around it runs the tool, and arguments that are no
        JSON are made into arguments and run.
        """
        item = super()._prepare_tool_item(tc)
        if self._rules is None or item.get("error"):
            return item
        name, arguments = self._call_as_it_runs(tc, item)
        if arguments is None:
            return item
        rule = self._rules.match(name, arguments)
        if rule is None:
            return item
        refused = rule.recommendation == "deny"
        with self._delegation_lock:
            self.rule_events.append(
                {
                    "call_id": tc.get("id"),
                    "tool": name,
                    "rule": rule.name,
                    "recommendation": rule.recommendation,
                    "refused": refused,
                    "scope": SUB_AGENT if inside_task_agent() else PARENT,
                }
            )
        if not refused:
            return item
        return {
            "call_id": tc.get("id"),
            "func_name": name,
            "header": chr(0x2717) + f" {name}: refused by the rule {rule.name}",
            "preview": "",
            "needs_approval": False,
            "error": ControllerText(refusal_text(rule)),
        }

    # -- sub-agents ----------------------------------------------------------

    def _apply_persona_visibility(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Keep the tools of the task's own servers out of a sub-agent's list.

        They act on the task's state, which is what a rollout is scored on.
        Called by the session, each such call is in the tool log and is a
        generated, recorded turn.  A sub-agent's conversation is neither
        logged nor trained on, so it would act on that state unseen.
        """
        visible = super()._apply_persona_visibility(tools)
        own: frozenset[str] = getattr(self, "_own_server_tools", frozenset())
        if not own or tools is not getattr(self, "_task_tools", None):
            return visible
        return [t for t in visible if t.get("function", {}).get("name") not in own]

    def _machinery_failed(self, what: str) -> None:
        with self._delegation_lock:
            if self._cancelled.is_set() and not self._stopped_by_failure:
                # The session had been told to stop before this went wrong: the
                # time limit passed (its client is closed right after, and a
                # sub-agent's call under way dies with it), a budget ran out,
                # or the sandbox went away.  What fails around a sub-agent from
                # then on follows from the stop.  It did not end the run, and
                # whose doing the stop was stays what it is.
                log.info("rollout.subagent_failed_after_the_stop", what=what[:200])
                return
            self._stopped_by_failure = True
            self.subagent_failures.append({"whose": "machinery", "what": what})
        # The model must not be asked to carry on from a result that is not
        # its sub-agent's.
        self._cancelled.set()

    def _prepare_task(self, call_id: str, args: dict[str, Any]) -> dict[str, Any]:
        with self._delegation_lock:
            if self.subagents_admitted >= self._max_subagents:
                self.subagents_refused += 1
                return {
                    "call_id": call_id,
                    "func_name": "task_agent",
                    "header": chr(0x2717) + " task_agent: limit reached",
                    "preview": "",
                    "needs_approval": False,
                    "error": ControllerText(
                        f"Error: no sub-agent was started: this run may start "
                        f"{self._max_subagents} sub-agents and has started them all. "
                        "Do the work yourself."
                    ),
                }
            item = super()._prepare_task(call_id, args)
            if not item.get("error"):
                self.subagents_admitted += 1
            return item

    def _execute_tools(self, *args: Any, **kwargs: Any) -> Any:
        if self._before_acting is not None:
            # The model is about to act for the first time.
            first, self._before_acting = self._before_acting, None
            first()
        # The time of a batch of tool calls is the commands' time.
        batch = self._meter.tool_batch() if self._meter is not None else contextlib.nullcontext()
        with batch:
            try:
                return super()._execute_tools(*args, **kwargs)
            finally:
                # Let the environment finish stopping what the batch left behind,
                # so a sandbox lost during it is known before the next model call.
                settle = getattr(self._exec_env, "settle", None)
                if callable(settle):
                    settle()

    def _run_agent(self, *args: Any, **kwargs: Any) -> str:
        call = self._agent_call
        try:
            result = super()._run_agent(*args, **kwargs)
        except GenerationCancelled:
            call.outcome = ("cancelled", None)
            raise
        except Exception as error:
            if _sub_agents_own_doing(error):
                # The session hands the model a failed tool result for it, and
                # that is what it is: the model delegated and this came back.
                call.outcome = ("own_doing", None)
                raise
            refused = _refusal(error)
            if refused is not None:
                # The server would not take the sub-agent's conversation, which
                # begins with the prompt the model wrote for it.  The model gets
                # that as the failed result of its delegation, in the server's
                # words, and the run goes on; the record keeps the reason.
                said = _refused_text("the sub-agent's", refused)
                call.outcome = ("own_doing", None)
                with self._delegation_lock:
                    self.subagent_failures.append({"whose": "model", "what": said})
                # What the session wrapped around the refusal holds the
                # sub-agent's earlier text, which the model is still handed.
                rest = "" if refused is error else chr(10) * 2 + str(error)
                raise SubAgentRefusedError(said + rest) from error
            # A request failed for the server's own reasons, the harness raised.
            call.outcome = ("machinery", None)
            self._machinery_failed(f"{type(error).__name__}: {str(error)[:300]}")
            raise
        call.outcome = ("returned", result)
        return result

    def _exec_task(self, item: dict[str, Any]) -> tuple[str, str]:
        call = self._agent_call
        call.outcome = None
        try:
            call_id, result = super()._exec_task(item)
        except GenerationCancelled:
            raise
        except Exception as error:
            self._machinery_failed(f"{type(error).__name__}: {str(error)[:300]}")
            raise
        outcome = call.outcome
        if outcome is None:
            # No sub-agent ran, and the session answered the call itself.
            self._machinery_failed(f"no sub-agent ran: {str(result)[:300]}")
        elif outcome[0] == "returned" and result != outcome[1]:
            # The sub-agent finished; what the model gets is something else,
            # which only a failure after it explains.
            self._machinery_failed(f"after the sub-agent returned: {str(result)[:300]}")
        return call_id, result


def _whose_loss(sandbox: Sandbox, state: dict[str, Any]) -> str | None:
    """Whose doing a lost sandbox is, for this run.

    The sandbox's own verdict follows from its settings: an end that nothing
    outside explains is the model's wherever the task lets its commands end the
    main process.  One case it cannot see: a sandbox found gone when the first
    of the model's tool calls was about to run.  The model had done nothing in
    it yet, so that loss is the machinery's whatever the settings would have
    let a command do.
    """
    cause: str | None = getattr(sandbox, "lost_cause", None)
    if cause == "model" and state.get("gone_before_the_model_acted"):
        return "machinery"
    return cause


def _plain_task(task: Task) -> dict[str, Any]:
    """The task for the record; content held as bytes is named by its size."""

    def sized(value: Any) -> Any:
        if isinstance(value, bytes):
            return {"bytes": len(value)}
        if isinstance(value, dict):
            return {key: sized(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [sized(item) for item in value]
        return value

    plain: dict[str, Any] = sized(asdict(task))
    return plain


def _plain(value: Any) -> Any:
    """A JSON-safe copy of session data."""
    return json.loads(json.dumps(value, default=str))


def _record_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The conversation as the record holds it.

    The session keeps bookkeeping of its own on a turn, under keys that begin
    with an underscore: who produced it, and its reasoning in the provider's
    blocks.  They are the framework's and change with it, so the record holds
    none of them.  A turn's reasoning is given as ``reasoning``, its text, on
    every assistant turn: ``None`` where the turn had none.
    """
    kept: list[dict[str, Any]] = []
    for message in messages:
        entry = {key: value for key, value in message.items() if not key.startswith("_")}
        if message.get("role") == "assistant":
            blocks = message.get("_provider_content")
            text = "".join(
                str(block.get("text") or "")
                for block in (blocks if isinstance(blocks, list) else [])
                if isinstance(block, dict) and block.get("type") == "reasoning_text"
            )
            entry.setdefault("reasoning", text or None)
        kept.append(entry)
    return kept


def _snapshot(session: Any) -> dict[str, Any]:
    """What the record needs from a session, read while it is still there."""
    return {
        "messages": _record_messages(_plain(session._full_messages())),
        "tool_log": _plain(list(session.tool_call_log)),
        "final_content": final_assistant_text(list(session.messages)) or "",
        "usage": dict(session.total_usage),
        "prefix_events": _plain(list(session.prefix_events)),
        "subagents_admitted": session.subagents_admitted,
        "subagents_refused": session.subagents_refused,
        "subagent_failures": _plain(list(session.subagent_failures)),
        "rule_events": _plain(list(session.rule_events)),
    }


_EMPTY_SNAPSHOT: dict[str, Any] = {
    "messages": [],
    "tool_log": [],
    "final_content": "",
    "usage": {"prompt": 0, "completion": 0},
    "prefix_events": [],
    "subagents_admitted": 0,
    "subagents_refused": 0,
    "subagent_failures": [],
    "rule_events": [],
}

# Every way a rollout ends, and whose doing it is.  A state the model can bring
# about is an outcome: the record stays trainable and the task scores it.  The
# others are failures of the server, the harness or the runtime, and such a
# record is not trainable.  ``finish_state`` is always one of these.
MODEL_OUTCOMES = {
    "completed": "the model ended its turn without calling a tool",
    "max_turns": "the model used all its turns",
    "length": "a reply reached the reply limit",
    "context_full": "the conversation filled the context window",
    "timeout": "the time limit passed while the model was generating or its tools were running",
    "token_budget": "the episode's budget of generated tokens was used up",
    "command_time": "the time the model's commands may run was used up",
    "sandbox_destroyed": "the model ended its own sandbox from inside",
    "request_refused": (
        "the server refused a later request of the session for what the conversation held"
    ),
}
MACHINERY_FAILURES = {
    "error": "a request of the session failed, or the harness raised",
    "server_stalled": "the time limit passed while the model server was giving nothing",
    "backstop": "the time limit passed with the model's budgets not used up",
    "sandbox_failed": "the sandbox could not be started",
    "sidecar_failed": "the task's sidecar could not be brought up",
    "before_run_failed": "a before-run command of the task failed, so the run did not start",
    "sidecar_lost": "the task's sidecar stopped during the run",
    "sandbox_lost": "the sandbox stopped by itself during the run",
    "subagent_failed": "a sub-agent failed for a reason that is not its work",
    "no_model_call": "the run ended before any model call",
    "abnormal_reply": "the last reply ended in a way the harness does not know",
}
# The endings of a run that did not start: no model call was made, so nothing
# that happened to the sandbox meanwhile is the model's doing.
_DID_NOT_START = frozenset({"sandbox_failed", "sidecar_failed", "before_run_failed"})


def _finish_state(
    calls: list[dict[str, Any]],
    messages: list[dict[str, Any]],
    forced: str | None,
    lost: str | None,
    lost_cause: str | None,
    subagent_failed: bool,
    sidecar_lost: str | None = None,
    cut_by_budget: bool = False,
    *,
    lost_when_the_run_ended: bool = True,
) -> str:
    """How the run ended.  Where several endings apply, the order here decides.

    *lost* and *lost_cause* are the sandbox's: why it is gone, and whose doing
    that is (:func:`_whose_loss`).  *lost_when_the_run_ended* says whether it
    was already known to be gone when the session's run ended.
    """
    if forced in _DID_NOT_START:
        # Whatever became of the sandbox while it was being brought up.
        return forced
    if lost:
        # The model's doing only once the model has done something: a sandbox
        # that went away before the first model turn was not ended by it.
        if lost_cause != "model" or not calls:
            return "sandbox_lost"
        # The model ended its own sandbox.  That is how the run ended, unless
        # the machinery had failed first, while the sandbox was still there: a
        # run the machinery cut short is not the model's outcome, whatever the
        # model did to its sandbox besides.  A sidecar never stops by the
        # model's doing; a sub-agent's failure is counted only while the
        # session has not been told to stop, so one that is counted came before
        # the loss was found; and a failed request or a time limit the server
        # ran into came first where the sandbox was not known to be gone when
        # the run ended.
        if sidecar_lost:
            return "sidecar_lost"
        if subagent_failed:
            return "subagent_failed"
        if forced in MACHINERY_FAILURES and not lost_when_the_run_ended:
            return forced
        return "sandbox_destroyed"
    if sidecar_lost and forced != "sidecar_failed":
        return "sidecar_lost"
    if subagent_failed:
        return "subagent_failed"
    if forced:
        return forced
    if not calls:
        return "no_model_call"
    if messages and messages[-1].get("role") == "tool":
        # The loop ended while the model was still calling tools.
        return "max_turns"
    reason = calls[-1].get("finish_reason")
    if reason == "stop":
        return "completed"
    if reason == "length":
        # Cut at the reply limit, or at what the token budget had left for it.
        return "token_budget" if cut_by_budget else "length"
    if reason == "tool_calls" and messages and not messages[-1].get("tool_calls"):
        # The server said the reply holds tool calls and handed over none: the
        # model began a call that the server's reader could make nothing of.
        # No tool ran and the turn was the last, as after any reply without a
        # call.  What a model can write must not end a run as the machinery's.
        return "completed"
    return "abnormal_reply"


def _ending_in_words(finish: str, task: Task, config: RolloutConfig, last: Any) -> str:
    """What happened in a run that ended as *finish*, for an ending that
    brought no words of its own; *last* is the session's last model call that
    ended normally."""
    if finish == "max_turns":
        return f"the model used all {task.max_turns} of its turns"
    if finish == "length":
        return f"the last reply reached the reply limit of {config.max_tokens:,} tokens"
    if finish == "token_budget" and last is not None and last.token_grant is not None:
        return (
            f"the last reply was cut at the {last.token_grant:,} tokens the budget of "
            "generated tokens had left"
        )
    if finish == "abnormal_reply" and last is not None:
        return f"{MACHINERY_FAILURES[finish]} (finish reason {last.finish_reason!r})"
    return MODEL_OUTCOMES.get(finish) or MACHINERY_FAILURES[finish]


def configured_sampling(config: RolloutConfig) -> dict[str, Any]:
    """The sampler settings the configuration asks every request to carry."""
    settings: dict[str, Any] = {
        k: v for k, v in config.extra_body.items() if k not in _NOT_SAMPLER_KEYS
    }
    if config.temperature is not None:
        settings["temperature"] = config.temperature
    return settings


def carried_sampling(request: dict[str, Any]) -> dict[str, Any]:
    """The sampler settings one recorded request really carried."""
    settings = {
        k: v for k, v in (request.get("extra_body") or {}).items() if k not in _NOT_SAMPLER_KEYS
    }
    for name in ("temperature", "top_p"):
        if name in (request.get("sampling") or {}):
            settings[name] = request["sampling"][name]
    return settings


def _neutral(name: str, value: Any) -> bool:
    """Whether a sampler setting leaves the model's distribution as it is."""
    if name in ("temperature", "top_p"):
        return bool(value == 1)
    if name == "top_k":
        return value in (0, -1)
    return False


def sampling_problems(
    settings: dict[str, Any], required: tuple[str, ...], logprobs: dict[str, Any]
) -> list[str]:
    """Why the recorded log-probabilities cannot be trained on as the sampler's.

    *settings* are the sampler settings the requests carried; *logprobs* is a
    probe's verdict, or the one the caller gave.
    """
    problems: list[str] = []
    missing = [name for name in required if name not in settings]
    if missing:
        problems.append(
            f"sampling settings left to the server's default, which the record cannot "
            f"name: {', '.join(missing)}"
        )
    reflects: dict[str, bool | None] = logprobs.get("reflects") or all_settings(
        logprobs.get("sampler_logprobs")
    )
    everything = all(reflects.get(name) is True for name in PROBED_SETTINGS)
    before: dict[str, Any] = {}
    unknown: dict[str, Any] = {}
    for name, value in settings.items():
        if _neutral(name, value):
            continue
        # A setting the probe does not measure counts as reflected only on a
        # server that reflects every setting it does measure.
        verdict = reflects.get(name) if name in PROBED_SETTINGS else (True if everything else None)
        if verdict is False:
            before[name] = value
        elif verdict is None:
            unknown[name] = value
    if before:
        problems.append(
            "the server reports the model's own log-probabilities, not those of the "
            f"distribution the tokens were sampled from ({json.dumps(before)})"
        )
    if unknown or (missing and not everything):
        problems.append(
            "it is not known which log-probabilities the server reports "
            f"({logprobs.get('reason') or 'not probed'}"
            + (f"; {json.dumps(unknown)}" if unknown else "")
            + ")"
        )
    return problems


def _time_spent(
    recorder: RecordingClient | None, meter: Meter | None, since: float | None, now: float
) -> dict[str, Any]:
    """Where the run's time went, and how fast the server was.

    Every instant from the start of the session's run is one of three: a model
    call was under way (``model_seconds``), or else a tool call was
    (``command_seconds``), or else neither (``harness_seconds``).
    """
    calls = list(recorder.calls) if recorder is not None else []

    def spent(scope: str) -> float:
        return sum(
            (c.ended_at if c.ended_at is not None else now) - c.started_at
            for c in calls
            if c.scope == scope and c.started_at
        )

    said: dict[str, Any] = {
        # The time inside the session's own model calls, and its sub-agents'.
        "own_model_seconds": round(spent(PARENT), 2),
        "subagent_model_seconds": round(spent(SUB_AGENT), 2),
        # The longest any call waited for the server's next part.
        "longest_server_silence": round(
            max(
                [c.longest_silence for c in calls]
                # The wait a call ended on, or is still in.
                + [(c.ended_at or now) - c.last_heard_at for c in calls if c.started_at],
                default=0.0,
            ),
            2,
        ),
    }
    if meter is not None and since is not None:
        split = meter.split(since, now)
        said["model_seconds"] = round(split["model"], 2)
        said["command_seconds"] = round(split["commands"], 2)
        said["harness_seconds"] = round(split["harness"], 2)
        said.update(meter.work(now))
    return said


def _classify_timeout(
    recorder: RecordingClient | None,
    cut_at: float,
    now: float,
    stall_seconds: float,
    budgets: Budgets,
) -> tuple[str, dict[str, Any]]:
    """What a run that reached its time limit was waiting on, where the time
    limit is the model's.

    Both the model and the server can use up the time: the model by slow
    commands and long replies, the server by not answering.  The time limit is
    the model's budget like its turns, so reaching it is the model's outcome,
    unless at that moment a model request had had nothing from the server for
    *stall_seconds*: then the run ended on the server.

    A request whose prompt the server is still working on has been silent for
    a reason the model can lengthen: the prompt.  It counts as stalled only
    once the silence is longer than *stall_seconds* and the time such a prompt
    takes at the minimum prompt speed.  Otherwise a model could fill its
    context until the limit fell into that silence.

    *cut_at* is when the limit passed.  A model call that was still open then,
    or was broken off from then on, is the one the run was waiting on.
    """
    calls = list(recorder.calls) if recorder is not None else []
    under_way = [
        c
        for c in calls
        if c.started_at and (c.ended_at is None or (not c.complete and c.ended_at >= cut_at))
    ]
    if not under_way:
        return "timeout", {"waiting_on": "tools"}
    speed = budgets.prompt_speed or _DEFAULT_PROMPT_SPEED
    window = budgets.context_window

    def allowed(call: Any) -> float:
        if call.first_token_at is not None:
            return stall_seconds
        prompt = min(call.prompt_bytes, window) if window else call.prompt_bytes
        return float(stall_seconds + prompt / speed)

    waits = [(min(c.ended_at or now, now) - c.last_heard_at, allowed(c)) for c in under_way]
    silence, most = max(waits, key=lambda wait: wait[0] - wait[1])
    detail = {
        "waiting_on": "model",
        "server_silence_seconds": round(silence, 2),
        "server_silence_allowed": round(most, 2),
    }
    return ("server_stalled" if silence >= most else "timeout"), detail


def effective_budgets(task: Task, config: RolloutConfig) -> Budgets:
    """The budgets *task* runs under on the server *config* names, with the
    wall-clock limit worked out where the task leaves it open.

    Raises :class:`~turnstone.rollout.settings.SetupError` for budgets that
    cannot work.
    """
    assert config.context_window is not None
    speed, prompt_speed = config.min_tokens_per_second, config.min_prompt_tokens_per_second
    for name, value in (
        ("max_generated_tokens", task.max_generated_tokens),
        ("max_command_seconds", task.max_command_seconds),
        ("min_tokens_per_second", speed),
        ("min_prompt_tokens_per_second", prompt_speed),
        ("timeout", task.timeout),
    ):
        if value is not None and value <= 0:
            raise SetupError(f"{name} must be above zero, not {value}")
    wall = task.timeout
    if task.max_generated_tokens is None:
        if wall is None:
            raise SetupError(
                "the task has no time limit: name one (timeout), or give the model a token "
                "budget (max_generated_tokens) and the time limit is worked out"
            )
    else:
        if speed is None:
            raise SetupError(
                "with a token budget the time limit is a backstop for the machinery, and a "
                "run that reaches it is judged by the server's speed: say how many tokens "
                "a second the server must at least generate (min_tokens_per_second)"
            )
        if wall is None:
            if task.max_command_seconds is None:
                raise SetupError(
                    "the time limit can be worked out from the budgets only when the time "
                    "the model's commands may run has one too (max_command_seconds)"
                )
            wall = backstop_seconds(
                max_generated_tokens=task.max_generated_tokens,
                min_tokens_per_second=speed,
                max_command_seconds=task.max_command_seconds,
                requests=task.max_turns + config.max_subagents,
                context_window=config.context_window,
                min_prompt_tokens_per_second=prompt_speed,
            )
    return Budgets(
        wall_seconds=math.ceil(wall),
        max_generated_tokens=task.max_generated_tokens,
        max_command_seconds=task.max_command_seconds,
        min_tokens_per_second=speed,
        min_prompt_tokens_per_second=prompt_speed,
        context_window=config.context_window,
    )


def _refused_for_the_models_part(refused: APIStatusError, recorder: RecordingClient | None) -> bool:
    """Whether a refusal of the session's own request is the model's doing.

    The first request of a run holds the task and nothing of the model's: a
    server that refuses it refuses the task or the configuration, and that is
    the machinery's.  Every later request is the one before it, which the
    server took, plus what the model wrote and what its tools gave back, so a
    refusal for what such a request holds is the model's own outcome.  Were it
    the machinery's, any text a server's template will not render would be a
    way out of an episode that is going badly.

    One refusal stays the machinery's whenever it comes: a prompt that is too
    long.  A run ends by itself before a request that could not fit the
    context window (``context_full``), so a server that still finds a prompt
    too long has another window than the run was told.
    """
    if recorder is None or is_context_overflow(refused):
        return False
    return any(c.complete and not c.error and c.prompt_token_ids for c in recorder.scoped(PARENT))


class _AbandonedError(Exception):
    """A used-up budget ended the run and the session did not stop in time."""


def _supervised(
    run: Callable[[], list[dict[str, Any]]],
    meter: Meter,
    stop: Callable[[], None],
) -> list[dict[str, Any]]:
    """Run the session and watch the time its commands take.

    The session works on a thread of its own.  Once its commands have run for
    longer than they may, the run is ended as the model's outcome
    (``command_time``): the session is told to stop, and is left behind if it
    has not within the time a cancelled session is given.
    """
    box: dict[str, Any] = {}

    def work() -> None:
        try:
            box["result"] = run()
        except BaseException as error:  # handed to the caller below
            box["error"] = error

    since = time.monotonic()
    worker = threading.Thread(target=work, daemon=True, name="rollout-session")
    worker.start()
    told = False
    while worker.is_alive():
        worker.join(0.1)
        now = time.monotonic()
        if meter.ended is None and meter.over_command_time(since, now):
            meter.end("command_time")
        if meter.ended is not None and not told:
            told = True
            stop()
        if (
            meter.ended_at is not None
            and now - meter.ended_at > _CANCEL_SECONDS
            and worker.is_alive()
        ):
            raise _AbandonedError(meter.ended)
    if "error" in box:
        raise box["error"]
    result: list[dict[str, Any]] = box["result"]
    return result


def _build_record(
    task: Task,
    config: RolloutConfig,
    state: dict[str, Any],
    sandbox: Sandbox,
    *,
    forced_state: str | None,
    error: str | None,
) -> dict[str, Any]:
    now = time.monotonic()
    recorder: RecordingClient | None = state.get("recorder")
    own = recorder.scoped(PARENT) if recorder is not None else []
    side = recorder.scoped(SUB_AGENT) if recorder is not None else []
    strict = config.require_token_id_check
    # A model turn is one of the session's own calls whose reply ended normally.
    # One cut short by the time limit or a transport failure is kept aside.
    accepted = [c for c in own if c.complete and not c.error and c.prompt_token_ids]
    discarded = [c for c in own if c not in accepted]
    call_dicts = [c.to_dict(require_id_check=strict) for c in accepted]
    tokens = build_token_record(call_dicts)
    # Calls are numbered as the token record numbers them, in every line.
    problems = verify_record(tokens)
    for number, call in enumerate(accepted):
        problems += [f"call {number}: {p}" for p in call.problems(require_id_check=strict)]

    snapshot: dict[str, Any] | None = state.get("snapshot")
    session = state.get("session")
    if snapshot is None and session is not None:
        # A run that timed out or failed: the worker may still hold the session.
        try:
            snapshot = _snapshot(session)
        except Exception as read_error:
            problems.append(f"could not read the session: {read_error}")
    snapshot = snapshot or dict(_EMPTY_SNAPSHOT)
    messages = snapshot["messages"]
    assistant_turns = sum(1 for m in messages if m.get("role") == "assistant")
    if session is not None and assistant_turns != len(accepted):
        problems.append(
            f"the session holds {assistant_turns} assistant turns but {len(accepted)} model "
            "calls ended normally"
        )

    # The sampler settings are read off the requests themselves: a setting the
    # configuration names and the request did not carry was not in force.
    carried = [carried_sampling(c.request) for c in accepted]
    settings = carried[0] if carried else configured_sampling(config)
    if any(other != settings for other in carried[1:]):
        problems.append("the model calls did not all carry the same sampler settings")
    logprobs: dict[str, Any] = state.get("logprobs") or {"sampler_logprobs": None}
    problems += sampling_problems(settings, config.required_sampling, logprobs)

    # What went wrong with a sub-agent besides its work.  Only a failure of the
    # machinery ends the run; a conversation the server refused came back to
    # the model as a failed tool result.
    troubles: list[dict[str, str]] = snapshot["subagent_failures"]
    failures = [t["what"] for t in troubles if t["whose"] == "machinery"]
    lost_sidecar = getattr(sandbox, "sidecar_lost", None)
    sidecar_lost: str | None = lost_sidecar() if callable(lost_sidecar) else None
    last = accepted[-1] if accepted else None
    finish = _finish_state(
        call_dicts,
        messages,
        forced_state,
        sandbox.lost,
        _whose_loss(sandbox, state),
        bool(failures),
        sidecar_lost,
        lost_when_the_run_ended=bool(state.get("lost_when_the_run_ended", True)),
        # The last reply was cut at what the token budget had left for it.
        cut_by_budget=bool(
            last is not None
            and last.token_grant_cut
            and last.token_grant is not None
            and len(last.token_ids) >= last.token_grant
        ),
    )
    # What makes a record untrainable is never something the model chose: the
    # token data above, and the machinery failing.
    if finish == "sandbox_lost":
        # What the model saw after that point came from no environment at all.
        problems.append(f"the sandbox was lost during the run ({sandbox.lost})")
    elif finish == "subagent_failed":
        problems += [f"a sub-agent failed for a reason that is not its work: {f}" for f in failures]
    elif finish in MACHINERY_FAILURES:
        if finish == "sidecar_lost":
            error = sidecar_lost
        problems.append(
            f"{MACHINERY_FAILURES[finish]}"
            + (f": {error}" if error else "")
            + (
                f" (finish reason {call_dicts[-1].get('finish_reason')!r})"
                if finish == "abnormal_reply"
                else ""
            )
        )

    # ``error`` says what happened in every ending but the one in which the
    # model simply ended its turn.  What it was given describes the ending the
    # run was forced into; an ending that was worked out here, or that came
    # before that one, has its own words.
    if finish in ("sandbox_destroyed", "sandbox_lost"):
        error = sandbox.lost
    elif finish == "sidecar_lost":
        error = sidecar_lost
    elif finish == "subagent_failed":
        error = "; ".join(failures)
    elif finish == "completed":
        error = None
    elif error is None or finish != forced_state:
        error = _ending_in_words(finish, task, config, last)

    prefix = tokens["prefix"]
    trainable = not problems
    if not trainable:
        log.error("rollout.not_trainable", key=task.key, problems=problems)
    total = now - state["t0"]
    meter: Meter | None = state.get("meter")
    budgets: Budgets | None = state.get("budgets")
    limits: dict[str, Limit] = state.get("limits") or {}
    sampling: dict[str, Setting] = state.get("sampling") or {}
    record: dict[str, Any] = {
        "key": task.key,
        "task": _plain_task(task),
        "model": config.model,
        "endpoint": config.base_url,
        "sampling": {
            # What the requests themselves carried.
            "requested": settings,
            # What the run was set to, and where each value came from.
            "temperature": config.temperature,
            "reasoning_effort": config.reasoning_effort,
            "extra_body": config.extra_body,
            "sources": {name: setting.source for name, setting in sampling.items()},
        },
        # The limits the run was under, each with where its value came from.
        "limits": {
            name: {"value": lim.value, "source": lim.source} for name, lim in limits.items()
        },
        "logprobs": logprobs,
        "token_id_check": "required" if strict else "not required",
        "finish_state": finish,
        # Whether the ending is the model's own outcome (to be scored) or a
        # failure of the server, the harness or the runtime.
        "finish_cause": "model" if finish in MODEL_OUTCOMES else "machinery",
        "error": error,
        "timeout": state.get("timeout"),
        # The whole of it, of which bringing the sandbox up took this much.
        "seconds": round(total, 2),
        "startup_seconds": state.get("startup_seconds"),
        # The task's before-run commands, each with its result; ``None`` for a
        # task that names none.  Their time is part of the start-up.
        "before_run": state.get("before_run"),
        "time": _time_spent(recorder, meter, state.get("run_started"), now),
        # The budgets the run was under, whose the time limit was, and what
        # was spent of each.
        "budgets": None
        if budgets is None
        else {
            **budgets.describe(),
            "generated_tokens": meter.generated() if meter is not None else 0,
            "command_seconds": round(
                meter.command_seconds(state["run_started"], now)
                if meter is not None and state.get("run_started") is not None
                else 0.0,
                2,
            ),
        },
        "turns": len(accepted),
        # The length of the rollout for any length rule: what the session itself
        # generated.  Sub-agent tokens are under ``subagents`` and never added in.
        "generated_tokens": tokens["generated_tokens"],
        "tool_log": snapshot["tool_log"],
        "final_content": snapshot["final_content"],
        "messages": messages,
        "usage": snapshot["usage"],
        "tokens": tokens,
        "prefix_ok": prefix["ok"],
        "prefix_breaks": [describe_break(b) for b in prefix["breaks"]],
        "prefix_events": snapshot["prefix_events"],
        # Every model turn's tokens and log-probabilities are sound and were
        # sampled the way the record says: each call can be trained on.
        "trainable": trainable,
        # That, and every prompt extended the previous one: the rollout is one
        # token sequence.
        "one_sequence": trainable and prefix["ok"],
        "problems": problems,
        "warnings": [],
        "calls": [
            {k: v for k, v in c.items() if k not in ("prompt_token_ids", "token_ids", "logprobs")}
            for c in call_dicts
        ],
        "discarded_calls": [c.to_dict(require_id_check=strict) for c in discarded],
        "subagents": {
            "not_for_training": True,
            # Counts only: no token IDs or log-probabilities are kept for a
            # sub-agent's calls, and none of the checks behind ``trainable``
            # is made on them.
            "token_data_kept": False,
            "started": snapshot["subagents_admitted"],
            "refused_at_limit": snapshot["subagents_refused"],
            # Every delegation that came back failed for another reason than
            # the sub-agent's work, in words and in order.  ``refused_by_server``
            # counts those that were the server refusing the sub-agent's
            # conversation: the model's doing, and the run went on.  The others
            # are the machinery's and ended the run as ``subagent_failed``.
            "failures": [t["what"] for t in troubles],
            "refused_by_server": sum(1 for t in troubles if t["whose"] == "model"),
            "model_calls": len(side),
            "prompt_tokens": sum((c.usage or {}).get("prompt_tokens", 0) for c in side),
            "generated_tokens": sum(len(c.token_ids) for c in side),
            "calls": [c.summary() for c in side],
        },
        "text": None,
        "sandbox": sandbox.describe(),
        # Whether the tools acted on the machine the rollout ran on instead of
        # in a sandbox, which takes the caller's explicit say.
        "acts_on_host": bool(state.get("acts_on_host")),
        # The pattern rules in force (name and a hash of their content) and
        # every call one of them matched.
        "rules": _rules_record(state.get("rules"), snapshot, messages),
    }
    if config.keep_text:
        record["text"] = [c.raw_text for c in accepted]
        record["subagents"]["text"] = [c.raw_text for c in side]
    return record


class _BeforeRunError(Exception):
    """A before-run command of the task failed: the run does not start."""


def _before_run(task: Task, sandbox: Sandbox) -> dict[str, Any] | None:
    """Run the task's before-run commands, in order, up to the first that
    fails.  Never raises.

    ``failure`` in what comes back says why the run cannot start; it is
    ``None`` when every command ended with status 0 within its time.
    """
    if not task.before_run:
        return None
    started = time.monotonic()
    count = len(task.before_run)
    out: dict[str, Any] = {"commands": [], "failure": None}
    # Kept from outside for as long as this step may take.
    _keep(
        sandbox,
        sum(command.timeout + _BEFORE_RUN_OVERHEAD for command in task.before_run) + _KEEP_MARGIN,
    )
    run: Callable[[BeforeRunCommand], dict[str, Any]] | None = getattr(sandbox, "before_run", None)
    if run is None:
        out["failure"] = "the sandbox cannot run commands before the run"
    else:
        for number, command in enumerate(task.before_run, start=1):
            try:
                result = run(command)
            except Exception as error:
                out["failure"] = (
                    f"before-run command {number} of {count} could not be run: "
                    f"{type(error).__name__}: {error}"
                )
                break
            out["commands"].append(result)
            out["failure"] = before_run_failure(number, count, command.timeout, result)
            if out["failure"] is not None:
                break
    out["seconds"] = round(time.monotonic() - started, 2)
    return out


def _verify(
    task: Task,
    sandbox: Sandbox,
    record: dict[str, Any],
    verifier: Callable[[VerificationContext], dict[str, Any] | None] | None,
    verifier_timeout: float,
    whose_loss: Callable[[], str | None] | None = None,
) -> dict[str, Any] | None:
    """Check the rollout once the model has finished.  Never raises.

    The after-run commands run first, in the still running container; then the
    container is stopped and the verifier is called.  ``failure`` in what comes
    back names a failure of this machinery: no verdict exists then, and the
    caller makes the record not trainable.  A check that ran and failed is in
    ``commands`` or ``result``, and is the model's outcome.
    """
    if not task.after_run and verifier is None:
        return None
    started = time.monotonic()
    # The containers are kept from outside for the step under way, not for a
    # time worked out when the run began.
    _keep(
        sandbox,
        sum(command.timeout + _AFTER_RUN_OVERHEAD for command in task.after_run) + _KEEP_MARGIN,
    )
    out: dict[str, Any] = {
        "commands": [],
        "result": None,
        "failure": None,
        "container_stopped": None,
        "helpers_removed": None,
    }
    usable = sandbox.lost is None and callable(getattr(sandbox, "after_run", None))
    if task.after_run and not usable:
        # Nothing to run them in.  Whose doing that is, the finish state says.
        out["commands_not_run"] = sandbox.lost or "the sandbox cannot run commands after the run"
    elif task.after_run:
        try:
            for command in task.after_run:
                out["commands"].append(sandbox.after_run(command))  # type: ignore[attr-defined]
        except Exception as error:
            cause = whose_loss() if whose_loss else getattr(sandbox, "lost_cause", None)
            lost_by_model = isinstance(error, ContainerLostError) and cause == "model"
            if lost_by_model:
                # Something the model left behind ended the container before
                # the check could run in it: its outcome, like ending it
                # during the run.
                out["commands_not_run"] = sandbox.lost
            else:
                out["failure"] = (
                    f"an after-run command could not be run: {type(error).__name__}: {error}"
                )
    _keep(sandbox, 120 + verifier_timeout + _KEEP_MARGIN if verifier is not None else 300)
    stop = getattr(sandbox, "stop", None)
    if callable(stop):
        out["container_stopped"] = bool(stop())
    if verifier is not None and out["failure"] is None:
        state = getattr(sandbox, "state", None)
        context = VerificationContext(
            task=task,
            container_name=getattr(sandbox, "name", None),
            sidecar_name=getattr(getattr(sandbox, "sidecar", None), "name", None),
            container_stopped=bool(out["container_stopped"]),
            container_state=state() if callable(state) else {},
            after_run=list(out["commands"]),
            record=record,
            lifetime=int(verifier_timeout) + _KEEP_MARGIN,
        )
        if out["container_stopped"] is False:
            out["failure"] = "the episode container could not be stopped before the check"
        else:
            out["result"], out["failure"] = run_verifier(verifier, context, verifier_timeout)
        out["helpers_removed"] = context.remove_started()
    out["seconds"] = round(time.monotonic() - started, 2)
    return out


def _confirmed_running(sandbox: Sandbox) -> bool:
    """Whether *sandbox* is running now, asked of the sandbox where it can be
    asked.  Never raises: one that cannot say is taken at what it knows."""
    confirm = getattr(sandbox, "confirm_running", None)
    if callable(confirm):
        try:
            return bool(confirm())
        except Exception as error:
            log.warning("rollout.confirm_running_failed", error=str(error)[:200])
    return sandbox.lost is None


def _keep(sandbox: Sandbox, seconds: float) -> None:
    """Tell the sandbox how long the step now starting may take, if it keeps
    time at all.  Never raises."""
    keep = getattr(sandbox, "keep_for", None)
    if not callable(keep):
        return
    try:
        keep(seconds)
    except Exception as error:
        log.warning("rollout.keep_for_failed", error=str(error)[:200])


def _rules_record(
    rules: RuleSet | None, snapshot: dict[str, Any], messages: list[dict[str, Any]]
) -> dict[str, Any] | None:
    if rules is None:
        return None
    events: list[dict[str, Any]] = snapshot["rule_events"]
    # The tool log holds the session's own calls in the order the model made
    # them; name the rule on the entry of each call one matched.
    call_ids = [
        call.get("id")
        for message in messages
        if message.get("role") == "assistant"
        for call in message.get("tool_calls") or []
    ]
    log_entries: list[dict[str, Any]] = snapshot["tool_log"]
    if len(call_ids) == len(log_entries):
        by_id = {e["call_id"]: e for e in events if e["scope"] == PARENT}
        for call_id, entry in zip(call_ids, log_entries, strict=True):
            if call_id in by_id:
                entry["rule"] = by_id[call_id]["rule"]
                entry["rule_says"] = by_id[call_id]["recommendation"]
    return {**rules.identity(), "events": events}


def _resolve_rules(config: RolloutConfig) -> RuleSet | None:
    if config.rules is None or isinstance(config.rules, RuleSet):
        return config.rules
    if config.rules == "shipped":
        return RuleSet.shipped()
    return RuleSet.from_file(config.rules)


def _detect_context_window(config: RolloutConfig) -> int | None:
    """The context window the model's endpoint reports for the rollout's model."""
    from turnstone.core.model_registry import detect_model

    with OpenAI(base_url=config.base_url, api_key=config.api_key, timeout=10.0) as client:
        model, window = detect_model(client, log_fn=lambda *_a, **_k: None, fatal=False)
    return window if model == config.model else None


# The numbers that are the rollout's own, with the least each can be and why
# nothing lies below it, for the refusal.  The limits the framework has a
# setting for, and the temperature, have theirs where they are resolved
# (:mod:`turnstone.rollout.settings`).
_OWN_LEAST: tuple[tuple[str, int, str], ...] = (
    ("max_subagents", 0, "0 lets a run start none, and nothing lies below it"),
    (
        "max_read_bytes",
        1,
        "a file tool that may read no byte fails every read, which would be scored as the model's",
    ),
)


def effective_config(
    config: RolloutConfig,
) -> tuple[RolloutConfig, dict[str, Limit], dict[str, Setting]]:
    """*config* with everything it leaves to the framework filled in, and where
    each limit and each sampling setting came from.

    Raises :class:`~turnstone.rollout.settings.SetupError` for a configuration
    that cannot work, a number below the least it can be among it: a run
    started on one would end in a way that says something about the number,
    and be scored as the model's.
    """
    for name, least, why in _OWN_LEAST:
        given = getattr(config, name)
        if given < least:
            raise SetupError(f"{name} must be at least {least}, not {given}: {why}")
    if not config.server_stall_seconds > 0:
        raise SetupError(
            f"server_stall_seconds must be above zero, not {config.server_stall_seconds}: at "
            "zero every time limit that passes during a model call would be the server's "
            "doing, and a model could leave a run that way"
        )
    sampling = resolve_sampling(
        model=config.model,
        base_url=config.base_url,
        temperature=config.temperature,
        reasoning_effort=config.reasoning_effort,
        extra_body=config.extra_body,
    )
    config = replace(
        config,
        temperature=sampling["temperature"].value,
        reasoning_effort=sampling["reasoning_effort"].value,
        extra_body=sampling["extra_body"].value,
    )
    limits = resolve_limits(
        model=config.model,
        base_url=config.base_url,
        max_tokens=config.max_tokens,
        context_window=config.context_window,
        tool_timeout=config.tool_timeout,
        subagent_max_turns=config.subagent_max_turns,
        max_output_chars=config.max_output_chars,
        detect_context_window=lambda: _detect_context_window(config),
    )
    effective = replace(
        config,
        max_tokens=limits["max_tokens"].value,
        context_window=limits["context_window"].value,
        tool_timeout=limits["tool_timeout"].value,
        subagent_max_turns=limits["subagent_max_turns"].value,
        max_output_chars=limits["max_output_chars"].value,
    )
    return effective, limits, sampling


def _logprob_verdict(config: RolloutConfig) -> dict[str, Any]:
    given = config.sampler_logprobs
    if isinstance(given, dict):
        values = {given.get(name) for name in PROBED_SETTINGS}
        verdict = True if values == {True} else False if values == {False} else None
        return {"sampler_logprobs": verdict, "reflects": dict(given), "reason": "given"}
    if given is not None:
        return {"sampler_logprobs": given, "reflects": all_settings(given), "reason": "given"}
    try:
        with OpenAI(base_url=config.base_url, api_key=config.api_key, timeout=60.0) as probe:
            return sampler_logprobs_reported(probe, config.model, extra_body=config.extra_body)
    except Exception as probe_error:
        return {
            "sampler_logprobs": None,
            "reflects": all_settings(None),
            "reason": f"the probe failed: {type(probe_error).__name__}",
        }


def run_episode(
    task: Task,
    config: RolloutConfig,
    *,
    sandbox_factory: Callable[[Task, RolloutConfig], Sandbox] | None = None,
    verifier: Callable[[VerificationContext], dict[str, Any] | None] | None = None,
    verifier_timeout: float = 3600.0,
    allow_host_execution: bool = False,
) -> dict[str, Any]:
    """Run *task* once and return its episode record.

    The model's tools act in the sandbox and nowhere else.  A sandbox whose
    execution environment acts on the machine the rollout runs on is refused
    with :class:`HostExecutionError` before anything runs, unless
    *allow_host_execution* is true.  That is for a caller who scripts every
    command itself, such as a test: the pattern rules a rollout enforces are
    not isolation and do not make a model's commands safe to run here.

    *verifier* is called once the model has finished, with the episode's
    container stopped (:mod:`turnstone.rollout.verification`).  What it returns
    is stored under ``verification``; it must hold a ``verdict``.  A verifier
    that raises, runs past *verifier_timeout* or returns no verdict leaves the
    rollout without a reward, and the record is then not trainable.  Whatever
    it does, the sandbox is removed afterwards.

    The sandbox is removed on every path, and a failed removal is reported in
    the record (``sandbox.removed`` false, a line in ``warnings``) instead of
    raised, so a finished rollout is never lost to its teardown.

    ``trainable`` is true when the token data is sound and its provenance is
    known: every model turn was recorded complete, with one log-probability per
    generated token, from a server whose log-probabilities are the sampling
    distribution's, under sampler settings the record names, and nothing but
    the model decided how the run went.  A run the server, the harness or the
    runtime ended (``finish_cause`` ``machinery``) is not trainable; one the
    model ended, however badly, is, and is for the task to score.
    ``one_sequence`` adds that every prompt extended the previous one.
    ``problems`` lists why ``trainable`` is false.
    """
    if not verifier_timeout > 0:
        raise SetupError(
            f"verifier_timeout must be above zero, not {verifier_timeout}: a check that is "
            "given no time gives no verdict, and the whole run would be lost to that"
        )
    # Before the clock starts: what the run's limits are, and which
    # log-probabilities the server reports.  Neither is the task's time.
    config, limits, sampling = effective_config(config)
    assert config.max_tokens is not None and config.tool_timeout is not None
    assert config.context_window is not None and config.subagent_max_turns is not None
    assert config.max_output_chars is not None
    budgets = effective_budgets(task, config)
    state: dict[str, Any] = {"limits": limits, "sampling": sampling, "budgets": budgets}
    # Counts the run's tokens and time; the recording client asks it before
    # every request.
    meter = Meter(budgets, lambda: list(state["recorder"].calls) if "recorder" in state else [])
    state["meter"] = meter
    state["logprobs"] = _logprob_verdict(config)
    state["rules"] = _resolve_rules(config)
    state["t0"] = time.monotonic()
    sandbox: Sandbox = (sandbox_factory or ContainerSandbox)(task, config)
    forced: str | None = None
    error: str | None = None

    worker_done = threading.Event()

    def setup(workdir: str) -> None:
        reset_storage()
        init_storage("sqlite", path=os.path.join(workdir, ".rollout.db"), run_migrations=False)

    def build_client() -> Any:
        inner = OpenAI(
            base_url=config.base_url,
            api_key=config.api_key,
            timeout=float(budgets.wall_seconds),
            http_client=shutdown_on_close(DefaultHttpxClient()),
        )
        state["recorder"] = RecordingClient(inner, extra_body=config.extra_body, gate=meter)
        return state["recorder"]

    try:
        try:
            environment = sandbox.start()
            state["acts_on_host"] = _acts_on_host(environment)
            if state["acts_on_host"] and not allow_host_execution:
                raise HostExecutionError(_HOST_REFUSAL)
            sandbox.put_files({**task.setup_files, **task.files})
            # The task's own commands, before anything is asked of the model.
            state["before_run"] = _before_run(task, sandbox)
            if (state["before_run"] or {}).get("failure"):
                raise _BeforeRunError(state["before_run"]["failure"])
        except HostExecutionError:
            raise
        except _BeforeRunError as not_prepared:
            forced, error = "before_run_failed", str(not_prepared)
        except SidecarError as sidecar_error:
            forced, error = "sidecar_failed", str(sidecar_error)
        except Exception as start_error:
            forced = "sandbox_failed"
            error = f"{type(start_error).__name__}: {start_error}"
        else:
            state["startup_seconds"] = round(time.monotonic() - state["t0"], 2)
            # This rollout's own MCP servers, if its task brings any.
            mcp_client = getattr(sandbox, "mcp_client", None)
            offered = config.tool_names if config.tool_names is not None else ROLLOUT_TOOL_NAMES
            if mcp_client is not None:
                offered = frozenset(offered) | {
                    t["function"]["name"] for t in mcp_client.get_tools()
                }

            def build_session(run_client: Any) -> tuple[Any, Callable[[], list[dict[str, Any]]]]:
                recorder: RecordingClient = run_client

                def before_acting() -> None:
                    # The first of the model's tool calls is about to run.  A
                    # sandbox found gone now went away without the model.
                    state["gone_before_the_model_acted"] = not _confirmed_running(sandbox)

                def last_call_tokens() -> int | None:
                    call = recorder.last_parent_call()
                    usage = call.usage if call is not None and call.complete else None
                    if not usage:
                        return None
                    return usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0)

                session = RolloutSession(
                    client=run_client,
                    model=config.model,
                    system_prompt_override=task.system_prompt,
                    instructions=None,
                    temperature=config.temperature,
                    max_tokens=config.max_tokens,
                    tool_timeout=config.tool_timeout,
                    reasoning_effort=config.reasoning_effort,
                    context_window=config.context_window,
                    tool_truncation=config.max_output_chars,
                    agent_max_turns=config.subagent_max_turns,
                    stable_prefix=True,
                    raw_arguments=RecordedArguments(recorder)
                    if config.argument_text == "parameter_blocks"
                    else None,
                    tool_names=offered,
                    mcp_client=mcp_client,
                    mcp_tools=mcp_client is not None,
                    exec_env=environment,
                    max_subagents=config.max_subagents,
                    last_call_tokens=last_call_tokens,
                    rules=state["rules"],
                    allow_host_execution=allow_host_execution,
                    meter=meter,
                    before_acting=before_acting,
                )
                state["session"] = session
                # A sandbox that is gone ends the run at the next turn: nothing
                # the model asks for could be done.
                sandbox.notify_lost(session._cancelled.set)

                def run() -> list[dict[str, Any]]:
                    try:
                        return session.send_headless(task.prompt, max_turns=task.max_turns)
                    finally:
                        worker_done.set()

                def stop() -> None:
                    # A budget of the model's is used up: nothing more is asked
                    # of it, and what it has running is ended.
                    session._cancelled.set()
                    with contextlib.suppress(Exception):
                        session.cancel()

                def drive() -> list[dict[str, Any]]:
                    if budgets.max_command_seconds is None:
                        return run()
                    return _supervised(run, meter, stop)

                return session, drive

            def finish(session: Any, _tool_log: list[dict[str, Any]]) -> dict[str, Any]:
                state["snapshot"] = _snapshot(session)
                return {}

            # The time limit starts now, with the sandbox up: the model's time,
            # or with a token budget the machinery's backstop.
            remaining = budgets.wall_seconds
            _keep(sandbox, remaining + _KEEP_MARGIN)
            state["run_started"] = time.monotonic()
            # The earliest moment the time limit can end the run.
            cut_at = state["run_started"] + remaining - 0.25

            def leave_behind() -> None:
                # The session is left to itself.  Stop what it is doing, so
                # that nothing of the model's is still acting while the record
                # is read.
                session = state.get("session")
                if session is not None:
                    with contextlib.suppress(Exception):
                        session.cancel()
                    state["worker_stopped"] = worker_done.wait(_CANCEL_SECONDS)

            def run_the_session() -> None:
                try:
                    run_with_lifecycle(
                        workdir_prefix="turnstone_rollout_",
                        setup=setup,
                        build_client=build_client,
                        build_session=build_session,
                        finish=finish,
                        test_timeout=remaining,
                        teardown_reset=reset_storage,
                        on_cwd_restore_failed=lambda _cwd: None,
                        attempts=1,
                    )
                finally:
                    # Whether the sandbox was already known to be gone when the
                    # run ended: what failed while it was still there came first.
                    state["lost_when_the_run_ended"] = sandbox.lost is not None

            try:
                run_the_session()
            except TimeoutError:
                now = time.monotonic()
                if meter.ended is not None:
                    # A budget of the model's had run out; the session had not
                    # stopped yet.
                    forced = meter.ended
                elif budgets.backstop:
                    forced, state["timeout"] = meter.at_the_backstop(state["run_started"], now)
                else:
                    forced, state["timeout"] = _classify_timeout(
                        state.get("recorder"), cut_at, now, config.server_stall_seconds, budgets
                    )
                error = f"the rollout did not finish within {remaining} s"
                leave_behind()
            except ContextFullError as full:
                forced, error = "context_full", str(full)
            except TokenBudgetError as spent:
                forced, error = "token_budget", str(spent)
            except _AbandonedError:
                forced = meter.ended
                leave_behind()
            except Exception as run_error:
                refused = _refusal(run_error)
                if meter.ended is not None:
                    # What the session raised when it was stopped.
                    forced = meter.ended
                elif refused is not None and _refused_for_the_models_part(
                    refused, state.get("recorder")
                ):
                    forced = "request_refused"
                    error = _refused_text("the session's", refused)
                else:
                    forced = "error"
                    error = f"{type(run_error).__name__}: {str(run_error)[:500]}"
            else:
                # Stopped by a used-up budget, the session ended in an orderly way.
                forced = meter.ended
            if forced in ("token_budget", "command_time") and error is None:
                error = MODEL_OUTCOMES[forced]
        record = _build_record(task, config, state, sandbox, forced_state=forced, error=error)
        # A run that did not start left nothing to check.
        record["verification"] = (
            None
            if record["finish_state"] in _DID_NOT_START
            else _verify(task, sandbox, record, verifier, verifier_timeout)
        )
        if (
            sandbox.lost
            and _whose_loss(sandbox, state) == "model"
            and record["finish_state"] != "sandbox_destroyed"
            and record["finish_cause"] == "model"
        ):
            # The model's doing ended the sandbox after its last turn, while
            # the check was under way.  The same outcome as during the run.
            record["finish_state"] = "sandbox_destroyed"
            record["error"] = sandbox.lost
        failure = (record["verification"] or {}).get("failure")
        if failure:
            # No reward exists for this rollout.
            record["problems"].append(f"the check after the run failed: {failure}")
            record["trainable"] = record["one_sequence"] = False
            log.error("rollout.verification_failed", key=task.key, failure=failure)
        if (record["verification"] or {}).get("helpers_removed") is False:
            record["warnings"].append("a verification container could not be confirmed removed")
    finally:
        teardown_started = time.monotonic()
        removed = sandbox.close()
    record["sandbox"] = sandbox.describe()
    # ``seconds`` is the run; removing the sandbox comes on top and is bounded
    # by the sandbox, not by the task's limit.
    record["teardown_seconds"] = round(time.monotonic() - teardown_started, 2)
    if state.get("worker_stopped") is False:
        record["warnings"].append(
            "the session was still working 10 s after it was told to stop; removing the "
            "sandbox ends it"
        )
    if not removed:
        record["warnings"].append("the sandbox could not be confirmed removed")
        log.error("rollout.sandbox_not_removed", key=task.key, sandbox=record["sandbox"])
    return record
