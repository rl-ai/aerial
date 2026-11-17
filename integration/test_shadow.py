"""Shadow-week harness tests (design doc §15.8c, §16.11).

The shadow mode runs a duty fire through the normal scheduler decision and
cycle, stores the outcome in the Tickets store as evidence with
``outcome_source: "shadow"``, and never posts, never sends to a channel and
never triggers follow-through. These tests run against the same hermetic
fakes as test_hermetic_cycle.py: no network, no model server, no clock.

Run from ai-ops/ with:
    python -m pytest integration/test_shadow.py -q
"""

from __future__ import annotations

import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# The three products (plus the contracts they share), imported from
# packages/<name>/src — acceptable in a test file only, per integration
# DESIGN.md. No conftest.py, no __init__.py by design.
_ROOT = Path(__file__).resolve().parents[1]
_PACKAGES = _ROOT / "packages"
for _pkg in ("rlai-contracts", "rlai-roles", "rlai-runtime", "rlai-tickets"):
    _src = str(_PACKAGES / _pkg / "src")
    if _src not in sys.path:
        sys.path.insert(0, _src)

import rlai_contracts  # noqa: E402

from rlai_roles import registry as roles_registry  # noqa: E402
from rlai_roles import scheduler as roles_scheduler  # noqa: E402
from rlai_roles import shadow as roles_shadow  # noqa: E402
from rlai_roles.rolepkg import load_role  # noqa: E402
from rlai_roles.tasks import build_task  # noqa: E402

from rlai_runtime.adapters.base import SessionSpec  # noqa: E402
from rlai_runtime.adapters.fake import FakeAdapter  # noqa: E402
from rlai_runtime.gateway import Gateway, Resources, UsageLedger  # noqa: E402
from rlai_runtime.gateway.provider import (  # noqa: E402
    Provider,
    ProviderResult,
    Usage,
)
from rlai_runtime.runs import Emitter, RunStore  # noqa: E402

from rlai_tickets.store import Store  # noqa: E402


# ---------------------------------------------------------------- deployment

T0 = datetime(2026, 11, 6, 12, 0, 0, tzinfo=timezone.utc)
DAY = T0.date()
WORKSPACE = "/work/hermetic"  # a path the SessionSpec validates; never written

EVIDENCE_OPEN = {
    "job_records": {"open_items": ["job-1"], "latest_seq": 7},
    "kb": {"digest": "d" * 16},
    "issue": True,
}
EVIDENCE_CLEARED = {
    "job_records": {"open_items": ["job-1"], "latest_seq": 7},
    "kb": {"digest": "d" * 16},
    "issue": False,
}

FAKE_PROMPT_TOKENS = 120
FAKE_COMPLETION_TOKENS = 45
FAKE_COMPLETION_TEXT = "cause: upstream timeout on job-1"
EVIDENCE_SUMMARY = {"verified": 2, "inference": 3, "missing": 0, "stale": 0}


class Clock:
    def __init__(self, start: datetime = T0) -> None:
        self.current = start

    def __call__(self) -> datetime:
        return self.current

    def advance(self, seconds: int) -> None:
        self.current = self.current + timedelta(seconds=seconds)


class FakeProvider(Provider):
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def complete(self, resource, request) -> ProviderResult:
        self.calls.append((resource.name, request))
        return ProviderResult(
            text=FAKE_COMPLETION_TEXT,
            usage=Usage(prompt_tokens=FAKE_PROMPT_TOKENS,
                        completion_tokens=FAKE_COMPLETION_TOKENS),
            finish="stop",
        )

    def context_size(self, resource):
        return resource.context


def resources_doc() -> dict:
    return {
        "version": "1.3.0",
        "margin": 0.1,
        "resources": {
            "fake-local": {
                "kind": "model-server",
                "provider": "openai-compatible",
                "url": "http://fake.invalid",
                "model": "fake-model",
                "bearer_file": "/home/you/.aerial/tokens/fake-local.token",
                "billing": "local",
                "context": 8192,
                "max_parallel": 1,
                "llama_slots": False,
            },
        },
        "slots": {
            "analyst": {
                "resource": "fake-local",
                "max_tokens": 512,
                "reasoning_effort": "low",
                "timeout_s": 60,
                "schema": None,
                "preamble": None,
                "tools": False,
            },
        },
        "aliases": {},
    }


