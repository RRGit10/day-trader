from datetime import datetime, time

import pytest

from daytrader.config import ExitConfig
from daytrader.exits import check_exit
from daytrader.models import ET, Position

CFG = ExitConfig()
FLAT = time(15, 55)
NOW = datetime(2026, 10, 28, 11, 0, tzinfo=ET)


def pos(direction="long", entry=1.00, stock=100.0, mid=99.0):
    return Position(
        ticker="AAPL", direction=direction, option_symbol="AAPL261030C00100000",
        qty=2, entry_price=entry, entry_stock_price=stock, range_mid=mid,
        opened_at=NOW,
    )


def run(p, mid, stock, now=NOW, flat=FLAT):
    return check_exit(p, mid, stock, now, flat, CFG)


# long: range_mid 99, entry stock 100 -> target 101
def test_no_exit():
    assert run(pos(), 1.05, 100.2) is None


def test_stop_loss_long():
    assert run(pos(), 0.85, 100.2) == "stop_loss"


def test_stop_loss_short():
    assert run(pos("short", stock=100.0, mid=101.0), 0.85, 99.8) == "stop_loss"


def test_stop_loss_boundary_inclusive():
    assert run(pos(entry=1.15), 1.15 * 0.9, 100.2) == "stop_loss"
    assert run(pos(), 0.90, 100.2) == "stop_loss"


def test_stock_stop_long():
    assert run(pos(), 1.0, 99.0) == "stock_stop"
    assert run(pos(), 1.0, 98.5) == "stock_stop"


def test_stock_stop_short():
    p = pos("short", stock=100.0, mid=101.0)
    assert run(p, 1.0, 101.0) == "stock_stop"
    assert run(p, 1.0, 101.5) == "stock_stop"


def test_take_profit_long_and_short():
    assert run(pos(), 1.30, 100.2) == "take_profit"
    assert run(pos("short", stock=100.0, mid=101.0), 1.30, 99.8) == "take_profit"


@pytest.mark.parametrize("entry", [1.0, 1.15, 2.35, 3.10])
def test_take_profit_boundary_exact(entry):
    assert run(pos(entry=entry), entry * 1.2, 100.2) == "take_profit"


def test_stock_target_long():
    assert run(pos(), 1.05, 101.0) == "stock_target"


def test_stock_target_short():
    p = pos("short", stock=100.0, mid=101.0)
    assert run(p, 1.05, 99.0) == "stock_target"


def test_stop_beats_target():
    # stop_loss and take_profit-style stock target both true
    assert run(pos(), 0.5, 101.5) == "stop_loss"


def test_stock_stop_beats_take_profit():
    assert run(pos(), 1.5, 98.0) == "stock_stop"


def test_take_profit_beats_stock_target():
    assert run(pos(), 1.5, 101.5) == "take_profit"


def test_option_mid_none_skips_option_checks():
    assert run(pos(), None, 100.2) is None


def test_option_mid_none_stock_stop_still_fires():
    assert run(pos(), None, 98.0) == "stock_stop"


def test_option_mid_none_stock_target_still_fires():
    assert run(pos(), None, 101.5) == "stock_target"


def test_time_exit_1555():
    now = datetime(2026, 10, 28, 15, 55, tzinfo=ET)
    assert run(pos(), 1.05, 100.2, now=now) == "time"


def test_time_before_flat_no_exit():
    now = datetime(2026, 10, 28, 15, 54, tzinfo=ET)
    assert run(pos(), 1.05, 100.2, now=now) is None


def test_time_exit_half_day():
    now = datetime(2026, 11, 27, 12, 55, tzinfo=ET)
    assert run(pos(), 1.05, 100.2, now=now, flat=time(12, 55)) == "time"


def test_uses_flat_time_param_not_cfg():
    now = datetime(2026, 11, 27, 13, 0, tzinfo=ET)
    assert run(pos(), 1.05, 100.2, now=now, flat=time(12, 55)) == "time"
    assert run(pos(), 1.05, 100.2, now=now, flat=time(15, 55)) is None
