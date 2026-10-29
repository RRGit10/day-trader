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
    """In-memory Broker. fill_policy: "fill" | "partial" | "never" (may change between calls).

    market_fill_policy, if set, applies to market orders instead of fill_policy.
    """

    def __init__(self, fill_policy: str = "fill", market_fill_policy: str | None = None) -> None:
        self.fill_policy = fill_policy
        self.market_fill_policy = market_fill_policy  # overrides fill_policy for market orders
        self.cancel_raises = False  # cancel succeeds, then raises (like a lost response)
        self.fill_on_cancel = False  # the order fills completely in a race with the cancel
        self.cancels: list[str] = []
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
        policy = self.fill_policy
        if type_ == "market" and self.market_fill_policy is not None:
            policy = self.market_fill_policy
        if policy == "fill":
            filled, status = qty, "filled"
        elif policy == "partial" and qty >= 2:
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
        self.cancels.append(order_id)
        s = self._state[order_id]
        if self.fill_on_cancel and s["status"] in ("new", "partially_filled"):
            o = self.orders[int(order_id[1:]) - 1]
            more = o["qty"] - s["filled"]
            s.update(status="filled", filled=o["qty"], price=o["limit"] or s["price"])
            sign = 1 if s["side"] == "buy" else -1
            self._positions[s["symbol"]] = self._positions.get(s["symbol"], 0) + sign * more
            if not self._positions[s["symbol"]]:
                del self._positions[s["symbol"]]
            return
        s["status"] = "canceled"
        if self.cancel_raises:
            raise RuntimeError("cancel response lost")

    def order_status(self, order_id: str):
        from daytrader.broker import OrderStatus

        s = self._state[order_id]
        return OrderStatus(s["status"], s["filled"], s["price"])

    def positions(self) -> dict[str, int]:
        return dict(self._positions)


class LiveFakeData(FakeData):
    """FakeData for session tests.

    Option quotes stored with ts=None are stamped with the clock's current time.
    `fail[method] = exc` makes that method raise. `bar_calls` records each bars fetch.
    """

    def __init__(self, clock: FakeClock) -> None:
        super().__init__()
        self.clock = clock
        self.fail: dict[str, Exception] = {}
        self.bar_calls: list[tuple[list[str], datetime]] = []

    def _maybe_fail(self, name: str) -> None:
        if name in self.fail:
            raise self.fail[name]

    def set_quote(self, symbol: str, bid: float, ask: float, ts: datetime | None = None) -> None:
        self.option_quotes[symbol] = (bid, ask, ts)

    def bars_5min(self, tickers, start, end):
        self._maybe_fail("bars_5min")
        self.bar_calls.append((list(tickers), self.clock.now()))
        return super().bars_5min(tickers, start, end)

    def option_chain(self, underlying, kind, today):
        self._maybe_fail("option_chain")
        return super().option_chain(underlying, kind, today)

    def latest_stock_price(self, ticker):
        self._maybe_fail("latest_stock_price")
        return super().latest_stock_price(ticker)

    def latest_option_quote(self, symbol):
        self._maybe_fail("latest_option_quote")
        bid, ask, ts = self.option_quotes[symbol]
        return bid, ask, ts if ts is not None else self.clock.now()


class ScriptedApprover:
    """Returns scripted decisions in order (then "approved"); records every ticket."""

    def __init__(self, decisions: Sequence[str] = (), on_request=None) -> None:
        self.decisions = list(decisions)
        self.tickets: list = []
        self.on_request = on_request

    def request(self, ticket):
        self.tickets.append(ticket)
        if self.on_request is not None:
            self.on_request(ticket)
        return self.decisions.pop(0) if self.decisions else "approved"
