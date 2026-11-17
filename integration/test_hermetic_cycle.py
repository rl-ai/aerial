"""A5a hermetic contract cycle (integration DESIGN.md, design doc §15.8a).

One full Ops Analyst duty through the three landed products against fakes
only: Roles hires and schedules the work and builds the C1; Runtime admits
it, runs it on the FakeAdapter with one gateway model call, and emits C2
records; Tickets ingests the records. No network, no model server, no clock:
time is injected everywhere the products accept a ``now``, every model call
goes to a fake provider, every database lives under tmp_path, and no
credential-shaped literal appears (a bearer_file here is a plain path).

Run from ai-ops/ with:
    python -m pytest integration -q

Gaps this file found in the landed code, stated rather than papered over:

* (Resolved 2026-11-10.) A root C1 carries no ``run_id`` or ``seq``: design doc
  5.1 requires them on C2 always and on C3 inside a run, and Runtime mints
  the run id on admit. Test 2 checks that rule; the 15.8a list was drift.
* (Resolved 2026-11-10.) ``RunStore.finish`` and ``RunStore.runs_for`` now
  exist (TK-EXAMPLE-3); tests 4 and 8 use them.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# --------------------------------------------------------------------------
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
from rlai_runtime.runs.ids import new_message_id  # noqa: E402

from rlai_tickets.store import Store  # noqa: E402


# ---------------------------------------------------------------- deployment

T0 = datetime(2026, 11, 6, 12, 0, 0, tzinfo=timezone.utc)
WORKSPACE = "/work/hermetic"  # a path the SessionSpec validates; never written

# Fixed evidence inputs for the intake duty. "issue" is deliberately not a
# fingerprint key of that duty: an issue opens with a successful run and
# clears (-> recovery) by flipping this flag alone.
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

# The fake provider's fixed completion (rule 1: a fixed completion with usage).
FAKE_PROMPT_TOKENS = 120
FAKE_COMPLETION_TOKENS = 45
FAKE_COMPLETION_TEXT = "cause: upstream timeout on job-1"

EVIDENCE_SUMMARY = {"verified": 2, "inference": 3, "missing": 0, "stale": 0}


class Clock:
    """A now() returning a timezone-aware UTC datetime, tickable via advance.

    (The same fake-at-the-top pattern as the roles scheduler tests.)"""

    def __init__(self, start: datetime = T0) -> None:
        self.current = start

    def __call__(self) -> datetime:
        return self.current

    def advance(self, seconds: int) -> None:
        self.current = self.current + timedelta(seconds=seconds)


class FakeProvider(Provider):
    """A provider that returns one fixed completion with usage; no HTTP."""

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
    """The fake C6 deployment doc: one local-billed resource, no rates."""
    return {
        "version": "1.3.0",
        "margin": 0.1,
        "resources": {
            "fake-local": {
                "kind": "model-server",
                "provider": "openai-compatible",
                "url": "http://fake.invalid",
                "model": "fake-model",
                # a plain path reference, never a credential (rule 1).
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
    """Everything the cycle runs against: three dbs, resources, fakes."""

    pkg: object
    clock: Clock
    roles_conn: sqlite3.Connection
    registry: "roles_registry.Registry"
    scheduler: "roles_scheduler.Scheduler"
    runtime_path: str
    runtime: RunStore
    tickets: Store
    channel: list  # the mock channel: a plain list of delivered records
    sink: object  # the Emitter transport (mock channel -> Tickets.record)
    resources: Resources
    provider: FakeProvider
    gateway: Gateway
    ledger: UsageLedger


@pytest.fixture()
def deployment(tmp_path):
    """Rule 1's deployment, freshly built for one test.

    The C2 sink is ``Tickets.record`` behind the mock channel: every record
    lands on the channel list and in Tickets through the Emitter's real
    at-least-once delivery path."""
    pkg = load_role(_PACKAGES / "rlai-roles" / "src" / "rlai_roles"
                    / "roster" / "curator-ops")
    clock = Clock()

    roles_conn = sqlite3.connect(str(tmp_path / "roles.db"))
    roles_conn.row_factory = sqlite3.Row
    registry = roles_registry.Registry(roles_conn, now=clock)
    scheduler = roles_scheduler.Scheduler(
        roles_conn, now=clock, open_seat=lambda employee_id, duty: None)

    runtime_path = str(tmp_path / "runtime.db")
    runtime = RunStore.open(runtime_path)

    tickets = Store.open(tmp_path / "hub.db")
    channel: list = []  # the mock channel (rule 1): records land in this list

    def sink(record: dict) -> None:
        """The cycle's delivery transport: mock channel, then Tickets.record."""
        channel.append(record)
        tickets.record(record)

    doc = resources_doc()
    problems = rlai_contracts.validate("c6-resources", doc)
    assert problems == [], f"fake resources doc invalid: {problems}"
    res_path = tmp_path / "resources.json"
    res_path.write_text(json.dumps(doc), encoding="utf-8")
    resources = Resources(doc)

    provider = FakeProvider()
    gateway = Gateway(resources)
    gateway.provider_override = provider  # the landed provider seam
    ledger = UsageLedger(str(tmp_path / "usage.jsonl"))

    return Deployment(pkg=pkg, clock=clock, roles_conn=roles_conn,
                      registry=registry, scheduler=scheduler,
                      runtime_path=runtime_path, runtime=runtime,
                      tickets=tickets, channel=channel, sink=sink,
                      resources=resources, provider=provider,
                      gateway=gateway, ledger=ledger)


