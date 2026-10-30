"""Behavioral coverage for the Files browser and its embedded preview viewer."""

from __future__ import annotations

import json
from pathlib import Path

from tests._js_harness_helpers import FAKE_DOM, demodulize, node_skip, run_node_source

_SHARED = Path(__file__).resolve().parent.parent / "turnstone/shared_static"
pytestmark = node_skip


def _run_files(scenario: str) -> None:
    source = (
        FAKE_DOM
        + f"\nconst {{ ShellPane }} = await import({json.dumps((_SHARED / 'pane.js').as_uri())});\n"
        + r"""
const assert = (value, message) => { if (!value) throw new Error(message); };
Object.defineProperty(FakeElement.prototype, "lastElementChild", {
  get() { return this.children.at(-1) || null; },
});
const requests = [];
const copies = [];
const timers = [];
const saved = [];
let copyOk = true;
const authFetch = (url) => new Promise((resolve, reject) => requests.push({url, resolve, reject}));
const copyTextToClipboard = async (text) => { copies.push(text); return copyOk; };
const redactCredentials = (text) => text;
const renderMarkdown = (text) => text;
const postRenderMarkdown = () => {};
const postRenderHljs = () => {};
const setSafeHtml = (node, text) => { node.textContent = text; };
globalThis.setTimeout = (fn) => timers.push(fn);
const flush = () => new Promise(setImmediate);
const mount = (extra = null) => {
  const pane = createFilesystemPane(extra, {persistMeta: (meta) => saved.push(meta)});
  pane.el = document.createElement("section");
  pane.bodyEl = document.createElement("div");
  pane.el.append(pane.bodyEl);
  pane.onMount();
  return pane;
};
const succeed = async (request, body = "", status = 200) => {
  request.resolve(new Response(body, {status}));
  await flush();
};
const listing = async (request, path, entries, truncated = false) => succeed(request, JSON.stringify({
  root: "/workspace/project", path, entries, truncated,
}));
const file = (path, kind = "text") => ({
  name: path.split("/").pop(), path, kind: "file", size: 12,
  preview_kind: kind, content_type: kind === "web" ? "text/html" : "text/plain",
});
const folder = (path) => ({name: path.split("/").pop(), path, kind: "directory"});
const row = (pane, path) => pane._list.querySelectorAll(".filesystem-entry").find((el) => el.dataset.path === path);
const href = (pane) => pane._preview._downloadLink.getAttribute("href");
"""
        + demodulize(_SHARED / "preview.js")
        + "\n"
        + demodulize(_SHARED / "filesystem.js")
        + "\n"
        + scenario
    )
    proc = run_node_source(source)
    assert proc.returncode == 0, f"files harness failed:\n{proc.stderr}\n{proc.stdout}"


def test_navigation_origin_transport_copy_download_and_refresh() -> None:
    _run_files(r"""
const pane = mount();
const ctx = {base: "/node/node A", wsId: "ws/A"};
pane.showFiles(ctx);
assert(requests[0].url === "/node/node A/v1/api/workstreams/ws%2FA/files?root=workspace&path=", "list uses originating transport");
await listing(requests.shift(), "", [folder("out put"), file("<script>.txt")]);
assert(row(pane, "<script>.txt").querySelector(".filesystem-name").textContent === "<script>.txt", "filenames stay inert text");
row(pane, "out put").click();
assert(requests[0].url.endsWith("&path=out%20put"), "folder path is encoded");
await listing(requests.shift(), "out put", [file("out put/hello #.js")]);
row(pane, "out put/hello #.js").click();
assert(requests[0].url.endsWith("/files/preview?root=workspace&path=out%20put%2Fhello%20%23.js"), "selected file uses preview route");
assert(!href(pane), "pending file cannot download");
await succeed(requests.shift(), "const answer = 42;\n");
assert(href(pane).endsWith("/files/content?root=workspace&path=out%20put%2Fhello%20%23.js"), "download is original file");
assert(pane._preview._downloadLink.hasAttribute("download"), "download saves instead of navigating");
pane._preview._copyBtn.click();
await flush();
assert(copies[0] === "const answer = 42;\n", "copy preserves original text");
assert(pane._preview._copyStatus.textContent === "Copied", "copy feedback is shown");
assert(saved.at(-1).selected === "out put/hello #.js", "selection is persisted");
pane.showFiles(ctx);
assert(requests[0].url.endsWith("&path=out%20put"), "same context retains folder");
assert(!href(pane), "refresh clears stale download while pending");
await listing(requests.shift(), "out put", [file("out put/hello #.js")]);
await succeed(requests.shift(), "updated");
assert(pane._preview._contentEl.querySelector("code").textContent === "updated", "refresh reloads selected file");
pane._up.click();
assert(requests[0].url.endsWith("&path="), "up returns to root");
await listing(requests.shift(), "", []);
assert(pane._up.disabled, "cannot escape workspace root");
assert(!href(pane), "directory changes clear preview");
""")


