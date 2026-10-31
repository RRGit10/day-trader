"""Performance summary of the trade journal."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime

from .journal import Journal
from .models import ET

_CAVEATS = (
    "Past results don't predict future results.",
    "Assumed spreads and bar-level fills are approximations; backtest VWAP uses "
    "full-market (SIP) data while live trading uses IEX, so signals can differ slightly.",
)


@dataclass(frozen=True)
class Summary:
    trades: int
    wins: int
    losses: int
    win_rate: float
    avg_win: float
    avg_loss: float
    total_pnl: float
    worst_day: float
    longest_losing_streak: int
    pnl_by_ticker: dict[str, float]
    pnl_by_month: dict[str, float]
    blocked_by: dict[str, int]
    skip_reasons: dict[str, int]


def _et_date(ts: datetime) -> date:
    return ts.astimezone(ET).date()


def _in_range(d: date, start: date | None, end: date | None) -> bool:
    return (start is None or d >= start) and (end is None or d <= end)


def _mean(xs: list[float]) -> float:
    return round(sum(xs) / len(xs), 2) if xs else 0.0


def summarize(journal: Journal, start: date | None = None, end: date | None = None) -> Summary:
    trades = sorted(
        (t for t in journal.all_trades() if _in_range(_et_date(t.closed_at), start, end)),
        key=lambda t: t.closed_at,
    )
    wins = [t.pnl for t in trades if t.pnl > 0]
    losses = [t.pnl for t in trades if t.pnl < 0]

    by_day: dict[date, float] = {}
    by_ticker: dict[str, float] = {}
    by_month: dict[str, float] = {}
    streak = longest = 0
    for t in trades:
        d = _et_date(t.closed_at)
        by_day[d] = by_day.get(d, 0.0) + t.pnl
        by_ticker[t.ticker] = by_ticker.get(t.ticker, 0.0) + t.pnl
        month = f"{d.year:04d}-{d.month:02d}"
        by_month[month] = by_month.get(month, 0.0) + t.pnl
        streak = streak + 1 if t.pnl < 0 else 0
        longest = max(longest, streak)

    blocked: Counter[str] = Counter()
    for ts, _ticker, triggered, checks in journal.check_rows():
        if triggered or not _in_range(_et_date(ts), start, end):
            continue
        passed = {c["name"]: c["passed"] for c in checks}
        if not passed.get("breakout"):
            continue
        for name in ("ema", "vwap", "rsi"):
            if name in passed and not passed[name]:
                blocked[name] += 1

    skips: Counter[str] = Counter(
        reason for ts, _ticker, reason in journal.skip_rows() if _in_range(_et_date(ts), start, end)
    )

    n = len(trades)
    return Summary(
        trades=n,
        wins=len(wins),
        losses=len(losses),
        win_rate=len(wins) / n if n else 0.0,
        avg_win=_mean(wins),
        avg_loss=_mean(losses),
        total_pnl=round(sum(t.pnl for t in trades), 2),
        worst_day=round(min(by_day.values()), 2) if by_day else 0.0,
        longest_losing_streak=longest,
        pnl_by_ticker={k: round(v, 2) for k, v in by_ticker.items()},
        pnl_by_month={k: round(v, 2) for k, v in by_month.items()},
        blocked_by=dict(blocked),
        skip_reasons=dict(skips),
    )


def _money(x: float) -> str:
    return f"-${-x:,.2f}" if x < 0 else f"${x:,.2f}"


def format_summary(s: Summary, source: str) -> str:
    lines = [
        f"Trades: {s.trades}  (wins {s.wins}, losses {s.losses})",
        f"Win rate: {s.win_rate:.1%}",
        f"Average win: {_money(s.avg_win)}",
        f"Average loss: {_money(s.avg_loss)}",
        f"Total P&L: {_money(s.total_pnl)}",
        f"Worst day: {_money(s.worst_day)}",
        f"Longest losing streak: {s.longest_losing_streak}",
        "",
        "P&L by ticker:",
    ]
    lines += [f"  {k}: {_money(v)}" for k, v in sorted(s.pnl_by_ticker.items(), key=lambda kv: (-kv[1], kv[0]))] or ["  (none)"]
    lines += ["", "P&L by month:"]
    lines += [f"  {k}: {_money(v)}" for k, v in sorted(s.pnl_by_month.items())] or ["  (none)"]
    lines += ["", "Blocked by (breakout passed, no trigger):"]
    lines += [f"  {k}: {v}" for k, v in sorted(s.blocked_by.items(), key=lambda kv: (-kv[1], kv[0]))] or ["  (none)"]
    lines += ["", "Skip reasons:"]
    lines += [f"  {k}: {v}" for k, v in sorted(s.skip_reasons.items(), key=lambda kv: (-kv[1], kv[0]))] or ["  (none)"]
    if source == "backtest":
        lines += ["", *_CAVEATS]
    return "\n".join(lines)
