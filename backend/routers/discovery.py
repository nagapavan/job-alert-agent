"""ATS portal discovery: the scrape engine, its cancellation flag and the run audit.

Extracted from ``main.py`` because the engine is the only place that fans out over the configured
portals and scores what they return, and because the sync-all runner needs to call it without
holding the app module. Every collaborator it touches (the scrapers, the preference gate, the
scoring pipeline, the pacer) is imported from its owning module, so a test patches the real owner.
"""

import json
import logging
import threading
from collections import namedtuple
from typing import Callable, List, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend import scraper
from backend.ai_helper import generate_embeddings, token_tracker
from backend.company_catalog import get_catalog_company
from backend.config import decrypt_data
from backend.database import (
    CONSENT_REQUIRED_FEATURES,
    Company,
    Job,
    OperationLog,
    Resume,
    get_db,
    get_feature_flags,
    has_consent,
    is_company_excluded,
)
from backend.dismissal import apply_dismissal_filter
from backend.job_scoring import (
    _parsed_resume_of,
    _score_job_with_engine,
    _score_provenance,
)
from backend.observability import sanitize_log_details
from backend.parser import clean_job_description
from backend.preference_filter import PreferenceCriteria, PreferenceFilter
from backend.requisition import extract_requisition_id
from backend.task_engine import CancellationToken

logger = logging.getLogger(__name__)

router = APIRouter(tags=["discovery"])

LATEST_SCRAPE_AUDIT: List[dict] = []

# Cancels the active scrape run; set by the stop route, cleared when a run starts.
scrape_cancel_event = threading.Event()


class ScrapeRequest(BaseModel):
    company_id: Optional[int] = None
    target_titles: Optional[str] = None
    target_locations: Optional[str] = None
    target_cities: Optional[str] = None
    work_mode: Optional[str] = None


class ScrapeResponse(BaseModel):
    jobs_found: int
    jobs_added: int
    companies_updated: int
    message: str


_ATS_PORTAL_SCRAPERS = {
    "greenhouse": "scrape_greenhouse_jobs",
    "lever": "scrape_lever_jobs",
    "ashby": "scrape_ashby_jobs",
    "smartrecruiters": "scrape_smartrecruiters_jobs",
    "workable": "scrape_workable_jobs",
    "workday": "scrape_workday_jobs",
    "adp_workforce_now": "scrape_adp_workforce_now_jobs",
}


_AtsPortal = namedtuple("_AtsPortal", "id label default_flag scraper_attr")


def _load_ats_portals():
    """Returns the configured ATS portals (id, label, default_flag, scraper attr) for dispatch."""
    from backend.ats.engine import load_portal_configs

    portals = []
    for cfg in load_portal_configs():
        attr = _ATS_PORTAL_SCRAPERS.get(cfg.id)
        if attr is None:
            continue
        portals.append(
            _AtsPortal(
                id=cfg.id,
                label=cfg.label,
                default_flag=cfg.default_flag,
                scraper_attr=attr,
            )
        )
    return portals


_ATS_PORTALS = _load_ats_portals()


@router.get("/api/jobs/scrape-audit")
def get_scrape_audit(db: Session = Depends(get_db)):
    """Returns detailed diagnostics of the last job scrape run broken down by company from persistent OperationLogs."""
    latest_op = (
        db.query(OperationLog)
        .filter(OperationLog.operation_type == "ats_scrape")
        .order_by(OperationLog.created_at.desc())
        .first()
    )
    audit_list = []
    if latest_op and latest_op.details_json:
        try:
            details = json.loads(latest_op.details_json)
            audit_list = details.get("audit_records", [])
        except Exception:
            pass
    if not audit_list:
        audit_list = LATEST_SCRAPE_AUDIT

    return {"total_companies": len(audit_list), "audit": audit_list}


