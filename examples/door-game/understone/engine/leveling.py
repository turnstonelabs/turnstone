"""Experience, level-ups, and the inn/healer restorative maths.

The XP curve and stat growth come from the content pack's settings, so no
progression constants live in this module. Level-ups loop (a single XP
award can cross several thresholds), grant flat stat growth, and fully
heal on each level gained.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from understone.engine.models import Player, Settings


@dataclass(slots=True)
class LevelUp:
    """A record of a single level gained, for narration."""

    new_level: int
    hp_gain: int
    atk_gain: int
    def_gain: int


def xp_for_level(level: int, settings: Settings) -> int:
    """Return cumulative XP required to *reach* ``level``.

    Level 1 needs 0. The default curve is ``base * n*(n+1)/2`` over the
    completed levels, i.e. a triangular ramp scaled by ``xp_base``.
    """
    if level <= 1:
        return 0
    completed = level - 1
    return settings.xp_base * completed * (completed + 1) // 2


def apply_xp(player: Player, amount: int, settings: Settings) -> list[LevelUp]:
    """Award ``amount`` XP to *player*, applying every level-up it unlocks.

    Returns one :class:`LevelUp` per level gained (empty when none). Each
    level grants flat growth from settings and fully heals the player.
    """
    player.xp += max(0, amount)
    gains: list[LevelUp] = []
    while player.xp >= xp_for_level(player.level + 1, settings):
        player.level += 1
        player.max_hp += settings.growth_max_hp
        player.atk += settings.growth_atk
        player.def_ += settings.growth_def
        player.hp = player.max_hp
        gains.append(
            LevelUp(
                new_level=player.level,
                hp_gain=settings.growth_max_hp,
                atk_gain=settings.growth_atk,
                def_gain=settings.growth_def,
            )
        )
    return gains


def rest(player: Player, cost: int) -> bool:
    """Fully heal *player* at the inn for a flat ``cost``.

    Returns ``False`` without mutation when the player cannot afford it.
    Resting when already at full HP still succeeds (and still charges),
    matching the inn's flat-rate fiction.
    """
    if player.gold < cost:
        return False
    player.gold -= cost
    player.hp = player.max_hp
    return True


@dataclass(slots=True)
class HealResult:
    """Outcome of a healer purchase: HP actually restored and gold spent."""

    healed: int
    cost: int


def heal(player: Player, amount: int, cost_per_hp: int) -> HealResult:
    """Restore up to ``amount`` HP at ``cost_per_hp`` gold each.

    Heals only the missing portion, charges only for HP actually restored,
    and is further bounded by what the player can afford. Returns the amount
    healed and the gold spent (both zero when nothing could be done).
    """
    missing = player.max_hp - player.hp
    want = max(0, min(amount, missing))
    if want <= 0 or cost_per_hp < 0:
        return HealResult(healed=0, cost=0)
    if cost_per_hp == 0:
        player.hp += want
        return HealResult(healed=want, cost=0)
    affordable = player.gold // cost_per_hp
    apply = min(want, affordable)
    if apply <= 0:
        return HealResult(healed=0, cost=0)
    spent = apply * cost_per_hp
    player.hp += apply
    player.gold -= spent
    return HealResult(healed=apply, cost=spent)
