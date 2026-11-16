# Trade calendar — design

Date: 2026-11-16 · Status: draft for approval

## Intent

A month calendar in the browser, like a brokerage P&L calendar, where every day
shows that day's P&L and is green for a winning day and red for a losing day. The
user keeps it open in the browser pane beside the Claude Code chat, including during
an approval session, so it should stay current without re-running anything.

## Goals

- See at a glance which days won and which lost, month by month.
- Numbers that agree with `daytrader report` and `daytrader day-log`.
- Stays up to date while the page is open: when the engine closes a trade, today's
  cell changes on its own.

## Non-goals

- No trading actions of any kind from the page (no approve, reject, orders).
- No charts beyond the calendar grid; no editing of journal data.
- Not reachable from other machines.

## Anti-goals (a failure even if it works)

- The calendar writes to, locks, or slows down a journal the engine is using.
- A day's total differs from what `report` / `day-log` say for that day.

## Constraints

- No new dependencies: Python standard library only (`http.server`, `sqlite3`,
  `json`); the page is one HTML file with inline CSS/JS, no CDN.
- Journals are the existing SQLite files `state/journal-{approval,bot,backtest}.db`.
- Days are ET calendar days, and a trade belongs to the day it **closed**, as in
  `report` and `day-log`.

## Approaches

- **A. Generated static page** (`daytrader calendar` writes `state/calendar.html`).
  Nothing runs in the background, but it is a snapshot that must be re-run after each
  trade.
- **B. Small local server** (`daytrader calendar --serve`). Always current; one more
  process while the page is open. **Picked by the user**, because the page is meant
  to stay open beside the chat during sessions.
- (Rejected) Publishing it as a hosted artifact: puts private trading data on a
  server and cannot read the local journals live.

## What gets built

### Command

`daytrader calendar [--port 8765]` starts a server on `127.0.0.1` only, prints the
URL, and runs until Ctrl-C. It respects `--config` (for `state_dir`). A
`.claude/launch.json` entry named `calendar` lets Claude open it in the browser pane.

### Server

- `GET /` → the page.
- `GET /api/days?journal=approval|bot|backtest` → JSON of every day with closed
  trades in that journal: date, P&L, % (below), trade count, wins, losses, and the
  day's trades (ticker, direction, entry, exit, P&L, exit reason, open/close times).
  Unknown journal name → 400.
- Journals are opened **read-only** (`sqlite3` URI `mode=ro`), fresh per request. A
  journal file that does not exist yet returns no days instead of creating it (the
  existing `Journal` class would create the file, so it is not used here).
- A missing `pnl`/`closed_at` (open trade) is left out.

### Page (dark theme, as in the user's screenshot)

- Header: "Calendar", ◀ YYYY/MM ▶, a Month | Year switch, and a journal switch
  (Approval | Bot | Backtest; default Approval).
- **Month view:** Sun–Sat grid, six rows, days of the neighbouring months dimmed.
  Each cell shows the day number ("Today" for today in ET) and `P&L  %`:
  - green background for P&L > 0, red for P&L < 0;
  - a past or current day with no closed trade: neutral grey, `0.00 0.00%` (as in the
    screenshot);
  - future days: `--`.
- Under the grid: the month's total P&L, winning / losing day counts and trade count.
- **Clicking a day** lists its trades below the grid (ticker, long/short, entry →
  exit, P&L, exit reason, close time).
- **Year view:** twelve month cells for the selected year, each with the month's
  P&L and %, coloured the same way; clicking one opens that month.
- The page re-fetches the data every 30 seconds while open, so today's cell updates
  when the engine closes a trade.

### The % figure (builder's call, flagged for the user)

daytrader stores no account balance, so % is the day's P&L divided by the money put
into that day's closed trades: `sum(pnl) / sum(entry_price × qty × 100)`. Today:
+$105.00 on $504.00 = +20.8%. A month's or year's % uses the same formula over all
its trades.

## Testing

- Unit tests for the day aggregation: trades grouped by ET close date, totals and %
  match `summarize()` for the same journal, open trades excluded, a missing journal
  file gives no days and is not created.
- A test that starts the server on a free port, fetches `/api/days` for a temp
  journal and `/`, and checks a bad journal name gets 400.
- Manual check: open it in the browser pane against the real approval journal and
  compare November with `daytrader report --journal approval --start 2026-11-01`.

## Left to the builder

Module layout (expected: `src/daytrader/calendar.py` for data + server, the page as a
package data file or an inline string), exact colours and spacing, port-in-use
message.
