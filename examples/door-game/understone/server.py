"""MCP server for Understone — the only module that imports ``mcp``.

Nine ``door_*`` tools form the entire player interface. Every handler is a
synchronous ``def`` that takes and returns ``str``; no exception is allowed
to cross the MCP boundary (each handler catches, logs server-side, and
returns an in-fiction line). The handlers are thin wrappers over a single
module-level :class:`~understone.game.Game`; all rules live behind that
façade.

Usage::

    understone                 # via entry point (stdio transport)
    python -m understone       # via module

Environment variables
---------------------
UNDERSTONE_DB         SQLite path (default: ./understone.db)
UNDERSTONE_WORLD      Content-pack directory (default: packaged world/data)
UNDERSTONE_TRANSPORT  "stdio" (default) or "streamable-http"
UNDERSTONE_HOST       Bind host for http transport (default: 127.0.0.1)
UNDERSTONE_PORT       Bind port for http transport (default: 8077)
UNDERSTONE_PATH       HTTP path for the MCP endpoint (default: /mcp)
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

from mcp.server.fastmcp import FastMCP

from understone.errors import WorldLoadError
from understone.game import Game
from understone.persistence import Store
from understone.world.loader import load_world

if TYPE_CHECKING:
    from starlette.applications import Starlette

log = logging.getLogger(__name__)

_PACKAGED_WORLD = Path(__file__).resolve().parent / "world" / "data"

_PREMISE = (
    "Understone is a shared-world BBS door game played through these tools. "
    "Call door_help first to learn how to narrate it."
)

# The DM manual returned by door_help — a module constant so it is stable and
# greppable. It teaches an assistant how to run the game responsibly.
_DM_MANUAL = """\
UNDERSTONE — A GUIDE FOR THE GAME MASTER

WHAT THIS IS
  Understone is a multiplayer, BBS-style ANSI door game — a small text RPG in
  the lineage of the classic BBS door games. Many players share ONE
  persistent world hosted by this server. You are the storyteller at the
  terminal; the server is the rules engine and the single source of truth.

THE GOLDEN RULE
  The server is authoritative. Never invent dice rolls, loot, gold, hit
  points, map tiles, or outcomes. Every number and event comes back from a
  tool call. Narrate AROUND the facts the tools return — never ahead of them.
  If you want something to happen, call the tool and see what the world says.

THE TWO MODES OF PLAY
  1. The overworld (TILE mode). Tools return an ASCII "keyframe": a bordered
     map window centred on the player. '@' is the player, '&' is another
     adventurer, letters are buildings (I inn, $ shop, + healer, > dungeon).
     Movement here is FREE — it costs no daily turns.
  2. Location interiors (MENU mode). Stepping onto a building opens a menu of
     options like (R)est, (B)uy, (H)eal, (D)escend, (L)eave.

PRESENTING FRAMES
  When a tool returns a map or a menu, show it to the player VERBATIM inside a
  fenced code block so the box-drawing lines stay aligned. Then add your prose
  underneath. Do not redraw or paraphrase the frame.

NARRATION
  Be vivid and in-fiction. Turn the terse result lines ("You travel 3 steps.",
  "+8 XP, +3 gold.") into atmosphere. Keep your additions consistent with the
  returned facts and the high-fantasy tone of the Vale of Understone.

THE DAILY RHYTHM
  Each adventurer has a small budget of turns per real-world UTC day. Only
  fighting and descending spend a turn; moving, resting, shopping and looking
  do not. When the budget is gone, the day is done — encourage the player to
  return tomorrow. This is a correspondence game: a little each day.

BESTOWING FORTUNE (use sparingly)
  door_bestow lets you, the storyteller, grant a little gold or healing to
  mark a great story moment — a heroic rescue, a clever solution, a poignant
  death-defiance. It NEVER grants items (gear comes from the shop) and NEVER
  grants turns (the clock does not bend). It is capped by a small daily pool
  per player, and every bestowal is written to the public log for all to see.
  Treat it as seasoning, not a salt-shaker: reserve it for the rare, earned
  beat, and never promise a reward you cannot actually deliver within the cap.

TOOL CHEAT-SHEET
  door_help                      This manual.
  door_join(player)              Sign in (creates or resumes a character).
  door_status(player)            Read the character sheet.
  door_look(player)              Redraw the current view (map or menu).
  door_move(player, ...)         Walk the overworld (free). steps="NNEE" or
                                 heading="east" + distance=3 (max 8 per call).
  door_action(player, action)    Context verb: fight, flee, rest, buy, sell,
                                 heal, descend, leave.
  door_log(player)               Catch up on what happened while away.
  door_rank(player)              The leaderboard.
  door_bestow(player, reason...) Grant a little gold/healing for a story beat.

