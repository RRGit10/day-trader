"""Option contract selection and position sizing."""
from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from daytrader.config import ContractConfig
from daytrader.models import ContractPick, Direction, OptionQuote


def pick_contract(
    direction: Direction,
    quotes: Sequence[OptionQuote],
    today: date,
    cfg: ContractConfig,
) -> ContractPick:
    want = "call" if direction == "long" else "put"

    # (1) type match, expiry strictly after today, nearest such expiry only
    cands = [x for x in quotes if x.type == want and x.expiry > today]
    if not cands:
        return ContractPick(None, "no_expiry")
    nearest = min(x.expiry for x in cands)
    cands = [x for x in cands if x.expiry == nearest]

    # (2) delta band
    cands = [
        x for x in cands
        if x.delta is not None and cfg.delta_min <= abs(x.delta) <= cfg.delta_max
    ]
    if not cands:
        return ContractPick(None, "no_delta_match")

    # (3) liquidity
    cands = [
        x for x in cands
        if x.bid > 0
        and x.spread_pct <= cfg.max_spread_pct
        and (x.open_interest is None or x.open_interest >= cfg.min_open_interest)
    ]
    if not cands:
        return ContractPick(None, "illiquid")

    # (4) price cap
    cands = [x for x in cands if x.ask <= cfg.max_price]
    if not cands:
        return ContractPick(None, "too_expensive")

    best = min(cands, key=lambda x: (abs(abs(x.delta) - cfg.delta_target), x.ask))
    return ContractPick(best, None)


def contracts_for_budget(price: float, budget: float) -> int:
    if price <= 0:
        return 0
    # epsilon guards against float error (e.g. 0.10 * 100 != 10.0 exactly)
    return max(0, math.floor(budget / (price * 100) + 1e-9))


def entry_limit_price(bid: float, ask: float) -> float:
    """Mid rounded half-up to a valid tick: $0.01 below $3.00, else $0.05."""
    mid = (Decimal(str(bid)) + Decimal(str(ask))) / 2
    tick = Decimal("0.01") if mid < Decimal("3.00") else Decimal("0.05")
    ticks = (mid / tick).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return float((ticks * tick).quantize(Decimal("0.01")))
