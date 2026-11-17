"""dashboard/ui_walk_worker.py — one real-user browser session (L4b worker).

Run under the Playwright venv (~/.venvs/shot):
    ~/.venvs/shot/bin/python -m dashboard.ui_walk_worker \
        --url http://127.0.0.1:PORT --token TOKEN --session N

Walks the console like a user: load /dashboard (bearer-authed), wait for
content, scroll, hover the queue, click a read-only view, navigate back.
Asserts: every response < 500 (and none 401 — the bearer must hold), no
stack traces in the DOM. Prints one JSON line: {"session": N, "ok": bool,
"responses": {...}, "failures": [...]}.
"""
from __future__ import annotations

import argparse
import json
import sys
import time

TRACE_MARKERS = ("Traceback (most recent call last)", "SyntaxError:",
                 "NameError:", "AttributeError:")


def dom_clean(html: str) -> bool:
    """No server-side stack traces leaked into the page DOM."""
    return not any(m in html for m in TRACE_MARKERS)


def walk(url: str, token: str, session: int, timeout_s: float = 90.0
         ) -> dict:
    from playwright.sync_api import sync_playwright

    failures: list = []
    statuses: dict = {}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(
            extra_http_headers={"Authorization": f"Bearer {token}"},
            viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        page.on("response", lambda r: statuses.__setitem__(
            r.url.split("?")[0], r.status))
        try:
            resp = page.goto(url + "/dashboard", timeout=30_000)
            if resp is None or resp.status >= 500 or resp.status == 401:
                failures.append(f"/dashboard status {resp and resp.status}")
            page.wait_for_load_state("networkidle", timeout=20_000)
            html = page.content()
            if not dom_clean(html):
                failures.append("stack trace in DOM on /dashboard")
            # scroll like a reader
            page.mouse.wheel(0, 1200)
            page.wait_for_timeout(400)
            page.mouse.wheel(0, -600)
            # hover the queue
            rows = page.locator("table tr, .queue li, .queue tr")
            if rows.count() > 0:
                rows.first.hover(timeout=5_000)
            # click a read-only view (first safe in-page link), then come back
            links = page.locator("a[href]:not([href^='javascript:'])")
            if links.count() > 0:
                href = links.first.get_attribute("href") or ""
                if href.startswith("/") or href.startswith(url):
                    links.first.click(timeout=5_000)
                    page.wait_for_load_state("networkidle", timeout=15_000)
                    html2 = page.content()
                    if not dom_clean(html2):
                        failures.append("stack trace in DOM on a sub-page")
                    page.go_back(timeout=10_000)
        except Exception as exc:
            failures.append(f"walk error: {type(exc).__name__}: {exc}")
        finally:
            try:
                ctx.close()
                browser.close()
            except Exception:
                pass
    bad = {u: s for u, s in statuses.items() if s and (s >= 500 or s == 401)}
    if bad:
        failures.append(f"bad responses: {bad}")
    return {"session": session, "ok": not failures,
            "responses": {k[-120:]: v for k, v in list(statuses.items())[:40]},
            "failures": failures}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--token", required=True)
    ap.add_argument("--session", type=int, default=0)
    args = ap.parse_args()
    out = walk(args.url, args.token, args.session)
    print(json.dumps(out))
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
