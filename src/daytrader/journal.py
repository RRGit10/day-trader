"""SQLite journal of checks, skips, tickets, trades and events."""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from pathlib import Path

from .models import ET, ClosedTrade, OrbResult, Position

_SCHEMA = """
CREATE TABLE IF NOT EXISTS checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    ticker TEXT NOT NULL,
    triggered INTEGER NOT NULL,
    direction TEXT,
    checks_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS skips (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    ticker TEXT NOT NULL,
    reason TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS tickets (
    id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    direction TEXT NOT NULL,
    option_symbol TEXT NOT NULL,
    qty INTEGER NOT NULL,
    entry_price REAL NOT NULL,
    entry_stock_price REAL NOT NULL,
    range_mid REAL NOT NULL,
    opened_at TEXT NOT NULL,
    exit_price REAL,
    pnl REAL,
    reason TEXT,
    closed_at TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    level TEXT NOT NULL,
    message TEXT NOT NULL
);
"""


def _parse(s: str) -> datetime:
    return datetime.fromisoformat(s).astimezone(ET)


class Journal:
    def __init__(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path))
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def _write(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        cur = self._conn.execute(sql, params)
        self._conn.commit()
        return cur

    def record_check(self, ts: datetime, result: OrbResult) -> None:
        checks = [{"name": c.name, "passed": c.passed, "detail": c.detail} for c in result.checks]
        self._write(
            "INSERT INTO checks (ts, ticker, triggered, direction, checks_json) VALUES (?,?,?,?,?)",
            (ts.isoformat(), result.ticker, int(result.triggered), result.direction, json.dumps(checks)),
        )

    def record_skip(self, ts: datetime, ticker: str, reason: str, detail: str = "") -> None:
        self._write(
            "INSERT INTO skips (ts, ticker, reason, detail) VALUES (?,?,?,?)",
            (ts.isoformat(), ticker, reason, detail),
        )

    def record_ticket(self, ticket_id: str, ts: datetime, payload: dict, status: str) -> None:
        self._write(
            "INSERT INTO tickets (id, ts, payload, status) VALUES (?,?,?,?)",
            (ticket_id, ts.isoformat(), json.dumps(payload), status),
        )

    def update_ticket(self, ticket_id: str, status: str) -> None:
        self._write("UPDATE tickets SET status=? WHERE id=?", (status, ticket_id))

    def open_trade(self, pos: Position) -> int:
        cur = self._write(
            "INSERT INTO trades (ticker, direction, option_symbol, qty, entry_price,"
            " entry_stock_price, range_mid, opened_at) VALUES (?,?,?,?,?,?,?,?)",
            (pos.ticker, pos.direction, pos.option_symbol, pos.qty, pos.entry_price,
             pos.entry_stock_price, pos.range_mid, pos.opened_at.isoformat()),
        )
        return int(cur.lastrowid)

    def close_trade(self, trade_id: int, exit_price: float, reason: str, closed_at: datetime) -> ClosedTrade:
        row = self._conn.execute(
            "SELECT ticker, entry_price, qty FROM trades WHERE id=? AND closed_at IS NULL",
            (trade_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"no open trade with id {trade_id}")
        ticker, entry, qty = row
        pnl = round((exit_price - entry) * qty * 100, 2)
        self._write(
            "UPDATE trades SET exit_price=?, pnl=?, reason=?, closed_at=? WHERE id=?",
            (exit_price, pnl, reason, closed_at.isoformat(), trade_id),
        )
        return ClosedTrade(ticker=ticker, pnl=pnl, closed_at=closed_at, reason=reason)

    def amend_open_trade(self, trade_id: int, qty: int, entry_price: float) -> None:
        cur = self._write(
            "UPDATE trades SET qty=?, entry_price=? WHERE id=? AND closed_at IS NULL",
            (qty, entry_price, trade_id),
        )
        if cur.rowcount != 1:
            raise ValueError(f"no open trade with id {trade_id}")

    def signaled_on(self, day: date) -> set[tuple[str, str]]:
        """(ticker, direction) pairs with a trade opened or a ticket created on `day`."""
        out: set[tuple[str, str]] = set()
        for ticker, direction, opened in self._conn.execute(
            "SELECT ticker, direction, opened_at FROM trades"
        ):
            if _parse(opened).date() == day:
                out.add((ticker, direction))
        for ts, payload in self._conn.execute("SELECT ts, payload FROM tickets"):
            if _parse(ts).date() == day:
                p = json.loads(payload)
                out.add((p["ticker"], p["direction"]))
        return out

    def open_trades(self) -> list[tuple[int, Position]]:
        rows = self._conn.execute(
            "SELECT id, ticker, direction, option_symbol, qty, entry_price, entry_stock_price,"
            " range_mid, opened_at FROM trades WHERE closed_at IS NULL ORDER BY id"
        ).fetchall()
        return [
            (r[0], Position(ticker=r[1], direction=r[2], option_symbol=r[3], qty=r[4],
                            entry_price=r[5], entry_stock_price=r[6], range_mid=r[7],
                            opened_at=_parse(r[8])))
            for r in rows
        ]

    def all_trades(self) -> list[ClosedTrade]:
        rows = self._conn.execute(
            "SELECT ticker, pnl, closed_at, reason FROM trades WHERE closed_at IS NOT NULL ORDER BY id"
        ).fetchall()
        return [ClosedTrade(ticker=r[0], pnl=r[1], closed_at=_parse(r[2]), reason=r[3]) for r in rows]

    def trades_on(self, day: date) -> list[ClosedTrade]:
        return [t for t in self.all_trades() if t.closed_at.astimezone(ET).date() == day]

    def record_event(self, ts: datetime, level: str, message: str) -> None:
        self._write(
            "INSERT INTO events (ts, level, message) VALUES (?,?,?)",
            (ts.isoformat(), level, message),
        )

    def check_rows(self) -> list[tuple[datetime, str, bool, list[dict]]]:
        rows = self._conn.execute(
            "SELECT ts, ticker, triggered, checks_json FROM checks ORDER BY id"
        ).fetchall()
        return [(_parse(r[0]), r[1], bool(r[2]), json.loads(r[3])) for r in rows]

    def skip_rows(self) -> list[tuple[datetime, str, str]]:
        rows = self._conn.execute("SELECT ts, ticker, reason FROM skips ORDER BY id").fetchall()
        return [(_parse(r[0]), r[1], r[2]) for r in rows]
