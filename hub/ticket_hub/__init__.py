"""ticket_hub — centralized object-oriented ticket graph (core lane).

Public surface (contract: DESIGN.md):
  - model: Actor, Ticket, Edge, Event, ThreadBinding, Status,
    ALLOWED_TRANSITIONS, EDGE_TYPES, EVENT_KINDS, PLATFORMS, new_id, new_ulid,
    SYSTEM_ACTOR, coerce_status, assert_transition
  - store: Store (SQLite WAL, append-only events, single writer)
  - lineage: lineage(store, ticket_id), lineage_dict, descendant_tree
"""
from .model import (
    ACTOR_KINDS,
    ALLOWED_TRANSITIONS,
    EDGE_TYPES,
    EVENT_KINDS,
    GRAPH_EDGE_TYPES,
    PLATFORMS,
    SYSTEM_ACTOR,
    Actor,
    Edge,
    Event,
    Status,
    ThreadBinding,
    Ticket,
    WikiPage,
    assert_transition,
    coerce_status,
    new_id,
    new_ulid,
)
from .store import Store
from .lineage import descendant_tree, lineage, lineage_dict
from . import translate

__all__ = [
    "ACTOR_KINDS",
    "ALLOWED_TRANSITIONS",
    "EDGE_TYPES",
    "EVENT_KINDS",
    "GRAPH_EDGE_TYPES",
    "PLATFORMS",
    "SYSTEM_ACTOR",
    "Actor",
    "Edge",
    "Event",
    "Status",
    "ThreadBinding",
    "Ticket",
    "WikiPage",
    "assert_transition",
    "coerce_status",
    "new_id",
    "new_ulid",
    "Store",
    "descendant_tree",
    "lineage",
    "lineage_dict",
    "translate",
]
