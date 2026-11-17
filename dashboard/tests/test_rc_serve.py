"""RC1 lane 4 self-test — V11 + V12 partial + one real end-to-end socket call.

template.py (lane 1) and actions.py (lane 2) are not merged yet, so this suite
stubs them under their pinned package paths; the integration lane runs the same
suite against the real modules. Run from the workspace root:

    pytest build/dashboard/tests/test_rc_serve.py -q
"""

from __future__ import annotations

from pathlib import Path
import io
import json
import os
import sys
import sys
import threading
import urllib.request
from urllib.error import HTTPError
from wsgiref.simple_server import make_server

import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, REPO_ROOT)

from dashboard.console import serve  # noqa: E402

READ_TOKEN = "test-read-token-rc1"
WRITE_TOKEN = "test-write-token-rc1"

BOARD = {"exec_summary": {"headlines": ["headline one"]}, "zone_b": [], "zone_a": []}

BANNER = serve.READONLY_BANNER_TEXT
FALLBACK_NOTICE = serve.FALLBACK_NOTICE


def _real_ctx(tmp_db=True):
    """Real Queue + MandateFlow from the canonical backend (same module copy
    console.actions imports) so POST tests exercise the real action handler.

    sqlite conns are created with check_same_thread=False so the wsgiref
    server thread (real-socket e2e) may serve them."""
    if "dashboard" not in sys.path:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import sqlite3 as _sq
    _orig_connect = _sq.connect
    _sq.connect = lambda *a, **k: _orig_connect(
        *a, **{**k, "check_same_thread": False})
    from exception_queue.queue import Queue, Finding
    from exception_queue.mandate_flow import MandateFlow
    import tempfile, os as _os
    db = _os.path.join(tempfile.mkdtemp(), "q.db") if tmp_db else ":memory:"
    q = Queue(db_path=db)
    item = q.admit(Finding(condition_key="c1", claim="claim-c1",
                           impact_class="degraded", admission_score=0.9,
                           eval_passed=True, proposed_action="act-1"))
    assert item is not None, "fixture finding not admitted"
    flow = MandateFlow(q, db_path=_os.path.join(tempfile.mkdtemp(), "m.db"),
                       write_token=None)
    return {"queue": q, "mandate_flow": flow}, q, item.item_id


# ---------------------------------------------------------------------------
# WSGI helpers (no middleware, direct environ)
# ---------------------------------------------------------------------------

def make_app(write_token=WRITE_TOKEN):
    return serve.build_app(lambda: dict(BOARD), {"queue": "stub-ctx"},
                           {"read_token": READ_TOKEN, "write_token": write_token})


def call(app, method, path, token=None, body=None):
    environ = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "SERVER_NAME": "test",
        "SERVER_PORT": "80",
        "wsgi.input": io.BytesIO(body or b""),
        "wsgi.errors": io.StringIO(),
        "wsgi.url_scheme": "http",
    }
    if body is not None:
        environ["CONTENT_LENGTH"] = str(len(body))
    if token:
        environ["HTTP_AUTHORIZATION"] = f"Bearer {token}"
    captured = {}

    def start_response(status, headers):
        captured["status"] = status
        captured["headers"] = dict(headers)

    out = app(environ, start_response)
    if not isinstance(out, bytes):  # tolerate iterable-style apps
        out = b"".join(out)
    return int(captured["status"].split()[0]), captured["headers"], out


def assert_no_redirect(status, headers):
    assert 300 <= status < 400 or True  # placeholder for readability
    assert not (300 <= status < 400), f"redirect issued: {status}"
    assert not any(k.lower() == "location" for k in headers), "Location header present"


# ---------------------------------------------------------------------------
# V11 — bearer gating, 401 page, no redirect
# ---------------------------------------------------------------------------

def test_v11_valid_bearer_returns_200_with_board_html():
    status, headers, body = call(make_app(), "GET", "/dashboard", token=READ_TOKEN)
    assert_no_redirect(status, headers)
    assert status == 200
    assert b"Operator Console" in body
    assert b"headline one" in body


def test_v11_no_bearer_returns_401_credentials_page():
    status, headers, body = call(make_app(), "GET", "/dashboard")
    assert_no_redirect(status, headers)
    assert status == 401
    assert b"credentials required" in body.lower()
    assert b"login" not in body.lower()  # no login form, ever


def test_v11_wrong_bearer_returns_401_not_redirect():
    status, headers, body = call(make_app(), "GET", "/dashboard", token="nope")
    assert_no_redirect(status, headers)
    assert status == 401
    assert b"credentials required" in body.lower()


def test_v11_write_token_does_not_grant_read():
    status, _, _ = call(make_app(), "GET", "/dashboard", token=WRITE_TOKEN)
    assert status == 401


def test_v11_static_snapshot_and_docs():
    status, _, body = call(make_app(), "GET", "/dashboard/static-snapshot",
                           token=READ_TOKEN)
    assert status == 200
    assert b"Operator Console" in body
    status, _, body = call(make_app(), "GET", "/dashboard/docs", token=READ_TOKEN)
    assert status == 200
    status, _, _ = call(make_app(), "GET", "/dashboard/docs")
    assert status == 401  # docs page is bearer-gated too


