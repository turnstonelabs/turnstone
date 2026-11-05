"""Line-buffered markdown to ANSI renderer for streaming output."""

import re

from turnstone.ui.colors import BOLD, CYAN, DIM, ITALIC, MAGENTA, RESET, show_controls

# A shown C1 control ends in its 7-bit form, and U+009F's is "_": an underscore
# right after U+241B belongs to it and never starts or ends a bold run.  A shown
# escape, or a U+241B the model typed, before "__" stops it the same way.
_BOLD_UNDERSCORES = re.compile("(?<!" + chr(0x241B) + ")__(.+?)(?<!" + chr(0x241B) + ")__")


def _shown_parts(line: str) -> tuple[str, str]:
    """*line*'s indentation and the rest, each with its control characters shown.

    The indentation is read from the line as written, where a form feed or
    another whitespace control still counts, so a fence or a list marker after
    one still reads as such.
    """
    text = line.lstrip()
    return show_controls(line[: len(line) - len(text)]), show_controls(text)


class MarkdownRenderer:
    """Line-buffered markdown → ANSI converter for streaming output.

    Buffers content until a newline arrives, then renders the complete line
    with regex-based markdown → ANSI conversion. Multi-line constructs
    (fenced code blocks) track state across lines.

    The text is a model's: each line's control characters are shown
    (``show_controls``) before the renderer sees it, so only the renderer's own
    codes act.  A carriage return just before a line feed is a line ending and
    is dropped; any other shows.
    """

    def __init__(self) -> None:
        self.in_code_block = False
        self._buf = ""

    def feed(self, text: str) -> str:
        """Feed text, return ANSI-rendered output for complete lines."""
        self._buf += text
        out = []
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            # A line splits off only once its line feed has arrived, so a CR LF
            # cut across two tokens still pairs up.
            if line.endswith("\r"):
                line = line[:-1]
            out.append(self._render_line(*_shown_parts(line)))
        return "\n".join(out) + "\n" if out else ""

    def flush(self) -> str:
        """Flush remaining buffer (end of stream)."""
        if self._buf:
            rendered = self._render_line(*_shown_parts(self._buf))
            self._buf = ""
            return rendered
        return ""

    def _render_line(self, indent: str, text: str) -> str:
        # Given the line already shown, split at its indentation, so no branch
        # can print the model's control characters raw.
        line = indent + text

        # Code block fence toggle
        if text.startswith("```"):
            self.in_code_block = not self.in_code_block
            return f"{DIM}{line}{RESET}"

        # Inside code block — cyan, no further markdown processing
        if self.in_code_block:
            return f"{CYAN}{line}{RESET}"

        # Headers (# H1, ## H2, ### H3, #### H4, ##### H5, ###### H6)
        m = re.match(r"^(#{1,6}) (.+)", line)
        if m:
            return f"{BOLD}{MAGENTA}{m.group(2)}{RESET}"

        # Inline formatting (order matters: bold before italic)
        text = re.sub(r"\*\*(.+?)\*\*", f"{BOLD}\\1{RESET}", text)
        text = _BOLD_UNDERSCORES.sub(f"{BOLD}\\1{RESET}", text)
        text = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", f"{ITALIC}\\1{RESET}", text)
        text = re.sub(r"`(.+?)`", f"{CYAN}\\1{RESET}", text)

        # Bullet lists — cyan bullet
        text = re.sub(r"^([-*]) ", f"{CYAN}\\1{RESET} ", text)

        # Numbered lists — cyan number
        text = re.sub(r"^(\d+)\. ", f"{CYAN}\\1.{RESET} ", text)

        return indent + text
