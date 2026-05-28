// Shared history normaliser — converts the raw `reconstruct_messages`
// shape returned by `GET /v1/api/workstreams/{ws_id}/history` into the
// projected wire shape that `_build_history` (turnstone/server.py) emits
// and the interactive `replayHistory` renderer consumes.
//
// Why this exists: the REST `/history` endpoint returns provider-native
// OpenAI-shaped messages — nested `tool_calls[].function.{name,arguments}`,
// side-channel `_source` / `_reminders` / `_attachments_meta`, multipart
// user content, and NO derived `denied` / `is_error` / `pending` flags.
// This normaliser does that projection client-side so the REST-fetched
// history renders identically to the legacy SSE replay.
//
// LIVE PROJECTION REFERENCE: the production REST `/history` shape comes
// from `reconstruct_messages` + `decorate_history_messages` /
// `extract_reasoning_for_history` (turnstone/core/history_decoration.py) —
// THAT is the shape this mirror must track. `server.py::_build_history` is
// the historical SSE projection that originally defined the target wire
// shape, but it has NO production callers post-convergence (test-only
// reference impl) — do not "keep lockstep" by editing it alone.
// (Transitional bridge: a planned server-side wire-shape unification will
// fold this projection back into the server so both kinds consume it.)
//
// `reasoning` and per-tool-call `verdict` / `output_assessment` are already
// projected top-level by the REST decoration and pass through unchanged.
// `advisories` pass through for STRING tool content (decorate strips the
// `<tool_output>` envelope server-side); LIST-content tool results are a
// KNOWN GAP — their envelope is not stripped here and a carried advisory is
// dropped (uncommon: image/structured MCP result + concurrent interjection;
// closed by the planned server-side unification).
//
// Pure + DOM-free so it can be unit-tested under node
// (tests/test_app_js.py::test_normalize_history_*).

const _WATCH_REMINDER_OPTIONAL_KEYS = [
  "watch_name",
  "command",
  "poll_count",
  "max_polls",
  "is_final",
];

const _TOOL_ERROR_PREFIXES = [
  "Error",
  "Command timed out",
  "Search timed out",
  "Unknown tool:",
  "JSON parse error:",
  "MCP prompt timed out",
  "MCP prompt error",
];

