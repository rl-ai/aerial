"""Hermetic tests for the runtime-optimization capability:
dashboard/ops_context.py (probe parsers + pure decision rules) and the
runner's probe-first fix-session dispatch. No real nvidia-smi / ss /
systemctl / pgrep calls — all external output is mocked."""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard import ops_context as oc                       # noqa: E402
from dashboard.followup import runner as rn                   # noqa: E402


# ---------------------------------------------------------------------------
# Parsers (mocked tool output)
# ---------------------------------------------------------------------------

NVIDIA_SMI_OUT = "0, NVIDIA RTX PRO 6000, 40000, 98304\n1, NVIDIA RTX PRO 5000, 10000, 98304\n"


def test_parse_nvidia_smi():
    gpus = oc.parse_nvidia_smi(NVIDIA_SMI_OUT)
    assert len(gpus) == 2
    assert gpus[0]["vram_free_mib"] == 58304
    assert gpus[1]["vram_free_mib"] == 88304


def test_probe_gpus_uses_injected_run():
    gpus = oc.probe_gpus(run=lambda cmd: NVIDIA_SMI_OUT)
    assert gpus[0]["name"] == "NVIDIA RTX PRO 6000"


SS_OUT = "\n".join([
    "LISTEN 0 64 0.0.0.0:18102 0.0.0.0:* users:((\"llama-server\",pid=1111,fd=5))",
    "LISTEN 0 64 0.0.0.0:18095 0.0.0.0:* users:((\"llama-server\",pid=2222,fd=5))",
    "LISTEN 0 64 0.0.0.0:8777 0.0.0.0:* users:((\"python\",pid=3333,fd=4))",
])


def _fake_cmdline_reader(cmd_by_pid):
    return lambda pid: cmd_by_pid.get(pid)


def test_parse_ss_and_probe_seats():
    cmd_by_pid = {
        1111: ("llama-server --alias kimi-k3.1-pro-q2 --model /srv/ai/hf/models"
               "/kimi-k3.1-pro-karlex/KLX-IQ2_XXS/x.gguf -ngl 0 -c 32768 "
               "--parallel 1"),
        2222: "llama-server --alias deepseek --model /m/ds.gguf -ngl 60 -c 393216",
        3333: "python serve_console.py",
    }
    seats = oc.probe_seats(
        run=lambda cmd: SS_OUT,
        cmdline_reader=_fake_cmdline_reader(cmd_by_pid),
        vram_for=lambda pid: {1111: 100, 2222: 50000, 3333: 0}[pid])
    by_port = {s["port"]: s for s in seats}
    assert by_port[18102]["alias"] == "kimi-k3.1-pro-q2"
    assert by_port[18102]["ngl"] == 0
    assert by_port[18102]["ctx"] == 32768
    assert by_port[18095]["vram_used_mib"] == 50000
    assert 8777 not in by_port          # not a seat port / process


def test_pid_in_systemd_unit(tmp_path, monkeypatch):
    def read(pid_path):
        # emulate: 1111 in a unit, 2222 bare
        pid = int(pid_path.split("/")[2])
        return ("0::/user.slice/user@1000.service/app.slice/"
                "morpheus-x.service\n" if pid == 1111 else "0::/\n")
    monkeypatch.setattr("builtins.open",
                        lambda p, *a, **k: open(os.devnull) if False else
                        _FakeFile(read(str(p))))
    assert oc._pid_in_systemd_unit(1111)
    assert not oc._pid_in_systemd_unit(2222)


class _FakeFile:
    def __init__(self, content):
        self._c = content

    def read(self):
        return self._c

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_probe_process_units_bare_vs_unit():
    pgrep_out = "1111 llama-server --alias kimi\n2222 nohup python scheduler.py\n"
    units_out = "morpheus-kimi-k31-pro.service loaded active running Kimi\n"
    cgroups = {1111: "/user@1000.service/app.slice/morpheus-kimi-k31-pro.service",
               2222: "0::/"}
    pu = oc.probe_process_units(
        pgrep_cmd=["pgrep"], systemctl_cmd=["systemctl"],
        run=lambda cmd: pgrep_out if cmd[0] == "pgrep" else units_out,
        cgroup_reader=lambda pid: cgroups[pid])
    assert [p["pid"] for p in pu["bare"]] == [2222]
    assert [p["pid"] for p in pu["unit"]] == [1111]
    assert "morpheus-kimi-k31-pro.service" in pu["active_units"]


def test_shard_completeness():
    complete = oc.parse_shard_state(
        ["model-00001-of-00017.gguf"] + [f"model-{i:05d}-of-00017.gguf"
                                         for i in range(2, 18)])
    assert complete["complete"] and complete["shards_total"] == 17
    kimi = oc.parse_shard_state(["model-00001-of-00017.gguf",
                                 "model-00002-of-00017.gguf"])
    assert not kimi["complete"] and kimi["missing"] == [3, 4, 5, 6, 7]
    mono = oc.parse_shard_state(["bigmodel-q8.gguf"])
    assert mono["complete"] and not mono["sharded"]
    empty = oc.parse_shard_state(["README.md"])
    assert not empty["complete"]


