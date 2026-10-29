import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from daytrader import cli
from daytrader.approval import Ticket
from daytrader.broker import PaperLockError
from daytrader.clock import MarketHours
from daytrader.journal import Journal
from daytrader.models import ET, Bar, Check, OptionQuote, Position
from fakes import FakeClock, FakeData, FakeHistorical

NOW = datetime(2026, 10, 29, 10, 0, tzinfo=ET)  # Thursday


def write_config(tmp_path, keys=True):
    cfg = tmp_path / "config.toml"
    cfg.write_text('[universe]\ntickers = ["SPY", "AAPL"]\n')
    return cfg


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in ("APPROVAL_API_KEY", "APPROVAL_API_SECRET", "BOT_API_KEY", "BOT_API_SECRET"):
        monkeypatch.delenv(k, raising=False)


@pytest.fixture
def cfg(tmp_path):
    return write_config(tmp_path)


@pytest.fixture
def keys(monkeypatch):
    for p in ("APPROVAL", "BOT"):
        monkeypatch.setenv(f"{p}_API_KEY", "k")
        monkeypatch.setenv(f"{p}_API_SECRET", "s")


def run(cfg, *argv, deps=None):
    return cli.main(["--config", str(cfg), *argv], deps)


def make_ticket(tid="t1"):
    return Ticket(id=tid, created_at=NOW, ticker="SPY", direction="long", option_symbol="SPYC",
                  strike=500.0, expiry=date(2026, 10, 30), delta=0.65, bid=1.0, ask=1.1,
                  limit=1.1, qty=4, cost=440.0, take_profit_price=1.32, stop_loss_price=0.99,
                  stock_stop=499.0, stock_target=502.0, flat_time=datetime(2026, 1, 1, 15, 55).time(),
                  checks=(Check("ema", True, ""),))


# ------------------------------------------------------------------ parsing
@pytest.mark.parametrize("argv,attrs", [
    (["run", "--mode", "bot"], {"command": "run", "mode": "bot"}),
    (["scan"], {"command": "scan"}),
    (["approve", "x1"], {"command": "approve", "id": "x1"}),
    (["reject", "x1"], {"command": "reject", "id": "x1"}),
    (["pending"], {"command": "pending"}),
    (["status"], {"command": "status", "mode": "bot"}),
    (["status", "--mode", "approval"], {"mode": "approval"}),
    (["report", "--journal", "backtest", "--start", "2026-01-01", "--end", "2026-02-01"],
     {"journal": "backtest", "start": date(2026, 1, 1), "end": date(2026, 2, 1)}),
    (["backtest", "--tickers", "spy, aapl"], {"tickers": ["SPY", "AAPL"], "start": None}),
    (["doctor"], {"command": "doctor"}),
    (["record", "--date", "2026-10-01", "--tickers", "SPY", "--out", "d"],
     {"date": date(2026, 10, 1), "tickers": ["SPY"], "out": "d"}),
])
def test_parsing(argv, attrs):
    args = cli._parser().parse_args(argv)
    for k, v in attrs.items():
        assert getattr(args, k) == v


def test_global_config_default():
    assert cli._parser().parse_args(["scan"]).config == Path("config.toml")


def test_bad_args_exit_2():
    with pytest.raises(SystemExit) as e:
        cli._parser().parse_args(["run", "--mode", "live"])
    assert e.value.code == 2


def test_missing_config_is_one_line_error(tmp_path, capsys):
    assert cli.main(["--config", str(tmp_path / "nope.toml"), "pending"]) == 1
    assert "config file not found" in capsys.readouterr().out


# ------------------------------------------------------------ approve/pending
def test_approve_unknown_id_exits_1(cfg, capsys):
    assert run(cfg, "approve", "nope") == 1
    assert "nope" in capsys.readouterr().out


