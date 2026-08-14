"""Durable complete memory-index rendering and storage semantics."""

import contextlib
import json
import random
from pathlib import Path

import pytest

from turnstone.core.memory import memory_index_health
from turnstone.core.memory_index import (
    MEMORY_INDEX_DEFAULT_BUDGET_CHARS,
    memory_index_base_char_count,
    memory_index_entry_metrics,
    memory_visibility_key,
    normalize_memory_description,
    parse_memory_visibility_key,
    render_memory_index,
    render_memory_pointer,
)
from turnstone.core.project_access import decide_project_access, fold_role_permissions

_DESCRIPTION_PARITY = json.loads(
    (Path(__file__).parent / "data" / "memory_description_parity.json").read_text()
)


def test_description_is_one_line_required_and_bounded() -> None:
    for codepoint in _DESCRIPTION_PARITY["whitespace_code_points"]:
        whitespace = chr(codepoint)
        assert (
            normalize_memory_description(
                f"{whitespace}alpha{whitespace}{whitespace}beta{whitespace}"
            )
            == "alpha beta"
        )
    preserved = "".join(
        chr(codepoint) for codepoint in _DESCRIPTION_PARITY["preserved_code_points"]
    )
    assert normalize_memory_description(f"{preserved}alpha{preserved}") == (
        f"{preserved}alpha{preserved}"
    )
    for invalid in [
        *_DESCRIPTION_PARITY["empty_inputs"],
        *_DESCRIPTION_PARITY["non_string_inputs"],
    ]:
        with pytest.raises(ValueError, match="required"):
            normalize_memory_description(invalid)
    for boundary in _DESCRIPTION_PARITY["boundaries"]:
        value = boundary["character"] * boundary["count"]
        if boundary["valid"]:
            assert normalize_memory_description(value) == value
        else:
            with pytest.raises(ValueError, match="512"):
                normalize_memory_description(value)


def test_visibility_key_is_deterministic_and_round_trips() -> None:
    scopes = [("user", "u1"), ("global", ""), ("global", "")]
    key = memory_visibility_key(scopes)
    assert parse_memory_visibility_key(key) == [("global", ""), ("user", "u1")]


@pytest.mark.parametrize(
    (
        "principal_id",
        "owner_id",
        "visibility",
        "state",
        "member",
        "permissions",
        "expected",
    ),
    [
        ("owner", "owner", "private", "active", False, set(), (True, True)),
        ("member", "owner", "private", "active", True, {"project.read"}, (True, False)),
        ("member", "owner", "private", "active", True, set(), (False, False)),
        ("reader", "owner", "public", "active", False, {"project.read"}, (True, False)),
        ("reader", "owner", "public", "active", False, set(), (False, False)),
        (
            "writer",
            "owner",
            "private",
            "active",
            True,
            {"project.read", "project.write"},
            (True, True),
        ),
        ("writer", "owner", "public", "active", False, {"project.write"}, (False, False)),
        ("owner", "owner", "public", "archived", True, {"project.read"}, (False, False)),
        ("owner", "owner", "public", "missing", True, {"project.read"}, (False, False)),
    ],
)
def test_project_access_policy_matrix(
    principal_id: str,
    owner_id: str,
    visibility: str,
    state: str,
    member: bool,
    permissions: set[str],
    expected: tuple[bool, bool],
) -> None:
    decision = decide_project_access(
        principal_id=principal_id,
        owner_id=owner_id,
        visibility=visibility,
        state=state,
        is_member=member,
        permissions=permissions,
    )
    assert (decision.can_read, decision.can_write) == expected


def test_builtin_grants_and_revokes_fold_before_project_policy() -> None:
    assert fold_role_permissions("project.read", revokes={"project.read"}) == set()
    assert fold_role_permissions("", grants={"project.read"}) == {"project.read"}


def test_complete_index_is_deterministic_escaped_and_body_free() -> None:
    rows = [
        {
            "memory_id": "2",
            "name": "later<script>\nforged line",
            "description": "safe & useful",
            "type": "reference",
            "scope": "user",
            "scope_id": "u1",
            "content": "MUST NOT APPEAR",
        },
        {
            "memory_id": "1",
            "name": "first",
            "description": "",
            "type": "general",
            "scope": "global",
            "scope_id": "",
            "content": "NOR THIS",
        },
    ]
    rendered = render_memory_index(rows, project_id='project<&"')
    assert rendered.entry_count == 2
    assert rendered.invalid_description_count == 1
    assert rendered.char_count == len(rendered.content)
    assert 'project_id="project&lt;&amp;&quot;"' in rendered.content
    assert "[global/general] first — hook unavailable; edit required" in rendered.content
    assert "later&lt;script&gt;\\u000aforged line" in rendered.content
    assert "\nforged line" not in rendered.content
    assert "safe &amp; useful" in rendered.content
    assert "MUST NOT APPEAR" not in rendered.content
    assert (
        rendered.content
        == render_memory_index(list(reversed(rows)), project_id='project<&"').content
    )
    assert 'project_id=""' in render_memory_index([]).content


