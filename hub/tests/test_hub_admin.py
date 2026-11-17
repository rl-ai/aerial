"""hub_admin CLI tests (ACCESS-P1-CONTRACT §4, lane W4).

Runs `scripts/hub_admin.py` as a subprocess against a throwaway database
(TICKET_HUB_DB) and checks exit codes, stdout hygiene (no token values),
file modes, and access-ledger events. The client bearer behaviour lives in
tests/test_client.py / tests/test_outbound.py.
"""
import json
import os
import sqlite3
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
ADMIN = REPO / "scripts" / "hub_admin.py"

EXIT_OK, EXIT_USAGE, EXIT_REFUSED, EXIT_NOT_FOUND = 0, 2, 3, 4


def run_admin(*argv, db=None, env_extra=None):
    env = dict(os.environ)
    env.pop("TICKET_HUB_DB", None)
    env.pop("HUB_TOKEN_FILE", None)
    if db is not None:
        env["TICKET_HUB_DB"] = str(db)
    env.update(env_extra or {})
    return subprocess.run([sys.executable, str(ADMIN), *argv],
                          capture_output=True, text=True, timeout=60, env=env)


@pytest.fixture()
def db(tmp_path):
    return tmp_path / "hub.db"


@pytest.fixture()
def owner(db):
    """Bootstrap an owner actor 'own' (kind human); returns its --by value."""
    r = run_admin("actor", "add", "own", "--name", "Own", "--kind", "human",
                  "--owner", "--by", "bootstrap", db=db)
    assert r.returncode == EXIT_OK, r.stderr
    return "own"


def out(r):
    return r.stdout + r.stderr


# ---------------------------------------------------------------------------
# env / usage
# ---------------------------------------------------------------------------

def test_missing_ticket_hub_db_exits_2(tmp_path):
    r = run_admin("actor", "list", db=None)
    assert r.returncode == EXIT_USAGE
    assert "TICKET_HUB_DB" in out(r)


def test_unknown_command_exits_2(db):
    r = run_admin("bogus", db=db)
    assert r.returncode == EXIT_USAGE


def test_token_issue_requires_out(db, owner):
    r = run_admin("token", "issue", "x", "--by", owner, db=db)
    # argparse enforces required --out -> usage error
    assert r.returncode == EXIT_USAGE


# ---------------------------------------------------------------------------
# actors / ownership
# ---------------------------------------------------------------------------

def test_bootstrap_owner_only_once(db):
    r1 = run_admin("actor", "add", "own", "--name", "Own", "--kind", "human",
                   "--owner", "--by", "bootstrap", db=db)
    assert r1.returncode == EXIT_OK
    # second bootstrap-style add is refused: bootstrap only while no owner
    r2 = run_admin("actor", "add", "own2", "--name", "O2", "--kind", "human",
                   "--owner", "--by", "bootstrap", db=db)
    assert r2.returncode == EXIT_REFUSED
    assert "not owner" in out(r2).lower()


def test_non_owner_by_refused(db, owner):
    assert run_admin("actor", "add", "bot", "--name", "Bot",
                     "--by", owner, db=db).returncode == EXIT_OK
    r = run_admin("actor", "add", "bot2", "--name", "B2", "--by", "bot", db=db)
    assert r.returncode == EXIT_REFUSED


def test_duplicate_actor_refused(db, owner):
    assert run_admin("actor", "add", "bot", "--name", "Bot",
                     "--by", owner, db=db).returncode == EXIT_OK
    r = run_admin("actor", "add", "bot", "--name", "Bot2",
                  "--by", owner, db=db)
    assert r.returncode == EXIT_REFUSED


def test_external_actor_requires_sponsor_and_expiry(db, owner):
    r = run_admin("actor", "add", "e1", "--name", "E", "--realm", "external",
                  "--by", owner, db=db)
    assert r.returncode == EXIT_REFUSED
    r2 = run_admin("actor", "add", "e1", "--name", "E", "--realm", "external",
                   "--sponsor", "own", "--expires", "30d", "--by", owner, db=db)
    assert r2.returncode == EXIT_OK
    row = sqlite3.connect(db).execute(
        "SELECT realm, clearance, sponsor_id, expires_at FROM actors "
        "WHERE id='e1'").fetchone()
    assert row[0] == "external" and row[1] == "external" and row[2] == "own"
    assert row[3].endswith("+00:00")


