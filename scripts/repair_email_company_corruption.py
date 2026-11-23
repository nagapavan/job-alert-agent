"""Repair companies minted from department / location / phrase text instead of the employer.

The Gmail job-alert ingestion could take a line from the alert body as the employer, so rows like
`AI CoE`, `FDE`, `Remote`, `Python is a must`, `Applying at Avalara` and `GitLab was submitted` were
created. LinkedIn alert digests have a regular block just above `View job:`:

    <role>
    <company>
    <location>
    [Top applicant | Apply with resume & profile | Be an early applicant]
    View job: https://www.linkedin.com/comm/jobs/view/...

This script recovers the employer (and a better title/location) from that block, moves the job to
the real company, and deletes companies left with no jobs.

Usage:
    ./.venv/bin/python scripts/repair_email_company_corruption.py            # dry run
    ./.venv/bin/python scripts/repair_email_company_corruption.py --apply    # write

Never touches a row whose employer cannot be recovered; those are listed as SKIP for manual review.
"""

import argparse
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Importing the app module runs init_db() at import; the scheduler must stay off so a
# data-repair run cannot fire a live browser scan while it mutates rows.
os.environ.setdefault("JOB_ALERT_AGENT_NO_SCHEDULER", "1")
import backend.main  # noqa: F401 - runs init_db() so SessionLocal is bound
from backend.database import Company, Job, SessionLocal

# Lines that appear inside a LinkedIn digest block but are not role/company/location.
_DIGEST_NOISE = {
    "top applicant",
    "apply with resume & profile",
    "be an early applicant",
    "easy apply",
    "actively recruiting",
    "viewed",
    "promoted",
    "new",
}

_APPLYING_PREFIX_RE = re.compile(r"^applying\s+(?:at|to)\s+", re.I)

# Labels that sit inside a digest block and are not a location.
_NOT_A_LOCATION = {
    "fast growing",
    "edge",
    "new",
    "promoted",
    "top applicant",
    "easy apply",
    "be an early applicant",
    "actively recruiting",
}


# Digest lines that are section labels, not employers.
_NOT_A_COMPANY = {"fde times", "edge", "new", "promoted", "top applicant"}

# Rows whose employer is only recoverable by hand (no digest block in the stored text).
EXPLICIT_FIXES = {
    # "Thanks for submitting your availability to GitLab for Staff Software Engineer - NLP."
    775: {"company": "gitlab", "title": "Staff Software Engineer - NLP"},
    # LinkedIn alert wrote the employer tagline into the company field.
    238: {"company": "NST Cyber"},
}


def _location_is_plausible(location: str) -> bool:
    """A digest block is only trusted when its third line reads as a real place."""
    loc = (location or "").strip()
    if not loc or loc.lower() in _NOT_A_LOCATION:
        return False
    if len(loc) > 60:
        return False
    try:
        from backend.geo import is_place_name, resolve_countries

        if is_place_name(loc) or resolve_countries(loc):
            return True
    except Exception:
        return False
    # "Bengaluru, Karnataka, India (Hybrid)" style values still carry a place token.
    return any(is_place_name(part.strip(" ()")) for part in re.split(r"[,/|-]", loc) if part.strip())


def parse_digest(description: str):
    """Return (role, company, location) from a LinkedIn alert digest, else None."""
    text = (description or "").replace("\r", "")
    idx = text.find("View job:")
    if idx == -1:
        return None
    lines = [ln.strip() for ln in text[:idx].split("\n")]
    block = []
    for line in reversed([ln for ln in lines if ln]):
        low = line.lower()
        if low.startswith("your job alert") or "match your preferences" in low:
            break
        if low in _DIGEST_NOISE:
            continue
        block.append(line)
    block.reverse()
    if len(block) >= 3:
        return block[-3], block[-2], block[-1]
    if len(block) == 2:
        return block[0], block[1], None
    return None


def cleaned_company(name: str) -> str:
    """Strip an `Applying at/to <employer>` prefix; otherwise return the name untouched."""
    raw = (name or "").strip()
    if not _APPLYING_PREFIX_RE.match(raw):
        return raw
    return _APPLYING_PREFIX_RE.sub("", raw).strip(" .,-")


