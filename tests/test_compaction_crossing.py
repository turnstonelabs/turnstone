"""Tests for the compaction crossing discipline: what crosses the summary
boundary VERBATIM (not only as summarizer paraphrase) and how the synthetic
summary turns are recognized.

- **Provenance tags** — ``_compact_messages`` and
  ``reconstruct_turns_checkpointed`` mark both synthetic summary turns
  ``source="compaction"``; ``_find_turn_boundaries`` and ``_generate_title``
  test the tag, not the ``[Conversation summary]`` content string.  A user
  who literally types the label therefore stays a REAL turn (previously it
  was silently treated as synthetic — provenance by spelling).
- **Carry budget** — ``_carry_budget_chars`` scales the verbatim-carry
  allowance to ~25% of the window (clamped by the summary output reserve,
  floored at ``_MIN_CARRY_BUDGET_CHARS``), replacing the fixed 400-char
  continuation-hint clip; oversize content keeps head + tail around an
  honest marker.
- **Wind-down spill** — with ``carry_spill=True`` (the end-of-turn site
  passes the ``stopped_to_compact`` latch) the final summarized assistant
  turn's text is copied onto the summary under ``## Wind-down (verbatim)``
  — shell concatenation, so the model's own plan statement survives the
  collapse even when the summarizer paraphrases it.
- The overflow-backstop compact-and-retry passes ``my_generation`` so a
  stale send cannot compact-and-swap a newer generation's history.
"""

from __future__ import annotations

import json
import re
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from tests._session_helpers import make_session
from turnstone.core.compaction import CompactionEngine
from turnstone.core.session import COMPACTION_SOURCE, COMPACTION_SUMMARY_LABEL
from turnstone.core.storage import get_storage
from turnstone.core.trajectory import turns_from_dicts


@pytest.fixture
def session(tmp_db, mock_openai_client):
    """Small-window session: context_window=10_000, compact_max_tokens=100 so
    the summary output reserve is tiny and the carry budget is easy to compute
    (reserve=100, margin=500, spare=9_400, budget=min(2_500, 9_400)=2_500
    tokens → 10_000 chars at the uncalibrated 4.0 chars/token)."""
    s = make_session(
        client=mock_openai_client,
        context_window=10_000,
        compact_max_tokens=100,
        max_tokens=1_000,
        tool_timeout=10,
    )
    _register_session_workstream(s)
    return s


def _register_session_workstream(session):
    """Give direct ChatSession fixtures their production parent row."""
    get_storage().register_workstream(
        session.ws_id,
        user_id=session._user_id,
        kind=session._kind,
        parent_ws_id=session._parent_ws_id,
    )
    return session


def _stub_summary(text: str = "DENSE"):
    return SimpleNamespace(
        content=text,
        finish_reason="stop",
        producer="test-summary-provider",
    )


def _summary_runtime(session):
    return session._build_summary_runtime(session._primary_lane())


def _carry_budget_chars(session, carries: int = 1) -> int:
    return session._compaction_engine.carry_budget_chars(
        _summary_runtime(session),
        carries,
    )


def _summary_output_tokens(session) -> int:
    return session._compaction_engine.summary_output_tokens(_summary_runtime(session))


def test_summary_message_projection_is_honest_and_exact_cap() -> None:
    source = "A" * 2500 + "Z" * 2500

    rendered = CompactionEngine.format_message_for_summary(
        {"role": "tool", "tool_call_id": "call-1", "content": source},
        {"call-1": "bash"},
    )

    assert rendered is not None
    prefix = "TOOL[bash]: "
    assert rendered.startswith(prefix + "A")
    assert rendered.endswith("Z")
    assert len(rendered) <= len(prefix) + 2000
    assert "truncated — 5,000 chars total" in rendered


# ---------------------------------------------------------------------------
# Provenance tags on the synthetic summary turns
# ---------------------------------------------------------------------------


