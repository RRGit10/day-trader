"""One day's journal as Markdown: trades, tickets, skipped signals and near-misses."""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime, time

from .approval import rules_line
from .journal import Journal, TradeRecord
from .report import near_miss_changes


def _money(x: float, sign: bool = False) -> str:
    s = f"${abs(x):,.2f}"
    if x < 0:
        return f"-{s}"
    return f"+{s}" if sign and x > 0 else s


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _held(start: datetime, end: datetime) -> str:
    secs = int((end - start).total_seconds())
    h, rest = divmod(secs, 3600)
    m, s = divmod(rest, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m {s:02d}s"


def _counts(c: Counter) -> str:
    return ", ".join(f"{k} {v}" for k, v in sorted(c.items(), key=lambda kv: (-kv[1], kv[0])))


def _trade_lines(n: int, t: TradeRecord, checks_rows) -> list[str]:
    head = f"### {n}. {t.ticker} {t.direction} · "
    head += f"{_money(t.pnl, sign=True)} · {t.reason}" if t.closed_at else "open"
    cost = t.entry_price * t.qty * 100
    lines = [head, "",
             f"- Contract: {t.qty}× {t.option_symbol}",
             f"- Entry: {t.opened_at:%H:%M:%S} ET at {_money(t.entry_price)} "
             f"(stock {_money(t.entry_stock_price)})"]
    if t.closed_at:
        lines += [f"- Exit: {t.closed_at:%H:%M:%S} ET at {_money(t.exit_price)}, "
                  f"after {_held(t.opened_at, t.closed_at)}",
                  f"- Return: {t.pnl / cost * 100:+.1f}% on {_money(cost)}"]
    else:
        lines.append("- Exit: still open")
    # the signal that opened it: the last triggered check for this ticker and side
    signal = [r for r in checks_rows
              if r[1] == t.ticker and r[2] and r[3] == t.direction and r[0] <= t.opened_at]
    rules = rules_line(t.direction, signal[-1][4]) if signal else "not recorded"
    lines.append(f"- Rules at entry: {rules}")
    return lines


def _outcome(ts: datetime, payload: dict, status: str, trades, skips) -> str:
    if status != "approved":
        return status
    sym = payload["option_symbol"]
    if any(t.option_symbol == sym and t.opened_at >= ts for t in trades):
        return "approved, filled"
    if any(r == "not_filled" and d.startswith(sym) and s >= ts for s, _, r, d in skips):
        return "approved, not filled"
    return "approved"


def format_day_log(journal: Journal, day: date, mode: str, entry_start: time) -> str:
    trades = journal.trade_records_on(day)
    tickets = journal.tickets_on(day)
    skips = journal.skips_on(day)
    checks_rows = journal.check_rows_on(day)

    # P&L belongs to the day a trade closed, as in `report` and `status`
    closed = [t for t in trades if t.closed_at and t.closed_at.date() == day]
    pnl = sum(t.pnl for t in closed)
    wins = sum(1 for t in closed if t.pnl > 0)
    losses = sum(1 for t in closed if t.pnl < 0)
    out = [f"# Trade journal: {day.isoformat()} ({mode})", "",
           f"Day P&L: {_money(pnl, sign=True)} from {_plural(len(closed), 'closed trade')} "
           f"({_plural(wins, 'win')}, {losses} {'loss' if losses == 1 else 'losses'}) · "
           f"{_plural(len(tickets), 'ticket')}", "",
           "## Trades", ""]
    if trades:
        for n, t in enumerate(trades, 1):
            out += _trade_lines(n, t, checks_rows) + [""]
    else:
        out += ["None.", ""]

    if tickets:
        out += ["## Tickets", "",
                "| Created | Ticket | Id | Contract | Limit | Cost | Outcome |",
                "|---|---|---|---|---|---|---|"]
        for ts, p, status in tickets:
            out.append(f"| {ts:%H:%M:%S} | {p['ticker']} {p['direction']} | {p['id'][:8]} | "
                       f"{p['qty']}× {p['option_symbol']} | {_money(p['limit'])} | "
                       f"{_money(p['cost'])} | {_outcome(ts, p, status, trades, skips)} |")
        out.append("")

    out += ["## Skipped signals", ""]
    early = Counter(r for s, _, r, _ in skips if s.time() < entry_start)
    later = [(s, tk, r, d) for s, tk, r, d in skips if s.time() >= entry_start]
    if early:
        out += [f"Ruled out before the entry window: {sum(early.values())} ({_counts(early)})", ""]
    if later:
        out += ["| Time | Ticker | Reason | Detail |", "|---|---|---|---|"]
        out += [f"| {s:%H:%M:%S} | {tk} | {r} | {d} |" for s, tk, r, d in later] + [""]
    if not early and not later:
        out += ["None.", ""]

    out += ["## Near-misses", ""]
    misses = near_miss_changes(checks_rows)
    failed = Counter(c.name.upper() for m in misses for c in m.checks if not c.passed)
    out.append(f"Near-misses: {len(misses)} ({_counts(failed)})" if misses else "None.")
    return "\n".join(out) + "\n"
