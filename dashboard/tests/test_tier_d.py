"""Hermetic tests for dashboard/tier_d.py (mocked CLI invocations)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dashboard.tier_d as td


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """Point sandbox root / log at a temp dir and fake the CLIs."""
    monkeypatch.setattr(td, "SANDBOX_ROOT", tmp_path / "tier-d")
    monkeypatch.setattr(td, "ARCHIVE_ROOT", tmp_path / "tier-d" / "archive")
    monkeypatch.setattr(td, "LOG_FILE", tmp_path / "tier-d.log")
    monkeypatch.setattr(td, "SIGNALS_ROOT", tmp_path / "tier-d-signals")
    monkeypatch.setattr(td, "PRACTICES_DOC", tmp_path / "frontier-practices.md")
    monkeypatch.setattr(td, "KIMI_PRACTICES",
                        tmp_path / "tier_d" / "kimi-practices.md")
    monkeypatch.setenv("TIER_D_KIMI_LEARN_ANALYST", "0")
    return tmp_path


def fake_run(monkeypatch, stdout="FINDING: off-by-one\n", rc=0,
             stderr=""):
    calls = []

    def _run(cmd, cwd=None, capture_output=True, text=True, timeout=None):
        calls.append({"cmd": cmd, "cwd": cwd, "timeout": timeout})
        return subprocess.CompletedProcess(cmd, rc, stdout, stderr)

    monkeypatch.setattr(td.subprocess, "run", _run)
    return calls


# ------------------------------------------------------------- commands

def test_build_command_claude():
    t = td.TierDTask("t1", "claude", "review it", max_turns=5)
    assert td.build_command(t) == ["claude", "-p", "review it",
                                   "--output-format", "text",
                                   "--max-turns", "5"]


def test_build_command_codex():
    t = td.TierDTask("t2", "codex", "review it")
    assert td.build_command(t) == ["codex", "exec", "--skip-git-repo-check",
                                   "review it"]


def test_unknown_tool_rejected():
    with pytest.raises(ValueError):
        td.TierDTask("t3", "gemini", "hi")


# ------------------------------------------------------------- run_task

def test_run_task_success_and_collection(isolated, monkeypatch):
    calls = fake_run(monkeypatch, "FINDING: x\nNO FINDINGS after\n")
    t = td.TierDTask("task-a", "claude", "review", files={"bug.py": "1/0"})
    out = td.run_task(t)
    assert out["ok"] is True and out["exit_code"] == 0
    assert "FINDING: x" in out["output"]
    assert calls[0]["cwd"] == str(td.SANDBOX_ROOT / "task-a")
    assert calls[0]["cmd"][:2] == ["claude", "-p"]
    # output collected + sandbox wiped after archive
    assert out.get("sandbox_wiped") is True
    assert not (td.SANDBOX_ROOT / "task-a").exists()
    arc = td.ARCHIVE_ROOT / "task-a"
    assert (arc / "output.md").read_text() == "FINDING: x\nNO FINDINGS after"
    assert json.loads((arc / "meta.json").read_text())["task_id"] == "task-a"


def test_run_task_logs_session(isolated, monkeypatch):
    fake_run(monkeypatch)
    td.run_task(td.TierDTask("task-log", "codex", "hello"))
    log = (td.LOG_FILE).read_text()
    assert "task=task-log" in log and "tool=codex" in log
    assert "phash=" in log and "ok=True" in log


def test_run_task_nonzero_exit_fail_closed(isolated, monkeypatch):
    fake_run(monkeypatch, rc=3, stderr="boom")
    out = td.run_task(td.TierDTask("task-b", "claude", "hi"))
    assert out["ok"] is False and out["exit_code"] == 3


def test_run_task_timeout(isolated, monkeypatch):
    def _to(*a, **k):
        raise subprocess.TimeoutExpired(cmd=a[0], timeout=1)
    monkeypatch.setattr(td.subprocess, "run", _to)
    out = td.run_task(td.TierDTask("task-c", "codex", "hi", timeout=0.01))
    assert out["ok"] is False and "timeout" in out["stderr"]


def test_run_task_missing_cli(isolated, monkeypatch):
    def _nf(*a, **k):
        raise FileNotFoundError("claude")
    monkeypatch.setattr(td.subprocess, "run", _nf)
    out = td.run_task(td.TierDTask("task-d", "claude", "hi"))
    assert out["ok"] is False and "not found" in out["stderr"]


def test_no_wipe_keeps_sandbox(isolated, monkeypatch):
    fake_run(monkeypatch)
    td.run_task(td.TierDTask("task-e", "claude", "hi"), wipe=False)
    assert (td.SANDBOX_ROOT / "task-e" / "output.md").exists()


# --------------------------------------------------------------- batch

def test_dispatch_batch_parallel(isolated, monkeypatch):
    import threading
    seen = []

    def _run(cmd, cwd=None, **k):
        seen.append(threading.current_thread().name)
        return subprocess.CompletedProcess(cmd, 0, "ok", "")

    monkeypatch.setattr(td.subprocess, "run", _run)
    tasks = [td.TierDTask(f"p{i}", "claude" if i % 2 else "codex", "go")
             for i in range(6)]
    outs = td.dispatch_batch(tasks, workers=4)
    assert len(outs) == 6 and all(o["ok"] for o in outs)
    assert len(set(seen)) >= 2          # actually ran on >1 worker thread
    assert [o["task_id"] for o in outs] == [t.task_id for t in tasks]


def test_dispatch_batch_worker_crash_fails_closed(isolated, monkeypatch):
    def bad_run(*a, **k):
        raise RuntimeError("kaboom")
    monkeypatch.setattr(td.subprocess, "run", bad_run)
    outs = td.dispatch_batch([td.TierDTask("x1", "codex", "hi")])
    assert outs[0]["ok"] is False and "kaboom" in outs[0]["stderr"]


# ------------------------------------------------------ cross-family review

def test_second_opinion_parses_findings(isolated, monkeypatch):
    fake_run(monkeypatch, "NO FINDINGS")
    res = td.second_opinion("f-1", "data loss risk", "code")
    assert res["claude"]["findings"] == [] and res["claude"]["ok"]
    log = (td.LOG_FILE).read_text()
    assert "second_opinion finding=f-1" in log


def test_second_opinion_never_raises(isolated, monkeypatch):
    monkeypatch.setattr(td, "review_signal",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    assert td.second_opinion("f-2", "claim", "code") is None


# ------------------------------------------------------------- gauntlet L6

def test_layer6_frontier_signals(isolated, monkeypatch):
    fake_run(monkeypatch,
             "## PLAN\n1. harden\nPRACTICE: threat-model first\n"
             "FINDING: none really\n")
    root = Path(__file__).resolve().parents[2]
    res = td.layer6_frontier_signals(
        target="dashboard/followup/classifier.py", repo_root=str(root))
    assert res["ok"] is True
    assert set(res["plan"]["sources"]) == {"claude", "codex"}
    assert set(res["review"]["sources"]) == {"claude", "codex"}
    assert res["practices_new"]                # ops learning happened
    # both signals persisted for Kimi
    assert td.load_signal("gauntlet-layer6-plan")["kind"] == "plan"
    assert td.load_signal("gauntlet-layer6-review")["kind"] == "review"


def test_layer6_missing_target_fails_closed(isolated):
    res = td.layer6_frontier_signals(target="nope/x.py",
                                     repo_root="/nonexistent")
    assert res["ok"] is False and "error" in res


# ------------------------------------------------------ signal files (Kimi)

def test_plan_signal_independent_and_persisted(isolated, monkeypatch):
    calls = fake_run(monkeypatch, "## PLAN\n1. do it\nPRACTICE: draft-then-verify\n")
    sig = td.plan_signal("sig-plan-1", "design the widget")
    assert set(sig["sources"]) == {"claude", "codex"}
    assert sig["kind"] == "plan" and sig["consumer"] == "kimi"
    assert all(s["ok"] for s in sig["sources"].values())
    assert "draft-then-verify" in sig["practices"]
    # two independent dispatches, one per tool, isolated sandboxes
    assert len(calls) == 2
    assert {c["cwd"] for c in calls} == \
        {str(td.SANDBOX_ROOT / "sig-plan-1-claude"),
         str(td.SANDBOX_ROOT / "sig-plan-1-codex")}
    on_disk = json.loads((td.SIGNALS_ROOT / "sig-plan-1" /
                          "signal.json").read_text())
    assert on_disk["task_id"] == "sig-plan-1"


def test_review_signal_findings_and_calibration(isolated, monkeypatch):
    fake_run(monkeypatch, "FINDING: rce in eval\nPRACTICE: check sinks\n")
    sig = td.review_signal("sig-rev-1", "eval(input())")
    assert sig["sources"]["claude"]["findings"] == ["rce in eval"]
    assert sig["context"]["filename"] == "chunk.py"
    assert td.latest_signals(kind="review")[0]["task_id"] == "sig-rev-1"


def test_review_signal_tool_failure_recorded(isolated, monkeypatch):
    fake_run(monkeypatch, rc=1, stderr="boom")
    sig = td.review_signal("sig-rev-2", "code")
    assert sig["sources"]["claude"]["ok"] is False
    assert sig["sources"]["codex"]["ok"] is False


def test_record_practices_dedups(isolated):
    td.record_practices(["Threat-model first", "  threat-model   FIRST "],
                        "t1", "review")
    new = td.record_practices(["threat-model first", "fuzz the parser"],
                              "t2", "plan")
    assert new == ["fuzz the parser"]      # only the unseen one recorded
    doc = td.PRACTICES_DOC.read_text()
    assert doc.count("Threat-model first") == 1
    assert "fuzz the parser" in doc


# ------------------------------------------------------------------ CLI

def test_cli_plan_and_review(isolated, monkeypatch, capsys, tmp_path):
    fake_run(monkeypatch, "## PLAN\nx\nPRACTICE: p\nFINDING: f\n")
    rc = td.main(["plan", "build it", "--task-id", "cli-plan"])
    assert rc == 0
    f = tmp_path / "c.py"
    f.write_text("print(1)")
    rc = td.main(["review", "--file", f"c.py={f}", "--task-id", "cli-rev"])
    assert rc == 0
    assert td.load_signal("cli-plan")["kind"] == "plan"
    assert td.load_signal("cli-rev")["kind"] == "review"


def test_cli_frontier(isolated, monkeypatch, capsys):
    fake_run(monkeypatch, "## PLAN\n1\nPRACTICE: p\nFINDING: f\n")
    root = Path(__file__).resolve().parents[2]
    rc = td.main(["frontier", "--target", "dashboard/followup/classifier.py"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True

def test_cli_dispatch(isolated, monkeypatch, capsys):
    fake_run(monkeypatch, "hello from cli")
    rc = td.main(["dispatch", "codex", "say hello", "--task-id", "cli-x"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["task_id"] == "cli-x" and out["ok"] is True


def test_cli_dispatch_with_file(isolated, monkeypatch, capsys, tmp_path):
    fake_run(monkeypatch)
    f = tmp_path / "c.py"
    f.write_text("print(1)")
    rc = td.main(["dispatch", "claude", "review", "--file", f"c.py={f}",
                  "--task-id", "file-seed"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["task_id"] == "file-seed" and out["ok"] is True
    arc = td.ARCHIVE_ROOT / "file-seed"
    assert (arc / "meta.json").exists()


def test_cli_batch(isolated, monkeypatch, capsys, tmp_path):
    fake_run(monkeypatch)
    spec = tmp_path / "batch.json"
    spec.write_text(json.dumps([
        {"task_id": "b1", "tool": "claude", "prompt": "a"},
        {"task_id": "b2", "tool": "codex", "prompt": "b"}]))
    rc = td.main(["batch", str(spec), "--workers", "2"])
    assert rc == 0
    outs = json.loads(capsys.readouterr().out)
    assert [o["task_id"] for o in outs] == ["b1", "b2"]
