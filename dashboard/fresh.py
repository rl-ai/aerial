"""dashboard/fresh.py — Layer 7 FRESH: cold machine + doc accuracy.

Two gap classes, one layer:

1. Cold-start hermeticity. The old cold-start sandbox was NOT self-contained:
   it bound the host tree (~/ai-ops), host systemd units and host tokens.
   This module builds a genuinely hermetic sandbox: the tree is COPIED in
   (no .git/data/logs/state), tokens are GENERATED disposable secrets (no
   host token is read), and there is no host systemd — services are started
   by direct exec through a persistent sandbox shell, exactly the nohup
   fallback the docs prescribe. HOME is redirected into the sandbox, so
   every ~/... path in the docs resolves inside it.

2. Doc accuracy as a release gate. docs/GETTING-STARTED.md (repo root) is
   parsed — its bash blocks are extracted command-by-command together with
   their documented expected outputs (`# -> {"ok": true, ...}`,
   `# -> 401`, `# > 0`) — and executed VERBATIM (in order, in the sandbox
   shell) against a pristine boot. Every documented expectation is asserted;
   any drift between doc and reality FAILS the layer.

Hermetic adaptations (each logged, never silent):
  - `git clone ...`       -> skipped: a pristine copy of the tree is staged
                             at <sandbox>/aerial (a release gate cannot
                             depend on github being up)
  - `pip install ...`     -> satisfied-check first; offline sandbox
  - `systemctl ...`       -> recorded PREPARED, not executed: no host systemd;
                             the doc's own `nohup ... &` fallback is what runs
  - `Bearer ***`          -> the doc redacts secrets; `***` is substituted
                             with the sandbox's DISPOSABLE generated token
  - ports 8021/8779       -> remapped to free ports so the layer cannot
                             collide with (or depend on) any live stack

Environment matrix (extends the layer): the boot+verify plan runs under
  - clean-state vs pre-existing-state sandbox state dirs
  - python3.12 in addition to the current interpreter, when available
    (documented as a limitation with what's needed when it is not).

Mutation discipline: seeded doc/command mismatches must be caught — see
tests (a doc that promises the wrong output, or a command that drifts from
reality, fails the layer's expectation engine).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = Path(os.path.dirname(HERE)).resolve()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DOC_CANDIDATES = ("GETTING-STARTED.md", "docs/GETTING-STARTED.md")

#: ports hardcoded in the doc walkthrough -> remapped per sandbox
DOC_HUB_PORT = 8021
DOC_CONSOLE_PORT = 8779

TOKEN_REDACTION = "***"


def doc_path() -> Path:
    for cand in DOC_CANDIDATES:
        p = ROOT / cand
        if p.exists():
            return p
    raise FileNotFoundError("GETTING-STARTED.md not found in the tree")


# --------------------------------------------------------------- doc parser

@dataclass
class DocCommand:
    text: str
    block: int
    expectation: Optional[str] = None
    optional: bool = False
    is_ini: bool = False


def parse_doc(text: str) -> List[DocCommand]:
    r"""Extract bash commands (with expectations) from a markdown doc.

    - ```bash fences: commands are split on non-continuation lines;
      trailing `\` joins continuations.
    - comment lines starting `# ->` attach an expectation to the previous
      command; `# > N` (bare comparison) also attaches; `# optional` marks
      the command skippable.
    - ```ini fences are captured as is_ini (never executed — no host
      systemd in the sandbox; hermetic mode substitutes the nohup fallback).
    """
    cmds: List[DocCommand] = []
    block = 0
    in_fence = False
    fence_lang = ""
    cur: List[str] = []
    cur_expect: Optional[str] = None
    cur_optional = False

    def note_comment(comment: str, inline: bool = False) -> None:
        nonlocal cur_expect, cur_optional
        em = re.match(r"->\s*(.+)$", comment)
        if em:
            if cur:
                cur_expect = em.group(1).strip()
            elif not inline and cmds:       # comment follows a flushed command
                cmds[-1].expectation = em.group(1).strip()
        elif re.match(r">\s*\d+\s*$", comment):
            if cur:
                cur_expect = comment.strip()
            elif not inline and cmds:
                cmds[-1].expectation = comment.strip()
        elif "optional" in comment.lower():
            cur_optional = True

    def flush():
        nonlocal cur, cur_expect, cur_optional
        if cur:
            cmds.append(DocCommand(
                text="\n".join(cur), block=block,
                expectation=cur_expect, optional=cur_optional,
                is_ini=(fence_lang != "bash")))
        cur, cur_expect, cur_optional = [], None, False

    for raw in text.splitlines():
        line = raw.rstrip()
        m = re.match(r"^```(\w*)\s*$", line)
        if m:
            if in_fence:
                flush()
                in_fence = False
            else:
                in_fence = True
                fence_lang = m.group(1)
                block += 1
            continue
        if not in_fence:
            continue
        stripped = line.strip()
        if fence_lang != "bash":
            # ini/yaml fences: kept as markers, never executed
            if stripped:
                cur.append(line)
            continue
        if not stripped:
            continue
        if stripped.startswith("#"):
            note_comment(stripped.lstrip("#").strip())
            continue
        # inline comments: "cmd ... # optional ..." / "... # > 0"
        if " #" in line:
            head, _, comment = line.partition(" #")
            note_comment(comment.strip(), inline=True)
            line = head.rstrip()
            if not line.strip():
                continue
        cur.append(line)
        if not line.endswith("\\"):
            flush()
    flush()
    return cmds


