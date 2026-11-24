"""Split "role + employer" titles that the LinkedIn saved-jobs card parser concatenated.

The card parser sometimes produced titles like

    "Principal Core Infrastructure Engineer (Verified job)Principal Core Infrastructure Engineer Oracle"
    "Senior Staff EngineerArcesium"
    "Principal Architect - AI Anaplan"

so the employer was never stored and every such row landed under one bucket company
("LinkedIn Network"). The splitting itself lives in ``backend/title_parser.py`` (shared with the
live scanner); this script applies it to rows already in the database and re-homes them, using the
company-synonym table so "Uber Technologies Inc" does not create a second "Uber" row.

Usage:
    ./.venv/bin/python scripts/repair_linkedin_saved_job_titles.py            # dry run
    ./.venv/bin/python scripts/repair_linkedin_saved_job_titles.py --apply
"""

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# The scheduler must stay off: a data-repair run must never fire a live browser scan while it
# mutates rows, and any module that transitively imports the app module would start it.
os.environ.setdefault("JOB_ALERT_AGENT_NO_SCHEDULER", "1")
from backend.database import Company, Job, SessionLocal, init_db  # noqa: E402
from backend.title_parser import is_bucket_company, resolve_company_name, split_role_employer

BUCKET_COMPANIES = {"linkedin network", "unknown company", "linkedin"}

init_db()


def main_cli() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        known = [c.name for c in db.query(Company).all() if c.name]
        fixes, skips = [], []
        for job in db.query(Job).order_by(Job.id).all():
            name = (job.company.name if job.company else "") or ""
            if name.lower() not in BUCKET_COMPANIES:
                continue
            role, employer, confidence = split_role_employer(job.title, known)
            if role and employer and confidence in ("high", "medium"):
                fixes.append((job, role, employer, confidence))
            else:
                skips.append((job, role, employer, confidence))

        print(f"{len(fixes)} title(s) splittable, {len(skips)} need manual input\n")
        for job, role, employer, confidence in fixes:
            print(f"  [{confidence}] {job.id}: {job.title[:58]!r} -> {role[:50]!r} @ {employer!r}")
        for job, role, employer, confidence in skips:
            hint = f" {confidence} guess {role!r} @ {employer!r}" if role else ""
            print(f"  SKIP {job.id}: {job.title[:70]!r}{hint}")

        if not args.apply:
            print("\nDry run. Re-run with --apply to write.")
            return 0

        moved = 0
        for job, role, employer, _confidence in fixes:
            canonical = resolve_company_name(employer, known) or employer
            comp = db.query(Company).filter(Company.name.ilike(canonical)).first()
            if not comp:
                comp = Company(name=canonical)
                db.add(comp)
                db.flush()
                known.append(canonical)
            job.company_id = comp.id
            job.title = role
            moved += 1
        db.commit()
        print(f"\nRe-homed {moved} saved-job row(s).")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main_cli())
