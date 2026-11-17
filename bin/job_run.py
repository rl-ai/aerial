#!/usr/bin/env python3
"""job_run.py — run wrapper + cron/Hermes registrar for ticket-hub jobs.

Stdlib only, Python 3.10+. Speaks the hub jobs API:
  POST /jobs/{job_id}/runs      start (or one-shot completed run)
  PATCH /jobs/runs/{run_id}     end a run
  PUT  /jobs/{job_id}           register/update a job
"""
import argparse
import glob
import json
import os
import re
import select
import shlex
import signal
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import hub_auth
import hub_jobs

# job_run WRITES to the hub (PUT/POST/PATCH on /jobs). Default the bearer to
# the ops write token when HUB_TOKEN_FILE is unset; read-only callers that
# set HUB_TOKEN_FILE explicitly are unaffected.
if not os.environ.get("HUB_TOKEN_FILE", "").strip():
    os.environ["HUB_TOKEN_FILE"] = os.path.expanduser(
        "~/.config/aiops/handoff/ops-write.token")

DEFAULT_HUB = "http://127.0.0.1:8020"
SHELL_CHARS = set(" ;|&<>()$*'\"`\\")


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def hub_base(args):
    return getattr(args, "hub", None) or os.environ.get("TICKET_HUB_URL") or DEFAULT_HUB


def node_name(args):
    return getattr(args, "node", None) or os.environ.get("AIOPS_NODE") \
        or socket.gethostname().split(".")[0]


def hub_request(base, method, path, body, timeout=5.0):
    """Return (ok, result_or_reason). Never raises."""
    try:
        headers = {"Content-Type": "application/json"}
        headers.update(hub_auth.bearer_header())
    except hub_auth.HubAuthError as e:  # reporting stays best-effort
        return False, str(e)
    url = base.rstrip("/") + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        return False, "HTTP %s" % e.code
    except Exception as e:  # noqa: BLE001 - reason goes to stderr
        reason = getattr(e, "reason", None) or e
        return False, str(reason)
    if not raw:
        return True, {}
    try:
        return True, json.loads(raw)
    except ValueError:
        return False, "bad JSON from hub"


def jquote(s):
    return urllib.parse.quote(str(s), safe="")


# ---------------------------------------------------------------- cron parsing


def slugify(text, maxlen=40):
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:maxlen].rstrip("-")


def cron_interval_s(fields):
    """Expected seconds between runs from 5 cron fields; None if unclear."""
    if len(fields) != 5:
        return None
    m, h, _dom, _mon, dow = fields

    def step(v):
        mm = re.fullmatch(r"\*/(\d+)", v)
        return int(mm.group(1)) if mm else None

    def fixed(v):
        return bool(re.fullmatch(r"\d+", v))

    if fixed(m) and h == "*" and dow == "*":
        return None
    if step(m) is not None and h == "*" and _dom == "*" and _mon == "*" and dow == "*":
        return step(m) * 60
    if fixed(m) and step(h) is not None and _dom == "*" and _mon == "*" and dow == "*":
        return step(h) * 3600
    if fixed(m) and fixed(h) and _dom == "*" and dow == "*":
        return 86400
    if fixed(m) and fixed(h) and _dom == "*" and re.fullmatch(r"\d", dow):
        return 604800
    return None


JOB_RUN_RE = re.compile(
    r"job_run\.py\s+run\b(?P<pre>[^;|&]*?)\s--job\s+(?P<job>\S+)(?P<rest>.*?)(?:\s--\s+(?P<cmd>.*))?$")
TRAILING_TAG_RE = re.compile(r"^(?P<cmd>.*?)(?:\s+#\s*(?P<tag>[A-Za-z0-9._-]+))?\s*$")

ENV_LINE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def parse_crontab(text, node):
    """Yield job dicts (job_id, name, kind, schedule, interval_s, command) per active line."""
    jobs = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or ENV_LINE_RE.match(line) or line.startswith("@reboot"):
            continue
        if line.startswith("@"):
            fields, command = [line.split()[0]] + [""] * 4, " ".join(line.split()[1:])
            schedule = line.split()[0]
        else:
            parts = line.split(None, 5)
            if len(parts) < 6:
                continue
            fields, command, schedule = parts[:5], parts[5], " ".join(parts[:5])
        m = TRAILING_TAG_RE.match(command)
        command, tag = (m.group("cmd") or "").strip(), m.group("tag")
        jm = JOB_RUN_RE.search(command)
        if jm and jm.group("job"):
            job_id = jm.group("job")
            command = (jm.group("cmd") or command).strip()
        else:
            job_id = "%s:%s" % (node, tag or slugify(command))
        jobs.append({
            "job_id": job_id,
            "name": tag or job_id.split(":", 1)[-1],
            "kind": "cron",
            "schedule": schedule,
            "interval_s": cron_interval_s(fields),
            "command": command,
        })
    return jobs


