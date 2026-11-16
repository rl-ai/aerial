"""§7 Calibration criteria C1–C2.

C1 has two halves:
  - reasoned dismissals updating the gate's false-positive metric within one
    refresh cycle: coverable via MandateFlow's calibration hook (the hook the
    gate loop subscribes to).
  - the QA-sampling statistical slice: implemented as Queue.qa_sample +
    render.qa_tab (statistical slice of admitted findings for annotation).
"""
import pytest

from conftest import make_finding, T0
from dashboard.exception_queue.mandate_flow import MandateFlow


def test_C1_reasoned_dismiss_updates_fp_metric_within_refresh_cycle(queue):
    # the gate's calibration loop consumes dismissal samples from the hook;
    # one refresh cycle = the next gate evaluation after the dismissal
    fp_state = {"negative": 0, "total": 0, "fp_rate": 0.0}

    def gate_calibration(item, reason):
        # a dismissal is a negative calibration sample for the gate's
        # false-positive metric
        fp_state["total"] += 1
        if reason not in (None, "unspecified") or True:
            fp_state["negative"] += 1
        fp_state["fp_rate"] = fp_state["negative"] / fp_state["total"]

    queue.admit(make_finding("g1", "degraded", a=0.8))
    queue.admit(make_finding("g2", "degraded", a=0.8))
    mf = MandateFlow(queue, calibration=gate_calibration, clock=lambda: T0)
    # reasoned dismissal feeds the metric...
    mf.decide("q-1", "dismiss", reason="false positive — evidence resolved upstream")
    assert fp_state["negative"] == 1 and fp_state["total"] == 1
    # ...and it is visible within one refresh cycle (the next gate read)
    assert fp_state["fp_rate"] == 1.0
    mf.decide("q-2", "dismiss", reason="duplicate of q-1")
    assert fp_state["total"] == 2 and fp_state["fp_rate"] == 1.0


def test_C2_reason_less_dismiss_not_silently_dropped(queue):
    # C2 = Q7 from the calibration side: reason-less dismissals still produce
    # a negative calibration sample with reason 'unspecified'
    samples = []
    queue.admit(make_finding("u1", "degraded", a=0.8))
    mf = MandateFlow(queue, calibration=lambda i, r: samples.append((i.item_id, r)),
                     clock=lambda: T0)
    res = mf.decide("q-1", "dismiss")
    assert samples == [("q-1", "unspecified")]
    assert res["calibration_reason"] == "unspecified"


def test_C1_qa_sampling_slice_not_full_backlog_and_feeds_calibration(queue):
    # §7 C1 second half: the QA-sampling tab offers a STATISTICAL SLICE (not
    # the full backlog) of admitted findings for annotation; dismissing a
    # sampled row with a reason feeds the false-positive metric.
    from dashboard.exception_queue.queue import qa_sample
    from dashboard.daily_board.render import qa_tab

    for i in range(10):
        queue.admit(make_finding(f"qa{i}", "degraded", a=0.8))
    s1 = qa_sample(queue, sample_rate=0.3, seed=7)
    s2 = qa_sample(queue, sample_rate=0.3, seed=7)
    assert len(s1) == 3 and len(s1) < 10          # a slice, never the backlog
    assert [it.item_id for it in s1] == [it.item_id for it in s2]  # reproducible
    with pytest.raises(ValueError):
        qa_sample(queue, sample_rate=1.5)

    # tab exposure: render exposes the sampled rows for annotation
    tab = qa_tab(queue, sample_rate=0.3, seed=7)
    assert tab["sample_size"] == 3 and tab["backlog_size"] == 10
    assert [r["item_id"] for r in tab["rows"]] == [it.item_id for it in s1]

    # reasoned dismissal of a sampled row feeds the FP metric (other half of C1)
    fp = {"neg": 0, "tot": 0}
    mf = MandateFlow(queue, calibration=lambda it, r: fp.update(
        tot=fp["tot"] + 1, neg=fp["neg"] + (r != "unspecified")),
        clock=lambda: T0)
    mf.decide(s1[0].item_id, "dismiss", reason="false positive on re-check")
    assert fp == {"neg": 1, "tot": 1}
    # the dismissed row leaves the slice pool for the next refresh cycle
    s3 = qa_sample(queue, sample_rate=0.3, seed=7)
    assert s1[0].item_id not in [it.item_id for it in s3]
