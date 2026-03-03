"""Tests for turnstone.eval — score_run and _match_action."""

from turnstone.eval import _match_action, score_run


class TestMatchAction:
    def test_tool_name_match(self):
        actual = {"tool": "bash", "args": {"command": "ls"}}
        expected = {"tool": "bash"}
        assert _match_action(actual, expected) is True

    def test_tool_name_mismatch(self):
        actual = {"tool": "bash", "args": {"command": "ls"}}
        expected = {"tool": "read_file"}
        assert _match_action(actual, expected) is False

    def test_exact_args_match(self):
        actual = {"tool": "bash", "args": {"command": "ls -la"}}
        expected = {"tool": "bash", "args": {"command": "ls -la"}}
        assert _match_action(actual, expected) is True

    def test_partial_key_matching(self):
        # Expected only specifies a subset of actual args
        actual = {"tool": "bash", "args": {"command": "ls", "extra": "val"}}
        expected = {"tool": "bash", "args": {"command": "ls"}}
        assert _match_action(actual, expected) is True

    def test_args_value_mismatch(self):
        actual = {"tool": "bash", "args": {"command": "ls"}}
        expected = {"tool": "bash", "args": {"command": "pwd"}}
        assert _match_action(actual, expected) is False

    def test_args_missing_key(self):
        actual = {"tool": "bash", "args": {"command": "ls"}}
        expected = {"tool": "bash", "args": {"path": "/tmp"}}
        assert _match_action(actual, expected) is False

    def test_args_pattern_regex_match(self):
        actual = {"tool": "bash", "args": {"command": "git log -5"}}
        expected = {"tool": "bash", "args_pattern": {"command": r"git\s+log"}}
        assert _match_action(actual, expected) is True

    def test_args_pattern_regex_mismatch(self):
        actual = {"tool": "bash", "args": {"command": "ls -la"}}
        expected = {"tool": "bash", "args_pattern": {"command": r"^git"}}
        assert _match_action(actual, expected) is False

    def test_raw_fallback_no_expected_args(self):
        actual = {"tool": "bash", "args": {"_raw": "something"}}
        expected = {"tool": "bash"}
        assert _match_action(actual, expected) is True

    def test_raw_fallback_with_expected_args(self):
        actual = {"tool": "bash", "args": {"_raw": "something"}}
        expected = {"tool": "bash", "args": {"command": "ls"}}
        assert _match_action(actual, expected) is False


class TestScoreRun:
    def test_empty_expected_actions_passes(self):
        result = score_run([{"tool": "bash", "args": {}}], [])
        assert result["pass"] is True
        assert result["score"] == 1.0

    def test_ordered_subset_all_match(self):
        tool_log = [
            {"tool": "read_file", "args": {"path": "a.py"}},
            {"tool": "bash", "args": {"command": "ls"}},
            {"tool": "edit_file", "args": {"path": "a.py"}},
        ]
        expected = [
            {"tool": "read_file"},
            {"tool": "edit_file"},
        ]
        result = score_run(tool_log, expected, match_mode="ordered_subset")
        assert result["pass"] is True
        assert result["score"] == 1.0

    def test_ordered_subset_wrong_order(self):
        tool_log = [
            {"tool": "edit_file", "args": {"path": "a.py"}},
            {"tool": "read_file", "args": {"path": "a.py"}},
        ]
        expected = [
            {"tool": "read_file"},
            {"tool": "edit_file"},
        ]
        result = score_run(tool_log, expected, match_mode="ordered_subset")
        # edit_file comes before read_file, so only one can match
        assert result["pass"] is False
        assert result["score"] == 0.5

    def test_exact_mode_pass(self):
        tool_log = [
            {"tool": "bash", "args": {"command": "ls"}},
            {"tool": "read_file", "args": {"path": "a.py"}},
        ]
        expected = [
            {"tool": "bash"},
            {"tool": "read_file"},
        ]
        result = score_run(tool_log, expected, match_mode="exact")
        assert result["pass"] is True
        assert result["score"] == 1.0

    def test_exact_mode_length_mismatch(self):
        tool_log = [
            {"tool": "bash", "args": {"command": "ls"}},
            {"tool": "read_file", "args": {"path": "a.py"}},
            {"tool": "edit_file", "args": {"path": "a.py"}},
        ]
        expected = [
            {"tool": "bash"},
            {"tool": "read_file"},
        ]
        result = score_run(tool_log, expected, match_mode="exact")
        # Length mismatch: 3 vs 2, so pass=False even though first 2 match
        assert result["pass"] is False

    def test_subset_mode_unordered(self):
        tool_log = [
            {"tool": "edit_file", "args": {"path": "a.py"}},
            {"tool": "read_file", "args": {"path": "a.py"}},
        ]
        expected = [
            {"tool": "read_file"},
            {"tool": "edit_file"},
        ]
        result = score_run(tool_log, expected, match_mode="subset")
        assert result["pass"] is True
        assert result["score"] == 1.0

    def test_contains_any_mode_pass(self):
        tool_log = [
            {"tool": "bash", "args": {"command": "ls"}},
            {"tool": "read_file", "args": {"path": "a.py"}},
        ]
        expected = [
            {"tool": "read_file"},
        ]
        result = score_run(tool_log, expected, match_mode="contains_any")
        assert result["pass"] is True
        assert result["score"] == 1.0

    def test_contains_any_mode_fail(self):
        tool_log = [
            {"tool": "bash", "args": {"command": "ls"}},
        ]
        expected = [
            {"tool": "read_file"},
        ]
        result = score_run(tool_log, expected, match_mode="contains_any")
        assert result["pass"] is False
        assert result["score"] == 0.0

    def test_score_partial(self):
        tool_log = [
            {"tool": "bash", "args": {"command": "ls"}},
        ]
        expected = [
            {"tool": "bash"},
            {"tool": "read_file"},
        ]
        result = score_run(tool_log, expected, match_mode="ordered_subset")
        assert result["score"] == 0.5
        assert len(result["unmatched"]) == 1
