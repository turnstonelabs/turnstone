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
_SHELL_CSS = _SHARED / "shell.css"
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


def test_console_launcher_creates_open_panes() -> None:
    """Workstream-lifecycle bugfix: BOTH launcher personas open the new session as
    an L-shell PANE (openPane), not a full-page nav — coordinator and interactive
    alike.  Full-page nav survives only as the shell-absent fallback."""
    app = _CONSOLE_APP.read_text(encoding="utf-8")
    assert 'pm.openPane("coordinator", res.data.ws_id)' in app, (
        "coordinator create must open a pane, not full-page nav"
    )
    assert 'pm.openPane("interactive", wsId, { nodeId: node || null })' in app, (
        "interactive create must open a node-proxied pane with the created node as hint"
    )
    # The saved-row interactive resume opens a pane (the pane resolves+opens the
    # node itself) — the bespoke restoreInteractiveSession helper is retired.
    assert 'pm.openPane("interactive", s.ws_id, { nodeId: s.node_id || null })' in app, (
        "saved interactive resume must open a pane with the origin node as hint"
    )
    assert "function restoreInteractiveSession" not in app, (
        "restoreInteractiveSession is folded into resolveInteractiveNode + the pane factory"
    )


def test_console_resolve_interactive_node_seam() -> None:
    """The console exposes resolveInteractiveNode(wsId, hint): origin-first
    POST /open (reuse a session already loaded on its node, no duplicate), with a
    rendezvous (/v1/api/route) fallback when the origin is gone.  This is what
    makes a node-proxied pane survive a reload — the shell's interactive factory
    calls it before streaming (the node /events 404s on a not-loaded ws)."""
    app = _CONSOLE_APP.read_text(encoding="utf-8")
    assert "window.TS_APP.resolveInteractiveNode = function (wsId, hintNodeId)" in app
    assert "/v1/api/workstreams/" in app and '"/open"' in app, (
        "origin-first must POST the node /open verb"
    )
    assert '"/v1/api/route?ws_id="' in app, "must rendezvous-fallback when the origin is gone"


def test_console_launcher_node_strategy() -> None:
    """Workstream-lifecycle bugfix: the interactive launcher gains a node-selection
    strategy (Least loaded | Specific node) with a live node picker, and the shared
    composer's task hint + node fields track the active persona."""
    app = _CONSOLE_APP.read_text(encoding="utf-8")
    assert 'id: "node_strategy"' in app and 'id: "node_id"' in app, (
        "launcher must expose the node-strategy + node-picker option fields"
    )
    assert "function _applyLauncherFields" in app, (
        "persona switch must update the hint + node-field visibility"
    )
    assert "function _populateLauncherNodes" in app, (
        "the specific-node picker must populate from the live cluster snapshot"
    )
    assert 'opts.node_strategy === "node"' in app, (
        "interactive create must pin to the chosen node only under the Specific strategy"
    )
    composer = (_SHARED / "composer.js").read_text(encoding="utf-8")
    assert "Composer.prototype.setPlaceholder" in composer, (
        "composer must support a per-persona placeholder swap"
    )
    assert "Composer.prototype.setOptionFieldVisible" in composer, (
        "composer must support conditionally revealing an option field"
    )


def test_pane_persists_meta_for_rehydrate() -> None:
    """Workstream-lifecycle bugfix: PaneManager persists a pane's serializable
    open-time meta (the interactive pane's resolved nodeId) and hands it back as
    `extra` on rehydrate, so a reload restores a node-proxied pane onto the SAME
    node (origin-first) instead of re-routing + duplicate-loading."""
    pane = _PANE_JS.read_text(encoding="utf-8")
    assert "setPaneMeta(paneId, meta)" in pane, "PaneManager must expose setPaneMeta"
    assert "entry.meta = p.meta" in pane, "_persist must include a pane's meta when present"
    assert "this.openPane(item.type, item.id, item.meta)" in pane, (
        "rehydrate must hand the persisted meta back to the factory as extra"
    )


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
    # 3b — the in-pane sidebar's mobile-drawer machinery + dead nav refs are deleted.
    for gone in (
        "_mobileSidebarOpen",
        "_injectMobileToggle",
        "_toggleMobileSidebar",
        ".admin-nav",
    ):
        assert gone not in body, f"retired admin sidebar/mobile symbol {gone!r} must be gone"
    index = _CONSOLE_INDEX.read_text(encoding="utf-8")
    assert 'id="admin-sidebar"' not in index, (
        "the in-pane admin sidebar markup must be deleted — the rail Manage groups navigate"
    )
    assert 'id="admin-content"' in index, "the admin content host must remain (the pane adopts it)"
    assert 'aria-labelledby="tab-' not in index, (
        "the adopted admin panels' dangling tab-* aria-labelledby (pointing at the deleted "
        "sidebar buttons) must be stripped"
    )


