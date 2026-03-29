"""Tests for edit_file tool — single edit and batch edit modes."""

from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest

from turnstone.core.session import ChatSession


@pytest.fixture
def session(tmp_db, mock_openai_client):
    """Create a ChatSession wired to a temp database."""
    return ChatSession(
        client=mock_openai_client,
        model="test-model",
        ui=MagicMock(),
        instructions=None,
        temperature=0.5,
        max_tokens=1000,
        tool_timeout=10,
    )


@pytest.fixture
def sample_file(tmp_path):
    """Create a sample file and return its path."""
    p = tmp_path / "test.py"
    p.write_text("line1\nline2\nline3\nline4\nline5\n")
    return str(p)


def _mark_read(session: ChatSession, path: str) -> None:
    """Simulate a prior read_file so the edit guard passes."""
    resolved = os.path.realpath(os.path.expanduser(path))
    session._read_files.add(resolved)


# ── Single edit (backward compat) ────────────────────────────────────


class TestSingleEdit:
    def test_basic_replace(self, session, sample_file):
        _mark_read(session, sample_file)
        result = session._prepare_edit_file(
            "c1",
            {
                "path": sample_file,
                "old_string": "line2",
                "new_string": "replaced",
            },
        )
        assert result["needs_approval"]
        assert result["func_name"] == "edit_file"

        call_id, msg = session._exec_edit_file(result)
        assert call_id == "c1"
        assert "applied 1 edit" in msg
        with open(sample_file) as f:
            assert f.read() == "line1\nreplaced\nline3\nline4\nline5\n"

    def test_missing_path(self, session):
        result = session._prepare_edit_file(
            "c1",
            {
                "old_string": "a",
                "new_string": "b",
            },
        )
        assert result.get("error")
        assert "missing path" in result["error"]

    def test_missing_old_string(self, session, sample_file):
        result = session._prepare_edit_file(
            "c1",
            {
                "path": sample_file,
                "new_string": "b",
            },
        )
        assert result.get("error")
        assert "old_string" in result["error"]

    def test_identical_strings(self, session, sample_file):
        _mark_read(session, sample_file)
        result = session._prepare_edit_file(
            "c1",
            {
                "path": sample_file,
                "old_string": "line1",
                "new_string": "line1",
            },
        )
        assert result.get("error")
        assert "identical" in result["error"]

    def test_old_string_not_found(self, session, sample_file):
        _mark_read(session, sample_file)
        result = session._prepare_edit_file(
            "c1",
            {
                "path": sample_file,
                "old_string": "nonexistent",
                "new_string": "replaced",
            },
        )
        assert result.get("error")
        assert "not found" in result["error"]

    def test_must_read_first(self, session, sample_file):
        # Don't call _mark_read — should fail
        result = session._prepare_edit_file(
            "c1",
            {
                "path": sample_file,
                "old_string": "line1",
                "new_string": "replaced",
            },
        )
        assert result.get("error")
        assert "must read_file" in result["error"]

    def test_multiple_occurrences_without_near_line(self, session, tmp_path):
        p = tmp_path / "dup.txt"
        p.write_text("foo\nbar\nfoo\n")
        path = str(p)
        _mark_read(session, path)
        result = session._prepare_edit_file(
            "c1",
            {
                "path": path,
                "old_string": "foo",
                "new_string": "baz",
            },
        )
        assert result.get("error")
        assert "found 2 times" in result["error"]

    def test_near_line_disambiguates(self, session, tmp_path):
        p = tmp_path / "dup.txt"
        p.write_text("foo\nbar\nfoo\n")
        path = str(p)
        _mark_read(session, path)
        result = session._prepare_edit_file(
            "c1",
            {
                "path": path,
                "old_string": "foo",
                "new_string": "baz",
                "near_line": 3,
            },
        )
        assert result["needs_approval"]

        call_id, msg = session._exec_edit_file(result)
        assert "applied 1 edit" in msg
        with open(path) as f:
            assert f.read() == "foo\nbar\nbaz\n"

    def test_deletion(self, session, sample_file):
        _mark_read(session, sample_file)
        result = session._prepare_edit_file(
            "c1",
            {
                "path": sample_file,
                "old_string": "line3\n",
                "new_string": "",
            },
        )
        assert result["needs_approval"]
        assert "deletion" in result["preview"]

        call_id, msg = session._exec_edit_file(result)
        with open(sample_file) as f:
            assert f.read() == "line1\nline2\nline4\nline5\n"


