"""``StorageBackend.list_saved_workstreams`` on both dialects.

The saved-session list pages, searches and sorts in SQL (#1268), so these tests
run the real query: which rows count as saved (history, not provisional, no
live owner lease), visibility parity with
:class:`turnstone.core.auth.WorkstreamProjectVisibility`, exact totals across
pages, every sort key, and search.
"""

from __future__ import annotations

from typing import Any

import pytest
import sqlalchemy as sa

from tests._storage_fakes import stamp_updated_newest_first
from turnstone.core.auth import WorkstreamProjectVisibility
from turnstone.core.storage._saved import (
    SQLITE_FOLD_FUNCTION,
    WHITESPACE,
    fold_text,
    query_saved_workstreams,
)
from turnstone.core.workstream import SAVED_WORKSTREAM_SORT_KEYS, WorkstreamKind

INTERACTIVE = WorkstreamKind.INTERACTIVE
COORDINATOR = WorkstreamKind.COORDINATOR
BOTH = [COORDINATOR, INTERACTIVE]


def _seed(backend: Any, ws_id: str, *, messages: int = 1, **fields: Any) -> None:
    fields.setdefault("fork_reservation_token", f"token-{ws_id}")
    assert backend.register_workstream(ws_id, **fields) is True
    for i in range(messages):
        backend.save_message(ws_id, "user", f"message {i}")


def _ids(backend: Any, **kwargs: Any) -> list[str]:
    kwargs.setdefault("kinds", BOTH)
    kwargs.setdefault("viewer", None)
    kwargs.setdefault("project_names", True)
    kwargs.setdefault("limit", 200)
    return [row["ws_id"] for row in backend.list_saved_workstreams(**kwargs).rows]


def _lease(backend: Any, ws_id: str, *, ttl_seconds: float, holder: str = "boot-1") -> Any:
    grant = backend.acquire_workstream_lease(
        ws_id,
        incarnation_token=f"token-{ws_id}",
        holder=holder,
        node_id="node-a",
        ttl_seconds=ttl_seconds,
    )
    assert grant is not None
    return grant.fence


# ---------------------------------------------------------------------------
# What counts as saved
# ---------------------------------------------------------------------------


def test_saved_requires_history_and_a_published_row(backend: Any) -> None:
    _seed(backend, "with-history")
    _seed(backend, "no-history", messages=0)
    _seed(backend, "provisional", state="creating")
    assert _ids(backend) == ["with-history"]


def test_saved_excludes_rows_a_live_lease_holds(backend: Any) -> None:
    """Loaded anywhere means a live owner lease; released or expired leases
    leave the row resumable whatever state it was stored in."""
    for ws_id in ("loaded", "released", "expired", "never-leased"):
        _seed(backend, ws_id, state="idle")
    _lease(backend, "loaded", ttl_seconds=60)
    backend.release_workstream_lease(_lease(backend, "released", ttl_seconds=60))
    _lease(backend, "expired", ttl_seconds=0)

    assert set(_ids(backend)) == {"released", "expired", "never-leased"}
    assert backend.list_saved_workstreams(kinds=BOTH, viewer=None).total == 3


def test_orphan_close_keeps_a_session_in_its_place(backend: Any) -> None:
    """Cleanup closing an abandoned session keeps its last-use time, so the
    session stays where it was in the newest-first list."""
    _seed(backend, "recent", state="closed")
    _seed(backend, "abandoned", state="idle")
    stamp_updated_newest_first(backend, ["recent", "abandoned"])
    before = backend.list_saved_workstreams(kinds=BOTH, viewer=None).rows
    assert [row["ws_id"] for row in before] == ["recent", "abandoned"]

    closed = backend.bulk_close_stale_orphans(
        INTERACTIVE, cutoff="2030-01-01T00:00:00", exclude_ws_ids=[]
    )

    assert closed == ["abandoned"]
    after = backend.list_saved_workstreams(kinds=BOTH, viewer=None).rows
    assert [(row["ws_id"], row["updated"]) for row in after] == [
        (row["ws_id"], row["updated"]) for row in before
    ]
    assert after[1]["state"] == "closed"


def test_kinds_select_rows(backend: Any) -> None:
    _seed(backend, "coord", kind="coordinator", state="closed")
    _seed(backend, "inter", kind="interactive", state="error")
    assert set(_ids(backend)) == {"coord", "inter"}
    assert _ids(backend, kinds=[COORDINATOR]) == ["coord"]
    assert _ids(backend, kinds=["interactive"]) == ["inter"]
    page = backend.list_saved_workstreams(kinds=[], viewer=None)
    assert (page.rows, page.total) == ([], 0)


