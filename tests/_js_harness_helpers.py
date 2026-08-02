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