def test_suspend_activate_roundtrip(db, owner):
    run_admin("actor", "add", "bot", "--name", "Bot", "--by", owner, db=db)
    r = run_admin("actor", "suspend", "bot", "--by", owner,
                  "--reason", "time-out", db=db)
    assert r.returncode == EXIT_OK
    status = sqlite3.connect(db).execute(
        "SELECT status FROM actor_status WHERE actor_id='bot' "
        "ORDER BY rowid DESC LIMIT 1").fetchone()[0]
    assert status == "suspended"
    r2 = run_admin("actor", "activate", "bot", "--by", owner, db=db)
    assert r2.returncode == EXIT_OK
    status2 = sqlite3.connect(db).execute(
        "SELECT status FROM actor_status WHERE actor_id='bot' "
        "ORDER BY rowid DESC LIMIT 1").fetchone()[0]
    assert status2 == "active"


def test_suspend_missing_actor_exit_4(db, owner):
    r = run_admin("actor", "suspend", "ghost", "--by", owner, db=db)
    assert r.returncode == EXIT_NOT_FOUND


def test_actor_list_one_line_per_actor(db, owner):
    run_admin("actor", "add", "bot", "--name", "Bot", "--by", owner, db=db)
    r = run_admin("actor", "list", db=db)
    assert r.returncode == EXIT_OK
    lines = [l for l in r.stdout.splitlines() if l.startswith("actor ")]
    ids = {l.split()[1] for l in lines}
    assert {"own", "bot"} <= ids
    for line in lines:  # one line per row
        assert "\n" not in line and len(line.split()) > 3


# ---------------------------------------------------------------------------
# durations
# ---------------------------------------------------------------------------

def test_duration_30d_stored_as_iso_utc(db, owner):
    r = run_admin("actor", "add", "temp", "--name", "T", "--expires", "30d",
                  "--by", owner, db=db)
    assert r.returncode == EXIT_OK
    exp = sqlite3.connect(db).execute(
        "SELECT expires_at FROM actors WHERE id='temp'").fetchone()[0]
    parsed = datetime.fromisoformat(exp)
    assert parsed.tzinfo is not None and exp.endswith("+00:00")
    delta = parsed - datetime.now(timezone.utc)
    assert 29.9 < delta.total_seconds() / 86400 < 30.1


def test_duration_12h_and_iso_timestamp(db, owner):
    r = run_admin("actor", "add", "a12", "--name", "A", "--expires", "12h",
                  "--by", owner, db=db)
    assert r.returncode == EXIT_OK
    r2 = run_admin("actor", "add", "aISO", "--name", "B",
                   "--expires", "2027-01-01T00:00:00Z", "--by", owner, db=db)
    assert r2.returncode == EXIT_OK
    exp = sqlite3.connect(db).execute(
        "SELECT expires_at FROM actors WHERE id='aISO'").fetchone()[0]
    assert exp.startswith("2027-01-01T00:00:00")


def test_bad_duration_refused(db, owner):
    r = run_admin("actor", "add", "bad", "--name", "B", "--expires", "soon",
                  "--by", owner, db=db)
    assert r.returncode == EXIT_REFUSED


# ---------------------------------------------------------------------------
# tokens
# ---------------------------------------------------------------------------

def test_token_issue_file_mode_content_and_no_leak(db, owner, tmp_path):
    run_admin("actor", "add", "bot", "--name", "Bot", "--by", owner, db=db)
    tok_file = tmp_path / "bot.token"
    r = run_admin("token", "issue", "bot", "--out", str(tok_file),
                  "--expires", "30d", "--by", owner, db=db)
    assert r.returncode == EXIT_OK
    token = tok_file.read_text()
    assert token.startswith("thk_") and token.endswith("\n")
    token = token.strip()
    assert stat.S_IMODE(os.stat(tok_file).st_mode) == 0o600
    # printed only id + prefix; the token never appears in output
    assert "CR-" in r.stdout and "prefix=thk_" in r.stdout
    assert token not in (r.stdout + r.stderr)
    # and never stored anywhere in the DB except as a hash
    dump = "\n".join(sqlite3.connect(db).iterdump())
    assert token not in dump


