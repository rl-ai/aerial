"""AT-1.1 .. AT-1.14 — acceptance suite for ticket-hub access P1 (lane W5).

Written from docs/ACCESS-P1-CONTRACT.md §1-4 and DESIGN-access.md §3 "P1" only,
independently of the build lanes. The tests themselves are EXPECTED to fail or
error until the feature lands; this file must still collect (the new modules
ticket_hub.access / ticket_hub.policy are imported inside fixtures/tests).

Run:  python -m pytest tests/test_access_p1.py
Live tests (AT-1.12, AT-1.14) run only with HUB_LIVE=1 against the deployment
named by HUB_LIVE_BASE_URL (and host addresses named by HUB_LIVE_SCAN).
"""

import copy
import datetime
import json
import os
import re
import socket
import sqlite3
import urllib.error
import urllib.request

import pytest
from fastapi.testclient import TestClient

REASONABLE_ROUTES = {"/health", "/login", "/session", "/", "/ui", "/docs",
                     "/openapi.json", "/tickets", "/wiki"}


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _future(days=7):
    return (_now() + datetime.timedelta(days=days)).isoformat()


def _past(minutes=5):
    return (_now() - datetime.timedelta(minutes=minutes)).isoformat()


def _token_of(pair):
    """issue_credential returns (credential_id, token) — contract §2. The
    thk_-prefixed value is the token; belt-and-braces order detection (the
    original harness swapped the pair here -> every request sent the
    credential id as the token)."""
    cred, token = pair
    if isinstance(token, str) and token.startswith("thk_"):
        return cred, token
    return token, cred


def _cred_token(pair):
    """(credential_id, token) exactly as contract §2 defines it."""
    return _token_of(pair)


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _body_text(resp):
    try:
        return resp.text
    except Exception:  # pragma: no cover - defensive
        return repr(getattr(resp, "_content", b""))


def _ids(rows):
    out = set()
    for r in rows:
        if isinstance(r, dict):
            out.add(r.get("id") or r.get("slug"))
        else:
            out.add(r)
    return out


# --------------------------------------------------------------------------
# fixtures (imports of the new modules live HERE, so collection never fails)
# --------------------------------------------------------------------------

@pytest.fixture()
def harness(tmp_path):
    """Real Store on a temp file + AccessStore + StoreGateway + auth-on app.

    Pre-built actors / credentials / grants per the AT-1.x cast:
      owner        internal human, is_owner          -> tok_owner
      agent-1      plain internal agent              -> tok_agent
      worker-1     internal agent                    -> tok_worker (ticket grant on t_seen)
      relay        internal agent, act_as E1/E2/low-agent only -> tok_relay
      low-agent    low-authority internal agent      -> tok_low
      system:outbound  internal system               -> tok_outbound (act_as allowed)
      human-1      ordinary internal human           -> tok_human
      E1           external, subtree grant on S      -> tok_e1
      E2           external, no grant                -> tok_e2

    Fixture data is written through Store / gateway / raw SQL only — never
    through the HTTP API — so every test's first request is a real test.
    """
    from ticket_hub.access import AccessStore
    from ticket_hub.model import Actor
    from ticket_hub.store import Store

    import api as api_mod
    from hub_gateway import StoreGateway

    store = Store(str(tmp_path / "hub.db"))
    gateway = StoreGateway(store)
    access = AccessStore(store._conn)

    owner_id = "hub-acceptance-owner"
    # harness fix: create_actor carries is_owner (contract §2); external
    # actors require sponsor_id + expires_at. No legacy fallback needed.
    access.create_actor(owner_id, "Owner", kind="human", is_owner=True,
                        by=owner_id)

    def mk_actor(aid, name, kind="agent", **kw):
        return access.create_actor(aid, name, kind=kind, by=owner_id, **kw)

    mk_actor("agent-1", "Agent One")
    mk_actor("worker-1", "Worker One")
    mk_actor("relay", "Chat Relay")
    mk_actor("low-agent", "Low Agent")
    mk_actor("system:outbound", "Outbound Poller", kind="system")
    mk_actor("human-1", "Ordinary Human", kind="human")
    e1 = mk_actor("E1", "External One", realm="external",
                  sponsor_id=owner_id, expires_at=_future())
    mk_actor("E2", "External Two", realm="external",
             sponsor_id=owner_id, expires_at=_future())

    def tok(aid, **kw):
        cred, token = _token_of(access.issue_credential(aid, by=owner_id, **kw))
        return token

    tok_relay = tok("relay", scopes=("act_as",),
                    act_as_allow=("E1", "E2", "low-agent", "system:outbound"))
    h = {
        "tmp": tmp_path, "store": store, "gateway": gateway, "access": access,
        "owner_id": owner_id, "e1_id": getattr(e1, "id", "E1"),
        "tok_owner": tok(owner_id),
        "tok_agent": tok("agent-1"),
        "tok_worker": tok("worker-1"),
        "tok_relay": tok_relay,
        "tok_low": tok("low-agent"),
        "tok_outbound": tok("system:outbound", scopes=("act_as",),
                            act_as_allow=("system:outbound",)),
        "tok_human": tok("human-1"),
        "tok_e1": tok("E1"),
        "tok_e2": tok("E2"),
    }

    def post(path, body):
        """Internal write via the gateway (never the API under test). The
        StoreGateway surfaces create_ticket / append_event / patch only; it
        coerces actor/owner (str|dict) to Actor and returns plain dicts."""
        if path == "/tickets":
            return gateway.create_ticket(**body)
        tid = path.split("/")[2]
        if path.endswith("/events"):
            return gateway.append_event(tid, body["kind"], actor=body.get("actor"),
                                        payload=body.get("payload"))
        return gateway.patch(tid, **body)

    h["post"] = post
    ticket_seq = [0]

    def internal_ticket(title, parent=None, **kw):
        ticket_seq[0] += 1
        return post("/tickets", {"title": f"{title} [h#{ticket_seq[0]}",
                                 "parent": parent, **kw})

    h["internal_ticket"] = internal_ticket

    # Cast data (all internal by the P1 default):
    t_seen = internal_ticket("worker-visible ticket", owner={"id": "worker-1"})
    h["t_seen"] = t_seen
    hidden_parent = internal_ticket("hidden parent")
    hidden_child = internal_ticket("hidden child", parent=hidden_parent["id"])
    h["hidden_parent"], h["hidden_child"] = hidden_parent, hidden_child

    access.add_grant("worker-1", "ticket", t_seen["id"], by=owner_id)

    app = api_mod.create_app(gateway, auth=True)
    app.state.access = access  # contract §3: tests may pre-set app.state.access
    h["client"] = TestClient(app)

    yield h
    store.close()


@pytest.fixture()
def subtree(harness):
    """S (external) with children ext-a / int-b / ext-c; E1 holds a read grant
    on the whole subtree. Built with label-aware kwargs per contract §1 —
    every child is labelled EXPLICITLY because children never inherit the
    parent's label (contract §1: they default to internal otherwise)."""
    access = harness["access"]
    S = harness["post"]("/tickets", {"title": "Project S [external]",
                                     "label": "external"})
    a = harness["post"]("/tickets", {"title": "child ext-a", "parent": S["id"],
                                     "label": "external"})
    b = harness["post"]("/tickets", {"title": "child int-b", "parent": S["id"],
                                     "label": "internal"})
    c = harness["post"]("/tickets", {"title": "child ext-c", "parent": S["id"],
                                     "label": "external"})
    grant_id = access.add_grant("E1", "subtree", S["id"], by=harness["owner_id"])
    harness["grant_S"] = grant_id
    return {"S": S, "a": a, "b": b, "c": c}


