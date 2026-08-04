"""#965 per-lane pins for the optimizer: content arrives IR-clean from the
drain seam, and the emptiness fallbacks are load-bearing.

The optimizer's five private regex strips are gone; these rows pin (a) that
tagged model output still yields clean prompts/systems (the seam does the
work now), and (b) the two ``or``-fallback flips: an all-reasoning pass
keeps the CURRENT observer system / prompt verbatim instead of wiping the
observer system or evaluating an empty-prompt evolution node.
"""

from __future__ import annotations

from tests._session_helpers import seam_provider
from turnstone.optimizer import _observe_and_update_optimizer, _propose_prompt_modification

_OBSERVER_SYSTEM = "You observe optimization runs and refine the optimizer system."


def test_observer_tagged_output_yields_clean_system() -> None:
    out = _observe_and_update_optimizer(
        client=object(),
        model="m",
        optimizer_system=_OBSERVER_SYSTEM,
        iterations=[],
        provider=seam_provider("<think>weighing the history</think>Refined observer text."),
    )
    assert out == "Refined observer text."


def test_observer_think_only_keeps_current_system_verbatim() -> None:
    # Before the seam, the private regex emptied the text AFTER the
    # ``or``-fallback check and the observer system was WIPED.
    out = _observe_and_update_optimizer(
        client=object(),
        model="m",
        optimizer_system=_OBSERVER_SYSTEM,
        iterations=[],
        provider=seam_provider("<think>no conclusion reached</think>"),
    )
    assert out == _OBSERVER_SYSTEM


def test_proposal_tagged_output_yields_clean_prompt() -> None:
    out = _propose_prompt_modification(
        client=object(),
        model="m",
        current_prompt="Old prompt.",
        test_cases=[],
        iteration_result={"cases": {}},
        history=[],
        provider=seam_provider("<think>rework section two</think>New prompt text."),
    )
    assert out == "New prompt text."


def test_proposal_think_only_keeps_current_prompt() -> None:
    # An all-reasoning pass reads as "no changes" downstream — never an
    # empty-prompt evolution-tree node.
    out = _propose_prompt_modification(
        client=object(),
        model="m",
        current_prompt="Old prompt.",
        test_cases=[],
        iteration_result={"cases": {}},
        history=[],
        provider=seam_provider("<think>hmm</think>"),
    )
    assert out == "Old prompt."


def test_observer_whitespace_only_content_keeps_current_system() -> None:
    # Falsiness is checked AFTER normalization: whitespace-only content
    # (tag-free, so seam-byte-identical and truthy) must not strip to ""
    # past the fallback and wipe the observer system.
    out = _observe_and_update_optimizer(
        client=object(),
        model="m",
        optimizer_system=_OBSERVER_SYSTEM,
        iterations=[],
        provider=seam_provider("\n\n  \n"),
    )
    assert out == _OBSERVER_SYSTEM


def test_proposal_whitespace_only_content_keeps_current_prompt() -> None:
    out = _propose_prompt_modification(
        client=object(),
        model="m",
        current_prompt="Old prompt.",
        test_cases=[],
        iteration_result={"cases": {}},
        history=[],
        provider=seam_provider("\n\n"),
    )
    assert out == "Old prompt."


def test_proposal_fallback_prompt_is_never_fence_stripped() -> None:
    # The fence-strip normalizes MODEL output only; a no-answer pass keeps
    # a fence-bearing current prompt VERBATIM, never reduced to its fence
    # innards.
    fenced_prompt = "Do the task.\n```python\nexample()\n```\nBe precise."
    out = _propose_prompt_modification(
        client=object(),
        model="m",
        current_prompt=fenced_prompt,
        test_cases=[],
        iteration_result={"cases": {}},
        history=[],
        provider=seam_provider("<think>no conclusion</think>"),
    )
    assert out == fenced_prompt


def test_proposal_model_output_fence_is_unwrapped() -> None:
    # The fence rule applies to MODEL output (only): a fenced proposal
    # yields its innards, and prose outside the fences is discarded.
    out = _propose_prompt_modification(
        client=object(),
        model="m",
        current_prompt="Old prompt.",
        test_cases=[],
        iteration_result={"cases": {}},
        history=[],
        provider=seam_provider("Here you go:\n```\nNew prompt text.\n```\nHope that helps!"),
    )
    assert out == "New prompt text."