# ── Batch edits ──────────────────────────────────────────────────────


class TestBatchEdit:
    def test_two_edits_applied_atomically(self, session, sample_file):
        _mark_read(session, sample_file)
        result = session._prepare_edit_file(
            "c1",
            {
                "path": sample_file,
                "edits": [
                    {"old_string": "line1", "new_string": "first"},
                    {"old_string": "line5", "new_string": "last"},
                ],
            },
        )
        assert result["needs_approval"]
        assert "2 edits" in result["header"]

        call_id, msg = session._exec_edit_file(result)
        assert "applied 2 edits" in msg
        with open(sample_file) as f:
            assert f.read() == "first\nline2\nline3\nline4\nlast\n"

    def test_three_edits_middle_of_file(self, session, sample_file):
        _mark_read(session, sample_file)
        result = session._prepare_edit_file(
            "c1",
            {
                "path": sample_file,
                "edits": [
                    {"old_string": "line2", "new_string": "second"},
                    {"old_string": "line3", "new_string": "third"},
                    {"old_string": "line4", "new_string": "fourth"},
                ],
            },
        )
        assert result["needs_approval"]

        call_id, msg = session._exec_edit_file(result)
        assert "applied 3 edits" in msg
        with open(sample_file) as f:
            assert f.read() == "line1\nsecond\nthird\nfourth\nline5\n"

    def test_overlapping_edits_rejected(self, session, tmp_path):
        p = tmp_path / "overlap.txt"
        p.write_text("abcdefgh\n")
        path = str(p)
        _mark_read(session, path)
        result = session._prepare_edit_file(
            "c1",
            {
                "path": path,
                "edits": [
                    {"old_string": "abcdef", "new_string": "XXX"},
                    {"old_string": "defgh", "new_string": "YYY"},
                ],
            },
        )
        assert result["needs_approval"]

        call_id, msg = session._exec_edit_file(result)
        assert "overlap" in msg.lower()
        # File should be untouched
        with open(path) as f:
            assert f.read() == "abcdefgh\n"

    def test_batch_edit_not_found(self, session, sample_file):
        _mark_read(session, sample_file)
        result = session._prepare_edit_file(
            "c1",
            {
                "path": sample_file,
                "edits": [
                    {"old_string": "line1", "new_string": "first"},
                    {"old_string": "nonexistent", "new_string": "oops"},
                ],
            },
        )
        assert result.get("error")
        assert "edits[1]" in result["error"]
        assert "not found" in result["error"]

    def test_batch_edit_missing_old_string(self, session, sample_file):
        result = session._prepare_edit_file(
            "c1",
            {
                "path": sample_file,
                "edits": [
                    {"old_string": "line1", "new_string": "first"},
                    {"new_string": "oops"},
                ],
            },
        )
        assert result.get("error")
        assert "edits[1]" in result["error"]
        assert "old_string" in result["error"]

    def test_batch_edit_identical_strings(self, session, sample_file):
        result = session._prepare_edit_file(
            "c1",
            {
                "path": sample_file,
                "edits": [
                    {"old_string": "line1", "new_string": "line1"},
                ],
            },
        )
        assert result.get("error")
        assert "identical" in result["error"]

    def test_batch_with_near_line(self, session, tmp_path):
        p = tmp_path / "dup.txt"
        p.write_text("foo\nbar\nfoo\nbaz\n")
        path = str(p)
        _mark_read(session, path)
        result = session._prepare_edit_file(
            "c1",
            {
                "path": path,
                "edits": [
                    {"old_string": "foo", "new_string": "first_foo", "near_line": 1},
                    {"old_string": "foo", "new_string": "second_foo", "near_line": 3},
                ],
            },
        )
        assert result["needs_approval"]

        call_id, msg = session._exec_edit_file(result)
        assert "applied 2 edits" in msg
        with open(path) as f:
            assert f.read() == "first_foo\nbar\nsecond_foo\nbaz\n"

    def test_batch_with_deletion(self, session, sample_file):
        _mark_read(session, sample_file)
        result = session._prepare_edit_file(
            "c1",
            {
                "path": sample_file,
                "edits": [
                    {"old_string": "line2\n", "new_string": ""},
                    {"old_string": "line4\n", "new_string": ""},
                ],
            },
        )
        assert result["needs_approval"]

        call_id, msg = session._exec_edit_file(result)
        with open(sample_file) as f:
            assert f.read() == "line1\nline3\nline5\n"

    def test_single_item_edits_array(self, session, sample_file):
        """An edits array with one item should work like a single edit."""
        _mark_read(session, sample_file)
        result = session._prepare_edit_file(
            "c1",
            {
                "path": sample_file,
                "edits": [
                    {"old_string": "line3", "new_string": "middle"},
                ],
            },
        )
        assert result["needs_approval"]
        # Single edit — no "(N edits)" count in header
        assert "edits)" not in result["header"]

        call_id, msg = session._exec_edit_file(result)
        assert "applied 1 edit" in msg
        with open(sample_file) as f:
            assert f.read() == "line1\nline2\nmiddle\nline4\nline5\n"


