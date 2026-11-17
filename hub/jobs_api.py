"""Jobs API routes (SPEC 'API'): registers /jobs* on the hub FastAPI app.

``register_jobs_routes(app, get_store)`` is called from ``api.create_app`` after
``get_store`` is defined and before the ``/ui`` mount. A ``JobStore`` (same DB
file as the hub, ``TICKET_HUB_DB``) is created lazily and cached on
``app.state.jobs``; tests may pre-set ``app.state.jobs`` before requests.

Errors use the existing JSON envelope:
``HTTPException(status_code, detail={"code": ..., "message": ...})``.

Access P1 (lane W3): every /jobs* route here is registered on the app *before*
``api.create_app`` calls ``check_policies(app)``, so each ``(METHOD, path)``
below has an entry in ``ticket_hub.policy.POLICIES`` (mutations are
``internal_write``, reads ``internal``). The auth middleware in api.py wraps
these handlers, so the full pipeline (token -> policy -> external filtering ->
access_log) applies unchanged. The bodies here carry no ``actor``/``owner``
identity fields (the jobs store records run metadata only), so nothing else
is needed for identity rules; ``digest_publish``'s event actor stays the
fixed system identity, matching its pre-P1 behaviour.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from fastapi import Body, FastAPI, HTTPException, Query, Response

from ticket_hub.jobs import FINAL_STATUSES, JobStore, digest_markdown
from ticket_hub.paths import hub_path

DEFAULT_HUB_DB = hub_path("hub.db")


def _get_jobs(app: FastAPI) -> JobStore:
    """Return the job store, lazily building one from TICKET_HUB_DB."""
    jobs = getattr(app.state, "jobs", None)
    if jobs is None:
        db_path = os.environ.get("TICKET_HUB_DB", DEFAULT_HUB_DB)
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        jobs = JobStore(db_path)
        app.state.jobs = jobs
    return jobs


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _since(since_h: float) -> datetime:
    """A ``since`` cutoff (UTC aware) ``since_h`` hours before now."""
    return _utcnow() - timedelta(hours=float(since_h))


def _make_resolve(get_store: Callable[[], Any]) -> Callable[[str], dict]:
    """Build ``resolve(ticket_id)``: nearest self-or-ancestor (root-first
    ancestry) tagged ``feature`` / ``project`` -> ``{id, title}`` or None."""
    def resolve(ticket_id: str) -> dict:
        ticket = get_store().get(ticket_id)
        if ticket is None:
            return {"feature": None, "project": None}
        # self first, then nearest ancestor (ancestry is root-first).
        chain = [ticket] + list(reversed(get_store().ancestry(ticket_id)))
        picked: dict = {"feature": None, "project": None}
        for level in ("feature", "project"):
            for node in chain:
                if level in (node.get("tags") or []):
                    picked[level] = {"id": node["id"], "title": node["title"]}
                    break
        return picked
    return resolve


def register_jobs_routes(app: FastAPI, get_store: Callable[[], Any]) -> None:
    """Register every /jobs* route on ``app`` (see SPEC 'API')."""
    jobs = lambda: _get_jobs(app)  # noqa: E731 - lazy per-request accessor

    def resolve() -> Callable[[str], dict]:
        return _make_resolve(get_store)

    # ---- list / rollup / digest (must precede /jobs/{job_id}) --------------

    @app.get("/jobs")
    def list_jobs(node: Optional[str] = Query(default=None)):
        return jobs().jobs_view(node=node or None)

    @app.get("/jobs/rollup")
    def rollup(level: str = "feature", since_h: float = 24.0):
        try:
            return jobs().rollup(level, resolve(), since=_since(since_h))
        except ValueError as exc:
            raise HTTPException(status_code=422,
                                detail={"code": "bad_level", "message": str(exc)})

    @app.get("/jobs/digest")
    def digest(since_h: float = 24.0, format: str = "json"):
        d = jobs().digest(resolve(), since=_since(since_h))
        if format == "md":
            return Response(content=digest_markdown(d),
                            media_type="text/markdown")
        return d

    @app.post("/jobs/digest/publish")
    def digest_publish(ticket_id: Optional[str] = Query(default=None),
                       since_h: float = 24.0):
        tid = ticket_id or os.environ.get("JOBS_DIGEST_TICKET")
        if not tid:
            raise HTTPException(
                status_code=422,
                detail={"code": "no_ticket",
                        "message": "no ticket_id and JOBS_DIGEST_TICKET unset"})
        if get_store().get(tid) is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "not_found", "message": f"Ticket {tid} not found"})
        d = jobs().digest(resolve(), since=_since(since_h))
        event = get_store().append_event(
            tid, "checkpoint",
            actor={"name": "jobs-digest", "family": "system", "kind": "agent"},
            payload={"type": "jobs_digest", "coverage": d["coverage"],
                     "totals": d["totals"], "problems": d["problems"][:20],
                     "markdown": digest_markdown(d)})
        if isinstance(event, dict):
            return {"published": True, "ticket_id": tid}
        return {"published": True, "ticket_id": tid,
                "event_id": getattr(event, "id", None)}

    @app.patch("/jobs/runs/{run_id}")
    def end_run(run_id: str, body: dict = Body(default={})):
        try:
            return jobs().end_run(
                run_id, body.get("status"), exit_code=body.get("exit_code"),
                summary=body.get("summary", "") or "",
                log_tail=body.get("log_tail", "") or "",
                meta=body.get("meta"))
        except ValueError as exc:
            raise HTTPException(status_code=422,
                                detail={"code": "bad_status", "message": str(exc)})
        except KeyError as exc:
            raise HTTPException(
                status_code=404,
                detail={"code": "not_found", "message": str(exc).strip("'")})

    # ---- per-job routes (declared last) ------------------------------------

    _JOB_BODY_KEYS = ("name", "node", "kind", "schedule", "interval_s",
                      "ticket_id", "command", "enabled")

    @app.put("/jobs/{job_id}")
    def put_job(job_id: str, body: dict = Body(default={})):
        try:
            return jobs().upsert_job(job_id, **body)
        except ValueError as exc:
            raise HTTPException(status_code=422,
                                detail={"code": "unknown_fields",
                                        "message": str(exc)})

    @app.get("/jobs/{job_id}")
    def get_job(job_id: str):
        job = jobs().get_job(job_id)
        if job is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "not_found", "message": f"Job {job_id} not found"})
        return {**job, "runs": jobs().runs(job_id=job_id, limit=50)}

    @app.post("/jobs/{job_id}/runs", status_code=201)
    def new_run(job_id: str, body: dict = Body(default={})):
        store = jobs()
        job_fields = {k: body[k] for k in _JOB_BODY_KEYS
                      if k in body and k != "node"}
        node = body.get("node", "") or ""
        pid = body.get("pid")
        meta = body.get("meta")
        status = body.get("status")
        if status in FINAL_STATUSES:
            run = store.record_run(
                job_id, status, body.get("started_at"), body.get("ended_at"),
                node=node, pid=pid, meta=meta, exit_code=body.get("exit_code"),
                summary=body.get("summary", "") or "",
                log_tail=body.get("log_tail", "") or "", **job_fields)
        else:
            run = store.start_run(job_id, node=node, pid=pid, meta=meta,
                                  **job_fields)
        return run
