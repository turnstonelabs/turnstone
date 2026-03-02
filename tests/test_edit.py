"""Tests for turnstone.core.edit — find_occurrences and pick_nearest."""

from turnstone.core.edit import find_occurrences, pick_nearest


class TestFindOccurrences:
    def test_empty_old_string_returns_empty(self):
        assert find_occurrences("hello world", "") == []

    def test_no_match_returns_empty(self):
        assert find_occurrences("hello world", "xyz") == []

    def test_single_occurrence(self):
        content = "line one\nline two\nline three"
        assert find_occurrences(content, "two") == [2]

    def test_multiple_occurrences(self):
        content = "aaa\nbbb\naaa\nccc\naaa"
        assert find_occurrences(content, "aaa") == [1, 3, 5]

    def test_occurrence_on_first_line(self):
        content = "hello\nworld"
        assert find_occurrences(content, "hello") == [1]

    def test_multiline_match(self):
        content = "start\nfoo\nbar\nend"
        assert find_occurrences(content, "foo\nbar") == [2]

    def test_overlapping_positions(self):
        content = "aaa"
        # "aa" occurs at index 0 (line 1) and index 1 (line 1)
        assert find_occurrences(content, "aa") == [1, 1]

    def test_empty_content(self):
        assert find_occurrences("", "hello") == []


class TestPickNearest:
    def test_returns_char_index(self):
        content = "aaa\nbbb\nccc"
        idx = pick_nearest(content, "bbb", 2)
        assert idx == 4  # "bbb" starts at char index 4

    def test_picks_nearest_to_target_line(self):
        content = "xxx\naaa\nbbb\nccc\naaa\nddd"
        # "aaa" on line 2 (idx=4) and line 5 (idx=16)
        # Near line 5 should pick second occurrence
        idx = pick_nearest(content, "aaa", 5)
        assert content[idx : idx + 3] == "aaa"
        assert idx == 16

    def test_picks_nearest_first_occurrence(self):
        content = "xxx\naaa\nbbb\nccc\naaa\nddd"
        # Near line 1 should pick first occurrence
        idx = pick_nearest(content, "aaa", 1)
        assert idx == 4

    def test_no_match_returns_negative(self):
        content = "hello world"
        assert pick_nearest(content, "xyz", 1) == -1
