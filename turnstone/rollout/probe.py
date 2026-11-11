"""Check which log-probabilities a server reports.

A trainer that weights by the sampler's probability needs the log-probability
of each sampled token under the distribution it was sampled from (after
temperature, top-k and top-p).  A server can instead report the model's own
log-probabilities, before those steps, or apply some of the steps before it
reports and others after.  The recording client stores whatever the server
reports, so a rollout is only trainable when this is known for every sampler
setting it uses.

The probe measures each of the three settings by itself.  A *control* request
asks for one token and the most likely alternatives at that position, with
temperature 1 and nothing cut off: the model's own distribution.  Then one
request per setting changes that setting alone:

- ``top_k`` of 1 and a ``top_p`` near zero each leave the sampler one token.
  Under the sampling distribution it is reported with log-probability 0; a
  server reporting the model's own repeats the control's value for it.
- a lower ``temperature`` spreads the log-probabilities of the alternatives
  apart: reported after it, the differences between the listed alternatives
  are the control's divided by the temperature; reported before it, they are
  the control's.

**A wrong verdict is worse than none.**  A server's own values move between
two identical requests when it serves other requests at the same time: by a
few hundredths for a likely token and by a tenth for an unlikely one, measured
on one server beside four rollouts.  So each decision rests on a difference
that such movement cannot produce, and what is not clearly one or the other
has no verdict (``None``), which is not a pass:

- for a cut-off the two candidates are 0 and the control's value for the token
  that came back, and the control must have left that token no more than
  about nine tenths of the probability.  For one candidate to be taken for
  the other, the token's probability would have to move from under 0.905 to
  over 0.99;
- for the temperature the decision is the slope of the listed alternatives'
  values against the control's, over all of them: 1 when reported before the
  temperature, 2 at the probe's temperature of a half when reported after it.
  The alternatives span several units, so a movement of a tenth changes the
  slope by a hundredth.  The value of the sampled token itself is not what
  decides: for a token whose probability the temperature hardly changes the
  two candidates are a few hundredths apart, which is the size of the
  movement.

A prompt whose first token the model is all but certain of is set aside and
the next one is tried.

**The probe measures the server, not the model.**  A model can be certain of
the first token of every reply (the word its reasoning opens with) and unsure
a few tokens on.  So once a first token has turned out certain, the control
of each further prompt asks for the first positions of the reply, not for
one token; and where the first tokens leave a setting without a verdict, the
prompts that were set aside are taken up again, and the settings are measured
at the first position at which the control says the model was unsure enough,
by the same rules.  Two replies to one prompt stand at the same place for as
long as they hold the same tokens, so what the control lists at that position
is the model's own distribution for every reply that came to it by the
control's tokens, and for no other.  A request that leaves the sampler one
token follows the model's likeliest tokens, so the position has to be one the
control's reply reached by those, and a reply is held against the control
only where each token before the position is shown to be the control's.  The
prompt that was asked for a single token is asked for more last, and not at
all where the model was certain of every token of the replies at hand.

The position that decides shows which kind of value a server reports.  It
need not speak for the others: a server that drafts several tokens in one
step takes the later ones from another head, by another path.  So what it
says is held against the other positions:

- under ``top_k`` of 1 and under a ``top_p`` near zero every position has one
  possible token, so every reported value must be 0, not only the one that
  decides;
- with a temperature and a small ``top_k``, the alternatives the server lists
  at each position are that position's whole distribution: their
  probabilities must sum to 1, and the sampled token must be one of them.

Where a later position decides, the same holds the other way round: it need
not speak for the first position, which a server computes by its ordinary
path, and the value of a token the model is certain of says little.  The
first position is then held against the decision by what it still shows:

- under a cut-off that leaves one token, the alternatives listed there: one
  possible token where the values are taken after the cut-off, the control's
  alternatives where they are taken before it;
- under the temperature, the slope of its alternatives against the control's.

A first position that shows the other kind is a contradiction, and one that
shows neither leaves the setting without a verdict.

A server whose positions contradict each other has no verdict for the
settings concerned.  What the other positions cannot show is a server that
applies the cut-offs there and leaves the temperature out of the reported
values; the temperature is measured at the position that decides, and at the
first position where that is another.
"""

from __future__ import annotations

import copy
import math
from typing import Any

from turnstone.rollout.recording import entry_value, logprob_entries

SETTINGS = ("temperature", "top_k", "top_p")

