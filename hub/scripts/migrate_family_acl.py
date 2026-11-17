#!/usr/bin/env python3
"""P6 phase-1 migration: family sensitivity registry + backfill.

Idempotent, additive only:
  1. creates the ``families`` table (name, label default 'personal', ...);
  2. backfills one row per distinct family referenced by any live ticket,
     labelled 'personal' (deny-by-default: family-scoped tokens read nothing
     sensitive through a family until an owner raises its label);
  3. touches NO credential: existing tokens keep no ``fam:`` scopes, so they
     stay legacy/unscoped and the 5-minute roles-runner, hourly board and
     30-minute hierarchy cadence continue unchanged.

Usage: python scripts/migrate_family_acl.py   (env TICKET_HUB_DB overrides path)
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from ticket_hub.family_acl import backfill_families_from_tickets  # noqa: E402
from ticket_hub.paths import hub_path  # noqa: E402


def main() -> int:
    db = os.environ.get("TICKET_HUB_DB", hub_path("hub.db"))
    import sqlite3
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    inserted = backfill_families_from_tickets(conn, by="migrate-family-acl")
    total = conn.execute("SELECT COUNT(*) FROM families").fetchone()[0]
    print(f"migrated {db}: families table ready; "
          f"{len(inserted)} new family rows {inserted}; {total} total")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
