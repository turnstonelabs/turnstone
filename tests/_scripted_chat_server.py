"""A scripted chat-completions server for tests. Standard library only.

It speaks enough of the chat-completions protocol for the SDK client and for
the session's provider: streamed (server-sent events) and non-streamed replies,
tool calls, a separate reasoning channel, usage and, when the request asks for
them, ``prompt_token_ids``, per-choice ``token_ids`` and ``logprobs``.

Prompts are rendered with a toy template and a toy tokenizer (one token per
character plus a few marker tokens), so token sequences behave like a real
server's: the prompt of a later request starts with the earlier prompt plus the
earlier generated tokens, provided the client sends the conversation back
unchanged, reasoning included.  A client that drops or rewrites anything it
sent before gets a prompt that does not.

What the model "says" is a script: step ``i`` answers the request that holds
``i`` assistant messages.  A step is a dict with optional ``reasoning``,
``content``, ``tool_calls`` (a list of ``{"name": ..., "arguments": {...}}``;
``arguments`` may also be a string, sent as it is, to script malformed calls;
``raw`` gives the spelling the model "wrote" for a typed argument) and
``finish_reason``.

The toy template writes a tool call the way a text-format template does: a
string argument as it is, any other value in JSON spelling.  A boolean the model
wrote as ``True`` therefore replays as ``true`` unless the client sends back the
text the model wrote.
"""

from __future__ import annotations

import contextlib
import json
import math
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

NL = chr(10)
START, END, THINK, THINK_END, CALL, CALL_END = 1, 2, 3, 4, 5, 6
_CHAR_BASE = 1000


def encode(text: str) -> list[int]:
    return [_CHAR_BASE + ord(c) for c in text]


def decode(ids: list[int]) -> str:
    names = {
        START: "<start>",
        END: "<end>",
        THINK: "<think>",
        THINK_END: "</think>",
        CALL: "<tool_call>",
        CALL_END: "</tool_call>",
    }
    return "".join(chr(i - _CHAR_BASE) if i >= _CHAR_BASE else names.get(i, "<?>") for i in ids)


def fake_logprob(token_id: int, position: int) -> float:
    return -(((token_id * 7 + position * 13) % 50) / 100.0) - 0.001


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return ""


def call_text(name: str, arguments: Any, raw: dict[str, str] | None = None) -> str:
    """One tool call as the toy template writes it, between the two call markers.

    The function name, then a block per argument::

        <function=NAME>
        <parameter=KEY>
        VALUE
        </parameter>
        </function>

    A string value is written as it is and any other value in JSON spelling.  A
    model may have spelled a typed value another way (``True`` for ``true``);
    *raw* gives those spellings for the text the model "generated", and nothing
    in the parsed arguments can reproduce them.
    """
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except ValueError:
            return NL + "<function=" + name + ">" + NL + arguments + NL + "</function>" + NL
    if not isinstance(arguments, dict):
        return NL + "<function=" + name + ">" + NL + json.dumps(arguments) + NL + "</function>" + NL
    out = NL + "<function=" + name + ">" + NL
    for key, value in arguments.items():
        spelled = (raw or {}).get(key)
        if spelled is None:
            spelled = value if isinstance(value, str) else json.dumps(value)
        out += "<parameter=" + key + ">" + NL + spelled + NL + "</parameter>" + NL
    return out + "</function>" + NL


def _wire_arguments(arguments: Any) -> str:
    """Arguments as the reply carries them: a scripted string goes out as it is."""
    return arguments if isinstance(arguments, str) else json.dumps(arguments)


def assistant_tokens(
    reasoning: str, content: str, calls: list[tuple[Any, ...]], ended: bool = True
) -> list[int]:
    """Tokens an assistant turn consists of after the generation prompt.

    *calls* are ``(name, arguments)`` or ``(name, arguments, raw spellings)``.
    """
    out = encode(reasoning) + [THINK_END] + encode(NL + NL) + encode(content)
    for call in calls:
        out += [CALL] + encode(call_text(*call)) + [CALL_END]
    if ended:
        out.append(END)
    return out