def test_step4_coordinator_pane_registered_and_wired() -> None:
    """Step 4b: the shell registers a ws_id-keyed coordinator pane (build chrome +
    controller on mount, connect on activate, destroy on close), installs the login
    fan-out registry, the rail opens coordinators as panes (not full-page nav), and
    the console loads the coordinator controller + its migrated chrome CSS."""
    shell = _SHELL_JS.read_text(encoding="utf-8")
    assert 'registerType("coordinator"' in shell, "shell must register the coordinator pane type"
    assert "createCoordinatorPane(this.bodyEl, id" in shell, (
        "onMount must build the controller into the pane body"
    )
    assert "this._ctl.connect()" in shell and "this._ctl.destroy()" in shell, (
        "per-pane Tier-2 connect on activate, destroy on close"
    )
    assert "pm.close(pane.id)" in shell, (
        "the coordinator's `end` must close the pane (not reload the whole console)"
    )
    assert "window.TS_LOGIN" in shell, "shell must install the login fan-out registry"
    rail = _RAIL_JS.read_text(encoding="utf-8")
    assert 'paneManager.openPane("coordinator", ws.id)' in rail, (
        "rail coordinator clicks must open a pane, not full-page nav"
    )
    assert "if (caps.orchestration)" in shell, (
        "step 6.0: the coordinator import is gated on the orchestration capability "
        "(a standalone turnstone-server has no /static/coordinator/*)"
    )
    assert "await import(" in shell and '"/static/coordinator/coordinator.js"' in shell, (
        "step 6.0: the shell imports the coordinator controller LAZILY (dynamic import) "
        "so a static import can't 404 the whole module on a standalone deployment"
    )
    index = _CONSOLE_INDEX.read_text(encoding="utf-8")
    assert "/static/coordinator/coordinator.js" not in index, (
        "coordinator.js is imported by shell.js now, not <script>-tagged in the console"
    )
    assert "coord-chrome.css" in index, "console must load the coordinator chrome CSS"


def test_step5_interactive_pane_registered_and_wired() -> None:
    """Step 5b: the shell registers a ws_id-keyed interactive pane over the
    NODE-PROXIED transport.  Both panes are ES modules the shell IMPORTS (step
    5e.0 lifted the coordinator off window too).  The pane is REHYDRATE-SAFE: on
    first activate it RESOLVES its owning node and (re)opens the session there
    before streaming (ensureInteractiveNode -> the console's resolveInteractiveNode
    origin-first POST /open; the node /events stream 404s on a ws not loaded on
    that node, so a reloaded pane can't connect blind), then PERSISTS the resolved
    node so a reload restores the pane onto the same node.  The rail opens it
    passing the owning node; the console loads the shared interactive stylesheet."""
    shell = _SHELL_JS.read_text(encoding="utf-8")
    assert 'import { createInteractivePane } from "./interactive.js"' in shell, (
        "interactive is ESM — the shell imports it (as it now does the coordinator)"
    )
    assert 'registerType("interactive"' in shell, "shell must register the interactive pane type"
    assert "createInteractivePane(this.bodyEl, id, {" in shell, (
        "first activate must build the controller into the pane body"
    )
    # Rehydrate-safety: the node is RESOLVED + the session (re)opened before the
    # pane streams, and the resolved node is persisted for the next reload.
    assert "ensureInteractiveNode(" in shell, (
        "the node-proxy target must be resolved (origin-first open) — rehydrate-safe"
    )
    assert "function ensureInteractiveNode(" in shell, "shell must define the node resolver/opener"
    assert "function nodeForWs(" in shell, "shell must keep the Tier-1 node fallback"
    assert "pm.setPaneMeta(pane.id" in shell, (
        "the resolved node must be persisted so a reload restores the same node"
    )
    # Focus-tracking lifecycle: connect/deactivate/destroy + login re-arm.
    for hook in ("this._ctl.connect()", "this._ctl.deactivate()", "this._ctl.destroy()"):
        assert hook in shell, f"interactive pane missing lifecycle {hook!r}"
    pane = _PANE_JS.read_text(encoding="utf-8")
    assert "this._types.get(type)(id, extra)" in pane, (
        "openPane must thread the open-time hint to the factory"
    )
    rail = _RAIL_JS.read_text(encoding="utf-8")
    assert 'openPane("interactive", ws.id, { nodeId: ws.node })' in rail, (
        "rail interactive clicks must open a node-proxied pane, not full-page nav"
    )
    index = _CONSOLE_INDEX.read_text(encoding="utf-8")
    assert "/shared/interactive.css" in index, "console must load the interactive stylesheet"


