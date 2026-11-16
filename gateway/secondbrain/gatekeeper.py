"""The gatekeeper: judge + surgeon, the only standing service (§6).

Properties held here:
- content is input, never authority — a question's text can never alter a
  seat, expand a scope, or change a budget (landscape A1, verbatim);
- citation check is mechanical: every citation hash must be inside the
  grant's scope closure at the pinned L2 version;
- the surgeon pass enforces the grant's disclosure rules (generalize
  location, date granularity, forbid list, require_citation);
- refusal bias: anything borderline is REFUSED and flagged, never dressed up
  as an answer; the verdict is answered | refused | partial (landscape A6);
- the pre-edit draft and the released output are both logged, so you can see
  exactly what was stripped.

v1 note: the de-ID step is rule-based (the disclosure rules are mechanical).
The LLM de-ID pass of the design doc is a v2 item; refusal bias covers the
gap conservatively — anything the rules can't clear is refused, not released.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from .answerer import compile_seat, draft_answer
from .vault import label_rank

_MONTH = re.compile(r"\b(\d{4})-(\d{2})-\d{2}(?![0-9])")
_LOCATIONS = re.compile(
    r"\b(Rua\b[^,.]*|Lv[- ]?\d+|\d+\s[\w-]+\s(?:street|st\.|road|rd)\b)",
    re.IGNORECASE)


class Gatekeeper:
    def __init__(self, workspace, book, *, shadow: bool = False,
                 flag_log=None) -> None:
        """``shadow=True`` is the flag-only rollout (A4): the full judge and
        surgeon pipeline runs and every verdict is logged, but nothing is
        enforced and no budget is consumed — you review the shadow log and
        flip to enforcing when it looks right.

        ``flag_log`` (G7): path of the persisted JSONL flag log. Defaults to
        ``flags.jsonl`` under the workspace root; restarts keep governance
        signals because the digest reads this log, not memory."""
        self.ws = workspace
        self.book = book
        self.shadow = shadow
        self.flags: list[dict] = []   # the uncertainty feed (§8 surface 5)
        self.flag_log = (Path(flag_log) if flag_log is not None
                         else Path(workspace.root) / "flags.jsonl")

    def ask(self, grant_id: str, key: str, question: str,
            now: float | None = None, *,
            sender_kind: str = "person",
            in_flight_request: bool = False,
            mandate_hints: dict | None = None) -> dict:
        """One grant-bound request. Returns {verdict, answer?, reason?}.

        Verdicts: answered | refused | handed_back | shadow. Admission
        strength is graded first (A2): follow-ups are context only and bots
        never open work."""
        from .admission import Admission, AdmissionError, HandBack
        admission = Admission(self.book)
        try:
            strength = admission.grade(grant_id, key, question,
                                       sender_kind=sender_kind,
                                       in_flight_request=in_flight_request)
        except AdmissionError as exc:
            # admission-level refusals (bots, bad keys, exhaustion) are
            # logged but never consume the budget: they are not the grant's
            # queries (A2); only gate-level refusals count (design §5).
            self.book.log(grant_id, "admission_refused", question=question,
                          reason=f"admission {exc}")
            return {"verdict": "refused", "reason": str(exc)}
        if strength == "follow-up":
            # context only: it reaches the owning request, never the gate's
            # budget — logged so the thread is auditable, nothing released
            self.book.log(grant_id, "followup", question=question,
                          reason="follow-up on an owned request: context only")
            return {"verdict": "follow-up",
                    "reason": "delivered as context to the owned request"}

        try:
            grant = self.book.active(grant_id, key, now=now)
            if not self.shadow:
                # single budget check (glm review 2026-11-11): shadow mode
                # consumes nothing, enforcing mode charges exactly once
                self.book.check_budget(grant)
        except Exception as exc:  # GrantError: refused before the worker spawns
            # expired, revoked, bad key or exhausted budget: admission-level,
            # logged but never charged against the grant (design §5 counts
            # only the gate's own refusals of in-scope asks)
            self.book.log(grant_id, "admission_refused", question=question,
                          reason=str(exc))
            return {"verdict": "refused", "reason": str(exc)}

        # The worker spawns seated on the scope slice (topology, not policy).
        seat = compile_seat(self.ws, grant)
        draft = draft_answer(seat, question)
        if not draft["covered"]:
            # A3: beyond-mandate asks are handed back for re-routing, never
            # silently expanded and never a bare refusal when the ask names
            # something the publisher flagged as adjacent-mandate.
            hints = mandate_hints or {}
            outside = [eid for eid, kws in hints.items()
                       if eid not in grant["anchor_entities"]
                       and any(kw in question.lower() for kw in kws)]
            if outside:
                reason = "hand-back: outside mandate, adjacent: " \
                         + ", ".join(outside)
                self.book.log(grant["id"], "handed_back", question=question,
                              reason=reason)
                self._flag(grant, question, reason)
                return {"verdict": "handed_back", "reason": reason}
            self.book.log(grant["id"], "refused", question=question,
                          reason="nothing in scope answers the question")
            self._flag(grant, question, "no in-scope content")
            return {"verdict": "refused",
                    "reason": "nothing in scope answers the question"}

        problems = self._surgeon(grant, seat, draft)
        if problems:
            reason = "; ".join(problems)
            if self.shadow:
                # A4 flag-only rollout: log what WOULD be refused, consume
                # nothing, release nothing.
                self.book.log(grant["id"], "shadow_refused",
                              question=question, draft=draft["text"],
                              reason=reason)
                self._flag(grant, question, "shadow: " + reason)
                return {"verdict": "shadow", "reason": reason,
                        "would": "refuse"}
            self.book.log(grant["id"], "refused", question=question,
                          draft=draft["text"], reason=reason)
            self._flag(grant, question, reason)
            return {"verdict": "refused", "reason": reason}

        released = self._sanitize(grant, draft["text"])
        stripped = "\n".join(
            f"- {a} -> {b}" for a, b in self._diff(draft["text"], released))
        if self.shadow:
            self.book.log(grant["id"], "shadow_pass", question=question,
                          draft=draft["text"], released=released,
                          stripped=stripped or "(nothing stripped)")
            return {"verdict": "shadow", "answer": released,
                    "citations": draft["citations"], "would": "answer"}
        # sanitization is the disclosure contract, not lost coverage: a
        # surgeon-passed release is `answered` (partial is reserved for
        # questions only partly covered — not detectable in v1).
        self.book.log(grant["id"], "answered", question=question,
                      draft=draft["text"], released=released,
                      stripped=stripped or "(nothing stripped)")
        return {"verdict": "answered", "answer": released,
                "citations": draft["citations"]}

    # -- the surgeon pass -----------------------------------------------------

    def _surgeon(self, grant: dict, seat, draft: dict) -> list[str]:
        """Citation check + per-drafted-node screening. Any problem refuses."""
        problems: list[str] = []
        scope_hashes = {r for nid in seat.node_ids
                        for r in seat.nodes[nid].refs if len(r) == 64}
        rules = grant["disclosure"]
        if rules.get("require_citation") and not draft["citations"]:
            problems.append("draft carries no citations")
        for citation in draft["citations"]:
            if citation not in scope_hashes:
                problems.append(f"citation {citation[:12]} outside scope")
        forbidden = set(rules.get("forbid", []))
        for nid in draft.get("node_ids", ()):
            node = seat.nodes[nid]
            if node.id in forbidden:
                problems.append(f"forbidden content drafted: {node.id}")
            if label_rank(node.label) >= label_rank("intimate"):
                # refusal bias: intimate-label originals never release
                problems.append(f"intimate-label content drafted: {node.id}")
        return problems

    def _sanitize(self, grant: dict, text: str) -> str:
        """Enforce the disclosure rules on the released text."""
        rules = grant["disclosure"]
        out = text
        if rules.get("generalize_location") == "city":
            out = _LOCATIONS.sub("[location generalized]", out)
        if rules.get("max_date_granularity") == "month":
            out = _MONTH.sub(lambda m: f"{m.group(1)}-{m.group(2)}", out)
        return out

    @staticmethod
    def _diff(before: str, after: str) -> list[tuple[str, str]]:
        changes = []
        for b_line, a_line in zip(before.splitlines(), after.splitlines()):
            if b_line != a_line:
                changes.append((b_line.strip(), a_line.strip()))
        return changes

    def _flag(self, grant: dict, question: str, why: str) -> None:
        entry = {"grant": grant["id"], "question": question[:80],
                 "why": why,
                 "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        self.flags.append(entry)
        # G7: persist the flag so a restart keeps the governance signal.
        try:
            self.flag_log.parent.mkdir(parents=True, exist_ok=True)
            with open(self.flag_log, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass  # the ledger already carries the underlying event

    def persisted_flags(self, since_ts: str | None = None) -> list[dict]:
        """Flags read back from the persisted JSONL log (G7): survives a
        process restart, unlike ``self.flags``. Corrupt lines are skipped."""
        if not self.flag_log.exists():
            return list(self.flags)
        rows: list[dict] = []
        try:
            text = self.flag_log.read_text(encoding="utf-8")
        except OSError:
            return list(self.flags)
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        if since_ts:
            rows = [f for f in rows if (f.get("ts") or "") >= since_ts]
        return rows
