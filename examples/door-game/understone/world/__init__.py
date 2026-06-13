"""Content-pack loading — JSON on disk becomes a runtime ``World``."""

from __future__ import annotations

from pathlib import Path

# The bundled starter pack ("The Vale of Understone"). Single source of truth
# for where packaged content lives — the server's default world and the
# scaffolder's template both resolve here.
PACKAGED_WORLD_DIR = Path(__file__).resolve().parent / "data"

# Zero-or-more bundled ALTERNATE worlds live one directory deeper, each in its
# own ``<slug>/`` subdirectory carrying a ``world.json``. The Vale is special
# (it is the default and lives at ``data/``); alternates are discovered here.
PACKS_DIR = Path(__file__).resolve().parent / "packs"

# The reserved slug of the default Vale — it is never a packs/ subdirectory but
# is always listed first by the discovery helper below.
VALE_SLUG = "vale"


def bundled_world_dirs() -> list[tuple[str, Path]]:
    """Return every bundled world as ``(slug, directory)``, the Vale first.

    The default Vale (slug :data:`VALE_SLUG`, the ``data/`` directory) always
    leads; the alternates follow in slug-alphabetical order. An alternate is
    any immediate subdirectory of :data:`PACKS_DIR` that contains a
    ``world.json`` — non-pack files (the README placeholder) and directories
    without a world file are skipped, so the list is exactly the loadable
    worlds. This is the single discovery path the ``worlds`` listing and any
    future world resolver share.
    """
    found: list[tuple[str, Path]] = [(VALE_SLUG, PACKAGED_WORLD_DIR)]
    if PACKS_DIR.is_dir():
        alternates = [
            (entry.name, entry)
            for entry in PACKS_DIR.iterdir()
            if entry.is_dir() and (entry / "world.json").is_file()
        ]
        found.extend(sorted(alternates, key=lambda pair: pair[0]))
    return found
