"""Wiki lane tests (DESIGN-v2 pillar 1): store roundtrip/append-only,
API endpoints (FakeStore via TestClient + real Store via gateway),
CLI roundtrip, and seed_wiki idempotency/freshness + machine attribution
(GLM v2 review requirements)."""
import os
import sqlite3
import sys

import pytest
from fastapi.testclient import TestClient

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)

from api import create_app
from fake_store import FakeStore
from hub_cli import main
from hub_client import HubClient
from hub_gateway import StoreGateway
from ticket_hub.store import Store

import importlib.util as _ilu
_spec = _ilu.spec_from_file_location(
    "seed_wiki", os.path.join(os.path.dirname(_HERE), "scripts",
                              "seed_wiki.py"))
assert _spec and _spec.loader
seed_wiki = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(seed_wiki)


@pytest.fixture()
def cli(hub_base_url):
    """Same pattern as tests/test_cli.py: drive hub_cli.main() against the
    in-process server (tmp DB via TICKET_HUB_DB from conftest)."""
    def run(*argv) -> tuple[int, str, str]:
        import contextlib
        import io
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(list(argv), client=HubClient(base_url=hub_base_url))
        return code, out.getvalue(), err.getvalue()
    return run


@pytest.fixture()
def store(tmp_path):
    s = Store(str(tmp_path / "hub.db"))
    yield s
    s.close()


# ---------------------------------------------------------------- store ----

def test_put_get_roundtrip(store):
    p = store.put_page("runbook", "Runbook", "body text",
                       tags=["ops", "docs"], actor="owner")
    assert p.id.startswith("WP-")
    assert p.slug == "runbook" and p.rev == 1
    assert p.tags == ["ops", "docs"] and p.actor == "owner"
    got = store.get_page("runbook")
    assert got == p
    assert store.get_page("nope") is None
    # dataclass round-trips through dict
    assert got.to_dict()["title"] == "Runbook"
    assert got.slim().get("body_md") is None


def test_rev_increments_and_history(store):
    store.put_page("ops", "Ops v1", "one", actor="owner")
    p2 = store.put_page("ops", "Ops v2", "two", actor="hermes")
    p3 = store.put_page("ops", "Ops v3", "three", actor="glm")
    assert [p.rev for p in (p2, p3)] == [2, 3]
    latest = store.get_page("ops")
    assert latest.rev == 3 and latest.title == "Ops v3"
    hist = store.page_history("ops")
    assert [p.rev for p in hist] == [1, 2, 3]
    assert [p.title for p in hist] == ["Ops v1", "Ops v2", "Ops v3"]
    assert hist[0].body_md == "one"  # old revisions stay intact


def test_list_pages_latest_per_slug(store):
    store.put_page("a", "A1", "")
    store.put_page("a", "A2", "")
    store.put_page("b", "B1", "")
    pages = store.list_pages()
    assert [p.slug for p in pages] == ["a", "b"]
    assert {p.rev for p in pages} == {1, 2}  # a is at rev 2, b at rev 1


def test_search_pages_case_insensitive_substring(store):
    store.put_page("alpha", "Alpha Plan", "nothing special",
                   tags=["build"], actor="owner")
    store.put_page("beta", "Beta", "mentions the FORGE loop here", actor="owner")
    store.put_page("gamma", "Gamma", "plain", tags=["FORGE-tag"], actor="owner")
    assert {p.slug for p in store.search_pages("alpha")} == {"alpha"}
    assert {p.slug for p in store.search_pages("FORGE")} == {"beta", "gamma"}
    assert store.search_pages("zzz-nothing") == []


def test_put_page_validates(store):
    with pytest.raises(ValueError):
        store.put_page("", "no slug")
    with pytest.raises(ValueError):
        store.put_page("ok", "   ")


def test_no_page_mutation_api(store):
    for name in ("update_page", "delete_page", "edit_page", "remove_page"):
        assert not hasattr(Store, name)


def test_db_triggers_block_page_update_delete(store):
    store.put_page("locked", "Locked", "body")
    raw = sqlite3.connect(store.db_path)
    with pytest.raises(sqlite3.IntegrityError):
        raw.execute("UPDATE wiki_pages SET title = 'tampered'")
    with pytest.raises(sqlite3.IntegrityError):
        raw.execute("DELETE FROM wiki_pages")
    raw.close()
    assert store.get_page("locked").title == "Locked"


def test_unique_slug_rev_constraint(store):
    store.put_page("dup", "first")
    raw = sqlite3.connect(store.db_path)
    with pytest.raises(sqlite3.IntegrityError):
        raw.execute(
            "INSERT INTO wiki_pages (page_id, slug, rev, title, ts) "
            "VALUES ('WP-x', 'dup', 1, 'collision', '2026-01-01T00:00:00Z')")
    raw.close()


