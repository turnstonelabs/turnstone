#!/usr/bin/env python3
"""Prompt optimizer for turnstone (UCB self-modify loop).

Consumes the measurement substrate in :mod:`turnstone.eval.core` — runs the
test suite, then uses a multi-agent pipeline (analyst, optimizer, observer,
diversifier, tool optimizer) to edit the developer prompt and tool
descriptions so more tests pass. An evolution tree with UCB1 selection
explores prompt variants without collapsing on a bad edit.

Usage:
    turnstone-optimizer tests.json
    turnstone-optimizer tests.json --no-optimize
    turnstone-optimizer tests.json --prompt prompt.txt --n-runs 5 --max-iter 10
"""

import argparse
import difflib
import json
import math
import os
import re
import textwrap
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from openai import OpenAI

from turnstone.core.model_turn import model_turn, resolve_lane
from turnstone.core.providers import LLMProvider, create_provider
from turnstone.core.session import ChatSession
from turnstone.core.trajectory import Role, Turn
from turnstone.eval.core import (
    _MCP_ONLY_TOOLS,
    BOLD,
    CYAN,
    DIM,
    GREEN,
    RESET,
    TOOLS,
    YELLOW,
    NullUI,
    _append_summary_tsv,
    _apply_tool_overrides,
    _detect_provider,
    _log,
    _make_client_and_provider,
    _print_summary_table,
    _run_iteration,
)


@dataclass
class EvolutionNode:
    """A node in the prompt evolution tree (UCB-based search)."""

    node_id: int
    parent_id: int | None
    prompt: str
    tool_overrides: dict[str, dict[str, Any]] = field(default_factory=dict)
    score: float = 0.0
    visit_count: int = 0
    children: list[int] = field(default_factory=list)
    iteration: int = 0
    optimizer_system: str = ""


# ─── Prompt optimizer ────────────────────────────────────────────────────────


DIVERSIFIER_SYSTEM = """\
You generate paraphrased variations of user prompts for a tool-use \
evaluation harness. Each variation must preserve the EXACT SAME INTENT \
— the same task, same expected outcome, same level of specificity — \
but use different phrasing, vocabulary, sentence structure, or tone.

Rules:
- Preserve the core action the user is asking for. If the original \
says "fix the typo in config.py", all variants must ask to fix a \
typo in config.py.
- Vary along these dimensions: formality (casual ↔ formal), \
directness (imperative ↔ descriptive), verbosity (terse ↔ detailed), \
framing (command ↔ question ↔ description of need).
- Do NOT change the expected tool behavior. If the original implies \
using bash, the variant must also imply bash.
- Do NOT add new requirements, constraints, or context not in the \
original.
- Each variant should be a single message, roughly similar length \
to the original.

Output a JSON array of strings, one per variant. No commentary, \
no markdown fences, just the JSON array.\
"""


ANALYST_SYSTEM = """\
You are a test failure analyst for an LLM tool-use evaluation harness. \
You receive test results showing how a coding assistant performed \
against expected tool call sequences.

Your job: identify SEMANTIC PATTERNS across failures and successes — \
not just what failed, but WHY, and what the failures have in common.

You have access to a `bash` tool. Use it to compute statistics, build \
confusion matrices, analyze tool co-occurrence, or quantify patterns — \
don't just eyeball the data.

## Analysis Framework

### 1. Failure Mode Patterns
Look across all failing runs and identify shared root causes:
- Does the model treat certain phrasings as conversation vs action?
- Are there tool confusion pairs (e.g., always picks write_file over edit_file)?
- Do failures cluster around implicit vs explicit instructions?
- Are there complexity thresholds where the model breaks down?

### 2. Success/Failure Contrast
Compare passing and failing cases to isolate what makes the difference:
- What do passing prompts have that failing ones lack?
- Do passing cases use action verbs while failing ones describe goals?
- Is there a pattern in prompt length, specificity, or structure?

### 3. Consistency Signals
For each failing case, classify:
- Systematic (0% pass): the prompt is fundamentally missing an instruction
- Flaky (1-79% pass): the prompt is ambiguous — wording nudge needed
- Marginal (80-99%): minor edge case — low priority

### 4. Actionable Diagnosis
For each pattern found, state:
- What the model is doing wrong (observed behavior)
- Why it's doing it (root cause hypothesis)
- What pattern or example could demonstrate the right behavior \
(prefer "show" over "tell" — a concrete tool chain example teaches \
better than a rule like "always do X")

## Output Format

```
## Failure Patterns
- [Pattern 1]: [which cases] — [observed behavior] because [root cause]
- [Pattern 2]: ...

## Success/Failure Contrast
[What distinguishes passing from failing cases]

## Consistency
- Systematic: [case_ids] — [what's missing]
- Flaky: [case_ids] — [what's ambiguous]
- Marginal: [case_ids] — [edge case description]

## Recommended Fixes (priority order)
1. [Highest impact fix]: addresses [N] cases — [pattern/example to add or adjust]
2. [Next fix]: ...
```

Be concise. Focus on patterns, not individual case narratives. \
The optimizer that reads your output needs actionable signal, \
not lengthy explanations. Frame fixes as patterns/examples to add, \
not imperative rules (avoid suggesting "ALWAYS", "NEVER", "MUST").\
"""


OPTIMIZER_SYSTEM = """\
You are a prompt optimizer. You receive a developer prompt that \
teaches a coding assistant how to use its tools, along with test \
results showing which tool-selection patterns the assistant gets \
right and wrong.

Your job: edit the prompt so more tests pass.

Style — the prompt teaches through patterns and examples, not rules:
- Good: "Delegate a subtask → task_agent:\\n   task_agent(prompt='...')"
- Bad: "You MUST call task_agent. NEVER skip it. ALWAYS use it."
- If the current prompt contains imperative rules (MUST, NEVER, \
ALWAYS, ABSOLUTE RULE, etc.), replace them with a pattern that \
demonstrates the right behavior. Rules are noise — examples teach.

Approach:
- Study the analyst's failure diagnosis (included in the input) and \
address the highest-priority patterns first.
- For each failing case, trace why the assistant picked the wrong \
tool, then add or adjust a pattern that demonstrates the right \
choice.
- Leave phrasing that drives 100%-pass cases alone.
- Remove lines that aren't pulling their weight. Shorter prompts \
that score the same are better prompts.
- Stay within 130% of the original prompt length.

Output the modified prompt only. No commentary, no fences.\
"""


OBSERVER_SYSTEM = """\
You tune the optimizer's instructions. The optimizer edits a developer \
prompt to improve tool-selection test scores. Your output replaces \
the optimizer's instructions — stay at the meta level (how the \
optimizer should work, not the prompt itself).

Read the iteration history and look for trends:
- Improving → small refinements only.
- Plateau (same score 2+ iters) → the optimizer is stuck. Remove \
ineffective guidance and try a different angle.
- Regression → the last change hurt. Steer the optimizer away from \
that kind of edit.
- Oscillation → edits are too broad. Push toward smaller, more \
targeted changes.

If scores haven't improved for 3+ iterations, nudge the optimizer \
toward more structural changes — reordering sections, pruning dead \
weight, or replacing rules with concrete tool chain examples.

Core principle: the developer prompt teaches by showing patterns \
(e.g., "Find and modify → search then read_file then edit_file"), \
not by stating rules (e.g., "always search before editing"). If the \
optimizer is producing rules, steer it back toward examples.

You can adjust: which failure modes to prioritize, whether to add \
patterns or simplify, length guidance, and diagnosis-to-fix mappings.

Make 2-3 targeted edits. Remove guidance that isn't working. Stay \
under 150% of the input length.

Output the modified optimizer instructions only.\
"""


