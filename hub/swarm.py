"""swarm.py — deterministic role-swarm harness (DESIGN-v2 pillar 3).

Reads ``roles/swarm.json``: a list of workers, each with ``role``, ``tag``,
``seat_cmd`` (receives the ticket packet on stdin; supports a ``{model}``
placeholder), ``interval_s`` and ``max_parallel``. Each tick, per worker:

  1. find ``running`` tickets carrying the worker's tag whose latest
     role-claim checkpoint is older than the last role completion (fresh or
     reopened work);
  2. CLAIM = append a ``checkpoint`` event with payload
     ``{claim: <role>, worker: <name>, ts}``, then RE-READ events and proceed
     only if the worker's own claim is the latest claim for that role
     (CAS via append-only events; a lost race skips the ticket — the hub owns
     state, the model never does);
  3. build the ticket packet (``hub_client.packet``), run the seat with the
     packet on stdin (injectable ``seat_runner`` callable for tests), and
     post the result as a ``comment`` event with actor ``role:<role>``;
  4. on any error append a ``checkpoint`` event carrying the reason — the
     harness NEVER changes ticket status.

Event appends retry up to 3 attempts (small sleep) on SQLite "database is
locked" — the hub survives its own restarts.

State: a watermark JSON (default ``~/.ticket-hub-swarm/state.json``,
atomic-written like outbound.py) keyed by claim event id; it prevents
re-posting the same claim.

Inert-safe: with no config file (or a worker whose ``seat_cmd`` is empty)
the harness logs what it would do, claims nothing, and calls nothing.

Run:
    python swarm.py run [--once] [--interval N] [--config PATH]
                        [--state PATH] [--url URL]
    python swarm.py status [--state PATH]
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import time

from hub_client import HubClient, HubError

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG_PATH = os.path.join(APP_DIR, "roles", "swarm.json")
DEFAULT_STATE_PATH = os.path.join(
    os.path.expanduser("~"), ".ticket-hub-swarm", "state.json"
)
DEFAULT_HUB_URL = os.environ.get("TICKET_HUB_URL", "http://127.0.0.1:8777")

# SQLite contention marker that is worth a bounded retry on event appends.
LOCKED_MARKER = "database is locked"
MAX_APPEND_ATTEMPTS = 3

__all__ = [
    "SwarmHarness",
    "default_seat_runner",
    "load_config",
    "render_status",
    "DEFAULT_CONFIG_PATH",
    "DEFAULT_STATE_PATH",
    "DEFAULT_HUB_URL",
]


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def load_config(path: str) -> dict:
    """Read swarm.json; missing/malformed -> {} (inert, never raises)."""
    if not path:
        return {}
    try:
        with open(os.path.expanduser(str(path)), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def default_seat_runner(cmd: str, packet: dict) -> str:
    """Run the seat as a subprocess with the packet JSON on stdin.

    Tests inject a callable with this same signature; nothing here runs in
    tests that inject one.
    """
    proc = subprocess.run(
        shlex.split(cmd),
        input=json.dumps(packet),
        capture_output=True,
        text=True,
        timeout=600,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"seat exited {proc.returncode}: "
            f"{(proc.stderr or proc.stdout or '')[:500]}"
        )
    return proc.stdout


class SwarmHarness:
    """Polls the hub, claims tickets via CAS, runs seats, posts evidence."""

    def __init__(self, hub, *, config: "dict | None" = None,
                 config_path: str = DEFAULT_CONFIG_PATH,
                 state_path: "str | None" = DEFAULT_STATE_PATH,
                 seat_runner=None,
                 sleep=time.sleep,
                 retry_delay_s: float = 0.05,
                 log=None):
        self.hub = hub
        self.config = config if config is not None else load_config(config_path)
        self.config_path = config_path
        self.state_path = state_path
        self.seat_runner = seat_runner or default_seat_runner
        self.sleep = sleep
        self.retry_delay_s = float(retry_delay_s)
        self.log = log if log is not None else (
            lambda msg: print(f"[swarm] {msg}"))
        # No workers configured -> globally inert (log-only, claims nothing).
        self.inert = not self.workers()
        self.state = self._load_state()

    # -- config ---------------------------------------------------------------

    def workers(self) -> list:
        out = []
        for w in (self.config or {}).get("workers") or []:
            if isinstance(w, dict) and w.get("role") and w.get("tag"):
                out.append(w)
        return out

    # -- state (atomic JSON, mirrors outbound.py) ------------------------------

    def _load_state(self) -> dict:
        if not self.state_path:
            return {"claims": {}}
        try:
            with open(os.path.expanduser(str(self.state_path)),
                      "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return {"claims": {}}
        if not isinstance(data, dict) or not isinstance(
                data.get("claims"), dict):
            return {"claims": {}}
        return data

    def _save_state(self) -> None:
        if not self.state_path:
            return
        path = os.path.expanduser(str(self.state_path))
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.state, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)

    # -- event append with locked-DB retry -------------------------------------

    def _append(self, ticket_id: str, kind: str, payload: dict, actor: str):
        """Append an event; bounded retry on 'database is locked'."""
        for attempt in range(MAX_APPEND_ATTEMPTS):
            try:
                return self.hub.event(ticket_id, kind, payload, actor=actor)
            except HubError as exc:
                if LOCKED_MARKER in (exc.message or "") and \
                        attempt < MAX_APPEND_ATTEMPTS - 1:
                    self.sleep(self.retry_delay_s)
                    continue
                raise
        raise RuntimeError("unreachable: append loop always returns or raises")

    # -- claim bookkeeping over the event log ----------------------------------

    def _role_events(self, ticket_id: str, role: str):
        """(claims, completions) for ``role`` on this ticket, sorted."""
        lin = self.hub.lineage(ticket_id) or {}
        events = [e for e in (lin.get("events") or [])
                  if isinstance(e, dict) and e.get("ticket_id") == ticket_id]
        events.sort(key=lambda e: (e.get("ts") or "", e.get("id") or ""))
        claims, completions = [], []
        for e in events:
            if e.get("kind") == "checkpoint" and \
                    (e.get("payload") or {}).get("claim") == role:
                claims.append(e)
            elif e.get("kind") == "comment" and \
                    (e.get("actor") or {}).get("name") == f"role:{role}":
                completions.append(e)
        return claims, completions

    def _posted_completion_ids(self) -> set:
        return {c.get("completion_id")
                for c in self.state["claims"].values()
                if isinstance(c, dict) and c.get("completion_id")}

    def _eligible(self, ticket_id: str, role: str) -> bool:
        """Claimable when: no completion exists and no claim has landed
        (fresh work); or the latest role completion is newer than the latest
        claim (re-triggered work — but NOT a completion we posted ourselves,
        which the watermark remembers; that is the anti-loop gate). A claim
        newer than the last completion means work in flight — skip, unless
        the claim is our own previous attempt that errored (retry path)."""
        claims, completions = self._role_events(ticket_id, role)
        if completions:
            last_c = completions[-1]
            last_claim_ts = claims[-1].get("ts") or "" if claims else ""
            if (last_c.get("ts") or "") <= last_claim_ts:
                return False  # claim newer than completion: in flight
            # Completion newer than claim: re-triggered work, unless it is
            # our own posted result (the watermark's anti-loop memory).
            return last_c.get("id") not in self._posted_completion_ids()
        if not claims:
            return True
        # Claim without completion: in flight — unless it is our own
        # attempt that errored (the watermark remembers our failures).
        entry = self.state["claims"].get(claims[-1].get("id")) or {}
        return bool(entry.get("error"))

    # -- one ticket, one worker --------------------------------------------------

    def _attempt(self, ticket: dict, worker: dict) -> bool:
        """Claim (CAS), run the seat, post the result. True if result posted."""
        role = worker["role"]
        name = worker.get("name") or role
        ticket_id = ticket["id"]

        # CLAIM: append, then re-read and check we are still the latest.
        claim = self._append(
            ticket_id, "checkpoint",
            {"claim": role, "worker": name, "ts": _now_iso()},
            actor=f"swarm:{name}")
        claims, _ = self._role_events(ticket_id, role)
        if not claims or claims[-1].get("id") != claim.get("id"):
            self.log(f"{name}: lost claim race on {ticket_id}; skipping")
            return False

        # Watermark: never re-post the same claim.
        claim_id = claim.get("id")
        if (self.state["claims"].get(claim_id) or {}).get("posted"):
            self.log(f"{name}: claim {claim_id} already posted; skipping")
            return False

        try:
            packet = self.hub.packet(ticket_id)
            seat_cmd = worker["seat_cmd"].replace(
                "{model}", worker.get("model") or "")
            output = self.seat_runner(seat_cmd, packet)
            result = self._append(ticket_id, "comment",
                                  {"text": str(output)}, actor=f"role:{role}")
            self.state["claims"][claim_id] = {
                "ticket": ticket_id, "role": role, "worker": name,
                "posted": True, "completion_id": result.get("id"),
                "ts": _now_iso()}
            self._save_state()
            self.log(f"{name}: posted result on {ticket_id}")
            return True
        except Exception as exc:  # seat or hub failure: record, keep status
            try:
                self._append(ticket_id, "checkpoint",
                             {"swarm_error": role, "reason": str(exc)},
                             actor=f"role:{role}")
            except Exception:
                pass  # a broken hub must not take the harness down mid-tick
            self.state["claims"][claim_id] = {
                "ticket": ticket_id, "role": role, "worker": name,
                "posted": False, "error": str(exc), "ts": _now_iso()}
            self._save_state()
            self.log(f"{name}: seat failed on {ticket_id}: {exc}")
            return False

    def _process(self, ticket: dict, worker: dict) -> bool:
        if not isinstance(ticket, dict) or not ticket.get("id"):
            return False
        if not self._eligible(ticket["id"], worker["role"]):
            return False
        return self._attempt(ticket, worker)

    # -- one pass ------------------------------------------------------------------

    def tick(self) -> int:
        """One poll pass over all configured workers. Returns results posted."""
        if self.inert:
            self.log(f"inert: no swarm config at {self.config_path}; "
                     f"nothing claimed, nothing called")
            return 0
        posted = 0
        for worker in self.workers():
            if not str(worker.get("seat_cmd") or "").strip():
                self.log(f"inert worker {worker.get('name') or worker['role']}: "
                         f"empty seat_cmd; would observe '{worker['role']}' "
                         f"tickets tagged '{worker['tag']}' — claiming nothing")
                continue
            try:
                tickets = self.hub.list(status="running", tag=worker["tag"]) \
                    or []
            except HubError as exc:
                self.log(f"worker {worker.get('name')}: list failed: "
                         f"{exc.code}: {exc.message}")
                continue
            for ticket in tickets:
                try:
                    if self._process(ticket, worker):
                        posted += 1
                except HubError as exc:
                    # Per-ticket hub failure: skip this ticket, next tick retries.
                    self.log(f"worker {worker.get('name')}: skip "
                             f"{(ticket or {}).get('id')}: "
                             f"{exc.code}: {exc.message}")
        return posted

    def run(self, once: bool = False, ticks: "int | None" = None,
            sleep=None):
        """Run ticks: ``once=True`` == one tick; ``ticks=N`` == N ticks;
        otherwise loop forever with the config/interval sleep."""
        sleep = sleep if sleep is not None else self.sleep
        limit = 1 if once else ticks
        interval = float((self.config or {}).get("interval_s") or 120.0)
        i = 0
        while True:
            self.tick()
            i += 1
            if limit is not None and i >= limit:
                return i
            sleep(interval)


# ---------------------------------------------------------------------------
# Status view (reads the watermark state file only — never the hub)
# ---------------------------------------------------------------------------

def render_status(state: dict) -> str:
    claims = (state or {}).get("claims") or {}
    if not claims:
        return "swarm: no claims yet (state file empty or absent)"
    lines = [f"{'CLAIM':<14} {'TICKET':<14} {'ROLE':<14} "
             f"{'WORKER':<12} {'POSTED':<7} TS/ERROR"]
    for claim_id in sorted(claims):
        c = claims[claim_id] or {}
        tail = c.get("ts", "") if c.get("posted") else \
            f"error: {c.get('error', '?')}"
        lines.append(f"{str(claim_id):<14} {str(c.get('ticket', '')):<14} "
                     f"{str(c.get('role', '')):<14} "
                     f"{str(c.get('worker', '')):<12} "
                     f"{('yes' if c.get('posted') else 'no'):<7} {tail}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="ticket-hub role-swarm harness (v2 pillar 3)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    rp = sub.add_parser("run", help="poll, claim, run seats, post results")
    rp.add_argument("--once", action="store_true", help="run one tick and exit")
    rp.add_argument("--interval", type=float, default=None,
                    help="override swarm.json interval_s")
    rp.add_argument("--config", default=DEFAULT_CONFIG_PATH,
                    help="swarm.json path")
    rp.add_argument("--state", default=DEFAULT_STATE_PATH,
                    help="swarm state (watermark) JSON path")
    rp.add_argument("--url", default=DEFAULT_HUB_URL, help="ticket-hub base URL")

    sp = sub.add_parser("status", help="print worker/claim map from state file")
    sp.add_argument("--state", default=DEFAULT_STATE_PATH,
                    help="swarm state (watermark) JSON path")

    args = parser.parse_args(argv)

    if args.cmd == "status":
        path = os.path.expanduser(str(args.state))
        try:
            with open(path, "r", encoding="utf-8") as fh:
                state = json.load(fh)
        except (OSError, ValueError):
            state = {"claims": {}}
        print(render_status(state))
        return 0

    harness = SwarmHarness(
        HubClient(base_url=args.url),
        config_path=args.config,
        state_path=args.state,
    )
    if args.interval is not None:
        harness.config = dict(harness.config, interval_s=args.interval)
    harness.run(once=args.once)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
