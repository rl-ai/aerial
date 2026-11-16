#!/usr/bin/env python3
"""PC-1..PC-7 conformance verifier — docs/pipeline-checkpoints.md.

One check function per gate, each asserting the gate rule against LIVE
sampled data. A failed rule → a finding classified through the followup
classifier (R6 default → daily aggregate; a BYPASS — an artifact that
crossed a stage boundary without its gate running at all — is R1 security
by definition, routed via the classifier's fail-open vocabulary to an
immediate ping).

Design rules (from the contract):
- Every check reads only persisted, queryable state (bridge/vault ingests,
  ingest reports, proposal ledger, followup sqlite, hub DB, Gauntlet
  tickets). Samplers are injectable so the whole pass is hermetic in tests.
- Fail-closed: a source that cannot be read yields `available: False`
  ("not-checkable") — never a fake pass, never fabricated samples.
- The pass is short (a runner duty): sample the last N records, assert,
  return. Anything slow belongs to a worker, not this duty.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time

from deployment_profile import pvalue as _pv
from typing import Any, Callable, Dict, List, Optional, Sequence

HERE = os.path.dirname(os.path.abspath(__file__))
_PKG_ROOT = os.path.dirname(HERE)
import sys

for _p in (_PKG_ROOT, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import exec_summary  # noqa: E402  (TICKET_ID_RE, verify helpers, caps)

#: The vault sensitivity lattice (mirrored from
#: gateway/bridge.py::SENSITIVITIES — the PC-1 enforcement
#: point). A missing label defaults to `personal`; a present-but-invalid
#: label is a bypass.
LATTICE = ("public", "personal", "sensitive", "intimate")
DEFAULT_LABEL = "personal"

#: PC-3 burst alarm ceiling (proposals.py BURST_ABS_PER_DAY).
BURST_ABS_PER_DAY = 50

#: PC-7 recency window for the latest Gauntlet run ticket.
GAUNTLET_RECENCY_S = 7 * 86400.0

DEFAULT_DIGEST = exec_summary.DEFAULT_DIGEST
DEFAULT_PROPOSALS_LEDGER = os.path.expanduser(
    str(_pv("proposals_ledger")))
DEFAULT_FOLLOWUPS_DB = str(_pv("run_dir") / "ops-agent-followups.db")
DEFAULT_SUMMARY_HTML = os.path.expanduser("~/workspaces/build/daily-board.html")

SAMPLE_N = 20


# ---------------------------------------------------------------- result shape

def _result(gate: str, ok: bool, checked: int, violations: List[str],
            *, bypass: bool = False, detail: str = "") -> Dict[str, Any]:
    return {"gate": gate, "ok": bool(ok), "checked": checked,
            "violations": violations, "bypass": bypass,
            "detail": detail or "; ".join(violations)[:400], "available": True}


def _not_checkable(gate: str, detail: str) -> Dict[str, Any]:
    return {"gate": gate, "ok": None, "checked": 0, "violations": [],
            "bypass": False, "detail": detail, "available": False}


# ---------------------------------------------------------------- PC-1

def check_pc1(ingests: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Recent ingests: each persisted label is on the lattice, or the item
    was recorded as defaulting to `personal` (missing label). A present-but-
    invalid label that ENTERED the vault = a bypass (R1)."""
    if not ingests:
        return _not_checkable("PC-1", "no recent ingests sampled")
    violations: List[str] = []
    bypass = False
    for rec in ingests:
        label = rec.get("sensitivity")
        iid = rec.get("id") or rec.get("guid") or "?"
        if label is None or not str(label).strip():
            continue                      # missing → conservative default, fine
        label = str(label).strip().lower()
        if label in LATTICE:
            continue
        violations.append(f"ingest {iid}: non-lattice label {label!r} entered the vault")
        bypass = True                     # the bridge must fail-closed, never relabel
    return _result("PC-1", not violations, len(ingests), violations, bypass=bypass)


# ---------------------------------------------------------------- PC-2

