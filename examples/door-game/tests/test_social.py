"""The v0.5 social slice — ambush (async PvP), inn mail, and inn dice.

Drives the game façade over the shipped world with a frozen clock and a seeded
RNG. Three feature areas:

* AMBUSH — the full eligibility matrix (every refusal branch), the win path
  (exact gold transfer, victim bounced to spawn at 1 HP, private mail visible
  only to the victim, public news), the lose path (attacker bounced, no
  transfer), the flee stalemate, per-day once-per-pair, and next-day retry.
* MAIL — ``post`` delivers a private note to the target's log once, the sender
  is confirmed, the daily cap refuses the overflow, the sanitizer rejects a
  newline body, and the Watch state payload NEVER carries a targeted row.
* DICE — win/lose/push under a seeded RNG, the bet band, affordability, the
  daily cap (a push still counts), and the Herald firing only on a big win.

Negative-test discipline (the SLEEP RULE has teeth):
  ``test_sleep_rule_guard_has_teeth`` documents the revert-and-observe check.
  Disabling the ``target.turn_day >= today`` clause in Game._ambush_refusal
  let an ALREADY-AWAKE target be ambushed — ``test_ambush_refused_target_awake``
  then failed (the attempt resolved instead of being refused). The clause was
  restored; that refusal test is the standing regression for the invariant.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import fixed_clock, utc
from understone.engine.models import Mode
from understone.engine.rng import GameRNG
from understone.game import Game
from understone.persistence import Store
from understone.watch import build_state_payload
from understone.world.loader import load_world

PACK = Path(__file__).resolve().parents[1] / "understone" / "world" / "data"

# The frozen "today" all these tests run on; the sleep rule keys off its ordinal.
_NOW = utc(2026, 6, 12, 10, 0)
_TODAY = _NOW.toordinal()


@pytest.fixture
def clock() -> object:
    return fixed_clock(_NOW)


def _game(tmp_path: Path, clock: object, seed: int = 7) -> Game:
    world = load_world(PACK)
    store = Store(tmp_path / "social.db")
    return Game(world, store, clock=clock, rng=GameRNG(seed=seed))  # type: ignore[arg-type]


def _arm_ambush(
    game: Game,
    *,
    attacker_level: int = 5,
    target_level: int = 5,
    target_asleep: bool = True,
    target_gold: int = 100,
) -> tuple[object, object]:
    """Join an attacker + target and tune their sheets for an ambush.

    The attacker is overworld and seasoned; the target sits at *target_level*
    with *target_gold*, and ``target_asleep`` controls the sleep rule (a
    sleeping target has not acted today). Returns ``(attacker, target)``.
    """
    game.join("Raider")
    game.join("Sleeper")
    attacker = game.players["Raider"]
    target = game.players["Sleeper"]
    attacker.level = attacker_level
    target.level = target_level
    target.gold = target_gold
    target.turn_day = _TODAY - 1 if target_asleep else _TODAY
    return attacker, target


# ---------------------------------------------------------------------------
# Ambush — eligibility matrix (each refusal is a distinct in-fiction line)
# ---------------------------------------------------------------------------


def test_ambush_refused_unknown_target(tmp_path: Path, clock: object) -> None:
    game = _game(tmp_path, clock)
    game.join("Raider")
    game.players["Raider"].level = 5
    out = game.action("Raider", "ambush", "Ghost", "")
    assert "signed the ledger" in out  # the unknown-player refusal
    # No turn spent on an unresolvable target.
    assert game.players["Raider"].turns_left == game.world.settings.daily_turns


def test_ambush_refused_self(tmp_path: Path, clock: object) -> None:
    game = _game(tmp_path, clock)
    game.join("Raider")
    game.players["Raider"].level = 5
    out = game.action("Raider", "ambush", "Raider", "")
    assert "yourself" in out.lower()


def test_ambush_refused_young_attacker(tmp_path: Path, clock: object) -> None:
    game = _game(tmp_path, clock)
    floor = game.world.settings.ambush_min_level
    _arm_ambush(game, attacker_level=floor - 1, target_level=floor + 1)
    out = game.action("Raider", "ambush", "Sleeper", "")
    assert "shields the young" in out
    assert game.players["Raider"].turns_left == game.world.settings.daily_turns


def test_ambush_refused_young_target(tmp_path: Path, clock: object) -> None:
    game = _game(tmp_path, clock)
    floor = game.world.settings.ambush_min_level
    # Attacker is seasoned but the target is below the floor: still shielded.
    _arm_ambush(game, attacker_level=floor + 1, target_level=floor - 1)
    out = game.action("Raider", "ambush", "Sleeper", "")
    assert "shields the young" in out


def test_ambush_refused_out_of_band(tmp_path: Path, clock: object) -> None:
    game = _game(tmp_path, clock)
    band = game.world.settings.ambush_level_band
    floor = game.world.settings.ambush_min_level
    _arm_ambush(
        game,
        attacker_level=floor + band + 5,
        target_level=floor,
    )
    out = game.action("Raider", "ambush", "Sleeper", "")
    assert "far from your measure" in out


def test_ambush_band_beats_awake_in_refusal_order(tmp_path: Path, clock: object) -> None:
    """PRECEDENCE: the band gate is checked before the sleep rule.

    A target who is BOTH out of band AND awake must report the band message,
    not the watchful one — pinning the documented order (level gates before the
    live-play sleep defence).
    """
    game = _game(tmp_path, clock)
    band = game.world.settings.ambush_level_band
    floor = game.world.settings.ambush_min_level
    _arm_ambush(
        game,
        attacker_level=floor + band + 1,  # one past the band...
        target_level=floor,
        target_asleep=False,  # ...and also awake
    )
    out = game.action("Raider", "ambush", "Sleeper", "")
    assert "far from your measure" in out  # the band gate wins
    assert "watchful today" not in out


def test_ambush_band_boundary_exact_is_allowed(tmp_path: Path, clock: object) -> None:
    """Exactly ``ambush_level_band`` apart clears the band gate (it is inclusive).

    Armed awake so the very next gate — the sleep rule — is what speaks: a
    'watchful today' refusal proves the band gate let this pair through.
    """
    game = _game(tmp_path, clock)
    band = game.world.settings.ambush_level_band
    floor = game.world.settings.ambush_min_level
    _arm_ambush(
        game,
        attacker_level=floor + band,  # exactly band levels above the floor
        target_level=floor,
        target_asleep=False,
    )
    out = game.action("Raider", "ambush", "Sleeper", "")
    assert "far from your measure" not in out  # past the band gate
    assert "watchful today" in out  # stopped by the next gate instead


def test_ambush_band_boundary_one_over_is_refused(tmp_path: Path, clock: object) -> None:
    """One level past ``ambush_level_band`` is refused with the band message."""
    game = _game(tmp_path, clock)
    band = game.world.settings.ambush_level_band
    floor = game.world.settings.ambush_min_level
    _arm_ambush(
        game,
        attacker_level=floor + band + 1,  # just over the band
        target_level=floor,
    )
    out = game.action("Raider", "ambush", "Sleeper", "")
    assert "far from your measure" in out
    assert game.store.has_ambushed("Raider", "Sleeper", _TODAY) is False


def test_ambush_refused_target_awake(tmp_path: Path, clock: object) -> None:
    """The SLEEP RULE: a target who has already acted today is un-ambushable.

    See the module docstring for the revert-and-observe check proving this
    refusal has teeth.
    """
    game = _game(tmp_path, clock)
    _arm_ambush(game, target_asleep=False)
    out = game.action("Raider", "ambush", "Sleeper", "")
    assert "watchful today" in out
    # Refused without resolving: no turn spent, no ambush recorded.
    assert game.players["Raider"].turns_left == game.world.settings.daily_turns
    assert game.store.has_ambushed("Raider", "Sleeper", _TODAY) is False


def test_ambush_refused_repeat_same_pair_same_day(tmp_path: Path, clock: object) -> None:
    game = _game(tmp_path, clock)
    attacker, target = _arm_ambush(game)
    # First attempt resolves (attacker overwhelming -> a clean win).
    attacker.atk = 200
    target.hp = 5
    game.action("Raider", "ambush", "Sleeper", "")
    assert game.store.has_ambushed("Raider", "Sleeper", _TODAY) is True
    # Re-arm the target as sleeping AND healed above 1 HP (so the mercy rule
    # does not intercept first); the SAME pair is still barred for the day.
    target.turn_day = _TODAY - 1
    target.hp = 20
    out = game.action("Raider", "ambush", "Sleeper", "")
    assert "already lain in wait" in out


def test_ambush_refused_pile_on_downed_victim(tmp_path: Path, clock: object) -> None:
    """MERCY RULE: a second, DIFFERENT attacker cannot kick a just-bounced sleeper.

    The first ambush leaves the victim at 1 HP (still asleep — being robbed does
    not start their day). A fresh raider then finds them battered in the ditch;
    even bandits have standards, so the pile-on is refused outright — no turn
    spent, no pair-row written for the second attacker.
    """
    game = _game(tmp_path, clock)
    first, target = _arm_ambush(game, target_gold=100)
    first.atk = 200  # one-shot: leaves the victim at 1 HP
    target.hp = 5
    game.action("Raider", "ambush", "Sleeper", "")
    assert target.hp == 1  # downed and still asleep

    # A second, seasoned raider tries to finish the job.
    game.join("Marauder")
    second = game.players["Marauder"]
    second.level = 5
    turns_before = second.turns_left
    out = game.action("Marauder", "ambush", "Sleeper", "")

    assert "battered in the ditch" in out
    # No turn spent and no attempt recorded for the second attacker.
    assert second.turns_left == turns_before
    assert game.store.has_ambushed("Marauder", "Sleeper", _TODAY) is False


def test_ambush_healed_victim_is_ambushable_again(tmp_path: Path, clock: object) -> None:
    """The mercy rule lifts once the victim mends: healed above 1 HP (and still
    asleep), a fresh attacker may strike."""
    game = _game(tmp_path, clock)
    first, target = _arm_ambush(game, target_gold=100)
    first.atk = 200
    target.hp = 5
    game.action("Raider", "ambush", "Sleeper", "")
    assert target.hp == 1

    # The victim is tended back above the floor (still asleep this day).
    target.hp = 18
    game.join("Marauder")
    second = game.players["Marauder"]
    second.level = 5
    second.atk = 200  # one-shot again
    out = game.action("Marauder", "ambush", "Sleeper", "")

    assert "battered in the ditch" not in out
    # The fresh ambush resolved: recorded, and the victim is bounced anew.
    assert game.store.has_ambushed("Marauder", "Sleeper", _TODAY) is True
    assert target.hp == 1


def test_ambush_refused_zero_turns(tmp_path: Path, clock: object) -> None:
    game = _game(tmp_path, clock)
    attacker, _ = _arm_ambush(game)
    attacker.turns_left = 0
    out = game.action("Raider", "ambush", "Sleeper", "")
    assert "spent for today" in out.lower()
    # Eligible but exhausted: nothing recorded (the attempt never landed).
    assert game.store.has_ambushed("Raider", "Sleeper", _TODAY) is False


# ---------------------------------------------------------------------------
# Ambush — outcomes (win / lose / flee) and the records they leave
# ---------------------------------------------------------------------------


def test_ambush_win_transfers_gold_and_bounces_victim(tmp_path: Path, clock: object) -> None:
    game = _game(tmp_path, clock)
    attacker, target = _arm_ambush(game, target_gold=100)
    attacker.atk = 200  # one-shot the sleeper
    target.hp = 5
    pct = game.world.settings.ambush_gold_pct
    steal = 100 * pct // 100  # 25 gold at the shipped 25%
    raider_gold_before = attacker.gold

    out = game.action("Raider", "ambush", "Sleeper", "")

    # Exact transfer: attacker up by steal, victim down by the same.
    assert attacker.gold == raider_gold_before + steal
    assert target.gold == 100 - steal
    # The victim wakes at the spawn at 1 HP, knocked out of any menu.
    assert target.hp == 1
    assert (target.x, target.y) == game.world.spawn
    assert target.mode is Mode.TILE
    assert target.at_location == ""
    assert f"{steal} gold" in out
    # The attempt is recorded.
    assert game.store.has_ambushed("Raider", "Sleeper", _TODAY) is True


def test_ambush_steals_only_carried_gold_not_the_vault(tmp_path: Path, clock: object) -> None:
    """A winning ambush robs carried gold only — banked vault gold is untouched.

    The steal is a slice of ``target.gold`` (gold in hand); the strongbox
    (``banked``) is safe by design. This pins the vault's whole point: bank your
    coin before you sleep and a sleeping-robber cannot lift it.
    """
    game = _game(tmp_path, clock)
    attacker, target = _arm_ambush(game, target_gold=40)
    target.banked = 1000  # a fat vault the raider must not be able to touch
    attacker.atk = 200  # one-shot the sleeper
    target.hp = 5
    pct = game.world.settings.ambush_gold_pct
    steal = 40 * pct // 100  # a slice of the CARRIED 40, not the banked 1000

    game.action("Raider", "ambush", "Sleeper", "")

    assert target.gold == 40 - steal  # carried gold robbed
    assert target.banked == 1000  # the vault is wholly untouched
    assert attacker.gold == game.world.settings.starting_gold + steal


def test_ambush_win_applies_attacker_wear(tmp_path: Path, clock: object) -> None:
    """A multi-round win banks the attacker's wear: the log narrates the
    sleeper's counter-blows, so the sheet must show the HP they cost.

    The one-shot win above leaves the attacker untouched, which would mask a
    WIN branch that drops ``hp_delta`` on the floor. Here the sleeper is tanky
    enough to trade blows before falling (and the attacker still wins), so the
    attacker must end below full HP. Stats and seed are tuned so the win is
    decisive but not instant.
    """
    game = _game(tmp_path, clock)
    attacker, target = _arm_ambush(game, target_gold=100)
    attacker.atk, attacker.def_ = 8, 2
    attacker.hp = attacker.max_hp = 30
    target.atk, target.def_, target.hp = 5, 1, 25

    out = game.action("Raider", "ambush", "Sleeper", "")

    # The win lands (victim robbed and bounced to 1 HP)...
    assert target.hp == 1
    assert (
        any(crow in out for crow in ("made off", "robbed the sleeping", "lifted")) or "rob" in out
    )
    # ...but the sleeper's counter-blows cost the attacker real HP this time.
    assert attacker.hp < attacker.max_hp
    assert attacker.hp >= 1  # never below the floor


def test_ambush_win_news_is_public_and_mail_is_private(tmp_path: Path, clock: object) -> None:
    """The victory crows on the public feed; the victim gets a PRIVATE note.

    A THIRD player must see the public ambush line but never the private one.
    """
    game = _game(tmp_path, clock)
    attacker, target = _arm_ambush(game, target_gold=80)
    attacker.atk = 200
    target.hp = 5
    game.join("Bystander")  # a third player who must never see the private note

    game.action("Raider", "ambush", "Sleeper", "")

    # The victim reads the private "While you slept" note in their own log.
    victim_log = game.log("Sleeper")
    assert "While you slept" in victim_log
    assert "ambushed you" in victim_log

    # The bystander sees the public crow but NOT the private note.
    third_log = game.log("Bystander")
    assert (
        "made off with" in third_log
        or "robbed the sleeping" in third_log
        or ("lifted" in third_log)
    )
    assert "While you slept" not in third_log


def test_ambush_win_on_pauper_steals_nothing_but_still_lands(tmp_path: Path, clock: object) -> None:
    """A win over a penniless sleeper: steal is 0, but the beat still plays.

    The victim is bounced to the spawn at 1 HP all the same, the public herald
    crows the robbery, and the private 'while you slept' note still reaches the
    victim — the gold transfer being empty changes none of that.
    """
    game = _game(tmp_path, clock)
    attacker, target = _arm_ambush(game, target_gold=0)
    attacker.atk = 200  # one-shot the sleeper
    target.hp = 5
    game.join("Bystander")
    raider_gold_before = attacker.gold

    out = game.action("Raider", "ambush", "Sleeper", "")

    # Nothing to steal: both purses are unchanged by the transfer.
    assert attacker.gold == raider_gold_before
    assert target.gold == 0
    assert "0 gold" in out
    # The victim is still bounced to the spawn at 1 HP.
    assert target.hp == 1
    assert (target.x, target.y) == game.world.spawn
    assert target.mode is Mode.TILE
    assert target.at_location == ""

    # Public herald fires (a bystander reads the crow)...
    third_log = game.log("Bystander")
    assert any(crow in third_log for crow in ("made off", "robbed the sleeping", "lifted"))
    # ...and the private mail still reaches the victim.
    victim_log = game.log("Sleeper")
    assert "While you slept" in victim_log
    assert "ambushed you" in victim_log


def test_ambush_lose_bounces_attacker_no_transfer(tmp_path: Path, clock: object) -> None:
    game = _game(tmp_path, clock)
    attacker, target = _arm_ambush(game, target_gold=100)
    # The sleeper is deadly: the ambush rebounds onto the attacker.
    target.atk = 200
    target.def_ = 100
    target.hp = 200
    attacker_gold_before = attacker.gold

    out = game.action("Raider", "ambush", "Sleeper", "")

    # No gold moved; the ATTACKER is the one bounced to spawn at 1 HP.
    assert attacker.gold == attacker_gold_before
    assert target.gold == 100
    assert attacker.hp == 1
    assert (attacker.x, attacker.y) == game.world.spawn
    assert "flee" in out.lower() or "wakes" in out.lower()
    # The attempt is still spent.
    assert game.store.has_ambushed("Raider", "Sleeper", _TODAY) is True


def test_ambush_records_attempt_on_every_outcome(tmp_path: Path, clock: object) -> None:
    """Win, lose, or flee — the (attacker, target, day) row is always written."""
    game = _game(tmp_path, clock)
    attacker, target = _arm_ambush(game)
    # Tune a flee: when neither side can meaningfully dent the other, the fight
    # grinds to the 50-round stalemate guard, which resolves as FLED with no
    # transfer. Both deal the 1-damage floor (atk << def), and both carry far
    # more HP than 50 rounds can drain, so neither drops first.
    attacker.atk, attacker.def_ = 1, 200
    attacker.hp = attacker.max_hp = 500
    target.atk, target.def_, target.hp = 1, 200, 500
    gold_before = attacker.gold

    out = game.action("Raider", "ambush", "Sleeper", "")

    assert game.store.has_ambushed("Raider", "Sleeper", _TODAY) is True
    assert attacker.gold == gold_before  # a flee moves no gold
    assert "slip away" in out.lower() or "nerve" in out.lower()


def test_ambush_next_day_retry_allowed(tmp_path: Path, clock: object) -> None:
    """A new UTC day clears the once-per-pair lock (advance the injected clock)."""
    game = _game(tmp_path, clock)
    attacker, target = _arm_ambush(game)
    attacker.atk = 200
    target.hp = 5
    game.action("Raider", "ambush", "Sleeper", "")
    assert game.store.has_ambushed("Raider", "Sleeper", _TODAY) is True

    # Advance past UTC midnight; re-arm the sleeper for the new day.
    tomorrow = utc(2026, 6, 13, 9, 0)
    game.clock = fixed_clock(tomorrow)  # type: ignore[assignment]
    target.turn_day = tomorrow.toordinal() - 1  # asleep again
    target.hp = 5
    out = game.action("Raider", "ambush", "Sleeper", "")
    assert "already lain in wait" not in out  # the new day permits a fresh attempt
    assert game.store.has_ambushed("Raider", "Sleeper", tomorrow.toordinal()) is True


def test_sleep_rule_guard_has_teeth(tmp_path: Path, clock: object) -> None:
    """Pin the sleep rule on a single-field divergence.

    The un-ambushable case and the ambushable case differ ONLY in ``turn_day``:
    with the target awake the action is refused, and flipping that one field to
    asleep makes the very same attempt resolve and record.
    """
    game = _game(tmp_path, clock)
    attacker, target = _arm_ambush(game, target_asleep=False)
    attacker.atk = 200
    target.hp = 5
    refused = game.action("Raider", "ambush", "Sleeper", "")
    assert "watchful today" in refused
    assert game.store.has_ambushed("Raider", "Sleeper", _TODAY) is False

    # Flip ONLY the sleep field; now the very same attempt lands.
    target.turn_day = _TODAY - 1
    resolved = game.action("Raider", "ambush", "Sleeper", "")
    assert "watchful today" not in resolved
    assert game.store.has_ambushed("Raider", "Sleeper", _TODAY) is True


def test_ambush_both_rows_persist_in_one_transaction(tmp_path: Path, clock: object) -> None:
    """A win commits BOTH fighters' rows; a store reopen sees the transfer."""
    game = _game(tmp_path, clock)
    attacker, target = _arm_ambush(game, target_gold=100)
    attacker.atk = 200
    target.hp = 5
    game.action("Raider", "ambush", "Sleeper", "")
    raider_gold = attacker.gold
    sleeper_gold = target.gold
    game.store.close()

    world = load_world(PACK)
    reopened = Store(tmp_path / "social.db")
    revived = Game(world, reopened, clock=clock)  # type: ignore[arg-type]
    assert revived.players["Raider"].gold == raider_gold
    assert revived.players["Sleeper"].gold == sleeper_gold
    assert revived.players["Sleeper"].hp == 1
    reopened.close()


