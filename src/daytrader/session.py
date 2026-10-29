"""Trading session: orchestrates one trading day for approval or bot mode.

All trading decisions come from the pure modules (orb, contracts, guard, exits);
this module only sequences them, talks to data/broker/approver, and journals.
"""
from __future__ import annotations

import json
import os
import tempfile
import traceback
import uuid
from dataclasses import replace
from datetime import datetime, time, timedelta
from typing import Literal

from .approval import Approver, Ticket, clear_pending
from .broker import Broker
from .clock import Clock, MarketHours, retry_until, session_times
from .config import Settings
from .contracts import contracts_for_budget, entry_limit_price, pick_contract
from .data import MarketData
from .exits import check_exit
from .guard import may_enter
from .journal import Journal
from .models import ET, Bar, ClosedTrade, ExitReason, OptionQuote, OrbResult, Position
from .orb import evaluate_orb
from .orders import Fill, Orders, Settled, UnsettledOrderError
from .orphans import Orphans, cancel_open_orders

STARTUP_AT = time(9, 25)
AFFORDABILITY_AT = time(9, 40)
AFFORDABILITY_RETRY_AT = time(9, 44)
STARTUP_GIVE_UP_AT = time(9, 44)
STARTUP_RETRY_EVERY = timedelta(seconds=30)
HEARTBEAT_EVERY = timedelta(seconds=15)
BAR_FETCH_DELAY = timedelta(seconds=5)
BAR_RETRY_EVERY = timedelta(seconds=10)
BAR_RETRY_FOR = timedelta(seconds=60)
MANAGE_EVERY = timedelta(seconds=15)
QUOTE_STALE_AFTER = timedelta(seconds=60)
ENTRY_TIMEOUT = timedelta(seconds=60)
EXIT_LIMIT_TIMEOUT = timedelta(seconds=10)
EXIT_LIMIT_ATTEMPTS = 3
EXIT_MARKET_TIMEOUT = timedelta(seconds=60)
REQUOTE_TOLERANCE = 0.05
SUMMARY_AFTER_CLOSE = timedelta(minutes=5)
PRIOR_DAY_LOOKBACK = 7


class ExitPaused(RuntimeError):
    """An exit order is unsettled; the exit resumes once it settles (journaled once by Orders)."""


class _BeatingClock:
    """Wraps the clock so that every wait (bar retries, order polls, the main loop) is cut
    into slices of at most HEARTBEAT_EVERY, with a heartbeat after each slice."""

    def __init__(self, clock: Clock, beat) -> None:
        self._clock, self._beat = clock, beat

    def now(self) -> datetime:
        return self._clock.now()

    def sleep_until(self, dt: datetime) -> None:
        while self._clock.now() < dt:
            self._clock.sleep_until(min(dt, self._clock.now() + HEARTBEAT_EVERY))
            self._beat()


