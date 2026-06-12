"""Game façade integration tests over the shipped world.

Drives a full session against a temp store, a frozen clock, and a seeded
RNG: join -> status -> look -> move -> action(buy/rest/fight) -> log ->
rank -> bestow. Persistence is exercised by reopening the store.

Negative-test discipline (turn guard and bestow cap):
  Two guards are pinned by assertions here. To confirm each assertion has
  teeth, the implementer temporarily reverted the guard line and observed
  the matching test FAIL, then restored it:

  * Turn guard (engine/turns.py spend_turn): replacing
    ``if player.turns_left <= 0: return False`` with ``return True``
    let fighting continue past the daily budget — ``test_turn_budget_blocks``
    then failed on the "spent for today" assertion. Restored.
  * Bestow cap (game.py bestow): removing the ``if cost > remaining``
    refusal let an over-budget bestowal through — ``test_bestow_cap_refuses``
    then failed on the unchanged-gold assertion. Restored.
  * Sanitizer control-char guard (game.py _sanitize): disabling the
    ``not cleaned.isprintable()`` clause let a newline-injected name create a
    player row and a public event — ``test_join_rejects_control_char_name``
    then failed. Restored. (See the comment block above the hygiene tests.)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import fixed_clock, utc
from understone.engine.models import Mode
from understone.engine.rng import GameRNG
from understone.game import Game
from understone.persistence import Store
from understone.world.loader import load_world

PACK = Path(__file__).resolve().parents[1] / "understone" / "world" / "data"


@pytest.fixture
def clock() -> object:
    return fixed_clock(utc(2026, 6, 12, 10, 0))


def _game(tmp_path: Path, clock: object, seed: int = 7) -> Game:
    world = load_world(PACK)
    store = Store(tmp_path / "game.db")
    return Game(world, store, clock=clock, rng=GameRNG(seed=seed))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Join / status / look
# ---------------------------------------------------------------------------


def test_join_creates_player_at_spawn(tmp_path: Path, clock: object) -> None:
    game = _game(tmp_path, clock)
    out = game.join("Brandr")
    player = game.players["Brandr"]
    assert (player.x, player.y) == game.world.spawn
    assert player.gold == game.world.settings.starting_gold
    assert "@" in out
    assert game.world.name in out


def test_join_resumes_existing(tmp_path: Path, clock: object) -> None:
    game = _game(tmp_path, clock)
    game.join("Brandr")
    game.players["Brandr"].gold = 123
    out = game.join("Brandr")
    assert "Welcome back" in out
    assert game.players["Brandr"].gold == 123


def test_status_unknown_player_is_friendly(tmp_path: Path, clock: object) -> None:
    game = _game(tmp_path, clock)
    out = game.status("Nobody")
    assert "has signed the ledger" in out
    assert "door_join" in out


def test_look_overworld_has_frame(tmp_path: Path, clock: object) -> None:
    game = _game(tmp_path, clock)
    game.join("Brandr")
    out = game.look("Brandr")
    assert "@" in out
    assert "┌" in out and "┐" in out
    assert len(out) < 2048


def test_look_in_menu_shows_location(tmp_path: Path, clock: object) -> None:
    game = _game(tmp_path, clock)
    game.join("Brandr")
    # Shop is two cells east of spawn along the road.
    game.move("Brandr", "", "east", 2)
    assert game.players["Brandr"].mode is Mode.MENU
    out = game.look("Brandr")
    assert "(B)uy" in out and "(L)eave" in out


# ---------------------------------------------------------------------------
# Move
# ---------------------------------------------------------------------------


def test_move_blocked_in_menu(tmp_path: Path, clock: object) -> None:
    game = _game(tmp_path, clock)
    game.join("Brandr")
    game.move("Brandr", "", "east", 2)  # into the shop menu
    out = game.move("Brandr", "", "east", 2)
    assert "inside" in out.lower()


def test_move_enters_location(tmp_path: Path, clock: object) -> None:
    game = _game(tmp_path, clock)
    game.join("Brandr")
    out = game.move("Brandr", "", "west", 2)  # inn is two cells west
    assert game.players["Brandr"].at_location == "inn"
    assert "step inside" in out.lower()


# ---------------------------------------------------------------------------
# Actions: rest, fight, turn budget
# ---------------------------------------------------------------------------


def test_rest_heals_and_charges(tmp_path: Path, clock: object) -> None:
    game = _game(tmp_path, clock)
    game.join("Brandr")
    player = game.players["Brandr"]
    player.hp = 5
    game.move("Brandr", "", "west", 2)  # inn
    out = game.action("Brandr", "rest", "", "")
    assert player.hp == player.max_hp
    assert player.gold == game.world.settings.starting_gold - game.world.settings.rest_cost
    assert "full health" in out.lower()


def test_fight_spends_a_turn_and_credits(tmp_path: Path, clock: object) -> None:
    game = _game(tmp_path, clock)
    game.join("Brandr")
    player = game.players["Brandr"]
    # Drop into the forest_near zone so an encounter is available.
    player.x, player.y = 35, 25
    before_turns = player.turns_left
    out = game.action("Brandr", "fight", "", "")
    assert player.turns_left == before_turns - 1
    assert player.xp > 0
    assert "XP" in out


def test_turn_budget_blocks(tmp_path: Path, clock: object) -> None:
    """Pins the spend_turn guard: at 0 turns, fighting is refused.

    See the module docstring for the revert-and-observe-failure check that
    proves this assertion has teeth.
    """
    game = _game(tmp_path, clock)
    game.join("Brandr")
    player = game.players["Brandr"]
    player.x, player.y = 35, 25
    player.turns_left = 0
    out = game.action("Brandr", "fight", "", "")
    assert "spent for today" in out.lower()
    # No turn was consumed past zero, and no XP was gained.
    assert player.turns_left == 0
    assert player.xp == 0


# ---------------------------------------------------------------------------
# Log / rank
# ---------------------------------------------------------------------------


def test_log_reports_then_advances(tmp_path: Path, clock: object) -> None:
    game = _game(tmp_path, clock)
    game.join("Brandr")
    # A second player acting creates a public event Brandr has not yet seen.
    game.join("Sigrun")
    first = game.log("Brandr")
    assert "Sigrun" in first or "Brandr" in first
    # The cursor advanced; a second read with no new events is quiet.
    second = game.log("Brandr")
    assert "All quiet" in second


def test_rank_marks_caller(tmp_path: Path, clock: object) -> None:
    game = _game(tmp_path, clock)
    game.join("Brandr")
    game.join("Sigrun")
    game.players["Sigrun"].level = 5
    out = game.rank("Brandr")
    assert "Brandr" in out and "Sigrun" in out
    assert "*" in out  # the caller's row is marked
    assert "┌" in out  # box-drawing table


def test_shared_world_other_player_marker(tmp_path: Path, clock: object) -> None:
    game = _game(tmp_path, clock)
    game.join("Brandr")
    game.join("Sigrun")
    # Stand Sigrun one cell east of Brandr's spawn so she lands in the view.
    sig = game.players["Sigrun"]
    brandr = game.players["Brandr"]
    sig.x, sig.y = brandr.x + 1, brandr.y
    out = game.look("Brandr")
    assert "&" in out  # the other player shows as '&'


# ---------------------------------------------------------------------------
# Bestow (+ cap negative test)
# ---------------------------------------------------------------------------


def test_bestow_grants_gold(tmp_path: Path, clock: object) -> None:
    game = _game(tmp_path, clock)
    game.join("Brandr")
    player = game.players["Brandr"]
    before = player.gold
    out = game.bestow("Brandr", "a daring rescue", 10, 0)
    assert player.gold == before + 10
    assert player.bestow_spent == 10
    assert "bestowal" in out.lower()


def test_bestow_heal_charges_only_applied(tmp_path: Path, clock: object) -> None:
    game = _game(tmp_path, clock)
    game.join("Brandr")
    player = game.players["Brandr"]
    player.hp = player.max_hp - 3  # only 3 missing
    game.bestow("Brandr", "mercy after a hard fight", 0, 10)
    assert player.hp == player.max_hp
    # Charged for 3 HP at heal_cost_per_hp, not the requested 10.
    assert player.bestow_spent == 3 * game.world.settings.heal_cost_per_hp


def test_bestow_requires_reason(tmp_path: Path, clock: object) -> None:
    game = _game(tmp_path, clock)
    game.join("Brandr")
    out = game.bestow("Brandr", "   ", 10, 0)
    assert "reason" in out.lower()
    assert game.players["Brandr"].gold == game.world.settings.starting_gold


def test_bestow_requires_nonzero(tmp_path: Path, clock: object) -> None:
    game = _game(tmp_path, clock)
    game.join("Brandr")
    out = game.bestow("Brandr", "nothing at all", 0, 0)
    assert "at least" in out.lower()


def test_bestow_cap_refuses(tmp_path: Path, clock: object) -> None:
    """Pins the bestow cap: an over-budget grant is refused without mutation.

    See the module docstring for the revert-and-observe-failure check that
    proves this assertion has teeth.
    """
    game = _game(tmp_path, clock)
    game.join("Brandr")
    player = game.players["Brandr"]
    budget = game.world.settings.bestow_daily_budget
    before_gold = player.gold
    out = game.bestow("Brandr", "an absurd windfall", budget + 100, 0)
    assert "the fates allow" in out.lower()
    # Refused cleanly: no gold moved and no pool spent.
    assert player.gold == before_gold
    assert player.bestow_spent == 0


def test_bestow_pool_resets_next_day(tmp_path: Path, clock: object) -> None:
    game = _game(tmp_path, clock)
    game.join("Brandr")
    player = game.players["Brandr"]
    game.bestow("Brandr", "first blessing", 20, 0)
    assert player.bestow_spent == 20
    # Advance the clock past UTC midnight; the next bestow sees a fresh pool.
    game.clock = fixed_clock(utc(2026, 6, 13, 0, 5))  # type: ignore[assignment]
    game.bestow("Brandr", "a new day's fortune", 20, 0)
    assert player.bestow_spent == 20  # reset to 0 then +20, not 40


# ---------------------------------------------------------------------------
# Persistence round-trip through the façade
# ---------------------------------------------------------------------------


def test_state_survives_store_reopen(tmp_path: Path, clock: object) -> None:
    game = _game(tmp_path, clock)
    game.join("Brandr")
    game.players["Brandr"].x, game.players["Brandr"].y = 35, 25
    game.action("Brandr", "fight", "", "")
    xp_after = game.players["Brandr"].xp
    gold_after = game.players["Brandr"].gold
    game.store.close()

    world = load_world(PACK)
    reopened = Store(tmp_path / "game.db")
    revived = Game(world, reopened, clock=clock)  # type: ignore[arg-type]
    assert revived.players["Brandr"].xp == xp_after
    assert revived.players["Brandr"].gold == gold_after


# ---------------------------------------------------------------------------
# Day rollover applies to fight/descend, not just join/bestow
# ---------------------------------------------------------------------------


class _MutableClock:
    """A clock whose reported moment can be advanced between calls."""

    def __init__(self, moment: object) -> None:
        self.moment = moment

    def __call__(self) -> object:
        return self.moment


def test_fight_refreshes_budget_across_midnight(tmp_path: Path) -> None:
    """A fight on a new UTC day must reset the budget without re-joining.

    Before the fix, _resolve_encounter spent a turn without calling
    _ensure_day, so an exhausted player who returned the next day was still
    blocked until they happened to re-join.
    """
    clk = _MutableClock(utc(2026, 6, 12, 23, 0))
    world = load_world(PACK)
    store = Store(tmp_path / "game.db")
    game = Game(world, store, clock=clk, rng=GameRNG(seed=7))  # type: ignore[arg-type]
    game.join("Brandr")
    player = game.players["Brandr"]
    player.x, player.y = 35, 25  # forest_near zone: an encounter is available
    player.turns_left = 0  # spent for the day
    daily = game.world.settings.daily_turns

    clk.moment = utc(2026, 6, 13, 0, 5)  # cross UTC midnight, no re-join
    out = game.action("Brandr", "fight", "", "")

    assert "spent for today" not in out.lower()  # the fresh day let the fight run
    assert player.turns_left == daily - 1  # reset to full, then one spent
    assert player.xp > 0
    assert f"/{daily} ]" in out  # footer shows the refreshed budget


def test_descend_refreshes_budget_across_midnight(tmp_path: Path) -> None:
    """Descending on a new UTC day resets the budget without re-joining."""
    clk = _MutableClock(utc(2026, 6, 12, 23, 0))
    world = load_world(PACK)
    store = Store(tmp_path / "game.db")
    game = Game(world, store, clock=clk, rng=GameRNG(seed=7))  # type: ignore[arg-type]
    game.join("Hero")
    player = game.players["Hero"]
    # Overwhelming stats so the gauntlet itself never bounces the player.
    player.level, player.atk, player.def_ = 20, 200, 100
    player.hp = player.max_hp = 500
    player.mode = Mode.MENU
    player.at_location = "dungeon"
    player.turns_left = 0
    daily = game.world.settings.daily_turns

    clk.moment = utc(2026, 6, 13, 0, 5)
    out = game.action("Hero", "descend", "", "")

    assert "too weary" not in out.lower()
    assert player.turns_left == daily - 1


# ---------------------------------------------------------------------------
# Input hygiene chokepoint (the _sanitize helper)
# ---------------------------------------------------------------------------
#
# Negative-test discipline (security invariant): to prove the control-char
# rejection in Game._sanitize has teeth, the implementer temporarily replaced
# its ``not cleaned.isprintable()`` clause with ``False`` (disabling the
# check) and confirmed test_join_rejects_control_char_name FAILED — the
# injected name created a player row and a public event. The clause was then
# restored. The newline-injection test below is the standing regression for
# that invariant.


def test_join_rejects_control_char_name(tmp_path: Path, clock: object) -> None:
    """A bell/control character in a name is refused with the runes line."""
    game = _game(tmp_path, clock)
    out = game.join("Bra\x07ndr")
    assert "strange runes" in out
    assert game.players == {}  # no row created
    assert game.events == []  # nothing persisted


def test_join_rejects_newline_name_no_persist(tmp_path: Path, clock: object) -> None:
    """An embedded newline (log-injection vector) is refused, nothing written.

    The name is kept short so it is the control-char clause — not the length
    clause — that rejects it; this is the standing regression for the
    isprintable security invariant documented in the module docstring.
    """
    game = _game(tmp_path, clock)
    out = game.join("Bra\nndr")  # 7 chars: well under the 24 limit
    assert "strange runes" in out  # the runes (bad-character) refusal, not length
    # The security invariant: no player row and no event row escaped the guard.
    assert game.players == {}
    assert game.events == []


def test_join_rejects_overlong_name(tmp_path: Path, clock: object) -> None:
    """A 25-character name is refused with the narrow-ledger line."""
    game = _game(tmp_path, clock)
    out = game.join("X" * 25)
    assert "ledger is narrow" in out
    assert game.players == {}


def test_join_accepts_max_length_name(tmp_path: Path, clock: object) -> None:
    """A 24-character name is exactly at the limit and accepted."""
    game = _game(tmp_path, clock)
    name = "X" * 24
    game.join(name)
    assert name in game.players


def test_bestow_rejects_newline_reason_no_persist(tmp_path: Path, clock: object) -> None:
    """A newline-embedded bestow reason is refused; no event, pool unchanged."""
    game = _game(tmp_path, clock)
    game.join("Brandr")
    player = game.players["Brandr"]
    events_before = len(game.events)
    out = game.bestow("Brandr", "heroics\nand a forged log line", 10, 0)
    assert "plainly-spoken" in out
    assert len(game.events) == events_before  # no bestow event appended
    assert player.bestow_spent == 0  # pool untouched


# ---------------------------------------------------------------------------
# Bestow: heal-only at full HP grants nothing (no empty grant persisted)
# ---------------------------------------------------------------------------


def test_bestow_heal_only_at_full_hp_refused(tmp_path: Path, clock: object) -> None:
    """A heal-only bestow at full HP applies nothing and must not persist."""
    game = _game(tmp_path, clock)
    game.join("Brandr")
    player = game.players["Brandr"]
    assert player.hp == player.max_hp  # join starts at full health
    events_before = len(game.events)
    out = game.bestow("Brandr", "a quiet blessing", 0, 10)
    assert "already hale" in out
    assert len(game.events) == events_before  # no "Fortune favours" line written
    assert player.bestow_spent == 0  # nothing charged


# ---------------------------------------------------------------------------
# Descend gauntlet: survive both tiers, or bounce to spawn on the first
# ---------------------------------------------------------------------------


def test_descend_survives_full_gauntlet(tmp_path: Path, clock: object) -> None:
    """A strong player clears both tiers: two foes fought, rewards banked."""
    game = _game(tmp_path, clock)
    game.join("Hero")
    player = game.players["Hero"]
    player.level, player.atk, player.def_ = 20, 200, 100
    player.hp = player.max_hp = 500
    player.mode = Mode.MENU
    player.at_location = "dungeon"
    before_turns, before_gold, before_xp = player.turns_left, player.gold, player.xp

    out = game.action("Hero", "descend", "", "")

    # Both ladder rungs were fought (the tier-4 and tier-5 boss names appear).
    assert "Cave Troll" in out
    assert "Stone Wyrm" in out
    assert player.turns_left == before_turns - 1
    assert player.gold > before_gold
    assert player.xp > before_xp


def test_descend_bounces_weak_player_to_spawn(tmp_path: Path, clock: object) -> None:
    """A fresh weak player falls on the first foe and wakes at the spawn."""
    game = _game(tmp_path, clock)
    game.join("Weakling")
    player = game.players["Weakling"]
    player.mode = Mode.MENU
    player.at_location = "dungeon"

    out = game.action("Weakling", "descend", "", "")

    assert player.hp == 1
    assert player.mode is Mode.TILE
    assert player.at_location == ""
    assert (player.x, player.y) == game.world.spawn
    # Felled by the first rung; the second boss never appears.
    assert "Cave Troll" in out
    assert "Stone Wyrm" not in out


# ---------------------------------------------------------------------------
# Shop façade: buy / upgrade / sell / heal stat arithmetic
# ---------------------------------------------------------------------------


def test_shop_buy_upgrade_sell_heal_cycle(tmp_path: Path, clock: object) -> None:
    """Equip deltas apply once on buy/upgrade and unwind cleanly on sell."""
    game = _game(tmp_path, clock)
    game.join("Brandr")
    player = game.players["Brandr"]
    player.gold = 1000
    player.mode = Mode.MENU
    player.at_location = "shop"

    short_sword = game.world.item_by_id("short_sword")
    war_axe = game.world.item_by_id("war_axe")
    starter = game.world.item_by_id(game.world.settings.starting_weapon)
    assert short_sword is not None and war_axe is not None and starter is not None

    starter_atk = player.atk  # 3 base + rusty dagger bonus

    # Buy the short sword: gold falls by its price, atk rises by the delta.
    gold0 = player.gold
    game.action("Brandr", "buy", "", "short_sword")
    assert player.gold == gold0 - short_sword.price
    assert player.atk == starter_atk + (short_sword.atk - starter.atk)
    atk_with_sword = player.atk

    # Upgrade to the war axe: atk reflects the difference, not a double-add.
    gold1 = player.gold
    game.action("Brandr", "buy", "", "war_axe")
    assert player.gold == gold1 - war_axe.price
    assert player.atk == atk_with_sword + (war_axe.atk - short_sword.atk)

    # Sell the war axe: half-price refund, atk falls back to the starter bonus.
    gold2 = player.gold
    game.action("Brandr", "sell", "", "")
    assert player.gold == gold2 + war_axe.price // 2
    assert player.atk == starter_atk

    # Heal at the shrine: HP restored, gold debited per missing point.
    player.mode = Mode.MENU
    player.at_location = "healer"
    player.hp = player.max_hp - 5
    per_hp = game.world.settings.heal_cost_per_hp
    gold3 = player.gold
    game.action("Brandr", "heal", "", "")
    assert player.hp == player.max_hp
    assert player.gold == gold3 - 5 * per_hp


def test_sell_starter_weapon_refused(tmp_path: Path, clock: object) -> None:
    """The starter blade is unsellable regardless of price (no free-gold loop)."""
    game = _game(tmp_path, clock)
    game.join("Brandr")
    player = game.players["Brandr"]
    assert player.weapon_id == game.world.settings.starting_weapon
    player.mode = Mode.MENU
    player.at_location = "shop"
    gold_before = player.gold
    out = game.action("Brandr", "sell", "", "")
    assert "nothing worth selling" in out.lower()
    assert player.gold == gold_before


# ---------------------------------------------------------------------------
# Bounded in-memory event tail (full history stays in SQLite)
# ---------------------------------------------------------------------------


def test_event_tail_is_capped_but_log_still_works(tmp_path: Path, clock: object) -> None:
    """Loading caps the resident tail; door_log still serves recent events."""
    from understone.engine.log import since
    from understone.game import EVENT_TAIL_KEEP

    db = tmp_path / "game.db"
    seed_store = Store(db)
    last_id = 0
    for i in range(EVENT_TAIL_KEEP + 50):
        last_id = seed_store.insert_event("t", "sys", "note", f"event {i}")
    seed_store.commit()
    seed_store.close()

    world = load_world(PACK)
    game = Game(world, Store(db), clock=clock, rng=GameRNG(seed=7))  # type: ignore[arg-type]
    # Only the most recent EVENT_TAIL_KEEP events are resident in memory.
    assert len(game.events) == EVENT_TAIL_KEEP
    assert game.events[-1].event_id == last_id

    # door_log still reports events after a recent cursor.
    recent_cursor = game.events[-3].event_id
    game.join("Brandr")
    game.players["Brandr"].log_cursor = recent_cursor
    out = game.log("Brandr")
    assert "While you were away" in out
    fresh, new_cursor = since(game.events, recent_cursor)
    assert fresh  # there are events past the cursor
    assert new_cursor == game.events[-1].event_id
