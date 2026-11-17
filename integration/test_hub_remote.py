"""Tests for the remote Tickets client (lane hub-remote).

All HTTP runs against a fake hub bound to 127.0.0.1 port 0 — no real hub
is ever contacted. The bearer value lives only in a temp file during a
test and is read by the transport's injected provider at call time.
"""

from __future__ import annotations

import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

# qualify_real needs packages/rlai-* (the products live in the private
# monorepo; the public tree ships without them). Skip rather than error.
from pathlib import Path as _Path
if not (_Path(__file__).resolve().parents[1] / "packages").is_dir():
    pytest.skip("needs packages/rlai-* (not shipped in the public tree)",
                allow_module_level=True)

import hub_remote
from hub_remote import HubRemoteError, HubTicketsRemote
import qualify_real


TOKEN_FILE = "/home/you/.aerial/tokens/x.token"  # a plain path, never a value


class FakeHubHandler(BaseHTTPRequestHandler):
    """The hub API surface the five Store methods map onto."""

    store_lock = threading.Lock()

    def log_message(self, *args):  # keep test output clean
        pass

    # -- plumbing ----------------------------------------------------------

    def _send(self, status: int, obj) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("content-length", 0))
        return json.loads(self.rfile.read(length) or b"{}")

    @classmethod
    def reset(cls, server_state):
        cls.state = server_state

    def _authed(self) -> bool:
        expected = self.state["token_value"]
        return self.headers.get("authorization") == "Bearer " + expected

    # -- routes ------------------------------------------------------------

    def do_POST(self):
        parts = [urllib.parse.unquote(p) for p in self.path.split("/")
                 if p]
        with self.store_lock:
            if not self._authed():
                self._send(403, {"error": "forbidden " + self.state["token_value"]})
                return
            if parts == ["tickets"]:
                data = self._body()
                tid = f"tkt-{len(self.state['tickets']) + 1}"
                ticket = {"ticket_id": tid, "title": data["title"],
                          "body": data["body"], "status": "draft",
                          "priority": data["priority"],
                          "tags": list(data["tags"]),
                          "created_by": data["actor"],
                          "parent": data.get("parent")}
                self.state["tickets"][tid] = ticket
                self.state["requests"].append(
                    ("POST", "/tickets", dict(data)))
                self._send(201, ticket)
                return
            if len(parts) == 3 and parts[0] == "tickets" and parts[2] == "events":
                tid = parts[1]
                if tid not in self.state["tickets"]:
                    self._send(404, {"error": "no such ticket"})
                    return
                data = self._body()
                events = self.state["events"].setdefault(tid, [])
                event = {"event_id": f"evt-{tid}-{len(events) + 1}",
                         "ticket_id": tid, "seq": len(events) + 1,
                         "kind": data["kind"], "payload": data["payload"],
                         "actor": data["actor"], "ts": "2026-11-10T00:00:00Z"}
                events.append(event)
                self.state["requests"].append(
                    ("POST", f"/tickets/{tid}/events", dict(data)))
                self._send(201, event)
                return
            if parts == ["records"]:
                data = self._body()
                mid = data.get("message_id")
                duplicate = mid in self.state["record_ids"]
                self.state["record_ids"].add(mid)
                events = self.state["events"].setdefault(data["ticket_id"], [])
                event = {"event_id": f"evt-rec-{len(events) + 1}",
                         "ticket_id": data["ticket_id"],
                         "seq": len(events) + 1,
                         "kind": "checkpoint",
                         "payload": {"record": data},
                         "actor": data.get("employee_id", ""),
                         "ts": "2026-11-10T00:00:00Z",
                         "duplicate": duplicate}
                events.append(event)
                self.state["requests"].append(("POST", "/records", dict(data)))
                self._send(200, event)
                return
            self._send(404, {"error": "not found"})

    def do_GET(self):
        parts = [urllib.parse.unquote(p) for p in self.path.split("/")
                 if p]
        with self.store_lock:
            if not self._authed():
                self._send(403, {"error": "forbidden " + self.state["token_value"]})
                return
            if len(parts) == 2 and parts[0] == "tickets":
                tid = parts[1]
                ticket = self.state["tickets"].get(tid)
                self.state["requests"].append(("GET", self.path, None))
                if ticket is None:
                    self._send(404, {"error": "no such ticket"})
                else:
                    self._send(200, ticket)
                return
            if len(parts) == 3 and parts[0] == "tickets" and parts[2] == "events":
                tid = parts[1]
                self.state["requests"].append(
                    ("GET", self.path, None))
                self._send(200, {"events": self.state["events"].get(tid, [])})
                return
            self._send(404, {"error": "not found"})


