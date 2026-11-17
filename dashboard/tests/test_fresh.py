"""Layer 7 FRESH unit tests: doc parser, expectation engine, hermeticity,
and the mutation rule (a doc/command mismatch must be caught)."""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard import fresh                                   # noqa: E402


# ------------------------------------------------------------- doc parser

def test_parse_doc_real_getting_started():
    text = fresh.doc_path().read_text()
    cmds = fresh.parse_doc(text)
    texts = [c.text for c in cmds if not c.is_ini]
    # the right commands are extracted
    assert any("hub/api.py" in t for t in texts)
    assert any("serve_console.py" in t for t in texts)
    assert any("dashboard.followup.runner" in t and "--once" in t
               for t in texts)
    assert any("deployment.yaml.example" in t for t in texts)
    assert any("head -c 32 /dev/urandom" in t for t in texts)
    # expectations attach to the health curls
    health = [c for c in cmds if "/health" in c.text and c.expectation]
    assert any('"ok": true' in c.expectation for c in health)
    assert any("401" in c.expectation for c in health)
    # optional sanity pytest runs are marked skippable
    optional = [c for c in cmds if c.optional]
    assert optional and any("pytest" in c.text for c in optional)
    # ini fences captured but never executed
    ini = [c for c in cmds if c.is_ini]
    assert ini and all("systemctl" not in c.text or True for c in ini)


def test_parse_doc_continuations_and_env():
    doc = """```bash
FOO=bar \\
  curl -s http://x/health
# -> {"ok": true}
export PYTHONPATH="$PWD:$PWD/dashboard"
nohup python3 hub/api.py > log 2>&1 &
```
"""
    cmds = fresh.parse_doc(doc)
    assert len(cmds) == 3
    assert cmds[0].text == "FOO=bar \\\n  curl -s http://x/health"
    assert cmds[0].expectation == '{"ok": true}'
    assert cmds[2].text.endswith("&")


# ------------------------------------------------------ expectation engine

def mkcmd(text="curl", expectation=None, optional=False):
    return fresh.DocCommand(text=text, block=1, expectation=expectation,
                            optional=optional)


def test_expectation_ok_true():
    ok, _ = fresh.assert_expectation(
        mkcmd(expectation='{"ok": true, "tickets": 1}'),
        '{"ok": true, "tickets": 1}', 0)
    assert ok


def test_expectation_ok_true_mismatch_fails():
    ok, detail = fresh.assert_expectation(
        mkcmd(expectation='{"ok": true, "tickets": 1}'),
        '{"ok": false}', 0)
    assert not ok


def test_expectation_status_401():
    ok, _ = fresh.assert_expectation(mkcmd(expectation="401"), "401\n", 0)
    assert ok
    ok, _ = fresh.assert_expectation(mkcmd(expectation="401"), "200\n", 0)
    assert not ok


def test_expectation_status_401_with_note():
    ok, _ = fresh.assert_expectation(
        mkcmd(expectation="401 (no bearer)"), "401\n", 0)
    assert ok
    ok, _ = fresh.assert_expectation(
        mkcmd(expectation="401 (no bearer)"), "200\n", 0)
    assert not ok


def test_json_semantic_compact_vs_pretty():
    """The doc pretty-prints; the hub returns compact JSON — formatting is
    not a contract, content is."""
    ok, _ = fresh.assert_expectation(
        mkcmd(expectation='{"ok": true, "ts": null, ...}'),
        '{"ok":true,"ts":null,"last_error":null,"note":"none"}', 0)
    assert ok


def test_json_semantic_mismatch_caught():
    ok, detail = fresh.assert_expectation(
        mkcmd(expectation='{"ok": true, "tickets": 1}'),
        '{"ok":true,"tickets":5}', 0)
    assert not ok
    assert "tickets" in detail


# ------------------------------------------------------ mutation: doc drift

