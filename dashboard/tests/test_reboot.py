"""Layer 8 REBOOT unit tests: the pure recovery assessor + mutation rule.

Every seeded fault class from the 2026-11-15 reboot cascade (dead service,
lost crontab entry, missing token) MUST produce a specific finding; a clean
reboot must produce NONE.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard import reboot                                 # noqa: E402

CRON = ["*/5 * * * * job-a # reboot-a",
        "*/10 * * * * job-b # reboot-b"]


def snap(services=None, crontab=None, tickets=None, token="tok"):
    return reboot.snapshot(
        tickets=(["reboot-check-0", "reboot-check-1"] if tickets is None
                 else tickets),
        ledger_lines=5, crontab=(CRON if crontab is None else crontab),
        token=token,
        services_up=services or {"hub": True, "console": True,
                                 "ops-agent": True, "scheduler": True})


def test_clean_reboot_no_findings():
    before = snap()
    after = snap(tickets=before["tickets"], token="tok")
    assert reboot.assess_recovery(before, after) == []


def test_dead_service_detected():
    before = snap()
    after = snap(services={"hub": True, "console": False,
                           "ops-agent": True, "scheduler": True},
                 tickets=before["tickets"], token="tok")
    findings = reboot.assess_recovery(before, after)
    assert "service-did-not-recover: console" in findings


def test_lost_crontab_entry_detected():
    before = snap()
    after = snap(crontab=CRON[:-1], tickets=before["tickets"], token="tok")
    findings = reboot.assess_recovery(before, after)
    assert any(f.startswith("crontab-entry-lost: ") for f in findings)


def test_missing_token_detected():
    before = snap()
    after = snap(tickets=before["tickets"], token="tok")
    after_token = reboot.snapshot(tickets=before["tickets"], ledger_lines=5,
                                  crontab=CRON, token="",  # file gone
                                  services_up=after["services_up"])
    findings = reboot.assess_recovery(before, after_token)
    assert any(f.startswith("token-changed-or-invalid") for f in findings)


def test_state_loss_detected():
    before = snap()
    after = snap(tickets=before["tickets"][:-1], token="tok")
    findings = reboot.assess_recovery(before, after)
    assert any(f.startswith("state-loss: tickets") for f in findings)


def test_mutation_inversion_all_faults_caught():
    """Each seeded fault class must be caught; a fault-free assessor that
    misses ANY of them would leave the layer un-armed."""
    before = snap()
    classes = set()
    for after in (
        snap(services={"hub": True, "console": False, "ops-agent": True,
                       "scheduler": False}, tickets=before["tickets"],
             token="tok"),
        snap(crontab=[], tickets=before["tickets"], token="tok"),
        snap(tickets=before["tickets"], token="rotated-token"),
    ):
        for f in reboot.assess_recovery(before, after):
            classes.add(f.split(":")[0])
    assert {"service-did-not-recover", "crontab-entry-lost",
            "token-changed-or-invalid"} <= classes
