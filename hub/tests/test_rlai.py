"""Aerial Tickets product on the hub: POST /records, POST /identity and the
/operator mount (docs/DESIGN-aerial-tickets.md).

Runs against the landed Aerial packages named by RLAI_PACKAGES_DIR (a folder
holding rlai-contracts, rlai-tickets, rlai-operator, rlai-runtime and
rlai-roles); skipped when it is unset.
"""
import os
import random
import sqlite3
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)

import pytest
from fastapi.testclient import TestClient

from api import create_app
from hub_gateway import StoreGateway
from ticket_hub import rlai
from ticket_hub.access import AccessStore
from ticket_hub.model import Actor
from ticket_hub.policy import check_policies
from ticket_hub.store import Store

PKGS = os.environ.get("RLAI_PACKAGES_DIR", "")
pytestmark = pytest.mark.skipif(
    not PKGS or not os.path.isdir(os.path.join(PKGS, "rlai-tickets")),
    reason="RLAI_PACKAGES_DIR not set to the Aerial packages folder")

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _rid(prefix: str) -> str:
    return prefix + "-" + "".join(random.choice(_CROCKFORD) for _ in range(26))


def c5(employee_id=None, **over) -> dict:
    msg = {"version": "1.0.0", "employee_id": employee_id or _rid("EM"),
           "display_name": "A5 Test", "kind": "agent",
           "token_ref": "/home/you/.aerial/tokens/a5-test-01-read.token",
           "message_id": _rid("MS"), "producer": "roles@test",
           "sent_at": "2026-11-10T00:00:00Z"}
    msg.update(over)
    return msg


def c2(ticket_id, employee_id, seq=1, **over) -> dict:
    msg = {"version": "1.1.0", "event_type": "run_started",
           "ticket_id": ticket_id, "employee_id": employee_id,
           "run_id": "RN-" + "C" * 26, "ts": "2026-11-10T00:00:00Z",
           "message_id": _rid("MS"), "producer": "runtime@test",
           "sent_at": "2026-11-10T00:00:00Z", "seq": seq}
    msg.update(over)
    return msg


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("RLAI_PACKAGES_DIR", PKGS)
    monkeypatch.delenv("RLAI_OPERATOR", raising=False)
    return _build(tmp_path)


def _build(tmp_path):
    store = Store(str(tmp_path / "hub.db"))
    gateway = StoreGateway(store)
    access = AccessStore(store._conn)
    access.create_actor("own", "Own", kind="human", is_owner=True,
                        by="bootstrap")
    access.create_actor("bot", "Bot", kind="agent", by="own")
    access.create_actor("bot2", "Bot Two", kind="agent", by="own")
    access.create_actor("e1", "E One", realm="external", sponsor_id="own",
                        expires_at="2099-01-01T00:00:00Z", by="own")
    tokens = {a: access.issue_credential(a, by="own")[1]
              for a in ("own", "bot", "bot2", "e1")}
    ticket = store.create_ticket("A5 task", owner=Actor(id="own", name="Own",
                                                        kind="human"))
    app = create_app(gateway, auth=True)
    app.state.access = access

    class Env:
        pass
    e = Env()
    e.store, e.gateway, e.access, e.app = store, gateway, access, app
    e.client, e.tokens, e.ticket = TestClient(app), tokens, ticket
    e.h = lambda a: {"Authorization": "Bearer " + tokens[a]}
    return e


def _register(env, who="bot", **over):
    msg = c5(**over)
    r = env.client.post("/identity", json=msg, headers=env.h(who))
    assert r.status_code == 201, r.text
    return msg


def _record_events(env):
    return [e for e in env.store.events(env.ticket.id)
            if "record" in e.payload]


# ---- POST /records ---------------------------------------------------------

def test_record_first_delivery_201_second_200_duplicate(env):
    emp = _register(env)["employee_id"]
    msg = c2(env.ticket.id, emp)
    r1 = env.client.post("/records", json=msg, headers=env.h("bot"))
    assert r1.status_code == 201, r1.text
    assert r1.json()["duplicate"] is False
    assert r1.json()["event"]["kind"] == "checkpoint"
    r2 = env.client.post("/records", json=msg, headers=env.h("bot"))
    assert r2.status_code == 200
    assert r2.json()["duplicate"] is True
    assert r2.json()["event"]["id"] == r1.json()["event"]["id"]
    stored = _record_events(env)
    assert [e.payload["record"]["message_id"] for e in stored] == \
        [msg["message_id"]]