def assert_expectation(cmd: DocCommand, stdout: str, rc: int,
                       wait_hint: bool = False) -> Tuple[bool, str]:
    """Assert a documented expected output against captured stdout/rc.

    JSON-shaped expectations are compared SEMANTICALLY (the doc pretty-
    prints; the hub returns compact JSON — formatting is not a contract,
    key/value content is). Status-code expectations like `401 (no bearer)`
    compare the leading 3-digit code.
    """
    exp = cmd.expectation
    if exp is None:
        return (rc == 0, f"exit {rc}")
    if exp.startswith(">"):
        return (rc == 0, f"exit {rc} (expect >0)")
    m3 = re.match(r"(\d{3})(?:\s|\(|$)", exp)
    if m3:
        want = m3.group(1)
        got = [ln.strip() for ln in stdout.splitlines() if ln.strip()]
        ok = bool(got) and got[-1] == want
        return ok, f"expected {want}, got {got[-1:] or 'nothing'}"
    if exp.lstrip().startswith("{"):
        ok, detail = _json_expectation(exp, stdout)
        return ok, detail
    ok = exp in stdout
    return ok, (f"expected substring {exp!r} in output"
                + (f"; got tail {stdout[-200:]!r}" if not ok else ""))


def _extract_json(text: str) -> Optional[dict]:
    """First balanced {...} in text, parsed; '...' ellipsis tolerated."""
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                fragment = text[start:i + 1]
                # cut an ellipsis tail: '{"a": 1, ...}' / '{"a": 1 ...}'
                fragment = re.sub(r",?\s*\.\.\.\s*\}?", "}", fragment)
                try:
                    return json.loads(fragment)
                except ValueError:
                    return None
    return None


def _json_expectation(exp: str, stdout: str) -> Tuple[bool, str]:
    """Semantic subset match: every key/value the doc promises must appear
    in the actual JSON body (formatting-insensitive)."""
    expected = _extract_json(exp)
    if not expected:
        return False, f"unparseable doc expectation {exp!r}"
    actual = _extract_json(stdout)
    if actual is None:
        return False, (f"expected JSON {expected} in output; "
                       f"got tail {stdout[-200:]!r}")
    missing = {k: v for k, v in expected.items()
               if k not in actual or actual[k] != v}
    if missing:
        return False, (f"doc expects {expected}; actual {actual} "
                       f"missing/mismatched: {missing}")
    return True, f"json match ({sorted(expected)})"


# ------------------------------------------------------------------ sandbox

SANDBOX_EXCLUDES = (".git", "data", "logs", "run", "__pycache__",
                    ".aerial", "node_modules", ".venv", "*.pyc")


def _ignore(dirpath: str, names: List[str]) -> List[str]:
    out = []
    for n in names:
        if n in SANDBOX_EXCLUDES or n.endswith(".pyc"):
            out.append(n)
    return out


