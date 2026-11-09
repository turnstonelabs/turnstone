"""web_fetch reads pages with the model ``tools.web_fetch_alias`` names, else with its caller's.

Empty means the model that called web_fetch: the session's, or a task agent's at the effort the
agent's own turns use. A set alias reads with its own binding and settings, is refused before
approval while it can't be used (it isn't loaded, or its client can't be built), and is never
replaced by another model.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from tests._session_helpers import make_session, scripted_chat_client
from turnstone.core.model_registry import (
    ModelClientConstructionError,
    ModelConfig,
    ModelRegistry,
)
from turnstone.core.model_turn import resolve_model_binding
from turnstone.core.providers._protocol import CompletionResult
from turnstone.core.trajectory import AttachmentRef, Turn

_URL = "https://example.com/"
_QUESTION = "What does it say?"


def _registry(**routing: Any) -> ModelRegistry:
    def cfg(alias: str, **kw: Any) -> ModelConfig:
        return ModelConfig(
            alias,
            f"http://{alias}.example.com/v1",
            "k",
            f"{alias}-model",
            provider="openai-compatible",
            **kw,
        )

    return ModelRegistry(
        models={
            "main": cfg("main", context_window=65_536),
            "reader": cfg(
                "reader",
                context_window=16_384,
                max_tokens=1_000,
                temperature=0.2,
                reasoning_effort="low",
                capabilities={"supports_pdf": True},
            ),
            "task": cfg("task", context_window=32_768, reasoning_effort="medium"),
        },
        default="main",
        **routing,
    )


@pytest.fixture
def store(tmp_db: Any, sqlite_backend_factory: Any) -> Any:
    from turnstone.core.config_store import ConfigStore

    return ConfigStore(sqlite_backend_factory(str(tmp_db), create_tables=True))


def _session(store: Any, registry: ModelRegistry | None = None, **kwargs: Any) -> Any:
    return make_session(
        registry=registry or _registry(),
        model_alias="main",
        config_store=store,
        **kwargs,
    )


def _page() -> MagicMock:
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.headers = {"content-type": "text/plain"}
    response.text = "The page body that holds the answer."
    response.content = response.text.encode()
    response.url = _URL
    return response


def _fetch(session: Any, **item: Any) -> tuple[str, MagicMock, MagicMock]:
    """Run one text fetch with the network and the model call replaced."""
    with (
        patch("turnstone.core.session.fetch_with_ssrf_guard", return_value=_page()) as fetch,
        patch.object(
            session,
            "_utility_completion",
            return_value=CompletionResult(content="Extracted answer."),
        ) as utility,
    ):
        _, answer = session._exec_web_fetch(
            {"call_id": "c1", "url": _URL, "question": _QUESTION, **item}
        )
    return answer, utility, fetch


def _prepare(session: Any) -> dict[str, Any]:
    item: dict[str, Any] = session._prepare_web_fetch("c1", {"url": _URL, "question": _QUESTION})
    return item


_NOT_LOADED = (
    "Error: web_fetch's model 'gone' is not available: no enabled model definition by that "
    "name is loaded. An operator has to fix it."
)
_CANT_BUILD = (
    "Error: web_fetch's model 'reader' can't be used: its client couldn't be built "
    "(failed to construct openai-compatible client for model alias 'reader': OSError "
    "(details in server log)). An operator has to fix it."
)


def _client_failure() -> ModelClientConstructionError:
    """The registry's error for a loaded alias whose SDK client can't be built."""
    return ModelClientConstructionError(
        "failed to construct openai-compatible client for model alias 'reader': "
        "OSError (details in server log)"
    )


class TestEmptyReadsWithTheCaller:
    def test_the_session_reads_with_its_own_lane_and_settings(self, store: Any) -> None:
        session = _session(store, reasoning_effort="high", temperature=0.3, max_tokens=512)

        answer, utility, _ = _fetch(session)

        assert answer == "Extracted answer."
        kw = utility.call_args.kwargs
        assert kw["lane"].alias == "main"
        assert kw["lane"].reasoning_effort == "high"
        assert kw["lane"].temperature == 0.3
        assert kw["max_tokens"] == 512

    def test_a_lane_handed_over_on_the_execution_copy_reads_instead(self, store: Any) -> None:
        """A task agent hands its lane over; the page budget follows that lane's window."""
        session = _session(store, max_tokens=16_384)
        task_lane = resolve_model_binding(session._registry, "task", config_store=store).lane
        caller = dataclasses.replace(task_lane, reasoning_effort="minimal")

        _, utility, _ = _fetch(session, _caller_lane=caller)

        kw = utility.call_args.kwargs
        assert kw["lane"] is caller
        assert kw["max_tokens"] == 32_768 // 4

    def test_the_cli_has_no_setting_to_read(self) -> None:
        session = make_session(registry=_registry(), model_alias="main", config_store=None)

        _, utility, _ = _fetch(session)

        assert utility.call_args.kwargs["lane"].alias == "main"
        assert session._prepare_web_fetch("c1", {"url": _URL, "question": _QUESTION})[
            "needs_approval"
        ]