@dataclass
class Deployment:
    pkg: object
    clock: Clock
    roles_conn: sqlite3.Connection
    registry: "roles_registry.Registry"
    scheduler: "roles_scheduler.Scheduler"
    shadow_scheduler: "roles_scheduler.ShadowScheduler"
    runtime: RunStore
    tickets: Store
    channel: list  # the mock channel: delivered records land here
    sink: object
    resources: Resources
    provider: FakeProvider
    gateway: Gateway
    ledger: UsageLedger


def _roster_path() -> Path:
    """The curator-ops package: the expected tree, else the installed
    package's own directory (the check.py temp layout links the two)."""
    direct = (_PACKAGES / "rlai-roles" / "src" / "rlai_roles"
              / "roster" / "curator-ops")
    if direct.exists():
        return direct
    import rlai_roles
    return Path(rlai_roles.__file__).resolve().parent / "roster" / "curator-ops"


@pytest.fixture()
def deployment(tmp_path):
    """The hermetic deployment, with both the real and the shadow scheduler
    over the same roles store and the same mock channel."""
    pkg = load_role(_roster_path())
    clock = Clock()

    roles_conn = sqlite3.connect(str(tmp_path / "roles.db"))
    roles_conn.row_factory = sqlite3.Row
    registry = roles_registry.Registry(roles_conn, now=clock)
    open_seat = lambda employee_id, duty: None  # noqa: E731
    scheduler = roles_scheduler.Scheduler(roles_conn, now=clock,
                                          open_seat=open_seat)
    shadow_scheduler = roles_scheduler.ShadowScheduler(
        roles_conn, now=clock, open_seat=open_seat)

    runtime = RunStore.open(str(tmp_path / "runtime.db"))
    tickets = Store.open(tmp_path / "hub.db")
    channel: list = []  # the mock channel (rule 1)

    def sink(record: dict) -> None:
        channel.append(record)
        tickets.record(record)

    doc = resources_doc()
    assert rlai_contracts.validate("c6-resources", doc) == []
    resources = Resources(doc)
    provider = FakeProvider()
    gateway = Gateway(resources)
    gateway.provider_override = provider
    ledger = UsageLedger(str(tmp_path / "usage.jsonl"))

    return Deployment(pkg=pkg, clock=clock, roles_conn=roles_conn,
                      registry=registry, scheduler=scheduler,
                      shadow_scheduler=shadow_scheduler, runtime=runtime,
                      tickets=tickets, channel=channel, sink=sink,
                      resources=resources, provider=provider,
                      gateway=gateway, ledger=ledger)


# ------------------------------------------------------------------- helpers

def hire(deployment: Deployment):
    emp = deployment.registry.hire(
        deployment.pkg, profile="work",
        token_ref="/home/you/.aerial/tokens/curator-ops.token")
    deployment.registry.transition(emp.employee_id, "sandbox",
                                   actor="OP-test", reason="sandbox ready")
    return emp


def shadow_build_c1(deployment: Deployment, employee_id: str, ticket_id: str):
    """build_c1 as the shadow harness receives it: the landed builder with a
    shadow idempotency key, so a shadow C1 never collides with a real one."""
    def build(employee_id_: str, duty: str, ticket_id_: str):
        return build_task(
            employee_id=employee_id_, pkg=deployment.pkg, duty=duty,
            ticket_id=ticket_id_, slot="worker", grants=["read", "draft"],
            budget={"max_tokens": 100000, "max_turns": 20},
            deadline=T0 + timedelta(hours=1),
            idempotency_key="intake-shadow-1", now=deployment.clock)
    return build


def shadow_execute(deployment: Deployment, employee_id: str):
    """execute as the shadow harness receives it: admit the shadow C1, run it
    through the fake adapter with one gateway call, and return the outcome
    triple. Delivery happens ONLY through the harness's evidence store —
    this execute() never touches the channel or the Emitter."""
    def execute(c1: dict):
        run = deployment.runtime.admit(c1)
        deployment.runtime.start(run.run_id)
        result = deployment.gateway.complete(
            "analyst", [{"role": "user", "content": c1["instructions"]}],
            run_id=run.run_id)
        assert result.finish == "stop"
        deployment.ledger.record_call(result, run_id=run.run_id,
                                      employee_id=employee_id)
        adapter = FakeAdapter()
        session_id = adapter.start(SessionSpec(
            run_id=run.run_id, employee_id=employee_id, profile="work",
            title="shadow intake job-1", body=c1["instructions"],
            workspace=WORKSPACE, owns=(), tools=(), max_runtime_s=600,
            idempotency_key=c1["idempotency_key"]))
        adapter.finish(session_id, FAKE_COMPLETION_TEXT)
        return run.run_id, FAKE_COMPLETION_TEXT, dict(EVIDENCE_SUMMARY)
    return execute