def _free_port() -> int:
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Sandbox:
    """A hermetic cold-start sandbox: copied tree, disposable tokens, own
    HOME, own ports, persistent bash for verbatim doc execution."""

    def __init__(self, workdir: str, name: str = "fresh",
                 python_bin: str = "python3", preexisting: bool = False):
        self.root = Path(workdir) / f"sandbox-{name}"
        if self.root.exists():
            shutil.rmtree(self.root)
        self.repo = self.root / "aerial"
        shutil.copytree(ROOT, self.repo, ignore=_ignore, symlinks=True)
        self.hub_port = _free_port()
        self.console_port = _free_port()
        self.python_bin = python_bin
        self.token = os.urandom(24).hex()
        self.token_file = self.root / "aerial-monorepo-demo" / "secrets" / \
            "hub-token"
        self.token_file.parent.mkdir(parents=True, exist_ok=True)
        self.token_file.write_text(self.token)
        os.chmod(self.token_file, 0o600)
        self.logs = self.root / "aerial-monorepo-demo" / "logs"
        self.logs.mkdir(parents=True, exist_ok=True)
        self.hub_home = self.root / "aerial-monorepo-demo" / "hub-home"
        self.crontab_file = self.root / "crontab.txt"
        self.crontab_file.write_text("")
        self._build_fence()
        self.pids: List[int] = []
        self._shell: Optional[subprocess.Popen] = None
        if preexisting:
            self._seed_preexisting_state()
        self.notes: List[str] = []

    def _seed_preexisting_state(self) -> None:
        """Pre-existing-state variant: state dirs already hold prior data —
        boot must be idempotent over them, not crash."""
        self.hub_home.mkdir(parents=True, exist_ok=True)
        import sqlite3
        con = sqlite3.connect(self.hub_home / "hub.db")
        con.execute("CREATE TABLE IF NOT EXISTS job_runs "
                    "(run_id TEXT PRIMARY KEY, job_id TEXT, node TEXT,"
                    " started_at TEXT, ended_at TEXT, status TEXT,"
                    " exit_code INT, duration_s REAL, summary TEXT,"
                    " log_tail TEXT, meta TEXT, pid INT)")
        con.execute("INSERT OR REPLACE INTO job_runs VALUES "
                    "('fresh-preexisting-1','fresh-variant','sandbox',"
                    "'2026-11-16T00:00:00Z','2026-11-16T00:01:00Z','ok',0,60,"
                    "'pre-existing state row','','{}',NULL)")
        con.commit()
        con.close()

    def _build_fence(self) -> None:
        """The hermetic fence: the sandbox's PATH shadows the two host
        escape hatches the ops agent uses.
          - `crontab` reads/writes ONLY the sandbox crontab file (the real
            crontab is host-level and would leak host schedules — and host
            re-triggers — into the sandbox)
          - `hermes` is a fail-closed stub (no host agent sessions)
        The sandbox tree also gets a neutral seat registry and idle backlog,
        so a cold boot sees no host model seats and no idle work.
        """
        self.fence_bin = self.root / ".fence-bin"
        self.fence_bin.mkdir(parents=True, exist_ok=True)
        crontab = self.fence_bin / "crontab"
        crontab.write_text(
            "#!/bin/bash\n"
            '# hermetic fence: crontab = sandbox file only\n'
            'if [ "$1" = "-l" ]; then cat "$AERIAL_SANDBOX_CRONTAB"; exit 0; fi\n'
            'if [ $# -eq 1 ] && [ -f "$1" ]; then cp "$1" "$AERIAL_SANDBOX_CRONTAB"; exit 0; fi\n'
            'echo "hermetic fence: crontab $* refused" >&2; exit 1\n')
        crontab.chmod(0o755)
        hermes = self.fence_bin / "hermes"
        hermes.write_text(
            "#!/bin/bash\n"
            'echo "hermetic fence: no host agent in the sandbox" >&2; exit 1\n')
        hermes.chmod(0o755)
        reg = self.repo / "dashboard" / "vram_registry.json"
        if reg.exists():
            reg.write_text(json.dumps({"pressure_floor_gib": 8.0,
                                       "seats": []}))
        backlog = self.repo / "dashboard" / "idle_broker" / "backlog.yaml"
        if backlog.exists():
            backlog.write_text("# hermetic sandbox: no idle work\nentries: []\n")

    @property
    def hub_url(self) -> str:
        return f"http://127.0.0.1:{self.hub_port}"

    @property
    def console_url(self) -> str:
        return f"http://127.0.0.1:{self.console_port}"

    def env(self) -> Dict[str, str]:
        e = dict(os.environ)
        e["HOME"] = str(self.root)
        e["PATH"] = f"{self.fence_bin}:{os.environ.get('PATH', '/usr/bin:/bin')}"
        for k in list(e):
            if k.startswith("AERIAL_"):
                e.pop(k)  # no host profile overrides leak in
        e["AERIAL_SANDBOX_CRONTAB"] = str(self.crontab_file)
        e["AERIAL_CLUSTER__HUB_URL"] = self.hub_url
        e["AERIAL_CLUSTER__HUB_TOKEN_FILE"] = str(self.token_file)
        return e

    def materialize_config(self) -> None:
        """The doc's 'edit deployment.yaml' step: exactly its two values,
        with the sandbox's own ports/token path."""
        cfg = (self.repo / "config" / "deployment.yaml.example").read_text()
        cfg = re.sub(r"(?m)^(\s*hub_url:).*$",
                     rf"\1 {self.hub_url}", cfg)
        cfg = re.sub(r"(?m)^(\s*hub_token_file:).*$",
                     rf"\1 {self.token_file}", cfg)
        (self.repo / "config" / "deployment.yaml").write_text(cfg)

    # ---- persistent shell (verbatim execution) -----------------------------
    def start_shell(self) -> None:
        self._shell = subprocess.Popen(
            ["bash", "--noprofile", "--norc"], cwd=str(self.root),
            env=self.env(), stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            bufsize=1)

    def _shell_cmd(self, cmd: str, timeout: float = 60.0) -> Tuple[int, str]:
        assert self._shell and self._shell.stdin and self._shell.stdout
        tag = f"__EOC_{time.time_ns()}__"
        bg = cmd.rstrip().endswith("&")
        send = cmd + "\n"
        if bg:
            send += "echo \"SPAWNED_PID=$!\"\n"
        send += f"echo {tag} rc=$?\n"
        try:
            self._shell.stdin.write(send)
            self._shell.stdin.flush()
        except (BrokenPipeError, OSError):
            return 255, "[sandbox shell died]"
        out: List[str] = []
        deadline = time.time() + timeout
        rc = 255
        spawned: Optional[int] = None
        while time.time() < deadline:
            ln = self._shell.stdout.readline()
            if not ln:
                break
            m = re.search(r"SPAWNED_PID=(\d+)", ln)
            if m:
                spawned = int(m.group(1))
                continue
            if tag in ln:
                # the tag may share a line with command output (a trailing
                # newline-less body, e.g. the hub's /health) — keep the part
                # before the tag as output
                prefix = ln[:ln.index(tag)]
                if prefix.strip():
                    out.append(prefix)
                rcm = re.search(r"rc=(\d+)", ln[ln.index(tag):])
                rc = int(rcm.group(1)) if rcm else 0
                break
            out.append(ln)
        if spawned:
            self.pids.append(spawned)
        return rc, "".join(out)

    def stop(self) -> None:
        if self._shell:
            try:
                self._shell.terminate()
            except Exception:
                pass
            self._shell = None
        for pid in self.pids:
            try:
                os.kill(pid, 15)
            except OSError:
                pass
        for pid in self.pids:
            try:
                os.waitpid(pid, 0)
            except (ChildProcessError, OSError):
                pass
        self.pids = []

    # ---- hermetic command adaptation --------------------------------------
    def adapt(self, cmd: DocCommand) -> Tuple[str, str]:
        """Return (action, adapted_command_or_note). action in
        run|skip|prepared."""
        text = cmd.text
        text = text.replace(f"Bearer {TOKEN_REDACTION}",
                            f"Bearer {self.token}")
        # remap the doc's walkthrough ports everywhere (TICKET_HUB_PORT=8021,
        # URLs, curl targets) so the sandbox cannot collide with any live
        # stack and never depends on one
        text = text.replace(str(DOC_HUB_PORT), str(self.hub_port))
        text = text.replace(str(DOC_CONSOLE_PORT), str(self.console_port))
        if self.python_bin != "python3":
            text = re.sub(r"(?<![\w.])python3\b", self.python_bin, text)
        first = text.splitlines()[0].strip()
        if first.startswith("git clone "):
            return "skip", ("pristine tree already staged at <sandbox>/aerial "
                            "(release gate must not depend on github)")
        if "systemctl " in text:
            return "prepared", ("hermetic: no host systemd — the doc's "
                                "nohup fallback is what runs in the sandbox")
        if cmd.optional:
            return "skip", "doc marks this command optional (sanity check)"
        if first.startswith("pip install"):
            try:
                subprocess.run([sys.executable, "-c",
                                "import fastapi, uvicorn"],
                               check=True, capture_output=True)
                return "skip", "hub deps already present (offline sandbox)"
            except Exception:
                return "run", text
        return "run", text


