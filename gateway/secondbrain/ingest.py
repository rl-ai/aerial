"""Plugin ingest (landscape B10): each source kind is an adapter that
emits originals with provenance into L0/L1 — nothing downstream changes
per source type.

v1 ships one adapter (notes folder) through the registry, so adding a
mail or chat-log adapter later is a registration, not a rewrite.
"""

from __future__ import annotations

from pathlib import Path

from .curator import Curator, parse_note
from .vault import L0Vault


class IngestAdapter:
    """One source kind. ``kind`` names it; ``ingest`` returns the hashes it
    stored. Adapters own parsing; the graph schema stays fixed."""

    kind = "abstract"

    def ingest(self, source_ref) -> list[str]:  # pragma: no cover - interface
        raise NotImplementedError


class NotesFolderAdapter(IngestAdapter):
    """Markdown notes with @entity/#tag/sensitivity markers (v1 source)."""

    kind = "notes-folder"

    def __init__(self, vault: L0Vault, curator: Curator) -> None:
        self.vault = vault
        self.curator = curator

    def ingest(self, source_ref) -> list[str]:
        return self.curator.ingest_folder(source_ref)

    def describe(self, source_ref) -> dict:
        """What an ingest WOULD do (the proposal loop, dry by default)."""
        files = sorted(Path(source_ref).glob("*.md"))
        return {"kind": self.kind, "files": [f.name for f in files],
                "entities": sorted({e for f in files
                                    for e in parse_note(
                                        f.read_text(encoding="utf-8"))
                                    ["entities"]})}


class Registry:
    """Kind -> adapter. Register at startup; ingest by kind."""

    def __init__(self) -> None:
        self._adapters: dict[str, IngestAdapter] = {}

    def register(self, adapter: IngestAdapter) -> None:
        if adapter.kind in self._adapters:
            raise ValueError(f"adapter kind already registered: {adapter.kind}")
        self._adapters[adapter.kind] = adapter

    def kinds(self) -> list[str]:
        return sorted(self._adapters)

    def ingest(self, kind: str, source_ref) -> list[str]:
        if kind not in self._adapters:
            raise KeyError(f"no adapter registered for kind {kind!r}")
        return self._adapters[kind].ingest(source_ref)
