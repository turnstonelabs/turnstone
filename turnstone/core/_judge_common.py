"""Shared helpers for the intent + output-guard LLM judges.

Both :class:`turnstone.core.judge.IntentJudge` and
:class:`turnstone.core.output_guard_judge.OutputGuardJudge` need the
same alias-resolution, client-config extraction, and JSON-verdict
parsing primitives.  Keeping them here means a future change to
``ModelRegistry`` resolution semantics, ``httpx`` client construction,
or the JSON-extraction strategy set lands in ONE place — the two
judges can't drift.

This module deliberately depends only on the public ``ModelRegistry``
+ providers surface; it does not import from either judge module, so
it stays import-cycle-free.

Currently consumed by ``OutputGuardJudge``.  ``IntentJudge`` keeps its
own copies for now (separate migration) but should converge here when
that PR lands.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

from turnstone.core.log import get_logger

if TYPE_CHECKING:
    from turnstone.core.providers._protocol import LLMProvider

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Alias resolution
# ---------------------------------------------------------------------------


def resolve_judge_model(
    config_field_value: str,
    config_field_name: str,
    *,
    session_provider: LLMProvider,
    session_client: Any,
    session_model: str,
    model_registry: Any | None,
) -> tuple[LLMProvider, dict[str, str], str, str]:
    """Resolve a judge model alias to ``(provider, client_factory_args, model, alias)``.

    ``config_field_value`` is the alias string the operator set (e.g.
    ``judge.output_guard_model = "gpt-5-mini"``).  ``config_field_name``
    is the dotted setting name used purely in the warning log when the
    alias is unknown.

    Behaviour mirrors :class:`IntentJudge.__init__` at ``judge.py:917-960``:
    an empty / unset alias falls through to the session model silently;
    a set-but-unknown alias logs a warning and also falls through.
    """
    if config_field_value and model_registry is not None:
        try:
            if model_registry.has_alias(config_field_value):
                client, model_name, _ = model_registry.resolve(config_field_value)
                provider = model_registry.get_provider(config_field_value)
                client_factory_args = extract_client_config(client, provider.provider_name)
                return provider, client_factory_args, model_name, config_field_value
        except Exception:
            log.debug(
                "judge_common.alias_resolution_failed",
                setting=config_field_name,
                alias=config_field_value,
            )

    if config_field_value:
        log.warning(
            "judge_common.alias_unresolved",
            setting=config_field_name,
            alias=config_field_value,
            fallback=session_model,
        )

    client_factory_args = extract_client_config(session_client, session_provider.provider_name)
    return session_provider, client_factory_args, session_model, ""


def extract_client_config(client: Any, provider_name: str) -> dict[str, str]:
    """Extract connection config from an existing SDK client for re-creation.

    Mirrors the helper IntentJudge ships at ``judge.py:965-969``.  Reads
    ``base_url`` and ``api_key`` from the client (with two attribute-name
    fallbacks for SDK variations) and returns the dict
    ``turnstone.core.providers.create_client`` accepts.
    """
    base_url = str(getattr(client, "base_url", getattr(client, "_base_url", "")))
    api_key = getattr(client, "api_key", "") or ""
    return {"provider_name": provider_name, "base_url": base_url, "api_key": api_key}


# ---------------------------------------------------------------------------
# JSON verdict parsing
# ---------------------------------------------------------------------------


def extract_json(
    text: str,
    *,
    fallback_keys: tuple[str, ...] = (),
) -> dict[str, Any] | None:
    """Extract a JSON object from text using up to four fallback strategies.

    Strategy 1: direct parse.
    Strategy 2: markdown code block (``` or ```json fenced).
    Strategy 3: balanced brace-pair from the first ``{``.
    Strategy 4 (only if ``fallback_keys`` is non-empty): regex
    field-by-field extraction.  ``fallback_keys`` is the tuple of
    string-valued keys the caller cares about; ``"confidence"`` is
    always also matched as a numeric field if present.

    Returns ``None`` when no strategy yields a dict.  This mirrors the
    behaviour of :func:`IntentJudge._extract_json` at
    ``judge.py:1604-1659`` — IntentJudge passes the intent-verdict keys
    explicitly, OutputGuardJudge passes the output-verdict keys, and
    callers that don't want strategy 4 (e.g. test code) leave the
    parameter at its empty default.
    """
    # Strategy 1: direct parse
    try:
        data = json.loads(text.strip())
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, ValueError):
        pass

    # Strategy 2: markdown code block
    md_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if md_match:
        try:
            data = json.loads(md_match.group(1))
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, ValueError):
            pass

    # Strategy 3: find first { and matching }
    start = text.find("{")
    if start >= 0:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        data = json.loads(text[start : i + 1])
                        if isinstance(data, dict):
                            return data
                    except (json.JSONDecodeError, ValueError):
                        pass
                    break

    # Strategy 4: regex field extraction — only when the caller asked for it
    if fallback_keys:
        fields: dict[str, Any] = {}
        for key in fallback_keys:
            m = re.search(rf'"{key}"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
            if m:
                fields[key] = m.group(1)
        conf_m = re.search(r'"confidence"\s*:\s*([\d.]+)', text)
        if conf_m:
            fields["confidence"] = float(conf_m.group(1))
        if fields:
            return fields

    return None