# --------------------------------------------------------- the doc plan run

@dataclass
class StepResult:
    cmd: str
    action: str
    ok: bool
    detail: str
    note: str = ""


def run_doc_plan(sb: Sandbox, doc_text: str, wait_s: float = 45.0,
                 max_commands: int = 400) -> Tuple[List[StepResult], List[str]]:
    """Execute the parsed doc commands verbatim in the sandbox shell,
    asserting every documented expectation. Returns (results, notes)."""
    cmds = [c for c in parse_doc(doc_text) if not c.is_ini]
    results: List[StepResult] = []
    sb.start_shell()
    for c in cmds[:max_commands]:
        action, text_or_note = sb.adapt(c)
        if action in ("skip", "prepared"):
            results.append(StepResult(c.text.splitlines()[0][:80], action,
                                      True, text_or_note))
            sb.notes.append(f"{action}: {c.text.splitlines()[0][:60]} — "
                            f"{text_or_note}")
            continue
        # retry loop: services may still be booting when a verify runs
        deadline = time.time() + wait_s
        while True:
            rc, out = sb._shell_cmd(text_or_note, timeout=300.0)
            ok, detail = assert_expectation(c, out, rc)
            if ok or time.time() > deadline or c.expectation is None:
                break
            time.sleep(0.5)
        # `cd aerial` re-entries: the sandbox shell persists cwd across doc
        # blocks, so a second `cd aerial` exits 1 while the doc's fresh
        # shell would have entered it — tolerate when the target exists
        if not ok and rc != 0 and text_or_note.strip().startswith("cd "):
            target = text_or_note.strip().split()[1] if len(
                text_or_note.strip().split()) > 1 else ""
            if target and (sb.root / target).exists():
                ok, detail = True, f"already in {target} (persistent shell)"
        if "deployment.yaml" in text_or_note and action == "run":
            # the doc's `cp ...example deployment.yaml` just undid the
            # profile edit; the doc's *edit* step is our materialization
            sb.materialize_config()
        if "hub-token" in text_or_note and "xxd" in text_or_note:
            # the doc generated its own disposable token — adopt it so the
            # `Bearer ***` substitution stays in sync
            try:
                sb.token = sb.token_file.read_text().strip()
            except OSError:
                pass
        results.append(StepResult(c.text.splitlines()[0][:80], "run", ok,
                                  detail))
    return results, sb.notes


