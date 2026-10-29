"""Daily risk guard: decides whether a new entry is allowed."""
from __future__ import annotations

from collections.abc import Sequence

from .config import RiskConfig
from .models import ClosedTrade


def may_enter(
    trades_today: Sequence[ClosedTrade], position_open: bool, cfg: RiskConfig
) -> tuple[bool, str | None]:
    if position_open:
        return False, "position_open"
    total = round(sum(t.pnl for t in trades_today), 2)  # cents: no float-noise misses
    if total >= cfg.daily_profit_target:
        return False, "profit_target_hit"
    if sum(1 for t in trades_today if t.pnl < 0) >= cfg.max_losses:
        return False, "max_losses"
    if total <= -cfg.daily_loss_limit:
        return False, "loss_limit"
    return True, None
