"""The probe of which log-probabilities a server reports, against a server
whose own values move between two identical requests.

No container.  The stand-in below is a client object with a toy model behind
it: a distribution over a small vocabulary at every position, a sampler with
temperature, top-k and top-p, and a choice of which of the three the reported
values are taken after.  Unlike the scripted chat server, its values can move
from request to request the way a real server's do when it serves other
requests at the same time, and its sampler really samples, so a request may
come back with any token.  The last class asks the scripted chat server,
through the SDK, about a model that is certain of the first token of every
reply.
"""

from __future__ import annotations

import itertools
import math
import random
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import openai
import pytest

from tests._scripted_chat_server import ScriptedChatServer
from turnstone.rollout.probe import (
    SETTINGS,
    _one_possible,
    _same_place,
    _shown,
    _unsure_place,
    sampler_logprobs_reported,
)

if TYPE_CHECKING:
    from collections.abc import Callable

# What the stand-in writes for a token that has no probability.
IMPOSSIBLE = -9999.0
# The first position of a reply as a live server gave it for the first probe
# prompt: the own log-probabilities of its twenty likeliest tokens.
MEASURED = (-0.7686, -0.9305, -2.4294, -4.1529, -5.0183, -5.2778, -5.8651, -5.9006, -6.7333)
MEASURED += (-6.9877, -7.0183, -7.1968, -7.1999, -7.2354, -7.2473, -7.2780, -7.4110, -7.5970)
MEASURED += (-7.6277, -7.9067)


def log_softmax(logits: list[float], kept: set[int] | None = None) -> list[float]:
    """Log-probabilities of *logits* over the tokens in *kept* (all of them
    without it); a token outside has none."""
    inside = range(len(logits)) if kept is None else kept
    top = max(logits[i] for i in inside)
    total = top + math.log(math.fsum(math.exp(logits[i] - top) for i in inside))
    return [
        logits[i] - total if kept is None or i in kept else IMPOSSIBLE for i in range(len(logits))
    ]


SIZE = 96


def with_tail(own: tuple[float, ...]) -> list[float]:
    """Logits whose likeliest tokens have the log-probabilities *own*, the rest
    of the probability spread evenly over the other tokens of the vocabulary."""
    rest = 1.0 - math.fsum(math.exp(v) for v in own)
    assert rest > 0.0
    return [*own, *[math.log(rest / (SIZE - len(own)))] * (SIZE - len(own))]


def drawn(seed: int) -> list[float]:
    """Logits of a first position no model is certain of, drawn from *seed*:
    the likeliest token has at most 0.85 of the probability."""
    source = random.Random(f"first/{seed}")
    logits = [source.gauss(0.0, 2.0) for _ in range(SIZE)]
    top = max(range(SIZE), key=lambda i: logits[i])
    rest = math.log(math.fsum(math.exp(v) for i, v in enumerate(logits) if i != top))
    logits[top] = min(logits[top], rest + math.log(0.85 / 0.15))
    return logits


def likely(seed: int, share: float = 0.97) -> list[float]:
    """Logits of a position the model is all but certain of, drawn from
    *seed*: one token holds *share* of the probability, and the others what a
    drawn position gives them."""
    source = random.Random(f"certain/{seed}")
    logits = [source.gauss(0.0, 2.0) for _ in range(SIZE)]
    top = max(range(SIZE), key=lambda i: logits[i])
    rest = math.log(math.fsum(math.exp(v) for i, v in enumerate(logits) if i != top))
    logits[top] = rest + math.log(share / (1.0 - share))
    return logits


def below_the_level(by: float) -> list[float]:
    """Logits of a first position whose token 2 has an own value *by* below the
    level at which a temperature of a half leaves a value as it is (the
    logarithm of the sum of the squared probabilities): the temperature makes
    that token less likely by *by*."""
    weights = [0.40, 0.20, 0.0, 0.08, 0.04, *[0.002] * (SIZE - 5)]
    share = 0.15
    for _ in range(200):
        scale = (1.0 - share) / math.fsum(weights)
        level = math.log(share * share + math.fsum((w * scale) ** 2 for w in weights))
        share = math.exp(level - by)
    scale = (1.0 - share) / math.fsum(weights)
    return [math.log(share if i == 2 else w * scale) for i, w in enumerate(weights)]