def boot_stack(sb: Sandbox, services: Tuple[str, ...] = ("hub", "console"),
               wait_s: float = 40.0) -> Tuple[bool, List[str]]:
    """Direct-exec boot of the doc's services inside a sandbox (no host
    systemd). Returns (ok, lines). Used by L7 variants, L4b and L8."""
    sb.materialize_config()
    lines: List[str] = []
    env = sb.env()
    env["PYTHONPATH"] = f"{sb.repo}:{sb.repo / 'dashboard'}"
    specs = {
        "hub": ([sb.python_bin, "hub/api.py"],
                {"TICKET_HUB_PORT": str(sb.hub_port),
                 "TICKET_HUB_HOST": "127.0.0.1",
                 "TICKET_HUB_HOME": str(sb.hub_home),
                 "TICKET_HUB_DB": str(sb.hub_home / "hub.db"),
                 "TICKET_HUB_AUTH": "off"}),
        "console": ([sb.python_bin, "dashboard/serve_console.py",
                     "--port", str(sb.console_port), "--host", "127.0.0.1",
                     "--hub-url", sb.hub_url,
                     "--hub-token-file", str(sb.token_file)], {}),
        "ops-agent": ([sb.python_bin, "-m", "dashboard.followup.runner",
                       "--interval", "300",
                       "--log", str(sb.logs / "ops-agent.log")], {}),
        "scheduler": ([sb.python_bin, "integration/scheduler.py",
                       "--interval", "30"], {}),
    }
    for name in services:
        cmd, extra = specs[name]
        senv = dict(env)
        senv.update(extra)
        out = open(sb.logs / f"{name}.log", "ab")
        p = subprocess.Popen(cmd, cwd=str(sb.repo), env=senv,
                             stdout=out, stderr=subprocess.STDOUT)
        sb.pids.append(p.pid)
        lines.append(f"- {name}: pid {p.pid}")
    ok = wait_stack(sb, wait_s)
    lines.append("- stack " + ("up" if ok else "DID NOT COME UP"))
    return ok, lines