# Prompts whose first token a model is rarely sure of.
_PROMPTS = (
    "Write sixteen random lowercase letters separated by spaces. Output only the letters.",
    "Pick a random number between 1 and 1000 and reply with only the number.",
    "Name one colour, one animal and one city, each chosen at random.",
)
# The most likely first token must be this far from certain for a verdict: the
# model leaves the other tokens about a tenth of the probability or more.
_UNCERTAIN_BELOW = -0.1
# A value this near 0 is that of a token the sampler had no choice about.
_ZERO_WITHIN = 0.01
# The temperature of the temperature measurement.
_PROBE_TEMPERATURE = 0.5
# Alternatives asked for at the probed position.
_ALTERNATIVES = 20
# The listed alternatives must span this much for their slope to mean anything.
_SPREAD_AT_LEAST = 1.0
# How far the slope may be from 1 (reported before the temperature) or from the
# temperature's inverse (after it), as a share of that value.
_SLOPE_WITHIN = 0.1
# What a server writes for a token that has no probability at all.
_IMPOSSIBLE_BELOW = -100.0
# Positions a reply is checked at beyond the first.
_POSITIONS = 24
# The cut-off of the request whose alternatives must make a whole distribution.
_WHOLE_TOP_K = 5

_Name = tuple[str, tuple[int, ...]]


def _first_entry(response: Any) -> Any:
    entries = logprob_entries(response.choices[0])
    return entries[0] if entries else None


def _not_certain(entries: list[Any]) -> int:
    """Positions whose reported value is not that of the only possible token."""
    return sum(1 for e in entries if abs(float(entry_value(e, "logprob"))) > _ZERO_WITHIN)


def _not_whole(entries: list[Any]) -> int:
    """Positions whose listed alternatives are not one whole distribution with
    the sampled token in it."""
    wrong = 0
    for entry in entries:
        alternatives = entry_value(entry, "top_logprobs") or []
        values = [float(entry_value(a, "logprob")) for a in alternatives]
        total = sum(math.exp(v) for v in values if v > _IMPOSSIBLE_BELOW)
        listed = {_name(a) for a in alternatives}
        if abs(total - 1.0) > 1e-3 or _name(entry) not in listed:
            wrong += 1
    return wrong


def _name(entry: Any) -> _Name:
    return str(entry_value(entry, "token")), tuple(entry_value(entry, "bytes") or ())


def _listed(entry: Any) -> dict[_Name, float]:
    """The alternatives a server lists at one position that have a probability,
    each with its value.  A name listed twice stands for two tokens that cannot
    be told apart, and is left out."""
    values: dict[_Name, float] = {}
    twice: set[_Name] = set()
    for alternative in entry_value(entry, "top_logprobs") or []:
        name = _name(alternative)
        if name in values:
            twice.add(name)
        values[name] = float(entry_value(alternative, "logprob"))
    return {
        name: value
        for name, value in values.items()
        if name not in twice and math.isfinite(value) and value > _IMPOSSIBLE_BELOW
    }


def _own(entry: Any) -> dict[_Name, float]:
    """The model's own distribution at a position of the control, as far as
    the control lists it, with the token that came back."""
    own = _listed(entry)
    own.setdefault(_name(entry), float(entry_value(entry, "logprob")))
    return own


def _shown(entry: Any) -> _Name | None:
    """The token a position came back with, where the alternatives listed
    there show which one it is: its name stands among them once, with the
    value reported for it.  ``None`` where they do not: two tokens can share
    a name, and a name alone does not say which of them came back."""
    name = _name(entry)
    listed = _listed(entry).get(name)
    if listed is not None and abs(listed - float(entry_value(entry, "logprob"))) <= 1e-3:
        return name
    return None


def _unsure_place(control: list[Any]) -> tuple[int | None, int]:
    """Where a control reply lets the settings be measured, and how many of
    its positions, from the first on, the model was certain of.

    The place is the first position whose likeliest token the control leaves
    no more than about nine tenths of the probability, provided the reply
    came to it by the model's likeliest tokens: a request that leaves the
    sampler one token goes that way and no other.  ``None`` where the model
    was certain of every position (the count is then the reply's length), and
    where the reply took another token than the likeliest at a position the
    model was certain of (the count is then that position): nothing after it
    is a place such a request comes to.
    """
    for position, entry in enumerate(control):
        own = _own(entry)
        likeliest = max(own, key=own.__getitem__)
        if own[likeliest] <= _UNCERTAIN_BELOW:
            return position, position
        if _shown(entry) != likeliest:
            return None, position
    return None, len(control)