# ---------------------------------------------------------------- the cycle

def hire(deployment: Deployment):
    """Hire curator-ops; move it to sandbox so it can hold work."""
    emp = deployment.registry.hire(
        deployment.pkg, profile="work",
        token_ref="/home/you/.aerial/tokens/curator-ops.token")
    deployment.registry.transition(emp.employee_id, "sandbox",
                                   actor="OP-test", reason="sandbox ready")
    return emp


def build_intake_c1(deployment: Deployment, employee_id: str, ticket_id: str):
    """The duty's C1, built by the landed build_task with fixed evidence."""
    return build_task(
        employee_id=employee_id, pkg=deployment.pkg, duty="intake",
        ticket_id=ticket_id, slot="worker", grants=["read", "draft"],
        budget={"max_tokens": 100000, "max_turns": 20},
        deadline=T0 + timedelta(hours=1),
        idempotency_key="intake-hermetic-1", now=deployment.clock)


def start_session(deployment: Deployment, run_id: str, employee_id: str,
                  task: dict) -> "tuple[FakeAdapter, str]":
    """Start the run's session on a fresh FakeAdapter (rule 2)."""
    adapter = FakeAdapter()
    session_id = adapter.start(SessionSpec(
        run_id=run_id, employee_id=employee_id, profile="work",
        title="intake job-1", body=task["instructions"],
        workspace=WORKSPACE, owns=(), tools=(), max_runtime_s=600,
        idempotency_key=task["idempotency_key"]))
    return adapter, session_id


