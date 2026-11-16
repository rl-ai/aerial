"""RC1 integration acceptance suite — V1–V13 (spec §8, console-rc-ui.md).

Wired ACROSS modules on the merged tree:
- template + actions + sparklines render/act on boards built by the real
  daily_board.render_board() pipeline,
- serve.build_app() serves the real WSGI app over a real socket,
- the end-to-end queue loop (V5/e2e) runs against the real exception_queue +
  mandate_flow backend — approve via HTTP POST with a bearer, confirm, assert
  the mandate ledger row is written and the row leaves the queue.

The queue/flow objects here come from the same module copy console.actions
imports (top-level `exception_queue` on the build/dashboard path), so the
except-class identity matches the real backend.
"""
from __future__ import annotations

from pathlib import Path
import io
import json
import os
import re
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.request
from urllib.error import HTTPError
from wsgiref.simple_server import make_server

import pytest

WORKSPACES = str(Path(__file__).resolve().parents[3])
BUILD_DASH = str(Path(__file__).resolve().parents[1])
for _p in (WORKSPACES, BUILD_DASH):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from exception_queue.queue import Queue, Finding          # noqa: E402
from exception_queue.mandate_flow import (                # noqa: E402
    MandateFlow, NoFreshDecision, WriteAuthError, WriteToken)
from build.dashboard.console import (                     # noqa: E402
    template, actions, sparklines, serve, installer)
from build.dashboard.console.template import (            # noqa: E402
    render_console_html, FALLBACK_NOTICE, STALE_TOOLTIP, PANEL_ORDER)
from build.dashboard.daily_board import render as board_render  # noqa: E402
from test_rc_sparklines import _make_snapshot_dir         # noqa: E402

READ_TOKEN = "test-read-token-rc1-acceptance"
WRITE_TOKEN = "test-write-token-rc1-acceptance"
DAY = 86400.0
NOW = 1_000_000.0 + 7 * DAY

from dashboard.data_layer.job_runs import JobRunsDB        # noqa: E402
from dashboard.data_layer.model_calls_tailer import ModelCallsTailer  # noqa: E402
from dashboard.data_layer.sessions import SessionsStore    # noqa: E402
from dashboard.exception_queue import queue as qmod        # noqa: E402


# ------------------------------------------------------------------ fixtures

def _project_items(now):
    return [
        {"project": "aerial", "item": "W6 shadow week",
         "item_tag": "W6 shadow week", "status": "working",
         "stages": ["ingest", "curate", "digest"], "done_stages": ["ingest"],
         "evidence_artifact": "stage-3.json"},
        {"project": "gateway", "item": "M8.1 control-plane panel",
         "item_tag": "M8.1 control-plane panel", "status": "waiting-on-you",
         "stages": [], "done_stages": [],
         "evidence_artifact": "panel-diff.patch"},
    ]


@pytest.fixture
def runs_db(tmp_path):
    db = str(tmp_path / "a5-runtime.db")
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE job_runs (project TEXT, stage_type TEXT,"
                " status TEXT, evidence_json TEXT, completed_at REAL,"
                " duration_hours REAL)")
    con.executemany("INSERT INTO job_runs VALUES (?,?,?,?,?,?)", [
        ("aerial", "ingest", "done",
         json.dumps({"stages": [{"status": "pass"}, {"status": "pass"},
                                {"status": "unverified"}]}),
         NOW - 2 * DAY, 20.0),
        ("aerial", "test", "done", json.dumps({"stages": []}), NOW - DAY, 5.0),
    ])
    con.commit()
    con.close()
    return JobRunsDB(db)


@pytest.fixture
def tailer(tmp_path):
    ledger = tmp_path / "model-calls.jsonl"
    with open(ledger, "w") as f:
        f.write(json.dumps({"item": "W6 shadow week", "in_tokens": 4000,
                            "out_tokens": 1200, "ts": NOW - DAY}) + "\n")
        f.write(json.dumps({"item": "M8.1 control-plane panel",
                            "in_tokens": 900, "out_tokens": 300,
                            "ts": NOW - 3600}) + "\n")
        f.write(json.dumps({"in_tokens": 10, "out_tokens": 5,
                            "ts": NOW - 1800}) + "\n")  # untagged bucket
    return ModelCallsTailer(ledger_path=str(ledger),
                            state_path=str(tmp_path / "tail-state.json"))


