# ORB Options Engine (Stage 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Python engine + Claude Code plugin that trades a 15-min ORB setup with weekly options on two Alpaca paper accounts (approval mode and bot mode), journals everything, reports performance, and backtests the same rules on history.

**Architecture:** All trading decisions live in pure modules (`indicators`, `orb`, `contracts`, `sizing`, `exits`, `guard`, `pricing`) that never touch the network. Adapters (`data`, `broker`, `clock`) wrap `alpaca-py` behind small protocols with fake implementations for tests. `session` runs a trading day; `backtest` replays history through the same pure modules; `cli` exposes both; Claude Code skills call the CLI.

**Tech Stack:** Python ≥ 3.11, `alpaca-py` 0.44.x, `sqlite3`, `tomllib`, `zoneinfo`, `argparse`, `pytest`.

**Spec:** `docs/superpowers/specs/2026-10-28-orb-options-design.md`

## Global Constraints

- Python ≥ 3.11. Runtime dependency: `alpaca-py>=0.44,<0.45` only. Dev: `pytest`. No pandas.
- All times are `America/New_York`, tz-aware `datetime` (`zoneinfo.ZoneInfo("America/New_York")`). Bar `t` = bar **start** time.
- Defaults (all overridable in `config.toml`): range 15 min, 5-min bars, entry window 09:55–11:30 (bar *close* times, inclusive), flat 15:55, EMA 9, RSI 14 (Wilder), long RSI > 60, short RSI < 40, shorts enabled, delta 0.60–0.70 target 0.65, spread ≤ 5% of mid, OI ≥ 500, contract ask ≤ $5.00, budget $500/trade, take profit +20%, stop −10% (option mid), daily stop at +$100 / 2 losses / −$100, one open position at a time, affordability check at 09:40, startup 09:25, entry cancel after 60 s, ticket expiry 120 s, re-quote tolerance 5%.
- Bot broker is constructed with `paper=True` in code — never from config. No live trading anywhere in Stage 1: `AlpacaBroker` also uses `paper=True` for approval mode.
- API keys come only from `.env` / environment: `APPROVAL_API_KEY`, `APPROVAL_API_SECRET`, `BOT_API_KEY`, `BOT_API_SECRET`. `.env` and `state/` are git-ignored.
- Live data feeds: stocks `DataFeed.IEX`, options `OptionsFeed.INDICATIVE`. Backtest stock feed: `DataFeed.SIP`.
- Package lives in `src/daytrader/`; tests in `tests/`; run all tests with `pytest -q`.

## Review Focus

1. **Missing bars in the opening range** (halted or thinly traded ticker) → no signal; the `range` check fails with detail "incomplete range (n/3 bars)". Test in Task 3.
2. **Half-day sessions** (e.g. Fri 2026-11-27, close 13:00) → flat time is `min(15:55, close − 5 min)` and the entry window ends no later than flat time. Test in Task 8.
3. **Stale or missing option quote while in a position** → only stock-based exits and time exit apply; a warning is journaled. Test in Task 6.
4. **Approval answered after expiry, or for an unknown ticket id** → treated as expired/unknown; nothing is bought. Test in Task 10.
5. **Partial entry fill** → position qty = filled qty, remainder cancelled, P&L computed on filled qty. Test in Task 11.

---

### Task 1: Project scaffold and config

**Files:**
- Create: `pyproject.toml`, `.gitignore`, `config.example.toml`, `.env.example`, `src/daytrader/__init__.py`, `src/daytrader/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces (`daytrader.config`), all `@dataclass(frozen=True)`:
  - `StrategyConfig(range_minutes=15, bar_minutes=5, entry_start=time(9,55), entry_end=time(11,30), shorts_enabled=True, ema_period=9, rsi_period=14, rsi_long_min=60.0, rsi_short_max=40.0)`
  - `ContractConfig(delta_min=0.60, delta_max=0.70, delta_target=0.65, max_spread_pct=0.05, min_open_interest=500, max_price=5.00)`
  - `ExitConfig(take_profit_pct=0.20, stop_loss_pct=0.10, flat_time=time(15,55))`
  - `RiskConfig(budget_per_trade=500.0, daily_profit_target=100.0, max_losses=2, daily_loss_limit=100.0)`
  - `BacktestConfig(assumed_spread_pct=0.03, risk_free_rate=0.04, default_months=12)`
  - `Keys(api_key: str, api_secret: str)`
  - `Settings(tickers: tuple[str, ...], strategy, contract, exits, risk, backtest, state_dir: Path, approval_keys: Keys | None, bot_keys: Keys | None)`
  - `load_settings(config_path: Path, env: Mapping[str, str] | None = None) -> Settings` — `env` defaults to `os.environ` merged with a `.env` file next to `config_path` (simple `KEY=VALUE` parser, `#` comments).
  - `ConfigError(Exception)`

