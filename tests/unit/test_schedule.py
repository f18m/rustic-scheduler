from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from rustic_scheduler.durations import format_duration, parse_duration
from rustic_scheduler.schedule import Schedule, parse_day, parse_time, resolve_local

ROME = ZoneInfo("Europe/Rome")


def sched(days, times, tz=ROME) -> Schedule:
    return Schedule(frozenset(days), tuple(time.fromisoformat(t) for t in times), tz)


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=UTC)


def test_next_run_uses_the_named_timezone():
    daily = sched(range(7), ["02:30"])
    # 2026-01-15 is winter: Rome is UTC+1
    assert daily.next_after(utc(2026, 1, 15, 0, 0)) == utc(2026, 1, 15, 1, 30)
    # 2026-07-15 is summer: Rome is UTC+2
    assert daily.next_after(utc(2026, 7, 15, 0, 0)) == utc(2026, 7, 15, 0, 30)


def test_next_run_is_strictly_after():
    daily = sched(range(7), ["02:30"])
    at = utc(2026, 1, 15, 1, 30)
    assert daily.next_after(at) == utc(2026, 1, 16, 1, 30)
    assert daily.previous(at) == at


def test_only_selected_weekdays_fire():
    # 2026-11-09 is a Monday
    mon_fri = sched([0, 4], ["12:00"], ZoneInfo("UTC"))
    found = mon_fri.between(utc(2026, 11, 8), utc(2026, 11, 22))
    assert [d.strftime("%a %d") for d in found] == ["Mon 09", "Fri 13", "Mon 16", "Fri 20"]


def test_weekday_is_judged_in_local_time():
    # 00:30 Monday in Rome is still Sunday in UTC
    monday = sched([0], ["00:30"])
    assert monday.next_after(utc(2026, 11, 8, 12)) == utc(2026, 11, 8, 23, 30)


def test_nonexistent_local_time_fires_once_after_the_gap():
    # Rome skips 02:00-03:00 on 2026-03-29
    daily = sched(range(7), ["02:30"])
    fired = daily.between(utc(2026, 3, 28, 12), utc(2026, 3, 29, 12))
    assert fired == [utc(2026, 3, 29, 1, 30)]  # 03:30 CEST
    assert fired[0].astimezone(ROME).strftime("%H:%M") == "03:30"


def test_gap_does_not_duplicate_a_run_that_collides():
    daily = sched(range(7), ["02:30", "03:30"])
    fired = daily.between(utc(2026, 3, 28, 12), utc(2026, 3, 29, 12))
    assert fired == [utc(2026, 3, 29, 1, 30)]


def test_ambiguous_local_time_fires_once_at_first_occurrence():
    # Rome repeats 02:00-03:00 on 2026-10-25
    daily = sched(range(7), ["02:30"])
    fired = daily.between(utc(2026, 10, 24, 12), utc(2026, 10, 25, 12))
    assert fired == [utc(2026, 10, 25, 0, 30)]  # the CEST (first) 02:30
    assert resolve_local(fired[0].astimezone(ROME).date(), time(2, 30), ROME) == fired[0]


def test_runs_stay_24h_apart_in_wall_clock_across_dst():
    daily = sched(range(7), ["12:00"])
    before = daily.next_after(utc(2026, 3, 28, 0))
    after = daily.next_after(before)
    assert after - before == timedelta(hours=23)


def test_between_is_capped_and_ordered():
    hourly = sched(range(7), [f"{h:02d}:00" for h in range(24)], ZoneInfo("UTC"))
    found = hourly.between(utc(2026, 1, 1), utc(2026, 3, 1), limit=10)
    assert len(found) == 10 and found == sorted(found)


@pytest.mark.parametrize("bad", ["25:00", "2:30", "02.30", "0230", "12:60", ""])
def test_bad_times_are_rejected(bad):
    with pytest.raises(ValueError, match="time"):
        parse_time(bad)


def test_day_names():
    assert parse_day("Mon") == 0 and parse_day("sunday") == 6
    with pytest.raises(ValueError, match="unknown day"):
        parse_day("funday")


@pytest.mark.parametrize(
    ("text", "seconds"),
    [
        ("30s", 30),
        ("15m", 900),
        ("12h", 43200),
        ("7d", 604800),
        ("1h30m", 5400),
        (90, 90),
        ("45", 45),
    ],
)
def test_durations(text, seconds):
    assert parse_duration(text).total_seconds() == seconds


@pytest.mark.parametrize("bad", ["", "abc", "10x", "-5", -1, True, None, "1h 30"])
def test_bad_durations(bad):
    with pytest.raises(ValueError):
        parse_duration(bad)


def test_format_duration():
    assert format_duration(3723) == "1h 02m 03s"
    assert format_duration(59) == "59s"
