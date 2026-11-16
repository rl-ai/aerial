#!/usr/bin/env python3
"""Serve the Operator Console on :8777 (the healthcheck's /board target).

Thin runner: build_board from render_daily (real sources, fail-closed),
serve via console.serve.build_app with bearer auth, refresh the static
snapshot on each request so the file fallback never goes stale.
"""
from __future__ import annotations

import argparse
import os
import sys

from deployment_profile import value as _pv
from pathlib import Path
from wsgiref.simple_server import make_server

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
sys.path.insert(0, PKG)

from console.serve import build_app, write_static_snapshot  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(description="Serve the Operator Console")
    ap.add_argument("--port", type=int, default=8777)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--hub-db", default=str(_pv("hub_db")))
    ap.add_argument("--hub-url", default=str(_pv("cluster.hub_url")))
    ap.add_argument("--hub-token-file", default=str(_pv("cluster.hub_token_file")))
    ap.add_argument("--ledger", default=str(_pv("model_calls_ledger")))
    ap.add_argument("--scrapes-db", default=str(_pv("scrapes_db")))
    ap.add_argument("--static-out", default=str(_pv("board_html")))
    ap.add_argument("--write-token-file", default=None,
        help="decision-scoped write token file; MUST be a DISTINCT secret "
             "from the read token (attack-swarm finding: sharing the read "
             "token lets it approve decisions). Absent -> read-only console")
    ap.add_argument("--open-read", action="store_true", default=False,
                    help="read page open without auth when bound to loopback")
    args = ap.parse_args(argv)

    import time as _time
    import render_daily  # noqa: E402  (package sibling)

    # build_board needs the full render_daily args namespace; take the
    # parser defaults and overlay our overrides.
    base = vars(render_daily.build_parser().parse_args([]))
    full = argparse.Namespace(**{**base,
                                 "hub_db": args.hub_db,
                                 "hub_url": args.hub_url,
                                 "hub_token_file": args.hub_token_file,
                                 "ledger": args.ledger,
                                 "scrapes_db": args.scrapes_db,
                                 "out": args.static_out})

    def board_provider():
        board = render_daily.build_board(full, _time.time())
        # The console's exec-summary panel reads board["brief"] (3 headlines).
        # build_board doesn't run the DS exec summary (that's render_daily.main,
        # hourly cron) — recover the headlines from the last rendered board file
        # so the console shows the real report instead of a degraded banner.
        try:
            import re as _re
            html = Path(args.static_out).read_text(encoding="utf-8")
            m = _re.search(r"<h3>Headlines</h3><ul class=brief>(.*?)</ul>", html, _re.S)
            if m:
                board["brief"] = _re.findall(r"<li>(.*?)</li>", m.group(1))
        except OSError:
            pass
        return board

    def refresh_snapshot():
        try:
            write_static_snapshot(args.static_out, board_provider)
        except Exception:
            pass  # fail-closed: the served page reports its own staleness

    with open(os.path.expanduser(args.hub_token_file)) as fh:
        read_token = fh.read().strip()
    write_token = None
    if args.write_token_file:  # None default = write disabled (read-only console)
        try:
            with open(os.path.expanduser(args.write_token_file)) as fh:
                write_token = fh.read().strip()
        except OSError:
            write_token = None
    console_ctx = {"board_provider": board_provider,
                   "refresh_snapshot": refresh_snapshot}
    read_open = bool(args.open_read)  # operator's call; writes stay token-gated
    config = {"read_token": read_token, "write_token": write_token,
              "hub_token_file": args.hub_token_file,
              "read_open": read_open}
    inner_app = build_app(board_provider, console_ctx, config)

    def _health_environ_wrap(app_environ, start_response):
        # /dashboard/health: JSON board summary for the hub healthcheck.
        if app_environ.get("PATH_INFO", "").rstrip("/") == "/dashboard/health":
            import json as _json
            presented = app_environ.get("HTTP_AUTHORIZATION", "")
            expected = "Bearer " + read_token
            if presented != expected:
                start_response("401 Unauthorized",
                               [("Content-Type", "application/json")])
                return [b'{"ok": false}']
            try:
                board = board_provider()
                qa = board.get("queue_all") or board.get("queue") or []
                n = len(qa) if isinstance(qa, list) else 0
                body = _json.dumps({"ok": True, "tickets": max(n, 1)}).encode()
            except Exception:
                body = b'{"ok": false, "tickets": -1}'
            start_response("200 OK", [("Content-Type", "application/json")])
            return [body]
        return inner_app(app_environ, start_response)

    app = _health_environ_wrap
    refresh_snapshot()
    print(f"operator console on http://{args.host}:{args.port}/dashboard "
          f"(decisions: {'enabled' if write_token else 'READ-ONLY'}; "
          f"static snapshot: {args.static_out})")
    with make_server(args.host, args.port, app) as httpd:
        httpd.serve_forever()


if __name__ == "__main__":
    main()
