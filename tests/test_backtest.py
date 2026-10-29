"""Backtest replays synthetic days from FakeHistorical through the pure decision modules."""
from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta

import pytest

from daytrader.backtest import BacktestSummary, run_backtest
from daytrader.clock import MarketHours
from daytrader.config import (
    BacktestConfig,
    ContractConfig,
    ExitConfig,
    RiskConfig,
    Settings,
    StrategyConfig,
)
from daytrader.journal import Journal
from daytrader.models import ET, Bar
from daytrader.pricing import bs_price
from daytrader.session import Session
from tests.fakes import FakeBroker, FakeClock, FakeData, FakeHistorical, ScriptedApprover

TODAY = date(2026, 10, 28)  # Wednesday
PREV = date(2026, 10, 27)
EXPIRY = date(2026, 10, 30)
H = 0.03 / 2  # half the assumed spread
ONE_MIN = timedelta(minutes=1)


def at(hh: int, mm: int, day: date = TODAY) -> datetime:
    return datetime(day.year, day.month, day.day, hh, mm, tzinfo=ET)


def _bar(t: datetime, c: float, h: float, l: float) -> Bar:
    return Bar(t, c, h, l, c, 1000.0)


def make_bars(direction: str = "long", trigger: time = time(9, 55)) -> tuple[list[Bar], list[Bar]]:
    """(prior_day, today) 5-min bars; ORB fires at the `trigger` close (range 99-101, mid 100)."""
    sign = 1 if direction == "long" else -1
    prior = [
        _bar(at(9, 30, PREV) + timedelta(minutes=5 * i), 100 - sign * (10 - 0.12 * i),
             100 - sign * (10 - 0.12 * i) + 0.1, 100 - sign * (10 - 0.12 * i) - 0.1)
        for i in range(78)
    ]
    trig = datetime.combine(TODAY, trigger, tzinfo=ET)
    today = []
    for i in range(78):
        t = at(9, 30) + timedelta(minutes=5 * i)
        if t < at(9, 45):
            c, h, l = 100.0, 101.0, 99.0
        elif t < trig - timedelta(minutes=10):
            c = 100 + sign * 0.2
            h, l = c + 0.3, c - 0.3
        elif t == trig - timedelta(minutes=10):
            c = 100 + sign * 2
            h, l = c + 0.2, c - 0.2
        else:
            c = 100 + sign * 3
            h, l = c + 0.2, c - 0.2
        today.append(_bar(t, c, h, l))
    return prior, today


# Option prices at sigma 0.30 for S=103 at the 09:55 trigger; strike 102 has delta ~0.67.
_T = (datetime.combine(EXPIRY, time(16), tzinfo=ET) - at(9, 55)).total_seconds() / (365 * 86400)
STRIKES = range(94, 113)
PRICES = {k: round(bs_price(103.0, k, _T, 0.04, 0.30, "call"), 2) for k in STRIKES}
P = PRICES[102]  # 1.56
ENTRY = P * (1 + H)
QTY = math.floor(500 / (ENTRY * 100))


def settings(tickers=("AAA",)) -> Settings:
    return Settings(
        tickers=tuple(tickers), strategy=StrategyConfig(), contract=ContractConfig(),
        exits=ExitConfig(), risk=RiskConfig(), backtest=BacktestConfig(),
        state_dir=None, approval_keys=None, bot_keys=None,
    )


