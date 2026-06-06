// conversation.js — shared conversational-pane substrate (step 5e).
//
// Deduplicated helpers used by BOTH the interactive pane
// (shared_static/interactive.js) and the coordinator pane
// (console/static/coordinator/coordinator.js).  Both panes are ES modules
// (interactive since 5a, the coordinator since 5e.0), so this is a plain ESM
// module they import directly — interactive via `./conversation.js`, the
// coordinator via the absolute `/shared/conversation.js`.  No window bridge:
// nothing classic consumes these (the standalone ui/static app.js drives the
// interactive Pane, which imports them on its behalf).
//
// House style holds: programmatic DOM (createElement / textContent / append),
// never innerHTML.  Builders return a detached element and let the caller append
// + scroll, so they stay pane- and transport-agnostic.

// ANSI / CSI escape stripper — a tool that emits control sequences (bash through
// MCP, or a child node) must land as readable text in the result block.
// Null-safe: a non-string argument coerces to "" rather than throwing.
export function stripAnsi(s) {
  return String(s == null ? "" : s).replace(
    // eslint-disable-next-line no-control-regex
    /\x1b(?:\[[0-9;?]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)?|[()#][A-Za-z0-9]|.)/g,
    "",
  );
}

// Structured `.msg.watch-result` card for a `watch_triggered` operator-context
// system turn — command-preview header + shell-output body + poll-counter footer.
// `content` is the formatted turn body; `meta` carries the structured fields
// (watch_name / command / output / poll_count / max_polls / is_final) delivered
// live on the `system_turn` SSE event and on the `/history` projection.  All text
// goes through textContent so shell output containing angle brackets / scripts /
// steering bytes renders inertly.  The caller appends + scrolls.
export function buildWatchResultCard(meta, content) {
  const el = document.createElement("div");
  el.className = "msg watch-result operator-context";
  el.setAttribute("role", "article");
  el.setAttribute("data-ts-role", "watch");
  el.setAttribute("aria-label", "watch");
  const header = document.createElement("div");
  header.className = "msg-watch-header";
  header.textContent =
    "watch" + (meta.watch_name ? " · " + String(meta.watch_name) : "");
  el.appendChild(header);
  if (meta.command) {
    const cmd = document.createElement("div");
    cmd.className = "msg-watch-cmd";
    cmd.textContent = "$ " + String(meta.command);
    el.appendChild(cmd);
  }
  const body = document.createElement("pre");
  body.className = "msg-watch-body";
  // Prefer the structured `output` (raw shell output alone) so the body doesn't
  // re-print the header / `$ command` lines the chrome already shows; fall back to
  // the full turn `content` for legacy turns predating the `output` meta field
  // (migration 060 is additive — no backfill).
  body.textContent = meta.output != null ? String(meta.output) : content || "";
  el.appendChild(body);
  if (meta.poll_count != null && meta.max_polls != null) {
    const footer = document.createElement("div");
    footer.className = "msg-watch-footer";
    const finalSuffix = meta.is_final ? " · final" : "";
    footer.textContent =
      "poll " +
      String(meta.poll_count) +
      "/" +
      String(meta.max_polls) +
      finalSuffix;
    el.appendChild(footer);
  }
  return el;
}

// Thin `.msg.user.system-nudge` marker — the visible-but-subtle anchor a
// wake-driven empty user turn renders, so the operator-context `system` turns
// that follow it land in the right place.  The caller handles empty-state
// removal + append.
export function buildSystemNudgeMarker() {
  const el = document.createElement("div");
  el.className = "msg user system-nudge";
  el.setAttribute("data-source", "system_nudge");
  el.setAttribute("aria-label", "system nudge");
  el.textContent = "system nudge";
  return el;
}

// Canonical risk-level vocabulary shared by both panes.  RISK_LEVELS is ordinal
// (index == severity rank); the aliases cover emitters that abbreviate.  An
// unknown / unrecognized level normalizes to "medium": not "low" (an unlabelled
// risk must not render benign) and not "high" (medium is the neutral default
// both panes already displayed; the coordinator's old rank used "high", which
// step 5e.1b brings to "medium" per the unify decision).
const RISK_LEVELS = ["low", "medium", "high", "critical"];
const RISK_ALIASES = { med: "medium", crit: "critical" };

export function normalizeRiskLevel(raw) {
  let s = String(raw == null ? "" : raw)
    .trim()
    .toLowerCase();
  s = RISK_ALIASES[s] || s;
  return RISK_LEVELS.indexOf(s) >= 0 ? s : "medium";
}

// Ordinal rank (low=0 .. critical=3) via the canonical normalize, so an alias or
// unknown value ranks consistently with how it displays.  Unknown -> medium (1).
export function riskRank(raw) {
  return RISK_LEVELS.indexOf(normalizeRiskLevel(raw));
}

// Pick the item carrying the highest risk_level (judge verdict preferred over
// heuristic) so a low-risk item[0] can't visually mask a higher-risk item[2].
// An item with NO verdict ranks below "low" (-1) so it never wins; a verdict
// with an unknown level ranks "medium" via riskRank.
export function maxSeverityItem(items) {
  function rank(it) {
    const v = it && (it.judge_verdict || it.heuristic_verdict);
    return v ? riskRank(v.risk_level) : -1;
  }
  let best = items[0];
  let bestRank = rank(best);
  for (let i = 1; i < items.length; i += 1) {
    const r = rank(items[i]);
    if (r > bestRank) {
      best = items[i];
      bestRank = r;
    }
  }
  return best;
}
