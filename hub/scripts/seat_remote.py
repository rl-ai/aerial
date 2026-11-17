#!/usr/bin/env python3
"""seat_remote.py — swarm seat adapter for remote-model-chat.

The swarm harness pipes the ticket packet JSON on stdin and expects the
seat's answer on stdout. remote-model-chat takes the prompt as a file
argument (its stdin is used for the bearer), so this shim renders the
packet into a prompt file and forwards it.

Usage: seat_remote.py <seat> [max_tokens]   (packet JSON on stdin)

Set HUB_REMOTE_MODEL_CHAT to override the path to the remote-model-chat binary.
"""
import json
import os
import subprocess
import sys
import tempfile

RMC = os.environ.get("HUB_REMOTE_MODEL_CHAT") or os.path.expanduser(
    "~/.hermes/bin/remote-model-chat")

PROMPT_TMPL = """You are the '{role}' seat worker '{worker}' in the ticket-hub swarm.
Work the ticket below. Be concrete and terse; evidence over opinion.
Reply with your result only (it is posted as a comment on the ticket).

TICKET PACKET (JSON):
{packet}
"""


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: seat_remote.py <seat> [max_tokens] [role]",
              file=sys.stderr)
        return 2
    seat = sys.argv[1]
    max_tokens = sys.argv[2] if len(sys.argv) > 2 else "2048"
    role_hint = sys.argv[3] if len(sys.argv) > 3 else "?"
    packet = json.load(sys.stdin)
    role = role_hint if role_hint != "?" else packet.get(
        "role", packet.get("ticket", {}).get("role", "?"))
    prompt = PROMPT_TMPL.format(role=role,
                                worker=packet.get("worker", "?"),
                                packet=json.dumps(packet, indent=1)[:60000])
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        f.write(prompt)
        path = f.name
    proc = subprocess.run([RMC, seat, path, max_tokens],
                          capture_output=True, text=True, timeout=900)
    if proc.returncode != 0:
        print(f"seat {seat} failed: {(proc.stderr or proc.stdout)[:500]}",
              file=sys.stderr)
        return 1
    print(proc.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
