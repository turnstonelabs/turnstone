"""SQLite persistence — the only storage layer, ``sqlite3`` only.

A single connection is held for the process lifetime. MCP tool handlers are
synchronous and run on one event-loop thread, so writes serialise naturally
and no connection pool or lock is needed [VERIFIED: handlers are sync def].
WAL journaling is enabled so reads never block the single writer.

The store loads all players and recent events into memory at construction
(a write-through cache). State-changing tools update the cache and the DB in
one transaction; the game façade owns the per-action commit policy.

The connection is opened with ``check_same_thread=False`` because the Store
may be CONSTRUCTED on a different thread than the event-loop thread that later
serves tools (both the test fixture and ``main`` do this). Post-construction
access is single-threaded: sync tools run inline on the loop [verified against
mcp 1.27 func_metadata], so writes still serialise without a lock.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

from understone.engine.log import Event
from understone.engine.models import Mode, Player
from understone.engine.rank import HallEntry, RankEntry

if TYPE_CHECKING:
    from pathlib import Path

# Pre-1.0 the schema mutates in place and the stamp is not yet meaningful;
# version discipline (and migrations) begins at 1.0.
_SCHEMA_VERSION = 1

# How many of the newest events to hydrate at construction. Full history stays
# in SQLite; this bounds the in-memory tail. Single source of truth — game.py
# imports it for the runtime trim, so the load size and the trim size cannot
# diverge. An ops knob (memory ceiling), never an economy value.
EVENT_TAIL_KEEP = 500

_PLAYER_COLUMNS = (
    "name",
    "x",
    "y",
    "hp",
    "max_hp",
    "level",
    "xp",
    "gold",
    "atk",
    "def_",
    "weapon_id",
    "armor_id",
    "turns_left",
    "turn_day",
    "mode",
    "at_location",
    "created_at",
    "last_seen",
    "log_cursor",
    "bestow_spent",
    "bestow_day",
    "wins",
    "posts_sent",
    "post_day",
    "gambles",
    "gamble_day",
    "deepest_rung",
    "satchel",
    "weapon_plus",
    "armor_plus",
    "banked",
)


class Store:
    """A write-through SQLite store for players and the shared event log."""

    def __init__(self, db_path: str | Path) -> None:
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    # -- schema ----------------------------------------------------------

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS players (
                name        TEXT PRIMARY KEY,
                x           INTEGER NOT NULL,
                y           INTEGER NOT NULL,
                hp          INTEGER NOT NULL,
                max_hp      INTEGER NOT NULL,
                level       INTEGER NOT NULL,
                xp          INTEGER NOT NULL,
                gold        INTEGER NOT NULL,
                atk         INTEGER NOT NULL,
                def_        INTEGER NOT NULL,
                weapon_id   TEXT NOT NULL,
                armor_id    TEXT NOT NULL,
                turns_left  INTEGER NOT NULL,
                turn_day    INTEGER NOT NULL,
                mode        TEXT NOT NULL,
                at_location TEXT NOT NULL,
                created_at  TEXT NOT NULL,
                last_seen   TEXT NOT NULL,
                log_cursor  INTEGER NOT NULL,
                bestow_spent INTEGER NOT NULL,
                bestow_day  INTEGER NOT NULL,
                wins        INTEGER NOT NULL DEFAULT 0,
                posts_sent  INTEGER NOT NULL DEFAULT 0,
                post_day    INTEGER NOT NULL DEFAULT 0,
                gambles     INTEGER NOT NULL DEFAULT 0,
                gamble_day  INTEGER NOT NULL DEFAULT 0,
                deepest_rung INTEGER NOT NULL DEFAULT 0,
                satchel     TEXT NOT NULL DEFAULT '',
                weapon_plus INTEGER NOT NULL DEFAULT 0,
                armor_plus  INTEGER NOT NULL DEFAULT 0,
                banked      INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS events (
                id     INTEGER PRIMARY KEY AUTOINCREMENT,
                ts     TEXT NOT NULL,
                actor  TEXT NOT NULL,
                kind   TEXT NOT NULL,
                text   TEXT NOT NULL,
                target TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS ambushes (
                attacker TEXT NOT NULL,
                target   TEXT NOT NULL,
                day      INTEGER NOT NULL,
                PRIMARY KEY (attacker, target, day)
            );

            CREATE TABLE IF NOT EXISTS hall_of_fame (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                name         TEXT NOT NULL,
                win_ts       TEXT NOT NULL,
                run_days     INTEGER NOT NULL,
                level_at_win INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS meta (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        self._conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
            (str(_SCHEMA_VERSION),),
        )
        self._conn.commit()

    def set_meta(self, key: str, value: str) -> None:
        """Upsert a meta key (e.g. ``world_name``) and commit."""
        self._conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self._conn.commit()

    def get_meta(self, key: str) -> str | None:
        """Return a meta value, or ``None`` if unset."""
        row = self._conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    # -- load ------------------------------------------------------------

    def load_all(self) -> tuple[dict[str, Player], list[Event]]:
        """Load every player and the most recent events into memory.

        Only the newest ``EVENT_TAIL_KEEP`` events are resident; the full history
        remains in SQLite. The tail is fetched newest-first then reversed so
        the returned list stays ascending by id (the order ``since`` expects).
        """
        players = {
            row["name"]: _row_to_player(row) for row in self._conn.execute("SELECT * FROM players")
        }
        rows = self._conn.execute(
            "SELECT * FROM events ORDER BY id DESC LIMIT ?", (EVENT_TAIL_KEEP,)
        ).fetchall()
        events = [_row_to_event(row) for row in reversed(rows)]
        return players, events

    # -- writes (no commit here; the façade commits per action) ----------

    def upsert_player(self, player: Player) -> None:
        """Insert or update a player row (no commit)."""
        placeholders = ", ".join("?" for _ in _PLAYER_COLUMNS)
        assignments = ", ".join(f"{col}=excluded.{col}" for col in _PLAYER_COLUMNS if col != "name")
        self._conn.execute(
            f"INSERT INTO players ({', '.join(_PLAYER_COLUMNS)}) VALUES ({placeholders}) "
            f"ON CONFLICT(name) DO UPDATE SET {assignments}",
            _player_to_row(player),
        )

    def insert_event(self, ts: str, actor: str, kind: str, text: str, target: str = "") -> int:
        """Append an event row (no commit) and return its new id.

        ``target`` is empty for a public event or a player name for a private
        note that only that player reads in their own catch-up.
        """
        cur = self._conn.execute(
            "INSERT INTO events(ts, actor, kind, text, target) VALUES(?, ?, ?, ?, ?)",
            (ts, actor, kind, text, target),
        )
        return int(cur.lastrowid or 0)

    def insert_hall_row(self, name: str, win_ts: str, run_days: int, level_at_win: int) -> int:
        """Append a Hall of Legends row (no commit) and return its new id."""
        cur = self._conn.execute(
            "INSERT INTO hall_of_fame(name, win_ts, run_days, level_at_win) VALUES(?, ?, ?, ?)",
            (name, win_ts, run_days, level_at_win),
        )
        return int(cur.lastrowid or 0)

    def record_ambush(self, attacker: str, target: str, day: int) -> None:
        """Mark that *attacker* has spent their ambush on *target* for *day*.

        Idempotent: the ``(attacker, target, day)`` primary key means a repeat
        write is ignored, so re-recording the same attempt is harmless. No
        commit — the façade folds this into the per-action transaction.
        """
        self._conn.execute(
            "INSERT OR IGNORE INTO ambushes(attacker, target, day) VALUES(?, ?, ?)",
            (attacker, target, day),
        )

    def has_ambushed(self, attacker: str, target: str, day: int) -> bool:
        """Return whether *attacker* already ambushed *target* on *day*."""
        row = self._conn.execute(
            "SELECT 1 FROM ambushes WHERE attacker=? AND target=? AND day=?",
            (attacker, target, day),
        ).fetchone()
        return row is not None

    def commit(self) -> None:
        """Commit the current transaction."""
        self._conn.commit()

    # -- read-only queries ----------------------------------------------

    def top_ranks(self, limit: int = 10) -> list[RankEntry]:
        """Return the leaderboard ordered by level, xp, then name."""
        rows = self._conn.execute(
            "SELECT name, level, xp, gold, wins FROM players "
            "ORDER BY level DESC, xp DESC, name ASC LIMIT ?",
            (limit,),
        )
        return [
            RankEntry(
                name=row["name"],
                level=row["level"],
                xp=row["xp"],
                gold=row["gold"],
                wins=row["wins"],
            )
            for row in rows
        ]

    def top_hall(self, limit: int = 5) -> list[HallEntry]:
        """Return the most recent Hall of Legends rows, newest first."""
        rows = self._conn.execute(
            "SELECT name, win_ts, run_days, level_at_win FROM hall_of_fame "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        return [
            HallEntry(
                name=row["name"],
                win_ts=row["win_ts"],
                run_days=row["run_days"],
                level_at_win=row["level_at_win"],
            )
            for row in rows
        ]

    def targeted_events_since(self, viewer: str, cursor: int) -> list[Event]:
        """Return *viewer*'s private notes past *cursor*, ascending by id.

        Public history older than the resident tail is ephemeral by design (the
        broadsheet does not keep), but private mail is durable: a note left while
        the recipient was away must survive however many public events have since
        pushed it out of the in-memory tail. The façade pulls the recipient's
        targeted rows from SQLite to backfill that gap before rendering.
        """
        rows = self._conn.execute(
            "SELECT * FROM events WHERE target=? AND id>? ORDER BY id",
            (viewer, cursor),
        ).fetchall()
        return [_row_to_event(row) for row in rows]

    def journal_mode(self) -> str:
        """Return the active journal mode (for diagnostics / tests)."""
        row = self._conn.execute("PRAGMA journal_mode").fetchone()
        return str(row[0])

    def close(self) -> None:
        """Close the underlying connection."""
        self._conn.close()


def _player_to_row(player: Player) -> tuple[object, ...]:
    return (
        player.name,
        player.x,
        player.y,
        player.hp,
        player.max_hp,
        player.level,
        player.xp,
        player.gold,
        player.atk,
        player.def_,
        player.weapon_id,
        player.armor_id,
        player.turns_left,
        player.turn_day,
        str(player.mode),
        player.at_location,
        player.created_at,
        player.last_seen,
        player.log_cursor,
        player.bestow_spent,
        player.bestow_day,
        player.wins,
        player.posts_sent,
        player.post_day,
        player.gambles,
        player.gamble_day,
        player.deepest_rung,
        player.satchel,
        player.weapon_plus,
        player.armor_plus,
        player.banked,
    )


def _row_to_player(row: sqlite3.Row) -> Player:
    return Player(
        name=row["name"],
        x=row["x"],
        y=row["y"],
        hp=row["hp"],
        max_hp=row["max_hp"],
        level=row["level"],
        xp=row["xp"],
        gold=row["gold"],
        atk=row["atk"],
        def_=row["def_"],
        weapon_id=row["weapon_id"],
        armor_id=row["armor_id"],
        turns_left=row["turns_left"],
        turn_day=row["turn_day"],
        mode=Mode(row["mode"]),
        at_location=row["at_location"],
        created_at=row["created_at"],
        last_seen=row["last_seen"],
        log_cursor=row["log_cursor"],
        bestow_spent=row["bestow_spent"],
        bestow_day=row["bestow_day"],
        wins=row["wins"],
        posts_sent=row["posts_sent"],
        post_day=row["post_day"],
        gambles=row["gambles"],
        gamble_day=row["gamble_day"],
        deepest_rung=row["deepest_rung"],
        satchel=row["satchel"],
        weapon_plus=row["weapon_plus"],
        armor_plus=row["armor_plus"],
        banked=row["banked"],
    )


def _row_to_event(row: sqlite3.Row) -> Event:
    return Event(
        event_id=row["id"],
        ts=row["ts"],
        kind=row["kind"],
        actor=row["actor"],
        text=row["text"],
        target=row["target"],
    )
