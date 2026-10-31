"""European Black-Scholes pricing (no dividends). `t` is in years."""
from __future__ import annotations

import math

from daytrader.models import OptionType

_SIGMA_LO = 0.01
_SIGMA_HI = 5.0


def _cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _d1_d2(S: float, K: float, t: float, r: float, sigma: float) -> tuple[float, float]:
    sd = sigma * math.sqrt(t)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * t) / sd
    return d1, d1 - sd


def bs_price(S: float, K: float, t: float, r: float, sigma: float, kind: OptionType) -> float:
    if t <= 0 or sigma <= 0:
        intrinsic = S - K if kind == "call" else K - S
        return max(intrinsic, 0.0)
    d1, d2 = _d1_d2(S, K, t, r, sigma)
    disc = K * math.exp(-r * t)
    if kind == "call":
        return S * _cdf(d1) - disc * _cdf(d2)
    return disc * _cdf(-d2) - S * _cdf(-d1)


def bs_delta(S: float, K: float, t: float, r: float, sigma: float, kind: OptionType) -> float:
    if t <= 0 or sigma <= 0:
        if kind == "call":
            return 1.0 if S > K else 0.0
        return -1.0 if S < K else 0.0
    d1, _ = _d1_d2(S, K, t, r, sigma)
    return _cdf(d1) if kind == "call" else _cdf(d1) - 1.0


def implied_vol(price: float, S: float, K: float, t: float, r: float, kind: OptionType) -> float | None:
    if t <= 0 or price <= 0:
        return None
    lo, hi = _SIGMA_LO, _SIGMA_HI
    if not (bs_price(S, K, t, r, lo, kind) <= price <= bs_price(S, K, t, r, hi, kind)):
        return None
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if bs_price(S, K, t, r, mid, kind) < price:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-6:
            break
    return 0.5 * (lo + hi)
