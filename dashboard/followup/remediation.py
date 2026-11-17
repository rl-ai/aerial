"""Remediation contract — a self-fix is PROVEN only by re-running the symptom probe.

The operator's directive: self-remediation must include test output proving the fix —
it cannot return success until the fix is proven. This module is that
contract:

  1. run the symptom_check  -> BEFORE output (ok + detail)
  2. execute fix_cmd (shell)
  3. re-run symptom_check   -> AFTER output
  4. success ONLY if AFTER is ok
  5. unproved -> the finding stays OPEN, escalation required, both outputs
     attached to the result.

requires_human=True marks operator-gated actions (container lifecycle restarts):
the runner NEVER executes fix_cmd — it produces a prepared copy-paste block
and pings the operator; the proof happens on the FOLLOWING poll, when the symptom
probe (re-run as the BEFORE check) comes back ok.

Registry: REMEDIATIONS maps remediation id -> Remediation. find_remediation
matches a finding by finding_id prefix or by check name/kind.
"""

from __future__ import annotations

import os
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional, Tuple, Union

CheckResult = Tuple[bool, str]
SymptomCheck = Callable[[], CheckResult]

OUTPUT_TAIL_CHARS = 2000


@dataclass
class Remediation:
    """One registered self-fix for a class of findings."""
    id: str
    #: finding_id prefix OR check name/kind this remediation applies to.
    finding_match: Union[str, Tuple[str, ...]]
    #: the probe that defines the symptom: () -> (ok, detail).
    symptom_check: SymptomCheck
    #: shell command that performs the fix.
    fix_cmd: str
    #: re-probe after the fix; defaults to the same symptom_check.
    verify_check: Optional[SymptomCheck] = None
    max_attempts: int = 1
    #: operator-gated: never executed by the runner — prepared for a human; the
    #: NEXT poll's symptom_check proves it.
    requires_human: bool = False

    def matches(self, finding) -> bool:
        matches = (self.finding_match,) if isinstance(self.finding_match, str) \
            else tuple(self.finding_match)
        fid = getattr(finding, "finding_id", "")
        check = getattr(finding, "check", None) or {}
        names = {check.get("name"), check.get("kind"),
                 check.get("check"), check.get("remediation")}
        for m in matches:
            if not m:
                continue
            if m in names:
                return True
            # finding_id prefix (segment-boundary: "hub-fd-exhaustion:...")
            if fid and (fid == m or fid.startswith(m + ":")):
                return True
        return False


@dataclass
class RemediationResult:
    """Structured outcome of one run_remediation pass."""
    id: str
    before: Dict[str, Any]
    after: Dict[str, Any]
    proved: bool
    attempts: int
    output_tail: str
    fix_exit: Optional[int] = None
    #: requires_human only: the prepared copy-paste block for the operator.
    prepared: Optional[str] = None
    #: set when NOT proved: escalation-required note with both outputs.
    escalation: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "id": self.id, "before": self.before, "after": self.after,
            "proved": self.proved, "attempts": self.attempts,
            "output_tail": self.output_tail, "fix_exit": self.fix_exit,
            "prepared": self.prepared, "escalation": self.escalation,
        }


def _run_check(check: SymptomCheck) -> Dict[str, Any]:
    """Run a symptom probe; never raise. -> {ok, detail}."""
    try:
        ok, detail = check()
    except Exception as exc:
        return {"ok": False, "detail": f"probe error: {exc!r}"}
    return {"ok": bool(ok), "detail": str(detail)[:1000]}


def run_fix(rem: Remediation) -> Dict[str, Any]:
    """Execute fix_cmd in a shell; never raise. -> {exit, output_tail}."""
    try:
        proc = subprocess.run(rem.fix_cmd, shell=True, timeout=600,
                              stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True)
        return {"exit": proc.returncode,
                "output_tail": (proc.stdout or "")[-OUTPUT_TAIL_CHARS:]}
    except subprocess.TimeoutExpired:
        return {"exit": None, "output_tail": "[fix timed out]"}
    except Exception as exc:
        return {"exit": None, "output_tail": f"[fix error: {exc!r}]"}


def prepared_block(rem: Remediation, finding, before: Dict[str, Any]) -> str:
    """The copy-paste block pinged to the operator for a requires_human remediation."""
    fid = getattr(finding, "finding_id", "?")
    return (
        f"REMEDIATION {rem.id} PREPARED — HUMAN ACTION REQUIRED (operator-gated)\n"
        f"finding: {fid}\n"
        f"symptom BEFORE: {before['detail']}\n"
        f"copy-paste command:\n    {rem.fix_cmd}\n"
        f"After a human runs it, the NEXT poll re-runs the symptom check "
        f"({rem.id} probe); the finding closes only when that probe returns "
        f"ok. Until then the finding stays OPEN."
    )


