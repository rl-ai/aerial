"""Aerial (rlai) Tickets-product storage on the hub's own database.

See docs/DESIGN-aerial-tickets.md. Nothing here is imported by the hub at
start-up: the Aerial packages load on first use from ``RLAI_PACKAGES_DIR``.

* :func:`load_packages` puts ``<dir>/<pkg>/src`` on ``sys.path``.
* :func:`hub_record_store` builds a ``rlai_tickets.Store`` subclass whose four
  storage hooks write to the hub's schema, so ``record()`` itself (validation,
  dedup by ``message_id``, ``seq_gap``, kind map) runs unchanged.
* :class:`IdentityStore` keeps C5 identity messages (append-only table).
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
from datetime import datetime, timezone
from typing import Any

from ticket_hub.model import Actor

PACKAGES_ENV = "RLAI_PACKAGES_DIR"
PACKAGES = ("rlai-contracts", "rlai-tickets", "rlai-operator", "rlai-runtime",
            "rlai-roles")

_LOAD_LOCK = threading.Lock()
#: id(conn) -> (conn, lock). The strong reference keeps the id from being
#: reused by a new connection (sqlite3.Connection has no weakref support).
_CONN_LOCKS: "dict[int, tuple[sqlite3.Connection, threading.RLock]]" = {}


def conn_lock(conn: sqlite3.Connection) -> threading.RLock:
    """One RLock per sqlite connection, shared by every Aerial writer on it
    (HubRecordStore and IdentityStore). The hub's own writers predate it and
    take no lock; this keeps the Aerial writers from interleaving with each
    other, not with them."""
    with _LOAD_LOCK:
        entry = _CONN_LOCKS.get(id(conn))
        if entry is None or entry[0] is not conn:
            entry = (conn, threading.RLock())
            _CONN_LOCKS[id(conn)] = entry
        return entry[1]


class RlaiUnavailable(RuntimeError):
    """The Aerial packages are not reachable from this hub process."""


def load_packages(packages_dir: "str | None" = None) -> str:
    """Add each Aerial package's ``src`` folder to ``sys.path`` (idempotent).

    Returns the packages folder. Raises :class:`RlaiUnavailable` when the
    folder is unset or lacks ``rlai-tickets``."""
    root = packages_dir or os.environ.get(PACKAGES_ENV, "").strip()
    if not root:
        raise RlaiUnavailable(f"{PACKAGES_ENV} is not set")
    root = os.path.abspath(os.path.expanduser(root))
    if not os.path.isdir(os.path.join(root, "rlai-tickets", "src")):
        raise RlaiUnavailable(f"no rlai-tickets package under {root}")
    with _LOAD_LOCK:
        for name in PACKAGES:
            src = os.path.join(root, name, "src")
            # front of sys.path: RLAI_PACKAGES_DIR wins over any other copy
            if os.path.isdir(src) and src not in sys.path:
                sys.path.insert(0, src)
    return root


def contracts():
    """The ``rlai_contracts`` module (as rlai_tickets loads it)."""
    load_packages()
    from rlai_tickets.store import store as _tstore
    module = _tstore._load_contracts()
    if module is None:
        raise RlaiUnavailable("rlai-contracts is not next to rlai-tickets")
    return module


# ---------------------------------------------------------------------------
# C2 records: rlai_tickets.Store.record() over the hub schema
# ---------------------------------------------------------------------------

_RECORD_STORE_CLASS = None


def _record_store_class():
    """Build (once) the Store subclass; needs the packages loaded."""
    global _RECORD_STORE_CLASS
    if _RECORD_STORE_CLASS is not None:
        return _RECORD_STORE_CLASS
    load_packages()
    from rlai_tickets.store.store import Store as TicketsStore

    class HubRecordStore(TicketsStore):
        """``rlai_tickets.Store`` whose storage hooks speak the hub schema.

        Only ``record()`` is meant to be called. The hooks overridden are the
        ones ``record()`` reaches: ``_ticket_by_id``, ``_ensure_actor``,
        ``_insert_event`` and ``_event_from_row``. The dedup and seq-gap
        queries read ``events.payload`` by ``json_extract`` and work on the
        hub table as written upstream.
        """

        def __init__(self, hub_store, contracts_module) -> None:
            super().__init__(hub_store._conn, lock=conn_lock(hub_store._conn),
                             contracts=contracts_module)
            self._hub = hub_store
            self._via: "str | None" = None
            self.inserted = False

        def record_as(self, message: dict, *, via: "str | None") -> tuple:
            """``record()``; returns ``(hub_event_dict, duplicate)``."""
            with self._lock:
                self._via = via
                self.inserted = False
                try:
                    event = super().record(message)
                finally:
                    self._via = None
                return event, not self.inserted

        # -- hooks ----------------------------------------------------------

        def _ticket_by_id(self, ticket_id, require=False):
            ticket = self._hub.get_ticket(ticket_id)
            if ticket is None and require:
                from rlai_tickets.store.errors import StoreError
                raise StoreError(f"no such ticket: {ticket_id}")
            return ticket

        def _ensure_actor(self, actor_id):
            # INSERT OR IGNORE semantics: never rename a known actor (the
            # hub's ensure_actor would overwrite the C5 display name).
            if self._hub.get_actor(actor_id) is None:
                self._hub.ensure_actor(Actor(id=actor_id, name=actor_id,
                                             family="aerial", kind="agent"))

        def _insert_event(self, ticket_id, kind, payload, actor_id):
            actor = self._hub.get_actor(actor_id) or Actor(
                id=actor_id, name=actor_id, family="aerial", kind="agent")
            via = self._via if self._via and self._via != actor_id else None
            event = self._hub._append_event(ticket_id, kind, actor=actor,
                                            payload=payload, via=via)
            self.inserted = True
            return event.to_dict()

        def _event_from_row(self, row):
            return self._hub._row_to_event(row).to_dict()

    _RECORD_STORE_CLASS = HubRecordStore
    return HubRecordStore


def hub_record_store(hub_store):
    """A ``HubRecordStore`` on *hub_store* (a ``ticket_hub.store.Store``)."""
    return _record_store_class()(hub_store, contracts())


# ---------------------------------------------------------------------------
# C5 identities
# ---------------------------------------------------------------------------

#: One statement per entry, run with execute(): executescript() would
#: COMMIT whatever another writer has pending on the shared connection.
IDENTITY_DDL = (
    """CREATE TABLE IF NOT EXISTS rlai_identities (
    message_id   TEXT PRIMARY KEY,
    employee_id  TEXT NOT NULL,
    display_name TEXT NOT NULL,
    kind         TEXT NOT NULL,
    token_ref    TEXT NOT NULL,
    producer     TEXT NOT NULL,
    sent_at      TEXT NOT NULL,
    received_at  TEXT NOT NULL,
    received_by  TEXT,
    message      TEXT NOT NULL
)""",
    """CREATE INDEX IF NOT EXISTS idx_rlai_identities_employee
    ON rlai_identities (employee_id)""",
    """CREATE TRIGGER IF NOT EXISTS rlai_identities_no_update
