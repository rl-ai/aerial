"""Source ingestion: sources.json entries + bridge note emission.

Reads the BP source entries (bp_sources.json format == the listening-pipeline
scrape format), validates them against the shared invariants, and converts
matched items into the bridge note format (lanes-ingest-bridge/bridge.py
build_note shape) with sensitivity: public.
"""
from __future__ import annotations

import json

REQUIRED_SOURCE_FIELDS = ("id", "kind", "topic", "target", "every_h", "match")
BP_KINDS = {"web_doc"}


def load_sources(path: str) -> list[dict]:
    """Load and validate BP source entries from a bp_sources.json file."""
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    sources = data.get("sources", [])
    for src in sources:
        missing = [f for f in REQUIRED_SOURCE_FIELDS if f not in src]
        if missing:
            raise ValueError(f"source {src.get('id')!r} missing fields: {missing}")
        if src["kind"] not in BP_KINDS:
            raise ValueError(f"unsupported BP kind: {src['kind']!r}")
        if not isinstance(src["match"], list) or not src["match"]:
            raise ValueError(f"source {src['id']} needs non-empty match groups")
        if src["id"] != f"{src['kind']}.{src['id'].split('.', 1)[1]}":
            raise ValueError(f"id must be <kind>.<slug>: {src['id']!r}")
    return sources


def source_ids(sources: list[dict]) -> set:
    return {s["id"] for s in sources}


def score_item(item: dict, groups: list[list[str]]) -> int:
    """Match-term scoring: number of groups fully hit in title+body."""
    hay = f"{item.get('title') or ''}\n{item.get('body') or ''}".lower()
    return sum(1 for g in groups if g and all(t.lower() in hay for t in g))


def build_note(item: dict, source: dict) -> str:
    """Bridge note (build_note shape from lanes-ingest-bridge) labeled public."""
    import re
    slug = re.sub(r"[^a-z0-9]+", "-", (source["topic"] or "").lower()).strip("-")[:40]
    lines = [f"@entity:bp-{slug}", f"@entity:{source['id']}", "",
             f"# {item.get('title') or '(untitled)'}", ""]
    if item.get("url"):
        lines.append(f"- url: {item['url']}")
    lines.append(f"- source: {source['id']}")
    lines.append(f"- framework: {source['topic']}")
    lines.append("")
    lines.append(str(item.get("body") or ""))
    lines.append("")
    lines.append("sensitivity: public")
    return "\n".join(lines) + "\n"