def _diversify_prompts(
    client: Any,
    model: str,
    cases: list[dict[str, Any]],
    n_variants: int,
    provider: LLMProvider | None = None,
) -> dict[str, list[str]]:
    """Generate paraphrased prompt variants for each test case.

    Returns {case_id: [variant1, variant2, ...]}. The original
    user_prompt is always included as the first variant.
    """
    prov = provider or create_provider("openai")
    # Temperature is not pinned (house rule) — sampling diversity for the
    # paraphraser belongs in the diversifier model's own configuration.
    lane = resolve_lane(prov, client, model)
    result: dict[str, list[str]] = {}

    for ci, case in enumerate(cases):
        cid = case["id"]
        original = case["user_prompt"]

        if n_variants <= 1:
            result[cid] = [original]
            continue

        # Use cached variants if present in the test case
        cached = case.get("user_prompts")
        if cached and isinstance(cached, list) and len(cached) >= n_variants:
            # Ensure original is always at slot 0
            variants = cached[:n_variants]
            if variants[0] != original:
                variants = [v for v in variants if v != original]
                variants.insert(0, original)
                variants = variants[:n_variants]
            result[cid] = variants
            print(
                f"  {DIM}[{ci + 1}/{len(cases)}] {cid}...{RESET}"
                f" {CYAN}{len(result[cid])} cached{RESET}"
            )
            continue

        # Partial cache: keep existing variants, only generate the delta
        existing: list[str] = []
        if cached and isinstance(cached, list):
            # Ensure original is at slot 0
            existing = [v for v in cached if v != original]
            existing.insert(0, original)

        needed = n_variants - max(len(existing), 1)  # original is always slot 0
        if needed <= 0:
            result[cid] = (existing or [original])[:n_variants]
            print(
                f"  {DIM}[{ci + 1}/{len(cases)}] {cid}...{RESET}"
                f" {CYAN}{len(result[cid])} cached{RESET}"
            )
            continue

        print(f"  {DIM}[{ci + 1}/{len(cases)}] {cid}...{RESET}", end="", flush=True)
        # Generate only the missing variants
        existing_json = json.dumps(existing[1:]) if len(existing) > 1 else "[]"
        user_content = f"Original prompt: {json.dumps(original)}\n\n"
        if len(existing) > 1:
            user_content += f"Existing variations (do NOT repeat these): {existing_json}\n\n"
        user_content += (
            f"Generate {needed} new paraphrased variations of the original prompt. "
            f"Output a JSON array of {needed} strings."
        )

        try:
            cr = model_turn(
                lane,
                [Turn.system(DIVERSIFIER_SYSTEM), Turn.user(user_content)],
                max_tokens=8192,
                reasoning_effort="low",
            )
            raw = (cr.content or "").strip()
            # Strip reasoning tags
            raw = re.sub(
                r"<(?:think|reasoning)>.*?</(?:think|reasoning)>",
                "",
                raw,
                flags=re.DOTALL,
            ).strip()
            # Strip markdown fences
            fence_match = re.search(r"```[^\n]*\n(.*?)```", raw, re.DOTALL)
            if fence_match:
                raw = fence_match.group(1).strip()

            new_variants = json.loads(raw)
            if isinstance(new_variants, list) and all(isinstance(v, str) for v in new_variants):
                # Merge existing + new, then deduplicate
                base = existing if existing else [original]
                seen: set[str] = {v.strip().lower() for v in base}
                unique: list[str] = list(base)
                dupes = 0
                for v in new_variants[:needed]:
                    key = v.strip().lower()
                    if key in seen:
                        dupes += 1
                    else:
                        seen.add(key)
                        unique.append(v)
                result[cid] = unique
                dupe_note = f", {dupes} dupes removed" if dupes else ""
                short_note = (
                    f" {YELLOW}(< {n_variants} requested, will cycle){RESET}"
                    if len(unique) < n_variants
                    else ""
                )
                print(f" {GREEN}{len(unique)} unique{dupe_note}{RESET}{short_note}")
            else:
                result[cid] = [original]
                print(f" {YELLOW}parse error, using original{RESET}")
        except Exception as e:
            _log(f"\n  Diversifier failed for {cid}: {e}", dim=True)
            result[cid] = [original]

    # Summary statistics
    total_variants = sum(len(v) for v in result.values())
    cases_with_variants = sum(1 for v in result.values() if len(v) > 1)
    avg_variants = total_variants / len(result) if result else 0
    print(
        f"  {total_variants} total variants across {cases_with_variants} cases"
        f" (avg {avg_variants:.1f}/case)"
    )

    return result


def _observe_and_update_optimizer(
    client: Any,
    model: str,
    optimizer_system: str,
    iterations: list[dict[str, Any]],
    provider: LLMProvider | None = None,
) -> str:
    """Analyze optimizer behavior and return a modified OPTIMIZER_SYSTEM."""
    parts: list[str] = []
    for i in range(1, len(iterations)):
        prev, curr = iterations[i - 1], iterations[i]
        prev_agg = prev.get("aggregate", {})
        curr_agg = curr.get("aggregate", {})
        prev_len = len(prev.get("prompt", ""))
        curr_len = len(curr.get("prompt", ""))
        len_delta = curr_len - prev_len
        score_prev = prev_agg.get("overall_pass_rate", 0)
        score_curr = curr_agg.get("overall_pass_rate", 0)
        score_delta = score_curr - score_prev

        part = f"Iteration {i - 1} → {i}:\n"
        part += f"  Prompt: {len_delta:+d} chars ({prev_len} → {curr_len})\n"
        part += f"  Score: {score_prev:.0%} → {score_curr:.0%} ({score_delta:+.0%})\n"

        prev_rates = prev_agg.get("per_case_pass_rates", {})
        curr_rates = curr_agg.get("per_case_pass_rates", {})
        improved: list[str] = []
        regressed: list[str] = []
        for case_id in set(prev_rates) | set(curr_rates):
            p = prev_rates.get(case_id, 0)
            c = curr_rates.get(case_id, 0)
            if c > p:
                improved.append(f"{case_id} ({p:.0%}→{c:.0%})")
            elif c < p:
                regressed.append(f"{case_id} ({p:.0%}→{c:.0%})")
        if improved:
            part += f"  Improved: {', '.join(improved)}\n"
        if regressed:
            part += f"  Regressed: {', '.join(regressed)}\n"
        if curr.get("prompt_diff"):
            diff_text = curr["prompt_diff"][:500]
            part += f"  Diff:\n{diff_text}\n"
        parts.append(part)

    # Summarize what the optimizer's output looked like (without showing
    # full developer messages, which cause the observer to mimic them)
    behavior_notes: list[str] = []
    for it in iterations[-3:]:
        idx = it.get("iteration", "?")
        prompt = it.get("prompt", "")
        score = it.get("aggregate", {}).get("overall_pass_rate", 0)
        has_bullets = "- " in prompt or "* " in prompt
        has_numbers = bool(re.search(r"^\d+\.", prompt, re.MULTILINE))
        has_headers = "**" in prompt or "##" in prompt
        has_rules = bool(
            re.search(
                r"(?i)(always|never|do not|must|rule|important)",
                prompt,
            )
        )
        has_patterns = "→" in prompt
        notes: list[str] = []
        if has_rules and not has_patterns:
            notes.append("rule-heavy, few patterns")
        elif has_rules and has_patterns:
            notes.append("mixed rules and patterns")
        elif has_patterns:
            notes.append("pattern-based")
        if has_bullets or has_numbers:
            notes.append("used bullet/numbered lists")
        if has_headers:
            notes.append("used bold headers")
        if len(prompt) > 1200:
            notes.append(f"length={len(prompt)} chars (over 1200)")
        elif len(prompt) < 600:
            notes.append(f"length={len(prompt)} chars (under 600)")
        else:
            notes.append(f"length={len(prompt)} chars")
        style = ", ".join(notes) if notes else "prose style"
        behavior_notes.append(f"Iteration {idx} ({score:.0%}): {style}")

    user_content = (
        f"## Optimizer Instructions (edit these)\n"
        f"```\n{optimizer_system}\n```\n\n"
        f"## What the Optimizer Produced (style observations, not content to copy)\n"
        + "\n".join(behavior_notes)
        + "\n\n## Iteration History\n"
        + "\n".join(parts)
    )

    prov = provider or create_provider("openai")
    cr = model_turn(
        resolve_lane(prov, client, model),
        [Turn.system(OBSERVER_SYSTEM), Turn.user(user_content)],
        max_tokens=8192,
        reasoning_effort="low",
    )

    result = cr.content or optimizer_system
    result = re.sub(
        r"<(?:think|reasoning)>.*?</(?:think|reasoning)>",
        "",
        result,
        flags=re.DOTALL,
    ).strip()

    # Strip markdown code fences if wrapped
    fence_match = re.search(r"```[^\n]*\n(.*?)```", result, re.DOTALL)
    if fence_match:
        result = fence_match.group(1).strip()

    # Reject degenerate outputs (>200% of input length)
    if len(result) > len(optimizer_system) * 2.0:
        _log(
            f"  Observer output too long ({len(result)} vs {len(optimizer_system)}), "
            "keeping current optimizer system",
            dim=True,
        )
        return optimizer_system

    return result