function normalizeHistoryMessages(messages) {
  if (!Array.isArray(messages)) return [];

  // Pre-scan: which tool_call_ids have a result message? An assistant
  // tool_call with no result is an orphan → its turn is "pending"
  // (awaiting approval on a live reconnect, or interrupted) and must not
  // render a fake "approved" badge. Mirrors `_build_history`'s
  // has_pending_approval handling for the realistic cases.
  const resultedCallIds = new Set();
  for (const m of messages) {
    if (m && m.role === "tool" && m.tool_call_id) {
      resultedCallIds.add(String(m.tool_call_id));
    }
  }

  const out = [];
  for (const msg of messages) {
    if (!msg || typeof msg !== "object") continue;
    const role = msg.role;
    let content = msg.content;
    let attachments = null;

    // (1) Collapse multipart user content (text + image_url + document
    //     parts) to a plain string + a derived attachment list.
    if (role === "user" && Array.isArray(content)) {
      const textParts = [];
      const meta = [];
      for (const part of content) {
        if (!part || typeof part !== "object") continue;
        if (part.type === "text") {
          textParts.push(String(part.text || ""));
        } else if (part.type === "image_url") {
          meta.push({ kind: "image", filename: "", mime_type: "" });
        } else if (part.type === "document") {
          const d = part.document || {};
          meta.push({
            kind: "text",
            filename: String(d.name || ""),
            mime_type: String(d.media_type || ""),
          });
        }
      }
      content = textParts.join("\n");
      if (meta.length) attachments = meta;
    }

    // (2) The authoritative `_attachments_meta` side-channel wins when
    //     present (carries image filenames the image_url part can't).
    const sideMeta = msg._attachments_meta;
    if (Array.isArray(sideMeta) && sideMeta.length) {
      attachments = sideMeta
        .filter((x) => x && typeof x === "object")
        .map((x) => ({
          kind: String(x.kind || ""),
          filename: String(x.filename || ""),
          mime_type: String(x.mime_type || ""),
        }));
    }

    const entry = { role: role, content: content };
    if (attachments && attachments.length) entry.attachments = attachments;

    // (3) `_source` side-channel → top-level `source` (drives the
    //     `.msg.user.system-nudge` marker).
    if (msg._source) entry.source = String(msg._source);

    // (4) `_reminders` side-channel → top-level `reminders`, filtered +
    //     key-projected (mirrors `_build_history`'s reminder filter).
    if (Array.isArray(msg._reminders)) {
      const clean = [];
      for (const r of msg._reminders) {
        if (!r || typeof r !== "object") continue;
        const rtype = String(r.type || "");
        const rtext = String(r.text || "");
        if (!rtype && !rtext) continue;
        const c = { type: rtype, text: rtext };
        for (const k of _WATCH_REMINDER_OPTIONAL_KEYS) {
          if (k in r) c[k] = r[k];
        }
        clean.push(c);
      }
      if (clean.length) entry.reminders = clean;
    }

    // (5) Reasoning is already projected top-level by the REST
    //     decoration — pass it through.
    if (msg.reasoning) entry.reasoning = msg.reasoning;

    // (6) Flatten OpenAI-nested tool_calls `{id, function:{name,
    //     arguments}}` → `{id, name, arguments}` that `replayHistory`
    //     reads, carrying the decoration (`verdict` / `output_assessment`,
    //     which `decorate_tool_call` placed top-level on the call).
    if (Array.isArray(msg.tool_calls) && msg.tool_calls.length) {
      const tcs = [];
      for (const tc of msg.tool_calls) {
        if (!tc || typeof tc !== "object") continue;
        const fn = tc.function || {};
        const id = tc.id || "";
        const flat = {
          id: id,
          name: fn.name || tc.name || "",
          arguments:
            fn.arguments != null
              ? fn.arguments
              : tc.arguments != null
                ? tc.arguments
                : "",
        };
        if (tc.verdict) flat.verdict = tc.verdict;
        if (tc.output_assessment) flat.output_assessment = tc.output_assessment;
        tcs.push(flat);
      }
      entry.tool_calls = tcs;
    }

    // (7) Tool results: carry `tool_call_id` + `advisories` (already
    //     top-level on the REST shape), coerce list content to text, and
    //     derive `denied` / `is_error` from the content prefix — the REST
    //     path does NOT pre-set these (mirrors `_build_history`).
    if (role === "tool") {
      if (msg.tool_call_id) entry.tool_call_id = String(msg.tool_call_id);
      if (Array.isArray(msg.advisories) && msg.advisories.length) {
        entry.advisories = msg.advisories;
      }
      if (Array.isArray(content)) {
        content = content
          .filter((p) => p && p.type === "text")
          .map((p) => String(p.text || ""))
          .join("\n");
        entry.content = content;
      }
      const c = typeof content === "string" ? content : "";
      if (c.startsWith("Denied by user") || c.startsWith("Blocked")) {
        entry.denied = true;
      }
      if (msg.is_error || _TOOL_ERROR_PREFIXES.some((p) => c.startsWith(p))) {
        entry.is_error = true;
      }
    }

    out.push(entry);
  }

  // (8) Propagate denial from a tool result to its parent assistant turn
  //     so the tool block renders the denied (not approved) badge.
  //     Mirrors `_build_history`'s last-assistant propagation.
  let lastAssistantIdx = -1;
  for (let i = 0; i < out.length; i++) {
    if (out[i].tool_calls) {
      lastAssistantIdx = i;
    } else if (
      out[i].role === "tool" &&
      out[i].denied &&
      lastAssistantIdx >= 0
    ) {
      out[lastAssistantIdx].denied = true;
    }
  }

  // (9) Mark `pending` ONLY on the LAST assistant tool-call turn, and only
  //     when it has an orphan (a tool_call with no result in the loaded
  //     window) — the proxy for "awaiting approval". Mirrors
  //     `_build_history`'s reversed single-turn pending. A mid-conversation
  //     cancelled/interrupted tool call is also an orphan but must still
  //     render its tool block (not vanish), so it is NOT marked pending.
  for (let i = out.length - 1; i >= 0; i--) {
    const e = out[i];
    if (e.tool_calls && e.tool_calls.length) {
      const hasOrphan = e.tool_calls.some(
        (tc) => tc.id && !resultedCallIds.has(String(tc.id)),
      );
      if (hasOrphan) e.pending = true;
      break;
    }
  }

  return out;
}

// Dual-mode: a browser global (loaded via <script>) AND a CommonJS export
// so node-based unit tests can require it without a DOM.
if (typeof module !== "undefined" && module.exports) {
  module.exports = { normalizeHistoryMessages };
}
