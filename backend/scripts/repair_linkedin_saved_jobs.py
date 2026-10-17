"""
Maintenance migration: repair LinkedIn job rows ingested before the card-parser fix.

Early LinkedIn ingests (before ``_parse_linkedin_job_card_text`` handled LinkedIn's collapsed
single-line cards) stored jumbled titles like
``"Solutions Architect-3FedEx ACC · HyderabadReposted 1w ago"`` and a placeholder company
(``"Unknown Company"`` / ``"LinkedIn Network"``). This is a **migration concern**, not an ingest
concern: the sync paths only enrich newly discovered items, and this script sweeps the
back-catalog.

Two repair modes:

  * **offline (default)** -- deterministic, no browser. Strips LinkedIn recency labels, recovers
    the location from the ``" · <location>"`` suffix, and recovers title + employer from the job
    URL slug (``.../<title>-at-<company>-<id>``). Fast and safe, but cannot fix rows whose URL is
    numeric-only (no slug), which is exactly the shape LinkedIn emits for saved jobs.

  * **--fetch** -- additionally opens the persistent browser profile to read each unresolved row's
    detail page (``[id^="JobDetails_AboutTheJob_"]``) for the real title/company/description. Use
    this for numeric-only-URL rows. Slower, needs the browser profile to be free.

After a row's title/company/description changes, the row is **re-scored** (deterministic engine
first, bounded LLM fallback) so a previously-unscored 0% row gets a real score.

Dry-run by default; pass ``--apply`` to write. Only rows under ``source_type == "LinkedIn"``
(or ``source`` like ``LinkedIn%``) that look malformed are touched.

Usage:
    python -m backend.scripts.repair_linkedin_saved_jobs                 # offline dry-run
    python -m backend.scripts.repair_linkedin_saved_jobs --apply         # offline write
    python -m backend.scripts.repair_linkedin_saved_jobs --fetch --apply # + detail-page repair
    python -m backend.scripts.repair_linkedin_saved_jobs --fetch --apply --limit 25  # bounded slice
"""
import datetime
import json
import os
import random
import re
import sys
import time

from sqlalchemy.orm import sessionmaker

from backend.database import init_db, Job, Company, ApplicationEvent, Resume
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


def _linkedin_source_filter():
    return (Job.source == "LinkedIn Saved Jobs") | (
        Job.source.like("LinkedIn%")
    ) | (Job.source_type == "LinkedIn")


def _resolve_company(db, name: str, apply: bool):
    """Find an existing Company by (case/diacritic-insensitive) name, else create on write."""
    if not name:
        return None
    target = db.query(Company).filter(Company.name.ilike(name)).first()
    if target:
        return target
    norm = _normalize(name)
    for c in db.query(Company).all():
        if _normalize(c.name) == norm:
            return c
    if apply:
        target = Company(name=name, domain=f"{norm}.com")
        db.add(target)
        db.flush()
        return target
    return None


def _fetch_detail(page, url: str) -> dict:
    """Best-effort detail-page read for a single job URL. Returns {} on any failure."""
    try:
        from backend.playwright_app import fetch_linkedin_job_details

        return fetch_linkedin_job_details(page, url) or {}
    except Exception:
        return {}