class StandIn:
    """An SDK client's shape with a toy model and a real sampler behind it.

    *first* gives the logits of the first position per probe prompt, in the
    order the prompts are first asked; the positions after it, and a prompt
    *first* does not reach, get logits drawn from *seed*.

    *reflects* names the sampler settings the reported values are taken after
    at the first position, and *later* at the positions after it (the same
    unless given).

    *certain_for* makes the model all but certain of that many positions of
    every reply, from the first on, where *first* does not say otherwise for
    the first: one token (number 0) holds all but three hundredths of the
    probability there.

    *moves* is how far every logit may move, up or down, from one request to
    the next: what serving other requests does to a real server's values.
    *disturb* changes the logits of one request by hand (it is given the
    request's number, counted from 0, and the logits), and *pick* names the
    token a request's first position comes back with, instead of sampling;
    *pick_at* does that for any position (it is given the request's number
    and the position).
    """

    def __init__(
        self,
        *,
        reflects: tuple[str, ...] = SETTINGS,
        later: tuple[str, ...] | None = None,
        first: list[list[float]] | None = None,
        moves: float = 0.0,
        seed: int = 0,
        listed: int = 20,
        disturb: Callable[[int, list[float]], list[float]] | None = None,
        pick: Callable[[int], int | None] | None = None,
        normalised: bool = True,
        applied_temperature: float | None = None,
        sampled_value_off: float = 0.0,
        certain_for: int = 0,
        pick_at: Callable[[int, int], int | None] | None = None,
    ) -> None:
        self.base_url = "http://stand-in.invalid/v1/"
        self.reflects, self.later = reflects, reflects if later is None else later
        self.first, self.moves, self.seed, self.size = first or [], moves, seed, SIZE
        self.listed, self.disturb, self.pick = listed, disturb, pick
        self.certain_for, self.pick_at = certain_for, pick_at
        # What can be wrong with the values of a server that fits neither kind.
        self.normalised = normalised
        self.applied_temperature = applied_temperature
        self.sampled_value_off = sampled_value_off
        self.requests: list[dict[str, Any]] = []
        self._prompts: list[str] = []
        self._random = random.Random(seed)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._chat))
        self.completions = SimpleNamespace(create=self._completion)

    # -- the model ---------------------------------------------------------------

    def _logits(self, prompt: str, before: tuple[int, ...]) -> list[float]:
        if prompt not in self._prompts:
            self._prompts.append(prompt)
        number = self._prompts.index(prompt)
        if not before and number < len(self.first):
            return list(self.first[number])
        if len(before) < self.certain_for:
            return certain()
        drawn = random.Random(f"{self.seed}/{number}/{before}")
        return [drawn.gauss(0.0, 2.5) for _ in range(self.size)]

    # -- one request ---------------------------------------------------------------

    def _entries(
        self, prompt: str, *, temperature: float, top_k: int, top_p: float, tokens: int, wanted: int
    ) -> list[tuple[int, float, list[tuple[int, float]]]]:
        """Per generated position: the sampled token, the value reported for
        it and the alternatives listed, likeliest first."""
        number = len(self.requests) - 1
        before: tuple[int, ...] = ()
        out = []
        for position in range(tokens):
            logits = self._logits(prompt, before)
            if self.moves:
                logits = [v + self._random.uniform(-self.moves, self.moves) for v in logits]
            if self.disturb is not None and position == 0:
                logits = self.disturb(number, logits)
            # The sampler: temperature, then the two cut-offs.
            scaled = [v / temperature for v in logits]
            order = sorted(range(self.size), key=lambda i: -scaled[i])
            in_top_k = set(order[:top_k]) if top_k > 0 else set(order)
            probabilities = [math.exp(v) for v in log_softmax(scaled)]
            in_top_p, total = set(), 0.0
            for i in order:
                in_top_p.add(i)
                total += probabilities[i]
                if total >= top_p:
                    break
            kept = in_top_k & in_top_p
            weights = [probabilities[i] if i in kept else 0.0 for i in range(self.size)]
            token = self._random.choices(range(self.size), weights)[0]
            picked = self.pick(number) if self.pick is not None and position == 0 else None
            if picked is None and self.pick_at is not None:
                picked = self.pick_at(number, position)
            token = token if picked is None else picked
            # What is reported: the model's own values, with the settings this
            # server takes its values after applied to them.
            after = self.reflects if position == 0 else self.later
            divisor = temperature if "temperature" in after else 1.0
            if self.applied_temperature is not None and temperature != 1.0:
                divisor = self.applied_temperature
            shown = set(range(self.size))
            if "top_k" in after:
                shown &= in_top_k
            if "top_p" in after:
                shown &= in_top_p
            if self.normalised:
                reported = log_softmax([v / divisor for v in logits], shown)
            else:
                own = log_softmax(logits)
                reported = [
                    own[i] / divisor if i in shown else IMPOSSIBLE for i in range(self.size)
                ]
            ranked = sorted(range(self.size), key=lambda i: -reported[i])
            ranked = ranked[: min(wanted, self.listed)]
            if token in ranked:
                ranked.remove(token)
                ranked.insert(0, token)
            value = reported[token] + (self.sampled_value_off if position == 0 else 0.0)
            out.append((token, value, [(i, reported[i]) for i in ranked]))
            before = (*before, token)
        return out

    # -- the two routes ---------------------------------------------------------------

    def _chat(self, **request: Any) -> Any:
        self.requests.append(request)
        prompt = str(request["messages"][0]["content"])
        entries = self._entries(
            prompt,
            temperature=request["temperature"],
            top_k=request["extra_body"]["top_k"],
            top_p=request["top_p"],
            tokens=request["max_tokens"],
            wanted=request["top_logprobs"],
        )

        def named(token: int, value: float) -> dict[str, Any]:
            text = f"t{token}"
            return {"token": text, "logprob": value, "bytes": list(text.encode())}

        content = [
            {**named(token, value), "top_logprobs": [named(*other) for other in listed]}
            for token, value, listed in entries
        ]
        return SimpleNamespace(choices=[SimpleNamespace(logprobs={"content": content})])

    def post(self, path: str, *, body: dict[str, Any], cast_to: Any) -> dict[str, Any]:
        """The tokenize route: a prompt's characters are its tokens."""
        assert path.endswith("/tokenize")
        return {"tokens": [ord(c) for c in body["messages"][0]["content"]]}

    def _completion(self, **request: Any) -> Any:
        self.requests.append(request)
        prompt = "".join(chr(i) for i in request["prompt"])
        entries = self._entries(
            prompt,
            temperature=request["temperature"],
            top_k=request["extra_body"]["top_k"],
            top_p=request["top_p"],
            tokens=request["max_tokens"],
            wanted=request["logprobs"],
        )
        logprobs = SimpleNamespace(
            tokens=[f"token_id:{token}" for token, _value, _listed in entries],
            token_logprobs=[value for _token, value, _listed in entries],
            top_logprobs=[
                {f"token_id:{other}": other_value for other, other_value in listed}
                for _token, _value, listed in entries
            ],
        )
        return SimpleNamespace(choices=[SimpleNamespace(logprobs=logprobs)])


