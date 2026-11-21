"""Prefix check and token record of a rollout, on hand-made token lists."""

import pytest

from turnstone.rollout.tokens import (
    build_token_record,
    check_prefix,
    describe_break,
    generated_positions,
    per_call_items,
    rebuild_final,
    rebuild_prompts,
    verify_record,
)


def call(prompt, generated):
    return {
        "prompt_token_ids": list(prompt),
        "token_ids": list(generated),
        "logprobs": [-0.01 * (i + 1) for i in range(len(generated))],
        "finish_reason": "stop",
    }


def growing_episode():
    first = call([1, 2, 3], [10, 11])
    second = call([1, 2, 3, 10, 11, 4, 5], [12])
    third = call([1, 2, 3, 10, 11, 4, 5, 12, 6], [13, 14, 15])
    return [first, second, third]


def test_prefix_holds_for_a_growing_conversation():
    report = check_prefix(growing_episode())
    assert report == {"ok": True, "calls": 3, "breaks": []}


def test_single_call_and_empty_episode_pass():
    assert check_prefix([call([1, 2], [3])])["ok"]
    assert check_prefix([])["ok"]
    assert rebuild_final(build_token_record([])) == []


def test_record_rebuilds_sequence_and_positions():
    calls = growing_episode()
    record = build_token_record(calls)
    assert record["prefix"]["ok"]
    final = rebuild_final(record)
    assert final == [1, 2, 3, 10, 11, 4, 5, 12, 6, 13, 14, 15]
    # The sequence is derived, not stored a second time.
    assert "final_token_ids" not in record and record["final_len"] == len(final)
    assert rebuild_prompts(record) == [c["prompt_token_ids"] for c in calls]
    # Shared tokens are stored once: only the first call keeps its whole prompt.
    assert [c["prompt_tail"] for c in record["calls"]] == [[1, 2, 3], [4, 5], [6]]
    assert [(c["start"], c["end"]) for c in record["calls"]] == [
        (3, 5),
        (7, 8),
        (9, 12),
    ]
    positions = generated_positions(record)
    assert [p["position"] for p in positions] == [3, 4, 7, 9, 10, 11]
    for p in positions:
        assert final[p["position"]] == p["token_id"]
    assert [p["logprob"] for p in positions] == [
        -0.01,
        -0.02,
        -0.01,
        -0.01,
        -0.02,
        -0.03,
    ]
    assert record["generated_tokens"] == 6
    assert verify_record(record) == []


def test_break_inside_generated_tokens_is_located():
    calls = growing_episode()
    # The second call's prompt replays the first call's second generated token differently.
    calls[1]["prompt_token_ids"][4] = 99
    report = check_prefix(calls)
    assert not report["ok"]
    first = report["breaks"][0]
    assert first["call"] == 1
    assert first["kind"] == "mismatch"
    assert first["position"] == 4
    assert first["region"] == "previous_generated"
    assert first["offset"] == 1
    assert first["expected_token_id"] == 11
    assert first["found_token_id"] == 99
    text = describe_break(first)
    assert (
        "call 1 does not extend call 0" in text
        and "position 4" in text
        and "generated tokens" in text
    )
    # The third call was built on the original text, so it does not extend the changed prompt.
    assert [b["call"] for b in report["breaks"]] == [1, 2]


def test_break_inside_earlier_prompt_is_located():
    calls = growing_episode()
    calls[2]["prompt_token_ids"][1] = 77
    report = check_prefix(calls)
    assert [b["call"] for b in report["breaks"]] == [2]
    found = report["breaks"][0]
    assert (found["position"], found["region"], found["offset"]) == (
        1,
        "previous_prompt",
        1,
    )
    assert (found["expected_token_id"], found["found_token_id"]) == (2, 77)


