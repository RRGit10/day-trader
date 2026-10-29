"""Shared test doubles. Later tasks extend this module (FakeBroker, FakeHistorical)."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Sequence

from daytrader.clock import MarketHours
from daytrader.models import Bar, OptionQuote, OptionType


class FakeClock:
    def __init__(self, start: datetime):
        self._now = start

    def now(self) -> datetime:
        return self._now

    def sleep_until(self, dt: datetime) -> None:
        self._now = max(self._now, dt)

    def advance(self, seconds: float) -> None:
        self._now = self._now + timedelta(seconds=seconds)


class FakeData:
    def __init__(self) -> None:
        self.hours: dict[date, MarketHours | None] = {}
        self.bars: dict[str, list[Bar]] = {}
        self.chains: dict[tuple[str, str], list[OptionQuote]] = {}
        self.stock_prices: dict[str, float] = {}
        self.option_quotes: dict[str, tuple[float, float, datetime]] = {}

    def market_hours(self, day: date) -> MarketHours | None:
        return self.hours.get(day)

    def bars_5min(
        self, tickers: Sequence[str], start: datetime, end: datetime
    ) -> dict[str, list[Bar]]:
        out: dict[str, list[Bar]] = {}
        for tk in tickers:
            sel = [
                b for b in self.bars.get(tk, [])
                if start <= b.t and b.t + timedelta(minutes=5) <= end
            ]
            if sel:
                out[tk] = sel
        return out

    def option_chain(self, underlying: str, kind: OptionType, today: date) -> list[OptionQuote]:
        return list(self.chains.get((underlying, kind), []))

    def latest_stock_price(self, ticker: str) -> float:
        return self.stock_prices[ticker]

    def latest_option_quote(self, symbol: str) -> tuple[float, float, datetime]:
        return self.option_quotes[symbol]


class FakeBroker:
    """In-memory Broker. fill_policy: "fill" | "partial" | "never" (may change between calls)."""

    def __init__(self, fill_policy: str = "fill") -> None:
        self.fill_policy = fill_policy
        self.orders: list[dict] = []
        self.market_price: dict[str, float] = {}
        self._state: dict[str, dict] = {}
        self._positions: dict[str, int] = {}

    def _place(self, side: str, symbol: str, qty: int, limit: float | None, type_: str) -> str:
        oid = f"o{len(self.orders) + 1}"
        self.orders.append(
            {"side": side, "symbol": symbol, "qty": qty, "limit": limit, "type": type_}
        )
        price = limit if limit is not None else self.market_price.get(symbol, 0.0)
        if self.fill_policy == "fill":
            filled, status = qty, "filled"
        elif self.fill_policy == "partial" and qty >= 2:
            filled, status = qty // 2, "partially_filled"
        else:  # "never", or a 1-lot under "partial"
            filled, status = 0, "new"
        self._state[oid] = {
            "status": status, "filled": filled,
            "price": price if filled else None, "side": side, "symbol": symbol,
        }
        delta = filled if side == "buy" else -filled
        if delta:
            left = self._positions.get(symbol, 0) + delta
            if left:
                self._positions[symbol] = left
            else:
                self._positions.pop(symbol, None)
        return oid

    def buy_limit(self, symbol: str, qty: int, limit: float) -> str:
        return self._place("buy", symbol, qty, limit, "limit")

    def sell_limit(self, symbol: str, qty: int, limit: float) -> str:
        return self._place("sell", symbol, qty, limit, "limit")

    def sell_market(self, symbol: str, qty: int) -> str:
        return self._place("sell", symbol, qty, None, "market")

    def cancel(self, order_id: str) -> None:
        self._state[order_id]["status"] = "canceled"

    def order_status(self, order_id: str):
        from daytrader.broker import OrderStatus

        s = self._state[order_id]
        return OrderStatus(s["status"], s["filled"], s["price"])

    def positions(self) -> dict[str, int]:
        return dict(self._positions)