def looks_like_department(name: str) -> bool:
    """Single-token / phrase names that are departments, locations or requirement text."""
    n = (name or "").strip()
    if not n:
        return True
    if _APPLYING_PREFIX_RE.match(n):
        return True
    if backend.main._company_looks_like_a_sentence(n):
        return True
    return False


def plan(db):
    """Build the repair plan: list of (job, target_company_name, new_title, new_location, reason)."""
    moves, skips = [], []
    for job in db.query(Job).order_by(Job.id).all():
        comp = job.company
        if comp is None:
            continue
        name = comp.name
        if job.id in EXPLICIT_FIXES:
            fix = EXPLICIT_FIXES[job.id]
            moves.append(
                (job, fix["company"], fix.get("title", job.title), job.location, "explicit")
            )
            continue
        digest = parse_digest(job.description)
        if digest:
            role, company, location = digest
            target = cleaned_company(company)
            if not target or target.lower() == name.lower():
                continue
            if not _location_is_plausible(location or ""):
                skips.append((job, name, f"digest block not trustworthy (location={location!r})"))
                continue
            if len(target.split()) > 5 or target.lower() in _NOT_A_COMPANY:
                skips.append((job, name, f"digest company not trustworthy ({target!r})"))
                continue
            moves.append((job, target, role, location, "digest"))
            continue
        target = cleaned_company(name)
        if target and target.lower() != name.lower():
            moves.append((job, target, job.title, job.location, "applying-prefix"))
            continue
        if looks_like_department(name):
            skips.append((job, name, "no employer recoverable from the stored text"))
    return moves, skips


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="write the changes")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        moves, skips = plan(db)
        print(f"{len(moves)} job(s) to re-home, {len(skips)} unrecoverable\n")
        collisions = 0
        for job, target, title, location, reason in moves:
            dup = (
                db.query(Job)
                .join(Company, Job.company_id == Company.id)
                .filter(Company.name.ilike(target))
                .filter(Job.title.ilike(title or job.title))
                .filter(Job.id != job.id)
                .count()
            )
            if dup:
                collisions += 1
            print(
                f"  job {job.id}: {job.company.name!r} -> {target!r} | "
                f"title {job.title!r} -> {title!r} | loc {job.location!r} -> {location!r} ({reason})"
                + (f" [DUPLICATE of {dup} existing row(s)]" if dup else "")
            )
        if collisions:
            print(f"\n  {collisions} move(s) would land next to an existing same-title row (kept both).")
        for job, name, why in skips:
            if job.id in EXPLICIT_FIXES:
                continue
            print(f"  SKIP job {job.id} ({name!r}): {why}")

        if not args.apply:
            print("\nDry run. Re-run with --apply to write.")
            return 0

        rehomed = 0
        for job, target, title, location, _reason in moves:
            comp = db.query(Company).filter(Company.name.ilike(target)).first()
            if not comp:
                comp = Company(name=target, domain=f"{target.lower().replace(' ', '')}.com")
                db.add(comp)
                db.flush()
            if title and len(title) >= len(job.title):
                job.title = title
            if location and job.location in (None, "", "Remote / Various"):
                job.location = location
            job.company_id = comp.id
            rehomed += 1
        db.commit()

        # Only drop a company that (a) lost its last job to a re-home in THIS run or (b) is a
        # department/phrase name, and never one the operator configured with a careers_url - those
        # are scan targets that simply have no discovered jobs yet.
        sources = {job.company_id for job, *_rest in moves}
        removed = 0
        for comp in db.query(Company).all():
            if comp.id not in sources and not looks_like_department(comp.name):
                continue
            if (comp.careers_url or "").strip():
                continue
            if db.query(Job).filter(Job.company_id == comp.id).count() != 0:
                continue
            print(f"  removing empty company {comp.id} {comp.name!r}")
            db.delete(comp)
            removed += 1
        db.commit()
        print(f"\nRe-homed {rehomed} job(s); removed {removed} empty company row(s).")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
