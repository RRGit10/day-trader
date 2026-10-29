import re
from types import SimpleNamespace as NS
from unittest.mock import MagicMock, patch

import pytest
from alpaca.common.exceptions import APIError
from alpaca.trading.enums import OrderSide, TimeInForce

from daytrader import broker as broker_mod
from daytrader.broker import AlpacaBroker, OrderStatus, PaperLockError, make_broker
from daytrader.config import ConfigError, Keys
from fakes import FakeBroker

KEYS = Keys("k", "s")


def make_broker_mocked():
    with patch.object(broker_mod, "TradingClient") as tc:
        b = AlpacaBroker(KEYS)
    return b, tc


def test_constructor_always_paper():
    b, tc = make_broker_mocked()
    tc.assert_called_once()
    assert tc.call_args.kwargs["paper"] is True
    with patch.object(broker_mod, "TradingClient") as tc2:
        make_broker(Keys("a", "b"), "approval")
        make_broker(Keys("a", "b"), "bot")
    assert [c.kwargs["paper"] for c in tc2.call_args_list] == [True, True]


@pytest.mark.parametrize("code", [401, 403])
def test_verify_paper_auth_rejection_is_paper_lock_error(code):
    b, _ = make_broker_mocked()
    b._client = MagicMock()
    b._client.get_account.side_effect = _api_error(code)
    with pytest.raises(PaperLockError, match="paper"):
        b.verify_paper()


@pytest.mark.parametrize("exc", [RuntimeError("net"), APIError('{"message": "x"}')])
def test_verify_paper_network_errors_are_not_paper_lock_errors(exc):
    b, _ = make_broker_mocked()
    b._client = MagicMock()
    b._client.get_account.side_effect = exc
    with pytest.raises(type(exc)):
        b.verify_paper()


def test_verify_paper_ok():
    b, _ = make_broker_mocked()
    b._client = MagicMock()
    b.verify_paper()
    b._client.get_account.assert_called_once()


def test_make_broker_missing_keys():
    with pytest.raises(ConfigError):
        make_broker(None, "bot")


def test_make_broker_verifies():
    with patch.object(broker_mod, "TradingClient") as tc:
        tc.return_value.get_account.side_effect = _api_error(401)
        with pytest.raises(PaperLockError):
            make_broker(KEYS, "bot")


def test_order_requests():
    b, _ = make_broker_mocked()
    b._client = MagicMock()
    b._client.submit_order.return_value = NS(id="abc")
    assert b.buy_limit("AAPL261030C00200000", 3, 1.25) == "abc"
    r = b._client.submit_order.call_args[0][0]
    assert (r.symbol, r.qty, r.side, r.limit_price, r.time_in_force) == (
        "AAPL261030C00200000", 3, OrderSide.BUY, 1.25, TimeInForce.DAY)
    b.sell_limit("X", 2, 2.5)
    r = b._client.submit_order.call_args[0][0]
    assert (r.symbol, r.qty, r.side, r.limit_price) == ("X", 2, OrderSide.SELL, 2.5)
    b.sell_market("X", 2)
    r = b._client.submit_order.call_args[0][0]
    assert (r.symbol, r.qty, r.side, r.time_in_force) == ("X", 2, OrderSide.SELL, TimeInForce.DAY)
    assert not hasattr(r, "limit_price")
    cids = [c[0][0].client_order_id for c in b._client.submit_order.call_args_list]
    assert all(re.fullmatch(r"[0-9a-f]{32}", c) for c in cids) and len(set(cids)) == 3
    b.buy_limit("X", 1, 1.0, client_order_id="mine")
    assert b._client.submit_order.call_args[0][0].client_order_id == "mine"


def _api_error(code):
    return APIError('{"message": "x"}', http_error=NS(response=NS(status_code=code)))


