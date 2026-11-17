"""ticket_hub lineage projection.

``lineage(store, ticket_id)`` returns the read-model defined in DESIGN.md:
ancestry chain + descendant tree + full ordered event chain for a ticket.
"""
from __future__ import annotations

from .store import Store


def descendant_tree(store: Store, ticket_id: str) -> dict:
    """Recursive tree node: ``{"ticket": <Ticket>, "children": [...]}``.
    Guards against pre-existing corruption with a visited set."""
    return _tree_node(store, ticket_id, set())


def _tree_node(store: Store, ticket_id: str, visited: "set[str]") -> dict:
    ticket = store.require_ticket(ticket_id)
    if ticket_id in visited:
        raise ValueError(f"corrupt hierarchy: cycle detected at {ticket_id}")
    visited = visited | {ticket_id}
    return {
        "ticket": ticket,
        "children": [_tree_node(store, c.id, visited) for c in store.children(ticket_id)],
    }


def lineage(store: Store, ticket_id: str) -> dict:
    """Lineage read-model for *ticket_id*:

    ``{
        "ticket": Ticket,
        "ancestry": [Ticket, ...],     # root-first (breadcrumb order)
        "descendants": {ticket, children: [...]},  # tree, depth-ordered
        "events": [Event, ...],        # full chain, ordered by (ts, seq)
    }``
    """
    ticket = store.require_ticket(ticket_id)
    return {
        "ticket": ticket,
        "ancestry": store.ancestry(ticket_id),
        "descendants": descendant_tree(store, ticket_id),
        "events": store.events(ticket_id),
    }


def lineage_dict(store: Store, ticket_id: str) -> dict:
    """JSON-serializable form of :func:`lineage` (uses ``to_dict``)."""
    lin = lineage(store, ticket_id)
    return {
        "ticket": lin["ticket"].to_dict(),
        "ancestry": [t.to_dict() for t in lin["ancestry"]],
        "descendants": _tree_to_dict(lin["descendants"]),
        "events": [e.to_dict() for e in lin["events"]],
    }


def _tree_to_dict(node: dict) -> dict:
    return {
        "ticket": node["ticket"].to_dict(),
        "children": [_tree_to_dict(c) for c in node["children"]],
    }
