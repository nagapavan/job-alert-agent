"""Remove jobs that were filed under a relay/scheduling vendor or a junk "company".

A scheduler mail ("… via GoodTime.io") used to become its own application row, and an alert
footer line ("View all jobs: https://…") used to become a job title. Both are data defects, not
behaviour: the code now refuses to create them, so this script only cleans up what already exists.

Dry-run by default. ``--apply`` deletes the job + its events, and the company when it holds no
other jobs.

    ./.venv/bin/python scripts/repair_relay_vendor_rows.py
    ./.venv/bin/python scripts/repair_relay_vendor_rows.py --apply
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.database import ApplicationEvent, Company, Job, init_db  # noqa: E402
from backend.header_segments import classify_segment  # noqa: E402
from backend.scraper import _EMAIL_ATS_DOMAINS, _is_ats_or_freemail_domain  # noqa: E402

# "goodtime.io" -> "goodtime": the vendor's own name, which a mail relay can leak as the company.
_VENDOR_SLDS = {domain.split(".")[0] for domain in _EMAIL_ATS_DOMAINS}


def _junk_company(name: str, domain: str) -> str:
    """Returns the reason a company row is not an employer, or "" when it is fine."""
    cleaned = (name or "").strip()
    if not cleaned:
        return "empty company"
    if _is_ats_or_freemail_domain(domain or "") or _is_ats_or_freemail_domain(
        cleaned.lower().replace(" ", "") + ".com"
    ):
        return "vendor sender domain"
    kind, _value = classify_segment(cleaned)
    # A single-word place is ambiguous ("Harvey", "Austin" are also employers); only a
    # multi-word location phrase is unambiguous junk.
    if kind in ("location", "work_mode") and len(cleaned.split()) > 1:
        return f"company is a {kind}"
    if cleaned.lower().endswith(" jobs") or "://" in cleaned or cleaned.lower().startswith("view all"):
        return "company is a page label"
    if cleaned.lower().replace(" ", "") in _VENDOR_SLDS:
        return "vendor name"
    return ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="delete the rows (default: dry run)")
    args = parser.parse_args()

    init_db()
    # init_db rebinds SessionLocal, so it must be imported after the call.
    from backend.database import SessionLocal

    db = SessionLocal()
    try:
        rows = (
            db.query(Job, Company)
            .join(Company, Company.id == Job.company_id)
            .filter(Job.source.ilike("Gmail%"))
            .order_by(Job.id)
            .all()
        )
        suspects = []
        for job, comp in rows:
            reason = _junk_company(comp.name, comp.domain)
            if reason:
                suspects.append((job, comp, reason))

        if not suspects:
            print("No relay/vendor rows found.")
            return 0

        for job, comp, reason in suspects:
            print(
                f"job {job.id}: {job.title[:60]!r} @ {comp.name!r} "
                f"[{job.status}, {job.source}] — {reason}"
            )

        if not args.apply:
            print(f"\n{len(suspects)} row(s) would be removed. Re-run with --apply.")
            return 0

        removed = 0
        for job, comp, _reason in suspects:
            db.query(ApplicationEvent).filter(ApplicationEvent.job_id == job.id).delete(
                synchronize_session=False
            )
            db.delete(job)
            db.flush()
            if db.query(Job).filter(Job.company_id == comp.id).count() == 0:
                db.delete(comp)
                db.flush()
            removed += 1
        db.commit()
        print(f"Removed {removed} row(s).")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
