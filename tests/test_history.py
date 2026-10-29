"""AlpacaHistorical: SDK mapping, read-through JSON cache, rate limiter (SDK mocked)."""
from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace as NS
from unittest.mock import MagicMock

import pytest

from alpaca.data.enums import DataFeed
from alpaca.data.timeframe import TimeFrameUnit
from alpaca.trading.enums import AssetStatus, ContractType

from daytrader.clock import MarketHours
from daytrader.config import Keys
from daytrader.history import AlpacaHistorical, RateLimiter
from daytrader.models import ET, Bar

UTC = timezone.utc
DAY = date(2026, 10, 28)


def make(tmp_path, **kw) -> AlpacaHistorical:
    h = AlpacaHistorical(Keys("k", "s"), tmp_path / "cache", **kw)
    h._stock, h._option, h._trading = MagicMock(), MagicMock(), MagicMock()
    return h


def sdk_bar(ts: datetime, c: float) -> NS:
    return NS(timestamp=ts, open=c, high=c + 1, low=c - 1, close=c, volume=10)


def test_stock_bars_sip_mapped_to_et_and_cached(tmp_path):
    h = make(tmp_path)
    ts = datetime(2026, 10, 28, 13, 30, tzinfo=UTC)  # 09:30 ET
    h._stock.get_stock_bars.return_value = NS(data={"AAPL": [sdk_bar(ts, 5.0)]})
    want = [Bar(datetime(2026, 10, 28, 9, 30, tzinfo=ET), 5.0, 6.0, 4.0, 5.0, 10.0)]

    assert h.stock_bars("AAPL", DAY, 5) == want
    req = h._stock.get_stock_bars.call_args.args[0]
    assert req.feed == DataFeed.SIP
    assert (req.timeframe.amount_value, req.timeframe.unit_value) == (5, TimeFrameUnit.Minute)
    # the SDK normalises to naive UTC: 09:30-16:00 ET
    assert (req.start, req.end) == (datetime(2026, 10, 28, 13, 30), datetime(2026, 10, 28, 20, 0))

    assert h.stock_bars("AAPL", DAY, 5) == want  # second identical call: cache, no SDK call
    assert h._stock.get_stock_bars.call_count == 1
    other = make(tmp_path)  # a new instance reads the same on-disk cache
    assert other.stock_bars("AAPL", DAY, 5) == want
    other._stock.get_stock_bars.assert_not_called()
    assert len(list((tmp_path / "cache").glob("*.json"))) == 1

    h._stock.get_stock_bars.return_value = NS(data={})
    assert h.stock_bars("AAPL", DAY, 1) == []  # a different request is a different file
    assert h._stock.get_stock_bars.call_count == 2


def test_option_bars_one_minute(tmp_path):
    h = make(tmp_path)
    ts = datetime(2026, 10, 28, 14, 0, tzinfo=UTC)
    h._option.get_option_bars.return_value = NS(data={"X": [sdk_bar(ts, 1.5)]})
    assert h.option_bars_1min("X", DAY) == [Bar(datetime(2026, 10, 28, 10, 0, tzinfo=ET), 1.5, 2.5, 0.5, 1.5, 10.0)]
    req = h._option.get_option_bars.call_args.args[0]
    assert req.timeframe.amount_value == 1 and req.timeframe.unit_value == TimeFrameUnit.Minute
    assert h.option_bars_1min("X", DAY) == h.option_bars_1min("X", DAY)
    assert h._option.get_option_bars.call_count == 1


def contract(sym: str, strike: float, expiry: date) -> NS:
    return NS(symbol=sym, strike_price=strike, expiration_date=expiry)


