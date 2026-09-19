"""Real-browser drive of the model editor's Anthropic Workspace ID field.

Builds the livepass console harness (the REAL ``admin.js`` + markup over
stubbed fetches), serves it over HTTP (ES modules refuse ``file://``), and
reads the state ``admin.js`` produced out of headless Chrome via the page
title the harness stamps.  Three behaviours are pinned: editing a scoped
definition repopulates the field from ``server_compat``, switching the
provider off the Anthropic protocol hides the row and shows the warning that
the save will drop the value, and Save serializes the field back into
``capabilities.server_compat`` on the PUT body.  Skips when no Chrome binary
is available.
"""

from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _ROOT / "scripts/livepass.py"
_SCOPE = "wrkspc_01LIVEPASS"  # the harness fixture's stored workspace id


def _chrome() -> str | None:
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return found
    fallback = Path("/usr/bin/google-chrome")
    return str(fallback) if fallback.exists() else None


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_args: Any) -> None:
        return


@pytest.fixture(scope="module")
def harness(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """Build the harness once and serve it on a loopback port."""
    chrome = _chrome()
    if chrome is None:
        pytest.skip("no Chrome binary available")
    out = tmp_path_factory.mktemp("livepass")
    spec = importlib.util.spec_from_file_location("livepass_workspace_script", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.build(out)

    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(_QuietHandler, directory=str(out)))
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, name="livepass-http", daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/console/livepass.html"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)


def _title(harness: str, query: str, tmp_path: Path) -> str:
    chrome = _chrome()
    assert chrome is not None
    profile = tmp_path / "chrome-profile"
    result = subprocess.run(
        [
            chrome,
            "--headless",
            "--disable-gpu",
            "--no-sandbox",
            "--no-first-run",
            f"--user-data-dir={profile}",
            # Chrome initializes its encrypted stores through the desktop
            # keyring on startup; with the keyring locked (a reboot before
            # anyone logs in) every navigation blocks until the timeout kills
            # it.  A headless test run has no use for the keyring.
            "--password-store=basic",
            "--hide-scrollbars",
            "--force-prefers-reduced-motion",
            "--window-size=1440,900",
            "--virtual-time-budget=9000",
            "--dump-dom",
            f"{harness}?{query}",
        ],
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    match = re.search(r"<title>(.*?)</title>", result.stdout, re.S)
    assert match is not None, "harness page rendered no <title>"
    return match.group(1).strip()


def test_edit_repopulates_the_field_from_server_compat(harness: str, tmp_path: Path) -> None:
    title = _title(harness, "open=model-edit", tmp_path)
    assert title == f"EDIT-OK-ws-{_SCOPE}-row-shown-note-hidden"


def test_row_hides_and_warns_when_the_provider_leaves_the_anthropic_protocol(
    harness: str, tmp_path: Path
) -> None:
    """The stored scope stays in the (hidden) field, and the warning that the
    save will drop it is shown — a silent deletion reported as success was
    the design review's first finding."""
    title = _title(harness, "open=model-edit&provider=openai", tmp_path)
    assert title == f"EDIT-OK-ws-{_SCOPE}-row-hidden-note-shown"


def test_save_serializes_the_field_into_server_compat(harness: str, tmp_path: Path) -> None:
    assert _title(harness, "open=model-save", tmp_path) == f"PUT-OK-1-ws-{_SCOPE}"
