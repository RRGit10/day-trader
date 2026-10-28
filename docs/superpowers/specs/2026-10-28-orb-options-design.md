# Daytrader — Stage 1 Design: ORB Options Engine

**Date:** 2026-10-28
**Owner:** Rabin Regmi
**Status:** Draft for review

## 1. Purpose

A personal day-trading tool Rabin uses every trading day. It finds one specific
opening-range-breakout (ORB) setup on liquid stocks, trades it with weekly
options, and manages the exit. Reliability and practical output matter more than
polish.

It runs in two modes on two separate Alpaca **paper** accounts:

- **Approval mode** — Rabin is at his computer; every entry needs his explicit "yes".
- **Bot mode (auto-paper)** — takes every signal on its own, to build an honest
  track record of the rule.

Success for Stage 1: both modes run a full session against Alpaca paper accounts,
follow the rules below exactly, and every signal/skip/trade is in the journal so
`/report` can show how the rule performs; and `daytrader backtest` replays the same
rules over past days (Feb 2024 onward) to estimate performance before paper trading.

Inspired by Jake Schincariol's open-source `day-trader-skill` (feature reference
only — no code reused).

## 2. Strategy (the rules)

### 2.1 Universe
- A ticker list in `config.toml` that Rabin edits. Starter list mixes liquid ETFs,
  mega-caps and liquid lower-priced optionable names.
- **Affordability filter (daily, ~9:40 AM ET):** a ticker is scanned today only if
  its option chain has a qualifying contract (§2.4) costing **≤ $500 for one
  contract** (ask ≤ $5.00). `/scan` shows which tickers made the cut and why others didn't.

### 2.2 Timeframe
- Signals on **5-minute candles**. Day trades only — nothing held overnight.
- **Opening range:** 9:30–9:45 AM ET (first three 5-min candles): range high, range low.
- **Entry window:** 5-min candle closes from **9:55 AM** through **11:30 AM** ET.
  No new entries after 11:30.
- **Flat time:** anything open is sold at **3:55 PM** ET.

### 2.3 Entry signal (all must be true on the 2nd confirming candle)

| Check | Long | Short |
|---|---|---|
| 2-candle rule | 2 consecutive 5-min closes **above** range high | 2 consecutive closes **below** range low |
| 9 EMA (5-min closes) | Close > EMA9 | Close < EMA9 |
| VWAP (session, resets 9:30) | Close > VWAP | Close < VWAP |
| RSI(14), Wilder smoothing | > 60 | < 40 |

- "Consecutive" means the two most recent closed candles. The first candle
  eligible to be a confirming candle is the 9:45–9:50 bar; earliest trigger is the
  close of the 9:50–9:55 bar (9:55 AM).
- One signal per ticker per direction per day.
- Shorts can be disabled with `shorts_enabled = false`.
- Every evaluation records a pass/fail per check, so you can see *why* a ticker did
  or didn't trigger.
- Data note: free Alpaca equity data is IEX-only, so VWAP/volume are approximations
  of the full-market values.
- EMA/RSI warm-up uses the previous session's 5-min bars, so values are valid at 9:55.

### 2.4 Contract selection
- Long → **call**; short → **put**.
- **Expiry:** the nearest listed expiration **after today** (never same-day / 0DTE).
- **Delta:** |delta| between **0.60 and 0.70**; if several, the one closest to 0.65.
- **Liquidity:** bid > 0, spread ≤ **5% of mid**, open interest ≥ **500**.
- **Cost:** one contract's ask ≤ $5.00 (≤ $500).
- No qualifying contract → skip, journaled with reason.

### 2.5 Sizing
- Budget **$500 per trade**. Contracts = floor(500 ÷ (limit price × 100)), min 1.
- **One open position at a time** (across all tickers).

### 2.6 Exits (first one hit wins)

| Exit | Trigger |
|---|---|
| Take profit | Option mid ≥ entry fill × **1.20** (≈ +$100) |
| Stop loss | Option mid ≤ entry fill × **0.90** (≈ −$50) |
| Stock stop | Stock last price crosses the **range midpoint** against the trade |
| Stock target | Stock moves **1R** in favor (R = |entry stock price − range midpoint|) |
| Time | **3:55 PM** ET |

- Exit checks every ~15 seconds using the latest option quote and stock trade.
- Exits are sent by the engine as a marketable limit sell (at the bid; re-priced
  every 10 s up to 3 times, then market order).
- Note: a stop can fill worse than −10% in a fast move. The ≈ −$50 figure is a
  target, not a guarantee.

### 2.7 Daily guard (no new entries for the rest of the day when any is true)
- Realized P&L for the day ≥ **+$100** (goal reached), or
- **2 losing trades** (realized P&L < 0 each), or
- Realized P&L ≤ **−$100**.

Open positions keep being managed after the guard trips.

### 2.8 Both modes use identical rules
Same signal, contract, sizing, exits and guard. The only difference is who
approves the entry. This keeps the bot's track record a valid test of what Rabin trades.

