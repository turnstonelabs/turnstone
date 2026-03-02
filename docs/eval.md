# Evaluation and Prompt Optimization (turnstone-eval)

`turnstone-eval` is the evaluation and prompt optimization system for turnstone. It
runs test cases against the LLM, scores tool call sequences against expected
actions, and optionally uses the model to self-optimize the developer prompt.

Source: `turnstone/eval.py`

---

## Overview

The system works in an iterative loop:

1. Run each test case N times against the current developer prompt.
2. Score each run by comparing the actual tool call sequence to expected actions.
3. If not all tests pass, use the model to rewrite the prompt based on failures.
4. Repeat until all tests pass or max iterations are reached.

When optimization is disabled (`--no-optimize`), only step 1 and 2 execute
(a single iteration).

---

## Test Case Format

Test suites are JSON files with this structure:

```json
{
  "defaults": {
    "n_runs": 3
  },
  "cases": [
    {
      "id": "test_name",
      "user_prompt": "the prompt to send to the model",
      "setup": {
        "files": {
          "filename.py": "file content here",
          "src/utils.py": "another file"
        }
      },
      "expected_actions": [
        {"tool": "read_file", "args": {"path": "filename.py"}},
        {"tool": "bash", "args_pattern": {"command": "python.*test"}},
        {"tool": "edit_file"}
      ],
      "match_mode": "ordered_subset",
      "max_turns": 10,
      "n_runs": 5
    }
  ]
}
```

### Fields

| Field              | Required | Default            | Description |
|--------------------|----------|--------------------|-------------|
| `id`               | yes      | --                 | Unique test case identifier. |
| `user_prompt`      | yes      | --                 | The message sent to the model. |
| `setup.files`      | no       | `{}`               | Files to create in the temp directory before running. Keys are relative paths, values are file content. |
| `expected_actions`  | no       | `[]`               | List of expected tool calls to match against. |
| `match_mode`       | no       | `"ordered_subset"` | How to match actual vs expected actions (see Scoring). |
| `max_turns`        | no       | `10`               | Maximum conversation turns before stopping. |
| `n_runs`           | no       | suite default or 3 | Per-case override for number of runs. |

### Expected Action Specs

Each entry in `expected_actions` can contain:

- `tool` (required): The tool name to match (e.g. `"read_file"`, `"bash"`).
- `args`: Exact key-value matching. Each key in `args` must exist in the actual call with the same string value.
- `args_pattern`: Regex key-value matching. Each key's value is a regex pattern tested against the actual argument value.
- If neither `args` nor `args_pattern` is specified, only the tool name is matched.

---

## Scoring

Scoring is handled by `score_run()`, which compares a run's tool call log
against the expected actions.

### Match Modes

| Mode              | Description |
|-------------------|-------------|
| `exact`           | Tool calls must match expected actions in exact order and exact count. Extra or missing calls cause failure. |
| `ordered_subset`  | Expected actions must appear in order within the actual tool log, but extra calls between them are allowed. This is the default. |
| `subset`          | Expected actions must all appear somewhere in the tool log, in any order. Each actual call can only match one expected action. |
| `contains_any`    | Passes if at least one expected action appears anywhere in the tool log. |

### Action Matching (`_match_action`)

A single actual tool call matches an expected action when:

1. The tool names are equal.
2. If `args` is specified: every key in `args` must exist in the actual call's
   arguments with the same string value (partial key matching -- extra actual
   args are ignored).
3. If `args_pattern` is specified: every key's regex pattern must match the
   corresponding actual argument value via `re.search()`.
4. If the actual args contain only `_raw` (unparseable JSON fallback), the
   action matches only when no `args` or `args_pattern` is expected.

### Score Calculation

- **Score** = number of matched expected actions / total expected actions.
- **Pass** = score equals 1.0 (all expected actions matched).
- The return dict includes: `pass`, `score`, `matched` (indices), `unmatched`
  (indices), `extra_tools`, and `detail` (human-readable summary).

