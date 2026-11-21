"""Minute-based cron; missed minutes are never replayed."""

import threading
from _thread import LockType
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from repoharbor.errors import HarborError


class Cron:
    def __init__(self, expression: str):
        if not isinstance(expression, str) or len(expression.split()) != 5:
            raise HarborError("Cron requires five fields")
        self.raw = expression.split()
        self.fields: list[set[int]] = []
        try:
            for raw, (lo, hi) in zip(
                self.raw, [(0, 59), (0, 23), (1, 31), (1, 12), (0, 7)], strict=True
            ):
                values: set[int] = set()
                for item in raw.split(","):
                    parts = item.split("/")
                    base = parts[0]
                    step = int(parts[1]) if len(parts) == 2 else 1
                    if len(parts) > 2 or step <= 0:
                        raise ValueError
                    if base == "*":
                        start, end = lo, hi
                    elif "-" in base:
                        a, b = base.split("-")
                        start, end = int(a), int(b)
                    else:
                        start = int(base)
                        end = hi if len(parts) == 2 else start
                    if not lo <= start <= end <= hi:
                        raise ValueError
                    values.update(range(start, end + 1, step))
                self.fields.append(values)
        except ValueError:
            raise HarborError("Invalid cron field or range") from None

    def matches(self, local: datetime) -> bool:
        minute, hour, *_ = self.fields
        return (
            local.minute in minute and local.hour in hour and self.matches_date(local)
        )

    def matches_date(self, local: datetime) -> bool:
        _, _, day, month, weekday = self.fields
        dom = local.day in day
        dow = (local.weekday() + 1) % 7 in {v % 7 for v in weekday}
        # Vixie cron uses AND if either day field starts with a wildcard.
        wildcard_day = self.raw[2].startswith("*") or self.raw[4].startswith("*")
        return local.month in month and (
            (dom and dow) if wildcard_day else (dom or dow)
        )

    def require_possible(self) -> None:
        # A Gregorian cycle includes every combination of month, day and weekday.
        day = datetime(2000, 1, 1)
        for _ in range(146097):
            if self.matches_date(day):
                return
            day += timedelta(days=1)
        raise HarborError("Cron has no possible calendar date")

    def next_after(self, now: datetime, zone: ZoneInfo) -> datetime | None:
        day = now.astimezone(zone).replace(hour=0, minute=0, second=0, microsecond=0)
        minutes, hours, *_ = self.fields
        times = [(hour, minute) for hour in sorted(hours) for minute in sorted(minutes)]
        # A Gregorian cycle covers every possible combination of calendar fields.
        for _ in range(146097):
            if self.matches_date(day):
                for hour, minute in times:
                    local = day.replace(hour=hour, minute=minute, fold=0)
                    instant = local.astimezone(UTC)
                    if instant > now and instant.astimezone(zone) == local:
                        return local
            day += timedelta(days=1)
        return None


class Scheduler:
    def __init__(
        self,
        cron: Cron,
        timezone: str,
        initial: datetime,
        on_skip: Callable[[int], None] | None = None,
        *,
        busy: LockType | None = None,
    ):
        self.cron, self.zone = cron, ZoneInfo(timezone)
        self.last_minute = int(initial.timestamp() // 60)
        self.last_local: tuple[int, ...] | None = None
        self.busy = busy if busy is not None else threading.Lock()
        self.skipped = 0
        self.on_skip = on_skip
        self._ticks = threading.Lock()

    def tick(
        self,
        now: datetime,
        run: Callable[[], object],
        dispatch: Callable[[Callable[[], None]], None] | None = None,
    ) -> bool:
        with self._ticks:
            minute = int(now.timestamp() // 60)
            if minute <= self.last_minute:
                return False
            self.last_minute = minute
            local = now.astimezone(self.zone)
            key = (local.year, local.month, local.day, local.hour, local.minute)
            # fold=1 is the repeated wall-clock minute: run it at most once.
            if local.fold or key == self.last_local or not self.cron.matches(local):
                return False
            self.last_local = key
            if not self.busy.acquire(blocking=False):
                if self.on_skip is not None:
                    self.on_skip(1)
                self.skipped += 1
                return False

        def execute() -> None:
            try:
                run()
            finally:
                self.busy.release()

        if dispatch is None:
            execute()
        else:
            try:
                dispatch(execute)
            except BaseException:
                self.busy.release()
                raise
        return True
