"""Broker state the journal does not know about: open orders and unknown positions.

Each mode trades its own dedicated paper account, so at startup every open order is
cancelled, and every broker position that is not an open journal trade is an orphan
(e.g. from a crash between a fill and its journal write). Orphans are not managed;
they are sold at market at flat time (controller ruling 9).
"""
from __future__ import annotations

from datetime import timedelta
from typing import Callable

from .broker import Broker
from .clock import Clock
from .orders import Orders, Settled

CANCEL_ALL_WAIT = timedelta(seconds=20)
CANCEL_ALL_POLL = timedelta(seconds=2)
ORPHAN_SELL_TIMEOUT = timedelta(seconds=60)


def cancel_open_orders(broker: Broker, clock: Clock, event: Callable[[str, str], None]) -> None:
    """Cancel every open order and wait until none is open; raise if they stay open."""
    open_ids = broker.open_orders()
    if not open_ids:
        return
    event("warn", f"cancelling {len(open_ids)} open order(s) found at startup: "
                  f"{', '.join(open_ids)}")
    broker.cancel_all_open()
    deadline = clock.now() + CANCEL_ALL_WAIT
    while broker.open_orders():
        if clock.now() >= deadline:
            raise RuntimeError(f"open orders still open {CANCEL_ALL_WAIT.seconds}s after cancel-all")
        clock.sleep_until(min(clock.now() + CANCEL_ALL_POLL, deadline))


class Orphans:
    def __init__(self, broker: Broker, orders: Orders, event: Callable[[str, str], None]) -> None:
        self.broker = broker
        self.orders = orders
        self._event = event
        self.held: dict[str, int] = {}  # symbol -> qty still to sell
        self._oids: dict[str, str] = {}  # unsettled sell order id -> symbol

    def add(self, symbol: str, qty: int, flat_label: str) -> None:
        if qty <= 0:
            self._event("warn", f"broker position {symbol} x{qty} not in journal: "
                                "not a long position, left alone")
            return
        self.held[symbol] = qty
        self._event("warn", f"broker position {symbol} x{qty} not in journal: not managed, "
                            f"sold at market at {flat_label}")

    def flatten(self) -> None:
        """Sell every orphan at market. Raises UnsettledOrderError while an order is
        unsettled (the caller retries on its next pass)."""
        if not self.held:
            return
        at_broker = self.broker.positions()
        for symbol in list(self.held):
            qty = min(self.held[symbol], at_broker.get(symbol, 0))
            if qty <= 0:
                del self.held[symbol]
                self._event("info", f"orphan {symbol} no longer held at the broker")
                continue
            fill = self.orders.execute(
                lambda cid: self.broker.sell_market(symbol, qty, cid), symbol, "sell", qty,
                ORPHAN_SELL_TIMEOUT,
            )
            if not fill.settled:
                self._oids[fill.order_id] = symbol
                return  # booked by book() once it settles
            self._sold(symbol, min(fill.qty, qty), fill.avg_price)

    def book(self, s: Settled) -> bool:
        """Book a settled orphan sell; False if the order is not an orphan sell."""
        symbol = self._oids.pop(s.order_id, None)
        if symbol is None:
            return False
        self._sold(symbol, s.status.filled_qty, s.status.avg_price)
        return True

    def _sold(self, symbol: str, qty: int, price: float | None) -> None:
        if qty <= 0:
            return
        shown = f"{price:.2f}" if price is not None else "unknown price"
        self._event("info", f"orphan_flatten {symbol} x{qty} @ {shown}")
        left = self.held.get(symbol, 0) - qty
        if left > 0:
            self.held[symbol] = left
        else:
            self.held.pop(symbol, None)