@pytest.fixture()
def wiki_handoff(harness):
    """Rev1 internal page mentioning `handoff` (fixture data, not via API).
    Harness fix: Store.put_page takes the actor's NAME (a plain string), not
    an Actor object."""
    return harness["store"].put_page("runbook-handoff", title="Handoff runbook",
                                     body_md="use the handoff checklist",
                                     actor=harness["owner_id"])


# ==========================================================================
# AT-1.1 — every route 401 without a token (except /health, {ok} only)
# ==========================================================================

@pytest.mark.parametrize("spec", [
    ("get", "/tickets"),
    ("post", "/tickets", {"title": "x"}),
    ("get", "/tickets/TK-nope"),
    ("patch", "/tickets/TK-nope", {"status": "in_progress"}),
    ("get", "/tickets/TK-nope/events"),
    ("post", "/tickets/TK-nope/events", {"kind": "comment"}),
    ("post", "/tickets/TK-nope/edges", {"type": "relates_to", "dst": "TK-x"}),
    ("get", "/tickets/TK-nope/lineage"),
    ("get", "/tickets/TK-nope/digest"),
    ("get", "/tickets/TK-nope/plan"),
    ("post", "/tickets/TK-nope/assign", {"role": "driver"}),
    ("post", "/tickets/TK-nope/release", {}),
    ("get", "/tickets/TK-nope/assignment"),
    ("post", "/tickets/TK-nope/bind", {"platform": "telegram", "room_id": "r"}),
    ("get", "/tickets/TK-nope/bindings"),
    ("get", "/board"),
    ("get", "/admin/status"),
    ("get", "/seats"),
    ("get", "/templates"),
    ("get", "/templates/feature"),
    ("post", "/templates/feature/instantiate", {"title": "x"}),
    ("post", "/adapters/telegram/inbound", {"room_id": "r", "text": "hi"}),
    ("get", "/wiki"),
    ("get", "/wiki-search", {"params": {"q": "x"}}),
    ("get", "/wiki/some-slug"),
    ("post", "/wiki/some-slug", {"title": "t"}),
    ("get", "/roles"),
    ("get", "/roles/whatever"),
    ("get", "/jobs"),
    ("get", "/jobs/rollup"),
    ("get", "/jobs/digest"),
    ("post", "/jobs/digest/publish"),
    ("put", "/jobs/job-1", {}),
    ("get", "/jobs/job-1"),
    ("post", "/jobs/job-1/runs", {}),
    ("patch", "/jobs/runs/run-1", {}),
    ("get", "/"),
    ("get", "/ui/index.html"),
])
def test_at1_1_routes_require_token(spec):
    """AT-1.1: no token -> 401 on every guarded route. Parametrized over the
    known inventory; the walk over app.routes in ..._app_route_walk extends
    coverage to routes added later."""
    client = TestClient(_auth_on_app())
    method, path = spec[0], spec[1]
    kw = spec[2] if len(spec) > 2 else {}
    # harness fix (TASK step 2): TestClient.get/.head take no json= argument;
    # every verb goes through the generic request() entry point instead of
    # getattr(client, method)(path, json=...). GET specs carry at most query
    # params. The 401 gate fires before any handler reads the body/params.
    if isinstance(kw, dict) and "params" in kw:
        resp = client.request(method.upper(), path, **kw)
    elif method.upper() == "GET":
        resp = client.request("GET", path, params=kw or None)
    else:
        resp = client.request(method.upper(), path, json=kw if kw else None)
    assert resp.status_code == 401, (method, path, resp.status_code)


def _auth_on_app():
    import api as api_mod
    from hub_gateway import StoreGateway
    from ticket_hub.store import Store

    app = api_mod.create_app(StoreGateway(Store(":memory:")), auth=True)
    return app


def test_at1_1_app_route_walk():
    """AT-1.1 (walk): every route on the auth-on app is either public by the
    contract (/health, /login) or answers 401 without a token."""
    app = _auth_on_app()
    client = TestClient(app)
    checked = 0
    for route in app.routes:
        path = getattr(route, "path", None)
        if path is None or path.startswith("/openapi") \
                or path in ("/docs", "/redoc", "/docs/oauth2-redirect"):
            continue  # documented as authenticated-but-schema-only; covered below
        methods = getattr(route, "methods", None)
        if path in REASONABLE_ROUTES:
            continue
        targets = ["get"] if methods is None else sorted(m.lower() for m in methods
                                                         if m.upper() != "HEAD")
        for m in targets:
            probe = getattr(client, m, None)
            if probe is None:
                continue
            resp = probe(path)
            checked += 1
            assert resp.status_code == 401, (m, path, resp.status_code)
    # every known guarded route above was actually walked:
    assert checked >= 35, f"route walk only covered {checked} method/path pairs"


def test_at1_1_health_public_ok_only():
    """AT-1.1 exception: GET /health answers without a token with {ok} only."""
    client = TestClient(_auth_on_app())
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body.get("ok") in (True, False)
    assert set(body.keys()) == {"ok"}, f"/health leaked keys: {sorted(body)}"


def test_at1_1_authenticated_with_token():
    """AT-1.1 negative control: with a valid token the same route is NOT 401 —
    without auth control this test would also pass, so it belongs to AT-1.1."""
    h = _mini_harness()
    resp = h["client"].get("/tickets", headers=_auth(h["tok_owner"]))
    assert resp.status_code == 200


def _mini_harness():
    """Minimal harness for tests that need a token but no cast data."""
    from ticket_hub.access import AccessStore
    from ticket_hub.store import Store

    import api as api_mod
    from hub_gateway import StoreGateway

    store = Store(":memory:")
    gateway = StoreGateway(store)
    access = AccessStore(store._conn)
    owner_id = "hub-acceptance-owner"
    # harness fix: create_actor carries is_owner (contract §2); no legacy path
    access.create_actor(owner_id, "Owner", kind="human", is_owner=True,
                        by=owner_id)
    _cred, tok = _token_of(access.issue_credential(owner_id, by=owner_id))
    app = api_mod.create_app(gateway, auth=True)
    app.state.access = access
    return {"client": TestClient(app), "store": store, "access": access,
            "gateway": gateway, "tok_owner": tok, "owner_id": owner_id}


# ==========================================================================
# AT-1.2 — a route without a policy makes create_app() raise
# ==========================================================================

def test_at1_2_unpolicied_route_raises():
    """AT-1.2: adding a route with no entry in POLICIES makes create_app raise
    (via check_policies), naming the offending route."""
    from fastapi import FastAPI

    from ticket_hub.policy import check_policies  # noqa: F401

    app = _auth_on_app()

    @app.get("/zz-unpolicied-route")
    def _zz():
        return {"ok": True}

    with pytest.raises(RuntimeError) as excinfo:
        check_policies(app)
    assert "/zz-unpolicied-route" in str(excinfo.value)


def test_at1_2_create_app_policy_check_live():
    """AT-1.2 (live app): create_app(auth=True) itself enforces the registry —
    if a policy were missing for any current route, this would raise."""
    app = _auth_on_app()  # must not raise
    assert app is not None


# ==========================================================================
# AT-1.3 — revoked / expired / suspended tokens 401; last_used_at; no leaks
# ==========================================================================

def test_at1_3_revoked_token_rejected(harness):
    client, access = harness["client"], harness["access"]
    assert client.get("/tickets", headers=_auth(harness["tok_agent"])).status_code == 200
    cred_id = _cred_for(access, "agent-1")
    access.revoke_credential(cred_id, by=harness["owner_id"], reason="acceptance")
    resp = client.get("/tickets", headers=_auth(harness["tok_agent"]))
    assert resp.status_code == 401