def test_shorter_later_prompt_is_a_break():
    calls = growing_episode()
    # As after a compaction: the later prompt is a shorter, different conversation.
    calls[2]["prompt_token_ids"] = [1, 2, 3, 10]
    report = check_prefix(calls)
    found = report["breaks"][0]
    assert found["call"] == 2 and found["kind"] == "shorter"
    assert found["position"] == 4 and found["found_token_id"] is None
    assert "ends there" in describe_break(found)


def test_record_stays_lossless_when_the_prefix_breaks():
    calls = growing_episode()
    calls[1]["prompt_token_ids"][4] = 99
    calls[2]["prompt_token_ids"][4] = 99
    record = build_token_record(calls)
    assert not record["prefix"]["ok"]
    assert [b["call"] for b in record["prefix"]["breaks"]] == [1]
    # Every call's own prompt still rebuilds exactly.
    assert rebuild_prompts(record) == [c["prompt_token_ids"] for c in calls]
    assert rebuild_final(record) == calls[2]["prompt_token_ids"] + calls[2]["token_ids"]
    # The first call's tokens are not part of the final sequence.
    assert [c["in_final"] for c in record["calls"]] == [False, True, True]
    assert verify_record(record) == []
    # Each call is sound, the rollout is not one sequence, and the record says both.
    assert record["trainable"] is True and record["one_sequence"] is False


def test_positions_are_refused_for_a_record_that_is_not_one_sequence():
    """Handing back only the calls after a break would look like the whole rollout:
    a trainer following the positions would drop the first call without a word."""
    calls = growing_episode()
    calls[1]["prompt_token_ids"][4] = 99
    calls[2]["prompt_token_ids"][4] = 99
    record = build_token_record(calls)

    with pytest.raises(ValueError, match="not one sequence: calls .0. are not part"):
        generated_positions(record)
    # Asked for explicitly, the calls that are in the final sequence come back...
    partial = generated_positions(record, allow_partial=True)
    assert {p["call"] for p in partial} == {1, 2}
    # ...and every call can be trained on from its own prompt.
    items = per_call_items(record)
    assert [i["call"] for i in items] == [0, 1, 2]
    assert [i["prompt_token_ids"] for i in items] == [c["prompt_token_ids"] for c in calls]
    assert [i["token_ids"] for i in items] == [c["token_ids"] for c in calls]
    assert [i["logprobs"] for i in items] == [c["logprobs"] for c in calls]


def test_a_replaced_last_token_is_named_as_such():
    calls = growing_episode()
    calls[1]["prompt_token_ids"][4] = 99  # call 0 generated [10, 11]; 11 was its last token
    calls[2]["prompt_token_ids"][4] = 99
    (found,) = build_token_record(calls)["prefix"]["breaks"]
    assert "the turn's last token, which the server replaced" in describe_break(found)


def test_a_call_without_usable_log_probabilities_makes_the_record_untrainable():
    calls = growing_episode()
    calls[1]["logprobs"] = None
    record = build_token_record(calls)
    assert record["trainable"] is False
    assert [c["logprobs"] is None for c in record["calls"]] == [False, True, False]
    # The tokens are still all there; only pairing them with values is refused.
    assert rebuild_final(record) == [1, 2, 3, 10, 11, 4, 5, 12, 6, 13, 14, 15]
    assert verify_record(record) == []
    # No positions are handed out for a record with a hole in it, whatever is asked.
    with pytest.raises(ValueError, match="calls .1."):
        generated_positions(record)
    with pytest.raises(ValueError, match="not trainable"):
        generated_positions(record, allow_partial=True)
    with pytest.raises(ValueError, match="not trainable"):
        per_call_items(record)
    assert record["one_sequence"] is False


def test_a_wrong_count_of_log_probabilities_is_not_paired():
    calls = growing_episode()
    calls[2]["logprobs"] = calls[2]["logprobs"][:-1]
    record = build_token_record(calls)
    assert record["trainable"] is False and record["calls"][2]["logprobs"] is None


def test_a_sound_record_is_trainable_and_one_sequence():
    record = build_token_record(growing_episode())
    assert record["trainable"] is True and record["one_sequence"] is True
