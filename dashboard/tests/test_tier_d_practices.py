"""Hermetic tests for the Kimi learning surface
(dashboard/tier_d/practices_extract.py + tier_d.kimi_learn wiring).

No LLM is contacted: the analyst is a mock, and every file write lands in
tmp_path.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "dashboard"))

import dashboard.tier_d as td  # noqa: E402


def _load():
    spec = importlib.util.spec_from_file_location(
        "tier_d_practices_extract", REPO / "dashboard" / "tier_d"
        / "practices_extract.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


pe = _load()

SIGNAL = {
    "kind": "plan",
    "task_id": "sig-learn-1",
    "sources": {
        "claude": {"ok": True, "plan": "## PLAN\n1. x",
                   "practices": ["threat-model before coding"]},
        "codex": {"ok": True, "plan": "## PLAN\n1. y",
                  "practices": ["propose a rollback per step"]},
    },
    "practices": ["propose a rollback per step", "threat-model before coding"],
}

MOCK_ANALYST_OUT = json.dumps([
    {"practice": "Threat-model before coding",
     "demonstrated_by": "claude",
     "how_kimi_applies": "Open every plan with a threat model section."},
    {"practice": "Propose a rollback per step",
     "demonstrated_by": "codex",
     "how_kimi_applies": "Each plan step names its undo."},
])


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """Mirror test_tier_d.py's fixture: point all tier_d writes at tmp."""
    monkeypatch.setattr(td, "SANDBOX_ROOT", tmp_path / "tier-d")
    monkeypatch.setattr(td, "ARCHIVE_ROOT", tmp_path / "tier-d" / "archive")
    monkeypatch.setattr(td, "LOG_FILE", tmp_path / "tier-d.log")
    monkeypatch.setattr(td, "SIGNALS_ROOT", tmp_path / "tier-d-signals")
    monkeypatch.setattr(td, "PRACTICES_DOC", tmp_path / "frontier-practices.md")
    monkeypatch.setattr(td, "KIMI_PRACTICES",
                        tmp_path / "tier_d" / "kimi-practices.md")
    monkeypatch.setenv("TIER_D_KIMI_LEARN_ANALYST", "0")
    return tmp_path


@pytest.fixture
def practices_file(tmp_path):
    return tmp_path / "tier_d" / "kimi-practices.md"


# ------------------------------------------------------------- parsing

def test_parse_entries_from_mock_analyst():
    entries = pe.parse_entries(MOCK_ANALYST_OUT)
    assert len(entries) == 2
    assert entries[0]["demonstrated_by"] == "claude"
    assert entries[0]["how_kimi_applies"]


def test_parse_entries_tolerates_garbage():
    assert pe.parse_entries("the model rambled, no JSON here") == []
    assert pe.parse_entries(None) == []
    assert pe.parse_entries('[{"practice": ""}]') == []


def test_fallback_entries_uses_frontier_practice_lines(practices_file):
    entries = pe.fallback_entries(SIGNAL)
    assert {e["practice"] for e in entries} == \
        {"threat-model before coding", "propose a rollback per step"}
    assert {e["demonstrated_by"] for e in entries} == {"claude", "codex"}


# ------------------------------------------------- extraction + append

def test_extraction_writes_dated_versioned_entries(practices_file):
    def analyst(prompt):
        assert "frontier" in prompt.lower()
        assert "sig-learn-1" in prompt          # the signal is shown
        return MOCK_ANALYST_OUT

    new = pe.extract_and_record(SIGNAL, analyst=analyst,
                                path=practices_file)
    assert len(new) == 2
    text = practices_file.read_text()
    assert "(version 1)" in text
    assert "### Threat-model before coding" in text
    assert "- Demonstrated by: claude" in text
    assert "How Kimi applies it:" in text
    # dated: the signal id + kind appear in the section header
    assert "plan signal sig-learn-1" in text


def test_file_persists_append_not_overwrite(practices_file):
    calls = {"n": 0}

    def analyst(prompt):
        calls["n"] += 1
        if calls["n"] == 1:
            return MOCK_ANALYST_OUT
        return json.dumps([
            {"practice": "Fuzz the parser before review",
             "demonstrated_by": "codex",
             "how_kimi_applies": "Generate malformed inputs first."}])

    pe.extract_and_record(SIGNAL, analyst=analyst, path=practices_file)
    second_signal = dict(SIGNAL, task_id="sig-learn-2")
    new = pe.extract_and_record(second_signal, analyst=analyst,
                                path=practices_file)
    assert [e["practice"] for e in new] == \
        ["Fuzz the parser before review"]
    text = practices_file.read_text()
    # first batch still present; both sections exist
    assert "Threat-model before coding" in text
    assert "sig-learn-1" in text and "sig-learn-2" in text
    assert text.count("### Threat-model before coding") == 1  # dedup on re-see
    assert "(version 2)" in text


def test_dedup_against_preexisting_file(practices_file):
    practices_file.parent.mkdir(parents=True)
    practices_file.write_text(
        "# Kimi accumulated practices (version 3)\n\n"
        "### threat-model   before coding\n- Demonstrated by: claude\n")
    new = pe.extract_and_record(
        SIGNAL, analyst=lambda p: MOCK_ANALYST_OUT, path=practices_file)
    assert [e["practice"] for e in new] == ["Propose a rollback per step"]
    assert "(version 4)" in practices_file.read_text()


def test_fallback_used_when_analyst_says_nothing(practices_file):
    def analyst(prompt):
        return "no JSON"
    new = pe.extract_and_record(SIGNAL, analyst=analyst,
                                path=practices_file)
    # the frontier's own PRACTICE lines are still learned (fallback)
    assert {e["practice"] for e in new} == \
        {"threat-model before coding", "propose a rollback per step"}
    text = practices_file.read_text()
    assert "threat-model before coding" in text


def test_never_raises_on_bad_signal(practices_file):
    assert pe.extract_and_record({}, analyst=lambda p: None,
                                 path=practices_file) == []


# ------------------------------------------------- tier_d.py wiring

def test_kimi_learn_writes_through_tier_d(isolated, monkeypatch):
    """tier_d._write_signal (post-consolidation) triggers the learning
    surface against the monkeypatched practices file."""
    monkeypatch.setattr(pe, "PRACTICES_PATH", td.KIMI_PRACTICES)
    monkeypatch.setattr(td.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("LLM must not be contacted")))
    # inject the loaded module where kimi_learn looks for it
    import types
    holder = types.ModuleType("tier_d_practices_extract")
    holder.extract_and_record = pe.extract_and_record
    real_spec = importlib.util.spec_from_file_location(
        "tier_d_practices_extract",
        REPO / "dashboard" / "tier_d" / "practices_extract.py")
    assert real_spec is not None and real_spec.loader is not None
    monkeypatch.setattr(importlib.util, "spec_from_file_location",
                        lambda name, loc: real_spec)
    monkeypatch.setattr(
        type(real_spec.loader), "exec_module",
        lambda self, mod: mod.__dict__.update(holder.__dict__))
    # build a signal the normal way with mocked CLI output
    fake = ("## PLAN\n1. do\nPRACTICE: decompose before ordering\n")
    def _run(cmd, cwd=None, capture_output=True, text=True, timeout=None):
        import subprocess as sp
        return sp.CompletedProcess(cmd, 0, fake, "")
    monkeypatch.setattr(td.subprocess, "run", _run)
    out = td.plan_signal("sig-wire-1", "design the widget")
    assert out["task_id"] == "sig-wire-1"
    text = td.KIMI_PRACTICES.read_text()
    assert "decompose before ordering" in text
    assert "plan signal sig-wire-1" in text