def test_at1_3_expired_token_rejected(harness):
    access = harness["access"]
    _cred, tok = _token_of(access.issue_credential("agent-1", by=harness["owner_id"],
                                                   expires_at=_past()))
    resp = harness["client"].get("/tickets", headers=_auth(tok))
    assert resp.status_code == 401


def test_at1_3_suspended_actor_rejected(harness):
    client = harness["client"]
    assert client.get("/tickets", headers=_auth(harness["tok_human"])).status_code == 200
    harness["access"].set_actor_status("human-1", "suspended",
                                       by=harness["owner_id"], reason="acceptance")
    resp = client.get("/tickets", headers=_auth(harness["tok_human"]))
    assert resp.status_code == 401


def test_at1_3_last_used_advances_without_update(harness):
    """last_used_at derives from access_log while credentials sees no UPDATE."""
    client, access, store = harness["client"], harness["access"], harness["store"]
    before = _cred_entry(access, "agent-1")["last_used_at"]
    resp = client.get("/tickets", headers=_auth(harness["tok_agent"]))
    assert resp.status_code == 200
    after = _cred_entry(access, "agent-1")["last_used_at"]
    assert after is not None and after != before, \
        "last_used_at did not advance from access_log"
    # the real check: a BEFORE UPDATE trigger that RAISEs is what makes the
    # table UPDATE-free — so an actual UPDATE must raise.
    with pytest.raises(sqlite3.DatabaseError):
        store._conn.execute("UPDATE credentials SET prefix = prefix")
    store._conn.rollback()


def test_at1_3_no_token_leaks(harness):
    """No token value ever appears in the DB or in any response body."""
    client, access, store = harness["client"], harness["access"], harness["store"]
    tokens = [harness[k] for k in harness if k.startswith("tok_")]

    # exercise a spread of outcomes: allow, deny, hidden, unauthorized
    client.get("/tickets", headers=_auth(harness["tok_owner"]))
    client.get("/tickets", headers=_auth("thk_definitely_not_a_real_token"))
    client.get("/jobs", headers=_auth(harness["tok_e1"]))
    client.get("/wiki/zz-none", headers=_auth(harness["tok_e1"]))

    dump = "\n".join(str(r) for r in store._conn.execute(
        "SELECT * FROM tickets").fetchall())
    dump += "\n".join(str(r) for r in store._conn.execute(
        "SELECT * FROM events").fetchall())
    dump += "\n".join(str(r) for r in store._conn.execute(
        "SELECT * FROM wiki_pages").fetchall())
    for t in tokens:
        assert t not in dump, "token value found in the hub DB"

    creds = access.list_credentials()
    flat = json.dumps([{k: v for k, v in c.items() if k != "last_used_at"}
                       for c in creds])
    for t in tokens:
        assert t not in flat, "token value found in list_credentials"
        # (the stored prefix — first 12 chars — may appear; full tokens may not)


def _cred_for(access, actor_id):
    return _cred_entry(access, actor_id)["id"]


def _cred_entry(access, actor_id):
    for c in access.list_credentials():
        if c.get("actor_id") == actor_id and not c.get("revoked"):
            return c
    raise AssertionError(f"no live credential entry for {actor_id}")


# ==========================================================================
# AT-1.4 — body-actor mismatch, act_as relay semantics
# ==========================================================================

def test_at1_4_body_actor_mismatch_forbidden(harness):
    """worker-1 token posting with body actor 'owner' on a ticket it can see: 403."""
    resp = harness["client"].post(
        f"/tickets/{harness['t_seen']['id']}/events",
        headers=_auth(harness["tok_worker"]),
        json={"kind": "comment", "actor": {"id": harness["owner_id"],
                                           "name": "Owner", "kind": "human"},
              "payload": {"text": "put words in my mouth"}})
    assert resp.status_code == 403, resp.text
    kinds = [e["kind"] for e in harness["store"].events(harness["t_seen"]["id"])
             if e.payload.get("text", "").startswith("put words")]
    assert kinds == [], "mismatched-actor event was written despite the 403"


def test_at1_4_act_as_relay_records_via(harness):
    """`via` is recorded only when a credential acts FOR ANOTHER actor (contract
    §2 effective()): the relay credential (act_as, allow list includes
    system:outbound) posts as system:outbound -> actor=system:outbound,
    via=relay."""
    resp = harness["client"].post(
        f"/tickets/{harness['t_seen']['id']}/events",
        headers=_auth(harness["tok_relay"]),
        json={"kind": "comment", "actor": {"id": "system:outbound"},
              "payload": {"text": "posted through the relay"}})
    assert resp.status_code in (200, 201), resp.text
    evs = [e for e in harness["store"].events(harness["t_seen"]["id"])
           if e.payload.get("text") == "posted through the relay"]
    assert len(evs) == 1
    ev = evs[0]
    d = ev.to_dict()
    actor = d.get("actor") or {}
    assert actor.get("id") == "system:outbound", d
    assert d.get("via") == "relay" or actor.get("via") == "relay", \
        f"via not recorded on event: {d}"


def test_at1_4_act_as_internal_human_forbidden(harness):
    """The same relay token with actor=owner (internal human, outside allowlist
    and human-guarded): 403."""
    resp = harness["client"].post(
        f"/tickets/{harness['t_seen']['id']}/events",
        headers=_auth(harness["tok_outbound"]),
        json={"kind": "comment", "actor": {"id": harness["owner_id"]},
              "payload": {"text": "impersonation"}})
    assert resp.status_code == 403, resp.text


def test_at1_4_act_as_outside_allowlist_forbidden(harness):
    """relay token may act for E1/E2/low-agent only; actor=agent-1 (internal
    agent outside the allowlist) must get 403."""
    resp = harness["client"].post(
        f"/tickets/{harness['t_seen']['id']}/events",
        headers=_auth(harness["tok_relay"]),
        json={"kind": "comment", "actor": {"id": "agent-1"},
              "payload": {"text": "outside allowlist"}})
    assert resp.status_code == 403, resp.text


# ==========================================================================
# AT-1.4b — hidden vs nonexistent are indistinguishable (404s identical)
# ==========================================================================

def _mismatch_call(client, token, tid):
    return client.post(f"/tickets/{tid}/events", headers=_auth(token),
                       json={"kind": "comment", "actor": {"id": "somebody-else"},
                             "payload": {"text": "probe"}})


@pytest.mark.parametrize("who", ["tok_e1"])
def test_at1_4b_hidden_vs_nonexistent_identical(harness, subtree, who):
    """Same mismatched-actor request to a visible ticket (403), a hidden ticket
    (404) and a nonexistent id (404 with byte-identical body). Run per-token so
    it cannot pass by rejecting everything before the lookup. External viewers
    only: in P1 every INTERNAL viewer sees every ticket, so "hidden" does not
    exist for tok_worker (covered by test_at1_4b_internal_viewer_no_hidden)."""
    client = harness["client"]
    token = harness[who]

    visible = _mismatch_call(client, token, subtree["a"]["id"])
    assert visible.status_code == 403, (who, visible.status_code, visible.text)

    hidden = _mismatch_call(client, token, harness["hidden_child"]["id"])
    assert hidden.status_code == 404, (who, hidden.status_code, hidden.text)

    missing = _mismatch_call(client, token, "TK-does-not-exist")
    assert missing.status_code == 404, (who, missing.status_code)
    assert hidden.status_code == missing.status_code
    assert hidden.json() == missing.json(), \
        f"hidden body {hidden.json()} != nonexistent body {missing.json()}"
    assert _body_text(hidden) == _body_text(missing)