@pytest.fixture()
def hub_env(tmp_path):
    """A fake hub on 127.0.0.1:0 plus a Hub wired to it."""
    token_file = tmp_path / "hub.token"
    token_value = "bearer-" + str(tmp_path)[:8]
    token_file.write_text(token_value)
    state = {"tickets": {}, "events": {}, "record_ids": set(),
             "requests": [], "token_value": token_value}
    FakeHubHandler.reset(state)
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeHubHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        from qualify_real import HTTPGetter, HTTPPoster, Hub

        hub = Hub(
            f"http://127.0.0.1:{server.server_port}",
            get=HTTPGetter().get, post=HTTPPoster().post,
            token_file=TOKEN_FILE, token_provider=lambda path: open(
                token_file).read())
        yield {"hub": hub, "state": state, "token_value": token_value,
               "token_file": token_file}
    finally:
        server.shutdown()
        server.server_close()


def make_client(env):
    return HubTicketsRemote(env["hub"])


# -- 1 ---------------------------------------------------------------------

def test_create_ticket_path_and_body(hub_env):
    env = hub_env
    client = make_client(env)
    ticket = client.create_ticket(
        "a5 job", body="the body", tags=("a5-qualification", "x"),
        parent="tkt-parent", actor="emp@a5")
    assert ticket.ticket_id.startswith("tkt-")
    assert ticket.title == "a5 job"
    assert ticket.body == "the body"
    assert ticket.tags == ("a5-qualification", "x")
    method, path, body = env["state"]["requests"][0]
    assert (method, path) == ("POST", "/tickets")
    assert body["title"] == "a5 job"
    assert body["body"] == "the body"
    assert body["parent"] == "tkt-parent"
    assert body["tags"] == ["a5-qualification", "x"]
    assert body["actor"] == "emp@a5"


# -- 2 ---------------------------------------------------------------------

def test_get(hub_env):
    env = hub_env
    client = make_client(env)
    created = client.create_ticket("one", tags=("a5-qualification",),
                                   actor="emp@a5")
    fetched = client.get(created.ticket_id)
    assert fetched.ticket_id == created.ticket_id
    assert fetched.title == "one"
    assert fetched.tags == ("a5-qualification",)
    assert client.get("tkt-missing") is None


# -- 3 ---------------------------------------------------------------------

def test_append_event_with_actor(hub_env):
    env = hub_env
    client = make_client(env)
    created = client.create_ticket("two", actor="emp@a5")
    event = client.append_event(created.ticket_id, "comment",
                                {"note": "hello"}, actor="emp@a5")
    assert event.event_id.startswith("evt-")
    assert event.kind == "comment"
    assert event.payload == {"note": "hello"}
    assert event.actor == "emp@a5"
    assert event.ts == "2026-11-10T00:00:00Z"
    method, path, body = env["state"]["requests"][-1]
    assert (method, path) == ("POST", f"/tickets/{created.ticket_id}/events")
    assert body["kind"] == "comment"
    assert body["actor"] == "emp@a5"


# -- 4 ---------------------------------------------------------------------

def test_events(hub_env):
    client = make_client(hub_env)
    created = client.create_ticket("three", actor="emp@a5")
    first = client.append_event(created.ticket_id, "checkpoint",
                                {"i": 1}, actor="emp@a5")
    second = client.append_event(created.ticket_id, "comment",
                                 {"i": 2}, actor="emp@a5")
    events = client.events(created.ticket_id)
    assert [e.event_id for e in events] == [first.event_id, second.event_id]
    assert [e.seq for e in events] == [1, 2]


# -- 5 ---------------------------------------------------------------------

def test_record_then_duplicate(hub_env):
    env = hub_env
    client = make_client(env)
    client.create_ticket("four", actor="emp@a5")
    c2 = {"message_id": "msg-1", "ticket_id": "tkt-1", "run_id": "r1",
          "producer": "p", "seq": 1, "event_type": "run_started",
          "employee_id": "emp@a5"}
    first = client.record(dict(c2))
    assert first.duplicate is False
    again = client.record(dict(c2))
    assert again.duplicate is True
    method, path, _ = env["state"]["requests"][-1]
    assert (method, path) == ("POST", "/records")


# -- 6 ---------------------------------------------------------------------

def test_quoting(hub_env):
    env = hub_env
    client = make_client(env)
    weird = "tk t/1?&#x"
    client.get(weird)
    client.events(weird)
    quoted = urllib.parse.quote(weird, safe="")
    paths = [req[1] for req in env["state"]["requests"]]
    assert f"/tickets/{quoted}" in paths
    assert f"/tickets/{quoted}/events" in paths
    # the raw, unquoted id never reaches the wire as a separate segment
    assert not any("/tickets/tk t/" in p for p in paths)


# -- 7 ---------------------------------------------------------------------

def test_403_raises_without_body(hub_env):
    env = hub_env
    from qualify_real import Hub

    bad_hub = Hub(env["hub"].base_url, get=env["hub"]._get,
                  post=env["hub"]._post, token_file=TOKEN_FILE,
                  token_provider=lambda path: "wrong-value")
    client = HubTicketsRemote(bad_hub)
    with pytest.raises(HubRemoteError) as excinfo:
        client.get("tkt-1")
    message = str(excinfo.value)
    assert "get" in message.lower() and "403" in message
    assert "forbidden" not in message  # the body stays out of the error
    with pytest.raises(HubRemoteError):
        client.append_event("tkt-1", "comment", {}, actor="emp@a5")
    with pytest.raises(HubRemoteError):
        client.create_ticket("x", actor="emp@a5")
    with pytest.raises(HubRemoteError):
        client.record({"message_id": "m", "ticket_id": "tkt-1"})


