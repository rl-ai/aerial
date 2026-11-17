"""S4 (buglist 2026-11-14): DELETE /tickets/{ticket_id}.

Covers the auth contract (401 unauthenticated / 200 with a WRITE-class
internal bearer / 404 missing-or-already-deleted) and the tombstone
behaviour (ticket gone from GET/list afterwards, edges cleaned up).
Auth-on cases run against the real Store + AccessStore like
test_api_access.py; auth-off cases run against FakeStore.
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)

import pytest
from fastapi.testclient import TestClient

from api import create_app
from fake_store import FakeStore
from hub_gateway import StoreGateway
from ticket_hub.access import AccessStore
from ticket_hub.model import Actor
from ticket_hub.store import Store


# ---------------------------------------------------------------------------
# auth ON: real store + access layer
# ---------------------------------------------------------------------------

@pytest.fixture()
def env(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "hub.db"))
    gateway = StoreGateway(store)
    access = AccessStore(store._conn)
    access.create_actor("own", "Own", kind="human", is_owner=True, by="bootstrap")
    access.create_actor("ro", "RO", kind="agent", by="own")
    tokens = {"own": access.issue_credential("own", by="own")[1],
              "ro": access.issue_credential("ro", by="own")[1]}
    # the S1 scoping gate: mutations require a write-class token
    monkeypatch.setenv("TICKET_HUB_WRITE_TOKENS", tokens["own"])
    own = Actor(id="own", name="Own", kind="human")
    ticket = store.create_ticket("Attack-created junk", owner=own)
    child = store.create_ticket("Child", parent=ticket.id, owner=own)
    store._conn.commit()  # handlers run on their own thread connection
    app = create_app(gateway, auth=True)
    app.state.access = access
    client = TestClient(app)

    class Env:
        pass
    e = Env()
    e.store, e.gateway, e.access = store, gateway, access
    e.client, e.tokens = client, tokens
    e.ticket, e.child = ticket, child
    e.h = lambda a: {"Authorization": "Bearer " + tokens[a]}
    return e


def test_delete_unauthenticated_401(env):
    r = env.client.delete(f"/tickets/{env.ticket.id}")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthorized"


def test_delete_with_bearer_200_and_ticket_gone(env):
    r = env.client.delete(f"/tickets/{env.ticket.id}", headers=env.h("own"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == env.ticket.id and body["deleted"] is True
    # ticket disappears from every read path
    assert env.client.get(f"/tickets/{env.ticket.id}",
                          headers=env.h("own")).status_code == 404
    ids = {t["id"] for t in env.client.get("/tickets",
                                           headers=env.h("own")).json()}
    assert env.ticket.id not in ids
    assert env.child.id in ids  # unrelated tickets untouched
    # edges to the deleted ticket are gone
    lin = env.client.get(f"/tickets/{env.child.id}/lineage",
                         headers=env.h("own")).json()
    assert lin["ancestry"] == []


def test_delete_read_only_token_403(env):
    r = env.client.delete(f"/tickets/{env.ticket.id}", headers=env.h("ro"))
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "read_only_token"


def test_delete_missing_404(env):
    r = env.client.delete("/tickets/TK-NOPE", headers=env.h("own"))
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


def test_delete_twice_404(env):
    env.client.delete(f"/tickets/{env.ticket.id}", headers=env.h("own"))
    r = env.client.delete(f"/tickets/{env.ticket.id}", headers=env.h("own"))
    assert r.status_code == 404


def test_delete_events_preserved_append_only(env):
    # SECURITY.md: stored events must never be deleted; the tombstone keeps
    # the ticket's event history intact in the DB.
    env.client.delete(f"/tickets/{env.ticket.id}", headers=env.h("own"))
    rows = env.store._conn.execute(
        "SELECT kind FROM events WHERE ticket_id = ?",
        (env.ticket.id,)).fetchall()
    kinds = [r["kind"] for r in rows]
    assert "created" in kinds and "ticket_deleted" in kinds


# ---------------------------------------------------------------------------
# auth OFF: FakeStore contract (legacy suite shape)
# ---------------------------------------------------------------------------

@pytest.fixture()
def client():
    app = create_app(store=FakeStore())
    return TestClient(app)


def _create(client, title):
    r = client.post("/tickets", json={"title": title})
    assert r.status_code == 201
    return r.json()


def test_delete_auth_off_200_then_404(client):
    t = _create(client, "Temp")
    r = client.delete(f"/tickets/{t['id']}")
    assert r.status_code == 200, r.text
    assert r.json()["deleted"] is True
    assert client.get(f"/tickets/{t['id']}").status_code == 404
    assert t["id"] not in {x["id"] for x in client.get("/tickets").json()}


def test_delete_auth_off_missing_404(client):
    r = client.delete("/tickets/TK-NOPE")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"
