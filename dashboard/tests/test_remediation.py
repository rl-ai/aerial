"""Remediation contract tests — a self-fix closes a finding ONLY when the
re-run symptom probe proves it (Ray's directive).

Covers: proved path (finding closes), unproved path (finding stays OPEN,
escalation carries before+after), the mutation test (a verification-skipping
mutant would wrongly close a no-op fix — the real gate refuses), the
requires_human path (prepared block, fix NOT executed, proven on the next
poll), and the hub-fd-exhaustion symptom probe against a 500ing /board.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard.followup import remediation as remd                # noqa: E402
from dashboard.followup import runner as rn                       # noqa: E402
from dashboard.followup.remediation import (                      # noqa: E402
    Remediation, RemediationResult, run_remediation)


class Clock:
    def __init__(self):
        self.t = 1_700_000_000.0

    def __call__(self):
        return self.t


def make_finding(fid="health:hub_fds:20261117"):
    name = fid.split(":")[1] if fid.count(":") >= 2 else fid
    return rn.OpsFinding(
        finding_id=fid, claim="hub fd exhaustion", reversible=True,
        scope="internal", tags=("health", name),
        proposed_action="restart ticket-hub",
        check={"kind": "health", "name": name})


@pytest.fixture
def runner(tmp_path):
    return rn.OpsRunner(
        followups_db=str(tmp_path / "f.db"),
        clock=Clock(),
        probe=lambda port, bearer=None: 200,
        hub_db=str(tmp_path / "hub.db"),
        health_path=str(tmp_path / "no-health.json"),
        crontab_fn=lambda: "",
        state_path=None)


@pytest.fixture
def clean_registry():
    saved = dict(remd.REMEDIATIONS)
    yield remd.REMEDIATIONS
    remd.REMEDIATIONS.clear()
    remd.REMEDIATIONS.update(saved)


# ---------------------------------------------------------------------------
# (a) proved path: symptom fails -> fix runs -> symptom passes -> proved
# ---------------------------------------------------------------------------

def test_proved_path_closes_finding(runner, clean_registry, tmp_path):
    flag = tmp_path / "board-ok.flag"

    def symptom():
        ok = flag.exists()
        return (ok, f"/board HTTP {200 if ok else 500}")

    rem = Remediation(id="test-fix", finding_match="test-fix",
                      symptom_check=symptom, fix_cmd=f"touch {flag}")
    clean_registry["test-fix"] = rem
    finding = make_finding("health:test-fix:20261117")

    result = run_remediation(rem, finding)   # real shell fix executor
    assert result.proved is True
    assert result.before["ok"] is False
    assert result.after["ok"] is True
    assert result.attempts == 1

    out = runner.handle(finding)
    assert out["state"] == "closed"
    assert out["proved"] is True
    assert runner.flow.state_of(finding.finding_id) == "closed"
    # the BEFORE/AFTER evidence is recorded on the verify transition
    hist = runner.flow.history(finding.finding_id)
    verify = [h for h in hist if h[1] == "verified"]
    assert verify and json.loads(verify[0][4])["ok"] is True


# ---------------------------------------------------------------------------
# (b) UNPROVED path: symptom still failing after the fix -> finding OPEN
# ---------------------------------------------------------------------------

def test_unproved_path_escalates_with_both_outputs(runner, clean_registry):
    state = {"ok": False}

    def symptom():
        return (state["ok"], "/board HTTP 500" if not state["ok"]
                else "/board HTTP 200")

    rem = Remediation(id="bad-fix", finding_match="bad-fix",
                      symptom_check=symptom, fix_cmd="true")
    clean_registry["bad-fix"] = rem
    finding = make_finding("health:bad-fix:20261117")

    def fix_executor(r):          # fix "runs" but changes nothing
        return {"exit": 0, "output_tail": "did nothing"}

    result = run_remediation(rem, finding, fix_executor=fix_executor)
    assert result.proved is False
    assert "BEFORE" in result.escalation and "AFTER" in result.escalation
    assert "/board HTTP 500" in result.escalation   # both outputs attached

    out = runner.handle(finding)
    assert out["state"] == "escalated"
    assert out["proved"] is False
    assert runner.flow.state_of(finding.finding_id) == "escalated"  # stays OPEN


# ---------------------------------------------------------------------------
# (c) MUTATION TEST: the verification gate must actually gate.
# A no-op fix must NOT close the finding; a mutant that skips the AFTER
# probe (closes on fix exit 0 alone) would wrongly close it — shown here
# so the gate's absence is detectable.
# ---------------------------------------------------------------------------

def test_mutation_verification_gate_blocks_noop_fix(runner, clean_registry):
    def symptom():
        return (False, "/board HTTP 500")   # symptom NEVER clears

    rem = Remediation(id="noop-fix", finding_match="noop-fix",
                      symptom_check=symptom, fix_cmd="true")   # no-op fix
    clean_registry["noop-fix"] = rem
    finding = make_finding("health:noop-fix:20261117")

    def noop_executor(r):
        return {"exit": 0, "output_tail": "no-op"}

    # the REAL contract: unproved, finding stays open
    real = run_remediation(rem, finding, fix_executor=noop_executor)
    assert real.proved is False

    # the MUTANT: skips the AFTER probe, "proves" on fix exit code alone —
    # it would wrongly close the finding. Assert that, so if anyone removes
    # the gate from run_remediation this test documents what breaks.
    def mutant(rem_, finding_=None):
        before = {"ok": False, "detail": "/board HTTP 500"}
        fix = noop_executor(rem_)
        if fix["exit"] == 0:        # NO re-probe — the removed gate
            return RemediationResult(
                id=rem_.id, before=before, after=before, proved=True,
                attempts=1, output_tail=fix["output_tail"],
                fix_exit=fix["exit"])
        return RemediationResult(
            id=rem_.id, before=before, after=before, proved=False,
            attempts=1, output_tail=fix["output_tail"])

    mutated = mutant(rem, finding)
    assert mutated.proved is True   # the mutant would wrongly close it

    out = runner.handle(finding)
    assert out["state"] == "escalated"     # the real gate refuses to close
    assert runner.flow.state_of(finding.finding_id) == "escalated"
    assert out["result"]["proved"] is False


# ---------------------------------------------------------------------------
# (d) requires_human: prepared block produced, fix NOT executed
# ---------------------------------------------------------------------------

def test_requires_human_prepares_and_does_not_execute(runner, clean_registry):
    state = {"ok": False}
    executed = []

    def symptom():
        return (state["ok"], "/board HTTP 500" if not state["ok"]
                else "/board HTTP 200")

    rem = Remediation(id="human-fix", finding_match="human-fix",
                      symptom_check=symptom, fix_cmd="echo SHOULD-NOT-RUN",
                      requires_human=True)
    clean_registry["human-fix"] = rem
    finding = make_finding("health:human-fix:20261117")

    def fix_executor(r):           # would run if the contract were broken
        executed.append(1)
        return {"exit": 0, "output_tail": "ran"}

    result = run_remediation(rem, finding, fix_executor=fix_executor)
    assert executed == []                       # fix NOT executed
    assert result.proved is False
    assert result.prepared is not None
    assert "echo SHOULD-NOT-RUN" in result.prepared   # copy-paste command
    assert "HUMAN ACTION REQUIRED" in result.prepared
    assert result.before["ok"] is False

    out = runner.handle(finding)
    assert out["state"] == "escalated"          # finding stays OPEN
    assert out["prepared"] and "echo SHOULD-NOT-RUN" in out["prepared"]

    # NEXT poll: the human ran the fix -> the probe (BEFORE) is ok -> proved.
    state["ok"] = True
    out2 = run_remediation(rem, finding, fix_executor=fix_executor)
    assert executed == []                       # still never executed
    assert out2.proved is True


# ---------------------------------------------------------------------------
# (e) the hub-fd remediation's symptom_check vs a 500ing /board
# ---------------------------------------------------------------------------

def test_hub_fd_symptom_check_false_on_500(monkeypatch):
    class Fake500:
        code = 500
        reason = "Internal Server Error"

    def fake_urlopen(req, timeout=None):
        raise remd.urllib.error.HTTPError(req.full_url, 500,
                                          "Internal Server Error", None, None)

    monkeypatch.setattr(remd.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(remd.os.path, "exists", lambda p: False)
    ok, detail = remd.hub_board_check(token_path="/nonexistent")
    assert ok is False
    assert "500" in detail


def test_hub_fd_symptom_check_true_on_200(monkeypatch):
    import io

    class Fake200:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"columns": {}}'

    monkeypatch.setattr(remd.urllib.request, "urlopen",
                        lambda req, timeout=None: Fake200())
    monkeypatch.setattr(remd.os.path, "exists", lambda p: False)
    ok, detail = remd.hub_board_check(token_path="/nonexistent")
    assert ok is True


def test_hub_fd_remediation_registered_and_ray_gated():
    rem = remd.REMEDIATIONS["hub-fd-exhaustion"]
    assert rem.requires_human is True
    assert "docker restart ticket-hub" in rem.fix_cmd
    assert rem.symptom_check is remd.hub_board_check
    assert rem.verify_check is remd.hub_board_check   # same probe re-run


def test_find_remediation_matches_check_name_and_prefix(clean_registry):
    rem = remd.REMEDIATIONS["hub-fd-exhaustion"]
    assert remd.find_remediation(make_finding("health:hub_fds:20261117")) is rem
    assert remd.find_remediation(make_finding("hub-fd-exhaustion:extra")) is rem
    assert remd.find_remediation(make_finding("health:unrelated:20261117")) is None


def test_findings_without_remediation_use_safe_fix_path(runner):
    """A finding with no registered remediation still goes through the
    generic (flow/mandate/fix-session) path."""
    pings = []

    class G:
        def __call__(self, cls, finding):
            pings.append(cls)
            return {"routed": "unrouted"}

    runner.notify = G()
    runner.flow.notify = runner.notify
    finding = rn.OpsFinding(
        finding_id="seat:kimi:20261117", claim="kimi down", reversible=True,
        scope="internal", tags=("seat-liveness", "kimi"),
        proposed_action="restart kimi",
        check={"kind": "seat", "name": "kimi", "port": 18102})
    fixes = []
    runner.fix_executor = lambda f, m: fixes.append(1) or {"exit": 0}
    out = runner.handle(finding)
    assert "remediation" not in out
    assert runner.flow.state_of(finding.finding_id) in ("closed", "escalated")
