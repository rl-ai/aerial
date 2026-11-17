"""P6 phase-1 family-ACL tests — hermetic (tmp sqlite, in-process TestClient).

Covers: deny-by-default cross-family reads, family-escape attempts (read and
write), sensitivity labels (family default 'personal', ticket-vs-family
ceiling), request-time revocation propagation, legacy-token compatibility,
family-scoped creation, and access_log rows on family denies.

Reuses the real-store env fixture from test_api_access.py.
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)

import pytest
from fastapi.testclient import TestClient

from api import create_app
from ticket_hub.access import AccessStore
from ticket_hub.family_acl import FamilyAcl, backfill_families_from_tickets
from ticket_hub.model import Actor
from ticket_hub.store import Store

from tests.test_api_access import env  # noqa: F401  (real-store fixture)


@pytest.fixture()
def facl_env(env, monkeypatch):
    """env + a family-scoped alpha token + alpha/beta/none-family tickets."""
    access = env.access
    _, env.tokens["alpha_ro"] = access.issue_scoped_credential(
        "bot", by="own", family_scopes=[("alpha", "read")])
    _, env.tokens["alpha_rw"] = access.issue_scoped_credential(
        "bot", by="own", family_scopes=[("alpha", "read"), ("alpha", "write")])
    _, env.tokens["beta_ro"] = access.issue_scoped_credential(
        "bot", by="own", family_scopes=[("beta", "read")])
    _, env.tokens["star_ro"] = access.issue_scoped_credential(
        "bot", by="own", family_scopes=[("*", "read")])
    env.facl = FamilyAcl(env.store._conn)
    env.alpha = env.store.create_ticket(
        "Alpha work", owner=Actor(id="own", name="Own", family="alpha"),
        families=["alpha"], label="internal")
    env.beta = env.store.create_ticket(
        "Beta work", owner=Actor(id="own", name="Own", family="beta"),
        families=["beta"], label="internal")
    env.nofam = env.store.create_ticket(
        "No family", owner=Actor(id="own", name="Own"))
    env.h2 = lambda name: {"Authorization": "Bearer " + env.tokens[name]}
    return env


def test_family_label_defaults_to_personal(facl_env):
    assert facl_env.facl.family_label("alpha") == "personal"
    assert facl_env.facl.family_label("never-seen") == "personal"


def test_deny_by_default_list_and_get(facl_env):
    # alpha family label is 'personal' -> internal-labeled tickets are hidden
    # even inside the scoped family; cross-family is 404-hidden.
    r = facl_env.client.get("/tickets", headers=facl_env.h2("alpha_ro"))
    assert r.status_code == 200
    assert r.json() == []  # deny-by-default: nothing leaks
    assert facl_env.client.get(
        f"/tickets/{facl_env.alpha.id}",
        headers=facl_env.h2("alpha_ro")).status_code == 404
    assert facl_env.client.get(
        f"/tickets/{facl_env.beta.id}",
        headers=facl_env.h2("alpha_ro")).status_code == 404
    assert facl_env.client.get(
        f"/tickets/{facl_env.nofam.id}",
        headers=facl_env.h2("alpha_ro")).status_code == 404


def test_scope_escape_attempts(facl_env, monkeypatch):
    # alpha-ro token cannot read beta by guessing its id, cannot list it, and
    # cannot write alpha (read scope only).
    monkeypatch.setenv("TICKET_HUB_WRITE_TOKENS",
                       ",".join({facl_env.tokens["own"],
                                 facl_env.tokens["alpha_rw"],
                                 facl_env.tokens["alpha_ro"]}))
    h = facl_env.h2("alpha_ro")
    assert facl_env.client.get(
        f"/tickets/{facl_env.beta.id}", headers=h).status_code == 404
    listing = facl_env.client.get("/tickets", headers=h).json()
    assert facl_env.beta.id not in [t["id"] for t in listing]
    r = facl_env.client.patch(f"/tickets/{facl_env.alpha.id}",
                              json={"status": "open"}, headers=h)
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "family_denied"


def test_write_scope_and_family_scoped_create(facl_env, monkeypatch):
    monkeypatch.setenv("TICKET_HUB_WRITE_TOKENS",
                       ",".join({facl_env.tokens["own"],
                                 facl_env.tokens["alpha_rw"]}))
    h = facl_env.h2("alpha_rw")
    facl_env.facl.set_family_label("alpha", "internal", by="own")
    assert facl_env.client.get(
        f"/tickets/{facl_env.alpha.id}", headers=h).status_code == 200
    r = facl_env.client.patch(f"/tickets/{facl_env.alpha.id}",
                              json={"status": "open"}, headers=h)
    assert r.status_code == 200, r.text
    # creation inside the write family works...
    r = facl_env.client.post("/tickets",
                             json={"title": "scoped create",
                                   "families": ["alpha"]}, headers=h)
    assert r.status_code == 201, r.text
    # ...outside it is denied
    r = facl_env.client.post("/tickets",
                             json={"title": "escape create",
                                   "families": ["beta"]}, headers=h)
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "family_denied"


def test_sensitivity_ceiling_via_family_label(facl_env):
    h = facl_env.h2("alpha_ro")
    # raise the family label -> internal tickets of that family become visible
    facl_env.facl.set_family_label("alpha", "internal", by="own")
    r = facl_env.client.get(f"/tickets/{facl_env.alpha.id}", headers=h)
    assert r.status_code == 200
    assert r.json()["id"] == facl_env.alpha.id
    listing = facl_env.client.get("/tickets", headers=h).json()
    assert [t["id"] for t in listing] == [facl_env.alpha.id]
    # beta stays hidden (its family label is still default personal, and it is
    # a different family anyway)
    assert facl_env.client.get(
        f"/tickets/{facl_env.beta.id}", headers=h).status_code == 404


def test_wildcard_scope_sees_all(facl_env):
    h = facl_env.h2("star_ro")
    for t in (facl_env.alpha, facl_env.beta, facl_env.nofam):
        assert facl_env.client.get(f"/tickets/{t.id}", headers=h).status_code == 200


def test_legacy_token_unaffected(facl_env):
    # a credential with no fam: scopes keeps today's behaviour (migration
    # compatibility for the cadence tokens)
    h = facl_env.h2("own")
    listing = facl_env.client.get("/tickets", headers=h).json()
    ids = {t["id"] for t in listing}
    assert {facl_env.alpha.id, facl_env.beta.id, facl_env.nofam.id} <= ids


def test_revocation_propagates_next_request(facl_env):
    h = facl_env.h2("alpha_ro")
    facl_env.facl.set_family_label("alpha", "internal", by="own")
    assert facl_env.client.get(
        f"/tickets/{facl_env.alpha.id}", headers=h).status_code == 200
    # find the credential and revoke it; the very next request is 401
    creds = [c for c in facl_env.access.list_credentials()
             if c["scopes"] == ["fam:alpha:read"]]
    assert len(creds) == 1
    facl_env.access.revoke_credential(creds[0]["id"], by="own",
                                      reason="p6 test")
    r = facl_env.client.get(f"/tickets/{facl_env.alpha.id}", headers=h)
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthorized"


def test_access_log_records_family_denies(facl_env):
    h = facl_env.h2("alpha_ro")
    before = facl_env.access._conn.execute(
        "SELECT COUNT(*) FROM access_log").fetchone()[0]
    assert facl_env.client.get(
        f"/tickets/{facl_env.beta.id}", headers=h).status_code == 404
    rows = facl_env.access._conn.execute(
        "SELECT * FROM access_log WHERE seq > ? AND decision = 'hidden' "
        "AND status = 404", (before,)).fetchall()
    assert len(rows) == 1
    assert rows[0]["route"] == "/tickets/{ticket_id}"
    assert rows[0]["actor_id"] == "bot"


def test_backfill_migration_idempotent(tmp_path):
    store = Store(str(tmp_path / "hub.db"))
    owner = Actor(id="own", name="Own", family="gamma")
    store.create_ticket("A", owner=owner, families=["gamma", "delta"])
    store.create_ticket("B", owner=owner, families=["gamma"])
    inserted = backfill_families_from_tickets(store._conn, by="migrate")
    assert inserted == ["delta", "gamma"]  # sorted, both distinct families
    acl = FamilyAcl(store._conn)
    assert acl.family_label("gamma") == "personal"
    assert acl.family_label("delta") == "personal"
    # second run inserts nothing
    assert backfill_families_from_tickets(store._conn, by="migrate") == []
    # ensure_family never rewrites an existing label
    acl.set_family_label("gamma", "internal", by="own")
    acl.ensure_family("gamma", by="migrate")
    assert acl.family_label("gamma") == "internal"
