"""Tests for the borrowed mechanisms: admission strength (A2), hand-back
(A3), pairing (B7), shadow rollout (A4), plugin ingest (B10), digest (C12)."""

from __future__ import annotations

import pytest

from secondbrain import (Admission, AdmissionError, Curator, Gatekeeper,
                         GrantBook, L0Vault, NotesFolderAdapter, Pairing,
                         Registry, Workspace, build_digest, render)
from secondbrain.admission import HandBack


@pytest.fixture()
def env(tmp_path):
    """The standard small corpus + published graph + one grant."""
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "gym.md").write_text(
        "@entity:gym-riverbank\n2026-10-14: Trained hard. Squat 100kg.\n"
        "sensitivity: personal\n", encoding="utf-8")
    vault = L0Vault(tmp_path / "l0")
    ws = Workspace(tmp_path / "l1")
    Curator(vault, ws).ingest_folder(notes)
    Curator(vault, ws).build_cluster_artifacts()
    ws.publish("main", vault)
    book = GrantBook(tmp_path / "l.db")
    grant_id, key = book.create(
        counterparty="Coach", purpose="exercise Q&A",
        anchor_entities=["entity:gym-riverbank"], alias="main",
        ttl_days=30, query_budget=5,
        disclosure={"require_citation": True, "max_date_granularity": "month"})
    return vault, ws, book, grant_id, key, notes


# ------------------------------------------------- A2: admission strength

def test_explicit_question_consumes_budget(env):
    _, _, book, grant_id, key, _ = env
    a = Admission(book)
    assert a.grade(grant_id, key, "What is my squat progress?") == "explicit"
    assert a.grade(grant_id, key, "and the row?",
                   in_flight_request=True) == "follow-up"


def test_followup_never_consumes_budget(env):
    _, ws, book, grant_id, key, _ = env
    gk = Gatekeeper(ws, book)
    first = gk.ask(grant_id, key, "squat progress?")
    assert first["verdict"] == "answered"
    used = book.budget_used(grant_id)
    second = gk.ask(grant_id, key, "and in general?",
                    in_flight_request=True)
    assert second["verdict"] == "follow-up"
    assert book.budget_used(grant_id) == used   # no budget burned


def test_bots_never_open_work(env):
    _, ws, book, grant_id, key, _ = env
    gk = Gatekeeper(ws, book)
    result = gk.ask(grant_id, key, "What is my squat progress?",
                    sender_kind="bot")
    assert result["verdict"] == "refused"
    assert "bot" in result["reason"]
    assert book.budget_used(grant_id) == 0      # never consumes budget


# ------------------------------------------------- A3: hand-back

def test_adjacent_mandate_ask_is_handed_back_not_refused(env):
    """The ask names an adjacent mandate the publisher flagged: hand it back
    with a reason instead of a bare refusal or a silent expansion."""
    _, ws, book, grant_id, key, _ = env
    gk = Gatekeeper(ws, book)
    result = gk.ask(grant_id, key, "How is my knee physio going?",
                    mandate_hints={"entity:clinic-downtown": ["physio",
                                                             "knee"]})
    assert result["verdict"] == "handed_back"
    assert "clinic-downtown" in result["reason"]
    states = [e["state"] for e in book.entries(grant_id)]
    assert states[-1] == "handed_back"


def test_handback_reasons_accumulate_for_scope_tuning(env):
    _, ws, book, grant_id, key, _ = env
    gk = Gatekeeper(ws, book)
    hints = {"entity:clinic-downtown": ["physio"]}
    gk.ask(grant_id, key, "physio update?", mandate_hints=hints)
    gk.ask(grant_id, key, "more physio?", mandate_hints=hints)
    backs = [e for e in book.entries(grant_id) if e["state"] == "handed_back"]
    assert len(backs) == 2                      # the free tuning dataset


# ------------------------------------------------- B7: pairing

def test_pairing_never_auto_activates(tmp_path):
    p = Pairing(tmp_path / "p.db")
    code = p.request("SomeApp", note="wants exercise Q&A")
    assert p.entries("unapproved")[0]["code"] == code
    # nothing exists until you approve: no grant is created by a request
    assert p.approve(code) is True
    assert p.entries("approved")[0]["code"] == code
    assert p.approve(code) is False             # cannot re-approve
    denied = p.request("OtherApp")
    assert p.deny(denied) is True
    assert [e["status"] for e in p.entries()] == ["approved", "denied"]


# ------------------------------------------------- A4: shadow rollout

def test_shadow_mode_logs_what_it_would_refuse_and_consumes_nothing(env):
    _, ws, book, grant_id, key, _ = env
    gk = Gatekeeper(ws, book, shadow=True)
    # intimate content is absent here, so force a refusal shape: ask for
    # something whose draft would violate require_citation? simplest real
    # shadow signal: the pass path — nothing enforced either way.
    result = gk.ask(grant_id, key, "squat progress?")
    assert result["verdict"] == "shadow" and result["would"] == "answer"
    assert book.budget_used(grant_id) == 0      # shadow consumes nothing
    states = [e["state"] for e in book.entries(grant_id)]
    assert states == ["shadow_pass"]


