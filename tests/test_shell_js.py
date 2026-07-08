"""Static smoke guards for the shared_static ES-module bundles.

The whole shared substrate is ES modules now (utils/toast/auth/composer/…
followed shell.js + pane.js; only theme.js — FOUC-critical — and the vendored
libs stay classic).  This mirrors ``test_app_js.py``'s posture — Python-side
string / parse assertions that catch the silent one-line regression — adapted
for module semantics: ``node --check`` only parses ``import`` / ``export``
when the file carries an ``.mjs`` extension, so the parse guard copies to a
temp ``.mjs`` first.
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
_UI_INDEX = _ROOT / "turnstone/ui/static/index.html"

_RAIL_JS = _SHARED / "rail.js"

# Every shared ES module — parse-guarded with module semantics.  (auth/kb/
# utils moved here from test_app_js's classic sweep when they were converted.)
_ESM_BUNDLES = [
    _SHELL_JS,
    _PANE_JS,
    _RAIL_JS,
    _SHARED / "utils.js",
    _SHARED / "toast.js",
    _SHARED / "kb.js",
    _SHARED / "cards.js",
    _SHARED / "auth.js",
    _SHARED / "renderer.js",
    _SHARED / "status_bar.js",
    _SHARED / "composer.js",
    _SHARED / "composer_attachments.js",
    _SHARED / "composer_queue.js",
    _SHARED / "interactive.js",
    _SHARED / "conversation.js",
    _SHARED / "preview.js",
    _SHARED / "redact_credentials.js",
]

# Sink scan: everything except renderer.js — the one sanctioned HTML-string
# producer (its output is consumed via setSafeHtml; see utils.js docstring).
_ESM_SINK_BUNDLES = [b for b in _ESM_BUNDLES if b.name != "renderer.js"]

# Style ratchet: var-free modules only.  The lifted legacy bundles (cards/
# renderer/status_bar/composer*) keep their pre-module `var` style — converting
# them was a loader change, not a rewrite; don't grow NEW var use elsewhere.
_ESM_NO_VAR_BUNDLES = [
    _SHELL_JS,
    _PANE_JS,
    _RAIL_JS,
    _SHARED / "utils.js",
    _SHARED / "toast.js",
    _SHARED / "kb.js",
    _SHARED / "auth.js",
    _SHARED / "interactive.js",
    _SHARED / "conversation.js",
    _SHARED / "preview.js",
    _SHARED / "redact_credentials.js",
]

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


@pytest.mark.parametrize("bundle", _ESM_SINK_BUNDLES, ids=lambda p: p.name)
def test_esm_bundle_no_unsafe_sink(bundle: Path) -> None:
    """The shell builds DOM with createElement / textContent / append — never
    an HTML-string sink.  Keep it that way (XSS-free by construction once the
    rail starts rendering cluster/workstream data in steps 2-3)."""
    body = bundle.read_text(encoding="utf-8")
    offenders = [body.count("\n", 0, m.start()) + 1 for m in _UNSAFE_CODE_SINK_RE.finditer(body)]
    assert not offenders, f"{bundle.name}: unsafe DOM/code sink at line(s) {offenders[:10]}"


@pytest.mark.parametrize("bundle", _ESM_NO_VAR_BUNDLES, ids=lambda p: p.name)
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


def test_persona_picker_surfaces_wired() -> None:
    """The persona creation/authoring surfaces are wired the same way every
    other feature is — losing an id or the shared data-layer script tag
    silently drops the picker without a JS error.

    The standalone server UI carries BOTH creation pickers (the quick-create
    ``dashboard-persona`` select and the full ``new-ws-persona`` dialog select)
    plus the shared ``personas.js`` data layer; the console carries the same
    data layer plus the admin authoring ``persona-shelf`` dialog, mounted by
    admin.js.  (The console launcher's own picker is a composer OPTION field,
    not a static id — see test_console_launcher_routes_by_kind.)
    """
    ui_index = _UI_INDEX.read_text(encoding="utf-8")
    assert 'id="new-ws-persona"' in ui_index, "the new-ws dialog must carry the persona select"
    assert 'id="dashboard-persona"' in ui_index, "the quick-create persona select must exist"
    assert '<script type="module" src="/shared/personas.js">' in ui_index, (
        "the standalone UI must load the shared personas data layer"
    )
    console_index = _CONSOLE_INDEX.read_text(encoding="utf-8")
    assert '<script type="module" src="/shared/personas.js">' in console_index, (
        "the console must load the shared personas data layer"
    )
    assert 'id="persona-shelf"' in console_index, "the admin persona authoring shelf must exist"
    admin = _CONSOLE_ADMIN.read_text(encoding="utf-8")
    assert "function loadAdminPersonas(" in admin, "admin.js must mount the persona list loader"
    assert "function submitPersonaShelf(" in admin, "admin.js must wire the persona-shelf submit"
    assert 'tab: "personas"' in admin, "the Personas admin tab must be in the IA (perm-gated)"


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


def test_rail_conveys_state_and_kind() -> None:
    """rail.js conveys state by shape+colour via the shared ui-base .ui-glyph-*
    vocabulary (not a private glyph class), nests children via the shared bucket
    helper, and tags sessions by KIND (COORD/INT)."""
    body = _RAIL_JS.read_text(encoding="utf-8")
    assert "ui-glyph-" in body, "rail must use ui-base .ui-glyph-* for state (shape+colour)"
    assert "bucketByParent" in body, "rail must nest children via the shared bucket helper"
    assert "COORD" in body and "INT" in body, "rail must tag sessions by kind"


def test_console_launcher_routes_by_kind() -> None:
    """Step 2b: the dashboard launcher carries a workstream kind, scope-gates
    the interactive option, branches submit + create by kind (coordinator =
    console-local, interactive = node-proxy), routes saved activation to the
    node for interactive rows, and the active-coordinators home table is gone
    (the rail covers it).  Pins the console-JS convention for the new logic."""
    app = _CONSOLE_APP.read_text(encoding="utf-8")
    assert "function _setLauncherKind" in app and "_launcherKind" in app
    assert "function _hasInteractivePermission" in app, (
        "launcher must scope-gate the interactive kind"
    )
    assert 'kind === "interactive"' in app, "submitHomeCoord must branch by kind"
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
    # "personas" now means the capability-bundle feature; the kind toggle
    # ids were reclaimed to kind-* (launcher-kinds / kind-coordinator / ...).
    assert 'id="launcher-kinds"' in index, "the kind toggle must be in the launcher panel"
    assert 'id="launcher-personas"' not in index, (
        "the old persona-squatting toggle id must stay gone"
    )


def test_console_launcher_creates_open_panes() -> None:
    """Workstream-lifecycle bugfix: BOTH launcher kinds open the new session as
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
    composer's task hint + node fields track the active kind."""
    app = _CONSOLE_APP.read_text(encoding="utf-8")
    assert 'id: "node_strategy"' in app and 'id: "node_id"' in app, (
        "launcher must expose the node-strategy + node-picker option fields"
    )
    assert "function _applyLauncherFields" in app, (
        "kind switch must update the hint + node-field visibility"
    )
    assert "function _populateLauncherNodes" in app, (
        "the specific-node picker must populate from the live cluster snapshot"
    )
    assert 'opts.node_strategy === "node"' in app, (
        "interactive create must pin to the chosen node only under the Specific strategy"
    )
    composer = (_SHARED / "composer.js").read_text(encoding="utf-8")
    assert "Composer.prototype.setPlaceholder" in composer, (
        "composer must support a per-kind placeholder swap"
    )
    assert "Composer.prototype.setOptionFieldVisible" in composer, (
        "composer must support conditionally revealing an option field"
    )
    # Review bug-1 regression: picking a node fires the composer `change` event,
    # which re-runs _populateLauncherNodes -> setOptionChoices (a rebuild that
    # resets the <select>).  The selection MUST be captured + restored across the
    # rebuild, else a Specific-node session can never be launched.
    assert 'const previous = _homeCoordComposer.getOptionValue("node_id")' in app, (
        "_populateLauncherNodes must snapshot the current node pick before rebuild"
    )
    assert 'if (previous) _homeCoordComposer.setOptionValue("node_id", previous)' in app, (
        "_populateLauncherNodes must restore the node pick after rebuild (bug-1)"
    )