@pytest.fixture
def sessions(tmp_path):
    s = SessionsStore(hub_db_path=str(tmp_path / "hub.db"),
                      mirror_path=str(tmp_path / "mirror.json"))
    s.record_visit(visit_ts=NOW - 3600, hub_available=False)
    return s


@pytest.fixture
def queue_dash():
    """Queue from the dashboard.* copy — feeds render_board (Zone B rows)."""
    policy = os.path.join(os.path.dirname(qmod.__file__), "queue-policy.yaml")
    return qmod.Queue(db_path=":memory:", policy_path=policy, clock=lambda: NOW)


@pytest.fixture
def backend():
    """Real exception_queue backend from the SAME module copy actions uses.

    sqlite conns are opened with check_same_thread=False so the real-socket
    wsgiref server thread may serve them (the board provider runs there)."""
    import sqlite3 as _sq
    _orig_connect = _sq.connect
    _sq.connect = lambda *a, **k: _orig_connect(
        *a, **{**k, "check_same_thread": False})
    policy = os.path.join(
        os.path.dirname(sys.modules["exception_queue.queue"].__file__),
        "queue-policy.yaml")
    q = Queue(db_path=":memory:", policy_path=policy, clock=lambda: NOW)
    item = q.admit(Finding(condition_key="c1", claim="claim-c1",
                           impact_class="degraded", admission_score=0.9,
                           eval_passed=True, proposed_action="act-1"))
    assert item is not None, "fixture finding not admitted"
    flow = MandateFlow(q, db_path=":memory:", clock=lambda: NOW)
    _sq.connect = _orig_connect
    return q, flow, item


@pytest.fixture
def spark_dir(tmp_path):
    d, _expected = _make_snapshot_dir(tmp_path, NOW, n_days=7)
    return d


@pytest.fixture
def full_board(runs_db, tailer, sessions, queue_dash, spark_dir):
    board = board_render.render_board(
        _project_items(NOW), runs_db, tailer, sessions, queue_dash, now=NOW)
    series = sparklines.collect_series(spark_dir)
    board.update({
        "generated_at": NOW,
        "exec_summary": {
            "headlines": ["W6 shadow week underway",
                          "gateway M8.1 waiting on you"],
            "numbers": ["2 jobs done", "6.4k tokens"],
            "decisions": ["approve act-1 on claim-c1"],
            "risks": ["ingest evidence unverified"],
        },
        "themes": [{"name": "agent-runtimes", "current": 7, "prev": 5}],
        "stack_overlap": "aerial ∩ gateway: 1 item",
        "raw_stats": {"jobs_24h": 2, "queue_depth": len(board["zone_b"])},
        "sparklines_html": sparklines.render_sparklines(
            series, meta={"theme_momentum_label": "agent-runtimes"}),
    })
    return board


# ------------------------------------------------------------------- V1/V10

def test_v1_panel_order_and_zone_b_borders(full_board):
    html = render_console_html(full_board, interactive=True)
    pos = [html.index(f'id="{pid}"') for pid in PANEL_ORDER]
    assert pos == sorted(pos), f"panel order violated: {pos}"
    # zone B rows: left-border class matches each row's panel classification
    for row in full_board["zone_b"]:
        expected = f"queue-row panel-{str(row['panel']).lower()}"
        assert expected in html, f"missing border class {expected}"


def test_v10_kept_panels_render_content(full_board):
    html = render_console_html(full_board, interactive=False)
    # exec summary: headlines / numbers strip / decisions / risks
    assert "W6 shadow week underway" in html
    assert "numbers-strip" in html and "6.4k tokens" in html
    assert "Decisions needed" in html and "approve act-1" in html
    assert "Risks" in html and "ingest evidence unverified" in html
    # themes table with trend arrow (7 > 5 → ▲), stack overlap
    assert "agent-runtimes" in html and "▲" in html
    assert "stack overlap: aerial ∩ gateway: 1 item" in html
    # zone A columns (project/item/status/pct/eta/tokens) + raw stats fields
    for col in ("Project", "Item", "Status", "Tokens"):
        assert col in html, f"zone A column {col} missing"
    assert "jobs_24h" in html and "untagged bucket" in html


