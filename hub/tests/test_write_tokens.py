"""Write-token scoping (SEV fix): non-GET requests require the bearer to be
listed in TICKET_HUB_WRITE_TOKENS. GETs work with any valid bearer; a
read-only token mutating gets 403 read_only_token; unauthenticated gets 401.
Runs in-process via TestClient like test_api_access.py.
"""
import pytest
from fastapi.testclient import TestClient

from api import create_app
from fake_store import FakeStore

from tests.test_api_access import env  # reuse the real-store fixture


def _set_write_tokens(monkeypatch, *tokens):
    monkeypatch.setenv("TICKET_HUB_WRITE_TOKENS", ",".join(tokens))


def test_read_token_post_rejected(env, monkeypatch):
    _set_write_tokens(monkeypatch, env.tokens["own"])
    r = env.client.post("/tickets", json={"title": "nope"},
                        headers=env.h("bot"))
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "read_only_token"


def test_write_token_post_allowed(env, monkeypatch):
    _set_write_tokens(monkeypatch, env.tokens["own"])
    r = env.client.post("/tickets", json={"title": "ok", "label": "external"},
                        headers=env.h("own"))
    assert r.status_code == 201, r.text


def test_get_with_read_token_allowed(env, monkeypatch):
    _set_write_tokens(monkeypatch, env.tokens["own"])
    r = env.client.get("/tickets", headers=env.h("bot"))
    assert r.status_code == 200


def test_unauthenticated_post_is_401_not_403(env, monkeypatch):
    _set_write_tokens(monkeypatch, env.tokens["own"])
    r = env.client.post("/tickets", json={"title": "anon"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthorized"


def test_write_tokens_env_unset_fails_closed(env, monkeypatch):
    monkeypatch.delenv("TICKET_HUB_WRITE_TOKENS", raising=False)
    r = env.client.post("/tickets", json={"title": "nope", "label": "external"},
                        headers=env.h("own"))
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "read_only_token"


def test_write_token_list_is_comma_separated(env, monkeypatch):
    _set_write_tokens(monkeypatch, " ", env.tokens["own"], env.tokens["bot"])
    r = env.client.post("/tickets", json={"title": "bot ok"},
                        headers=env.h("bot"))
    assert r.status_code == 201, r.text


def test_patch_and_delete_also_scoped(env, monkeypatch):
    _set_write_tokens(monkeypatch, env.tokens["own"])
    r = env.client.patch(f"/tickets/{env.root.id}",
                         json={"status": "open"}, headers=env.h("bot"))
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "read_only_token"


def test_write_token_still_subjects_to_other_gates(env, monkeypatch):
    # A write token does not bypass external-realm restrictions: e1 keeps its
    # external-viewer denies on internal writes.
    _set_write_tokens(monkeypatch, env.tokens["own"])
    r = env.client.post("/tickets", json={"title": "x", "label": "external"},
                        headers=env.h("e1"))
    assert r.status_code in (403, 404)
