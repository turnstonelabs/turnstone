"""The probe on the completions route, where a run with prompts as token IDs
asks: the same decisions as on the chat route, read from that route's shape of
log-probabilities, with tokens named by their IDs.

The stand-in is the one of ``test_rollout_probe.py``.
"""

from __future__ import annotations

import pytest

from tests.test_rollout_probe import (
    EVERY_KIND,
    MEASURED,
    SETTINGS,
    StandIn,
    certain,
    likely,
    the_certain_token,
    truth,
    with_tail,
    wrong_and_undecided,
)
from turnstone.rollout.probe import sampler_logprobs_reported


class TestOnTheCompletionsRoute:
    @pytest.mark.parametrize("reflects", EVERY_KIND)
    def test_each_setting_by_itself(self, reflects):
        server = StandIn(reflects=reflects, first=[with_tail(MEASURED)])
        verdict = sampler_logprobs_reported(server, "m", route="completions")
        assert verdict["reflects"] == truth(reflects) and verdict["route"] == "completions"
        # Every request was a prompt of token IDs, the alternatives asked for by number.
        assert all(isinstance(request["prompt"], list) for request in server.requests)
        assert {request["logprobs"] for request in server.requests} <= {20, 5}

    def test_the_chat_route_says_so_too(self):
        server = StandIn(first=[with_tail(MEASURED)])
        assert sampler_logprobs_reported(server, "m")["route"] == "chat"
        assert all("messages" in request for request in server.requests)

    def test_the_slope_is_read_from_alternatives_named_by_token_id(self):
        server = StandIn(first=[with_tail(MEASURED)])
        verdict = sampler_logprobs_reported(server, "m", route="completions")
        (tried,) = verdict["tried"]
        assert tried["temperature_slope"] == pytest.approx(2.0, abs=1e-3)
        assert tried["temperature_alternatives"] == 20
        assert [request["max_tokens"] for request in server.requests] == [1, 24, 24, 1, 24]

    @pytest.mark.parametrize("moves", [0.05, 0.15, 0.4])
    def test_no_wrong_verdict_whatever_moves(self, moves):
        wrong, undecided, settings = wrong_and_undecided(moves, route="completions")
        assert wrong == []
        if moves <= 0.05:
            assert undecided == 0, f"{undecided} of {settings} settings without a verdict"


class TestAModelCertainOfItsFirstTokenOnTheCompletionsRoute:
    """The settings measured at the first later position the model is unsure
    of, with every token named by its ID."""

    @pytest.mark.parametrize("reflects", EVERY_KIND)
    def test_each_setting_is_measured_at_a_later_position(self, reflects):
        server = StandIn(reflects=reflects, first=[certain()] * 3, pick=the_certain_token)
        verdict = sampler_logprobs_reported(server, "m", route="completions")
        assert verdict["reflects"] == truth(reflects) and verdict["route"] == "completions"
        measured = verdict["tried"][1]
        assert measured["position"] == 1 and measured["first"] == truth(reflects)
        assert all(isinstance(request["prompt"], list) for request in server.requests)
        asked = [request["max_tokens"] for request in server.requests]
        assert asked[:6] == [1, 24, 24, 24, 24, 2]

    def test_a_model_certain_of_every_token_has_no_verdict(self):
        server = StandIn(certain_for=24, pick_at=lambda number, position: 0)
        verdict = sampler_logprobs_reported(server, "m", route="completions")
        assert verdict["reflects"] == dict.fromkeys(SETTINGS)
        assert "certain of every token" in verdict["reason"] and len(server.requests) == 3

    @pytest.mark.parametrize(("first", "later"), [((), SETTINGS), (SETTINGS, ())])
    def test_a_first_position_that_reports_otherwise_leaves_no_verdict(self, first, later):
        server = StandIn(reflects=first, later=later, first=[certain(-0.005)] * 3)
        server.pick = the_certain_token
        verdict = sampler_logprobs_reported(server, "m", route="completions")
        assert verdict["reflects"] == dict.fromkeys(SETTINGS)
        assert verdict["reason"].startswith("the positions after the first contradict it")

    @pytest.mark.parametrize("moves", [0.05, 0.15, 0.4])
    def test_no_wrong_verdict_whatever_moves(self, moves):
        wrong, undecided, settings = wrong_and_undecided(
            moves, position=likely, route="completions"
        )
        assert wrong == []
        if moves <= 0.05:
            assert undecided == 0, f"{undecided} of {settings} settings without a verdict"
