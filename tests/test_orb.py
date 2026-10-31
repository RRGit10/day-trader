from datetime import datetime, time, timedelta

import pytest

from daytrader.config import StrategyConfig
from daytrader.models import ET, Bar
from daytrader.orb import evaluate_orb

DAY = datetime(2026, 10, 27, 9, 30, tzinfo=ET)
CFG = StrategyConfig()
NAMES = ["range", "window", "breakout", "ema", "vwap", "rsi"]


def make_day(closes, start=DAY, vols=None):
    return [
        Bar(start + timedelta(minutes=5 * i), c, c + 0.1, c - 0.1, c,
            vols[i] if vols else 1000)
        for i, c in enumerate(closes)
    ]


def flat_prior(price=100.0, n=40):
    return make_day([price] * n, start=DAY - timedelta(days=1))


def failed(result):
    return [c.name for c in result.checks if not c.passed]


def test_long_trigger_all_pass():
    day = make_day([100, 100, 100, 101.0, 101.5])
    r = evaluate_orb("AAPL", flat_prior(), day, CFG)
    assert r.triggered, r.checks
    assert [c.name for c in r.checks] == NAMES
    assert r.direction == "long"
    assert r.trigger_time == DAY + timedelta(minutes=20)  # 09:50 bar
    assert r.trigger_close == 101.5
    assert r.range_high == pytest.approx(100.1)
    assert r.range_low == pytest.approx(99.9)
    assert r.range_mid == pytest.approx(100.0)


def test_short_trigger_all_pass():
    day = make_day([100, 100, 100, 99.0, 98.5])
    r = evaluate_orb("AAPL", flat_prior(), day, CFG)
    assert r.triggered, r.checks
    assert r.direction == "short"
    assert r.trigger_time == DAY + timedelta(minutes=20)
    assert r.trigger_close == 98.5
    assert r.range_mid == pytest.approx(100.0)


def test_one_close_above_is_not_enough():
    day = make_day([100, 100, 100, 100.0, 101.5])
    r = evaluate_orb("AAPL", flat_prior(), day, CFG)
    assert not r.triggered
    assert r.direction is None
    assert failed(r) == ["breakout", "ema", "vwap", "rsi"]
    assert r.checks[3].detail == "no breakout"
    assert r.trigger_time is None and r.trigger_close is None


FILTER_CASES = {
    "ema": (
        flat_prior,
        [100, 100, 100] + [100 + i for i in range(1, 13)] + [116, 109.5],
        None,
    ),
    "vwap": (
        flat_prior,
        [100, 100, 100, 101, 105, 104, 104.5],
        [1000, 1000, 1000, 1000, 50000, 1000, 1000],
    ),
    "rsi": (
        lambda: make_day([120 - 0.5 * i for i in range(40)], start=DAY - timedelta(days=1)),
        [100, 100, 100, 101, 101.5],
        None,
    ),
}


@pytest.mark.parametrize("which", ["ema", "vwap", "rsi"])
def test_each_filter_fails(which):
    prior_fn, closes, vols = FILTER_CASES[which]
    # trigger bar must close inside the entry window: 15 closes end 10:45, fine
    r = evaluate_orb("AAPL", prior_fn(), make_day(closes, vols=vols), CFG)
    assert not r.triggered
    assert r.direction == "long"
    assert failed(r) == [which], [(c.name, c.passed, c.detail) for c in r.checks]
    assert r.trigger_time is None


def test_incomplete_range():
    day = make_day([100, 100, 100, 101.0, 101.5])
    del day[1]  # drop the 09:35 bar
    r = evaluate_orb("AAPL", flat_prior(), day, CFG)
    assert not r.triggered
    assert [c.name for c in r.checks] == NAMES
    rng = r.checks[0]
    assert not rng.passed
    assert rng.detail == "incomplete range (2/3 bars)"
    assert r.range_high is None and r.range_low is None and r.range_mid is None
    assert r.checks[3].detail == "incomplete range"


@pytest.mark.parametrize(
    "n_bars,end,expected",
    [
        (5, None, True),    # trigger bar closes 09:55
        (4, None, False),   # closes 09:50, before entry_start
        (12, None, True),   # trigger bar closes 10:25
    ],
)
def test_window_basic(n_bars, end, expected):
    closes = [100, 100, 100] + [101 + 0.5 * i for i in range(n_bars - 3)]
    r = evaluate_orb("AAPL", flat_prior(), make_day(closes), CFG, end)
    assert r.checks[1].passed is expected


def _long_day(n_bars):
    return make_day([100, 100, 100] + [101 + 0.5 * i for i in range(n_bars - 3)])


def test_window_edges():
    # trigger bar closing at 11:30 passes (last bar starts 11:25 -> 24 bars)
    day = _long_day(24)
    assert day[-1].t.time() == time(11, 25)
    r = evaluate_orb("AAPL", flat_prior(), day, CFG)
    assert r.checks[1].passed
    # 11:35 close fails
    day = _long_day(25)
    r = evaluate_orb("AAPL", flat_prior(), day, CFG)
    assert not r.checks[1].passed
    assert not r.triggered
    # override honored
    r = evaluate_orb("AAPL", flat_prior(), day, CFG, entry_end=time(12, 50))
    assert r.checks[1].passed
    assert r.triggered, r.checks


def test_shorts_disabled():
    cfg = StrategyConfig(shorts_enabled=False)
    day = make_day([100, 100, 100, 99.0, 98.5])
    r = evaluate_orb("AAPL", flat_prior(), day, cfg)
    assert not r.triggered
    assert r.direction is None
    breakout = r.checks[2]
    assert not breakout.passed and breakout.detail == "shorts disabled"