@router.post("/api/companies/{company_id}/scrape-jobs", response_model=ScrapeResponse)
def scrape_single_company_jobs(
    company_id: int,
    target_titles: Optional[str] = None,
    target_locations: Optional[str] = None,
    target_cities: Optional[str] = None,
    work_mode: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Scrapes latest job postings for a single target organization with optional
    role/country/city/work-mode filters."""
    return trigger_jobs_scrape(
        ScrapeRequest(
            company_id=company_id,
            target_titles=target_titles,
            target_locations=target_locations,
            target_cities=target_cities,
            work_mode=work_mode,
        ),
        db=db,
    )


@router.post("/api/jobs/scrape", response_model=ScrapeResponse)
def trigger_jobs_scrape(
    payload: ScrapeRequest = ScrapeRequest(), db: Session = Depends(get_db)
):
    """Synchronous ATS portal discovery scan (request path)."""
    return _execute_ats_scrape(payload, db)


def _execute_ats_scrape(
    payload: ScrapeRequest,
    db: Session,
    cancel_token: Optional[CancellationToken] = None,
    progress_cb: Optional[Callable] = None,
) -> ScrapeResponse:
    """
    Triggers fast job discovery across ATS portals (Greenhouse/Lever/Custom)
    with instant match scoring without blocking on heavy LLM text generation.
    """
    scrape_cancel_event.clear()
    if progress_cb:
        progress_cb(
            step=1,
            label="Scanning ATS portals (Greenhouse/Lever/Ashby/custom)...",
            total_steps=1,
        )
    start_tok = token_tracker.get_stats()
    # Opt-in portal gating: disabled portals/blocklisted companies are skipped entirely.
    _flags = get_feature_flags(db)
    _ats_on = bool(_flags.get("ats_portals", True))
    query = db.query(Company)
    if payload.company_id:
        query = query.filter(Company.id == payload.company_id)
    companies = query.all()

    if not companies:
        return ScrapeResponse(
            jobs_found=0,
            jobs_added=0,
            companies_updated=0,
            message="No companies configured for scraping.",
        )

    # Fetch active candidate resume for automated scoring
    active_resume = (
        db.query(Resume)
        .filter(Resume.is_active == True)
        .order_by(Resume.created_at.desc())
        .first()
    )
    resume_text = decrypt_data(active_resume.content_encrypted) if active_resume else ""
    parsed_resume = _parsed_resume_of(active_resume)

    # Parse target criteria filters into the shared preference gate (single source of truth).
    title_filters = (
        [t.strip().lower() for t in payload.target_titles.split(",") if t.strip()]
        if payload.target_titles
        else []
    )
    loc_filters = (
        [l.strip().lower() for l in payload.target_locations.split(",") if l.strip()]
        if payload.target_locations
        else []
    )
    city_filters = (
        [c.strip().lower() for c in payload.target_cities.split(",") if c.strip()]
        if payload.target_cities
        else []
    )
    wm_filter = (payload.work_mode or "").strip().lower()
    pref_filter = PreferenceFilter(
        PreferenceCriteria(
            titles=title_filters,
            locations=loc_filters,
            cities=city_filters,
            work_mode=wm_filter,
        )
    )

    total_found = 0
    total_added = 0
    companies_updated = 0

    # One shared pacer for the whole run: same-host requests across portals stay spaced and share
    # the per-run request ceiling. See docs/POLITE_CRAWLING_AND_WORKDAY.md §2.
    try:
        from backend.ats.engine import set_active_pacer
        from backend.ats.pacing import COOLDOWN_STATE_PATH, build_pacer

        # Cooldowns persist across runs (finding 48): a host that answered 429/503 last run is
        # not re-hit at full speed by this one.
        set_active_pacer(build_pacer(persist_path=COOLDOWN_STATE_PATH))
    except Exception as _pacing_err:  # noqa: BLE001 - pacing is best-effort
        logger.warning(f"Could not initialise scrape pacing: {_pacing_err}")

    global LATEST_SCRAPE_AUDIT
    audit_records = []

    try:
        for comp in companies:
            if cancel_token:
                cancel_token.check()
            if scrape_cancel_event.is_set():
                logger.info("Job scraping cancelled by user.")
                break

            # Blocklist gate: never scan excluded employers/domains (e.g. Amazon).
            if is_company_excluded(comp.name, comp.domain or comp.careers_url or "", db):
                logger.info(f"Skipping scan for excluded company '{comp.name}'.")
                audit_records.append(
                    {
                        "company": comp.name,
                        "portal_type": "Excluded",
                        "careers_url": comp.careers_url or "",
                        "status": "Excluded",
                        "raw_found": 0,
                        "added": 0,
                        "diagnostics": "Skipped: company matches the excluded-companies blocklist.",
                    }
                )
                continue

            # 1. Update company intelligence if empty from catalog or config
            if not comp.recent_news or not comp.salary_insights:
                cat_match = get_catalog_company(comp.name)
                if cat_match:
                    comp.description = comp.description or cat_match.get("description")
                    comp.recent_news = cat_match.get("recent_news")
                    comp.salary_insights = cat_match.get("salary_insights")
                    comp.hiring_process = cat_match.get("hiring_process")
                    companies_updated += 1

            discovered_jobs = []
            comp_lower = comp.name.lower()
            # A portal that raises must cost one audit row, not the rest of the scan (finding 46).
            portal_errors: List[str] = []

            try:
                from backend.ats.engine import active_pacer

                _run_pacer = active_pacer()
            except Exception:  # noqa: BLE001 - pacing is best-effort
                _run_pacer = None
            _refusals_before = _run_pacer.refusals() if _run_pacer is not None else {}

            # A. Specialized company APIs (e.g. Uber) — opt-in (unofficial internal API)
            if (
                _ats_on
                and _flags.get("ats_uber")
                and has_consent(db, "ats_uber")
                and scraper.is_uber_company(comp.name)
            ):
                try:
                    discovered_jobs.extend(scraper.scrape_uber_jobs()[:35])
                except Exception as portal_err:  # noqa: BLE001 - one portal cannot abort the scan
                    portal_errors.append(f"uber: {portal_err}")
                    logger.warning(f"Portal 'uber' failed for {comp.name}: {portal_err}")

            # B–C. Config-driven ATS portals (Greenhouse/Lever/Ashby/SmartRecruiters/Workable/Workday).
            # One loop over data/ats_portals.json; each portal is gated by its own opt-in feature flag.
            # A careers_url that *is* an ATS board pins the scan to that portal. Any other careers_url
            # (auto-derived `https://www.<domain>/careers`, a direct portal) is never usable as a board
            # slug, so those companies are probed by name instead (Uber (A) and Custom HTML (D) stay
            # bespoke).
            _pinned_portal = scraper.ats_for_url(comp.careers_url) if comp.careers_url else None
            for _portal in _ATS_PORTALS:
                if not (_ats_on and _flags.get(_portal.default_flag)):
                    continue
                if (
                    _portal.default_flag in CONSENT_REQUIRED_FEATURES
                    and not has_consent(db, _portal.default_flag)
                ):
                    # A CONSENT_VERSION bump invalidates the stored acknowledgement while the flag
                    # stays true, so the run must stop rather than keep scraping (finding 42).
                    logger.info(
                        f"Skipping portal '{_portal.id}' for {comp.name}: consent is not current."
                    )
                    continue
                if _pinned_portal and _portal.id != _pinned_portal:
                    continue
                _portal_fn = getattr(scraper, _portal.scraper_attr)
                try:
                    _portal_jobs = _portal_fn(
                        comp.name,
                        title_filters=title_filters,
                        city_filters=city_filters,
                        location_filters=loc_filters,
                        work_mode=wm_filter,
                        max_matches=35,
                        careers_url=comp.careers_url if _pinned_portal == _portal.id else None,
                    )
                except Exception as portal_err:  # noqa: BLE001 - one portal cannot abort the scan
                    portal_errors.append(f"{_portal.id}: {portal_err}")
                    logger.warning(
                        f"Portal '{_portal.id}' failed for {comp.name}: {portal_err}"
                    )
                    continue
                if _portal_jobs:
                    discovered_jobs.extend(_portal_jobs)

            # D. Custom HTML portal scraper — opt-in (arbitrary HTML scraping)
            if (
                _ats_on
                and _flags.get("ats_custom_html")
                and has_consent(db, "ats_custom_html")
                and comp.careers_url
            ):
                try:
                    custom_jobs = scraper.scrape_custom_jobs(comp.careers_url)
                except Exception as portal_err:  # noqa: BLE001 - one portal cannot abort the scan
                    portal_errors.append(f"custom_html: {portal_err}")
                    logger.warning(
                        f"Portal 'custom_html' failed for {comp.name}: {portal_err}"
                    )
                    custom_jobs = []
                if custom_jobs:
                    discovered_jobs.extend(custom_jobs[:35])

            # A pacer refusal is a rate-limit signal, not "this employer has no jobs" (finding 47).
            _refusals_after = _run_pacer.refusals() if _run_pacer is not None else {}
            refused_hosts = {
                host: reason
                for host, reason in _refusals_after.items()
                if _refusals_before.get(host) != reason
            }

            # Deduplicate on a normalised URL (tracking params, trailing slash, scheme/host case),
            # so the same posting seen through two portals does not count twice, and report the
            # audit's raw total after dedupe rather than before (finding 52).
            unique_jobs = []
            seen_urls = set()
            for job in discovered_jobs:
                key = scraper.normalize_job_url(job.get("url") or "")
                if key and key in seen_urls:
                    continue
                seen_urls.add(key)
                unique_jobs.append(job)
            discovered_jobs = unique_jobs

            comp_raw_count = len(discovered_jobs)
            total_found += comp_raw_count
            comp_added = 0

            # 3. Process each discovered job
            for j in discovered_jobs:
                if scrape_cancel_event.is_set():
                    logger.info("Job scraping cancelled by user.")
                    break

                # Semantic & Synonym Filtering via the shared preference gate
                if not pref_filter.accepts(j.get("title", ""), j.get("location", ""), j.get("description", "")):
                    continue

                # The requisition id names exactly one posting, so it dedupes retitled roles
                # that the (company, title) key cannot (finding 53).
                req_id, req_source = extract_requisition_id(j.get("url") or "")
                existing = None
                if req_id:
                    existing = (
                        db.query(Job)
                        .filter(Job.company_id == comp.id, Job.requisition_id == req_id)
                        .first()
                    )
                if existing is None:
                    existing = (
                        db.query(Job)
                        .filter(Job.company_id == comp.id, Job.title == j["title"])
                        .first()
                    )

                if existing:
                    continue

                # Evaluate due diligence / ghost job flags
                dd = scraper.evaluate_job_due_diligence(
                    job_title=j["title"],
                    source_type=j.get("source_type", "Direct"),
                    portal_jobs=discovered_jobs,
                )

                # Negative role filter
                dismissal = apply_dismissal_filter(
                    db, j["title"], j.get("description", ""), comp.name
                )
                dismissal_match = dismissal.match
                job_status = dismissal.status
                match_analysis = dismissal.analysis

                # Requirement-coverage match score (deterministic engine).
                match_score = 0
                match_scored = False
                score_method = match_scored_at = match_band = apply_recommendation = None
                if not dismissal_match:
                    (
                        match_score,
                        engine_analysis,
                        match_scored,
                        score_method,
                        match_scored_at,
                        match_band,
                        apply_recommendation,
                        strengths,
                        gaps,
                        partials,
                    ) = _score_job_with_engine(
                        db,
                        resume_text,
                        j.get("description") or "",
                        parsed_resume=parsed_resume,
                        job_title=j.get("title") or "",
                    )
                    if engine_analysis:
                        match_analysis = engine_analysis

                job_emb = generate_embeddings(
                    f"{j['title']} {clean_job_description(j.get('description', '') or j['title'])}"
                )
                new_job = Job(
                    company_id=comp.id,
                    title=j["title"],
                    description=j.get("description", ""),
                    url=j.get("url", comp.careers_url or ""),
                    requisition_id=req_id or None,
                    requisition_source=req_source or None,
                    salary_range=j.get("salary_range")
                    or comp.salary_insights
                    or "Competitive",
                    location=j.get("location", "Remote / Various"),
                    source=j.get("source", "Careers Portal"),
                    source_type=j.get("source_type", "Direct"),
                    status=job_status,
                    match_score=match_score,
                    match_analysis=match_analysis,
                    match_scored=match_scored,
                    score_method=score_method,
                    match_scored_at=match_scored_at,
                    **(_score_provenance(db) if match_scored else {}),
                    match_band=match_band,
                    apply_recommendation=apply_recommendation,
                    match_strengths_json=json.dumps(strengths) if match_scored else None,
                    match_gaps_json=json.dumps(gaps) if match_scored else None,
                    match_partials_json=json.dumps(partials) if match_scored else None,
                    cover_letter_draft=None,
                    tailored_resume_points=None,
                    cold_message_draft=None,
                    is_ghost_job=dd["is_ghost_job"],
                    repost_count=0,
                    embedding=job_emb,
                )
                db.add(new_job)
                total_added += 1
                comp_added += 1

            # Commit per company so the SQLite write lock is held for one portal fetch instead of the
            # whole scan: a long run starved every other writer past the 30 s busy_timeout.
            persist_error = None
            try:
                db.commit()
            except Exception as commit_err:  # noqa: BLE001 - one failed batch must not abort the scan
                persist_error = str(commit_err)[:200]
                logger.warning(
                    f"Failed to persist scrape batch for '{comp.name}': {commit_err}"
                )
                try:
                    db.rollback()
                except Exception:  # noqa: BLE001 - recovery is best-effort
                    pass
                total_added -= comp_added
                comp_added = 0

            status_text = (
                "Success"
                if comp_added > 0
                else ("Filtered" if comp_raw_count > 0 else "0 Postings Found")
            )
            status_note = ""
            if portal_errors:
                status_note = "Portal error(s): " + "; ".join(portal_errors)[:200]
                if comp_added == 0:
                    status_text = "Failed"
            elif refused_hosts and comp_raw_count == 0:
                status_text = "Rate limited"
                status_note = "Pacer refused: " + ", ".join(
                    f"{host} ({reason})" for host, reason in sorted(refused_hosts.items())
                )
            cat_match = get_catalog_company(comp.name)
            comp_portal_type = (cat_match.get("portal_type") if cat_match else None) or (
                "Greenhouse"
                if "greenhouse" in (comp.careers_url or "").lower()
                else "Direct"
            )
            diag_text = (
                f"Found {comp_raw_count} postings, added {comp_added} matching targets."
            )
            if comp_raw_count == 0:
                diag_text = f"No public API listings returned for {comp.name} ({comp_portal_type}). May require direct portal login or SPA renderer."
            if persist_error:
                status_text = "Failed"
                diag_text = (
                    f"Found {comp_raw_count} postings, none persisted: batch write failed "
                    f"({persist_error})."
                )
            if status_note:
                diag_text = f"{diag_text} {status_note}"

            audit_records.append(
                {
                    "company": comp.name,
                    "portal_type": comp_portal_type,
                    "careers_url": comp.careers_url or "",
                    "status": status_text,
                    "raw_found": comp_raw_count,
                    "added": comp_added,
                    "diagnostics": diag_text,
                }
            )

    finally:
        # Drop the run-level pacer on every exit path, including an unexpected abort, so a
        # failed run cannot leave its counters/cooldowns active for the next one (finding 46).
        try:
            from backend.ats.engine import clear_active_pacer

            clear_active_pacer()
        except Exception:  # noqa: BLE001 - best-effort cleanup
            pass

    LATEST_SCRAPE_AUDIT = audit_records
    is_cancelled = scrape_cancel_event.is_set()
    if is_cancelled:
        msg = f"Scrape stopped by user. {total_added} targeted jobs saved."
    elif total_added == 0 and total_found > 0:
        filter_summary = f"Roles: '{payload.target_titles or 'All'}' | Loc: '{payload.target_locations or 'All'}'"
        msg = f"Scanned {total_found} positions across {len(companies)} companies. 0 matched filters ({filter_summary}). Tip: Select 'All Countries' or adjust role keywords."
    else:
        msg = f"Scrape completed: {total_found} positions scanned, {total_added} targeted jobs added."

    # Persist Operation Audit Log
    try:
        end_tok = token_tracker.get_stats()
        p_used = max(0, end_tok["prompt_tokens"] - start_tok["prompt_tokens"])
        c_used = max(0, end_tok["completion_tokens"] - start_tok["completion_tokens"])

        op_log = OperationLog(
            operation_type="ats_scrape",
            status="Cancelled" if is_cancelled else "Completed",
            summary=msg,
            details_json=sanitize_log_details(
                {
                    "companies_scanned": len(companies),
                    "jobs_found": total_found,
                    "jobs_added": total_added,
                    "audit": audit_records,
                }
            ),
            jobs_count=total_added,
            prompt_tokens=p_used,
            completion_tokens=c_used,
        )
        db.add(op_log)
        db.commit()
    except Exception as op_err:
        logger.warning(f"Failed to persist OperationLog for scrape: {op_err}")

    if progress_cb:
        progress_cb(step=1, label=msg, items_found=total_added, total_steps=1)

    return ScrapeResponse(
        jobs_found=total_found,
        jobs_added=total_added,
        companies_updated=companies_updated,
        message=msg,
    )


@router.post("/api/jobs/scrape/stop")
def stop_jobs_scrape():
    """Signals the active job scraping background loop to cancel immediately."""
    scrape_cancel_event.set()
    return {"message": "Job scraping process stopped."}
