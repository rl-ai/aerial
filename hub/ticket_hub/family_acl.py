"""Family-ACL enforcement (Project #6 phase 1).

Actor × family × action scoped tokens, family sensitivity labels, and
deny-by-default cross-family reads, on top of the existing access tables.

Design (documented deviations noted per the standing convention):

Token format
    The wire token is unchanged: ``thk_`` + ``secrets.token_urlsafe(32)``,
    stored only as sha256 (AccessStore contract). The family+action dimension
    lives in the credential's ``scopes`` column as entries of the form::

        fam:<family>:<action>       action ∈ {read, write}; family may be *
                                    (wildcard = all families)

    A credential with at least one ``fam:`` scope is *family-scoped* and is
    deny-by-default outside its scopes. A credential with NO ``fam:`` scopes
    is legacy/unscoped and keeps today's behaviour — this is the migration
    path: the roles-runner, hourly board and hierarchy cadence tokens are
    minted without ``fam:`` scopes and continue to work untouched.

Family sensitivity labels
    New ``families`` table: (name PRIMARY KEY, label, created_at, created_by).
    Lattice: personal < public < external < internal (FAMILY_LABEL_RANK),
    default label ``personal`` — a family that has never been classified
    leaks nothing: a family-scoped read of a ticket through it requires the
    ticket's own sensitivity (its ``label`` rank) to be at or below the
    family's label rank. Unclassified tickets (empty ``families`` list) are
    invisible to every family-scoped token unless the token carries the
    ``fam:*`` wildcard.

Enforcement points (api.py)
    - middleware: any route with a ``ticket_id`` path param — GET/HEAD need a
      matching read scope + sensitivity (404 hidden, identical envelope to a
      missing ticket); mutations need ``fam:<family>:write`` (403
      family_denied). External-realm viewers are exempt (their visibility is
      already grant-filtered in ExternalReader).
    - ``GET /tickets`` list: family-scoped viewers get only the tickets their
      scopes admit (post-filter, same decision function).
    - ``POST /tickets``: the resolved families must intersect the token's
      write scopes.

Auditing: family denies flow through the existing middleware ``_audit`` path,
so every check lands in ``access_log``. Revocation is request-time:
``viewer_for_token`` re-consults ``credential_revocations`` on every request.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from .model import LABEL_RANK

#: family label lattice, low -> high (family sensitivity labels)
FAMILY_LABELS: tuple[str, ...] = ("personal", "public", "external", "internal")
FAMILY_LABEL_RANK: dict[str, int] = {
    "personal": 0, "public": 1, "external": 2, "internal": 3,
}
#: default sensitivity label for families (and for the families table rows)
DEFAULT_FAMILY_LABEL = "personal"

FAMILY_ACTIONS: frozenset[str] = frozenset({"read", "write"})

FAMILY_SCOPE_PREFIX = "fam:"


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS families (
    name TEXT PRIMARY KEY,
    label TEXT NOT NULL DEFAULT 'personal',
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL DEFAULT ''
);
"""


def parse_family_scopes(scopes) -> "dict[str, set[str]] | None":
    """scopes -> {family: {actions}}; None when the credential carries no
    ``fam:`` scopes (legacy/unscoped token). ``*`` is kept as a literal
    family key meaning wildcard."""
    fam: dict[str, set[str]] = {}
    for s in scopes or ():
        if not isinstance(s, str) or not s.startswith(FAMILY_SCOPE_PREFIX):
            continue
        parts = s[len(FAMILY_SCOPE_PREFIX):].split(":")
        if len(parts) != 2:
            continue  # malformed scope entries fail closed (ignored)
        family, action = parts
        if action not in FAMILY_ACTIONS or not family:
            continue
        fam.setdefault(family, set()).add(action)
    return fam or None


def make_family_scope(family: str, action: str) -> str:
    if action not in FAMILY_ACTIONS:
        raise ValueError(f"unknown family action: {action!r} "
                         f"(expected one of {sorted(FAMILY_ACTIONS)})")
    family = str(family).strip()
    if not family:
        raise ValueError("family scope requires a non-empty family (or '*')")
    return f"{FAMILY_SCOPE_PREFIX}{family}:{action}"


