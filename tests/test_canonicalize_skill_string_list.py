"""Unit tests for ``_canonicalize_skill_string_list`` in console.server.

Backs the admin create/update handlers' wire-shape normalization for
JSON-array-string skill fields (``paths`` today; ``arguments`` once
#572 wires its consumer).  The interesting cases are the corruption
paths the regex / split previously took on ``None``/empty input —
without explicit None handling, ``str(None)`` slid through CSV-split
and stored the literal value ``["None"]``.
"""

from __future__ import annotations

from turnstone.console.server import _canonicalize_skill_string_list


class TestList:
    def test_list_of_strings(self) -> None:
        assert _canonicalize_skill_string_list(["**/*.py", "docs/**"]) == '["**/*.py", "docs/**"]'

    def test_list_trims_and_drops_blank(self) -> None:
        assert _canonicalize_skill_string_list(["  a  ", "", "b"]) == '["a", "b"]'

    def test_empty_list(self) -> None:
        assert _canonicalize_skill_string_list([]) == "[]"


class TestJsonString:
    def test_valid_json_array(self) -> None:
        assert _canonicalize_skill_string_list('["**/*.py", "docs/**"]') == '["**/*.py", "docs/**"]'

    def test_json_array_trims_elements(self) -> None:
        assert _canonicalize_skill_string_list('["  a  ", " ", "b"]') == '["a", "b"]'

    def test_malformed_json_array_collapses_to_empty(self) -> None:
        """``[``-prefixed unparseable input → empty array, not CSV-split."""
        assert _canonicalize_skill_string_list("[not-json") == "[]"

    def test_non_array_json_treated_as_csv(self) -> None:
        """A string that doesn't start with ``[`` is CSV input by contract,
        even if it happens to be valid JSON for some other shape.  No commas
        means a single-element list.  Pragmatic over strict — the admin UI
        round-trips through this helper and a typo doesn't need to error."""
        assert _canonicalize_skill_string_list('{"k": "v"}') == '["{\\"k\\": \\"v\\"}"]'


class TestCsvString:
    def test_comma_separated(self) -> None:
        assert (
            _canonicalize_skill_string_list("**/*.py, docs/**, src/api/**")
            == '["**/*.py", "docs/**", "src/api/**"]'
        )

    def test_csv_trims_and_drops_blank(self) -> None:
        assert _canonicalize_skill_string_list("a , , b ,") == '["a", "b"]'

    def test_single_value_no_comma(self) -> None:
        assert _canonicalize_skill_string_list("**/*.py") == '["**/*.py"]'


class TestNullAndEmpty:
    def test_none_returns_empty_array(self) -> None:
        """``None`` must NOT corrupt into ``'["None"]'`` (regression bug-1/bug-2)."""
        assert _canonicalize_skill_string_list(None) == "[]"

    def test_empty_string(self) -> None:
        assert _canonicalize_skill_string_list("") == "[]"

    def test_whitespace_only_string(self) -> None:
        assert _canonicalize_skill_string_list("   ") == "[]"

    def test_empty_json_array_string(self) -> None:
        assert _canonicalize_skill_string_list("[]") == "[]"