def _classify_failure(
    run: dict[str, Any],
    expected_actions: list[dict[str, Any]],
) -> str:
    """Classify a failed run into a failure mode bucket."""
    tools = run.get("tool_sequence", [])
    if run.get("json_dump"):
        return "json_dump"
    if not tools:
        return "no_tool_call"
    detail = run.get("detail", "")
    lower_detail = detail.lower()
    if "timed out" in lower_detail:
        return "timeout"
    if "skipped" in lower_detail:
        return "skipped"
    if detail.startswith("Error:") or lower_detail.startswith("subprocess error:"):
        return "error"

    unmatched = run.get("unmatched", [])
    extra = run.get("extra_tools", [])
    matched = run.get("matched", [])

    if not unmatched and extra:
        return "extra_tools"

    # Check for tool substitution — expected one tool, got a different one
    expected_names = {expected_actions[i]["tool"] for i in unmatched if i < len(expected_actions)}
    actual_names = set(tools)
    if expected_names and not expected_names & actual_names:
        return "wrong_tool"

    # Check if right tools were called but args didn't match
    if matched and unmatched:
        unmatched_expected = {
            expected_actions[i]["tool"] for i in unmatched if i < len(expected_actions)
        }
        if unmatched_expected & actual_names:
            return "wrong_args"

    return "missing_tool"


def _build_failure_analysis(
    iteration_result: dict[str, Any],
    test_cases: list[dict[str, Any]],
) -> str:
    """Build a semantic failure analysis summary across all cases."""
    case_index = {c["id"]: c for c in test_cases}

    # Classify every failed run
    mode_cases: dict[str, list[str]] = {}  # mode -> [case_id, ...]
    mode_details: dict[str, list[str]] = {}  # mode -> [detail strings]
    consistency: dict[str, str] = {}  # case_id -> systematic|flaky|marginal

    for case_id, case_result in iteration_result["cases"].items():
        case_def = case_index.get(case_id)
        expected = case_def.get("expected_actions", []) if case_def else []
        pr = case_result["pass_rate"]

        if pr == 1.0:
            continue

        # Consistency classification
        if pr == 0:
            consistency[case_id] = "systematic"
        elif pr < 0.8:
            consistency[case_id] = "flaky"
        else:
            consistency[case_id] = "marginal"

        for run in case_result["runs"]:
            if run.get("pass"):
                continue
            mode = _classify_failure(run, expected)
            mode_cases.setdefault(mode, [])
            if case_id not in mode_cases[mode]:
                mode_cases[mode].append(case_id)

            # Build a detail string for tool substitution cases
            if mode == "wrong_tool":
                expected_names = [
                    expected[i]["tool"] for i in run.get("unmatched", []) if i < len(expected)
                ]
                actual = run.get("tool_sequence", [])
                detail = f"{case_id}: expected {expected_names}, got {actual}"
                mode_details.setdefault(mode, []).append(detail)

    if not mode_cases:
        return ""

    # Build the summary
    lines: list[str] = []

    # Failure mode summary
    mode_labels = {
        "no_tool_call": "Text-only response (no tool call made)",
        "wrong_tool": "Wrong tool selected",
        "missing_tool": "Missing required tool in sequence",
        "wrong_args": "Right tool, wrong arguments",
        "extra_tools": "Correct sequence but unnecessary extra calls",
        "timeout": "Timed out",
        "error": "Tool execution error",
        "skipped": "Skipped (fast-fail)",
        "json_dump": "Tool call emitted as JSON text instead of function call",
    }
    for mode, cases in sorted(mode_cases.items(), key=lambda x: -len(x[1])):
        label = mode_labels.get(mode, mode)
        lines.append(f"- {label}: {', '.join(cases)}")
        for detail in (mode_details.get(mode, []))[:3]:
            lines.append(f"    {detail}")

    # Consistency summary
    systematic = [c for c, v in consistency.items() if v == "systematic"]
    flaky = [c for c, v in consistency.items() if v == "flaky"]
    marginal = [c for c, v in consistency.items() if v == "marginal"]

    if systematic or flaky or marginal:
        lines.append("")
        if systematic:
            lines.append(
                f"Systematic failures (always fail, need new instruction): {', '.join(systematic)}"
            )
        if flaky:
            lines.append(
                f"Flaky failures (sometimes pass, prompt is ambiguous): {', '.join(flaky)}"
            )
        if marginal:
            lines.append(f"Marginal failures (mostly pass, minor edge case): {', '.join(marginal)}")

    return "\n".join(lines)


_ANALYST_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Execute a bash command. Use for jq, awk, sort, uniq, etc.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Bash command to execute."},
                },
                "required": ["command"],
            },
        },
    },
]


def _exec_analyst_tool(name: str, arguments: str) -> str:
    """Execute a tool call from the analyst agent."""
    try:
        args = json.loads(arguments)
    except json.JSONDecodeError:
        return f"Invalid JSON arguments: {arguments[:200]}"

    if name == "bash":
        import subprocess

        try:
            proc = subprocess.run(
                ["bash", "-c", args.get("command", "")],
                capture_output=True,
                text=True,
                timeout=15,
            )
            output = proc.stdout + proc.stderr
            return output[:4000] or "(no output)"
        except subprocess.TimeoutExpired:
            return "Command timed out after 15s"
    return f"Unknown tool: {name}"


def _run_analyst(
    client: Any,
    model: str,
    test_cases: list[dict[str, Any]],
    iteration_result: dict[str, Any],
    provider: LLMProvider | None = None,
    optimize_tools: bool = False,
    tool_overrides: dict[str, dict[str, Any]] | None = None,
) -> str:
    """Run the analyst agent to produce a semantic failure analysis.

    Multi-turn agent with a bash tool for computing statistics.
    Phase 1 of the two-phase optimization: analyst diagnoses patterns,
    then optimizer uses the diagnosis to modify the prompt.
    """
    # Build structured input: per-case results with rule-based pre-analysis
    case_index = {c["id"]: c for c in test_cases}
    case_parts: list[str] = []
    for case_id, case_result in iteration_result["cases"].items():
        case_def = case_index.get(case_id)
        if not case_def:
            continue
        pr = case_result["pass_rate"]
        status = "PASS" if pr == 1.0 else "WEAK" if pr >= 0.5 else "FAIL"

        part = (
            f"[{status}] {case_id} (pass_rate={pr:.0%})\n"
            f"  User prompt: {case_def['user_prompt']}\n"
            f"  Expected tools: {json.dumps(case_def.get('expected_actions', []))}\n"
            f"  Actual sequences: "
            f"{[r.get('tool_sequence', []) for r in case_result['runs']]}"
        )

        # Add per-run failure classifications
        expected = case_def.get("expected_actions", [])
        failed_runs = [r for r in case_result["runs"] if not r.get("pass")]
        if failed_runs:
            modes = [_classify_failure(r, expected) for r in failed_runs]
            part += f"\n  Failure modes: {modes}"

        case_parts.append(part)

    # Include rule-based pre-analysis as a starting point
    rule_analysis = _build_failure_analysis(iteration_result, test_cases)

    # Always show available tool names so the analyst doesn't hallucinate
    # about missing tools
    tool_names = sorted(
        t["function"]["name"] for t in TOOLS if t["function"]["name"] not in _MCP_ONLY_TOOLS
    )
    user_content = f"## Available Tools\n{', '.join(tool_names)}\n\n"
    user_content += "## Test Results\n" + "\n\n".join(case_parts)
    if rule_analysis:
        user_content += f"\n\n## Rule-Based Pre-Analysis\n{rule_analysis}"

    if optimize_tools:
        # Include current tool descriptions (with any overrides applied)
        effective_tools = _apply_tool_overrides(TOOLS, tool_overrides) if tool_overrides else TOOLS
        tool_index = {t["function"]["name"]: t for t in effective_tools}
        # Collect tools that appear in expected or actual sequences
        relevant_names: set[str] = set()
        for case_id, case_result in iteration_result["cases"].items():
            case_def = case_index.get(case_id)
            if not case_def:
                continue
            for action in case_def.get("expected_actions", []):
                if isinstance(action, str):
                    relevant_names.add(action)
                elif isinstance(action, dict):
                    tool_name = action.get("tool")
                    if isinstance(tool_name, str):
                        relevant_names.add(tool_name)
            for run in case_result["runs"]:
                for tool_name in run.get("tool_sequence", []):
                    relevant_names.add(tool_name)
        tool_parts: list[str] = []
        for name in sorted(relevant_names):
            tool = tool_index.get(name)
            if tool:
                desc = tool["function"].get("description", "")
                tool_parts.append(f"**{name}**: {desc}")
        if tool_parts:
            user_content += "\n\n## Current Tool Descriptions\n" + "\n\n".join(tool_parts)

    user_content += (
        "\n\nAnalyze the semantic patterns across these results. "
        "Focus on WHY failures happen and what passing cases have in common. "
        "Use the bash tool if you need to compute statistics, "
        "build confusion matrices, or analyze distributions."
    )

    analyst_system = ANALYST_SYSTEM
    if optimize_tools:
        analyst_system += (
            "\n\nFOCUS: Tool description optimization mode. The system prompt "
            "is frozen — your recommendations should target tool descriptions "
            "and schemas, not system prompt wording. Analyze:\n"
            "- Which tools are confused with each other and why\n"
            "- What description changes would disambiguate them\n"
            "- Whether parameter names/descriptions mislead the model"
        )

    prov = provider or create_provider("openai")
    turns: list[Turn] = [
        Turn.system(analyst_system),
        Turn.user(user_content),
    ]

    # Multi-turn loop: let the analyst call tools up to 5 rounds.
    # Temperature is not pinned (house rule) — the analyst model's own
    # configuration governs sampling.
    lane = resolve_lane(prov, client, model)
    max_turns = 5
    for _turn in range(max_turns):
        mtr = model_turn(
            lane,
            turns,
            tools=_ANALYST_TOOLS,
            max_tokens=8192,
            reasoning_effort="medium",
        )

        # Same degenerate-repetition cap as before; a capped turn drops its
        # native lane rather than replay orphan native tool blocks the
        # mirror no longer carries.
        capped = mtr.tool_calls[:5]
        assistant_turn = mtr.turn
        if len(mtr.tool_calls) > len(capped):
            assistant_turn = Turn.assistant(
                mtr.content, tool_calls=mtr.turn.tool_calls[: len(capped)]
            )
        turns.append(assistant_turn)

        if not capped:
            break

        # Execute tool calls
        for tc in capped:
            func_name = tc["function"]["name"]
            output = _exec_analyst_tool(func_name, tc["function"]["arguments"])
            turns.append(Turn.tool(tc["id"], output))

    # Extract final text response
    result = ""
    for t in reversed(turns):
        if t.role is Role.ASSISTANT and t.text:
            result = t.text
            break

    # Strip reasoning tags if present
    result = re.sub(
        r"<(?:think|reasoning)>.*?</(?:think|reasoning)>",
        "",
        result,
        flags=re.DOTALL,
    ).strip()

    return result