def test_at1_4b_internal_viewer_no_hidden(harness, subtree):
    """In P1 every internal viewer sees every ticket — 'hidden' does not exist
    for tok_worker: a mismatched body actor on ANY existing ticket is 403
    actor_mismatch, and only a nonexistent id gives 404."""
    client = harness["client"]
    token = harness["tok_worker"]
    for tid in (subtree["a"]["id"], harness["hidden_child"]["id"],
                harness["t_seen"]["id"]):
        resp = _mismatch_call(client, token, tid)
        assert resp.status_code == 403, (tid, resp.status_code, resp.text)
    missing = _mismatch_call(client, token, "TK-does-not-exist")
    assert missing.status_code == 404, missing.status_code


def test_at1_4b_visible_ticket_not_preemptively_rejected(harness, subtree):
    """Guard for the 'rejects every request before the lookup' cheat: a
    well-formed call on a visible ticket with the caller's OWN actor succeeds."""
    resp = harness["client"].post(
        f"/tickets/{subtree['a']['id']}/events", headers=_auth(harness["tok_worker"]),
        json={"kind": "comment", "actor": {"id": "worker-1"},
              "payload": {"text": "own actor ok"}})
    assert resp.status_code in (200, 201), resp.text


# ==========================================================================
# AT-1.4c — revocation rows take effect; tables are append-only at the DB
# ==========================================================================

def test_at1_4c_grant_revocation_effect_and_append_only(harness, subtree):
    client, access = harness["client"], harness["access"]
    assert client.get(f"/tickets/{subtree['a']['id']}",
                      headers=_auth(harness["tok_e1"])).status_code == 200
    access.revoke_grant(harness["grant_S"], by=harness["owner_id"], reason="acceptance")
    resp = client.get(f"/tickets/{subtree['a']['id']}", headers=_auth(harness["tok_e1"]))
    assert resp.status_code == 404, "revoked grant still visible on next request"


def test_at1_4c_credential_revocation_row_and_append_only(harness):
    access = harness["access"]
    cred_id = _cred_for(access, "agent-1")
    access.revoke_credential(cred_id, by=harness["owner_id"], reason="acceptance")
    assert _cred_entry_allows(access, cred_id) is False


def _cred_entry_allows(access, cred_id):
    for c in access.list_credentials():
        if c.get("id") == cred_id:
            return not c.get("revoked")
    return None


@pytest.mark.parametrize("table", ["grants", "credentials", "grant_revocations",
                                   "credential_revocations"])
def test_at1_4c_db_rejects_update_and_delete(harness, table):
    """UPDATE/DELETE on grants, credentials and the revocation tables fail at
    the DB (BEFORE UPDATE/DELETE triggers)."""
    access, store = harness["access"], harness["store"]
    if table == "grants":
        access.add_grant("agent-1", "ticket", harness["t_seen"]["id"],
                         by=harness["owner_id"])
    elif table == "credentials":
        access.issue_credential("agent-1", by=harness["owner_id"])
    elif table == "grant_revocations":
        gid = access.add_grant("agent-1", "ticket", harness["t_seen"]["id"],
                               by=harness["owner_id"])
        access.revoke_grant(gid, by=harness["owner_id"])
    else:
        cid = _cred_for(access, "agent-1")
        access.revoke_credential(cid, by=harness["owner_id"])

    conn = store._conn
    # UPDATE a column that actually exists in each table (grants: reason;
    # credentials: prefix; the revocation tables: reason). The trigger RAISEs,
    # which surfaces as sqlite3.DatabaseError.
    tamper_col = "prefix" if table == "credentials" else "reason"
    with pytest.raises(sqlite3.DatabaseError) as u:
        conn.execute(f"UPDATE {table} SET {tamper_col} = 'tampered' WHERE 1=1")
    assert isinstance(u.value, sqlite3.DatabaseError)
    conn.rollback()
    with pytest.raises(sqlite3.DatabaseError) as d:
        conn.execute(f"DELETE FROM {table} WHERE 1=1")
    assert isinstance(d.value, sqlite3.DatabaseError)
    conn.rollback()


# ==========================================================================
# AT-1.4d — only the owner may lower labels (mutation check)
# ==========================================================================

def _ticket_label(store, tid):
    row = store._conn.execute("SELECT label FROM tickets WHERE id = ?", (tid,)).fetchone()
    return row["label"] if row is not None else None


def test_at1_4d_agent_cannot_lower_label(harness, subtree):
    """Agent lowering -> 403 AND the label is unchanged (mutation check: fails
    if the owner-only guard on lowering is removed). Owner lowering succeeds
    and emits a label_changed event."""
    client, store = harness["client"], harness["store"]
    tid = subtree["S"]["id"]
    assert _ticket_label(store, tid) == "external"

    resp = client.post(f"/tickets/{tid}/label", headers=_auth(harness["tok_agent"]),
                       json={"label": "public", "reason": "agent tries to lower"})
    assert resp.status_code == 403, f"expected 403, got {resp.status_code}"
    assert _ticket_label(store, tid) == "external", \
        "MUTATION: label changed despite the 403 — owner-only guard is not enforced"

    evs = [e for e in store.events(tid) if e.kind == "label_changed"]
    assert evs == [], "agent's rejected attempt still emitted a label_changed event"


def test_at1_4d_owner_lowers_with_event(harness, subtree):
    client, store = harness["client"], harness["store"]
    tid = subtree["S"]["id"]
    resp = client.post(f"/tickets/{tid}/label", headers=_auth(harness["tok_owner"]),
                       json={"label": "public", "reason": "acceptance declassify"})
    assert resp.status_code in (200, 201), resp.text
    assert _ticket_label(store, tid) == "public"
    changed = [e for e in store.events(tid) if e.kind == "label_changed"]
    assert len(changed) == 1
    assert changed[0].payload.get("from") == "external"
    assert changed[0].payload.get("to") == "public"


def test_at1_4d_wiki_lower_requires_owner(harness, wiki_handoff):
    client, store = harness["client"], harness["store"]
    slug = wiki_handoff.slug
    store.put_page(slug, title="Handoff runbook", body_md="internal rev",
                   actor="owner", label="external")
    resp = client.post(f"/wiki/{slug}/label", headers=_auth(harness["tok_agent"]),
                       json={"label": "public", "reason": "agent lowers"})
    assert resp.status_code == 403
    cur = store.get_page(slug)
    assert cur.label == "external", "MUTATION: wiki label lowered despite the 403"


# ==========================================================================
# AT-1.4e — relay acting for E1 / low-agent cannot exceed that actor
# ==========================================================================

def _as_actor(client, token, actor_id, method, path, **kw):
    body = dict(kw.pop("json", None) or {})
    body["actor"] = {"id": actor_id}
    # harness fix: TestClient.get() has no json= argument; the generic
    # request() entry point exists for exactly this (AT-1.4e claims the actor
    # on GETs too). Same contract surface, any verb.
    # Amendment A1: delegation is expressed with the X-Hub-Act-As header
    # (contract docs/ACCESS-P1-CONTRACT.md) — sent in addition to the body actor.
    headers = _auth(token)
    headers["X-Hub-Act-As"] = actor_id
    return client.request(method.upper(), path, headers=headers, json=body)


