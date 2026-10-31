"""Clock abstraction and trading-session time derivation."""
from __future__ import annotations

import time as _time
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Callable, Protocol, TypeVar

from .config import ExitConfig, StrategyConfig
from .models import ET


class Clock(Protocol):
    def now(self) -> datetime: ...

    def sleep_until(self, dt: datetime) -> None: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(ET)

    def sleep_until(self, dt: datetime) -> None:
        while True:
            remaining = (dt - self.now()).total_seconds()
            if remaining <= 0:
                return
            _time.sleep(min(1.0, remaining))


T = TypeVar("T")


def retry_until(
    fn: Callable[[], T],
    clock: Clock,
    deadline: datetime,
    every: timedelta,
    on_failure: Callable[[Exception], None],
    fatal: tuple[type[BaseException], ...] = (),
) -> T:
    """Call fn until it succeeds, waiting `every` between failed attempts (the last attempt
    is at `deadline`). Each failure before the deadline goes to on_failure; the failure at
    or after the deadline, and any `fatal` exception, is raised."""
    while True:
        try:
            return fn()
        except fatal:
            raise
        except Exception as exc:
            if clock.now() >= deadline:
                raise
            on_failure(exc)
            clock.sleep_until(min(clock.now() + every, deadline))


STARTUP_GIVE_UP_AT = time(9, 44)
STARTUP_RETRY_EVERY = timedelta(seconds=30)
LATE_START_RETRY_FOR = timedelta(minutes=10)


def startup_deadline(launch: datetime, flat_time: time) -> datetime:
    """Last startup attempt. A normal start retries until 09:44 ET; a start at or after
    09:44 (a crash restart, which must resume managing positions) retries for 10 minutes
    from launch, never past flat time (controller ruling 10)."""
    give_up = datetime.combine(launch.date(), STARTUP_GIVE_UP_AT, tzinfo=launch.tzinfo)
    if launch < give_up:
        return give_up
    return min(launch + LATE_START_RETRY_FOR,
               datetime.combine(launch.date(), flat_time, tzinfo=launch.tzinfo))


@dataclass(frozen=True)
class MarketHours:
    open: datetime
    close: datetime


def session_times(
    hours: MarketHours, exits: ExitConfig, strategy: StrategyConfig
) -> tuple[time, time]:
    """Return (entry_end, flat_time), clamped so we are flat 5 min before the close."""
    flat = min(exits.flat_time, (hours.close - timedelta(minutes=5)).time())
    return min(strategy.entry_end, flat), flat
