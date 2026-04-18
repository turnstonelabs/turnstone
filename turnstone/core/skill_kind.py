"""Skill classifier shared across storage, API, and handler layers.

StrEnum so members are drop-in ``str`` replacements for the DB column,
JSON payloads, and existing ``==`` comparisons.  Mirrors the shape of
:class:`turnstone.core.workstream.WorkstreamKind`: narrow internal
annotations to this type; wide boundaries (DB row, HTTP body) stay
``str`` and parse via ``SkillKind(raw)`` at the edge.
"""

from __future__ import annotations

import enum


class SkillKind(enum.StrEnum):
    """Which ``list_skills`` surface a skill row is visible on.

    ``INTERACTIVE`` — only the interactive-session activation path sees
    this row.  ``COORDINATOR`` — only the coordinator's ``list_skills``
    tool sees this row.  ``ANY`` — both sides (default for legacy rows
    predating the classifier).
    """

    INTERACTIVE = "interactive"
    COORDINATOR = "coordinator"
    ANY = "any"