def run_cycle(deployment: Deployment) -> dict:
    """Rule 2's full duty: hire, fire the due duty, admit, start on the fake
    adapter, one gateway model call, finish with a result, and send every
    emitted C2 record to Tickets. Returns what the products returned."""
    emp = hire(deployment)
    pkg = deployment.pkg

    due = deployment.scheduler.due(emp.employee_id, pkg)
    assert "intake" in due, f"intake not among the due duties: {due}"
    decision = deployment.scheduler.decide(
        emp.employee_id, pkg, "intake", dict(EVIDENCE_OPEN))
    assert decision.action == "run", decision.reason

    ticket = deployment.tickets.create_ticket(
        "Hermetic cycle: failed job job-1", actor=emp.employee_id)
    task = build_intake_c1(deployment, emp.employee_id, ticket.ticket_id)

    run = deployment.runtime.admit(task)
    assert run.state == "queued"
    assert deployment.runtime.start(run.run_id).state == "running"

    emitter = Emitter(deployment.runtime, "runtime@hermetic",
                      deployment.sink)
    records = [emitter.emit(run.run_id, "run_started")]

    # The run's one model call, through the governed gateway to the fake.
    result = deployment.gateway.complete(
        "analyst", [{"role": "user", "content": task["instructions"]}],
        run_id=run.run_id)
    assert result.finish == "stop"
    deployment.ledger.record_call(result, run_id=run.run_id,
                                  employee_id=emp.employee_id)
    records.append(emitter.emit(
        run.run_id, "tool_call", action_class="read", decision="proceed",
        measured_usage={"tokens_in": result.usage.prompt_tokens,
                        "tokens_out": result.usage.completion_tokens,
                        "wall_ms": round(result.elapsed_s * 1000.0, 3)}))

    # The session runs on the FakeAdapter and finishes with a result.
    adapter, session_id = start_session(deployment, run.run_id,
                                        emp.employee_id, task)
    assert adapter.status(session_id).state == "running"
    adapter.finish(session_id, FAKE_COMPLETION_TEXT)

    # The outcome record: agent_reported with an evidence summary (rule 2).
    records.append(emitter.emit(
        run.run_id, "outcome", result="resolved", elapsed_s=1.0,
        evidence_summary=dict(EVIDENCE_SUMMARY)))

    deployment.scheduler.record_outcome(emp.employee_id, "intake", decision,
                                        ok=True)
    return {"employee": emp, "ticket": ticket, "task": task, "run": run,
            "adapter": adapter, "session_id": session_id, "records": records,
            "gateway_result": result}


# ------------------------------------------------------------------- tests

def test_hermetic_1_scheduler_fire_skip_and_recovery(deployment):
    """Test 1: The scheduler fires, a second fire with the same inputs skips
    and builds no C1, and a recovery decision appears when the issue clears."""
    emp = hire(deployment)
    pkg = deployment.pkg

    due = deployment.scheduler.due(emp.employee_id, pkg)
    assert "intake" in due
    d1 = deployment.scheduler.decide(emp.employee_id, pkg, "intake",
                                     dict(EVIDENCE_OPEN))
    assert d1.action == "run"
    # The fire builds its C1 through the landed builder.
    ticket = deployment.tickets.create_ticket("Cycle", actor=emp.employee_id)
    task1 = build_intake_c1(deployment, emp.employee_id, ticket.ticket_id)
    assert task1["idempotency_key"] == "intake-hermetic-1"
    deployment.scheduler.record_outcome(emp.employee_id, "intake", d1,
                                        ok=True)

    # A second fire with the same inputs: skip-no-change. The dispatch step
    # runs only on a run decision, so the skip builds no C1.
    built: list = []
    d2 = deployment.scheduler.decide(emp.employee_id, pkg, "intake",
                                     dict(EVIDENCE_OPEN))
    assert d2.action == "skip-no-change"
    if d2.action == "run":  # dispatch policy: only a run decision builds a C1
        built.append(build_intake_c1(deployment, emp.employee_id,
                                     ticket.ticket_id))
    assert built == []

    # When the issue clears, the same inputs yield a recovery decision.
    d3 = deployment.scheduler.decide(emp.employee_id, pkg, "intake",
                                     dict(EVIDENCE_CLEARED))
    assert d3.action == "recovery"
    assert d3.event is not None and d3.event["kind"] == "recovery"
    assert d3.event["employee_id"] == emp.employee_id