class TestASetAliasReadsWithItsOwnBinding:
    def test_its_client_window_and_settings_not_the_sessions(self, store: Any) -> None:
        store.set("tools.web_fetch_alias", "reader", changed_by="test")
        session = _session(store, reasoning_effort="high", temperature=0.3, max_tokens=4_096)

        _, utility, _ = _fetch(session)

        kw = utility.call_args.kwargs
        lane = kw["lane"]
        assert (lane.alias, lane.model) == ("reader", "reader-model")
        assert lane.client is session._registry.get_client("reader")
        assert lane.reasoning_effort == "low"
        assert lane.temperature == 0.2
        # Its own max_tokens rung, which is under its window's reserve (16_384 // 4).
        assert kw["max_tokens"] == 1_000

    def test_naming_the_sessions_alias_still_reads_with_that_aliass_settings(
        self, store: Any
    ) -> None:
        """A user's /reason choice and the workstream's temperature don't reach it."""
        store.set("tools.web_fetch_alias", "main", changed_by="test")
        session = _session(store, reasoning_effort="high", temperature=0.3)

        _, utility, _ = _fetch(session)

        lane = utility.call_args.kwargs["lane"]
        assert lane.alias == "main"
        assert lane.reasoning_effort is None
        assert lane.temperature is None

    def test_it_outranks_a_lane_handed_over_by_a_task_agent(self, store: Any) -> None:
        store.set("tools.web_fetch_alias", "reader", changed_by="test")
        session = _session(store)
        caller = resolve_model_binding(session._registry, "task", config_store=store).lane

        _, utility, _ = _fetch(session, _caller_lane=caller)

        assert utility.call_args.kwargs["lane"].alias == "reader"

    def test_its_capabilities_pick_the_pdf_route(self, store: Any) -> None:
        """A native-PDF reader gets the document itself; the session's model has no PDF input."""
        store.set("tools.web_fetch_alias", "reader", changed_by="test")
        session = _session(store)
        assert not session._primary_lane().capabilities.supports_pdf
        captured: dict[str, Any] = {}

        def complete(turns: list[Turn], **kwargs: Any) -> CompletionResult:
            ref = next(b for b in turns[1].content if isinstance(b, AttachmentRef))
            captured["part"] = kwargs["resolve_attachments"]([ref.attachment_id])[ref.attachment_id]
            captured["lane"] = kwargs["lane"]
            return CompletionResult(content="PDF answer.")

        pdf = MagicMock()
        pdf.raise_for_status.return_value = None
        pdf.headers = {"content-type": "application/pdf"}
        pdf.content = b"%PDF-1.4 fetched body"
        pdf.url = "https://example.com/a.pdf"
        with (
            patch("turnstone.core.session.fetch_with_ssrf_guard", return_value=pdf),
            patch.object(session, "_utility_completion", side_effect=complete),
        ):
            _, answer = session._exec_web_fetch(
                {"call_id": "pdf", "url": "https://example.com/a.pdf", "question": _QUESTION}
            )

        assert answer == "PDF answer."
        assert captured["lane"].alias == "reader"
        assert captured["part"]["type"] == "document"