def truth(reflects: tuple[str, ...]) -> dict[str, bool]:
    return {name: name in reflects for name in SETTINGS}


EVERY_KIND = [
    tuple(name for name, taken in zip(SETTINGS, chosen, strict=True) if taken)
    for chosen in itertools.product((False, True), repeat=3)
]


def wrong_and_undecided(
    moves: float, *, position: Callable[[int], list[float]] = drawn, **probe: Any
) -> tuple[list[Any], int, int]:
    """Probe every kind of server with a dozen seeds each while every logit
    moves by up to *moves* between any two requests: the settings that came
    out wrong, how many came out without a verdict, and how many there were.
    *position* draws the first position of each prompt from a seed."""
    wrong, undecided, settings = [], 0, 0
    for seed, reflects in itertools.product(range(12), EVERY_KIND):
        first = [position(3 * seed + n) for n in range(3)]
        server = StandIn(reflects=reflects, first=first, moves=moves, seed=seed)
        verdict = sampler_logprobs_reported(server, "m", **probe)
        for name, expected in truth(reflects).items():
            said = verdict["reflects"][name]
            settings += 1
            if said is None:
                undecided += 1
            elif said is not expected:
                wrong.append((seed, reflects, name, verdict["tried"]))
    return wrong, undecided, settings


class TestWhatItSays:
    @pytest.mark.parametrize("reflects", EVERY_KIND)
    def test_each_setting_by_itself_on_a_server_that_holds_still(self, reflects):
        server = StandIn(reflects=reflects, first=[with_tail(MEASURED)])
        verdict = sampler_logprobs_reported(server, "m")
        assert verdict["reflects"] == truth(reflects)
        summed = True if len(reflects) == 3 else False if not reflects else None
        assert verdict["sampler_logprobs"] is summed

    def test_what_a_probe_costs(self):
        """Five requests where the first prompt decides: the control, one for
        each setting, and one for the alternatives at the later positions."""
        server = StandIn(first=[with_tail(MEASURED)])
        verdict = sampler_logprobs_reported(server, "m")
        assert [r["max_tokens"] for r in server.requests] == [1, 24, 24, 1, 24]
        (tried,) = verdict["tried"]
        assert tried["used"] is True and tried["likeliest"] == pytest.approx(-0.7686, abs=1e-4)
        assert tried["top_k"] == tried["top_p"] == 0.0
        assert tried["temperature_slope"] == pytest.approx(2.0, abs=1e-3)
        assert tried["temperature_alternatives"] == 20
        assert verdict["positions"]["top_k"] == {"checked": 23, "contradicting": 0}
        assert verdict["positions"]["whole"] == {"checked": 24, "contradicting": 0}

    def test_on_a_server_reporting_the_models_own_it_is_four(self):
        server = StandIn(reflects=(), first=[with_tail(MEASURED)])
        verdict = sampler_logprobs_reported(server, "m")
        assert verdict["sampler_logprobs"] is False and len(server.requests) == 4
        assert verdict["tried"][0]["temperature_slope"] == pytest.approx(1.0, abs=1e-3)


