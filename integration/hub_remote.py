"""A remote Tickets client for the A5 harness (lane hub-remote).

:class:`HubTicketsRemote` implements the five ``rlai_tickets`` ``Store``
methods the harness calls (``create_ticket``, ``get``, ``append_event``,
``events``, ``record``) against Ray's hub through the existing injected
:class:`Hub` transport. Every id that goes into a path is percent-encoded,
a non-2xx answer raises :class:`HubRemoteError` naming the method and the
status (never a body or a token), and the bearer is read only by the
transport at call time — this module never sees its value.
"""

from __future__ import annotations

import json
import urllib.parse
from dataclasses import dataclass, field


class HubRemoteError(Exception):
    """A hub call failed; the message names the method and status only."""


@dataclass(frozen=True)
class RemoteTicket:
    """The ticket attributes the harness reads."""

    ticket_id: str
    title: str = ""
    body: str = ""
    status: str = ""
    priority: str = "normal"
    tags: tuple[str, ...] = ()
    created_by: str = ""
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class RemoteEvent:
    """One event; ``actor``/``ts`` are the hub's names for actor/ts."""

    event_id: str
    ticket_id: str = ""
    seq: int = 0
    kind: str = ""
    payload: dict = field(default_factory=dict)
    actor: str = ""
    ts: str = ""
    duplicate: bool = False

    @property
    def actor_id(self) -> str:
        """The actor, under the local Store Event's attribute name."""
        return self.actor

    @property
    def created_at(self) -> str:
        """The event time, under the local Store Event's attribute name."""
        return self.ts


def _decode_ticket(data: dict) -> RemoteTicket:
    return RemoteTicket(
        # the real hub names it `id` (checked live, 2026-11-10)
        ticket_id=data.get("ticket_id") or data.get("id", ""),
        title=data.get("title", ""),
        body=data.get("body", ""),
        status=data.get("status", ""),
        priority=data.get("priority", "normal"),
        tags=tuple(data.get("tags", ())),
        created_by=data.get("created_by", ""),
        created_at=data.get("created_at", ""),
        updated_at=data.get("updated_at", ""))


def _decode_event(data: dict) -> RemoteEvent:
    return RemoteEvent(
        event_id=data.get("event_id") or data.get("id", ""),
        ticket_id=data.get("ticket_id", ""),
        seq=int(data.get("seq", 0)),
        kind=data.get("kind", ""),
        payload=dict(data.get("payload", {})),
        actor=data.get("actor", data.get("actor_id", "")),
        ts=data.get("ts", data.get("created_at", "")),
        duplicate=bool(data.get("duplicate", False)))


class HubTicketsRemote:
    """The remote stand-in for ``Store.open(--tickets-db)`` (rule 1)."""

    def __init__(self, hub) -> None:
        """Bind to the harness's existing ``Hub`` (base url, token file,
        injected get and post). No call happens here."""
        self._hub = hub

    # -- helpers -----------------------------------------------------------

    def _q(self, value: str) -> str:
        """Percent-encode one path segment (rule 3)."""
        return urllib.parse.quote(value, safe="")

    def _json(self, status: int, body: str):
        if not 200 <= status < 300:
            raise HubRemoteError(f"hub request failed with status {status}")
        try:
            return json.loads(body) if body else {}
        except ValueError:
            raise HubRemoteError(
                f"hub request failed with status {status}") from None

    def _fail(self, method: str, status: int) -> HubRemoteError:
        return HubRemoteError(f"{method} failed with status {status}")

    # -- the five Store methods --------------------------------------------

    def create_ticket(self, title: str, *, body: str = "",
                      tags: tuple[str, ...] = (), priority: str = "normal",
                      parent: str | None = None, actor: str) -> RemoteTicket:
        """POST /tickets with title, body, parent and tags."""
        status, text = self._hub.post("/tickets", {
            "title": title, "body": body, "tags": list(tags),
            "priority": priority, "parent": parent, "actor": actor})
        if not 200 <= status < 300:
            raise self._fail("create_ticket", status)
        return _decode_ticket(self._json(status, text))

    def get(self, ticket_id: str) -> RemoteTicket | None:
        """GET /tickets/{id}; a missing ticket comes back as None."""
        status, text = self._hub.get(f"/tickets/{self._q(ticket_id)}")
        if status == 404:
            return None
        if not 200 <= status < 300:
            raise self._fail("get", status)
        return _decode_ticket(self._json(status, text))

    def append_event(self, ticket_id: str, kind: str,
                     payload: dict | None = None, *,
                     actor: str) -> RemoteEvent:
        """POST /tickets/{id}/events with kind, payload and actor."""
        status, text = self._hub.post(
            f"/tickets/{self._q(ticket_id)}/events",
            {"kind": kind, "payload": dict(payload or {}), "actor": actor})
        if not 200 <= status < 300:
            raise self._fail("append_event", status)
        return _decode_event(self._json(status, text))

    def events(self, ticket_id: str) -> list[RemoteEvent]:
        """GET /tickets/{id}/events, ordered by seq."""
        status, text = self._hub.get(f"/tickets/{self._q(ticket_id)}/events")
        if not 200 <= status < 300:
            raise self._fail("events", status)
        doc = self._json(status, text) if text else []
        # the real hub returns a plain list; a {"events": [...]} body works too
        items = doc if isinstance(doc, list) else doc.get("events", [])
        return [_decode_event(e) for e in items]

    def record(self, c2_message: dict) -> RemoteEvent:
        """POST /records with the C2 message; a repeat gets duplicate=True."""
        status, text = self._hub.post("/records", dict(c2_message))
        if not 200 <= status < 300:
            raise self._fail("record", status)
        return _decode_event(self._json(status, text))
