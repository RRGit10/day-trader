"""Opening-range-breakout rule with per-check results."""
from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Sequence

from daytrader.config import StrategyConfig
from daytrader.indicators import ema, rsi, session_vwap
from daytrader.models import Bar, Check, Direction, OrbResult

SESSION_OPEN = time(9, 30)


def _add_minutes(t: time, minutes: int) -> time:
    base = datetime(2000, 1, 1, t.hour, t.minute)
    return (base + timedelta(minutes=minutes)).time()


def evaluate_orb(
    ticker: str,
    prior_bars: Sequence[Bar],
    today_bars: Sequence[Bar],
    cfg: StrategyConfig,
    entry_end: time | None = None,
) -> OrbResult:
    end_limit = entry_end if entry_end is not None else cfg.entry_end
    range_end = _add_minutes(SESSION_OPEN, cfg.range_minutes)
    expected = cfg.range_minutes // cfg.bar_minutes

    range_bars = [b for b in today_bars if SESSION_OPEN <= b.t.time() < range_end]
    range_ok = len(range_bars) == expected
    checks: list[Check] = []

    if range_ok:
        range_high: float | None = max(b.h for b in range_bars)
        range_low: float | None = min(b.l for b in range_bars)
        checks.append(Check("range", True, f"range {range_low:.2f}-{range_high:.2f} ({expected} bars)"))
    else:
        range_high = range_low = None
        checks.append(
            Check("range", False, f"incomplete range ({len(range_bars)}/{expected} bars)")
        )

    # window
    if len(today_bars) < 2:
        checks.append(Check("window", False, "need at least 2 bars"))
    else:
        last = today_bars[-1]
        prev = today_bars[-2]
        close_time = _add_minutes(last.t.time(), cfg.bar_minutes)
        in_window = cfg.entry_start <= close_time <= end_limit
        after_range = prev.t.time() >= range_end and last.t.time() >= range_end
        detail = (
            f"trigger bar closes {close_time:%H:%M} "
            f"(window {cfg.entry_start:%H:%M}-{end_limit:%H:%M})"
        )
        if not after_range:
            detail += "; last two bars not both after range end"
        checks.append(Check("window", in_window and after_range, detail))

    # breakout
    direction: Direction | None = None
    if not range_ok:
        checks.append(Check("breakout", False, "incomplete range"))
    elif len(today_bars) < 2:
        checks.append(Check("breakout", False, "need at least 2 bars"))
    else:
        c1, c2 = today_bars[-2].c, today_bars[-1].c
        if c1 > range_high and c2 > range_high:
            direction = "long"
            checks.append(
                Check("breakout", True, f"closes {c1:.2f}, {c2:.2f} > range high {range_high:.2f}")
            )
        elif c1 < range_low and c2 < range_low:
            if cfg.shorts_enabled:
                direction = "short"
                checks.append(
                    Check("breakout", True, f"closes {c1:.2f}, {c2:.2f} < range low {range_low:.2f}")
                )
            else:
                checks.append(Check("breakout", False, "shorts disabled"))
        else:
            checks.append(
                Check(
                    "breakout",
                    False,
                    f"closes {c1:.2f}, {c2:.2f} not both beyond range "
                    f"{range_low:.2f}-{range_high:.2f}",
                )
            )

    # filters
    if not range_ok or direction is None:
        reason = "incomplete range" if not range_ok else "no breakout"
        checks.extend(Check(n, False, reason) for n in ("ema", "vwap", "rsi"))
    else:
        close = today_bars[-1].c
        closes = [b.c for b in prior_bars] + [b.c for b in today_bars]
        long = direction == "long"

        ema_v = ema(closes, cfg.ema_period)[-1]
        if ema_v is None:
            checks.append(Check("ema", False, "insufficient data for ema"))
        else:
            ok = close > ema_v if long else close < ema_v
            checks.append(Check("ema", ok, f"close {close:.2f} {'>' if long else '<'} ema {ema_v:.2f}"))

        vwap_v = session_vwap(today_bars)[-1]
        ok = close > vwap_v if long else close < vwap_v
        checks.append(Check("vwap", ok, f"close {close:.2f} {'>' if long else '<'} vwap {vwap_v:.2f}"))

        rsi_v = rsi(closes, cfg.rsi_period)[-1]
        if rsi_v is None:
            checks.append(Check("rsi", False, "insufficient data for rsi"))
        elif long:
            checks.append(
                Check("rsi", rsi_v > cfg.rsi_long_min, f"rsi {rsi_v:.1f} > {cfg.rsi_long_min:.1f}")
            )
        else:
            checks.append(
                Check("rsi", rsi_v < cfg.rsi_short_max, f"rsi {rsi_v:.1f} < {cfg.rsi_short_max:.1f}")
            )

    triggered = all(c.passed for c in checks)
    return OrbResult(
        ticker=ticker,
        triggered=triggered,
        direction=direction,
        checks=tuple(checks),
        range_high=range_high,
        range_low=range_low,
        trigger_time=today_bars[-1].t if triggered else None,
        trigger_close=today_bars[-1].c if triggered else None,
    )
