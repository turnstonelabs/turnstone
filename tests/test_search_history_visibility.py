"""Tenancy scoping for conversation-history search (recall tool + /history).

``search_history`` / ``search_history_recent`` used to search every
workstream's rows regardless of who asked — with private projects
(migration 062) that is a cross-tenant read.  The SQL predicate
(``HISTORY_VISIBILITY_SCOPE_SQL``) mirrors ``WorkstreamProjectVisibility``
(core.auth), THE statement of the tenancy rule: a row is hidden only when
its workstream links to an EXISTING project whose visibility is private and
the searcher is neither the workstream creator, the project owner, nor a
member.  Covered here:

- unscoped (``user_id=None``) stays tenant-wide — single-user CLI back-compat;
- trusted-team default: no-project rows are visible across users;
- private project: hidden from strangers; visible to the workstream creator,
  the project owner, and members — in both search and recent;
- public project and dangling project link stay visible;
- a NULL-creator workstream in a private project hides (COALESCE guard);
- compaction markers stay excluded under scoping;
- the sqlite LIKE fallback path applies the same predicate;
- parity: SQL verdicts match ``ws_visible`` across the case matrix, so the
  two statements of the rule cannot drift silently;
- session plumbing: ``_prepare_recall`` pins the scope at prepare time,
  ``_exec_recall`` searches with the pinned identity and refuses to run
  unpinned.
"""

from __future__ import annotations

import pytest

from tests._session_helpers import make_session
from turnstone.core.auth import WorkstreamProjectVisibility

NEEDLE = "zebrafinch"


def _ws(st, ws_id: str, owner: str | None, project_id: str | None = None) -> str:
    st.register_workstream(
        ws_id, user_id=owner, title="t", kind="interactive", project_id=project_id
    )
    st.save_message(ws_id, "user", f"{NEEDLE} in {ws_id}")
    return ws_id


def _found(st, user_id: str | None) -> set[str]:
    return {r[1] for r in st.search_history(NEEDLE, limit=50, user_id=user_id)}


def _recent(st, user_id: str | None) -> set[str]:
    return {r[1] for r in st.search_history_recent(limit=50, user_id=user_id)}


@pytest.fixture
def world(storage_backend):
    """One of each visibility case.

    - ``ws_none``      — no project link (alice's)
    - ``ws_dangling``  — links a project that does not exist (bob's)
    - ``ws_public``    — public project, owned by alice
    - ``ws_priv_own``  — private project ``P`` (owner alice), ws created by alice
    - ``ws_priv_mem``  — private project ``P``, ws created by member bob
    - ``ws_priv_other``— private project ``Q`` (owner dave, no members)
    """
    st = storage_backend
    st.create_project("pub", "Pub", owner_id="alice", visibility="public")
    st.create_project("P", "P", owner_id="alice", visibility="private")
    st.create_project("Q", "Q", owner_id="dave", visibility="private")
    st.add_project_member("P", "bob")
    _ws(st, "ws_none", "alice")
    _ws(st, "ws_dangling", "bob", project_id="ghost")
    _ws(st, "ws_public", "alice", project_id="pub")
    _ws(st, "ws_priv_own", "alice", project_id="P")
    _ws(st, "ws_priv_mem", "bob", project_id="P")
    _ws(st, "ws_priv_other", "dave", project_id="Q")
    return st


ALL_WS = {"ws_none", "ws_dangling", "ws_public", "ws_priv_own", "ws_priv_mem", "ws_priv_other"}


