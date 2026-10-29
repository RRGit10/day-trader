from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace as NS
from unittest.mock import MagicMock

import pytest

from daytrader.config import Keys
from daytrader.data import AlpacaData, with_retries
from daytrader.models import ET, Bar
from fakes import FakeData

UTC = timezone.utc


def test_retries_succeed_third_try():
    calls, slept = [], []

    def fn():
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("boom")
        return "ok"

    assert with_retries(fn, sleep=slept.append) == "ok"
    assert slept == [1, 2]


def test_retries_raise_last_after_three_failures():
    slept, n = [], []

    def fn():
        n.append(1)
        raise ValueError(f"fail{len(n)}")

    with pytest.raises(ValueError, match="fail3"):
        with_retries(fn, sleep=slept.append)
    assert len(n) == 3 and slept == [1, 2]


def make_data():
    d = AlpacaData(Keys("k", "s"))
    d._stock, d._option, d._trading = MagicMock(), MagicMock(), MagicMock()
    return d


def test_bars_mapped_to_et():
    d = make_data()
    ts = datetime(2026, 10, 28, 13, 30, tzinfo=UTC)  # 09:30 ET (EDT)
    d._stock.get_stock_bars.return_value = NS(
        data={"AAPL": [NS(timestamp=ts, open=1.0, high=2.0, low=0.5, close=1.5, volume=100)]}
    )
    out = d.bars_5min(["AAPL", "MSFT"], datetime(2026, 10, 28, 9, 30, tzinfo=ET),
                      datetime(2026, 10, 28, 10, 0, tzinfo=ET))
    assert out == {"AAPL": [Bar(datetime(2026, 10, 28, 9, 30, tzinfo=ET), 1.0, 2.0, 0.5, 1.5, 100)]}
    assert out["AAPL"][0].t.tzinfo is not None and out["AAPL"][0].t.hour == 9


def test_option_chain_merges_contracts():
    d = make_data()
    today = date(2026, 10, 28)
    q = lambda b, a: NS(bid_price=b, ask_price=a, timestamp=None)
    d._option.get_option_chain.return_value = {
        "AAPL261030C00200000": NS(latest_quote=q(1.0, 1.2), greeks=NS(delta=0.65)),
        "AAPL261030C00210000": NS(latest_quote=None, greeks=None),
        "ORPHAN": NS(latest_quote=q(1, 2), greeks=NS(delta=0.5)),
    }
    c = lambda sym, k, oi: NS(symbol=sym, expiration_date=date(2026, 10, 30), type="call",
                              strike_price=k, open_interest=oi, underlying_symbol="AAPL")
    d._trading.get_option_contracts.side_effect = [
        NS(option_contracts=[c("AAPL261030C00200000", 200.0, "750")], next_page_token="p2"),
        NS(option_contracts=[c("AAPL261030C00210000", 210.0, None)], next_page_token=None),
    ]
    out = {o.symbol: o for o in d.option_chain("AAPL", "call", today)}
    assert set(out) == {"AAPL261030C00200000", "AAPL261030C00210000"}
    a = out["AAPL261030C00200000"]
    assert (a.bid, a.ask, a.delta, a.open_interest, a.strike, a.expiry, a.type, a.underlying) == (
        1.0, 1.2, 0.65, 750, 200.0, date(2026, 10, 30), "call", "AAPL")
    b = out["AAPL261030C00210000"]
    assert (b.bid, b.ask, b.delta, b.open_interest) == (0.0, 0.0, None, None)
    req = d._option.get_option_chain.call_args[0][0]
    assert req.expiration_date_gte == today + timedelta(days=1)
    assert req.expiration_date_lte == today + timedelta(days=10)


def test_calendar_hours_and_closed():
    d = make_data()
    day = date(2026, 10, 28)
    d._trading.get_calendar.return_value = [NS(
        date=day, open=datetime(2026, 10, 28, 9, 30), close=datetime(2026, 10, 28, 16, 0))]
    h = d.market_hours(day)
    assert h.open == datetime(2026, 10, 28, 9, 30, tzinfo=ET) and h.close.hour == 16
    d._trading.get_calendar.return_value = []
    assert d.market_hours(day) is None


def test_latest_prices():
    d = make_data()
    d._stock.get_stock_latest_trade.return_value = {"AAPL": NS(price=190.5)}
    assert d.latest_stock_price("AAPL") == 190.5
    ts = datetime(2026, 10, 28, 14, 0, tzinfo=UTC)
    d._option.get_option_latest_quote.return_value = {"X": NS(bid_price=1.0, ask_price=1.1, timestamp=ts)}
    bid, ask, t = d.latest_option_quote("X")
    assert (bid, ask) == (1.0, 1.1) and t == ts and t.hour == 10


def test_fake_data_bars_filter():
    f = FakeData()
    t0 = datetime(2026, 10, 28, 9, 30, tzinfo=ET)
    f.bars["A"] = [Bar(t0 + timedelta(minutes=5 * i), 1, 1, 1, 1, 1) for i in range(4)]
    out = f.bars_5min(["A", "B"], t0 + timedelta(minutes=5), t0 + timedelta(minutes=15))
    assert [b.t.minute for b in out["A"]] == [35, 40] and "B" not in out
