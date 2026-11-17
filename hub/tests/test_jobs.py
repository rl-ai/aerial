"""Tests for ticket_hub.jobs — JobStore data lane (SPEC 'Tests')."""
import threading
from datetime import datetime, timedelta, timezone

import pytest

from ticket_hub.jobs import JobStore, digest_markdown, job_status

NOW = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)


def ago(**kw):
    return NOW - timedelta(**kw)


@pytest.fixture()
def store(tmp_path):
    s = JobStore(str(tmp_path / "jobs.db"))
    yield s
    s.close()


# ---------------------------------------------------------------- jobs ----

def test_upsert_create_update_keeps_created_at(store):
    j = store.upsert_job("m:scraper", name="Scraper", node="node-b",
                         interval_s=3600, now=NOW)
    assert j["name"] == "Scraper" and j["interval_s"] == 3600
    assert j["created_at"] == j["updated_at"] == "2026-10-28T12:00:00Z"
    j2 = store.upsert_job("m:scraper", schedule="*/30 * * * *", now=ago(hours=1))
    assert j2["created_at"] == "2026-10-28T12:00:00Z"
    assert j2["updated_at"] == "2026-10-28T11:00:00Z"
    assert j2["schedule"] == "*/30 * * * *" and j2["name"] == "Scraper"


def test_upsert_unknown_field_raises(store):
    with pytest.raises(ValueError):
        store.upsert_job("j1", color="red")


def test_upsert_update_binds_sorted_fields(store):
    # SET names are sorted; values must follow the same order (review fix 1)
    store.upsert_job("j1", now=NOW)
    j = store.upsert_job("j1", kind="agent", command="c", now=NOW)  # non-alpha kwargs
    assert j["kind"] == "agent" and j["command"] == "c"


def test_concurrent_start_end_runs(store):
    # shared sqlite connection must be serialized by one RLock (review fix 2)
    errors = []

    def worker(t):
        try:
            for _ in range(20):
                run = store.start_run(f"job-{t}", now=NOW)
                store.end_run(run["run_id"], "ok", exit_code=0, now=NOW)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(8)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert not errors
    runs = store.runs(limit=200)
    assert len(runs) == 160
    assert all(r["status"] == "ok" for r in runs)


def test_record_run_dedupe_key_returns_existing(store):
    # meta.dedupe_key folds repeats per job instead of inserting (review fix 3)
    first = store.record_run("j", "ok", ago(hours=2), ago(hours=1),
                             meta={"dedupe_key": "hermes:42"},
                             exit_code=0, summary="first")
    again = store.record_run("j", "failed", ago(hours=1), NOW,
                             meta={"dedupe_key": "hermes:42"},
                             summary="second")
    assert again["run_id"] == first["run_id"]
    assert again["status"] == "ok" and again["summary"] == "first"
    other = store.record_run("j", "ok", ago(minutes=30), NOW,
                             meta={"dedupe_key": "hermes:43"})
    assert other["run_id"] != first["run_id"]
    plain1 = store.record_run("k", "ok", ago(hours=2), NOW)
    plain2 = store.record_run("k", "ok", ago(hours=2), NOW)  # no dedupe_key -> both
    assert plain1["run_id"] != plain2["run_id"]
    assert len(store.runs(job_id="j")) == 2 and len(store.runs(job_id="k")) == 2


def test_upsert_defaults_and_enabled_flag(store):
    j = store.upsert_job("j1", now=NOW)
    assert j["name"] == "j1"          # name defaults to job_id
    assert j["kind"] == "cron" and j["enabled"] == 1
    j = store.upsert_job("j1", enabled=False, now=NOW)
    assert j["enabled"] == 0


def test_list_jobs_filters(store):
    store.upsert_job("a", node="node-a", now=NOW)
    store.upsert_job("b", node="node-b", now=NOW)
    store.upsert_job("c", node="node-a", enabled=False, now=NOW)
    assert [j["job_id"] for j in store.list_jobs()] == ["a", "b"]
    assert [j["job_id"] for j in store.list_jobs(node="node-a")] == ["a"]
    assert [j["job_id"] for j in
            store.list_jobs(include_disabled=True)] == ["a", "b", "c"]


# ---------------------------------------------------------------- runs ----