def read_crontab(path=None):
    if path:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    r = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit("job_run: crontab -l failed (%d)" % r.returncode)
    return r.stdout


def cron_put_body(job, node):
    """PUT body for a parsed crontab job; always carries the node."""
    body = dict(job)
    body["node"] = node
    return body


def cmd_register_cron(args):
    text = read_crontab(args.crontab_file)
    base, node = hub_base(args), node_name(args)
    fail = 0
    for job in parse_crontab(text, node):
        job_id = job.pop("job_id")
        if args.dry_run:
            # never print the command: crontab lines carry paths and env
            print("%-50s schedule=%-15s interval_s=%s"
                  % (job_id, job["schedule"], job["interval_s"]))
            continue
        ok, reason = hub_request(base, "PUT", "/jobs/" + jquote(job_id),
                                 cron_put_body(job, node))
        if not ok:
            print("job_run: hub unavailable (%s)" % reason, file=sys.stderr)
            fail = 1
    return fail


# ------------------------------------------------------------------ run wrapper


class Tail:
    """Keeps the last `limit` chars of streamed output."""

    def __init__(self, limit=4000):
        self.limit, self.text = limit, ""

    def add(self, s):
        self.text = (self.text + s)[-self.limit:]


_child = None
_signalled = [False]


def _forward(sig, _frame):
    _signalled[0] = True
    if _child is not None and _child.poll() is None:
        try:
            _child.send_signal(signal.SIGTERM)
        except OSError:
            pass


def build_argv(cmd):
    """Cron-style single arg with spaces/shell chars -> run via /bin/sh -c."""
    if len(cmd) == 1 and (set(cmd[0]) & SHELL_CHARS):
        return ["/bin/sh", "-c", cmd[0]]
    return cmd


def execute(argv, timeout=None):
    """Run argv, stream output to our stdout, return (rc, tail, timed_out)."""
    global _child
    tail = Tail()
    _child = proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    old = [(s, signal.signal(s, _forward)) for s in (signal.SIGINT, signal.SIGTERM)]
    deadline = time.monotonic() + timeout if timeout else None
    timed_out = False
    try:
        while True:
            rlist, _, _ = select.select([proc.stdout], [], [], 0.1)
            if rlist:
                chunk = proc.stdout.read1(4096) if hasattr(proc.stdout, "read1") \
                    else os.read(proc.stdout.fileno(), 4096)
                if not chunk:
                    break
                sys.stdout.buffer.write(chunk)
                sys.stdout.buffer.flush()
                tail.add(chunk.decode("utf-8", "replace"))
            elif proc.poll() is not None and not rlist:
                rest = proc.stdout.read()
                if rest:
                    sys.stdout.buffer.write(rest)
                    sys.stdout.buffer.flush()
                    tail.add(rest.decode("utf-8", "replace"))
                break
            if deadline and time.monotonic() > deadline and proc.poll() is None:
                proc.kill()
                timed_out = True
                break
        rc = proc.wait()
        # drain whatever the child left on stdout (esp. after a kill) before reporting
        rest = proc.stdout.read()
        if rest:
            sys.stdout.buffer.write(rest)
            sys.stdout.buffer.flush()
            tail.add(rest.decode("utf-8", "replace"))
    finally:
        for s, h in old:
            signal.signal(s, h)
        _child = None
    return rc, tail, timed_out


def last_line(text, limit=500):
    for line in reversed(text.splitlines()):
        if line.strip():
            return line.strip()[:limit]
    return ""


