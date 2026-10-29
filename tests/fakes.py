"""Shared test doubles. Later tasks extend this module (FakeBroker, FakeHistorical)."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Sequence

from daytrader.clock import MarketHours
from daytrader.models import Bar, OptionQuote, OptionType


class FakeClock:
    def __init__(self, start: datetime):
        self._now = start

    def now(self) -> datetime:
        return self._now

    def sleep_until(self, dt: datetime) -> None:
        self._now = max(self._now, dt)

    def advance(self, seconds: float) -> None:
        self._now = self._now + timedelta(seconds=seconds)


class FakeData:
    def __init__(self) -> None:
        self.hours: dict[date, MarketHours | None] = {}
        self.bars: dict[str, list[Bar]] = {}
        self.chains: dict[tuple[str, str], list[OptionQuote]] = {}
        self.stock_prices: dict[str, float] = {}
        self.option_quotes: dict[str, tuple[float, float, datetime]] = {}

    def market_hours(self, day: date) -> MarketHours | None:
        return self.hours.get(day)

    def bars_5min(
        self, tickers: Sequence[str], start: datetime, end: datetime
    ) -> dict[str, list[Bar]]:
        out: dict[str, list[Bar]] = {}
        for tk in tickers:
            sel = [
                b for b in self.bars.get(tk, [])
                if start <= b.t and b.t + timedelta(minutes=5) <= end
            ]
            if sel:
                out[tk] = sel
        return out

    def option_chain(self, underlying: str, kind: OptionType, today: date) -> list[OptionQuote]:
        return list(self.chains.get((underlying, kind), []))

    def latest_stock_price(self, ticker: str) -> float:
        return self.stock_prices[ticker]

    def latest_option_quote(self, symbol: str) -> tuple[float, float, datetime]:
        return self.option_quotes[symbol]