# ---------------------------------------------------------------------------
# Mail — post delivers privately, confirms, caps, sanitizes
# ---------------------------------------------------------------------------


def test_post_delivers_to_target_once_with_confirmation(tmp_path: Path, clock: object) -> None:
    game = _game(tmp_path, clock)
    game.join("Scribe")
    game.join("Reader")
    confirm = game.action("Scribe", "post", "Reader", "", "meet me at the inn")
    assert "tucks the note" in confirm  # the sender's in-fiction confirmation
    # No turn spent on a post.
    assert game.players["Scribe"].turns_left == game.world.settings.daily_turns

    first = game.log("Reader")
    assert "While you were away" in first
    assert "meet me at the inn" in first
    # Read once: the cursor advanced, so a second read no longer shows it.
    second = game.log("Reader")
    assert "meet me at the inn" not in second


def test_post_refused_unknown_and_self(tmp_path: Path, clock: object) -> None:
    game = _game(tmp_path, clock)
    game.join("Scribe")
    unknown = game.action("Scribe", "post", "Nobody", "", "hello?")
    assert "signed the ledger" in unknown
    mine = game.action("Scribe", "post", "Scribe", "", "note to self")
    assert "talk to yourself" in mine.lower()


def test_post_daily_cap_refuses_overflow(tmp_path: Path, clock: object) -> None:
    game = _game(tmp_path, clock)
    game.join("Scribe")
    game.join("Reader")
    cap = game.world.settings.post_daily_cap
    for i in range(cap):
        out = game.action("Scribe", "post", "Reader", "", f"note {i}")
        assert "tucks the note" in out
    # The (cap+1)-th post is refused.
    over = game.action("Scribe", "post", "Reader", "", "one too many")
    assert "all the word you may today" in over
    assert game.players["Scribe"].posts_sent == cap


