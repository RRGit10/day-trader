"""Clock abstraction and trading-session time derivation."""
from __future__ import annotations

import time as _time
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Protocol

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
