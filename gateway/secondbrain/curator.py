"""The Curator: entities-first graph builder over one source type (§9.2).

v1 source type: a notes folder of markdown files. Entities are the primary
clustering axis: a note declares its entities with ``@entity:name`` markers
and soft topics with ``#tag``. Sensitivity is labelled at ingest (marker
``sensitivity: <label>``), conservatively defaulting to ``personal``.

The Curator writes only to L1: original nodes (hash refs), entity nodes,
derived cluster-summary artifacts with mandatory derived_from edges.
Recommended artifacts are proposals — publish (you) makes them L2.
"""

from __future__ import annotations

import re
from pathlib import Path

from .vault import SENSITIVITY


class IngestReport(dict):
    """The ``{stored, skipped, errors}`` per-file isolation report (G6).

    Backward compatible: iterating a report yields the stored hashes, so
    callers that treated ``ingest_folder``'s return value as a hash list
    keep working.
    """

    def __iter__(self):
        return iter(self["stored"])

    def __len__(self):
        return len(self["stored"])


_ENTITY = re.compile(r"@entity:([a-z0-9-]+)")
_TAG = re.compile(r"#([a-z0-9-]+)")
_SENS = re.compile(r"sensitivity:\s*(\w+)")
_SUPERSEDES = re.compile(r"supersedes:\s*([0-9a-f]{64})")


def parse_note(text: str) -> dict:
    """Pull the markers out of a note: entities, tags, sensitivity label,
    and a superseded-hash reference (G2).

    G9 fix: the sensitivity marker is only honoured on the note's LAST
    non-empty line (where writers put front-matter). A phrase like
    ``the sensitivity: public debate`` mid-body can no longer re-label a
    note; unknown or missing labels fall back to the conservative
    ``personal`` default."""
    entities = sorted(set(_ENTITY.findall(text)))
    tags = sorted(set(_TAG.findall(text)))
    supersedes = _SUPERSEDES.findall(text)
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    match = _SENS.fullmatch(lines[-1]) if lines else None
    label = match.group(1) if match and match.group(1) in SENSITIVITY \
        else "personal"  # conservative default
    return {"entities": entities, "tags": tags, "sensitivity": label,
            "supersedes": supersedes}


class Curator:
    """Builds the L1 graph from a notes folder."""

    def __init__(self, vault, workspace) -> None:
        self.vault = vault
        self.ws = workspace

    def ingest_folder(self, folder) -> IngestReport:
        """Ingest every note; returns an IngestReport {stored, skipped,
        errors}. Content-addressed (G1 normalized hashing), so a re-run
        is a no-op for unchanged notes. Per-file error isolation (G6):
        one corrupt or unreadable file is recorded in ``errors`` — it
        never aborts the batch or loses the report of everything else.
        ``skipped`` stays reserved for actual skip decisions (dedupe),
        so a malformed file reports {stored: n, skipped: 0,
        errors: [that one]}."""
        report = IngestReport(stored=[], skipped=[], errors=[])
        for path in sorted(Path(folder).glob("*.md")):
            try:
                text = path.read_text(encoding="utf-8")
                parsed = parse_note(text)
                digest = self.vault.ingest(
                    text, source=str(path.name),
                    sensitivity=parsed["sensitivity"])
            except Exception as exc:  # isolate: record, continue
                report["errors"].append(
                    {"file": path.name,
                     "error": f"{type(exc).__name__}: {exc}"})
                continue
            report["stored"].append(digest)
            if digest in {n.refs[0] for n in self.ws.nodes.values()
                          if n.type == "original" and n.refs}:
                continue
            try:
                note = self.ws.add_node(
                    "original", label=parsed["sensitivity"], refs=(digest,),
                    text=text, meta={"source": path.name})
                for name in parsed["entities"]:
                    entity = self._entity_node(name)
                    self.ws.add_edge(entity.id, note.id, "mentions")
                # G2: supersession edges — this note replaces the
                # referenced older original; the older one stops
                # surfacing as a sibling in cluster artifacts.
                for old_hash in parsed["supersedes"]:
                    for node in self.ws.nodes.values():
                        if node.type == "original" and old_hash in node.refs:
                            self.ws.add_edge(note.id, node.id, "supersedes")
            except Exception as exc:
                report["errors"].append(
                    {"file": path.name,
                     "error": f"{type(exc).__name__}: {exc}"})
        return report

    def _entity_node(self, name: str):
        node_id = f"entity:{name}"
        if node_id not in self.ws.nodes:
            self.ws.add_node("entity", node_id=node_id,
                             text=name.replace("-", " "))
        return self.ws.nodes[node_id]

    def build_cluster_artifacts(self) -> list:
        """One derived summary artifact per entity cluster: which originals
        mention the entity, with mandatory provenance edges (§4 rule 2).

        Idempotent (G3): each artifact is keyed by its entity node id
        (``artifact:entity:<name>``) and rebuilt in place — a re-run updates
        the existing artifact node instead of appending a new one, and
        provenance edges are rebuilt without duplication. Two consecutive
        calls produce the same node set."""
        artifacts = []
        for node in list(self.ws.nodes.values()):
            if node.type != "entity":
                continue
            notes = [e.dst for e in self.ws.edges
                     if e.kind == "mentions" and e.src == node.id]
            # G2: a note that has been superseded is not a sibling — it
            # is history. Exclude it from the cluster artifact.
            superseded = {e.dst for e in self.ws.edges
                          if e.kind == "supersedes"}
            notes = [n for n in notes if n not in superseded]
            if not notes:
                continue
            note_nodes = [self.ws.nodes[n] for n in notes]
            refs = tuple(dict.fromkeys(
                r for n in note_nodes for r in n.refs))
            lines = [f"Cluster: {node.text} — {len(note_nodes)} note(s)."]
            for n in note_nodes:
                lines.append(f"- {n.meta.get('source', '?')}: "
                             + n.text.strip().splitlines()[0][:80])
            artifact_id = f"artifact:{node.id}"
            if artifact_id in self.ws.nodes:
                # rebuild in place: refresh text/refs, drop stale edges
                artifact = self.ws.nodes[artifact_id]
                artifact.text = "\n".join(lines)
                artifact.refs = refs
                self.ws.edges = [
                    e for e in self.ws.edges
                    if not (e.src == artifact_id
                            and e.kind in ("derived_from", "part_of"))
                    and not (e.dst == artifact_id and e.kind == "part_of")]
            else:
                artifact = self.ws.add_node(
                    "artifact", node_id=artifact_id, text="\n".join(lines),
                    refs=refs, meta={"cluster": node.id})
            # provenance: derived_from each source note (sensitivity flows)
            for n in notes:
                self.ws.add_edge(artifact.id, n, "derived_from")
            self.ws.add_edge(node.id, artifact.id, "part_of")
            artifacts.append(artifact)
        self.ws.recompute_labels()
        return artifacts