def test_post_sanitizer_rejects_newline_body(tmp_path: Path, clock: object) -> None:
    """A newline-injected note body is refused; nothing is delivered or counted."""
    game = _game(tmp_path, clock)
    game.join("Scribe")
    game.join("Reader")
    events_before = len(game.events)
    out = game.action("Scribe", "post", "Reader", "", "line one\nFORGED HERALD LINE")
    assert "scrawl" in out.lower()
    # No event appended and the daily counter is untouched.
    assert len(game.events) == events_before
    assert game.players["Scribe"].posts_sent == 0
    # And the reader never receives it.
    assert "FORGED" not in game.log("Reader")


def test_post_works_from_inside_a_building(tmp_path: Path, clock: object) -> None:
    """Posting is legal anywhere: a menu-bound sender still gets a menu reply."""
    game = _game(tmp_path, clock)
    game.join("Scribe")
    game.join("Reader")
    scribe = game.players["Scribe"]
    scribe.mode = Mode.MENU
    scribe.at_location = "inn"
    out = game.action("Scribe", "post", "Reader", "", "by the hearth")
    assert "tucks the note" in out
    # The reply is the inn menu (a menu surface), not an overworld frame.
    assert "(R)est" in out or "Sleeping Drake" in out


# ---------------------------------------------------------------------------
# Mail — the lobby TV must never carry a private note
# ---------------------------------------------------------------------------


