"""Exit rules for an open position."""
from __future__ import annotations

from datetime import datetime, time

from .config import ExitConfig
from .models import ExitReason, Position


def check_exit(
    pos: Position,
    option_mid: float | None,
    stock_price: float,
    now: datetime,
    flat_time: time,
    cfg: ExitConfig,
) -> ExitReason | None:
    """Return the first triggered exit reason, or None.

    Priority: stop_loss, stock_stop, take_profit, stock_target, time.
    All boundaries are inclusive; option mid and thresholds are rounded to 6 dp so
    float noise (e.g. 1.15 * 1.2) cannot hide an exact-boundary trigger.
    """
    if option_mid is not None:
        if round(option_mid, 6) <= round(pos.entry_price * (1 - cfg.stop_loss_pct), 6):
            return "stop_loss"

    long = pos.direction == "long"
    if (stock_price <= pos.range_mid) if long else (stock_price >= pos.range_mid):
        return "stock_stop"

    if option_mid is not None:
        if round(option_mid, 6) >= round(pos.entry_price * (1 + cfg.take_profit_pct), 6):
            return "take_profit"

    target = pos.stock_target
    if (stock_price >= target) if long else (stock_price <= target):
        return "stock_target"

    if now.time() >= flat_time:
        return "time"
    return None
