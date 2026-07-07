// preview.js — the preview pane: rich rendering of tool-selected content
// (a fetched web page, PDF, image, data table, text/markdown document) in a
// dedicated pane beside the conversation.
//
// One singleton pane per shell (PaneManager type "preview").  Content arrives
// as a DESCRIPTOR — the structured object the open_preview tool folds onto
// its tool turn ({kind, title, source, attachment_id, content_type, size}) —
// plus a transport context {base, wsId} from the ORIGINATING conversation
// pane, so blob fetches route through the same node proxy that session
// streams from.  Bytes are never inlined into events; everything loads from
// the per-workstream preview route on cookie auth (the house pattern:
// plain same-origin src URLs, no blob:/createObjectURL).
//
// Developer-tool posture: keyboard-operable (←/→ walk the session's preview
// history), sandboxed (web content renders in a fully sandboxed iframe on top
// of the route's own CSP), and coexistent — it opens BESIDE the conversation
// via openPaneBeside, never replacing it.
//
// House style: ES module, programmatic DOM (createElement / textContent /
// append), NO innerHTML — the one HTML sink is setSafeHtml over
// renderMarkdown, the sanctioned renderer.js lane.  All queries root-scoped.

import { ShellPane } from "./pane.js";
import { authFetch } from "./auth.js";
import { redactCredentials } from "./redact_credentials.js";
import { renderMarkdown } from "./renderer.js";
import { setSafeHtml } from "./utils.js";

// How many viewed descriptors the ←/→ history keeps.  Session-scoped and
// in-memory; only the CURRENT one is persisted for reload (pane.meta).
const HISTORY_CAP = 20;
// Table renderer row cap — past this the DOM cost buys no decision value;
// the notice row reports what was withheld.
const TABLE_ROW_CAP = 5000;
// Automatic reloads for the persist race: the live descriptor beats the
// tool-turn fold that commits its blob whenever a parallel SIBLING tool is
// still running — the fold waits on the whole batch, so the gap is that
// sibling's remaining runtime, not milliseconds.  Backoff doubles from
// RETRY_BASE_MS across MAX_AUTO_RETRIES attempts (0.9s, 1.8s, 3.6s, 7.2s —
// ~13.5s covered) before the manual Retry card takes over.
const RETRY_BASE_MS = 900;
const MAX_AUTO_RETRIES = 4;

function make(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

// The per-workstream serving route for a descriptor, through the originating
// pane's transport base ("" local, "/node/{id}" console-proxied).
function previewContentUrl(ctx, descriptor) {
  const base = (ctx && ctx.base) || "";
  const ws = (ctx && ctx.wsId) || "";
  return (
    base +
    "/v1/api/workstreams/" +
    encodeURIComponent(ws) +
    "/attachments/" +
    encodeURIComponent(descriptor.attachment_id || "") +
    "/preview"
  );
}

// ---------------------------------------------------------------------------
// Delimited-text parsing (table kind).  Minimal RFC-4180 state machine:
// quoted fields, "" escapes, \r\n and \n rows.  Returns rows of strings.
// ---------------------------------------------------------------------------
function parseDelimited(text, delim) {
  const rows = [];
  let row = [];
  let field = "";
  let quoted = false;
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (quoted) {
      if (ch === '"') {
        if (text[i + 1] === '"') {
          field += '"';
          i++;
        } else {
          quoted = false;
        }
      } else {
        field += ch;
      }
    } else if (ch === '"' && field === "") {
      quoted = true;
    } else if (ch === delim) {
      row.push(field);
      field = "";
    } else if (ch === "\n" || ch === "\r") {
      if (ch === "\r" && text[i + 1] === "\n") i++;
      row.push(field);
      field = "";
      rows.push(row);
      row = [];
    } else {
      field += ch;
    }
  }
  if (field !== "" || row.length) {
    row.push(field);
    rows.push(row);
  }
  // Drop a pure-empty trailing row (text ending in a newline).
  if (rows.length && rows[rows.length - 1].every((f) => f === "")) rows.pop();
  return rows;
}