@pytest.mark.parametrize("entry_count", [0, 9, 10, 99, 100])
@pytest.mark.parametrize("project_id", ["", 'project<&"', "π\u0000\u202e"])
def test_renderer_metrics_are_exact(entry_count: int, project_id: str) -> None:
    rows = [
        {
            "memory_id": f"m{index:03d}",
            "name": f"hook_{index}_π\u0000",
            "description": "authored 🙂 hook" if index % 2 else "",
            "type": "reference" if index % 3 else "general",
            "scope": "global",
            "scope_id": "",
        }
        for index in range(entry_count)
    ]
    rendered = render_memory_index(rows, project_id=project_id)
    entry_chars = sum(memory_index_entry_metrics(row)[0] for row in rows)
    invalid = sum(memory_index_entry_metrics(row)[1] for row in rows)

    assert memory_index_base_char_count(entry_count, project_id=project_id) + entry_chars == len(
        rendered.content
    )
    assert rendered.char_count == len(rendered.content)
    assert rendered.invalid_description_count == invalid


def test_pointer_uses_exact_json_quoted_names_and_scopes() -> None:
    pointer = render_memory_pointer([{"name": 'odd "name"', "scope": "project"}])
    assert 'scope="project"' in pointer
    assert 'name="odd \\"name\\""' in pointer
    assert "untrusted metadata" in pointer


@pytest.mark.parametrize(
    ("unsafe", "marker"),
    [
        ("\u0085", r"\u0085"),
        ("\u2028", r"\u2028"),
        ("\u2029", r"\u2029"),
        ("\u202e", r"\u202e"),
        ("\ud800", r"\ud800"),
        ("\ufffe", r"\ufffe"),
    ],
)
def test_renderers_make_unicode_layout_controls_visible(
    unsafe: str,
    marker: str,
) -> None:
    import xml.etree.ElementTree as ET

    row = {
        "memory_id": "m1",
        "name": f"safe{unsafe}forged",
        "description": "authored hook",
        "type": "general",
        "scope": "global",
        "scope_id": "",
    }
    index = render_memory_index([row]).content
    pointer = render_memory_pointer([row])

    assert unsafe not in index
    assert unsafe not in pointer
    assert marker in index
    assert marker.replace("\\", "\\\\") in pointer
    assert index.count("\n") == 3
    ET.fromstring(index)


