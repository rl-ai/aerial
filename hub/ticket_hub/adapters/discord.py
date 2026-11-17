"""Discord adapter — thread per ticket, created under a parent root message.

Config section (in ``$TICKET_HUB_HOME/adapters.json``, never in repo)::

    "discord": {"token": "...", "guild_id": "...",
                "user_map": {"123": "Owner"}}

No config file / no discord section / no token -> inert mode: posts recorded
in ``self.outbox``, inbound still maps, zero network.
"""

from __future__ import annotations

from .base import ChatAdapter

__all__ = ["DiscordAdapter"]

_API = "https://discord.com/api/v10"


class DiscordAdapter(ChatAdapter):
    platform = "discord"

    def _send(self, text, binding, reply_to):
        """Post into the ticket's thread (thread_id = channel id of the
        thread). Child tickets reply under the parent's root message: when a
        reply_to root exists and the child has no thread yet, start a thread
        from that root message and post the reply there."""
        channel = binding.get("thread_id")
        if not channel and reply_to and binding.get("room_id"):
            # Subthread: create a thread anchored at the parent's root msg.
            created = self._request(
                "POST",
                f"{_API}/channels/{binding['room_id']}/messages/"
                f"{reply_to}/threads",
                headers=self._headers(),
                body={"name": (binding.get("ticket_id") or "ticket")[:96]},
            )
            channel = created.get("id")
            binding["thread_id"] = channel
        elif not channel:
            channel = binding.get("room_id")
        url = f"{_API}/channels/{channel}/messages"
        body = {"content": text}
        if reply_to and binding.get("thread_id"):
            body["message_reference"] = {"message_id": reply_to,
                                         "fail_if_not_exists": False}
        resp = self._request("POST", url, headers=self._headers(), body=body)
        return resp.get("id")

    def _headers(self):
        return {"Authorization": f"Bot {self._token}"}

    def _parse_inbound(self, raw):
        # Gateway MESSAGE_CREATE / MESSAGE_UPDATE payload (trimmed):
        # {id, channel_id, author: {id, username}, content,
        #  message_reference: {message_id}}
        author = raw.get("author") or {}
        chat_user = author.get("id", "unknown")
        ref = raw.get("message_reference") or {}
        referenced = ref.get("message_id")
        channel = raw.get("channel_id")
        binding = None
        for b in self._bindings.values():
            if b.get("thread_id") == channel or b.get("room_id") == channel:
                if referenced and b.get("root_msg_id") and \
                        b["root_msg_id"] != referenced and b.get("thread_id") != channel:
                    continue
                binding = b
                break
        return {
            "ticket_id": binding["ticket_id"] if binding else None,
            "ts": raw.get("timestamp"),
            "actor": self.resolve_actor(chat_user),
            "chat_user": chat_user,
            "body": raw.get("content", ""),
            "msg_id": raw.get("id"),
            "thread_id": channel,
            "room_id": binding["room_id"] if binding else channel,
        }
