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
_CONSOLE_ADMIN = _ROOT / "turnstone/console/static/admin.js"

_RAIL_JS = _SHARED / "rail.js"
_ESM_BUNDLES = [_SHELL_JS, _PANE_JS, _RAIL_JS]

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
    assert 'from "./rail.js"' in body, "shell must import the rail module"
    assert "mountRail(" in body, "shell must mount the live rail (Cluster + Workspaces)"


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


def test_rail_seam_exposed_and_bottom_bar_retired() -> None:
    """Step 2: app.js exposes the Tier-1 rail seam (getClusterState + onRender,
    fired from renderFromState), and the legacy bottom #cluster-status-bar + its
    renderers are GONE (the rail replaces it — single writer per region)."""
    app = _CONSOLE_APP.read_text(encoding="utf-8")
    assert "window.TS_APP.getClusterState" in app, "app.js must expose getClusterState for the rail"
    assert "window.TS_APP.onRender" in app, "app.js must expose the onRender subscribe hook"
    assert "_fireRenderSubs()" in app, "renderFromState must fire rail subscribers"
    for gone in ("renderStatusBar", "renderNodePicker", "cluster-status-bar", "csb-"):
        assert gone not in app, f"retired bottom-bar symbol {gone!r} must be gone from app.js"
    index = _CONSOLE_INDEX.read_text(encoding="utf-8")
    assert "cluster-status-bar" not in index, "the #cluster-status-bar markup must be deleted"


def test_rail_conveys_state_and_persona() -> None:
    """rail.js conveys state by shape+colour via the shared ui-base .ui-glyph-*
    vocabulary (not a private glyph class), nests children via the shared bucket
    helper, and tags sessions by persona (COORD/INT)."""
    body = _RAIL_JS.read_text(encoding="utf-8")
    assert "ui-glyph-" in body, "rail must use ui-base .ui-glyph-* for state (shape+colour)"
    assert "bucketByParent" in body, "rail must nest children via the shared bucket helper"
    assert "COORD" in body and "INT" in body, "rail must tag sessions by persona"


def test_console_launcher_routes_by_persona() -> None:
    """Step 2b: the dashboard launcher carries a persona kind, scope-gates the
    interactive option, branches submit + create by kind (coordinator =
    console-local, interactive = node-proxy), routes saved activation to the
    node for interactive rows, and the active-coordinators home table is gone
    (the rail covers it).  Pins the console-JS convention for the new logic."""
    app = _CONSOLE_APP.read_text(encoding="utf-8")
    assert "function _setLauncherKind" in app and "_launcherKind" in app
    assert "function _hasInteractivePermission" in app, (
        "launcher must scope-gate the interactive persona"
    )
    assert 'kind === "interactive"' in app, "submitHomeCoord must branch by persona kind"
    assert "function _createInteractive" in app, "the interactive create path must exist"
    assert '"/v1/api/cluster/workstreams/new"' in app, (
        "interactive create must use the node-proxy endpoint"
    )
    assert 's.kind !== "coordinator"' in app, (
        "saved activation must route interactive sessions to their node"
    )
    for gone in ("function _renderHomeView", "_activeCoordsFromClusterState"):
        assert gone not in app, f"removed home-view symbol {gone!r} must stay gone from app.js"
    index = _CONSOLE_INDEX.read_text(encoding="utf-8")
    assert 'id="active-coordinators"' not in index, (
        "the active-coordinators table must be removed (the rail covers it)"
    )
    assert 'id="launcher-personas"' in index, "the persona toggle must be in the launcher panel"


def test_step3_admin_pane_registered_and_manage_mounted() -> None:
    """Step 3: the shell registers the singleton Admin pane (which adopts
    #view-admin) and mounts the rail's Manage groups from the admin IA seam."""
    body = _SHELL_JS.read_text(encoding="utf-8")
    assert 'registerType("admin"' in body, "shell must register the admin pane type"
    assert 'getElementById("view-admin")' in body, "the admin pane must adopt #view-admin"
    assert "mountManage(" in body, "shell must mount the rail Manage groups"
    assert "mountManage" in body and 'from "./rail.js"' in body, (
        "shell must import mountManage from rail.js"
    )


def test_step3_rail_manage_builds_from_admin_seam() -> None:
    """rail.js builds the Manage groups from the TS_ADMIN seam — perm-filtered,
    collapsible .grp vocabulary, programmatic DOM — and routes a row click
    through the seam's openTab rather than reaching into admin DOM."""
    body = _RAIL_JS.read_text(encoding="utf-8")
    assert "export function mountManage" in body, "rail must export mountManage"
    assert "window.TS_ADMIN" in body, "Manage must read the admin IA seam"
    assert "isTabAllowed" in body, "Manage must permission-filter its tabs"
    assert "openTab" in body, "a Manage row click must route through the seam's openTab"
    assert '"grp"' in body and '"grp-items"' in body, "Manage reuses the .grp vocabulary"
    # collapse + active state ride a class + aria, never colour alone
    assert "aria-expanded" in body, "collapsible group heads must expose aria-expanded"


def test_step3_admin_seam_and_thin_show_admin() -> None:
    """admin.js exposes the TS_ADMIN seam (IA + shared perm gate + active-tab +
    openTab) and showAdmin is now a thin delegator that opens the singleton
    Admin pane — the legacy in-#main view toggle + history push are gone."""
    body = _CONSOLE_ADMIN.read_text(encoding="utf-8")
    assert "const ADMIN_IA = [" in body, "admin must define the IA data"
    assert "window.TS_ADMIN.ia = ADMIN_IA" in body, "admin must expose the IA seam"
    assert "window.TS_ADMIN.isTabAllowed" in body and "window.TS_ADMIN.openTab" in body
    assert "function adminTabAllowed(tab)" in body, "the shared permission gate must exist"
    assert 'pm.openPane("admin")' in body, "showAdmin must open the singleton Admin pane"
    for gone in ('currentView = "admin"', 'history.pushState({ view: "admin" }'):
        assert gone not in body, f"legacy admin view-model bit {gone!r} must be gone"
