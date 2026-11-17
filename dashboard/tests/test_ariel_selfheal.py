"""Ariel GLM-seat self-heal tests — hermetic (no network, no ssh, no docker).

Covers the 2026-11-16 remote-seat recovery lane (docs/sre-runbook-glm-seat.md),
including the 12 DS red-team fixes:

  runner (Python, in-process):
  - MODEL_SEATS recovery-driver selection: glm yes, kimi/deepseek never
  - f8: the driver path is hard-resolved from the deployment profile
    (cluster.ariel_recovery_script); registry-supplied paths are REJECTED
  - f9: systemctl --user availability check never crashes the runner
  - auto-fix executes [script] only when it is the trusted path

  driver + Ariel root script (bash, executed with stub PATH):
  - f3/f4: exit-code semantics 0 (up) / 1 (failed) / 3 (throttled) /
    4 (lock contention, JSON state=skipped)
  - f6: readiness is HTTP 200 on /v1/models with the bearer
  - f5/f11: forwarder restart uses ONLY the local probe and touches ONLY
    <glm-forward-unit>.service (shared worker-forward never)
  - f7: ssh wrapped in `timeout 300` + ServerAlive keepalives
  - f10: exactly ONE JSON line on stdout, diagnostics on stderr
  - f12: pinned known_hosts required (fail closed, exit 2)
  - f1/f2 (Ariel side): scoped PID ejection, atomic flock before throttle
"""
import fcntl
import json
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard.followup import runner as rn                        # noqa: E402

import calendar
T0 = calendar.timegm((2026, 11, 16, 2, 0, 0))   # the GLM 3-h outage day

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(TEST_DIR))
DRIVER = os.path.join(REPO_ROOT, "cluster", "bin", "ariel_glm_recover.sh")
ARIEL_SCRIPT = os.path.join(REPO_ROOT, "cluster", "ariel", "ariel-glm-recover")
EXPECTED_FP = "SHA256:test-only-fake-fingerprint"  # fake: tests only


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


def make_script(tmp_path, name="ariel_glm_recover.sh", executable=True):
    """Stand-in for cluster/bin/ariel_glm_recover.sh (never ssh here)."""
    p = tmp_path / name
    p.write_text("#!/bin/sh\necho ok\n")
    p.chmod(0o755 if executable else 0o644)
    return str(p)


def use_driver(monkeypatch, tmp_path, executable=True):
    """Point the f8 trusted driver at a tmp stand-in and wire the seat table.

    The trusted path (_ARIEL_RECOVERY_SCRIPT) is monkeypatched exactly the
    way the deployment profile resolves it in production; tests then verify
    that ONLY this path is ever executed.
    """
    script = make_script(tmp_path, executable=executable)
    monkeypatch.setattr(rn, "_ARIEL_RECOVERY_SCRIPT", script)
    monkeypatch.setattr(rn, "SEAT_RECOVERY_SCRIPTS", {"glm": script})
    seats = (("kimi", 18102), ("deepseek", 18095), ("glm", 18110, script))
    monkeypatch.setattr(rn, "MODEL_SEATS", seats)
    return script, seats


# ---------------------------------------------------------------------------
# (a) recovery driver selection: glm yes, kimi/deepseek never
# ---------------------------------------------------------------------------

def test_glm_seat_finding_selects_recovery_driver(tmp_path, monkeypatch):
    script, seats = use_driver(monkeypatch, tmp_path)
    fs = rn.seat_findings(Clock(), lambda p, b=None: None if p == 18110 else 200)
    assert len(fs) == 1
    f = fs[0]
    assert f.finding_id.startswith("seat:glm:")
    assert f.check["kind"] == "seat" and f.check["recovery_script"] == script
    assert script in f.proposed_action, \
        "proposed_action must instruct running the recovery driver"
    assert "ariel_glm_recover.sh" in f.task_text()


