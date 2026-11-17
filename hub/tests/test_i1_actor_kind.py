"""I1 regressions (incident cluster I1, 2026-11-12, packet §6).

Signatures under test, 1:1:
(a) missing actor kind on ticket creation -> rejected with a clear error
    (fail closed, NOT defaulted);
(b) malformed legacy ticket row (stored-None actor kind / status) -> listed,
    not 400 ("unknown actor kind: None" / "None is not a valid Status");
(c) unauthorized request -> the 401 path is intact (auth defects are never
    masked by validation fixes);
plus the guarded legacy-row report + normalization path.

Unit tests run against the module directly (no live socket); the HTTP
creation/401 checks use the in-process real app from conftest.
"""
import json
import sqlite3

import pytest

from ticket_hub.store import Store


# ---------------------------------------------------------------------------
# (b) legacy rows: reads tolerate stored-None, never 400
# ---------------------------------------------------------------------------

@pytest.fixture()
def legacy_store(tmp_path):
    """A Store opened over a LEGACY database (pre-validation schema: kind and
    status nullable) containing an actor with kind NULL and a ticket with
    status NULL owned by it — the rows behind the I1 400s on reads."""
    db = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(db)
    # Legacy schema: nullable kind / status (no label columns either).
    conn.executescript("""
        CREATE TABLE actors (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            family TEXT NOT NULL DEFAULT 'unknown',
            kind TEXT
        );
        CREATE TABLE tickets (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            body TEXT NOT NULL DEFAULT '',
            status TEXT,
            priority TEXT NOT NULL DEFAULT 'normal',
            owner_id TEXT,
            tags TEXT NOT NULL DEFAULT '[]',
            families TEXT NOT NULL DEFAULT '[]',
            external_refs TEXT NOT NULL DEFAULT '{}',
            artifacts TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
    """)
    now = "2026-11-12T00:00:00.000+00:00"
    conn.execute(
        "INSERT INTO actors (id, name, family, kind) VALUES (?, ?, ?, ?)",
        ("legacy-bot", "Legacy Bot", "unknown", None))
    conn.execute(
        """INSERT INTO tickets (id, title, body, status, priority, owner_id,
                                 tags, families, external_refs, artifacts,
                                 created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        ("TK-LEGACY1", "Legacy ticket", "", None, "normal", "legacy-bot",
         "[]", "[]", "{}", "[]", now, now))
    conn.commit()
    conn.close()
    return Store(db)


def test_legacy_null_kind_actor_reads_as_system(legacy_store):
    actor = legacy_store.get_actor("legacy-bot")
    assert actor is not None
    assert actor.kind == "system"  # tolerated read, not ValueError


def test_legacy_null_status_ticket_listed_not_400(legacy_store):
    tickets = legacy_store.list_tickets()
    ids = [t.id for t in tickets]
    assert "TK-LEGACY1" in ids  # listed, not "None is not a valid Status"
    t = legacy_store.get_ticket("TK-LEGACY1")
    assert t.status.value == "draft"  # tolerated read default


def test_legacy_events_tolerate_null_kind(legacy_store):
    st = legacy_store
    st.append_event("TK-LEGACY1", "comment", actor=None,
                    payload={"text": "hi"})
    events = st.events("TK-LEGACY1")
    assert events  # timeline reads fine on the legacy ticket


def test_report_legacy_rows_read_only(legacy_store):
    report = legacy_store.report_legacy_rows()
    assert report["legacy_actor_kind"]["count"] == 1
    assert report["legacy_actor_kind"]["ids"] == ["legacy-bot"]
    assert report["legacy_ticket_status"]["ids"] == ["TK-LEGACY1"]
    # read-only: nothing changed after the report
    assert legacy_store.get_actor("legacy-bot").kind == "system"  # read view
    raw = sqlite3.connect(str(legacy_store._conn.execute(
        "PRAGMA database_list").fetchone()[2]))
    kind = raw.execute(
        "SELECT kind FROM actors WHERE id = 'legacy-bot'").fetchone()[0]
    raw.close()
    assert kind is None  # the row itself was NOT rewritten by the report


def test_guarded_normalization_only_null_kinds(legacy_store):
    st = legacy_store
    res = st.normalize_legacy_actor_kinds(by="integrator")
    assert res["normalized_actors"] == ["legacy-bot"]
    assert res["tagged_tickets"] == ["TK-LEGACY1"]
    # actor row now 'system', ticket tagged in meta
    raw = sqlite3.connect(str(st._conn.execute(
        "PRAGMA database_list").fetchone()[2]))
    kind = raw.execute(
        "SELECT kind FROM actors WHERE id = 'legacy-bot'").fetchone()[0]
    refs = json.loads(raw.execute(
        "SELECT external_refs FROM tickets WHERE id = 'TK-LEGACY1'"
    ).fetchone()[0])
    raw.close()
    assert kind == "system"
    assert refs["legacy_actor_kind_normalized"] is True
    # idempotent: second pass normalizes nothing new
    assert st.normalize_legacy_actor_kinds(by="integrator")[
        "normalized_actors"] == []


# ---------------------------------------------------------------------------
# (a) creation: missing/unknown actor kind rejected, fail closed
# ---------------------------------------------------------------------------

def test_create_missing_actor_kind_rejected(hub_base_url):
    import urllib.error
    import urllib.request
    req = urllib.request.Request(
        hub_base_url + "/tickets",
        data=json.dumps({"title": "no kind",
                         "owner": {"id": "ghost", "name": "Ghost"}}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=5)
    assert exc.value.code == 400
    body = json.loads(exc.value.read())
    assert body["error"]["code"] == "missing_actor_kind"


def test_create_unknown_actor_kind_rejected(hub_base_url):
    import urllib.error
    import urllib.request
    req = urllib.request.Request(
        hub_base_url + "/tickets",
        data=json.dumps({"title": "bad kind",
                         "owner": {"id": "w", "name": "W",
                                   "kind": "wizard"}}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=5)
    assert exc.value.code == 400
    body = json.loads(exc.value.read())
    assert body["error"]["code"] == "unknown_actor_kind"


def test_create_with_valid_kind_still_works(hub_base_url):
    import urllib.request
    req = urllib.request.Request(
        hub_base_url + "/tickets",
        data=json.dumps({"title": "valid kind",
                         "owner": {"id": "ok", "name": "Ok",
                                   "kind": "agent"}}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=5) as resp:
        assert resp.status == 201


# ---------------------------------------------------------------------------
# (c) unauthorized request -> 401 path intact
# ---------------------------------------------------------------------------

def test_unauthenticated_request_is_401(tmp_path):
    from fastapi.testclient import TestClient
    from api import create_app
    from fake_store import FakeStore

    client = TestClient(create_app(FakeStore(), auth=True))
    r = client.get("/tickets")
    assert r.status_code == 401  # separate signature from the 400s above
    r = client.post("/tickets", json={"title": "x"})
    assert r.status_code == 401
