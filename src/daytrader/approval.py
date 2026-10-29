"""Trade approval: tickets, auto approver, and file-based approver."""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Callable, Literal, Protocol

from .clock import Clock
from .models import Check, Direction

Decision = Literal["approved", "rejected", "expired"]


def _money(x: float) -> str:
    return f"${x:,.2f}"


def _whole_or_money(x: float) -> str:
    return f"${x:,.0f}" if float(x).is_integer() else _money(x)


@dataclass(frozen=True)
class Ticket:
    id: str
    created_at: datetime
    ticker: str
    direction: Direction
    option_symbol: str
    strike: float
    expiry: date
    delta: float
    bid: float
    ask: float
    limit: float
    qty: int
    cost: float
    take_profit_price: float
    stop_loss_price: float
    stock_stop: float
    stock_target: float
    flat_time: time
    checks: tuple[Check, ...]

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "created_at": self.created_at.isoformat(),
            "ticker": self.ticker,
            "direction": self.direction,
            "option_symbol": self.option_symbol,
            "strike": self.strike,
            "expiry": self.expiry.isoformat(),
            "delta": self.delta,
            "bid": self.bid,
            "ask": self.ask,
            "limit": self.limit,
            "qty": self.qty,
            "cost": self.cost,
            "take_profit_price": self.take_profit_price,
            "stop_loss_price": self.stop_loss_price,
            "stock_stop": self.stock_stop,
            "stock_target": self.stock_target,
            "flat_time": self.flat_time.isoformat(),
            "checks": [
                {"name": c.name, "passed": c.passed, "detail": c.detail} for c in self.checks
            ],
        }

    @classmethod
    def from_dict(cls, d: dict) -> Ticket:
        return cls(
            id=d["id"],
            created_at=datetime.fromisoformat(d["created_at"]),
            ticker=d["ticker"],
            direction=d["direction"],
            option_symbol=d["option_symbol"],
            strike=d["strike"],
            expiry=date.fromisoformat(d["expiry"]),
            delta=d["delta"],
            bid=d["bid"],
            ask=d["ask"],
            limit=d["limit"],
            qty=d["qty"],
            cost=d["cost"],
            take_profit_price=d["take_profit_price"],
            stop_loss_price=d["stop_loss_price"],
            stock_stop=d["stock_stop"],
            stock_target=d["stock_target"],
            flat_time=time.fromisoformat(d["flat_time"]),
            checks=tuple(Check(c["name"], c["passed"], c["detail"]) for c in d["checks"]),
        )

    def summary(self) -> str:
        kind = "call" if self.direction == "long" else "put"
        tp_pct = (self.take_profit_price / self.limit - 1) * 100
        sl_pct = (1 - self.stop_loss_price / self.limit) * 100
        return (
            f"{self.ticker} {self.direction} — buy {self.qty}× {self.expiry.isoformat()} "
            f"{_whole_or_money(self.strike)} {kind} (Δ{self.delta:.2f}) at "
            f"{_money(self.limit)} limit, cost {_whole_or_money(self.cost)} = max loss. "
            f"Exit at +{tp_pct:.0f}% ({_money(self.take_profit_price)}), "
            f"−{sl_pct:.0f}% ({_money(self.stop_loss_price)}), "
            f"stock {_money(self.stock_stop)} / {_money(self.stock_target)}, "
            f"or {self.flat_time.strftime('%H:%M')}."
        )


class Approver(Protocol):
    def request(self, ticket: Ticket) -> Decision: ...


class AutoApprover:
    def request(self, ticket: Ticket) -> Decision:
        return "approved"


def _write_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _dirs(state_dir: Path) -> tuple[Path, Path, Path]:
    base = Path(state_dir) / "approval"
    return base / "pending", base / "decisions", base / "expired"


class FileApprover:
    def __init__(
        self,
        state_dir: Path,
        clock: Clock,
        timeout_s: int = 120,
        poll_s: float = 1.0,
        on_poll: Callable[[], None] | None = None,
    ) -> None:
        self.state_dir = Path(state_dir)
        self.clock = clock
        self.timeout_s = timeout_s
        self.poll_s = poll_s
        self.on_poll = on_poll

    def request(self, ticket: Ticket) -> Decision:
        pending_dir, decisions_dir, expired_dir = _dirs(self.state_dir)
        for d in (pending_dir, decisions_dir, expired_dir):
            d.mkdir(parents=True, exist_ok=True)
        pending = pending_dir / f"{ticket.id}.json"
        decision_file = decisions_dir / f"{ticket.id}.json"
        _write_atomic(pending, ticket.to_dict())
        deadline = self.clock.now() + timedelta(seconds=self.timeout_s)
        try:
            while True:
                if self.on_poll is not None:
                    self.on_poll()
                if decision_file.exists():
                    result = self._read_decision(decision_file)
                    decision_file.unlink(missing_ok=True)
                    return result
                if self.clock.now() >= deadline:
                    # Move out of pending first so write_decision can no longer match it.
                    os.replace(pending, expired_dir / pending.name)
                    return "expired"
                self.clock.sleep_until(self.clock.now() + timedelta(seconds=self.poll_s))
        finally:
            pending.unlink(missing_ok=True)

    @staticmethod
    def _read_decision(path: Path) -> Decision:
        try:
            value = json.loads(path.read_text()).get("decision")
        except (OSError, ValueError, AttributeError):
            return "rejected"
        return "approved" if value == "approve" else "rejected"


def write_decision(state_dir: Path, ticket_id: str, decision: str) -> bool:
    pending_dir, decisions_dir, _ = _dirs(state_dir)
    if not (pending_dir / f"{ticket_id}.json").exists():
        return False
    _write_atomic(decisions_dir / f"{ticket_id}.json", {"decision": decision})
    return True