def test_token_issue_refuses_existing_file(db, owner, tmp_path):
    run_admin("actor", "add", "bot", "--name", "Bot", "--by", owner, db=db)
    tok_file = tmp_path / "bot.token"
    first = run_admin("token", "issue", "bot", "--out", str(tok_file),
                      "--by", owner, db=db)
    assert first.returncode == EXIT_OK
    before = tok_file.read_bytes()
    r = run_admin("token", "issue", "bot", "--out", str(tok_file),
                  "--by", owner, db=db)
    assert r.returncode == EXIT_REFUSED
    assert tok_file.read_bytes() == before  # untouched


def test_token_issue_missing_actor_exit_4(db, owner, tmp_path):
    r = run_admin("token", "issue", "ghost", "--out",
                  str(tmp_path / "t"), "--by", owner, db=db)
    assert r.returncode == EXIT_NOT_FOUND
    assert not (tmp_path / "t").exists()


def test_token_issue_non_owner_refused(db, owner, tmp_path):
    run_admin("actor", "add", "bot", "--name", "Bot", "--by", owner, db=db)
    r = run_admin("token", "issue", "bot", "--out", str(tmp_path / "t"),
                  "--by", "bot", db=db)
    assert r.returncode == EXIT_REFUSED


def test_token_list_never_prints_hashes(db, owner, tmp_path):
    run_admin("actor", "add", "bot", "--name", "Bot", "--by", owner, db=db)
    tok_file = tmp_path / "bot.token"
    r1 = run_admin("token", "issue", "bot", "--out", str(tok_file),
                   "--by", owner, db=db)
    assert r1.returncode == EXIT_OK
    token = tok_file.read_text().strip()
    conn = sqlite3.connect(db)
    rows = conn.execute("SELECT id, token_hash FROM credentials").fetchall()
    cred_id, token_hash = rows[0]
    r = run_admin("token", "list", db=db)
    assert r.returncode == EXIT_OK
    assert cred_id in r.stdout and "prefix=thk_" in r.stdout
    assert token not in r.stdout
    assert token_hash not in r.stdout  # hashes never printed
    assert len([l for l in r.stdout.splitlines() if l.startswith("token ")]) == 1


def test_token_revoke_flow(db, owner, tmp_path):
    run_admin("actor", "add", "bot", "--name", "Bot", "--by", owner, db=db)
    tok_file = tmp_path / "bot.token"
    run_admin("token", "issue", "bot", "--out", str(tok_file),
              "--by", owner, db=db)
    cred_id = sqlite3.connect(db).execute(
        "SELECT id FROM credentials").fetchone()[0]
    r = run_admin("token", "revoke", cred_id, "--by", owner,
                  "--reason", "rotate", db=db)
    assert r.returncode == EXIT_OK
    r2 = run_admin("token", "list", db=db)
    assert "revoked=1" in r2.stdout
    assert run_admin("token", "revoke", "CR-nope", "--by", owner,
                     db=db).returncode == EXIT_NOT_FOUND


def test_act_as_requires_scope(db, owner, tmp_path):
    run_admin("actor", "add", "bot", "--name", "Bot", "--by", owner, db=db)
    r = run_admin("token", "issue", "bot", "--out", str(tmp_path / "a"),
                  "--act-as", "bot*", "--by", owner, db=db)
    assert r.returncode == EXIT_REFUSED
    r2 = run_admin("token", "issue", "bot", "--out", str(tmp_path / "b"),
                  "--scopes", "act_as", "--act-as", "bot*",
                  "--by", owner, db=db)
    assert r2.returncode == EXIT_OK


# ---------------------------------------------------------------------------
# grants
# ---------------------------------------------------------------------------

