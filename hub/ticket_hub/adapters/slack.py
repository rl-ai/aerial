"""Slack adapter — thread_ts semantics inside a channel.

Config section (in ``$TICKET_HUB_HOME/adapters.json``, never in repo)::

    "slack": {"token": "xoxb-...", "user_map": {"U123": "Owner"}}

No config file / no slack section / no token -> inert mode: posts recorded
in ``self.outbox``, inbound still maps, zero network.
"""

from __future__ import annotations

from .base import ChatAdapter

__all__ = ["SlackAdapter"]

_API = "https://slack.com/api"


class SlackAdapter(ChatAdapter):
    platform = "slack"

    def _send(self, text, binding, reply_to):
        """chat.postMessage. thread_ts = the ticket's thread root; child
        tickets reply under the parent's root thread_ts (subthread)."""
        thread_ts = reply_to or binding.get("thread_id")
        body = {"channel": binding.get("room_id"), "text": text}
        if thread_ts:
            body["thread_ts"] = thread_ts
        resp = self._request("POST", f"{_API}/chat.postMessage",
                             headers=self._headers(), body=body)
        if not resp.get("ok", True):
            raise RuntimeError(f"slack post failed: {resp.get('error')}")
        # First post in a channel without a thread creates the thread root.
        if not thread_ts and resp.get("ts"):
            binding["thread_id"] = resp["ts"]
        return resp.get("ts")

    def _headers(self):
        return {"Authorization": f"Bearer {self._token}"}

    def _parse_inbound(self, raw):
        # Events API / RTM message (trimmed): {type: "message", channel, ts,
        #  thread_ts, user, text}
        user = raw.get("user", "unknown")
        channel = raw.get("channel")
        thread_ts = raw.get("thread_ts")
        binding = None
        for b in self._bindings.values():
            if b.get("room_id") == channel and (
                not thread_ts or b.get("thread_id") in (None, thread_ts)
                or b.get("root_msg_id") == thread_ts
            ):
                binding = b
                break
        return {
            "ticket_id": binding["ticket_id"] if binding else None,
            "ts": raw.get("ts"),
            "actor": self.resolve_actor(user),
            "chat_user": user,
            "body": raw.get("text", ""),
            "msg_id": raw.get("ts"),
            "thread_id": thread_ts,
            "room_id": channel,
        }