def test_card_target_resolves_on_origin_and_selects_file_without_manual_navigation() -> None:
    _run_files(r"""
const pane = mount();
pane.showFiles({base: "/node/B", wsId: "session/A"}, "/data/reports/a #.html");
assert(requests[0].url === "/node/B/v1/api/workstreams/session%2FA/files/resolve?root=workspace&path=%2Fdata%2Freports%2Fa%20%23.html", "resolve must preserve node and encode full path");
await succeed(requests.shift(), JSON.stringify({
  root: "/data", root_id: "data", path: "reports", selected: "reports/a #.html",
  entries: [file("reports/a #.html", "web")], truncated: true,
}));
assert(pane._rootId === "data" && pane._rootSelect.value === "data", "resolved root must become active");
assert(pane._path === "reports", "resolved parent must be visible");
assert(row(pane, "reports/a #.html").getAttribute("aria-current") === "true", "target must be selected");
assert(requests[0].url.includes("/files/preview?root=data&path=reports%2Fa%20%23.html"), "preview uses resolved root");
await succeed(requests.shift(), "<h1>Report</h1>");
assert(href(pane).includes("/files/content?root=data&path="), "download uses resolved root");
assert(saved.at(-1).selected === "reports/a #.html", "resolved selection is persisted");
""")


def test_capped_card_selection_survives_refresh_reopen_and_pane_restore() -> None:
    _run_files(r"""
let pane = mount();
const ctx = {base: "/node/B", wsId: "session/A"};
const target = "/data/reports/late #.txt";
const answerBrowse = async request => {
  const resolving = request.url.includes("/files/resolve?");
  await succeed(request, JSON.stringify({
    root: "/data", root_id: "data", path: "reports", selected: "reports/late #.txt",
    entries: resolving ? [file("reports/late #.txt")] : [], truncated: true,
  }));
};
const answerPreview = async text => {
  assert(requests[0]?.url.includes("/files/preview?root=data&path=reports%2Flate%20%23.txt"), "capped-out target must be previewed");
  await succeed(requests.shift(), text);
  assert(row(pane, "reports/late #.txt")?.getAttribute("aria-current") === "true", "capped-out target stays selected");
  assert(pane._preview._contentEl.querySelector("pre").textContent === text, "preview reloads current file bytes");
};
pane.showFiles(ctx, "reports/late #.txt");
await answerBrowse(requests.shift());
await answerPreview("initial");
assert(saved.at(-1).selectedTarget === target, "resolved target persists its canonical absolute path");
pane.bodyEl.querySelectorAll("button").find(button => button.getAttribute("aria-label") === "Refresh files").click();
assert(requests[0].url.endsWith("/files/resolve?root=data&path=%2Fdata%2Freports%2Flate%20%23.txt"), "refresh must resolve the selected target");
await answerBrowse(requests.shift());
await answerPreview("refreshed");
pane.showFiles(ctx);
await answerBrowse(requests.shift());
await answerPreview("reopened");
const restoredMeta = saved.at(-1);
pane.onClose();
pane = mount(restoredMeta);
assert(requests[0].url.startsWith("/node/B/v1/api/workstreams/session%2FA/files/resolve?"), "restore retains origin and resolves the target");
await answerBrowse(requests.shift());
await answerPreview("restored");
pane._up.click();
assert(!requests[0].url.includes("/resolve?") && requests[0].url.endsWith("&path="), "folder navigation must clear the direct target");
await listing(requests.shift(), "", []);
assert(!saved.at(-1).selectedTarget && !href(pane), "folder navigation clears persisted target and preview");
""")