BEFORE UPDATE ON rlai_identities
BEGIN SELECT RAISE(ABORT, 'rlai_identities are append-only'); END""",
    """CREATE TRIGGER IF NOT EXISTS rlai_identities_no_delete
BEFORE DELETE ON rlai_identities
BEGIN SELECT RAISE(ABORT, 'rlai_identities are append-only'); END""",
)


def ensure_identity_schema(conn: sqlite3.Connection) -> None:
    """Create the identity table and its triggers. Start-up only: the app
    calls it before serving, while no other writer can have a transaction
    open on *conn*, so the commit here covers only this DDL."""
    with conn_lock(conn):
        for statement in IDENTITY_DDL:
            conn.execute(statement)
        conn.commit()


class IdentityConflict(ValueError):
    """The message's kind differs from the stored actor's kind."""


class IdentityStore:
    """Append-only C5 identity log on the hub connection."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._lock = conn_lock(conn)

    def _require_schema(self) -> None:
        """Never DDL on a request: the table is created once at start-up
        (:func:`ensure_identity_schema`). Missing -> RlaiUnavailable (503)."""
        present = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table'"
            " AND name = 'rlai_identities'").fetchone()
        if present is None:
            raise RlaiUnavailable("rlai_identities table missing; restart "
                                  "the hub to create it")

    def is_registered(self, employee_id: str) -> bool:
        with self._lock:
            self._require_schema()
            row = self._conn.execute(
                "SELECT 1 FROM rlai_identities WHERE employee_id = ? LIMIT 1",
                (employee_id,)).fetchone()
        return row is not None

    def registrar(self, employee_id: str) -> "str | None":
        """Who delivered the employee's first identity (None if unknown or
        delivered with auth off)."""
        with self._lock:
            self._require_schema()
            row = self._conn.execute(
                "SELECT received_by FROM rlai_identities WHERE employee_id = ?"
                " ORDER BY received_at, rowid LIMIT 1",
                (employee_id,)).fetchone()
        return row["received_by"] if row is not None else None

    def accept(self, message: dict, *, access, received_by: "str | None"
               ) -> bool:
        """Store a validated C5 message; create the actor when new.

        Returns True for a first delivery, False for a repeat
        ``message_id`` (nothing written). Raises :class:`IdentityConflict`
        when the actor exists with another ``kind``."""
        with self._lock:
            self._require_schema()
            seen = self._conn.execute(
                "SELECT 1 FROM rlai_identities WHERE message_id = ?",
                (message["message_id"],)).fetchone()
            if seen is not None:
                return False
            employee_id = message["employee_id"]
            row = self._conn.execute(
                "SELECT kind, family FROM actors WHERE id = ?",
                (employee_id,)).fetchone()
            # A placeholder is the actor HubRecordStore._ensure_actor made for
            # an owner-posted record before any identity arrived: the first
            # C5 may set its kind and name.
            placeholder = (row is not None and row["family"] == "aerial"
                           and not self.is_registered(employee_id))
            if row is not None and not placeholder \
                    and row["kind"] != message["kind"]:
                raise IdentityConflict(
                    f"actor {employee_id} is kind {row['kind']!r}, "
                    f"message says {message['kind']!r}")
            try:
                # identity row first: create_actor commits, so both rows land
                # in one commit (a failure before it rolls both back)
                self._conn.execute(
                    "INSERT INTO rlai_identities (message_id, employee_id,"
                    " display_name, kind, token_ref, producer, sent_at,"
                    " received_at, received_by, message)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (message["message_id"], employee_id,
                     message["display_name"], message["kind"],
                     message["token_ref"], message["producer"],
                     message["sent_at"],
                     datetime.now(timezone.utc).isoformat(
                         timespec="milliseconds"),
                     received_by, json.dumps(message, sort_keys=True)))
                if placeholder:
                    self._conn.execute(
                        "UPDATE actors SET kind = ?, name = ? WHERE id = ?",
                        (message["kind"], message["display_name"],
                         employee_id))
                if row is None:
                    access.create_actor(
                        employee_id, message["display_name"],
                        family="aerial", kind=message["kind"],
                        by=received_by or "rlai-identity")
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise
            return True