TOOL_OPTIMIZER_SYSTEM = """\
You are a tool description optimizer for an LLM coding assistant. You \
receive the current tool definitions and test results showing where the \
assistant picked the wrong tool.

GOAL: Modify tool descriptions so the assistant picks the correct tool \
for each task. Make MINIMAL, TARGETED changes.

WHAT YOU CAN CHANGE:
- The top-level "description" field on each tool.
- The "description" field on individual parameters.

WHAT YOU CANNOT CHANGE:
- Tool names, parameter names, types, or required fields.
- The number of tools or their parameter schemas.

PRINCIPLES:
- When two tools are confused, clarify the BOUNDARY between them. \
State when to use each one and when NOT to.
- Add negative guidance ("Do NOT use this for X") when confusion \
is systematic.
- Keep descriptions concise. Every word should help tool selection.
- Preserve accurate descriptions of what the tool actually does.
- Do not invent capabilities a tool does not have.

OUTPUT FORMAT: A JSON object mapping tool names to override objects. \
Only include tools you are modifying. Each override object can have:
- "description": new top-level description string
- "parameters": object mapping parameter names to new description strings

Example:
{"bash": {"description": "Execute a bash command. For running programs, \
git, and system commands. Do NOT use for reading files (use read_file) \
or searching code (use search)."}}

Output ONLY the JSON object. No commentary outside the JSON.\
"""


def _propose_tool_overrides(
    client: Any,
    model: str,
    current_tools: list[dict[str, Any]],
    current_overrides: dict[str, dict[str, Any]],
    test_cases: list[dict[str, Any]],
    iteration_result: dict[str, Any],
    analyst_output: str,
    provider: LLMProvider | None = None,
) -> dict[str, dict[str, Any]]:
    """Propose tool description overrides based on failure analysis.

    Returns a merged override dict (current + new changes).
    """
    # Build effective tool descriptions for display
    effective_tools = _apply_tool_overrides(current_tools, current_overrides)
    tool_parts: list[str] = []
    for tool in effective_tools:
        fn = tool["function"]
        params = fn.get("parameters", {}).get("properties", {})
        param_descs = ", ".join(f"{k}: {v.get('description', '')}" for k, v in params.items())
        tool_parts.append(f"- {fn['name']}: {fn['description']}")
        if param_descs:
            tool_parts.append(f"    params: {param_descs}")

    # Collect only wrong_tool failures
    case_index = {c["id"]: c for c in test_cases}
    confusion_parts: list[str] = []
    for case_id, case_result in iteration_result["cases"].items():
        if case_result["pass_rate"] == 1.0:
            continue
        case_def = case_index.get(case_id)
        expected = case_def.get("expected_actions", []) if case_def else []
        for run in case_result["runs"]:
            if run.get("pass"):
                continue
            if _classify_failure(run, expected) == "wrong_tool" and case_def:
                expected_names = [
                    expected[i]["tool"] for i in run.get("unmatched", []) if i < len(expected)
                ]
                actual = run.get("tool_sequence", [])
                confusion_parts.append(
                    f"- {case_id}: expected {expected_names}, got {actual}\n"
                    f"    prompt: {case_def['user_prompt']}"
                )

    user_content = "## Current Tool Descriptions\n" + "\n".join(tool_parts) + "\n\n"
    if current_overrides:
        user_content += (
            f"## Active Overrides\nAlready modified: {', '.join(sorted(current_overrides))}\n\n"
        )
    user_content += "## Tool Confusion Failures\n" + "\n".join(confusion_parts) + "\n\n"
    if analyst_output:
        user_content += f"## Analyst Diagnosis\n{analyst_output}\n\n"
    user_content += (
        "Modify the MINIMUM tool descriptions needed to resolve the confusion. "
        "Output ONLY a JSON override object."
    )

    prov = provider or create_provider("openai")
    cr = model_turn(
        resolve_lane(prov, client, model),
        [Turn.system(TOOL_OPTIMIZER_SYSTEM), Turn.user(user_content)],
        max_tokens=8192,
        reasoning_effort="medium",
    )

    raw = (cr.content or "").strip()
    # Strip reasoning tags
    raw = re.sub(
        r"<(?:think|reasoning)>.*?</(?:think|reasoning)>", "", raw, flags=re.DOTALL
    ).strip()
    # Strip markdown fences
    fence_match = re.search(r"```[^\n]*\n(.*?)```", raw, re.DOTALL)
    if fence_match:
        raw = fence_match.group(1).strip()

    try:
        new_overrides = json.loads(raw)
    except json.JSONDecodeError:
        _log(f"  Tool optimizer returned invalid JSON: {raw[:200]}", dim=True)
        return current_overrides

    if not isinstance(new_overrides, dict):
        return current_overrides

    # Validate: only allow known tool names, only description/parameters keys
    valid_names = {t["function"]["name"] for t in current_tools}
    validated: dict[str, dict[str, Any]] = {}
    for name, override in new_overrides.items():
        if name not in valid_names or not isinstance(override, dict):
            continue
        clean: dict[str, Any] = {}
        if "description" in override and isinstance(override["description"], str):
            clean["description"] = override["description"]
        if "parameters" in override and isinstance(override["parameters"], dict):
            clean["parameters"] = {
                k: v for k, v in override["parameters"].items() if isinstance(v, str)
            }
        if clean:
            validated[name] = clean

    # Merge: new overrides take precedence, deep-merge parameters
    merged = {**current_overrides}
    for name, override in validated.items():
        if name in merged:
            existing = merged[name]
            combined = {**existing, **override}
            if "parameters" in existing and "parameters" in override:
                combined["parameters"] = {**existing["parameters"], **override["parameters"]}
            merged[name] = combined
        else:
            merged[name] = override

    return merged