class Day:
    def __init__(self, tmp_path) -> None:
        self.src = FakeHistorical()
        self.src.hours[PREV] = MarketHours(at(9, 30, PREV), at(16, 0, PREV))
        self.src.hours[TODAY] = MarketHours(at(9, 30), at(16, 0))
        self.journal = Journal(tmp_path / "bt.db")
        self.tickers: list[str] = []

    def add(self, tk: str, trigger: time = time(9, 55), options: str = "full") -> None:
        """options: "full" (chain + bars), "no_bars" (chain only), "sparse" (no bars after trigger)."""
        self.tickers.append(tk)
        prior, today = make_bars("long", trigger)
        self.src.stock[(tk, PREV, 5)] = prior
        self.src.stock[(tk, TODAY, 5)] = today
        close = datetime.combine(TODAY, trigger, tzinfo=ET)
        minutes = [at(9, 30) + ONE_MIN * i for i in range(390)]
        self.src.stock[(tk, TODAY, 1)] = [_bar(t, 103.0, 103.1, 102.9) for t in minutes if t >= close]
        contracts = [(f"{tk}C{k}", float(k)) for k in STRIKES]
        self.src.contracts[(tk, "call", EXPIRY)] = contracts
        if options == "no_bars":
            return
        for sym, k in contracts:  # one 1-min bar inside the trigger bar for every strike
            self.src.option_bars[(sym, TODAY)] = [_bar(close - 2 * ONE_MIN, PRICES[k], PRICES[k], PRICES[k])]
        if options == "full":
            self.src.option_bars[(f"{tk}C102", TODAY)] += [_bar(t, P, P, P) for t in minutes if t >= close]

    def set_option(self, tk: str, t: datetime, h: float, l: float, c: float) -> None:
        bars = self.src.option_bars[(f"{tk}C102", TODAY)]
        self.src.option_bars[(f"{tk}C102", TODAY)] = sorted(
            [b for b in bars if b.t != t] + [_bar(t, c, h, l)], key=lambda b: b.t)

    def set_stock(self, tk: str, t: datetime, h: float, l: float, c: float) -> None:
        bars = self.src.stock[(tk, TODAY, 1)]
        self.src.stock[(tk, TODAY, 1)] = sorted(
            [b for b in bars if b.t != t] + [_bar(t, c, h, l)], key=lambda b: b.t)

    def run(self) -> BacktestSummary:
        return run_backtest(self.src, settings(self.tickers), TODAY, TODAY, self.journal)

    def trades(self) -> list[tuple]:
        return self.journal._conn.execute(
            "SELECT ticker, option_symbol, qty, entry_price, exit_price, pnl, reason, opened_at,"
            " closed_at FROM trades ORDER BY id").fetchall()

    def skips(self) -> list[tuple[str, str]]:
        return [(tk, reason) for _, tk, reason in self.journal.skip_rows()]


@pytest.fixture
def day(tmp_path):
    d = Day(tmp_path)
    yield d
    d.journal.close()


def test_target_day(day):
    day.add("AAA", time(9, 55))
    day.add("BBB", time(10, 5))  # fires while AAA is open: guard skip, never retried
    day.set_option("AAA", at(10, 20), ENTRY * 1.2 + 0.01, P, 1.80)
    summary = day.run()

    assert summary == BacktestSummary(days=1, signals=2, trades=1, no_data_skips=0)
    [(tk, sym, qty, entry, exit_, pnl, reason, opened, closed)] = day.trades()
    assert (tk, sym, qty, reason) == ("AAA", "AAAC102", QTY, "take_profit")
    assert entry == pytest.approx(ENTRY)
    assert exit_ == pytest.approx(ENTRY * 1.2 * (1 - H))
    assert pnl == pytest.approx(round((exit_ - entry) * QTY * 100, 2))
    assert (opened, closed) == (at(9, 55).isoformat(), at(10, 21).isoformat())
    assert day.skips() == [("BBB", "position_open")]
    # a check row for every ticker at every 5-min close 09:55..11:30
    rows = day.journal.check_rows()
    assert len(rows) == 2 * 20
    assert rows[0][0] == at(9, 55) and rows[-1][0] == at(11, 30)


def test_candidates_are_ten_nearest_strikes(day, monkeypatch):
    day.add("AAA")
    asked: list[str] = []
    real = day.src.option_bars_1min
    monkeypatch.setattr(day.src, "option_bars_1min", lambda s, d: asked.append(s) or real(s, d))
    day.run()
    assert sorted(set(asked)) == sorted(f"AAAC{k}" for k in range(98, 108))


def test_stop_day(day):
    day.add("AAA")
    day.set_option("AAA", at(10, 10), P, 1.40, 1.45)
    summary = day.run()
    assert summary.trades == 1
    [(_, _, qty, entry, exit_, pnl, reason, _, closed)] = day.trades()
    assert reason == "stop_loss"
    assert exit_ == pytest.approx(ENTRY * 0.9 * (1 - H))
    assert pnl < 0 and closed == at(10, 11).isoformat()