- [ ] **Step 1: Write the failing tests** in `tests/test_config.py`:
  - `test_defaults_match_spec`: load a TOML with only `[universe] tickers = ["AMD"]` → every default above equals the spec value (assert each field).
  - `test_overrides_and_times`: `[strategy] entry_end = "11:00"`, `shorts_enabled = false` → `time(11,0)`, `False`.
  - `test_keys_from_env`: env with the four key vars → `approval_keys == Keys("a","b")`, `bot_keys == Keys("c","d")`; missing vars → `None`.
  - `test_dotenv_file_read`: `.env` beside config is parsed; real env wins over file.
  - `test_invalid_values_raise`: `delta_min = 0.8, delta_max = 0.7` → `ConfigError`; empty tickers → `ConfigError`.
- [ ] **Step 2:** `pytest tests/test_config.py -q` → FAIL (module missing).
- [ ] **Step 3: Implement.** `pyproject.toml`: setuptools, `src` layout, `[project.scripts] daytrader = "daytrader.cli:main"`, deps per Global Constraints, `[project.optional-dependencies] dev = ["pytest"]`. `.gitignore`: `.env`, `state/`, `__pycache__/`, `*.egg-info/`, `.pytest_cache/`. `config.example.toml` lists every section with defaults and starter tickers `["SPY","QQQ","IWM","TSLA","NVDA","AMD","AAPL","META","AMZN","MSFT","PLTR","SOFI","F","BAC","INTC","AAL","NIO","RIVN","HOOD","MARA"]`. Times in TOML are `"HH:MM"` strings.
- [ ] **Step 4:** `pip install -e '.[dev]' && pytest tests/test_config.py -q` → PASS.
- [ ] **Step 5: Commit** `feat: project scaffold and config loading`.

### Task 2: Models and indicators

**Files:**
- Create: `src/daytrader/models.py`, `src/daytrader/indicators.py`
- Test: `tests/test_indicators.py`

**Interfaces:**
- Produces (`daytrader.models`), frozen dataclasses unless noted:
  - `ET = ZoneInfo("America/New_York")`; `Direction = Literal["long", "short"]`; `OptionType = Literal["call", "put"]`
  - `Bar(t: datetime, o: float, h: float, l: float, c: float, v: float)`
  - `Check(name: str, passed: bool, detail: str)`
  - `OrbResult(ticker: str, triggered: bool, direction: Direction | None, checks: tuple[Check, ...], range_high: float | None, range_low: float | None, trigger_time: datetime | None, trigger_close: float | None)` with property `range_mid -> float | None`
  - `OptionQuote(symbol: str, underlying: str, type: OptionType, strike: float, expiry: date, bid: float, ask: float, delta: float | None, open_interest: int | None)` with properties `mid` and `spread_pct` (`(ask-bid)/mid`, `inf` if mid ≤ 0)
  - `ContractPick(quote: OptionQuote | None, skip_reason: str | None)`
  - `Position(ticker, direction, option_symbol, qty: int, entry_price: float, entry_stock_price: float, range_mid: float, opened_at: datetime)` with properties `risk_per_share = abs(entry_stock_price - range_mid)`, `stock_target` (entry ± R in trade direction), `cost = entry_price * qty * 100`
  - `ClosedTrade(ticker: str, pnl: float, closed_at: datetime, reason: str)`
  - `ExitReason = Literal["stop_loss", "stock_stop", "take_profit", "stock_target", "time"]`
- Produces (`daytrader.indicators`):
  - `ema(values: Sequence[float], period: int) -> list[float | None]` — `None` until index `period-1`, seeded with the SMA of the first `period` values, `k = 2/(period+1)`.
  - `rsi(closes: Sequence[float], period: int = 14) -> list[float | None]` — Wilder smoothing; first value at index `period`; avg loss 0 → 100.
  - `session_vwap(bars: Sequence[Bar]) -> list[float]` — cumulative Σ(typical·v)/Σv, typical = (h+l+c)/3; if Σv = 0 so far, use the bar close.

- [ ] **Step 1: Write the failing tests:**
  - `test_ema_linear`: `ema([1..10], 3)` → `[None, None, 2.0, 3.0, ..., 9.0]`.
  - `test_rsi_hand_computed`: `rsi([10, 11, 10.5, 11.5, 12], 2)` → `[None, None, 66.667, 85.714, 90.909]` (approx 1e-3).
  - `test_rsi_all_up_is_100` / `test_rsi_all_down_is_0`.
  - `test_vwap`: bars (h=l=c=10, v=100), (h=l=c=20, v=300) → `[10.0, 17.5]`; zero-volume first bar → its close.
  - `test_position_levels`: long entry_stock 101, mid 99 → R 2, target 103; short entry 97, mid 99 → target 95.
- [ ] **Step 2:** run → FAIL.
- [ ] **Step 3:** implement both modules.
- [ ] **Step 4:** `pytest tests/test_indicators.py -q` → PASS.
- [ ] **Step 5: Commit** `feat: models and indicators`.

### Task 3: ORB rule

**Files:**
- Create: `src/daytrader/orb.py`
- Test: `tests/test_orb.py`

