"""Grants and the ledger (design doc §5-§6).

A grant is the primitive you author: scope (anchor entities on a pinned L2
version), TTL, query budget, disclosure rules. Invitation-only — ingress
exists only when a grant exists. Revocation is one status flip; the ledger
keeps every ask, answer, and everything stripped, per counterparty.

Requests are logged as task records (state machine received → answered /
refused / expired), not chat transcripts (landscape C11) — budgets, TTLs and
audit queries are mechanical.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import time
from pathlib import Path

STATES = ("received", "answered", "refused", "expired")


class GrantError(Exception):
    """A request the gatekeeper refuses (the refusal is the answer)."""


class GrantBook:
    """Grants + the ledger over one SQLite file."""

    def __init__(self, path) -> None:
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """CREATE TABLE IF NOT EXISTS grants (
                   id TEXT PRIMARY KEY, counterparty TEXT, purpose TEXT,
                   anchor_entities TEXT, alias TEXT, ttl_days INTEGER,
                   query_budget INTEGER, disclosure TEXT, key_hash TEXT,
                   status TEXT, created_at TEXT);
               CREATE TABLE IF NOT EXISTS ledger (
                   id INTEGER PRIMARY KEY AUTOINCREMENT, grant_id TEXT,
                   ts TEXT, state TEXT, question TEXT, draft TEXT,
                   released TEXT, stripped TEXT, reason TEXT);""")

    # -- grant lifecycle ------------------------------------------------------

    def create(self, *, counterparty: str, purpose: str, anchor_entities,
               alias: str, ttl_days: int, query_budget: int,
               disclosure: dict) -> tuple[str, str]:
        """Create one grant; returns (grant_id, key). The key is shown once —
        only its hash is stored (the gatekeeper authenticates with it)."""
        grant_id = f"g-{time.strftime('%Y-%m', time.gmtime())}-{secrets.token_hex(3)}"
        key = "sk-g" + secrets.token_hex(16)
        auth = _verifier(key, grant_id)  # the verifier; the key never returns
        self.conn.execute(
            "INSERT INTO grants VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (grant_id, counterparty, purpose, json.dumps(list(anchor_entities)),
             alias, ttl_days, query_budget, json.dumps(disclosure),
             auth, "active",
             time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())))
        self.conn.commit()
        return grant_id, key

    def get(self, grant_id: str) -> dict:
        row = self.conn.execute(
            "SELECT * FROM grants WHERE id = ?", (grant_id,)).fetchone()
        if row is None:
            raise GrantError(f"unknown grant {grant_id}")
        doc = dict(row)
        doc["anchor_entities"] = json.loads(doc["anchor_entities"])
        doc["disclosure"] = json.loads(doc["disclosure"])
        return doc

    def revoke(self, grant_id: str) -> None:
        self.conn.execute(
            "UPDATE grants SET status = 'revoked' WHERE id = ?", (grant_id,))
        self.conn.commit()

    def active(self, grant_id: str, key: str, now: float | None = None) -> dict:
        """Authenticate + status/TTL check. Refusals name the reason."""
        grant = self.get(grant_id)
        if grant["status"] != "active":
            raise GrantError(f"grant {grant_id} is {grant['status']}")
        if not secrets.compare_digest(_verifier(key, grant["id"]),
                                      grant["key_hash"]):
            raise GrantError("bad grant key")
        created = time.mktime(time.strptime(grant["created_at"],
                                            "%Y-%m-%dT%H:%M:%SZ"))
        if now is None:
            now = time.time()
        if now > created + grant["ttl_days"] * 86400:
            self.conn.execute(
                "UPDATE grants SET status = 'expired' WHERE id = ?",
                (grant_id,))
            self.conn.commit()
            raise GrantError(f"grant {grant_id} expired (TTL {grant['ttl_days']}d)")
        return grant

    def budget_used(self, grant_id: str) -> int:
        """Answered + refused queries both count against the budget (§5)."""
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM ledger WHERE grant_id = ?"
            " AND state IN ('answered', 'refused')", (grant_id,)).fetchone()
        return row["n"]

    def check_budget(self, grant: dict) -> None:
        used = self.budget_used(grant["id"])
        if used >= grant["query_budget"]:
            raise GrantError(
                f"budget exhausted: {used}/{grant['query_budget']} queries used")

    # -- ledger ------------------------------------------------------------------

    def log(self, grant_id: str, state: str, *, question: str = "",
            draft: str = "", released: str = "", stripped: str = "",
            reason: str = "") -> int:
        cur = self.conn.execute(
            "INSERT INTO ledger (grant_id, ts, state, question, draft,"
            " released, stripped, reason) VALUES (?,?,?,?,?,?,?,?)",
            (grant_id, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
             state, question, draft, released, stripped, reason))
        self.conn.commit()
        return cur.lastrowid

    def entries(self, grant_id: str) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM ledger WHERE grant_id = ? ORDER BY id",
            (grant_id,)).fetchall()]

    def burn_summary(self, grant_id: str) -> dict:
        """Budget burn visible per counterparty (§8 surface 3)."""
        rows = self.conn.execute(
            "SELECT state, COUNT(*) AS n FROM ledger WHERE grant_id = ?"
            " GROUP BY state", (grant_id,)).fetchall()
        return {r["state"]: r["n"] for r in rows}


def _verifier(key: str, grant_id: str) -> str:
    """The stored verifier: sha256(key + grant_id). The key itself is never
    stored; the grant id is the per-grant salt."""
    import hashlib
    return hashlib.sha256((key + grant_id).encode()).hexdigest()
