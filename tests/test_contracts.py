from dataclasses import replace
from datetime import date

from daytrader.config import ContractConfig
from daytrader.contracts import contracts_for_budget, entry_limit_price, pick_contract
from daytrader.models import OptionQuote

TODAY = date(2026, 10, 28)
NEXT = date(2026, 10, 30)
LATER = date(2026, 11, 6)
CFG = ContractConfig()


def q(symbol="C1", type="call", delta=0.65, bid=2.00, ask=2.05, oi=1000,
      expiry=NEXT, underlying="AAPL", strike=200.0):
    return OptionQuote(symbol, underlying, type, strike, expiry, bid, ask, delta, oi)


def test_picks_closest_to_target_and_excludes_out_of_band():
    quotes = [q("A", delta=0.62), q("B", delta=0.66), q("C", delta=0.71)]
    pick = pick_contract("long", quotes, TODAY, CFG)
    assert pick.skip_reason is None
    assert pick.quote.symbol == "B"


def test_tie_goes_to_lower_ask():
    quotes = [q("A", delta=0.63, bid=2.20, ask=2.25), q("B", delta=0.67, bid=2.00, ask=2.05)]
    assert pick_contract("long", quotes, TODAY, CFG).quote.symbol == "B"


def test_puts_use_abs_delta():
    quotes = [
        q("P1", type="put", delta=-0.62),
        q("P2", type="put", delta=-0.66),
        q("C1", type="call", delta=0.65),
    ]
    pick = pick_contract("short", quotes, TODAY, CFG)
    assert pick.quote.symbol == "P2"


def test_same_day_expiry_ignored_and_nearest_expiry_wins_over_better_delta():
    quotes = [
        q("TODAY", expiry=TODAY, delta=0.65),
        q("NEAR", expiry=NEXT, delta=0.61),
        q("FAR", expiry=LATER, delta=0.65),
    ]
    assert pick_contract("long", quotes, TODAY, CFG).quote.symbol == "NEAR"


def test_no_expiry():
    assert pick_contract("long", [], TODAY, CFG).skip_reason == "no_expiry"
    only_today = [q(expiry=TODAY)]
    assert pick_contract("long", only_today, TODAY, CFG).skip_reason == "no_expiry"
    wrong_type = [q(type="put", delta=-0.65)]
    assert pick_contract("long", wrong_type, TODAY, CFG).skip_reason == "no_expiry"


def test_no_delta_match():
    for d in (None, 0.50, 0.80):
        pick = pick_contract("long", [q(delta=d)], TODAY, CFG)
        assert pick.quote is None
        assert pick.skip_reason == "no_delta_match"


def test_illiquid_variants():
    for bad in (q(bid=0.0, ask=0.05), q(bid=1.80, ask=2.20), q(oi=100)):
        pick = pick_contract("long", [bad], TODAY, CFG)
        assert pick.skip_reason == "illiquid"


def test_open_interest_none_passes_liquidity():
    pick = pick_contract("long", [q(oi=None)], TODAY, CFG)
    assert pick.skip_reason is None
    assert pick.quote.open_interest is None


def test_too_expensive():
    pick = pick_contract("long", [q(bid=5.95, ask=6.00)], TODAY, CFG)
    assert pick.quote is None
    assert pick.skip_reason == "too_expensive"


def test_max_price_boundary_inclusive():
    pick = pick_contract("long", [q(bid=4.95, ask=5.00)], TODAY, replace(CFG))
    assert pick.skip_reason is None


def test_contracts_for_budget():
    assert contracts_for_budget(4.80, 500) == 1
    assert contracts_for_budget(2.40, 500) == 2
    assert contracts_for_budget(5.01, 500) == 0
    assert contracts_for_budget(0, 500) == 0
    assert contracts_for_budget(-1, 500) == 0
    assert contracts_for_budget(0.10, 10) == 1  # float-safe: 0.10*100 != 10.0 exactly


def test_entry_limit_price():
    assert entry_limit_price(2.40, 2.46) == 2.43
    assert entry_limit_price(4.10, 4.30) == 4.20
    assert entry_limit_price(4.10, 4.33) == 4.20  # mid 4.215 -> nearest 0.05
    assert entry_limit_price(2.40, 2.43) == 2.42  # mid 2.415 -> half-up on cent grid
    assert entry_limit_price(2.95, 3.04) == 3.00  # mid 2.995 -> 3.00
