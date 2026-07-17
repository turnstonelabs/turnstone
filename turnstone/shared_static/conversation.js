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

import { redactCredentials } from "./redact_credentials.js";

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

// Compaction result card — the settled state of a context compaction.  Renders
// from BOTH sources of the same fact: the live `compaction` end event and the
// `/history` projection of the persisted marker row (role="system",
// source="compaction"), so a reload paints the identical card.  `meta` carries
// the structured fields ({before_tokens, after_tokens, trigger}); `summary` is
// the produced summary text, offered behind a <details> fold rather than
// inline — it exists for provenance, not for re-reading every visit.
export function buildCompactionCard(meta, summary) {
  const m = meta && typeof meta === "object" ? meta : {};
  const el = document.createElement("div");
  el.className = "msg compaction-card";
  el.setAttribute("role", "article");
  el.setAttribute("data-ts-role", "compaction");
  el.setAttribute("aria-label", "context compacted");
  const header = document.createElement("div");
  header.className = "msg-compaction-header";
  header.textContent =
    "context compacted" + (m.trigger === "auto" ? " · auto" : "");
  el.appendChild(header);
  const before = Number(m.before_tokens);
  const after = Number(m.after_tokens);
  if (Number.isFinite(before) && Number.isFinite(after) && before > 0) {
    const detail = document.createElement("div");
    detail.className = "msg-compaction-detail";
    detail.textContent =
      "~" +
      before.toLocaleString() +
      " → ~" +
      after.toLocaleString() +
      " tokens";
    el.appendChild(detail);
  }
  const text = String(summary == null ? "" : summary).trim();
  if (text) {
    const fold = document.createElement("details");
    fold.className = "msg-compaction-fold";
    const label = document.createElement("summary");
    label.textContent = "summary";
    fold.appendChild(label);
    const body = document.createElement("pre");
    body.className = "msg-compaction-body";
    body.textContent = text;
    fold.appendChild(body);
    el.appendChild(fold);
  }
  return el;
}

// In-progress compaction card — the transient affordance between the
// `compaction` start and end events.  Starts with an indeterminate bar
// (a single-batch summarization emits no progress events); the first
// part-k-of-N progress event flips it determinate via
// updateCompactionProgress.  The end event replaces the card with
// buildCompactionCard (or a failure notice).
export function buildCompactionProgressCard(isAuto) {
  const el = document.createElement("div");
  el.className = "msg compaction-card compaction-running";
  el.setAttribute("role", "status");
  el.setAttribute("data-ts-role", "compaction");
  el.setAttribute("aria-label", "compacting context");
  const header = document.createElement("div");
  header.className = "msg-compaction-header";
  header.textContent = "compacting context…" + (isAuto ? " · auto" : "");
  el.appendChild(header);
  const bar = document.createElement("div");
  bar.className = "msg-compaction-bar indeterminate";
  const fill = document.createElement("div");
  fill.className = "msg-compaction-bar-fill";
  bar.appendChild(fill);
  el.appendChild(bar);
  const note = document.createElement("div");
  note.className = "msg-compaction-note";
  note.textContent = "summarizing conversation…";
  el.appendChild(note);
  return el;
}

// Advance an in-progress compaction card from a `compaction` progress event.
// Depth 0 = summarizing transcript batches (determinate part k of N); deeper
// levels merge partial summaries — those update the note ONLY, never the bar:
// an over-window depth-0 batch subdivides mid-loop (emitting depth>0 events
// between depth-0 parts), so any width a merge event wrote would snap
// backwards when the outer loop resumed.  The depth-0 width itself is
// monotonic (max with the current fill) for the same reason.  A retry wait
// ({retry_in, error}) and the truncated-summary warning annotate the note
// without touching the bar.
export function updateCompactionProgress(el, evt) {
  const bar = el.querySelector(".msg-compaction-bar");
  const fill = el.querySelector(".msg-compaction-bar-fill");
  const note = el.querySelector(".msg-compaction-note");
  if (!bar || !fill || !note) return;
  if (evt.warning === "summary_truncated") {
    note.textContent = "summary was truncated — continuing…";
    return;
  }
  if (evt.retry_in != null) {
    note.textContent =
      "retrying in " +
      Math.round(Number(evt.retry_in)) +
      "s (" +
      String(evt.error || "error") +
      ")…";
    return;
  }
  const part = Number(evt.part);
  const total = Number(evt.total);
  if (!Number.isFinite(part) || !Number.isFinite(total) || total < 1) return;
  if (Number(evt.depth) > 0) {
    note.textContent = "merging summaries (" + part + " of " + total + ")…";
    return;
  }
  bar.classList.remove("indeterminate");
  // part is emitted BEFORE its batch summarizes — show the k-1 completed
  // fraction so the bar never claims work that hasn't happened yet.
  const pct = Math.max(0, Math.min(100, ((part - 1) / total) * 100));
  const current = parseFloat(fill.style.width) || 0;
  fill.style.width = Math.max(current, pct) + "%";
  note.textContent = "summarizing part " + part + " of " + total + "…";
}

