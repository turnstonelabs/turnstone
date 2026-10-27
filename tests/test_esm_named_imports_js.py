"""Every named import in the browser panes' module graphs is exported by the module it names.

An ES module graph with one import of a name its target does not export fails to link,
and the browser then runs none of it: the whole pane goes blank, not just the feature
that used the name, and a bad import in any module the pane pulls in does the same. A
syntax check and the source-pattern pins elsewhere miss this.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests._js_harness_helpers import strip_js_comments

_ROOT = Path(__file__).resolve().parent.parent
_SHARED = _ROOT / "turnstone/shared_static"
# The pane modules whose graphs are walked; add a path here to extend the check.
_IMPORTERS = (
    _ROOT / "turnstone/console/static/coordinator/coordinator.js",
    _SHARED / "interactive.js",
)
_NAMED_IMPORT = re.compile(r'^import\s*\{([^}]*)\}\s*from\s*"([^"]+)"', re.MULTILINE)
_BARE_IMPORT = re.compile(r'^import\s*"([^"]+)"', re.MULTILINE)
# Every static import or re-export; each must parse as one of the two forms above, so a
# form this test does not read fails loudly instead of going unchecked.
_STATEMENT = re.compile(r"^(?:import\b|export\s*(?:\*[^;]*?|\{[^}]*\})\s*from\b)", re.MULTILINE)
_EXPORT_DECL = re.compile(
    r"^export\s+(?:async\s+)?(?:function\*?|class|const|let|var)\s+([A-Za-z_$][\w$]*)",
    re.MULTILINE,
)
_EXPORT_LIST = re.compile(r"^export\s*\{([^}]*)\}", re.MULTILINE)


def _source(path: Path) -> str:
    return strip_js_comments(path.read_text(encoding="utf-8"))


def _resolve(importer: Path, spec: str) -> Path:
    if spec.startswith("/shared/"):
        return _SHARED / spec.removeprefix("/shared/")
    if spec.startswith(("./", "../")):
        return (importer.parent / spec).resolve()
    raise AssertionError(f"{importer.name}: no rule resolves import specifier {spec!r}")


def _clause_names(clause: str, *, side: int) -> list[str]:
    """Names in an ``{ a, b as c }`` clause: ``side=0`` the left of each ``as``,
    ``side=-1`` the right (a bare name is both)."""
    return [part.split()[side] for part in clause.split(",") if part.strip()]


def _exports(module: Path) -> set[str]:
    src = _source(module)
    names = set(_EXPORT_DECL.findall(src))
    for clause in _EXPORT_LIST.findall(src):
        names.update(_clause_names(clause, side=-1))
    return names


@pytest.mark.parametrize("importer", _IMPORTERS, ids=lambda path: path.name)
def test_every_named_import_in_the_pane_graph_is_exported(importer: Path) -> None:
    assert _NAMED_IMPORT.findall(_source(importer)), (
        f"{importer.name}: no named imports found; the pattern no longer matches"
    )
    todo, seen, missing = [importer], set(), []
    while todo:
        module = todo.pop()
        if module in seen:
            continue
        seen.add(module)
        src = _source(module)
        named, bare = _NAMED_IMPORT.findall(src), _BARE_IMPORT.findall(src)
        assert len(_STATEMENT.findall(src)) == len(named) + len(bare), (
            f"{module.name}: an import or re-export form this test does not parse"
        )
        todo += [_resolve(module, spec) for spec in bare]
        for clause, spec in named:
            target = _resolve(module, spec)
            assert target.is_file(), f"{module.name}: {spec} names no file"
            todo.append(target)
            exported = _exports(target)
            assert exported, f"{spec}: no exports found; the export patterns no longer match"
            missing += [
                f"{name} ({module.name} -> {spec})"
                for name in _clause_names(clause, side=0)
                if name not in exported
            ]
    assert not missing, f"names imported but not exported: {missing}"