def test_linked_target_survives_repeated_refresh_and_restore_while_loading() -> None:
    _run_files(r"""
let pane = mount();
const ctx = {base: "/node/A", wsId: "one"};
const target = "/tmp/reports/linked.txt";
const resolved = {
  root: "/tmp", root_id: "tmp", path: "reports", selected: "reports/linked.txt",
  entries: [file("reports/linked.txt")], truncated: true,
};
const refresh = () => pane.bodyEl.querySelectorAll("button")
  .find(button => button.getAttribute("aria-label") === "Refresh files").click();
const nextResolve = () => {
  const request = requests.shift();
  assert(request.url.includes("/files/resolve?") && request.url.endsWith("path=%2Ftmp%2Freports%2Flinked.txt"), "loading must preserve the linked target for another refresh or restore");
  return request;
};
const finish = async request => {
  await succeed(request, JSON.stringify(resolved));
  assert(requests[0]?.url.includes("/files/preview?root=tmp&path=reports%2Flinked.txt"), "resolved target must preview");
  await succeed(requests.shift(), "linked");
  assert(pane._selected === "reports/linked.txt", "capped-out file must remain selected");
};
pane.showFiles(ctx, target);
await finish(nextResolve());
refresh();
const firstRefresh = nextResolve();
refresh();
await finish(nextResolve());
await succeed(firstRefresh, JSON.stringify(resolved));
assert(!requests.length, "superseded refresh must not start another preview");
refresh();
const oldPaneRequest = nextResolve();
const loadingMeta = saved.at(-1);
assert(loadingMeta.selectedTarget === target, "persist the pending target before the request completes");
pane.onClose();
pane = mount(loadingMeta);
const restoredRequest = nextResolve();
assert(restoredRequest.url.startsWith("/node/A/v1/api/workstreams/one/"), "restoration preserves the originating node");
await succeed(oldPaneRequest, JSON.stringify(resolved));
assert(!requests.length, "closed pane response cannot start a preview");
await finish(restoredRequest);
""")


def test_selecting_another_file_clears_card_target_and_stale_resolution_is_ignored() -> None:
    _run_files(r"""
const pane = mount();
const ctx = {base: "/node/A", wsId: "one"};
const resolved = {
  root: "/tmp", root_id: "tmp", path: "reports", selected: "reports/linked.txt",
  entries: [file("reports/linked.txt"), file("reports/other.txt")], truncated: true,
};
pane.showFiles(ctx, "/tmp/reports/linked.txt");
await succeed(requests.shift(), JSON.stringify(resolved));
await succeed(requests.shift(), "linked");
row(pane, "reports/other.txt").click();
await succeed(requests.shift(), "other");
assert(!saved.at(-1).selectedTarget, "selecting another file clears the previous direct target");
pane.showFiles(ctx);
assert(!requests[0].url.includes("/resolve?"), "reopen must not switch back to the old linked file");
await listing(requests.shift(), "reports", resolved.entries, true);
await succeed(requests.shift(), "other updated");
pane.showFiles(ctx, "/tmp/reports/linked.txt");
const stale = requests.shift();
pane._up.click();
await listing(requests.shift(), "", []);
await succeed(stale, JSON.stringify(resolved));
assert(pane._path === "" && pane._selected === null && !saved.at(-1).selectedTarget, "stale resolution cannot restore a target after folder navigation");
assert(!requests.length && !href(pane), "stale resolution must not trigger a preview");
""")


