"""Tests for ticket_hub core lane: CRUD, hierarchy, cycles, event
immutability, lineage ordering, filters."""
import sqlite3

import pytest

from ticket_hub import (
    ALLOWED_TRANSITIONS,
    Actor,
    Status,
    Store,
    assert_transition,
    lineage,
    lineage_dict,
)

OWNER = Actor(id="a-owner", name="Owner", family="owner", kind="human")
HERMES = Actor(id="a-hermes", name="Hermes", family="hermes", kind="agent")
FORGE = Actor(id="a-forge", name="Forge", family="forge", kind="agent")


@pytest.fixture()
def store(tmp_path):
    s = Store(str(tmp_path / "hub.db"))
    yield s
    s.close()


# ---------------------------------------------------------------- CRUD ----


def test_create_and_get_ticket(store):
    t = store.create_ticket("Ship v0", "body text", owner=OWNER, tags=["launch"])
    assert t.id.startswith("TK-")
    assert t.status is Status.DRAFT
    assert t.owner.id == "a-owner"
    assert t.tags == ["launch"]
    got = store.get_ticket(t.id)
    assert got == t


def test_get_missing_ticket_returns_none(store):
    assert store.get_ticket("TK-nope") is None


def test_create_missing_parent_raises(store):
    with pytest.raises(KeyError):
        store.create_ticket("orphan", parent="TK-does-not-exist", owner=OWNER)


def test_update_ticket_bumps_status_and_denormalized_row(store):
    t = store.create_ticket("w", owner=HERMES)
    t2 = store.update_ticket(t.id, status="open", actor=HERMES)
    assert t2.status is Status.OPEN
    assert t2.updated_at >= t.updated_at
    kinds = [e.kind for e in store.events(t.id)]
    assert kinds == ["created", "status_changed"]


def test_illegal_transition_rejected(store):
    t = store.create_ticket("w", owner=OWNER)  # draft
    with pytest.raises(ValueError):
        store.update_ticket(t.id, status="done", actor=OWNER)
    # draft stays put
    assert store.get_ticket(t.id).status is Status.DRAFT


def test_transition_table_shape():
    assert Status.OPEN in ALLOWED_TRANSITIONS[Status.DRAFT]
    assert len(ALLOWED_TRANSITIONS[Status.DONE]) == 0
    assert len(ALLOWED_TRANSITIONS[Status.CANCELLED]) == 0
    with pytest.raises(ValueError):
        assert_transition(Status.DONE, Status.OPEN)


def test_update_emits_updated_event(store):
    t = store.create_ticket("w", owner=OWNER)
    store.update_ticket(t.id, title="w2", actor=OWNER)
    kinds = [e.kind for e in store.events(t.id)]
    assert kinds == ["created", "updated"]


def test_unknown_event_kind_rejected(store):
    t = store.create_ticket("w", owner=OWNER)
    with pytest.raises(ValueError):
        store.append_event(t.id, "not_a_kind")


def test_append_event_roundtrip(store):
    t = store.create_ticket("w", owner=OWNER)
    ev = store.append_event(t.id, "comment", actor=HERMES, payload={"text": "hi"})
    assert ev.id.startswith("EV-")
    assert ev.ticket_id == t.id
    events = store.events(t.id)
    assert [e.id for e in events] == [events[0].id, ev.id]
    assert events[-1].payload == {"text": "hi"}
    assert events[-1].actor.id == "a-hermes"


# --------------------------------------------------------- hierarchy ------


def _three_levels(store):
    root = store.create_ticket("root", owner=OWNER)
    mid = store.create_ticket("mid", parent=root.id, owner=HERMES)
    leaf = store.create_ticket("leaf", parent=mid.id, owner=FORGE)
    return root, mid, leaf


def test_hierarchy_depth_3(store):
    root, mid, leaf = _three_levels(store)
    assert [c.id for c in store.children(root.id)] == [mid.id]
    assert store.parent_of(mid.id).id == root.id
    assert [t.id for t in store.ancestry(leaf.id)] == [root.id, mid.id]  # root-first
    assert [t.id for t in store.descendants(root.id)] == [mid.id, leaf.id]  # BFS
    assert store.descendants(leaf.id) == []


def test_cycle_rejected_via_add_edge(store):
    root, mid, leaf = _three_levels(store)
    # leaf -> root would close the parent chain
    with pytest.raises(ValueError):
        store.add_edge(leaf.id, root.id, "parent_of")
    # leaf -> mid direct cycle
    with pytest.raises(ValueError):
        store.add_edge(leaf.id, mid.id, "parent_of")


def test_cycle_rejected_on_create_parent(store):
    root, mid, leaf = _three_levels(store)
    # A brand-new child can't cycle, but re-parenting an ancestor under its
    # own descendant is caught by add_edge; also self-parent is rejected:
    with pytest.raises(ValueError):
        store.add_edge(root.id, root.id, "parent_of")


def test_second_parent_rejected(store):
    root, mid, leaf = _three_levels(store)
    other = store.create_ticket("other", owner=OWNER)
    with pytest.raises(ValueError):
        store.add_edge(other.id, mid.id, "parent_of")


