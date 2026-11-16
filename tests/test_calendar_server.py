import json
import threading
import urllib.error
import urllib.request
from datetime import datetime

import pytest

from daytrader.calendar_server import make_server
from daytrader.journal import Journal
from daytrader.models import ET
from test_pnl_calendar import add_trade

NOW = datetime(2026, 11, 16, 13, 0, tzinfo=ET)


@pytest.fixture
def served(tmp_path):
    server = make_server(tmp_path, 0, lambda: NOW)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    yield server, base, tmp_path
    server.shutdown()
    server.server_close()


def get(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.status, r.headers, r.read().decode()


def status_of(url):
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(url, timeout=5)
    return exc.value.code


def test_index_serves_page(served):
    _, base, _ = served
    status, headers, body = get(base + "/")
    assert status == 200
    assert headers["Content-Type"].startswith("text/html")
    assert "Calendar" in body and "/api/days" in body


def test_api_days_returns_journal(served):
    _, base, state = served
    j = Journal(state / "journal-approval.db")
    add_trade(j, "NFLX", 3, 1.68, 2.03, datetime(2026, 11, 16, 12, 57, tzinfo=ET))
    j.close()
    status, headers, body = get(base + "/api/days?journal=approval")
    data = json.loads(body)
    assert status == 200
    assert headers["Content-Type"].startswith("application/json")
    assert headers["Cache-Control"] == "no-store"
    assert data["today"] == "2026-11-16"
    assert data["days"][0]["pnl"] == 105.0


def test_api_default_missing_journal(served):
    _, base, state = served
    _, _, body = get(base + "/api/days?journal=bot")
    assert json.loads(body)["days"] == []
    assert not (state / "journal-bot.db").exists()


def test_api_bad_journal_400(served):
    _, base, _ = served
    assert status_of(base + "/api/days?journal=../x") == 400
    assert status_of(base + "/api/days") == 400


def test_unknown_path_404(served):
    _, base, _ = served
    assert status_of(base + "/etc/passwd") == 404
    assert status_of(base + "/calendar_page.html") == 404


def test_binds_localhost_only(served):
    server, _, _ = served
    assert server.server_address[0] == "127.0.0.1"