def test_card_resolution_retry_and_stale_response_cannot_replace_newer_target() -> None:
    _run_files(r"""
const pane = mount();
const ctx = {base: "/node/A", wsId: "one"};
pane.showFiles(ctx, "/workspace/missing.txt");
await succeed(requests.shift(), JSON.stringify({error: "File or directory not found"}), 404);
assert(pane._status.textContent === "File or directory not found", "missing file gets actionable error");
pane._list.querySelector("button").click();
assert(requests[0].url.includes("/files/resolve?") && requests[0].url.endsWith("missing.txt"), "retry must resolve the original target");
const old = requests.shift();
pane.showFiles(ctx, "/tmp/latest.txt");
await succeed(requests.shift(), JSON.stringify({root: "/tmp", root_id: "tmp", path: "", selected: "latest.txt", entries: [file("latest.txt")]}));
await succeed(requests.shift(), "latest");
await succeed(old, JSON.stringify({root: "/workspace", root_id: "workspace", path: "", selected: "missing.txt", entries: [file("missing.txt")]}));
assert(pane._rootId === "tmp" && pane._selected === "latest.txt", "older response cannot steal target");
assert(!row(pane, "missing.txt"), "older response cannot repaint the list");
""")


def test_context_changes_discard_stale_listing_and_preview() -> None:
    _run_files(r"""
const pane = mount();
pane.showFiles({base: "/node/A", wsId: "one"});
const oldList = requests.shift();
pane.showFiles({base: "/node/B", wsId: "two"});
await listing(requests.shift(), "", [file("new.txt")]);
await listing(oldList, "old", [file("old.txt")]);
assert(row(pane, "new.txt") && !row(pane, "old.txt"), "stale listing cannot replace new context");
row(pane, "new.txt").click();
const oldPreview = requests.shift();
pane.showFiles({base: "/node/C", wsId: "three"});
assert(pane._selected === null && !href(pane), "new context clears selection and actions immediately");
await succeed(oldPreview, "stale bytes");
assert(!pane._preview._contentEl.querySelector("pre"), "stale preview cannot render");
await listing(requests.shift(), "", [file("last.txt")]);
row(pane, "last.txt").click();
await succeed(requests.shift(), "last");
assert(href(pane).startsWith("/node/C/v1/api/workstreams/three/"), "new file retains new origin");
""")


def test_browser_exposes_file_types_and_a_breadcrumb_location() -> None:
    _run_files(r"""
const pane = mount();
pane.showFiles({base: "", wsId: "one"});
await listing(requests.shift(), "", [folder("output")]);
const directory = row(pane, "output");
assert(directory.dataset.type === "folder", "folders have distinct presentation");
assert(directory.querySelector(".filesystem-type").textContent === "Folder", "folder type is not color-only");
assert(directory.querySelector(".filesystem-glyph").getAttribute("aria-hidden") === "true", "decorative icons do not replace file names");
directory.click();
await listing(requests.shift(), "output", [
  file("output/analysis.js"), file("output/report.html", "web"),
  file("output/data.csv", "table"), file("output/chart.png", "image"),
  file("output/notes.md", "markdown"), file("output/archive.zip", "binary"),
  file("output/readme.txt"),
]);
assert(pane._directoryName.textContent === "output", "list is headed by the current directory");
assert(pane._crumbs.title === "/workspace/project/output", "address bar exposes the full current path");
assert(pane._crumbs.lastElementChild.getAttribute("aria-current") === "location", "current breadcrumb is identified");
for (const [path, type, label] of [
  ["analysis.js", "code", "JS"], ["report.html", "code", "HTML"],
  ["data.csv", "data", "Data"], ["chart.png", "image", "Image"],
  ["notes.md", "document", "MD"], ["archive.zip", "binary", "Binary"],
  ["readme.txt", "text", "Text"],
]) {
  const entry = row(pane, "output/" + path);
  assert(entry.dataset.type === type, "file category: " + path);
  assert(entry.querySelector(".filesystem-type").textContent === label, "visible file type: " + path);
}
pane._crumbs.querySelector("button").click();
assert(requests[0].url.endsWith("&path="), "root breadcrumb navigates back to root");
""")


