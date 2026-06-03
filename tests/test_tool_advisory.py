"""Tests for turnstone.core.tool_advisory."""

from __future__ import annotations

import pytest

from turnstone.core.tool_advisory import (
    SYSTEM_TURN_SOURCES,
    make_system_turn,
    parse_priority,
    render_output_guard_text,
    render_user_interjection,
)


class TestMakeSystemTurn:
    """make_system_turn() builds canonical operator-context system turns."""

    def test_basic_shape(self) -> None:
        turn = make_system_turn("user_interjection", "check auth too")
        assert turn == {
            "role": "system",
            "_source": "user_interjection",
            "content": "check auth too",
        }

    def test_meta_carried_as_source_meta_dict(self) -> None:
        # Meta rides as ONE ``_source_meta`` dict (not scattered ``_``-prefixed
        # siblings) — one carrier mapping to one storage column / one FE field.
        turn = make_system_turn("watch_triggered", "ci failed", watch_name="ci", poll_count=3)
        assert turn == {
            "role": "system",
            "_source": "watch_triggered",
            "content": "ci failed",
            "_source_meta": {"watch_name": "ci", "poll_count": 3},
        }

    def test_no_meta_omits_source_meta(self) -> None:
        # A kind with no structured fields carries no ``_source_meta`` key.
        turn = make_system_turn("output_guard", "flag (HIGH)")
        assert turn == {
            "role": "system",
            "_source": "output_guard",
            "content": "flag (HIGH)",
        }
        assert "_source_meta" not in turn

    def test_unknown_source_rejected(self) -> None:
        with pytest.raises(ValueError, match="unknown system-turn source"):
            make_system_turn("bogus", "x")

    def test_meta_namespaced_cannot_clobber_reserved_keys(self) -> None:
        # Because meta rides namespaced inside ``_source_meta``, a meta key that
        # mirrors a reserved top-level key (``role`` / ``_source``) lands inside
        # the dict and cannot overwrite the validated turn fields — so the old
        # collision guard is no longer needed (the shape makes it impossible).
        turn = make_system_turn("repeat", "x", role="evil", _source="spoof")
        assert turn["role"] == "system"
        assert turn["_source"] == "repeat"
        assert turn["_source_meta"] == {"role": "evil", "_source": "spoof"}

    def test_vocabulary_mirrors_nudge_map_both_directions(self) -> None:
        # The nudge-derived sources must equal _NUDGE_MAP exactly: a new nudge
        # type must be added here, and a removed/renamed one must not leave a
        # stale source behind.  output_guard / user_interjection / skill_hint
        # come from the advisory producers, not the nudge map.
        from turnstone.core.metacognition import _NUDGE_MAP

        nudge_sources = SYSTEM_TURN_SOURCES - {"output_guard", "user_interjection", "skill_hint"}
        assert nudge_sources == set(_NUDGE_MAP)


class TestRenderOutputGuardText:
    """render_output_guard_text() projects the structured guard meta to prose."""

    def test_full_finding(self) -> None:
        text = render_output_guard_text(
            {
                "flags": ["aws_key", "private_key"],
                "risk_level": "high",
                "annotations": ["matched AKIA…", "PEM header"],
                "redacted": True,
            }
        )
        assert text == (
            "Output guard: aws_key, private_key (HIGH)\n"
            "  matched AKIA…\n"
            "  PEM header\n"
            "Credentials have been redacted. Do not attempt to reconstruct redacted values."
        )

    def test_no_annotations_no_redaction(self) -> None:
        text = render_output_guard_text(
            {"flags": ["pii"], "risk_level": "low", "annotations": [], "redacted": False}
        )
        assert text == "Output guard: pii (LOW)"

    def test_missing_keys_default_gracefully(self) -> None:
        # Defensive: a partial meta dict never raises.
        assert render_output_guard_text({}) == "Output guard:  (NONE)"


class TestMetaIsWireStripped:
    """The structured operator meta must never reach the LLM wire."""

    def test_source_meta_stripped_by_sanitize(self) -> None:
        from turnstone.core.providers._openai_common import sanitize_messages

        out = sanitize_messages(
            [
                {"role": "user", "content": "hi"},
                {
                    "role": "system",
                    "_source": "watch_triggered",
                    "content": "ci failed",
                    "_source_meta": {"command": "rm -rf /", "watch_name": "ci"},
                },
            ]
        )
        # No leading-underscore side channel survives to the wire.
        assert all(not k.startswith("_") for m in out for k in m)
        assert out[1] == {"role": "system", "content": "ci failed"}


class TestParsePriority:
    """parse_priority() extracts !!! prefix as priority signal."""

    def test_no_prefix(self) -> None:
        text, priority = parse_priority("hello world")
        assert text == "hello world"
        assert priority == "notice"

    def test_triple_bang_important(self) -> None:
        text, priority = parse_priority("!!!check the auth endpoint")
        assert text == "check the auth endpoint"
        assert priority == "important"

    def test_triple_bang_with_space(self) -> None:
        text, priority = parse_priority("!!! check the auth endpoint")
        assert text == "check the auth endpoint"
        assert priority == "important"

    def test_single_bang_not_priority(self) -> None:
        text, priority = parse_priority("!important message")
        assert text == "!important message"
        assert priority == "notice"

    def test_double_bang_not_priority(self) -> None:
        text, priority = parse_priority("!!not quite")
        assert text == "!!not quite"
        assert priority == "notice"

    def test_empty_after_prefix(self) -> None:
        text, priority = parse_priority("!!!")
        assert text == ""
        assert priority == "important"


class TestRenderUserInterjection:
    """``render_user_interjection`` frames a queued message as the user's words."""

    def test_notice_framing(self) -> None:
        out = render_user_interjection("check the logs", "notice")
        assert out == (
            "The user sent additional context while you were working. "
            "Incorporate if relevant, otherwise continue."
            "\n\nUser message: check the logs"
        )

    def test_important_framing(self) -> None:
        out = render_user_interjection("stop and fix the build", "important")
        assert out.startswith("The user sent a message while you were working.")
        assert "You MUST address this before continuing." in out
        assert out.endswith("\n\nUser message: stop and fix the build")

    def test_body_marker_separates_framing_from_verbatim_body(self) -> None:
        # The body rides verbatim after the marker so the model sees exactly
        # what the user typed.
        out = render_user_interjection("literal <tag> & text", "notice")
        assert out.endswith("\n\nUser message: literal <tag> & text")