def _propose_prompt_modification(
    client: Any,
    model: str,
    current_prompt: str,
    test_cases: list[dict[str, Any]],
    iteration_result: dict[str, Any],
    history: list[dict[str, Any]],
    optimizer_system: str = OPTIMIZER_SYSTEM,
    provider: LLMProvider | None = None,
    parent_scores: dict[str, float] | None = None,
    analyst_output: str = "",
    tool_overrides: dict[str, dict[str, Any]] | None = None,
) -> str:
    """Use the model to propose a new prompt based on evaluation results."""
    # Build summary of results
    case_index = {c["id"]: c for c in test_cases}
    summary_parts: list[str] = []
    for case_id, case_result in iteration_result["cases"].items():
        case_def = case_index.get(case_id)
        if not case_def:
            continue
        pr = case_result["pass_rate"]
        status = "PASS" if pr == 1.0 else "WEAK" if pr >= 0.5 else "FAIL"
        delta_str = ""
        if parent_scores is not None:
            delta = pr - parent_scores.get(case_id, 0)
            delta_str = f", delta={delta:+.0%}"
        summary_parts.append(
            f"[{status}] {case_id} (pass_rate={case_result['pass_rate']:.0%}{delta_str})\n"
            f"  User prompt: {case_def['user_prompt']}\n"
            f"  Expected: {json.dumps(case_def['expected_actions'])}\n"
            f"  Actual sequences: "
            f"{[r.get('tool_sequence', []) for r in case_result['runs']]}"
        )

    # Build history summary (last 3 iterations)
    history_parts: list[str] = []
    for h in history[-3:]:
        agg = h.get("aggregate", {})
        history_parts.append(
            f"Iteration {h['iteration']}: "
            f"overall_pass_rate={agg.get('overall_pass_rate', 0):.0%}, "
            f"per_case={agg.get('per_case_pass_rates', {})}"
        )

    history_text = "\n".join(history_parts) if history_parts else "(first iteration)"

    # Separate passing vs failing cases for clearer diagnosis
    passing = [p for p in summary_parts if p.startswith("[PASS]")]
    failing = [p for p in summary_parts if not p.startswith("[PASS]")]

    user_content = f"## Current Prompt\n```\n{current_prompt}\n```\n\n"
    if passing:
        user_content += (
            "## Passing Tests (leave the phrasing that drives these alone)\n"
            + "\n\n".join(passing)
            + "\n\n"
        )
    if failing:
        user_content += (
            "## Failing Tests (diagnose each, one targeted fix per case)\n"
            + "\n\n".join(failing)
            + "\n\n"
        )

    # Failure analysis from analyst agent (phase 1)
    if analyst_output:
        user_content += f"## Failure Analysis (from analyst)\n{analyst_output}\n\n"

    if tool_overrides:
        user_content += (
            "## Note: Tool Descriptions Modified\n"
            "The tool optimizer has customized descriptions for: "
            f"{', '.join(sorted(tool_overrides))}. "
            "You do not need to add workarounds for tool confusion already "
            "addressed by the modified descriptions.\n\n"
        )

    user_content += (
        f"## Score History\n{history_text}\n\n"
        "Fix failing tests without breaking passing ones. "
        "Output the modified prompt text only."
    )

    prov = provider or create_provider("openai")
    cr = model_turn(
        resolve_lane(prov, client, model),
        [Turn.system(optimizer_system), Turn.user(user_content)],
        max_tokens=16384,
        reasoning_effort="medium",
    )

    new_prompt = cr.content or current_prompt

    # Strip reasoning tags if present
    new_prompt = re.sub(
        r"<(?:think|reasoning)>.*?</(?:think|reasoning)>",
        "",
        new_prompt,
        flags=re.DOTALL,
    ).strip()

    # Strip markdown code fences if the model wrapped the prompt.
    # Also discard any explanation text outside the fences.
    fence_match = re.search(r"```[^\n]*\n(.*?)```", new_prompt, re.DOTALL)
    if fence_match:
        new_prompt = fence_match.group(1).strip()
    elif new_prompt.startswith("```"):
        # Opening fence without closing — strip just the first line
        new_prompt = "\n".join(new_prompt.split("\n")[1:]).strip()

    return new_prompt


def _simple_diff(old: str, new: str) -> str:
    """Generate a simple line-level diff between two prompts."""
    old_lines = old.splitlines(keepends=True)
    new_lines = new.splitlines(keepends=True)
    diff = difflib.unified_diff(old_lines, new_lines, fromfile="before", tofile="after")
    return "".join(diff)


# ─── Evolution tree helpers ───────────────────────────────────────────────────


def _ucb_select(nodes: dict[int, EvolutionNode], c: float) -> int:
    """Select the node with highest UCB1 score.

    Unvisited nodes (visit_count == 0) are selected first, in creation order.
    N (total plays) is the sum of all visit counts across nodes.
    """
    best_id = 0
    best_score = -1.0
    total_visits = sum(n.visit_count for n in nodes.values())
    log_n = math.log(total_visits) if total_visits > 1 else 0.0
    for nid in sorted(nodes):
        node = nodes[nid]
        if node.visit_count == 0:
            return nid
        exploit = node.score
        explore = c * math.sqrt(log_n / node.visit_count)
        ucb = exploit + explore
        if ucb > best_score:
            best_score = ucb
            best_id = nid
    return best_id


def _node_to_dict(node: EvolutionNode) -> dict[str, Any]:
    """Serialize an EvolutionNode for JSON output."""
    d: dict[str, Any] = {
        "node_id": node.node_id,
        "parent_id": node.parent_id,
        "prompt": node.prompt,
        "score": node.score,
        "visit_count": node.visit_count,
        "children": node.children,
        "iteration": node.iteration,
    }
    if node.tool_overrides:
        d["tool_overrides"] = node.tool_overrides
    return d


