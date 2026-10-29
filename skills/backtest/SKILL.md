---
name: backtest
description: Replay the strategy over past trading days to estimate how it would have performed. Use when the user wants to test the rules on history or on specific tickers.
---

# Backtest

## Before you run anything

1. Work from the project folder that holds `config.toml`. If `config.toml` is not in the current folder, either use `daytrader --config <path-to-config.toml> ...` or ask the user where it is.
2. Run `daytrader --help`. If it fails, tell the user to run `pip install -e .` in the daytrader repo, then stop.
3. Never ask for, print or handle API keys. They live in `.env`.

## Steps

1. Run `daytrader backtest` (default: last 12 months). Options: `--start YYYY-MM-DD`, `--end YYYY-MM-DD`, `--tickers SPY,QQQ`. Data starts February 2024. The first run downloads data and can take a while; later runs use the cache.
2. Explain the results in plain English, including the report's caveats (assumed spreads, SIP versus IEX data, skipped "no data" signals). A backtest is an estimate, not a prediction.

## Rules

- Never place, approve or modify orders.