// Shared compaction-lifecycle reducer — ONE state machine for the
// interactive pane and the coordinator viewer (each previously carried a
// hand-synced copy, and they drifted within their first diff).  `holder` is
// the pane's mutable `{card}` slot (nulled wherever the transcript DOM is
// wiped); `hooks` supplies the pane-specific seams:
//   container      — the transcript element to append into
//   renderedIds    — the pane's rendered-event-id Set (dedups the ok-end
//                    card against the /history-projected marker row)
//   onNotice(msg)  — render a non-error failure notice (info styling)
//   scroll(force)  — the pane's scroll-to-bottom
// reason="error" ends render NO notice here: the backend pairs them with a
// typed `error` event, which each pane's existing error handler styles red
// (and which feeds the node's error metrics) — emitting here too would show
// the message twice.
export function applyCompactionEvent(holder, evt, hooks) {
  // Lifecycle ownership: events carry the backend's compaction_id and the
  // holder remembers which compaction painted the live card, so a stale
  // event — a force-abandoned compaction retiring after a successor
  // started — can't animate or tear down the successor's card.  The id
  // gates only while a live card exists (with no card there is nothing to
  // protect), and a missing id on either side matches everything so
  // replays from older backends keep working.
  const owns =
    !holder.card ||
    holder.cid == null ||
    evt.compaction_id == null ||
    String(evt.compaction_id) === holder.cid;
  if (evt.phase === "start") {
    if (holder.card) holder.card.remove();
    holder.card = buildCompactionProgressCard(evt.trigger === "auto");
    holder.cid = evt.compaction_id != null ? String(evt.compaction_id) : null;
    hooks.container.appendChild(holder.card);
    hooks.scroll(true);
    return;
  }
  if (evt.phase === "progress") {
    if (!owns) return;
    // Defensive create: a fresh connect mid-compaction (dead replay
    // buffer) can see a progress event with no preceding start.  The
    // `false` is deliberate: progress events carry no trigger field, so
    // this card cannot know it should wear the "· auto" suffix —
    // cosmetic, and threading trigger through the summarize stack for it
    // is disproportionate.
    if (!holder.card) {
      holder.card = buildCompactionProgressCard(false);
      holder.cid = evt.compaction_id != null ? String(evt.compaction_id) : null;
      hooks.container.appendChild(holder.card);
    }
    updateCompactionProgress(holder.card, evt);
    hooks.scroll(false);
    return;
  }
  if (evt.phase === "end") {
    if (owns && holder.card) {
      holder.card.remove();
      holder.card = null;
      holder.cid = null;
    }
    if (evt.ok) {
      // The persisted marker row is stamped with THIS event's id, so
      // whichever of /history repaint or live/replayed event renders
      // first wins.  Rendered even for a non-owning end: a completed
      // compaction's result is real regardless of whose card is live.
      const eid = evt._event_id != null ? String(evt._event_id) : null;
      if (eid && hooks.renderedIds.has(eid)) return;
      hooks.container.appendChild(
        buildCompactionCard(
          {
            before_tokens: evt.before_tokens,
            after_tokens: evt.after_tokens,
            trigger: evt.trigger,
          },
          evt.summary || "",
        ),
      );
      if (eid) hooks.renderedIds.add(eid);
    } else if (
      owns &&
      !evt.superseded &&
      evt.reason !== "error" &&
      !(evt.reason === "cancelled" && evt.trigger === "auto")
    ) {
      // cancelled / not_enough_messages / irreducible / empty_summary —
      // informational, not an error state.  Three suppressions: a stale
      // end's notice would narrate a dead compaction underneath a live
      // one; a superseded end (a force-abandoned compaction retiring
      // late, flagged by the backend) is one nobody is waiting on — its
      // notice mid-turn reads as the LIVE work being cancelled; and an
      // auto-compaction's cancel is just part of cancelling the
      // surrounding turn — the send loop emits its own "[Generation
      // cancelled]" info line, so a second line here stacked two notices
      // for one Stop click.  (A superseded OK end above still renders
      // its result card — the history swap really happened.)
      // HAND-SYNCED SIBLING: cli.py TerminalUI.on_compaction implements
      // this same three-clause policy for the terminal — change both or
      // they drift (two runtimes, no shared code path).
      hooks.onNotice(evt.message || "Compaction skipped.");
    }
    hooks.scroll(true);
  }
}