# ---------------------------------------------------------------------------
# V12 partial — read-only mode when write token absent
# ---------------------------------------------------------------------------

def test_v12_no_write_token_renders_readonly_with_banner():
    app = make_app(write_token=None)
    status, _, body = call(app, "GET", "/dashboard", token=READ_TOKEN)
    assert status == 200
    html = body.decode("utf-8")
    assert serve.FALLBACK_NOTICE in html
    assert BANNER in html
    assert "disabled" in html  # actions render disabled
    assert "<script>" not in html  # no POST helper when read-only


def test_v12_write_token_present_enables_interactive_no_banner():
    app = make_app(write_token=WRITE_TOKEN)
    status, _, body = call(app, "GET", "/dashboard", token=READ_TOKEN)
    assert status == 200
    html = body.decode("utf-8")
    assert "<script>" in html  # POST helper wired when interactive
    assert BANNER not in html


# ---------------------------------------------------------------------------
# POST endpoints — actions.handle_queue_action contract
# ---------------------------------------------------------------------------

def test_post_returns_status_dict_from_actions_contract():
    ctx, queue, item_id = _real_ctx()
    app = serve.build_app(lambda: dict(BOARD), ctx,
                          {"read_token": READ_TOKEN, "write_token": WRITE_TOKEN})
    body = json.dumps({"item_id": item_id, "payload": {"phase": "confirm"}}).encode()
    status, headers, out = call(app, "POST", "/dashboard/queue/approve",
                                token=WRITE_TOKEN, body=body)
    assert_no_redirect(status, headers)
    assert status == 200
    assert headers["Content-Type"] == "application/json"
    result = json.loads(out.decode("utf-8"))
    # real actions contract: two-phase approve confirm -> panel data, NO write
    assert result["phase"] == "confirm"
    assert result["prefill"]["item_id"] == item_id
    assert set(result) >= {"status", "message", "attempts", "phase"}


def test_post_requires_write_token():
    body = json.dumps({"item_id": "row-1"}).encode()
    status, _, _ = call(make_app(), "POST", "/dashboard/queue/approve",
                        token=READ_TOKEN, body=body)
    assert status == 401
    status, _, _ = call(make_app(), "POST", "/dashboard/queue/approve", body=body)
    assert status == 401


def test_post_unknown_action_and_bad_body():
    app = make_app()
    status, _, _ = call(app, "POST", "/dashboard/queue/bogus",
                        token=WRITE_TOKEN, body=b"{}")
    assert status == 404
    status, _, _ = call(app, "POST", "/dashboard/queue/approve",
                        token=WRITE_TOKEN, body=b"not json")
    assert status == 400
    status, _, _ = call(app, "POST", "/dashboard/queue/approve",
                        token=WRITE_TOKEN, body=b"{}")
    assert status == 400  # missing item_id


# ---------------------------------------------------------------------------
# static snapshot helper
# ---------------------------------------------------------------------------

def test_write_static_snapshot(tmp_path):
    target = tmp_path / "dashboard.html"
    serve.write_static_snapshot(str(target), lambda: dict(BOARD))
    html = target.read_text(encoding="utf-8")
    assert serve.FALLBACK_NOTICE in html
    assert BANNER in html


# ---------------------------------------------------------------------------
# real end-to-end: wsgiref server, real socket, real HTTP client
# ---------------------------------------------------------------------------

def test_end_to_end_real_socket_wsgiref():
    ctx, _q, item_id = _real_ctx()
    app = serve.build_app(lambda: dict(BOARD), ctx,
                          {"read_token": READ_TOKEN, "write_token": WRITE_TOKEN})
    server = make_server("127.0.0.1", 0, app)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{port}"

        # 200 with a valid bearer over a real socket
        req = urllib.request.Request(
            f"{base}/dashboard",
            headers={"Authorization": f"Bearer {READ_TOKEN}"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 200
            html = resp.read().decode("utf-8")
            assert "Operator Console" in html
            assert "headline one" in html

        # 401 without a bearer — and it is NOT a redirect
        try:
            urllib.request.urlopen(f"{base}/dashboard", timeout=5)
            raise AssertionError("expected 401")
        except HTTPError as e:
            assert e.code == 401
            assert b"credentials required" in e.read().lower()

        # POST round-trip over the socket (real two-phase approve, confirm phase)
        body = json.dumps({"item_id": item_id, "payload": {"phase": "confirm"}}).encode()
        req = urllib.request.Request(
            f"{base}/dashboard/queue/approve", data=body, method="POST",
            headers={"Authorization": f"Bearer {WRITE_TOKEN}",
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 200
            result = json.loads(resp.read().decode("utf-8"))
            assert result["phase"] == "confirm"
            assert set(result) >= {"status", "message", "attempts", "phase"}
    finally:
        server.shutdown()
        thread.join(timeout=5)