def test_order_by_client_id():
    from alpaca.trading.enums import OrderStatus as SdkStatus

    b, _ = make_broker_mocked()
    b._client = MagicMock()
    b._client.get_order_by_client_id.return_value = NS(
        id="oid1", status=SdkStatus.PARTIALLY_FILLED, filled_qty="1", filled_avg_price="2.5")
    assert b.order_by_client_id("c1") == ("oid1", OrderStatus("partially_filled", 1, 2.5))
    b._client.get_order_by_client_id.assert_called_once_with("c1")
    b._client.get_order_by_client_id.side_effect = _api_error(404)
    assert b.order_by_client_id("c2") is None
    b._client.get_order_by_client_id.side_effect = _api_error(500)
    with pytest.raises(APIError):
        b.order_by_client_id("c3")


def test_open_orders_and_cancel_all():
    from alpaca.trading.enums import QueryOrderStatus

    b, _ = make_broker_mocked()
    b._client = MagicMock()
    b._client.get_orders.return_value = [NS(id="a"), NS(id="b")]
    assert b.open_orders() == ["a", "b"]
    assert b._client.get_orders.call_args[0][0].status == QueryOrderStatus.OPEN
    b.cancel_all_open()
    b._client.cancel_orders.assert_called_once_with()


def test_submit_not_retried():
    b, _ = make_broker_mocked()
    b._client = MagicMock()
    b._client.submit_order.side_effect = RuntimeError("timeout")
    with pytest.raises(RuntimeError):
        b.buy_limit("X", 1, 1.0)
    assert b._client.submit_order.call_count == 1


@pytest.mark.parametrize("raw,mapped", [
    ("new", "new"), ("accepted", "new"), ("pending_new", "new"), ("held", "new"),
    ("partially_filled", "partially_filled"), ("filled", "filled"),
    ("canceled", "canceled"), ("expired", "canceled"), ("done_for_day", "canceled"),
    ("replaced", "canceled"),
    ("rejected", "rejected"), ("suspended", "rejected"), ("stopped", "rejected"),
    ("pending_cancel", "new"),
])
def test_status_mapping(raw, mapped):
    from alpaca.trading.enums import OrderStatus as SdkStatus

    b, _ = make_broker_mocked()
    b._client = MagicMock()
    b._client.get_order_by_id.return_value = NS(
        status=SdkStatus(raw), filled_qty="2", filled_avg_price="1.5")
    assert b.order_status("id") == OrderStatus(mapped, 2, 1.5)


def test_status_unfilled_has_no_price():
    from alpaca.trading.enums import OrderStatus as SdkStatus

    b, _ = make_broker_mocked()
    b._client = MagicMock()
    b._client.get_order_by_id.return_value = NS(
        status=SdkStatus.NEW, filled_qty="0", filled_avg_price=None)
    assert b.order_status("id") == OrderStatus("new", 0, None)


def test_positions_and_cancel():
    b, _ = make_broker_mocked()
    b._client = MagicMock()
    b._client.get_all_positions.return_value = [NS(symbol="A", qty="3"), NS(symbol="B", qty="1")]
    assert b.positions() == {"A": 3, "B": 1}
    b.cancel("id")
    b._client.cancel_order_by_id.assert_called_once_with("id")


def test_fake_partial_reports_half_filled():
    f = FakeBroker("partial")
    oid = f.buy_limit("X", 5, 1.0)
    assert oid == "o1"
    assert f.order_status(oid) == OrderStatus("partially_filled", 2, 1.0)
    assert f.positions() == {"X": 2}
    assert f.orders == [{"side": "buy", "symbol": "X", "qty": 5, "limit": 1.0, "type": "limit"}]


def test_fake_partial_single_lot_is_new():
    f = FakeBroker("partial")
    assert f.order_status(f.buy_limit("X", 1, 1.0)) == OrderStatus("new", 0, None)


def test_fake_fill_never_cancel_and_positions():
    f = FakeBroker()
    f.market_price["X"] = 2.0
    f.buy_limit("X", 2, 1.0)
    assert f.positions() == {"X": 2}
    oid = f.sell_market("X", 2)
    assert f.order_status(oid) == OrderStatus("filled", 2, 2.0)
    assert f.positions() == {}
    f.fill_policy = "never"
    oid = f.buy_limit("X", 2, 1.0)
    assert f.order_status(oid) == OrderStatus("new", 0, None)
    f.cancel(oid)
    assert f.order_status(oid).status == "canceled" and f.positions() == {}