def test_start_run_auto_registers_job(store):
    run = store.start_run("m:cron", node="node-b", pid=4242, now=NOW,
                          schedule="17 */6 * * *", interval_s=21600,
                          ticket_id="T-1", kind="cron")
    job = store.get_job("m:cron")
    assert job["name"] == "m:cron" and job["interval_s"] == 21600
    assert job["ticket_id"] == "T-1" and job["node"] == "node-b"
    assert run["run_id"].startswith("run_") and run["status"] == "running"
    assert run["pid"] == 4242 and run["started_at"] == "2026-10-28T12:00:00Z"


def test_start_run_updates_existing_job_fields(store):
    store.upsert_job("j", name="Keep me", now=NOW)
    store.start_run("j", now=NOW, interval_s=600)
    job = store.get_job("j")
    assert job["name"] == "Keep me" and job["interval_s"] == 600


def test_start_end_run_duration_and_meta(store):
    run = store.start_run("j", meta={"a": 1}, now=NOW)
    done = store.end_run(run["run_id"], "ok", exit_code=0, summary="fine",
                         meta={"b": 2}, now=NOW + timedelta(seconds=60))
    assert done["status"] == "ok" and done["exit_code"] == 0
    assert done["ended_at"] == "2026-10-28T12:01:00Z"
    assert done["duration_s"] == pytest.approx(60.0)
    assert done["meta"] == {"a": 1, "b": 2}


def test_end_run_validates(store):
    run = store.start_run("j", now=NOW)
    with pytest.raises(ValueError):
        store.end_run(run["run_id"], "running")
    with pytest.raises(KeyError):
        store.end_run("run_nope", "ok", now=NOW)


def test_summary_and_log_tail_truncation(store):
    run = store.start_run("j", now=NOW)
    done = store.end_run(run["run_id"], "failed",
                         summary="x" * 600,
                         log_tail="h" * 5000 + "END", now=NOW)
    assert len(done["summary"]) == 500
    assert len(done["log_tail"]) == 4000 and done["log_tail"].endswith("END")


def test_record_run_one_shot(store):
    run = store.record_run("rep", "ok", ago(hours=2), ago(minutes=90),
                           exit_code=0, summary="done", node="node-a",
                           interval_s=3600)
    assert run["status"] == "ok"
    assert run["duration_s"] == pytest.approx(1800.0)
    assert store.get_job("rep")["interval_s"] == 3600
    assert run["started_at"] == "2026-10-28T10:00:00Z"
    with pytest.raises(ValueError):
        store.record_run("rep", "running", ago(hours=1), NOW)


def test_runs_ordering_since_limit(store):
    for h in (1, 2, 3):
        store.record_run("j", "ok", ago(hours=h), ago(hours=h), node="node-a")
        store.record_run("k", "failed", ago(hours=h), ago(hours=h))
    all_runs = store.runs()
    assert len(all_runs) == 6
    assert [r["started_at"] for r in all_runs] == sorted(
        (r["started_at"] for r in all_runs), reverse=True)
    assert len(store.runs(job_id="j")) == 3
    assert len(store.runs(since=ago(hours=2, minutes=30))) == 4  # j+k newest two
    assert len(store.runs(limit=4)) == 4


# -------------------------------------------------------------- status ----