def test_record_event_actor_is_employee_with_via_poster(env):
    emp = _register(env)["employee_id"]
    env.client.post("/records", json=c2(env.ticket.id, emp),
                    headers=env.h("bot"))
    (ev,) = _record_events(env)
    assert ev.actor.id == emp
    assert ev.actor.name == "A5 Test"  # the C5 display name is kept
    assert ev.via == "bot"


def test_record_invalid_422(env):
    emp = _register(env)["employee_id"]
    bad = c2(env.ticket.id, emp)
    del bad["seq"]
    r = env.client.post("/records", json=bad, headers=env.h("bot"))
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_c2"
    assert _record_events(env) == []


def test_record_missing_ticket_404(env):
    emp = _register(env)["employee_id"]
    r = env.client.post("/records", json=c2("TK-" + "Z" * 26, emp),
                        headers=env.h("bot"))
    assert r.status_code == 404


def test_record_unregistered_employee_403(env):
    r = env.client.post("/records", json=c2(env.ticket.id, _rid("EM")),
                        headers=env.h("bot"))
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "unknown_employee"
    assert _record_events(env) == []


def test_record_other_internal_poster_403(env):
    emp = _register(env, who="bot")["employee_id"]
    r = env.client.post("/records", json=c2(env.ticket.id, emp),
                        headers=env.h("bot2"))
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "not_registrar"
    assert _record_events(env) == []


def test_identity_reregister_by_other_internal_403_owner_ok(env):
    msg = _register(env, who="bot")
    again = c5(employee_id=msg["employee_id"], token_ref="/elsewhere/x")
    r = env.client.post("/identity", json=again, headers=env.h("bot2"))
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "not_registrar"
    r = env.client.post("/identity", json=again, headers=env.h("own"))
    assert r.status_code == 201


def test_record_owner_may_post_for_unregistered(env):
    r = env.client.post("/records", json=c2(env.ticket.id, _rid("EM")),
                        headers=env.h("own"))
    assert r.status_code == 201, r.text


def test_record_external_403_and_no_token_401(env):
    emp = _register(env)["employee_id"]
    msg = c2(env.ticket.id, emp)
    assert env.client.post("/records", json=msg,
                           headers=env.h("e1")).status_code == 403
    assert env.client.post("/records", json=msg).status_code == 401
    assert _record_events(env) == []


def test_record_seq_gap_flagged(env):
    emp = _register(env)["employee_id"]
    first = c2(env.ticket.id, emp, seq=1)
    env.client.post("/records", json=first, headers=env.h("bot"))
    r = env.client.post("/records", json=c2(
        env.ticket.id, emp, seq=3, causation_id=first["message_id"]),
        headers=env.h("bot"))
    assert r.status_code == 201
    assert r.json()["event"]["seq_gap"] is True


def test_record_unknown_enum_stored_unrecognized(env):
    emp = _register(env)["employee_id"]
    r = env.client.post("/records",
                        json=c2(env.ticket.id, emp, event_type="future_kind"),
                        headers=env.h("bot"))
    assert r.status_code == 201, r.text
    assert r.json()["event"]["unrecognized"] is True


def test_record_outcome_kind(env):
    emp = _register(env)["employee_id"]
    r = env.client.post("/records", json=c2(
        env.ticket.id, emp, event_type="outcome", result="resolved",
        elapsed_s=1.0, outcome_source="agent_reported"),
        headers=env.h("bot"))
    assert r.status_code == 201, r.text
    assert r.json()["event"]["kind"] == "checkpoint"


def test_records_503_without_packages(tmp_path, monkeypatch):
    monkeypatch.delenv("RLAI_PACKAGES_DIR", raising=False)
    monkeypatch.setattr(rlai, "_RECORD_STORE_CLASS", None)
    saved = {k: v for k, v in sys.modules.items()
             if k.startswith(("rlai_tickets", "rlai_contracts"))}
    e = _build(tmp_path)
    try:
        r = e.client.post("/records", json=c2(e.ticket.id, _rid("EM")),
                          headers=e.h("own"))
        assert r.status_code == 503
        assert r.json()["error"]["code"] == "rlai_unavailable"
    finally:
        sys.modules.update(saved)


# ---- POST /identity -------------------------------------------------------

