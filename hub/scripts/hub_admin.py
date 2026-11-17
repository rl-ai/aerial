#!/usr/bin/env python3
"""hub_admin.py — access administration CLI (ACCESS-P1-CONTRACT §4, lane W4).

Operates directly on the hub database named by env ``TICKET_HUB_DB`` (required;
exit 2 with a clear message when unset). Stdlib + ticket_hub only.

Commands:
    actor add ID --name N [--family F] [--kind agent|human|system]
              [--realm internal|external] [--sponsor ID] [--expires 30d|ISO]
              [--owner] --by ID
    actor suspend ID --by ID [--reason R]      actor activate ID --by ID [--reason R]
    actor adopt ID [--kind K] [--family F] [--owner] --by ID
    actor adopt-legacy --by ID
    actor list
    token issue ACTOR --out FILE [--expires 30d|ISO] [--scopes S ...]
              [--act-as GLOB ...] --by ID
    token revoke CRED_ID --by ID [--reason R]   token list
    grant add ACTOR --scope-type ticket|subtree|wiki_page --scope ID
              [--expires 90d|ISO] --by ID [--reason R]
    grant revoke GRANT_ID --by ID [--reason R]  grant list [--actor ID]
    label ticket ID LABEL --by ID [--reason R]  label wiki SLUG LABEL --by ID [--reason R]

Rules:
- Every mutating command requires ``--by ID``; that actor must be is_owner
  (exit 3 otherwise). Exception: ``actor add ... --owner`` with any --by value
  ONLY while no owner exists yet (bootstrap); ``actor adopt ... --owner`` uses
  the same bootstrap rule.
- ``token issue --out FILE`` creates FILE with mode 0600 (O_CREAT|O_EXCL),
  refuses if it exists (exit 3), writes token + newline, and prints only the
  credential id and prefix. The token string is never printed, logged, or put
  in an error message.
- Durations ``30d`` / ``12h`` or an ISO-8601 UTC timestamp -> stored ISO UTC.
- Every mutating command appends a ``decision`` event (label internal, payload
  with ids only, never token values) on the ticket tagged ``access-ledger``,
  creating that ticket (label internal, title "access-ledger") on first use.

Exit codes: 0 ok, 2 usage, 3 refused/validation (ValueError/PermissionError),
4 not found (KeyError).
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from ticket_hub.access import AccessStore  # noqa: E402
from ticket_hub.model import Actor, check_label  # noqa: E402
from ticket_hub.store import Store  # noqa: E402


EXIT_OK = 0
EXIT_USAGE = 2
EXIT_REFUSED = 3
EXIT_NOT_FOUND = 4

LEDGER_TAG = "access-ledger"
LEDGER_TITLE = "access-ledger"

_DURATION_RE = re.compile(r"^(\d+)\s*([dh])$", re.IGNORECASE)
_DURATION_UNIT = {"d": "days", "h": "hours"}


class UsageError(Exception):
    """Raised for argument-level problems that argparse cannot express."""


def parse_duration(value: str) -> str:
    """'30d' / '12h' / ISO-8601 UTC timestamp -> canonical ISO UTC string.

    Relative durations are measured from now (UTC). Anything else raises
    ValueError (exit 3)."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"invalid duration/timestamp: {value!r}")
    text = value.strip()
    m = _DURATION_RE.match(text)
    if m:
        amount = int(m.group(1))
        unit = _DURATION_UNIT[m.group(2).lower()]
        dt = datetime.now(timezone.utc) + timedelta(**{unit: amount})
        return dt.isoformat(timespec="milliseconds")
    probe = text
    if probe.endswith(("Z", "z")):
        probe = probe[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(probe)
    except ValueError:
        raise ValueError(
            f"invalid duration/timestamp: {value!r} "
            "(expected e.g. 30d, 12h, or an ISO-8601 UTC timestamp)") from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds")


# ---------------------------------------------------------------------------
# ledger
# ---------------------------------------------------------------------------

def _find_ledger(store: Store) -> "str | None":
    for t in store.list_tickets(tag=LEDGER_TAG):
        return t.id
    return None


def ensure_ledger(store: Store) -> str:
    """Return the access-ledger ticket id, creating it (internal label,
    tag access-ledger) on first use. The ticket is owned by the system actor
    regardless of who triggered creation (a bootstrap --by value is not
    necessarily a real actor)."""
    tid = _find_ledger(store)
    if tid is not None:
        return tid
    ticket = store.create_ticket(
        LEDGER_TITLE,
        body="Append-only record of access-administration operations "
             "(actor/token/grant/label changes). Ids only; never token values.",
        tags=[LEDGER_TAG],
        label="internal",
    )
    return ticket.id


def ledger_event(store: Store, access: AccessStore, by_id: str,
                 op: str, payload: dict) -> None:
    """Append a 'decision' event (label internal) on the access-ledger ticket.
    Payload carries ids only — never token values."""
    actor = access.actor_attrs(by_id)
    model_actor = (Actor(id=actor["id"], name=actor["name"],
                         family=actor["family"], kind=actor["kind"])
                   if actor is not None
                   else Actor(id=by_id or "unknown", name=by_id or "unknown"))
    tid = ensure_ledger(store)
    body = {"op": op}
    body.update(payload)
    store.append_event(tid, "decision", actor=model_actor, payload=body,
                       label="internal")


# ---------------------------------------------------------------------------
# authority
# ---------------------------------------------------------------------------

def require_owner(access: AccessStore, by_id: str, what: str) -> None:
    """Every mutating command: --by must name a LIVE (G4) is_owner actor.
    A suspended/revoked or expired owner is refused. PermissionError (exit 3)
    otherwise."""
    if not by_id:
        raise UsageError(f"--by is required for {what}")
    attrs = access.actor_attrs(by_id)
    if attrs is None or not attrs["is_owner"]:
        raise PermissionError(
            f"not owner: --by actor {by_id!r} is not an owner; "
            f"{what} requires an owner")
    if not access.actor_live(by_id):
        raise PermissionError(
            f"not owner: --by actor {by_id!r} is an owner but not live "
            f"(suspended/revoked or expired); "
            f"{what} requires a live owner")


def has_any_owner(store: Store, access: AccessStore) -> bool:
    """True once any actor row carries is_owner (checked via the public
    actor_attrs). On a fresh DB the actors table simply has no rows."""
    for row in store.conn.execute("SELECT id FROM actors"):
        attrs = access.actor_attrs(row["id"])
        if attrs and attrs["is_owner"]:
            return True
    return False


# ---------------------------------------------------------------------------
# command handlers (return exit code; raise for error paths)
# ---------------------------------------------------------------------------

def cmd_actor_add(store, access, args) -> int:
    expires_at = parse_duration(args.expires) if args.expires else None
    bootstrap = bool(args.owner) and not has_any_owner(store, access)
    if bootstrap:
        # Bootstrap: --owner with ANY --by value, but ONLY while no owner
        # exists; no owner check runs on this path.
        pass
    elif args.owner:
        # adding an owner once one exists is itself an owner-only mutation
        require_owner(access, args.by, "actor add --owner")
    else:
        require_owner(access, args.by, "actor add")
    access.create_actor(
        args.id, args.name,
        family=args.family, kind=args.kind, realm=args.realm,
        sponsor_id=args.sponsor, expires_at=expires_at,
        is_owner=bool(args.owner), by=args.by,
    )
    ledger_event(store, access, args.by, "actor_add", {
        "actor": args.id, "kind": args.kind, "realm": args.realm,
        "owner": bool(args.owner), "bootstrap": bootstrap,
        "sponsor": args.sponsor, "expires_at": expires_at,
    })
    print(f"actor {args.id} added (realm={args.realm}, "
          f"kind={args.kind}, owner={bool(args.owner)})")
    return EXIT_OK


def _set_status(store, access, args, status: str) -> int:
    require_owner(access, args.by, f"actor {status}")
    access.set_actor_status(args.id, status, by=args.by,
                            reason=args.reason or "")
    ledger_event(store, access, args.by, f"actor_{status}",
                 {"actor": args.id, "reason": args.reason or ""})
    print(f"actor {args.id} -> {status}")
    return EXIT_OK


def cmd_actor_suspend(store, access, args) -> int:
    return _set_status(store, access, args, "suspended")


def cmd_actor_activate(store, access, args) -> int:
    return _set_status(store, access, args, "active")


def cmd_actor_adopt(store, access, args) -> int:
    bootstrap = bool(args.owner) and not has_any_owner(store, access)
    if bootstrap:
        # same rule as actor add --owner: ANY --by value, but ONLY while no
        # owner exists; after bootstrap the adopted actor is live and is_owner.
        pass
    elif args.owner:
        require_owner(access, args.by, "actor adopt --owner")
    else:
        require_owner(access, args.by, "actor adopt")
    actor = access.adopt_actor(
        args.id, by=args.by, kind=args.kind, family=args.family,
        is_owner=bool(args.owner),
    )
    ledger_event(store, access, args.by, "actor_adopt", {
        "actor": args.id, "kind": args.kind, "family": args.family,
        "owner": bool(args.owner), "bootstrap": bootstrap,
    })
    print(f"actor {args.id} adopted (kind={actor.kind}, "
          f"family={actor.family}, owner={bool(args.owner)})")
    return EXIT_OK


def cmd_actor_adopt_legacy(store, access, args) -> int:
    require_owner(access, args.by, "actor adopt-legacy")
    ids = access.adopt_legacy(by=args.by)
    ledger_event(store, access, args.by, "actor_adopt_legacy",
                 {"count": len(ids), "actors": ids})
    print(f"adopted {len(ids)} legacy actor(s): "
          + (", ".join(ids) if ids else "-"))
    return EXIT_OK


def cmd_actor_list(store, access, args) -> int:
    # one line per actor, built from the Store rows + AccessStore attrs
    for row in store.conn.execute(
            "SELECT id FROM actors ORDER BY id").fetchall():
        attrs = access.actor_attrs(row["id"]) or {}
        print(f"actor {row['id']} name={attrs.get('name') or '-'} "
              f"family={attrs.get('family') or '-'} "
              f"kind={attrs.get('kind') or '-'} "
              f"realm={attrs.get('realm') or '-'} "
              f"clearance={attrs.get('clearance') or '-'} "
              f"sponsor={attrs.get('sponsor_id') or '-'} "
              f"expires_at={attrs.get('expires_at') or '-'} "
              f"is_owner={1 if attrs.get('is_owner') else 0} "
              f"status={attrs.get('status') or 'none'}")
    return EXIT_OK


def cmd_token_issue(store, access, args) -> int:
    require_owner(access, args.by, "token issue")
    if not args.out:
        raise UsageError("token issue requires --out FILE")
    expires_at = parse_duration(args.expires) if args.expires else None
    scopes = list(args.scopes or [])
    act_as_allow = list(args.act_as or [])
    # Claim the output file FIRST (O_CREAT|O_EXCL, mode 0600): if it already
    # exists we refuse (exit 3) WITHOUT issuing a credential. The token value
    # never appears in any message below.
    try:
        fd = os.open(args.out, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise PermissionError(
            f"token file already exists (refusing to overwrite): {args.out}")
    except OSError as exc:
        raise PermissionError(
            f"cannot create token file {args.out}: {exc.strerror}") from None
    try:
        cred_id, token = access.issue_credential(
            args.actor_id, by=args.by, expires_at=expires_at,
            scopes=scopes, act_as_allow=act_as_allow)
    except Exception:
        # issue failed: remove the empty placeholder we just created.
        _discard_token_file(fd, args.out)
        raise
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(token + "\n")
    except OSError:
        # The write failed after issuing: revoke the credential immediately
        # and remove the (empty or partial) file. Token value stays out of
        # every message.
        _discard_token_file(None, args.out)
        try:
            access.revoke_credential(cred_id, by=args.by,
                                     reason="token file write failed")
        except Exception:
            pass  # the revocation attempt is best-effort; report the write
        raise PermissionError(f"cannot write token file: {args.out}") from None
    prefix = token[:12]
    ledger_event(store, access, args.by, "token_issue", {
        "actor": args.actor_id, "credential": cred_id, "prefix": prefix,
        "expires_at": expires_at, "scopes": scopes,
        "act_as_allow": act_as_allow,
    })
    print(f"credential {cred_id} prefix={prefix} actor={args.actor_id} "
          f"expires_at={expires_at or '-'} token written to {args.out}")
    return EXIT_OK


def _discard_token_file(fd, path: str) -> None:
    """Close *fd* if still open and remove the placeholder token file *path*."""
    if fd is not None:
        try:
            os.close(fd)
        except OSError:
            pass
    try:
        os.unlink(path)
    except OSError:
        pass


def cmd_token_revoke(store, access, args) -> int:
    require_owner(access, args.by, "token revoke")
    access.revoke_credential(args.credential_id, by=args.by,
                             reason=args.reason or "")
    ledger_event(store, access, args.by, "token_revoke",
                 {"credential": args.credential_id,
                  "reason": args.reason or ""})
    print(f"credential {args.credential_id} revoked")
    return EXIT_OK


def cmd_token_list(store, access, args) -> int:
    for c in access.list_credentials():
        scopes = ",".join(c["scopes"]) or "-"
        print(f"token {c['id']} actor={c['actor_id']} prefix={c['prefix']} "
              f"scopes={scopes} expires_at={c['expires_at'] or '-'} "
              f"revoked={1 if c['revoked'] else 0} "
              f"last_used_at={c['last_used_at'] or '-'}")
    return EXIT_OK


def cmd_grant_add(store, access, args) -> int:
    require_owner(access, args.by, "grant add")
    expires_at = parse_duration(args.expires) if args.expires else None
    grant_id = access.add_grant(
        args.actor_id, args.scope_type, args.scope_id,
        by=args.by, reason=args.reason or "", expires_at=expires_at)
    ledger_event(store, access, args.by, "grant_add", {
        "actor": args.actor_id, "grant": grant_id,
        "scope_type": args.scope_type, "scope": args.scope_id,
        "expires_at": expires_at, "reason": args.reason or "",
    })
    print(f"grant {grant_id} actor={args.actor_id} "
          f"scope={args.scope_type}:{args.scope_id} "
          f"expires_at={expires_at or '-'}")
    return EXIT_OK


def cmd_grant_revoke(store, access, args) -> int:
    require_owner(access, args.by, "grant revoke")
    access.revoke_grant(args.grant_id, by=args.by, reason=args.reason or "")
    ledger_event(store, access, args.by, "grant_revoke",
                 {"grant": args.grant_id, "reason": args.reason or ""})
    print(f"grant {args.grant_id} revoked")
    return EXIT_OK


def cmd_grant_list(store, access, args) -> int:
    if args.actor:
        if access.actor_attrs(args.actor) is None:
            raise KeyError(f"actor not found: {args.actor}")
        actor_ids = [args.actor]
    else:
        actor_ids = [r["id"] for r in store.conn.execute(
            "SELECT id FROM actors ORDER BY id")]
    for aid in actor_ids:
        for g in access.live_grants(aid):
            print(f"grant {g['id']} actor={g['actor_id']} "
                  f"scope={g['scope_type']}:{g['scope_id']} "
                  f"verbs={','.join(g['verbs'])} max_label={g['max_label']} "
                  f"expires_at={g['expires_at'] or '-'} "
                  f"granted_by={g['granted_by']}")
    return EXIT_OK


def _label_target_exists(store, access, kind: str, target: str) -> bool:
    if kind == "ticket":
        return store.get_ticket(target) is not None
    # wiki: the Store's public page API answers existence (any revision)
    return store.get_page(target) is not None


def cmd_label(store, access, args) -> int:
    require_owner(access, args.by, f"label {args.target_kind}")
    check_label(args.label)  # ValueError -> exit 3 (label_unsupported)
    if not _label_target_exists(store, access, args.target_kind, args.id):
        raise KeyError(
            f"{'ticket' if args.target_kind == 'ticket' else 'wiki page'} "
            f"not found: {args.id}")
    # Load the acting actor from the database; constructing Actor(id=by,
    # name=by) would make ensure_actor overwrite the owner's stored row
    # (kind human -> agent default), enabling act_as relay impersonation.
    actor = store.get_actor(args.by)
    if actor is None:
        raise KeyError(f"actor not found: {args.by}")
    old_label = None
    if args.target_kind == "ticket":
        old_label = store.get_ticket(args.id).label
        store.set_ticket_label(args.id, args.label, actor=actor,
                               reason=args.reason or "")
    else:
        # Wiki label change = new revision with same title/body/tags and the
        # new label (contract §3; mirrors POST /wiki/{slug}/label).
        page = store.get_page(args.id)
        old_label = page.label
        if page.label != args.label:
            store.put_page(page.slug, page.title, body_md=page.body_md,
                           tags=list(page.tags), actor=args.by,
                           label=args.label)
    ledger_event(store, access, args.by, f"label_{args.target_kind}", {
        "target": args.id, "from": old_label, "to": args.label,
        "reason": args.reason or "",
    })
    print(f"{args.target_kind} {args.id} label -> {args.label}")
    return EXIT_OK


# ---------------------------------------------------------------------------
# argument parsing
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hub_admin.py",
        description="ticket-hub access administration "
                    "(operates on $TICKET_HUB_DB)")
    sub = parser.add_subparsers(dest="group", required=True)

    # actor ---------------------------------------------------------------
    actor = sub.add_parser("actor", help="actor management")
    asub = actor.add_subparsers(dest="cmd", required=True)

    aadd = asub.add_parser("add", help="add an actor")
    aadd.add_argument("id")
    aadd.add_argument("--name", required=True)
    aadd.add_argument("--family", default="unknown")
    aadd.add_argument("--kind", default="agent",
                      choices=["agent", "human", "system"])
    aadd.add_argument("--realm", default="internal",
                      choices=["internal", "external"])
    aadd.add_argument("--sponsor", default=None)
    aadd.add_argument("--expires", default=None,
                      help="30d / 12h / ISO-8601 UTC")
    aadd.add_argument("--owner", action="store_true")
    aadd.add_argument("--by", required=True)

    for status_cmd in ("suspend", "activate"):
        sp = asub.add_parser(status_cmd, help=f"set actor status: {status_cmd}")
        sp.add_argument("id")
        sp.add_argument("--by", required=True)
        sp.add_argument("--reason", default="")

    aadopt = asub.add_parser(
        "adopt", help="adopt a pre-access-control actor row")
    aadopt.add_argument("id")
    aadopt.add_argument("--kind", default=None,
                        choices=["agent", "human", "system"])
    aadopt.add_argument("--family", default=None)
    aadopt.add_argument("--owner", action="store_true")
    aadopt.add_argument("--by", required=True)

    alegacy = asub.add_parser(
        "adopt-legacy", help="activate every actor with no status row")
    alegacy.add_argument("--by", required=True)

    asub.add_parser("list", help="list actors")

    # token ---------------------------------------------------------------
    token = sub.add_parser("token", help="credential management")
    tsub = token.add_subparsers(dest="cmd", required=True)

    tadd = tsub.add_parser("issue", help="issue a bearer token to FILE")
    tadd.add_argument("actor_id")
    tadd.add_argument("--out", required=True,
                      help="file to create with mode 0600 (never overwritten)")
    tadd.add_argument("--expires", default=None, help="30d / 12h / ISO-8601 UTC")
    tadd.add_argument("--scopes", action="append", default=[],
                      help="repeatable; e.g. --scopes act_as")
    tadd.add_argument("--act-as", dest="act_as", action="append", default=[],
                      help="repeatable fnmatch glob over actor ids")
    tadd.add_argument("--by", required=True)

    trev = tsub.add_parser("revoke", help="revoke a credential")
    trev.add_argument("credential_id")
    trev.add_argument("--by", required=True)
    trev.add_argument("--reason", default="")

    tsub.add_parser("list", help="list credentials (never prints hashes)")

    # grant ---------------------------------------------------------------
    grant = sub.add_parser("grant", help="read-grant management")
    gsub = grant.add_subparsers(dest="cmd", required=True)

    gadd = gsub.add_parser("add", help="add a read grant")
    gadd.add_argument("actor_id")
    gadd.add_argument("--scope-type", dest="scope_type", required=True,
                      choices=["ticket", "subtree", "wiki_page"])
    gadd.add_argument("--scope", dest="scope_id", required=True)
    gadd.add_argument("--expires", default=None, help="90d / 12h / ISO-8601 UTC")
    gadd.add_argument("--by", required=True)
    gadd.add_argument("--reason", default="")

    grev = gsub.add_parser("revoke", help="revoke a grant")
    grev.add_argument("grant_id")
    grev.add_argument("--by", required=True)
    grev.add_argument("--reason", default="")

    gls = gsub.add_parser("list", help="list live grants")
    gls.add_argument("--actor", default=None)

    # label ---------------------------------------------------------------
    label = sub.add_parser("label", help="label changes (owner only)")
    lsub = label.add_subparsers(dest="cmd", required=True)
    for kind, target in (("ticket", "ticket id"), ("wiki", "wiki slug")):
        lp = lsub.add_parser(kind, help=f"set the label of a {target}")
        lp.add_argument("id", help=target)
        lp.add_argument("label", help="public | external | internal")
        lp.add_argument("--by", required=True)
        lp.add_argument("--reason", default="")
        lp.set_defaults(target_kind=kind)

    return parser


_HANDLERS = {
    ("actor", "add"): cmd_actor_add,
    ("actor", "suspend"): cmd_actor_suspend,
    ("actor", "activate"): cmd_actor_activate,
    ("actor", "adopt"): cmd_actor_adopt,
    ("actor", "adopt-legacy"): cmd_actor_adopt_legacy,
    ("actor", "list"): cmd_actor_list,
    ("token", "issue"): cmd_token_issue,
    ("token", "revoke"): cmd_token_revoke,
    ("token", "list"): cmd_token_list,
    ("grant", "add"): cmd_grant_add,
    ("grant", "revoke"): cmd_grant_revoke,
    ("grant", "list"): cmd_grant_list,
    ("label", "ticket"): cmd_label,
    ("label", "wiki"): cmd_label,
}


def main(argv=None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        # G1: --help raises SystemExit(0) and must exit 0; only non-zero
        # SystemExit codes (argparse usage errors) map to EXIT_USAGE.
        code = exc.code if isinstance(exc.code, int) else EXIT_USAGE
        return EXIT_OK if code == 0 else EXIT_USAGE

    db_path = os.environ.get("TICKET_HUB_DB")
    if not db_path:
        print("hub_admin: TICKET_HUB_DB is not set; export it as the path to "
              "the hub database (e.g. ~/.ticket-hub/hub.db)", file=sys.stderr)
        return EXIT_USAGE

    handler = _HANDLERS[(args.group, args.cmd)]
    store = Store(db_path)
    try:
        access = AccessStore(store.conn)
        try:
            return handler(store, access, args)
        except UsageError as exc:
            print(f"hub_admin: {exc}", file=sys.stderr)
            return EXIT_USAGE
        except (PermissionError, ValueError) as exc:
            print(f"hub_admin: refused: {exc}", file=sys.stderr)
            return EXIT_REFUSED
        except KeyError as exc:
            # KeyError('...') repr would quote; unwrap the message.
            message = exc.args[0] if exc.args else "not found"
            print(f"hub_admin: not found: {message}", file=sys.stderr)
            return EXIT_NOT_FOUND
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