def s(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def fake_job(interval=None, created=NOW):
    return {"job_id": "j", "interval_s": interval, "created_at": s(created)}


def fake_run(status, started):
    return {"status": status, "started_at": s(started)}


def test_status_stuck():
    # horizon = max(2*interval_s, 6h)
    j = fake_job(interval=3600)
    assert job_status(j, fake_run("running", ago(hours=7)), NOW) == "stuck"
    assert job_status(j, fake_run("running", ago(hours=5)), NOW) == "running"  # 6h floor
    j2 = fake_job(interval=14400)                    # 2*interval = 8h dominates
    assert job_status(j2, fake_run("running", ago(hours=7)), NOW) == "running"
    assert job_status(j2, fake_run("running", ago(hours=9)), NOW) == "stuck"
    # no interval -> 6h horizon
    j3 = fake_job()
    assert job_status(j3, fake_run("running", ago(hours=7)), NOW) == "stuck"
    assert job_status(j3, fake_run("running", ago(hours=5)), NOW) == "running"


def test_status_overdue_incl_never_run():
    # never ran, created long enough ago -> overdue; fresh -> unknown
    assert job_status(fake_job(3600, ago(hours=2)), None, NOW) == "overdue"
    assert job_status(fake_job(3600, ago(hours=1)), None, NOW) == "unknown"
    # last run too old -> overdue beats the run's own status
    assert job_status(fake_job(3600), fake_run("ok", ago(hours=2)), NOW) == "overdue"
    assert job_status(fake_job(3600), fake_run("failed", ago(hours=2)), NOW) == "overdue"
    # no interval -> never overdue
    assert job_status(fake_job(), fake_run("ok", ago(days=5)), NOW) == "ok"


def test_status_failed_ok_unknown():
    recent = ago(minutes=10)
    assert job_status(fake_job(), fake_run("failed", recent), NOW) == "failed"
    assert job_status(fake_job(), fake_run("timeout", recent), NOW) == "failed"
    assert job_status(fake_job(), fake_run("ok", recent), NOW) == "ok"
    assert job_status(fake_job(), fake_run("skipped", recent), NOW) == "ok"
    assert job_status(fake_job(), None, NOW) == "unknown"


# ------------------------------------------------------------ jobs_view ----

def test_jobs_view_counts_and_next_due(store):
    store.upsert_job("j", interval_s=3600, now=ago(days=2))
    store.record_run("j", "ok", ago(hours=1), ago(hours=1))
    store.record_run("j", "failed", ago(hours=2), ago(hours=2))
    store.record_run("j", "ok", ago(hours=30), ago(hours=30))  # outside 24h
    store.upsert_job("n", now=NOW)
    views = {v["job_id"]: v for v in store.jobs_view(now=NOW)}
    v = views["j"]
    assert v["status"] == "ok" and v["runs_24h"] == 2 and v["fails_24h"] == 1
    assert v["next_due"] == "2026-10-28T12:00:00Z"  # 11:00 start + 3600s
    assert v["last_run"]["status"] == "ok"
    n = views["n"]
    assert n["status"] == "unknown" and n["next_due"] is None
    assert n["last_run"] is None and n["runs_24h"] == 0


# --------------------------------------------------------------- rollup ----

TREE = {"T-f1": {"feature": {"id": "F1", "title": "Feat one"},
                 "project": {"id": "P1", "title": "Proj one"}}}


def seeded_store(store):
    calls = []

    def resolve(tid):
        calls.append(tid)
        return TREE.get(tid)

    store.upsert_job("j-a", ticket_id="T-f1", interval_s=600, now=ago(days=2))
    store.start_run("j-a", now=ago(hours=7))          # stuck: 7h > max(1200, 6h)
    store.record_run("j-b", "ok", ago(minutes=10), ago(minutes=10), ticket_id="T-f1")
    store.record_run("j-b", "ok", ago(minutes=20), ago(minutes=20), ticket_id="T-f1")
    store.record_run("j-c", "failed", ago(minutes=10), ago(minutes=10),
                     summary="boom")
    return resolve, calls


def test_rollup_grouping_and_status_precedence(store):
    resolve, calls = seeded_store(store)
    groups = {g["id"]: g for g in store.rollup("feature", resolve, now=NOW)}
    assert set(groups) == {"F1", None}
    assert groups[None]["title"] == "(no parent)"
    f1, un = groups["F1"], groups[None]
    assert f1["jobs"] == 2 and f1["stuck"] == 1 and f1["status"] == "stuck"
    assert (f1["runs"], f1["ok"], f1["failed"]) == (3, 2, 0)  # incl. j-a's running run
    assert f1["success_rate"] == 1.0 and f1["last_failure"] is None
    assert un["jobs"] == 1 and un["status"] == "failed" and un["success_rate"] == 0.0
    assert un["last_failure"]["summary"] == "boom"
    assert un["last_failure"]["job_id"] == "j-c"
    ordered = store.rollup("feature", resolve, now=NOW)
    assert [g["id"] for g in ordered] == ["F1", None]  # no-parent last


def test_rollup_caches_resolve_per_ticket(store):
    resolve, calls = seeded_store(store)
    store.rollup("feature", resolve, now=NOW)
    store.rollup("project", resolve, now=NOW)
    assert calls == ["T-f1", "T-f1"]  # once per call, not per job


def test_rollup_bad_level(store):
    with pytest.raises(ValueError):
        store.rollup("epic", lambda t: None, now=NOW)


# --------------------------------------------------------------- digest ----

def seeded_digest(store):
    store.record_run("s1", "ok", ago(minutes=10), ago(minutes=10),
                     interval_s=900, ticket_id="T-f1")
    store.upsert_job("s2", interval_s=3600, ticket_id="T-f1", now=NOW)
    store.upsert_job("u1", now=NOW)
    return lambda tid: TREE.get(tid)


def test_digest_coverage_and_totals(store):
    resolve = seeded_digest(store)
    d = store.digest(resolve, now=NOW)
    assert d["coverage"] == {"registered": 2, "reporting": 1, "pct": 50.0}
    assert d["totals"] == {"jobs": 3, "runs": 1, "ok": 1, "failed": 0,
                           "overdue": 0, "stuck": 0}
    assert d["problems"] == [] and d["since"] == s(ago(days=1))


def test_digest_tree_and_markdown(store):
    resolve = seeded_digest(store)
    d = store.digest(resolve, now=NOW)
    projs = {p["id"]: p for p in d["projects"]}
    p1 = projs["P1"]
    assert p1["title"] == "Proj one" and p1["status"] == "ok"
    feat = p1["features"][0]
    assert (feat["id"], len(feat["jobs"]), feat["jobs"][0]["runs"]) == ("F1", 2, 1)
    assert projs[None]["title"] == "(no parent)"
    md = store.digest_markdown(d)
    assert "# Jobs digest 2026-10-28T12:00:00Z" in md
    assert "1/2 reporting (50.0%)" in md and "No problems." in md
    assert "## Proj one" in md
    assert "- Feat one: 2 jobs, 1 runs, 0 failed [ok]" in md
    assert digest_markdown(d) == md  # module function and method agree


def test_digest_problems_lists_overdue(store):
    resolve = seeded_digest(store)
    store.upsert_job("s3", interval_s=600, name="S3", node="node-a", now=ago(hours=2))
    d = store.digest(resolve, now=NOW)
    assert d["totals"]["overdue"] == 1
    assert d["problems"] == [{"job_id": "s3", "name": "S3", "node": "node-a",
                              "status": "overdue", "last_summary": "",
                              "ticket_id": None}]
    assert "Problems:" in digest_markdown(d)


def test_digest_markdown_length_cap():
    d = {"generated_at": s(NOW), "since": s(ago(days=1)),
         "coverage": {"registered": 0, "reporting": 0, "pct": 100.0},
         "totals": {"jobs": 0, "runs": 0, "ok": 0, "failed": 0,
                    "overdue": 0, "stuck": 0},
         "problems": [],
         "projects": [{"id": f"P{i}", "title": "Project " + "x" * 80,
                       "status": "ok",
                       "features": [{"id": f"F{i}", "title": "Feat", "status": "ok",
                                     "jobs": []}]}
                      for i in range(100)]}
    md = digest_markdown(d)
    assert len(md) < 4000
    assert "more" in md and "… " in md


# ---------------------------------------------------------------- prune ----

def test_prune_keeps_running_runs(store):
    old_ok = store.record_run("j", "ok", ago(days=40), ago(days=40))
    old_running = store.start_run("j", now=ago(days=40))
    fresh = store.record_run("j", "ok", ago(days=1), ago(days=1))
    assert store.prune(30, now=NOW) == 1
    remaining = {r["run_id"] for r in store.runs()}
    assert remaining == {old_running["run_id"], fresh["run_id"]}



def test_auto_register_accepts_name_and_node_fields(tmp_path):
    # reporters send name/node with the first run; auto-register must not pass them twice
    js = JobStore(str(tmp_path / "h.db"))
    run = js.start_run("n:a", node="n", name="A job", ticket_id="T1", interval_s=900)
    assert js.get_job("n:a")["name"] == "A job" and js.get_job("n:a")["ticket_id"] == "T1"
    js.end_run(run["run_id"], "ok")
    one = js.record_run("n:b", "ok", "2026-10-28T00:00:00Z", "2026-10-28T00:01:00Z",
                        node="n", name="B job", kind="cron")
    assert js.get_job("n:b")["name"] == "B job" and one["status"] == "ok"
