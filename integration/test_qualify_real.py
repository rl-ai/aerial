"""Tests for the A5 qualification harness, stages 0-2 (integration DESIGN.md).

Fakes only: no network, no hermes or docker binary, no real hub. The token
file is a plain path (/home/you/.aerial/tokens/a5-test-01-read.token) and is never
opened — the fake transport's auth header is a fixed literal, so no test
ever touches a credential value, and no credential-shaped literal appears
in this file or in any evidence the harness writes.

Numbering follows the DESIGN.md test list (1-14); the three dry-run tests
are 1a/1b/1c for stages 0/1/2.

Run from ai-ops/ with:
    python -m pytest integration -q
"""

from __future__ import annotations

import io
import json
import pathlib
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

# qualify_real needs packages/rlai-* (the products live in the private
# monorepo; the public tree ships without them). Skip rather than error.
from pathlib import Path as _Path
if not (_Path(__file__).resolve().parents[1] / "packages").is_dir():
    pytest.skip("needs packages/rlai-* (not shipped in the public tree)",
                allow_module_level=True)

# The three products (plus contracts), imported from packages/<name>/src —
# the integration test-file pattern of DESIGN.md. qualify_real does the same
# at its __main__ boundary; importing it as a module needs them in place.
_ROOT = Path(__file__).resolve().parents[1]
_PACKAGES = _ROOT / "packages"
for _pkg in ("rlai-contracts", "rlai-roles", "rlai-runtime",
             "rlai-tickets", "rlai-metrics"):
    _src = str(_PACKAGES / _pkg / "src")
    if _src not in sys.path:
        sys.path.insert(0, _src)

import qualify_real as qr  # noqa: E402  (same folder; no conftest by design)

from rlai_runtime.gateway import Gateway, Resources  # noqa: E402
from rlai_runtime.gateway.provider import (  # noqa: E402
    Provider,
    ProviderResult,
    Usage,
)
from rlai_runtime.runs import RunStore  # noqa: E402
from rlai_tickets.store import Store  # noqa: E402


T0 = datetime(2026, 11, 10, 12, 0, 0, tzinfo=timezone.utc)

# The token file is a plain path; no test ever opens it.
TOKEN_FILE = "/home/you/.aerial/tokens/a5-test-01-read.token"

# The fake transport's fixed auth header (built from parts so this file
# carries no credential-shaped literal; never a value read from any file).
FAKE_AUTH_HEADER = "Bear" + "er " + "fake-fixture-header"


class FakeTransport:
    """A get/post fake that records calls; status is scripted per path."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.get_status: dict[str, int] = {}   # substring -> status
        # the harness's own loopback mount answers 403 without a token;
        # everything else answers 200 until a test scripts it otherwise.
        self.get_status["/operator/"] = 403
        self.default_get_status = 200
        self.post_status = 201
        self.opened_paths: list[str] = []      # nothing should ever appear

    def _status_for(self, url: str) -> int:
        for needle, status in self.get_status.items():
            if needle in url:
                return status
        return self.default_get_status

    def get(self, url: str, headers=None):
        # The transport proves the token value is never needed: it answers
        # from a fixed header literal and never opens the token file.
        self.calls.append({"method": "GET", "url": url,
                           "auth_header": (headers or {}).get(
                               "authorization")})
        # the hub's /health reports its own flag; stage 0 requires ok true
        body = '{"ok": true}' if url.endswith("/health") else "{}"
        return self._status_for(url), body

    def post(self, url: str, body, headers=None):
        self.calls.append({"method": "POST", "url": url, "body": body,
                           "headers": dict(headers or {})})
        return self.post_status, "{}"


class FakeHermesBin:
    """A hermes/docker side that answers from data; runs nothing."""

    def __init__(self, *, cli=None, docker=None, binary="/usr/bin/hermes",
                 docker_ok=True, board=qr.HERMES_BOARD) -> None:
        self._binary = binary
        self._docker_ok = docker_ok
        self.cli = cli if cli is not None else FakeHermesCli()
        self.docker = docker if docker is not None else FakeDockerCli()
        self.board = board

    def hermes_answer(self):
        return self._binary

    def docker_answer(self) -> bool:
        return self._docker_ok


class FakeHermesCli(qr.HermesCli):
    """Real HermesCli argv building; the subprocess run is scripted."""

    def __init__(self, board="a5-qualification"):
        self.procs: list = []
        self._cards: dict[str, bool] = {}
        self._seq = 0
        super().__init__(board, hermes="/usr/bin/hermes",
                         run=self._run)

    def _run(self, argv, capture_output, text, timeout):
        import subprocess as sp
        joined = " ".join(argv)
        if "create" in joined:
            self._seq += 1
            card_id = f"a5card{self._seq:04d}"
            self._cards[card_id] = True
            out = json.dumps({"id": card_id})
        elif "show" in joined:
            card_id = argv[argv.index("show") + 1]
            if card_id not in self._cards:
                raise RuntimeError("no such card")  # becomes AdapterError
            out = json.dumps({"task": {"id": card_id, "status": "running"},
                              "runs": [], "latest_summary": "echo done"})
        else:
            out = "[]"
        proc = sp.CompletedProcess(argv, 0, stdout=out, stderr="")
        self.procs.append((list(argv), proc))
        return proc


class FakeDockerCli(qr.DockerCli):
    """Real DockerCli argv building; scripted answers, no docker binary."""

    def __init__(self):
        super().__init__(run=self._run)

    def _run(self, argv, capture_output, text, timeout):
        import subprocess as sp
        return sp.CompletedProcess(argv, 0, stdout="", stderr="")


class FakeProvider(Provider):
    """One fixed completion with usage; no HTTP (same shape as A5a).

    With ``blocks`` set, complete() waits until close_tracked (the gateway's
    cancel path) and then raises ConnectionResetError — the same shape a real
    cancel produces — so the stage-3 model-request scenario exercises a
    genuine cancellation through the real gateway."""

    PROMPT_TOKENS = 90
    COMPLETION_TOKENS = 30
    TEXT = "recommendation: rerun a5-job-1 after refreshing its input"

    def __init__(self) -> None:
        import threading
        self.calls: list = []
        self.blocks = False
        self._cancelled = threading.Event()

    def complete(self, resource, request) -> ProviderResult:
        self.calls.append((resource.name, request))
        if self.blocks and self._cancelled.wait(timeout=120):
            raise ConnectionResetError("cancelled")
        return ProviderResult(
            text=self.TEXT,
            usage=Usage(prompt_tokens=self.PROMPT_TOKENS,
                        completion_tokens=self.COMPLETION_TOKENS),
            finish="stop")

    def attach(self, run_id):
        """The gateway's per-call tag seam: a token it can close on cancel."""
        return f"fake-{run_id}"

    def close_tracked(self, token) -> None:
        self._cancelled.set()

    def context_size(self, resource):
        return resource.context


