# Intent Validation (Judge)

> See also: [Judge Architecture diagram](diagrams/png/22-judge-architecture.png)

Intent validation provides advisory risk assessments for tool calls that require
human approval. An LLM judge evaluates each tool call and presents a structured
verdict alongside the approval prompt, helping users make informed decisions.

## Overview

When a tool call requires approval, the intent validation system runs a two-tier
evaluation:

1. **Heuristic tier** (instant) -- Pattern-based risk classification using a
   rule table. Zero cost, sub-millisecond latency.
2. **LLM judge tier** (async) -- Semantic evaluation using an LLM with
   read-only tool access. Runs on a daemon thread and delivers its verdict
   progressively.

The verdict is purely advisory -- the user always makes the final decision.

The heuristic verdict is attached to the `approve_request` SSE event immediately.
The LLM verdict arrives later via an `intent_verdict` SSE event, allowing the
UI to show a spinner that resolves into a richer assessment. Both verdicts are
persisted to the `intent_verdicts` table for audit and future calibration.

---

## Configuration

### config.toml

```toml
[judge]
enabled = true
model = ""                    # empty = same as session model
provider = ""                 # empty = same as session provider
base_url = ""
api_key = ""
confidence_threshold = 0.7   # reserved for v2 smart approvals (not used in v1)
max_context_ratio = 0.5       # max % of judge context window for history
timeout = 60.0                # seconds (generous for local models)
read_only_tools = true        # judge can use read_file/list_directory
```

All fields are optional. The judge is enabled by default; use `enabled = false`
(or `--no-judge` on the command line) to disable it.

### CLI flags

```
--judge / --no-judge           Enable/disable (default: enabled)
--judge-model MODEL            Model for judge
--judge-provider PROVIDER      Provider for judge
--judge-timeout SECONDS        LLM judge timeout (default: 60)
--judge-confidence FLOAT       Confidence threshold (default: 0.7)
```

CLI flags override `config.toml` values.

---

## Judge Model Selection

- **Default (self-consistency)**: When `model` is empty, the session model
  evaluates its own tool calls. Research shows self-consistency achieves
  comparable accuracy to multi-agent debate at a fraction of the cost.
- **Cross-model**: Use a different model for the judge (e.g. local model for
  the session, commercial model for the judge). Set `model` and `provider`
  in the `[judge]` config section, or use `--judge-model` / `--judge-provider`
  CLI flags.
- **Cross-provider**: When both `model` and `provider` are set, the judge
  creates its own LLM client. You can optionally specify `base_url` and
  `api_key` for non-default endpoints.

---

## Heuristic Rules

The heuristic tier scans a priority-ordered rule table (critical first, low
last) and returns the first matching rule. Each rule has:

- **Tool pattern**: fnmatch glob matched against `func_name` and `approval_label`
- **Argument patterns**: Regex patterns matched against the tool's primary
  argument text (command string for bash, path for file tools, JSON for others)
- **Risk level, confidence, and recommendation**: Pre-assigned per rule

### Rule tiers (36 rules)

| Tier     | Confidence | Recommendation | Examples |
|----------|-----------|----------------|----------|
| Critical | 0.90      | deny           | `rm -rf /`, `mkfs`, `dd if=`, pipe-to-shell, chmod 777 on root, write/edit to `/etc/` or `.ssh/`, download-then-execute chains (`curl -o file && chmod +x && bash`) |
| High     | 0.80      | review         | `sudo`, `kill -9`, destructive git, DROP TABLE, write/edit secrets, HTTP mutations, `ssh`/`scp`, credential file access, browser automation + data export, transitive installs (`npx skills add`, `pip install git+`), control plane mutations (`crontab`, `systemctl enable/start/stop`) |
| Medium   | 0.70      | review         | Content ingestion pipelines (`curl \| python3`), interpreter execution (`python3 script.py`, `node build.js`), cloud CLI mutations (`az/gcloud/aws/kubectl/terraform` with create/delete/destroy verbs), package installs, `write_file`, MCP tools, Docker operations |
| Low      | 0.85      | approve        | `read_file`, `list_directory`, `search`, `recall`, `man`, `use_prompt`, `tool_search`, `read_resource`, `web_search`, read-only bash (`ls`, `cat`, `head`, `grep`, `find`, etc.) |

When no rule matches, the heuristic returns a default verdict: medium risk,
0.50 confidence, "review" recommendation.

