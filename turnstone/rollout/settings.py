"""Where a rollout's limits and sampling settings come from.

A rollout does not carry settings of its own where the framework already has
one.  Each of these is taken, in order, from: the rollout's own explicit
override; the model's definition in ``config.toml`` (``[models.NAME]``); the
configured setting (``[model]`` / ``[tools]`` in ``config.toml``); and the
setting's registered default.  That is the order an interactive session
resolves them in, read from the configuration file: a rollout runs in its own
process with throwaway storage and does not read a server's database.

The limits (:func:`resolve_limits`): the reply limit, the context window, the
time one tool call may take, the turns of a sub-agent, the output truncation.
The context window has one more source, as it has for the terminal client:
what the model's endpoint reports.  It has no default.  A window nobody names
would be a guess, and a rollout that ends because a guessed window is full
would be scored as the model's outcome for a property of the configuration,
so such a setup is refused (:class:`SetupError`).  For the same reason the
reply limit is at most a quarter of the window, which is the most the
framework itself reserves for a reply: with a reply limit near the window's
size the first long tool result would end every rollout.  A run that sends its
prompts as token IDs knows each prompt's length before it goes out and keeps
the window exactly, so there nothing is set aside (``reserve_reply=False``).

The sampling settings (:func:`resolve_sampling`): the temperature, the
reasoning effort and the model definition's extra request parameters.  A
temperature nothing names is 1, the model's own distribution: left to the
server it would be a value the record cannot name.

Every value comes back with where it came from, and the episode record keeps
that.

The configuration file is read by the framework's own loader, and a file that
loader refuses is refused here (:func:`read_configuration`): one that
``--config`` or ``TURNSTONE_CONFIG`` names and that is not there, and any
configuration file that cannot be read or is no TOML.  Taken as an empty
configuration, such a file would silently cost the run its model definition:
the reasoning effort, the reply limit, the request parameters.
"""

from __future__ import annotations

import copy
import json
import math
from dataclasses import dataclass
from typing import Any

from turnstone.core.config import ConfigError, load_config
from turnstone.core.log import get_logger
from turnstone.core.settings_registry import SETTINGS

log = get_logger(__name__)

OVERRIDE = "rollout config"


class SetupError(ValueError):
    """The configuration a rollout was given cannot work.  Nothing was run."""


def read_configuration(section: str | None = None) -> dict[str, Any]:
    """The configuration file, or one section of it, as the framework reads it.

    Raises :class:`SetupError` where the framework's loader refuses the file:
    a file named by ``--config`` or ``TURNSTONE_CONFIG`` that is not there,
    and any configuration file that cannot be read or parsed.  Where neither
    names one, the default file is read if it exists, and a default file that
    is absent is an empty configuration.
    """
    try:
        return load_config(section)
    except ConfigError as error:
        raise SetupError(
            f"the configuration file cannot be used ({error}). A run without it would lose "
            "what it says of the model: the reasoning effort, the reply limit, the request "
            "parameters"
        ) from None


@dataclass(frozen=True)
class Limit:
    value: int
    source: str


@dataclass(frozen=True)
class Setting:
    """A sampling setting and where its value came from."""

    value: Any
    source: str


def _no_constant(name: str) -> Any:
    raise ValueError(f"{name} is no number in JSON")