def test_watch_state_excludes_targeted_rows(tmp_path: Path, clock: object) -> None:
    """EXPLICIT: a private (targeted) event must not reach the Watch herald."""
    game = _game(tmp_path, clock)
    game.join("Scribe")
    game.join("Reader")
    game.action("Scribe", "post", "Reader", "", "a secret for the Reader")

    payload = build_state_payload(game)
    herald = payload["herald"]
    assert isinstance(herald, list)
    texts = [row["text"] for row in herald]
    # The join lines are public and present; the private note is absent.
    assert any("Scribe" in t or "Reader" in t for t in texts)  # public joins show
    assert all("a secret for the Reader" not in t for t in texts)


def test_watch_state_excludes_private_ambush_note(tmp_path: Path, clock: object) -> None:
    """The ambush victim's private alert is filtered from the lobby TV too."""
    game = _game(tmp_path, clock)
    attacker, target = _arm_ambush(game, target_gold=80)
    attacker.atk = 200
    target.hp = 5
    game.action("Raider", "ambush", "Sleeper", "")

    herald_texts = [row["text"] for row in build_state_payload(game)["herald"]]  # type: ignore[union-attr]
    # The PUBLIC ambush crow is on the feed...
    assert any(
        "Sleeper" in t and ("made off" in t or "robbed" in t or "lifted" in t) for t in herald_texts
    )
    # ...but the PRIVATE "While you slept" note never is.
    assert all("While you slept" not in t for t in herald_texts)


