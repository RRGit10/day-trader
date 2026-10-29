"""Orders: settling async cancels and never discarding a terminal status."""
from datetime import datetime, timedelta

import pytest

from daytrader.models import ET
from daytrader.orders import Orders, UnsettledOrderError
from tests.fakes import FakeBroker, FakeClock

T0 = datetime(2026, 10, 28, 10, 0, tzinfo=ET)


def make(fill_policy="never"):
    broker, clock, events = FakeBroker(fill_policy), FakeClock(T0), []
    return Orders(broker, clock, lambda lv, m: events.append((lv, m))), broker, clock, events


def sell(o, broker, qty=2):
    return o.execute(lambda: broker.sell_limit("X", qty, 1.0), "X", "sell", qty, timedelta(seconds=10))


def test_cancel_is_polled_until_terminal():
    o, broker, clock, _ = make()
    broker.cancel_delay_reads, broker.fill_during_cancel = 3, True
    fill = sell(o, broker)
    assert fill.settled and fill.qty == 2
    assert o.unsettled == {}


def test_stuck_order_is_unsettled_with_one_warning():
    o, broker, clock, events = make()
    broker.cancel_delay_reads = 10_000
    fill = sell(o, broker)
    assert not fill.settled
    assert o.unsettled == {"o1": ("X", "sell")}
    assert clock.now() >= T0 + timedelta(seconds=30)
    for _ in range(3):
        o.refresh()
    assert [lv for lv, _ in events] == ["warn"]


def test_terminal_status_is_held_until_collected_and_blocks_new_orders():
    o, broker, _, _ = make()
    broker.cancel_delay_reads = 10_000
    sell(o, broker)
    broker.force_fill("o1")
    # the order settles inside execute()'s own refresh: it must not be discarded,
    # and no new order may be placed before the caller has booked it
    with pytest.raises(UnsettledOrderError):
        sell(o, broker)
    assert len(broker.orders) == 1
    (s,) = o.collect()
    assert (s.order_id, s.side, s.status.status, s.status.filled_qty) == ("o1", "sell", "filled", 2)
    assert o.collect() == []
    broker.fill_policy = "fill"
    assert sell(o, broker).settled


def test_status_exception_cancels_and_tracks_order():
    o, broker, _, _ = make()
    broker.status_fails = True
    fill = sell(o, broker)
    assert not fill.settled and broker.cancels == ["o1"]
    broker.status_fails = False
    o.refresh()
    (s,) = o.collect()
    assert s.status.status == "canceled"
