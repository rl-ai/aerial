"""secondbrain_mcp — the governed read path for any agent over your private
notes, exposed over the Model Context Protocol (stdio).

Exactly two tools (SPEC §4):
- brain_search: search the workspace graph; every result passes the
  gatekeeper pipeline in order (intimate refusal -> scope closure ->
  sensitivity label -> disclosure sanitization). Refused items are counted
  in ``refused_count`` — structured governance, never errors.
- brain_get: full document by hash, or a structured refusal.

Identity = grant: the session binds to exactly one grant (--grant/--key).
Fail closed on any check the server cannot complete (SPEC §3.2): a degraded
gateway returns tool errors, never open content.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Reuse the secondbrain modules from the lane dir; never copy-paste logic.
_LANE = Path(__file__).resolve().parent.parent
if str(_LANE) not in sys.path:
    sys.path.insert(0, str(_LANE))

from secondbrain import Gatekeeper, GrantBook, L0Vault, Workspace  # noqa: E402
from secondbrain.answerer import _stems  # noqa: E402
from secondbrain.grants import GrantError  # noqa: E402
from secondbrain.vault import label_rank  # noqa: E402

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "secondbrain-mcp", "version": "0.1.0"}

TOOLS = [
    {
        "name": "brain_search",
        "description": (
            "Search the governed knowledge graph. Every result carries a "
            "sensitivity label, provenance, and sanitized text; content "
            "that is intimate or outside the session grant's scope is "
            "refused and only counted in refused_count."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "What to look for in the notes."},
                "entity": {"type": "string",
                           "description": "Optional entity name filter."},
                "tag": {"type": "string",
                        "description": "Optional tag filter."},
                "limit": {"type": "integer",
                          "description": "Max results (default 8, max 25)."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "brain_get",
        "description": (
            "Fetch one full document by its hash (from a prior brain_search "
            "result). Returns a structured refusal for intimate, "
            "out-of-scope, tombstoned or unknown hashes."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "hash": {"type": "string",
                         "description": "The document hash (64 hex chars)."},
            },
            "required": ["hash"],
        },
    },
]


class GatewayError(Exception):
    """Identity/infrastructure failure -> MCP tool error, fail closed."""


class GatewaySession:
    """One MCP client session bound to exactly one grant."""

    def __init__(self, vault_root, grant_id: str, key: str) -> None:
        root = Path(vault_root).expanduser()
        self.vault = L0Vault(root / "l0")
        self.ws = Workspace(root / "l1")
        self._load_l1(root / "l1" / "l1.json")
        self.book = GrantBook(root / "grants.db")
        self.gk = Gatekeeper(self.ws, self.book,
                             flag_log=root / "flags.jsonl")
        self.grant_id = grant_id
        self.key = key
        self._grant = None
        self._scope: set[str] = set()
        self._version = None

    def _load_l1(self, path: Path) -> None:
        """Restore L1 (nodes/edges/aliases) from l1.json. Note: L2 version
        snapshots are not persisted by Workspace.save(); the pinned version
        is reconstructed from the current L1 state in _authenticate."""
        if not path.exists():
            return
        from secondbrain.graph import Edge, Node
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            # corrupt/unreadable workspace -> fail closed (SPEC §3.2)
            raise GatewayError(
                f"vault_unreadable: refusing to serve (fail closed): {exc}"
            ) from None
        counter = 0
        for nid, n in doc.get("nodes", {}).items():
            self.ws.nodes[nid] = Node(
                n["id"], n["type"], n.get("label", "personal"),
                tuple(n.get("refs") or ()), n.get("text", ""),
                dict(n.get("meta") or {}))
            digits = "".join(ch for ch in nid.split("-")[-1] if ch.isdigit())
            if digits:
                counter = max(counter, int(digits))
        self.ws.edges = [Edge(e["src"], e["dst"], e["kind"])
                         for e in doc.get("edges", [])]
        self.ws.aliases = dict(doc.get("aliases", {}))
        self.ws._counter = counter

    # -- identity & scope (fail closed) --------------------------------------

    def _authenticate(self) -> dict:
        """Validate the grant and compile the scope closure on every call.
        Any failure raises GatewayError: never fall back to unscoped."""
        try:
            grant = self.book.active(self.grant_id, self.key)
            self.book.check_budget(grant)
            try:
                version = self.ws.resolve(grant["alias"])
            except KeyError:
                # L2 snapshots are not persisted (Workspace.save() stores
                # nodes/edges/aliases only): reconstruct the pinned version
                # from the current L1 state under the grant's alias.
                version_id = self.ws.aliases.get(grant["alias"])
                if version_id is None:
                    raise GatewayError(
                        f"grant_invalid: alias {grant['alias']!r} not "
                        "published in this vault (fail closed)") from None
                version = {"nodes": dict(self.ws.nodes),
                           "edges": list(self.ws.edges)}
                self.ws.versions[version_id] = version
                self.ws.aliases[grant["alias"]] = version_id
            scope = self.ws.scope_closure(version, grant["anchor_entities"])
        except GrantError as exc:
            raise GatewayError(f"grant_invalid: {exc}") from None
        except GatewayError:
            raise
        except Exception as exc:  # corrupt index, unreadable closure, IO
            raise GatewayError(
                f"vault_unreadable: refusing to serve (fail closed): {exc}")
        self._grant, self._scope, self._version = grant, scope, version
        return grant

    # -- the gatekeeper pipeline per result (SPEC §3.2, in order) -------------

    def _governed_item(self, node, doc, query: str, sanitize: bool) -> dict | None:
        """Run one candidate through the pipeline. Returns the governed
        result dict, or None when refused (intimate / outside scope)."""
        grant = self._grant
        # 1. intimate refusal — never the content, regardless of scope
        label = node.label if node else doc.sensitivity
        if label not in ("public", "personal", "sensitive", "intimate"):
            label = "personal"                      # conservative default
        if label_rank(label) >= label_rank("intimate"):
            self.gk._flag(grant, query, "intimate-label content withheld")
            return None
        # 2. scope closure — citation closure must be inside the grant scope
        if node is None or node.id not in self._scope:
            self.gk._flag(grant, query, "outside scope closure")
            return None
        # 3. sensitivity label carried on the item
        # 4. disclosure sanitization on the text
        text = doc.text
        was_sanitized = False
        if sanitize:
            clean = self.gk._sanitize(grant, text)
            if clean != text:
                self.gk._flag(grant, query,
                              "disclosure sanitization applied to result")
                text = clean
                was_sanitized = True
        citations = [r for r in node.refs if len(r) == 64] if node else []
        entities = sorted(
            self._version["nodes"][e.src].text
            for e in self._version["edges"]
            if e.kind == "mentions" and e.dst == node.id
            and e.src in self._version["nodes"])
        return {
            "hash": doc.hash,
            "label": label,
            "sensitivity": label,
            "source": doc.source,
            "ingested_at": doc.ingested_at,
            "snippet": text[:280],
            "entities": entities,
            "citations": citations,
        }, text, was_sanitized

    # -- tools -----------------------------------------------------------------

    def brain_search(self, args: dict) -> dict:
        grant = self._authenticate()
        query = str(args.get("query", ""))
        try:
            limit = int(args.get("limit") or 8)
        except (TypeError, ValueError):
            limit = 8
        limit = max(1, min(25, limit))
        entity_filter = args.get("entity")
        tag_filter = args.get("tag")

        words = _stems(query)
        # entity mentions per original node, for filtering + scoring
        mentions: dict[str, list[str]] = {}
        for e in self._version["edges"]:
            if e.kind == "mentions":
                mentions.setdefault(e.dst, []).append(e.src)

        scored = []
        for nid, node in self._version["nodes"].items():
            if node.type != "original" or not node.refs:
                continue
            entity_ids = mentions.get(nid, [])
            if entity_filter is not None:
                names = [self._version["nodes"][x].text.replace(" ", "-")
                         for x in entity_ids if x in self._version["nodes"]]
                if entity_filter not in entity_ids and entity_filter not in names:
                    continue
            if tag_filter is not None and tag_filter not in node.meta.get(
                    "tags", []):
                continue
            entity_words: set[str] = set()
            for x in entity_ids:
                enode = self._version["nodes"].get(x)
                if enode:
                    entity_words |= _stems(enode.text)
            score = len(words & _stems(node.text)) + 2 * len(words & entity_words)
            if words and score == 0:
                continue
            scored.append((score, nid))
        scored.sort(key=lambda pair: (-pair[0], pair[1]))

        results, refused_count, sanitized_count = [], 0, 0
        for _, nid in scored:
            node = self._version["nodes"][nid]
            refs64 = [r for r in node.refs if len(r) == 64]
            doc_hash = refs64[0] if refs64 else None
            if doc_hash is None:
                raise GatewayError(
                    "vault_unreadable: graph node without an L0 hash "
                    "(fail closed)")
            doc = self.vault.get(doc_hash)
            if doc is None:
                raise GatewayError(
                    f"vault_unreadable: object {doc_hash[:12]} missing for a "
                    "graph node (fail closed)")
            if not self.vault.is_alive(doc_hash):
                continue  # tombstoned: withheld, not counted as a refusal
            governed = self._governed_item(node, doc, query, sanitize=True)
            if governed is None:
                refused_count += 1
                continue
            item, _, item_sanitized = governed
            if item_sanitized:
                sanitized_count += 1
            results.append(item)
            if len(results) >= limit:
                break

        self._audit(query, results, refused_count, sanitized_count)
        return {"results": results,
                "refused_count": refused_count,
                "sanitized_count": sanitized_count}

    def brain_get(self, args: dict) -> dict:
        grant = self._authenticate()
        doc_hash = str(args.get("hash", "")).strip()
        doc = self.vault.get(doc_hash) if doc_hash else None
        if doc is None or not self.vault.is_alive(doc_hash):
            # unknown and tombstoned are indistinguishable (no deletion leak)
            self._audit(f"get {doc_hash[:12]}", [], 0, 0, refused=True,
                        reason="not_found")
            return {"refused": True, "reason": "not_found"}
        node = next((n for n in self._version["nodes"].values()
                     if n.type == "original" and doc_hash in n.refs), None)
        governed = self._governed_item(node, doc, f"get {doc_hash[:12]}",
                                       sanitize=True)
        if governed is None:
            # the pipeline refused: intimate (node known) or outside scope
            reason = "intimate" if (
                node and label_rank(node.label) >= label_rank("intimate")
            ) else "outside_scope"
            self._audit(f"get {doc_hash[:12]}", [], 0, 0, refused=True,
                        reason=reason)
            return {"refused": True, "reason": reason}
        item, text, _ = governed
        self._audit(f"get {doc_hash[:12]}", [item], 0, 0)
        return {"hash": doc.hash, "label": item["label"],
                "sensitivity": item["sensitivity"], "text": text,
                "entities": item["entities"], "citations": item["citations"],
                "provenance": {"source": doc.source,
                               "ingested_at": doc.ingested_at}}

    # -- audit (SPEC §3.3) ------------------------------------------------------

    def _audit(self, question, results, refused_count, sanitized_count, *,
               refused=False, reason="") -> None:
        """Every tool call lands in the existing grant ledger."""
        try:
            state = "refused" if refused else "answered"
            if reason:
                why = reason
            elif refused_count or sanitized_count:
                why = (f"mcp call: {len(results)} returned, "
                       f"{refused_count} refused, {sanitized_count} sanitized")
            else:
                why = "mcp call"
            self.book.log(
                self.grant_id, state, question=question,
                released=json.dumps(results)[:2000] if results else "",
                reason=why)
        except Exception:
            pass  # audit is best-effort; ledger errors never open content


# -- MCP protocol framing (JSON-RPC 2.0 over stdio) ---------------------------


def _result(id_, result):
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def _error(id_, code, message):
    return {"jsonrpc": "2.0", "id": id_,
            "error": {"code": code, "message": message}}


class MCPServer:
    def __init__(self, session: GatewaySession) -> None:
        self.session = session

    def handle(self, msg: dict):
        """Handle one JSON-RPC message; returns a reply dict or None for
        notifications."""
        method = msg.get("method", "")
        id_ = msg.get("id")
        params = msg.get("params") or {}
        if method == "initialize":
            return _result(id_, {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": SERVER_INFO,
            })
        if method.startswith("notifications/"):
            return None  # notifications never get a reply
        if method == "tools/list":
            return _result(id_, {"tools": TOOLS})
        if method == "tools/call":
            return self._call(id_, params)
        if id_ is not None:
            return _error(id_, -32601, f"method not found: {method}")
        return None

    def _call(self, id_, params):
        name = params.get("name", "")
        args = params.get("arguments") or {}
        try:
            if name == "brain_search":
                data = self.session.brain_search(args)
            elif name == "brain_get":
                data = self.session.brain_get(args)
            else:
                return _error(id_, -32602, f"unknown tool: {name}")
        except GatewayError as exc:
            # identity/infrastructure: tool error, fail closed (SPEC §3.2,
            # §4.3) — never open content, never unscoped fallback.
            return _result(id_, {
                "content": [{"type": "text", "text": str(exc)}],
                "structuredContent": {"error": str(exc).split(":")[0],
                                      "detail": str(exc)},
                "isError": True,
            })
        except Exception as exc:
            return _result(id_, {
                "content": [{"type": "text",
                             "text": f"vault_error (fail closed): {exc}"}],
                "structuredContent": {"error": "vault_error",
                                      "detail": str(exc)},
                "isError": True,
            })
        return _result(id_, {
            "content": [{"type": "text", "text": json.dumps(data)}],
            "structuredContent": data,
            "isError": False,
        })

    def serve(self, stdin=None, stdout=None) -> None:
        stdin = stdin or sys.stdin
        stdout = stdout or sys.stdout
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                reply = _error(None, -32700, "parse error")
            else:
                reply = self.handle(msg)
            if reply is not None:
                stdout.write(json.dumps(reply) + "\n")
                stdout.flush()


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(
        prog="secondbrain-mcp",
        description="Governed knowledge gateway over MCP stdio.")
    parser.add_argument("--vault", required=True,
                        help="Workspace root (contains l0/, l1/, grants.db).")
    parser.add_argument("--grant", required=True, help="Grant id.")
    parser.add_argument("--key", required=True, help="Grant key (shown once).")
    args = parser.parse_args(argv)
    session = GatewaySession(args.vault, args.grant, args.key)
    MCPServer(session).serve()


if __name__ == "__main__":
    main()
