"""hubp1-w1: store labels, actor access columns, `via` (ACCESS-P1-CONTRACT §1)."""
import pathlib
import sqlite3

import pytest

from ticket_hub.model import (
    LABELS,
    LABEL_RANK,
    Actor,
    Event,
    Ticket,
    WikiPage,
    check_label,
    max_label,
)
from ticket_hub.store import Store

OWNER = Actor(id="owner", name="owner", family="owner", kind="human")
BOT = Actor(id="bot", name="bot", family="forge", kind="agent")

_OLD_SCHEMA = """
CREATE TABLE actors (id TEXT PRIMARY KEY, name TEXT NOT NULL,
    family TEXT NOT NULL DEFAULT 'unknown', kind TEXT NOT NULL DEFAULT 'agent');
CREATE TABLE tickets (id TEXT PRIMARY KEY, title TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '', status TEXT NOT NULL,
    priority TEXT NOT NULL DEFAULT 'normal', owner_id TEXT,
    tags TEXT NOT NULL DEFAULT '[]', families TEXT NOT NULL DEFAULT '[]',
    external_refs TEXT NOT NULL DEFAULT '{}', artifacts TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
INSERT INTO tickets VALUES ('TK-OLD','old ticket','','open','normal',NULL,
    '[]','[]','{}','[]','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z');
"""


@pytest.fixture()
def store(tmp_path):
    s = Store(str(tmp_path / "hub.db"))
    yield s
    s.close()


# ------------------------------------------------------- model vocabulary ----

def test_label_constants_and_check_label():
    assert LABELS == ("public", "external", "internal")
    assert LABEL_RANK == {"public": 0, "external": 1, "internal": 2}
    for lab in LABELS:
        assert check_label(lab) == lab


@pytest.mark.parametrize("bad", ["restricted", "vault:alpha", "", "Internal", "PUBLIC"])
def test_check_label_rejects_unsupported(bad):
    with pytest.raises(ValueError) as ei:
        check_label(bad)
    assert str(ei.value).startswith("label_unsupported")


def test_max_label():
    assert max_label("public", "internal") == "internal"
    assert max_label("internal", "public") == "internal"
    assert max_label("external", "public") == "external"
    assert max_label("public", "public") == "public"
    with pytest.raises(ValueError):
        max_label("restricted", "public")


def test_dataclass_defaults_and_dicts():
    t = Ticket(id="TK-x", title="t")
    e = Event(id="EV-x", ticket_id="TK-x", ts="", actor=None, kind="comment")
    w = WikiPage(id="WP-x", slug="s", rev=1, title="T")
    assert t.label == e.label == w.label == "internal"
    assert t.to_dict()["label"] == "internal"
    assert e.to_dict()["label"] == "internal"
    assert w.to_dict()["label"] == "internal"
    assert "via" not in e.to_dict()  # only exposed when set


# ------------------------------------------------------------ migration ----

def test_old_database_rows_read_internal(tmp_path):
    db = str(tmp_path / "old.db")
    conn = sqlite3.connect(db)
    conn.executescript(_OLD_SCHEMA)
    conn.commit()
    conn.close()
    s = Store(db)
    try:
        # AT-1.5: existing rows read back as internal
        assert s.get_ticket("TK-OLD").label == "internal"
        # actor access columns exist after migration
        cols = {r["name"] for r in s._conn.execute("PRAGMA table_info(actors)")}
        assert {"realm", "clearance", "sponsor_id", "expires_at", "is_owner"} <= cols
        # tickets/events/wiki_pages got their label columns
        for table in ("tickets", "events", "wiki_pages"):
            tcols = {r["name"] for r in s._conn.execute(f"PRAGMA table_info({table})")}
            assert "label" in tcols
        # migration is idempotent: reopen twice, same columns, rows intact
        s2 = Store(db)
        s2.close()
        assert s.get_ticket("TK-OLD").label == "internal"
        cols2 = {r["name"] for r in s._conn.execute("PRAGMA table_info(actors)")}
        assert cols2 == cols | {"realm", "clearance", "sponsor_id", "expires_at", "is_owner"}
    finally:
        s.close()


def test_migration_never_rewrites_existing_rows(tmp_path):
    db = str(tmp_path / "old.db")
    conn = sqlite3.connect(db)
    conn.executescript(_OLD_SCHEMA)
    conn.commit()
    conn.close()
    s = Store(db)
    row = s._conn.execute(
        "SELECT title, status, created_at FROM tickets WHERE id='TK-OLD'"
    ).fetchone()
    assert (row["title"], row["status"], row["created_at"]) == (
        "old ticket", "open", "2026-01-01T00:00:00Z"
    )
    s.close()


# ---------------------------------------------------------- create_ticket ----

def test_create_ticket_label_stored_and_default(store):
    t = store.create_ticket("pub", owner=OWNER, label="public")
    assert t.label == "public"
    assert store.get_ticket(t.id).label == "public"
    assert t.to_dict()["label"] == "public"
    default = store.create_ticket("def", owner=OWNER)
    assert default.label == "internal"
    with pytest.raises(ValueError) as ei:
        store.create_ticket("bad", owner=OWNER, label="restricted")
    assert str(ei.value).startswith("label_unsupported")