def generation_prompt() -> list[int]:
    return [START] + encode("assistant" + NL) + [THINK] + encode(NL)


def render_prompt(messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None) -> list[int]:
    """The toy chat template: conversation so far plus the generation prompt."""
    out: list[int] = []
    if tools:
        names = [t.get("function", {}).get("name", "") for t in tools]
        out += [START] + encode("tools" + NL + json.dumps(names)) + [END] + encode(NL)
    for message in messages:
        role = message.get("role", "")
        if role == "assistant":
            reasoning = message.get("reasoning") or message.get("reasoning_content") or ""
            calls = [
                (
                    c.get("function", {}).get("name", ""),
                    c.get("function", {}).get("arguments", "{}"),
                )
                for c in message.get("tool_calls") or []
            ]
            out += generation_prompt() + assistant_tokens(
                reasoning, _text_of(message.get("content")), calls
            )
            out += encode(NL)
        else:
            out += (
                [START] + encode(role + NL + _text_of(message.get("content"))) + [END] + encode(NL)
            )
    return out + generation_prompt()


class ScriptedChatServer:
    """Scripted server on a free local port. Use as a context manager."""

    def __init__(
        self,
        script: list[dict[str, Any]],
        *,
        model: str = "fake-model",
        delay: float = 0.0,
        edit_history_from_call: int | None = None,
        skip_logprobs_on_call: int | None = None,
        faults: dict[int, str] | None = None,
        confident: bool = False,
        confident_first: int = 0,
        agent_status: int = 200,
        plain_names: bool = False,
        tokens_per_chunk: int = 3,
        sampler_logprobs: bool = False,
        reflects: tuple[str, ...] | None = None,
        agent_script: list[dict[str, Any]] | None = None,
        agent_marker: str = "# Task Agent",
        first_position_only: bool = False,
    ) -> None:
        self.script = script
        # A server whose values are the sampling distribution's at the first
        # generated position and the model's own from there on, as one that
        # takes later tokens of a step from another head might report them.
        self.first_position_only = first_position_only
        # A second conversation: requests whose first message holds
        # ``agent_marker`` (a sub-agent's system prompt) are answered from here.
        self.agent_script = agent_script
        self.agent_marker = agent_marker
        self.model = model
        self.delay = delay
        # From this call on, the first message is rendered with extra text, as if earlier
        # context had been rewritten.
        self.edit_history_from_call = edit_history_from_call
        # On this call one chunk carries token IDs without log-probabilities.
        # What goes wrong on which call (by its number among all requests):
        # ``skip_logprobs`` (token IDs without log-probabilities), ``drop_tokens``
        # (a group of tokens missing from both), ``null_logprob``, ``wrong_ids``
        # (log-probabilities named for other tokens), ``no_usage``, ``http_500``
        # (the request is answered with a server error).
        self.faults: dict[int, str] = dict(faults or {})
        if skip_logprobs_on_call is not None:
            self.faults[skip_logprobs_on_call] = "skip_logprobs"
        # A model so sure of every token that its own log-probabilities are near
        # zero; or so sure of the first tokens of every reply only, that many of
        # them, as a model is that opens every reply with the same words.
        self.confident = confident
        self.confident_first = confident_first
        # The status a sub-agent's request is answered with; not 200 to fail it.
        self.agent_status = agent_status
        # A server that never names tokens by ID, whatever the request asks.
        self.plain_names = plain_names
        self.tokens_per_chunk = tokens_per_chunk
        # The sampler settings the reported log-probabilities are taken after.  The toy
        # model's distribution at every position has two tokens, the emitted one and one
        # other: a request that leaves the sampler one choice (``top_k`` of 1, a tiny
        # ``top_p``) gets exactly zero where that setting is reflected, and a temperature
        # sharpens the pair.  ``sampler_logprobs`` reflects all three.
        self.reflects: tuple[str, ...] = (
            reflects
            if reflects is not None
            else (("temperature", "top_k", "top_p") if sampler_logprobs else ())
        )
        self._request = threading.local()
        self.requests: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._connections: set[socket.socket] = set()
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # -- lifecycle ---------------------------------------------------------------

    def start(self) -> ScriptedChatServer:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:
                pass

            def do_GET(self) -> None:
                owner._send_json(
                    self,
                    {
                        "object": "list",
                        "data": [{"id": owner.model, "object": "model"}],
                    },
                )

            def setup(self) -> None:
                super().setup()
                with owner._lock:
                    owner._connections.add(self.connection)

            def finish(self) -> None:
                with owner._lock:
                    owner._connections.discard(self.connection)
                with contextlib.suppress(OSError):
                    super().finish()

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length) or b"{}")
                # A client that stops reading mid-reply is part of some tests.
                with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                    owner._handle_chat(self, body)

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        # ``server_close`` joins the handler threads, so ``stop`` leaves none behind.
        self._httpd.daemon_threads = False
        self._httpd.block_on_close = True
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            # Wake any handler still blocked on its client, then join them all.
            with self._lock:
                connections = list(self._connections)
            for connection in connections:
                with contextlib.suppress(OSError):
                    connection.shutdown(socket.SHUT_RDWR)
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def __enter__(self) -> ScriptedChatServer:
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.stop()

    def is_agent_request(self, body: dict[str, Any]) -> bool:
        messages = body.get("messages") or []
        return (
            self.agent_script is not None
            and bool(messages)
            and self.agent_marker in _text_of(messages[0].get("content"))
        )

    @property
    def parent_requests(self) -> list[dict[str, Any]]:
        return [r for r in list(self.requests) if not self.is_agent_request(r)]

    @property
    def agent_requests(self) -> list[dict[str, Any]]:
        return [r for r in list(self.requests) if self.is_agent_request(r)]

    @property
    def base_url(self) -> str:
        assert self._httpd is not None
        return f"http://127.0.0.1:{self._httpd.server_address[1]}/v1"

    # -- request handling --------------------------------------------------------

    def _send_json(self, handler: BaseHTTPRequestHandler, payload: dict[str, Any]) -> None:
        data = json.dumps(payload).encode("utf-8")
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(data)))
        # One request per connection: no handler thread waits on an idle client.
        handler.send_header("Connection", "close")
        handler.end_headers()
        handler.wfile.write(data)
        handler.close_connection = True

    def _handle_chat(self, handler: BaseHTTPRequestHandler, body: dict[str, Any]) -> None:
        with self._lock:
            call_index = len(self.requests)
            self.requests.append(body)
        if self.delay:
            time.sleep(self.delay)
        status = self.agent_status if self.is_agent_request(body) else 200
        if self.faults.get(call_index) == "http_500":
            status = 500
        if status != 200:
            data = json.dumps({"error": {"message": "scripted failure", "type": "invalid"}})
            handler.send_response(status)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", str(len(data)))
            handler.send_header("Connection", "close")
            handler.end_headers()
            handler.wfile.write(data.encode("utf-8"))
            handler.close_connection = True
            return
        messages = [dict(m) for m in body.get("messages") or []]
        turn = sum(1 for m in messages if m.get("role") == "assistant")
        script = self.agent_script if self.is_agent_request(body) else self.script
        assert script is not None
        step = script[turn] if turn < len(script) else {"content": "done"}
        if (
            self.edit_history_from_call is not None
            and call_index >= self.edit_history_from_call
            and messages
        ):
            messages[0]["content"] = _text_of(messages[0].get("content")) + " (edited)"
        prompt_ids = render_prompt(messages, body.get("tools"))
        top_p = body.get("top_p")
        self._request.certain = ("top_k" in self.reflects and body.get("top_k") == 1) or (
            "top_p" in self.reflects and isinstance(top_p, (int, float)) and top_p <= 0.01
        )
        temperature = body.get("temperature")
        self._request.temperature = (
            float(temperature)
            if "temperature" in self.reflects and isinstance(temperature, (int, float))
            else 1.0
        )
        # ``plain_names``: a server that ignores the request to name tokens by ID.
        self._request.id_names = bool(body.get("return_tokens_as_token_ids")) and (
            self.faults.get(call_index) != "plain_names" and not self.plain_names
        )
        self._request.alternatives = bool(body.get("top_logprobs"))
        want_ids = bool(body.get("return_token_ids"))
        want_logprobs = bool(body.get("logprobs"))
        calls = [
            (c["name"], c.get("arguments", {}), c.get("raw")) for c in step.get("tool_calls") or []
        ]
        finish = step.get("finish_reason") or ("tool_calls" if calls else "stop")
        pieces = self._pieces(step, calls, finish, call_index)
        completion_tokens = sum(len(ids) for ids, _delta in pieces)
        usage = {
            "prompt_tokens": len(prompt_ids),
            "completion_tokens": completion_tokens,
            "total_tokens": len(prompt_ids) + completion_tokens,
        }
        base = {
            "id": f"chatcmpl-scripted-{call_index}",
            "created": int(time.time()),
            "model": body.get("model"),
        }
        fault = self.faults.get(call_index)
        if body.get("stream"):
            self._stream(
                handler,
                body,
                base,
                prompt_ids,
                pieces,
                finish,
                usage,
                want_ids,
                want_logprobs,
                fault,
            )
        else:
            self._whole(
                handler,
                base,
                prompt_ids,
                pieces,
                step,
                calls,
                finish,
                usage,
                want_ids,
                want_logprobs,
                call_index,
            )

    def _pieces(
        self,
        step: dict[str, Any],
        calls: list[tuple[Any, ...]],
        finish: str,
        call_index: int,
    ) -> list[tuple[list[int], dict[str, Any]]]:
        """Generated tokens in transmission order, each group with the delta it travels with."""
        size = self.tokens_per_chunk
        pieces: list[tuple[list[int], dict[str, Any]]] = []

        def spread(text: str, key: str) -> None:
            for i in range(0, len(text), size):
                part = text[i : i + size]
                pieces.append((encode(part), {key: part}))

        spread(step.get("reasoning") or "", "reasoning")
        pieces.append(([THINK_END] + encode(NL + NL), {}))
        spread(step.get("content") or "", "content")
        for index, (name, arguments, raw) in enumerate(calls):
            call_id = f"call_{call_index}_{index}"
            header = {
                "index": index,
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": ""},
            }
            pieces.append(([CALL], {"tool_calls": [header]}))
            text = call_text(name, arguments, raw)
            for i in range(0, len(text), size):
                delta: dict[str, Any] = {}
                if i == 0:
                    delta = {
                        "tool_calls": [
                            {
                                "index": index,
                                "function": {"arguments": _wire_arguments(arguments)},
                            }
                        ]
                    }
                pieces.append((encode(text[i : i + size]), delta))
            pieces.append(([CALL_END], {}))
        if finish != "length":
            pieces.append(([END], {}))
        return pieces

    def _logprob_entries(self, ids: list[int], offset: int) -> list[dict[str, Any]]:
        entries = []
        temperature = getattr(self._request, "temperature", 1.0) or 1.0
        for i, t in enumerate(ids):
            text = decode([t])
            sure = self.confident or offset + i < self.confident_first
            own = -1e-7 if sure else fake_logprob(t, offset + i)
            # The other token of the pair holds the rest of the probability.
            other = math.log(max(1.0 - math.exp(own), 1e-300))
            reflected = not self.first_position_only or offset + i == 0
            if not reflected:
                value, other_value = own, other
            elif getattr(self._request, "certain", False):
                value, other_value = 0.0, -1e9
            else:
                normaliser = math.log(math.exp(own / temperature) + math.exp(other / temperature))
                value = own / temperature - normaliser if temperature != 1.0 else own
                other_value = other / temperature - normaliser if temperature != 1.0 else other
            name = f"token_id:{t}" if getattr(self._request, "id_names", False) else text
            alternatives = []
            if getattr(self._request, "alternatives", False):
                alternatives = [
                    {
                        "token": name,
                        "logprob": value,
                        "bytes": list(text.encode("utf-8", errors="replace")),
                    },
                    {"token": "<other>", "logprob": other_value, "bytes": [0]},
                ]
            entries.append(
                {
                    "token": name,
                    "logprob": value,
                    "bytes": list(text.encode("utf-8", errors="replace")),
                    "top_logprobs": alternatives,
                }
            )
        return entries

    def _stream(
        self,
        handler: BaseHTTPRequestHandler,
        body: dict[str, Any],
        base: dict[str, Any],
        prompt_ids: list[int],
        pieces: list[tuple[list[int], dict[str, Any]]],
        finish: str,
        usage: dict[str, Any],
        want_ids: bool,
        want_logprobs: bool,
        fault: str | None,
    ) -> None:
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.send_header("Cache-Control", "no-cache")
        handler.send_header("Connection", "close")
        handler.end_headers()

        def send(payload: dict[str, Any]) -> None:
            handler.wfile.write(("data: " + json.dumps(payload) + NL + NL).encode("utf-8"))
            handler.wfile.flush()

        def chunk(delta: dict[str, Any], finish_reason: str | None = None) -> dict[str, Any]:
            choice = {
                "index": 0,
                "delta": delta,
                "finish_reason": finish_reason,
                "logprobs": None,
            }
            return {**base, "object": "chat.completion.chunk", "choices": [choice]}

        first = chunk({"role": "assistant", "content": ""})
        if want_ids:
            first["prompt_token_ids"] = prompt_ids
        send(first)
        offset = 0
        for number, (ids, delta) in enumerate(pieces):
            last = number == len(pieces) - 1
            payload = chunk(delta, finish if last else None)
            # A fault spoils the second group of tokens of the reply.
            spoil = fault if number == 1 else None
            if want_ids and spoil != "drop_tokens":
                payload["choices"][0]["token_ids"] = ids
            if want_logprobs and spoil not in ("skip_logprobs", "drop_tokens"):
                entries = self._logprob_entries(ids, offset)
                if spoil == "null_logprob":
                    entries[0]["logprob"] = None
                if spoil == "wrong_ids":
                    for entry in entries:
                        entry["token"] = "token_id:0"
                payload["choices"][0]["logprobs"] = {"content": entries}
            offset += len(ids)
            send(payload)
        if (body.get("stream_options") or {}).get("include_usage") and fault != "no_usage":
            send(
                {
                    **base,
                    "object": "chat.completion.chunk",
                    "choices": [],
                    "usage": usage,
                }
            )
        handler.wfile.write(("data: [DONE]" + NL + NL).encode("utf-8"))
        handler.wfile.flush()
        handler.close_connection = True

    def _whole(
        self,
        handler: BaseHTTPRequestHandler,
        base: dict[str, Any],
        prompt_ids: list[int],
        pieces: list[tuple[list[int], dict[str, Any]]],
        step: dict[str, Any],
        calls: list[tuple[Any, ...]],
        finish: str,
        usage: dict[str, Any],
        want_ids: bool,
        want_logprobs: bool,
        call_index: int,
    ) -> None:
        ids = [t for group, _delta in pieces for t in group]
        message: dict[str, Any] = {
            "role": "assistant",
            "content": step.get("content") or "",
        }
        if step.get("reasoning"):
            message["reasoning"] = step["reasoning"]
        if calls:
            message["tool_calls"] = [
                {
                    "id": f"call_{call_index}_{i}",
                    "type": "function",
                    "function": {"name": name, "arguments": _wire_arguments(arguments)},
                }
                for i, (name, arguments, _raw) in enumerate(calls)
            ]
        choice: dict[str, Any] = {
            "index": 0,
            "message": message,
            "finish_reason": finish,
            "logprobs": None,
        }
        if want_ids:
            choice["token_ids"] = ids
        if want_logprobs:
            choice["logprobs"] = {"content": self._logprob_entries(ids, 0)}
        payload = {
            **base,
            "object": "chat.completion",
            "choices": [choice],
            "usage": usage,
        }
        if want_ids:
            payload["prompt_token_ids"] = prompt_ids
        self._send_json(handler, payload)
