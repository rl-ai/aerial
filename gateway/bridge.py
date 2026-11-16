#!/usr/bin/env python3
"""Ingest bridge (gap A1): scrapes.db -> gateway curated layer, daily.

Moves the day's top matched items from the scrape store into the
second-brain gateway: rank by match-term relevance, convert to markdown
notes with @entity: markers and a sensitivity computed from the item's
source classification (unknown labels FAIL CLOSED — the bridge never
relabels an unclassified item), ingest via the
Curator, rebuild cluster artifacts, and produce a digest file.

Idempotent: a state file (guid -> content hash) under the gateway root
skips guids already ingested; the content-addressed vault handles
identical text regardless.

This is a stopgap; the listening pipeline supersedes it (see README.md).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Port of the match-term groups concept from the scrape sources
# (listening-pipeline.md): an item matches if all terms of any one group
# appear in title+body. Score = number of groups hit; tie-break freshest.
MATCH_GROUPS: list[list[str]] = [
    ["benchmark"],
    ["cost", "pricing"],
    ["gpu", "inference"],
    ["agent", "tooling"],
    ["llm", "model"],
]

ENTITY_RE = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    s = ENTITY_RE.sub("-", (text or "").lower()).strip("-")
    return s[:40] or "unknown"


def _to_epoch(value) -> float | None:
    """Normalize a fetched_at/published value (epoch or ISO text) to epoch."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    if re.fullmatch(r"\d+(\.\d+)?", text):
        return float(text)
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def fetch_items(db_path: str, since_hours: float) -> list[dict]:
    cutoff = time.time() - since_hours * 3600
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT * FROM items").fetchall()
    finally:
        conn.close()
    items = []
    for row in rows:
        item = dict(row)
        ts = _to_epoch(item.get("fetched_at"))
        if ts is not None and ts >= cutoff:
            item["_ts"] = ts
            items.append(item)
    return items


def score_item(item: dict, groups: list[list[str]]) -> int:
    """Number of match-term groups fully hit in title+body."""
    haystack = f"{item.get('title') or ''}\n{item.get('body') or ''}".lower()
    return sum(1 for g in groups
               if g and all(t.lower() in haystack for t in g))


def select_top(items: list[dict], groups, top: int) -> list[dict]:
    matched = [it for it in items if score_item(it, groups) > 0]
    matched.sort(key=lambda it: (-score_item(it, groups),
                                 -it["_ts"],
                                 str(it.get("id"))))
    return matched[:top]


# Sensitivity lattice, mirrored from secondbrain.vault.SENSITIVITY
# (public -> personal -> sensitive -> intimate). The bridge only stamps
# labels from this lattice; a MISSING label defaults to ``personal``
# (the vault's conservative default), an INVALID label fails closed.
SENSITIVITIES = ("public", "personal", "sensitive", "intimate")


class ClassificationError(ValueError):
    """An item's source/policy classification is INVALID (a label not on
    the vault SENSITIVITY lattice).

    Fail-closed: the item is marked in the run's error_details and
    skipped — never relabeled to personal/public by default. A MISSING
    label is not an error: it takes the conservative default ``personal``
    (per the vault design rule).
    """


def note_sensitivity(item: dict) -> str:
    """Sensitivity computed from the item's source/policy classification.

    The scrape source entry carries its ``sensitivity`` field (the same
    classification the listening pipeline honours). Only labels on the
    vault lattice are accepted. A MISSING label (absent or blank) gets
    the conservative default ``personal`` per the vault design rule; an
    INVALID label (present but not on the lattice) raises
    ClassificationError so the item is skipped (fail closed) instead of
    silently relabeled.
    """
    raw = item.get("sensitivity")
    if raw is None or not str(raw).strip():
        return "personal"  # missing label -> conservative default
    label = str(raw).strip().lower()
    if label in SENSITIVITIES:
        return label
    guid = item.get("guid") or item.get("id")
    raise ClassificationError(
        f"unknown sensitivity {label!r} for item {guid!r}: not on the "
        f"lattice {SENSITIVITIES}")


def build_note(item: dict, supersedes: str | None = None) -> str:
    """Markdown note: @entity markers from source_id/topic, computed
    sensitivity (fail-closed on unknown classification).

    G4: ``published:`` is stamped from the source timestamp on every
    note, falling back to ``fetched_at`` when the source has no publish
    date. G2: when this guid has an earlier ingested version, the new
    note carries ``supersedes: <old-hash>`` so the Curator wires the
    supersession edge and the stale version stops surfacing.

    The sensitivity line sits on the note's LAST line (G9: the Curator
    only honours the marker there) and is computed from the item's
    source classification — unknown labels fail closed."""
    entities = []
    for key in ("source_id", "topic"):
        slug = slugify(str(item.get(key) or ""))
        marker = f"@entity:{slug}"
        if marker not in entities:
            entities.append(marker)
    lines = ["\n".join(entities), ""]
    lines.append(f"# {item.get('title') or '(untitled)'}")
    lines.append("")
    if item.get("url"):
        lines.append(f"- url: {item['url']}")
    published = item.get("published") or item.get("fetched_at")
    if published:
        lines.append(f"- published: {published}")
    if supersedes:
        lines.append(f"- supersedes: {supersedes}")
    lines.append(f"- source: {item.get('source_id')}")
    lines.append(f"- topic: {item.get('topic')}")
    lines.append(f"- guid: {item.get('guid')}")
    lines.append("")
    lines.append(str(item.get("body") or ""))
    lines.append("")  # sensitivity stays on the LAST line (G9)
    lines.append(f"sensitivity: {note_sensitivity(item)}")
    return "\n".join(lines) + "\n"


