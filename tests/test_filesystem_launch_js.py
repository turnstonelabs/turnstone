"""The Files launcher must preserve node locality and keyboard approval intent."""

import json
from pathlib import Path

from tests._js_harness_helpers import FAKE_DOM, extract_braced, node_skip, run_node_source

_ROOT = Path(__file__).resolve().parent.parent
_SHARED = _ROOT / "turnstone/shared_static"


@node_skip
def test_content_links_keep_keyboard_activation_during_pending_approval() -> None:
    source = (_SHARED / "interactive.js").read_text()
    signature = 'this.el.addEventListener("keydown", (e) => {'
    keys = extract_braced(source, signature)[len(signature) : -1]
    proc = run_node_source(
        FAKE_DOM
        + "const {buildFileChip, buildPreviewChip} = await import("
        + json.dumps((_SHARED / "conversation.js").as_uri())
        + ");\n"
        + "const handleKey = function(e) {"
        + keys
        + """};
const opened = [];
const path = '/workspace/report.html';
const descriptor = {source: path, title: 'Report', kind: 'html'};
const file = buildFileChip(path, value => opened.push(value));
const preview = buildPreviewChip(descriptor, value => opened.push(value));
let approvals = 0;
const pane = {
  pendingApproval: true,
  approvalBlockEl: document.createElement('div'),
  _oldestCycleId: () => 'pending',
  getFeedback: () => '',
  resolveApproval: () => { approvals++; },
};
for (const [chip, expected] of [[file, path], [preview, descriptor]]) {
  for (const target of [chip, chip.querySelector('span')]) {
    for (const key of ['Enter', ' ', 'y', 'n', 'a', 'Escape']) {
      let prevented = false;
      chip.focus();
      handleKey.call(pane, {target, key, preventDefault() { prevented = true; }});
      if (prevented || approvals) throw new Error('Content links must not resolve pending approval');
      if (key === 'Enter' || key === ' ') {
        const before = opened.length;
        // Browser button activation follows only an uncancelled key event.
        chip.click();
        if (opened.length !== before + 1 || opened.at(-1) !== expected)
          throw new Error('Native activation must open the intended content once');
      }
    }
  }
}
// Keep the advertised approval shortcut available elsewhere in the pane.
let prevented = false;
const target = document.createElement('div');
target.focus();
handleKey.call(pane, {target, key: 'Enter', preventDefault() { prevented = true; }});
if (!prevented || approvals !== 1) throw new Error('Pane approval shortcut must still work');
"""
    )
    assert proc.returncode == 0, (
        f"Content link keyboard routing failed:\n{proc.stderr}\n{proc.stdout}"
    )


@node_skip
def test_status_bar_files_button_opens_without_approving() -> None:
    source = (_SHARED / "interactive.js").read_text()
    status_dom = source.split("    // Per-workstream status bar (above input)", 1)[1].split(
        "    // Input area", 1
    )[0]
    signature = 'this.el.addEventListener("keydown", (e) => {'
    keys = extract_braced(source, signature)[len(signature) : -1]
    proc = run_node_source(
        FAKE_DOM
        + """
let opened = 0;
const hasScope = () => true;
const hasPermission = () => true;
const pane = {
  el: document.createElement("div"),
  _host: {onFiles() { opened++; }},
  pendingApproval: true,
  approvalBlockEl: {},
  _oldestCycleId() { throw new Error("Files must not trigger approval shortcuts"); },
};
"""
        + "(function() {"
        + status_dom
        + "}).call(pane);\n"
        + "(function(e) {"
        + keys
        + """}).call(pane, {target: pane._sbFiles, key: "Enter"});
pane._sbFiles.click();
if (opened !== 1) throw new Error("Files click must open exactly once");
if (pane._sbFiles.type !== "button") throw new Error("Files must not submit the composer");
if (!pane.statusBarEl.contains(pane._sbFiles)) throw new Error("Files belongs in the status bar");
if (pane._sbFiles.querySelector("span").getAttribute("aria-hidden") !== "true")
  throw new Error("The decorative open symbol must not alter the accessible name");
"""
    )
    assert proc.returncode == 0, f"Files launcher failed:\n{proc.stderr}\n{proc.stdout}"


