"""Tests for the Jobs API routes (SPEC 'Tests' / tests/test_jobs_api.py).

Runs FastAPI TestClient against the REAL app with a real StoreGateway(Store)
and a tmp JobStore sharing the same DB file.
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)

import pytest
from fastapi.testclient import TestClient

from api import create_app
from hub_gateway import StoreGateway
from ticket_hub.jobs import JobStore
from ticket_hub.store import Store


@pytest.fixture()
def hub(tmp_path):
    db = str(tmp_path / "hub.db")
    app = create_app(store=StoreGateway(Store(db)))
    app.state.jobs = JobStore(db)   # tests set the store before requests
    return TestClient(app)


# ---------------------------------------------------------------- jobs ------

def test_put_and_get_job(hub):
    r = hub.put("/jobs/m:scraper",
                json={"name": "Scraper", "node": "node-b", "interval_s": 3600})
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "Scraper" and r.json()["interval_s"] == 3600
    got = hub.get("/jobs/m:scraper")
    assert got.status_code == 200
    body = got.json()
    assert body["job_id"] == "m:scraper" and isinstance(body["runs"], list)


def test_get_unknown_job_404(hub):
    r = hub.get("/jobs/nope")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


def test_put_unknown_fields_422(hub):
    r = hub.put("/jobs/j1", json={"color": "red"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "unknown_fields"


def test_list_jobs_and_node_filter(hub):
    hub.put("/jobs/a", json={"node": "node-a"})
    hub.put("/jobs/b", json={"node": "node-b"})
    all_jobs = hub.get("/jobs").json()
    assert {j["job_id"] for j in all_jobs} == {"a", "b"}
    assert all("status" in j for j in all_jobs)  # jobs_view shape
    node_a = hub.get("/jobs?node=node-a").json()
    assert [j["job_id"] for j in node_a] == ["a"]


# ---------------------------------------------------------------- runs ------

def test_start_run_then_end_run(hub):
    start = hub.post("/jobs/m:cron/runs",
                     json={"node": "node-b", "pid": 4242, "meta": {"k": 1}})
    assert start.status_code == 201, start.text
    run = start.json()
    assert run["status"] == "running" and run["pid"] == 4242
    assert run["meta"] == {"k": 1}
    assert hub.get("/jobs/m:cron").json()["name"] == "m:cron"  # auto-registered
    done = hub.patch(f"/jobs/runs/{run['run_id']}",
                     json={"status": "ok", "exit_code": 0, "summary": "fine"})
    assert done.status_code == 200
    assert done.json()["status"] == "ok" and done.json()["exit_code"] == 0
    assert done.json()["duration_s"] is not None


def test_one_shot_run_final_status(hub):
    r = hub.post("/jobs/rep/runs",
                 json={"status": "failed", "exit_code": 3, "summary": "boom",
                       "node": "node-a", "interval_s": 3600})
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "failed" and r.json()["exit_code"] == 3
    assert hub.get("/jobs/rep").json()["interval_s"] == 3600


def test_patch_end_run_404_and_422(hub):
    start = hub.post("/jobs/j/runs", json={}).json()
    bad = hub.patch(f"/jobs/runs/{start['run_id']}", json={"status": "nope"})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "bad_status"
    miss = hub.patch("/jobs/runs/run_missing", json={"status": "ok"})
    assert miss.status_code == 404 and miss.json()["error"]["code"] == "not_found"


def test_get_job_returns_last_50_runs(hub):
    for _ in range(55):
        hub.post("/jobs/busy/runs", json={"status": "ok"})
    body = hub.get("/jobs/busy").json()
    assert len(body["runs"]) == 50


# ------------------------------------------------------- rollup (real tree) --

def _ticket(hub, title, **kw):
    return hub.post("/tickets", json={"title": title, **kw}).json()["id"]


def _link(hub, parent, child):
    r = hub.post(f"/tickets/{parent}/edges",
                 json={"dst": child, "type": "parent_of"})
    assert r.status_code in (200, 201), r.text


def test_rollup_groups_by_feature_and_project(hub):
    proj = _ticket(hub, "Proj", tags=["project"])
    feat = _ticket(hub, "Feat", tags=["feature"])
    _link(hub, proj, feat)
    leaf = _ticket(hub, "Leaf")
    _link(hub, feat, leaf)
    hub.put("/jobs/tied", json={"ticket_id": leaf, "node": "node-a"})
    hub.put("/jobs/free", json={"node": "node-a"})
    hub.post("/jobs/tied/runs", json={"status": "ok", "summary": "good"})
    hub.post("/jobs/free/runs", json={"status": "ok"})

    by_feat = {g["id"]: g for g in hub.get("/jobs/rollup?level=feature").json()}
    assert feat in by_feat and None in by_feat
    assert by_feat[feat]["jobs"] == 1 and by_feat[feat]["ok"] == 1
    assert by_feat[feat]["success_rate"] == 1.0
    assert by_feat[None]["title"] == "(no parent)" and by_feat[None]["jobs"] == 1

    by_proj = {g["id"]: g for g in hub.get("/jobs/rollup?level=project").json()}
    assert proj in by_proj and by_proj[proj]["jobs"] == 1

    bad = hub.get("/jobs/rollup?level=epic")
    assert bad.status_code == 422


# ------------------------------------------------------------- digest -------

def test_digest_json_and_markdown(hub):
    hub.post("/jobs/s1/runs", json={"status": "ok", "interval_s": 900})
    d = hub.get("/jobs/digest").json()
    assert d["coverage"]["registered"] == 1
    assert d["totals"]["runs"] == 1 and d["totals"]["ok"] == 1
    assert set(d) == {"generated_at", "since", "coverage", "totals",
                      "problems", "projects"}
    md = hub.get("/jobs/digest?format=md")
    assert md.status_code == 200
    assert md.headers["content-type"].startswith("text/markdown")
    assert "# Jobs digest" in md.text


# ------------------------------------------------------------- publish ------

def test_publish_appends_checkpoint_event(hub):
    proj = _ticket(hub, "Proj", tags=["project"])
    r = hub.post(f"/jobs/digest/publish?ticket_id={proj}")
    assert r.status_code == 200 and r.json()["published"] is True
    events = hub.get(f"/tickets/{proj}/events").json()
    cp = [e for e in events if e["kind"] == "checkpoint"]
    assert cp and cp[-1]["payload"]["type"] == "jobs_digest"
    assert "coverage" in cp[-1]["payload"] and "markdown" in cp[-1]["payload"]


def test_publish_requires_ticket(hub, monkeypatch):
    monkeypatch.delenv("JOBS_DIGEST_TICKET", raising=False)
    r = hub.post("/jobs/digest/publish")
    assert r.status_code == 422 and r.json()["error"]["code"] == "no_ticket"
    miss = hub.post("/jobs/digest/publish?ticket_id=TK-NOPE")
    assert miss.status_code == 404


def test_publish_default_ticket_from_env(hub, monkeypatch):
    proj = _ticket(hub, "Proj", tags=["project"])
    monkeypatch.setenv("JOBS_DIGEST_TICKET", proj)
    r = hub.post("/jobs/digest/publish")
    assert r.status_code == 200 and r.json()["ticket_id"] == proj


# ------------------------------------------------- route order + serving -----

def test_rollup_route_not_shadowed_by_job(hub):
    # /jobs/rollup must resolve to the rollup route, not GET /jobs/{job_id}.
    hub.put("/jobs/rollup", json={"name": "a job literally named rollup"})
    resp = hub.get("/jobs/rollup?level=feature")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_jobs_js_is_served(hub):
    r = hub.get("/ui/jobs.js")
    assert r.status_code == 200
    assert "registerJobsView" in r.text or "jobs-body" in r.text
