"""Regression guards for the native paste-over-HTTP livepass."""

from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _ROOT / "scripts/livepass.py"


def _load_livepass() -> Any:
    spec = importlib.util.spec_from_file_location("livepass_paste_script", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_paste_livepass_builds_real_clipboard_event_path(tmp_path: Path) -> None:
    livepass = _load_livepass()
    livepass.build(tmp_path)

    page_path = tmp_path / "paste/livepass.html"
    page = page_path.read_text(encoding="utf-8")
    assert (tmp_path / "paste/shared").resolve() == _ROOT / "turnstone/shared_static"
    assert 'import { Composer } from "./shared/composer.js";' in page
    assert "PASTE_ATTACHMENT_CHARS" in page
    assert "new Composer(" in page
    assert "event.isTrusted" in page
    assert "event.clipboardData" in page
    assert "window.isSecureContext" in page
    assert 'attachment.name === "pasted-text.txt"' in page
    assert 'attachment.type === "text/plain"' in page
    assert 'document.title = "PASTE-HTTP-READY"' in page
    assert 'document.title = "PASTE-HTTP-FAILED-" + reason' in page

    # These shortcuts would make the harness green without exercising the
    # browser's native clipboard event and therefore invalidate its purpose.
    assert "navigator.clipboard" not in page
    assert "new ClipboardEvent" not in page
    assert ".dispatchEvent(" not in page
    assert "__pasteProbe" not in page


def test_generated_paste_module_is_valid_javascript(tmp_path: Path) -> None:
    if shutil.which("node") is None:
        pytest.skip("node binary not available on PATH")
    livepass = _load_livepass()
    livepass.build(tmp_path)
    page = (tmp_path / "paste/livepass.html").read_text(encoding="utf-8")
    match = re.search(r'<script type="module">\s*(.*?)\s*</script>', page, re.S)
    assert match is not None
    module = tmp_path / "paste_livepass.mjs"
    module.write_text(match.group(1), encoding="utf-8")

    result = subprocess.run(
        ["node", "--check", str(module)],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