def _same_place(control: list[Any], entries: list[Any], place: int) -> bool:
    """Whether a reply stands at position *place* where the control's reply
    stood there: it came to it by the same tokens, each shown to be the same.
    What the control lists at *place* is then the model's own distribution
    for this reply too."""
    if min(len(control), len(entries)) <= place:
        return False
    return all(
        _shown(mine) is not None and _shown(mine) == _shown(theirs)
        for mine, theirs in zip(control[:place], entries[:place], strict=True)
    )


def _one_possible(own: dict[_Name, float], entry: Any) -> bool | None:
    """What the alternatives listed at a position show under a cut-off that
    leaves the sampler one token, whatever the model's certainty there:
    ``True`` where the token that came back is the only one listed with a
    probability, with the value 0 (values taken after the cut-off), ``False``
    where several are (taken before it), ``None`` where neither is shown.

    *own* is the control's listing at the same position.  A server that
    lists no alternatives there shows nothing either way.
    """
    if len(own) < 2:
        return None
    listed = _listed(entry)
    if len(listed) > 1:
        return False
    if set(listed) == {_name(entry)} and abs(listed[_name(entry)]) <= _ZERO_WITHIN:
        return True
    return None


def _cut_off(reported: float, own: float | None) -> bool | None:
    """Which candidate the value reported for the one token a cut-off left is:
    ``True`` that of the sampling distribution (0), ``False`` the model's own
    (*own*, the control's value for that token), ``None`` when it cannot be
    told.

    It cannot be told for a token the control did not list, and for one the
    model was all but certain of anyway: 0 and its own value are then too near
    each other.  The band around the model's own value reaches half-way to 0
    and no further.
    """
    if own is None or own > _UNCERTAIN_BELOW:
        return None
    if abs(reported) <= _ZERO_WITHIN:
        return True
    if abs(reported - own) <= -own / 2:
        return False
    return None


def _sharpened(
    own: dict[_Name, float], entry: Any, temperature: float
) -> tuple[bool | None, dict[str, Any]]:
    """Whether the values a server lists under *temperature* are taken after it
    (``True``), before it (``False``) or neither clearly (``None``), and what
    that was read from.

    *own* is the control's distribution at the same position and *entry* the
    position's entry from the request with the temperature.  Reported after
    the temperature, each alternative's value is the control's divided by the
    temperature, less a constant; reported before it, it is the control's.  So
    the slope of one against the other, over every alternative both requests
    list, is the temperature's inverse or 1.
    """
    listed = _listed(entry)
    pairs = [(own[name], listed[name]) for name in own if name in listed]
    found: dict[str, Any] = {"alternatives": len(pairs), "slope": None}
    if len(pairs) < 2:
        return None, found
    spread = max(x for x, _ in pairs) - min(x for x, _ in pairs)
    if spread < _SPREAD_AT_LEAST:
        return None, found
    mean_x = math.fsum(x for x, _ in pairs) / len(pairs)
    mean_y = math.fsum(y for _, y in pairs) / len(pairs)
    slope = math.fsum((x - mean_x) * (y - mean_y) for x, y in pairs) / math.fsum(
        (x - mean_x) ** 2 for x, _ in pairs
    )
    found["slope"] = round(slope, 4)
    off = [y - mean_y - slope * (x - mean_x) for x, y in pairs]
    if math.sqrt(math.fsum(d * d for d in off) / len(off)) > 0.1 * spread:
        return None, found  # no line at all: neither kind of value
    # The value of the sampled token must be the one listed for it, and the
    # listed alternatives must be probabilities: together no more than 1, and
    # no less of the whole than the control's hold.
    sampled = listed.get(_name(entry))
    if sampled is None or abs(sampled - float(entry_value(entry, "logprob"))) > 1e-3:
        return None, found
    mass = math.fsum(math.exp(y) for y in listed.values())
    if mass > 1.0 + 1e-3 or mass < math.fsum(math.exp(x) for x in own.values()) - 0.05:
        return None, found
    if abs(slope - 1.0) <= _SLOPE_WITHIN:
        return False, found
    if abs(slope * temperature - 1.0) <= _SLOPE_WITHIN:
        return True, found
    return None, found