// Send-POST abort bound for a pane, selected off its compaction holder.
// Sends during a slash-command window PARK server-side and dispatch when
// the window closes — a manual /compact legitimately runs for minutes, so
// while ITS progress card is live the composer must not abort the parked
// POST at the wedged-node default (~15s) and silently drop the message.
// The card is the one cross-tab signal that a long window is in progress
// (SSE-driven via applyCompactionEvent); with no card the short default
// stands — a WEDGED quick command past 15s should fail loudly, not hang
// the composer for 10 minutes.  Deployment note: reverse proxies bound
// the effective park at their own read timeout (documented in the API
// reference).  Shared by the interactive composer and the coordinator
// viewer so the policy can't drift between panes.
export function sendAbortMs(holder) {
  return holder && holder.card ? 600000 : 15000;
}

// Retire a pane's in-progress compaction card (if any) and clear the
// holder.  The teardown half of the lifecycle the reducer above owns —
// exported so the panes' stream_end handlers (force-stop abandons the
// compaction worker without an end event in flight) and transcript-wipe
// sites share ONE implementation instead of four hand-synced copies.
// Element.remove() on an already-detached node (a wipe that
// replaceChildren()'d the transcript) is a harmless no-op.
export function resetCompactionHolder(holder) {
  if (holder.card) holder.card.remove();
  holder.card = null;
  holder.cid = null;
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

// Kicker text for a tool-batch head, shared so the interactive pane and the
// coordinator render byte-identical labels.  `state`: "pending" | "evaluating" |
// "running" | "done" (default/solo).  `n` = tool count.
export function batchKicker(state, n) {
  const par = n >= 2;
  if (state === "pending")
    return par ? "⚠ Approval · Parallel " + n : "⚠ Approval";
  if (state === "evaluating")
    return par ? "Evaluating · Parallel " + n : "Evaluating";
  if (state === "running") return par ? "Running · Parallel " + n : "Running";
  return par ? "Parallel · " + n + " tools" : "Tool";
}

// "1/3"-style index label for a parallel row; "" for a solo batch.
export function indexLabel(idx, n) {
  return n >= 2 ? idx + 1 + "/" + n : "";
}

// ===========================================================================
// Shared approval-card builders (step 5e.2)
//
// Pure leaf DOM builders for the unified `.conv-*` card (conversation.css).
// Both panes' (stateful) orchestration ASSEMBLES these; the builders own only
// the DOM + class vocabulary, so the coordinator and interactive surfaces
// render the SAME card.  Everything stateful -- the toolRows map, idempotent
// upgrade-in-place, the early-paint announce shell, SSE routing -- stays in
// the pane and CALLS these.
//
// House style: createElement / textContent, never innerHTML.  Set textContent
// with NO leading whitespace: the .conv-row-cmd / -result / -diff CSS is
// white-space:pre-wrap, so HTML/source indentation would render as a phantom
// left indent (a real bug if a caller passes pre-indented text).
// ===========================================================================

// Empty batch shell (.conv-batch) + header strip (kicker / summary / tier).
// The caller appends rows + actions/status and flips the state modifier
// (--pending/--auto/--running/--approved/--denied/--error).  opts:
//   parallel (bool)  -- >=2 calls: rows share the left rail
//   stateClass (str) -- e.g. "conv-batch--pending" (omit for the neutral shell)
//   kickerText / summaryText / tierText
export function buildConvBatchShell(opts) {
  opts = opts || {};
  const batch = document.createElement("div");
  batch.className = "conv-batch";
  batch.classList.add(
    opts.parallel ? "conv-batch--parallel" : "conv-batch--solo",
  );
  if (opts.stateClass) batch.classList.add(opts.stateClass);

  const head = document.createElement("div");
  head.className = "conv-batch-head";
  const kicker = document.createElement("span");
  kicker.className = "conv-batch-kicker";
  kicker.textContent = opts.kickerText || "Tool";
  head.appendChild(kicker);
  if (opts.summaryText) {
    const summary = document.createElement("span");
    summary.className = "conv-batch-summary";
    summary.textContent = opts.summaryText;
    head.appendChild(summary);
  }
  if (opts.tierText) {
    const tier = document.createElement("span");
    tier.className = "conv-batch-tier";
    tier.textContent = opts.tierText;
    head.appendChild(tier);
  }
  batch.appendChild(head);
  return batch;
}

// Tool-call row (.conv-row) + call line (idx / name / auto-tag / args).  The
// caller appends verdict / cmd / warning / result / status children + manages
// state.  item: {func_name, call_id, auto_approved, auto_approve_reason}.  opts:
//   indexLabel  -- "1/3" pill for parallel batches (omit -> no pill)
//   argsText    -- pre-formatted arg summary (coordinator); interactive omits
//                  this and appends buildConvCmd() instead
//   first/last  -- rail-tuck markers for parallel batches
export function buildConvRow(item, opts) {
  item = item || {};
  opts = opts || {};
  const row = document.createElement("div");
  row.className = "conv-row";
  if (opts.first) row.classList.add("conv-row--first");
  if (opts.last) row.classList.add("conv-row--last");
  if (item.call_id) row.dataset.callId = item.call_id;
  // Stamp the function name so the metacog dim rule (memory/recall) and the
  // pane's per-call routing have something to match.
  if (item.func_name) row.dataset.toolName = item.func_name;

  const call = document.createElement("div");
  call.className = "conv-row-call";
  if (opts.indexLabel) {
    const idx = document.createElement("span");
    idx.className = "conv-row-idx";
    idx.textContent = opts.indexLabel;
    call.appendChild(idx);
  }
  const name = document.createElement("span");
  name.className = "conv-row-name";
  name.textContent = item.func_name || "(unknown tool)";
  if (item.auto_approved) {
    const auto = document.createElement("span");
    auto.className = "conv-row-auto";
    const reason = item.auto_approve_reason || "auto_approve_tools";
    auto.textContent = " auto: " + reason;
    auto.title = "Tool auto-approved (no operator prompt) -- reason: " + reason;
    name.appendChild(auto);
  }
  call.appendChild(name);
  if (opts.argsText) {
    const args = document.createElement("span");
    args.className = "conv-row-args";
    args.textContent = redactCredentials(opts.argsText);
    call.appendChild(args);
  }
  row.appendChild(call);
  return row;
}

// Bash `$ cmd` line + optional unified-diff preview (interactive tool rows).
// Returns a fragment to append into a .conv-row after the call line.  Lifts
// buildToolDiv's header-clean + diff-parse; textContent only.
export function buildConvCmd(item) {
  item = item || {};
  const frag = document.createDocumentFragment();
  const headerText = stripAnsi(item.header || "");
  const cleaned = headerText.replace(/^[^\s]+\s+\w+:\s*/, "");
  const cmdText = cleaned || headerText;
  if (cmdText) {
    const cmd = document.createElement("div");
    cmd.className = "conv-row-cmd";
    if (item.func_name === "bash") {
      const dollar = document.createElement("span");
      dollar.className = "conv-row-cmd-dollar";
      dollar.textContent = "$ ";
      cmd.append(dollar, redactCredentials(cmdText));
    } else {
      cmd.textContent = redactCredentials(cmdText);
    }
    frag.appendChild(cmd);
  }
  if (item.preview) {
    const diff = document.createElement("div");
    diff.className = "conv-row-diff";
    // The preview is uncapped upstream (a whole multiline command / one line
    // per edited line) — cap what we RENDER: past ~400 lines the preview
    // carries no decision value, the DOM cost is ~2 nodes/line in every
    // transcript row, and an argument-spread append of an unbounded node
    // list can throw RangeError mid-paint (engines cap spread arity around
    // 65k args), killing the tool card — and the approval gate — for the
    // batch.  Appended incrementally for the same reason.
    // Redact credentials ONCE on the full text before splitting, rather than
    // running 7 regex sweeps per line (~2800 passes at 400 lines).
    const MAX_PREVIEW_LINES = 400;
    const raw = stripAnsi(item.preview);
    const redacted = redactCredentials(raw);
    let lines = redacted.split("\n");
    const omitted = lines.length - MAX_PREVIEW_LINES;
    if (omitted > 0) lines = lines.slice(0, MAX_PREVIEW_LINES);
    lines.forEach((line, i) => {
      if (i > 0) diff.appendChild(document.createTextNode("\n"));
      const trimmed = line.trim();
      let cls = null;
      if (trimmed.startsWith("-")) cls = "conv-diff-del";
      else if (trimmed.startsWith("+")) cls = "conv-diff-add";
      else if (trimmed.startsWith("Warning:")) cls = "conv-diff-warn";
      if (cls) {
        const span = document.createElement("span");
        span.className = cls;
        span.textContent = line;
        diff.appendChild(span);
      } else {
        diff.appendChild(document.createTextNode(line));
      }
    });
    frag.appendChild(diff);
    // The omission notice sits BELOW the scroll box as a sibling, not as the
    // diff's last child: .conv-row-diff is a 240px inner scroller, so an
    // inline marker would sit thousands of pixels below its fold — invisible
    // exactly at the approval moment, where the operator must know the
    // preview is partial.  Its own neutral class (not .conv-diff-warn):
    // an omission is informational, not a command warning, and raw --warn
    // fails AA on the light panel background.
    if (omitted > 0) {
      const more = document.createElement("div");
      more.className = "conv-diff-omit";
      more.textContent = "… " + omitted + " more preview lines not shown";
      frag.appendChild(more);
    }
  }
  return frag;
}

// Verdict badge (.conv-verdict) + expandable detail.  verdict: the judge /
// heuristic verdict object (risk_level, recommendation, confidence,
// intent_summary, reasoning, evidence[], tier, judge_model).  opts.judgePending
// appends the "judge analyzing" spinner.  When `verdict` is null + judgePending,
// returns a spinner-only badge (the stripe stays NEUTRAL -- risk isn't known
// yet -- which is why the --{risk} class is withheld here, not just guarded in
// CSS).  Returns a fragment [badge] or [badge, detail].
export function buildConvVerdict(verdict, opts) {
  opts = opts || {};
  const frag = document.createDocumentFragment();
  const badge = document.createElement("div");
  badge.className = "conv-verdict";

  if (!verdict) {
    if (opts.judgePending) badge.appendChild(_convSpinner());
    frag.appendChild(badge);
    return frag;
  }

  const risk = normalizeRiskLevel(verdict.risk_level);
  badge.classList.add("conv-verdict--" + risk);
  badge.setAttribute("data-risk", risk);
  if (verdict.call_id) badge.setAttribute("data-call-id", verdict.call_id);

  const riskSpan = document.createElement("span");
  riskSpan.className = "conv-verdict-risk";
  riskSpan.textContent = risk.toUpperCase();
  badge.appendChild(riskSpan);

  const rec = verdict.recommendation || "review";
  const recSpan = document.createElement("span");
  recSpan.className =
    "conv-verdict-rec conv-verdict-rec--" +
    (rec === "approve" ? "approve" : rec === "deny" ? "deny" : "review");
  recSpan.textContent = rec;
  badge.appendChild(recSpan);

  if (verdict.confidence != null) {
    const conf = document.createElement("span");
    conf.className = "conv-verdict-conf";
    const v = verdict.confidence;
    conf.textContent = (typeof v === "number" ? Math.round(v * 100) : v) + "%";
    badge.appendChild(conf);
  }

  if (opts.judgePending) badge.appendChild(_convSpinner());

  const hasDetail = !!(
    verdict.intent_summary ||
    verdict.reasoning ||
    (verdict.evidence && verdict.evidence.length) ||
    verdict.tier ||
    verdict.judge_model
  );
  let detail = null;
  if (hasDetail) {
    const expand = document.createElement("button");
    expand.type = "button";
    expand.className = "conv-verdict-expand";
    expand.textContent = "details";
    badge.appendChild(expand);

    detail = document.createElement("div");
    detail.className = "conv-verdict-detail";
    detail.style.display = "none";
    if (verdict.intent_summary) {
      const s = document.createElement("div");
      s.className = "conv-verdict-summary";
      s.textContent = verdict.intent_summary;
      detail.appendChild(s);
    }
    if (verdict.reasoning) {
      const r = document.createElement("div");
      r.className = "conv-verdict-reasoning";
      r.textContent = verdict.reasoning;
      detail.appendChild(r);
    }
    const evidence = verdict.evidence || [];
    if (evidence.length) {
      const ev = document.createElement("div");
      ev.className = "conv-verdict-evidence";
      for (const e of evidence) {
        const erow = document.createElement("div");
        erow.textContent = "• " + e;
        ev.appendChild(erow);
      }
      detail.appendChild(ev);
    }
    const tier = document.createElement("div");
    tier.className = "conv-verdict-tier";
    let t = (verdict.tier || "heuristic") + " tier";
    if (verdict.judge_model) t += " | " + verdict.judge_model;
    tier.textContent = t;
    detail.appendChild(tier);

    expand.addEventListener("click", () => {
      const shown = detail.style.display !== "none";
      detail.style.display = shown ? "none" : "block";
      expand.textContent = shown ? "details" : "hide";
    });
  }

  frag.appendChild(badge);
  if (detail) frag.appendChild(detail);
  return frag;
}

function _convSpinner() {
  const spin = document.createElement("span");
  spin.className = "conv-verdict-spinner";
  const dot = document.createElement("span");
  dot.className = "conv-verdict-spinner-dot";
  dot.setAttribute("aria-hidden", "true");
  spin.append(dot, " judge analyzing…");
  return spin;
}

// Output-guard warning chip (.conv-warning--{risk}).  assessment: {risk_level,
// flags[], redacted, tier, judge_risk, confidence, judge_model, reasoning}.
// Risk is normalized (unknown -> medium), folding the per-site `|| "medium"`
// fallbacks both panes carried.  Returns the chip element.
export function buildConvWarning(assessment) {
  const a = assessment || {};
  const risk = normalizeRiskLevel(a.risk_level);
  const flags = a.flags || [];
  const chip = document.createElement("div");
  chip.className = "conv-warning conv-warning--" + risk;
  chip.setAttribute("role", "status");
  const label = document.createElement("span");
  label.className = "conv-warning-label";
  label.textContent = "⚠ " + risk.toUpperCase();
  chip.appendChild(label);
  if (flags.length) {
    chip.appendChild(document.createTextNode(" " + flags.join(", ")));
  }
  if (a.redacted) {
    const red = document.createElement("span");
    red.className = "conv-warning-redacted";
    red.textContent = " (credentials redacted)";
    chip.appendChild(red);
  }
  // LLM-judge attribution -- the judge's OWN verdict when it differs from the
  // displayed (merged) risk, plus confidence + model, so a regex match reads
  // apart from a model judgement.
  if (a.tier === "llm") {
    const tier = document.createElement("span");
    tier.className = "conv-warning-tier";
    let t = "⚖ LLM";
    if (a.judge_risk && a.judge_risk !== risk) t += ": " + a.judge_risk;
    if (a.confidence > 0) t += " · " + Math.round(a.confidence * 100) + "%";
    if (a.judge_model) t += " · " + a.judge_model;
    tier.textContent = t;
    chip.appendChild(tier);
  }
  if (a.reasoning) {
    const r = document.createElement("div");
    r.className = "conv-warning-reasoning";
    r.textContent = a.reasoning;
    chip.appendChild(r);
  }
  return chip;
}

// One action button (.conv-btn--{role}).  role: "approve" | "always" | "deny".
// kbdHint renders as a .conv-kbd chip -- the pane passes its OWN keybinding so
// muscle memory is preserved (the look converges; the keys stay per-pane).
export function buildConvButton(role, label, kbdHint, ariaLabel) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "conv-btn conv-btn--" + role;
  btn.dataset.role = role;
  btn.appendChild(document.createTextNode(label));
  if (kbdHint) {
    const kbd = document.createElement("span");
    kbd.className = "conv-kbd";
    kbd.setAttribute("aria-hidden", "true");
    kbd.textContent = kbdHint;
    btn.appendChild(kbd);
  }
  if (ariaLabel) btn.setAttribute("aria-label", ariaLabel);
  return btn;
}