class TestMemoryIndexStorage:
    def test_metadata_lists_are_body_free_and_scope_exact(self, backend) -> None:
        backend.create_structured_memory(
            "m1", "global_note", "global hook", "general", "global", "", "secret-global"
        )
        backend.create_structured_memory(
            "m2", "user_note", "user hook", "general", "user", "u1", "secret-user"
        )
        backend.create_structured_memory(
            "m3", "other_note", "other hook", "general", "user", "u2", "secret-other"
        )
        rows = backend.list_visible_memory_index_entries([("global", ""), ("user", "u1")])
        assert {row["name"] for row in rows} == {"global_note", "user_note"}
        assert all("content" not in row for row in rows)

    def test_snapshot_first_writer_wins_and_is_deleted_with_workstream(self, backend) -> None:
        backend.register_workstream("ws-index", user_id="u1")
        backend.create_structured_memory(
            "m-first", "first", "first hook", "general", "global", "", "first body"
        )
        first = backend.acquire_memory_index_snapshot("ws-index", "u1")
        backend.create_structured_memory(
            "m-second", "second", "second hook", "general", "global", "", "second body"
        )
        second = backend.acquire_memory_index_snapshot("ws-index", "u2")
        assert first is not None and second is not None
        assert first["content"] == second["content"]
        assert "first hook" in first["content"]
        assert "second hook" not in first["content"]
        assert first["principal_id"] == second["principal_id"] == "u1"
        assert backend.delete_workstream("ws-index") is True
        assert backend.get_memory_index_snapshot("ws-index") is None

    def test_snapshot_commit_context_rejection_rolls_back_candidate(self, backend) -> None:
        backend.register_workstream("ws-guard", user_id="u1")

        @contextlib.contextmanager
        def reject_commit(candidate):
            assert candidate is not None
            assert candidate["ws_id"] == "ws-guard"
            raise RuntimeError("generation superseded")
            yield

        with pytest.raises(RuntimeError, match="generation superseded"):
            backend.acquire_memory_index_snapshot(
                "ws-guard",
                "u1",
                commit_context=reject_commit,
            )

        assert backend.get_memory_index_snapshot("ws-guard") is None

    def test_snapshot_commit_context_is_not_entered_without_candidate(self, backend) -> None:
        @contextlib.contextmanager
        def unexpected_context(_candidate):
            raise AssertionError("missing workstreams have no commit candidate")
            yield

        assert (
            backend.acquire_memory_index_snapshot(
                "missing-workstream",
                "u1",
                commit_context=unexpected_context,
            )
            is None
        )

    def test_writes_do_not_count_as_fetches_and_lists_omit_content(self, backend) -> None:
        backend.create_structured_memory(
            "m1", "note", "first hook", "general", "global", "", "body"
        )
        created = backend.get_structured_memory("m1")
        assert created["last_accessed"] == ""
        assert created["access_count"] == 0
        backend.upsert_structured_memory(
            "different-id", "note", "second hook", None, "global", "", "new body"
        )
        updated = backend.get_structured_memory("m1")
        assert updated["last_accessed"] == ""
        assert updated["access_count"] == 0
        assert "content" not in backend.list_structured_memories()[0]
        assert backend.search_structured_memories("new body") == []

    def test_health_stays_red_without_snapshot_rows_at_the_exact_budget_edge(
        self,
        backend,
    ) -> None:
        rows = [
            {
                "memory_id": f"m{i:03d}",
                "name": f"hook_{i:03d}",
                "description": "x" * 512,
                "type": "general",
                "scope": "global",
                "scope_id": "",
            }
            for i in range(121)
        ]
        assert render_memory_index(rows[:-1]).char_count == 65_533
        assert render_memory_index(rows).char_count == 66_076

        backend.register_workstream("ws-health", user_id="u1")
        for row in rows:
            backend.create_structured_memory(
                row["memory_id"],
                row["name"],
                row["description"],
                row["type"],
                row["scope"],
                row["scope_id"],
                "private body",
            )
        backend.acquire_memory_index_snapshot("ws-health", "u1")

        before = memory_index_health(
            budget_chars=MEMORY_INDEX_DEFAULT_BUDGET_CHARS,
            storage=backend,
        )
        assert before["over_budget"] is True
        assert before["max_char_count"] == 66_076

        assert backend.delete_workstream("ws-health") is True
        assert backend.get_memory_index_snapshot("ws-health") is None
        after_snapshot_delete = memory_index_health(
            budget_chars=MEMORY_INDEX_DEFAULT_BUDGET_CHARS,
            storage=backend,
        )
        assert after_snapshot_delete["over_budget"] is True
        assert after_snapshot_delete["max_char_count"] == before["max_char_count"]

        assert backend.delete_structured_memory("hook_120") is True
        after_memory_delete = memory_index_health(
            budget_chars=MEMORY_INDEX_DEFAULT_BUDGET_CHARS,
            storage=backend,
        )
        assert after_memory_delete["over_budget"] is False
        assert after_memory_delete["max_char_count"] == 65_533

    def test_health_maximum_matches_real_interactive_and_coordinator_captures(
        self,
        backend,
    ) -> None:
        backend.create_user("owner", "owner", "Owner", "hash")
        backend.create_user("member", "member", "Member", "hash")
        backend.create_project("health-project", "Health Project", "owner")
        backend.create_role(
            "health-reader",
            "health-reader",
            "Health Reader",
            "project.read",
            False,
        )
        backend.assign_role("member", "health-reader")
        backend.add_project_member("health-project", "member")
        rows = [
            ("global", "", "global_hook", "global description"),
            ("user", "owner", "owner_hook", "owner description"),
            ("user", "member", "member_hook", "member description"),
            ("coordinator", "owner", "owner_coord", "owner coordinator description"),
            ("coordinator", "member", "member_coord", "member coordinator description"),
            ("project", "health-project", "project_hook", "project description"),
        ]
        for index, (scope, scope_id, name, description) in enumerate(rows):
            backend.create_structured_memory(
                f"health-memory-{index}",
                name,
                description,
                "general",
                scope,
                scope_id,
                "private body",
            )

        captures = []
        for kind in ("interactive", "coordinator"):
            for principal in ("owner", "member"):
                ws_id = f"health-{kind}-{principal}"
                backend.register_workstream(
                    ws_id,
                    user_id="owner",
                    kind=kind,
                    project_id="health-project",
                )
                snapshot = backend.acquire_memory_index_snapshot(ws_id, principal)
                assert snapshot is not None
                assert snapshot["project_id"] == "health-project"
                captures.append(snapshot)

        global_only = render_memory_index(
            [
                {
                    "memory_id": "health-memory-0",
                    "name": "global_hook",
                    "description": "global description",
                    "type": "general",
                    "scope": "global",
                    "scope_id": "",
                }
            ]
        )
        health = memory_index_health(budget_chars=65_536, storage=backend)
        assert health["max_char_count"] == max(
            global_only.char_count,
            *(int(snapshot["char_count"]) for snapshot in captures),
        )
        assert health["max_entry_count"] == max(
            global_only.entry_count,
            *(int(snapshot["entry_count"]) for snapshot in captures),
        )

    @pytest.mark.parametrize("kind", ["interactive", "coordinator"])
    @pytest.mark.parametrize(
        ("scenario", "principal", "visibility", "state", "member", "role", "expected"),
        [
            ("owner-no-role", "owner", "private", "active", False, "none", True),
            ("private-member-read", "member", "private", "active", True, "read", True),
            ("private-member-no-read", "member", "private", "active", True, "none", False),
            ("public-nonmember-read", "reader", "public", "active", False, "read", True),
            ("public-nonmember-no-read", "reader", "public", "active", False, "none", False),
            (
                "builtin-read-revoked",
                "member",
                "private",
                "active",
                True,
                "revoked-read",
                False,
            ),
            (
                "override-read-granted",
                "member",
                "private",
                "active",
                True,
                "granted-read",
                True,
            ),
            ("archived", "owner", "private", "archived", False, "none", False),
            ("missing", "reader", "private", "missing", False, "read", False),
        ],
    )
    def test_rbac_health_envelopes_are_realizable(
        self,
        backend,
        kind: str,
        scenario: str,
        principal: str,
        visibility: str,
        state: str,
        member: bool,
        role: str,
        expected: bool,
    ) -> None:
        """Health uses the exact capture policy for every RBAC topology."""
        for user_id in {"owner", principal}:
            backend.create_user(user_id, user_id, user_id.title(), "hash")
        project_id = f"matrix-{scenario}-{kind}"
        if state != "missing":
            backend.create_project(project_id, "Matrix Project", "owner", visibility=visibility)
            if state == "archived":
                assert backend.update_project(project_id, state="archived") is True
        if member:
            backend.add_project_member(project_id, principal)
        if role != "none":
            baseline = "project.read" if role in {"read", "revoked-read"} else ""
            backend.create_role("matrix-role", "matrix-role", "Matrix Role", baseline, True)
            backend.assign_role(principal, "matrix-role")
            if role == "revoked-read":
                backend.set_role_overrides("matrix-role", set(), {"project.read"})
            elif role == "granted-read":
                backend.set_role_overrides("matrix-role", {"project.read"}, set())

        global_row = {
            "memory_id": "matrix-global",
            "name": "global_hook",
            "description": "Global matrix hook",
            "type": "general",
            "scope": "global",
            "scope_id": "",
        }
        backend.create_structured_memory(
            "matrix-global",
            "global_hook",
            "Global matrix hook",
            "general",
            "global",
            "",
            "global body",
        )
        backend.create_structured_memory(
            "matrix-project-memory",
            "project_hook",
            "P" * 400,
            "general",
            "project",
            project_id,
            "project body",
        )
        principal_scope = "coordinator" if kind == "coordinator" else "user"
        for candidate in {principal, "owner"}:
            backend.create_structured_memory(
                f"matrix-{principal_scope}-{candidate}",
                f"{candidate}_hook",
                f"{candidate} {principal_scope} hook",
                "general",
                principal_scope,
                candidate,
                "private body",
            )

        captures: dict[str, dict[str, object]] = {}
        for candidate in sorted({principal, "owner"}):
            ws_id = f"matrix-{scenario}-{kind}-{candidate}"
            backend.register_workstream(
                ws_id,
                user_id=candidate,
                kind=kind,
                project_id=project_id,
            )
            snapshot = backend.acquire_memory_index_snapshot(ws_id, candidate)
            assert snapshot is not None
            captures[candidate] = snapshot

        tested = captures[principal]
        assert bool(tested["project_id"]) is expected
        assert ("project_hook" in str(tested["content"])) is expected

        global_only = render_memory_index([global_row])
        health = memory_index_health(budget_chars=65_536, storage=backend)
        assert health["max_char_count"] == max(
            global_only.char_count,
            *(int(snapshot["char_count"]) for snapshot in captures.values()),
        )
        assert health["max_entry_count"] == max(
            global_only.entry_count,
            *(int(snapshot["entry_count"]) for snapshot in captures.values()),
        )


