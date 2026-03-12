"""Terminal CLI frontend for turnstone.

Provides TerminalUI (implementing the SessionUI protocol), readline setup,
model auto-detection, workstream management, and the main() REPL entry point.
"""

from __future__ import annotations

import argparse
import os
import readline
import sys
import textwrap
import threading
from typing import TYPE_CHECKING, Any

from turnstone.core.session import ChatSession, SessionUI
from turnstone.core.workstream import Workstream, WorkstreamManager, WorkstreamState
from turnstone.ui.colors import (
    BOLD,
    DIM,
    RED,
    RESET,
    YELLOW,
    bold,
    cyan,
    dim,
    green,
    red,
    yellow,
)
from turnstone.ui.markdown import MarkdownRenderer
from turnstone.ui.spinner import Spinner

if TYPE_CHECKING:
    from collections.abc import Callable

# ─── Readline ─────────────────────────────────────────────────────────────

SLASH_COMMANDS = [
    "/instructions",
    "/clear",
    "/new",
    "/workstreams",
    "/resume",
    "/name",
    "/delete",
    "/history",
    "/model",
    "/raw",
    "/reason",
    "/compact",
    "/creative",
    "/debug",
    "/mcp",
    "/help",
    "/exit",
    "/quit",
    "/q",
    "/ws",
    "/cluster",
]


def _completer(text: str, state: int) -> str | None:
    """Tab-complete slash commands."""
    matches = [c for c in SLASH_COMMANDS if c.startswith(text)] if text.startswith("/") else []
    if state < len(matches):
        return matches[state] + " "
    return None


def setup_readline() -> None:
    """Set up readline with tab completion."""
    readline.set_history_length(1000)
    readline.set_completer(_completer)
    readline.set_completer_delims("")  # treat entire line as completion input
    readline.parse_and_bind("tab: complete")


# ─── TerminalUI ───────────────────────────────────────────────────────────


