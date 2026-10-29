"""Broker port and Alpaca adapter. Always paper: there is no live-trading path."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest

from .config import ConfigError, Keys
from .data import with_retries


class PaperLockError(Exception):
    """Raised when the keys cannot be verified as Alpaca paper-trading keys."""


@dataclass(frozen=True)
class OrderStatus:
    status: str  # "new" | "partially_filled" | "filled" | "canceled" | "rejected"
    filled_qty: int
    avg_price: float | None


class Broker(Protocol):
    def buy_limit(self, symbol: str, qty: int, limit: float) -> str: ...

    def sell_limit(self, symbol: str, qty: int, limit: float) -> str: ...

    def sell_market(self, symbol: str, qty: int) -> str: ...

    def cancel(self, order_id: str) -> None: ...

    def order_status(self, order_id: str) -> OrderStatus: ...

    def positions(self) -> dict[str, int]: ...


_STATUS_MAP = {
    "new": "new", "accepted": "new", "pending_new": "new", "held": "new",
    "partially_filled": "partially_filled",
    "filled": "filled",
    "canceled": "canceled", "expired": "canceled", "done_for_day": "canceled",
    "replaced": "canceled",
    "rejected": "rejected", "suspended": "rejected", "stopped": "rejected",
}


class AlpacaBroker:
    def __init__(self, keys: Keys):
        self._client = TradingClient(keys.api_key, keys.api_secret, paper=True)

    def verify_paper(self) -> None:
        try:
            self._client.get_account()
        except Exception as exc:
            raise PaperLockError(
                "could not authenticate with Alpaca: the keys must be Alpaca paper "
                f"trading keys ({exc})"
            ) from exc

    def get_account(self):
        return with_retries(lambda: self._client.get_account())

    # Submissions are deliberately not retried: a retried submit could double-order.
    def buy_limit(self, symbol: str, qty: int, limit: float) -> str:
        req = LimitOrderRequest(
            symbol=symbol, qty=qty, side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY, limit_price=limit,
        )
        return str(self._client.submit_order(req).id)

    def sell_limit(self, symbol: str, qty: int, limit: float) -> str:
        req = LimitOrderRequest(
            symbol=symbol, qty=qty, side=OrderSide.SELL,
            time_in_force=TimeInForce.DAY, limit_price=limit,
        )
        return str(self._client.submit_order(req).id)

    def sell_market(self, symbol: str, qty: int) -> str:
        req = MarketOrderRequest(
            symbol=symbol, qty=qty, side=OrderSide.SELL, time_in_force=TimeInForce.DAY,
        )
        return str(self._client.submit_order(req).id)

    def cancel(self, order_id: str) -> None:
        with_retries(lambda: self._client.cancel_order_by_id(order_id))

    def order_status(self, order_id: str) -> OrderStatus:
        o = with_retries(lambda: self._client.get_order_by_id(order_id))
        raw = getattr(o.status, "value", o.status)
        # Transitional states (pending_cancel, pending_replace, ...) are still live: "new".
        status = _STATUS_MAP.get(str(raw), "new")
        price = o.filled_avg_price
        return OrderStatus(
            status,
            int(float(o.filled_qty or 0)),
            float(price) if price is not None else None,
        )

    def positions(self) -> dict[str, int]:
        pos = with_retries(lambda: self._client.get_all_positions())
        return {p.symbol: int(float(p.qty)) for p in pos}


def make_broker(keys: Keys | None, mode: str) -> AlpacaBroker:
    if keys is None:
        raise ConfigError(f"missing API keys for {mode} mode (set them in .env or the environment)")
    broker = AlpacaBroker(keys)
    broker.verify_paper()
    return broker
