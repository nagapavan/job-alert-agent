"""LinkedIn ingestion: saved jobs, job alerts, and the sync-all stage that drives them.

Extracted from ``main.py``. Everything here is session-driven LinkedIn automation, which is the
one channel with an account-risk profile of its own (see docs/ATS_PORTALS.md): the scan gate keeps
a manual sync and a Sync All run from driving the single-owner browser profile at the same time.
"""

import datetime
import json
import logging
import re
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from playwright.sync_api import sync_playwright
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend import linkedin_scan_queue
from backend.ai_helper import generate_embeddings, token_tracker
from backend.config import (
    LINKEDIN_AUTOMATION_PACING_SECONDS,
    LINKEDIN_SAVED_JOB_DESC_FETCH_LIMIT,
    decrypt_data,
)
from backend.database import (
    ApplicationEvent,
    Company,
    Job,
    OperationLog,
    Resume,
    get_db,
    get_user_preferences,
    has_consent,
    is_feature_enabled,
)
from backend.dismissal import apply_dismissal_filter
from backend.job_scoring import _parsed_resume_of, _score_job_with_engine, _score_provenance
from backend.observability import sanitize_log_details
from backend.parser import clean_job_description
from backend.playwright_app import (
    fetch_linkedin_job_details,
    get_persistent_browser_context,
    scan_and_sync_linkedin_saved_jobs,
    scan_linkedin_job_alerts,
    scan_linkedin_job_recommendations,
)
from backend.preference_filter import PreferenceFilter
from backend.scraper import evaluate_job_due_diligence, extract_portal_info_from_job_url
from backend.task_engine import CancellationToken, task_engine
from backend.title_parser import safe_company_name as _safe_company_name

logger = logging.getLogger(__name__)

router = APIRouter(tags=["linkedin"])

# Set by POST /api/linkedin/sync/stop and checked at the boundaries between scan stages. A stage
# is a sequence of Playwright navigations that cannot be interrupted mid-flight, so "stop" means
# "do not start the next stage": the current page work finishes first. Cleared when a scan takes
# the gate, so a stop can never abort a scan that starts afterwards.
linkedin_cancel_event = threading.Event()

_LINKEDIN_SCAN_GATE = linkedin_scan_queue.gate


class LinkedInSyncResponse(BaseModel):
    total_processed: int = 0
    recommendations_found: int
    recommendations_added: int
    saved_jobs_checked: int
    saved_jobs_added: int = 0
    already_existing: int = 0
    dismissed_filtered: int = 0
    closed_jobs_pruned: int
    message: str


class LinkedInLastSyncOut(BaseModel):
    """Summary of the most recent LinkedIn sync, powering the "N new from sync" review banner."""

    sync_batch_id: Optional[str] = None
    total_added: int = 0
    saved_jobs_added: int = 0
    recommendations_added: int = 0
    already_existing: int = 0
    closed_jobs_pruned: int = 0
    # Jobs from this batch still unreviewed (sync_batch_id set, not yet acted on).
    pending_review: int = 0
    created_at: Optional[datetime.datetime] = None
    has_sync: bool = False


def _linkedin_upsert_company(db: Session, raw_name: Optional[str]) -> Company:
    """Find or create the employer row for a LinkedIn posting (flush assigns the id)."""
    comp_name = _safe_company_name(raw_name, "LinkedIn Network")
    comp = db.query(Company).filter(Company.name.ilike(comp_name)).first()
    if not comp:
        comp = Company(
            name=comp_name,
            domain=f"{comp_name.lower().replace(' ', '')}.com",
        )
        db.add(comp)
        db.flush()
    return comp


def _linkedin_resolve_portal(
    comp: Company, url: str, company_name: str
) -> Tuple[str, Optional[dict]]:
    """
    Upgrade a LinkedIn posting URL to the employer's canonical ATS apply URL when
    discoverable, and backfill the company's careers URL / domain. Returns the resolved
    URL plus the raw portal metadata (or None when nothing was discoverable).
    """
    portal_info = extract_portal_info_from_job_url(url, company_name)
    if not portal_info:
        return url, None
    if portal_info.get("careers_url") and (
        not comp.careers_url or "linkedin.com" in (comp.careers_url or "")
    ):
        comp.careers_url = portal_info["careers_url"]
    if portal_info.get("domain") and not comp.domain:
        comp.domain = portal_info["domain"]
    canonical = portal_info.get("canonical_apply_url")
    if canonical and "linkedin.com" not in canonical:
        return canonical, portal_info
    return url, portal_info


