"""Sensitivity labeling for BP-KB ingest.

External best-practice sources (framework docs, SRE books, security benchmarks)
are public by definition. The default bridge labels everything 'personal'
(conservative); the BP pipeline overrides to 'public' at labeling time —
explicitly, and only for sources whose provenance is the curated BP set.
"""
from __future__ import annotations

from .schema import validate_practice


def label_sensitivity(record: dict, source_id: str, bp_source_ids: set) -> dict:
    """Label a record. Only BP-set sources get 'public'; anything else stays
    'personal' and is rejected from the BP-KB (fail closed)."""
    if source_id in bp_source_ids:
        record["sensitivity"] = "public"
        return record
    # fail closed: an unknown source never silently becomes public
    record["sensitivity"] = "personal"
    record["_rejected"] = "source not in BP source set"
    return record


def ingest_practice(record: dict, source_id: str, bp_source_ids: set) -> dict:
    """Label then validate. Raises SchemaError if the record is invalid;
    ValueError if the source is not in the BP set."""
    record = label_sensitivity(record, source_id, bp_source_ids)
    if record.get("_rejected"):
        raise ValueError(record["_rejected"])
    return validate_practice(record)