def test_approve_and_reject_write_decision(cfg, tmp_path):
    pend = tmp_path / "state" / "approval" / "pending"
    pend.mkdir(parents=True)
    (pend / "t1.json").write_text(json.dumps(make_ticket().to_dict()))
    assert run(cfg, "approve", "t1") == 0
    dec = tmp_path / "state" / "approval" / "decisions" / "t1.json"
    assert json.loads(dec.read_text()) == {"decision": "approve"}
    assert run(cfg, "reject", "t1") == 0
    assert json.loads(dec.read_text()) == {"decision": "reject"}


def test_pending_lists_summaries(cfg, tmp_path, capsys):
    assert run(cfg, "pending") == 0
    assert "No pending tickets." in capsys.readouterr().out
    pend = tmp_path / "state" / "approval" / "pending"
    pend.mkdir(parents=True)
    t = make_ticket()
    (pend / "t1.json").write_text(json.dumps(t.to_dict()))
    assert run(cfg, "pending") == 0
    assert f"t1  {t.summary()}" in capsys.readouterr().out


# ------------------------------------------------------------------- status
def test_status_stale_heartbeat(cfg, tmp_path, capsys):
    state = tmp_path / "state"
    state.mkdir()
    (state / "heartbeat-bot.json").write_text(json.dumps(
        {"ts": (NOW - timedelta(minutes=5)).isoformat(), "position": None, "day_pnl": 0.0}))
    assert run(cfg, "status", deps=cli.Deps(now=lambda: NOW)) == 0
    assert "STALE" in capsys.readouterr().out


def test_status_fresh_and_off_hours_not_stale(cfg, tmp_path, capsys):
    state = tmp_path / "state"
    state.mkdir()
    hb = state / "heartbeat-bot.json"
    hb.write_text(json.dumps({"ts": (NOW - timedelta(seconds=10)).isoformat()}))
    run(cfg, "status", deps=cli.Deps(now=lambda: NOW))
    assert "STALE" not in capsys.readouterr().out
    night = NOW.replace(hour=20)
    hb.write_text(json.dumps({"ts": (night - timedelta(minutes=5)).isoformat()}))
    run(cfg, "status", deps=cli.Deps(now=lambda: night))
    assert "STALE" not in capsys.readouterr().out


def test_status_not_running_and_journal_state(cfg, tmp_path, capsys):
    run(cfg, "status", deps=cli.Deps(now=lambda: NOW))
    assert "not running" in capsys.readouterr().out
    j = Journal(tmp_path / "state" / "journal-bot.db")
    for i in range(2):
        pos = Position("SPY", "long", "SPYC", 1, 1.0, 100.0, 99.0, NOW.replace(hour=9, minute=40 + i))
        tid = j.open_trade(pos)
        j.close_trade(tid, 0.5, "stop_loss", NOW.replace(hour=9, minute=50 + i))
    j.open_trade(Position("AAPL", "short", "AAPLP", 2, 1.5, 100.0, 101.0, NOW))
    j.close()
    run(cfg, "status", deps=cli.Deps(now=lambda: NOW))
    out = capsys.readouterr().out
    assert "AAPLP" in out and "-$100.00" in out and "blocked (position_open)" in out


# ------------------------------------------------------------------- report
def test_report_missing_journal(cfg, capsys):
    assert run(cfg, "report", "--journal", "bot") == 1
    assert "no journal" in capsys.readouterr().out


def test_report_prints_summary(cfg, tmp_path, capsys):
    Journal(tmp_path / "state" / "journal-backtest.db").close()
    assert run(cfg, "report", "--journal", "backtest") == 0
    out = capsys.readouterr().out
    assert "Trades: 0" in out and "Past results" in out


# ----------------------------------------------------------------- backtest
def hist_deps(hist, now=NOW):
    return cli.Deps(historical=lambda keys, cache: hist, now=lambda: now)