## 3. Modes

### 3.1 Approval mode (`/trade-session`)
- Started from Claude Code before 9:45 AM. Claude launches
  `daytrader run --mode approval` in the background and monitors it.
- On a signal the engine writes a **ticket** to `state/approval/pending/<id>.json`:
  ticker, direction, contract, delta, bid/ask/spread, contracts, cost (= max loss),
  exit levels (option +20%/−10%, stock stop/target, 3:55), and which checks passed.
- Claude shows the ticket in plain words. Nothing is sent until Rabin says yes;
  Claude then runs `daytrader approve <id>` (or `reject <id>`). The same commands
  work from any terminal without Claude.
- Tickets expire after **2 minutes**. On approval the engine re-quotes; if the limit
  price moved more than 5% from the ticket, it re-asks instead of buying.
- Approval covers entry **and** the exit plan: exits then run automatically.

### 3.2 Bot mode (`daytrader run --mode bot`)
- Runs as a plain background process; Claude is not involved. Can be started
  automatically at 9:25 AM on weekdays by the OS scheduler (cron / launchd / Task
  Scheduler — set up for Rabin's OS during the plan).
- Takes every signal that passes the guard, no approval.
- **Hard-locked to paper:** the bot's Alpaca client is constructed with paper
  mode in code (not a config option), and at start-up it confirms the account
  responds on the paper endpoint; otherwise it exits with an error.

### 3.3 Accounts and keys
- Two Alpaca paper accounts: `APPROVAL_*` keys and `BOT_*` keys in a local `.env`
  (git-ignored). Keys are never pasted into chat.
- Both accounts need options trading enabled (checked during setup by
  `daytrader doctor`).
- **Live-money trading is out of scope for Stage 1.** Approval mode is paper-only
  too; enabling live is a later, deliberate change.

### 3.4 Backtest (`daytrader backtest`)
Runs the exact Stage 1 rules over past trading days to estimate how they would
have performed, in minutes instead of months of paper trading.

- **Period:** any range from **February 2024** onward (Alpaca's historical option
  data starts then). Default: the last 12 months.
- **Stock data:** 5-min bars from Alpaca's full-market (SIP) feed, which the free
  plan allows for data older than 15 minutes. (Live trading uses IEX, so VWAP may
  differ slightly between backtest and live — noted in every report.)
- **Same logic:** reuses `orb`, `sizing`, `exits` and `guard` unchanged, with the same
  entry window, one-position rule and daily guard.
- **Contract choice:** at a signal, fetch historical bars for the nearest-after-today
  expiry's strikes near the money; compute each strike's delta with Black-Scholes
  from implied volatility backed out of its price (risk-free rate from config,
  default 4%, dividends ignored); pick |delta| 0.60–0.70 closest to 0.65 with
  price ≤ $5.00. Open interest and live spread can't be checked historically —
  instead the contract needs at least one trade in the signal bar, and fills are
  charged an assumed spread (below).
- **Fills (conservative):** entry at the option's price at the signal + half an
  assumed spread (default 3% of price); exits at the trigger price − half the
  assumed spread. Exits checked on 1-min option bars (+20% / −10% by bar
  high/low) and stock bars (midpoint / 1R). If a stop and a target are both hit
  inside one bar, assume the **stop** hit first.
- **Missing data:** if no option bars exist for the contract, the signal is
  counted as "no data" and skipped — never invented.
- **Cache:** downloaded bars are cached locally so re-runs are fast; first run for
  12 months × the ticker list may take a while within the 200 calls/min limit.
- **Output:** writes to `state/journal-backtest.db` (same schema as the live
  journals) so `/report` works on it, plus a summary: trades, win rate, avg
  win/loss, total P&L, worst day, longest losing streak, P&L by ticker and by
  month, which checks blocked signals, and the count of "no data" skips.
- **Caveat printed on every report:** past results don't predict future results;
  assumed spreads and bar-level fills are approximations.

## 4. Architecture

One Python package (`daytrader`) holds all logic once; Claude Code skills are thin
front-ends that call its CLI.

| Module | Responsibility | Depends on |
|---|---|---|
| `config` | Load `config.toml` + `.env`; validate; expose typed settings | — |
| `clock` | Market-time helpers (ET), market calendar via Alpaca; injectable fake clock for tests | data |
| `data` | Alpaca stock bars (5-min), latest stock trade, option chain snapshots (quotes, greeks, OI); retries with backoff; staleness detection | alpaca-py |
| `indicators` | EMA, session VWAP, RSI(14) Wilder — pure functions | — |
| `orb` | Bars → `Signal` or `NoSignal` with per-check results — pure function | indicators |
| `contracts` | Signal + chain → chosen contract or skip reason — pure function | — |
| `sizing` | Budget + price → contract count | — |
| `guard` | Today's journal → may-enter? + reason | journal |
| `broker` | Interface: buy/sell limit, cancel, positions, account. `AlpacaBroker` and `FakeBroker` implementations | alpaca-py |
| `exits` | Position + latest prices → exit reason or hold — pure function | — |
| `journal` | SQLite (`state/journal-<mode>.db`): signals, skips, tickets, orders, trades, days | sqlite3 |
| `session` | The day loop for either mode; uses an `Approver` (auto for bot, file-based for approval) | all above |
| `pricing` | Black-Scholes price, delta, implied-vol solver — pure functions | — |
| `backtest` | Replays past days through `orb`/`sizing`/`exits`/`guard` with historical bars, simulated fills, local cache | data, pricing, journal |
| `cli` | `run`, `scan`, `approve`, `reject`, `status`, `report`, `backtest`, `doctor`, `record` | session, backtest |

Pure functions (`indicators`, `orb`, `contracts`, `sizing`, `exits`) hold all
trading decisions and never touch the network, so they can be tested exhaustively.

### 4.1 Claude Code plugin (skills)
- `/trade-session` — start approval mode, relay tickets, take yes/no, summarize the day.
- `/scan` — one-off: affordability filter + current state of each ticker's checks.
- `/bot-status` — is the bot running (heartbeat), open position, today's P&L, guard state.
- `/report` — performance from any journal (bot, approval, backtest): win rate, avg
  win/loss, P&L by day and ticker, which checks blocked signals most, skip reasons.
- `/backtest` — run a backtest over a chosen period and explain the results.

## 5. Daily flow (both modes)

1. **9:25** — start-up: load config, confirm account (bot: paper), market open today,
   reconcile any open position from a previous crash (manage it first), write heartbeat.
2. **9:40** — affordability filter → today's scan list.
3. **9:45** — lock each ticker's opening range.
4. **Each 5-min close, 9:55–11:30** — fetch the closed bars (one multi-symbol request),
   evaluate `orb` per ticker, journal results. On a signal: guard check → contract →
   sizing → (approval: ticket & wait) → limit buy at mid, cancel if unfilled after 60 s.
5. **While a position is open** — every ~15 s evaluate `exits`; sell on the first hit.
6. **After 11:30** — manage exits only. **3:55** — flatten. **4:05** — write day summary, exit.

## 6. Error handling

- **Data errors:** retry with backoff (3 tries). If bars for a 5-min close are still
  missing after 60 s, skip that ticker for that candle and journal it.
- **Stale quotes:** if the option quote is older than 60 s during an open position,
  fall back to the stock-based exits only and log a warning.
- **Rate limits:** stay well under Alpaca's free limit (200 calls/min) — about
  one bars call per 5 min, a chain call per signal and per ticker at 9:40, ~8 calls/min
  while in a position.
- **Order not filled:** entry cancelled after 60 s → skip. Exit re-priced then
  market (§2.6).
- **Crash / restart:** heartbeat file every 15 s; on start the engine reconciles
  broker positions against the journal and resumes managing them. Known risk: while
  the process is down, nothing manages the open position — `/bot-status` flags a
  stale heartbeat.
- **Any unexpected exception in the loop:** journal it, stop new entries for the day,
  keep managing exits.

## 7. Testing

- **Unit tests** for every pure module: indicators against hand-computed values;
  `orb` against crafted bar sequences covering every check passing/failing, both
  directions, window edges (9:55, 11:30); `contracts` against fixture chains (delta,
  spread, OI, price limits, expiry choice); `exits` for every exit and ties;
  `guard` for each stop condition; `sizing` edge cases.
- **Session tests:** full simulated days with `FakeBroker`, fake data and a fake
  clock — signal → order → each exit type → guard trips → no more entries; approval
  ticket accept / reject / expire / re-quote; crash-reconcile.
- **Pricing:** Black-Scholes price/delta against published reference values;
  implied-vol solver round-trips (price → IV → price).
- **Backtest:** fixture days with known outcomes (target hit, stop hit, both in one
  bar → stop, time exit, guard trips, missing option data → skipped), and a check
  that live session and backtest produce the same signals on the same bars.
- **Recorded data:** `daytrader record` saves a real day's Alpaca bars and chains to
  fixtures so real sessions can be replayed as tests.
- **Smoke test:** `daytrader doctor` on Rabin's computer checks keys, paper
  endpoints, options permission and data access. (This build environment can't reach
  Alpaca, so live checks run on his machine.)

## 8. Tech choices

- Python 3.11+, `alpaca-py` (official SDK), `sqlite3`, `tomllib`, `argparse`; `pytest` for tests.
- No pandas — plain lists/dataclasses keep it small and fast.
- Repo is also a Claude Code plugin (`.claude-plugin/`); the engine installs with
  `pip install -e .` and skills call the `daytrader` CLI.

## 9. Out of scope (later stages)

- Stage 2: Time Machine (per-ticket odds from thousands of simulated sessions),
  deeper risk tooling.
- Stage 3: news monitor, earnings analyst.
- Stage 4: price alerts, Pelosi tracker.
- Live-money trading, phone notifications, more setups beyond ORB.
