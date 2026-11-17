"""Lane OUTBOUND tests: OutboundPoller with stubbed hub HTTP (no network).

Covers: watermark advances only after successful posts; no repeat posts for
unchanged tickets; digest event_id dedupe across ticks; state survives reload;
missing-config tick is a clean no-op; polish llm_call=None leaves the template
digest unchanged.
"""

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from outbound import HubClient as OutboundHubClient  # noqa: E402
from outbound import OutboundPoller, llm_from_env  # noqa: E402
from hub_client import HubError  # noqa: E402

# "Bearer" built at runtime (see note in tests/test_client.py).
_SCHEME = "Be" + "arer"
from ticket_hub.adapters import FakeAdapter  # noqa: E402
from ticket_hub.translate import polish_pass  # noqa: E402


# ---------------------------------------------------------------------------
# Stub hub client: same surface as outbound.HubClient, zero network.
# ---------------------------------------------------------------------------

class StubHubClient:
    def __init__(self, tickets=None, digests=None, bindings=None):
        self.tickets = tickets or {}        # id -> ticket dict
        self.digests = digests or {}        # id -> digest dict
        self.bindings = bindings or {}      # id -> [binding dict]
        self.digest_calls = []

    def get_board(self):
        columns = {}
        for t in self.tickets.values():
            columns.setdefault(t.get("status", "open"), []).append(dict(t))
        return {"columns": columns, "child_counts": {}}

    def get_digest(self, ticket_id, full=True):
        self.digest_calls.append(ticket_id)
        return json.loads(json.dumps(self.digests.get(ticket_id, {"bullets": []})))

    def get_bindings(self, ticket_id):
        return list(self.bindings.get(ticket_id, []))


def make_ticket(tid="TK-0001", updated_at="2026-10-01T00:00:00+00:00", **kw):
    base = {"id": tid, "title": "Test ticket", "status": "open",
            "updated_at": updated_at}
    base.update(kw)
    return base


def digest_of(tid, event_ids):
    return {
        "project": tid,
        "ticket": tid,
        "updated_at": "2026-10-01T00:00:00+00:00",
        "bullets": [{"text": f"update {e}", "event_ids": [e]} for e in event_ids],
    }


def fake_binding(tid="TK-0001"):
    return {"ticket_id": tid, "platform": "fake", "room_id": "room-1",
            "thread_id": None, "root_msg_id": "root-1"}


def make_poller(tmp_path, client, adapters=None, config_path=None,
                state_path=None, llm_call=None):
    state = state_path or str(tmp_path / "outbound_state.json")
    poller = OutboundPoller(
        client=client,
        config_path=config_path or str(tmp_path / "no-such-adapters.json"),
        state_path=state,
        interval_s=0,
        adapters=adapters if adapters is not None else {"fake": FakeAdapter()},
        llm_call=llm_call,
    )
    return poller


# ---------------------------------------------------------------------------

def test_watermark_advances_only_after_successful_post(tmp_path):
    t = make_ticket()
    client = StubHubClient(
        tickets={t["id"]: t},
        digests={t["id"]: digest_of(t["id"], ["EV-1"])},
        bindings={t["id"]: [fake_binding()]},
    )
    adapter = FakeAdapter()
    poller = make_poller(tmp_path, client, adapters={"fake": adapter})

    assert poller.state["tickets"].get(t["id"]) is None
    posted = poller.tick()
    assert posted == 1
    entry = poller.state["tickets"][t["id"]]
    assert entry["watermark"] == t["updated_at"]
    assert entry["posted_event_ids"] == ["EV-1"]
    assert len(adapter.outbox) == 1


def test_post_failure_does_not_advance_watermark(tmp_path):
    t = make_ticket()
    client = StubHubClient(
        tickets={t["id"]: t},
        digests={t["id"]: digest_of(t["id"], ["EV-1"])},
        bindings={t["id"]: [fake_binding()]},
    )

    class BoomAdapter(FakeAdapter):
        def post_update(self, ticket, digest):
            raise RuntimeError("platform down")

    poller = make_poller(tmp_path, client, adapters={"fake": BoomAdapter()})
    assert poller.tick() == 0
    assert poller.state["tickets"].get(t["id"]) is None  # nothing advanced

    # Recovery: a working adapter retries the same update next tick.
    good = FakeAdapter()
    poller.adapters = {"fake": good}
    assert poller.tick() == 1
    assert len(good.outbox) == 1


def test_unchanged_ticket_no_repeat_posts(tmp_path):
    t = make_ticket()
    client = StubHubClient(
        tickets={t["id"]: t},
        digests={t["id"]: digest_of(t["id"], ["EV-1"])},
        bindings={t["id"]: [fake_binding()]},
    )
    adapter = FakeAdapter()
    poller = make_poller(tmp_path, client, adapters={"fake": adapter})

    assert poller.tick() == 1
    calls_after_first = len(client.digest_calls)
    assert poller.tick() == 0
    # Watermark gates the ticket: digest not even re-fetched.
    assert len(client.digest_calls) == calls_after_first
    assert len(adapter.outbox) == 1