**Interfaces:**
- Consumes: `Bar`, `Check`, `OrbResult`, `StrategyConfig`, `ema`, `rsi`, `session_vwap`.
- Produces: `evaluate_orb(ticker: str, prior_bars: Sequence[Bar], today_bars: Sequence[Bar], cfg: StrategyConfig, entry_end: time | None = None) -> OrbResult`
  - `today_bars`: today's **closed** 5-min bars from 09:30 in order; the last bar is the candidate trigger bar. `prior_bars`: the previous session's 5-min bars (warm-up for EMA/RSI). `entry_end` overrides `cfg.entry_end` (half days).
  - Checks, always in this order and always all present: `range`, `window`, `breakout`, `ema`, `vwap`, `rsi`.
    - `range`: bars with `09:30 ≤ t < 09:30 + range_minutes` must number `range_minutes / bar_minutes`; else failed, detail `"incomplete range (n/3 bars)"`.
    - `window`: trigger bar close time (`t + bar_minutes`) within `[entry_start, entry_end]`, and the last two bars both start at/after range end.
    - `breakout`: last two closes both `> range_high` → long; both `< range_low` → short (only if `shorts_enabled`, else failed with detail `"shorts disabled"`).
    - `ema`: close vs EMA(prior+today closes) at the trigger bar; `vwap`: close vs `session_vwap(today_bars)[-1]`; `rsi`: RSI(prior+today closes) `> rsi_long_min` (long) / `< rsi_short_max` (short). If no direction, these three fail with detail `"no breakout"`.
  - `triggered` = all six passed. Details carry the numbers, e.g. `"close 101.20 > ema 100.85"`.

- [ ] **Step 1: Write the failing tests** using a helper `make_day(closes, start=09:30)` that builds 5-min bars (h=c+0.1, l=c-0.1, v=1000) and a flat `prior` of 40 bars at 100:
  - `test_long_trigger_all_pass`: range bars ~100, then 101.0, 101.5 rising → triggered, direction long, trigger_time = 09:50 bar, range_mid correct.
  - `test_short_trigger_all_pass` (mirror).
  - `test_one_close_above_is_not_enough`.
  - `test_each_filter_fails` (parametrized): construct cases where only `ema`, only `vwap`, only `rsi` fails → `triggered False` and exactly that check failed.
  - `test_incomplete_range` (Review Focus 1): drop the 09:35 bar → `range` failed, detail `"incomplete range (2/3 bars)"`.
  - `test_window_edges`: trigger bar closing at 09:55 passes; closing at 11:30 passes; 11:35 fails; `entry_end=time(12,50)` override honored.
  - `test_shorts_disabled`.
- [ ] **Step 2:** run → FAIL.
- [ ] **Step 3:** implement `evaluate_orb`.
- [ ] **Step 4:** run → PASS.
- [ ] **Step 5: Commit** `feat: ORB rule with per-check results`.

### Task 4: Option pricing (Black-Scholes)

**Files:**
- Create: `src/daytrader/pricing.py`
- Test: `tests/test_pricing.py`

