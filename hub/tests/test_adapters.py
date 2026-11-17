"""Tests for ticket_hub.adapters (Lane C).

No network: everything either runs in inert mode or uses an injected
http_call recorder (monkeypatch-style; urllib is never touched).
"""

import json
import os

import pytest

from ticket_hub.adapters import (
    DiscordAdapter,
    FakeAdapter,
    MatrixAdapter,
    SlackAdapter,
)

PLATFORMS = [MatrixAdapter, DiscordAdapter, SlackAdapter]


def digest(*texts):
    return {"project": "p", "updated_at": "t",
            "bullets": [{"text": t, "event_ids": ["e1"]} for t in texts]}


# ------------------------------------------------------------ FakeAdapter

def test_fake_posts_record_markdown_bullets():
    a = FakeAdapter()
    msg_id = a.post_update({"id": "TK-1"}, digest("Advanced to Review.",
                                                  "Produced report.pdf."))
    assert msg_id == "fake-msg-1"
    assert len(a.outbox) == 1
    text = a.outbox[0]["text"]
    assert text == "- Advanced to Review.\n- Produced report.pdf."


def test_fake_inbound_roundtrip_resolves_actor():
    a = FakeAdapter(config={"fake": {"user_map": {"U1": "Owner"}}})
    a.register_mapping("TK-1", room_id="room-a", thread_id="th-1")
    event = a.on_inbound({"user_id": "U1", "text": "looks good",
                          "msg_id": "m9", "thread_id": "th-1",
                          "room_id": "room-a", "ts": "2026-10-01T00:00:00Z",
                          "ticket_id": "TK-1"})
    assert event["kind"] == "thread_message"
    assert event["ticket_id"] == "TK-1"
    assert event["actor"]["name"] == "Owner"
    assert event["payload"]["body"] == "looks good"
    assert event["payload"]["msg_id"] == "m9"


def test_fake_register_mapping_returns_thread_binding():
    a = FakeAdapter()
    b = a.register_mapping("TK-2", room_id="r", thread_id="t",
                           root_msg_id="m1", parent_ticket="TK-1")
    assert b["platform"] == "fake"
    assert a.binding_for("TK-2") == b


def test_child_ticket_posts_reply_under_parent_root():
    a = FakeAdapter()
    a.register_mapping("TK-1", room_id="r", root_msg_id="root-1")
    a.register_mapping("TK-2", room_id="r", parent_ticket="TK-1")
    a.post_update({"id": "TK-2"}, digest("Advanced to Review."))
    assert a.outbox[0]["reply_to"] == "root-1"


def test_parent_ticket_posts_no_reply_to():
    a = FakeAdapter()
    a.register_mapping("TK-1", room_id="r", root_msg_id="root-1")
    a.post_update({"id": "TK-1"}, digest("Created."))
    assert a.outbox[0]["reply_to"] is None


# ------------------------------------------------ inert-mode construction

@pytest.mark.parametrize("cls", PLATFORMS)
def test_inert_when_config_file_missing(cls, tmp_path):
    missing = tmp_path / "nope" / "adapters.json"
    assert not missing.exists()
    a = cls(config_path=str(missing))
    assert a.inert is True


@pytest.mark.parametrize("cls", PLATFORMS)
def test_inert_when_platform_section_or_token_absent(cls, tmp_path):
    other = tmp_path / "other.json"
    other.write_text(json.dumps({"nonsense": {"token": "x"}}))
    assert cls(config_path=str(other)).inert is True

    no_token = tmp_path / "no-token.json"
    no_token.write_text(json.dumps({cls.platform: {}}))
    assert cls(config_path=str(no_token)).inert is True