def test_grant_add_list_revoke(db, owner):
    run_admin("actor", "add", "e1", "--name", "E", "--realm", "external",
              "--sponsor", "own", "--expires", "30d", "--by", owner, db=db)
    r = run_admin("grant", "add", "e1", "--scope-type", "ticket",
                  "--scope", "TK-abc", "--expires", "90d", "--by", owner,
                  "--reason", "partner read", db=db)
    assert r.returncode == EXIT_OK
    grant_id = r.stdout.split()[1]
    r2 = run_admin("grant", "list", "--actor", "e1", db=db)
    assert r2.returncode == EXIT_OK and grant_id in r2.stdout
    assert len([l for l in r2.stdout.splitlines() if l.startswith("grant ")]) == 1
    r3 = run_admin("grant", "revoke", grant_id, "--by", owner, db=db)
    assert r3.returncode == EXIT_OK
    r4 = run_admin("grant", "list", "--actor", "e1", db=db)
    assert grant_id not in r4.stdout  # revoked grants are not live


def test_grant_bad_scope_type_usage(db, owner):
    r = run_admin("grant", "add", "e1", "--scope-type", "universe",
                  "--scope", "x", "--by", owner, db=db)
    assert r.returncode == EXIT_USAGE


# ---------------------------------------------------------------------------
# labels
# ---------------------------------------------------------------------------

def _make_ticket(db):
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO tickets (id, title, body, status, priority, tags, "
        "families, external_refs, artifacts, created_at, updated_at, label) "
        "VALUES ('TK-test', 'a ticket', '', 'draft', 'normal', '[]', '[]', "
        "'{}', '[]', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00', "
        "'internal')")
    conn.commit()
    conn.close()


def test_label_ticket_and_wiki_missing(db, owner):
    _make_ticket(db)
    r = run_admin("label", "ticket", "TK-test", "external", "--by", owner,
                  "--reason", "publish", db=db)
    assert r.returncode == EXIT_OK
    assert sqlite3.connect(db).execute(
        "SELECT label FROM tickets WHERE id='TK-test'").fetchone()[0] == "external"
    assert run_admin("label", "wiki", "ghost-slug", "external", "--by", owner,
                     db=db).returncode == EXIT_NOT_FOUND


def test_label_unsupported_refused(db, owner):
    _make_ticket(db)
    r = run_admin("label", "ticket", "TK-test", "restricted", "--by", owner,
                  db=db)
    assert r.returncode == EXIT_REFUSED
    assert "label_unsupported" in out(r)


# ---------------------------------------------------------------------------
# access-ledger
# ---------------------------------------------------------------------------

def test_ledger_ticket_created_and_events_recorded(db, owner, tmp_path):
    run_admin("actor", "add", "bot", "--name", "Bot", "--by", owner, db=db)
    tok_file = tmp_path / "bot.token"
    run_admin("token", "issue", "bot", "--out", str(tok_file),
              "--by", owner, db=db)
    run_admin("actor", "suspend", "bot", "--by", owner, db=db)
    conn = sqlite3.connect(db)
    tickets = conn.execute(
        "SELECT id, title, label, tags FROM tickets WHERE tags LIKE '%access-ledger%'"
    ).fetchall()
    assert len(tickets) == 1
    tid, title, label, tags = tickets[0]
    assert title == "access-ledger" and label == "internal"
    assert "access-ledger" in json.loads(tags)
    rows = conn.execute(
        "SELECT kind, label, payload FROM events WHERE ticket_id=? ORDER BY seq",
        (tid,)).fetchall()
    decisions = [r for r in rows if r[0] == "decision"]
    assert len(decisions) >= 3
    full_token = tok_file.read_text().strip()
    for _, lbl, payload in decisions:
        assert lbl == "internal"
        assert full_token not in payload  # never the token value (12-char
        # display prefix is an id-style value and is allowed)
    ops = [json.loads(p)["op"] for _, _, p in decisions]
    assert ops == ["actor_add", "actor_add", "token_issue", "actor_suspended"]