def test_row_carries_enrichment_columns(backend: Any) -> None:
    """model_alias + launch_skill (workstream_config), child_count
    (parent_ws_id), context_tokens (latest usage event of this workstream) and
    context_ratio (those tokens over the model_definitions window)."""
    _seed(backend, "parent", node_id="n1", state="error", kind="coordinator", user_id="alice")
    backend.save_workstream_config("parent", {"model_alias": "m1", "skill": "news"})
    # A child counts without history of its own; a provisional one does not.
    _seed(backend, "child", messages=0, parent_ws_id="parent")
    _seed(backend, "hidden-child", messages=0, parent_ws_id="parent", state="creating")
    backend.record_usage_event("e-old", "alice", "parent", "n1", "m1", 100, 1, 0)
    backend.record_usage_event("e-parent", "alice", "parent", "n1", "m1", 250, 1, 0)
    with backend._engine.begin() as conn:
        conn.execute(
            sa.text(
                "UPDATE usage_events SET timestamp = '2020-01-01T00:00:00' WHERE event_id = 'e-old'"
            )
        )
    # A usage event on another workstream must not bleed into the parent's.
    backend.record_usage_event("e-other", "alice", "child", "n1", "m1", 999, 1, 0)
    backend.create_model_definition("d1", alias="m1", model="m1-model", context_window=1000)

    row = backend.list_saved_workstreams(kinds=[COORDINATOR], viewer=None).rows[0]
    assert row["ws_id"] == "parent"
    assert row["node_id"] == "n1"
    assert row["state"] == "error"
    assert row["kind"] == "coordinator"
    assert row["model_alias"] == "m1"
    assert row["launch_skill"] == "news"
    assert row["message_count"] == 1
    assert row["child_count"] == 1
    assert row["context_tokens"] == 250
    assert float(row["context_ratio"]) == 0.25
    # The protocol's row keys, exactly.
    assert set(row) == {
        "ws_id",
        "alias",
        "title",
        "name",
        "created",
        "updated",
        "message_count",
        "node_id",
        "state",
        "kind",
        "model_alias",
        "launch_skill",
        "child_count",
        "context_tokens",
        "context_ratio",
        "project_id",
        "persona",
    }


def test_row_enrichment_is_null_when_absent(backend: Any) -> None:
    _seed(backend, "bare")
    row = backend.list_saved_workstreams(kinds=BOTH, viewer=None).rows[0]
    assert row["model_alias"] is None
    assert row["launch_skill"] is None
    assert row["child_count"] == 0
    assert row["context_tokens"] is None
    assert float(row["context_ratio"]) == 0


def test_child_count_excludes_creating_until_publication(backend: Any) -> None:
    _seed(backend, "parent", kind="coordinator", user_id="alice")
    _seed(
        backend,
        "child",
        messages=0,
        parent_ws_id="parent",
        state="creating",
        fork_reservation_token="child-incarnation",
    )
    assert (
        backend.list_saved_workstreams(kinds=[COORDINATOR], viewer=None).rows[0]["child_count"] == 0
    )
    assert backend.publish_deferred_create("child", "child-incarnation") is True
    assert (
        backend.list_saved_workstreams(kinds=[COORDINATOR], viewer=None).rows[0]["child_count"] == 1
    )


# ---------------------------------------------------------------------------
# Paging
# ---------------------------------------------------------------------------


def test_pages_partition_every_row_with_an_exact_total(backend: Any) -> None:
    """More than 50 rows per kind: every page reports the full total and the
    pages together list each row exactly once, newest first."""
    ws_ids = [f"inter-{i:03d}" for i in range(60)] + [f"coord-{i:03d}" for i in range(55)]
    for ws_id in ws_ids:
        _seed(backend, ws_id, kind="coordinator" if ws_id.startswith("coord") else "interactive")
    newest_first = sorted(ws_ids, key=lambda w: (int(w[-3:]), w))
    stamp_updated_newest_first(backend, newest_first)

    seen: list[str] = []
    for offset in range(0, 150, 50):
        page = backend.list_saved_workstreams(kinds=BOTH, viewer=None, limit=50, offset=offset)
        assert page.total == 115
        seen.extend(row["ws_id"] for row in page.rows)
    assert seen == newest_first

    past_the_end = backend.list_saved_workstreams(kinds=BOTH, viewer=None, offset=115)
    assert (past_the_end.rows, past_the_end.total) == ([], 115)


