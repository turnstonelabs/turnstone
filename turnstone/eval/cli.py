#!/usr/bin/env python3
"""cli.py — measure-only ``turnstone-eval`` entry point.

Runs a test suite once against a model and prints a scored summary. This is the
old ``--no-optimize`` behaviour promoted to the whole job: it calls
:func:`turnstone.eval.core._run_iteration` exactly once and reports the result.
Prompt optimization lives in :mod:`turnstone.optimizer` (``turnstone-optimizer``).

Usage:
    turnstone-eval tests.json
    turnstone-eval tests.json --prompt prompt.txt --n-runs 5 --parallel 4
"""

import argparse
import json
import os
import re
import textwrap
from datetime import datetime
from typing import Any

from openai import OpenAI

from turnstone.core.session import ChatSession
from turnstone.eval.core import (
    NullUI,
    _append_summary_tsv,
    _print_skill_adherence_table,
    _print_summary_table,
    _run_iteration,
    run_skill_adherence,
)


def _run_skill_adherence_cli(
    args: argparse.Namespace,
    client: OpenAI,
    model: str,
    api_key: str,
) -> None:
    """Load a skill-scenario dataset and report per-case adherence lift."""
    with open(args.test_file) as f:
        suite: dict[str, Any] = json.load(f)

    cases: list[dict[str, Any]] = suite["cases"]
    for i, case in enumerate(cases):
        if "id" not in case:
            raise SystemExit(f"Test case {i} missing required 'id' field")
        if "user_prompt" not in case:
            raise SystemExit(f"Test case '{case.get('id', i)}' missing 'user_prompt'")
        skill = case.get("skill")
        if skill is not None and (
            not isinstance(skill, dict) or not skill.get("name") or not skill.get("content")
        ):
            raise SystemExit(
                f"Test case '{case['id']}' has a malformed 'skill' — it must be an "
                "object with non-empty 'name' and 'content'"
            )
    if not any(c.get("skill") for c in cases):
        raise SystemExit("No cases carry a 'skill' — nothing to measure for adherence")

    defaults = suite.get("defaults", {})
    resolved_n_runs: int = (
        args.n_runs if args.n_runs is not None else int(defaults.get("n_runs", 3))
    )
    parallel = args.parallel if args.parallel != 0 else (os.cpu_count() or 4)

    result = run_skill_adherence(
        client=client,
        base_url=args.base_url,
        api_key=api_key,
        model=model,
        cases=cases,
        n_runs=resolved_n_runs,
        temperature=args.temperature,
        max_tokens=args.max_tokens,
        reasoning_effort=args.reasoning_effort,
        context_window=args.context_window,
        test_timeout=args.test_timeout,
        parallel=parallel,
        verbose=args.verbose,
    )
    result["meta"] = {
        "model": model,
        "base_url": args.base_url,
        "test_suite": args.test_file,
        "n_runs": resolved_n_runs,
        "started": datetime.now().isoformat(),
    }

    _print_skill_adherence_table(result)

    with open(args.output, "w") as f:
        json.dump(result, f, indent=2)
        f.write("\n")
    print(f"Results written to {args.output}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Headless measurement for turnstone (scores tool use against expected actions)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Examples:
              # Measure with the built-in developer prompt
              turnstone-eval tests.json

              # Measure a custom prompt with more runs, in parallel
              turnstone-eval tests.json --prompt prompt.txt --n-runs 5 --parallel 4

            Prompt optimization now lives in turnstone-optimizer.
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
        "--skill-adherence",
        action="store_true",
        help=(
            "Measure skill adherence: for each case carrying a 'skill', run a "
            "treatment arm (skill applied via the real set_skill composition "
            "path) vs a control arm (no skill) and report the pass-rate lift"
        ),
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Show detailed per-turn logging (API calls, tool args, results)",
    )
    from turnstone.core.config import add_config_arg, apply_config

    add_config_arg(parser)
    apply_config(parser, ["api", "model"])
    args = parser.parse_args()

    api_key = os.environ.get("OPENAI_API_KEY", "dummy")
    client = OpenAI(
        base_url=args.base_url,
        api_key=api_key,
    )

    model = args.model
    if not model:
        from turnstone.core.model_registry import detect_model

        detected, _ = detect_model(client)
        assert detected is not None  # fatal=True guarantees non-None or SystemExit
        model = detected

    # Skill-adherence mode is a distinct two-arm measurement — it uses natural
    # prompt composition (no --prompt), so branch before the initial-prompt path.
    if args.skill_adherence:
        _run_skill_adherence_cli(args, client, model, api_key)
        return

    # Resolve the initial prompt: --prompt file, else turnstone's built-in.
    initial_prompt: str | None = None
    if args.prompt:
        with open(args.prompt) as f:
            initial_prompt = f.read()
    if initial_prompt is None:
        # Extract the default developer message from a temporary ChatSession
        tmp = ChatSession(
            client=client,
            model=model,
            ui=NullUI(),
            instructions=None,
            temperature=args.temperature,
            max_tokens=args.max_tokens,
            tool_timeout=30,
            reasoning_effort=args.reasoning_effort,
            context_window=args.context_window,
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

    # Load test cases
    with open(args.test_file) as f:
        suite: dict[str, Any] = json.load(f)

    cases: list[dict[str, Any]] = suite["cases"]
    for i, case in enumerate(cases):
        if "id" not in case:
            raise SystemExit(f"Test case {i} missing required 'id' field")
        if "user_prompt" not in case:
            raise SystemExit(f"Test case '{case.get('id', i)}' missing 'user_prompt'")
    defaults = suite.get("defaults", {})
    # Precedence: CLI arg (non-None) > tests.json defaults > code default (3)
    resolved_n_runs: int = (
        args.n_runs if args.n_runs is not None else int(defaults.get("n_runs", 3))
    )

    # Auto-load cached prompt variants if present (parity with the optimizer path).
    prompt_variants: dict[str, list[str]] | None = None
    cached_variants = {
        c["id"]: c["user_prompts"]
        for c in cases
        if isinstance(c.get("user_prompts"), list) and len(c["user_prompts"]) > 1
    }
    if cached_variants:
        prompt_variants = cached_variants
        total_v = sum(len(v) for v in cached_variants.values())
        print(
            f"\n  Using cached variants for {len(cached_variants)} cases ({total_v} total prompts)"
        )

    parallel = args.parallel if args.parallel != 0 else (os.cpu_count() or 4)

    iter_result = _run_iteration(
        client=client,
        model=model,
        system_prompt=initial_prompt,
        cases=cases,
        n_runs=resolved_n_runs,
        temperature=args.temperature,
        max_tokens=args.max_tokens,
        reasoning_effort=args.reasoning_effort,
        context_window=args.context_window,
        verbose=args.verbose,
        test_timeout=args.test_timeout,
        fast_fail=not args.no_fast_fail,
        parallel=parallel,
        base_url=args.base_url,
        api_key=api_key,
        prompt_variants=prompt_variants,
    )
    iter_result["iteration"] = 0
    iter_result["prompt"] = initial_prompt
    iter_result["timestamp"] = datetime.now().isoformat()

    _print_summary_table(iter_result)

    results = {
        "meta": {
            "model": model,
            "base_url": args.base_url,
            "test_suite": args.test_file,
            "n_runs_default": resolved_n_runs,
            "started": iter_result["timestamp"],
        },
        "iterations": [iter_result],
    }
    with open(args.output, "w") as f:
        json.dump(results, f, indent=2)
        f.write("\n")

    tsv_path = os.path.splitext(args.output)[0] + ".tsv"
    _append_summary_tsv(tsv_path, iter_result, [c["id"] for c in cases])

    print(f"Results written to {args.output}")


if __name__ == "__main__":
    main()
