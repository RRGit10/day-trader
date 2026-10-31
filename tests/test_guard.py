from datetime import datetime

import pytest

from daytrader.config import RiskConfig
from daytrader.guard import may_enter
from daytrader.models import ET, ClosedTrade

CFG = RiskConfig()
T = datetime(2026, 10, 28, 11, 0, tzinfo=ET)


def tr(pnl):
    return ClosedTrade("AAPL", pnl, T, "x")


def test_no_trades_allowed():
    assert may_enter([], False, CFG) == (True, None)


def test_profit_target_hit():
    assert may_enter([tr(100)], False, CFG) == (False, "profit_target_hit")


def test_just_under_target_allowed():
    assert may_enter([tr(99.99)], False, CFG) == (True, None)


def test_two_losses_max_losses():
    assert may_enter([tr(-30), tr(-30)], False, CFG) == (False, "max_losses")


def test_one_big_loss_limit():
    assert may_enter([tr(-100)], False, CFG) == (False, "loss_limit")


def test_mixed_allowed():
    assert may_enter([tr(60), tr(-20)], False, CFG) == (True, None)


def test_position_open_first():
    assert may_enter([tr(100), tr(-30), tr(-30)], True, CFG) == (False, "position_open")


def test_max_losses_before_loss_limit():
    assert may_enter([tr(-60), tr(-60)], False, CFG) == (False, "max_losses")


def test_breakeven_not_a_loss():
    assert may_enter([tr(0), tr(-10)], False, CFG) == (True, None)


@pytest.mark.parametrize("pnls", [(79.13, 5.82, 15.05),  # 99.99999999999999 on Python 3.11
                                  (4.14, 0.29, 95.57)])  # below 100 even with 3.12+'s sum()
def test_float_sum_rounded_to_cents(pnls):
    assert may_enter([tr(p) for p in pnls], False, CFG) == (False, "profit_target_hit")
