"""Translation layer: work events -> reader-facing digest (DESIGN.md §Translation).

Deterministic-first: template_pass maps event kinds to fixed sentence
templates. polish_pass optionally runs one LLM call over the template
digest; the template output is the fallback and the truth source.

Never fabricate:
  * every bullet cites the event ids it summarizes;
  * a digest is only ever produced from events that were passed in;
  * unknown kinds render a generic honest sentence, nothing invented.
"""

from __future__ import annotations

import json

__all__ = ["template_pass", "polish_pass"]

# Markers that turn any payload into the "known issue logged" sentence
# (DESIGN.md: QUEUED FINDING / KNOWN-FAILS payloads).
_KNOWN_ISSUE_MARKERS = ("QUEUED FINDING", "KNOWN-FAILS")


def _actor_name(actor):
    """Resolve an actor field (str | {name,...} | None) to a display name."""
    if actor is None:
        return "the system"
    if isinstance(actor, dict):
        return actor.get("name") or actor.get("id") or "the system"
    return str(actor)


def _payload_text(event):
    """Stringify an event payload for marker scanning and excerpts."""
    payload = event.get("payload") or {}
    if isinstance(payload, str):
        return payload
    try:
        return json.dumps(payload, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return str(payload)


def _payload_field(event, *names, default=None):
    """First present string field from the payload (dict payloads only)."""
    payload = event.get("payload") or {}
    if isinstance(payload, str):
        return payload if default is None else payload
    for name in names:
        value = payload.get(name)
        if value not in (None, ""):
            return value
    return default


def _excerpt(text, limit=140):
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _known_issue(event):
    return any(m in _payload_text(event).upper() for m in _KNOWN_ISSUE_MARKERS)


def _sentence_for(event):
    """Map one event to a reader-facing sentence. Deterministic, total.

    Returns None only for events without an id (skipped by template_pass).
    """
    kind = event.get("kind")

    # Known-issue payloads override their raw kind (DESIGN.md §Translation 2).
    if _known_issue(event):
        return "Known issue logged."

    if kind == "status_changed":
        to = _payload_field(event, "to", "new_status", "status")
        frm = _payload_field(event, "from", "old_status")
        if not to:
            return "Status updated."
        to = str(to)
        if to == "Done":
            return "Completed — moved to Done."
        if to == "Blocked":
            if frm:
                return f"Blocked (was {frm})."
            return "Blocked."
        if to == "Cancelled":
            return "Cancelled."
        return f"Advanced to {to}."

    if kind == "comment":
        body = _payload_field(event, "body", "text", "content", default="")
        who = _actor_name(event.get("actor"))
        if body:
            return f"{who} commented: {_excerpt(body)}"
        return f"{who} left a comment."

    if kind == "checkpoint":
        name = _payload_field(event, "name", "title", "label", "text")
        if name:
            return f"Checkpoint reached: {_excerpt(name)}."
        return "Checkpoint recorded."

    if kind == "decision":
        text = _payload_field(event, "decision", "text", "title", "body")
        if text:
            return f"Decision recorded: {_excerpt(text)}."
        return "Decision recorded."

    if kind == "approval_receipt":
        approver = _payload_field(
            event, "approver", "approved_by", "by",
            default=_actor_name(event.get("actor")),
        )
        return f"Approved by {approver}."

    if kind == "artifact":
        name = _payload_field(event, "name", "title", "path", "file", "url")
        if name:
            return f"Produced {_excerpt(name)}."
        return "Artifact produced."

    if kind == "thread_message":
        who = _payload_field(event, "author", "chat_user",
                             default=_actor_name(event.get("actor")))
        return f"Chat reply from {who} in the bound thread."

    if kind == "handoff":
        to = _payload_field(event, "to", "assignee", "owner")
        if to:
            return f"Handed off to {_actor_name(to)}."
        return "Handed off."

    if kind == "created":
        title = _payload_field(event, "title", default=None)
        if title:
            return f"Created: {_excerpt(title)}."
        return "Ticket created."

    if kind == "updated":
        fields = _payload_field(event, "fields", "changed", default=None)
        if isinstance(fields, (list, tuple)) and fields:
            return "Ticket updated (" + ", ".join(str(f) for f in fields) + ")."
        return "Ticket details updated."

    if kind == "link":
        dst = _payload_field(event, "dst", "target", "url")
        if dst:
            return f"Linked to {_excerpt(dst)}."
        return "Related item linked."

    # Unknown kind: honest generic sentence, never a fabricated story.
    label = str(kind) if kind else "unknown"
    return f"Activity recorded ({label})."


def _iter_events(events, timeline):
    """Accept either `events` or `timeline` (DESIGN.md timeline = events +
    linked thread messages), exactly one of them."""
    if (events is None) == (timeline is None):
        raise TypeError("template_pass requires exactly one of events or timeline")
    seq = events if events is not None else timeline
    return list(seq)


def template_pass(events=None, *, timeline=None, project=None):
    """Template pass over an event list (or a timeline projection).

    Returns a digest dict:
        {project, updated_at, bullets: [{text, event_ids}]}

    Properties:
      * deterministic: same input -> byte-identical output;
      * every input event with an id is cited by exactly one bullet;
      * every bullet cites only event ids present in the input.
    """
    seq = _iter_events(events, timeline)

    bullets = []
    timestamps = []
    for event in seq:
        if not isinstance(event, dict):
            raise TypeError(f"events must be dicts, got {type(event).__name__}")
        event_id = event.get("id")
        if event_id is None:
            # No id -> cannot cite it -> cannot include it (no-fabrication).
            continue
        ts = event.get("ts")
        if ts is not None:
            timestamps.append(ts)
        bullets.append({"text": _sentence_for(event), "event_ids": [event_id]})

    updated_at = max(timestamps) if timestamps else None
    if project is None:
        ticket_ids = [e.get("ticket_id") for e in seq if e.get("ticket_id")]
        project = ticket_ids[0] if ticket_ids else None

    return {"project": project, "updated_at": updated_at, "bullets": bullets}


def polish_pass(digest, llm_call=None):
    """Optional polish over a template digest.

    * llm_call is None -> template output returned unchanged (truth source).
    * llm_call is provided -> called once with the digest. May return:
        - str: single polished sentence;
        - list of {text, event_ids} bullets;
        - dict with "bullets".
      event_ids are always preserved from the template digest.
    * Any llm_call failure falls back to the unchanged template digest.
    """
    if llm_call is None:
        return digest

    try:
        result = llm_call(digest)
        template_ids = []
        for bullet in digest.get("bullets", []):
            template_ids.extend(bullet.get("event_ids", []))

        if isinstance(result, str):
            new_bullets = [{"text": result, "event_ids": list(template_ids)}]
        elif isinstance(result, dict):
            new_bullets = result.get("bullets")
        elif isinstance(result, list):
            new_bullets = result
        else:
            return digest

        if not isinstance(new_bullets, list) or not new_bullets:
            return digest

        polished = []
        allowed = set(template_ids)
        for bullet in new_bullets:
            if not isinstance(bullet, dict) or "text" not in bullet:
                return digest
            cited = list(bullet.get("event_ids") or template_ids)
            # Polished bullets may only cite ids the template cited.
            if not all(i in allowed for i in cited):
                return digest
            polished.append({"text": str(bullet["text"]), "event_ids": cited})

        out = dict(digest)
        out["bullets"] = polished
        out["polished"] = True
        return out
    except Exception:
        # Template output is the fallback and the truth source.
        return digest
