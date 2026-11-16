"""The reviewed digest (landscape C12): the Curator's flags, gatekeeper
uncertainty and hand-backs arrive as one digest you open on your cadence —
never as per-event chatter. This is the interruption discipline the
gateway demands of third parties, applied to itself."""

from __future__ import annotations

import time


def build_digest(book, gatekeeper, *, since_ts: str | None = None) -> dict:
    """Collect everything that waits for a person, grouped and counted.

    ``since_ts`` filters ledger entries: only entries at or after the stamp
    are surfaced, so a cadence view never re-shows already-reviewed history."""
    grants = book.conn.execute(
        "SELECT id, counterparty, status FROM grants ORDER BY rowid"
    ).fetchall()
    sections: dict[str, list] = {"refusals": [], "hand_backs": [],
                                 "shadow": [], "flags": [],
                                 "admission": []}
    for g in grants:
        for entry in book.entries(g["id"]):
            if since_ts and (entry["ts"] or "") < since_ts:
                continue
            state = entry["state"]
            line = {"grant": g["id"], "question": entry["question"][:80],
                    "reason": entry["reason"] or ""}
            if state == "refused":
                sections["refusals"].append(line)
            elif state == "handed_back":
                sections["hand_backs"].append(line)
            elif state in ("shadow_pass", "shadow_refused"):
                sections["shadow"].append(dict(
                    line, would=("answer" if state == "shadow_pass"
                                 else "refuse")))
            elif state in ("admission_refused", "followup"):
                # admission-level events (bots, bad keys, exhaustion) and
                # follow-up deliveries: reviewable, never budget-relevant
                sections["admission"].append(dict(line, kind=state))
    # G7: flags now come from the persisted JSONL log when the gatekeeper
    # exposes one — a digest built in a fresh process still shows them.
    persisted = getattr(gatekeeper, "persisted_flags", None)
    if callable(persisted):
        all_flags = persisted()
    else:
        all_flags = list(gatekeeper.flags)
    sections["flags"] = [
        f for f in all_flags
        if not since_ts or (f.get("ts") or "") >= since_ts]
    counts = {k: len(v) for k, v in sections.items()}
    return {"built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "counts": counts,
            "waits_for_you": any(counts.values()),
            "sections": sections}


def render(digest: dict) -> str:
    """Markdown for the digest surface: 'what waits for you' first; healthy
    state takes zero space (a clean digest renders as one line)."""
    if not digest["waits_for_you"]:
        return "Nothing waits for you."
    lines = [f"# Digest {digest['built_at']}", ""]
    titles = {"hand_backs": "Hand-backs", "refusals": "Refusals",
              "shadow": "Shadow log (would-answer / would-refuse)",
              "admission": "Admission events (refused/follow-up)",
              "flags": "Uncertainty flags"}
    for key in ("hand_backs", "refusals", "shadow", "admission", "flags"):
        rows = digest["sections"].get(key, [])
        if not rows:
            continue
        lines.append(f"## {titles[key]} ({len(rows)})")
        for r in rows:
            who = r.get("grant", "")
            q = r.get("question") or r.get("why", "")
            why = r.get("reason") or r.get("would") or r.get("why", "")
            lines.append(f"- [{who}] {q} — {why}")
        lines.append("")
    return "\n".join(lines)
