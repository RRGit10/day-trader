from datetime import date, datetime, time

from daytrader.daylog import format_day_log
from daytrader.journal import Journal
from daytrader.models import ET, Check, OrbResult, Position

DAY = date(2026, 11, 2)


def at(h, m, s=0):
    return datetime(2026, 11, 2, h, m, s, tzinfo=ET)


def checks(failed=(), ema="close 282.15 > ema9 281.28", rsi="rsi 64.2 > 60.0"):
    details = {"ema": ema, "rsi": rsi}
    return tuple(Check(n, n not in failed, details.get(n, ""))
                 for n in ("range", "window", "breakout", "ema", "vwap", "rsi"))


def ticket(tid, created, ticker, direction, symbol, qty, limit):
    return {"id": tid, "created_at": created.isoformat(), "ticker": ticker,
            "direction": direction, "option_symbol": symbol, "strike": 1.0,
            "expiry": "2026-11-06", "delta": 0.65, "bid": limit - 0.01, "ask": limit + 0.01,
            "limit": limit, "qty": qty, "cost": round(limit * qty * 100, 2),
            "take_profit_price": 1.0, "stop_loss_price": 1.0, "stock_stop": 1.0,
            "stock_target": 1.0, "flat_time": "15:55:00", "checks": []}


def build(tmp_path):
    j = Journal(tmp_path / "j.db")
    j.record_skip(at(9, 40), "AAPL", "too_expensive", "call: too_expensive; put: illiquid")
    j.record_skip(at(9, 40), "MSFT", "illiquid", "call: illiquid; put: illiquid")
    # SOFI ticket expired
    j.record_check(at(9, 55, 5), OrbResult("SOFI", True, "long", checks(), 1, 1, None, None))
    j.record_ticket("sofi0001xx", at(9, 55, 7), ticket("sofi0001xx", at(9, 55, 7), "SOFI", "long",
                                                         "SOFI261106C00017000", 7, 0.67), "expired")
    j.record_skip(at(9, 57, 8), "SOFI", "expired", "sofi0001xx")
    # IWM approved and filled, closed at take profit
    j.record_check(at(10, 0, 5), OrbResult("IWM", False, "long", checks(failed=("ema",)), 1, 1, None, None))
    j.record_check(at(10, 5, 5), OrbResult("IWM", True, "long", checks(), 1, 1, None, None))
    j.record_ticket("iwm00002xx", at(10, 5, 6), ticket("iwm00002xx", at(10, 5, 6), "IWM", "long",
                                                        "IWM261103C00281000", 2, 2.12), "approved")
    tid = j.open_trade(Position("IWM", "long", "IWM261103C00281000", 2, 2.11, 282.15, 281.29,
                                at(10, 5, 46)))
    j.close_trade(tid, 2.49, "take_profit", at(10, 12, 32))
    # INTC approved but not filled
    short = checks(ema="close 63.26 < ema9 63.72", rsi="rsi 38.0 < 40.0")
    j.record_check(at(11, 0, 5), OrbResult("INTC", False, "short",
                                           checks(failed=("rsi",), rsi="rsi 42.5 < 40.0"), 1, 1, None, None))
    j.record_check(at(11, 10, 5), OrbResult("INTC", True, "short", short, 1, 1, None, None))
    j.record_ticket("intc0003xx", at(11, 10, 6), ticket("intc0003xx", at(11, 10, 6), "INTC", "short",
                                                         "INTC261106P00065000", 1, 3.20), "approved")
    j.record_skip(at(11, 11, 56), "INTC", "not_filled", "INTC261106P00065000 @ 3.20")
    # another day, ignored
    j.record_skip(datetime(2026, 11, 3, 10, 0, tzinfo=ET), "SPY", "illiquid", "")
    return j


def test_day_log_trades_tickets_skips_and_near_misses(tmp_path):
    j = build(tmp_path)
    out = format_day_log(j, DAY, "approval", time(9, 55))
    assert out.startswith("# Trade journal: 2026-11-02 (approval)\n")
    assert "Day P&L: +$76.00 from 1 closed trade (1 win, 0 losses) · 3 tickets" in out
    assert "### 1. IWM long · +$76.00 · take_profit" in out
    assert "- Contract: 2× IWM261103C00281000" in out
    assert "- Entry: 10:05:46 ET at $2.11 (stock $282.15)" in out
    assert "- Exit: 10:12:32 ET at $2.49, after 6m 46s" in out
    assert "- Return: +18.0% on $422.00" in out
    assert ("- Rules at entry: ✓ ORB Breakout · ✓ 2 Candle Rule · ✓ 9 EMA Price Above · "
            "✓ VWAP Price Above · ✓ RSI Above 60") in out
    assert "| 09:55:07 | SOFI long | sofi0001 | 7× SOFI261106C00017000 | $0.67 | $469.00 | expired |" in out
    assert "| 10:05:06 | IWM long | iwm00002 | 2× IWM261103C00281000 | $2.12 | $424.00 | approved, filled |" in out
    assert "| 11:10:06 | INTC short | intc0003 | 1× INTC261106P00065000 | $3.20 | $320.00 | approved, not filled |" in out
    assert "Ruled out before the entry window: 2 (illiquid 1, too_expensive 1)" in out
    assert "| 11:11:56 | INTC | not_filled | INTC261106P00065000 @ 3.20 |" in out
    assert "SPY" not in out
    assert "Near-misses: 2 (EMA 1, RSI 1)" in out


def test_day_log_open_trade_and_empty_day(tmp_path):
    j = Journal(tmp_path / "j.db")
    assert format_day_log(j, DAY, "bot", time(9, 55)) == (
        "# Trade journal: 2026-11-02 (bot)\n\n"
        "Day P&L: $0.00 from 0 closed trades (0 wins, 0 losses) · 0 tickets\n\n"
        "## Trades\n\nNone.\n\n"
        "## Skipped signals\n\nNone.\n\n"
        "## Near-misses\n\nNone.\n"
    )
    j.open_trade(Position("SPY", "short", "SPY261106P00500000", 1, 3.0, 500.0, 501.0, at(10, 30)))
    out = format_day_log(j, DAY, "bot", time(9, 55))
    assert "### 1. SPY short · open" in out
    assert "- Exit: still open" in out
    assert "- Rules at entry: not recorded" in out
    assert "Day P&L: $0.00 from 0 closed trades" in out


def test_overnight_trade_counts_on_the_day_it_closed(tmp_path):
    # matches `report` and `status`, which attribute P&L by closing date
    j = Journal(tmp_path / "j.db")
    tid = j.open_trade(Position("SPY", "long", "SPY261106C00500000", 1, 2.0, 500.0, 499.0,
                                datetime(2026, 11, 2, 15, 0, tzinfo=ET)))
    j.close_trade(tid, 2.5, "time", datetime(2026, 11, 3, 9, 35, tzinfo=ET))
    opened_day = format_day_log(j, DAY, "bot", time(9, 55))
    closed_day = format_day_log(j, date(2026, 11, 3), "bot", time(9, 55))
    assert "Day P&L: $0.00 from 0 closed trades" in opened_day
    assert "Day P&L: +$50.00 from 1 closed trade (1 win, 0 losses)" in closed_day
    assert "### 1. SPY long · +$50.00 · time" in opened_day
    assert "### 1. SPY long · +$50.00 · time" in closed_day