class TerminalUI(SessionUI):
    """Terminal-based UI using ANSI colors, MarkdownRenderer, and Spinner."""

    def __init__(self) -> None:
        self.md = MarkdownRenderer()
        self.spinner: Spinner | None = None
        self._print_lock = threading.Lock()
        self.auto_approve = False

    def on_thinking_start(self) -> None:
        self.spinner = Spinner("Thinking")
        self.spinner.start()

    def on_thinking_stop(self) -> None:
        if self.spinner:
            self.spinner.stop()
            self.spinner = None

    def on_reasoning_token(self, text: str) -> None:
        sys.stdout.write(f"{DIM}{text}{RESET}")
        sys.stdout.flush()

    def on_content_token(self, text: str) -> None:
        rendered = self.md.feed(text)
        if rendered:
            sys.stdout.write(rendered)
            sys.stdout.flush()

    def on_stream_end(self) -> None:
        remainder = self.md.flush()
        if remainder:
            sys.stdout.write(remainder)
        self.md.in_code_block = False
        sys.stdout.write("\n")
        sys.stdout.flush()

    def approve_tools(self, items: list[dict[str, Any]]) -> tuple[bool, str | None]:
        """Display tool previews and prompt for batch approval.

        Returns (approved: bool, feedback: str | None).
        """
        pending = [it for it in items if it.get("needs_approval") and not it.get("error")]

        with self._print_lock:
            # Print all headers and previews
            for item in items:
                if item.get("error"):
                    sys.stdout.write(f"  {red(item['header'])}\n")
                else:
                    sys.stdout.write(f"  {yellow(item['header'])}\n")
                if item.get("preview"):
                    sys.stdout.write(item["preview"] + "\n")
            sys.stdout.flush()

            if not pending or self.auto_approve:
                return True, None

            # Prompt
            try:
                if len(pending) == 1:
                    label = pending[0].get("approval_label", pending[0]["func_name"])
                    prompt_text = (
                        f"    \001{BOLD}\002Allow {label}?\001{RESET}\002 "
                        f"\001{DIM}\002[y/n/a(lways), optional message]\001{RESET}\002 "
                    )
                else:
                    labels = ", ".join(it.get("approval_label", it["func_name"]) for it in pending)
                    prompt_text = (
                        f"    \001{BOLD}\002Allow {len(pending)} tools ({labels})?\001{RESET}\002 "
                        f"\001{DIM}\002[y/n/a(lways), optional message]\001{RESET}\002 "
                    )
                resp = input(prompt_text).strip()
            except (EOFError, KeyboardInterrupt):
                sys.stdout.write("\n")
                resp = "n"

            # Parse decision and optional feedback: "y, use absolute path"
            decision = resp.lower()
            feedback = None
            for sep in (",", " "):
                if sep in resp:
                    decision = resp[: resp.index(sep)].strip().lower()
                    feedback = resp[resp.index(sep) + 1 :].strip() or None
                    break

            if decision in ("a", "always"):
                self.auto_approve = True
                return True, feedback
            elif decision in ("y", "yes"):
                return True, feedback
            else:
                denial_msg = "Denied by user"
                if feedback:
                    denial_msg += f": {feedback}"
                for item in pending:
                    item["denied"] = True
                    item["denial_msg"] = denial_msg
                return False, None

    def on_tool_result(self, call_id: str, name: str, output: str) -> None:
        pass  # Optional: display summary

    def on_tool_output_chunk(self, call_id: str, chunk: str) -> None:
        pass  # Terminal shows spinner during tool execution

    def on_status(self, usage: dict[str, Any], context_window: int, effort: str) -> None:
        total_tok = usage["prompt_tokens"] + usage["completion_tokens"]
        pct = total_tok / context_window * 100 if context_window > 0 else 0
        parts = [f"{total_tok:,} / {context_window:,} tokens ({pct:.0f}%)"]
        if effort != "medium":
            parts.append(f"reasoning: {effort}")
        sys.stdout.write(f"\n  {DIM}[{' · '.join(parts)}]{RESET}\n")
        sys.stdout.flush()

    def on_plan_review(self, content: str) -> str:
        sys.stdout.write(f"\n{DIM}{'─' * 60}{RESET}\n")
        for line in content.splitlines():
            sys.stdout.write(f"  {line}\n")
        sys.stdout.write(f"{DIM}{'─' * 60}{RESET}\n")
        sys.stdout.flush()
        try:
            prompt_text = (
                f"    \001{BOLD}\002Plan ready.\001{RESET}\002 "
                f"\001{DIM}\002[enter to approve, feedback to amend, "
                f"ctrl-c to reject]\001{RESET}\002 "
            )
            resp = input(prompt_text).strip()
        except EOFError:
            resp = ""
        except KeyboardInterrupt:
            resp = "reject"
        return resp

    def on_info(self, message: str) -> None:
        print(message)

    def on_error(self, message: str) -> None:
        sys.stdout.write(f"{RED}{message}{RESET}\n")
        sys.stdout.flush()

    def on_state_change(self, state: str) -> None:
        pass  # base TerminalUI ignores state changes

    def on_rename(self, name: str) -> None:
        pass  # base TerminalUI ignores renames


# ─── WorkstreamTerminalUI ─────────────────────────────────────────────────


# State display config: (symbol, color_fn, label)
_STATE_DISPLAY: dict[WorkstreamState, tuple[str, Callable[[str], str], str]] = {
    WorkstreamState.IDLE: ("·", dim, "idle"),
    WorkstreamState.THINKING: ("◌", cyan, "thinking"),
    WorkstreamState.RUNNING: ("▸", green, "running"),
    WorkstreamState.ATTENTION: ("◆", yellow, "attention"),
    WorkstreamState.ERROR: ("✖", red, "error"),
}