def check_pc2(reports: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Ingest reports reconcile: seen = ingested + skipped + failed (no
    silent drops). G-3: no persisted seen-count exists on the listening side
    yet — reports without one are reported not-checkable, never a pass."""
    if not reports:
        return _not_checkable("PC-2", "no recent ingest reports sampled")
    violations: List[str] = []
    checkable = 0
    for rep in reports:
        seen = rep.get("seen")
        if seen is None:
            continue                      # G-3 gap, not a gate failure
        checkable += 1
        total = (int(rep.get("ingested") or 0) + int(rep.get("skipped") or 0)
                 + int(rep.get("failed") or 0))
        if total != int(seen):
            violations.append(
                f"report {rep.get('id') or '?'}: ingested+skipped+failed="
                f"{total} != seen={seen}")
    if not checkable:
        return _not_checkable("PC-2", "no report carries a seen-count (G-3)")
    return _result("PC-2", not violations, checkable, violations)


# ---------------------------------------------------------------- PC-3

def check_pc3(rows: Sequence[Dict[str, Any]],
              ceiling: int = BURST_ABS_PER_DAY) -> Dict[str, Any]:
    """Admitted proposals: each has a ledger entry, matches no active
    suppression key, and the day's admissions stay under the burst ceiling."""
    if not rows:
        return _not_checkable("PC-3", "no recently admitted proposals sampled")
    violations: List[str] = []
    for row in rows:
        key = row.get("key")
        if not key:
            violations.append("admitted proposal with no ledger key (no entry)")
            continue
        if row.get("suppressed"):
            violations.append(f"proposal {key} admitted while suppressed")
        if int(row.get("admissions_today") or 0) > ceiling:
            violations.append(f"proposal {key}: daily burst ceiling {ceiling} exceeded")
    return _result("PC-3", not violations, len(rows), violations)


# ---------------------------------------------------------------- PC-4

def check_pc4(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Persisted proposal rows carry ≥3 verbatim evidence items with source
    links — a proposal is never admitted on a bare claim."""
    if not rows:
        return _not_checkable("PC-4", "no proposal rows sampled")
    violations: List[str] = []
    for row in rows:
        key = row.get("key") or row.get("id") or "?"
        evidence = row.get("evidence") or []
        good = [e for e in evidence
                if isinstance(e, dict)
                and str(e.get("text") or e.get("quote") or "").strip()
                and str(e.get("link") or e.get("source") or "").strip()]
        good += [e for e in evidence if isinstance(e, str) and e.strip()]
        if len(good) < 3:
            violations.append(f"proposal {key}: {len(good)} evidence items (<3)")
    return _result("PC-4", not violations, len(rows), violations)


# ---------------------------------------------------------------- PC-5

def check_pc5(followups: Sequence[Dict[str, Any]],
              transitions: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Findings classified (fix_class present); no `unknown`-class finding
    reached fix-attempted; every closed finding has a persisted verify
    transition."""
    if not followups and not transitions:
        return _not_checkable("PC-5", "followup sqlite unavailable/empty")
    by_fid: Dict[str, List[Dict[str, Any]]] = {}
    for t in transitions:
        by_fid.setdefault(str(t.get("finding_id")), []).append(t)
    violations: List[str] = []
    checked = 0
    for f in followups:
        fid = str(f.get("finding_id"))
        checked += 1
        cls = f.get("fix_class")
        if not cls:
            violations.append(f"finding {fid}: no fix_class recorded")
            continue
        states = [str(t.get("to_state")) for t in by_fid.get(fid, [])]
        if cls == "unknown" and "fix-attempted" in states:
            violations.append(f"finding {fid}: unknown class reached fix-attempted")
        if f.get("state") == "closed" and "verified" not in states:
            violations.append(f"finding {fid}: closed with no verify transition")
    return _result("PC-5", not violations, checked, violations)


# ---------------------------------------------------------------- PC-6

def check_pc6(summary_html: str, ticket_exists: Callable[[str], bool],
              headline_cap: int = exec_summary.HEADLINE_CAP) -> Dict[str, Any]:
    """Latest summary: (a) every cited TK-/EM- id exists in the hub, (b) no
    `unavailable` block contributed numbers (a digit inside an unavailable
    marker = fabricated data), (c) section counts within caps."""
    if not summary_html:
        return _not_checkable("PC-6", "no produced summary found")
    violations: List[str] = []
    ids = set(exec_summary.TICKET_ID_RE.findall(summary_html))
    for tid in sorted(ids):
        try:
            ok = bool(ticket_exists(tid))
        except Exception as exc:
            violations.append(f"ticket {tid}: verification error {exc!r}")
            continue
        if not ok:
            violations.append(f"cited ticket {tid} does not exist in the hub")
    for m in re.finditer(r"<p class=unavailable>(.*?)</p>", summary_html, re.S):
        if re.search(r"\d", m.group(1)):
            violations.append("unavailable block contains numbers (fabrication risk)")
    m = re.search(r"<ul class=brief>(.*?)</ul>", summary_html, re.S)
    if m:
        n = len(re.findall(r"<li>", m.group(1)))
        if n > headline_cap:
            violations.append(f"{n} headlines rendered (cap {headline_cap})")
    return _result("PC-6", not violations,
                   len(ids) + len(re.findall(r"<p class=unavailable>", summary_html)),
                   violations)


# ---------------------------------------------------------------- PC-7

def check_pc7(gauntlet_tickets: Sequence[Dict[str, Any]], now: float,
              recency_s: float = GAUNTLET_RECENCY_S) -> Dict[str, Any]:
    """The Gauntlet release gate: the gauntlet run ticket exists, is recent,
    and its trail shows 5/5 layer PASS records. A release event with a
    missing or failed layer record = a bypass (R1)."""
    if not gauntlet_tickets:
        return _not_checkable("PC-7", "no gauntlet run tickets found")
    runs = sorted(gauntlet_tickets,
                  key=lambda t: float(t.get("created_at") or 0), reverse=True)
    latest = runs[0]
    violations: List[str] = []
    bypass = False
    created = latest.get("created_at")
    if created is None:
        violations.append("latest gauntlet run ticket has no timestamp")
    elif now - float(created) > recency_s:
        violations.append(f"latest gauntlet run is stale "
                          f"({(now - float(created)) / 86400:.1f}d old)")
    layers = latest.get("layers") or {}
    if len(layers) < 5:
        violations.append(f"gauntlet trail shows {len(layers)}/5 layer records")
        bypass = True                  # release crossed the gate without it running
    else:
        for n in range(1, 6):
            verdict = str(layers.get(str(n)) or layers.get(n) or "").upper()
            if verdict != "PASS":
                violations.append(f"layer {n}: {verdict or 'NO RECORD'} (need PASS)")
                if not verdict:
                    bypass = True
    return _result("PC-7", not violations, 1, violations, bypass=bypass)


# ---------------------------------------------------------------- samplers (G-2)

def sample_ingests(digest_path: str = DEFAULT_DIGEST,
                   n: int = SAMPLE_N) -> Optional[List[Dict[str, Any]]]:
    """Last N ingests from the bridge digest (persisted, queryable state)."""
    try:
        with open(digest_path, "r", encoding="utf-8") as f:
            digest = json.load(f)
    except (OSError, ValueError):
        return None
    items = digest.get("ingested") if isinstance(digest, dict) else None
    if not isinstance(items, list):
        return None
    out = []
    for it in items[-n:]:
        if isinstance(it, dict):
            out.append({"id": it.get("guid") or it.get("id"),
                        "sensitivity": it.get("sensitivity")})
        else:
            out.append({"id": None, "sensitivity": None})
    return out


def sample_proposals(ledger_path: str = DEFAULT_PROPOSALS_LEDGER,
                     n: int = SAMPLE_N) -> Optional[List[Dict[str, Any]]]:
    """Last N proposal rows from the persisted candidate ledger."""
    try:
        with open(ledger_path, "r", encoding="utf-8") as f:
            ledger = json.load(f)
    except (OSError, ValueError):
        return None
    cands = ledger.get("candidates") if isinstance(ledger, dict) else None
    if not isinstance(cands, dict):
        return None
    out = []
    for key, rec in list(cands.items())[-n:]:
        if isinstance(rec, dict):
            out.append({"key": key, "evidence": rec.get("evidence") or [],
                        "suppressed": bool(rec.get("suppressed")),
                        "admissions_today": rec.get("admissions_today") or 0})
    return out


def sample_followups(db_path: str = DEFAULT_FOLLOWUPS_DB
                     ) -> Optional[tuple]:
    """(followups rows, transitions rows) from the followup sqlite."""
    if not os.path.exists(db_path):
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=0.2)
        conn.row_factory = sqlite3.Row
        fu = [dict(r) for r in conn.execute(
            "SELECT finding_id, fix_class, state FROM followups")]
        tr = [dict(r) for r in conn.execute(
            "SELECT finding_id, from_state, to_state, action, at, detail "
            "FROM transitions")]
        conn.close()
    except sqlite3.Error:
        return None
    return fu, tr


def sample_summary_html(path: str = DEFAULT_SUMMARY_HTML) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


# ---------------------------------------------------------------- the pass

def run_all(*, now: Optional[float] = None,
            digest_path: str = DEFAULT_DIGEST,
            proposals_ledger: str = DEFAULT_PROPOSALS_LEDGER,
            followups_db: str = DEFAULT_FOLLOWUPS_DB,
            summary_path: str = DEFAULT_SUMMARY_HTML,
            ticket_exists: Optional[Callable[[str], bool]] = None,
            gauntlet_tickets: Optional[Sequence[Dict[str, Any]]] = None,
            samples: Optional[Dict[str, Any]] = None,
            ) -> List[Dict[str, Any]]:
    """One conformance pass over live data. `samples` overrides the samplers
    entirely (hermetic tests); `ticket_exists`/`gauntlet_tickets` inject the
    hub-backed probes. Missing sources → not-checkable, never a fake pass."""
    now = now if now is not None else time.time()
    s = samples or {}

    ingests = s.get("ingests", sample_ingests(digest_path))
    pc1 = check_pc1(ingests if ingests is not None else [])

    reports = s.get("reports", [])
    pc2 = check_pc2(reports)

    rows = s.get("proposals", sample_proposals(proposals_ledger))
    pc3 = check_pc3(rows if rows is not None else [])
    pc4 = check_pc4(rows if rows is not None else [])

    fu = s.get("followups", sample_followups(followups_db))
    if fu is None:
        pc5 = _not_checkable("PC-5", f"followup sqlite not found: {followups_db}")
    else:
        pc5 = check_pc5(fu[0], fu[1])

    html_txt = s.get("summary_html", sample_summary_html(summary_path))
    if html_txt is None:
        pc6 = _not_checkable("PC-6", f"no summary at {summary_path}")
    elif ticket_exists is None:
        pc6 = _not_checkable("PC-6", "no hub ticket-verification probe wired")
    else:
        pc6 = check_pc6(html_txt, ticket_exists)

    gt = s.get("gauntlet_tickets", gauntlet_tickets)
    if gt is None:
        pc7 = _not_checkable("PC-7", "no gauntlet ticket probe wired (G-4)")
    else:
        pc7 = check_pc7(gt, now)

    return [pc1, pc2, pc3, pc4, pc5, pc6, pc7]


# ---------------------------------------------------------------- findings

def finding_for(result: Dict[str, Any], clock: Callable[[], float]) -> Optional[Any]:
    """A gate result → an OpsFinding classified through the followup
    classifier. A bypass claim carries fail-open vocabulary so R1
    (sev-security, immediate ping) wins; a merely failing gate has unknown
    reversibility/scope so R6 fail-closed routes it to the daily aggregate.
    Lazy OpsFinding import avoids a runner↔conformance import cycle."""
    if not result.get("available") or result.get("ok") is not False:
        return None
    gate = result.get("gate", "?")
    detail = result.get("detail") or "gate rule failed"
    day = time.strftime("%Y%m%d", time.gmtime(clock()))
    from dashboard.followup.runner import OpsFinding  # noqa: E402  (lazy: cycle)
    if result.get("bypass"):
        return OpsFinding(
            finding_id=f"conformance:{gate}:bypass:{day}",
            claim=(f"pipeline gate bypass: fail-open stage crossing at {gate} "
                   f"— {detail}"),
            reversible=True, scope="internal", covered_by_mandate=False,
            tags=("conformance", "bypass", gate),
            proposed_action=(f"halt the {gate} stage crossing, re-run the gate, "
                             f"and audit artifacts that crossed without it"),
            evidence=dict(result),
            check={"kind": "conformance", "gate": gate, "bypass": True},
            verify=lambda r=result: not r.get("bypass"))
    return OpsFinding(
        finding_id=f"conformance:{gate}:{day}",
        claim=f"conformance gate {gate} failed against live data — {detail}",
        reversible=None, scope=None, covered_by_mandate=False,
        tags=("conformance", gate),
        proposed_action=f"diagnose the {gate} violation and re-run the gate",
        evidence=dict(result),
        check={"kind": "conformance", "gate": gate, "bypass": False},
        verify=lambda r=result: r.get("ok") is True)


def conformance_findings(results: Sequence[Dict[str, Any]],
                         clock: Callable[[], float]) -> List[Any]:
    out = []
    for r in results:
        f = finding_for(r, clock)
        if f is not None:
            out.append(f)
    return out
