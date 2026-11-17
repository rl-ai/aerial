"""Template engine: repo templates, instantiation, substitution, overrides."""
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)

import pytest
from fastapi.testclient import TestClient

import templates as tpl_mod
from api import create_app
from fake_store import FakeStore
from seats import SeatRouter

REG = {"roles": {r: {"seats": ["w1", "w2"], "authority": "build",
                     "max_concurrent": 99}
                 for r in ["pmf", "design", "code-design", "execution",
                           "measurement", "product-owner",
                           "service-manager", "biz-dev"]},
       "rules": {"builder_exclusion": False}}


@pytest.fixture()
def store():
    return FakeStore()


@pytest.fixture()
def router(store):
    return SeatRouter(store, registry=REG,
                      health=lambda: {"w1": "up", "w2": "up"})


def test_repo_templates_all_load():
    names = {t["name"] for t in tpl_mod.list_templates()}
    assert {"feature", "bugfix", "infra", "spike", "project",
            "biz-opportunity", "forge-cycle"} <= names


def test_instantiate_creates_tree(store, router):
    res = tpl_mod.instantiate(store, router, "feature", "Dark mode",
                              actor={"name": "owner"})
    root = res["root"]
    assert root["title"] == "Dark mode"
    assert "feature" in root["tags"]
    kids = store.children(root["id"])
    assert len(kids) == 6
    titles = [k["title"] for k in kids]
    assert "PMF check: Dark mode" in titles          # {title} substituted
    assert any("Build: Dark mode" == t for t in titles)
    # every step got a seat assignment
    for k in kids:
        assert router.current_holder(k["id"]) is not None


def test_nested_steps(store, router):
    res = tpl_mod.instantiate(store, router, "forge-cycle", "Cycle 14",
                              actor={"name": "owner"})
    root = res["root"]
    m = [n for n in res["tree"] if n["title"].startswith("Integrate")][0]
    assert len(m["children"]) == 1
    grandkids = store.children(m["id"])
    assert len(grandkids) == 1
    assert grandkids[0]["title"] == "Clean-clone verify"
    # progress rolls up: 0/8 done at creation
    desc = store.descendants(root["id"])
    assert len(desc) == 6   # 5 steps + 1 nested verify


def test_assign_failure_does_not_abort(store):
    bad = SeatRouter(store, registry={"roles": {}, "rules": {}})
    res = tpl_mod.instantiate(store, bad, "bugfix", "Crash on launch")
    assert store.children(res["root"]["id"])       # tickets still created
    errs = [n["assign_error"] for n in res["tree"] if n["assign_error"]]
    assert errs and all(e == "unknown_role" for e in errs)


def test_user_override_wins(store, router, tmp_path, monkeypatch):
    (tmp_path / "feature.json").write_text(json.dumps(
        {"name": "feature", "description": "personal",
         "tags": ["mine"], "steps": [{"title": "Just do {title}",
                                      "role": "execution"}]}))
    monkeypatch.setattr(tpl_mod, "USER_TEMPLATES", str(tmp_path))
    t = tpl_mod._load_named("feature")
    assert t["description"] == "personal"
    res = tpl_mod.instantiate(store, router, "feature", "X")
    assert len(store.children(res["root"]["id"])) == 1


def test_unknown_template(store, router):
    with pytest.raises(tpl_mod.TemplateError) as e:
        tpl_mod.instantiate(store, router, "nope", "t")
    assert e.value.code == "not_found"


def test_bad_name_rejected():
    with pytest.raises(tpl_mod.TemplateError) as e:
        tpl_mod._load_named("../etc/passwd")
    assert e.value.code == "bad_name"


# ---- API --------------------------------------------------------------------

def _client(tmp_path, monkeypatch):
    reg = tmp_path / "roles.json"
    reg.write_text(json.dumps({"version": 1, "roles": [
        {"id": r, "mission": "m", "quality_bar": "q", "inputs": ["i"],
         "outputs": ["o"], "seat_pref": "s", "default_model_hint": "x",
         "max_parallel": 99, "seats": ["w1", "w2"]}
        for r in ["pmf", "design", "code-design", "execution",
                  "measurement", "product-owner", "service-manager",
                  "biz-dev"]]}))
    monkeypatch.setenv("TICKET_HUB_ROLES", str(reg))
    monkeypatch.setenv("TICKET_HUB_SEATS_CMD", "true")
    return TestClient(create_app(store=FakeStore()))


def test_templates_api(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    r = client.get("/templates")
    assert r.status_code == 200
    names = {t["name"] for t in r.json()["templates"]}
    assert "feature" in names
    r = client.get("/templates/feature")
    assert r.status_code == 200 and len(r.json()["steps"]) == 6
    r = client.get("/templates/ghost")
    assert r.status_code == 404


def test_instantiate_api(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    r = client.post("/templates/spike/instantiate",
                    json={"title": "Local embeddings?"})
    assert r.status_code == 201, r.text
    root = r.json()["root"]
    got = client.get(f"/tickets/{root['id']}/plan").json()
    assert got["progress"]["total"] == 4