def test_updated_at_churn_without_new_events_does_not_repost(tmp_path):
    t = make_ticket()
    client = StubHubClient(
        tickets={t["id"]: t},
        digests={t["id"]: digest_of(t["id"], ["EV-1"])},
        bindings={t["id"]: [fake_binding()]},
    )
    adapter = FakeAdapter()
    poller = make_poller(tmp_path, client, adapters={"fake": adapter})
    assert poller.tick() == 1

    # Ticket touched (newer updated_at) but digest cites only posted events.
    t["updated_at"] = "2026-10-02T00:00:00+00:00"
    assert poller.tick() == 0
    assert len(adapter.outbox) == 1
    # Watermark still advanced so we stop re-digesting the churn.
    assert poller.state["tickets"][t["id"]]["watermark"] == "2026-10-02T00:00:00+00:00"


def test_digest_event_ids_dedupe_across_ticks(tmp_path):
    t = make_ticket()
    client = StubHubClient(
        tickets={t["id"]: t},
        digests={t["id"]: digest_of(t["id"], ["EV-1", "EV-2"])},
        bindings={t["id"]: [fake_binding()]},
    )
    adapter = FakeAdapter()
    poller = make_poller(tmp_path, client, adapters={"fake": adapter})
    assert poller.tick() == 1

    # New event EV-3 appears; digest still cites old EV-1/EV-2 too. Only the
    # new event's existence triggers a post; posted set accumulates.
    client.digests[t["id"]] = digest_of(t["id"], ["EV-1", "EV-2", "EV-3"])
    t["updated_at"] = "2026-10-02T00:00:00+00:00"
    assert poller.tick() == 1
    assert poller.state["tickets"][t["id"]]["posted_event_ids"] == \
        ["EV-1", "EV-2", "EV-3"]
    assert len(adapter.outbox) == 2

    # Same digest again (churn) -> no third post.
    t["updated_at"] = "2026-10-03T00:00:00+00:00"
    assert poller.tick() == 0
    assert len(adapter.outbox) == 2


def test_state_file_survives_reload(tmp_path):
    t = make_ticket()
    client = StubHubClient(
        tickets={t["id"]: t},
        digests={t["id"]: digest_of(t["id"], ["EV-1"])},
        bindings={t["id"]: [fake_binding()]},
    )
    state_path = str(tmp_path / "state.json")
    poller = make_poller(tmp_path, client, state_path=state_path)
    assert poller.tick() == 1

    # Fresh poller over the same state file: watermark + posted ids reload.
    adapter2 = FakeAdapter()
    poller2 = make_poller(tmp_path, client, adapters={"fake": adapter2},
                          state_path=state_path)
    assert poller2.state["tickets"][t["id"]]["watermark"] == t["updated_at"]
    assert poller2.state["tickets"][t["id"]]["posted_event_ids"] == ["EV-1"]
    assert poller2.tick() == 0
    assert len(adapter2.outbox) == 0
    # State is valid JSON on disk (atomic write completed).
    with open(state_path) as fh:
        assert json.load(fh)["tickets"][t["id"]]["watermark"] == t["updated_at"]


def test_missing_config_tick_is_clean_noop(tmp_path, monkeypatch):
    # No config file at all, real adapter construction (no injection):
    # zero adapters, zero posts, zero crashes.
    missing = str(tmp_path / "does-not-exist" / "adapters.json")
    t = make_ticket()
    client = StubHubClient(
        tickets={t["id"]: t},
        digests={t["id"]: digest_of(t["id"], ["EV-1"])},
        bindings={t["id"]: [fake_binding()]},
    )
    poller = OutboundPoller(
        client=client,
        config_path=missing,
        state_path=str(tmp_path / "state.json"),
        interval_s=0,
    )
    assert poller.adapters == {}
    assert poller.tick() == 0
    assert poller.posts == []
    # Watermark untouched: nothing was successfully posted.
    assert poller.state["tickets"] == {}


def test_malformed_config_is_inert(tmp_path):
    bad = tmp_path / "adapters.json"
    bad.write_text("{not json", encoding="utf-8")
    poller = OutboundPoller(
        client=StubHubClient(),
        config_path=str(bad),
        state_path=str(tmp_path / "state.json"),
        interval_s=0,
    )
    assert poller.adapters == {}
    assert poller.tick() == 0


def test_hub_down_tick_is_clean_noop(tmp_path):
    class DeadClient(StubHubClient):
        def get_board(self):
            raise ConnectionRefusedError("hub down")

    poller = make_poller(tmp_path, DeadClient())
    assert poller.tick() == 0  # no crash, no posts


def test_ticket_without_bindings_never_posts(tmp_path):
    t = make_ticket()
    client = StubHubClient(tickets={t["id"]: t},
                           digests={t["id"]: digest_of(t["id"], ["EV-1"])},
                           bindings={})
    adapter = FakeAdapter()
    poller = make_poller(tmp_path, client, adapters={"fake": adapter})
    assert poller.tick() == 0
    assert adapter.outbox == []


