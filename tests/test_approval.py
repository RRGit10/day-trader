import json
from datetime import date, datetime, time

from daytrader.approval import AutoApprover, FileApprover, Ticket, write_decision
from daytrader.models import ET, Check
from tests.fakes import FakeClock

T0 = datetime(2026, 11, 2, 10, 0, tzinfo=ET)


def make_ticket(tid="t1") -> Ticket:
    return Ticket(
        id=tid, created_at=T0, ticker="TSLA", direction="long",
        option_symbol="TSLA261106C00360000", strike=360.0, expiry=date(2026, 11, 6),
        delta=0.64, bid=4.10, ask=4.30, limit=4.20, qty=1, cost=420.0,
        take_profit_price=5.04, stop_loss_price=3.78, stock_stop=358.32,
        stock_target=370.04, flat_time=time(15, 55),
        checks=(Check("rsi", True, "RSI 62"), Check("ema", False, "below")),
    )


def test_round_trip():
    t = make_ticket()
    d = json.loads(json.dumps(t.to_dict()))
    assert Ticket.from_dict(d) == t


def test_summary():
    s = make_ticket().summary()
    assert s == (
        "TSLA long — buy 1× 2026-11-06 $360 call (Δ0.64) at $4.20 limit, cost $420 = max loss. "
        "Exit at +20% ($5.04), −10% ($3.78), stock $358.32 / $370.04, or 15:55."
    )


def test_summary_short_put_fractional_strike():
    t = make_ticket()
    t = Ticket(**{**t.__dict__, "direction": "short", "strike": 362.5})
    s = t.summary()
    assert "$362.50 put" in s and "TSLA short" in s


def test_auto():
    assert AutoApprover().request(make_ticket()) == "approved"


def test_approve_before_timeout(tmp_path):
    clock = FakeClock(T0)
    polls = []

    def hook():
        polls.append(1)
        if len(polls) == 3:
            assert write_decision(tmp_path, "t1", "approve")

    a = FileApprover(tmp_path, clock, on_poll=hook)
    assert a.request(make_ticket()) == "approved"
    assert not (tmp_path / "approval/pending/t1.json").exists()
    assert not (tmp_path / "approval/decisions/t1.json").exists()
    assert not (tmp_path / "approval/expired/t1.json").exists()


def test_pending_file_visible_while_waiting(tmp_path):
    seen = []
    a = FileApprover(tmp_path, FakeClock(T0),
                     on_poll=lambda: seen.append((tmp_path / "approval/pending/t1.json").exists()))
    a.request(make_ticket())
    assert seen and all(seen)


def test_reject(tmp_path):
    a = FileApprover(tmp_path, FakeClock(T0),
                     on_poll=lambda: write_decision(tmp_path, "t1", "reject"))
    assert a.request(make_ticket()) == "rejected"
    assert not (tmp_path / "approval/pending/t1.json").exists()


def test_unrecognized_decision_is_rejected(tmp_path):
    def hook():
        (tmp_path / "approval/decisions").mkdir(parents=True, exist_ok=True)
        (tmp_path / "approval/decisions/t1.json").write_text('{"decision": "maybe"}')

    a = FileApprover(tmp_path, FakeClock(T0), on_poll=hook)
    assert a.request(make_ticket()) == "rejected"


def test_expires(tmp_path):
    clock = FakeClock(T0)
    a = FileApprover(tmp_path, clock)
    assert a.request(make_ticket()) == "expired"
    assert (clock.now() - T0).total_seconds() >= 120
    assert (tmp_path / "approval/expired/t1.json").exists()
    assert not (tmp_path / "approval/pending/t1.json").exists()


def test_write_decision_unknown_id(tmp_path):
    assert write_decision(tmp_path, "nope", "approve") is False
    assert not (tmp_path / "approval/decisions").exists() or not list(
        (tmp_path / "approval/decisions").iterdir())


def test_decision_after_expiry_ignored(tmp_path):
    a = FileApprover(tmp_path, FakeClock(T0))
    assert a.request(make_ticket()) == "expired"
    assert write_decision(tmp_path, "t1", "approve") is False
    assert (tmp_path / "approval/expired/t1.json").exists()
    assert not (tmp_path / "approval/decisions/t1.json").exists()


def test_stale_decision_not_honored(tmp_path):
    d = tmp_path / "approval/decisions"
    d.mkdir(parents=True)
    (d / "t1.json").write_text('{"decision": "approve"}')
    a = FileApprover(tmp_path, FakeClock(T0))
    assert a.request(make_ticket()) == "expired"
    assert not (d / "t1.json").exists()


def test_bad_ticket_ids(tmp_path):
    import pytest

    for bad in ("../x", "a/b", "a\\b", ".."):
        assert write_decision(tmp_path, bad, "approve") is False
        with pytest.raises(ValueError):
            FileApprover(tmp_path, FakeClock(T0)).request(make_ticket(bad))
