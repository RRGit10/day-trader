---
name: bot-status
description: Show whether the daytrader bot is running, its open position, today's P&L and guard state. Use when the user asks how the bot or approval session is doing.
---

# Bot status

## Before you run anything

1. Work from the project folder that holds `config.toml`. If `config.toml` is not in the current folder, either use `daytrader --config <path-to-config.toml> ...` or ask the user where it is.
2. Run `daytrader --help`. If it fails, tell the user to run `pip install -e .` in the daytrader repo, then stop.
3. Never ask for, print or handle API keys. They live in `.env`.

## Steps

1. Run `daytrader status` (the default is the bot; use `daytrader status --mode approval` for an approval session).
2. Explain in plain English: is the heartbeat fresh, is a position open, today's P&L, and whether the daily guard has stopped new entries.
3. If the heartbeat is stale during market hours, say so and suggest checking the process or the scheduler.

## Rules

- Never place, approve or modify orders, and never start or stop the engine unless the user asks.
