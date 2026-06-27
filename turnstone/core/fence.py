"""Nonce-delimited fences for trust boundaries at the LLM wire.

A *fence* wraps a span of content in ``[start {tag}_{nonce}] ... [end
{tag}_{nonce}]`` markers whose nonce an adversary cannot reproduce, and
neutralises any literal marker in adjacent untrusted text so a leaked or guessed
nonce alone cannot forge or break the boundary.  One mechanism, two trust
polarities:

* **Output-guard judge** (:mod:`turnstone.core.output_guard_judge`) wraps
  UNTRUSTED tool output before handing it to the judge LLM.  The nonce stops
  that content from breaking *out* of the fence; the judge's system prompt
  declares the fence *form* (``[start tool_output_NONCE]``) as untrusted data,
  so a fresh per-call nonce is enough.

* **Operator fold** (``lowering.fold_system_turns``) wraps TRUSTED operator
  instructions folded into a neighbouring turn for models without native
  mid-conversation system messages.  The nonce stops untrusted host text from
  forging a *fake* trusted block; the system prompt
  (:func:`turnstone.prompts.build_operator_instruction_declaration`) declares
  the fence *exact value* as the sole trusted marker, so the nonce must live in
  the (cached) system prefix — minted once per session, not per fold.

The marker shape is bracketed ``start``/``end`` keywords rather than the prior
``<{tag}_{nonce}>`` XML form: angle-bracket markup pushed some local models out
of distribution and toward emitting their own turn-structure tokens.  The chat
templates most at risk are the ones built around rigid ``<...>``-style
structural tokens, so a fold marker that resembles them derails the template
once a few accumulate.  ``start``/``end`` carry no slash — no ``</`` or ``[/``
closing-tag shape — and read as ordinary text the model has seen everywhere.

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
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Iterable

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

# Open / close keywords for the bracketed marker — ``[start {tag}_{nonce}]`` /
# ``[end {tag}_{nonce}]``.  Slash-free by design (see the module docstring): the
# open/close discriminator is the keyword, not a ``/`` that would re-create the
# closing-tag shape that derails those models' chat templates.  Both fence
# kinds share these, so the detection regexes here and in
# ``output_guard._RE_FENCE_MARKER`` (built via :func:`detection_pattern`) cannot
# drift from what :func:`wrap` emits.
_OPEN_KW: Final = "start"
_CLOSE_KW: Final = "end"


def mint_nonce() -> str:
    """Mint a 64-bit unguessable hex nonce for a fence tag."""
    return secrets.token_hex(_NONCE_BYTES)


def _marker_pattern(tag: str, *, opening: bool) -> re.Pattern[str]:
    """Compile the marker pattern for *tag*.

    ``[end tag`` (closing) is always matched — that is how content breaks *out*
    of a fence wrapping it.  ``[start tag`` (opening) is matched too when
    *opening* is set — that is how surrounding text forges a fake fence to break
    *in*.

    The single capture spans the run between ``[`` and the tag — optional
    whitespace, the ``start``/``end`` keyword, the separating whitespace — so the
    defang (a backslash right after ``[``) lands in front of the keyword and the
    marker no longer matches.  Built from the same ``start``/``end`` keywords as
    :func:`wrap` and :func:`detection_pattern`, so a marker can never be
    emitted-but-not-detected.  Only the tag *prefix* is anchored, so a nonce
    suffix (``[start system-reminder_abcd]``) is matched and defanged regardless
    of whether the hex matches the real nonce.
    """
    kw = rf"(?:{_OPEN_KW}|{_CLOSE_KW})" if opening else _CLOSE_KW
    return re.compile(rf"\[(\s*{kw}\s+){re.escape(tag)}", re.IGNORECASE)


def neutralize(text: str, tag: str, *, opening: bool = False) -> str:
    """Defang literal fence markers for *tag* in untrusted *text*.

    Inserts a backslash after ``[`` (``[\\end tag`` / ``[\\start tag``) so the
    sequence stays human-readable in logs but no longer matches the fence's
    open/close marker — even if the adversary has learned the nonce.
    Idempotent: an already-defanged ``[\\end tag`` is not re-matched.

    By default only the *closing* marker is neutralised (break-out defence, for
    a fence wrapping untrusted content).  Pass ``opening=True`` to also
    neutralise the *opening* marker (forge-in defence, for untrusted text that
    surrounds a trusted fence).
    """
    if "[" not in text:
        return text
    pattern = _marker_pattern(tag, opening=opening)
    return pattern.sub(lambda m: f"[\\{m.group(1)}{tag}", text)


def wrap(content: str, nonce: str, tag: str) -> str:
    """Wrap *content* in a ``[start {tag}_{nonce}] ... [end {tag}_{nonce}]`` fence.

    The body's *closing* marker is neutralised first so content cannot break
    out of the fence even if it knows the nonce.  Forge-in defence
    (neutralising the *opening* marker in the untrusted text that *surrounds*
    the fence) is the caller's job via :func:`neutralize` with ``opening=True``
    — only the operator fold has an untrusted host to defend; the judge fence
    wraps a standalone message.
    """
    body = neutralize(content, tag)
    return f"[{_OPEN_KW} {tag}_{nonce}]\n{body}\n[{_CLOSE_KW} {tag}_{nonce}]"


def detection_pattern(tags: Iterable[str]) -> re.Pattern[str]:
    """Compile an open-or-close marker detector for any of *tags*.

    Single source for the marker *shape* used by forgery / leak scanning
    (``output_guard._RE_FENCE_MARKER``), so a detector cannot drift from what
    :func:`wrap` emits.  Matches either the ``start`` or the ``end`` keyword form
    and captures the ``_<hex>`` nonce suffix (or nothing) as group 1, so a caller
    can tell a leaked exact-nonce marker from a bare or wrong-nonce forgery.
    Only the tag prefix is anchored, so a marker is caught whether or not its hex
    matches a real nonce.
    """
    alt = "|".join(re.escape(t) for t in tags)
    return re.compile(
        rf"\[\s*(?:{_OPEN_KW}|{_CLOSE_KW})\s+(?:{alt})(_[0-9a-f]+)?",
        re.IGNORECASE,
    )