@pytest.mark.parametrize("cls", PLATFORMS)
def test_inert_posts_and_inbound_need_no_credentials(cls, tmp_path):
    a = cls(config_path=str(tmp_path / "absent.json"))
    a.register_mapping("TK-1", room_id="room", thread_id="th",
                       root_msg_id="root")
    msg_id = a.post_update({"id": "TK-1"}, digest("Advanced to Review."))
    assert msg_id.startswith("inert-")
    assert a.outbox[-1]["inert"] is True
    assert "- Advanced to Review." in a.outbox[-1]["text"]
    # inbound mapping works credential-free
    ev = a.on_inbound(inbound_fixture(cls, "hello"))
    assert ev["kind"] == "thread_message"
    assert ev["payload"]["body"] == "hello"


def inbound_fixture(cls, text):
    if cls is MatrixAdapter:
        return {"event_id": "$m", "room_id": "room",
                "sender": "@u:srv", "origin_server_ts": 1,
                "content": {"body": text,
                            "m.relates_to": {"rel_type": "m.thread",
                                             "event_id": "root"}}}
    if cls is DiscordAdapter:
        return {"id": "m", "channel_id": "th", "author": {"id": "1"},
                "content": text, "timestamp": "t"}
    if cls is SlackAdapter:
        return {"type": "message", "channel": "room", "user": "U1",
                "text": text, "ts": "1.1", "thread_ts": "th"}
    raise AssertionError


# ------------------------------------------------- live-mode HTTP (no net)

class Recorder:
    def __init__(self, responses=None):
        self.calls = []
        self.responses = responses or {}

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, body))
        return self.responses.get(len(self.calls), {"ok": True})


def test_matrix_live_threads_reply_to_root_event():
    rec = Recorder({1: {"event_id": "$evt1"}})
    a = MatrixAdapter(config={"matrix": {"token": "tok",
                                         "homeserver": "https://m.example"}},
                      http_call=rec)
    assert a.inert is False
    a.register_mapping("TK-1", room_id="!room", root_msg_id="$root")
    assert a.post_update({"id": "TK-1"}, digest("Advanced to Review.")) == "$evt1"
    method, url, headers, body = rec.calls[0]
    assert method == "PUT" and "/rooms/!room/send/m.room.message/" in url
    assert headers["Authorization"] == "Bearer tok"
    assert body["m.relates_to"]["event_id"] == "$root"
    assert body["m.relates_to"]["rel_type"] == "m.thread"


def test_matrix_child_replies_under_parent_root():
    rec = Recorder({1: {"event_id": "$c1"}})
    a = MatrixAdapter(config={"matrix": {"token": "t",
                                         "homeserver": "https://m"}},
                      http_call=rec)
    a.register_mapping("TK-1", room_id="!room", root_msg_id="$root")
    a.register_mapping("TK-2", room_id="!room", parent_ticket="TK-1")
    a.post_update({"id": "TK-2", "parent": "TK-1"}, digest("Done."))
    assert rec.calls[0][3]["m.relates_to"]["event_id"] == "$root"


def test_slack_live_uses_thread_ts():
    rec = Recorder({1: {"ok": True, "ts": "17.2"}})
    a = SlackAdapter(config={"slack": {"token": "xoxb-t"}}, http_call=rec)
    a.register_mapping("TK-1", room_id="#chan", thread_id="17.0")
    assert a.post_update({"id": "TK-1"}, digest("Created.")) == "17.2"
    _, url, headers, body = rec.calls[0]
    assert url.endswith("/chat.postMessage")
    assert body["thread_ts"] == "17.0" and body["channel"] == "#chan"
    assert headers["Authorization"].startswith("Bearer ")


def test_slack_child_replies_under_parent_thread_ts():
    rec = Recorder({1: {"ok": True, "ts": "18.9"}})
    a = SlackAdapter(config={"slack": {"token": "xoxb-t"}}, http_call=rec)
    a.register_mapping("TK-1", room_id="#chan", thread_id="17.0")
    a.register_mapping("TK-2", room_id="#chan", parent_ticket="TK-1")
    a.post_update({"id": "TK-2"}, digest("Done."))
    assert rec.calls[0][3]["thread_ts"] == "17.0"