def lower(token: int, by: float, on: int) -> Callable[[int, list[float]], list[float]]:
    """A disturbance: the logit of *token* is lower by *by* in request *on*."""

    def disturb(number: int, logits: list[float]) -> list[float]:
        if number == on:
            logits = list(logits)
            logits[token] -= by
        return logits

    return disturb


class TestUnderLoad:
    """A busy server: its values move by a few hundredths between the control
    and the request held against it.  First the two cases in which the value
    of the one token that came back says the wrong thing, then every kind of
    server with every value moving."""

    def test_a_wrong_false_by_the_sampled_tokens_value(self):
        """A server that reports after the temperature.  Its control comes back
        with the likeliest token at -0.794 where a quiet server says -0.769,
        and the request with the temperature comes back with the second token,
        whose sharpened value (-0.890) is 0.04 from its own (-0.931).  Held
        against that control, the sharpened value lies within a few hundredths
        of the token's own value: read from that one value, the verdict is
        "before the temperature"."""
        server = StandIn(first=[with_tail(MEASURED)], disturb=lower(0, 0.047, on=0))
        server.pick = lambda number: 1 if number == 3 else None
        verdict = sampler_logprobs_reported(server, "m")
        (tried,) = verdict["tried"]
        assert tried["likeliest"] == pytest.approx(-0.794, abs=2e-3)
        assert tried["temperature"] == pytest.approx(-0.8897, abs=2e-3)
        assert verdict["reflects"] == truth(SETTINGS)
        assert tried["temperature_slope"] == pytest.approx(2.0, abs=0.02)

    def test_the_mirror_case_a_wrong_true(self):
        """A server that reports BEFORE the temperature.  The request with the
        temperature comes back with a token the temperature makes less likely
        by 0.12, and the control had that token's own value 0.06 too high.
        The sharpened value worked out from that control is then the token's
        true own value, which is what the server reports: by the sampled
        token's value alone the verdict is "after the temperature"."""
        logits = below_the_level(0.12)
        own = log_softmax(logits)[2]
        raised = lower(2, -0.06 / (1.0 - math.exp(own)), on=0)
        server = StandIn(reflects=(), first=[logits], disturb=raised)
        server.pick = lambda number: 2 if number == 3 else None
        verdict = sampler_logprobs_reported(server, "m")
        (tried,) = verdict["tried"]
        assert tried["temperature"] == pytest.approx(own, abs=1e-6)
        assert verdict["reflects"] == truth(())
        assert tried["temperature_slope"] == pytest.approx(1.0, abs=0.02)

    @pytest.mark.parametrize("moves", [0.05, 0.15, 0.4])
    def test_no_wrong_verdict_whatever_moves(self, moves):
        """Every logit moves by up to *moves* between any two requests: about
        what was measured beside four rollouts, three times that, and eight
        times that.  A setting may come out without a verdict; it never comes
        out with the wrong one."""
        wrong, undecided, settings = wrong_and_undecided(moves)
        assert wrong == []
        # At what was measured every probe decides.
        if moves <= 0.05:
            assert undecided == 0, f"{undecided} of {settings} settings without a verdict"

    def test_the_measured_position_moving_as_measured_always_decides(self):
        """The first position a live server gave, moving by a tenth: 300 probes
        of each kind of server, none wrong and none without a verdict."""
        for seed, reflects in itertools.product(range(300), [SETTINGS, ()]):
            server = StandIn(reflects=reflects, first=[with_tail(MEASURED)], moves=0.1, seed=seed)
            verdict = sampler_logprobs_reported(server, "m")
            assert verdict["reflects"] == truth(reflects), (seed, verdict["tried"])
            assert len(verdict["tried"]) == 1


def certain(value: float = -0.03) -> list[float]:
    """A first position whose likeliest token has the log-probability *value*."""
    return with_tail((value,))