# ---------------------------------------------------------------------------
# operator view: in-process getter + read-only product sources
# ---------------------------------------------------------------------------

def _c2_view(event: dict) -> dict:
    """An event holding a C2 record, as the operator view reads it."""
    record = (event.get("payload") or {}).get("record") or {}
    kind = record.get("event_type") or event.get("kind")
    payload = dict(event.get("payload") or {})
    if kind == "outcome":
        cost = record.get("cost_usd", record.get("provider_charge_usd"))
        payload = {"outcome": record.get("result"),
                   "employee_id": record.get("employee_id"),
                   "elapsed_s": record.get("elapsed_s"), "cost": cost}
    return {"kind": kind, "payload": payload}


class InProcessGetter:
    """``HubTickets``' GET-only getter, served from the hub store directly.

    Paths: ``/tickets[?status=&tag=]``, ``/tickets/{id}``,
    ``/tickets/{id}/events`` and ``/events?since=``. Anything else is None.
    """

    def __init__(self, gateway) -> None:
        self._gw = gateway
        self._store = getattr(gateway, "_store", gateway)

    def __call__(self, path: str) -> Any:
        from urllib.parse import parse_qs, unquote, urlsplit
        parts = urlsplit(path)
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        segs = [unquote(s) for s in parts.path.strip("/").split("/") if s]
        if segs == ["tickets"]:
            rows = self._gw.list(status=query.get("status"),
                                 tag=query.get("tag"))
            return [self._ticket(t) for t in rows]
        if len(segs) == 2 and segs[0] == "tickets":
            ticket = self._gw.get(segs[1])
            return self._ticket(ticket) if ticket else None
        if len(segs) == 3 and segs[0] == "tickets" and segs[2] == "events":
            if self._gw.get(segs[1]) is None:
                return []
            return [self._event(e.to_dict())
                    for e in self._store.events(segs[1])]
        if segs == ["events"]:
            return self._events_since(query.get("since") or "")
        return None

    def _ticket(self, ticket: dict) -> dict:
        tid = ticket.get("id")
        conn = self._store._conn
        blocks = [{"ticket_id": r["src"]} for r in conn.execute(
            "SELECT src FROM edges WHERE dst = ? AND type = 'blocks'",
            (tid,))]
        return {"ticket_id": tid, "title": ticket.get("title"),
                "status": ticket.get("status"), "holder": self._holder(tid),
                "updated_at": ticket.get("updated_at"), "blocks": blocks}

    def _holder(self, ticket_id: str) -> "str | None":
        for ev in reversed(self._store.timeline(ticket_id)):
            p = ev.get("payload") or {}
            if p.get("role") and p.get("action") == "assigned":
                return p.get("seat")
            if p.get("action") == "released":
                return None
        return None

    @staticmethod
    def _event(event: dict) -> dict:
        view = {"event_id": event.get("id"), "ticket_id": event.get("ticket_id"),
                "kind": event.get("kind"), "actor": event.get("actor"),
                "at": event.get("ts"), "payload": event.get("payload") or {}}
        if "record" in view["payload"]:
            view.update(_c2_view(event))
        return view

    def _events_since(self, since: str) -> list[dict]:
        # hub ts is isoformat with +00:00; since is ...Z. Compare as instants.
        cutoff = _parse_ts(since)
        if cutoff is None:
            return []  # the contract always sends since; never dump it all
        # prefilter on the UTC date-time prefix, then compare exactly below
        prefix = cutoff.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        rows = self._store._conn.execute(
            "SELECT * FROM events WHERE substr(ts, 1, 19) >= ? ORDER BY seq",
            (prefix,)).fetchall()
        out = []
        for row in rows:
            ev = self._store._row_to_event(row).to_dict()
            at = _parse_ts(ev.get("ts") or "")
            if at is None or at < cutoff:
                continue
            out.append(self._event(ev))
        return out


