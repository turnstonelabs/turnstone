"""Guards for the shared conversational-pane module
(``turnstone/shared_static/conversation.js``).

Born in step 5e.1: the deduplicated substrate BOTH the interactive pane
(shared_static/interactive.js) and the coordinator pane
(console/static/coordinator/coordinator.js) import.  These pin the exports plus
the load-bearing invariants (operator-context marker, null-safe ANSI strip, no
innerHTML) so a regression in the shared module fails loudly here rather than
silently in one pane.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from tests._js_harness_helpers import node_skip

_CONVERSATION_JS = (
    Path(__file__).resolve().parent.parent / "turnstone/shared_static/conversation.js"
)


def _body() -> str:
    return _CONVERSATION_JS.read_text(encoding="utf-8")


def test_exports_the_shared_helpers() -> None:
    """The three helpers both panes import must be exported — drop one and the
    importing pane module fails to load entirely."""
    body = _body()
    for name in ("stripAnsi", "buildWatchResultCard", "buildSystemNudgeMarker"):
        assert f"export function {name}" in body, f"{name} must be exported"


def test_strip_ansi_is_null_safe() -> None:
    """Unified on the coordinator's null-safe variant: a non-string argument
    coerces to "" rather than throwing (interactive's old copy did not guard,
    so this is a strict-superset behaviour for its call sites)."""
    body = _body()
    assert 'String(s == null ? "" : s).replace(' in body, (
        "stripAnsi must coerce its argument before .replace"
    )


def test_watch_card_carries_operator_context_marker() -> None:
    """The watch-result card keeps the shared ``operator-context`` marker (the
    retry-walk in both panes skips rows carrying it) and stays textContent-only."""
    body = _body()
    assert '"msg watch-result operator-context"' in body
    assert 'setAttribute("data-ts-role", "watch")' in body
    for part in (
        "msg-watch-header",
        "msg-watch-cmd",
        "msg-watch-body",
        "msg-watch-footer",
    ):
        assert part in body, f"watch card missing {part}"


def test_nudge_marker_shape() -> None:
    body = _body()
    assert '"msg user system-nudge"' in body
    assert 'setAttribute("data-source", "system_nudge")' in body


def test_no_inner_html() -> None:
    """House style: programmatic DOM only — no innerHTML *usage* in the shared
    module (the header comment names it; guard the access pattern)."""
    assert ".innerHTML" not in _body()


def test_agent_card_exposes_hidden_context_badge() -> None:
    body = _body()
    assert "export function formatAgentContextTokens(value)" in body
    assert "export function agentContextIsWarning(promptTokens, contextWindow)" in body
    assert 'context.className = "conv-agent-context";' in body
    assert "context.hidden = true;" in body
    assert 'context.setAttribute("aria-hidden", "true");' in body
    assert "return { wrap, body, label, context, toggle };" in body


@node_skip
def test_agent_context_token_formatter_is_compact_and_deterministic() -> None:
    script = f"""
import {{
  formatAgentContextTokens,
  agentContextIsWarning,
}} from {json.dumps(_CONVERSATION_JS.as_uri())};
const cases = [
  [999, "999"],
  [1000, "1k"],
  [1280, "1.3k"],
  [41000, "41k"],
  [128000, "128k"],
  [999499, "999k"],
  [999500, "1m"],
  [999999, "1m"],
  [1000000, "1m"],
  [-1, ""],
];
for (const [value, expected] of cases) {{
  const actual = formatAgentContextTokens(value);
  if (actual !== expected) throw new Error(`${{value}}: ${{actual}} !== ${{expected}}`);
}}
if (agentContextIsWarning(79, 100)) throw new Error("79% warned early");
if (!agentContextIsWarning(80, 100)) throw new Error("80% did not warn");
if (!agentContextIsWarning(81, 100)) throw new Error("81% did not warn");
if (agentContextIsWarning(1, 0)) throw new Error("zero window warned");
"""
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 0, proc.stderr


@node_skip
def test_task_agent_compaction_reducer_is_nested_transient_and_id_safe() -> None:
    """Task compaction reuses the shared lifecycle without leaking a result."""
    script = f"""
