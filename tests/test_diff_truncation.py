"""Honest bounded diff output regressions."""

from __future__ import annotations

from tests._session_helpers import make_session
from turnstone.core.truncation import ProjectedText


def _write_pair(tmp_path, lines=1000):
    before = tmp_path / "before.txt"
    after = tmp_path / "after.txt"
    before.write_text("".join(f"old-{i:04d}\n" for i in range(lines)))
    after.write_text("".join(f"new-{i:04d}\n" for i in range(lines)))
    return {
        "call_id": "diff",
        "path_a": str(before),
        "path_b": str(after),
        "content_b": None,
        "context_lines": 3,
    }


def test_diff_executor_returns_the_complete_diff_within_the_retention(tmp_path) -> None:
    """The executor never cuts a diff the retention can hold: the fold is the
    single cut, and its marker reports the diff's real size."""
    session = make_session()
    session.tool_truncation = 60_000

    _, output = session._exec_diff(_write_pair(tmp_path))

    assert not isinstance(output, ProjectedText)
    assert 20_000 < len(output) <= session._executor_capture_chars()
    assert "chars truncated" not in output
    assert "new-0999" in output
    result = session._truncate_output_result(output, maximum_chars=300)
    assert len(result.text) <= 300
    assert result.original_chars == len(output)
    assert "chars truncated" in result.text
    assert "new-0999" in result.text


def test_diff_streaming_retention_carries_true_size_through_a_smaller_fold_cut(
    tmp_path,
) -> None:
    session = make_session()
    session.tool_truncation = 600
    retention = session._executor_capture_chars()

    _, output = session._exec_diff(_write_pair(tmp_path, lines=4000))

    # The executor's rendering is bounded and honest, and it still carries its
    # source edges plus the true size.
    assert isinstance(output, ProjectedText)
    assert len(output) <= retention
    assert output.source.original_chars > 2 * retention
    assert "chars truncated" in output
    assert "new-3999" in output
    # A smaller fold cut renders from the edges, so its marker reports omission
    # against the real diff rather than against the 300-character rendering.
    fold = session._truncate_output_result(output, maximum_chars=150)
    assert len(fold.text) <= 150
    assert fold.original_chars == output.source.original_chars
    assert fold.retained_chars + fold.omitted_chars == fold.original_chars
    assert f"[{fold.omitted_chars} chars truncated" in fold.text
    assert "new-3999" in fold.text


def test_identical_diff_stays_explicit(tmp_path) -> None:
    path = tmp_path / "same.txt"
    path.write_text("same\n")
    session = make_session()

    _, output = session._exec_diff(
        {
            "call_id": "diff",
            "path_a": str(path),
            "path_b": str(path),
            "content_b": None,
            "context_lines": 3,
        }
    )

    assert output == "(no differences)"