# ---------------------------------------------------------------------------
# Dice — win / lose / push under a seeded RNG, bands, cap, herald gate
# ---------------------------------------------------------------------------


def _at_inn(game: Game, name: str) -> object:
    """Join *name* and seat them at the inn (MENU surface)."""
    game.join(name)
    player = game.players[name]
    player.mode = Mode.MENU
    player.at_location = "inn"
    return player


def test_gamble_win_under_seeded_rng(tmp_path: Path, clock: object) -> None:
    game = _game(tmp_path, clock)
    player = _at_inn(game, "Gambler")
    player.gold = 100
    # Seed 2 makes the gamble child roll 11 (you) vs 9 (house) -> a win.
    game.rng = GameRNG(seed=2)
    out = game.action("Gambler", "gamble", "", "", "", 10)
    assert player.gold == 110  # stake doubled back
    assert "win" in out.lower()
    # No turn spent; one game counted.
    assert player.turns_left == game.world.settings.daily_turns
    assert player.gambles == 1


def test_gamble_lose_under_seeded_rng(tmp_path: Path, clock: object) -> None:
    game = _game(tmp_path, clock)
    player = _at_inn(game, "Gambler")
    player.gold = 100
    # Seed 0 rolls 4 (you) vs 9 (house) -> a loss.
    game.rng = GameRNG(seed=0)
    out = game.action("Gambler", "gamble", "", "", "", 10)
    assert player.gold == 90
    assert "lose" in out.lower()
    assert player.gambles == 1