# ------------------------------------------------- gateway + API (real) ----

@pytest.fixture()
def gw_client(tmp_path):
    store = Store(str(tmp_path / "gw.db"))
    app = create_app(store=StoreGateway(store))
    client = TestClient(app)
    yield client
    store.close()


def _post(client, slug, payload):
    return client.post(f"/wiki/{slug}", json=payload)


def test_gateway_dict_shapes(store):
    gw = StoreGateway(store)
    page = gw.put_page("gw", title="GW", body_md="body", tags=["a"],
                       actor={"name": "hermes"})
    assert isinstance(page, dict)
    assert page["actor"] == "hermes" and page["tags"] == ["a"]
    assert isinstance(gw.get_page("gw"), dict)
    assert isinstance(gw.list_pages()[0], dict)
    assert isinstance(gw.page_history("gw")[0], dict)
    assert isinstance(gw.search_pages("body")[0], dict)


def test_api_endpoints_via_gateway(gw_client):
    c = gw_client
    # empty index
    assert c.get("/wiki").json() == []
    # 404 on missing page, JSON error envelope
    miss = c.get("/wiki/ghost")
    assert miss.status_code == 404
    assert miss.json()["error"]["code"] == "not_found"
    # create rev 1
    r = _post(c, "guide", {"title": "Guide", "body_md": "rev one body",
                           "tags": ["docs"], "actor": "owner"})
    assert r.status_code == 201, r.text
    assert r.json()["rev"] == 1
    # index entry
    idx = c.get("/wiki").json()
    assert idx == [{"slug": "guide", "title": "Guide", "tags": ["docs"],
                    "rev": 1, "ts": idx[0]["ts"]}]
    # new revision
    r2 = _post(c, "guide", {"title": "Guide v2",
                            "body_md": "rev two body", "tags": ["docs"]})
    assert r2.status_code == 201 and r2.json()["rev"] == 2
    # full page + slim history
    detail = c.get("/wiki/guide").json()
    assert detail["page"]["rev"] == 2
    assert detail["page"]["body_md"] == "rev two body"
    assert [h["rev"] for h in detail["history"]] == [1, 2]
    assert all("body_md" not in h for h in detail["history"])  # slim
    # search hits latest only, with snippet
    res = c.get("/wiki-search?q=rev two").json()
    assert [r["slug"] for r in res] == ["guide"]
    assert res[0]["snippet"].startswith("rev two body")
    assert c.get("/wiki-search?q=absentterm").json() == []
    # validation: missing title -> 422 JSON envelope
    bad = c.post("/wiki/guide", json={"body_md": "no title"})
    assert bad.status_code == 422
    assert bad.json()["error"]["code"] == "validation"


def test_api_endpoints_via_fake_store():
    c = TestClient(create_app(store=FakeStore()))
    r = _post(c, "fake", {"title": "F", "body_md": "fake body",
                          "tags": ["t"], "actor": {"name": "glm"}})
    assert r.status_code == 201 and r.json()["actor"] == "glm"
    assert c.get("/wiki").json()[0]["slug"] == "fake"
    assert c.get("/wiki/fake").json()["page"]["rev"] == 1
    assert c.get("/wiki-search?q=FAKE body").json()[0]["slug"] == "fake"
    assert c.get("/wiki/missing").status_code == 404


# ------------------------------------------------------------------ CLI ----

def test_cli_wiki_set_get_roundtrip(cli):
    code, out, err = cli("wiki", "set", "cli-page", "--title", "CLI Page",
                         "--body", "hello wiki", "--tag", "cli",
                         "--actor", "owner")
    assert code == 0, err
    assert "cli-page" in out and "r1" in out
    # get human view shows body + actor
    code, out, err = cli("wiki", "get", "cli-page")
    assert code == 0, err
    assert "hello wiki" in out and "actor: owner" in out
    # get --json includes slim history
    code, out, _ = cli("wiki", "get", "cli-page", "--json")
    detail = out and __import__("json").loads(out)
    assert detail["page"]["title"] == "CLI Page"
    assert detail["history"][0]["rev"] == 1
    # second revision increments
    code, out, err = cli("wiki", "set", "cli-page", "--title", "CLI Page v2",
                         "--body", "second")
    assert code == 0 and "r2" in out, err
    # ls lists latest
    code, out, _ = cli("wiki", "ls")
    assert "cli-page  r2  CLI Page v2" in out
    code, out, _ = cli("wiki", "ls", "--json")
    pages = __import__("json").loads(out)
    assert any(p["slug"] == "cli-page" and p["rev"] == 2 for p in pages)


