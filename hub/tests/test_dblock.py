"""Concurrent use of one Store must not hand a caller another caller's rows
(fix batch item 9: 'unknown actor kind: None' under load)."""

import threading

from ticket_hub.dblock import DB_LOCK, serialized
from ticket_hub.model import Actor
from ticket_hub.store import Store


def test_methods_hold_the_lock():
    seen = []

    @serialized
    class Probe:
        def __init__(self):
            seen.append(DB_LOCK._is_owned())

        def run(self):
            seen.append(DB_LOCK._is_owned())

        @staticmethod
        def helper():
            seen.append(DB_LOCK._is_owned())

        @classmethod
        def build(cls):
            seen.append(DB_LOCK._is_owned())

    Probe().run()
    Probe.helper()
    Probe.build()
    assert seen == [True, True, True, True]
    assert not DB_LOCK._is_owned()


def test_concurrent_writes_and_reads_return_their_own_rows(tmp_path):
    store = Store(str(tmp_path / "hub.db"))
    errors = []
    start = threading.Barrier(8)

    def worker(n):
        start.wait()
        try:
            for i in range(100):
                aid = f"w{n}-{i}"
                store.ensure_actor(Actor(id=aid, name=aid, family=f"f{n}", kind="agent"))
                got = store.get_actor(aid)
                if got is None or got.id != aid or got.family != f"f{n}" or got.kind != "agent":
                    errors.append((aid, got))
                ticket = store.create_ticket(f"t{n}-{i}")
                if store.get_ticket(ticket.id).title != f"t{n}-{i}":
                    errors.append(("ticket", ticket.id))
        except Exception as exc:  # any interleaving error fails the test
            errors.append(repr(exc))

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert len(store.list_tickets()) == 8 * 100
