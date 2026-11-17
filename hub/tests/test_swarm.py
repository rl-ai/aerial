"""Swarm harness tests: CAS claim race, locked-DB retry, inert mode,
watermark anti-repost — all against stub hubs. Plus packet builder and CLI
verb against the real app (wiki-absent graceful). No real subprocess, no
network beyond the in-process test hub."""
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)

import pytest

from hub_client import HubClient, HubError
from swarm import SwarmHarness, render_status


# ---------------------------------------------------------------------------
# Stub hub: in-memory HubClient surface (list / lineage / event / packet)
# ---------------------------------------------------------------------------

class StubHub:
    """Enough of the hub for the harness: append-only event log, list by
    status+tag, lineage carrying events. `interceptor` runs after each event
    append (used to simulate a rival claim landing mid-race); `lock_failures`
    makes the first N appends raise 'database is locked'."""

    def __init__(self, tickets=None, interceptor=None, lock_failures=0):
        self.tickets = tickets or []
        self.events = []
        self._seq = 0
        self.interceptor = interceptor
        self.lock_failures = lock_failures
        self.append_calls = 0

    def list(self, status=None, tag=None, **kw):
        out = []
        for t in self.tickets:
            if status and t["status"] != status:
                continue
            if tag and tag not in t.get("tags", []):
                continue
            out.append(dict(t))
        return out

    def event(self, ticket_id, kind, payload=None, actor=None):
        self.append_calls += 1
        if self.append_calls <= self.lock_failures:
            raise HubError("http_error",
                           "HTTP 500: database is locked")
        self._seq += 1
        ev = {
            "id": f"EV-{self._seq:04d}",
            "ticket_id": ticket_id,
            "ts": f"2026-10-12T00:00:{self._seq:02d}",
            "kind": kind,
            "actor": {"name": actor or "unknown", "kind": "agent"},
            "payload": dict(payload or {}),
        }
        self.events.append(ev)
        if self.interceptor is not None:
            self.interceptor(self, ev)
        return dict(ev)

    def lineage(self, ticket_id):
        return {
            "ticket": next(t for t in self.tickets if t["id"] == ticket_id),
            "ancestry": [],
            "events": [dict(e) for e in self.events
                       if e["ticket_id"] == ticket_id],
        }

    def packet(self, ticket_id):
        return {"ticket": next(t for t in self.tickets
                                if t["id"] == ticket_id),
                "ancestors": [], "plan": None, "blocked": [],
                "events": [], "threads": [], "wiki_pages": []}


def _ticket(tid="TK-1", status="running", tag="swarm-build"):
    return {"id": tid, "title": f"work {tid}", "body": "do it",
            "status": status, "tags": [tag]}


def _worker(name="w1", role="execution", tag="swarm-build",
            seat_cmd="echo hi"):
    return {"name": name, "role": role, "tag": tag,
            "seat_cmd": seat_cmd, "interval_s": 60, "max_parallel": 1}


def _harness(hub, worker, tmp_path, name=None, seat_runner=None, **kw):
    logs = []
    return SwarmHarness(
        hub,
        config={"interval_s": 60,
                "workers": [worker if name is None else
                            dict(worker, name=name)]},
        state_path=str(tmp_path / f"state-{name or worker['name']}.json"),
        seat_runner=seat_runner or (lambda cmd, packet: "RESULT: all done"),
        sleep=lambda s: None,
        log=logs.append,
        **kw), logs


# ---------------------------------------------------------------------------
# CAS claim race: N fake workers, one ticket — exactly one proceeds
# ---------------------------------------------------------------------------

def test_claim_cas_exactly_one_winner(tmp_path):
    """N fake workers, one ticket, one shared watermark. When 'loser'
    appends its claim, the interceptor injects a concurrent rival that
    claims AND completes right after (its claim is no longer ours to win).
    loser's CAS re-read must fail and its seat must never run; the ticket
    then flows to the next worker whose claim wins CAS — exactly one seat
    run and exactly one result comment from our workers, ever."""
    def rival_injection(hub, ev):
        payload = ev.get("payload") or {}
        if payload.get("worker") == "loser":
            # A concurrent rival claims after ours and finishes the work:
            # we lost the race; the ticket is (re-)triggered for a fresh
            # claim by the completion landing after the rival's claim.
            hub.event(ev["ticket_id"], "checkpoint",
                      {"claim": "execution", "worker": "ghost-rival"},
                      actor="swarm:ghost")
            hub.event(ev["ticket_id"], "comment",
                      {"text": "ghost did it first"},
                      actor="role:execution")

    hub = StubHub(tickets=[_ticket()], interceptor=rival_injection)
    seat_runs = []

    def counting_seat(cmd, packet):
        seat_runs.append(cmd)
        return "built it"

    logs = []
    h = SwarmHarness(
        hub,
        config={"interval_s": 60, "workers": [
            _worker(name="loser"), _worker(name="worker-b"),
            _worker(name="worker-c")]},
        state_path=str(tmp_path / "state.json"),
        seat_runner=counting_seat,
        sleep=lambda s: None,
        log=logs.append)
    posted = h.tick()
    assert posted == 1, "exactly one worker may proceed on one ticket"
    assert len(seat_runs) == 1
    ours = [e for e in hub.events if e["kind"] == "comment"
            and (e.get("payload") or {}).get("text") == "built it"]
    assert len(ours) == 1
    assert ours[0]["actor"]["name"] == "role:execution"
    # loser lost the CAS race and said so; its seat never ran.
    assert any("lost claim race" in m for m in logs)
    # Second tick: watermark + completion gate keep every worker quiet.
    assert h.tick() == 0
    assert len(seat_runs) == 1