def _linkedin_enrich_saved_job(page, item: Dict[str, Any]) -> str:
    """Fetches a saved-job posting's detail page to (a) supply a real description for scoring and
    (b) repair a merged title / placeholder employer produced by a collapsed list card.

    Mutates ``item`` in place for title/company and returns the description text. Best-effort:
    any failure leaves the item untouched so ingest degrades to the unscored baseline.
    """
    url = item.get("url", "")
    if "/jobs/view/" not in url:
        return (item.get("description") or "").strip()
    try:
        details = fetch_linkedin_job_details(page, url)
    except Exception as e:
        logger.debug(f"LinkedIn saved-job detail fetch skipped: {e}")
        return (item.get("description") or "").strip()

    dom_title = (details.get("title") or "").strip()
    dom_company = (details.get("company") or "").strip()
    cur_title = (item.get("title") or "").strip()
    cur_company = (item.get("company") or "").strip()

    # Repair the merged "TitleCompany" title when the detail page gives a shorter, clean title.
    if dom_title:
        norm_title = re.sub(r"[^a-z0-9]", "", cur_title.lower())
        norm_dom = re.sub(r"[^a-z0-9]", "", dom_title.lower())
        if not cur_title or (norm_dom and norm_dom in norm_title):
            item["title"] = dom_title

    if dom_company and (
        not cur_company
        or cur_company.lower() in ("unknown company", "linkedin network", "linkedin")
    ):
        item["company"] = dom_company

    if details.get("canonical_url"):
        item["url"] = details["canonical_url"]
    return (details.get("description") or "").strip()


def _linkedin_find_existing_job(
    db: Session, title: str, url: str, company_id: Optional[int] = None
) -> Optional[Job]:
    """Locate an already-tracked posting by URL, or by title + employer/source."""
    if company_id is not None:
        cond = (Job.url == url) | (
            (Job.title == title) & (Job.company_id == company_id)
        )
    else:
        cond = (Job.url == url) | (
            (Job.title.ilike(title)) & (Job.source_type == "LinkedIn")
        )
    return db.query(Job).filter(cond).first()


def _log_linkedin_automation_warning() -> None:
    """Emits a one-line WARNING whenever authenticated LinkedIn automation runs."""
    logger.warning(
        "Authenticated LinkedIn automation is active. This drives your logged-in session "
        "and performs DOM scraping/un-saves that may conflict with LinkedIn's User Agreement "
        "and risk account limits. Proceeding at the user's own risk."
    )


@router.get("/api/linkedin/last-sync", response_model=LinkedInLastSyncOut)
def linkedin_last_sync(db: Session = Depends(get_db)):
    """
    Returns the most recent LinkedIn sync batch so the SPA can show a "N new from sync"
    review banner and deep-link to the jobs that still need review.
    """
    log = (
        db.query(OperationLog)
        .filter(OperationLog.operation_type == "linkedin_sync")
        .order_by(OperationLog.id.desc())
        .first()
    )
    if not log:
        return LinkedInLastSyncOut(has_sync=False)

    details: Dict[str, Any] = {}
    try:
        details = json.loads(log.details_json or "{}") or {}
    except Exception:
        details = {}

    batch_id = details.get("sync_batch_id")
    if batch_id:
        pending = (
            db.query(Job)
            .filter(Job.sync_batch_id == batch_id)
            .count()
        )
    else:
        pending = 0

    return LinkedInLastSyncOut(
        sync_batch_id=batch_id,
        total_added=int(details.get("total_added", 0) or 0),
        saved_jobs_added=int(details.get("saved_jobs_added", 0) or 0),
        recommendations_added=int(details.get("recommendations_added", 0) or 0),
        already_existing=int(details.get("already_existing", 0) or 0),
        closed_jobs_pruned=int(details.get("closed_jobs_pruned", 0) or 0),
        pending_review=pending,
        created_at=log.created_at,
        has_sync=True,
    )


# One LinkedIn saved-jobs scan at a time. The manual endpoint and the Sync-All stage share the
# single-owner browser profile, and is_profile_in_use() only reflects a browser that already holds
# it, so two callers entering together both pass that check (issue #114). A request that arrives
# while the gate is held is queued rather than rejected (issue #115).


def _submit_queued_linkedin_scan(request_id: int) -> str:
    """Queue a LinkedIn scan as a background task and stamp the task id on its row."""
    task_id = task_engine.submit_task(
        task_type="linkedin_sync",
        task_name=f"LinkedIn sync (queued #{request_id})",
        fn=_run_queued_linkedin_scan,
        request_id=request_id,
        total_steps=3,
    )
    try:
        from backend.database import SessionLocal

        db = SessionLocal()
        try:
            linkedin_scan_queue.attach_task_id(db, request_id, task_id)
        finally:
            db.close()
    except Exception as stamp_err:  # noqa: BLE001 - the id is display-only
        logger.warning(
            f"Could not stamp task id on queued LinkedIn scan #{request_id}: {stamp_err}"
        )
    return task_id


