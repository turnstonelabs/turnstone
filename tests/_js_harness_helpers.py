"""Shared helpers for the Python-driven node harnesses that evaluate the
``shared_static`` ES modules with script semantics."""

from __future__ import annotations

import re
import shutil
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path


def has_node() -> bool:
    return shutil.which("node") is not None


# Module-level ``pytestmark = node_skip`` in each harness suite — the node
# detection lives here once, so a future change (version floor, env
# override) cannot land in one suite and silently miss another.
node_skip = pytest.mark.skipif(not has_node(), reason="node not available")


def demodulize(path: Path) -> str:
    """Strip ES-module syntax so ``vm.runInThisContext`` (script semantics)
    can evaluate the file: imports drop (the harness loads the whole
    dependency set into one shared context, so cross-file bindings resolve
    as context globals, exactly like the pre-module classic scripts), and
    ``export`` keywords peel off their declarations.

    Single-sourced here for every JS harness: a new module syntax form
    (``export default``, re-exports) must be handled once, not per suite —
    a divergence between per-file copies surfaces as a confusing
    ``vm.runInThisContext`` SyntaxError in whichever suite lagged.
    """
    src = path.read_text(encoding="utf-8")
    src = re.sub(r"^import\s+\{[\s\S]*?\}\s+from\s+\"[^\"]+\";\s*$", "", src, flags=re.M)
    src = re.sub(r"^import\s+[^;\n]+;\s*$", "", src, flags=re.M)
    src = re.sub(
        r"^export\s+(?=(?:async\s+)?(?:function|const|let|var|class)\b)", "", src, flags=re.M
    )
    src = re.sub(r"^export\s*\{[^}]*\};\s*$", "", src, flags=re.M)
    return src


def strip_js_comments(source: str) -> str:
    """Strip ``//`` and ``/* */`` comments for source-pattern assertions —
    the single implementation every JS harness suite shares.

    STRING-AWARE and OFFSET-PRESERVING (comments become spaces, byte
    length identical): a ``//`` inside a string literal (``"https://…"``)
    is content, not a comment — a string-blind scanner truncates the rest
    of the line, and pattern pins then silently assert against corrupted
    text (a ``not in`` guard passes vacuously after the pattern it
    polices was reintroduced).  Length preservation keeps downstream
    offset math (brace walkers, ``.index`` comparisons) valid.  This is
    the strict superset of every per-suite predecessor, hoisted so the
    suites cannot diverge again.

    Limitation — regex literals (``/pattern/flags``) are not detected: a
    ``//`` inside one would be misread as a line comment.  Safe for
    every region currently scanned; extend the tracker before scanning a
    region with regex literals.
    """
    out: list[str] = []
    n = len(source)
    i = 0
    in_str: str | None = None
    while i < n:
        ch = source[i]
        if in_str:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(source[i + 1])
                i += 2
                continue
            if ch == in_str:
                in_str = None
            i += 1
            continue
        # Line comment: replace with spaces up to newline (preserve
        # length so downstream offset math still works).
        if ch == "/" and i + 1 < n and source[i + 1] == "/":
            j = source.find("\n", i)
            if j == -1:
                j = n
            out.append(" " * (j - i))
            i = j
            continue
        # Block comment: replace with spaces up to closing */.
        if ch == "/" and i + 1 < n and source[i + 1] == "*":
            j = source.find("*/", i + 2)
            if j == -1:
                out.append(" " * (n - i))
                i = n
                continue
            out.append(" " * (j + 2 - i))
            i = j + 2
            continue
        if ch in ('"', "'", "`"):
            in_str = ch
        out.append(ch)
        i += 1
    return "".join(out)
