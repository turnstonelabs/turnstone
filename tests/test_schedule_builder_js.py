"""Behavioral tests for the schedule "When" builder's pure helpers
(``turnstone/console/static/schedule_builder.js``): compile, describe,
reverse-parse and the time formatters, driven through ``node``.

Both the admin schedule shelf and the dashboard launcher's Scheduled kind
compile their timing through these, so the arithmetic is pinned once here.
The DOM instance (template cloning, event wiring, preview fetches) is
browser-verified only: the node harness has no template / cloneNode.  The
id transform the instance scoping rests on is a pure function (``scopedId``)
and is pinned here; ``test_shell_js.py`` pins that the constructor routes
every id / for / aria-labelledby through it.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests._js_harness_helpers import node_skip

_ROOT = Path(__file__).resolve().parent.parent
_BUILDER_JS = _ROOT / "turnstone/console/static/schedule_builder.js"

pytestmark = node_skip

_HARNESS = """
const vm = require('vm');
global.window = global;
global.document = { getElementById: () => null, createElement: () => ({}) };
vm.runInThisContext(%(src)s);
const SB = global.TurnstoneScheduleBuilder;
const out = (%(expr)s);
process.stdout.write(JSON.stringify(out === undefined ? null : out));
"""


def _eval(expr: str, tz: str = "UTC") -> Any:
    """Evaluate ``expr`` with the builder loaded as ``SB`` under the ``tz``
    local time (UTC by default so the datetime-local conversions are
    deterministic; a named zone for the tests that prove the conversion)."""
    harness = _HARNESS % {
        "src": json.dumps(_BUILDER_JS.read_text(encoding="utf-8")),
        "expr": expr,
    }
    result = subprocess.run(
        ["node", "-e", harness],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
        env={**os.environ, "TZ": tz},
    )
    return json.loads(result.stdout)


def _compile(**state: Any) -> dict[str, Any]:
    return _eval(f"SB.compileSchedule(Object.assign(SB.defaultState(), {json.dumps(state)}))")


def _describe(tz: str = "UTC", **state: Any) -> str:
    return _eval(f"SB.describeSchedule(Object.assign(SB.defaultState(), {json.dumps(state)}))", tz)


# ---------------------------------------------------------------------------
# compileSchedule — builder state -> wire fields
# ---------------------------------------------------------------------------


def test_default_state_compiles_to_daily_0600() -> None:
    assert _compile() == {"schedule_type": "cron", "cron_expr": "0 6 * * *", "at_time": ""}


def test_daily_uses_the_chosen_time() -> None:
    assert _compile(mode="daily", dailyTime="09:30")["cron_expr"] == "30 9 * * *"


def test_daily_blank_time_falls_back_to_0600() -> None:
    """A cleared <input type=time> reads as the default, never as NaN fields."""
    assert _compile(mode="daily", dailyTime="")["cron_expr"] == "0 6 * * *"


def test_weekly_sorts_days_numerically() -> None:
    out = _compile(mode="weekly", weeklyTime="07:15", weeklyDays=[5, 1, 3])
    assert out["cron_expr"] == "15 7 * * 1,3,5"


def test_weekly_requires_a_day() -> None:
    assert _compile(mode="weekly", weeklyDays=[]) == {"error": "Select at least one day"}


def test_monthly_day_and_time() -> None:
    assert _compile(mode="monthly", monthlyDom=15, monthlyTime="07:00")["cron_expr"] == (
        "0 7 15 * *"
    )


@pytest.mark.parametrize("dom", [0, 32, "", "abc", "15.5"])
def test_monthly_rejects_an_impossible_day(dom: Any) -> None:
    assert _compile(mode="monthly", monthlyDom=dom) == {"error": "Day of month must be 1-31"}


def test_interval_hours_and_minutes() -> None:
    assert _compile(mode="interval", intervalEvery=4, intervalUnit="hours")["cron_expr"] == (
        "0 */4 * * *"
    )
    assert _compile(mode="interval", intervalEvery="15", intervalUnit="minutes")["cron_expr"] == (
        "*/15 * * * *"
    )


@pytest.mark.parametrize("every", [0, -1, "", "x", "4.5", "1e1.5"])
def test_interval_rejects_less_than_one_or_a_fraction(every: Any) -> None:
    """A number input reports "4.5" as its value even though its step is 1;
    the builder refuses it rather than truncating to a cadence the read-out
    never named."""
    assert _compile(mode="interval", intervalEvery=every) == {
        "error": "Interval must be a whole number, at least 1"
    }


def test_interval_accepts_an_exponent_form_as_the_whole_number_it_is() -> None:
    assert _compile(mode="interval", intervalEvery="1e1", intervalUnit="hours")["cron_expr"] == (
        "0 */10 * * *"
    )
    assert _describe(mode="interval", intervalEvery="1e1", intervalUnit="hours") == (
        "every 10 hours, restarting at midnight"
    )


def test_interval_step_is_bounded_by_the_cron_field() -> None:
    """ "*/90" in the minute field matches minute 0 only (hourly) and "0 */30"
    in the hour field matches hour 0 only (daily); croniter accepts both, so
    the builder refuses them instead of storing a schedule that fires on a
    different cadence than the one described."""
    assert _compile(mode="interval", intervalEvery=59, intervalUnit="minutes")["cron_expr"] == (
        "*/59 * * * *"
    )
    assert _compile(mode="interval", intervalEvery=60, intervalUnit="minutes") == {
        "error": "Minutes interval must be 1-59"
    }
    assert _compile(mode="interval", intervalEvery=90, intervalUnit="minutes") == {
        "error": "Minutes interval must be 1-59"
    }
    assert _compile(mode="interval", intervalEvery=23, intervalUnit="hours")["cron_expr"] == (
        "0 */23 * * *"
    )
    for every in (24, 30):
        assert _compile(mode="interval", intervalEvery=every, intervalUnit="hours") == {
            "error": "Hours interval must be 1-23 (Daily runs once a day)"
        }


def test_once_compiles_local_time_to_an_offset_bearing_at_time() -> None:
    out = _compile(mode="once", atLocal="2026-09-08T10:00")
    assert out == {"schedule_type": "at", "cron_expr": "", "at_time": "2026-09-08T10:00:00+00:00"}


@pytest.mark.parametrize("at", ["", "not-a-date"])
def test_once_requires_a_parseable_date(at: str) -> None:
    assert _compile(mode="once", atLocal=at) == {"error": "Pick a date and time"}


def test_cron_mode_trims_the_raw_expression() -> None:
    assert _compile(mode="cron", cron="  0 9 * * MON-FRI ")["cron_expr"] == "0 9 * * MON-FRI"


def test_cron_mode_requires_an_expression() -> None:
    assert _compile(mode="cron", cron="   ") == {"error": "Cron expression is required"}


def test_unknown_mode_is_an_error_not_an_interval() -> None:
    assert _compile(mode="hourly") == {"error": "Unknown schedule mode"}


# ---------------------------------------------------------------------------
# describeSchedule — the read-out header
# ---------------------------------------------------------------------------


def test_descriptions() -> None:
    assert _describe() == "every day at 06:00 UTC"
    assert _describe(mode="daily", dailyTime="9:5") == "every day at 09:05 UTC"
    assert _describe(mode="weekly", weeklyDays=[5, 1]) == "every Mon, Fri at 06:00 UTC"
    assert _describe(mode="weekly", weeklyDays=[]) == "no days selected"
    assert _describe(mode="monthly", monthlyDom=12) == "monthly on day 12 at 06:00 UTC"
    assert _describe(mode="interval") == "every 4 hours"
    assert _describe(mode="once") == "one time"


def test_interval_description_says_how_an_uneven_step_restarts() -> None:
    """A step that divides its field is a true cadence; one that does not
    restarts at the field boundary ("*/7" hours fires 00, 07, 14, 21, then
    00) and the read-out says so.  A blank or out-of-range step (which
    compile refuses) gets the plain phrase, never a claim."""
    even = [(15, "minutes"), (30, "minutes"), (1, "minutes"), (4, "hours"), (12, "hours")]
    for n, unit in even:
        assert _describe(mode="interval", intervalEvery=n, intervalUnit=unit) == (
            f"every {n} {unit}"
        )
    for n, unit in [(7, "minutes"), (45, "minutes")]:
        assert _describe(mode="interval", intervalEvery=n, intervalUnit=unit) == (
            f"every {n} {unit}, restarting each hour"
        )
    for n, unit in [(5, "hours"), (7, "hours")]:
        assert _describe(mode="interval", intervalEvery=n, intervalUnit=unit) == (
            f"every {n} {unit}, restarting at midnight"
        )
    assert _describe(mode="interval", intervalEvery="", intervalUnit="hours") == "every ? hours"
    # The read-out names the step compile will use, never the raw text.
    assert _describe(mode="interval", intervalEvery="4.5", intervalUnit="hours") == "every ? hours"
    assert _describe(mode="monthly", monthlyDom="15.5") == "monthly on day ? at 06:00 UTC"
    assert _describe(mode="interval", intervalEvery=90, intervalUnit="minutes") == (
        "every 90 minutes"
    )
    assert _describe(mode="once", atLocal="2026-09-08T10:00") == (
        "once at Tue 2026-09-08 10:00 UTC"
    )
    assert _describe(mode="once", atLocal="2026-09-08T10:00", tz="America/New_York") == (
        "once at Tue 2026-09-08 10:00 EDT"
    )
    assert _describe(mode="cron") == "custom cron"


# ---------------------------------------------------------------------------
# cronToScheduleMode / stateFromSaved — edit mode reverse-parse
# ---------------------------------------------------------------------------


def test_reverse_parse_recognises_every_builder_shape() -> None:
    parse = "SB.cronToScheduleMode(%s)"
    assert _eval(parse % '"30 9 * * *"') == {"mode": "daily", "h": 9, "min": 30}
    assert _eval(parse % '"0 6 * * 1,5"') == {"mode": "weekly", "h": 6, "min": 0, "days": [1, 5]}
    assert _eval(parse % '"0 7 15 * *"') == {"mode": "monthly", "h": 7, "min": 0, "dom": 15}
    assert _eval(parse % '"0 */4 * * *"') == {"mode": "interval", "every": 4, "unit": "hours"}
    assert _eval(parse % '"*/15 * * * *"') == {"mode": "interval", "every": 15, "unit": "minutes"}


def test_reverse_parse_maps_cron_sunday_7_to_0() -> None:
    assert _eval('SB.cronToScheduleMode("0 6 * * 7")')["days"] == [0]


def test_reverse_parse_falls_back_to_cron_mode() -> None:
    for expr in ("0 9 * * MON-FRI", "*/5 8-18 * * *", "", "garbage"):
        assert _eval(f"SB.cronToScheduleMode({json.dumps(expr)})") == {"mode": "cron"}


@pytest.mark.parametrize(
    "expr",
    [
        "30 9 * * *",
        "0 6 * * 1,5",
        "0 7 15 * *",
        "0 */4 * * *",
        "*/15 * * * *",
        # The bound's own edges, still inside: open in Interval, round-trip.
        "0 */23 * * *",
        "*/59 * * * *",
    ],
)
def test_builder_shapes_round_trip_through_edit(expr: str) -> None:
    """Open a saved builder-made schedule for edit, save unchanged: same cron,
    and the raw expression is carried so Cron mode never shows an empty box."""
    state = _eval(f'SB.stateFromSaved("cron", {json.dumps(expr)}, "")')
    assert state["mode"] != "cron" and state["cron"] == expr
    out = _eval(f'SB.compileSchedule(SB.stateFromSaved("cron", {json.dumps(expr)}, ""))')
    assert out["cron_expr"] == expr


@pytest.mark.parametrize("expr", ["*/60 * * * *", "*/90 * * * *", "0 */24 * * *", "0 */30 * * *"])
def test_saved_out_of_range_step_opens_in_cron_mode_unchanged(expr: str) -> None:
    """croniter accepts these, so they can be stored (by the Cron escape
    hatch or an API client) and must stay editable: the reverse-parse falls
    through to Cron mode with the expression intact, and saving unchanged
    keeps it — the builder refuses to CREATE such a step, never to keep one."""
    state = _eval(f'SB.stateFromSaved("cron", {json.dumps(expr)}, "")')
    assert state["mode"] == "cron" and state["cron"] == expr
    out = _eval(f'SB.compileSchedule(SB.stateFromSaved("cron", {json.dumps(expr)}, ""))')
    assert out == {"schedule_type": "cron", "cron_expr": expr, "at_time": ""}


def test_unrecognised_cron_opens_in_cron_mode_with_the_raw_text() -> None:
    state = _eval('SB.stateFromSaved("cron", "0 9 * * MON-FRI", "")')
    assert state["mode"] == "cron" and state["cron"] == "0 9 * * MON-FRI"
    assert _eval('SB.compileSchedule(SB.stateFromSaved("cron", "0 9 * * MON-FRI", ""))') == {
        "schedule_type": "cron",
        "cron_expr": "0 9 * * MON-FRI",
        "at_time": "",
    }


def test_saved_at_time_round_trips() -> None:
    state = _eval('SB.stateFromSaved("at", "", "2026-09-08T10:00:00+00:00")')
    assert state["mode"] == "once" and state["atLocal"] == "2026-09-08T10:00"
    assert state["cron"] == "", "a one-shot carries no cron"
    out = _eval('SB.compileSchedule(SB.stateFromSaved("at", "", "2026-09-08T10:00:00+00:00"))')
    assert out["at_time"] == "2026-09-08T10:00:00+00:00"


# ---------------------------------------------------------------------------
# Time formatters
# ---------------------------------------------------------------------------


def test_format_local_reads_a_suffix_less_server_time_as_utc() -> None:
    """next_run comes back "YYYY-MM-DDTHH:MM:SS" with no offset: it is UTC,
    never browser-local, and the read-out shows it in the operator's zone."""
    assert _eval('SB.formatLocal("2026-09-08T06:00:00")') == "Tue 2026-09-08 06:00 UTC"
    assert _eval('SB.formatLocal("2026-09-08T06:00:00+00:00")') == "Tue 2026-09-08 06:00 UTC"
    ny = "America/New_York"  # UTC-4 in September
    assert _eval('SB.formatLocal("2026-09-08T06:00:00")', ny) == "Tue 2026-09-08 02:00 EDT"
    assert _eval('SB.formatLocal("2026-09-08T06:00:00+00:00")', ny) == "Tue 2026-09-08 02:00 EDT"