class Element {{
  constructor(tag) {{
    this.tagName = tag;
    this.children = [];
    this.parentNode = null;
    this.className = "";
    this.style = {{}};
    this.textContent = "";
    this.attributes = {{}};
    this.classList = {{
      remove: (...names) => {{
        const drop = new Set(names);
        this.className = this.className
          .split(/\\s+/)
          .filter((name) => name && !drop.has(name))
          .join(" ");
      }},
      contains: (name) => this.className.split(/\\s+/).includes(name),
    }};
  }}
  setAttribute(name, value) {{ this.attributes[name] = String(value); }}
  appendChild(child) {{
    if (child.parentNode) child.remove();
    this.children.push(child);
    child.parentNode = this;
    return child;
  }}
  querySelector(selector) {{
    const cls = selector.startsWith(".") ? selector.slice(1) : "";
    for (const child of this.children) {{
      if (cls && child.className.split(/\\s+/).includes(cls)) return child;
      const nested = child.querySelector(selector);
      if (nested) return nested;
    }}
    return null;
  }}
  remove() {{
    if (!this.parentNode) return;
    const i = this.parentNode.children.indexOf(this);
    if (i >= 0) this.parentNode.children.splice(i, 1);
    this.parentNode = null;
  }}
}}
globalThis.document = {{ createElement: (tag) => new Element(tag) }};
const {{ applyCompactionEvent }} = await import({json.dumps(_CONVERSATION_JS.as_uri())});

const container = new Element("div");
const nested = new Element("div");
const holder = {{ card: null, cid: null }};
let notices = 0;
let scrolls = 0;
const hooks = {{
  container,
  renderedIds: new Set(),
  renderResult: false,
  append: (node) => nested.appendChild(node),
  onNotice: () => {{ notices += 1; }},
  scroll: () => {{ scrolls += 1; }},
}};
applyCompactionEvent(holder, {{
  phase: "start", target: "task_agent", trigger: "auto", compaction_id: 12,
}}, hooks);
if (!holder.card || holder.cid !== "12") throw new Error("start was not retained");
if (nested.children.length !== 1 || container.children.length !== 0)
  throw new Error("task progress escaped its nested placement");
const header = holder.card.querySelector(".msg-compaction-header");
if (!header || header.textContent !== "compacting task context… · auto")
  throw new Error("task-specific progress copy was not rendered");

applyCompactionEvent(holder, {{
  phase: "progress", target: "task_agent", compaction_id: 12,
  part: 2, total: 4, depth: 0,
}}, hooks);
const fill = holder.card.querySelector(".msg-compaction-bar-fill");
if (fill.style.width !== "25%") throw new Error("progress did not advance");

applyCompactionEvent(holder, {{
  phase: "end", target: "task_agent", compaction_id: 11, ok: true,
  summary: "must stay private",
}}, hooks);
if (!holder.card || nested.children.length !== 1)
  throw new Error("stale end retired the live task compaction");
if (container.children.length !== 0) throw new Error("task summary leaked to transcript");

applyCompactionEvent(holder, {{
  phase: "end", target: "task_agent", compaction_id: 12, ok: true,
  summary: "must stay private",
}}, hooks);
if (holder.card || holder.cid != null || nested.children.length !== 0)
  throw new Error("owning end did not retire progress");
if (container.children.length !== 0 || notices !== 0)
  throw new Error("task end rendered a durable result or notice");