def test_backtest_clamps_early_start(cfg, keys, tmp_path, capsys, monkeypatch):
    seen = {}

    def fake_run(source, settings, start, end, journal, tickers):
        seen.update(start=start, end=end, tickers=tickers)
        return SimpleNamespace(days=0, signals=0, trades=0, no_data_skips=0)

    monkeypatch.setattr(cli, "run_backtest", fake_run)
    assert run(cfg, "backtest", "--start", "2023-06-01", "--tickers", "spy",
               deps=hist_deps(FakeHistorical())) == 0
    assert seen["start"] == date(2024, 2, 1)
    assert seen["tickers"] == ["SPY"]
    assert "clamped to 2024-02-01" in capsys.readouterr().out


def test_backtest_defaults_and_end_clamp(cfg, keys, capsys, monkeypatch):
    seen = {}
    monkeypatch.setattr(cli, "run_backtest", lambda s, st, a, b, j, t: seen.update(start=a, end=b)
                        or SimpleNamespace(days=0, signals=0, trades=0, no_data_skips=0))
    run(cfg, "backtest", deps=hist_deps(FakeHistorical()))
    assert seen == {"start": date(2025, 10, 28), "end": date(2026, 10, 28)}
    run(cfg, "backtest", "--end", "2026-10-29", deps=hist_deps(FakeHistorical()))
    assert seen["end"] == date(2026, 10, 28)
    assert "end date clamped to 2026-10-28" in capsys.readouterr().out


def test_backtest_recreates_journal_and_uses_cache_dir(cfg, keys, tmp_path, capsys):
    path = tmp_path / "state" / "journal-backtest.db"
    j = Journal(path)
    j.record_skip(NOW, "SPY", "stale_row")
    j.close()
    caches = []
    deps = cli.Deps(historical=lambda k, c: caches.append(c) or FakeHistorical(),
                    now=lambda: NOW)
    assert run(cfg, "backtest", "--start", "2026-10-01", "--end", "2026-10-02", deps=deps) == 0
    assert caches == [tmp_path / "state" / "cache"]
    j = Journal(path)
    assert j.skip_rows() == []
    j.close()
    assert "Trades: 0" in capsys.readouterr().out


def test_backtest_without_keys_is_config_error(cfg, capsys):
    assert run(cfg, "backtest", deps=hist_deps(FakeHistorical())) == 1
    assert "no API keys" in capsys.readouterr().out


# ---------------------------------------------------------------------- run
def test_run_blocked_by_live_lock(cfg, tmp_path, capsys):
    state = tmp_path / "state"
    state.mkdir()
    lock = state / "run-bot.lock"
    lock.write_text(str(os.getpid()))
    assert run(cfg, "run", "--mode", "bot") == 1
    assert "already running" in capsys.readouterr().out
    assert lock.exists()  # someone else's lock is left alone