def test_child_defaults_internal_regardless_of_parent(store):
    parent = store.create_ticket("p", owner=OWNER, label="public")
    child = store.create_ticket("c", parent=parent.id, owner=OWNER)
    assert child.label == "internal"
    grandchild = store.create_ticket("g", parent=child.id, owner=OWNER, label="external")
    assert grandchild.label == "external"  # explicit label honoured on children


# -------------------------------------------------------------- events ----

def test_event_label_decided_at_insert_not_read(store):
    t = store.create_ticket("ext", owner=OWNER, label="external")
    e1 = store.append_event(t.id, "comment", actor=BOT, payload={"n": 1})
    assert e1.label == "external"  # ticket's label at insert
    store.set_ticket_label(t.id, "internal", actor=OWNER, reason="pull back")
    e2 = store.append_event(t.id, "comment", actor=BOT, payload={"n": 2})
    assert e2.label == "internal"
    events = {e.id: e for e in store.events(t.id)}
    # stored, not derived at read time: e1 keeps its original label
    assert events[e1.id].label == "external"
    assert events[e2.id].label == "internal"
    # and in the raw rows, proving nothing is computed on read
    row = store._conn.execute("SELECT label FROM events WHERE id=?", (e1.id,)).fetchone()
    assert row["label"] == "external"


def test_event_explicit_label_and_check(store):
    t = store.create_ticket("i", owner=OWNER)  # internal
    e = store.append_event(t.id, "comment", actor=BOT, label="public")
    assert e.label == "public"
    with pytest.raises(ValueError) as ei:
        store.append_event(t.id, "comment", actor=BOT, label="restricted")
    assert str(ei.value).startswith("label_unsupported")


def test_event_via_in_actor_json_and_dict(store):
    t = store.create_ticket("t", owner=OWNER)
    e = store.append_event(t.id, "comment", actor=BOT, via="relay-1")
    raw = store._conn.execute(
        "SELECT actor_json FROM events WHERE id=?", (e.id,)
    ).fetchone()[0]
    import json
    ad = json.loads(raw)
    assert ad["via"] == "relay-1"
    assert ad["id"] == BOT.id and ad["name"] == BOT.name
    d = store.events(t.id)[-1].to_dict()
    assert d["via"] == "relay-1"
    assert d["actor"]["id"] == BOT.id
    # no via -> key absent, actor_json unchanged shape
    e2 = store.append_event(t.id, "comment", actor=BOT)
    d2 = store.events(t.id)[-1].to_dict()
    assert "via" not in d2 and "via" not in d2["actor"]


# ------------------------------------------------------ set_ticket_label ----

def test_set_ticket_label_appends_internal_event(store):
    t = store.create_ticket("t", owner=OWNER, label="external")
    out = store.set_ticket_label(t.id, "internal", actor=OWNER, reason="r1")
    assert out.label == "internal"
    lc = [e for e in store.events(t.id) if e.kind == "label_changed"]
    assert len(lc) == 1
    assert lc[0].label == "internal"
    assert lc[0].payload == {"from": "external", "to": "internal", "reason": "r1"}


def test_set_ticket_label_noop_when_unchanged(store):
    t = store.create_ticket("t", owner=OWNER)
    before = len(store.events(t.id))
    out = store.set_ticket_label(t.id, "internal", actor=OWNER)
    assert out.label == "internal"
    assert len(store.events(t.id)) == before
    assert not [e for e in store.events(t.id) if e.kind == "label_changed"]


def test_set_ticket_label_checks_and_requires_ticket(store):
    t = store.create_ticket("t", owner=OWNER)
    with pytest.raises(ValueError) as ei:
        store.set_ticket_label(t.id, "restricted", actor=OWNER)
    assert str(ei.value).startswith("label_unsupported")
    with pytest.raises(KeyError):
        store.set_ticket_label("TK-nope", "public", actor=OWNER)


def test_update_ticket_status_event_inherits_label(store):
    t = store.create_ticket("t", owner=OWNER, label="external")
    store.update_ticket(t.id, status="open", actor=OWNER)
    sc = [e for e in store.events(t.id) if e.kind == "status_changed"]
    assert len(sc) == 1 and sc[0].label == "external"


def test_update_ticket_via_passes_through(store):
    t = store.create_ticket("t", owner=OWNER)
    store.update_ticket(t.id, title="renamed", actor=OWNER, via="relay-9")
    upd = [e for e in store.events(t.id) if e.kind == "updated"]
    assert len(upd) == 1 and upd[0].via == "relay-9"


# ------------------------------------------------------------- wiki ----

def test_put_page_label_rules(store):
    w1 = store.put_page("pg", title="one", label="public")
    assert w1.label == "public"
    w2 = store.put_page("pg", title="two")  # None -> current latest revision's label
    assert w2.label == "public"
    w3 = store.put_page("pg", title="three", label="internal")
    assert w3.label == "internal"
    assert store.get_page("pg").label == "internal"
    w4 = store.put_page("other", title="fresh")  # no revisions -> internal
    assert w4.label == "internal"
    with pytest.raises(ValueError) as ei:
        store.put_page("pg", title="x", label="vault:1")
    assert str(ei.value).startswith("label_unsupported")
    hist = store.page_history("pg")
    assert [p.label for p in hist] == ["public", "public", "internal"]