def all_settings(verdict: bool | None) -> dict[str, bool | None]:
    """One verdict for every setting: what a caller that already knows passes."""
    return dict.fromkeys(SETTINGS, verdict)


def sampler_logprobs_reported(
    client: Any, model: str, *, extra_body: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Probe the server behind *client* (an SDK client); return the verdict and its evidence.

    ``reflects`` says, per sampler setting, whether the reported
    log-probabilities are taken after it (``True``), before it (``False``) or
    could not be told (``None``).  ``sampler_logprobs`` sums that up: ``True``
    when all three are reflected (the sampling distribution), ``False`` when
    none is (the model's own), ``None`` otherwise.  *extra_body* is sent with
    every request (chat-template arguments the rollout itself uses); its
    sampler settings are replaced by the probe's.

    ``tried`` holds one entry per probe prompt: the ``prompt``, the control's
    ``likeliest`` value for the first token, whether the prompt was ``used``,
    the ``position`` of the reply its settings were measured at (0, the first
    token, unless the model was certain of that one), and for each setting
    that was measured on it the value reported for the token at that position
    (``temperature``, ``top_k``, ``top_p``); for the temperature also what the
    decision was read from (``temperature_slope``, and how many alternatives
    it is the slope of, ``temperature_alternatives``).  A prompt measured at a
    later position also says the control's likeliest value there
    (``likeliest_at_position``) and what the first position showed for each
    setting (``first``); one that was looked at beyond its first token and
    not used says how many positions the model was certain of
    (``certain_positions``).
    """
    body = copy.deepcopy(extra_body or {})
    for key in ("top_k", "top_p", "min_p"):
        body.pop(key, None)
    reflects: dict[str, bool | None] = all_settings(None)
    tried: list[dict[str, Any]] = []
    # What the positions other than the one that decided said, per check.
    positions: dict[str, dict[str, int]] = {}
    contradicted: list[str] = []
    # The prompts whose first token the model was certain of, each with its
    # control reply where that was asked for beyond the first token, and its
    # entry in ``tried``.
    aside: list[tuple[list[dict[str, str]], list[Any] | None, dict[str, Any]]] = []

    def reply(
        messages: list[dict[str, str]],
        *,
        temperature: float,
        top_k: int,
        top_p: float,
        tokens: int = 1,
        alternatives: int = _ALTERNATIVES,
    ) -> list[Any]:
        """The log-probability entries of one reply of up to *tokens* tokens."""
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=tokens,
            temperature=temperature,
            top_p=top_p,
            logprobs=True,
            top_logprobs=alternatives,
            extra_body={**body, "top_k": top_k},
        )
        return logprob_entries(response.choices[0])

    def result(reason: str) -> dict[str, Any]:
        values = set(reflects.values())
        verdict = True if values == {True} else False if values == {False} else None
        return {
            "sampler_logprobs": verdict,
            "reflects": reflects,
            "reason": reason,
            "tried": tried,
            "positions": positions,
        }

    def measure(
        messages: list[dict[str, str]], control: list[Any], place: int, note: dict[str, Any]
    ) -> bool:
        """Measure every setting that has no verdict yet at position *place*
        of the replies to *messages*, against what the *control* lists there,
        and note what was found.  ``False`` where the server returned no
        log-probabilities."""
        own = _own(control[place])
        note["used"], note["position"] = True, place
        if place:
            note["likeliest_at_position"] = max(own.values())
            # What the first position shows, per setting measured here.
            note["first"] = {}
        # (temperature, top_k, top_p) with one setting changed.
        measurements: dict[str, tuple[float, int, float]] = {
            "top_k": (1.0, 1, 1.0),
            "top_p": (1.0, 0, 0.001),
            "temperature": (_PROBE_TEMPERATURE, 0, 1.0),
        }
        for setting, (temperature, top_k, top_p) in measurements.items():
            if reflects[setting] is not None:
                continue
            # The two cut-offs are asked over several positions: one decides,
            # the others must not contradict it.  The temperature is asked up
            # to the position it is measured at.
            several = _POSITIONS if setting != "temperature" else place + 1
            entries = reply(
                messages, temperature=temperature, top_k=top_k, top_p=top_p, tokens=several
            )
            if not entries:
                return False
            if not _same_place(control, entries, place):
                # The reply left the control's tokens before the position:
                # nothing the control lists there is this reply's.
                continue
            entry, others = entries[place], entries[:place] + entries[place + 1 :]
            reported = float(entry_value(entry, "logprob"))
            note[setting] = reported
            said: bool | None
            if setting == "temperature":
                said, found = _sharpened(own, entry, temperature)
                note["temperature_slope"] = found["slope"]
                note["temperature_alternatives"] = found["alternatives"]
            else:
                said = _cut_off(reported, own.get(_name(entry)))
                if said is True and others:
                    # One possible token at every position: every value must be 0.
                    off = _not_certain(others)
                    positions[setting] = {"checked": len(others), "contradicting": off}
                    if off:
                        said = None
                        contradicted.append(setting)
            if place and said is not None:
                # A later position decided: the first must show the same.
                first_own = _own(control[0])
                first = (
                    _sharpened(first_own, entries[0], temperature)[0]
                    if setting == "temperature"
                    else _one_possible(first_own, entries[0])
                )
                note["first"][setting] = first
                if first is not said:
                    if first is not None:
                        contradicted.append(setting)
                    said = None
            reflects[setting] = said
        if reflects["top_k"] is True and "whole" not in positions:
            # With the cut-off reflected, the alternatives listed at a position
            # are its whole distribution, at every position.
            entries = reply(
                messages,
                temperature=_PROBE_TEMPERATURE,
                top_k=_WHOLE_TOP_K,
                top_p=1.0,
                tokens=_POSITIONS,
                alternatives=_WHOLE_TOP_K,
            )
            off = _not_whole(entries)
            positions["whole"] = {"checked": len(entries), "contradicting": off}
            if off:
                reflects["top_k"] = reflects["temperature"] = None
                contradicted.append("the alternatives")
        return True

    # The first token of each prompt, where the model is unsure of it.
    for prompt in _PROMPTS:
        if None not in reflects.values() or contradicted:
            break
        messages = [{"role": "user", "content": prompt}]
        # One token is asked for, until the model has turned out certain of a
        # first token: from then on the first positions of the reply, so
        # that a later one is at hand.
        whole = bool(aside)
        control = reply(
            messages, temperature=1.0, top_k=0, top_p=1.0, tokens=_POSITIONS if whole else 1
        )
        if not control:
            return result("the server returned no log-probabilities")
        # The model's own distribution at the first position, as far as listed.
        likeliest = max(_own(control[0]).values())
        note: dict[str, Any] = {"prompt": prompt, "likeliest": likeliest, "used": False}
        tried.append(note)
        if likeliest > _UNCERTAIN_BELOW:
            aside.append((messages, control if whole else None, note))
        elif not measure(messages, control, 0, note):
            return result("the server returned no log-probabilities")

    # Then, for a setting that is still open, the first later position the
    # model was unsure of in a reply whose first token it was certain of:
    # the replies that are at hand first, then the prompt that was asked for
    # a single token.  ``everywhere``: the model was certain of every token
    # of every reply looked at so far.
    everywhere = True
    at_hand_first = sorted(aside, key=lambda item: item[1] is None)
    for looked, (messages, at_hand, note) in enumerate(at_hand_first):
        if None not in reflects.values() or contradicted:
            break
        if at_hand is None:
            if looked and everywhere:
                # Certain of every token of the replies at hand: one more
                # reply is not asked for.
                break
            at_hand = reply(messages, temperature=1.0, top_k=0, top_p=1.0, tokens=_POSITIONS)
            if not at_hand:
                return result("the server returned no log-probabilities")
        place, certain = _unsure_place(at_hand)
        everywhere = everywhere and place is None and certain == len(at_hand)
        if place is None:
            note["certain_positions"] = certain
        elif not measure(messages, at_hand, place, note):
            return result("the server returned no log-probabilities")

    undecided = [name for name, value in reflects.items() if value is None]
    if contradicted:
        return result(
            "the positions after the first contradict it: " + ", ".join(sorted(set(contradicted)))
        )
    if not any(t.get("used") for t in tried):
        if everywhere:
            return result("the model was certain of every token of every reply it was asked for")
        return result(
            "the model was certain of the first token of every probe prompt, and no control "
            "reply came by its likeliest tokens to a token it was unsure of"
        )
    if undecided:
        return result(f"no measurement could decide: {', '.join(undecided)}")
    if all(reflects.values()):
        return result("a token the sampler had to pick is reported with log-probability 0")
    if not any(reflects.values()):
        return result(
            "a token the sampler had to pick is reported with the model's own log-probability"
        )
    return result(
        "the server reports log-probabilities after some sampler settings and before others"
    )
