"""Market data port and Alpaca adapter."""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta
from typing import Callable, Protocol, Sequence, TypeVar

from alpaca.data.enums import DataFeed, OptionsFeed
from alpaca.data.historical import OptionHistoricalDataClient, StockHistoricalDataClient
from alpaca.data.requests import (
    OptionChainRequest,
    OptionLatestQuoteRequest,
    StockBarsRequest,
    StockLatestTradeRequest,
)
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import AssetStatus, ContractType
from alpaca.trading.requests import GetCalendarRequest, GetOptionContractsRequest

from .clock import MarketHours
from .config import Keys
from .models import ET, Bar, OptionQuote, OptionType

T = TypeVar("T")


class DataError(Exception):
    """The data API answered but without the data asked for (e.g. no latest quote)."""


def with_retries(
    fn: Callable[[], T],
    tries: int = 3,
    delays: Sequence[float] = (1, 2, 4),
    sleep=time.sleep,
) -> T:
    for attempt in range(tries):
        try:
            return fn()
        except Exception:
            if attempt == tries - 1:
                raise
            sleep(delays[min(attempt, len(delays) - 1)])
    raise AssertionError("unreachable")  # pragma: no cover


class MarketData(Protocol):
    def market_hours(self, day: date) -> MarketHours | None: ...

    def bars_5min(
        self, tickers: Sequence[str], start: datetime, end: datetime
    ) -> dict[str, list[Bar]]: ...

    def option_chain(
        self, underlying: str, kind: OptionType, today: date
    ) -> list[OptionQuote]: ...

    def latest_stock_price(self, ticker: str) -> float: ...

    def latest_option_quote(self, symbol: str) -> tuple[float, float, datetime]: ...


def _et(dt: datetime) -> datetime:
    return dt.replace(tzinfo=ET) if dt.tzinfo is None else dt.astimezone(ET)


class AlpacaData:
    def __init__(self, keys: Keys):
        self._stock = StockHistoricalDataClient(keys.api_key, keys.api_secret)
        self._option = OptionHistoricalDataClient(keys.api_key, keys.api_secret)
        self._trading = TradingClient(keys.api_key, keys.api_secret, paper=True)

    def market_hours(self, day: date) -> MarketHours | None:
        req = GetCalendarRequest(start=day, end=day)
        cal = with_retries(lambda: self._trading.get_calendar(req))
        for entry in cal:
            if entry.date == day:
                return MarketHours(_et(entry.open), _et(entry.close))
        return None

    def bars_5min(
        self, tickers: Sequence[str], start: datetime, end: datetime
    ) -> dict[str, list[Bar]]:
        req = StockBarsRequest(
            symbol_or_symbols=list(tickers),
            timeframe=TimeFrame(5, TimeFrameUnit.Minute),
            start=start,
            end=end,
            feed=DataFeed.IEX,
        )
        raw = with_retries(lambda: self._stock.get_stock_bars(req)).data
        return {
            sym: [
                Bar(_et(b.timestamp), float(b.open), float(b.high), float(b.low),
                    float(b.close), float(b.volume))
                for b in bars
            ]
            for sym, bars in raw.items()
            if bars
        }

    def option_chain(
        self, underlying: str, kind: OptionType, today: date
    ) -> list[OptionQuote]:
        ctype = ContractType.CALL if kind == "call" else ContractType.PUT
        gte, lte = today + timedelta(days=1), today + timedelta(days=10)
        snap_req = OptionChainRequest(
            underlying_symbol=underlying,
            feed=OptionsFeed.INDICATIVE,
            type=ctype,
            expiration_date_gte=gte,
            expiration_date_lte=lte,
        )
        snaps = with_retries(lambda: self._option.get_option_chain(snap_req))

        contracts = {}
        token = None
        while True:
            req = GetOptionContractsRequest(
                underlying_symbols=[underlying],
                type=ctype,
                expiration_date_gte=gte,
                expiration_date_lte=lte,
                status=AssetStatus.ACTIVE,
                limit=1000,
                page_token=token,
            )
            resp = with_retries(lambda: self._trading.get_option_contracts(req))
            for c in resp.option_contracts:
                contracts[c.symbol] = c
            token = resp.next_page_token
            if not token:
                break

        out: list[OptionQuote] = []
        for sym, snap in snaps.items():
            c = contracts.get(sym)
            if c is None:
                continue
            quote = snap.latest_quote
            greeks = snap.greeks
            oi = c.open_interest
            out.append(
                OptionQuote(
                    symbol=sym,
                    underlying=underlying,
                    type=kind,
                    strike=float(c.strike_price),
                    expiry=c.expiration_date,
                    bid=float(quote.bid_price) if quote else 0.0,
                    ask=float(quote.ask_price) if quote else 0.0,
                    delta=float(greeks.delta) if greeks and greeks.delta is not None else None,
                    open_interest=int(oi) if oi is not None else None,
                )
            )
        return out

    def latest_stock_price(self, ticker: str) -> float:
        req = StockLatestTradeRequest(symbol_or_symbols=ticker, feed=DataFeed.IEX)
        trade = with_retries(lambda: self._stock.get_stock_latest_trade(req)).get(ticker)
        if trade is None:
            raise DataError(f"no quote for {ticker}")
        return float(trade.price)

    def latest_option_quote(self, symbol: str) -> tuple[float, float, datetime]:
        req = OptionLatestQuoteRequest(symbol_or_symbols=symbol, feed=OptionsFeed.INDICATIVE)
        q = with_retries(lambda: self._option.get_option_latest_quote(req)).get(symbol)
        if q is None:
            raise DataError(f"no quote for {symbol}")
        return float(q.bid_price), float(q.ask_price), _et(q.timestamp)
