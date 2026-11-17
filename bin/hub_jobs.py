#!/usr/bin/env python3
"""hub_jobs.py — best-effort job-run reporting for in-process tools.

Stdlib only, Python 3.10+. Reports to the hub Jobs API:
  POST /jobs/{job_id}/runs   start a run (or one-shot completed run)
  PATCH /jobs/runs/{run_id}  end a run

Contract: never raises into the caller, never blocks more than `timeout`
seconds per HTTP call, and prints at most one stderr line per process.
"""
import json
import os
import re
import socket
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone

if __package__ in (None, ""):  # run/imported as a top-level script from bin/
    import pathlib
    import sys as _sys
    _BIN_DIR = str(pathlib.Path(__file__).resolve().parent)
    if _BIN_DIR not in _sys.path:
        _sys.path.insert(0, _BIN_DIR)

import hub_auth  # noqa: E402

DEFAULT_HUB = "http://127.0.0.1:8020"
STATUSES = ("ok", "failed", "timeout", "skipped")

_warned = False
_ALNUM_RE = re.compile(r"[A-Za-z0-9]")


def hub_url():
    return os.environ.get("TICKET_HUB_URL") or DEFAULT_HUB


def node_name():
    return os.environ.get("AIOPS_NODE") or socket.gethostname().split(".")[0]


def enabled():
    return os.environ.get("AIOPS_JOBS_REPORT", "").strip().lower() \
        not in ("0", "false", "no")


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _warn_once(reason):
    global _warned
    if not _warned:
        _warned = True
        sys.stderr.write("hub_jobs: hub unavailable (%s)\n" % reason)


def summary_line(text, limit=500):
    """Last line of text holding >=2 alphanumeric chars (so a bare "ok" counts); '' if none."""
    for line in reversed((text or "").splitlines()):
        s = line.strip()
        if len(_ALNUM_RE.findall(s)) >= 2:
            return s[:limit]
    return ""


def _http(method, url, body, timeout):
    """Default transport: urllib JSON request. Raises on non-2xx/errors."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"}
    headers.update(hub_auth.bearer_header(method))
    req = urllib.request.Request(url, data=data, method=method,
                                 headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        code = resp.getcode()
        raw = resp.read()
    if not 200 <= code < 300:
        raise RuntimeError("HTTP %s" % code)
    if not raw:
        return {}
    return json.loads(raw)


class JobReport:
    """One run of `job_id` at the hub. Use as a context manager or call
    start()/finish() by hand. Every failure is swallowed (one stderr line)."""

    def __init__(self, job_id, *, kind="agent", name=None, ticket_id=None,
                 node=None, meta=None, hub=None, timeout=3.0, http=None):
        self.job_id = job_id
        self.kind = kind
        self.name = name or job_id
        self.ticket_id = ticket_id
        self.node = node or node_name()
        self.meta = meta
        self.hub = (hub or hub_url()).rstrip("/")
        self.timeout = timeout
        self.http = http or _http
        self.run_id = None
        self.started_at = None
        self._finished = False

    # -- internals ---------------------------------------------------------

    def _url(self, sub):
        return self.hub + sub

    def _start_path(self):
        return "/jobs/" + urllib.parse.quote(str(self.job_id), safe="") + "/runs"

    def _call(self, method, sub, body):
        try:
            res = self.http(method, self._url(sub), body, self.timeout)
        except Exception as e:  # noqa: BLE001 - best effort by contract
            _warn_once(getattr(e, "reason", None) or e)
            return None
        return res if isinstance(res, dict) else None

    def _base_body(self):
        body = {"node": self.node, "pid": os.getpid(),
                "kind": self.kind, "name": self.name}
        if self.ticket_id:
            body["ticket_id"] = self.ticket_id
        return body

    # -- public API ----------------------------------------------------------

    def start(self):
        if not enabled():
            return self
        self.started_at = now_iso()
        body = self._base_body()
        if self.meta is not None:
            body["meta"] = self.meta
        res = self._call("POST", self._start_path(), body)
        if res:
            self.run_id = res.get("run_id")
        return self

    def finish(self, status, exit_code=None, summary="", log_tail="", meta=None):
        if self._finished or not enabled():
            return
        self._finished = True
        if status not in STATUSES:
            status = "failed"
        end = {"status": status, "exit_code": exit_code,
               "summary": (summary or "")[:500],
               "log_tail": (log_tail or "")[-4000:]}
        merged = {**(self.meta or {}), **(meta or {})}
        if merged:
            end["meta"] = merged
        if self.run_id:
            self._call("PATCH", "/jobs/runs/" + urllib.parse.quote(str(self.run_id), safe=""), end)
            return
        end.update(self._base_body())
        if self.started_at:
            end["started_at"] = self.started_at
        end["ended_at"] = now_iso()
        self._call("POST", self._start_path(), end)

    def __enter__(self):
        return self.start()

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None:
            self.finish("failed", summary="%s: %s" % (exc_type.__name__, exc))
        else:
            self.finish("ok")
        return False
