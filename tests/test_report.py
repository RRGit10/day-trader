from datetime import date, datetime

from daytrader.journal import Journal
from daytrader.models import ET, Check, OrbResult, Position
from daytrader.report import Summary, format_summary, summarize

CAVEAT1 = "Past results don't predict future results."
CAVEAT2 = (
    "Assumed spreads and bar-level fills are approximations; backtest VWAP uses "
    "full-market (SIP) data while live trading uses IEX, so signals can differ slightly."
)


def add_trade(j, ticker, entry, exit_, closed):
    pos = Position(ticker=ticker, direction="long", option_symbol=f"{ticker}C",
                   qty=1, entry_price=entry, entry_stock_price=100.0, range_mid=99.0,
                   opened_at=closed.replace(hour=10, minute=0))
    tid = j.open_trade(pos)
    j.close_trade(tid, exit_, "x", closed)


def add_check(j, ts, ticker, triggered, **passed):
    checks = tuple(Check(n, p, "") for n, p in passed.items())
    j.record_check(ts, OrbResult(ticker=ticker, triggered=triggered, direction="long",
                                 checks=checks, range_high=1.0, range_low=0.5,
                                 trigger_time=None, trigger_close=None))


def build(tmp_path):
    j = Journal(tmp_path / "j.db")
    # pnl = (exit - entry) * 100
    add_trade(j, "AAPL", 1.00, 2.00, datetime(2026, 9, 30, 11, 0, tzinfo=ET))   # +100
    add_trade(j, "MSFT", 1.00, 0.50, datetime(2026, 10, 1, 11, 0, tzinfo=ET))   # -50
    add_trade(j, "MSFT", 1.00, 0.70, datetime(2026, 10, 1, 12, 0, tzinfo=ET))   # -30
    add_trade(j, "AAPL", 1.00, 1.00, datetime(2026, 10, 2, 11, 0, tzinfo=ET))   # 0
    add_trade(j, "AAPL", 1.00, 0.90, datetime(2026, 10, 5, 11, 0, tzinfo=ET))   # -10
    add_trade(j, "AAPL", 1.00, 1.50, datetime(2026, 10, 6, 11, 0, tzinfo=ET))   # +50
    return j


def test_summarize_stats(tmp_path):
    s = summarize(build(tmp_path))
    assert s.trades == 6
    assert s.wins == 2
    assert s.losses == 3
    assert s.win_rate == 2 / 6
    assert s.avg_win == 75.0
    assert s.avg_loss == -30.0
    assert s.total_pnl == 60.0
    assert s.worst_day == -80.0
    assert s.longest_losing_streak == 2
    assert s.pnl_by_ticker == {"AAPL": 140.0, "MSFT": -80.0}
    assert s.pnl_by_month == {"2026-09": 100.0, "2026-10": -40.0}


def test_date_filter_inclusive(tmp_path):
    s = summarize(build(tmp_path), start=date(2026, 10, 1), end=date(2026, 10, 2))
    assert s.trades == 3
    assert s.total_pnl == -80.0
    assert s.longest_losing_streak == 2


def test_blocked_by_and_skips(tmp_path):
    j = Journal(tmp_path / "j.db")
    t = datetime(2026, 10, 1, 10, 0, tzinfo=ET)
    add_check(j, t, "AAPL", False, breakout=True, ema=False, vwap=False, rsi=True)
    add_check(j, t, "AAPL", False, breakout=True, ema=True, vwap=False, rsi=False)
    add_check(j, t, "AAPL", False, breakout=False, ema=False, vwap=False, rsi=False)
    add_check(j, t, "AAPL", True, breakout=True, ema=True, vwap=True, rsi=True)
    j.record_skip(t, "AAPL", "spread")
    j.record_skip(t, "MSFT", "spread")
    j.record_skip(t, "MSFT", "oi")
    j.record_skip(datetime(2026, 10, 9, 10, 0, tzinfo=ET), "MSFT", "oi")
    s = summarize(j, end=date(2026, 10, 1))
    assert s.blocked_by == {"ema": 1, "vwap": 2, "rsi": 1}
    assert s.skip_reasons == {"spread": 2, "oi": 1}


def test_empty_journal(tmp_path):
    s = summarize(Journal(tmp_path / "j.db"))
    assert (s.trades, s.wins, s.losses, s.win_rate) == (0, 0, 0, 0.0)
    assert (s.avg_win, s.avg_loss, s.total_pnl, s.worst_day) == (0.0, 0.0, 0.0, 0.0)
    assert s.longest_losing_streak == 0
    assert s.pnl_by_ticker == {} and s.pnl_by_month == {}
    assert s.blocked_by == {} and s.skip_reasons == {}
    assert "Trades: 0" in format_summary(s, "live")


def test_format_sorted_and_caveats(tmp_path):
    s = summarize(build(tmp_path))
    live = format_summary(s, "live")
    assert CAVEAT1 not in live and CAVEAT2 not in live
    assert live.index("AAPL") < live.index("MSFT")
    assert live.index("2026-09") < live.index("2026-10")
    bt = format_summary(s, "backtest")
    assert CAVEAT1 in bt.splitlines() and CAVEAT2 in bt.splitlines()
    assert bt.splitlines()[-2:] == [CAVEAT1, CAVEAT2]


def test_format_blocked_sorted_by_count():
    s = Summary(0, 0, 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0, {}, {}, {"ema": 1, "vwap": 5}, {"oi": 2, "spread": 9})
    out = format_summary(s, "live")
    assert out.index("vwap") < out.index("ema")
    assert out.index("spread") < out.index("oi")