def test_cli_wiki_set_from_file(cli, tmp_path):
    src = tmp_path / "note.md"
    src.write_text("file body content\n", encoding="utf-8")
    code, out, err = cli("wiki", "set", "from-file", "--title", "From File",
                         "--file", str(src))
    assert code == 0, err
    code, out, _ = cli("wiki", "get", "from-file")
    assert "file body content" in out


def test_cli_wiki_set_requires_title_and_bodies_exclusive(cli):
    code, _, err = cli("wiki", "set", "bad", "--body", "x")
    assert code == 1 and "--title" in err
    code, _, err = cli("wiki", "set", "bad", "--title", "T",
                       "--body", "a", "--file", "b")
    assert code == 1 and "not both" in err


def test_cli_wiki_search_and_missing(cli):
    cli("wiki", "set", "findme", "--title", "Find Me",
        "--body", "uniqueneedle text")
    code, out, err = cli("wiki", "search", "uniqueneedle")
    assert code == 0 and "findme" in out, err
    code, out, _ = cli("wiki", "search", "nothingmatches", "--json")
    assert code == 0 and __import__("json").loads(out) == []
    code, _, err = cli("wiki", "get", "ghost-page")
    assert code == 1 and "not_found" in err


# --------------------------------------------------------------- seed ----

@pytest.fixture()
def fake_repo(tmp_path):
    """A minimal repo dir with one doc; ops pages are script-embedded."""
    (tmp_path / "README.md").write_text("# Test README\n\nhello.\n",
                                        encoding="utf-8")
    return str(tmp_path)


def test_seed_creates_pages_with_machine_actor(store, fake_repo):
    recs = seed_wiki.seed(store, fake_repo)
    by_slug = {r["slug"]: r for r in recs}
    assert by_slug["readme"]["action"] == "created"
    assert by_slug["readme"]["rev"] == 1
    # every write carries the machine actor, never a human name
    for rec in recs:
        page = store.get_page(rec["slug"])
        assert page.actor == seed_wiki.MACHINE_ACTOR == "seed-import"
    # source quote line + wiki links
    body = store.get_page("readme").body_md
    assert body.startswith("> source: REPO README.md")
    assert "[[cli]]" in body
    ops = store.get_page("ops-runbook").body_md
    assert ops.startswith("> source: ops knowledge")
    assert "[[forge-loop]]" in store.get_page("cluster-seats").body_md


def test_seed_idempotent_second_run_skips(store, fake_repo):
    seed_wiki.seed(store, fake_repo)
    recs = seed_wiki.seed(store, fake_repo)
    assert all(r["action"] == "skipped" for r in recs)
    assert store.get_page("readme").rev == 1
    assert len(store.page_history("readme")) == 1


def test_seed_reposts_changed_source_as_rev2(store, fake_repo):
    """GLM requirement: changed repo doc -> new revision, actor seed-import."""
    seed_wiki.seed(store, fake_repo)
    with open(os.path.join(fake_repo, "README.md"), "a",
              encoding="utf-8") as fh:
        fh.write("\nFresh section added.\n")
    recs = seed_wiki.seed(store, fake_repo)
    by_slug = {r["slug"]: r for r in recs}
    assert by_slug["readme"]["action"] == "updated"
    assert by_slug["readme"]["rev"] == 2
    page = store.get_page("readme")
    assert "Fresh section added." in page.body_md
    assert page.actor == "seed-import"
    # untouched pages stay at rev 1
    assert store.get_page("glossary").rev == 1
    hist = store.page_history("readme")
    assert [h.rev for h in hist] == [1, 2]
    assert all(h.actor == "seed-import" for h in hist)


def test_seed_corpus_covers_required_slugs():
    """Seeding the REAL repo root (in-memory store — nothing touched on
    disk) must cover every required slug."""
    s = Store(":memory:")
    try:
        slugs = {r["slug"] for r in seed_wiki.seed(s, seed_wiki._REPO_ROOT)}
    finally:
        s.close()
    required = {"readme", "design-v1", "design-v2", "contributing",
                "security", "changelog", "cli", "api", "agent-guide",
                "deploy", "terminology", "forge-loop", "ops-runbook",
                "cluster-seats", "glossary", "decision-log"}
    assert required <= slugs


def test_seed_no_secrets_or_real_ips_in_ops_pages():
    """Ops pages must carry placeholders/localhost only."""
    import re
    for slug, _title, _tags, content in seed_wiki._ops_pages():
        assert not re.search(
            r"\b(?!127\.0\.0\.1|0\.0\.0\.0)(?:\d{1,3}\.){3}\d{1,3}\b",
            content), slug
        lowered = content.lower()
        for marker in ("sk-", "password", "secret_token", "api_key= "):
            assert marker not in lowered, (slug, marker)