def _run_queued_linkedin_scan(
    request_id: int,
    cancel_token: Optional[CancellationToken] = None,
    progress_cb: Optional[Callable] = None,
) -> Dict[str, Any]:
    """Background entry point for a queued LinkedIn scan: wait for the gate, then scan."""
    db_gen = get_db()
    db = next(db_gen)
    acquired = False
    try:
        if progress_cb:
            progress_cb(
                step=1,
                label="Waiting for the running LinkedIn scan to finish...",
                total_steps=3,
            )

        def _cancelled() -> bool:
            return bool(cancel_token and cancel_token.is_cancelled)

        acquired = _LINKEDIN_SCAN_GATE.acquire(should_cancel=_cancelled)
        if not acquired:
            linkedin_scan_queue.mark_finished(
                db,
                request_id,
                linkedin_scan_queue.STATUS_CANCELLED,
                summary="Cancelled before it started.",
            )
            return {"jobs_found": 0, "skipped": "cancelled_before_start"}

        linkedin_scan_queue.mark_running(db, request_id)
        if progress_cb:
            progress_cb(
                step=2,
                label="Scanning LinkedIn saved jobs...",
                items_found=0,
                total_steps=3,
            )

        result = _sync_linkedin_alerts_and_saved(db)
        linkedin_scan_queue.mark_finished(
            db,
            request_id,
            linkedin_scan_queue.STATUS_COMPLETED,
            summary=result.message,
        )
        if progress_cb:
            progress_cb(
                step=3,
                label=result.message,
                items_found=result.saved_jobs_added,
                total_steps=3,
            )
        return {
            "jobs_found": result.saved_jobs_added,
            "total_processed": result.total_processed,
            "saved_jobs_added": result.saved_jobs_added,
            "message": result.message,
        }
    except Exception as scan_err:
        try:
            db.rollback()
        except Exception:  # noqa: BLE001 - recovery is best-effort
            pass
        try:
            linkedin_scan_queue.mark_finished(
                db,
                request_id,
                linkedin_scan_queue.STATUS_FAILED,
                error=str(scan_err),
            )
        except Exception as mark_err:  # noqa: BLE001 - never mask the scan error
            logger.warning(
                f"Could not mark queued LinkedIn scan #{request_id} failed: {mark_err}"
            )
        raise
    finally:
        if acquired:
            _LINKEDIN_SCAN_GATE.release()
        db.close()


@router.post("/api/linkedin/sync", response_model=LinkedInSyncResponse)
def sync_linkedin_alerts_and_saved(db: Session = Depends(get_db)):
    """
    Syncs live LinkedIn subscribed job alerts, recommended openings, and saved jobs.
    Prunes stale unacted 'To Apply' alert jobs (>7 days old) while strictly preserving Shortlisted & Applied jobs.
    Prunes/unsaves any closed or expired jobs from the LinkedIn saved list and updates DB status.

    When another scan already owns the browser profile the request is queued (202) instead of
    rejected; it runs as a background task as soon as the running scan finishes.
    """
    if _LINKEDIN_SCAN_GATE.try_acquire():
        # A stop request can only apply to the scan that was running when it was made; clear it
        # here so the next scan is not aborted by a flag set minutes ago.
        linkedin_cancel_event.clear()
        try:
            return _sync_linkedin_alerts_and_saved(db)
        finally:
            _LINKEDIN_SCAN_GATE.release()

    queued = linkedin_scan_queue.enqueue(db, origin="manual")
    position = linkedin_scan_queue.queue_position(db, queued.id)
    try:
        task_id = _submit_queued_linkedin_scan(queued.id)
    except Exception as submit_err:  # noqa: BLE001 - report, never leave a dangling row
        logger.warning(f"Could not queue LinkedIn scan #{queued.id}: {submit_err}")
        linkedin_scan_queue.mark_finished(
            db,
            queued.id,
            linkedin_scan_queue.STATUS_FAILED,
            error=str(submit_err),
        )
        raise HTTPException(
            status_code=503,
            detail=f"Could not queue the LinkedIn scan: {submit_err}",
        )

    logger.info(
        f"LinkedIn scan queued as task {task_id} (request #{queued.id}, position {position})."
    )
    return JSONResponse(
        status_code=202,
        content={
            "status": "queued",
            "queued": True,
            "request_id": queued.id,
            "task_id": task_id,
            "queue_position": position,
            "message": (
                f"A LinkedIn scan is already running; this request is queued as task {task_id} "
                f"(position {position}). It starts automatically when the current scan finishes."
            ),
        },
    )