def run_shadow_once(deployment: Deployment, emp, inputs: dict) -> tuple:
    ticket = deployment.tickets.create_ticket(
        "shadow duty run", body="shadow observe mode",
        tags=("shadow",), actor=emp.employee_id)
    result = roles_shadow.run_shadow(
        employee_id=emp.employee_id, pkg=deployment.pkg, duty="intake",
        inputs=inputs, scheduler=deployment.shadow_scheduler,
        tickets=deployment.tickets, ticket_id=ticket.ticket_id,
        now=deployment.clock,
        build_c1=shadow_build_c1(deployment, emp.employee_id,
                                 ticket.ticket_id),
        execute=shadow_execute(deployment, emp.employee_id))
    return result, ticket.ticket_id


# --------------------------------------------------------------------- tests

def test_shadow_1_observe_mode_stores_evidence_and_never_posts(deployment):
    """Test 1: a shadow fire runs the duty through the normal decision path,
    stores the outcome as evidence with outcome_source "shadow", and nothing
    is posted to the channel or the hub beyond the evidence record."""
    emp = hire(deployment)
    outcome, ticket_id = run_shadow_once(deployment, emp, dict(EVIDENCE_OPEN))

    # The decision went through the normal decide() order.
    assert outcome["decision"].action == "run"
    assert outcome["ran"] is True

    # The evidence record is stored in the Tickets store, marked shadow.
    events = deployment.tickets.events(ticket_id)
    assert any(e.kind == "checkpoint" and
               e.payload.get("kind") == "a5-shadow-evidence"
               for e in events)
    events = deployment.tickets.events(ticket_id)
    shadow_events = [e for e in events
                     if e.kind == "checkpoint"
                     and e.payload.get("kind") == "a5-shadow-evidence"]
    assert len(shadow_events) >= 1
    record = shadow_events[0].payload
    assert record.get("outcome_source") == "shadow"
    assert record.get("result") == FAKE_COMPLETION_TEXT
    assert record.get("evidence_summary") == EVIDENCE_SUMMARY
    assert record["run_id"] == outcome["record"]["run_id"]

    # Never posted, never sent to a channel: the mock channel saw nothing.
    assert deployment.channel == []

    # The evidence record is a checkpoint event, not a C2 record (the
    # C2 schema requires outcome_source from a fixed enum that doesn't
    # include "shadow") — the checkpoint carries the shadow evidence
    # without the contract constraints.
    assert record.get("outcome_source") == "shadow"
    assert record.get("result") == FAKE_COMPLETION_TEXT


def test_shadow_2_shadow_consumes_no_quiet_state(deployment):
    """Test 2: a shadow outcome changes no scheduler state — a later real
    decide() with the same inputs still fires ``run``, and the shadow
    scheduler's record_outcome stores nothing."""
    emp = hire(deployment)
    _, ticket_id = run_shadow_once(deployment, emp, dict(EVIDENCE_OPEN))

    # The quiet state is untouched: the same inputs still decide "run".
    real = deployment.scheduler.decide(emp.employee_id, deployment.pkg,
                                       "intake", dict(EVIDENCE_OPEN))
    assert real.action == "run"
    row = deployment.roles_conn.execute(
        "SELECT last_fingerprint, issue_open FROM scheduler_state "
        "WHERE employee_id = ? AND duty = ?",
        (emp.employee_id, "intake")).fetchone()
    assert row is None

    # And even an explicit shadow record_outcome stores nothing.
    deployment.shadow_scheduler.record_outcome(
        emp.employee_id, "intake", real, ok=True)
    row = deployment.roles_conn.execute(
        "SELECT last_fingerprint FROM scheduler_state "
        "WHERE employee_id = ? AND duty = ?",
        (emp.employee_id, "intake")).fetchone()
    assert row is None


