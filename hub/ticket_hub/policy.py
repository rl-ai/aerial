"""Route policies for the access pipeline — ACCESS-P1-CONTRACT §3 (lane W3).

``POLICIES`` maps ``(METHOD, route.path)`` to a policy name. It covers EVERY
route ``api.create_app`` registers (api.py + jobs_api.py, plus the FastAPI
docs routes, the UI mount and the auth/label routes). ``check_policies(app)``
raises ``RuntimeError`` naming every registered route (and every method an
APIRoute accepts) that has no policy — ``create_app`` calls it at the end of
construction whenever auth is on, so a route added without a policy can never
ship.

Head methods are policy-inherited: a HEAD request is always checked against
the GET policy for the same path (see ``policy_for``), so HEAD never needs its
own entry.

Policy vocabulary (contract §3):
    public                  no auth required
    authenticated           any valid viewer (any realm)
    external_read           the six §0 read routes; external viewers are
                            served from ExternalReader only
    internal                every remaining GET
    internal_write_ticket   mutating route with {ticket_id}
    internal_write_wiki     POST /wiki/{slug}, POST /wiki/{slug}/label
    internal_write          every other mutation
"""
from __future__ import annotations

from starlette.routing import Mount, Route

POLICY_NAMES: frozenset[str] = frozenset({
    "public",
    "authenticated",
    "external_read",
    "internal",
    "internal_write_ticket",
    "internal_write_wiki",
    "internal_write",
})

#: the six external-readable routes (contract §0); everything else is internal.
EXTERNAL_READ_ROUTES: frozenset[str] = frozenset({
    "GET /tickets",
    "GET /tickets/{ticket_id}",
    "GET /tickets/{ticket_id}/events",
    "GET /wiki",
    "GET /wiki/{slug}",
    "GET /wiki-search",
})

#: (METHOD, route.path) -> policy. Every entry is exact against the routes
#: api.create_app + register_jobs_routes actually register.
POLICIES: dict[tuple[str, str], str] = {
    # -- public -------------------------------------------------------------
    ("GET", "/health"): "public",
    ("GET", "/login"): "public",
    ("POST", "/session"): "public",
    # The operator mount authorizes itself (admin only, 403 otherwise; see
    # rlai_api.operator_auth). Public here so the pipeline does not 401 first.
    ("GET", "/operator"): "public",
    # -- authenticated (UI/docs/session plumbing; any realm may read) -------
    ("GET", "/"): "authenticated",
    ("GET", "/ui"): "authenticated",  # Mount: /ui/... served under it
    ("GET", "/openapi.json"): "authenticated",
    ("GET", "/docs"): "authenticated",
    ("GET", "/docs/oauth2-redirect"): "authenticated",
    ("GET", "/redoc"): "authenticated",
    ("POST", "/logout"): "authenticated",
    # -- external_read (the six §0 routes) ------------------------------------
    ("GET", "/tickets"): "external_read",
    ("GET", "/tickets/{ticket_id}"): "external_read",
    ("GET", "/tickets/{ticket_id}/events"): "external_read",
    ("GET", "/wiki"): "external_read",
    ("GET", "/wiki/{slug}"): "external_read",
    ("GET", "/wiki-search"): "external_read",
    # -- internal_write_ticket (every mutation with {ticket_id}) --------------
    ("PATCH", "/tickets/{ticket_id}"): "internal_write_ticket",
    ("DELETE", "/tickets/{ticket_id}"): "internal_write_ticket",  # S4
    ("POST", "/tickets/{ticket_id}/events"): "internal_write_ticket",
    ("POST", "/tickets/{ticket_id}/label"): "internal_write_ticket",
    ("POST", "/tickets/{ticket_id}/edges"): "internal_write_ticket",
    ("POST", "/tickets/{ticket_id}/bind"): "internal_write_ticket",
    ("POST", "/tickets/{ticket_id}/assign"): "internal_write_ticket",
    ("POST", "/tickets/{ticket_id}/release"): "internal_write_ticket",
    # -- internal_write_wiki ----------------------------------------------------
    ("POST", "/wiki/{slug}"): "internal_write_wiki",
    ("POST", "/wiki/{slug}/label"): "internal_write_wiki",
    # -- internal_write ---------------------------------------------------------
    ("POST", "/tickets"): "internal_write",
    ("POST", "/adapters/{platform}/inbound"): "internal_write",
    ("POST", "/templates/{name}/instantiate"): "internal_write",
    ("PUT", "/jobs/{job_id}"): "internal_write",
    ("POST", "/jobs/{job_id}/runs"): "internal_write",
    ("PATCH", "/jobs/runs/{run_id}"): "internal_write",
    ("POST", "/jobs/digest/publish"): "internal_write",
    ("POST", "/records"): "internal_write",      # Aerial C2 ingest
    ("POST", "/identity"): "internal_write",     # Aerial C5 identity
    # -- internal (every remaining GET) -----------------------------------------
    ("GET", "/board"): "internal",
    ("GET", "/admin/status"): "internal",
    ("GET", "/tickets/{ticket_id}/lineage"): "internal",
    ("GET", "/tickets/{ticket_id}/digest"): "internal",
    ("GET", "/tickets/{ticket_id}/plan"): "internal",
    ("GET", "/tickets/{ticket_id}/assignment"): "internal",
    ("GET", "/tickets/{ticket_id}/bindings"): "internal",
    ("GET", "/seats"): "internal",
    ("GET", "/templates"): "internal",
    ("GET", "/templates/{name}"): "internal",
    ("GET", "/roles"): "internal",
    ("GET", "/roles/{role_id}"): "internal",
    ("GET", "/jobs"): "internal",
    ("GET", "/jobs/rollup"): "internal",
    ("GET", "/jobs/digest"): "internal",
    ("GET", "/jobs/{job_id}"): "internal",
}


def policy_for(method: str, path: str) -> "str | None":
    """Policy for a (method, path) pair. HEAD inherits the GET policy of the
    same path; every other method must have an explicit entry (None means the
    caller must FAIL CLOSED — authenticate, then 404)."""
    m = method.upper()
    if m == "HEAD":
        m = "GET"
    return POLICIES.get((m, path))


def check_policies(app) -> None:
    """Raise RuntimeError naming every APIRoute/Route/Mount on *app* (per
    HTTP method it accepts) that has no entry in POLICIES."""
    missing: list[str] = []
    for route in app.routes:
        path = getattr(route, "path", None)
        if path is None:
            missing.append(f"<route without a path: {route!r}>")
            continue
        if isinstance(route, Mount):
            methods = ["GET"]  # mounts serve GET; unknown verbs get 405
        elif isinstance(route, Route):
            methods = sorted(m for m in (route.methods or {"GET"})
                             if m != "HEAD")
        else:
            missing.append(f"<unsupported route type {type(route).__name__}: "
                           f"{path!r}>")
            continue
        for method in methods:
            if (method, path) not in POLICIES:
                missing.append(f"({method}, {path})")
    if missing:
        raise RuntimeError("routes without a policy: " + ", ".join(missing))