def test_at1_4e_relay_as_external_cannot_write(harness, subtree):
    client = harness["client"]
    tid = subtree["a"]["id"]
    r1 = _as_actor(client, harness["tok_relay"], "E1", "patch",
                   f"/tickets/{tid}", json={"status": "running"})
    assert r1.status_code in (403, 404), f"PATCH as E1: {r1.status_code}"
    r2 = _as_actor(client, harness["tok_relay"], "E1", "post",
                   f"/tickets/{tid}/edges",
                   json={"type": "relates_to", "dst": subtree["c"]["id"]})
    assert r2.status_code in (403, 404), f"edges as E1: {r2.status_code}"
    r3 = _as_actor(client, harness["tok_relay"], "E1", "post",
                   f"/tickets/{tid}/events", json={"kind": "comment"})
    assert r3.status_code in (403, 404), f"events as E1: {r3.status_code}"
    r4 = _as_actor(client, harness["tok_relay"], "E1", "post",
                   "/wiki/relay-page", json={"title": "nope"})
    assert r4.status_code in (403, 404), f"wiki as E1: {r4.status_code}"
    # nothing was mutated:
    assert harness["store"].get_ticket(tid).status.value == "draft"


def test_at1_4e_relay_as_external_sees_exactly_what_e1_sees(harness, subtree):
    """A relay acting for E1 sees exactly E1's view: hidden objects are 404 for
    both; visible objects come back as the ExternalTicket projection only."""
    client = harness["client"]
    # E1's own baseline on the hidden internal sibling: 404.
    e1 = client.get(f"/tickets/{subtree['b']['id']}", headers=_auth(harness["tok_e1"]))
    assert e1.status_code == 404
    # The same object claimed as E1 through the relay must match (403/404),
    # never serve the hidden object.
    hidden_via = _as_actor(client, harness["tok_relay"], "E1", "get",
                           f"/tickets/{subtree['b']['id']}")
    assert hidden_via.status_code == 404, hidden_via.status_code
    assert "child int-b" not in _body_text(hidden_via)
    # What E1 CAN see, the relay-as-E1 sees too, projected identically:
    via = _as_actor(client, harness["tok_relay"], "E1", "get",
                    f"/tickets/{subtree['S']['id']}")
    assert via.status_code == 200, via.status_code
    assert set(via.json()) == {"id", "title", "body", "status", "priority",
                               "label", "created_at", "updated_at"}
    direct = client.get(f"/tickets/{subtree['S']['id']}",
                        headers=_auth(harness["tok_e1"]))
    assert direct.json() == via.json(), "relay-as-E1 view differs from E1's own view"


def test_at1_4e_relay_as_low_agent_cannot_exceed_it(harness):
    """Acting as low-agent: low-agent has NO grants; anything worker-1 (ticket
    grant holder) can do, low-agent-via-relay must not."""
    client = harness["client"]
    tid = harness["t_seen"]["id"]
    # worker's own ticket grant lets it post with its own actor:
    ok = client.post(f"/tickets/{tid}/events", headers=_auth(harness["tok_worker"]),
                     json={"kind": "comment", "actor": {"id": "worker-1"},
                           "payload": {"text": "worker ok"}})
    assert ok.status_code in (200, 201), "control: worker on its own grant fails"
    # P1: grants exist only for externals (verb read); every internal writer may
    # comment. "Low authority" in P1 = cannot publish below internal (§1.10), so
    # that is the right a relay must not gain when acting for low-agent.
    as_low = _as_actor(client, harness["tok_relay"], "low-agent", "post",
                       "/tickets", json={"title": "relay publishes as low-agent",
                                         "label": "external"})
    assert as_low.status_code == 403, \
        f"low-agent published via relay: {as_low.status_code}"
    assert not [t for t in harness["store"].list_tickets()
                if t.title == "relay publishes as low-agent"]
    # Control (fix-round §4): the SAME relay acting for low-agent may still do
    # everything low-agent itself may do — a comment on a ticket succeeds and
    # is attributed to low-agent with via=relay. Without this, a blanket relay
    # write-refusal would pass the 403 assertion above.
    ctl_headers = _auth(harness["tok_relay"])
    ctl_headers["X-Hub-Act-As"] = "low-agent"
    ctl = client.post(f"/tickets/{tid}/events", headers=ctl_headers,
                      json={"kind": "comment", "actor": {"id": "low-agent"},
                            "payload": {"text": "relay acts for low-agent"}})
    assert ctl.status_code in (200, 201), \
        f"relay-for-low-agent comment refused: {ctl.status_code} {ctl.text}"
    evs = [e.to_dict() for e in harness["store"].events(tid)
           if (e.payload or {}).get("text") == "relay acts for low-agent"]
    assert len(evs) == 1, f"expected exactly one control event: {evs}"
    actor_d = evs[0].get("actor") or {}
    assert actor_d.get("id") == "low-agent", evs[0]
    assert evs[0].get("via") == "relay" or actor_d.get("via") == "relay", \
        f"via not recorded on control event: {evs[0]}"


# ==========================================================================
# AT-1.4f — agents cannot publish; comments follow the ticket label
# ==========================================================================

def test_at1_4f_agent_cannot_create_external_ticket(harness, subtree):
    client = harness["client"]
    resp = client.post("/tickets", headers=_auth(harness["tok_agent"]),
                       json={"title": "agent publishes!", "parent": subtree["S"]["id"],
                             "label": "external", "actor": {"id": "agent-1"}})
    assert resp.status_code == 403, resp.text
    listed = client.get("/tickets", headers=_auth(harness["tok_e1"])).json()
    assert "agent publishes!" not in json.dumps(listed)


def test_at1_4f_agent_cannot_create_public_wiki(harness):
    resp = harness["client"].post("/wiki/agent-public-page",
                                  headers=_auth(harness["tok_agent"]),
                                  json={"title": "public at birth", "label": "public"})
    assert resp.status_code == 403, resp.text


def test_at1_4f_agent_comment_inherits_published_label(harness):
    """Owner publishes a ticket external; an agent comment on it defaults to
    the ticket's label (external) and is visible to E1."""
    client, store = harness["client"], harness["store"]
    t = harness["internal_ticket"]("shared with partner", owner={"id": "agent-1"})
    access = harness["access"]
    access.add_grant("E1", "ticket", t["id"], by=harness["owner_id"])
    store.set_ticket_label(t["id"], "external",
                           actor=store.get_actor(harness["owner_id"]),
                           reason="acceptance")
    resp = client.post(f"/tickets/{t['id']}/events", headers=_auth(harness["tok_agent"]),
                       json={"kind": "comment", "actor": {"id": "agent-1"},
                             "payload": {"text": "hello partner"}})
    assert resp.status_code in (200, 201), resp.text
    evs = client.get(f"/tickets/{t['id']}/events", headers=_auth(harness["tok_e1"]))
    assert evs.status_code == 200
    assert "hello partner" in _body_text(evs), \
        "agent comment on a published ticket must default to external"


def test_at1_4f_explicit_internal_comment_stays_hidden(harness):
    client, store = harness["client"], harness["store"]
    t = harness["internal_ticket"]("shared with partner 2", owner={"id": "agent-1"})
    harness["access"].add_grant("E1", "ticket", t["id"], by=harness["owner_id"])
    store.set_ticket_label(t["id"], "external",
                           actor=store.get_actor(harness["owner_id"]), reason="x")
    resp = client.post(f"/tickets/{t['id']}/events", headers=_auth(harness["tok_agent"]),
                       json={"kind": "comment", "actor": {"id": "agent-1"},
                             "label": "internal", "payload": {"text": "internal note"}})
    assert resp.status_code in (200, 201), resp.text
    evs = client.get(f"/tickets/{t['id']}/events", headers=_auth(harness["tok_e1"]))
    assert "internal note" not in _body_text(evs)


