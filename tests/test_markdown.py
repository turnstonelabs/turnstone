"""Tests for turnstone.ui.markdown — MarkdownRenderer."""

import pytest

from turnstone.ui import markdown
from turnstone.ui.markdown import MarkdownRenderer


# Colour is off under pytest (output is piped), so each code would be "" and a
# check that one is in the output would always pass: these run with colour on.
@pytest.mark.usefixtures("colour_on")
class TestMarkdownRenderer:
    def setup_method(self):
        self.r = MarkdownRenderer()

    def test_header_rendering(self):
        result = self.r.feed("# Hello\n")
        assert markdown.BOLD in result
        assert markdown.MAGENTA in result
        assert "Hello" in result

    def test_h2_header(self):
        result = self.r.feed("## Sub\n")
        assert markdown.BOLD in result
        assert markdown.MAGENTA in result
        assert "Sub" in result

    def test_bold_text(self):
        result = self.r.feed("some **bold** text\n")
        assert markdown.BOLD in result
        assert "bold" in result

    def test_underscore_bold(self):
        result = self.r.feed("some __bold__ text\n")
        assert markdown.BOLD in result
        assert "bold" in result

    def test_inline_code(self):
        result = self.r.feed("use `code` here\n")
        assert markdown.CYAN in result
        assert "code" in result

    def test_code_block_toggle(self):
        # Opening fence
        result = self.r.feed("```python\n")
        assert markdown.DIM in result
        assert self.r.in_code_block is True

        # Content inside code block
        result = self.r.feed("x = 1\n")
        assert markdown.CYAN in result

        # Closing fence
        result = self.r.feed("```\n")
        assert markdown.DIM in result
        assert self.r.in_code_block is False

    def test_bullet_list_cyan(self):
        result = self.r.feed("- item one\n")
        assert result == markdown.CYAN + "-" + markdown.RESET + " item one\n"

    def test_asterisk_bullet_list(self):
        result = self.r.feed("* item one\n")
        assert result == markdown.CYAN + "*" + markdown.RESET + " item one\n"

    def test_numbered_list_cyan(self):
        result = self.r.feed("1. first\n")
        assert result == markdown.CYAN + "1." + markdown.RESET + " first\n"

    def test_indented_list_keeps_its_indentation(self):
        result = self.r.feed("  - nested\n")
        assert result == "  " + markdown.CYAN + "-" + markdown.RESET + " nested\n"

    def test_flush_returns_remaining_buffer(self):
        # Feed text without a newline
        result = self.r.feed("no newline yet")
        assert result == ""  # No complete line yet

        # Flush should return the buffered content
        result = self.r.flush()
        assert "no newline yet" in result

    def test_flush_empty_buffer(self):
        assert self.r.flush() == ""

    def test_italic_text(self):
        result = self.r.feed("some *italic* text\n")
        assert markdown.ITALIC in result
        assert "italic" in result


ESC = chr(27)
CR = chr(13)


@pytest.fixture
def colour_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Colour as in a terminal (tests run piped, which turns it off)."""
    codes = (("BOLD", "1"), ("CYAN", "36"), ("DIM", "2"), ("ITALIC", "3"), ("MAGENTA", "35"))
    for name, number in (*codes, ("RESET", "0")):
        monkeypatch.setattr(markdown, name, ESC + "[" + number + "m")


class TestModelText:
    """A reply is a model's text: its control characters show, before the
    renderer adds codes, and a CR LF is a line ending."""

    def test_control_characters_show_and_the_renderer_codes_act(self, colour_on: None) -> None:
        out = MarkdownRenderer().feed("**bold**" + ESC + "[2K\n")
        assert out == markdown.BOLD + "bold" + markdown.RESET + chr(0x241B) + "[2K\n"

    def test_a_crlf_cut_across_two_tokens_is_one_line_ending(self) -> None:
        r = MarkdownRenderer()
        assert r.feed("one" + CR) == ""
        assert r.feed("\ntwo" + CR + "\n") == "one\ntwo\n"

    def test_a_lone_carriage_return_shows(self) -> None:
        r = MarkdownRenderer()
        assert r.feed("a" + CR + "b\n") == "a" + chr(0x240D) + "b\n"
        r.feed("tail" + CR)
        assert r.flush() == "tail" + chr(0x240D)

    def test_a_form_feed_before_a_fence_still_opens_the_block(self) -> None:
        r = MarkdownRenderer()
        r.feed(chr(12) + "```\n")
        assert r.in_code_block is True
        r.feed("```\n")
        assert r.in_code_block is False

    def test_a_list_marker_after_a_whitespace_control_still_counts(self, colour_on: None) -> None:
        out = MarkdownRenderer().feed(chr(0x85) + "- item\n")
        assert out == chr(0x241B) + "E" + markdown.CYAN + "-" + markdown.RESET + " item\n"

    def test_every_branch_shows_the_line(self, colour_on: None) -> None:
        """A heading, a fence and a line in a code block print the model's
        control characters shown, as any other line does."""
        r = MarkdownRenderer()
        shown = chr(0x241B) + "[2J"
        assert r.feed("# Title" + ESC + "[2J\n") == (
            markdown.BOLD + markdown.MAGENTA + "Title" + shown + markdown.RESET + "\n"
        )
        assert r.feed("```" + ESC + "[?7l\n") == (
            markdown.DIM + "```" + chr(0x241B) + "[?7l" + markdown.RESET + "\n"
        )
        assert r.feed("x = 1" + ESC + "[8m\n") == (
            markdown.CYAN + "x = 1" + chr(0x241B) + "[8m" + markdown.RESET + "\n"
        )

    def test_a_shown_c1_control_keeps_its_underscore(self, colour_on: None) -> None:
        """U+009F shows as U+241B "_": that underscore is the control's, so it
        never pairs with the model's own into a bold marker."""
        shown_apc = chr(0x241B) + "_"
        assert MarkdownRenderer().feed(chr(0x9F) + "_x__\n") == shown_apc + "_x__\n"
        assert MarkdownRenderer().feed("__x" + chr(0x9F) + "_\n") == "__x" + shown_apc + "_\n"
        bold = MarkdownRenderer().feed("__x__ " + chr(0x9F) + "\n")
        assert bold == markdown.BOLD + "x" + markdown.RESET + " " + shown_apc + "\n"
