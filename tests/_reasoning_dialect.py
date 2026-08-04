"""Inline-reasoning dialect conformance catalog.

Passthrough servers (parserless vLLM/llama.cpp, LM Studio, bare gateways)
emit model reasoning inline as ``<think>``/``<reasoning>`` blocks inside the
content stream — a *dialect* of model output.  This module is that dialect's
executable specification for ``split_inline_reasoning``: each case maps an
utterance to the exact ``(content, reasoning)`` lanes the one-shot must
produce.

Consumers: the one-shot conformance and one-shot≡streaming property suites
in tests/test_think_tag_split.py.  Lane suites (session, judge,
output-guard, optimizer, drain-stream) pin their lanes with suite-local
utterances through their own fakes — adding a case HERE extends the
semantics spec, not automatically any lane suite.

The split is RAW (residue whitespace stays; ``drain_stream`` owns the one
trim over its joined runs).  ``passthrough`` marks cases the split must
return BYTE-IDENTICAL: tag-free text, and text whose only tags are orphan
CLOSE tags.  The latter is a
review ruling, not an accident: a close tag whose open never arrived is
indistinguishable from prose QUOTING the tag, and drained lanes routinely
quote third-party text (web-fetch answers citing pages about reasoning
models, guard verdicts echoing judged content) — any reclassification
would let quoted text destroy real results.  Display lanes wanting
stricter cosmetic peeling (the title) own that locally as formatting.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class DialectCase:
    id: str
    utterance: str
    content: str
    reasoning: str
    # The split returns the utterance byte-identical (no tag consumed):
    # tag-free text, or orphan-close-only text (quoted-tag safety).
    passthrough: bool = False


CASES: tuple[DialectCase, ...] = (
    DialectCase(
        id="no_tag_byte_identity",
        utterance="Just an answer.",
        content="Just an answer.",
        reasoning="",
        passthrough=True,
    ),
    DialectCase(
        # The fast path must not strip: unconsumed content is
        # byte-identical, whitespace included.
        id="no_tag_preserves_whitespace",
        utterance="  spaced  \n",
        content="  spaced  \n",
        reasoning="",
        passthrough=True,
    ),
    DialectCase(
        id="leading_block",
        utterance="<think>plan</think>Answer",
        content="Answer",
        reasoning="plan",
    ),
    DialectCase(
        # The split is RAW — tag residue stays; drain_stream owns the ONE
        # blank-edge-line trim over its joined runs (pinned there), which
        # preserves the code block's first-line indentation.
        id="indented_code_block_raw_residue",
        utterance="<think>plan</think>\n\n    print(1)\n    more()",
        content="\n\n    print(1)\n    more()",
        reasoning="plan",
    ),
    DialectCase(
        id="leading_block_raw_residue",
        utterance="<think>plan</think>\n\nAnswer\n",
        content="\n\nAnswer\n",
        reasoning="plan",
    ),
    DialectCase(
        id="interleaved_blocks_both_vocabularies",
        utterance="Intro <think>a</think>mid <reasoning>b</reasoning>end",
        content="Intro mid end",
        reasoning="ab",
    ),
    DialectCase(
        id="unterminated_open_tail_is_reasoning",
        utterance="Answer part<think>never closed",
        content="Answer part",
        reasoning="never closed",
    ),
    DialectCase(
        # QUOTED-CLOSE SAFETY (review ruling): an orphan close is
        # indistinguishable from a quoted tag — everything passes through.
        # A malicious page embedding the literal string must not be able
        # to wipe the extraction that quotes it.
        id="orphan_close_passes_through",
        utterance="The page says templates emit </think> after the preamble. Answer: 42.",
        content="The page says templates emit </think> after the preamble. Answer: 42.",
        reasoning="",
        passthrough=True,
    ),
    DialectCase(
        # Template-pre-injected shape ("reasoning</think>answer"): the seam
        # deliberately passes it through — segregating it would require
        # treating every quoted close as a boundary.  Post-#831 every lane
        # streams, and known streaming surfaces strip the orphan close
        # server-side; display lanes peel cosmetically on their own.
        id="preinject_shape_passes_through",
        utterance="plan text</think>\n\nAnswer",
        content="plan text</think>\n\nAnswer",
        reasoning="",
        passthrough=True,
    ),
    DialectCase(
        id="immediate_close_passes_through",
        utterance="</think>Answer",
        content="</think>Answer",
        reasoning="",
        passthrough=True,
    ),
    DialectCase(
        # Any close tag closes any open block (splitter semantics; the old
        # pairwise per-caller strip treated this as unterminated).
        id="cross_vocabulary_close",
        utterance="<think>x</reasoning>Answer",
        content="Answer",
        reasoning="x",
    ),
    DialectCase(
        id="think_only",
        utterance="<think>all reasoning</think>",
        content="",
        reasoning="all reasoning",
    ),
    DialectCase(
        id="think_only_unterminated",
        utterance="<think>everything",
        content="",
        reasoning="everything",
    ),
    DialectCase(
        # A balanced block followed by a stray close: the block is
        # consumed, the stray close stays in content (quoted-tag safety),
        # and the consumed-tag strip applies.
        id="balanced_block_then_stray_close",
        utterance="<think>a</think>b</think>c",
        content="b</think>c",
        reasoning="a",
    ),
    DialectCase(
        id="multiple_blocks_accumulate",
        utterance="<think>one</think>mid<think>two</think>tail",
        content="midtail",
        reasoning="onetwo",
    ),
    DialectCase(
        # ACCEPTED RESIDUAL (R2): the split is content-blind, so a literal
        # OPEN tag in legitimate prose misroutes the remainder — the same
        # false positive the interactive splitter has carried in the
        # field.  This pin makes any future fix a conscious change.
        id="literal_open_tag_false_positive_r2",
        utterance="The `<think>` tag opens a block.",
        content="The `",
        reasoning="` tag opens a block.",
    ),
)