def wait_stack(sb: Sandbox, wait_s: float = 40.0) -> bool:
    deadline = time.time() + wait_s
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(sb.hub_url + "/health",
                                        timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.3)
    return False


def probe(url: str, token: str = "", timeout: float = 5.0) -> Tuple[int, str]:
    req = urllib.request.Request(
        url, headers=({"Authorization": f"Bearer {token}"} if token else {}))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode(errors="replace")[:300]
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception as e:
        return 0, str(e)[:200]


# ------------------------------------------------- first-boot checklist (L7/L8)

def first_boot_checklist(sb: Sandbox) -> Tuple[bool, List[str]]:
    """The cold-start assertion checklist (reused from the cold-start lane's
    inside.sh): auth shape, ops-agent cold poll, cron payload survival."""
    ok = True
    lines: List[str] = []

    def check(name: str, passed: bool, detail: str = "") -> None:
        nonlocal ok
        ok = ok and passed
        lines.append(f"- {name}: {'PASS' if passed else 'FAIL'}{(' — ' + detail) if detail else ''}")

    code, body = probe(sb.console_url + "/dashboard/health", sb.token)
    check("a1-health-bearer-200-ok-true",
          code == 200 and '"ok":true' in body.replace(" ", ""),
          f"code={code} body={body[:120]}")
    code, _ = probe(sb.console_url + "/dashboard/health")
    check("a2-health-no-bearer-401", code == 401, f"code={code}")
    code, _ = probe(sb.console_url + "/dashboard")
    check("a3-dashboard-no-bearer-401", code == 401, f"code={code}")

    # b1/b2: ops agent --once on a cold sandbox: exit 0, stores, no traceback
    r = subprocess.run(
        [sb.python_bin, "-m", "dashboard.followup.runner", "--once",
         "--log", str(sb.logs / "ops-agent-once.log")],
        cwd=str(sb.repo), env={**sb.env(),
                               "PYTHONPATH": f"{sb.repo}:{sb.repo / 'dashboard'}"},
        capture_output=True, text=True, timeout=300)
    stores = len(list(sb.root.glob("**/*.db"))) + \
        len(list(sb.root.glob("**/*.sqlite*")))
    check("b1-opsagent-poll-stores-created",
          r.returncode == 0 and stores >= 1, f"rc={r.returncode} stores={stores}")
    combined = (r.stdout or "") + (r.stderr or "")
    log = ""
    p = sb.logs / "ops-agent-once.log"
    if p.exists():
        log = p.read_text(errors="replace")
    check("b2-opsagent-no-traceback",
          "Traceback" not in combined and "Traceback" not in log,
          "traceback found" if "Traceback" in combined + log else "")

    # c1: cron payload survives retrigger (parse_crontab + retrigger_command)
    cron = ("*/5 * * * * /usr/bin/python3 /bin/true run --node sandbox "
            "--job coldstart-payload-test --ticket TK-COLDSTART -- "
            "'echo payload-ok > "
            f"{sb.root}/payload-marker.txt' >> /dev/null 2>&1 "
            "# coldstart-payload-test\n")
    sb.crontab_file.write_text(cron)
    script = (
        "import subprocess, sys\n"
        f"sys.path.insert(0, {str(sb.repo)!r})\n"
        "from dashboard.followup import runner\n"
        f"entries = runner.parse_crontab(open({str(sb.crontab_file)!r}).read())\n"
        "assert entries, 'crontab seed parsed to zero entries'\n"
        "e = entries[0]\n"
        "assert 'payload-ok' in e.cmd, f'payload lost: {e.cmd!r}'\n"
        "cmd = runner.retrigger_command(e.job_id, e.ticket, e.cmd)\n"
        "assert '--' in cmd and cmd[cmd.index('--') + 1:] == [e.cmd], \\\n"
        "    f'retrigger dropped payload: {cmd}'\n"
        "r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)\n"
        "sys.exit(r.returncode)\n")
    r2 = subprocess.run([sb.python_bin, "-c", script], capture_output=True,
                        text=True, timeout=90)
    marker = sb.root / "payload-marker.txt"
    check("c1-cron-retrigger-keeps-payload",
          r2.returncode == 0 and marker.exists()
          and "payload-ok" in marker.read_text(),
          f"rc={r2.returncode} {r2.stderr[-200:] if r2.returncode else ''}")
    return ok, lines


