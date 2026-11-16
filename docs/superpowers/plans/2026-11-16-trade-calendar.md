# Trade Calendar Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `daytrader calendar` serves a live, browser-based P&L calendar (month and year views) from the bot, approval and backtest journals.

**Architecture:** A pure data module reads a journal read-only and groups closed trades by ET close date; a stdlib `ThreadingHTTPServer` on 127.0.0.1 serves one static HTML page plus a JSON endpoint; the page renders the grids and re-fetches every 30 s.

**Tech Stack:** Python ≥3.11 stdlib only (`sqlite3`, `http.server`, `json`, `importlib.resources`), vanilla HTML/CSS/JS, pytest.

**Spec:** `docs/superpowers/specs/2026-11-16-trade-calendar-design.md`

## Global Constraints

- No new dependencies; page is one HTML file with inline CSS/JS, no CDN.
- Bind `127.0.0.1` only; default port `8765`.
- Journals: `<state_dir>/journal-{approval,bot,backtest}.db`, opened read-only (`file:...?mode=ro`, `uri=True`), never created.
- A trade belongs to the ET date of `closed_at`; open trades (no `closed_at`/`pnl`) are excluded.
- `% = sum(pnl) / sum(entry_price × qty × 100) × 100`, rounded to 2 dp; `0.0` when cost is 0.
- Page: dark theme; green cell for P&L > 0, red for < 0, grey `0.00 0.00%` for past/current days with no trades, `--` for future days; "Today" label for today in ET; journal switch Approval | Bot | Backtest (default Approval); refresh every 30 000 ms.
- No trading actions from the page.

## Review Focus

- A journal the running engine is writing to → reads must not fail or block it: connect with `timeout=5`, read-only (Task 1 test `test_reads_while_writer_holds_transaction`).
- A trade closed 19:30 ET (00:30 UTC next day) → counted on the ET day (Task 1 `test_groups_by_et_close_date`).
- Port 8765 already in use → `error: port 8765 is in use; try --port N`, exit 1, no traceback (Task 3 `test_calendar_port_in_use`).
- A break-even day (P&L exactly 0) → returned with pnl 0.0, page shows it grey, not green/red (Task 1 `test_break_even_day`, page rule in Task 2).
- Unknown URL or journal name → 404 / 400, never a traceback or a file read outside the package (Task 2 tests).

---

### Task 1: Day aggregation (`pnl_calendar.py`)

**Files:**
- Create: `src/daytrader/pnl_calendar.py` (named to avoid shadowing stdlib `calendar`)
- Test: `tests/test_pnl_calendar.py`

**Interfaces:**
- Produces:
  - `JOURNALS: tuple[str, ...] = ("approval", "bot", "backtest")`
  - `@dataclass(frozen=True) CalendarTrade(ticker: str, direction: str, qty: int, entry_price: float, exit_price: float, pnl: float, reason: str, opened_at: datetime, closed_at: datetime)` (datetimes in ET)
  - `@dataclass(frozen=True) CalendarDay(day: date, pnl: float, cost: float, pct: float, wins: int, losses: int, trades: tuple[CalendarTrade, ...])`
  - `load_days(db_path: Path) -> list[CalendarDay]` sorted by day
  - `days_payload(days: list[CalendarDay], today: date) -> dict` → `{"today": "YYYY-MM-DD", "days": [{"date", "pnl", "cost", "pct", "wins", "losses", "trades": [{"ticker","direction","qty","entry_price","exit_price","pnl","reason","opened_at","closed_at"}]}]}` (times as `HH:MM` ET strings)

- [ ] **Step 1: Create branch** `git switch -c feat/trade-calendar`; `git add docs/superpowers/specs/2026-11-16-trade-calendar-design.md docs/superpowers/plans/2026-11-16-trade-calendar.md`.

- [ ] **Step 2: Write failing tests** (build journals with `Journal` + `add_trade`-style helper as in `tests/test_report.py`, but set `qty` and entry):
  - `test_groups_by_et_close_date`: trades closing 2026-11-16 11:00 ET and 2026-11-16 19:30 ET land on one day `date(2026,11,16)` with 2 trades.
  - `test_totals_and_pct`: +$105 on 3×1.68 → `pnl == 105.0`, `cost == 504.0`, `pct == 20.83`, `wins == 1`, `losses == 0`.
  - `test_matches_report_daily_totals`: for the `build()` journal from `test_report.py`, `{d.day: d.pnl}` equals per-day sums from `summarize`'s inputs (2026-10-01 → -80.0) and `min(pnl) == summarize(j).worst_day`.
  - `test_break_even_day`: entry 1.00 exit 1.00 → day present, `pnl == 0.0`, `wins == losses == 0`.
  - `test_open_trade_excluded`: an opened, unclosed trade yields no day.
  - `test_missing_journal_not_created`: `load_days(tmp_path/"nope.db") == []` and the file does not exist afterwards.
  - `test_reads_while_writer_holds_transaction`: a second `sqlite3` connection runs `BEGIN; INSERT ...` (uncommitted) on the journal; `load_days` still returns the committed days.
  - `test_days_payload_shape`: keys and `"closed_at": "12:57"` formatting, `"today"` echoed.

