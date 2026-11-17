"""Roles lane tests: registry schema + /roles API endpoints."""
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)

import pytest
from fastapi.testclient import TestClient

from api import create_app
from fake_store import FakeStore

REGISTRY_PATH = os.path.join(os.path.dirname(_HERE), "roles", "registry.json")

EXPECTED_ROLES = {"design", "pmf", "code-design", "execution",
                  "measurement", "product-owner", "orchestrator"}


def test_registry_has_the_core_charter_roles(registry):
    # Core charters are required; other roles (e.g. service-manager,
    # biz-dev) may be added without breaking this contract.
    ids = [r["id"] for r in registry["roles"]]
    assert EXPECTED_ROLES.issubset(set(ids))
    assert len(ids) == len(set(ids)), "no duplicate role ids"

REQUIRED_FIELDS = {
    "id": str,
    "mission": str,
    "quality_bar": str,
    "inputs": list,
    "outputs": list,
    "seat_pref": str,
    "default_model_hint": str,
    "max_parallel": int,
}


@pytest.fixture()
def client():
    return TestClient(create_app(store=FakeStore()))


@pytest.fixture(scope="module")
def registry():
    with open(REGISTRY_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


# ---- registry file schema ----------------------------------------------------

def test_registry_top_level_shape(registry):
    assert registry.get("version") == 1
    assert isinstance(registry.get("roles"), list)


def test_registry_each_charter_schema(registry):
    for role in registry["roles"]:
        for field, typ in REQUIRED_FIELDS.items():
            assert field in role, f"{role.get('id')}: missing {field}"
            assert isinstance(role[field], typ), \
                f"{role['id']}.{field}: expected {typ.__name__}"
        assert role["mission"].strip()
        assert role["quality_bar"].strip()
        assert role["inputs"] and all(isinstance(x, str) and x.strip()
                                      for x in role["inputs"])
        assert role["outputs"] and all(isinstance(x, str) and x.strip()
                                       for x in role["outputs"])
        assert role["max_parallel"] >= 1


# ---- /roles API ---------------------------------------------------------------

def test_roles_index_serves_registry(client, registry):
    resp = client.get("/roles")
    assert resp.status_code == 200
    body = resp.json()
    assert EXPECTED_ROLES.issubset({r["id"] for r in body["roles"]})


def test_roles_read_at_request_time(client, registry, tmp_path, monkeypatch):
    """Editing the file needs no restart: the endpoint re-reads every call."""
    import api as api_mod
    roles_dir = tmp_path / "roles"
    roles_dir.mkdir()
    (roles_dir / "registry.json").write_text(json.dumps({"version": 1, "roles": [
        {"id": "temp-role", "mission": "m", "quality_bar": "q",
         "inputs": ["i"], "outputs": ["o"], "seat_pref": "s",
         "default_model_hint": "x", "max_parallel": 1}]}))
    # Re-point APP_DIR's roles dir, then build the app so its closure
    # captures the patched path (edit = no restart behavior).
    monkeypatch.setattr(api_mod, "APP_DIR", str(tmp_path))
    fresh = TestClient(create_app(store=FakeStore()))
    resp = fresh.get("/roles/temp-role")
    assert resp.status_code == 200
    assert resp.json()["id"] == "temp-role"


def test_role_by_id(client):
    resp = client.get("/roles/execution")
    assert resp.status_code == 200
    role = resp.json()
    assert role["id"] == "execution"
    assert "evidence" in role["quality_bar"].lower()


def test_unknown_role_404_error_envelope(client):
    resp = client.get("/roles/nope")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"