// Action row (.conv-actions) -- Deny / Approve all / Approve, right-aligned via
// a spacer (the coordinator idiom).  opts:
//   onApprove / onDeny / onAlways -- click callbacks (pane-specific resolution)
//   alwaysLabel  -- aria/title for the "always" button; omit -> no Approve-all
//                   button (nothing to persist)
//   kbd: {approve, deny, always}  -- per-pane keybinding hints
//   glowRec      -- "approve"|"deny"|"review" -> recommended-button glow
//   withFeedback -- append the inline feedback input (interactive affordance)
// Returns the .conv-actions element; the caller appends it to the batch.
export function buildConvActions(opts) {
  opts = opts || {};
  const kbd = opts.kbd || {};
  const actions = document.createElement("div");
  actions.className = "conv-actions";
  if (opts.glowRec) {
    const g =
      opts.glowRec === "approve"
        ? "approve"
        : opts.glowRec === "deny"
          ? "deny"
          : "review";
    actions.classList.add("conv-verdict-glow--" + g);
  }
  const spacer = document.createElement("div");
  spacer.className = "conv-actions-spacer";
  actions.appendChild(spacer);

  const deny = buildConvButton("deny", "Deny", kbd.deny, "Deny");
  if (opts.onDeny) deny.addEventListener("click", opts.onDeny);
  actions.appendChild(deny);

  if (opts.alwaysLabel) {
    const always = buildConvButton(
      "always",
      "Approve all",
      kbd.always,
      opts.alwaysLabel,
    );
    always.title = opts.alwaysLabel;
    if (opts.onAlways) always.addEventListener("click", opts.onAlways);
    actions.appendChild(always);
  }

  const approve = buildConvButton("approve", "Approve", kbd.approve, "Approve");
  if (opts.onApprove) approve.addEventListener("click", opts.onApprove);
  actions.appendChild(approve);

  if (opts.withFeedback) {
    const fb = document.createElement("input");
    fb.type = "text";
    fb.className = "conv-feedback";
    fb.placeholder = "feedback (optional)";
    actions.appendChild(fb);
  }
  return actions;
}