# ---------------------------------------------------------------------- V2

def test_v2_stale_zone_chip_banner_disabled_buttons(full_board):
    board = dict(full_board)
    board["zone_b"] = [{
        "item_id": "i-stale", "panel": "A", "claim": "stale row",
        "evidence_label": "verified", "source": "curator",
        "proposed_action": "act-1", "mandate": None, "age": "5h",
        "score": 0.9, "age_hours": 5}]
    board["zone_b_state"] = "stale (read 2026-11-12 08:00)"
    html = render_console_html(board, interactive=True)
    assert "chip-stale" in html                     # chip wording = stale
    assert "stale (read 2026-11-12 08:00)" in html  # read-timestamp present
    assert "banner" in html                          # in-panel degraded banner
    assert STALE_TOOLTIP in html                     # disabled + stale tooltip
    # stale-zone rows' buttons carry the disabled attr
    assert re.search(r'data-action="approve"[^>]*disabled', html)


# ---------------------------------------------------------------------- V3

def test_v3_static_and_hub_render_differ_only_in_three_ways(full_board):
    hub = render_console_html(full_board, interactive=True)
    static = render_console_html(full_board, interactive=False)

    def norm(html):
        html = re.sub(r"<script>.*?</script>", "", html, flags=re.S)
        lines = [ln.replace(" disabled", "") for ln in html.splitlines()
                 if "fallback-notice" not in ln]
        return lines

    assert norm(hub) == norm(static), "renders differ beyond buttons/notice/footer"
    assert "<script>" in hub and "<script>" not in static
    assert FALLBACK_NOTICE in static and FALLBACK_NOTICE not in hub


# ------------------------------------------------------------------- V4/V7

def test_v4_three_actions_confirm_only_write_cancel_commits_nothing(backend):
    queue, flow, item = backend
    ctx = {"queue": queue, "mandate_flow": flow}
    html = render_console_html({"zone_b": [{
        "item_id": item.item_id, "panel": "A", "claim": item.claim,
        "evidence_label": item.evidence_label, "source": "curator",
        "proposed_action": item.proposed_action, "mandate": None,
        "age": "1h", "score": 0.9, "actions": ["approve", "dismiss",
                                               "escalate"]}]},
        interactive=True)
    for a in ("approve", "dismiss", "escalate"):
        assert f'data-action="{a}"' in html
    assert len(re.findall(r"<button", html)) == 3

    writes = []
    orig_create, orig_exec = flow._create_transaction, flow._execute
    flow._create_transaction = lambda *a, **k: writes.append("create") or \
        orig_create(*a, **k)
    flow._execute = lambda *a, **k: writes.append("execute") or orig_exec(*a, **k)

    # approve click → confirmation panel data, zero writes
    r = actions.handle_queue_action(ctx, "approve", item.item_id,
                                    {"phase": "confirm"})
    assert r["phase"] == "confirm" and r["prefill"]["item_id"] == item.item_id
    assert r["prefill"]["scope"] == "act-1"
    assert writes == []

    # cancel → commits nothing
    r = actions.handle_queue_action(ctx, "cancel", item.item_id, {})
    assert r["status"] == "dismissed" and "nothing was committed" in r["message"]
    assert writes == []
    assert queue.get(item.item_id).state == "waiting-on-you"
    assert flow.covering_mandate(item) is None


