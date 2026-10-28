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
`/report` can show how the rule performs.

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
| `cli` | `run`, `scan`, `approve`, `reject`, `status`, `report`, `doctor`, `record` | session |

Pure functions (`indicators`, `orb`, `contracts`, `sizing`, `exits`) hold all
trading decisions and never touch the network, so they can be tested exhaustively.

### 4.1 Claude Code plugin (skills)
- `/trade-session` — start approval mode, relay tickets, take yes/no, summarize the day.
- `/scan` — one-off: affordability filter + current state of each ticker's checks.
- `/bot-status` — is the bot running (heartbeat), open position, today's P&L, guard state.
- `/report` — performance from either journal: win rate, avg win/loss, P&L by day and
  ticker, which checks blocked signals most, skip reasons.

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

- Stage 2: Time Machine / backtesting (option P&L via a pricing model), deeper risk tooling.
- Stage 3: news monitor, earnings analyst.
- Stage 4: price alerts, Pelosi tracker.
- Live-money trading, phone notifications, more setups beyond ORB.
