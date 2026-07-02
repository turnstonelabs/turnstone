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
        session = _session(
            mock_openai_client,
            persona_snapshot=_snap(tools=frozenset({"bash"})),
        )
        item = {
            "call_id": "c1",
            "func_name": "bash",
            "execute": lambda _item: ("c1", "ok"),
            "needs_approval": True,
            "header": "test",
            "preview": "",
        }
        with (
            patch.object(session, "_prepare_tool", return_value=item),
            patch.object(session, "_evaluate_intent"),
            patch.object(session, "_emit_state"),
            patch.object(session, "_init_system_messages"),
            patch.object(session, "_check_cancelled"),
        ):
            session.ui.approve_tools.return_value = (True, None)
            session._execute_tools([{"id": "c1", "function": {"name": "bash", "arguments": "{}"}}])
        session.ui.approve_tools.assert_called_once()


# ---------------------------------------------------------------------------
# Guard 2 — empty-toolset persona: NO tools block in the composed prompt,
# zero tool definitions on the wire.  (The wire half also lives in
# test_server_live.py::test_empty_toolset_persona_no_tools_on_wire, which
# asserts the provider call itself.)
# ---------------------------------------------------------------------------


class TestEmptyToolset:
    def test_prompt_has_no_tools_block_and_wire_is_empty(
        self, tmp_db, mock_openai_client
    ) -> None:
        session = _session(mock_openai_client, persona_snapshot=_snap(tools=frozenset()))
        prompt = session.system_messages[0]["content"]
        # tools.md's IC block opener — self-suppressed on an empty envelope.
        assert "read_file" not in prompt
        assert "You have" not in prompt or "memories in scope" not in prompt
        assert _wire_names(session) == []

    def test_base_override_replaces_only_base(self, tmp_db, mock_openai_client) -> None:
        session = _session(
            mock_openai_client,
            persona_snapshot=_snap(prompt="You are a scribe on a guard test.", tools=frozenset()),
        )
        prompt = session.system_messages[0]["content"]
        assert "You are a scribe on a guard test." in prompt
        # base.md's IC framing is REPLACED...
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

    def test_included_keeps_pathway_and_unions_discovered(
        self, tmp_db, mock_openai_client
    ) -> None:
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
                "applies_to_kinds": ["coordinator"],
            }
        )
        session = self._coord_session(mock_openai_client)
        item = session._prepare_spawn_workstream("c1", {"persona": "coord-only"})
        assert item.get("error")
        assert "interactive" in item["error"]

    def test_valid_persona_travels_to_spawn_body(self, tmp_db, mock_openai_client) -> None:
        get_storage().create_persona(
            {"persona_id": "py", "name": "scribe", "applies_to_kinds": ["interactive"]}
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
# Guard 7 — task_agent has no persona parameter (sub-agents keep their own
# identity; persona is a workstream-level concept).
# ---------------------------------------------------------------------------


def test_task_agent_schema_has_no_persona_param() -> None:
    from turnstone.core.tools import TOOLS

    task_agent = next(t for t in TOOLS if t["function"]["name"] == "task_agent")
    assert "persona" not in task_agent["function"]["parameters"]["properties"]


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


# ---------------------------------------------------------------------------
# Guard 10 — mandatory prompt policies compose under EVERY persona, including
# empty-toolset ones; tool-gated policies drop with their tool.
# ---------------------------------------------------------------------------


class TestPolicyComposition:
    def test_db_policy_rides_on_top_of_override(self) -> None:
        from turnstone.prompts import ClientType, SessionContext, compose_system_message

        ctx = SessionContext(
            current_datetime="2026-07-02T10:00", timezone="UTC", username="guard"
        )
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
            {"persona_id": "p2", "name": "exec", "applies_to_kinds": ["coordinator"]}
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
# test_saved_handler_unified.py).
# ---------------------------------------------------------------------------


def test_ws_created_event_shape_includes_persona() -> None:
    import inspect

    from turnstone.console import collector

    src = inspect.getsource(collector)
    # Both the snapshot-diff and SSE-relay ws_created builders must carry it.
    assert src.count('"persona"') >= 3


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
        mgr = SessionManager(
            adapter, storage=get_storage(), max_active=10, event_emitter=adapter
        )
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

        yield TestClient(app, raise_server_exceptions=False), mgr

    def _seed_default(self) -> None:
        get_storage().create_persona(
            {
                "persona_id": "pd",
                "name": "engineer",
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