def test_gamble_push_under_seeded_rng(tmp_path: Path, clock: object) -> None:
    game = _game(tmp_path, clock)
    player = _at_inn(game, "Gambler")
    player.gold = 100
    # Seed 1 rolls 6 vs 6 -> a push: no gold change, but it still counts.
    game.rng = GameRNG(seed=1)
    out = game.action("Gambler", "gamble", "", "", "", 10)
    assert player.gold == 100
    assert "push" in out.lower()
    assert player.gambles == 1  # a push still consumes a daily game


def test_gamble_bet_band_refused(tmp_path: Path, clock: object) -> None:
    game = _game(tmp_path, clock)
    player = _at_inn(game, "Gambler")
    player.gold = 100_000
    max_bet = game.world.settings.gamble_max_bet
    low = game.action("Gambler", "gamble", "", "", "", 0)
    assert f"1 to {max_bet}" in low
    high = game.action("Gambler", "gamble", "", "", "", max_bet + 1)
    assert f"1 to {max_bet}" in high
    # A rejected bet neither moves gold nor counts toward the cap.
    assert player.gold == 100_000
    assert player.gambles == 0


def test_gamble_unaffordable_refused(tmp_path: Path, clock: object) -> None:
    game = _game(tmp_path, clock)
    player = _at_inn(game, "Gambler")
    player.gold = 5
    out = game.action("Gambler", "gamble", "", "", "", 10)  # within band, can't cover
    assert "can't cover" in out.lower()
    assert player.gold == 5
    assert player.gambles == 0


