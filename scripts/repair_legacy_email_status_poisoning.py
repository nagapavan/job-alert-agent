#!/usr/bin/env python
"""Revert rows whose interview stage came from a legacy email writer (2026-11-16).

The evidence notes identify the writer. ``interview_invite`` events and
"Gmail status update detected:" descriptions do not exist anywhere in the current codebase — they
survive only in ``.git/lost-found`` blobs (older uncommitted versions of ``backend/main.py``).
Every row whose only outcome evidence is one of those fingerprints is residue from a pre-#76 sync,
and the current pipeline stages all of those mails instead of applying them.

    ./.venv/bin/python scripts/repair_legacy_email_status_poisoning.py           # dry run
    ./.venv/bin/python scripts/repair_legacy_email_status_poisoning.py --apply

Each revert writes a ``maintenance_repair`` event so the dossier explains the correction.
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("JOB_ALERT_AGENT_NO_SCHEDULER", "1")

# job id -> (expected current status, status to restore, why)
TARGETS = {
    102: ("Interview", "Applied", "applied_at is real; the only interview evidence is a LinkedIn digest"),
    109: ("Interview", "Shortlisted", "no application evidence; legacy 'Gmail status update detected' writer"),
    164: ("Interview", "To Apply", "no application evidence; the interview evidence is a LinkedIn digest"),
    183: ("Interview", "Shortlisted", "the interview mail is about a different employer (Confluent)"),
    187: ("Interview", "Shortlisted", "the mail is an interview feedback survey, not an invitation"),
    766: ("Interview", "Shortlisted", "the interview evidence is a LinkedIn digest"),
}

# Writers that no longer exist in the codebase. The current writer emits
# "Mail <date> from <sender>: <from> → <to> — '<subject>'" with an ``email_<type>`` event type.
LEGACY_FINGERPRINTS = (
    "interview_invite",
    "Gmail status update detected",
    "Interview invite email received",
    "Application confirmation email received",
    "Decision email received",
)


def _legacy_evidence(db, job_id: int) -> str:
    """The legacy evidence note on this row, or '' when the row has none."""
    from backend.database import ApplicationEvent

    for event in (
        db.query(ApplicationEvent)
        .filter(ApplicationEvent.job_id == job_id)
        .order_by(ApplicationEvent.id.desc())
        .all()
    ):
        blob = f"{event.event_type} {event.description or ''}"
        if any(fingerprint in blob for fingerprint in LEGACY_FINGERPRINTS):
            return blob.strip()[:160]
    return ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write the changes (default: dry run)")
    args = parser.parse_args()

    import backend.main  # noqa: F401 - binds SessionLocal via init_db()
    from backend.database import ApplicationEvent, Job, SessionLocal

    db = SessionLocal()
    try:
        changed = 0
        for job_id, (expected, target, why) in sorted(TARGETS.items()):
            job = db.get(Job, job_id)
            if job is None:
                print(f"job {job_id}: missing — skipped")
                continue
            company = job.company.name if job.company else "?"
            if job.status != expected:
                print(f"job {job_id} ({company}): status is '{job.status}', expected '{expected}' — skipped")
                continue
            evidence = _legacy_evidence(db, job_id)
            if not evidence:
                print(f"job {job_id} ({company}): no legacy evidence fingerprint — skipped")
                continue
            print(f"job {job_id} ({company}) {job.title[:45]!r}: {job.status} -> {target}")
            print(f"    evidence: {evidence}")
            print(f"    reason:   {why}")
            if not args.apply:
                continue
            job.status = target
            # The interview stage is being retracted, so the timestamp that asserted it goes too.
            job.interview_scheduled_at = None
            db.add(
                ApplicationEvent(
                    job_id=job_id,
                    event_type="maintenance_repair",
                    description=(
                        f"Corrected status '{expected}' -> '{target}': {why}. The evidence note was "
                        "written by a legacy email writer that no longer exists in the codebase; "
                        "the current pipeline stages those mails for review."
                    ),
                )
            )
            db.commit()
            changed += 1
        if args.apply:
            print(f"\n{changed} row(s) reverted.")
        else:
            print("\nDry run — pass --apply to write.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