def repair_linkedin_saved_jobs(
    db, apply: bool = False, fetch: bool = False, page=None, limit: int = 0
) -> dict:
    """Repairs malformed LinkedIn rows, then re-scores the ones whose content changed.

    ``fetch=True`` opens the persistent browser profile (when ``page`` is not supplied) to read
    detail pages for rows the offline slug split cannot resolve.

    ``limit`` bounds how many *candidates* are processed in this run (>0 enables the cap), so a
    large back-catalog can be repaired in resumable slices across several invocations.
    """
    jobs = db.query(Job).filter(_linkedin_source_filter()).all()

    summary = {
        "scanned": len(jobs), "candidates": 0, "repaired": 0,
        "company_fixed": 0, "rescored": 0, "fetched": 0, "skipped": 0,
    }
    now = datetime.datetime.now(datetime.timezone.utc)

    # Resolve resume + scorer lazily so the offline/no-op path never pays for them.
    resume_text = None
    parsed_resume = None
    _score = None

    def _ensure_scorer():
        nonlocal resume_text, parsed_resume, _score
        if _score is not None:
            return _score
        from backend.main import _score_job_with_engine, _parsed_resume_of
        from backend.config import decrypt_data

        active = db.query(Resume).filter(Resume.is_active == True).first()
        try:
            resume_text = decrypt_data(active.content_encrypted) if active else ""
        except Exception:
            resume_text = ""
        parsed_resume = _parsed_resume_of(active)
        _score = _score_job_with_engine
        return _score

    own_playwright = None
    own_context = None
    if fetch and page is None:
        try:
            from playwright.sync_api import sync_playwright
            from backend.playwright_app import get_persistent_browser_context

            own_playwright = sync_playwright().start()
            own_context = get_persistent_browser_context(own_playwright, headless=True)
            page = (
                own_context.pages[0]
                if (hasattr(own_context, "pages") and isinstance(own_context.pages, list) and own_context.pages)
                else own_context.new_page()
            )
        except Exception as e:
            print(f"[warn] browser unavailable for --fetch: {e}; continuing offline only")
            page = None

    try:
        for job in jobs:
            company_name = job.company.name if job.company else ""
            if not _is_malformed(job.title or "", company_name):
                continue
            if limit and summary["candidates"] >= limit:
                break
            summary["candidates"] += 1

            original = (job.title or "", job.location or "", job.company_id, job.description or "")

            # --- Tier 1: offline normalization (recency + " · <location>" suffix). ---
            new_title = _LINKEDIN_RECENCY_RE.sub("", job.title or "").strip().strip(" ·-")
            new_location = job.location
            if " · " in new_title:
                left, _, right = new_title.rpartition(" · ")
                new_title = left.strip().strip(" ·-")
                if right.strip(" ·-"):
                    new_location = right.strip().strip(" ·-")

            # --- Tier 2: URL slug (authoritative for title + employer when present). ---
            title_slug, company_slug = _parse_linkedin_slug(job.url or "")
            derived_company = _titleize(company_slug) if company_slug else ""
            if title_slug:
                new_title = _titleize(title_slug)

            new_company = None
            if derived_company:
                new_company = _resolve_company(db, derived_company, apply)

            new_desc = job.description or ""

            # --- Tier 3 (--fetch): detail page for rows the slug cannot resolve. ---
            needs_fetch = fetch and page is not None and (
                not title_slug
                or not derived_company
                or (company_name or "").strip().lower() in _UNKNOWN_COMPANIES
            )
            if needs_fetch:
                details = _fetch_detail(page, job.url or "")
                summary["fetched"] += 1
                # Strict inter-request pacing: LinkedIn's anti-automation heuristics flag
                # bursts. Use the configured base pacing (LINKEDIN_AUTOMATION_PACING_SECONDS,
                # default 2.5s) plus 0-50% jitter so the cadence looks human and isn't a
                # fixed interval. See docs/ATS_PORTALS.md §"Risk tiers" — LinkedIn is a
                # session-driven, off-by-default channel; this script uses the same
                # persistent profile as live sync, so the same risk applies.
                try:
                    base = float(os.environ.get("LINKEDIN_AUTOMATION_PACING_SECONDS", "2.5"))
                except ValueError:
                    base = 2.5
                if base > 0:
                    time.sleep(base * (1.0 + random.random() * 0.5))
                dom_title = (details.get("title") or "").strip()
                dom_company = (details.get("company") or "").strip()
                if dom_title:
                    norm_cur = _normalize(new_title)
                    norm_dom = _normalize(dom_title)
                    if not new_title or (norm_dom and norm_dom in norm_cur) or dom_title != new_title:
                        new_title = dom_title
                if dom_company:
                    resolved = _resolve_company(db, dom_company, apply)
                    if resolved:
                        new_company = resolved
                if details.get("description"):
                    new_desc = details["description"].strip()
                if details.get("canonical_url"):
                    job.url = details["canonical_url"]

            changed_title = bool(new_title) and new_title != job.title
            changed_loc = bool(new_location) and new_location != job.location
            changed_company = bool(new_company and job.company_id != new_company.id)
            changed_desc = bool(new_desc) and new_desc != (job.description or "")
            changed = changed_title or changed_loc or changed_company or changed_desc

            if not changed:
                summary["skipped"] += 1
                continue

            if apply:
                if changed_title:
                    job.title = new_title
                if changed_loc:
                    job.location = new_location
                if changed_company:
                    job.company_id = new_company.id
                    summary["company_fixed"] += 1
                if changed_desc:
                    job.description = new_desc

                # Re-score only when the description actually changed (that is what the scorer
                # reads); a title/company-only fix leaves a valid score intact.
                if changed_desc:
                    scorer = _ensure_scorer()
                    (
                        score, analysis, scored, method, scored_at, band, rec, strengths, gaps, partials
                    ) = scorer(
                        db, resume_text or "", new_desc,
                        parsed_resume=parsed_resume, job_title=new_title or "",
                    )
                    if scored:
                        job.match_score = score
                        job.match_analysis = analysis
                        job.match_scored = True
                        job.score_method = method
                        job.match_scored_at = scored_at
                        job.match_band = band
                        job.apply_recommendation = rec
                        job.match_strengths_json = json.dumps(strengths)
                        job.match_gaps_json = json.dumps(gaps)
                        job.match_partials_json = json.dumps(partials)
                        summary["rescored"] += 1

                db.add(ApplicationEvent(
                    job_id=job.id,
                    event_type="maintenance_repair",
                    description=(
                        "Repaired malformed LinkedIn ingest (title/location/company/description)"
                        + (" and re-scored." if changed_desc else ".")
                    ),
                    timestamp=now,
                ))
            else:
                print(
                    f"[dry-run] job {job.id}: title {job.title!r} -> {new_title!r}; "
                    f"location {job.location!r} -> {new_location!r}; "
                    f"company {company_name!r} -> "
                    f"{(new_company.name if new_company else company_name)!r}"
                    + ("; desc+rescore" if changed_desc else "")
                )
            summary["repaired"] += 1

        if apply:
            db.commit()
    finally:
        if own_context is not None:
            try:
                own_context.close()
            except Exception:
                pass
        if own_playwright is not None:
            try:
                own_playwright.stop()
            except Exception:
                pass

    return summary


def run_repair(argv=None):
    # Must be set before `backend.main` is imported (it boots the scheduler at import time).
    os.environ.setdefault("JOB_ALERT_AGENT_NO_SCHEDULER", "1")
    argv = argv if argv is not None else sys.argv[1:]
    apply = "--apply" in argv
    fetch = "--fetch" in argv
    limit = 0
    if "--limit" in argv:
        try:
            limit = int(argv[argv.index("--limit") + 1])
        except (IndexError, ValueError):
            print("[warn] --limit needs an integer; ignoring")
            limit = 0
    engine = init_db()
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        summary = repair_linkedin_saved_jobs(db, apply=apply, fetch=fetch, limit=limit)
        mode = "APPLIED" if apply else "DRY-RUN"
        tier = "offline+fetch" if fetch else "offline"
        print(
            f"[{mode}/{tier}] scanned={summary['scanned']} candidates={summary['candidates']} "
            f"repaired={summary['repaired']} company_fixed={summary['company_fixed']} "
            f"fetched={summary['fetched']} rescored={summary['rescored']} skipped={summary['skipped']}"
        )
        if not apply:
            print("Re-run with --apply to write changes.")
    finally:
        db.close()


if __name__ == "__main__":
    run_repair()
