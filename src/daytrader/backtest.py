"""Backtest: replay past trading days through the same pure decision modules as the session.

Conservative by construction: no option bar inside the trigger bar means no quote
("no_data"), stops are checked before profits within a bar, stops that gap fill at the
bar open while profits never get gap credit, and every fill pays half of the assumed spread.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Protocol, Sequence

from .clock import MarketHours, session_times
from .config import Settings
from .contracts import contracts_for_budget, pick_contract
from .guard import may_enter
from .history import AlpacaHistorical
from .journal import Journal
from .models import ET, Bar, ClosedTrade, ExitReason, OptionQuote, OptionType, OrbResult, Position
from .orb import evaluate_orb
from .pricing import bs_delta, implied_vol

__all__ = ["AlpacaHistorical", "BacktestSummary", "HistoricalSource", "run_backtest"]

CANDIDATE_STRIKES = 10
EXPIRY_CLOSE = time(16, 0)
PRIOR_DAY_LOOKBACK = timedelta(days=10)
YEAR_SECONDS = 365 * 24 * 3600
ONE_MIN = timedelta(minutes=1)


class HistoricalSource(Protocol):
    def trading_days(self, start: date, end: date) -> list[tuple[date, MarketHours]]: ...

    def stock_bars(self, ticker: str, day: date, minutes: int) -> list[Bar]: ...

    def option_contracts(
        self, underlying: str, kind: OptionType, expiry: date
    ) -> list[tuple[str, float]]: ...

    def expiries_after(self, underlying: str, day: date) -> list[date]: ...

    def option_bars_1min(self, symbol: str, day: date) -> list[Bar]: ...


@dataclass(frozen=True)
class BacktestSummary:
    days: int
    signals: int
    trades: int
    no_data_skips: int


@dataclass
class _Counts:
    signals: int = 0
    trades: int = 0
    no_data_skips: int = 0


def _in_hours(bars: Sequence[Bar], hours: MarketHours) -> list[Bar]:
    return sorted((b for b in bars if hours.open <= b.t < hours.close), key=lambda b: b.t)


class _Day:
    """One replayed trading day; mirrors Session's bar-close / entry / exit sequencing."""

    def __init__(self, source: HistoricalSource, settings: Settings, journal: Journal,
                 day: date, hours: MarketHours, prior: tuple[date, MarketHours] | None,
                 tickers: Sequence[str], counts: _Counts) -> None:
        self.src, self.settings, self.journal = source, settings, journal
        self.day, self.hours, self.tickers, self.counts = day, hours, list(tickers), counts
        self.entry_end, self.flat_time = session_times(hours, settings.exits, settings.strategy)
        self.h = settings.backtest.assumed_spread_pct / 2
        self.bars5 = {tk: _in_hours(source.stock_bars(tk, day, 5), hours) for tk in tickers}
        self.prior = {
            tk: _in_hours(source.stock_bars(tk, prior[0], 5), prior[1]) if prior is not None else []
            for tk in tickers
        }
        self.signaled: set[tuple[str, str]] = set()
        self.trades_today: list[ClosedTrade] = []
        self.busy_until: datetime | None = None  # exit time of the open position
        self._option_bars: dict[str, list[Bar]] = {}

    def _at(self, t: time) -> datetime:
        return datetime.combine(self.day, t, tzinfo=ET)

    def _skip(self, ts: datetime, ticker: str, reason: str, detail: str = "") -> None:
        if reason == "no_data":
            self.counts.no_data_skips += 1
        self.journal.record_skip(ts, ticker, reason, detail)

    def _obars(self, symbol: str) -> list[Bar]:
        if symbol not in self._option_bars:
            self._option_bars[symbol] = _in_hours(self.src.option_bars_1min(symbol, self.day),
                                                  self.hours)
        return self._option_bars[symbol]

    def run(self) -> None:
        step = timedelta(minutes=self.settings.strategy.bar_minutes)
        close = self._at(self.settings.strategy.entry_start)
        while close.time() <= self.entry_end:
            self.on_bar_close(close, step)
            close += step

    def on_bar_close(self, close: datetime, step: timedelta) -> None:
        results: list[OrbResult] = []
        for tk in self.tickers:
            bars = [b for b in self.bars5[tk] if b.t + step <= close]
            if not any(b.t == close - step for b in bars):
                self._skip(close, tk, "no_bars", f"bar {close - step:%H:%M} missing")
                continue
            res = evaluate_orb(tk, self.prior[tk], bars, self.settings.strategy, self.entry_end)
            self.journal.record_check(close, res)
            results.append(res)
        for res in results:  # ticker order; the guard allows only one open position
            key = (res.ticker, str(res.direction))
            if not res.triggered or key in self.signaled:
                continue
            self.signaled.add(key)
            self.counts.signals += 1
            self._try_entry(res, close)

    # ------------------------------------------------------------------ entry
    def _try_entry(self, res: OrbResult, close: datetime) -> None:
        tk = res.ticker
        position_open = self.busy_until is not None and close < self.busy_until
        ok, why = may_enter(self.trades_today, position_open, self.settings.risk)
        if not ok:
            self._skip(close, tk, str(why))
            return
        quotes = self._quotes(res, close)
        if not quotes:
            self._skip(close, tk, "no_data", "no option bars inside the trigger bar")
            return
        pick = pick_contract(res.direction, quotes, self.day, self.settings.contract)
        if pick.quote is None:
            self._skip(close, tk, str(pick.skip_reason))
            return
        quote = pick.quote
        entry = quote.ask
        qty = contracts_for_budget(entry, self.settings.risk.budget_per_trade)
        if qty == 0:
            self._skip(close, tk, "too_expensive", f"{quote.symbol} ask {entry:.2f}")
            return
        pos = Position(
            ticker=tk, direction=res.direction, option_symbol=quote.symbol, qty=qty,
            entry_price=entry, entry_stock_price=res.trigger_close, range_mid=res.range_mid,
            opened_at=close,
        )
        trade_id = self.journal.open_trade(pos)
        self.counts.trades += 1
        reason, exit_price, exit_at = self._walk(pos, quote.mid, close)
        self.trades_today.append(self.journal.close_trade(trade_id, exit_price, reason, exit_at))
        self.busy_until = exit_at

    def _quotes(self, res: OrbResult, close: datetime) -> list[OptionQuote]:
        """Synthetic quotes for the 10 strikes nearest the trigger close, nearest expiry."""
        expiries = [e for e in self.src.expiries_after(res.ticker, self.day) if e > self.day]
        if not expiries:
            return []
        expiry = min(expiries)
        kind: OptionType = "call" if res.direction == "long" else "put"
        spot = res.trigger_close
        contracts = sorted(self.src.option_contracts(res.ticker, kind, expiry),
                           key=lambda c: (abs(c[1] - spot), c[1]))[:CANDIDATE_STRIKES]
        years = (datetime.combine(expiry, EXPIRY_CLOSE, tzinfo=ET) - close).total_seconds() / YEAR_SECONDS
        r = self.settings.backtest.risk_free_rate
        quotes: list[OptionQuote] = []
        for symbol, strike in contracts:
            inside = [b for b in self._obars(symbol) if res.trigger_time <= b.t < close]
            if not inside:
                continue
            p = inside[-1].c
            iv = implied_vol(p, spot, strike, years, r, kind)
            delta = bs_delta(spot, strike, years, r, iv, kind) if iv is not None else None
            quotes.append(OptionQuote(
                symbol=symbol, underlying=res.ticker, type=kind, strike=strike, expiry=expiry,
                bid=p * (1 - self.h), ask=p * (1 + self.h), delta=delta, open_interest=None,
            ))
        return quotes

    # ------------------------------------------------------------------- exit
    def _walk(self, pos: Position, last: float, start: datetime) -> tuple[ExitReason, float, datetime]:
        """Walk 1-min bars from `start` to the flat time; returns (reason, fill, exit time)."""
        cfg, h = self.settings.exits, self.h
        stop = pos.entry_price * (1 - cfg.stop_loss_pct)
        target = pos.entry_price * (1 + cfg.take_profit_pct)
        long = pos.direction == "long"
        opt = {b.t: b for b in self._obars(pos.option_symbol)}
        stock = {b.t: b for b in _in_hours(self.src.stock_bars(pos.ticker, self.day, 1), self.hours)}
        flat = self._at(self.flat_time)
        t = start
        while t < flat:
            ob = opt.get(t)
            op, hi, lo, last = (ob.o, ob.h, ob.l, ob.c) if ob is not None else (last,) * 4
            sb = stock.get(t)
            done = t + ONE_MIN
            # stops: a gap through the level fills at the (worse) open; profits never get gap credit
            if round(lo, 6) <= round(stop, 6):
                return "stop_loss", min(op, stop) * (1 - h), done
            if sb is not None and ((sb.l <= pos.range_mid) if long else (sb.h >= pos.range_mid)):
                gapped = (sb.o <= pos.range_mid) if long else (sb.o >= pos.range_mid)
                return "stock_stop", (min(op, last) if gapped else last) * (1 - h), done
            if round(hi, 6) >= round(target, 6):
                return "take_profit", target * (1 - h), done
            if sb is not None and ((sb.h >= pos.stock_target) if long else (sb.l <= pos.stock_target)):
                return "stock_target", last * (1 - h), done
            t = done
        return "time", last * (1 - h), flat


def run_backtest(
    source: HistoricalSource,
    settings: Settings,
    start: date,
    end: date,
    journal: Journal,
    tickers: Sequence[str] | None = None,
) -> BacktestSummary:
    tickers = list(tickers) if tickers is not None else list(settings.tickers)
    calendar = source.trading_days(start - PRIOR_DAY_LOOKBACK, end)
    counts = _Counts()
    days = 0
    prior: tuple[date, MarketHours] | None = None
    for day, hours in calendar:
        if day >= start:
            days += 1
            _Day(source, settings, journal, day, hours, prior, tickers, counts).run()
        prior = (day, hours)
    return BacktestSummary(days=days, signals=counts.signals, trades=counts.trades,
                           no_data_skips=counts.no_data_skips)