class TestSummaryTurnProvenance:
    def test_compact_tags_both_summary_turns(self, session):
        session.messages = turns_from_dicts(
            [
                {"role": "user", "content": "do the thing"},
                {"role": "assistant", "content": "did the thing"},
            ]
        )
        session._msg_tokens = [1, 1]
        with patch.object(session, "_utility_completion", return_value=_stub_summary()):
            assert session._compact_messages(auto=True) is True

        label, summary = session.messages[0], session.messages[1]
        assert label.text == COMPACTION_SUMMARY_LABEL
        assert label.source == COMPACTION_SOURCE
        assert summary.source == COMPACTION_SOURCE

    def test_boundaries_exclude_tagged_label_only(self, session):
        session.messages = turns_from_dicts(
            [
                {
                    "role": "user",
                    "content": COMPACTION_SUMMARY_LABEL,
                    "_source": COMPACTION_SOURCE,
                },
                {"role": "assistant", "content": "summary"},
                {"role": "user", "content": "real follow-up"},
            ]
        )
        assert session._find_turn_boundaries() == [2]

    def test_literal_label_from_user_is_a_real_boundary(self, session):
        """A user who literally types '[Conversation summary]' is not a
        compaction artifact — provenance rides the tag, not the spelling."""
        session.messages = turns_from_dicts([{"role": "user", "content": COMPACTION_SUMMARY_LABEL}])
        assert session._find_turn_boundaries() == [0]

    def test_title_gen_titles_from_literal_label_user(self, session):
        """The tag distinction reaches _generate_title: a synthetic label is
        skipped (pinned in test_cooperative_compaction), but a REAL user
        message that happens to equal the label is titled from normally."""
        session.messages = turns_from_dicts(
            [
                {"role": "user", "content": COMPACTION_SUMMARY_LABEL},
                {"role": "assistant", "content": "an answer"},
            ]
        )
        with (
            patch.object(
                session, "_utility_completion", return_value=_stub_summary("A Title")
            ) as uc,
            patch.object(session, "ui", new=MagicMock()),
        ):
            session._generate_title()

        uc.assert_called_once()
        prompt = uc.call_args[0][0][-1].text
        assert COMPACTION_SUMMARY_LABEL in prompt  # titled FROM the real message


class TestCheckpointReconstructionProvenance:
    def test_resume_turns_carry_compaction_source(self, storage_backend):
        """A reopened session must see the same provenance the live session
        held: reconstruct_turns_checkpointed tags the synthetic label AND the
        marker-backed summary turn, while real tail rows stay untagged."""
        st = storage_backend
        st.register_workstream("ws1", user_id="u1", title="t", kind="interactive")
        st.save_message("ws1", "user", "old question")
        st.save_message("ws1", "assistant", "old answer")
        watermark = st.get_compaction_watermark("ws1", 0)
        st.save_message(
            "ws1",
            "assistant",
            "THE SUMMARY",
            source=COMPACTION_SOURCE,
            meta=json.dumps({"watermark": watermark}),
        )
        st.save_message("ws1", "user", "new question")

        turns = st.load_message_turns("ws1")
        assert [t.text for t in turns] == [
            COMPACTION_SUMMARY_LABEL,
            "THE SUMMARY",
            "new question",
        ]
        assert turns[0].source == COMPACTION_SOURCE
        assert turns[1].source == COMPACTION_SOURCE
        assert turns[2].source is None


# ---------------------------------------------------------------------------
# Carry budget — the verbatim-crossing allowance
# ---------------------------------------------------------------------------


def _isolate_overhead(s, system_tokens: int = 0) -> None:
    """Pin the fixed prompt overhead (system + tool defs) for exact budget
    arithmetic — the real values vary with the composed prompt and registered
    tools (same isolation pattern as TestRemainingTokenBudget)."""
    s._system_tokens = system_tokens
    s._tools = []


