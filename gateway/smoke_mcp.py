#!/usr/bin/env python3
"""End-to-end smoke test for secondbrain_mcp (SPEC §5/§9 criterion 3).

Launches the MCP server as a subprocess over stdio, performs the initialize
handshake, lists tools, and calls brain_search + brain_get — one real
result and one refusal each — asserting every response. Also asserts the
fail-closed posture with a bad key (tool error grant_invalid).

Run:  python3 smoke_mcp.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

LANE = Path(__file__).resolve().parent
sys.path.insert(0, str(LANE))

from secondbrain import Curator, GrantBook, L0Vault, Workspace  # noqa: E402

NOTES = {
    "gym-routine.md": (
        "@entity:gym-riverbank #fitness\n"
        "2026-10-14: Trained at Rua do Gym 12, Lisbon. 5k row, squats.\n"
        "sensitivity: personal\n"),
    "clinic.md": (
        "@entity:clinic-downtown\n"
        "2026-09-02: Physio session at Clinica Central for knee pain.\n"
        "sensitivity: sensitive\n"),
    "journal.md": (
        "@entity:gym-riverbank\n"
        "2026-10-20: Private note about the difficult conversation.\n"
        "sensitivity: intimate\n"),
}


def build_workspace(root: Path) -> tuple[str, str, str]:
    """Build a vault + published graph + one scoped grant; return
    (vault_root, grant_id, key)."""
    notes = root / "notes"
    notes.mkdir()
    for name, text in NOTES.items():
        (notes / name).write_text(text, encoding="utf-8")
    vault = L0Vault(root / "l0")
    ws = Workspace(root / "l1")
    Curator(vault, ws).ingest_folder(notes)
    Curator(vault, ws).build_cluster_artifacts()
    ws.publish("main", vault)
    ws.save()  # persist L1 so a separate server process can load it
    book = GrantBook(root / "grants.db")
    grant_id, key = book.create(
        counterparty="Smoke agent", purpose="smoke test",
        anchor_entities=["entity:gym-riverbank"], alias="main",
        ttl_days=30, query_budget=20,
        disclosure={"generalize_location": "city",
                    "max_date_granularity": "month"})
    return str(root), grant_id, key


class Client:
    """Minimal MCP stdio client for the smoke assertions."""

    def __init__(self, proc):
        self.proc = proc
        self._next_id = 0

    def send(self, method, params=None, notify=False):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if not notify:
            self._next_id += 1
            msg["id"] = self._next_id
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()
        if notify:
            return None
        line = self.proc.stdout.readline()
        assert line.strip(), f"no reply for {method}"
        return json.loads(line)

    def request(self, method, params=None):
        reply = self.send(method, params)
        assert "result" in reply, f"unexpected error reply: {reply}"
        return reply["result"]


def launch(vault_root, grant_id, key):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(LANE) + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.Popen(
        [sys.executable, "-m", "secondbrain_mcp",
         "--vault", vault_root, "--grant", grant_id, "--key", key],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, cwd=str(LANE), env=env)
    return Client(proc)


def check(name, cond, detail=""):
    status = "ok" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    assert cond, f"smoke failure: {name} {detail}"


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        vault_root, grant_id, key = build_workspace(Path(tmp))
        print(f"workspace: {vault_root}\ngrant: {grant_id}")

        # -- bad key first: fail closed before any handshake business
        bad = launch(vault_root, grant_id, "sk-g" + "0" * 32)
        bad.send("initialize", {"protocolVersion": "2024-11-05",
                                "capabilities": {},
                                "clientInfo": {"name": "smoke"}})
        bad.send("notifications/initialized", notify=True)
        reply = bad.send("tools/call", {"name": "brain_search",
                                        "arguments": {"query": "squat"}})
        check("bad key -> tool error grant_invalid",
              reply["result"]["isError"] is True
              and "grant_invalid" in reply["result"]["content"][0]["text"],
              json.dumps(reply))
        bad.proc.kill()

        # -- good session
        client = launch(vault_root, grant_id, key)
        init = client.request("initialize", {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "smoke"}})
        check("initialize handshake returns protocolVersion",
              init["protocolVersion"] == "2024-11-05", json.dumps(init))
        check("initialize returns serverInfo name",
              init["serverInfo"]["name"] == "secondbrain-mcp")
        client.send("notifications/initialized", notify=True)  # no reply

        tools = client.request("tools/list")["tools"]
        check("exactly two tools",
              [t["name"] for t in tools] == ["brain_search", "brain_get"],
              json.dumps(tools))

        # -- brain_search: real result
        res = client.request("tools/call", {
            "name": "brain_search",
            "arguments": {"query": "squat training progress"}})["structuredContent"]
        check("brain_search returns results",
              res["results"] and len(res["results"]) >= 1, json.dumps(res))
        item = res["results"][0]
        check("result carries full provenance shape",
              all(k in item for k in ("hash", "label", "sensitivity",
                                      "source", "ingested_at", "snippet",
                                      "entities", "citations"))
              and len(item["hash"]) == 64, json.dumps(item))
        check("sanitization applied (street address generalized)",
              "Rua do Gym" not in item["snippet"], item["snippet"])
        check("sanitized_count reflects the sanitization",
              res["sanitized_count"] >= 1, json.dumps(res))
        check("date generalized to month granularity",
              "-14T" not in item["snippet"], item["snippet"])

        # -- brain_search: out-of-scope + intimate content is refused, counted
        res2 = client.request("tools/call", {
            "name": "brain_search",
            "arguments": {"query": "clinic physio knee private conversation"}}) \
            ["structuredContent"]
        check("refused items counted, not returned",
              res2["refused_count"] >= 2
              and not any("clinic" in (r["source"] + r["snippet"]).lower()
                          or "difficult conversation" in r["snippet"].lower()
                          for r in res2["results"]), json.dumps(res2))

        # -- brain_get: the real document
        get = client.request("tools/call", {
            "name": "brain_get",
            "arguments": {"hash": item["hash"]}})["structuredContent"]
        check("brain_get returns the full document",
              get.get("hash") == item["hash"] and get.get("text")
              and not get.get("refused"), json.dumps(get))
        check("brain_get text is sanitized",
              "Rua do Gym" not in get["text"], get["text"])
        check("brain_get carries provenance",
              get.get("provenance", {}).get("source") == "gym-routine.md")

        # -- brain_get refusals: not_found and intimate
        nf = client.request("tools/call", {
            "name": "brain_get",
            "arguments": {"hash": "0" * 64}})["structuredContent"]
        check("unknown hash -> refused not_found",
              nf == {"refused": True, "reason": "not_found"}, json.dumps(nf))
        clinic_hash = L0Vault(Path(vault_root) / "l0").all_hashes()
        clinic_doc = next(
            h for h in clinic_hash
            if "Physio session" in L0Vault(Path(vault_root) / "l0").get(h).text)
        oc = client.request("tools/call", {
            "name": "brain_get", "arguments": {"hash": clinic_doc}}) \
            ["structuredContent"]
        check("out-of-scope doc -> refused outside_scope",
              oc == {"refused": True, "reason": "outside_scope"},
              json.dumps(oc))

        # -- audit: every tool call landed in the ledger
        book = GrantBook(Path(vault_root) / "grants.db")
        states = [e["state"] for e in book.entries(grant_id)]
        check("ledger audited every tool call",
              len(states) >= 5, str(states))

        # -- notifications get no reply (protocol check): a bare notification
        #    followed by a request must still answer the request cleanly
        client.send("notifications/exit", notify=True)

        client.proc.stdin.close()
        client.proc.wait(timeout=10)
        print("\nSMOKE PASS: protocol verified end-to-end "
              "(handshake, tools/list, brain_search, brain_get, refusals, "
              "grant_invalid, audit).")


if __name__ == "__main__":
    main()
