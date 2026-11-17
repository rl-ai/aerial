"""Lane B API tests against the FakeStore contract (runs standalone)."""

import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)

import pytest
from fastapi.testclient import TestClient

from api import create_app
from fake_store import FakeStore


@pytest.fixture()
def client():
    store = FakeStore()
    app = create_app(store=store)
    return TestClient(app)


def _create(client, title, **kw):
    resp = client.post("/tickets", json={"title": title, **kw})
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---- tickets ---------------------------------------------------------------

def test_create_ticket(client):
    t = _create(client, "Parent ticket", owner={"name": "owner", "family": "owner",
                                                "kind": "human"}, tags=["core"])
    assert t["id"].startswith("TK-")
    assert t["status"] == "draft"
    fetched = client.get(f"/tickets/{t['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["title"] == "Parent ticket"


def test_get_missing_404_json_error(client):
    resp = client.get("/tickets/TK-NOPE")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


def test_create_child_via_parent_field(client):
    parent = _create(client, "Parent")
    child = _create(client, "Child", parent=parent["id"])
    lineage = client.get(f"/tickets/{child['id']}/lineage").json()
    assert [a["id"] for a in lineage["ancestry"]] == [parent["id"]]
    lineage_p = client.get(f"/tickets/{parent['id']}/lineage").json()
    assert [d["id"] for d in lineage_p["descendants"]] == [child["id"]]


def test_list_filters(client):
    a = _create(client, "Alpha thing", tags=["ui"],
                owner={"name": "hermes-1", "family": "hermes",
                       "kind": "agent"})
    b = _create(client, "Beta thing")
    client.patch(f"/tickets/{a['id']}", json={"status": "open"})
    assert {t["id"] for t in client.get("/tickets?status=Open").json()} == {a["id"]}
    assert {t["id"] for t in client.get("/tickets?family=hermes").json()} == {a["id"]}
    assert {t["id"] for t in client.get("/tickets?tag=ui").json()} == {a["id"]}
    assert {t["id"] for t in client.get("/tickets?q=Beta").json()} == {b["id"]}
    bad = client.get("/tickets?status=Bogus")
    assert bad.status_code == 400
    assert bad.json()["error"]["code"] == "bad_request"


# ---- patch / events ----------------------------------------------------------

def test_patch_status_emits_event(client):
    t = _create(client, "Patchable")
    resp = client.patch(f"/tickets/{t['id']}",
                        json={"status": "review", "actor": {"name": "glm"}})
    assert resp.status_code == 200
    assert resp.json()["status"] == "review"
    events = client.get(f"/tickets/{t['id']}/lineage").json()["events"]
    sc = [e for e in events if e["kind"] == "status_changed"]
    assert len(sc) == 1
    assert sc[0]["payload"] == {"from": "draft", "to": "review"}


def test_patch_invalid_status_400(client):
    t = _create(client, "X")
    resp = client.patch(f"/tickets/{t['id']}", json={"status": "Frozen"})
    assert resp.status_code == 400
    assert "error" in resp.json()


def test_append_event(client):
    t = _create(client, "Evented")
    resp = client.post(f"/tickets/{t['id']}/events",
                       json={"kind": "artifact",
                             "actor": {"name": "forge", "family": "forge"},
                             "payload": {"name": "round-13 package"}})
    assert resp.status_code == 201
    ev = resp.json()
    assert ev["kind"] == "artifact" and ev["ticket_id"] == t["id"]
    bad = client.post(f"/tickets/{t['id']}/events", json={"kind": "yell"})
    assert bad.status_code == 400


# ---- edges -------------------------------------------------------------------

def test_edge_created(client):
    a = _create(client, "A")
    b = _create(client, "B")
    resp = client.post(f"/tickets/{a['id']}/edges",
                       json={"type": "blocks", "dst": b["id"]})
    assert resp.status_code == 201
    edge = resp.json()
    assert edge["src"] == a["id"] and edge["dst"] == b["id"]
    assert b["id"] in [t["id"] for t in client.get(f"/tickets/{a['id']}/lineage").json()["related"]]


def test_edge_cycle_rejected_400(client):
    parent = _create(client, "P")
    child = _create(client, "C", parent=parent["id"])
    grand = _create(client, "G", parent=child["id"])
    # Closing the loop: grand -> parent as parent_of must be rejected.
    resp = client.post(f"/tickets/{grand['id']}/edges",
                       json={"type": "parent_of", "dst": parent["id"]})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "cycle_rejected"
    # Self-edge too.
    resp2 = client.post(f"/tickets/{parent['id']}/edges",
                        json={"type": "parent_of", "dst": parent["id"]})
    assert resp2.status_code == 400
    # Unknown edge type rejected.
    resp3 = client.post(f"/tickets/{parent['id']}/edges",
                        json={"type": "friend_of", "dst": child["id"]})
    assert resp3.status_code == 400


def test_edge_missing_dst_404(client):
    a = _create(client, "A")
    resp = client.post(f"/tickets/{a['id']}/edges",
                       json={"type": "relates_to", "dst": "TK-GHOST"})
    assert resp.status_code == 404


def test_graph_edge_types_created(client):
    a = _create(client, "A")
    b = _create(client, "B")
    for ttype in ("about", "same-as"):
        resp = client.post(f"/tickets/{a['id']}/edges",
                           json={"type": ttype, "dst": b["id"]})
        assert resp.status_code == 201, resp.text
        assert resp.json()["type"] == ttype
    # Unknown types are still rejected.
    bad = client.post(f"/tickets/{a['id']}/edges",
                      json={"type": "bogus", "dst": b["id"]})
    assert bad.status_code == 400


def test_graph_edge_loop_allowed(client):
    # Graph edge types are not cycle-checked: A about B and B about A both fine.
    a = _create(client, "A")
    b = _create(client, "B")
    r1 = client.post(f"/tickets/{a['id']}/edges",
                     json={"type": "about", "dst": b["id"]})
    r2 = client.post(f"/tickets/{b['id']}/edges",
                     json={"type": "about", "dst": a["id"]})
    assert r1.status_code == 201, r1.text
    assert r2.status_code == 201, r2.text


# ---- digest ------------------------------------------------------------------

def test_digest_structured_bullets_cite_event_ids(client):
    parent = _create(client, "Launch hub")
    child = _create(client, "Subtask", parent=parent["id"])
    client.patch(f"/tickets/{child['id']}", json={"status": "review",
                                                  "actor": {"name": "hermes"}})
    ev_art = client.post(f"/tickets/{child['id']}/events", json={
        "kind": "artifact", "actor": {"name": "forge"},
        "payload": {"name": "snapshot tarball"}}).json()
    ev_appr = client.post(f"/tickets/{child['id']}/events", json={
        "kind": "approval_receipt", "actor": {"name": "owner", "family": "owner"},
        "payload": {"approver": "Owner"}}).json()
    ev_dec = client.post(f"/tickets/{child['id']}/events", json={
        "kind": "decision", "actor": {"name": "owner"},
        "payload": {"text": "ship v0"}}).json()
    ev_known = client.post(f"/tickets/{parent['id']}/events", json={
        "kind": "comment", "actor": {"name": "forge"},
        "payload": {"text": "QUEUED FINDING: flaky replay"}}).json()

    resp = client.get(f"/tickets/{parent['id']}/digest?format=json")
    assert resp.status_code == 200
    digest = resp.json()
    assert digest["project"] == parent["id"]
    texts = [b["text"] for b in digest["bullets"]]
    assert "advanced to Review" in texts
    assert "produced snapshot tarball" in texts
    assert "approved by Owner" in texts
    assert "decision recorded" in texts
    assert "known issue logged" in texts
    # Every bullet cites at least one real event id, and cited ids cover the
    # translated events.
    for b in digest["bullets"]:
        assert len(b["event_ids"]) >= 1
    all_cited = {eid for b in digest["bullets"] for eid in b["event_ids"]}
    assert {ev_art["id"], ev_appr["id"], ev_dec["id"], ev_known["id"]} <= all_cited


def test_digest_from_child_scopes_to_project(client):
    parent = _create(client, "Project root")
    child = _create(client, "Child", parent=parent["id"])
    client.patch(f"/tickets/{child['id']}", json={"status": "running"})
    digest = client.get(f"/tickets/{child['id']}/digest").json()
    assert digest["project"] == parent["id"]
    assert any("advanced to Running" == b["text"] for b in digest["bullets"])


def test_digest_markdown_format(client):
    t = _create(client, "MD project")
    client.patch(f"/tickets/{t['id']}", json={"status": "done"})
    resp = client.get(f"/tickets/{t['id']}/digest?format=md")
    assert resp.status_code == 200
    assert "text/markdown" in resp.headers["content-type"]
    assert "advanced to Done" in resp.text


def test_digest_bad_format_400(client):
    t = _create(client, "X")
    resp = client.get(f"/tickets/{t['id']}/digest?format=xml")
    assert resp.status_code == 400


# ---- board -------------------------------------------------------------------

def test_board_columns(client):
    a = _create(client, "A")
    b = _create(client, "B")
    client.patch(f"/tickets/{b['id']}", json={"status": "open"})
    board = client.get("/board").json()
    assert set(board["columns"]) >= {"draft", "open", "running", "blocked",
                                     "review", "done", "cancelled"}
    assert [t["id"] for t in board["columns"]["draft"]] == [a["id"]]
    assert [t["id"] for t in board["columns"]["open"]] == [b["id"]]


def test_board_parents_map(client):
    a = _create(client, "ParentOfBoard")
    b = _create(client, "ChildOfBoard")
    client.post(f"/tickets/{a['id']}/edges",
                json={"dst": b["id"], "type": "parent_of"})
    board = client.get("/board").json()
    assert board["parents"][b["id"]] == a["id"]
    assert board["child_counts"][a["id"]] == 1


# ---- thread binding / inbound --------------------------------------------------

def test_bind_thread(client):
    t = _create(client, "Chatty")
    resp = client.post(f"/tickets/{t['id']}/bind", json={
        "platform": "matrix", "room_id": "!room:example.org",
        "thread_id": "$root", "root_msg_id": "$root"})
    assert resp.status_code == 201
    assert resp.json()["ticket_id"] == t["id"]
    bindings = client.get(f"/tickets/{t['id']}/bindings").json()
    assert bindings[0]["platform"] == "matrix"
    bad = client.post(f"/tickets/{t['id']}/bind",
                      json={"platform": "myspace", "room_id": "x"})
    assert bad.status_code == 400


def test_inbound_creates_thread_message_event(client):
    t = _create(client, "Chatty")
    client.post(f"/tickets/{t['id']}/bind",
                json={"platform": "discord", "room_id": "chan-1",
                      "thread_id": "thr-1"})
    resp = client.post("/adapters/discord/inbound", json={
        "room_id": "chan-1", "thread_id": "thr-1",
        "actor": {"name": "owner", "family": "owner", "kind": "human"},
        "text": "looks good, ship it"})
    assert resp.status_code == 200
    event = resp.json()["event"]
    assert event["kind"] == "thread_message"
    assert event["ticket_id"] == t["id"]
    assert event["payload"]["text"] == "looks good, ship it"
    # Unbound room -> 404 JSON error.
    miss = client.post("/adapters/discord/inbound",
                       json={"room_id": "nope", "text": "hi"})
    assert miss.status_code == 404
    assert miss.json()["error"]["code"] == "not_found"


# ---- UI ------------------------------------------------------------------------

def test_ui_served(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "ticket-hub" in resp.text.lower()
    for asset in ("/ui/app.js", "/ui/style.css"):
        r = client.get(asset)
        assert r.status_code == 200, asset


def test_ui_mission_control_nav(client):
    """Mission Control shell: nav ids + view sections + new render functions."""
    html = client.get("/").text
    for nav_id in ("nav-wiki", "nav-monitoring", "nav-admin", "nav-roles"):
        assert nav_id in html, nav_id
    for view_id in ("view-wiki", "view-monitoring", "view-admin", "view-roles"):
        assert view_id in html, view_id
    js = client.get("/ui/app.js").text
    for fn in ("renderWiki", "renderMonitoring", "renderAdmin", "renderRoles"):
        assert fn in js, fn


def test_validation_error_is_json(client):
    resp = client.post("/tickets", json={"body": "no title"})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation"

def test_plan_endpoint(client):
    root = client.post("/tickets", json={"title": "Root"}).json()
    a = client.post("/tickets", json={"title": "Child done"}).json()
    b = client.post("/tickets", json={"title": "Child open"}).json()
    c = client.post("/tickets", json={"title": "Blocked one"}).json()
    for kid in (a, b, c):
        r = client.post(f"/tickets/{root['id']}/edges",
                        json={"dst": kid["id"], "type": "parent_of"})
        assert r.status_code == 201, r.text
    for s in ("open", "running", "review", "done"):
        client.patch(f"/tickets/{a['id']}", json={"status": s})
    client.patch(f"/tickets/{c['id']}", json={"status": "open"})
    client.patch(f"/tickets/{c['id']}", json={"status": "blocked"})
    r = client.get(f"/tickets/{root['id']}/plan")
    assert r.status_code == 200
    plan = r.json()
    assert plan["progress"]["total"] == 3
    assert plan["progress"]["done"] == 1
    assert {i["id"] for i in plan["remaining_items"]} == {b["id"], c["id"]}
    assert {i["id"] for i in plan["blocked_by_owner"]} == {c["id"]}


def _add_marker_child(client, root, title, marker):
    kid = _create(client, title)
    r = client.post(f"/tickets/{root['id']}/edges",
                    json={"dst": kid["id"], "type": "parent_of"})
    assert r.status_code == 201, r.text
    r = client.post(f"/tickets/{kid['id']}/events",
                    json={"kind": "checkpoint", "payload": {"marker": marker}})
    assert r.status_code == 201, r.text
    return kid


def _blocked_ids(client, root):
    plan = client.get(f"/tickets/{root['id']}/plan").json()
    return {i["id"] for i in plan["blocked_by_owner"]}


def test_plan_owner_gated_marker_blocks(client):
    root = _create(client, "Gated root")
    kid = _add_marker_child(client, root, "Owner-gated child", "OWNER-GATED")
    assert _blocked_ids(client, root) == {kid["id"]}


def test_plan_legacy_gate_marker_with_env(client, monkeypatch):
    monkeypatch.setenv("TICKET_HUB_EXTRA_GATE_MARKERS", "LEGACY-GATED")
    root = _create(client, "Legacy root")
    kid = _add_marker_child(client, root, "Legacy-gated child", "LEGACY-GATED")
    assert _blocked_ids(client, root) == {kid["id"]}


def test_plan_legacy_gate_marker_without_env(client):
    root = _create(client, "Legacy root")
    _add_marker_child(client, root, "Legacy-gated child", "LEGACY-GATED")
    assert _blocked_ids(client, root) == set()


def test_admin_status_aggregate(client, tmp_path, monkeypatch):
    monkeypatch.setenv("TICKET_HUB_DIR", str(tmp_path))
    (tmp_path / "health-status.json").write_text(
        '{"ok": true, "ts": "t", "tickets": 3}')
    bdir = tmp_path / "backups"
    bdir.mkdir()
    (bdir / "hub-20261012T030000Z.db").write_text("x")
    resp = client.get("/admin/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["health"]["ok"] is True
    assert body["backups"]["count"] == 1
    assert body["backups"]["newest"].endswith(".db")
    assert body["maintenance"]["last_run"] is None
    assert isinstance(body["services"], list)
    assert body["db_path"].endswith("hub.db")


ASSIGN_REG = {
    "version": 1,
    "roles": [
        {"id": "execution", "mission": "m", "quality_bar": "q",
         "inputs": ["i"], "outputs": ["o"], "seat_pref": "s",
         "default_model_hint": "x", "max_parallel": 2,
         "seats": ["w1", "w2"]},
        {"id": "product-owner", "mission": "m", "quality_bar": "q",
         "inputs": ["i"], "outputs": ["o"], "seat_pref": "s",
         "default_model_hint": "x", "max_parallel": 1,
         "seats": ["w1"]},
    ],
}


def _assign_client(tmp_path, monkeypatch):
    reg = tmp_path / "roles.json"
    reg.write_text(json.dumps(ASSIGN_REG))
    monkeypatch.setenv("TICKET_HUB_ROLES", str(reg))
    monkeypatch.setenv("TICKET_HUB_SEATS_CMD", "true")  # health unknown
    return TestClient(create_app(store=FakeStore()))


def test_assign_release_flow(tmp_path, monkeypatch):
    client = _assign_client(tmp_path, monkeypatch)
    tid = client.post("/tickets", json={"title": "assign me"}).json()["id"]
    r = client.post(f"/tickets/{tid}/assign", json={"role": "execution"})
    assert r.status_code == 201, r.text
    assert r.json()["seat"] in ("w1", "w2")
    assert r.json()["authority"] == "build"
    holder = client.get(f"/tickets/{tid}/assignment").json()["holder"]
    assert holder["seat"] == r.json()["seat"]
    # accept role on a seat that has since contributed => excluded
    r2 = client.post(f"/tickets/{tid}/assign",
                     json={"role": "product-owner", "seat": "w1"})
    # w1 may or may not be the earlier pick; either 201 or 409 with a real code
    assert r2.status_code in (201, 409)
    rel = client.post(f"/tickets/{tid}/release", json={})
    assert rel.status_code == 200
    assert client.get(f"/tickets/{tid}/assignment").json()["holder"] is None


def test_assign_unknown_role_409(tmp_path, monkeypatch):
    client = _assign_client(tmp_path, monkeypatch)
    tid = client.post("/tickets", json={"title": "t"}).json()["id"]
    r = client.post(f"/tickets/{tid}/assign", json={"role": "wizard"})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "unknown_role"


def test_seats_roster_endpoint(tmp_path, monkeypatch):
    client = _assign_client(tmp_path, monkeypatch)
    tid = client.post("/tickets", json={"title": "t"}).json()["id"]
    client.post(f"/tickets/{tid}/assign", json={"role": "execution"})
    body = client.get("/seats").json()
    assert sum(s["load"] for s in body["seats"].values()) == 1
    assert len(body["assignments"]) == 1


def test_list_events(client):
    tid = client.post("/tickets", json={"title": "ev"}).json()["id"]
    client.post(f"/tickets/{tid}/events",
                json={"kind": "thread_message", "payload": {"text": "hi"}})
    resp = client.get(f"/tickets/{tid}/events")
    assert resp.status_code == 200
    kinds = [e["kind"] for e in resp.json()]
    assert kinds[0] == "created" and kinds[-1] == "thread_message"
    assert client.get("/tickets/TK-NOPE/events").status_code == 404