def test_claim_written_as_checkpoint_with_claim_payload(tmp_path):
    hub = StubHub(tickets=[_ticket()])
    h, logs = _harness(hub, _worker(), tmp_path)
    assert h.tick() == 1
    claim = hub.events[0]
    assert claim["kind"] == "checkpoint"
    assert claim["payload"]["claim"] == "execution"
    assert claim["payload"]["worker"] == "w1"
    assert "ts" in claim["payload"]


def test_watermark_never_reposts_same_claim(tmp_path):
    hub = StubHub(tickets=[_ticket()])
    h, logs = _harness(hub, _worker(), tmp_path)
    assert h.tick() == 1
    # Second tick: own completion is remembered -> no re-claim, no re-post.
    assert h.tick() == 0
    comments = [e for e in hub.events if e["kind"] == "comment"]
    assert len(comments) == 1


def test_state_file_written_and_status_renders(tmp_path):
    hub = StubHub(tickets=[_ticket()])
    h, logs = _harness(hub, _worker(), tmp_path)
    h.tick()
    with open(h.state_path, "r", encoding="utf-8") as fh:
        state = json.load(fh)
    assert len(state["claims"]) == 1
    entry = next(iter(state["claims"].values()))
    assert entry["posted"] is True and entry["ticket"] == "TK-1"
    out = render_status(state)
    assert "TK-1" in out and "execution" in out and "yes" in out
    assert "no claims" in render_status({"claims": {}})


# ---------------------------------------------------------------------------
# Locked-DB retry
# ---------------------------------------------------------------------------

def test_locked_db_retry_then_success(tmp_path):
    """First two appends hit 'database is locked'; the bounded retry must
    recover and complete the claim + post."""
    hub = StubHub(tickets=[_ticket()], lock_failures=2)
    sleeps = []
    h, logs = _harness(hub, _worker(), tmp_path)
    h.sleep = sleeps.append
    assert h.tick() == 1
    assert len(sleeps) >= 1
    comments = [e for e in hub.events if e["kind"] == "comment"]
    assert len(comments) == 1


def test_locked_db_exhausted_records_error_never_touches_status(tmp_path):
    hub = StubHub(tickets=[_ticket()], lock_failures=10)
    h, logs = _harness(hub, _worker(), tmp_path)
    assert h.tick() == 0
    # Status must be untouched: the stub only changes status via patch(),
    # which the harness never calls; and no seat output comment exists.
    assert not [e for e in hub.events if e["kind"] == "comment"]
    assert hub.tickets[0]["status"] == "running"


def test_seat_failure_posts_reason_checkpoint_not_status_change(tmp_path):
    def boom(cmd, packet):
        raise RuntimeError("seat exploded")
    hub = StubHub(tickets=[_ticket()])
    h, logs = _harness(hub, _worker(), tmp_path, seat_runner=boom)
    assert h.tick() == 0
    err = [e for e in hub.events
           if e["kind"] == "checkpoint" and "swarm_error" in e["payload"]]
    assert len(err) == 1
    assert "seat exploded" in err[0]["payload"]["reason"]
    assert hub.tickets[0]["status"] == "running"


def test_seat_cmd_model_placeholder_injected(tmp_path):
    seen = {}

    def spy(cmd, packet):
        seen["cmd"] = cmd
        seen["stdin"] = packet
        return "ok"
    hub = StubHub(tickets=[_ticket()])
    w = dict(_worker(seat_cmd="chat --model {model}"), model="fast-mid")
    h, logs = _harness(hub, w, tmp_path, seat_runner=spy)
    h.tick()
    assert "{model}" not in seen["cmd"]
    assert "fast-mid" in seen["cmd"]
    assert seen["stdin"]["ticket"]["id"] == "TK-1"


