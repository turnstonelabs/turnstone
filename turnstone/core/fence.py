"""Nonce-delimited fences for trust boundaries at the LLM wire.

A *fence* wraps a span of content in ``<{tag}_{nonce}>...</{tag}_{nonce}>``
markers whose nonce an adversary cannot reproduce, and neutralises any literal
marker in adjacent untrusted text so a leaked or guessed nonce alone cannot
forge or break the boundary.  One mechanism, two trust polarities:

* **Output-guard judge** (:mod:`turnstone.core.output_guard_judge`) wraps
  UNTRUSTED tool output before handing it to the judge LLM.  The nonce stops
  that content from breaking *out* of the fence; the judge's system prompt
  declares the fence *form* (``<tool_output_NONCE>``) as untrusted data, so a
  fresh per-call nonce is enough.

* **Operator fold** (``ChatSession._fold_system_turns``) wraps TRUSTED operator
  instructions folded into a neighbouring turn for models without native
  mid-conversation system messages.  The nonce stops untrusted host text from
  forging a *fake* trusted block; the system prompt
  (:func:`turnstone.prompts.build_operator_instruction_declaration`) declares
  the fence *exact value* as the sole trusted marker, so the nonce must live in
  the (cached) system prefix — minted once per session, not per fold.

The lifecycle difference (per-call form vs. per-session value) belongs to the
callers; the mint / neutralise / wrap mechanism is shared here so the two
boundaries cannot drift in nonce width or escaping.  They did drift once — the
operator path had regressed to a 32-bit, no-escape nonce while the judge used a
64-bit per-call nonce with closing-tag escaping — which is the divergence this
module exists to prevent.
"""

from __future__ import annotations

import re
import secrets
from typing import Final

# Tag bases for the two fence kinds.  Kept distinct so the two trust
# declarations never cross-contaminate: ``tool_output`` content is declared
# UNTRUSTED (to the judge), ``system-reminder`` content is declared TRUSTED (to
# the assistant).  A shared tag would let one declaration's semantics bleed onto
# the other's markers.
TOOL_OUTPUT_TAG: Final = "tool_output"
SYSTEM_REMINDER_TAG: Final = "system-reminder"

# 8 bytes → 16 hex chars → 64 bits.  An adversary whose payload is fixed before
# the nonce is minted cannot guess it; and because the fold path also
# neutralises markers in the untrusted host text (see :func:`neutralize`), even
# a mid-session leak of the reused per-session operator nonce cannot be turned
# into a forged block.  Matching the judge's width here is the point: one
# constant, no per-caller drift.
_NONCE_BYTES: Final = 8


def mint_nonce() -> str:
    """Mint a 64-bit unguessable hex nonce for a fence tag."""
    return secrets.token_hex(_NONCE_BYTES)


def _marker_pattern(tag: str, *, opening: bool) -> re.Pattern[str]:
    """Compile the marker pattern for *tag*.

    ``</tag`` (closing) is always matched — that is how content breaks *out* of
    a fence wrapping it.  ``<tag`` (opening) is matched too when *opening* is
    set — that is how surrounding text forges a fake fence to break *in*.
    ``\\s*`` tolerates ``</ tag`` whitespace tricks.  Only the tag *prefix* is
    anchored, so a nonce suffix (``<system-reminder_abcd>``) is matched and
    defanged regardless of whether the hex matches the real nonce.
    """
    slash = "/?" if opening else "/"
    return re.compile(rf"<({slash})(\s*){re.escape(tag)}", re.IGNORECASE)


def neutralize(text: str, tag: str, *, opening: bool = False) -> str:
    """Defang literal fence markers for *tag* in untrusted *text*.

    Inserts a backslash after ``<`` (``<\\/tag`` / ``<\\tag``) so the sequence
    stays human-readable in logs but no longer matches the fence's open/close
    marker — even if the adversary has learned the nonce.  Idempotent: an
    already-defanged ``<\\tag`` is not re-matched.

    By default only the *closing* marker is neutralised (break-out defence, for
    a fence wrapping untrusted content).  Pass ``opening=True`` to also
    neutralise the *opening* marker (forge-in defence, for untrusted text that
    surrounds a trusted fence).
    """
    if "<" not in text:
        return text
    pattern = _marker_pattern(tag, opening=opening)
    return pattern.sub(lambda m: f"<\\{m.group(1)}{m.group(2)}{tag}", text)


def wrap(content: str, nonce: str, tag: str) -> str:
    """Wrap *content* in a ``<{tag}_{nonce}>...</{tag}_{nonce}>`` fence.

    The body's *closing* marker is neutralised first so content cannot break
    out of the fence even if it knows the nonce.  Forge-in defence
    (neutralising the *opening* marker in the untrusted text that *surrounds*
    the fence) is the caller's job via :func:`neutralize` with ``opening=True``
    — only the operator fold has an untrusted host to defend; the judge fence
    wraps a standalone message.
    """
    body = neutralize(content, tag)
    return f"<{tag}_{nonce}>\n{body}\n</{tag}_{nonce}>"
