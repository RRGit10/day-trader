"""Command-line interface: thin wiring over the engine modules (no trading logic here)."""
from __future__ import annotations

import argparse
import json
import os
import sys
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Callable, Sequence

from .approval import AutoApprover, FileApprover, Ticket, write_decision
from .backtest import run_backtest
from .broker import PaperLockError, make_broker
from .clock import SystemClock
from .config import ConfigError, Keys, Settings, load_settings
from .contracts import pick_contract
from .data import AlpacaData
from .guard import may_enter
from .history import AlpacaHistorical
from .journal import Journal
from .models import ET
from .orb import evaluate_orb
from .report import format_summary, summarize
from .session import Session

BACKTEST_EARLIEST = date(2024, 2, 1)
STALE_AFTER_S = 60


@dataclass
class Deps:
    """Factories for everything that touches the network or the clock; tests inject fakes."""

    data: Callable = AlpacaData  # (keys) -> MarketData
    broker: Callable = make_broker  # (keys, mode) -> Broker (verifies paper keys)
    historical: Callable = AlpacaHistorical  # (keys, cache_dir) -> HistoricalSource
    clock: Callable = SystemClock  # () -> Clock
    now: Callable[[], datetime] = lambda: datetime.now(ET)


# ------------------------------------------------------------------ helpers
def _day(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got {text!r}") from None


def _tickers(text: str) -> list[str]:
    return [t.strip().upper() for t in text.split(",") if t.strip()]


def _data_keys(settings: Settings) -> Keys:
    keys = settings.bot_keys or settings.approval_keys
    if keys is None:
        raise ConfigError("no API keys: set BOT_API_KEY/BOT_API_SECRET or "
                          "APPROVAL_API_KEY/APPROVAL_API_SECRET in .env or the environment")
    return keys


def _mode_keys(settings: Settings, mode: str) -> Keys | None:
    return settings.bot_keys if mode == "bot" else settings.approval_keys


def _journal_path(settings: Settings, name: str) -> Path:
    return settings.state_dir / f"journal-{name}.db"


def _months_before(d: date, months: int) -> date:
    idx = d.year * 12 + d.month - 1 - months
    year, month = divmod(idx, 12)
    month += 1
    return date(year, month, min(d.day, monthrange(year, month)[1]))


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _acquire_lock(path: Path) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                pid = int(path.read_text().strip())
            except (OSError, ValueError):
                pid = -1
            if pid > 0 and _pid_alive(pid):
                return False
            path.unlink(missing_ok=True)  # stale lock
            continue
        with os.fdopen(fd, "w") as fh:
            fh.write(str(os.getpid()))
        return True
    return False


# ----------------------------------------------------------------- commands
def _cmd_run(args, settings: Settings, deps: Deps) -> int:
    lock = settings.state_dir / f"run-{args.mode}.lock"
    if not _acquire_lock(lock):
        print(f"another 'run --mode {args.mode}' is already running (lock: {lock})")
        return 1
    journal = None
    try:
        keys = _mode_keys(settings, args.mode)
        if keys is None:
            raise ConfigError(f"missing API keys for {args.mode} mode "
                              "(set them in .env or the environment)")
        data = deps.data(keys)
        broker = deps.broker(keys, args.mode)
        journal = Journal(_journal_path(settings, args.mode))
        clock = deps.clock()
        approver = (FileApprover(settings.state_dir, clock) if args.mode == "approval"
                    else AutoApprover())
        Session(args.mode, settings, data, broker, journal, approver, clock).run_day()
        return 0
    except KeyboardInterrupt:
        print("interrupted")
        return 130
    finally:
        if journal is not None:
            journal.close()
        lock.unlink(missing_ok=True)


def _cmd_scan(args, settings: Settings, deps: Deps) -> int:
    data = deps.data(_data_keys(settings))
    now = deps.now()
    today = now.date()
    hours = data.market_hours(today)
    if hours is None:
        print(f"market closed on {today.isoformat()}")
        return 0
    prior: dict = {}
    day = today
    for _ in range(10):
        day -= timedelta(days=1)
        ph = data.market_hours(day)
        if ph is not None:
            prior = data.bars_5min(list(settings.tickers), ph.open, ph.close)
            break
    bars = data.bars_5min(list(settings.tickers), hours.open, min(now, hours.close))
    kinds = ["call", "put"] if settings.strategy.shorts_enabled else ["call"]
    print(f"{'TICKER':<8}{'STATUS':<22}CHECKS")
    for tk in settings.tickers:
        reasons = []
        for kind in kinds:
            try:
                chain = data.option_chain(tk, kind, today)
            except Exception as exc:
                reasons.append(f"chain_error ({exc})")
                continue
            pick = pick_contract("long" if kind == "call" else "short", chain, today,
                                 settings.contract)
            if pick.quote is not None:
                reasons = []
                break
            reasons.append(str(pick.skip_reason))
        status = "eligible" if not reasons else reasons[0]
        res = evaluate_orb(tk, prior.get(tk, []), bars.get(tk, []), settings.strategy)
        checks = "  ".join(f"{c.name} {'✓' if c.passed else '✗'}" for c in res.checks)
        print(f"{tk:<8}{status:<22}{checks}")
    return 0


def _cmd_decide(args, settings: Settings, decision: str) -> int:
    if not write_decision(settings.state_dir, args.id, decision):
        print(f"no pending ticket with id {args.id!r}")
        return 1
    print(f"{decision}: {args.id}")
    return 0


def _cmd_pending(args, settings: Settings) -> int:
    files = sorted((settings.state_dir / "approval" / "pending").glob("*.json"))
    if not files:
        print("No pending tickets.")
        return 0
    for f in files:
        try:
            ticket = Ticket.from_dict(json.loads(f.read_text()))
        except (OSError, ValueError, KeyError):
            continue  # being written or removed
        print(f"{ticket.id}  {ticket.summary()}")
    return 0


def _market_hours_now(now: datetime) -> bool:
    return now.weekday() < 5 and time(9, 25) <= now.time() <= time(16, 5)


def _cmd_status(args, settings: Settings, deps: Deps) -> int:
    now = deps.now().astimezone(ET)
    hb = settings.state_dir / f"heartbeat-{args.mode}.json"
    if not hb.is_file():
        print(f"{args.mode}: not running (no heartbeat)")
    else:
        ts = datetime.fromisoformat(json.loads(hb.read_text())["ts"])
        age = (now - ts).total_seconds()
        stale = age > STALE_AFTER_S and _market_hours_now(now)
        print(f"{args.mode}: heartbeat {age:.0f}s ago" + (" -- STALE" if stale else ""))
    path = _journal_path(settings, args.mode)
    if not path.is_file():
        print("no journal yet")
        return 0
    journal = Journal(path)
    try:
        trades = journal.trades_on(now.date())
        open_trades = journal.open_trades()
        if open_trades:
            pos = open_trades[0][1]
            print(f"open position: {pos.qty}x {pos.option_symbol} ({pos.ticker} {pos.direction}) "
                  f"entry {pos.entry_price:.2f}")
        else:
            print("open position: none")
        pnl = sum(t.pnl for t in trades)
        print(f"day P&L: {'-' if pnl < 0 else ''}${abs(pnl):,.2f} ({len(trades)} trades)")
        ok, reason = may_enter(trades, bool(open_trades), settings.risk)
        print("guard: entries allowed" if ok else f"guard: blocked ({reason})")
    finally:
        journal.close()
    return 0


def _cmd_report(args, settings: Settings) -> int:
    path = _journal_path(settings, args.journal)
    if not path.is_file():
        print(f"no journal at {path}")
        return 1
    journal = Journal(path)
    try:
        print(format_summary(summarize(journal, args.start, args.end), args.journal))
    finally:
        journal.close()
    return 0


def _cmd_backtest(args, settings: Settings, deps: Deps) -> int:
    keys = _data_keys(settings)
    yesterday = deps.now().astimezone(ET).date() - timedelta(days=1)
    end = args.end if args.end is not None else yesterday
    if end > yesterday:
        print(f"end date clamped to {yesterday} (the last complete day)")
        end = yesterday
    start = args.start if args.start is not None else _months_before(
        end, settings.backtest.default_months)
    if start < BACKTEST_EARLIEST:
        print(f"start date clamped to {BACKTEST_EARLIEST} (earliest supported)")
        start = BACKTEST_EARLIEST
    if start > end:
        print(f"start {start} is after end {end}")
        return 1
    source = deps.historical(keys, settings.state_dir / "cache")
    path = _journal_path(settings, "backtest")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    journal = Journal(path)
    try:
        print(f"backtesting {start} to {end} ...")
        summary = run_backtest(source, settings, start, end, journal, args.tickers)
        print(f"days {summary.days}, signals {summary.signals}, trades {summary.trades}, "
              f"no-data skips {summary.no_data_skips}")
        print(format_summary(summarize(journal), "backtest"))
    finally:
        journal.close()
    return 0


def _cmd_doctor(args, settings: Settings, deps: Deps) -> int:
    failed = False

    def line(ok: bool, text: str) -> None:
        nonlocal failed
        failed |= not ok
        print(f"{'PASS' if ok else 'FAIL'}  {text}")

    for name, prefix, keys in (("approval", "APPROVAL", settings.approval_keys),
                               ("bot", "BOT", settings.bot_keys)):
        if keys is None:
            line(False, f"{name}: keys missing (set {prefix}_API_KEY and {prefix}_API_SECRET)")
            continue
        line(True, f"{name}: keys present")
        try:
            broker = deps.broker(keys, name)
        except (PaperLockError, ConfigError) as exc:
            line(False, f"{name}: paper keys verification: {exc}")
            continue
        except Exception as exc:
            line(False, f"{name}: paper keys verification: {exc!r}")
            continue
        line(True, f"{name}: keys verified as paper")
        try:
            level = int(broker.get_account().options_trading_level)
            line(level >= 1, f"{name}: options trading level {level} "
                             f"({'long calls/puts allowed' if level >= 1 else 'need >= 1'})")
        except Exception as exc:
            line(False, f"{name}: options approval check: {exc!r}")
        try:
            data = deps.data(keys)
            today = deps.now().astimezone(ET).date()
            bars: dict = {}
            day = today
            for _ in range(10):
                day -= timedelta(days=1)
                hours = data.market_hours(day)
                if hours is not None:
                    bars = data.bars_5min(["SPY"], hours.open, hours.close)
                    break
            line(bool(bars.get("SPY")), f"{name}: IEX 5-min bars for SPY"
                                       + (f" ({len(bars['SPY'])} bars on {day})" if bars.get("SPY") else " (none)"))
            chain = data.option_chain("SPY", "call", today)
            line(bool(chain), f"{name}: SPY option chain ({len(chain)} contracts)")
        except Exception as exc:
            line(False, f"{name}: data check: {exc!r}")
    return 1 if failed else 0


def _cmd_record(args, settings: Settings, deps: Deps) -> int:
    source = deps.historical(_data_keys(settings), settings.state_dir / "cache")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    tickers = args.tickers or list(settings.tickers)

    def enc(bars):
        return [[b.t.isoformat(), b.o, b.h, b.l, b.c, b.v] for b in bars]

    expiries = source.expiries_after(tickers[0], args.date) if tickers else []
    for tk in tickers:
        five = source.stock_bars(tk, args.date, 5)
        payload = {"ticker": tk, "date": args.date.isoformat(), "bars_5min": enc(five),
                   "bars_1min": enc(source.stock_bars(tk, args.date, 1)), "option_bars": {}}
        if five and expiries:
            ref = five[0].o
            for kind in ("call", "put"):
                contracts = sorted(source.option_contracts(tk, kind, expiries[0]),
                                   key=lambda c: abs(c[1] - ref))[:5]
                for symbol, strike in contracts:
                    payload["option_bars"][symbol] = {
                        "kind": kind, "strike": strike, "expiry": expiries[0].isoformat(),
                        "bars_1min": enc(source.option_bars_1min(symbol, args.date)),
                    }
        path = out / f"{tk}-{args.date.isoformat()}.json"
        path.write_text(json.dumps(payload))
        print(f"wrote {path}")
    return 0


# ------------------------------------------------------------------- parser
def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="daytrader", description="ORB options day-trading engine (paper)")
    p.add_argument("--config", type=Path, default=Path("config.toml"),
                   help="path to config.toml (default ./config.toml)")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("run", help="run the trading day")
    s.add_argument("--mode", choices=("approval", "bot"), required=True)
    sub.add_parser("scan", help="show affordability and current ORB checks per ticker")
    for name in ("approve", "reject"):
        s = sub.add_parser(name, help=f"{name} a pending ticket")
        s.add_argument("id")
    sub.add_parser("pending", help="list pending approval tickets")
    s = sub.add_parser("status", help="heartbeat, position, P&L, guard state")
    s.add_argument("--mode", choices=("approval", "bot"), default="bot")
    s = sub.add_parser("report", help="performance report from a journal")
    s.add_argument("--journal", choices=("bot", "approval", "backtest"), required=True)
    s.add_argument("--start", type=_day)
    s.add_argument("--end", type=_day)
    s = sub.add_parser("backtest", help="replay history (default: last 12 months)")
    s.add_argument("--start", type=_day)
    s.add_argument("--end", type=_day)
    s.add_argument("--tickers", type=_tickers)
    sub.add_parser("doctor", help="check keys, paper lock, options approval and data access")
    s = sub.add_parser("record", help="save a day's bars as JSON fixtures")
    s.add_argument("--date", type=_day, required=True)
    s.add_argument("--tickers", type=_tickers, required=True)
    s.add_argument("--out", required=True)
    return p


def main(argv: Sequence[str] | None = None, deps: Deps | None = None) -> int:
    args = _parser().parse_args(argv)
    deps = deps or Deps()
    try:
        settings = load_settings(args.config)
        cmd = args.command
        if cmd == "run":
            return _cmd_run(args, settings, deps)
        if cmd == "scan":
            return _cmd_scan(args, settings, deps)
        if cmd in ("approve", "reject"):
            return _cmd_decide(args, settings, "approve" if cmd == "approve" else "reject")
        if cmd == "pending":
            return _cmd_pending(args, settings)
        if cmd == "status":
            return _cmd_status(args, settings, deps)
        if cmd == "report":
            return _cmd_report(args, settings)
        if cmd == "backtest":
            return _cmd_backtest(args, settings, deps)
        if cmd == "doctor":
            return _cmd_doctor(args, settings, deps)
        return _cmd_record(args, settings, deps)
    except (ConfigError, PaperLockError) as exc:
        print(f"error: {exc}")
        return 1
    except KeyboardInterrupt:
        print("interrupted")
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