if (scrolls !== 2) throw new Error("unexpected lifecycle scroll count: " + scrolls);
"""
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 0, proc.stderr


def test_normalize_risk_level_unknown_to_medium() -> None:
    """Unified canonical fallback (step 5e.1b): an unknown / unrecognized risk
    normalizes to "medium" (the user's decision; the coordinator's old rank used
    "high").  The crit/med abbreviations alias to critical/medium so a 'crit'
    verdict no longer renders as medium (the latent interactive bug)."""
    body = _body()
    assert 'return RISK_LEVELS.indexOf(s) >= 0 ? s : "medium";' in body
    assert 'crit: "critical"' in body and 'med: "medium"' in body


def test_risk_rank_and_max_severity_exported() -> None:
    """riskRank + maxSeverityItem (lifted from the coordinator's _riskRank /
    _maxSeverityItem) are exported and build on the canonical normalize, so the
    rank and the display can't disagree on the fallback.  An item with no verdict
    ranks below low so it never wins the max-severity pick."""
    body = _body()
    assert "export function riskRank(" in body
    assert "export function maxSeverityItem(" in body
    assert "? riskRank(v.risk_level) : -1;" in body


# --- step 5e.2b: the shared approval-card builders ---------------------------


def test_card_builders_exported() -> None:
    """The leaf DOM builders both panes' orchestration calls (5e.2c).  Drop one
    and the calling pane fails to construct its half of the converged card."""
    body = _body()
    for name in (
        "buildConvBatchShell",
        "buildConvRow",
        "buildConvCmd",
        "buildConvVerdict",
        "buildConvWarning",
        "buildConvButton",
        "buildConvActions",
        "buildConvStatus",
        "buildConvResult",
    ):
        assert f"export function {name}(" in body, f"{name} must be exported"


def test_builders_emit_conv_vocabulary() -> None:
    """The builders speak ONLY the neutral .conv-* vocabulary (conversation.css)
    — no leaked .coord-tool-* / .ts-approval-* / .verdict-* class strings."""
    body = _body()
    for cls in (
        '"conv-batch"',
        '"conv-row"',
        '"conv-row-call"',
        '"conv-verdict"',
        '"conv-warning conv-warning--"',
        '"conv-actions"',
        '"conv-btn conv-btn--"',
        '"conv-status"',
        '"conv-row-result"',
    ):
        assert cls in body, f"builders missing {cls}"
    for stale in ("coord-tool-", "ts-approval-", "verdict-badge"):
        assert stale not in body, f"builders leaked stale vocab: {stale}"


def test_approve_all_label_unified() -> None:
    """Button language (BRIEFING): the persistent action reads 'Approve all'
    (a dashed --ok ghost), NOT the coordinator's old 'Always'.  The trio is
    Approve / Deny / Approve all on the .conv-btn--{role} vocabulary."""
    body = _body()
    assert '"Approve all"' in body  # unified persistent-action label
    assert '"Always"' not in body  # the coordinator's old label is gone
    assert 'buildConvButton("approve", "Approve"' in body
    assert 'buildConvButton("deny", "Deny"' in body
    assert "conv-btn conv-btn--" in body


def test_warning_and_verdict_normalize_risk() -> None:
    """Both risk-bearing builders route risk through normalizeRiskLevel, so the
    per-site `|| "medium"` fallbacks collapse onto the canonical unknown->medium
    fold (5e.1b) and 'crit' aliases to 'critical'."""
    body = _body()
    assert "normalizeRiskLevel(verdict.risk_level)" in body, "verdict must normalize"
    assert "normalizeRiskLevel(a.risk_level)" in body, "warning must normalize"
    assert '"conv-warning conv-warning--" + risk' in body
    assert 'badge.classList.add("conv-verdict--" + risk)' in body


def test_unbounded_render_inputs_are_capped() -> None:
    """Perf-audit P0: the two builders that used to render unbounded input.
    The diff preview caps rendered lines and appends incrementally — the old
    single ``diff.append(...nodes)`` spread threw RangeError past engine
    spread-arity limits, killing the tool card (and the approval gate) for
    the batch.  The raw result body clamps at RAW_CAP so one multi-MB tool
    output can't become a multi-MB pre-wrap text node rebuilt on every
    re-render."""
    body = _body()
    assert "MAX_PREVIEW_LINES" in body
    assert "diff.append(...nodes)" not in body, (
        "preview nodes must append incrementally, not via one spread call"
    )
    assert "more preview lines not shown" in body
    assert "RAW_CAP" in body
    assert "truncated for display" in body


def test_retry_note_validates_backoff_before_rendering() -> None:
    """retry_in is a server-emitted backoff coerced with Number(); like the
    part/total pair just below it, it must be finiteness-validated (and
    non-negative) so a malformed value can't render "retrying in NaNs".  The
    error text is kept regardless — it is the load-bearing half of the note."""
    body = _body()
    assert "Number.isFinite(secs) && secs >= 0" in body, (
        "retry_in must be validated (finite, non-negative) before its seconds render"
    )
    assert "Math.round(Number(evt.retry_in))" not in body, (
        "retry_in must not be Math.round(Number(...))'d without a finiteness guard"
    )