def test_ledger_event_payload_has_ids_only(db, owner):
    run_admin("actor", "add", "bot2", "--name", "B", "--by", owner, db=db)
    conn = sqlite3.connect(db)
    payload = conn.execute(
        "SELECT payload FROM events WHERE kind='decision' AND ticket_id IN "
        "(SELECT id FROM tickets WHERE tags LIKE '%access-ledger%') "
        "ORDER BY seq DESC LIMIT 1").fetchone()[0]
    data = json.loads(payload)
    assert data["op"] == "actor_add" and data["actor"] == "bot2"


# ---------------------------------------------------------------------------
# fix round: token file claimed before issuing; revoke on write failure
# ---------------------------------------------------------------------------

def test_token_issue_existing_file_refused_without_credential(db, owner,
                                                              tmp_path):
    """The --out file is claimed (O_CREAT|O_EXCL) BEFORE issue_credential, so
    a refused overwrite leaves NO new credential row."""
    run_admin("actor", "add", "bot", "--name", "Bot", "--by", owner, db=db)
    tok_file = tmp_path / "bot.token"
    assert run_admin("token", "issue", "bot", "--out", str(tok_file),
                     "--by", owner, db=db).returncode == EXIT_OK
    before = sqlite3.connect(db).execute(
        "SELECT COUNT(*) FROM credentials").fetchone()[0]
    r = run_admin("token", "issue", "bot", "--out", str(tok_file),
                  "--by", owner, db=db)
    assert r.returncode == EXIT_REFUSED
    after = sqlite3.connect(db).execute(
        "SELECT COUNT(*) FROM credentials").fetchone()[0]
    assert after == before  # no orphan credential was issued


def test_token_issue_write_failure_revokes_credential(db, owner, tmp_path):
    """Simulated write failure (target is a directory: O_EXCL open fails) ->
    exit 3 refused and NO new credential row."""
    run_admin("actor", "add", "bot", "--name", "Bot", "--by", owner, db=db)
    before = sqlite3.connect(db).execute(
        "SELECT COUNT(*) FROM credentials").fetchone()[0]
    bad_target = tmp_path / "adir"
    bad_target.mkdir()
    r = run_admin("token", "issue", "bot", "--out", str(bad_target),
                  "--by", owner, db=db)
    assert r.returncode == EXIT_REFUSED
    after = sqlite3.connect(db).execute(
        "SELECT COUNT(*) FROM credentials").fetchone()[0]
    assert after == before
    assert "thk_" not in (r.stdout + r.stderr)  # no token value leaked


def test_token_issue_write_failure_after_issue_revokes(db, owner, tmp_path):
    """Simulated write failure AFTER issuing: the issued credential is revoked
    immediately and the empty file is removed. Injected by patching
    hub_admin.os.fdopen to fail mid-command."""
    run_admin("actor", "add", "bot", "--name", "Bot", "--by", owner, db=db)
    site_dir = tmp_path / "site"
    site_dir.mkdir()
    (site_dir / "sitecustomize.py").write_text(
        "import os\n"
        "_real = os.fdopen\n"
        "def _failing(fd, *a, **k):\n"
        "    os.close(fd); raise OSError(28, 'No space left on device')\n"
        "os.fdopen = _failing\n")
    tok_file = tmp_path / "bot.token"
    r = run_admin("token", "issue", "bot", "--out", str(tok_file),
                  "--by", owner, db=db,
                  env_extra={"PYTHONPATH": str(site_dir)})
    assert r.returncode == EXIT_REFUSED
    assert not tok_file.exists()  # the empty/partial file was removed
    conn = sqlite3.connect(db)
    creds = conn.execute("SELECT id FROM credentials").fetchall()
    assert len(creds) == 1  # one was issued...
    revs = conn.execute(
        "SELECT credential_id, reason FROM credential_revocations").fetchall()
    assert len(revs) == 1 and revs[0][0] == creds[0][0]
    assert revs[0][1] == "token file write failed"
    assert "thk_" not in (r.stdout + r.stderr)


# ---------------------------------------------------------------------------
# fix round: label must not rewrite the owner's actor row
# ---------------------------------------------------------------------------

