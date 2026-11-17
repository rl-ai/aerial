"""Seat/role assignment for tickets (DESIGN-v2: models assigned around tickets).

Prior art folded in:
- models.json roles/stages: route by SEAT NAME through the cluster SSOT;
  never hardcode endpoints. Seat health comes from remote-model-chat --list
  (liveness), cached.
- Forge gates: reviewer must not be the builder (builder-exclusion), and
  accept authority is separate from build authority.

Protocol (append-only, event-sourced — no new tables):
- assignment = ``handoff`` event with payload.action == "assigned"
  {role, seat}; release = action == "released". The latest such event per
  ticket is the current holder.
- contributors = seat names seen as actors on any event of the ticket
  (used by builder-exclusion for review/accept roles).
- load = tickets a seat currently holds with status open/running/blocked.
"""
from __future__ import annotations

import json
import os
import subprocess
from typing import Any, Callable, Optional

from ticket_hub.paths import hub_path

DEFAULT_ROLES_PATH = hub_path("roles.json")
CHARTER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "roles", "registry.json")
# Authority ladder: none < build < review < accept. review/accept triggers
# builder-exclusion (a seat that has history on a ticket cannot review it).
AUTHORITY_BY_ROLE = {
    "design": "review",
    "pmf": "review",
    "code-design": "review",
    "execution": "build",
    "measurement": "none",
    "product-owner": "accept",
    "orchestrator": "none",
    "service-manager": "build",
    "biz-dev": "build",
}
# Seat bindings for charter roles (cluster seat names — resolve through the
# SSOT at call time). A charter may declare its own "seats" list to override.
SEAT_BINDINGS = {
    "design": ["critic", "glm"],
    "pmf": ["glm", "critic"],
    "code-design": ["glm", "critic"],
    "execution": ["utility_worker", "ariel_orchestrator", "eva_operator"],
    "measurement": ["utility_worker"],
    "product-owner": ["glm", "critic"],
    "orchestrator": ["glm", "critic"],
    "service-manager": ["eva_operator"],
    "biz-dev": ["glm", "critic"],
}
HEALTH_CACHE_TTL = 30.0
BUSY_STATUSES = ("open", "running", "blocked")
ACTIVE_STATUSES = None  # anything except done/cancelled


