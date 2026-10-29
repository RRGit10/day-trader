# daytrader

A Python engine that day-trades options using an opening-range breakout (ORB) strategy, plus a Claude Code plugin that lets you run it from a chat. **Stage 1 is paper trading only.** No real money is ever used.

It runs the same rules in two modes:

- **Approval mode**: the engine finds a signal and writes a trade ticket. Nothing is bought until you say yes.
- **Bot mode**: the engine takes every signal on its own, on a separate paper account. This builds a track record of what the rules do without you in the loop.

You can also **backtest** the rules on past days (from February 2024 onward) in minutes.

## The strategy

All of this is configurable in `config.toml`. These are the defaults.

| Part | Rule |
|---|---|
| Opening range | High and low of 9:30-9:45 AM ET |
| Signal | Two 5-minute closes in a row beyond the range (above the high = long call, below the low = short, via a put) |
| Filters | Close above (long) or below (short) the 9 EMA and VWAP; RSI(14) above 60 (long) or below 40 (short) |
| Entry window | Signals on 5-minute closes from 9:55 to 11:30 AM ET |
| Contract | Weekly expiry, at least 1 day out (never 0DTE); delta 0.60-0.70 (target 0.65); spread at most 5% of mid; open interest at least 500 |
| Size | One contract costs at most $500, and a trade risks at most $500 |
| Exit: option | +20% take profit, -10% stop (on option mid) |
| Exit: stock | Stock crosses the range midpoint against you, or moves 1R in your favor (R = distance from entry to range midpoint) |
| Exit: time | Everything is sold at 3:55 PM ET |
| Daily stop | No new entries after +$100 realized, 2 losing trades, or -$100 realized |
| Positions | One open position at a time |

A stop can fill worse than -10% in a fast market. Free Alpaca stock data is IEX-only, so VWAP and volume are approximations.

## Setup

1. **Two Alpaca paper accounts.** Create two paper-trading accounts at alpaca.markets, one for approval mode and one for bot mode.
2. **Enable options trading** on both paper accounts in the Alpaca dashboard.
3. **Keys.** Generate a paper API key and secret for each account. Copy `.env.example` to `.env` and fill it in:

   ```
   APPROVAL_API_KEY=...
   APPROVAL_API_SECRET=...
   BOT_API_KEY=...
   BOT_API_SECRET=...
   ```

   `.env` is git-ignored. Never paste keys into a chat.
4. **Config.** Copy `config.example.toml` to `config.toml` and edit the ticker list or any rule you want to change.
5. **Install** (Python 3.11 or newer), from the repo folder:

   ```
   pip install -e .
   ```
6. **Check everything:**

   ```
   daytrader doctor
   ```

   It checks your keys, that both accounts are paper, that options are enabled, and data access. Fix anything it flags before going on.

Run `daytrader` from the folder that holds `config.toml`, or pass `daytrader --config /path/to/config.toml <command>`. Runtime files (journals, tickets, caches) go in a `state/` folder next to the config.

## Daily use

### Approval mode (you decide each trade)

In Claude Code, from the project folder, before 9:45 AM ET on a trading day:

```
/daytrader:trade-session
```

Claude checks `doctor`, starts the engine in the background and watches for tickets. Each ticket shows the ticker, direction, contract, delta, prices, cost (your maximum loss) and exit levels. Claude only approves or rejects after you explicitly answer yes or no for that ticket. Tickets expire after 2 minutes. When you approve, the engine re-quotes and asks again if the price moved more than 5%. Exits then run automatically. At the end you get a report.

Without Claude, use two terminals:

```
daytrader run --mode approval     # leave running, start before 9:45 AM ET
daytrader pending                 # list tickets
daytrader approve <id>
daytrader reject <id>
```

### Bot mode (fully automatic, paper)

```
daytrader run --mode bot
```

To start it automatically at 9:25 AM ET every weekday, see [scripts/schedule-bot.md](scripts/schedule-bot.md). Check on it any time:

```
daytrader status
daytrader report --journal bot
```

### Other commands

```
daytrader scan                                  # which tickers are affordable, and where each stands on the checks
daytrader status --mode approval                # approval session status
daytrader report --journal approval --start 2026-11-02
daytrader backtest                              # last 12 months
daytrader backtest --start 2025-01-01 --end 2025-06-30 --tickers SPY,QQQ
```

## Claude Code plugin

Once the repo is on GitHub:

```
/plugin marketplace add <your-github-user>/daytrader
/plugin install daytrader@daytrader
```

The skills then appear as `/daytrader:trade-session`, `/daytrader:scan`, `/daytrader:bot-status`, `/daytrader:report` and `/daytrader:backtest`. The plugin only gives Claude instructions; you still need the Python package installed (`pip install -e .`) and a `config.toml` and `.env` in your project folder. The skills never handle API keys and never place orders except an approval you explicitly gave in the chat.

## Risks and disclaimer

This is an educational, personal tool. It is not financial advice. Stage 1 is paper trading only, and paper results do not guarantee live results: real fills are worse, and IEX data and indicative option quotes differ from what you would trade on. Options can lose 100% of the premium paid. A backtest is an estimate built on assumptions (assumed spreads, modeled deltas), not a prediction. Going live would be a separate, deliberate change, and you would be responsible for any losses.

## Credit

Inspired by Jake Schincariol's day-trader-skill project. No code from it is used here.

## License

MIT. See [LICENSE](LICENSE).