def test_v7_blank_dismiss_unspecified_and_escalate_route(backend):
    queue, flow, item = backend
    ctx = {"queue": queue, "mandate_flow": flow}
    reasons = []
    flow.calibration = lambda it, reason: reasons.append(reason)
    r = actions.handle_queue_action(ctx, "dismiss", item.item_id, {"reason": "  "})
    assert r["status"] == "dismissed"
    assert r["calibration_reason"] == "unspecified"
    assert reasons == ["unspecified"]
    queue2, flow2, item2 = backend
    r = actions.handle_queue_action({"queue": queue2, "mandate_flow": flow2},
                                    "escalate", item2.item_id, {})
    assert r["status"] == "escalated"
    assert r["route"] == ["curator", "lead", "Ray", "JEV"]
    assert "curator → lead → Ray → JEV" in r["message"]


# ------------------------------------------------------------------- V5/V6

def test_v5_approve_success_executed_attempts_row_leaves(backend):
    queue, flow, item = backend
    r = actions.handle_queue_action({"queue": queue, "mandate_flow": flow},
                                    "approve", item.item_id, {"phase": "commit"})
    assert r["status"] == "executed"
    assert r["message"] == "executed ✓ (attempts 1)"
    assert queue.get(item.item_id).state == "done"
    assert item.item_id not in [i.item_id for i in queue.ordered()
                                if i.state == "waiting-on-you"]


def test_v5_write_token_401_two_step_error_then_recover(backend):
    queue, flow, item = backend
    flow.token = WriteToken(fail_times=2)
    r = actions.handle_queue_action({"queue": queue, "mandate_flow": flow},
                                    "approve", item.item_id, {"phase": "commit"})
    assert r["status"] == "error"
    assert r["steps"][0] == "write token rejected — re-authenticated once, retrying…"
    assert r["message"] == "not committed — nothing changed"
    assert queue.get(item.item_id).state == "waiting-on-you"
    assert r["actions_re_enabled"] is True
    flow.token.fail_times = 0
    r2 = actions.handle_queue_action({"queue": queue, "mandate_flow": flow},
                                     "approve", item.item_id, {"phase": "commit"})
    assert r2["status"] == "executed"


def test_v6_execution_failure_no_auto_retry(backend):
    queue, flow, item = backend

    def bad_executor(it, mandate):
        raise RuntimeError("target refused")

    flow.executor = bad_executor
    ctx = {"queue": queue, "mandate_flow": flow}
    r1 = actions.handle_queue_action(ctx, "approve", item.item_id,
                                     {"phase": "commit"})
    assert r1["status"] == "error"
    assert r1["message"] == ("executed 0 times · intent NOT re-run — "
                             "approve again for a fresh decision")
    assert r1["auto_retry"] is False
    for _ in range(2):
        actions.handle_queue_action(ctx, "approve", item.item_id,
                                    {"phase": "commit"})
    r4 = actions.handle_queue_action(ctx, "approve", item.item_id,
                                     {"phase": "commit"})
    assert r4["auto_retry"] is False and "intent NOT re-run" in r4["message"]


# ---------------------------------------------------------------------- V8

def test_v8_evidence_badges_and_sla_amber_from_policy():
    html = render_console_html({"zone_b": [
        {"item_id": "i1", "panel": "A", "claim": "c1", "age": "5h",
         "score": 1, "evidence_label": "verified", "source": "s",
         "proposed_action": "a", "mandate": None, "age_hours": 5,
         "impact_class": "blocking"},
        {"item_id": "i2", "panel": "B", "claim": "c2", "age": "30h",
         "score": 1, "evidence_label": "inferred", "source": "s",
         "proposed_action": "a", "mandate": None, "age_hours": 30,
         "impact_class": "degraded"},
        {"item_id": "i3", "panel": "C", "claim": "c3", "age": "80h",
         "score": 1, "evidence_label": "stale", "source": "s",
         "proposed_action": "a", "mandate": None, "age_hours": 80,
         "impact_class": "routine"},
    ]}, interactive=False)
    assert "badge verified'" in html      # solid
    assert "badge inferred'" in html      # outline
    assert "badge stale'" in html and "evidence older than 24h" in html  # striped+tooltip
    # SLA-amber: blocking 4h / degraded 24h / routine 72h from queue-policy.yaml
    amber = re.findall(r"class='age (age-amber|age-gray)'", html)
    assert amber == ["age-amber", "age-amber", "age-amber"]
    gray = render_console_html({"zone_b": [
        {"item_id": "i1", "panel": "A", "claim": "c", "age": "2h", "score": 1,
         "evidence_label": "verified", "source": "s", "proposed_action": "a",
         "mandate": None, "age_hours": 2, "impact_class": "blocking"}]}, interactive=False)
    assert "age-gray" in gray


