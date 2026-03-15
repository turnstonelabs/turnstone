"""Tests for turnstone.core.metacognition — detection, nudging, formatting."""

from turnstone.core.metacognition import (
    NUDGE_COMPLETION,
    NUDGE_CORRECTION,
    NUDGE_DENIAL,
    NUDGE_RESUME,
    NUDGE_START,
    detect_completion,
    detect_correction,
    format_nudge,
    should_nudge,
)


class TestDetectCorrection:
    """Strong patterns always fire; weak 'no <word>' uses allowlist."""

    # -- strong patterns (always fire) --

    def test_no_comma(self):
        assert detect_correction("no, that's wrong") is True

    def test_no_period(self):
        assert detect_correction("no. do it differently") is True

    def test_dont(self):
        assert detect_correction("don't use tabs") is True

    def test_stop(self):
        assert detect_correction("stop adding comments") is True

    def test_actually(self):
        assert detect_correction("actually, use pytest instead") is True

    def test_instead(self):
        assert detect_correction("instead, try this approach") is True

    def test_wrong(self):
        assert detect_correction("wrong, the port is 8080") is True

    def test_i_said(self):
        assert detect_correction("I said use snake_case") is True

    def test_i_meant(self):
        assert detect_correction("I meant the other file") is True

    def test_please_dont(self):
        assert detect_correction("please don't mock the database") is True

    # -- weak pattern: "no" + allowlisted context word --

    def test_no_space(self):
        assert detect_correction("no I meant the other one") is True

    def test_no_that(self):
        assert detect_correction("no that's wrong") is True

    def test_no_it(self):
        assert detect_correction("no it should be different") is True

    def test_no_the(self):
        assert detect_correction("no the other one") is True

    def test_no_not(self):
        assert detect_correction("no not that file") is True

    def test_no_you(self):
        assert detect_correction("no you should use pytest") is True

    # -- negatives: "no <word>" not in allowlist --

    def test_negative_no_problem(self):
        assert detect_correction("no problem") is False

    def test_negative_no_worries(self):
        assert detect_correction("no worries") is False

    def test_negative_no_rush(self):
        assert detect_correction("no rush") is False

    def test_negative_no_one(self):
        assert detect_correction("no one knows") is False

    def test_negative_no_thanks(self):
        assert detect_correction("no thanks") is False

    def test_negative_no_doubt(self):
        assert detect_correction("no doubt about it") is False

    def test_negative_no_idea(self):
        assert detect_correction("no idea what you mean") is False

    def test_negative_no_kidding(self):
        assert detect_correction("no kidding") is False

    def test_negative_no_luck(self):
        assert detect_correction("no luck finding the bug") is False

    # -- negatives: unrelated messages --

    def test_negative_notice(self):
        assert detect_correction("I noticed the test passes") is False

    def test_negative_nobody(self):
        assert detect_correction("nobody knows the answer") is False

    def test_negative_innovation(self):
        assert detect_correction("innovation in AI is exciting") is False

    def test_negative_normal(self):
        assert detect_correction("can you refactor this function?") is False

    def test_negative_empty(self):
        assert detect_correction("") is False

    def test_negative_note(self):
        assert detect_correction("note that this requires Python 3.11") is False

    def test_negative_nonstop(self):
        assert detect_correction("nonstop improvements to the codebase") is False