class WorkstreamTerminalUI(TerminalUI):
    """TerminalUI with workstream awareness: buffers output when in background,
    blocks on approval until foregrounded."""

    def __init__(self, ws_id: str, manager: WorkstreamManager) -> None:
        super().__init__()
        self.ws_id = ws_id
        self.manager = manager
        self._output_buffer: list[tuple[str, str]] = []  # (event_type, text)
        self._fg_event = threading.Event()
        self._fg_event.set()  # starts as foreground

    @property
    def is_foreground(self) -> bool:
        return self.manager.active_id == self.ws_id

    def set_foreground(self, fg: bool) -> None:
        if fg:
            self._fg_event.set()
        else:
            self._fg_event.clear()

    def on_state_change(self, state: str) -> None:
        try:
            ws_state = WorkstreamState(state)
        except ValueError:
            return
        self.manager.set_state(self.ws_id, ws_state)

    # -- output buffering when in background --------------------------------

    def on_thinking_start(self) -> None:
        if self.is_foreground:
            super().on_thinking_start()

    def on_thinking_stop(self) -> None:
        if self.is_foreground:
            super().on_thinking_stop()
        elif self.spinner:
            self.spinner.stop()
            self.spinner = None

    def _buffer(self, event_type: str, text: str) -> None:
        with self._print_lock:
            self._output_buffer.append((event_type, text))

    def on_reasoning_token(self, text: str) -> None:
        if self.is_foreground:
            super().on_reasoning_token(text)
        else:
            self._buffer("reasoning", text)

    def on_content_token(self, text: str) -> None:
        if self.is_foreground:
            super().on_content_token(text)
        else:
            self._buffer("content", text)

    def on_stream_end(self) -> None:
        if self.is_foreground:
            super().on_stream_end()
        else:
            self._buffer("stream_end", "")

    def on_status(self, usage: dict[str, Any], context_window: int, effort: str) -> None:
        if self.is_foreground:
            super().on_status(usage, context_window, effort)
        # silently drop status for background streams

    def on_info(self, message: str) -> None:
        if self.is_foreground:
            super().on_info(message)
        else:
            self._buffer("info", message)

    def on_error(self, message: str) -> None:
        if self.is_foreground:
            super().on_error(message)
        else:
            self._buffer("error", message)

    def on_tool_result(self, call_id: str, name: str, output: str) -> None:
        if self.is_foreground:
            super().on_tool_result(call_id, name, output)

    def on_tool_output_chunk(self, call_id: str, chunk: str) -> None:
        if self.is_foreground:
            super().on_tool_output_chunk(call_id, chunk)

    def on_plan_review(self, content: str) -> str:
        # Must wait until foregrounded to show plan review
        if not self.is_foreground:
            self._buffer(
                "info",
                f"{YELLOW}[Plan ready — switch to this workstream to review]{RESET}",
            )
            self._fg_event.wait()
        return super().on_plan_review(content)

    def approve_tools(self, items: list[dict[str, Any]]) -> tuple[bool, str | None]:
        """Block until foregrounded if in background, then show approval prompt."""
        if not self.is_foreground:
            tool_names = ", ".join(
                it.get("approval_label", it.get("func_name", "?"))
                for it in items
                if it.get("needs_approval") and not it.get("error")
            )
            if tool_names:
                self._buffer("info", f"{YELLOW}Waiting for approval: {tool_names}{RESET}")
            self._fg_event.wait()
        return super().approve_tools(items)

    def flush_buffer(self) -> None:
        """Replay buffered output when switching to foreground."""
        with self._print_lock:
            if not self._output_buffer:
                return
            buf = list(self._output_buffer)
            self._output_buffer.clear()
        sys.stdout.write(f"\n  {DIM}--- buffered output ({len(buf)} events) ---{RESET}\n")
        replay_md = MarkdownRenderer()
        for event_type, text in buf:
            if event_type == "reasoning":
                sys.stdout.write(f"{DIM}{text}{RESET}")
            elif event_type == "content":
                rendered = replay_md.feed(text)
                if rendered:
                    sys.stdout.write(rendered)
            elif event_type == "stream_end":
                remainder = replay_md.flush()
                if remainder:
                    sys.stdout.write(remainder)
                replay_md.in_code_block = False
                sys.stdout.write("\n")
            elif event_type == "info":
                sys.stdout.write(f"{text}\n")
            elif event_type == "error":
                sys.stdout.write(f"{RED}{text}{RESET}\n")
        sys.stdout.write(f"  {DIM}--- end buffered output ---{RESET}\n\n")
        sys.stdout.flush()


# ─── Workstream commands ──────────────────────────────────────────────────


def _print_ws_status_line(manager: WorkstreamManager) -> None:
    """Print a one-line status of background workstreams that are active."""
    active_id = manager.active_id
    parts = []
    for ws in manager.list_all():
        if ws.id == active_id:
            continue
        if ws.state in (WorkstreamState.IDLE,):
            continue
        sym, color_fn, label = _STATE_DISPLAY[ws.state]
        idx = manager.index_of(ws.id)
        parts.append(color_fn(f"{sym} {idx}:{ws.name} ({label})"))
    if parts:
        sys.stderr.write(f"  {'  '.join(parts)}\n")
        sys.stderr.flush()