def _parse_ts(value: str) -> "datetime | None":
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    # a value without an offset is read as UTC (never compare naive/aware)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _ro_conn(path: str) -> sqlite3.Connection:
    if not path or not os.path.exists(path):
        raise RlaiUnavailable(f"database not found: {path or '(unset)'}")
    from pathlib import Path
    uri = Path(path).absolute().as_uri() + "?mode=ro"  # percent-encoded
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


class _Unavailable:
    """A source whose every call raises: build_view marks its panels."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def __getattr__(self, name):
        def fail(*_a, **_k):
            raise self._exc
        return fail


def build_operator_view(gateway, operator_id: str, *, now=None) -> dict:
    """The operator view document (rlai_operator.view.build_view).

    *gateway* is the app's store as ``get_store()`` returns it (normally a
    ``StoreGateway``); the getter uses its dict API and reaches the raw hub
    Store through ``gateway._store``."""
    load_packages()
    from rlai_operator.sources import HubTickets, RolesApi, RuntimeApi
    from rlai_operator.view import build_view

    tickets = HubTickets(InProcessGetter(gateway))
    clock = now or (lambda: datetime.now(timezone.utc))

    opened: list[sqlite3.Connection] = []
    registry = None
    try:
        from rlai_roles.registry import Registry
        conn = _ro_conn(os.environ.get("RLAI_ROLES_DB", ""))
        opened.append(conn)
        registry = Registry(conn, now=clock)
        roles: Any = RolesApi(registry)
    except Exception as exc:  # noqa: BLE001 - the panel shows the class
        roles = _Unavailable(exc)

    try:
        from rlai_runtime.runs import RunStore
        conn = _ro_conn(os.environ.get("RLAI_RUNTIME_DB", ""))
        opened.append(conn)
        store = RunStore(conn, threading.RLock(), None, "agent")
        ids = (lambda: [e.employee_id for e in registry.list()]) \
            if registry is not None else None
        runtime: Any = RuntimeApi(store, employee_ids=ids)
    except Exception as exc:  # noqa: BLE001
        runtime = _Unavailable(exc)

    try:
        return build_view(tickets, runtime, roles, now=clock,
                          operator_id=operator_id)
    finally:
        for conn in opened:
            conn.close()