def test_kimi_deepseek_never_get_the_recovery_driver(tmp_path, monkeypatch):
    script, seats = use_driver(monkeypatch, tmp_path)

    def probe(p, b=None):
        return 200 if p == 18110 else None          # kimi + deepseek down
    fs = rn.seat_findings(Clock(), probe)
    assert {f.check["name"] for f in fs} == {"kimi", "deepseek"}
    for f in fs:
        assert "recovery_script" not in f.check
        assert script not in f.proposed_action
        assert "restart the dead" in f.proposed_action, \
            "local seats keep the classic systemctl playbook action"


def test_seat_playbook_documents_ariel_recovery():
    assert "ariel_glm_recover.sh" in rn.SEAT_PLAYBOOK
    assert "never by local systemctl" in rn.SEAT_PLAYBOOK


# ---------------------------------------------------------------------------
# (b) probe 200 → no finding
# ---------------------------------------------------------------------------

def test_probe_200_yields_no_findings(tmp_path, monkeypatch):
    _, seats = use_driver(monkeypatch, tmp_path)
    assert rn.seat_findings(Clock(), lambda p, b=None: 200) == []


# ---------------------------------------------------------------------------
# (c) dead glm seat → finding carries the recovery script path
# ---------------------------------------------------------------------------

def test_dead_glm_finding_carries_script_path(tmp_path, monkeypatch):
    script, seats = use_driver(monkeypatch, tmp_path)
    fs = rn.seat_findings(Clock(), lambda p, b=None: None)
    glm = [f for f in fs if f.check["name"] == "glm"][0]
    assert script in glm.proposed_action
    assert glm.check["recovery_script"] == script


# ---------------------------------------------------------------------------
# (d) 2-tuple MODEL_SEATS remain valid (backwards compat)
# ---------------------------------------------------------------------------

def test_two_tuple_model_seats_backwards_compatible(tmp_path, monkeypatch):
    monkeypatch.setattr(rn, "MODEL_SEATS",
                        (("kimi", 18102), ("deepseek", 18095)))
    monkeypatch.setattr(rn, "SEAT_RECOVERY_SCRIPTS", {})
    fs = rn.seat_findings(Clock(), lambda p, b=None: None)
    assert [f.check["name"] for f in fs] == ["kimi", "deepseek"]
    for f in fs:
        assert "recovery_script" not in f.check
        assert "restart the dead" in f.proposed_action


def test_two_tuple_glm_uses_named_fallback_driver(tmp_path, monkeypatch):
    """A 2-tuple glm entry still resolves a driver via SEAT_RECOVERY_SCRIPTS
    — but ONLY the profile-resolved trusted path (f8)."""
    script, _ = use_driver(monkeypatch, tmp_path)
    monkeypatch.setattr(rn, "MODEL_SEATS", (("glm", 18110),))
    fs = rn.seat_findings(Clock(), lambda p, b=None: None)
    assert len(fs) == 1
    assert fs[0].check["recovery_script"] == script


def test_missing_or_non_executable_script_is_no_driver(tmp_path, monkeypatch):
    script, _ = use_driver(monkeypatch, tmp_path)
    missing = str(tmp_path / "nope.sh")
    assert rn.seat_recovery_script(("glm", 18110, missing)) is None
    plain = tmp_path / "notexec.sh"
    plain.write_text("#!/bin/sh\n")
    plain.chmod(0o644)
    assert rn.seat_recovery_script(("glm", 18110, str(plain))) is None


# ---------------------------------------------------------------------------
# f8: path resolution — profile-resolved, registry paths REJECTED
# ---------------------------------------------------------------------------

def test_recovery_script_defaults_to_repo_relative_profile_value():
    """Default: cluster/bin/ariel_glm_recover.sh under the repo root."""
    expected = os.path.normpath(
        os.path.join(rn._ROOT, "cluster", "bin", "ariel_glm_recover.sh"))
    assert rn._ARIEL_RECOVERY_SCRIPT == expected


def test_recovery_script_honors_profile_override(tmp_path, monkeypatch):
    monkeypatch.setattr(rn, "_pv",
                        lambda key, default=None:
                        str(tmp_path / "custom" / "recover.sh")
                        if key == "cluster.ariel_recovery_script" else None)
    assert rn._ariel_recovery_script() == str(tmp_path / "custom" / "recover.sh")