class TestCarryBudget:
    def test_scales_to_quarter_window(self, session):
        # overhead=0, reserve=100 (compact_max_tokens), margin=500,
        # spare=9_400; min(10_000 // 4, 9_400) = 2_500 tokens * 4.0 chars/token.
        _isolate_overhead(session)
        assert _carry_budget_chars(session) == 10_000

    def test_floors_on_tiny_window(self, tmp_db, mock_openai_client):
        tiny = make_session(client=mock_openai_client, context_window=1_000, tool_timeout=10)
        _isolate_overhead(tiny)
        assert _carry_budget_chars(tiny) == tiny._compaction_engine.MIN_CARRY_BUDGET_CHARS

    @pytest.mark.parametrize("carries", [1, 2])
    def test_overhead_reserve_and_carries_fit_window_at_shipped_defaults(
        self, tmp_db, mock_openai_client, carries
    ):
        """The invariant that prevents a carry-induced overflow, pinned at the
        SHIPPED defaults (budget bugs hide behind test-sized configs), for
        BOTH carry counts, and INCLUDING the fixed prompt overhead: the
        post-compaction prompt is system + tools + summary + carries, so a
        budget that ignores the overhead (or sizes carries independently)
        stacks past the window and the backstop re-compacts the carries
        away."""
        s = make_session(client=mock_openai_client, tool_timeout=10)
        _isolate_overhead(s, system_tokens=4_000)  # a chunky composed prompt
        reserve = _summary_output_tokens(s)
        per_carry_tokens = _carry_budget_chars(s, carries) / s._chars_per_token
        margin = int(s.context_window * s._compaction_engine.SUMMARY_SAFETY_MARGIN)
        assert 4_000 + reserve + carries * per_carry_tokens + margin <= s.context_window

    def test_budget_shrinks_with_prompt_overhead(self, tmp_db, mock_openai_client):
        """Monotonicity pin: the overhead term is genuinely in the formula —
        a bigger system prompt leaves less to carry."""
        s = make_session(client=mock_openai_client, tool_timeout=10)
        _isolate_overhead(s, system_tokens=0)
        roomy = _carry_budget_chars(s, 2)
        _isolate_overhead(s, system_tokens=8_000)
        assert _carry_budget_chars(s, 2) < roomy

    def test_double_carry_splits_the_spare(self, tmp_db, mock_openai_client):
        """At shipped defaults the spare (window − overhead − reserve −
        margin) binds two carries: each gets spare // 2, strictly less than
        the solo quarter-window allowance."""
        s = make_session(client=mock_openai_client, tool_timeout=10)
        _isolate_overhead(s, system_tokens=2_000)
        reserve = _summary_output_tokens(s)
        margin = int(s.context_window * s._compaction_engine.SUMMARY_SAFETY_MARGIN)
        spare = s.context_window - reserve - margin - 2_000
        assert _carry_budget_chars(s, 2) == int((spare // 2) * s._chars_per_token)
        assert _carry_budget_chars(s, 2) < _carry_budget_chars(s, 1)


class TestContinuationHintCarry:
    def test_long_ask_crosses_verbatim(self, session):
        """A 3_000-char user message is within the 10_000-char carry budget and
        must cross whole — the old fixed clip kept 400 chars of it."""
        ask = "spec line\n" * 300  # 3_000 chars
        session.messages = turns_from_dicts(
            [
                {"role": "user", "content": ask},
                {"role": "assistant", "content": "working on it"},
            ]
        )
        session._msg_tokens = [1, 1]
        with patch.object(session, "_utility_completion", return_value=_stub_summary()):
            assert session._compact_messages(auto=True) is True

        summary_text = session.messages[1].text or ""
        assert ask.strip() in summary_text  # verbatim, not clipped
        assert "## Continue" in summary_text

    def test_oversize_ask_keeps_head_and_tail_with_marker(self, session):
        head_sentinel = "HEAD-OF-SPEC"
        tail_sentinel = "TAIL-OF-SPEC"
        ask = head_sentinel + ("x" * 20_000) + tail_sentinel  # over the 10_000 budget
        session.messages = turns_from_dicts(
            [
                {"role": "user", "content": ask},
                {"role": "assistant", "content": "working on it"},
            ]
        )
        session._msg_tokens = [1, 1]
        with patch.object(session, "_utility_completion", return_value=_stub_summary()):
            assert session._compact_messages(auto=True) is True

        summary_text = session.messages[1].text or ""
        assert head_sentinel in summary_text
        assert tail_sentinel in summary_text
        # The marker reports the ORIGINAL size, and the summary tells the
        # model the full text is retrievable — a truncated carry is a cache
        # miss with a pointer, not a silent loss.
        assert f"…[truncated — {len(ask):,} chars total]…" in summary_text
        assert "the recall tool can retrieve it" in summary_text
        assert ask not in summary_text  # genuinely truncated

    def test_untruncated_carry_gets_no_recall_pointer(self, session):
        """The retrievability note appears ONLY when something was cut."""
        session.messages = turns_from_dicts(
            [
                {"role": "user", "content": "short ask"},
                {"role": "assistant", "content": "working on it"},
            ]
        )
        session._msg_tokens = [1, 1]
        with patch.object(session, "_utility_completion", return_value=_stub_summary()):
            assert session._compact_messages(auto=True) is True
        assert "recall tool" not in (session.messages[1].text or "")


# ---------------------------------------------------------------------------
# Wind-down spill — the model's plan statement crosses verbatim
# ---------------------------------------------------------------------------


class TestWindDownSpill:
    SPILL = (
        "Goal: finish the migration.\n"
        "Remaining: backfill rows 300-900, rerun the verifier.\n"
        "Next step: resume at scripts/backfill.py --from 300."
    )

    def _compacted_summary(self, session, *, carry_spill: bool) -> str:
        session.messages = turns_from_dicts(
            [
                {"role": "user", "content": "please migrate the database"},
                {"role": "assistant", "content": self.SPILL},
            ]
        )
        session._msg_tokens = [1, 1]
        with patch.object(session, "_utility_completion", return_value=_stub_summary()):
            assert session._compact_messages(auto=True, carry_spill=carry_spill) is True
        return session.messages[1].text or ""

    def test_spill_copied_verbatim_under_heading(self, session):
        summary_text = self._compacted_summary(session, carry_spill=True)
        assert "## Wind-down (verbatim)" in summary_text
        assert self.SPILL in summary_text  # copied, not paraphrased
        # Ordering: recorded plan first, then how to resume.
        assert summary_text.index("## Wind-down (verbatim)") < summary_text.index("## Continue")

    def test_no_spill_without_flag(self, session):
        summary_text = self._compacted_summary(session, carry_spill=False)
        assert "## Wind-down (verbatim)" not in summary_text

    def test_no_spill_when_last_summarized_turn_is_not_assistant(self, session):
        session.messages = turns_from_dicts(
            [
                {"role": "assistant", "content": "answer"},
                {"role": "user", "content": "next task"},
            ]
        )
        session._msg_tokens = [1, 1]
        with patch.object(session, "_utility_completion", return_value=_stub_summary()):
            assert session._compact_messages(auto=True, carry_spill=True) is True
        assert "## Wind-down (verbatim)" not in (session.messages[1].text or "")

    def test_empty_spill_adds_no_heading(self, session):
        session.messages = turns_from_dicts(
            [
                {"role": "user", "content": "task"},
                {"role": "assistant", "content": "   "},
            ]
        )
        session._msg_tokens = [1, 1]
        with patch.object(session, "_utility_completion", return_value=_stub_summary()):
            assert session._compact_messages(auto=True, carry_spill=True) is True
        assert "## Wind-down (verbatim)" not in (session.messages[1].text or "")

    def test_oversize_spill_truncated_by_carry_budget(self, session):
        big_spill = "PLAN-HEAD " + ("y" * 20_000) + " PLAN-TAIL"
        session.messages = turns_from_dicts(
            [
                {"role": "user", "content": "task"},
                {"role": "assistant", "content": big_spill},
            ]
        )
        session._msg_tokens = [1, 1]
        with patch.object(session, "_utility_completion", return_value=_stub_summary()):
            assert session._compact_messages(auto=True, carry_spill=True) is True
        summary_text = session.messages[1].text or ""
        assert "PLAN-HEAD" in summary_text and "PLAN-TAIL" in summary_text
        assert "…[truncated —" in summary_text
        assert "the recall tool can retrieve it" in summary_text

    def test_double_carry_shares_the_budget(self, tmp_db, mock_openai_client):
        """Spill + hint on ONE compaction — the end-of-turn shape — must fit
        the window together.  At the shipped window defaults each carry gets
        spare // 2, so two oversize carries land truncated to the shared
        budget instead of stacking two solo quarter-window allowances on top
        of the half-window summary reserve."""
        s = _register_session_workstream(make_session(client=mock_openai_client, tool_timeout=10))
        per_carry = _carry_budget_chars(s, 2)
        ask = "ASK-HEAD " + "a" * (per_carry * 2) + " ASK-TAIL"
        spill = "PLAN-HEAD " + "b" * (per_carry * 2) + " PLAN-TAIL"
        s.messages = turns_from_dicts(
            [
                {"role": "user", "content": ask},
                {"role": "assistant", "content": spill},
            ]
        )
        s._msg_tokens = [1, 1]
        with patch.object(s, "_utility_completion", return_value=_stub_summary()):
            assert s._compact_messages(auto=True, carry_spill=True) is True

        text = s.messages[1].text or ""
        assert "## Wind-down (verbatim)" in text and "## Continue" in text
        for sentinel in ("ASK-HEAD", "ASK-TAIL", "PLAN-HEAD", "PLAN-TAIL"):
            assert sentinel in text
        assert text.count("…[truncated —") == 2  # both carries hit the shared cap
        framing = 700  # headings, hint wording, stub summary, recall pointer
        assert len(text) <= 2 * per_carry + framing

    def test_do_auto_compact_forwards_carry_spill(self, session):
        """The end-of-turn site passes carry_spill=stopped_to_compact through
        _do_auto_compact — pin the forwarding."""
        generation = session._claim_generation()
        with patch.object(session, "_compact_messages", return_value=True) as cm:
            session._do_auto_compact(my_generation=generation, carry_spill=True)
        assert cm.call_args.kwargs["carry_spill"] is True
        assert cm.call_args.kwargs["my_generation"] == generation


# ---------------------------------------------------------------------------
# Coordinator handles — the id↔meaning pairing crosses DETERMINISTICALLY
# ---------------------------------------------------------------------------


TASKS = [
    {
        "id": "tsk_b4fbdb7d95a7",
        "title": "summarise the incident timeline",
        "status": "in_progress",
        "child_ws_id": "ws_a1b2c3d4",
        "note": "",
    },
    {
        "id": "tsk_0d1e2f3a4b5c",
        "title": "cut p99 latency to <200ms",
        "status": "needs_user",
        "child_ws_id": "",
        "note": "waiting on the change window",
    },
]
CHILDREN = [
    {"ws_id": "ws_a1b2c3d4", "name": "incident-timeline", "state": "running"},
    {"ws_id": "ws_e5f6a7b8", "name": "postmortem-draft", "state": "idle"},
]


def _coord_client(tasks=None, children=None) -> MagicMock:
    """A coord client stubbed at the two in-process storage reads the handles
    block makes — the same calls ``_exec_tasks`` / ``_exec_list_workstreams``
    already make on the worker thread."""
    client = MagicMock()
    client.tasks_get.return_value = {"version": 1, "tasks": TASKS if tasks is None else tasks}
    client.list_children.return_value = {
        "children": CHILDREN if children is None else children,
        "truncated": False,
    }
    return client


def _coord_session(mock_openai_client, *, coord_client=..., **kwargs):
    from turnstone.core.workstream import WorkstreamKind

    return _register_session_workstream(
        make_session(
            client=mock_openai_client,
            context_window=10_000,
            compact_max_tokens=100,
            max_tokens=1_000,
            tool_timeout=10,
            kind=WorkstreamKind.COORDINATOR,
            user_id="u1",
            coord_client=_coord_client() if coord_client is ... else coord_client,
            **kwargs,
        )
    )


def _compact(s, *, carry_spill: bool = False, summary: str = "DENSE") -> str:
    s.messages = turns_from_dicts(
        [
            {"role": "user", "content": "run the incident review"},
            {"role": "assistant", "content": "on it"},
        ]
    )
    s._msg_tokens = [1, 1]
    with patch.object(s, "_utility_completion", return_value=_stub_summary(summary)):
        assert s._compact_messages(auto=True, carry_spill=carry_spill) is True
    return s.messages[1].text or ""


class TestCoordinatorHandles:
    """The pairing an id needs — task id↔title↔status, child id↔name↔state —
    is written by the HARNESS from storage, not transcribed by the summarizer.

    The load-bearing test is ``test_pairing_survives_a_summary_that_has_no
    _ids``: the summarizer returns text containing no id at all (the density
    failure this exists for), and every id still crosses.
    """

    def test_pairing_survives_a_summary_that_has_no_ids(self, tmp_db, mock_openai_client):
        s = _coord_session(mock_openai_client)
        text = _compact(s, summary="## Decisions\nreviewed the incident.")

        assert "## Handles" in text
        # Both halves of every handle, and the ids character-for-character.
        for task in TASKS:
            assert task["id"] in text
            assert task["title"] in text
            assert task["status"] in text
        for child in CHILDREN:
            assert child["ws_id"] in text
            assert child["name"] in text
            assert child["state"] in text
        assert "waiting on the change window" in text  # the note says what is needed
        assert "→ child `ws_a1b2c3d4`" in text  # task↔child linkage kept

    def test_summarizer_is_never_asked_for_handles(self, tmp_db, mock_openai_client):
        """The design decision, pinned: the compactor prompt is kind-INDEPENDENT.

        Deterministic insertion is the whole point — a prompt section asking the
        model to transcribe ids would spend attention to get a fallible copy, and
        could contradict the block storage renders.  If someone adds one, this
        fails and they must revisit that trade rather than stack both.
        """
        coord = _coord_session(mock_openai_client)
        interactive = _register_session_workstream(
            make_session(client=mock_openai_client, context_window=10_000, tool_timeout=10)
        )
        prompts = []
        for s in (coord, interactive):
            s.messages = turns_from_dicts(
                [
                    {"role": "user", "content": "q"},
                    {"role": "assistant", "content": "a"},
                ]
            )
            s._msg_tokens = [1, 1]
            with patch.object(s, "_utility_completion", return_value=_stub_summary()) as uc:
                assert s._compact_messages(auto=True) is True
            prompts.append(uc.call_args[0][0][0].text)

        assert prompts[0] == prompts[1]
        assert "Handles" not in prompts[0]

    def test_interactive_session_never_reads_and_never_renders(self, tmp_db, mock_openai_client):
        """Kind gate: an interactive session has no task envelope and no
        children, so the reads are skipped entirely — not merely rendered
        empty — and its summary is what it was before this existed."""
        client = _coord_client()
        s = _register_session_workstream(
            make_session(
                client=mock_openai_client,
                context_window=10_000,
                compact_max_tokens=100,
                tool_timeout=10,
                coord_client=client,  # present but irrelevant: kind decides
            )
        )
        text = _compact(s)
        assert "## Handles" not in text
        client.tasks_get.assert_not_called()
        client.list_children.assert_not_called()

    def test_coordinator_without_a_client_is_silent(self, tmp_db, mock_openai_client):
        """Eval / rehydration shells run coordinator-kind sessions with no coord
        client; compaction must not care."""
        s = _coord_session(mock_openai_client, coord_client=None)
        assert "## Handles" not in _compact(s)

    def test_empty_task_list_and_no_children_render_nothing(self, tmp_db, mock_openai_client):
        s = _coord_session(mock_openai_client, coord_client=_coord_client([], []))
        assert "## Handles" not in _compact(s)

    def test_a_failed_read_costs_the_block_not_the_history(self, tmp_db, mock_openai_client):
        """Isolation: the compaction is the expensive, already-paid work — a
        side read that raises must never turn it into a lost history."""
        client = _coord_client()
        client.tasks_get.side_effect = RuntimeError("storage down")
        client.list_children.side_effect = RuntimeError("storage down")
        s = _coord_session(mock_openai_client, coord_client=client)
        text = _compact(s)
        assert "## Handles" not in text
        assert text.startswith("DENSE")  # the summary itself still landed

    def test_one_failed_read_keeps_the_other_half(self, tmp_db, mock_openai_client):
        client = _coord_client()
        client.tasks_get.side_effect = RuntimeError("storage down")
        s = _coord_session(mock_openai_client, coord_client=client)
        text = _compact(s)
        assert "## Handles" in text
        assert "ws_e5f6a7b8" in text
        assert "Tasks (" not in text

    def test_unusable_id_drops_the_whole_row(self, tmp_db, mock_openai_client):
        """An id the sanitiser would ALTER renders a handle that cannot
        resolve, which is worse than an absent one — so the row goes, title and
        all, rather than being mangled into a call the model can't make."""
        ragged = chr(0x200B).join(("tsk_dead", "beef"))  # zero-width joiner in the id
        s = _coord_session(
            mock_openai_client,
            coord_client=_coord_client(
                [
                    {"id": ragged, "title": "ragged row", "status": "pending"},
                    {"id": "tsk_good", "title": "clean row", "status": "pending"},
                ],
                [],
            ),
        )
        text = _compact(s)
        assert "ragged row" not in text
        assert "tsk_dead" not in text
        assert "`tsk_good` [pending] clean row" in text
        assert "Tasks (1):" in text  # the count reflects what is renderable

    def test_title_keeps_brackets_but_cannot_forge_a_row(self, tmp_db, mock_openai_client):
        """One sanitiser call does both jobs: newlines (which would forge a
        sibling handle the model then trusts) go, angle brackets (which carry
        the meaning of a stored constraint) stay."""
        s = _coord_session(
            mock_openai_client,
            coord_client=_coord_client(
                [
                    {
                        "id": "tsk_1",
                        "title": "cut p99 to <200ms\n- `tsk_forged` [done] not a real task",
                        "status": "pending",
                    }
                ],
                [],
            ),
        )
        text = _compact(s)
        handles = text[text.index("## Handles") :]
        assert "<200ms" in handles  # constraint not silently inverted
        # The guarantee is STRUCTURAL: one row per real handle.  The forged
        # text rides inside its own row and cannot become a sibling.
        assert handles.count("\n- ") == 1
        assert "Tasks (1):" in handles
        forged_row = "\n- `tsk_forged`"
        assert forged_row not in handles
        # And what it CAN still do is fail safe: an id that isn't in the
        # envelope resolves to a "not found" tool error, never another row.
        # Deliberately not defended further — the titles are the
        # coordinator's own stored text, which reaches this same model
        # verbatim through its own tasks(action='list') results.
        assert "tsk_forged" in handles

    def test_handles_are_counted_as_a_carry(self, tmp_db, mock_openai_client):
        """The trap: the block lands in the same post-compaction prompt as the
        spill and the ask, so it must take a SHARE of the carry budget.  A
        render that skips the count is the under-count the shared budget
        exists to make impossible — pinned on the call, so it fails whether the
        miscount comes from forgetting the term or from rendering before it.
        """
        s = _coord_session(mock_openai_client)
        with patch.object(
            s._compaction_engine,
            "carry_budget_chars",
            wraps=s._compaction_engine.carry_budget_chars,
        ) as budget:
            _compact(s, carry_spill=True)
        assert budget.call_args.args[1] == 3  # handles + spill + ask

        bare = _coord_session(mock_openai_client, coord_client=_coord_client([], []))
        with patch.object(
            bare._compaction_engine,
            "carry_budget_chars",
            wraps=bare._compaction_engine.carry_budget_chars,
        ) as budget:
            _compact(bare, carry_spill=True)
        assert budget.call_args.args[1] == 2  # no handles, no third share

    def test_block_fits_the_budget_and_cuts_only_at_row_boundaries(
        self, tmp_db, mock_openai_client
    ):
        """Overflow drops WHOLE handles and says how many went.  Head+tail
        truncation through a list would leave a half-copied id — a call that
        cannot resolve, dressed as one that can."""
        many = [{"id": f"tsk_{i:04d}", "title": "x" * 180, "status": "pending"} for i in range(60)]
        s = _coord_session(mock_openai_client, coord_client=_coord_client(many, CHILDREN))
        budget = _carry_budget_chars(s, 1)
        block = s._render_handles_block(*s._coordinator_handle_rows(), budget)

        assert len(block) <= budget
        assert "Tasks (60):" in block  # the heading counts ALL of them
        rendered = [t["id"] for t in many if f"`{t['id']}`" in block]
        assert 0 < len(rendered) < 60
        assert f"… and {60 - len(rendered)} more" in block
        assert "tasks(action='list')" in block  # the authoritative source
        # No id was cut in half: every backticked id in the block is a WHOLE id
        # that storage actually returned.
        known = {t["id"] for t in many} | {c["ws_id"] for c in CHILDREN}
        assert set(re.findall(r"^- `([^`]+)`", block, re.M)) <= known
        # Children are reserved room rather than starved off the page.
        assert "ws_e5f6a7b8" in block

    def test_persisted_checkpoint_carries_the_handles(self, tmp_db, mock_openai_client):
        """The reopen path is the whole point: an idle coordinator that gets
        rehydrated reads the checkpoint row, so the handles must be IN it."""
        s = _coord_session(mock_openai_client)
        s._ws_id = "ws-coord"
        with (
            patch.object(get_storage(), "get_compaction_watermark", return_value=7),
            patch("turnstone.core.session.save_message") as saved,
        ):
            _compact(s)
        assert "## Handles" in saved.call_args[0][2]
        assert "tsk_b4fbdb7d95a7" in saved.call_args[0][2]