def test_discord_child_creates_subthread_under_parent_root():
    rec = Recorder({1: {"id": "thr-9"}, 2: {"id": "msg-5"}})
    a = DiscordAdapter(config={"discord": {"token": "dtok"}}, http_call=rec)
    a.register_mapping("TK-1", room_id="chan-1", root_msg_id="msg-root")
    a.register_mapping("TK-2", room_id="chan-1", parent_ticket="TK-1")
    assert a.post_update({"id": "TK-2"}, digest("Blocked.")) == "msg-5"
    # call 1: create thread from parent's root message; call 2: post reply
    assert "/channels/chan-1/messages/msg-root/threads" in rec.calls[0][1]
    assert rec.calls[1][1].endswith("/channels/thr-9/messages")
    assert rec.calls[1][2]["Authorization"] == "Bot dtok"
    assert rec.calls[1][3]["message_reference"]["message_id"] == "msg-root"


# ------------------------------------------------- inbound platform mapping

def test_matrix_inbound_maps_thread_root_to_ticket():
    a = MatrixAdapter(config={"matrix": {"token": "t", "homeserver": "h",
                                         "user_map": {"@owner:srv": "Owner"}}})
    a.register_mapping("TK-1", room_id="!room", root_msg_id="$root")
    ev = a.on_inbound({"event_id": "$m1", "room_id": "!room",
                       "sender": "@owner:srv", "origin_server_ts": 42,
                       "content": {"body": "yes",
                                   "m.relates_to": {"rel_type": "m.thread",
                                                    "event_id": "$root"}}})
    assert ev["ticket_id"] == "TK-1"
    assert ev["actor"]["name"] == "Owner"
    assert ev["payload"]["thread_id"] == "$root"


def test_discord_inbound_thread_channel_maps_to_ticket():
    a = DiscordAdapter(config={"discord": {"token": "t",
                                           "user_map": {"777": "Owner"}}})
    a.register_mapping("TK-1", room_id="chan-1", thread_id="thr-1")
    ev = a.on_inbound({"id": "m1", "channel_id": "thr-1",
                       "author": {"id": "777"}, "content": "lgtm",
                       "timestamp": "t"})
    assert ev["ticket_id"] == "TK-1"
    assert ev["actor"]["name"] == "Owner"
    assert ev["payload"]["body"] == "lgtm"


def test_slack_inbound_thread_ts_maps_to_ticket():
    a = SlackAdapter(config={"slack": {"token": "t",
                                       "user_map": {"U9": "Owner"}}})
    a.register_mapping("TK-1", room_id="#chan", thread_id="17.0")
    ev = a.on_inbound({"type": "message", "channel": "#chan", "user": "U9",
                       "text": "approved", "ts": "18.1", "thread_ts": "17.0"})
    assert ev["ticket_id"] == "TK-1"
    assert ev["actor"]["name"] == "Owner"
    assert ev["payload"]["thread_id"] == "17.0"


def test_unknown_chat_user_falls_back_to_raw_id():
    a = SlackAdapter(config={"slack": {"token": "t"}})
    ev = a.on_inbound({"channel": "#c", "user": "U404", "text": "hi",
                       "ts": "1"})
    assert ev["actor"]["name"] == "U404"


# --------------------------------------------------- security / repo hygiene

def test_default_config_path_is_runtime_home_not_repo():
    from ticket_hub.adapters.base import DEFAULT_CONFIG_PATH
    from ticket_hub.paths import hub_path
    assert DEFAULT_CONFIG_PATH == hub_path("adapters.json")
    assert "hub-adapters" not in DEFAULT_CONFIG_PATH


def test_repo_sources_contain_no_token_literals():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    adapters_dir = os.path.join(root, "ticket_hub")
    for dirpath, _dirs, files in os.walk(adapters_dir):
        for fn in files:
            if fn.endswith(".py"):
                src = open(os.path.join(dirpath, fn), encoding="utf-8").read()
                # xoxb-t etc. appear only in tests, never in package sources
                for marker in ("xoxb-1", "Nt-", "m.toke"):
                    assert marker not in src, f"{fn} leaks {marker!r}"