def test_option_contracts_include_expired_and_paginate(tmp_path):
    h = make(tmp_path)
    exp = date(2026, 10, 30)
    pages = {
        (AssetStatus.INACTIVE, None): NS(option_contracts=[contract("A100", 100.0, exp)], next_page_token="p2"),
        (AssetStatus.INACTIVE, "p2"): NS(option_contracts=[contract("A101", 101.0, exp)], next_page_token=None),
        (AssetStatus.ACTIVE, None): NS(option_contracts=[contract("A99", 99.0, exp)], next_page_token=None),
    }
    h._trading.get_option_contracts.side_effect = lambda r: pages[(r.status, r.page_token)]

    got = h.option_contracts("A", "call", exp)
    assert got == [("A99", 99.0), ("A100", 100.0), ("A101", 101.0)]
    reqs = [c.args[0] for c in h._trading.get_option_contracts.call_args_list]
    assert {r.status for r in reqs} == {AssetStatus.INACTIVE, AssetStatus.ACTIVE}
    assert all(r.type == ContractType.CALL and r.underlying_symbols == ["A"] for r in reqs)
    assert all(r.expiration_date_gte == exp == r.expiration_date_lte for r in reqs)
    assert h.option_contracts("A", "call", exp) == got
    assert h._trading.get_option_contracts.call_count == 3


def test_expiries_after(tmp_path):
    h = make(tmp_path)
    h._trading.get_option_contracts.return_value = NS(
        option_contracts=[contract("A1", 1.0, date(2026, 11, 6)), contract("A2", 2.0, date(2026, 10, 30)),
                          contract("A3", 3.0, date(2026, 10, 30))],
        next_page_token=None,
    )
    assert h.expiries_after("A", DAY) == [date(2026, 10, 30), date(2026, 11, 6)]
    req = h._trading.get_option_contracts.call_args.args[0]
    assert req.expiration_date_gte == date(2026, 10, 29)


def test_trading_days(tmp_path):
    h = make(tmp_path)
    h._trading.get_calendar.return_value = [
        NS(date=DAY, open=datetime(2026, 10, 28, 9, 30), close=datetime(2026, 10, 28, 16, 0)),
        NS(date=date(2026, 11, 27), open=datetime(2026, 11, 27, 9, 30), close=datetime(2026, 11, 27, 13, 0)),
    ]
    got = h.trading_days(DAY, date(2026, 11, 30))
    assert got == [
        (DAY, MarketHours(datetime(2026, 10, 28, 9, 30, tzinfo=ET), datetime(2026, 10, 28, 16, 0, tzinfo=ET))),
        (date(2026, 11, 27), MarketHours(datetime(2026, 11, 27, 9, 30, tzinfo=ET),
                                         datetime(2026, 11, 27, 13, 0, tzinfo=ET))),
    ]
    assert h.trading_days(DAY, date(2026, 11, 30)) == got
    assert h._trading.get_calendar.call_count == 1


def test_rate_limiter_waits_once_180_calls_in_a_minute():
    clock = [0.0]
    slept: list[float] = []

    def sleep(s: float) -> None:
        slept.append(s)
        clock[0] += s

    lim = RateLimiter(180, 60.0, now=lambda: clock[0], sleep=sleep)
    for i in range(180):
        clock[0] = i * 0.1
        lim.acquire()
    assert slept == []
    lim.acquire()  # the 181st call waits until the first leaves the 60 s window
    assert slept == [pytest.approx(60.0 - 17.9)]
    assert clock[0] == pytest.approx(60.0)


def test_sdk_calls_go_through_the_limiter(tmp_path):
    clock = [0.0]
    slept: list[float] = []

    def sleep(s: float) -> None:
        slept.append(s)
        clock[0] += s

    h = make(tmp_path, now=lambda: clock[0], sleep=sleep, calls_per_minute=1)
    h._stock.get_stock_bars.return_value = NS(data={})
    h.stock_bars("A", DAY, 5)
    h.stock_bars("A", DAY, 5)  # cached: no call, no wait
    assert slept == []
    h.stock_bars("B", DAY, 5)
    assert slept == [60.0]