// JSON payloads → (header, rows).  Array of objects: columns = key union in
// first-seen order.  Array of scalars/arrays: index-labelled columns.  A bare
// object: two-column key/value listing.
function tableFromJson(parsed) {
  if (Array.isArray(parsed)) {
    if (
      parsed.length &&
      parsed.every((r) => r && typeof r === "object" && !Array.isArray(r))
    ) {
      const cols = [];
      for (const r of parsed) {
        for (const k of Object.keys(r)) if (!cols.includes(k)) cols.push(k);
      }
      const rows = parsed.map((r) =>
        cols.map((c) => {
          const v = r[c];
          if (v == null) return "";
          return typeof v === "object" ? JSON.stringify(v) : String(v);
        }),
      );
      return { header: cols, rows };
    }
    const rows = parsed.map((r) =>
      Array.isArray(r)
        ? r.map((v) =>
            v == null
              ? ""
              : typeof v === "object"
                ? JSON.stringify(v)
                : String(v),
          )
        : [
            typeof r === "object" && r != null
              ? JSON.stringify(r)
              : String(r ?? ""),
          ],
    );
    const width = rows.reduce((w, r) => Math.max(w, r.length), 0);
    const header = [];
    for (let i = 0; i < width; i++) header.push(String(i + 1));
    return { header, rows };
  }
  if (parsed && typeof parsed === "object") {
    return {
      header: ["key", "value"],
      rows: Object.entries(parsed).map(([k, v]) => [
        k,
        typeof v === "object" && v != null
          ? JSON.stringify(v)
          : String(v ?? ""),
      ]),
    };
  }
  return { header: ["value"], rows: [[String(parsed)]] };
}

// Numeric-aware comparator for column sorts: numbers order numerically,
// everything else falls back to locale string compare.
function compareCells(a, b) {
  const na = parseFloat(a);
  const nb = parseFloat(b);
  const bothNumeric =
    !Number.isNaN(na) &&
    !Number.isNaN(nb) &&
    a.trim() !== "" &&
    b.trim() !== "";
  if (bothNumeric && na !== nb) return na < nb ? -1 : 1;
  if (bothNumeric) return 0;
  return a.localeCompare(b);
}

