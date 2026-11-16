"""L1 workspace + L2 published graph (design doc §3-§4).

L1 holds hash references and everything the Curator writes; all blast radius
lives here until publish. L2 is a versioned snapshot: publish = atomic alias
bump, revert = repoint alias. Grants pin an L2 version.

Rules implemented here:
- scope = entity cluster + its provenance closure (a graph query, §4 rule 1);
- sensitivity propagates along derived_from edges (union of sources);
- a derived node with no live derived_from path to a surviving original is
  invalid and pruned at publish (rule 4.2) — deletion shrinks the brain;
- every answer must cite original hashes (checked at the gatekeeper).
"""

from __future__ import annotations

import copy
import json
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path

from .vault import label_rank, union_label

NODE_TYPES = ("original", "entity", "event", "artifact", "human_edit")
EDGE_TYPES = ("mentions", "relates_to", "part_of", "derived_from", "tagged",
              "supersedes")


@dataclass
class Node:
    id: str
    type: str
    label: str = "personal"          # sensitivity label (conservative default)
    refs: tuple[str, ...] = ()       # L0 hashes (originals) or node ids
    text: str = ""
    meta: dict = field(default_factory=dict)


@dataclass
class Edge:
    src: str
    dst: str
    kind: str


class Workspace:
    """The L1 graph the Curator builds, plus the L2 publish/revert machinery."""

    def __init__(self, root) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.nodes: dict[str, Node] = {}
        self.edges: list[Edge] = []
        self.aliases: dict[str, str] = {}      # alias -> version id
        self.versions: dict[str, dict] = {}    # version -> {"nodes","edges"}
        self._counter = 0

    # -- construction --------------------------------------------------------

    def _fresh_id(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}-{self._counter:04d}"

    def load(self) -> bool:
        """Restore L1 from l1.json written by save() (cross-run
        persistence: supersession edges and node ids survive). Returns
        True when a snapshot was loaded."""
        path = self.root / "l1.json"
        if not path.exists():
            return False
        doc = json.loads(path.read_text(encoding="utf-8"))
        self.nodes = {nid: Node(n["id"], n["type"], n.get("label", "personal"),
                                tuple(n.get("refs") or ()), n.get("text", ""),
                                dict(n.get("meta") or {}))
                      for nid, n in doc.get("nodes", {}).items()}
        self.edges = [Edge(e["src"], e["dst"], e["kind"])
                      for e in doc.get("edges", [])]
        self.aliases = dict(doc.get("aliases", {}))
        counter = 0
        for nid in self.nodes:
            try:
                counter = max(counter, int(nid.rsplit("-", 1)[-1]))
            except ValueError:
                continue
        self._counter = counter
        return True

    def add_node(self, type_: str, *, label: str = "personal",
                 refs: tuple = (), text: str = "", meta: dict | None = None,
                 node_id: str | None = None) -> Node:
        if type_ not in NODE_TYPES:
            raise ValueError(f"unknown node type: {type_}")
        node = Node(node_id or self._fresh_id(type_), type_, label,
                    tuple(refs), text, dict(meta or {}))
        self.nodes[node.id] = node
        return node

    def add_edge(self, src: str, dst: str, kind: str) -> Edge:
        if kind not in EDGE_TYPES:
            raise ValueError(f"unknown edge type: {kind}")
        if src not in self.nodes or dst not in self.nodes:
            raise KeyError("edge endpoints must exist")
        edge = Edge(src, dst, kind)
        self.edges.append(edge)
        return edge

    # -- provenance & sensitivity ---------------------------------------------

    def provenance_closure(self, node_ids) -> set[str]:
        """Node ids plus everything reachable backwards over derived_from."""
        seen: set[str] = set()
        stack = list(node_ids)
        while stack:
            nid = stack.pop()
            if nid in seen:
                continue
            seen.add(nid)
            for edge in self.edges:
                if edge.kind == "derived_from" and edge.src == nid:
                    stack.append(edge.dst)
        return seen

    def live_originals(self, vault, node_ids) -> set[str]:
        """L0 hashes reachable from these nodes that are still alive."""
        hashes: set[str] = set()
        for nid in self.provenance_closure(node_ids):
            node = self.nodes[nid]
            hashes.update(r for r in node.refs
                          if r in vault.all_hashes() or len(r) == 64)
        return {h for h in hashes if vault.is_alive(h)}

    def recompute_labels(self) -> None:
        """Sensitivity propagates along derived_from: a derived node's label
        is the union of its provenance closure's labels (§4 rule 2)."""
        for node in self.nodes.values():
            if node.type in ("artifact", "event"):
                closure = self.provenance_closure([node.id])
                node.label = union_label(
                    self.nodes[n].label for n in closure if n != node.id)

    # -- publish / revert ------------------------------------------------------

    def publish(self, alias: str, vault=None) -> str:
        """Snapshot L1 into a new immutable L2 version and bump the alias.

        Before snapshotting: derived nodes with no live derived_from path to
        a surviving original are pruned (definitional, rule 4.2). With a
        vault, "surviving" means not tombstoned in L0."""
        pruned = self.prune_unrooted(vault)
        self.recompute_labels()
        version = "v" + time.strftime("%Y%m%dT%H%M%S", time.gmtime()) \
            + "-" + secrets.token_hex(2)
        self.versions[version] = {
            "nodes": {nid: copy.copy(n) for nid, n in self.nodes.items()
                      if nid not in pruned},
            "edges": [copy.copy(e) for e in self.edges
                      if e.src not in pruned and e.dst not in pruned],
        }
        self.aliases[alias] = version  # atomic bump: dict assignment
        return version

    def prune_unrooted(self, vault=None) -> set[str]:
        """Derived nodes with no live original in their provenance closure
        are invalid by definition (rule 4.2). With a vault, an original is
        live only when its L0 object is not tombstoned. Returns the pruned
        ids (they stay in L1 history but never enter L2)."""
        pruned: set[str] = set()
        for node in list(self.nodes.values()):
            if node.type not in ("artifact", "event", "human_edit"):
                continue
            roots = [n for n in self.provenance_closure([node.id])
                     if self.nodes[n].type == "original"
                     and (vault is None or vault.is_alive(
                         next(iter(self.nodes[n].refs), "")))]
            if not roots:
                pruned.add(node.id)
        for nid in pruned:
            self.nodes.pop(nid, None)
        self.edges = [e for e in self.edges
                      if e.src not in pruned and e.dst not in pruned]
        return pruned

    def resolve(self, alias: str) -> dict:
        """The pinned version's nodes/edges (what a grant's seat compiles)."""
        if alias not in self.aliases:
            raise KeyError(f"unknown alias: {alias}")
        return self.versions[self.aliases[alias]]

    # -- scope slices (the seat) -------------------------------------------------

    def scope_closure(self, version: dict, anchor_entities) -> set[str]:
        """Anchor entities + provenance closure + everything that mentions
        them — the scope slice an answer worker is seated on (§4 rule 1)."""
        nodes: dict = version["nodes"]
        anchors = set(anchor_entities) & set(nodes)
        slice_: set[str] = set()
        for nid, node in nodes.items():
            if node.type == "entity" and nid in anchors:
                slice_.add(nid)
                continue
            linked = any(e.kind in ("mentions", "part_of")
                         and ((e.src == nid and e.dst in anchors)
                              or (e.dst == nid and e.src in anchors))
                         for e in version["edges"])
            if linked or nid in anchors:
                slice_.add(nid)
        # provenance closure pulls in the originals behind derived nodes
        closure: set[str] = set()
        stack = list(slice_)
        while stack:
            nid = stack.pop()
            if nid in closure:
                continue
            closure.add(nid)
            for edge in version["edges"]:
                if edge.kind == "derived_from" and edge.src == nid:
                    stack.append(edge.dst)
        return closure

    # -- persistence (L1 wipe/rebuild survives via L0 + this file) ---------------

    def save(self) -> None:
        doc = {
            "nodes": {nid: {"id": n.id, "type": n.type, "label": n.label,
                            "refs": list(n.refs), "text": n.text,
                            "meta": n.meta}
                      for nid, n in self.nodes.items()},
            "edges": [{"src": e.src, "dst": e.dst, "kind": e.kind}
                      for e in self.edges],
            "aliases": self.aliases,
        }
        (self.root / "l1.json").write_text(json.dumps(doc, indent=2),
                                           encoding="utf-8")
