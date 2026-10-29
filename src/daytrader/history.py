"""Historical data for the backtest: Alpaca SDK + read-through JSON cache + rate limiter."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time as _time
from collections import deque
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Callable

from alpaca.data.enums import DataFeed
from alpaca.data.historical import OptionHistoricalDataClient, StockHistoricalDataClient
from alpaca.data.requests import OptionBarsRequest, StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import AssetStatus, ContractType
from alpaca.trading.requests import GetCalendarRequest, GetOptionContractsRequest

from .clock import MarketHours
from .config import Keys
from .data import _et, with_retries
from .models import ET, Bar, OptionType

CALLS_PER_MINUTE = 180
DAY_OPEN, DAY_CLOSE = time(9, 30), time(16, 0)
EXPIRY_LOOKAHEAD = timedelta(days=10)  # same expiry window as the live option chain
RECENT_DAYS = 3  # empty answers for days this recent may just be data not published yet


class RateLimiter:
    """Allows at most `max_calls` per sliding `period` seconds."""

    def __init__(self, max_calls: int, period: float,
                 now: Callable[[], float] = _time.monotonic,
                 sleep: Callable[[float], None] = _time.sleep) -> None:
        self.max_calls, self.period = max_calls, period
        self._now, self._sleep = now, sleep
        self._calls: deque[float] = deque()

    def acquire(self) -> None:
        now = self._now()
        while self._calls and now - self._calls[0] >= self.period:
            self._calls.popleft()
        if len(self._calls) >= self.max_calls:
            self._sleep(self._calls[0] + self.period - now)
            self._calls.popleft()
            now = self._now()
        self._calls.append(now)


def _bars_to_json(bars) -> list[list]:
    return [[_et(b.timestamp).isoformat(), float(b.open), float(b.high), float(b.low),
             float(b.close), float(b.volume)] for b in bars]


def _bars_from_json(rows: list[list]) -> list[Bar]:
    return [Bar(datetime.fromisoformat(r[0]).astimezone(ET), *map(float, r[1:])) for r in rows]


class AlpacaHistorical:
    """HistoricalSource over Alpaca: SIP stock bars, 1-min option bars, active + expired contracts.

    Every response is cached as one JSON file per request under `cache_dir` (file name =
    sha256 of the canonical request key); cached requests make no API call.
    """

    def __init__(self, keys: Keys, cache_dir: Path, *,
                 now: Callable[[], float] = _time.monotonic,
                 sleep: Callable[[float], None] = _time.sleep,
                 calls_per_minute: int = CALLS_PER_MINUTE,
                 today: Callable[[], date] = lambda: datetime.now(ET).date()) -> None:
        self._stock = StockHistoricalDataClient(keys.api_key, keys.api_secret)
        self._option = OptionHistoricalDataClient(keys.api_key, keys.api_secret)
        self._trading = TradingClient(keys.api_key, keys.api_secret, paper=True)
        self._cache_dir = Path(cache_dir)
        self._limiter = RateLimiter(calls_per_minute, 60.0, now, sleep)
        self._sleep = sleep
        self._today = today

    # ------------------------------------------------------------- plumbing
    def _call(self, fn: Callable[[], Any]) -> Any:
        def limited():
            self._limiter.acquire()
            return fn()
        return with_retries(limited, sleep=self._sleep)

    def _cached(self, key: dict, fetch: Callable[[], Any], day: date | None = None) -> Any:
        """Read-through cache; an empty answer for a `day` in the last RECENT_DAYS calendar
        days is returned but not cached."""
        canonical = json.dumps(key, sort_keys=True, separators=(",", ":"))
        path = self._cache_dir / f"{hashlib.sha256(canonical.encode()).hexdigest()}.json"
        if path.is_file():
            return json.loads(path.read_text())["value"]
        value = fetch()
        if not value and day is not None and (self._today() - day).days <= RECENT_DAYS:
            return value
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self._cache_dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump({"key": key, "value": value}, fh)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        return value

    def _window(self, day: date) -> tuple[datetime, datetime]:
        return datetime.combine(day, DAY_OPEN, tzinfo=ET), datetime.combine(day, DAY_CLOSE, tzinfo=ET)

    def _contracts(self, underlying: str, kind: OptionType | None, gte: date, lte: date) -> list[list]:
        """[[symbol, strike, expiry iso]] for active and expired contracts, all pages."""
        out: dict[str, list] = {}
        ctype = None if kind is None else (ContractType.CALL if kind == "call" else ContractType.PUT)
        for status in (AssetStatus.INACTIVE, AssetStatus.ACTIVE):
            token = None
            while True:
                req = GetOptionContractsRequest(
                    underlying_symbols=[underlying], type=ctype, status=status,
                    expiration_date_gte=gte, expiration_date_lte=lte, limit=1000, page_token=token,
                )
                resp = self._call(lambda: self._trading.get_option_contracts(req))
                for c in resp.option_contracts:
                    out[c.symbol] = [c.symbol, float(c.strike_price), c.expiration_date.isoformat()]
                token = resp.next_page_token
                if not token:
                    break
        return sorted(out.values(), key=lambda c: (c[2], c[1], c[0]))

    # ------------------------------------------------------ HistoricalSource
    def trading_days(self, start: date, end: date) -> list[tuple[date, MarketHours]]:
        def fetch():
            req = GetCalendarRequest(start=start, end=end)
            return [[c.date.isoformat(), _et(c.open).isoformat(), _et(c.close).isoformat()]
                    for c in self._call(lambda: self._trading.get_calendar(req))]
        rows = self._cached({"m": "calendar", "start": start.isoformat(), "end": end.isoformat()}, fetch)
        return [(date.fromisoformat(d), MarketHours(datetime.fromisoformat(o), datetime.fromisoformat(c)))
                for d, o, c in rows]

    def stock_bars(self, ticker: str, day: date, minutes: int) -> list[Bar]:
        def fetch():
            start, end = self._window(day)
            req = StockBarsRequest(symbol_or_symbols=[ticker],
                                   timeframe=TimeFrame(minutes, TimeFrameUnit.Minute),
                                   start=start, end=end, feed=DataFeed.SIP)
            return _bars_to_json(self._call(lambda: self._stock.get_stock_bars(req)).data.get(ticker, []))
        key = {"m": "stock_bars", "ticker": ticker, "day": day.isoformat(), "minutes": minutes, "feed": "sip"}
        return _bars_from_json(self._cached(key, fetch, day))

    def option_bars_1min(self, symbol: str, day: date) -> list[Bar]:
        def fetch():
            start, end = self._window(day)
            req = OptionBarsRequest(symbol_or_symbols=[symbol],
                                    timeframe=TimeFrame(1, TimeFrameUnit.Minute), start=start, end=end)
            return _bars_to_json(self._call(lambda: self._option.get_option_bars(req)).data.get(symbol, []))
        key = {"m": "option_bars", "symbol": symbol, "day": day.isoformat()}
        return _bars_from_json(self._cached(key, fetch, day))

    def option_contracts(self, underlying: str, kind: OptionType, expiry: date) -> list[tuple[str, float]]:
        key = {"m": "option_contracts", "underlying": underlying, "kind": kind, "expiry": expiry.isoformat()}
        rows = self._cached(key, lambda: self._contracts(underlying, kind, expiry, expiry))
        return [(sym, strike) for sym, strike, _ in rows]

    def expiries_after(self, underlying: str, day: date) -> list[date]:
        gte, lte = day + timedelta(days=1), day + EXPIRY_LOOKAHEAD
        key = {"m": "expiries", "underlying": underlying, "gte": gte.isoformat(), "lte": lte.isoformat()}
        rows = self._cached(key, lambda: self._contracts(underlying, None, gte, lte))
        return sorted({date.fromisoformat(exp) for _, _, exp in rows})
