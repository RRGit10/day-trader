---
name: scan
description: Show which tickers are affordable today and how each stock currently stands against the opening-range-breakout checks. Use when the user asks what daytrader is watching or why a ticker has or has not triggered.
---

# Scan

## Before you run anything

1. Work from the project folder that holds `config.toml`. If `config.toml` is not in the current folder, either use `daytrader --config <path-to-config.toml> ...` or ask the user where it is.
2. Run `daytrader --help`. If it fails, tell the user to run `pip install -e .` in the daytrader repo, then stop.
3. Never ask for, print or handle API keys. They live in `.env`.

## Steps

1. Run `daytrader scan`.
2. Explain the output in plain English: which tickers qualify (a contract costing $500 or less) and why others do not, and for each ticker which checks pass or fail (range break, EMA, VWAP, RSI).

## Rules

- Never place, approve or modify orders.
