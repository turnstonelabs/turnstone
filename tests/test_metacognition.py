"""Tests for turnstone.core.metacognition — detection, nudging, formatting."""

from turnstone.core.metacognition import (
    NUDGE_COMPLETION,
    NUDGE_CORRECTION,
    NUDGE_DENIAL,
    NUDGE_IDLE_CHILDREN_DISPLAY_CAP,
    NUDGE_IDLE_CHILDREN_WAIT_CAP,
    NUDGE_REPEAT,
    NUDGE_RESUME,
    NUDGE_START,
    NUDGE_TOOL_ERROR,
    RepeatDetector,
    detect_completion,
    detect_correction,
    format_idle_children_nudge,
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

    def test_tool_error(self):
        assert format_nudge("tool_error") == NUDGE_TOOL_ERROR

    def test_invalid(self):
        assert format_nudge("invalid") == ""


class TestToolErrorNudge:
    def test_fires(self):
        state: dict[str, float] = {}
        assert should_nudge("tool_error", state, message_count=5, memory_count=3) is True

    def test_cooldown(self):
        state: dict[str, float] = {}
        assert should_nudge("tool_error", state, message_count=5, memory_count=3) is True
        assert should_nudge("tool_error", state, message_count=6, memory_count=3) is False

    def test_not_on_first_message(self):
        state: dict[str, float] = {}
        assert should_nudge("tool_error", state, message_count=1, memory_count=3) is False

    def test_not_with_zero_memories(self):
        state: dict[str, float] = {}
        assert should_nudge("tool_error", state, message_count=5, memory_count=0) is False


class TestRepeatNudge:
    def test_format(self):
        assert format_nudge("repeat") == NUDGE_REPEAT

    def test_fires(self):
        state: dict[str, float] = {}
        assert should_nudge("repeat", state, message_count=5) is True

    def test_cooldown(self):
        state: dict[str, float] = {}
        assert should_nudge("repeat", state, message_count=5) is True
        assert should_nudge("repeat", state, message_count=6) is False

    def test_no_memory_requirement(self):
        """Repeat nudge should fire even with zero memories."""
        state: dict[str, float] = {}
        assert should_nudge("repeat", state, message_count=5, memory_count=0) is True


class TestRepeatDetector:
    """Repeat-detection streak machine — fires only when the same signature
    is recorded ``threshold`` times *consecutively* (default 3).  Recording
    any different signature resets the streak, so an interrupted repeat
    isn't flagged as a stuck loop."""

    def test_below_threshold_does_not_fire(self):
        det = RepeatDetector()
        assert det.record("a") is False
        assert det.record("a") is False  # second call still under threshold

    def test_at_threshold_fires(self):
        det = RepeatDetector()
        det.record("a")
        det.record("a")
        assert det.record("a") is True

    def test_continues_to_fire_past_threshold(self):
        # Caller is responsible for clearing after a fire — until they do,
        # subsequent identical calls keep returning True.
        det = RepeatDetector()
        det.record("a")
        det.record("a")
        assert det.record("a") is True
        assert det.record("a") is True

    def test_clear_resets_count(self):
        det = RepeatDetector()
        det.record("a")
        det.record("a")
        det.clear()
        assert det.record("a") is False  # back to 1 after clear

    def test_intervening_sig_resets_streak(self):
        # The streak is consecutive: recording any other sig mid-streak
        # discards the in-progress count.  An alternating pattern like
        # [A, A, B, A, A] is two short streaks of 2, not a streak of 4.
        det = RepeatDetector()
        det.record("a")
        det.record("a")
        assert det.record("b") is False  # b at count 1; a's streak is gone
        assert det.record("a") is False  # a starts fresh at 1
        assert det.record("a") is False  # a at 2
        assert det.record("a") is True  # a hits 3 — fresh streak completes

    def test_errored_signature_counts_toward_repeat(self):
        # Regression: when metacog was split out of the system message,
        # the error-output skip got reintroduced and stuck-loop detection
        # silently broke for tools that kept failing.  Detector itself is
        # signature-only — error vs. success is the caller's policy.
        det = RepeatDetector()
        # Caller records an errored call's sig the same as a successful one;
        # the streak is what matters.
        for _ in range(3):
            last = det.record("bash:ls /nonexistent")
        assert last is True

    def test_custom_threshold(self):
        det = RepeatDetector(threshold=2)
        assert det.record("a") is False
        assert det.record("a") is True

    def test_threshold_one_fires_immediately(self):
        det = RepeatDetector(threshold=1)
        assert det.record("a") is True


class TestFormatIdleChildrenNudge:
    """``format_idle_children_nudge`` renders the wake-driven idle_children
    body — no ``<system-reminder>`` envelope (the side-channel splice
    wraps it at the wire boundary).
    """

    def test_empty_list_returns_empty_string(self):
        # Caller short-circuits on `if not text: return` — so empty
        # input MUST produce empty output, not a header-only stub.
        assert format_idle_children_nudge([]) == ""

    def test_single_child_renders(self):
        children = [{"ws_id": "ws-abc12345", "name": "research-task", "state": "running"}]
        text = format_idle_children_nudge(children)
        assert "ws-abc12" in text  # short-id form (8 chars)
        assert "research-task" in text
        assert "running" in text
        assert "wait_for_workstream" in text
        assert "ws-abc12345" in text  # full id appears in the suggestion's ws_ids list

    def test_under_display_cap_no_overflow_line(self):
        children = [
            {"ws_id": f"ws-{i:08d}", "name": f"task-{i}", "state": "running"} for i in range(3)
        ]
        text = format_idle_children_nudge(children)
        assert "...and" not in text
        for i in range(3):
            assert f"task-{i}" in text

    def test_over_display_cap_renders_overflow_line(self):
        n = NUDGE_IDLE_CHILDREN_DISPLAY_CAP + 4
        children = [
            {"ws_id": f"ws-{i:08d}", "name": f"task-{i}", "state": "thinking"} for i in range(n)
        ]
        text = format_idle_children_nudge(children)
        assert f"...and {n - NUDGE_IDLE_CHILDREN_DISPLAY_CAP} more" in text
        # First N children are inline; later ones are folded into "...and N more".
        for i in range(NUDGE_IDLE_CHILDREN_DISPLAY_CAP):
            assert f"task-{i}" in text
        for i in range(NUDGE_IDLE_CHILDREN_DISPLAY_CAP, n):
            # Names beyond the display cap aren't visible; only counted.
            assert f"task-{i}" not in text

    def test_over_wait_cap_truncates_suggestion_ws_ids(self):
        n = NUDGE_IDLE_CHILDREN_WAIT_CAP + 5
        children = [
            {"ws_id": f"ws-{i:08d}", "name": f"task-{i}", "state": "running"} for i in range(n)
        ]
        text = format_idle_children_nudge(children)
        # The first WAIT_CAP ids appear in the suggestion; later ones don't.
        first_in_suggestion = f"ws-{NUDGE_IDLE_CHILDREN_WAIT_CAP - 1:08d}"
        first_excluded = f"ws-{NUDGE_IDLE_CHILDREN_WAIT_CAP:08d}"
        assert first_in_suggestion in text
        assert first_excluded not in text

    def test_unnamed_child_falls_back(self):
        children = [{"ws_id": "ws-deadbeef", "name": "", "state": "attention"}]
        text = format_idle_children_nudge(children)
        assert "(unnamed)" in text
        assert "attention" in text

    def test_missing_state_renders_question_mark(self):
        children = [{"ws_id": "ws-12345678", "name": "x"}]
        text = format_idle_children_nudge(children)
        # Defensive default — exotic state keys / partial dicts shouldn't crash.
        assert "?" in text

    def test_no_system_reminder_envelope(self):
        # The side-channel ``_apply_reminders_for_provider`` splice
        # adds ``<system-reminder>`` at the wire boundary; the formatter
        # MUST NOT wrap, or the model would see a doubled envelope.
        text = format_idle_children_nudge([{"ws_id": "ws-x", "name": "y", "state": "running"}])
        assert "<system-reminder>" not in text
        assert "</system-reminder>" not in text

    def test_format_nudge_returns_empty_for_idle_children(self):
        # The static map's idle_children entry is the empty string by
        # design — format_idle_children_nudge produces the real body.
        assert format_nudge("idle_children") == ""

    def test_should_nudge_recognises_idle_children_type(self, monkeypatch):
        # Type registration in ``_NUDGE_MAP`` makes ``should_nudge``
        # recognise it for cooldown gating; without the entry it would
        # silently return False on every call.
        state: dict[str, float] = {}
        # message_count > 1 to clear the first-message gate.
        assert should_nudge("idle_children", state, message_count=4, memory_count=0) is True
        # Cooldown set on success → second immediate call returns False.
        assert should_nudge("idle_children", state, message_count=5, memory_count=0) is False


class TestSanitizePayload:
    """Shared sanitiser used by ``idle_children`` and ``watch_triggered``
    producers.  Strips ASCII control chars (except TAB/LF/CR), Unicode
    steering vectors (bidi, zero-width, BOM, tag chars), and angle-bracket
    tag breakers — keeps everything else intact.
    """

    def test_empty_input_returns_empty(self):
        from turnstone.core.metacognition import sanitize_payload

        assert sanitize_payload("") == ""

    def test_strips_ascii_control_chars(self):
        """``\\x00``-``\\x1f`` minus TAB/LF/CR plus ``\\x7f`` (DEL) become spaces."""
        from turnstone.core.metacognition import sanitize_payload

        # BEL (0x07), VT (0x0b), FF (0x0c) — all in strip set.
        assert sanitize_payload("a\x07b\x0bc\x0cd") == "a b c d"
        # DEL (0x7f).
        assert sanitize_payload("a\x7fb") == "a b"

    def test_preserves_tab_lf_cr(self):
        """TAB / LF / CR are intentionally preserved so multi-line shell
        output keeps its line structure when sanitised as a watch payload.
        """
        from turnstone.core.metacognition import sanitize_payload

        # Newlines kept; only the leading + trailing strip happens.
        out = sanitize_payload("line1\nline2\n\tindented\rline3")
        assert out == "line1\nline2\n\tindented\rline3"

    def test_strips_bidi_and_zero_width(self):
        from turnstone.core.metacognition import sanitize_payload

        # U+202E RIGHT-TO-LEFT OVERRIDE; U+200B ZERO WIDTH SPACE.
        assert sanitize_payload("a‮b​c") == "a b c"

    def test_strips_angle_bracket_tag_breakers(self):
        from turnstone.core.metacognition import sanitize_payload

        # "<" / ">" go away entirely (not replaced with space) so a name
        # like "</thinking>" doesn't leave a hole the model can read as
        # a structural marker.
        assert sanitize_payload("a</thinking>b") == "a/thinkingb"