class TestSearchHistoryScope:
    def test_unscoped_stays_tenant_wide(self, world):
        """CLI back-compat: ``user_id=None`` applies no filter."""
        assert _found(world, None) == ALL_WS
        assert _recent(world, None) == ALL_WS

    def test_stranger_loses_only_private_rows(self, world):
        """Trusted-team default: everything visible except other people's
        private-project workstreams."""
        expected = ALL_WS - {"ws_priv_own", "ws_priv_mem", "ws_priv_other"}
        assert _found(world, "carol") == expected
        assert _recent(world, "carol") == expected

    def test_project_owner_sees_all_project_rows(self, world):
        """alice owns P: sees bob's ws in P too; still not dave's Q."""
        assert _found(world, "alice") == ALL_WS - {"ws_priv_other"}

    def test_member_sees_project_rows(self, world):
        """bob is a member of P: sees alice's ws in P; still not Q."""
        assert _found(world, "bob") == ALL_WS - {"ws_priv_other"}

    def test_ws_creator_sees_own_row_in_private_project(self, world):
        """dave is neither owner nor member of P — but Q's rows are his."""
        assert "ws_priv_other" in _found(world, "dave")

    def test_null_creator_private_ws_hides(self, storage_backend):
        """A NULL-creator ws in a private project must hide, not leak: plain
        ``<>`` goes NULL against a NULL creator and would drop the row from
        the hide-subquery (the COALESCE guard in the predicate)."""
        st = storage_backend
        st.create_project("P", "P", owner_id="alice", visibility="private")
        _ws(st, "ws_orphan_creator", None, project_id="P")
        assert _found(st, "carol") == set()
        assert _found(st, "alice") == {"ws_orphan_creator"}  # project owner

    def test_markers_stay_excluded_under_scope(self, world):
        """The compaction-marker exclusion composes with the tenancy scope."""
        world.save_message(
            "ws_none",
            "assistant",
            f"{NEEDLE} SUMMARY",
            source="compaction",
            meta='{"watermark": 1}',
        )
        rows = world.search_history(NEEDLE, limit=50, user_id="alice")
        assert not any("SUMMARY" in (r[3] or "") for r in rows)

    def test_like_fallback_applies_same_predicate(self, world):
        """The sqlite non-FTS path must scope identically."""
        if not hasattr(world, "_fts5_available"):
            pytest.skip("LIKE fallback is sqlite-only")
        world._fts5_available = False
        expected = ALL_WS - {"ws_priv_own", "ws_priv_mem", "ws_priv_other"}
        assert _found(world, "carol") == expected


class TestParityWithWsVisible:
    """The SQL predicate and ``WorkstreamProjectVisibility`` are two
    statements of one rule; this pins them together so neither can drift
    without failing here."""

    # (ws_id, creator, project_id) — mirrors the ``world`` fixture rows.
    MATRIX = [
        ("ws_none", "alice", None),
        ("ws_dangling", "bob", "ghost"),
        ("ws_public", "alice", "pub"),
        ("ws_priv_own", "alice", "P"),
        ("ws_priv_mem", "bob", "P"),
        ("ws_priv_other", "dave", "Q"),
    ]

    @pytest.mark.parametrize("searcher", ["alice", "bob", "carol", "dave"])
    def test_sql_matches_python_predicate(self, world, searcher):
        vis = WorkstreamProjectVisibility(searcher, storage=world)
        expected = {
            ws_id
            for ws_id, creator, project_id in self.MATRIX
            if vis.ws_visible(project_id, ws_owner=creator or "")
        }
        assert _found(world, searcher) == expected
        assert _recent(world, searcher) == expected


class TestRecallScopePlumbing:
    def _recorder(self, calls):
        def fake_search_history(query, limit=20, offset=0, *, user_id=None):
            calls.append(user_id)
            return []

        return fake_search_history

    def test_prepare_pins_owner_without_acting_user(self):
        session = make_session(user_id="owner")
        item = session._prepare_recall("c1", {"query": "x"})
        assert item["scope_user_id"] == "owner"

    def test_prepare_pins_acting_user_over_owner(self):
        session = make_session(user_id="owner")
        session.bind_acting_user("driver")
        item = session._prepare_recall("c1", {"query": "x"})
        assert item["scope_user_id"] == "driver"

    def test_prepare_pins_none_for_single_user_lanes(self):
        session = make_session()  # user_id defaults to "" — CLI lane
        item = session._prepare_recall("c1", {"query": "x"})
        assert item["scope_user_id"] is None

    def test_exec_searches_as_pinned_user(self, monkeypatch):
        calls: list[str | None] = []
        monkeypatch.setattr("turnstone.core.session.search_history", self._recorder(calls))
        session = make_session(user_id="owner")
        item = session._prepare_recall("c1", {"query": "x"})
        session._exec_recall(item)
        assert calls == ["owner"]

    def test_exec_refuses_unpinned_item(self, monkeypatch):
        """Fail loudly rather than fall back to a tenant-wide search."""
        calls: list[str | None] = []
        monkeypatch.setattr("turnstone.core.session.search_history", self._recorder(calls))
        session = make_session(user_id="owner")
        item = session._prepare_recall("c1", {"query": "x"})
        del item["scope_user_id"]
        with pytest.raises(KeyError):
            session._exec_recall(item)
        assert calls == []
