"""What a verdict about a server was made for, and whether a run may take it.

Two things are asked of a server before a run, and can be asked once for a
whole batch: which log-probabilities it reports
(:mod:`turnstone.rollout.probe`), and whether it can be sent prompts as token
IDs (:func:`turnstone.rollout.idprompt.check_token_id_route`).  A verdict
handed to a run in place of asking again is good for what it was made for and
for nothing else, so each verdict names that:

- ``model``: the model's name as it was asked for;
- ``endpoint``: the API root the questions went to;
- ``checked_at``: when, in ISO 8601 and UTC;

beside what it already says of itself: the probe the ``route`` it asked on,
the check the reply ``format`` and the ``template_arguments`` it rendered with.

A run refuses a verdict made for another model, at another endpoint, on the
other route, for another format or with other template arguments.  It refuses
one that does not say, too: a verdict without these names nothing it could be
held against, and has to be made again.  And it refuses one of the wrong
shape with a sentence.  Every refusal is a
:class:`~turnstone.rollout.settings.SetupError`: nothing was run.

What cannot be held against anything is the time.  ``checked_at`` is kept in
the record, for a reader who knows when the server was last restarted.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from turnstone.rollout.settings import SetupError

# The sampler settings a probe's verdict speaks of, and the routes it can
# have asked on.
SETTINGS = ("temperature", "top_k", "top_p")
ROUTES = ("chat", "completions")
# What each verdict must say it was made for.
_PROBE_NAMES = ("model", "endpoint", "route", "checked_at")
_CHECK_NAMES = ("model", "endpoint", "checked_at", "format", "template_arguments")


def endpoint_of(client: Any) -> str | None:
    """The API root an SDK client asks, as a verdict names it; ``None`` for a
    client that does not say."""
    base = getattr(client, "base_url", None)
    return str(base).rstrip("/") if base is not None else None


def made_at(endpoint: str | None, model: str) -> dict[str, Any]:
    """What a verdict made now, by asking *model* at *endpoint*, was made for."""
    return {
        "model": model,
        "endpoint": endpoint.rstrip("/") if endpoint is not None else None,
        "checked_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def made_for(client: Any, model: str) -> dict[str, Any]:
    """What a verdict made now, by asking *model* through *client*, was made for."""
    return made_at(endpoint_of(client), model)


def summed_up(reflects: dict[str, bool | None]) -> bool | None:
    """One word for all three settings: ``True`` when every one is reflected,
    ``False`` when none is, ``None`` otherwise."""
    values = {reflects.get(name) for name in SETTINGS}
    return True if values == {True} else False if values == {False} else None


def _place(url: str) -> tuple[str, str, int | None, str] | str:
    """*url* in the form that two spellings of one API root share: the scheme
    and the host in lower case, the port a scheme has by itself filled in, no
    slash at the end."""
    text = url.strip()
    try:
        parts = urlsplit(text)
        port = parts.port or {"http": 80, "https": 443}.get(parts.scheme.lower())
    except ValueError:
        return text.rstrip("/")
    return parts.scheme.lower(), (parts.hostname or "").lower(), port, parts.path.rstrip("/")


def _a_time(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return datetime.fromisoformat(value).tzinfo is not None
    except ValueError:
        return False


def _said(value: Any) -> str:
    """*value* for a refusal's sentence, short."""
    try:
        return json.dumps(value, sort_keys=True)[:300]
    except (TypeError, ValueError):
        return repr(value)[:300]


def _says_what_for(verdict: dict[str, Any], names: tuple[str, ...], what: str, again: str) -> None:
    """Refuse *verdict* where it does not say what it was made for."""
    missing = [name for name in names if verdict.get(name) is None]
    if missing:
        raise SetupError(
            f"the given {what} does not say what it was made for ({', '.join(missing)} "
            f"missing), so nothing shows that it is this run's: {again}"
        )
    for name in ("model", "endpoint"):
        if not isinstance(verdict[name], str):
            raise SetupError(f"the given {what} names no {name} ({name} is no text): {again}")
    if not _a_time(verdict["checked_at"]):
        raise SetupError(
            f"the given {what} does not say when it was made (checked_at is no time in "
            f"ISO 8601 with its offset): {again}"
        )