// ---------------------------------------------------------------------------
// createPreviewPane — the PaneManager factory body.
//   hostApi: { persistMeta(meta), setTitle(text) } — the two pm-owned verbs
//   the pane needs, injected by the shell so the pane owns no manager ref.
//   extra:   the rehydrate hint ({descriptor, ctx}) persisted via pane.meta.
// ---------------------------------------------------------------------------
export function createPreviewPane(extra, hostApi) {
  const api = hostApi || {};
  const pane = new ShellPane({
    type: "preview",
    title: "Preview",
    glyph: "▤",
  });

  // Viewed-descriptor history (session-scoped): entries are {descriptor, ctx}.
  pane._stack = [];
  pane._idx = -1;
  pane._loadToken = 0;

  const shortTitle = (d) => {
    const t = redactCredentials(d.title || d.source || "preview");
    return t.length > 40 ? t.slice(0, 39) + "…" : t;
  };

  const setNavState = () => {
    pane._backBtn.disabled = pane._idx <= 0;
    pane._fwdBtn.disabled = pane._idx >= pane._stack.length - 1;
  };

  const renderEmpty = () => {
    pane._contentEl.replaceChildren(
      make(
        "div",
        "preview-empty",
        "Nothing previewed yet — ask the assistant to show a page, file, image, or data table.",
      ),
    );
  };

  const renderError = (message, entry) => {
    const wrap = make("div", "preview-error");
    wrap.append(make("div", "preview-error-msg", message));
    const retry = make("button", "preview-retry", "Retry");
    retry.type = "button";
    // A manual retry is a single deliberate attempt — no silent backoff run.
    retry.addEventListener("click", () => renderEntry(entry, MAX_AUTO_RETRIES));
    wrap.append(retry);
    pane._contentEl.replaceChildren(wrap);
  };

  const renderNote = (text) => make("div", "preview-note", text);

  // ----- per-kind renderers -------------------------------------------------

  const renderWeb = (url) => {
    const frame = make("iframe", "preview-frame");
    // Full lockdown: no scripts, no same-origin, no forms/popups.  The route
    // additionally serves the document under its own CSP sandbox — neither
    // layer alone is load-bearing.
    frame.setAttribute("sandbox", "");
    frame.setAttribute("referrerpolicy", "no-referrer");
    frame.title = "Web page preview";
    frame.src = url;
    pane._contentEl.replaceChildren(frame);
  };

  const renderPdf = (url, d) => {
    const frame = make("iframe", "preview-frame");
    // No sandbox attribute: Chromium's built-in PDF viewer refuses to paint
    // in a sandboxed context, and the response is inert media (the serving
    // route omits CSP for application/pdf for the same reason).
    frame.title = (d.title || "Document") + " (PDF preview)";
    frame.src = url;
    pane._contentEl.replaceChildren(frame);
  };

  const renderImage = (url, d) => {
    const holder = make("div", "preview-imgwrap");
    const img = make("img", "preview-img");
    img.alt = d.title || "Image preview";
    img.decoding = "async";
    img.src = url;
    holder.append(img);
    pane._contentEl.replaceChildren(holder);
  };

  const renderText = (text) => {
    const pre = make("pre", "preview-pre");
    pre.textContent = text;
    pane._contentEl.replaceChildren(pre);
  };

  const renderMarkdownDoc = (text) => {
    const doc = make("div", "preview-markdown");
    // The one sanctioned HTML lane: renderer.js output through setSafeHtml.
    setSafeHtml(doc, renderMarkdown(text));
    pane._contentEl.replaceChildren(doc);
  };

  const renderTable = (text, d) => {
    let header;
    let rows;
    const bare = String(d.content_type || "")
      .split(";")[0]
      .trim()
      .toLowerCase();
    if (bare === "application/json") {
      let parsed;
      try {
        parsed = JSON.parse(text);
      } catch (e) {
        renderText(text); // not actually JSON — degrade to plain text
        return;
      }
      ({ header, rows } = tableFromJson(parsed));
    } else {
      const delim = bare === "text/tab-separated-values" ? "\t" : ",";
      const all = parseDelimited(text, delim);
      if (!all.length) {
        renderText(text);
        return;
      }
      header = all[0];
      rows = all.slice(1);
      // Ragged files: a short first row must not silently hide trailing
      // columns of later rows — pad the header out to the widest row so
      // every parsed cell renders (and sorts).
      const width = rows.reduce((w, r) => Math.max(w, r.length), header.length);
      while (header.length < width) header.push(String(header.length + 1));
    }

    const wrap = make("div", "preview-tablewrap");
    const table = make("table", "preview-table");
    const thead = document.createElement("thead");
    const headRow = document.createElement("tr");
    const state = { col: -1, dir: 1, rows };

    const tbody = document.createElement("tbody");
    const renderBody = () => {
      tbody.replaceChildren();
      const shown = state.rows.slice(0, TABLE_ROW_CAP);
      for (const r of shown) {
        const tr = document.createElement("tr");
        for (let c = 0; c < header.length; c++) {
          const td = document.createElement("td");
          td.textContent = r[c] != null ? r[c] : "";
          tr.append(td);
        }
        tbody.append(tr);
      }
    };

    header.forEach((h, ci) => {
      const th = document.createElement("th");
      th.scope = "col";
      const btn = make("button", "preview-th", h);
      btn.type = "button";
      btn.title = "Sort by " + h;
      btn.addEventListener("click", () => {
        state.dir = state.col === ci ? -state.dir : 1;
        state.col = ci;
        state.rows = state.rows
          .slice()
          .sort((a, b) => state.dir * compareCells(a[ci] || "", b[ci] || ""));
        for (const other of headRow.querySelectorAll("th")) {
          other.removeAttribute("aria-sort");
        }
        th.setAttribute(
          "aria-sort",
          state.dir > 0 ? "ascending" : "descending",
        );
        renderBody();
      });
      th.append(btn);
      headRow.append(th);
    });
    thead.append(headRow);
    table.append(thead, tbody);
    renderBody();
    wrap.append(table);
    pane._contentEl.replaceChildren(wrap);
    if (rows.length > TABLE_ROW_CAP) {
      pane._contentEl.append(
        renderNote(
          "Showing " +
            TABLE_ROW_CAP.toLocaleString() +
            " of " +
            rows.length.toLocaleString() +
            " rows",
        ),
      );
    }
  };

  // ----- load + dispatch ----------------------------------------------------

  // attempt 0..MAX_AUTO_RETRIES-1 = silent backoff retries for the persist
  // race; past that (including the manual Retry button, which passes
  // MAX_AUTO_RETRIES) failures land on the error card immediately.
  const renderEntry = (entry, attempt) => {
    const token = ++pane._loadToken;
    const d = entry.descriptor;
    const url = previewContentUrl(entry.ctx, d);
    const failed = (why) => {
      if (token !== pane._loadToken) return;
      if (attempt < MAX_AUTO_RETRIES) {
        setTimeout(
          () => {
            if (token === pane._loadToken) renderEntry(entry, attempt + 1);
          },
          RETRY_BASE_MS * Math.pow(2, attempt),
        );
        return;
      }
      renderError(why, entry);
    };

    pane._kindEl.textContent = d.kind || "";
    // Display strings can be raw URLs — never print embedded credentials.
    // The ext link below keeps the RAW url (it must actually navigate).
    pane._titleEl.textContent = redactCredentials(d.title || d.source || "");
    pane._titleEl.title = redactCredentials(d.source || "");
    const isWeb = d.kind === "web" && /^https?:\/\//.test(d.source || "");
    pane._extLink.hidden = !isWeb;
    if (isWeb) pane._extLink.href = d.source;
    api.setTitle && api.setTitle("Preview · " + shortTitle(d));

    pane._contentEl.replaceChildren(make("div", "preview-loading", "Loading…"));

    if (d.kind === "web" || d.kind === "pdf" || d.kind === "image") {
      // src-loaded kinds: preflight with authFetch so the persist race and
      // auth failures surface as a typed error card, not a broken frame.
      authFetch(url, { method: "HEAD" })
        .then((r) => {
          if (token !== pane._loadToken) return;
          if (!r.ok) {
            failed(
              r.status === 404
                ? "This preview's content isn't available yet."
                : "Could not load the preview (" + r.status + ").",
            );
            return;
          }
          if (d.kind === "web") renderWeb(url);
          else if (d.kind === "pdf") renderPdf(url, d);
          else renderImage(url, d);
        })
        .catch(() => failed("Could not load the preview."));
      return;
    }
    // text-family kinds render client-side from fetched text.
    authFetch(url)
      .then((r) => {
        if (token !== pane._loadToken) return null;
        if (!r.ok) {
          failed(
            r.status === 404
              ? "This preview's content isn't available yet."
              : "Could not load the preview (" + r.status + ").",
          );
          return null;
        }
        return r.text();
      })
      .then((text) => {
        if (text == null || token !== pane._loadToken) return;
        if (d.kind === "table") renderTable(text, d);
        else if (d.kind === "markdown") renderMarkdownDoc(text);
        else renderText(text);
      })
      .catch(() => failed("Could not load the preview."));
  };

  const showAt = (idx) => {
    if (idx < 0 || idx >= pane._stack.length) return;
    pane._idx = idx;
    setNavState();
    const entry = pane._stack[idx];
    renderEntry(entry, 0);
    // Persist ONLY the current view — reload restores it via the factory's
    // rehydrate hint (the meta shape is exactly the entry: serializable).
    api.persistMeta &&
      api.persistMeta({ descriptor: entry.descriptor, ctx: entry.ctx });
  };

  /** Public API: show a descriptor (new gesture or chip re-open). */
  pane.showPreview = (descriptor, ctx) => {
    if (!descriptor || !descriptor.attachment_id) return;
    const top = pane._stack[pane._idx];
    if (
      top &&
      top.descriptor.attachment_id === descriptor.attachment_id &&
      top.descriptor.kind === descriptor.kind
    ) {
      // Same content re-requested — re-render in place (retry semantics),
      // don't grow the history with duplicates.
      showAt(pane._idx);
      return;
    }
    // A new view truncates any forward history (browser-history semantics).
    pane._stack = pane._stack.slice(0, pane._idx + 1);
    pane._stack.push({ descriptor, ctx: ctx || null });
    if (pane._stack.length > HISTORY_CAP) pane._stack.shift();
    showAt(pane._stack.length - 1);
  };

  pane.onMount = function () {
    const root = make("div", "preview-root");

    const bar = make("div", "preview-bar");
    const back = make("button", "preview-nav", "◀");
    back.type = "button";
    back.title = "Previous preview (←)";
    back.setAttribute("aria-label", "Previous preview");
    back.addEventListener("click", () => showAt(pane._idx - 1));
    const fwd = make("button", "preview-nav", "▶");
    fwd.type = "button";
    fwd.title = "Next preview (→)";
    fwd.setAttribute("aria-label", "Next preview");
    fwd.addEventListener("click", () => showAt(pane._idx + 1));
    const kind = make("span", "preview-kindchip", "");
    const title = make("span", "preview-titletext", "");
    const ext = make("a", "preview-ext", "Open in browser ↗");
    ext.target = "_blank";
    ext.rel = "noopener noreferrer";
    ext.hidden = true;
    bar.append(back, fwd, kind, title, ext);

    const content = make("div", "preview-content");

    pane._backBtn = back;
    pane._fwdBtn = fwd;
    pane._kindEl = kind;
    pane._titleEl = title;
    pane._extLink = ext;
    pane._contentEl = content;

    root.append(bar, content);
    this.bodyEl.append(root);

    // ←/→ walk the preview history while the pane has focus.  The pane hosts
    // no text inputs; the guard keeps future in-pane fields (and the header
    // link) from losing their native arrow behaviour.
    this.el.addEventListener("keydown", (e) => {
      if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
      const t = e.target;
      if (
        t &&
        (t.tagName === "INPUT" ||
          t.tagName === "TEXTAREA" ||
          t.isContentEditable)
      )
        return;
      e.preventDefault();
      showAt(pane._idx + (e.key === "ArrowLeft" ? -1 : 1));
    });

    setNavState();
    // Rehydrate: a reload restores the LAST viewed preview from pane.meta.
    if (extra && extra.descriptor) {
      pane.showPreview(extra.descriptor, extra.ctx || null);
    } else {
      renderEmpty();
    }
  };

  pane.onClose = function () {
    pane._loadToken++; // orphan any in-flight loads
    pane._stack = [];
    pane._idx = -1;
  };

  return pane;
}
