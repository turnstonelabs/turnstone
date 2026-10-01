"""THIRD-PARTY-NOTICES matches the libraries the package vendors (#1234).

Each library and font the package vendors lives in a version-named directory
under turnstone/shared_static/ with its LICENSE file.  Its section in
THIRD-PARTY-NOTICES opens with its name and version, and reproduces that
LICENSE or names the file.  scripts/update-vendored-js.sh rewrites the header
when it updates a JavaScript library; these guards also cover the fonts, which
have no update script, updates made by hand, and a new upstream LICENSE.
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_NOTICES = _ROOT / "THIRD-PARTY-NOTICES"
_SHARED = _ROOT / "turnstone/shared_static"
# A vendored directory: a lowercase name, a hyphen and a dotted version.
_VERSIONED_DIR = re.compile(r"(?P<name>[a-z][a-z0-9_-]*)-(?P<version>\d+(?:\.\d+)+)")
# The name each vendored directory's section header gives, by directory name.
_NOTICE_NAMES = {
    "hljs": "highlight.js",
    "hls": "hls.js",
    "inter": "Inter",
    "jetbrains-mono": "JetBrains Mono",
    "katex": "KaTeX",
    "mermaid": "Mermaid",
}
# The line of "=" signs between sections; the text before the first is the
# preamble.
_SEPARATOR = re.compile(r"^=+$", re.MULTILINE)


def _sections() -> dict[str, str]:
    """Each section after the preamble, by its first line."""
    sections = _SEPARATOR.split(_NOTICES.read_text(encoding="utf-8"))[1:]
    return {section.strip().splitlines()[0]: section for section in sections}


def _vendored() -> dict[str, Path]:
    """Each version-named directory under shared_static/, by the header of the
    section it needs."""
    found = [
        (path, match)
        for path in sorted(_SHARED.iterdir())
        if path.is_dir() and (match := _VERSIONED_DIR.fullmatch(path.name))
    ]
    # An empty scan would let the checks below pass on an empty notices file.
    assert found
    unnamed = sorted({match["name"] for _, match in found} - _NOTICE_NAMES.keys())
    assert not unnamed, (
        f"vendored {unnamed}: add a THIRD-PARTY-NOTICES section and a _NOTICE_NAMES entry"
    )
    return {f"{_NOTICE_NAMES[match['name']]} {match['version']}": path for path, match in found}


def test_notices_name_the_version_of_each_vendored_directory() -> None:
    assert set(_sections()) == set(_vendored())


def test_notices_reproduce_or_name_each_shipped_license() -> None:
    sections = _sections()
    for header, directory in _vendored().items():
        license_file = directory / "LICENSE"
        section = sections.get(header, "")
        assert (
            license_file.read_text(encoding="utf-8").strip() in section
            or license_file.relative_to(_ROOT).as_posix() in section
        ), f"no {header} section reproduces or names {license_file.relative_to(_ROOT)}"
