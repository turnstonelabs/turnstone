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

It also has the two routes a client needs to send its prompts as token IDs: a
tokenize route (``/tokenize``, beside ``/v1``) that renders messages with the
same toy template, or tokenizes a text, and a completions route
(``/v1/completions``) that takes a prompt of token IDs and answers with text,
token IDs and log-probabilities, parsing nothing.  A step with ``merge`` makes
the model "generate" one token for the two characters ``ab``, a split the toy
template never writes, so a prompt rendered from messages differs from what
was generated; a step with ``end_token`` ends the turn on another token than
the template's.  A completions prompt that stops inside a conversation this
server rendered before is continued the way that rendering goes on, unless the
model is played with a choice of its own there (``other_choice``); a request
that names the next token then steers it back, where the server is played as
taking such a request.

Where a real server is less kind than a toy one, the stand-in can be told to
be: its template can refuse a conversation, on the chat route and the tokenize
route alike (``refuses``, with ``no_user_query`` as the refusal a real template
is known for); and its completions route can hand on its tokens in parts of a
given size, markers among the other tokens (``part_tokens``), where it
otherwise sends each marker in a part of its own.
"""

from __future__ import annotations

import contextlib
import json
import math
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

NL = chr(10)
START, END, THINK, THINK_END, CALL, CALL_END = 1, 2, 3, 4, 5, 6
# Another token a model can end its turn on; the toy template never writes it.
OTHER_END = 7
# One token for the two characters ``ab``: a split the toy tokenizer never makes.
MERGED, MERGED_TEXT = 900, "ab"
# The token a model with a choice of its own writes where a prompt stops inside
# a turn; the toy template never writes it there.
OWN_CHOICE = 901
_CHAR_BASE = 1000
_MARKERS = {THINK: "<think>", THINK_END: "</think>", CALL: "<tool_call>", CALL_END: "</tool_call>"}


def encode(text: str) -> list[int]:
    return [_CHAR_BASE + ord(c) for c in text]


def encode_merged(text: str) -> list[int]:
    """*text* with every ``ab`` as the one merged token."""
    out: list[int] = []
    for index, part in enumerate(text.split(MERGED_TEXT)):
        out += ([MERGED] if index else []) + encode(part)
    return out


def decode(ids: list[int], *, special: bool = True, markers: bool = True) -> str:
    """The text of *ids*; without *special* the turn tokens are left out, and
    without *markers* the reasoning and call markers too."""
    names = {MERGED: MERGED_TEXT, OWN_CHOICE: "~"}
    names.update(_MARKERS if markers else dict.fromkeys(_MARKERS, ""))
    if special:
        names.update({START: "<start>", END: "<end>", OTHER_END: "<other_end>"})
    else:
        names.update({START: "", END: "", OTHER_END: ""})
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


def render_prompt(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    generation_prompt_added: bool = True,
) -> list[int]:
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
    return out + (generation_prompt() if generation_prompt_added else [])


NO_USER_QUERY = "No user query found in messages."


def no_user_query(messages: list[dict[str, Any]]) -> str | None:
    """What a chat template says of a conversation in which every user message
    is a block of tool results: it finds nothing a user asked, and refuses.
    ``None`` for a conversation it renders.  For ``ScriptedChatServer(refuses=...)``."""
    for message in messages:
        if message.get("role") != "user":
            continue
        text = _text_of(message.get("content")).strip()
        if not (text.startswith("<tool_response>") and text.endswith("</tool_response>")):
            return None
    return NO_USER_QUERY


def _find(ids: list[int], part: list[int]) -> int:
    """How often *part* occurs in *ids*."""
    return sum(1 for i in range(len(ids) - len(part) + 1) if ids[i : i + len(part)] == part)


class VersionFault:
    """A version question answered with *status* and a body that is no version."""

    def __init__(self, status: int = 503, body: str = "no version here") -> None:
        self.status = status
        self.body = body


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
        keep_reply_limit: bool = False,
        chunk_delay: float = 0.0,
        prompt_tokens_per_second: float | None = None,
        first_position_only: bool = False,
        version_answers: list[Any] | None = None,
        tokenize_status: int = 200,
        tokenize_differs: bool = False,
        unstable_rendering: bool = False,
        drop_marker_text: bool = False,
        hide_stop_token: bool = False,
        completions_status: int = 200,
        echo_other_prompt: bool = False,
        other_choice: bool = False,
        logit_bias: str = "taken",
        allowed_token_ids: str = "taken",
        runs_past_end: bool = False,
        refuses: Callable[[list[dict[str, Any]]], str | None] | None = None,
        refusal_status: int = 400,
        part_tokens: int | None = None,
    ) -> None:
        self.script = script
        # What ``GET /version`` answers, one entry per question and the last
        # one from then on: a JSON value, or a ``VersionFault``.  Without any,
        # the server has no such address.  ``version_requests`` notes for each
        # question how many model requests had arrived before it, on whatever
        # route.
        self.version_answers = list(version_answers) if version_answers is not None else None
        self.version_requests: list[dict[str, Any]] = []
        # What can be wrong with the two routes for prompts as token IDs: the
        # tokenize route answers with this status; it renders one token more
        # than the chat route; it renders an assistant turn differently once
        # messages follow it; the completions route leaves the markers out of
        # its text; it does not return the token a turn ended on; it answers
        # with this status; it reports another prompt than it was sent.
        self.tokenize_status = tokenize_status
        self.tokenize_differs = tokenize_differs
        self.unstable_rendering = unstable_rendering
        self.drop_marker_text = drop_marker_text
        self.hide_stop_token = hide_stop_token
        self.completions_status = completions_status
        self.echo_other_prompt = echo_other_prompt
        # A model with a choice of its own: where a completions prompt stops
        # inside a turn, its likeliest next token is not the one the template
        # writes there.  And what the server makes of the two request
        # parameters that name the next token, ``logit_bias`` and
        # ``allowed_token_ids``: ``taken`` (the next token is the one named),
        # ``ignored`` (accepted, without effect) or ``refused`` (status 400).
        self.other_choice = other_choice
        self.steering = {"logit_bias": logit_bias, "allowed_token_ids": allowed_token_ids}
        # A server that does not stop on the end-of-turn token: it goes on
        # generating after it.
        self.runs_past_end = runs_past_end
        # A template that refuses some conversations.  ``refuses`` is asked
        # about the messages of every request that carries messages, on the
        # chat route and on the tokenize route, and returns the words to
        # refuse with or ``None``.  A refused request is answered with
        # ``refusal_status`` and those words, is not counted among the
        # requests, and its route is noted in ``refused``.  A prompt of token
        # IDs holds no messages and is never refused.
        self.refuses = refuses
        self.refusal_status = refusal_status
        self.refused: list[str] = []
        # A completions route that hands on this many tokens a part, whatever
        # they are: a marker's token then shares its part with others.
        self.part_tokens = part_tokens
        # Every conversation the tokenize route rendered, as token IDs.
        self.renderings: list[list[int]] = []
        self.tokenize_requests: list[dict[str, Any]] = []
        # A server whose values are the sampling distribution's at the first
        # generated position and the model's own from there on, as one that
        # takes later tokens of a step from another head might report them.
        self.first_position_only = first_position_only
        # A server that cuts a reply at the request's reply limit, with the
        # finish reason for that; without it a scripted reply goes out whole.
        self.keep_reply_limit = keep_reply_limit
        # Seconds before each part of a streamed reply: a server that generates slowly.
        self.chunk_delay = chunk_delay
        # A server that takes time over a prompt in proportion to its length.
        self.prompt_tokens_per_second = prompt_tokens_per_second
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
                if self.path.split("?")[0] == "/version":
                    owner._answer_version(self)
                    return
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
                    if self.path.endswith("/tokenize"):
                        owner._handle_tokenize(self, body)
                    else:
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
        if self.agent_script is None:
            return False
        if isinstance(body.get("prompt"), list):
            # A prompt of token IDs: the sub-agent's system prompt is in its text.
            return self.agent_marker in decode(body["prompt"])
        messages = body.get("messages") or []
        return bool(messages) and self.agent_marker in _text_of(messages[0].get("content"))

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

    @property
    def version_url(self) -> str:
        assert self._httpd is not None
        return f"http://127.0.0.1:{self._httpd.server_address[1]}/version"

    def _answer_version(self, handler: BaseHTTPRequestHandler) -> None:
        with self._lock:
            asked = len(self.version_requests)
            self.version_requests.append(
                {
                    "chat_requests_before": len(self.requests),
                    "tokenize_requests_before": len(self.tokenize_requests),
                    "authorization": handler.headers.get("Authorization"),
                }
            )
        answers = self.version_answers
        answer = VersionFault(404) if not answers else answers[min(asked, len(answers) - 1)]
        if not isinstance(answer, VersionFault):
            self._send_json(handler, answer)
            return
        data = answer.body.encode("utf-8")
        handler.send_response(answer.status)
        handler.send_header("Content-Type", "text/plain")
        handler.send_header("Content-Length", str(len(data)))
        handler.send_header("Connection", "close")
        handler.end_headers()
        handler.wfile.write(data)
        handler.close_connection = True

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

    def _refuse(
        self,
        handler: BaseHTTPRequestHandler,
        status: int,
        message: str = "scripted failure",
        kind: str = "invalid",
    ) -> None:
        data = json.dumps({"error": {"message": message, "type": kind}})
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(data)))
        handler.send_header("Connection", "close")
        handler.end_headers()
        handler.wfile.write(data.encode("utf-8"))
        handler.close_connection = True

    def _template_refuses(
        self, handler: BaseHTTPRequestHandler, body: dict[str, Any], route: str
    ) -> bool:
        """Answer a request whose conversation the template refuses; whether it did."""
        messages = body.get("messages")
        if self.refuses is None or not isinstance(messages, list):
            return False
        words = self.refuses(messages)
        if words is None:
            return False
        with self._lock:
            self.refused.append(route)
        self._refuse(handler, self.refusal_status, words, "BadRequestError")
        return True

    def _render(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        generation_prompt_added: bool = True,
    ) -> list[int]:
        """The toy template as this server applies it, on either route."""
        ids = render_prompt(messages, tools, generation_prompt_added)
        last_turn = max(
            (i for i, m in enumerate(messages) if m.get("role") == "assistant"), default=-1
        )
        if self.unstable_rendering and 0 <= last_turn < len(messages) - 1:
            # An assistant turn that is not the last message any more is
            # rendered with one character more, right at its start.
            before = len(render_prompt(messages[:last_turn], tools, False))
            ids = ids[:before] + encode("~") + ids[before:]
        return ids

    def _handle_tokenize(self, handler: BaseHTTPRequestHandler, body: dict[str, Any]) -> None:
        if self._template_refuses(handler, body, "tokenize"):
            return
        with self._lock:
            self.tokenize_requests.append(body)
        if self.tokenize_status != 200:
            self._refuse(handler, self.tokenize_status)
            return
        if "messages" in body:
            messages = [dict(m) for m in body["messages"]]
            ids = self._render(messages, body.get("tools"), body.get("add_generation_prompt", True))
            if self.tokenize_differs:
                ids = encode("#") + ids
            with self._lock:
                self.renderings.append(ids)
        else:
            text = str(body.get("prompt", ""))
            by_text = {name: token for token, name in _MARKERS.items()}
            ids = [by_text[text]] if text in by_text else encode(text)
        self._send_json(handler, {"tokens": ids, "count": len(ids), "max_model_len": 1 << 20})

    def _handle_chat(self, handler: BaseHTTPRequestHandler, body: dict[str, Any]) -> None:
        if self._template_refuses(handler, body, "chat"):
            return
        with self._lock:
            call_index = len(self.requests)
            self.requests.append(body)
        if self.delay:
            time.sleep(self.delay)
        by_ids = isinstance(body.get("prompt"), list)
        status = self.agent_status if self.is_agent_request(body) else 200
        if self.faults.get(call_index) == "http_500":
            status = 500
        if by_ids and self.completions_status != 200:
            status = self.completions_status
        if by_ids and any(body.get(k) and v == "refused" for k, v in self.steering.items()):
            status = 400
        if status != 200:
            self._refuse(handler, status)
            return
        script = self.agent_script if self.is_agent_request(body) else self.script
        assert script is not None
        continued: list[int] | None = None
        if by_ids:
            prompt_ids = [int(t) for t in body["prompt"]]
            opening = generation_prompt()
            if prompt_ids[-len(opening) :] != opening:
                # A prompt that stops inside a conversation: go on as a
                # rendering this server made of it goes on.
                with self._lock:
                    known = [r for r in self.renderings if r[: len(prompt_ids)] == prompt_ids]
                rest = max(known, key=len)[len(prompt_ids) :] if known else encode("?")
                continued = rest[: rest.index(END) + 1] if END in rest else rest
                if self.runs_past_end and continued[-1:] == [END]:
                    continued = continued + encode(" and on")
                if self.other_choice:
                    continued = [OWN_CHOICE, *continued]
                named = [body[k] for k, v in self.steering.items() if body.get(k) and v == "taken"]
                if named:
                    # The token the request names, and after any token but the
                    # one a turn ends on, more.
                    wanted = int(next(iter(named[0])))
                    goes_on = wanted != END or self.runs_past_end
                    continued = [wanted, *(encode(" and on") if goes_on else [])]
            turn = max(_find(prompt_ids, opening) - 1, 0)
        else:
            messages = [dict(m) for m in body.get("messages") or []]
            turn = sum(1 for m in messages if m.get("role") == "assistant")
            if (
                self.edit_history_from_call is not None
                and call_index >= self.edit_history_from_call
                and messages
            ):
                messages[0]["content"] = _text_of(messages[0].get("content")) + " (edited)"
            prompt_ids = self._render(messages, body.get("tools"))
        step = script[turn] if turn < len(script) else {"content": "done"}
        if self.prompt_tokens_per_second:
            time.sleep(len(prompt_ids) / self.prompt_tokens_per_second)
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
        # The chat route lists alternatives with ``top_logprobs``, the completions
        # route with a number of them under ``logprobs``.
        self._request.alternatives = bool(body.get("top_logprobs")) or (
            by_ids and bool(body.get("logprobs"))
        )
        want_ids = bool(body.get("return_token_ids"))
        want_logprobs = bool(body.get("logprobs"))
        calls = [
            (c["name"], c.get("arguments", {}), c.get("raw")) for c in step.get("tool_calls") or []
        ]
        finish = step.get("finish_reason") or ("tool_calls" if calls else "stop")
        pieces = self._pieces(step, calls, finish, call_index)
        limit = body.get("max_completion_tokens", body.get("max_tokens"))
        if continued is not None:
            pieces = [([t], {}) for t in continued]
            finish = "stop"
        if (self.keep_reply_limit or continued is not None) and isinstance(limit, int):
            pieces, cut = self._cut(pieces, limit)
            finish = "length" if cut else finish
        if by_ids:
            # The completions route reads nothing: a reply ends or is cut.
            finish = "length" if finish == "length" else "stop"
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
        if by_ids:
            self._completion(handler, body, base, prompt_ids, pieces, finish, usage, fault)
        elif body.get("stream"):
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
            tokens = encode_merged(text) if step.get("merge") else encode(text)
            for i in range(0, len(tokens), size):
                part = tokens[i : i + size]
                pieces.append((part, {key: decode(part)}))

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
            pieces.append(([step.get("end_token", END)], {}))
            if self.runs_past_end:
                spread(" and on", "content")
        return pieces

    @staticmethod
    def _cut(
        pieces: list[tuple[list[int], dict[str, Any]]], limit: int
    ) -> tuple[list[tuple[list[int], dict[str, Any]]], bool]:
        """*pieces* up to *limit* tokens, and whether anything was left out."""
        kept: list[tuple[list[int], dict[str, Any]]] = []
        room = limit
        for ids, delta in pieces:
            if len(ids) > room:
                if room > 0:
                    # Part of a group: its tokens, without the text they belong to.
                    kept.append((ids[:room], {}))
                return kept, True
            kept.append((ids, delta))
            room -= len(ids)
        return kept, False

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

    def _completion(
        self,
        handler: BaseHTTPRequestHandler,
        body: dict[str, Any],
        base: dict[str, Any],
        prompt_ids: list[int],
        pieces: list[tuple[list[int], dict[str, Any]]],
        finish: str,
        usage: dict[str, Any],
        fault: str | None,
    ) -> None:
        """A reply of the completions route: text, token IDs, log-probabilities."""
        want_ids = bool(body.get("return_token_ids"))
        want_logprobs = body.get("logprobs") is not None
        groups = [ids for ids, _delta in pieces]
        if self.hide_stop_token and groups and groups[-1] in ([END], [OTHER_END]):
            groups = groups[:-1]
        if self.part_tokens:
            tokens = [t for ids in groups for t in ids]
            size = self.part_tokens
            groups = [tokens[i : i + size] for i in range(0, len(tokens), size)]

        def part(number: int, ids: list[int], offset: int, last: bool) -> dict[str, Any]:
            choice: dict[str, Any] = {
                "index": 0,
                "text": decode(ids, special=False, markers=not self.drop_marker_text),
                "logprobs": None,
                "finish_reason": finish if last else None,
            }
            # A fault spoils the second group of tokens of the reply.
            spoil = fault if number == 1 else None
            if want_ids and spoil != "drop_tokens":
                choice["token_ids"] = ids
            if want_ids and number == 0:
                choice["prompt_token_ids"] = (
                    [*prompt_ids, 0] if self.echo_other_prompt else prompt_ids
                )
            if want_logprobs and spoil not in ("skip_logprobs", "drop_tokens"):
                entries = self._logprob_entries(ids, offset)
                names = [e["token"] for e in entries]
                if spoil == "wrong_ids":
                    names = ["token_id:0" for _ in names]
                choice["logprobs"] = {
                    "tokens": names,
                    "token_logprobs": [
                        None if spoil == "null_logprob" else e["logprob"] for e in entries
                    ],
                    "top_logprobs": [
                        {a["token"]: a["logprob"] for a in e["top_logprobs"]} for e in entries
                    ],
                }
            return {**base, "object": "text_completion", "choices": [choice]}

        parts = []
        offset = 0
        for number, ids in enumerate(groups):
            parts.append(part(number, ids, offset, number == len(groups) - 1))
            offset += len(ids)
        if not parts:
            parts.append(part(0, [], 0, True))
        if not body.get("stream"):
            whole = part(0, [t for ids in groups for t in ids], 0, True)
            if fault != "no_usage":
                whole["usage"] = usage
            self._send_json(handler, whole)
            return
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.send_header("Cache-Control", "no-cache")
        handler.send_header("Connection", "close")
        handler.end_headers()

        def send(payload: dict[str, Any]) -> None:
            handler.wfile.write(("data: " + json.dumps(payload) + NL + NL).encode("utf-8"))
            handler.wfile.flush()

        for payload in parts:
            if self.chunk_delay:
                time.sleep(self.chunk_delay)
            send(payload)
        if (body.get("stream_options") or {}).get("include_usage") and fault != "no_usage":
            send({**base, "object": "text_completion", "choices": [], "usage": usage})
        handler.wfile.write(("data: [DONE]" + NL + NL).encode("utf-8"))
        handler.wfile.flush()
        handler.close_connection = True

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
            if self.chunk_delay:
                time.sleep(self.chunk_delay)
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