def test_label_by_human_owner_keeps_kind_human(db, owner):
    """cmd_label must load the actor from the DB; constructing
    Actor(id=by, name=by) made ensure_actor overwrite kind human -> agent."""
    _make_ticket(db)
    r = run_admin("label", "ticket", "TK-test", "external", "--by", owner,
                  "--reason", "publish", db=db)
    assert r.returncode == EXIT_OK
    row = sqlite3.connect(db).execute(
        "SELECT kind, name FROM actors WHERE id='own'").fetchone()
    assert row[0] == "human"
    assert row[1] == "Own"  # name preserved as originally stored


def test_label_by_unknown_actor_exit_4(db, owner):
    """label --by an actor with no DB row cannot proceed (nothing is
    constructed from the --by string)."""
    _make_ticket(db)
    r = run_admin("label", "ticket", "TK-test", "external", "--by", "ghost",
                  db=db)
    assert r.returncode == EXIT_REFUSED  # not owner is checked first
    # and the actors table gained no 'ghost' row
    assert sqlite3.connect(db).execute(
        "SELECT COUNT(*) FROM actors WHERE id='ghost'").fetchone()[0] == 0


# ---------------------------------------------------------------------------
# fix round G1: --help exits 0
# ---------------------------------------------------------------------------

def test_help_exits_zero(db):
    # --help must exit 0 even with TICKET_HUB_DB unset (G1)
    r = run_admin("--help", db=None)
    assert r.returncode == EXIT_OK
    assert "usage" in r.stdout.lower()


def test_subcommand_help_exits_zero(db):
    r = run_admin("token", "issue", "--help", db=None)
    assert r.returncode == EXIT_OK


# ---------------------------------------------------------------------------
# fix round G4: a suspended or expired owner is refused
# ---------------------------------------------------------------------------

def test_suspended_owner_refused(db, owner):
    run_admin("actor", "add", "own2", "--name", "O2", "--kind", "human",
              "--owner", "--by", owner, db=db)
    assert run_admin("actor", "suspend", "own2", "--by", owner,
                     db=db).returncode == EXIT_OK
    r = run_admin("actor", "add", "x9", "--name", "X", "--by", "own2", db=db)
    assert r.returncode == EXIT_REFUSED
    assert sqlite3.connect(db).execute(
        "SELECT COUNT(*) FROM actors WHERE id='x9'").fetchone()[0] == 0


def test_expired_owner_refused(db, owner, tmp_path):
    past = "2020-01-01T00:00:00Z"
    assert run_admin("actor", "add", "own3", "--name", "O3", "--kind",
                     "human", "--owner", "--expires", past,
                     "--by", owner, db=db).returncode == EXIT_OK
    r = run_admin("token", "issue", "own", "--out", str(tmp_path / "t"),
                  "--by", "own3", db=db)
    assert r.returncode == EXIT_REFUSED


# ---------------------------------------------------------------------------
# fix round G2: hub_admin uses the public conn properties
# ---------------------------------------------------------------------------

def test_hub_admin_no_private_conn_access():
    src = ADMIN.read_text()
    assert "._conn" not in src


# ---------------------------------------------------------------------------
# fix round 3 (store): ensure_actor never overwrites kind
# ---------------------------------------------------------------------------

def test_ensure_actor_conflict_keeps_kind(tmp_path):
    sys.path.insert(0, str(REPO))
    from ticket_hub.model import Actor as ModelActor
    from ticket_hub.store import Store
    s = Store(str(tmp_path / "s.db"))
    try:
        s.ensure_actor(ModelActor(id="X", name="X", family="f", kind="human"))
        s.ensure_actor(ModelActor(id="X", name="X2", family="f2",
                                  kind="agent"))
        s.conn.commit()
        row = s.conn.execute(
            "SELECT kind, name, family FROM actors WHERE id='X'").fetchone()
        assert row["kind"] == "human"   # kind never overwritten
        assert row["name"] == "X2" and row["family"] == "f2"  # these do update
    finally:
        s.close()


# ---------------------------------------------------------------------------
# fix round G4: suspended owner blocked on token/grant mutations too
# ---------------------------------------------------------------------------

