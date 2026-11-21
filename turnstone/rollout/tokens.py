"""Token record of a rollout, and the prefix check.

A rollout is a list of model calls.  Call ``t`` has a prompt (the token IDs the
server rendered from the conversation so far) and generated tokens with their
log-probabilities.  When the conversation only grows and earlier text is never
rewritten, the prompt of call ``t + 1`` starts with the prompt of call ``t``
followed by the tokens call ``t`` generated.  The whole rollout is then one
token sequence (the last prompt plus the last generated tokens) and every
generated token has one position in it.

The session's ``stable_prefix`` option makes that true at the message level.
:func:`check_prefix` verifies it independently on the token IDs the server
returned, and reports exactly where it breaks.  :func:`build_token_record`
stores the rollout without repeating shared tokens and without assuming the
property: every call's prompt and generated tokens can be rebuilt whether or
not the check passed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence


def common_prefix_len(a: Sequence[int], b: Sequence[int]) -> int:
    limit = min(len(a), len(b))
    i = 0
    while i < limit and a[i] == b[i]:
        i += 1
    return i


def check_prefix(calls: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Check that each call's prompt extends the previous prompt plus generated tokens.

    *calls* are dicts with ``prompt_token_ids`` and ``token_ids``.  Returns
    ``{"ok": bool, "calls": n, "breaks": [...]}``.  A break names the later call
    (``call``), the first differing position (``position``), whether it lies in
    the previous call's prompt or in its generated tokens (``region``, with the
    ``offset`` inside it), the token expected and the token found, and the kind:
    ``mismatch`` (a token differs) or ``shorter`` (the later prompt ends before
    the earlier sequence does).
    """
    breaks: list[dict[str, Any]] = []
    for t in range(1, len(calls)):
        prev_prompt = calls[t - 1]["prompt_token_ids"]
        prev_generated = calls[t - 1]["token_ids"]
        prompt = calls[t]["prompt_token_ids"]
        expected = list(prev_prompt) + list(prev_generated)
        k = common_prefix_len(expected, prompt)
        if k == len(expected):
            continue
        in_prompt = k < len(prev_prompt)
        breaks.append(
            {
                "call": t,
                "kind": "mismatch" if k < len(prompt) else "shorter",
                "position": k,
                "region": "previous_prompt" if in_prompt else "previous_generated",
                "offset": k if in_prompt else k - len(prev_prompt),
                "expected_token_id": expected[k],
                "found_token_id": prompt[k] if k < len(prompt) else None,
                "previous_prompt_len": len(prev_prompt),
                "previous_generated_len": len(prev_generated),
                "prompt_len": len(prompt),
                "expected_window": expected[max(0, k - 5) : k + 5],
                "found_window": list(prompt[max(0, k - 5) : k + 5]),
            }
        )
    return {"ok": not breaks, "calls": len(calls), "breaks": breaks}


def describe_break(item: dict[str, Any]) -> str:
    """One readable line for one break reported by :func:`check_prefix`."""
    where = "prompt" if item["region"] == "previous_prompt" else "generated tokens"
    if item["kind"] == "shorter":
        found = f"the prompt of call {item['call']} ends there (length {item['prompt_len']})"
    else:
        found = f"call {item['call']} has token {item['found_token_id']}"
    line = (
        f"call {item['call']} does not extend call {item['call'] - 1}: position "
        f"{item['position']} (offset {item['offset']} in the {where} of call "
        f"{item['call'] - 1}) expected token {item['expected_token_id']}, {found}"
    )
    if item["region"] == "previous_generated" and (
        item["offset"] == item["previous_generated_len"] - 1
    ):
        # The turn ended on a token the template does not write there, such as
        # an end-of-text token where it writes its end-of-turn token.
        line += "; this is the turn's last token, which the server replaced"
    return line


