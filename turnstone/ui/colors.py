"""ANSI color constants and helper functions.

Respects the NO_COLOR convention (https://no-color.org/) and suppresses
color when stdout is not a terminal (piped output).  ``show_controls`` makes
the control characters in text the client didn't write visible, whether or not
color is on.
"""

import os
import re
import sys

_use_color = "NO_COLOR" not in os.environ and sys.stdout.isatty()

RESET = "\033[0m" if _use_color else ""
BOLD = "\033[1m" if _use_color else ""
DIM = "\033[2m" if _use_color else ""
ITALIC = "\033[3m" if _use_color else ""
RED = "\033[31m" if _use_color else ""
GREEN = "\033[32m" if _use_color else ""
YELLOW = "\033[33m" if _use_color else ""
BLUE = "\033[34m" if _use_color else ""
MAGENTA = "\033[35m" if _use_color else ""
CYAN = "\033[36m" if _use_color else ""
GRAY = "\033[90m" if _use_color else ""


def red(s: str) -> str:
    return f"{RED}{s}{RESET}"


def yellow(s: str) -> str:
    return f"{YELLOW}{s}{RESET}"


def dim(s: str) -> str:
    return f"{DIM}{s}{RESET}"


def bold(s: str) -> str:
    return f"{BOLD}{s}{RESET}"


def cyan(s: str) -> str:
    return f"{CYAN}{s}{RESET}"


def green(s: str) -> str:
    return f"{GREEN}{s}{RESET}"


# What a terminal acts on: C0 controls except tab and line feed, DEL, and the
# C1 controls U+0080-U+009F, which some terminals act on in UTF-8 too (U+009B
# starts a control sequence there, as ESC "[" does).  Lone surrogates as well,
# which a JSON escape can carry: under a C, POSIX or C.UTF-8 locale, or in
# Python's UTF-8 mode, stdout writes U+DC80-U+DCFF as raw bytes, so U+DCC2
# U+DC9B arrives as U+009B, and anywhere else, or for any other surrogate, the
# write fails.
_CONTROLS = re.compile("[" + r"\x00-\x08\x0b-\x1f\x7f-\x9f" + chr(0xD800) + "-" + chr(0xDFFF) + "]")


def _control_picture(match: re.Match[str]) -> str:
    code = ord(match.group())
    if code < 0x20:
        return chr(0x2400 + code)
    if code == 0x7F:
        return chr(0x2421)
    if code >= 0xD800:
        # Half of a character, never a whole one: the replacement character.
        return chr(0xFFFD)
    # A C1 control has no picture: the escape's, then the character that follows
    # an escape in its 7-bit form.
    return chr(0x241B) + chr(code - 0x40)


def show_controls(text: object) -> str:
    """*text*, as an f-string would print it, with its control characters shown as symbols.

    For model-written and outside text the terminal client prints.  A carriage
    return, a backspace or an escape sequence in it could rewrite what the
    operator reads, such as an approval card's command, or leave the terminal in
    a mode that hides what is printed next.  Each C0 control shows as its
    Control Pictures symbol (an escape as U+241B), as on the web tool cards, and
    DEL as U+2421; line feed and tab stay.  A C1 control shows as the escape's
    symbol and its 7-bit form, so U+009B shows as U+241B "[", and a lone
    surrogate as U+FFFD.  Apply it to the value before the client adds its own
    color, so only the client's codes act.
    """
    return _CONTROLS.sub(_control_picture, str(text))
