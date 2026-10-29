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

from .approval import Approver, Ticket
from .broker import Broker, OrderStatus
from .clock import Clock, MarketHours, session_times
from .config import Settings
from .contracts import contracts_for_budget, entry_limit_price, pick_contract
from .data import MarketData
from .exits import check_exit
from .guard import may_enter
from .journal import Journal
from .models import ET, Bar, ClosedTrade, ExitReason, OptionQuote, OrbResult, Position
from .orb import evaluate_orb

STARTUP_AT = time(9, 25)
AFFORDABILITY_AT = time(9, 40)
BAR_FETCH_DELAY = timedelta(seconds=5)
BAR_RETRY_EVERY = timedelta(seconds=10)
BAR_RETRY_FOR = timedelta(seconds=60)
MANAGE_EVERY = timedelta(seconds=15)
QUOTE_STALE_AFTER = timedelta(seconds=60)
ORDER_POLL = timedelta(seconds=2)
ENTRY_TIMEOUT = timedelta(seconds=60)
EXIT_LIMIT_TIMEOUT = timedelta(seconds=10)
EXIT_LIMIT_ATTEMPTS = 3
EXIT_MARKET_TIMEOUT = timedelta(seconds=60)
REQUOTE_TOLERANCE = 0.05
SUMMARY_AFTER_CLOSE = timedelta(minutes=5)
PRIOR_DAY_LOOKBACK = 7
_TERMINAL = ("filled", "canceled", "rejected")


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
        self.clock = clock

        self.today = clock.now().date()
        self.hours: MarketHours | None = None
        self.entry_end: time = settings.strategy.entry_end
        self.flat_time: time = settings.exits.flat_time
        self.scan_list: list[str] = []
        self.prior_bars: dict[str, list[Bar]] = {}
        self.signaled: set[tuple[str, str]] = set()
        self.no_new_entries = False

        self.position: Position | None = None
        self._trade_id: int | None = None
        self._sold: list[tuple[int, float]] = []  # exit fills (qty, price) for the open position
        self._stale_warned = False

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

    def _set_position(self, pos: Position | None, trade_id: int | None) -> None:
        self.position, self._trade_id = pos, trade_id
        self._sold = []
        self._stale_warned = False

    def _settle(self, order_id: str, qty: int, timeout: timedelta) -> OrderStatus:
        """Poll until filled/terminal or timeout; on timeout cancel, then re-read the status.

        Cancel may raise even when it succeeded, so the re-read status is always what we act on.
        """
        deadline = self.clock.now() + timeout
        while True:
            st = self.broker.order_status(order_id)
            if st.filled_qty >= qty or st.status in _TERMINAL:
                return st
            if self.clock.now() >= deadline:
                break
            self.clock.sleep_until(min(self.clock.now() + ORDER_POLL, deadline))
        try:
            self.broker.cancel(order_id)
        except Exception as exc:
            self._event("warn", f"cancel {order_id} raised ({exc!r}); re-reading status")
        return self.broker.order_status(order_id)

    # ---------------------------------------------------------------- startup
    def startup(self) -> None:
        self.today = self.clock.now().date()
        self.hours = self.data.market_hours(self.today)
        if self.hours is None:
            self._event("info", f"market closed on {self.today.isoformat()}")
            return
        self.entry_end, self.flat_time = session_times(
            self.hours, self.settings.exits, self.settings.strategy
        )
        self._reconcile()
        self._load_prior_bars()

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
            if held[sym] != pos.qty:
                self._event("warn", f"{sym}: journal qty {pos.qty} != broker qty {held[sym]}; "
                                    f"managing {min(pos.qty, held[sym])}")
                pos = replace(pos, qty=min(pos.qty, held[sym]))
            self._set_position(pos, trade_id)
            self._event("info", f"resumed managing trade {trade_id} {sym} x{pos.qty}")
        for sym, qty in held.items():
            if sym not in known:
                self._event("warn", f"broker position {sym} x{qty} not in journal: not managed")

    def _load_prior_bars(self) -> None:
        day = self.today
        try:
            for _ in range(PRIOR_DAY_LOOKBACK):
                day -= timedelta(days=1)
                hours = self.data.market_hours(day)
                if hours is not None:
                    self.prior_bars = self.data.bars_5min(
                        list(self.settings.tickers), hours.open, hours.close
                    )
                    return
            self._event("warn", "no prior trading day found for indicator warm-up")
        except Exception:
            self._event("warn", "prior-day warm-up failed:\n" + traceback.format_exc())

    # ---------------------------------------------------------- affordability
    def affordability_filter(self) -> list[str]:
        kinds = ["call", "put"] if self.settings.strategy.shorts_enabled else ["call"]
        kept: list[str] = []
        for tk in self.settings.tickers:
            reasons: list[str] = []
            for kind in kinds:
                try:
                    chain = self.data.option_chain(tk, kind, self.today)
                except Exception as exc:
                    reasons.append("chain_error")
                    self._event("warn", f"{tk} {kind} chain failed at affordability: {exc!r}")
                    continue
                direction = "long" if kind == "call" else "short"
                pick = pick_contract(direction, chain, self.today, self.settings.contract)
                if pick.quote is not None:
                    kept.append(tk)
                    break
                reasons.append(str(pick.skip_reason))
            else:
                detail = "; ".join(f"{k}: {r}" for k, r in zip(kinds, reasons))
                self._skip(tk, reasons[0], detail)
        self.scan_list = kept
        return kept

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
        start = self.hours.open if self.hours is not None else self._at(time(9, 30))
        last_start = close_time - timedelta(minutes=self.settings.strategy.bar_minutes)

        def fetch(tickers: list[str]) -> dict[str, list[Bar]]:
            got = self.data.bars_5min(tickers, start, close_time)
            out = {}
            for tk, bs in got.items():
                bs = [b for b in bs if b.t < close_time]  # never use an in-progress bar
                if any(b.t == last_start for b in bs):
                    out[tk] = bs
            return out

        bars = fetch(list(self.scan_list))
        missing = [tk for tk in self.scan_list if tk not in bars]
        deadline = self.clock.now() + BAR_RETRY_FOR
        while missing and self.clock.now() < deadline:
            self.clock.sleep_until(min(self.clock.now() + BAR_RETRY_EVERY, deadline))
            if self.position is not None:
                self.manage_position()
            bars.update(fetch(missing))
            missing = [tk for tk in missing if tk not in bars]
        for tk in missing:
            self._skip(tk, "no_bars", f"bar {last_start:%H:%M} missing after 60s")
        return bars

    # ------------------------------------------------------------------ entry
    def _try_entry(self, res: OrbResult) -> None:
        tk = res.ticker
        if self.no_new_entries:
            self._skip(tk, "no_new_entries")
            return
        ok, why = may_enter(self._trades_today(), self.position is not None, self.settings.risk)
        if not ok:
            self._skip(tk, str(why))
            return
        kind = "call" if res.direction == "long" else "put"
        chain = self.data.option_chain(tk, kind, self.today)
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
            bid, ask, _ = self.data.latest_option_quote(quote.symbol)
            new_limit = entry_limit_price(bid, ask)
            if round(abs(new_limit - ticket.limit) / ticket.limit, 9) <= REQUOTE_TOLERANCE:
                self.enter(ticket)
                return
            self.journal.update_ticket(ticket.id, "price_moved")
            quote = replace(quote, bid=bid, ask=ask)
        self._skip(tk, "price_moved", f"{quote.symbol} re-quoted twice outside 5%")

    def _ticket(self, res: OrbResult, quote: OptionQuote) -> Ticket | None:
        limit = entry_limit_price(quote.bid, quote.ask)
        qty = contracts_for_budget(limit, self.settings.risk.budget_per_trade)
        if qty == 0:
            return None
        mid, close = res.range_mid, res.trigger_close
        risk = abs(close - mid)
        cfg = self.settings.exits
        return Ticket(
            id=uuid.uuid4().hex, created_at=self.clock.now(), ticker=res.ticker,
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
        order_id = self.broker.buy_limit(ticket.option_symbol, ticket.qty, ticket.limit)
        st = self._settle(order_id, ticket.qty, ENTRY_TIMEOUT)
        filled = min(st.filled_qty, ticket.qty)
        if filled <= 0:
            self._skip(ticket.ticker, "not_filled", f"{ticket.option_symbol} @ {ticket.limit:.2f}")
            return None
        try:
            stock = self.data.latest_stock_price(ticket.ticker)
        except Exception as exc:
            stock = (ticket.stock_target + ticket.stock_stop) / 2  # = the trigger close
            self._event("warn", f"stock price unavailable after fill ({exc!r}); using trigger close")
        pos = Position(
            ticker=ticket.ticker, direction=ticket.direction, option_symbol=ticket.option_symbol,
            qty=filled, entry_price=st.avg_price if st.avg_price is not None else ticket.limit,
            entry_stock_price=stock, range_mid=ticket.stock_stop, opened_at=self.clock.now(),
        )
        self._set_position(pos, self.journal.open_trade(pos))
        note = "" if filled == ticket.qty else f" (partial: {filled}/{ticket.qty})"
        self._event("info", f"entered {pos.option_symbol} x{filled} @ {pos.entry_price:.2f}{note}")
        return pos

    # ------------------------------------------------------------------- exit
    def manage_position(self) -> None:
        try:
            self._manage()
        except Exception:
            self._error()

    def _manage(self) -> None:
        pos = self.position
        if pos is None:
            return
        bid, ask, ts = self.data.latest_option_quote(pos.option_symbol)
        stock = self.data.latest_stock_price(pos.ticker)
        now = self.clock.now()
        mid: float | None = (bid + ask) / 2 if ask > 0 else None
        if now - ts > QUOTE_STALE_AFTER:
            mid = None
            if not self._stale_warned:
                self._stale_warned = True
                self._event("warn", f"{pos.option_symbol} quote stale since {ts.isoformat()}; "
                                    "using stock-based exits only")
        reason = check_exit(pos, mid, stock, now, self.flat_time, self.settings.exits)
        if reason is not None:
            self.exit_position(reason)

    def _current_bid(self, symbol: str) -> float:
        try:
            return self.data.latest_option_quote(symbol)[0]
        except Exception as exc:
            self._event("warn", f"{symbol} quote unavailable for exit ({exc!r}); selling at market")
            return 0.0

    def _record_exit_fill(self, st: OrderStatus, remaining: int, fallback: float) -> int:
        filled = min(st.filled_qty, remaining)
        if filled > 0:
            self._sold.append((filled, st.avg_price if st.avg_price is not None else fallback))
        return filled

    def exit_position(self, reason: ExitReason) -> ClosedTrade:
        pos = self.position
        if pos is None or self._trade_id is None:
            raise RuntimeError("exit_position called with no open position")
        sym = pos.option_symbol
        remaining = pos.qty - sum(q for q, _ in self._sold)
        bid = 0.0
        for _ in range(EXIT_LIMIT_ATTEMPTS):
            if remaining <= 0:
                break
            bid = self._current_bid(sym)
            if bid <= 0:
                break
            st = self._settle(self.broker.sell_limit(sym, remaining, bid), remaining,
                              EXIT_LIMIT_TIMEOUT)
            remaining -= self._record_exit_fill(st, remaining, bid)
        if remaining > 0:
            st = self._settle(self.broker.sell_market(sym, remaining), remaining,
                              EXIT_MARKET_TIMEOUT)
            remaining -= self._record_exit_fill(st, remaining, bid)
            if remaining > 0:
                self._event("error", f"exit of {sym} incomplete: {remaining} still held")
                raise RuntimeError(f"exit of {sym} incomplete: {remaining} still held")
        exit_price = round(sum(q * p for q, p in self._sold) / sum(q for q, _ in self._sold), 4)
        closed = self.journal.close_trade(self._trade_id, exit_price, reason, self.clock.now())
        self._set_position(None, None)
        self._event("info", f"exited {sym} @ {exit_price:.4f} ({reason}), pnl {closed.pnl:+.2f}")
        return closed

    # ---------------------------------------------------------------- the day
    def _write_heartbeat(self) -> None:
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
        self.startup()
        if self.hours is None:
            return
        afford_at = self._at(AFFORDABILITY_AT)
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
            self._write_heartbeat()
            if now >= end_at:
                self._summary()
                self._write_heartbeat()
                return
            if not afford_done and now >= afford_at:
                afford_done = True
                try:
                    self.affordability_filter()
                except Exception:
                    self._error()
            elif pending and now >= pending[0] + BAR_FETCH_DELAY:
                self.on_bar_close(pending.pop(0))
            elif not flattened and now >= flat_at:
                flattened = True
                if self.position is not None:
                    try:
                        self.exit_position("time")
                    except Exception:
                        self._error()
            elif self.position is not None and now >= next_manage:
                self.manage_position()
                next_manage = self.clock.now() + MANAGE_EVERY
            else:
                events = [end_at]
                if not afford_done:
                    events.append(afford_at)
                if pending:
                    events.append(pending[0] + BAR_FETCH_DELAY)
                if not flattened:
                    events.append(flat_at)
                if self.position is not None:
                    events.append(next_manage)
                self.clock.sleep_until(min(events))
