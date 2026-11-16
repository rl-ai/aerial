#!/usr/bin/env python3
"""OSS integration test: the stack runs with generic defaults only.

Asserts that (a) every top-level module imports, (b) the deployment
profile resolves to generic defaults when no config/deployment.yaml
exists, (c) email delivery is a no-op without a recipient, (d) no
deployment-specific value leaks through the profile, and (e) the
followup runner builds its re-trigger command from generic values.
"""
import importlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BANNED = ("ai-eva", "rlai.io", "morpheus", "ariel", "rl-ai",
          "/home/ai-eva", "tp-data", "a5-harness")


def _fresh_profile():
    """Import deployment_profile with no config/deployment.yaml."""
    for mod in list(sys.modules):
        if mod.startswith("deployment_profile"):
            del sys.modules[mod]
    sys.path.insert(0, str(ROOT))
    assert not (ROOT / "config" / "deployment.yaml").exists(), \
        "OSS tree must ship without a real deployment profile"
    return importlib.import_module("deployment_profile")


def test_profile_defaults_are_generic():
    dp = _fresh_profile()
    import json
    flat = json.dumps(dp.load(), default=str)
    for banned in BANNED:
        assert banned not in flat, f"generic profile contains {banned!r}"


def test_modules_import_without_profile():
    _fresh_profile()
    for name in ("dashboard.email_delivery", "dashboard.email_report",
                 "dashboard.chaos", "dashboard.exec_summary",
                 "dashboard.followup.runner", "dashboard.followup.job_tickets",
                 "integration.scheduler"):
        importlib.import_module(name)


def test_email_disabled_without_recipient():
    _fresh_profile()
    from dashboard import email_delivery as ed
    assert ed.DEFAULT_TO == ""
    # No recipient configured -> send_report is a no-op returning False.
    assert ed.send_report("test", "body") is False


def test_node_name_and_retrigger_generic():
    _fresh_profile()
    from dashboard.followup import runner as rn
    assert rn.NODE_NAME == "aerial"
    cmd = rn.retrigger_command("job-x", "TK-1")
    assert "aerial" in cmd and "morpheus" not in " ".join(cmd)
    assert Path(cmd[1]).exists() or cmd[1].endswith("job_run.py")


def test_provider_and_model_generic():
    _fresh_profile()
    from integration import run_shadow_hierarchy as rsh
    assert "morpheus" not in rsh.PROVIDER
    assert rsh.EPIC == ""


def test_source_tree_has_no_banned_tokens():
    """Scan shipped code for deployment-specific literals."""
    bad = []
    for pattern in ("*.py", "*.sh", "*.json", "*.yaml"):
        for p in ROOT.rglob(pattern):
            s = str(p)
            if "__pycache__" in s or "/tests/" in s or p.name.startswith("test_"):
                continue
            if "vram_registry.json" in s:
                continue  # data file; sanitized separately
            try:
                text = p.read_text(encoding="utf-8")
            except Exception:
                continue
            for banned in BANNED:
                if banned in text:
                    bad.append(f"{p.name}: {banned}")
    assert not bad, f"banned tokens in OSS tree: {bad}"


def test_hermetic_cycle_runs_generic():
    """The hermetic cycle suite proves the pipeline end-to-end offline."""
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "integration/test_hermetic_cycle.py",
         "-q", "-p", "no:cacheprovider"],
        cwd=ROOT, capture_output=True, text=True, timeout=600)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