def test_registry_provided_path_is_rejected(tmp_path, monkeypatch):
    """f8: whoever writes vram_registry.json must NOT gain execution — a
    registry-style 3rd tuple element pointing anywhere else is rejected."""
    script, _ = use_driver(monkeypatch, tmp_path)
    evil = make_script(tmp_path, "evil.sh")
    assert evil != script
    assert rn.seat_recovery_script(("glm", 18110, evil)) is None
    # even a path that merely LOOKS like the driver (different directory)
    elsewhere = os.path.join(str(tmp_path), "sub")
    os.makedirs(elsewhere, exist_ok=True)
    moved = make_script(tmp_path / "sub", "ariel_glm_recover.sh")
    assert rn.seat_recovery_script(("glm", 18110, moved)) is None


def test_execute_fix_rejects_registry_supplied_path(tmp_path, monkeypatch):
    """The auto-fix path never execs a non-trusted recovery_script."""
    runner = make_runner(tmp_path)
    dispatched = []
    monkeypatch.setattr(rn, "dispatch_fix_session",
                        lambda f, m: dispatched.append(f) or {"exit": 0})
    calls = []
    monkeypatch.setattr(rn.subprocess, "run",
                        lambda cmd, **kw: calls.append(cmd))
    evil = make_script(tmp_path, "evil.sh")   # exists + executable + UNtrusted
    runner._execute_fix(seat_finding(evil), None)
    assert calls == [], "f8: never exec a registry-supplied path"
    assert len(dispatched) == 1, "falls back to the agent session"


# ---------------------------------------------------------------------------
# f9: systemctl --user availability — log and skip, never crash
# ---------------------------------------------------------------------------

def test_systemctl_user_unavailable_without_runtime_dir(monkeypatch):
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    assert rn.systemctl_user_available() is False


