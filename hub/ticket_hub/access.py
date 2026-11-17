"""ticket_hub access core — ACCESS-P1-CONTRACT §2 (lane W2).

Pure stdlib. Operates on the Store's sqlite3 connection: ``AccessStore(conn)``
creates its six append-only tables idempotently in ``__init__``. Tokens are
handled per contract: ``thk_`` + ``secrets.token_urlsafe(32)``, stored only as
sha256 hex (plus a 12-char display prefix), compared by hash lookup only. The
token string is returned once by ``issue_credential`` and is never stored,
logged, or included in an exception message.

All external visibility filtering happens in SQL WHERE clauses (recursive CTE
over ``parent_of`` edges for subtree grants) — never fetch-all-then-filter in
Python.

Stricter readings chosen where the contract leaves room (noted here, per
"deviations are reported, never silent"):
- Grant verbs must be a non-empty subset of {"read"} in P1 (empty verbs would
  be a vacuous grant). max_label of a grant is always "external" (forced).
- Credential / grant expiry comparison uses ``expires_at > now`` strictly.
- actor_status status values are restricted to active|suspended|revoked; a
  suspended or revoked actor fails ``actor_live``.
- ExternalReader methods run the full visibility decision as one SQL query per
  call; they never load unfiltered rows.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import secrets
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .model import Actor, new_id

# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------

TOKEN_PREFIX = "thk_"
#: first 12 chars of the token, stored for display only ("thk_" + 8 body chars)
TOKEN_DISPLAY_LEN = 12

REALM_INTERNAL = "internal"
REALM_EXTERNAL = "external"

#: labels an external viewer can ever see (P1; contract §0/§2)
EXTERNAL_VISIBLE_LABELS: tuple[str, ...] = ("public", "external")

ACTOR_STATUSES: frozenset[str] = frozenset({"active", "suspended", "revoked"})
ACTOR_KINDS: frozenset[str] = frozenset({"agent", "human", "system"})
GRANT_SCOPE_TYPES: frozenset[str] = frozenset({"ticket", "subtree", "wiki_page"})
P1_VERBS: frozenset[str] = frozenset({"read"})


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _canon_ts(value: str) -> str:
    """Parse any ISO-8601 timestamp (trailing 'Z' accepted; naive = UTC) and
    re-emit it in exactly the store's _utcnow() format: UTC, '+00:00',
    millisecond precision. ValueError on anything unparseable. All stored
    expires_at values and all caller-supplied `now` arguments go through this
    so expiry comparisons are never lexical across differing formats."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"invalid timestamp: {value!r}")
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(f"invalid timestamp: {value!r}") from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def _canon_now(now: "str | None") -> str:
    """Canonicalise a caller-supplied `now`, defaulting to the current time."""
    return _canon_ts(now) if now is not None else _utcnow()


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# schema (idempotent; every table is append-only via BEFORE UPDATE/DELETE
# triggers that RAISE(ABORT) — revocation is always a new row, never an UPDATE)
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS credentials (
    id TEXT PRIMARY KEY,
    actor_id TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    prefix TEXT NOT NULL,
    scopes TEXT NOT NULL DEFAULT '[]',
    act_as_allow TEXT NOT NULL DEFAULT '[]',
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT
);
CREATE TABLE IF NOT EXISTS credential_revocations (
    id TEXT PRIMARY KEY,
    credential_id TEXT NOT NULL,
    actor TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS grants (
    id TEXT PRIMARY KEY,
    actor_id TEXT NOT NULL,
    scope_type TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    verbs TEXT NOT NULL,
    max_label TEXT NOT NULL,
    granted_by TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    expires_at TEXT
);
CREATE TABLE IF NOT EXISTS grant_revocations (
    id TEXT PRIMARY KEY,
    grant_id TEXT NOT NULL,
    actor TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actor_status (
    id TEXT PRIMARY KEY,
    actor_id TEXT NOT NULL,
    status TEXT NOT NULL,
    actor TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS access_log (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    actor_id TEXT,
    credential_id TEXT,
    method TEXT NOT NULL,
    route TEXT NOT NULL,
    status INTEGER NOT NULL,
    decision TEXT NOT NULL
);

-- Append-only enforcement for every access table: revocation is a new row in
-- the *_revocations / actor_status log, never an UPDATE or DELETE. The
-- RAISE(ABORT) fires on any UPDATE and on any DELETE that visits rows.
CREATE TRIGGER IF NOT EXISTS credentials_no_update BEFORE UPDATE ON credentials
BEGIN SELECT RAISE(ABORT, 'credentials are append-only: update forbidden'); END;
CREATE TRIGGER IF NOT EXISTS credentials_no_delete BEFORE DELETE ON credentials
BEGIN SELECT RAISE(ABORT, 'credentials are append-only: delete forbidden'); END;

CREATE TRIGGER IF NOT EXISTS credential_revocations_no_update BEFORE UPDATE ON credential_revocations
BEGIN SELECT RAISE(ABORT, 'credential_revocations are append-only: update forbidden'); END;
CREATE TRIGGER IF NOT EXISTS credential_revocations_no_delete BEFORE DELETE ON credential_revocations
BEGIN SELECT RAISE(ABORT, 'credential_revocations are append-only: delete forbidden'); END;

CREATE TRIGGER IF NOT EXISTS grants_no_update BEFORE UPDATE ON grants
BEGIN SELECT RAISE(ABORT, 'grants are append-only: update forbidden'); END;
CREATE TRIGGER IF NOT EXISTS grants_no_delete BEFORE DELETE ON grants
BEGIN SELECT RAISE(ABORT, 'grants are append-only: delete forbidden'); END;

CREATE TRIGGER IF NOT EXISTS grant_revocations_no_update BEFORE UPDATE ON grant_revocations
BEGIN SELECT RAISE(ABORT, 'grant_revocations are append-only: update forbidden'); END;
CREATE TRIGGER IF NOT EXISTS grant_revocations_no_delete BEFORE DELETE ON grant_revocations
BEGIN SELECT RAISE(ABORT, 'grant_revocations are append-only: delete forbidden'); END;

CREATE TRIGGER IF NOT EXISTS actor_status_no_update BEFORE UPDATE ON actor_status
BEGIN SELECT RAISE(ABORT, 'actor_status is append-only: update forbidden'); END;
CREATE TRIGGER IF NOT EXISTS actor_status_no_delete BEFORE DELETE ON actor_status
BEGIN SELECT RAISE(ABORT, 'actor_status is append-only: delete forbidden'); END;

CREATE TRIGGER IF NOT EXISTS access_log_no_update BEFORE UPDATE ON access_log
BEGIN SELECT RAISE(ABORT, 'access_log is append-only: update forbidden'); END;
CREATE TRIGGER IF NOT EXISTS access_log_no_delete BEFORE DELETE ON access_log
BEGIN SELECT RAISE(ABORT, 'access_log is append-only: delete forbidden'); END;
"""


def _new_uuid() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Viewer
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Viewer:
    actor: Actor                 # id, name, family, kind
    realm: str                   # internal | external
    clearance: str
    is_owner: bool
    credential_id: "str | None"
    scopes: tuple[str, ...]
    act_as_allow: tuple[str, ...]
    via: "str | None" = None     # set when acting through a relay (see effective())


# ---------------------------------------------------------------------------
# AccessStore
# ---------------------------------------------------------------------------


class AccessStore:
    """Actors, credentials, grants, status log and audit log on the Store's
    sqlite3 connection. Creates its tables idempotently in ``__init__``."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        # Only the six access tables are owned here; they carry their own
        # contract-mandated append-only triggers in _SCHEMA. Store tables
        # (tickets / events / wiki_pages) keep whatever triggers the store
        # defines — AccessStore adds none of its own.
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # -- actors ---------------------------------------------------------------

    def create_actor(
        self,
        id: str,
        name: str,
        *,
        family: str = "unknown",
        kind: str = "agent",
        realm: str = REALM_INTERNAL,
        sponsor_id: "str | None" = None,
        expires_at: "str | None" = None,
        is_owner: bool = False,
        by: str,
    ) -> Actor:
        """Create an actor row with access attributes and log it active.
        External realm: sponsor_id and expires_at REQUIRED, clearance forced
        to 'external', is_owner forbidden (ValueError). kind human + internal
        realm is allowed. A *kind* outside {{agent, human, system}} (exact, no
        whitespace/case tolerance) raises ValueError BEFORE any write. If the
        actor id already exists, ValueError("actor exists") — identity and
        access attributes of an existing actor are never rewritten (re-creating
        could silently turn an internal human into an act_as target)."""
        if not id or not id.strip():
            raise ValueError("actor id must be non-empty")
        if kind not in ACTOR_KINDS:
            raise ValueError(f"unknown actor kind: {kind!r} "
                             f"(expected one of {sorted(ACTOR_KINDS)})")
        if realm not in (REALM_INTERNAL, REALM_EXTERNAL):
            raise ValueError(f"unknown realm: {realm!r}")
        if realm == REALM_EXTERNAL:
            if not sponsor_id:
                raise ValueError("external actors require a sponsor_id")
            if not expires_at:
                raise ValueError("external actors require an expires_at")
            if is_owner:
                raise ValueError("external actors cannot be is_owner")
            clearance = "external"
        else:
            clearance = "internal"
        expires_at = _canon_ts(expires_at) if expires_at is not None else None
        existing = self._conn.execute(
            "SELECT 1 FROM actors WHERE id = ?", (id,)
        ).fetchone()
        if existing is not None:
            raise ValueError("actor exists")
        self._conn.execute(
            """INSERT INTO actors (id, name, family, kind, realm, clearance,
                                   sponsor_id, expires_at, is_owner)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (id, name, family, kind, realm, clearance, sponsor_id, expires_at,
             1 if is_owner else 0),
        )
        self.set_actor_status(id, "active", by=by)
        self._conn.commit()
        return Actor(id=id, name=name, family=family, kind=kind)

    def adopt_actor(
        self,
        id: str,
        *,
        by: str,
        kind: "str | None" = None,
        family: "str | None" = None,
        is_owner: bool = False,
    ) -> Actor:
        """Adopt an actor row that predates access control. The row MUST exist
        (KeyError otherwise). Optionally set *kind* (validated exactly like
        create_actor: exact membership of ACTOR_KINDS, ValueError before any
        write) and *family*; ``is_owner=True`` is refused for realm=external
        (ValueError). Appends an actor_status 'active' row UNLESS the latest
        status is already active, and refuses (ValueError) to re-activate an
        actor whose latest status is suspended or revoked — those go through
        set_actor_status explicitly. Realm, clearance, sponsor_id and
        expires_at are NEVER changed. Returns the updated Actor."""
        row = self._conn.execute(
            "SELECT * FROM actors WHERE id = ?", (id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"actor not found: {id}")
        if kind is not None and kind not in ACTOR_KINDS:
            raise ValueError(f"unknown actor kind: {kind!r} "
                             f"(expected one of {sorted(ACTOR_KINDS)})")
        if is_owner and row["realm"] == REALM_EXTERNAL:
            raise ValueError("external actors cannot be is_owner")
        st = self._conn.execute(
            "SELECT status FROM actor_status WHERE actor_id = ? "
            "ORDER BY rowid DESC LIMIT 1",
            (id,),
        ).fetchone()
        latest = st["status"] if st is not None else None
        if latest in ("suspended", "revoked"):
            raise ValueError(
                f"actor {id!r} is {latest}; adopt_actor does not re-activate — "
                "use set_actor_status explicitly")
        # validate everything before any write
        if kind is not None and kind != row["kind"]:
            self._conn.execute(
                "UPDATE actors SET kind = ? WHERE id = ?", (kind, id))
        if family is not None and family != row["family"]:
            self._conn.execute(
                "UPDATE actors SET family = ? WHERE id = ?", (family, id))
        if is_owner and not row["is_owner"]:
            self._conn.execute(
                "UPDATE actors SET is_owner = 1 WHERE id = ?", (id,))
        if latest != "active":
            self.set_actor_status(id, "active", by=by, reason="adopted")
        else:
            self._conn.commit()
        updated = self._conn.execute(
            "SELECT * FROM actors WHERE id = ?", (id,)
        ).fetchone()
        return Actor(id=updated["id"], name=updated["name"],
                     family=updated["family"], kind=updated["kind"])

    def adopt_legacy(self, *, by: str) -> "list[str]":
        """For every actor with NO actor_status row at all, append status
        'active'. Changes nothing else (no kind/family/is_owner write). Actors
        whose latest status is suspended/revoked are untouched — those went
        through set_actor_status deliberately. Idempotent: the second call
        returns []. Returns the ids adopted (sorted)."""
        rows = self._conn.execute(
            """SELECT a.id FROM actors a
               WHERE NOT EXISTS (SELECT 1 FROM actor_status s
                                 WHERE s.actor_id = a.id)
               ORDER BY a.id ASC"""
        ).fetchall()
        ids = [r["id"] for r in rows]
        for actor_id in ids:
            self.set_actor_status(actor_id, "active", by=by,
                                  reason="adopt-legacy")
        return ids

    def set_actor_status(self, actor_id: str, status: str, *, by: str, reason: str = "") -> None:
        """Append an actor_status row (the latest row is the actor's status)."""
        if status not in ACTOR_STATUSES:
            raise ValueError(f"unknown actor status: {status!r} "
                             f"(expected one of {sorted(ACTOR_STATUSES)})")
        if self._conn.execute("SELECT 1 FROM actors WHERE id = ?", (actor_id,)).fetchone() is None:
            raise KeyError(f"actor not found: {actor_id}")
        self._conn.execute(
            "INSERT INTO actor_status (id, actor_id, status, actor, reason, ts) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (_new_uuid(), actor_id, status, by, reason, _utcnow()),
        )
        self._conn.commit()

    def actor_attrs(self, actor_id: str) -> "dict | None":
        """realm, clearance, sponsor_id, expires_at, is_owner, status (latest
        actor_status row), or None if the actor does not exist."""
        row = self._conn.execute(
            "SELECT * FROM actors WHERE id = ?", (actor_id,)
        ).fetchone()
        if row is None:
            return None
        st = self._conn.execute(
            "SELECT status FROM actor_status WHERE actor_id = ? "
            "ORDER BY rowid DESC LIMIT 1",
            (actor_id,),
        ).fetchone()
        return {
            "id": row["id"],
            "name": row["name"],
            "family": row["family"],
            "kind": row["kind"],
            "realm": row["realm"],
            "clearance": row["clearance"],
            "sponsor_id": row["sponsor_id"],
            "expires_at": row["expires_at"],
            "is_owner": bool(row["is_owner"]),
            "status": st["status"] if st is not None else None,
        }

    def actor_live(self, actor_id: str, now: "str | None" = None) -> bool:
        """exists, latest status is 'active', and not expired (expires_at > now
        or NULL)."""
        attrs = self.actor_attrs(actor_id)
        if attrs is None:
            return False
        if attrs["status"] != "active":
            return False
        now = _canon_now(now)
        exp = attrs["expires_at"]
        if exp is not None and _canon_ts(exp) <= now:
            return False
        return True

    # -- credentials ------------------------------------------------------------

    def issue_credential(
        self,
        actor_id: str,
        *,
        by: str,
        expires_at: "str | None" = None,
        scopes: "tuple[str, ...] | list[str]" = (),
        act_as_allow: "tuple[str, ...] | list[str]" = (),
    ) -> "tuple[str, str]":
        """Issue a credential; returns (credential_id, token) — the token is
        returned exactly once here and never stored, logged or quoted in an
        exception. Comparison later is by sha256 hash lookup only."""
        attrs = self.actor_attrs(actor_id)
        if attrs is None:
            raise KeyError(f"actor not found: {actor_id}")
        scopes = [s for s in scopes]
        patterns = [p for p in act_as_allow]
        if patterns and "act_as" not in scopes:
            raise ValueError("act_as_allow requires the 'act_as' scope")
        # reject patterns that could match an internal human (checked again at
        # use time in effective(); the token never appears in these messages)
        for pattern in patterns:
            self._reject_internal_human_pattern(pattern)
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        cred_id = new_id("CR")
        expires_at = _canon_ts(expires_at) if expires_at is not None else None
        self._conn.execute(
            """INSERT INTO credentials (id, actor_id, token_hash, prefix, scopes,
                                        act_as_allow, created_by, created_at, expires_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (cred_id, actor_id, _token_hash(token), token[:TOKEN_DISPLAY_LEN],
             json.dumps(scopes), json.dumps(patterns), by, _utcnow(), expires_at),
        )
        self._conn.commit()
        return cred_id, token

    def _reject_internal_human_pattern(self, pattern: str) -> None:
        rows = self._conn.execute(
            "SELECT id FROM actors WHERE realm = 'internal' AND kind = 'human'"
        ).fetchall()
        for r in rows:
            if fnmatch.fnmatchcase(r["id"], pattern):
                raise ValueError(
                    f"act_as pattern {pattern!r} could match internal human "
                    f"actor {r['id']!r}"
                )

    def issue_scoped_credential(
        self,
        actor_id: str,
        *,
        by: str,
        family_scopes: "list[tuple[str, str]] | tuple[tuple[str, str], ...]" = (),
        expires_at: "str | None" = None,
    ) -> "tuple[str, str]":
        """Mint an actor × family × action scoped credential: each entry is
        (family, action) with action ∈ {read, write} and family a family name
        or '*' (wildcard). Stored in credentials.scopes as ``fam:<f>:<a>``
        entries. A credential with at least one fam: scope is family-scoped
        (deny-by-default outside its scopes); none -> legacy/unscoped."""
        from .family_acl import make_family_scope
        scopes = [make_family_scope(f, a) for f, a in family_scopes]
        return self.issue_credential(actor_id, by=by, scopes=scopes,
                                     expires_at=expires_at)

    def revoke_credential(self, credential_id: str, *, by: str, reason: str = "") -> None:
        """Append a credential_revocations row (never an UPDATE)."""
        if self._conn.execute(
            "SELECT 1 FROM credentials WHERE id = ?", (credential_id,)
        ).fetchone() is None:
            raise KeyError(f"credential not found: {credential_id}")
        self._conn.execute(
            "INSERT INTO credential_revocations (id, credential_id, actor, reason, ts) "
            "VALUES (?, ?, ?, ?, ?)",
            (_new_uuid(), credential_id, by, reason, _utcnow()),
        )
        self._conn.commit()

    def list_credentials(self) -> "list[dict]":
        """id, actor_id, prefix, scopes, expires_at, revoked (bool),
        last_used_at (latest access_log entry for the credential). token_hash is
        never exposed."""
        rows = self._conn.execute(
            """SELECT c.id, c.actor_id, c.prefix, c.scopes, c.expires_at,
                      EXISTS (SELECT 1 FROM credential_revocations r
                              WHERE r.credential_id = c.id) AS revoked,
                      (SELECT MAX(a.ts) FROM access_log a
                       WHERE a.credential_id = c.id) AS last_used_at
               FROM credentials c
               ORDER BY c.created_at ASC, c.rowid ASC"""
        ).fetchall()
        return [
            {
                "id": r["id"],
                "actor_id": r["actor_id"],
                "prefix": r["prefix"],
                "scopes": json.loads(r["scopes"]),
                "expires_at": r["expires_at"],
                "revoked": bool(r["revoked"]),
                "last_used_at": r["last_used_at"],
            }
            for r in rows
        ]

    def viewer_for_token(self, token: str, now: "str | None" = None) -> "Viewer | None":
        """None unless the credential exists (hash lookup), is unrevoked and
        unexpired, and its actor is live."""
        if not isinstance(token, str):
            return None
        row = self._conn.execute(
            "SELECT * FROM credentials WHERE token_hash = ?", (_token_hash(token),)
        ).fetchone()
        if row is None:
            return None
        now = _canon_now(now)
        if row["expires_at"] is not None and _canon_ts(row["expires_at"]) <= now:
            return None
        if self._conn.execute(
            "SELECT 1 FROM credential_revocations WHERE credential_id = ?", (row["id"],)
        ).fetchone() is not None:
            return None
        attrs = self.actor_attrs(row["actor_id"])
        if attrs is None or not self.actor_live(row["actor_id"], now):
            return None
        return Viewer(
            actor=Actor(id=attrs["id"], name=attrs["name"],
                        family=attrs["family"], kind=attrs["kind"]),
            realm=attrs["realm"],
            clearance=attrs["clearance"],
            is_owner=attrs["is_owner"],
            credential_id=row["id"],
            scopes=tuple(json.loads(row["scopes"])),
            act_as_allow=tuple(json.loads(row["act_as_allow"])),
        )

    # -- grants ---------------------------------------------------------------

    def add_grant(
        self,
        actor_id: str,
        scope_type: str,
        scope_id: str,
        *,
        verbs: "tuple[str, ...] | list[str]" = ("read",),
        by: str,
        reason: str = "",
        expires_at: "str | None" = None,
    ) -> str:
        """Add a read grant (scope_type ticket|subtree|wiki_page). External
        actors: max_label forced to 'external'; any other verb raises
        ValueError."""
        attrs = self.actor_attrs(actor_id)
        if attrs is None:
            raise KeyError(f"actor not found: {actor_id}")
        if scope_type not in GRANT_SCOPE_TYPES:
            raise ValueError(f"unknown grant scope_type: {scope_type!r} "
                             f"(expected one of {sorted(GRANT_SCOPE_TYPES)})")
        verbs = list(verbs)
        if not verbs or not set(verbs) <= P1_VERBS:
            raise ValueError(f"verbs must be a non-empty subset of {sorted(P1_VERBS)} in P1")
        # stricter reading (noted above): an external grant is capped at 'external'
        max_label = "external"
        expires_at = _canon_ts(expires_at) if expires_at is not None else None
        grant_id = new_id("GR")
        self._conn.execute(
            """INSERT INTO grants (id, actor_id, scope_type, scope_id, verbs,
                                   max_label, granted_by, reason, created_at, expires_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (grant_id, actor_id, scope_type, scope_id, json.dumps(verbs),
             max_label, by, reason, _utcnow(), expires_at),
        )
        self._conn.commit()
        return grant_id

    def revoke_grant(self, grant_id: str, *, by: str, reason: str = "") -> None:
        """Append a grant_revocations row (never an UPDATE)."""
        if self._conn.execute(
            "SELECT 1 FROM grants WHERE id = ?", (grant_id,)
        ).fetchone() is None:
            raise KeyError(f"grant not found: {grant_id}")
        self._conn.execute(
            "INSERT INTO grant_revocations (id, grant_id, actor, reason, ts) "
            "VALUES (?, ?, ?, ?, ?)",
            (_new_uuid(), grant_id, by, reason, _utcnow()),
        )
        self._conn.commit()

    def live_grants(self, actor_id: str, now: "str | None" = None) -> "list[dict]":
        """Grants for *actor_id* with no revocation row and
        (expires_at IS NULL OR expires_at > now). Stored and argument
        timestamps are canonicalised via _canon_ts before comparing."""
        now = _canon_now(now)
        rows = self._conn.execute(
            """SELECT g.* FROM grants g
               WHERE g.actor_id = ?
                 AND (g.expires_at IS NULL OR g.expires_at > ?)
                 AND NOT EXISTS (SELECT 1 FROM grant_revocations r
                                 WHERE r.grant_id = g.id)
               ORDER BY g.created_at ASC, g.rowid ASC""",
            (actor_id, now),
        ).fetchall()
        return [
            {
                "id": r["id"],
                "actor_id": r["actor_id"],
                "scope_type": r["scope_type"],
                "scope_id": r["scope_id"],
                "verbs": json.loads(r["verbs"]),
                "max_label": r["max_label"],
                "granted_by": r["granted_by"],
                "reason": r["reason"],
                "created_at": r["created_at"],
                "expires_at": r["expires_at"],
            }
            for r in rows
        ]

    # -- delegation -------------------------------------------------------------

    def effective(self, viewer: Viewer, claimed_actor_id: "str | None") -> Viewer:
        """Resolve 'acting as'. None / self -> the viewer itself. Otherwise the
        credential needs the 'act_as' scope and a matching act_as_allow pattern;
        actor_live, and must NOT be an internal human (re-checked here at use
        time). A target whose stored kind is not one of ACTOR_KINDS is refused
        (ValueError in the store is not trusted; fail closed). Returns the
        TARGET's Viewer with via = the relay's (the original viewer's) actor
        id. PermissionError otherwise."""
        if claimed_actor_id is None:
            return viewer
        # re-check at use time (an internal human may have been created after
        # the credential was issued); self-claims always pass.
        if claimed_actor_id != viewer.actor.id:
            target = self.actor_attrs(claimed_actor_id)
            if target is not None:
                if target["kind"] not in ACTOR_KINDS:
                    raise PermissionError(
                        f"act_as refused: {claimed_actor_id!r} has unknown "
                        f"kind {target['kind']!r}")
                if (target["realm"] == REALM_INTERNAL
                        and target["kind"] == "human"):
                    raise PermissionError(
                        f"act_as refused: {claimed_actor_id!r} is an internal human")
        if claimed_actor_id == viewer.actor.id:
            return viewer
        if "act_as" not in viewer.scopes:
            raise PermissionError(f"act_as not allowed: credential lacks the 'act_as' scope")
        if not any(fnmatch.fnmatchcase(claimed_actor_id, p) for p in viewer.act_as_allow):
            raise PermissionError(f"act_as not allowed: no act_as_allow pattern matches "
                                  f"{claimed_actor_id!r}")
        attrs = self.actor_attrs(claimed_actor_id)
        if attrs is None:
            raise PermissionError(f"act_as target not found: {claimed_actor_id!r}")
        if not self.actor_live(claimed_actor_id):
            raise PermissionError(f"act_as target is not live: {claimed_actor_id!r}")
        if attrs["realm"] == REALM_INTERNAL and attrs["kind"] == "human":
            raise PermissionError(f"act_as refused: {claimed_actor_id!r} is an internal human")
        return Viewer(
            actor=Actor(id=attrs["id"], name=attrs["name"],
                        family=attrs["family"], kind=attrs["kind"]),
            realm=attrs["realm"],
            clearance=attrs["clearance"],
            is_owner=attrs["is_owner"],
            credential_id=viewer.credential_id,
            scopes=viewer.scopes,
            act_as_allow=viewer.act_as_allow,
            via=viewer.actor.id,
        )

    # -- audit ----------------------------------------------------------------

    def log_access(
        self, *, actor_id: "str | None", credential_id: "str | None",
        method: str, route: str, status: int, decision: str,
    ) -> None:
        self._conn.execute(
            "INSERT INTO access_log (ts, actor_id, credential_id, method, route, "
            "status, decision) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (_utcnow(), actor_id, credential_id, method, route, int(status), decision),
        )
        self._conn.commit()


# ---------------------------------------------------------------------------
# ExternalReader — the ONLY read path for realm=external viewers.
# Every filter below is a SQL WHERE clause; nothing is fetched wholesale and
# filtered in Python.
# ---------------------------------------------------------------------------

#: one row per ticket the viewer may see (live read grant by ticket id, plus
#: the live subtrees rooted at subtree grants expanded over parent_of edges).
#: A 'ticket' grant covers ONLY its own ticket — descendants are expanded from
#: 'subtree' scope ids alone. Structure: `seed` = live subtree grant roots;
#: `sub(id)` = those roots and their descendants (UNION dedups cycles); the
#: final `granted` set = live direct ticket-grant ids UNION sub.
_GRANT_CTE = """
WITH RECURSIVE seed(id) AS (
    SELECT g.scope_id FROM grants g
     WHERE g.actor_id = ?
       AND g.scope_type = 'subtree'
       AND EXISTS (SELECT 1 FROM json_each(g.verbs) WHERE value = 'read')
       AND (g.expires_at IS NULL OR g.expires_at > ?)
       AND NOT EXISTS (SELECT 1 FROM grant_revocations r WHERE r.grant_id = g.id)
),
sub(id) AS (
    SELECT id FROM seed
    UNION
    -- descendant expansion over parent_of edges (sqlite exposes a bare CTE
    -- alias as an implicit table-valued function column, so qualify with
    -- sub.id rather than an alias); UNION dedups cycles.
    SELECT e.dst FROM edges e JOIN sub ON e.src = sub.id
     WHERE e.type = 'parent_of'
),
granted(id) AS (
    SELECT g.scope_id FROM grants g
     WHERE g.actor_id = ?
       AND g.scope_type = 'ticket'
       AND EXISTS (SELECT 1 FROM json_each(g.verbs) WHERE value = 'read')
       AND (g.expires_at IS NULL OR g.expires_at > ?)
       AND NOT EXISTS (SELECT 1 FROM grant_revocations r WHERE r.grant_id = g.id)
    UNION
    SELECT id FROM sub
)
"""

_EXT_TICKET_SELECT = """
SELECT t.id, t.title, t.body, t.status, t.priority, t.label,
       t.created_at, t.updated_at
  FROM tickets t
 WHERE t.label IN ('public', 'external')
   AND t.id IN (SELECT id FROM granted)
"""

_EXT_EVENT_SELECT = """
SELECT ev.id, ev.ticket_id, ev.ts, ev.kind, ev.payload, ev.label,
       ev.actor_json,
       COALESCE(a.name, 'Team') AS actor_name,
       COALESCE(a.realm, 'internal') AS actor_realm
  FROM events ev
  LEFT JOIN actors a ON a.id = ev.actor_id
 WHERE ev.ticket_id = ?
   AND ev.label IN ('public', 'external')
   AND EXISTS (
        SELECT 1 FROM tickets t
         WHERE t.id = ? AND t.label IN ('public', 'external')
           AND t.id IN (SELECT id FROM granted)
   )
"""

_EXT_PAGE_SELECT = """
SELECT w.slug, w.rev, w.title, w.body_md, w.label, w.ts
  FROM wiki_pages w
 WHERE w.rev = (SELECT MAX(w2.rev) FROM wiki_pages w2 WHERE w2.slug = w.slug)
   AND w.label IN ('public', 'external')
   AND EXISTS (
        SELECT 1 FROM grants g
         WHERE g.actor_id = ? AND g.scope_type = 'wiki_page' AND g.scope_id = w.slug
           AND EXISTS (SELECT 1 FROM json_each(g.verbs) WHERE value = 'read')
           AND (g.expires_at IS NULL OR g.expires_at > ?)
           AND NOT EXISTS (SELECT 1 FROM grant_revocations r WHERE r.grant_id = g.id)
   )
"""


class ExternalReader:
    """The ONLY read path for realm=external viewers. All filtering in SQL
    WHERE. Projections are exact: ExternalTicket {id, title, body, status,
    priority, label, created_at, updated_at}; ExternalEvent {id, ticket_id, ts,
    kind, payload, label, actor:{name}} (name is the actor's name only when
    that actor is realm=external, else 'Team'); ExternalPage {slug, rev, title,
    body_md, label, ts}."""

    def __init__(self, conn: sqlite3.Connection, viewer: Viewer, now: "str | None" = None) -> None:
        self._conn = conn
        self._viewer = viewer
        # grant expires_at values are canonicalised at insert (add_grant), so a
        # canonicalised `now` compares correctly inside the SQL WHERE clauses.
        self._now = _canon_now(now)

    # -- internals -----------------------------------------------------------

    @staticmethod
    def _ticket_row(r: sqlite3.Row) -> dict:
        return {
            "id": r["id"], "title": r["title"], "body": r["body"],
            "status": r["status"], "priority": r["priority"], "label": r["label"],
            "created_at": r["created_at"], "updated_at": r["updated_at"],
        }

    @staticmethod
    def _event_row(r: sqlite3.Row) -> dict:
        name = r["actor_name"] if r["actor_realm"] == REALM_EXTERNAL else "Team"
        return {
            "id": r["id"], "ticket_id": r["ticket_id"], "ts": r["ts"],
            "kind": r["kind"], "payload": json.loads(r["payload"]),
            "label": r["label"], "actor": {"name": name},
        }

    @staticmethod
    def _page_row(r: sqlite3.Row) -> dict:
        return {
            "slug": r["slug"], "rev": r["rev"], "title": r["title"],
            "body_md": r["body_md"], "label": r["label"], "ts": r["ts"],
        }

    def _granted(self) -> "list[str]":
        rows = self._conn.execute(
            _GRANT_CTE + "SELECT id FROM granted",
            (self._viewer.actor.id, self._now, self._viewer.actor.id, self._now),
        ).fetchall()
        return [r["id"] for r in rows]

    # -- tickets ---------------------------------------------------------------

    def list_tickets(
        self, *, status: "str | None" = None, tag: "str | None" = None,
        q: "str | None" = None,
    ) -> "list[dict]":
        sql = _GRANT_CTE + _EXT_TICKET_SELECT
        args: list = [self._viewer.actor.id, self._now, self._viewer.actor.id, self._now]
        if status is not None:
            sql += " AND t.status = ?"
            args.append(status)
        if tag is not None:
            sql += " AND EXISTS (SELECT 1 FROM json_each(t.tags) WHERE value = ?)"
            args.append(tag)
        if q is not None:
            like = f"%{q}%"
            sql += " AND (t.title LIKE ? OR t.body LIKE ? OR t.id LIKE ?)"
            args.extend([like, like, like])
        sql += " ORDER BY t.created_at ASC, t.rowid ASC"
        return [self._ticket_row(r) for r in self._conn.execute(sql, args).fetchall()]

    def get_ticket(self, ticket_id: str) -> "dict | None":
        sql = _GRANT_CTE + _EXT_TICKET_SELECT + " AND t.id = ?"
        row = self._conn.execute(
            sql,
            (self._viewer.actor.id, self._now, self._viewer.actor.id, self._now, ticket_id),
        ).fetchone()
        return self._ticket_row(row) if row is not None else None

    def ticket_visible(self, ticket_id: str) -> bool:
        return self.get_ticket(ticket_id) is not None

    # -- events ---------------------------------------------------------------

    def events(self, ticket_id: str) -> "list[dict] | None":
        """Visible events of a visible ticket; None if the ticket itself is not
        visible."""
        if not self.ticket_visible(ticket_id):
            return None
        sql = _GRANT_CTE + _EXT_EVENT_SELECT + " ORDER BY ev.ts ASC, ev.seq ASC"
        args = [self._viewer.actor.id, self._now, self._viewer.actor.id, self._now,
                ticket_id, ticket_id]
        return [self._event_row(r) for r in self._conn.execute(sql, args).fetchall()]

    # -- wiki ------------------------------------------------------------------

    def _page(self, slug: str) -> "sqlite3.Row | None":
        sql = _EXT_PAGE_SELECT + " AND w.slug = ?"
        return self._conn.execute(
            sql, (self._viewer.actor.id, self._now, slug)
        ).fetchone()

    def list_pages(self) -> "list[dict]":
        sql = _EXT_PAGE_SELECT + " ORDER BY w.slug ASC"
        rows = self._conn.execute(sql, (self._viewer.actor.id, self._now)).fetchall()
        return [self._page_row(r) for r in rows]

    def get_page(self, slug: str) -> "dict | None":
        """Latest *visible* revision: visible ⇔ a live wiki_page read grant on
        the slug AND the current (max rev) label and the revision's own label
        are both public/external. When the current revision is internal the page
        is entirely hidden (history included), so get_page returns None."""
        row = self._page(slug)
        if row is None:
            # not visible as current; only an older revision could qualify when
            # the current one is public/external too, so nothing further qualifies.
            return None
        return self._page_row(row)

    def page_history(self, slug: str) -> "list[dict] | None":
        """All visible revisions of *slug* (oldest first), or None if the page
        is not visible at all (no live grant, or current revision label is
        internal)."""
        if self._page(slug) is None:
            return None
        sql = """
SELECT w.slug, w.rev, w.title, w.body_md, w.label, w.ts
  FROM wiki_pages w
 WHERE w.slug = ?
   AND w.label IN ('public', 'external')
   AND EXISTS (
        SELECT 1 FROM grants g
         WHERE g.actor_id = ? AND g.scope_type = 'wiki_page' AND g.scope_id = w.slug
           AND EXISTS (SELECT 1 FROM json_each(g.verbs) WHERE value = 'read')
           AND (g.expires_at IS NULL OR g.expires_at > ?)
           AND NOT EXISTS (SELECT 1 FROM grant_revocations r WHERE r.grant_id = g.id)
   )
 ORDER BY w.rev ASC
"""
        rows = self._conn.execute(sql, (slug, self._viewer.actor.id, self._now)).fetchall()
        return [self._page_row(r) for r in rows]

    def page_visible(self, slug: str) -> bool:
        return self._page(slug) is not None

    def search_pages(self, q: str) -> "list[dict]":
        """Slugs whose CURRENT revision is visible and matches q (title/body
        substring), matching on that revision."""
        like = f"%{q}%"
        sql = (_EXT_PAGE_SELECT
               + " AND (w.title LIKE ? OR w.body_md LIKE ?)"
               + " ORDER BY w.slug ASC")
        rows = self._conn.execute(
            sql, (self._viewer.actor.id, self._now, like, like)
        ).fetchall()
        return [self._page_row(r) for r in rows]
