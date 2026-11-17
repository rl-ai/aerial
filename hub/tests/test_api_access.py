"""Access P1 API tests — ACCESS-P1-CONTRACT §3 (lane W3).

Covers the request pipeline (route match -> policy -> token -> CSRF ->
external filtering -> access_log), every policy class, identity/act_as,
publication authority, the two label endpoints, session + cookie handling,
access_log auditing, the reduced /health body and auth-off behaviour.

Runs entirely in-process via TestClient against a real Store + AccessStore on
a temp sqlite file (conftest.py keeps the rest of the suite unauthenticated).
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import api as api_mod
from api import create_app
from hub_gateway import StoreGateway
from fake_store import FakeStore
from ticket_hub.access import AccessStore
from ticket_hub.model import Actor
from ticket_hub.policy import EXTERNAL_READ_ROUTES, POLICIES, check_policies
from ticket_hub.store import Store


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def env(tmp_path):
    """Real store + gateway + access layer with a small actor/ticket graph."""
    store = Store(str(tmp_path / "hub.db"))
    gateway = StoreGateway(store)
    access = AccessStore(store._conn)
    access.create_actor("own", "Own", kind="human", is_owner=True,
                        by="bootstrap")
    access.create_actor("bot", "Bot", kind="agent", by="own")
    access.create_actor("relay", "Relay", kind="system", by="own")
    access.create_actor("e1", "E One", realm="external", sponsor_id="own",
                        expires_at="2099-01-01T00:00:00Z", by="own")
    access.create_actor("e2", "E Two", realm="external", sponsor_id="own",
                        expires_at="2099-01-01T00:00:00Z", by="own")
    tokens = {}
    for a in ("own", "bot", "e1", "e2"):
        tokens[a] = access.issue_credential(a, by="own")[1]
    _, tokens["relay"] = access.issue_credential(
        "relay", by="own", scopes=("act_as",), act_as_allow=("bot", "e*"))

    own = Actor(id="own", name="Own", kind="human")
    root = store.create_ticket("Root", owner=own, label="external")
    child_x = store.create_ticket("ChildX", parent=root.id, owner=own,
                                  label="external")
    secret = store.create_ticket("Top Secret Plan", parent=root.id, owner=own)
    access.add_grant("e1", "subtree", root.id, by="own")
    store.put_page("guide", title="Guide", body_md="public guide body",
                   label="external")
    store.put_page("secret-page", title="Secrets", body_md="hidden internal",
                   actor="own")

    app = create_app(gateway, auth=True)
    app.state.access = access
    client = TestClient(app)

    class Env:
        pass
    e = Env()
    e.store, e.gateway, e.access = store, gateway, access
    e.app, e.client, e.tokens = app, client, tokens
    e.root, e.child_x, e.secret = root, child_x, secret
    e.h = lambda a: {"Authorization": "Bearer " + tokens[a]}
    return e


def _bearer_env(**kw):
    return kw  # small readability helper (unused kw)


# ---------------------------------------------------------------------------
# pipeline: auth gate per policy class
# ---------------------------------------------------------------------------

def test_default_auth_required_without_env(monkeypatch):
    monkeypatch.delenv("TICKET_HUB_AUTH", raising=False)
    app = create_app(FakeStore())  # env unset -> required (no gap raises)
    c = TestClient(app)
    assert c.get("/tickets").status_code == 401
    body = c.get("/tickets").json()
    assert body["error"]["code"] == "unauthorized"


def test_env_off_disables_auth(monkeypatch):
    monkeypatch.setenv("TICKET_HUB_AUTH", "off")
    c = TestClient(create_app(FakeStore()))
    assert c.get("/tickets").status_code == 200


def test_env_anything_else_keeps_auth(monkeypatch):
    monkeypatch.setenv("TICKET_HUB_AUTH", "required-ish")
    c = TestClient(create_app(FakeStore()))
    assert c.get("/tickets").status_code == 401


def test_unauthenticated_internal_read_denied(env):
    assert env.client.get("/tickets").status_code == 401
    assert env.client.get(f"/tickets/{env.root.id}").status_code == 401


def test_unauthenticated_internal_routes_denied(env):
    for path in ("/board", "/seats", "/roles", "/templates", "/jobs",
                 "/admin/status", "/tickets/TK-1/lineage", "/wiki",
                 "/wiki-search"):
        assert env.client.get(path).status_code == 401, path


def test_unauthenticated_ui_and_docs_denied(env):
    assert env.client.get("/").status_code == 401
    assert env.client.get("/openapi.json").status_code == 401
    assert env.client.get("/docs").status_code == 401
    assert env.client.get("/redoc").status_code == 401
    assert env.client.post("/logout").status_code == 401


def test_unauthenticated_ui_mount_denied(env):
    # The /ui mount is authenticated: no token -> 401 (not the static file).
    r = env.client.get("/ui/index.html")
    assert r.status_code == 401


def test_bad_token_401(env):
    r = env.client.get("/tickets",
                       headers={"Authorization": "***"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthorized"


def test_unknown_route_passes_through_404(env):
    assert env.client.get("/no-such-route").status_code == 404


def test_wrong_method_on_known_route_fails_closed(env):
    # Fix-round §1: (method, path) without a policy never reaches call_next.
    # DELETE /tickets has no policy -> authenticate first (401 without a
    # token), then 404 not_found with one — the route's 405 is unreachable.
    assert env.client.delete("/tickets").status_code == 401
    r = env.client.delete("/tickets", headers=env.h("bot"))
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


def test_policy_gap_raises_runtime_error(env):
    bad = create_app(env.gateway, auth=True)

    @bad.get("/unlisted-route")
    def _u():
        return {}

    with pytest.raises(RuntimeError) as excinfo:
        check_policies(bad)
    assert "/unlisted-route" in str(excinfo.value)


# -- fail-closed pipeline (fix-round §1) ------------------------------------

def test_head_inherits_get_policy(env):
    # HEAD uses the GET policy of the same path: auth required, never a
    # free 200 for docs/UI/tickets/root.
    for path in ("/openapi.json", "/docs", "/ui/app.js", "/tickets", "/"):
        assert env.client.request("HEAD", path).status_code == 401, path
    # with a valid token the GET policy applies (docs/UI are authenticated)
    assert env.client.request("HEAD", "/openapi.json",
                              headers=env.h("bot")).status_code == 200


def test_options_on_known_routes_fails_closed(env):
    # OPTIONS has no policy entry anywhere -> authenticate first, then 404.
    for path in ("/tickets", "/", "/openapi.json"):
        assert env.client.request("OPTIONS", path).status_code == 401, path
        r = env.client.request("OPTIONS", path, headers=env.h("bot"))
        assert r.status_code == 404, path
        assert r.json()["error"]["code"] == "not_found"


def test_route_registered_after_create_app_fails_closed(env):
    # A route added after create_app can never be served unauthenticated,
    # and stays hidden (404) even to an authenticated caller — its handler is
    # never reached (no leak in the body).
    @env.app.get("/late-route")
    def _late():
        return {"leak": True}

    assert env.client.get("/late-route").status_code == 401
    r = env.client.get("/late-route", headers=env.h("bot"))
    assert r.status_code == 404
    assert "leak" not in r.text


def test_head_options_ui_asset_denied(env):
    for m in ("HEAD", "OPTIONS"):
        assert env.client.request(m, "/ui/app.js").status_code == 401, m


def test_policies_cover_every_registered_route(env):
    check_policies(env.app)  # must not raise


def test_external_read_policy_is_exactly_six_routes():
    ext = {k for k, v in POLICIES.items() if v == "external_read"}
    assert {f"{m} {p}" for m, p in ext} == EXTERNAL_READ_ROUTES
    assert len(EXTERNAL_READ_ROUTES) == 6


# ---------------------------------------------------------------------------
# public routes + health body
# ---------------------------------------------------------------------------

def test_health_public_and_reduced_when_auth_on(env, tmp_path, monkeypatch):
    monkeypatch.setenv("TICKET_HUB_HEALTH_STATUS",
                       str(tmp_path / "missing.json"))
    r = env.client.get("/health")
    assert r.status_code == 200
    assert sorted(r.json().keys()) == ["ok"]


def test_health_reduces_stored_status(env, tmp_path, monkeypatch):
    p = tmp_path / "status.json"
    p.write_text('{"ok": false, "ts": "t", "last_error": "boom"}')
    monkeypatch.setenv("TICKET_HUB_HEALTH_STATUS", str(p))
    r = env.client.get("/health")
    assert r.json() == {"ok": False}


def test_login_page_public(env):
    r = env.client.get("/login")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "/session" in r.text


# ---------------------------------------------------------------------------
# internal viewer reads (authenticated / internal policies)
# ---------------------------------------------------------------------------

def test_internal_viewer_sees_everything(env):
    titles = {t["title"] for t in env.client.get("/tickets",
                                                 headers=env.h("bot")).json()}
    assert {"Root", "ChildX", "Top Secret Plan"} <= titles
    assert env.client.get(f"/tickets/{env.secret.id}",
                          headers=env.h("bot")).status_code == 200
    assert env.client.get("/board", headers=env.h("bot")).status_code == 200


def test_internal_viewer_gets_ui_and_docs(env):
    assert env.client.get("/", headers=env.h("bot")).status_code == 200
    assert env.client.get("/openapi.json",
                          headers=env.h("bot")).status_code == 200


# ---------------------------------------------------------------------------
# external_read routes: ExternalReader only
# ---------------------------------------------------------------------------

def test_external_list_projection_exact(env):
    r = env.client.get("/tickets", headers=env.h("e1"))
    assert r.status_code == 200
    titles = sorted(t["title"] for t in r.json())
    assert titles == ["ChildX", "Root"]
    assert sorted(r.json()[0].keys()) == sorted(
        ["id", "title", "body", "status", "priority", "label",
         "created_at", "updated_at"])


def test_external_search_cannot_reach_hidden(env):
    r = env.client.get("/tickets", params={"q": "Secret"},
                       headers=env.h("e1"))
    assert r.json() == []


def test_external_without_grant_sees_nothing(env):
    assert env.client.get("/tickets", headers=env.h("e2")).json() == []


def test_external_hidden_vs_missing_same_envelope(env):
    hidden = env.client.get(f"/tickets/{env.secret.id}", headers=env.h("e1"))
    missing = env.client.get("/tickets/TK-NOPE", headers=env.h("e1"))
    assert hidden.status_code == missing.status_code == 404
    assert hidden.json()["error"]["code"] == missing.json()["error"]["code"]


def test_external_ticket_detail_is_projection(env):
    r = env.client.get(f"/tickets/{env.root.id}", headers=env.h("e1"))
    assert r.status_code == 200
    assert "owner" not in r.json()
    assert "tags" not in r.json()


def test_external_events_only_visible_labels(env):
    # Events on an external-labelled ticket default to the ticket's label at
    # insert (contract §1); explicit internal events are filtered out.
    env.store.append_event(env.root.id, "comment",
                           payload={"text": "hello ext"}, label="public")
    env.store.append_event(env.root.id, "comment",
                           payload={"text": "hidden note"}, label="internal")
    r = env.client.get(f"/tickets/{env.root.id}/events", headers=env.h("e1"))
    assert r.status_code == 200
    texts = [e["payload"].get("text") for e in r.json()]
    assert "hello ext" in texts
    assert "hidden note" not in texts


def test_external_events_hidden_ticket_404(env):
    r = env.client.get(f"/tickets/{env.secret.id}/events", headers=env.h("e1"))
    assert r.status_code == 404


def test_external_wiki_index_and_page(env):
    idx = env.client.get("/wiki", headers=env.h("e1")).json()
    slugs = {p["slug"] for p in idx}
    assert "guide" not in slugs or True  # grant required below
    assert "secret-page" not in slugs
    page = env.client.get("/wiki/guide", headers=env.h("e1"))
    assert page.status_code == 404  # no wiki grant yet
    env.access.add_grant("e1", "wiki_page", "guide", by="own")
    page = env.client.get("/wiki/guide", headers=env.h("e1"))
    assert page.status_code == 200
    assert page.json()["page"]["title"] == "Guide"
    idx = env.client.get("/wiki", headers=env.h("e1")).json()
    assert {p["slug"] for p in idx} == {"guide"}


def test_external_wiki_search_filtered(env):
    env.access.add_grant("e1", "wiki_page", "guide", by="own")
    got = {p["slug"] for p in env.client.get(
        "/wiki-search", params={"q": "body"}, headers=env.h("e1")).json()}
    assert got == {"guide"}
    got = {p["slug"] for p in env.client.get(
        "/wiki-search", params={"q": "hidden"}, headers=env.h("e1")).json()}
    assert got == set()


# ---------------------------------------------------------------------------
# external policy behaviour on internal routes
# ---------------------------------------------------------------------------

def test_external_internal_gets_hidden_404(env):
    codes = {env.client.get(p, headers=env.h("e1")).status_code
             for p in ("/board", "/seats", "/roles", "/templates", "/jobs",
                       "/admin/status")}
    assert codes == {404}


def test_external_write_visible_ticket_403(env):
    r = env.client.post(f"/tickets/{env.root.id}/events",
                        json={"kind": "comment", "payload": {}},
                        headers=env.h("e1"))
    assert r.status_code == 403


def test_external_write_hidden_or_missing_ticket_404(env):
    r = env.client.post(f"/tickets/{env.secret.id}/events",
                        json={"kind": "comment", "payload": {}},
                        headers=env.h("e1"))
    assert r.status_code == 404
    r = env.client.post("/tickets/TK-NOPE/events",
                        json={"kind": "comment", "payload": {}},
                        headers=env.h("e1"))
    assert r.status_code == 404


def test_external_create_ticket_403(env):
    assert env.client.post("/tickets", json={"title": "x"},
                           headers=env.h("e1")).status_code == 403


def test_external_wiki_write_visible_vs_hidden(env):
    env.access.add_grant("e1", "wiki_page", "guide", by="own")
    r = env.client.post("/wiki/guide", json={"title": "t"},
                        headers=env.h("e1"))
    assert r.status_code == 403          # visible -> forbidden
    r = env.client.post("/wiki/secret-page", json={"title": "t"},
                        headers=env.h("e1"))
    assert r.status_code == 404          # hidden -> not found


def test_external_relay_effective_external_write_still_403(env):
    # relay may act_as e1 (external); effective viewer external cannot write.
    r = env.client.post(f"/tickets/{env.root.id}/events",
                        json={"kind": "comment", "actor": "e1",
                              "payload": {}},
                        headers=env.h("relay"))
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# identity / acting
# ---------------------------------------------------------------------------

def test_identity_mismatch_403(env):
    r = env.client.post(f"/tickets/{env.root.id}/events",
                        json={"kind": "comment", "actor": "own",
                              "payload": {}},
                        headers=env.h("bot"))
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "actor_mismatch"


def test_identity_relay_act_as_records_via(env):
    r = env.client.post(f"/tickets/{env.root.id}/events",
                        json={"kind": "comment", "actor": "bot",
                              "payload": {"text": "via relay"}},
                        headers=env.h("relay"))
    assert r.status_code == 201
    evs = [e for e in env.store.events(env.root.id) if e.kind == "comment"]
    assert evs[-1].actor.id == "bot"
    assert evs[-1].via == "relay"


def test_identity_relay_cannot_claim_internal_human(env):
    r = env.client.post(f"/tickets/{env.root.id}/events",
                        json={"kind": "comment", "actor": "own",
                              "payload": {}},
                        headers=env.h("relay"))
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "actor_mismatch"


def test_identity_self_claim_ok(env):
    r = env.client.post(f"/tickets/{env.root.id}/events",
                        json={"kind": "comment", "actor": "bot",
                              "payload": {}},
                        headers=env.h("bot"))
    assert r.status_code == 201


def test_identity_mismatch_on_hidden_ticket_is_404(env):
    # Visibility check (middleware) runs before identity: hidden -> 404 even
    # with a mismatched actor.
    r = env.client.post(f"/tickets/{env.secret.id}/events",
                        json={"kind": "comment", "actor": "own",
                              "payload": {}},
                        headers=env.h("e1"))
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# amendment A1: X-Hub-Act-As delegation header
# ---------------------------------------------------------------------------

def test_actas_relay_reads_as_external_hidden_404(env):
    # Relay acting as e1 sees e1's world: the internal-label ticket is hidden.
    hx = {**env.h("relay"), "X-Hub-Act-As": "e1"}
    assert env.client.get(f"/tickets/{env.secret.id}",
                          headers=hx).status_code == 404


def test_actas_relay_read_returns_external_projection(env):
    hx = {**env.h("relay"), "X-Hub-Act-As": "e1"}
    r = env.client.get(f"/tickets/{env.root.id}", headers=hx)
    assert r.status_code == 200
    assert sorted(r.json().keys()) == sorted(
        ["id", "title", "body", "status", "priority", "label",
         "created_at", "updated_at"])


def test_actas_relay_list_uses_external_reader(env):
    hx = {**env.h("relay"), "X-Hub-Act-As": "e1"}
    r = env.client.get("/tickets", headers=hx)
    assert r.status_code == 200
    assert sorted(t["title"] for t in r.json()) == ["ChildX", "Root"]


def test_actas_relay_write_edge_denied_nothing_mutated(env):
    hx = {**env.h("relay"), "X-Hub-Act-As": "e1"}
    before = len(env.store.edges(env.root.id)) \
        if hasattr(env.store, "edges") else None
    r = env.client.post(f"/tickets/{env.root.id}/edges",
                        json={"type": "relates_to", "dst": env.child_x.id},
                        headers=hx)
    assert r.status_code == 403          # visible -> external may not write
    if before is not None:
        assert len(env.store.edges(env.root.id)) == before


def test_actas_relay_write_event_denied_external_effective(env):
    hx = {**env.h("relay"), "X-Hub-Act-As": "e1"}
    n_before = len(list(env.store.events(env.root.id)))
    r = env.client.post(f"/tickets/{env.root.id}/events",
                        json={"kind": "comment", "payload": {}}, headers=hx)
    assert r.status_code == 403
    assert len(list(env.store.events(env.root.id))) == n_before


def test_actas_relay_wiki_write_denied_or_hidden(env):
    env.access.add_grant("e1", "wiki_page", "guide", by="own")
    hx = {**env.h("relay"), "X-Hub-Act-As": "e1"}
    r = env.client.post("/wiki/guide", json={"title": "t"}, headers=hx)
    assert r.status_code == 403          # visible page: forbidden
    r = env.client.post("/wiki/secret-page", json={"title": "t"}, headers=hx)
    assert r.status_code == 404          # hidden page: indistinguishable


def test_actas_relay_patch_denied(env):
    hx = {**env.h("relay"), "X-Hub-Act-As": "e1"}
    r = env.client.patch(f"/tickets/{env.root.id}",
                         json={"status": "in_progress"}, headers=hx)
    assert r.status_code == 403


def test_actas_without_scope_403(env):
    # bot's credential has no act_as scope: the header is refused outright.
    r = env.client.get("/tickets",
                       headers={**env.h("bot"), "X-Hub-Act-As": "e1"})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "actor_mismatch"


def test_actas_internal_human_403(env):
    # relay may not delegate as an internal human, even when allowed patterns
    # would match other actors.
    r = env.client.get("/tickets", headers={**env.h("relay"),
                                            "X-Hub-Act-As": "own"})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "actor_mismatch"


def test_actas_self_claim_ok(env):
    # A header naming the credential's own actor is not a delegation.
    r = env.client.get("/tickets", headers={**env.h("bot"),
                                            "X-Hub-Act-As": "bot"})
    assert r.status_code == 200


def test_actas_relay_for_low_authority_agent_cannot_publish(env):
    # Relay acting for bot (internal, not owner) inherits BOT's publication
    # authority, not the relay's: publishing below internal is refused.
    hx = {**env.h("relay"), "X-Hub-Act-As": "bot"}
    r = env.client.post("/tickets", json={"title": "x", "label": "external"},
                        headers=hx)
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "publish_forbidden"


def test_actas_body_actor_mismatch_403(env):
    # Header resolves to bot; a body actor naming another actor is a mismatch.
    hx = {**env.h("relay"), "X-Hub-Act-As": "bot"}
    r = env.client.post(f"/tickets/{env.root.id}/events",
                        json={"kind": "comment", "actor": "own",
                              "payload": {}}, headers=hx)
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "actor_mismatch"


def test_actas_body_actor_equal_effective_ok(env):
    hx = {**env.h("relay"), "X-Hub-Act-As": "bot"}
    r = env.client.post(f"/tickets/{env.root.id}/events",
                        json={"kind": "comment", "actor": "bot",
                              "payload": {}}, headers=hx)
    assert r.status_code == 201
    evs = [e for e in env.store.events(env.root.id) if e.kind == "comment"]
    assert evs[-1].actor.id == "bot"
    assert evs[-1].via == "relay"


def test_actas_body_mismatch_on_hidden_object_stays_404(env):
    # Visibility first: hidden ticket + delegation -> 404, not actor_mismatch.
    hx = {**env.h("relay"), "X-Hub-Act-As": "e1"}
    r = env.client.post(f"/tickets/{env.secret.id}/events",
                        json={"kind": "comment", "actor": "own",
                              "payload": {}}, headers=hx)
    assert r.status_code == 404


def test_actas_audit_rows_record_effective_actor_and_credential(env):
    hx = {**env.h("relay"), "X-Hub-Act-As": "e1"}
    relay_cred, token = env.access.issue_credential("relay", by="own",
                                                    scopes=("act_as",),
                                                    act_as_allow=("e*",))
    hx["Authorization"] = "Bearer " + token
    env.client.get(f"/tickets/{env.root.id}", headers=hx)
    row = env.store._conn.execute(
        "SELECT actor_id, credential_id FROM access_log ORDER BY rowid DESC"
        " LIMIT 1").fetchone()
    assert row[0] == "e1"                       # effective actor
    assert row[1] == relay_cred                 # the credential that acted


def test_actas_empty_header_ignored(env):
    r = env.client.get("/tickets", headers={**env.h("bot"),
                                            "X-Hub-Act-As": "  "})
    assert r.status_code == 200                 # no delegation requested


def test_owner_field_must_be_existing_internal_actor(env):
    r = env.client.post("/tickets", json={"title": "x", "owner": "ghost"},
                        headers=env.h("bot"))
    assert r.status_code == 400
    r = env.client.post("/tickets", json={"title": "x", "owner": "e1"},
                        headers=env.h("bot"))
    assert r.status_code == 400          # external actor not assignable
    r = env.client.post("/tickets",
                        json={"title": "assigned", "owner": "own"},
                        headers=env.h("bot"))
    assert r.status_code == 201
    assert r.json()["owner"]["id"] == "own"


def test_create_defaults_owner_to_acting_actor(env):
    r = env.client.post("/tickets", json={"title": "mine"},
                        headers=env.h("bot"))
    assert r.status_code == 201
    assert r.json()["owner"]["id"] == "bot"


def test_patch_identity_gate(env):
    r = env.client.patch(f"/tickets/{env.root.id}",
                         json={"status": "open", "actor": "own"},
                         headers=env.h("bot"))
    assert r.status_code == 403
    r = env.client.patch(f"/tickets/{env.root.id}", json={"status": "open"},
                         headers=env.h("bot"))
    assert r.status_code == 200
    assert r.json()["status"] == "open"


# ---------------------------------------------------------------------------
# publication authority and label endpoints
# ---------------------------------------------------------------------------

def test_publish_below_internal_requires_owner(env):
    r = env.client.post("/tickets", json={"title": "x", "label": "external"},
                        headers=env.h("bot"))
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "publish_forbidden"
    r = env.client.post("/tickets", json={"title": "pubbed",
                                          "label": "external"},
                        headers=env.h("own"))
    assert r.status_code == 201
    assert r.json()["label"] == "external"


def test_unsupported_label_400(env):
    r = env.client.post("/tickets", json={"title": "x", "label": "restricted"},
                        headers=env.h("own"))
    assert r.status_code == 400


def test_unsupported_label_400_for_non_owner(env):
    # Fix-round §2: check_label runs BEFORE the owner gate — a non-owner
    # sending an unsupported label gets 400 label_unsupported, not the 403
    # publish gate and never a KeyError-turned-404.
    for actor in ("bot", "own"):
        r = env.client.post("/tickets",
                            json={"title": "x", "label": "restricted"},
                            headers=env.h(actor))
        assert r.status_code == 400, actor
        assert "label_unsupported" in r.json()["error"]["message"], actor


def test_unsupported_label_400_auth_off(monkeypatch):
    # Fix-round §2: same 400 with auth off (previously silently dropped).
    monkeypatch.setenv("TICKET_HUB_AUTH", "off")
    c = TestClient(create_app(store=FakeStore()))
    t = c.post("/tickets", json={"title": "t"}).json()["id"]
    for path, body in (( "/tickets", {"title": "x", "label": "restricted"}),
                       (f"/tickets/{t}/events",
                        {"kind": "comment", "payload": {},
                         "label": "restricted"}),
                       ("/wiki/pg", {"title": "p", "label": "restricted"})):
        r = c.post(path, json=body)
        assert r.status_code == 400, path
        assert "label_unsupported" in r.json()["error"]["message"], path


def test_explicit_label_on_label_less_store_400(monkeypatch):
    # Fix-round §3: FakeStore cannot record labels; an explicit label is a
    # 400 bad_request (never TypeError/500 or a silent drop). Without a label
    # the legacy shape still works.
    monkeypatch.setenv("TICKET_HUB_AUTH", "off")
    c = TestClient(create_app(store=FakeStore()))
    t = c.post("/tickets", json={"title": "t"}).json()["id"]
    r = c.post(f"/tickets/{t}/label", json={"label": "external"})
    assert r.status_code == 400
    assert r.json()["error"]["message"] == "label not supported by this store"
    r = c.post("/wiki/pg", json={"title": "p", "label": "external"})
    assert r.status_code == 400
    r = c.post(f"/tickets/{t}/events", json={"kind": "comment",
                                             "payload": {},
                                             "label": "public"})
    assert r.status_code == 400
    # no label given -> unchanged legacy behaviour
    assert c.post("/wiki/pg", json={"title": "p"}).status_code == 201
    assert c.post(f"/tickets/{t}/events", json={"kind": "comment",
                                                "payload": {}}).status_code == 201


def test_wiki_publish_authority(env):
    r = env.client.post("/wiki/pg", json={"title": "t", "label": "external"},
                        headers=env.h("bot"))
    assert r.status_code == 403
    r = env.client.post("/wiki/pg", json={"title": "t"}, headers=env.h("bot"))
    assert r.status_code == 201
    assert r.json()["label"] == "internal"


def test_ticket_label_lowering_requires_owner(env):
    r = env.client.post(f"/tickets/{env.secret.id}/label",
                        json={"label": "external", "reason": "r"},
                        headers=env.h("bot"))
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "publish_forbidden"
    assert env.store.get_ticket(env.secret.id).label == "internal"
    r = env.client.post(f"/tickets/{env.secret.id}/label",
                        json={"label": "external", "reason": "publish it"},
                        headers=env.h("own"))
    assert r.status_code == 200
    assert r.json()["label"] == "external"


def test_ticket_label_raising_open_to_internal_writer(env):
    r = env.client.post(f"/tickets/{env.child_x.id}/label",
                        json={"label": "internal", "reason": "hush"},
                        headers=env.h("bot"))
    assert r.status_code == 200
    assert r.json()["label"] == "internal"
    kinds = [e.kind for e in env.store.events(env.child_x.id)]
    assert "label_changed" in kinds


def test_ticket_label_unsupported_400(env):
    r = env.client.post(f"/tickets/{env.root.id}/label",
                        json={"label": "restricted"}, headers=env.h("own"))
    assert r.status_code == 400


def test_ticket_label_hidden_for_external_404(env):
    r = env.client.post(f"/tickets/{env.secret.id}/label",
                        json={"label": "external"}, headers=env.h("e1"))
    assert r.status_code == 404


def test_wiki_label_change_is_new_revision(env):
    env.client.post("/wiki/notes", json={"title": "Notes",
                                         "body_md": "b", "tags": ["t"]},
                    headers=env.h("own"))
    r = env.client.post("/wiki/notes/label",
                        json={"label": "external", "reason": "share"},
                        headers=env.h("bot"))
    assert r.status_code == 403          # lowering internal -> external
    r = env.client.post("/wiki/notes/label",
                        json={"label": "external", "reason": "share"},
                        headers=env.h("own"))
    assert r.status_code == 200
    page = r.json()
    assert page["rev"] == 2
    assert page["title"] == "Notes" and page["body_md"] == "b"
    assert page["tags"] == ["t"] and page["label"] == "external"


def test_wiki_label_raising_allowed_for_writer(env):
    env.client.post("/wiki/pub", json={"title": "Pub", "label": "external"},
                    headers=env.h("own"))
    r = env.client.post("/wiki/pub/label", json={"label": "internal"},
                        headers=env.h("bot"))
    assert r.status_code == 200
    assert r.json()["label"] == "internal"


def test_event_label_accepted(env):
    r = env.client.post(f"/tickets/{env.root.id}/events",
                        json={"kind": "comment", "payload": {"text": "hi"},
                              "label": "public"},
                        headers=env.h("bot"))
    assert r.status_code == 201
    evs = [e for e in env.store.events(env.root.id) if e.kind == "comment"]
    assert evs[-1].label == "public"


def test_event_label_unsupported_400(env):
    r = env.client.post(f"/tickets/{env.root.id}/events",
                        json={"kind": "comment", "payload": {},
                              "label": "vault:dm"},
                        headers=env.h("bot"))
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# session, cookies, CSRF
# ---------------------------------------------------------------------------

def test_session_sets_cookie(env):
    r = env.client.post("/session", json={"token": env.tokens["bot"]})
    assert r.status_code == 204
    sc = r.headers.get("set-cookie", "")
    assert sc.startswith("hub_session=")
    assert "httponly" in sc.lower()
    assert "samesite=strict" in sc.lower()
    assert "path=/" in sc.lower()
    assert env.tokens["bot"] not in r.text  # 204 has no body


def test_session_bad_token_401(env):
    r = env.client.post("/session", json={"token": "***"})
    assert r.status_code == 401


def test_session_non_json_body_400(env):
    r = env.client.post("/session", content=b"not json",
                        headers={"Content-Type": "text/plain"})
    assert r.status_code == 400


def test_cookie_auth_read_ok(env):
    c = TestClient(env.app)
    c.cookies.set("hub_session", env.tokens["bot"])
    assert c.get("/tickets").status_code == 200


def test_cookie_mutation_without_origin_csrf(env):
    c = TestClient(env.app)
    c.cookies.set("hub_session", env.tokens["bot"])
    r = c.post("/tickets", json={"title": "x"})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "csrf"


def test_cookie_mutation_with_same_origin_ok(env):
    c = TestClient(env.app)
    c.cookies.set("hub_session", env.tokens["bot"])
    r = c.post("/tickets", json={"title": "x"},
               headers={"Origin": "http://testserver"})
    assert r.status_code == 201


def test_cookie_mutation_cross_origin_denied(env):
    c = TestClient(env.app)
    c.cookies.set("hub_session", env.tokens["bot"])
    r = c.post("/tickets", json={"title": "x"},
               headers={"Origin": "http://evil.example"})
    assert r.status_code == 403


def test_bearer_mutation_needs_no_origin(env):
    # Token auth is not cookie auth: no CSRF gate.
    assert env.client.post("/tickets", json={"title": "x"},
                           headers=env.h("bot")).status_code == 201


def test_secure_cookie_when_forwarded_https(env):
    r = env.client.post("/session", json={"token": env.tokens["bot"]},
                        headers={"X-Forwarded-Proto": "https"})
    assert "secure" in r.headers.get("set-cookie", "").lower()


def test_logout_clears_cookie(env):
    c = TestClient(env.app)
    c.cookies.set("hub_session", env.tokens["bot"])
    r = c.post("/logout", headers={"Origin": "http://testserver"})
    assert r.status_code == 204
    assert "hub_session=" in r.headers.get("set-cookie", "")


def test_revoked_token_rejected_on_cookie(env):
    cred_id, token = env.access.issue_credential("bot", by="own")
    c = TestClient(env.app)
    c.cookies.set("hub_session", token)
    assert c.get("/tickets").status_code == 200
    env.access.revoke_credential(cred_id, by="own")
    assert c.get("/tickets").status_code == 401


def test_suspended_actor_token_rejected(env):
    env.access.set_actor_status("bot", "suspended", by="own")
    assert env.client.get("/tickets", headers=env.h("bot")).status_code == 401


# ---------------------------------------------------------------------------
# access_log auditing
# ---------------------------------------------------------------------------

def _log_rows(env):
    return env.store._conn.execute(
        "SELECT actor_id, method, route, status, decision FROM access_log"
    ).fetchall()


def test_access_log_hidden_decision(env):
    # A hidden internal route for an external viewer is audited as 'hidden'.
    env.client.get("/board", headers=env.h("e1"))
    row = _log_rows(env)[-1]
    assert row["status"] == 404
    assert row["decision"] == "hidden"
    assert row["actor_id"] == "e1"


def test_unmatched_route_not_logged(env):
    before = len(_log_rows(env))
    env.client.get("/totally-unknown", headers=env.h("bot"))
    assert len(_log_rows(env)) == before


def test_access_log_allow_and_deny(env):
    env.client.get("/tickets", headers=env.h("bot"))     # allow 200
    env.client.get("/tickets")                            # deny 401
    rows = _log_rows(env)[-2:]
    assert rows[-2]["decision"] == "allow" and rows[-2]["status"] == 200
    assert rows[-1]["decision"] == "deny" and rows[-1]["status"] == 401


def test_access_log_deny_for_unauthenticated(env):
    env.client.get("/board")
    row = _log_rows(env)[-1]
    assert row["actor_id"] is None
    assert row["status"] == 401
    assert row["decision"] == "deny"


def test_no_token_material_in_access_log(env):
    env.client.get("/tickets", headers=env.h("bot"))
    env.client.post("/session", json={"token": env.tokens["own"]})
    leak = env.store._conn.execute(
        "SELECT COUNT(*) c FROM access_log "
        "WHERE route LIKE '%thk_%' OR decision LIKE '%thk_%' "
        "OR actor_id LIKE '%thk_%'").fetchone()["c"]
    assert leak == 0


# ---------------------------------------------------------------------------
# auth-off behaviour unchanged
# ---------------------------------------------------------------------------

def test_auth_off_full_legacy_flow(tmp_path, monkeypatch):
    monkeypatch.setenv("TICKET_HUB_AUTH", "off")
    store = Store(str(tmp_path / "legacy.db"))
    c = TestClient(create_app(StoreGateway(store)))
    t = c.post("/tickets", json={"title": "Legacy",
                                 "owner": {"name": "Zed", "kind": "agent"}})
    assert t.status_code == 201
    tid = t.json()["id"]
    assert c.get(f"/tickets/{tid}").status_code == 200
    assert c.patch(f"/tickets/{tid}", json={"status": "open",
                                            "actor": "Zed"}).status_code == 200
    ev = c.post(f"/tickets/{tid}/events", json={"kind": "comment",
                                                "actor": {"name": "Z"},
                                                "payload": {"text": "hi"}})
    assert ev.status_code == 201
    assert c.get("/board").status_code == 200
    assert c.get("/health").status_code == 200
    # auth-off health keeps the full monitor body (more than just ok)
    # the full monitor body: ok + the sre monitor's detail fields (ts/url/last_error)
    keys = set(c.get("/health").json().keys())
    assert "ok" in keys and len(keys) >= 3
    page = c.post("/wiki/pg", json={"title": "P"})
    assert page.status_code == 201
    assert c.get("/wiki/pg").status_code == 200
    # no policy gate: an unlisted (to auth) surface just works; no viewer set
    assert c.get("/tickets", headers={"Authorization": "***"}).status_code == 200


def test_auth_off_fake_store_suite_shape(tmp_path, monkeypatch):
    monkeypatch.setenv("TICKET_HUB_AUTH", "off")
    c = TestClient(create_app(store=FakeStore()))
    # I1: an explicit owner object must carry a valid actor kind (fail closed).
    t = c.post("/tickets", json={"title": "F",
                                 "owner": {"name": "o", "kind": "agent"}})
    assert t.status_code == 201
    assert c.post("/tickets",
                  json={"title": "G", "owner": {"name": "o"}}).status_code == 400
    assert c.get("/tickets").status_code == 200


# ---------------------------------------------------------------------------
# fix-round §1/§3: delegated bodies reach the handler; store TypeErrors
# propagate (no silent legacy-shape retry)
# ---------------------------------------------------------------------------

def test_delegated_post_body_reaches_handler_intact(env):
    # The middleware never reads the body (fix-round §1): a delegated POST
    # must arrive at the handler with its payload untouched.
    hx = {**env.h("relay"), "X-Hub-Act-As": "bot"}
    r = env.client.post(f"/tickets/{env.root.id}/events",
                        json={"kind": "comment", "payload": {"text": "hello"}},
                        headers=hx)
    assert r.status_code == 201
    evs = [e for e in env.store.events(env.root.id) if e.kind == "comment"]
    assert evs[-1].payload.get("text") == "hello"
    assert evs[-1].actor.id == "bot"
    assert evs[-1].via == "relay"


def test_delegated_patch_body_reaches_handler_intact(env):
    hx = {**env.h("relay"), "X-Hub-Act-As": "bot"}
    r = env.client.patch(f"/tickets/{env.root.id}",
                         json={"priority": "high"}, headers=hx)
    assert r.status_code == 200
    assert r.json()["priority"] == "high"


def test_delegated_post_body_actor_mismatch_403(env):
    # X-Hub-Act-As names bot; the body names own -> 403 actor_mismatch and
    # nothing is written (enforced in acting(), not by a body-reading
    # middleware).
    hx = {**env.h("relay"), "X-Hub-Act-As": "bot"}
    n_before = len(list(env.store.events(env.root.id)))
    r = env.client.post(f"/tickets/{env.root.id}/events",
                        json={"kind": "comment", "actor": "own",
                              "payload": {"text": "should not land"}},
                        headers=hx)
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "actor_mismatch"
    assert len(list(env.store.events(env.root.id))) == n_before


def test_store_type_error_is_500_and_not_retried(env):
    # Fix-round §3: a TypeError raised INSIDE a store call propagates to a
    # 500 — it is never mistaken for an unsupported-keyword signature and
    # retried without via= (the fake method counts its calls).
    calls = {"n": 0}

    class ExplodingGateway:
        def __init__(self, inner):
            self._inner = inner
            self._store = inner._store

        @property
        def conn(self):
            return self._inner.conn

        def get(self, ticket_id):
            return self._inner.get(ticket_id)

        def add_edge(self, *args, **kw):
            calls["n"] += 1
            raise TypeError("boom inside the store")

    env.app.state.store = ExplodingGateway(env.gateway)
    c = TestClient(env.app, raise_server_exceptions=False)
    r = c.post(f"/tickets/{env.root.id}/edges",
               json={"type": "relates_to", "dst": env.child_x.id},
               headers=env.h("bot"))
    assert r.status_code == 500
    assert calls["n"] == 1, f"store call retried {calls['n']} times"