# ---------------------------------------------------------------------------
# Sorting
# ---------------------------------------------------------------------------


@pytest.fixture
def sortable(backend: Any) -> Any:
    """Three rows each column orders differently; ``viewer`` owns ws-b."""
    backend.create_project("omega", "Omega", "owner", visibility="public")
    backend.create_project("beta", "beta", "owner")  # private: not in viewer's list
    backend.create_persona(
        {
            "persona_id": "id-saved-z",
            "name": "saved-z",
            "display_name": "Zeta",
            "description": "",
            "base_prompt": "You are a test persona.",
            "applies_to_kinds": ["interactive", "coordinator"],
        }
    )
    _seed(
        backend,
        "ws-a",
        messages=3,
        alias="Bravo",
        kind="coordinator",
        persona="saved-z",
        project_id="omega",
    )
    _seed(
        backend,
        "ws-b",
        messages=1,
        title="alpha",
        persona="p-a",
        user_id="viewer",
        project_id="beta",
    )
    _seed(backend, "ws-c", messages=2, name="charlie")
    backend.save_workstream_config("ws-a", {"model_alias": "m-b"})
    backend.save_workstream_config("ws-c", {"model_alias": "M-A"})
    for i in range(2):
        _seed(backend, f"child-b{i}", messages=0, parent_ws_id="ws-b")
    _seed(backend, "child-c", messages=0, parent_ws_id="ws-c")
    backend.create_model_definition("da", alias="m-b", model="mb", context_window=1000)
    backend.create_model_definition("dc", alias="M-A", model="ma", context_window=1000)
    backend.record_usage_event("ua", "", "ws-a", "", "mb", 500, 1, 0)
    backend.record_usage_event("ub", "", "ws-b", "", "x", 100, 1, 0)
    backend.record_usage_event("uc", "", "ws-c", "", "ma", 900, 1, 0)
    stamp_updated_newest_first(backend, ["ws-a", "ws-c", "ws-b", "child-b0", "child-b1", "child-c"])
    return backend


_ASCENDING = {
    "name": ["ws-b", "ws-a", "ws-c"],  # alpha, Bravo, charlie: folded ("B" < "a" raw)
    "kind": ["ws-a", "ws-b", "ws-c"],  # coordinator, then interactive by ws_id
    "persona": ["ws-c", "ws-b", "ws-a"],  # "", slug "p-a", label "Zeta"
    "project": ["ws-b", "ws-c", "ws-a"],  # unlisted "", none "", "Omega"
    "model": ["ws-b", "ws-c", "ws-a"],  # "", "m-a", "m-b"
    "message_count": ["ws-b", "ws-c", "ws-a"],  # 1, 2, 3
    "child_count": ["ws-a", "ws-c", "ws-b"],  # 0, 1, 2
    "context_ratio": ["ws-b", "ws-a", "ws-c"],  # no window, 0.5, 0.9
    "updated": ["ws-b", "ws-c", "ws-a"],
    "ws_id": ["ws-a", "ws-b", "ws-c"],
}


def test_sort_cases_cover_every_key() -> None:
    assert set(_ASCENDING) == SAVED_WORKSTREAM_SORT_KEYS


@pytest.mark.parametrize("key", sorted(_ASCENDING))
def test_sort_keys_order_both_ways(sortable: Any, key: str) -> None:
    def parents(**kwargs: Any) -> list[str]:
        return [
            w for w in _ids(sortable, viewer="viewer", sort=key, **kwargs) if w.startswith("ws-")
        ]

    assert parents(descending=False) == _ASCENDING[key]
    assert parents(descending=True) == list(reversed(_ASCENDING[key]))


def test_project_sort_reads_names_the_viewer_can_see(sortable: Any) -> None:
    """Service scope sees every project name, so the private one now sorts."""
    ids = _ids(sortable, sort="project", descending=False)
    assert [w for w in ids if w.startswith("ws-")] == ["ws-c", "ws-b", "ws-a"]