def test_ineligible_when_claim_in_flight(tmp_path):
    """A live claim (no completion) means work in flight: skip."""
    hub = StubHub(tickets=[_ticket()])
    hub.event("TK-1", "checkpoint",
              {"claim": "execution", "worker": "someone-else"},
              actor="swarm:other")
    h, logs = _harness(hub, _worker(), tmp_path)
    assert h.tick() == 0
    assert not any((e.get("payload") or {}).get("worker") == "w1"
                   for e in hub.events)


# ---------------------------------------------------------------------------
# Inert mode
# ---------------------------------------------------------------------------

class ExplodingHub:
    def list(self, *a, **kw):
        raise AssertionError("inert harness must not touch the hub")

    def event(self, *a, **kw):
        raise AssertionError("inert harness must not touch the hub")


def test_inert_without_config(tmp_path):
    logs = []
    h = SwarmHarness(ExplodingHub(), config_path=str(tmp_path / "nope.json"),
                     state_path=str(tmp_path / "s.json"),
                     log=logs.append)
    assert h.inert is True
    assert h.tick() == 0
    assert any("inert" in m for m in logs)


def test_inert_empty_workers_config(tmp_path):
    h = SwarmHarness(ExplodingHub(), config={"workers": []},
                     state_path=str(tmp_path / "s.json"),
                     log=lambda m: None)
    assert h.inert is True
    assert h.tick() == 0


def test_inert_empty_seat_cmd_claims_nothing(tmp_path):
    hub = StubHub(tickets=[_ticket()])
    logs = []
    h = SwarmHarness(
        hub,
        config={"workers": [_worker(seat_cmd="")]},
        state_path=str(tmp_path / "s.json"),
        seat_runner=lambda c, p: "should never run",
        sleep=lambda s: None,
        log=logs.append)
    assert h.tick() == 0
    assert hub.events == []          # nothing claimed
    assert hub.append_calls == 0     # nothing appended
    assert any("inert" in m for m in logs)


# ---------------------------------------------------------------------------
# Packet: real app, wiki absent -> graceful; CLI verb renders sections
# ---------------------------------------------------------------------------

@pytest.fixture()
def hub(hub_base_url):
    return HubClient(base_url=hub_base_url)


@pytest.fixture()
def cli(hub_base_url):
    from hub_cli import main as cli_main

    def run(*argv):
        import contextlib
        import io
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli_main(list(argv),
                            client=HubClient(base_url=hub_base_url))
        return code, out.getvalue(), err.getvalue()
    return run


def _running_ticket(hub, title="Packet target"):
    parent = hub.create_ticket("Packet parent")
    t = hub.create_ticket(title, body="the body of work",
                          parent=parent["id"], tags=["packetq"],
                          owner="owner")
    hub.patch(t["id"], status="open")
    hub.patch(t["id"], status="running")
    hub.comment(t["id"], "first note")
    return parent, t


def test_packet_sections_with_wiki_absent(hub):
    parent, t = _running_ticket(hub)
    hub.bind(t["id"], "matrix", "room-9", thread_id="th-9")
    pkt = hub.packet(t["id"])
    assert pkt["ticket"]["id"] == t["id"]
    assert [a["id"] for a in pkt["ancestors"]] == [parent["id"]]
    assert pkt["plan"] is not None and "progress" in pkt["plan"]
    assert isinstance(pkt["blocked"], list)
    kinds = [e["kind"] for e in pkt["events"]]
    assert "comment" in kinds and len(pkt["events"]) <= 15
    assert any(th.get("room_id") == "room-9" for th in pkt["threads"])
    # Wiki lane is not in this worktree: graceful empty, no raise.
    assert pkt["wiki_pages"] == []


def test_packet_events_capped_at_15(hub):
    parent, t = _running_ticket(hub)
    for i in range(20):
        hub.event(t["id"], "checkpoint", {"n": i})
    pkt = hub.packet(t["id"])
    assert len(pkt["events"]) == 15


def test_cli_packet_text_and_json(cli, hub_base_url):
    hub = HubClient(base_url=hub_base_url)
    parent, t = _running_ticket(hub, title="CLI packet render")
    code, out, err = cli("packet", t["id"])
    assert code == 0, err
    assert "PACKET" in out and t["id"] in out
    for section in ("ancestors", "plan", "events", "threads",
                    "related wiki pages"):
        assert section in out
    code, out, err = cli("packet", t["id"], "--json")
    assert code == 0, err
    pkt = json.loads(out)
    assert pkt["ticket"]["id"] == t["id"]
    assert pkt["wiki_pages"] == []
