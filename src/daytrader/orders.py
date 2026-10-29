"""Order execution: submit, poll, cancel, and track orders whose final state is unknown.

Alpaca cancels are asynchronous and status reads can fail, so an order is only
"settled" once the broker reports it terminal. Anything else is kept in
`unsettled`, and no new order is placed until every unsettled order has settled.
"""
from __future__ import annotations

import traceback
from dataclasses import dataclass
from datetime import timedelta
from typing import Callable

from .broker import Broker, OrderStatus
from .clock import Clock

TERMINAL = ("filled", "canceled", "rejected")
POLL_EVERY = timedelta(seconds=2)
CANCEL_WAIT = timedelta(seconds=20)


@dataclass(frozen=True)
class Fill:
    order_id: str
    qty: int  # last filled qty the broker reported (authoritative only if settled)
    avg_price: float | None
    settled: bool


class UnsettledOrderError(RuntimeError):
    """Raised instead of placing an order while an earlier order is unsettled."""


class Orders:
    def __init__(self, broker: Broker, clock: Clock, event: Callable[[str, str], None]) -> None:
        self.broker = broker
        self.clock = clock
        self._event = event
        self.unsettled: dict[str, str] = {}  # order id -> symbol

    def refresh(self) -> dict[str, OrderStatus]:
        """Re-poll unsettled orders (re-sending cancel to live ones); return those now terminal."""
        settled: dict[str, OrderStatus] = {}
        for oid in list(self.unsettled):
            try:
                st = self.broker.order_status(oid)
                if st.status in TERMINAL:
                    del self.unsettled[oid]
                    settled[oid] = st
                else:
                    self.broker.cancel(oid)
            except Exception:
                continue
        return settled

    def execute(
        self, place: Callable[[], str], symbol: str, qty: int, timeout: timedelta
    ) -> Fill:
        """Place an order, wait up to `timeout` for a full fill, else cancel and settle it."""
        self.refresh()
        if self.unsettled:
            raise UnsettledOrderError(
                f"not placing a {symbol} order: unsettled orders {sorted(self.unsettled)}"
            )
        oid = place()
        st: OrderStatus | None = None
        try:
            st = self._poll(oid, qty, timeout)
            if st.filled_qty >= qty or st.status in TERMINAL:
                return Fill(oid, st.filled_qty, st.avg_price, True)
            try:
                self.broker.cancel(oid)
            except Exception as exc:
                self._event("warn", f"cancel {oid} raised ({exc!r}); polling its status")
            st = self._poll(oid, None, CANCEL_WAIT)
            if st.status in TERMINAL:
                return Fill(oid, st.filled_qty, st.avg_price, True)
            why = f"still {st.status} {CANCEL_WAIT.seconds}s after cancel"
        except Exception:
            why = traceback.format_exc()
            try:
                self.broker.cancel(oid)
            except Exception:
                pass
        self.unsettled[oid] = symbol
        self._event("error", f"order {oid} ({symbol}) unsettled: {why}")
        return Fill(oid, st.filled_qty if st else 0, st.avg_price if st else None, False)

    def _poll(self, oid: str, qty: int | None, timeout: timedelta) -> OrderStatus:
        """Poll until terminal (or `qty` filled) or timeout; return the last status read."""
        deadline = self.clock.now() + timeout
        while True:
            st = self.broker.order_status(oid)
            if st.status in TERMINAL or (qty is not None and st.filled_qty >= qty):
                return st
            if self.clock.now() >= deadline:
                return st
            self.clock.sleep_until(min(self.clock.now() + POLL_EVERY, deadline))