def _sync_linkedin_alerts_and_saved(db: Session) -> LinkedInSyncResponse:
    if not is_feature_enabled(db, "linkedin_sync"):
        return LinkedInSyncResponse(
            total_processed=0,
            recommendations_found=0,
            recommendations_added=0,
            saved_jobs_checked=0,
            saved_jobs_added=0,
            already_existing=0,
            dismissed_filtered=0,
            closed_jobs_pruned=0,
            message="LinkedIn automation is disabled. Enable it under Settings → Opt-in Discovery Features.",
        )
    if not has_consent(db, "linkedin_sync"):
        raise HTTPException(
            status_code=403,
            detail="Consent required for LinkedIn automation. Acknowledge the risk in Settings → Discovery Features.",
        )

    _log_linkedin_automation_warning()

    active_resume = (
        db.query(Resume)
        .filter(Resume.is_active == True)
        .order_by(Resume.created_at.desc())
        .first()
    )
    resume_text = decrypt_data(active_resume.content_encrypted) if active_resume else ""
    parsed_resume = _parsed_resume_of(active_resume)

    # Prefer server-persisted user preferences; fall back to resume-derived targets.
    pref = get_user_preferences(db)
    target_roles = [
        t.strip() for t in (pref.target_titles or "").split(",") if t.strip()
    ]
    _pref_cities = [
        c.strip() for c in (pref.target_cities or "").split(",") if c.strip()
    ]
    target_location = (
        (pref.target_country or "").strip() or ", ".join(_pref_cities[:2]) or "India"
    )
    if not target_roles and active_resume and active_resume.parsed_json_encrypted:
        try:
            p_json = json.loads(decrypt_data(active_resume.parsed_json_encrypted))
            target_roles = p_json.get("skills", [])[:5]
            target_location = p_json.get("location") or target_location
        except Exception:
            pass

    # 1. Prune stale LinkedIn 'To Apply' alert jobs older than 7 days (leaving Shortlisted & Applied 100% untouched)
    stale_pruned = 0
    try:
        stale_cutoff = datetime.datetime.now(
            datetime.timezone.utc
        ) - datetime.timedelta(days=7)
        stale_pruned = (
            db.query(Job)
            .filter(
                Job.source_type == "LinkedIn",
                Job.status == "To Apply",
                Job.created_at < stale_cutoff,
            )
            .delete(synchronize_session=False)
        )
        db.commit()
    except Exception as prune_err:
        logger.debug(f"Stale alert prune notice: {prune_err}")

    start_tok = token_tracker.get_stats()
    recs_found = 0
    recs_added = 0
    saved_checked = 0
    saved_added = 0
    closed_pruned = 0
    already_existing = 0
    dismissed_filtered = 0
    # Review batch id: every job newly ingested by this run is tagged with it so the UI can
    # show a "new from sync" review queue until the user acts on each job.
    sync_batch_id = f"li-{datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d%H%M%S')}"

    try:
        with sync_playwright() as p:
            context = get_persistent_browser_context(p, headless=True)
            page = (
                context.pages[0]
                if (
                    hasattr(context, "pages")
                    and isinstance(context.pages, list)
                    and len(context.pages) > 0
                )
                else context.new_page()
            )
            time.sleep(LINKEDIN_AUTOMATION_PACING_SECONDS)

            try:
                # 2. Scan subscribed job alert feeds + recommendations on shared page.
                # Each stage is skipped (never interrupted) when a stop was requested: the page
                # work in flight cannot be cancelled, so the earliest safe abort is a boundary.
                alert_postings = []
                recommendations = []
                if not linkedin_cancel_event.is_set():
                    alert_postings = scan_linkedin_job_alerts(
                        p,
                        headless=True,
                        max_alerts=5,
                        max_jobs_per_alert=10,
                        target_roles=target_roles,
                        target_location=target_location,
                        page=page,
                    )
                    time.sleep(LINKEDIN_AUTOMATION_PACING_SECONDS)
                if not linkedin_cancel_event.is_set():
                    recommendations = scan_linkedin_job_recommendations(
                        p, headless=True, limit=10, page=page
                    )

                # Combine alert items without duplicate URLs
                all_alert_items = []
                seen_urls = set()
                for item in alert_postings + recommendations:
                    u = item.get("url")
                    if u and u not in seen_urls:
                        seen_urls.add(u)
                        all_alert_items.append(item)

                recs_found = len(all_alert_items)

                for rec in all_alert_items:
                    comp = _linkedin_upsert_company(db, rec.get("company"))

                    # Resolve external portal link if available
                    resolved_url, portal_info = _linkedin_resolve_portal(
                        comp, rec.get("url", ""), comp.name
                    )
                    if portal_info and resolved_url != rec.get("url", ""):
                        rec["url"] = resolved_url
                        rec["source_type"] = "Direct"
                        rec["source"] = (
                            f"{portal_info.get('portal_type', 'Direct')} Portal"
                        )

                    # Check if job already exists
                    existing = _linkedin_find_existing_job(
                        db, rec["title"], rec["url"], comp.id
                    )

                    if existing:
                        already_existing += 1
                    else:
                        # Due diligence & AI scoring
                        dd = evaluate_job_due_diligence(
                            job_title=rec["title"],
                            source_type=rec.get("source_type", "LinkedIn"),
                            portal_jobs=[],
                        )

                        # Zero-token negative role filter
                        dismissal = apply_dismissal_filter(
                            db, rec["title"], rec.get("description", ""), comp.name
                        )
                        dismissal_match = dismissal.match
                        job_status = dismissal.status

                        score_method = None
                        match_scored_at = None
                        match_band = None
                        apply_recommendation = None
                        strengths = []
                        gaps = []
                        if dismissal_match:
                            dismissed_filtered += 1
                            match_score, match_scored = 0, False
                            match_analysis = dismissal.analysis
                        else:
                            (
                                match_score,
                                match_analysis,
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
                                rec.get("description") or "",
                                parsed_resume=parsed_resume,
                                job_title=rec.get("title") or "",
                            )
                            if not match_analysis:
                                match_analysis = "Direct LinkedIn Job Alert match."

                        job_emb = generate_embeddings(
                            f"{rec['title']} {clean_job_description(rec.get('description', '') or rec['title'])}"
                        )
                        new_job = Job(
                            company_id=comp.id,
                            title=rec["title"],
                            description=rec.get("description", ""),
                            url=rec.get("url", ""),
                            location=rec.get("location", "Remote / Various"),
                            source=rec.get("source", "LinkedIn Job Alert"),
                            source_type=rec.get("source_type", "LinkedIn"),
                            status=job_status,
                            sync_batch_id=sync_batch_id,
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
                            is_ghost_job=dd.get("is_ghost_job", False),
                            embedding=job_emb,
                        )
                        db.add(new_job)
                        recs_added += 1

                # 3. Scan saved jobs across all pages on shared page, ingest active ones as Shortlisted, and prune closed listings
                saved_jobs = (
                    []
                    if linkedin_cancel_event.is_set()
                    else scan_and_sync_linkedin_saved_jobs(
                        p, headless=True, auto_unsave_closed=True, page=page
                    )
                )
                saved_checked = len(saved_jobs)
                saved_added = 0
                saved_jd_fetches = 0

                for item in saved_jobs:
                    if item.get("is_closed"):
                        # Find job in DB
                        matched_job = (
                            db.query(Job)
                            .filter(
                                (Job.title.ilike(item["title"]))
                                | (Job.url == item.get("url", "___"))
                            )
                            .first()
                        )

                        if matched_job:
                            # Only close pre-application states; never clobber active progress
                            # or an explicit dismissal.
                            if matched_job.status in ("To Apply", "Shortlisted"):
                                matched_job.status = "Closed"
                                event = ApplicationEvent(
                                    job_id=matched_job.id,
                                    event_type="status_change",
                                    description="Closed on LinkedIn: position expired or no longer accepting applications (automatically unsaved).",
                                )
                                db.add(event)
                            closed_pruned += 1
                        elif item.get("unsaved"):
                            closed_pruned += 1
                    else:
                        # Active saved job - check if already in DB
                        existing_job = _linkedin_find_existing_job(
                            db, item["title"], item.get("url", "___")
                        )

                        if existing_job:
                            already_existing += 1
                            # Respect the user's explicit save: promote anything not yet actioned.
                            # Back-catalog repair of rows ingested before the card parser was fixed
                            # is handled by backend/scripts/repair_linkedin_saved_jobs.py, not here.
                            if existing_job.status in ("To Apply", "Not Interested"):
                                existing_job.status = "Shortlisted"
                        else:
                            # Ingest new active saved job. Enrich from the detail page first so a
                            # merged card title / placeholder employer is repaired and the scorer
                            # gets the real JD text (cards carry no description).
                            saved_jd = (item.get("description") or "").strip()
                            if saved_jd_fetches < LINKEDIN_SAVED_JOB_DESC_FETCH_LIMIT:
                                saved_jd_fetches += 1
                                saved_jd = _linkedin_enrich_saved_job(page, item) or saved_jd

                            comp = _linkedin_upsert_company(db, item.get("company"))

                            resolved_url, _portal_info = _linkedin_resolve_portal(
                                comp, item.get("url", ""), comp.name
                            )
                            item["url"] = resolved_url

                            dd = evaluate_job_due_diligence(
                                job_title=item["title"],
                                source_type="LinkedIn",
                                portal_jobs=[],
                            )

                            (
                                match_score,
                                match_analysis,
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
                                saved_jd or "",
                                parsed_resume=parsed_resume,
                                job_title=item.get("title") or "",
                            )
                            if not match_analysis:
                                match_analysis = "Saved directly on LinkedIn."

                            job_emb = generate_embeddings(
                                f"{item['title']} {clean_job_description(saved_jd or item['title'])}"
                            )
                            new_saved_job = Job(
                                company_id=comp.id,
                                title=item["title"],
                                description=saved_jd or f"LinkedIn Saved Job opening for {item['title']} at {comp.name}.",
                                url=item.get("url", ""),
                                location=item.get("location", "Remote / Various"),
                                source="LinkedIn Saved Jobs",
                                source_type="LinkedIn",
                                status="Shortlisted",
                                sync_batch_id=sync_batch_id,
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
                                is_ghost_job=dd.get("is_ghost_job", False),
                                embedding=job_emb,
                            )
                            db.add(new_saved_job)
                            saved_added += 1

                db.commit()
            finally:
                try:
                    context.close()
                except Exception:
                    pass

        total_processed = recs_found + saved_checked
        total_added = recs_added + saved_added
        stopped = linkedin_cancel_event.is_set()
        summary_msg = (
            f"LinkedIn sync stopped by request: Scanned {total_processed} total posting(s) "
            f"({recs_found} alerts/recommendations, {saved_checked} saved). {total_added} new "
            f"added, {already_existing} already tracked, {closed_pruned} closed job(s) pruned. "
            "Remaining stages were skipped."
            if stopped
            else f"LinkedIn sync complete: Scanned {total_processed} total posting(s) ({recs_found} alerts/recommendations, {saved_checked} saved). {total_added} new added, {already_existing} already tracked, {closed_pruned} closed job(s) pruned."
        )

        # Persist Operation Audit Log
        try:
            end_tok = token_tracker.get_stats()
            p_used = max(0, end_tok["prompt_tokens"] - start_tok["prompt_tokens"])
            c_used = max(
                0, end_tok["completion_tokens"] - start_tok["completion_tokens"]
            )

            op_log = OperationLog(
                operation_type="linkedin_sync",
                status="Partial" if stopped else "Completed",
                summary=summary_msg,
                details_json=sanitize_log_details(
                    {
                        "total_processed": total_processed,
                        "recommendations_found": recs_found,
                        "recommendations_added": recs_added,
                        "saved_jobs_checked": saved_checked,
                        "saved_jobs_added": saved_added,
                        "already_existing": already_existing,
                        "dismissed_filtered": dismissed_filtered,
                        "closed_jobs_pruned": closed_pruned,
                        "sync_batch_id": sync_batch_id,
                        "total_added": total_added,
                    }
                ),
                jobs_count=total_added + closed_pruned,
                prompt_tokens=p_used,
                completion_tokens=c_used,
            )
            db.add(op_log)
            db.commit()
        except Exception as op_err:
            logger.warning(
                f"Failed to persist OperationLog for LinkedIn sync: {op_err}"
            )

    except Exception as e:
        logger.error(f"Error during LinkedIn sync: {e}")
        total_processed = recs_found + saved_checked
        return LinkedInSyncResponse(
            total_processed=total_processed,
            recommendations_found=recs_found,
            recommendations_added=recs_added,
            saved_jobs_checked=saved_checked,
            saved_jobs_added=0,
            already_existing=already_existing,
            dismissed_filtered=dismissed_filtered,
            closed_jobs_pruned=closed_pruned,
            message=f"LinkedIn sync completed with notice: {str(e)}",
        )

    return LinkedInSyncResponse(
        total_processed=total_processed,
        recommendations_found=recs_found,
        recommendations_added=recs_added,
        saved_jobs_checked=saved_checked,
        saved_jobs_added=saved_added,
        already_existing=already_existing,
        dismissed_filtered=dismissed_filtered,
        closed_jobs_pruned=closed_pruned,
        message=summary_msg,
    )


