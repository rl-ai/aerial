"""Hermetic tests for the Aerial Gauntlet: ticket structure + layer sequencing.

No hub, no network, no real layers: the hub API and the layer runners are
stubbed; we verify the run-ticket/child-ticket structure and that every
layer runs sequentially and reports fail-closed, with SEV-only escalation.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dashboard import gauntlet as g  # noqa: E402


class FakeHub:
    """In-memory hub: records ticket creations and comments."""

    def __init__(self):
        self.tickets = []
        self.comments = []
        self._n = 0

    def api(self, path, method="GET", body=None):
        if method == "POST" and path == "/tickets":
            self._n += 1
            tid = f"TK-FAKE{self._n:04d}"
            self.tickets.append({"id": tid, **(body or {})})
            return {"id": tid}
        if method == "POST" and path.endswith("/events"):
            self.comments.append((path.split("/")[2], body["payload"]["body"]))
            return {"ok": True}
        raise AssertionError(f"unexpected call {method} {path}")


@pytest.fixture
def fake_hub(monkeypatch):
    hub = FakeHub()
    monkeypatch.setattr(g, "_hub", hub.api)
    monkeypatch.setattr(g.jt, "_api", hub.api)
    return hub


def test_ticket_structure_parent_plus_layer_children(fake_hub):
    t = g.create_run_tickets("2026-11-14")
    assert len(fake_hub.tickets) == 9
    parent = fake_hub.tickets[0]
    assert parent["title"] == "Gauntlet run 2026-11-14"
    assert "gauntlet" in parent["tags"]
    assert parent["parent_id"] == g.EPIC
    children = fake_hub.tickets[1:]
    assert [c["title"] for c in children] == \
        [g.LAYER_NAMES[n] for n in (1, 2, 3, 4, 5, 6, 7, 8)]
    # each child is parented to the run ticket and carries its scope
    for c in children:
        assert c["parent_id"] == t["parent"]
        assert "Scope:" in c["body"]
        assert "gauntlet-layer" in c["tags"]


def test_all_layers_run_even_after_failure_sequenced(monkeypatch, fake_hub):
    calls = []

    def mk(n, ok):
        def f(*a, **k):
            calls.append(n)
            return g.LayerResult(n, ok=ok, lines=[f"{n}: {'pass' if ok else 'boom'}"])
        return f

    monkeypatch.setattr(g, "layer1_units", mk(1, True))
    monkeypatch.setattr(g, "layer2_stress", mk(2, False))
    monkeypatch.setattr(g, "layer3_backtest", mk(3, False))
    monkeypatch.setattr(g, "layer4_ditl", mk(4, True))
    monkeypatch.setattr(g, "layer5_smoke", mk(5, True))
    monkeypatch.setattr(g, "layer6_soak", mk(6, True))
    monkeypatch.setattr(g, "layer7_fresh", mk(7, True))
    monkeypatch.setattr(g, "layer8_reboot", mk(8, True))
    monkeypatch.setattr(g, "escalate", lambda r: None)
    out = g.run_gauntlet(workdir="/tmp/gauntlet-test", post_tickets=True)
    # sequential order, all layers ran despite failures at 2 and 3
    assert calls == [1, 2, 3, 4, 5, 6, 7, 8]
    assert out["verdict"] == "FAIL"
    assert {n: r["ok"] for n, r in out["layers"].items()} == \
        {1: True, 2: False, 3: False, 4: True, 5: True, 6: True, 7: True,
         8: True}
    # fail-closed reporting: every layer's result posted to its child ticket
    layer_ids = {t["title"]: t["id"] for t in fake_hub.tickets[1:]}
    commented = {tid for tid, _ in fake_hub.comments}
    assert commented == set(layer_ids.values()) | {out["tickets"]["parent"]}
    assert sum(1 for tid, b in fake_hub.comments if tid == out["tickets"]["parent"]) == 1
    assert "Gauntlet run" in fake_hub.comments[-1][1]
    assert "FAIL" in fake_hub.comments[-1][1]


def test_crashed_layer_reports_fail_not_raise(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("harness exploded")

    monkeypatch.setattr(g, "layer1_units", boom)
    monkeypatch.setattr(g, "escalate", lambda r: None)
    out = g.run_gauntlet(workdir="/tmp/gauntlet-test", layers=(1,),
                         post_tickets=False)
    assert out["verdict"] == "FAIL"
    assert "RuntimeError" in out["layers"][1]["lines"][0]


def test_verdict_pass_requires_all_layers(monkeypatch):
    for ok, expected in ((True, "PASS"), (False, "FAIL")):
        monkeypatch.setattr(g, "layer1_units",
                            lambda *a, _ok=ok: g.LayerResult(1, ok=_ok))
        monkeypatch.setattr(g, "escalate", lambda r: None)
        out = g.run_gauntlet(workdir="/tmp/gauntlet-test", layers=(1,),
                             post_tickets=False)
        assert out["verdict"] == expected


def test_escalation_sev_only_immediate(monkeypatch):
    import dashboard.email_delivery as email_delivery
    pings = []
    monkeypatch.setattr(email_delivery, "send_report",
                        lambda *a, **k: pings.append(a) or True)

    sev = g.LayerResult(5, ok=False, lines=["- console: platform fully dead"])
    assert g.escalate(sev) in (g.SEV_INCIDENT, g.SEV_SECURITY)
    assert len(pings) == 1  # immediate ping went out

    mundane = g.LayerResult(1, ok=False, lines=["- dashboard suite: 2 failed"])
    cls = g.escalate(mundane)
    assert cls not in (g.SEV_INCIDENT, g.SEV_SECURITY)
    assert len(pings) == 1  # no ping: aggregates to the daily report

    assert g.escalate(g.LayerResult(1, ok=True)) is None
    assert len(pings) == 1


def test_layer_result_body_and_stress_reference():
    r = g.LayerResult(2, ok=False, lines=["- cause_named: 0.5 vs reference 1.00 → FAIL"])
    assert r.body().startswith("FAIL — Layer 2 Stress")
    assert g.STRESS_REFERENCE == {"cause_named": 1.0, "honesty": 0.96}
