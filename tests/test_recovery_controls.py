"""Fast guards for the browser harness's negative-control table (no browser, no server).

``scripts/recovery_e2e.py`` proves each scenario can still fail by serving a mutated copy of one
client module.  A control whose needle no longer occurs exactly as recorded is an error, and the
client code under the needles moves often, so the needle check runs here in the ordinary suite
instead of only when someone next runs the browser controls by hand.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from tests._sse_recovery_server import mutated_source

if TYPE_CHECKING:
    from collections.abc import Iterator

_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _ROOT / "scripts" / "recovery_e2e.py"


def _load_harness() -> Any:
    name = "recovery_e2e_controls_under_test"
    spec = importlib.util.spec_from_file_location(name, _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered before execution: dataclasses resolve their module through sys.modules.
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


@pytest.fixture(scope="module")
def harness() -> Iterator[Any]:
    module = _load_harness()
    yield module
    sys.modules.pop(module.__name__, None)


def test_every_control_needle_matches_the_tree(
    harness: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    errors = harness.check_controls()
    out = capsys.readouterr().out
    assert errors == 0, out


def test_a_stale_control_needle_is_an_error(harness: Any) -> None:
    control = harness.CONTROLS[0]
    stale = harness.SourceEdit(control.edit.served, "no such text in the module", "")
    with pytest.raises(ValueError, match="found 0"):
        harness._control_overrides(harness.Control(**{**vars(control), "edit": stale}))


def test_every_scenario_has_a_control_or_a_reason(harness: Any) -> None:
    controlled = {control.scenario for control in harness.CONTROLS}
    for scenario in harness.SCENARIOS:
        has_reason = scenario.uncontrolled is not None
        assert (scenario.name in controlled) != has_reason, scenario.name


def test_mutated_source_replaces_the_expected_count(tmp_path: Path) -> None:
    path = tmp_path / "module.js"
    path.write_text("a();\nb();\na();\n", encoding="utf-8")
    assert mutated_source(path, "b();\n", "") == b"a();\na();\n"
    assert mutated_source(path, "a();", "c();", count=2) == b"c();\nb();\nc();\n"


@pytest.mark.parametrize(
    ("needle", "count"),
    [
        ("missing();", 1),  # stale anchor: zero occurrences
        ("a();", 1),  # occurs twice, recorded once
        ("b();", 2),  # occurs once, recorded twice
    ],
)
def test_mutated_source_rejects_a_count_mismatch(tmp_path: Path, needle: str, count: int) -> None:
    path = tmp_path / "module.js"
    path.write_text("a();\nb();\na();\n", encoding="utf-8")
    with pytest.raises(ValueError, match="occurrence"):
        mutated_source(path, needle, "", count=count)


def test_mutated_source_rejects_a_zero_count(tmp_path: Path) -> None:
    path = tmp_path / "module.js"
    path.write_text("a();\n", encoding="utf-8")
    with pytest.raises(ValueError, match="at least 1"):
        mutated_source(path, "x", "", count=0)