def load_state(state_path: Path) -> dict:
    if state_path.exists():
        try:
            return json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def run(scrapes_db: str, gateway_root: str, since_hours: float = 24,
        top: int = 40, dry_run: bool = False,
        match_groups=None) -> dict:
    groups = match_groups if match_groups is not None else MATCH_GROUPS
    sys.path.insert(0, str(gateway_root))
    from secondbrain import (Curator, Gatekeeper, GrantBook, L0Vault,
                             Workspace)

    root = Path(gateway_root)
    bridge_dir = root / "bridge"
    staging = bridge_dir / "staging"
    state_path = bridge_dir / "state.json"
    digest_path = bridge_dir / "digest-latest.json"

    fetched = fetch_items(scrapes_db, since_hours)
    ranked = select_top(fetched, groups, top)

    state = load_state(state_path)

    def note_digest(text: str) -> str:
        # Same normalized-content hashing as the vault (G1), so the
        # bridge's guid state and the vault agree on identity.
        return hashlib.sha256(
            L0Vault.normalize_text(text).encode("utf-8")).hexdigest()

    # One-time migration: pre-G1 state entries hold raw-text hashes. If
    # the referenced L0 object still exists, remap the guid to the
    # normalized digest of its stored text so unchanged items stay
    # skipped (no duplicate re-ingest, no spurious supersedes marker).
    vault = L0Vault(root / "l0")
    for guid, old in list(state.items()):
        obj = vault.get(str(old)) if old else None
        if obj is not None:
            state[guid] = note_digest(obj.text)

    candidates, dedupe_skipped, errors = [], 0, []
    for item in ranked:
        guid = str(item.get("guid") or f"id-{item.get('id')}")
        try:
            # identity digest is computed WITHOUT the supersedes marker,
            # so an unchanged item stays skipped even once it has history
            identity = note_digest(build_note(item))
            note_text = build_note(item, supersedes=state.get(guid))
        except Exception as exc:  # G6: one bad item never aborts the batch
            errors.append({"guid": guid,
                           "error": f"{type(exc).__name__}: {exc}"})
            continue
        if state.get(guid) == identity:
            dedupe_skipped += 1
            continue
        candidates.append((guid, note_text, item))

    summary = {
        "fetched": len(fetched),
        "selected": len(candidates),
        "ingested": 0,
        "skipped": dedupe_skipped,
        "errors": len(errors),
        "digest_path": None,
        "error_details": errors,
    }
    if dry_run:
        summary["dry_run"] = True
        return summary

    if candidates:
        if staging.exists():
            for old in staging.glob("*.md"):
                old.unlink()
        staging.mkdir(parents=True, exist_ok=True)
        for i, (guid, note_text, _item) in enumerate(candidates):
            name = f"{i:03d}-{slugify(guid)}.md"
            (staging / name).write_text(note_text, encoding="utf-8")

        ws = Workspace(root / "l1")
        ws.load()   # cross-run persistence: supersedes edges + node ids
        curator = Curator(vault, ws)
        report = curator.ingest_folder(staging)
        curator.build_cluster_artifacts()
        ws.publish("main", vault)
        ws.save()   # persist the L1 graph (supersedes edges survive runs)

        summary["ingested"] = len(report["stored"])
        summary["skipped"] += len(report["skipped"])
        summary["errors"] += len(report["errors"])

        # guid -> identity dedupe state (normalized digest WITHOUT any
        # supersedes marker, matching the skip check above)
        for guid, _text, _item in candidates:
            state[guid] = note_digest(build_note(_item))
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(state, indent=1), encoding="utf-8")

    book = GrantBook(root / "ledger.db")
    gatekeeper = Gatekeeper(Workspace(root / "l1"), book)
    since_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    digest = build_digest_summary(book, gatekeeper, candidates,
                                  summary["ingested"], since_ts)
    digest_path.parent.mkdir(parents=True, exist_ok=True)
    digest_path.write_text(json.dumps(digest, indent=1), encoding="utf-8")
    summary["digest_path"] = str(digest_path)
    return summary


def build_digest_summary(book, gatekeeper, candidates, ingested: int,
                         since_ts: str) -> dict:
    """Gateway digest plus the bridge's ingested-items section."""
    from secondbrain import build_digest
    from secondbrain.curator import parse_note
    digest = build_digest(book, gatekeeper, since_ts=since_ts)
    digest["ingested"] = []
    for (_guid, note_text, item) in candidates:
        parsed = parse_note(note_text)
        digest["ingested"].append(
            {"title": (item.get("title") or "(untitled)")[:80],
             "url": item.get("url"),
             "source": item.get("source_id"),
             "topic": item.get("topic"),
             # governance labels, parsed back out of the ingested note
             # (ground truth: what the Curator actually saw)
             "sensitivity": parsed["sensitivity"],
             "entities": parsed["entities"]})
    digest["ingested_count"] = ingested
    return digest


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--scrapes-db", required=True)
    ap.add_argument("--gateway-root", required=True)
    ap.add_argument("--since-hours", type=float, default=24)
    ap.add_argument("--top", type=int, default=40)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    summary = run(args.scrapes_db, args.gateway_root, args.since_hours,
                  args.top, args.dry_run)
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