def test_health_metric_index_matches_brute_force_envelopes() -> None:
    """The optimized range-max calculation must remain renderer-exact."""

    def principal_ids(inputs):
        result = {str(row.get("user_id") or "") for row in inputs["users"] if row.get("user_id")}
        for row in inputs["entries"]:
            if row["scope"] in {"user", "coordinator"} and row["scope_id"]:
                result.add(row["scope_id"])
        for row in inputs["projects"]:
            if row.get("owner_id"):
                result.add(row["owner_id"])
        for row in inputs["members"] + inputs["workstreams"]:
            if row.get("user_id"):
                result.add(row["user_id"])
        return result

    def brute_force(inputs):
        entries = inputs["entries"]
        principals = principal_ids(inputs)
        projects = {row["project_id"]: row for row in inputs["projects"]}
        members = {(row["project_id"], row["user_id"]) for row in inputs["members"]}

        overrides = {}
        for row in inputs["role_overrides"]:
            grants, revokes = overrides.setdefault(row["role_id"], (set(), set()))
            (grants if row["action"] == "grant" else revokes).add(row["permission"])
        role_permissions = {}
        for row in inputs["roles"]:
            grants, revokes = overrides.get(row["role_id"], (set(), set()))
            if not row["builtin"]:
                grants, revokes = set(), set()
            role_permissions[row["role_id"]] = fold_role_permissions(
                row["permissions"], grants=grants, revokes=revokes
            )
        principal_permissions = {}
        for row in inputs["user_roles"]:
            principal_permissions.setdefault(row["user_id"], set()).update(
                role_permissions.get(row["role_id"], set())
            )

        def project_visible(project_id, principal_id):
            if not project_id or project_id not in projects or not principal_id:
                return False
            project = projects[project_id]
            return decide_project_access(
                principal_id=principal_id,
                owner_id=project["owner_id"],
                visibility=project["visibility"],
                state=project["state"],
                is_member=(project_id, principal_id) in members,
                permissions=principal_permissions.get(principal_id, set()),
            ).can_read

        envelopes = [
            render_memory_index(
                [row for row in entries if (row["scope"], row["scope_id"]) == ("global", "")]
            )
        ]
        for workstream in inputs["workstreams"]:
            ws_id = workstream["ws_id"]
            project_id = workstream.get("project_id") or ""
            if workstream["kind"] == "coordinator":
                candidates = sorted(principals)
            else:
                candidates = ["", *sorted(principals)]
            for principal_id in candidates:
                if workstream["kind"] == "coordinator":
                    scopes = {("coordinator", principal_id)}
                else:
                    scopes = {("global", ""), ("workstream", ws_id)}
                    if principal_id:
                        scopes.add(("user", principal_id))
                visible_project = project_id if project_visible(project_id, principal_id) else ""
                if visible_project:
                    scopes.add(("project", visible_project))
                envelopes.append(
                    render_memory_index(
                        [row for row in entries if (row["scope"], row["scope_id"]) in scopes],
                        project_id=visible_project,
                    )
                )
        return {
            "max_char_count": max(envelope.char_count for envelope in envelopes),
            "max_entry_count": max(envelope.entry_count for envelope in envelopes),
            "envelope_count": len(envelopes),
        }

    class FakeStorage:
        def __init__(self, inputs):
            self.inputs = inputs

        def get_memory_index_health_inputs(self):
            return self.inputs

    rng = random.Random(902)
    scope_ids = {
        "global": [""],
        "workstream": ["w0", "w1"],
        "user": ["u0", "u1", "u2"],
        "coordinator": ["u0", "u1", "u2"],
        "project": ["p0", "p1"],
    }
    for _ in range(100):
        entries = []
        for index in range(rng.randrange(20)):
            scope = rng.choice(list(scope_ids))
            entries.append(
                {
                    "memory_id": f"m{index}",
                    "name": f"hook_{index}_{rng.randrange(10)}",
                    "description": "x" * rng.randrange(1, 513),
                    "type": rng.choice(["general", "reference"]),
                    "scope": scope,
                    "scope_id": rng.choice(scope_ids[scope]),
                }
            )
        inputs = {
            "entries": entries,
            "workstreams": [
                {
                    "ws_id": "w0",
                    "kind": "interactive",
                    "user_id": "u0",
                    "project_id": rng.choice(["", "p0", "p1"]),
                },
                {
                    "ws_id": "w1",
                    "kind": rng.choice(["interactive", "coordinator"]),
                    "user_id": "u1",
                    "project_id": rng.choice(["", "p0", "p1"]),
                },
            ],
            "projects": [
                {
                    "project_id": "p0",
                    "owner_id": "u0",
                    "visibility": rng.choice(["private", "public"]),
                    "state": rng.choice(["active", "active", "archived"]),
                },
                {
                    "project_id": "p1",
                    "owner_id": "u2",
                    "visibility": rng.choice(["private", "public"]),
                    "state": rng.choice(["active", "active", "archived"]),
                },
            ],
            "members": [
                {"project_id": "p0", "user_id": "u1"},
                {"project_id": "p1", "user_id": "u1"},
            ][: rng.randrange(3)],
            "users": [{"user_id": f"u{index}"} for index in range(rng.randrange(4))],
            "roles": [
                {
                    "role_id": "reader",
                    "permissions": rng.choice(["", "project.read", "project.write"]),
                    "builtin": True,
                },
                {
                    "role_id": "custom",
                    "permissions": rng.choice(["", "project.read", "project.write"]),
                    "builtin": False,
                },
            ],
            "user_roles": [
                {"user_id": f"u{index}", "role_id": rng.choice(["reader", "custom"])}
                for index in range(3)
                if rng.choice([True, False])
            ],
            "role_overrides": [
                {
                    "role_id": "reader",
                    "permission": "project.read",
                    "action": rng.choice(["grant", "revoke"]),
                }
            ][: rng.randrange(2)],
        }
        expected = brute_force(inputs)
        actual = memory_index_health(budget_chars=65_536, storage=FakeStorage(inputs))
        assert {key: actual[key] for key in expected} == expected, inputs