### JSON Dump Detection

When a run fails and the model's final text content contains JSON that looks
like a tool call (keys like `"tool"`, `"command"`, `"path"`), the run is
flagged with `json_dump: true`. This indicates the model tried to call a tool
but emitted JSON as text instead of using the function-calling interface.

---

## HeadlessSession

`HeadlessSession` extends `ChatSession` for headless evaluation. It provides
deterministic, non-interactive execution suitable for automated testing.

### Differences from ChatSession

| Aspect          | ChatSession             | HeadlessSession            |
|-----------------|-------------------------|----------------------------|
| Streaming       | Streaming API           | Non-streaming (`stream=False`) |
| Tool approval   | User confirmation       | `auto_approve = True`       |
| UI              | Terminal/Web UI         | `NullUI` (discards output)  |
| Stdout          | Normal                  | Suppressed during execution |
| Tool logging    | Display only            | Structured `tool_call_log`  |
| System prompt   | Built-in developer prompt | Overridable via constructor |

### NullUI

A minimal UI adapter that satisfies the `SessionUI` protocol by discarding
all output. `approve_tools()` always returns `(True, None)`.

### send_headless()

```python
def send_headless(
    self,
    user_input: str,
    max_turns: int = 10,
    verbose: bool = False,
    log_prefix: str = "",
) -> list[dict]:
```

Runs a complete multi-turn conversation:

1. Appends the user message.
2. Calls the model API (non-streaming).
3. If tool calls are returned, executes them (with stdout suppressed) and
   logs each call to `self.tool_call_log`.
4. Repeats up to `max_turns` or until the model responds without tool calls.
5. Returns the tool call log: list of dicts with keys `tool`, `args`,
   `result` (truncated to 500 chars), and `turn`.

Parallel tool calls are capped at 10 per turn to prevent degenerate repetition.

### Retry Logic

`send_headless()` is called inside `_run_single_test()` with retry logic:
3 attempts with exponential backoff (sleep `2^attempt` seconds) on any
exception. This prevents transient API errors from poisoning eval scores.

---

## Test Execution

Each test case runs in isolation:

1. A fresh temp directory is created.
2. Setup files are written to the temp directory.
3. The working directory is changed to the temp directory.
4. A new `HeadlessSession` is created with the current developer prompt.
5. `send_headless()` runs the user prompt through the conversation loop.
6. The tool log is scored against expected actions.
7. The temp directory is cleaned up.

The memory database is also isolated per test (an ephemeral SQLite database
in the temp directory) so tests do not pollute each other or the user's
real memory store.

---

## Optimization Loop

`run_optimization()` is the main entry point for iterative prompt optimization.

### Flow

```
for iteration in 0..max_iterations:
    1. Run all test cases n_runs times with current prompt
    2. Score and aggregate results
    3. Save intermediate results to JSON
    4. If all tests pass -> stop
    5. Every 3 iterations (at iteration 2, 5, 8, ...):
       -> Observer reviews optimizer strategy
       -> Reset prompt to best-performing iteration
    6. Propose new prompt via optimizer model call
    7. If prompt unchanged -> stop
    8. Continue with new prompt
```

### Prompt Proposal (`_propose_prompt_modification`)

Uses the model to rewrite the developer prompt based on test results:

- **Input**: Current prompt, test case definitions, per-case results with
  actual vs expected tool sequences, and a history of the last 3 iterations.
- **Optimizer system prompt** (`OPTIMIZER_SYSTEM`): Instructs the model to
  act as a text rewriter. Key guidance includes:
  - Address critical failure modes (text-only responses, write_file vs edit_file,
    unnecessary search before create, missing plan calls).
  - Preserve phrasing that drives 100% pass rate on passing tests.
  - Use direct imperative style with concrete tool call examples.
  - Stay within 130% of original prompt length.
- **Output**: The rewritten prompt text (stripped of reasoning tags and code fences).

