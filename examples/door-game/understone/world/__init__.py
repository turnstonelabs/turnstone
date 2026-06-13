"""Content-pack loading — JSON on disk becomes a runtime ``World``."""

from pathlib import Path

# The bundled starter pack ("The Vale of Understone"). Single source of truth
# for where packaged content lives — the server's default world and the
# scaffolder's template both resolve here.
PACKAGED_WORLD_DIR = Path(__file__).resolve().parent / "data"