# ------------------------------------------------------------ triggers ----

def test_append_only_triggers_still_block(store):
    store.create_ticket("t", owner=OWNER)
    store.append_event(store.list_tickets()[0].id, "comment", actor=OWNER)
    store.put_page("p", title="x")
    with pytest.raises(sqlite3.DatabaseError):
        store._conn.execute("UPDATE events SET label='public'")
    with pytest.raises(sqlite3.DatabaseError):
        store._conn.execute("DELETE FROM events")
    with pytest.raises(sqlite3.DatabaseError):
        store._conn.execute("UPDATE wiki_pages SET label='public'")


# ------------------------------------------- legacy-guard machinery (w2fix) ----

def test_init_migrates_once_and_errors_raise(tmp_path, monkeypatch):
    """The retry loop is gone: __init__ calls _migrate() exactly once and lets
    migration errors propagate (no swallow, no second attempt)."""
    import ticket_hub.store as store_mod

    calls = []

    class Flaky(store_mod.Store):
        def _migrate(self):
            calls.append(1)
            raise sqlite3.OperationalError("blocked")

    with pytest.raises(sqlite3.OperationalError):
        Flaky(str(tmp_path / "x.db"))
    assert len(calls) == 1  # once, never retried


def test_w2fix_source_has_no_legacy_guard_left():
    src = (pathlib.Path(__file__).resolve().parents[1]
           / "ticket_hub" / "store.py").read_text()
    for gone in ("for attempt in range", "_columns(", "_has(", "ev_cols",
                 "wp_cols"):
        assert gone not in src, f"legacy guard machinery still present: {gone!r}"


def test_w2fix_always_writes_label_column(store):
    """No fallback INSERT branches: events and wiki revisions carry the label
    column unconditionally, even when the label resolves to the default."""
    t = store.create_ticket("t", owner=OWNER)  # internal by default
    e = store.append_event(t.id, "comment", actor=BOT)
    raw = store._conn.execute(
        "SELECT label FROM events WHERE id=?", (e.id,)
    ).fetchone()
    assert raw["label"] == "internal"
    w1 = store.put_page("s", title="a")
    w2 = store.put_page("s", title="b")
    labels = [r["label"] for r in store._conn.execute(
        "SELECT label FROM wiki_pages WHERE slug='s' ORDER BY rev")]
    assert labels == ["internal", "internal"]
    assert (w1.label, w2.label) == ("internal", "internal")


# ------------------------------------------------------------- gateway ----

def test_gateway_passthrough_and_conn(store):
    from hub_gateway import StoreGateway

    g = StoreGateway(store)
    assert g.conn is store._conn
    t = g.create_ticket(title="gw", owner="ann", label="external")
    assert t["label"] == "external"
    ch = g.create_ticket(title="ch", parent=t["id"], owner="ann")
    assert ch["label"] == "internal"
    ev = g.append_event(t["id"], "comment", actor="ann", payload={"a": 1}, via="relay-2")
    assert ev["via"] == "relay-2" and ev["label"] == "external"
    patched = g.patch(t["id"], actor="ann", title="new", via="relay-3")
    assert patched["label"] == "external"
    upd_ev = [e for e in g.events(t["id"]) if e["kind"] == "updated"][-1]
    assert upd_ev["via"] == "relay-3"
    out = g.set_ticket_label(t["id"], "internal", actor="ann", reason="back")
    assert out["label"] == "internal"
    lc = [e for e in g.events(t["id"]) if e["kind"] == "label_changed"]
    assert lc[-1]["payload"]["reason"] == "back"
    page = g.put_page("slug", title="p", actor="ann", label="public")
    assert page["label"] == "public"
    page2 = g.put_page("slug", title="p2")
    assert page2["label"] == "public"


def test_api_event_kinds_includes_label_changed():
    import api
    assert "label_changed" in api.EVENT_KINDS


# ------------------------------------------------------------ ensure_actor --

def test_ensure_actor_conflict_never_overwrites_kind(store):
    """Fix round 3: kind drives security decisions (act_as relay targets must
    not be able to flip a human's kind), so an upsert conflict updates only
    name/family."""
    store.ensure_actor(Actor(id="X", name="Exdee", family="f", kind="human"))
    again = store.ensure_actor(Actor(id="X", name="Exdee 2", family="f2",
                                     kind="agent"))
    store.conn.commit()
    row = store.conn.execute(
        "SELECT kind, name, family FROM actors WHERE id='X'").fetchone()
    assert row["kind"] == "human"          # never overwritten
    assert row["name"] == "Exdee 2"        # name/family DO update
    assert row["family"] == "f2"
    # get_actor reflects the stored (safe) kind
    assert store.get_actor("X").kind == "human"
    assert again.kind == "agent"  # return value is just the passed actor