class FamilyAcl:
    """Family sensitivity labels over the Store's sqlite3 connection.
    Idempotent schema (families table); the access tables stay owned by
    AccessStore."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # -- family registry ------------------------------------------------------

    def ensure_family(self, name: str, *, by: str = "",
                      label: str = DEFAULT_FAMILY_LABEL) -> str:
        """Insert the family row if absent (idempotent); never rewrites an
        existing label. Returns the family's current label."""
        if label not in FAMILY_LABEL_RANK:
            raise ValueError(f"unknown family label: {label!r} "
                             f"(expected one of {list(FAMILY_LABELS)})")
        row = self._conn.execute(
            "SELECT label FROM families WHERE name = ?", (name,)).fetchone()
        if row is not None:
            return row["label"]
        self._conn.execute(
            "INSERT INTO families (name, label, created_at, created_by) "
            "VALUES (?, ?, ?, ?)", (name, label, _utcnow(), by))
        self._conn.commit()
        return label

    def set_family_label(self, name: str, label: str, *, by: str = "") -> str:
        """Classify (or re-classify) a family. Raising the label WIDENS what
        family-scoped tokens can read through it — caller authority is the
        write path's problem (owner-gated in the API layer)."""
        if label not in FAMILY_LABEL_RANK:
            raise ValueError(f"unknown family label: {label!r} "
                             f"(expected one of {list(FAMILY_LABELS)})")
        self.ensure_family(name, by=by)
        self._conn.execute(
            "UPDATE families SET label = ? WHERE name = ?", (label, name))
        self._conn.commit()
        return label

    def family_label(self, name: str) -> str:
        row = self._conn.execute(
            "SELECT label FROM families WHERE name = ?", (name,)).fetchone()
        return row["label"] if row is not None else DEFAULT_FAMILY_LABEL

    def list_families(self) -> "list[dict]":
        rows = self._conn.execute(
            "SELECT name, label, created_at, created_by FROM families "
            "ORDER BY name ASC").fetchall()
        return [dict(r) for r in rows]

    # -- the decision function --------------------------------------------------

    def read_visible(self, fam_scopes: "dict[str, set[str]] | None",
                     ticket: dict) -> "bool | None":
        """None  -> token is not family-scoped (legacy: unrestricted);
        True/False -> is this ticket readable? Deny-by-default across
        families: every family on the token must match a family of the
        ticket AND the ticket's sensitivity must sit at or below the
        family's label."""
        if fam_scopes is None:
            return None
        wildcard = "*" in fam_scopes and "read" in fam_scopes["*"]
        if wildcard:
            return True
        tfamilies = ticket.get("families") or []
        tlabel = ticket.get("label") or "internal"
        trank = LABEL_RANK.get(tlabel, LABEL_RANK["internal"])
        for family in fam_scopes:
            if "read" not in fam_scopes[family]:
                continue
            if family in tfamilies:
                frank = FAMILY_LABEL_RANK[self.family_label(family)]
                if trank <= frank:
                    return True
        return False

    def write_allowed(self, fam_scopes: "dict[str, set[str]] | None",
                      ticket: dict) -> "bool | None":
        """None -> unscoped (legacy write path governs); True/False."""
        if fam_scopes is None:
            return None
        if "*" in fam_scopes and "write" in fam_scopes["*"]:
            return True
        tfamilies = ticket.get("families") or []
        return any("write" in fam_scopes[f] for f in tfamilies if f in fam_scopes)


def backfill_families_from_tickets(conn: sqlite3.Connection, *, by: str) -> "list[str]":
    """Migration helper: one families row per distinct family referenced by
    any ticket, labelled with the default (personal). Idempotent. Returns the
    family names inserted."""
    acl = FamilyAcl(conn)
    rows = conn.execute(
        "SELECT DISTINCT value AS f FROM tickets, json_each(tickets.families) "
        "WHERE deleted_at IS NULL ORDER BY f"
    ).fetchall()
    inserted = []
    for r in rows:
        before = conn.execute(
            "SELECT 1 FROM families WHERE name = ?", (r["f"],)).fetchone()
        acl.ensure_family(r["f"], by=by)
        if before is None:
            inserted.append(r["f"])
    return inserted
