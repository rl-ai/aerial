"""Minimal in-memory FakeStore implementing the ticket_hub.store.py contract
surface from DESIGN.md, so Lane B (api.py + UI) tests run standalone while
Lane A builds the real store concurrently.

Contract surface: create_ticket, get, list, patch, append_event, add_edge,
children, ancestry, descendants, related, timeline, bind_thread, board
(+ list_bindings / get_binding used by the API's thread endpoints).
"""

from __future__ import annotations

import itertools
import datetime as _dt

_STATUSES = ["draft", "open", "running", "blocked", "review", "done", "cancelled"]


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


class CycleError(ValueError):
    """Raised when an edge would create a cycle."""


class FakeStore:
    def __init__(self):
        self.tickets: dict[str, dict] = {}
        self.events: list[dict] = []
        self.edges: list[dict] = []
        self.bindings: list[dict] = []
        self.pages: list[dict] = []
        self._ids = itertools.count(1)

    # -- helpers ----------------------------------------------------------

    def _next_id(self, prefix: str) -> str:
        return f"{prefix}-{next(self._ids):04d}"

    def _require(self, ticket_id: str) -> dict:
        ticket = self.tickets.get(ticket_id)
        if ticket is None:
            raise KeyError(ticket_id)
        return ticket

    # -- contract surface ---------------------------------------------------

    def create_ticket(self, *, title: str, body: str = "", parent: str | None = None,
                      owner: dict | None = None, tags: list[str] | None = None,
                      families: list[str] | None = None, priority: str = "medium",
                      external_refs: dict | None = None) -> dict:
        ts = _now()
        ticket_id = self._next_id("TK")
        owner = owner or {"id": "unknown", "name": "unknown",
                          "family": "unknown", "kind": "agent"}
        ticket = {
            "id": ticket_id,
            "title": title,
            "body": body,
            "status": "draft",
            "priority": priority,
            "owner": owner,
            "tags": list(tags or []),
            "families": list(families or []),
            "external_refs": dict(external_refs or {}),
            "artifacts": [],
            "created_at": ts,
            "updated_at": ts,
        }
        self.tickets[ticket_id] = ticket
        self.append_event(ticket_id, kind="created", actor=owner,
                          payload={"title": title})
        if parent is not None:
            self.add_edge(parent, ticket_id, "parent_of", {})
        return dict(ticket)

    def get(self, ticket_id: str) -> dict | None:
        ticket = self.tickets.get(ticket_id)
        if ticket is None or ticket.get("deleted_at"):
            return None
        return dict(ticket)

    def list(self, *, status: str | None = None, family: str | None = None,
             tag: str | None = None, q: str | None = None) -> list[dict]:
        out = []
        for t in self.tickets.values():
            if t.get("deleted_at"):
                continue
            if status and t["status"] != status:
                continue
            if family and family not in t["families"]:
                continue
            if tag and tag not in t["tags"]:
                continue
            if q and q.lower() not in (t["title"] + " " + t["body"]).lower():
                continue
            out.append(dict(t))
        return out

    def patch(self, ticket_id: str, *, actor: dict, **fields) -> dict:
        ticket = self._require(ticket_id)
        changed = {}
        for key, value in fields.items():
            if key not in ("status", "priority", "title", "body"):
                raise ValueError(f"Cannot patch field: {key}")
            if key == "status" and value not in _STATUSES:
                raise ValueError(f"Unknown status: {value}")
            if ticket[key] != value:
                changed[key] = {"old": ticket[key], "new": value}
                ticket[key] = value
        ticket["updated_at"] = _now()
        if "status" in changed:
            self.append_event(ticket_id, kind="status_changed", actor=actor,
                              payload={"from": changed["status"]["old"],
                                       "to": changed["status"]["new"]})
        else:
            self.append_event(ticket_id, kind="updated", actor=actor,
                              payload={"fields": sorted(changed)})
        return dict(ticket)

    def delete_ticket(self, ticket_id: str, *, actor: dict,
                      via: str | None = None) -> dict:
        """S4: tombstone-delete (mirrors Store.delete_ticket)."""
        self._require(ticket_id)
        ts = _now()
        ticket = self.tickets[ticket_id]
        ticket["deleted_at"] = ts
        self.edges = [e for e in self.edges
                      if e.get("src") != ticket_id and e.get("dst") != ticket_id]
        self.bindings = [b for b in self.bindings
                         if b.get("ticket_id") != ticket_id]
        self.append_event(ticket_id, kind="ticket_deleted", actor=actor,
                          payload={"deleted_at": ts})
        return {"id": ticket_id, "deleted": True, "deleted_at": ts}

    def append_event(self, ticket_id: str, *, kind: str, actor: dict,
                     payload: dict | None = None) -> dict:
        self._require(ticket_id)
        event = {
            "id": self._next_id("EV"),
            "ticket_id": ticket_id,
            "ts": _now(),
            "actor": actor,
            "kind": kind,
            "payload": dict(payload or {}),
        }
        self.events.append(event)
        self.tickets[ticket_id]["updated_at"] = event["ts"]
        return dict(event)

    def add_edge(self, src: str, dst: str, edge_type: str,
                 meta: dict | None = None) -> dict:
        self._require(src)
        self._require(dst)
        if edge_type in ("parent_of", "blocks"):
            if src == dst or dst in self._reachable(src, edge_type):
                raise CycleError("Edge would create a cycle")
        edge = {
            "id": self._next_id("ED"),
            "src": src,
            "dst": dst,
            "type": edge_type,
            "meta": dict(meta or {}),
        }
        self.edges.append(edge)
        return dict(edge)

    def _reachable(self, start: str, edge_type: str) -> set[str]:
        """All nodes reachable from start following edges of edge_type."""
        seen: set[str] = set()
        frontier = [start]
        while frontier:
            node = frontier.pop()
            for e in self.edges:
                if e["src"] == node and e["type"] == edge_type and e["dst"] not in seen:
                    seen.add(e["dst"])
                    frontier.append(e["dst"])
        return seen

    def children(self, ticket_id: str) -> list[dict]:
        self._require(ticket_id)
        ids = [e["dst"] for e in self.edges
               if e["type"] == "parent_of" and e["src"] == ticket_id]
        return [dict(self.tickets[i]) for i in ids if i in self.tickets]

    def ancestry(self, ticket_id: str) -> list[dict]:
        """Parents chain, nearest parent first."""
        self._require(ticket_id)
        chain: list[dict] = []
        seen = {ticket_id}
        current = ticket_id
        while True:
            parent_id = next((e["src"] for e in self.edges
                              if e["type"] == "parent_of" and e["dst"] == current), None)
            if parent_id is None or parent_id in seen:
                break
            chain.append(dict(self.tickets[parent_id]))
            seen.add(parent_id)
            current = parent_id
        return chain

    def descendants(self, ticket_id: str) -> list[dict]:
        self._require(ticket_id)
        out = []
        for node_id in sorted(self._reachable(ticket_id, "parent_of")):
            out.append(dict(self.tickets[node_id]))
        return out

    def related(self, ticket_id: str) -> list[dict]:
        self._require(ticket_id)
        seen = {ticket_id}
        out = []
        for e in self.edges:
            if e["type"] == "parent_of":
                continue
            other = None
            if e["src"] == ticket_id:
                other = e["dst"]
            elif e["dst"] == ticket_id:
                other = e["src"]
            if other and other not in seen and other in self.tickets:
                seen.add(other)
                out.append(dict(self.tickets[other]))
        return out

    def timeline(self, ticket_id: str) -> list[dict]:
        self._require(ticket_id)
        return [dict(e) for e in self.events if e["ticket_id"] == ticket_id]

    def bind_thread(self, ticket_id: str, *, platform: str, room_id: str,
                    thread_id: str | None = None,
                    root_msg_id: str | None = None) -> dict:
        self._require(ticket_id)
        binding = {
            "ticket_id": ticket_id,
            "platform": platform,
            "room_id": room_id,
            "thread_id": thread_id,
            "root_msg_id": root_msg_id,
        }
        self.bindings.append(binding)
        return dict(binding)

    def list_bindings(self, ticket_id: str) -> list[dict]:
        self._require(ticket_id)
        return [dict(b) for b in self.bindings if b["ticket_id"] == ticket_id]

    def get_binding(self, *, platform: str, room_id: str,
                    thread_id: str | None = None) -> dict | None:
        for b in self.bindings:
            if b["platform"] != platform or b["room_id"] != room_id:
                continue
            if b.get("thread_id") and thread_id and b["thread_id"] != thread_id:
                continue
            return dict(b)
        return None

    def board(self) -> dict[str, list[dict]]:
        columns = {s: [] for s in _STATUSES}
        for t in self.tickets.values():
            columns[t["status"]].append(dict(t))
        return columns

    # -- wiki (DESIGN-v2 pillar 1) ------------------------------------------

    def put_page(self, slug: str, *, title: str, body_md: str = "",
                 tags: list[str] | None = None, actor=None) -> dict:
        if not slug or not slug.strip():
            raise ValueError("wiki slug must be non-empty")
        if not title or not str(title).strip():
            raise ValueError("wiki title must be non-empty")
        slug = slug.strip()
        rev = max((p["rev"] for p in self.pages if p["slug"] == slug),
                  default=0) + 1
        if isinstance(actor, dict):
            actor = actor.get("name") or actor.get("id") or ""
        page = {
            "id": self._next_id("WP"),
            "slug": slug,
            "rev": rev,
            "title": title,
            "body_md": body_md,
            "tags": list(tags or []),
            "actor": str(actor or ""),
            "ts": _now(),
        }
        self.pages.append(page)
        return dict(page)

    def get_page(self, slug: str) -> dict | None:
        latest = None
        for p in self.pages:
            if p["slug"] == slug and (latest is None or p["rev"] > latest["rev"]):
                latest = p
        return dict(latest) if latest else None

    def list_pages(self) -> list[dict]:
        latest: dict[str, dict] = {}
        for p in self.pages:
            cur = latest.get(p["slug"])
            if cur is None or p["rev"] > cur["rev"]:
                latest[p["slug"]] = p
        return [dict(p) for _, p in sorted(latest.items())]

    def page_history(self, slug: str) -> list[dict]:
        return [dict(p) for p in sorted(
            (p for p in self.pages if p["slug"] == slug), key=lambda p: p["rev"])]

    def search_pages(self, q: str) -> list[dict]:
        needle = (q or "").lower()
        out = []
        for p in self.list_pages():
            hay = (p["title"] + " " + p["body_md"] + " "
                   + " ".join(p["tags"])).lower()
            if needle in hay:
                out.append(p)
        return out