def test_format_local_crosses_the_day_line() -> None:
    assert _eval('SB.formatLocal("2026-09-08T02:00:00")', "America/New_York") == (
        "Mon 2026-09-07 22:00 EDT"
    )


def test_format_local_converts_a_negative_offset() -> None:
    assert _eval('SB.formatLocal("2026-09-08T06:00:00-05:00")') == "Tue 2026-09-08 11:00 UTC"


def test_format_local_echoes_the_unparseable() -> None:
    assert _eval('SB.formatLocal("soon")') == "soon"


def test_runs_message_kind() -> None:
    """An unmet compile is a hint until the mode is edited or a submit is
    refused on it; a clean compile has no message."""
    kind = "SB.runsMessageKind(%s, %s)"
    assert _eval(kind % ('{error: "Select at least one day"}', "false")) == "hint"
    assert _eval(kind % ('{error: "Select at least one day"}', "true")) == "err"
    assert _eval(kind % ('{schedule_type: "cron", cron_expr: "0 6 * * *"}', "false")) is None
    assert _eval(kind % ('{schedule_type: "cron", cron_expr: "0 6 * * *"}', "true")) is None


def test_relative_to_now_buckets() -> None:
    now = "Date.parse('2026-09-08T06:00:00Z')"
    rel = "SB.relativeToNow(%s, %s)"
    assert _eval(rel % ('"2026-09-11T06:00:00"', now)) == "in 3d"
    assert _eval(rel % ('"2026-09-08T07:30:00"', now)) == "in 2h"
    assert _eval(rel % ('"2026-09-08T06:30:00"', now)) == "in 30m"
    assert _eval(rel % ('"2026-09-08T05:00:00"', now)) == "now"
    assert _eval(rel % ('"soon"', now)) == ""


