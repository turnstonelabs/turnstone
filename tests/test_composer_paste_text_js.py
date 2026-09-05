"""Behavior and wiring tests for large-paste attachment conversion."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_HELPER = _ROOT / "turnstone/shared_static/composer_paste_text.js"
_COMPOSER = _ROOT / "turnstone/shared_static/composer.js"
_ATTACHMENTS = _ROOT / "turnstone/shared_static/composer_attachments.js"
_INTERACTIVE = _ROOT / "turnstone/shared_static/interactive.js"
_UI_APP = _ROOT / "turnstone/ui/static/app.js"
_CONSOLE_APP = _ROOT / "turnstone/console/static/app.js"
_COORDINATOR = _ROOT / "turnstone/console/static/coordinator/coordinator.js"


def test_paste_text_helper_behavior(tmp_path: Path) -> None:
    """Execute the real ESM and pin its fixed threshold, byte cap, and dedup."""
    if shutil.which("node") is None:
        pytest.skip("node binary not available on PATH")

    module = tmp_path / "composer_paste_text.mjs"
    module.write_text(_HELPER.read_text(encoding="utf-8"), encoding="utf-8")
    script = tmp_path / "paste_harness.mjs"
    module_url = json.dumps(module.as_uri())
    script.write_text(
        f"const moduleUrl = {module_url};\n"
        + r"""
import { Blob, File } from "node:buffer";
globalThis.Blob = Blob;
globalThis.File = File;
globalThis.window = globalThis;

function check(condition, message) {
  if (!condition) throw new Error(message);
}

const paste = await import(moduleUrl);
check(paste.PASTE_ATTACHMENT_CHARS === 2000, "fixed threshold drifted");
check(
  paste.pasteTextToFile("x".repeat(1999)) === null,
  "below-threshold text converted",
);
const exactText = "x".repeat(2000);
const exact = paste.pasteTextToFile(exactText);
check(exact === null, "exact-threshold text converted");
const convertedText = "x".repeat(2001);
const converted = paste.pasteTextToFile(convertedText);
check(converted instanceof File, "above-threshold text did not convert");
check(converted.name === "pasted-text.txt", "filename drifted");
check(converted.type === "text/plain", "MIME drifted");
check(converted.size === 2001, "byte size drifted");
check(paste.isPastedTextFile(converted) === true, "synthesized file not recognised");
check(
  paste.isPastedTextFile(new File(["x"], "real.txt", { type: "text/plain" })) === false,
  "a real file mistaken for a paste",
);
check(paste.isPastedTextFile(null) === false, "null mistaken for a paste");
check(
  (await converted.text()) === convertedText,
  "file content did not round-trip",
);
check(
  paste.pasteTextToFile("") === null,
  "empty text converted",
);
check(
  paste.pasteTextToFile("😀".repeat(2000)) === null,
  "UTF-16 code units were counted as characters",
);
check(
  paste.pasteTextToFile("😀".repeat(2001)) instanceof File,
  "Unicode code points above the threshold did not convert",
);
const cjkAtCap = "界".repeat(Math.floor(paste.TEXT_ATTACHMENT_MAX_BYTES / 3));
check(
  paste.pasteTextToFile(cjkAtCap) instanceof File,
  "text within the byte ceiling did not convert",
);
check(
  paste.pasteTextToFile(cjkAtCap + "界") === null,
  "multibyte text escaped the byte ceiling",
);

const sameText = "same".repeat(501);
const sameA = paste.pasteTextToFile(sameText);
const sameB = paste.pasteTextToFile(sameText);
const different = paste.pasteTextToFile("size".repeat(501));
check(
  paste.isDuplicatePastedTextFile(sameB, [sameA]),
  "identical synthesized pastes did not deduplicate",
);
check(
  !paste.isDuplicatePastedTextFile(different, [sameA]),
  "different same-size pastes were deduplicated",
);
check(
  !paste.isDuplicatePastedTextFile(
    new File([sameText], "ordinary.txt", { type: "text/plain" }),
    [sameA],
  ),
  "ordinary user files entered synthesized-paste dedup",
);
""",
        encoding="utf-8",
    )
    proc = subprocess.run(
        ["node", str(script)],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 0, f"paste harness failed:\n{proc.stderr}\n{proc.stdout}"


def test_attachment_snapshot_reports_in_flight_upload(tmp_path: Path) -> None:
    """A synthesized paste cannot disappear from a send-time snapshot while
    its immediate upload is still resolving."""
    if shutil.which("node") is None:
        pytest.skip("node binary not available on PATH")

    script = tmp_path / "attachment_snapshot_harness.mjs"
    module_url = json.dumps(_ATTACHMENTS.as_uri())
    script.write_text(
        f"const moduleUrl = {module_url};\n"
        + r"""
import { File } from "node:buffer";
globalThis.File = File;
globalThis.window = globalThis;

