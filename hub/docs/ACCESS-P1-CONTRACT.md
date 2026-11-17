# Access P1: frozen build contract

Implements P1 of `DESIGN-access.md` (accepted r3). Lanes build against these signatures;
deviations are reported, never silent. Section refs (§) point at DESIGN-access.md.

## 0. Scope decisions for P1 (narrowing the design, not changing it)

- **Labels allowed in P1: `public`, `external`, `internal`** (order public < external < internal).
  `restricted` and `vault:*` are P4: writing them returns 400 `label_unsupported`. So every
  internal viewer sees everything, exactly as today, and **only external reads are filtered**.
- External-readable routes (P1): `GET /tickets`, `GET /tickets/{ticket_id}`,
  `GET /tickets/{ticket_id}/events`, `GET /wiki`, `GET /wiki/{slug}`, `GET /wiki-search`.
  Every other route is internal (§0 table below).
- Artifacts: `ExternalTicket` has **no artifacts field** in P1 (AT-1.6b); per-artifact labels
  land with the P2 reference check.
- Admin (D2): only actors flagged `is_owner` may publish (create below `internal`, lower a label),
  grant, issue tokens. Sponsors are not built in P1.
- Browser UI: a minimal cookie session (`GET /login`, `POST /session`, `POST /logout`) is pulled
  forward from P2 so the UI keeps working after cut-over.
- Auth switch: `create_app(store=None, *, auth=None)`. `auth=None` reads env
  `TICKET_HUB_AUTH`; **default `required`**. Only the literal `off` disables auth (legacy tests set
  it in `tests/conftest.py`). Production never sets it.

## 1. Store changes (`ticket_hub/store.py`) — lane W1

Migration (idempotent, run in `Store.__init__` after `_SCHEMA`; use `PRAGMA table_info` to add
missing columns; never rewrite rows):

```
ALTER TABLE tickets    ADD COLUMN label TEXT NOT NULL DEFAULT 'internal';
ALTER TABLE events     ADD COLUMN label TEXT NOT NULL DEFAULT 'internal';
ALTER TABLE wiki_pages ADD COLUMN label TEXT NOT NULL DEFAULT 'internal';
ALTER TABLE actors     ADD COLUMN realm TEXT NOT NULL DEFAULT 'internal';
ALTER TABLE actors     ADD COLUMN clearance TEXT NOT NULL DEFAULT 'internal';
ALTER TABLE actors     ADD COLUMN sponsor_id TEXT;
ALTER TABLE actors     ADD COLUMN expires_at TEXT;
ALTER TABLE actors     ADD COLUMN is_owner INTEGER NOT NULL DEFAULT 0;
```

The events and wiki_pages append-only triggers stay exactly as they are (ADD COLUMN does not
fire them). Existing rows read back as `internal` (AT-1.5).

Module constants in `ticket_hub/model.py`:
```python
LABELS = ("public", "external", "internal")            # P1 order, low -> high
LABEL_RANK = {"public": 0, "external": 1, "internal": 2}
def check_label(label: str) -> str   # returns label; ValueError("label_unsupported: ...") otherwise
def max_label(a: str, b: str) -> str
```

`Ticket`, `Event`, `WikiPage` dataclasses gain `label: str = "internal"`; `to_dict()` includes it.