def _finite(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ValueError(f"{text} is too large to be a number")
    return value


def strict_json(text: str | bytes, **hooks: Any) -> Any:
    """*text* read as JSON and as nothing more.

    The standard reader also takes ``NaN``, ``Infinity`` and ``-Infinity``,
    and reads a number too large for a float as an infinity.  None of them is
    JSON, none is a number a rollout can work with, and one that got into a
    record would make it a file a strict reader refuses.  Here each is a
    ``ValueError``, like any other text that is no JSON.
    """
    return json.loads(text, parse_constant=_no_constant, parse_float=_finite, **hooks)


def not_finite(value: Any, where: str = "") -> str | None:
    """Where *value* holds a number that is not finite (``NaN``, an infinity),
    ``None`` when it holds none."""
    if isinstance(value, float) and not math.isfinite(value):
        return where or "the value itself"
    if isinstance(value, dict):
        for key, item in value.items():
            found = not_finite(item, f"{where}.{key}" if where else str(key))
            if found is not None:
                return found
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            found = not_finite(item, f"{where}[{index}]")
            if found is not None:
                return found
    return None


def _positive(value: Any, *, least: int = 1) -> int | None:
    """*value* as a usable setting: an integer at or above *least*, else ``None``."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = int(value)
    except (ValueError, OverflowError):
        # A text that is no number; a NaN or an infinity, which TOML can write.
        return None
    return number if number >= least else None


def model_definition(model: str, base_url: str) -> tuple[str, dict[str, Any]] | None:
    """The ``[models.*]`` entry of ``config.toml`` for *model*: the one of that
    name, else one that serves that model (at *base_url* when several do).

    Raises :class:`SetupError` for a configuration file that cannot be used."""
    models = read_configuration("models")
    entries = {k: v for k, v in models.items() if isinstance(v, dict)}
    if model in entries:
        return model, entries[model]
    serving = [(alias, entry) for alias, entry in entries.items() if entry.get("model") == model]
    here = [
        (alias, entry)
        for alias, entry in serving
        if str(entry.get("base_url", "")).rstrip("/") == base_url.rstrip("/")
    ]
    chosen = here or serving
    return chosen[0] if len(chosen) == 1 else None


# Why a limit has no value below its least one, for the refusal of an
# override that names one.  A configured value below it is passed over, as the
# framework passes it over; an override is the caller's own word and is refused.
_NOTHING_BELOW = {
    "max_tokens": "a reply holds at least one token",
    "context_window": (
        "a window that holds no token leaves a reply no room, and a run it cut short would "
        "be scored as the model's outcome"
    ),
    "tool_timeout": "a tool call that may take no time is stopped as it starts",
    "subagent_max_turns": "-1 is no limit of its own, and nothing lies below it",
    "max_output_chars": "0 is the framework's automatic share, and nothing lies below it",
}


def _override(name: str, value: int, least: int) -> Limit:
    """The rollout's explicit override of the limit *name*, refused where it
    is below the *least* such a limit can be."""
    if value < least:
        raise SetupError(f"{name} must be at least {least}, not {value}: {_NOTHING_BELOW[name]}")
    return Limit(value, OVERRIDE)


def _from_settings(
    name: str,
    override: int | None,
    section: str,
    key: str,
    setting: str,
    *,
    least: int = 1,
    definition: tuple[str, dict[str, Any]] | None = None,
) -> Limit:
    if override is not None:
        return _override(name, override, least)
    if definition is not None:
        alias, entry = definition
        value = _positive(entry.get(key), least=least)
        if value is not None:
            return Limit(value, f"config.toml [models.{alias}] {key}")
    value = _positive(read_configuration(section).get(key), least=least)
    if value is not None:
        return Limit(value, f"config.toml [{section}] {key}")
    return Limit(int(SETTINGS[setting].default), f"the framework's default for {setting}")


def resolve_limits(
    *,
    model: str,
    base_url: str,
    max_tokens: int | None,
    context_window: int | None,
    tool_timeout: int | None,
    subagent_max_turns: int | None,
    max_output_chars: int | None,
    detect_context_window: Any = None,
    reserve_reply: bool = True,
) -> dict[str, Limit]:
    """The limits one rollout runs under, each with its source.

    An argument that is not ``None`` is the rollout's explicit override.  One
    below the least its limit can be is refused: a reply limit, a context
    window or a tool time limit below 1, a sub-agent turn limit below -1 (no
    limit of its own), an output truncation below 0 (the automatic share).
    *detect_context_window* is called, with no arguments, when neither an
    override nor the configuration names a context window; it returns what the
    model's endpoint reports, or ``None``.

    *reserve_reply* caps the reply limit at a quarter of the window.  A run
    that knows each prompt's length before it is sent keeps the window exactly
    and passes ``False``.

    Raises :class:`SetupError` when nothing says what the context window is,
    and for an override below its limit's least value.
    """
    definition = model_definition(model, base_url)
    limits = {
        "max_tokens": _from_settings(
            "max_tokens",
            max_tokens,
            "model",
            "max_tokens",
            "model.max_tokens",
            definition=definition,
        ),
        "tool_timeout": _from_settings(
            "tool_timeout", tool_timeout, "tools", "timeout", "tools.timeout"
        ),
        # -1 means no limit of its own, as in the framework.
        "subagent_max_turns": _from_settings(
            "subagent_max_turns",
            subagent_max_turns,
            "tools",
            "agent_max_turns",
            "tools.agent_max_turns",
            least=-1,
        ),
        # 0 means the framework's automatic share of the remaining context.
        "max_output_chars": _from_settings(
            "max_output_chars", max_output_chars, "tools", "truncation", "tools.truncation", least=0
        ),
    }
    if context_window is not None:
        limits["context_window"] = _override("context_window", context_window, 1)
    else:
        window: Limit | None = None
        if definition is not None:
            value = _positive(definition[1].get("context_window"))
            if value is not None:
                window = Limit(value, f"config.toml [models.{definition[0]}] context_window")
        if window is None:
            value = _positive(read_configuration("model").get("context_window"))
            if value is not None:
                window = Limit(value, "config.toml [model] context_window")
        if window is None and detect_context_window is not None:
            try:
                value = _positive(detect_context_window())
            except Exception as error:
                log.warning("rollout.context_window_not_detected", error=str(error)[:200])
                value = None
            if value is not None:
                window = Limit(value, "reported by the model's endpoint")
        if window is None:
            raise SetupError(
                f"the context window of {model} is not known: the rollout names none "
                "(context_window), config.toml names none ([models.NAME] or [model] "
                "context_window) and the model's endpoint reports none. A rollout does not "
                "guess it: a run that ends because a guessed window is full would be scored "
                "as the model's outcome"
            )
        limits["context_window"] = window
    # The most the framework reserves of a window for one reply.
    reserve = max(limits["context_window"].value // 4, 1)
    reply = limits["max_tokens"]
    if reserve_reply and reply.value > reserve:
        limits["max_tokens"] = Limit(
            reserve,
            f"a quarter of the context window, the most the framework reserves for a reply "
            f"({reply.source} says {reply.value})",
        )
    return limits


def merge_into(target: dict[str, Any], source: dict[str, Any]) -> None:
    """Merge *source* into *target*; nested dicts merge key by key, *source* wins."""
    for key, value in source.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            merge_into(target[key], value)
        else:
            target[key] = copy.deepcopy(value)


def _temperature(value: Any) -> float | None:
    """*value* as a temperature the framework accepts, else ``None``."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return number if 0.0 <= number <= 2.0 else None


def resolve_sampling(
    *,
    model: str,
    base_url: str,
    temperature: float | None,
    reasoning_effort: str | None,
    extra_body: dict[str, Any],
) -> dict[str, Setting]:
    """The sampling settings one rollout runs under, each with its source.

    *temperature* and *reasoning_effort* that are not ``None`` are the
    rollout's explicit overrides; a temperature that is no finite number or
    lies below zero is refused.  *extra_body* is the rollout's own; the
    model definition's extra request parameters
    (``capabilities.server_compat.extra_body``) go under it, the rollout's
    winning key by key.
    """
    definition = model_definition(model, base_url)
    alias, entry = definition if definition is not None else ("", {})
    global_section = read_configuration("model")

    chosen: Setting | None = None
    if temperature is not None:
        if not_finite(float(temperature)) is not None:
            raise SetupError(f"the temperature must be a finite number, not {temperature}")
        if temperature < 0:
            raise SetupError(
                f"the temperature must be zero or above, not {temperature}: no sampler has one "
                "below zero"
            )
        chosen = Setting(temperature, OVERRIDE)
    if chosen is None and _temperature(entry.get("temperature")) is not None:
        chosen = Setting(
            _temperature(entry.get("temperature")), f"config.toml [models.{alias}] temperature"
        )
    if chosen is None and _temperature(global_section.get("temperature")) is not None:
        chosen = Setting(
            _temperature(global_section.get("temperature")), "config.toml [model] temperature"
        )
    if chosen is None:
        chosen = Setting(1.0, "the rollout's own: nothing in the configuration names one")
    settings = {"temperature": chosen}

    effort: Setting | None = None
    if reasoning_effort is not None:
        effort = Setting(reasoning_effort, OVERRIDE)
    if effort is None and str(entry.get("reasoning_effort") or ""):
        effort = Setting(
            str(entry["reasoning_effort"]), f"config.toml [models.{alias}] reasoning_effort"
        )
    if effort is None and str(global_section.get("reasoning_effort") or ""):
        effort = Setting(
            str(global_section["reasoning_effort"]), "config.toml [model] reasoning_effort"
        )
    settings["reasoning_effort"] = effort or Setting(None, "not set: the model's own default")

    capabilities = entry.get("capabilities")
    compat = capabilities.get("server_compat") if isinstance(capabilities, dict) else None
    defined = compat.get("extra_body") if isinstance(compat, dict) else None
    if isinstance(defined, dict) and defined:
        merged = copy.deepcopy(defined)
        merge_into(merged, extra_body)
        where = f"config.toml [models.{alias}] capabilities.server_compat.extra_body"
        settings["extra_body"] = Setting(
            merged, f"{where}, with the rollout's own on top" if extra_body else where
        )
    else:
        settings["extra_body"] = Setting(
            copy.deepcopy(extra_body), OVERRIDE if extra_body else "nothing sets any"
        )
    # A request parameter that is no finite number (a configuration file can
    # write one) could be sent to no server and written into no record.
    found = not_finite(settings["extra_body"].value)
    if found is not None:
        raise SetupError(
            f"the request parameters hold a number that is not finite ({found}; "
            f"{settings['extra_body'].source})"
        )
    return settings