GETTING STARTED
  Ask the player their adventurer's name, call door_join, present the opening
  keyframe, and set the scene: a small town at the western edge of a wooded
  vale, a road running east toward darker country and a dungeon mouth.
"""

_BLANK_NAME = 'The gatekeeper squints. "I didn\'t catch your name, traveller."'

# Module-level game singleton, built lazily so tests can inject their own.
_GAME: Game | None = None


def _build_game() -> Game:
    """Construct the module Game from environment configuration."""
    db_path = os.environ.get("UNDERSTONE_DB", "understone.db")
    world_dir = os.environ.get("UNDERSTONE_WORLD") or str(_PACKAGED_WORLD)
    world = load_world(world_dir)
    store = Store(db_path)
    return Game(world, store)


def _game() -> Game:
    """Return the module game, building it on first use."""
    global _GAME
    if _GAME is None:
        _GAME = _build_game()
    return _GAME


def _set_game(game: Game) -> None:
    """Install a prebuilt game (used by create_app / tests)."""
    global _GAME
    _GAME = game


def _guard_name(player: str) -> str | None:
    """Return the blank-name refusal when *player* is empty, else None."""
    return None if player.strip() else _BLANK_NAME


mcp: FastMCP = FastMCP(
    "understone",
    instructions=_PREMISE,
)


@mcp.tool()
def door_help() -> str:
    """Read the Understone game-master manual — start here.

    Returns a short guide for running this multiplayer, BBS-style ANSI door
    game (a classic text RPG / dungeon adventure): the two play modes, how
    to present the ASCII map frames, the daily-turn rhythm, and the full tool
    cheat-sheet. Call door_help before your first session to learn how to run
    the game, then call door_join to begin.
    """
    return _DM_MANUAL


@mcp.tool()
def door_join(player: str) -> str:
    """Sign an adventurer into the shared world of Understone — call this first.

    Understone is a multiplayer, BBS-style ANSI door game: a text adventure /
    dungeon RPG in the spirit of the classic BBS door games, played entirely
    through these tools. This creates a new character at the town, or resumes
    an existing one by name, and returns the opening overworld map frame. New
    to running it? Call door_help before your first session.

    Args:
        player: The adventurer's name (their identity in the world).
    """
    blank = _guard_name(player)
    if blank is not None:
        return blank
    try:
        return _game().join(player)
    except Exception:
        log.exception("door_join failed for %r", player)
        return _unexpected()


@mcp.tool()
def door_status(player: str) -> str:
    """Show an adventurer's character sheet (level, HP, gear, gold, turns).

    Read-only. Use it to check progress before deciding what to do next.

    Args:
        player: The adventurer's name.
    """
    blank = _guard_name(player)
    if blank is not None:
        return blank
    try:
        return _game().status(player)
    except Exception:
        log.exception("door_status failed for %r", player)
        return _unexpected()


@mcp.tool()
def door_look(player: str) -> str:
    """Redraw what the adventurer currently sees (read-only).

    On the overworld this is an ASCII map keyframe centred on the player
    ('@' is you, '&' are other players, letters are buildings). Inside a
    building it is that location's menu. Present the result verbatim in a
    fenced code block, then narrate.

    Args:
        player: The adventurer's name.
    """
    blank = _guard_name(player)
    if blank is not None:
        return blank
    try:
        return _game().look(player)
    except Exception:
        log.exception("door_look failed for %r", player)
        return _unexpected()


@mcp.tool()
def door_move(player: str, steps: str = "", heading: str = "", distance: int = 1) -> str:
    """Walk the overworld — movement is free and never costs a daily turn.

    Only valid on the overworld (in a building, use door_action 'leave'
    first). Give EITHER a compact ``steps`` string of cardinal letters such
    as "NNEE", OR a ``heading`` ("north"/"south"/"east"/"west") with a
    ``distance``. At most 8 cells move per call; the walk stops early at
    walls, water, a building door, or a wandering monster.

    Args:
        player: The adventurer's name.
        steps: Cardinal letters, e.g. "NNEE" (takes precedence if given).
        heading: A compass direction used with distance.
        distance: How many cells to travel along heading (1-8).
    """
    blank = _guard_name(player)
    if blank is not None:
        return blank
    try:
        return _game().move(player, steps, heading, distance)
    except Exception:
        log.exception("door_move failed for %r", player)
        return _unexpected()


@mcp.tool()
def door_action(player: str, action: str, target: str = "", item: str = "") -> str:
    """Take a context-sensitive action in the world.

    The legal verbs depend on where the adventurer is. On the overworld:
    'fight' or 'flee' a wandering monster (fighting spends one daily turn).
    Inside a building: 'rest' (inn), 'buy'/'sell' (shop), 'heal' (healer),
    'descend' (dungeon), or 'leave'. An illegal verb returns the list of
    verbs that are valid right here.

    Args:
        player: The adventurer's name.
        action: The verb to attempt (fight, flee, rest, buy, sell, heal,
            descend, leave).
        target: Reserved for future targeted actions.
        item: For shop 'buy', the item id to purchase.
    """
    blank = _guard_name(player)
    if blank is not None:
        return blank
    try:
        return _game().action(player, action, target, item)
    except Exception:
        log.exception("door_action failed for %r action=%r", player, action)
        return _unexpected()


@mcp.tool()
def door_log(player: str) -> str:
    """Catch up on what happened in the shared world while the player was away.

    Returns the events since this adventurer last checked (fights, deaths,
    blessings, descents by anyone) and advances their personal marker.

    Args:
        player: The adventurer's name.
    """
    blank = _guard_name(player)
    if blank is not None:
        return blank
    try:
        return _game().log(player)
    except Exception:
        log.exception("door_log failed for %r", player)
        return _unexpected()


@mcp.tool()
def door_rank(player: str = "") -> str:
    """Show the Roll of Heroes — the top-ten leaderboard.

    Ordered by level, then experience. If the caller names themselves and
    they place in the top ten, their row is marked.

    Args:
        player: The caller's name (optional; marks their row when present).
    """
    try:
        return _game().rank(player)
    except Exception:
        log.exception("door_rank failed for %r", player)
        return _unexpected()


@mcp.tool()
def door_bestow(player: str, reason: str, gold: int = 0, heal: int = 0) -> str:
    """Grant a small gift of gold or healing to mark a great story moment.

    This is the game master's discretionary channel, to be used SPARINGLY for
    earned, story-driven beats. It grants only gold and/or healing — never
    items (gear comes from the shop) and never turns (the daily clock does not
    bend). Each grant is capped by a small daily pool per adventurer and is
    written to the public log, so spend it on the rare moment that deserves
    it; do not promise more than the cap allows. When a pack sets healing to
    cost nothing, a bestowed heal is free and draws nothing from the pool.

    Args:
        player: The adventurer receiving the gift.
        reason: A short, in-fiction reason (<= 120 characters).
        gold: Gold to grant (>= 0).
        heal: HP to restore (>= 0); only the missing portion is applied and
            charged against the pool.
    """
    blank = _guard_name(player)
    if blank is not None:
        return blank
    try:
        return _game().bestow(player, reason, gold, heal)
    except Exception:
        log.exception("door_bestow failed for %r", player)
        return _unexpected()


def _unexpected() -> str:
    """In-fiction line for an unexpected server-side error."""
    return (
        "A strange fog rolls through the Vale and the moment slips away. "
        "(Something went wrong; try again.)"
    )


def create_app(db_path: str, world_dir: str | None = None) -> Starlette:
    """Build the streamable-HTTP ASGI app backed by a fresh game.

    Used by both ``main`` (for the http transport) and the integration tests,
    so tests can point at a temp DB without environment juggling.
    """
    world = load_world(world_dir or str(_PACKAGED_WORLD))
    store = Store(db_path)
    _set_game(Game(world, store))
    return mcp.streamable_http_app()


def main() -> None:
    """Run the Understone MCP server.

    Honours UNDERSTONE_TRANSPORT: "stdio" (default) or "streamable-http".
    For http, host/port/path are read from the environment and applied to the
    FastMCP settings before serving.
    """
    logging.basicConfig(level=logging.INFO)
    transport = os.environ.get("UNDERSTONE_TRANSPORT", "stdio")

    if transport == "streamable-http":
        mcp.settings.host = os.environ.get("UNDERSTONE_HOST", "127.0.0.1")
        mcp.settings.port = int(os.environ.get("UNDERSTONE_PORT", "8077"))
        mcp.settings.streamable_http_path = os.environ.get("UNDERSTONE_PATH", "/mcp")
        mcp.settings.stateless_http = False
        # Build the game eagerly so a config error surfaces before serving.
        _game()
        try:
            mcp.run(transport="streamable-http")
        except WorldLoadError as exc:
            raise SystemExit(f"failed to load world: {exc}") from exc
        return

    try:
        _game()
    except WorldLoadError as exc:
        raise SystemExit(f"failed to load world: {exc}") from exc
    mcp.run(transport="stdio")
