"""Shared data models."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

Direction = Literal["long", "short"]
OptionType = Literal["call", "put"]
ExitReason = Literal["stop_loss", "stock_stop", "take_profit", "stock_target", "time"]


@dataclass(frozen=True)
class Bar:
    t: datetime
    o: float
    h: float
    l: float
    c: float
    v: float


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class OrbResult:
    ticker: str
    triggered: bool
    direction: Direction | None
    checks: tuple[Check, ...]
    range_high: float | None
    range_low: float | None
    trigger_time: datetime | None
    trigger_close: float | None

    @property
    def range_mid(self) -> float | None:
        if self.range_high is None or self.range_low is None:
            return None
        return (self.range_high + self.range_low) / 2


@dataclass(frozen=True)
class OptionQuote:
    symbol: str
    underlying: str
    type: OptionType
    strike: float
    expiry: date
    bid: float
    ask: float
    delta: float | None
    open_interest: int | None

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def spread_pct(self) -> float:
        mid = self.mid
        if mid <= 0:
            return float("inf")
        return (self.ask - self.bid) / mid


@dataclass(frozen=True)
class ContractPick:
    quote: OptionQuote | None
    skip_reason: str | None


@dataclass(frozen=True)
class Position:
    ticker: str
    direction: Direction
    option_symbol: str
    qty: int
    entry_price: float
    entry_stock_price: float
    range_mid: float
    opened_at: datetime

    @property
    def risk_per_share(self) -> float:
        return abs(self.entry_stock_price - self.range_mid)

    @property
    def stock_target(self) -> float:
        r = self.risk_per_share
        return self.entry_stock_price + r if self.direction == "long" else self.entry_stock_price - r

    @property
    def cost(self) -> float:
        return self.entry_price * self.qty * 100


@dataclass(frozen=True)
class ClosedTrade:
    ticker: str
    pnl: float
    closed_at: datetime
    reason: str