def build_token_record(calls: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Build the token record from the model calls of one rollout.

    Per call the record keeps ``shared`` (how many leading tokens of its prompt
    equal the previous call's prompt plus generated tokens), ``prompt_tail``
    (the rest of its prompt), ``token_ids`` and ``logprobs``.  The first call
    keeps its whole prompt as the tail, so no token is stored twice.

    A call whose ``logprobs`` is ``None``, or not one per token, has no usable
    log-probabilities: it is stored with ``logprobs: None`` and the record's
    ``trainable`` is ``False``.

    What a trainer reads:

    - per call ``start`` and ``end``: where its generated tokens sit in the
      final sequence (:func:`rebuild_final`), and ``in_final``: whether they
      really are there (true for every call when the prefix check passed, false
      for calls before a break);
    - ``prefix``: the result of :func:`check_prefix`;
    - ``trainable``: every call has one log-probability per generated token,
      so each call can be trained on from its own prompt;
    - ``one_sequence``: that, and the prefix check passed, so the whole rollout
      is one token sequence with every generated token in it.
    """
    prefix = check_prefix(calls)
    stored: list[dict[str, Any]] = []
    previous: list[int] = []
    wholes: list[list[int]] = []
    for t, call in enumerate(calls):
        prompt = list(call["prompt_token_ids"])
        generated = list(call["token_ids"])
        logprobs = call.get("logprobs")
        if logprobs is not None and len(logprobs) != len(generated):
            logprobs = None
        shared = common_prefix_len(previous, prompt) if t else 0
        stored.append(
            {
                "call": t,
                "shared": shared,
                "prompt_tail": prompt[shared:],
                "prompt_len": len(prompt),
                "token_ids": generated,
                "logprobs": None if logprobs is None else list(logprobs),
                "finish_reason": call.get("finish_reason"),
                "start": len(prompt),
                "end": len(prompt) + len(generated),
            }
        )
        previous = prompt + generated
        wholes.append(previous)
    final = previous
    for item, whole in zip(stored, wholes, strict=True):
        item["in_final"] = final[: len(whole)] == whole
    trainable = all(c["logprobs"] is not None for c in stored)
    return {
        "calls": stored,
        "final_len": len(final),
        "generated_tokens": sum(len(c["token_ids"]) for c in stored),
        "prefix": prefix,
        "trainable": trainable,
        "one_sequence": trainable and prefix["ok"],
    }


def rebuild_prompts(record: dict[str, Any]) -> list[list[int]]:
    """Every call's full prompt, rebuilt from the stored tails."""
    prompts: list[list[int]] = []
    previous: list[int] = []
    for item in record["calls"]:
        prompt = previous[: item["shared"]] + list(item["prompt_tail"])
        prompts.append(prompt)
        previous = prompt + list(item["token_ids"])
    return prompts


def rebuild_final(record: dict[str, Any]) -> list[int]:
    """The full final token sequence: the last prompt plus the last generated tokens."""
    if not record["calls"]:
        return []
    return rebuild_prompts(record)[-1] + list(record["calls"][-1]["token_ids"])


def generated_positions(
    record: dict[str, Any], *, allow_partial: bool = False
) -> list[dict[str, Any]]:
    """One entry per model-generated token: call, position, token ID, log-probability.

    Positions index the final sequence, so this is for a record that is
    ``one_sequence``.  Raises ``ValueError`` otherwise:

    - for a record that is not ``trainable`` (a call without log-probabilities),
      always;
    - for a record whose prefix check failed, unless *allow_partial* is true.
      The calls before a break are not part of the final sequence, and handing
      back only the calls after it would look like the whole rollout.  With
      *allow_partial* exactly that is returned; :func:`per_call_items` gives
      every call instead, each with its own prompt.
    """
    if not record["trainable"]:
        missing = [c["call"] for c in record["calls"] if c["logprobs"] is None]
        raise ValueError(f"not trainable: no usable log-probabilities for calls {missing}")
    outside = [c["call"] for c in record["calls"] if not c["in_final"]]
    if outside and not allow_partial:
        raise ValueError(
            f"not one sequence: calls {outside} are not part of the final sequence; use "
            "per_call_items, or pass allow_partial=True to get only the calls that are"
        )
    out: list[dict[str, Any]] = []
    for item in record["calls"]:
        if not item["in_final"]:
            continue
        for i, (token, logprob) in enumerate(zip(item["token_ids"], item["logprobs"], strict=True)):
            out.append(
                {
                    "call": item["call"],
                    "position": item["start"] + i,
                    "token_id": token,
                    "logprob": logprob,
                }
            )
    return out


def per_call_items(record: dict[str, Any]) -> list[dict[str, Any]]:
    """One training item per call: its own prompt, generated tokens and log-probabilities.

    This is how a rollout whose prefix check failed is trained on: every call
    from the prompt the server really rendered for it.  Raises ``ValueError``
    for a record that is not ``trainable``.
    """
    if not record["trainable"]:
        missing = [c["call"] for c in record["calls"] if c["logprobs"] is None]
        raise ValueError(f"not trainable: no usable log-probabilities for calls {missing}")
    return [
        {
            "call": item["call"],
            "prompt_token_ids": prompt,
            "token_ids": list(item["token_ids"]),
            "logprobs": list(item["logprobs"]),
        }
        for item, prompt in zip(record["calls"], rebuild_prompts(record), strict=True)
    ]


def verify_record(record: dict[str, Any]) -> list[str]:
    """Self-consistency problems of a token record; empty when it is sound."""
    problems: list[str] = []
    final = rebuild_final(record)
    if len(final) != record["final_len"]:
        problems.append("the final sequence does not rebuild to its recorded length")
    for item, prompt in zip(record["calls"], rebuild_prompts(record), strict=True):
        if len(prompt) != item["prompt_len"]:
            problems.append(f"call {item['call']}: rebuilt prompt length differs")
        if item["logprobs"] is not None and len(item["token_ids"]) != len(item["logprobs"]):
            problems.append(
                f"call {item['call']}: token IDs and log-probabilities differ in length"
            )
        if item["in_final"] and final[item["start"] : item["end"]] != list(item["token_ids"]):
            problems.append(
                f"call {item['call']}: generated tokens are not at their recorded position"
            )
    if record["prefix"]["ok"] and not all(item["in_final"] for item in record["calls"]):
        problems.append("the prefix check passed but a call is not part of the final sequence")
    return problems
