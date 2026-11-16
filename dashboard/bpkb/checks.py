"""Compliance checks: measure Aerial's practices against external best practices.

Each check is a function(state: dict) -> (status, evidence) where status is
'compliant' | 'gap' | 'na'. `state` is a plain dict gathered by poll.gather_state
— hermetic by construction: no check touches the filesystem itself, so tests
inject state directly. The default checks cover the decision points whose
authoritative rules are known locally (D1, D2, D3).
"""
from __future__ import annotations

CHECKS: dict[str, callable] = {}


def check(name):
    def deco(fn):
        CHECKS[name] = fn
        return fn
    return deco


@check("bp.failure.classified")
def failure_classified(state):
    """SRE/OpEx: every failure is classified and routed (D1/D3)."""
    if state.get("followup_classifier_present"):
        return "compliant", "followup classifier R1-R6 present"
    return "gap", "no failure classifier: failures would be routed ad hoc"


@check("bp.failure.autorecovery")
def failure_autorecovery(state):
    """WA Reliability: automatic recovery over manual intervention."""
    if state.get("failure_flow_state_machine"):
        return "compliant", "failure-flow state machine (D3) drives scoped recovery"
    return "gap", "no automated recovery flow"


@check("bp.capacity.demand_model")
def capacity_demand(state):
    """WA Reliability/OpEx: stop guessing capacity — use measured demand (D2)."""
    if state.get("vram_demand_load"):
        return "compliant", "demand-load VRAM manager (vram-ctl) owns placement"
    return "gap", "capacity decided statically, not by demand"


@check("bp.fault.no_handstop")
def no_handstop(state):
    """D2 rule: a failed demand-load escalates; services are never hand-stopped."""
    if state.get("handstop_forbidden"):
        return "compliant", "hand-stop forbidden; capacity signals escalate"
    return "gap", "manual service stops possible"


@check("bp.change.auditable")
def change_auditable(state):
    """WA OpEx: operations as code, auditable changes."""
    if state.get("audit_log"):
        return "compliant", f"audit trail: {state.get('audit_log')}"
    return "gap", "no audit trail for state transitions"


@check("bp.security.least_privilege")
def least_privilege(state):
    """WA Security: least privilege — separate accounts, no credential handling."""
    if state.get("separate_accounts") and state.get("no_credential_handling"):
        return "compliant", "agent account isolated; no direct credential access"
    return "gap", "privilege separation or credential policy missing"


@check("bp.security.injection_defense")
def injection_defense(state):
    """OWASP/NIST for LLM agents: prompt-injection defense on untrusted input."""
    if state.get("injection_defense"):
        return "compliant", f"injection defense: {state.get('injection_defense')}"
    return "gap", "no explicit prompt-injection defense on untrusted input"


@check("bp.reliability.no_spof")
def no_spof(state):
    """WA Reliability: no single point of failure — a degraded component must
    not take the platform down (GLM currently depends on one host)."""
    if state.get("failover_seat"):
        return "compliant", f"failover available: {state.get('failover_seat')}"
    return "gap", "single-host dependency with no documented failover seat"


@check("bp.postmortem.written")
def postmortem_written(state):
    """SRE: every incident produces a written, blameless postmortem finding."""
    if state.get("postmortems"):
        return "compliant", f"postmortem artifact: {state.get('postmortems')}"
    return "gap", "failures classified but no written postmortem per incident"


@check("bp.sre.slo")
def slo_defined(state):
    """SRE: SLOs and error budgets defined before taking reliability risks."""
    if state.get("slos"):
        return "compliant", f"SLOs: {state.get('slos')}"
    return "na", "no SLOs defined for agent-ops surfaces yet"


@check("bp.alert.symptoms")
def alert_symptoms(state):
    """SRE: alert on symptoms, not causes — signals reach the operator."""
    if state.get("daily_report"):
        return "compliant", f"operator signal: {state.get('daily_report')}"
    return "gap", "no operator-facing signal surface"


@check("bp.cost.budgeted")
def cost_budgeted(state):
    """WA Cost: budgets and right-sizing on every model call."""
    if state.get("token_budgets"):
        return "compliant", "per-call token budgets enforced"
    return "gap", "model calls unbudgeted"


def run_check(check_id: str, state: dict) -> tuple[str, str]:
    fn = CHECKS.get(check_id)
    if fn is None:
        return "na", f"no check registered for {check_id}"
    return fn(state)
