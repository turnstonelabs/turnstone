# Evaluation and Prompt Optimization (turnstone-eval)

`turnstone-eval` is the evaluation and prompt optimization system for turnstone. It
runs test cases against the LLM, scores tool call sequences against expected
actions, and optionally uses a multi-agent pipeline to optimize the developer
prompt and tool descriptions.

Source: `turnstone/eval.py`

---

## Overview

The system uses UCB tree search to explore prompt variants:

1. Maintain an **evolution tree** of prompt variants, starting from the initial prompt.
2. Each iteration, **UCB1 selects** the most promising node to evaluate.
3. Run each test case N times against the selected prompt.
4. Score each run by comparing the actual tool call sequence to expected actions.
5. If not all tests pass, run a **three-phase optimization pipeline**:
   - Phase 1: Analyst diagnoses semantic failure patterns
   - Phase 2: Tool optimizer adjusts tool descriptions (when `--optimize-tools`)
   - Phase 3: Prompt optimizer proposes a child variant (when not `--optimize-tools`)
6. Add the child to the tree and repeat until all tests pass or max iterations reached.

This approach (inspired by [Learning to Self-Evolve](https://arxiv.org/abs/2603.18620))
prevents irrecoverable collapse from bad edits — UCB naturally backtracks to
high-scoring ancestors instead of following a linear chain.

When optimization is disabled (`--no-optimize`), only steps 2-4 execute
(a single iteration evaluating the root node).

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
| `holdout`          | no       | `false`            | If `true`, this case is evaluated but excluded from optimizer feedback. Used to measure progress without overfitting. |

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
| Cancellation    | N/A                     | `_cancelled` event for timeout cleanup |

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
2. Checks `_cancelled` event — stops if set (timeout cleanup).
3. Calls the model API (non-streaming).
4. If tool calls are returned, executes them (with stdout suppressed) and
   logs each call to `self.tool_call_log`.
5. Repeats up to `max_turns` or until the model responds without tool calls.
6. Returns the tool call log: list of dicts with keys `tool`, `args`,
   `result` (truncated to 500 chars), and `turn`.

Parallel tool calls are capped at 10 per turn to prevent degenerate repetition.

### Timeout and Cancellation

Each test runs in a `ThreadPoolExecutor(max_workers=1)` with a per-test
timeout (`--test-timeout`). Each attempt gets its own `OpenAI` client with
a matching httpx read timeout. On timeout, three layers of defense prevent
zombie connections:

1. **httpx timeout**: Per-request read timeout aborts the HTTP call and
   releases the server slot.
2. **`_cancelled` event**: Prevents the orphan thread from starting new turns.
3. **`run_client.close()`**: Closes the connection pool to abort any
   in-flight request.

### Retry Logic

`send_headless()` is called inside `_run_single_test()` with retry logic:
3 attempts with exponential backoff (sleep `2^attempt` seconds) on any
exception. `TimeoutError` is re-raised immediately (no retry).

---

## Test Execution

Each test case runs in isolation:

1. A fresh temp directory is created.
2. Setup files are written to the temp directory.
3. The working directory is changed to the temp directory.
4. A per-attempt `OpenAI` client is created with httpx timeout matching `--test-timeout`.
5. A new `HeadlessSession` is created with the current developer prompt.
6. `send_headless()` runs the user prompt through the conversation loop.
7. The tool log is scored against expected actions.
8. The temp directory is cleaned up.

The memory database is also isolated per test (an ephemeral SQLite database
in the temp directory) so tests do not pollute each other or the user's
real memory store.

### Parallel Execution

With `--parallel N` (N > 1), tests run in a `ProcessPoolExecutor` with N
workers. Each subprocess creates its own `OpenAI` client. This is suitable
for remote API endpoints but will overwhelm local inference servers. The
default (`--parallel 1`) runs tests serially.

---

## Model Roles

The eval pipeline uses up to five separate model roles, each independently
configurable. All roles inherit from the test model by default, with a
cascade chain:

```
test model (--base-url, --model)
  └─ optimizer (--optimizer-*)
       ├─ observer (--observer-*)
       ├─ analyst (--analyst-*)
       ├─ diversifier (--diversifier-*)
       └─ tool optimizer (--tool-optimizer-*)
```

| Role | Purpose | When it runs |
|------|---------|--------------|
| **Test** | The model being evaluated | Every iteration |
| **Analyst** | Diagnoses semantic failure patterns with tool use | When pass rate < 100% |
| **Optimizer** | Rewrites the developer prompt | Every iteration (unless `--optimize-tools`) |
| **Tool optimizer** | Rewrites tool descriptions | When `--optimize-tools` is set |
| **Observer** | Tunes the optimizer's strategy | Every 3 iterations |
| **Diversifier** | Generates prompt paraphrases | Once before the loop (when `--diversify N`) |

Typical setup: local model for test, Opus for analyst, Sonnet for
optimizer/observer/diversifier.

---

## Optimization Pipeline

### Flow

```
for iteration in 0..max_iterations:
    1. UCB select → pick the most promising tree node
    2. Run all test cases n_runs times with selected node's prompt
    3. Update node score (rolling mean) and visit count
    4. Save intermediate results + tree state to JSON
    5. If all tests pass → stop
    6. Phase 1: Analyst diagnoses semantic failure patterns
    7. Phase 2 (--optimize-tools only): Tool optimizer adjusts descriptions
    8. Phase 3 (default only): Prompt optimizer proposes new prompt
    9. Every 3 iterations: Observer tunes the optimizer's strategy
   10. Add child node to tree (if prompt or tools changed)
```

### Phase 1: Analyst (`_run_analyst`)

A multi-turn agent with `math` (Python) and `bash` tools for computing
statistics. It receives per-case results with failure classifications and
produces a structured diagnosis:

- **Failure patterns**: Shared root causes across failing cases
- **Success/failure contrast**: What distinguishes passing from failing cases
- **Consistency signals**: Systematic (0%), flaky (1-79%), marginal (80-99%)
- **Recommended fixes**: Priority-ordered patterns/examples to add or adjust

The analyst is instructed to frame fixes as patterns and examples, not
imperative rules — this feeds cleaner signal to the optimizer.

In `--optimize-tools` mode, the analyst receives the current tool descriptions
(with any overrides applied) and focuses on tool confusion and description
issues rather than system prompt patterns.

### Phase 2: Tool Optimizer (`_propose_tool_overrides`)

Runs when `--optimize-tools` is set. Receives the current tool descriptions,
confusion failures (where the model picked the wrong tool), and the analyst's
diagnosis. Returns a JSON override dict that modifies tool descriptions.

Overrides are validated against known tool names — only `description` and
`parameters` changes are accepted (no tool renaming at eval time).

After each iteration, changed descriptions are logged as old → new diffs
for easy visual inspection.

### Phase 3: Prompt Optimizer (`_propose_prompt_modification`)

Skipped in `--optimize-tools` mode. Receives the current prompt, test
results with per-case pass rates and deltas from the parent node, and the
analyst's diagnosis. Returns a rewritten prompt.

The optimizer is instructed to prefer patterns over rules — concrete tool
chain examples teach better than imperative directives like "ALWAYS" or
"NEVER." If the current prompt contains rule-heavy language, the optimizer
is guided to replace it with examples.

### Two Optimization Surfaces

The system supports alternating between two optimization surfaces:

1. **System prompt optimization** (default): Freeze tool descriptions,
   optimize the developer prompt. Run until scores plateau.
2. **Tool description optimization** (`--optimize-tools`): Freeze the system
   prompt, optimize tool descriptions only. Run until scores plateau.

Each surface lifts the floor for the other — tool description improvements
may unlock system prompt gains that weren't reachable before, and vice versa.

### Observer (`_observe_and_update_optimizer`)

Every 3 iterations, a meta-level observer reviews the optimizer's strategy:

- Analyzes iteration history: score trends, regressions, prompt length changes,
  and diffs between iterations.
- Detects whether the optimizer is producing rule-heavy or pattern-based output.
- Rewrites the optimizer's own system prompt to correct course.
- Rejects degenerate outputs (over 200% of input length).

### Prompt Diversification

When `--diversify N` is set, the diversifier generates N paraphrased variants
of each test case's user prompt before the optimization loop. Each run cycles
through variants (round-robin), testing robustness across phrasings.

Variants can be cached back to the test suite JSON with `--save-variants`,
and auto-loaded on subsequent runs even without `--diversify`.

---

## Evolution Tree

The optimization maintains a tree of prompt variants (`EvolutionNode`), where
each node stores its prompt text, tool overrides, aggregated score, and visit
count. The root node (ID 0) contains the initial prompt.

**UCB1 selection**: Each iteration picks the node with the highest Upper
Confidence Bound score: `R_bar + C * sqrt(ln(N) / v)`, where `R_bar` is the
node's mean score, `N` is total visits across all nodes, `v` is the node's
visit count, and `C` is the exploration constant (`--explore-constant`,
default sqrt(2)). Unvisited nodes are always selected first.

### Holdout Cases

Test cases with `"holdout": true` are evaluated every iteration but excluded
from the optimizer's feedback. This prevents the optimizer from overfitting
to specific test cases. Node scores are computed from holdout cases only
(when present). If fewer than 2 non-holdout cases remain, holdout is disabled.

### Improvement-Based Feedback

The optimizer sees delta scores (`delta=+20%`) alongside absolute pass rates,
showing how each case improved relative to the parent node's evaluation. This
provides a cleaner signal than absolute scores alone — the optimizer can
distinguish beneficial edits from harmful ones regardless of starting point.

---

## Result Persistence

After each iteration, results are written to the output JSON file. The
structure is:

```json
{
  "meta": {
    "model": "model-name",
    "base_url": "http://localhost:8000/v1",
    "optimizer_model": "claude-opus-4-6",
    "observer_model": "claude-opus-4-6",
    "started": "2025-01-01T00:00:00",
    "test_suite": "tests.json",
    "n_runs_default": 3,
    "explore_constant": 1.414,
    "holdout_ids": [],
    "diversify": 10,
    "prompt_variants": {"case_id": ["variant1", "variant2"]}
  },
  "iterations": [
    {
      "iteration": 0,
      "prompt": "the developer prompt used",
      "prompt_diff": null,
      "optimizer_system": "the optimizer system prompt",
      "analyst": "analyst diagnosis output",
      "tool_overrides": {"bash": {"description": "..."}},
      "timestamp": "2025-01-01T00:01:00",
      "tree_node_id": 0,
      "tree_child_id": 1,
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
        "per_case_pass_rates": {"test_name": 1.0}
      }
    }
  ],
  "tree": [
    {
      "node_id": 0,
      "parent_id": null,
      "prompt": "initial prompt",
      "tool_overrides": {},
      "score": 0.85,
      "visit_count": 3,
      "children": [1, 2],
      "iteration": 0
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
turnstone-eval tests.json --optimize-tools         # optimize tool descriptions only
turnstone-eval tests.json --diversify 10           # test with prompt variants
turnstone-eval tests.json -v                       # verbose per-turn logging
```

### Multi-model setup (local test model, cloud optimizer)

```
turnstone-eval tests.json \
  --base-url http://localhost:8000/v1 \
  --optimizer-base-url https://api.anthropic.com \
  --optimizer-model claude-sonnet-4-6 \
  --analyst-model claude-opus-4-6
```

### All Options

| Flag                    | Default                    | Description |
|-------------------------|----------------------------|-------------|
| `test_file`             | (positional, required)     | Path to test cases JSON file. |
| `--base-url`            | `http://localhost:8000/v1` | API base URL for the test model. |
| `--model`               | auto-detect                | Model name. Auto-detected from the API if not specified. |
| `--prompt`              | turnstone built-in prompt  | Path to initial prompt text file. |
| `--n-runs`              | from tests.json or 3       | Number of runs per test case. |
| `--max-iter`            | 5                          | Maximum optimization iterations. |
| `--no-optimize`         | false                      | Run evaluation only (sets max-iter to 1). |
| `--temperature`         | 0.7                        | Sampling temperature. |
| `--max-tokens`          | 32768                      | Max completion tokens. |
| `--reasoning-effort`    | `medium`                   | Reasoning effort: `low`, `medium`, or `high`. |
| `--context-window`      | 131072                     | Context window size. |
| `--output`              | `eval_results.json`        | Output results file path. |
| `-v`, `--verbose`       | false                      | Show detailed per-turn logging. |
| `--explore-constant`    | 1.414 (sqrt(2))            | UCB exploration constant C. |
| `--test-timeout`        | 300                        | Per-test timeout in seconds. |
| `--suite-timeout`       | 0 (unlimited)              | Total suite timeout in seconds. |
| `--no-fast-fail`        | false                      | Disable early termination on all-zero initial runs. |
| `--parallel`            | 1 (serial)                 | Parallel workers (0=auto, N=use N workers). |
| `--optimizer-model`     | same as `--model`          | Model for prompt optimization. |
| `--optimizer-base-url`  | same as `--base-url`       | Base URL for optimizer model. |
| `--observer-model`      | same as optimizer           | Model for meta-optimization (observer). |
| `--observer-base-url`   | same as optimizer           | Base URL for observer model. |
| `--analyst-model`       | same as optimizer           | Model for failure analysis. |
| `--analyst-base-url`    | same as optimizer           | Base URL for analyst model. |
| `--diversify`           | 0 (disabled)               | Generate N prompt variants per test case. |
| `--diversifier-model`   | same as optimizer           | Model for prompt diversification. |
| `--diversifier-base-url`| same as optimizer           | Base URL for diversifier model. |
| `--save-variants`       | false                      | Save generated variants back to test suite JSON. |
| `--optimize-tools`      | false                      | Optimize tool descriptions only (freeze system prompt). |
| `--tool-optimizer-model` | same as optimizer          | Model for tool description optimization. |
| `--tool-optimizer-base-url` | same as optimizer      | Base URL for tool optimizer model. |
| `--save-tools`          | false                      | Write optimized tool descriptions back to `turnstone/tools/*.json`. |

### Precedence for n_runs

The number of runs per test case is resolved in this order:

1. Per-case `n_runs` field in the test case definition.
2. CLI `--n-runs` argument (if provided).
3. Suite-level `defaults.n_runs` in the test JSON file.
4. Code default: 3.
