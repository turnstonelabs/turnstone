"""cli.py — ``turnstone-rollout``: run one rollout and print what was recorded.

Usage:
    turnstone-rollout --base-url http://127.0.0.1:8000/v1 --model NAME
        --prompt "Create hello.txt containing hi, then list the directory."
        --top-k 20 --top-p 0.95
        [--file notes.txt=some text] [--image IMAGE]
        [--extra-body '{"chat_template_kwargs": {...}}'] [--out episode.json]

The reply limit, the context window, the tool time limit, the sub-agent turn
limit, the temperature and the reasoning effort are the framework's for the
model (its definition in ``config.toml``, the configured setting, the default)
unless a flag overrides them.

Exit status: 0 for a trainable rollout, however the model ended it; 1 when the
server, the harness or the runtime ended it; 2 when it ran to its own end but
its token data cannot be trained on.
"""

from __future__ import annotations

import argparse
import json
import sys

from turnstone.core.config import set_config_path
from turnstone.rollout.container import ContainerSpec
from turnstone.rollout.episode import DEFAULT_SYSTEM_PROMPT, RolloutConfig, Task, run_episode
from turnstone.rollout.settings import SetupError
from turnstone.rollout.tokens import generated_positions

_NL = chr(10)


def _shown(text: object) -> str:
    """*text* for the terminal: what the model, a tool or the server wrote may
    hold control sequences, which are shown, not sent."""
    return "".join(
        ch if ch.isprintable() else ch.encode("unicode_escape").decode("ascii") for ch in str(text)
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="turnstone-rollout", description=__doc__.split(_NL)[0])
    parser.add_argument("--base-url", required=True, help="chat-completions API root, ending /v1")
    parser.add_argument("--model", required=True)
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--system-prompt", default=DEFAULT_SYSTEM_PROMPT)
    parser.add_argument(
        "--file", action="append", default=[], metavar="PATH=CONTENT", help="setup file, repeatable"
    )
    parser.add_argument("--image", default=ContainerSpec.image)
    parser.add_argument("--workdir", default=ContainerSpec.workdir)
    parser.add_argument(
        "--user", default=ContainerSpec.user, help="the user commands run as inside (0:0 is root)"
    )
    parser.add_argument(
        "--writable-root", action="store_true", help="make the image's whole file system writable"
    )
    parser.add_argument(
        "--cap", action="append", default=[], metavar="NAME", help="a capability, repeatable"
    )
    parser.add_argument(
        "--env", action="append", default=[], metavar="NAME=VALUE", help="repeatable"
    )
    parser.add_argument("--memory", default=ContainerSpec.memory)
    parser.add_argument("--cpus", type=float, default=ContainerSpec.cpus)
    parser.add_argument("--max-turns", type=int, default=Task.max_turns)
    parser.add_argument(
        "--timeout",
        type=int,
        default=None,
        help="time limit for the session's run, seconds (default: 300; with a token budget "
        "it is a backstop for the machinery and, left out, worked out from the budgets)",
    )
    parser.add_argument(
        "--max-generated-tokens",
        type=int,
        default=None,
        help="the model's budget of generated tokens, its sub-agents' included",
    )
    parser.add_argument(
        "--max-command-seconds",
        type=float,
        default=None,
        help="the model's budget of time its commands may run",
    )
    parser.add_argument(
        "--min-tokens-per-second",
        type=float,
        default=None,
        help="tokens a second the server must at least generate (needed with a token budget)",
    )
    parser.add_argument(
        "--min-prompt-tokens-per-second",
        type=float,
        default=None,
        help="tokens a second the server must at least process of a prompt",
    )
    parser.add_argument(
        "--tool-timeout",
        type=int,
        default=None,
        help="seconds one tool call may run (default: the framework's tools.timeout)",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=None,
        help="sampling temperature (default: the framework's for this model, else 1)",
    )
    parser.add_argument("--top-k", type=int, default=None)
    parser.add_argument("--top-p", type=float, default=None)
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=None,
        help="reply limit per model call (default: the framework's for this model)",
    )
    parser.add_argument(
        "--context-window",
        type=int,
        default=None,
        help="the model's context window (default: its definition, else what its endpoint reports)",
    )
    parser.add_argument(
        "--subagent-max-turns",
        type=int,
        default=None,
        help="turns one sub-agent may take (default: the framework's tools.agent_max_turns)",
    )
    parser.add_argument("--max-subagents", type=int, default=RolloutConfig.max_subagents)
    parser.add_argument(
        "--config",
        default=None,
        metavar="PATH",
        help="path to config.toml; a file that is not there or cannot be read or parsed is "
        "refused, never taken as an empty configuration",
    )
    parser.add_argument(
        "--rules",
        default="shipped",
        metavar="PATH",
        help="the pattern rules enforced on tool calls: a rule file, 'shipped' (the default) "
        "or 'none'",
    )
    parser.add_argument(
        "--effort",
        default=None,
        help="reasoning effort (default: the framework's for this model, if it names one)",
    )
    parser.add_argument(
        "--extra-body",
        default=None,
        metavar="JSON",
        help="JSON object merged into every request's body (sampler and template arguments)",
    )
    parser.add_argument(
        "--keep-text", action="store_true", help="keep each call's generated text in the record"
    )
    parser.add_argument(
        "--sampler-logprobs",
        choices=("probe", "yes", "no"),
        default="probe",
        help="whether the server reports log-probabilities of the sampling distribution; "
        "'probe' (the default) asks it before the run",
    )
    parser.add_argument("--out", help="write the full episode record here as JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.config:
        set_config_path(args.config)
    files = {}
    for item in args.file:
        path, _, content = item.partition("=")
        files[path] = content
    extra_body: dict[str, object] = json.loads(args.extra_body) if args.extra_body else {}
    if not isinstance(extra_body, dict):
        raise SystemExit("--extra-body must be a JSON object")
    if args.top_k is not None:
        extra_body["top_k"] = args.top_k
    if args.top_p is not None:
        extra_body["top_p"] = args.top_p
    task = Task(
        prompt=args.prompt,
        key="cli",
        system_prompt=args.system_prompt,
        setup_files=files,
        max_turns=args.max_turns,
        # Without a token budget the time limit is the model's and has a default.
        timeout=args.timeout
        if args.timeout is not None or args.max_generated_tokens is not None
        else 300,
        max_generated_tokens=args.max_generated_tokens,
        max_command_seconds=args.max_command_seconds,
        container=ContainerSpec(
            image=args.image,
            workdir=args.workdir,
            user=args.user,
            read_only_root=not args.writable_root,
            capabilities=tuple(args.cap),
            environment=dict(item.partition("=")[::2] for item in args.env),
            memory=args.memory,
            cpus=args.cpus,
        ),
    )
    config = RolloutConfig(
        base_url=args.base_url,
        model=args.model,
        temperature=args.temperature,
        max_tokens=args.max_tokens,
        context_window=args.context_window,
        tool_timeout=args.tool_timeout,
        subagent_max_turns=args.subagent_max_turns,
        max_subagents=args.max_subagents,
        reasoning_effort=args.effort,
        keep_text=args.keep_text,
        extra_body=extra_body,
        sampler_logprobs={"probe": None, "yes": True, "no": False}[args.sampler_logprobs],
        rules=None if args.rules == "none" else args.rules,
        min_tokens_per_second=args.min_tokens_per_second,
        min_prompt_tokens_per_second=args.min_prompt_tokens_per_second,
    )
    try:
        record = run_episode(task, config)
    except SetupError as error:
        # Nothing was run: the configuration cannot work.
        raise SystemExit(f"turnstone-rollout: {error}") from None
    if args.out:
        # ASCII output: text the model produced may hold what no encoding can write.
        with open(args.out, "w", encoding="ascii") as handle:
            handle.write(json.dumps(record) + _NL)

    tokens = record["tokens"]
    print(
        f"finish_state: {record['finish_state']} (cause: {record['finish_cause']})",
        "| error:",
        _shown(record["error"]),
    )
    print(
        "turns:", record["turns"], "| seconds:", record["seconds"], "|", json.dumps(record["time"])
    )
    for name, limit in record["limits"].items():
        print(f"  limit {name}: {limit['value']} ({limit['source']})")
    print("budgets:", json.dumps(record["budgets"]))
    if record["timeout"]:
        print("at the time limit:", json.dumps(record["timeout"]))
    print("sampling:", json.dumps(record["sampling"]["requested"]))
    for name, source in record["sampling"]["sources"].items():
        print(f"  setting {name}: {json.dumps(record['sampling'][name])} ({source})")
    print("log-probabilities:", json.dumps(record["logprobs"]))
    for call in tokens["calls"]:
        logprobs = "none" if call["logprobs"] is None else len(call["logprobs"])
        print(
            f"  call {call['call']}: prompt {call['prompt_len']} tokens ({call['shared']} shared "
            f"with the sequence so far), generated {len(call['token_ids'])}, log-probabilities "
            f"{logprobs}, finish {call['finish_reason']}, in final sequence: {call['in_final']}"
        )
    print("final sequence:", tokens["final_len"], "tokens | generated:", record["generated_tokens"])
    if record["one_sequence"]:
        print("generated tokens with a position:", len(generated_positions(tokens)))
    print("prefix check:", "held" if record["prefix_ok"] else "BROKEN")
    for line in record["prefix_breaks"]:
        print("  " + _shown(line))
    print("prefix events:", record["prefix_events"] or "none")
    print("trainable:", "yes" if record["trainable"] else "NO")
    print("one sequence:", "yes" if record["one_sequence"] else "NO")
    for line in record["problems"]:
        print("  problem: " + _shown(line))
    for line in record["warnings"]:
        print("  warning: " + _shown(line))
    print("discarded calls:", len(record["discarded_calls"]))
    sub = record["subagents"]
    print(
        f"sub-agents: {sub['started']} started, {sub['refused_at_limit']} refused at a limit, "
        f"{sub['model_calls']} model calls, {sub['generated_tokens']} generated tokens "
        f"(not for training), failures: {_shown(sub['failures'] or 'none')}"
    )
    rules = record["rules"]
    if rules is not None:
        print(f"rules: {rules['name']} ({rules['rules']} rules, {rules['sha256'][:12]})")
    for entry in record["tool_log"]:
        arguments = json.dumps(entry["args"])[:100]
        rule = f" rule={entry['rule']} ({entry['rule_says']})" if "rule" in entry else ""
        print(f"  tool turn {entry['turn']} {entry['tool']} {arguments} ok={entry['ok']}{rule}")
    print("final content:", _shown((record["final_content"] or "")[:300]))
    print("sandbox:", json.dumps(record["sandbox"]))
    if record["finish_cause"] == "machinery":
        return 1
    return 0 if record["trainable"] else 2


if __name__ == "__main__":
    sys.exit(main())