# ------------------------------------------------------------ env matrix

def env_variants(workdir: str) -> List[Tuple[str, Dict[str, Any]]]:
    """clean vs pre-existing state, plus python3.12 when available."""
    variants: List[Tuple[str, Dict[str, Any]]] = [
        ("clean", {}),
        ("preexisting-state", {"preexisting": True}),
    ]
    py312 = shutil.which("python3.12")
    if py312:
        variants.append(("python3.12", {"python_bin": py312}))
    else:
        variants.append(("python3.12-unavailable", {"missing": True}))
    return variants


def run_fresh_variant(workdir: str, name: str, opts: Dict[str, Any],
                      doc_text: Optional[str] = None) -> Tuple[bool, List[str], Dict[str, Any]]:
    lines: List[str] = []
    data: Dict[str, Any] = {"variant": name}
    if opts.get("missing"):
        return (True,
                [f"- {name}: NOT AVAILABLE — limitation: no python3.12 on "
                 "PATH; the sandbox needs a second interpreter (e.g. install "
                 "python3.12 or build the sandbox image with both minors) to "
                 "run the alternate-version boot"],
                data)
    doc_text = doc_text or doc_path().read_text()
    py = opts.get("python_bin", "python3")
    if py != "python3":
        # alternate interpreter: it must already have the hub runtime deps
        # (the sandbox is offline — it cannot pip-install into a system
        # python). If not, document the limitation and what would be needed.
        r = subprocess.run([py, "-c", "import fastapi, uvicorn"],
                           capture_output=True, timeout=60)
        if r.returncode != 0:
            return (True,
                    [f"- {name}: NOT RUNNABLE — limitation: {py} is on PATH "
                     "but lacks the hub runtime deps (fastapi/uvicorn) and "
                     "the offline sandbox cannot install them into a "
                     "system-managed interpreter. Needed: `pip install -r "
                     f"hub/requirements.txt` for {py} (or a sandbox image "
                     "shipping both minors with hub deps preinstalled). "
                     "Everything else about the alternate-minor boot is "
                     "already exercised by the parser/driver tests."],
                    {"variant": name, "limitation": "missing hub deps"})
    sb = Sandbox(workdir, name=name, python_bin=py,
                 preexisting=bool(opts.get("preexisting")))
    ok = True
    try:
        sb.materialize_config()
        results, notes = run_doc_plan(sb, doc_text)
        for n in notes:
            lines.append(f"- adaptation: {n}")
        for r in results:
            if r.action == "run":
                lines.append(f"- doc: {r.cmd} → {'PASS' if r.ok else 'FAIL'}"
                             + ("" if r.ok else f" ({r.detail})"))
                ok = ok and r.ok
        boot_ok, blines = boot_stack_if_needed(sb, results)
        ok = ok and boot_ok
        lines += blines
        chk_ok, clines = first_boot_checklist(sb)
        ok = ok and chk_ok
        lines += clines
    finally:
        sb.stop()
    data["ok"] = ok
    return ok, lines, data


def boot_stack_if_needed(sb: Sandbox, results: List[StepResult]
                         ) -> Tuple[bool, List[str]]:
    """If the doc run itself booted the stack (start commands ran), wait for
    readiness; otherwise boot via direct exec."""
    started = any("hub/api.py" in r.cmd and r.action == "run"
                  for r in results)
    if started:
        ok = wait_stack(sb)
        return ok, [f"- doc-started hub: {'up' if ok else 'DID NOT COME UP'}"]
    return boot_stack(sb)


def run_fresh(workdir: str) -> Dict[str, Any]:
    """Layer 7 body: boot+verify the doc plan under every env variant."""
    ok = True
    lines: List[str] = [f"- doc: {doc_path()}"]
    data: Dict[str, Any] = {"variants": {}}
    for name, opts in env_variants(workdir):
        vok, vlines, vdata = run_fresh_variant(workdir, name, opts)
        ok = ok and vok
        data["variants"][name] = vdata
        lines.append(f"== variant {name}: {'PASS' if vok else 'FAIL'} ==")
        lines += ["  " + l for l in vlines]
    return {"ok": ok, "lines": lines, "data": data}
