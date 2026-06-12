"""SQLite persistence tests.

Covers idempotent schema init, a full player round-trip through every
column (including ``def_``, ``turn_day``, ``log_cursor`` and the bestow
fields), event append with cursor-based catch-up, leaderboard tie-breaks,
and that WAL journaling is active.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests.conftest import make_player
from understone.engine.log import since
from understone.engine.models import Mode
from understone.persistence import Store

if TYPE_CHECKING:
    from pathlib import Path


def _store(tmp_path: Path) -> Store:
    return Store(tmp_path / "understone.db")


def test_schema_init_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "understone.db"
    Store(db).close()
    # Re-opening the same file must not error or duplicate schema.
    second = Store(db)
    assert second.get_meta("schema_version") == "1"
    second.close()


def test_wal_mode_active(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.journal_mode().lower() == "wal"
    store.close()


def test_player_round_trip_all_columns(tmp_path: Path) -> None:
    store = _store(tmp_path)
    player = make_player(
        name="Brandr",
        x=12,
        y=7,
        hp=18,
        max_hp=26,
        level=3,
        xp=305,
        gold=88,
        atk=9,
        def_=4,
        weapon_id="short_sword",
        armor_id="leather_armor",
        turns_left=6,
        turn_day=739_400,
        mode=Mode.MENU,
        at_location="inn",
        log_cursor=42,
        bestow_spent=15,
        bestow_day=739_400,
    )
    store.upsert_player(player)
    store.commit()
    store.close()

    reopened = _store(tmp_path)
    players, _ = reopened.load_all()
    loaded = players["Brandr"]
    assert loaded == player
    # Spot-check the fields most prone to silent drop.
    assert loaded.def_ == 4
    assert loaded.turn_day == 739_400
    assert loaded.log_cursor == 42
    assert loaded.bestow_spent == 15
    assert loaded.bestow_day == 739_400
    assert loaded.mode is Mode.MENU
    reopened.close()


def test_upsert_updates_existing_row(tmp_path: Path) -> None:
    store = _store(tmp_path)
    player = make_player(name="Sigrun", gold=10)
    store.upsert_player(player)
    store.commit()
    player.gold = 999
    store.upsert_player(player)
    store.commit()
    store.close()

    reopened = _store(tmp_path)
    players, _ = reopened.load_all()
    assert players["Sigrun"].gold == 999
    assert len(players) == 1
    reopened.close()


def test_event_append_and_since_cursor(tmp_path: Path) -> None:
    store = _store(tmp_path)
    id1 = store.insert_event("t1", "Brandr", "fight", "slew a rat")
    id2 = store.insert_event("t2", "Sigrun", "bestow", "blessed with gold")
    store.commit()
    store.close()

    reopened = _store(tmp_path)
    _, events = reopened.load_all()
    assert [e.event_id for e in events] == [id1, id2]

    # Catch up from a cursor before both, then advance past the first.
    fresh, cursor = since(events, 0)
    assert len(fresh) == 2
    assert cursor == id2

    after_first, cursor2 = since(events, id1)
    assert [e.event_id for e in after_first] == [id2]
    assert cursor2 == id2

    nothing, cursor3 = since(events, id2)
    assert nothing == []
    assert cursor3 == id2
    reopened.close()


def test_top_ranks_tie_breaks(tmp_path: Path) -> None:
    store = _store(tmp_path)
    # Same level: higher XP ranks first; equal XP breaks by name ascending.
    store.upsert_player(make_player(name="Carol", level=5, xp=1200, gold=10))
    store.upsert_player(make_player(name="Alice", level=5, xp=1500, gold=10))
    store.upsert_player(make_player(name="Bob", level=5, xp=1500, gold=10))
    store.upsert_player(make_player(name="Dave", level=4, xp=9999, gold=10))
    store.commit()

    ranks = store.top_ranks(limit=10)
    assert [r.name for r in ranks] == ["Alice", "Bob", "Carol", "Dave"]
    store.close()


def test_top_ranks_honours_limit(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for i in range(15):
        store.upsert_player(make_player(name=f"P{i:02d}", level=i, xp=i * 10))
    store.commit()
    ranks = store.top_ranks(limit=10)
    assert len(ranks) == 10
    # Highest level first.
    assert ranks[0].name == "P14"
    store.close()


def test_meta_round_trip(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.set_meta("world_name", "The Vale of Understone")
    assert store.get_meta("world_name") == "The Vale of Understone"
    assert store.get_meta("missing") is None
    store.close()