# -------------------------------------------------------------
# Google for Jobs Followed Searches & Discovery Endpoints
# -------------------------------------------------------------


def _run_linkedin_sync_task_runner(
    cancel_token: Optional[CancellationToken] = None,
    progress_cb: Optional[Callable] = None,
) -> Dict[str, Any]:
    """Background runner for LinkedIn Saved Jobs and Job Alerts synchronization."""
    db_gen = get_db()
    db = next(db_gen)
    total_found = 0
    scan_lock_held = False
    scan_errors: List[str] = []
    # Saved jobs are explicit user intent and bypass the preference gate; alerts/recommendations do not.
    pref_filter = PreferenceFilter.from_db(db)
    alerts_filtered = 0

    try:
        # Defense in depth: the runner gates itself, so no caller (including a forced "Run now")
        # can dispatch authenticated LinkedIn automation without an explicit acknowledgment.
        if not is_feature_enabled(db, "linkedin_sync") or not has_consent(
            db, "linkedin_sync"
        ):
            if progress_cb:
                progress_cb(
                    step=1,
                    label="Skipped: LinkedIn automation disabled or consent not given.",
                    items_found=0,
                    total_steps=3,
                )
            return {"jobs_found": 0, "skipped": "consent_required"}

        _log_linkedin_automation_warning()

        if progress_cb:
            progress_cb(
                step=1,
                label="Scanning LinkedIn Saved Jobs in persistent browser...",
                items_found=0,
                total_steps=3,
            )

        if cancel_token:
            cancel_token.check()

        # Yield to an active interactive/assisted browser session (single-owner profile).
        from backend.playwright_app import is_profile_in_use

        if is_profile_in_use():
            if progress_cb:
                progress_cb(
                    step=3,
                    label="Skipped: browser profile in use by another session.",
                    total_steps=3,
                )
            return {"jobs_found": 0, "skipped": "browser_profile_busy"}

        # Mutual exclusion with a manual POST /api/linkedin/sync: both scans drive the same
        # single-owner browser profile (issue #114). The stage skips rather than queues so a
        # Sync All run never blocks for minutes behind a manual scan.
        if not _LINKEDIN_SCAN_GATE.try_acquire():
            if progress_cb:
                progress_cb(
                    step=3,
                    label="Skipped: a LinkedIn scan is already running.",
                    total_steps=3,
                )
            return {"jobs_found": 0, "skipped": "linkedin_sync_in_progress"}
        scan_lock_held = True
        # The flag applies to whichever scan holds the gate; a stale request must not abort this one.
        linkedin_cancel_event.clear()

        saved_count = 0
        # Review batch id for this background sync run: every job newly ingested is tagged so the
        # UI can show a "new from sync" review queue until the user acts on each job.
        sync_batch_id = f"li-{datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d%H%M%S')}"

        # Active resume drives match scoring for newly ingested saved jobs (same inputs the manual
        # sync path uses).
        active_resume = (
            db.query(Resume)
            .filter(Resume.is_active == True)
            .order_by(Resume.created_at.desc())
            .first()
        )
        resume_text = (
            decrypt_data(active_resume.content_encrypted) if active_resume else ""
        )
        parsed_resume = _parsed_resume_of(active_resume)
        saved_jd_fetches = 0

        try:
            with sync_playwright() as p:
                # The saved-jobs scan manages its own browser context. Enrichment (detail-page
                # fetches) reuses the scan's page when available; otherwise we lazily open a
                # context only if there is actually a new job to enrich, so a no-op sync never
                # spawns a browser.
                enrichment_page = None
                enrichment_context = None

                def _get_enrichment_page():
                    nonlocal enrichment_page, enrichment_context
                    if enrichment_page is None:
                        enrichment_context = get_persistent_browser_context(p, headless=True)
                        enrichment_page = (
                            enrichment_context.pages[0]
                            if (hasattr(enrichment_context, "pages") and isinstance(enrichment_context.pages, list) and len(enrichment_context.pages) > 0)
                            else enrichment_context.new_page()
                        )
                    return enrichment_page

                try:
                    saved_jobs = (
                        []
                        if linkedin_cancel_event.is_set()
                        else scan_and_sync_linkedin_saved_jobs(
                            p, headless=True, auto_unsave_closed=True, max_pages=3
                        )
                    )
                    for item in saved_jobs or []:
                        if item.get("is_closed"):
                            matched_job = (
                                db.query(Job)
                                .filter(
                                    (Job.title.ilike(item.get("title", "")))
                                    | (Job.url == item.get("url", "___"))
                                )
                                .first()
                            )
                            if matched_job and matched_job.status in (
                                "To Apply",
                                "Shortlisted",
                            ):
                                matched_job.status = "Closed"
                                event = ApplicationEvent(
                                    job_id=matched_job.id,
                                    event_type="status_change",
                                    description="Closed on LinkedIn: position expired or no longer accepting applications (automatically unsaved).",
                                )
                                db.add(event)
                        else:
                            existing = _linkedin_find_existing_job(
                                db, item.get("title", ""), item.get("url", "___")
                            )
                            if existing:
                                # Respect the user's explicit save: promote anything not yet actioned.
                                # Back-catalog repair of rows ingested before the card parser was fixed
                                # is handled by backend/scripts/repair_linkedin_saved_jobs.py.
                                if existing.status in ("To Apply", "Not Interested"):
                                    existing.status = "Shortlisted"
                            else:
                                # Enrich from the detail page first so a merged card title /
                                # placeholder employer is repaired and the scorer gets real JD text.
                                saved_jd = (item.get("description") or "").strip()
                                if saved_jd_fetches < LINKEDIN_SAVED_JOB_DESC_FETCH_LIMIT:
                                    saved_jd_fetches += 1
                                    saved_jd = _linkedin_enrich_saved_job(_get_enrichment_page(), item) or saved_jd

                                comp = _linkedin_upsert_company(db, item.get("company"))

                                resolved_url, _portal_info = _linkedin_resolve_portal(
                                    comp, item.get("url", ""), comp.name
                                )
                                item["url"] = resolved_url

                                dd = evaluate_job_due_diligence(
                                    job_title=item.get("title", ""),
                                    source_type="LinkedIn",
                                    portal_jobs=[],
                                )

                                (
                                    match_score,
                                    match_analysis,
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
                                    saved_jd or "",
                                    parsed_resume=parsed_resume,
                                    job_title=item.get("title") or "",
                                )
                                if not match_analysis:
                                    match_analysis = "Saved on LinkedIn (High user interest)."

                                job_emb = generate_embeddings(
                                    f"{item.get('title', '')} {clean_job_description(saved_jd or item.get('title', ''))}"
                                )
                                new_job = Job(
                                    company_id=comp.id,
                                    title=item.get("title", ""),
                                    description=saved_jd
                                    or f"LinkedIn Saved Job opening for {item.get('title', '')} at {comp.name}.",
                                    url=item.get("url", ""),
                                    location=item.get("location", "Remote / Various"),
                                    source="LinkedIn Saved Jobs",
                                    source_type="LinkedIn",
                                    status="Shortlisted",
                                    sync_batch_id=sync_batch_id,
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
                                    is_ghost_job=dd.get("is_ghost_job", False),
                                    embedding=job_emb,
                                )
                                db.add(new_job)
                                saved_count += 1
                    db.commit()
                    total_found += saved_count
                finally:
                    if enrichment_context is not None:
                        try:
                            enrichment_context.close()
                        except Exception:
                            pass
        except Exception as s_err:
            scan_errors.append(f"saved_jobs: {s_err}")
            logger.warning(f"LinkedIn saved jobs scan notice: {s_err}")

        if cancel_token:
            cancel_token.check()

        if progress_cb:
            progress_cb(
                step=2,
                label="Scanning LinkedIn Job Alerts & Recommendation feeds...",
                items_found=total_found,
                total_steps=3,
            )

        alerts_count = 0
        try:
            with sync_playwright() as p:
                alerts_items = (
                    []
                    if linkedin_cancel_event.is_set()
                    else scan_linkedin_job_alerts(
                        p, headless=True, max_alerts=10, max_jobs_per_alert=10
                    )
                )
                for rec in alerts_items or []:
                    # Preference gate: alert/recommendation feeds are discovery, not explicit intent.
                    if pref_filter.rejection_reason(
                        rec.get("title", ""), rec.get("location") or "", rec.get("description", "")
                    ):
                        alerts_filtered += 1
                        continue
                    comp = _linkedin_upsert_company(db, rec.get("company"))

                    resolved_url, portal_info = _linkedin_resolve_portal(
                        comp, rec.get("url", ""), comp.name
                    )
                    if portal_info and resolved_url != rec.get("url", ""):
                        rec["url"] = resolved_url
                        rec["source_type"] = "Direct"
                        rec["source"] = (
                            f"{portal_info.get('portal_type', 'Direct')} Portal"
                        )

                    existing = _linkedin_find_existing_job(
                        db, rec.get("title", ""), rec.get("url", ""), comp.id
                    )
                    if not existing:
                        dismissal = apply_dismissal_filter(
                            db,
                            rec.get("title", ""),
                            rec.get("description", ""),
                            comp.name,
                        )
                        dismissal_match = dismissal.match
                        job_status = dismissal.status
                        match_score = 0
                        match_analysis = (
                            dismissal.analysis or "Direct LinkedIn Job Alert match."
                        )

                        dd = evaluate_job_due_diligence(
                            job_title=rec.get("title", ""),
                            source_type=rec.get("source_type", "LinkedIn"),
                            portal_jobs=[],
                        )
                        job_emb = generate_embeddings(
                            f"{rec.get('title', '')} {clean_job_description(rec.get('description', '') or rec.get('title', ''))}"
                        )
                        new_job = Job(
                            company_id=comp.id,
                            title=rec.get("title", ""),
                            description=rec.get("description", ""),
                            url=rec.get("url", ""),
                            location=rec.get("location", "Remote / Various"),
                            source=rec.get("source", "LinkedIn Job Alert"),
                            source_type=rec.get("source_type", "LinkedIn"),
                            status=job_status,
                            sync_batch_id=sync_batch_id,
                            match_score=match_score,
                            match_analysis=match_analysis,
                            is_ghost_job=dd.get("is_ghost_job", False),
                            embedding=job_emb,
                        )
                        db.add(new_job)
                        alerts_count += 1
                db.commit()
                total_found += alerts_count
        except Exception as a_err:
            scan_errors.append(f"alerts: {a_err}")
            logger.warning(f"LinkedIn alerts scan notice: {a_err}")

        if progress_cb:
            progress_cb(
                step=3,
                label=f"Sync completed. Ingested {total_found} postings ({alerts_filtered} filtered out).",
                items_found=total_found,
                total_steps=3,
            )

        # Record a linkedin_sync OperationLog so the "N new from sync" review banner (which reads
        # the latest linkedin_sync log) reflects background/Sync-All runs, not just manual syncs.
        try:
            # A run where both scans raised is not a completed sync: it reports the failure
            # instead, so "no saved jobs" and "LinkedIn automation broke" stop looking identical.
            stopped_by_request = linkedin_cancel_event.is_set()
            log_status = (
                "Failed"
                if len(scan_errors) >= 2
                else ("Partial" if (scan_errors or stopped_by_request) else "Completed")
            )
            db.add(OperationLog(
                operation_type="linkedin_sync",
                status=log_status,
                summary=(
                    f"LinkedIn sync (background): {total_found} new added "
                    f"({saved_count} saved, {alerts_count} alerts/recommendations, "
                    f"{alerts_filtered} filtered)."
                    + (f" Errors: {'; '.join(scan_errors)[:200]}" if scan_errors else "")
                ),
                details_json=sanitize_log_details(
                    {
                        "total_processed": total_found,
                        "recommendations_found": alerts_count,
                        "recommendations_added": alerts_count,
                        "saved_jobs_checked": saved_count,
                        "saved_jobs_added": saved_count,
                        "already_existing": 0,
                        "dismissed_filtered": alerts_filtered,
                        "closed_jobs_pruned": 0,
                        "sync_batch_id": sync_batch_id,
                        "total_added": total_found,
                    }
                ),
                jobs_count=total_found,
            ))
            db.commit()
        except Exception as log_err:
            logger.warning(f"Failed to persist background LinkedIn sync log: {log_err}")

        return {
            "saved_jobs_ingested": saved_count,
            "alerts_ingested": alerts_count,
            "alerts_filtered_out_of_preference": alerts_filtered,
            "jobs_found": total_found,
        }
    finally:
        if scan_lock_held:
            _LINKEDIN_SCAN_GATE.release()
        db.close()


@router.post("/api/linkedin/sync/stop")
def stop_linkedin_sync(db: Session = Depends(get_db)):
    """Stop the running LinkedIn sync at its next stage boundary, and cancel queued scans.

    A scan stage is a sequence of Playwright navigations, so the running one cannot be
    interrupted mid-flight: this stops it from starting the next stage. The response says
    which of those happened instead of always claiming the process stopped.
    """
    running = _LINKEDIN_SCAN_GATE.is_held()
    linkedin_cancel_event.set()

    cancelled = 0
    try:
        for row in linkedin_scan_queue.cancel_pending(db):
            cancelled += 1
            if row.task_id:
                task_engine.cancel_task(row.task_id)
    except Exception as cancel_err:  # noqa: BLE001 - stopping the live scan still succeeds
        logger.warning(f"Could not cancel queued LinkedIn scans: {cancel_err}")

    if running:
        message = (
            "Stop requested. The scan in progress finishes its current page and skips the "
            "remaining stages."
        )
    elif cancelled:
        message = "No LinkedIn scan was running."
    else:
        message = "No LinkedIn scan was running and nothing was queued."
    if cancelled:
        message += f" {cancelled} queued scan(s) cancelled."
    return {"message": message}
