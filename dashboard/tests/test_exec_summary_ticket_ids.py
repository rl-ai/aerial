"""F2 (buglist 2026-11-14): transcript ticket-id verification.

Agent transcripts occasionally hallucinate TK-/EM- ids; the report layer
must verify every id against the hub (GET /tickets/<id> with the bearer)
before rendering, and mark unverifiable ids with " (unverified id)".

Hermetic: a mock hub in-thread answers 200 for TK-REAL... and 404 for
TK-FAKE...; no real hub is touched.
"""
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
if PKG not in sys.path:
    sys.path.insert(0, PKG)

import exec_summary as es  # noqa: E402


class _MockHub(HTTPServer):
    def __init__(self):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.token = "tok-test"


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        hub = self.server
        auth = self.headers.get("Authorization", "")
        if auth != f"Bearer {hub.token}":
            self.send_response(401)
            self.end_headers()
            return
        tid = self.path.rsplit("/", 1)[-1]
        code = 200 if tid.startswith("TK-REAL") else 404
        self.send_response(code)
        self.end_headers()

    def log_message(self, *a):  # silence
        pass


@pytest.fixture()
def mock_hub():
    hub = _MockHub()
    t = threading.Thread(target=hub.serve_forever, daemon=True)
    t.start()
    yield hub
    hub.shutdown()
    hub.server_close()


def test_agent_actions_line_marks_fake_ids(mock_hub, monkeypatch):
    monkeypatch.setenv("A5_HUB_URL", f"http://127.0.0.1:{mock_hub.server_port}")
    monkeypatch.setenv("A5_HUB_TOKEN", mock_hub.token)
    actions = {"available": True, "sources": {}, "phrases": [
        "hierarchy run TK-REAL123", "hierarchy run TK-FAKE999"]}
    line = es.agent_actions_line(actions)
    assert "TK-REAL123" in line
    assert "TK-REAL123 (unverified id)" not in line
    assert "TK-FAKE999 (unverified id)" in line


def test_verify_ticket_ids_fail_closed_on_hub_down(monkeypatch):
    # hub unreachable -> nothing can be verified -> everything is marked
    monkeypatch.setenv("A5_HUB_URL", "http://127.0.0.1:1")  # nothing listens
    monkeypatch.setenv("A5_HUB_TOKEN", "x")
    out = es.verify_ticket_ids("did TK-ABC123 and EM-XY77")
    assert "TK-ABC123 (unverified id)" in out
    assert "EM-XY77 (unverified id)" in out


def test_verify_ticket_ids_no_ids_untouched():
    assert es.verify_ticket_ids("plain text, no ids") == "plain text, no ids"
    assert es.verify_ticket_ids("") == ""