class TestASetAliasThatCantRun:
    def test_it_is_refused_before_approval_and_nothing_is_fetched(self, store: Any) -> None:
        store.set("tools.web_fetch_alias", "gone", changed_by="test")
        session = _session(store)

        item = _prepare(session)

        assert item["needs_approval"] is False
        assert "execute" not in item
        assert item["header"] == "✗ web_fetch: its model 'gone' is not available"
        assert item["error"] == _NOT_LOADED

    def test_a_store_outage_suggests_trying_the_fetch_again(self, store: Any) -> None:
        store.set("tools.web_fetch_alias", "gone", changed_by="test")
        registry = _registry(
            skipped_aliases={"gone": "secret://vault/llm: store unreachable"},
            retryable_skips={"gone"},
        )
        session = _session(store, registry)

        assert _prepare(session)["error"] == (
            "Error: web_fetch's model 'gone' is not available: the secret store holding its "
            "API key can't be reached. Try the fetch again shortly."
        )

    def test_gone_after_approval_it_never_reads_with_another_model(self, store: Any) -> None:
        session = _session(store)
        assert _prepare(session)["needs_approval"] is True
        store.set("tools.web_fetch_alias", "gone", changed_by="test")

        answer, utility, fetch = _fetch(session)

        fetch.assert_called_once()
        utility.assert_not_called()
        assert answer == _NOT_LOADED

    def test_a_client_that_cant_be_built_is_refused_before_approval(self, store: Any) -> None:
        """The alias is loaded, so only the same bind the read uses can tell."""
        store.set("tools.web_fetch_alias", "reader", changed_by="test")
        session = _session(store)

        with patch("turnstone.core.session.resolve_model_binding", side_effect=_client_failure()):
            item = _prepare(session)

        assert item["needs_approval"] is False
        assert "execute" not in item
        assert item["error"] == _CANT_BUILD

    def test_a_client_that_breaks_after_approval_says_so(self, store: Any) -> None:
        """The read's own bind fails after the fetch, and no other model reads instead."""
        store.set("tools.web_fetch_alias", "reader", changed_by="test")
        session = _session(store)
        assert _prepare(session)["needs_approval"] is True

        with patch("turnstone.core.session.resolve_model_binding", side_effect=_client_failure()):
            answer, utility, _ = _fetch(session)

        utility.assert_not_called()
        assert answer == _CANT_BUILD


class TestTaskAgents:
    """A task agent's fetch goes through the same code; empty reads with the agent's model."""

    @staticmethod
    def _run_agent_fetch(session: Any, client: Any, **agent_kwargs: Any) -> MagicMock:
        """Script *client* to call web_fetch once, run the agent, return the read's mock."""
        client.chat.completions.create = scripted_chat_client(
            {
                "tool_calls": [
                    {
                        "id": "web-1",
                        "name": "web_fetch",
                        "arguments": json.dumps({"url": _URL, "question": _QUESTION}),
                    }
                ],
                "finish_reason": "tool_calls",
            },
            {"content": "done"},
        )
        prepared = {
            "call_id": "web-1",
            "func_name": "web_fetch",
            "needs_approval": False,
            "execute": session._exec_web_fetch,
            "url": _URL,
            "question": _QUESTION,
            "allow_private_origin": False,
        }
        with (
            patch.object(session, "_prepare_tool", return_value=prepared),
            patch("turnstone.core.session.fetch_with_ssrf_guard", return_value=_page()),
            patch.object(
                session,
                "_utility_completion",
                return_value=CompletionResult(content="Extracted answer."),
            ) as utility,
        ):
            session._run_agent(
                [Turn.user("fetch the page")],
                label="task",
                tools=[{"type": "function", "function": {"name": "web_fetch"}}],
                auto_tools={"web_fetch"},
                **agent_kwargs,
            )
        return utility

    def test_empty_reads_with_the_task_model_at_the_task_effort(self, store: Any) -> None:
        store.set("model.task_alias", "task", changed_by="test")
        store.set("model.task_effort", "minimal", changed_by="test")
        session = _session(store, reasoning_effort="high")

        utility = self._run_agent_fetch(session, session._registry.get_client("task"))

        utility.assert_called_once()
        lane = utility.call_args.kwargs["lane"]
        assert (lane.alias, lane.model) == ("task", "task-model")
        assert lane.reasoning_effort == "minimal"

    def test_empty_reads_at_the_task_aliass_own_effort_without_a_task_effort(
        self, store: Any
    ) -> None:
        """The conversation's /reason effort stays on its own lane: it relays only there."""
        store.set("model.task_alias", "task", changed_by="test")
        session = _session(store, reasoning_effort="high")

        utility = self._run_agent_fetch(session, session._registry.get_client("task"))

        lane = utility.call_args.kwargs["lane"]
        assert (lane.alias, lane.reasoning_effort) == ("task", "medium")

    def test_empty_reads_with_the_session_model_an_agent_shares(self, store: Any) -> None:
        store.set("model.task_effort", "minimal", changed_by="test")
        session = _session(store, reasoning_effort="high")

        utility = self._run_agent_fetch(session, session._primary_lane().client)

        lane = utility.call_args.kwargs["lane"]
        assert lane.alias == "main"
        assert lane.reasoning_effort == "minimal"

    def test_a_set_alias_reads_for_task_agents_too(self, store: Any) -> None:
        store.set("model.task_alias", "task", changed_by="test")
        store.set("tools.web_fetch_alias", "reader", changed_by="test")
        session = _session(store)

        utility = self._run_agent_fetch(session, session._registry.get_client("task"))

        assert utility.call_args.kwargs["lane"].alias == "reader"