def test_step5d_rail_open_marker_tracks_active_pane() -> None:
    """Step 5d (designer fix): the rail's Workspaces `.open` marker tracks the
    active pane instead of being hardcoded to Dashboard, so the rail map and the
    tab bar agree on what is focused.  PaneManager exposes getActive/onActiveChange
    and fires on activate/close; the rail keys `.open` off it and re-renders."""
    pane = _PANE_JS.read_text(encoding="utf-8")
    assert "getActive()" in pane and "onActiveChange(cb)" in pane
    assert "_notifyActive()" in pane, "active-change must fan out to subscribers"
    rail = _RAIL_JS.read_text(encoding="utf-8")
    assert "paneManager.getActive()" in rail
    assert "onActiveChange(render)" in rail, "rail must re-render on pane activation"
    # The Dashboard row is no longer unconditionally open; a session row gets it.
    assert 'dash.className = "row open"' not in rail
    assert "active && active.rawId === ws.id" in rail


def test_step7_tab_dropdown_mechanism() -> None:
    """Step 7: PaneManager owns the GENERIC tab-action dropdown — a caret on any
    pane that exposes ``tabMenu()`` (the Dashboard home exposes none, so no
    caret), opening a keyboard-navigable ``.tab-menu``.  The caret is a <span>,
    NOT a nested <button> inside the tab <button> (invalid markup); the menu is
    reachable by keyboard via ContextMenu / Shift+F10, and a single menu is open
    at a time (Escape / outside-click close it)."""
    body = _PANE_JS.read_text(encoding="utf-8")
    assert "_openTabMenu(" in body and "_closeTabMenu(" in body, (
        "PaneManager must own the dropdown open/close mechanism"
    )
    assert 'typeof pane.tabMenu === "function"' in body, (
        "the caret is gated on the pane exposing a tabMenu() descriptor"
    )
    assert '"tab-caret"' in body, "the tab must get a caret affordance"
    for cls in ('"tab-menu"', '"tab-menu-item"', '"tab-menu-sep"'):
        assert cls in body, f"the dropdown must build the namespaced {cls} chrome"
    assert "ContextMenu" in body and "F10" in body, (
        "the menu must be keyboard-openable (ContextMenu / Shift+F10)"
    )
    assert 'e.key === "Escape"' in body, "Escape must close the open menu"


def test_step7_tab_menu_wired_per_persona() -> None:
    """Step 7: the shell wires each pane type's tab menu via convTabMenu —
    pane-type AND deployment derived.  The load-bearing recovery: the coordinator
    header's removed Export + end (5e.2e) return here as Export + Close workstream
    (its controller's closeSession).  The three-verb close (Close pane = pm.close
    != Close workstream != Delete) is the spine; the standalone interactive verbs
    are feature-detected globals, so the console degrades to a reduced menu."""
    shell = _SHELL_JS.read_text(encoding="utf-8")
    assert "function convTabMenu(" in shell, "the shared tab-menu builder must exist"
    assert shell.count("pane.tabMenu =") >= 3, (
        "coordinator, interactive, and admin panes must each expose a tabMenu"
    )
    # The three-verb close — the BRIEFING's load-bearing distinction.
    assert '"Close pane"' in shell and "pm.close(pane.id)" in shell
    assert '"Close workstream"' in shell, "Close workstream must be distinct from Close pane"
    assert '"Delete"' in shell
    # Coordinator recovery: Close workstream routes to the controller's server-close.
    assert "pane._ctl.closeSession()" in shell, (
        "the coordinator's Close workstream must call the controller's closeSession "
        "(the end button removed from its header in 5e.2e)"
    )
    assert "exportWorkstreamDownload" in shell, "Export conversation must wire the shared util"
    # Deployment-aware: the standalone interactive verbs are feature-detected globals.
    assert 'typeof window.closeWorkstream === "function"' in shell, (
        "the interactive Close workstream is a standalone-only global (feature-detected)"
    )
    assert "refreshWorkstreamTitle" in shell and "confirmDeleteWorkstream" in shell, (
        "the interactive title/delete verbs are feature-detected standalone globals"
    )


