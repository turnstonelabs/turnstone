"""Tests for turnstone.core.tool_advisory."""

from __future__ import annotations

import pytest

from turnstone.core.tool_advisory import (
    SYSTEM_TURN_SOURCES,
    make_system_turn,
    parse_priority,
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

    def test_meta_keys_underscore_prefixed(self) -> None:
        turn = make_system_turn(
            "watch_triggered", "ci failed", watch_name="ci", priority="important"
        )
        assert turn["_watch_name"] == "ci"
        assert turn["_priority"] == "important"
        assert turn["role"] == "system"
        assert turn["_source"] == "watch_triggered"
        assert turn["content"] == "ci failed"

    def test_already_underscored_meta_kept(self) -> None:
        turn = make_system_turn("repeat", "stop repeating", _event_id=7)
        assert turn["_event_id"] == 7

    def test_unknown_source_rejected(self) -> None:
        with pytest.raises(ValueError, match="unknown system-turn source"):
            make_system_turn("bogus", "x")

    def test_meta_key_colliding_with_source_rejected(self) -> None:
        # `source=` can't even reach **meta — it's a named parameter, so Python
        # raises TypeError first.  The only reachable collision is an explicit
        # ``_source=`` meta key, which the builder rejects rather than letting it
        # silently clobber the validated source.
        with pytest.raises(ValueError, match="collides with reserved"):
            make_system_turn("repeat", "x", _source="evil")

    def test_vocabulary_mirrors_nudge_map_both_directions(self) -> None:
        # The nudge-derived sources must equal _NUDGE_MAP exactly: a new nudge
        # type must be added here, and a removed/renamed one must not leave a
        # stale source behind.  output_guard / user_interjection / skill_hint
        # come from the advisory producers, not the nudge map.
        from turnstone.core.metacognition import _NUDGE_MAP

        nudge_sources = SYSTEM_TURN_SOURCES - {"output_guard", "user_interjection", "skill_hint"}
        assert nudge_sources == set(_NUDGE_MAP)


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
