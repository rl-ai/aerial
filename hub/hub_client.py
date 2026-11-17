"""hub_client — stdlib-only HTTP client for the ticket-hub API.

Any python3 (no venv, no pip) can import this: urllib, json, os only.
The live contract is api.py; error envelope is
``{"error": {"code": ..., "message": ...}}`` — surfaced as HubError.

Usage:
    from hub_client import HubClient, HubError
    hub = HubClient()                      # $TICKET_HUB_URL or :8777
    t = hub.create_ticket("Fix flaky replay", owner="hermes", tags=["qa"])
    hub.patch(t["id"], status="open")
"""
from __future__ import annotations

import json
import os
from typing import Any
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_BASE_URL = "http://127.0.0.1:8777"
DEFAULT_TIMEOUT = 30.0

#: env naming the file that holds this client's hub bearer token
#: (ACCESS-P1-CONTRACT §4). The file is read AT CALL TIME, never cached.
HUB_TOKEN_FILE_ENV = "HUB_TOKEN_FILE"


class HubError(Exception):
    """Raised on any API error envelope or non-2xx response.

    ``code`` is the API error code (not_found, bad_request, cycle_rejected,
    validation, ...) or a transport pseudo-code (http_error, connection_error,
    bad_response).
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def bearer_header(env=None) -> dict:
    """Return ``{"Authorization": "Bearer <token>"}`` for one call.

    The token is read AT CALL TIME from the file named by env
    ``HUB_TOKEN_FILE`` (whitespace stripped). Variable unset -> {} (no
    header). Unreadable or empty file -> HubError("token_unavailable") whose
    message names HUB_TOKEN_FILE but never contains the file contents or the
    token itself. Importable by any stdlib-only module (outbound, scripts).
    """
    env = os.environ if env is None else env
    path = env.get(HUB_TOKEN_FILE_ENV)
    if not path:
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            token = fh.read().strip()
    except OSError:
        raise HubError(
            "token_unavailable",
            f"cannot read the hub bearer token from the file named by "
            f"{HUB_TOKEN_FILE_ENV} ({path})",
        ) from None
    if not token:
        raise HubError(
            "token_unavailable",
            f"the hub bearer token file named by {HUB_TOKEN_FILE_ENV} "
            f"({path}) is empty",
        )
    return {"Authorization": f"Bearer {token}"}


class HubClient:
    """Thin, synchronous client over the ticket-hub HTTP API."""

    def __init__(self, base_url: str | None = None,
                 timeout: float = DEFAULT_TIMEOUT) -> None:
        self.base_url = (base_url or os.environ.get("TICKET_HUB_URL")
                         or DEFAULT_BASE_URL).rstrip("/")
        self.timeout = float(timeout)

    # -- transport ----------------------------------------------------------

    def _request(self, method: str, path: str,
                 body: dict | None = None,
                 params: dict | None = None) -> Any:
        url = self.base_url + path
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                url += "?" + urllib.parse.urlencode(clean)
        data = None
        headers = {"Accept": "application/json"}
        headers.update(bearer_header())  # read at call time (HUB_TOKEN_FILE)
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers,
                                     method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                status = resp.status
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            status = exc.code
        except urllib.error.URLError as exc:
            raise HubError("connection_error",
                           f"cannot reach hub at {self.base_url}: {exc.reason}") from exc
        parsed = None
        if raw:
            try:
                parsed = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                if 200 <= status < 300:
                    # Non-JSON success (e.g. digest markdown) — return text.
                    return raw.decode("utf-8", errors="replace")
                raise HubError("http_error",
                               f"HTTP {status}: {raw.decode('utf-8', errors='replace')[:500]}")
        if not (200 <= status < 300):
            if isinstance(parsed, dict) and isinstance(parsed.get("error"), dict):
                err = parsed["error"]
                raise HubError(str(err.get("code", "http_error")),
                               str(err.get("message", f"HTTP {status}")))
            raise HubError("http_error", f"HTTP {status} on {method} {path}")
        if parsed is None:
            return None
        return parsed

    # -- tickets ------------------------------------------------------------

    def create_ticket(self, title: str, body: str = "",
                      parent: str | None = None, owner=None,
                      tags: list[str] | None = None,
                      families: list[str] | None = None,
                      priority: str | None = None) -> dict:
        payload: dict = {"title": title, "body": body}
        if parent is not None:
            payload["parent"] = parent
        if owner is not None:
            payload["owner"] = owner
        if tags:
            payload["tags"] = list(tags)
        if families:
            payload["families"] = list(families)
        if priority is not None:
            payload["priority"] = priority
        return self._request("POST", "/tickets", body=payload)

    def get(self, ticket_id: str) -> dict:
        return self._request("GET", f"/tickets/{urllib.parse.quote(ticket_id)}")

    def list(self, status: str | None = None, family: str | None = None,
             tag: str | None = None, q: str | None = None) -> list:
        return self._request("GET", "/tickets",
                             params={"status": status, "family": family,
                                     "tag": tag, "q": q})

    def patch(self, ticket_id: str, status: str | None = None,
              priority: str | None = None, title: str | None = None,
              body: str | None = None, actor=None) -> dict:
        payload: dict = {}
        for key, val in (("status", status), ("priority", priority),
                         ("title", title), ("body", body), ("actor", actor)):
            if val is not None:
                payload[key] = val
        return self._request("PATCH", f"/tickets/{urllib.parse.quote(ticket_id)}",
                             body=payload)

    def event(self, ticket_id: str, kind: str,
              payload: dict | None = None, actor=None) -> dict:
        body: dict = {"kind": kind, "payload": payload or {}}
        if actor is not None:
            body["actor"] = actor
        return self._request("POST",
                             f"/tickets/{urllib.parse.quote(ticket_id)}/events",
                             body=body)

    def comment(self, ticket_id: str, text: str, actor=None) -> dict:
        return self.event(ticket_id, "comment", {"text": text}, actor=actor)

    def edge(self, ticket_id: str, dst: str, edge_type: str,
             meta: dict | None = None) -> dict:
        return self._request("POST",
                             f"/tickets/{urllib.parse.quote(ticket_id)}/edges",
                             body={"type": edge_type, "dst": dst,
                                   "meta": meta or {}})

    # -- projections ----------------------------------------------------------

    def lineage(self, ticket_id: str) -> dict:
        return self._request("GET",
                             f"/tickets/{urllib.parse.quote(ticket_id)}/lineage")

    def digest(self, ticket_id: str, fmt: str = "json",
               full: bool = False):
        return self._request(
            "GET", f"/tickets/{urllib.parse.quote(ticket_id)}/digest",
            params={"format": fmt, "full": "true" if full else None})

    def board(self) -> dict:
        return self._request("GET", "/board")

    def api_get(self, path: str):
        return self._request("GET", path)

    def api_post(self, path: str, body: dict):
        return self._request("POST", path, body=body)

    def seats(self) -> dict:
        return self._request("GET", "/seats")

    def assign(self, ticket_id: str, role: str,
               seat: str | None = None,
               actor: dict | None = None) -> dict:
        body: dict = {"role": role, "actor": actor}
        if seat:
            body["seat"] = seat
        return self._request("POST",
                             f"/tickets/{urllib.parse.quote(ticket_id)}/assign",
                             body=body)

    def release(self, ticket_id: str,
                actor: dict | None = None) -> dict:
        return self._request(
            "POST", f"/tickets/{urllib.parse.quote(ticket_id)}/release",
            body={"actor": actor})

    # -- wiki -----------------------------------------------------------------

    def wiki_ls(self) -> list:
        return self._request("GET", "/wiki")

    def wiki_get(self, slug: str) -> dict:
        return self._request("GET", f"/wiki/{urllib.parse.quote(slug)}")

    def wiki_set(self, slug: str, title: str, body_md: str = "",
                 tags: list[str] | None = None, actor=None) -> dict:
        payload: dict = {"title": title, "body_md": body_md}
        if tags:
            payload["tags"] = list(tags)
        if actor is not None:
            payload["actor"] = actor
        return self._request("POST", f"/wiki/{urllib.parse.quote(slug)}",
                             body=payload)

    def plan(self, ticket_id: str) -> dict:
        return self._request("GET",
                             f"/tickets/{urllib.parse.quote(ticket_id)}/plan")

    # -- threads / chat bridges -----------------------------------------------

    def bind(self, ticket_id: str, platform: str, room_id: str,
             thread_id: str | None = None,
             root_msg_id: str | None = None) -> dict:
        return self._request("POST",
                             f"/tickets/{urllib.parse.quote(ticket_id)}/bind",
                             body={"platform": platform, "room_id": room_id,
                                   "thread_id": thread_id,
                                   "root_msg_id": root_msg_id})

    def bindings(self, ticket_id: str) -> list:
        return self._request("GET",
                             f"/tickets/{urllib.parse.quote(ticket_id)}/bindings")

    def inbound(self, platform: str, room_id: str, text: str,
                thread_id: str | None = None,
                root_msg_id: str | None = None, actor=None) -> dict:
        body: dict = {"room_id": room_id, "text": text}
        if thread_id is not None:
            body["thread_id"] = thread_id
        if root_msg_id is not None:
            body["root_msg_id"] = root_msg_id
        if actor is not None:
            body["actor"] = actor
        return self._request("POST",
                             f"/adapters/{urllib.parse.quote(platform)}/inbound",
                             body=body)

    # -- wiki ------------------------------------------------------------------

    def wiki_search(self, q: str) -> list:
        return self._request("GET", "/wiki-search", params={"q": q}) or []

    # -- packet (ticket as shared packet, v2 pillar 3) ---------------------------

    def packet(self, ticket_id: str) -> dict:
        """The complete context any agent needs for one ticket.

        Composes: ticket, ancestor chain, plan (children + progress %),
        blocked list, last 15 events, bound chat threads, and matching wiki
        pages (search by title). If the wiki layer is absent (older hub or
        wiki lane not merged), wiki pages degrade to [] — the packet still
        builds. If it's not in the packet, it doesn't exist for the lane.
        """
        ticket = self.get(ticket_id)
        try:
            lin = self.lineage(ticket_id) or {}
        except HubError:
            lin = {}
        try:
            plan = self.plan(ticket_id)
        except HubError:
            plan = None
        try:
            threads = self.bindings(ticket_id) or []
        except HubError:
            threads = []
        try:
            wiki_pages = self.wiki_search(ticket.get("title", ""))
        except HubError:
            wiki_pages = []  # wiki absent / search unsupported: graceful
        events = lin.get("events") or []
        return {
            "ticket": ticket,
            "ancestors": lin.get("ancestry") or [],
            "plan": plan,
            "blocked": (plan or {}).get("blocked_by_owner") or [],
            "events": events[-15:],
            "threads": threads,
            "wiki_pages": wiki_pages,
        }