def test_step7_tab_menu_css_promoted_shared() -> None:
    """Step 7: the dropdown chrome is promoted to the SHARED shell sheet (so both
    deployments render it), recovered from the retired .ws-tab-dropdown design but
    translated onto the DS token vocabulary (--panel/--hair/--ink-*/--err, not the
    retired ui/static --bg-surface/--border/--fg-*)."""
    css = _SHELL_CSS.read_text(encoding="utf-8")
    for sel in (".tab-caret", ".tab-menu", ".tab-menu-item", ".tab-menu-sep", ".tab-menu-key"):
        assert sel in css, f"shell.css must carry the {sel} chrome"
    assert "@keyframes tab-menu-in" in css, "the dropdown entrance keyframe must be defined"
    assert "color-mix(in oklab, var(--err)" in css, (
        "the destructive item must use the DS --err token (the translation happened)"
    )


def test_step7_live_tab_state_glyphs() -> None:
    """Step 7 #2: conversational tabs show LIVE state glyphs driven by Tier-1 —
    the SAME source + builder the rail uses (one writer; the pane's Tier-2 stream
    drives its body, not the tab glyph), so tab and rail agree and the glyph never
    sits stale at an open-time placeholder.  The coord/int static placeholders are
    retired for a `stateful` flag."""
    rail = _RAIL_JS.read_text(encoding="utf-8")
    assert "export function glyph(" in rail, (
        "rail must export its state-glyph builder so the tab reuses it (single source)"
    )
    pane = _PANE_JS.read_text(encoding="utf-8")
    assert "setTabGlyph(paneId, el)" in pane and "statefulTabs()" in pane, (
        "PaneManager must expose the generic glyph setter + the stateful-tab list"
    )
    assert "this.stateful" in pane, "ShellPane must carry the stateful flag"
    shell = _SHELL_JS.read_text(encoding="utf-8")
    assert 'import { mountRail, mountManage, glyph } from "./rail.js"' in shell, (
        "the shell must import the rail's glyph builder (one source for tab + rail)"
    )
    assert "function stateForWs(" in shell and "function paintConvTabs(" in shell
    assert "window.TS_APP.onRender" in shell and "paintConvTabs(pm)" in shell, (
        "tab glyphs (and titles) must repaint on every Tier-1 render (live, not stale)"
    )
    assert shell.count("stateful: true") == 2, (
        "the coordinator + interactive panes are stateful (their static placeholders retired)"
    )
    css = _SHELL_CSS.read_text(encoding="utf-8")
    assert ".tab .tab-glyph" in css, "the tab-glyph spacing rule must apply to static + live glyphs"


def test_step7_new_tab_launcher_button() -> None:
    """Step 7 #3: the tab bar's right tail carries a [+] new-session button that
    focuses the persona launcher (the Dashboard pane hosts it; a new session needs
    a task prompt so it composes there).  Cross-deployment via showHome with an
    openPane fallback; reuses the scaffold's .tab-add styling."""
    shell = _SHELL_JS.read_text(encoding="utf-8")
    assert 'make("button", "tab-add")' in shell, "the [+] new-tab button must exist"
    assert "shell.tail.append(addTab)" in shell, "the [+] lives in the right-floated tail slot"
    assert "window.showHome()" in shell, "[+] must focus the persona launcher (showHome)"
    css = _SHELL_CSS.read_text(encoding="utf-8")
    assert ".tab-add" in css, "the .tab-add button style must exist (from the scaffold)"


def test_step7_auth_gated_open_pane() -> None:
    """Step 7 #4: openPane auth-gates CREATION via a per-type canOpen predicate
    (deny -> no pane; focusing an already-open pane is never re-gated).  The shell
    supplies the gate so PaneManager stays generic.  The coordinator type gates on
    the admin.coordinator scope (the SAME sessionStorage-backed helper the launcher
    uses — survives refresh, so rehydrate gates correctly), covering the rail /
    child-link / rehydrate open paths at once."""
    pane = _PANE_JS.read_text(encoding="utf-8")
    assert "this._gates" in pane, "PaneManager must hold a per-type auth-gate map"
    assert "setAuthGate(type, opts)" in pane, (
        "PaneManager must expose setAuthGate (kept separate from the 2-arg registerType)"
    )
    assert "gate.canOpen" in pane and "gate.onDeny" in pane, (
        "openPane must consult canOpen on create and call onDeny on deny"
    )
    # the legacy factory-call shape (open-time hint threading) is preserved
    assert "this._types.get(type)(id, extra)" in pane
    shell = _SHELL_JS.read_text(encoding="utf-8")
    assert "_hasCoordPermission" in shell, (
        "the coordinator pane must gate on the admin.coordinator scope helper"
    )
    assert "canOpen:" in shell and "onDeny:" in shell, (
        "the coordinator registerType must supply the auth gate"
    )
