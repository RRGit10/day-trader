"""Daily P&L from a journal, for the browser calendar. Reads journals read-only."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from .models import ET

JOURNALS: tuple[str, ...] = ("approval", "bot", "backtest")


@dataclass(frozen=True)
class CalendarTrade:
    ticker: str
    direction: str
    qty: int
    entry_price: float
    exit_price: float
    pnl: float
    reason: str
    opened_at: datetime
    closed_at: datetime


@dataclass(frozen=True)
class CalendarDay:
    day: date
    pnl: float
    cost: float
    pct: float
    wins: int
    losses: int
    trades: tuple[CalendarTrade, ...]


def _pct(pnl: float, cost: float) -> float:
    return round(pnl / cost * 100, 2) if cost else 0.0


def _et(s: str) -> datetime:
    return datetime.fromisoformat(s).astimezone(ET)


def load_days(db_path: Path) -> list[CalendarDay]:
    """Closed trades grouped by ET close date. A missing journal gives [] and is not created."""
    path = Path(db_path)
    if not path.exists():
        return []
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    try:
        rows = conn.execute(
            "SELECT ticker, direction, qty, entry_price, exit_price, pnl, reason, opened_at,"
            " closed_at FROM trades WHERE closed_at IS NOT NULL AND pnl IS NOT NULL ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    by_day: dict[date, list[CalendarTrade]] = {}
    for r in rows:
        t = CalendarTrade(ticker=r[0], direction=r[1], qty=r[2], entry_price=r[3],
                          exit_price=r[4], pnl=r[5], reason=r[6] or "",
                          opened_at=_et(r[7]), closed_at=_et(r[8]))
        by_day.setdefault(t.closed_at.date(), []).append(t)
    out = []
    for day in sorted(by_day):
        trades = tuple(sorted(by_day[day], key=lambda t: t.closed_at))
        pnl = round(sum(t.pnl for t in trades), 2)
        cost = round(sum(t.entry_price * t.qty * 100 for t in trades), 2)
        out.append(CalendarDay(day=day, pnl=pnl, cost=cost, pct=_pct(pnl, cost),
                               wins=sum(t.pnl > 0 for t in trades),
                               losses=sum(t.pnl < 0 for t in trades), trades=trades))
    return out


def days_payload(days: list[CalendarDay], today: date) -> dict:
    return {
        "today": today.isoformat(),
        "days": [
            {"date": d.day.isoformat(), "pnl": d.pnl, "cost": d.cost, "pct": d.pct,
             "wins": d.wins, "losses": d.losses,
             "trades": [{"ticker": t.ticker, "direction": t.direction, "qty": t.qty,
                         "entry_price": t.entry_price, "exit_price": t.exit_price,
                         "pnl": t.pnl, "reason": t.reason,
                         "opened_at": t.opened_at.strftime("%H:%M"),
                         "closed_at": t.closed_at.strftime("%H:%M")}
                        for t in d.trades]}
            for d in days
        ],
    }