Signature changes (all new params keyword-only, defaults keep today's behaviour):
```python
Store.create_ticket(..., label: str = "internal", via: str | None = None) -> Ticket
    # label checked; children do NOT inherit the parent's label
Store.append_event(ticket_id, kind, actor=None, payload=None, *,
                   label: str | None = None, via: str | None = None) -> Event
    # label None -> the ticket's label AT INSERT, stored on the row (never derived at read)
Store.update_ticket(..., via: str | None = None)          # existing fields unchanged
Store.put_page(slug, title, body_md="", tags=None, actor="", *, label: str | None = None) -> WikiPage
    # label None -> label of the current latest revision, else "internal"
Store.set_ticket_label(ticket_id, label, *, actor: Actor, reason: str = "",
                       via: str | None = None) -> Ticket
    # UPDATE tickets.label + append event kind "label_changed" with label "internal",
    # payload {"from": old, "to": new, "reason": reason}; no-op (no event) if unchanged
```
`via`: when set, the event's `actor_json` is `{**actor.to_dict(), "via": via}`; `Event.to_dict()`
exposes `"via"` when present. Internal callers that pass nothing see no change.

`"label_changed"` is added to the api's `EVENT_KINDS`.

Tests: `tests/test_store_labels.py`. Full existing suite must still pass.

## 2. Access core (`ticket_hub/access.py`) — lane W2

Pure stdlib; operates on the Store's sqlite3 connection (`AccessStore(conn)`); creates its tables
idempotently in `__init__`.

```
credentials (id TEXT PK, actor_id TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE,
             prefix TEXT NOT NULL, scopes TEXT NOT NULL DEFAULT '[]',
             act_as_allow TEXT NOT NULL DEFAULT '[]', created_by TEXT NOT NULL,
             created_at TEXT NOT NULL, expires_at TEXT)
credential_revocations (id TEXT PK, credential_id TEXT NOT NULL, actor TEXT NOT NULL,
             reason TEXT NOT NULL DEFAULT '', ts TEXT NOT NULL)
grants (id TEXT PK, actor_id TEXT NOT NULL, scope_type TEXT NOT NULL, scope_id TEXT NOT NULL,
        verbs TEXT NOT NULL, max_label TEXT NOT NULL, granted_by TEXT NOT NULL,
        reason TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, expires_at TEXT)
grant_revocations (id TEXT PK, grant_id TEXT NOT NULL, actor TEXT NOT NULL,
        reason TEXT NOT NULL DEFAULT '', ts TEXT NOT NULL)
actor_status (id TEXT PK, actor_id TEXT NOT NULL, status TEXT NOT NULL,   -- active|suspended|revoked
        actor TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '', ts TEXT NOT NULL)
access_log (seq INTEGER PK AUTOINCREMENT, ts TEXT NOT NULL, actor_id TEXT, credential_id TEXT,
        method TEXT NOT NULL, route TEXT NOT NULL, status INTEGER NOT NULL, decision TEXT NOT NULL)
```
Every table above gets `BEFORE UPDATE` and `BEFORE DELETE` triggers that `RAISE(ABORT, ...)`.

Tokens: `thk_` + `secrets.token_urlsafe(32)`; stored as `sha256(token).hexdigest()`; `prefix` =
first 12 chars of the token. Compare by hash lookup only. The token string is returned once by
`issue_credential` and never stored, logged or included in any exception message.

```python
@dataclass(frozen=True)
class Viewer:
    actor: Actor                 # id, name, family, kind
    realm: str                   # internal | external
    clearance: str
    is_owner: bool
    credential_id: str | None
    scopes: tuple[str, ...]
    act_as_allow: tuple[str, ...]
    via: str | None = None       # set when acting through a relay (see effective())

class AccessStore:
    def __init__(self, conn: sqlite3.Connection) -> None
    # actors
    def create_actor(self, id, name, *, family="unknown", kind="agent", realm="internal",
                     sponsor_id=None, expires_at=None, is_owner=False, by: str) -> Actor
        # realm external: sponsor_id and expires_at REQUIRED, clearance forced "external",
        # is_owner forbidden (ValueError). kind human+internal allowed. Writes actor_status active.
    def set_actor_status(self, actor_id, status, *, by: str, reason="") -> None
    def actor_attrs(self, actor_id) -> dict | None   # realm, clearance, sponsor_id, expires_at,
                                                     # is_owner, status (latest log row)
    def actor_live(self, actor_id, now=None) -> bool # exists, status active, not expired
    # credentials
    def issue_credential(self, actor_id, *, by: str, expires_at=None, scopes=(),
                         act_as_allow=()) -> tuple[str, str]       # (credential_id, token)
        # act_as_allow requires "act_as" in scopes; patterns are fnmatch globs over actor ids;
        # ValueError if any pattern could match an internal human (checked again at use time)
    def revoke_credential(self, credential_id, *, by: str, reason="") -> None
    def list_credentials(self) -> list[dict]   # id, actor_id, prefix, scopes, expires_at,
                                               # revoked (bool), last_used_at (from access_log)
    def viewer_for_token(self, token: str, now=None) -> Viewer | None
        # None unless credential exists, unrevoked, unexpired, and actor_live
    # grants
    def add_grant(self, actor_id, scope_type, scope_id, *, verbs=("read",), by: str,
                  reason="", expires_at=None) -> str
        # scope_type in {"ticket","subtree","wiki_page"}; verbs subset of {"read"} in P1;
        # external actors: max_label "external". ValueError otherwise.
    def revoke_grant(self, grant_id, *, by: str, reason="") -> None
    def live_grants(self, actor_id, now=None) -> list[dict]
    # delegation
    def effective(self, viewer: Viewer, claimed_actor_id: str | None) -> Viewer
        # None or == viewer.actor.id -> viewer. Otherwise requires "act_as" scope and a matching
        # act_as_allow pattern, target actor_live, target NOT (realm internal and kind human);
        # returns the TARGET's Viewer (its realm/clearance/is_owner) with via=viewer.actor.id.
        # Raises PermissionError otherwise.
    # audit
    def log_access(self, *, actor_id, credential_id, method, route, status, decision) -> None

class ExternalReader:
    """The ONLY read path for realm=external viewers. All filtering in SQL WHERE."""
    def __init__(self, conn, viewer: Viewer, now=None)
    def list_tickets(self, *, status=None, tag=None, q=None) -> list[dict]   # ExternalTicket
    def get_ticket(self, ticket_id) -> dict | None                           # ExternalTicket
    def ticket_visible(self, ticket_id) -> bool
    def events(self, ticket_id) -> list[dict] | None    # None if ticket not visible
    def list_pages(self) -> list[dict]
    def get_page(self, slug) -> dict | None
    def page_history(self, slug) -> list[dict] | None
    def page_visible(self, slug) -> bool
    def search_pages(self, q) -> list[dict]
```

Visibility (external viewer E, `now` from the caller):
- ticket T visible ⇔ `T.label IN ('public','external')` AND E has a live `read` grant with
  (`scope_type='ticket' AND scope_id=T.id`) OR (`scope_type='subtree'` AND scope_id is T or an
  ancestor of T via `parent_of` edges; recursive CTE).
- event visible ⇔ its ticket visible AND `event.label IN ('public','external')`.
- wiki revision R of slug S visible ⇔ live `read` grant `scope_type='wiki_page' AND scope_id=S`
  AND current (max rev) label of S ∈ {public, external} AND `R.label` ∈ {public, external}.
  `get_page` returns the latest *visible* revision; `list_pages`/`search_pages` only slugs whose
  current revision is visible, matching on that revision.
- live grant: `expires_at IS NULL OR expires_at > now`, and no `grant_revocations` row.

Projections:
- `ExternalTicket` = `{id, title, body, status, priority, label, created_at, updated_at}`.
  Nothing else (no owner, tags, families, external_refs, artifacts, edges).
- `ExternalEvent` = `{id, ticket_id, ts, kind, payload, label, actor: {"name": N}}` where N is the
  actor's name if that actor is `realm=external`, else `"Team"`.
- `ExternalPage` = `{slug, rev, title, body_md, label, ts}`.

Tests: `tests/test_access_core.py`.

## 3. Route policy and auth (`ticket_hub/policy.py`, `api.py`) — lane W3

```python
POLICIES: dict[tuple[str, str], str]   # (METHOD, route.path) -> policy
# policies: "public" | "authenticated" | "external_read" | "internal" |
#           "internal_write_ticket" | "internal_write_wiki" | "internal_write"
def check_policies(app) -> None   # raise RuntimeError naming every APIRoute/Route/Mount
                                  # (method, path) that has no policy
```
- public: `GET /health` (returns only `{"ok": bool}` when auth is on), `GET /login`,
  `POST /session`.
- authenticated: `GET /`, `/ui` mount, `GET /openapi.json`, `GET /docs`,
  `GET /docs/oauth2-redirect`, `GET /redoc`, `POST /logout`.
- external_read: the six routes in §0.
- internal_write_ticket: every mutating route with `{ticket_id}`.
- internal_write_wiki: `POST /wiki/{slug}`, `POST /wiki/{slug}/label`.
- internal_write: `POST /tickets`, `POST /adapters/{platform}/inbound`, `/jobs*` mutations,
  `POST /templates/{name}/instantiate`.
- internal: everything else (all remaining GETs: board, lineage, digest, plan, bindings, seats,
  roles, templates, jobs, admin/status, assignment).

Request pipeline (auth on), implemented as one HTTP middleware plus helpers:
1. Find the matching route (`route.matches(scope)`); look up its policy (no match → pass through
   to normal 404).
2. `public` → no auth. Otherwise resolve the token: `Authorization: Bearer <t>`, else cookie
   `hub_session`. No/invalid token → **401** `{"error":{"code":"unauthorized",...}}`.
   Cookie-authenticated mutating requests (POST/PATCH/PUT/DELETE) require an `Origin` (or
   `Referer`) whose host:port equals the request `Host`; otherwise 403 `csrf`.
3. Put `request.state.viewer` (a `Viewer`). External viewer:
   - `internal` → **404** not_found (same body as a missing object).
   - `internal_write_ticket` → 403 if `ExternalReader.ticket_visible(ticket_id)`, else 404.
   - `internal_write_wiki` → 403 if `page_visible(slug)`, else 404.
   - `internal_write` → 403.
   - `external_read` → handler serves from `ExternalReader` only; missing/hidden → 404.
4. Log one `access_log` row per authenticated request (status, decision allow|deny|hidden).

Delegation header (amendment A1, from the acceptance suite's AT-1.4e): a request may carry
`X-Hub-Act-As: <actor-id>`. Right after resolving the token's Viewer (step 2), the pipeline calls
`AccessStore.effective(viewer, header_value)`; PermissionError → 403 `actor_mismatch`. The returned
effective Viewer (with `via` = the relay) replaces `request.state.viewer` for EVERYTHING that
follows: external handling in step 3, ExternalReader on external_read routes, publication authority,
and handlers. A relay acting for an external actor therefore reads exactly what that actor reads
and cannot write. A body `actor` that differs from the effective actor → 403 `actor_mismatch`
(after the visibility check). The audit row records the effective actor and the credential.

Identity in handlers (auth on): every body `actor`/`owner`-carrying route calls
`acting(request, body_actor) -> (Actor, via)`:
- visibility/404 checks run BEFORE this (step 3), so a mismatch on a hidden object is a 404;
- body actor absent or equal to the viewer → viewer's actor;
- otherwise `AccessStore.effective(...)`; PermissionError → 403 `actor_mismatch`;
- the effective viewer must be `realm=internal` to write, else 403.
`owner` on `POST /tickets` is an assignment: absent → acting actor; present → must be an existing
internal actor id (no auto-create) else 400. Stored actors are always the DB-loaded `Actor`.

Label endpoints and publication authority (owner = `viewer.is_owner`):
- `POST /tickets` and `POST /wiki/{slug}` accept optional `label`; a label below `internal`
  requires owner, else 403 `publish_forbidden`.
- `POST /tickets/{ticket_id}/events` accepts optional `label` (any P1 label; effective label is
  max(ticket, event) by construction).
- `POST /tickets/{ticket_id}/label` and `POST /wiki/{slug}/label` body `{label, reason}`:
  lowering requires owner (403 `publish_forbidden` otherwise); raising allowed for any internal
  writer. Wiki label change = new revision with the same title/body/tags and the new label.

Session: `GET /login` serves a static form (no JS framework); `POST /session` body
`{"token": "..."}` → 204 + `Set-Cookie: hub_session=<token>; HttpOnly; SameSite=Strict; Path=/`
(+ `Secure` when the request scheme is https or `X-Forwarded-Proto: https`); invalid token → 401.
`POST /logout` clears it. The cookie is re-validated on every request (expiry/revocation apply).

`app.state.access` is created lazily as `AccessStore(store.conn)`; `StoreGateway` gains a
`conn` property returning the underlying connection. Tests may pre-set `app.state.access`.

When auth is off, behaviour is exactly today's (legacy tests unchanged).

Tests: `tests/test_api_access.py` (pipeline, policies, identity, labels, session).

## 4. Admin CLI and clients — lane W4

`scripts/hub_admin.py` (stdlib + ticket_hub; operates on `$TICKET_HUB_DB`), every mutating command
requires `--by <owner-actor-id>` (must be `is_owner`, except `actor add --owner` bootstrap when no
owner exists yet) and records an event on the `access-ledger` ticket (created on first use, label
internal, tag `access-ledger`):
```
actor add ID --name N [--family F] [--kind agent|human|system] [--realm internal|external]
          [--sponsor ID] [--expires 30d|ISO] [--owner] --by ID
actor suspend|activate ID --by ID [--reason R]        actor list
token issue ACTOR --out FILE [--expires 30d|ISO] [--act-as GLOB ...] --by ID
      # writes the token to FILE (created 0600, refuses to overwrite), prints only id + prefix
token revoke CRED_ID --by ID [--reason R]              token list
grant add ACTOR --scope-type ticket|subtree|wiki_page --scope ID [--expires 90d|ISO] --by ID [--reason R]
grant revoke GRANT_ID --by ID [--reason R]             grant list [--actor ID]
label ticket ID LABEL --by ID [--reason R]             label wiki SLUG LABEL --by ID [--reason R]
```
Exit codes: 0 ok, 2 usage, 3 refused (not owner / validation), 4 not found.

Amendment A2 (found rehearsing the cut-over on a copy of a real database): existing actor rows have no
`actor_status` row, so they are not live, and `actor add` refuses existing ids. Added
`actor adopt ID [--kind K] [--family F] [--owner] --by ID` and `actor adopt-legacy --by ID`
(`AccessStore.adopt_actor`, `adopt_legacy`). `adopt --owner` follows the same bootstrap exception as
`actor add --owner`: any `--by` value, only while no owner exists. Neither re-activates a suspended
or revoked actor, and neither changes realm, clearance, sponsor or expiry.

`token issue` takes `--scopes act_as` explicitly alongside `--act-as GLOB`.

Clients send `Authorization: Bearer <token>` read **at call time** from the file named by
`HUB_TOKEN_FILE` (absent var → no header; unreadable file → clear error naming the variable, not
the contents): `hub_client.HubClient._request`, `outbound.HubClient` (both urlopen sites), and
`scripts/seat_remote.py` if it calls the hub. Never log or echo the token.

Tests: `tests/test_hub_admin.py`, additions to `tests/test_client.py` / `tests/test_outbound.py`.

## 5. Acceptance suite — lane W5

`tests/test_access_p1.py` implements AT-1.1 … AT-1.14 from DESIGN-access.md §3 P1 against a real
`Store` on a temp file, `AccessStore`, and `create_app(store, auth=True)` via TestClient. AT-1.12
and AT-1.14 (live/deployment) are marked `@pytest.mark.live` and skipped by default.
Written independently of lanes W2-W4 (it sees only this contract and the design).