def test_probe_model_shards(tmp_path):
    good = tmp_path / "good"
    good.mkdir()
    (good / "m-00001-of-00002.gguf").write_bytes(b"x")
    (good / "m-00002-of-00002.gguf").write_bytes(b"x")
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "m-00001-of-00017.gguf").write_bytes(b"x")
    out = oc.probe_model_shards(str(tmp_path))
    by = {m["model"]: m for m in out}
    assert by["good"]["complete"]
    assert not by["bad"]["complete"]


def test_probe_cron():
    out = oc.probe_cron(run_shell=lambda c: (
        "# comment\n*/10 * * * * ~/ai-ops-ops/sre_health_monitor.py\n"))
    assert out["count"] == 1 and "sre_health_monitor" in out["entries"][0]


# ---------------------------------------------------------------------------
# Pure decision rules
# ---------------------------------------------------------------------------

def test_ngl_for_free_vram():
    # 48 GiB free, 1 GiB weights per layer → floor(48 * 0.9) = 43 layers
    assert oc.ngl_for_free_vram(48 * 1024, 1024 * 1024 * 1024) == 43
    assert oc.ngl_for_free_vram(0, 1 << 30) == 0
    assert oc.ngl_for_free_vram(1024, 0) == 0
    # Kimi incident: near-full GPUs → CPU-only degraded mode
    assert oc.ngl_for_free_vram(100, 500 * 1024 * 1024) == 0


def test_pick_seat():
    gpus = [{"index": 0, "vram_free_mib": 58304},
            {"index": 1, "vram_free_mib": 88304}]
    seats = [{"vram_used_mib": 40000}, {"vram_used_mib": 10000}]
    assert oc.pick_seat(gpus, seats, 60000)["gpu"] == 1
    no_fit = oc.pick_seat(gpus, seats, 90000)
    assert no_fit["gpu"] is None and "no GPU" in no_fit["reason"]


def test_context_split():
    ok = oc.context_split(32768, 1, 30000, 2000)
    assert ok["fits"] and ok["per_slot"] == 32768
    bad = oc.context_split(32768, 2, 20000, 8000)
    assert not bad["fits"] and bad["per_slot"] == 16384
    assert "raise" in bad["reason"] or "lower" in bad["reason"]
    assert not oc.context_split(32768, 0, 1, 1)["ok"]


def test_systemd_conversion_candidates():
    pu = {"bare": [{"pid": 7, "cmdline": "nohup python scheduler.py"}],
          "unit": [{"pid": 8, "cmdline": "llama-server"}]}
    cands = oc.systemd_conversion_candidates(pu)
    assert len(cands) == 1 and cands[0]["pid"] == 7
    assert "systemd user unit" in cands[0]["action"]


def test_monitor_budget():
    assert oc.monitor_budget_checks(3, 5)["over_budget"] is False
    over = oc.monitor_budget_checks(7, 5)
    assert over["over_budget"] and "group" in over["reason"]


# ---------------------------------------------------------------------------
# Runner wiring: refresh + probe-first TASK.md
# ---------------------------------------------------------------------------

class _Finding:
    finding_id = "seat:kimi:20261115"
    claim = "kimi seat down"
    playbook = "play"

    def task_text(self, mandate=None):
        return f"Finding: {self.finding_id}\nplaybook\n"


def test_dispatch_fix_session_probe_first(tmp_path):
    cmds = []

    def fake_refresh(ws):
        return {"exit": 0}

    ctx = {"gpus": [{"index": 0, "vram_free_mib": 58304}]}
    result = rn.dispatch_fix_session(
        _Finding(), None, workspace_root=str(tmp_path),
        command=["true"], timeout_s=5,
        refresh=fake_refresh, ops_context_fn=lambda: ctx)
    task = open(result["task_md"]).read()
    assert "python3 -m dashboard.ops_context" in task
    assert "Probe first" in task
    assert "docs/runtime-optimization.md" in task
    assert json.loads(task.split("```json\n")[1].split("\n```")[0]) == ctx
    assert "playbook" in task
    assert result["refresh"] == {"exit": 0}


def test_dispatch_fix_session_probe_failure_is_nonfatal(tmp_path):
    result = rn.dispatch_fix_session(
        _Finding(), None, workspace_root=str(tmp_path),
        command=["true"], timeout_s=5,
        refresh=lambda ws: {"exit": 1},
        ops_context_fn=lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    task = open(result["task_md"]).read()
    assert "RuntimeError('boom')" in task


def test_sandbox_refresh_script(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    os.system(f"cd {src} && git init -q && git config user.email a@b && "
              f"git config user.name a && echo hi > f && git add f && "
              f"git commit -qm init")
    ws = tmp_path / "ws"
    script = os.path.join(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))), "scripts",
        "sandbox-refresh.sh")
    ret = os.system(f"bash {script} {ws} mine:{src} >/dev/null 2>&1")
    assert os.path.exists(ws / "mine" / "f")
    assert os.path.exists(ws / "refresh.log")
    assert (ws / "refresh.log").read_text().count("ok: clone mine") == 1
    assert ret == 0


def test_collect_context_shape():
    ctx = oc.collect_context()
    assert set(ctx) >= {"generated_at", "gpus", "seats", "process_units",
                        "model_shards", "cron", "summary"}
