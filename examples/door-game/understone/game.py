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
from understone.engine.log import Event, since_visible
from understone.engine.models import Mode, Monster, Player, Slot
from understone.engine.rank import HallEntry, RankEntry, leaderboard
from understone.engine.rng import GameRNG
from understone.engine.satchel import decode_satchel, encode_satchel
from understone.engine.textwidth import is_grid_safe
from understone.persistence import EVENT_TAIL_KEEP
from understone.screen.grid import Cell, CellGrid
from understone.screen.menus import render_menu
from understone.screen.palette import Color
from understone.screen.text_renderer import render_frame
from understone.screen.texture import textured
from understone.screen.viewport import compute_window

if TYPE_CHECKING:
    from collections.abc import Callable

    from understone.engine.models import Item, LocationDef
    from understone.engine.world import World
    from understone.persistence import Store

VIEW_W = 48
VIEW_H = 16

# One feed write: ``(kind, actor, text, target)``. An empty target is a PUBLIC
# Herald beat; a player name is a PRIVATE note only that player reads.
EventSpec = tuple[str, str, str, str]

# Free-text length ceilings for player-authored input (the sanitizer chokepoint).
_NAME_MAX_LEN = 24
_REASON_MAX_LEN = 120

# A dice win at or above this many gold is loud enough to reach the Herald.
_GAMBLE_HERALD_MIN = 25

_COLOR_BY_NAME = {c.value: c for c in Color}

# The Understone Herald — the public feed's masthead and its "all quiet" line.
_HERALD_HEADER = "═══ The Understone Herald ═══"
_HERALD_QUIET = "The Vale is still; the Herald has no fresh word for you."

# Public-feed write templates, 2-3 phrasings per notable kind, picked per write
# so the broadsheet reads with period variety. Only NOTABLE beats reach the feed
# (joins, blessings, level-ups, defeats, and the Wyrm's fate); routine town
# errands stay private. Player names are sanitised at join and monster names come
# from the validated pack, so interpolation here is safe.
_HERALD_TEMPLATES: dict[str, tuple[str, ...]] = {
    # --- PUBLIC (via _herald): everyone reads these on the broadsheet ---
    "join": (
        "{name} has signed the ledger and set out into the Vale.",
        "A new adventurer, {name}, arrives at the western gate.",
        "Word spreads of {name}, newly come to seek their fortune.",
    ),
    "bestow": (
        "Fortune favours {name}: {granted} — {reason}",
        "A boon falls upon {name}: {granted}. ({reason})",
        "The fates smile on {name} with {granted} — {reason}",
    ),
    "level_up": (
        "{name} now strides the Vale at level {n}.",
        "Hardened by trials, {name} rises to level {n}.",
        "{name} has grown in renown, reaching level {n}.",
    ),
    "defeat": (
        "{name} was dragged back to town by a {monster}.",
        "A {monster} bested {name}, who limped home to lick their wounds.",
        "{name} fell to a {monster} and woke at the spawn-stone.",
    ),
    "wyrm_win": (
        "THE WYRM IS SLAIN! {name} has freed the Vale!",
        "THE WYRM BELOW IS DEAD! {name} stands triumphant over its ruin!",
    ),
    "wyrm_lose": (
        "{name} was devoured by the Wyrm Below.",
        "The Wyrm Below swallowed {name} whole; the Vale mourns.",
    ),
    "wyrm_flee": (
        "{name} fled the Wyrm Below, alive but unproven.",
        "{name} broke from the Wyrm Below and ran for the light.",
    ),
    # Ambush — the asynchronous player-kill. These win/shame/flee beats crow on
    # the public feed; the victim's own private alert is the "ambushed" kind below.
    "ambush": (
        "{name} fell upon {target} as they slept — and made off with {steal} gold!",
        "Under cover of dawn {name} robbed the sleeping {target} of {steal} gold!",
        "{target} slept too long; {name} crept in and lifted {steal} gold!",
    ),
    "ambush_shame": (
        "{target} woke blade-in-hand; {name} fled bleeding.",
        "{name} misjudged the sleeper: {target} woke and sent them running.",
    ),
    "ambush_flee": (
        "{name} crept up on {target} but lost their nerve and slipped away.",
        "{name} thought better of robbing {target} and melted into the dark.",
    ),
    "gamble": (
        "{name} took the house for {amount} gold at dice!",
        "The dice ran hot for {name} — {amount} gold off the house!",
    ),
    # A rare named beast has fallen — loud enough for the whole Vale to hear.
    "rare_kill": (
        "A rare {monster} has fallen to {name}!",
        "{name} has slain the rare {monster} — a deed for the songs!",
        "Word races the Vale: {name} felled the rare {monster}!",
    ),
    # --- PRIVATE (via _mail): only the named target reads these ---
    "ambushed": ("While you slept: {name} ambushed you — {steal} gold stolen.",),
    "post": ("{name} left word for {target}: {text}",),
}


def _is_narrow_text(text: str) -> bool:
    """Return whether every code point in *text* fits a single ledger column.

    True only when each character is grid-safe — one printable column, no
    fullwidth rune, no combining mark. Spaces qualify (they are narrow), so
    multi-word reasons and mail pass; a CJK ideograph, an emoji, a fullwidth
    letter, or a decomposed accent does not.
    """
    return all(is_grid_safe(ch) for ch in text)


