"""
One-off maintenance: repair LinkedIn saved-job rows ingested before the card-parser fix.

Early saved-job ingests (before ``_parse_linkedin_job_card_text`` handled LinkedIn's collapsed
single-line cards) stored jumbled titles like
``"Solutions Architect-3FedEx ACC · HyderabadReposted 1w ago"`` and a placeholder company
(``"Unknown Company"``). This script performs a best-effort, deterministic repair:

  * strips LinkedIn recency labels ("Reposted 1w ago") from titles,
  * recovers the location from the ``" · <location>"`` suffix,
  * recovers title + employer from the LinkedIn job URL slug (``.../<title>-at-<company>-<id>``),
  * re-links the job to the correct ``Company`` (matched by normalized name, else created),
  * records an audit ``ApplicationEvent`` per repaired job.

Dry-run by default; pass ``--apply`` to write. It only touches rows under
``source == "LinkedIn Saved Jobs"`` / ``source_type == "LinkedIn"`` that look malformed.

Usage:
    python -m backend.scripts.repair_linkedin_saved_jobs            # dry-run
    python -m backend.scripts.repair_linkedin_saved_jobs --apply
"""
import datetime
import re
import sys

from sqlalchemy.orm import sessionmaker

from backend.database import init_db, Job, Company, ApplicationEvent
from backend.playwright_app import _LINKEDIN_RECENCY_RE

_UNKNOWN_COMPANIES = {"", "unknown company", "linkedin network", "linkedin"}

# Keep common acronyms upper-cased when titleizing a URL slug.
_ACRONYMS = {
    "ai": "AI", "ml": "ML", "ui": "UI", "ux": "UX", "api": "API", "apis": "APIs",
    "swe": "SWE", "sre": "SRE", "qa": "QA", "ios": "iOS", "devops": "DevOps",
    "sdk": "SDK", "sql": "SQL", "aws": "AWS", "gcp": "GCP", "seo": "SEO", "crm": "CRM",
    "ii": "II", "iii": "III", "iv": "IV",
}


def _normalize(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def _titleize(slug: str) -> str:
    words = []
    for raw in (slug or "").split("-"):
        if not raw:
            continue
        low = raw.lower()
        if low in _ACRONYMS:
            words.append(_ACRONYMS[low])
        elif raw.isdigit():
            words.append(raw)
        else:
            words.append(raw[:1].upper() + raw[1:])
    return " ".join(words)


def _parse_linkedin_slug(url: str):
    """Returns (title_slug, company_slug) from a LinkedIn ``/jobs/view/`` URL, else ("", "")."""
    m = re.search(r"/jobs/view/([^/?#]+)", url or "")
    if not m:
        return "", ""
    slug = m.group(1)
    # Greedy title so we split on the LAST "-at-" (title may legitimately contain "-at-").
    m2 = re.search(r"^(?P<title>.+)-at-(?P<company>.+?)-(\d{6,})$", slug)
    if not m2:
        m2 = re.search(r"^(?P<title>.+)-at-(?P<company>.+)$", slug)
    if not m2:
        return "", ""
    return m2.group("title"), m2.group("company")


def _is_malformed(title: str, company_name: str) -> bool:
    if (company_name or "").strip().lower() in _UNKNOWN_COMPANIES:
        return True
    if " · " in (title or ""):
        return True
    return bool(_LINKEDIN_RECENCY_RE.search(title or ""))


def repair_linkedin_saved_jobs(db, apply: bool = False) -> dict:
    """Repairs malformed LinkedIn saved-job rows. Returns a summary dict."""
    jobs = db.query(Job).filter(
        (Job.source == "LinkedIn Saved Jobs") | (Job.source_type == "LinkedIn")
    ).all()

    summary = {"scanned": len(jobs), "candidates": 0, "repaired": 0,
               "company_fixed": 0, "skipped": 0}
    now = datetime.datetime.now(datetime.timezone.utc)

    for job in jobs:
        company_name = job.company.name if job.company else ""
        if not _is_malformed(job.title or "", company_name):
            continue
        summary["candidates"] += 1

        # Baseline: strip recency + recover the " · <location>" suffix.
        new_title = _LINKEDIN_RECENCY_RE.sub("", job.title or "").strip().strip(" ·-")
        new_location = job.location
        if " · " in new_title:
            left, _, right = new_title.rpartition(" · ")
            new_title = left.strip().strip(" ·-")
            if right.strip(" ·-"):
                new_location = right.strip().strip(" ·-")

        # Prefer the URL slug: it cleanly separates "<title>-at-<company>-<id>".
        title_slug, company_slug = _parse_linkedin_slug(job.url or "")
        if title_slug:
            new_title = _titleize(title_slug)

        derived_company = company_slug.replace("-", " ").strip() if company_slug else ""
        target_company = None
        if derived_company and company_name.strip().lower() in _UNKNOWN_COMPANIES:
            target_company = db.query(Company).filter(Company.name.ilike(derived_company)).first()
            if not target_company:
                norm = _normalize(derived_company)
                for c in db.query(Company).all():
                    if _normalize(c.name) == norm:
                        target_company = c
                        break
            if not target_company and apply:
                target_company = Company(
                    name=_titleize(company_slug),
                    domain=f"{_normalize(company_slug)}.com",
                )
                db.add(target_company)
                db.flush()

        changed_title = bool(new_title) and new_title != job.title
        changed_loc = bool(new_location) and new_location != job.location
        changed_company = bool(target_company and job.company_id != target_company.id)

        if not (changed_title or changed_loc or changed_company):
            summary["skipped"] += 1
            continue

        if apply:
            if changed_title:
                job.title = new_title
            if changed_loc:
                job.location = new_location
            if changed_company:
                job.company_id = target_company.id
                summary["company_fixed"] += 1
            db.add(ApplicationEvent(
                job_id=job.id,
                event_type="maintenance_repair",
                description="Repaired malformed LinkedIn saved-job ingest (title/location/company).",
                timestamp=now,
            ))
        else:
            print(
                f"[dry-run] job {job.id}: title {job.title!r} -> {new_title!r}; "
                f"location {job.location!r} -> {new_location!r}; "
                f"company {company_name!r} -> "
                f"{(target_company.name if target_company else company_name)!r}"
            )
        summary["repaired"] += 1

    if apply:
        db.commit()
    return summary


def run_repair(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    apply = "--apply" in argv
    engine = init_db()
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        summary = repair_linkedin_saved_jobs(db, apply=apply)
        mode = "APPLIED" if apply else "DRY-RUN"
        print(
            f"[{mode}] scanned={summary['scanned']} candidates={summary['candidates']} "
            f"repaired={summary['repaired']} company_fixed={summary['company_fixed']} "
            f"skipped={summary['skipped']}"
        )
        if not apply:
            print("Re-run with --apply to write changes.")
    finally:
        db.close()


if __name__ == "__main__":
    run_repair()
