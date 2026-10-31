from datetime import datetime, time

from daytrader.clock import MarketHours, SystemClock, session_times
from daytrader.config import ExitConfig, StrategyConfig
from daytrader.models import ET
from fakes import FakeClock

D = (2026, 10, 28)


def hours(close_h, close_m=0):
    return MarketHours(datetime(*D, 9, 30, tzinfo=ET), datetime(*D, close_h, close_m, tzinfo=ET))


def test_normal_day():
    assert session_times(hours(16), ExitConfig(), StrategyConfig()) == (time(11, 30), time(15, 55))


def test_half_day():
    assert session_times(hours(13), ExitConfig(), StrategyConfig()) == (time(11, 30), time(12, 55))


def test_hypothetical_early_close_clamps_entry_end():
    assert session_times(hours(11), ExitConfig(), StrategyConfig()) == (time(10, 55), time(10, 55))


def test_fake_clock():
    c = FakeClock(datetime(*D, 9, 0, tzinfo=ET))
    c.sleep_until(datetime(*D, 9, 30, tzinfo=ET))
    assert c.now().minute == 30
    c.sleep_until(datetime(*D, 9, 10, tzinfo=ET))  # never goes backwards
    assert c.now().minute == 30
    c.advance(90)
    assert (c.now().minute, c.now().second) == (31, 30)


def test_system_clock_now_is_et_aware_and_past_target_returns():
    c = SystemClock()
    assert c.now().tzinfo is not None and c.now().utcoffset() == datetime.now(ET).utcoffset()
    c.sleep_until(datetime(2000, 1, 1, tzinfo=ET))
