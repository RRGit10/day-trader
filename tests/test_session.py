"""Session scenarios: FakeClock + LiveFakeData + FakeBroker + a temp Journal."""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime, time, timedelta

import pytest

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
from daytrader.models import ET, Bar, OptionQuote, Position
from daytrader.session import Session
from tests.fakes import FakeBroker, FakeClock, LiveFakeData, ScriptedApprover

TODAY = date(2026, 10, 28)  # Wednesday
PREV = date(2026, 10, 27)
EXPIRY = date(2026, 10, 30)


def at(hh: int, mm: int, ss: int = 0, day: date = TODAY) -> datetime:
    return datetime(day.year, day.month, day.day, hh, mm, ss, tzinfo=ET)


def _bar(t: datetime, c: float, h: float, l: float) -> Bar:
    return Bar(t, c, h, l, c, 1000.0)


def make_bars(direction: str = "long", trigger: time = time(9, 55)) -> tuple[list[Bar], list[Bar]]:
    """(prior_day, today) 5-min bars; ORB fires at the `trigger` close (range 99-101, mid 100)."""
    sign = 1 if direction == "long" else -1
    prior = [
        _bar(at(9, 30, day=PREV) + timedelta(minutes=5 * i), 100 - sign * (10 - 0.12 * i),
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


def quote(tk: str, kind: str = "call", bid: float = 2.45, ask: float = 2.55) -> OptionQuote:
    return OptionQuote(
        symbol=f"{tk}{'C' if kind == 'call' else 'P'}", underlying=tk, type=kind, strike=100.0,
        expiry=EXPIRY, bid=bid, ask=ask, delta=0.65 if kind == "call" else -0.65,
        open_interest=1000,
    )


class Env:
    def __init__(self, tmp_path, tickers=("AAA",), close=at(16, 0), approver=None,
                 data_cls=LiveFakeData, mode="bot", start=at(9, 25)):
        self.clock = FakeClock(start)
        self.data = data_cls(self.clock)
        self.broker = FakeBroker()
        self.journal = Journal(tmp_path / "journal.db")
        self.approver = approver or ScriptedApprover()
        self.settings = Settings(
            tickers=tuple(tickers), strategy=StrategyConfig(), contract=ContractConfig(),
            exits=ExitConfig(), risk=RiskConfig(), backtest=BacktestConfig(),
            state_dir=tmp_path / "state", approval_keys=None, bot_keys=None,
        )
        self.data.hours[TODAY] = MarketHours(at(9, 30), close)
        self.data.hours[PREV] = MarketHours(at(9, 30, day=PREV), at(16, 0, day=PREV))
        self.session = Session(mode, self.settings, self.data, self.broker, self.journal,
                               self.approver, self.clock)

    def add(self, tk: str, direction: str = "long", trigger: time = time(9, 55)) -> None:
        prior, today = make_bars(direction, trigger)
        self.data.bars[tk] = prior + today
        self.data.chains[(tk, "call")] = [quote(tk, "call")]
        self.data.chains[(tk, "put")] = [quote(tk, "put")]
        self.data.set_quote(f"{tk}C", 2.45, 2.55)
        self.data.set_quote(f"{tk}P", 2.45, 2.55)
        self.data.stock_prices[tk] = today[-1].c

    def open_day(self) -> None:
        self.session.startup()
        self.clock.sleep_until(at(9, 40))
        self.session.affordability_filter()

    def skips(self) -> list[tuple[str, str]]:
        return [(tk, reason) for _, tk, reason in self.journal.skip_rows()]

    def events(self, level: str | None = None) -> list[tuple[str, str]]:
        rows = self.journal._conn.execute("SELECT level, message FROM events ORDER BY id")
        return [(lv, m) for lv, m in rows if level is None or lv == level]

    def trade_rows(self) -> list[tuple]:
        return self.journal._conn.execute(
            "SELECT ticker, qty, entry_price, exit_price, pnl, reason FROM trades ORDER BY id"
        ).fetchall()

    def ticket_statuses(self) -> list[str]:
        return [r[0] for r in self.journal._conn.execute("SELECT status FROM tickets ORDER BY ts, rowid")]


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.journal.close()


def test_bot_day_take_profit(tmp_path):
    e = Env(tmp_path, tickers=("AAA", "BBB"))
    e.add("AAA", "long", time(9, 55))
    e.add("BBB", "long", time(10, 0))
    e.open_day()

    e.session.on_bar_close(at(9, 55))
    pos = e.session.position
    assert pos is not None and pos.option_symbol == "AAAC"
    assert (pos.qty, pos.entry_price, pos.entry_stock_price, pos.range_mid) == (2, 2.50, 103.0, 100.0)
    assert e.broker.orders[0] == {"side": "buy", "symbol": "AAAC", "qty": 2, "limit": 2.50, "type": "limit"}
    ticket = e.approver.tickets[0]
    assert (ticket.limit, ticket.qty, ticket.cost) == (2.50, 2, 500.0)
    assert (ticket.take_profit_price, ticket.stop_loss_price) == (3.00, 2.25)
    assert e.ticket_statuses() == ["approved"]

    e.clock.advance(15)
    e.session.manage_position()
    assert e.session.position is not None  # nothing hit yet

    e.data.set_quote("AAAC", 3.00, 3.10)  # mid 3.05 >= 2.50 * 1.2
    e.data.stock_prices["AAA"] = 103.5
    e.clock.advance(15)
    e.session.manage_position()
    assert e.session.position is None
    assert e.trade_rows() == [("AAA", 2, 2.50, 3.00, 100.0, "take_profit")]
    assert e.broker.orders[-1] == {"side": "sell", "symbol": "AAAC", "qty": 2, "limit": 3.00, "type": "limit"}
    assert e.broker.positions() == {}

    e.session.on_bar_close(at(10, 0))  # BBB triggers; AAA long already signaled today
    assert ("BBB", "profit_target_hit") in e.skips()
    assert len(e.broker.orders) == 2
    assert len(e.approver.tickets) == 1
    e.journal.close()


def _stop_out(e: Env, tk: str) -> None:
    e.data.set_quote(f"{tk}C", 2.20, 2.30)  # mid 2.25 = -10%
    e.clock.advance(15)
    e.session.manage_position()


def test_two_losses_stop_the_day(tmp_path):
    e = Env(tmp_path, tickers=("AAA", "BBB", "CCC"))
    e.add("AAA", "long", time(9, 55))
    e.add("BBB", "long", time(10, 0))
    e.add("CCC", "long", time(10, 5))
    e.open_day()

    e.session.on_bar_close(at(9, 55))
    _stop_out(e, "AAA")
    e.session.on_bar_close(at(10, 0))
    assert e.session.position.ticker == "BBB"
    _stop_out(e, "BBB")
    assert [r[5] for r in e.trade_rows()] == ["stop_loss", "stop_loss"]
    assert [r[4] for r in e.trade_rows()] == [-60.0, -60.0]

    e.session.on_bar_close(at(10, 5))
    assert ("CCC", "max_losses") in e.skips()
    assert e.session.position is None
    assert sum(1 for o in e.broker.orders if o["side"] == "buy") == 2
    e.journal.close()


def _enter_aaa(e: Env) -> Position:
    e.add("AAA")
    e.open_day()
    e.session.on_bar_close(at(9, 55))
    assert e.session.position is not None
    return e.session.position


def test_time_exit(env):
    _enter_aaa(env)
    env.clock.sleep_until(at(15, 54, 45))
    env.session.manage_position()
    assert env.session.position is not None
    env.clock.sleep_until(at(15, 55))
    env.session.manage_position()
    assert env.session.position is None
    assert env.trade_rows()[0][5] == "time"


def test_half_day_time_exit(tmp_path):
    e = Env(tmp_path, close=at(13, 0))
    _enter_aaa(e)
    e.clock.sleep_until(at(12, 54, 45))
    e.session.manage_position()
    assert e.session.position is not None
    e.clock.sleep_until(at(12, 55))
    e.session.manage_position()
    assert e.session.position is None
    assert e.trade_rows()[0][5] == "time"
    e.journal.close()


def _approval_denied(tmp_path, decision: str) -> None:
    e = Env(tmp_path, approver=ScriptedApprover([decision]), mode="approval")
    e.add("AAA")
    e.open_day()
    e.session.on_bar_close(at(9, 55))
    assert e.broker.orders == []
    assert e.session.position is None
    assert ("AAA", decision) in e.skips()
    assert e.ticket_statuses() == [decision]
    e.journal.close()


def test_approval_rejected_buys_nothing(tmp_path):
    _approval_denied(tmp_path, "rejected")


def test_approval_expired_buys_nothing(tmp_path):
    _approval_denied(tmp_path, "expired")


def test_price_moved_reasks_then_skips(tmp_path):
    moves = iter([(2.80, 2.90), (3.20, 3.30)])

    def on_request(ticket):
        bid, ask = next(moves)  # quote moves while the human is deciding
        e.data.set_quote("AAAC", bid, ask)

    e = Env(tmp_path, approver=ScriptedApprover(on_request=on_request), mode="approval")
    e.add("AAA")
    e.open_day()
    e.session.on_bar_close(at(9, 55))

    first, second = e.approver.tickets
    assert first.limit == 2.50 and second.limit == 2.85
    assert second.qty == 1 and second.id != first.id
    assert e.broker.orders == []
    assert ("AAA", "price_moved") in e.skips()
    assert e.ticket_statuses() == ["price_moved", "price_moved"]
    e.journal.close()


def test_requote_within_tolerance_enters_at_ticket_limit(tmp_path):
    e = Env(tmp_path, approver=ScriptedApprover(
        on_request=lambda t: e.data.set_quote("AAAC", 2.55, 2.65)))  # mid 2.60: +4%
    _enter_aaa(e)
    assert len(e.approver.tickets) == 1
    assert e.broker.orders[0]["limit"] == 2.50
    e.journal.close()


def test_partial_fill_uses_filled_qty(env):
    env.broker.fill_policy = "partial"
    pos = _enter_aaa(env)
    assert pos.qty == 1
    assert env.broker.cancels == ["o1"]
    assert env.trade_rows()[0][1] == 1
    assert env.broker.positions() == {"AAAC": 1}


def test_cancel_exception_still_reads_filled_qty(env):
    env.broker.fill_policy = "partial"
    env.broker.cancel_raises = True
    pos = _enter_aaa(env)
    assert pos.qty == 1
    assert env.events("warn")


def test_fill_racing_the_cancel_is_kept(env):
    env.broker.fill_policy = "never"
    env.broker.fill_during_cancel = True
    pos = _enter_aaa(env)
    assert pos.qty == 2
    assert env.broker.positions() == {"AAAC": 2}


def test_unfilled_entry_cancelled(env):
    env.broker.fill_policy = "never"
    env.add("AAA")
    env.open_day()
    env.session.on_bar_close(at(9, 55))
    assert env.session.position is None
    assert env.broker.cancels == ["o1"]
    assert ("AAA", "not_filled") in env.skips()
    assert env.trade_rows() == []
    assert env.clock.now() >= at(9, 55, 5) + timedelta(seconds=60)


def test_stale_quote_uses_stock_exits(env):
    _enter_aaa(env)
    old = env.clock.now() - timedelta(seconds=61)
    env.data.set_quote("AAAC", 1.00, 1.10, ts=old)  # would be a stop_loss if trusted
    env.clock.advance(15)
    env.session.manage_position()
    env.clock.advance(15)
    env.session.manage_position()
    assert env.session.position is not None
    assert len(env.events("warn")) == 1  # warned once per position

    env.data.stock_prices["AAA"] = 100.0  # back to range mid
    env.clock.advance(15)
    env.session.manage_position()
    assert env.session.position is None
    assert env.trade_rows()[0][5] == "stock_stop"


def _journal_position(e: Env, symbol: str = "AAAC", qty: int = 2) -> int:
    return e.journal.open_trade(Position(
        ticker="AAA", direction="long", option_symbol=symbol, qty=qty, entry_price=2.50,
        entry_stock_price=103.0, range_mid=100.0, opened_at=at(10, 0, day=PREV),
    ))


def test_restart_resumes_open_position(env):
    env.add("AAA")
    trade_id = _journal_position(env)
    env.broker._positions["AAAC"] = 2
    env.session.startup()
    assert env.session.position is not None and env.session.position.qty == 2

    env.data.set_quote("AAAC", 3.00, 3.10)
    env.clock.advance(15)
    env.session.manage_position()
    assert env.session.position is None
    row = env.journal._conn.execute("SELECT reason, pnl FROM trades WHERE id=?", (trade_id,)).fetchone()
    assert row == ("take_profit", 100.0)


def test_reconcile_missing_broker_position(env):
    env.add("AAA")
    _journal_position(env)
    env.broker._positions["ZZZC"] = 3  # broker-only: warn, never managed
    env.session.startup()
    assert env.session.position is None
    assert env.trade_rows() == [("AAA", 2, 2.50, 2.50, 0.0, "reconciled_missing")]
    assert any("ZZZC" in m for _, m in env.events("warn"))


def test_exception_stops_new_entries_but_manages_exits(tmp_path):
    e = Env(tmp_path, tickers=("AAA", "BBB"))
    e.add("AAA", "long", time(9, 55))
    e.add("BBB", "long", time(10, 5))
    e.open_day()
    e.session.on_bar_close(at(9, 55))
    assert e.session.position is not None

    e.data.fail["bars_5min"] = RuntimeError("boom")
    e.session.on_bar_close(at(10, 0))  # must not raise
    assert e.session.no_new_entries
    errors = e.events("error")
    assert len(errors) == 1 and "boom" in errors[0][1] and "Traceback" in errors[0][1]

    del e.data.fail["bars_5min"]
    e.data.set_quote("AAAC", 3.00, 3.10)
    e.clock.advance(15)
    e.session.manage_position()
    assert e.trade_rows()[0][5] == "take_profit"

    e.data.set_quote("AAAC", 2.45, 2.55)
    e.session.on_bar_close(at(10, 5))
    assert ("BBB", "no_new_entries") in e.skips()
    assert sum(1 for o in e.broker.orders if o["side"] == "buy") == 1


def test_manage_exception_is_journaled(env):
    _enter_aaa(env)
    env.data.fail["latest_option_quote"] = RuntimeError("quote down")
    env.session.manage_position()  # must not raise
    assert env.session.no_new_entries
    assert env.session.position is not None
    assert len(env.events("error")) == 1


def test_affordability_filter_drops_expensive_tickers(tmp_path):
    e = Env(tmp_path, tickers=("AAA", "BBB", "CCC"))
    for tk in ("AAA", "BBB", "CCC"):
        e.add(tk)
    e.data.chains[("AAA", "call")] = [quote("AAA", "call", 5.40, 5.60)]
    e.data.chains[("AAA", "put")] = [quote("AAA", "put", 5.40, 5.60)]
    e.data.chains[("CCC", "call")] = [quote("CCC", "call", 5.40, 5.60)]  # puts still affordable
    e.session.startup()
    assert e.session.affordability_filter() == ["BBB", "CCC"]
    assert e.skips() == [("AAA", "too_expensive")]
    e.journal.close()


def test_affordability_filter_calls_only_when_shorts_disabled(tmp_path):
    e = Env(tmp_path, tickers=("CCC",))
    e.session.settings = replace(e.settings, strategy=StrategyConfig(shorts_enabled=False))
    e.add("CCC")
    e.data.chains[("CCC", "call")] = [quote("CCC", "call", 5.40, 5.60)]
    e.session.startup()
    assert e.session.affordability_filter() == []
    e.journal.close()


def test_missing_bar_skipped_after_60s(tmp_path):
    e = Env(tmp_path, tickers=("AAA", "BBB"))
    e.add("AAA", "long", time(10, 30))
    e.add("BBB", "long", time(10, 30))
    e.data.bars["BBB"] = [b for b in e.data.bars["BBB"] if b.t != at(9, 50)]
    e.open_day()
    e.session.on_bar_close(at(9, 55))

    first_tickers, first_at = e.data.bar_calls[1]  # [0] is the prior-day warm-up fetch
    assert first_tickers == ["AAA", "BBB"] and first_at == at(9, 55, 5)
    retries = e.data.bar_calls[2:]
    assert [t for t, _ in retries] == [["BBB"]] * 6
    assert [ts for _, ts in retries] == [at(9, 55, 5) + timedelta(seconds=10 * i) for i in range(1, 7)]
    assert e.skips() == [("BBB", "no_bars")]
    assert [tk for _, tk, _, _ in e.journal.check_rows()] == ["AAA"]
    e.journal.close()


def test_exit_reprices_then_goes_market(env):
    env.add("AAA")
    env.data.chains[("AAA", "call")] = [quote("AAA", "call", 1.20, 1.25)]
    env.data.set_quote("AAAC", 1.20, 1.25)
    env.open_day()
    env.session.on_bar_close(at(9, 55))
    pos = env.session.position
    assert (pos.qty, pos.entry_price) == (4, 1.23)  # limit 1.23 -> floor(500 / 123)

    env.broker.fill_policy = "partial"  # 4 -> fills 2, 2 -> fills 1, a 1-lot never fills
    env.broker.market_fill_policy = "fill"
    env.broker.market_price["AAAC"] = 1.00
    next_bids = iter([1.06, 1.04])
    real_sell_limit = env.broker.sell_limit

    def sell_limit(symbol, qty, limit):  # the bid drops after each limit attempt
        oid = real_sell_limit(symbol, qty, limit)
        bid = next(next_bids, None)
        if bid is not None:
            env.data.set_quote("AAAC", bid, bid + 0.02)
        return oid

    env.broker.sell_limit = sell_limit
    env.data.set_quote("AAAC", 1.08, 1.10)  # mid 1.09 <= 1.23 * 0.9 -> stop_loss
    env.clock.advance(15)
    start = env.clock.now()
    env.session.manage_position()

    sells = [o for o in env.broker.orders if o["side"] == "sell"]
    assert [(o["type"], o["qty"], o["limit"]) for o in sells] == [
        ("limit", 4, 1.08), ("limit", 2, 1.06), ("limit", 1, 1.04), ("market", 1, None)]
    assert env.broker.cancels[-3:] == ["o2", "o3", "o4"]
    # qty-weighted: (2 * 1.08 + 1 * 1.06 + 1 * 1.00) / 4 = 1.055
    assert env.trade_rows()[0][3:] == (1.055, -70.0, "stop_loss")
    assert env.clock.now() - start >= timedelta(seconds=30)
    assert env.broker.positions() == {}


def test_exit_with_zero_bid_goes_straight_to_market(env):
    _enter_aaa(env)
    env.broker.market_price["AAAC"] = 0.05
    env.data.set_quote("AAAC", 0.0, 0.10)
    env.clock.advance(15)
    env.session.manage_position()
    sells = [o for o in env.broker.orders if o["side"] == "sell"]
    assert [o["type"] for o in sells] == ["market"]
    assert env.trade_rows()[0][3] == 0.05


class DayData(LiveFakeData):
    """Option quote and stock price follow the clock: take profit becomes available at 10:30."""

    def latest_option_quote(self, symbol):
        if self.clock.now() >= at(10, 30):
            return 3.00, 3.10, self.clock.now()
        return 2.45, 2.55, self.clock.now()


def test_run_day_end_to_end(tmp_path):
    e = Env(tmp_path, tickers=("AAA", "BBB"), data_cls=DayData, start=at(9, 0))
    e.add("AAA", "long", time(9, 55))
    e.add("BBB", "short", time(10, 40))
    e.session.run_day()

    assert e.clock.now() == at(16, 5)
    assert e.trade_rows() == [("AAA", 2, 2.50, 3.00, 100.0, "take_profit")]
    assert ("BBB", "profit_target_hit") in e.skips()
    hb = json.loads((tmp_path / "state" / "heartbeat-bot.json").read_text())
    assert hb == {"ts": at(16, 5).isoformat(), "position": None, "day_pnl": 100.0}
    assert any("summary" in m for _, m in e.events("info"))
    # bar closes 09:55 .. 11:30 fetched once each (+1 warm-up), none after entry_end
    assert len(e.data.bar_calls) == 1 + 20
    assert e.data.bar_calls[-1][1] == at(11, 30, 5)
    e.journal.close()


def test_run_day_flattens_at_flat_time(tmp_path):
    e = Env(tmp_path, data_cls=LiveFakeData, start=at(9, 0))
    e.add("AAA")
    e.session.run_day()
    rows = e.journal._conn.execute("SELECT reason, closed_at FROM trades").fetchall()
    assert rows[0][0] == "time"
    assert datetime.fromisoformat(rows[0][1]) < at(15, 56)
    e.journal.close()


def test_run_day_restart_ignores_past_bar_closes(tmp_path):
    e = Env(tmp_path, start=at(10, 32))
    e.add("AAA")
    e.session.run_day()
    assert e.data.bar_calls[1][1] == at(10, 35, 5)  # [0] is the warm-up fetch
    e.journal.close()


def test_run_day_market_closed_returns_early(tmp_path):
    e = Env(tmp_path, start=at(9, 0))
    e.data.hours[TODAY] = None
    e.session.run_day()
    assert e.clock.now() == at(9, 25)
    assert e.broker.orders == []
    e.journal.close()


# ---------------------------------------------------------------- review round 1
def _sells(e: Env) -> list[dict]:
    return [o for o in e.broker.orders if o["side"] == "sell"]


def test_entry_cancel_is_async_and_fill_during_cancel_is_kept(env):
    env.broker.fill_policy = "never"
    env.broker.cancel_delay_reads = 3  # still "new" for 3 reads after cancel, then fills
    env.broker.fill_during_cancel = True
    pos = _enter_aaa(env)
    assert pos.qty == 2
    assert env.broker.positions() == {"AAAC": 2}
    assert env.trade_rows()[0][1] == 2


def test_entry_order_stuck_after_cancel_uses_broker_positions(env):
    env.broker.fill_policy = "never"
    env.broker.cancel_delay_reads = 10_000  # never settles on its own
    env.add("AAA")
    env.open_day()
    env.session.on_bar_close(at(9, 55))
    assert env.session.position is None
    assert any("o1" in m and "unsettled" in m for _, m in env.events("warn"))
    assert env.clock.now() >= at(9, 55, 5) + timedelta(seconds=60 + 20)

    env.broker.force_fill("o1")  # the "cancelled" buy fills after all
    env.clock.advance(15)
    env.session.manage_position()
    pos = env.session.position
    assert pos is not None and pos.qty == 2
    assert env.trade_rows()[0][1] == 2


def test_entry_status_failure_tracks_order_and_uses_broker_positions(tmp_path):
    e = Env(tmp_path, tickers=("AAA", "BBB"))
    e.add("AAA", "long", time(9, 55))
    e.add("BBB", "long", time(10, 0))
    e.broker.fill_policy = "partial"  # 1 of 2 fills
    e.broker.status_fails = True
    e.open_day()
    e.session.on_bar_close(at(9, 55))
    assert e.broker.cancels == ["o1"]  # the live order was cancelled, not abandoned
    assert e.session.position is not None and e.session.position.qty == 1
    assert any("o1" in m and "unsettled" in m for _, m in e.events("warn"))

    e.broker.status_fails = False
    e.session.on_bar_close(at(10, 0))
    assert ("BBB", "position_open") in e.skips()
    e.broker.fill_policy = "fill"
    e.data.set_quote("AAAC", 3.00, 3.10)
    e.clock.advance(15)
    e.session.manage_position()
    assert [o["qty"] for o in _sells(e)] == [1]
    assert e.trade_rows()[0][1:] == (1, 2.50, 3.00, 50.0, "take_profit")
    e.journal.close()


def test_exit_cancel_is_async_and_fill_during_cancel_is_counted(env):
    _enter_aaa(env)
    env.broker.fill_policy = "never"
    env.broker.cancel_delay_reads = 3
    env.broker.fill_during_cancel = True
    _stop_out(env, "AAA")
    assert env.session.position is None
    assert [(o["type"], o["qty"]) for o in _sells(env)] == [("limit", 2)]
    assert env.trade_rows()[0][3:] == (2.20, -60.0, "stop_loss")
    assert env.broker.positions() == {}


def test_exit_order_stuck_after_cancel_never_oversells(env):
    _enter_aaa(env)
    env.broker.fill_policy = "never"
    env.broker.cancel_delay_reads = 10_000
    _stop_out(env, "AAA")
    assert env.session.position is not None
    assert len(_sells(env)) == 1
    env.clock.advance(15)
    env.session.manage_position()
    assert len(_sells(env)) == 1  # no new sell while o2 is unsettled

    env.broker.force_fill("o2")
    env.clock.advance(15)
    env.session.manage_position()
    assert env.session.position is None
    assert len(_sells(env)) == 1
    assert env.broker.positions() == {}
    assert env.trade_rows()[0][3:] == (2.20, -60.0, "stop_loss")


def test_exit_status_failure_never_oversells(env):
    _enter_aaa(env)
    env.broker.fill_policy = "never"
    env.broker.status_fails = True
    _stop_out(env, "AAA")
    assert env.session.position is not None
    assert env.broker.cancels == ["o2"]
    env.clock.advance(15)
    env.session.manage_position()
    assert len(_sells(env)) == 1

    env.broker.status_fails = False
    env.broker.fill_policy = "fill"
    env.clock.advance(15)
    env.session.manage_position()
    assert env.session.position is None
    assert [o["qty"] for o in _sells(env)] == [2, 2]
    assert env.broker.positions() == {}


def test_restart_does_not_resignal_today(tmp_path):
    e = Env(tmp_path)
    e.add("AAA", "long", time(9, 55))
    e.open_day()
    e.session.on_bar_close(at(9, 55))
    _stop_out(e, "AAA")

    restarted = Session("bot", e.settings, e.data, e.broker, e.journal, e.approver, e.clock)
    restarted.startup()
    restarted.affordability_filter()
    restarted.on_bar_close(at(10, 0))  # AAA long still "triggers" bar after bar
    assert sum(1 for o in e.broker.orders if o["side"] == "buy") == 1
    assert len(e.approver.tickets) == 1
    e.journal.close()


def test_reconcile_qty_mismatch_corrects_journal_qty(env):
    env.add("AAA")
    _journal_position(env, qty=2)
    env.broker._positions["AAAC"] = 1
    env.session.startup()
    assert env.journal.open_trades()[0][1].qty == 1
    env.data.set_quote("AAAC", 3.00, 3.10)
    env.clock.advance(15)
    env.session.manage_position()
    assert env.trade_rows()[0][1:] == (1, 2.50, 3.00, 50.0, "take_profit")


@pytest.mark.parametrize("bid,ask,reason", [(5.40, 5.60, "too_expensive"),
                                            (2.60, 3.20, "illiquid")])
def test_reask_rechecks_contract_filters(tmp_path, bid, ask, reason):
    e = Env(tmp_path, mode="approval", approver=ScriptedApprover(
        on_request=lambda t: e.data.set_quote("AAAC", bid, ask)))
    e.add("AAA")
    e.open_day()
    e.session.on_bar_close(at(9, 55))
    assert len(e.approver.tickets) == 1
    assert ("AAA", reason) in e.skips()
    assert e.broker.orders == []
    e.journal.close()


@pytest.mark.parametrize("stale", [True, False])
def test_stale_or_empty_requote_skips(tmp_path, stale):
    def on_request(t):
        if stale:
            e.data.set_quote("AAAC", 2.45, 2.55, ts=e.clock.now() - timedelta(seconds=61))
        else:
            e.data.set_quote("AAAC", 0.0, 0.0)

    e = Env(tmp_path, mode="approval", approver=ScriptedApprover(on_request=on_request))
    e.add("AAA")
    e.open_day()
    e.session.on_bar_close(at(9, 55))
    assert ("AAA", "stale_quote") in e.skips()
    assert e.broker.orders == []
    assert e.ticket_statuses() == ["stale_quote"]
    e.journal.close()


def test_market_fill_without_avg_price_uses_last_known_price(env):
    _enter_aaa(env)
    env.broker.market_price["AAAC"] = None  # broker reports a fill with no avg price
    env.data.set_quote("AAAC", 0.0, 0.10)  # mid 0.05; zero bid -> straight to market
    env.clock.advance(15)
    env.session.manage_position()
    assert [o["type"] for o in _sells(env)] == ["market"]
    assert env.trade_rows()[0][3] == 0.05
    assert any("avg" in m for _, m in env.events("error"))


# ---------------------------------------------------------------- review round 2
def test_reviewer_repro_exit_order_settles_between_refreshes_no_oversell(env):
    _enter_aaa(env)
    env.broker.fill_policy = "never"
    env.broker.cancel_delay_reads = 10_000
    _stop_out(env, "AAA")  # o2 unsettled
    assert env.session.position is not None

    env.broker._state["o2"]["cancel_reads"] = 1  # settles on the 2nd status read from now
    env.broker.fill_during_cancel = True
    env.broker.fill_policy = "fill"
    for _ in range(3):
        env.clock.advance(15)
        env.session.manage_position()
    assert [(o["type"], o["qty"]) for o in _sells(env)] == [("limit", 2)]
    assert env.broker.positions() == {}  # never short
    assert env.session.position is None
    assert env.trade_rows()[0][3:] == (2.20, -60.0, "stop_loss")  # o2's fill was booked


def test_unsettled_exit_journals_once_not_every_cycle(env):
    _enter_aaa(env)
    env.broker.fill_policy = "never"
    env.broker.cancel_delay_reads = 10_000
    _stop_out(env, "AAA")
    for _ in range(4):
        env.clock.advance(15)
        env.session.manage_position()
    assert env.events("error") == []
    assert sum("unsettled" in m for _, m in env.events("warn")) == 1


def test_position_gone_at_broker_while_exit_unsettled_closes_from_booked_fills(env):
    _enter_aaa(env)
    env.broker.fill_policy = "never"
    env.broker.status_fails = True  # o2's status can never be read
    env.broker.cancel_delay_reads = 10_000
    _stop_out(env, "AAA")
    env.broker._positions.clear()  # ...but the broker no longer holds the contracts
    env.clock.advance(15)
    env.session.manage_position()
    assert env.session.position is None
    ticker, qty, entry, exit_price, pnl, reason = env.trade_rows()[0]
    assert (qty, reason) == (2, "stop_loss") and exit_price > 0
    assert len(_sells(env)) == 1


def test_exit_sell_capped_at_broker_held_qty_after_unsettled(env):
    _enter_aaa(env)
    env.broker.fill_policy = "never"
    env.broker.status_fails = True
    _stop_out(env, "AAA")  # o2 unsettled (cancelled; status unreadable)
    env.broker._positions["AAAC"] = 1  # something else sold one contract
    env.broker.status_fails = False
    env.broker.fill_policy = "fill"
    env.clock.advance(15)
    env.session.manage_position()
    assert [o["qty"] for o in _sells(env)] == [2, 1]
    assert env.broker.positions() == {}
    assert env.session.position is None


def test_pending_entry_settled_late_uses_real_avg_fill(env):
    env.broker.fill_policy = "never"
    env.broker.cancel_delay_reads = 10_000
    env.add("AAA")
    env.open_day()
    env.session.on_bar_close(at(9, 55))
    assert env.session.position is None
    st = env.broker._state["o1"]
    st["fill_price"] = 2.48  # real fill better than the 2.50 limit
    env.broker.force_fill("o1")
    env.clock.advance(15)
    env.session.manage_position()
    pos = env.session.position
    assert (pos.qty, pos.entry_price) == (2, 2.48)
    assert env.journal.open_trades()[0][1].entry_price == 2.48


def test_pending_entry_opened_from_positions_then_amended_on_settle(env):
    env.broker.fill_policy = "never"
    env.broker.cancel_delay_reads = 10_000
    env.add("AAA")
    env.open_day()
    env.session.on_bar_close(at(9, 55))
    env.broker._state["o1"]["fill_price"] = 2.47
    env.broker._positions["AAAC"] = 2  # broker shows the contracts before the status settles
    env.clock.advance(15)
    env.session.manage_position()
    assert env.session.position.entry_price == 2.50  # provisional: limit price
    env.broker.force_fill("o1")  # positions already counted; status now terminal
    env.broker._positions["AAAC"] = 2
    env.clock.advance(15)
    env.session.manage_position()
    assert (env.session.position.qty, env.session.position.entry_price) == (2, 2.47)
    assert env.journal.open_trades()[0][1].entry_price == 2.47


def test_reconcile_resize_logs_managed_qty(env):
    env.add("AAA")
    _journal_position(env, qty=2)
    env.broker._positions["AAAC"] = 1
    env.session.startup()
    assert any("resumed" in m and "x1" in m for _, m in env.events("info"))
