"""Metadata-only memory pointer relevance."""

from typing import Any

from turnstone.core.memory_relevance import MemoryConfig, score_memories


class TestScoreMemories:
    def test_empty_inputs(self) -> None:
        assert score_memories([], "query") == []
        memories = [{"name": "alpha", "description": "first hook"}]
        assert score_memories(memories, "   ") == []

    def test_scores_name_and_authored_description(self) -> None:
        memories = [
            {"name": "database_config", "description": "postgres connection settings"},
            {"name": "garden", "description": "tomato watering schedule"},
        ]
        assert score_memories(memories, "postgres database", k=1)[0]["name"] == "database_config"

    def test_body_never_participates_in_pointer_scoring(self) -> None:
        memories = [
            {
                "name": "opaque",
                "description": "unrelated hook",
                "content": "ultraviolet-only-secret",
            }
        ]
        assert score_memories(memories, "ultraviolet-only-secret") == []

    def test_no_match_and_k_limit(self) -> None:
        memories = [
            {"name": f"alpha_{index}", "description": "shared alpha hook"} for index in range(10)
        ]
        assert score_memories(memories, "unmatched") == []
        assert len(score_memories(memories, "alpha", k=2)) == 2


class TestScoreMemoriesReranking:
    _MEMORIES = [
        {"name": "alpha", "description": "shared topic"},
        {"name": "beta", "description": "shared topic"},
        {"name": "gamma", "description": "shared topic"},
    ]

    def test_reranker_reorders_metadata_matches(self) -> None:
        baseline = score_memories(self._MEMORIES, "shared topic", k=3)
        reranked = score_memories(
            self._MEMORIES,
            "shared topic",
            k=3,
            reranker=lambda _query, documents: list(range(len(documents)))[::-1],
        )
        assert [memory["name"] for memory in reranked] == [memory["name"] for memory in baseline][
            ::-1
        ]

    def test_filtering_floor_can_suppress_all_matches(self) -> None:
        assert (
            score_memories(
                self._MEMORIES,
                "shared topic",
                k=3,
                reranker=lambda _query, _documents: [],
                rerank_filters=True,
            )
            == []
        )

    def test_reorder_mode_falls_back_when_reranker_returns_empty(self) -> None:
        baseline = score_memories(self._MEMORIES, "shared topic", k=3)
        assert (
            score_memories(
                self._MEMORIES,
                "shared topic",
                k=3,
                reranker=lambda _query, _documents: [],
                rerank_filters=False,
            )
            == baseline
        )


class TestPointerPlanning:
    @staticmethod
    def _session(**overrides: Any) -> Any:
        from tests._helpers import make_chat_session

        kwargs: dict[str, Any] = {
            "ws_id": "pointer-ws",
            "user_id": "pointer-user",
            "memory_config": MemoryConfig(relevance_k=2),
        }
        kwargs.update(overrides)
        return make_chat_session(**kwargs)

    @staticmethod
    def _save(name: str, description: str, content: str) -> None:
        from turnstone.core.memory import save_structured_memory_strict

        save_structured_memory_strict(
            name,
            content,
            description=description,
            scope="global",
        )

    def test_live_pointer_names_metadata_match_without_body(self, tmp_db) -> None:
        session = self._session()
        self._save("postgres_runbook", "database recovery procedure", "opaque body")
        self._save("hidden_body_match", "garden notes", "database recovery procedure")
        access = session._memory_access("pointer-user")

        pointer = session._plan_memory_pointer("database recovery", access=access)

        assert "postgres_runbook" in pointer
        assert "hidden_body_match" not in pointer

    def test_pointer_planning_does_not_touch_access_metadata(self, tmp_db) -> None:
        from turnstone.core.storage import get_storage

        session = self._session()
        self._save("postgres_runbook", "database recovery procedure", "body")
        session._plan_memory_pointer(
            "database recovery",
            access=session._memory_access("pointer-user"),
        )
        row = get_storage().get_structured_memory_by_name("postgres_runbook", "global", "")
        assert row["access_count"] == 0
        assert row["last_accessed"] == ""

    def test_pointer_respects_nudge_and_tool_visibility_gates(self, tmp_db) -> None:
        session = self._session(memory_config=MemoryConfig(nudges=False))
        self._save("postgres_runbook", "database recovery procedure", "body")
        assert (
            session._plan_memory_pointer(
                "database recovery",
                access=session._memory_access("pointer-user"),
            )
            == ""
        )


def test_memory_config_defaults_to_complete_index_soft_budget() -> None:
    config = MemoryConfig()
    assert config.index_budget_chars == 65_536
    assert config.relevance_k == 5
