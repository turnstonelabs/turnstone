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
    args.textContent = opts.argsText;
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
      cmd.append(dollar, cmdText);
    } else {
      cmd.textContent = cmdText;
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
    const MAX_PREVIEW_LINES = 400;
    let lines = stripAnsi(item.preview).split("\n");
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