def test_hermetic_2_c1_carries_the_envelope_and_validates(deployment):
    """Test 2: The C1 task carries the envelope fields message_id, producer,
    sent_at and idempotency_key, and validates as C1. A root task has no
    run_id or seq: design doc 5.1 requires run_id on C2 always and on C3
    inside a run, and seq only where run_id is present. Runtime mints the
    run id on admit (Claude review, 2026-11-10: the 15.8a list was drift)."""
    emp = hire(deployment)
    ticket = deployment.tickets.create_ticket("Envelope check",
                                              actor=emp.employee_id)
    task = build_intake_c1(deployment, emp.employee_id, ticket.ticket_id)

    for field in ("message_id", "producer", "sent_at", "idempotency_key"):
        assert task.get(field), f"C1 envelope field {field} missing"
    assert "run_id" not in task and "seq" not in task, (
        "a root C1 carries no run_id or seq (design doc 5.1)")

    problems = rlai_contracts.validate("c1-task", task)
    assert problems == [], f"C1 does not validate as c1-task: {problems}"


def test_hermetic_3_admit_and_idempotent_replay(deployment):
    """Test 3: Runtime admits the C1, and a second delivery with the same
    idempotency_key returns the existing run, not a new one."""
    emp = hire(deployment)
    ticket = deployment.tickets.create_ticket("Admit check",
                                              actor=emp.employee_id)
    task = build_intake_c1(deployment, emp.employee_id, ticket.ticket_id)

    run = deployment.runtime.admit(task)
    assert run.state == "queued"
    assert run.employee_id == emp.employee_id
    assert run.idempotency_key == task["idempotency_key"]

    # A redelivery with a fresh message_id but the same idempotency_key.
    replay = dict(task, message_id=new_message_id())
    assert replay["message_id"] != run.message_id
    again = deployment.runtime.admit(replay)
    assert again.run_id == run.run_id
    assert again.message_id == run.message_id  # the stored run, not a new one


def test_hermetic_4_run_finishes_through_the_fake_adapter(deployment):
    """Test 4: The run reaches its finished state through the FakeAdapter."""
    emp = hire(deployment)
    ticket = deployment.tickets.create_ticket("Finish check",
                                              actor=emp.employee_id)
    task = build_intake_c1(deployment, emp.employee_id, ticket.ticket_id)
    run = deployment.runtime.admit(task)
    deployment.runtime.start(run.run_id)

    adapter, session_id = start_session(deployment, run.run_id,
                                        emp.employee_id, task)
    assert adapter.status(session_id).state == "running"
    adapter.finish(session_id, "checked job-1")

    # The finished state, as the adapter returns it.
    status = adapter.status(session_id)
    assert status.state == "done"
    assert status.result == "checked job-1"

    # The join to the store: while the run is live, the store accepts the
    # session's result, then RunStore.finish moves the stored run to done.
    assert deployment.runtime.accept_result(
        run.run_id, task["message_id"], {"result": "checked job-1"}) is True
    finished = deployment.runtime.finish(run.run_id)
    assert finished.state == "done"
    assert deployment.runtime.get_run(run.run_id).state == "done"
    # a late result after the end is discarded
    assert deployment.runtime.accept_result(
        run.run_id, task["message_id"], {"result": "late"}) is False


def test_hermetic_5_c2_records_envelope_and_seq(deployment):
    """Test 5: C2 records carry the envelope, their seq runs in order with no
    gap, and each validates as C2."""
    outcome = run_cycle(deployment)
    records = list(outcome["records"])
    assert len(records) == 3

    assert [rec["seq"] for rec in records] == [1, 2, 3]  # in order, no gap
    for index, rec in enumerate(records):
        for field in ("version", "message_id", "producer", "sent_at",
                      "run_id", "seq", "ticket_id", "employee_id", "ts",
                      "event_type"):
            assert field in rec, f"record seq {rec['seq']} lacks {field}"
        assert rec["run_id"] == outcome["run"].run_id
        assert rec["ticket_id"] == outcome["ticket"].ticket_id
        assert rec["employee_id"] == outcome["employee"].employee_id
        if index > 0:  # envelope rule: causation links to the previous record
            assert rec["causation_id"] == records[index - 1]["message_id"]
        problems = rlai_contracts.validate("c2-record", rec)
        assert problems == [], f"C2 seq {rec['seq']} invalid: {problems}"

    # The mock channel saw exactly what the Emitter delivered to Tickets.
    assert [rec["message_id"] for rec in deployment.channel] == \
        [rec["message_id"] for rec in records]