def _handle_ws_command(
    manager: WorkstreamManager,
    cmd_line: str,
    skip_permissions: bool,
) -> bool:
    """Handle /ws subcommands.  Returns (switched: bool)."""
    parts = cmd_line.strip().split()
    sub = parts[1] if len(parts) > 1 else "list"

    if sub == "list":
        active_id = manager.active_id
        all_ws = manager.list_all()
        max_name = max((len(ws.name) for ws in all_ws), default=0)
        for ws in all_ws:
            idx = manager.index_of(ws.id)
            sym, color_fn, label = _STATE_DISPLAY[ws.state]
            marker = " *" if ws.id == active_id else "  "
            padded = ws.name.ljust(max_name)
            print(f"  {marker}{idx}. {color_fn(f'{sym} {padded}')}  {dim(label)}")
        return False

    elif sub == "new":
        name = parts[2] if len(parts) > 2 else ""
        try:
            ws = manager.create(
                name=name,
                ui_factory=lambda wid: WorkstreamTerminalUI(wid, manager),
            )
        except RuntimeError as e:
            print(red(str(e)))
            return False
        if skip_permissions and isinstance(ws.ui, TerminalUI):
            ws.ui.auto_approve = True
        # Mark old active as background
        old = manager.get_active()
        if old and isinstance(old.ui, WorkstreamTerminalUI):
            old.ui.set_foreground(False)
        manager.switch(ws.id)
        if isinstance(ws.ui, WorkstreamTerminalUI):
            ws.ui.set_foreground(True)
        print(f"Created workstream {cyan(ws.name)} (#{manager.index_of(ws.id)})")
        return True

    elif sub.isdigit():
        idx = int(sub)
        old = manager.get_active()
        ws: Workstream | None = manager.switch_by_index(idx)  # type: ignore[no-redef]
        if ws:
            if old and isinstance(old.ui, WorkstreamTerminalUI):
                old.ui.set_foreground(False)
            if isinstance(ws.ui, WorkstreamTerminalUI):
                ws.ui.set_foreground(True)
                ws.ui.flush_buffer()
            print(f"Switched to {cyan(ws.name)}")
            return True
        else:
            print(red(f"No workstream #{idx}"))
            return False

    elif sub == "close":
        target_idx = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None
        ws_id: str | None = None
        if target_idx is not None:
            all_ws = manager.list_all()
            if 1 <= target_idx <= len(all_ws):
                ws_id = all_ws[target_idx - 1].id
            else:
                print(red(f"No workstream #{target_idx}"))
                return False
        else:
            ws_id = manager.active_id
            if ws_id is None:
                return False

        assert ws_id is not None
        ws_obj = manager.get(ws_id)
        ws_name = ws_obj.name if ws_obj else "?"
        if manager.close(ws_id):
            print(f"Closed workstream {ws_name}")
            # Ensure new active is foregrounded
            new_active = manager.get_active()
            if new_active and isinstance(new_active.ui, WorkstreamTerminalUI):
                new_active.ui.set_foreground(True)
            return True
        else:
            print(red("Cannot close the last workstream"))
            return False

    elif sub == "rename":
        new_name = " ".join(parts[2:]) if len(parts) > 2 else ""
        if not new_name:
            print(red("Usage: /ws rename <name>"))
            return False
        ws_active: Workstream | None = manager.get_active()
        if ws_active:
            old_name = ws_active.name
            ws_active.name = new_name
            print(f"Renamed {old_name} -> {cyan(new_name)}")
        return False

    else:
        print(f"Unknown /ws subcommand: {sub}")
        print("Usage: /ws [list|new [name]|<N>|close [N]|rename <name>]")
        return False


# ─── Cluster commands ─────────────────────────────────────────────────────