def resources_doc() -> dict:
    """A C6 doc with one local-billed route and a worker slot (no rates)."""
    return {
        "version": "1.3.0",
        "margin": 0.1,
        "resources": {
            "a5-route": {
                "kind": "model-server",
                "provider": "openai-compatible",
                "url": "http://fake.invalid:9099",
                "model": "a5-fake-model",
                "bearer_file": "/home/you/.aerial/tokens/a5-route.token",  # a path
                "billing": "local",
                "context": 8192,
                "max_parallel": 1,
                "llama_slots": False,
            },
        },
        "slots": {
            "worker": {
                "resource": "a5-route",
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


def _open_transport_hub(transport: FakeTransport, monkeypatch) -> None:
    """Prove that opening a token file is not needed: patch open() so any
    read of the token path raises during a harness run."""
    real_open = open

    def guarded(file, *args, **kwargs):
        if "tokens" in str(file):
            raise AssertionError(f"token file opened: {file}")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr("builtins.open", guarded)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """A full fake deployment: scripted transport, faked hermes side, the
    real products over tmp_path databases, and a tagged epic."""
    _open_transport_hub(transport := FakeTransport(), monkeypatch)

    doc = resources_doc()
    resources = Resources(doc)

    hub = qr.Hub("http://hub.fake", get=transport.get, post=transport.post,
                 token_file=TOKEN_FILE,
                 token_provider=lambda path: "fake-fixture-header")
    hermes = FakeHermesBin(cli=FakeHermesCli(), docker=FakeDockerCli())
    models = qr.Models({"a5-route": "http://fake.invalid:9099"},
                       transport.get)

    tickets_hub = Store.open(tmp_path / "hub.db")
    epic = tickets_hub.create_ticket("A5 qualification epic",
                                     body="stage evidence lives here",
                                     tags=(qr.TAG,), actor="operator")
    runtime = RunStore.open(str(tmp_path / "runtime.db"))
    gateway = Gateway(resources)
    provider = FakeProvider()
    gateway.provider_override = provider
    roles_conn = sqlite3.connect(str(tmp_path / "roles.db"))
    roles_conn.row_factory = sqlite3.Row

    dep = qr.Deployment(
        hub=hub, hermes=hermes, models=models, resources=resources,
        tickets_hub=tickets_hub, runtime=runtime, gateway=gateway,
        ledger_path=str(tmp_path / "usage.jsonl"),
        roles_conn=roles_conn, now=qr.Clock(T0),
        epic_ticket_id=epic.ticket_id, model_routes={"worker": "a5-route"},
        runtime_path=str(tmp_path / "runtime.db"),
        sandbox_control=FakeStopSandbox(),
        inference_control=FakeInference(gateway=gateway))
    return dep, transport, epic, provider


def harness(dep, out_dir=None, lines=None):
    sink = lines if lines is not None else []
    return qr.Harness(dep, out_dir, print_fn=sink.append), sink


# ------------------------------------------------- stage 3: stop tests (§6.5)

class FakeStopSandbox:
    """A SandboxControl fake with the harness StopSandbox semantics: a
    queued run has no sandbox (alive False by construction); a started run
    is alive until terminate confirms it dead; anything else is unknown."""

    def __init__(self) -> None:
        self.sessions: dict[str, str] = {}
        self.queued: set[str] = set()
        self.terminated: list[str] = []
        self.dead: set[str] = set()

    def start_session(self, run_id, *, employee_id, profile, title, body,
                      workspace, tools, idempotency_key,
                      max_runtime_s=600):
        self.sessions[run_id] = "sess-" + idempotency_key[-8:]
        return self.sessions[run_id]

    def mark_queued(self, run_id) -> None:
        self.queued.add(run_id)

    def terminate(self, run_id) -> None:
        self.terminated.append(run_id)
        self.dead.add(run_id)

    def alive(self, run_id):
        if run_id in self.queued and run_id not in self.sessions:
            return False
        if run_id in self.dead:
            return False
        if run_id in self.sessions:
            return True
        return None


class FakeInference:
    """An InferenceControl fake: in flight until cancel is called for the
    run. Cancel also cancels the real gateway's tracked call, so the
    stage-3 model-request scenario cancels for real."""

    def __init__(self, gateway=None) -> None:
        self.cancelled_runs: set[str] = set()
        self._gateway = gateway

    def cancel(self, run_id) -> None:
        self.cancelled_runs.add(run_id)
        if self._gateway is not None:
            self._gateway.cancel(run_id)

    def in_flight(self, run_id):
        # The gateway's own tracking is the truth: a fake-True answer before
        # the call is registered made cancel fire into the void (race found
        # by the post-review test run, 2026-11-11).
        if self._gateway is not None:
            return self._gateway.in_flight(run_id)
        return run_id not in self.cancelled_runs


class NeverConfirmsInference:
    """The restart scenario's fault, mirrored from the harness: the check
    never confirms, so the stop times out unverified."""

    def cancel(self, run_id) -> None:
        pass

    def in_flight(self, run_id):
        return True


def _hire(env):
    """Stage 1 live (the hire stage 2 and 3 reuse)."""
    dep, _, _, _ = env
    h, _ = harness(dep)
    result = h.run_stage(1, live=True)
    assert result["verdict"] == "pass", result
    return result


# ------------------------------------------------- 1-3: dry runs plan only

def test_dry_run_stage0_prints_plan_and_calls_nothing(env, tmp_path):
    """DESIGN test 1 (stage 0): the plan prints; nothing is called/written."""
    dep, transport, epic, _ = env
    h, lines = harness(dep, tmp_path / "out")
    result = h.run_stage(0, live=False)

    text = "\n".join(lines)
    assert "plan: GET hub http://hub.fake/health" in text
    assert "/v1/models" in text                      # each route named
    assert "403" in text                             # operator mount check
    assert "docker ps" in text and "hermes" in text  # the CLI checks
    assert transport.calls == []                     # nothing was called
    assert dep.models.calls == []
    assert not (tmp_path / "out").exists()           # nothing was written
    assert result["verdict"] == "unverified"         # a plan proves nothing


def test_dry_run_stage1_prints_plan_and_writes_nothing(env, tmp_path):
    """DESIGN test 2 (stage 1): the hire plan prints; no write happens."""
    dep, transport, epic, _ = env
    h, lines = harness(dep, tmp_path / "out")
    result = h.run_stage(1, live=False)

    text = "\n".join(lines)
    assert "plan:" in text
    assert "role.toml" in text                       # the fixture named
    assert "draft" in text                           # the preset named
    assert TOKEN_FILE in text                        # the reference path
    assert transport.calls == []
    rows = dep.roles_conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE type='table' AND "
        "name='registry_employees'").fetchone()[0]
    assert rows == 0                                 # the db was not created
    assert not (tmp_path / "out").exists()
    assert result["verdict"] == "unverified"


def test_dry_run_stage2_prints_plan_and_writes_nothing(env, tmp_path):
    """DESIGN test 3 (stage 2): the end-to-end plan prints; nothing runs."""
    dep, transport, epic, provider = env
    h, lines = harness(dep, tmp_path / "out")
    result = h.run_stage(2, live=False)

    text = "\n".join(lines)
    assert "plan:" in text
    assert "a5-qualification" in text
    assert "--board a5-qualification" in text        # the kanban argv
    assert provider.calls == []                      # no model call
    assert transport.calls == []
    assert dep.tickets_hub.get(epic.ticket_id).status == "draft"  # untouched
    assert not (tmp_path / "out").exists()
    assert result["verdict"] == "unverified"


# ------------------------------------------------- 4: the evidence shape

def test_live_stage0_writes_the_evidence_file_shape(env, tmp_path):
    """DESIGN test 4: stage-N.json carries stage, verdict, checks+evidence
    and started/ended stamps."""
    dep, _, _, _ = env
    out = tmp_path / "out"
    h, _ = harness(dep, out)
    result = h.run_stage(0, live=True)
    assert result["verdict"] == "pass"

    path = out / "stage-0.json"
    assert path.is_file()
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["stage"] == 0
    assert doc["verdict"] in qr._VERDICTS
    assert doc["verdict"] == "pass"
    names = {c["name"] for c in doc["checks"]}
    assert {"hub-authorized", "hermes-binary", "docker-answers",
            "model-route-a5-route", "operator-mount-403"} <= names
    for check in doc["checks"]:
        assert isinstance(check["evidence"], dict)
    for stamp in ("started", "ended"):
        assert doc[stamp].endswith("Z") and "T" in doc[stamp]


# ------------------------------------------- 5-7: stage 0 failure naming

def test_stage0_failure_names_the_hub(env, tmp_path):
    """DESIGN test 5: an unauthorized hub names the failing check."""
    dep, transport, _, _ = env
    # the authenticated read of the epic is what proves the token works
    transport.get_status["hub.fake/tickets/"] = 401
    h, lines = harness(dep)
    result = h.run_stage(0, live=True)

    failed = {c["name"]: c for c in result["checks"]
              if c["result"] != "pass"}
    assert "hub-authorized" in failed
    assert failed["hub-authorized"]["evidence"]["status"] == 401
    assert result["verdict"] == "fail"               # no partial pass


def test_stage0_failure_names_the_hermes_binary(env):
    """DESIGN test 6: a missing hermes binary names its check."""
    dep, _, _, _ = env
    dep.hermes._binary = None                        # not found on PATH
    h, _ = harness(dep)
    result = h.run_stage(0, live=True)

    failed = {c["name"] for c in result["checks"] if c["result"] != "pass"}
    assert "hermes-binary" in failed
    assert result["verdict"] == "fail"


def test_stage0_failure_names_a_model_route(env):
    """DESIGN test 7: a dead route names which route check failed."""
    dep, transport, _, _ = env
    transport.get_status["fake.invalid:9099/v1/models"] = 503
    h, _ = harness(dep)
    result = h.run_stage(0, live=True)

    failed = {c["name"] for c in result["checks"] if c["result"] != "pass"}
    assert "model-route-a5-route" in failed
    assert result["verdict"] == "fail"


# ------------------------------------------------------ 8: stage 0 passes

def test_stage0_passes_when_everything_answers(env, tmp_path):
    """DESIGN test 8: all four answers green -> verdict pass, all checks."""
    dep, _, _, _ = env
    out = tmp_path / "out"
    h, _ = harness(dep, out)
    result = h.run_stage(0, live=True)

    assert result["verdict"] == "pass"
    assert all(c["result"] == "pass" for c in result["checks"])
    # the operator mount answered 403 without authorization:
    op = next(c for c in result["checks"]
              if c["name"] == "operator-mount-403")
    assert op["evidence"]["status"] == 403
    assert op["evidence"]["authorized"] is False


# ------------------------------------------- 9: the hire uses fixture+draft

def test_stage1_hire_uses_the_fixture_role_and_draft_preset(env, tmp_path):
    """DESIGN test 9: the live hire records the fixture role; the policy is
    built with preset draft."""
    dep, _, epic, _ = env
    out = tmp_path / "out"
    h, _ = harness(dep, out)
    result = h.run_stage(1, live=True)

    assert result["verdict"] == "pass"
    hire = next(c for c in result["checks"] if c["name"] == "hire-recorded")
    assert hire["result"] == "pass"
    assert hire["evidence"]["role_name"] == "a5-test"   # the fixture role
    assert hire["evidence"]["token_ref"] == qr.TOKEN_REF
    row = dep.roles_conn.execute(
        "SELECT * FROM registry_employees").fetchall()
    assert len(row) == 1 and row[0]["role_name"] == "a5-test"

    preset = next(c for c in result["checks"]
                  if c["name"] == "policy-preset-draft")
    assert preset["evidence"]["preset"] == "draft"
    tiers = preset["evidence"]["tiers"]
    assert tiers["read"] == "autonomous"
    assert tiers["internal-write"] == "blocked"


# ------------------------------------------------ 10: no token value read

def test_no_token_value_is_ever_read(env):
    """DESIGN test 10: the hub carries only the path; nothing is opened.

    The env fixture guards builtins.open so any read of a tokens path
    raises; stage 0 and stage 1 live runs pass under that guard. The fake
    transport's auth header is a fixed literal, proving no value is needed.
    """
    dep, transport, _, _ = env
    h, _ = harness(dep)
    assert h.run_stage(0, live=True)["verdict"] == "pass"
    assert h.run_stage(1, live=True)["verdict"] == "pass"

    # the hub object holds a path only, never a value:
    assert dep.hub.token_file == TOKEN_FILE
    assert not hasattr(dep.hub, "_token_value")
    # the POST's auth header is the fixture literal, not a file read:
    posts = [c for c in transport.calls if c["method"] == "POST"]
    assert posts and all(c["headers"].get("authorization")
                         == FAKE_AUTH_HEADER for c in posts)


# --------------------------------------------------- 11: stage 2 checks

def test_stage2_live_runs_the_duty_and_a5a_checks_pass(env):
    """DESIGN test 11: end to end passes the A5a assertions plus the epic
    comment check."""
    dep, _, _, provider = env
    # stage 1 first: stage 2 uses the employee stage 1 hired.
    h, _ = harness(dep)
    assert h.run_stage(1, live=True)["verdict"] == "pass"

    h2, _ = harness(dep)
    result = h2.run_stage(2, live=True)

    by_name = {c["name"]: c for c in result["checks"]}
    expected = ("employee-hired", "epic-tagged", "duty-fires",
                "c1-validates", "dedup-admit", "hermes-session-started",
                "model-call", "c2-emitted", "c2-envelope-and-order",
                "c2-dedup-at-tickets", "outcome-agent-reported",
                "usage-no-provider-charge", "recommendation-comment")
    for name in expected:
        assert by_name[name]["result"] == "pass", (name, by_name[name])
    assert result["verdict"] == "pass"
    # one model call, on the deployed route; usage stayed local-class:
    assert len(provider.calls) == 1
    usage = by_name["usage-no-provider-charge"]["evidence"]
    assert usage["class"] == "local" and usage["provider_charge"] is None


# ------------------------------------------- 12: untagged ticket refused

def test_untagged_ticket_is_refused(env):
    """DESIGN test 12: a ticket without the a5-qualification tag refuses."""
    dep, _, _, _ = env
    plain = dep.tickets_hub.create_ticket("No tag here",
                                          actor="operator")  # no tags
    h, _ = harness(dep)
    with pytest.raises(qr.HarnessError) as info:
        h.require_tagged(plain.ticket_id)
    assert qr.TAG in str(info.value)
    # the tagged epic still passes the same gate:
    assert h.require_tagged(dep.epic_ticket_id).ticket_id \
        == dep.epic_ticket_id


# ---------------------------------------- 13: --live without --stage

def test_live_without_stage_is_refused(env):
    """DESIGN test 13: run_cli refuses --live with no --stage named."""
    dep, _, _, _ = env
    with pytest.raises(qr.HarnessError):
        qr.run_cli(["--live", "--out", "/tmp/unused"], dep=dep)


# ------------------------------------------ 14: no secret-shaped evidence

def test_no_secret_shaped_text_reaches_the_evidence(env, tmp_path):
    """DESIGN test 14: evidence with credential-shaped text is refused and
    the file is never written; clean evidence passes."""
    dep, _, _, _ = env
    out = tmp_path / "out"
    h, _ = harness(dep, out)

    marker = "Bear" + "er " + "abc123"          # built, not a literal run-on
    checks = [{"name": "x", "result": "pass",
               "evidence": {"note": marker}}]
    with pytest.raises(qr.HarnessError):
        qr.scan_for_secrets({"note": marker})   # the guard itself refuses
    h._write_evidence(0, "pass", checks)        # the writer scrubs it out
    doc = json.loads((out / "stage-0.json").read_text(encoding="utf-8"))
    assert doc["verdict"] == "fail"             # never a pass on bad evidence
    assert "Bear" + "er " not in (out / "stage-0.json").read_text(
        encoding="utf-8")

    # a clean live run writes evidence that carries no secret-shaped text:
    result = h.run_stage(0, live=True)
    assert result["verdict"] == "pass"
    text = (out / "stage-0.json").read_text(encoding="utf-8")
    assert "Bear" + "er " not in text
    assert "sk-" not in text



def test_model_probe_sends_the_route_key_and_never_keeps_it(tmp_path):
    """Claude review, 2026-11-10: a route that needs a key gets it from its
    bearer file at call time; the value is not kept on the probe object."""
    key_file = tmp_path / "route.key"
    key_file.write_text("synthetic-route-value")
    seen = {}

    def get(url, headers):
        seen.update(headers)
        return 200, "{}"
    models = qr.Models({"r": "http://fake.invalid:1"}, get, bearer_files={"r": str(key_file)})
    assert models.probe("r") == (200, "{}")
    assert seen.get("Authorization") == "Be" + "arer " + "synthetic-route-value"
    assert "synthetic-route-value" not in json.dumps(models.calls)



def test_stage0_requires_the_hub_health_flag(env):
    """Claude review, 2026-11-10: /health answering 200 with ok false is an
    unhealthy hub, and stage 0 says so."""
    dep, transport, _, _ = env
    h, _ = harness(dep)
    real_get = dep.hub._get  # the Hub holds the transport it was given

    def unhealthy(url, headers=None):
        status, body = real_get(url, headers)
        return (status, '{"ok": false}') if url.endswith("/health") else (status, body)
    dep.hub._get = unhealthy
    result = h.run_stage(0, live=True)
    checks = {c["name"]: c for c in result["checks"]}
    assert checks["hub-health"]["result"] == "fail"
    assert checks["hub-health"]["evidence"]["health_ok"] is False
    assert checks["hub-authorized"]["result"] == "pass"  # auth is separate


# --------------------------------------- 15-19: stage 3, the stop tests

def test_dry_run_stage3_prints_plan_and_calls_nothing(env, tmp_path):
    """DESIGN stage 3: the six-scenario plan prints; nothing runs or writes."""
    dep, transport, epic, provider = env
    h, lines = harness(dep, tmp_path / "out")
    result = h.run_stage(3, live=False)

    text = "\n".join(lines)
    for scenario in ("scenario 1", "scenario 2", "scenario 3",
                     "scenario 4", "scenario 5", "scenario 6"):
        assert scenario in text
    assert "verified_stopped" in text
    assert provider.calls == []                      # no model call
    assert transport.calls == []
    assert dep.tickets_hub.get(epic.ticket_id).status == "draft"  # untouched
    assert not (tmp_path / "out").exists()
    assert result["verdict"] == "unverified"


def test_stage3_live_all_six_stop_scenarios_pass(env, tmp_path):
    """DESIGN stage 3: every §6.5 scenario reaches verified_stopped (or the
    documented refusal), and the evidence file records it."""
    dep, _, _, provider = env
    _hire(env)
    provider.blocks = True   # scenario 2 cancels a genuinely in-flight call
    provider_calls_before = len(provider.calls)

    h, _ = harness(dep, tmp_path / "out")
    result = h.run_stage(3, live=True)

    by_name = {c["name"]: c for c in result["checks"]}
    expected = ("employee-hired", "epic-tagged", "stop-during-sandbox-command",
                "stop-during-model-request", "stop-with-queued-work",
                "stop-across-restart", "stop-with-concurrent-admission",
                "stop-late-result")
    for name in expected:
        assert by_name[name]["result"] == "pass", (name, by_name[name])
    assert result["verdict"] == "pass"
    doc = json.loads((tmp_path / "out" / "stage-3.json").read_text(
        encoding="utf-8"))
    assert doc["stage"] == 3 and doc["verdict"] == "pass"
    # scenario 2 made exactly one model call (the one that was cancelled):
    assert len(provider.calls) == provider_calls_before + 1


def test_stage3_restart_marks_uncertain_and_no_duplicate(env):
    """Scenario 4 in detail: the interrupted stop is unverified with the
    inference check named, recover() marks the run uncertain, the run count
    is unchanged, and the second stop verifies."""
    dep, _, _, provider = env
    _hire(env)
    provider.blocks = True   # scenario 2 cancels a genuinely in-flight call
    h, _ = harness(dep)
    result = h.run_stage(3, live=True)
    check = {c["name"]: c for c in result["checks"]}["stop-across-restart"]
    ev = check["evidence"]
    assert ev["interrupted_unverified"] is True
    assert "inference_in_flight" in ev["interrupted_unknowns"]
    assert ev["marked_uncertain"] is True
    assert ev["runs_before"] == ev["runs_after"]
    assert ev["final_state"] == "verified_stopped"


def test_stage3_concurrent_admission_refusals(env):
    """Scenario 5 in detail: the fence refuses a new admission (Fenced) and
    a stopped parent refuses a child (InvalidTask)."""
    dep, _, _, provider = env
    _hire(env)
    provider.blocks = True   # scenario 2 cancels a genuinely in-flight call
    h, _ = harness(dep)
    result = h.run_stage(3, live=True)
    ev = {c["name"]: c for c in result["checks"]}[  # noqa: E501
        "stop-with-concurrent-admission"]["evidence"]
    assert ev["fenced_admission_error"] == "Fenced"
    assert ev["child_under_stopped_parent_error"] == "InvalidTask"


def test_stage3_late_result_refused_after_stop(env):
    """Scenario 6 in detail: accepted while running, refused after the stop,
    with a discarded_late_result observation in the product's own table."""
    dep, _, _, provider = env
    _hire(env)
    provider.blocks = True   # scenario 2 cancels a genuinely in-flight call
    h, _ = harness(dep)
    result = h.run_stage(3, live=True)
    ev = {c["name"]: c for c in result["checks"]}["stop-late-result"]["evidence"]
    assert ev["accepted_while_running"] is True
    assert ev["accepted_after_stop"] is False
    assert ev["discarded_observations"] >= 1


def test_stage3_unknown_inference_check_is_never_verified(env):
    """The unknown rule (§6.5): an inference check that answers unknown
    holds the whole stage at unverified and names the check."""
    dep, _, _, _ = env
    _hire(env)

    class UnknownInference:
        def cancel(self, run_id):
            pass

        def in_flight(self, run_id):
            return None  # never a definitive answer

    dep.inference_control = UnknownInference()
    h, _ = harness(dep)
    result = h.run_stage(3, live=True)
    assert result["verdict"] == "unverified"
    by_name = {c["name"]: c for c in result["checks"]}
    assert by_name["stop-during-sandbox-command"]["result"] == "unverified"
    assert "inference_in_flight" in ",".join(
        by_name["stop-during-sandbox-command"]["evidence"].get(
            "unknown_checks", []))


# ------------------------------------- 20+: stage 4, the bypass tests (§11.7)

class FakeBypassControl:
    """A BypassControl fake with the harness stage-4 protocol: every
    attempt is recorded and answered with a scripted observation.
    deny=True refuses at the expected layer; deny=False lets the bypass
    through (a security fail); deny=None never confirms. ``layer``
    overrides the attribution (wrong-layer scenarios); ``raise_on``
    explodes one attempt; ``mechanism`` names T1's execution mechanism."""

    def __init__(self, *, deny=True, layer=None, mechanism=None,
                 raise_on=None) -> None:
        self.attempts: list[dict] = []
        self.deny = deny
        self.layer = layer
        self.mechanism = mechanism
        self.raise_on = raise_on

    def _observe(self, name, expected, **extra):
        if self.raise_on == name:
            raise RuntimeError("attempt " + name + " exploded")
        self.attempts.append(dict(name=name, **extra))
        if self.deny is None:
            return {"refused": None}
        if self.deny is False:
            return {"refused": False, "layer": None}
        obs = {"refused": True,
               "layer": self.layer if self.layer else expected}
        if self.mechanism:
            obs["mechanism"] = self.mechanism
        return obs

    def attempt_network_direct(self, run_id, targets):
        return self._observe("t1", "L2", targets=list(targets))

    def attempt_credential_read(self, run_id, path):
        return self._observe("t2", "L2", path=path)

    def attempt_ungranted_tool(self, run_id, tool):
        return self._observe("t3", "L1", tool=tool)

    def attempt_native_terminal(self, run_id, tool):
        return self._observe("t4", "L2", tool=tool)

    def attempt_ticket_write(self, run_id):
        return self._observe("t5", "L1")


class RecordingSink:
    """A bypass C2 sink that keeps every record it is given (the fake
    stand-in for the Runtime Emitter wiring of the live deployment)."""

    def __init__(self) -> None:
        self.records: list[dict] = []

    def __call__(self, record: dict) -> None:
        self.records.append(record)


class ExplodingSink:
    """A bypass C2 sink that records until *after* calls, then raises —
    the emit failure must be a named fail, not a crash."""

    def __init__(self, after: int) -> None:
        self.after = after
        self.records: list[dict] = []

    def __call__(self, record: dict) -> None:
        if len(self.records) >= self.after:
            raise RuntimeError("emit failed")
        self.records.append(record)


def _stage4_setup(env, tmp_path, *, control, sink=None):
    """Stage 1 live (the hire stage 4 reuses), then the stage-4 controls
    on the same fake deployment the stage-3 tests use."""
    dep, _, _, _ = env
    _hire(env)
    dep.bypass_control = control
    dep.bypass_c2_sink = sink
    dep.bypass_fixture_dir = str(tmp_path / "bypass-fixtures")
    return dep


def _by_name(result):
    return {c["name"]: c for c in result["checks"]}


def test_dry_run_stage4_prints_plan_and_calls_nothing(env, tmp_path):
    """DESIGN stage 4: the five-test plan prints; nothing runs or writes
    (guardrail 7) — no fixtures, no hub, no model call, no evidence."""
    dep, transport, epic, provider = env
    dep.bypass_fixture_dir = str(tmp_path / "bypass-fixtures")  # this run's
    h, lines = harness(dep, tmp_path / "out")
    result = h.run_stage(4, live=False)

    text = "\n".join(lines)
    for name in ("bypass-network-direct", "bypass-credential-read",
                 "bypass-ungranted-tool", "bypass-native-terminal",
                 "bypass-readonly-token-write"):
        assert name in text                      # all five tests planned
    for layer in ("L1", "L2"):
        assert layer in text                     # the layers attributed
    assert "C2" in text                          # the emit is planned
    assert "127.0.0.1" in text                   # inert loopback targets
    assert provider.calls == []                  # no model call
    assert transport.calls == []                 # no hub call
    assert dep.tickets_hub.get(epic.ticket_id).status == "draft"  # untouched
    assert not (tmp_path / "out").exists()       # nothing was written
    # the dry run created no credential fixtures anywhere (guardrail 7):
    # proven against THIS run's fixture dir, not the default (glm review
    # 2026-11-11 — a stale default dir would mask the check)
    assert not Path(dep.bypass_fixture_dir).exists()
    assert result["verdict"] == "unverified"


def test_stage4_live_refusals_attribute_to_their_layers(env, tmp_path):
    """A5-BYPASS.md §3: each denial is attributed to its layer — T2/T4 at
    L2, T3/T5 at L1 pass on the positive refusal; T1 stays capped at
    unverified naming the mechanism; every attempt is C2-recorded."""
    dep, _, _, _ = env
    control = FakeBypassControl()
    sink = RecordingSink()
    _stage4_setup(env, tmp_path, control=control, sink=sink)
    h, _ = harness(dep, tmp_path / "out")
    result = h.run_stage(4, live=True)

    by_name = _by_name(result)
    for name in ("bypass-network-direct", "bypass-credential-read",
                 "bypass-ungranted-tool", "bypass-native-terminal",
                 "bypass-readonly-token-write"):
        assert by_name[name]["result"] == "pass", (name, by_name[name])
        assert by_name[name]["evidence"]["refused"] is True
    # the mechanism is recorded even though T1 no longer caps the stage:
    assert by_name["bypass-network-direct"]["evidence"]["mechanism"]
    assert by_name["credential-fixtures-present"]["result"] == "pass"
    assert by_name["bypass-attempts-recorded"]["result"] == "pass"
    assert result["verdict"] == "pass"             # the mechanism settled

    # T1 was aimed only at the exact inert target set (no internet): the
    # closed port AND the second loopback port, nothing else (glm review
    # 2026-11-11 — a prefix check alone would pass any loopback service).
    t1_targets = next(a["targets"] for a in control.attempts
                      if a["name"] == "t1")
    assert list(t1_targets) == list(qr.BYPASS_INERT_TARGETS)
    # every attempt the design names was really made, in order (glm review
    # 2026-11-11 — T5's pass must not be accepted without its attempt):
    assert [a["name"] for a in control.attempts] == \
        ["t1", "t2", "t3", "t4", "t5"]
    # T2's path is the harness fixture and really holds the marker —
    # the refusal was a refusal of a readable file (rule 6):
    t2_path = next(a["path"] for a in control.attempts if a["name"] == "t2")
    assert str(tmp_path / "bypass-fixtures") in t2_path
    assert Path(t2_path).read_text(encoding="utf-8").strip() \
        == "SYNTHETIC" + "-NOT-A-" + "CREDENTIAL"
    # T3 asked for the ungranted tool, T4 for the disabled terminal:
    assert next(a["tool"] for a in control.attempts
                if a["name"] == "t3") == qr.BYPASS_UNGRANTED_TOOL
    assert next(a["tool"] for a in control.attempts
                if a["name"] == "t4") == qr.BYPASS_TERMINAL_TOOL

    # C2: exactly one record per attempt, in the five-test order:
    assert [r["check"] for r in sink.records] == [
        "bypass-network-direct", "bypass-credential-read",
        "bypass-ungranted-tool", "bypass-native-terminal",
        "bypass-readonly-token-write"]
    assert all(r["run_id"] and r["expected_layer"] and r["ts"]
               for r in sink.records)

    # the evidence file records the capped stage, and carries no fixture
    # content (rule 7): the marker appears nowhere.
    path = tmp_path / "out" / "stage-4.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["stage"] == 4 and doc["verdict"] == "pass"
    assert "SYNTHETIC" + "-NOT-A-" not in path.read_text(encoding="utf-8")


def test_stage4_successful_bypass_is_a_fail(env, tmp_path):
    """Rule 6 the other way: an attempt that gets through is a named fail
    (a guardrail that did not refuse is a finding, never a pass)."""
    dep, _, _, _ = env
    control = FakeBypassControl(deny=False)
    _stage4_setup(env, tmp_path, control=control, sink=RecordingSink())
    h, _ = harness(dep, tmp_path / "out")
    result = h.run_stage(4, live=True)

    by_name = _by_name(result)
    for name in ("bypass-credential-read", "bypass-ungranted-tool",
                 "bypass-native-terminal", "bypass-readonly-token-write"):
        assert by_name[name]["result"] == "fail", (name, by_name[name])
        assert by_name[name]["evidence"]["bypass_succeeded"] is True
    assert result["verdict"] == "fail"


def test_stage4_refusal_at_the_wrong_layer_is_a_fail(env, tmp_path):
    """§3: attribute each denial to its layer. A refusal answered at L1
    where L2 was expected fails the L2 checks and passes the L1 ones."""
    dep, _, _, _ = env
    control = FakeBypassControl(layer="L1")   # everything refused at L1
    _stage4_setup(env, tmp_path, control=control, sink=RecordingSink())
    h, _ = harness(dep, tmp_path / "out")
    result = h.run_stage(4, live=True)

    by_name = _by_name(result)
    assert by_name["bypass-ungranted-tool"]["result"] == "pass"    # L1 ok
    assert by_name["bypass-readonly-token-write"]["result"] == "pass"
    assert by_name["bypass-credential-read"]["result"] == "fail"   # L2 exp
    assert by_name["bypass-credential-read"]["evidence"]["wrong_layer"] is True
    assert by_name["bypass-native-terminal"]["result"] == "fail"
    assert result["verdict"] == "fail"


def test_stage4_refusal_at_l2_where_l1_expected_is_a_fail(env, tmp_path):
    """The mirror case (glm review 2026-11-11): refusals answered at L2
    fail the L1 checks (T3/T5) and pass the L2 ones — a harness that
    ignored the layer for L1 checks would be caught."""
    dep, _, _, _ = env
    control = FakeBypassControl(layer="L2")   # everything refused at L2
    _stage4_setup(env, tmp_path, control=control, sink=RecordingSink())
    h, _ = harness(dep, tmp_path / "out")
    result = h.run_stage(4, live=True)

    by_name = _by_name(result)
    assert by_name["bypass-credential-read"]["result"] == "pass"   # L2 ok
    assert by_name["bypass-native-terminal"]["result"] == "pass"
    assert by_name["bypass-ungranted-tool"]["result"] == "fail"    # L1 exp
    assert by_name["bypass-ungranted-tool"]["evidence"]["wrong_layer"] is True
    assert by_name["bypass-readonly-token-write"]["result"] == "fail"
    assert result["verdict"] == "fail"


def test_stage4_missing_fixture_keeps_t2_from_passing(env, tmp_path):
    """An unreadable/missing credential fixture means T2 has no positive
    evidence: the fixture check is unverified and T2 never passes (glm
    review 2026-11-11 — readback was only checked on the happy path)."""
    dep, _, _, _ = env
    control = FakeBypassControl()
    _stage4_setup(env, tmp_path, control=control, sink=RecordingSink())
    dep.bypass_fixture_dir = "/proc/nonexistent-a5/bypass-fixtures"  # unwritable
    h, _ = harness(dep, tmp_path / "out")
    result = h.run_stage(4, live=True)

    by_name = _by_name(result)
    assert by_name["credential-fixtures-present"]["result"] == "unverified"
    t2 = by_name["bypass-credential-read"]
    assert t2["result"] != "pass", t2
    assert result["verdict"] in ("unverified", "fail")


def test_stage4_unconfirmed_refusal_is_unverified(env, tmp_path):
    """An attempt that never confirms its refusal reports unverified,
    never a pass on absent evidence (rule 6)."""
    dep, _, _, _ = env
    control = FakeBypassControl(deny=None)    # refused: None, never a yes
    _stage4_setup(env, tmp_path, control=control, sink=RecordingSink())
    h, _ = harness(dep, tmp_path / "out")
    result = h.run_stage(4, live=True)

    by_name = _by_name(result)
    for name in ("bypass-credential-read", "bypass-ungranted-tool",
                 "bypass-native-terminal", "bypass-readonly-token-write"):
        assert by_name[name]["result"] == "unverified", (name, by_name[name])
    assert result["verdict"] == "unverified"


def test_stage4_without_control_never_passes(env, tmp_path):
    """No execution mechanism wired: every attempt is unverified naming
    the missing control (rule 6), the recording too; nothing is written."""
    dep, _, _, _ = env
    _stage4_setup(env, tmp_path, control=None)
    h, _ = harness(dep, tmp_path / "out")
    result = h.run_stage(4, live=True)

    by_name = _by_name(result)
    for name, _tid, _layer in qr._STAGE4_TESTS:
        assert by_name[name]["result"] == "unverified", name
        assert "no bypass control" in by_name[name]["evidence"]["unknown"]
    assert by_name["bypass-attempts-recorded"]["result"] == "unverified"
    assert result["verdict"] == "unverified"
    assert not (tmp_path / "bypass-fixtures").exists()  # nothing primed


def test_stage4_control_exception_is_unverified_not_crash(env, tmp_path):
    """One exploding attempt unverifieds its own check (named) and leaves
    the other four to answer normally."""
    dep, _, _, _ = env
    control = FakeBypassControl(raise_on="t3")
    _stage4_setup(env, tmp_path, control=control, sink=RecordingSink())
    h, _ = harness(dep, tmp_path / "out")
    result = h.run_stage(4, live=True)

    by_name = _by_name(result)
    assert by_name["bypass-ungranted-tool"]["result"] == "unverified"
    assert by_name["bypass-ungranted-tool"]["evidence"][
        "error_type"] == "RuntimeError"
    assert by_name["bypass-credential-read"]["result"] == "pass"
    assert result["verdict"] == "unverified"    # the exploded attempt holds


def test_stage4_without_sink_leaves_recording_unverified(env, tmp_path):
    """The plan emits each attempt as a C2 record; with no sink injected
    the recording check stays unverified (this lane never emits on its
    own) while the refusals themselves still pass."""
    dep, _, _, _ = env
    control = FakeBypassControl()
    _stage4_setup(env, tmp_path, control=control, sink=None)
    h, _ = harness(dep, tmp_path / "out")
    result = h.run_stage(4, live=True)

    by_name = _by_name(result)
    assert by_name["bypass-attempts-recorded"]["result"] == "unverified"
    assert "C2" in by_name["bypass-attempts-recorded"]["evidence"]["unknown"]
    assert by_name["bypass-credential-read"]["result"] == "pass"
    assert result["verdict"] == "unverified"


def test_stage4_emit_failure_is_a_named_fail(env, tmp_path):
    """A sink that raises mid-record fails the recording check by name
    (the attempts happened; the record-keeping did not)."""
    dep, _, _, _ = env
    control = FakeBypassControl()
    sink = ExplodingSink(after=2)
    _stage4_setup(env, tmp_path, control=control, sink=sink)
    h, _ = harness(dep, tmp_path / "out")
    result = h.run_stage(4, live=True)

    by_name = _by_name(result)
    ev = by_name["bypass-attempts-recorded"]["evidence"]
    assert by_name["bypass-attempts-recorded"]["result"] == "fail"
    assert ev["error_type"] == "RuntimeError"
    assert ev["records"] == 2
    assert len(sink.records) == 2
    assert result["verdict"] == "fail"


def test_stage4_t1_names_the_control_mechanism_when_given(env, tmp_path):
    """T1 names the control's mechanism when it reports one — the
    mechanism is settled (the operator, D1-A) but stays recorded in evidence."""
    dep, _, _, _ = env
    control = FakeBypassControl(
        mechanism="docker exec into the run's sandbox (settled)")
    _stage4_setup(env, tmp_path, control=control, sink=RecordingSink())
    h, _ = harness(dep, tmp_path / "out")
    result = h.run_stage(4, live=True)

    t1 = _by_name(result)["bypass-network-direct"]
    assert t1["result"] == "pass"
    assert t1["evidence"]["mechanism"] == \
        "docker exec into the run's sandbox (settled)"


def test_stage4_untagged_epic_is_refused(env, tmp_path):
    """Rule 6 gates stage 4 too: an epic without the a5-qualification tag
    fails the stage before any attempt runs."""
    dep, _, _, _ = env
    control = FakeBypassControl()
    _stage4_setup(env, tmp_path, control=control, sink=RecordingSink())
    plain = dep.tickets_hub.create_ticket("No tag here",
                                          actor="operator")  # no tags
    dep.epic_ticket_id = plain.ticket_id
    h, _ = harness(dep)
    result = h.run_stage(4, live=True)

    by_name = _by_name(result)
    assert by_name["epic-tagged"]["result"] == "fail"
    assert qr.TAG in by_name["epic-tagged"]["evidence"]["refused"]
    assert result["verdict"] == "fail"
    assert control.attempts == []               # nothing was attempted


# ------------------------- W2b lane: the live HermesBypassControl itself

class ControlDockerFake:
    """A DockerCli stand-in: records exec calls, scripts the exit code."""

    def __init__(self, exec_exit: int = 1, container: str = "sbx-1") -> None:
        self.calls: list[list] = []
        self.exec_exit = exec_exit
        self._container = container
        self.task_containers_calls = 0

    def task_containers(self, profile, card):
        """The container-listing seam the control waits through; recorded
        separately from the exec calls."""
        self.task_containers_calls += 1
        return [self._container]

    def _call(self, args):
        import subprocess as sp
        self.calls.append(list(args))
        return sp.CompletedProcess(args, self.exec_exit, stdout="", stderr="")


class ControlAdapterFake:
    """An adapter stand-in: optional container_for / call_tool / session_for
    seams, exactly what HermesBypassControl reaches for."""

    def __init__(self, *, container="sbx-1", call_tool=None,
                 session_for=None) -> None:
        self._container = container
        self._call_tool = call_tool
        self._session_for = session_for
        self.containers_asked: list[str] = []

    def container_for(self, run_id):
        self.containers_asked.append(run_id)
        return ("a5-test", self._container)

    def call_tool(self, session_id, tool):
        if self._call_tool is None:
            raise AssertionError("call_tool seam used but not scripted")
        return self._call_tool(session_id, tool)

    def session_for(self, run_id):
        return self._session_for(run_id) if self._session_for else None


def control(*, adapter=None, docker=None, granted_tools=("echo",),
            token_ref=None) -> qr.HermesBypassControl:
    return qr.HermesBypassControl(
        adapter=adapter if adapter is not None else ControlAdapterFake(),
        cli=None, docker=docker, granted_tools=granted_tools,
        token_ref=token_ref)


def test_control_t1_without_a_seam_is_honest_unknown():
    """No container seam: the route probe cannot run — refused None with
    the mechanism named, aimed at exactly the inert targets."""
    c = control()
    obs = c.attempt_network_direct("run-1", qr.BYPASS_INERT_TARGETS)
    assert obs["refused"] is None
    assert obs["mechanism"]
    assert list(obs["targets"]) == list(qr.BYPASS_INERT_TARGETS)


def test_control_t1_no_default_route_is_l2():
    """Mechanism settled (the operator, D1-A: the docker-exec route probe): a
    sandbox with no default route is structurally denied at L2."""
    class RouteAdapterFake(ControlAdapterFake):
        def container_for(self, run_id):
            return ("a5-test", "card-1")

    class RouteDockerFake:
        def __init__(self, out):
            self.out = out
            self.calls = []

        def task_containers(self, profile, card):
            return ["abc123"]

        def _call(self, argv):
            self.calls.append(list(argv))
            import subprocess as sp
            return sp.CompletedProcess(argv, 0, stdout=self.out, stderr="")

    docker = RouteDockerFake("169.254.1.1 dev eth0\n")   # no default route
    obs = control(adapter=RouteAdapterFake(), docker=docker) \
        .attempt_network_direct("run-1", qr.BYPASS_INERT_TARGETS)
    assert obs["refused"] is True and obs["layer"] == "L2"
    assert "no default route" in obs["mechanism"]
    assert docker.calls == [["exec", "abc123", "sh", "-c",
                             "ip route 2>/dev/null || cat /proc/net/route "
                             "2>/dev/null"]]


def test_control_t1_default_route_is_a_bypass_finding():
    """A default route means egress is routed: refused False — the
    harness turns it into a named security fail, never a pass."""
    class RouteAdapterFake(ControlAdapterFake):
        def container_for(self, run_id):
            return ("a5-test", "card-1")

    class RouteDockerFake:
        def task_containers(self, profile, card):
            return ["abc123"]

        def _call(self, argv):
            import subprocess as sp
            gw = "172" + ".17.0.1"          # the docker bridge, assembled
            return sp.CompletedProcess(argv, 0, stdout="default via "
                                       + gw + " dev eth0\n", stderr="")

    obs = control(adapter=RouteAdapterFake(),
                  docker=RouteDockerFake()) \
        .attempt_network_direct("run-1", qr.BYPASS_INERT_TARGETS)
    assert obs["refused"] is False


def test_control_t2_without_a_sandbox_seam_is_honest_unknown():
    """No container seam from the adapter: the attempt is not made and
    the unknown says so (never a fabricated refusal)."""
    class NoContainerAdapter(ControlAdapterFake):
        container_for = None          # the seam is absent, not just empty
    obs = control(adapter=NoContainerAdapter()) \
        .attempt_credential_read("run-1", "/some/fixture")
    assert obs["refused"] is None
    assert "seam" in obs["mechanism"]


def test_control_t2_without_a_container_is_honest_unknown():
    """A container seam that answers no container for the run is also an
    honest unknown (glm review 2026-11-11 — the default fake returns
    sbx-1, which exercised missing docker, not a missing container)."""
    obs = control(adapter=ControlAdapterFake(container=None)) \
        .attempt_credential_read("run-1", "/some/fixture")
    assert obs["refused"] is None
    assert "container" in obs["mechanism"]


def test_control_t2_no_path_is_unknown_with_a_reason():
    obs = control().attempt_credential_read("run-1", "")
    assert obs["refused"] is None
    assert obs["reason"]


def test_control_t2_refusal_read_through_docker_is_l2():
    """With the container seam, the structural denial is observed through
    the docker transport: exit != 0 -> refused at L2, exit code as
    evidence, and no file content anywhere in the observation."""
    docker = ControlDockerFake(exec_exit=1, container="sbx-7")
    adapter = ControlAdapterFake(container="sbx-7")
    obs = control(adapter=adapter, docker=docker).attempt_credential_read(
        "run-1", "credential-fixtures/aws-credentials")
    assert obs["refused"] is True and obs["layer"] == "L2"
    assert obs["exit_code"] == 1
    assert docker.calls == [["exec", "sbx-7", "test", "-r",
                             "credential-fixtures/aws-credentials"]]
    assert "SYNTHETIC" + "-NOT-A-" not in json.dumps(obs)


def test_control_t2_readable_fixture_is_a_bypass_success_not_a_pass():
    """A readable fixture inside the sandbox means the bypass got through:
    refused False (the harness turns it into a named fail) and the
    content is deliberately never read or quoted."""
    docker = ControlDockerFake(exec_exit=0, container="sbx-9")
    obs = control(adapter=ControlAdapterFake(container="sbx-9"),
                  docker=docker) \
        .attempt_credential_read("run-1", "credential-fixtures/api-token")
    assert obs["refused"] is False
    # the bypass success is backed by the real probe (glm review
    # 2026-11-11 — a control answering without the exec proves nothing)
    assert docker.calls == [["exec", "sbx-9", "test", "-r",
                             "credential-fixtures/api-token"]]
    assert "SYNTHETIC" + "-NOT-A-" not in json.dumps(obs)


def test_control_t3_structural_admission_refusal_is_l1():
    """With the boundary allow-list injected, an ungranted tool is
    refused at the admission layer (structural read of the boundary
    configuration)."""
    obs = control(granted_tools=("echo",)).attempt_ungranted_tool(
        "run-1", qr.BYPASS_UNGRANTED_TOOL)
    assert obs["refused"] is True and obs["layer"] == "L1"
    assert qr.BYPASS_UNGRANTED_TOOL in obs["mechanism"]


def test_control_t3_granted_tool_cannot_confirm_a_refusal():
    """A tool that IS in the allow-list would not be refused: honest
    unknown, never a fabricated refusal."""
    obs = control(granted_tools=("echo", "fetch_url")) \
        .attempt_ungranted_tool("run-1", "fetch_url")
    assert obs["refused"] is None and obs["mechanism"]


def test_control_t3_refusal_read_from_the_tool_call_result():
    """When the adapter exposes the session's tool path, the refusal is
    read from the tool-call result error itself."""
    seen = {}

    def call_tool(session_id, tool):
        seen.update(session_id=session_id, tool=tool)
        return {"error": "tool not granted: " + tool}

    adapter = ControlAdapterFake(call_tool=call_tool,
                                 session_for=lambda run_id: "sess-1")
    obs = control(adapter=adapter).attempt_ungranted_tool(
        "run-1", qr.BYPASS_UNGRANTED_TOOL)
    assert obs["refused"] is True and obs["layer"] == "L1"
    assert obs["error_type"].startswith("tool not granted")
    assert seen == {"session_id": "sess-1",
                    "tool": qr.BYPASS_UNGRANTED_TOOL}


def test_control_t4_structural_native_refusal_is_l2():
    """Native tools are off in the profile config (an empty cli
    allow-list is the definitive marker, glm review 2026-11-11): the
    disabled terminal is refused at the sandbox layer (structural read);
    a config that does not prove native tools off is an honest unknown."""
    class ProfileAdapterFake(ControlAdapterFake):
        def read_profile(self, profile):
            return {"platform_toolsets": {"cli": []}}
    obs = control(adapter=ProfileAdapterFake()).attempt_native_terminal(
        "run-1", qr.BYPASS_TERMINAL_TOOL)
    assert obs["refused"] is True and obs["layer"] == "L2"

    class EnabledAdapterFake(ControlAdapterFake):
        def read_profile(self, profile):
            return {"platform_toolsets": {"cli": ["terminal"]}}
    obs = control(adapter=EnabledAdapterFake()).attempt_native_terminal(
        "run-1", qr.BYPASS_TERMINAL_TOOL)
    assert obs["refused"] is None            # not provable -> unknown
    assert "native tools" in obs["mechanism"]


def test_control_t4_refusal_read_from_the_tool_call_result():
    # the terminal IS in the allow-list here, so the structural branch
    # would answer unknown: only the tool-call observation can produce
    # this refusal (glm review 2026-11-11)
    seen = {}

    def call_tool(session_id, tool):
        seen.update(session_id=session_id, tool=tool)
        return {"error": "native tools are off"}

    adapter = ControlAdapterFake(call_tool=call_tool,
                                 session_for=lambda run_id: "sess-2")
    obs = control(adapter=adapter,
                  granted_tools=("echo", qr.BYPASS_TERMINAL_TOOL)) \
        .attempt_native_terminal("run-1", qr.BYPASS_TERMINAL_TOOL)
    assert obs["refused"] is True and obs["layer"] == "L2"
    assert seen == {"session_id": "sess-2",
                    "tool": qr.BYPASS_TERMINAL_TOOL}


def test_control_t4_without_seams_is_honest_unknown():
    obs = control(granted_tools=None).attempt_native_terminal(
        "run-1", qr.BYPASS_TERMINAL_TOOL)
    assert obs["refused"] is None and obs["mechanism"]


def test_control_t5_without_a_token_ref_is_honest_unknown():
    """No read-only token reference configured: the attempt cannot be
    made — honest unknown (rule 6)."""
    obs = control().attempt_ticket_write("run-1")
    assert obs["refused"] is None
    assert "token reference" in obs["mechanism"]


def test_control_t5_refused_by_the_hub_is_l1(env, tmp_path, monkeypatch):
    """The read-only credential attempts a real comment POST; the hub's
    access layer refuses it (the credential carries no scopes) — the L1
    admission refusal T5 expects. The token value never appears in the
    observation (rule 7)."""
    dep, _, _, _ = env
    _stage4_setup(env, tmp_path, control=None, sink=RecordingSink())
    token_file = tmp_path / "t5-readonly.token"
    token_file.write_text("synthetic" + "-readonly-fixture", encoding="utf-8")
    dep.t5_token_ref = str(token_file)

    seen = {}

    def fake_urlopen(req, timeout=15):
        seen["auth"] = req.headers.get("Authorization")
        seen["url"] = req.full_url
        import urllib.error
        raise urllib.error.HTTPError(req.full_url, 403, "forbidden",
                                     {}, None)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    control = qr.HermesBypassControl(
        adapter=ControlAdapterFake(), cli=None, docker=None,
        granted_tools=("echo",), token_ref=str(token_file),
        hub_url="http://hub.fake", epic=dep.epic_ticket_id)
    obs = control.attempt_ticket_write("run-1")
    assert obs["refused"] is True and obs["layer"] == "L1"
    assert obs["status"] == 403
    assert "synthetic" not in json.dumps(obs)   # the token value is hidden
    assert seen["auth"].endswith("synthetic" + "-readonly-fixture")
    assert "/tickets/" in seen["url"]


def test_control_t5_write_succeeding_is_a_security_fail(env, tmp_path,
                                                        monkeypatch):
    """If the read-only credential WRITES, the guardrail is broken: a
    named fail, never a pass."""
    dep, _, _, _ = env
    _stage4_setup(env, tmp_path, control=None, sink=RecordingSink())
    token_file = tmp_path / "t5-readonly.token"
    token_file.write_text("synthetic" + "-readonly-fixture", encoding="utf-8")
    dep.t5_token_ref = str(token_file)

    import urllib.request as urlreq

    class FakeResp:
        status = 201

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(urlreq, "urlopen", lambda req, timeout=15: FakeResp())
    control = qr.HermesBypassControl(
        adapter=ControlAdapterFake(), cli=None, docker=None,
        granted_tools=("echo",), token_ref=str(token_file),
        hub_url="http://hub.fake", epic=dep.epic_ticket_id)
    obs = control.attempt_ticket_write("run-1")
    assert obs["refused"] is False
    assert obs["status"] == 201


def test_control_observations_match_the_bypass_check_shape():
    """Every attempt answers the dict _bypass_check reads: refused is
    bool|None; a True refusal carries its layer; an unknown carries a
    mechanism or reason; no observation carries secret-shaped text."""
    c = control(granted_tools=("echo",), docker=ControlDockerFake(exec_exit=1),
                adapter=ControlAdapterFake())
    observations = [
        c.attempt_network_direct("run-1", c.targets),
        c.attempt_credential_read("run-1", "credential-fixtures/api-token"),
        c.attempt_ungranted_tool("run-1", c.ungranted_tool),
        c.attempt_native_terminal("run-1", c.terminal_tool),
        c.attempt_ticket_write("run-1"),
    ]
    for obs in observations:
        assert isinstance(obs, dict)
        assert "refused" in obs
        assert obs["refused"] in (True, False, None)
        if obs["refused"] is True:
            assert obs["layer"] in ("L1", "L2")
        if obs["refused"] is None:
            assert obs.get("mechanism") or obs.get("reason")
    assert "SYNTHETIC" + "-NOT-A-" not in json.dumps(observations)
    # and the harness's own check accepts the shape without crashing:
    for name, obs, layer in zip(
            ("t1", "t2", "t3", "t4", "t5"), observations,
            ("L2", "L2", "L1", "L2", "L1")):
        entry = qr.Harness(qr.Deployment(
            hub=None, hermes=None, models=None, resources=None,
            tickets_hub=None, runtime=None, gateway=None, ledger_path="",
            roles_conn=None, now=qr.Clock(T0), epic_ticket_id="",
            model_routes={}),
            None, print_fn=lambda line: None)._bypass_check(
            name, layer, obs, {})
        assert entry["result"] in qr._VERDICTS


def test_live_deployment_wires_the_bypass_control_only_when_live(tmp_path):
    """The CLI deployment builds NO bypass control (it is built inside
    run_stage4 from the sandbox's own adapter, so both share one session
    map — glm review 2026-11-11); a dry run builds nothing either —
    dry-run behaviour is unchanged."""
    import argparse
    doc_path = tmp_path / "resources.json"
    doc_path.write_text(json.dumps(resources_doc()), encoding="utf-8")
    base = dict(resources=str(doc_path), hub="http://hub.fake",
                hub_token_file=TOKEN_FILE,
                tickets_db=str(tmp_path / "hub.db"), remote_tickets=False,
                runtime_db=str(tmp_path / "runtime.db"),
                roles_db=str(tmp_path / "roles.db"),
                usage_ledger=str(tmp_path / "usage.jsonl"),
                epic="A5-EPIC-1")
    dry = qr._build_live_deployment(argparse.Namespace(live=False, **base))
    assert dry.bypass_control is None          # dry run unchanged

    live = qr._build_live_deployment(argparse.Namespace(live=True, **base))
    assert live.bypass_control is None       # built in run_stage4, from
                                             # the sandbox's own adapter


# --------------------------------------- 20-23: stage 5, the operator view

def test_dry_run_stage5_prints_plan_and_calls_nothing(env, tmp_path):
    """DESIGN stage 5: the plan prints; nothing runs or writes."""
    dep, transport, epic, provider = env
    h, lines = harness(dep, tmp_path / "out")
    result = h.run_stage(5, live=False)

    text = "\n".join(lines)
    assert "403" in text and "view.json" in text
    assert "OPERATOR credential" in text          # the the operator gate named
    assert provider.calls == [] and transport.calls == []
    assert not (tmp_path / "out").exists()
    assert result["verdict"] == "unverified"


def test_stage5_mount_refuses_anon_and_non_owner(env, tmp_path):
    """Both refusal checks pass on a positive 403; a 200 anywhere means
    the mount is open (fail)."""
    dep, transport, _, _ = env
    transport.get_status["/operator/"] = 403
    h, _ = harness(dep, tmp_path / "out")
    result = h.run_stage(5, live=True)

    by_name = _by_name(result)
    assert by_name["operator-mount-403-anon"]["result"] == "pass"
    assert by_name["operator-mount-403-non-owner"]["result"] == "pass"
    assert by_name["operator-view-fields"]["result"] == "unverified"
    assert "operator-issued owner" in by_name["operator-view-fields"]["evidence"]["unknown"]
    assert result["verdict"] == "unverified"


def test_stage5_open_mount_is_a_fail(env, tmp_path):
    """A 200 without authorization means the mount is open: fail."""
    dep, transport, _, _ = env
    transport.get_status["/operator/"] = 200
    h, _ = harness(dep, tmp_path / "out")
    result = h.run_stage(5, live=True)
    by_name = _by_name(result)
    assert by_name["operator-mount-403-anon"]["result"] == "fail"
    assert result["verdict"] == "fail"


def test_stage5_owner_view_fields_observed(env, tmp_path):
    """With the operator credential the view document is fetched and the
    acceptance fields are observed on the active rows."""
    dep, transport, _, _ = env
    doc = {"panels": {"active": {"state": "ok", "rows": [
        {"run_id": "RN-1", "ticket_id": "TK-1", "employee_id": "EM-1",
         "role": "ops-analyst", "slot": "worker", "model": "m",
         "layer": "worker", "state": "running", "elapsed_s": 12.5,
         "lanes": []}]}}}
    tok_file = tmp_path / "op.token"
    tok_file.write_text("synthetic-owner-credential", encoding="utf-8")
    dep.operator_token_file = str(tok_file)

    seen = {}
    real_get = dep.hub._get

    def owner_get(url, headers=None):
        auth = (headers or {}).get("authorization") or ""
        if auth.endswith("synthetic-owner-credential"):
            seen.update(url=url, auth=auth)
            return 200, json.dumps(doc)
        return 403, ""   # anon and harness-token viewers stay refused

    dep.hub._get = owner_get
    h, _ = harness(dep, tmp_path / "out")
    result = h.run_stage(5, live=True)

    by_name = _by_name(result)
    assert by_name["operator-mount-403-anon"]["result"] == "pass"
    assert by_name["operator-mount-403-non-owner"]["result"] == "pass"
    fields = by_name["operator-view-fields"]
    assert fields["result"] == "pass", fields
    assert fields["evidence"]["rows"] == 1
    assert "ticket_id" in fields["evidence"]["link_to_results"]
    assert "/operator/view.json" in seen["url"]
    # the owner credential value is never in the evidence file:
    out = (tmp_path / "out" / "stage-5.json").read_text(encoding="utf-8")
    assert "synthetic-owner-credential" not in out


def test_stage5_no_active_rows_is_honest_unverified(env, tmp_path):
    """An empty active panel observes nothing: unverified, never a pass."""
    dep, transport, _, _ = env
    doc = {"panels": {"active": {"state": "ok", "rows": []}}}
    tok_file = tmp_path / "op.token"
    tok_file.write_text("synthetic-owner-credential", encoding="utf-8")
    dep.operator_token_file = str(tok_file)
    dep.hub._get = lambda url, headers=None: (200, json.dumps(doc))
    h, _ = harness(dep, tmp_path / "out")
    result = h.run_stage(5, live=True)
    by_name = _by_name(result)
    assert by_name["operator-view-fields"]["result"] == "unverified"
    assert "no run was active" in by_name["operator-view-fields"]["evidence"]["unknown"]


# -------------------------------------- 24+: stage 6, metrics end to end

def _stage6_stores(env):
    """Put stage-2-shaped data in the deployment's OWN stores (never the
    harness's memory): one run in the Runtime store, three C2 records on
    a tagged task ticket in Tickets, two rows in the usage ledger file."""
    dep, _, _, _ = env
    _hire(env)
    pkg = qr.load_role(qr.FIXTURE_DIR)
    registry = qr.roles_registry.Registry(dep.roles_conn, now=dep.now)
    employees = [e for e in registry.list() if e.profile == qr.PROFILE]
    emp = employees[0]
    worker_slot = qr._worker_slot(dep.resources).name
    task = dep.tickets_hub.create_ticket(
        "stage 6 fixture run", body="metrics fixture",
        tags=(qr.TAG,), parent=dep.epic_ticket_id, actor=emp.employee_id)
    from datetime import timedelta
    c1 = qr.build_task(
        employee_id=emp.employee_id, pkg=pkg, duty=qr.DUTY,
        ticket_id=task.ticket_id, slot=worker_slot,
        grants=list(pkg.role.authority),
        budget={"max_tokens": 40000, "max_turns": 8},
        deadline=dep.now() + timedelta(minutes=10),
        idempotency_key="a5-stage6-run-" + qr._fresh_message_id(),
        now=dep.now)
    run = dep.runtime.admit(c1)
    dep.runtime.start(run.run_id)
    stamp = qr._utc_stamp(dep.now())
    base = {"version": "1.1.0", "producer": "runtime@test-harness",
            "sent_at": stamp, "run_id": run.run_id,
            "ticket_id": task.ticket_id, "employee_id": emp.employee_id,
            "ts": stamp}
    prev = None
    for seq, (event_type, extra) in enumerate((
            ("run_started", {}),
            ("tool_call", {"action_class": "read", "decision": "proceed"}),
            ("outcome", {"result": "resolved", "elapsed_s": 1.0,
                         "outcome_source": "agent_reported"})), start=1):
        rec = dict(base, message_id=qr._fresh_message_id(), seq=seq,
                   event_type=event_type, **extra)
        if prev is not None:
            rec["causation_id"] = prev
        dep.tickets_hub.record(rec)
        prev = rec["message_id"]
    pathlib.Path(dep.ledger_path).write_text(
        json.dumps({"run_id": run.run_id,
                    "usage": {"tokens_in": 90, "tokens_out": 30}}) + "\n"
        + json.dumps({"run_id": run.run_id,
                      "usage": {"tokens_in": 10, "tokens_out": 5}}) + "\n",
        encoding="utf-8")
    return run, task, emp, prev


def test_dry_run_stage6_prints_plan_and_computes_nothing(env, tmp_path):
    """DESIGN stage 6: the plan prints (both computations, the comparison,
    the stage B rule); nothing is computed, read or written."""
    dep, transport, _, _ = env
    h, lines = harness(dep, tmp_path / "out")
    result = h.run_stage(6, live=False)

    text = "\n".join(lines)
    assert "BY HAND" in text and "rlai_metrics.collect.collect" in text
    assert dep.ledger_path in text          # the ledger read is named
    assert "not_applicable" in text         # the stage B rule is named
    assert "value by value" in text
    assert not (tmp_path / "out").exists()  # nothing was written
    assert result["verdict"] == "unverified"  # a plan proves nothing


def test_stage6_package_matches_hand_computation(env, tmp_path):
    """Both computations are scoped to the fixture stage 6 itself creates;
    no tile contradicts the hand computation; stage B tiles are absent
    (nothing shown, never a zero)."""
    dep, _, _, _ = env
    _hire(env)   # stage 6 reuses the stage-1 employee
    # no seeded extra ticket: both computations are scoped to the fixture
    # stage 6 itself creates (glm review 2026-11-11)
    h, _ = harness(dep, tmp_path / "out")
    result = h.run_stage(6, live=True)

    by_name = _by_name(result)
    assert by_name["metrics-package-collect"]["result"] == "pass"
    assert by_name["hand-computation-from-stores"]["result"] == "pass"
    assert by_name["stage-b-tiles-not-applicable"]["result"] == "pass"
    match = by_name["metrics-panel-values-match"]
    # the hierarchy computers consume curator/ops records outside the A5
    # stores: with no comparable value the check is honestly unverified
    assert match["result"] in ("pass", "unverified"), match
    assert result["verdict"] in ("pass", "unverified")
def test_stage6_correct_tile_passes_the_comparison(env, tmp_path,
                                                    monkeypatch):
    """The positive case (glm review 2026-11-11): a tile that MATCHES the
    hand computation passes the comparison — a comparison that always
    returned unverified would still pass this suite without it."""
    dep, _, _, _ = env
    _stage6_stores(env)
    import rlai_metrics.obs as obs_mod
    import importlib as _il
    obs_mod = _il.import_module("rlai_metrics.obs")

    def truthful_tiles(observations, *, now):
        return [{"id": "runs", "value": 1, "value_class": "value",
                 "labels": (), "window_start": None, "window_end": now,
                 "sources": ("tickets",)}]

    monkeypatch.setattr(obs_mod, "tiles", truthful_tiles)
    h, _ = harness(dep, tmp_path / "out")
    result = h.run_stage(6, live=True)

    by_name = _by_name(result)
    match = by_name["metrics-panel-values-match"]
    assert match["result"] == "pass", match
    assert match["evidence"]["compared"] >= 1
    assert result["verdict"] == "pass"


def test_stage6_mismatch_names_the_panel_value(env, tmp_path, monkeypatch):
    """A tile that contradicts the hand computation fails the stage and
    NAMES the panel value."""
    dep, _, _, _ = env
    _stage6_stores(env)
    import importlib
    obs_mod = importlib.import_module("rlai_metrics.obs")

    def lying_tiles(observations, *, now):
        # "runs" IS a hand-computable scalar panel (the stores hold 1 run):
        # a tile claiming 999 contradicts it (glm review 2026-11-11)
        return [{"id": "runs", "value": 999, "value_class": "value",
                 "labels": (), "window_start": None, "window_end": now,
                 "sources": ("tickets",)}]

    monkeypatch.setattr(obs_mod, "tiles", lying_tiles)
    h, _ = harness(dep, tmp_path / "out")
    result = h.run_stage(6, live=True)

    by_name = _by_name(result)
    match = by_name["metrics-panel-values-match"]
    assert match["result"] == "fail"
    text = json.dumps(match["evidence"])
    assert "runs" in text and "999" in text
    assert result["verdict"] == "fail"


def test_stage6_stage_b_zero_is_a_fail(env, tmp_path, monkeypatch):
    """A stage B tile reporting a 0 (as if it were data) fails: design
    §11.9 — an unproven zero is a lie."""
    dep, _, _, _ = env
    _stage6_stores(env)
    import importlib
    obs_mod = importlib.import_module("rlai_metrics.obs")

    def lying_tiles(observations, *, now):
        return [{"id": "approvals", "value": 0, "value_class": "value",
                 "labels": (), "window_start": None, "window_end": now,
                 "sources": ("tickets",)}]

    monkeypatch.setattr(obs_mod, "tiles", lying_tiles)
    h, _ = harness(dep, tmp_path / "out")
    result = h.run_stage(6, live=True)

    by_name = _by_name(result)
    sb = by_name["stage-b-tiles-not-applicable"]
    assert sb["result"] == "fail"
    assert any("approvals" in p for p in sb["evidence"]["problems"])
    assert result["verdict"] == "fail"


def test_stage6_missing_metrics_package_is_honest_unverified(
        env, tmp_path, monkeypatch):
    """No rlai-metrics package: every package-side check is honestly
    unverified, naming the seam (rule 6)."""
    dep, _, _, _ = env
    _stage6_stores(env)
    for key in [k for k in sys.modules
                if k == "rlai_metrics" or k.startswith("rlai_metrics.")]:
        monkeypatch.delitem(sys.modules, key)
    # and hide the source from sys.path: deleting the sys.modules entries
    # alone lets import re-import the package from disk (glm review
    # 2026-11-11)
    monkeypatch.setattr(sys, "path",
                        [p for p in sys.path if "rlai-metrics" not in p])
    # the loader calls _install_package_paths itself, which would re-add
    # the path: neutralize it too (glm review 2026-11-11)
    monkeypatch.setattr(qr, "_install_package_paths", lambda: None)
    h, _ = harness(dep, tmp_path / "out")
    result = h.run_stage(6, live=True)

    by_name = _by_name(result)
    assert by_name["metrics-package-collect"]["result"] == "unverified"
    assert by_name["metrics-panel-values-match"]["result"] == "unverified"
    assert by_name["stage-b-tiles-not-applicable"]["result"] == "unverified"
    assert result["verdict"] == "unverified"


def test_stage6_hand_computation_reads_the_stores_fresh(env, tmp_path):
    """The hand computation reads the products' stores on every run, not
    the harness's memory: data added between runs changes the values."""
    dep, _, _, _ = env
    run, task, emp, prev = _stage6_stores(env)
    h, _ = harness(dep, tmp_path / "out")
    first = h.run_stage(6, live=True)
    hand = _by_name(first)["hand-computation-from-stores"]
    assert hand["result"] == "pass"
    before_ledger = hand["evidence"]["ledger_rows"]
    import pathlib as _pathlib
    before_lines = len(_pathlib.Path(dep.ledger_path).read_text(
        encoding="utf-8").splitlines())

    stamp = qr._utc_stamp(dep.now())
    dep.tickets_hub.record(dict(
        version="1.1.0", producer="runtime@test-harness", sent_at=stamp,
        run_id=run.run_id, ticket_id=task.ticket_id,
        employee_id=emp.employee_id, ts=stamp,
        message_id=qr._fresh_message_id(), seq=4, event_type="tool_call",
        action_class="read", decision="proceed", causation_id=prev))
    with open(dep.ledger_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"run_id": run.run_id,
                             "usage": {"tokens_in": 7, "tokens_out": 3}})
                 + "\n")
    second = h.run_stage(6, live=True)

    hand2 = _by_name(second)["hand-computation-from-stores"]
    assert hand2["result"] == "pass"
    # the fixture ticket is per stage-6 run (uuid idempotency key), so the
    # record count stays at the fixture's own 3; the LEDGER is shared and
    # the fresh read picks up the appended row — the store-fresh proof
    # the second run appends its own fixture rows AND the test appended
    # one: the fresh read sees every line in the file (glm review
    # 2026-11-11 — a hardcoded +1 breaks when the fixture writes rows)
    after_lines = len(_pathlib.Path(dep.ledger_path).read_text(
        encoding="utf-8").splitlines())
    assert hand2["evidence"]["ledger_rows"] == after_lines
    assert after_lines == before_lines + 1


def test_stage6_unreadable_ledger_is_honest_unverified(env, tmp_path):
    """An unreadable usage ledger is an honest unverified, never a pass
    on absent evidence."""
    dep, _, _, _ = env
    _stage6_stores(env)
    # a CORRUPT ledger (invalid jsonl) is an error: the hand computation
    # cannot know what the rows meant. An ABSENT ledger is zero rows —
    # the truth, never an error (glm review 2026-11-11).
    bad = tmp_path / "usage.jsonl"
    bad.write_text("{not json at all\n", encoding="utf-8")
    dep.ledger_path = str(bad)
    h, _ = harness(dep, tmp_path / "out")
    result = h.run_stage(6, live=True)
    by_name = _by_name(result)
    hand = by_name["hand-computation-from-stores"]
    assert hand["result"] == "unverified"
    assert "ledger" in json.dumps(hand["evidence"])
    assert result["verdict"] == "unverified"




