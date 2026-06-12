"""Combat resolution — pure math over an injected RNG.

A fight runs deterministic rounds: both sides trade blows until one drops.
Damage is ``max(1, attacker_atk - defender_def)`` jittered by a small RNG
swing so identical stats still produce varied logs. The result is a value
object; turn accounting and the spawn-bounce on defeat are applied by the
caller (the game façade), keeping this module side-effect free.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from understone.engine.models import Monster, Player
    from understone.engine.rng import GameRNG

_MAX_ROUNDS = 50


class Outcome(StrEnum):
    """How a fight ended."""

    WIN = "win"
    LOSE = "lose"
    FLED = "fled"


@dataclass(slots=True)
class FightResult:
    """The full outcome of a combat exchange.

    Deltas are signed and meant to be applied to the player by the caller.
    ``bounce_to_spawn`` signals a defeat: the caller sets ``hp`` to 1 and
    moves the player back to the spawn point.
    """

    outcome: Outcome
    log: list[str] = field(default_factory=list)
    xp_delta: int = 0
    gold_delta: int = 0
    hp_delta: int = 0
    bounce_to_spawn: bool = False
    monster_name: str = ""


def _swing(rng: GameRNG, atk: int, def_: int) -> int:
    """Return one blow's damage: floor of 1, with a small RNG jitter."""
    base = atk - def_
    jitter = rng.randint(-1, 2)
    return max(1, base + jitter)


def resolve_fight(rng: GameRNG, player: Player, monster: Monster) -> FightResult:
    """Run a full fight between *player* and *monster*.

    The player strikes first each round. On victory the player banks the
    monster's xp/gold and keeps any hp lost during the exchange. On defeat
    the result flags a spawn bounce for the caller to apply.
    """
    result = FightResult(outcome=Outcome.WIN, monster_name=monster.name)
    player_hp = player.hp
    monster_hp = monster.hp
    result.log.append(f"You close with the {monster.name}.")

    for _ in range(_MAX_ROUNDS):
        dealt = _swing(rng, player.atk, monster.def_)
        monster_hp -= dealt
        result.log.append(f"You strike for {dealt}. ({monster.name}: {max(monster_hp, 0)} HP)")
        if monster_hp <= 0:
            result.outcome = Outcome.WIN
            result.xp_delta = monster.xp
            result.gold_delta = monster.gold
            result.hp_delta = player_hp - player.hp
            result.log.append(f"The {monster.name} falls. +{monster.xp} XP, +{monster.gold} gold.")
            return result

        taken = _swing(rng, monster.atk, player.def_)
        player_hp -= taken
        result.log.append(f"It hits back for {taken}. (You: {max(player_hp, 0)} HP)")
        if player_hp <= 0:
            result.outcome = Outcome.LOSE
            result.bounce_to_spawn = True
            result.log.append(
                f"The {monster.name} lays you low. You wake at the spawn, barely alive."
            )
            return result

    # Stalemate guard: treat an unresolved marathon as a flight to safety.
    result.outcome = Outcome.FLED
    result.hp_delta = player_hp - player.hp
    result.log.append("The fight grinds on until you break away, winded.")
    return result


def resolve_flee(rng: GameRNG, player: Player, monster: Monster) -> FightResult:
    """Attempt to flee a fight.

    A successful flee escapes clean. A failed flee costs one free blow from
    the monster but never drops the player below 1 HP (fleeing is a way out,
    not a death trap).
    """
    result = FightResult(outcome=Outcome.FLED, monster_name=monster.name)
    if rng.chance(0.6):
        result.log.append(f"You slip away from the {monster.name}.")
        return result

    taken = _swing(rng, monster.atk, player.def_)
    taken = min(taken, max(player.hp - 1, 0))
    result.hp_delta = -taken
    result.log.append(f"You turn to run; the {monster.name} catches you for {taken} as you go.")
    return result
