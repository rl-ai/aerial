"""L0 — the original vault: append-only, content-addressed.

Hash = identity. There is no mutation and no delete of content: a tombstone
only marks an original as removed for graph-pruning purposes (design doc §3,
rule 4.2 — deletion shrinks the brain at publish, the bytes stay).
Only the Curator reads it fully; nothing else ever touches it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

#: The sensitivity lattice, ordered low -> high (design doc §4).
SENSITIVITY = ("public", "personal", "sensitive", "intimate")


def label_rank(label: str) -> int:
    """Rank of a sensitivity label; unknown labels rank highest."""
    try:
        return SENSITIVITY.index(label)
    except ValueError:
        return len(SENSITIVITY)


def union_label(labels) -> str:
    """A derived node's label = the union (max) of its sources' labels."""
    best = "public"
    for label in labels:
        if label_rank(label) > label_rank(best):
            best = label
    return best


@dataclass(frozen=True)
class Original:
    hash: str
    source: str
    ingested_at: str
    sensitivity: str
    text: str


class L0Vault:
    """Content-addressed append-only store on disk (one JSON file per hash)."""

    def __init__(self, root) -> None:
        self.root = Path(root)
        (self.root / "objects").mkdir(parents=True, exist_ok=True)
        self._tomb_path = self.root / "tombstones.json"

    # -- internals ---------------------------------------------------------

    def _path(self, digest: str) -> Path:
        return self.root / "objects" / f"{digest}.json"

    @staticmethod
    def normalize_text(text: str) -> str:
        """Canonical form for identity (G1): whitespace runs collapse to a
        single space and the ends are trimmed. Release-note re-renders and
        reposts that differ only in line wrapping hash identically, so the
        vault cannot flood with near-duplicates."""
        return re.sub(r"\s+", " ", text or "").strip()

    @classmethod
    def _digest(cls, text: str) -> str:
        return hashlib.sha256(cls.normalize_text(text).encode("utf-8")).hexdigest()

    def _tombstones(self) -> dict:
        if not self._tomb_path.exists():
            return {}
        try:
            return json.loads(self._tomb_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, ValueError):
            # G8: a corrupt tombstone file is treated as empty rather than
            # crashing is_alive/publish; the next tombstone() write repairs
            # it wholesale (atomic replace below).
            return {}

    # -- the append-only API -------------------------------------------------

    def ingest(self, text: str, *, source: str, sensitivity: str = "personal",
               now: float | None = None) -> str:
        """Store an original; returns its hash. Re-ingesting identical text
        is a no-op returning the same hash (content-addressed identity)."""
        if sensitivity not in SENSITIVITY:
            raise ValueError(f"unknown sensitivity label: {sensitivity}")
        digest = self._digest(text)
        path = self._path(digest)
        if not path.exists():
            doc = {"hash": digest, "source": source,
                   "ingested_at": time.strftime(
                       "%Y-%m-%dT%H:%M:%SZ", time.gmtime(now or time.time())),
                   "sensitivity": sensitivity, "text": text}
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(doc, ensure_ascii=False),
                           encoding="utf-8")
            os.replace(tmp, path)  # atomic: a partial object is never visible
        return digest

    def get(self, digest: str) -> Original | None:
        path = self._path(digest)
        if not path.exists():
            return None
        doc = json.loads(path.read_text(encoding="utf-8"))
        return Original(**doc)

    def tombstone(self, digest: str) -> None:
        """Mark an original deleted. The content stays (append-only); the
        published graph prunes anything whose provenance relied on it."""
        tombs = self._tombstones()
        tombs.setdefault(digest, time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        # G8: same tmp + os.replace treatment as objects (vault.py:85-88) —
        # a crash mid-write never leaves a partial tombstone file visible.
        tmp = self._tomb_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(tombs, indent=2), encoding="utf-8")
        os.replace(tmp, self._tomb_path)

    def is_alive(self, digest: str) -> bool:
        """A hash is alive when the object exists and is not tombstoned."""
        return self._path(digest).exists() and digest not in self._tombstones()

    def all_hashes(self) -> list[str]:
        return sorted(p.stem for p in (self.root / "objects").glob("*.json"))
