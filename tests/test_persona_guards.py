"""Guard suite for personas (#683) — the invariants the feature must hold.

Each test pins one of the locked design decisions: personas shape the
persona's own hands (composition + tool visibility) without ever weakening
the approval path, task-agent identity, compaction mechanics, or the
stamped-at-create isolation from later persona edits.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from turnstone.core.personas import PersonaSnapshot, snapshot_from_persona
from turnstone.core.session import ChatSession
from turnstone.core.storage import get_storage
from turnstone.core.storage._utils import PERSONA_MUTABLE
from turnstone.core.tools import TASK_AGENT_TOOLS
from turnstone.core.workstream import WorkstreamKind


def _snap(
    *,
    name: str = "guard",
    prompt: str = "",
    tools: frozenset[str] | None = None,
    mcp: bool = True,
    memory: bool = True,
) -> PersonaSnapshot:
    return PersonaSnapshot(name=name, prompt=prompt, tools=tools, mcp=mcp, memory=memory)


def _session(mock_openai_client: Any, **kwargs: Any) -> ChatSession:
    defaults: dict[str, Any] = dict(
        client=mock_openai_client,
        model="local-model",
        ui=MagicMock(),
        instructions=None,
        temperature=0.5,
        max_tokens=1000,
        tool_timeout=10,
    )
    defaults.update(kwargs)
    return ChatSession(**defaults)


def _wire_names(session: ChatSession) -> list[str]:
    from turnstone.core.providers._protocol import ModelCapabilities

    caps = ModelCapabilities(supports_web_search=True)
    with patch.object(session, "_get_capabilities", return_value=caps):
        tools = session._get_active_tools() or []
    return [t.get("function", {}).get("name") for t in tools]


# ---------------------------------------------------------------------------
# Guard 1 — rank guard: a gated tool call under ANY persona still hits the
# normal approval path.  Persona visibility shapes what's advertised, never
# what's approved.
# ---------------------------------------------------------------------------


class TestRankGuard:
    def test_approval_path_unchanged_under_persona(self, tmp_db, mock_openai_client) -> None:
        # bash is IN the persona's allowlist, so it survives the visibility
        # filter — but the REAL preparer must still derive needs_approval
        # from the tool (not a patched stub), and the gated call must still
        # route through ui.approve_tools.  Persona visibility shapes what's
        # advertised, never what's approved.
        session = _session(
            mock_openai_client,
            persona_snapshot=_snap(tools=frozenset({"bash"})),
        )
        tc = {
            "id": "c1",
            "function": {"name": "bash", "arguments": json.dumps({"command": "echo hi"})},
        }
        # The real _prepare_bash dispatch derives the approval flag.
        prepared = session._prepare_tool(tc)
        assert prepared["needs_approval"] is True
        session.ui.approve_tools.return_value = (True, None)
        with (
            patch.object(session, "_exec_bash", return_value=("c1", "ok")) as exec_bash,
            patch.object(session, "_evaluate_intent"),
            patch.object(session, "_emit_state"),
            patch.object(session, "_check_cancelled"),
        ):
            session._execute_tools([tc])
        session.ui.approve_tools.assert_called_once()
        # The item the gate actually saw carried the derived flag.
        gated = session.ui.approve_tools.call_args.args[0]
        assert gated[0]["needs_approval"] is True
        exec_bash.assert_called_once()


# ---------------------------------------------------------------------------
# Guard 2 — empty-toolset persona: NO tools block in the composed prompt,
# zero tool definitions on the wire.  (The wire half also lives in
# test_server_live.py::test_empty_toolset_persona_no_tools_on_wire, which
# asserts the provider call itself.)
# ---------------------------------------------------------------------------


class TestEmptyToolset:
    def test_prompt_has_no_tools_block_and_wire_is_empty(self, tmp_db, mock_openai_client) -> None:
        session = _session(mock_openai_client, persona_snapshot=_snap(tools=frozenset()))
        prompt = session.system_messages[0]["content"]
        # tools.md's IC block opener — self-suppressed on an empty envelope.
        assert "read_file" not in prompt
        assert _wire_names(session) == []
        # Even with memories IN SCOPE, the "memories in scope" advisory must
        # not compose — the empty toolset hides the memory tool, and the
        # preamble must never point the model at a tool the wire omits.  The
        # prior `"You have" not in prompt or ...` disjunction was vacuous
        # (no memory was ever in scope, so the branch was unreachable).
        fake = [{"memory_id": "m1", "name": "n", "scope": "user", "scope_id": "u", "content": "c"}]
        with patch.object(session, "_select_memory_candidates", return_value=(fake, "recency")):
            session._init_system_messages()
        assert "memories in scope" not in session.system_messages[0]["content"]

    def test_base_override_replaces_only_base(self, tmp_db, mock_openai_client) -> None:
        session = _session(
            mock_openai_client,
            persona_snapshot=_snap(prompt="You are a scribe on a guard test.", tools=frozenset()),
        )
        prompt = session.system_messages[0]["content"]
        assert "You are a scribe on a guard test." in prompt
        # personas/engineer.md's IC framing is REPLACED...
        assert "read before you edit" not in prompt
        # ...but CONTEXT still composes (the removed /creative fork dropped it).
        assert "Current time:" in prompt or "Session context" in prompt or "User:" in prompt


# ---------------------------------------------------------------------------
# Guard 3 — tool_search escape hatch: included ⇒ soft set (discovered tools
# join the visible wire); omitted ⇒ hard set (the pathway is disabled).
# ---------------------------------------------------------------------------


class TestToolSearchEscape:
    def _mcp_client(self) -> MagicMock:
        mcp = MagicMock()
        mcp.get_tools.return_value = [
            {
                "type": "function",
                "function": {"name": "mcp_widget", "description": "widget", "parameters": {}},
            }
        ]
        mcp.resource_count_for_user.return_value = 0
        mcp.prompt_count_for_user.return_value = 0
        return mcp

    def test_included_keeps_pathway_and_unions_discovered(self, tmp_db, mock_openai_client) -> None:
        session = _session(
            mock_openai_client,
            mcp_client=self._mcp_client(),
            tool_search="on",
            persona_snapshot=_snap(tools=frozenset({"read_file", "tool_search"})),
        )
        assert session._tool_search is not None
        names = _wire_names(session)
        assert set(names) == {"read_file", "tool_search"}
        # Discovery expands the visible set — the allowlist unions with it.
        session._tool_search.expand_visible(["mcp_widget"])
        names = _wire_names(session)
        assert "mcp_widget" in names

    def test_soft_set_survives_global_setting_off(self, tmp_db, mock_openai_client) -> None:
        # The authored escape hatch must not silently degrade to a hard set
        # on deployments where the global setting/threshold wouldn't have
        # constructed a ToolSearchManager.
        session = _session(
            mock_openai_client,
            mcp_client=self._mcp_client(),
            tool_search="off",
            persona_snapshot=_snap(tools=frozenset({"read_file", "tool_search"})),
        )
        assert session._tool_search is not None
        assert "tool_search" in _wire_names(session)

    def test_mid_session_adopt_of_hard_set_drops_tool_search(
        self, tmp_db, mock_openai_client
    ) -> None:
        from turnstone.core.memory import register_workstream, save_workstream_config

        # Unstamped session with a live ToolSearchManager and a discovered tool.
        session = _session(mock_openai_client, mcp_client=self._mcp_client(), tool_search="on")
        assert session._tool_search is not None
        session._tool_search.expand_visible(["mcp_widget"])
        # Adopt a hard-set (scribe-shaped) stamp via non-fork resume.
        register_workstream("t" * 32)
        from turnstone.core.memory import save_message

        save_message("t" * 32, "user", "hi")
        save_workstream_config(
            "t" * 32, _snap(name="scribe", tools=frozenset(), memory=False).to_config()
        )
        assert session.resume("t" * 32)
        # The pathway is re-gated: no manager, no hint target, no escape hatch.
        assert session._tool_search is None
        assert _wire_names(session) == []

    def test_omitted_is_hard(self, tmp_db, mock_openai_client) -> None:
        session = _session(
            mock_openai_client,
            mcp_client=self._mcp_client(),
            tool_search="on",
            persona_snapshot=_snap(tools=frozenset({"read_file"})),
        )
        # The whole pathway is disabled — covers provider-native
        # defer_loading mode, which has no synthetic name to filter.
        assert session._tool_search is None
        assert session._get_deferred_names() is None
        assert set(_wire_names(session)) == {"read_file"}
        # ...and it stays disabled across an MCP catalog refresh.
        session._rebuild_tool_search()
        assert session._tool_search is None

    def test_soft_set_expansion_recomposes_prompt(self, tmp_db, mock_openai_client) -> None:
        # A soft set composed the prompt against the pre-expansion visible
        # names, so tool-gated policy segments for a just-discovered tool
        # were dropped.  Expanding a NEW name via tool_search must recompose
        # so the operator's guidance lands with the tool — but a repeat
        # (already-expanded) discovery must NOT pay the recompose again.
        session = _session(
            mock_openai_client,
            mcp_client=self._mcp_client(),
            tool_search="on",
            persona_snapshot=_snap(tools=frozenset({"read_file", "tool_search"})),
        )
        assert session._tool_search is not None
        widget = next(
            t
            for t in session._tool_search.get_deferred_tools()
            if t["function"]["name"] == "mcp_widget"
        )
        with patch.object(session._tool_search, "search", return_value=[widget]):
            with patch.object(session, "_init_system_messages") as recompose:
                session._exec_tool_search({"query": "widget", "call_id": "c1"})
            recompose.assert_called_once()
            # Second discovery of the same name adds nothing new — no recompose.
            with patch.object(session, "_init_system_messages") as recompose_again:
                session._exec_tool_search({"query": "widget", "call_id": "c2"})
            recompose_again.assert_not_called()

    def test_legacy_expansion_does_not_recompose(self, tmp_db, mock_openai_client) -> None:
        # Without a persona set (_persona_tools is None) the prompt is
        # composed against the full catalog, so a tool_search expansion
        # needn't recompose — the soft-set recompose is persona-specific.
        session = _session(mock_openai_client, mcp_client=self._mcp_client(), tool_search="on")
        assert session._tool_search is not None
        assert session._persona_tools is None
        widget = next(
            t
            for t in session._tool_search.get_deferred_tools()
            if t["function"]["name"] == "mcp_widget"
        )
        with patch.object(session._tool_search, "search", return_value=[widget]):
            with patch.object(session, "_init_system_messages") as recompose:
                session._exec_tool_search({"query": "widget", "call_id": "c1"})
            recompose.assert_not_called()


# ---------------------------------------------------------------------------
# Guard 4 — memory-off: no recall injection, memory tool hidden, memory
# nudges suppressed; compaction mechanics stay untouched.
# ---------------------------------------------------------------------------


class TestMemoryOff:
    def test_memory_levers(self, tmp_db, mock_openai_client) -> None:
        session = _session(mock_openai_client, persona_snapshot=_snap(memory=False))
        with patch.object(session, "_select_memory_candidates") as select:
            session._init_system_messages()
        select.assert_not_called()
        assert "memory" not in _wire_names(session)
        # Memory-directed nudges are suppressed; behavioural nudges stay.
        session._memory_config.nudges = True
        assert not session._nudges_enabled("start")
        assert not session._nudges_enabled("tool_error")
        assert session._nudges_enabled("repeat")
        assert session._nudges_enabled("compaction_pending")

    def test_allowlist_hiding_memory_tool_also_gates_nudges(
        self, tmp_db, mock_openai_client
    ) -> None:
        # memory lever ON but the visibility set omits the memory tool —
        # nudges directing the model at memory(...) must not fire either.
        session = _session(
            mock_openai_client,
            persona_snapshot=_snap(tools=frozenset({"read_file"}), memory=True),
        )
        session._memory_config.nudges = True
        assert not session._nudges_enabled("start")
        assert session._nudges_enabled("repeat")

    def test_recall_pointer_gates_on_visibility(self, tmp_db, mock_openai_client) -> None:
        # scribe-shaped: empty toolset hides recall — the compaction pointer
        # must not direct the model at a tool it can't call.
        hidden = _session(mock_openai_client, persona_snapshot=_snap(tools=frozenset()))
        assert not hidden._persona_tool_visible("recall")
        open_hands = _session(mock_openai_client, persona_snapshot=_snap())
        assert open_hands._persona_tool_visible("recall")

    @staticmethod
    def _drive_advised_compaction(session: ChatSession) -> tuple[Any, Any]:
        """Drive a REAL advised-stop compaction + auto-resume through send().

        Mirrors ``test_cooperative_compaction.py``'s end-to-end recipe: the
        model pauses mid-task (latching ``_compaction_advised``), the turn is
        over-threshold, a real summary is produced via ``_utility_completion``,
        and the loop hands a ``compaction_resume`` user turn back.  Returns the
        ``_utility_completion`` and ``_append_user_turn`` spies so callers can
        assert the spill happened and which resume-nudge variant fired.
        """
        from types import SimpleNamespace

        from turnstone.core.trajectory import turns_from_dicts

        session.messages = turns_from_dicts(
            [
                {"role": "user", "content": "do the task"},
                {"role": "assistant", "content": "on it"},
            ]
        )
        session._msg_tokens = [5, 5]
        session._title_generated = True
        session.compact_max_tokens = 100
        session._system_tokens = 0
        summary = SimpleNamespace(content="## Open tasks\nfinish it", finish_reason="stop")
        n = {"i": 0}

        def stream(*_a: Any, **_k: Any) -> dict[str, str]:
            n["i"] += 1
            if n["i"] == 1:
                session._compaction_advised = True  # advisory fired this turn
                return {"role": "assistant", "content": "pausing to compact"}
            return {"role": "assistant", "content": "all done"}

        def est(*_a: Any, **_k: Any) -> int:
            return 9_999 if n["i"] <= 1 else 10  # over threshold only on the stop turn

        with (
            patch.object(session, "_create_stream_with_retry", return_value=iter([])),
            patch.object(session, "_stream_response", side_effect=stream),
            patch.object(session, "_full_messages", return_value=[]),
            patch.object(session, "_update_token_table"),
            patch.object(session, "_print_status_line"),
            patch.object(session, "_emit_state"),
            patch.object(session, "_estimated_prompt_tokens", side_effect=est),
            patch.object(session, "_utility_completion", return_value=summary) as uc,
            patch.object(session, "_append_user_turn", wraps=session._append_user_turn) as resume,
            patch("turnstone.core.session.save_message"),
        ):
            session.send("go")
        return uc, resume

    def test_real_compaction_recall_pointer_when_recall_visible(
        self, tmp_db, mock_openai_client
    ) -> None:
        # memory-off hides only the memory tool — recall stays visible, so an
        # actual compaction spills a summary AND the resume nudge points at
        # recall (NUDGE_COMPACTION_RESUME).
        from tests._session_helpers import make_session
        from turnstone.core.metacognition import NUDGE_COMPACTION_RESUME

        session = make_session(
            client=mock_openai_client,
            context_window=10_000,
            max_tokens=1_000,
            tool_timeout=10,
            persona_snapshot=_snap(memory=False),
        )
        assert session._persona_tool_visible("recall")
        uc, resume = self._drive_advised_compaction(session)
        assert uc.call_count >= 1  # a real summary was produced (the spill)
        resume_calls = [
            c for c in resume.call_args_list if c.kwargs.get("source") == "compaction_resume"
        ]
        assert len(resume_calls) == 1
        assert resume_calls[0].args[0] == NUDGE_COMPACTION_RESUME

    def test_real_compaction_recall_pointer_when_recall_hidden(
        self, tmp_db, mock_openai_client
    ) -> None:
        # scribe-shaped empty toolset hides recall — the spill still happens
        # but the resume nudge switches to the no-recall variant.
        from tests._session_helpers import make_session
        from turnstone.core.metacognition import NUDGE_COMPACTION_RESUME_NO_RECALL

        session = make_session(
            client=mock_openai_client,
            context_window=10_000,
            max_tokens=1_000,
            tool_timeout=10,
            persona_snapshot=_snap(tools=frozenset()),
        )
        assert not session._persona_tool_visible("recall")
        uc, resume = self._drive_advised_compaction(session)
        assert uc.call_count >= 1
        resume_calls = [
            c for c in resume.call_args_list if c.kwargs.get("source") == "compaction_resume"
        ]
        assert len(resume_calls) == 1
        assert resume_calls[0].args[0] == NUDGE_COMPACTION_RESUME_NO_RECALL


# ---------------------------------------------------------------------------
# Guard 5 — MCP-off is session-wide: no merge into _tools OR _task_tools,
# no listeners, refresh callback inert.
# ---------------------------------------------------------------------------


class TestMcpOff:
    def test_session_wide_gate(self, tmp_db, mock_openai_client) -> None:
        mcp = MagicMock()
        mcp.get_tools.return_value = [
            {"type": "function", "function": {"name": "mcp_widget", "parameters": {}}}
        ]
        session = _session(
            mock_openai_client,
            mcp_client=mcp,
            persona_snapshot=_snap(mcp=False),
        )
        assert session._mcp_client is None
        names = {t["function"]["name"] for t in session._tools if "function" in t}
        assert "mcp_widget" not in names
        task_names = {t["function"]["name"] for t in session._task_tools if "function" in t}
        assert "mcp_widget" not in task_names
        mcp.add_listener.assert_not_called()
        mcp.add_resource_listener.assert_not_called()
        mcp.add_prompt_listener.assert_not_called()
        # A late catalog change can't re-merge.
        session._on_mcp_tools_changed()
        names_after = {t["function"]["name"] for t in session._tools if "function" in t}
        assert "mcp_widget" not in names_after

    def test_memory_off_does_not_touch_task_tools(self, tmp_db, mock_openai_client) -> None:
        # The deliberate asymmetry with guard 5: memory-off shapes the
        # persona's OWN hands; task agents keep their identity/envelope.
        session = _session(mock_openai_client, persona_snapshot=_snap(memory=False))
        assert [t["function"]["name"] for t in session._task_tools] == [
            t["function"]["name"] for t in TASK_AGENT_TOOLS
        ]


# ---------------------------------------------------------------------------
# Guard 6 — spawn: persona honored + validated at prep time; omitted means
# the KIND default (resolved server-side at child creation), never the
# parent's persona.
# ---------------------------------------------------------------------------


class TestSpawnPersona:
    def _coord_session(self, mock_openai_client: Any) -> ChatSession:
        return _session(
            mock_openai_client,
            kind=WorkstreamKind.COORDINATOR,
            user_id="u1",
            coord_client=MagicMock(),
        )

    def test_unknown_persona_is_clean_tool_error(self, tmp_db, mock_openai_client) -> None:
        session = self._coord_session(mock_openai_client)
        item = session._prepare_spawn_workstream("c1", {"persona": "nope"})
        assert item.get("error")
        assert "nope" in item["error"]

    def test_kind_mismatch_is_clean_tool_error(self, tmp_db, mock_openai_client) -> None:
        get_storage().create_persona(
            {
                "persona_id": "px",
                "name": "coord-only",
                "base_prompt": "C",
                "applies_to_kinds": ["coordinator"],
            }
        )
        session = self._coord_session(mock_openai_client)
        item = session._prepare_spawn_workstream("c1", {"persona": "coord-only"})
        assert item.get("error")
        assert "interactive" in item["error"]

    def test_valid_persona_travels_to_spawn_body(self, tmp_db, mock_openai_client) -> None:
        get_storage().create_persona(
            {
                "persona_id": "py",
                "name": "scribe",
                "base_prompt": "S",
                "applies_to_kinds": ["interactive"],
            }
        )
        session = self._coord_session(mock_openai_client)
        item = session._prepare_spawn_workstream("c1", {"persona": "scribe"})
        assert not item.get("error")
        assert item["persona"] == "scribe"
        session._coord_client.spawn.return_value = {"ws_id": "w" * 32, "name": "child"}
        with patch.object(session, "_report_tool_result"):
            session._exec_spawn_workstream(item)
        assert session._coord_client.spawn.call_args.kwargs["persona"] == "scribe"

    def test_omitted_persona_is_not_inherited(self, tmp_db, mock_openai_client) -> None:
        # The parent coordinator has its own persona; the child body must
        # NOT carry it — the receiving node resolves the interactive
        # default at child-creation time.
        session = _session(
            mock_openai_client,
            kind=WorkstreamKind.COORDINATOR,
            user_id="u1",
            coord_client=MagicMock(),
            persona_snapshot=_snap(name="executive"),
        )
        item = session._prepare_spawn_workstream("c1", {"initial_message": "go"})
        assert item["persona"] == ""
        session._coord_client.spawn.return_value = {"ws_id": "w" * 32, "name": "child"}
        with patch.object(session, "_report_tool_result"):
            session._exec_spawn_workstream(item)
        assert session._coord_client.spawn.call_args.kwargs["persona"] == ""


# ---------------------------------------------------------------------------
# Guard 7 — task_agent HAS a persona parameter: a sub-agent's identity comes
# from a persona (default = the built-in task-agent identity), validated at
# prep against the interactive kind.  Revises the original "no persona for
# task agents" stance now that personas are first-class on every path.
# ---------------------------------------------------------------------------


def test_task_agent_schema_has_persona_param() -> None:
    from turnstone.core.tools import TOOLS

    task_agent = next(t for t in TOOLS if t["function"]["name"] == "task_agent")
    props = task_agent["function"]["parameters"]["properties"]
    assert "persona" in props
    # skill= is capability now — the description frames it that way.
    assert "capability" in props["skill"]["description"].lower()


# ---------------------------------------------------------------------------
# Guard 8 — immutability: nothing mutates a workstream's persona post-create.
# The stamp is written from constructor attrs; config rewrites re-emit the
# SAME stamp, and the persona row's slug is immutable.
# ---------------------------------------------------------------------------


class TestImmutability:
    def test_save_config_reemits_same_stamp(self, tmp_db, mock_openai_client) -> None:
        from turnstone.core.memory import load_workstream_config

        snap = _snap(name="scribe", prompt="P", tools=frozenset({"read_file"}), memory=False)
        session = _session(mock_openai_client, ws_id="w1" * 16, persona_snapshot=snap)
        before = {
            k: v for k, v in load_workstream_config("w1" * 16).items() if k.startswith("persona")
        }
        session.temperature = 0.9  # any config-touching change
        session._save_config()
        after = {
            k: v for k, v in load_workstream_config("w1" * 16).items() if k.startswith("persona")
        }
        assert before == after == snap.to_config()

    def test_persona_slug_is_immutable_in_storage(self) -> None:
        assert "name" not in PERSONA_MUTABLE

    def test_legacy_ws_never_gets_backstamped(self, tmp_db, mock_openai_client) -> None:
        from turnstone.core.memory import load_workstream_config

        session = _session(mock_openai_client, ws_id="w2" * 16)  # no persona
        session._save_config()
        keys = load_workstream_config("w2" * 16)
        assert not any(k.startswith("persona") for k in keys)


# ---------------------------------------------------------------------------
# Guard 9 — the stamp survives rehydrate: SessionManager.open threads it
# pre-construction (the same lane as the saved model alias).
# ---------------------------------------------------------------------------
# (resume()-adoption is covered in test_sessions.py::test_resume_restores_config)


class TestRehydrateThreading:
    def test_open_threads_snapshot_into_build_session(self) -> None:
        from tests.test_session_manager import FakeAdapter, _make_manager

        class RecordingAdapter(FakeAdapter):
            def __init__(self) -> None:
                super().__init__()
                self.last_build_kwargs: dict[str, Any] = {}

            def build_session(self, ws: Any, **kwargs: Any) -> Any:
                self.last_build_kwargs = dict(kwargs)
                return super().build_session(ws, **{"model": kwargs.get("model")})

        adapter = RecordingAdapter()
        mgr, _, storage = _make_manager(adapter)
        ws = mgr.create(user_id="u1", persona="scribe")
        ws_id = ws.id
        snap = _snap(name="scribe", tools=frozenset(), mcp=False, memory=False)
        storage.ws_config[ws_id] = snap.to_config()
        mgr.close(ws_id)

        reopened = mgr.open(ws_id)
        assert reopened is not None
        assert reopened.persona == "scribe"
        assert adapter.last_build_kwargs["persona_snapshot"] == snap

    def test_corrupt_stamp_fails_loudly_never_falls_back(self) -> None:
        from tests.test_session_manager import _make_manager

        mgr, _, storage = _make_manager()
        ws = mgr.create(user_id="u1")
        ws_id = ws.id
        # Partial stamp = corruption (missing companions).
        storage.ws_config[ws_id] = {"persona": "scribe"}
        mgr.close(ws_id)
        with pytest.raises(ValueError, match="corrupt persona snapshot"):
            mgr.open(ws_id)

    def test_corrupt_stamp_releases_the_slot(self) -> None:
        # The parse raise must unwind exactly like a build_session failure:
        # the placeholder slot is released (no max_active pin) and a retry
        # raises the SAME loud error instead of "already tracked".
        from tests.test_session_manager import _make_manager

        mgr, _, storage = _make_manager()
        ws = mgr.create(user_id="u1")
        ws_id = ws.id
        storage.ws_config[ws_id] = {"persona": "scribe"}
        mgr.close(ws_id)
        with pytest.raises(ValueError, match="corrupt persona snapshot"):
            mgr.open(ws_id)
        assert mgr.get(ws_id) is None  # slot released, not a stuck placeholder
        with pytest.raises(ValueError, match="corrupt persona snapshot"):
            mgr.open(ws_id)  # retry reproduces the loud error, not RuntimeError

    def test_stamp_survives_compaction_then_resume(self, tmp_db, mock_openai_client) -> None:
        # Compaction rewrites the conversation, never the persona stamp (it
        # lives in workstream_config).  After a real compaction on a stamped
        # workstream, a fresh resume re-adopts all five levers intact.
        from types import SimpleNamespace

        from turnstone.core.memory import (
            register_workstream,
            save_message,
            save_workstream_config,
        )
        from turnstone.core.trajectory import turns_from_dicts

        snap = _snap(
            name="scribe", prompt="P", tools=frozenset({"read_file"}), mcp=False, memory=False
        )
        ws_id = "w" * 32
        register_workstream(ws_id)
        save_message(ws_id, "user", "do the thing")
        save_message(ws_id, "assistant", "did the thing")
        save_workstream_config(ws_id, snap.to_config())

        session = _session(mock_openai_client, ws_id=ws_id, persona_snapshot=snap)
        session.messages = turns_from_dicts(
            [
                {"role": "user", "content": "do the thing"},
                {"role": "assistant", "content": "did the thing"},
            ]
        )
        session._msg_tokens = [5, 5]
        session.compact_max_tokens = 100
        session._system_tokens = 0
        summary = SimpleNamespace(content="## Decisions\ndense", finish_reason="stop")
        with patch.object(session, "_utility_completion", return_value=summary):
            assert session._compact_messages(auto=True) is True

        fresh = _session(mock_openai_client)
        assert fresh.resume(ws_id)
        assert fresh._persona_name == "scribe"
        assert fresh._persona_prompt == "P"
        assert fresh._persona_tools == frozenset({"read_file"})
        assert fresh._persona_mcp is False
        assert fresh._persona_memory is False


# ---------------------------------------------------------------------------
# Guard 9, resume-adoption lane — a mid-session ``resume()`` parses the target
# stamp BEFORE mutating session state: a corrupt stamp leaves this session
# intact; an MCP-on stamp is refused by an MCP-gated-off session; an MCP-off
# stamp narrows the live MCP surface in place.
# ---------------------------------------------------------------------------


class TestResumeAdoption:
    @staticmethod
    def _mcp() -> MagicMock:
        mcp = MagicMock()
        mcp.get_tools.return_value = [
            {"type": "function", "function": {"name": "mcp_widget", "parameters": {}}}
        ]
        mcp.resource_count_for_user.return_value = 0
        mcp.prompt_count_for_user.return_value = 0
        return mcp

    def test_corrupt_target_stamp_leaves_session_intact(self, tmp_db, mock_openai_client) -> None:
        from turnstone.core.memory import (
            load_workstream_config,
            register_workstream,
            save_message,
            save_workstream_config,
        )

        a_id, b_id = "a" * 32, "b" * 32
        register_workstream(a_id)
        save_message(a_id, "user", "hi from A")
        session = _session(mock_openai_client)
        assert session.resume(a_id)  # this session lives on A
        before_msgs = list(session.messages)

        register_workstream(b_id)
        save_message(b_id, "user", "hi from B")
        save_workstream_config(b_id, {"persona": "scribe"})  # partial = corrupt
        b_config_before = load_workstream_config(b_id)

        with pytest.raises(ValueError, match="corrupt persona snapshot"):
            session.resume(b_id)
        # Parse-before-mutate: identity + history untouched, still on A.
        assert session._ws_id == a_id
        assert session.messages == before_msgs
        # ...and a later config save writes A's row, never repairs B's stamp
        # with a persona the operator never chose for B.
        session._save_config()
        assert load_workstream_config(b_id) == b_config_before

    def test_mcp_on_stamp_refused_when_gated_off(self, tmp_db, mock_openai_client) -> None:
        from turnstone.core.memory import (
            register_workstream,
            save_message,
            save_workstream_config,
        )

        # A real client was withheld by the persona gate → _mcp_gated_off.
        session = _session(
            mock_openai_client, mcp_client=self._mcp(), persona_snapshot=_snap(mcp=False)
        )
        assert session._mcp_gated_off is True
        b_id = "b" * 32
        register_workstream(b_id)
        save_message(b_id, "user", "hi")
        save_workstream_config(b_id, _snap(name="scribe", mcp=True).to_config())
        with pytest.raises(ValueError, match="open the workstream fresh"):
            session.resume(b_id)

    def test_mcp_off_stamp_narrows_in_place(self, tmp_db, mock_openai_client) -> None:
        from turnstone.core.memory import (
            register_workstream,
            save_message,
            save_workstream_config,
        )

        mcp = self._mcp()
        session = _session(mock_openai_client, mcp_client=mcp)  # legacy: MCP live
        assert session._mcp_client is mcp
        assert "mcp_widget" in {t["function"]["name"] for t in session._tools if "function" in t}

        b_id = "b" * 32
        register_workstream(b_id)
        save_message(b_id, "user", "hi")
        save_workstream_config(b_id, _snap(name="scribe", mcp=False).to_config())
        assert session.resume(b_id)
        # Surface dropped in place: client gone, no MCP tools on either set.
        assert session._mcp_client is None
        assert "mcp_widget" not in {
            t["function"]["name"] for t in session._tools if "function" in t
        }
        assert "mcp_widget" not in {
            t["function"]["name"] for t in session._task_tools if "function" in t
        }
        # ...and the three listeners were deregistered on the way out.
        mcp.remove_listener.assert_called()
        mcp.remove_resource_listener.assert_called()
        mcp.remove_prompt_listener.assert_called()


# ---------------------------------------------------------------------------
# Guard 10 — mandatory prompt policies compose under EVERY persona, including
# empty-toolset ones; tool-gated policies drop with their tool.
# ---------------------------------------------------------------------------


class TestPolicyComposition:
    def test_db_policy_rides_on_top_of_override(self) -> None:
        from turnstone.prompts import ClientType, SessionContext, compose_system_message

        ctx = SessionContext(current_datetime="2026-07-02T10:00", timezone="UTC", username="guard")
        policies = [
            {"name": "mandatory", "content": "ALWAYS-ON-POLICY", "enabled": True},
            {
                "name": "gated",
                "content": "BASH-GATED-POLICY",
                "tool_gate": "bash",
                "enabled": True,
            },
        ]
        composed = compose_system_message(
            ClientType.CLI,
            ctx,
            frozenset(),  # empty visible set — scribe-shaped
            db_policies=policies,
            base_override="You are a scribe.",
        )
        assert "You are a scribe." in composed
        assert "ALWAYS-ON-POLICY" in composed
        assert "BASH-GATED-POLICY" not in composed  # its tool is hidden


# ---------------------------------------------------------------------------
# Guard 11 — CLI --persona resolution: seed persona loads; unknown name
# errors clearly at startup; --resume adopts the target's stamp.
# ---------------------------------------------------------------------------


class TestCliPersona:
    def test_seed_persona_loads(self, tmp_db) -> None:
        from turnstone.cli import resolve_cli_persona_kwargs

        storage = get_storage()
        storage.create_persona(
            {
                "persona_id": "p1",
                "name": "writer",
                "base_prompt": "W",
                "tool_allowlist": [],
                "mcp_enabled": False,
                "applies_to_kinds": ["interactive"],
            }
        )
        kwargs = resolve_cli_persona_kwargs(storage, "writer", None)
        assert kwargs["persona"] == "writer"
        assert kwargs["persona_snapshot"].tools == frozenset()

    def test_unknown_name_exits(self, tmp_db, capsys) -> None:
        from turnstone.cli import resolve_cli_persona_kwargs

        with pytest.raises(SystemExit):
            resolve_cli_persona_kwargs(get_storage(), "nope", None)
        assert "not found or disabled" in capsys.readouterr().out

    def test_kind_mismatch_exits(self, tmp_db) -> None:
        from turnstone.cli import resolve_cli_persona_kwargs

        storage = get_storage()
        storage.create_persona(
            {
                "persona_id": "p2",
                "name": "exec",
                "base_prompt": "E",
                "applies_to_kinds": ["coordinator"],
            }
        )
        with pytest.raises(SystemExit):
            resolve_cli_persona_kwargs(storage, "exec", None)

    def test_resume_adopts_target_stamp(self, tmp_db) -> None:
        from turnstone.cli import resolve_cli_persona_kwargs
        from turnstone.core.memory import save_workstream_config

        snap = _snap(name="scribe", tools=frozenset(), mcp=False, memory=False)
        save_workstream_config("t" * 32, snap.to_config())
        kwargs = resolve_cli_persona_kwargs(get_storage(), None, "t" * 32)
        assert kwargs["persona_snapshot"] == snap

    def test_no_default_yields_legacy(self, tmp_db) -> None:
        from turnstone.cli import resolve_cli_persona_kwargs

        assert resolve_cli_persona_kwargs(get_storage(), None, None) == {}


# ---------------------------------------------------------------------------
# Guard 13 — template independence: edit AND archive the persona after
# creating a workstream from it — the stamp is untouched.
# ---------------------------------------------------------------------------


class TestTemplateIndependence:
    def test_edit_and_archive_leave_stamp_alone(self, tmp_db, mock_openai_client) -> None:
        from turnstone.core.memory import load_workstream_config
        from turnstone.core.personas import snapshot_from_config

        storage = get_storage()
        storage.create_persona(
            {
                "persona_id": "p1",
                "name": "scribe",
                "base_prompt": "ORIGINAL",
                "tool_allowlist": [],
                "mcp_enabled": False,
                "memory_enabled": False,
                "applies_to_kinds": ["interactive"],
            }
        )
        row = storage.get_persona("p1")
        assert row is not None
        session = _session(
            mock_openai_client, ws_id="w3" * 16, persona_snapshot=snapshot_from_persona(row)
        )
        original = session.system_messages[0]["content"]
        assert "ORIGINAL" in original

        storage.update_persona("p1", base_prompt="EDITED", tool_allowlist=None)
        storage.update_persona("p1", enabled=False)

        stamped = snapshot_from_config(load_workstream_config("w3" * 16))
        assert stamped is not None
        assert stamped.prompt == "ORIGINAL"
        assert stamped.tools == frozenset()
        # A fresh construction from the stamp reproduces the ORIGINAL prompt.
        rehydrated = _session(mock_openai_client, ws_id="w3" * 16, persona_snapshot=stamped)
        assert "ORIGINAL" in rehydrated.system_messages[0]["content"]
        assert "EDITED" not in rehydrated.system_messages[0]["content"]


# ---------------------------------------------------------------------------
# Guard 15 — the collector's delta path carries persona on the rows it
# builds from ws_created events (the saved-list twin lives in
# test_saved_handler_unified.py).  Both ws_created builders are exercised
# behaviourally: the poll-diff additions path (_reconcile_node) and the
# SSE-relay path (_apply_delta).  Precedent: test_console.py::TestCollectorDelta.
# ---------------------------------------------------------------------------


def _persona_collector() -> Any:
    from tests._coord_test_helpers import MockStorage
    from turnstone.console.collector import ClusterCollector

    return ClusterCollector(storage=MockStorage(), discovery_interval=999)


def test_ws_created_reconcile_lane_carries_persona() -> None:
    # Poll-diff additions: a freshly-appeared workstream becomes a ws_created
    # event AND is stored on the node snapshot — persona rides both.
    from turnstone.console.collector import NodeSnapshot

    c = _persona_collector()
    node = NodeSnapshot(node_id="node-a", server_url="http://a:8080")
    c._nodes["node-a"] = node
    pending = c._reconcile_node(
        "node-a",
        node,
        [{"id": "ws1", "name": "n", "state": "idle", "kind": "interactive", "persona": "scribe"}],
    )
    created = [e for e in pending if e["type"] == "ws_created"]
    assert len(created) == 1
    assert created[0]["persona"] == "scribe"
    assert node.workstreams["ws1"]["persona"] == "scribe"


def test_ws_created_apply_delta_lane_carries_persona() -> None:
    # SSE relay: a node's ws_created delta fans out to console listeners AND
    # seeds node.workstreams — persona rides the emitted row and the store.
    import queue

    from turnstone.console.collector import NodeSnapshot

    c = _persona_collector()
    c._nodes["node-a"] = NodeSnapshot(node_id="node-a", server_url="http://a:8080")
    q: queue.Queue[dict[str, Any]] = queue.Queue()
    c.register_listener(q)
    c._apply_delta(
        "node-a",
        {"type": "ws_created", "ws_id": "ws1", "name": "new", "persona": "scribe"},
    )
    event = q.get_nowait()
    assert event["type"] == "ws_created"
    assert event["persona"] == "scribe"
    assert c._nodes["node-a"].workstreams["ws1"]["persona"] == "scribe"


def test_snapshot_roundtrip_via_json() -> None:
    # The stamp's config form is plain strings — JSON-safe end to end.
    snap = _snap(name="s", prompt="p", tools=frozenset({"a"}), mcp=False, memory=False)
    assert json.loads(json.dumps(snap.to_config())) == snap.to_config()


# ---------------------------------------------------------------------------
# Guard 9, fork lane — creating with ``resume_ws`` adopts the SOURCE
# workstream's stamp at construction time (all four levers, including the
# construction-time MCP gate), never the kind default; a corrupt source
# stamp is a loud 400; an unstamped legacy source forks unstamped.
# ---------------------------------------------------------------------------


class TestForkAdoptsStamp:
    @pytest.fixture()
    def _fork_app(self, tmp_db):
        """The production ``make_create_handler`` over a real SessionManager,
        with a session factory that forwards ``persona_snapshot`` (the same
        contract the server factory honors)."""
        import queue
        import threading

        from starlette.applications import Starlette
        from starlette.middleware import Middleware
        from starlette.middleware.base import BaseHTTPMiddleware
        from starlette.routing import Mount, Route
        from starlette.testclient import TestClient

        from turnstone.core.adapters.interactive_adapter import InteractiveAdapter
        from turnstone.core.auth import AuthResult
        from turnstone.core.session_manager import SessionManager
        from turnstone.core.session_routes import SessionEndpointConfig, make_create_handler
        from turnstone.server import (
            WebUI,
            _interactive_create_build_kwargs,
            _interactive_create_post_install,
            _interactive_create_validate_request,
            _interactive_manager_lookup,
            _interactive_tenant_check,
        )

        class _Auth(BaseHTTPMiddleware):
            async def dispatch(self, request: Any, call_next: Any) -> Any:
                request.state.auth_result = AuthResult(
                    user_id="test-user",
                    scopes=frozenset({"approve"}),
                    token_source="config",
                    permissions=frozenset({"read", "write", "approve"}),
                )
                return await call_next(request)

        def _session_factory(ui: Any, model_alias: Any = None, ws_id: Any = None, **kw: Any):
            return ChatSession(
                client=MagicMock(),
                model=model_alias or "test-model",
                ui=ui,
                instructions=None,
                temperature=0.5,
                max_tokens=1000,
                tool_timeout=10,
                ws_id=ws_id,
                persona_snapshot=kw.get("persona_snapshot"),
            )

        gq: queue.Queue[dict[str, Any]] = queue.Queue()
        WebUI._global_queue = gq
        adapter = InteractiveAdapter(
            global_queue=gq,
            ui_factory=lambda ws: WebUI(
                ws_id=ws.id,
                user_id=ws.user_id,
                kind=ws.kind,
                parent_ws_id=ws.parent_ws_id,
            ),
            session_factory=_session_factory,
        )
        mgr = SessionManager(adapter, storage=get_storage(), max_active=10, event_emitter=adapter)
        handler = make_create_handler(
            SessionEndpointConfig(
                permission_gate=None,
                manager_lookup=_interactive_manager_lookup,
                tenant_check=_interactive_tenant_check,
                not_found_label="Workstream not found",
                audit_action_prefix="workstream",
                create_supports_attachments=True,
                create_supports_user_id_override=True,
                create_validate_request=_interactive_create_validate_request,
                create_build_kwargs=_interactive_create_build_kwargs,
                create_post_install=_interactive_create_post_install,
            )
        )
        app = Starlette(
            routes=[
                Mount(
                    "/v1",
                    routes=[Route("/api/workstreams/new", handler, methods=["POST"])],
                )
            ],
            middleware=[Middleware(_Auth)],
        )
        app.state.workstreams = mgr
        app.state.skip_permissions = True
        app.state.global_queue = gq
        app.state.global_listeners = []
        app.state.global_listeners_lock = threading.Lock()

        try:
            yield TestClient(app, raise_server_exceptions=False), mgr
        finally:
            # Close the sessions this manager created, then release the
            # process-global WebUI queue so it can't leak into the next test
            # (precedent: test_coord_rich_ws_state_payload.py teardown).
            for ws in mgr.list_all():
                mgr.close(ws.id)
            WebUI._global_queue = None

    def _seed_default(self) -> None:
        get_storage().create_persona(
            {
                "persona_id": "pd",
                "name": "engineer",
                "base_prompt": "D",
                "applies_to_kinds": ["interactive"],
                "is_default": True,
            }
        )

    def test_fork_adopts_source_stamp_not_default(self, _fork_app) -> None:
        from turnstone.core.memory import register_workstream, save_workstream_config

        client, mgr = _fork_app
        self._seed_default()  # present, and must LOSE to the source stamp
        src = "s" * 32
        register_workstream(src)
        snap = _snap(name="scribe", tools=frozenset(), mcp=False, memory=False)
        save_workstream_config(src, snap.to_config())

        resp = client.post("/v1/api/workstreams/new", json={"resume_ws": src})
        assert resp.status_code == 200, resp.text
        ws = mgr.get(resp.json()["ws_id"])
        assert ws is not None and ws.session is not None
        assert ws.persona == "scribe"
        assert ws.session._persona_name == "scribe"
        assert ws.session._persona_tools == frozenset()
        assert ws.session._persona_mcp is False
        assert ws.session._persona_memory is False

    def test_corrupt_source_stamp_is_400(self, _fork_app) -> None:
        from turnstone.core.memory import register_workstream, save_workstream_config

        client, mgr = _fork_app
        src = "c" * 32
        register_workstream(src)
        save_workstream_config(src, {"persona": "scribe"})  # partial = corrupt

        resp = client.post("/v1/api/workstreams/new", json={"resume_ws": src})
        assert resp.status_code == 400
        assert "cannot fork" in resp.json()["error"]

    def test_unstamped_legacy_source_forks_unstamped(self, _fork_app) -> None:
        from turnstone.core.memory import register_workstream

        client, mgr = _fork_app
        self._seed_default()  # the default must NOT leak onto the fork
        src = "l" * 32
        register_workstream(src)

        resp = client.post("/v1/api/workstreams/new", json={"resume_ws": src})
        assert resp.status_code == 200, resp.text
        ws = mgr.get(resp.json()["ws_id"])
        assert ws is not None and ws.session is not None
        assert ws.session._persona_name == ""  # unstamped, not the default
        assert not ws.persona


# ---------------------------------------------------------------------------
# Guard 6 (receiving side) — a fresh create resolves the body ``persona`` at
# creation time and stamps it: no separate persona permission is required
# (workstreams.create alone suffices); a kind-mismatch / unknown persona is a
# loud 4xx; an omitted persona takes the kind default; a FAILED default lookup
# fails closed (503) rather than silently widening to the stock envelope.
# ---------------------------------------------------------------------------


class TestCreateStampsPersona:
    @pytest.fixture()
    def _create_app(self, tmp_db):
        """The production ``make_create_handler`` behind the production
        permission gate — the caller carries ONLY ``workstreams.create`` (no
        service scope, no persona-specific permission), so a successful stamp
        proves persona needs no dedicated permission."""
        import queue
        import threading

        from starlette.applications import Starlette
        from starlette.middleware import Middleware
        from starlette.middleware.base import BaseHTTPMiddleware
        from starlette.routing import Mount, Route
        from starlette.testclient import TestClient

        from turnstone.core.adapters.interactive_adapter import InteractiveAdapter
        from turnstone.core.auth import AuthResult
        from turnstone.core.session_manager import SessionManager
        from turnstone.core.session_routes import SessionEndpointConfig, make_create_handler
        from turnstone.server import (
            WebUI,
            _interactive_create_build_kwargs,
            _interactive_create_post_install,
            _interactive_create_validate_request,
            _interactive_manager_lookup,
            _interactive_tenant_check,
        )

        class _Auth(BaseHTTPMiddleware):
            async def dispatch(self, request: Any, call_next: Any) -> Any:
                request.state.auth_result = AuthResult(
                    user_id="test-user",
                    scopes=frozenset(),  # no service scope — no bypass
                    token_source="config",
                    permissions=frozenset({"workstreams.create"}),
                )
                return await call_next(request)

        def _session_factory(ui: Any, model_alias: Any = None, ws_id: Any = None, **kw: Any):
            return ChatSession(
                client=MagicMock(),
                model=model_alias or "test-model",
                ui=ui,
                instructions=None,
                temperature=0.5,
                max_tokens=1000,
                tool_timeout=10,
                ws_id=ws_id,
                persona_snapshot=kw.get("persona_snapshot"),
            )

        gq: queue.Queue[dict[str, Any]] = queue.Queue()
        WebUI._global_queue = gq
        adapter = InteractiveAdapter(
            global_queue=gq,
            ui_factory=lambda ws: WebUI(
                ws_id=ws.id,
                user_id=ws.user_id,
                kind=ws.kind,
                parent_ws_id=ws.parent_ws_id,
            ),
            session_factory=_session_factory,
        )
        mgr = SessionManager(adapter, storage=get_storage(), max_active=10, event_emitter=adapter)
        handler = make_create_handler(
            SessionEndpointConfig(
                permission_gate=None,
                manager_lookup=_interactive_manager_lookup,
                tenant_check=_interactive_tenant_check,
                not_found_label="Workstream not found",
                audit_action_prefix="workstream",
                create_supports_attachments=True,
                create_supports_user_id_override=True,
                create_validate_request=_interactive_create_validate_request,
                create_build_kwargs=_interactive_create_build_kwargs,
                create_post_install=_interactive_create_post_install,
            ),
            accepted_permissions=("workstreams.create", "admin.coordinator"),
        )
        app = Starlette(
            routes=[
                Mount(
                    "/v1",
                    routes=[Route("/api/workstreams/new", handler, methods=["POST"])],
                )
            ],
            middleware=[Middleware(_Auth)],
        )
        app.state.workstreams = mgr
        app.state.skip_permissions = True
        app.state.global_queue = gq
        app.state.global_listeners = []
        app.state.global_listeners_lock = threading.Lock()

        try:
            yield TestClient(app, raise_server_exceptions=False), mgr
        finally:
            for ws in mgr.list_all():
                mgr.close(ws.id)
            WebUI._global_queue = None

    @staticmethod
    def _seed(name: str, kinds: list[str], **extra: Any) -> None:
        get_storage().create_persona(
            {
                "persona_id": "p_" + name,
                "name": name,
                "base_prompt": f"You are {name}.",
                "applies_to_kinds": kinds,
                **extra,
            }
        )

    def test_create_stamps_persona_with_create_permission_only(self, _create_app) -> None:
        client, mgr = _create_app
        self._seed("scribe", ["interactive"], base_prompt="S", tool_allowlist=[], mcp_enabled=False)
        resp = client.post("/v1/api/workstreams/new", json={"persona": "scribe"})
        assert resp.status_code == 200, resp.text
        ws = mgr.get(resp.json()["ws_id"])
        assert ws is not None and ws.session is not None
        assert ws.persona == "scribe"
        assert ws.session._persona_name == "scribe"

    def test_create_kind_mismatch_is_400(self, _create_app) -> None:
        client, mgr = _create_app
        self._seed("orchestrator", ["coordinator"])  # coordinator-only
        resp = client.post("/v1/api/workstreams/new", json={"persona": "orchestrator"})
        assert resp.status_code == 400
        assert "does not apply to kind" in resp.json()["error"]

    def test_create_unknown_persona_is_400(self, _create_app) -> None:
        client, mgr = _create_app
        resp = client.post("/v1/api/workstreams/new", json={"persona": "nonexistent"})
        assert resp.status_code == 400
        assert "not found or disabled" in resp.json()["error"]

    def test_create_omitted_persona_stamps_default(self, _create_app) -> None:
        client, mgr = _create_app
        self._seed("engineer", ["interactive"], is_default=True)
        resp = client.post("/v1/api/workstreams/new", json={"name": "x"})
        assert resp.status_code == 200, resp.text
        ws = mgr.get(resp.json()["ws_id"])
        assert ws is not None and ws.session is not None
        assert ws.session._persona_name == "engineer"

    def test_create_default_lookup_failure_is_503(self, _create_app) -> None:
        # Fail-closed: a storage blip during default resolution must not
        # degrade to the stock (unstamped) envelope — the operator may have
        # promoted a restricted persona to default, and silently widening it
        # is the failure mode this lane guards against.
        client, mgr = _create_app
        storage = get_storage()
        with patch.object(
            type(storage), "get_default_persona", side_effect=RuntimeError("db down")
        ):
            resp = client.post("/v1/api/workstreams/new", json={"name": "x"})
        assert resp.status_code == 503
        assert "persona resolution unavailable" in resp.json()["error"]

    def test_create_clean_none_default_is_unstamped_legacy(self, _create_app) -> None:
        # No persona in the body and no default configured — a clean ``None``
        # (not a lookup failure) still creates, unstamped, byte-identical to a
        # legacy pre-persona workstream.
        client, mgr = _create_app
        resp = client.post("/v1/api/workstreams/new", json={"name": "x"})
        assert resp.status_code == 200, resp.text
        ws = mgr.get(resp.json()["ws_id"])
        assert ws is not None and ws.session is not None
        assert ws.session._persona_name == ""
        assert not ws.persona