class TestDetectCompletion:
    """Strong patterns always fire; weak patterns gated by length + continuation."""

    # -- strong patterns (always fire) --

    def test_thats_all(self):
        assert detect_completion("that's all for now") is True

    def test_lgtm(self):
        assert detect_completion("lgtm") is True

    # -- weak patterns: short message, no continuation --

    def test_thanks(self):
        assert detect_completion("thanks, that's perfect") is True

    def test_thanks_standalone(self):
        assert detect_completion("thanks") is True

    def test_thanks_exclaim(self):
        assert detect_completion("thanks!") is True

    def test_looks_good(self):
        assert detect_completion("looks good to me") is True

    def test_perfect(self):
        assert detect_completion("perfect") is True

    def test_done(self):
        assert detect_completion("done") is True

    def test_great_job(self):
        assert detect_completion("great job") is True

    def test_that_works(self):
        assert detect_completion("that works") is True

    # -- negatives: "thanks for" is acknowledgment --

    def test_negative_thanks_for(self):
        assert detect_completion("thanks for the update") is False

    def test_negative_thanks_for_looking(self):
        assert detect_completion("thanks for looking into this") is False

    # -- negatives: continuation markers suppress weak patterns --

    def test_negative_thanks_but(self):
        assert detect_completion("thanks but can you also add tests") is False

    def test_negative_thanks_though(self):
        assert detect_completion("thanks though I have one more question") is False

    def test_negative_looks_good_but(self):
        assert detect_completion("looks good but can you also add validation") is False

    def test_negative_perfect_now(self):
        assert detect_completion("perfect, now add error handling") is False

    def test_negative_done_can_you(self):
        assert detect_completion("done with that, can you start on the tests?") is False

    def test_negative_question_mark(self):
        assert detect_completion("can you add error handling?") is False

    # -- negatives: long messages suppress weak patterns --

    def test_negative_thanks_long(self):
        msg = "thanks, this is really helpful — I was also wondering about the deployment pipeline and whether we need to update the CI config"
        assert detect_completion(msg) is False

    def test_negative_looks_good_long(self):
        msg = "looks good overall, there are a few things I'd like to tweak though — the error messages could be more descriptive and the retry logic needs a backoff"
        assert detect_completion(msg) is False

    # -- negatives: unrelated --

    def test_negative_empty(self):
        assert detect_completion("") is False


class TestShouldNudge:
    def test_basic_fires(self):
        state: dict[str, float] = {}
        assert should_nudge("correction", state, message_count=3, memory_count=0) is True

    def test_cooldown(self):
        state: dict[str, float] = {}
        should_nudge("correction", state, message_count=3, memory_count=0)
        assert should_nudge("correction", state, message_count=3, memory_count=0) is False

    def test_different_types_independent(self):
        state: dict[str, float] = {}
        should_nudge("correction", state, message_count=3, memory_count=0)
        assert should_nudge("denial", state, message_count=3, memory_count=0) is True

    def test_no_nudge_first_message(self):
        state: dict[str, float] = {}
        assert should_nudge("correction", state, message_count=1, memory_count=0) is False

    def test_resume_requires_memories(self):
        state: dict[str, float] = {}
        assert should_nudge("resume", state, message_count=5, memory_count=0) is False
        assert should_nudge("resume", state, message_count=5, memory_count=3) is True

    def test_resume_allowed_on_first_message(self):
        state: dict[str, float] = {}
        assert should_nudge("resume", state, message_count=1, memory_count=3) is True

    def test_start_fires_on_first_message_with_memories(self):
        state: dict[str, float] = {}
        assert should_nudge("start", state, message_count=1, memory_count=3) is True

    def test_start_requires_memories(self):
        state: dict[str, float] = {}
        assert should_nudge("start", state, message_count=1, memory_count=0) is False

    def test_start_only_on_first_message(self):
        state: dict[str, float] = {}
        assert should_nudge("start", state, message_count=2, memory_count=3) is False

    def test_invalid_type(self):
        state: dict[str, float] = {}
        assert should_nudge("invalid", state, message_count=3, memory_count=0) is False


class TestFormatNudge:
    def test_correction(self):
        assert format_nudge("correction") == NUDGE_CORRECTION

    def test_denial(self):
        assert format_nudge("denial") == NUDGE_DENIAL

    def test_resume(self):
        assert format_nudge("resume") == NUDGE_RESUME

    def test_completion(self):
        assert format_nudge("completion") == NUDGE_COMPLETION

    def test_start(self):
        assert format_nudge("start") == NUDGE_START

    def test_invalid(self):
        assert format_nudge("invalid") == ""
