"""Skill search — BM25-based discovery for activation="search" skills.

Mirrors the tool_search.py progressive disclosure pattern: skills with
activation="search" are not loaded by default but discoverable via the
``/skill search <query>`` slash command.
"""

from __future__ import annotations

import json
from typing import Any

from turnstone.core.bm25 import BM25Index


class SkillSearchManager:
    """Session-scoped skill discovery via BM25 search.

    Indexes skills that have ``activation="search"`` and makes them
    discoverable via keyword search over name, description, tags, and
    a content prefix.
    """

    def __init__(self, skills: list[dict[str, Any]]) -> None:
        self._skills = skills
        self._index: BM25Index | None = None
        if skills:
            texts = [self._skill_text(s) for s in skills]
            self._index = BM25Index(texts)

    @staticmethod
    def _skill_text(skill: dict[str, Any]) -> str:
        """Build searchable text from skill fields."""
        parts = [skill.get("name", ""), skill.get("description", "")]
        parts.append(skill.get("category", ""))
        tags_raw = skill.get("tags", "[]")
        if isinstance(tags_raw, str):
            try:
                tags = json.loads(tags_raw)
            except (json.JSONDecodeError, TypeError):
                tags = []
        else:
            tags = tags_raw
        parts.extend(tags)
        # Include first 500 chars of content for semantic matching
        parts.append(skill.get("content", "")[:500])
        return " ".join(str(p) for p in parts)

    def search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        """Search for skills matching *query*. Returns skill dicts."""
        if not self._index:
            return []
        indices = self._index.search(query, k=limit)
        return [self._skills[i] for i in indices]

    @property
    def count(self) -> int:
        """Number of indexed skills."""
        return len(self._skills)