class Session:
    def __init__(
        self,
        mode: Literal["approval", "bot"],
        settings: Settings,
        data: MarketData,
        broker: Broker,
        journal: Journal,
        approver: Approver,
        clock: Clock,
    ) -> None:
        self.mode = mode
        self.settings = settings
        self.data = data
        self.broker = broker
        self.journal = journal
        self.approver = approver
        self._beat_warned = False
        self.clock = _BeatingClock(clock, self._beat)

        self.today = clock.now().date()
        self.hours: MarketHours | None = None
        self.entry_end: time = settings.strategy.entry_end
        self.flat_time: time = settings.exits.flat_time
        self.scan_list: list[str] = []
        self.prior_bars: dict[str, list[Bar]] = {}
        self.signaled: set[tuple[str, str]] = set()
        self.no_new_entries = False
        self._afford_retry: list[str] = []  # tickers whose 09:40 check failed on an error
        self._empty_scan_noted = False
        self._trigger_closes: dict[str, float] = {}
        self._startup_done: set[str] = set()  # ticket id -> signal's trigger close

        self.orders = Orders(broker, self.clock, self._event)
        self.orphans = Orphans(broker, self.orders, self._event)
        self.position: Position | None = None
        self._trade_id: int | None = None
        self._pending_entry: tuple[Ticket, str] | None = None  # entry whose buy is unsettled
        self._set_position(None, None)

    # ---------------------------------------------------------------- helpers
    def _at(self, t: time) -> datetime:
        return datetime.combine(self.today, t, tzinfo=ET)

    def _event(self, level: str, message: str) -> None:
        self.journal.record_event(self.clock.now(), level, message)

    def _skip(self, ticker: str, reason: str, detail: str = "") -> None:
        self.journal.record_skip(self.clock.now(), ticker, reason, detail)

    def _error(self) -> None:
        self.no_new_entries = True
        self._event("error", traceback.format_exc())

    def _trades_today(self) -> list[ClosedTrade]:
        return self.journal.trades_on(self.today)

    def _day_pnl(self) -> float:
        return round(sum(t.pnl for t in self._trades_today()), 2)

    def _busy(self) -> bool:
        return self.position is not None or self._pending_entry is not None

    def _set_position(self, pos: Position | None, trade_id: int | None) -> None:
        self.position, self._trade_id = pos, trade_id
        self._sold: list[tuple[int, float]] = []  # exit fills (qty, price) for this position
        self._exit_oids: set[str] = set()  # exit orders placed for this position
        self._exit_reason: ExitReason | None = None  # set once an exit has started
        self._exit_unsettled_seen = False  # then broker positions cap every further sell
        self._last_price = pos.entry_price if pos is not None else 0.0  # last known option price
        self._stale_warned = False
        self._stock_warned = False

    def _amend(self, qty: int, entry_price: float, why: str) -> None:
        old = self.position
        self.position = replace(old, qty=qty, entry_price=entry_price)
        self.journal.amend_open_trade(self._trade_id, qty, entry_price)
        self._event("warn", f"{old.option_symbol}: qty {old.qty} -> {qty}, entry "
                            f"{old.entry_price:.2f} -> {entry_price:.2f} ({why})")

    def _held(self) -> int:
        return self.broker.positions().get(self.position.option_symbol, 0)

    # ---------------------------------------------------------------- startup
    def startup(self) -> None:
        """One attempt at the startup steps not yet done; raises if a step fails
        (run_day retries every 30 s until 09:44)."""
        done = self._startup_done
        if "hours" not in done:
            self.today = self.clock.now().date()
            self.hours = self.data.market_hours(self.today)
            done.add("hours")
            if self.hours is not None:
                self.entry_end, self.flat_time = session_times(
                    self.hours, self.settings.exits, self.settings.strategy
                )
            else:
                self._event("info", f"market closed on {self.today.isoformat()}")
        if self.hours is None:
            return
        if "broker" not in done:
            if self.mode == "approval":
                clear_pending(self.settings.state_dir)  # no old ticket can be approved now
            cancel_open_orders(self.broker, self.clock, self._event)
            self._reconcile()
            self.signaled |= self.journal.signaled_on(self.today)  # restart-safe: one a day
            done.add("broker")
        if "prior_bars" not in done:
            self._load_prior_bars()
            done.add("prior_bars")

    def _start(self) -> bool:
        """startup() with retries; False if the day cannot be traded or managed at all."""
        def failed(exc: Exception) -> None:
            self._event("warn", f"startup failed ({exc!r}); retrying in "
                                f"{STARTUP_RETRY_EVERY.seconds}s")
        try:
            retry_until(self.startup, self.clock, self._at(STARTUP_GIVE_UP_AT),
                        STARTUP_RETRY_EVERY, failed)
            return True
        except Exception:
            self._event("error", "startup failed; giving up for today:\n"
                                 + traceback.format_exc())
        if {"hours", "broker"} <= self._startup_done:  # only the warm-up is missing:
            self.no_new_entries = True  # no entries, but manage and flatten what is held
            return True
        return False

    def _reconcile(self) -> None:
        held = self.broker.positions()
        known: set[str] = set()
        for trade_id, pos in self.journal.open_trades():
            sym = pos.option_symbol
            if sym not in held:
                self.journal.close_trade(trade_id, pos.entry_price, "reconciled_missing", self.clock.now())
                self._event("warn", f"journal trade {trade_id} {sym} not held at broker: closed")
                continue
            known.add(sym)
            if self.position is not None:
                self._event("warn", f"second open trade {trade_id} {sym} at broker: not managed")
                continue
            self._set_position(pos, trade_id)
            if held[sym] < pos.qty:
                self._amend(held[sym], pos.entry_price, "broker holds fewer than the journal")
            elif held[sym] > pos.qty:
                self._event("warn", f"{sym}: broker holds {held[sym]} > journal {pos.qty}; "
                                    f"managing {pos.qty}")
            self._event("info", f"resumed managing trade {trade_id} {sym} x{self.position.qty}")
        for sym, qty in held.items():
            if sym not in known:
                self.orphans.add(sym, qty, f"{self.flat_time:%H:%M}")

    def _load_prior_bars(self) -> None:
        day = self.today
        for _ in range(PRIOR_DAY_LOOKBACK):
            day -= timedelta(days=1)
            hours = self.data.market_hours(day)
            if hours is not None:
                self.prior_bars = self.data.bars_5min(
                    list(self.settings.tickers), hours.open, hours.close
                )
                return
        self._event("warn", "no prior trading day found for indicator warm-up")

    # ---------------------------------------------------------- affordability
    def affordability_filter(self) -> list[str]:
        """09:40 check. A ticker that failed only because of a data error (not a pick
        reason) is not skipped yet: retry_affordability() checks it once more at 09:44."""
        kept: list[str] = []
        self._afford_retry = []
        for tk in self.settings.tickers:
            reasons = self._affordable(tk)
            if not reasons:
                kept.append(tk)
            elif "chain_error" in reasons:
                self._afford_retry.append(tk)
            else:
                self._skip_unaffordable(tk, reasons)
        self.scan_list = kept
        return kept

    def retry_affordability(self) -> list[str]:
        retry, self._afford_retry = self._afford_retry, []
        kept = set(self.scan_list)
        for tk in retry:
            reasons = self._affordable(tk)
            if reasons:
                self._skip_unaffordable(tk, reasons)
            else:
                kept.add(tk)
        self.scan_list = [tk for tk in self.settings.tickers if tk in kept]
        return self.scan_list

    def _kinds(self) -> list[str]:
        return ["call", "put"] if self.settings.strategy.shorts_enabled else ["call"]

    def _affordable(self, tk: str) -> list[str]:
        """[] if an affordable contract exists, else one failure reason per option kind."""
        reasons: list[str] = []
        for kind in self._kinds():
            try:
                chain = self.data.option_chain(tk, kind, self.today)
            except Exception as exc:
                reasons.append("chain_error")
                self._event("warn", f"{tk} {kind} chain failed at affordability: {exc!r}")
                continue
            direction = "long" if kind == "call" else "short"
            pick = pick_contract(direction, chain, self.today, self.settings.contract)
            if pick.quote is not None:
                return []
            reasons.append(str(pick.skip_reason))
        return reasons

    def _skip_unaffordable(self, tk: str, reasons: list[str]) -> None:
        detail = "; ".join(f"{k}: {r}" for k, r in zip(self._kinds(), reasons))
        self._skip(tk, reasons[0], detail)

    # -------------------------------------------------------------- bar close
    def on_bar_close(self, close_time: datetime) -> None:
        try:
            self._on_bar_close(close_time)
        except Exception:
            self._error()

    def _on_bar_close(self, close_time: datetime) -> None:
        self.clock.sleep_until(close_time + BAR_FETCH_DELAY)
        bars = self._fetch_closed_bars(close_time)
        results: list[OrbResult] = []
        for tk in self.scan_list:
            if tk not in bars:
                continue
            res = evaluate_orb(tk, self.prior_bars.get(tk, []), bars[tk],
                               self.settings.strategy, self.entry_end)
            self.journal.record_check(self.clock.now(), res)
            results.append(res)
        for res in results:  # scan-list order; the guard allows only one open position
            key = (res.ticker, str(res.direction))
            if not res.triggered or key in self.signaled:
                continue
            self.signaled.add(key)
            self._try_entry(res)

    def _fetch_closed_bars(self, close_time: datetime) -> dict[str, list[Bar]]:
        if not self.scan_list:
            if not self._empty_scan_noted:
                self._empty_scan_noted = True
                self._event("info", "no tickers passed the affordability filter")
            return {}
        start = self.hours.open if self.hours is not None else self._at(time(9, 30))
        step = timedelta(minutes=self.settings.strategy.bar_minutes)
        last_start = close_time - step
        # the just-closed bar and the one before it must both be there
        need = [t for t in (last_start - step, last_start) if t >= start]
        errors: list[Exception] = []

        def fetch(tickers: list[str]) -> dict[str, list[Bar]]:
            try:
                got = self.data.bars_5min(tickers, start, close_time)
            except Exception as exc:  # a data error counts as "missing": retried below
                if not errors:
                    self._event("warn", f"bars fetch for the {close_time:%H:%M} close failed "
                                        f"({exc!r}); retrying")
                errors.append(exc)
                return {}
            out = {}
            for tk, bs in got.items():
                bs = [b for b in bs if b.t < close_time]  # never use an in-progress bar
                if all(any(b.t == t for b in bs) for t in need):
                    out[tk] = bs
            return out

        bars = fetch(list(self.scan_list))
        missing = [tk for tk in self.scan_list if tk not in bars]
        deadline = self.clock.now() + BAR_RETRY_FOR
        while missing and self.clock.now() < deadline:
            self.clock.sleep_until(min(self.clock.now() + BAR_RETRY_EVERY, deadline))
            if self._busy():
                self.manage_position()
            bars.update(fetch(missing))
            missing = [tk for tk in missing if tk not in bars]
        why = f"; last error {errors[-1]!r}" if errors else ""
        for tk in missing:
            self._skip(tk, "no_bars",
                       f"bar {'/'.join(f'{t:%H:%M}' for t in need)} missing after 60s{why}")
        return bars

    # ------------------------------------------------------------------ entry
    def _try_entry(self, res: OrbResult) -> None:
        tk = res.ticker
        if self.no_new_entries:
            self._skip(tk, "no_new_entries")
            return
        if not self._settle_orders():
            self._skip(tk, "unsettled_order", ", ".join(sorted(self.orders.unsettled)))
            return
        ok, why = may_enter(self._trades_today(), self._busy(), self.settings.risk)
        if not ok:
            self._skip(tk, str(why))
            return
        kind = "call" if res.direction == "long" else "put"
        try:
            chain = self.data.option_chain(tk, kind, self.today)
        except Exception as exc:  # a data error skips this signal, not the day
            self._skip(tk, "chain_error", repr(exc))
            return
        pick = pick_contract(res.direction, chain, self.today, self.settings.contract)
        if pick.quote is None:
            self._skip(tk, str(pick.skip_reason))
            return
        quote = pick.quote
        for _ in range(2):  # the original ask plus one re-ask after a price move
            ticket = self._ticket(res, quote)
            if ticket is None:
                self._skip(tk, "too_expensive", f"{quote.symbol} ask {quote.ask:.2f}")
                return
            self.journal.record_ticket(ticket.id, self.clock.now(), ticket.to_dict(), "pending")
            decision = self.approver.request(ticket)
            self.journal.update_ticket(ticket.id, decision)
            if decision != "approved":
                self._skip(tk, decision, ticket.id)
                return
            try:
                bid, ask, ts = self.data.latest_option_quote(quote.symbol)
            except Exception as exc:
                self.journal.update_ticket(ticket.id, "stale_quote")
                self._skip(tk, "stale_quote", f"{quote.symbol} re-quote failed: {exc!r}")
                return
            if ask <= 0 or self.clock.now() - ts > QUOTE_STALE_AFTER:
                self.journal.update_ticket(ticket.id, "stale_quote")
                self._skip(tk, "stale_quote", f"{quote.symbol} {bid}/{ask} at {ts.isoformat()}")
                return
            new_limit = entry_limit_price(bid, ask)
            if round(abs(new_limit - ticket.limit) / ticket.limit, 9) <= REQUOTE_TOLERANCE:
                self.enter(ticket)
                return
            self.journal.update_ticket(ticket.id, "price_moved")
            recheck = pick_contract(res.direction, [replace(quote, bid=bid, ask=ask)],
                                    self.today, self.settings.contract)
            if recheck.quote is None:
                self._skip(tk, str(recheck.skip_reason), f"{quote.symbol} re-quoted {bid}/{ask}")
                return
            quote = recheck.quote
        self._skip(tk, "price_moved", f"{quote.symbol} re-quoted twice outside 5%")

    def _ticket(self, res: OrbResult, quote: OptionQuote) -> Ticket | None:
        limit = entry_limit_price(quote.bid, quote.ask)
        qty = contracts_for_budget(limit, self.settings.risk.budget_per_trade)
        if qty == 0:
            return None
        mid, close = res.range_mid, res.trigger_close
        risk = abs(close - mid)
        cfg = self.settings.exits
        tid = uuid.uuid4().hex
        self._trigger_closes[tid] = close  # the position's stock R uses the same close
        return Ticket(
            id=tid, created_at=self.clock.now(), ticker=res.ticker,
            direction=res.direction, option_symbol=quote.symbol, strike=quote.strike,
            expiry=quote.expiry, delta=quote.delta, bid=quote.bid, ask=quote.ask,
            limit=limit, qty=qty, cost=round(limit * qty * 100, 2),
            take_profit_price=round(limit * (1 + cfg.take_profit_pct), 2),
            stop_loss_price=round(limit * (1 - cfg.stop_loss_pct), 2),
            stock_stop=mid,
            stock_target=close + risk if res.direction == "long" else close - risk,
            flat_time=self.flat_time, checks=res.checks,
        )

    def enter(self, ticket: Ticket) -> Position | None:
        sym = ticket.option_symbol
        fill = self.orders.execute(
            lambda cid: self.broker.buy_limit(sym, ticket.qty, ticket.limit, cid), sym, "buy",
            ticket.qty, ENTRY_TIMEOUT,
        )
        if not fill.settled:  # broker positions decide the qty until the order settles
            self._pending_entry = (ticket, fill.order_id)
            self._settle_orders()
            return self.position
        filled = min(fill.qty, ticket.qty)
        if filled <= 0:
            self._skip(ticket.ticker, "not_filled", f"{sym} @ {ticket.limit:.2f}")
            return None
        return self._open(ticket, filled, fill.avg_price)

    def _open(self, ticket: Ticket, qty: int, avg_price: float | None) -> Position:
        pos = Position(  # stock R from the trigger close, as on the ticket and in the backtest
            ticker=ticket.ticker, direction=ticket.direction, option_symbol=ticket.option_symbol,
            qty=qty, entry_price=avg_price if avg_price is not None else ticket.limit,
            entry_stock_price=self._trigger_closes[ticket.id], range_mid=ticket.stock_stop, opened_at=self.clock.now(),
        )
        self._set_position(pos, self.journal.open_trade(pos))
        note = "" if qty == ticket.qty else f" (partial: {qty}/{ticket.qty})"
        self._event("info", f"entered {pos.option_symbol} x{qty} @ {pos.entry_price:.2f}{note}")
        return pos

    # ------------------------------------------------------- order settlement
    def _settle_orders(self) -> bool:
        """Book every order that settled since last time; True when none is still unsettled."""
        self.orders.refresh()
        for settled in self.orders.collect():
            self._book(settled)
        if self.orders.unsettled and self._pending_entry is not None:
            ticket = self._pending_entry[0]
            held = min(self.broker.positions().get(ticket.option_symbol, 0), ticket.qty)
            if self.position is None and held > 0:
                self._open(ticket, held, None)
            elif self.position is not None and 0 < held != self.position.qty:
                self._amend(held, self.position.entry_price, "broker position, entry unsettled")
        return not self.orders.unsettled

    def _book(self, s: Settled) -> None:
        st = s.status
        pending = self._pending_entry
        if s.side == "buy" and pending is not None and pending[1] == s.order_id:
            ticket, self._pending_entry = pending[0], None
            filled = min(st.filled_qty, ticket.qty)
            price = st.avg_price if st.avg_price is not None else ticket.limit
            if self.position is None and filled > 0:
                self._open(ticket, filled, price)
            elif self.position is None:
                self._skip(ticket.ticker, "not_filled", f"{ticket.option_symbol} (settled late)")
            elif filled > 0 and (filled, price) != (self.position.qty, self.position.entry_price):
                self._amend(filled, price, f"entry order {s.order_id} settled")
        elif s.side == "sell" and self.position is not None and s.order_id in self._exit_oids:
            filled = min(st.filled_qty, self._remaining())
            if filled > 0:
                self._book_sale(s.order_id, filled, st.avg_price, self._last_price)
        elif self.orphans.book(s):
            pass
        elif st.filled_qty:
            self._event("warn", f"order {s.order_id} ({s.side} {s.symbol}) settled with "
                                f"{st.filled_qty} filled and no open trade to book it to")

    def _book_sale(self, order_id: str, qty: int, avg: float | None, fallback: float) -> None:
        if avg is None:
            self._event("error", f"order {order_id} filled with no avg price; "
                                 f"booking at last known {fallback:.4f}")
            avg = fallback
        self._sold.append((qty, avg))

    # ------------------------------------------------------------------- exit
    def manage_position(self) -> None:
        try:
            self._manage()
        except (ExitPaused, UnsettledOrderError):
            pass  # journaled once when the order went unsettled; retried next pass
        except Exception:
            self._error()

    def _manage(self) -> None:
        all_settled = self._settle_orders()
        pos = self.position
        if pos is None:
            return
        if self._exit_unsettled_seen and self._pending_entry is None and self._held() == 0:
            self._close()  # the broker no longer holds it: close from booked fills
            return
        if not all_settled:
            return  # no exit decisions while any order is unsettled
        if self._exit_reason is not None:
            self.exit_position(self._exit_reason)  # resume an exit that was paused
            return
        try:
            stock = self.data.latest_stock_price(pos.ticker)
        except Exception as exc:  # no stock price: no exit decision this pass
            if not self._stock_warned:
                self._stock_warned = True
                self._event("warn", f"{pos.ticker} stock price unavailable ({exc!r}); "
                                    "waiting for the next pass")
            return
        self._stock_warned = False
        now = self.clock.now()
        try:
            bid, ask, ts = self.data.latest_option_quote(pos.option_symbol)
            mid: float | None = (bid + ask) / 2 if ask > 0 else None
            stale = f"stale since {ts.isoformat()}" if now - ts > QUOTE_STALE_AFTER else None
        except Exception as exc:  # treated like a stale quote
            mid, stale = None, f"unavailable ({exc!r})"
        if stale is not None:
            mid = None
            if not self._stale_warned:
                self._stale_warned = True
                self._event("warn", f"{pos.option_symbol} quote {stale}; "
                                    "using stock-based exits only")
        if mid is not None:
            self._last_price = mid
        reason = check_exit(pos, mid, stock, now, self.flat_time, self.settings.exits)
        if reason is not None:
            self.exit_position(reason)

    def _current_bid(self, symbol: str) -> float:
        try:
            bid = self.data.latest_option_quote(symbol)[0]
        except Exception as exc:
            self._event("warn", f"{symbol} quote unavailable for exit ({exc!r}); selling at market")
            return 0.0
        if bid > 0:
            self._last_price = bid
        return bid

    def _remaining(self) -> int:
        return self.position.qty - sum(q for q, _ in self._sold)

    def _to_sell(self) -> int:
        """Qty to sell now; capped at the broker-held qty once an exit order went unsettled."""
        left = self._remaining()
        if left > 0 and self._exit_unsettled_seen:
            left = min(left, self._held())
        return left

    def _record_exit(self, fill: Fill, qty: int, fallback: float) -> None:
        if fill.order_id:  # "" = not placed
            self._exit_oids.add(fill.order_id)
        if not fill.settled:
            self._exit_unsettled_seen = True
            raise ExitPaused(f"exit order {fill.order_id} unsettled")
        filled = min(fill.qty, qty)
        if filled > 0:
            self._book_sale(fill.order_id, filled, fill.avg_price, fallback)

    def exit_position(self, reason: ExitReason) -> ClosedTrade:
        if self.position is None or self._trade_id is None:
            raise RuntimeError("exit_position called with no open position")
        self._exit_reason = self._exit_reason or reason
        if not self._settle_orders():
            raise ExitPaused("an order is unsettled; exit resumes once it settles")
        sym = self.position.option_symbol
        for _ in range(EXIT_LIMIT_ATTEMPTS):
            qty = self._to_sell()
            if qty <= 0:
                break
            bid = self._current_bid(sym)
            if bid <= 0:
                break
            fill = self.orders.execute(lambda cid: self.broker.sell_limit(sym, qty, bid, cid),
                                       sym, "sell", qty, EXIT_LIMIT_TIMEOUT)
            self._record_exit(fill, qty, bid)
        qty = self._to_sell()
        if qty > 0:
            fill = self.orders.execute(lambda cid: self.broker.sell_market(sym, qty, cid),
                                       sym, "sell", qty, EXIT_MARKET_TIMEOUT)
            self._record_exit(fill, qty, self._last_price)
            if self._to_sell() > 0:
                self._event("error", f"exit of {sym} incomplete: {self._to_sell()} still held")
                raise RuntimeError(f"exit of {sym} incomplete")
        return self._close()

    def _close(self) -> ClosedTrade:
        """Close the open trade from booked exit fills (any unseen remainder at last price)."""
        sym, left = self.position.option_symbol, self._remaining()
        if left > 0:
            self._event("warn", f"{sym}: {left} no longer held but no fill seen; "
                                f"booked at last known {self._last_price:.4f}")
            self._sold.append((left, self._last_price))
        reason = self._exit_reason or "reconciled_missing"
        exit_price = round(sum(q * p for q, p in self._sold) / sum(q for q, _ in self._sold), 4)
        closed = self.journal.close_trade(self._trade_id, exit_price, reason, self.clock.now())
        self._set_position(None, None)
        self._event("info", f"exited {sym} @ {exit_price:.4f} ({reason}), pnl {closed.pnl:+.2f}")
        return closed

    def flatten_orphans(self) -> None:
        """Sell unknown broker positions at market (from flat time on, every pass)."""
        try:
            self._settle_orders()
            self.orphans.flatten()
        except UnsettledOrderError:
            pass  # retried on the next pass
        except Exception:
            self._error()

    # ---------------------------------------------------------------- the day
    def _beat(self) -> None:
        """Heartbeat from inside waits: a failed write must never interrupt trading."""
        try:
            self.write_heartbeat()
        except Exception as exc:
            if not self._beat_warned:
                self._beat_warned = True
                self._event("warn", f"heartbeat write failed: {exc!r}")

    def write_heartbeat(self) -> None:
        state_dir = self.settings.state_dir
        state_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "ts": self.clock.now().isoformat(),
            "position": self.position.option_symbol if self.position is not None else None,
            "day_pnl": self._day_pnl(),
        }
        fd, tmp = tempfile.mkstemp(dir=state_dir, suffix=".tmp")
        with os.fdopen(fd, "w") as fh:
            json.dump(payload, fh)
        os.replace(tmp, state_dir / f"heartbeat-{self.mode}.json")

    def _summary(self) -> None:
        trades = self._trades_today()
        wins = sum(1 for t in trades if t.pnl > 0)
        losses = sum(1 for t in trades if t.pnl < 0)
        held = f"; STILL HOLDING {self.position.option_symbol}" if self.position else ""
        self._event("info", f"day summary {self.today.isoformat()}: {len(trades)} trades, "
                            f"{wins} wins, {losses} losses, pnl {self._day_pnl():+.2f}{held}")

    def run_day(self) -> None:
        self.today = self.clock.now().date()
        self.clock.sleep_until(self._at(STARTUP_AT))
        self._beat()
        if not self._start() or self.hours is None:
            return
        afford_at = self._at(AFFORDABILITY_AT)
        afford_retry_at = self._at(AFFORDABILITY_RETRY_AT)
        flat_at = self._at(self.flat_time)
        end_at = self.hours.close + SUMMARY_AFTER_CLOSE
        step = timedelta(minutes=self.settings.strategy.bar_minutes)
        closes: list[datetime] = []
        close = self._at(self.settings.strategy.entry_start)
        while close.time() <= self.entry_end:
            closes.append(close)
            close += step
        # after a restart, never act on bar closes that have already passed
        pending = [c for c in closes if c + BAR_FETCH_DELAY >= self.clock.now()]
        afford_done = flattened = False
        next_manage = self.clock.now()

        while True:
            now = self.clock.now()
            self._beat()
            if now >= end_at:
                self._summary()
                self._beat()
                return
            if not afford_done and now >= afford_at:
                afford_done = True
                try:
                    self.affordability_filter()
                except Exception:
                    self._error()
            elif afford_done and self._afford_retry and now >= afford_retry_at:
                try:
                    self.retry_affordability()
                except Exception:
                    self._error()
            elif pending and now >= pending[0] + BAR_FETCH_DELAY:
                self.on_bar_close(pending.pop(0))
            elif not flattened and now >= flat_at:
                flattened = True
                if self.position is not None:
                    try:
                        self.exit_position("time")
                    except (ExitPaused, UnsettledOrderError):
                        pass  # managed every 15 s until it settles
                    except Exception:
                        self._error()
                self.flatten_orphans()
            elif (self._busy() or (flattened and self.orphans.held)) and now >= next_manage:
                if self._busy():
                    self.manage_position()
                if flattened:
                    self.flatten_orphans()
                next_manage = self.clock.now() + MANAGE_EVERY
            else:
                events = [end_at, now + HEARTBEAT_EVERY]
                if not afford_done:
                    events.append(afford_at)
                elif self._afford_retry:
                    events.append(afford_retry_at)
                if pending:
                    events.append(pending[0] + BAR_FETCH_DELAY)
                if not flattened:
                    events.append(flat_at)
                if self._busy() or (flattened and self.orphans.held):
                    events.append(next_manage)
                self.clock.sleep_until(min(events))
