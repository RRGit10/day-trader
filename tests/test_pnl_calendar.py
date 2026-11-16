import sqlite3
from datetime import date, datetime

from daytrader.journal import Journal
from daytrader.models import ET, Position
from daytrader.pnl_calendar import days_payload, load_days
from daytrader.report import summarize
from test_report import build


def add_trade(j, ticker, qty, entry, exit_, closed, direction="short", reason="take_profit"):
    pos = Position(ticker=ticker, direction=direction, option_symbol=f"{ticker}P",
                   qty=qty, entry_price=entry, entry_stock_price=100.0, range_mid=99.0,
                   opened_at=closed.replace(hour=10, minute=15))
    tid = j.open_trade(pos)
    j.close_trade(tid, exit_, reason, closed)


def test_groups_by_et_close_date(tmp_path):
    j = Journal(tmp_path / "j.db")
    add_trade(j, "NFLX", 1, 1.00, 1.10, datetime(2026, 11, 16, 11, 0, tzinfo=ET))
    add_trade(j, "BAC", 1, 1.00, 0.90, datetime(2026, 11, 16, 19, 30, tzinfo=ET))  # 00:30 UTC next day
    days = load_days(tmp_path / "j.db")
    assert [d.day for d in days] == [date(2026, 11, 16)]
    assert len(days[0].trades) == 2


def test_totals_and_pct(tmp_path):
    j = Journal(tmp_path / "j.db")
    add_trade(j, "NFLX", 3, 1.68, 2.03, datetime(2026, 11, 16, 12, 57, tzinfo=ET))
    (d,) = load_days(tmp_path / "j.db")
    assert d.pnl == 105.0
    assert d.cost == 504.0
    assert d.pct == 20.83
    assert (d.wins, d.losses) == (1, 0)


def test_matches_report_daily_totals(tmp_path):
    j = build(tmp_path)
    by_day = {d.day: d.pnl for d in load_days(tmp_path / "j.db")}
    assert by_day[date(2026, 10, 1)] == -80.0
    assert min(by_day.values()) == summarize(j).worst_day
    assert round(sum(by_day.values()), 2) == summarize(j).total_pnl


def test_break_even_day(tmp_path):
    j = Journal(tmp_path / "j.db")
    add_trade(j, "AAPL", 1, 1.00, 1.00, datetime(2026, 11, 3, 11, 0, tzinfo=ET))
    (d,) = load_days(tmp_path / "j.db")
    assert d.pnl == 0.0 and d.pct == 0.0
    assert (d.wins, d.losses) == (0, 0)


def test_open_trade_excluded(tmp_path):
    j = Journal(tmp_path / "j.db")
    j.open_trade(Position(ticker="SPY", direction="long", option_symbol="SPYC", qty=1,
                          entry_price=1.0, entry_stock_price=100.0, range_mid=99.0,
                          opened_at=datetime(2026, 11, 16, 10, 0, tzinfo=ET)))
    assert load_days(tmp_path / "j.db") == []


def test_missing_journal_not_created(tmp_path):
    path = tmp_path / "nope.db"
    assert load_days(path) == []
    assert not path.exists()


def test_reads_while_writer_holds_transaction(tmp_path):
    j = Journal(tmp_path / "j.db")
    add_trade(j, "NFLX", 3, 1.68, 2.03, datetime(2026, 11, 16, 12, 57, tzinfo=ET))
    writer = sqlite3.connect(str(tmp_path / "j.db"))
    writer.execute("BEGIN")
    writer.execute("INSERT INTO events (ts, level, message) VALUES ('x', 'info', 'y')")
    try:
        assert [d.pnl for d in load_days(tmp_path / "j.db")] == [105.0]
    finally:
        writer.rollback()
        writer.close()


def test_days_payload_shape(tmp_path):
    j = Journal(tmp_path / "j.db")
    add_trade(j, "NFLX", 3, 1.68, 2.03, datetime(2026, 11, 16, 12, 57, 19, tzinfo=ET))
    payload = days_payload(load_days(tmp_path / "j.db"), date(2026, 11, 16))
    assert payload["today"] == "2026-11-16"
    (day,) = payload["days"]
    assert day == {
        "date": "2026-11-16", "pnl": 105.0, "cost": 504.0, "pct": 20.83,
        "wins": 1, "losses": 0,
        "trades": [{"ticker": "NFLX", "direction": "short", "qty": 3, "entry_price": 1.68,
                    "exit_price": 2.03, "pnl": 105.0, "reason": "take_profit",
                    "opened_at": "10:15", "closed_at": "12:57"}],
    }
