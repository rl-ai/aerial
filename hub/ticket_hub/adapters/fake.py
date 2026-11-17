"""FakeAdapter: in-memory adapter for tests (DESIGN.md §Chat bridges).

No config, no network, ever. Records posts; round-trips inbound events.
"""

from __future__ import annotations

from .base import ChatAdapter

__all__ = ["FakeAdapter"]


class FakeAdapter(ChatAdapter):
    platform = "fake"

    def __init__(self, config_path=None, config=None, http_call=None):
        # FakeAdapter is never credential-driven; ignore any real config path.
        super().__init__(config_path=None, config=config, http_call=http_call)
        self.inert = False

    def _send(self, text, binding, reply_to):
        self._counter += 1
        return f"fake-msg-{self._counter}"

    def _parse_inbound(self, raw):
        chat_user = raw.get("user_id", "unknown")
        return {
            "ticket_id": raw.get("ticket_id"),
            "ts": raw.get("ts"),
            "actor": self.resolve_actor(chat_user),
            "chat_user": chat_user,
            "body": raw.get("text", ""),
            "msg_id": raw.get("msg_id"),
            "thread_id": raw.get("thread_id"),
            "room_id": raw.get("room_id"),
        }
