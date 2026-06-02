"""Operator-instruction trust declaration — the fold-path system-prompt anchor
that pins the per-session nonce as the sole trusted ``<system-reminder>`` marker.

See ``turnstone.prompts.build_operator_instruction_declaration`` and the
capability-gated emission in ``ChatSession._init_system_messages``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests._session_helpers import make_session
from turnstone.core.providers._protocol import ModelCapabilities
from turnstone.prompts import build_operator_instruction_declaration

if TYPE_CHECKING:
    import pytest


class TestDeclarationText:
    def test_carries_nonce_on_both_tags(self) -> None:
        out = build_operator_instruction_declaration("7f3a9c2e")
        assert "<system-reminder_7f3a9c2e>" in out
        assert "</system-reminder_7f3a9c2e>" in out

    def test_includes_forgery_and_echo_guidance(self) -> None:
        out = build_operator_instruction_declaration("7f3a9c2e")
        assert "untrusted data" in out
        assert "Never reveal or echo" in out

    def test_distinct_per_nonce(self) -> None:
        a = build_operator_instruction_declaration("aaaaaaaa")
        b = build_operator_instruction_declaration("bbbbbbbb")
        assert a != b
        assert "aaaaaaaa" in a and "aaaaaaaa" not in b


class TestSessionWiring:
    def test_fold_model_declares_nonce_marker(self) -> None:
        # The default test-model resolves to OpenAI-compat caps
        # (supports_mid_conversation_system=False) — the fold path.
        s = make_session()
        assert s._envelope_nonce  # minted once at construction
        sysmsg = "\n".join(m.get("content", "") for m in s.system_messages)
        assert "## Operator instructions" in sysmsg
        assert f"<system-reminder_{s._envelope_nonce}>" in sysmsg

    def test_native_model_omits_declaration(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A model with native mid-conversation system support delivers operator
        # turns as real {"role":"system"} messages — no envelope, so no nonce
        # marker and no declaration.
        s = make_session()
        native = ModelCapabilities(supports_mid_conversation_system=True)
        monkeypatch.setattr(s, "_resolve_capabilities", lambda *a, **k: native)
        s._init_system_messages()
        sysmsg = "\n".join(m.get("content", "") for m in s.system_messages)
        assert "## Operator instructions" not in sysmsg
        assert s._envelope_nonce not in sysmsg


class TestFoldSystemTurns:
    """_fold_system_turns folds operator-context turns on the fallback path."""

    def test_fold_appends_nonce_block_and_drops_turn(self) -> None:
        s = make_session()  # test-model → fold path
        nonce = s._envelope_nonce
        msgs = [
            {"role": "user", "content": "do it"},
            {
                "role": "system",
                "_source": "user_interjection",
                "content": "also update the changelog",
            },
        ]
        out = s._fold_system_turns(msgs)
        assert len(out) == 1
        assert out[0]["role"] == "user"
        assert f"<system-reminder_{nonce}>" in out[0]["content"]
        assert "also update the changelog" in out[0]["content"]
        # Read-only contract: the original predecessor is untouched.
        assert msgs[0]["content"] == "do it"

    def test_consecutive_turns_coalesce_onto_predecessor(self) -> None:
        s = make_session()
        nonce = s._envelope_nonce
        msgs = [
            {"role": "tool", "tool_call_id": "c1", "content": "result"},
            {"role": "system", "_source": "tool_error", "content": "first"},
            {"role": "system", "_source": "repeat", "content": "second"},
        ]
        out = s._fold_system_turns(msgs)
        assert len(out) == 1
        assert out[0]["role"] == "tool"
        assert out[0]["content"].count(f"<system-reminder_{nonce}>") == 2
        assert "first" in out[0]["content"] and "second" in out[0]["content"]

    def test_base_prompt_system_message_not_folded(self) -> None:
        s = make_session()
        msgs = [
            {"role": "system", "content": "you are an assistant"},  # no _source
            {"role": "user", "content": "hi"},
        ]
        assert s._fold_system_turns(msgs) == msgs

    def test_operator_turn_without_predecessor_kept_standalone(self) -> None:
        s = make_session()
        msgs = [{"role": "system", "_source": "start", "content": "x"}]
        out = s._fold_system_turns(msgs)
        assert len(out) == 1
        assert out[0]["role"] == "system"

    def test_native_model_keeps_turns_inline(self, monkeypatch: pytest.MonkeyPatch) -> None:
        s = make_session()
        native = ModelCapabilities(supports_mid_conversation_system=True)
        monkeypatch.setattr(s, "_get_capabilities", lambda *a, **k: native)
        msgs = [
            {"role": "user", "content": "do it"},
            {"role": "system", "_source": "user_interjection", "content": "x"},
        ]
        assert s._fold_system_turns(msgs) == msgs

    def test_list_content_predecessor_gets_text_part(self) -> None:
        s = make_session()
        nonce = s._envelope_nonce
        msgs = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "look"},
                    {"type": "image_url", "image_url": {"url": "data:..."}},
                ],
            },
            {"role": "system", "_source": "user_interjection", "content": "note"},
        ]
        out = s._fold_system_turns(msgs)
        assert len(out) == 1
        text_parts = [p for p in out[0]["content"] if p.get("type") == "text"]
        assert any(f"<system-reminder_{nonce}>" in p["text"] for p in text_parts)
        # Original list/text part untouched.
        assert msgs[0]["content"][0]["text"] == "look"