class TestWhatItLeavesOpen:
    """Where the evidence is not clear there is no verdict, never a guess."""

    def test_a_model_that_is_all_but_certain_of_every_token(self):
        """Its own values are near zero wherever it is asked, so a zero proves
        nothing anywhere: one first token, the first positions of two more
        replies, and no verdict.  A third reply is not asked for."""
        server = StandIn(certain_for=24, pick_at=lambda number, position: 0)
        verdict = sampler_logprobs_reported(server, "m")
        assert verdict["reflects"] == dict.fromkeys(SETTINGS)
        assert verdict["reason"] == (
            "the model was certain of every token of every reply it was asked for"
        )
        assert [r["max_tokens"] for r in server.requests] == [1, 24, 24]
        assert [t["used"] for t in verdict["tried"]] == [False, False, False]
        assert [t.get("certain_positions") for t in verdict["tried"]] == [None, 24, 24]

    def test_the_next_prompt_is_tried_and_decides(self):
        server = StandIn(first=[certain(), with_tail(MEASURED)])
        verdict = sampler_logprobs_reported(server, "m")
        assert verdict["reflects"] == truth(SETTINGS)
        assert [t["used"] for t in verdict["tried"]] == [False, True]

    def test_where_certain_begins(self):
        """The model must leave the other tokens about a tenth of the
        probability: 0 and the control's value are then far apart."""
        barely = StandIn(first=[certain(-0.11)])
        (tried,) = sampler_logprobs_reported(barely, "m")["tried"]
        assert tried["used"] is True and tried["position"] == 0
        # Just short of it, and as certain of every token after the first.
        not_quite = StandIn(first=[certain(-0.09)] * 3, certain_for=24)
        verdict = sampler_logprobs_reported(not_quite, "m")
        assert verdict["sampler_logprobs"] is None
        assert [t["used"] for t in verdict["tried"]] == [False, False, False]

    def test_a_temperature_that_is_another_than_the_requests(self):
        """Values sharpened, but not by the temperature that was asked for:
        neither the model's own nor the sampling distribution's."""
        server = StandIn(first=[with_tail(MEASURED)] * 3, applied_temperature=0.75)
        verdict = sampler_logprobs_reported(server, "m")
        assert verdict["reflects"]["temperature"] is None
        assert verdict["reason"] == "no measurement could decide: temperature"
        assert [t["temperature_slope"] for t in verdict["tried"]] == [
            pytest.approx(1 / 0.75, abs=1e-3)
        ] * 3

    def test_values_that_are_no_probabilities(self):
        """Divided by the temperature and not made a distribution again: the
        slope is the sampling distribution's, the values are not."""
        server = StandIn(reflects=("temperature",), first=[with_tail(MEASURED)] * 3)
        server.normalised = False
        verdict = sampler_logprobs_reported(server, "m")
        assert verdict["reflects"]["temperature"] is None

    def test_a_sampled_token_whose_value_is_not_the_one_listed_for_it(self):
        server = StandIn(first=[with_tail(MEASURED)] * 3, sampled_value_off=-0.3)
        verdict = sampler_logprobs_reported(server, "m")
        assert verdict["reflects"]["temperature"] is None
        # A cut-off's one token is then reported with neither 0 nor its own value.
        assert verdict["reflects"]["top_k"] is None and verdict["reflects"]["top_p"] is None

    def test_a_server_that_lists_no_alternatives(self):
        server = StandIn(first=[with_tail(MEASURED)] * 3, listed=1)
        verdict = sampler_logprobs_reported(server, "m")
        assert verdict["reflects"]["temperature"] is None
        assert verdict["tried"][0]["temperature_alternatives"] <= 1

    def test_later_positions_that_contradict_the_first(self):
        """The first position reported after the cut-offs, the others before."""
        server = StandIn(first=[with_tail(MEASURED)], later=("temperature",))
        verdict = sampler_logprobs_reported(server, "m")
        assert verdict["sampler_logprobs"] is None
        assert verdict["reason"].startswith("the positions after the first contradict it")
        assert verdict["positions"]["top_k"]["contradicting"] > 0


def the_certain_token(number: int) -> int | None:
    """Every request comes back with token 0 first: the one a model that is
    certain of its first token (``certain``) is certain of."""
    return 0