// Resolved status pill (.conv-status) -- replaces the action row after
// approve/deny, or prefills on history replay of a resolved batch.  opts:
//   approved (bool), always (bool), feedback (str), auto (bool).
export function buildConvStatus(opts) {
  opts = opts || {};
  const status = document.createElement("div");
  status.className = "conv-status";
  const label = document.createElement("span");
  if (opts.auto) {
    status.classList.add("conv-status--auto");
    label.textContent = "✓ auto-approved";
  } else if (opts.approved) {
    status.classList.add("conv-status--approved");
    label.textContent = opts.always ? "✓ approved · always" : "✓ approved";
  } else {
    status.classList.add("conv-status--denied");
    label.textContent = "✗ denied";
  }
  status.appendChild(label);
  if (opts.feedback) {
    const fb = document.createElement("span");
    fb.className = "conv-status-feedback";
    fb.textContent = "— " + opts.feedback;
    status.appendChild(fb);
  }
  return status;
}

// Tool result block (.conv-row-result).  output: raw text; opts.isError marks
// it (lead glyph + colour).  Pretty-prints small JSON payloads (coordinator
// idiom).  Returns the block; the caller appends it (and may special-case
// media / MCP-error embeds before falling back to this -- interactive).
export function buildConvResult(output, opts) {
  opts = opts || {};
  const block = document.createElement("div");
  block.className = "conv-row-result";
  const lead = document.createElement("span");
  lead.className = "conv-row-result-lead";
  lead.textContent = opts.isError ? "✗ error: " : "↳ result: ";
  block.appendChild(lead);
  const cleaned = stripAnsi(output || "");
  let pretty = cleaned;
  // Pretty-print only small JSON (cap 32 KiB): a large payload can deepen into
  // a multi-MB graph and stall the main thread; the parent is pre-wrap so raw
  // text still wraps past the cap.
  const CAP = 32 * 1024;
  if (cleaned && cleaned.length <= CAP) {
    const head = cleaned.charCodeAt(0);
    if (head === 0x7b || head === 0x5b) {
      try {
        const parsed = JSON.parse(cleaned);
        if (parsed && typeof parsed === "object") {
          pretty = JSON.stringify(parsed, null, 2);
        }
      } catch (_e) {
        /* not JSON -- fall through to raw text */
      }
    }
  }
  // Clamp the rendered body — same rationale as the JSON pretty-print cap
  // above: the server ships tool output verbatim, and a single multi-MB
  // result (an agent cat-ing a large file) becomes a multi-MB pre-wrap text
  // node that stalls layout on insert and is rebuilt on every full
  // re-render.  The transcript shows the head; the full output stays in
  // history/storage.
  const RAW_CAP = 64 * 1024;
  if (pretty.length > RAW_CAP) {
    pretty =
      pretty.slice(0, RAW_CAP) +
      "\n… (" +
      pretty.length.toLocaleString() +
      " chars total — truncated for display)";
  }
  const body = document.createElement("span");
  body.textContent = redactCredentials(pretty);
  block.appendChild(body);
  return block;
}

