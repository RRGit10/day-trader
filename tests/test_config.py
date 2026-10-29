from datetime import time
from pathlib import Path

import pytest

from daytrader.config import ConfigError, Keys, load_settings

ENV_KEYS = {
    "APPROVAL_API_KEY": "a",
    "APPROVAL_API_SECRET": "b",
    "BOT_API_KEY": "c",
    "BOT_API_SECRET": "d",
}


def write_cfg(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "config.toml"
    p.write_text(body)
    return p


def test_defaults_match_spec(tmp_path):
    s = load_settings(write_cfg(tmp_path, '[universe]\ntickers = ["AMD"]\n'), env={})
    assert s.tickers == ("AMD",)
    st = s.strategy
    assert (st.range_minutes, st.bar_minutes) == (15, 5)
    assert (st.entry_start, st.entry_end) == (time(9, 55), time(11, 30))
    assert st.shorts_enabled is True
    assert (st.ema_period, st.rsi_period) == (9, 14)
    assert (st.rsi_long_min, st.rsi_short_max) == (60.0, 40.0)
    c = s.contract
    assert (c.delta_min, c.delta_max, c.delta_target) == (0.60, 0.70, 0.65)
    assert (c.max_spread_pct, c.min_open_interest, c.max_price) == (0.05, 500, 5.00)
    e = s.exits
    assert (e.take_profit_pct, e.stop_loss_pct, e.flat_time) == (0.20, 0.10, time(15, 55))
    r = s.risk
    assert (r.budget_per_trade, r.daily_profit_target) == (500.0, 100.0)
    assert (r.max_losses, r.daily_loss_limit) == (2, 100.0)
    b = s.backtest
    assert (b.assumed_spread_pct, b.risk_free_rate, b.default_months) == (0.03, 0.04, 12)
    assert s.approval_keys is None and s.bot_keys is None


def test_overrides_and_times(tmp_path):
    cfg = write_cfg(
        tmp_path,
        '[universe]\ntickers = ["AMD"]\n[strategy]\nentry_end = "11:00"\nshorts_enabled = false\n',
    )
    s = load_settings(cfg, env={})
    assert s.strategy.entry_end == time(11, 0)
    assert s.strategy.shorts_enabled is False


def test_keys_from_env(tmp_path):
    cfg = write_cfg(tmp_path, '[universe]\ntickers = ["AMD"]\n')
    s = load_settings(cfg, env=ENV_KEYS)
    assert s.approval_keys == Keys("a", "b")
    assert s.bot_keys == Keys("c", "d")
    s = load_settings(cfg, env={"APPROVAL_API_KEY": "a"})
    assert s.approval_keys is None and s.bot_keys is None


def test_dotenv_file_read(tmp_path, monkeypatch):
    for k in ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    cfg = write_cfg(tmp_path, '[universe]\ntickers = ["AMD"]\n')
    (tmp_path / ".env").write_text(
        "# comment\nAPPROVAL_API_KEY=a\nAPPROVAL_API_SECRET=b\n\nBOT_API_KEY=c\nBOT_API_SECRET=d\n"
    )
    s = load_settings(cfg)
    assert s.approval_keys == Keys("a", "b")
    assert s.bot_keys == Keys("c", "d")
    monkeypatch.setenv("BOT_API_KEY", "real")
    assert load_settings(cfg).bot_keys == Keys("real", "d")


def test_invalid_values_raise(tmp_path):
    bad = write_cfg(
        tmp_path,
        '[universe]\ntickers = ["AMD"]\n[contract]\ndelta_min = 0.8\ndelta_max = 0.7\n',
    )
    with pytest.raises(ConfigError):
        load_settings(bad, env={})
    empty = write_cfg(tmp_path, "[universe]\ntickers = []\n")
    with pytest.raises(ConfigError):
        load_settings(empty, env={})


def test_state_dir_default_and_override(tmp_path):
    cfg = write_cfg(tmp_path, '[universe]\ntickers = ["AMD"]\n')
    assert load_settings(cfg, env={}).state_dir == tmp_path / "state"
    cfg = write_cfg(tmp_path, '[universe]\ntickers = ["AMD"]\n[paths]\nstate_dir = "data/x"\n')
    assert load_settings(cfg, env={}).state_dir == tmp_path / "data" / "x"
    abs_dir = tmp_path / "elsewhere"
    cfg = write_cfg(tmp_path, f'[universe]\ntickers = ["AMD"]\n[paths]\nstate_dir = "{abs_dir}"\n')
    assert load_settings(cfg, env={}).state_dir == abs_dir
