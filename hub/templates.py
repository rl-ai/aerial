"""Ticket templates (DESIGN-v2 pillar 4): reusable workflow shapes.

A template is a JSON file describing a subtree: a root ticket plus plan
steps (children, optionally nested), each step declaring its charter role,
tags, and priority. Instantiating creates the whole tree in one call and
(optionally) assigns seats via the SeatRouter — assignment failures never
abort instantiation; they are reported per step.

Locations, later wins:
  1. templates/<name>.json            (repo, canonical)
  2. $TICKET_HUB_HOME/templates/      (personal overrides/additions)

Template schema:
  {
    "name": "feature",
    "description": "...",
    "tags": ["feature"], "priority": "medium",       # applied to root
    "steps": [
      {"title": "PMF check", "role": "pmf", "tags": ["pmf"],
       "steps": [...]}                                # nested one level
    ]
  }
Titles support "{title}" substitution from the instantiation title.
"""
from __future__ import annotations

import json
import os
from typing import Any, Optional

from ticket_hub.paths import hub_path

REPO_TEMPLATES = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "templates")
USER_TEMPLATES = hub_path("templates")


class TemplateError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _load_named(name: str) -> dict:
    if "/" in name or ".." in name or not name:
        raise TemplateError("bad_name", f"invalid template name: {name!r}")
    found = None
    for d in (REPO_TEMPLATES, USER_TEMPLATES):
        p = os.path.join(d, name + ".json")
        if os.path.exists(p):
            found = p
    if found is None:
        raise TemplateError("not_found", f"no template named {name!r}")
    with open(found) as f:
        tpl = json.load(f)
    tpl.setdefault("name", name)
    tpl.setdefault("steps", [])
    return tpl


def list_templates() -> list[dict]:
    out: dict[str, dict] = {}
    for d in (REPO_TEMPLATES, USER_TEMPLATES):
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if fn.endswith(".json"):
                try:
                    t = _load_named(fn[:-5])
                    t["source"] = "user" if d == USER_TEMPLATES else "repo"
                    out[t["name"]] = t
                except Exception:
                    continue
    return list(out.values())


def _create_step(store: Any, router: Any, parent_id: Optional[str],
                 spec: dict, title: str, actor: Optional[dict],
                 assign: bool) -> dict:
    t = store.create_ticket(title=title, body=spec.get("body", ""),
                            parent=parent_id,
                            owner=spec.get("owner") or actor,
                            tags=list(spec.get("tags") or []),
                            priority=spec.get("priority", "medium"))
    rec = {"id": t["id"], "title": title, "assigned": None,
           "assign_error": None}
    role = spec.get("role")
    if role and assign:
        try:
            res = router.assign(t["id"], role=role, actor=actor)
            rec["assigned"] = f"{res['seat']}@{role}"
        except Exception as e:  # seat down/at capacity: keep the ticket
            rec["assign_error"] = getattr(e, "code", str(e))
    kids = []
    for sub in spec.get("steps", []):
        kids.append(_create_step(store, router, t["id"], sub,
                                 sub["title"].replace("{title}", title),
                                 actor, assign))
    rec["children"] = kids
    return rec


def instantiate(store: Any, router: Any, name: str, title: str, *,
                parent: Optional[str] = None,
                actor: Optional[dict] = None,
                assign: bool = True) -> dict:
    """Create the template's whole subtree under a new root ticket.
    Returns {root, tree} where tree reports per-step seat assignment."""
    tpl = _load_named(name)
    root = store.create_ticket(title=title, body=tpl.get("body", ""),
                               parent=parent, owner=actor,
                               tags=list(tpl.get("tags") or []),
                               priority=tpl.get("priority", "medium"))
    tree = []
    for step in tpl["steps"]:
        tree.append(_create_step(store, router, root["id"], step,
                                 step["title"].replace("{title}", title),
                                 actor, assign))
    return {"root": root, "tree": tree}
