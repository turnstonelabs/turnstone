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
        posts_sent=3,
        post_day=739_400,
        gambles=2,
        gamble_day=739_400,
        banked=420,
    )
    store.upsert_player(player)
    store.commit()
    store.close()

    reopened = _store(tmp_path)
    players, _ = reopened.load_all()
    loaded = players["Brandr"]
    assert loaded == player
    assert loaded.banked == 420
    # Spot-check the fields most prone to silent drop.
    assert loaded.def_ == 4
    assert loaded.turn_day == 739_400
    assert loaded.log_cursor == 42
    assert loaded.bestow_spent == 15
    assert loaded.bestow_day == 739_400
    assert loaded.mode is Mode.MENU
    # The v0.5 social columns survive the round-trip too.
    assert loaded.posts_sent == 3
    assert loaded.post_day == 739_400
    assert loaded.gambles == 2
    assert loaded.gamble_day == 739_400
    reopened.close()


def test_event_target_round_trips(tmp_path: Path) -> None:
    """A targeted (private) event keeps its target across a reopen; public is ''."""
    store = _store(tmp_path)
    pub = store.insert_event("t1", "Brandr", "join", "set out")
    priv = store.insert_event("t2", "Sigrun", "ambushed", "robbed in your sleep", "Brandr")
    store.commit()
    store.close()

    reopened = _store(tmp_path)
    _, events = reopened.load_all()
    by_id = {e.event_id: e for e in events}
    assert by_id[pub].target == ""  # public stays empty
    assert by_id[priv].target == "Brandr"  # private keeps its recipient
    reopened.close()


def test_ambush_table_per_day_uniqueness(tmp_path: Path) -> None:
    """The ambushes PK is (attacker, target, day): one row per pair per day."""
    store = _store(tmp_path)
    day = 739_400
    assert store.has_ambushed("Brandr", "Sigrun", day) is False
    store.record_ambush("Brandr", "Sigrun", day)
    store.commit()
    assert store.has_ambushed("Brandr", "Sigrun", day) is True
    # A second record for the same pair/day is a no-op (INSERT OR IGNORE):
    # the duplicate must not raise and must not add a row.
    store.record_ambush("Brandr", "Sigrun", day)
    store.commit()
    rows = store._conn.execute(
        "SELECT COUNT(*) AS n FROM ambushes WHERE attacker=? AND target=? AND day=?",
        ("Brandr", "Sigrun", day),
    ).fetchone()
    assert rows["n"] == 1
    # A new day is a fresh attempt; the old day stays recorded.
    assert store.has_ambushed("Brandr", "Sigrun", day + 1) is False
    store.record_ambush("Brandr", "Sigrun", day + 1)
    store.commit()
    assert store.has_ambushed("Brandr", "Sigrun", day) is True
    assert store.has_ambushed("Brandr", "Sigrun", day + 1) is True
    store.close()


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


def test_retention_columns_round_trip(tmp_path: Path) -> None:
    """The retention columns survive a reopen: depth, the v0.10 stack-encoded
    satchel, the two forged plusses, and the v0.10 banked vault gold."""
    store = _store(tmp_path)
    player = make_player(
        name="Delver",
        deepest_rung=2,
        satchel="minor_potion:3,iron_ore:5",  # v0.10 "id:qty" stack encoding
        weapon_plus=2,
        armor_plus=1,
        banked=300,
    )
    store.upsert_player(player)
    store.commit()
    store.close()

    reopened = _store(tmp_path)
    players, _ = reopened.load_all()
    loaded = players["Delver"]
    assert loaded == player  # full equality across every column
    assert loaded.deepest_rung == 2
    assert loaded.satchel == "minor_potion:3,iron_ore:5"
    assert loaded.weapon_plus == 2
    assert loaded.armor_plus == 1
    assert loaded.banked == 300
    reopened.close()


def test_v0_7_depth_columns_default_for_legacy_rows(tmp_path: Path) -> None:
    """A row written without the new columns loads them at their defaults.

    The schema mutates in place (no migration, stamp stays 1), so the new
    columns carry DB-side defaults: a pre-v0.7 player row (inserted with the
    legacy column set) must read back deepest_rung 0, an empty satchel, and
    zero plusses rather than erroring.
    """
    store = _store(tmp_path)
    store._conn.execute(
        "INSERT INTO players "
        "(name, x, y, hp, max_hp, level, xp, gold, atk, def_, weapon_id, armor_id, "
        " turns_left, turn_day, mode, at_location, created_at, last_seen, log_cursor, "
        " bestow_spent, bestow_day) "
        "VALUES ('Old', 5, 5, 20, 20, 1, 0, 20, 5, 1, 'rusty_dagger', 'cloth_tunic', "
        " 10, 0, 'tile', '', 't0', 't0', 0, 0, 0)",
    )
    store.commit()
    store.close()

    reopened = _store(tmp_path)
    players, _ = reopened.load_all()
    old = players["Old"]
    assert old.deepest_rung == 0
    assert old.satchel == ""
    assert old.weapon_plus == 0
    assert old.armor_plus == 0
    assert old.banked == 0  # the v0.10 vault column defaults too
    assert reopened.get_meta("schema_version") == "1"  # stamp unchanged
    reopened.close()