The bash "read-only" rule handles simple pipelines and command chains by
splitting on `|`, `&&`, `||`, and `;`, then checking each segment individually.

### Rules derived from audit data

Several rules were calibrated using analysis of 25K public agent skill
security audits across three independent auditors:

- **`download-exec`**: Two-step download-then-execute chains that bypass the
  existing `pipe-to-shell` rule. 8% of critical-tier skills use this pattern.
- **`transitive-install`**: Installing packages from URLs or git repos rather
  than vetted registries. Socket flags this as supply-chain critical in 36%
  of dangerous skills.
- **`browser-data-export`**: Browser automation combined with cookie/session/
  profile export. OpenClaw treats browser profile access as operator-level
  capability.
- **`control-plane-mutation`**: Persistent system changes (crontab, systemd)
  that outlive the session. OpenClaw denies control-plane tools by default.
- **`content-ingestion`**: Fetch-and-process pipelines where remote content
  feeds into an interpreter (Snyk W011 pattern — indirect prompt injection
  surface).
- **`interpreter-exec`**: Running a script file whose content hasn't been
  inspected. Opaque to command-level heuristics.
- **`cloud-infra-mutation`**: Distinguishes destructive cloud CLI verbs
  (`create`, `delete`, `destroy`) from read-only ones (`show`, `list`, `get`).

---

## LLM Judge

The LLM judge runs on a daemon thread and performs a multi-turn evaluation:

1. **Context preparation**: Recent conversation history is FIFO-truncated to
   fit within `max_context_ratio` of the judge's context window. The tool call
   details (name, approval label, full arguments) are appended as a user message.
2. **Multi-turn loop** (up to 5 turns): The judge can use `read_file` and
   `list_directory` to gather evidence before rendering its verdict. Each tool
   result is appended to the conversation and the judge is called again. On
   the final turn, tools are stripped and a forcing message instructs the
   judge to render its verdict immediately.
3. **Verdict parsing**: The judge's final text response is parsed as JSON using
   a four-stage strategy: direct parse, markdown code block extraction,
   brace-counting, and regex field extraction as a last resort.
4. **Arbitration**: If the LLM verdict has higher confidence than the heuristic,
   it replaces the heuristic via the `intent_verdict` SSE event.

### Read-only tools

When `read_only_tools` is enabled (default), the judge can use two tools:

- **`read_file`**: Read file contents (capped at 32 KB)
- **`list_directory`**: List directory entries (capped at 200 entries)

Security hardening blocks access to sensitive paths:

| Category | Blocked patterns |
|----------|-----------------|
| System directories | `/etc/`, `/root/`, `/proc/`, `/sys/`, `/dev/` |
| Credential directories | `.ssh`, `.gnupg`, `.aws`, `.config` |
| Key files | `*.pem`, `*.key`, `*.p12`, `*.pfx` |

### Timeout

The `timeout` setting (default 60 seconds) is a total budget across all judge
turns. Time is decremented after each LLM call. If the budget expires mid-turn,
the judge attempts to parse whatever partial response is available.

---

## Verdict Structure

Each verdict (heuristic or LLM) is an `IntentVerdict` with these fields:

| Field            | Type       | Description |
|------------------|------------|-------------|
| `verdict_id`     | string     | Unique identifier (UUID prefix) |
| `call_id`        | string     | Correlates with the tool call's `call_id` |
| `func_name`      | string     | Tool function name |
| `intent_summary` | string     | One-sentence description of what the tool call does |
| `risk_level`     | string     | `"low"`, `"medium"`, `"high"`, or `"critical"` |
| `confidence`     | float      | 0.0--1.0, how certain the assessment is |
| `recommendation` | string     | `"approve"`, `"review"`, or `"deny"` |
| `reasoning`      | string     | Explanation of the assessment |
| `evidence`       | list[str]  | Supporting evidence (rule name or file excerpts) |
| `tier`           | string     | `"heuristic"` or `"llm"` |
| `judge_model`    | string     | Model used (empty for heuristic tier) |
| `latency_ms`     | int        | Evaluation time in milliseconds |

---

## Session Integration

The judge is lazy-initialized on first use. When `ChatSession` prepares tool
calls for approval, it calls `_evaluate_intent()` which:

1. Instantiates `IntentJudge` if not already created
2. Extracts `func_name`, `func_args`, and `approval_label` from each pending item
3. Calls `judge.evaluate()` which returns heuristic verdicts immediately
4. Attaches each heuristic verdict to its item as `_heuristic_verdict`
5. The daemon thread runs the LLM judge and delivers results via `ui.on_intent_verdict()`

Sub-agents (plan agent, task agent) are exempt from intent validation -- they
always get full tool visibility without judge evaluation.

---

## Storage and Audit

All verdicts are persisted to the `intent_verdicts` table (migration 012):

- Heuristic verdicts are stored when the `approve_request` event is emitted
- LLM verdicts are stored when the `intent_verdict` event is delivered
- The `user_decision` column is updated when the user approves or denies

The console admin panel exposes verdict history via:

```
GET /v1/api/admin/verdicts?ws_id=&since=&until=&risk_level=&limit=100&offset=0
```

This endpoint requires the `admin.judge` permission.

---

## SSE Events

### `approve_request` (extended)

When the judge is active, `approve_request` items include a `verdict` field
with the heuristic verdict, and the event includes a `judge_pending` flag
indicating that an LLM verdict is in flight:

```json
{
  "type": "approve_request",
  "judge_pending": true,
  "items": [
    {
      "call_id": "call_abc123",
      "header": "bash: npm install express",
      "preview": "",
      "func_name": "bash",
      "approval_label": "bash",
      "needs_approval": true,
      "error": null,
      "verdict": {
        "verdict_id": "a1b2c3d4e5f6",
        "call_id": "call_abc123",
        "func_name": "bash",
        "intent_summary": "Package installation: npm install express",
        "risk_level": "medium",
        "confidence": 0.70,
        "recommendation": "review",
        "reasoning": "Command installs a software package which may modify the environment.",
        "evidence": ["Matched rule: package-install"],
        "tier": "heuristic",
        "judge_model": "",
        "latency_ms": 0
      }
    }
  ]
}
```

### `intent_verdict`

Delivered asynchronously when the LLM judge completes. The UI replaces the
heuristic verdict badge with the LLM verdict:

```json
{
  "type": "intent_verdict",
  "verdict_id": "f7e8d9c0b1a2",
  "call_id": "call_abc123",
  "func_name": "bash",
  "intent_summary": "Install Express.js web framework via npm",
  "risk_level": "medium",
  "confidence": 0.85,
  "recommendation": "review",
  "reasoning": "The command installs express from npm. This is a well-known package but will modify node_modules and package.json.",
  "evidence": ["Checked package.json — express is not currently a dependency"],
  "tier": "llm",
  "judge_model": "gpt-5",
  "latency_ms": 2340
}
```

---

## Skill Scanner

Skills are evaluated by a content scanner at creation and update time. The
scanner runs the same class of pattern analysis as the heuristic rules but
operates on SKILL.md content rather than individual tool calls. It evaluates
four independent risk axes:

1. **Content risk** — command execution scope, external downloads, credential
   handling, eval/exec, sudo, data exfiltration, browser automation
2. **Supply chain risk** — pipe-to-shell, transitive installs (`npx skills add`),
   obfuscation, download-execute chains, executable URLs from untrusted domains
3. **Vulnerability risk** — prompt injection patterns, insecure credential
   handling, third-party content exposure (indirect prompt injection surface)
4. **Declared capability risk** — parsed from the skill's `allowed_tools` field.
   `Bash(*)` (unrestricted shell) is high risk. `Bash(git:*)` is low.
   Read-only tools are safe.

Results are stored in `scan_status` (tier: safe/low/medium/high/critical) and
`scan_report` (JSON breakdown) on the `prompt_templates` table. These fields are
system-managed and not editable via the admin API.

The scanner is a pure function (~2ms) with no I/O. It runs synchronously in
the storage layer. Scanner failures are silently caught to never block skill
creation.

See [docs/governance.md](governance.md) for the skill governance model.

---

## v2 Calibration Path

Run v1 with all tools requiring manual approval to build a local verdict
dataset. The `intent_verdicts` table accumulates `(tool_call, verdict,
user_decision)` triples over time. In v2, calibration tooling will analyze
this dataset to:

- Identify tools that are always approved (candidates for auto-approve policies)
- Detect false positives in heuristic rules
- Measure LLM judge accuracy against human decisions
- Recommend policy changes to reduce approval fatigue

This data-driven approach means v1 is both useful on its own and a foundation
for automated policy tuning.
