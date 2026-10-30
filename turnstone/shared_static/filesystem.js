// A user-operated, read-only view of the originating node's file roots. The
// directory browser and the existing preview renderer share one shell pane;
// browsing never creates an assistant turn or copies files into attachments.
import { ShellPane } from "./pane.js";
import { authFetch } from "./auth.js";
import { createPreviewPane, sourceLanguage } from "./preview.js";

function fileElement(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

function fileButton(text, label, action) {
  const button = fileElement("button", "filesystem-action", text);
  button.type = "button";
  button.title = label;
  button.setAttribute("aria-label", label);
  button.addEventListener("click", action);
  return button;
}

function filesUrl(ctx, endpoint = "", path = "") {
  return (
    (ctx.base || "") +
    "/v1/api/workstreams/" +
    encodeURIComponent(ctx.wsId) +
    "/files" +
    (endpoint ? "/" + endpoint : "") +
    "?root=" +
    encodeURIComponent(ctx.rootId || "workspace") +
    "&path=" +
    encodeURIComponent(path)
  );
}

// Console requests reach a node through /node/{encoded node id}; a local
// session's base is "" and names no node.
function filesNode(base) {
  const match = /^\/node\/([^/]+)/.exec(base || "");
  if (!match) return "";
  try {
    return decodeURIComponent(match[1]);
  } catch (_) {
    return match[1];
  }
}

function fileSize(size) {
  if (size == null) return "";
  if (size < 1024) return size + " B";
  if (size < 1024 * 1024) return (size / 1024).toFixed(1) + " KB";
  return (size / (1024 * 1024)).toFixed(1) + " MB";
}

function fileAppearance(entry) {
  if (entry.kind === "directory") return { type: "folder", label: "Folder", icon: "📁" };
  const kinds = {
    web: { type: "code", label: "HTML", icon: "📄" },
    table: { type: "data", label: "Data", icon: "▦" },
    image: { type: "image", label: "Image", icon: "▧" },
    pdf: { type: "document", label: "PDF", icon: "📄" },
    markdown: { type: "document", label: "MD", icon: "📄" },
    binary: { type: "binary", label: "Binary", icon: "⬡" },
  };
  if (kinds[entry.preview_kind]) return kinds[entry.preview_kind];
  const language = sourceLanguage(entry.path);
  if (language) {
    const extension = entry.name.includes(".") ? entry.name.split(".").pop() : "Code";
    return { type: "code", label: extension.slice(0, 8).toUpperCase(), icon: "‹›" };
  }
  return { type: "text", label: "Text", icon: "📄" };
}

export function createFilesystemPane(extra, hostApi) {
  const api = hostApi || {};
  const pane = new ShellPane({
    type: "filesystem",
    title: "Files",
    glyph: "▤",
    ephemeral: true,
  });
  pane._ctx = null;
  pane._path = "";
  pane._root = "";
  pane._rootId = "workspace";
  pane._selected = null;
  pane._selectedTarget = null;
  pane._requestToken = 0;
  pane._closed = false;

  const persist = () => {
    if (pane._ctx && api.persistMeta) {
      api.persistMeta({
        ctx: pane._ctx,
        rootId: pane._rootId,
        path: pane._path,
        selected: pane._selected,
        selectedTarget: pane._selectedTarget,
      });
    }
  };

  const clearSelection = () => {
    pane._selected = null;
    pane._selectedTarget = null;
    pane._preview.clearPreview();
    for (const row of pane._list.querySelectorAll(".filesystem-entry")) {
      row.classList.remove("is-selected");
      row.removeAttribute("aria-current");
    }
  };

  const selectFile = (entry, targetPath = null) => {
    if (pane._closed) return;
    pane._selected = entry.path;
    pane._selectedTarget = targetPath;
    for (const row of pane._list.querySelectorAll(".filesystem-entry")) {
      const selected = row.dataset.path === entry.path;
      row.classList.toggle("is-selected", selected);
      if (selected) row.setAttribute("aria-current", "true");
      else row.removeAttribute("aria-current");
    }
    pane._preview.showPreview(
      {
        file_path: entry.path,
        kind: entry.preview_kind || "binary",
        title: entry.name,
        source: entry.path,
        content_type: entry.content_type || "application/octet-stream",
        size: entry.size,
      },
      { ...pane._ctx, rootId: pane._rootId },
    );
    persist();
  };

  // Fade the breadcrumbs' start edge while earlier crumbs are scrolled out, so
  // a clipped root is not read as a relative path after the node prefix.
  const markClipped = () => {
    pane._crumbs.classList.toggle("is-clipped-start", pane._crumbs.scrollLeft > 0);
  };

  const paintLocation = () => {
    const rootPath = pane._root || (pane._rootId === "workspace" ? "Workspace" : "/" + pane._rootId);
    // Each node has its own roots, so a proxied path is prefixed with its node,
    // like host:path. The prefix is visual; the location landmark's name
    // carries the node for screen readers.
    const node = filesNode(pane._ctx && pane._ctx.base);
    pane._node.hidden = !node;
    pane._node.title = node ? "Node " + node : "";
    // Replicas share long id prefixes, so a narrow bar truncates the middle of
    // a long id and keeps its distinguishing end visible. Split by code point.
    const chars = Array.from(node);
    const cut = chars.length > 12 ? chars.length - 10 : 0;
    pane._nodeName.replaceChildren(
      fileElement("span", "filesystem-node-head", chars.slice(0, cut).join("")),
      fileElement("span", "filesystem-node-tail", chars.slice(cut).join("")),
    );
    pane._crumbs.setAttribute("aria-label", node ? "File location on node " + node : "File location");
    const rootCrumb = fileButton(rootPath, "Open selected root", () => navigate(""));
    rootCrumb.classList.add("filesystem-crumb", "filesystem-crumb-root");
    rootCrumb.title = rootPath;
    pane._crumbs.replaceChildren(rootCrumb);
    // The "Workspace" placeholder shown before a listing is a label, not a path.
    const namespace = node && rootPath.startsWith("/") ? node + ":" : "";
    pane._crumbs.title = namespace + rootPath.replace(/\/$/, "") + (pane._path ? "/" + pane._path : "");
    let path = "";
    for (const part of pane._path.split("/").filter(Boolean)) {
      path += (path ? "/" : "") + part;
      const target = path;
      const slash = fileElement("span", "filesystem-separator", "/");
      slash.setAttribute("aria-hidden", "true");
      const crumb = fileButton(part, "Open " + path, () => navigate(target));
      crumb.classList.add("filesystem-crumb");
      pane._crumbs.append(slash, crumb);
    }
    const current = pane._crumbs.lastElementChild;
    if (current) current.setAttribute("aria-current", "location");
    // Keep the current folder visible when the breadcrumb trail overflows.
    // Scroll only this address bar, without moving the page or keyboard focus.
    pane._crumbs.scrollLeft = pane._crumbs.scrollWidth;
    markClipped();
    pane._directoryName.textContent = pane._path.split("/").pop() || (pane._rootId === "workspace" ? "Workspace" : "/" + pane._rootId);
    pane._directoryName.title = pane._crumbs.title;
    pane._up.disabled = !pane._path;
    if (api.setTitle) api.setTitle("Files");
  };

  const listError = (message, path, selection, targetPath) => {
    const error = fileElement("div", "filesystem-message filesystem-error", message);
    const retry = fileButton("Retry", "Reload this directory", () =>
      navigate(path, selection, targetPath),
    );
    pane._list.replaceChildren(error, retry);
    pane._status.textContent = message;
  };

  const navigate = async (path, selection = null, targetPath = null) => {
    if (!pane._ctx || pane._closed) return;
    const token = ++pane._requestToken;
    const retainFocus = pane._list.contains(document.activeElement) ||
      pane._crumbs.contains(document.activeElement) ||
      document.activeElement === pane._up;
    const ctx = { ...pane._ctx, rootId: pane._rootId };
    pane._path = path;
    clearSelection();
    // Preserve pending intent for another refresh or restoration while loading.
    pane._selectedTarget = targetPath;
    paintLocation();
    persist();
    pane._list.setAttribute("aria-busy", "true");
    pane._list.replaceChildren(fileElement("div", "filesystem-message", "Loading files…"));
    pane._status.textContent = "Loading files…";
    // Folder/crumb buttons are replaced by navigation. Keep keyboard focus
    // inside the browser while loading; do not reclaim it if the user leaves.
    if (retainFocus) pane._list.focus({ preventScroll: true });
    try {
      const response = await authFetch(filesUrl(ctx, targetPath ? "resolve" : "", targetPath || path));
      if (token !== pane._requestToken || pane._closed) return;
      if (!response.ok) {
        let message = response.status === 404
          ? "This directory is no longer available."
          : response.status === 403
            ? "You do not have access to these files."
            : response.status === 409
              ? "Reopen the conversation to browse its files on this node."
              : "Could not load files (" + response.status + ").";
        try {
          const error = await response.json();
          if (typeof error.error === "string" && error.error.trim()) {
            message = error.error.slice(0, 400);
          }
        } catch (_) { /* Proxy errors may not carry a JSON body. */ }
        if (token !== pane._requestToken || pane._closed) return;
        listError(message, path, selection, targetPath);
        return;
      }
      const data = await response.json();
      if (token !== pane._requestToken || pane._closed) return;
      if (!Array.isArray(data.entries) || typeof data.path !== "string") {
        throw new Error("Invalid directory response");
      }
      pane._path = data.path;
      pane._root = data.root || "";
      if (targetPath) {
        if (!["workspace", "data", "tmp"].includes(data.root_id) || typeof data.selected !== "string") {
          throw new Error("Invalid file response");
        }
        pane._rootId = data.root_id;
        pane._rootSelect.value = data.root_id;
        selection = data.selected;
        // Keep direct targets resolvable after refresh or pane restoration,
        // even when the ordinary directory listing would omit this entry.
        targetPath = pane._root.replace(/\/$/, "") + "/" + selection;
      }
      paintLocation();
      pane._list.replaceChildren();
      for (const entry of data.entries) {
        const directory = entry.kind === "directory";
        const appearance = fileAppearance(entry);
        const row = fileButton("", (directory ? "Open folder " : "Preview file ") + entry.name, () => {
          if (directory) navigate(entry.path);
          else selectFile(entry, entry.path === selection ? targetPath : null);
        });
        row.className = "filesystem-entry";
        row.dataset.path = entry.path;
        row.dataset.type = appearance.type;
        const glyph = fileElement("span", "filesystem-glyph", appearance.icon);
        glyph.setAttribute("aria-hidden", "true");
        const name = fileElement("span", "filesystem-name", entry.name);
        const label = fileElement("span", "filesystem-type", appearance.label);
        const info = fileElement("span", "filesystem-entry-info");
        info.append(name, label);
        const detail = fileElement("span", "filesystem-size", directory ? "›" : fileSize(entry.size));
        if (directory) detail.setAttribute("aria-hidden", "true");
        row.append(glyph, info, detail);
        pane._list.append(row);
      }
      if (!data.entries.length) {
        pane._list.append(fileElement("div", "filesystem-message", "This directory is empty."));
      }
      if (data.truncated) {
        pane._list.append(fileElement("div", "filesystem-message", "Only the first entries are shown. Open a subfolder to browse further."));
      }
      pane._status.textContent = data.entries.length + " entries" + (data.truncated ? " (listing limited)" : "");
      const restored = data.entries.find((entry) => entry.kind === "file" && entry.path === selection);
      if (restored) selectFile(restored, targetPath);
      persist();
    } catch (_) {
      if (token === pane._requestToken && !pane._closed) {
        listError("Could not load files. Check the connection and try again.", path, selection, targetPath);
      }
    } finally {
      if (token === pane._requestToken && !pane._closed) {
        pane._list.removeAttribute("aria-busy");
        if (retainFocus && document.activeElement === pane._list) {
          (pane._list.querySelector("button") || pane._rootSelect).focus({ preventScroll: true });
        }
      }
    }
  };

  // Re-opening from the same conversation retains the directory and refreshes
  // disk state. Another conversation always starts at its own workspace root.
  pane.showFiles = (ctx, targetPath = null) => {
    if (!ctx || !ctx.wsId || pane._closed) return;
    const same = pane._ctx && pane._ctx.wsId === ctx.wsId && pane._ctx.base === (ctx.base || "");
    const path = same ? pane._path : "";
    const selection = same ? pane._selected : null;
    const selectedTarget = same ? pane._selectedTarget : null;
    pane._ctx = { base: ctx.base || "", wsId: ctx.wsId };
    if (!same) {
      pane._root = "";
      pane._rootId = "workspace";
      pane._rootSelect.value = "workspace";
    }
    navigate(path, selection, targetPath || selectedTarget);
    pane._rootSelect.focus({ preventScroll: true });
  };

  pane.onMount = function () {
    const root = fileElement("div", "filesystem-root");
    const toolbar = fileElement("div", "filesystem-toolbar");
    const controls = fileElement("div", "filesystem-controls");
    const rootChoice = fileElement("label", "filesystem-root-choice");
    rootChoice.append(fileElement("span", "", "Location"));
    pane._rootSelect = fileElement("select", "filesystem-root-select");
    pane._rootSelect.setAttribute("aria-label", "Filesystem root");
    for (const [id, name] of [["workspace", "Workspace"], ["data", "/data"], ["tmp", "/tmp"]]) {
      const option = fileElement("option", "", name);
      option.value = id;
      pane._rootSelect.append(option);
    }
    pane._rootSelect.value = pane._rootId;
    pane._rootSelect.addEventListener("change", () => {
      const id = pane._rootSelect.value;
      if (!["workspace", "data", "tmp"].includes(id)) return;
      pane._rootId = id;
      pane._root = "";
      navigate("");
    });
    rootChoice.append(pane._rootSelect);
    const navigation = fileElement("div", "filesystem-navigation");
    pane._up = fileButton("↑", "Go to parent directory", () =>
      navigate(pane._path.split("/").slice(0, -1).join("/")),
    );
    pane._up.disabled = true;
    pane._crumbs = fileElement("nav", "filesystem-breadcrumbs");
    pane._crumbs.setAttribute("aria-label", "File location");
    pane._crumbs.addEventListener("scroll", markClipped, { passive: true });
    const refresh = fileButton("↻ Refresh", "Refresh files", () =>
      navigate(pane._path, pane._selected, pane._selectedTarget),
    );
    controls.append(rootChoice, refresh);
    const address = fileElement("div", "filesystem-address");
    // The node prefix stays outside the scrolling breadcrumbs so a deep path
    // cannot push it out of view.
    pane._node = fileElement("span", "filesystem-node");
    pane._node.hidden = true;
    pane._node.setAttribute("aria-hidden", "true");
    pane._nodeName = fileElement("span", "filesystem-node-name");
    pane._node.append(pane._nodeName, fileElement("span", "filesystem-separator", ":"));
    address.append(fileElement("span", "filesystem-path-label", "Path"), pane._node, pane._crumbs);
    navigation.append(pane._up, address);
    toolbar.append(controls, navigation);
    const body = fileElement("div", "filesystem-body");
    const browser = fileElement("div", "filesystem-browser");
    const directory = fileElement("div", "filesystem-directory");
    const directoryIcon = fileElement("span", "filesystem-directory-icon", "📂");
    directoryIcon.setAttribute("aria-hidden", "true");
    pane._directoryName = fileElement("span", "filesystem-directory-name", "Workspace");
    directory.append(directoryIcon, pane._directoryName);
    pane._list = fileElement("div", "filesystem-list");
    pane._list.setAttribute("role", "region");
    pane._list.setAttribute("aria-label", "Files and folders");
    pane._list.tabIndex = -1;
    pane._status = fileElement("div", "filesystem-status");
    pane._status.setAttribute("role", "status");
    browser.append(directory, pane._list, pane._status);
    const viewer = fileElement("div", "filesystem-viewer");
    viewer.setAttribute("role", "region");
    viewer.setAttribute("aria-label", "Selected file");
    pane._preview = createPreviewPane(null, {
      contentUrl: (ctx, descriptor, endpoint = "preview") => filesUrl(ctx, endpoint, descriptor.file_path),
      history: false,
      autoRetry: false,
      responseErrors: true,
      downloadOnPreviewError: true,
      copy: true,
      emptyText: "Choose a file to preview, copy, or download.",
    });
    pane._preview.el = viewer;
    pane._preview.bodyEl = viewer;
    pane._preview.onMount();
    body.append(browser, viewer);
    root.append(toolbar, body);
    this.bodyEl.append(root);
    if (extra && extra.ctx && extra.ctx.wsId) {
      pane._ctx = { base: extra.ctx.base || "", wsId: extra.ctx.wsId };
      pane._rootId = ["workspace", "data", "tmp"].includes(extra.rootId) ? extra.rootId : "workspace";
      pane._rootSelect.value = pane._rootId;
      navigate(extra.path || "", extra.selected || null, extra.selectedTarget || null);
    } else {
      pane._list.append(fileElement("div", "filesystem-message", "Open Files from a conversation to browse its workspace."));
    }
  };

  pane.onClose = () => {
    pane._closed = true;
    pane._requestToken++;
    if (pane._preview) pane._preview.onClose();
  };
  return pane;
}
