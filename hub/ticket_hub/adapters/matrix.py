"""Matrix adapter (Element) — thread semantics: reply to the thread root event.

Config section (in ``$TICKET_HUB_HOME/adapters.json``, never in repo)::

    "matrix": {"token": "...", "homeserver": "https://matrix.example",
               "user_map": {"@alice:example": "Alice"}}

No config file / no matrix section / no token -> inert mode: posts are
recorded in ``self.outbox``, inbound still maps, zero network.
"""

from __future__ import annotations

from .base import ChatAdapter

__all__ = ["MatrixAdapter"]


class MatrixAdapter(ChatAdapter):
    platform = "matrix"

    def __init__(self, config_path=None, config=None, http_call=None):
        super().__init__(config_path=config_path, config=config,
                         http_call=http_call)
        # Matrix needs homeserver too; treat its absence as inert as well.
        self._homeserver = (self._config.get("homeserver") or "").rstrip("/")
        if not self._homeserver:
            self.inert = True

    def _send(self, text, binding, reply_to):
        """Post to room; when reply_to (a thread root event id) is present,
        reply inside that thread — child tickets land as subthread replies
        under the parent's root event."""
        room_id = binding.get("room_id")
        url = (f"{self._homeserver}/_matrix/client/v3/rooms/"
               f"{room_id}/send/m.room.message/{self._next_txn()}")
        content = {"msgtype": "m.text", "body": text}
        root = reply_to or binding.get("root_msg_id")
        if root:
            content["m.relates_to"] = {
                "rel_type": "m.thread", "event_id": root,
                "is_falling_back": True,
                "m.in_reply_to": {"event_id": root},
            }
        resp = self._request(
            "PUT", url,
            headers={"Authorization": f"Bearer {self._token}"},
            body=content,
        )
        return resp.get("event_id")

    def _next_txn(self):
        self._counter += 1
        return f"tickethub-{self._counter}"

    def _parse_inbound(self, raw):
        # Sync-API event shape (trimmed): {event_id, room_id, sender,
        # origin_server_ts, content, [m.relates_to]}
        content = raw.get("content") or {}
        relates = content.get("m.relates_to") or {}
        thread_root = relates.get("event_id") if relates.get("rel_type") == "m.thread" \
            else None
        sender = raw.get("sender", "unknown")
        binding = None
        for b in self._bindings.values():
            if b.get("room_id") == raw.get("room_id") and (
                not thread_root or b.get("root_msg_id") == thread_root
            ):
                binding = b
                break
        return {
            "ticket_id": binding["ticket_id"] if binding else None,
            "ts": raw.get("origin_server_ts"),
            "actor": self.resolve_actor(sender),
            "chat_user": sender,
            "body": content.get("body", ""),
            "msg_id": raw.get("event_id"),
            "thread_id": thread_root,
            "room_id": raw.get("room_id"),
        }
