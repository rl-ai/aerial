#!/usr/bin/env python3
"""The Now list — report-design-v2.md §6 (task t_75327824).

The one thing Ray reads when he opens the console: the top ≤3 things he could
do right now, at different effort levels, with a crossed-off mechanic and a
tally. Not a filtered queue — a hard-capped list.

Sourcing (§6.1) — union of three sources, ranked, capped at 3:
  rank 1  decisions needed from the exec summary
  rank 2  Zone B decision-queue items with admission score ≥ ADMISSION_GATE
          (Ray-gated gates arrive here — their score is pinned high because
          they are Ray-gated, not a fourth source)
  rank 3  pending onboarding / setup steps (declared checklists only)

NEVER on the list (§6.1 never-list): queue items below the admission gate,
anything an agent is already handling (Zone A working, followup fix/verify
states), risks/watch entries, raw metrics, recommendations without a
decision. The exclusion is enforced in build_now_list(), not by hoping the
feeds are clean.

Effort ladder (§6.2): static per-type default table, overridable by a
declared record field; `deep` is never auto-assigned at render time.

Focus mode (§6.3): exactly 1 item when the top item is `deep`, any item is
in-progress, or the manual toggle is on; hidden items stay visible as
"+N more · <effort mix>".

States (§6.4): pending → in-progress → done, plus done-by-agent. State lives
in the underlying record; this module only reads it. The tally is computed
from record timestamps, never from UI state.
"""

from __future__ import annotations

import html
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

# --- constants ---------------------------------------------------------------

NOW_CAP = 3                 # hard cap on pending items, ever (§6.1)
FOCUS_CAP = 1               # focus mode shows exactly one item (§6.3)
AGE_DECAY_H = 72.0          # >72h pending with no change → one rank down (§6.4)
DONE_GROUP_MAX = 5          # completed items shown in the done-today group

#: The admission threshold constant — the same gate that blocks autonomous
#: action (the highest class admission_threshold in queue-policy.yaml,
#: `blocking: 0.5`). Queue items below it never earn a Now slot (§6.1).
ADMISSION_GATE = 0.5

#: Static per-type effort defaults (§6.2) — dumb but predictable. The record
#: override (`effort` field) carries the truth; render-time guessing is
#: forbidden and `deep` has NO default (only declared).
EFFORT_DEFAULTS = {
    "decision": "quick-win",    # decisions with discrete options
    "queue-gate": "medium",     # gate checks / fix reviews
    "onboarding": "medium",     # onboarding tests ("clean-venv test")
}
EFFORT_ORDER = {"quick-win": 0, "medium": 1, "deep": 2}
EFFORT_BADGE = {"quick-win": "⚡", "medium": "⏱", "deep": "🧠"}
EFFORT_HINT = {"quick-win": "under 5 minutes", "medium": "about 30 minutes",
               "deep": "focus block (> 30 min)"}

#: States meaning "an agent is already on it" — never a Now item (§6.1).
#: (`in-progress` is deliberately NOT here: an item Ray took in-progress is
#: the list itself per §6.3.2.)
AGENT_HANDLED_STATES = frozenset({
    "working", "fix-attempted", "verifying", "in-fix",
    "mandate-created", "auto-fix"})

#: Zero-state, verbatim (§6.1) — never an empty region.
ZERO_STATE = "Nothing needs you right now — the agents have it."

#: Ranks per source (§6.1). Lower rank = higher priority.
RANK_DECISION = 1
RANK_QUEUE = 2
RANK_ONBOARDING = 3

_NOW_ITEM_KINDS = ("decision", "queue-gate", "onboarding")


# --- item ---------------------------------------------------------------------