class Game:
    """Stateful coordinator over a single shared world."""

    def __init__(
        self,
        world: World,
        store: Store,
        *,
        clock: Callable[[], datetime] | None = None,
        rng: GameRNG | None = None,
        watch_url: str | None = None,
    ) -> None:
        self.world = world
        self.store = store
        self.clock = clock or (lambda: datetime.now(UTC))
        self.rng = rng or GameRNG()
        # When the http transport is up, this is the spectator page URL; the
        # join banner and the help manual advertise it. None under stdio.
        self.watch_url = watch_url
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

    def watch_line(self) -> str:
        """Return the 'Watch the Vale live' advertisement, or '' when no URL.

        Surfaced by ``join`` (appended to its banner) and by the server's
        ``door_help`` manual. Empty under stdio, where there is no page to view.
        """
        return f"Watch the Vale live: {self.watch_url}" if self.watch_url else ""

    def _get(self, name: str) -> Player | None:
        return self.players.get(name.strip())

    def _unknown(self, name: str) -> str:
        return f"No adventurer named {name.strip()!r} has signed the ledger. Try door_join first."

    @staticmethod
    def _sanitize(text: str, max_len: int) -> str | None:
        """Return *text* stripped, or ``None`` if it fails free-text hygiene.

        Rejects empty input, anything longer than *max_len*, any string
        carrying a non-printable or control character (``\\n``, ``\\r``,
        ``\\t`` included — ``str.isprintable`` treats them all as unprintable),
        and any string carrying a glyph that will not fit the narrow ledger:
        a fullwidth rune or a combining mark (the same one-column contract the
        map glyphs obey, via :func:`~understone.engine.textwidth.is_grid_safe`).
        Player names, bestow reasons, and inn mail all render inside fixed-width
        frames and tables, so a wide rune would shove a column out of true.
        This is the single chokepoint for player-authored free text reaching
        the durable store and the public log.
        """
        cleaned = text.strip()
        if not cleaned or len(cleaned) > max_len or not cleaned.isprintable():
            return None
        if not _is_narrow_text(cleaned):
            return None
        return cleaned

    # -- the satchel: a stack-based carried bag (v0.7 potions, v0.10 stacks) ---

    @staticmethod
    def _satchel_stacks(player: Player) -> list[tuple[str, int]]:
        """Return the carried satchel as ordered ``(item_id, qty)`` stacks.

        The game-side façade over :func:`~understone.engine.satchel.decode_satchel`:
        the codec owns the ``"id:qty"`` wire shape ('' stored = empty bag, order
        preserved, malformed/zero-qty fragments skipped); the rest of the façade
        calls this helper by its established name.
        """
        return decode_satchel(player.satchel)

    @staticmethod
    def _satchel_set_stacks(player: Player, stacks: list[tuple[str, int]]) -> None:
        """Store *stacks* back as the comma-joined ``id:qty`` satchel.

        Delegates to :func:`~understone.engine.satchel.encode_satchel`, which
        drops any qty <= 0 (the single home for the drop-at-empty rule), so
        callers may decrement freely and let the empty stack fall away.
        """
        player.satchel = encode_satchel(stacks)

    def _satchel_distinct(self, player: Player) -> int:
        """Return how many DISTINCT item stacks the satchel currently holds."""
        return len(self._satchel_stacks(player))

    def _satchel_find(self, player: Player, item_id: str) -> tuple[int, int] | None:
        """Return the ``(stack index, qty)`` of *item_id*, or ``None`` if absent."""
        for index, (existing_id, qty) in enumerate(self._satchel_stacks(player)):
            if existing_id == item_id:
                return index, qty
        return None

    def _strongest_potion(self, stacks: list[tuple[str, int]]) -> tuple[int, Item] | None:
        """Return the (stack index, item) of the highest-heal POTION, or None.

        Resolves each stack's id against the pack and picks the consumable with
        the greatest ``heal``; ties keep the earliest stack. Non-consumable
        stacks (ore and other materials) and unknown ids are ignored, so a bag
        of nothing but ore — or one edited out from under a save — yields
        ``None``. The index returned is the stack's position, for decrementing.
        """
        best: tuple[int, Item] | None = None
        for index, (item_id, _qty) in enumerate(stacks):
            item = self.world.item_by_id(item_id)
            if item is None or item.slot is not Slot.CONSUMABLE:
                continue
            if best is None or item.heal > best[1].heal:
                best = (index, item)
        return best

    def _satchel_blurb(self, player: Player) -> str:
        """Render the satchel as a status line: ``Name ×qty`` stacks or empty."""
        stacks = self._satchel_stacks(player)
        if not stacks:
            return "Satchel: empty"
        parts = []
        for item_id, qty in stacks:
            item = self.world.item_by_id(item_id)
            name = item.name if item is not None else item_id
            parts.append(f"{name} ×{qty}")
        cap = self.world.settings.satchel_max
        return f"Satchel ({len(stacks)}/{cap}): " + ", ".join(parts)

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

    def _spent_for_today(self, player: Player) -> str:
        """Persist *player* and render the shared overworld 'out of turns' frame.

        The turn-exhausted refusal for the overworld verbs (fight and ambush):
        the day-roll has already run and may have mutated state, so commit the
        player before reporting, then surface the spent line on the map.
        """
        self.store.upsert_player(player)
        self.store.commit()
        return self._overworld_frame(
            player,
            lines=["You're spent for today. Rest at the inn and return tomorrow."],
        )

    def _herald(self, kind: str, actor: str, **fields: object) -> EventSpec:
        """Build a PUBLIC feed event for *kind*, picking a phrasing via the RNG.

        Returns the ``(kind, actor, text, target)`` tuple ``_persist`` expects,
        with an empty target (everyone sees it). The phrasing is chosen from
        :data:`_HERALD_TEMPLATES` so the broadsheet varies; the choice is
        deterministic under a seeded RNG.
        """
        phrasings = _HERALD_TEMPLATES[kind]
        text = phrasings[self.rng.choice_index(len(phrasings))].format(name=actor, **fields)
        return (kind, actor, text, "")

    def _mail(self, kind: str, actor: str, target: str, **fields: object) -> EventSpec:
        """Build a PRIVATE note for *kind*, delivered only to *target*.

        Same phrasing machinery as :meth:`_herald`, but the returned tuple
        carries *target* so only that player reads it in their own catch-up;
        it never reaches the public broadsheet or the lobby TV. The recipient
        name is exposed to the template as ``{target}`` (templates that don't
        reference it simply ignore it), so callers needn't repeat it.
        """
        phrasings = _HERALD_TEMPLATES[kind]
        chosen = phrasings[self.rng.choice_index(len(phrasings))]
        text = chosen.format(name=actor, target=target, **fields)
        return (kind, actor, text, target)

    def _persist(self, player: Player, *events: EventSpec, also: Player | None = None) -> None:
        """Update + append in one transaction, then commit (state changes).

        Each event tuple is ``(kind, actor, text, target)``. New events are
        appended to the in-memory feed with the id the store assigns. When
        *also* is given (e.g. an ambush victim), that second player's row is
        upserted in the SAME transaction, so both fighters and their events
        commit atomically.
        """
        player.last_seen = self._now_iso()
        self.store.upsert_player(player)
        if also is not None:
            self.store.upsert_player(also)
        ts = self._now_iso()
        for kind, actor, text, target in events:
            event_id = self.store.insert_event(ts, actor, kind, text, target)
            self.events.append(
                Event(event_id=event_id, ts=ts, kind=kind, actor=actor, text=text, target=target)
            )
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
            # Location glyphs are landmarks; never texture them.
            loc_color = _COLOR_BY_NAME.get(loc.color, Color.TOWN)
            return Cell(loc.glyph, loc_color)
        # Terrain glyphs get a deterministic, position-keyed variant for texture.
        return Cell(textured(terrain.glyph, x, y), color)

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
            self._mark(grid, x0, y0, other.x, other.y, Cell("☻", Color.OTHER_PLAYER))
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
        """Create a new adventurer, or resume an existing one by name.

        Resume is identity-preserving and runs FIRST: an exact stripped-name
        match against a stored adventurer is welcomed back without re-running
        the name hygiene gate, so a character whose name predates a since-
        tightened rule (e.g. a wide rune now barred at creation) is never locked
        out of their own save. The sanitizer therefore governs CREATION only —
        a NEW name must still pass it.
        """
        existing = self.players.get(name.strip())
        if existing is not None:
            return self._resume(existing)

        clean = self._sanitize(name, _NAME_MAX_LEN)
        if clean is None:
            stripped = name.strip()
            if len(stripped) > _NAME_MAX_LEN:
                return "The ledger is narrow — choose a name of 24 letters or fewer."
            if stripped.isprintable() and not _is_narrow_text(stripped):
                return "The ledger's columns are narrow — wide runes will not fit."
            return "The gatekeeper squints at those strange runes. Plain letters, traveller."

        settings = self.world.settings
        atk, def_, max_hp = self._fresh_combat_stats()
        now = self._now_iso()
        today = self.clock().toordinal()
        player = Player(
            name=clean,
            x=self.world.spawn[0],
            y=self.world.spawn[1],
            hp=max_hp,
            max_hp=max_hp,
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
            wins=0,
            posts_sent=0,
            post_day=today,
            gambles=0,
            gamble_day=today,
        )
        self.players[clean] = player
        self._persist(player, self._herald("join", clean))
        banner = (
            f"You arrive in {self.world.name}, {clean}. A road runs east from the town.\n"
            "New here? Call door_help to learn how the world is run."
        )
        return self._overworld_frame(player, lines=self._with_watch(banner))

    def _resume(self, existing: Player) -> str:
        """Welcome a stored adventurer back, rolling their day and persisting.

        The resume path for ``join``: it never re-validates the name (an exact
        stored identity is admitted as-is, however old the rule it predates),
        rolls the daily clock, commits, and renders the overworld frame with the
        'welcome back' banner and the watch line.
        """
        self._ensure_day(existing)
        self.store.upsert_player(existing)
        self.store.commit()
        banner = f"Welcome back to {self.world.name}, {existing.name}."
        return self._overworld_frame(existing, lines=self._with_watch(banner))

    def _with_watch(self, banner: str) -> list[str]:
        """Return the join banner as frame lines, plus the watch line if set."""
        line = self.watch_line()
        return [banner, line] if line else [banner]

    def _fresh_combat_stats(self) -> tuple[int, int, int]:
        """Return the fresh ``(atk, def_, max_hp)`` for the starting kit.

        Shared by ``join`` and the Wyrm-slain legacy reset so a new hero and a
        reborn one start from exactly the same numbers. The un-equipped human
        baseline (HP/ATK/DEF) comes from the content pack's settings; the
        starting weapon/armour bonuses are added on top.
        """
        settings = self.world.settings
        weapon = self.world.item_by_id(settings.starting_weapon)
        armor = self.world.item_by_id(settings.starting_armor)
        atk = settings.start_atk + (weapon.atk if weapon else 0)
        def_ = settings.start_def + (armor.def_ if armor else 0)
        return atk, def_, settings.start_hp

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
        weapon_name = weapon.name if weapon else player.weapon_id
        armor_name = armor.name if armor else player.armor_id
        rungs = len(self.world.settings.dungeon_tiers)
        lines = [
            f"Adventurer: {player.name}",
            f"Level {player.level}   XP {player.xp}",
            f"HP {player.hp}/{player.max_hp}   ATK {player.atk}   DEF {player.def_}",
            f"Weapon: {_with_plus(weapon_name, player.weapon_plus)}",
            f"Armor:  {_with_plus(armor_name, player.armor_plus)}",
            f"Gold: {player.gold} on hand, {player.banked} in the vault",
            f"Turns {player.turns_left}/{self.world.settings.daily_turns}",
            f"Deep: rung {player.deepest_rung}/{rungs}",
            self._satchel_blurb(player),
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
        if result.event is not None:
            lines.append(self._narrate_event(result.event))
        if result.pending_fight is not None:
            lines.append("Something blocks your path, snarling. (door_action: fight  or  flee)")
        return lines

    @staticmethod
    def _narrate_event(event: movement.MoveEvent) -> str:
        """Render a private overworld event line (already applied to the player)."""
        if event.kind == "gold":
            return f"You find {event.text} — +{event.amount} gold."
        if event.kind == "heal":
            if event.amount <= 0:
                return f"You come upon {event.text}, but you are already whole."
            return f"You come upon {event.text} — +{event.amount} HP."
        if event.kind == "trap":
            if event.amount <= 0:
                return f"You stumble into {event.text}, but shrug it off."
            return f"You stumble into {event.text} — -{event.amount} HP."
        return f"You pass {event.text}"

    # -- tool: action ----------------------------------------------------

    def action(
        self,
        name: str,
        action: str,
        target: str,
        item: str,
        text: str = "",
        amount: int = 0,
    ) -> str:
        """Dispatch a context verb against the player's current surface.

        Most verbs are surface-bound (fight/flee on the overworld, rest/buy/
        gamble inside a building). ``post`` (leave a note for another player)
        is the exception: it works anywhere and costs no turn, so it is handled
        before the surface branch.
        """
        player = self._get(name)
        if player is None:
            return self._unknown(name)
        verb = action.strip().lower()

        if verb == "post":
            return self._post(player, target, text)
        if verb == "quaff":
            return self._quaff(player)
        if player.mode is Mode.TILE:
            return self._tile_action(player, verb, target)
        return self._menu_action(player, verb, target, item, amount)

    # -- tile-context actions (fight / flee / ambush) --------------------

    def _tile_action(self, player: Player, verb: str, target: str) -> str:
        if verb in {"fight", "flee"}:
            return self._resolve_encounter(player, verb)
        if verb == "ambush":
            return self._ambush(player, target)
        legal = "fight, flee, ambush a sleeping rival, or move on with door_move"
        return self._overworld_frame(
            player, lines=[f"There's nothing to '{verb}' out here. You can {legal}."]
        )

    def _pick_monster(self, player: Player) -> Monster | None:
        """Pick a random forest foe from the player's zone band, WEIGHTED.

        Each non-boss monster in the band is drawn in proportion to its
        ``weight`` (default 10), so a low-weight rare (weight 1) surfaces
        seldom among the common foes. Rung guardians do NOT come through here —
        ``_descend`` takes ``band[0]`` directly, so a rung is a fixed,
        repeatable guardian rather than a weighted roll.
        """
        zone = self.world.zone_for(player.x, player.y)
        if zone is None:
            return None
        band = self.world.monsters_for_tier_band(zone.tier_lo, zone.tier_hi)
        if not band:
            return None
        weights = [m.weight for m in band]
        return band[self.rng.weighted_index(weights)]

    def _resolve_encounter(self, player: Player, verb: str) -> str:
        self._ensure_day(player)
        monster = self._pick_monster(player)
        if monster is None:
            return self._overworld_frame(
                player, lines=["The air is still — nothing stirs to fight here."]
            )

        if not turns.spend_turn(player):
            return self._spent_for_today(player)

        child = self.rng.child()
        if verb == "flee":
            result = combat.resolve_flee(child, player, monster)
        else:
            result = combat.resolve_fight(child, player, monster)
        return self._apply_fight(player, result, monster)

    def _apply_xp_with_herald(
        self, player: Player, amount: int, lines: list[str]
    ) -> list[EventSpec]:
        """Award XP, append per-level narration to *lines*, and herald the climb.

        Returns the public-feed events to persist: a single ``level_up`` beat at
        the highest level reached (a multi-level jump is one notable beat, not a
        flood), or none when no level was gained.
        """
        gains = leveling.apply_xp(player, amount, self.world.settings)
        for gain in gains:
            lines.append(
                f"You reach level {gain.new_level}! "
                f"(+{gain.hp_gain} HP, +{gain.atk_gain} ATK, +{gain.def_gain} DEF)"
            )
        if not gains:
            return []
        return [self._herald("level_up", player.name, n=gains[-1].new_level)]

    @staticmethod
    def _append_kill_and_reward(lines: list[str], result: combat.FightResult) -> None:
        """Compose the ``"The <monster> falls. +X XP, +Y gold."`` line.

        Combat itself no longer narrates the kill's reward — the engine cannot
        know whether the caller will actually bank the xp/gold. This appends the
        full falls+reward line at the ownership layer, the instant the gold/xp
        are applied, so a reward is never narrated where none is granted (the
        Wyrm-win legacy reset builds its own celebration and calls this NOT at
        all). The text matches the old single-line form exactly.
        """
        lines.append(
            f"The {result.monster_name} falls. +{result.xp_delta} XP, +{result.gold_delta} gold."
        )

    _DEATH_SAVE_LINE = (
        "As the dark closes in, the elixir burns down your throat — "
        "you stagger back from death's edge."
    )

    def _death_save(self, player: Player, lines: list[str]) -> bool:
        """Spend the strongest carried potion to cheat a lethal loss, if able.

        The v0.7 death-save: called on the ACTIVE fighter the instant a fight
        is about to bounce them to the spawn. If they carry at least one
        usable potion, the strongest is drunk instead of the bounce — hp is set
        to that potion's heal (capped at max_hp), the potion leaves the
        satchel, a dramatic line is spliced into the narration, and the method
        returns ``True`` so the caller skips the spawn reset. With an empty (or
        all-unknown) satchel it does nothing and returns ``False``.

        This lives entirely in the façade: ``combat`` only reports the outcome,
        and the satchel + the survival are decided here.
        """
        stacks = self._satchel_stacks(player)
        best = self._strongest_potion(stacks)
        if best is None:
            return False
        index, potion = best
        self._satchel_spend(player, index)
        player.hp = min(player.max_hp, potion.heal)
        lines.append(self._DEATH_SAVE_LINE)
        return True

    def _satchel_try_add(self, player: Player, item_id: str, qty: int = 1) -> bool:
        """Add *qty* of *item_id* into the satchel if there is room; return success.

        Stack-aware (v0.10): if a stack of *item_id* already exists its qty is
        bumped (this ALWAYS fits — per-stack qty is unbounded). Otherwise a new
        stack is appended only while the DISTINCT-stack count is below
        ``satchel_max``; a full bag refuses without mutation and returns
        ``False``. The single chokepoint for adding to the satchel, so the
        distinct-stack cap lives in exactly one place. *qty* must be >= 1.
        """
        stacks = self._satchel_stacks(player)
        for index, (existing_id, existing_qty) in enumerate(stacks):
            if existing_id == item_id:
                stacks[index] = (existing_id, existing_qty + qty)
                self._satchel_set_stacks(player, stacks)
                return True
        if len(stacks) >= self.world.settings.satchel_max:
            return False
        stacks.append((item_id, qty))
        self._satchel_set_stacks(player, stacks)
        return True

    def _satchel_spend(self, player: Player, index: int, qty: int = 1) -> None:
        """Decrement the stack at *index* by *qty*, dropping it when it empties.

        The single home for taking from a stack: used by quaff, the death-save,
        and the forge (ore). The empty-stack drop is handled by
        :meth:`_satchel_set_stacks`, so a spent-to-zero stack falls away.
        """
        stacks = self._satchel_stacks(player)
        item_id, have = stacks[index]
        stacks[index] = (item_id, have - qty)
        self._satchel_set_stacks(player, stacks)

    def _apply_rare_kill(
        self, player: Player, monster: Monster, lines: list[str]
    ) -> list[EventSpec]:
        """Celebrate a rare kill: a public Herald flash and a guaranteed drop.

        Splices the drop line into *lines* (into the satchel if there is room,
        else a "no room" note) and returns the public ``rare_kill`` beat for
        the feed. The drop item is the pack's validated ``rare_drop_item`` (a
        known consumable), so it is always safe to add by id.
        """
        drop_id = self.world.settings.rare_drop_item
        if self._satchel_try_add(player, drop_id):
            lines.append("It guarded a draught — into your satchel it goes.")
        else:
            lines.append("It guarded a draught — but your satchel had no room.")
        return [self._herald("rare_kill", player.name, monster=monster.name)]

    def _grant_ore(self, player: Player, qty: int, lines: list[str]) -> None:
        """Drop *qty* forge ore into the satchel on a won fight, narrating it.

        The ore goes through the same stack-add chokepoint as every other
        satchel add, so it bumps an existing ore stack (always fits) or opens a
        new stack while a distinct slot is free. When the bag can hold no ore
        (no ore stack AND the distinct-stack cap is full), the ore is simply
        DROPPED with a "no room" note — never an error, and never a turn lost.
        A non-positive *qty* (a pack tuned to 0, or an unlucky 0 forest roll) is
        a no-op, so no empty "you find ore" line is spliced in.
        """
        if qty <= 0:
            return
        ore = self.world.item_by_id(self.world.settings.forge_ore_item)
        ore_name = ore.name if ore is not None else self.world.settings.forge_ore_item
        if self._satchel_try_add(player, self.world.settings.forge_ore_item, qty):
            lines.append(f"You pry {qty} {ore_name} from the wreck and pocket it.")
        else:
            lines.append(f"You spy {ore_name} in the wreck, but you've no room to pocket the ore.")

    def _apply_fight(
        self, player: Player, result: combat.FightResult, monster: Monster | None = None
    ) -> str:
        lines = list(result.log)
        events: list[EventSpec] = []

        player.hp = max(1, player.hp + result.hp_delta)
        if result.outcome is combat.Outcome.WIN:
            self._append_kill_and_reward(lines, result)
            player.gold += result.gold_delta
            events.extend(self._apply_xp_with_herald(player, result.xp_delta, lines))
            # A forest kill sometimes turns up forge ore (the deep is the surer
            # source; here it is an occasional bonus on a won bout).
            if self.rng.chance(self.world.settings.ore_forest_chance):
                self._grant_ore(player, 1, lines)
            if monster is not None and monster.rare:
                events.extend(self._apply_rare_kill(player, monster, lines))

        if result.bounce_to_spawn and not self._death_save(player, lines):
            player.hp = 1
            player.x, player.y = self.world.spawn
            events.append(self._herald("defeat", player.name, monster=result.monster_name))

        self._persist(player, *events)
        return self._overworld_frame(player, lines=lines)

    # -- tile-context action: ambush (asynchronous PvP) ------------------

    def _ambush(self, attacker: Player, target_name: str) -> str:
        """Fall upon a sleeping rival to rob them — the classic door-game player-kill beat.

        Legal on the overworld only. Eligibility is checked in a fixed order,
        each with its own in-fiction refusal: the target must exist, not be
        yourself, both of you must be seasoned (the gatekeeper shields the
        young), you must be within the level band, and — the SLEEP RULE — the
        target must not yet have begun their own day (anyone who has acted today
        is awake and un-ambushable). Then the day rolls and a turn is spent,
        exactly as a fight. The attempt is spent (recorded) on every outcome.
        """
        refusal = self._ambush_refusal(attacker, target_name)
        if refusal is not None:
            return self._overworld_frame(attacker, lines=[refusal])
        target = self.players[target_name.strip()]

        self._ensure_day(attacker)
        if not turns.spend_turn(attacker):
            return self._spent_for_today(attacker)

        return self._resolve_ambush(attacker, target)

    def _ambush_refusal(self, attacker: Player, target_name: str) -> str | None:
        """Return the in-fiction refusal for an illegal ambush, or ``None``.

        Each branch maps to a distinct rule, checked in order so the message
        names the first thing wrong. The order matters: existence before
        identity, level gates before the band, and the sleep/once-a-day rules
        last (they are the live-play defences).
        """
        target = self._get(target_name)
        if target is None:
            return self._unknown(target_name)
        if target.name == attacker.name:
            return "You can hardly ambush yourself, traveller."
        settings = self.world.settings
        floor = settings.ambush_min_level
        if attacker.level < floor or target.level < floor:
            return f"The gatekeeper shields the young: ambush is barred below level {floor}."
        if abs(attacker.level - target.level) > settings.ambush_level_band:
            return (
                f"{target.name} is too far from your measure to make a fair mark "
                f"(within {settings.ambush_level_band} levels only)."
            )
        if target.turn_day >= self._today_ordinal():
            return (
                f"{target.name} is already abroad and watchful today — you cannot "
                "catch them sleeping."
            )
        if target.hp <= 1:
            # Mercy rule: a freshly-robbed sleeper sits at 1 HP. Pile-on bandits
            # don't get to kick someone already in the ditch (this is checked
            # after the sleep rule, so an awake 1-HP rival reports as watchful).
            return (
                f"{target.name} already lies battered in the ditch — even bandits have standards."
            )
        if self.store.has_ambushed(attacker.name, target.name, self._today_ordinal()):
            return f"You have already lain in wait for {target.name} today."
        return None

    def _today_ordinal(self) -> int:
        """Return the attacker-current UTC ordinal (the sleep-rule boundary)."""
        return self.clock().toordinal()

    def _resolve_ambush(self, attacker: Player, target: Player) -> str:
        """Resolve a committed ambush and persist both fighters atomically."""
        settings = self.world.settings
        sleeper = Monster(
            tier=0,
            name=target.name,
            hp=target.hp,
            atk=target.atk,
            def_=target.def_,
            xp=0,
            gold=0,
        )
        result = combat.resolve_fight(self.rng.child(), attacker, sleeper)
        lines = list(result.log)
        events: list[EventSpec] = []
        day = self._today_ordinal()

        if result.outcome is combat.Outcome.WIN:
            # The sleeper still trades blows before falling; bank the attacker's
            # wear so the narrated counter-strikes match the sheet (mirrors
            # _apply_fight). A win never drops the attacker below 1 HP.
            attacker.hp = max(1, attacker.hp + result.hp_delta)
            steal = max(0, target.gold * settings.ambush_gold_pct // 100)
            attacker.gold += steal
            target.gold -= steal
            target.hp = 1
            target.x, target.y = self.world.spawn
            target.mode = Mode.TILE
            target.at_location = ""
            lines.append(f"You rob {target.name} of {steal} gold and melt away.")
            events.append(self._herald("ambush", attacker.name, target=target.name, steal=steal))
            events.append(self._mail("ambushed", attacker.name, target.name, steal=steal))
        elif result.bounce_to_spawn:
            # The ATTACKER is the active fighter, so their satchel can save them
            # from a lethal counter-strike (the sleeping VICTIM never quaffs —
            # they are asleep). A death-save keeps the attacker standing where
            # they are; otherwise they bounce to the spawn at 1 HP.
            lines.append(f"{target.name} wakes blade-in-hand and you flee bleeding.")
            if not self._death_save(attacker, lines):
                attacker.hp = 1
                attacker.x, attacker.y = self.world.spawn
            events.append(self._herald("ambush_shame", attacker.name, target=target.name))
        else:
            # A grinding stalemate: no gold moves, but the attacker keeps any
            # wear taken before breaking off (the fight/wyrm-flee convention).
            attacker.hp = max(1, attacker.hp + result.hp_delta)
            lines.append(f"Your nerve fails and you slip away from {target.name}.")
            events.append(self._herald("ambush_flee", attacker.name, target=target.name))

        # The attempt is spent on every outcome; record it inside the txn.
        self.store.record_ambush(attacker.name, target.name, day)
        self._persist(attacker, *events, also=target)
        return self._overworld_frame(attacker, lines=lines)

    # -- menu-context actions --------------------------------------------

    def _menu_action(self, player: Player, verb: str, target: str, item: str, amount: int) -> str:
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
        if verb == "deposit":
            return self._deposit(player, amount)
        if verb == "withdraw":
            return self._withdraw(player, amount)
        if verb == "heal":
            return self._heal(player)
        if verb == "buy":
            return self._buy(player, item)
        if verb == "sell":
            return self._sell(player)
        if verb == "forge":
            return self._forge(player, target)
        if verb == "descend":
            return self._descend(player)
        if verb == "challenge":
            return self._challenge(player)
        if verb == "gamble":
            return self._gamble(player, amount)
        return self._location_menu(player, lines=[f"The '{verb}' option isn't ready."])

    def _leave(self, player: Player) -> str:
        player.mode = Mode.TILE
        player.at_location = ""
        self._persist(player)
        return self._overworld_frame(player, lines=["You step back out into the open air."])

    def _rest(self, player: Player) -> str:
        cost = self.world.settings.rest_cost
        if leveling.rest(player, cost):
            # A night's rest is a private errand, not Herald news; persist only.
            self._persist(player)
            return self._location_menu(
                player, lines=[f"You sleep deeply and wake at full health. (-{cost} gold)"]
            )
        return self._location_menu(
            player, lines=[f"You can't afford the {cost}-gold bed. (You have {player.gold}.)"]
        )

    # -- the Vault: bank gold at the inn (safe from ambush) --------------

    def _deposit(self, player: Player, amount: int) -> str:
        """Move *amount* gold from the hand into the inn strongbox (no turn).

        Banked gold is SAFE from ambush and survives the Wyrm-win legacy reset.
        Refuses without mutation when there is nothing to bank or the amount
        exceeds the carried gold; both refusals are friendly and in-fiction.
        """
        if player.gold <= 0:
            return self._location_menu(player, lines=["You've no coin in hand to bank."])
        if amount < 1 or amount > player.gold:
            return self._location_menu(
                player,
                lines=[f"Name an amount from 1 to {player.gold} to set aside."],
            )
        player.gold -= amount
        player.banked += amount
        self._persist(player)
        return self._location_menu(
            player,
            lines=[
                f"The innkeep counts your coin into the strongbox. "
                f"({amount} banked; {player.banked} in the vault, {player.gold} in hand.)"
            ],
        )

    def _withdraw(self, player: Player, amount: int) -> str:
        """Move *amount* gold from the inn strongbox back into the hand (no turn).

        Refuses without mutation when the vault is empty or the amount exceeds
        what is banked; both refusals are friendly and in-fiction.
        """
        if player.banked <= 0:
            return self._location_menu(player, lines=["Your vault stands empty."])
        if amount < 1 or amount > player.banked:
            return self._location_menu(
                player,
                lines=[f"You may draw 1 to {player.banked} gold from the vault."],
            )
        player.banked -= amount
        player.gold += amount
        self._persist(player)
        return self._location_menu(
            player,
            lines=[
                f"The innkeep counts coin from the strongbox into your hand. "
                f"({amount} drawn; {player.gold} in hand, {player.banked} in the vault.)"
            ],
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
        # Mending at the shrine is a private errand; persist without Herald news.
        self._persist(player)
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
        # A consumable goes into the satchel to carry; a full bag refuses the
        # sale (no gold spent). Weapons/armour equip immediately as before.
        if item.slot is Slot.CONSUMABLE and not self._satchel_try_add(player, item.item_id):
            return self._location_menu(
                player,
                lines=["Your satchel bulges — no room for another draught."],
            )
        player.gold -= item.price
        line = self._equip_or_stow(player, item)
        # Shopping is a private errand; persist without Herald news.
        self._persist(player)
        return self._location_menu(player, lines=[line])

    def _equip_or_stow(self, player: Player, item: Item) -> str:
        """Equip a weapon/armour (clearing its slot first) or stow a draught.

        Buying gear clears the old slot through the shared unequip helper — which
        drops the old base bonus AND the forged plus and zeroes the plus, so a +N
        blade replaced leaves no phantom stat — then adds the new base bonus.
        Consumables were already placed in the satchel by the caller; this only
        narrates the stow.
        """
        if item.slot is Slot.WEAPON:
            self._clear_weapon_slot(player)
            player.atk += item.atk
            player.weapon_id = item.item_id
            return f"You take up the {item.name}. (-{item.price} gold)"
        if item.slot is Slot.ARMOR:
            self._clear_armor_slot(player)
            player.def_ += item.def_
            player.armor_id = item.item_id
            return f"You don the {item.name}. (-{item.price} gold)"
        return f"You stow the {item.name} in your satchel. (-{item.price} gold)"

    def _equipped_bonus(self, item_id: str, slot: Slot) -> int:
        item = self.world.item_by_id(item_id)
        if item is None or item.slot is not slot:
            return 0
        return item.atk if slot is Slot.WEAPON else item.def_

    def _clear_weapon_slot(self, player: Player) -> None:
        """Drop the equipped weapon's base bonus AND forged plus, then zero it.

        The single home for the weapon-slot unequip invariant: a swap, a sell,
        and the legacy reset all clear the slot through here, so a forged +N can
        never be left as phantom atk on the next blade. Subtracts the current
        weapon's base ATK bonus and ``weapon_plus`` from the live stat and zeroes
        the plus; the caller then equips whatever comes next.
        """
        player.atk -= self._equipped_bonus(player.weapon_id, Slot.WEAPON) + player.weapon_plus
        player.weapon_plus = 0

    def _clear_armor_slot(self, player: Player) -> None:
        """Drop the equipped armour's base bonus AND forged plus, then zero it.

        The armour twin of :meth:`_clear_weapon_slot` — the one home for the
        armour-slot unequip invariant, so a forged +N never lingers as phantom
        DEF. Subtracts the current armour's base DEF bonus and ``armor_plus``
        from the live stat and zeroes the plus.
        """
        player.def_ -= self._equipped_bonus(player.armor_id, Slot.ARMOR) + player.armor_plus
        player.armor_plus = 0

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
        # Clear the sold slot through the shared helper (drops the blade's base
        # bonus AND any forged plus, zeroes the plus), then fall back to the
        # starter and add its base bonus.
        self._clear_weapon_slot(player)
        player.atk += fallback.atk if fallback else 0
        player.weapon_id = starter
        # Selling back gear is a private errand; persist without Herald news.
        self._persist(player)
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
        lines.append("Buying weapon/armour equips it; potions go into your satchel.")
        lines.append("Forge a +1 edge with (F)orge weapon / armour; (Q)uaff a draught anywhere.")
        return lines

    @staticmethod
    def _stat_blurb(item: Item) -> str:
        if item.slot is Slot.WEAPON:
            return f"(+{item.atk} ATK)"
        if item.slot is Slot.ARMOR:
            return f"(+{item.def_} DEF)"
        return f"(+{item.heal} HP)"

    # -- the inn mailbox: leave word for another player ------------------

    def _surface(self, player: Player, *, lines: list[str]) -> str:
        """Render *lines* on the player's current surface (map or menu).

        Used by ``post``, which is legal in either mode, so its reply must
        match whichever surface the player is standing on.
        """
        if player.mode is Mode.MENU:
            return self._location_menu(player, lines=lines)
        return self._overworld_frame(player, lines=lines)

    def _post(self, player: Player, target_name: str, text: str) -> str:
        """Leave a private note for another player (legal anywhere; no turn).

        The note is delivered as a PRIVATE event the recipient alone reads in
        their next ``door_log`` ("While you were away"); the sender gets an
        in-fiction confirmation. The body runs through the same sanitizer as
        every other player-authored string, and a small daily cap keeps the
        hearth from becoming a billboard.
        """
        target = self._get(target_name)
        if target is None:
            return self._surface(player, lines=[self._unknown(target_name)])
        if target.name == player.name:
            return self._surface(player, lines=["You need no note to talk to yourself."])
        clean = self._sanitize(text, _REASON_MAX_LEN)
        if clean is None:
            return self._surface(
                player,
                lines=["The innkeep can't make out that scrawl. Plain words, briefly put."],
            )

        self._ensure_day(player)
        cap = self.world.settings.post_daily_cap
        if player.posts_sent >= cap:
            return self._surface(
                player,
                lines=[f"You've left all the word you may today ({cap} notes). Try tomorrow."],
            )
        player.posts_sent += 1
        note = self._mail("post", player.name, target.name, text=clean)
        self._persist(player, note)
        return self._surface(player, lines=["The innkeep tucks the note above the hearth."])

    # -- quaff: drink the strongest carried potion (anywhere; no turn) ---

    def _quaff(self, player: Player) -> str:
        """Drink the strongest carried potion (legal anywhere, costs no turn).

        Picks the highest-heal draught in the satchel, heals up to ``max_hp``,
        and removes it. An empty satchel refuses; quaffing at full HP refuses
        too, so a draught is never wasted. Renders on the player's current
        surface (map or menu), like ``post``.
        """
        stacks = self._satchel_stacks(player)
        best = self._strongest_potion(stacks)
        if best is None:
            return self._surface(player, lines=["Your satchel holds no draught to quaff."])
        if player.hp >= player.max_hp:
            return self._surface(
                player,
                lines=["You are already hale; the draught stays corked."],
            )
        index, potion = best
        before = player.hp
        player.hp = min(player.max_hp, player.hp + potion.heal)
        self._satchel_spend(player, index)
        self._persist(player)
        return self._surface(
            player,
            lines=[f"You quaff the {potion.name}, recovering {player.hp - before} HP."],
        )

    # -- forge: spend gold to enhance the equipped weapon or armour ------

    def _forge(self, player: Player, target: str) -> str:
        """Enhance the equipped weapon or armour by +1 — the late-game gold+ore sink.

        ``target`` is "weapon" or "armour"/"armor". A +1 step costs
        ``forge_base_cost * (current_plus + 1)`` GOLD *and*
        ``(current_plus + 1) * forge_ore_per_plus`` of the world's forge ore
        (carried in the satchel, won in the deep). The plus is capped at
        ``forge_max_plus``. On success BOTH the gold and the ore are deducted,
        the slot's plus rises, and the LIVE stat rises with it (weapon→atk,
        armour→def). A forge short of gold or ore refuses without mutation,
        naming both requirements; a capped slot refuses; a missing/invalid
        target asks which slot.
        """
        slot = target.strip().lower()
        if slot == "weapon":
            return self._forge_slot(
                player, current=player.weapon_plus, label="weapon", apply=self._forge_weapon
            )
        if slot in {"armor", "armour"}:
            return self._forge_slot(
                player, current=player.armor_plus, label="armour", apply=self._forge_armor
            )
        return self._location_menu(player, lines=["Forge what — (weapon) or (armour)?"])

    def _forge_slot(
        self,
        player: Player,
        *,
        current: int,
        label: str,
        apply: Callable[[Player], None],
    ) -> str:
        """Shared forge accounting for one slot: cap, gold+ore cost, deduct, apply."""
        settings = self.world.settings
        if current >= settings.forge_max_plus:
            return self._location_menu(player, lines=[f"Your {label} can take no finer edge."])
        cost = settings.forge_base_cost * (current + 1)
        ore_need = (current + 1) * settings.forge_ore_per_plus
        ore = self.world.item_by_id(settings.forge_ore_item)
        ore_name = ore.name if ore is not None else settings.forge_ore_item
        found = self._satchel_find(player, settings.forge_ore_item)
        ore_have = found[1] if found is not None else 0
        if player.gold < cost or ore_have < ore_need:
            return self._location_menu(
                player,
                lines=[
                    f"The smith wants {cost} gold and {ore_need} {ore_name} to better your "
                    f"{label} — you hold {player.gold} gold and {ore_have} {ore_name}."
                ],
            )
        player.gold -= cost
        if ore_need > 0 and found is not None:
            self._satchel_spend(player, found[0], ore_need)
        apply(player)
        self._persist(player)
        new_plus = player.weapon_plus if label == "weapon" else player.armor_plus
        ore_note = f", -{ore_need} {ore_name}" if ore_need > 0 else ""
        return self._location_menu(
            player,
            lines=[f"The smith works your {label} to +{new_plus}. (-{cost} gold{ore_note})"],
        )

    @staticmethod
    def _forge_weapon(player: Player) -> None:
        player.weapon_plus += 1
        player.atk += 1

    @staticmethod
    def _forge_armor(player: Player) -> None:
        player.armor_plus += 1
        player.def_ += 1

    # -- the inn dice game: wager against the house ---------------------

    def _gamble(self, player: Player, amount: int) -> str:
        """Wager *amount* gold on a single 2d6 roll against the house.

        Inn only (the menu gates the verb). Player and house each roll two
        dice; higher total wins the stake, a tie pushes (no gold moves) and a
        loss forfeits it. A daily count cap limits how many times the cup comes
        out; a push still counts. Costs no turn. A notable win is heralded.
        """
        max_bet = self.world.settings.gamble_max_bet
        if amount < 1 or amount > max_bet:
            return self._location_menu(
                player,
                lines=[f"The house takes wagers of 1 to {max_bet} gold. Name your stake."],
            )
        if player.gold < amount:
            return self._location_menu(
                player,
                lines=[f"You can't cover a {amount}-gold wager (you hold {player.gold})."],
            )

        self._ensure_day(player)
        cap = self.world.settings.gamble_daily_cap
        if player.gambles >= cap:
            return self._location_menu(
                player,
                lines=[f"The innkeep waves you off — {cap} games is enough for one day."],
            )
        player.gambles += 1

        child = self.rng.child()
        you = child.randint(1, 6) + child.randint(1, 6)
        house = child.randint(1, 6) + child.randint(1, 6)
        events: list[EventSpec] = []
        if you > house:
            player.gold += amount
            line = f"You roll {you}, the house {house}. You win {amount} gold!"
            if amount >= _GAMBLE_HERALD_MIN:
                events.append(self._herald("gamble", player.name, amount=amount))
        elif you < house:
            player.gold -= amount
            line = f"You roll {you}, the house {house}. You lose {amount} gold."
        else:
            line = f"You roll {you}, the house {house}. A push — your stake stands."
        self._persist(player, *events)
        return self._location_menu(player, lines=[line])

    def _descend(self, player: Player) -> str:
        """Descend ONE rung of the deep — the next guardian past your deepest.

        The deep is a ladder of ``settings.dungeon_tiers`` rungs, fought one per
        descent: a descent faces ``dungeon_tiers[deepest_rung]`` (the rung after
        your deepest), the fixed guardian ``band[0]`` of that tier. A win
        advances ``deepest_rung``; reaching the last rung opens the Wyrm's door.
        A loss bounces you to the spawn but PRESERVES ``deepest_rung`` — you
        re-enter where you left off. Standing already at the bottom, descend
        costs no turn and simply points you at the challenge.
        """
        tiers = self.world.settings.dungeon_tiers
        floor = len(tiers)
        if player.deepest_rung >= floor:
            return self._location_menu(
                player,
                lines=[
                    "You have plumbed the deep to its floor. There is nothing below "
                    "now but the Wyrm itself — challenge it when you are ready."
                ],
            )

        self._ensure_day(player)
        if not turns.spend_turn(player):
            return self._location_menu(
                player,
                lines=["You're too weary to descend today. Return tomorrow."],
            )

        # band[0] on purpose: the rung guardian is a fixed, repeatable foe (the
        # classic fixed-foe convention), never a weighted roll.
        next_rung = player.deepest_rung  # 0-indexed into the tier ladder
        tier = tiers[next_rung]
        band = self.world.monsters_for_tier_band(tier, tier)
        # The loader guarantees every dungeon tier is backed by a non-boss
        # monster, so band is non-empty; guard defensively all the same.
        if not band:
            return self._location_menu(
                player, lines=["The way down is choked with rubble; no foe stirs here."]
            )
        monster = band[0]

        lines = [f"You descend to the {_ordinal(next_rung + 1)} rung of the Understone Deep..."]
        events: list[EventSpec] = []
        result = combat.resolve_fight(self.rng.child(), player, monster)
        lines.append("")
        lines.extend(result.log)
        player.hp = max(1, player.hp + result.hp_delta)

        if result.bounce_to_spawn:
            # A carried draught saves the active fighter in ANY fight, the deep
            # included: the strongest potion is drunk instead of the bounce. A
            # save keeps the hero standing at the dungeon — depth UNCHANGED (the
            # rung was not cleared, but the depth already earned is kept), the
            # turn still spent. Without a potion it is the standard spawn bounce.
            # A descent loss is private, so the public defeat herald is written
            # only on a genuine (un-saved) bounce.
            if not self._death_save(player, lines):
                player.hp = 1
                player.x, player.y = self.world.spawn
                player.mode = Mode.TILE
                player.at_location = ""
                events.append(self._herald("defeat", player.name, monster=monster.name))
                self._persist(player, *events)
                return self._overworld_frame(player, lines=lines)
            self._persist(player, *events)
            return self._location_menu(player, lines=lines)

        if result.outcome is combat.Outcome.WIN:
            self._append_kill_and_reward(lines, result)
            player.gold += result.gold_delta
            events.extend(self._apply_xp_with_herald(player, result.xp_delta, lines))
            # The deep is the reliable ore source: a cleared rung always yields
            # forge ore (the forge gate is built to be fed by descending).
            self._grant_ore(player, self.world.settings.ore_dungeon_drop, lines)
            player.deepest_rung = next_rung + 1
            lines.append("")
            if player.deepest_rung >= floor:
                lines.append(
                    "You stand at the Wyrm's door. The deep has no deeper — only the "
                    "Wyrm Below remains. Challenge it when you are ready."
                )
            else:
                lines.append(
                    f"You have reached rung {player.deepest_rung} of {floor}, and climb "
                    "back to the surface to gather your strength."
                )
        else:
            # A stalemate flight: no rung gained, but the wear taken is kept.
            lines.append("")
            lines.append("You break off and climb back to the surface, winded.")

        self._persist(player, *events)
        return self._location_menu(player, lines=lines)

    # -- the Wyrm Below: the endgame challenge ---------------------------

    def _challenge(self, player: Player) -> str:
        """Face the Wyrm Below — the win condition, with a classic-door-game-style reset.

        Gated behind ``wyrm_min_level``; spends a daily turn like a fight; then
        resolves a single fight against the boss. A win immortalises the run in
        the Hall of Legends and reincarnates the hero (legend kept as a ★); a
        loss bounces them to the spawn; a stalemate counts as a flight.
        """
        min_level = self.world.settings.wyrm_min_level
        if player.level < min_level:
            return self._location_menu(
                player,
                lines=[
                    f"The Wyrm Below stirs only for those of the {_ordinal(min_level)} "
                    "circle or beyond. Grow stronger, then return."
                ],
            )
        # The depth gate, checked AFTER the level gate so a low-level shallow
        # hero hears the level message first: the Wyrm will not stir until the
        # deep has been plumbed rung by rung to its floor.
        if player.deepest_rung < len(self.world.settings.dungeon_tiers):
            return self._location_menu(
                player,
                lines=[
                    "The Wyrm Below will not stir for one who has not yet plumbed "
                    "the deep to its floor."
                ],
            )

        boss = self.world.monster_by_id(self.world.settings.boss_monster)
        if boss is None:
            # Defensive: the loader guarantees the boss resolves, so this is an
            # in-fiction fallback rather than a crossing exception. Checked
            # before the day-roll so an unfightable challenge never mutates state.
            return self._location_menu(
                player, lines=["The deep is silent; the Wyrm does not answer today."]
            )

        self._ensure_day(player)
        if not turns.spend_turn(player):
            self.store.upsert_player(player)
            self.store.commit()
            return self._location_menu(
                player,
                lines=["You are too spent to challenge the Wyrm today. Return tomorrow."],
            )

        result = combat.resolve_fight(self.rng.child(), player, boss)
        if result.outcome is combat.Outcome.WIN:
            return self._wyrm_won(player, result)
        if result.bounce_to_spawn:
            return self._wyrm_lost(player, result)
        return self._wyrm_fled(player, result)

    def _wyrm_won(self, player: Player, result: combat.FightResult) -> str:
        """Record the kill, herald it, and reincarnate the hero with a ★."""
        lines = list(result.log)
        level_at_win = player.level
        run_days = self._run_days(player)
        self.store.insert_hall_row(player.name, self._now_iso(), run_days, level_at_win)
        event = self._herald("wyrm_win", player.name)
        self._reset_with_legacy(player)
        self._persist(player, event)

        days_word = "day" if run_days == 1 else "days"
        lines.append("")
        lines.append(f"THE WYRM BELOW IS SLAIN. {player.name}, you have freed the Vale.")
        lines.append(
            f"Your legend is carved into the Hall of Legends: "
            f"level {level_at_win}, a {run_days}-{days_word} road."
        )
        lines.append(
            "The Vale renews itself around you. You begin again at the town, your gear "
            "and gold as on your first day — but the star of this victory is yours forever "
            f"(★ x{player.wins})."
        )
        return self._overworld_frame(player, lines=lines)

    def _wyrm_lost(self, player: Player, result: combat.FightResult) -> str:
        """A lethal Wyrm bout: a carried draught saves the hero, else they fall.

        The universal death-save reaches the Wyrm too: a potion in the satchel
        is drunk instead of the devouring. A save is NOT a win — there is no
        legacy reset (level/gold/depth all stand) — but it is NOT the devouring
        either, so the hero keeps their place at the dungeon and the PUBLIC beat
        is the survival one (the ``wyrm_flee`` "driven back, alive but unproven"
        herald), never the "devoured" lie. The turn is spent regardless. With no
        potion it is the standard defeat: bounce to the spawn at 1 HP, devoured.
        """
        lines = list(result.log)
        if self._death_save(player, lines):
            self._persist(player, self._herald("wyrm_flee", player.name))
            return self._location_menu(player, lines=lines)
        player.hp = 1
        player.x, player.y = self.world.spawn
        player.mode = Mode.TILE
        player.at_location = ""
        self._persist(player, self._herald("wyrm_lose", player.name))
        return self._overworld_frame(player, lines=lines)

    def _wyrm_fled(self, player: Player, result: combat.FightResult) -> str:
        """Stalemate flight: keep any HP lost, herald the retreat, stay put."""
        lines = list(result.log)
        player.hp = max(1, player.hp + result.hp_delta)
        self._persist(player, self._herald("wyrm_flee", player.name))
        return self._location_menu(player, lines=lines)

    def _run_days(self, player: Player) -> int:
        """Whole days between the hero's creation and now (clock-derived, >= 0)."""
        try:
            created = datetime.fromisoformat(player.created_at)
        except ValueError:
            return 0
        return max(0, (self.clock() - created).days)

    def _reset_with_legacy(self, player: Player) -> None:
        """Reincarnate *player* to fresh-start values, banking the win as legend.

        Stats, equipment, HP, carried gold, level/xp and position all return to
        the first-day baseline and ``created_at`` is restamped; ``wins`` rises
        by one. The v0.7 retention state — dungeon depth, forged enhancements,
        and the satchel — resets too (a reborn hero re-earns the deep and
        carries nothing forged or bottled). The daily clock (turns/turn_day),
        the bestow pool, and the log cursor are deliberately UNTOUCHED — a
        legacy run is a fresh character, not a fresh day. The VAULT (``banked``)
        SURVIVES, too: it is gold set aside in the strongbox, not on the reborn
        hero, so it persists across runs as a small standing reward — the only
        wealth, besides the ★, that a legacy reset does not clear.
        """
        settings = self.world.settings
        # Clear both gear slots through the shared unequip helpers FIRST, so the
        # forged-plus reset lives in the one place every slot change uses (the
        # fresh stats below are absolute, but the plus-zeroing invariant stays
        # centralised rather than hand-copied here).
        self._clear_weapon_slot(player)
        self._clear_armor_slot(player)
        atk, def_, max_hp = self._fresh_combat_stats()
        player.wins += 1
        player.level = 1
        player.xp = 0
        player.gold = settings.starting_gold
        player.atk = atk
        player.def_ = def_
        player.max_hp = max_hp
        player.hp = max_hp
        player.weapon_id = settings.starting_weapon
        player.armor_id = settings.starting_armor
        player.x, player.y = self.world.spawn
        player.mode = Mode.TILE
        player.at_location = ""
        player.created_at = self._now_iso()
        # The deep and the satchel are first-day fresh too: a reborn hero must
        # plumb the deep again and carries no draught across the renewal (the
        # forged edges were already cleared with the gear slots above).
        player.deepest_rung = 0
        player.satchel = ""

    # -- tool: log -------------------------------------------------------

    def _backfill_durable_mail(self, player: Player, visible: list[Event]) -> list[Event]:
        """Merge any private notes that predate the resident tail into *visible*.

        Public history older than the in-memory tail is gone by design (the
        broadsheet does not keep), but mail is durable: a note left while the
        recipient was away must still surface however many public events have
        since evicted it from the resident tail. When the player's cursor lies
        before the oldest resident event, we pull their targeted rows from
        SQLite for that gap and splice them in by id, deduped against the rows
        already shown. The cursor still advances to the highest resident id.
        """
        oldest_resident = self.events[0].event_id if self.events else 0
        # The resident tail already covers everything from oldest_resident on;
        # a backfill is only needed when the cursor predates that boundary.
        if player.log_cursor >= oldest_resident - 1:
            return visible
        durable = self.store.targeted_events_since(player.name, player.log_cursor)
        seen = {event.event_id for event in visible}
        merged = visible + [event for event in durable if event.event_id not in seen]
        merged.sort(key=lambda event: event.event_id)
        return merged

    def log(self, name: str) -> str:
        """Report events since the player's cursor, then advance it.

        Dressed as the Understone Herald broadsheet: a masthead, the public
        dispatches, any PRIVATE notes left for this player ("While you were
        away"), then the status footer. Public and private rows ride one id
        order, so the cursor advances identically whether or not a private note
        appeared — a note meant for someone else is consumed, never re-shown,
        and never leaks here.
        """
        player = self._get(name)
        if player is None:
            return self._unknown(name)
        visible, new_cursor = since_visible(self.events, player.log_cursor, player.name)
        visible = self._backfill_durable_mail(player, visible)
        # Advance past every fresh row (visible or not) so private notes for
        # others are consumed once; persist the new cursor.
        if new_cursor != player.log_cursor:
            player.log_cursor = new_cursor
            self._persist(player)
        if not visible:
            return f"{_HERALD_HEADER}\n{_HERALD_QUIET}\n" + self._footer(player)
        public = [e for e in visible if not e.target]
        private = [e for e in visible if e.target == player.name]
        lines = [_HERALD_HEADER]
        if public:
            lines.append("Word from across the Vale since your last visit:")
            lines.extend(f"  - {event.text}" for event in public)
        if private:
            lines.append("While you were away, word was left for you:")
            lines.extend(f"  - {event.text}" for event in private)
        return "\n".join(lines) + "\n" + self._footer(player)

    # -- tool: rank ------------------------------------------------------

    def rank(self, name: str) -> str:
        """Render the top-10 leaderboard, marking the caller's row.

        Names carry one ★ per Wyrm slain. Main ordering is unchanged (level
        desc, xp desc, name asc). Below the table, the Hall of Legends lists
        the most recent completed runs; it is omitted entirely when empty.
        """
        entries = [
            RankEntry(name=p.name, level=p.level, xp=p.xp, gold=p.gold, wins=p.wins)
            for p in self.players.values()
        ]
        top = leaderboard(entries, limit=10)
        caller = name.strip()
        lines = _render_rank_table(top, caller)
        lines.extend(_render_hall(self.store.top_hall(5)))
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
        self._persist(
            player, self._herald("bestow", player.name, granted=granted, reason=clean_reason)
        )
        confirm = (
            f"A bestowal falls upon {player.name}: {granted}.\n"
            f"Reason: {clean_reason}\n"
            f"Fortune pool: {player.bestow_spent}/{budget} spent today."
        )
        return confirm + "\n" + self._footer(player)


def _render_rank_table(entries: list[RankEntry], caller: str) -> list[str]:
    """Render a box-drawing leaderboard table, marking the caller's row.

    The name and the win-stars live in separate columns so a long name can
    never swallow its own ★s: a 24-wide name field, then a 6-wide stars field
    (``★`` per win up to five, then a compact ``★xN`` for more).
    """
    header = f"  #  {'Adventurer':<24} {'★':<6} Lv     XP   Gold"
    width = len(header) + 2
    top = "┌" + "─" * width + "┐"
    bottom = "└" + "─" * width + "┘"
    lines = [top, "│ " + "The Roll of Heroes".center(width - 1) + "│", "│" + "─" * width + "│"]
    lines.append("│ " + header.ljust(width - 1) + "│")
    if not entries:
        lines.append("│ " + "(no heroes have ventured yet)".ljust(width - 1) + "│")
    for i, entry in enumerate(entries, start=1):
        mark = "*" if entry.name == caller else " "
        stars = _win_stars(entry.wins)
        row = (
            f"{mark}{i:>2}  {entry.name:<24.24} {stars:<6} "
            f"{entry.level:>2} {entry.xp:>6} {entry.gold:>6}"
        )
        lines.append("│ " + row.ljust(width - 1) + "│")
    lines.append(bottom)
    return lines


def _win_stars(wins: int) -> str:
    """Render the win column: blank at zero, ``★`` per win to five, then ``★xN``."""
    if wins <= 0:
        return ""
    if wins <= 5:
        return "★" * wins
    return f"★x{wins}"


def _render_hall(entries: list[HallEntry]) -> list[str]:
    """Render the Hall of Legends block under the leaderboard, or nothing.

    Each row is one immortalised run: the hero's name, the level they slew the
    Wyrm at, the length of that run in days, and the date. Returns an empty
    list when no one has yet won, so the section vanishes entirely.
    """
    if not entries:
        return []
    out = ["", "Hall of Legends — those who slew the Wyrm Below:"]
    for entry in entries:
        days_word = "day" if entry.run_days == 1 else "days"
        date = entry.win_ts[:10]
        out.append(
            f"  ★ {entry.name:<20.20} Lv {entry.level_at_win:>2}  "
            f"{entry.run_days:>3} {days_word:<4}  {date}"
        )
    return out


_ORDINALS = (
    "zeroth",
    "first",
    "second",
    "third",
    "fourth",
    "fifth",
    "sixth",
    "seventh",
    "eighth",
    "ninth",
    "tenth",
)


def _ordinal(n: int) -> str:
    """Return a lowercase fictional ordinal for *n* (falls back to ``Nth``)."""
    if 0 <= n < len(_ORDINALS):
        return _ORDINALS[n]
    return f"{n}th"


def _with_plus(name: str, plus: int) -> str:
    """Append a ``+N`` enhancement suffix to an item name (blank at zero)."""
    return f"{name} +{plus}" if plus > 0 else name