def test_gamble_daily_cap_refused(tmp_path: Path, clock: object) -> None:
    game = _game(tmp_path, clock)
    player = _at_inn(game, "Gambler")
    player.gold = 100_000
    cap = game.world.settings.gamble_daily_cap
    player.gambles = cap  # already at the cap
    out = game.action("Gambler", "gamble", "", "", "", 5)
    assert "enough for one day" in out
    assert player.gambles == cap  # not incremented past the cap


def test_gamble_outside_inn_refused(tmp_path: Path, clock: object) -> None:
    """The dice live at the inn: the verb is illegal in another building."""
    game = _game(tmp_path, clock)
    player = _at_inn(game, "Gambler")
    player.at_location = "shop"  # the shop has no 'gamble' action
    player.gold = 100
    out = game.action("Gambler", "gamble", "", "", "", 10)
    assert "can't 'gamble' here" in out.lower()
    assert player.gold == 100


def test_gamble_big_win_heralds(tmp_path: Path, clock: object) -> None:
    """A win of >= 25 gold reaches the public Herald; a small one does not."""
    game = _game(tmp_path, clock)
    player = _at_inn(game, "Gambler")
    player.gold = 1000

    # A 50-gold win (>= the 25 threshold) writes a public dice line.
    game.rng = GameRNG(seed=2)  # a winning roll
    events_before = len(game.events)
    game.action("Gambler", "gamble", "", "", "", 50)
    new = game.events[events_before:]
    assert any(e.kind == "gamble" and e.target == "" for e in new)
    assert player.gold == 1050