def test_persona_sort_reads_an_archived_persona_by_its_slug(backend: Any) -> None:
    """The dashboards label an archived persona by its slug (their persona list
    holds enabled ones only), so its display name does not sort it."""
    for name, display_name in (("live", "Middle"), ("zz-archived", "Aardvark")):
        backend.create_persona(
            {
                "persona_id": f"id-{name}",
                "name": name,
                "display_name": display_name,
                "description": "",
                "base_prompt": "You are a test persona.",
                "applies_to_kinds": ["interactive"],
            }
        )
    with backend._engine.begin() as conn:
        conn.execute(sa.text("UPDATE personas SET enabled = 0 WHERE name = 'zz-archived'"))
    _seed(backend, "ws-archived", persona="zz-archived")
    _seed(backend, "ws-live", persona="live")
    assert _ids(backend, sort="persona", descending=False) == ["ws-live", "ws-archived"]


def test_unknown_sort_key_is_rejected(backend: Any) -> None:
    with pytest.raises(ValueError):
        backend.list_saved_workstreams(kinds=BOTH, viewer=None, sort="state")


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


def test_search_matches_each_field_case_insensitively(backend: Any) -> None:
    backend.create_project("p-pub", "Quarterly Plans", "owner", visibility="public")
    _seed(backend, "by-alias", alias="Release-Notes")
    _seed(backend, "by-title", title="Weekly RELEASE review")
    _seed(backend, "by-name", name="release candidate")
    _seed(backend, "by-project", project_id="p-pub")
    _seed(backend, "release-by-id")
    _seed(backend, "unrelated", title="something else")

    assert set(_ids(backend, search="  release ")) == {
        "by-alias",
        "by-title",
        "by-name",
        "release-by-id",
    }
    assert _ids(backend, search="QUARTERLY", viewer="someone") == ["by-project"]
    page = backend.list_saved_workstreams(kinds=BOTH, viewer=None, search="release", limit=2)
    assert page.total == 4
    assert len(page.rows) == 2


def test_search_folds_case_beyond_ascii(backend: Any) -> None:
    """Text typed as stored always matches, whatever the database's own case
    rules; SQLite also folds letters its built-in lower() leaves alone, in
    text that mixes them with ASCII."""
    _seed(backend, "accented", title="Élan vital")
    _seed(backend, "plain", title="Release notes")
    for search in ("Élan", "ÉLAN VITAL", "VITAL", "élan"):
        if search == "élan" and backend._engine.dialect.name != "sqlite":
            continue  # PostgreSQL folds É under its database locale
        assert _ids(backend, search=search) == ["accented"], search


def test_sqlite_folds_ascii_text_without_calling_python(backend: Any) -> None:
    """Each call of a Python function from SQL takes the interpreter lock, so
    a busy thread in the process slows a search to a crawl. ASCII text folds
    with SQLite's own lower(); only other text reaches Python."""
    if backend._engine.dialect.name != "sqlite":
        pytest.skip("SQLite folds through a Python function")
    _seed(backend, "plain", title="Release notes")
    _seed(backend, "accented", title="Élan vital")
    folded: list[str] = []

    def counting_fold(value: Any) -> Any:
        folded.append(value)
        return fold_text(value)

    with backend._engine.connect() as conn:
        conn.connection.driver_connection.create_function(
            SQLITE_FOLD_FUNCTION, 1, counting_fold, deterministic=True
        )
        page = query_saved_workstreams(
            conn,
            dialect_name="sqlite",
            kinds=BOTH,
            viewer=None,
            project_names=False,
            search="VITAL",
            sort="name",
            descending=False,
            limit=20,
            offset=0,
        )
    assert [row["ws_id"] for row in page.rows] == ["accented"]
    assert folded
    assert not [value for value in folded if value.isascii()]


def test_search_folds_each_letter_on_its_own(backend: Any) -> None:
    """A capital sigma at the end of a search folds as it does inside the
    stored word (``lower`` would give it the final form). PostgreSQL folds
    under its database locale."""
    if backend._engine.dialect.name != "sqlite":
        pytest.skip("PostgreSQL folds under its database locale")
    _seed(backend, "greek", title="ΟΔΟΣΑ")
    assert _ids(backend, search="ΟΔΟΣ") == ["greek"]
    assert _ids(backend, search="Σ") == ["greek"]


def test_search_treats_wildcards_literally(backend: Any) -> None:
    _seed(backend, "percent", title="100% done")
    _seed(backend, "plain", title="1000 done")
    _seed(backend, "underscore", title="snake_case")
    _seed(backend, "letter", title="snakeXcase")
    assert _ids(backend, search="100%") == ["percent"]
    assert _ids(backend, search="e_c") == ["underscore"]


