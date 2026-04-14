"""Server compatibility profiles for OpenAI-compatible backends.

Different local model servers (vLLM, llama.cpp, SGLang) need different
request shaping.  This module separates two concerns:

1. **Model capabilities** — ``thinking_mode`` and ``thinking_param`` are
   properties of the *model* (Gemma thinks, Llama doesn't).  These go
   into the ``capabilities`` dict and flow through ``ModelCapabilities``
   so the provider can act on them (just like Anthropic's thinking mode).

2. **Server workarounds** — ``extra_body`` overrides like
   ``skip_special_tokens=false`` are properties of the *server* (vLLM
   bug workaround).  These stay in ``server_compat`` and get merged
   into the request's ``extra_body`` at call time.

Profiles are *suggestions* only.  The admin UI auto-fills them on
Detect; the operator has final say, and the stored DB config is what
actually gets used at request time.
"""

from __future__ import annotations

import copy
from typing import Any

# ---------------------------------------------------------------------------
# Profile suggestions
# ---------------------------------------------------------------------------

# Each profile has two optional parts:
#   "capabilities" — merged into the model's capabilities dict (thinking_mode etc.)
#   "server_compat" — stored as server_compat (extra_body workarounds)

_PROFILES: dict[str, dict[str, Any]] = {
    "vllm-gemma-thinking": {
        "capabilities": {
            "thinking_mode": "manual",
            "thinking_param": "enable_thinking",
        },
        "server_compat": {
            "server_type": "vllm",
            # Workaround: vLLM strips special tokens before the Gemma4
            # reasoning parser sees them.  skip_special_tokens=false
            # preserves <|channel> / <channel|> markers so reasoning
            # content is extracted correctly.
            "extra_body": {"skip_special_tokens": False},
        },
    },
    "vllm-qwen-thinking": {
        "capabilities": {
            "thinking_mode": "manual",
            "thinking_param": "enable_thinking",
        },
        "server_compat": {
            "server_type": "vllm",
        },
    },
    "vllm-granite-thinking": {
        "capabilities": {
            "thinking_mode": "manual",
            "thinking_param": "thinking",
        },
        "server_compat": {
            "server_type": "vllm",
        },
    },
    "vllm-deepseek-thinking": {
        "capabilities": {
            "thinking_mode": "manual",
            "thinking_param": "thinking",
        },
        "server_compat": {
            "server_type": "vllm",
        },
    },
    "vllm-holo-thinking": {
        "capabilities": {
            "thinking_mode": "manual",
            "thinking_param": "enable_thinking",
        },
        "server_compat": {
            "server_type": "vllm",
        },
    },
    "vllm": {
        "server_compat": {
            "server_type": "vllm",
        },
    },
    "llama.cpp": {
        "server_compat": {
            "server_type": "llama.cpp",
        },
    },
    "llama.cpp-thinking": {
        "capabilities": {
            "thinking_mode": "manual",
            "thinking_param": "enable_thinking",
        },
        "server_compat": {
            "server_type": "llama.cpp",
            # llama.cpp uses reasoning_format (top-level request param) to
            # extract thinking into the reasoning_content response field.
            # "auto" lets the server decide based on the model's template;
            # "deepseek" forces extraction for all thinking models.
            "extra_body": {"reasoning_format": "auto"},
        },
    },
    "sglang": {
        "server_compat": {
            "server_type": "sglang",
        },
    },
}

# Model-family → profile key mapping.  Checked in order; first match wins.
_VLLM_MODEL_PROFILES: list[tuple[str, str]] = [
    ("gemma-4", "vllm-gemma-thinking"),
    ("gemma-3", "vllm-gemma-thinking"),
    ("gemma4", "vllm-gemma-thinking"),
    ("gemma3", "vllm-gemma-thinking"),
    ("qwen3", "vllm-qwen-thinking"),
    ("qwq", "vllm-qwen-thinking"),
    ("granite-3", "vllm-granite-thinking"),
    ("granite3", "vllm-granite-thinking"),
    ("deepseek-r1", "vllm-deepseek-thinking"),
    ("holo2", "vllm-holo-thinking"),
]

# llama.cpp model-family → profile key mapping.
_LLAMA_CPP_MODEL_PROFILES: list[tuple[str, str]] = [
    ("gemma-4", "llama.cpp-thinking"),
    ("gemma-3", "llama.cpp-thinking"),
    ("gemma4", "llama.cpp-thinking"),
    ("gemma3", "llama.cpp-thinking"),
    ("qwen3", "llama.cpp-thinking"),
    ("qwq", "llama.cpp-thinking"),
    ("deepseek-r1", "llama.cpp-thinking"),
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def suggest_profile(server_type: str, model_id: str) -> dict[str, Any]:
    """Suggest capabilities and server compat based on server type and model.

    Returns a dict with optional ``"capabilities"`` and ``"server_compat"``
    keys.  Empty dict when no special settings are needed.
    """
    profile_key: str | None = None
    model_lower = (model_id or "").lower()
    if server_type == "vllm":
        for substring, key in _VLLM_MODEL_PROFILES:
            if substring in model_lower:
                profile_key = key
                break
        if profile_key is None:
            profile_key = "vllm"
    elif server_type == "llama.cpp":
        for substring, key in _LLAMA_CPP_MODEL_PROFILES:
            if substring in model_lower:
                profile_key = key
                break
        if profile_key is None:
            profile_key = "llama.cpp"
    elif server_type in _PROFILES:
        profile_key = server_type

    if profile_key is None:
        return {}
    return copy.deepcopy(_PROFILES[profile_key])


def merge_server_compat(
    base_chat_template_kwargs: dict[str, Any],
    server_compat: dict[str, Any],
) -> dict[str, Any]:
    """Build the ``extra_body`` dict by merging server compat into base kwargs.

    *base_chat_template_kwargs* always contains at least ``reasoning_effort``.
    *server_compat* comes from ``ModelConfig.server_compat``.

    Note: thinking-mode params (``enable_thinking``, ``thinking``) are **not**
    merged here — the provider handles those via ``ModelCapabilities``.
    This function only merges server workarounds from ``extra_body``.

    Returns the complete dict to pass as ``extra_body`` to the OpenAI client.
    """
    extra: dict[str, Any] = {"chat_template_kwargs": dict(base_chat_template_kwargs)}

    # Merge top-level extra_body overrides (skip_special_tokens, etc.)
    compat_eb = server_compat.get("extra_body")
    if isinstance(compat_eb, dict):
        for key, value in compat_eb.items():
            if key == "chat_template_kwargs":
                # Prevent accidental override of the assembled ctk —
                # operator should use capabilities for thinking params.
                continue
            extra[key] = value

    return extra