# -- 8 ---------------------------------------------------------------------

def test_no_token_value_in_errors_or_attributes(hub_env):
    env = hub_env
    client = make_client(env)
    from qualify_real import Hub

    bad_hub = Hub(env["hub"].base_url, get=env["hub"]._get,
                  post=env["hub"]._post, token_file=TOKEN_FILE,
                  token_provider=lambda path: env["token_value"] + "x")
    bad_client = HubTicketsRemote(bad_hub)
    with pytest.raises(HubRemoteError) as excinfo:
        bad_client.get("tkt-1")
    token = env["token_value"]
    # nothing the client keeps or raises ever carries the token value
    assert token not in str(excinfo.value)
    assert token not in repr(bad_client)
    assert token not in repr(bad_client.__dict__)
    assert token not in repr(client)
    assert token not in repr(client.__dict__)


# -- 9/10 ------------------------------------------------------------------

class _FakeServer:
    url = "http://127.0.0.1:1"
    bearer_file = None


class _FakeResources:
    _slots = {"worker": None}

    def __init__(self, doc=None):
        self.doc = doc

    def servers(self):
        return {"worker": _FakeServer()}

    def slot(self, name):
        class _S:
            resource = type("R", (), {"name": name})()
        return _S()

    def widths(self):
        return {}


def _patch_basics(monkeypatch):
    monkeypatch.setattr(qualify_real, "load_resources_doc", lambda path: {})
    monkeypatch.setattr(qualify_real, "Resources", _FakeResources)
    monkeypatch.setattr(qualify_real, "Gateway", lambda resources: None)
    monkeypatch.setattr(qualify_real, "default_models", lambda resources: None)
    monkeypatch.setattr(qualify_real, "default_hermes", lambda *a, **k: None)
    monkeypatch.setattr(qualify_real, "_open_run_store",
                        lambda resources, path: None)
    monkeypatch.setattr(qualify_real, "_open_roles_conn",
                        lambda path: None)


def _args(remote_tickets, tickets_db=None):
    import argparse

    return argparse.Namespace(
        resources="res.json", hub="http://127.0.0.1:1",
        hub_token_file=TOKEN_FILE, live=True, tickets_db=tickets_db,
        runtime_db="rt.db", roles_db="roles.db", usage_ledger="u.jsonl",
        epic="epic-1", remote_tickets=remote_tickets)


def test_remote_tickets_selects_the_remote(monkeypatch):
    _patch_basics(monkeypatch)
    opened = []
    monkeypatch.setattr(qualify_real.Store, "open",
                        classmethod(lambda cls, path: opened.append(path)))
    made = []
    real_remote = hub_remote.HubTicketsRemote

    class RecordingRemote(real_remote):
        def __init__(self, hub):
            super().__init__(hub)
            made.append(hub)

    monkeypatch.setattr(qualify_real, "HubTicketsRemote", RecordingRemote)
    dep = qualify_real._build_live_deployment(_args(remote_tickets=True))
    assert opened == []  # no local store is opened
    assert len(made) == 1
    assert isinstance(dep.tickets_hub, RecordingRemote)
    assert dep.tickets_hub._hub is made[0]
    assert made[0].base_url == "http://127.0.0.1:1"


def test_without_flag_local_store_is_used(monkeypatch):
    _patch_basics(monkeypatch)
    opened = []
    fake_store = object()
    monkeypatch.setattr(qualify_real.Store, "open",
                        classmethod(
                            lambda cls, path: opened.append(path) or fake_store))
    made = []

    class RecordingRemote(hub_remote.HubTicketsRemote):
        def __init__(self, hub):
            super().__init__(hub)
            made.append(hub)

    monkeypatch.setattr(qualify_real, "HubTicketsRemote", RecordingRemote)
    dep = qualify_real._build_live_deployment(
        _args(remote_tickets=False, tickets_db="db/tickets.sqlite"))
    assert opened == ["db/tickets.sqlite"]
    assert made == []
    assert dep.tickets_hub is fake_store



def test_real_hub_shapes_id_fields_and_a_plain_event_list():
    """Claude review, 2026-11-10, checked against the live hub: tickets and
    events carry `id`, and GET /tickets/{id}/events returns a plain list."""
    import hub_remote as hr

    class FakeHub:
        def get(self, path, *, auth=True):
            if path.endswith("/events"):
                return 200, '[{"id": "EV-1", "kind": "comment", "seq": 1, "payload": {}, "actor": "a", "ts": "t"}]'
            return 200, '{"id": "TK-1", "title": "x", "status": "open", "tags": ["a5-qualification"]}'

        def post(self, path, body):
            return 201, "{}"
    r = hr.HubTicketsRemote(FakeHub())
    assert r.get("TK-1").ticket_id == "TK-1"
    events = r.events("TK-1")
    assert [e.event_id for e in events] == ["EV-1"] and events[0].kind == "comment"