@dataclass
class NowItem:
    """One candidate on the Now list. `state`/`resolved_at`/`actor` are read
    from the underlying record (decision record, queue entry, onboarding
    step) — never UI state (§6.4)."""
    id: str
    title: str
    kind: str                      # decision | queue-gate | onboarding
    rank: int                      # 1=decision, 2=queue, 3=onboarding
    state: str = "pending"         # pending|in-progress|done|done-by-agent
    admission_score: float = 0.0
    created_at: Optional[float] = None
    resolved_at: Optional[float] = None
    actor: str = ""                # "you" | "agent" | ...
    effort_override: Optional[str] = None   # declared value beats the table
    ray_gated: bool = False
    options: Sequence[Dict[str, Any]] = ()  # A/B/C options where applicable
    evidence: Sequence[Dict[str, Any]] = ()
    detail: Dict[str, Any] = field(default_factory=dict)

    @property
    def effort(self) -> str:
        if self.effort_override in EFFORT_ORDER:
            return self.effort_override  # type: ignore[return-value]
        return EFFORT_DEFAULTS.get(self.kind, "medium")

    @property
    def age_s(self) -> float:
        if self.created_at is None:
            return 0.0
        return max(0.0, time.time() - float(self.created_at)) if self.created_at <= time.time() \
            else 0.0

    @property
    def age_text(self) -> str:
        h = self.age_s / 3600.0
        if h >= 48:
            return f"open {h / 24:.0f}d"
        if h >= 1:
            return f"open {h:.0f}h"
        if self.created_at is None:
            return "unavailable"
        return f"open {max(1, int(h * 60))}m"

    @property
    def done_text(self) -> str:
        if self.resolved_at is None:
            return "unavailable"
        hhmm = time.strftime("%H:%M", time.gmtime(float(self.resolved_at)))
        actor = "by the agent" if self.state == "done-by-agent" or self.actor == "agent" \
            else "by you"
        return f"✓ done {hhmm} · {actor}"


# --- adapters (source → NowItem) ------------------------------------------------

_DECISION_NUM_RE = re.compile(r"^\s*(?:\d+[.)]|[-•*])\s+")


def item_from_decision(line: str, idx: int,
                       evidence: Optional[Sequence[Dict[str, Any]]] = None,
                       options: Optional[Sequence[Dict[str, Any]]] = None) -> NowItem:
    """An exec-summary 'Decisions needed' line → rank-1 Now item (§6.1.1)."""
    title = _DECISION_NUM_RE.sub("", str(line or "").strip())
    title = re.sub(r"\s+", " ", title)
    return NowItem(
        id=f"decision:{idx}", title=title or "unavailable", kind="decision",
        rank=RANK_DECISION, admission_score=1.0,
        options=tuple(options or ()),
        evidence=tuple(evidence or ()),
        detail={"source": "exec summary — decisions needed"})


def item_from_queue(item: Any, now: float) -> Optional[NowItem]:
    """An admitted Zone B queue item → rank-2 Now item (§6.1.2).

    Ray-gated gates arrive through this source: a record that declares
    `ray_gated` (or a claim tagged 'ray-gated') is admitted at a pinned-high
    score. Returns None for anything that must never appear (below-gate,
    agent-handled states) — the never-list is enforced here AND in
    build_now_list.
    """
    claim = str(getattr(item, "claim", "") or "")
    score = float(getattr(item, "admission_score", 0.0) or 0.0)
    state = str(getattr(item, "state", "") or "waiting-on-you").lower()
    ray_gated = bool(getattr(item, "ray_gated", False)) or "ray-gated" in claim.lower()
    effective = 1.0 if ray_gated else score
    if effective < ADMISSION_GATE:
        return None                      # below-gate noise stays in Zone B
    if state in AGENT_HANDLED_STATES:
        return None                      # an agent is already handling it
    resolved = _resolved_state(state)
    if resolved is None:
        return None
    return NowItem(
        id=str(getattr(item, "item_id", "") or "unavailable"),
        title=claim or "unavailable", kind="queue-gate", rank=RANK_QUEUE,
        state=resolved, admission_score=effective,
        created_at=getattr(item, "created_at", None),
        resolved_at=getattr(item, "resolved_at", None),
        actor=str(getattr(item, "actor", "") or ""),
        ray_gated=ray_gated,
        options=(
            {"label": "Approve", "consequence": "the agent's proposed action proceeds"},
            {"label": "Reject", "consequence": "the proposed action is dropped"},
            {"label": "Escalate", "consequence": "a person takes it manually"},
        ),
        evidence=[{"label": str(getattr(item, "evidence_label", "") or "unavailable")}],
        detail={
            "proposed_action": str(getattr(item, "proposed_action", "") or "unavailable"),
            "source_agent": str(getattr(item, "source_agent", "") or "unavailable"),
            "impact_class": str(getattr(item, "impact_class", "") or "unavailable"),
            "steps": list(getattr(item, "steps", ()) or ()),
        })


