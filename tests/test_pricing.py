import pytest

from daytrader.pricing import bs_delta, bs_price, implied_vol

ARGS = dict(S=100.0, K=100.0, t=1.0, r=0.05, sigma=0.2)


def test_reference_prices():
    assert bs_price(**ARGS, kind="call") == pytest.approx(10.4506, abs=1e-4)
    assert bs_price(**ARGS, kind="put") == pytest.approx(5.5735, abs=1e-4)


def test_reference_deltas():
    assert bs_delta(**ARGS, kind="call") == pytest.approx(0.6368, abs=1e-4)
    assert bs_delta(**ARGS, kind="put") == pytest.approx(-0.3632, abs=1e-4)


@pytest.mark.parametrize("kind", ["call", "put"])
def test_implied_vol_roundtrip(kind):
    p = bs_price(100, 105, 0.5, 0.05, 0.35, kind)
    assert implied_vol(p, 100, 105, 0.5, 0.05, kind) == pytest.approx(0.35, abs=1e-4)


def test_implied_vol_below_intrinsic_is_none():
    # ITM call intrinsic ~ 20; price 5 is impossible
    assert implied_vol(5.0, 120, 100, 0.5, 0.05, "call") is None


def test_implied_vol_above_upper_bound_is_none():
    assert implied_vol(99.0, 100, 100, 0.5, 0.05, "call") is None


def test_implied_vol_nonpositive_t_or_price_is_none():
    assert implied_vol(5.0, 100, 100, 0.0, 0.05, "call") is None
    assert implied_vol(5.0, 100, 100, -1.0, 0.05, "call") is None
    assert implied_vol(0.0, 100, 100, 1.0, 0.05, "call") is None
