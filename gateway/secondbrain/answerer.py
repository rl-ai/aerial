"""The answer worker: stateless, per-request, seated on a scope slice (§6).

The seat is topology: the worker compiles the grant's scope closure from the
pinned L2 version and can only see that subgraph — it has no handle on the
vault or anything outside the slice. It retrieves, drafts with citation
hashes, and dies.
"""

from __future__ import annotations

import re

_WORD = re.compile(r"[a-z0-9-]+")

#: Function words that carry no topical signal — matching on them made any
#: draft pull in unrelated notes ("at", "the", ...).
_STOPWORDS = frozenset(
    "a an about and are as at be by did do for from had has have how i in is it "
    "me my of on or say says that the their them then there these they this "
    "to was we what when where which who why will with you your".split())


def _stems(text: str) -> set[str]:
    """Lowercase word stems (crude suffix strip) minus stopwords."""
    out = set()
    for word in _WORD.findall(text.lower()):
        if word in _STOPWORDS:
            continue
        for suffix in ("ing", "ed", "es", "s"):
            if len(word) > 3 and word.endswith(suffix):
                word = word[: -len(suffix)]
                break
        out.add(word)
    return out


class Seat:
    """The compiled scope slice @ a pinned L2 version."""

    def __init__(self, workspace, version: dict, node_ids: set[str]) -> None:
        self.nodes = {nid: workspace.nodes.get(nid, version["nodes"][nid])
                      for nid in node_ids}
        self.edges = [e for e in version["edges"]
                      if e.src in node_ids and e.dst in node_ids]
        self.node_ids = set(node_ids)


def compile_seat(workspace, grant: dict) -> Seat:
    """The only way a worker touches the graph: scope = anchor entities +
    provenance closure, from the grant's pinned L2 version (§4 rule 1)."""
    version = workspace.resolve(grant["alias"])
    node_ids = workspace.scope_closure(version, grant["anchor_entities"])
    return Seat(workspace, version, node_ids)


def retrieve(seat: Seat, question: str) -> list:
    """Rank slice nodes by word overlap with the question.

    v1 drafts from **originals only**: artifacts are the curation layer for
    your review (they union-label along their whole cluster — one intimate
    note would taint every summary it touches), while answers must cite
    originals anyway (rule 3)."""
    words = _stems(question)
    scored = []
    for node in seat.nodes.values():
        if node.type != "original":
            continue
        overlap = len(words & _stems(node.text))
        if overlap:
            scored.append((overlap, node))
    scored.sort(key=lambda pair: (-pair[0], pair[1].id))
    return [node for _, node in scored[:4]]


def draft_answer(seat: Seat, question: str) -> dict:
    """A draft answer with citation hashes: only lines traceable to an
    original in the slice, each citing its L0 hash (§4 rule 3)."""
    picked = retrieve(seat, question)
    if not picked:
        return {"text": "", "citations": [], "node_ids": [], "covered": False}
    lines: list[str] = []
    citations: list[str] = []
    node_ids: list[str] = []
    for node in picked:
        hashes = [r for r in node.refs if len(r) == 64]
        if not hashes:
            continue  # uncited content is never drafted (rule 3)
        for line in node.text.strip().splitlines():
            line = line.strip()
            if line and not line.startswith("#") \
                    and not line.startswith("sensitivity:") \
                    and not line.startswith("@entity:"):
                lines.append(f"{line} [{hashes[0][:12]}]")
        citations.extend(hashes)
        node_ids.append(node.id)
    return {"text": "\n".join(lines), "citations": sorted(set(citations)),
            "node_ids": node_ids, "covered": bool(lines)}