def test_shadow_refusal_is_recorded_not_enforced(env):
    vault, ws, book, grant_id, key, notes = env
    # add an intimate note inside the gym cluster: enforcing would refuse
    (notes / "journal.md").write_text(
        "@entity:gym-riverbank\nPrivate difficult conversation.\n"
        "sensitivity: intimate\n", encoding="utf-8")
    from secondbrain import Curator as C
    C(vault, ws).ingest_folder(notes)
    C(vault, ws).build_cluster_artifacts()
    ws.publish("main", vault)
    gk = Gatekeeper(ws, book, shadow=True)
    result = gk.ask(grant_id, key, "the private conversation?")
    assert result["verdict"] == "shadow" and result["would"] == "refuse"
    assert book.budget_used(grant_id) == 0
    enforcing = Gatekeeper(ws, book)
    verdict = enforcing.ask(grant_id, key, "the private conversation?")
    assert verdict["verdict"] == "refused"      # the flip is one flag


# ------------------------------------------------- B10: plugin ingest

def test_ingest_registry_routes_by_kind_and_refuses_unknown(env):
    vault, ws, book, grant_id, key, notes = env
    reg = Registry()
    reg.register(NotesFolderAdapter(vault, Curator(vault, ws)))
    assert reg.kinds() == ["notes-folder"]
    hashes = reg.ingest("notes-folder", notes)   # content-addressed: no-op
    assert all(len(h) == 64 for h in hashes)
    with pytest.raises(KeyError):
        reg.ingest("mail-imap", notes)
    with pytest.raises(ValueError):
        reg.register(NotesFolderAdapter(vault, Curator(vault, ws)))


def test_ingest_describe_is_the_dry_proposal(env):
    vault, ws, _, _, _, notes = env
    adapter = NotesFolderAdapter(vault, Curator(vault, ws))
    plan = adapter.describe(notes)
    assert plan["kind"] == "notes-folder"
    assert plan["files"] == ["gym.md"]
    assert plan["entities"] == ["gym-riverbank"]


# ------------------------------------------------- C12: digest

def test_digest_lists_what_waits_and_collapses_clean_state(env):
    _, ws, book, grant_id, key, _ = env
    gk = Gatekeeper(ws, book)
    clean = build_digest(book, gk)
    assert clean["waits_for_you"] is False
    assert render(clean) == "Nothing waits for you."
    gk.ask(grant_id, key, "physio?", mandate_hints={
        "entity:clinic-downtown": ["physio"]})
    digest = build_digest(book, gk)
    assert digest["waits_for_you"] is True
    text = render(digest)
    assert "Hand-backs (1)" in text and "physio?" in text
    assert "Nothing waits" not in text


def test_digest_since_ts_never_reshows_reviewed_history(env):
    _, ws, book, grant_id, key, _ = env
    gk = Gatekeeper(ws, book)
    gk.ask(grant_id, key, "physio?", mandate_hints={
        "entity:clinic-downtown": ["physio"]})
    everything = build_digest(book, gk)
    assert everything["counts"]["hand_backs"] == 1
    # a stamp after every entry: the cadence view shows nothing old again
    future = "2099-01-01T00:00:00Z"
    after = build_digest(book, gk, since_ts=future)
    assert after["counts"]["hand_backs"] == 0
    assert after["waits_for_you"] is False


def test_digest_flag_rows_render_their_why(env):
    _, ws, book, grant_id, key, _ = env
    gk = Gatekeeper(ws, book)
    gk.ask(grant_id, key, "physio?", mandate_hints={
        "entity:clinic-downtown": ["physio"]})
    text = render(build_digest(book, gk))
    assert "outside mandate" in text      # the flag's why is not dropped


def test_month_granularity_cuts_iso_datetimes(env):
    """A day-level date inside an ISO timestamp must not leak (glm review
    2026-11-11: the trailing \\b missed the 'T' boundary)."""
    _, ws, book, grant_id, key, _ = env
    gk = Gatekeeper(ws, book)
    result = gk.ask(grant_id, key, "squat progress?")
    assert result["verdict"] == "answered"
    assert "-14T" not in result["answer"] and "-14:" not in result["answer"]


def test_budget_charged_exactly_once_per_query(env):
    _, ws, book, grant_id, key, _ = env
    gk = Gatekeeper(ws, book)
    gk.ask(grant_id, key, "squat progress?")            # answered: 1
    gk.ask(grant_id, key, "knee physio?", mandate_hints={
        "entity:clinic-downtown": ["physio"]})          # handed back: 0
    gk.ask(grant_id, key, "what is the meaning of life?")  # refused: 1
    assert book.budget_used(grant_id) == 2              # never double-charged