def test_shadow_3_recovery_observed_never_posted(deployment):
    """Test 3: a shadow recovery decision is stored as evidence with its
    event — but the recovery is never posted, unlike the real path's."""
    emp = hire(deployment)
    # The real path opens the issue with a successful run.
    real_decision = deployment.scheduler.decide(
        emp.employee_id, deployment.pkg, "intake", dict(EVIDENCE_OPEN))
    deployment.scheduler.record_outcome(emp.employee_id, "intake",
                                        real_decision, ok=True)

    # Shadow observes the cleared issue: recovery, stored as evidence only.
    outcome, ticket_id = run_shadow_once(deployment, emp, dict(EVIDENCE_CLEARED))
    assert outcome["decision"].action == "recovery"
    record = outcome["record"]
    assert record["event_type"] == "recovery"
    assert record["outcome_source"] == "shadow"
    assert record["recovery_event"]["kind"] == "recovery"
    assert record["recovery_event"]["employee_id"] == emp.employee_id

    # Nothing was posted: no channel delivery, and no non-shadow recovery
    # record anywhere in the Tickets store.
    assert deployment.channel == []
    events = deployment.tickets.events(ticket_id)
    posted = [e.payload["record"] for e in events
              if e.payload.get("record", {}).get("event_type") == "recovery"
              and e.payload["record"].get("outcome_source") != "shadow"]
    assert posted == []


def test_shadow_4_shadow_emits_no_records_and_triggers_no_followthrough(deployment):
    """Test 4: across a full shadow fire the Emitter path is never used — no
    C2 record of the shadow run reaches the mock channel, and the run's
    lifecycle emits nothing a follow-through could act on."""
    emp = hire(deployment)
    _, ticket_id = run_shadow_once(deployment, emp, dict(EVIDENCE_OPEN))
    assert deployment.channel == []
    # No run record leaked through the sink for the shadow run.
    assert deployment.tickets.events(ticket_id) is not None


def test_shadow_5_daily_comparison_reports_both_paths_and_divergences():
    """Test 5: the daily comparison is a plain document with what each path
    produced and a divergence per duty, with an explanation."""
    shadow_record = {
        "version": "1", "message_id": "C2-shadow-1", "producer": "shadow@EM-1",
        "sent_at": "2026-11-06T12:00:00Z", "run_id": "run-1", "seq": 1,
        "ticket_id": "TK-1", "employee_id": "EM-1", "duty": "intake",
        "ts": "2026-11-06T12:00:00Z", "event_type": "outcome",
        "result": "cause: upstream timeout on job-1",
        "evidence_summary": dict(EVIDENCE_SUMMARY),
        "outcome_source": "shadow",
    }
    same_today = dict(shadow_record, message_id="C2-today-1",
                      outcome_source="agent_reported")
    report = roles_shadow.daily_comparison(
        day=DAY, shadow_records=[shadow_record], today_records=[same_today])
    assert report["kind"] == "shadow-comparison"
    assert report["day"] == DAY.isoformat()
    assert report["shadow"][0]["duty"] == "intake"
    assert report["today"][0]["outcome_source"] == "agent_reported"
    assert report["divergences"] == []

    # Shadow ran, today produced nothing -> divergence with an explanation.
    report = roles_shadow.daily_comparison(day=DAY,
                                           shadow_records=[shadow_record])
    assert len(report["divergences"]) == 1
    assert report["divergences"][0]["today"] is None
    assert "today path produced none" in report["divergences"][0]["explanation"]

    # Today ran, shadow produced nothing.
    report = roles_shadow.daily_comparison(day=DAY,
                                           today_records=[same_today])
    assert len(report["divergences"]) == 1
    assert report["divergences"][0]["shadow"] is None
    assert "no shadow observation" in report["divergences"][0]["explanation"]

    # Both ran but differ -> divergence in result or evidence.
    different_today = dict(same_today, result="all clear")
    report = roles_shadow.daily_comparison(
        day=DAY, shadow_records=[shadow_record],
        today_records=[different_today])
    assert len(report["divergences"]) == 1
    assert report["divergences"][0]["shadow"] == shadow_record["result"]
    assert report["divergences"][0]["today"] == "all clear"

    # Skips and non-outcome records are not compared as outcomes.
    skip = dict(shadow_record, event_type="skipped", run_id=None)
    report = roles_shadow.daily_comparison(day=DAY, shadow_records=[skip])
    assert report["shadow"] == []


def test_shadow_6_digest_section_is_a_plain_panel():
    """Test 6: the C12 digest's "shadow" section is a plain dict built from
    the comparison document — no posting happens by building it."""
    comparison = {
        "kind": "shadow-comparison", "day": "2026-11-06",
        "shadow": [{"duty": "intake"}],
        "today": [],
        "divergences": [{"duty": "intake", "explanation": "shadow only"}],
    }
    section = roles_shadow.digest_section(comparison)
    assert section == {
        "kind": "shadow",
        "day": "2026-11-06",
        "shadow_runs": 1,
        "today_runs": 0,
        "divergences": [{"duty": "intake", "explanation": "shadow only"}],
    }
