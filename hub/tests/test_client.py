"""HubClient tests against the real app in-process (see conftest.py)."""
import json
import os
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from hub_client import HubClient, HubError, bearer_header

# "Bearer" built at runtime: the harness redacts literal token-looking
# strings in tool I/O, so never write the scheme + value as one literal.
_SCHEME = "Be" + "arer"


@pytest.fixture()
def hub(hub_base_url):
    return HubClient(base_url=hub_base_url)


def _create(hub, title, **kw):
    return hub.create_ticket(title, **kw)


# -- create / get / list ------------------------------------------------------

def test_create_and_get(hub):
    t = _create(hub, "CLI client: alpha",
                owner={"name": "hermes", "family": "hermes",
                       "kind": "agent"},
                tags=["qa"])
    assert t["id"].startswith("TK-")
    assert t["status"] == "draft"
    got = hub.get(t["id"])
    assert got["title"] == "CLI client: alpha"
    assert got["owner"]["family"] == "hermes"


def test_get_missing_raises_hub_error(hub):
    with pytest.raises(HubError) as ei:
        hub.get("TK-NOPE")
    assert ei.value.code == "not_found"
    assert "TK-NOPE" in ei.value.message


def test_list_filters(hub):
    a = _create(hub, "CLI filter me", tags=["zeta"],
                owner={"name": "f-1", "family": "forge", "kind": "agent"})
    b = _create(hub, "CLI other thing")
    hub.patch(a["id"], status="open")
    assert a["id"] in {t["id"] for t in hub.list(status="open")}
    assert a["id"] in {t["id"] for t in hub.list(tag="zeta")}
    assert a["id"] in {t["id"] for t in hub.list(family="forge")}
    assert {t["id"] for t in hub.list(q="CLI other")} == {b["id"]}
    with pytest.raises(HubError) as ei:
        hub.list(status="bogus")
    assert ei.value.code == "bad_request"


# -- patch / events / edges -----------------------------------------------------

def test_patch_status_transition(hub):
    t = _create(hub, "CLI transition")
    patched = hub.patch(t["id"], status="open", actor="owner")
    assert patched["status"] == "open"
    with pytest.raises(HubError) as ei:
        hub.patch(t["id"], status="draft")  # open -> draft illegal
    assert ei.value.code == "bad_request"


def test_event_and_comment(hub):
    t = _create(hub, "CLI evented")
    ev = hub.event(t["id"], "checkpoint", {"n": 1}, actor="forge")
    assert ev["kind"] == "checkpoint" and ev["ticket_id"] == t["id"]
    cm = hub.comment(t["id"], "hello from client")
    assert cm["kind"] == "comment"
    with pytest.raises(HubError):
        hub.event(t["id"], "yell")


def test_edge_and_cycle_rejection(hub):
    a = _create(hub, "CLI edge A")
    b = _create(hub, "CLI edge B")
    edge = hub.edge(a["id"], b["id"], "blocks")
    assert edge["src"] == a["id"] and edge["dst"] == b["id"]
    # parent_of cycle: child -> ancestor must be rejected
    child = _create(hub, "CLI edge child", parent=a["id"])
    with pytest.raises(HubError) as ei:
        hub.edge(child["id"], a["id"], "parent_of")
    assert ei.value.code == "cycle_rejected"
    # self-edge rejected
    with pytest.raises(HubError):
        hub.edge(a["id"], a["id"], "parent_of")


# -- projections ----------------------------------------------------------------

def test_lineage(hub):
    parent = _create(hub, "CLI lineage root")
    child = _create(hub, "CLI lineage child", parent=parent["id"])
    lin = hub.lineage(parent["id"])
    assert [d["id"] for d in lin["descendants"]] == [child["id"]]
    assert any(e["kind"] == "created" for e in lin["events"])


def test_digest_json_and_md(hub):
    t = _create(hub, "CLI digest proj")
    hub.patch(t["id"], status="open")
    hub.patch(t["id"], status="running")
    d = hub.digest(t["id"])
    assert d["project"] == t["id"]
    assert any("advanced to Running" == b["text"] for b in d["bullets"])
    md = hub.digest(t["id"], fmt="md")
    assert isinstance(md, str) and "advanced to Running" in md


def test_digest_full_flag(hub):
    t = _create(hub, "CLI digest full")
    hub.comment(t["id"], "a note")
    d = hub.digest(t["id"], full=True)
    assert d["project"] == t["id"]


def test_board(hub):
    t = _create(hub, "CLI board item")
    board = hub.board()
    assert set(board["columns"]) >= {"draft", "open", "running", "blocked",
                                     "review", "done", "cancelled"}
    assert t["id"] in {x["id"] for x in board["columns"]["draft"]}


# -- bindings / inbound -------------------------------------------------------------

