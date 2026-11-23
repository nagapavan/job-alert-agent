#!/usr/bin/env python
"""Fix the four outcome rows the 2026-11-16 stage audit found (dry-run by default).

Audit result (68 outcome rows): two misattributed, two incomplete. Everything else — the 21
applications with an ``applied_at``, the three real interviews, the LinkedIn "position expired"
auto-prunes and the ``requisition_closed`` probes — is evidence-backed and is left alone.

    ./.venv/bin/python scripts/repair_misattributed_outcome_rows.py           # dry run
    ./.venv/bin/python scripts/repair_misattributed_outcome_rows.py --apply
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("JOB_ALERT_AGENT_NO_SCHEDULER", "1")

SALESFORCE_COMPANY_ID = 26


def _event_at(db, job_id: int, event_type: str):
    from backend.database import ApplicationEvent

    event = (
        db.query(ApplicationEvent)
        .filter(ApplicationEvent.job_id == job_id, ApplicationEvent.event_type == event_type)
        .order_by(ApplicationEvent.id.asc())
        .first()
    )
    return event.timestamp if event else None


def _repoint_to_salesforce(db) -> str:
    """job 1545: a Workday confirmation for Salesforce req R262578 filed under Analog Devices."""
    from backend.database import ApplicationEvent, Company, Job

    job = db.get(Job, 1545)
    if job is None:
        return "job 1545: missing — skipped"
    if job.company_id == SALESFORCE_COMPANY_ID:
        return "job 1545: already re-pointed — skipped"
    if job.company is None or job.company.name != "Analog Devices":
        return f"job 1545: company is {job.company.name if job.company else '?'!r} — skipped"
    salesforce = db.get(Company, SALESFORCE_COMPANY_ID)
    if salesforce is None:
        return "job 1545: Salesforce company row missing — skipped"
    job.company_id = SALESFORCE_COMPANY_ID
    if not job.rejected_at:
        job.rejected_at = _event_at(db, 1545, "email_rejection")
    db.add(
        ApplicationEvent(
            job_id=1545,
            event_type="maintenance_repair",
            description=(
                "Employer corrected: 'Analog Devices' -> 'Salesforce'. Requisition R262578 is a "
                "Salesforce requisition; the Workday confirmation was attributed to the wrong "
                "employer."
            ),
        )
    )
    db.commit()
    return "job 1545: re-pointed to Salesforce (status kept: Rejected)"


def _revert_confluent_rejection(db) -> str:
    """job 433: a Confluent message reply was filed as a Rightmove rejection."""
    from backend.database import ApplicationEvent, Job

    job = db.get(Job, 433)
    if job is None:
        return "job 433: missing — skipped"
    if job.status != "Rejected":
        return f"job 433: status is {job.status!r} — skipped"
    evidence = _event_at(db, 433, "email_rejection")
    if evidence is None:
        return "job 433: no email_rejection event — skipped"
    job.status = "Shortlisted"
    job.rejected_at = None
    db.add(
        ApplicationEvent(
            job_id=433,
            event_type="maintenance_repair",
            description=(
                "Corrected status 'Rejected' -> 'Shortlisted': the only rejection evidence is a "
                "Confluent message reply ('Principal Engineer: Re-imagine Observability at "
                "Confluent'), not a Rightmove decision."
            ),
        )
    )
    db.commit()
    return "job 433: Rejected -> Shortlisted (Confluent message removed as evidence)"


def _revert_wrong_salesforce_repoint(db) -> str:
    """Undo the re-point: R262578 is an *Analog Devices* requisition, not Salesforce.

    Verified against the source mail: sender ``Workday Analog Devices
    <analogdevices@myworkday.com>``, subject "Thank you for applying - R262578 Principal System
    Software Architect (Closed)". The earlier re-point inferred Salesforce from a foreign event
    that had bled onto the Salesforce row — one wrong inference stacked on another.
    """
    from backend.database import ApplicationEvent, Company, Job

    job = db.get(Job, 1545)
    if job is None:
        return "job 1545: missing — skipped"
    if job.company_id == 611:
        return "job 1545: already Analog Devices — skipped"
    analog = db.get(Company, 611)
    if analog is None or analog.name != "Analog Devices":
        return "job 1545: Analog Devices company row missing — skipped"
    job.company_id = 611
    db.add(
        ApplicationEvent(
            job_id=1545,
            event_type="maintenance_repair",
            description=(
                "Employer corrected: 'Salesforce' -> 'Analog Devices'. The source mail is from "
                "'Workday Analog Devices <analogdevices@myworkday.com>' for requisition R262578; "
                "the Salesforce attribution was inferred from a foreign event on another row."
            ),
        )
    )
    db.commit()
    return "job 1545: re-pointed back to Analog Devices (status kept: Rejected)"


def _drop_foreign_events_from_salesforce_row(db) -> str:
    """job 1458 (Salesforce PMTS) carries two events that belong to other employers.

    - "Thank you for Applying!" is from ``walmart@myworkday.com`` (5 Nov), not Salesforce.
    - The R262578 rejection is Analog Devices' (12 Nov); job 1545 already holds it.

    The row's own evidence is the Salesforce prep mail from ``salesforce@myworkday.com``
    (10 Nov), so the status stays Interview and ``applied_at`` moves to that mail's date.
    """
    import datetime as dt

    from backend.database import ApplicationEvent, Job

    job = db.get(Job, 1458)
    if job is None:
        return "job 1458: missing — skipped"
    foreign = []
    for event in db.query(ApplicationEvent).filter(ApplicationEvent.job_id == 1458).all():
        blob = event.description or ""
        if event.event_type.startswith("email_") and (
            "Thank you for Applying!" in blob or "R262578" in blob
        ):
            foreign.append(event)
    if not foreign:
        return "job 1458: no foreign events left — skipped"
    removed = ", ".join(f"#{e.id} {e.event_type}" for e in foreign)
    for event in foreign:
        db.delete(event)
    job.applied_at = dt.datetime(2026, 11, 10, 11, 56)  # the Salesforce prep mail's date
    for event in db.query(ApplicationEvent).filter(ApplicationEvent.job_id == 1458).all():
        if event.event_type == "maintenance_repair" and "Backfilled the application date" in (
            event.description or ""
        ):
            event.description = (
                "Backfilled the application date from the Salesforce interview mail "
                "(10 Nov 2026): an interview proves an application was made."
            )
    db.add(
        ApplicationEvent(
            job_id=1458,
            event_type="maintenance_repair",
            description=(
                f"Removed foreign evidence ({removed}): 'Thank you for Applying!' is from "
                "walmart@myworkday.com and the R262578 rejection is Analog Devices' requisition "
                "(kept on job 1545). The Salesforce evidence is the interview-prep mail from "
                "salesforce@myworkday.com dated 10 Nov 2026."
            ),
        )
    )
    db.commit()
    return f"job 1458: removed {len(foreign)} foreign event(s); applied_at -> 2026-11-10"


def _backfill_applied_at(db, job_id: int) -> str:
    """An interview mail proves an application; record when it arrived."""
    from backend.database import ApplicationEvent, Job

    job = db.get(Job, job_id)
    if job is None:
        return f"job {job_id}: missing — skipped"
    if job.applied_at:
        return f"job {job_id}: applied_at already set — skipped"
    when = _event_at(db, job_id, "email_interview") or _event_at(db, job_id, "interview_invite")
    if when is None:
        return f"job {job_id}: no interview event — skipped"
    job.applied_at = when
    db.add(
        ApplicationEvent(
            job_id=job_id,
            event_type="maintenance_repair",
            description=(
                "Backfilled the application date from the interview mail: an interview proves an "
                "application was made, and the row showed 'Applied: N/A'."
            ),
        )
    )
    db.commit()
    return f"job {job_id}: applied_at backfilled from {when:%Y-%m-%d}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write the changes (default: dry run)")
    args = parser.parse_args()

    import backend.main  # noqa: F401 - binds SessionLocal via init_db()
    from backend.database import SessionLocal

    db = SessionLocal()
    try:
        if not args.apply:
            print("Dry run — planned changes:")
            for job_id, note in (
                (1545, "re-point back to Analog Devices (R262578 is an AD requisition), keep Rejected"),
                (1458, "drop the Walmart confirmation + AD rejection, applied_at -> 2026-11-10"),
                (433, "Rejected -> Shortlisted (Confluent message is not a Rightmove decision)"),
                (775, "backfill applied_at from the interview mail"),
                (1458, "backfill applied_at from the interview mail"),
            ):
                print(f"  job {job_id}: {note}")
            print("\nPass --apply to write.")
            return 0

        print(_revert_wrong_salesforce_repoint(db))
        print(_drop_foreign_events_from_salesforce_row(db))
        print(_revert_confluent_rejection(db))
        print(_backfill_applied_at(db, 775))
        print(_backfill_applied_at(db, 1458))
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
