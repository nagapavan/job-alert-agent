"""
Database Clean-up Utility (standalone maintenance script).

Applies one-off corrections to legacy misattributed application statuses (for example,
statuses mis-set by early email scans) and records an audit ``ApplicationEvent`` for each
change. This script is **not** imported by the application, scheduler, or test suite — it is
run manually on demand.

Usage:
    python -m backend.scripts.cleanup_interview_statuses

Configure the corrections you need in ``CORRECTIONS`` below (or call ``run_cleanup(items)``
from your own code). No candidate- or company-specific data is hardcoded here by design; the
table ships empty so each operator supplies their own job ids and target values.
"""
import datetime
from typing import Dict, List, Optional

from sqlalchemy.orm import sessionmaker

from backend.database import init_db, Job, Company, ApplicationEvent


# Each entry targets a Job by id. Provide any of:
#   - to_status:      reset the lifecycle status (also clears applied/interview dates)
#   - company_name:   re-link the job to this company (created if missing)
#   - company_domain: optional domain for a newly created company
#   - reason:         human-readable note stored on the audit event
CORRECTIONS: List[Dict] = [
    # {
    #     "job_id": 123,
    #     "to_status": "To Apply",
    #     "reason": "Corrected legacy email misattribution.",
    # },
    # {
    #     "job_id": 124,
    #     "company_name": "Example Co",
    #     "company_domain": "example.com",
    #     "reason": "Re-linked to the proper company name.",
    # },
]


def run_cleanup(corrections: Optional[List[Dict]] = None) -> None:
    """Applies the supplied corrections (defaults to ``CORRECTIONS``) and commits."""
    corrections = CORRECTIONS if corrections is None else corrections
    engine = init_db()
    Session = sessionmaker(bind=engine)
    db = Session()
    now = datetime.datetime.now(datetime.timezone.utc)

    try:
        for item in corrections:
            job = db.query(Job).filter(Job.id == item.get("job_id")).first()
            if not job:
                continue

            if item.get("company_name"):
                company = (
                    db.query(Company)
                    .filter(Company.name.ilike(item["company_name"]))
                    .first()
                )
                if not company:
                    company = Company(
                        name=item["company_name"],
                        domain=item.get("company_domain", ""),
                    )
                    db.add(company)
                    db.flush()
                job.company_id = company.id

            if item.get("to_status"):
                job.status = item["to_status"]
                job.applied_at = None
                job.interview_scheduled_at = None

            db.add(ApplicationEvent(
                job_id=job.id,
                event_type="status_change",
                description=item.get("reason", "Maintenance correction."),
                timestamp=now,
            ))

        db.commit()
        print("Clean-up completed successfully!")
    finally:
        db.close()


if __name__ == "__main__":
    run_cleanup()