def test_utc_to_local_datetime() -> None:
    assert _eval('SB.utcToLocalDatetime("2026-09-08T06:00:00+00:00")') == "2026-09-08T06:00"
    assert _eval('SB.utcToLocalDatetime("")') == ""
    assert _eval('SB.utcToLocalDatetime("not a time")') == "not a time"


def test_utc_to_local_datetime_reads_a_suffix_less_server_time_as_utc() -> None:
    """The admin list's NEXT RUN cell: next_run arrives with no offset and is
    UTC, so a browser four hours behind sees 02:00, not 06:00 relabelled."""
    tz = "America/New_York"  # UTC-4 in September
    assert _eval('SB.utcToLocalDatetime("2026-09-08T06:00:00")', tz) == "2026-09-08T02:00"
    assert _eval('SB.utcToLocalDatetime("2026-09-08T06:00:00+00:00")', tz) == ("2026-09-08T02:00")


def test_once_round_trips_across_a_non_utc_browser() -> None:
    """Edit a saved one-shot in New York: the picker shows local time and
    compiles back to the same UTC instant."""
    tz = "America/New_York"
    state = _eval('SB.stateFromSaved("at", "", "2026-09-08T10:00:00+00:00")', tz)
    assert state["atLocal"] == "2026-09-08T06:00"
    out = _eval('SB.compileSchedule(SB.stateFromSaved("at", "", "2026-09-08T10:00:00+00:00"))', tz)
    assert out["at_time"] == "2026-09-08T10:00:00+00:00"


# ---------------------------------------------------------------------------
# scopedId — the instance-scoping transform every cloned id goes through
# ---------------------------------------------------------------------------


def test_scoped_id_replaces_the_template_prefix_only() -> None:
    assert _eval('SB.scopedId("when-seg", "when2-")') == "when2-seg"
    assert _eval('SB.scopedId("when-time-daily", "when1-")') == "when1-time-daily"
    # Outside the template vocabulary: untouched (no double prefix, no mangling).
    assert _eval('SB.scopedId("sch-name", "when1-")') == "sch-name"
    assert _eval('SB.scopedId("when1-seg", "when2-")') == "when1-seg"
