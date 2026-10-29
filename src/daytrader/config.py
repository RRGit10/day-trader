"""Configuration loading: config.toml plus API keys from env / .env."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, fields
from datetime import time
from pathlib import Path
from typing import Any, Mapping


class ConfigError(Exception):
    """Raised for invalid or unreadable configuration."""


@dataclass(frozen=True)
class StrategyConfig:
    range_minutes: int = 15
    bar_minutes: int = 5
    entry_start: time = time(9, 55)
    entry_end: time = time(11, 30)
    shorts_enabled: bool = True
    ema_period: int = 9
    rsi_period: int = 14
    rsi_long_min: float = 60.0
    rsi_short_max: float = 40.0


@dataclass(frozen=True)
class ContractConfig:
    delta_min: float = 0.60
    delta_max: float = 0.70
    delta_target: float = 0.65
    max_spread_pct: float = 0.05
    min_open_interest: int = 500
    max_price: float = 5.00


@dataclass(frozen=True)
class ExitConfig:
    take_profit_pct: float = 0.20
    stop_loss_pct: float = 0.10
    flat_time: time = time(15, 55)


@dataclass(frozen=True)
class RiskConfig:
    budget_per_trade: float = 500.0
    daily_profit_target: float = 100.0
    max_losses: int = 2
    daily_loss_limit: float = 100.0


@dataclass(frozen=True)
class BacktestConfig:
    assumed_spread_pct: float = 0.03
    risk_free_rate: float = 0.04
    default_months: int = 12


@dataclass(frozen=True)
class Keys:
    api_key: str
    api_secret: str


@dataclass(frozen=True)
class Settings:
    tickers: tuple[str, ...]
    strategy: StrategyConfig
    contract: ContractConfig
    exits: ExitConfig
    risk: RiskConfig
    backtest: BacktestConfig
    state_dir: Path
    approval_keys: Keys | None
    bot_keys: Keys | None


def _parse_time(value: Any, where: str) -> time:
    if isinstance(value, time):
        return value
    try:
        hh, mm = str(value).split(":")
        return time(int(hh), int(mm))
    except ValueError:
        raise ConfigError(f"{where}: expected 'HH:MM', got {value!r}") from None


def _build(cls, section: str, raw: Mapping[str, Any]):
    if not isinstance(raw, Mapping):
        raise ConfigError(f"[{section}] must be a table")
    known = {f.name: f for f in fields(cls)}
    unknown = set(raw) - set(known)
    if unknown:
        raise ConfigError(f"[{section}] unknown keys: {', '.join(sorted(unknown))}")
    defaults = cls()
    kwargs: dict[str, Any] = {}
    for name, value in raw.items():
        default = getattr(defaults, name)
        where = f"[{section}] {name}"
        if isinstance(default, time):
            value = _parse_time(value, where)
        elif isinstance(default, bool):
            if not isinstance(value, bool):
                raise ConfigError(f"{where}: expected true/false, got {value!r}")
        elif isinstance(default, int):
            if isinstance(value, bool) or not isinstance(value, int):
                raise ConfigError(f"{where}: expected integer, got {value!r}")
        elif isinstance(default, float):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ConfigError(f"{where}: expected number, got {value!r}")
            value = float(value)
        kwargs[name] = value
    return cls(**kwargs)


def _validate(st: StrategyConfig, c: ContractConfig, e: ExitConfig, r: RiskConfig) -> None:
    if not 0 < c.delta_min <= c.delta_target <= c.delta_max <= 1:
        raise ConfigError("contract: need 0 < delta_min <= delta_target <= delta_max <= 1")
    if st.range_minutes <= 0 or st.bar_minutes <= 0 or st.range_minutes % st.bar_minutes:
        raise ConfigError("strategy: range_minutes must be a positive multiple of bar_minutes")
    if st.entry_start >= st.entry_end:
        raise ConfigError("strategy: entry_start must be before entry_end")
    if st.ema_period < 1 or st.rsi_period < 1:
        raise ConfigError("strategy: ema_period and rsi_period must be >= 1")
    if e.take_profit_pct <= 0 or not 0 < e.stop_loss_pct < 1:
        raise ConfigError("exits: take_profit_pct > 0 and 0 < stop_loss_pct < 1 required")
    if st.entry_end >= e.flat_time:
        raise ConfigError("exits: flat_time must be after strategy entry_end")
    if c.max_spread_pct < 0 or c.min_open_interest < 0 or c.max_price <= 0:
        raise ConfigError("contract: spread/open-interest must be >= 0 and max_price > 0")
    if r.budget_per_trade <= 0 or r.daily_profit_target <= 0 or r.daily_loss_limit <= 0:
        raise ConfigError("risk: budget, profit target and loss limit must be > 0")
    if r.max_losses < 1:
        raise ConfigError("risk: max_losses must be >= 1")


def _parse_dotenv(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        out[key.strip()] = val
    return out


def _keys(env: Mapping[str, str], prefix: str) -> Keys | None:
    key = env.get(f"{prefix}_API_KEY")
    secret = env.get(f"{prefix}_API_SECRET")
    return Keys(key, secret) if key and secret else None


def load_settings(config_path: Path, env: Mapping[str, str] | None = None) -> Settings:
    config_path = Path(config_path)
    try:
        with config_path.open("rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {config_path}") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {config_path}: {exc}") from None

    tickers = data.get("universe", {}).get("tickers", [])
    if (
        not isinstance(tickers, list)
        or not tickers
        or not all(isinstance(t, str) and t.strip() for t in tickers)
    ):
        raise ConfigError("[universe] tickers must be a non-empty list of strings")

    strategy = _build(StrategyConfig, "strategy", data.get("strategy", {}))
    contract = _build(ContractConfig, "contract", data.get("contract", {}))
    exits = _build(ExitConfig, "exits", data.get("exits", {}))
    risk = _build(RiskConfig, "risk", data.get("risk", {}))
    backtest = _build(BacktestConfig, "backtest", data.get("backtest", {}))
    _validate(strategy, contract, exits, risk)

    base = config_path.parent
    state_raw = data.get("paths", {}).get("state_dir")
    state_dir = base / "state" if state_raw is None else base / Path(str(state_raw))

    if env is None:
        env = {**_parse_dotenv(base / ".env"), **os.environ}

    return Settings(
        tickers=tuple(t.strip().upper() for t in tickers),
        strategy=strategy,
        contract=contract,
        exits=exits,
        risk=risk,
        backtest=backtest,
        state_dir=state_dir,
        approval_keys=_keys(env, "APPROVAL"),
        bot_keys=_keys(env, "BOT"),
    )
