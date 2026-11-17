"""ticket_hub store — SQLite (WAL), append-only events, single writer.

The Store is the only writer of hub state. Events are immutable: UPDATE/DELETE
are blocked by DB triggers and there are no update/delete APIs for events.
Ticket rows denormalize ``status``/``updated_at`` for cheap reads; the event
chain remains the source of truth for history.

Stdlib only. Python 3.12+. See DESIGN.md for the contract.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone

from .model import (
    ACTOR_KINDS,
    EDGE_TYPES,
    GRAPH_EDGE_TYPES,
    Actor,
    Edge,
    Event,
    Status,
    SYSTEM_ACTOR,
    ThreadBinding,
    Ticket,
    WikiPage,
    assert_transition,
    check_label,
    coerce_status,
    new_id,
)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


# ---------------------------------------------------------------------------
# I1 legacy-row tolerance (incident cluster I1, 2026-11-12): rows written
# before actor-kind validation may store NULL kind (and NULL/invalid status).
# READS tolerate them so list/get/events never 400 with
# "unknown actor kind: None" / "None is not a valid Status". WRITES stay fail
# closed: the API rejects missing/unknown actor kind at ticket creation, and
# only the guarded normalize_legacy_actor_kinds() mutates legacy rows.
# ---------------------------------------------------------------------------

#: Read-time default for legacy rows whose stored actor kind is NULL/invalid.
LEGACY_ACTOR_KIND = "system"
#: Read-time default for legacy rows whose stored status is NULL/invalid.
LEGACY_TICKET_STATUS = Status.DRAFT.value
_VALID_STATUS_VALUES = frozenset(s.value for s in Status)


def _tolerant_actor_kind(value: "str | None") -> str:
    """Read-time actor kind: NULL or unknown legacy values read as 'system'."""
    return value if value in ACTOR_KINDS else LEGACY_ACTOR_KIND


def _tolerant_status(value: "str | None") -> Status:
    """Read-time status: NULL or unknown legacy values read as DRAFT."""
    if value in _VALID_STATUS_VALUES:
        return Status(value)
    return Status(LEGACY_TICKET_STATUS)


#: Idempotent column migration (ACCESS-P1-CONTRACT §1): table -> [(column, ddl suffix)].
#: Run after _SCHEMA in Store.__init__ via PRAGMA table_info; only missing
#: columns are added; rows are never rewritten. ADD COLUMN does not fire the
#: append-only triggers (they are BEFORE UPDATE/DELETE).
_MIGRATIONS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    ("tickets", (("label", "TEXT NOT NULL DEFAULT 'internal'"),
                 ("deleted_at", "TEXT"),  # S4: soft-delete tombstone (NULL = live)
                 )),
    ("events", (("label", "TEXT NOT NULL DEFAULT 'internal'"),)),
    ("wiki_pages", (("label", "TEXT NOT NULL DEFAULT 'internal'"),)),
    ("actors", (
        ("realm", "TEXT NOT NULL DEFAULT 'internal'"),
        ("clearance", "TEXT NOT NULL DEFAULT 'internal'"),
        ("sponsor_id", "TEXT"),
        ("expires_at", "TEXT"),
        ("is_owner", "INTEGER NOT NULL DEFAULT 0"),
    )),
)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS actors (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    family TEXT NOT NULL DEFAULT 'unknown',
    kind TEXT NOT NULL DEFAULT 'agent'
);

CREATE TABLE IF NOT EXISTS tickets (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    priority TEXT NOT NULL DEFAULT 'normal',
    owner_id TEXT REFERENCES actors(id),
    tags TEXT NOT NULL DEFAULT '[]',
    families TEXT NOT NULL DEFAULT '[]',
    external_refs TEXT NOT NULL DEFAULT '{}',
    artifacts TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS edges (
    id TEXT PRIMARY KEY,
    src TEXT NOT NULL REFERENCES tickets(id),
    dst TEXT NOT NULL REFERENCES tickets(id),
    type TEXT NOT NULL,
    meta TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE (src, dst, type)
);

CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    ticket_id TEXT NOT NULL REFERENCES tickets(id),
    ts TEXT NOT NULL,
    actor_id TEXT,
    actor_json TEXT,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_events_ticket ON events (ticket_id, seq);

-- Append-only enforcement: no updates, no deletes, ever.
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
BEGIN
    SELECT RAISE(ABORT, 'events are append-only: update forbidden');
END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN
    SELECT RAISE(ABORT, 'events are append-only: delete forbidden');
END;

CREATE TABLE IF NOT EXISTS thread_bindings (
    id TEXT PRIMARY KEY,
    ticket_id TEXT NOT NULL REFERENCES tickets(id),
    platform TEXT NOT NULL,
    room_id TEXT NOT NULL,
    thread_id TEXT,
    root_msg_id TEXT,
    created_at TEXT NOT NULL
);

-- Wiki (DESIGN-v2 pillar 1): append-only page revisions. Current page for a
-- slug = max(rev). Same durability ethos as events: no UPDATE/DELETE.
CREATE TABLE IF NOT EXISTS wiki_pages (
    page_id TEXT PRIMARY KEY,
    slug TEXT NOT NULL,
    rev INTEGER NOT NULL,
    title TEXT NOT NULL,
    body_md TEXT NOT NULL DEFAULT '',
    tags TEXT NOT NULL DEFAULT '[]',
    actor TEXT NOT NULL DEFAULT '',
    ts TEXT NOT NULL,
    UNIQUE (slug, rev)
);
CREATE INDEX IF NOT EXISTS idx_wiki_slug ON wiki_pages (slug, rev);

CREATE TRIGGER IF NOT EXISTS wiki_pages_no_update BEFORE UPDATE ON wiki_pages
BEGIN
    SELECT RAISE(ABORT, 'wiki pages are append-only: update forbidden');
END;
CREATE TRIGGER IF NOT EXISTS wiki_pages_no_delete BEFORE DELETE ON wiki_pages
BEGIN
    SELECT RAISE(ABORT, 'wiki pages are append-only: delete forbidden');
END;
"""


