"""The balance instrument — a greedy bot that PLAYS a world to measure it.

This is a TUNING PROBE, not an optimiser. It drives a deliberately simple,
greedy heuristic adventurer through the *real* :class:`~understone.game.Game`
façade — the same ``join`` / ``move`` / ``action`` methods the MCP tools call —
over a fresh in-memory store, a seeded :class:`~understone.engine.rng.GameRNG`,
and an injected clock. Because it plays through the actual façade, a sim run is
also a fierce end-to-end integration test of the whole stack: every system the
report reflects (movement, the zone-banded forest, the rung ladder, the forge
and satchel, the Wyrm endgame) is exercised by the real engine, never mocked.

The bot is a yardstick, not a player to admire. Its policy is the obvious greedy
one — spend each daily turn on the single best-looking action, navigate for free
between town and the wilds, keep itself geared and potioned — so the resulting
:class:`BalanceReport` answers "is this world *shaped* right for a competent but
unclever hero?": does a run make steady progress, is the fight/descend mix sane,
and — the load-bearing question — is the world *winnable*, i.e. can a greedy bot
actually slay the Wyrm in a reasonable number of days?

The module is PURE: it imports the engine, the game façade, and the loader only,
and never touches ``mcp``, ``starlette``, or the network.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, TextIO

from understone.engine import turns
from understone.engine.models import Item, Mode, Monster, Player, Slot
from understone.engine.rng import GameRNG
from understone.engine.satchel import decode_satchel
from understone.game import Game
from understone.persistence import Store
from understone.world.loader import load_world

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from understone.engine.world import World

# The bot's adventurer name in every sim (a fixed handle keeps the run readable).
_BOT_NAME = "Probe"

# A run begins at this fixed UTC instant; the clock advances exactly one day per
# simulated day so the daily turn budget resets cleanly between sim-days. Midday
# avoids the Watch day/night edges (irrelevant here, but keeps the instant tidy).
_SIM_START = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

# Heuristic margins (all fractions of max_hp). The bot rests/heals below
# ``_REST_BELOW``; it only commits to a fight or a rung it expects to END at or
# above ``_FIGHT_MARGIN`` / ``_DESCEND_MARGIN`` of full health, so it spends
# turns on bouts it can likely win rather than feeding the death-save.
_REST_BELOW = 0.5
_FIGHT_MARGIN = 0.30
_DESCEND_MARGIN = 0.45
# The Wyrm is the climax and a legacy reset rides on it, so the bot will not
# stake a challenge until a pessimistic full-health bout clears this much hp —
# it keeps grinding the deep for levels, gear, and forge edges until it can
# actually win, rather than feeding doomed challenges to the boss.
_WYRM_MARGIN = 0.20

# "Flush" gold floor for opportunistic spending (gear, forge, potions): the bot
# keeps at least this much in reserve so a shopping spree never strands it unable
# to rest. A small, world-agnostic buffer expressed against a day's rest cost.
_RESERVE_RESTS = 3

# Hard cap on free navigation moves toward a single waypoint, so a bot that can
# never thread a monster-thick wood (or a malformed map) ends the day instead of
# looping forever. Generous — a clear path is a handful of 8-cell hops.
_GOTO_MAX_MOVES = 200


@dataclass(frozen=True, slots=True)
class BalanceReport:
    """The measured outcome of one greedy bot run over a world.

    Every field is a yardstick for tuning, not a score. ``deaths_survived`` are
    the bouts the satchel's death-save snatched back; ``deaths_taken`` the
    genuine spawn bounces. ``realized_fight_share`` is the fraction of
    turn-spending actions that were forest fights (vs descents and the Wyrm
    challenge) — the run's actual analogue of the pack's authored fight-weight.
    ``ending_satchel`` is the potions still carried at the final bell.

    ``final_level`` is the level at the FINAL bell, not the peak reached: a Wyrm
    kill reincarnates the hero with a legacy reset (``level`` → 1), so a winning
    run can report a ``final_level`` as low as 1. Read it alongside
    ``wyrm_killed`` — a low level on a won run means the hero is mid-reclimb, not
    that the run stalled. Averaging ``final_level`` across a sweep that mixes wins
    and losses conflates the two; the renders pair the two fields for this reason.
    """

    days: int
    seed: int
    final_level: int
    total_gold_earned: int
    deaths_survived: int
    deaths_taken: int
    rungs_cleared: int
    wyrm_killed: bool
    day_of_first_wyrm_kill: int | None
    fights_fought: int
    realized_fight_share: float
    ending_satchel: tuple[str, ...]


class _Clock:
    """A mutable injected clock: reports a fixed instant, steppable by a day."""

    def __init__(self, start: datetime) -> None:
        self._now = start

    def __call__(self) -> datetime:
        return self._now

    def advance_day(self) -> None:
        """Move the clock forward one UTC day (the sim-day boundary)."""
        self._now += timedelta(days=1)


def simulate(pack_dir: Path, days: int, seed: int) -> BalanceReport:
    """Play a greedy bot through the world at *pack_dir* and return its report.

    Builds a real :class:`Game` over a fresh in-memory store with the seeded RNG
    and the steppable clock, joins the bot, then runs *days* sim-days: each day
    the bot spends its turn budget on the best available action (with free
    navigation between), and the clock advances one day so turns reset. The
    returned :class:`BalanceReport` is fully determined by ``(pack_dir, days,
    seed)`` — same inputs, identical report.
    """
    world = load_world(pack_dir)
    clock = _Clock(_SIM_START)
    store = Store(":memory:")
    game = Game(world, store, clock=clock, rng=GameRNG(seed=seed))
    bot = _Bot(game, world, clock)
    bot.run(days)
    return bot.report(days=days, seed=seed)


class _Bot:
    """The greedy heuristic adventurer that drives the real game façade."""

    def __init__(self, game: Game, world: World, clock: _Clock) -> None:
        self.game = game
        self.world = world
        self.clock = clock
        self.name = _BOT_NAME
        self._walkable = _reachable(world)
        self._waypoints = _named_waypoints(world)
        self._hunt_spots = _zone_hunt_spots(world, self._walkable)
        # Accumulators the report is built from.
        self.total_gold_earned = 0
        self.deaths_survived = 0
        self.deaths_taken = 0
        self.fights_fought = 0
        self.descents = 0
        self.challenges = 0
        self.rungs_cleared = 0
        self.wins = 0
        self.day_of_first_wyrm_kill: int | None = None

    # -- the run loop ----------------------------------------------------

    def run(self, days: int) -> None:
        """Play *days* sim-days, advancing the clock one day between each."""
        self.game.join(self.name)
        for _day in range(days):
            self._play_day()
            self.clock.advance_day()

    def _play_day(self) -> None:
        """Spend the day's turn budget on the best action the bot can find.

        Movement and town errands (rest/heal/buy/forge) are free, so a single
        day may interleave many of them around the few turn-spending bouts. The
        loop ends when the bot is out of turns or can find nothing useful to do.
        """
        # Roll the daily budget eagerly so the loop gate below sees the fresh
        # turn count. The game does this lazily on the first action; the bot's
        # gate reads turns_left BEFORE acting, so it must roll first itself. The
        # roll is idempotent — the next façade action re-rolls the same ordinal
        # and persists it — so this never double-grants.
        turns.ensure_day(self._player(), self.clock, self._turn_budget())
        # Iteration cap: a pure runaway guard, never reached in normal play. Each
        # loop either spends a turn or does a free errand (recover/buy/forge),
        # and the free errands per day are bounded (gear is finite, forge and
        # satchel cap out), so the real per-day count is well under this — a
        # generous multiple of the turn budget that a healthy day never nears.
        guard = 0
        max_steps = self._turn_budget() * 8 + 32
        while self._turns_left() > 0 and guard < max_steps:
            guard += 1
            if not self._take_best_action():
                break

    def _take_best_action(self) -> bool:
        """Do the single best thing right now; return False when nothing helps.

        Priority order (the greedy policy): survive (rest/heal when hurt), keep
        the satchel and the gear strong while flush, then spend a turn on the
        best bout — challenge the Wyrm if ready, else descend a rung the bot can
        likely clear, else hunt the best survivable forest zone.
        """
        player = self._player()
        # 1. Survive: mend before risking a turn, if a bed/shrine is affordable.
        if player.hp < player.max_hp * _REST_BELOW and self._recover():
            return True
        # 2. Spend down a flush purse on lasting advantages (free, no turn).
        if self._upgrade_gear() or self._stock_satchel() or self._forge_edge():
            return True
        # 3. The Wyrm: the win condition, the moment it is reachable.
        if self._wyrm_ready():
            self._heal_full_if_possible()
            return self._challenge()
        # 4. Descend a rung the bot expects to clear (mended first).
        if self._should_descend():
            self._heal_full_if_possible()
            return self._descend()
        # 5. Otherwise hunt the best survivable forest for XP and gold.
        return self._hunt()

    # -- decisions: recovery & spending ----------------------------------

    def _recover(self) -> bool:
        """Rest at the inn (preferred) or heal at the shrine; True if mended."""
        settings = self.world.settings
        player = self._player()
        if "inn" in self._waypoints and player.gold >= settings.rest_cost:
            return self._errand("inn", "rest")
        free_heal = settings.heal_cost_per_hp == 0
        if "healer" in self._waypoints and (
            free_heal or player.gold >= settings.heal_cost_per_hp * 4
        ):
            return self._errand("healer", "heal")
        return False

    def _upgrade_gear(self) -> bool:
        """Buy the best affordable weapon/armour upgrade if flush; True if bought."""
        if not self._shop_offers("buy"):
            return False
        player = self._player()
        weapon = self._best_upgrade(Slot.WEAPON, player.weapon_id)
        armor = self._best_upgrade(Slot.ARMOR, player.armor_id)
        target = self._dearer(weapon, armor)
        if target is None or not self._can_afford(target.price):
            return False
        return self._errand("shop", "buy", item=target.item_id)

    def _stock_satchel(self) -> bool:
        """Top the satchel up with the strongest affordable potion, if flush.

        Counts POTIONS carried (across consumable stacks), not distinct stacks:
        the bot wants a small reserve of draughts for the death-save, and since
        v0.10 potions of one kind stack, the cap is read as "carry up to
        ``satchel_max`` draughts" — which also bounds the buy loop (buying the
        same potion bumps one stack, so a stack-count gate would never fill).
        Ore the bot wins in the deep shares the bag but is not a draught, so it
        never blocks topping up potions here.
        """
        if not self._shop_offers("buy"):
            return False
        if self._potions_carried() >= self.world.settings.satchel_max:
            return False
        potion = self._best_affordable_potion()
        if potion is None:
            return False
        return self._errand("shop", "buy", item=potion.item_id)

    def _forge_edge(self) -> bool:
        """Forge a +1 edge on weapon then armour when gold AND ore are plentiful.

        Only fires once the bot is genuinely flush (a forge is the late-game
        gold sink), only when its gear is already the best the shop sells (so it
        never forges a blade it is about to replace), and only when it actually
        holds the ORE the +1 step costs — ore is won in the deep, so the bot
        forges as the deep feeds it, exactly as real play does. Without the ore
        for a step it skips it rather than spinning on a forge it cannot pay.
        """
        if not self._shop_offers("forge"):
            return False
        settings = self.world.settings
        player = self._player()
        if not self._gear_is_best():
            return False
        ore_have = self._satchel_qty(settings.forge_ore_item)
        for current, slot_arg in (
            (player.weapon_plus, "weapon"),
            (player.armor_plus, "armour"),
        ):
            if current >= settings.forge_max_plus:
                continue
            cost = settings.forge_base_cost * (current + 1)
            ore_need = (current + 1) * settings.forge_ore_per_plus
            if not self._can_afford(cost) or ore_have < ore_need:
                continue
            return self._errand("shop", "forge", target=slot_arg)
        return False

    # -- decisions: the turn-spending bouts ------------------------------

    def _wyrm_ready(self) -> bool:
        """True when the bot can BEAT the Wyrm: both gates plus a winnable bout.

        Meeting the engine's gates (level floor and full depth) only OPENS the
        challenge; the bot adds its own readiness test — a pessimistic
        full-health bout against the boss clearing :data:`_WYRM_MARGIN` — so it
        challenges when it can win, not the instant it is allowed to. Until then
        the deep-zone grind keeps raising its level, gear, and forge edges.
        """
        settings = self.world.settings
        player = self._player()
        if "dungeon" not in self._waypoints:
            return False
        if player.level < settings.wyrm_min_level:
            return False
        if player.deepest_rung < len(settings.dungeon_tiers):
            return False
        boss = self.world.monster_by_id(settings.boss_monster)
        if boss is None:
            return False
        return _survivable(
            player.max_hp, player.max_hp, player.atk, player.def_, boss, _WYRM_MARGIN
        )

    def _challenge(self) -> bool:
        """Enter the dungeon and challenge the Wyrm (spends a turn).

        Returns False if the dungeon menu can't be entered (malformed map), so
        the day loop ends rather than spinning on a no-op overworld challenge.
        """
        if not self._enter("dungeon"):
            return False
        wins_before = self._player().wins
        out = self._act("challenge")
        self.challenges += 1
        self._note_outcome(out, wins_before)
        self._leave_if_in_menu()
        return True

    def _should_descend(self) -> bool:
        """True when a dungeon exists, a rung remains, and the bot can likely win.

        Looks at the SPECIFIC next-rung guardian (``band[0]`` of the next tier,
        the engine's fixed-foe rung) and only commits if a full-health bout
        projects to end above the descend margin — so the bot pushes the deep
        when geared for it rather than throwing turns at a wall.
        """
        settings = self.world.settings
        player = self._player()
        if "dungeon" not in self._waypoints:
            return False
        if player.deepest_rung >= len(settings.dungeon_tiers):
            return False
        guardian = self._rung_guardian(player.deepest_rung)
        if guardian is None:
            return False
        # Judged from FULL health (the bot heals before descending), so the gate
        # asks "can I clear this rung fresh?" not "from my current scratches?".
        return _survivable(
            player.max_hp, player.max_hp, player.atk, player.def_, guardian, _DESCEND_MARGIN
        )

    def _descend(self) -> bool:
        """Enter the dungeon and descend one rung (spends a turn).

        Returns False if the dungeon menu can't be entered, so a malformed map
        ends the day instead of spinning. ``advanced`` (a cleared rung) suppresses
        the bounce check, since a cleared rung climbs out rather than bouncing.
        """
        if not self._enter("dungeon"):
            return False
        before = self._player().deepest_rung
        wins_before = self._player().wins
        out = self._act("descend")
        self.descents += 1
        after = self._player().deepest_rung
        self.rungs_cleared = max(self.rungs_cleared, after)
        self._note_outcome(out, wins_before, advanced=after > before)
        self._leave_if_in_menu()
        return True

    def _hunt(self) -> bool:
        """Fight in the best survivable forest zone; True if a bout was fought.

        Returns False when there is no reachable zone the bot can survive, OR
        when the walk to the spot ended in a location MENU (a door swallowed the
        navigation) — in either case the day yields the loop's turn rather than
        burning it. A genuine bout always spends a turn, so a True return makes
        real progress toward the daily cap.
        """
        spot = self._best_hunt_spot()
        if spot is None:
            return False
        self._goto_xy(spot)
        if self._player().mode is not Mode.TILE:
            # A door swallowed the walk; step back out and try once more.
            self._leave_if_in_menu()
            self._goto_xy(spot)
        # Authoritative pre-check: a "fight" issued from a MENU is REJECTED by the
        # engine without spending a turn (it is not a tile action), so counting it
        # as a fought bout would over-count and let the day loop spin to its cap.
        # If still not on open ground, yield the turn — leave the menu and bail.
        if self._player().mode is not Mode.TILE:
            self._leave_if_in_menu()
            return False
        wins_before = self._player().wins
        out = self._act("fight")
        if "nothing stirs to fight here" in out:
            # Standing in TILE mode but outside any zone (navigation fell short):
            # the engine spent no turn, so this is not a fought bout. Give up for
            # now rather than re-rolling the same dry cell forever.
            return False
        self.fights_fought += 1
        self._note_outcome(out, wins_before)
        return True

    # -- outcome bookkeeping ---------------------------------------------

    def _note_outcome(self, out: str, wins_before: int, *, advanced: bool = False) -> None:
        """Fold one bout's result into the accumulators from observable state.

        A Wyrm kill shows as ``wins`` ticking up (the legacy reset bumps it); a
        death-save shows as the engine's death-save line in *out*; a genuine
        bounce shows as the hero standing at the spawn at 1 HP without either of
        the above. ``advanced`` (descend only) suppresses the bounce check on a
        cleared rung, which never bounces. Gold earned is tracked separately,
        per call, in :meth:`_act`.
        """
        player = self._player()
        if player.wins > wins_before:
            self.wins += player.wins - wins_before
            if self.day_of_first_wyrm_kill is None:
                self.day_of_first_wyrm_kill = self._current_day
            return
        if Game._DEATH_SAVE_LINE in out:
            self.deaths_survived += 1
            return
        if not advanced and (player.x, player.y) == self.world.spawn and player.hp <= 1:
            self.deaths_taken += 1

    # -- low-level game driving ------------------------------------------

    def _act(self, action: str, *, target: str = "", item: str = "") -> str:
        """Call ``game.action`` and accrue any positive gold delta as earnings.

        Earnings are POSITIVE inflows only (a fight reward, a forest gold find,
        a dice win), so spending at the shop/inn/forge never counts against the
        total. The before/after read brackets the single façade call, so every
        gold source the engine applies is captured without enumerating them.
        """
        before = self._player().gold
        out = self.game.action(self.name, action, target, item)
        delta = self._player().gold - before
        if delta > 0:
            self.total_gold_earned += delta
        return out

    def _goto(self, waypoint: str) -> None:
        """Navigate to a named waypoint (inn/shop/healer/dungeon door cell)."""
        target = self._waypoints.get(waypoint)
        if target is not None:
            self._goto_xy(target)

    def _enter(self, waypoint: str) -> bool:
        """Ensure the bot stands INSIDE *waypoint*'s menu; return success.

        A location's menu opens only by MOVING onto its door — standing on the
        door cell in TILE mode (e.g. right after leaving) does not reopen it. So
        this walks to a cell ADJACENT to the door and steps in, guaranteeing the
        ``entered_location`` flip to MENU mode. Already in the right menu, it is a
        no-op. This is the entry every town errand uses, so a buy/rest/forge is
        never attempted from the overworld (which would silently no-op and spin).
        """
        door = self._waypoints.get(waypoint)
        if door is None:
            return False
        player = self._player()
        if player.mode is Mode.MENU and player.at_location == waypoint:
            return True
        self._leave_if_in_menu()
        approach = _adjacent_open(self.world, self._walkable, door)
        if approach is None:
            return False
        self._goto_xy(approach)
        if (self._player().x, self._player().y) != approach:
            return False
        step = _step_between(approach, door)
        if step is None:
            return False
        self.game.move(self.name, step, "", 0)
        p = self._player()
        return p.mode is Mode.MENU and p.at_location == waypoint

    def _errand(self, waypoint: str, action: str, *, target: str = "", item: str = "") -> bool:
        """Enter *waypoint*'s menu and run one free town *action*; True if done.

        Returns False when the menu can't be entered (a malformed map), so the
        caller treats the errand as "couldn't help" rather than spinning on a
        no-op the way a TILE-mode buy would. The menu is left afterwards so the
        next decision starts cleanly on the overworld.
        """
        if not self._enter(waypoint):
            return False
        self._act(action, target=target, item=item)
        self._leave_if_in_menu()
        return True

    def _goto_xy(self, goal: tuple[int, int]) -> None:
        """Walk the bot to *goal* over free moves, threading incidental foes.

        Steps the BFS path in 8-cell hops, re-planning from the actual position
        after each hop because a walk can stop early (a wall, a door, or a
        wandering monster). Incidental forest encounters need no handling: a
        blocked move simply makes no progress that hop, and the next hop re-rolls
        from the new cell, so the bot threads the wood without spending a turn.
        A move-count cap prevents an unthreadable map from looping forever.
        """
        moves = 0
        while moves < _GOTO_MAX_MOVES:
            player = self._player()
            if player.mode is Mode.MENU:
                # Already at a door; if it is the goal door, we're there.
                loc = self.world.location_at(*goal)
                if loc is not None and (player.x, player.y) == goal:
                    return
                self._leave_if_in_menu()
                player = self._player()
            if (player.x, player.y) == goal:
                return
            path = _bfs_step_path(self.world, self._walkable, (player.x, player.y), goal)
            if not path:
                return
            steps = "".join(path[:8])
            self.game.move(self.name, steps, "", 0)
            moves += 1

    def _leave_if_in_menu(self) -> None:
        """Step back onto the overworld if the bot is inside a location menu."""
        if self._player().mode is Mode.MENU:
            self.game.action(self.name, "leave", "", "")

    def _heal_full_if_possible(self) -> None:
        """Rest/heal to full before a marquee bout, if at all affordable."""
        if self._player().hp < self._player().max_hp:
            self._recover()

    # -- heuristics over world content -----------------------------------

    def _best_hunt_spot(self) -> tuple[int, int] | None:
        """Pick the highest-tier forest zone whose toughest foe the bot survives.

        Escalates the bot from the starter wood to deeper zones as its gear and
        level grow: it scans zones high tier first and returns the first whose
        toughest COMMON foe a full-health bout clears above the fight margin.
        When nothing yet qualifies (a fresh, under-geared bot), it falls back to
        the LOWEST-tier zone — the starter wood — so the bot always has the
        gentlest available ground to grind on rather than throwing itself at the
        deep. The death-save covers the occasional unlucky bout there.
        """
        player = self._player()
        if not self._hunt_spots:
            return None
        ranked = sorted(self._hunt_spots, key=lambda zs: zs[0], reverse=True)
        for _tier_hi, spot, toughest in ranked:
            if toughest is None:
                continue
            # Full-health yardstick: the bot heals below _REST_BELOW, so "can I
            # win this zone's toughest common foe fresh?" is the right question.
            if _survivable(
                player.max_hp, player.max_hp, player.atk, player.def_, toughest, _FIGHT_MARGIN
            ):
                return spot
        # Nothing clears the margin yet: grind the gentlest (lowest-tier) zone.
        return ranked[-1][1]

    def _rung_guardian(self, rung_index: int) -> Monster | None:
        """Return the fixed guardian of the next rung (``band[0]`` of its tier)."""
        tiers = self.world.settings.dungeon_tiers
        if not 0 <= rung_index < len(tiers):
            return None
        band = self.world.monsters_for_tier_band(tiers[rung_index], tiers[rung_index])
        return band[0] if band else None

    def _best_upgrade(self, slot: Slot, equipped_id: str) -> Item | None:
        """Return the best purchasable *slot* item that beats the equipped one.

        Compares by the slot's base combat bonus (weapon→atk, armour→def): the
        dearest shop item whose bonus exceeds the equipped item's. A None result
        means nothing in the shop improves on what the bot wears.
        """
        equipped = self.world.item_by_id(equipped_id)
        equipped_bonus = self._slot_stat(equipped, slot) if equipped else 0
        best: Item | None = None
        for item in self.world.items:
            if item.slot is not slot or item.price <= 0:
                continue
            if self._slot_stat(item, slot) <= equipped_bonus:
                continue
            if best is None or item.price > best.price:
                best = item
        return best

    def _best_affordable_potion(self) -> Item | None:
        """Return the strongest-heal consumable the bot can currently afford."""
        best: Item | None = None
        for item in self.world.items:
            if item.slot is not Slot.CONSUMABLE or item.price <= 0:
                continue
            if not self._can_afford(item.price):
                continue
            if best is None or item.heal > best.heal:
                best = item
        return best

    def _shop_offers(self, verb: str) -> bool:
        """True when a shop exists AND its menu actually exposes *verb*.

        The bot drives an arbitrary authored pack, whose shop need not list every
        verb the Vale's does: a pack may sell wares but offer no forge, say. The
        engine rejects a verb absent from a location's ``actions`` WITHOUT
        spending a turn or coin, and :meth:`_errand` cannot tell that no-op from a
        real one — so it would report success and the day loop would spin. Gating
        the shop errands on the verb being genuinely on offer closes that spin.
        """
        if "shop" not in self._waypoints:
            return False
        loc = self.world.location_by_key("shop")
        return loc is not None and verb in loc.actions

    def _gear_is_best(self) -> bool:
        """True when both equipped slots are already the shop's strongest."""
        player = self._player()
        return (
            self._best_upgrade(Slot.WEAPON, player.weapon_id) is None
            and self._best_upgrade(Slot.ARMOR, player.armor_id) is None
        )

    def _can_afford(self, price: int) -> bool:
        """True when paying *price* still leaves the rest-cost reserve intact."""
        reserve = self.world.settings.rest_cost * _RESERVE_RESTS
        return self._player().gold - price >= reserve

    @staticmethod
    def _slot_stat(item: Item, slot: Slot) -> int:
        return item.atk if slot is Slot.WEAPON else item.def_

    @staticmethod
    def _dearer(a: Item | None, b: Item | None) -> Item | None:
        """Return whichever upgrade is the dearer (a rough 'bigger jump') pick."""
        if a is None:
            return b
        if b is None:
            return a
        return a if a.price >= b.price else b

    # -- report ----------------------------------------------------------

    def report(self, *, days: int, seed: int) -> BalanceReport:
        """Freeze the run's accumulators into a :class:`BalanceReport`."""
        player = self._player()
        turn_actions = self.fights_fought + self.descents + self.challenges
        fight_share = self.fights_fought / turn_actions if turn_actions else 0.0
        satchel = tuple(f"{item_id}×{qty}" for item_id, qty in self._satchel_stacks(player.satchel))
        return BalanceReport(
            days=days,
            seed=seed,
            final_level=player.level,
            total_gold_earned=self.total_gold_earned,
            deaths_survived=self.deaths_survived,
            deaths_taken=self.deaths_taken,
            rungs_cleared=self.rungs_cleared,
            wyrm_killed=self.wins > 0,
            day_of_first_wyrm_kill=self.day_of_first_wyrm_kill,
            fights_fought=self.fights_fought,
            realized_fight_share=fight_share,
            ending_satchel=satchel,
        )

    # -- tiny accessors --------------------------------------------------

    @property
    def _current_day(self) -> int:
        """The 1-indexed sim-day the clock currently sits on."""
        return (self.clock().date() - _SIM_START.date()).days + 1

    def _player(self) -> Player:
        return self.game.players[self.name]

    @staticmethod
    def _satchel_stacks(satchel: str) -> list[tuple[str, int]]:
        """Decode the player's ``"id:qty"`` satchel into ``(id, qty)`` stacks.

        Delegates to the shared
        :func:`~understone.engine.satchel.decode_satchel` codec, so the bot
        reasons about its own bag (potion reserve, ore on hand) over the same
        parse the game façade uses — without reaching into private façade helpers.
        """
        return decode_satchel(satchel)

    def _satchel_qty(self, item_id: str) -> int:
        """Return how many of *item_id* the bot carries (0 if none)."""
        return sum(
            qty
            for stack_id, qty in self._satchel_stacks(self._player().satchel)
            if stack_id == item_id
        )

    def _potions_carried(self) -> int:
        """Return the total number of consumable draughts in the satchel."""
        total = 0
        for item_id, qty in self._satchel_stacks(self._player().satchel):
            item = self.world.item_by_id(item_id)
            if item is not None and item.slot is Slot.CONSUMABLE:
                total += qty
        return total

    def _turns_left(self) -> int:
        return self._player().turns_left

    def _turn_budget(self) -> int:
        return self.world.settings.daily_turns


# ---------------------------------------------------------------------------
# Combat & navigation helpers (pure functions over world content)
# ---------------------------------------------------------------------------


def _survivable(
    cur_hp: int, max_hp: int, atk: int, def_: int, monster: Monster, margin: float
) -> bool:
    """Project whether a bout from *cur_hp* against *monster* clears *margin*.

    A PESSIMISTIC estimate: the engine jitters each blow by ``randint(-1, 2)``
    (:func:`understone.engine.combat._swing`), so this assumes the player's blows
    land at the low end (``-1``) and the monster's at the high end (``+2``),
    player striking first. The surviving hp is expressed as a fraction of
    ``max_hp`` and compared to *margin*. Because the real bout is usually kinder
    AND the satchel death-save backstops a bad one, a margin-clearing bout is a
    safe turn to spend; the gate errs toward over-preparing, which is what a
    careful greedy probe should do.
    """
    player_dmg = max(1, (atk - 1) - monster.def_)
    monster_dmg = max(1, (monster.atk + 2) - def_)
    rounds_to_kill = -(-monster.hp // player_dmg)  # ceil division
    # The player strikes first, so they take one fewer hit than rounds-to-kill.
    hits_taken = rounds_to_kill - 1
    remaining = cur_hp - hits_taken * monster_dmg
    return remaining >= max_hp * margin


def _reachable(world: World) -> set[tuple[int, int]]:
    """Return every walkable cell reachable from the spawn (a BFS flood).

    Computed once per run so navigation never re-floods. Location doors count as
    walkable, so the town buildings and the dungeon mouth are in the set.
    """
    seen = {world.spawn}
    frontier: deque[tuple[int, int]] = deque([world.spawn])
    while frontier:
        x, y = frontier.popleft()
        for dx, dy in ((0, -1), (0, 1), (1, 0), (-1, 0)):
            nxt = (x + dx, y + dy)
            if nxt not in seen and world.is_walkable(*nxt):
                seen.add(nxt)
                frontier.append(nxt)
    return seen


def _bfs_step_path(
    world: World,
    walkable: set[tuple[int, int]],
    start: tuple[int, int],
    goal: tuple[int, int],
) -> list[str]:
    """Return cardinal steps (N/S/E/W) along a shortest walkable path to *goal*.

    A plain breadth-first search over the precomputed *walkable* set; returns the
    step letters the game's ``move`` accepts, or an empty list when *goal* is
    unreachable (it never is, for a bundled world's town and dungeon, but the
    bot tolerates a malformed pack gracefully). The goal cell itself need not be
    in *walkable* beyond being a location door, which the flood already included.
    """
    if start == goal:
        return []
    came_from: dict[tuple[int, int], tuple[tuple[int, int], str]] = {}
    frontier: deque[tuple[int, int]] = deque([start])
    seen = {start}
    deltas = (((0, -1), "N"), ((0, 1), "S"), ((1, 0), "E"), ((-1, 0), "W"))
    while frontier:
        cur = frontier.popleft()
        if cur == goal:
            return _reconstruct(came_from, start, goal)
        cx, cy = cur
        for (dx, dy), letter in deltas:
            nxt = (cx + dx, cy + dy)
            if nxt in seen or nxt not in walkable:
                continue
            seen.add(nxt)
            came_from[nxt] = (cur, letter)
            frontier.append(nxt)
    return []


def _reconstruct(
    came_from: dict[tuple[int, int], tuple[tuple[int, int], str]],
    start: tuple[int, int],
    goal: tuple[int, int],
) -> list[str]:
    """Walk the BFS parent links back from *goal* to *start* into step letters."""
    steps: list[str] = []
    node = goal
    while node != start:
        prev, letter = came_from[node]
        steps.append(letter)
        node = prev
    steps.reverse()
    return steps


def _named_waypoints(world: World) -> dict[str, tuple[int, int]]:
    """Map each location KEY (inn/shop/healer/dungeon) to its door cell."""
    return {loc.key: (loc.x, loc.y) for loc in world.locations}


_STEP_DELTAS: dict[tuple[int, int], str] = {(0, -1): "N", (0, 1): "S", (1, 0): "E", (-1, 0): "W"}


def _adjacent_open(
    world: World, walkable: set[tuple[int, int]], door: tuple[int, int]
) -> tuple[int, int] | None:
    """Return a walkable, non-location cell orthogonally adjacent to *door*.

    The cell the bot stands on to then step INTO the door (opening its menu). It
    must itself not be another location door, or stepping would enter the wrong
    building. ``None`` only for a door walled in on all four sides, which a
    bundled world never has.
    """
    dx, dy = door
    for ox, oy in ((0, -1), (0, 1), (1, 0), (-1, 0)):
        cell = (dx + ox, dy + oy)
        if cell in walkable and world.location_at(*cell) is None:
            return cell
    return None


def _step_between(start: tuple[int, int], goal: tuple[int, int]) -> str | None:
    """Return the single cardinal step from *start* to an adjacent *goal*."""
    return _STEP_DELTAS.get((goal[0] - start[0], goal[1] - start[1]))


def _zone_hunt_spots(
    world: World, walkable: set[tuple[int, int]]
) -> list[tuple[int, tuple[int, int], Monster | None]]:
    """Return one huntable spot per zone: ``(tier_hi, cell, toughest_foe)``.

    For each zone, the nearest-to-spawn reachable cell inside it (so the bot can
    actually stand there and fight) plus the toughest COMMON (non-rare) foe in
    the zone's band — the survivability yardstick. Rares are excluded from that
    yardstick: they carry a low weight (they surface seldom) and the death-save
    covers an unlucky tough draw, so gating the whole zone on a rare it almost
    never meets would freeze the bot in the starter wood. Zones with no reachable
    cell or no fightable foe are dropped, so every spot is a real hunting ground.
    """
    out: list[tuple[int, tuple[int, int], Monster | None]] = []
    for zone in world.zones:
        cell = _nearest_in_zone(world, walkable, zone)
        if cell is None:
            continue
        band = world.monsters_for_tier_band(zone.tier_lo, zone.tier_hi)
        common = [m for m in band if not m.rare] or band
        toughest = max(common, key=lambda m: m.hp + m.atk) if common else None
        # A zone whose tier band holds no fightable foe is no hunting ground: a
        # fight there only ever yields "nothing stirs". Drop it so the fallback
        # in _best_hunt_spot (ranked[-1]) can never land the bot on a dead zone.
        if toughest is None:
            continue
        out.append((zone.tier_hi, cell, toughest))
    return out


def _nearest_in_zone(
    world: World, walkable: set[tuple[int, int]], zone: object
) -> tuple[int, int] | None:
    """Return the reachable, non-door cell inside *zone* closest to the spawn.

    Location-door cells are walkable (``is_walkable`` returns True for a door),
    but standing on one flips the bot into that location's MENU — useless ground
    for a forest fight. So door cells are skipped here, mirroring the filter in
    :func:`_adjacent_open`, leaving only true open ground the bot can fight on.
    """
    sx, sy = world.spawn
    best: tuple[int, int] | None = None
    best_d = None
    for x, y in walkable:
        if not zone.contains(x, y):  # type: ignore[attr-defined]
            continue
        if world.location_at(x, y) is not None:
            continue
        d = abs(x - sx) + abs(y - sy)
        if best_d is None or d < best_d:
            best_d = d
            best = (x, y)
    return best


# ---------------------------------------------------------------------------
# CLI rendering
# ---------------------------------------------------------------------------


def cli_simulate(
    pack_dir: Path,
    days: int,
    seed: int,
    out: TextIO | None = None,
    seeds: int | None = None,
) -> int:
    """Run the bot over *pack_dir* and print a readable balance report; return 0.

    With *seeds* unset (or 1) this runs a single seed and prints its full
    report. With ``seeds=K`` it runs seeds ``seed .. seed+K-1`` and prints an
    AGGREGATE: per-seed one-liners plus the mean and spread of the headline
    measures across the sweep — the form an author reads to judge whether a
    world is reliably winnable and sanely paced, not just lucky on one seed.
    """
    import sys

    out = out if out is not None else sys.stdout
    world = load_world(pack_dir)
    count = seeds if seeds and seeds > 1 else 1
    reports = [simulate(pack_dir, days, seed + i) for i in range(count)]
    if count == 1:
        print(_render_report(world.name, reports[0]), file=out)
    else:
        print(_render_sweep(world.name, days, reports), file=out)
    return 0


def _render_report(world_name: str, r: BalanceReport) -> str:
    """Render a single-seed report as an aligned, human-readable block."""
    wyrm = f"yes (first on day {r.day_of_first_wyrm_kill})" if r.wyrm_killed else "no"
    satchel = ", ".join(r.ending_satchel) if r.ending_satchel else "(empty)"
    lines = [
        f"{world_name} — greedy bot, {r.days} days, seed {r.seed}",
        f"  final level       : {r.final_level}  "
        "(level at the final bell; a Wyrm kill resets to 1 — read with Wyrm slain)",
        f"  gold earned       : {r.total_gold_earned}",
        f"  fights fought     : {r.fights_fought}",
        f"  fight share       : {r.realized_fight_share:.0%} of turn-actions",
        f"  rungs cleared     : {r.rungs_cleared}",
        f"  death-saves       : {r.deaths_survived} survived, {r.deaths_taken} taken",
        f"  Wyrm slain        : {wyrm}",
        f"  ending satchel    : {satchel}",
    ]
    return "\n".join(lines)


def _render_sweep(world_name: str, days: int, reports: list[BalanceReport]) -> str:
    """Render a multi-seed sweep: per-seed lines, then means and spreads."""
    lines = [f"{world_name} — greedy bot sweep, {days} days, {len(reports)} seeds", ""]
    for r in reports:
        kill = f"day {r.day_of_first_wyrm_kill}" if r.wyrm_killed else "—"
        lines.append(
            f"  seed {r.seed:>3}: Lv{r.final_level:<3} "
            f"gold {r.total_gold_earned:>6}  fights {r.fights_fought:>3}  "
            f"rungs {r.rungs_cleared}  Wyrm {kill}"
        )
    lines.append("")
    kills = sum(1 for r in reports if r.wyrm_killed)
    kill_days = [r.day_of_first_wyrm_kill for r in reports if r.day_of_first_wyrm_kill is not None]
    lines.append("  aggregate (mean [min..max]):")
    lines.append(
        f"    final level   : {_stat_line(r.final_level for r in reports)}  "
        "(level at the final bell; a Wyrm kill resets to 1 — read with Wyrm slain)"
    )
    lines.append(f"    gold earned   : {_stat_line(r.total_gold_earned for r in reports)}")
    lines.append(f"    fights fought : {_stat_line(r.fights_fought for r in reports)}")
    lines.append(f"    fight share   : {_mean(r.realized_fight_share for r in reports):.0%}")
    lines.append(f"    rungs cleared : {_stat_line(r.rungs_cleared for r in reports)}")
    lines.append(f"    Wyrm slain    : {kills}/{len(reports)} seeds")
    if kill_days:
        lines.append(f"    first kill day: {_stat_line(iter(kill_days))}")
    return "\n".join(lines)


def _stat_line(values: Iterable[int]) -> str:
    """Render ``mean [min..max]`` for an integer measure across the sweep."""
    data = list(values)
    if not data:
        return "—"
    return f"{sum(data) / len(data):.1f} [{min(data)}..{max(data)}]"


def _mean(values: Iterable[float]) -> float:
    """Return the arithmetic mean of *values* (0.0 when empty)."""
    data = list(values)
    return sum(data) / len(data) if data else 0.0