def _filter_for_optimizer(
    cases: list[dict[str, Any]],
    iter_result: dict[str, Any],
    holdout_ids: set[str],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Strip holdout cases from optimizer feedback, including aggregates."""
    if not holdout_ids:
        return cases, iter_result
    filtered_cases = [c for c in cases if c["id"] not in holdout_ids]
    filtered_case_results = {
        cid: cr for cid, cr in iter_result["cases"].items() if cid not in holdout_ids
    }
    # Recompute aggregate from non-holdout cases only so no holdout signal leaks
    total_runs = sum(len(cr["runs"]) for cr in filtered_case_results.values())
    total_passes = sum(
        sum(1 for r in cr["runs"] if r["pass"]) for cr in filtered_case_results.values()
    )
    filtered_result = dict(iter_result)
    filtered_result["cases"] = filtered_case_results
    filtered_result["aggregate"] = {
        **iter_result["aggregate"],
        "overall_pass_rate": total_passes / total_runs if total_runs else 0,
        "overall_avg_score": (
            sum(cr["avg_score"] for cr in filtered_case_results.values())
            / len(filtered_case_results)
            if filtered_case_results
            else 0
        ),
        "per_case_pass_rates": {cid: cr["pass_rate"] for cid, cr in filtered_case_results.items()},
    }
    return filtered_cases, filtered_result


def _compute_holdout_score(iter_result: dict[str, Any], holdout_ids: set[str]) -> float:
    """Compute pass rate from holdout cases only (or all if no holdout)."""
    cases = iter_result.get("cases", {})
    if holdout_ids:
        rates = [cr["pass_rate"] for cid, cr in cases.items() if cid in holdout_ids]
    else:
        rates = [cr["pass_rate"] for cr in cases.values()]
    return sum(rates) / len(rates) if rates else 0.0


# ─── Main optimization loop ─────────────────────────────────────────────────


def run_optimization(
    base_url: str,
    model: str | None,
    test_file: str,
    initial_prompt: str | None = None,
    n_runs: int | None = 3,
    max_iterations: int = 5,
    temperature: float = 0.7,
    max_tokens: int = 32768,
    reasoning_effort: str = "medium",
    output_file: str = "eval_results.json",
    context_window: int = 131072,
    verbose: bool = False,
    test_timeout: int = 300,
    suite_timeout: int = 0,
    fast_fail: bool = True,
    parallel: int = 1,
    optimizer_base_url: str | None = None,
    optimizer_model: str | None = None,
    observer_base_url: str | None = None,
    observer_model: str | None = None,
    analyst_base_url: str | None = None,
    analyst_model: str | None = None,
    diversifier_base_url: str | None = None,
    diversifier_model: str | None = None,
    diversify: int = 0,
    save_variants: bool = False,
    optimize_tools: bool = False,
    tool_optimizer_base_url: str | None = None,
    tool_optimizer_model: str | None = None,
    save_tools: bool = False,
    explore_constant: float = 1.414,
) -> dict[str, Any]:
    """Main optimization loop with UCB tree search.

    Maintains an evolution tree of prompt variants. Each iteration selects
    the most promising node via UCB1, evaluates it, then uses the optimizer
    to propose a child variant. The observer tunes optimizer strategy every
    3 iterations.

    Three separate model roles:
      - test: the model being evaluated (base_url / model)
      - optimizer: rewrites the developer prompt (--optimizer-*)
      - observer: tunes the optimizer's strategy (--observer-*)

    Inheritance: observer defaults ← optimizer defaults ← test defaults.
    Provider is auto-detected from the base URL.
    """
    # --- Test model (always OpenAI-compatible for headless eval) ---
    api_key = os.environ.get("OPENAI_API_KEY", "dummy")
    client = OpenAI(
        base_url=base_url,
        api_key=api_key,
    )
    if not model:
        from turnstone.core.model_registry import detect_model

        detected, _ = detect_model(client)
        assert detected is not None  # fatal=True guarantees non-None or SystemExit
        model = detected

    # --- Optimizer model (inherits from test if not specified) ---
    opt_base = optimizer_base_url or base_url
    opt_model = optimizer_model or model
    opt_key_env = (
        "ANTHROPIC_API_KEY" if _detect_provider(opt_base) == "anthropic" else "OPENAI_API_KEY"
    )
    opt_key = os.environ.get(opt_key_env, api_key)
    opt_client, opt_provider = _make_client_and_provider(opt_base, opt_key)

    # --- Observer model (inherits from optimizer if not specified) ---
    obs_base = observer_base_url or opt_base
    obs_model = observer_model or opt_model
    obs_key_env = (
        "ANTHROPIC_API_KEY" if _detect_provider(obs_base) == "anthropic" else "OPENAI_API_KEY"
    )
    obs_key = os.environ.get(obs_key_env, opt_key)
    obs_client, obs_provider = _make_client_and_provider(obs_base, obs_key)

    # --- Analyst model (inherits from optimizer if not specified) ---
    ana_base = analyst_base_url or opt_base
    ana_model = analyst_model or opt_model
    ana_key_env = (
        "ANTHROPIC_API_KEY" if _detect_provider(ana_base) == "anthropic" else "OPENAI_API_KEY"
    )
    ana_key = os.environ.get(ana_key_env, opt_key)
    ana_client, ana_provider = _make_client_and_provider(ana_base, ana_key)

    # --- Diversifier model (inherits from optimizer if not specified) ---
    div_base = diversifier_base_url or opt_base
    div_model = diversifier_model or opt_model
    div_key_env = (
        "ANTHROPIC_API_KEY" if _detect_provider(div_base) == "anthropic" else "OPENAI_API_KEY"
    )
    div_key = os.environ.get(div_key_env, opt_key)
    div_client, div_provider = _make_client_and_provider(div_base, div_key)

    # --- Tool optimizer model (inherits from optimizer if not specified) ---
    tool_opt_base = tool_optimizer_base_url or opt_base
    tool_opt_model = tool_optimizer_model or opt_model
    tool_opt_key_env = (
        "ANTHROPIC_API_KEY" if _detect_provider(tool_opt_base) == "anthropic" else "OPENAI_API_KEY"
    )
    tool_opt_key = os.environ.get(tool_opt_key_env, opt_key)
    tool_opt_client, tool_opt_provider = _make_client_and_provider(tool_opt_base, tool_opt_key)

    # Log role assignments
    _log(f"  Test model:  {model} @ {base_url}", dim=True)
    _log(f"  Optimizer:   {opt_model} @ {opt_base}", dim=True)
    if obs_model != opt_model or obs_base != opt_base:
        _log(f"  Observer:    {obs_model} @ {obs_base}", dim=True)
    if ana_model != opt_model or ana_base != opt_base:
        _log(f"  Analyst:     {ana_model} @ {ana_base}", dim=True)
    if diversify > 0 and (div_model != opt_model or div_base != opt_base):
        _log(f"  Diversifier: {div_model} @ {div_base}", dim=True)
    if optimize_tools and (tool_opt_model != opt_model or tool_opt_base != opt_base):
        _log(f"  Tool opt:    {tool_opt_model} @ {tool_opt_base}", dim=True)

    # Load test cases
    with open(test_file) as f:
        suite: dict[str, Any] = json.load(f)

    cases: list[dict[str, Any]] = suite["cases"]
    for i, case in enumerate(cases):
        if "id" not in case:
            raise SystemExit(f"Test case {i} missing required 'id' field")
        if "user_prompt" not in case:
            raise SystemExit(f"Test case '{case.get('id', i)}' missing 'user_prompt'")
    defaults = suite.get("defaults", {})
    # Precedence: CLI arg (non-None) > tests.json defaults > code default (3)
    resolved_n_runs: int = n_runs if n_runs is not None else int(defaults.get("n_runs", 3))

    total_runs = sum(c.get("n_runs", resolved_n_runs) for c in cases)
    print(
        f"  {len(cases)} cases, {resolved_n_runs} runs/case ({total_runs} total), "
        f"max {max_iterations} iterations"
    )

    # Holdout split — holdout cases are evaluated but excluded from optimizer feedback
    holdout_ids: set[str] = {c["id"] for c in cases if c.get("holdout", False)}
    training_count = len(cases) - len(holdout_ids)
    if holdout_ids and training_count < 2:
        _log(
            f"  Warning: only {training_count} non-holdout cases, disabling holdout split",
            dim=True,
        )
        holdout_ids = set()
    elif holdout_ids:
        _log(
            f"  Holdout: {len(holdout_ids)} cases ({', '.join(sorted(holdout_ids))})",
            dim=True,
        )

    # Get initial prompt
    if initial_prompt is None:
        # Extract the default developer message from a temporary ChatSession
        tmp = ChatSession(
            client=client,
            model=model,
            ui=NullUI(),
            instructions=None,
            temperature=temperature,
            max_tokens=max_tokens,
            tool_timeout=30,
            reasoning_effort=reasoning_effort,
            context_window=context_window,
        )
        initial_prompt = next(
            (m["content"] for m in tmp.system_messages if m["role"] in ("developer", "system")),
            None,
        )
        if initial_prompt is None:
            raise SystemExit("No developer prompt found. Provide one with --prompt <file>")
        # Strip memory reminder — it's a runtime artifact, not part of the prompt
        initial_prompt = re.sub(
            r"\n*REMINDER: You currently have \d+ memories stored\..*$",
            "",
            initial_prompt,
        ).strip()

    # --- Evolution tree ---
    nodes: dict[int, EvolutionNode] = {
        0: EvolutionNode(node_id=0, parent_id=None, prompt=initial_prompt),
    }
    next_node_id = 1

    current_optimizer_system = OPTIMIZER_SYSTEM

    results: dict[str, Any] = {
        "meta": {
            "model": model,
            "base_url": base_url,
            "optimizer_model": opt_model,
            "optimizer_base_url": opt_base,
            "observer_model": obs_model,
            "observer_base_url": obs_base,
            "analyst_model": ana_model,
            "analyst_base_url": ana_base,
            "started": datetime.now().isoformat(),
            "test_suite": test_file,
            "n_runs_default": resolved_n_runs,
            "explore_constant": explore_constant,
            "holdout_ids": sorted(holdout_ids) if holdout_ids else [],
        },
        "iterations": [],
        "tree": [],
    }

    tsv_path = os.path.splitext(output_file)[0] + ".tsv"
    case_ids = [c["id"] for c in cases]

    # Diversify prompts — generate paraphrased variants before the loop
    # Auto-detect: if any case has cached user_prompts, use them even without --diversify
    prompt_variants: dict[str, list[str]] | None = None
    if diversify == 0:
        cached_variants = {
            c["id"]: c["user_prompts"]
            for c in cases
            if isinstance(c.get("user_prompts"), list) and len(c["user_prompts"]) > 1
        }
        if cached_variants:
            prompt_variants = cached_variants
            total_v = sum(len(v) for v in cached_variants.values())
            print(
                f"\n  Using cached variants for {len(cached_variants)} cases"
                f" ({total_v} total prompts)"
            )

    if diversify > 0:
        print(f"\nGenerating {diversify} prompt variants per case...")
        prompt_variants = _diversify_prompts(
            client=div_client,
            model=div_model,
            cases=cases,
            n_variants=diversify,
            provider=div_provider,
        )
        results["meta"]["diversify"] = diversify
        results["meta"]["prompt_variants"] = prompt_variants

        # Save variants back to test suite JSON for caching
        if save_variants:
            updated = False
            for case in cases:
                cid = case["id"]
                variants = prompt_variants.get(cid, [])
                if len(variants) > 1 and case.get("user_prompts") != variants:
                    case["user_prompts"] = variants
                    updated = True
            if updated:
                suite["cases"] = cases
                with open(test_file, "w") as f:
                    json.dump(suite, f, indent=2)
                    f.write("\n")
                print(f"  Saved variants to {test_file}")

    cumulative_tokens = 0
    suite_t0 = time.monotonic()

    for iteration in range(max_iterations):
        if suite_timeout > 0:
            suite_elapsed = time.monotonic() - suite_t0
            if suite_elapsed >= suite_timeout:
                print(
                    f"\nSuite timeout ({suite_timeout}s) reached"
                    f" after {suite_elapsed:.0f}s. Stopping."
                )
                break

        # UCB select — pick the most promising node to evaluate/extend
        selected_id = _ucb_select(nodes, explore_constant)
        selected = nodes[selected_id]

        tree_info = f"node {selected_id}"
        if selected.visit_count > 0:
            tree_info += f", score={selected.score:.0%}, visits={selected.visit_count}"
        else:
            tree_info += ", unvisited"
        if len(nodes) > 1:
            tree_info += f", tree={len(nodes)} nodes"

        print(f"\n{'=' * 60}")
        print(f"Iteration {iteration} ({tree_info})")
        print(f"{'=' * 60}")

        iter_t0 = time.monotonic()
        iter_result = _run_iteration(
            client=client,
            model=model,
            system_prompt=selected.prompt,
            cases=cases,
            n_runs=resolved_n_runs,
            temperature=temperature,
            max_tokens=max_tokens,
            reasoning_effort=reasoning_effort,
            context_window=context_window,
            verbose=verbose,
            test_timeout=test_timeout,
            fast_fail=fast_fail,
            parallel=parallel,
            base_url=base_url,
            api_key=api_key,
            prompt_variants=prompt_variants,
            tool_overrides=selected.tool_overrides or None,
        )
        iter_result["iteration"] = iteration
        iter_result["prompt"] = selected.prompt
        iter_result["prompt_diff"] = None
        iter_result["optimizer_system"] = current_optimizer_system
        iter_result["timestamp"] = datetime.now().isoformat()
        iter_result["tree_node_id"] = selected_id
        if selected.tool_overrides:
            iter_result["tool_overrides"] = selected.tool_overrides

        # Update node score (rolling mean)
        new_score = _compute_holdout_score(iter_result, holdout_ids)
        old_score = selected.score
        if selected.visit_count == 0:
            selected.score = new_score
        else:
            selected.score = (selected.score * selected.visit_count + new_score) / (
                selected.visit_count + 1
            )
        selected.visit_count += 1
        if selected.visit_count > 1:
            _log(
                f"  Node {selected_id} score: {old_score:.0%} → {selected.score:.0%}"
                f" (this eval: {new_score:.0%})",
                dim=True,
            )

        cumulative_tokens += iter_result.get("aggregate", {}).get("total_tokens", 0)

        results["iterations"].append(iter_result)

        # Serialize tree state
        results["tree"] = [_node_to_dict(n) for n in nodes.values()]
        with open(output_file, "w") as f:
            json.dump(results, f, indent=2)

        _print_summary_table(iter_result)
        iter_wall = time.monotonic() - iter_t0
        _append_summary_tsv(
            tsv_path,
            iter_result,
            case_ids,
            cumulative_tokens=cumulative_tokens,
            node_score=selected.score,
            wall_time=iter_wall,
        )

        # Check if all passing
        agg = iter_result["aggregate"]
        if agg["overall_pass_rate"] == 1.0:
            print("\nAll test cases passing! Stopping.")
            break

        # Propose new prompt (skip on last iteration)
        if iteration < max_iterations - 1:
            # Observer: update optimizer strategy every 3 iterations
            if iteration >= 2 and iteration % 3 == 2:
                _log("  Observer updating optimizer prompt...", dim=True)
                try:
                    new_opt = _observe_and_update_optimizer(
                        obs_client,
                        obs_model,
                        current_optimizer_system,
                        results["iterations"],
                        provider=obs_provider,
                    )
                    if new_opt != current_optimizer_system:
                        opt_diff = _simple_diff(current_optimizer_system, new_opt)
                        _log(f"  Observer diff:\n{opt_diff}", dim=True)
                        current_optimizer_system = new_opt
                    else:
                        _log("  Observer: no changes", dim=True)
                except Exception as e:
                    _log(f"  Observer error: {e}", dim=True)

            # Build parent scores for improvement-based feedback (delta signal).
            # Use the parent node's last evaluation, not the current node's.
            parent_scores: dict[str, float] | None = None
            if selected.parent_id is not None:
                for prev_iter in reversed(results["iterations"][:-1]):
                    if prev_iter.get("tree_node_id") == selected.parent_id:
                        parent_scores = {
                            cid: cr["pass_rate"] for cid, cr in prev_iter["cases"].items()
                        }
                        break

            # Filter out holdout cases from optimizer feedback
            opt_cases, opt_result = _filter_for_optimizer(
                cases,
                iter_result,
                holdout_ids,
            )

            # Phase 1: Analyst diagnoses semantic patterns
            analyst_output = ""
            if opt_result["aggregate"].get("overall_pass_rate", 0) < 1.0:
                print("\nAnalyzing failures...")
                try:
                    analyst_output = _run_analyst(
                        client=ana_client,
                        model=ana_model,
                        test_cases=opt_cases,
                        iteration_result=opt_result,
                        provider=ana_provider,
                        optimize_tools=optimize_tools,
                        tool_overrides=selected.tool_overrides or None,
                    )
                    if analyst_output:
                        _log(f"  Analyst:\n{analyst_output}", dim=True)
                except Exception as e:
                    _log(f"  Analyst failed: {e}", dim=True)

            # Store analyst output before optimizer (survives optimizer failure)
            if analyst_output:
                iter_result["analyst"] = analyst_output

            # Phase 2: Tool description optimizer
            new_tool_overrides = selected.tool_overrides
            if optimize_tools:
                print("\nOptimizing tool descriptions...")
                try:
                    new_tool_overrides = _propose_tool_overrides(
                        client=tool_opt_client,
                        model=tool_opt_model,
                        current_tools=TOOLS,
                        current_overrides=selected.tool_overrides,
                        test_cases=opt_cases,
                        iteration_result=opt_result,
                        analyst_output=analyst_output,
                        provider=tool_opt_provider,
                    )
                    if new_tool_overrides != selected.tool_overrides:
                        added_tools = set(new_tool_overrides) - set(selected.tool_overrides)
                        modified_tools = set(new_tool_overrides) & set(selected.tool_overrides)
                        parts = []
                        if added_tools:
                            parts.append(f"added: {', '.join(sorted(added_tools))}")
                        if modified_tools:
                            parts.append(f"updated: {', '.join(sorted(modified_tools))}")
                        _log(f"  Tool overrides: {'; '.join(parts)}", dim=True)

                        # Show description diffs for each changed tool
                        base_index = {t["function"]["name"]: t for t in TOOLS}
                        prev_overrides = selected.tool_overrides or {}
                        for tname in sorted(set(new_tool_overrides) | set(prev_overrides)):
                            base_desc = (
                                base_index.get(tname, {}).get("function", {}).get("description", "")
                            )
                            old_desc = prev_overrides.get(tname, {}).get("description", base_desc)
                            new_desc = new_tool_overrides.get(tname, {}).get(
                                "description", base_desc
                            )
                            if old_desc != new_desc:
                                print(f"\n  {BOLD}{tname}{RESET}:")
                                print(f"    {DIM}old: {old_desc}{RESET}")
                                print(f"    new: {new_desc}")

                        iter_result["tool_overrides"] = new_tool_overrides
                except Exception as e:
                    _log(f"  Tool optimization failed: {e}", dim=True)
                    if optimize_tools:
                        continue

            # Phase 3: Prompt optimizer (skipped in --optimize-tools mode)
            new_prompt = selected.prompt
            if not optimize_tools:
                print("\nOptimizing prompt...")
                try:
                    new_prompt = _propose_prompt_modification(
                        client=opt_client,
                        model=opt_model,
                        current_prompt=selected.prompt,
                        test_cases=opt_cases,
                        iteration_result=opt_result,
                        history=results["iterations"],
                        optimizer_system=current_optimizer_system,
                        provider=opt_provider,
                        parent_scores=parent_scores,
                        analyst_output=analyst_output,
                        tool_overrides=new_tool_overrides or None,
                    )
                except Exception as e:
                    _log(f"  Prompt modification failed: {e}", dim=True)
                    continue

            prompt_changed = new_prompt != selected.prompt
            tools_changed = new_tool_overrides != selected.tool_overrides

            if prompt_changed:
                diff = _simple_diff(selected.prompt, new_prompt)
                print(f"Prompt modified ({len(selected.prompt)} -> {len(new_prompt)} chars)")
                if diff:
                    print(diff)
                iter_result["prompt_diff"] = diff

            if prompt_changed or tools_changed:
                # Add child node to evolution tree
                child = EvolutionNode(
                    node_id=next_node_id,
                    parent_id=selected_id,
                    prompt=new_prompt,
                    tool_overrides=new_tool_overrides,
                    iteration=iteration,
                    optimizer_system=current_optimizer_system,
                )
                nodes[next_node_id] = child
                selected.children.append(next_node_id)
                iter_result["tree_child_id"] = next_node_id
                _log(
                    f"  Created node {next_node_id} (child of {selected_id},"
                    f" tree={len(nodes)} nodes)",
                    dim=True,
                )
                next_node_id += 1

                # Re-write with tree update and diff
                results["tree"] = [_node_to_dict(n) for n in nodes.values()]
                with open(output_file, "w") as f:
                    json.dump(results, f, indent=2)
            else:
                print("No changes to prompt or tool descriptions. Stopping.")
                break

    # Report best node
    best_node = max(
        (n for n in nodes.values() if n.visit_count > 0),
        key=lambda n: n.score,
        default=nodes[0],
    )
    print(
        f"\nBest node: {best_node.node_id} "
        f"(score={best_node.score:.2%}, visits={best_node.visit_count})"
    )
    if best_node.tool_overrides:
        print(f"  Tool overrides: {', '.join(sorted(best_node.tool_overrides))}")

    # Save optimized tool descriptions back to JSON files
    if save_tools and best_node.tool_overrides:
        from turnstone.core.tools import _TOOLS_DIR

        for tool_name, override in best_node.tool_overrides.items():
            tool_path = _TOOLS_DIR / f"{tool_name}.json"
            if not tool_path.exists():
                continue
            with open(tool_path) as f:
                tool_json = json.load(f)
            if "description" in override:
                tool_json["description"] = override["description"]
            for param, desc in override.get("parameters", {}).items():
                if param in tool_json.get("parameters", {}).get("properties", {}):
                    tool_json["parameters"]["properties"][param]["description"] = desc
            with open(tool_path, "w") as f:
                json.dump(tool_json, f, indent=2)
                f.write("\n")
            print(f"  Saved: {tool_path}")

    print(f"Results written to {output_file}")
    return results


# ─── CLI ─────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Prompt optimization and evaluation for turnstone",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Examples:
              # Run evaluation with default prompt
              turnstone-optimizer tests.json

              # Run with custom initial prompt from file
              turnstone-optimizer tests.json --prompt prompt.txt

              # Single evaluation pass (no optimization)
              turnstone-optimizer tests.json --no-optimize

              # Configure runs and iterations
              turnstone-optimizer tests.json --n-runs 5 --max-iter 10
        """),
    )
    parser.add_argument(
        "test_file",
        help="Path to test cases JSON file",
    )
    parser.add_argument(
        "--base-url",
        default="http://localhost:8000/v1",
        help="API base URL (default: http://localhost:8000/v1)",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Model name (default: auto-detect)",
    )
    parser.add_argument(
        "--optimizer-model",
        default=None,
        help="Model for prompt optimization (default: same as --model)",
    )
    parser.add_argument(
        "--optimizer-base-url",
        default=None,
        help="Base URL for optimizer model (default: same as --base-url)",
    )
    parser.add_argument(
        "--observer-model",
        default=None,
        help="Model for meta-optimization (default: same as --optimizer-model)",
    )
    parser.add_argument(
        "--observer-base-url",
        default=None,
        help="Base URL for observer model (default: same as --optimizer-base-url)",
    )
    parser.add_argument(
        "--analyst-model",
        default=None,
        help="Model for failure analysis (default: same as --optimizer-model)",
    )
    parser.add_argument(
        "--analyst-base-url",
        default=None,
        help="Base URL for analyst model (default: same as --optimizer-base-url)",
    )
    parser.add_argument(
        "--diversifier-model",
        default=None,
        help="Model for prompt diversification (default: same as --optimizer-model)",
    )
    parser.add_argument(
        "--diversifier-base-url",
        default=None,
        help="Base URL for diversifier model (default: same as --optimizer-base-url)",
    )
    parser.add_argument(
        "--diversify",
        type=int,
        default=0,
        help="Generate N prompt variants per test case (0=disabled, includes original)",
    )
    parser.add_argument(
        "--save-variants",
        action="store_true",
        help="Save generated variants back to test suite JSON for caching",
    )
    parser.add_argument(
        "--optimize-tools",
        action="store_true",
        help="Optimize tool descriptions only (freeze system prompt). "
        "Useful after system prompt optimization plateaus",
    )
    parser.add_argument(
        "--tool-optimizer-model",
        default=None,
        help="Model for tool description optimization (default: same as --optimizer-model)",
    )
    parser.add_argument(
        "--tool-optimizer-base-url",
        default=None,
        help="Base URL for tool optimizer model (default: same as --optimizer-base-url)",
    )
    parser.add_argument(
        "--save-tools",
        action="store_true",
        help="Write optimized tool descriptions back to turnstone/tools/*.json",
    )
    parser.add_argument(
        "--prompt",
        default=None,
        help="Path to initial prompt text file (default: use turnstone's built-in)",
    )
    parser.add_argument(
        "--n-runs",
        type=int,
        default=None,
        help="Number of runs per test case (default: from tests.json or 3)",
    )
    parser.add_argument(
        "--max-iter",
        type=int,
        default=5,
        help="Maximum optimization iterations (default: 5)",
    )
    parser.add_argument(
        "--no-optimize",
        action="store_true",
        help="Run evaluation only, no prompt optimization",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=0.7,
        help="Sampling temperature (default: 0.7)",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=32768,
        help="Max completion tokens (default: 32768)",
    )
    parser.add_argument(
        "--reasoning-effort",
        default="medium",
        choices=["low", "medium", "high"],
        help="Reasoning effort (default: medium)",
    )
    parser.add_argument(
        "--context-window",
        type=int,
        default=131072,
        help="Context window size (default: 131072)",
    )
    parser.add_argument(
        "--output",
        default="eval_results.json",
        help="Output results file (default: eval_results.json)",
    )
    parser.add_argument(
        "--test-timeout",
        type=int,
        default=300,
        help="Per-test timeout in seconds (default: 300)",
    )
    parser.add_argument(
        "--suite-timeout",
        type=int,
        default=0,
        help="Total suite timeout in seconds (default: unlimited)",
    )
    parser.add_argument(
        "--no-fast-fail",
        action="store_true",
        help="Disable early termination when all initial runs score 0.0",
    )
    parser.add_argument(
        "--parallel",
        type=int,
        default=1,
        help="Parallel workers (default: 1=serial, 0=auto)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Show detailed per-turn logging (API calls, tool args, results)",
    )
    parser.add_argument(
        "--explore-constant",
        type=float,
        default=1.414,
        help="UCB exploration constant C (default: sqrt(2) ≈ 1.414)",
    )
    from turnstone.core.config import add_config_arg, apply_config

    add_config_arg(parser)
    apply_config(parser, ["api", "model"])
    args = parser.parse_args()

    # Load initial prompt if provided
    initial_prompt = None
    if args.prompt:
        with open(args.prompt) as f:
            initial_prompt = f.read()

    max_iter = 1 if args.no_optimize else args.max_iter

    run_optimization(
        base_url=args.base_url,
        model=args.model,
        test_file=args.test_file,
        initial_prompt=initial_prompt,
        n_runs=args.n_runs,
        max_iterations=max_iter,
        temperature=args.temperature,
        max_tokens=args.max_tokens,
        reasoning_effort=args.reasoning_effort,
        output_file=args.output,
        context_window=args.context_window,
        verbose=args.verbose,
        test_timeout=args.test_timeout,
        suite_timeout=args.suite_timeout,
        fast_fail=not args.no_fast_fail,
        parallel=args.parallel if args.parallel != 0 else (os.cpu_count() or 4),
        optimizer_base_url=args.optimizer_base_url,
        optimizer_model=args.optimizer_model,
        observer_base_url=args.observer_base_url,
        observer_model=args.observer_model,
        analyst_base_url=args.analyst_base_url,
        analyst_model=args.analyst_model,
        diversifier_base_url=args.diversifier_base_url,
        diversifier_model=args.diversifier_model,
        diversify=args.diversify,
        save_variants=args.save_variants,
        optimize_tools=args.optimize_tools,
        tool_optimizer_base_url=args.tool_optimizer_base_url,
        tool_optimizer_model=args.tool_optimizer_model,
        save_tools=args.save_tools,
        explore_constant=args.explore_constant,
    )


if __name__ == "__main__":
    main()