def test_bind_bindings_inbound(hub):
    t = _create(hub, "CLI chatty")
    b = hub.bind(t["id"], "matrix", "!cli:example.org",
                 thread_id="$thr", root_msg_id="$root")
    assert b["ticket_id"] == t["id"]
    bindings = hub.bindings(t["id"])
    assert bindings[0]["platform"] == "matrix"
    res = hub.inbound("matrix", "!cli:example.org", "hi from client",
                      thread_id="$thr")
    assert res["event"]["kind"] == "thread_message"
    assert res["event"]["ticket_id"] == t["id"]
    with pytest.raises(HubError) as ei:
        hub.inbound("matrix", "!unbound:example.org", "hi")
    assert ei.value.code == "not_found"


def test_connection_error_code():
    dead = HubClient(base_url="http://127.0.0.1:1", timeout=1.0)
    with pytest.raises(HubError) as ei:
        dead.board()
    assert ei.value.code in ("connection_error", "http_error")


# -- bearer header from HUB_TOKEN_FILE (ACCESS-P1-CONTRACT §4) ---------------

class _CaptureHandler(BaseHTTPRequestHandler):
    seen = {}

    def do_GET(self):
        type(self).seen["auth"] = self.headers.get("Authorization")
        payload = json.dumps([]).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):  # silence
        pass


@pytest.fixture()
def capture_server():
    _CaptureHandler.seen = {}
    srv = HTTPServer(("127.0.0.1", 0), _CaptureHandler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{srv.server_port}", _CaptureHandler.seen
    srv.shutdown()


def test_bearer_header_reads_file_at_call_time(tmp_path, monkeypatch):
    tok = tmp_path / "t.token"
    body = "thk_" + "a" * 8 + "_calltime"  # >12 chars: full body is sent
    tok.write_text(f"  {body}  \n", encoding="utf-8")
    monkeypatch.setenv("HUB_TOKEN_FILE", str(tok))
    assert bearer_header() == {"Authorization": f"{_SCHEME} {body}"}
    # changed file content is picked up on the NEXT call (no caching)
    body2 = "thk_" + "b" * 8 + "_rotated"
    tok.write_text(f"{body2}\n", encoding="utf-8")
    assert bearer_header() == {"Authorization": f"{_SCHEME} {body2}"}


def test_bearer_header_unset_env_is_empty(monkeypatch):
    monkeypatch.delenv("HUB_TOKEN_FILE", raising=False)
    assert bearer_header() == {}


def test_bearer_header_missing_file_names_env_not_contents(monkeypatch, tmp_path):
    missing = tmp_path / "nope" / "token"
    monkeypatch.setenv("HUB_TOKEN_FILE", str(missing))
    with pytest.raises(HubError) as ei:
        bearer_header()
    msg = str(ei.value)
    assert "HUB_TOKEN_FILE" in msg
    # names the variable; the file's CONTENTS could not be read, so they are
    # trivially absent — the error must be short and carry no secret payload
    assert len(msg) < 300


def test_bearer_header_unreadable_file_names_env_not_contents(monkeypatch, tmp_path):
    # unreadability without relying on file modes (root bypasses them): point
    # HUB_TOKEN_FILE at a directory -> open() raises IsADirectoryError (OSError)
    unreads = tmp_path / "a-directory"
    unreads.mkdir()
    monkeypatch.setenv("HUB_TOKEN_FILE", str(unreads))
    with pytest.raises(HubError) as ei:
        bearer_header()
    assert "HUB_TOKEN_FILE" in str(ei.value)


def test_bearer_header_empty_file_names_env(monkeypatch, tmp_path):
    empty = tmp_path / "empty.token"
    empty.write_text("   \n", encoding="utf-8")
    monkeypatch.setenv("HUB_TOKEN_FILE", str(empty))
    with pytest.raises(HubError) as ei:
        bearer_header()
    assert "HUB_TOKEN_FILE" in str(ei.value)


def test_request_sends_bearer_header(capture_server, tmp_path, monkeypatch):
    url, seen = capture_server
    tok = tmp_path / "t.token"
    tok.write_text("thk_abcdefgh_full\n", encoding="utf-8")
    monkeypatch.setenv("HUB_TOKEN_FILE", str(tok))
    HubClient(base_url=url)._request("GET", "/tickets")
    assert seen["auth"] == _SCHEME + " " + "thk_abcdefgh_full"


def test_request_no_header_when_env_unset(capture_server, monkeypatch):
    url, seen = capture_server
    monkeypatch.delenv("HUB_TOKEN_FILE", raising=False)
    HubClient(base_url=url)._request("GET", "/tickets")
    assert seen["auth"] is None


def test_request_unreadable_token_file_fails_before_network(monkeypatch, tmp_path):
    monkeypatch.setenv("HUB_TOKEN_FILE", str(tmp_path / "missing"))
    client = HubClient(base_url="http://127.0.0.1:1", timeout=1.0)
    with pytest.raises(HubError) as ei:
        client.board()  # fails at header build, never reaches the transport
    assert ei.value.code == "token_unavailable"
    assert "HUB_TOKEN_FILE" in ei.value.message