def item_from_onboarding(step: Dict[str, Any]) -> NowItem:
    """A declared onboarding/setup step → rank-3 Now item (§6.1.3).

    Declared checklists only — the step record must declare its own effort
    (e.g. the clean-venv test's '30 min' is a declared fact that ships with
    the item)."""
    rec = dict(step or {})
    return NowItem(
        id=str(rec.get("id") or rec.get("step_id") or "unavailable"),
        title=str(rec.get("title") or rec.get("step") or "unavailable"),
        kind="onboarding", rank=RANK_ONBOARDING,
        state=_resolved_state(str(rec.get("state") or "pending").lower()) or "pending",
        created_at=rec.get("created_at"),
        resolved_at=rec.get("resolved_at"),
        actor=str(rec.get("actor") or ""),
        effort_override=rec.get("effort"),
        evidence=tuple(rec.get("evidence") or ()),
        detail={"command": rec.get("command"), "click_path": rec.get("click_path"),
                "source": "declared onboarding checklist"})


def _resolved_state(state: str) -> Optional[str]:
    """Map an underlying-record state onto a Now-list state; None = excluded."""
    if state in ("done", "complete", "completed", "succeeded", "success", "ok",
                 "approved", "rejected", "resolved"):
        actor = "agent" if state in ("approved", "rejected", "resolved") else ""
        return "done-by-agent" if actor == "agent" and state != "done" else "done"
    if state in ("done-by-agent", "done_by_agent"):
        return "done-by-agent"
    if state in ("in-progress", "in_progress", "started"):
        return "in-progress"
    if state in ("pending", "waiting-on-you", "waiting_on_you", "open",
                 "new", "", "waiting"):
        return "pending"
    return None      # anything else (dismissed, expired, working, ...) never shows


# --- sourcing + ranking -----------------------------------------------------------

def _age_decayed_rank(item: NowItem, now: float) -> int:
    """§6.4: an item pending >72h with no state change is aged one rank down."""
    rank = item.rank
    if item.state == "pending" and item.created_at is not None:
        if now - float(item.created_at) > AGE_DECAY_H * 3600.0:
            rank += 1
    return rank


def _admission_rank_key(item: NowItem, now: float):
    """§6.1 rank order; secondary tiebreak: admission score, then age
    (oldest first). Decay (>72h) pushes one rank down."""
    return (_age_decayed_rank(item, now), -item.admission_score,
            float(item.created_at or now))


def _effort_display_key(item: NowItem, rank_key):
    """§6.3 default display order: quick-win → medium → deep; ties keep rank
    order (rank decides admission; effort decides display)."""
    return (EFFORT_ORDER.get(item.effort, 9), rank_key)


def _focus_reason(pending_rank: Sequence[NowItem], toggle: bool) -> Optional[str]:
    """§6.3 focus triggers, in the spec's order: (a) a deep focus block is
    pending, (b) any item is in-progress, (c) the manual toggle."""
    if toggle:
        return "manual toggle"
    if any(i.state == "in-progress" for i in pending_rank):
        return "an item is in-progress"
    if any(i.effort == "deep" for i in pending_rank):
        return "a deep focus block is pending"
    return None