### Observer System (`_observe_and_update_optimizer`)

Every 3 iterations, a meta-level "observer" reviews the optimizer's strategy:

- Analyzes the iteration history: score trends, regressions, prompt length changes,
  and diffs between iterations.
- Summarizes the optimizer's behavioral patterns (list style, header usage, length).
- Uses `OBSERVER_SYSTEM` to rewrite the optimizer's own system prompt.
- Rejects degenerate outputs (over 200% of input length).
- After updating the optimizer prompt, resets the developer prompt to the
  best-performing iteration so far.

This two-level optimization (optimizer + observer) helps the system escape
local minima and adjust its rewriting strategy.

### Result Persistence

After each iteration, results are written to the output JSON file. The
structure is:

```json
{
  "meta": {
    "model": "model-name",
    "base_url": "http://localhost:8000/v1",
    "started": "2025-01-01T00:00:00",
    "test_suite": "tests.json",
    "n_runs_default": 3
  },
  "iterations": [
    {
      "iteration": 0,
      "prompt": "the developer prompt used",
      "prompt_diff": null,
      "optimizer_system": "the optimizer system prompt",
      "timestamp": "2025-01-01T00:01:00",
      "cases": {
        "test_name": {
          "runs": [
            {
              "pass": true,
              "score": 1.0,
              "matched": [0, 1],
              "unmatched": [],
              "extra_tools": [],
              "detail": "Ordered subset: 2/2",
              "tool_sequence": ["read_file", "edit_file"],
              "tool_args": [{"read_file": {"path": "f.py"}}, ...],
              "elapsed": 3.2
            }
          ],
          "pass_rate": 1.0,
          "avg_score": 1.0
        }
      },
      "aggregate": {
        "total_cases": 5,
        "total_runs": 15,
        "overall_pass_rate": 0.8,
        "overall_avg_score": 0.87,
        "json_dumps": 0,
        "per_case_pass_rates": {"test_name": 1.0, ...}
      }
    }
  ]
}
```

---

## CLI Usage

The entry point is `turnstone-eval` (installed as a console script) or
`python -m turnstone.eval`.

```
turnstone-eval tests.json                          # evaluate + optimize
turnstone-eval tests.json --no-optimize            # evaluate only (single iteration)
turnstone-eval tests.json --n-runs 5 --max-iter 10 # more thorough evaluation
turnstone-eval tests.json --prompt custom.txt      # start from a custom prompt
turnstone-eval tests.json -v                       # verbose per-turn logging
```

### All Options

| Flag                | Default                       | Description |
|---------------------|-------------------------------|-------------|
| `test_file`         | (positional, required)        | Path to test cases JSON file. |
| `--base-url`        | `http://localhost:8000/v1`    | API base URL. |
| `--model`           | auto-detect                   | Model name. Auto-detected from the API if not specified. |
| `--prompt`          | turnstone built-in prompt         | Path to initial prompt text file. |
| `--n-runs`          | from tests.json or 3          | Number of runs per test case. |
| `--max-iter`        | 5                             | Maximum optimization iterations. |
| `--no-optimize`     | false                         | Run evaluation only (sets max-iter to 1). |
| `--temperature`     | 0.7                           | Sampling temperature. |
| `--max-tokens`      | 32768                         | Max completion tokens. |
| `--reasoning-effort` | `medium`                     | Reasoning effort: `low`, `medium`, or `high`. |
| `--context-window`  | 131072                        | Context window size. |
| `--output`          | `eval_results.json`           | Output results file path. |
| `-v`, `--verbose`   | false                         | Show detailed per-turn logging (API calls, tool args, results). |

### Precedence for n_runs

The number of runs per test case is resolved in this order:

1. Per-case `n_runs` field in the test case definition.
2. CLI `--n-runs` argument (if provided).
3. Suite-level `defaults.n_runs` in the test JSON file.
4. Code default: 3.