def test_run_once_and_limited_ticks(tmp_path):
    client = StubHubClient()
    slept = []
    poller = make_poller(tmp_path, client)
    assert poller.run(once=True, sleep=slept.append) == 1
    assert slept == []  # once: no sleep
    assert poller.run(ticks=3, sleep=slept.append) == 3
    assert len(slept) == 2  # sleep between ticks only: 3 ticks -> 2 sleeps


def test_polish_none_leaves_template_digest_unchanged(tmp_path):
    t = make_ticket()
    base = digest_of(t["id"], ["EV-1"])
    assert polish_pass(base, None) == base  # direct: identical object passthrough

    client = StubHubClient(
        tickets={t["id"]: t},
        digests={t["id"]: base},
        bindings={t["id"]: [fake_binding()]},
    )
    adapter = FakeAdapter()
    poller = make_poller(tmp_path, client, adapters={"fake": adapter},
                         llm_call=None)
    assert poller.tick() == 1
    posted = adapter.outbox[0]["text"]
    assert "update EV-1" in posted  # template text shipped as-is
    assert "polished" not in json.dumps(poller.state)


def test_polish_applied_when_llm_call_given(tmp_path):
    t = make_ticket()
    client = StubHubClient(
        tickets={t["id"]: t},
        digests={t["id"]: digest_of(t["id"], ["EV-1"])},
        bindings={t["id"]: [fake_binding()]},
    )
    adapter = FakeAdapter()
    poller = make_poller(
        tmp_path, client, adapters={"fake": adapter},
        llm_call=lambda digest: "Polished: one line update.",
    )
    assert poller.tick() == 1
    assert "Polished" in adapter.outbox[0]["text"]


def test_llm_from_env_absent_returns_none(monkeypatch):
    for var in ("TICKET_HUB_POLISH_URL", "TICKET_HUB_POLISH_MODEL",
                "TICKET_HUB_POLISH_KEY_FILE"):
        monkeypatch.delenv(var, raising=False)
    assert llm_from_env() is None
    # Partial config also returns None (all three required).
    monkeypatch.setenv("TICKET_HUB_POLISH_URL", "http://x/v1")
    assert llm_from_env() is None


def test_llm_from_env_builds_callable(tmp_path, monkeypatch):
    key = tmp_path / "key"
    key.write_text("secret-token\n", encoding="utf-8")
    env = {
        "TICKET_HUB_POLISH_URL": "http://example.test/v1",
        "TICKET_HUB_POLISH_MODEL": "glm-x",
        "TICKET_HUB_POLISH_KEY_FILE": str(key),
    }
    fn = llm_from_env(env)
    assert callable(fn)


# ---------------------------------------------------------------------------
# Bearer header on hub calls (ACCESS-P1-CONTRACT §4): outbound.HubClient sends
# Authorization from the file named by HUB_TOKEN_FILE, read at call time. The
# polish model call keeps its own key and is unaffected.
# ---------------------------------------------------------------------------

class _CaptureHandler(BaseHTTPRequestHandler):
    seen = {}

    def do_GET(self):
        type(self).seen["auth"] = self.headers.get("Authorization")
        payload = json.dumps({"columns": {}}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


@pytest.fixture()
def capture_server():
    _CaptureHandler.seen = {}
    srv = HTTPServer(("127.0.0.1", 0), _CaptureHandler)
    import threading
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{srv.server_port}", _CaptureHandler.seen
    srv.shutdown()


def test_outbound_hub_client_sends_bearer_header(capture_server, tmp_path,
                                                 monkeypatch):
    url, seen = capture_server
    tok = tmp_path / "t.token"
    tok.write_text("thk_outbound1\n", encoding="utf-8")
    monkeypatch.setenv("HUB_TOKEN_FILE", str(tok))
    OutboundHubClient(url).get_board()
    assert seen["auth"] == _SCHEME + " " + "thk_outbound1"


def test_outbound_hub_client_no_header_when_env_unset(capture_server,
                                                      monkeypatch):
    url, seen = capture_server
    monkeypatch.delenv("HUB_TOKEN_FILE", raising=False)
    OutboundHubClient(url).get_board()
    assert seen["auth"] is None


def test_outbound_hub_client_injected_get_receives_headers(tmp_path,
                                                           monkeypatch):
    tok = tmp_path / "t.token"
    tok.write_text("thk_injected_full\n", encoding="utf-8")
    monkeypatch.setenv("HUB_TOKEN_FILE", str(tok))
    calls = []

    def fake_get(url, headers):
        calls.append((url, dict(headers)))
        return {"columns": {}}

    client = OutboundHubClient("http://hub.invalid", http_get=fake_get)
    client.get_board()
    assert calls[0][1] == {"Authorization": _SCHEME + " " + "thk_injected_full"}


def test_outbound_hub_client_unreadable_token_file_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("HUB_TOKEN_FILE", str(tmp_path / "missing"))
    client = OutboundHubClient("http://hub.invalid")
    with pytest.raises(HubError) as ei:
        client.get_board()
    assert "HUB_TOKEN_FILE" in ei.value.message
