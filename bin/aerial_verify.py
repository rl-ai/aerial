#!/usr/bin/env python3
"""aerial verify — one command that checks the whole Aerial chain.

Checks (in order): hub reachable, hub auth token valid, console serving,
ops state writable, ledger appending, model seats answering. Prints a
pass/fail table and exits nonzero if any REQUIRED check fails.

Usage:
    python3 bin/aerial_verify.py                # uses the deployment profile
    python3 bin/aerial_verify.py --hub-url URL --hub-token-file PATH ...
"""
import argparse
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from deployment_profile import value as _pv  # noqa: E402

OK, FAIL, WARN = "✓", "✗", "•"


def _get(url, token=None, timeout=5):
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode(errors="replace")
    except Exception as e:
        return 0, f"unreachable: {e}"


def check_hub(url, token):
    status, body = _get(f"{url}/health", token)
    ok = False
    if status == 200:
        try:
            ok = bool(json.loads(body).get("ok"))
        except ValueError:
            ok = False
    return ok, f"HTTP {status}"


def check_console(url):
    status, body = _get(f"{url}/dashboard/health")
    ok = False
    if status == 200:
        try:
            ok = bool(json.loads(body).get("ok"))
        except ValueError:
            ok = False
    return ok, f"HTTP {status}"


def check_ledger(path):
    if not os.path.exists(path):
        return False, "missing (no calls recorded yet)"
    size = os.path.getsize(path)
    return size > 0, f"{size:,} bytes"


def check_state_dir(state):
    os.makedirs(state, exist_ok=True)
    probe = os.path.join(state, ".verify-probe")
    try:
        open(probe, "w").write("ok")
        os.remove(probe)
        return True, "writable"
    except OSError as e:
        return False, str(e)


def check_seats(registry_path, token):
    import json
    try:
        reg = json.load(open(registry_path))
    except (OSError, ValueError) as e:
        return False, f"registry unreadable: {e}"
    seats = reg.get("seats") or []
    results = []
    for s in seats:
        port = s.get("port")
        try:
            status, _ = _get(f"http://127.0.0.1:{port}/v1/models", token, timeout=3)
            results.append((s.get("name"), status == 200))
        except Exception:
            results.append((s.get("name"), False))
    up = sum(1 for _, ok in results if ok)
    detail = ", ".join(f"{n}:{'up' if ok else 'DOWN'}" for n, ok in results)
    return up == len(results) and bool(results), f"{up}/{len(results)} up — {detail}"


def main(argv=None):
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ap = argparse.ArgumentParser(description="Verify the Aerial chain end to end")
    ap.add_argument("--hub-url", default=str(_pv("cluster.hub_url")))
    ap.add_argument("--hub-token-file", default=str(_pv("cluster.hub_token_file")))
    ap.add_argument("--console-url", default="http://127.0.0.1:8777")
    ap.add_argument("--ledger", default=str(_pv("model_calls_ledger")))
    ap.add_argument("--registry", default=os.path.join(here, "dashboard", "vram_registry.json"))
    ap.add_argument("--state-dir", default=None)
    args = ap.parse_args()
    state = args.state_dir or os.path.expanduser(str(_pv("paths.state_dir") or "~/.aerial"))

    token = None
    try:
        token = open(os.path.expanduser(args.hub_token_file)).read().strip()
    except OSError:
        pass

    print(f"aerial verify — {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
    checks = []
    ok, detail = check_hub(args.hub_url, token)
    checks.append(("hub", f"{args.hub_url}", ok, detail))
    ok, detail = check_console(args.console_url)
    checks.append(("console", args.console_url, ok, detail))
    ok, detail = check_state_dir(state)
    checks.append(("state dir", state, ok, detail))
    ok, detail = check_ledger(os.path.expanduser(args.ledger))
    checks.append(("usage ledger", os.path.expanduser(args.ledger), ok, detail))
    ok, detail = check_seats(args.registry, token)
    checks.append(("model seats", args.registry, ok, detail))

    width = max(len(c[0]) for c in checks) + 2
    n_fail = 0
    for name, target, ok, detail in checks:
        mark = OK if ok else FAIL
        if not ok:
            n_fail += 1
        print(f" {mark} {name:<{width}} {detail}")
    print()
    if n_fail:
        print(f"{n_fail} check(s) failed — see docs/GETTING-STARTED.md troubleshooting.")
        return 1
    print("all checks passed — the chain is live.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
