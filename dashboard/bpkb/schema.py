"""Practice-record schema and validation."""
from __future__ import annotations

VALID_STATUS = {"compliant", "gap", "na"}
VALID_SENSITIVITY = {"public"}
VALID_PILLARS = {"reliability", "security", "cost", "operational-excellence", "sre"}
REQUIRED_FIELDS = ("id", "source", "pillar", "statement", "status", "evidence", "decision_point")


class SchemaError(ValueError):
    pass


def validate_practice(rec: dict) -> dict:
    """Validate one practice record; returns it unchanged or raises SchemaError."""
    if not isinstance(rec, dict):
        raise SchemaError("practice must be a dict")
    missing = [f for f in REQUIRED_FIELDS if not str(rec.get(f) or "").strip()]
    if missing:
        raise SchemaError(f"missing fields: {missing}")
    if rec["status"] not in VALID_STATUS:
        raise SchemaError(f"bad status {rec['status']!r}")
    if rec["pillar"] not in VALID_PILLARS:
        raise SchemaError(f"bad pillar {rec['pillar']!r}")
    if not rec["id"].startswith("bp."):
        raise SchemaError(f"id must start 'bp.': {rec['id']!r}")
    dp = rec["decision_point"]
    if dp != "none" and not (dp.startswith("D") and dp[1:].isdigit() and 1 <= int(dp[1:]) <= 12):
        raise SchemaError(f"decision_point must be D1-D12 or 'none': {dp!r}")
    if rec.get("sensitivity") not in (None, "public"):
        raise SchemaError("sensitivity must be 'public' at ingestion")
    rec = dict(rec)
    rec["sensitivity"] = "public"
    rec.setdefault("links", [])
    if not isinstance(rec["links"], list):
        raise SchemaError("links must be a list")
    return rec


def validate_kb(records: list) -> list:
    """Validate a full KB; duplicate ids rejected."""
    seen = set()
    out = []
    for rec in records:
        rec = validate_practice(rec)
        if rec["id"] in seen:
            raise SchemaError(f"duplicate practice id: {rec['id']}")
        seen.add(rec["id"])
        out.append(rec)
    return out
