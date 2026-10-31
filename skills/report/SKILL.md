---
name: report
description: Show a performance report from the bot, approval or backtest journal. Use when the user asks how the strategy has performed.
---

# Report

## Before you run anything

1. Work from the project folder that holds `config.toml`. If `config.toml` is not in the current folder, either use `daytrader --config <path-to-config.toml> ...` or ask the user where it is.
2. Run `daytrader --help`. If it fails, tell the user to run `pip install -e .` in the daytrader repo, then stop.
3. Never ask for, print or handle API keys. They live in `.env`.

## Steps

1. Ask which journal (`bot`, `approval` or `backtest`) and date range if not given. Dates are YYYY-MM-DD.
2. Run `daytrader report --journal <name>`, adding `--start` and `--end` if requested.
3. Explain in plain English: number of trades, win rate, total and average P&L, worst losses, and any caveats printed in the report. Do not oversell small samples.

## Rules

- Never place, approve or modify orders.