def test_address_bar_prefixes_paths_with_the_originating_node() -> None:
    _run_files(r"""
const shown = (pane) => pane._nodeName.children.map((part) => part.textContent);
let pane = mount();
assert(pane._node.hidden, "an unopened pane names no node");
pane.showFiles({base: "/node/node-5", wsId: "one"});
assert(!pane._node.hidden && shown(pane).join("") === "node-5", "a proxied path starts with its node");
assert(pane._node.title === "Node node-5", "the prefix explains itself on hover");
assert(pane._node.getAttribute("aria-hidden") === "true", "the visual prefix is not read twice");
assert(pane._crumbs.getAttribute("aria-label") === "File location on node node-5", "the location landmark names the node");
assert(pane._node.parentNode.contains(pane._crumbs) && !pane._crumbs.contains(pane._node), "node prefix does not scroll with the breadcrumbs");
assert(pane._crumbs.title === "Workspace", "the placeholder root label is not namespaced as a path");
await listing(requests.shift(), "docs", [file("docs/a.txt")]);
assert(pane._crumbs.title === "node-5:/workspace/project/docs", "full location is namespaced by node");
assert(pane._directoryName.title === pane._crumbs.title, "directory heading shares the namespaced location");
const restoredMeta = saved.at(-1);
pane.onClose();
pane = mount(restoredMeta);
assert(shown(pane).join("") === "node-5", "a restored pane names its node from the saved transport");
await listing(requests.shift(), "docs", []);
const replica = "turnstone-server-7d9f8c6b5-x2x4p_ab12";
pane.showFiles({base: "/node/" + encodeURIComponent(replica), wsId: "two"});
assert(shown(pane)[1] === "x2x4p_ab12" && shown(pane).join("") === replica, "the distinguishing end of a long id never truncates");
await listing(requests.shift(), "", []);
pane.showFiles({base: "/node/node-12_ab12", wsId: "short"});
assert(shown(pane)[0] === "" && shown(pane)[1] === "node-12_ab12", "a short id is never split into a one- or two-character head");
await listing(requests.shift(), "", []);
const rocket = String.fromCodePoint(0x1f680);
pane.showFiles({base: "/node/" + encodeURIComponent("turnstone-" + rocket + "x2x4p_ab1"), wsId: "astral"});
assert(shown(pane)[0] === "turnstone-" && shown(pane)[1] === rocket + "x2x4p_ab1", "the split never separates a surrogate pair");
await listing(requests.shift(), "", []);
pane.showFiles({base: "/node/" + encodeURIComponent("rack 3"), wsId: "spaced"});
assert(shown(pane).join("") === "rack 3", "an encoded node id is shown decoded");
await listing(requests.shift(), "", []);
pane.showFiles({base: "/node/100%", wsId: "odd"});
assert(shown(pane).join("") === "100%", "an undecodable node id is shown as sent");
await listing(requests.shift(), "", []);
pane.showFiles({base: "", wsId: "three"});
assert(pane._node.hidden && !shown(pane).join(""), "a local session has no node prefix");
assert(pane._crumbs.getAttribute("aria-label") === "File location", "a local location landmark names no node");
await listing(requests.shift(), "", []);
assert(pane._crumbs.title === "/workspace/project", "local paths are not namespaced");
""")