**Interfaces:**
- Produces: `bs_price(S: float, K: float, t: float, r: float, sigma: float, kind: OptionType) -> float`; `bs_delta(S, K, t, r, sigma, kind) -> float` (put delta negative); `implied_vol(price: float, S, K, t, r, kind) -> float | None` (bisection on σ ∈ [0.01, 5.0], tol 1e-6, ≤ 200 iterations; `None` if price is outside the bounds' prices or `t <= 0`). Normal CDF via `math.erf`. `t` in years.

- [ ] **Step 1: Write the failing tests:** `S=100,K=100,t=1,r=0.05,σ=0.2` → call 10.4506, put 5.5735, call delta 0.6368, put delta −0.3632 (abs 1e-4); `implied_vol(bs_price(...σ=0.35...)) ≈ 0.35` for call and put; price below intrinsic → `None`; `t=0` → `None`.
- [ ] **Step 2:** run → FAIL. **Step 3:** implement. **Step 4:** run → PASS.
- [ ] **Step 5: Commit** `feat: Black-Scholes price, delta, implied vol`.

### Task 5: Contract picker and sizing

**Files:**
- Create: `src/daytrader/contracts.py`
- Test: `tests/test_contracts.py`

**Interfaces:**
- Consumes: `OptionQuote`, `ContractPick`, `ContractConfig`.
- Produces:
  - `pick_contract(direction: Direction, quotes: Sequence[OptionQuote], today: date, cfg: ContractConfig) -> ContractPick`. Stages, in order; the first stage that leaves nothing sets `skip_reason`: (1) type matches (call for long, put for short) and `expiry > today`, keep only the **nearest** such expiry → `"no_expiry"`; (2) `delta is not None` and `delta_min ≤ |delta| ≤ delta_max` → `"no_delta_match"`; (3) `bid > 0`, `spread_pct ≤ max_spread_pct`, and (`open_interest is None` or `≥ min_open_interest`) → `"illiquid"`; (4) `ask ≤ max_price` → `"too_expensive"`. Choose `|delta|` closest to `delta_target`, tie → lower ask.
  - `contracts_for_budget(price: float, budget: float) -> int` = `floor(budget / (price * 100))`, `0` if `price <= 0`.
  - `entry_limit_price(bid: float, ask: float) -> float` — mid rounded to a valid tick: $0.01 if mid < 3.00, else $0.05.

- [ ] **Step 1: Write the failing tests:** picks 0.66 over 0.62 and 0.71 (excluded); puts use |delta|; same-day expiry ignored, next expiry chosen even if a later one has a better delta; each skip reason produced by a minimal chain; `open_interest=None` passes liquidity (backtest); `contracts_for_budget(4.80, 500) == 1`, `(2.40, 500) == 2`, `(5.01, 500) == 0`; `entry_limit_price(2.40, 2.46) == 2.43`, `(4.10, 4.30) == 4.20`.
- [ ] **Step 2–4:** fail → implement → pass.
- [ ] **Step 5: Commit** `feat: contract picker and sizing`.

### Task 6: Exits and daily guard

**Files:**
- Create: `src/daytrader/exits.py`, `src/daytrader/guard.py`
- Test: `tests/test_exits.py`, `tests/test_guard.py`

**Interfaces:**
- Consumes: `Position`, `ExitReason`, `ClosedTrade`, `ExitConfig`, `RiskConfig`.
- Produces:
  - `check_exit(pos: Position, option_mid: float | None, stock_price: float, now: datetime, flat_time: time, cfg: ExitConfig) -> ExitReason | None`. Evaluated in priority order: `stop_loss` (mid ≤ entry × (1 − stop_loss_pct)), `stock_stop` (long: stock ≤ range_mid; short: ≥), `take_profit` (mid ≥ entry × (1 + take_profit_pct)), `stock_target` (long: ≥ target; short: ≤), `time` (`now.time() ≥ flat_time`). If `option_mid is None`, skip the two option checks.
  - `may_enter(trades_today: Sequence[ClosedTrade], position_open: bool, cfg: RiskConfig) -> tuple[bool, str | None]` with reasons `"position_open"`, `"profit_target_hit"` (Σpnl ≥ target), `"max_losses"` (count pnl < 0 ≥ max_losses), `"loss_limit"` (Σpnl ≤ −limit), checked in that order.

- [ ] **Step 1: Write the failing tests:** each exit in isolation for long and short; boundary equality triggers (mid exactly 1.20× → take_profit); stop beats target when both true; `option_mid=None` with mid-level conditions met → no option exit, but stock_stop still fires (Review Focus 3); time exit at 15:55 and at a half-day flat time 12:55. Guard: +100 → profit_target_hit; two −30 losses → max_losses; one −100 → loss_limit; +60 and −20 → allowed; position open → blocked first.
- [ ] **Step 2–4:** fail → implement → pass.
- [ ] **Step 5: Commit** `feat: exit rules and daily guard`.

### Task 7: Journal

**Files:**
- Create: `src/daytrader/journal.py`
- Test: `tests/test_journal.py`

**Interfaces:**
- Consumes: `OrbResult`, `Position`, `ClosedTrade`.
- Produces: `class Journal(path: Path)` (creates the SQLite file and tables `checks`, `skips`, `tickets`, `trades`, `events`; `closed_at` / `opened_at` stored as ISO strings):
  - `record_check(ts: datetime, result: OrbResult) -> None` (checks stored as JSON)
  - `record_skip(ts: datetime, ticker: str, reason: str, detail: str = "") -> None`
  - `record_ticket(ticket_id: str, ts: datetime, payload: dict, status: str) -> None`; `update_ticket(ticket_id: str, status: str) -> None`
  - `open_trade(pos: Position) -> int`; `close_trade(trade_id: int, exit_price: float, reason: str, closed_at: datetime) -> ClosedTrade` (pnl = (exit − entry) × qty × 100)
  - `open_trades() -> list[tuple[int, Position]]`; `trades_on(day: date) -> list[ClosedTrade]`; `all_trades() -> list[ClosedTrade]`
  - `record_event(ts: datetime, level: str, message: str) -> None`
  - `check_rows() -> list[tuple[datetime, str, bool, list[dict]]]` and `skip_rows() -> list[tuple[datetime, str, str]]` (for reports)

- [ ] **Step 1: Write the failing tests:** round-trip open → close computes pnl (entry 2.50, exit 3.00, qty 2 → +100.0); `open_trades` returns only unclosed with identical `Position`; `trades_on` filters by ET date; checks round-trip with check names; reopening the same file keeps data.
- [ ] **Step 2–4:** fail → implement → pass.
- [ ] **Step 5: Commit** `feat: SQLite journal`.

### Task 8: Clock and market data adapters

**Files:**
- Create: `src/daytrader/clock.py`, `src/daytrader/data.py`, `tests/fakes.py`
- Test: `tests/test_clock.py`, `tests/test_data.py`

**Interfaces:**
- Produces (`daytrader.clock`):
  - `class Clock(Protocol)`: `now() -> datetime`; `sleep_until(dt: datetime) -> None`
  - `SystemClock()`; `FakeClock(start: datetime)` in `tests/fakes.py` (`sleep_until` jumps the time; `advance(seconds)`)
  - `MarketHours(open: datetime, close: datetime)`; `session_times(hours: MarketHours, exits: ExitConfig, strategy: StrategyConfig) -> tuple[time, time]` returning `(entry_end, flat_time)` = `(min(strategy.entry_end, flat), flat)` where `flat = min(exits.flat_time, (close − 5 min).time())`.
- Produces (`daytrader.data`):
  - `class MarketData(Protocol)`: `market_hours(day: date) -> MarketHours | None`; `bars_5min(tickers: Sequence[str], start: datetime, end: datetime) -> dict[str, list[Bar]]`; `option_chain(underlying: str, kind: OptionType, today: date) -> list[OptionQuote]` (expiries from tomorrow to today + 10 days); `latest_stock_price(ticker: str) -> float`; `latest_option_quote(symbol: str) -> tuple[float, float, datetime]` (bid, ask, quote time)
  - `class AlpacaData(MarketData)`: `__init__(keys: Keys)`; uses `StockHistoricalDataClient` (feed IEX), `OptionHistoricalDataClient.get_option_chain` (feed INDICATIVE; delta from `greeks.delta`) merged with `TradingClient(paper=True).get_option_contracts` for `open_interest`; `TradingClient.get_calendar` for hours. Every call goes through `with_retries(fn, tries=3, delays=(1, 2, 4))`.
  - `with_retries(fn: Callable[[], T], tries: int = 3, delays: Sequence[float] = (1, 2, 4), sleep=time.sleep) -> T`
  - `FakeData(MarketData)` in `tests/fakes.py`: built from dicts; tests set `stock_prices`, `option_quotes`, `bars`, `chains`, `hours`.

- [ ] **Step 1: Write the failing tests:** `session_times` normal day → `(11:30, 15:55)`; half day close 13:00 → `(11:30, 12:55)` (Review Focus 2); a hypothetical close at 11:00 → entry_end `10:55`. `with_retries` succeeds on 3rd try with recorded sleeps `[1, 2]`, raises after 3 failures. `AlpacaData` mapping tested with `unittest.mock` stubs of the SDK clients: chain snapshot + contracts → `OptionQuote` with delta and OI; bars mapped to ET-aware `Bar`.
- [ ] **Step 2–4:** fail → implement → pass.
- [ ] **Step 5: Commit** `feat: clock and Alpaca data adapter`.

### Task 9: Broker adapters with paper lock

**Files:**
- Create: `src/daytrader/broker.py`; extend `tests/fakes.py`
- Test: `tests/test_broker.py`

**Interfaces:**
- Produces:
  - `OrderStatus(status: str, filled_qty: int, avg_price: float | None)` (status in `"new" | "partially_filled" | "filled" | "canceled" | "rejected"`)
  - `class Broker(Protocol)`: `buy_limit(symbol: str, qty: int, limit: float) -> str`; `sell_limit(symbol, qty, limit) -> str`; `sell_market(symbol, qty) -> str`; `cancel(order_id: str) -> None`; `order_status(order_id: str) -> OrderStatus`; `positions() -> dict[str, int]`
  - `class AlpacaBroker(Broker)`: `__init__(keys: Keys)` → `TradingClient(key, secret, paper=True)` always; `verify_paper() -> None` calls `get_account()` and raises `PaperLockError` on any auth failure. Orders: `LimitOrderRequest` / `MarketOrderRequest`, `time_in_force=DAY`.
  - `make_broker(keys: Keys | None, mode: str) -> AlpacaBroker` — raises `ConfigError` if keys missing; calls `verify_paper()`.
  - `PaperLockError(Exception)`
  - `FakeBroker` in `tests/fakes.py`: `fill_policy` ∈ `"fill" | "partial" | "never"`, records orders, tracks positions.

- [ ] **Step 1: Write the failing tests:** `AlpacaBroker` with mocked `TradingClient` → constructor called with `paper=True` (assert kwargs) for both modes; `verify_paper` turns an `APIError` into `PaperLockError`; `make_broker(None, "bot")` → `ConfigError`; order requests carry symbol/qty/limit/side; `FakeBroker` partial policy reports half the qty filled.
- [ ] **Step 2–4:** fail → implement → pass.
- [ ] **Step 5: Commit** `feat: Alpaca broker locked to paper`.

### Task 10: Approvers

**Files:**
- Create: `src/daytrader/approval.py`
- Test: `tests/test_approval.py`

**Interfaces:**
- Produces:
  - `Ticket(id: str, created_at: datetime, ticker: str, direction: Direction, option_symbol: str, strike: float, expiry: date, delta: float, bid: float, ask: float, limit: float, qty: int, cost: float, take_profit_price: float, stop_loss_price: float, stock_stop: float, stock_target: float, flat_time: time, checks: tuple[Check, ...])` with `to_dict()` / `from_dict()` and `summary() -> str` (one plain-English paragraph, e.g. "TSLA long — buy 1× 2026-11-06 $360 call (Δ0.64) at $4.20 limit, cost $420 = max loss. Exit at +20% ($5.04), −10% ($3.78), stock $358.32 / $370.04, or 15:55.")
  - `class Approver(Protocol)`: `request(ticket: Ticket) -> Literal["approved", "rejected", "expired"]`
  - `AutoApprover` → always `"approved"`.
  - `FileApprover(state_dir: Path, clock: Clock, timeout_s: int = 120, poll_s: float = 1.0)`: writes `state_dir/approval/pending/<id>.json`; polls `state_dir/approval/decisions/<id>.json` (`{"decision": "approve" | "reject"}`); on timeout moves the ticket to `approval/expired/` and returns `"expired"`; always removes the pending file when done.
  - `write_decision(state_dir: Path, ticket_id: str, decision: str) -> bool` — returns `False` (writes nothing) if no pending ticket with that id exists.

- [ ] **Step 1: Write the failing tests** (FakeClock): approve before timeout → `"approved"`; reject → `"rejected"`; no decision → `"expired"` after 120 s and file in `expired/`; `write_decision` for unknown id → `False`; decision written after expiry → ticket stays expired (Review Focus 4); `summary()` contains ticker, cost and all exit levels.
- [ ] **Step 2–4:** fail → implement → pass.
- [ ] **Step 5: Commit** `feat: auto and file-based approvers`.

### Task 11: Trading session

**Files:**
- Create: `src/daytrader/session.py`
- Test: `tests/test_session.py`

**Interfaces:**
- Consumes: everything above.
- Produces: `class Session(mode: Literal["approval", "bot"], settings: Settings, data: MarketData, broker: Broker, journal: Journal, approver: Approver, clock: Clock)` with:
  - `run_day() -> None` — full schedule: 09:25 `startup()`; return early if `market_hours` is `None`; 09:40 `affordability_filter()`; at each 5-min bar close from 09:50 through `flat_time` call `on_bar_close(close_time)`; between closes, while a position is open, `manage_position()` every 15 s; at `flat_time` flatten; at close + 5 min write a day summary event and return. Writes `state_dir/heartbeat-<mode>.json` (`{"ts": iso, "position": symbol|null, "day_pnl": float}`) every loop iteration.
  - `startup() -> None` — reconcile `journal.open_trades()` with `broker.positions()`: both present → resume managing; journal-only → close in journal with reason `"reconciled_missing"` at entry price; broker-only → `record_event("warn", ...)`, not managed.
  - `affordability_filter() -> list[str]` — a ticker is kept if `pick_contract` succeeds for calls (or puts when shorts enabled); others journaled as skips with the pick's reason.
  - `on_bar_close(close_time: datetime) -> None` — fetch bars for scan list (one call); tickers whose just-closed bar is missing are re-fetched every 10 s for up to 60 s, then skipped for this candle with reason `"no_bars"`; `evaluate_orb` each, journal; for triggered results not yet signaled today in that direction, in scan-list order: `may_enter` → `option_chain` → `pick_contract` → `contracts_for_budget` (0 → skip `"too_expensive"`) → build `Ticket` → `approver.request` → if approved, re-quote; if `|new_limit − ticket.limit| / ticket.limit > 0.05` re-ask once with a fresh ticket, else skip `"price_moved"` → `enter(ticket)`. Only the first entry per bar close (one position).
  - `enter(ticket: Ticket) -> Position | None` — `buy_limit`, poll status every 2 s up to 60 s; unfilled → cancel, skip `"not_filled"`; partially filled → cancel remainder, position with filled qty (Review Focus 5); journal `open_trade`.
  - `manage_position() -> None` — fetch option quote and stock price; quote older than 60 s → `option_mid=None` plus a `warn` event (once per position); `check_exit`; on a reason → `exit_position(reason)`.
  - `exit_position(reason: ExitReason) -> ClosedTrade` — `sell_limit` at bid, re-price to the new bid every 10 s up to 3 times, then `sell_market`; `close_trade` with the fill price.
  - Any exception in `on_bar_close`/`manage_position` → `record_event("error", traceback)`, set `no_new_entries = True`, continue.

- [ ] **Step 1: Write the failing tests** (FakeClock, FakeData, FakeBroker, temp Journal), one scenario each:
  - `test_bot_day_take_profit`: long signal at 09:55 → 1 position, option mid rises 20% → closed `take_profit`, pnl ≈ +$100 on a $500 position; guard then blocks a second signal (`profit_target_hit`).
  - `test_two_losses_stop_the_day`: two stop-outs → third signal skipped `max_losses`.
  - `test_time_exit` at 15:55; `test_half_day_time_exit` at 12:55.
  - `test_approval_rejected_buys_nothing`; `test_approval_expired_buys_nothing`; `test_price_moved_reasks_then_skips`.
  - `test_partial_fill_uses_filled_qty` (Review Focus 5).
  - `test_unfilled_entry_cancelled`.
  - `test_stale_quote_uses_stock_exits` (Review Focus 3).
  - `test_restart_resumes_open_position` and `test_reconcile_missing_broker_position`.
  - `test_exception_stops_new_entries_but_manages_exits`.
  - `test_affordability_filter_drops_expensive_tickers`.
  - `test_missing_bar_skipped_after_60s`.
- [ ] **Step 2–4:** fail → implement → pass.
- [ ] **Step 5: Commit** `feat: trading session loop for approval and bot modes`.

### Task 12: Backtest

**Files:**
- Create: `src/daytrader/backtest.py`
- Test: `tests/test_backtest.py`

**Interfaces:**
- Consumes: `evaluate_orb`, `pick_contract`, `contracts_for_budget`, `may_enter`, `implied_vol`, `bs_delta`, `Journal`, configs.
- Produces:
  - `class HistoricalSource(Protocol)`: `trading_days(start: date, end: date) -> list[tuple[date, MarketHours]]`; `stock_bars(ticker: str, day: date, minutes: int) -> list[Bar]` (`minutes` ∈ {1, 5}); `option_contracts(underlying: str, kind: OptionType, expiry: date) -> list[tuple[str, float]]` (symbol, strike; includes expired contracts); `expiries_after(underlying: str, day: date) -> list[date]`; `option_bars_1min(symbol: str, day: date) -> list[Bar]`
  - `AlpacaHistorical(keys: Keys, cache_dir: Path)` — SIP stock feed; option bars via `get_option_bars`; contracts via `get_option_contracts(status=inactive|active)`; every response cached as JSON under `cache_dir` keyed by request; respects ≤ 180 calls/min with a simple limiter.
  - `run_backtest(source: HistoricalSource, settings: Settings, start: date, end: date, journal: Journal, tickers: Sequence[str] | None = None) -> BacktestSummary` where `BacktestSummary(days: int, signals: int, trades: int, no_data_skips: int)`.
  - Per day: at each 5-min close in the window, evaluate every ticker (prior bars = previous trading day). For a triggered signal when `may_enter` allows: candidate strikes = the 10 nearest the trigger close for the nearest expiry after the day; price each from its last 1-min bar **inside the trigger bar** (none → not a candidate); IV via `implied_vol`, delta via `bs_delta` (t = expiry 16:00 ET − trigger time, in years of 365 days); build `OptionQuote(bid=p·(1−h), ask=p·(1+h), open_interest=None)` with `h = assumed_spread_pct / 2`; `pick_contract`; no candidates at all → skip `"no_data"`. Entry fill = ask. Then walk 1-min bars (option and stock, aligned by time) after the trigger: stop_loss if option low ≤ entry × 0.90 (fill = entry × 0.90 × (1 − h)); stock_stop if stock crosses range mid (fill = option close × (1 − h)); take_profit if option high ≥ entry × 1.20 (fill = entry × 1.20 × (1 − h)); stock_target (fill = option close × (1 − h)); time at flat_time (option close × (1 − h)). Within one bar, stop conditions are checked before profit conditions. Missing option bar for a minute → carry the last price. Next signal is only considered at a 5-min close after the exit.
  - Writes checks, skips and trades to `journal` exactly like the live session.

- [ ] **Step 1: Write the failing tests** with a `FakeHistorical` (in `tests/fakes.py`) built from synthetic days:
  - `test_target_day`, `test_stop_day`, `test_both_in_one_bar_counts_as_stop`, `test_time_exit_day`, `test_guard_stops_after_two_losses`, `test_missing_option_bars_counts_no_data`.
  - `test_backtest_and_session_agree_on_signals`: the same synthetic day through `Session` (fakes) and `run_backtest` records identical `(time, ticker, triggered)` check rows.
- [ ] **Step 2–4:** fail → implement → pass.
- [ ] **Step 5: Commit** `feat: backtest on historical bars`.

### Task 13: Report

**Files:**
- Create: `src/daytrader/report.py`
- Test: `tests/test_report.py`

**Interfaces:**
- Consumes: `Journal`.
- Produces: `Summary(trades: int, wins: int, losses: int, win_rate: float, avg_win: float, avg_loss: float, total_pnl: float, worst_day: float, longest_losing_streak: int, pnl_by_ticker: dict[str, float], pnl_by_month: dict[str, float], blocked_by: dict[str, int], skip_reasons: dict[str, int])`; `summarize(journal: Journal, start: date | None = None, end: date | None = None) -> Summary`; `format_summary(s: Summary, source: str) -> str` (plain text; for `source == "backtest"` it appends the two caveats from the spec: past results don't predict future results; assumed spreads/bar-level fills are approximations, and IEX vs SIP VWAP may differ). `blocked_by` counts, among check rows where `breakout` passed but `triggered` is false, each failed check name among `ema`, `vwap`, `rsi`.
- [ ] **Step 1: Write the failing tests:** known trade list → exact win rate, averages, worst day, streak, by-ticker/month sums; blocked_by counts; empty journal → zeros and no division error; backtest caveat present only for backtest.
- [ ] **Step 2–4:** fail → implement → pass.
- [ ] **Step 5: Commit** `feat: performance report`.

### Task 14: CLI

**Files:**
- Create: `src/daytrader/cli.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Produces: `main(argv: Sequence[str] | None = None) -> int`. Global `--config PATH` (default `./config.toml`). Subcommands:
  - `run --mode {approval,bot}` — builds Session with `AlpacaData(approval or bot keys)`, `make_broker`, `Journal(state/journal-<mode>.db)`, `FileApprover` (approval) / `AutoApprover` (bot), `SystemClock`; refuses to start if another `run` for that mode holds `state/run-<mode>.lock`.
  - `scan` — affordability filter + current `evaluate_orb` per ticker, printed as a table (ticker, eligible/reason, each check ✓/✗).
  - `approve ID` / `reject ID` — `write_decision`; exit code 1 + message if unknown.
  - `pending` — prints pending tickets' `summary()` (used by the skill).
  - `status [--mode bot]` — heartbeat age (flag "STALE" if > 60 s during market hours), open position, today's P&L and guard state.
  - `report --journal {bot,approval,backtest} [--start] [--end]`.
  - `backtest [--start YYYY-MM-DD] [--end] [--tickers A,B]` — default last 12 months, never before 2024-02-01 (earlier start → clamp with a message); journal `state/journal-backtest.db` (recreated each run); prints `format_summary`.
  - `doctor` — for each key set: keys present, `verify_paper()`, options approval (`get_account().options_trading_level` ≥ 1, buying long calls/puts), one IEX bars call and one option chain call succeed. Prints PASS/FAIL per line; exit 1 on any FAIL.
  - `record --date YYYY-MM-DD --tickers A,B --out DIR` — saves that day's 5-min/1-min bars (and option bars for strikes near the money) as JSON fixtures.
- [ ] **Step 1: Write the failing tests:** argument parsing for every subcommand; `approve` unknown id → exit 1; `backtest --start 2023-06-01` clamps to 2024-02-01; `run` second instance blocked by lock; `status` with a 5-minute-old heartbeat prints `STALE`. Network-touching commands are tested with injected fakes via a `deps` factory argument on internal `_cmd_*` functions.
- [ ] **Step 2–4:** fail → implement → pass.
- [ ] **Step 5: Commit** `feat: daytrader CLI`.

### Task 15: Claude Code plugin, skills and README

**Files:**
- Create: `.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json`, `skills/trade-session/SKILL.md`, `skills/scan/SKILL.md`, `skills/bot-status/SKILL.md`, `skills/report/SKILL.md`, `skills/backtest/SKILL.md`, `README.md`, `scripts/schedule-bot.md`
- Test: `tests/test_plugin.py`

**Interfaces:**
- Consumes: CLI commands from Task 14.
- Skill contents (each `SKILL.md` has `name` + `description` frontmatter):
  - `trade-session`: check `daytrader doctor` passes; start `daytrader run --mode approval` in the background before 09:45; poll `daytrader pending` every ~15 s; show each ticket's summary verbatim and ask the user; run `daytrader approve|reject ID` **only** on the user's explicit yes/no in this conversation, never on its own; after the session, run `daytrader report --journal approval --start today`.
  - `scan`, `bot-status`, `report`, `backtest`: run the matching command, then explain the output in plain English; never place, approve or modify orders.
- `README.md`: what it is, the strategy table from the spec, setup (two Alpaca paper accounts, enable options, `.env`, `pip install -e .`, `daytrader doctor`), daily use for both modes, `/plugin marketplace add <your-github-user>/daytrader` + `/plugin install daytrader@daytrader`, risks/disclaimer, credit to Jake Schincariol's project as inspiration.
- `scripts/schedule-bot.md`: cron (Linux), launchd (macOS) and Task Scheduler (Windows) entries to start `daytrader run --mode bot` at 09:25 ET Mon–Fri.
- [ ] **Step 1: Write the failing test** `tests/test_plugin.py`: both manifests are valid JSON with `name == "daytrader"`; every `skills/*/SKILL.md` has frontmatter `name` and `description`; every `daytrader <subcommand>` mentioned in skills exists in the CLI parser.
- [ ] **Step 2–4:** fail → write files → pass.
- [ ] **Step 5: Commit** `feat: Claude Code plugin, skills and README`.

### Task 16: Final verification

- [ ] **Step 1:** `pytest -q` → all pass, no warnings about un-awaited/unclosed resources.
- [ ] **Step 2:** `pip install -e . && daytrader --help && daytrader backtest --help` → usage printed, exit 0.
- [ ] **Step 3:** Run a full fake-day end-to-end via `pytest tests/test_session.py tests/test_backtest.py -q` and inspect one journal with `daytrader report --journal backtest` pointed at a test DB — output shows trades, win rate and both caveats.
- [ ] **Step 4:** Grep check: `grep -rn "paper=" src/` shows only `paper=True`.
- [ ] **Step 5: Commit** any fixes; tag `v0.1.0`.

**Live checks the user runs on his computer (this environment cannot reach Alpaca):** `daytrader doctor` → all PASS; `daytrader backtest --start <3 months ago>` completes; one bot session on a market day writes trades/skips to `state/journal-bot.db`.