function fakeElement() {
  return {
    children: [],
    dataset: {},
    appendChild(child) {
      this.children.push(child);
      return child;
    },
    addEventListener() {},
    querySelector() { return null; },
    setAttribute() {},
    remove() {},
  };
}
globalThis.document = { createElement: () => fakeElement() };

let resolveUpload;
const uploadResponse = new Promise((resolve) => { resolveUpload = resolve; });
const attachmentsModule = await import(moduleUrl);
const controller = attachmentsModule.createAttachmentController({
  chipsEl: fakeElement(),
  getWsId: () => "ws-1",
  authFetch: () => uploadResponse,
});

let snap = controller.snapshot();
if (snap.uploading || snap.attachment_ids.length)
  throw new Error("empty controller reported an upload");

controller.upload(new File(["large paste"], "pasted-text.txt", {
  type: "text/plain",
}));
snap = controller.snapshot();
if (!snap.uploading)
  throw new Error("in-flight placeholder was omitted from snapshot state");
if (snap.attachments.length || snap.attachment_ids.length)
  throw new Error("placeholder escaped into stable attachment arrays");

resolveUpload({
  ok: true,
  status: 200,
  json: () => Promise.resolve({
    attachment_id: "attachment-1",
    filename: "pasted-text.txt",
    size_bytes: 11,
    mime_type: "text/plain",
    kind: "text",
  }),
});
await new Promise((resolve) => setTimeout(resolve, 0));
snap = controller.snapshot();
if (snap.uploading)
  throw new Error("settled upload remained marked in flight");
if (snap.attachment_ids.join(",") !== "attachment-1")
  throw new Error("settled upload was not sendable: " + snap.attachment_ids);
""",
        encoding="utf-8",
    )
    proc = subprocess.run(
        ["node", str(script)],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 0, (
        f"attachment snapshot harness failed:\n{proc.stderr}\n{proc.stdout}"
    )


def test_paste_text_wiring_guard_rails() -> None:
    """Guard every surface around the behavior-tested shared helper."""
    helper = _HELPER.read_text(encoding="utf-8")
    composer = _COMPOSER.read_text(encoding="utf-8")
    attachments = _ATTACHMENTS.read_text(encoding="utf-8")
    interactive = _INTERACTIVE.read_text(encoding="utf-8")
    ui_app = _UI_APP.read_text(encoding="utf-8")
    console_app = _CONSOLE_APP.read_text(encoding="utf-8")
    coordinator = _COORDINATOR.read_text(encoding="utf-8")

    assert "window.TurnstonePasteText" in helper
    assert "PASTE_ATTACHMENT_CHARS = 2000" in helper
    assert "paste_attachment_chars" not in helper
    assert "loadPasteThresholdChars" not in helper
    assert 'from "./composer_paste_text.js"' in composer
    assert "pasteTextToFile(text" in composer
    assert "2000" not in composer, "the threshold belongs in the shared helper"
    assert composer.index("if (uploaded > 0)") < composer.index("pasteTextToFile(text")
    assert "if (accepted !== false) e.preventDefault();" in composer
    assert "if (pending.has(info.attachment_id))" in attachments
    assert "pending.delete(placeholderId);" in attachments
    assert "uploading: uploading" in attachments

    assert "function _handleComposerPaste(" in ui_app
    assert "if (files.length > 0)" in ui_app
    assert "if (!textFile || addFiles([textFile]) === false) return false;" in ui_app
    assert "_handleComposerPaste(event, _newWsAddFiles)" in ui_app
    assert "_handleComposerPaste(e, _addDashboardFiles)" in ui_app
    assert "initEl.onpaste" in ui_app
    assert ui_app.count("Add a message to send with this attachment.") == 2
    assert "_loadPasteAttachmentSetting" not in ui_app

    assert "return _homeStageFile(file);" in console_app
    assert "isDuplicatePastedTextFile(file, _homeStagedFiles)" in console_app
    assert "Add a message to send with this attachment." in console_app
    assert "_loadPasteAttachmentSetting" not in console_app

    for pane in (interactive, coordinator):
        assert "Add a message to send with this attachment." in pane
        assert "Attachments can't be sent while the assistant is working." in pane
        assert "if (snap.uploading)" in pane
        assert "Wait for attachments to finish uploading before sending." in pane
        assert "!attachments.isEmpty()" in pane or "!this.attachments.isEmpty()" in pane
    assert "loadPasteThresholdChars" not in coordinator

    for page in (
        _ROOT / "turnstone/ui/static/index.html",
        _ROOT / "turnstone/console/static/index.html",
        _ROOT / "turnstone/console/static/coordinator/index.html",
    ):
        body = page.read_text(encoding="utf-8")
        assert "/shared/composer_paste_text.js" in body, page
        assert body.index("/shared/composer_paste_text.js") < body.index("/shared/composer.js"), (
            page
        )