def test_breadcrumb_start_fades_only_while_earlier_crumbs_are_scrolled_out() -> None:
    _run_files(r"""
const pane = mount();
const fades = () => pane._crumbs.classList.contains("is-clipped-start");
const refresh = () => pane.bodyEl.querySelectorAll("button")
  .find(button => button.getAttribute("aria-label") === "Refresh files").click();
pane.showFiles({base: "/node/node-5", wsId: "one"});
await listing(requests.shift(), "a/b", []);
assert(!fades(), "a trail that fits has no fade");
pane._crumbs.scrollWidth = 240;
refresh();
assert(fades(), "a trail scrolled to its current folder fades its clipped start");
await listing(requests.shift(), "a/b", []);
pane._crumbs.scrollLeft = 0;
pane._crumbs.dispatch("scroll");
assert(!fades(), "scrolling back to the root removes the fade");
pane._crumbs.scrollLeft = 30;
pane._crumbs.dispatch("scroll");
assert(fades(), "scrolling away from the root restores the fade");
""")


def test_html_raw_copy_and_binary_download_use_embedded_viewer() -> None:
    _run_files(r"""
const pane = mount();
pane.showFiles({base: "", wsId: "one"});
await listing(requests.shift(), "", [file("page.html", "web"), file("archive.zip", "binary")]);
row(pane, "page.html").click();
assert(requests[0].url.endsWith("/preview?root=workspace&path=page.html&probe=1"), "probe extends existing query string");
await succeed(requests.shift());
const frame = pane._preview._contentEl.querySelector("iframe");
assert(frame.getAttribute("sandbox") === "", "HTML remains fully sandboxed");
assert(frame.src.endsWith("/preview?root=workspace&path=page.html"), "frame URL identifies selected file");
assert(pane._preview.bodyEl.querySelector(".preview-history").hidden, "browser owns navigation");
pane._preview._copyBtn.click();
assert(requests[0].url.endsWith("/preview?root=workspace&path=page.html"), "HTML copy reads decoded source");
await succeed(requests.shift(), "<!doctype html>\n<h1>Hello</h1>");
assert(copies[0] === "<!doctype html>\n<h1>Hello</h1>", "HTML copy is source, not rendered text");
row(pane, "archive.zip").click();
assert(!requests.length, "binary does not fetch bytes into a text renderer");
assert(href(pane).endsWith("/content?root=workspace&path=archive.zip"), "unsupported binary remains downloadable");
assert(pane._preview._copyBtn.disabled, "binary cannot be copied as text");
assert(pane._preview._contentEl.querySelector(".preview-note"), "binary has explicit unsupported state");
assert(pane._preview._stack.length === 1, "embedded viewer does not accumulate history");
""")


def test_html_copy_preserves_legacy_encoding(tmp_path, monkeypatch) -> None:
    from turnstone.core import filesystem

    source = "<!doctype html>\n<h1>Café — résumé</h1>"
    original = source.encode("cp1252")
    (tmp_path / "page.html").write_bytes(original)
    monkeypatch.setattr(filesystem, "get_workspace_dir", lambda: str(tmp_path))
    preview = filesystem.read_file("page.html", preview=True)[0]
    download = filesystem.read_file("page.html", preview=False)[0]
    assert download == original

    _run_files(
        f"const expectedSource = {json.dumps(source)};\n"
        f"const previewBytes = {json.dumps(list(preview))};\n"
        f"const downloadBytes = {json.dumps(list(download))};\n"
        + r"""
const pane = mount();
pane.showFiles({base: "", wsId: "one"});
await listing(requests.shift(), "", [file("page.html", "web")]);
row(pane, "page.html").click();
await succeed(requests.shift(), null, 204);
pane._preview._copyBtn.click();
const request = requests.shift();
const bytes = request.url.includes("/preview?") ? previewBytes : downloadBytes;
await succeed(request, new Uint8Array(bytes));
assert(copies[0] === expectedSource, "HTML copy preserves accented letters and punctuation");
assert(pane._preview._copyStatus.textContent === "Copied", "copy succeeds for legacy HTML");
assert(href(pane).endsWith("/content?root=workspace&path=page.html"), "download still uses original bytes");
"""
    )


