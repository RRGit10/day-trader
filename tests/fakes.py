"""Shared test doubles. Later tasks extend this module (FakeBroker, FakeHistorical)."""
from __future__ import annotations

import uuid
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
    Cancels are asynchronous like Alpaca's: with cancel_delay_reads = N the order still
    reports "new" for N status reads after cancel, then becomes "canceled" (or "filled"
    when fill_during_cancel). status_fails makes order_status raise.
    place_raises = "before" | "after" makes the next submits raise before/after the order
    reaches the broker; lookup_fails = N makes the next N order_by_client_id calls raise.
    """

    def __init__(self, fill_policy: str = "fill", market_fill_policy: str | None = None) -> None:
        self.fill_policy = fill_policy
        self.market_fill_policy = market_fill_policy
        self.cancel_raises = False  # cancel succeeds, then raises (like a lost response)
        self.cancel_delay_reads = 0
        self.fill_during_cancel = False  # the rest of the order fills while the cancel is pending
        self.status_fails = False
        self.place_raises: str | None = None
        self.lookup_fails = 0
        self.cid_lookups: list[str] = []
        self.cids: dict[str, str] = {}  # order id -> client order id
        self.cancel_all_calls = 0
        self.cancels: list[str] = []
        self.orders: list[dict] = []
        self.market_price: dict[str, float] = {}
        self._state: dict[str, dict] = {}
        self._positions: dict[str, int] = {}

    def _move(self, side: str, symbol: str, qty: int) -> None:
        left = self._positions.get(symbol, 0) + (qty if side == "buy" else -qty)
        if left:
            self._positions[symbol] = left
        else:
            self._positions.pop(symbol, None)

    def _place(self, side: str, symbol: str, qty: int, limit: float | None, type_: str,
               cid: str | None) -> str:
        if self.place_raises == "before":
            raise RuntimeError("submit failed")
        oid = f"o{len(self.orders) + 1}"
        self.cids[oid] = cid or uuid.uuid4().hex
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
            "status": status, "filled": filled, "qty": qty, "fill_price": price,
            "price": price if filled else None, "side": side, "symbol": symbol,
            "cancel_reads": None,
        }
        if filled:
            self._move(side, symbol, filled)
        if self.place_raises == "after":
            raise RuntimeError("submit response lost")
        return oid

    def buy_limit(self, symbol: str, qty: int, limit: float, client_order_id=None) -> str:
        return self._place("buy", symbol, qty, limit, "limit", client_order_id)

    def sell_limit(self, symbol: str, qty: int, limit: float, client_order_id=None) -> str:
        return self._place("sell", symbol, qty, limit, "limit", client_order_id)

    def sell_market(self, symbol: str, qty: int, client_order_id=None) -> str:
        return self._place("sell", symbol, qty, None, "market", client_order_id)

    def order_by_client_id(self, client_order_id: str):
        self.cid_lookups.append(client_order_id)
        if self.lookup_fails > 0:
            self.lookup_fails -= 1
            raise RuntimeError("lookup failed")
        for oid, cid in self.cids.items():
            if cid == client_order_id:
                return oid, self.order_status(oid)
        return None

    def open_orders(self) -> list[str]:
        return [oid for oid in self._state if self.order_status(oid).status in ("new", "partially_filled")]

    def cancel_all_open(self) -> None:
        self.cancel_all_calls += 1
        for oid, st in self._state.items():
            if st["status"] in ("new", "partially_filled"):
                self.cancel(oid)

    def force_fill(self, order_id: str) -> None:
        """The rest of a live order fills now (e.g. while its cancel is still pending)."""
        s = self._state[order_id]
        more = s["qty"] - s["filled"]
        if more:
            self._move(s["side"], s["symbol"], more)
        s.update(status="filled", filled=s["qty"], price=s["fill_price"], cancel_reads=None)

    def _finish_cancel(self, s: dict) -> None:
        s["cancel_reads"] = None
        if self.fill_during_cancel:
            more = s["qty"] - s["filled"]
            if more:
                self._move(s["side"], s["symbol"], more)
            s.update(status="filled", filled=s["qty"], price=s["fill_price"])
        else:
            s["status"] = "canceled"

    def cancel(self, order_id: str) -> None:
        self.cancels.append(order_id)
        s = self._state[order_id]
        if s["status"] in ("new", "partially_filled") and s["cancel_reads"] is None:
            if self.cancel_delay_reads:
                s["cancel_reads"] = self.cancel_delay_reads
            else:
                self._finish_cancel(s)
        if self.cancel_raises:
            raise RuntimeError("cancel response lost")

    def order_status(self, order_id: str):
        from daytrader.broker import OrderStatus

        if self.status_fails:
            raise RuntimeError("order status unavailable")
        s = self._state[order_id]
        if s["cancel_reads"] is not None:
            if s["cancel_reads"] > 0:
                s["cancel_reads"] -= 1
            else:
                self._finish_cancel(s)
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


class FakeHistorical:
    """In-memory HistoricalSource built from dicts.

    hours[day] = MarketHours; stock[(ticker, day, minutes)] = bars;
    contracts[(underlying, kind, expiry)] = [(symbol, strike)]; option_bars[(symbol, day)] = bars.
    """

    def __init__(self) -> None:
        self.hours: dict[date, MarketHours] = {}
        self.stock: dict[tuple[str, date, int], list[Bar]] = {}
        self.contracts: dict[tuple[str, str, date], list[tuple[str, float]]] = {}
        self.option_bars: dict[tuple[str, date], list[Bar]] = {}

    def trading_days(self, start: date, end: date) -> list[tuple[date, MarketHours]]:
        return sorted((d, h) for d, h in self.hours.items() if start <= d <= end)

    def stock_bars(self, ticker: str, day: date, minutes: int) -> list[Bar]:
        return list(self.stock.get((ticker, day, minutes), []))

    def option_contracts(self, underlying: str, kind: OptionType, expiry: date) -> list[tuple[str, float]]:
        return list(self.contracts.get((underlying, kind, expiry), []))

    def expiries_after(self, underlying: str, day: date) -> list[date]:
        return sorted({e for (u, _, e) in self.contracts if u == underlying and e > day})

    def option_bars_1min(self, symbol: str, day: date) -> list[Bar]:
        return list(self.option_bars.get((symbol, day), []))
