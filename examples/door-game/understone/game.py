"""The game façade — the single seam between the MCP layer and everything else.

``Game`` owns the world, the store, the clock, and the master RNG. It holds
the persistence policy (state-changing actions update the cache and commit
in one transaction; reads never commit), applies engine results to the
durable :class:`Player`, and renders every reply into a finished string
(frame or menu, narration lines, then the status footer).

The MCP layer calls these methods and returns their strings verbatim; it
never reaches past this façade into the engine, screen, or store.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from understone.engine import combat, leveling, movement, turns
from understone.engine.log import Event, since
from understone.engine.models import Mode, Player, Slot
from understone.engine.rank import RankEntry, leaderboard
from understone.engine.rng import GameRNG
from understone.persistence import EVENT_TAIL_KEEP
from understone.screen.grid import Cell, CellGrid
from understone.screen.menus import render_menu
from understone.screen.palette import Color
from understone.screen.text_renderer import render_frame
from understone.screen.viewport import compute_window

if TYPE_CHECKING:
    from collections.abc import Callable

    from understone.engine.models import Item, LocationDef, Monster
    from understone.engine.world import World
    from understone.persistence import Store

VIEW_W = 48
VIEW_H = 16

# Free-text length ceilings for player-authored input (the sanitizer chokepoint).
_NAME_MAX_LEN = 24
_REASON_MAX_LEN = 120

_COLOR_BY_NAME = {c.value: c for c in Color}


class Game:
    """Stateful coordinator over a single shared world."""

    def __init__(
        self,
        world: World,
        store: Store,
        *,
        clock: Callable[[], datetime] | None = None,
        rng: GameRNG | None = None,
    ) -> None:
        self.world = world
        self.store = store
        self.clock = clock or (lambda: datetime.now(UTC))
        self.rng = rng or GameRNG()
        players, events = store.load_all()
        self.players: dict[str, Player] = players
        self.events: list[Event] = events
        # The world is immutable, so the shop's stock is fixed for the run.
        self._purchasable: frozenset[str] = frozenset(
            it.item_id for it in world.items if it.price > 0
        )
        store.set_meta("world_name", world.name)

    # -- helpers ---------------------------------------------------------

    def _now_iso(self) -> str:
        return self.clock().isoformat()

    def _get(self, name: str) -> Player | None:
        return self.players.get(name.strip())

    def _unknown(self, name: str) -> str:
        return f"No adventurer named {name.strip()!r} has signed the ledger. Try door_join first."

    @staticmethod
    def _sanitize(text: str, max_len: int) -> str | None:
        """Return *text* stripped, or ``None`` if it fails free-text hygiene.

        Rejects empty input, anything longer than *max_len*, and any string
        carrying a non-printable or control character (``\\n``, ``\\r``,
        ``\\t`` included — ``str.isprintable`` treats them all as unprintable).
        This is the single chokepoint for player-authored free text reaching
        the durable store and the public log.
        """
        cleaned = text.strip()
        if not cleaned or len(cleaned) > max_len or not cleaned.isprintable():
            return None
        return cleaned

    def _footer(self, player: Player) -> str:
        nxt = leveling.xp_for_level(player.level + 1, self.world.settings)
        return (
            f"[ {player.name}  Lv{player.level}  HP {player.hp}/{player.max_hp}  "
            f"XP {player.xp}/{nxt}  Gold {player.gold}  "
            f"Turns {player.turns_left}/{self.world.settings.daily_turns} ]"
        )

    def _ensure_day(self, player: Player) -> None:
        if turns.ensure_day(player, self.clock, self.world.settings.daily_turns):
            player.last_seen = self._now_iso()

    def _persist(self, player: Player, *events: tuple[str, str, str]) -> None:
        """Update + append in one transaction, then commit (state changes).

        Each event tuple is ``(kind, actor, text)``. New events are appended
        to the in-memory feed with the id the store assigns.
        """
        player.last_seen = self._now_iso()
        self.store.upsert_player(player)
        ts = self._now_iso()
        for kind, actor, text in events:
            event_id = self.store.insert_event(ts, actor, kind, text)
            self.events.append(Event(event_id=event_id, ts=ts, kind=kind, actor=actor, text=text))
        # Full history lives in SQLite; keep only the recent tail resident.
        if len(self.events) > EVENT_TAIL_KEEP:
            del self.events[:-EVENT_TAIL_KEEP]
        self.store.commit()

    # -- rendering -------------------------------------------------------

    def _cell_for_terrain(self, x: int, y: int) -> Cell:
        terrain = self.world.terrain_at(x, y)
        color = _COLOR_BY_NAME.get(terrain.color, Color.DEFAULT)
        loc = self.world.location_at(x, y)
        if loc is not None:
            loc_color = _COLOR_BY_NAME.get(loc.color, Color.TOWN)
            return Cell(loc.glyph, loc_color)
        return Cell(terrain.glyph, color)

    def _paint_viewport(self, player: Player) -> CellGrid:
        x0, y0 = compute_window(
            self.world.width, self.world.height, VIEW_W, VIEW_H, player.x, player.y
        )
        grid = CellGrid(VIEW_H, VIEW_W)
        for row in range(VIEW_H):
            for col in range(VIEW_W):
                mx, my = x0 + col, y0 + row
                if self.world.in_bounds(mx, my):
                    grid.set(row, col, self._cell_for_terrain(mx, my))
        # Other players first, so the caller's own marker always wins overlap.
        for other in self.players.values():
            if other.name == player.name or other.mode is not Mode.TILE:
                continue
            self._mark(grid, x0, y0, other.x, other.y, Cell("&", Color.OTHER_PLAYER))
        self._mark(grid, x0, y0, player.x, player.y, Cell("@", Color.PLAYER))
        return grid

    @staticmethod
    def _mark(grid: CellGrid, x0: int, y0: int, mx: int, my: int, cell: Cell) -> None:
        col, row = mx - x0, my - y0
        if 0 <= row < grid.rows and 0 <= col < grid.cols:
            grid.set(row, col, cell)

    def _overworld_frame(self, player: Player, *, lines: list[str] | None = None) -> str:
        grid = self._paint_viewport(player)
        title = self.world.name
        frame = render_frame(grid, title=title, status=self._footer(player))
        if lines:
            return frame + "\n" + "\n".join(lines)
        return frame

    def _location_menu(self, player: Player, *, lines: list[str] | None = None) -> str:
        loc = self.world.location_by_key(player.at_location)
        if loc is None:
            # Defensive: a stale location key drops the player back outdoors.
            player.mode = Mode.TILE
            player.at_location = ""
            return self._overworld_frame(player, lines=["The doorway fades; you stand outside."])
        body = list(loc.flavor)
        if lines:
            body.append("")
            body.extend(lines)
        options = [self._verb_label(loc, a) for a in loc.actions]
        return render_menu(loc.name, body, options, self._footer(player))

    @staticmethod
    def _verb_label(loc: LocationDef, action: str) -> str:
        return f"({action[0].upper()}){action[1:]}"

    # -- tool: join ------------------------------------------------------

    def join(self, name: str) -> str:
        """Create a new adventurer, or resume an existing one by name."""
        clean = self._sanitize(name, _NAME_MAX_LEN)
        if clean is None:
            if len(name.strip()) > _NAME_MAX_LEN:
                return "The ledger is narrow — choose a name of 24 letters or fewer."
            return "The gatekeeper squints at those strange runes. Plain letters, traveller."
        existing = self._get(clean)
        if existing is not None:
            self._ensure_day(existing)
            self.store.upsert_player(existing)
            self.store.commit()
            banner = f"Welcome back to {self.world.name}, {existing.name}."
            return self._overworld_frame(existing, lines=[banner])

        settings = self.world.settings
        weapon = self.world.item_by_id(settings.starting_weapon)
        armor = self.world.item_by_id(settings.starting_armor)
        atk = 3 + (weapon.atk if weapon else 0)
        def_ = 0 + (armor.def_ if armor else 0)
        now = self._now_iso()
        today = self.clock().toordinal()
        player = Player(
            name=clean,
            x=self.world.spawn[0],
            y=self.world.spawn[1],
            hp=20,
            max_hp=20,
            level=1,
            xp=0,
            gold=settings.starting_gold,
            atk=atk,
            def_=def_,
            weapon_id=settings.starting_weapon,
            armor_id=settings.starting_armor,
            turns_left=settings.daily_turns,
            turn_day=today,
            mode=Mode.TILE,
            at_location="",
            created_at=now,
            last_seen=now,
            log_cursor=self._latest_event_id(),
            bestow_spent=0,
            bestow_day=today,
        )
        self.players[clean] = player
        self._persist(
            player,
            ("join", clean, f"{clean} stepped into {self.world.name} for the first time."),
        )
        banner = (
            f"You arrive in {self.world.name}, {clean}. A road runs east from the town.\n"
            "New here? Call door_help to learn how the world is run."
        )
        return self._overworld_frame(player, lines=[banner])

    def _latest_event_id(self) -> int:
        return self.events[-1].event_id if self.events else 0

    # -- tool: status ----------------------------------------------------

    def status(self, name: str) -> str:
        """Return the character sheet (read-only)."""
        player = self._get(name)
        if player is None:
            return self._unknown(name)
        weapon = self.world.item_by_id(player.weapon_id)
        armor = self.world.item_by_id(player.armor_id)
        lines = [
            f"Adventurer: {player.name}",
            f"Level {player.level}   XP {player.xp}",
            f"HP {player.hp}/{player.max_hp}   ATK {player.atk}   DEF {player.def_}",
            f"Weapon: {weapon.name if weapon else player.weapon_id}",
            f"Armor:  {armor.name if armor else player.armor_id}",
            f"Gold {player.gold}   Turns {player.turns_left}/{self.world.settings.daily_turns}",
        ]
        return "\n".join(lines) + "\n" + self._footer(player)

    # -- tool: look ------------------------------------------------------

    def look(self, name: str) -> str:
        """Render the current view (overworld frame or location menu)."""
        player = self._get(name)
        if player is None:
            return self._unknown(name)
        if player.mode is Mode.MENU:
            return self._location_menu(player)
        return self._overworld_frame(player)

    # -- tool: move ------------------------------------------------------

    def move(self, name: str, steps: str, heading: str, distance: int) -> str:
        """Walk the overworld (free). Movement is blocked while in a menu."""
        player = self._get(name)
        if player is None:
            return self._unknown(name)
        if player.mode is Mode.MENU:
            loc = self.world.location_by_key(player.at_location)
            where = loc.name if loc else "building"
            return self._location_menu(
                player,
                lines=[f"You are inside the {where}. (use door_action with 'leave' to step out)"],
            )

        try:
            result = movement.resolve_move(
                self.world, player, self.rng, steps=steps, heading=heading, distance=distance
            )
        except ValueError as exc:
            return self._overworld_frame(player, lines=[f"You hesitate: {exc}."])

        lines = self._narrate_move(result)
        # A move changes position; persist even though it spends no turn.
        self._persist(player)
        return self._overworld_frame(player, lines=lines)

    def _narrate_move(self, result: movement.MoveResult) -> list[str]:
        lines: list[str] = []
        if result.steps_taken == 0 and result.blocked:
            lines.append(f"You can't go that way — {result.blocked_reason} bars the path.")
            return lines
        if result.steps_taken:
            cells = "step" if result.steps_taken == 1 else "steps"
            lines.append(f"You travel {result.steps_taken} {cells}.")
        if result.entered_location:
            loc = self.world.location_by_key(result.entered_location)
            if loc is not None:
                lines.append(f"You reach {loc.name} and step inside.")
        elif result.blocked:
            lines.append(f"Your way is blocked by {result.blocked_reason}.")
        if result.pending_fight is not None:
            lines.append("Something blocks your path, snarling. (door_action: fight  or  flee)")
        return lines

    # -- tool: action ----------------------------------------------------

    def action(self, name: str, action: str, target: str, item: str) -> str:
        """Dispatch a context verb against the player's current surface."""
        player = self._get(name)
        if player is None:
            return self._unknown(name)
        verb = action.strip().lower()

        if player.mode is Mode.TILE:
            return self._tile_action(player, verb)
        return self._menu_action(player, verb, item)

    # -- tile-context actions (fight / flee) -----------------------------

    def _tile_action(self, player: Player, verb: str) -> str:
        if verb in {"fight", "flee"}:
            return self._resolve_encounter(player, verb)
        legal = "fight, flee, or move on with door_move"
        return self._overworld_frame(
            player, lines=[f"There's nothing to '{verb}' out here. You can {legal}."]
        )

    def _pick_monster(self, player: Player) -> Monster | None:
        zone = self.world.zone_for(player.x, player.y)
        if zone is None:
            return None
        band = self.world.monsters_for_tier_band(zone.tier_lo, zone.tier_hi)
        if not band:
            return None
        return band[self.rng.choice_index(len(band))]

    def _resolve_encounter(self, player: Player, verb: str) -> str:
        self._ensure_day(player)
        monster = self._pick_monster(player)
        if monster is None:
            return self._overworld_frame(
                player, lines=["The air is still — nothing stirs to fight here."]
            )

        if not turns.spend_turn(player):
            self.store.upsert_player(player)
            self.store.commit()
            return self._overworld_frame(
                player,
                lines=["You're spent for today. Rest at the inn and return tomorrow."],
            )

        child = self.rng.child()
        if verb == "flee":
            result = combat.resolve_flee(child, player, monster)
        else:
            result = combat.resolve_fight(child, player, monster)
        return self._apply_fight(player, result)

    def _apply_fight(self, player: Player, result: combat.FightResult) -> str:
        lines = list(result.log)
        events: list[tuple[str, str, str]] = []

        player.hp = max(1, player.hp + result.hp_delta)
        if result.gold_delta:
            player.gold += result.gold_delta
        if result.xp_delta:
            gains = leveling.apply_xp(player, result.xp_delta, self.world.settings)
            for gain in gains:
                lines.append(
                    f"You reach level {gain.new_level}! "
                    f"(+{gain.hp_gain} HP, +{gain.atk_gain} ATK, +{gain.def_gain} DEF)"
                )
                events.append(
                    ("level", player.name, f"{player.name} reached level {gain.new_level}.")
                )

        if result.bounce_to_spawn:
            player.hp = 1
            player.x, player.y = self.world.spawn
            events.append(
                ("defeat", player.name, f"{player.name} was felled by a {result.monster_name}.")
            )
        elif result.outcome is combat.Outcome.WIN:
            events.append(
                ("victory", player.name, f"{player.name} bested a {result.monster_name}.")
            )

        self._persist(player, *events)
        return self._overworld_frame(player, lines=lines)

    # -- menu-context actions --------------------------------------------

    def _menu_action(self, player: Player, verb: str, item: str) -> str:
        loc = self.world.location_by_key(player.at_location)
        if loc is None:
            player.mode = Mode.TILE
            player.at_location = ""
            self._persist(player)
            return self._overworld_frame(player, lines=["You step back outside."])

        if verb not in loc.actions:
            legal = ", ".join(loc.actions)
            return self._location_menu(player, lines=[f"You can't '{verb}' here. Try: {legal}."])

        if verb == "leave":
            return self._leave(player)
        if verb == "rest":
            return self._rest(player)
        if verb == "heal":
            return self._heal(player)
        if verb == "buy":
            return self._buy(player, item)
        if verb == "sell":
            return self._sell(player)
        if verb == "descend":
            return self._descend(player)
        return self._location_menu(player, lines=[f"The '{verb}' option isn't ready."])

    def _leave(self, player: Player) -> str:
        player.mode = Mode.TILE
        player.at_location = ""
        self._persist(player)
        return self._overworld_frame(player, lines=["You step back out into the open air."])

    def _rest(self, player: Player) -> str:
        cost = self.world.settings.rest_cost
        if leveling.rest(player, cost):
            self._persist(
                player, ("rest", player.name, f"{player.name} slept the night at the inn.")
            )
            return self._location_menu(
                player, lines=[f"You sleep deeply and wake at full health. (-{cost} gold)"]
            )
        return self._location_menu(
            player, lines=[f"You can't afford the {cost}-gold bed. (You have {player.gold}.)"]
        )

    def _heal(self, player: Player) -> str:
        per_hp = self.world.settings.heal_cost_per_hp
        missing = player.max_hp - player.hp
        if missing <= 0:
            return self._location_menu(player, lines=["You are already hale and whole."])
        outcome = leveling.heal(player, missing, per_hp)
        if outcome.healed <= 0:
            return self._location_menu(
                player,
                lines=[f"You haven't the coin for healing ({per_hp} gold per point)."],
            )
        self._persist(player, ("heal", player.name, f"{player.name} was mended at the shrine."))
        return self._location_menu(
            player,
            lines=[f"The keeper restores {outcome.healed} HP. (-{outcome.cost} gold)"],
        )

    def _buy(self, player: Player, item_id: str) -> str:
        wanted = item_id.strip()
        if not wanted:
            return self._location_menu(player, lines=self._stock_lines())
        item = self.world.item_by_id(wanted)
        if item is None or item.item_id not in self._purchasable:
            return self._location_menu(player, lines=["No such wares here.", *self._stock_lines()])
        if player.gold < item.price:
            return self._location_menu(
                player,
                lines=[f"The {item.name} costs {item.price}; you hold {player.gold}."],
            )
        player.gold -= item.price
        line = self._equip_or_quaff(player, item)
        self._persist(player, ("buy", player.name, f"{player.name} bought {item.name}."))
        return self._location_menu(player, lines=[line])

    def _equip_or_quaff(self, player: Player, item: Item) -> str:
        if item.slot is Slot.WEAPON:
            player.atk += item.atk - self._equipped_bonus(player.weapon_id, Slot.WEAPON)
            player.weapon_id = item.item_id
            return f"You take up the {item.name}. (-{item.price} gold)"
        if item.slot is Slot.ARMOR:
            player.def_ += item.def_ - self._equipped_bonus(player.armor_id, Slot.ARMOR)
            player.armor_id = item.item_id
            return f"You don the {item.name}. (-{item.price} gold)"
        before = player.hp
        player.hp = min(player.max_hp, player.hp + item.heal)
        return (
            f"You quaff the {item.name}, recovering {player.hp - before} HP. (-{item.price} gold)"
        )

    def _equipped_bonus(self, item_id: str, slot: Slot) -> int:
        item = self.world.item_by_id(item_id)
        if item is None or item.slot is not slot:
            return 0
        return item.atk if slot is Slot.WEAPON else item.def_

    def _sell(self, player: Player) -> str:
        weapon = self.world.item_by_id(player.weapon_id)
        # The starter blade is never sellable, whatever a pack prices it at —
        # otherwise re-buying it would mint free gold on every cycle.
        if (
            weapon is None
            or weapon.price <= 0
            or player.weapon_id == self.world.settings.starting_weapon
        ):
            return self._location_menu(player, lines=["You have nothing worth selling back."])
        refund = weapon.price // 2
        player.gold += refund
        starter = self.world.settings.starting_weapon
        fallback = self.world.item_by_id(starter)
        player.atk -= weapon.atk - (fallback.atk if fallback else 0)
        player.weapon_id = starter
        self._persist(player, ("sell", player.name, f"{player.name} sold a {weapon.name}."))
        return self._location_menu(
            player,
            lines=[
                f"You sell the {weapon.name} for {refund} gold and fall back to your old blade."
            ],
        )

    def _stock_lines(self) -> list[str]:
        lines = ["Wares (buy by id):"]
        for item in self.world.items:
            if item.price <= 0:
                continue
            stat = self._stat_blurb(item)
            lines.append(f"  {item.item_id:<14} {item.price:>4}g  {item.name} {stat}")
        lines.append("Buying weapon/armour equips it; potions are drunk at once.")
        return lines

    @staticmethod
    def _stat_blurb(item: Item) -> str:
        if item.slot is Slot.WEAPON:
            return f"(+{item.atk} ATK)"
        if item.slot is Slot.ARMOR:
            return f"(+{item.def_} DEF)"
        return f"(+{item.heal} HP)"

    def _descend(self, player: Player) -> str:
        """Run the dungeon gauntlet: one foe per configured tier, back to back."""
        self._ensure_day(player)
        if not turns.spend_turn(player):
            return self._location_menu(
                player,
                lines=["You're too weary to descend today. Return tomorrow."],
            )
        lines = ["You descend into the Understone Deep..."]
        for tier in self.world.settings.dungeon_tiers:
            band = self.world.monsters_for_tier_band(tier, tier)
            if not band:
                continue
            # band[0] on purpose: the boss ladder is a fixed, repeatable endgame
            # encounter (the the classic door games fixed-dragon convention), never randomized.
            monster = band[0]
            result = combat.resolve_fight(self.rng.child(), player, monster)
            lines.append("")
            lines.extend(result.log)
            player.hp = max(1, player.hp + result.hp_delta)
            if result.bounce_to_spawn:
                player.hp = 1
                player.x, player.y = self.world.spawn
                player.mode = Mode.TILE
                player.at_location = ""
                self._persist(
                    player,
                    ("defeat", player.name, f"{player.name} fell in the Deep to a {monster.name}."),
                )
                return self._overworld_frame(player, lines=lines)
            if result.gold_delta:
                player.gold += result.gold_delta
            if result.xp_delta:
                for gain in leveling.apply_xp(player, result.xp_delta, self.world.settings):
                    lines.append(
                        f"You reach level {gain.new_level}! "
                        f"(+{gain.hp_gain} HP, +{gain.atk_gain} ATK, +{gain.def_gain} DEF)"
                    )
        lines.append("")
        lines.append("You climb back to the surface, victorious and laden.")
        self._persist(
            player, ("descend", player.name, f"{player.name} cleared the Understone Deep.")
        )
        return self._location_menu(player, lines=lines)

    # -- tool: log -------------------------------------------------------

    def log(self, name: str) -> str:
        """Report events since the player's cursor, then advance it."""
        player = self._get(name)
        if player is None:
            return self._unknown(name)
        fresh, new_cursor = since(self.events, player.log_cursor)
        if not fresh:
            return "All quiet since your last visit.\n" + self._footer(player)
        lines = ["While you were away:"]
        lines.extend(f"  - {event.text}" for event in fresh)
        player.log_cursor = new_cursor
        self._persist(player)
        return "\n".join(lines) + "\n" + self._footer(player)

    # -- tool: rank ------------------------------------------------------

    def rank(self, name: str) -> str:
        """Render the top-10 leaderboard, marking the caller's row."""
        entries = [
            RankEntry(name=p.name, level=p.level, xp=p.xp, gold=p.gold)
            for p in self.players.values()
        ]
        top = leaderboard(entries, limit=10)
        caller = name.strip()
        lines = _render_rank_table(top, caller)
        player = self._get(caller)
        footer = self._footer(player) if player is not None else "[ The Vale of Understone ]"
        return "\n".join(lines) + "\n" + footer

    # -- tool: bestow ----------------------------------------------------

    def bestow(self, name: str, reason: str, gold: int, heal: int) -> str:
        """Grant discretionary gold and/or healing from the daily fortune pool.

        Never grants items or turns. Heals only the missing portion and
        charges the pool only for the gold and the HP actually applied.
        Refuses (without mutation) when the request would overrun the
        player's remaining daily budget.
        """
        player = self._get(name)
        if player is None:
            return self._unknown(name)

        clean_reason = self._sanitize(reason, _REASON_MAX_LEN)
        if clean_reason is None:
            return "The fates require a plainly-spoken reason.\n" + self._footer(player)
        if gold < 0 or heal < 0:
            return "Bestowals cannot be negative.\n" + self._footer(player)
        if gold == 0 and heal == 0:
            return "Bestow at least some gold or healing, or not at all.\n" + self._footer(player)

        self._ensure_day(player)
        per_hp = self.world.settings.heal_cost_per_hp
        missing = player.max_hp - player.hp
        heal_applied = max(0, min(heal, missing))
        # A heal-only grant at full HP applies nothing and costs nothing; refuse
        # it before any mutation rather than persist an empty "Fortune favours" line.
        if gold == 0 and heal_applied == 0:
            return (
                f"{player.name} is already hale — the fates see nothing to grant.\n"
                + self._footer(player)
            )
        cost = gold + heal_applied * per_hp

        budget = self.world.settings.bestow_daily_budget
        remaining = budget - player.bestow_spent
        if cost > remaining:
            return (
                f"The fates allow only {max(remaining, 0)} more gold of fortune today "
                f"(this would cost {cost}). Spend it on the rarest moments.\n"
                + self._footer(player)
            )

        player.gold += gold
        if heal_applied:
            player.hp += heal_applied
        player.bestow_spent += cost

        parts: list[str] = []
        if gold:
            parts.append(f"+{gold} gold")
        if heal_applied:
            parts.append(f"+{heal_applied} HP")
        granted = " and ".join(parts)
        text = f"Fortune favours {player.name}: {granted} — {clean_reason}"
        self._persist(player, ("bestow", player.name, text))
        confirm = (
            f"A bestowal falls upon {player.name}: {granted}.\n"
            f"Reason: {clean_reason}\n"
            f"Fortune pool: {player.bestow_spent}/{budget} spent today."
        )
        return confirm + "\n" + self._footer(player)


def _render_rank_table(entries: list[RankEntry], caller: str) -> list[str]:
    """Render a box-drawing leaderboard table, marking the caller's row."""
    header = "  #  Adventurer            Lv     XP   Gold"
    width = len(header) + 2
    top = "┌" + "─" * width + "┐"
    bottom = "└" + "─" * width + "┘"
    lines = [top, "│ " + "The Roll of Heroes".center(width - 1) + "│", "│" + "─" * width + "│"]
    lines.append("│ " + header.ljust(width - 1) + "│")
    if not entries:
        lines.append("│ " + "(no heroes have ventured yet)".ljust(width - 1) + "│")
    for i, entry in enumerate(entries, start=1):
        mark = "*" if entry.name == caller else " "
        row = f"{mark}{i:>2}  {entry.name:<20.20} {entry.level:>2} {entry.xp:>6} {entry.gold:>6}"
        lines.append("│ " + row.ljust(width - 1) + "│")
    lines.append(bottom)
    return lines
