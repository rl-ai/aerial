"""ticket-hub HTTP API (Lane B).

Serves the DESIGN.md HTTP contract on top of any Store object matching the
ticket_hub.store.py signatures (create_ticket, get, list, patch, append_event,
add_edge, children, ancestry, descendants, related, timeline, bind_thread,
board). Lane A's real store is resolved lazily; tests inject a FakeStore.

Run:
    python api.py            # port from $TICKET_HUB_PORT, default 8777
"""

from __future__ import annotations

import inspect
import json
import os
from typing import Any, Optional

from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.routing import Match

from ticket_hub.model import LABEL_RANK, check_label
from ticket_hub.paths import hub_home, hub_path

APP_DIR = os.path.dirname(os.path.abspath(__file__))
UI_DIR = os.path.join(APP_DIR, "ui")

STATUSES = [s.value for s in __import__("ticket_hub.model", fromlist=["Status"]).Status]


def _canon_status(value: str) -> str:
    """Case-insensitive status normalization to the canonical lowercase value."""
    v = str(value).strip().lower()
    if v not in STATUSES:
        raise ValueError(f"Unknown status: {value}")
    return v
from ticket_hub.model import (
    ACTOR_KINDS,
    EDGE_TYPES,
    gate_markers,  # single source of truth: ticket_hub/model.py
)
# Types that must stay acyclic in the graph. Graph edge types are NOT checked.
CYCLE_EDGE_TYPES: set[str] = {"parent_of", "blocks"}
EVENT_KINDS = {
    "created", "updated", "status_changed", "comment", "checkpoint",
    "decision", "approval_receipt", "artifact", "handoff",
    "thread_message", "link", "label_changed",
}
PLATFORMS = {"matrix", "discord", "slack", "fake"}
KNOWN_ISSUE_MARKERS = ("QUEUED FINDING", "KNOWN-FAILS")

#: ACCESS-P1-CONTRACT §0: only the literal "off" disables auth; anything else
#: (including unset) means auth is required.
AUTH_ENV = "TICKET_HUB_AUTH"
SESSION_COOKIE = "hub_session"
_UNSET = object()  # sentinel: caller passed no explicit body actor


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def normalize_actor(raw: Any) -> dict:
    """Coerce an owner/actor (string, dict, or None) into the Actor shape."""
    if raw is None:
        return {"id": "unknown", "name": "unknown", "family": "unknown", "kind": "agent"}
    if isinstance(raw, str):
        raw = {"name": raw}
    if not isinstance(raw, dict):
        raise ValueError("actor must be a string or object")
    name = raw.get("name") or raw.get("id") or "unknown"
    return {
        "id": str(raw.get("id") or name),
        "name": str(name),
        "family": str(raw.get("family") or "unknown"),
        "kind": str(raw.get("kind") or "agent"),
    }


def _is_known_issue(payload: dict) -> bool:
    for value in (payload or {}).values():
        if isinstance(value, str):
            upper = value.upper()
            if any(marker in upper for marker in KNOWN_ISSUE_MARKERS):
                return True
    return False


def translate_digest(project: dict, events: list[dict]) -> list[dict]:
    """Template pass per DESIGN.md translation rules.

    Maps event kinds to reader-facing sentences; never fabricates — every
    bullet cites the event ids it paraphrases.
    """
    bullets: list[dict] = []
    for ev in events:
        kind = ev.get("kind")
        payload = ev.get("payload") or {}
        text = None
        if _is_known_issue(payload):
            text = "known issue logged"
        elif kind == "status_changed":
            text = f"advanced to {str(payload.get('to', '?')).capitalize()}"
        elif kind == "artifact":
            name = payload.get("name") or payload.get("artifact") or "an artifact"
            text = f"produced {name}"
        elif kind == "decision":
            text = "decision recorded"
        elif kind == "approval_receipt":
            approver = payload.get("approver") or (ev.get("actor") or {}).get("name")
            text = f"approved by {approver}" if approver else "approval recorded"
        if text:
            bullets.append({"text": text, "event_ids": [ev.get("id")]})
    return bullets


def digest_markdown(digest: dict) -> str:
    lines = [f"# Updates — {digest.get('project_title') or digest.get('project')}"]
    for bullet in digest.get("bullets", []):
        cites = ", ".join(bullet.get("event_ids", []))
        lines.append(f"- {bullet['text']} ({cites})")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# Request models
# --------------------------------------------------------------------------

class TicketCreate(BaseModel):
    title: str
    body: str = ""
    parent: Optional[str] = None
    owner: Optional[Any] = None
    tags: list[str] = []
    families: list[str] = []
    priority: str = "medium"
    external_refs: dict[str, Any] = {}
    label: Optional[str] = None


class TicketPatch(BaseModel):
    status: Optional[str] = None
    priority: Optional[str] = None
    title: Optional[str] = None
    body: Optional[str] = None
    actor: Optional[Any] = None


class EventCreate(BaseModel):
    kind: str
    actor: Optional[Any] = None
    payload: dict[str, Any] = {}
    label: Optional[str] = None


class LabelUpdate(BaseModel):
    label: str
    reason: str = ""


class EdgeCreate(BaseModel):
    type: str
    dst: str
    meta: dict[str, Any] = {}


class BindCreate(BaseModel):
    platform: str
    room_id: str
    thread_id: Optional[str] = None
    root_msg_id: Optional[str] = None


class InboundMessage(BaseModel):
    room_id: str
    thread_id: Optional[str] = None
    root_msg_id: Optional[str] = None
    actor: Optional[Any] = None
    text: str


class WikiPageCreate(BaseModel):
    title: str
    body_md: str = ""
    tags: list[str] = []
    actor: Optional[Any] = None
    label: Optional[str] = None


class AssignCreate(BaseModel):
    role: str
    seat: Optional[str] = None
    actor: Optional[Any] = None


class ReleaseCreate(BaseModel):
    actor: Optional[Any] = None


class InstantiateCreate(BaseModel):
    title: str
    parent: Optional[str] = None
    actor: Optional[Any] = None
    assign: bool = True


# --------------------------------------------------------------------------
# App factory
# --------------------------------------------------------------------------

_LOGIN_HTML = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>ticket-hub — sign in</title></head>
<body style="font-family: system-ui, sans-serif; max-width: 26em; margin: 4em auto;">
<h1>ticket-hub</h1>
<p>Paste your hub token (<code>thk_...</code>) to start a browser session.</p>
<form id="f">
<input id="t" type="password" autocomplete="off" style="width: 100%; padding: .5em;"
       placeholder="token">
<p><button type="submit" style="padding: .5em 1em;">Sign in</button>
   <span id="msg"></span></p>