# ── Mutual exclusivity ──────────────────────────────────────────────


class TestMutualExclusivity:
    def test_both_single_and_batch_rejected(self, session, sample_file):
        _mark_read(session, sample_file)
        result = session._prepare_edit_file(
            "c1",
            {
                "path": sample_file,
                "old_string": "line1",
                "new_string": "replaced",
                "edits": [
                    {"old_string": "line2", "new_string": "also_replaced"},
                ],
            },
        )
        assert result.get("error")
        assert "not both" in result["error"]

    def test_neither_single_nor_batch(self, session, sample_file):
        result = session._prepare_edit_file(
            "c1",
            {
                "path": sample_file,
            },
        )
        assert result.get("error")
        assert "old_string" in result["error"]

    def test_empty_edits_array_falls_through_to_single(self, session, sample_file):
        """An empty edits array should be treated as no batch."""
        result = session._prepare_edit_file(
            "c1",
            {
                "path": sample_file,
                "edits": [],
            },
        )
        # Falls through to single-edit path, which requires old_string
        assert result.get("error")
        assert "old_string" in result["error"]


# ── TOCTOU edge cases ───────────────────────────────────────────────


class TestExecEdgeCases:
    def test_file_changed_between_prepare_and_exec(self, session, sample_file):
        _mark_read(session, sample_file)
        result = session._prepare_edit_file(
            "c1",
            {
                "path": sample_file,
                "old_string": "line2",
                "new_string": "replaced",
            },
        )
        assert result["needs_approval"]

        # Modify the file after prepare
        with open(sample_file, "w") as f:
            f.write("completely different content\n")

        call_id, msg = session._exec_edit_file(result)
        assert "no longer found" in msg

    def test_file_deleted_between_prepare_and_exec(self, session, sample_file):
        _mark_read(session, sample_file)
        result = session._prepare_edit_file(
            "c1",
            {
                "path": sample_file,
                "old_string": "line2",
                "new_string": "replaced",
            },
        )
        assert result["needs_approval"]

        os.unlink(sample_file)

        call_id, msg = session._exec_edit_file(result)
        assert "Error" in msg

    def test_batch_file_changed_partial_match(self, session, sample_file):
        """If file changes so one edit fails, none should be applied."""
        _mark_read(session, sample_file)
        result = session._prepare_edit_file(
            "c1",
            {
                "path": sample_file,
                "edits": [
                    {"old_string": "line1", "new_string": "first"},
                    {"old_string": "line5", "new_string": "last"},
                ],
            },
        )
        assert result["needs_approval"]

        # Remove line5 between prepare and exec
        with open(sample_file, "w") as f:
            f.write("line1\nline2\nline3\nline4\n")

        call_id, msg = session._exec_edit_file(result)
        assert "no longer found" in msg
        # line1 should NOT have been edited (atomic failure)
        with open(sample_file) as f:
            assert "line1" in f.read()