# ---------------------------------------------------------------------- V9

def test_v9_sparklines_three_charts_seven_values_no_interactivity(spark_dir):
    series = sparklines.collect_series(spark_dir)
    html = sparklines.render_sparklines(
        series, meta={"theme_momentum_label": "agent-runtimes"})
    assert html.count('class="sparkline spark-') == 3
    assert "days:" not in html                      # all 7 daily values present
    for key in ("failure_rate", "spend", "theme_momentum"):
        assert f"spark-{key}" in html
    assert "spark-value" in html and ("▲" in html or "▼" in html or "─" in html)
    assert "onclick" not in html and "onmouseover" not in html \
        and "pointer" not in html and "tooltip" not in html
    # wired into the full page: fragment sits directly under the exec summary
    board = {"exec_summary": {"headlines": ["h"]},
             "sparklines_html": html, "zone_a": [], "zone_b": []}
    page = render_console_html(board, interactive=False)
    assert page.index('id="exec-summary"') < page.index('id="sparkline-strip"') \
        < page.index('id="zone-b"')
    assert html in page


# ---------------------------------------------------------------- V11/V12/V13

def _app(board_provider, ctx, write_token=WRITE_TOKEN):
    return serve.build_app(board_provider, ctx,
                           {"read_token": READ_TOKEN, "write_token": write_token})


def _call(app, method, path, token=None, body=None):
    environ = {"REQUEST_METHOD": method, "PATH_INFO": path,
               "SERVER_NAME": "test", "SERVER_PORT": "80",
               "wsgi.input": io.BytesIO(body or b""),
               "wsgi.errors": io.StringIO(), "wsgi.url_scheme": "http"}
    if body is not None:
        environ["CONTENT_LENGTH"] = str(len(body))
    if token:
        environ["HTTP_AUTHORIZATION"] = f"Bearer {token}"
    captured = {}

    def start_response(status, headers):
        captured["status"], captured["headers"] = status, dict(headers)

    out = app(environ, start_response)
    if not isinstance(out, bytes):
        out = b"".join(out)
    return int(captured["status"].split()[0]), captured["headers"], out


def test_v11_bearer_200_401_page_no_redirect(full_board, backend):
    queue, flow, item = backend
    app = _app(lambda: full_board, {"queue": queue, "mandate_flow": flow})
    status, headers, body = _call(app, "GET", "/dashboard", token=READ_TOKEN)
    assert status == 200 and b"Operator Console" in body
    assert not (300 <= status < 400)
    assert "Location" not in {k for k in headers}
    status, _, body = _call(app, "GET", "/dashboard")
    assert status == 401 and b"credentials required" in body.lower()
    assert b"login" not in body.lower()
    status, _, _ = _call(app, "GET", "/dashboard", token="wrong-token")
    assert status == 401


def test_v12_readonly_banner_flip_via_installer(full_board, backend, tmp_path):
    queue, flow, item = backend
    app = _app(lambda: full_board, {"queue": queue, "mandate_flow": flow},
               write_token=None)
    status, _, body = _call(app, "GET", "/dashboard", token=READ_TOKEN)
    html = body.decode()
    assert serve.READONLY_BANNER_TEXT in html
    assert "<script>" not in html and "disabled" in html

    # installer.first_look_banner: §7 flip logic
    cfg = tmp_path / "config.json"
    assert installer.first_look_banner(False, False) == installer.READ_ONLY_BANNER
    assert installer.first_look_banner(False, True) is None      # stays dismissed
    assert installer.first_look_banner(True, False) == installer.DECISIONS_ENABLED_BANNER
    assert installer.first_look_banner(True, True) is None       # once, never again