def _effort_mix(items: Iterable[NowItem]) -> str:
    counts: Dict[str, int] = {}
    for i in items:
        counts[i.effort] = counts.get(i.effort, 0) + 1
    return " · ".join(f"{n} {e}" for e, n in
                      sorted(counts.items(), key=lambda kv: EFFORT_ORDER.get(kv[0], 9)))


def build_now_list(decisions: Sequence[Any] = (),
                   queue_items: Sequence[Any] = (),
                   onboarding_steps: Sequence[Any] = (),
                   *, now: float,
                   focus_toggle: bool = False,
                   excluded_ids: Sequence[str] = (),
                   decisions_evidence: Optional[Sequence[Sequence[Dict[str, Any]]]] = None,
                   decisions_options: Optional[Sequence[Sequence[Dict[str, Any]]]] = None,
                   ) -> Dict[str, Any]:
    """Source → rank → never-list filter → hard cap 3 → focus → tally.

    Returns the render-ready payload:
      items       every non-excluded item (pending capped, done today kept)
      shown/hidden  the focus collapse (§6.3)
      tally       {closed_today, carried, new, total_done, header}
      focused / focus_reason / zero
    """
    excluded = set(excluded_ids or ())
    candidates: List[NowItem] = []

    for i, d in enumerate(decisions):
        ev = (decisions_evidence or [()] * len(decisions))[i] \
            if decisions_evidence else ()
        op = (decisions_options or [()] * len(decisions))[i] \
            if decisions_options else ()
        if isinstance(d, NowItem):
            candidates.append(d)
        else:
            candidates.append(item_from_decision(d, i, evidence=ev, options=op))

    for q in queue_items:
        it = q if isinstance(q, NowItem) else item_from_queue(q, now)
        if it is not None:
            candidates.append(it)

    for s in onboarding_steps:
        candidates.append(s if isinstance(s, NowItem) else item_from_onboarding(s))

    # --- never-list enforcement (§6.1) -----------------------------------
    kept: List[NowItem] = []
    for it in candidates:
        if it.id in excluded:
            continue                                    # agent-handled / excluded
        if it.kind not in _NOW_ITEM_KINDS:
            continue                                    # unknown source → out
        if it.kind == "queue-gate" and it.admission_score < ADMISSION_GATE:
            continue                                    # below-gate stays in Zone B
        if it.state in AGENT_HANDLED_STATES:
            continue
        if it.state is None:
            continue
        if it.title.lower().startswith(("risk", "watch", "metric", "recommend")) \
                and it.kind == "decision":
            continue                                    # risks/watch ≠ acting
        kept.append(it)

    rank_keys = {id(it): _admission_rank_key(it, now) for it in kept}
    # Active items = pending + in-progress (an in-progress item is the list
    # itself per §6.3.2 and counts against the cap like any pending item).
    active = [i for i in kept if i.state in ("pending", "in-progress")]
    active.sort(key=lambda it: rank_keys[id(it)])
    # Hard cap 3 (§6.1): a 4th candidate is never rendered — it stays in Zone B.
    pending = active[:NOW_CAP]

    # done/crossed-off items resolved today stay visible (§6.4); older done
    # items are carried until the next daily generation, then drop off.
    today_start = _utc_day_start(now)
    done = [i for i in kept if i.state in ("done", "done-by-agent")]
    done_today = [i for i in done if i.resolved_at is not None
                  and float(i.resolved_at) >= today_start]
    carried = [i for i in done if i.resolved_at is not None
               and float(i.resolved_at) < today_start]
    done_today.sort(key=lambda i: -(float(i.resolved_at or 0)))
    carried.sort(key=lambda i: -(float(i.resolved_at or 0)))

    # Focus mode (§6.3): exactly 1 pending item when triggered.
    ordered = sorted(pending, key=lambda it: _effort_display_key(it, rank_keys[id(it)]))
    reason = _focus_reason(pending, focus_toggle)
    if reason and ordered:
        if any(i.state == "in-progress" for i in ordered):
            top = next(i for i in ordered if i.state == "in-progress")
        elif reason == "a deep focus block is pending":
            top = next(i for i in pending if i.effort == "deep")
        else:
            top = ordered[0]          # manual toggle: the top-ranked item
        shown = [top]
        hidden = [i for i in ordered if i.id != top.id]
    else:
        shown, hidden = ordered, []

    closed_today = len(done_today)
    total_today = closed_today + len(pending)
    if total_today and closed_today == total_today:
        header = "All clear ✓"
    else:
        header = f"NOW — {closed_today} of {total_today} done today"

    return {
        "items": ordered,
        "shown": shown,
        "hidden": hidden,
        "focused": bool(reason and ordered),
        "focus_reason": reason,
        "done_today": done_today,
        "carried": carried,
        "zero": not ordered and not done_today,
        "tally": {
            "closed_today": closed_today,
            "carried": len(carried),
            "new": sum(1 for i in pending
                       if i.created_at is not None
                       and float(i.created_at) >= today_start),
            "total_done": closed_today + len(carried),
            "header": header,
        },
    }