def test_health_project_authorization_scales_with_distinct_live_projects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Public-project health reuses reader metrics instead of P x J matrices."""
    from turnstone.core.memory import _PrincipalMetricSet

    principal_count = 64
    project_count = 64
    principals = [f"u{index}" for index in range(principal_count)]
    inputs = {
        "entries": [],
        "workstreams": [
            {
                "ws_id": f"p{index}-interactive",
                "kind": "interactive",
                "user_id": principals[index % principal_count],
                "project_id": f"p{index}",
            }
            for index in range(project_count)
        ]
        + [
            {
                "ws_id": f"p{index}-coordinator",
                "kind": "coordinator",
                "user_id": principals[index % principal_count],
                "project_id": f"p{index}",
            }
            for index in range(project_count)
        ],
        "projects": [
            {
                "project_id": f"p{index}",
                "owner_id": principals[index % principal_count],
                "visibility": "public",
                "state": "active",
            }
            for index in range(project_count)
        ],
        "members": [],
        "users": [{"user_id": user_id} for user_id in principals],
        "roles": [
            {
                "role_id": "reader",
                "permissions": "project.read",
                "builtin": False,
            }
        ],
        "user_roles": [{"user_id": user_id, "role_id": "reader"} for user_id in principals],
        "role_overrides": [],
    }

    class FakeStorage:
        def get_memory_index_health_inputs(self):
            return inputs

    metric_bucket_counts: list[int] = []
    real_init = _PrincipalMetricSet.__init__

    def counting_init(self, buckets):
        metric_bucket_counts.append(len(buckets))
        real_init(self, buckets)

    monkeypatch.setattr(_PrincipalMetricSet, "__init__", counting_init)

    memory_index_health(budget_chars=65_536, storage=FakeStorage())

    # Two unfiltered scope metrics plus two project.read-filtered metrics.
    # Neither distinct projects nor workstreams multiply principal scans.
    assert metric_bucket_counts == [principal_count] * 4