def test_console_launcher_interactive_create_carries_attachments() -> None:
    """Create-time attachments for interactive sessions: the launcher gate is gone
    and ``_createInteractive`` frames a multipart body (``meta`` JSON + ``file``
    parts, via the shared ``_createWorkstreamFetchOpts`` helper) when files are
    staged, so the cluster proxy can forward the blobs to the node.  Was
    previously blocked with "Attachments aren't supported for interactive
    sessions yet"."""
    app = _CONSOLE_APP.read_text(encoding="utf-8")
    assert "Attachments aren't supported for interactive sessions yet." not in app, (
        "the create-time interactive attachment gate must be removed"
    )
    # The shared create-fetch helper frames multipart (meta JSON + file parts).
    helper = app[app.index("function _createWorkstreamFetchOpts(") :]
    helper = helper[: helper.index("\nfunction ")]
    assert "new FormData()" in helper
    assert 'form.append("meta"' in helper and 'form.append("file"' in helper, (
        "the create-fetch helper must send meta JSON + file parts"
    )
    # _createInteractive routes through that helper, so staged files are sent.
    rest = app[app.index("function _createInteractive(") + 1 :]
    cut = rest.find("\nfunction ")
    interactive = rest if cut == -1 else rest[:cut]
    assert "_createWorkstreamFetchOpts(body, files)" in interactive, (
        "_createInteractive must build its create body via the shared helper"
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


def test_rail_manage_row_badge_hook() -> None:
    """rail.js owns a GENERIC Manage-row count badge — `setRowBadge(tabKey, count,
    label?)` stamps a glyph+count chip on a tab row (DS warn `.rail-badge`), and so
    a COLLAPSED group never hides the signal, mirrors the group's running total onto
    its head.  rail.js stays agnostic about what the count means (no consent
    specifics here); a subsystem drives it.  `mountManage` registers the row/head
    refs and re-applies live counts across a (re)mount.  This is the re-homing
    target for the MCP consent badge after its settings-gear host was deleted."""
    body = _RAIL_JS.read_text(encoding="utf-8")
    assert "export function setRowBadge(tabKey, count, label)" in body, (
        "rail must export the generic setRowBadge hook (mechanism, not meaning)"
    )
    # The chip pairs colour with a glyph (never colour alone — chip-contrast rule).
    assert "rail-badge" in body and '"⚠"' in body, (
        "the badge must carry a ⚠ glyph alongside the count (not colour alone)"
    )
    # The collapsed-group head must carry the group total so a hidden row's signal
    # still surfaces — pin the head propagation + the per-group sum.
    assert "function _groupCount(" in body, "the head badge must sum the group's tab counts"
    assert "_groupEls" in body and "_rowEls" in body, (
        "mountManage must register row + owning-group-head refs for the badge hook"
    )
    assert "_reapplyBadges()" in body, (
        "a (re)mount must re-apply any live badge state (the refs are rebuilt)"
    )
    # rail.js stays agnostic — the hook takes a generic tabKey/count, with no
    # consent-specific endpoint, fetch, or branch (an explanatory comment naming a
    # sample caller is fine; logic is not).  `setRowBadge` itself never fetches.
    badge_fn = body[body.index("export function setRowBadge") :]
    badge_fn = badge_fn[: badge_fn.index("\n}\n") + 3]
    assert "fetch" not in badge_fn and "/v1/" not in badge_fn, (
        "the generic badge hook must not reach into a subsystem (no fetch / endpoint)"
    )
    # No colour-only treatment: the chip uses DS warn tokens AND a glyph.
    css = _SHELL_CSS.read_text(encoding="utf-8")
    assert ".rail-badge" in css, "shell.css must carry the .rail-badge chip rule"
    badge_block = css[css.index(".rail-badge") :]
    badge_block = badge_block[: badge_block.index("\n.grp") if "\n.grp" in badge_block else 800]
    assert "var(--warn" in badge_block, (
        "the badge chip must use the DS --warn family (theme-flips by construction)"
    )
    assert "#" not in badge_block, "the badge chip must be token-only (no hex) so themes flip"


def test_shell_bridges_setrowbadge_for_classic_subsystems() -> None:
    """shell.js is the ESM module bridge: a classic-script subsystem (the
    standalone consent badge in ui/static/app.js) can't import rail.js, so the
    shell re-exports `setRowBadge` on the `window.TS_SHELL` seam.  Pin the import
    and the seam so the bridge can't be silently dropped (which would re-break the
    badge the same way the gear deletion did)."""
    body = _SHELL_JS.read_text(encoding="utf-8")
    assert 'setRowBadge } from "./rail.js"' in body, "shell must import setRowBadge from rail.js"
    ts_shell = body[body.index("window.TS_SHELL = {") :][:200]
    assert "setRowBadge" in ts_shell, (
        "TS_SHELL must expose setRowBadge for classic subsystems (the consent-badge bridge)"
    )


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
    assert "createInteractivePane(pane.bodyEl, id, {" in shell, (
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
    # Hot-path optimisation (review perf-1): a LIVE session (Tier-1 already names
    # its node) connects directly — only the dormant/reload case pays the
    # resolve+open round-trip.
    assert "const liveNode = caps.cluster ? nodeForWs(id)" in shell, (
        "a live ws (Tier-1 names its node) must connect directly, skipping POST /open"
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


def test_step7_tab_menu_wired_per_kind() -> None:
    """Step 7: the shell wires each pane type's tab menu via convTabMenu —
    pane-type AND deployment derived.  The load-bearing recovery: the coordinator
    header's removed Export + end (5e.2e) return here as Export + Close workstream
    (its controller's closeSession).  The three-verb close (Close pane = pm.close
    != Close workstream != Delete) is the spine; the standalone interactive verbs
    prefer the feature-detected globals (which also manage its local roster), and
    a deployment without them (the console) falls back to the base-aware lane
    (see test_tab_menu_base_aware_verb_lane)."""
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
        "the interactive Close workstream prefers the standalone global (feature-detected)"
    )
    assert "refreshWorkstreamTitle" in shell and "confirmDeleteWorkstream" in shell, (
        "the interactive title/delete verbs prefer the standalone globals"
    )


def test_coordinator_tab_menu_enables_title_verbs() -> None:
    """Coordinators carry LLM/auto titles like interactive workstreams, so
    their tab dropdown must surface Refresh/Edit title — convTabMenu's
    ``titleVerbs`` block, POSTed to the console-origin coord
    ``refresh-title`` / ``title`` routes via the base-aware lane (default
    base ""). Scoped to the coordinator registerType block so it can't
    pass on the interactive pane's long-standing ``titleVerbs``."""
    shell = _SHELL_JS.read_text(encoding="utf-8")
    start = shell.index('registerType("coordinator"')
    tail = shell[start:]
    nxt = tail.find("registerType(", 1)  # bound at the next pane registration
    coord_block = tail[:nxt] if nxt != -1 else tail
    assert "pane._ctl.closeSession()" in coord_block, (
        "sanity: the extracted block is the coordinator pane"
    )
    assert "convTabMenu(" in coord_block, "the coordinator pane must wire a tab menu"
    assert "titleVerbs: true" in coord_block, (
        "the coordinator tab menu must enable titleVerbs (Refresh/Edit title)"
    )


def test_tab_menu_base_aware_verb_lane() -> None:
    """Lifecycle round 2: a proxied interactive pane's tab menu must act on the
    pane's OWN transport base, not the console origin — the globals lane only
    exists on the standalone.  convTabMenu therefore takes a `base` getter and
    falls back to POSTing the verb at {base}/v1/api/workstreams/{ws}/{verb}; a
    node-verb is OMITTED while no base is resolvable (never aimed at the wrong
    origin), and Export forwards the base to the shared util (a proxied export
    must come from the node that owns the conversation)."""
    shell = _SHELL_JS.read_text(encoding="utf-8")
    assert "function postWsVerb(" in shell, "the base-aware verb POST helper must exist"
    assert '"/v1/api/workstreams/" + encodeURIComponent(wsId) + "/" + verb' in shell
    # The interactive pane supplies its current base: a LIVE controller's
    # (exact), else the persisted node hint, else the live Tier-1 node, else null
    # (a DEAD controller's stale base is special-cased — see the test below).
    assert "const menuBase = ()" in shell, "the interactive pane must expose a base getter"
    assert "pane._ctl && pane._ctl.base != null" in shell, (
        "a built controller's base is authoritative for the menu verbs"
    )
    # Fallback verbs exist for the console: refresh-title / title / close / delete.
    for verb in ('"refresh-title"', '"title"', '"close"', '"delete"'):
        assert (
            f"postWsVerb(base, wsId, {verb}" in shell
            or f"postWsVerb(closeBase, id, {verb}" in shell
        ), f"the {verb} verb must have a base-aware fallback"
    # Export rides the base too (3-arg form), and node-verbs are null-gated.
    assert "exportWorkstreamDownload(wsId, null, base)" in shell
    assert "base != null" in shell, "node-verbs must be omitted while the base is unresolved"
    # Destructive fallbacks confirm first (window.confirm is the house precedent).
    assert shell.count("window.confirm(") >= 2, (
        "the close + delete fallbacks must confirm before acting"
    )
    # No leading separator when the verb section is empty.
    assert "if (items.length) items.push({ separator: true })" in shell
    util = (_SHARED / "utils.js").read_text(encoding="utf-8")
    assert "function exportWorkstreamDownload(wsId, btn, base)" in util, (
        "the shared export util must accept the transport base"
    )
    assert '(base || "") +' in util, "the export URL must be base-prefixed"


def test_tab_menu_dead_controller_prefers_live_node() -> None:
    """Lifecycle round 2 follow-up: a DEAD controller's base is stale — its node
    may have lost or RE-HOMED the ws — so the tab-menu base getter must not keep
    aiming verbs at it.  Otherwise the close/delete 404-as-success lanes would
    silently drop a tab whose session is alive on the node it re-homed to.  When
    dead, ``menuBase`` mirrors the revive path (the live Tier-1 node leads); the
    stale controller base is only the gone-cluster-wide fallback.
    """
    shell = _SHELL_JS.read_text(encoding="utf-8")
    body = shell[shell.index("const menuBase = ()") :]
    body = body[: body.index("};") + 2]
    # The dead-controller guard must come FIRST — before the live-base return —
    # so a stale base can never authorise the 404-as-success drop.
    assert "pane._ctl.isDead && pane._ctl.isDead()" in body, (
        "menuBase must special-case a dead controller"
    )
    assert body.index("isDead()") < body.index("pane._ctl.base != null"), (
        "the dead-controller guard must precede the authoritative live-base return"
    )
    # When dead the live Tier-1 node leads (mirrors beginConnect's hint chain),
    # falling back to the controller's own (stale) base only when no live node.
    assert 'live ? "/node/" + encodeURIComponent(live) : pane._ctl.base' in body, (
        "a dead pane aims at the live node, falling back to the stale base only "
        "when the ws is gone cluster-wide"
    )


def test_attachment_lane_is_base_aware() -> None:
    """Console regression: an interactive pane is node-proxied, so its attachment
    upload / list / delete / preview requests must ride the pane's transport base
    ("/node/{id}").  Without it they hit the console's OWN coord route and 404 as
    "coordinator not found" (the standalone server, base="", was unaffected —
    which masked the bug).  Mirrors the base-aware verb lane: the controller
    resolves a base from ``opts.getBase`` and prefixes every attachment URL;
    ``buildAttachmentPreview`` takes the base for its thumbnail / content src; the
    interactive pane wires both."""
    attach = (_SHARED / "composer_attachments.js").read_text(encoding="utf-8")
    assert "function _attachUrl(base, wsId, id, suffix)" in attach, (
        "the per-attachment URL builder must be base-first"
    )
    assert "function _base()" in attach and "opts.getBase" in attach, (
        "the controller must resolve a node base from opts.getBase"
    )
    assert "base: _base()" in attach, "committed-chip previews must carry the base"
    assert "base = opts.base" in attach, "buildAttachmentPreview must consume opts.base"
    # upload + remove + rehydrate must each base-prefix their collection/row URL.
    assert attach.count("_base() +") >= 3, (
        "upload, remove, and rehydrate must each base-prefix their URL"
    )
    pane = (_SHARED / "interactive.js").read_text(encoding="utf-8")
    assert "getBase: () =>" in pane, (
        "the interactive pane must pass its node base into the attachment controller"
    )
    assert "base: attachBase" in pane, "history-pill previews must ride the pane's base too"


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
    assert 'import { mountRail, mountManage, glyph, setRowBadge } from "./rail.js"' in shell, (
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


def test_split_view_controls() -> None:
    """The revived split-view's affordance surface: Split right / Split down /
    Unsplit buttons in the tab-bar tail (they REPLACED the redundant [+] — the
    permanent Dashboard tab is the launcher).  Deliberately NO contextmenu
    override (the pre-L-shell split UI hijacked right-click); denials surface
    as a toast with the manager's reason."""
    shell = _SHELL_JS.read_text(encoding="utf-8")
    assert 'tbBtn("tb-split", "◫", "Split right")' in shell
    assert 'tbBtn("tb-split tb-split--down", "◫", "Split down")' in shell
    assert 'pm.splitFocused("right")' in shell and 'pm.splitFocused("down")' in shell
    assert "pm.unsplit()" in shell, "the Unsplit button must call pm.unsplit"
    assert "unsplitBtn.hidden = !pm.isSplit()" in shell, (
        "Unsplit only shows while split (synced via onActiveChange)"
    )
    assert "shell.tail.append(splitRightBtn, splitDownBtn, unsplitBtn)" in shell
    # The [+] is gone with its showHome/focusLauncher plumbing kept out.
    assert "tab-add" not in shell, "the [+] new-tab button was replaced by the split controls"
    css = _SHELL_CSS.read_text(encoding="utf-8")
    assert ".tb-split" in css and ".tb-split--down .tb-glyph" in css, (
        "split buttons styled; the down variant rotates the GLYPH (not the button)"
    )
    assert "tab-add" not in css, "the dead .tab-add style must not survive"


def test_pane_manager_split_engine() -> None:
    """The split-view engine in PaneManager: an optional binary layout tree
    (null = the pre-feature single-pane behaviour, bit-for-bit).  Visible panes
    are positioned by inline % insets — NEVER reparented, so live stream DOM,
    scroll state and media survive every layout change.  Tabs stay global:
    active = the focused cell, a backgrounded tab swaps into it, a click inside
    a visible pane focuses its cell.  Separators resize by drag AND keyboard
    (role=separator + aria-value*); the tree persists in the working-set blob
    and rehydrate prunes leaves whose pane did not restore."""
    pane = _PANE_JS.read_text(encoding="utf-8")
    # public surface (splitFocused takes an optional explicit fill — openPaneBeside)
    assert "splitFocused(dir, fillId)" in pane
    assert "unsplit()" in pane and "isSplit()" in pane
    # no reparenting: layout is applied as % insets on the pane elements
    assert 'p.el.style.left = r.x * 100 + "%"' in pane
    # the focused-cell swap + pure focus move both live in activate()
    assert "this._leafFor(paneId)" in pane and "target.paneId = paneId" in pane
    # close() collapses the cell and prefers the absorbing sibling as fallback
    assert "_collapseLeaf(leaf)" in pane and "preferFallback" in pane
    # auto-fill source: most-recently-focused backgrounded pane
    assert "_nextBackgroundPane()" in pane and "this._mru" in pane
    # separators: ARIA + keyboard + pointer-capture drag, ratio bounds from the
    # split node's OWN px region (nested splits clamp against their own space)
    assert 'setAttribute("role", "separator")' in pane
    assert "setPointerCapture" in pane and "_ratioBounds(node)" in pane
    assert '"aria-valuenow"' in pane and "ArrowRight" in pane
    # the ARIA range mirrors the REAL clamp (_ratioBounds per handle in the
    # _applyLayout loop) — never a hard-coded constant
    assert "this._ratioBounds(h.node)" in pane
    assert '"aria-valuemin"' in pane and '"aria-valuemax"' in pane
    assert 'setAttribute("aria-valuemin", "10")' not in pane
    # limits: cell minimums + cap (the old ui/static ceiling, kept)
    assert "SPLIT_MAX_CELLS = 6" in pane
    assert "SPLIT_MIN_W = 200" in pane and "SPLIT_MIN_H = 150" in pane
    # persistence: layout rides the working-set blob; restore prunes dead leaves
    assert "state.layout = this._serializeLayout(this._layout)" in pane
    assert "_restoreLayout(data)" in pane and "seen.has(d.paneId)" in pane
    # the visible-but-unfocused tab marker
    assert 'classList.toggle("shown"' in pane
    # per-pane ✕: split mode hides ONE cell keeping the tab (closeCell), EXCEPT
    # an ephemeral pane which closes outright; single-pane it closes the pane
    # (withheld from non-closable) — the click decides at click time, the label
    # tracks the mode.  Manager-injected into the pane SECTION (content
    # untouched), removed via _clearCellStyle.  Ephemeral-dismiss behaviour has
    # its own deep coverage in test_preview_js.py::TestEphemeralDismiss.
    assert "closeCell(paneId)" in pane and "_refreshCellChips()" in pane
    assert 'b.className = "cell-unsplit"' in pane
    assert '"Close pane"' in pane, "the single-pane chip mode"
    # mode-DISTINCT glyphs (designer P1: identical signifier + locus with a
    # reversible/destructive divergence is a mode-error trap) — a click that
    # cannot be a reversible cell-hide shows ✕, else −.
    assert "const destroys = !multi || pane.ephemeral;" in pane
    assert 'b.textContent = destroys ? "✕" : "−"' in pane
    assert '"cell-unsplit--close"' in pane
    assert "this._removeCellChip(pane)" in pane
    # open-beside: the coordinator child-link placement (split right of the
    # focused cell, degrade to the plain swap on deny)
    assert "openPaneBeside(type, id, extra)" in pane
    assert 'this.splitFocused("right", paneId)' in pane
    css = _SHELL_CSS.read_text(encoding="utf-8")
    assert ".panes--split > section.pane" in css, (
        "split cells must target section.pane ONLY — the interactive pane's inner "
        "div also carries .pane (the step-5b lesson)"
    )
    assert ".split-handle" in css and "col-resize" in css and "row-resize" in css
    assert ".tab.shown:not(.active)" in css, "the visible-but-unfocused tab marker"
    # the focused ring rides an ::after OVERLAY — an inset shadow on the
    # section itself paints UNDER edge-touching children (the status-bar
    # occlusion bug); the ::before bar sits above the ring line
    assert ".panes--split > section.pane.split-focused::after" in css
    assert ".cell-unsplit" in css, "the per-cell hide-from-split chip style"
    assert ".cell-unsplit--close:hover" in css, "destructive mode telegraphs on hover"
    # the pane is the chip's containing block in BOTH modes — unpositioned,
    # the single-pane chip anchored to the VIEWPORT (offsetParent <body>)
    assert ".panes > section.pane" in css
    # pane-hosted coordinator sidebar drops below the chip's corner lane —
    # the chip sat exactly on the Children refresh button (user report)
    coord_chrome = (_ROOT / "turnstone/console/static/coordinator/coord-chrome.css").read_text(
        encoding="utf-8"
    )
    assert ".pane-body.coord-chrome-root #coord-sidebar.sidebar" in coord_chrome


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


# ---------------------------------------------------------------------------
# Workstream-lifecycle round 2: dead-session revive + explicit-reopen seam.
# ---------------------------------------------------------------------------


def test_pane_manager_reopen_seam() -> None:
    """openPane() on an ALREADY-OPEN pane fires `pane.onReopen(extra)` — the
    explicit-intent signal (saved-list resume, rail row, child link) that
    activate() cannot carry: hooks no-op on the already-active pane, and
    onActivate also fires on plain tab switches.  Fired AFTER activate so the
    pane is visible when it reacts.  getPane lets the shell reach a pane for
    cross-cutting lifecycle signals."""
    pane = _PANE_JS.read_text(encoding="utf-8")
    assert "onReopen(extra) {}" in pane, "ShellPane must document the onReopen hook"
    assert "const existed = !!pane" in pane, "openPane must remember create-vs-focus"
    assert "pane.onReopen(extra)" in pane, "openPane must fire onReopen on existing panes"
    # Ordering: the reopen signal comes after activation.
    assert pane.index("this.activate(paneId)") < pane.index("pane.onReopen(extra)")
    assert "getPane(type, id)" in pane, "PaneManager must expose getPane for the shell"


def test_interactive_pane_dead_session_revive() -> None:
    """The reported round-2 bug: an interactive session whose stream died
    (closed / evicted / node restarted) could never reconnect while its tab
    existed — openPane focused the dead pane, onActivate's connect() is one-shot,
    and the controller's recovery loop re-dialed the SAME node forever.  The fix:
    the shell paints a click-to-reconnect banner when the controller reports
    dead, and an explicit reopen (onReopen) revives — tear down the dead
    controller, re-resolve the node (POST /open), rebuild."""
    shell = _SHELL_JS.read_text(encoding="utf-8")
    assert "const showDeadBanner = ()" in shell, "the dead banner painter must exist"
    assert "pane-dead-banner" in shell, "the banner carries its own style hook"
    css = _SHELL_CSS.read_text(encoding="utf-8")
    assert ".pane-dead-banner" in css and ".pane-dead-banner:focus-visible" in css, (
        "the banner is a real <button>; shell.css must reset its native chrome "
        "and give it a keyboard focus treatment"
    )
    assert "Session disconnected" in shell, "the banner states the terminal condition"
    assert "const revive = (freshNodeId)" in shell, "the revive path must exist"
    assert "onDead: showDeadBanner" in shell, (
        "the controller's terminal give-up must surface the banner"
    )
    # Revive is full teardown + re-resolve: unsubscribe login, destroy, rebuild.
    ridx = shell.index("const revive =")
    rbody = shell[ridx : ridx + 900]
    assert "TS_LOGIN.unsubscribe" in rbody and "destroy()" in rbody
    assert "beginConnect(true)" in rbody, "revive must force the resolve path"
    # onActivate shows the banner for a dead controller instead of connect();
    # onReopen revives (the resume-with-a-pre-existing-tab path).
    assert "this._ctl.isDead && this._ctl.isDead()" in shell
    assert "revive(reExtra && reExtra.nodeId)" in shell, (
        "onReopen must revive with the caller's fresh node hint"
    )
    # The standalone revive path must (re)open the local session — /events 404s
    # on an unloaded ws; only the forceResolve lane POSTs /open.
    assert "function ensureInteractiveNode(caps, wsId, hint, openFirst)" in shell
    eidx = shell.index("function ensureInteractiveNode(")
    ebody = shell[eidx : eidx + 700]
    assert '"/open"' in ebody and '{ method: "POST" }' in ebody.replace("\n", " ").replace(
        "  ", " "
    ).replace("  ", " "), "standalone openFirst must POST /open"
    # Revive must skip BOTH fast paths (a stale Tier-1 row must not bypass the
    # /open), while the live node — when present — stays the resolve HINT so an
    # origin-first /open reuses a genuinely-live session instead of loading a
    # duplicate copy on the old meta node.
    assert "if (!forceResolve && (liveNode || !caps.cluster))" in shell, (
        "beginConnect's fast paths must both yield to the forced resolve"
    )
    assert "liveNode || (pane.meta && pane.meta.nodeId)" in shell, (
        "the live Tier-1 node must lead the resolve-hint chain"
    )


def test_shell_closes_pane_on_ws_closed() -> None:
    """Tier-1 ws_closed → the open pane CLOSES outright (tab gone; a split
    cell collapses onto its sibling) — the coordinator-closes-its-child flow,
    matching the standalone's pane-auto-close.  The dead-BANNER lane survives
    for streams that die WITHOUT a ws_closed (node crash / network), where the
    session may still be revivable."""
    shell = _SHELL_JS.read_text(encoding="utf-8")
    assert "const notifySessionClosed = (wsId)" in shell
    assert 'pm.getPane("interactive", wsId)' in shell
    assert "if (p) pm.close(p.id)" in shell, "ws_closed closes the pane, not mark-dead"
    assert "showDeadBanner" in shell, "the banner lane must survive for non-closed deaths"
    ts_shell = shell[shell.index("window.TS_SHELL = {") :][:200]
    assert "panes: pm" in ts_shell and "notifySessionClosed" in ts_shell, (
        "the seam must be exported on TS_SHELL for the console's Tier-1 handler"
    )
    app = _CONSOLE_APP.read_text(encoding="utf-8")
    closed = app.index('=== "ws_closed"')
    block = app[closed : closed + 1200]
    assert "notifySessionClosed" in block, (
        "the console ws_closed handler must notify the shell's open pane"
    )


def test_coordinator_pane_reconnects_on_reopen() -> None:
    """The coordinator variant of resume-with-a-pre-existing-tab: the saved-list
    resume POSTs /open BEFORE openPane, so the dead pane just needs a fresh
    stream — onReopen calls the controller's reconnect(), which resets backoff
    and reconnects only when the source is gone or CLOSED (OPEN is healthy;
    CONNECTING means native retry / a fresh connect is already in flight)."""
    shell = _SHELL_JS.read_text(encoding="utf-8")
    assert "this._ctl.reconnect()" in shell, (
        "the coordinator pane's onReopen must drive the controller's reconnect"
    )
    coord = (_ROOT / "turnstone/console/static/coordinator/coordinator.js").read_text(
        encoding="utf-8"
    )
    assert "function reconnect()" in coord
    assert "readyState !== EventSource.CLOSED) return" in coord.replace("\n", " "), (
        "reconnect must only act on a missing/CLOSED stream"
    )
    assert "reconnect: reconnect," in coord, "the factory must return reconnect"


# ---------------------------------------------------------------------------
# Polish round: rail collapse + mobile drawer + the shared popup-menu helper.
# ---------------------------------------------------------------------------


def test_rail_collapse_glyph_strip() -> None:
    """Desktop rail collapse: a persisted preference (localStorage
    ``turnstone_interface.rail``) shrinks the rail to a 52px glyph-only strip —
    live state glyphs stay the navigation, the cluster pills keep glyph+count
    (the label rides a hideable span), Manage gets a single gear stand-in that
    opens the Admin pane, and the toggle mirrors its state through
    aria-expanded/aria-controls."""
    shell = _SHELL_JS.read_text(encoding="utf-8")
    assert 'const RAIL_COLLAPSE_KEY = "turnstone_interface.rail"' in shell, (
        "the collapse preference must persist under the turnstone_interface key"
    )
    assert 'make("button", "rail-collapse")' in shell, "the collapse toggle must exist"
    assert 'collapseBtn.setAttribute("aria-controls", "shell-rail")' in shell, (
        "the toggle must reference the rail it controls"
    )
    assert 'classList.toggle("rail-collapsed", collapsed)' in shell, (
        "collapse must be a class flip on .app (CSS owns the layout change)"
    )
    rail = _RAIL_JS.read_text(encoding="utf-8")
    assert '"cpill-label"' in rail, (
        "cluster pill labels must ride a span so the collapsed strip can hide "
        "them while keeping glyph + count"
    )
    assert "manage-glyph" in rail, (
        "Manage needs its collapsed-strip gear stand-in (rail.js mountManage)"
    )
    css = _SHELL_CSS.read_text(encoding="utf-8")
    assert "grid-template-columns: 52px 1fr" in css, "the collapsed rail is 52px"
    assert ".app.rail-collapsed .manage-glyph" in css, (
        "the gear stand-in must flip on while collapsed"
    )
    assert "@media (min-width: 769px)" in css, (
        "the collapse block must be desktop-scoped (the drawer owns mobile)"
    )


def test_mobile_drawer_off_canvas() -> None:
    """Mobile drawer: below the breakpoint the rail overlays off-canvas at full
    width.  Burger in the tab bar opens it (focus moves into the rail);
    Escape / scrim tap / any pane activation close it; the closed drawer is
    visibility:hidden so its buttons leave the Tab order."""
    shell = _SHELL_JS.read_text(encoding="utf-8")
    assert 'make("button", "rail-burger"' in shell, "the drawer toggle must exist"
    assert 'make("div", "rail-scrim")' in shell, "the backdrop scrim must exist"
    assert 'classList.toggle("rail-open", open)' in shell, (
        "drawer state must be a class flip on .app"
    )
    assert "pm.onActiveChange(() => setDrawer(false))" in shell, (
        "opening/focusing a pane must close the drawer that did it"
    )
    css = _SHELL_CSS.read_text(encoding="utf-8")
    assert "@media (max-width: 768px)" in css, "the drawer is mobile-scoped"
    assert "translateX(-100%)" in css, "the closed drawer parks off-canvas"
    assert "visibility: hidden" in css, (
        "the closed drawer must leave the Tab order / a11y tree, not merely translate off-screen"
    )


def test_popup_menu_shared_helper() -> None:
    """One popup-menu chrome: pane.js exports openPopupMenu (items,
    positioning with flip+clamp, dismissal, aria-expanded mirroring, arrow
    roving) and BOTH consumers ride it — the tab-action dropdown and the
    shell's footer user menu (which pops up from the viewport-bottom chip)."""
    pane = _PANE_JS.read_text(encoding="utf-8")
    assert "export function openPopupMenu(" in pane, "the shared helper must exist"
    assert pane.count("openPopupMenu(") >= 2, (
        "the tab-action dropdown must route through the helper"
    )
    shell = _SHELL_JS.read_text(encoding="utf-8")
    assert "openPopupMenu(" in shell, "the user menu must ride the shared helper"
    assert 'prefer: "up"' in shell, (
        "the footer chip sits at the viewport bottom — the menu pops upward"
    )
