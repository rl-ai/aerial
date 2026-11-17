"""The store is safe across request threads (2026-11-11 hub incident).

One real connection shared by FastAPI's worker threads interleaved implicit
transactions and wedged writes ("database is locked"). Per-thread connections
fixed that but broke components that keep the connection they were given: used
from another thread it raised sqlite3.InterfaceError. Store hands out one stable
ThreadConnection that always resolves to the calling thread's connection.
"""
import sqlite3
import threading

import pytest

from ticket_hub.store import Store, ThreadConnection


@pytest.fixture()
def store(tmp_path):
    s = Store(str(tmp_path / "hub.db"))
    yield s
    s.close()


def _in_thread(fn):
    """Run fn in a new thread; return its result or raise its error here."""
    box = {}

    def run():
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - re-raised below
            box["error"] = exc

    t = threading.Thread(target=run)
    t.start()
    t.join(30)
    if "error" in box:
        raise box["error"]
    return box.get("value")


def test_conn_is_one_stable_stand_in(store):
    assert isinstance(store._conn, ThreadConnection)
    assert store._conn is store._conn is store.conn


def test_kept_connection_resolves_per_thread(store):
    kept = store._conn                    # kept on the main thread, like AccessStore does
    main_real = store._thread_conn()
    other_real = _in_thread(store._thread_conn)
    assert main_real is not other_real
    # using the kept object from another thread runs there, with no InterfaceError
    _in_thread(lambda: kept.execute("SELECT count(*) FROM tickets").fetchall())


def test_kept_connection_transactions_are_per_thread(store):
    kept = store._conn
    t = store.create_ticket("seed")
    kept.execute("UPDATE tickets SET title = 'uncommitted' WHERE id = ?", (t.id,))
    assert kept.in_transaction
    # another thread sees no open transaction and the committed title
    assert _in_thread(lambda: kept.in_transaction) is False
    seen = _in_thread(lambda: kept.execute(
        "SELECT title FROM tickets WHERE id = ?", (t.id,)).fetchall()[0][0])
    assert seen == "seed"
    kept.rollback()


def test_concurrent_writers_never_wedge_or_misuse(store):
    tickets = [store.create_ticket(f"t{i}") for i in range(4)]
    kept = store._conn
    errors, n_threads, per_thread = [], 8, 25

    def worker(k):
        try:
            for i in range(per_thread):
                tid = tickets[(k + i) % len(tickets)].id
                store.append_event(tid, "comment", payload={"text": f"{k}-{i}"})
                kept.execute("SELECT count(*) FROM events").fetchall()
        except Exception as exc:  # noqa: BLE001 - collected for the assert
            errors.append(repr(exc))

    threads = [threading.Thread(target=worker, args=(k,)) for k in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(120)
    assert errors == []
    total = store._conn.execute(
        "SELECT count(*) FROM events WHERE kind = 'comment'").fetchall()[0][0]
    assert total == n_threads * per_thread


def test_close_closes_every_thread_connection(store):
    _in_thread(store._thread_conn)
    real = store._thread_conn()
    store.close()
    with pytest.raises(sqlite3.ProgrammingError):
        real.execute("SELECT 1")


def test_memory_store_shares_its_one_connection():
    """":memory:" is test-only: the data lives in one connection, shared by threads."""
    s = Store(":memory:")
    try:
        t = s.create_ticket("m")
        assert _in_thread(lambda: s.get_ticket(t.id).title) == "m"
        assert _in_thread(s._thread_conn) is s._thread_conn()
    finally:
        s.close()


def test_with_statement_yields_the_stand_in(store):
    with store.conn as c:
        assert c is store.conn
        c.execute("SELECT 1").fetchall()
    _in_thread(lambda: c.execute("SELECT count(*) FROM tickets").fetchall())