def _utc_day_start(now: float) -> float:
    return datetime.fromtimestamp(now, tz=timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0).timestamp()


# --- rendering ------------------------------------------------------------------

def _esc(v: Any) -> str:
    return html.escape("" if v is None else str(v))


def _effort_pill(item: NowItem) -> str:
    e = item.effort
    return (f'<span class="now-effort" data-effort="{e}" title="{_esc(EFFORT_HINT.get(e, ""))}">'
            f'{_esc(EFFORT_BADGE.get(e, ""))} {_esc(e)}</span>')


def _row_html(item: NowItem) -> str:
    cls = {"pending": "now-pending", "in-progress": "now-in-progress",
           "done": "now-done", "done-by-agent": "now-done now-by-agent"}[item.state]
    if item.state == "pending":
        marker = '<span class="now-box" aria-label="pending"></span>'
        title = f'<span class="now-title">{_esc(item.title)}</span>'
        tail = f'<span class="now-age">{_esc(item.age_text)}</span>'
    elif item.state == "in-progress":
        marker = '<span class="now-dot" aria-label="in-progress"></span>'
        title = f'<span class="now-title now-ip">{_esc(item.title)}</span>'
        tail = f'<span class="now-age">{_esc(item.age_text)}</span>'
    else:
        marker = '<span class="now-check">✓</span>'
        title = f'<span class="now-title now-crossed">{_esc(item.title)}</span>'
        extra = ("" if item.state == "done" else
                 '<div class="now-crossed-for">crossed off for you — '
                 'the agent resolved it</div>')
        tail = (f'<span class="now-done-line">{_esc(item.done_text)}{extra}</span>')
    return (f'<li class="now-row {cls}" data-item-id="{_esc(item.id)}" '
            f'data-kind="{_esc(item.kind)}">'
            f'{marker}{title}{_effort_pill(item)}{tail}</li>')


def now_list_html(result: Dict[str, Any]) -> str:
    """The 'The Now list' panel (§6.7 layout). Zero-state is written, never
    blank; hidden focus items remain visible as one line; the done-today
    group stays at the bottom (≤5 shown, then a count)."""
    rows = [_row_html(i) for i in result.get("shown") or []]
    hidden = result.get("hidden") or []
    if hidden:
        rows.append(f'<li class="now-row now-hidden">+{len(hidden)} more · '
                    f'{_esc(_effort_mix(hidden))}</li>')
    if result.get("zero"):
        rows.append(f'<li class="now-row now-zero">{_esc(ZERO_STATE)}</li>')
    done_group = result.get("done_today") or []
    if done_group:
        rows.append('<li class="now-done-group"><span class=now-age>done today</span>'
                    '<ul class="now-done-list">')
        for i in done_group[:DONE_GROUP_MAX]:
            rows.append(_row_html(i))
        if len(done_group) > DONE_GROUP_MAX:
            rows.append(f'<li class="now-row now-hidden">'
                        f'+{len(done_group) - DONE_GROUP_MAX} more done</li>')
        rows.append("</ul></li>")
    focus = ""
    if result.get("focused"):
        focus = f'<span class="now-focus" data-reason="{_esc(result.get("focus_reason", ""))}">focus ⟳</span>'
    return (f'<section id="now-list" class="panel">'
            f'<h2>{_esc((result.get("tally") or {}).get("header", "NOW"))} {focus}</h2>'
            f'<ul class="now-rows">{"".join(rows)}</ul></section>')