def test_errors_are_not_empty_directories_and_files_do_not_retry() -> None:
    _run_files(r"""
const pane = mount();
pane.showFiles({base: "", wsId: "one"});
await succeed(requests.shift(), "denied", 403);
assert(pane._list.querySelector(".filesystem-error"), "403 is an error, not an empty directory");
assert(pane._status.textContent.includes("access"), "permission error is explained");
pane._list.querySelector("button").click();
await listing(requests.shift(), "", [file("gone.txt")], true);
assert(pane._status.textContent.includes("limited"), "truncation is explicit");
row(pane, "gone.txt").click();
await succeed(requests.shift(), "missing", 404);
assert(!timers.length, "live files do not use attachment-persist retries");
assert(pane._preview._contentEl.querySelector(".preview-error"), "missing files expose retry immediately");
assert(!href(pane), "missing file download is disabled");
pane.showFiles({base: "", wsId: "one"});
await succeed(requests.shift(), "closed", 409);
assert(pane._status.textContent.includes("Reopen"), "closed sessions explain how to recover");
""")


def test_rehydrate_and_close_fence_pending_work() -> None:
    _run_files(r"""
const pane = mount({ctx: {base: "/node/A", wsId: "one"}, path: "output", selected: "output/kept.txt"});
assert(requests[0].url.endsWith("&path=output"), "reload restores directory");
await listing(requests.shift(), "output", [file("output/kept.txt")]);
assert(pane._selected === "output/kept.txt", "reload restores existing selected file");
await succeed(requests.shift(), "kept");
assert(href(pane), "restored preview enables download when ready");
pane.showFiles({base: "/node/A", wsId: "one"});
const pending = requests.shift();
pane.onClose();
await listing(pending, "output", [file("output/ghost.txt")]);
assert(!row(pane, "output/ghost.txt"), "closed pane ignores pending list");
assert(!href(pane), "close invalidates preview actions");
pane.showFiles({base: "", wsId: "new"});
assert(!requests.length, "closed pane cannot restart requests");
""")


def test_pending_html_copy_is_cancelled_on_selection_change() -> None:
    _run_files(r"""
const pane = mount();
pane.showFiles({base: "", wsId: "one"});
await listing(requests.shift(), "", [file("page.html", "web"), file("other.txt")]);
row(pane, "page.html").click();
await succeed(requests.shift());
pane._preview._copyBtn.click();
const oldCopy = requests.shift();
row(pane, "other.txt").click();
await succeed(oldCopy, "old html");
assert(copies.length === 0, "pending copy cannot copy stale selection");
await succeed(requests.shift(), "current");
copyOk = false;
pane._preview._copyBtn.click();
await flush();
assert(pane._preview._copyStatus.textContent === "Could not copy", "clipboard failure is visible");
assert(!pane._preview._copyBtn.disabled, "clipboard failures permit retry");
""")


def test_preview_limits_keep_download_and_surface_server_errors() -> None:
    _run_files(r"""
const pane = mount();
pane.showFiles({base: "", wsId: "one"});
await listing(requests.shift(), "", [file("large.txt")]);
row(pane, "large.txt").click();
await succeed(requests.shift(), JSON.stringify({error: "File exceeds the 2 MiB preview limit"}), 413);
assert(pane._preview._contentEl.querySelector(".preview-error-msg").textContent.includes("2 MiB"), "preview explains size limit");
assert(href(pane).endsWith("/content?root=workspace&path=large.txt"), "preview limit retains original download");
assert(pane._preview._copyBtn.disabled, "failed preview does not enable copy");
pane.showFiles({base: "", wsId: "one"});
await succeed(requests.shift(), JSON.stringify({error: "Workspace directory is unavailable"}), 503);
assert(pane._status.textContent === "Workspace directory is unavailable", "listing shows backend recovery information");
pane.showFiles({base: "", wsId: "one"});
await succeed(requests.shift(), JSON.stringify({error: "x".repeat(500)}), 503);
assert(pane._status.textContent.length === 400, "backend messages are bounded");
""")