class SeatError(ValueError):
    """Assignment refused (registry/health/capacity/exclusion). Carries a
    machine code mirroring the hub error envelope."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def load_registry(path: Optional[str] = None) -> dict:
    """Load the role registry. Accepts the charter shape
    (roles/registry.json: {"version": 1, "roles": [charters...]}) — the
    canonical source — or the simple dict shape used by tests. Charter
    entries gain seat bindings from their ``seats`` field and authority
    from AUTHORITY_BY_ROLE."""
    for p in (path, os.environ.get("TICKET_HUB_ROLES"),
              DEFAULT_ROLES_PATH, CHARTER_PATH):
        if p and os.path.exists(p):
            with open(p) as f:
                reg = json.load(f)
            raw = reg.get("roles", {})
            if isinstance(raw, list):  # charter shape
                roles = {}
                for c in raw:
                    rid = c["id"]
                    roles[rid] = {
                        "mission": c.get("mission", ""),
                        "seats": c.get("seats") or SEAT_BINDINGS.get(rid, []),
                        "authority": c.get(
                            "authority",
                            AUTHORITY_BY_ROLE.get(rid, "none")),
                        "max_concurrent": c.get("max_parallel", 1),
                    }
                reg["roles"] = roles
            reg.setdefault("rules", {})
            reg["rules"].setdefault("builder_exclusion", True)
            return reg
    raise SeatError("no_registry", "no roles registry found")


def default_health_probe() -> dict[str, str]:
    """seat -> up|down via the cluster SSOT CLI. Missing/failing CLI =>
    {} which callers treat as health-unknown (optimistic)."""
    cmd = os.environ.get(
        "TICKET_HUB_SEATS_CMD", "~/.hermes/bin/remote-model-chat --list")
    try:
        out = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                             timeout=20).stdout
    except Exception:
        return {}
    seats: dict[str, str] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].upper() in ("UP", "DOWN", "BLOCKED"):
            seats[parts[1]] = "up" if parts[0].upper() == "UP" else "down"
    return seats


class SeatRouter:
    def __init__(self, store: Any, *, registry: Optional[dict] = None,
                 health: Optional[Callable[[], dict[str, str]]] = None):
        self._store = store
        self._registry = registry if registry is not None else load_registry()
        self._health_fn = health or default_health_probe
        self._health_cache: tuple[float, dict[str, str]] = (0.0, {})

    # -- registry / health ---------------------------------------------------

    @property
    def registry(self) -> dict:
        return self._registry

    def health(self) -> dict[str, str]:
        import time
        ts, cached = self._health_cache
        if time.monotonic() - ts > HEALTH_CACHE_TTL:
            cached = self._health_fn()
            self._health_cache = (time.monotonic(), cached)
        return cached

    def seat_up(self, seat: str) -> bool:
        h = self.health()
        return h.get(seat, "up") == "up"  # unknown => optimistic

    # -- projections -----------------------------------------------------------

    def current_holder(self, ticket_id: str) -> Optional[dict]:
        for ev in reversed(self._store.timeline(ticket_id)):
            p = ev.get("payload") or {}
            if p.get("role") and p.get("action") == "assigned":
                return {"seat": p.get("seat"), "role": p.get("role"),
                        "event_id": ev.get("id"), "ts": ev.get("ts")}
            if p.get("action") == "released":
                return None
        return None

    def contributors(self, ticket_id: str) -> set[str]:
        out = set()
        for ev in self._store.timeline(ticket_id):
            name = (ev.get("actor") or {}).get("name")
            if name:
                out.add(name)
        return out

    def load_map(self) -> dict[str, int]:
        loads: dict[str, int] = {}
        for t in self._store.list():
            if t.get("status") in ("done", "cancelled"):
                continue
            holder = self.current_holder(t["id"])
            if holder and holder.get("seat"):
                loads[holder["seat"]] = loads.get(holder["seat"], 0) + 1
        return loads

    # -- assignment --------------------------------------------------------------

    def assign(self, ticket_id: str, *, role: str, seat: Optional[str] = None,
               actor: Optional[dict] = None) -> dict:
        roles = self._registry["roles"]
        if role not in roles:
            raise SeatError("unknown_role",
                            f"unknown role: {role} "
                            f"(have: {', '.join(sorted(roles))})")
        spec = roles[role]
        ticket = self._store.get(ticket_id)
        if ticket is None:
            raise SeatError("not_found", f"no ticket {ticket_id}")

        candidates = list(spec.get("seats", []))
        if seat:
            if seat not in candidates:
                raise SeatError(
                    "seat_not_in_role",
                    f"seat {seat!r} is not licensed for role {role!r} "
                    f"(licensed: {', '.join(candidates)})")
            candidates = [seat]
        if self._registry.get("rules", {}).get("builder_exclusion", True) \
                and spec.get("authority") in ("review", "accept"):
            built = self.contributors(ticket_id)
            eligible = [s for s in candidates if s not in built]
            if not eligible:
                raise SeatError(
                    "builder_exclusion",
                    f"all seats licensed for {role!r} have history on this "
                    f"ticket ({', '.join(sorted(candidates))}); pick another "
                    f"ticket or role")
            candidates = eligible
        loads = self.load_map()
        cap = spec.get("max_concurrent", 99)
        healthy = [s for s in candidates if self.seat_up(s)]
        free = [s for s in healthy if loads.get(s, 0) < cap]
        if not healthy:
            raise SeatError("no_healthy_seat",
                            f"no healthy seat for role {role!r}: "
                            f"{', '.join(candidates)}")
        if not free:
            raise SeatError(
                "at_capacity",
                f"all healthy seats for {role!r} are at max_concurrent={cap}: "
                + ", ".join(f"{s}={loads.get(s, 0)}" for s in healthy))
        chosen = min(free, key=lambda s: (loads.get(s, 0), candidates.index(s)))
        ev = self._store.append_event(
            ticket_id, kind="handoff", actor=actor,
            payload={"action": "assigned", "role": role, "seat": chosen,
                     "authority": spec.get("authority", "none")})
        return {"ticket_id": ticket_id, "seat": chosen, "role": role,
                "authority": spec.get("authority", "none"),
                "event_id": ev["id"] if isinstance(ev, dict) else ev.id}

    def release(self, ticket_id: str, *, actor: Optional[dict] = None) -> dict:
        holder = self.current_holder(ticket_id) or {}
        ev = self._store.append_event(
            ticket_id, kind="handoff", actor=actor,
            payload={"action": "released", "seat": holder.get("seat"),
                     "role": holder.get("role")})
        return {"ticket_id": ticket_id, "released": holder or None,
                "event_id": ev["id"] if isinstance(ev, dict) else ev.id}

    # -- rollup for UI/CLI -------------------------------------------------------

    def roster(self) -> dict:
        loads = self.load_map()
        seats_out: dict[str, dict] = {}
        for role, spec in self._registry["roles"].items():
            for s in spec.get("seats", []):
                entry = seats_out.setdefault(s, {"roles": [], "load": loads.get(s, 0),
                                                 "health": self.health().get(s, "unknown")})
                entry["roles"].append(role)
        return {"roles": self._registry["roles"], "seats": seats_out,
                "assignments": self.assignments_open()}

    def assignments_open(self) -> list[dict]:
        out = []
        for t in self._store.list():
            if t.get("status") in ("done", "cancelled"):
                continue
            holder = self.current_holder(t["id"])
            if holder:
                out.append({"ticket_id": t["id"], "title": t["title"],
                            "status": t["status"], **holder})
        return out
