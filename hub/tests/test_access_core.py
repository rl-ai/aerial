"""Tests for ticket_hub.access — ACCESS-P1-CONTRACT §2 (lane W2).

Covers: table append-only triggers, token hygiene, actor realm rules,
credential lifecycle, grants and liveness, act_as delegation, ExternalReader
visibility (SQL-side filtering incl. subtree CTE) and the exact projections.
hubp1-w2fix adds regression tests: ticket grants do NOT expand descendants,
no *_no_delete_p1 triggers on store tables, create_actor refuses an existing
id, kind validation, and canonicalised timestamp comparison (_canon_ts).
hubp1-w8 adds adopt_actor / adopt_legacy (cut-over for pre-access-control
actor rows).
"""
import sqlite3

import pytest

from ticket_hub.access import AccessStore, ExternalReader, Viewer, _canon_ts
from ticket_hub.model import Actor
from ticket_hub.store import Store


@pytest.fixture()
def store(tmp_path):
    s = Store(str(tmp_path / "hub.db"))
    yield s
    s.close()


@pytest.fixture()
def acc(store):
    a = AccessStore(store._conn)
    a.create_actor("own", "Own", kind="human", is_owner=True, by="bootstrap")
    a.create_actor("hum", "Hum", kind="human", by="own")
    a.create_actor("bot", "Bot", family="forge", kind="agent", by="own")
    a.create_actor("relay", "Relay", kind="system", by="own")
    a.create_actor(
        "ext1", "Ext One", realm="external", sponsor_id="own",
        expires_at="2099-01-01T00:00:00Z", by="own",
    )
    return a


@pytest.fixture()
def ext_token(acc):
    return acc.issue_credential("ext1", by="own")[1]


# ---------------------------------------------------------------------------
# actors
# ---------------------------------------------------------------------------


