"""Saved-session listing SQL shared by both storage backends.

``GET /v1/api/workstreams/saved`` lists the persisted workstreams a caller can
resume. Filtering, search, sorting and paging all run in SQL, so a page costs
the same however much history is stored and the reported total is exact.

A workstream is saved when it has conversation history, is neither a
provisional create nor deleted, and holds no live owner lease: no process has
it loaded. A workstream left behind by a crashed process becomes saved once its
lease expires, whatever state it was last stored in.

A page is read in two steps: the first orders the matching rows by the sort key
alone and keeps one page of ids, and the second computes the display columns
(message and child counts, context use, model and skill) for those ids only.

:func:`workstream_visible_predicate` is the SQL form of
:meth:`turnstone.core.auth.WorkstreamProjectVisibility.ws_visibility`, which
stays the authority on project visibility. ``tests/test_storage_saved_workstreams.py``
checks that the two agree on both backends, so change them together.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import sqlalchemy as sa

from turnstone.core.storage._lease import unleased_predicate
from turnstone.core.storage._protocol import SavedWorkstreamPage
from turnstone.core.storage._schema import (
    conversations,
    model_definitions,
    personas,
    project_members,
    projects,
    usage_events,
    workstream_config,
    workstreams,
)
from turnstone.core.storage._utils import LIKE_ESCAPE, escape_like, listed_project_predicate
from turnstone.core.workstream import SAVED_WORKSTREAM_SORT_KEYS, WorkstreamKind

if TYPE_CHECKING:
    from collections.abc import Sequence

#: SQLite function the SQLite backend registers on every connection, because
#: SQLite's own ``lower()`` folds ASCII letters only.
SQLITE_FOLD_FUNCTION = "turnstone_fold"

#: The characters ``str.strip()`` removes (those ``str.isspace()`` accepts), for
#: SQL's trim, which removes only spaces unless told otherwise.
WHITESPACE = "".join(
    map(
        chr,
        (
            *range(0x09, 0x0E),
            *range(0x1C, 0x21),
            0x85,
            0xA0,
            0x1680,
            *range(0x2000, 0x200B),
            0x2028,
            0x2029,
            0x202F,
            0x205F,
            0x3000,
        ),
    )
)


def fold_text(value: Any) -> Any:
    """Python side of :data:`SQLITE_FOLD_FUNCTION`: case-fold text, pass NULL.

    ``casefold`` maps each character on its own, so text typed exactly as stored
    folds to a substring of the folded field (``lower`` gives a capital sigma
    its final form at the end of a word, and so of a search).
    """
    return value.casefold() if isinstance(value, str) else value


def _fold(dialect_name: str, expr: Any) -> sa.ColumnElement[str]:
    """Case-fold text for search and sort, the same way on every operand.

    SQLite folds ASCII-only text with its own ``lower()`` (it agrees with
    ``casefold`` there) and calls the Python function for the rest, since every
    such call takes the interpreter lock from inside the query.
    """
    if dialect_name == "sqlite":
        ascii_only = sa.func.length(sa.cast(expr, sa.LargeBinary)) == sa.func.length(expr)
        return sa.case(
            (ascii_only, sa.func.lower(expr)),
            else_=sa.Function(SQLITE_FOLD_FUNCTION, expr, type_=sa.Text),
        )
    return sa.func.lower(expr, type_=sa.Text)


def _trim(dialect_name: str, expr: Any) -> sa.ColumnElement[str]:
    """Trim the whitespace ``str.strip()`` does, not only spaces."""
    trim = sa.func.trim if dialect_name == "sqlite" else sa.func.btrim
    return trim(expr, WHITESPACE, type_=sa.Text)


def _member(project: sa.FromClause, viewer: str) -> sa.ColumnElement[bool]:
    return sa.exists().where(
        project_members.c.project_id == project.c.project_id,
        project_members.c.user_id == viewer,
    )


def _project_grants(project: sa.FromClause, viewer: str) -> sa.ColumnElement[bool]:
    """Whether *viewer* may see the workstreams attached to *project*.

    Mirrors ``WorkstreamProjectVisibility._project_grants``: a missing or empty
    visibility counts as private, and an anonymous viewer sees only projects
    that are not private.
    """
    public = sa.func.coalesce(sa.func.nullif(project.c.visibility, ""), "private") != "private"
    if not viewer:
        return public
    return sa.or_(public, project.c.owner_id == viewer, _member(project, viewer))


def workstream_visible_predicate(dialect_name: str, viewer: str) -> sa.ColumnElement[bool]:
    """Rows of ``workstreams`` that *viewer* may see, as a SQL predicate.

    Mirrors ``WorkstreamProjectVisibility(viewer).ws_visible`` for a viewer
    without service scope: a workstream with no project link, or a link to a
    project that no longer exists, is visible; so is the viewer's own
    workstream; otherwise the linked project must grant access. An empty
    *viewer* is anonymous and never matches an owner.

    Neither project test refers to the workstream row, so the database works
    each out once per statement instead of once per row. ``NOT IN`` reads as
    "no such project" because project ids are never NULL and neither is the
    trimmed link.
    """
    project_id = _trim(dialect_name, sa.func.coalesce(workstreams.c.project_id, ""))
    linked = projects.alias("visible_project")
    clauses: list[sa.ColumnElement[bool]] = [
        project_id == "",
        project_id.not_in(sa.select(linked.c.project_id)),
        project_id.in_(sa.select(linked.c.project_id).where(_project_grants(linked, viewer))),
    ]
    if viewer:
        clauses.append(workstreams.c.user_id == viewer)
    return sa.or_(*clauses)


def _listed_project(project: sa.FromClause, viewer: str | None) -> sa.ColumnElement[bool]:
    """Whether the viewer's own project list includes *project*.

    The browser labels a project column from that list
    (``list_projects_for_user``), so search and sort read a project's name
    only where the browser could show it, through the same predicate.
    Service scope reads every name.
    """
    if viewer is None:
        return project.c.project_id.is_not(None)
    return listed_project_predicate(project, viewer)


def query_saved_workstreams(
    conn: sa.Connection,
    *,
    dialect_name: str,
    kinds: Sequence[WorkstreamKind | str],
    viewer: str | None,
    project_names: bool,
    search: str,
    sort: str,
    descending: bool,
    limit: int,
    offset: int,
) -> SavedWorkstreamPage:
    """Run one saved-list page and its total on *conn*.

    See :meth:`StorageBackend.list_saved_workstreams` for the contract.
    """
    if sort not in SAVED_WORKSTREAM_SORT_KEYS:
        raise ValueError(f"unknown saved-workstream sort key: {sort!r}")

    model_cfg = workstream_config.alias("model_cfg")
    skill_cfg = workstream_config.alias("skill_cfg")
    model_def = model_definitions.alias("model_def")
    persona = personas.alias("persona_row")
    project = projects.alias("project_row")
    child = workstreams.alias("child")

    def joined(needs: frozenset[str], base: sa.FromClause = workstreams) -> sa.FromClause:
        """*base* with the lookups *needs* names, so each statement joins only
        what it reads."""
        source = base
        if needs & {"model", "model_def"}:
            source = source.outerjoin(
                model_cfg,
                sa.and_(model_cfg.c.ws_id == workstreams.c.ws_id, model_cfg.c.key == "model_alias"),
            )
        if "model_def" in needs:
            source = source.outerjoin(model_def, model_def.c.alias == model_cfg.c.value)
        if "skill" in needs:
            source = source.outerjoin(
                skill_cfg,
                sa.and_(skill_cfg.c.ws_id == workstreams.c.ws_id, skill_cfg.c.key == "skill"),
            )
        if "persona" in needs:
            # The dashboards label a persona from the enabled ones; an archived
            # persona shows its slug.
            source = source.outerjoin(
                persona, sa.and_(persona.c.name == workstreams.c.persona, persona.c.enabled == 1)
            )
        if "project" in needs:
            source = source.outerjoin(project, project.c.project_id == workstreams.c.project_id)
        return source

    message_count = (
        sa.select(sa.func.count())
        .select_from(conversations)
        .where(conversations.c.ws_id == workstreams.c.ws_id)
        .scalar_subquery()
        .label("message_count")
    )
    child_count = (
        sa.select(sa.func.count())
        .select_from(child)
        .where(child.c.parent_ws_id == workstreams.c.ws_id, child.c.state != "creating")
        .scalar_subquery()
        .label("child_count")
    )
    # The prompt size of the latest model call is the context occupancy the
    # session was left with; idx_usage_events_ws_timestamp answers it.
    context_tokens = (
        sa.select(usage_events.c.prompt_tokens)
        .where(usage_events.c.ws_id == workstreams.c.ws_id)
        .order_by(usage_events.c.timestamp.desc())
        .limit(1)
        .scalar_subquery()
    )
    # A project's name counts only for callers who may read project names, and
    # then only where their own project list includes it; folded for search
    # and sort.
    project_name: sa.ColumnElement[str] = (
        sa.case(
            (
                _listed_project(project, viewer),
                _fold(dialect_name, sa.func.coalesce(project.c.name, "")),
            ),
            else_="",
        )
        if project_names
        else sa.literal("")
    )
    project_needs = frozenset({"project"}) if project_names else frozenset()

    conditions: list[sa.ColumnElement[bool]] = [
        sa.exists().where(conversations.c.ws_id == workstreams.c.ws_id),
        workstreams.c.state.not_in(("creating", "deleted")),
        workstreams.c.kind.in_([WorkstreamKind(kind).value for kind in kinds]),
        unleased_predicate(dialect_name),
    ]
    # The lookups the conditions read, for the count as much as the page.
    condition_needs: frozenset[str] = frozenset()
    if viewer is not None:
        conditions.append(workstream_visible_predicate(dialect_name, viewer))
    needle = search.strip()
    if needle:
        # The fields the browser's filter matched, joined the same way. Each
        # field and the search fold through one function, so text typed
        # exactly as stored always matches whatever the database's own case
        # rules are.
        fields = [
            _fold(dialect_name, sa.func.coalesce(workstreams.c.alias, "")),
            _fold(dialect_name, sa.func.coalesce(workstreams.c.title, "")),
            _fold(dialect_name, sa.func.coalesce(workstreams.c.name, "")),
        ]
        if project_names:
            fields.append(project_name)
            condition_needs |= project_needs
        fields.append(_fold(dialect_name, workstreams.c.ws_id))
        haystack = fields[0]
        for field in fields[1:]:
            haystack = haystack + " " + field
        pattern = (
            sa.literal("%") + _fold(dialect_name, sa.literal(escape_like(needle))) + sa.literal("%")
        )
        conditions.append(haystack.like(pattern, escape=LIKE_ESCAPE))

    display_name = sa.func.coalesce(
        sa.func.nullif(workstreams.c.alias, ""),
        sa.func.nullif(workstreams.c.title, ""),
        sa.func.nullif(workstreams.c.name, ""),
        workstreams.c.ws_id,
    )
    persona_label = sa.func.coalesce(
        sa.func.nullif(persona.c.display_name, ""), workstreams.c.persona, ""
    )
    # Context occupancy as of the latest model call: 0 without usage or
    # without a known window (config-only models have no model_definitions
    # row). The one derivation both the CTX sort and the CTX value read.
    context_ratio = sa.func.coalesce(
        context_tokens * 1.0 / sa.func.nullif(model_def.c.context_window, 0), 0
    ).label("context_ratio")
    # Every key is non-null, so both dialects order the same way. Each names
    # the lookups it reads.
    sort_keys: dict[str, tuple[Any, frozenset[str]]] = {
        "updated": (workstreams.c.updated, frozenset()),
        "name": (_fold(dialect_name, display_name), frozenset()),
        "kind": (workstreams.c.kind, frozenset()),
        "persona": (_fold(dialect_name, persona_label), frozenset({"persona"})),
        "project": (project_name, project_needs),
        "model": (
            _fold(dialect_name, sa.func.coalesce(model_cfg.c.value, "")),
            frozenset({"model"}),
        ),
        "message_count": (message_count, frozenset()),
        "child_count": (child_count, frozenset()),
        "context_ratio": (context_ratio, frozenset({"model_def"})),
        "ws_id": (workstreams.c.ws_id, frozenset()),
    }
    sort_key, sort_needs = sort_keys[sort]
    direction = sa.desc if descending else sa.asc

    total = int(
        conn.execute(
            sa.select(sa.func.count()).select_from(joined(condition_needs)).where(*conditions)
        ).scalar_one()
    )
    if not limit or offset >= total:
        return SavedWorkstreamPage(rows=[], total=total)

    keys = sa.select(workstreams.c.ws_id, sort_key.label("sort_key")).select_from(
        joined(condition_needs | sort_needs)
    )
    order_by = [direction(keys.selected_columns.sort_key)]
    if sort != "ws_id":
        # A total order keeps rows from repeating or vanishing between pages.
        order_by.append(direction(workstreams.c.ws_id))
    page_keys = keys.where(*conditions).order_by(*order_by).limit(limit).offset(offset).subquery()
    final_order = [direction(page_keys.c.sort_key)]
    if sort != "ws_id":
        final_order.append(direction(page_keys.c.ws_id))
    page = (
        sa.select(
            workstreams.c.ws_id,
            workstreams.c.alias,
            workstreams.c.title,
            workstreams.c.name,
            workstreams.c.created,
            workstreams.c.updated,
            message_count,
            workstreams.c.node_id,
            workstreams.c.state,
            workstreams.c.kind,
            model_cfg.c.value.label("model_alias"),
            skill_cfg.c.value.label("launch_skill"),
            child_count,
            context_tokens.label("context_tokens"),
            context_ratio,
            workstreams.c.project_id,
            workstreams.c.persona,
        )
        .select_from(
            joined(
                frozenset({"model_def", "skill"}),
                page_keys.join(workstreams, workstreams.c.ws_id == page_keys.c.ws_id),
            )
        )
        .order_by(*final_order)
    )
    rows = [dict(row._mapping) for row in conn.execute(page)]
    return SavedWorkstreamPage(rows=rows, total=total)