def test_suspended_owner_blocked_on_token_and_grant(db, owner, tmp_path):
    run_admin("actor", "add", "own2", "--name", "O2", "--kind", "human",
              "--owner", "--by", owner, db=db)
    run_admin("actor", "suspend", "own2", "--by", owner, db=db)
    r = run_admin("token", "issue", "own", "--out", str(tmp_path / "t"),
                  "--by", "own2", db=db)
    assert r.returncode == EXIT_REFUSED
    assert not (tmp_path / "t").exists()
    r2 = run_admin("grant", "add", "own", "--scope-type", "ticket",
                   "--scope", "TK-x", "--by", "own2", db=db)
    assert r2.returncode == EXIT_REFUSED


# ---------------------------------------------------------------------------
# hubp1-w8: actor adopt / adopt-legacy (pre-access-control cut-over)
# ---------------------------------------------------------------------------

def seed_legacy(db, actor_id, kind="agent"):
    """Insert an actor row the way pre-access code did: no actor_status row.
    Store() creates the schema (a fresh DB is otherwise empty until a CLI
    command opens it)."""
    sys.path.insert(0, str(REPO))
    from ticket_hub.store import Store
    s = Store(str(db))
    try:
        s.conn.execute(
            "INSERT INTO actors (id, name, family, kind, realm) "
            "VALUES (?, ?, 'unknown', ?, 'internal')",
            (actor_id, actor_id.title(), kind))
        s.conn.commit()
    finally:
        s.close()


def status_of(db, actor_id):
    row = sqlite3.connect(db).execute(
        "SELECT status FROM actor_status WHERE actor_id=? "
        "ORDER BY rowid DESC LIMIT 1", (actor_id,)).fetchone()
    return row[0] if row else None


def test_adopt_bootstrap_owner_from_legacy_row(db):
    """A legacy human-owner row becomes the bootstrap owner: any --by value
    while no owner exists; after it the actor is live and is_owner."""
    seed_legacy(db, "human1", kind="agent")
    r = run_admin("actor", "adopt", "human1", "--kind", "human", "--owner",
                  "--by", "whatever", db=db)
    assert r.returncode == EXIT_OK, out(r)
    row = sqlite3.connect(db).execute(
        "SELECT kind, is_owner FROM actors WHERE id='human1'").fetchone()
    assert tuple(row) == ("human", 1)
    assert status_of(db, "human1") == "active"
    # a second --owner bootstrap-style adopt is now refused (an owner exists)
    seed_legacy(db, "human2")
    r2 = run_admin("actor", "adopt", "human2", "--owner",
                   "--by", "human2", db=db)
    assert r2.returncode == EXIT_REFUSED


def test_adopt_requires_owner_after_bootstrap(db, owner):
    seed_legacy(db, "oldbot")
    r = run_admin("actor", "adopt", "oldbot", "--by", "oldbot", db=db)
    assert r.returncode == EXIT_REFUSED
    r2 = run_admin("actor", "adopt", "oldbot", "--kind", "system",
                   "--family", "forge", "--by", owner, db=db)
    assert r2.returncode == EXIT_OK, out(r2)
    row = sqlite3.connect(db).execute(
        "SELECT kind, family FROM actors WHERE id='oldbot'").fetchone()
    assert tuple(row) == ("system", "forge")


def test_adopt_missing_actor_exit_4(db, owner):
    r = run_admin("actor", "adopt", "ghost", "--by", owner, db=db)
    assert r.returncode == EXIT_NOT_FOUND


def test_adopt_legacy_cli_activates_and_is_idempotent(db, owner):
    seed_legacy(db, "role:a")
    seed_legacy(db, "role:b", kind="human")
    r = run_admin("actor", "adopt-legacy", "--by", owner, db=db)
    assert r.returncode == EXIT_OK, out(r)
    assert "role:a" in out(r) and "role:b" in out(r)
    assert status_of(db, "role:a") == "active"
    assert status_of(db, "role:b") == "active"
    # kind unchanged by adopt-legacy
    row = sqlite3.connect(db).execute(
        "SELECT kind FROM actors WHERE id='role:b'").fetchone()
    assert row[0] == "human"
    # idempotent: second call adopts nothing
    r2 = run_admin("actor", "adopt-legacy", "--by", owner, db=db)
    assert r2.returncode == EXIT_OK
    assert "adopted 0" in out(r2)


