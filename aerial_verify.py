#!/usr/bin/env python3
"""aerial verify — one command that checks the whole first-boot chain.

Run from the repo root:

    python3 aerial_verify.py

Checks, in order (PASS / FAIL / SKIP, each with a one-line fix from the
GETTING-STARTED.md troubleshooting table):

  1. python      Python version >= 3.10
  2. imports     repo root and dashboard/ importable (the PYTHONPATH bug
                 class #1)
  3. hub deps    fastapi / uvicorn importable (spec check only — never
                 imported here)
  4. state dirs  ~/.aerial/secrets, logs, data (or the paths the
                 deployment profile names)
  5. token file  exists, non-empty, not world-readable (chmod 600)
  6. hub health  GET <hub-url>/health answers {"ok": true}
  7. console     GET <console-url>/dashboard/health answers ok with the
                 bearer and is refused (401) without it
  8. ops agent   the ops-agent log exists and has an INFO line

Exit codes: 0 = all checks passed, 1 = at least one check failed,
2 = usage error. ``--json`` prints the same results as a JSON object;
``--skip-network`` runs only the local checks (6 and 7 report SKIP).
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import stat
import sys
import urllib.error
import urllib.request

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

#: Populated lazily so that --help and the import itself never depend on
#: the profile being importable (check 2 reports that failure instead).
PROFILE = None


def _profile():
    global PROFILE
    if PROFILE is None:
        import deployment_profile as PROFILE  # noqa: PLW0603
    return PROFILE


PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
NETWORK_CHECKS = ("hub health", "console auth")


class Check:
    def __init__(self, name: str, status: str, detail: str, fix: str = ""):
        self.name = name
        self.status = status
        self.detail = detail
        self.fix = fix

    def as_dict(self) -> dict:
        return {"name": self.name, "status": self.status,
                "detail": self.detail, "fix": self.fix}


def _http_get(url: str, token: str | None = None, timeout: float = 5.0):
    """GET url. Returns (status, body); status 0 means the request failed."""
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception as e:  # URLError, socket.timeout, ValueError on bad URL
        return 0, f"unreachable: {e}"


def check_python() -> Check:
    if sys.version_info >= (3, 10):
        return Check("python", PASS, f"Python {sys.version_info.major}."
                     f"{sys.version_info.minor}")
    return Check("python", FAIL,
                 f"Python {sys.version_info.major}.{sys.version_info.minor}"
                 " is too old (need >= 3.10)",
                 "install Python 3.10+ and re-run with it (python3 --version)")


def check_imports() -> Check:
    """The #1 first-boot failure mode: PYTHONPATH missing the repo root
    and dashboard/."""
    missing = []
    for mod in ("deployment_profile", "dashboard"):
        try:
            __import__(mod)
        except Exception:
            missing.append(mod)
    if not missing:
        return Check("imports", PASS, "repo root and dashboard/ importable")
    return Check(
        "imports", FAIL,
        "cannot import: " + ", ".join(missing),
        'export PYTHONPATH="$PWD:$PWD/dashboard" from the repo root '
        "(in unit files use the Environment=PYTHONPATH=... line)")


def check_hub_deps() -> Check:
    missing = [m for m in ("fastapi", "uvicorn")
               if importlib.util.find_spec(m) is None]
    if not missing:
        return Check("hub deps", PASS, "fastapi, uvicorn importable")
    return Check("hub deps", FAIL,
                 "not installed: " + ", ".join(missing),
                 "pip install -r hub/requirements.txt")


def _profile_state_dir() -> str:
    try:
        return os.path.expanduser(str(_profile().value("paths.state_dir")
                                      or "~/.aerial"))
    except Exception:
        return os.path.expanduser("~/.aerial")


def check_state_dirs(state_dir: str | None = None) -> Check:
    root = state_dir or _profile_state_dir()
    missing = [sub for sub in ("secrets", "logs", "data")
               if not os.path.isdir(os.path.join(root, sub))]
    if not missing:
        return Check("state dirs", PASS, f"{root}/{{secrets,logs,data}} exist")
    return Check(
        "state dirs", FAIL,
        "missing under " + root + ": " + ", ".join(missing),
        "mkdir -p " + " ".join(os.path.join(root, m) for m in missing))


def check_token_file(token_file: str) -> tuple[Check, str]:
    """Returns (check, token). The token value is never printed."""
    path = os.path.expanduser(token_file)
    if not os.path.isfile(path):
        return (Check("token file", FAIL, f"{path} does not exist",
                      "create it: head -c 32 /dev/urandom | xxd -p -c 64 > "
                      f"{path}"), "")
    try:
        with open(path, encoding="utf-8") as fh:
            token = fh.read().strip()
    except OSError as e:
        return (Check("token file", FAIL, f"{path} unreadable: {e}",
                      f"chmod 600 {path}"), "")
    if not token:
        return (Check("token file", FAIL, f"{path} is empty",
                      "regenerate it: head -c 32 /dev/urandom | xxd -p -c 64 "
                      f"> {path}"), "")
    mode = stat.S_IMODE(os.stat(path).st_mode)
    if mode & 0o077:
        return (Check("token file", FAIL,
                      f"{path} is readable by group/other (mode "
                      f"{oct(mode)})", f"chmod 600 {path}"), "")
    return (Check("token file", PASS, path), token)


def check_hub_health(hub_url: str, timeout: float = 5.0) -> Check:
    status, body = _http_get(f"{hub_url.rstrip('/')}/health",
                             timeout=timeout)
    if status == 200:
        try:
            if bool(json.loads(body).get("ok")):
                return Check("hub health", PASS, f"{hub_url}/health ok")
        except ValueError:
            pass
        return Check("hub health", FAIL, f"{hub_url}/health returned 200 "
                     "but ok is not true",
                     "with auth on, /health reflects the last "
                     "healthcheck.sh run — schedule it, or use "
                     "TICKET_HUB_AUTH=off for a loopback demo")
    if status == 0:
        return Check("hub health", FAIL, f"{hub_url}/health unreachable: "
                     f"{body}",
                     "start the hub: TICKET_HUB_PORT=... python3 hub/api.py "
                     "(see GETTING-STARTED section 4)")
    return Check("hub health", FAIL, f"{hub_url}/health -> HTTP {status}",
                 "confirm the hub_url in the deployment profile (or "
                 "--hub-url) matches where the hub listens")


def check_console(console_url: str, token: str, timeout: float = 5.0) -> Check:
    if not token:
        return Check("console auth", SKIP, "no token available (token file "
                     "check failed)",
                     "fix the token file first, then re-run")
    url = f"{console_url.rstrip('/')}/dashboard/health"
    status, body = _http_get(url, token=token, timeout=timeout)
    no_bearer_status, _ = _http_get(url, timeout=timeout)
    if status != 200:
        detail = f"{url} with bearer -> HTTP {status}"
        if status == 0:
            detail = f"{url} unreachable: {body}"
        return Check("console auth", FAIL, detail,
                     "start the console: export PYTHONPATH=\"$PWD:$PWD/"
                     'dashboard" then python3 dashboard/serve_console.py '
                     "(GETTING-STARTED section 5)")
    try:
        ok = bool(json.loads(body).get("ok"))
    except ValueError:
        ok = False
    if not ok:
        return Check("console auth", FAIL, f"{url} -> 200 but ok is false",
                     "confirm the console can reach the hub at "
                     "cluster.hub_url (check the console log)")
    if no_bearer_status != 401:
        return Check("console auth", FAIL,
                     f"{url} without bearer -> HTTP {no_bearer_status} "
                     "(expected 401)",
                     "the health endpoint must be token-gated — omit "
                     "--open-read and restart the console")
    return Check("console auth", PASS,
                 f"{url} ok with bearer, 401 without it")


def check_ops_log() -> Check:
    try:
        log_path = _profile().pvalue("ops_log")
    except Exception:
        log_path = None
    path = str(log_path) if log_path else os.path.expanduser(
        "~/.aerial/logs/ops-agent.log")
    if not os.path.isfile(path):
        return Check("ops agent", FAIL, f"{path} does not exist",
                     "run a first poll: python3 -m dashboard.followup.runner"
                     f" --once --log {path}")
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            has_info = any(" INFO " in line for line in fh)
    except OSError as e:
        return Check("ops agent", FAIL, f"{path} unreadable: {e}",
                     f"chmod 600 {path} or check its permissions")
    if not has_info:
        return Check("ops agent", FAIL, f"{path} has no INFO line",
                     "run a first poll: python3 -m dashboard.followup.runner"
                     f" --once --log {path}")
    return Check("ops agent", PASS, path)


def run_checks(args: argparse.Namespace) -> list[Check]:
    checks = [check_python(), check_imports(), check_hub_deps()]
    checks.append(check_state_dirs(getattr(args, "state_dir", None)))
    token = ""
    if args.token_file:
        token_check, token = check_token_file(args.token_file)
        checks.append(token_check)
    else:
        checks.append(Check("token file", SKIP, "no token file given "
                            "(--token-file or the profile's "
                            "cluster.hub_token_file)", ""))
    if args.skip_network:
        for name in NETWORK_CHECKS:
            checks.append(Check(name, SKIP, "--skip-network", ""))
    else:
        checks.append(check_hub_health(args.hub_url))
        checks.append(check_console(args.console_url, token))
    checks.append(check_ops_log())
    return checks


def _defaults_from_profile() -> dict:
    """Profile-derived defaults; a broken/unimportable profile falls back
    to the generic ones (check 2 reports the import problem)."""
    try:
        prof = _profile()
        return {
            "hub_url": str(prof.value("cluster.hub_url")
                           or "http://127.0.0.1:8020"),
            "token_file": str(prof.value("cluster.hub_token_file")
                              or "~/.aerial/secrets/hub-token"),
        }
    except Exception:
        return {"hub_url": "http://127.0.0.1:8020",
                "token_file": "~/.aerial/secrets/hub-token"}


def build_parser() -> argparse.ArgumentParser:
    defaults = _defaults_from_profile()
    ap = argparse.ArgumentParser(
        prog="aerial_verify.py",
        description="Verify the Aerial first-boot chain end to end")
    ap.add_argument("--json", action="store_true",
                    help="print results as JSON instead of a table")
    ap.add_argument("--hub-url", default=defaults["hub_url"],
                    help="hub base URL (default: cluster.hub_url)")
    ap.add_argument("--console-url", default="http://127.0.0.1:8777",
                    help="console base URL (default: http://127.0.0.1:8777)")
    ap.add_argument("--token-file", default=defaults["token_file"],
                    help="hub token file (default: cluster.hub_token_file)")
    ap.add_argument("--skip-network", action="store_true",
                    help="run only the local checks (hub/console -> SKIP)")
    ap.add_argument("--state-dir", default=None,
                    help="override paths.state_dir for the state-dir check")
    return ap


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as e:
        # argparse exits 2 on usage errors; keep that contract
        return int(e.code) if isinstance(e.code, int) else 2

    checks = run_checks(args)
    failed = [c for c in checks if c.status == FAIL]

    if args.json:
        print(json.dumps({
            "checks": [c.as_dict() for c in checks],
            "summary": {"passed": sum(1 for c in checks
                                      if c.status == PASS),
                        "failed": len(failed),
                        "skipped": sum(1 for c in checks
                                       if c.status == SKIP),
                        "exit_code": 1 if failed else 0},
        }, indent=2))
    else:
        print("aerial verify\n")
        width = max(len(c.name) for c in checks) + 2
        for c in checks:
            mark = {"PASS": "[PASS]", "FAIL": "[FAIL]",
                    "SKIP": "[SKIP]"}[c.status]
            line = f" {mark} {c.name:<{width}} {c.detail}"
            print(line)
            if c.status == FAIL and c.fix:
                print(f"        fix: {c.fix}")
        print()
        if failed:
            print(f"{len(failed)} check(s) failed — see the fixes above "
                  "and GETTING-STARTED.md troubleshooting.")
        else:
            print("all checks passed — the first-boot chain is live.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