def now_list_text(result: Dict[str, Any]) -> str:
    """Console/plain template for the Now list (titles + effort tags only)."""
    lines = [(result.get("tally") or {}).get("header", "NOW")]
    for i in result.get("shown") or []:
        lines.append(f"  {EFFORT_BADGE.get(i.effort, '·')}  {i.title}"
                     f"  ({i.effort}, {i.age_text})")
    hidden = result.get("hidden") or []
    if hidden:
        lines.append(f"  +{len(hidden)} more · {_effort_mix(hidden)}")
    if result.get("zero"):
        lines.append(f"  {ZERO_STATE}")
    for i in (result.get("done_today") or [])[:DONE_GROUP_MAX]:
        lines.append(f"  {i.done_text} — {i.title}")
    return "\n".join(lines)


# --- detail view (§6.5) ------------------------------------------------------------

def detail_fields(item: NowItem) -> Dict[str, str]:
    """Flat key → rendered-value map; every missing field renders
    'unavailable' (fail-closed), never a blank that looks like 'none'."""
    ev = ", ".join(str(e.get("label") or e.get("source") or "unavailable")
                   for e in (item.evidence or ())) or "unavailable"
    out = {
        "title": item.title or "unavailable",
        "effort": f"{item.effort} ({EFFORT_HINT.get(item.effort, 'unavailable')})",
        "age": item.age_text or "unavailable",
        "source": str((item.detail or {}).get("source")
                      or {"decision": "exec summary — decisions needed",
                          "queue-gate": "Zone B decision queue",
                          "onboarding": "declared onboarding checklist"}.get(item.kind)
                      or "unavailable"),
        "evidence": ev,
    }
    if item.kind == "queue-gate":
        d = item.detail or {}
        out["admission_score"] = (f"{item.admission_score:.2f}"
                                  if item.admission_score else "unavailable")
        out["why_admitted"] = (f"admission score {item.admission_score:.2f} ≥ gate "
                               f"{ADMISSION_GATE}"
                               + (" (Ray-gated: score pinned high)" if item.ray_gated else ""))
        out["shadow_action"] = str(d.get("proposed_action") or "unavailable")
        out["steps"] = "; ".join(str(s) for s in (d.get("steps") or ())) or "unavailable"
    elif item.kind == "decision":
        out["options"] = "; ".join(
            f"{o.get('label', 'unavailable')} → {o.get('consequence', 'unavailable')}"
            for o in (item.options or ())) or "unavailable"
    elif item.kind == "onboarding":
        d = item.detail or {}
        out["command"] = str(d.get("command") or d.get("click_path") or "unavailable")
    return out


def detail_html(item: NowItem) -> str:
    fields = detail_fields(item)
    rows = "".join(f"<div class='now-detail-row'><b>{_esc(k)}</b>: {_esc(v)}</div>"
                   for k, v in fields.items())
    if item.kind == "queue-gate":
        rows += "".join(
            f"<button class='btn' data-item-id='{_esc(item.id)}' "
            f"data-action='gate-{_esc(o.get('label', '').lower())}'>"
            f"{_esc(o.get('label', 'unavailable'))}</button>"
            for o in (item.options or ()))
    elif item.kind == "decision" and item.options:
        rows += "".join(
            f"<button class='btn' data-item-id='{_esc(item.id)}' "
            f"data-action='decide-{_esc(o.get('label', '').lower())}'>"
            f"{_esc(o.get('label', 'unavailable'))}</button>"
            for o in item.options)
    return f"<div class='now-detail'>{rows}</div>"