def _handle_cluster_command(cmd_line: str, console_url: str | None, auth_token: str = "") -> None:
    """Handle /cluster subcommands querying the turnstone-console API."""
    import httpx

    if not console_url:
        print(red("No console URL configured. Use --console-url or set [console] url in config."))
        return

    headers: dict[str, str] = {}
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"

    parts = cmd_line.strip().split()
    sub = parts[1] if len(parts) > 1 else "status"

    try:
        if sub == "status":
            resp = httpx.get(f"{console_url}/v1/api/cluster/overview", timeout=5, headers=headers)
            data = resp.json()
            states = data.get("states", {})
            agg = data.get("aggregate", {})
            print(f"\n  {bold('Cluster Overview')}")
            print(
                f"  Nodes: {cyan(str(data.get('nodes', 0)))}  "
                f"Workstreams: {cyan(str(data.get('workstreams', 0)))}"
            )
            print()
            for state_name in ["running", "thinking", "attention", "idle", "error"]:
                count = states.get(state_name, 0)
                sym, color_fn, label = _STATE_DISPLAY.get(
                    {
                        "running": WorkstreamState.RUNNING,
                        "thinking": WorkstreamState.THINKING,
                        "attention": WorkstreamState.ATTENTION,
                        "idle": WorkstreamState.IDLE,
                        "error": WorkstreamState.ERROR,
                    }[state_name],
                    ("?", dim, state_name),
                )
                print(f"    {color_fn(f'{sym} {label}')}: {count}")
            print()
            tokens = agg.get("total_tokens", 0)
            tools = agg.get("total_tool_calls", 0)
            tok_str = f"{tokens / 1000:.1f}k" if tokens >= 1000 else str(tokens)
            print(f"  {dim(f'{tok_str} tokens · {tools} tool calls')}")
            print()

        elif sub == "nodes":
            limit = 20
            resp = httpx.get(
                f"{console_url}/v1/api/cluster/nodes?sort=activity&limit={limit}",
                timeout=5,
                headers=headers,
            )
            data = resp.json()
            nodes = data.get("nodes", [])
            total = data.get("total", len(nodes))
            if not nodes:
                print(dim("  No nodes discovered."))
                return
            # Column widths
            max_name = max(len(n["node_id"]) for n in nodes)
            print(
                f"\n  {'NODE'.ljust(max_name)}  {'WS':>4}  {'RUN':>4}  {'ATTN':>4}  {'TOKENS':>8}"
            )
            print(f"  {'-' * max_name}  {'----':>4}  {'----':>4}  {'----':>4}  {'--------':>8}")
            for n in nodes:
                name = n["node_id"].ljust(max_name)
                ws = str(n.get("ws_total", 0))
                run = n.get("ws_running", 0)
                attn = n.get("ws_attention", 0)
                tok = n.get("total_tokens", 0)
                tok_str = f"{tok / 1000:.1f}k" if tok >= 1000 else str(tok)
                run_str = green(str(run)) if run else dim("0")
                attn_str = yellow(str(attn)) if attn else dim("0")
                print(f"  {cyan(name)}  {ws:>4}  {run_str:>4}  {attn_str:>4}  {dim(tok_str):>8}")
            if total > len(nodes):
                print(dim(f"\n  Showing {len(nodes)} of {total} nodes"))
            print()

        elif sub == "workstreams" or sub == "ws":
            params = "sort=state&per_page=20"
            # Parse optional filters: /cluster ws running, /cluster ws node=X
            for arg in parts[2:]:
                if "=" in arg:
                    key, val = arg.split("=", 1)
                    if key in ("state", "node", "search"):
                        params += f"&{key}={val}"
                else:
                    params += f"&state={arg}"
            resp = httpx.get(
                f"{console_url}/v1/api/cluster/workstreams?{params}",
                timeout=5,
                headers=headers,
            )
            data = resp.json()
            ws_list = data.get("workstreams", [])
            total = data.get("total", len(ws_list))
            if not ws_list:
                print(dim("  No matching workstreams."))
                return
            max_name = max(len(w.get("name", "")[:20]) for w in ws_list)
            max_node = max(len(w.get("node", "")[:16]) for w in ws_list)
            print(
                f"\n  {'STATE':<8}  {'NAME'.ljust(max_name)}  {'NODE'.ljust(max_node)}  {'TOKENS':>8}  {'CTX':>4}"
            )
            for w in ws_list:
                state = w.get("state", "idle")
                ws_state = {
                    "running": WorkstreamState.RUNNING,
                    "thinking": WorkstreamState.THINKING,
                    "attention": WorkstreamState.ATTENTION,
                    "idle": WorkstreamState.IDLE,
                    "error": WorkstreamState.ERROR,
                }.get(state, WorkstreamState.IDLE)
                sym, color_fn, label = _STATE_DISPLAY[ws_state]
                name = w.get("name", "")[:20].ljust(max_name)
                node = w.get("node", "")[:16].ljust(max_node)
                tok = w.get("tokens", 0)
                tok_str = f"{tok / 1000:.1f}k" if tok >= 1000 else str(tok)
                ctx = w.get("context_ratio", 0)
                ctx_str = f"{int(ctx * 100)}%" if ctx > 0 else ""
                print(
                    f"  {color_fn(f'{sym} {label:<5}')}  {bold(name)}  {dim(node)}  {dim(tok_str):>8}  {ctx_str:>4}"
                )
            if total > len(ws_list):
                print(dim(f"\n  Showing {len(ws_list)} of {total} workstreams"))
            print()

        elif sub == "node":
            if len(parts) < 3:
                print(red("Usage: /cluster node <node_id>"))
                return
            node_id = parts[2]
            resp = httpx.get(
                f"{console_url}/v1/api/cluster/node/{node_id}", timeout=5, headers=headers
            )
            data = resp.json()
            if "error" in data:
                print(red(data["error"]))
                return
            ws_list = data.get("workstreams", [])
            print(f"\n  {bold(node_id)}  ({data.get('server_url', '')})")
            if not ws_list:
                print(dim("  No workstreams."))
                return
            for w in ws_list:
                state = w.get("state", "idle")
                ws_state = {
                    "running": WorkstreamState.RUNNING,
                    "thinking": WorkstreamState.THINKING,
                    "attention": WorkstreamState.ATTENTION,
                    "idle": WorkstreamState.IDLE,
                    "error": WorkstreamState.ERROR,
                }.get(state, WorkstreamState.IDLE)
                sym, color_fn, _ = _STATE_DISPLAY[ws_state]
                name = w.get("name", "")
                title = w.get("title", "")
                activity = w.get("activity", "")
                print(f"    {color_fn(sym)} {bold(name)}  {dim(title)}")
                if activity:
                    print(f"      {dim(activity)}")
            print()

        else:
            print(f"Unknown /cluster subcommand: {sub}")
            print("Usage: /cluster [status|nodes|workstreams [state|node=X]|node <id>]")

    except httpx.ConnectError:
        print(red(f"Cannot connect to console at {console_url}"))
    except Exception as e:
        print(red(f"Cluster command failed: {e}"))


