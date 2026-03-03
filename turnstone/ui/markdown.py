"""Line-buffered markdown to ANSI renderer for streaming output."""

import re

from turnstone.ui.colors import BOLD, CYAN, DIM, ITALIC, MAGENTA, RESET


class MarkdownRenderer:
    """Line-buffered markdown → ANSI converter for streaming output.

    Buffers content until a newline arrives, then renders the complete line
    with regex-based markdown → ANSI conversion. Multi-line constructs
    (fenced code blocks) track state across lines.
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
            out.append(self._render_line(line))
        return "\n".join(out) + "\n" if out else ""

    def flush(self) -> str:
        """Flush remaining buffer (end of stream)."""
        if self._buf:
            rendered = self._render_line(self._buf)
            self._buf = ""
            return rendered
        return ""

    def _render_line(self, line: str) -> str:
        # Code block fence toggle
        if line.strip().startswith("```"):
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
        line = re.sub(r"\*\*(.+?)\*\*", f"{BOLD}\\1{RESET}", line)
        line = re.sub(r"__(.+?)__", f"{BOLD}\\1{RESET}", line)
        line = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", f"{ITALIC}\\1{RESET}", line)
        line = re.sub(r"`(.+?)`", f"{CYAN}\\1{RESET}", line)

        # Bullet lists — cyan bullet
        line = re.sub(r"^(\s*)([-*]) ", f"\\1{CYAN}\\2{RESET} ", line)

        # Numbered lists — cyan number
        line = re.sub(r"^(\s*)(\d+)\. ", f"\\1{CYAN}\\2.{RESET} ", line)

        return line
