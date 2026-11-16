"""The six-question demo (design doc §9.7).

An invited fake app (a health coach) holds one grant scoped to the gym
cluster on a pinned L2 version. Four useful sanitized answers, two refusals
(an out-of-scope probe and a borderline leak), and the ledger showing
exactly what left and what was stripped.

Run: python -m demo  (from secondbrain's parent folder)
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from secondbrain import (Curator, Gatekeeper, GrantBook, L0Vault,
                         Workspace)

NOTES = {
    "gym-routine.md": (
        "@entity:gym-riverbank #fitness\n"
        "2026-10-14: Trained at Rua do Gym 12, Lisbon. 5k row, 3x5 squats.\n"
        "sensitivity: personal\n"),
    "gym-progress.md": (
        "@entity:gym-riverbank #fitness\n"
        "2026-10-28: Squat up to 100kg. Coach happy with progress.\n"
        "sensitivity: personal\n"),
    "clinic.md": (
        "@entity:clinic-downtown\n"
        "2026-09-02: Physio session at Clinica Central for knee pain.\n"
        "sensitivity: sensitive\n"),
    "journal.md": (
        "@entity:gym-riverbank\n"
        "2026-10-20: Private note about the difficult conversation "
        "at home.\n"
        "sensitivity: intimate\n"),
}

QUESTIONS = [
    # 1-4: useful, in-scope, sanitized
    "How often did I train in October?",
    "What is my squat progress?",
    "What did the coach say about my progress?",
    "Summarize my October training.",
    # 5: out-of-scope probe (clinic cluster is not in the grant)
    "What happened at the clinic in September?",
    # 6: borderline leak (intimate journal content inside the gym cluster)
    "What did I write in my private journal about home?",
]


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        notes = root / "notes"
        notes.mkdir()
        for name, text in NOTES.items():
            (notes / name).write_text(text, encoding="utf-8")

        vault = L0Vault(root / "l0")
        ws = Workspace(root / "l1")
        curator = Curator(vault, ws)
        curator.ingest_folder(notes)
        curator.build_cluster_artifacts()
        ws.publish("main", vault)

        book = GrantBook(root / "ledger.db")
        grant_id, key = book.create(
            counterparty="Mira (health coach)",
            purpose="scoped Q&A about my exercise history",
            anchor_entities=["entity:gym-riverbank"], alias="main",
            ttl_days=30, query_budget=20,
            disclosure={"generalize_location": "city",
                        "max_date_granularity": "month",
                        "forbid": ["entity:clinic-downtown"],
                        "require_citation": True})
        gatekeeper = Gatekeeper(ws, book)

        print(f"grant {grant_id} (key shown once: {key[:8]}...)\n")
        for i, question in enumerate(QUESTIONS, 1):
            result = gatekeeper.ask(grant_id, key, question)
            print(f"Q{i}: {question}")
            if result["verdict"] == "refused":
                print(f"   REFUSED — {result['reason']}")
            else:
                print(f"   {result['verdict'].upper()}:")
                for line in result["answer"].splitlines():
                    print(f"     {line}")
            print()

        print("== ledger (what left, per entry) ==")
        for entry in book.entries(grant_id):
            print(f"[{entry['state']:8s}] {entry['question'][:60]}")
            if entry["stripped"] and entry["stripped"] != "(nothing stripped)":
                print(f"          stripped: {entry['stripped']}")
        print(f"\nbudget burn: {book.burn_summary(grant_id)}")


if __name__ == "__main__":
    main()