class TestAModelCertainOfItsFirstToken:
    """A model can open every reply with the same token and be unsure a few
    tokens on.  The probe measures the server, not the model, so it measures
    at the first position the model is unsure of."""

    @pytest.mark.parametrize("reflects", EVERY_KIND)
    def test_each_setting_is_measured_at_a_later_position(self, reflects):
        server = StandIn(reflects=reflects, first=[certain()] * 3, pick=the_certain_token)
        verdict = sampler_logprobs_reported(server, "m")
        assert verdict["reflects"] == truth(reflects)
        # The first prompt was asked for one token; the second is the first
        # whose reply is at hand beyond that.
        asked_for_one, measured, not_needed = verdict["tried"]
        assert measured["used"] is True and measured["position"] == 1
        assert measured["likeliest"] == pytest.approx(-0.03, abs=1e-3)
        assert measured["likeliest_at_position"] <= -0.1
        # The first position showed the same kind of value for each setting.
        assert measured["first"] == truth(reflects)
        for other in (asked_for_one, not_needed):
            assert set(other) == {"prompt", "likeliest", "used"} and other["used"] is False

    def test_what_such_a_probe_costs(self):
        """The first token of the first prompt, and once that has turned out
        certain the first positions of the other two replies; then for one
        prompt the two cut-offs, the temperature up to the position it is
        measured at, and the alternatives."""
        server = StandIn(first=[certain()] * 3, pick=the_certain_token)
        verdict = sampler_logprobs_reported(server, "m")
        assert [r["max_tokens"] for r in server.requests] == [1, 24, 24, 24, 24, 2, 24]
        assert verdict["positions"]["top_k"] == {"checked": 23, "contradicting": 0}
        assert verdict["positions"]["top_p"] == {"checked": 23, "contradicting": 0}
        assert verdict["positions"]["whole"] == {"checked": 24, "contradicting": 0}
        tried = verdict["tried"][1]
        assert tried["top_k"] == tried["top_p"] == 0.0
        assert tried["temperature_slope"] == pytest.approx(2.0, abs=1e-3)

    def test_the_first_token_of_another_prompt_comes_before_a_later_position(self):
        """Where the first token of one prompt is unsure, that token decides,
        and no later position of another prompt is looked at."""
        server = StandIn(first=[certain(), with_tail(MEASURED)])
        verdict = sampler_logprobs_reported(server, "m")
        assert [r["max_tokens"] for r in server.requests] == [1, 24, 24, 24, 1, 24]
        assert [t.get("position") for t in verdict["tried"]] == [None, 0]
        assert verdict["reflects"] == truth(SETTINGS)

    def test_a_control_reply_that_leaves_the_likeliest_tokens_is_not_used(self):
        """A request that leaves the sampler one token follows the model's
        likeliest tokens.  A control reply that took another token where the
        model was certain stands at a place no such request comes to."""

        def strays(number: int, position: int) -> int | None:
            # Request 1 is the second prompt's control, the first at hand.
            return (5 if number == 1 else 0) if position == 0 else None

        server = StandIn(first=[certain()] * 3, pick_at=strays)
        verdict = sampler_logprobs_reported(server, "m")
        first, second, third = verdict["tried"]
        assert second["used"] is False and second["certain_positions"] == 0
        assert third["used"] is True and third["position"] >= 1
        # The prompt that was asked for one token was not needed again.
        assert first["used"] is False and "certain_positions" not in first
        assert verdict["reflects"] == truth(SETTINGS)
        asked = [r["max_tokens"] for r in server.requests]
        assert asked == [1, 24, 24, 24, 24, third["position"] + 1, 24]

    def test_where_every_control_reply_leaves_them_the_reason_says_so(self):
        """The two replies at hand were not certain of every token, so the
        prompt that was asked for one token is asked for more as well."""
        server = StandIn(first=[certain()] * 3, pick=lambda number: 5)
        verdict = sampler_logprobs_reported(server, "m")
        assert verdict["reflects"] == dict.fromkeys(SETTINGS)
        assert verdict["reason"] == (
            "the model was certain of the first token of every probe prompt, and no control "
            "reply came by its likeliest tokens to a token it was unsure of"
        )
        assert [t["certain_positions"] for t in verdict["tried"]] == [0, 0, 0]
        assert [r["max_tokens"] for r in server.requests] == [1, 24, 24, 24]

    def test_the_prompt_asked_for_one_token_is_asked_for_more_when_the_others_fail(self):
        """The two replies at hand left the likeliest tokens, which is chance
        and not the model's certainty: the first prompt, asked for a single
        token so far, is asked for its reply's first positions and decides."""

        def strays(number: int, position: int) -> int | None:
            # Requests 1 and 2 are the controls of the second and third prompt.
            return (5 if number in (1, 2) else 0) if position == 0 else None

        server = StandIn(first=[certain()] * 3, pick_at=strays)
        verdict = sampler_logprobs_reported(server, "m")
        first, second, third = verdict["tried"]
        assert second["certain_positions"] == third["certain_positions"] == 0
        assert first["used"] is True and first["position"] >= 1
        assert verdict["reflects"] == truth(SETTINGS)
        asked = [r["max_tokens"] for r in server.requests]
        assert asked == [1, 24, 24, 24, 24, 24, first["position"] + 1, 24]

    def test_a_reply_that_parts_from_the_control_is_not_held_against_it(self):
        """The request with the temperature comes back with another first
        token than the control's reply: at the position it stands elsewhere,
        and what the control lists there is not its distribution.  Nothing is
        read from it, and the next prompt decides the temperature."""

        def parts(number: int, position: int) -> int | None:
            # Request 5 is the one with the temperature, for the second prompt.
            return (5 if number == 5 else 0) if position == 0 else None

        server = StandIn(first=[certain()] * 3, pick_at=parts)
        verdict = sampler_logprobs_reported(server, "m")
        _first, second, third = verdict["tried"]
        assert "top_k" in second and "top_p" in second and "temperature" not in second
        assert "temperature" in third and "top_k" not in third
        assert verdict["reflects"] == truth(SETTINGS)

    @pytest.mark.parametrize(("first", "later"), [((), SETTINGS), (SETTINGS, ())])
    def test_a_first_position_that_reports_otherwise_leaves_no_verdict(self, first, later):
        """A later position need not speak for the first, which a server
        computes by its ordinary path.  The model is so certain of its first
        token here (-0.005) that the value reported for that token fits either
        kind; the alternatives listed there, and their slope under the
        temperature, do not."""
        server = StandIn(reflects=first, later=later, first=[certain(-0.005)] * 3)
        server.pick = the_certain_token
        verdict = sampler_logprobs_reported(server, "m")
        assert verdict["reflects"] == dict.fromkeys(SETTINGS)
        assert verdict["reason"] == (
            "the positions after the first contradict it: temperature, top_k, top_p"
        )
        tried = verdict["tried"][1]
        assert tried["position"] == 1 and tried["first"] == truth(first)
        if later:
            # No value contradicted the zero at the position that decided.
            assert verdict["positions"]["top_k"] == {"checked": 23, "contradicting": 0}

    def test_a_first_position_whose_value_shows_it_is_a_contradiction_as_before(self):
        """At -0.03 the first token's own value is no zero: the first position
        reports the model's own, the later ones after the cut-offs."""
        server = StandIn(reflects=(), later=SETTINGS, first=[certain()] * 3)
        server.pick = the_certain_token
        verdict = sampler_logprobs_reported(server, "m")
        assert verdict["reflects"] == dict.fromkeys(SETTINGS)
        assert verdict["positions"]["top_k"] == {"checked": 23, "contradicting": 1}

    def test_a_first_position_that_cannot_be_read_leaves_the_setting_open(self):
        """A server that lists one alternative shows nothing at a position
        the model is certain of."""
        server = StandIn(first=[certain()] * 3, listed=1, pick=the_certain_token)
        verdict = sampler_logprobs_reported(server, "m")
        assert verdict["reflects"] == dict.fromkeys(SETTINGS)
        assert verdict["reason"] == "no measurement could decide: temperature, top_k, top_p"

    @pytest.mark.parametrize("moves", [0.05, 0.15, 0.4])
    def test_no_wrong_verdict_whatever_moves(self, moves):
        """Every kind of server, the model certain of the first token of
        every prompt (0.97), every logit moving between any two requests."""
        wrong, undecided, settings = wrong_and_undecided(moves, position=likely)
        assert wrong == []
        if moves <= 0.05:
            assert undecided == 0, f"{undecided} of {settings} settings without a verdict"


