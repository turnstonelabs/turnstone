"""ANSI color constants and helper functions.

Respects the NO_COLOR convention (https://no-color.org/) and suppresses
color when stdout is not a terminal (piped output).
"""

import os
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


def red(s):
    return f"{RED}{s}{RESET}"


def yellow(s):
    return f"{YELLOW}{s}{RESET}"


def dim(s):
    return f"{DIM}{s}{RESET}"


def bold(s):
    return f"{BOLD}{s}{RESET}"


def cyan(s):
    return f"{CYAN}{s}{RESET}"


def green(s):
    return f"{GREEN}{s}{RESET}"