def test_identity_201_creates_actor_then_200_duplicate(env):
    msg = c5()
    r1 = env.client.post("/identity", json=msg, headers=env.h("bot"))
    assert r1.status_code == 201, r1.text
    assert r1.json() == {"employee_id": msg["employee_id"],
                         "message_id": msg["message_id"],
                         "duplicate": False}
    actor = env.store.get_actor(msg["employee_id"])
    assert actor.name == "A5 Test" and actor.kind == "agent"
    attrs = env.access.actor_attrs(msg["employee_id"]) \
        if hasattr(env.access, "actor_attrs") else None
    if attrs is not None:
        assert not attrs["is_owner"] and attrs["realm"] == "internal"
    r2 = env.client.post("/identity", json=msg, headers=env.h("bot"))
    assert r2.status_code == 200 and r2.json()["duplicate"] is True
    n = env.store._conn.execute(
        "SELECT COUNT(*) FROM rlai_identities").fetchone()[0]
    assert n == 1


def test_identity_new_message_same_employee_is_accepted(env):
    msg = _register(env)
    again = c5(employee_id=msg["employee_id"])
    r = env.client.post("/identity", json=again, headers=env.h("bot"))
    assert r.status_code == 201


def test_identity_invalid_422(env):
    r = env.client.post("/identity", json=c5(token_ref="not-a-path"),
                        headers=env.h("bot"))
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_c5"


def test_identity_kind_conflict_409(env):
    msg = _register(env)
    r = env.client.post("/identity",
                        json=c5(employee_id=msg["employee_id"],
                                kind="system"),
                        headers=env.h("bot"))
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "kind_conflict"


def test_identity_human_needs_owner(env):
    r = env.client.post("/identity", json=c5(kind="human"),
                        headers=env.h("bot"))
    assert r.status_code == 403
    r = env.client.post("/identity", json=c5(kind="human"),
                        headers=env.h("own"))
    assert r.status_code == 201


def test_identity_never_echoes_token_ref(env):
    msg = c5()
    r = env.client.post("/identity", json=msg, headers=env.h("bot"))
    assert msg["token_ref"] not in r.text


def test_identity_external_403_no_token_401(env):
    assert env.client.post("/identity", json=c5(),
                           headers=env.h("e1")).status_code == 403
    assert env.client.post("/identity", json=c5()).status_code == 401


def test_identity_table_is_append_only(env):
    _register(env)
    with pytest.raises(sqlite3.DatabaseError):
        env.store._conn.execute("DELETE FROM rlai_identities")


# ---- compatibility: record() itself on a hub Store --------------------------

def test_hub_record_store_compat(tmp_path, monkeypatch):
    monkeypatch.setenv("RLAI_PACKAGES_DIR", PKGS)
    store = Store(str(tmp_path / "hub.db"))
    t = store.create_ticket("compat")
    rs = rlai.hub_record_store(store)
    emp = _rid("EM")
    msg = c2(t.id, emp)
    ev1, dup1 = rs.record_as(msg, via=None)
    ev2, dup2 = rs.record_as(dict(msg), via=None)
    assert (dup1, dup2) == (False, True)
    assert ev1["id"] == ev2["id"]
    # the plain upstream entry point works too
    ev3 = rs.record(c2(t.id, emp, seq=2, causation_id=msg["message_id"]))
    assert ev3["kind"] == "checkpoint"
    assert store.get_actor(emp).family == "aerial"


# ---- /operator -------------------------------------------------------------

@pytest.fixture()
def op_env(tmp_path, monkeypatch):
    monkeypatch.setenv("RLAI_PACKAGES_DIR", PKGS)
    monkeypatch.setenv("RLAI_OPERATOR", "1")
    monkeypatch.delenv("RLAI_RUNTIME_DB", raising=False)
    monkeypatch.delenv("RLAI_ROLES_DB", raising=False)
    return _build(tmp_path)


def test_operator_not_mounted_without_flag(env):
    r = env.client.get("/operator/", headers=env.h("own"))
    assert r.status_code == 404


def test_operator_403_without_authorization(op_env):
    r = op_env.client.get("/operator/")
    assert r.status_code == 403


def test_operator_403_for_non_owner_and_external(op_env):
    assert op_env.client.get("/operator/",
                             headers=op_env.h("bot")).status_code == 403
    assert op_env.client.get("/operator/",
                             headers=op_env.h("e1")).status_code == 403
    assert op_env.client.get(
        "/operator/", headers={"Authorization": "Bearer nope"}
    ).status_code == 403


