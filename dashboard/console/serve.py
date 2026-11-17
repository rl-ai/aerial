"""RC1 lane 4 — hub serving + bearer auth for the Operator Console.

Pinned interface (lead card t_d456900d): ``build_app(board_provider, console_ctx,
config) -> WSGI app``. Serves:

- GET  /dashboard                     — bearer-gated board render (hub read token)
- POST /dashboard/queue/<action>      — decision-scoped write token only
- GET  /dashboard/static-snapshot     — interactive=False render (static fallback)
- GET  /dashboard/docs                — operator docs page

Auth scheme reuses the existing token handling (data_layer/hub_client.py,
exception_queue/mandate_flow.py): a single ``Authorization: Bearer <token>``
header, hub read token gates GETs, the decision-scoped write token gates POSTs.
Tokens are compared with ``hmac.compare_digest``.

401 on page load renders a minimal "credentials required" HTML page. NO redirect,
NO login form, ever (spec §3 — the bearer comes from operator config, not the
browser). No route ever issues a 3xx.

One template, two renders: every render goes through
``template.render_console_html(board, interactive)``; the hub render passes
``interactive = (write token configured)``. When the write token is absent the
page renders read-only with the §7 banner text (V12 partial — banner flip logic
itself is lane 5's).

Queue writes import ``actions.handle_queue_action(ctx, action, item_id, payload)``
and return its status dict as JSON — the interface is imported, never
reimplemented. Imports are lazy so this module stays loadable in the integration
lane before/after the sibling lanes' modules land.
"""

from __future__ import annotations

import hmac
import json
import os
from typing import Any, Callable, Dict, Optional, Tuple

__all__ = ["build_app", "write_static_snapshot", "CREDENTIALS_REQUIRED_HTML",
           "READONLY_BANNER_TEXT"]

# §7 banner text (read-only until write token granted). Lane 5 owns the flip;
# serve.py only guarantees this text is present when no write token is
# configured. Single source of truth: template.py (re-exported here).
from .template import READONLY_BANNER_TEXT  # noqa: E402,F401
FALLBACK_NOTICE = "actions disabled — open the hub /dashboard to decide"

CREDENTIALS_REQUIRED_HTML = (
    "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">"
    "<title>Operator Console — credentials required</title>"
    "</head><body><h1>Credentials required</h1>"
    "<p>This page requires a bearer token from your operator config. "
    "There is no sign-in page; configure your hub read token and reload.</p>"
    "</body></html>")

_DOCS_HTML = (
    "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">"
    "<title>Operator Console — docs</title></head><body>"
    "<h1>Operator Console docs</h1>"
    "<p>Hub-served page at <code>/dashboard</code>; bearer-gated. Static "
    "fallback at <code>/dashboard/static-snapshot</code>. Queue actions POST to "
    "<code>/dashboard/queue/&lt;action&gt;</code> with the decision-scoped "
    "write token. Reads never need the write token.</p>"
    "</body></html>")

_MAX_BODY = 1 << 20  # 1 MiB cap on POST bodies


# ---------------------------------------------------------------------------
# config / token helpers
# ---------------------------------------------------------------------------

