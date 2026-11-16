"""Unit tests for the secondbrain-mcp gateway (design §3-§4): governance
reuse, refusal taxonomy, fail-closed posture, ledger audit. The end-to-end
stdio run is verified separately on a clean venv (acceptance criterion 3).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from secondbrain.vault import L0Vault, label_rank
from secondbrain_mcp.server import GatewayError, GatewaySession, MCPServer

from dogfood.prepare_data import prepare


@pytest.fixture(scope="module")
def data(tmp_path_factory):
    return prepare(tmp_path_factory.mktemp("dogfood"))


@pytest.fixture()
def env(tmp_path_factory):
    root = tmp_path_factory.mktemp("gateway")
    prepared = prepare(root)
    planner = next(g for g in prepared["grants"]
                   if "planner" in g["counterparty"])
    worker = next(g for g in prepared["grants"]
                  if "worker" in g["counterparty"])
    data_dir = Path(prepared["data_dir"])
    gw = GatewaySession(data_dir, planner["grant_id"], planner["key"])
    return gw, prepared, planner, worker, data_dir


def test_brain_search_returns_labeled_citation_carrying_results(env):
    gw, _, _, _, _ = env
    out = gw.brain_search({"query": "squat progress"})
    assert out["results"], "expected in-scope gym hits"
    for item in out["results"]:
        assert item["label"] in ("public", "personal", "intimate")
        assert len(item["citations"]) == 1
        assert item["citations"][0] == item["hash"]
        assert item["source"] and item["ingested_at"]
        assert "sensitivity" in item and "entities" in item
    assert out["refused_count"] >= 0
    # the intimate journal IS a candidate for a matching query, and is
    # withheld: counted, never shown, in any form
    intimate_out = gw.brain_search({"query": "difficult conversation"})
    assert intimate_out["results"] == []
    assert intimate_out["refused_count"] == 1
    # intimate content never appears, in any form
    assert all(label_rank(r["label"]) < label_rank("intimate")
               for r in out["results"])
    assert "difficult conversation" not in str(out)


def test_brain_get_intimate_refusal_is_structured_success(env):
    gw, prepared, planner, _, _ = env
    data_dir = Path(prepared["data_dir"])
    vault = L0Vault(data_dir / "l0")
    intimate = next(h for h in vault.all_hashes()
                    if vault.get(h).sensitivity == "intimate")
    result = gw.brain_get({"hash": intimate})
    assert result == {"refused": True, "reason": "intimate"}


def test_brain_get_outside_scope_refusal(env):
    gw, prepared, _, worker, data_dir = env
    # the worker's scope is the clinic cluster; ask for a gym-cluster hash
    vault = L0Vault(data_dir / "l0")
    clinic_gw = GatewaySession(data_dir, worker["grant_id"], worker["key"])
    # find a gym original: it is outside the clinic grant's closure
    planner_results = gw.brain_search({"query": "squat"})
    assert planner_results["results"]
    gym_hash = planner_results["results"][0]["hash"]
    result = clinic_gw.brain_get({"hash": gym_hash})
    assert result == {"refused": True, "reason": "outside_scope"}


def test_brain_get_unknown_hash_is_not_found(env):
    gw, _, _, _, _ = env
    result = gw.brain_get({"hash": "0" * 64})
    assert result == {"refused": True, "reason": "not_found"}


def test_brain_get_returns_full_sanitized_document(env):
    gw, prepared, _, _, data_dir = env
    vault = L0Vault(data_dir / "l0")
    results = gw.brain_search({"query": "squat"})
    h = results["results"][0]["hash"]
    doc = gw.brain_get({"hash": h})
    assert doc["hash"] == h and not doc.get("refused")
    assert doc["citations"] == [h]
    assert doc["provenance"]["source"] == vault.get(h).source
    assert "Rua Aurora 12" not in doc["text"]      # location generalized


def test_grant_invalid_is_a_tool_error_not_a_refusal(env):
    gw, prepared, planner, _, data_dir = env
    bad = GatewaySession(data_dir, planner["grant_id"], "sk-g" + "0" * 32)
    with pytest.raises(GatewayError, match="grant_invalid"):
        bad.brain_search({"query": "squat"})
    with pytest.raises(GatewayError, match="grant_invalid"):
        bad.brain_get({"hash": "0" * 64})


def test_every_call_lands_in_the_grant_ledger(env):
    gw, _, _, _, _ = env
    before = len(gw.book.entries(gw.grant_id))
    gw.brain_search({"query": "squat"})
    gw.brain_get({"hash": "0" * 64})                # a refusal too
    entries = gw.book.entries(gw.grant_id)
    assert len(entries) == before + 2               # audited, no new store
    assert entries[-1]["state"] == "refused"


def test_fail_closed_on_unreadable_workspace(tmp_path):
    """A corrupt l1.json must fail closed with GatewayError, never serve
    from a degraded workspace (SPEC §3.2)."""
    root = tmp_path / "gateway"
    prepared = prepare(root)
    planner = next(g for g in prepared["grants"]
                   if "planner" in g["counterparty"])
    data_dir = Path(prepared["data_dir"])
    (data_dir / "l1" / "l1.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(GatewayError, match="vault_unreadable"):
        GatewaySession(data_dir, planner["grant_id"], planner["key"])


def test_search_limits_are_clamped(env):
    gw, _, _, _, _ = env
    out = gw.brain_search({"query": "squat", "limit": 999})
    assert len(out["results"]) <= 25


def test_server_exposes_exactly_two_tools(env):
    gw, _, _, _, _ = env
    mcp = MCPServer(gw)
    reply = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = sorted(t["name"] for t in reply["result"]["tools"])
    assert names == ["brain_get", "brain_search"]
    # and the JSON-RPC framing round-trips a real tool call fail-closed
    call = mcp.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                       "params": {"name": "brain_get",
                                  "arguments": {"hash": "0" * 64}}})
    assert call["result"]["structuredContent"] == {
        "refused": True, "reason": "not_found"}
    assert json.loads(call["result"]["content"][0]["text"]) == \
        call["result"]["structuredContent"]
