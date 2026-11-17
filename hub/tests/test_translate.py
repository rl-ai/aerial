"""Tests for ticket_hub.translate (Lane C)."""

import json

import pytest

from ticket_hub.translate import polish_pass, template_pass


def ev(id_, kind, ts="2026-10-01T00:00:00Z", ticket_id="TK-1", actor=None, **payload):
    e = {"id": id_, "ticket_id": ticket_id, "kind": kind, "ts": ts}
    if actor is not None:
        e["actor"] = actor
    if payload:
        e["payload"] = payload
    return e


# ---------------------------------------------------------------- mapping

def test_status_changed_advances():
    d = template_pass(events=[ev("e1", "status_changed", to="Review")])
    assert d["bullets"][0]["text"] == "Advanced to Review."
    assert "Review" in d["bullets"][0]["text"]


def test_status_changed_done_and_blocked():
    e = {"id": "e2", "ticket_id": "TK-1", "kind": "status_changed",
         "ts": "t", "payload": {"from": "Running", "to": "Blocked"}}
    d = template_pass(events=[ev("e1", "status_changed", to="Done"), e])
    assert "Done" in d["bullets"][0]["text"]
    assert "Blocked" in d["bullets"][1]["text"]
    assert "Running" in d["bullets"][1]["text"]


def test_comment_quotes_body_and_actor():
    d = template_pass(events=[
        ev("e1", "comment", actor={"name": "Owner"}, body="ship it"),
    ])
    text = d["bullets"][0]["text"]
    assert "Owner" in text and "ship it" in text


def test_checkpoint_and_decision():
    d = template_pass(events=[
        ev("e1", "checkpoint", name="alpha build"),
        ev("e2", "decision", decision="use sqlite"),
    ])
    assert d["bullets"][0]["text"] == "Checkpoint reached: alpha build."
    assert d["bullets"][1]["text"] == "Decision recorded: use sqlite."


def test_approval_receipt_names_approver():
    d = template_pass(events=[
        ev("e1", "approval_receipt", approved_by="Owner"),
    ])
    assert d["bullets"][0]["text"] == "Approved by Owner."


def test_artifact_produced_name():
    d = template_pass(events=[ev("e1", "artifact", name="report.pdf")])
    assert d["bullets"][0]["text"] == "Produced report.pdf."


def test_thread_message_sentence():
    d = template_pass(events=[
        ev("e1", "thread_message", chat_user="U1"),
    ])
    assert "Chat reply" in d["bullets"][0]["text"]


@pytest.mark.parametrize("kind,payload", [
    ("comment", {"body": "QUEUED FINDING: flaky test"}),
    ("checkpoint", {"text": "KNOWN-FAILS: 2"}),
    ("artifact", {"name": "known-fails.txt", "note": "KNOWN-FAILS"}),
])
def test_known_issue_payloads_map_to_known_issue_logged(kind, payload):
    e = {"id": "e1", "ticket_id": "TK-1", "kind": kind, "ts": "t",
         "payload": payload}
    d = template_pass(events=[e])
    assert d["bullets"][0]["text"] == "Known issue logged."


def test_unknown_kind_generic_honest_sentence():
    d = template_pass(events=[ev("e1", "quantum_flux")])
    text = d["bullets"][0]["text"]
    assert "Activity recorded" in text  # generic, no fabricated specifics


# ------------------------------------------------------- citations / facts

def test_every_bullet_cites_existing_event_ids():
    events = [ev("a", "created"), ev("b", "status_changed", to="Open"),
              ev("c", "artifact", name="x"), ev("d", "comment", body="hi")]
    d = template_pass(events=events)
    ids = {e["id"] for e in events}
    for bullet in d["bullets"]:
        assert bullet["event_ids"]
        assert all(i in ids for i in bullet["event_ids"])


def test_no_fabrication_all_events_cited_exactly_once():
    events = [ev(f"e{n}", "comment", body=f"m{n}") for n in range(5)]
    events.append(ev("z", "approval_receipt", approved_by="Owner"))
    d = template_pass(events=events)
    cited = [i for b in d["bullets"] for i in b["event_ids"]]
    assert sorted(cited) == sorted(e["id"] for e in events)


def test_events_without_id_are_dropped_not_invented():
    bad = {"ticket_id": "TK-1", "kind": "comment", "payload": {"body": "x"}}
    d = template_pass(events=[bad])
    assert d["bullets"] == []


def test_digest_shape_and_updated_at():
    events = [ev("e1", "created", ts="2026-01-01T00:00:00Z"),
              ev("e2", "updated", ts="2026-01-05T00:00:00Z")]
    d = template_pass(events=events, project="forge")
    assert set(d) == {"project", "updated_at", "bullets"}
    assert d["project"] == "forge"
    assert d["updated_at"] == "2026-01-05T00:00:00Z"


def test_project_defaults_to_ticket_id():
    d = template_pass(events=[ev("e1", "created", ticket_id="TK-42")])
    assert d["project"] == "TK-42"


def test_timeline_accepted_and_requires_exactly_one_source():
    tl = [ev("e1", "comment", body="hi")]
    d = template_pass(timeline=tl)
    assert d["bullets"][0]["event_ids"] == ["e1"]
    with pytest.raises(TypeError):
        template_pass()
    with pytest.raises(TypeError):
        template_pass(events=tl, timeline=tl)


def test_deterministic_same_input_same_output():
    events = [ev("e1", "status_changed", to="Review"),
              ev("e2", "artifact", name="a.tar.gz")]
    a = json.dumps(template_pass(events=events), sort_keys=True)
    b = json.dumps(template_pass(events=list(events)), sort_keys=True)
    assert a == b


# ---------------------------------------------------------------- polish

def test_polish_pass_none_returns_template_unchanged():
    d = template_pass(events=[ev("e1", "created")])
    assert polish_pass(d, llm_call=None) == d
    assert polish_pass(d) == d


def test_polish_pass_rewrites_text_preserves_event_ids():
    d = template_pass(events=[ev("e1", "status_changed", to="Review")])
    out = polish_pass(d, llm_call=lambda dig: [
        {"text": "Now in review!", "event_ids": ["e1"]}])
    assert out["bullets"][0]["text"] == "Now in review!"
    assert out["bullets"][0]["event_ids"] == ["e1"]


def test_polish_pass_string_result_keeps_all_citations():
    d = template_pass(events=[ev("e1", "created"), ev("e2", "updated")])
    out = polish_pass(d, llm_call=lambda dig: "Great progress this week.")
    assert out["bullets"][0]["event_ids"] == ["e1", "e2"]


def test_polish_pass_falls_back_on_exception():
    d = template_pass(events=[ev("e1", "created")])

    def boom(_):
        raise RuntimeError("llm down")

    assert polish_pass(d, llm_call=boom) == d


def test_polish_pass_rejects_bullets_with_unknown_event_ids():
    d = template_pass(events=[ev("e1", "created")])
    out = polish_pass(d, llm_call=lambda dig: [
        {"text": "made up", "event_ids": ["TK-999"]}])
    # fabricating citations reverts to the template truth source
    assert out["bullets"][0]["event_ids"] == ["e1"]
    assert "made up" not in json.dumps(out)