def test_both_in_one_bar_counts_as_stop(day):
    day.add("AAA")
    day.set_option("AAA", at(10, 10), 2.50, 1.30, 1.90)
    day.run()
    [(_, _, _, _, exit_, _, reason, _, _)] = day.trades()
    assert reason == "stop_loss"
    assert exit_ == pytest.approx(ENTRY * 0.9 * (1 - H))


def test_stock_stop_beats_take_profit_and_fills_at_option_close(day):
    day.add("AAA")
    day.set_option("AAA", at(10, 30), 2.50, 1.50, 1.70)
    day.set_stock("AAA", at(10, 30), 103.0, 99.9, 100.5)  # crosses range mid 100
    day.run()
    [(_, _, _, _, exit_, _, reason, _, _)] = day.trades()
    assert reason == "stock_stop"
    assert exit_ == pytest.approx(1.70 * (1 - H))


def test_stock_target(day):
    day.add("AAA")
    day.set_stock("AAA", at(11, 0), 106.0, 103.0, 105.5)  # target = 103 + (103 - 100)
    day.run()
    [(_, _, _, _, exit_, _, reason, _, _)] = day.trades()
    assert reason == "stock_target"
    assert exit_ == pytest.approx(P * (1 - H))


def test_time_exit_day(day):
    day.add("AAA", options="sparse")  # no option bars after the trigger except one
    day.set_option("AAA", at(12, 0), 1.62, 1.58, 1.60)  # carried to the flat time
    day.run()
    [(_, _, _, _, exit_, _, reason, _, closed)] = day.trades()
    assert reason == "time"
    assert exit_ == pytest.approx(1.60 * (1 - H))
    assert closed == at(15, 55).isoformat()


def test_guard_stops_after_two_losses(day):
    day.add("AAA", time(9, 55))
    day.add("BBB", time(10, 30))
    day.add("CCC", time(11, 0))
    day.set_option("AAA", at(10, 0), P, 1.30, 1.35)
    day.set_option("BBB", at(10, 40), P, 1.30, 1.35)
    summary = day.run()
    assert summary == BacktestSummary(days=1, signals=3, trades=2, no_data_skips=0)
    assert [r[6] for r in day.trades()] == ["stop_loss", "stop_loss"]
    assert day.skips() == [("CCC", "max_losses")]


def test_missing_option_bars_counts_no_data(day):
    day.add("AAA", options="no_bars")
    summary = day.run()
    assert summary == BacktestSummary(days=1, signals=1, trades=0, no_data_skips=1)
    assert day.trades() == []
    assert day.skips() == [("AAA", "no_data")]


def test_backtest_and_session_agree_on_signals(tmp_path):
    shapes = {"AAA": ("long", time(9, 55)), "BBB": ("short", time(10, 15))}
    tickers = tuple(shapes)

    src = FakeHistorical()
    src.hours[PREV] = MarketHours(at(9, 30, PREV), at(16, 0, PREV))
    src.hours[TODAY] = MarketHours(at(9, 30), at(16, 0))
    clock = FakeClock(at(9, 25))
    data = FakeData()
    data.hours = dict(src.hours)
    for tk, (direction, trigger) in shapes.items():
        prior, today = make_bars(direction, trigger)
        src.stock[(tk, PREV, 5)], src.stock[(tk, TODAY, 5)] = prior, today
        data.bars[tk] = prior + today

    bt = Journal(tmp_path / "bt.db")
    run_backtest(src, settings(tickers), TODAY, TODAY, bt)

    live = Journal(tmp_path / "live.db")
    session = Session("bot", settings(tickers), data, FakeBroker(), live, ScriptedApprover(), clock)
    session.startup()
    session.scan_list = list(tickers)  # no option chains here: skip the affordability filter
    close = at(9, 55)
    while close <= at(11, 30):
        session.on_bar_close(close)
        close += timedelta(minutes=5)

    def signals(j: Journal) -> dict[str, list[tuple]]:
        out: dict[str, list[tuple]] = {}
        for tk, trig, direction in j._conn.execute(
                "SELECT ticker, triggered, direction FROM checks ORDER BY id"):
            out.setdefault(tk, []).append((tk, bool(trig), direction))
        return out

    got, want = signals(bt), signals(live)
    assert got == want
    assert ("AAA", True, "long") in got["AAA"] and ("BBB", True, "short") in got["BBB"]
    assert len(got["AAA"]) == 20
    bt.close()
    live.close()
