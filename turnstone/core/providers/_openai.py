"""Re-export shim for backwards compatibility.

The OpenAI provider family is split into:
- ``_openai_chat.py``      — Chat Completions API (local model servers)
- ``_openai_responses.py`` — Responses API (commercial OpenAI)
- ``_openai_common.py``    — shared capability table, helpers

``OpenAIProvider`` is preserved as an alias for ``OpenAIChatCompletionsProvider``
so existing code that imports it directly continues to work.
"""

from turnstone.core.providers._openai_chat import (
    OpenAIChatCompletionsProvider,
)
from turnstone.core.providers._openai_chat import (
    OpenAIChatCompletionsProvider as OpenAIProvider,
)

# Backwards-compatible aliases for the capability tables
from turnstone.core.providers._openai_common import (
    OPENAI_CAPABILITIES as _OPENAI_CAPABILITIES,  # noqa: F401
)
from turnstone.core.providers._openai_common import OPENAI_DEFAULT as _OPENAI_DEFAULT  # noqa: F401
from turnstone.core.providers._openai_responses import OpenAIResponsesProvider

__all__ = [
    "OpenAIChatCompletionsProvider",
    "OpenAIProvider",
    "OpenAIResponsesProvider",
]
