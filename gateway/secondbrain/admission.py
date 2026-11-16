"""Admission strength + pairing + hand-back (landscape A2, A3, B7).

Admission strength grades every inbound message before it can trigger work
(dsh docs/002, borrowed):
- ``explicit`` — an allowed counterparty addressing the gateway with a
  question inside their grant: consumes budget, spawns an answer worker;
- ``follow-up`` — a message on a request that is already owned: context
  only, bypasses the gate, grants no new authority, never consumes budget;
- ``bot`` — automated senders are candidates at best: never consume budget
  and never escalate.

Hand-back (A3): an answer worker owns exactly one request under its
mandate; anything beyond the mandate is handed back with a reason instead
of silently expanded. Hand-back reasons accumulate in the ledger — the
free dataset of where scopes are too tight or too loose.

Pairing (OpenClaw B7): a counterparty request creates an *unapproved
pairing* entry; a grant exists only after you approve. Never auto-activate.
"""

from __future__ import annotations

import secrets
import sqlite3
import time

STRENGTHS = ("explicit", "follow-up", "bot", "refused")


class AdmissionError(Exception):
    """The message may not trigger work (the refusal is logged)."""


class HandBack(Exception):
    """Raised by an answer worker when the request exceeds its mandate.

    Carries the reason that feeds the scope-tuning dataset (A3)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class Admission:
    """Grades inbound messages against the grant book."""

    def __init__(self, book) -> None:
        self.book = book

    def grade(self, grant_id: str, key: str, question: str,
              *, sender_kind: str = "person",
              in_flight_request: bool = False) -> str:
        """Return the admission strength; raises AdmissionError when the
        message may not trigger work at all."""
        if sender_kind == "bot":
            # a bot on the bot list is an inferred candidate at best: never
            # budget, never escalate (A2). Logged as refused at the gate.
            raise AdmissionError("bot senders cannot open work: candidate only")
        try:
            grant = self.book.active(grant_id, key)
        except Exception as exc:
            raise AdmissionError(str(exc)) from None
        if in_flight_request:
            return "follow-up"  # context only: no new authority, no budget
        # The budget is checked in exactly one place, Gatekeeper.ask (glm
        # review 2026-11-11): checking it here too would double-charge a
        # query, and shadow mode must not consume budget at all.
        if not question.strip():
            raise AdmissionError("empty question")
        return "explicit"


class Pairing:
    """Unapproved pairing entries; a grant exists only after approval (B7)."""

    def __init__(self, path) -> None:
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """CREATE TABLE IF NOT EXISTS pairings (
                   code TEXT PRIMARY KEY, counterparty TEXT, note TEXT,
                   status TEXT, requested_at TEXT, decided_at TEXT)""")

    def request(self, counterparty: str, note: str = "") -> str:
        """A counterparty asks; this creates an *unapproved* entry and
        returns the pairing code you would approve with. Nothing activates."""
        code = "pair-" + secrets.token_hex(4)
        self.conn.execute(
            "INSERT INTO pairings VALUES (?,?,?,?,?,?)",
            (code, counterparty, note, "unapproved",
             time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), None))
        self.conn.commit()
        return code

    def approve(self, code: str) -> bool:
        """You approve; the pairing becomes approved — the grant itself is
        still a separate, explicit act you perform."""
        cur = self.conn.execute(
            "UPDATE pairings SET status = 'approved', decided_at = ?"
            " WHERE code = ? AND status = 'unapproved'",
            (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), code))
        self.conn.commit()
        return cur.rowcount == 1

    def deny(self, code: str) -> bool:
        cur = self.conn.execute(
            "UPDATE pairings SET status = 'denied', decided_at = ?"
            " WHERE code = ? AND status = 'unapproved'",
            (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), code))
        self.conn.commit()
        return cur.rowcount == 1

    def entries(self, status: str | None = None) -> list[dict]:
        if status:
            rows = self.conn.execute(
                "SELECT * FROM pairings WHERE status = ? ORDER BY rowid",
                (status,)).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM pairings ORDER BY rowid").fetchall()
        return [dict(r) for r in rows]