</form>
<script>
document.getElementById('f').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const token = document.getElementById('t').value;
  const r = await fetch('/session', {method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({token})});
  if (r.status === 204) { location.href = '/'; }
  else { document.getElementById('msg').textContent = 'invalid token'; }
});
</script>
</body>
</html>
"""


def _auth_enabled(auth: "bool | None") -> bool:
    """Contract §0: auth=None reads env TICKET_HUB_AUTH; default required.
    Only the literal ``off`` disables."""
    if auth is not None:
        return bool(auth)
    return os.environ.get(AUTH_ENV, "required").strip() != "off"


_WT_FILE_CACHE: "dict[str, object]" = {"path": None, "mtime": None,
                                       "tokens": frozenset()}


def _write_tokens_from_file() -> "frozenset[str]":
    """Tokens from TICKET_HUB_WRITE_TOKENS_FILE (one per line, '#' comments).
    Re-read only when mtime changes; any error yields the empty set — the
    union is additive, so a missing file can never widen access."""
    path = os.environ.get("TICKET_HUB_WRITE_TOKENS_FILE", "").strip()
    if not path:
        return frozenset()
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        return frozenset()
    if (_WT_FILE_CACHE["path"] == path
            and _WT_FILE_CACHE["mtime"] == mtime):
        return _WT_FILE_CACHE["tokens"]  # type: ignore[return-value]
    try:
        with open(path, encoding="utf-8") as fh:
            toks = frozenset(ln.strip() for ln in fh
                             if ln.strip() and not ln.strip().startswith("#"))
    except OSError:
        return frozenset()
    _WT_FILE_CACHE.update({"path": path, "mtime": mtime, "tokens": toks})
    return toks


def create_app(store: Any = None, *, auth: "bool | None" = None) -> FastAPI:
    app = FastAPI(title="ticket-hub", version="0.1.0")
    app.state.store = store
    auth_on = _auth_enabled(auth)
    app.state.auth_on = auth_on

    def get_store():
        if app.state.store is None:
            try:
                import os
                from ticket_hub.store import Store  # Lane A
                from hub_gateway import StoreGateway
            except Exception as exc:  # pragma: no cover - depends on env
                raise HTTPException(
                    status_code=503,
                    detail={"code": "store_unavailable",
                            "message": f"No store configured: {exc}"},
                )
            db_path = os.environ.get("TICKET_HUB_DB", hub_path("hub.db"))
            os.makedirs(os.path.dirname(db_path), exist_ok=True)
            app.state.store = StoreGateway(Store(db_path))
        return app.state.store

    # ---- access helpers (auth pipeline; contract §3) ----------------------

    def get_access():
        """Lazily create AccessStore on the store's sqlite connection.
        Tests may pre-set app.state.access."""
        access = getattr(app.state, "access", None)
        if access is None:
            from ticket_hub.access import AccessStore
            st = get_store()
            conn = getattr(st, "conn", None)
            if conn is None:  # raw Store fallback (no gateway wrapper)
                conn = getattr(st, "_conn", None)
            if conn is None:
                raise HTTPException(
                    status_code=503,
                    detail={"code": "access_unavailable",
                            "message": "Store exposes no sqlite connection "
                                       "for the access layer"},
                )
            access = AccessStore(conn)
            app.state.access = access
        return access

    def get_conn():
        st = get_store()
        conn = getattr(st, "conn", None)
        if conn is None:
            conn = getattr(st, "_conn", None)
        if conn is None and getattr(st, "_store", None) is not None:
            inner = st._store
            conn = getattr(inner, "conn", None) or getattr(inner, "_conn", None)
        if conn is None:
            raise HTTPException(
                status_code=503,
                detail={"code": "access_unavailable",
                        "message": "Store exposes no sqlite connection"},
            )
        return conn

    def get_family_acl():
        """Lazily create the FamilyAcl (families sensitivity table) on the
        store's connection. Tests may pre-set app.state.family_acl."""
        facl = getattr(app.state, "family_acl", None)
        if facl is None:
            from ticket_hub.family_acl import FamilyAcl
            facl = FamilyAcl(get_conn())
            app.state.family_acl = facl
        return facl

    def _fam_scopes(viewer):
        """Parse a Viewer's ``fam:<family>:<action>`` scopes.
        None -> legacy/unscoped token (no family restriction)."""
        from ticket_hub.family_acl import parse_family_scopes
        return parse_family_scopes(getattr(viewer, "scopes", ()) or ())

    def external_reader(request: Request):
        from ticket_hub.access import ExternalReader
        reader = getattr(request.state, "_ext_reader", None)
        if reader is None:
            reader = ExternalReader(get_conn(), request.state.viewer)
            request.state._ext_reader = reader
        return reader

    def _store_supports_labels() -> bool:
        """Fix-round §3: can the current store record labels? The real
        Store/StoreGateway surface set_ticket_label and accept label=;
        FakeStore (the auth-off legacy test double) surfaces neither. Gate on
        that capability instead of hoping a TypeError stays invisible."""
        store = get_store()
        if hasattr(store, "set_ticket_label"):
            return True
        inner = getattr(store, "_store", None)  # gateway wrapping a raw Store
        return inner is not None and hasattr(inner, "set_ticket_label")

    def _supports_kwarg(func: Any, name: str) -> bool:
        """Explicit capability check (fix-round §3): does this callable
        accept keyword *name*? This replaces the old catch-and-retry fallback —
        a TypeError raised INSIDE a store call must propagate, never be
        mistaken for an unsupported-keyword signature. A **kwargs catch-all
        accepts anything."""
        try:
            params = inspect.signature(func).parameters
        except (TypeError, ValueError):  # builtins/C callables: assume support
            return True
        p = params.get(name)
        if p is not None and p.kind in (
                inspect.Parameter.KEYWORD_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD):
            return True
        return any(pp.kind == inspect.Parameter.VAR_KEYWORD
                   for pp in params.values())

    def acting(request: Request, body_actor: Any = _UNSET):
        """Resolve 'who writes this' (contract §3 identity rules).
        Auth ON: returns ``(Actor, via)`` where Actor is ALWAYS the DB-loaded
        actor. Body actor absent or equal to the viewer -> the viewer's actor.
        Otherwise AccessStore.effective() (relay/act_as); PermissionError ->
        403 actor_mismatch. The effective viewer must be realm=internal to
        write, else 403.

        Auth OFF: returns the legacy ``(dict, None)`` free-form actor so
        pre-P1 store call shapes are unchanged (byte-identical behaviour)."""
        viewer = getattr(request.state, "viewer", None)
        if viewer is None:  # auth off: legacy free-form actor
            return (normalize_actor(body_actor if body_actor is not _UNSET
                                    else None), None)
        claimed = None
        header_claim = getattr(request.state, "act_as_claim", None)
        if header_claim is not None:
            # Amendment A1: X-Hub-Act-As names the effective actor (already
            # resolved by the pipeline; the viewer was swapped, so comparing
            # the claim to viewer.actor.id here would be dead code — check
            # the body actor against the EFFECTIVE viewer instead). A body
            # actor that differs from the EFFECTIVE actor -> 403
            # actor_mismatch. Visibility/404 checks run before this, so a
            # mismatch on a hidden object stays a 404.
            if (body_actor is not _UNSET and body_actor is not None
                    and normalize_actor(body_actor)["id"] != viewer.actor.id):
                raise HTTPException(
                    status_code=403,
                    detail={"code": "actor_mismatch",
                            "message": "body actor differs from the "
                                       "X-Hub-Act-As actor"})
        if body_actor is not _UNSET and body_actor is not None:
            norm = normalize_actor(body_actor)
            if norm["id"] != viewer.actor.id:
                claimed = norm["id"]
        try:
            eff = get_access().effective(viewer, claimed)
        except PermissionError as exc:
            raise HTTPException(
                status_code=403,
                detail={"code": "actor_mismatch", "message": str(exc)})
        if eff.realm != "internal":
            raise HTTPException(
                status_code=403,
                detail={"code": "external_writer",
                        "message": "effective viewer must be internal to write"},
            )
        actor = None
        get_actor = getattr(get_store(), "get_actor", None) \
            or getattr(getattr(get_store(), "_store", None), "get_actor", None)
        if get_actor is not None:
            actor = get_actor(eff.actor.id)
        if actor is None:
            # CONTRACT-GAP: the gateway surfaces no get_actor; rebuild the
            # Actor from the Viewer instead of inventing a store API.
            from ticket_hub.model import Actor as _Actor
            actor = _Actor(id=eff.actor.id, name=eff.actor.name,
                           family=eff.actor.family, kind=eff.actor.kind)
        return actor, eff.via

    def _actor_as_dict(actor) -> dict:
        """Actor (auth on) or dict (auth off) -> plain dict for helpers that
        only speak dicts (seats/templates)."""
        return actor if isinstance(actor, dict) else actor.to_dict()

    def viewer_is_off(request: Request) -> bool:
        """True when the pipeline placed no viewer (auth off)."""
        return getattr(request.state, "viewer", None) is None

    def require_owner(request: Request):
        viewer = getattr(request.state, "viewer", None)
        if viewer is not None and not viewer.is_owner:
            raise HTTPException(
                status_code=403,
                detail={"code": "publish_forbidden",
                        "message": "only owner actors may publish below "
                                   "internal or lower a label"},
            )

    def check_publish_label(request: Request, label: "str | None"):
        """Create-time label gate (fix-round §2): the label is validated with
        check_label FIRST (unsupported -> ValueError -> 400 label_unsupported
        for every caller, owner or not, auth on or off); an explicit label
        below internal then requires owner (403 publish_forbidden). Returns
        the store-facing label."""
        if label is None:
            return "internal"
        from ticket_hub.model import check_label
        check_label(label)  # ValueError -> 400 bad_request label_unsupported
        viewer = getattr(request.state, "viewer", None)
        if viewer is not None and not viewer.is_owner \
                and LABEL_RANK[label] < LABEL_RANK["internal"]:
            raise HTTPException(
                status_code=403,
                detail={"code": "publish_forbidden",
                        "message": "only owner actors may publish below internal"},
            )
        return label

    def get_ticket_or_404(ticket_id: str) -> dict:
        ticket = get_store().get(ticket_id)
        if ticket is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "not_found", "message": f"Ticket {ticket_id} not found"},
            )
        return ticket

    # ---- JSON error envelope ---------------------------------------------

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request, exc):
        detail = exc.detail if isinstance(exc.detail, dict) else {
            "code": "error", "message": str(exc.detail)}
        return JSONResponse({"error": detail}, status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request, exc):
        return JSONResponse(
            {"error": {"code": "validation",
                       "message": "Request validation failed",
                       "details": exc.errors()}},
            status_code=422,
        )

    @app.exception_handler(KeyError)
    async def key_error_handler(request, exc):
        return JSONResponse(
            {"error": {"code": "not_found", "message": str(exc).strip("'")}},
            status_code=404,
        )

    @app.exception_handler(ValueError)
    async def value_error_handler(request, exc):
        return JSONResponse(
            {"error": {"code": "bad_request", "message": str(exc)}},
            status_code=400,
        )

    @app.exception_handler(PermissionError)
    async def permission_error_handler(request, exc):
        # Contract §3: every PermissionError from AccessStore.effective() is a
        # 403 actor_mismatch on every route (belt and braces alongside the
        # catch inside acting()).
        return JSONResponse(
            {"error": {"code": "actor_mismatch", "message": str(exc)}},
            status_code=403,
        )

    # ---- auth pipeline (contract §3; only when auth is on) -----------------

    if auth_on:
        from ticket_hub.policy import check_policies, policy_for

        def _error(status: int, code: str, message: str) -> JSONResponse:
            return JSONResponse({"error": {"code": code, "message": message}},
                                status_code=status)

        async def _read_json_body(request: Request) -> dict:
            try:
                data = await request.json()
            except Exception:
                raise HTTPException(
                    status_code=400,
                    detail={"code": "bad_request",
                            "message": "body must be JSON"})
            if not isinstance(data, dict):
                raise HTTPException(
                    status_code=400,
                    detail={"code": "bad_request",
                            "message": "body must be a JSON object"})
            return data

        def _not_found_response() -> JSONResponse:
            # identical envelope to a missing object (hidden == missing)
            return JSONResponse(
                {"error": {"code": "not_found", "message": "not found"}},
                status_code=404,
            )

        def _token_from(request: Request) -> "str | None":
            header = request.headers.get("authorization") or ""
            scheme, _, creds = header.partition(" ")
            if scheme.lower() == "bearer" and creds.strip():
                return creds.strip()
            return request.cookies.get(SESSION_COOKIE) or None

        def _origin_ok(request: Request) -> bool:
            """CSRF gate: cookie-authenticated mutations need an Origin (or
            Referer) whose host:port equals the request Host."""
            host = (request.headers.get("host") or "").strip().lower()
            candidate = request.headers.get("origin") \
                or request.headers.get("referer")
            if not candidate or not host:
                return False
            try:
                parsed = urlparse(candidate)
                cand_host = parsed.netloc
                if not cand_host:
                    cand_host = urlparse(f"//{candidate}").netloc
            except ValueError:
                return False
            return cand_host.strip().lower() == host

        def _audit(viewer, method: str, route: str, status: int,
                   decision: str) -> None:
            try:
                get_access().log_access(
                    actor_id=viewer.actor.id if viewer else None,
                    credential_id=viewer.credential_id if viewer else None,
                    method=method, route=route, status=int(status),
                    decision=decision)
            except Exception:  # never let auditing break the response
                pass

        @app.middleware("http")
        async def auth_pipeline(request: Request, call_next):
            scope = request.scope
            match, child_scope = None, {}
            matched_route = None
            for route in request.app.routes:
                m, cs = route.matches(scope)
                if m == Match.FULL:
                    match, matched_route, child_scope = m, route, cs
                    break
                if m == Match.PARTIAL and matched_route is None:
                    match, matched_route, child_scope = m, route, cs
            if matched_route is None:
                return await call_next(request)  # normal 404, unlogged
            path = getattr(matched_route, "path", "")
            # FAIL CLOSED (fix-round §1): HEAD inherits the GET policy; any
            # other (method, path) without a policy is treated like a
            # non-public route — authenticate first, then 404. call_next is
            # never reached for an unlisted pair.
            policy = policy_for(request.method, path)
            unlisted = policy is None
            if unlisted:
                policy = "authenticated"  # gate it; the handler is never run
            if policy == "public":
                return await call_next(request)
            token = _token_from(request)
            viewer = get_access().viewer_for_token(token) if token else None
            if viewer is None:
                _audit(None, request.method, path, 401, "deny")
                return _error(401, "unauthorized",
                              "authentication required")
            # Write-token scoping: mutations need a write-class bearer.
            # Env read per-request so tests can set it dynamically. The
            # literal "*" (test/dev only) lets any valid bearer through;
            # unset or empty fails closed. Optional TICKET_HUB_WRITE_TOKENS_FILE
            # (one token per line, '#' = comment) unions in — re-read on mtime
            # change, so write tokens rotate by editing the file instead of
            # recreating the container (identity-lifecycle requirement).
            if request.method not in ("GET", "HEAD", "OPTIONS"):
                write_tokens = {
                    t.strip() for t in os.environ.get(
                        "TICKET_HUB_WRITE_TOKENS", "").split(",")
                    if t.strip()
                }
                write_tokens |= _write_tokens_from_file()
                if "*" not in write_tokens and token not in write_tokens:
                    _audit(viewer, request.method, path, 403, "deny")
                    return _error(403, "read_only_token",
                                  "write requires a write-class token")
            cookie_auth = (request.cookies.get(SESSION_COOKIE) == token)
            if cookie_auth and request.method in ("POST", "PATCH", "PUT",
                                                  "DELETE"):
                if not _origin_ok(request):
                    _audit(viewer, request.method, path, 403, "deny")
                    return _error(403, "csrf",
                                  "cookie mutation needs a same-origin header")
            # Amendment A1: X-Hub-Act-As is resolved right after the token.
            # The effective Viewer (via = the relay) replaces the viewer for
            # EVERYTHING that follows: external handling, ExternalReader,
            # publication authority and handlers. The audit row records the
            # effective actor and the credential.
            header = request.headers.get("x-hub-act-as")
            if header is not None:
                header = header.strip() or None
            request.state.viewer = viewer
            request.state.act_as_claim = header
            if header:
                try:
                    viewer = get_access().effective(viewer, header)
                except PermissionError as exc:
                    _audit(viewer, request.method, path, 403, "deny")
                    return _error(403, "actor_mismatch", str(exc))
                request.state.viewer = viewer
            if unlisted:  # authenticated but (method, path) has no policy
                _audit(viewer, request.method, path, 404, "deny")
                return _not_found_response()
            if match == Match.PARTIAL:  # method not allowed on the route
                return await call_next(request)
            params = child_scope.get("path_params") or {}
            decision = "allow"
            try:
                # P6 phase 1: family-ACL enforcement. Family-scoped tokens
                # (any ``fam:`` scope) are deny-by-default: GET/HEAD on a
                # ticket outside the scopes is 404 hidden; a mutation needs
                # ``fam:<family>:write`` (403 family_denied). Legacy tokens
                # (no fam: scopes) are unrestricted; external-realm viewers
                # keep their grant-filtered path.
                fam_scopes = _fam_scopes(viewer)
                if (fam_scopes is not None and viewer.realm == "internal"
                        and params.get("ticket_id")):
                    facl = get_family_acl()
                    ticket = get_store().get(params["ticket_id"])
                    if request.method in ("GET", "HEAD", "OPTIONS"):
                        if ticket is None or facl.read_visible(
                                fam_scopes, ticket) is False:
                            _audit(viewer, request.method, path, 404, "hidden")
                            return _not_found_response()
                    else:
                        if ticket is None or not facl.write_allowed(
                                fam_scopes, ticket):
                            decision = "deny"
                            _audit(viewer, request.method, path, 403, decision)
                            return _error(
                                403, "family_denied",
                                "token has no write scope on this ticket's "
                                "family")
                if viewer.realm == "external":
                    if policy == "authenticated":
                        pass  # session/UI plumbing only (e.g. /logout)
                    elif policy == "internal":
                        _audit(viewer, request.method, path, 404, "hidden")
                        return _not_found_response()
                    elif policy == "internal_write_ticket":
                        tid = params.get("ticket_id", "")
                        if external_reader(request).ticket_visible(tid):
                            decision = "deny"
                            _audit(viewer, request.method, path, 403, decision)
                            return _error(403, "forbidden",
                                          "external viewers may not write")
                        _audit(viewer, request.method, path, 404, "hidden")
                        return _not_found_response()
                    elif policy == "internal_write_wiki":
                        if external_reader(request).page_visible(
                                params.get("slug", "")):
                            decision = "deny"
                            _audit(viewer, request.method, path, 403, decision)
                            return _error(403, "forbidden",
                                          "external viewers may not write")
                        _audit(viewer, request.method, path, 404, "hidden")
                        return _not_found_response()
                    elif policy == "internal_write":
                        _audit(viewer, request.method, path, 403, "deny")
                        return _error(403, "forbidden",
                                      "external viewers may not write")
                    # external_read: the handler serves from ExternalReader.
                # Amendment A1: a body actor that differs from the effective
                # actor is a 403 actor_mismatch, enforced in acting() when the
                # handler passes its parsed body actor — the middleware NEVER
                # reads the request body (fix-round §1). Visibility/404 checks
                # run before the handlers, so a mismatch on a hidden object
                # stays a 404.
                response = await call_next(request)
            except HTTPException as exc:
                detail = exc.detail if isinstance(exc.detail, dict) else {
                    "code": "error", "message": str(exc.detail)}
                decision = "hidden" if exc.status_code == 404 \
                    else ("deny" if exc.status_code in (401, 403) else "allow")
                _audit(viewer, request.method, path, exc.status_code, decision)
                return JSONResponse({"error": detail},
                                    status_code=exc.status_code)
            _audit(viewer, request.method, path, response.status_code,
                   decision)
            return response

        # ---- session routes (public; contract §3) --------------------------

        def _secure(request: Request) -> bool:
            if request.url.scheme == "https":
                return True
            fwd = request.headers.get("x-forwarded-proto", "")
            return fwd.split(",")[0].strip().lower() == "https"

        @app.get("/login", include_in_schema=False)
        def login_page():
            return Response(_LOGIN_HTML, media_type="text/html")

        @app.post("/session", status_code=204, include_in_schema=False)
        async def session_start(request: Request):
            body = await _read_json_body(request)
            token = (body or {}).get("token")
            viewer = get_access().viewer_for_token(token) \
                if isinstance(token, str) and token else None
            if viewer is None:
                raise HTTPException(
                    status_code=401,
                    detail={"code": "unauthorized",
                            "message": "invalid token"})
            response = Response(status_code=204)
            response.set_cookie(
                SESSION_COOKIE, token, httponly=True, samesite="strict",
                path="/", secure=_secure(request))
            return response

        @app.post("/logout", status_code=204, include_in_schema=False)
        def session_logout():
            response = Response(status_code=204)
            response.delete_cookie(SESSION_COOKIE, path="/")
            return response

    # ---- Tickets ----------------------------------------------------------

    @app.post("/tickets", status_code=201)
    def create_ticket(req: TicketCreate, request: Request):
        viewer = getattr(request.state, "viewer", None)
        if req.parent is not None and get_store().get(req.parent) is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "not_found",
                        "message": f"Ticket {req.parent} not found"},
            )
        # Fix-round §2: validate before any owner/privilege lookup, auth on or
        # off (unsupported label -> 400 label_unsupported for every caller).
        # Auth off keeps the legacy store shape: no label key when absent.
        if req.label is None and viewer is None:
            label = None
        else:
            label = check_publish_label(request, req.label)
        # I1 fail closed (decision 2026-11-12): an explicit owner OBJECT on
        # ticket creation must carry a valid actor kind — a missing/NULL kind
        # is rejected (missing_actor_kind), never defaulted, and an unknown
        # kind is rejected (unknown_actor_kind). Bare-string owners keep the
        # pre-P1 auth-off legacy free-form name shape (no kind field to
        # validate; the acting viewer path covers auth-on identity).
        if req.owner is not None and isinstance(req.owner, dict):
            owner_kind = req.owner.get("kind")
            if owner_kind is None:
                raise HTTPException(
                    status_code=400,
                    detail={"code": "missing_actor_kind",
                            "message": "owner actor kind is required on "
                                       "ticket creation (expected one of "
                                       f"{sorted(ACTOR_KINDS)})"})
            if owner_kind not in ACTOR_KINDS:
                raise HTTPException(
                    status_code=400,
                    detail={"code": "unknown_actor_kind",
                            "message": f"unknown actor kind: {owner_kind!r} "
                                       f"(expected one of "
                                       f"{sorted(ACTOR_KINDS)})"})
        actor, via = acting(request, _UNSET)
        owner = actor
        if req.owner is not None and viewer is not None:
            claimed = normalize_actor(req.owner)
            store = get_store()
            # CONTRACT-GAP: StoreGateway surfaces no get_actor for the raw
            # Actor; read it through the underlying store instead of adding
            # a gateway method.
            inner = getattr(store, "_store", store)
            stored = inner.get_actor(claimed["id"]) \
                if hasattr(inner, "get_actor") else None
            attrs = get_access().actor_attrs(claimed["id"])
            if attrs is None or attrs.get("realm") != "internal" \
                    or stored is None:
                raise HTTPException(
                    status_code=400,
                    detail={"code": "bad_request",
                            "message": f"owner {claimed['id']!r} is not an "
                                       "existing internal actor"})
            owner = stored
        elif req.owner is not None:
            owner = normalize_actor(req.owner)  # auth off: legacy shape
        families = list(req.families)
        owner_family = getattr(owner, "family", None) or \
            (owner.get("family") if isinstance(owner, dict) else "unknown")
        if not families and owner_family and owner_family != "unknown":
            families = [owner_family]
        # P6 phase 1: a family-scoped writer may only create tickets inside
        # its write families (fail closed when the resolved list is empty).
        if viewer is not None and viewer.realm == "internal":
            fam_scopes = _fam_scopes(viewer)
            if fam_scopes is not None:
                write_fams = {f for f, acts in fam_scopes.items()
                              if "write" in acts}
                if not write_fams & set(families):
                    raise HTTPException(
                        status_code=403,
                        detail={"code": "family_denied",
                                "message": "token has no write scope on the "
                                           "ticket's families"})
        store = get_store()
        kwargs = {}
        # Fix-round §3: explicit capability checks, never a TypeError retry.
        if _supports_kwarg(store.create_ticket, "via"):
            kwargs["via"] = via
        if label is not None and viewer is None:
            # auth off with an explicit label (check_publish_label already ran
            # — a supported label): an explicit label on a store that cannot
            # record it is a 400 bad_request, never TypeError/500 or a silent
            # drop.
            if not _supports_kwarg(store.create_ticket, "label"):
                raise HTTPException(
                    status_code=400,
                    detail={"code": "bad_request",
                            "message": "label not supported by this store"})
        elif (label is not None or viewer is not None) \
                and _supports_kwarg(store.create_ticket, "label"):
            # auth on (label resolved to at least "internal"), on a store
            # that can record it.
            kwargs["label"] = label
        if not kwargs and not hasattr(store, "get_actor"):
            # CONTRACT-GAP: FakeStore (no get_actor) is the auth-off test
            # double; keep its exact legacy create_ticket call shape there.
            return get_store().create_ticket(
                title=req.title,
                body=req.body,
                parent=req.parent,
                owner=owner,
                tags=list(req.tags),
                families=families,
                priority=req.priority,
                external_refs=dict(req.external_refs),
            )
        return get_store().create_ticket(
            title=req.title,
            body=req.body,
            parent=req.parent,
            owner=owner,
            tags=list(req.tags),
            families=families,
            priority=req.priority,
            external_refs=dict(req.external_refs),
            **kwargs,
        )

    @app.get("/tickets")
    def list_tickets(request: Request, status: Optional[str] = None,
                     family: Optional[str] = None,
                     tag: Optional[str] = None, q: Optional[str] = None):
        viewer = getattr(request.state, "viewer", None)
        if viewer is not None and viewer.realm == "external":
            return external_reader(request).list_tickets(
                status=status, tag=tag, q=q)
        if status:
            status = _canon_status(status)
        tickets = get_store().list(status=status, family=family, tag=tag, q=q)
        # P6 phase 1: deny-by-default family scoping on the list — a
        # family-scoped viewer sees only the tickets its scopes admit.
        if viewer is not None and viewer.realm == "internal":
            fam_scopes = _fam_scopes(viewer)
            if fam_scopes is not None:
                facl = get_family_acl()
                tickets = [t for t in tickets
                           if facl.read_visible(fam_scopes, t) is True]
        return list(tickets)

    @app.get("/tickets/{ticket_id}")
    def get_ticket(ticket_id: str, request: Request):
        viewer = getattr(request.state, "viewer", None)
        if viewer is not None and viewer.realm == "external":
            ext = external_reader(request).get_ticket(ticket_id)
            if ext is None:
                raise HTTPException(
                    status_code=404,
                    detail={"code": "not_found",
                            "message": f"Ticket {ticket_id} not found"},
                )
            return ext
        return get_ticket_or_404(ticket_id)

    @app.patch("/tickets/{ticket_id}")
    def patch_ticket(ticket_id: str, req: TicketPatch, request: Request):
        if req.status is not None:
            req.status = _canon_status(req.status)
        fields = {k: v for k, v in
                  (("status", req.status), ("priority", req.priority),
                   ("title", req.title), ("body", req.body))
                  if v is not None}
        if not fields:
            raise HTTPException(status_code=400,
                                detail={"code": "bad_request",
                                        "message": "No fields to update"})
        actor, via = acting(request, req.actor)
        store = get_store()
        if viewer_is_off(request):
            # auth off: exactly the pre-P1 call shape.
            return store.patch(ticket_id, actor=actor, **fields)
        return store.patch(ticket_id, actor=actor, via=via, **fields)

    @app.delete("/tickets/{ticket_id}")
    def delete_ticket(ticket_id: str, request: Request):
        """S4 (buglist 2026-11-14): remove a ticket.

        Auth: WRITE-class bearer, reusing the SEV scoping lane's gate (the
        auth pipeline already demands token ∈ TICKET_HUB_WRITE_TOKENS for
        every non-GET; see api.py "Write-token scoping"). DELETE inherits
        that check by virtue of being a mutation — no separate gate here.
        Tombstone soft-delete in the store (events stay append-only).
        """
        actor, via = acting(request, None)
        store = get_store()
        if viewer_is_off(request):
            # auth off: legacy call shape (no via)
            return store.delete_ticket(ticket_id, actor=actor)
        return store.delete_ticket(ticket_id, actor=actor, via=via)

    @app.get("/tickets/{ticket_id}/events")
    def list_events(ticket_id: str, request: Request):
        viewer = getattr(request.state, "viewer", None)
        if viewer is not None and viewer.realm == "external":
            events = external_reader(request).events(ticket_id)
            if events is None:
                raise HTTPException(
                    status_code=404,
                    detail={"code": "not_found",
                            "message": f"Ticket {ticket_id} not found"},
                )
            return events
        get_ticket_or_404(ticket_id)
        return get_store().timeline(ticket_id)

    @app.post("/tickets/{ticket_id}/events", status_code=201)
    def append_event(ticket_id: str, req: EventCreate, request: Request):
        get_ticket_or_404(ticket_id)
        if req.kind not in EVENT_KINDS:
            raise HTTPException(status_code=400,
                                detail={"code": "bad_request",
                                        "message": f"Unknown event kind: {req.kind}"})
        store = get_store()
        # Fix-round §2/§3: validate the label first (check_label below via
        # the ValueError envelope), and an explicit label on a store that
        # cannot record it is a 400 — never TypeError/500 or a silent drop.
        # Only when no label was given may the legacy call shape be used.
        if req.label is not None:
            check_label(req.label)  # ValueError -> 400 label_unsupported
            if not _store_supports_labels():
                raise HTTPException(
                    status_code=400,
                    detail={"code": "bad_request",
                            "message": "label not supported by this store"})
        actor, via = acting(request, req.actor)
        kwargs = {}
        if _supports_kwarg(store.append_event, "via"):
            kwargs["via"] = via
        if req.label is not None:
            # explicit label only (support proven by the gate above); a
            # label=None kwarg would defeat check_label defaults in the store.
            kwargs["label"] = req.label
        return store.append_event(ticket_id, kind=req.kind, actor=actor,
                                  payload=dict(req.payload), **kwargs)

    @app.post("/tickets/{ticket_id}/label", status_code=200)
    def set_ticket_label_ep(ticket_id: str, req: LabelUpdate,
                            request: Request):
        from ticket_hub.model import check_label
        ticket = get_ticket_or_404(ticket_id)
        check_label(req.label)  # Fix-round §2: ValueError -> 400 label_unsupported
        current = ticket.get("label") or "internal"  # legacy stores: no key
        if LABEL_RANK[req.label] < LABEL_RANK[current]:
            require_owner(request)
        actor, via = acting(request, _UNSET)
        store = get_store()
        if not hasattr(store, "set_ticket_label"):
            # Fix-round §3: a store that cannot record labels -> 400, never
            # TypeError/500. (No silent drop: a label change was requested.)
            raise HTTPException(
                status_code=400,
                detail={"code": "bad_request",
                        "message": "label not supported by this store"})
        return store.set_ticket_label(ticket_id, req.label,
                                      actor=actor, reason=req.reason,
                                      via=via)

    @app.post("/tickets/{ticket_id}/edges", status_code=201)
    def add_edge(ticket_id: str, req: EdgeCreate, request: Request):
        get_ticket_or_404(ticket_id)
        if req.type not in EDGE_TYPES:
            raise HTTPException(status_code=400,
                                detail={"code": "bad_request",
                                        "message": f"Unknown edge type: {req.type}"})
        get_ticket_or_404(req.dst)
        actor, via = acting(request, _UNSET)  # identity/visibility gate (auth on)
        # API-level cycle guard (the store also guards; defense in depth).
        if req.type in CYCLE_EDGE_TYPES:
            store = get_store()
            if req.dst == ticket_id or any(
                d.get("id") == ticket_id for d in store.descendants(req.dst)
            ):
                raise HTTPException(
                    status_code=400,
                    detail={"code": "cycle_rejected",
                            "message": "Edge would create a cycle"},
                )
        store = get_store()
        kwargs = ({"via": via}
                  if _supports_kwarg(store.add_edge, "via") else {})
        return store.add_edge(ticket_id, req.dst, req.type, dict(req.meta),
                              **kwargs)

    # ---- Projections ------------------------------------------------------

    def _build_tree(ticket_id: str, depth: int = 0, seen: Optional[set] = None) -> dict:
        seen = seen if seen is not None else set()
        store = get_store()
        node = {"ticket": get_ticket_or_404(ticket_id), "children": []}
        if ticket_id in seen or depth > 64:
            return node
        seen.add(ticket_id)
        for child in store.children(ticket_id):
            node["children"].append(_build_tree(child["id"], depth + 1, seen))
        return node

    @app.get("/tickets/{ticket_id}/lineage")
    def lineage(ticket_id: str):
        get_ticket_or_404(ticket_id)
        store = get_store()
        descendants = list(store.descendants(ticket_id))
        event_ids = {ticket_id} | {d["id"] for d in descendants}
        events: list[dict] = []
        for tid in event_ids:
            events.extend(store.timeline(tid))
        events.sort(key=lambda e: (e.get("ts") or "", e.get("id") or ""))
        return {
            "ticket": get_ticket_or_404(ticket_id),
            "ancestry": list(store.ancestry(ticket_id)),
            "descendants": descendants,
            "related": list(store.related(ticket_id)),
            "tree": _build_tree(ticket_id),
            "events": events,
        }

    @app.get("/tickets/{ticket_id}/digest")
    def digest(ticket_id: str, format: str = "json", full: bool = False):
        if format not in ("json", "md"):
            raise HTTPException(status_code=400,
                                detail={"code": "bad_request",
                                        "message": "format must be json|md"})
        ticket = get_ticket_or_404(ticket_id)
        store = get_store()
        ancestry = list(store.ancestry(ticket_id))
        project = ancestry[0] if ancestry else ticket
        descendants = list(store.descendants(ticket_id))
        ids = {ticket_id} | {d["id"] for d in descendants}
        events: list[dict] = []
        for tid in ids:
            events.extend(store.timeline(tid))
        events.sort(key=lambda e: (e.get("ts") or "", e.get("id") or ""))
        if full:
            # Canonical lane-C translation layer (all event kinds, no
            # fabrication, cites event ids).
            from ticket_hub import translate as _translate
            canon = _translate.template_pass(events=events, project=project)
            bullets = canon.get("bullets", [])
        else:
            bullets = translate_digest(project, events)
        payload = {
            "project": project["id"],
            "project_title": project.get("title"),
            "ticket": ticket["id"],
            "updated_at": ticket.get("updated_at"),
            "bullets": bullets,
        }
        if format == "md":
            return Response(digest_markdown(payload), media_type="text/markdown")
        return payload

    @app.get("/board")
    def board():
        store = get_store()
        columns = store.board()
        child_counts = {}
        parents = {}
        for status_tickets in columns.values():
            for t in status_tickets:
                kids = list(store.children(t["id"]))
                child_counts[t["id"]] = len(kids)
                for k in kids:
                    parents[k["id"]] = t["id"]
        return {"columns": columns, "child_counts": child_counts,
                "parents": parents}

    @app.get("/health")
    def health(request: Request):
        """Last-known health written by scripts/healthcheck.sh (monitor
        surface per GLM review: a FAIL must be visible between checks).
        With auth on the body is reduced to {"ok": bool} (contract §3)."""
        if auth_on:
            path = os.environ.get("TICKET_HUB_HEALTH_STATUS",
                                  hub_path("health-status.json"))
            try:
                with open(path, encoding="utf-8") as fh:
                    data = json.load(fh)
            except (OSError, ValueError):
                data = {}
            return {"ok": bool(data.get("ok"))}
        path = os.environ.get("TICKET_HUB_HEALTH_STATUS",
                              hub_path("health-status.json"))
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return {"ok": True, "ts": None, "last_error": None,
                    "note": "no healthcheck has run yet"}
        return data

    def _read_json_file(path: str) -> dict | None:
        try:
            with open(os.path.expanduser(path), encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return None

    @app.get("/admin/status")
    def admin_status():
        """Mission Control aggregate. Read-only: local files + launchctl
        list (display-only; actions stay with the owner)."""
        hub_dir = os.environ.get("TICKET_HUB_DIR", hub_home())
        store = get_store()

        counts = {}
        try:
            conn = getattr(store, "_store", store)
            raw = getattr(conn, "_conn", None)
            if raw is not None:
                for table, key in (("tickets", "tickets"), ("edges", "edges"),
                                   ("events", "events"),
                                   ("wiki_pages", "wiki_pages")):
                    try:
                        counts[key] = raw.execute(
                            f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    except Exception:
                        counts[key] = None
        except Exception:
            pass

        services = []
        try:
            import subprocess
            out = subprocess.run(
                ["launchctl", "list"], capture_output=True, text=True,
                timeout=5).stdout
            for line in out.splitlines():
                cols = line.split("\t")
                if len(cols) >= 3 and cols[2].startswith("com.ticket-hub."):
                    pid, _state, label = cols[0], cols[1], cols[2]
                    services.append({
                        "label": label,
                        "loaded": True,
                        "pid": None if pid in ("-", "") else int(pid),
                        "state": ("running" if pid not in ("-", "")
                                  else "loaded (not running)")})
        except Exception:
            pass

        backups = {"newest": None, "age_h": None, "count": 0}
        try:
            bdir = os.path.join(hub_dir, "backups")
            snaps = sorted(
                (f for f in os.listdir(bdir) if f.endswith(".db")),
                key=lambda f: os.path.getmtime(os.path.join(bdir, f)),
                reverse=True)
            if snaps:
                import time
                backups = {"newest": snaps[0],
                           "age_h": round(
                               (time.time() - os.path.getmtime(
                                   os.path.join(bdir, snaps[0]))) / 3600, 1),
                           "count": len(snaps)}
        except OSError:
            pass

        return {
            "health": _read_json_file(
                os.path.join(hub_dir, "health-status.json"))
            or {"ok": None, "ts": None, "tickets": None,
                "last_error": "no healthcheck has run"},
            "services": services,
            "backups": backups,
            "maintenance": _read_json_file(
                os.path.join(hub_dir, "maintenance-status.json"))
            or {"last_run": None, "summary": "not run yet"},
            "hygiene": _read_json_file(
                os.path.join(hub_dir, "hygiene-status.json"))
            or {"result": "unknown", "ts": None},
            "counts": counts,
            "db_path": hub_dir + "/hub.db",
            "seats_note": "seat health: run remote-model-health; "
                          "not polled from the hub by design (loopback scope)",
        }

    @app.get("/tickets/{ticket_id}/plan")
    def plan(ticket_id: str):
        """Introspection view: what this ticket is about, progress, and
        what remains. Progress = descendant work items done vs open;
        remaining = unfinished children + latest 'remaining items' checkpoint
        (checkpoint events may carry payload.remaining: [..]); blocked_by_owner
        = open items flagged blocked/OWNER-GATED (marker event or blocked
        status)."""
        store = get_store()
        ticket = get_ticket_or_404(ticket_id)
        descendants = list(store.descendants(ticket_id))

        def flag_blocked(t) -> bool:
            # Current state only: completed/cancelled items drop off even if
            # they carry historical OWNER-GATED marker events (GLM review).
            if t["status"] in ("done", "cancelled"):
                return False
            if t["status"] == "blocked":
                return True
            return any(
                (e.get("payload") or {}).get("marker") in gate_markers()
                for e in store.timeline(t["id"]))

        done = [t for t in descendants if t["status"] == "done"]
        cancelled = [t for t in descendants if t["status"] == "cancelled"]
        open_items = [t for t in descendants
                      if t["status"] not in ("done", "cancelled")]
        blocked = [t for t in descendants if flag_blocked(t)]
        # latest checkpoint listing remaining items (hand-authored plans)
        remaining_notes: list[dict] = []
        for tid in {ticket_id} | {t["id"] for t in descendants}:
            for ev in store.timeline(tid):
                rem = (ev.get("payload") or {}).get("remaining")
                if isinstance(rem, list) and rem:
                    remaining_notes.append(
                        {"ticket_id": tid, "ts": ev.get("ts", ""),
                         "items": [str(x) for x in rem]})
        remaining_notes.sort(key=lambda n: n["ts"])
        total_work = len(descendants) - len(cancelled)
        pct = int(100 * len(done) / total_work) if total_work else (
            100 if ticket["status"] == "done" else 0)
        brief = {"ticket": ticket,
                 "summary": (ticket.get("body") or "").strip(),
                 "progress": {"done": len(done), "open": len(open_items),
                              "total": total_work, "percent": pct},
                 "remaining_items": [
                     {"id": t["id"], "title": t["title"], "status": t["status"],
                      "owner": (t.get("owner") or {}).get("name")}
                     for t in open_items],
                 "remaining_notes": remaining_notes[-1]["items"] if remaining_notes else [],
                 "blocked_by_owner": [
                     {"id": t["id"], "title": t["title"], "status": t["status"]}
                     for t in blocked],
                 "recent_activity": list(store.timeline(ticket_id))[-8:]}
        return brief

    # ---- Seats / roles (DESIGN-v2: models assigned around tickets) --------

    _seat_router: dict = {}

    def get_seat_router():
        if "r" not in _seat_router:
            from seats import SeatRouter
            _seat_router["r"] = SeatRouter(get_store())
        return _seat_router["r"]

    @app.get("/seats")
    def get_seats():
        return get_seat_router().roster()

    @app.post("/tickets/{ticket_id}/assign", status_code=201)
    def assign_seat(ticket_id: str, req: AssignCreate, request: Request):
        get_ticket_or_404(ticket_id)
        from seats import SeatError
        actor, _via = acting(request, req.actor)
        actor_dict = _actor_as_dict(actor)
        try:
            return get_seat_router().assign(
                ticket_id, role=req.role, seat=req.seat,
                actor=actor_dict)
        except SeatError as e:
            raise HTTPException(status_code=409,
                                detail={"code": e.code, "message": e.message})

    @app.post("/tickets/{ticket_id}/release")
    def release_seat(ticket_id: str, req: ReleaseCreate, request: Request):
        get_ticket_or_404(ticket_id)
        actor, _via = acting(request, req.actor)
        return get_seat_router().release(ticket_id,
                                         actor=_actor_as_dict(actor))

    @app.get("/tickets/{ticket_id}/assignment")
    def ticket_assignment(ticket_id: str):
        get_ticket_or_404(ticket_id)
        r = get_seat_router()
        return {"ticket_id": ticket_id,
                "holder": r.current_holder(ticket_id),
                "contributors": sorted(r.contributors(ticket_id))}

    # ---- Templates (workflow shapes) ---------------------------------------

    @app.get("/templates")
    def templates_list():
        from templates import list_templates
        return {"templates": [
            {"name": t["name"], "description": t.get("description", ""),
             "tags": t.get("tags", []), "steps": len(t.get("steps", [])),
             "source": t.get("source", "repo")}
            for t in list_templates()]}

    @app.get("/templates/{name}")
    def template_get(name: str):
        from templates import _load_named, TemplateError
        try:
            return _load_named(name)
        except TemplateError as e:
            raise HTTPException(status_code=404,
                                detail={"code": e.code, "message": e.message})

    @app.post("/templates/{name}/instantiate", status_code=201)
    def template_instantiate(name: str, req: InstantiateCreate,
                             request: Request):
        from templates import instantiate, TemplateError
        actor, _via = acting(request, req.actor)
        try:
            return instantiate(get_store(), get_seat_router(), name,
                               req.title, parent=req.parent,
                               actor=_actor_as_dict(actor),
                               assign=req.assign)
        except TemplateError as e:
            raise HTTPException(status_code=404,
                                detail={"code": e.code, "message": e.message})

    # ---- Threads / chat bridges ------------------------------------------

    @app.post("/tickets/{ticket_id}/bind", status_code=201)
    def bind_thread(ticket_id: str, req: BindCreate, request: Request):
        get_ticket_or_404(ticket_id)
        if req.platform not in PLATFORMS:
            raise HTTPException(status_code=400,
                                detail={"code": "bad_request",
                                        "message": f"Unknown platform: {req.platform}"})
        actor, via = acting(request, _UNSET)  # identity gate (auth on)
        store = get_store()
        kwargs = ({"via": via}
                  if _supports_kwarg(store.bind_thread, "via") else {})
        return store.bind_thread(
            ticket_id, platform=req.platform, room_id=req.room_id,
            thread_id=req.thread_id, root_msg_id=req.root_msg_id,
            **kwargs,
        )

    @app.get("/tickets/{ticket_id}/bindings")
    def list_bindings(ticket_id: str):
        get_ticket_or_404(ticket_id)
        store = get_store()
        if not hasattr(store, "list_bindings"):
            raise HTTPException(status_code=501,
                                detail={"code": "unsupported",
                                        "message": "Store does not expose bindings"})
        return list(store.list_bindings(ticket_id))

    @app.post("/adapters/{platform}/inbound")
    def adapter_inbound(platform: str, req: InboundMessage, request: Request):
        if platform not in PLATFORMS:
            raise HTTPException(status_code=400,
                                detail={"code": "bad_request",
                                        "message": f"Unknown platform: {platform}"})
        actor, via = acting(request, req.actor)
        store = get_store()
        if not hasattr(store, "get_binding"):
            raise HTTPException(status_code=501,
                                detail={"code": "unsupported",
                                        "message": "Store does not expose bindings"})
        binding = store.get_binding(platform=platform, room_id=req.room_id,
                                    thread_id=req.thread_id)
        if not binding:
            raise HTTPException(
                status_code=404,
                detail={"code": "not_found",
                        "message": f"No thread binding for {platform} room {req.room_id}"},
            )
        actor_dict = _actor_as_dict(actor)
        payload = {
            "platform": platform,
            "room_id": req.room_id,
            "thread_id": req.thread_id,
            "root_msg_id": req.root_msg_id,
            "text": req.text,
            "chat_actor": actor_dict.get("name"),
        }
        kwargs = ({"via": via}
                  if _supports_kwarg(store.append_event, "via") else {})
        event = store.append_event(binding["ticket_id"],
                                   kind="thread_message",
                                   actor=actor_dict,
                                   payload=payload,
                                   **kwargs)
        # Reply-posts-back is Lane C (adapters/) territory; inert in v0.
        return {"event": event, "posted": False}

    # ---- Wiki (DESIGN-v2 pillar 1) ------------------------------------------

    def _wiki_or_404(slug: str) -> dict:
        page = get_store().get_page(slug)
        if page is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "not_found",
                        "message": f"Wiki page {slug} not found"},
            )
        return page

    @app.get("/wiki")
    def list_wiki(request: Request):
        """Index: latest revision per slug (slim, no body)."""
        viewer = getattr(request.state, "viewer", None)
        if viewer is not None and viewer.realm == "external":
            return [{"slug": p["slug"], "title": p["title"], "tags": [],
                     "rev": p["rev"], "ts": p["ts"]}
                    for p in external_reader(request).list_pages()]
        pages = get_store().list_pages()
        return [
            {"slug": p.get("slug"), "title": p.get("title"),
             "tags": p.get("tags", []), "rev": p.get("rev"),
             "ts": p.get("ts")}
            for p in pages
        ]

    @app.get("/wiki-search")
    def search_wiki(request: Request, q: str = ""):
        """Case-insensitive substring match on title/body/tags of the
        latest revision; returns slug/title/snippet."""
        viewer = getattr(request.state, "viewer", None)
        if viewer is not None and viewer.realm == "external":
            return [{"slug": p["slug"], "title": p["title"],
                     "snippet": (p.get("body_md") or "")[:200]}
                    for p in external_reader(request).search_pages(q)]
        store = get_store()
        if not hasattr(store, "search_pages"):
            raise HTTPException(status_code=501,
                                detail={"code": "unsupported",
                                        "message": "Store does not expose wiki"})
        results = []
        for p in store.search_pages(q):
            body = p.get("body_md") or ""
            results.append({"slug": p.get("slug"),
                            "title": p.get("title"),
                            "snippet": body[:200]})
        return results

    @app.get("/wiki/{slug}")
    def get_wiki(slug: str, request: Request):
        """Full latest page + slim history (all revisions, no bodies)."""
        viewer = getattr(request.state, "viewer", None)
        if viewer is not None and viewer.realm == "external":
            reader = external_reader(request)
            page = reader.get_page(slug)
            if page is None:
                raise HTTPException(
                    status_code=404,
                    detail={"code": "not_found",
                            "message": f"Wiki page {slug} not found"},
                )
            history = [{"slug": h["slug"], "rev": h["rev"],
                        "title": h["title"], "tags": [],
                        "actor": "", "ts": h["ts"]}
                       for h in (reader.page_history(slug) or [])]
            return {"page": page, "history": history}
        page = _wiki_or_404(slug)
        history = [
            {"slug": h.get("slug"), "rev": h.get("rev"),
             "title": h.get("title"), "tags": h.get("tags", []),
             "actor": h.get("actor", ""), "ts": h.get("ts")}
            for h in get_store().page_history(slug)
        ]
        return {"page": page, "history": history}

    @app.post("/wiki/{slug}", status_code=201)
    def put_wiki(slug: str, req: WikiPageCreate, request: Request):
        viewer = getattr(request.state, "viewer", None)
        # Fix-round §2/§3: validate the label before any privilege lookup
        # (auth on or off); auth off with no label keeps the legacy store
        # call shape.
        if req.label is None and viewer is None:
            label = None
        else:
            label = check_publish_label(request, req.label)
        actor, _via = acting(request, req.actor)
        store = get_store()
        if label is None and not _store_supports_labels():
            # CONTRACT-GAP: FakeStore.put_page takes no label=; with no label
            # requested the pre-P1 call shape is preserved there.
            return store.put_page(slug, title=req.title, body_md=req.body_md,
                                  tags=list(req.tags), actor=actor)
        # Fix-round §3: FakeStore.put_page takes no label=; an explicit label
        # on a store that cannot record it is a bad request, never a
        # TypeError/500 or a silent drop.
        if label is not None and not _store_supports_labels():
            raise HTTPException(
                status_code=400,
                detail={"code": "bad_request",
                        "message": "label not supported by this store"})
        return store.put_page(
            slug, title=req.title, body_md=req.body_md,
            tags=list(req.tags), actor=actor, label=label)

    @app.post("/wiki/{slug}/label", status_code=200)
    def set_wiki_label(slug: str, req: LabelUpdate, request: Request):
        """Label change = a NEW revision with the same title/body/tags and
        the new label (wiki rows are append-only). Lowering requires owner;
        raising is allowed for any internal writer."""
        from ticket_hub.model import check_label
        check_label(req.label)  # Fix-round §2: validate before any lookup
        page = _wiki_or_404(slug)
        current = page.get("label") or "internal"
        if LABEL_RANK[req.label] < LABEL_RANK[current]:
            require_owner(request)
        actor, _via = acting(request, _UNSET)
        store = get_store()
        if not hasattr(store, "put_page"):
            raise HTTPException(status_code=501,
                                detail={"code": "unsupported",
                                        "message": "Store does not expose wiki"})
        if req.label != current and not _supports_kwarg(store.put_page,
                                                        "label"):
            # Fix-round §3: FakeStore.put_page takes no label=; a real label
            # change cannot be recorded there -> 400, never TypeError/501.
            raise HTTPException(
                status_code=400,
                detail={"code": "bad_request",
                        "message": "label not supported by this store"})
        # A no-op relabel on a label-less store changes nothing: return the
        # unchanged page rather than passing an unsupported label=.
        if req.label == current and not _supports_kwarg(store.put_page,
                                                        "label"):
            return page
        return store.put_page(
            slug, title=page["title"], body_md=page.get("body_md", ""),
            tags=list(page.get("tags") or []), actor=actor,
            label=req.label)
    # ---- Roles (v2 pillar 3) ----------------------------------------------

    def _load_registry() -> dict:
        """Read roles/registry.json fresh at request time (edit = no restart)."""
        path = os.path.join(APP_DIR, "roles", "registry.json")
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            raise HTTPException(
                status_code=404,
                detail={"code": "not_found",
                        "message": "roles/registry.json not found"},
            )
        except (OSError, ValueError) as exc:
            raise HTTPException(
                status_code=503,
                detail={"code": "roles_unavailable",
                        "message": f"role registry unreadable: {exc}"},
            )
        if not isinstance(data, dict) or not isinstance(
                data.get("roles"), list):
            raise HTTPException(
                status_code=503,
                detail={"code": "roles_unavailable",
                        "message": "role registry must be an object "
                                   "with a 'roles' list"},
            )
        return data

    @app.get("/roles")
    def list_roles():
        return _load_registry()

    @app.get("/roles/{role_id}")
    def get_role(role_id: str):
        for role in _load_registry()["roles"]:
            if isinstance(role, dict) and role.get("id") == role_id:
                return role
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found",
                    "message": f"Role {role_id} not found"},
        )

    from jobs_api import register_jobs_routes
    register_jobs_routes(app, get_store)

    # Aerial Tickets product: POST /records, POST /identity, /operator
    # (docs/DESIGN-aerial-tickets.md). Registered before check_policies.
    from rlai_api import register_rlai_routes
    register_rlai_routes(app, get_store, get_access,
                         session_cookie=SESSION_COOKIE)

    # ---- UI ----------------------------------------------------------------

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(os.path.join(UI_DIR, "index.html"))

    app.mount("/ui", StaticFiles(directory=UI_DIR), name="ui")

    # ---- policy completeness (contract §3): raise on a gap ------------------

    if auth_on:
        check_policies(app)

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("TICKET_HUB_PORT", "8777"))
    # Loopback-only posture (SECURITY.md); override only if you know why.
    host = os.environ.get("TICKET_HUB_HOST", "127.0.0.1")
    uvicorn.run(app, host=host, port=port)