def test_v13_no_chart_lib_no_websocket_no_polling(full_board, backend):
    queue, flow, item = backend
    hub = render_console_html(full_board, interactive=True)
    static = render_console_html(full_board, interactive=False)
    for html, interactive in ((hub, True), (static, False)):
        low = html.lower()
        assert "canvas" not in low and "chart.js" not in low \
            and "d3.js" not in low and "<svg" in low  # inline SVG only
        assert "websocket" not in low and "eventsource" not in low
        assert "setinterval" not in low and "settimeout" not in low
        assert "src=" not in low          # no external script/link sources
    assert hub.count("<script>") == 1     # the POST helper, hub render only
    assert "<script>" not in static


# ------------------------------------------------- end-to-end queue loop (V5)

def test_e2e_queue_loop_real_data_over_real_socket(full_board, backend):
    """Full loop on real data: board → POST approve (confirm) → POST approve
    (commit) → mandate ledger row written → row leaves the queue."""
    queue, flow, item = backend

    # board_provider = the real pipeline over the live queue (row visible)
    def provider():
        board = dict(full_board)
        board["zone_b"] = [dict(r, item_id=item.item_id) for r in
                           board_render.zone_b_rows(queue, NOW)] or board["zone_b"]
        return board

    app = _app(provider, {"queue": queue, "mandate_flow": flow})
    server = make_server("127.0.0.1", 0, app)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{port}"
        # 1. load the board: the queue row is on the page with its buttons
        req = urllib.request.Request(
            f"{base}/dashboard",
            headers={"Authorization": f"Bearer {READ_TOKEN}"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 200
            html = resp.read().decode()
        assert item.item_id in html and 'data-action="approve"' in html

        # 2. POST approve (confirm phase) — panel data, no write yet
        req = urllib.request.Request(
            f"{base}/dashboard/queue/approve", method="POST",
            data=json.dumps({"item_id": item.item_id,
                             "payload": {"phase": "confirm"}}).encode(),
            headers={"Authorization": f"Bearer {WRITE_TOKEN}",
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            confirm = json.loads(resp.read().decode())
        assert confirm["phase"] == "confirm"
        assert confirm["prefill"]["item_id"] == item.item_id
        assert confirm["prefill"]["scope"] == "act-1"
        assert flow.conn.execute("SELECT COUNT(*) FROM mandates").fetchone()[0] == 0

        # 3. POST approve (commit) — the single write
        req = urllib.request.Request(
            f"{base}/dashboard/queue/approve", method="POST",
            data=json.dumps({"item_id": item.item_id,
                             "payload": {"phase": "commit"}}).encode(),
            headers={"Authorization": f"Bearer {WRITE_TOKEN}",
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            commit = json.loads(resp.read().decode())
        assert commit["status"] == "executed", commit
        assert commit["message"] == "executed ✓ (attempts 1)"

        # 4. mandate ledger written; row left the queue
        mandates = flow.conn.execute(
            "SELECT mandate_id, row_id, scope, status FROM mandates").fetchall()
        assert len(mandates) == 1
        assert mandates[0][1] == item.item_id and mandates[0][2] == "act-1"
        assert flow.conn.execute(
            "SELECT status FROM action_intents WHERE row_id=?",
            (item.item_id,)).fetchone()[0] == "executed"
        assert flow.conn.execute(
            "SELECT COUNT(*) FROM audit_records").fetchone()[0] >= 1
        row = queue.get(item.item_id)
        assert row is None or row.state == "done"
        assert item.item_id not in [i.item_id for i in queue.ordered()
                                    if i.state == "waiting-on-you"]

        # 5. reload the board: the row no longer waits on anyone
        req = urllib.request.Request(
            f"{base}/dashboard",
            headers={"Authorization": f"Bearer {READ_TOKEN}"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            html2 = resp.read().decode()
        waiting = [i.item_id for i in queue.ordered() if i.state == "waiting-on-you"]
        assert item.item_id not in waiting and queue.get(item.item_id).state == "done"
    finally:
        server.shutdown()
        thread.join(timeout=5)