def test_gamble_small_win_is_quiet(tmp_path: Path, clock: object) -> None:
    game = _game(tmp_path, clock)
    player = _at_inn(game, "Gambler")
    player.gold = 1000
    # A 10-gold win is below the 25-gold Herald threshold: no public line.
    game.rng = GameRNG(seed=2)
    events_before = len(game.events)
    game.action("Gambler", "gamble", "", "", "", 10)
    new = game.events[events_before:]
    assert all(e.kind != "gamble" for e in new)
    assert player.gold == 1010


# ---------------------------------------------------------------------------
# The Vault — deposit/withdraw at the inn (no turn; banked gold is safe)
# ---------------------------------------------------------------------------


def test_deposit_moves_gold_to_the_vault_no_turn(tmp_path: Path, clock: object) -> None:
    """Deposit moves coin from hand to vault, costs no turn, and is friendly."""
    game = _game(tmp_path, clock)
    player = _at_inn(game, "Saver")
    player.gold = 100
    turns_before = player.turns_left

    out = game.action("Saver", "deposit", "", "", "", 60)

    assert player.gold == 40
    assert player.banked == 60
    assert player.turns_left == turns_before  # banking spends no turn
    assert "strongbox" in out.lower()


def test_withdraw_moves_gold_back_to_hand(tmp_path: Path, clock: object) -> None:
    """Withdraw moves coin from vault to hand."""
    game = _game(tmp_path, clock)
    player = _at_inn(game, "Saver")
    player.gold = 10
    player.banked = 90

    game.action("Saver", "withdraw", "", "", "", 50)

    assert player.gold == 60
    assert player.banked == 40


def test_deposit_amount_exceeding_holdings_refused(tmp_path: Path, clock: object) -> None:
    """Depositing more than you carry is refused without mutation."""
    game = _game(tmp_path, clock)
    player = _at_inn(game, "Saver")
    player.gold = 30
    player.banked = 0

    out = game.action("Saver", "deposit", "", "", "", 50)

    assert player.gold == 30  # unchanged
    assert player.banked == 0
    assert "1 to 30" in out


def test_deposit_with_nothing_in_hand_refused(tmp_path: Path, clock: object) -> None:
    """Depositing with an empty hand is a friendly refusal."""
    game = _game(tmp_path, clock)
    player = _at_inn(game, "Saver")
    player.gold = 0

    out = game.action("Saver", "deposit", "", "", "", 10)

    assert player.banked == 0
    assert "no coin" in out.lower()


def test_withdraw_amount_exceeding_vault_refused(tmp_path: Path, clock: object) -> None:
    """Withdrawing more than is banked is refused without mutation."""
    game = _game(tmp_path, clock)
    player = _at_inn(game, "Saver")
    player.gold = 0
    player.banked = 20

    out = game.action("Saver", "withdraw", "", "", "", 50)

    assert player.gold == 0
    assert player.banked == 20  # unchanged
    assert "1 to 20" in out


def test_withdraw_empty_vault_refused(tmp_path: Path, clock: object) -> None:
    """Withdrawing from an empty vault is a friendly refusal."""
    game = _game(tmp_path, clock)
    player = _at_inn(game, "Saver")
    player.banked = 0

    out = game.action("Saver", "withdraw", "", "", "", 10)

    assert player.gold == game.world.settings.starting_gold  # unchanged
    assert "empty" in out.lower()


def test_status_shows_carried_and_vault_gold(tmp_path: Path, clock: object) -> None:
    """door_status reports gold as carried-on-hand plus banked-in-the-vault."""
    game = _game(tmp_path, clock)
    game.join("Saver")
    player = game.players["Saver"]
    player.gold = 75
    player.banked = 250

    out = game.status("Saver")

    assert "75 on hand" in out
    assert "250 in the vault" in out