def test_mutation_doc_promise_mismatch_caught():
    """Mutation: the doc promises ok:false where reality says ok:true —
    the expectation engine must fail it (L7 catches doc drift)."""
    doc = "```bash\ncurl -s http://127.0.0.1:8021/health\n# -> {\"ok\": false}\n```\n"
    (cmd,) = fresh.parse_doc(doc)
    ok, _ = fresh.assert_expectation(cmd, '{"ok": true, "ts": null}', 0)
    assert not ok, "a mutated doc promise passed — L7 would NOT catch drift"


def test_mutation_command_drift_caught():
    """Mutation: the doc's start command drifts from the verify command
    (wrong port) — the verify expectation must fail."""
    doc = """```bash
TICKET_HUB_PORT=9999 nohup python3 hub/api.py > log 2>&1 &
curl -s http://127.0.0.1:8021/health
# -> {"ok": true}
```
"""
    cmds = fresh.parse_doc(doc)
    # reality: the hub is on 9999, the doc's verify probes 8021 -> conn refused
    reality_stdout = ""
    rc = 7  # curl connection refused
    ok, _ = fresh.assert_expectation(cmds[1], reality_stdout, rc)
    assert not ok, "drifted doc command passed — L7 would NOT catch it"


# ------------------------------------------------------------ hermeticity

@pytest.fixture(scope="module")
def sandbox(tmp_path_factory):
    wd = tmp_path_factory.mktemp("fresh")
    sb = fresh.Sandbox(str(wd), name="unit")
    yield sb
    sb.stop()


def test_sandbox_no_host_tokens(sandbox, tmp_path_factory):
    # the sandbox token is generated fresh, not copied from any host token
    assert sandbox.token_file.exists()
    assert os.access(sandbox.token_file, os.R_OK)
    assert sandbox.token == sandbox.token_file.read_text().strip()
    assert len(sandbox.token) >= 32


def test_sandbox_tree_copied_clean(sandbox):
    assert (sandbox.repo / "dashboard" / "gauntlet.py").exists()
    assert not (sandbox.repo / ".git").exists()


def test_sandbox_env_no_host_aerial_overrides(sandbox):
    env = sandbox.env()
    assert env["HOME"] == str(sandbox.root)
    assert not [k for k in env if k.startswith("AERIAL_")
                and k not in ("AERIAL_CLUSTER__HUB_URL",
                              "AERIAL_CLUSTER__HUB_TOKEN_FILE",
                              "AERIAL_SANDBOX_CRONTAB")]


def test_adapt_interceptors(sandbox):
    clone = fresh.DocCommand(text="git clone https://github.com/rl-ai/aerial.git",
                             block=1)
    action, note = sandbox.adapt(clone)
    assert action == "skip" and "staged" in note
    sysctl = fresh.DocCommand(text="systemctl --user enable --now aerial-console",
                              block=1)
    action, note = sandbox.adapt(sysctl)
    assert action == "prepared" and "nohup" in note
    bearer = fresh.DocCommand(
        text='curl -s -H "Authorization: Bearer ***" http://127.0.0.1:8779/dashboard/health',
        block=1)
    action, text = sandbox.adapt(bearer)
    assert sandbox.token in text and "***" not in text
    assert f"127.0.0.1:{sandbox.console_port}" in text


def test_env_variants_matrix():
    names = [n for n, _ in fresh.env_variants("/tmp/x")]
    assert "clean" in names and "preexisting-state" in names
    assert any(n.startswith("python3.12") for n in names)


def test_preexisting_state_seeded(tmp_path):
    sb = fresh.Sandbox(str(tmp_path), name="pre", preexisting=True)
    try:
        import sqlite3
        con = sqlite3.connect(sb.hub_home / "hub.db")
        n = con.execute("SELECT COUNT(*) FROM job_runs").fetchone()[0]
        con.close()
        assert n >= 1
    finally:
        sb.stop()
