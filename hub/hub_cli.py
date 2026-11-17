"""hub_cli — terminal front-end for the ticket-hub API.

Human output is one compact line per ticket: ``ID  STATUS  TITLE  [owner]``.
Every read verb takes ``--json`` for machine use.
Exit codes: 0 ok, 1 on HubError (message on stderr).

Usage examples:
    hub new "Fix flaky replay" --body "see logs" --tag qa --owner hermes
    hub ls --status open --json
    hub start TK-...        # draft -> open -> running (two patches if needed)
    hub done TK-...         # walks to review then done
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from typing import Any

from hub_client import HubClient, HubError

STATUS_ORDER = ["draft", "open", "running", "blocked", "review", "done",
                "cancelled"]


def _actor_name(args) -> str:
    return (args.actor_name or os.environ.get("HUB_ACTOR")
            or getpass.getuser())


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def ticket_line(t: dict) -> str:
    owner = t.get("owner") or {}
    name = owner.get("name")
    line = f"{t['id']}  {t.get('status', '?')}  {t.get('title', '')}"
    if name and name != "unknown":
        line += f"  [{name}]"
    return line


def _dump(obj: Any) -> str:
    return json.dumps(obj, indent=2)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def _cmd_new(hub: HubClient, args) -> int:
    t = hub.create_ticket(args.title, body=args.body or "",
                          parent=args.parent, owner=args.owner,
                          tags=args.tag or None, priority=args.priority)
    print(_dump(t) if getattr(args, "json", False) else ticket_line(t))
    return 0


def _cmd_get(hub: HubClient, args) -> int:
    t = hub.get(args.id)
    print(_dump(t) if args.json else ticket_line(t))
    return 0


def _cmd_ls(hub: HubClient, args) -> int:
    tickets = hub.list(status=args.status, family=args.family,
                       tag=args.tag, q=args.q)
    if args.json:
        print(_dump(tickets))
    else:
        for t in tickets:
            print(ticket_line(t))
    return 0


def _cmd_set(hub: HubClient, args) -> int:
    if not any(v is not None for v in
               (args.status, args.priority, args.title, args.body)):
        print("set: give at least one of --status/--priority/--title/--body",
              file=sys.stderr)
        return 1
    t = hub.patch(args.id, status=args.status, priority=args.priority,
                  title=args.title, body=args.body, actor=args.actor)
    print(ticket_line(t))
    return 0


def _cmd_comment(hub: HubClient, args) -> int:
    ev = hub.comment(args.id, args.text, actor=args.actor)
    print(f"{ev['id']}  comment  on {ev['ticket_id']}")
    return 0


def _cmd_event(hub: HubClient, args) -> int:
    payload: dict = {}
    if args.json_payload is not None:
        try:
            payload = json.loads(args.json_payload)
        except ValueError as exc:
            print(f"event: --json is not valid JSON: {exc}", file=sys.stderr)
            return 1
    ev = hub.event(args.id, args.kind, payload, actor=args.actor)
    print(f"{ev['id']}  {ev['kind']}  on {ev['ticket_id']}")
    return 0


def _cmd_link(hub: HubClient, args) -> int:
    edge = hub.edge(args.id, args.dst, args.type)
    print(f"{edge['id']}  {edge['src']}  -{edge['type']}->  {edge['dst']}")
    return 0


def _cmd_plan(hub: HubClient, args) -> int:
    plan = hub.plan(args.id)
    if args.json:
        print(_dump(plan))
        return 0
    t = plan["ticket"]
    p = plan["progress"]
    print(f"{t['id']}  [{t['status']}]  {t['title']}")
    if plan.get("summary"):
        print()
        for line in plan["summary"].splitlines()[:6]:
            print(f"  {line}")
    print()
    bar_w = 30
    filled = int(bar_w * p["percent"] / 100)
    print(f"  progress  [{'#' * filled}{'.' * (bar_w - filled)}] "
          f"{p['percent']}%  ({p['done']}/{p['total']} done)")
    if plan["blocked_by_owner"]:
        print(f"\n  BLOCKED BY OWNER ({len(plan['blocked_by_owner'])}):")
        for b in plan["blocked_by_owner"][:10]:
            print(f"    ! {b['id']}  [{b['status']}]  {b['title']}")
    if plan["remaining_items"]:
        print(f"\n  REMAINING ({len(plan['remaining_items'])}):")
        for r in plan["remaining_items"][:12]:
            owner = f"  [{r['owner']}]" if r.get("owner") else ""
            print(f"    - {r['id']}  [{r['status']}]  {r['title']}{owner}")
        if len(plan["remaining_items"]) > 12:
            print(f"    … {len(plan['remaining_items']) - 12} more")
    if plan["remaining_notes"]:
        print("\n  REMAINING (noted in plan):")
        for item in plan["remaining_notes"][:8]:
            print(f"    · {item}")
    act = plan.get("recent_activity") or []
    if act:
        print("\n  RECENT:")
        for e in act[-5:]:
            actor = (e.get("actor") or {}).get("name", "?")
            print(f"    {e.get('ts', '')[:19]}  {actor}: {e.get('kind')}")
    return 0


def _cmd_lineage(hub: HubClient, args) -> int:
    lin = hub.lineage(args.id)
    if args.json:
        print(_dump(lin))
        return 0
    ancestry = lin.get("ancestry", [])
    if ancestry:
        chain = " -> ".join(t["id"] for t in ancestry)
        print(f"ancestry:  {chain} -> {lin['ticket']['id']}")
    print(ticket_line(lin["ticket"]))
    print(f"children: {len(lin.get('descendants', []))}  "
          f"related: {len(lin.get('related', []))}  "
          f"events: {len(lin.get('events', []))}")
    return 0


def _cmd_digest(hub: HubClient, args) -> int:
    if args.md:
        text = hub.digest(args.id, fmt="md", full=args.full)
        sys.stdout.write(text if text.endswith("\n") else text + "\n")
        return 0
    digest = hub.digest(args.id, fmt="json", full=args.full)
    if args.json:
        print(_dump(digest))
        return 0
    print(f"# Updates — {digest.get('project_title') or digest.get('project')}")
    for b in digest.get("bullets", []):
        cites = ", ".join(b.get("event_ids", []))
        print(f"- {b['text']} ({cites})")
    return 0


def _cmd_packet(hub: HubClient, args) -> int:
    pkt = hub.packet(args.id)
    if args.json:
        print(_dump(pkt))
        return 0
    t = pkt["ticket"]
    print(f"== PACKET: {t['id']} [{t.get('status')}] {t.get('title')} ==")
    owner = (t.get("owner") or {}).get("name")
    if owner and owner != "unknown":
        print(f"owner: {owner}")
    if t.get("tags"):
        print(f"tags: {', '.join(t['tags'])}")
    if (t.get("body") or "").strip():
        print("\n-- ticket body --")
        for line in (t["body"].strip().splitlines())[:40]:
            print(f"  {line}")
    ancestors = pkt.get("ancestors") or []
    if ancestors:
        print("\n-- ancestors --")
        for a in ancestors:
            print(f"  {a['id']}  [{a.get('status')}]  {a.get('title')}")
    plan = pkt.get("plan")
    if plan:
        p = plan.get("progress") or {}
        print("\n-- plan --")
        print(f"  progress {p.get('percent', 0)}% "
              f"({p.get('done', 0)}/{p.get('total', 0)} done)")
        for r in (plan.get("remaining_items") or [])[:12]:
            print(f"  - {r['id']}  [{r['status']}]  {r['title']}")
    blocked = pkt.get("blocked") or []
    if blocked:
        print(f"\n-- blocked ({len(blocked)}) --")
        for b in blocked[:10]:
            print(f"  ! {b['id']}  [{b['status']}]  {b['title']}")
    events = pkt.get("events") or []
    print(f"\n-- last {len(events)} events --")
    for e in events:
        actor = (e.get("actor") or {}).get("name", "?")
        payload = e.get("payload") or {}
        text = payload.get("text") or payload.get("marker") or ""
        extra = f"  {str(text)[:100]}" if text else ""
        print(f"  {e.get('ts', '')[:19]}  {actor}: {e.get('kind')}{extra}")
    threads = pkt.get("threads") or []
    print(f"\n-- threads ({len(threads)}) --")
    for th in threads[:10]:
        print(f"  {th.get('platform')}  room={th.get('room_id')}"
              f"  thread={th.get('thread_id') or '-'}")
    pages = pkt.get("wiki_pages") or []
    print(f"\n-- related wiki pages ({len(pages)}) --")
    for pg in pages[:10]:
        slug = pg.get("slug") or pg.get("id") or "?"
        print(f"  [[{slug}]]  {pg.get('title', '')}")
    return 0


def _cmd_seats(hub: HubClient, args) -> int:
    roster = hub.seats()
    if args.json:
        print(_dump(roster))
        return 0
    seats = roster.get("seats", {})
    if seats:
        print("SEAT                 HEALTH  LOAD  ROLES")
        for name, s in sorted(seats.items()):
            print(f"  {name:<18}  {s['health']:<6}  {s['load']:<4}  "
                  f"{', '.join(s['roles'])}")
    assignments = roster.get("assignments", [])
    if assignments:
        print(f"\nOPEN ASSIGNMENTS ({len(assignments)}):")
        for a in assignments:
            print(f"  {a['ticket_id']}  {a['seat']}@{a['role']}  "
                  f"[{a['status']}]  {a['title'][:50]}")
    if not seats and not assignments:
        print("no seats registered / no open assignments")
    return 0


def _cmd_assign(hub: HubClient, args) -> int:
    res = hub.assign(args.id, args.role, seat=args.seat,
                     actor={"name": _actor_name(args)})
    print(f"{res['ticket_id']}  ->  {res['seat']}  ({res['role']}, "
          f"{res['authority']})  {res['event_id']}")
    return 0


def _cmd_release(hub: HubClient, args) -> int:
    res = hub.release(args.id, actor={"name": _actor_name(args)})
    rel = res.get("released") or {}
    print(f"{res['ticket_id']}  released  "
          f"{rel.get('seat', '?')}@{rel.get('role', '?')}")
    return 0


def _cmd_templates(hub: HubClient, args) -> int:
    data = hub.api_get("/templates")
    if args.json:
        print(_dump(data))
        return 0
    for t in data.get("templates", []):
        print(f"  {t['name']:<18} [{t['source']}] {t['steps']} steps  "
              f"— {t['description'][:60]}")
    return 0


def _cmd_template(hub: HubClient, args) -> int:
    t = hub.api_get(f"/templates/{args.name}")
    print(_dump(t))
    return 0


def _cmd_instantiate(hub: HubClient, args) -> int:
    res = hub.api_post("/templates/%s/instantiate" % args.name,
                   {"title": args.title, "parent": args.parent,
                    "actor": {"name": _actor_name(args)},
                    "assign": not args.no_assign})
    root = res["root"]
    print(f"root: {root['id']}  {root['title']}")

    def walk(nodes, depth=1):
        for n in nodes:
            mark = n["assigned"] or (f"!{n['assign_error']}"
                                     if n.get("assign_error") else "-")
            print("  " * (depth + 1) + f"{n['id']}  [{mark}]  {n['title']}")
            walk(n.get("children", []), depth + 1)
    walk(res["tree"])
    return 0


def _cmd_board(hub: HubClient, args) -> int:
    board = hub.board()
    if args.json:
        print(_dump(board))
        return 0
    columns = board.get("columns", {})
    counts = board.get("child_counts", {})
    order = STATUS_ORDER + [c for c in columns if c not in STATUS_ORDER]
    for status in order:
        tickets = columns.get(status, [])
        if not tickets:
            continue
        print(f"== {status} ({len(tickets)}) ==")
        for t in tickets:
            line = ticket_line(t)
            kids = counts.get(t["id"], 0)
            if kids:
                line += f"  ({kids} children)"
            print(line)
    return 0


def _cmd_bind(hub: HubClient, args) -> int:
    b = hub.bind(args.id, args.platform, args.room,
                 thread_id=args.thread, root_msg_id=args.root)
    print(f"{b.get('id', '')}  bound  {b['platform']}:{b['room_id']}"
          + (f"  thread={b['thread_id']}" if b.get("thread_id") else ""))
    return 0


def _walk_to(hub: HubClient, ticket_id: str, target: str,
             actor: str | None) -> dict:
    """Multi-hop status walk using only legal transitions."""
    t = hub.get(ticket_id)
    if target == "running":
        if t["status"] == "draft":
            t = hub.patch(ticket_id, status="open", actor=actor)
        if t["status"] != "running":
            t = hub.patch(ticket_id, status="running", actor=actor)
    elif target == "done":
        if t["status"] == "draft":
            t = hub.patch(ticket_id, status="open", actor=actor)
        if t["status"] not in ("review", "done"):
            t = hub.patch(ticket_id, status="review", actor=actor)
        if t["status"] != "done":
            t = hub.patch(ticket_id, status="done", actor=actor)
    else:  # pragma: no cover - guarded by argparse choices
        raise ValueError(f"unknown target status: {target}")
    return t


def _cmd_start(hub: HubClient, args) -> int:
    print(ticket_line(_walk_to(hub, args.id, "running", args.actor)))
    return 0


def _cmd_done(hub: HubClient, args) -> int:
    print(ticket_line(_walk_to(hub, args.id, "done", args.actor)))
    return 0


# ---------------------------------------------------------------------------
# Wiki commands
# ---------------------------------------------------------------------------

def wiki_line(p: dict) -> str:
    tags = f"  [{','.join(p.get('tags') or [])}]" if p.get("tags") else ""
    return f"{p['slug']}  r{p.get('rev', '?')}  {p.get('title', '')}{tags}"


def _cmd_wiki_ls(hub: HubClient, args) -> int:
    pages = hub.wiki_ls()
    if args.json:
        print(_dump(pages))
    else:
        for p in pages:
            print(wiki_line(p))
    return 0


def _cmd_wiki_get(hub: HubClient, args) -> int:
    result = hub.wiki_get(args.slug)
    if args.json:
        print(_dump(result))
        return 0
    page = result["page"]
    print(f"# {page.get('title', '')}")
    print(f"slug: {page['slug']}  rev: {page.get('rev')}  "
          f"actor: {page.get('actor', '')}  ts: {page.get('ts', '')}")
    if page.get("tags"):
        print("tags: " + ", ".join(page["tags"]))
    print()
    print(page.get("body_md", ""))
    return 0


def _cmd_wiki_set(hub: HubClient, args) -> int:
    if not args.title:
        print("wiki set: --title is required", file=sys.stderr)
        return 1
    if args.body is not None and args.file is not None:
        print("wiki set: give --body or --file, not both", file=sys.stderr)
        return 1
    body = args.body or ""
    if args.file is not None:
        try:
            with open(args.file, encoding="utf-8") as fh:
                body = fh.read()
        except OSError as exc:
            print(f"wiki set: cannot read {args.file}: {exc}",
                  file=sys.stderr)
            return 1
    page = hub.wiki_set(args.slug, args.title, body, tags=args.tag or None,
                        actor=args.actor)
    print(wiki_line(page))
    return 0


def _cmd_wiki_search(hub: HubClient, args) -> int:
    results = hub.wiki_search(args.query)
    if args.json:
        print(_dump(results))
    else:
        for r in results:
            snippet = (r.get("snippet") or "").replace("\n", " ")
            line = f"{r['slug']}  {r.get('title', '')}"
            if snippet:
                line += f"  — {snippet[:80]}"
            print(line)
    return 0


# ---------------------------------------------------------------------------
# Parser / entry point
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="hub", description="ticket-hub command-line client")
    p.add_argument("--url", help="hub base URL (default: $TICKET_HUB_URL or "
                                 "http://127.0.0.1:8777)")
    p.add_argument("--timeout", type=float, default=None,
                   help="HTTP timeout seconds")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("new", help="create a ticket")
    sp.add_argument("title")
    sp.add_argument("--body", default="")
    sp.add_argument("--parent", default=None)
    sp.add_argument("--owner", default=None)
    sp.add_argument("--tag", action="append", default=[])
    sp.add_argument("--priority", default=None)
    sp.add_argument("--json", action="store_true", help="print created ticket as raw JSON")
    sp.set_defaults(func=_cmd_new)

    sp = sub.add_parser("get", help="show one ticket")
    sp.add_argument("id")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=_cmd_get)

    sp = sub.add_parser("ls", help="list tickets")
    sp.add_argument("--status", default=None)
    sp.add_argument("--family", default=None)
    sp.add_argument("--tag", default=None)
    sp.add_argument("--q", default=None)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=_cmd_ls)

    sp = sub.add_parser("set", help="patch fields on a ticket")
    sp.add_argument("id")
    sp.add_argument("--status", default=None)
    sp.add_argument("--priority", default=None)
    sp.add_argument("--title", default=None)
    sp.add_argument("--body", default=None)
    sp.add_argument("--actor", default=None)
    sp.set_defaults(func=_cmd_set)

    sp = sub.add_parser("comment", help="add a comment event")
    sp.add_argument("id")
    sp.add_argument("text")
    sp.add_argument("--actor", default=None)
    sp.set_defaults(func=_cmd_comment)

    sp = sub.add_parser("event", help="append a typed event")
    sp.add_argument("id")
    sp.add_argument("kind")
    sp.add_argument("--json", dest="json_payload", default=None,
                    help="event payload as a JSON object string")
    sp.add_argument("--actor", default=None)
    sp.set_defaults(func=_cmd_event)

    sp = sub.add_parser("link", help="add an edge ID -> DST of TYPE")
    sp.add_argument("id")
    sp.add_argument("dst")
    sp.add_argument("type")
    sp.set_defaults(func=_cmd_link)

    sp = sub.add_parser("lineage", help="ancestry + descendants + events")
    sp.add_argument("id")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=_cmd_lineage)

    sp = sub.add_parser("plan",
                        help="what this ticket is about, progress, what remains")
    sp.add_argument("id")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=_cmd_plan)

    sp = sub.add_parser("digest", help="translated project digest")
    sp.add_argument("id")
    sp.add_argument("--md", action="store_true", help="markdown output")
    sp.add_argument("--json", action="store_true", help="raw JSON output")
    sp.add_argument("--full", action="store_true",
                    help="canonical lane-C translation (all event kinds)")
    sp.set_defaults(func=_cmd_digest)

    sp = sub.add_parser("packet",
                        help="complete shared packet for one agent "
                             "(ticket, ancestors, plan, events, threads, wiki)")
    sp.add_argument("id")
    sp.add_argument("--json", action="store_true", help="raw JSON output")
    sp.set_defaults(func=_cmd_packet)

    sp = sub.add_parser("board", help="tickets grouped by status")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=_cmd_board)

    sp = sub.add_parser("seats",
                        help="seat roster: health, load, licensed roles, "
                             "open assignments")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=_cmd_seats)

    sp = sub.add_parser("templates", help="list workflow templates")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=_cmd_templates)

    sp = sub.add_parser("template", help="show one template (JSON)")
    sp.add_argument("name")
    sp.set_defaults(func=_cmd_template)

    sp = sub.add_parser("instantiate",
                        help="create a ticket subtree from a template, "
                             "auto-assigning seats per step")
    sp.add_argument("name")
    sp.add_argument("title")
    sp.add_argument("--parent", help="attach the root under a project id")
    sp.add_argument("--actor-name", default=None)
    sp.add_argument("--no-assign", action="store_true",
                    help="create tickets without seat assignment")
    sp.set_defaults(func=_cmd_instantiate)

    sp = sub.add_parser("assign",
                        help="assign a role (and optionally a seat) to a "
                             "ticket")
    sp.add_argument("id")
    sp.add_argument("role")
    sp.add_argument("--seat", help="pin a specific seat (must be licensed "
                                   "for the role)")
    sp.add_argument("--actor-name", default=None)
    sp.set_defaults(func=_cmd_assign)

    sp = sub.add_parser("release", help="release a ticket's current holder")
    sp.add_argument("id")
    sp.add_argument("--actor-name", default=None)
    sp.set_defaults(func=_cmd_release)

    sp = sub.add_parser("bind", help="bind a chat thread to a ticket")
    sp.add_argument("id")
    sp.add_argument("platform")
    sp.add_argument("room")
    sp.add_argument("--thread", default=None)
    sp.add_argument("--root", default=None)
    sp.set_defaults(func=_cmd_bind)

    sp = sub.add_parser("start", help="move ticket to running "
                                      "(open first if draft)")
    sp.add_argument("id")
    sp.add_argument("--actor", default=None)
    sp.set_defaults(func=_cmd_start)

    sp = sub.add_parser("done", help="move ticket to done "
                                     "(via open/review as needed)")
    sp.add_argument("id")
    sp.add_argument("--actor", default=None)
    sp.set_defaults(func=_cmd_done)

    # -- wiki (DESIGN-v2 pillar 1) ------------------------------------------
    wiki = sub.add_parser("wiki", help="wiki pages (ls|get|set|search)")
    wsub = wiki.add_subparsers(dest="wiki_command", required=True)

    wsp = wsub.add_parser("ls", help="list pages (latest revision each)")
    wsp.add_argument("--json", action="store_true")
    wsp.set_defaults(func=_cmd_wiki_ls)

    wsp = wsub.add_parser("get", help="show one page (with history)")
    wsp.add_argument("slug")
    wsp.add_argument("--json", action="store_true")
    wsp.set_defaults(func=_cmd_wiki_get)

    wsp = wsub.add_parser("set", help="append a new revision of a page")
    wsp.add_argument("slug")
    wsp.add_argument("--title", default=None)
    wsp.add_argument("--body", default=None)
    wsp.add_argument("--file", default=None,
                     help="read body markdown from FILE (alternative to --body)")
    wsp.add_argument("--tag", action="append", default=[])
    wsp.add_argument("--actor", default=None)
    wsp.set_defaults(func=_cmd_wiki_set)

    wsp = wsub.add_parser("search", help="substring search title/body/tags")
    wsp.add_argument("query")
    wsp.add_argument("--json", action="store_true")
    wsp.set_defaults(func=_cmd_wiki_search)

    return p


def main(argv: list[str] | None = None,
         client: HubClient | None = None) -> int:
    args = build_parser().parse_args(argv)
    hub = client or HubClient(base_url=args.url,
                              **({} if args.timeout is None
                                 else {"timeout": args.timeout}))
    try:
        return args.func(hub, args)
    except HubError as exc:
        print(f"error: {exc.code}: {exc.message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
