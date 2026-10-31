"""Technical indicators (pure Python)."""
from __future__ import annotations

from typing import Sequence

from daytrader.models import Bar


def ema(values: Sequence[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if period < 1 or len(values) < period:
        return out
    k = 2 / (period + 1)
    prev = sum(values[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def _rsi_value(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0
    return 100 - 100 / (1 + avg_gain / avg_loss)


def rsi(closes: Sequence[float], period: int = 14) -> list[float | None]:
    out: list[float | None] = [None] * len(closes)
    if period < 1 or len(closes) <= period:
        return out
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    avg_gain = sum(max(d, 0.0) for d in deltas[:period]) / period
    avg_loss = sum(max(-d, 0.0) for d in deltas[:period]) / period
    out[period] = _rsi_value(avg_gain, avg_loss)
    for i in range(period, len(deltas)):
        d = deltas[i]
        avg_gain = (avg_gain * (period - 1) + max(d, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-d, 0.0)) / period
        out[i + 1] = _rsi_value(avg_gain, avg_loss)
    return out


def session_vwap(bars: Sequence[Bar]) -> list[float]:
    out: list[float] = []
    pv = 0.0
    vol = 0.0
    for b in bars:
        pv += (b.h + b.l + b.c) / 3 * b.v
        vol += b.v
        out.append(pv / vol if vol > 0 else b.c)
    return out