class ThreadConnection:
    """A stand-in for one sqlite3 connection that always acts on the calling
    thread's own connection.

    The hub's components (AccessStore, the Aerial stores, the gateway) keep the
    connection object they are given. With one real connection shared across
    FastAPI's worker threads, their implicit transactions interleaved and
    writes wedged with "database is locked"; with per-thread connections, a
    kept connection was used from other threads and raised
    ``sqlite3.InterfaceError`` (2026-11-11). Handing out this one stable object
    keeps both promises: anything that holds it, from any thread, runs on that
    thread's connection and transaction.
    """

    __slots__ = ("_get",)

    def __init__(self, get) -> None:
        object.__setattr__(self, "_get", get)

    def __getattr__(self, name):
        return getattr(self._get(), name)

    def __setattr__(self, name, value) -> None:
        setattr(self._get(), name, value)

    def __enter__(self):
        self._get().__enter__()
        return self          # the stand-in, so `with conn as c:` keeps the per-thread guarantee

    def __exit__(self, *exc):
        return self._get().__exit__(*exc)

    def __repr__(self) -> str:
        return "<ThreadConnection for the calling thread>"


class Store:
    """SQLite store for the ticket graph, safe across request threads.

    ``Store(path)`` opens (creating if needed) a WAL-mode database at *path*.
    Each thread gets its own connection and so its own transaction; ``_conn``
    and ``conn`` return one stable :class:`ThreadConnection` that resolves to
    the calling thread's connection. ``":memory:"`` is for tests only: an
    in-memory database lives in one connection, so that one connection is
    shared by every thread and is not safe for concurrent writers (production
    always uses a file).
    """

    def __init__(self, db_path: str) -> None:
        self.db_path = str(db_path)
        self._local = threading.local()
        self._opened: list[sqlite3.Connection] = []
        self._opened_lock = threading.Lock()
        self._shared = None
        if self.db_path == ":memory:":
            self._shared = self._open()
        self._proxy = ThreadConnection(self._thread_conn)
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.commit()

    def _open(self) -> sqlite3.Connection:
        """A new connection with the hub's settings (WAL, foreign keys, 30 s busy wait)."""
        conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=30.0)
        try:
            conn.row_factory = sqlite3.Row
            if self.db_path != ":memory:":
                conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=30000")
        except sqlite3.Error:
            conn.close()
            raise
        with self._opened_lock:
            self._opened.append(conn)
        return conn

    def _thread_conn(self) -> sqlite3.Connection:
        """The calling thread's connection, opened on first use."""
        if self._shared is not None:
            return self._shared
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._open()
            self._local.conn = conn
        return conn

    # -- schema introspection -------------------------------------------------

    def _migrate(self) -> None:
        """Add missing label/access columns idempotently (never rewrites rows)."""
        for table, cols in _MIGRATIONS:
            have = {r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})")}
            for name, ddl in cols:
                if name not in have:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

    # -- lifecycle ----------------------------------------------------------

    @property
    def _conn(self) -> ThreadConnection:
        """The calling thread's connection, through the stable stand-in."""
        return self._proxy

    @property
    def conn(self) -> ThreadConnection:
        """Read-only access to the calling thread's connection (callers must
        not keep it across close(); mirrors the StoreGateway.conn property)."""
        return self._proxy

    def _one(self, sql: str, params: tuple = ()) -> "sqlite3.Row | None":
        """Run a read and fully exhaust the cursor (first row or None).

        A fetchone() on a query that can return several rows leaves the
        statement open, so its read transaction never ends and a later write
        can fail with "database is locked". fetchall() always closes it.
        """
        rows = self._conn.execute(sql, params).fetchall()
        return rows[0] if rows else None

    def close(self) -> None:
        """Close every connection this store opened, on any thread."""
        with self._opened_lock:
            opened, self._opened = self._opened, []
        for conn in opened:
            try:
                conn.close()
            except sqlite3.Error:
                pass
        self._local = threading.local()

    # -- actors -------------------------------------------------------------

    def ensure_actor(self, actor: Actor | None) -> Actor:
        """Upsert *actor* (identity by id) and return it. None -> SYSTEM_ACTOR.
        On conflict only ``name`` and ``family`` are updated; ``kind`` is never
        overwritten — kind now drives security decisions (act_as relay targets
        must not be able to flip a human's kind)."""
        if actor is None:
            actor = SYSTEM_ACTOR
        self._conn.execute(
            """INSERT INTO actors (id, name, family, kind) VALUES (?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                 name=excluded.name, family=excluded.family""",
            (actor.id, actor.name, actor.family, actor.kind),
        )
        return actor

    def get_actor(self, actor_id: str) -> Actor | None:
        row = self._conn.execute("SELECT * FROM actors WHERE id = ?", (actor_id,)).fetchone()
        if row is None:
            return None
        # Actor carries only its model fields; access columns (realm, ...) on
        # the actors table belong to ticket_hub.access, not to Actor.
        data = {k: row[k] for k in row.keys() if k in ("id", "name", "family", "kind")}
        # I1: legacy rows may store NULL kind — read as 'system', never 400.
        data["kind"] = _tolerant_actor_kind(data.get("kind"))
        return Actor(**data)

    # -- tickets ------------------------------------------------------------

    def create_ticket(
        self,
        title: str,
        body: str = "",
        parent: str | None = None,
        owner: Actor | None = None,
        tags: list[str] | None = None,
        priority: str = "normal",
        families: list[str] | None = None,
        external_refs: dict | None = None,
        artifacts: list | None = None,
        actor: Actor | None = None,
        *,
        label: str = "internal",
        via: str | None = None,
    ) -> Ticket:
        """Create a ticket; when *parent* (a ticket id) is given, link it with
        a ``parent_of`` edge. Raises KeyError if the parent does not exist and
        ValueError on a cycle (impossible for a fresh child; guarded anyway).
        Emits a ``created`` event. *label* must be a supported label (checked);
        children never inherit the parent's label — they default to 'internal'
        unless a label is passed explicitly."""
        if not title or not title.strip():
            raise ValueError("ticket title must be non-empty")
        check_label(label)
        if parent is not None:
            if self.get_ticket(parent) is None:
                raise KeyError(f"parent ticket not found: {parent}")
        owner = self.ensure_actor(owner)
        tid = new_id("TK")
        now = _utcnow()
        families = list(families or [])
        if owner.family and owner.family not in families:
            families.append(owner.family)
        self._conn.execute(
            """INSERT INTO tickets (id, title, body, status, priority, owner_id,
                                    tags, families, external_refs, artifacts,
                                    created_at, updated_at, label)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                tid,
                title,
                body,
                Status.DRAFT.value,
                priority,
                owner.id,
                json.dumps(list(tags or [])),
                json.dumps(families),
                json.dumps(external_refs or {}),
                json.dumps(artifacts or []),
                now,
                now,
                label,
            ),
        )
        if parent is not None:
            self._insert_edge(parent, tid, "parent_of")
        self._append_event(
            tid,
            "created",
            actor=actor or owner,
            payload={"title": title, "parent": parent, "tags": list(tags or [])},
            label=label,
            via=via,
        )
        self._conn.commit()
        ticket = self.get_ticket(tid)
        assert ticket is not None
        return ticket

    def get_ticket(self, ticket_id: str) -> "Ticket | None":
        row = self._conn.execute(
            "SELECT * FROM tickets WHERE id = ? AND deleted_at IS NULL",
            (ticket_id,)).fetchone()
        return self._row_to_ticket(row) if row else None

    def _require_ticket(self, ticket_id: str) -> Ticket:
        t = self.get_ticket(ticket_id)
        if t is None:
            raise KeyError(f"ticket not found: {ticket_id}")
        return t

    def delete_ticket(self, ticket_id: str, *, actor: "Actor | None",
                      via: str | None = None) -> dict:
        """S4: remove a ticket from every read path (buglist 2026-11-14).

        Tombstone soft-delete: ``tickets.deleted_at`` is stamped and the
        ticket disappears from get/list/board, its edges and thread bindings
        are removed. The ticket's events are NOT deleted — they are
        append-only by design (events_no_delete trigger, SECURITY.md treats
        stored-event deletion as a reportable bug) — so the row itself stays
        as an audit anchor while every read path reports the ticket gone.
        Raises KeyError (→ 404 at the API layer) when missing or already
        deleted. Returns the tombstone record.
        """
        row = self._one(
            "SELECT id, deleted_at FROM tickets WHERE id = ?", (ticket_id,))
        if row is None or row["deleted_at"]:
            raise KeyError(f"ticket not found: {ticket_id}")
        ts = _utcnow()
        # Append the tombstone event FIRST: append_event validates the ticket
        # via get_ticket(), which filters deleted rows — so it must run while
        # the ticket is still live.
        self.append_event(
            ticket_id, kind="ticket_deleted", actor=actor, via=via,
            payload={"deleted_at": ts})
        self._conn.execute(
            "UPDATE tickets SET deleted_at = ? WHERE id = ?", (ts, ticket_id))
        self._conn.execute(
            "DELETE FROM edges WHERE src = ? OR dst = ?", (ticket_id, ticket_id))
        self._conn.execute(
            "DELETE FROM thread_bindings WHERE ticket_id = ?", (ticket_id,))
        self._conn.commit()
        return {"id": ticket_id, "deleted": True, "deleted_at": ts}

    # Public alias used by projections (lineage.py) and lanes B/C.
    require_ticket = _require_ticket

    def list_tickets(
        self,
        status: "Status | str | None" = None,
        family: str | None = None,
        tag: str | None = None,
        q: str | None = None,
    ) -> "list[Ticket]":
        """Filter by status, family (owner family or declared families),
        tag, and/or free-text q (title/body/id substring)."""
        sql = ["SELECT * FROM tickets WHERE 1=1 AND deleted_at IS NULL"]
        args: list = []
        if status is not None:
            sql.append("AND status = ?")
            args.append(coerce_status(status).value)
        if family is not None:
            sql.append(
                """AND (owner_id IN (SELECT id FROM actors WHERE family = ?)
                        OR EXISTS (SELECT 1 FROM json_each(families) WHERE value = ?))"""
            )
            args.extend([family, family])
        if tag is not None:
            sql.append("AND EXISTS (SELECT 1 FROM json_each(tags) WHERE value = ?)")
            args.append(tag)
        if q is not None:
            sql.append("AND (title LIKE ? OR body LIKE ? OR id LIKE ?)")
            like = f"%{q}%"
            args.extend([like, like, like])
        sql.append("ORDER BY created_at ASC, rowid ASC")
        rows = self._conn.execute(" ".join(sql), args).fetchall()
        return [self._row_to_ticket(r) for r in rows]

    def update_ticket(
        self,
        ticket_id: str,
        status: "Status | str | None" = None,
        priority: str | None = None,
        title: str | None = None,
        body: str | None = None,
        tags: list[str] | None = None,
        external_refs: dict | None = None,
        artifacts: list | None = None,
        actor: Actor | None = None,
        *,
        via: str | None = None,
    ) -> "Ticket":
        """Patch mutable fields. A status change must be an allowed transition
        (ValueError otherwise) and emits ``status_changed``; other field
        changes emit ``updated``. Bumps denormalized ``updated_at``."""
        t = self._require_ticket(ticket_id)
        changes: dict = {}
        new_status = None
        if status is not None:
            new_status = coerce_status(status)
            if new_status is not t.status:
                assert_transition(t.status, new_status)
                changes["status"] = {"from": t.status.value, "to": new_status.value}
        if priority is not None and priority != t.priority:
            changes["priority"] = {"from": t.priority, "to": priority}
        if title is not None and title != t.title:
            if not title.strip():
                raise ValueError("ticket title must be non-empty")
            changes["title"] = {"from": t.title, "to": title}
        if body is not None and body != t.body:
            changes["body"] = {"to_length": len(body)}
        if tags is not None and tags != t.tags:
            changes["tags"] = {"from": t.tags, "to": list(tags)}
        if external_refs is not None:
            changes["external_refs"] = {"to_keys": sorted(external_refs)}
        if artifacts is not None and artifacts != t.artifacts:
            changes["artifacts"] = {"from": t.artifacts, "to": list(artifacts)}

        if changes:
            now = _utcnow()
            self._conn.execute(
                """UPDATE tickets SET
                     title = COALESCE(?, title),
                     body = COALESCE(?, body),
                     status = COALESCE(?, status),
                     priority = COALESCE(?, priority),
                     tags = COALESCE(?, tags),
                     external_refs = COALESCE(?, external_refs),
                     artifacts = COALESCE(?, artifacts),
                     updated_at = ?
                   WHERE id = ?""",
                (
                    title,
                    body,
                    new_status.value if new_status is not None else None,
                    priority,
                    json.dumps(tags) if tags is not None else None,
                    json.dumps(external_refs) if external_refs is not None else None,
                    json.dumps(artifacts) if artifacts is not None else None,
                    now,
                    ticket_id,
                ),
            )
            if "status" in changes:
                self._append_event(
                    ticket_id, "status_changed", actor=actor,
                    payload={"from": changes["status"]["from"], "to": changes["status"]["to"]},
                    via=via,
                )
            other = {k: v for k, v in changes.items() if k != "status"}
            if other:
                self._append_event(ticket_id, "updated", actor=actor,
                                   payload={"fields": sorted(other)}, via=via)
            self._conn.commit()
        return self._require_ticket(ticket_id)

    # -- edges / graph --------------------------------------------------------

    def _insert_edge(self, src: str, dst: str, type: str, meta: dict | None = None) -> Edge:
        if type not in EDGE_TYPES:
            raise ValueError(f"unknown edge type: {type!r}")
        self._require_ticket(src)
        self._require_ticket(dst)
        if src == dst:
            raise ValueError(f"self-edge rejected: {src} -> {dst}")
        if type == "parent_of":
            existing = self._one(
                "SELECT src FROM edges WHERE dst = ? AND type = 'parent_of'", (dst,)
            )
            if existing is not None:
                raise ValueError(f"{dst} already has a parent: {existing['src']}")
            # cycle check: dst must not already be an ancestor of src
            if dst in self._ancestry_ids(src):
                raise ValueError(f"cycle rejected: {dst} is already an ancestor of {src}")
        cur = self._conn.execute(
            "INSERT INTO edges (id, src, dst, type, meta, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (new_id("ED"), src, dst, type, json.dumps(meta or {}), _utcnow()),
        )
        row = self._conn.execute("SELECT * FROM edges WHERE rowid = ?", (cur.lastrowid,)).fetchone()
        return self._row_to_edge(row)

    def add_edge(self, src: str, dst: str, type: str, meta: dict | None = None) -> Edge:
        """Add an edge. Rejects self-edges, unknown types, duplicate
        ``(src, dst, type)``, a second parent, and ``parent_of`` cycles
        (ValueError)."""
        dupe = self._one(
            "SELECT 1 FROM edges WHERE src = ? AND dst = ? AND type = ?", (src, dst, type)
        )
        if dupe is not None:
            raise ValueError(f"edge already exists: {src} -[{type}]-> {dst}")
        edge = self._insert_edge(src, dst, type, meta)
        self._append_event(
            src, "link", actor=SYSTEM_ACTOR,
            payload={"edge": edge.to_dict()},
        )
        self._conn.commit()
        return edge

    def get_edges(
        self, ticket_id: str, type: str | None = None, direction: str = "both"
    ) -> "list[Edge]":
        """Edges touching *ticket_id*. direction: 'out' | 'in' | 'both'."""
        self._require_ticket(ticket_id)
        clauses, args = [], []
        if direction in ("out", "both"):
            clauses.append("src = ?")
            args.append(ticket_id)
        if direction in ("in", "both"):
            clauses.append("dst = ?")
            args.append(ticket_id)
        sql = f"SELECT * FROM edges WHERE ({' OR '.join(clauses)})"
        if type is not None:
            sql += " AND type = ?"
            args.append(type)
        sql += " ORDER BY created_at ASC, rowid ASC"
        return [self._row_to_edge(r) for r in self._conn.execute(sql, args).fetchall()]

    def parent_of(self, ticket_id: str) -> "Ticket | None":
        row = self._one(
            "SELECT t.* FROM edges e JOIN tickets t ON t.id = e.src "
            "WHERE e.dst = ? AND e.type = 'parent_of'",
            (ticket_id,),
        )
        return self._row_to_ticket(row) if row else None

    def children(self, ticket_id: str) -> "list[Ticket]":
        rows = self._conn.execute(
            "SELECT t.* FROM edges e JOIN tickets t ON t.id = e.dst "
            "WHERE e.src = ? AND e.type = 'parent_of' ORDER BY e.created_at, e.rowid",
            (ticket_id,),
        ).fetchall()
        return [self._row_to_ticket(r) for r in rows]

    def _ancestry_ids(self, ticket_id: str) -> "list[str]":
        """Parent ids walking upward; nearest parent first. Guarded against
        pre-existing corruption with a visited set."""
        ids: list[str] = []
        seen = {ticket_id}
        current = ticket_id
        while True:
            parent = self.parent_of(current)
            if parent is None:
                return ids
            if parent.id in seen:
                raise ValueError(f"corrupt hierarchy: cycle detected at {parent.id}")
            ids.append(parent.id)
            seen.add(parent.id)
            current = parent.id

    def ancestry(self, ticket_id: str) -> "list[Ticket]":
        """Ancestors root-first (breadcrumb order: root ... parent)."""
        self._require_ticket(ticket_id)
        return [self._require_ticket(pid) for pid in reversed(self._ancestry_ids(ticket_id))]

    def descendants(self, ticket_id: str) -> "list[Ticket]":
        """All descendants, BFS order (depth ascending, then creation order)."""
        self._require_ticket(ticket_id)
        out: list[Ticket] = []
        queue = self.children(ticket_id)
        seen: set[str] = set()
        while queue:
            nxt: list[Ticket] = []
            for t in queue:
                if t.id in seen:
                    continue
                seen.add(t.id)
                out.append(t)
                nxt.extend(self.children(t.id))
            queue = nxt
        return out

    def related(self, ticket_id: str) -> "list[Ticket]":
        """Tickets joined by non-hierarchy edges (blocks/relates_to/duplicate_of
        plus the GRAPH_EDGE_TYPES semantic links), either direction,
        deduplicated."""
        self._require_ticket(ticket_id)
        types = ("blocks", "relates_to", "duplicate_of") + tuple(sorted(GRAPH_EDGE_TYPES))
        placeholders = ", ".join("?" for _ in types)
        rows = self._conn.execute(
            "SELECT t.* FROM edges e JOIN tickets t "
            "  ON t.id = CASE WHEN e.src = ? THEN e.dst ELSE e.src END "
            "WHERE (e.src = ? OR e.dst = ?) "
            f"  AND e.type IN ({placeholders}) "
            "ORDER BY e.created_at, e.rowid",
            (ticket_id, ticket_id, ticket_id, *types),
        ).fetchall()
        seen: set[str] = set()
        out: list[Ticket] = []
        for r in rows:
            t = self._row_to_ticket(r)
            if t.id not in seen:
                seen.add(t.id)
                out.append(t)
        return out

    # -- events ---------------------------------------------------------------

    def append_event(
        self, ticket_id: str, kind: str, actor: Actor | None = None, payload: dict | None = None, *,
        label: str | None = None, via: str | None = None,
    ) -> Event:
        """Append an immutable event to *ticket_id*. There is no API to update
        or delete events; DB triggers enforce this even for raw SQL.

        The event's label is decided AT INSERT: explicit *label* (checked) if
        given, else the ticket's current label; it is stored on the row and
        never derived at read time."""
        self._require_ticket(ticket_id)
        ev = self._append_event(ticket_id, kind, actor=actor, payload=payload,
                                label=label, via=via)
        self._conn.commit()
        return ev

    def _append_event(
        self, ticket_id: str, kind: str, actor: Actor | None = None, payload: dict | None = None, *,
        label: str | None = None, via: str | None = None,
    ) -> Event:
        if label is not None:
            check_label(label)
        else:
            # decide at insert: the ticket's current label (stored on the row)
            row = self._conn.execute(
                "SELECT label FROM tickets WHERE id = ?", (ticket_id,)
            ).fetchone()
            label = (row["label"] if row is not None and row["label"] else "internal")
        actor = self.ensure_actor(actor)
        ts = _utcnow()
        ev_id = new_id("EV")
        payload = dict(payload or {})
        actor_dict = actor.to_dict()
        if via is not None:
            actor_dict = {**actor_dict, "via": via}
        cur = self._conn.execute(
            "INSERT INTO events (id, ticket_id, ts, actor_id, actor_json, kind, payload, label) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (ev_id, ticket_id, ts, actor.id, json.dumps(actor_dict), kind, json.dumps(payload), label),
        )
        return Event(
            id=ev_id, ticket_id=ticket_id, ts=ts, actor=actor, kind=kind,
            payload=payload, seq=cur.lastrowid or 0, label=label, via=via,
        )

    def set_ticket_label(
        self, ticket_id: str, label: str, *, actor: Actor, reason: str = "",
        via: str | None = None,
    ) -> Ticket:
        """Change a ticket's label and record it. Appends a ``label_changed``
        event labelled ``internal`` with payload {from, to, reason}. An
        unchanged label is a no-op (no event). Returns the updated ticket."""
        t = self._require_ticket(ticket_id)
        check_label(label)
        if t.label == label:
            return t
        self._conn.execute("UPDATE tickets SET label = ? WHERE id = ?", (label, ticket_id))
        self._append_event(
            ticket_id, "label_changed", actor=actor,
            payload={"from": t.label, "to": label, "reason": reason},
            label="internal", via=via,
        )
        self._conn.commit()
        return self._require_ticket(ticket_id)

    def events(self, ticket_id: str) -> "list[Event]":
        """Full event chain for a ticket, ordered by (ts, seq)."""
        self._require_ticket(ticket_id)
        rows = self._conn.execute(
            "SELECT * FROM events WHERE ticket_id = ? ORDER BY ts ASC, seq ASC", (ticket_id,)
        ).fetchall()
        return [self._row_to_event(r) for r in rows]

    # -- thread bindings --------------------------------------------------------

    def bind_thread(
        self,
        ticket_id: str,
        platform: str,
        room_id: str,
        thread_id: str | None = None,
        root_msg_id: str | None = None,
    ) -> ThreadBinding:
        self._require_ticket(ticket_id)
        binding = ThreadBinding(
            id=new_id("TB"), ticket_id=ticket_id, platform=platform,
            room_id=room_id, thread_id=thread_id, root_msg_id=root_msg_id,
        )
        self._conn.execute(
            "INSERT INTO thread_bindings (id, ticket_id, platform, room_id, thread_id, root_msg_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (binding.id, ticket_id, platform, room_id, thread_id, root_msg_id, _utcnow()),
        )
        self._append_event(
            ticket_id, "link", actor=SYSTEM_ACTOR,
            payload={"bound_thread": binding.to_dict()},
        )
        self._conn.commit()
        return binding

    def bindings(self, ticket_id: str) -> "list[ThreadBinding]":
        self._require_ticket(ticket_id)
        rows = self._conn.execute(
            "SELECT * FROM thread_bindings WHERE ticket_id = ? ORDER BY created_at, rowid",
            (ticket_id,),
        ).fetchall()
        return [
            ThreadBinding(
                id=r["id"], ticket_id=r["ticket_id"], platform=r["platform"],
                room_id=r["room_id"], thread_id=r["thread_id"], root_msg_id=r["root_msg_id"],
            )
            for r in rows
        ]

    # -- wiki (DESIGN-v2 pillar 1, append-only revisions) ----------------------

    def put_page(
        self,
        slug: str,
        title: str,
        body_md: str = "",
        tags: list[str] | None = None,
        actor: str = "",
        *,
        label: str | None = None,
    ) -> WikiPage:
        """Append a new revision of *slug* (rev = current max + 1) and return
        it. Pages are append-only: like events, there is no update/delete API
        and DB triggers enforce it even for raw SQL. With ``label=None`` the
        revision keeps the label of the current latest revision (else
        ``internal``); an explicit label is checked."""
        if not slug or not slug.strip():
            raise ValueError("wiki slug must be non-empty")
        if not title or not title.strip():
            raise ValueError("wiki title must be non-empty")
        if label is not None:
            check_label(label)
        slug = slug.strip()
        row = self._conn.execute(
            "SELECT COALESCE(MAX(rev), 0) AS rev FROM wiki_pages WHERE slug = ?",
            (slug,),
        ).fetchone()
        rev = int(row["rev"]) + 1
        if label is None:
            if rev > 1:
                prev = self._conn.execute(
                    "SELECT label FROM wiki_pages WHERE slug = ? AND rev = ?", (slug, rev - 1)
                ).fetchone()
                label = (prev["label"] or "internal") if prev is not None else "internal"
            else:
                label = "internal"
        page_id = new_id("WP")
        ts = _utcnow()
        self._conn.execute(
            """INSERT INTO wiki_pages (page_id, slug, rev, title, body_md,
                                       tags, actor, ts, label)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                page_id,
                slug,
                rev,
                title,
                body_md,
                json.dumps(list(tags or [])),
                actor or "",
                ts,
                label,
            ),
        )
        self._conn.commit()
        return WikiPage(
            id=page_id, slug=slug, rev=rev, title=title, body_md=body_md,
            tags=list(tags or []), actor=actor or "", ts=ts, label=label,
        )

    def get_page(self, slug: str) -> "WikiPage | None":
        """Latest revision of *slug*, or None."""
        row = self._conn.execute(
            "SELECT * FROM wiki_pages WHERE slug = ? ORDER BY rev DESC LIMIT 1",
            (slug,),
        ).fetchone()
        return self._row_to_page(row) if row else None

    def list_pages(self) -> "list[WikiPage]":
        """Latest revision per slug, ordered by slug."""
        rows = self._conn.execute(
            """SELECT w.* FROM wiki_pages w
               JOIN (SELECT slug, MAX(rev) AS rev FROM wiki_pages GROUP BY slug) m
                 ON m.slug = w.slug AND m.rev = w.rev
               ORDER BY w.slug ASC"""
        ).fetchall()
        return [self._row_to_page(r) for r in rows]

    def page_history(self, slug: str) -> "list[WikiPage]":
        """All revisions of *slug*, oldest first."""
        rows = self._conn.execute(
            "SELECT * FROM wiki_pages WHERE slug = ? ORDER BY rev ASC",
            (slug,),
        ).fetchall()
        return [self._row_to_page(r) for r in rows]

    def search_pages(self, q: str) -> "list[WikiPage]":
        """Latest revisions matching *q* (case-insensitive substring on
        title, body, or tags), ordered by slug."""
        like = f"%{q}%"
        rows = self._conn.execute(
            """SELECT w.* FROM wiki_pages w
               JOIN (SELECT slug, MAX(rev) AS rev FROM wiki_pages GROUP BY slug) m
                 ON m.slug = w.slug AND m.rev = w.rev
               WHERE w.title LIKE ? OR w.body_md LIKE ? OR w.tags LIKE ?
               ORDER BY w.slug ASC""",
            (like, like, like),
        ).fetchall()
        return [self._row_to_page(r) for r in rows]

    # -- projections --------------------------------------------------------------

    def board(self) -> "dict[str, list[Ticket]]":
        """Tickets grouped by status value (all statuses present, possibly empty)."""
        grouped: dict[str, list[Ticket]] = {s.value: [] for s in Status}
        for t in self.list_tickets():
            grouped[t.status.value].append(t)
        return grouped

    def timeline(self, ticket_id: str) -> "list[dict]":
        """Digest-friendly event list: the ticket's own events merged with
        ``thread_message`` events from any ticket sharing one of its thread
        bindings (same platform + room + thread). Each entry is an event dict
        plus ``source_ticket``; ordered by (ts, seq)."""
        self._require_ticket(ticket_id)  # validation side effect only
        own = [
            {**ev.to_dict(), "source_ticket": ticket_id} for ev in self.events(ticket_id)
        ]
        foreign: list[dict] = []
        for b in self.bindings(ticket_id):
            peers = self._conn.execute(
                "SELECT ticket_id FROM thread_bindings "
                "WHERE platform = ? AND room_id = ? "
                "  AND IFNULL(thread_id, '') = IFNULL(?, '') AND ticket_id != ?",
                (b.platform, b.room_id, b.thread_id, ticket_id),
            ).fetchall()
            for p in peers:
                for ev in self.events(p["ticket_id"]):
                    if ev.kind == "thread_message":
                        foreign.append({**ev.to_dict(), "source_ticket": p["ticket_id"]})
        merged = own + foreign
        merged.sort(key=lambda e: (e["ts"], e["seq"]))
        return merged

    # -- I1 legacy-row report + guarded normalization -------------------------

    def report_legacy_rows(self) -> dict:
        """READ-ONLY I1 report: rows storing legacy NULL/invalid actor kind
        or ticket status. Never mutates anything; ids are sorted."""
        actors = [
            r["id"]
            for r in self._conn.execute(
                "SELECT id, kind FROM actors").fetchall()
            if r["kind"] not in ACTOR_KINDS
        ]
        statuses = [
            r["id"]
            for r in self._conn.execute(
                "SELECT id, status FROM tickets").fetchall()
            if r["status"] not in _VALID_STATUS_VALUES
        ]
        return {
            "legacy_actor_kind": {"count": len(actors), "ids": sorted(actors)},
            "legacy_ticket_status": {"count": len(statuses),
                                     "ids": sorted(statuses)},
        }

    def normalize_legacy_actor_kinds(self, *, by: str) -> dict:
        """Guarded I1 normalization: ONLY actor rows with kind IS NULL are
        defaulted to 'system' (decision: fail closed elsewhere — unknown
        non-NULL kinds are reported, never rewritten). Every ticket owned by
        a normalized actor is tagged in its external_refs meta with
        ``legacy_actor_kind_normalized: true``. Returns what changed."""
        rows = self._conn.execute(
            "SELECT id FROM actors WHERE kind IS NULL").fetchall()
        actor_ids = sorted(r["id"] for r in rows)
        tagged: list[str] = []
        for aid in actor_ids:
            for t in self._conn.execute(
                "SELECT id, external_refs FROM tickets WHERE owner_id = ?",
                (aid,),
            ).fetchall():
                refs = json.loads(t["external_refs"] or "{}")
                if not refs.get("legacy_actor_kind_normalized"):
                    refs["legacy_actor_kind_normalized"] = True
                    self._conn.execute(
                        "UPDATE tickets SET external_refs = ? WHERE id = ?",
                        (json.dumps(refs), t["id"]),
                    )
                    tagged.append(t["id"])
            self._conn.execute(
                "UPDATE actors SET kind = ? WHERE id = ?",
                (LEGACY_ACTOR_KIND, aid),
            )
        self._conn.commit()
        return {"normalized_actors": actor_ids,
                "tagged_tickets": sorted(tagged), "by": by}

    # -- row mappers ------------------------------------------------------------------

    def _row_to_ticket(self, row: sqlite3.Row) -> "Ticket":
        owner = None
        if row["owner_id"]:
            owner = self.get_actor(row["owner_id"])
        keys = row.keys()
        return Ticket(
            id=row["id"],
            title=row["title"],
            body=row["body"],
            # I1: legacy rows may store NULL/invalid status — read as DRAFT.
            status=_tolerant_status(row["status"]),
            priority=row["priority"],
            owner=owner,
            tags=json.loads(row["tags"]),
            families=json.loads(row["families"]),
            external_refs=json.loads(row["external_refs"]),
            artifacts=json.loads(row["artifacts"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            label=(row["label"] or "internal") if "label" in keys else "internal",
        )

    def _row_to_edge(self, row: sqlite3.Row) -> Edge:
        return Edge(
            id=row["id"], src=row["src"], dst=row["dst"], type=row["type"],
            meta=json.loads(row["meta"]),
        )

    def _row_to_event(self, row: sqlite3.Row) -> Event:
        actor = None
        via = None
        if row["actor_json"]:
            ad = json.loads(row["actor_json"])
            via = ad.pop("via", None)
            # I1: legacy event rows may carry a NULL/unknown kind — read as
            # 'system' instead of raising "unknown actor kind: None".
            ad["kind"] = _tolerant_actor_kind(ad.get("kind"))
            actor = Actor(**ad)
        keys = row.keys()
        return Event(
            id=row["id"], ticket_id=row["ticket_id"], ts=row["ts"], actor=actor,
            kind=row["kind"], payload=json.loads(row["payload"]), seq=row["seq"],
            label=(row["label"] or "internal") if "label" in keys else "internal",
            via=via,
        )

    def _row_to_page(self, row: sqlite3.Row) -> WikiPage:
        keys = row.keys()
        return WikiPage(
            id=row["page_id"],
            slug=row["slug"],
            rev=row["rev"],
            title=row["title"],
            body_md=row["body_md"],
            tags=json.loads(row["tags"]),
            actor=row["actor"],
            ts=row["ts"],
            label=(row["label"] or "internal") if "label" in keys else "internal",
        )
