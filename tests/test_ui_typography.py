"""Check the font contents behind the UI's typography requests (#1231)."""

from __future__ import annotations

import re
from functools import cache
from html import unescape
from pathlib import Path

import pytest
from fontTools.ttLib import TTFont

_ROOT = Path(__file__).resolve().parent.parent
_SHARED = _ROOT / "turnstone/shared_static"
_BASE = (_SHARED / "base.css").read_text(encoding="utf-8")
_FACES = tuple(
    dict(re.findall(r"([a-z-]+):\s*([^;]+);", block))
    for block in re.findall(r"@font-face\s*\{([^}]+)\}", (_SHARED / "fonts.css").read_text())
)


def _family(face: dict[str, str]) -> str:
    return face["font-family"].strip('"')


def _path(face: dict[str, str]) -> Path:
    match = re.search(r'url\("/shared/([^"]+)"\)', face["src"])
    assert match, face
    return _SHARED / match[1]


@cache
def _cmap(path: Path) -> set[int]:
    with TTFont(path) as font:
        return set(font.getBestCmap())


def _coverage(face: dict[str, str]) -> set[int]:
    cmap = _cmap(_path(face))
    if "unicode-range" not in face:
        return cmap
    declared: set[int] = set()
    for item in face["unicode-range"].split(","):
        endpoints = item.strip().removeprefix("U+").split("-")
        declared.update(range(int(endpoints[0], 16), int(endpoints[-1], 16) + 1))
    return cmap & declared


def _text_faces(family: str, style: str) -> list[dict[str, str]]:
    return [
        face
        for face in _FACES
        if _family(face) == family and face["font-style"] == style and ord("a") in _coverage(face)
    ]


@pytest.mark.parametrize(
    ("family", "features"),
    [("Inter", {"ss01", "cv11"}), ("JetBrains Mono", {"zero", "ss01"})],
)
def test_requested_features_have_live_substitutions(family: str, features: set[str]) -> None:
    faces = _text_faces(family, "normal")
    assert faces, family
    for face in faces:
        with TTFont(_path(face)) as font:
            active = {
                record.FeatureTag
                for record in font["GSUB"].table.FeatureList.FeatureRecord
                if record.Feature.LookupListIndex
            }
        assert features <= active, f"{family} lacks {features - active}"


@pytest.mark.parametrize("family", ["Inter", "JetBrains Mono"])
def test_italic_text_uses_an_italic_font(family: str) -> None:
    faces = _text_faces(family, "italic")
    assert faces, f"{family} needs an italic face"
    for face in faces:
        with TTFont(_path(face)) as font:
            assert font["post"].italicAngle != 0
            assert font["OS/2"].fsSelection & 1


def test_attention_chip_weights_exist_in_the_monospace_faces() -> None:
    css = (_SHARED / "conversation.css").read_text(encoding="utf-8")
    weights = []
    for selector in ["conv-agent-context--warning", "conv-agent-step-issue"]:
        block = re.search(r"\." + selector + r"\s*\{([^}]+)\}", css)
        assert block, selector
        weight = re.search(r"font-weight:\s*(\d+)", block[1])
        assert weight, selector
        weights.append(int(weight[1]))
    for style in ["normal", "italic"]:
        faces = _text_faces("JetBrains Mono", style)
        assert faces, style
        for face in faces:
            declared = [int(weight) for weight in face["font-weight"].split()]
            with TTFont(_path(face)) as font:
                axis = next(axis for axis in font["fvar"].axes if axis.axisTag == "wght")
                assert axis.minValue <= min(declared) <= max(declared) <= axis.maxValue
                for weight in weights:
                    assert min(declared) <= weight <= max(declared)


@pytest.mark.parametrize("family", ["Inter", "JetBrains Mono"])
@pytest.mark.parametrize("style", ["normal", "italic"])
def test_text_faces_keep_language_coverage(family: str, style: str) -> None:
    # Latin extensions, Cyrillic, Greek and Vietnamese from the previous builds.
    sample = "ÀéñøßŁőœȘčğİЖёжйЄҐђљЃΩάέήίόύώĐăơưộếỹ"
    covered = set().union(
        *(
            _coverage(face)
            for face in _FACES
            if _family(face) == family and face["font-style"] == style
        )
    )
    missing = set(map(ord, sample)) - covered
    assert not missing, f"{family} {style} lacks {''.join(chr(cp) for cp in sorted(missing))}"


def _ui_symbols() -> set[int]:
    sources = list(_SHARED.glob("*"))
    for directory in ["turnstone/ui/static", "turnstone/console/static"]:
        sources.extend((_ROOT / directory).rglob("*"))
    symbols: set[int] = set()
    unicode_escape = re.compile(re.escape(chr(92)) + r"u(?:([0-9A-Fa-f]{4})|\{([0-9A-Fa-f]+)\})")
    for source in sources:
        if source.suffix not in {".js", ".css", ".html"}:
            continue
        text = unescape(source.read_text(encoding="utf-8"))
        codepoints = set(map(ord, text))
        codepoints.update(int(match[1] or match[2], 16) for match in unicode_escape.finditer(text))
        symbols.update(cp for cp in codepoints if 0x2190 <= cp <= 0x27FF)
    # These status/admonition markers have default emoji presentation and use
    # the platform's color emoji font, like the UI's other emoji.
    symbols.difference_update({0x23F3, 0x26D4, 0x2757})
    assert {0x2192, 0x21BB, 0x25B8, 0x25BE, 0x25D0, 0x2699, 0x26A0, 0x2715} <= symbols
    return symbols


@pytest.mark.parametrize("token", ["ui", "mono"])
def test_ui_symbols_have_a_bundled_font_in_each_stack(token: str) -> None:
    declaration = re.search(r"--font-" + token + r":\s*([^;]+);", _BASE)
    assert declaration, token
    stack = set(re.findall(r'"([^"]+)"', declaration[1]))
    covered = set().union(
        *(
            _coverage(face)
            for face in _FACES
            if _family(face) in stack and face["font-style"] == "normal"
        )
    )
    missing = _ui_symbols() - covered
    assert not missing, (
        f"--font-{token} uses system symbols: {','.join(f'U+{cp:04X}' for cp in sorted(missing))}"
    )
