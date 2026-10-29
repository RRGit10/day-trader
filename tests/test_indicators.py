from datetime import datetime

import pytest

from daytrader.indicators import ema, rsi, session_vwap
from daytrader.models import ET, Bar, Position


def _bar(price, v):
    return Bar(datetime(2026, 10, 28, 9, 30, tzinfo=ET), price, price, price, price, v)


def test_ema_linear():
    out = ema(list(range(1, 11)), 3)
    assert out[:2] == [None, None]
    assert out[2:] == pytest.approx([2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0])


def test_rsi_hand_computed():
    out = rsi([10, 11, 10.5, 11.5, 12], 2)
    assert out[:2] == [None, None]
    assert out[2:] == pytest.approx([66.667, 85.714, 90.909], abs=1e-3)


def test_rsi_all_up_is_100():
    assert rsi([1, 2, 3, 4, 5], 2)[2:] == [100.0, 100.0, 100.0]


def test_rsi_all_down_is_0():
    assert rsi([5, 4, 3, 2, 1], 2)[2:] == pytest.approx([0.0, 0.0, 0.0])


def test_vwap():
    assert session_vwap([_bar(10, 100), _bar(20, 300)]) == pytest.approx([10.0, 17.5])
    assert session_vwap([_bar(10, 0), _bar(20, 300)]) == pytest.approx([10.0, 20.0])


def _pos(direction, entry_stock):
    return Position("AAPL", direction, "AAPL261030C00100000", 2, 1.5, entry_stock, 99.0,
                    datetime(2026, 10, 28, 10, 0, tzinfo=ET))


def test_position_levels():
    p = _pos("long", 101.0)
    assert p.risk_per_share == 2.0
    assert p.stock_target == 103.0
    assert p.cost == pytest.approx(300.0)
    s = _pos("short", 97.0)
    assert s.risk_per_share == 2.0
    assert s.stock_target == 95.0
