from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from repoharbor.errors import HarborError
from repoharbor.scheduler import Cron, Scheduler


@pytest.mark.parametrize(
    "expression",
    [
        "* * * *",
        "60 * * * *",
        "*/0 * * * *",
        "*/1/2 * * * *",
        "1-0 * * * *",
        "a * * * *",
        "* * 0 * *",
        "* * * 13 *",
        "* * * * 8",
        "1, * * * *",
    ],
)
def test_bad_cron(expression):
    with pytest.raises(HarborError):
        Cron(expression)


def test_cron_ranges_lists_steps_and_dates():
    assert Cron("*/6 1,3 1-29 2 0,7").matches(datetime(2024, 2, 29, 3, 12))
    assert not Cron("0 3 29 2 *").matches(datetime(2025, 2, 28, 3))
    assert Cron("0 3 1 * 1").matches(datetime(2026, 11, 2, 3))  # Monday, not 1st
    assert Cron("0 3 1 * 1").matches(datetime(2026, 11, 1, 3))  # 1st, not Monday
    assert not Cron("0 3 1 * 1").matches(datetime(2026, 11, 3, 3))
    assert Cron("0 3 * * 7").matches(datetime(2026, 11, 1, 3))
    assert Cron("5/10 * * * *").matches(datetime(2026, 1, 1, 1, 15))


def test_no_backfill_restart_overlap_and_dst():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    scheduler = Scheduler(Cron("* * * * *"), "UTC", now)
    calls = []
    assert not scheduler.tick(now, lambda: calls.append(1))
    assert scheduler.tick(datetime(2026, 1, 2, tzinfo=UTC), lambda: calls.append(1))
    assert calls == [1]  # one current tick, no replay of the previous day
    scheduler.busy.acquire()
    assert not scheduler.tick(
        datetime(2026, 1, 2, 0, 1, tzinfo=UTC), lambda: calls.append(1)
    )
    scheduler.busy.release()
    assert scheduler.skipped == 1 and calls == [1]
    assert scheduler.tick(datetime(2026, 1, 2, 0, 2, tzinfo=UTC), lambda: None)
    berlin = ZoneInfo("Europe/Berlin")
    scheduler = Scheduler(Cron("30 2 * * *"), "Europe/Berlin", now)
    assert scheduler.tick(
        datetime(2026, 10, 25, 2, 30, tzinfo=berlin), lambda: calls.append(1)
    )
    assert not scheduler.tick(
        datetime(2026, 10, 25, 2, 30, tzinfo=berlin, fold=1), lambda: calls.append(1)
    )
    scheduler = Scheduler(Cron("30 2 * * *"), "Europe/Berlin", now)
    assert not scheduler.tick(
        datetime(2026, 3, 29, 1, 30, tzinfo=UTC), lambda: calls.append(1)
    )


def test_lock_released_on_failure():
    scheduler = Scheduler(Cron("* * * * *"), "UTC", datetime(2026, 1, 1, tzinfo=UTC))

    def fail():
        raise RuntimeError

    with pytest.raises(RuntimeError):
        scheduler.tick(datetime(2026, 1, 1, 0, 1, tzinfo=UTC), fail)
    assert not scheduler.busy.locked()


@pytest.mark.parametrize(
    "expression,days",
    [
        ("0 3 */1 * 1", {2, 9, 16, 23, 30}),
        ("0 3 * * 1", {2, 9, 16, 23, 30}),
        ("0 3 1 * */1", {1}),
        ("0 3 1 * *", {1}),
        ("0 3 */2 * 1", {9, 23}),
        ("0 3 1 * */2", {1}),
        ("0 3 */2 * */2", {1, 3, 5, 7, 15, 17, 19, 21, 29}),
        ("0 3 1,15 * 1-5/2", {1, 2, 4, 6, 9, 11, 13, 15, 16, 18, 20, 23, 25, 27, 30}),
        ("0 3 */1,15 * 1", {2, 9, 16, 23, 30}),
        ("0 3 1,* * 1", set(range(1, 31))),
        ("0 3 1 * 1,*", set(range(1, 31))),
        ("0 3 1/1 * 1", set(range(1, 31))),
        ("0 3 *,15 * 1", {2, 9, 16, 23, 30}),
        ("0 3 1 * *,3", {1}),
        ("0 3 1-31 * 1", set(range(1, 31))),
        ("0 3 1 * 0-7", set(range(1, 31))),
    ],
)
def test_cron_day_wildcards_steps_and_combinations(expression, days):
    cron = Cron(expression)
    assert {
        day for day in range(1, 31) if cron.matches(datetime(2026, 11, day, 3))
    } == days
    assert not cron.matches(datetime(2026, 11, 1, 4))
    assert not cron.matches(datetime(2026, 11, 1, 3, 1))


@pytest.mark.parametrize(
    "date,expected",
    [
        (datetime(2026, 11, 9, 4, 30), True),
        (datetime(2026, 11, 11, 5, 45), True),
        (datetime(2026, 11, 2, 4, 30), False),
        (datetime(2026, 11, 3, 4, 30), False),
        (datetime(2026, 11, 9, 6, 30), False),
        (datetime(2026, 11, 9, 4, 31), False),
        (datetime(2026, 12, 9, 4, 30), False),
    ],
)
def test_cron_day_steps_respect_other_fields(date, expected):
    assert Cron("*/15 3-5 */2 11 1,3").matches(date) is expected