def _resolve_tokens(config: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    """Read/write tokens from config first, then env. Env fallbacks mirror the
    hub-side convention (HUB_* bearer tokens); config wins so an operator install
    can pin its own credentials."""
    read_token = config.get("read_token") or os.environ.get("HUB_READ_TOKEN")
    write_token = config.get("write_token") or os.environ.get("HUB_WRITE_TOKEN")
    return read_token, write_token


def _bearer_from_headers(headers: Dict[str, str]) -> Optional[str]:
    raw = headers.get("Authorization") or headers.get("authorization")
    if not raw or not raw.startswith("Bearer "):
        return None
    token = raw[len("Bearer "):].strip()
    return token or None


def _token_ok(presented: Optional[str], expected: Optional[str]) -> bool:
    if not expected:
        return False
    if not presented:
        return False
    return hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8"))


# ---------------------------------------------------------------------------
# responses
# ---------------------------------------------------------------------------

def _start(start_response, status: str, body: bytes, content_type: str) -> list:
    # WSGI apps must return an iterable of byte chunks (a bare bytes object
    # iterates as ints and breaks strict servers like wsgiref).
    start_response(status, [
        ("Content-Type", content_type),
        ("Content-Length", str(len(body))),
        ("Cache-Control", "no-store"),
    ])
    return [body]


def _html(start_response, status: str, html: str) -> bytes:
    return _start(start_response, status, html.encode("utf-8"), "text/html; charset=utf-8")


def _json(start_response, status: str, obj: Any) -> bytes:
    return _start(start_response, status, json.dumps(obj).encode("utf-8"),
                  "application/json")


def _401(start_response) -> bytes:
    """Minimal credentials-required page. No redirect, no login form."""
    return _html(start_response, "401 Unauthorized", CREDENTIALS_REQUIRED_HTML)


# ---------------------------------------------------------------------------
# lazy sibling-module imports (template.py / actions.py land from other lanes)
# ---------------------------------------------------------------------------

def _render(board: Dict[str, Any], interactive: bool) -> str:
    from . import template  # pinned: template.render_console_html(board, interactive)
    return template.render_console_html(board, interactive)


def _handle_queue_action(console_ctx: Dict[str, Any], action: str,
                         item_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    from . import actions  # pinned: actions.handle_queue_action(ctx, action, item_id, payload)
    return actions.handle_queue_action(console_ctx, action, item_id, payload)


# ---------------------------------------------------------------------------
# the app
# ---------------------------------------------------------------------------

def build_app(board_provider: Callable[[], Dict[str, Any]],
              console_ctx: Dict[str, Any],
              config: Dict[str, Any]) -> Callable:
    """Build the WSGI app. ``board_provider()`` returns the board dict fed to
    ``template.render_console_html``. ``config`` keys: ``read_token``,
    ``write_token`` (env fallbacks: HUB_READ_TOKEN / HUB_WRITE_TOKEN)."""
    read_token, write_token = _resolve_tokens(config or {})
    interactive = write_token is not None  # V12: absent write token → read-only

    def wsgi(environ, start_response):
        method = environ.get("REQUEST_METHOD", "GET").upper()
        path = environ.get("PATH_INFO", "").rstrip("/") or "/"
        headers = {k[5:].replace("_", "-").lower(): v
                   for k, v in environ.items() if k.startswith("HTTP_")}

        # POST /dashboard/queue/<action> — decision-scoped write token ONLY
        if path.startswith("/dashboard/queue/") and method == "POST":
            action = path[len("/dashboard/queue/"):].strip("/")
            known = set((config or {}).get("queue_actions")
                        or ("approve", "dismiss", "escalate"))
            if not action or "/" in action or action not in known:
                return _json(start_response, "404 Not Found",
                             {"status": "error", "message": "unknown action route"})
            presented = _bearer_from_headers(headers)
            if not _token_ok(presented, write_token):
                return _401(start_response)
            try:
                length = int(environ.get("CONTENT_LENGTH") or 0)
            except ValueError:
                length = 0
            if length > _MAX_BODY:
                return _json(start_response, "413 Payload Too Large",
                             {"status": "error", "message": "body too large"})
            raw = environ["wsgi.input"].read(length) if length else b""
            try:
                body = json.loads(raw.decode("utf-8")) if raw else {}
            except (ValueError, UnicodeDecodeError):
                return _json(start_response, "400 Bad Request",
                             {"status": "error", "message": "invalid JSON body"})
            item_id = body.get("item_id")
            if not item_id:
                return _json(start_response, "400 Bad Request",
                             {"status": "error", "message": "item_id required"})
            payload = body.get("payload") or {}
            try:
                result = _handle_queue_action(console_ctx, action, item_id, payload)
            except Exception as exc:  # never leak a traceback as a partial success
                return _json(start_response, "500 Internal Server Error",
                             {"status": "error", "message": repr(exc)})
            return _json(start_response, "200 OK", result)

        if method != "GET":
            return _start(start_response, "405 Method Not Allowed", b"method not allowed",
                          "text/plain; charset=utf-8")

        if path == "/dashboard":
            # read page: open when read_open is set (operator's call —
            # loopback or LAN). Decisions (POST) always require the
            # decision-scoped write token.
            if not config.get("read_open"):
                presented = _bearer_from_headers(headers)
                if not _token_ok(presented, read_token):
                    from urllib.parse import parse_qs
                    qs = parse_qs((environ.get("QUERY_STRING") or ""))
                    presented = (qs.get("token") or [None])[0]
                if not _token_ok(presented, read_token):
                    return _401(start_response)
            try:
                board = board_provider()
            except Exception as exc:
                return _html(start_response, "503 Service Unavailable",
                             "<!DOCTYPE html><html><body><h1>Board unavailable</h1>"
                             f"<p>{_esc(repr(exc))}</p></body></html>")
            return _html(start_response, "200 OK", _render(board, interactive))

        if path == "/dashboard/static-snapshot":
            # fallback emit: always the interactive=False render, no auth gate
            # change — it still requires the read token (it is the same page).
            presented = _bearer_from_headers(headers)
            if not _token_ok(presented, read_token):
                return _401(start_response)
            try:
                board = board_provider()
            except Exception as exc:
                return _html(start_response, "503 Service Unavailable",
                             "<!DOCTYPE html><html><body><h1>Board unavailable</h1>"
                             f"<p>{_esc(repr(exc))}</p></body></html>")
            html = _render(board, False) + f"\n<!-- {FALLBACK_NOTICE} -->\n"
            return _html(start_response, "200 OK", html)

        if path == "/dashboard/docs":
            presented = _bearer_from_headers(headers)
            if not _token_ok(presented, read_token):
                return _401(start_response)
            return _html(start_response, "200 OK", _DOCS_HTML)

        return _start(start_response, "404 Not Found", b"not found",
                      "text/plain; charset=utf-8")

    return wsgi


def _esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def write_static_snapshot(path: str,
                          board_provider: Callable[[], Dict[str, Any]]) -> str:
    """Emit the interactive=False render to ``path`` (static fallback file)."""
    html = _render(board_provider(), False)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(html)
    return path