// Preview chip (.conv-preview-chip) — the transcript affordance for a tool
// result that carries a preview-pane descriptor.  Live results auto-open the
// pane; this chip is how the operator RE-opens one (after closing the pane,
// or from a replayed transcript, where nothing auto-opens).  `descriptor` is
// the structured preview object off the tool_result event / history entry;
// `onOpen(descriptor)` is the pane-supplied opener (routed through the host
// seam so this builder stays shell-agnostic).  textContent only.
export function buildPreviewChip(descriptor, onOpen) {
  const d = descriptor || {};
  const chip = document.createElement("button");
  chip.type = "button";
  chip.className = "conv-preview-chip";
  // The label can be a raw URL (no <title> found) — a target with embedded
  // basic-auth must not print credentials into the transcript.
  const label = redactCredentials(d.title || d.source || "preview");
  chip.setAttribute("aria-label", "Open preview: " + label);
  chip.title = d.source
    ? "Open preview — " + redactCredentials(d.source)
    : "Open preview";
  const glyph = document.createElement("span");
  glyph.className = "conv-preview-glyph";
  glyph.setAttribute("aria-hidden", "true");
  glyph.textContent = "▤";
  chip.appendChild(glyph);
  const text = document.createElement("span");
  text.className = "conv-preview-title";
  text.textContent = label;
  chip.appendChild(text);
  if (d.kind) {
    const kind = document.createElement("span");
    kind.className = "conv-preview-kind";
    kind.textContent = d.kind;
    chip.appendChild(kind);
  }
  if (typeof onOpen === "function") {
    chip.addEventListener("click", () => onOpen(d));
  }
  return chip;
}