def run_remediation(rem: Remediation, finding=None,
                    fix_executor: Optional[Callable[[Remediation], Dict[str, Any]]] = None
                    ) -> RemediationResult:
    """Run the remediation contract: probe -> fix -> re-probe -> prove.

    proved=True only when the AFTER probe (or, for requires_human, the
    BEFORE probe on a later poll) is ok. Unproved => escalation required
    with both outputs attached.
    """
    execute = fix_executor or run_fix
    verify = rem.verify_check or rem.symptom_check

    before = _run_check(rem.symptom_check)

    # operator-gated: prepare, never execute. Proof = the NEXT poll's probe
    # (the BEFORE check below comes back ok once the human ran the fix).
    if rem.requires_human:
        if before["ok"]:
            return RemediationResult(
                id=rem.id, before=before, after=dict(before), proved=True,
                attempts=0, output_tail=before["detail"],
                escalation=None)
        return RemediationResult(
            id=rem.id, before=before, after={"ok": None, "detail": "not run — awaiting human"},
            proved=False, attempts=0, output_tail=before["detail"],
            prepared=prepared_block(rem, finding, before),
            escalation=("requires_human: fix NOT executed — prepared block "
                        "produced, finding stays OPEN until the next poll's "
                        f"probe is ok. BEFORE: {before['detail']}"))

    if before["ok"]:
        # Nothing to fix; the probe is the proof.
        return RemediationResult(
            id=rem.id, before=before, after=dict(before), proved=True,
            attempts=0, output_tail=before["detail"])

    attempts = 0
    after = before
    fix_out: Dict[str, Any] = {"exit": None, "output_tail": ""}
    while attempts < max(1, rem.max_attempts):
        attempts += 1
        fix_out = execute(rem)
        after = _run_check(verify)
        if after["ok"]:
            break

    output_tail = (fix_out.get("output_tail") or "")[-OUTPUT_TAIL_CHARS:]
    if after["ok"]:
        return RemediationResult(
            id=rem.id, before=before, after=after, proved=True,
            attempts=attempts, output_tail=output_tail,
            fix_exit=fix_out.get("exit"))
    return RemediationResult(
        id=rem.id, before=before, after=after, proved=False,
        attempts=attempts, output_tail=output_tail,
        fix_exit=fix_out.get("exit"),
        escalation=("fix NOT proved — escalation required, finding stays "
                    f"OPEN. BEFORE: {before['detail']} | AFTER: "
                    f"{after['detail']} | fix exit {fix_out.get('exit')}"))


# ---------------------------------------------------------------------------
# The registry + the first real remediation: hub fd exhaustion
# (2026-11-17 incident: ticket-hub hit 1023/1024 fds, /board 500s, writes
# stopped). Symptom = the same probe the SRE monitor uses: GET /board with
# the READ token -> 200. Fix = docker restart ticket-hub — operator-gated.
# ---------------------------------------------------------------------------

HUB_BOARD_URL = "http://127.0.0.1:8020/board"
HUB_TOKEN_PATH = os.path.expanduser("~/.config/aiops/hub.token")   # READ-class


def hub_board_check(url: str = HUB_BOARD_URL,
                    token_path: str = HUB_TOKEN_PATH,
                    timeout: float = 10.0) -> CheckResult:
    """GET /board with the READ token -> (status == 200, detail)."""
    try:
        with open(token_path) as f:
            token = f.read().strip()
    except OSError:
        token = None
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return (resp.status == 200, f"/board HTTP {resp.status}")
    except urllib.error.HTTPError as e:
        return (False, f"/board HTTP {e.code}")
    except Exception as e:
        return (False, f"/board unreachable: {str(e)[:200]}")


HUB_FD_REMEDIATION = Remediation(
    id="hub-fd-exhaustion",
    finding_match=("hub-fd-exhaustion", "hub_fds", "hub_fd_exhaustion"),
    symptom_check=hub_board_check,
    fix_cmd="docker restart ticket-hub",
    verify_check=hub_board_check,
    max_attempts=1,
    requires_human=True,       # container lifecycle restart = operator-gated
)

REMEDIATIONS: Dict[str, Remediation] = {
    HUB_FD_REMEDIATION.id: HUB_FD_REMEDIATION,
}


def find_remediation(finding) -> Optional[Remediation]:
    """The registered remediation matching this finding, or None."""
    for rem in REMEDIATIONS.values():
        if rem.matches(finding):
            return rem
    return None


def register(rem: Remediation) -> None:
    REMEDIATIONS[rem.id] = rem