def _made_here(verdict: dict[str, Any], what: str, again: str, model: str, endpoint: str) -> None:
    """Refuse *verdict* where it was made for another model or at another
    endpoint than this run's."""
    if verdict["model"] != model:
        raise SetupError(
            f"the given {what} was made for the model {verdict['model']!r}, and this run asks "
            f"{model!r}: {again}"
        )
    if _place(verdict["endpoint"]) != _place(endpoint):
        raise SetupError(
            f"the given {what} was made at {verdict['endpoint']}, and this run's requests go "
            f"to {endpoint}: {again}"
        )


def probe_verdict(given: Any, *, model: str, endpoint: str, route: str) -> dict[str, Any]:
    """The verdict of a probe that a run was *given*, as the run's record keeps
    it, for a run that asks *model* at *endpoint* on *route*.

    Raises :class:`SetupError` where it is no verdict of a probe, was probed on
    the other route, does not say what it was made for, or was made for another
    model or endpoint.  The verdict comes back as it was given; a check for
    prompts as token IDs printed into the same object is left out, because a
    run keeps that under a key of its own.
    """
    again = "probe again"
    reflects = given.get("reflects") if isinstance(given, dict) else None
    if (
        not isinstance(reflects, dict)
        or set(reflects) != set(SETTINGS)
        or any(v is not None and not isinstance(v, bool) for v in reflects.values())
    ):
        raise SetupError(
            "the given verdict of the probe is not what a probe returns: an object whose "
            f"reflects says true, false or null for each of {', '.join(SETTINGS)}, with the "
            "model, the endpoint and the route it was made for"
        )
    probed_on = given.get("route")
    if probed_on is not None and probed_on not in ROUTES:
        raise SetupError(
            f"the given verdict of the probe names a route there is none of "
            f"({_said(probed_on)}): {again}"
        )
    if probed_on is not None and probed_on != route:
        raise SetupError(
            f"the given verdict of the probe was probed on the {probed_on} route, and this "
            f"run's requests go to the {route} route: probe again "
            f"{'with' if route == 'completions' else 'without'} prompts as token IDs"
        )
    _says_what_for(given, _PROBE_NAMES, "verdict of the probe", again)
    _made_here(given, "verdict of the probe", again, model, endpoint)
    if "sampler_logprobs" in given and given["sampler_logprobs"] is not summed_up(reflects):
        raise SetupError(
            "the given verdict of the probe contradicts itself: sampler_logprobs is "
            f"{_said(given['sampler_logprobs'])} and reflects says {_said(reflects)}: {again}"
        )
    kept = {key: value for key, value in given.items() if key != "token_id_route"}
    kept.setdefault("sampler_logprobs", summed_up(reflects))
    return kept


def check_verdict(
    given: Any, *, model: str, endpoint: str, fmt: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    """The verdict of a check for prompts as token IDs that a run was *given*,
    for a run that asks *model* at *endpoint*, reads replies in the format
    *fmt* and renders with the template arguments *arguments*.

    Raises :class:`SetupError` where it is no verdict of a check, does not say
    what it was made for, was made for another model, endpoint, format or set
    of template arguments, or says that the server cannot.  The verdict comes
    back as it was given.
    """
    again = "check the server again for this run"
    if not isinstance(given, dict) or not isinstance(given.get("ok"), bool):
        raise SetupError(
            "the given check of the server is not what a check returns: an object that says "
            "whether the server can be sent prompts as token IDs (ok), what the check found "
            "and what it was made for"
        )
    _says_what_for(given, _CHECK_NAMES, "check of the server", again)
    if given["format"] != fmt:
        raise SetupError(
            f"the given check of the server is for the reply format {_said(given['format'])}, "
            f"the run reads {fmt!r}: {again}"
        )
    _made_here(given, "check of the server", again, model, endpoint)
    if given["template_arguments"] != arguments:
        # What a template writes at the end of a turn and at the start of a
        # reply can depend on its arguments.
        raise SetupError(
            "the given check of the server was made with the template arguments "
            f"{_said(given['template_arguments'])}, and the run's are {_said(arguments)}: "
            f"{again}"
        )
    if not given["ok"]:
        raise SetupError(f"prompts cannot be sent as token IDs: {given.get('reason')}")
    return dict(given)
