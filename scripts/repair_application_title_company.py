"""Re-derive job title + company for application rows whose employer was never resolved.

Email ingestion sometimes stored the ATS/relay/aggregator as the employer (`Hirist`, `Kekamail`,
`Myworkday`, `LinkedIn`) or a phrase from the subject (`Staff Engineer role`). The stored
description usually still contains the real employer in one of two shapes:

  A. "<Employer> <Role>: <blurb>"            (LinkedIn opportunity digest)
  B. "applying to <Employer> for the role of <Role>"   (careers-portal confirmation)

This script re-parses those rows, moves them to the real employer, and fixes the title. Rows with no
recoverable employer are reported as SKIP — never guessed.

Usage:
    ./.venv/bin/python scripts/repair_application_title_company.py            # dry run
    ./.venv/bin/python scripts/repair_application_title_company.py --apply
"""

import argparse
import os
import html
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Importing the app module runs init_db() at import; the scheduler must stay off so a
# data-repair run cannot fire a live browser scan while it mutates rows.
os.environ.setdefault("JOB_ALERT_AGENT_NO_SCHEDULER", "1")
import backend.main as main  # noqa: F401 - runs init_db()
from backend.database import Company, Job, SessionLocal

APPLICATION_STATUSES = ("Applied", "Screening", "Interview", "Offered", "Rejected")

# ATS relays / aggregators that are never the employer in an application email.
RELAY_COMPANIES = {
    "hirist",
    "kekamail",
    "myworkday",
    "workday",
    "greenhouse",
    "lever",
    "linkedin",
    "indeed",
    "recruiterflowmail",
    "jobgether",
    "karat",
    "smartrecruiters",
    "naukri",
    "instahyre",
}

_DIGEST_HEADER_RE = re.compile(r"^(?P<employer>[A-Z][\w&.,'’\- ]{1,40}?)\s+(?P<role>[A-Z][^:]{3,70}?):\s")
_PORTAL_APPLY_RE = re.compile(
    r"applying to\s+(?P<employer>[A-Z][\w&.,'’\- ]{1,40}?)\s+for the role of\s+"
    r"(?P<role>[^.\n]{3,80})",
    re.I,
)


def _clean_role(role: str) -> str:
    role = html.unescape(role or "").strip(" ,.")
    # "…& Architect through our career portal" -> "…& Architect"
    role = re.split(r"\s+(?:through|via)\s+", role, flags=re.I)[0]
    return role.strip(" ,.")


def parse_employer_and_role(description: str):
    text = html.unescape((description or "").replace("\r", "").replace("\u200b", "")).strip()
    if not text:
        return None
    portal = _PORTAL_APPLY_RE.search(text)
    if portal:
        return portal.group("employer").strip(" ,."), _clean_role(portal.group("role"))
    head = text.split("\n", 1)[0]
    digest = _DIGEST_HEADER_RE.match(head)
    if digest:
        return digest.group("employer").strip(" ,."), _clean_role(digest.group("role"))
    return None


def needs_repair(job) -> bool:
    name = (job.company.name if job.company else "") or ""
    low = name.lower()
    return (
        low in RELAY_COMPANIES
        or main._company_looks_like_a_sentence(name)
        or low.endswith((" role", " position"))
        or not name
    )


def main_cli() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        fixes, skips = [], []
        for job in db.query(Job).filter(Job.status.in_(APPLICATION_STATUSES)).all():
            if not needs_repair(job):
                continue
            parsed = parse_employer_and_role(job.description)
            if not parsed:
                skips.append(job)
                continue
            employer, role = parsed
            if not employer or len(employer.split()) > 5:
                skips.append(job)
                continue
            fixes.append((job, employer, role))

        print(f"{len(fixes)} application row(s) re-parsable, {len(skips)} need manual input\n")
        for job, employer, role in fixes:
            print(
                f"  job {job.id}: {job.company.name!r} -> {employer!r} | "
                f"{job.title!r} -> {role!r} | status={job.status}"
            )
        for job in skips:
            print(f"  SKIP job {job.id} ({job.company.name!r} / {job.title!r}): no employer in stored text")

        if not args.apply:
            print("\nDry run. Re-run with --apply to write.")
            return 0

        moved = 0
        for job, employer, role in fixes:
            comp = db.query(Company).filter(Company.name.ilike(employer)).first()
            if not comp:
                comp = Company(name=employer, domain=f"{employer.lower().replace(' ', '')}.com")
                db.add(comp)
                db.flush()
            job.company_id = comp.id
            if role and len(role) >= len(job.title or ""):
                job.title = role
            moved += 1
        db.commit()
        print(f"\nRe-parsed {moved} application row(s).")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main_cli())