class TestActors:
    def test_create_internal_defaults(self, acc):
        attrs = acc.actor_attrs("bot")
        assert attrs["realm"] == "internal"
        assert attrs["clearance"] == "internal"
        assert attrs["is_owner"] is False
        assert attrs["status"] == "active"

    def test_external_requires_sponsor(self, acc):
        with pytest.raises(ValueError):
            acc.create_actor("x", "x", realm="external",
                             expires_at="2099-01-01T00:00:00Z", by="own")

    def test_external_requires_expiry(self, acc):
        with pytest.raises(ValueError):
            acc.create_actor("x", "x", realm="external", sponsor_id="own", by="own")

    def test_external_forbids_owner(self, acc):
        with pytest.raises(ValueError):
            acc.create_actor("x", "x", realm="external", sponsor_id="own",
                             expires_at="2099-01-01T00:00:00Z", is_owner=True, by="own")

    def test_external_clearance_forced(self, acc):
        assert acc.actor_attrs("ext1")["clearance"] == "external"

    def test_internal_human_allowed(self, acc):
        attrs = acc.actor_attrs("hum")
        assert attrs["kind"] == "human" and attrs["realm"] == "internal"

    def test_actor_attrs_missing(self, acc):
        assert acc.actor_attrs("nobody") is None

    def test_no_p1_delete_triggers_on_store_tables(self, store, acc):
        """hubp1-w2fix: AccessStore must not bolt *_no_delete_p1 triggers onto
        store tables — tickets/events/wiki_pages keep only the store's own."""
        names = {r[0] for r in store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger'")}
        assert not [n for n in names if n.endswith("_no_delete_p1")]
        # the six access tables keep their own contract triggers
        for t in ("credentials", "credential_revocations", "grants",
                  "grant_revocations", "actor_status", "access_log"):
            assert f"{t}_no_update" in names and f"{t}_no_delete" in names
        # and a raw UPDATE on tickets (no store trigger there) is allowed
        t = store.create_ticket("editable", owner=Actor(id="own", name="Own"))
        store._conn.execute("UPDATE tickets SET title = 'x' WHERE id = ?", (t.id,))
        store._conn.rollback()

    def test_actor_live_status_transitions(self, acc):
        assert acc.actor_live("hum") is True
        acc.set_actor_status("hum", "suspended", by="own", reason="t")
        assert acc.actor_live("hum") is False
        assert acc.actor_attrs("hum")["status"] == "suspended"
        acc.set_actor_status("hum", "active", by="own")
        assert acc.actor_live("hum") is True

    def test_actor_live_expired_uses_now(self, acc):
        assert acc.actor_live("ext1", now="2098-01-01T00:00:00Z") is True
        assert acc.actor_live("ext1", now="2100-01-01T00:00:00Z") is False

    def test_actor_live_missing(self, acc):
        assert acc.actor_live("ghost") is False

    def test_unknown_status_rejected(self, acc):
        with pytest.raises(ValueError):
            acc.set_actor_status("hum", "banished", by="own")

    def test_status_log_is_append_only(self, store, acc):
        with pytest.raises(sqlite3.DatabaseError):
            store._conn.execute("DELETE FROM actor_status")
        with pytest.raises(sqlite3.DatabaseError):
            store._conn.execute("UPDATE actor_status SET status = 'active'")

    def test_create_actor_refuses_existing_id(self, acc):
        """Re-registration is gone: an existing id raises ValueError('actor
        exists') and the stored identity/kind are untouched (a re-create could
        otherwise turn an internal human into an act_as target)."""
        with pytest.raises(ValueError) as ei:
            acc.create_actor("hum", "Hacked", kind="agent", by="own")
        assert "actor exists" in str(ei.value)
        attrs = acc.actor_attrs("hum")
        assert (attrs["name"], attrs["kind"], attrs["realm"]) == ("Hum", "human", "internal")
        # and the failed call wrote nothing: still exactly one status row for hum
        n = acc._conn.execute(
            "SELECT COUNT(*) AS n FROM actor_status WHERE actor_id = 'hum'"
        ).fetchone()["n"]
        assert n == 1

    @pytest.mark.parametrize("bad", ["Human ", " human", "HUMAN", "human,", "", "robot"])
    def test_kind_validated_exact_before_write(self, acc, bad):
        with pytest.raises(ValueError):
            acc.create_actor("weird", "W", kind=bad, by="own")
        assert acc.actor_attrs("weird") is None

    def test_effective_refuses_unknown_kind_target(self, store, acc):
        """A target row whose kind is not agent/human/system (e.g. written
        before validation existed) can never be an act_as target."""
        store._conn.execute(
            "INSERT INTO actors (id, name, family, kind, realm) "
            "VALUES ('odd', 'Odd', 'unknown', 'cryptid', 'internal')")
        store._conn.commit()
        acc.set_actor_status("odd", "active", by="own")
        _, tok = acc.issue_credential("relay", by="own", scopes=("act_as",),
                                      act_as_allow=("odd",))
        v = acc.viewer_for_token(tok)
        with pytest.raises(PermissionError):
            acc.effective(v, "odd")


# ---------------------------------------------------------------------------
# hubp1-w8: adoption of pre-access-control actor rows
# ---------------------------------------------------------------------------


class TestAdopt:
    """adopt_actor / adopt_legacy cut-over for legacy rows created before the
    access tables existed (no actor_status row -> not live)."""

    def _legacy(self, store, acc, actor_id, **kw):
        """Insert a raw actor row the way pre-access code did: no status."""
        cols = {"id": actor_id, "name": actor_id.title(), "family": "unknown",
                "kind": "agent", "realm": "internal"}
        cols.update(kw)
        store._conn.execute(
            "INSERT INTO actors (id, name, family, kind, realm) "
            "VALUES (:id, :name, :family, :kind, :realm)", cols)
        store._conn.commit()
        return acc

    def test_legacy_row_is_not_live(self, store, acc):
        self._legacy(store, acc, "oldbot")
        assert acc.actor_attrs("oldbot")["status"] is None
        assert acc.actor_live("oldbot") is False

    def test_adopt_legacy_activates_and_makes_usable(self, store, acc):
        self._legacy(store, acc, "oldbot")
        self._legacy(store, acc, "oldbot2", kind="human")
        ids = acc.adopt_legacy(by="own")
        assert ids == ["oldbot", "oldbot2"]
        for a in ids:
            assert acc.actor_attrs(a)["status"] == "active"
            assert acc.actor_live(a) is True
        # usable: credential issued, token resolves to the legacy actor
        cid, tok = acc.issue_credential("oldbot", by="own")
        v = acc.viewer_for_token(tok)
        assert v is not None and v.actor.id == "oldbot"

    def test_adopt_legacy_kind_unchanged(self, store, acc):
        self._legacy(store, acc, "oldhum", kind="human")
        acc.adopt_legacy(by="own")
        attrs = acc.actor_attrs("oldhum")
        assert (attrs["kind"], attrs["realm"], attrs["is_owner"]) == (
            "human", "internal", False)

    def test_adopt_legacy_idempotent(self, store, acc):
        self._legacy(store, acc, "oldbot")
        assert acc.adopt_legacy(by="own") == ["oldbot"]
        assert acc.adopt_legacy(by="own") == []

    def test_adopt_legacy_skips_suspended(self, store, acc):
        # an actor that went through set_actor_status deliberately has a
        # status row: adopt_legacy must not touch it.
        acc.set_actor_status("bot", "suspended", by="own", reason="bad")
        self._legacy(store, acc, "oldbot")
        assert acc.adopt_legacy(by="own") == ["oldbot"]
        assert acc.actor_attrs("bot")["status"] == "suspended"
        assert acc.actor_live("bot") is False

    def test_adopt_actor_missing_raises_keyerror(self, acc):
        with pytest.raises(KeyError):
            acc.adopt_actor("ghost", by="own")

    def test_adopt_actor_kind_and_owner_on_legacy_row(self, store, acc):
        self._legacy(store, acc, "oldowner")
        actor = acc.adopt_actor("oldowner", by="own", kind="human",
                                family="owner", is_owner=True)
        assert (actor.kind, actor.family) == ("human", "owner")
        attrs = acc.actor_attrs("oldowner")
        assert attrs["status"] == "active" and attrs["is_owner"] is True
        assert acc.actor_live("oldowner") is True

    def test_adopt_actor_kind_validated(self, store, acc):
        self._legacy(store, acc, "oldbot")
        with pytest.raises(ValueError):
            acc.adopt_actor("oldbot", by="own", kind="Human")
        assert acc.actor_attrs("oldbot")["status"] is None  # nothing written

    def test_adopt_actor_external_owner_refused(self, store, acc):
        self._legacy(store, acc, "oldext", realm="external")
        with pytest.raises(ValueError):
            acc.adopt_actor("oldext", by="own", is_owner=True)
        assert acc.actor_attrs("oldext")["status"] is None

    def test_adopt_actor_refuses_suspended_or_revoked(self, store, acc):
        acc.set_actor_status("bot", "suspended", by="own")
        with pytest.raises(ValueError):
            acc.adopt_actor("bot", by="own")
        assert acc.actor_attrs("bot")["status"] == "suspended"
        acc.set_actor_status("relay", "revoked", by="own")
        with pytest.raises(ValueError):
            acc.adopt_actor("relay", by="own")
        assert acc.actor_attrs("relay")["status"] == "revoked"

    def test_adopt_actor_active_idempotent_no_extra_status_row(self, acc):
        n0 = acc._conn.execute(
            "SELECT COUNT(*) AS n FROM actor_status WHERE actor_id='bot'"
        ).fetchone()["n"]
        acc.adopt_actor("bot", by="own")
        n1 = acc._conn.execute(
            "SELECT COUNT(*) AS n FROM actor_status WHERE actor_id='bot'"
        ).fetchone()["n"]
        assert n1 == n0

    def test_adopt_never_touches_realm_clearance_sponsor_expiry(self, store, acc):
        store._conn.execute(
            "INSERT INTO actors (id, name, family, kind, realm, clearance, "
            "sponsor_id, expires_at, is_owner) VALUES "
            "('oldext', 'Old Ext', 'unknown', 'agent', 'external', "
            "'external', 'own', '2099-01-01T00:00:00+00:00', 0)")
        store._conn.commit()
        before = {k: acc.actor_attrs("oldext")[k]
                  for k in ("realm", "clearance", "sponsor_id",
                            "expires_at", "is_owner")}
        acc.adopt_actor("oldext", by="own", kind="system")
        after = {k: acc.actor_attrs("oldext")[k]
                 for k in before}
        assert after == before

    def test_adopted_human_blocked_for_act_as(self, store, acc):
        """The human-owner cut-over must not open an act_as hole: a pattern
        matching the adopted internal human is refused at ISSUE time, and
        act_as to them is refused at USE time even via a matching-to-other
        wildcard issued before adoption."""
        self._legacy(store, acc, "oldhum", kind="human")
        acc.adopt_legacy(by="own")
        # issue-time refusal: pattern now matches an internal human
        with pytest.raises(ValueError):
            acc.issue_credential("relay", by="own", scopes=("act_as",),
                                 act_as_allow=("old*",))
        # use-time refusal: credential issued BEFORE adoption whose pattern
        # later matches the adopted human
        _, tok = acc.issue_credential("relay", by="own", scopes=("act_as",),
                                      act_as_allow=("rel*",))
        v = acc.viewer_for_token(tok)
        with pytest.raises(PermissionError):
            acc.effective(v, "oldhum")


# ---------------------------------------------------------------------------
# credentials & tokens
# ---------------------------------------------------------------------------


class TestCredentials:
    def test_token_shape_and_prefix(self, acc):
        cid, tok = acc.issue_credential("bot", by="own")
        assert tok.startswith("thk_") and len(tok) > 20
        entry = {c["id"]: c for c in acc.list_credentials()}[cid]
        assert entry["prefix"] == tok[:12]

    def test_token_never_stored(self, store, acc):
        _, tok = acc.issue_credential("bot", by="own")
        dump = "\n".join(store._conn.iterdump())
        assert tok not in dump
        # only the sha256 hex lives in the row
        row = store._conn.execute(
            "SELECT token_hash FROM credentials ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        assert len(row["token_hash"]) == 64 and tok not in row["token_hash"]

    def test_viewer_for_token_roundtrip(self, acc):
        _, tok = acc.issue_credential("bot", by="own")
        v = acc.viewer_for_token(tok)
        assert v.actor.id == "bot" and v.realm == "internal" and v.is_owner is False
        assert v.credential_id is not None

    def test_bad_token_none(self, acc):
        assert acc.viewer_for_token("thk_nope") is None
        assert acc.viewer_for_token("") is None

    def test_revoke_kills_viewer(self, acc):
        cid, tok = acc.issue_credential("bot", by="own")
        acc.revoke_credential(cid, by="own", reason="leaked")
        assert acc.viewer_for_token(tok) is None
        assert {c["id"]: c for c in acc.list_credentials()}[cid]["revoked"] is True

    def test_expired_credential(self, acc):
        _, tok = acc.issue_credential("bot", by="own", expires_at="2000-01-01T00:00:00Z")
        assert acc.viewer_for_token(tok) is None

    def test_suspended_actor_kills_viewer(self, acc):
        _, tok = acc.issue_credential("hum", by="own")
        acc.set_actor_status("hum", "suspended", by="own")
        assert acc.viewer_for_token(tok) is None
        acc.set_actor_status("hum", "active", by="own")
        assert acc.viewer_for_token(tok) is not None

    def test_external_actor_expiry_checked_at_use(self, acc, ext_token):
        assert acc.viewer_for_token(ext_token) is not None
        assert acc.viewer_for_token(ext_token, now="2100-01-01T00:00:00Z") is None

    def test_act_as_allow_requires_scope(self, acc):
        with pytest.raises(ValueError):
            acc.issue_credential("relay", by="own", act_as_allow=("bot",))

    def test_act_as_allow_rejects_internal_human_at_issue(self, acc):
        with pytest.raises(ValueError):
            acc.issue_credential("relay", by="own", scopes=("act_as",),
                                 act_as_allow=("hum",))
        with pytest.raises(ValueError):
            acc.issue_credential("relay", by="own", scopes=("act_as",),
                                 act_as_allow=("h*",))

    def test_token_never_in_exception(self, acc):
        cid, tok = acc.issue_credential("bot", by="own")
        try:
            acc.revoke_credential("no-such-cred", by="own")
        except Exception as exc:  # KeyError
            assert tok not in str(exc)

    def test_list_credentials_projection(self, acc):
        cid, _ = acc.issue_credential("bot", by="own", scopes=("read", "act_as"))
        entry = {c["id"]: c for c in acc.list_credentials()}[cid]
        assert set(entry) == {"id", "actor_id", "prefix", "scopes", "expires_at",
                              "revoked", "last_used_at"}
        assert entry["scopes"] == ["read", "act_as"]
        assert entry["last_used_at"] is None

    def test_last_used_from_access_log(self, acc):
        cid, tok = acc.issue_credential("bot", by="own")
        v = acc.viewer_for_token(tok)
        acc.log_access(actor_id="bot", credential_id=cid, method="GET",
                       route="/tickets", status=200, decision="allow")
        entry = {c["id"]: c for c in acc.list_credentials()}[cid]
        assert entry["last_used_at"] is not None
        acc.log_access(actor_id=v.actor.id, credential_id=cid, method="GET",
                       route="/tickets", status=404, decision="hidden")
        assert {c["id"]: c for c in acc.list_credentials()}[cid]["last_used_at"] >= entry["last_used_at"]

    def test_credentials_tables_append_only(self, store, acc):
        cid, _ = acc.issue_credential("bot", by="own")
        acc.revoke_credential(cid, by="own")  # seed rows (triggers are per-row)
        with pytest.raises(sqlite3.DatabaseError):
            store._conn.execute("DELETE FROM credentials")
        with pytest.raises(sqlite3.DatabaseError):
            store._conn.execute("UPDATE credentials SET actor_id = 'hum'")


# ---------------------------------------------------------------------------
# canonical timestamps (hubp1-w2fix finding A)
# ---------------------------------------------------------------------------


class TestCanonicalTimestamps:
    def test_canon_ts_formats(self):
        assert _canon_ts("2030-01-01T00:00:00Z") == "2030-01-01T00:00:00.000+00:00"
        assert _canon_ts("2030-01-01 00:00:00") == "2030-01-01T00:00:00.000+00:00"  # naive = UTC
        assert _canon_ts("2030-01-01T02:00:00+02:00") == "2030-01-01T00:00:00.000+00:00"
        assert _canon_ts("2030-01-01T00:00:00.5-00:00") == "2030-01-01T00:00:00.500+00:00"
        for bad in ("", "  ", "not-a-date", "2030-13-01T00:00:00Z", "2030"):
            with pytest.raises(ValueError):
                _canon_ts(bad)

    def test_credential_z_expiry_vs_offset_now(self, acc):
        """Contract probe: expiring at 2030-01-01T00:00:00Z is live one
        millisecond before and dead exactly at/after, however `now` is spelled."""
        _, tok = acc.issue_credential("bot", by="own", expires_at="2030-01-01T00:00:00Z")
        assert acc.viewer_for_token(tok, now="2029-12-31T23:59:59.999+00:00") is not None
        assert acc.viewer_for_token(tok, now="2030-01-01T00:00:00.000+00:00") is None
        assert acc.viewer_for_token(tok, now="2030-01-01T00:00:00Z") is None
        # stored form is canonical, so a lexical SQL comparison stays correct too
        row = acc._conn.execute(
            "SELECT expires_at FROM credentials ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        assert row["expires_at"] == "2030-01-01T00:00:00.000+00:00"

    def test_mixed_timezone_comparisons(self, acc):
        _, tok = acc.issue_credential("bot", by="own", expires_at="2030-06-01T12:00:00+05:00")
        # 07:00:00.000Z == 12:00+05:00: dead at and after the instant, live before
        assert acc.viewer_for_token(tok, now="2030-06-01T06:59:59.999Z") is not None
        assert acc.viewer_for_token(tok, now="2030-06-01T07:00:00Z") is None

    def test_actor_expiry_canonicalised(self, acc):
        acc.create_actor("extx", "X", realm="external", sponsor_id="own",
                         expires_at="2099-06-01T00:00:00+09:00", by="own")
        assert acc.actor_attrs("extx")["expires_at"] == "2099-05-31T15:00:00.000+00:00"
        assert acc.actor_live("extx", now="2099-05-31T14:59:59.999Z") is True
        assert acc.actor_live("extx", now="2099-05-31T15:00:00Z") is False
        with pytest.raises(ValueError):
            acc.create_actor("exty", "Y", realm="external", sponsor_id="own",
                             expires_at="someday", by="own")

    def test_grant_expiry_canonicalised_in_sql(self, acc):
        gid = acc.add_grant("ext1", "ticket", "TK-Z", by="own",
                            expires_at="2030-03-01T00:00:00Z")
        assert acc.live_grants("ext1", now="2030-02-28T23:59:59.999+00:00") != []
        assert acc.live_grants("ext1", now="2030-03-01T01:00:00+01:00") == []
        row = acc._conn.execute(
            "SELECT expires_at FROM grants WHERE id = ?", (gid,)).fetchone()
        assert row["expires_at"] == "2030-03-01T00:00:00.000+00:00"

    def test_reader_now_canonicalised(self, store, acc):
        own = Actor(id="own", name="Own", kind="human")
        t = store.create_ticket("zt", owner=own, label="external")
        acc.add_grant("ext1", "ticket", t.id, by="own",
                      expires_at="2030-03-01T00:00:00Z")
        tok = acc.issue_credential("ext1", by="own")[1]

        def titles(now):
            r = ExternalReader(store._conn, acc.viewer_for_token(tok), now=now)
            return [x["id"] for x in r.list_tickets()]

        assert titles("2030-02-28T23:59:59.999Z") == [t.id]
        assert titles("2030-03-01T01:00:00+01:00") == []

    def test_invalid_now_rejected(self, acc):
        with pytest.raises(ValueError):
            acc.actor_live("bot", now="whenever")
        # a valid token resolves far enough to reach the invalid-now check...
        _, tok = acc.issue_credential("bot", by="own")
        with pytest.raises(ValueError):
            acc.viewer_for_token(tok, now="tomorrow")
        # ...an unknown token still returns None without touching `now`
        assert acc.viewer_for_token("thk_nope", now="tomorrow") is None


# ---------------------------------------------------------------------------
# grants
# ---------------------------------------------------------------------------


class TestGrants:
    def test_add_and_list(self, acc):
        gid = acc.add_grant("ext1", "ticket", "TK-1", by="own", reason="r")
        g = acc.live_grants("ext1")[0]
        assert g["id"] == gid and g["scope_type"] == "ticket" and g["scope_id"] == "TK-1"
        assert g["verbs"] == ["read"] and g["max_label"] == "external"

    def test_bad_verb_rejected(self, acc):
        with pytest.raises(ValueError):
            acc.add_grant("ext1", "ticket", "TK-1", verbs=("promote",), by="own")

    def test_bad_scope_type_rejected(self, acc):
        with pytest.raises(ValueError):
            acc.add_grant("ext1", "universe", "everything", by="own")

    def test_unknown_scope_type_rejected(self, acc):
        with pytest.raises(ValueError):
            acc.add_grant("ext1", "board", "x", by="own")

    def test_revoke_removes_from_live(self, acc):
        gid = acc.add_grant("ext1", "wiki_page", "pg", by="own")
        assert len(acc.live_grants("ext1")) == 1
        acc.revoke_grant(gid, by="own", reason="done")
        assert acc.live_grants("ext1") == []

    def test_expired_grant_not_live(self, acc):
        acc.add_grant("ext1", "ticket", "TK-1", by="own", expires_at="2001-01-01T00:00:00Z")
        assert acc.live_grants("ext1") == []
        assert len(acc.live_grants("ext1", now="2000-01-01T00:00:00Z")) == 1

    def test_grant_missing_actor(self, acc):
        with pytest.raises(KeyError):
            acc.add_grant("ghost", "ticket", "TK-1", by="own")

    def test_grants_tables_append_only(self, store, acc):
        gid = acc.add_grant("ext1", "ticket", "TK-1", by="own")
        acc.revoke_grant(gid, by="own")
        cid, _ = acc.issue_credential("bot", by="own")
        acc.revoke_credential(cid, by="own")  # row for credential_revocations
        acc.log_access(actor_id="bot", credential_id=None, method="GET",
                       route="/x", status=200, decision="allow")
        store._conn.commit()
        # BEFORE triggers fire per-row only, so every table must hold a row
        # (an UPDATE/DELETE of an empty table is a no-op, not a violation).
        for table in ("grants", "grant_revocations", "credential_revocations",
                      "access_log"):
            with pytest.raises(sqlite3.DatabaseError):
                store._conn.execute(f"DELETE FROM {table}")
            with pytest.raises(sqlite3.DatabaseError):
                col = "decision" if table == "access_log" else "reason"
                store._conn.execute(f"UPDATE {table} SET {col} = 'x'")


# ---------------------------------------------------------------------------
# effective() / act_as delegation
# ---------------------------------------------------------------------------


@pytest.fixture()
def relay_viewer(acc):
    _, tok = acc.issue_credential("relay", by="own", scopes=("act_as",),
                                  act_as_allow=("bot", "ext*"))
    return acc.viewer_for_token(tok)


class TestEffective:
    def test_none_and_self_pass_through(self, acc, relay_viewer):
        assert acc.effective(relay_viewer, None) is relay_viewer
        assert acc.effective(relay_viewer, "relay") is relay_viewer

    def test_target_viewer_with_via(self, acc, relay_viewer):
        eff = acc.effective(relay_viewer, "bot")
        assert eff.actor.id == "bot" and eff.realm == "internal" and eff.via == "relay"

    def test_external_target_keeps_clearance(self, acc, relay_viewer):
        eff = acc.effective(relay_viewer, "ext1")
        assert (eff.actor.id, eff.realm, eff.clearance, eff.via) == (
            "ext1", "external", "external", "relay")

    def test_no_scope_refused(self, acc):
        _, tok = acc.issue_credential("bot", by="own")
        v = acc.viewer_for_token(tok)
        with pytest.raises(PermissionError):
            acc.effective(v, "hum")

    def test_pattern_mismatch_refused(self, acc, relay_viewer):
        with pytest.raises(PermissionError):
            acc.effective(relay_viewer, "hum")  # no pattern covers hum
        with pytest.raises(PermissionError):
            acc.effective(relay_viewer, "own")  # owner not in allow-list

    def test_internal_human_never_target_despite_pattern(self, acc):
        _, tok = acc.issue_credential("relay", by="own", scopes=("act_as",),
                                      act_as_allow=("bot",))
        v = acc.viewer_for_token(tok)
        with pytest.raises(PermissionError):
            acc.effective(v, "hum")

    def test_human_created_after_issue_refused_at_use(self, acc):
        _, tok = acc.issue_credential("relay", by="own", scopes=("act_as",),
                                      act_as_allow=("newbie",))
        v = acc.viewer_for_token(tok)
        acc.create_actor("newbie", "New", kind="human", by="own")  # internal human later
        with pytest.raises(PermissionError):
            acc.effective(v, "newbie")

    def test_dead_target_refused(self, acc, relay_viewer):
        acc.set_actor_status("bot", "suspended", by="own")
        with pytest.raises(PermissionError):
            acc.effective(relay_viewer, "bot")

    def test_missing_target_refused(self, acc, relay_viewer):
        with pytest.raises(PermissionError):
            acc.effective(relay_viewer, "ghost")

    def test_self_claim_of_internal_human_without_scope(self, acc):
        _, tok = acc.issue_credential("hum", by="own")
        v = acc.viewer_for_token(tok)
        assert acc.effective(v, "hum").actor.id == "hum"


# ---------------------------------------------------------------------------
# ExternalReader visibility
# ---------------------------------------------------------------------------


@pytest.fixture()
def world(store, acc, ext_token):
    """Ticket tree S > c-ext > grandchild, c-int (internal child), other."""
    own = Actor(id="own", name="Own", kind="human")
    S = store.create_ticket("S", owner=own, label="external")
    c_ext = store.create_ticket("c-ext", parent=S.id, owner=own, label="external")
    c_int = store.create_ticket("c-int SECRETWORD", parent=S.id, owner=own)
    gchild = store.create_ticket("grandchild", parent=c_ext.id, owner=own, label="external")
    other = store.create_ticket("other", owner=own, label="external")
    gid = acc.add_grant("ext1", "subtree", S.id, by="own")
    reader = ExternalReader(store._conn, acc.viewer_for_token(ext_token))
    return dict(store=store, acc=acc, reader=reader, gid=gid, S=S, c_ext=c_ext,
                c_int=c_int, gchild=gchild, other=other)


class TestExternalTickets:
    def test_subtree_visibility(self, world):
        titles = sorted(t["title"] for t in world["reader"].list_tickets())
        assert titles == ["S", "c-ext", "grandchild"]

    def test_projection_keys(self, world):
        assert sorted(world["reader"].get_ticket(world["S"].id)) == sorted(
            ["id", "title", "body", "status", "priority", "label",
             "created_at", "updated_at"])

    def test_internal_child_hidden(self, world):
        assert world["reader"].get_ticket(world["c_int"].id) is None
        assert world["reader"].ticket_visible(world["c_int"].id) is False

    def test_ungranted_ticket_hidden(self, world):
        assert world["reader"].get_ticket(world["other"].id) is None

    def test_query_cannot_leak_secret(self, world):
        assert world["reader"].list_tickets(q="SECRETWORD") == []
        assert world["reader"].list_tickets(q="c-") != []

    def test_direct_ticket_grant(self, store, acc, ext_token):
        own = Actor(id="own", name="Own", kind="human")
        t = store.create_ticket("solo", owner=own, label="public")
        acc.add_grant("ext1", "ticket", t.id, by="own")
        r = ExternalReader(store._conn, acc.viewer_for_token(ext_token))
        assert [x["id"] for x in r.list_tickets()] == [t.id]

    def test_ticket_grant_does_not_expand_descendants(self, store, acc):
        """hubp1-w2fix: a `ticket` grant covers ONLY that ticket — an
        external-labelled child of it stays hidden; the same grant as
        `subtree` exposes the child."""
        own = Actor(id="own", name="Own", kind="human")
        T = store.create_ticket("T", owner=own, label="external")
        C = store.create_ticket("C", parent=T.id, owner=own, label="external")
        acc.create_actor("e1", "E1", realm="external", sponsor_id="own",
                         expires_at="2099-01-01T00:00:00Z", by="own")
        acc.create_actor("e2", "E2", realm="external", sponsor_id="own",
                         expires_at="2099-01-01T00:00:00Z", by="own")
        acc.add_grant("e1", "ticket", T.id, by="own")
        r1 = ExternalReader(store._conn, acc.viewer_for_token(
            acc.issue_credential("e1", by="own")[1]))
        assert sorted(t["title"] for t in r1.list_tickets()) == ["T"]
        assert r1.get_ticket(T.id) is not None
        assert r1.get_ticket(C.id) is None
        assert r1.ticket_visible(C.id) is False
        acc.add_grant("e2", "subtree", T.id, by="own")
        r2 = ExternalReader(store._conn, acc.viewer_for_token(
            acc.issue_credential("e2", by="own")[1]))
        assert sorted(t["title"] for t in r2.list_tickets()) == ["C", "T"]

    def test_ticket_grant_child_events_hidden(self, store, acc):
        own = Actor(id="own", name="Own", kind="human")
        T = store.create_ticket("T", owner=own, label="external")
        C = store.create_ticket("C", parent=T.id, owner=own, label="external")
        store.append_event(C.id, "comment", actor=own, payload={}, label="external")
        acc.add_grant("ext1", "ticket", T.id, by="own")
        r = ExternalReader(store._conn, acc.viewer_for_token(
            acc.issue_credential("ext1", by="own")[1]))
        assert r.events(C.id) is None

    def test_grant_revocation_hides_all(self, world):
        world["acc"].revoke_grant(world["gid"], by="own")
        r = ExternalReader(world["store"]._conn,
                           world["acc"].viewer_for_token(
                               world["acc"].issue_credential("ext1", by="own")[1]))
        assert r.list_tickets() == []

    def test_expired_grant_hides_all(self, store, acc, ext_token, world):
        acc.create_actor("ext2", "Ext Two", realm="external", sponsor_id="own",
                         expires_at="2099-01-01T00:00:00Z", by="own")
        tok2 = acc.issue_credential("ext2", by="own")[1]
        acc.add_grant("ext2", "subtree", world["S"].id, by="own",
                      expires_at="2001-01-01T00:00:00Z")
        r = ExternalReader(store._conn, acc.viewer_for_token(tok2))
        assert r.list_tickets() == []

    def test_ungranted_viewer_sees_nothing(self, store, acc, world):
        _, tok = acc.issue_credential("ext1", by="own")
        for g in acc.live_grants("ext1"):
            acc.revoke_grant(g["id"], by="own")
        r = ExternalReader(store._conn, acc.viewer_for_token(tok))
        assert r.list_tickets() == []

    def test_status_and_tag_filters(self, store, acc, ext_token):
        own = Actor(id="own", name="Own", kind="human")
        t = store.create_ticket("tagged", owner=own, label="external", tags=["pub"])
        store.update_ticket(t.id, status="open", actor=own)
        acc.add_grant("ext1", "ticket", t.id, by="own")
        r = ExternalReader(store._conn, acc.viewer_for_token(ext_token))
        assert [x["id"] for x in r.list_tickets(status="open")] == [t.id]
        assert r.list_tickets(status="draft") == []
        assert [x["id"] for x in r.list_tickets(tag="pub")] == [t.id]
        assert r.list_tickets(tag="nope") == []


class TestExternalEvents:
    def test_only_visible_events(self, world):
        own = Actor(id="own", name="Own", kind="human")
        s = world["store"]
        ev_pub = s.append_event(world["S"].id, "comment", actor=own,
                                payload={"x": 1}, label="external")
        ev_int = s.append_event(world["S"].id, "comment", actor=own,
                                payload={"x": 2}, label="internal")
        ids = [e["id"] for e in world["reader"].events(world["S"].id)
               if e["kind"] == "comment"]
        assert ev_pub.id in ids and ev_int.id not in ids

    def test_event_projection_and_team_masking(self, world):
        own = Actor(id="own", name="Own", kind="human")
        s = world["store"]
        s.append_event(world["S"].id, "comment", actor=own, payload={"x": 1})
        evs = [e for e in world["reader"].events(world["S"].id) if e["kind"] == "comment"]
        assert sorted(evs[0]) == sorted(["id", "ticket_id", "ts", "kind", "payload",
                                         "label", "actor"])
        assert evs[0]["actor"] == {"name": "Team"}  # internal actor masked

    def test_event_from_external_actor_shows_name(self, world):
        s = world["store"]
        ext = Actor(id="ext1", name="Ext One", kind="agent")
        s.append_event(world["S"].id, "comment", actor=ext, payload={})
        evs = [e for e in world["reader"].events(world["S"].id) if e["kind"] == "comment"]
        assert evs[-1]["actor"] == {"name": "Ext One"}

    def test_events_hidden_ticket_returns_none(self, world):
        assert world["reader"].events(world["c_int"].id) is None


class TestExternalWiki:
    @pytest.fixture()
    def wiki(self, store, acc, ext_token):
        store.put_page("pg", title="t", body_md="r1 internal")          # r1 internal
        store.put_page("pg", title="t", body_md="r2", label="external")  # r2 external
        store.put_page("pg", title="t", body_md="r3")                    # r3 inherits external
        acc.add_grant("ext1", "wiki_page", "pg", by="own")
        return ExternalReader(store._conn, acc.viewer_for_token(ext_token))

    def test_history_only_public_external_revs(self, store, wiki):
        assert [p["rev"] for p in wiki.page_history("pg")] == [2, 3]

    def test_get_page_latest_visible(self, store, wiki):
        page = wiki.get_page("pg")
        assert page["rev"] == 3 and sorted(page) == sorted(
            ["slug", "rev", "title", "body_md", "label", "ts"])

    def test_raising_current_label_hides_page(self, store, acc, wiki):
        store.put_page("pg", title="t", body_md="r4", label="internal")
        r = ExternalReader(store._conn, acc.viewer_for_token(
            acc.issue_credential("ext1", by="own")[1]))
        assert r.get_page("pg") is None
        assert r.page_history("pg") is None
        assert r.list_pages() == []
        assert r.search_pages("r") == []

    def test_no_grant_hides_even_external_page(self, store, wiki):
        store.put_page("np", title="n", body_md="hidden", label="external")
        assert wiki.get_page("np") is None
        assert wiki.page_visible("np") is False

    def test_list_and_search_match_current_rev(self, store, wiki):
        assert [p["slug"] for p in wiki.list_pages()] == ["pg"]
        assert [p["rev"] for p in wiki.search_pages("r3")] == [3]
        assert wiki.search_pages("r1 internal") == []  # only on hidden r1