// Expandable body for a task_agent row's nested sub-steps (the "agent card").
// A task agent runs its own sub-tools; this gives that row a collapsible body
// the pane renders the live step stream into, so the steps nest UNDER the
// task_agent call instead of scattering at the top level.  The pane appends the
// returned `wrap` into the task_agent .conv-row and renders step rows (ordinary
// .conv-row leaves, matched by call_id) into `body`; `label` shows the live
// step count.  House style: programmatic DOM, textContent only.
let _agentCardSeq = 0;

export function buildAgentCardBody() {
  const wrap = document.createElement("div");
  wrap.className = "conv-agent";
  // Single source of truth for collapse: the data attribute drives BOTH the
  // body visibility and the caret rotation (in CSS), so a programmatic toggle
  // (collapse-by-default here, or the auto-expand-on-approval in the pane) can't
  // desync caret/aria the way per-node textContent did.
  //
  // Collapsed by default: a task agent can run 100+ steps, and the parent often
  // fans out many in parallel — expanded, that's a wall.  The label carries the
  // live count + state ("12 steps · running"), so you expand on demand.  The
  // pane force-expands a card when a nested approval is pending (it's blocking —
  // it can't hide behind the toggle).
  wrap.dataset.collapsed = "true";
  const bodyId = "conv-agent-body-" + (_agentCardSeq += 1);
  const toggle = document.createElement("button");
  toggle.type = "button";
  toggle.className = "conv-agent-toggle";
  toggle.setAttribute("aria-expanded", "false");
  toggle.setAttribute("aria-controls", bodyId);
  toggle.setAttribute("aria-label", "Show or hide sub-agent steps");
  const caret = document.createElement("span");
  caret.className = "conv-agent-caret";
  caret.setAttribute("aria-hidden", "true");
  caret.textContent = "▾"; // CSS rotates it when collapsed
  const label = document.createElement("span");
  label.className = "conv-agent-label";
  label.textContent = "0 steps";
  toggle.append(caret, label);
  const body = document.createElement("div");
  body.className = "conv-agent-body";
  body.id = bodyId;
  toggle.addEventListener("click", () => {
    const collapsed = wrap.dataset.collapsed === "true";
    wrap.dataset.collapsed = collapsed ? "false" : "true";
    toggle.setAttribute("aria-expanded", collapsed ? "true" : "false");
  });
  wrap.append(toggle, body);
  return { wrap, body, label };
}