def test_run_replaces_stale_lock_and_removes_its_own(cfg, keys, tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    lock = state / "run-bot.lock"
    lock.write_text("999999999")  # no such pid
    clock = FakeClock(NOW.replace(hour=9, minute=0))
    data = FakeData()  # market closed today: run_day returns right after startup
    built = []
    deps = cli.Deps(data=lambda k: data, broker=lambda k, m: built.append(m) or object(),
                    clock=lambda: clock, now=clock.now)
    assert run(cfg, "run", "--mode", "bot", deps=deps) == 0
    assert built == ["bot"] and not lock.exists()
    assert (state / "journal-bot.db").exists()


def test_run_ctrl_c_exits_130_and_removes_lock(cfg, keys, tmp_path):
    def boom(k):
        raise KeyboardInterrupt

    deps = cli.Deps(data=boom)
    assert run(cfg, "run", "--mode", "approval", deps=deps) == 130
    assert not (tmp_path / "state" / "run-approval.lock").exists()


def test_run_missing_keys_removes_lock(cfg, tmp_path, capsys):
    assert run(cfg, "run", "--mode", "bot") == 1
    assert "missing API keys" in capsys.readouterr().out
    assert not (tmp_path / "state" / "run-bot.lock").exists()


# ------------------------------------------------------------------- doctor
class DocBroker:
    def __init__(self, level):
        self.level = level

    def get_account(self):
        return SimpleNamespace(options_trading_level=self.level)


def doctor_deps(level=2, paper_ok=True, chain=True):
    data = FakeData()
    for d in range(1, 11):
        day = NOW.date() - timedelta(days=d)
        data.hours[day] = MarketHours(datetime.combine(day, datetime.min.time().replace(hour=9, minute=30), ET),
                                      datetime.combine(day, datetime.min.time().replace(hour=16), ET))
        if d == 1:
            data.bars["SPY"] = [Bar(data.hours[day].open, 1, 1, 1, 1, 1)]
    if chain:
        data.chains[("SPY", "call")] = [OptionQuote("SPYC", "SPY", "call", 500, NOW.date(), 1, 1.1, 0.6, 600)]

    def broker(keys, mode):
        if not paper_ok:
            raise PaperLockError("not paper keys")
        return DocBroker(level)

    return cli.Deps(data=lambda k: data, broker=broker, now=lambda: NOW)


def test_doctor_all_pass(cfg, keys, capsys):
    assert run(cfg, "doctor", deps=doctor_deps()) == 0
    out = capsys.readouterr().out
    assert out.count("PASS") == 10 and "FAIL" not in out and "options trading level 2" in out


def test_doctor_missing_keys_fail_names_env_vars(cfg, capsys):
    assert run(cfg, "doctor", deps=doctor_deps()) == 1
    out = capsys.readouterr().out
    assert "FAIL  approval: keys missing (set APPROVAL_API_KEY and APPROVAL_API_SECRET)" in out
    assert "BOT_API_KEY" in out


def test_doctor_low_options_level_and_paper_failure(cfg, keys, capsys):
    assert run(cfg, "doctor", deps=doctor_deps(level=0)) == 1
    assert "FAIL  approval: options trading level 0" in capsys.readouterr().out
    assert run(cfg, "doctor", deps=doctor_deps(paper_ok=False)) == 1
    assert "FAIL  bot: paper keys verification: not paper keys" in capsys.readouterr().out


# ------------------------------------------------------------------- record
def test_record_writes_fixture(cfg, keys, tmp_path):
    day = date(2026, 10, 1)
    h = FakeHistorical()
    t0 = datetime(2026, 10, 1, 9, 30, tzinfo=ET)
    h.stock[("SPY", day, 5)] = [Bar(t0, 500, 501, 499, 500.5, 10)]
    h.stock[("SPY", day, 1)] = [Bar(t0, 500, 500.5, 499.5, 500.2, 3)]
    exp = date(2026, 10, 2)
    h.contracts[("SPY", "call", exp)] = [(f"SPYC{k}", float(k)) for k in range(490, 520, 5)]
    h.contracts[("SPY", "put", exp)] = [("SPYP500", 500.0)]
    h.option_bars[("SPYP500", day)] = [Bar(t0, 1, 1, 1, 1, 1)]
    out = tmp_path / "fx"
    assert run(cfg, "record", "--date", "2026-10-01", "--tickers", "SPY", "--out", str(out),
               deps=hist_deps(h)) == 0
    payload = json.loads((out / "SPY-2026-10-01.json").read_text())
    assert len(payload["bars_5min"]) == 1 and len(payload["bars_1min"]) == 1
    assert len(payload["option_bars"]) == 6  # 5 nearest calls + 1 put
    assert payload["option_bars"]["SPYP500"]["bars_1min"][0][1] == 1.0


# --------------------------------------------------------------------- scan
def test_scan_prints_table(cfg, keys, capsys):
    data = FakeData()
    day = NOW.date()
    data.hours[day] = MarketHours(NOW.replace(hour=9, minute=30), NOW.replace(hour=16, minute=0))
    assert run(cfg, "scan", deps=cli.Deps(data=lambda k: data, now=lambda: NOW)) == 0
    out = capsys.readouterr().out
    assert "SPY" in out and "AAPL" in out and "✗" in out
