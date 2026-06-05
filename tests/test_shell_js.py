"""Static smoke guards for the L-shell ES-module bundles.

``shell.js`` + ``pane.js`` are the first ES-module citizens in
``shared_static`` (the rest are classic IIFE scripts loaded via ``<script
src>``).  This mirrors ``test_app_js.py``'s posture — Python-side string /
parse assertions that catch the silent one-line regression — adapted for
module semantics: ``node --check`` only parses ``import`` / ``export`` when
the file carries an ``.mjs`` extension, so the parse guard copies to a temp
``.mjs`` first.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_SHARED = _ROOT / "turnstone/shared_static"
_SHELL_JS = _SHARED / "shell.js"
_PANE_JS = _SHARED / "pane.js"
_CONSOLE_INDEX = _ROOT / "turnstone/console/static/index.html"
_CONSOLE_APP = _ROOT / "turnstone/console/static/app.js"

_ESM_BUNDLES = [_SHELL_JS, _PANE_JS]

# The same unsafe DOM-write / dynamic-code sink set that ``test_app_js.py``
# pins for the classic bundles — kept local so the two test files stay
# independent.  Covers inner/outer-HTML assignment (plain + concat),
# insertAdjacentHTML, legacy doc-write, string-eval, dynamic Function, and
# string-first-arg timer scheduling.
_UNSAFE_CODE_SINK_RE = re.compile(
    r"\.(?:inner|outer)HTML\s*\+?=(?!=)"
    r"|\.insertAdjacentHTML\s*\("
    r"|document\.write\("
    r"|\beval\s*\("
    r"|\bnew\s+Function\s*\("
    r"|\bset(?:Timeout|Interval)\s*\(\s*['\"`]"
)


@pytest.mark.parametrize("bundle", _ESM_BUNDLES, ids=lambda p: p.name)
def test_esm_bundle_parses(bundle: Path) -> None:
    """``node --check`` each ESM bundle.  Copied to a temp ``.mjs`` so node
    parses it with module semantics (``import`` / ``export``).  Skipped if
    ``node`` is not on PATH so local dev without Node still passes."""
    if shutil.which("node") is None:
        pytest.skip("node binary not available on PATH")
    with tempfile.TemporaryDirectory() as td:
        mjs = Path(td) / (bundle.stem + ".mjs")
        mjs.write_text(bundle.read_text(encoding="utf-8"), encoding="utf-8")
        proc = subprocess.run(
            ["node", "--check", str(mjs)],
            capture_output=True,
            text=True,
            timeout=15,
        )
    assert proc.returncode == 0, f"node --check failed for {bundle.name}:\n{proc.stderr}"


@pytest.mark.parametrize("bundle", _ESM_BUNDLES, ids=lambda p: p.name)
def test_esm_bundle_no_unsafe_sink(bundle: Path) -> None:
    """The shell builds DOM with createElement / textContent / append — never
    an HTML-string sink.  Keep it that way (XSS-free by construction once the
    rail starts rendering cluster/workstream data in steps 2-3)."""
    body = bundle.read_text(encoding="utf-8")
    offenders = [body.count("\n", 0, m.start()) + 1 for m in _UNSAFE_CODE_SINK_RE.finditer(body)]
    assert not offenders, f"{bundle.name}: unsafe DOM/code sink at line(s) {offenders[:10]}"


@pytest.mark.parametrize("bundle", _ESM_BUNDLES, ids=lambda p: p.name)
def test_esm_bundle_has_no_var_decl(bundle: Path) -> None:
    """Match the modern-keyword posture of the swept classic bundles."""
    body = bundle.read_text(encoding="utf-8")
    stray = re.findall(r"^\s*var\s+\w", body, re.MULTILINE) + re.findall(
        r"\bfor\s*\(\s*var\s+", body
    )
    assert not stray, f"{bundle.name}: {len(stray)} stray `var` — use const/let."


def test_pane_exports_manager_and_base() -> None:
    """``pane.js`` must export both classes ``shell.js`` imports."""
    body = _PANE_JS.read_text(encoding="utf-8")
    assert "export class PaneManager" in body
    assert "export class ShellPane" in body


def test_shell_drives_legacy_boot_and_registers_dashboard() -> None:
    """The step-1 handoff has a few load-bearing wires: the shell imports the
    PaneManager, registers the default dashboard pane, and drives the classic
    app boot once the rail + status DOM exist.  It also relocates the cluster
    stream's status targets by id (re-point without rewiring connectSSE)."""
    body = _SHELL_JS.read_text(encoding="utf-8")
    assert 'from "./pane.js"' in body, "shell must import from pane.js"
    assert "window.TS_APP.boot()" in body, "shell must drive the legacy app boot"
    assert 'registerType("dashboard"' in body, "shell must register the dashboard pane"
    assert 'getElementById("status-bar")' in body, (
        "shell must relocate the connectSSE status target"
    )
    assert 'getElementById("cluster-status-bar")' in body, (
        "shell must handle the legacy cluster bar"
    )


def test_console_index_loads_shell_module_and_caps() -> None:
    """The console index must wire the ESM shell, its stylesheet, and the
    capability flags — losing any of them silently un-installs the L-shell."""
    body = _CONSOLE_INDEX.read_text(encoding="utf-8")
    assert '<script type="module" src="/shared/shell.js">' in body, (
        "console index must load the shell ES module"
    )
    assert "/shared/shell.css" in body, "console index must link shell.css"
    assert "TURNSTONE_SHELL_CAPS" in body, "console index must set the shell capability flags"


def test_console_app_exposes_boot_for_shell() -> None:
    """app.js must expose ``window.TS_APP.boot`` (driven by the shell) rather
    than auto-running init at parse, while keeping ``window.onLoginSuccess`` —
    the path that actually starts the Tier-1 stream on login / refresh."""
    body = _CONSOLE_APP.read_text(encoding="utf-8")
    assert "window.TS_APP.boot = function" in body, (
        "app.js must expose TS_APP.boot for the shell to drive"
    )
    assert "window.onLoginSuccess" in body, "the SSE-start hook must remain intact"
