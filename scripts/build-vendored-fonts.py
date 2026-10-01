#!/usr/bin/env python3
# /// script
# dependencies = ["fonttools[woff]==4.66.1", "brotli==1.2.0", "zopfli==0.4.3"]
# ///
"""Rebuild the UI fonts from checksum-pinned upstream release archives.

Download the three archives listed in SOURCES into one directory, then run::

    uv run --no-project scripts/build-vendored-fonts.py SOURCE_DIR

Changed bytes require a fresh immutable directory name, updated fonts.css URLs,
wheel includes, notices, architecture paths and test_ui_static_assets digests.
"""

from __future__ import annotations

import argparse
import hashlib
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

from fontTools import subset
from fontTools.ttLib import TTFont
from fontTools.varLib.instancer import instantiateVariableFont

ROOT = Path(__file__).resolve().parents[1] / "turnstone/shared_static"
# Preserve the previously shipped scripts and add punctuation, arrows, geometric
# shapes and marks. Layout closure retains alternates and ligatures as well.
TEXT_RANGES = (
    (0x0000, 0x052F),
    (0x1C80, 0x1DBF),
    (0x1E00, 0x27FF),
    (0x2C60, 0x2C7F),
    (0x2DE0, 0x2DFF),
    (0xA640, 0xA69F),
    (0xA720, 0xA7FF),
    (0xFE2E, 0xFE2F),
    (0xFEFF, 0xFEFF),
    (0xFFFD, 0xFFFD),
)
SOURCES = (
    (
        "https://github.com/rsms/inter/releases/download/v4.1/Inter-4.1.zip",
        "9883fdd4a49d4fb66bd8177ba6625ef9a64aa45899767dde3d36aa425756b11e",
        "inter-4.001.1",
        "LICENSE.txt",
        (
            ("InterVariable.ttf", "inter-normal.woff2", 900),
            ("InterVariable-Italic.ttf", "inter-italic.woff2", 900),
        ),
    ),
    (
        "https://github.com/JetBrains/JetBrainsMono/releases/download/v2.304/JetBrainsMono-2.304.zip",
        "6f6376c6ed2960ea8a963cd7387ec9d76e3f629125bc33d1fdcd7eb7012f7bbf",
        "jetbrains-mono-2.304",
        "OFL.txt",
        (
            ("fonts/variable/JetBrainsMono[wght].ttf", "jetbrains-mono-normal.woff2", 800),
            ("fonts/variable/JetBrainsMono-Italic[wght].ttf", "jetbrains-mono-italic.woff2", 800),
        ),
    ),
    (
        "https://github.com/dejavu-fonts/dejavu-fonts/releases/download/version_2_37/dejavu-sans-ttf-2.37.zip",
        "5c6e497a2f36552cb5ffb112c413a6af39c0f3c47653662b90b4fa6499822fd7",
        "dejavu-sans-2.37",
        "dejavu-sans-ttf-2.37/LICENSE",
        (("dejavu-sans-ttf-2.37/ttf/DejaVuSans.ttf", "symbols.woff2", None),),
    ),
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_dir", type=Path)
    args = parser.parse_args()
    artifacts: dict[Path, bytes] = {}
    for url, expected, directory, license_path, faces in SOURCES:
        path = args.source_dir / url.rsplit("/", 1)[-1]
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != expected:
            raise SystemExit(f"Source checksum mismatch: {path}")
        with ZipFile(BytesIO(data)) as archive:
            artifacts[ROOT / directory / "LICENSE"] = archive.read(license_path)
            for source, filename, maximum_weight in faces:
                font = TTFont(BytesIO(archive.read(source)), recalcTimestamp=False)
                ranges = TEXT_RANGES if maximum_weight else ((0x2190, 0x27FF),)
                options = subset.Options()
                options.layout_features = ["*"]
                options.name_IDs = ["*"]
                options.name_languages = ["*"]
                options.drop_tables += ["FFTM"]
                subsetter = subset.Subsetter(options=options)
                subsetter.populate(
                    unicodes={cp for start, end in ranges for cp in range(start, end + 1)}
                )
                subsetter.subset(font)
                if maximum_weight:
                    axes: dict[str, float | tuple[int, int, int]] = {
                        "wght": (400, 400, maximum_weight)
                    }
                    if any(axis.axisTag == "opsz" for axis in font["fvar"].axes):
                        axes["opsz"] = 14
                    instantiateVariableFont(font, axes, inplace=True)
                font.flavor = "woff2"
                output = BytesIO()
                font.save(output)
                artifacts[ROOT / directory / filename] = output.getvalue()
                font.close()
    for path, data in artifacts.items():
        if path.exists() and path.read_bytes() != data:
            raise SystemExit(f"Immutable asset would change: {path}; choose a fresh directory")
    for path, data in artifacts.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        print(f"{path.relative_to(ROOT)}: {len(data):,} bytes")


if __name__ == "__main__":
    main()