def test_non_hierarchy_edges_and_related(store):
    a = store.create_ticket("a", owner=OWNER)
    b = store.create_ticket("b", owner=HERMES)
    c = store.create_ticket("c", owner=FORGE)
    store.add_edge(a.id, b.id, "blocks")
    store.add_edge(c.id, a.id, "relates_to")
    assert {t.id for t in store.related(a.id)} == {b.id, c.id}
    edge = store.get_edges(a.id, type="blocks", direction="out")[0]
    assert edge.type == "blocks" and edge.src == a.id
    with pytest.raises(ValueError):
        store.add_edge(a.id, b.id, "blocks")  # duplicate
    with pytest.raises(ValueError):
        store.add_edge(a.id, b.id, "nonsense")  # unknown type


def test_related_includes_graph_edge_types(store):
    a = store.create_ticket("a", owner=OWNER)
    b = store.create_ticket("b", owner=HERMES)
    store.add_edge(a.id, b.id, "derived-from")
    assert b.id in {t.id for t in store.related(a.id)}
    assert a.id in {t.id for t in store.related(b.id)}  # either direction


# -------------------------------------------------- event immutability ----


def test_no_event_mutation_api(store):
    for name in ("update_event", "delete_event", "edit_event", "remove_event"):
        assert not hasattr(Store, name)


def test_db_triggers_block_event_update_delete(store):
    store.create_ticket("w", owner=OWNER)
    raw = sqlite3.connect(store.db_path)
    with pytest.raises(sqlite3.IntegrityError):
        raw.execute("UPDATE events SET payload = '{}'")
    with pytest.raises(sqlite3.IntegrityError):
        raw.execute("DELETE FROM events")
    raw.close()


# ------------------------------------------------------------- lineage ----


def test_lineage_projection(store):
    root, mid, leaf = _three_levels(store)
    store.append_event(mid.id, "decision", actor=HERMES, payload={"what": "pick A"})
    lin = lineage(store, mid.id)
    assert lin["ticket"].id == mid.id
    assert [t.id for t in lin["ancestry"]] == [root.id]
    assert lin["descendants"]["ticket"].id == mid.id
    assert [c["ticket"].id for c in lin["descendants"]["children"]] == [leaf.id]
    ev_ids = [e.id for e in lin["events"]]
    assert ev_ids == sorted(ev_ids, key=lambda i: [e.seq for e in lin["events"] if e.id == i][0])
    kinds = [e.kind for e in lin["events"]]
    assert kinds == ["created", "decision"]
    # json-serializable variant
    d = lineage_dict(store, mid.id)
    assert d["ticket"]["status"] == "draft"
    assert d["events"][-1]["kind"] == "decision"


def test_events_ordered_by_seq(store):
    t = store.create_ticket("w", owner=OWNER)
    for i in range(5):
        store.append_event(t.id, "checkpoint", actor=OWNER, payload={"i": i})
    evs = store.events(t.id)
    assert [e.payload.get("i") for e in evs[1:]] == [0, 1, 2, 3, 4]
    assert evs == sorted(evs, key=lambda e: (e.ts, e.seq))


# ------------------------------------------------------------ filters -----


def test_list_filters(store):
    a = store.create_ticket("alpha plan", owner=HERMES, tags=["build"])
    b = store.create_ticket("beta", owner=FORGE, tags=["docs"])
    store.update_ticket(a.id, status="open", actor=HERMES)
    assert [t.id for t in store.list_tickets(status="open")] == [a.id]
    assert [t.id for t in store.list_tickets(status="draft")] == [b.id]
    assert [t.id for t in store.list_tickets(family="hermes")] == [a.id]
    assert [t.id for t in store.list_tickets(tag="docs")] == [b.id]
    assert [t.id for t in store.list_tickets(q="alpha")] == [a.id]
    assert store.list_tickets(q="zzz") == []
    assert len(store.list_tickets()) == 2


def test_family_filter_includes_declared_families(store):
    a = store.create_ticket("cross-family", owner=HERMES, families=["forge"])
    assert a.id in [t.id for t in store.list_tickets(family="forge")]
    assert a.id in [t.id for t in store.list_tickets(family="hermes")]


# ------------------------------------------------ thread bindings --------


def test_bind_thread_and_timeline(store):
    a = store.create_ticket("parent conv", owner=OWNER)
    child = store.create_ticket("child conv", parent=a.id, owner=HERMES)
    store.bind_thread(a.id, "matrix", "!room:ex", thread_id="$thread", root_msg_id="$root")
    store.bind_thread(child.id, "matrix", "!room:ex", thread_id="$thread")
    store.append_event(child.id, "thread_message", actor=FORGE, payload={"text": "from chat"})
    tl = store.timeline(a.id)
    kinds = [(e["kind"], e.get("source_ticket")) for e in tl]
    assert ("thread_message", child.id) in kinds
    # ordering holds in the merged view
    keys = [(e["ts"], e["seq"]) for e in tl]
    assert keys == sorted(keys)
    bindings = store.bindings(a.id)
    assert len(bindings) == 1 and bindings[0].platform == "matrix"
    with pytest.raises(ValueError):
        store.bind_thread(a.id, "irc", "!room:ex")  # unknown platform


def test_wal_mode_enabled(store):
    conn = sqlite3.connect(store.db_path)
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    conn.close()
    assert mode.lower() == "wal"


def test_board_grouping(store):
    t = store.create_ticket("w", owner=OWNER)
    store.update_ticket(t.id, status="open", actor=OWNER)
    board = store.board()
    assert [x.id for x in board["open"]] == [t.id]
    assert board["done"] == []