def test_operator_200_for_owner_bearer_and_cookie(op_env):
    r = op_env.client.get("/operator/", headers=op_env.h("own"))
    assert r.status_code == 200, r.text
    assert "text/html" in r.headers["content-type"]
    c = TestClient(op_env.app)
    c.cookies.set("hub_session", op_env.tokens["own"])
    assert c.get("/operator/").status_code == 200


def test_operator_view_json_panels(op_env):
    emp = _register(op_env, who="own")["employee_id"]
    op_env.client.post("/records", json=c2(
        op_env.ticket.id, emp, event_type="outcome", result="resolved",
        elapsed_s=2.0, cost_usd=0.0, outcome_source="agent_reported"),
        headers=op_env.h("own"))
    r = op_env.client.get("/operator/view.json", headers=op_env.h("own"))
    assert r.status_code == 200, r.text
    doc = r.json()["panels"]
    for panel in ("employees", "active", "blocked", "outcomes", "findings"):
        assert panel in doc, panel
    # no runtime/roles databases configured: those panels are unavailable
    assert doc["employees"]["state"] == "unavailable"
    assert doc["outcomes"]["state"] == "ok"
    rows = doc["outcomes"]["rows"]
    assert rows and rows[0]["ticket_id"] == op_env.ticket.id
    assert rows[0]["outcome"] == "resolved"


def test_operator_refuses_non_get(op_env):
    r = op_env.client.post("/operator/", headers=op_env.h("own"))
    assert r.status_code in (404, 405)


def test_operator_decisions_audited(op_env):
    op_env.client.get("/operator/")
    op_env.client.get("/operator/", headers=op_env.h("own"))
    rows = op_env.store._conn.execute(
        "SELECT actor_id, decision FROM access_log WHERE route = '/operator'"
        " ORDER BY seq").fetchall()
    assert [(r[0], r[1]) for r in rows] == [(None, "deny"), ("own", "allow")]


def test_policies_complete_with_operator(op_env):
    check_policies(op_env.app)


def test_policies_complete_without_operator(env):
    check_policies(env.app)


def test_operator_reads_roles_and_runtime_read_only(tmp_path, monkeypatch):
    """Roles and Runtime databases built by their own packages, then opened
    by the view with mode=ro: the employees and active panels are ok and
    neither file changes."""
    monkeypatch.setenv("RLAI_PACKAGES_DIR", PKGS)
    rlai.load_packages()
    from datetime import datetime, timezone
    from rlai_roles.registry import Registry
    from rlai_runtime.runs import RunStore
    roles_db, runtime_db = tmp_path / "roles.db", tmp_path / "runtime.db"
    conn = sqlite3.connect(str(roles_db))
    Registry(conn, now=lambda: datetime.now(timezone.utc))
    conn.commit()
    conn.close()
    RunStore.open(str(runtime_db))._conn.close()  # checkpoints the WAL
    before = (roles_db.read_bytes(), runtime_db.read_bytes())
    monkeypatch.setenv("RLAI_OPERATOR", "1")
    monkeypatch.setenv("RLAI_ROLES_DB", str(roles_db))
    monkeypatch.setenv("RLAI_RUNTIME_DB", str(runtime_db))
    (tmp_path / "hub").mkdir()
    e = _build(tmp_path / "hub")
    r = e.client.get("/operator/view.json", headers=e.h("own"))
    assert r.status_code == 200, r.text
    panels = r.json()["panels"]
    assert panels["employees"]["state"] == "ok", panels["employees"]
    assert panels["active"]["state"] == "ok", panels["active"]
    assert (roles_db.read_bytes(), runtime_db.read_bytes()) == before


# ---- review round 1 regressions ---------------------------------------------

def test_events_since_naive_offset_and_empty(env):
    emp = _register(env)["employee_id"]
    env.client.post("/records", json=c2(env.ticket.id, emp),
                    headers=env.h("bot"))
    get = rlai.InProcessGetter(env.gateway)
    everything = get("/events?since=2000-01-01T00:00:00Z")
    assert everything
    # no offset: read as UTC, no TypeError on naive vs aware
    assert len(get("/events?since=2000-01-01T00:00:00")) == len(everything)
    # an offset is honoured: 2099 at +14:00 is still far in the future
    assert get("/events?since=2099-01-01T00:00:00%2B14:00") == []
    # missing or unparsable since never dumps the table
    assert get("/events") == []
    assert get("/events?since=yesterday") == []