def test_project_names_need_the_caller_to_read_projects(backend: Any) -> None:
    """Without ``project_names`` (no ``project.read``) search and the project
    sort leave project names out; with it, the total counts every match."""
    backend.create_project("p-q", "Quarterly Plans", "owner", visibility="public")
    backend.create_project("p-a", "Annual Plans", "owner", visibility="public")
    _seed(backend, "quarterly", project_id="p-q")
    _seed(backend, "annual", project_id="p-a")
    _seed(backend, "plain", title="no project")

    assert set(_ids(backend, viewer="someone", search="plans")) == {"quarterly", "annual"}
    page = backend.list_saved_workstreams(
        kinds=BOTH, viewer="someone", project_names=True, search="plans"
    )
    assert page.total == 2
    assert _ids(backend, viewer="someone", search="plans", project_names=False) == []

    by_project = {"sort": "project", "descending": False, "viewer": "someone"}
    assert _ids(backend, **by_project) == ["plain", "annual", "quarterly"]
    # Every project reads the same, so ws_id alone orders the rows.
    assert _ids(backend, **by_project, project_names=False) == ["annual", "plain", "quarterly"]


def test_search_reads_project_names_only_where_listed(backend: Any) -> None:
    """A viewer's own workstream in someone's private project stays visible,
    but that project's name is not theirs to search by."""
    backend.create_project("p-secret", "Secret Plans", "alice")
    _seed(backend, "bobs", user_id="bob", project_id="p-secret")
    assert _ids(backend, viewer="bob") == ["bobs"]
    assert _ids(backend, viewer="bob", search="secret") == []
    assert _ids(backend, viewer="alice", search="secret") == ["bobs"]
    backend.update_project("p-secret", state="archived")
    assert _ids(backend, viewer="alice", search="secret") == []


# ---------------------------------------------------------------------------
# Visibility parity with WorkstreamProjectVisibility
# ---------------------------------------------------------------------------


def test_whitespace_is_what_str_strip_removes() -> None:
    stripped = "".join(ch for ch in map(chr, range(0x110000)) if ch.isspace())
    assert stripped == WHITESPACE


def test_visibility_matches_the_python_predicate(backend: Any) -> None:
    """The SQL predicate and ``WorkstreamProjectVisibility.ws_visible`` agree
    on every combination of project state, ownership and membership."""
    backend.create_project("p-priv", "Private", "owner")
    backend.create_project("p-pub", "Public", "owner", visibility="public")
    backend.create_project("p-member", "Members", "owner")
    backend.add_project_member("p-member", "member")
    backend.create_project("p-blank", "Blank visibility", "owner")
    with backend._engine.begin() as conn:
        conn.execute(sa.text("UPDATE projects SET visibility = '' WHERE project_id = 'p-blank'"))

    # Python's strip() removes every whitespace character, SQL's trim() only
    # spaces unless told which.
    padded = [chr(9) + "p-priv", "p-priv" + chr(10), chr(0xA0) + "p-priv" + chr(0x3000)]
    project_ids = [
        None,
        "",
        "  ",
        WHITESPACE,
        "p-priv",
        "  p-priv ",
        *padded,
        "p-pub",
        "p-member",
        "p-blank",
        "gone",
    ]
    ws_owners = ["", "viewer", "member", "owner"]
    expected_rows: dict[str, tuple[str | None, str]] = {}
    for i, project_id in enumerate(project_ids):
        for j, ws_owner in enumerate(ws_owners):
            ws_id = f"ws-{i}-{j}"
            _seed(backend, ws_id, user_id=ws_owner, project_id=project_id)
            expected_rows[ws_id] = (project_id, ws_owner)

    for viewer in ["", "viewer", "member", "owner", "stranger"]:
        python = WorkstreamProjectVisibility(viewer, storage=backend)
        expected = {
            ws_id
            for ws_id, (project_id, ws_owner) in expected_rows.items()
            if python.ws_visible(project_id, ws_owner=ws_owner)
        }
        assert set(_ids(backend, viewer=viewer)) == expected, viewer
        assert backend.list_saved_workstreams(kinds=BOTH, viewer=viewer).total == len(expected)

    assert set(_ids(backend, viewer=None)) == set(expected_rows)