def test_files_launcher_is_shared_and_passes_originating_transport() -> None:
    source = (_SHARED / "interactive.js").read_text()
    factory = source.split("function createInteractivePane(", 1)[1]
    assert "window.TS_SHELL.openFiles({ base: base, wsId: wsId }, path);" in factory
    shell = (_SHARED / "shell.js").read_text()
    assert 'pm.registerType("filesystem", (id, extra)' in shell
    assert 'pm.openPaneBeside("filesystem")' in shell
    assert "pane.showFiles(ctx, path)" in shell
    for path in ("turnstone/ui/static/index.html", "turnstone/console/static/index.html"):
        assert 'href="/shared/filesystem.css"' in (_ROOT / path).read_text()


@node_skip
def test_launcher_and_pane_require_file_permission_and_write_scope() -> None:
    source = (_SHARED / "interactive.js").read_text()
    status_dom = source.split("    // Per-workstream status bar (above input)", 1)[1].split(
        "    // Input area", 1
    )[0]
    shell = (_SHARED / "shell.js").read_text()
    gate = shell[shell.index('  pm.setAuthGate("filesystem", {') :]
    gate = gate.split("  const openFiles =", 1)[0]
    proc = run_node_source(
        FAKE_DOM
        + """
let canWrite = false;
let canReadFiles = false;
const hasScope = (scope) => scope === "write" && canWrite;
const hasPermission = (permission) => permission === "files.read" && canReadFiles;
let fileGate;
const pm = {setAuthGate(type, gate) { fileGate = gate; }};
"""
        + gate
        + """
for (const [write, readFiles] of [[false, false], [false, true], [true, false], [true, true]]) {
  canWrite = write;
  canReadFiles = readFiles;
  const pane = {el: document.createElement("div"), _host: {onFiles() {}}};
  (function() {
"""
        + status_dom
        + """
  }).call(pane);
  const allowed = write && readFiles;
  if (pane._sbFiles.hidden !== !allowed) throw new Error("Initial launcher must require both grants");
  if (fileGate.canOpen() !== allowed) throw new Error("Opening or restoring Files must require both grants");
}
"""
    )
    assert proc.returncode == 0, f"Files permission gate failed:\n{proc.stderr}\n{proc.stdout}"


@node_skip
def test_authority_changes_clear_files_and_update_all_launchers() -> None:
    source = (_SHARED / "shell.js").read_text()
    start = source.index("  const syncFilesAccess = () => {")
    end = source.index("  // Tier-1 lifecycle", start)
    proc = run_node_source(
        FAKE_DOM
        + """
const buttons = [document.createElement("button"), document.createElement("button")];
document.querySelectorAll = () => buttons;
let canWrite = true;
const hasScope = () => canWrite;
let canReadFiles = true;
const hasPermission = (permission) => permission === "files.read" && canReadFiles;
let authChanged;
const onAuthChange = (cb) => { authChanged = cb; };
const whenPermissionsReady = (cb) => cb();
let filePaneOpen = true;
let closes = 0;
const pm = {
  hasPane: () => filePaneOpen,
  close(id) {
    if (id !== "filesystem") throw new Error("Must not close conversations");
    closes++;
    filePaneOpen = false;
  },
};
"""
        + source[start:end]
        + """
if (buttons.some(b => b.hidden)) throw new Error("Authorized users need the launcher");
canReadFiles = false;
authChanged();
if (filePaneOpen || closes !== 1) throw new Error("Permission loss must clear rendered files");
if (buttons.some(b => !b.hidden)) throw new Error("Write scope alone must not show launchers");
canReadFiles = true;
authChanged();
if (buttons.some(b => b.hidden)) throw new Error("Permission grant must restore launchers");
if (filePaneOpen || closes !== 1) throw new Error("Permission grant must not reopen files");
filePaneOpen = true;
canWrite = false;
authChanged();
if (filePaneOpen || closes !== 2) throw new Error("Scope loss must clear rendered files");
if (buttons.some(b => !b.hidden)) throw new Error("File permission alone must not show launchers");
"""
    )
    assert proc.returncode == 0, f"Files authority change failed:\n{proc.stderr}\n{proc.stdout}"
