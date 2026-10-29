"""Order execution: submit, poll, cancel, and track orders whose final state is unknown.

Alpaca cancels are asynchronous and status reads can fail, so an order is only
"settled" once the broker reports it terminal. Orders that are not settled when
`execute` gives up are kept in `unsettled`. When `refresh` later sees one become
terminal, its final status is held until the caller `collect`s it, so no fill is
ever discarded. `execute` refuses to place an order while anything is unsettled
or uncollected.
"""
from __future__ import annotations

import traceback
from dataclasses import dataclass
from datetime import timedelta
from typing import Callable, Literal

from .broker import Broker, OrderStatus
from .clock import Clock

TERMINAL = ("filled", "canceled", "rejected")
POLL_EVERY = timedelta(seconds=2)
CANCEL_WAIT = timedelta(seconds=20)

Side = Literal["buy", "sell"]


@dataclass(frozen=True)
class Fill:
    order_id: str
    qty: int  # last filled qty the broker reported (authoritative only if settled)
    avg_price: float | None
    settled: bool


@dataclass(frozen=True)
class Settled:
    order_id: str
    symbol: str
    side: Side
    status: OrderStatus


class UnsettledOrderError(RuntimeError):
    """Raised instead of placing an order while an earlier order is unsettled or uncollected."""


class Orders:
    def __init__(self, broker: Broker, clock: Clock, event: Callable[[str, str], None]) -> None:
        self.broker = broker
        self.clock = clock
        self._event = event
        self.unsettled: dict[str, tuple[str, Side]] = {}  # order id -> (symbol, side)
        self._settled: list[Settled] = []

    def refresh(self) -> None:
        """Re-poll unsettled orders (re-sending cancel to live ones); hold terminal results."""
        for oid, (symbol, side) in list(self.unsettled.items()):
            try:
                st = self.broker.order_status(oid)
                if st.status in TERMINAL:
                    del self.unsettled[oid]
                    self._settled.append(Settled(oid, symbol, side, st))
                else:
                    self.broker.cancel(oid)
            except Exception:
                continue

    def collect(self) -> list[Settled]:
        """Hand over (and forget) every order that settled since the last collect."""
        out, self._settled = self._settled, []
        return out

    def execute(
        self, place: Callable[[], str], symbol: str, side: Side, qty: int, timeout: timedelta
    ) -> Fill:
        """Place an order, wait up to `timeout` for a full fill, else cancel and settle it."""
        self.refresh()
        if self.unsettled or self._settled:
            raise UnsettledOrderError(
                f"not placing a {symbol} order: unsettled {sorted(self.unsettled)}, "
                f"uncollected {[s.order_id for s in self._settled]}"
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
        self.unsettled[oid] = (symbol, side)
        self._event("warn", f"order {oid} ({side} {symbol}) unsettled; no new orders until it "
                            f"settles: {why}")
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
