"""dashboard/ui_walk.py — Layer 4b: real-user UI walk (Playwright).

Drives the console like a real operator against a live stack: multiple
parallel browser sessions (compressed walk — several users at once), each
loading /dashboard, scrolling, hovering the queue, clicking a read-only
view. The layer FAILS on: any 5xx, any 401 mid-walk (bearer broke), any
server stack trace leaked into the DOM, or a session that crashes.

Worker sessions run as subprocesses under the Playwright venv
(~/.venvs/shot) so the gauntlet's own interpreter never needs playwright.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

SHOT_PY = os.path.expanduser("~/.venvs/shot/bin/python")
DEFAULT_WORKERS = 3


def shot_python() -> Optional[str]:
    if os.path.exists(SHOT_PY):
        try:
            subprocess.run([SHOT_PY, "-c", "import playwright"],
                           check=True, capture_output=True, timeout=60)
            return SHOT_PY
        except Exception:
            return None
    return None


def run_session(url: str, token: str, session: int, timeout: float = 180.0
                ) -> Dict[str, Any]:
    py = shot_python()
    if not py:
        return {"session": session, "ok": False,
                "failures": ["playwright venv unavailable (~/.venvs/shot)"]}
    try:
        p = subprocess.run(
            [py, "-m", "dashboard.ui_walk_worker", "--url", url,
             "--token", token, "--session", str(session)],
            cwd=ROOT, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"session": session, "ok": False,
                "failures": [f"session {session} timed out after {timeout}s"]}
    for line in reversed((p.stdout or "").splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                break
    return {"session": session, "ok": False,
            "failures": [f"worker rc={p.returncode}",
                         (p.stderr or "")[-300:]]}


def run_walk(url: str, token: str, workers: int = DEFAULT_WORKERS,
             timeout: float = 180.0) -> Dict[str, Any]:
    """workers parallel sessions; ok only if every session is clean."""
    with ThreadPoolExecutor(max_workers=workers) as ex:
        results = list(ex.map(lambda i: run_session(url, token, i, timeout),
                              range(workers)))
    ok = all(r["ok"] for r in results)
    lines: List[str] = []
    for r in results:
        status = "PASS" if r["ok"] else "FAIL"
        lines.append(f"- session {r['session']}: {status} "
                     f"({len(r.get('responses', {}))} responses)")
        for f in r.get("failures", []):
            lines.append(f"    {f}")
    return {"ok": ok, "lines": lines, "data": {"sessions": results,
                                               "workers": workers}}