def cmd_run(args):
    argv = build_argv(args.cmd)
    base, node = hub_base(args), node_name(args)
    body = {"node": node, "pid": os.getpid(), "name": args.name or args.job,
            "kind": args.kind, "command": " ".join(shlex.quote(a) for a in argv)}
    for attr, key in (("ticket", "ticket_id"), ("schedule", "schedule")):
        v = getattr(args, attr)
        if v:
            body[key] = v
    if args.interval is not None:
        body["interval_s"] = args.interval
    started_at = now_iso()
    warned = [False]

    def warn(reason):
        if not warned[0]:
            warned[0] = True
            print("job_run: hub unavailable (%s)" % reason, file=sys.stderr)

    run_id = None
    ok, res = hub_request(base, "POST", "/jobs/" + jquote(args.job) + "/runs", body)
    if ok:
        run_id = res.get("run_id")
    else:
        warn(res)

    rc, tail, timed_out = execute(argv, args.timeout)
    ended_at = now_iso()
    if timed_out:
        status, exit_code = "timeout", 124
    elif rc < 0 or _signalled[0]:
        status, exit_code = "failed", 128 - min(rc, 0) if rc < 0 else 1
    elif args.skip_exit is not None and rc == args.skip_exit:
        # the command declined to run (e.g. `flock -n -E 75` found the lock
        # held by a still-running cycle): a skip, not a failure
        status, exit_code = "skipped", rc
    else:
        status, exit_code = ("ok" if rc == 0 else "failed"), rc

    end = {"status": status, "exit_code": exit_code,
           "summary": hub_jobs.summary_line(tail.text),
           "log_tail": tail.text}
    if run_id:
        ok, reason = hub_request(base, "PATCH", "/jobs/runs/" + jquote(run_id), end)
    else:  # start failed: one-shot completed run
        end.update({"node": node, "pid": os.getpid(), "started_at": started_at,
                    "ended_at": ended_at, "name": args.name or args.job, "kind": args.kind})
        for k in ("ticket_id", "schedule", "interval_s", "command"):
            if k in body:
                end[k] = body[k]
        ok, reason = hub_request(base, "POST", "/jobs/" + jquote(args.job) + "/runs", end)
    if not ok:
        warn(reason)
    return 0 if status == "skipped" else exit_code


# ------------------------------------------------------------- hermes registry


EVERY_RE = re.compile(r"every\s+(\d+)\s*(m|mn|min|mins|minute|minutes|h|hr|hour|hours|d|day|days)\b",
                      re.IGNORECASE)
EVERY_MULT = {"m": 60, "h": 3600, "d": 86400}


def hermes_interval_s(text):
    """Parse 'every 15m' / 'every 2h' / cron text into seconds; None if unclear."""
    if not text:
        return None
    m = EVERY_RE.search(str(text))
    if m:
        return int(m.group(1)) * EVERY_MULT[m.group(2).lower()[0]]
    fields = str(text).split()
    if len(fields) == 5:
        return cron_interval_s(fields)
    return None


def hermes_job_files():
    home = os.path.expanduser("~/.hermes")
    paths = [os.path.join(home, "cron", "jobs.json")]
    paths += sorted(glob.glob(os.path.join(home, "profiles", "*", "cron", "jobs.json")))
    return [p for p in paths if os.path.isfile(p)]


def hermes_jobs():
    """Read only name/id, schedule and paused flags — never print file contents."""
    out = []
    for path in hermes_job_files():
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        items = data.get("jobs", data) if isinstance(data, dict) else data
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            name = item.get("name") or item.get("id")
            if not name:
                continue
            sched = item.get("schedule") or item.get("cron") or item.get("interval") or ""
            out.append({"name": str(name), "schedule": str(sched),
                        "enabled": not item.get("paused", False)})
    return out


def cmd_register_hermes(args):
    base, node = hub_base(args), node_name(args)
    fail = 0
    for job in hermes_jobs():
        job_id = "%s:hermes:%s" % (node, job["name"])
        body = {"name": job["name"], "node": node, "kind": "hermes-cron",
                "schedule": job["schedule"], "interval_s": hermes_interval_s(job["schedule"]),
                "enabled": 1 if job["enabled"] else 0}
        if args.dry_run:
            print("%-60s schedule=%-20s interval_s=%-7s enabled=%d"
                  % (job_id, job["schedule"], body["interval_s"], body["enabled"]))
            continue
        ok, reason = hub_request(base, "PUT", "/jobs/" + jquote(job_id), body)
        if not ok:
            print("job_run: hub unavailable (%s)" % reason, file=sys.stderr)
            fail = 1
    return fail


# ------------------------------------------------------------ hermes runs import