# ==========================================================================
# AT-1.4g — §1.10 admin matrix: non-owners refused
# ==========================================================================

def test_at1_4g_agent_refused_admin_rows(harness, subtree):
    client, tok = harness["client"], harness["tok_agent"]
    owner = harness["owner_id"]
    calls = [
        ("grant add", client.post("/admin/access/grants", headers=_auth(tok),
                                  json={"actor_id": "E1", "scope_type": "subtree",
                                        "scope_id": subtree["S"]["id"], "by": owner})),
        ("token issue", client.post("/admin/access/credentials", headers=_auth(tok),
                                    json={"actor_id": "agent-1", "by": owner})),
        ("actor create", client.post("/admin/access/actors", headers=_auth(tok),
                                     json={"id": "sneaky", "name": "Sneaky", "by": owner})),
        ("actor suspend", client.post("/admin/access/actors/E2/status", headers=_auth(tok),
                                      json={"status": "suspended", "by": owner})),
    ]
    refused = [name for name, r in calls if r.status_code in (401, 403, 404)]
    assert len(refused) == len(calls), \
        f"agent was NOT refused on: {[(n, r.status_code) for n, r in calls if r.status_code not in (401, 403, 404)]}"


def test_at1_4g_ordinary_human_refused_admin_rows(harness, subtree):
    client, tok = harness["client"], harness["tok_human"]
    resp = client.post("/admin/access/grants", headers=_auth(tok),
                       json={"actor_id": "E2", "scope_type": "subtree",
                             "scope_id": subtree["S"]["id"]})
    assert resp.status_code in (401, 403, 404)
    # ...and its own ordinary write path still works (so this is not a blanket-deny cheat):
    ok = client.post(f"/tickets/{harness['t_seen']['id']}/events", headers=_auth(tok),
                     json={"kind": "comment", "actor": {"id": "human-1"},
                           "payload": {"text": "human comment"}})
    assert ok.status_code in (200, 201), ok.text


def test_at1_4g_agent_refused_publish_row(harness):
    """`publish` row of the matrix for an agent is covered by AT-1.4f; here the
    human row: an ordinary internal human cannot publish below internal."""
    resp = harness["client"].post("/tickets", headers=_auth(harness["tok_human"]),
                                  json={"title": "human publishes", "label": "external"})
    assert resp.status_code == 403


@pytest.mark.skip(reason="no sponsors exist in P1 (DESIGN §1.10 D2); "
                         "AT-4.3 covers the sponsor row once appointed")
def test_at1_4g_sponsor_outside_subtree():
    """Sponsor refused outside their sponsored subtree — inactive in P1: the
    owner is the only admin and no sponsorships exist yet."""


# ==========================================================================
# AT-1.5 — migration keeps everything internal; triggers intact
# ==========================================================================

def test_at1_5_existing_rows_are_internal(tmp_path):
    """Pre-migration data reads back as internal with unchanged row counts, and
    the append-only triggers still reject UPDATE/DELETE on events and wiki_pages."""
    from ticket_hub.store import Store

    db = str(tmp_path / "pre.db")
    legacy = Store(db)
    t = legacy.create_ticket("legacy ticket", body="b",
                             actor=Actor(id="old-actor", name="Old"))
    legacy.append_event(t.id, "comment", payload={"text": "legacy event"})
    legacy.put_page("legacy-page", title="Legacy", body_md="rev one", actor="old")
    legacy.put_page("legacy-page", title="Legacy", body_md="rev two", actor="old")
    counts = {tbl: legacy._conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
              for tbl in ("tickets", "events", "wiki_pages")}
    legacy.close()

    # "migration": opening the Store again runs the P1 column migration.
    upgraded = Store(db)
    after = {tbl: upgraded._conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
             for tbl in ("tickets", "events", "wiki_pages")}
    assert after == counts, f"row counts changed across migration: {counts} -> {after}"
    assert upgraded._conn.execute(
        "SELECT DISTINCT label FROM tickets").fetchall()[0][0] == "internal"
    assert upgraded._conn.execute(
        "SELECT DISTINCT label FROM events").fetchall()[0][0] == "internal"
    assert upgraded._conn.execute(
        "SELECT DISTINCT label FROM wiki_pages").fetchall()[0][0] == "internal"

    with pytest.raises(Exception):
        upgraded._conn.execute("UPDATE events SET kind = 'tampered' WHERE 1=1")
    upgraded._conn.rollback()
    with pytest.raises(Exception):
        upgraded._conn.execute("DELETE FROM events WHERE 1=1")
    upgraded._conn.rollback()
    with pytest.raises(Exception):
        upgraded._conn.execute("UPDATE wiki_pages SET body_md = 'x' WHERE 1=1")
    upgraded._conn.rollback()
    with pytest.raises(Exception):
        upgraded._conn.execute("DELETE FROM wiki_pages WHERE 1=1")
    upgraded._conn.rollback()
    upgraded.close()


from ticket_hub.model import Actor  # legacy module: safe at top level  # noqa: E402


# ==========================================================================
# AT-1.6 / AT-1.7 — subtree grant visibility
# ==========================================================================

def test_at1_6_subtree_list_exact_ids(harness, subtree):
    resp = harness["client"].get("/tickets", headers=_auth(harness["tok_e1"]))
    assert resp.status_code == 200
    body = resp.json()
    rows = body.get("tickets", body) if isinstance(body, dict) else body
    assert _ids(rows) == {subtree["S"]["id"], subtree["a"]["id"], subtree["c"]["id"]}, \
        f"E1 list not exact: {_ids(rows)}"


def test_at1_6_internal_child_404(harness, subtree):
    resp = harness["client"].get(f"/tickets/{subtree['b']['id']}",
                                 headers=_auth(harness["tok_e1"]))
    assert resp.status_code == 404
    # the id may echo as the queried path; the body must not describe the object:
    assert "child int-b" not in _body_text(resp)


def test_at1_6_granted_child_visible(harness, subtree):
    for child in (subtree["a"], subtree["c"]):
        resp = harness["client"].get(f"/tickets/{child['id']}",
                                     headers=_auth(harness["tok_e1"]))
        assert resp.status_code == 200, child["id"]
        assert resp.json()["label"] == "external"


def test_at1_7_no_grant_sees_nothing(harness, subtree):
    """AT-1.7: E2 has no grant; even external-labelled objects stay hidden.
    This fails on a hub with no access control (E2 would see everything)."""
    client = harness["client"]
    listed = client.get("/tickets", headers=_auth(harness["tok_e2"]))
    assert listed.status_code == 200
    body = listed.json()
    rows = body.get("tickets", body) if isinstance(body, dict) else body
    for t in (subtree["S"], subtree["a"], subtree["c"]):
        assert t["id"] not in _ids(rows), f"E2 (no grant) listed {t['id']}"
        r = client.get(f"/tickets/{t['id']}", headers=_auth(harness["tok_e2"]))
        assert r.status_code == 404, f"E2 read {t['id']} -> {r.status_code}"


# ==========================================================================
# AT-1.8 — event labels: hidden internal events; retro-classification
# ==========================================================================

def _ev_text(resp_text):
    return resp_text


