from datetime import date, datetime

from daytrader.journal import Journal
from daytrader.models import ET, Check, OrbResult, Position


def make_pos(opened=None, ticker="AAPL"):
    return Position(
        ticker=ticker,
        direction="long",
        option_symbol=f"{ticker}261120C00200000",
        qty=2,
        entry_price=2.50,
        entry_stock_price=200.0,
        range_mid=198.0,
        opened_at=opened or datetime(2026, 10, 28, 10, 5, tzinfo=ET),
    )


def test_open_close_roundtrip_pnl(tmp_path):
    j = Journal(tmp_path / "sub" / "j.db")
    tid = j.open_trade(make_pos())
    closed = j.close_trade(tid, 3.00, "take_profit", datetime(2026, 10, 28, 10, 30, tzinfo=ET))
    assert closed.pnl == 100.0
    assert closed.ticker == "AAPL"
    assert closed.reason == "take_profit"
    assert closed.closed_at == datetime(2026, 10, 28, 10, 30, tzinfo=ET)
    assert j.all_trades() == [closed]
    j.close()


def test_pnl_rounded_to_cents(tmp_path):
    j = Journal(tmp_path / "j.db")
    tid = j.open_trade(make_pos())
    closed = j.close_trade(tid, 2.333, "time", datetime(2026, 10, 28, 15, 55, tzinfo=ET))
    assert closed.pnl == round((2.333 - 2.50) * 2 * 100, 2)


def test_open_trades_only_unclosed_identical_position(tmp_path):
    j = Journal(tmp_path / "j.db")
    p1, p2 = make_pos(ticker="AAPL"), make_pos(ticker="MSFT")
    t1 = j.open_trade(p1)
    t2 = j.open_trade(p2)
    j.close_trade(t1, 2.0, "stop_loss", datetime(2026, 10, 28, 11, 0, tzinfo=ET))
    assert j.open_trades() == [(t2, p2)]


def test_trades_on_filters_by_et_date(tmp_path):
    j = Journal(tmp_path / "j.db")
    ids = [j.open_trade(make_pos()) for _ in range(3)]
    j.close_trade(ids[0], 3.0, "take_profit", datetime(2026, 10, 28, 10, 30, tzinfo=ET))
    j.close_trade(ids[1], 3.0, "take_profit", datetime(2026, 10, 29, 10, 30, tzinfo=ET))
    # 23:30 ET on the 28th is already the 29th in UTC; ET date must win
    late = datetime(2026, 10, 28, 23, 30, tzinfo=ET)
    j.close_trade(ids[2], 3.0, "time", late)
    assert len(j.trades_on(date(2026, 10, 28))) == 2
    assert len(j.trades_on(date(2026, 10, 29))) == 1
    assert len(j.all_trades()) == 3


def test_checks_roundtrip(tmp_path):
    j = Journal(tmp_path / "j.db")
    res = OrbResult(
        ticker="AAPL", triggered=True, direction="long",
        checks=(Check("breakout", True, "close 201 > 200"), Check("rsi", False, "55")),
        range_high=200.0, range_low=196.0,
        trigger_time=None, trigger_close=None,
    )
    ts = datetime(2026, 10, 28, 10, 0, tzinfo=ET)
    j.record_check(ts, res)
    rows = j.check_rows()
    assert rows == [(ts, "AAPL", True, [
        {"name": "breakout", "passed": True, "detail": "close 201 > 200"},
        {"name": "rsi", "passed": False, "detail": "55"},
    ])]


def test_skips_tickets_events_and_reopen(tmp_path):
    path = tmp_path / "j.db"
    ts = datetime(2026, 10, 28, 9, 40, tzinfo=ET)
    j = Journal(path)
    j.record_skip(ts, "AAPL", "spread", "6%")
    j.record_ticket("t1", ts, {"a": 1}, "pending")
    j.update_ticket("t1", "approved")
    j.record_event(ts, "INFO", "hello")
    tid = j.open_trade(make_pos())
    j.close()

    j2 = Journal(path)
    assert j2.skip_rows() == [(ts, "AAPL", "spread")]
    assert [t for t, _ in j2.open_trades()] == [tid]
    status = j2._conn.execute("SELECT status FROM tickets WHERE id='t1'").fetchone()[0]
    assert status == "approved"
    assert j2._conn.execute("SELECT message FROM events").fetchone()[0] == "hello"
    j2.close()


def test_signaled_on_includes_trades_and_tickets_opened_that_day(tmp_path):
    j = Journal(tmp_path / "j.db")
    day = date(2026, 10, 28)
    j.open_trade(make_pos(ticker="AAPL"))  # still open, opened today
    tid = j.open_trade(make_pos(ticker="MSFT"))
    j.close_trade(tid, 2.0, "stop_loss", datetime(2026, 10, 28, 10, 30, tzinfo=ET))
    j.open_trade(make_pos(opened=datetime(2026, 10, 27, 10, 5, tzinfo=ET), ticker="OLD"))
    j.record_ticket("t1", datetime(2026, 10, 28, 9, 55, tzinfo=ET),
                    {"ticker": "SPY", "direction": "short"}, "rejected")
    j.record_ticket("t2", datetime(2026, 10, 27, 9, 55, tzinfo=ET),
                    {"ticker": "QQQ", "direction": "long"}, "approved")
    assert j.signaled_on(day) == {("AAPL", "long"), ("MSFT", "long"), ("SPY", "short")}
    j.close()


def test_amend_open_trade_changes_qty_price_and_pnl(tmp_path):
    j = Journal(tmp_path / "j.db")
    tid = j.open_trade(make_pos())
    j.amend_open_trade(tid, 1, 2.40)
    pos = j.open_trades()[0][1]
    assert (pos.qty, pos.entry_price) == (1, 2.40)
    closed = j.close_trade(tid, 3.00, "take_profit", datetime(2026, 10, 28, 10, 30, tzinfo=ET))
    assert closed.pnl == 60.0
    try:
        j.amend_open_trade(tid, 2, 2.50)  # closed trades are immutable
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError")
    j.close()