- [ ] **Step 3: Run** `pytest tests/test_pnl_calendar.py -v` → FAIL (module missing).

- [ ] **Step 4: Implement** `load_days`/`days_payload`: `sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)`, select from `trades WHERE closed_at IS NOT NULL AND pnl IS NOT NULL`, parse with `datetime.fromisoformat(...).astimezone(ET)`; return `[]` if the file does not exist (check before connecting).

- [ ] **Step 5: Run** `pytest tests/test_pnl_calendar.py -v` → PASS; `pytest -q` → all pass.

- [ ] **Step 6: Commit** `feat: trade calendar day aggregation` (spec, plan, module, tests).

### Task 2: Server and page

**Files:**
- Create: `src/daytrader/calendar_server.py`, `src/daytrader/calendar_page.html`
- Modify: `pyproject.toml` (add `[tool.setuptools.package-data] daytrader = ["*.html"]`)
- Test: `tests/test_calendar_server.py`

**Interfaces:**
- Consumes: `JOURNALS`, `load_days`, `days_payload` (Task 1); `ET` from `daytrader.models`.
- Produces: `make_server(state_dir: Path, port: int, now: Callable[[], datetime]) -> ThreadingHTTPServer` bound to `127.0.0.1`; raises `OSError` when the port is taken.

- [ ] **Step 1: Write failing tests** (start `make_server(tmp_path, 0, now)` in a thread, read `server.server_address[1]`, `urllib.request` against it, `shutdown()` in teardown):
  - `test_index_serves_page`: `GET /` → 200, `text/html`, body contains `Calendar` and `/api/days`.
  - `test_api_days_returns_journal`: temp `journal-approval.db` with one trade → `GET /api/days?journal=approval` → 200 JSON, `days[0]["pnl"] == 105.0`, `today` from `now`.
  - `test_api_default_missing_journal`: `journal=bot` with no file → `{"days": []}` and no file created.
  - `test_api_bad_journal_400`: `journal=../x` → 400.
  - `test_unknown_path_404`: `GET /etc/passwd` → 404.
  - `test_binds_localhost_only`: `server.server_address[0] == "127.0.0.1"`.

- [ ] **Step 2: Run** `pytest tests/test_calendar_server.py -v` → FAIL.

- [ ] **Step 3: Implement `make_server`** with a `BaseHTTPRequestHandler` subclass: exact routes `/` (page via `importlib.resources.files("daytrader") / "calendar_page.html"`) and `/api/days`; JSON with `Cache-Control: no-store`; silence `log_message`.

- [ ] **Step 4: Implement `calendar_page.html`** per Global Constraints and the spec's Page section: header (Calendar, ◀ YYYY/MM ▶, Month | Year, journal switch), 6-row Sun–Sat grid with neighbouring-month days dimmed, month summary line (total P&L, winning/losing days, trades), click a day → trade list below (ticker, long/short, entry → exit, P&L, reason, close time), year view of 12 month cells (P&L and % from the same formula, click → month), `setInterval(load, 30000)`. Cell text `P&L.toFixed(2) pct.toFixed(2)%`; grey when pnl is 0.

- [ ] **Step 5: Run** `pytest -q` → all pass.

- [ ] **Step 6: Commit** `feat: trade calendar server and page`.

### Task 3: CLI command and launch config

**Files:**
- Modify: `src/daytrader/cli.py` (parser + dispatch + `_cmd_calendar`)
- Create: `.claude/launch.json`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `make_server` (Task 2), `settings.state_dir`, `deps.now()`.

- [ ] **Step 1: Write failing tests:**
  - `test_calendar_port_in_use`: hold a socket on a port, `main(["--config", cfg, "calendar", "--port", str(port)])` → returns 1, stdout `error: port {port} is in use; try --port N`.
  - `test_calendar_parser_default_port`: `_parser().parse_args(["calendar"]).port == 8765`.

- [ ] **Step 2: Run** → FAIL.

- [ ] **Step 3: Implement** `calendar` subparser (`--port`, int, default 8765, help "browser P&L calendar on 127.0.0.1"); `_cmd_calendar` prints `calendar: http://127.0.0.1:{port}  (Ctrl-C to stop)`, `serve_forever()`, catches `OSError` on bind → message above, returns 1. Add `.claude/launch.json` entry `{"name": "calendar", "runtimeExecutable": "daytrader", "runtimeArgs": ["calendar"], "port": 8765}`.

- [ ] **Step 4: Run** `pytest -q` → all pass.

- [ ] **Step 5: Commit** `feat: daytrader calendar command`.

### Task 4: Verify against real journals

- [ ] **Step 1:** Open it in the browser pane (`preview_start` name `calendar`); check November 2026 for the approval journal: Nov 16 green `105.00 20.83%`; compare each coloured day with `daytrader report --journal approval --start 2026-11-01` / `day-log --date`.
- [ ] **Step 2:** Switch to Bot and Backtest; check Year view and one month of 2026 for backtest; click a day and check its trade list.
- [ ] **Step 3:** Confirm the bot engine is unaffected (`daytrader status --mode bot` heartbeat fresh).