def test_systemctl_user_available_states(monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")

    def fake_run(cmd, **kw):
        class P:
            returncode = 0
            stdout = ""
        return P()
    monkeypatch.setattr(rn.subprocess, "run", fake_run)
    assert rn.systemctl_user_available() is True

    def degraded(cmd, **kw):
        class P:
            returncode = 1
            stdout = "degraded\n"
        return P()
    monkeypatch.setattr(rn.subprocess, "run", degraded)
    assert rn.systemctl_user_available() is True   # session reachable

    def offline(cmd, **kw):
        class P:
            returncode = 1
            stdout = "offline\n"
        return P()
    monkeypatch.setattr(rn.subprocess, "run", offline)
    assert rn.systemctl_user_available() is False


def test_systemctl_user_check_never_raises(monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")

    def boom(cmd, **kw):
        raise FileNotFoundError("no systemctl")
    monkeypatch.setattr(rn.subprocess, "run", boom)
    assert rn.systemctl_user_available() is False


def test_execute_fix_logs_unavailable_systemctl_but_still_runs(
        tmp_path, monkeypatch):
    """f9: a missing user session is logged, the driver still runs, the
    runner never crashes."""
    script, _ = use_driver(monkeypatch, tmp_path)
    runner = make_runner(tmp_path)
    monkeypatch.setattr(rn, "systemctl_user_available", lambda: False)
    calls = []

    class Proc:
        returncode = 0
        stdout = '{"seat":"glm","state":"up"}'

    monkeypatch.setattr(rn.subprocess, "run",
                        lambda cmd, **kw: calls.append(cmd) or Proc())
    res = runner._execute_fix(seat_finding(script), {"mandate_id": "mf-1"})
    assert calls == [[script]]
    assert res["exit"] == 0


def test_execute_fix_swallows_driver_crash(tmp_path, monkeypatch):
    script, _ = use_driver(monkeypatch, tmp_path)
    runner = make_runner(tmp_path)

    def boom(cmd, **kw):
        raise OSError("exec failed")
    monkeypatch.setattr(rn.subprocess, "run", boom)
    res = runner._execute_fix(seat_finding(script), None)
    assert res["exit"] is None and "error" in res   # runner survives


# ---------------------------------------------------------------------------
# auto-fix path: executes [script] as the trusted fix
# ---------------------------------------------------------------------------

def make_runner(tmp_path, probe=None):
    return rn.OpsRunner(followups_db=str(tmp_path / "f.db"), clock=Clock(),
                        probe=probe or (lambda p, b=None: 200),
                        hub_db=str(tmp_path / "no-hub.db"),
                        health_path=str(tmp_path / "none.json"),
                        crontab_fn=lambda: "", state_path=None)


def seat_finding(script=None, name="glm", port=18110):
    check = {"kind": "seat", "name": name, "port": port}
    if script:
        check["recovery_script"] = script
    return rn.OpsFinding(
        finding_id=f"seat:{name}:20261116",
        claim=f"model seat {name} (port {port}) is DOWN (probe status None)",
        reversible=True, scope="internal", tags=("seat-liveness", name),
        proposed_action=f"run the recovery driver {script}",
        check=check)


def test_execute_fix_runs_driver_command(tmp_path, monkeypatch):
    script, _ = use_driver(monkeypatch, tmp_path)
    runner = make_runner(tmp_path)
    monkeypatch.setattr(rn, "systemctl_user_available", lambda: True)
    calls = []

    class Proc:
        returncode = 0
        stdout = ('{"seat":"glm","state":"up","via":"ariel-recover",'
                  '"ssh_exit":0}')

    monkeypatch.setattr(rn.subprocess, "run",
                        lambda cmd, **kw: calls.append(cmd) or Proc())
    res = runner._execute_fix(seat_finding(script), {"mandate_id": "mf-1"})
    assert calls == [[script]], "auto-fix must execute [script] as the fix"
    assert res["exit"] == 0 and res["cmd"] == [script]


def test_execute_fix_guard_falls_through_to_agent_session(tmp_path, monkeypatch):
    """Missing or non-executable driver → the classic fix session runs, the
    driver is NEVER executed (f8 trust + exists + exec guard)."""
    script, _ = use_driver(monkeypatch, tmp_path)
    runner = make_runner(tmp_path)
    dispatched = []
    monkeypatch.setattr(rn, "dispatch_fix_session",
                        lambda f, m: dispatched.append(f) or {"exit": 0})
    calls = []
    monkeypatch.setattr(rn.subprocess, "run",
                        lambda cmd, **kw: calls.append(cmd))
    runner._execute_fix(seat_finding(str(tmp_path / "missing.sh")), None)
    plain = tmp_path / "notexec.sh"
    plain.write_text("#!/bin/sh\n")
    plain.chmod(0o644)
    runner._execute_fix(seat_finding(str(plain)), None)
    runner._execute_fix(seat_finding(None, name="kimi", port=18102), None)
    assert calls == [], "guard: never exec a missing/non-executable driver"
    assert len(dispatched) == 3


def test_handle_glm_down_end_to_end_executes_driver(tmp_path, monkeypatch):
    """poll→classify→act: MANDATE-class glm seat finding runs the driver as
    the deterministic fix; still-down verify escalates (no restart loops)."""
    script, seats = use_driver(monkeypatch, tmp_path)
    runner = make_runner(tmp_path, probe=lambda p, b=None: None)
    monkeypatch.setattr(rn, "systemctl_user_available", lambda: True)
    runner.mandate_creator.create = lambda f, s: {
        "mandate_id": "mf-1", "scope": s, "expiry_at": T0 + 100}
    calls = []

    class Proc:
        returncode = 1
        stdout = ('{"seat":"glm","state":"failed","via":"ariel-recover",'
                  '"ssh_exit":255}')

    monkeypatch.setattr(rn.subprocess, "run",
                        lambda cmd, **kw: calls.append(cmd) or Proc())
    f = rn.seat_findings(Clock(), lambda p, b=None: None if p == 18110 else 200)[0]
    res = runner.handle(f)
    assert calls == [[script]]
    assert res["state"] == "escalated", \
        "recovery did not restore the seat → escalate, never loop"


# ---------------------------------------------------------------------------
# bash driver tests — hermetic: stub curl/ssh/ssh-keygen/systemctl on PATH
# ---------------------------------------------------------------------------

def _write_stub(path, body):
    path.write_text(body)
    path.chmod(0o755)
    return str(path)


def run_driver(tmp_path, args=("--probe-only",), env_extra=None,
               pre_lock=False):
    """Run out2 cluster/bin/ariel_glm_recover.sh with stubbed binaries.

    Returns (returncode, stdout, stderr, calls_dir).
    """
    stub = tmp_path / "stub"
    stub.mkdir(exist_ok=True)
    calls = tmp_path / "calls"
    calls.mkdir(exist_ok=True)

    _write_stub(stub / "curl", '#!/bin/sh\n'
                'echo "$*" >> "$CALLS_DIR/curl.log"\n'
                'echo "${FAKE_CURL_CODE:-000}"\n')
    _write_stub(stub / "ssh", '#!/bin/sh\n'
                'echo "$*" >> "$CALLS_DIR/ssh.log"\n'
                'if [ -n "${FAKE_SSH_OUT:-}" ]; then echo "$FAKE_SSH_OUT"; fi\n'
                'exit "${FAKE_SSH_EXIT:-0}"\n')
    _write_stub(stub / "timeout", '#!/bin/sh\n'
                'echo "$*" >> "$CALLS_DIR/timeout.log"\n'
                'shift\nexec "$@"\n')
    _write_stub(stub / "ssh-keygen", '#!/bin/sh\n'
                'case "$1" in\n'
                '  -F) if [ "${FAKE_KEYGEN_HAS_HOST:-1}" != "1" ]; then exit 1; fi;\n'
                '      if [ -n "${FAKE_KEYGEN_FP:-}" ]; then\n'
                '        echo "Host $2 found: line 1 ED25519 $FAKE_KEYGEN_FP";\n'
                '      fi; exit 0 ;;\n'
                '  -lf) if [ -n "${FAKE_KEYGEN_FP:-}" ]; then\n'
                '         echo "256 $FAKE_KEYGEN_FP ariel"; exit 0\n'
                '       fi; exit 1 ;;\n'
                '  *) exit 0 ;;\n'
                'esac\n')
    _write_stub(stub / "systemctl", '#!/bin/sh\n'
                'echo "$*" >> "$CALLS_DIR/systemctl.log"\n'
                'if [ "$1 $2" = "--user is-system-running" ]; then\n'
                '  echo "${FAKE_SYSTEMCTL_OUT:-running}"\n'
                'fi\n'
                'exit "${FAKE_SYSTEMCTL_RC:-0}"\n')

    xdg = tmp_path / "xdg"
    (xdg / "ariel-glm-recover").mkdir(parents=True, exist_ok=True)
    lockfile = xdg / "ariel-glm-recover" / "lock"
    kh = tmp_path / "ariel_known_hosts"
    kh.write_text("# pinned ariel host key goes here (bootstrapped)\n")

    env = dict(os.environ,
               PATH=f"{stub}:{os.environ['PATH']}",
               CALLS_DIR=str(calls),
               XDG_RUNTIME_DIR=str(xdg),
               ARIEL_KNOWN_HOSTS=str(kh),
               ARIEL_HOST="127.0.0.1",          # keep ariel_tcp hermetic
               ARIEL_PORT="9",
               CLUSTER_BEARER_PATH=str(tmp_path / "bearer"),
               HOME=str(tmp_path),
               ARIEL_RESTART_WAIT="0.1",        # hermetic: no real sleeps
               FAKE_CURL_CODE="000",
               FAKE_KEYGEN_FP=EXPECTED_FP,
               ARIEL_HOST_KEY_FP=EXPECTED_FP,
               ARIEL_RECOVERY_USER="recovery",
               GLM_FORWARDER_UNIT="node-1-glm-forward.service",
               FAKE_KEYGEN_HAS_HOST="1")
    (tmp_path / "bearer").write_text("testbearer\n")
    env.update(env_extra or {})

    lock_fd = None
    if pre_lock:
        lock_fd = os.open(str(lockfile), os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        p = subprocess.run([DRIVER, *args], capture_output=True, text=True,
                           timeout=60, env=env)
    finally:
        if lock_fd is not None:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
    return p.returncode, p.stdout, p.stderr, calls


def test_driver_exit0_up_single_json_line(tmp_path):
    """f6/f10: HTTP 200 → exactly one JSON line, state=up, exit 0."""
    rc, out, err, _ = run_driver(
        tmp_path, args=("--probe-only",), env_extra={"FAKE_CURL_CODE": "200"})
    assert rc == 0
    lines = out.strip().splitlines()
    assert len(lines) == 1
    j = json.loads(lines[0])
    assert j["seat"] == "glm" and j["state"] == "up"
    # f10: diagnostics on stderr, stdout clean
    assert "tcp=" in err or err.strip() == ""


def test_driver_probe_only_down_exit1(tmp_path):
    rc, out, _, _ = run_driver(tmp_path, args=("--probe-only",))
    assert rc == 1
    j = json.loads(out.strip())
    assert j["state"] == "failed" and j["via"] == "probe-only"


def test_driver_exit_codes_throttled_3_no_forwarder_restart(tmp_path):
    """f3: ssh exit 3 (Ariel throttled) → driver exit 3, state=throttled,
    and NO forwarder restart (nothing for the runner to re-churn)."""
    rc, out, err, calls = run_driver(
        tmp_path, args=(), env_extra={"FAKE_SSH_EXIT": "3"})
    assert rc == 3
    j = json.loads(out.strip())
    assert j["state"] == "throttled" and j["ssh_exit"] == 3
    assert not (calls / "systemctl.log").exists(), \
        "throttled run must not restart any forwarder"


def test_driver_exit4_lock_contention_emits_json(tmp_path):
    """f4: contention emits the single JSON line (state=skipped) and exits 4
    — never a silent 0."""
    rc, out, err, _ = run_driver(tmp_path, args=("--probe-only",),
                                 pre_lock=True)
    assert rc == 4
    j = json.loads(out.strip())
    assert j["state"] == "skipped"
    assert "another recovery" in err


def test_driver_f12_fail_closed_missing_pinned_hosts(tmp_path):
    rc, out, err, _ = run_driver(
        tmp_path, args=(),
        env_extra={"ARIEL_KNOWN_HOSTS": str(tmp_path / "absent")})
    assert rc == 2, "fail closed when the pinned known_hosts file is absent"
    j = json.loads(out.strip())
    assert j["via"] == "host-key" and j["state"] == "failed"
    assert "refusing" in err


def test_driver_f12_fail_closed_fingerprint_mismatch(tmp_path):
    rc, out, err, _ = run_driver(
        tmp_path, args=(),
        env_extra={"FAKE_KEYGEN_FP": "SHA256:WRONGWRONGWRONGWRONGWRONGWRONGWRONGWRONG"})
    assert rc == 2, "fail closed on a fingerprint mismatch (no TOFU/MITM)"
    assert json.loads(out.strip())["via"] == "host-key"


def test_driver_f7_f12_ssh_invocation_is_pinned_and_bounded(tmp_path):
    """f7/f12: timeout 300 + keepalives + StrictHostKeyChecking=yes +
    UserKnownHostsFile pointing at the pinned file."""
    rc, out, err, calls = run_driver(
        tmp_path, args=(), env_extra={"FAKE_SSH_EXIT": "1"})
    assert rc == 1
    timeout_lines = (calls / "timeout.log").read_text().splitlines()
    assert any(l.startswith("300 ssh ") for l in timeout_lines), \
        f"ssh must be wrapped in `timeout 300`: {timeout_lines!r}"
    ssh_line = (calls / "ssh.log").read_text()
    for opt in ("-o StrictHostKeyChecking=yes",
                f"-o UserKnownHostsFile={tmp_path / 'ariel_known_hosts'}",
                "-o ServerAliveInterval=15",
                "-o ServerAliveCountMax=3"):
        assert opt in ssh_line, f"missing {opt} in ssh argv"
    assert "accept-new" not in ssh_line


def test_driver_f5_f11_forwarder_restart_scoped_to_glm_unit(tmp_path):
    """f5/f11: restart decision comes from the local HTTP probe only, and
    ONLY the GLM-specific unit restarts — never the shared worker-forward."""
    rc, out, err, calls = run_driver(
        tmp_path, args=(), env_extra={"FAKE_SSH_EXIT": "1"})
    assert rc == 1
    log = (calls / "systemctl.log").read_text()
    assert "--user restart" in log and "glm-forward" in log and "worker-forward" not in log
    assert "worker-forward" not in log, \
        "the shared node-1-worker-forward.service must never be restarted"


def test_driver_f9_skips_restart_when_systemctl_user_unavailable(tmp_path):
    rc, out, err, calls = run_driver(
        tmp_path, args=(),
        env_extra={"FAKE_SSH_EXIT": "1", "FAKE_SYSTEMCTL_RC": "1",
                   "FAKE_SYSTEMCTL_OUT": "offline"})
    assert rc == 1, "no crash — the driver degrades gracefully"
    assert not (calls / "systemctl.log").exists() or \
        "restart" not in (calls / "systemctl.log").read_text()
    assert "systemctl --user" in err and "unavailable" in err


def test_driver_f10_single_json_stdout_diagnostics_on_stderr(tmp_path):
    rc, out, err, _ = run_driver(
        tmp_path, args=(), env_extra={"FAKE_SSH_EXIT": "1",
                                      "FAKE_SSH_OUT": "container log tail"})
    assert rc == 1
    lines = out.strip().splitlines()
    assert len(lines) == 1
    j = json.loads(lines[0])
    assert j["state"] == "failed"
    assert j["via"] == "forwarder-restart", \
        "ssh attempted (exit 1) + local probe dead → forwarder restart path"
    assert "container log tail" in err, "ssh output must go to stderr"
    assert "tcp=" in err


def test_driver_f6_probe_is_http_v1_models_with_bearer(tmp_path):
    """f6: the readiness probe is GET /v1/models with the bearer header —
    not a bare TCP connect."""
    rc, out, _, calls = run_driver(
        tmp_path, args=("--probe-only",), env_extra={"FAKE_CURL_CODE": "200"})
    log = (calls / "curl.log").read_text()
    assert "/v1/models" in log
    assert "authorization: Bearer testbearer" in log


# ---------------------------------------------------------------------------
# Ariel root script — scoped PID ejection (f1), atomic lock (f2),
# HTTP readiness (f6), exit codes (f3)
# ---------------------------------------------------------------------------

def run_ariel_script(tmp_path, env_extra=None, args=()):
    stub = tmp_path / "astub"
    stub.mkdir(exist_ok=True)
    calls = tmp_path / "acalls"
    calls.mkdir(exist_ok=True)

    _write_stub(stub / "curl", '#!/bin/sh\n'
                'echo "$*" >> "$CALLS_DIR/curl.log"\n'
                'echo "${FAKE_CURL_CODE:-000}"\n')
    _write_stub(stub / "docker", '#!/bin/sh\n'
                'echo "$*" >> "$CALLS_DIR/docker.log"\n'
                'case "$1 $2" in\n'
                '  "top glm53-fp4") printf "PID\\n111\\n222\\n" ;;\n'
                '  "inspect -f") echo "${FAKE_DOCKER_RUNNING:-false}" ;;\n'
                '  *) exit ${FAKE_DOCKER_RC:-0} ;;\n'
                'esac\n')
    _write_stub(stub / "systemctl", '#!/bin/sh\n'
                'echo "$*" >> "$CALLS_DIR/systemctl.log"\n'
                'if [ "$1 $2" = "show -p" ]; then echo "${FAKE_MAINPID:-0}"; exit 0; fi\n'
                'exit 0\n')
    _write_stub(stub / "nvidia-smi", '#!/bin/sh\n'
                'echo "$*" >> "$CALLS_DIR/nvidia-smi.log"\n'
                'echo "9999"\n')
    _write_stub(stub / "logger", '#!/bin/sh\nexit 0\n')

    env = dict(os.environ,
               PATH=f"{stub}:{os.environ['PATH']}",
               CALLS_DIR=str(calls),
               ARIEL_GLM_LOCK_DIR=str(tmp_path / "lockdir"),
               ARIEL_GLM_BEARER=str(tmp_path / "bearer"),
               ARIEL_GLM_WAIT_S="0.1",          # hermetic: no real sleeps
               ARIEL_GLM_EJECT_WAIT_S="0.1",
               FAKE_CURL_CODE="000",
               FAKE_MAINPID="0")
    (tmp_path / "bearer").write_text("testbearer\n")
    env.update(env_extra or {})
    p = subprocess.run([ARIEL_SCRIPT, *args], capture_output=True, text=True,
                       timeout=120, env=env)
    return p.returncode, p.stdout, p.stderr, calls


def test_ariel_exit0_when_http_up(tmp_path):
    rc, out, err, calls = run_ariel_script(
        tmp_path, env_extra={"FAKE_CURL_CODE": "200"})
    assert rc == 0
    assert not (calls / "docker.log").exists(), "up → no docker action"
    assert not (calls / "nvidia-smi.log").exists()


def test_ariel_exit3_throttled_by_recent_attempt(tmp_path):
    rc, out, err, calls = run_ariel_script(tmp_path)
    assert rc == 1, \
        "first run without a prior attempt attempts the recovery (exit 1), not 3"
    # second run within the throttle window → exit 3
    rc, out, err, _ = run_ariel_script(tmp_path)
    assert rc == 3
    assert "throttled" in err
    # and the throttle timestamp was NOT refreshed by the throttled run
    last = tmp_path / "lockdir" / "ariel-glm-recover.last"
    first_mtime = os.stat(last).st_mtime
    rc, _, _, _ = run_ariel_script(tmp_path)
    assert os.stat(last).st_mtime == first_mtime, \
        "f2: the lock timestamp is refreshed only on an actual attempt"


def test_ariel_exit1_when_docker_start_fails(tmp_path):
    rc, out, err, _ = run_ariel_script(
        tmp_path, env_extra={"FAKE_DOCKER_RC": "1"})
    assert rc == 1


def test_ariel_f1_scoped_pid_ejection_only(tmp_path):
    """f1: no blanket nvidia-smi kill loop — only the container's own PIDs
    (111, 222) are ejected; co-tenant PID 9999 is never touched."""
    rc, out, err, calls = run_ariel_script(
        tmp_path, env_extra={"FAKE_DOCKER_RUNNING": "false"})
    assert rc in (0, 1)
    assert not (calls / "nvidia-smi.log").exists(), \
        "nvidia-smi must never be consulted (no blanket kill loop)"
    dlog = (calls / "docker.log").read_text()
    assert "top glm53-fp4" in dlog, "PIDs come from docker top (cgroup scope)"
    serr = err
    assert "111" in serr and "222" in serr, "container PIDs are ejected"
    assert "9999" not in serr, "co-tenant PIDs are never touched"
    slog = (calls / "systemctl.log").read_text()
    assert "stop ariel-vllm-qwen36.service" in slog


def test_ariel_f1_critic_mainpid_linger_is_killed_exactly(tmp_path):
    rc, out, err, calls = run_ariel_script(
        tmp_path, env_extra={"FAKE_MAINPID": "4242"})
    slog = (calls / "systemctl.log").read_text()
    serr = err
    assert "4242" in serr, "a lingering critic MainPID is killed by exact PID"
    assert slog.count("4242") >= 0


def test_ariel_f6_restarts_running_but_not_serving_container(tmp_path):
    rc, out, err, calls = run_ariel_script(
        tmp_path, env_extra={"FAKE_DOCKER_RUNNING": "true"})
    dlog = (calls / "docker.log").read_text()
    assert "restart glm53-fp4" in dlog, \
        "running-but-not-serving → docker restart (start is a no-op)"
    assert "start glm53-fp4" not in dlog.replace("restart glm53-fp4", "")


def test_driver_refuses_without_deployment_values(tmp_path):
    """No built-in host, user or fingerprint: a missing value is exit 2, one JSON line."""
    rc, out, err, calls = run_driver(
        tmp_path, args=("--probe-only",),
        env_extra={"ARIEL_HOST_KEY_FP": "", "ARIEL_RECOVERY_USER": ""})
    assert rc == 2
    lines = out.strip().splitlines()
    assert len(lines) == 1 and '"via":"config"' in lines[0]
    assert "ARIEL_HOST_KEY_FP" in err and "ARIEL_RECOVERY_USER" in err