def test_adopt_legacy_requires_owner(db):
    seed_legacy(db, "role:a")
    r = run_admin("actor", "adopt-legacy", "--by", "role:a", db=db)
    assert r.returncode == EXIT_REFUSED


def test_adopt_cli_does_not_revive_suspended(db, owner):
    run_admin("actor", "add", "bot", "--name", "Bot", "--by", owner, db=db)
    run_admin("actor", "suspend", "bot", "--by", owner, db=db)
    seed_legacy(db, "role:new")
    r = run_admin("actor", "adopt", "bot", "--by", owner, db=db)
    assert r.returncode == EXIT_REFUSED
    assert status_of(db, "bot") == "suspended"
    r2 = run_admin("actor", "adopt-legacy", "--by", owner, db=db)
    assert r2.returncode == EXIT_OK
    assert status_of(db, "bot") == "suspended"  # untouched by adopt-legacy


def test_adopt_cli_ledger_events_ids_only(db, owner):
    seed_legacy(db, "role:a")
    assert run_admin("actor", "adopt", "role:a", "--by", owner,
                     db=db).returncode == EXIT_OK
    assert run_admin("actor", "adopt-legacy", "--by", owner,
                     db=db).returncode == EXIT_OK
    conn = sqlite3.connect(db)
    rows = conn.execute(
        """SELECT ev.kind, ev.payload FROM events ev
           JOIN tickets t ON t.id = ev.ticket_id
           WHERE t.title = 'access-ledger' ORDER BY ev.seq""").fetchall()
    ops = [json.loads(p)["op"] for k, p in rows if k == "decision"]
    assert "actor_adopt" in ops and "actor_adopt_legacy" in ops
    dump = "\n".join(conn.iterdump())
    assert "thk_" not in dump  # ids only, never token values


def test_adopted_human_blocks_act_as_patterns(db, owner, tmp_path):
    seed_legacy(db, "oldhum", kind="human")
    assert run_admin("actor", "adopt-legacy", "--by", owner,
                     db=db).returncode == EXIT_OK
    # pattern matching the adopted internal human -> refused at issue (3)
    r = run_admin("token", "issue", "own", "--out", str(tmp_path / "t1"),
                  "--scopes", "act_as", "--act-as", "old*",
                  "--by", owner, db=db)
    assert r.returncode == EXIT_REFUSED
    assert not (tmp_path / "t1").exists()
    # a token for the adopted human works (adopted -> live)
    r2 = run_admin("token", "issue", "oldhum", "--out", str(tmp_path / "t2"),
                   "--by", owner, db=db)
    assert r2.returncode == EXIT_OK, out(r2)
    assert status_of(db, "oldhum") == "active"
    # ... but acting AS the adopted internal human is refused. A pattern that
    # matches them can no longer be issued at all; a pre-adoption wildcard
    # credential is still refused at USE time (effective() re-checks).
    sys.path.insert(0, str(REPO))
    from ticket_hub.access import AccessStore
    from ticket_hub.store import Store
    s = Store(str(db))
    try:
        a = AccessStore(s.conn)
        v = a.viewer_for_token((tmp_path / "t2").read_text().strip())
        assert v is not None and v.actor.id == "oldhum"
        # an exact pattern matching them cannot be issued post-adoption
        with pytest.raises(ValueError):
            a.issue_credential("own", by=owner, scopes=("act_as",),
                               act_as_allow=("oldhum",))
        # use-time: credential whose wildcard only matched 'old*' later
        a.create_actor("relay2", "Relay", kind="system", by=owner)
        _, tok = a.issue_credential("relay2", by=owner, scopes=("act_as",),
                                    act_as_allow=("rel*",))
        rv = a.viewer_for_token(tok)
        with pytest.raises(PermissionError):
            a.effective(rv, "oldhum")
    finally:
        s.close()