EXEC_COLS = {
    "exec_id": ["exec_id", "execution_id", "id"],
    "job": ["job_id", "job_name", "job", "name"],
    "started": ["started_at", "start", "started", "started_time"],
    "ended": ["ended_at", "end", "ended", "finished_at", "completed_at"],
    "status": ["status", "result", "outcome"],
}


def _pick(columns, candidates):
    lowered = {c.lower(): c for c in columns}
    for cand in candidates:
        if cand in lowered:
            return lowered[cand]
    return None


def _map_status(value):
    v = str(value).lower()
    if any(k in v for k in ("ok", "success", "done", "complete", "finish")):
        return "ok"
    if any(k in v for k in ("fail", "error", "timeout", "cancel")):
        return "failed"
    return None


def cmd_import_hermes_runs(args):
    db = os.path.expanduser("~/.hermes/cron/executions.db")
    if not os.path.isfile(db):
        print("job_run: no hermes executions.db found, skipping")
        return 0
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
    except sqlite3.Error as e:
        print("job_run: executions.db unreadable (%s), skipping" % e)
        return 0
    base, node = hub_base(args), node_name(args)
    since = (datetime.now(timezone.utc) - timedelta(hours=args.since_h)
             ).strftime("%Y-%m-%dT%H:%M:%SZ")
    posted, seen = 0, set()
    try:
        for table in tables:
            cols = [r[1] for r in conn.execute('PRAGMA table_info("%s")' % table)]
            picked = {k: _pick(cols, v) for k, v in EXEC_COLS.items()}
            if any(v is None for v in picked.values()):
                continue
            q = 'SELECT "%s","%s","%s","%s","%s" FROM "%s"' % (
                picked["exec_id"], picked["job"], picked["started"],
                picked["ended"], picked["status"], table)
            for exec_id, job, started, ended, status in conn.execute(q):
                if str(exec_id) in seen or str(started or "") < since:
                    continue
                final = _map_status(status)
                if final is None or ended is None:
                    continue
                seen.add(str(exec_id))
                body = {"node": node, "status": final, "started_at": str(started),
                        "ended_at": str(ended),
                        "meta": {"hermes_exec_id": str(exec_id),
                                 "dedupe_key": "hermes:%s" % exec_id}}
                ok, reason = hub_request(base, "POST",
                                         "/jobs/%s/runs" % jquote("%s:hermes:%s" % (node, job)), body)
                if ok:
                    posted += 1
                else:
                    print("job_run: hub unavailable (%s)" % reason, file=sys.stderr)
    finally:
        conn.close()
    print("job_run: imported %d hermes run(s)" % posted)
    return 0


# ------------------------------------------------------------------------ main


def build_parser():
    p = argparse.ArgumentParser(prog="job_run.py", description=__doc__)
    sub = p.add_subparsers(dest="cmd_name", required=True)

    r = sub.add_parser("run", help="wrap a command, report start/end to the hub")
    r.add_argument("--job", required=True)
    r.add_argument("--name")
    r.add_argument("--ticket")
    r.add_argument("--schedule")
    r.add_argument("--interval", type=int)
    r.add_argument("--kind", default="cron")
    r.add_argument("--node")
    r.add_argument("--hub")
    r.add_argument("--timeout", type=float)
    r.add_argument("--skip-exit", type=int, metavar="RC",
                   help="record exit code RC as a skipped run and exit 0")
    r.add_argument("cmd", nargs=argparse.REMAINDER, metavar="-- CMD ARGS...")

    for name, fn in (("register-cron", cmd_register_cron), ("register-hermes", cmd_register_hermes)):
        s = sub.add_parser(name, help="register scheduled jobs with the hub")
        s.add_argument("--node")
        s.add_argument("--hub")
        s.add_argument("--dry-run", action="store_true")
        if name == "register-cron":
            s.add_argument("--crontab-file")
        s.set_defaults(fn=fn)

    i = sub.add_parser("import-hermes-runs", help="post past hermes cron runs (best effort)")
    i.add_argument("--since-h", type=float, default=24.0)
    i.add_argument("--node")
    i.add_argument("--hub")
    i.set_defaults(fn=cmd_import_hermes_runs)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.cmd_name == "run":
        cmd = args.cmd
        if cmd and cmd[0] == "--":
            cmd = cmd[1:]
        if not cmd:
            print("job_run: run needs a command after --", file=sys.stderr)
            return 2
        args.cmd = cmd
        return cmd_run(args)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())