def test_identity_422_names_fields_only(env):
    secret = "not a path but a secret-looking value"
    r = env.client.post("/identity", json=c5(token_ref=secret),
                        headers=env.h("bot"))
    assert r.status_code == 422
    assert secret not in r.text
    assert "token_ref" in r.json()["error"]["message"]


def test_aerial_writers_share_one_lock(env):
    rs = rlai.hub_record_store(env.store)
    ids = rlai.IdentityStore(env.store._conn)
    assert rs._lock is ids._lock is rlai.conn_lock(env.store._conn)


# ---- review round 2 regressions ---------------------------------------------

def test_placeholder_actor_takes_first_identity_kind(env):
    emp = _rid("EM")
    r = env.client.post("/records", json=c2(env.ticket.id, emp),
                        headers=env.h("own"))
    assert r.status_code == 201
    assert env.store.get_actor(emp).kind == "agent"  # placeholder
    r = env.client.post("/identity", json=c5(employee_id=emp, kind="human",
                                             display_name="Person"),
                        headers=env.h("own"))
    assert r.status_code == 201, r.text
    actor = env.store.get_actor(emp)
    assert (actor.kind, actor.name) == ("human", "Person")
    # once registered, the kind is fixed
    r = env.client.post("/identity", json=c5(employee_id=emp, kind="agent"),
                        headers=env.h("own"))
    assert r.status_code == 409


def test_conn_lock_not_shared_by_a_new_connection(tmp_path):
    a = sqlite3.connect(str(tmp_path / "a.db"))
    lock_a = rlai.conn_lock(a)
    b = sqlite3.connect(str(tmp_path / "b.db"))
    assert rlai.conn_lock(b) is not lock_a
    assert rlai.conn_lock(a) is lock_a


def test_ro_conn_handles_odd_path(tmp_path):
    odd = tmp_path / "we?ird #db%.db"
    sqlite3.connect(str(odd)).close()
    conn = rlai._ro_conn(str(odd))
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("CREATE TABLE t (x)")
    conn.close()


# ---- review round 3 regressions ---------------------------------------------

def test_identity_store_never_issues_ddl(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "x.db"))
    with pytest.raises(rlai.RlaiUnavailable):
        rlai.IdentityStore(conn).is_registered("EM-" + "A" * 26)
    assert conn.execute("SELECT COUNT(*) FROM sqlite_master"
                        " WHERE name = 'rlai_identities'").fetchone()[0] == 0
    rlai.ensure_identity_schema(conn)
    assert not conn.in_transaction
    rlai.IdentityStore(conn)  # present now


def test_schema_created_at_startup_for_a_lazy_store(tmp_path, monkeypatch):
    """Production shape: create_app() with no store builds it on the first
    get_store(); the lifespan start-up creates the identity table before
    any request."""
    monkeypatch.setenv("RLAI_PACKAGES_DIR", PKGS)
    db = tmp_path / "lazy.db"
    monkeypatch.setenv("TICKET_HUB_DB", str(db))
    app = create_app(None, auth=False)
    with TestClient(app):  # runs the lifespan
        conn = sqlite3.connect(str(db))
        n = conn.execute("SELECT COUNT(*) FROM sqlite_master"
                         " WHERE name = 'rlai_identities'").fetchone()[0]
        conn.close()
    assert n == 1


def test_503_body_names_no_path(tmp_path, monkeypatch):
    monkeypatch.setenv("RLAI_PACKAGES_DIR", str(tmp_path / "nowhere"))
    monkeypatch.setattr(rlai, "_RECORD_STORE_CLASS", None)
    e = _build(tmp_path)
    r = e.client.post("/records", json=c2(e.ticket.id, _rid("EM")),
                      headers=e.h("own"))
    assert r.status_code == 503
    assert "nowhere" not in r.text


def test_startup_schema_failure_does_not_stop_the_hub(tmp_path, monkeypatch):
    monkeypatch.setenv("RLAI_PACKAGES_DIR", PKGS)
    monkeypatch.setenv("TICKET_HUB_DB", str(tmp_path / "lazy.db"))

    def boom(conn):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(rlai, "ensure_identity_schema", boom)
    app = create_app(None, auth=False)
    with TestClient(app) as client:  # start-up survives
        assert client.get("/health").status_code == 200
        r = client.post("/identity", json=c5())
        assert r.status_code == 503