# A reply whose second token the scripted server's toy model is unsure of in
# the way a measurement needs: its own value is -0.201, and the other token of
# its pair lies more than a unit below.
OPENS_THE_SAME = [{"reasoning": "We need a note.", "content": "done"}]


def scripted(**played: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The verdict of a probe of the scripted chat server on its chat route,
    and the requests it took."""
    with ScriptedChatServer(OPENS_THE_SAME, **played) as server:
        client = openai.OpenAI(base_url=server.base_url, api_key="none", max_retries=0)
        try:
            verdict = sampler_logprobs_reported(client, "m")
        finally:
            client.close()
        return verdict, list(server.requests)


class TestAServerWhoseModelOpensEveryReplyTheSame:
    """Through the SDK, on the chat route: the scripted server's toy model
    made certain of the first token of every reply (``confident_first``) or
    of every token (``confident``)."""

    def test_the_settings_are_measured_at_the_second_token(self):
        verdict, requests = scripted(sampler_logprobs=True, confident_first=1)
        assert verdict["reflects"] == truth(SETTINGS) and verdict["sampler_logprobs"] is True
        assert [r["max_tokens"] for r in requests] == [1, 24, 24, 24, 24, 2, 24]
        asked_for_one, measured, not_needed = verdict["tried"]
        assert measured["used"] is True and measured["position"] == 1
        assert measured["likeliest"] > -0.001
        assert measured["likeliest_at_position"] == pytest.approx(-0.201, abs=1e-6)
        assert measured["top_k"] == measured["top_p"] == 0.0
        assert measured["first"] == truth(SETTINGS)
        assert asked_for_one["used"] is False and not_needed["used"] is False

    def test_a_server_reporting_the_models_own_values_is_told_there_too(self):
        verdict, requests = scripted(confident_first=1)
        assert verdict["reflects"] == truth(()) and verdict["sampler_logprobs"] is False
        assert len(requests) == 6
        tried = verdict["tried"][1]
        assert tried["top_k"] == tried["top_p"] == tried["temperature"]
        assert tried["top_k"] == pytest.approx(-0.201, abs=1e-6) and tried["first"] == truth(())

    @pytest.mark.parametrize("reflected", [("temperature",), ("top_k", "top_p"), ("top_p",)])
    def test_each_setting_is_measured_by_itself_there(self, reflected):
        verdict, _requests = scripted(reflects=reflected, confident_first=1)
        assert verdict["reflects"] == truth(reflected)

    def test_a_model_certain_of_every_token_has_no_verdict_and_the_reason_says_so(self):
        verdict, requests = scripted(sampler_logprobs=True, confident=True)
        assert verdict["reflects"] == dict.fromkeys(SETTINGS)
        assert verdict["reason"] == (
            "the model was certain of every token of every reply it was asked for"
        )
        assert [r["max_tokens"] for r in requests] == [1, 24, 24]
        tried = verdict["tried"]
        assert [t["used"] for t in tried] == [False, False, False]
        assert "certain_positions" not in tried[0]
        assert tried[1]["certain_positions"] == tried[2]["certain_positions"] > 1

    def test_a_first_position_reported_otherwise_than_the_rest_has_no_verdict(self):
        """The first position under the sampling distribution, the rest the
        model's own.  The second token says "the model's own"; the first
        position lists one possible token under a cut-off."""
        verdict, _requests = scripted(
            sampler_logprobs=True, first_position_only=True, confident_first=1
        )
        assert verdict["reflects"] == dict.fromkeys(SETTINGS)
        assert verdict["reason"].startswith("the positions after the first contradict it")
        assert verdict["tried"][1]["first"] == truth(SETTINGS)


def entry(token: str, value: float, *listed: tuple[str, float]) -> dict[str, Any]:
    """One position of a reply as the chat route gives it: the token that came
    back with its value, and the alternatives listed there."""

    def named(text: str, logprob: float) -> dict[str, Any]:
        return {"token": text, "logprob": logprob, "bytes": list(text.encode())}

    return {**named(token, value), "top_logprobs": [named(*other) for other in listed]}


class TestTheSamePlace:
    """What the control lists at a later position is another reply's
    distribution only where that reply came there by the same tokens, and a
    name alone does not say which token came back: two tokens can share one."""

    def test_a_token_is_shown_by_its_name_once_with_its_value(self):
        assert _shown(entry("a", -0.02, ("a", -0.02), ("b", -4.0))) == ("a", (97,))
        # Two tokens of that name are listed: which of them came back is not shown.
        assert _shown(entry("a", -0.02, ("a", -0.02), ("a", -5.0), ("b", -4.0))) is None
        # The one listed has another value than the token that came back:
        # that token is another one of the same name, further down.
        assert _shown(entry("a", -7.5, ("a", -0.02), ("b", -4.0))) is None
        # Not among the alternatives at all, and no number for a value.
        assert _shown(entry("c", -9.0, ("a", -0.02), ("b", -4.0))) is None
        assert _shown(entry("a", float("nan"), ("a", -0.02))) is None

    def test_two_replies_stand_at_the_same_place_while_their_tokens_are_shown_the_same(self):
        control = [
            entry("a", -0.02, ("a", -0.02), ("b", -4.0)),
            entry("x", -0.01, ("x", -0.01), ("y", -6.0)),
            entry("m", -0.9, ("m", -0.9), ("n", -0.6)),
        ]
        same = [entry("a", 0.0, ("a", 0.0)), entry("x", 0.0, ("x", 0.0)), entry("n", 0.0)]
        assert _same_place(control, same, 0) and _same_place(control, same, 2)
        other = [entry("a", 0.0, ("a", 0.0)), entry("y", 0.0, ("y", 0.0)), entry("n", 0.0)]
        assert _same_place(control, other, 1) and not _same_place(control, other, 2)
        # The same names, but the second token is not shown to be the control's.
        twin = [same[0], entry("x", -7.0, ("x", -0.01), ("y", -6.0)), same[2]]
        assert not _same_place(control, twin, 2)
        # A reply that ended before the position has nothing there.
        assert not _same_place(control, same[:2], 2)

    def test_the_place_is_the_first_position_the_model_is_unsure_of(self):
        sure = entry("a", -0.02, ("a", -0.02), ("b", -4.0))
        unsure = entry("m", -0.9, ("m", -0.9), ("n", -0.6))
        assert _unsure_place([sure, sure, unsure, sure]) == (2, 2)
        assert _unsure_place([unsure, sure]) == (0, 0)
        # Certain of every position: the count is the reply's length.
        assert _unsure_place([sure, sure, sure]) == (None, 3)
        # The reply took another token than the likeliest where the model was
        # certain: no request that follows the likeliest tokens comes there.
        strayed = entry("b", -4.0, ("a", -0.02), ("b", -4.0))
        assert _unsure_place([sure, strayed, unsure]) == (None, 1)
        # Unsure by the likeliest token, whichever token came back.
        assert _unsure_place([sure, entry("n", -0.6, ("m", -0.9), ("n", -0.6))]) == (1, 1)

    def test_what_a_certain_position_shows_under_a_cut_off(self):
        own = {("a", (97,)): -0.02, ("b", (98,)): -4.0}
        after = entry("a", 0.0, ("a", 0.0), ("b", -9999.0))
        before = entry("a", -0.02, ("a", -0.02), ("b", -4.0))
        assert _one_possible(own, after) is True
        assert _one_possible(own, before) is False
        # One token listed with a value that is no zero, or another token
        # than the one that came back: neither kind.
        assert _one_possible(own, entry("a", -0.02, ("a", -0.02))) is None
        assert _one_possible(own, entry("a", 0.0, ("b", 0.0))) is None
        # A control that listed no alternatives there shows nothing either way.
        assert _one_possible({("a", (97,)): -0.02}, after) is None