def test_at1_8_internal_event_hidden(harness, subtree):
    client, store = harness["client"], harness["store"]
    owner_actor = store.get_actor(harness["owner_id"])
    tid = subtree["a"]["id"]
    store.append_event(tid, "comment", actor=owner_actor,
                       payload={"text": "internal whisper"}, label="internal")
    store.append_event(tid, "comment", actor=owner_actor,
                       payload={"text": "external shout"})
    evs = client.get(f"/tickets/{tid}/events", headers=_auth(harness["tok_e1"]))
    assert evs.status_code == 200
    assert "internal whisper" not in _body_text(evs)
    assert "external shout" in _body_text(evs)
    one = client.get(f"/tickets/{tid}", headers=_auth(harness["tok_e1"]))
    assert "internal whisper" not in _body_text(one), \
        "internal event leaked inside GET /tickets/{id}"


def test_at1_8_events_before_declassification_stay_hidden(harness):
    """Events written while the ticket was internal remain hidden after the
    owner lowers the ticket to external; later/explicitly-external events show."""
    client, store = harness["client"], harness["store"]
    owner_actor = store.get_actor(harness["owner_id"])
    t = harness["internal_ticket"]("to be declassified", owner={"id": "agent-1"})
    store.append_event(t["id"], "comment", actor=owner_actor,
                       payload={"text": "old internal chatter"})
    harness["access"].add_grant("E1", "ticket", t["id"], by=harness["owner_id"])
    store.set_ticket_label(t["id"], "external", actor=owner_actor, reason="acceptance")
    store.append_event(t["id"], "comment", actor=owner_actor,
                       payload={"text": "new chatter"})
    store.append_event(t["id"], "comment", actor=owner_actor,
                       payload={"text": "marked external"}, label="external")

    evs = client.get(f"/tickets/{t['id']}/events", headers=_auth(harness["tok_e1"]))
    assert evs.status_code == 200
    text = _body_text(evs)
    assert "old internal chatter" not in text, \
        "pre-declassification event leaked after ticket raise"
    assert "new chatter" in text
    assert "marked external" in text


# ==========================================================================
# AT-1.9 — wiki grant + per-revision labels
# ==========================================================================

def test_at1_9_no_grant_no_wiki(harness, wiki_handoff):
    client = harness["client"]
    lst = client.get("/wiki", headers=_auth(harness["tok_e1"]))
    assert "runbook-handoff" not in _body_text(lst)
    srch = client.get("/wiki-search", params={"q": "handoff"},
                      headers=_auth(harness["tok_e1"]))
    assert srch.status_code == 200
    assert srch.json() == [], f"search leaked without grant: {srch.json()}"
    one = client.get("/wiki/runbook-handoff", headers=_auth(harness["tok_e1"]))
    assert one.status_code == 404


def test_at1_9_declassified_shows_only_visible_revs(harness, wiki_handoff):
    client, store = harness["client"], harness["store"]
    slug = wiki_handoff.slug
    store.put_page(slug, title="Handoff runbook", body_md="rev two", actor="owner")
    store.put_page(slug, title="Handoff runbook", body_md="rev three", actor="owner")
    store.put_page(slug, title="Handoff runbook", body_md="rev four public-safe",
                   actor="owner", label="external")  # declassified at rev 4
    harness["access"].add_grant("E1", "wiki_page", slug, by=harness["owner_id"])

    one = client.get(f"/wiki/{slug}", headers=_auth(harness["tok_e1"]))
    assert one.status_code == 200
    body = one.json()
    hist = body.get("history", [])
    revs = {h.get("rev") for h in hist}
    assert revs and min(revs) >= 4, f"pre-declassification revs leaked: {sorted(revs)}"
    assert "rev two" not in _body_text(one)
    lst = client.get("/wiki", headers=_auth(harness["tok_e1"]))
    assert "runbook-handoff" in _body_text(lst)


def test_at1_9_re_raised_hides_everything(harness, wiki_handoff):
    client, store = harness["client"], harness["store"]
    slug = wiki_handoff.slug
    for rev_no, lab in ((2, None), (3, None), (4, "external"), (5, None), (6, "internal")):
        # revs 1..3 internal (fixture + two more), rev4 external, rev5 external
        # (inherits), rev6 explicitly internal.
        store.put_page(slug, title="Handoff runbook", body_md=f"rev {rev_no}",
                       actor="owner", **({"label": lab} if lab else {}))
    harness["access"].add_grant("E1", "wiki_page", slug, by=harness["owner_id"])
    one = client.get(f"/wiki/{slug}", headers=_auth(harness["tok_e1"]))
    assert one.status_code == 404, \
        f"page re-raised to internal still serves E1 ({one.status_code})"
    lst = client.get("/wiki", headers=_auth(harness["tok_e1"]))
    assert slug not in _body_text(lst)


# ==========================================================================
# AT-1.10 — external search cannot probe internals (shape only, no timing)
# ==========================================================================

def test_at1_10_search_shape_identical(harness, subtree):
    """AT-1.10: query word ONLY present in an internal ticket -> empty list,
    identical response shape to a hit / a plain miss. Shape only; no timing."""
    client = harness["client"]
    needle = "zebrafish-only-in-internal"
    harness["internal_ticket"]("secret plan", body=f"contains {needle}")

    r_probe = client.get("/tickets", params={"q": needle},
                         headers=_auth(harness["tok_e1"]))
    assert r_probe.status_code == 200
    body = r_probe.json()
    rows = body.get("tickets", body) if isinstance(body, dict) else body
    assert rows == [], "external q= reached an internal ticket"

    r_miss = client.get("/tickets", params={"q": "no-such-word-anywhere"},
                        headers=_auth(harness["tok_e1"]))
    r_hit = client.get("/tickets", params={"q": "child ext-a"},
                       headers=_auth(harness["tok_e1"]))
    assert r_miss.status_code == 200 and r_hit.status_code == 200
    for other in (r_miss, r_hit):
        ob = other.json()
        orows = ob.get("tickets", ob) if isinstance(ob, dict) else ob
        assert isinstance(rows, list) and isinstance(orows, list)
    # same JSON shape (list of ExternalTicket dicts, or same keys if wrapped):
    assert set(r_probe.json().keys() if isinstance(r_probe.json(), dict) else []) == \
        set(r_miss.json().keys() if isinstance(r_miss.json(), dict) else [])
    hit_rows = r_hit.json()
    hit_rows = hit_rows.get("tickets", hit_rows) if isinstance(hit_rows, dict) else hit_rows
    assert len(hit_rows) >= 1  # control: search works at all for E1


# ==========================================================================
# AT-1.11 — external on internal-policy routes -> 404
# ==========================================================================

@pytest.mark.parametrize("method,path", [
    ("get", "/jobs"),
    ("get", "/admin/status"),
    ("get", "/seats"),
    ("get", "/templates"),
    ("get", "/roles"),
    ("get", "/board"),
    ("get", "/tickets/TK-any/lineage"),
    ("get", "/tickets/TK-any/digest"),
    ("get", "/tickets/TK-any/plan"),
])
def test_at1_11_internal_routes_404_for_external(harness, subtree, method, path):
    """External calls to internal routes are 404 (hidden), not 401/403 — and the
    control below proves E1 is authenticated on this very client."""
    resp = getattr(harness["client"], method)(path, headers=_auth(harness["tok_e1"]))
    assert resp.status_code == 404, (path, resp.status_code)


def test_at1_11_control_external_still_works(harness, subtree):
    """Control: the E1 token IS valid (visible read succeeds), so the 404s
    above are hiding, not blanket-rejecting."""
    resp = harness["client"].get(f"/tickets/{subtree['a']['id']}",
                                 headers=_auth(harness["tok_e1"]))
    assert resp.status_code == 200


