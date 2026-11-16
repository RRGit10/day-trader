"""Local web server for the P&L calendar: the page plus a read-only JSON endpoint."""
from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .models import ET
from .pnl_calendar import JOURNALS, days_payload, load_days

HOST = "127.0.0.1"


def make_server(state_dir: Path, port: int, now: Callable[[], datetime]) -> ThreadingHTTPServer:
    """Bind the calendar server on 127.0.0.1:port (OSError if the port is taken)."""
    state_dir = Path(state_dir)

    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, ctype: str, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _text(self, status: int, message: str) -> None:
            self._send(status, "text/plain; charset=utf-8", message.encode())

        def do_GET(self) -> None:  # noqa: N802 (http.server naming)
            url = urlsplit(self.path)
            if url.path == "/":
                page = (files("daytrader") / "calendar_page.html").read_bytes()
                self._send(200, "text/html; charset=utf-8", page)
                return
            if url.path == "/api/days":
                name = parse_qs(url.query).get("journal", [""])[0]
                if name not in JOURNALS:
                    self._text(400, f"journal must be one of {', '.join(JOURNALS)}")
                    return
                days = load_days(state_dir / f"journal-{name}.db")
                body = json.dumps(days_payload(days, now().astimezone(ET).date()))
                self._send(200, "application/json", body.encode())
                return
            self._text(404, "not found")

        def log_message(self, format: str, *args) -> None:  # keep the terminal quiet
            pass

    return ThreadingHTTPServer((HOST, port), Handler)
