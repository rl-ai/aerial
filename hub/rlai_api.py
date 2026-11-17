"""Aerial (rlai) Tickets-product routes: ``POST /records``, ``POST /identity``
and the ``/operator`` mount. See docs/DESIGN-aerial-tickets.md.

``register_rlai_routes`` is called from ``api.create_app`` before
``check_policies``; every route here has an entry in
``ticket_hub.policy.POLICIES``. The Aerial packages load lazily from
``RLAI_PACKAGES_DIR``: without them the POST routes answer 503 and the
operator mount is not registered.
"""
from __future__ import annotations

import contextlib
import os
import sys
from typing import Any, Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from ticket_hub import rlai

OPERATOR_ENV = "RLAI_OPERATOR"


def _unavailable(exc: Exception) -> HTTPException:
    # the reason may name a host path: keep it out of the response
    return HTTPException(status_code=503, detail={
        "code": "rlai_unavailable",
        "message": "the Aerial packages are not available on this hub"})


async def _json_object(request: Request) -> dict:
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail={
            "code": "bad_request", "message": "body must be JSON"})
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail={
            "code": "bad_request", "message": "body must be a JSON object"})
    return data


def register_rlai_routes(app: FastAPI, get_store: Callable[[], Any],
                         get_access: Callable[[], Any], *,
                         session_cookie: str) -> None:
    """Register /records, /identity and (behind RLAI_OPERATOR=1) /operator.
    *session_cookie* is the hub's session cookie name (api.SESSION_COOKIE)."""

    def hub_store():
        st = get_store()
        inner = getattr(st, "_store", st)
        if not hasattr(inner, "_append_event"):
            raise HTTPException(status_code=503, detail={
                "code": "rlai_unavailable",
                "message": "the store is not a hub Store"})
        return inner

    def identities() -> rlai.IdentityStore:
        ids = getattr(app.state, "rlai_identities", None)
        if ids is None:
            ids = rlai.IdentityStore(hub_store()._conn)
            app.state.rlai_identities = ids
        try:
            with ids._lock:
                ids._require_schema()  # a check only; never DDL here
        except rlai.RlaiUnavailable as exc:
            raise _unavailable(exc)
        return ids

    # ---- schema: created once, before any request (never on a request) ----

    def init_schema() -> None:
        """Never fatal: the hub must start even if the Aerial table cannot
        be made; the identity routes then answer 503 (table missing)."""
        try:
            st = hub_store()
        except HTTPException:
            return  # no hub Store (e.g. a test double): routes answer 503
        try:
            rlai.ensure_identity_schema(st._conn)
        except Exception as exc:  # noqa: BLE001 - logged, hub keeps running
            print(f"rlai: identity schema not created "
                  f"({type(exc).__name__}); /identity and /records will "
                  "answer 503", file=sys.stderr)

    if app.state.store is not None:
        init_schema()  # store given to create_app: no request has run yet
    else:
        # production builds the store lazily; build it and the schema in
        # the lifespan start-up, before uvicorn serves the first request
        inner_lifespan = app.router.lifespan_context

        @contextlib.asynccontextmanager
        async def lifespan(asgi_app):
            init_schema()
            async with inner_lifespan(asgi_app) as state:
                yield state

        app.router.lifespan_context = lifespan

    def record_store():
        rs = getattr(app.state, "rlai_records", None)
        if rs is None:
            try:
                rs = rlai.hub_record_store(hub_store())
            except rlai.RlaiUnavailable as exc:
                raise _unavailable(exc)
            app.state.rlai_records = rs
        return rs

    def contracts():
        try:
            return rlai.contracts()
        except rlai.RlaiUnavailable as exc:
            raise _unavailable(exc)

    # ---- C2 records -------------------------------------------------------

    @app.post("/records")
    async def post_record(request: Request):
        """Ingest one C2 record through rlai_tickets record() (201 new,
        200 duplicate)."""
        message = await _json_object(request)
        mod = contracts()
        errors = mod.validate("c2-record", message)
        if errors and not all("unknown-enum" in e for e in errors):
            raise HTTPException(status_code=422, detail={
                "code": "invalid_c2", "message": "; ".join(errors)})
        viewer = getattr(request.state, "viewer", None)
        is_owner = bool(viewer is not None and viewer.is_owner)
        employee_id = message.get("employee_id")
        ids = identities()
        # viewer is None only with auth off (TICKET_HUB_AUTH=off, dev only):
        # there is no poster to check, so only registration applies.
        if not is_owner:
            if not ids.is_registered(employee_id):
                raise HTTPException(status_code=403, detail={
                    "code": "unknown_employee",
                    "message": f"{employee_id} has no registered identity"})
            if viewer is not None and viewer.actor.id not in (
                    employee_id, ids.registrar(employee_id)):
                raise HTTPException(status_code=403, detail={
                    "code": "not_registrar",
                    "message": "only the employee, its registrar or an "
                               "owner may post its records"})
        via = None
        if viewer is not None:
            via = viewer.via or viewer.actor.id
        from rlai_tickets.store.errors import InvalidC2Message, StoreError
        rs = record_store()
        if hub_store().get_ticket(message.get("ticket_id")) is None:
            raise HTTPException(status_code=404, detail={
                "code": "not_found",
                "message": f"Ticket {message.get('ticket_id')} not found"})
        try:
            event, duplicate = rs.record_as(message, via=via)
        except InvalidC2Message as exc:
            raise HTTPException(status_code=422, detail={
                "code": "invalid_c2", "message": str(exc)})
        except StoreError as exc:
            raise HTTPException(status_code=400, detail={
                "code": "bad_request", "message": str(exc)})
        payload = event.get("payload") or {}
        body = {"duplicate": duplicate,
                "event": {"id": event.get("id"),
                          "ticket_id": event.get("ticket_id"),
                          "seq": event.get("seq"), "kind": event.get("kind"),
                          "ts": event.get("ts"),
                          "message_id": (payload.get("record") or {}
                                         ).get("message_id"),
                          "seq_gap": bool(payload.get("seq_gap")),
                          "unrecognized": bool(payload.get("unrecognized"))}}
        return JSONResponse(body, status_code=200 if duplicate else 201)

    # ---- C5 identity --------------------------------------------------------

    @app.post("/identity")
    async def post_identity(request: Request):
        """Register one C5 identity (201 new, 200 duplicate). The token_ref
        is stored as a path reference and never returned."""
        message = await _json_object(request)
        errors = contracts().validate("c5-identity", message)
        if errors:
            # field names only: a validator message may quote a value, and
            # token_ref must never be echoed
            import re
            fields = sorted({head for head in (e.split(":", 1)[0].strip()
                                               for e in errors)
                             if re.fullmatch(r"[a-z_]{1,40}", head)})
            raise HTTPException(status_code=422, detail={
                "code": "invalid_c5",
                "message": "invalid fields: " + ", ".join(fields)})
        viewer = getattr(request.state, "viewer", None)
        is_owner = bool(viewer is not None and viewer.is_owner)
        # auth off (dev only) has no viewer: every check below is a no-op
        if message["kind"] == "human" and viewer is not None \
                and not is_owner:
            raise HTTPException(status_code=403, detail={
                "code": "forbidden",
                "message": "only an owner may register a human identity"})
        ids = identities()
        if viewer is not None and not is_owner \
                and ids.is_registered(message["employee_id"]) \
                and ids.registrar(message["employee_id"]) != viewer.actor.id:
            raise HTTPException(status_code=403, detail={
                "code": "not_registrar",
                "message": "only the first registrar or an owner may "
                           "re-register this employee"})
        try:
            first = ids.accept(
                message, access=get_access(),
                received_by=viewer.actor.id if viewer is not None else None)
        except rlai.IdentityConflict as exc:
            raise HTTPException(status_code=409, detail={
                "code": "kind_conflict", "message": str(exc)})
        body = {"employee_id": message["employee_id"],
                "message_id": message["message_id"],
                "duplicate": not first}
        return JSONResponse(body, status_code=201 if first else 200)

    # ---- /operator (A4 hub integration) -----------------------------------

    if os.environ.get(OPERATOR_ENV, "").strip() != "1":
        return
    try:
        rlai.load_packages()
        from rlai_operator.page import asgi_app
    except Exception:  # noqa: BLE001 - a broken package must not stop the hub
        return  # flag on but no usable package: stay unmounted (404)

    def _token(headers: dict) -> "str | None":
        scheme, _, creds = (headers.get("authorization") or "").partition(" ")
        if scheme.lower() == "bearer" and creds.strip():
            return creds.strip()
        for part in (headers.get("cookie") or "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == session_cookie and value:
                return value
        return None

    def operator_auth(headers: dict) -> "str | None":
        """The hub admin (an owner viewer) only; anything else is None
        (the package answers 403). Every decision is audited."""
        viewer, access = None, None
        try:
            access = get_access()
            token = _token(headers)
            viewer = access.viewer_for_token(token) if token else None
        except Exception:  # noqa: BLE001 - deny (still audited if possible)
            viewer = None
        allowed = viewer is not None and viewer.is_owner \
            and viewer.realm == "internal"
        if access is not None:
            try:
                access.log_access(
                    actor_id=viewer.actor.id if viewer else None,
                    credential_id=viewer.credential_id if viewer else None,
                    method="GET", route="/operator",
                    status=200 if allowed else 403,
                    decision="allow" if allowed else "deny")
            except Exception:  # noqa: BLE001 - auditing never breaks a reply
                pass
        return viewer.actor.id if allowed else None

    def view_fn(operator_id: str) -> dict:
        return rlai.build_operator_view(get_store(), operator_id)

    app.mount("/operator", asgi_app(view_fn, authorize=operator_auth),
              name="operator")