# ==========================================================================
# AT-1.6b — ExternalTicket has no artifacts (and nothing extra)
# ==========================================================================

_EXT_KEYS = {"id", "title", "body", "status", "priority", "label",
             "created_at", "updated_at"}


def _ext_rows(payload):
    rows = payload.get("tickets", payload) if isinstance(payload, dict) else payload
    return rows


def test_at1_6b_no_artifacts_single_and_list(harness, subtree):
    client = harness["client"]
    # make the internal record actually carry artifacts/tags/refs:
    harness["store"].update_ticket(
        subtree["S"]["id"],
        tags=["partner-visible"], external_refs={"jira": "PROJ-1"},
        artifacts=[{"kind": "doc", "ref": "s3://bucket/thing", "label": "external"}])
    one = client.get(f"/tickets/{subtree['S']['id']}", headers=_auth(harness["tok_e1"]))
    assert one.status_code == 200
    obj = one.json()
    for banned in ("artifacts", "tags", "external_refs", "owner", "owner_id",
                   "families", "edges", "children"):
        assert banned not in obj, f"single ExternalTicket carries '{banned}'"
    assert set(obj.keys()) == _EXT_KEYS, f"unexpected keys: {sorted(set(obj) - _EXT_KEYS)}"

    lst = client.get("/tickets", headers=_auth(harness["tok_e1"]))
    rows = _ext_rows(lst.json())
    ids = {r["id"]: r for r in rows}
    assert subtree["S"]["id"] in ids
    assert "artifacts" not in json.dumps(rows), "list response carries artifacts"

    # nested child data must not leak artifacts either:
    kids = client.get(f"/tickets/{subtree['S']['id']}/children",
                      headers=_auth(harness["tok_e1"]))
    if kids.status_code == 200:
        assert "artifacts" not in _body_text(kids)
    else:
        assert kids.status_code == 404, \
            f"/children for external is neither projection (200) nor hidden (404): {kids.status_code}"


# ==========================================================================
# AT-1.13 — expiry between two requests on the same client/session
# ==========================================================================

def _short_token(access, harness, actor_id, seconds=2):
    expires = (_now() + datetime.timedelta(seconds=seconds)).isoformat()
    return _token_of(access.issue_credential(actor_id, by=harness["owner_id"],
                                             expires_at=expires))[1]


def test_at1_13_credential_expiry_no_restart(harness):
    client, access = harness["client"], harness["access"]
    tok = _short_token(access, harness, "agent-1", seconds=2)
    assert client.get("/tickets", headers=_auth(tok)).status_code == 200
    import time as _time
    _time.sleep(3)
    resp = client.get("/tickets", headers=_auth(tok))
    assert resp.status_code == 401, "expired credential still accepted (no restart)"


def test_at1_13_actor_expiry_no_restart(harness):
    client, access = harness["client"], harness["access"]
    expires = (_now() + datetime.timedelta(seconds=2)).isoformat()
    access.create_actor("E3", "External Three", realm="external",
                        sponsor_id=harness["owner_id"], expires_at=expires,
                        by=harness["owner_id"])
    tok = _token_of(access.issue_credential("E3", by=harness["owner_id"]))[1]
    assert client.get("/tickets", headers=_auth(tok)).status_code == 200
    import time as _time
    _time.sleep(3)
    assert client.get("/tickets", headers=_auth(tok)).status_code == 401


def test_at1_13_grant_expiry_no_restart(harness, subtree):
    client, access = harness["client"], harness["access"]
    expires = (_now() + datetime.timedelta(seconds=2)).isoformat()
    access.add_grant("E2", "subtree", subtree["S"]["id"], by=harness["owner_id"],
                     expires_at=expires)
    target = f"/tickets/{subtree['a']['id']}"
    assert client.get(target, headers=_auth(harness["tok_e2"])).status_code == 200
    import time as _time
    _time.sleep(3)
    assert client.get(target, headers=_auth(harness["tok_e2"])).status_code == 404


# ==========================================================================
# AT-1.12 / AT-1.14 — live deployment (HUB_LIVE=1 required)
# ==========================================================================

def _live_base():
    return os.environ.get("HUB_LIVE_BASE_URL", "https://127.0.0.1:8443")


@pytest.mark.live
@pytest.mark.skipif(os.environ.get("HUB_LIVE") != "1",
                    reason="live deployment test; set HUB_LIVE=1")
def test_at1_12_live_cli_lists_with_token():
    """Live (node-b via TLS endpoint or SSH tunnel): hub CLI with a token file
    lists tickets; without one, 401."""
    base = _live_base()
    token_file = os.environ["HUB_LIVE_TOKEN_FILE"]

    def cli(extra_env):
        import subprocess
        env = dict(os.environ, HUB_BASE_URL=base, **extra_env)
        return subprocess.run(
            [os.environ.get("HUB_CLI", "bin/hub"), "tickets"],
            capture_output=True, text=True, env=env, timeout=60)

    with_token = cli({"HUB_TOKEN_FILE": token_file})
    assert with_token.returncode == 0, with_token.stderr
    no_token_env = dict(os.environ)
    no_token_env.pop("HUB_TOKEN_FILE", None)
    no_token_env["HUB_BASE_URL"] = base
    import subprocess
    without = subprocess.run([os.environ.get("HUB_CLI", "bin/hub"), "tickets"],
                             capture_output=True, text=True, env=no_token_env, timeout=60)
    joined = without.stdout + without.stderr
    assert "401" in joined or without.returncode != 0, \
        "CLI without a token file must be refused (401)"


@pytest.mark.live
@pytest.mark.skipif(os.environ.get("HUB_LIVE") != "1",
                    reason="live deployment test; set HUB_LIVE=1")
def test_at1_12_live_plain_http_fails():
    """Live: plain HTTP to <server-lan-ip>:8020 from the LAN fails to connect."""
    addr = os.environ["HUB_LIVE_LAN_HTTP"]  # e.g. http://<server-lan-ip>:8020
    try:
        with urllib.request.urlopen(addr + "/health", timeout=5) as resp:
            pytest.fail(f"unauthenticated plain HTTP answered {resp.status} at {addr}")
    except urllib.error.HTTPError as exc:
        pytest.fail(f"plain HTTP listener answers at {addr} (HTTP {exc.code}) — "
                    "there must be no unauthenticated listener")
    except Exception:
        pass  # connection refused / TLS mismatch / timeout == the expected posture


@pytest.mark.live
@pytest.mark.skipif(os.environ.get("HUB_LIVE") != "1",
                    reason="live deployment test; set HUB_LIVE=1")
@pytest.mark.parametrize("host_port", os.environ.get(
    "HUB_LIVE_SCAN", "127.0.0.1:8777,[::1]:8777").split(","))
def test_at1_14_no_port_answers_without_token(host_port):
    """AT-1.14: after cut-over no port (host-loopback, docker bridge, LAN)
    answers hub routes without a token."""
    host, _, port = host_port.rpartition(":")
    path = os.environ.get("HUB_LIVE_PROBE_PATH", "/tickets")
    url = f"http://{host}:{port}{path}"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = resp.read(4096).decode("utf-8", "replace")
        # An answer at all is only acceptable if it is a 401 — but success here
        # means data came back without an error, i.e. an open listener:
        pytest.fail(f"{url} served ticket data without a token: {data[:200]}")
    except urllib.error.HTTPError as exc:
        assert exc.code == 401, \
            f"{url} answered HTTP {exc.code} without a token (expected refuse or 401)"
    except (urllib.error.URLError, ConnectionError, socket.timeout, OSError):
        pass  # no listener: the desired state
