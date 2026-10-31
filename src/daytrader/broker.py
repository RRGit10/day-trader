"""Broker port and Alpaca adapter. Always paper: there is no live-trading path."""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Protocol

from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import GetOrdersRequest, LimitOrderRequest, MarketOrderRequest

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
    """Every submit carries a client order id (a fresh uuid4 hex if none is given)."""

    def buy_limit(self, symbol: str, qty: int, limit: float,
                  client_order_id: str | None = None) -> str: ...

    def sell_limit(self, symbol: str, qty: int, limit: float,
                   client_order_id: str | None = None) -> str: ...

    def sell_market(self, symbol: str, qty: int, client_order_id: str | None = None) -> str: ...

    def cancel(self, order_id: str) -> None: ...

    def order_status(self, order_id: str) -> OrderStatus: ...

    def order_by_client_id(self, client_order_id: str) -> tuple[str, OrderStatus] | None: ...

    def open_orders(self) -> list[str]: ...

    def cancel_all_open(self) -> None: ...

    def positions(self) -> dict[str, int]: ...


_STATUS_MAP = {
    "new": "new", "accepted": "new", "pending_new": "new", "held": "new",
    "partially_filled": "partially_filled",
    "filled": "filled",
    "canceled": "canceled", "expired": "canceled", "done_for_day": "canceled",
    "replaced": "canceled",
    "rejected": "rejected", "suspended": "rejected", "stopped": "rejected",
}


def _to_status(o) -> OrderStatus:
    raw = getattr(o.status, "value", o.status)
    # Transitional states (pending_cancel, pending_replace, ...) are still live: "new".
    status = _STATUS_MAP.get(str(raw), "new")
    price = o.filled_avg_price
    return OrderStatus(
        status,
        int(float(o.filled_qty or 0)),
        float(price) if price is not None else None,
    )


class AlpacaBroker:
    def __init__(self, keys: Keys):
        self._client = TradingClient(keys.api_key, keys.api_secret, paper=True)

    def verify_paper(self) -> None:
        """An auth rejection (HTTP 401/403) is a PaperLockError; any other error (network,
        5xx) is raised unchanged so the caller can retry."""
        try:
            self._client.get_account()
        except APIError as exc:
            if exc.status_code in (401, 403):
                raise PaperLockError(
                    "could not authenticate with Alpaca: the keys must be Alpaca paper "
                    f"trading keys ({exc})"
                ) from exc
            raise

    def get_account(self):
        return with_retries(lambda: self._client.get_account())

    # Submissions are deliberately not retried: a retried submit could double-order.
    # A submit whose response is lost is found again by its client order id (orders.py).
    def buy_limit(self, symbol: str, qty: int, limit: float,
                  client_order_id: str | None = None) -> str:
        req = LimitOrderRequest(
            symbol=symbol, qty=qty, side=OrderSide.BUY, time_in_force=TimeInForce.DAY,
            limit_price=limit, client_order_id=client_order_id or uuid.uuid4().hex,
        )
        return str(self._client.submit_order(req).id)

    def sell_limit(self, symbol: str, qty: int, limit: float,
                   client_order_id: str | None = None) -> str:
        req = LimitOrderRequest(
            symbol=symbol, qty=qty, side=OrderSide.SELL, time_in_force=TimeInForce.DAY,
            limit_price=limit, client_order_id=client_order_id or uuid.uuid4().hex,
        )
        return str(self._client.submit_order(req).id)

    def sell_market(self, symbol: str, qty: int, client_order_id: str | None = None) -> str:
        req = MarketOrderRequest(
            symbol=symbol, qty=qty, side=OrderSide.SELL, time_in_force=TimeInForce.DAY,
            client_order_id=client_order_id or uuid.uuid4().hex,
        )
        return str(self._client.submit_order(req).id)

    def cancel(self, order_id: str) -> None:
        with_retries(lambda: self._client.cancel_order_by_id(order_id))

    def order_status(self, order_id: str) -> OrderStatus:
        return _to_status(with_retries(lambda: self._client.get_order_by_id(order_id)))

    def order_by_client_id(self, client_order_id: str) -> tuple[str, OrderStatus] | None:
        """(order id, status), or None if Alpaca has no order with this client id (404)."""
        try:
            o = self._client.get_order_by_client_id(client_order_id)
        except APIError as exc:
            if exc.status_code == 404:
                return None
            raise
        return str(o.id), _to_status(o)

    def open_orders(self) -> list[str]:
        req = GetOrdersRequest(status=QueryOrderStatus.OPEN)
        return [str(o.id) for o in with_retries(lambda: self._client.get_orders(req))]

    def cancel_all_open(self) -> None:
        with_retries(lambda: self._client.cancel_orders())

    def positions(self) -> dict[str, int]:
        pos = with_retries(lambda: self._client.get_all_positions())
        return {p.symbol: int(float(p.qty)) for p in pos}


def make_broker(keys: Keys | None, mode: str) -> AlpacaBroker:
    if keys is None:
        raise ConfigError(f"missing API keys for {mode} mode (set them in .env or the environment)")
    broker = AlpacaBroker(keys)
    broker.verify_paper()
    return broker