def test_root_selection_routes_every_request_and_restores_independently() -> None:
    _run_files(r"""
const pane = mount();
const ctx = {base: "/node/A", wsId: "one"};
pane.showFiles(ctx);
const staleWorkspace = requests.shift();
pane._rootSelect.value = "data";
pane._rootSelect.dispatch("change");
assert(requests[0].url.endsWith("?root=data&path="), "root selector lists chosen root");
await listing(requests.shift(), "", [file("same.txt")]);
await listing(staleWorkspace, "", [file("stale.txt")]);
assert(!row(pane, "stale.txt"), "old root listing cannot replace current root");
row(pane, "same.txt").click();
assert(requests[0].url.endsWith("/preview?root=data&path=same.txt"), "preview uses selected root");
await succeed(requests.shift(), "data bytes");
assert(href(pane).endsWith("/content?root=data&path=same.txt"), "download uses selected root");
assert(saved.at(-1).rootId === "data", "root id persisted independently of display path");
pane.showFiles(ctx);
assert(requests[0].url.includes("root=data"), "same conversation preserves selected root");
await listing(requests.shift(), "", []);
pane.showFiles({base: "/node/B", wsId: "two"});
assert(requests[0].url.endsWith("?root=workspace&path="), "new conversation starts at workspace");
await listing(requests.shift(), "", []);
const restored = mount({ctx, rootId: "tmp", path: "output", selected: "output/a.txt"});
assert(requests[0].url.endsWith("?root=tmp&path=output"), "reload restores selected root");
await listing(requests.shift(), "output", [file("output/a.txt")]);
assert(requests[0].url.includes("root=tmp"), "restored selection preview uses restored root");
await succeed(requests.shift(), "tmp bytes");
assert(href(restored).includes("root=tmp"), "restored selection download uses restored root");
""")


def test_explicit_open_focuses_files_but_restoration_is_passive() -> None:
    _run_files(r"""
const outside = document.createElement("button");
outside.focus();
const ctx = {base: "", wsId: "one"};
const pane = mount({ctx});
assert(document.activeElement === outside, "restoring Files does not take focus");
await listing(requests.shift(), "", []);
assert(document.activeElement === outside, "restored listing leaves focus alone");
pane.showFiles(ctx);
assert(document.activeElement === pane._rootSelect, "explicit open focuses the root selector");
await listing(requests.shift(), "", [file("note.txt")]);
assert(document.activeElement === pane._rootSelect, "listing completion preserves initial focus");
assert(pane._list.getAttribute("role") === "region", "file browser exposes its accessible label");
assert(pane._preview.el.getAttribute("role") === "region", "viewer exposes its accessible label");
""")


def test_keyboard_navigation_keeps_focus_and_does_not_steal_it_back() -> None:
    _run_files(r"""
const pane = mount();
pane.showFiles({base: "", wsId: "one"});
await listing(requests.shift(), "", [folder("output")]);
row(pane, "output").focus();
row(pane, "output").click();
assert(document.activeElement === pane._list, "loading retains keyboard focus in browser");
await listing(requests.shift(), "output", [folder("output/next")]);
assert(document.activeElement === row(pane, "output/next"), "navigation focuses first new entry");
row(pane, "output/next").click();
pane._rootSelect.focus();
await listing(requests.shift(), "output/next", [file("output/next/a.txt")]);
assert(document.activeElement === pane._rootSelect, "completion does not steal focus user moved elsewhere");
""")
