"""Tests for turnstone.ui.markdown — MarkdownRenderer."""

from turnstone.ui.markdown import MarkdownRenderer
from turnstone.ui.colors import BOLD, MAGENTA, CYAN, DIM, ITALIC, RESET


class TestMarkdownRenderer:
    def setup_method(self):
        self.r = MarkdownRenderer()

    def test_header_rendering(self):
        result = self.r.feed("# Hello\n")
        assert BOLD in result
        assert MAGENTA in result
        assert "Hello" in result

    def test_h2_header(self):
        result = self.r.feed("## Sub\n")
        assert BOLD in result
        assert MAGENTA in result
        assert "Sub" in result

    def test_bold_text(self):
        result = self.r.feed("some **bold** text\n")
        assert BOLD in result
        assert "bold" in result

    def test_underscore_bold(self):
        result = self.r.feed("some __bold__ text\n")
        assert BOLD in result
        assert "bold" in result

    def test_inline_code(self):
        result = self.r.feed("use `code` here\n")
        assert CYAN in result
        assert "code" in result

    def test_code_block_toggle(self):
        # Opening fence
        result = self.r.feed("```python\n")
        assert DIM in result
        assert self.r.in_code_block is True

        # Content inside code block
        result = self.r.feed("x = 1\n")
        assert CYAN in result

        # Closing fence
        result = self.r.feed("```\n")
        assert DIM in result
        assert self.r.in_code_block is False

    def test_bullet_list_cyan(self):
        result = self.r.feed("- item one\n")
        assert CYAN in result

    def test_asterisk_bullet_list(self):
        result = self.r.feed("* item one\n")
        assert CYAN in result

    def test_numbered_list_cyan(self):
        result = self.r.feed("1. first\n")
        assert CYAN in result

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
        assert ITALIC in result
        assert "italic" in result