# ─── Model auto-detection ─────────────────────────────────────────────────


def detect_model(client: Any, provider: str = "openai") -> tuple[str, int | None]:
    """Auto-detect model — delegates to :func:`turnstone.core.model_registry.detect_model`."""
    from turnstone.core.model_registry import detect_model as _detect

    return _detect(client, provider=provider)


# ─── Main ──────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Interactive CLI for OpenAI-compatible models with tool calling.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Examples:
              python3 chat.py                          # auto-detect model
              python3 chat.py --model kappa_20b_131k    # explicit model
              python3 chat.py --temperature 0.7         # lower temperature
        """),
    )
    parser.add_argument(
        "--base-url",
        default="http://localhost:8000/v1",
        help="OpenAI-compatible API base URL (default: http://localhost:8000/v1)",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Model name (default: auto-detect from server)",
    )
    parser.add_argument(
        "--instructions",
        default=None,
        help="Developer instructions injected as developer message",
    )
    parser.add_argument(
        "--template",
        default=None,
        help="Prompt template name (replaces default templates)",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=0.5,
        help="Sampling temperature (default: 0.5)",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=32768,
        help="Max completion tokens (default: 32768)",
    )
    parser.add_argument(
        "--tool-timeout",
        type=int,
        default=30,
        help="Bash command timeout in seconds (default: 30)",
    )
    parser.add_argument(
        "--reasoning-effort",
        default="medium",
        choices=["none", "minimal", "low", "medium", "high", "xhigh", "max"],
        help="Reasoning effort level (default: medium)",
    )
    parser.add_argument(
        "--provider",
        default="openai",
        choices=["openai", "anthropic"],
        help="LLM provider for the default model (default: openai)",
    )
    parser.add_argument(
        "--context-window",
        type=int,
        default=131072,
        help="Context window size in tokens (default: 131072)",
    )
    parser.add_argument(
        "--compact-max-tokens",
        type=int,
        default=32768,
        help="Max tokens for compaction summary (default: 32768)",
    )
    parser.add_argument(
        "--auto-compact-pct",
        type=float,
        default=0.8,
        help="Auto-compact when prompt exceeds this fraction of context window (default: 0.8)",
    )
    parser.add_argument(
        "--agent-max-turns",
        type=int,
        default=-1,
        help="Max tool turns for agent sub-sessions, -1 for unlimited (default: -1)",
    )
    parser.add_argument(
        "--tool-truncation",
        type=int,
        default=0,
        help="Tool output truncation limit in chars, 0 for auto (50%% of context window) (default: 0)",
    )
    parser.add_argument(
        "--tool-search",
        choices=["auto", "on", "off"],
        default="auto",
        help="Dynamic tool search: auto (enable when tool count exceeds threshold), on, off (default: auto)",
    )
    parser.add_argument(
        "--tool-search-threshold",
        type=int,
        default=20,
        help="Min tools before tool search activates (default: 20)",
    )
    parser.add_argument(
        "--tool-search-max-results",
        type=int,
        default=5,
        help="Max tools returned per tool search query (default: 5)",
    )
    parser.add_argument(
        "--resume",
        default=None,
        metavar="WS",
        help="Resume a previous workstream by alias or ws_id",
    )
    parser.add_argument(
        "--skip-permissions",
        action="store_true",
        help="Auto-approve all tool calls (no confirmation prompts)",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="API key (default: $OPENAI_API_KEY, or 'dummy' for local servers)",
    )
    parser.add_argument(
        "--retention-days",
        type=int,
        default=90,
        metavar="DAYS",
        help="Delete unnamed workstreams older than DAYS days on startup, 0 to disable (default: 90)",
    )
    parser.add_argument(
        "--console-url",
        default=None,
        help="Turnstone console URL for /cluster commands (e.g., http://localhost:8090)",
    )
    parser.add_argument(
        "--auth-token",
        default=os.environ.get("TURNSTONE_AUTH_TOKEN", ""),
        help="Bearer token for authenticating to turnstone services (default: $TURNSTONE_AUTH_TOKEN)",
    )
    parser.add_argument(
        "--mcp-config",
        default=None,
        metavar="PATH",
        help="Path to MCP server config file (standard mcpServers JSON format)",
    )

    from turnstone.core.config import nonneg_float

    parser.add_argument(
        "--mcp-refresh-interval",
        type=nonneg_float,
        default=14400,
        metavar="SECONDS",
        help="Periodic MCP tool refresh interval for servers without push notifications (default: 14400 = 4h, 0 to disable)",
    )
    from turnstone.core.config import apply_config

    apply_config(parser, ["api", "model", "session", "tools", "console", "auth", "mcp", "database"])
    args = parser.parse_args()

    from turnstone.core.log import configure_logging

    configure_logging(level="WARNING", service="cli")

    # Initialize storage backend
    from turnstone.core.storage import init_storage

    db_backend = getattr(args, "db_backend", None) or os.environ.get(
        "TURNSTONE_DB_BACKEND", "sqlite"
    )
    db_url = getattr(args, "db_url", None) or os.environ.get("TURNSTONE_DB_URL", "")
    db_path = getattr(args, "db_path", None) or os.environ.get("TURNSTONE_DB_PATH", "")
    db_pool_size = int(
        getattr(args, "db_pool_size", None) or os.environ.get("TURNSTONE_DB_POOL_SIZE", "5")
    )
    init_storage(db_backend, path=db_path, url=db_url, pool_size=db_pool_size)

    # Prune stale / empty workstreams on startup
    from turnstone.core.memory import prune_workstreams

    prune_workstreams(retention_days=args.retention_days, log_fn=print)

    # Set up readline
    setup_readline()

    # Create client and detect model
    provider_name = args.provider
    api_key = (
        args.api_key
        or os.environ.get("ANTHROPIC_API_KEY" if provider_name == "anthropic" else "OPENAI_API_KEY")
        or "dummy"
    )
    base_url = args.base_url
    if provider_name == "anthropic" and base_url == "http://localhost:8000/v1":
        # Override local default to Anthropic API
        base_url = "https://api.anthropic.com"
    from turnstone.core.providers import create_client

    client = create_client(provider_name, base_url=base_url, api_key=api_key)
    if args.model:
        model = args.model
        detected_ctx = None
    else:
        model, detected_ctx = detect_model(client, provider=provider_name)

    # Use detected context window when the user hasn't explicitly set one
    context_window = args.context_window
    if detected_ctx and context_window == 131072:  # default unchanged
        context_window = detected_ctx

    # Build model registry (reads [models.*] sections from config.toml)
    from turnstone.core.model_registry import load_model_registry

    registry = load_model_registry(
        base_url=base_url,
        api_key=api_key,
        model=model,
        context_window=context_window,
        provider=provider_name,
    )

    # Initialize MCP client (connects to configured MCP servers, if any)
    from turnstone.core.mcp_client import create_mcp_client

    mcp_client = create_mcp_client(
        getattr(args, "mcp_config", None),
        refresh_interval=getattr(args, "mcp_refresh_interval", 14400),
    )

    # ChatSession factory — captures shared config for creating workstreams
    def session_factory(
        ui: SessionUI | None, model_alias: str | None = None, ws_id: str | None = None
    ) -> ChatSession:
        assert ui is not None, "session_factory requires a non-None UI"
        r_client, r_model, r_cfg = registry.resolve(model_alias)
        return ChatSession(
            client=r_client,
            model=r_model,
            ui=ui,
            instructions=args.instructions,
            temperature=args.temperature,
            max_tokens=args.max_tokens,
            tool_timeout=args.tool_timeout,
            reasoning_effort=args.reasoning_effort,
            context_window=r_cfg.context_window,
            compact_max_tokens=args.compact_max_tokens,
            auto_compact_pct=args.auto_compact_pct,
            agent_max_turns=args.agent_max_turns,
            tool_truncation=args.tool_truncation,
            mcp_client=mcp_client,
            registry=registry,
            model_alias=model_alias or registry.default,
            tool_search=args.tool_search,
            tool_search_threshold=args.tool_search_threshold,
            tool_search_max_results=args.tool_search_max_results,
            template=args.template,
        )

    # Create workstream manager and initial workstream
    manager = WorkstreamManager(session_factory)
    ws = manager.create(
        ui_factory=lambda wid: WorkstreamTerminalUI(wid, manager),
    )
    if args.skip_permissions and isinstance(ws.ui, TerminalUI):
        ws.ui.auto_approve = True

    # Handle --resume
    if args.resume:
        from turnstone.core.memory import resolve_workstream

        target_id = resolve_workstream(args.resume)
        if not target_id:
            print(red(f"Workstream not found: {args.resume}"))
            sys.exit(1)
        if ws.session is None:
            print(red("No session available."))
            sys.exit(1)
        if not ws.session.resume(target_id):
            print(red(f"Workstream '{args.resume}' has no messages."))
            sys.exit(1)
        print(f"Resumed workstream {bold(target_id)} ({len(ws.session.messages)} messages)")

    # Background attention notification — write to stderr while user types
    def _bg_attention_notify(ws_id: str, state: WorkstreamState) -> None:
        if state == WorkstreamState.ATTENTION and ws_id != manager.active_id:
            bg_ws = manager.get(ws_id)
            if bg_ws:
                idx = manager.index_of(ws_id)
                sys.stderr.write(
                    f"\a\r\033[s\033[1A\033[K"
                    f"  {YELLOW}\u25c6 {idx}:{bg_ws.name} needs attention{RESET}"
                    f"\033[u"
                )
                sys.stderr.flush()

    manager._on_state_change = _bg_attention_notify

    # Print banner
    print(f"\n{bold('Chat')} with {cyan(model)}")
    if registry.count > 1:
        others = [a for a in registry.list_aliases() if a != registry.default]
        print(f"Models: {registry.default} (default), {', '.join(others)}")
    if mcp_client:
        mcp_tools = mcp_client.get_tools()
        if mcp_tools:
            print(f"MCP tools: {len(mcp_tools)} from {mcp_client.server_count} server(s)")
        from turnstone.core.storage import get_storage as _cli_get_storage

        mcp_client.set_storage(_cli_get_storage())
    print("Type /help for commands, /ws for workstreams, /exit or Ctrl+D to quit.\n")

    # Prompt string -- use a short display name
    display_name = model.split("/")[-1]  # strip path prefixes if any
    if len(display_name) > 30:
        display_name = display_name[:27] + "..."

    # Main loop
    while True:
        try:
            # Show background workstream status if any need attention
            if manager.count > 1:
                _print_ws_status_line(manager)

            # Build prompt with workstream info
            active = manager.get_active()
            if manager.count > 1 and active is not None:
                idx = manager.index_of(active.id)
                prompt_str = f"\001{BOLD}\002{idx}:{active.name}\001{RESET}\002 > "
            else:
                prompt_str = f"\001{BOLD}\002[{display_name}]\001{RESET}\002 > "
            user_input = input(prompt_str)
        except (EOFError, KeyboardInterrupt):
            print()
            break

        user_input = user_input.strip()
        if not user_input:
            continue

        if user_input.startswith("/ws"):
            _handle_ws_command(manager, user_input, args.skip_permissions)
            continue

        if user_input.startswith("/cluster"):
            _handle_cluster_command(user_input, args.console_url, args.auth_token)
            continue

        active = manager.get_active()
        if active is None or active.session is None:
            continue
        if user_input.startswith("/"):
            should_exit = active.session.handle_command(user_input)
            if should_exit:
                break
        else:
            try:
                active.session.send(user_input)
            except KeyboardInterrupt:
                print(f"\n{yellow('Interrupted.')}")
            except Exception as e:
                print(f"\n{red(f'Error: {e}')}")

    # Close active session (removes MCP listener) before shutting down MCP
    if active and active.session:
        active.session.close()
    if mcp_client:
        mcp_client.shutdown()
    registry.shutdown()

    print("Goodbye.")


if __name__ == "__main__":
    main()
