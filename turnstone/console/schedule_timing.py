"""Schedule timing: the zone-aware cron walk and what it means.

A recurring schedule's cron is evaluated in the schedule's IANA zone, so a
wall-clock time keeps its meaning across daylight-saving changes; a one-shot
names its own instant.  ``next_run`` is always naive UTC, the shape the due
query compares as a string against the clock.  Used by the console's schedule
endpoints (validation, preview, the next run on create and update) and by the
scheduler daemon's advance after each firing.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import CroniterError, croniter

# The stored shape of a schedule's times: naive UTC to the second, the
# shape the due query compares as a string against the clock.
TS_FMT = "%Y-%m-%dT%H:%M:%S"


def resolve_zone(name: str) -> ZoneInfo | None:
    """The zone *name* names, or None when this host cannot resolve it.

    Every zone lookup goes through here so the request-side check and the
    scheduler's cron walk agree on what counts as unresolvable: a
    placeholder or path-shaped key (ValueError), a key the database lacks
    (ZoneInfoNotFoundError) and an unreadable database file (OSError).
    """
    try:
        return ZoneInfo(name)
    except (ValueError, OSError, ZoneInfoNotFoundError):
        return None


def no_next_run_reason(cron_expr: str, timezone: str) -> str:
    """Why ``next_cron_runs`` found no next firing, for a schedule's run
    history.  Kept beside the walk: its None has exactly these three
    causes, so a fourth belongs here as well."""
    if resolve_zone(timezone) is None:
        return f"time zone {timezone} could not be resolved on this host"
    if not croniter.is_valid(cron_expr):
        return f"the expression {cron_expr!r} is not a valid cron"
    return f"the expression {cron_expr!r} never matches a real calendar date"


_CRON_LITERAL_FIELD = re.compile(r"\d+(?:-\d+)?(?:,\d+(?:-\d+)?)*")

# croniter's shorthand forms in the five-field shape the literal check reads
# (croniter keeps its own table private).
_CRON_ALIASES = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}


def cron_names_a_time(cron_expr: str) -> bool:
    """True when the minute and hour fields are both literal values or
    ranges without a step.

    Such an expression names times of day rather than a cadence, which is
    the distinction the repeated fall-back hour turns on: ``30 1 * * *``
    means once at 01:30 and ``0 1-2 * * *`` twice, ``*/30 * * * *`` means
    every thirty minutes of real time.  croniter expands steps into value
    lists, so the raw fields decide.
    """
    expr = cron_expr.strip()
    fields = _CRON_ALIASES.get(expr.lower(), expr).split()
    return len(fields) >= 2 and all(_CRON_LITERAL_FIELD.fullmatch(f) for f in fields[:2])


def is_second_occurrence(fire: datetime) -> bool:
    """True when *fire* is the later of two instants sharing one wall-clock
    time — the second pass of a repeated fall-back hour.

    A property of the instant alone, so it needs no memory of earlier
    firings: the two readings of the naive local time (``fold`` 0 and 1)
    have different offsets only inside the repeated hour.  Both sides are
    compared as UTC because aware datetimes sharing one tzinfo compare by
    their naive fields, which the two occurrences share.
    """
    naive = fire.replace(tzinfo=None)
    first = naive.replace(tzinfo=fire.tzinfo, fold=0)
    second = naive.replace(tzinfo=fire.tzinfo, fold=1)
    return first.utcoffset() != second.utcoffset() and fire.astimezone(UTC) == second.astimezone(
        UTC
    )


def next_cron_runs(
    cron_expr: str,
    count: int,
    timezone: str = "UTC",
    start: datetime | None = None,
) -> list[str] | None:
    """Next *count* firings of *cron_expr*, evaluated in *timezone*, as
    naive-UTC ISO strings.

    The cron's fields are wall-clock in *timezone* (an IANA name), so
    ``30 2 * * *`` in ``America/New_York`` fires at 02:30 local on either side
    of a daylight-saving change — 07:30 UTC in winter, 06:30 UTC in summer —
    and a weekly day is the local day.  croniter walks zone-aware datetimes:
    a time the spring-forward gap removes fires at the first instant after
    it.  The fall-back hour repeats, and croniter visits a wall-clock time in
    it once per offset; a fixed time of day (``cron_names_a_time``) is
    de-duplicated here so it fires once, while a cadence keeps firing
    through the repeated hour, which is real time.  *start* pins the search
    origin (tests); the clock otherwise.

    Returns None when there is no next firing: an expression that passes
    croniter.is_valid but can never match a real calendar date (``0 0 30 2 *``
    — get_next raises CroniterBadDateError after exhausting its search
    window), a stored expression this croniter no longer parses, or a stored
    zone this host can no longer resolve (a legacy key dropped by a
    zone-database change).  The request paths validate first, so the stored
    cases are the scheduler's: None disables the schedule instead of
    aborting the tick, and ``no_next_run_reason`` says which it was.
    """
    zone = resolve_zone(timezone)
    if zone is None:
        return None
    # A time of day fires once even where the fall-back hour repeats it: the
    # second occurrence of a wall-clock time is skipped.  A cadence is not,
    # since the repeated hour is real time.
    names_a_time = cron_names_a_time(cron_expr)
    runs: list[str] = []
    try:
        cron = croniter(cron_expr, (start or datetime.now(UTC)).astimezone(zone))
        while len(runs) < count:
            fire = cron.get_next(datetime)
            if names_a_time and is_second_occurrence(fire):
                continue
            runs.append(fire.astimezone(UTC).strftime(TS_FMT))
    except CroniterError:
        return None
    return runs


def compute_next_run(
    schedule_type: str, cron_expr: str, at_time: str, timezone: str = "UTC"
) -> str:
    """Compute the next run time for a schedule. Empty string if invalid.

    ``next_run`` is one shape for both types — naive UTC — because the due
    query compares it as a string against a naive-UTC clock.  A one-shot's
    ``at_time`` carries its own offset and is kept as submitted; folding the
    offset in here is what makes a ``+05:30`` fire at the instant it names
    rather than hours late (kept verbatim, it would sort as if it were UTC).
    """
    if schedule_type == "at":
        try:
            at = datetime.fromisoformat(at_time)
        except ValueError:
            return ""
        if at.tzinfo is None:
            return ""
        return at.astimezone(UTC).strftime(TS_FMT)
    if schedule_type == "cron" and cron_expr:
        runs = next_cron_runs(cron_expr, 1, timezone)
        return runs[0] if runs else ""
    return ""