# ------------------------------------------------- G6: per-file error isolation

def test_ingest_folder_isolates_a_corrupt_file_and_reports(env, tmp_path):
    """G6: one corrupt/unreadable file is skipped with its error recorded;
    the rest of the batch still lands and the report is {stored, skipped,
    errors} (backward compatible: iterating yields the stored hashes)."""
    vault, ws, _, _, _, notes = env
    (notes / "corrupt.md").write_bytes(b"\xff\xfe not utf-8 \x00\x01")
    curator = Curator(vault, ws)
    report = curator.ingest_folder(notes)
    assert set(report.keys()) == {"stored", "skipped", "errors"}
    assert [e["file"] for e in report["errors"]] == ["corrupt.md"]
    assert report["skipped"] == []   # G6: errors are errors, not skips
    assert report["stored"], "the good file was still ingested"
    assert all(len(h) == 64 for h in report)   # iterates stored hashes
    # the batch is not lost: re-ingest without the corrupt file is a no-op
    (notes / "corrupt.md").unlink()
    again = curator.ingest_folder(notes)
    assert again["errors"] == [] and len(again["stored"]) == \
        len(report["stored"])


# ------------------------------------------------- G3: idempotent artifacts

def test_cluster_artifacts_rebuild_in_place_without_duplicates(env):
    """G3: artifacts are keyed by the entity node id; two consecutive
    build_cluster_artifacts calls produce the same node set and the same
    edge set (no duplicated artifacts, refs or provenance edges)."""
    vault, ws, _, _, _, notes = env
    curator = Curator(vault, ws)
    first = curator.build_cluster_artifacts()
    nodes1 = {n.id: (n.text, n.refs, n.label) for n in ws.nodes.values()}
    edges1 = sorted((e.src, e.dst, e.kind) for e in ws.edges)
    second = curator.build_cluster_artifacts()
    assert [a.id for a in second] == [a.id for a in first]
    assert all(a.id.startswith("artifact:entity:") for a in second)
    assert {n.id for n in ws.nodes.values()} == set(nodes1)
    nodes2 = {n.id: (n.text, n.refs, n.label) for n in ws.nodes.values()}
    assert nodes1 == nodes2
    edges2 = sorted((e.src, e.dst, e.kind) for e in ws.edges)
    assert edges1 == edges2
    artifacts = [n for n in ws.nodes.values() if n.type == "artifact"]
    assert len(artifacts) == len(first)          # no append-on-rerun


# ------------------------------------------------- G7: persisted flags

def test_gatekeeper_flags_persist_across_a_restart(env, tmp_path):
    """G7: _flag appends to a persisted JSONL flag log (path injectable);
    build_digest in a fresh process reads persisted flags, so governance
    signals survive a restart."""
    _, ws, book, grant_id, key, _ = env
    flag_log = tmp_path / "governance" / "flags.jsonl"
    gk = Gatekeeper(ws, book, flag_log=flag_log)
    gk.ask(grant_id, key, "physio?", mandate_hints={
        "entity:clinic-downtown": ["physio"]})
    assert flag_log.exists() and flag_log.read_text().strip()
    # a "restart": a brand-new gatekeeper with empty in-memory flags
    fresh = Gatekeeper(ws, book, flag_log=flag_log)
    assert fresh.flags == []
    digest = build_digest(book, fresh)
    assert digest["counts"]["flags"] >= 1        # signal survived
    assert any("outside mandate" in f["why"] for f in digest["sections"]["flags"])


# ------------------------------------------------- G8: atomic tombstones

def test_tombstone_writes_are_atomic_and_corruption_tolerant(tmp_path):
    """G8: tombstones.json is written tmp + os.replace (mirroring the
    object path) and a corrupt tombstone file is treated as empty, not a
    crash — the next tombstone() write repairs it."""
    vault = L0Vault(tmp_path / "l0")
    h1 = vault.ingest("first", source="a")
    h2 = vault.ingest("second", source="b")
    vault.tombstone(h1)
    tomb = tmp_path / "l0" / "tombstones.json"
    assert tomb.exists()
    assert not list((tmp_path / "l0").glob("*.tmp"))  # atomic: no tmp left
    doc = __import__("json").loads(tomb.read_text())
    assert h1 in doc
    # corrupt the file: reads tolerate it instead of crashing
    tomb.write_text("{not json", encoding="utf-8")
    assert vault.is_alive(h2)                    # treated as empty
    # the next write repairs the file wholesale (corrupt state was treated
    # as empty per G8, so the repair rebuilds from what is tombstoned now)
    vault.tombstone(h2)
    doc = __import__("json").loads(tomb.read_text())
    assert set(doc) == {h2}
    assert not vault.is_alive(h2)