def test_hermetic_6_tickets_stores_each_record_once(deployment):
    """Test 6: Tickets stores each C2 record once: a second delivery of the
    same message_id is acknowledged and not applied twice."""
    outcome = run_cycle(deployment)
    before = deployment.tickets.events(outcome["ticket"].ticket_id)

    replays = [deployment.tickets.record(dict(rec))
               for rec in outcome["records"]]  # redeliver each

    after = deployment.tickets.events(outcome["ticket"].ticket_id)
    assert len(after) == len(before)  # nothing extra applied
    for original, ack in zip(outcome["records"], replays):
        stored = [e for e in after if e.payload.get("record", {}).get(
            "message_id") == original["message_id"]]
        assert len(stored) == 1
        assert ack.event_id == stored[0].event_id  # acknowledged, not doubled


def test_hermetic_7_outcome_recorded_agent_reported(deployment):
    """Test 7: An outcome record with result agent_reported and an evidence
    summary is stored."""
    outcome = run_cycle(deployment)
    events = deployment.tickets.events(outcome["ticket"].ticket_id)
    stored = [e.payload["record"] for e in events
              if e.payload.get("record", {}).get("event_type") == "outcome"]
    assert len(stored) == 1
    record = stored[0]
    assert record["outcome_source"] == "agent_reported"
    assert record["result"] == "resolved"
    assert record["evidence_summary"] == EVIDENCE_SUMMARY


def test_hermetic_8_controller_restart_no_duplicate_run(deployment):
    """Test 8: A simulated controller restart (reopen the Runtime store
    mid-run) makes recover() report the run as pending or uncertain, and
    creates no duplicate run."""
    emp = hire(deployment)
    ticket = deployment.tickets.create_ticket("Restart check",
                                              actor=emp.employee_id)
    task = build_intake_c1(deployment, emp.employee_id, ticket.ticket_id)
    run = deployment.runtime.admit(task)
    deployment.runtime.start(run.run_id)

    # The controller dies; a fresh store opens on the same file (the restart)
    # and recover() runs at startup.
    reopened = RunStore.open(deployment.runtime_path)
    try:
        marked = reopened.recover()
        # Exactly one run is reported, as uncertain (pending-or-uncertain).
        assert [r.run_id for r in marked] == [run.run_id]
        assert reopened.get_run(run.run_id).state == "uncertain"
        # No duplicate run: this employee still has exactly one run.
        assert [r.run_id for r in reopened.runs_for(emp.employee_id)] == [run.run_id]
        assert reopened.get_run(run.run_id).attempt == 1
        again = reopened.recover()
        assert again == []  # nothing else to mark: one run, already resolved
    finally:
        reopened._conn.close()


def test_hermetic_9_usage_recorded_no_provider_charge(deployment):
    """Test 9: Usage is recorded with the call's token counts. The local
    class shows no provider charge (absent, never 0), as the usage summary
    reports it."""
    outcome = run_cycle(deployment)
    result = outcome["gateway_result"]

    lines = [json.loads(line)
             for line in Path(deployment.ledger.path).read_text(
                 encoding="utf-8").splitlines() if line.strip()]
    measured = [e for e in lines if e["class"] == "measured_usage"]
    assert len(measured) == 1
    entry = measured[0]
    assert entry["run_id"] == outcome["run"].run_id
    assert entry["employee_id"] == outcome["employee"].employee_id
    assert entry["resource"] == "fake-local"
    assert entry["data"]["prompt_tokens"] == result.usage.prompt_tokens \
        == FAKE_PROMPT_TOKENS
    assert entry["data"]["completion_tokens"] == \
        result.usage.completion_tokens == FAKE_COMPLETION_TOKENS

    summary = deployment.ledger.summary(employee_id=
                                        outcome["employee"].employee_id)
    assert summary["provider_charge"] is None  # absent, never 0: local class
    assert summary["measured_usage"]["calls"] == 1
    assert "cost" not in json.dumps(summary)
