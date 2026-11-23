"""Google for Jobs discovery: the followed-query surface, the SERP scrape and the scheduled runner.

Extracted from ``main.py``. Google answers automated traffic with an interstitial, so this channel
carries its own circuit breaker (``backend/google_jobs_state.py``): a block stops the run, persists a
cooldown and rotates the query cursor. Task *submission* (``/api/tasks/discovery/google-jobs``) stays
in ``main`` with the other dispatch endpoints.
"""

import json
import logging
from typing import Any, Callable, Dict, List, Optional

from fastapi import APIRouter, Depends
from playwright.sync_api import sync_playwright
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend import channel_status
from backend.ai_helper import generate_embeddings, token_tracker
from backend.config import decrypt_data
from backend.database import (
    Company,
    Job,
    find_or_create_company,
    known_company_names,
    OperationLog,
    Resume,
    get_db,
    get_user_preferences,
    is_feature_enabled,
)
from backend.dismissal import apply_dismissal_filter
from backend.google_searches import (
    _normalize_google_queries,
    load_google_searches,
    normalize_google_query,
    save_google_searches,
)
from backend.job_scoring import _parsed_resume_of, _score_job_with_engine, _score_provenance
from backend.observability import sanitize_log_details
from backend.parser import clean_job_description
from backend.playwright_app import (
    fetch_google_jobs_followed_queries_playwright,
    fetch_google_jobs_playwright,
)
from backend.preference_filter import PreferenceFilter
from backend.scraper import evaluate_job_due_diligence, extract_portal_info_from_job_url
from backend.task_engine import CancellationToken
from backend.title_parser import resolve_company_name

logger = logging.getLogger(__name__)

router = APIRouter(tags=["google-jobs"])

# Both ingestion paths (manual endpoint and scheduled runner) must write the same value, or the
# endpoint's dedupe filter cannot see a posting the runner already stored.
GOOGLE_JOBS_SOURCE_TYPE = "Google"


class GoogleSearchesUpdate(BaseModel):
    queries: List[str]


class GoogleJobsScrapeRequest(BaseModel):
    queries: Optional[List[str]] = None
    query: Optional[str] = None
    location: Optional[str] = None
    limit: int = 15
    use_browser: bool = False
    sync_followed_tabs: bool = False


class GoogleJobsScrapeResponse(BaseModel):
    total_discovered: int = 0
    total_added: int = 0
    already_existing: int = 0
    filtered_out_of_preference: int = 0
    dismissed_filtered: int = 0
    queries_scanned: List[str] = []
    per_query_breakdown: List[dict] = []
    message: str


@router.get("/api/jobs/google-jobs/queries")
def get_google_jobs_queries():
    """Returns the list of followed / configured Google Jobs search queries."""
    return {"queries": load_google_searches()}


@router.post("/api/jobs/google-jobs/queries")
def update_google_jobs_queries(payload: GoogleSearchesUpdate):
    """Updates and saves the configured Google Jobs search queries."""
    saved = save_google_searches(payload.queries)
    return {
        "queries": saved,
        "message": f"Successfully saved {len(saved)} search queries.",
    }


@router.post("/api/jobs/google-jobs/sync-followed")
def sync_google_jobs_followed_queries(db: Session = Depends(get_db)):
    """
    Spawns Playwright to navigate to Google for Jobs 'Following' tab,
    extracts followed alert queries, merges them into saved searches, and returns the updated list.

    This is a second path to google.com, so it carries the same gates as the scrape route: the
    channel flag, the persisted block cooldown and the run pacer (finding 51).
    """
    current = load_google_searches()
    if not is_feature_enabled(db, "google_jobs"):
        return {
            "queries": current,
            "new_queries_found": 0,
            "discovered": [],
            "skipped": "Google Jobs is disabled. Enable it in Preferences to sync followed queries.",
        }

    from backend import google_jobs_state

    cooldown = google_jobs_state.cooldown_reason()
    if cooldown:
        return {
            "queries": current,
            "new_queries_found": 0,
            "discovered": [],
            "skipped": cooldown,
        }

    from backend.ats.pacing import build_pacer

    discovered = []
    try:
        with sync_playwright() as p:
            discovered = fetch_google_jobs_followed_queries_playwright(
                p, headless=True, pacer=build_pacer()
            )
    except Exception as e:
        logger.warning(f"Failed to extract followed queries via Playwright: {e}")

    new_queries = [q for q in discovered if q and q not in current]
    all_queries = current + new_queries
    if new_queries:
        all_queries = save_google_searches(all_queries)

    return {
        "queries": all_queries,
        "new_queries_found": len(new_queries),
        "discovered": discovered,
        "message": f"Followed searches synced. Discovered {len(discovered)} alert(s), {len(new_queries)} new query(ies) saved.",
    }


@router.post("/api/jobs/google-jobs/scrape", response_model=GoogleJobsScrapeResponse)
def scrape_google_jobs_endpoint(
    req: Optional[GoogleJobsScrapeRequest] = None, db: Session = Depends(get_db)
):
    """
    Scrapes Google for Jobs (`udm=8`) and SERP Schema.org JobPosting listings
    across multiple configured followed queries.
    Performs cross-source deduplication, ATS portal learning, zero-token filtering,
    match alignment scoring, and automated 384-d vector embeddings indexing.
    """
    req = req or GoogleJobsScrapeRequest()
    start_tok = token_tracker.get_stats()

    # Same gates as the scheduled runner. Without them the SPA button was a second, ungated path
    # to Google: a channel the operator switched off still scraped, and the persisted block
    # cooldown could be defeated by re-posting here, which is the one place a retry would.
    if not is_feature_enabled(db, "google_jobs"):
        return GoogleJobsScrapeResponse(
            message="Google Jobs is disabled. Enable it in Preferences to scan."
        )
    from backend import google_jobs_state

    cooldown = google_jobs_state.cooldown_reason()
    if cooldown:
        return GoogleJobsScrapeResponse(message=f"Skipped: {cooldown}")

    # If requested, sync followed tabs first
    if req.sync_followed_tabs:
        try:
            with sync_playwright() as p:
                discovered_tabs = fetch_google_jobs_followed_queries_playwright(
                    p, headless=True
                )
                if discovered_tabs:
                    current_searches = load_google_searches()
                    new_tabs = [
                        t for t in discovered_tabs if t and t not in current_searches
                    ]
                    if new_tabs:
                        save_google_searches(current_searches + new_tabs)
        except Exception as e:
            logger.warning(f"Sync followed tabs in scrape endpoint failed: {e}")

    # Determine query list
    queries_to_run = []
    if req.queries and len(req.queries) > 0:
        queries_to_run = [q.strip() for q in req.queries if q.strip()]
    elif req.query and req.query.strip():
        queries_to_run = [req.query.strip()]
    else:
        queries_to_run = load_google_searches()

    # Candidate active resume data for match scoring
    active_resume = db.query(Resume).filter(Resume.is_active == True).first()
    resume_text = decrypt_data(active_resume.content_encrypted) if active_resume else ""
    parsed_resume = _parsed_resume_of(active_resume)
    resume_skills = (parsed_resume or {}).get("skills", [])

    if not queries_to_run:
        default_q = "Software Engineer"
        if resume_skills:
            default_q = f"{resume_skills[0]} Software Engineer"
        queries_to_run = [default_q]

    total_discovered = 0
    total_added = 0
    already_existing = 0
    dismissed_filtered = 0
    filtered_out_of_preference = 0
    per_query_breakdown = []
    seen_in_batch_urls = set()

    # Same politeness and preference machinery the scheduled runner uses: one run-level pacer for
    # google.com, the operator's criteria applied before ingestion, and the query cursor rotated
    # instead of replaying every stored query on each click.
    from backend.ats.pacing import build_pacer

    google_pacer = build_pacer()
    pref_filter = PreferenceFilter.from_db(db)
    known_names = known_company_names(db)

    # The manual path used to hardcode "India", overriding the operator's target country for every
    # query. Fall back to the persisted preferences exactly as the scheduled runner does (#68).
    pref = get_user_preferences(db)
    search_loc = (req.location or "").strip() or (pref.target_country or "").strip()
    if not search_loc:
        pref_cities = [
            c.strip() for c in (pref.target_cities or "").split(",") if c.strip()
        ]
        search_loc = pref_cities[0] if pref_cities else "India"

    queries_to_run, _total_queries = google_jobs_state.select_queries(queries_to_run)

    try:
        for query_str in queries_to_run:
            q_discovered = 0
            q_added = 0
            q_existing = 0
            q_dismissed = 0
            q_filtered = 0
            discovered_jobs = []
            outcome_info: Dict[str, Any] = {}

            try:
                # Playwright is the sole Google Jobs fetcher: the direct SERP is permanently
                # robots-gated (google.com/robots.txt disallows /search) and can never succeed.
                with sync_playwright() as p:
                    discovered_jobs = fetch_google_jobs_playwright(
                        p,
                        query=query_str,
                        location=search_loc,
                        headless=True,
                        max_results=req.limit,
                        pacer=google_pacer,
                        outcome=outcome_info,
                    )
            except Exception as q_err:
                logger.warning(f"Error fetching jobs for query '{query_str}': {q_err}")
                # Record the failure: without it the query looked like a legitimate zero and the
                # run status could not be derived (finding 67).
                outcome_info.setdefault("outcome", "error")
                discovered_jobs = []

            if outcome_info.get("outcome") == "cooling_down":
                per_query_breakdown.append(
                    {
                        "query": query_str,
                        "discovered": 0,
                        "added": 0,
                        "existing": 0,
                        "dismissed": 0,
                        "filtered": 0,
                        "outcome": "cooling_down",
                    }
                )
                logger.warning(
                    "Google Jobs stopped: google.com is cooling down or over its per-run ceiling."
                )
                break

            q_discovered = len(discovered_jobs)
            total_discovered += q_discovered

            for j in discovered_jobs:
                job_url = j.get("url", "").strip()
                job_title = j.get("title", "").strip()
                job_location = (j.get("location") or search_loc or "").strip()
                comp_name = (j.get("company") or "").strip()
                if not job_title or not comp_name:
                    # The organic fallback carries no employer, and inventing one files a web
                    # result as a posting at a company that never advertised it.
                    continue
                # Synonym/alias resolution needs the stored names, or "Uber Technologies" and
                # "Uber" keep creating separate employers (finding 57).
                comp_name = resolve_company_name(comp_name, known_names) or comp_name

                # Shared preference gate: the scheduled runner never ingests outside the
                # operator's criteria, and the manual button must not be a way around it.
                if pref_filter.rejection_reason(
                    job_title, job_location, j.get("description", "") or ""
                ):
                    q_filtered += 1
                    filtered_out_of_preference += 1
                    continue

                # Employer resolution & ATS portal learning (before dedupe: the key needs it)
                comp, _company_created = find_or_create_company(db, comp_name)
                if comp is None:
                    continue

                portal_info = extract_portal_info_from_job_url(job_url, comp_name)
                if portal_info:
                    if portal_info.get("careers_url") and (
                        not comp.careers_url or "google.com" in (comp.careers_url or "")
                    ):
                        comp.careers_url = portal_info["careers_url"]
                    if portal_info.get("domain") and not comp.domain:
                        comp.domain = portal_info["domain"]
                    if (
                        portal_info.get("canonical_apply_url")
                        and "google.com" not in portal_info["canonical_apply_url"]
                    ):
                        j["url"] = portal_info["canonical_apply_url"]
                        job_url = j["url"]

                # Batch deduplication check
                batch_key = (
                    job_url if job_url else f"{job_title}::{comp.name}::{job_location}"
                ).lower()
                if batch_key in seen_in_batch_urls:
                    q_existing += 1
                    already_existing += 1
                    continue
                seen_in_batch_urls.add(batch_key)

                # Database deduplication check: the employer is part of the key, otherwise a
                # posting at employer B is dropped because employer A has the same title.
                existing = None
                if job_url:
                    existing = db.query(Job).filter(Job.url == job_url).first()
                if not existing:
                    existing = (
                        db.query(Job)
                        .filter(
                            (Job.company_id == comp.id)
                            & (Job.title.ilike(job_title))
                            & (Job.source_type == GOOGLE_JOBS_SOURCE_TYPE)
                        )
                        .first()
                    )

                if existing:
                    q_existing += 1
                    already_existing += 1
                    continue

                # Due diligence
                dd = evaluate_job_due_diligence(
                    job_title=job_title, source_type="Direct", portal_jobs=[]
                )

                # Zero-token negative role filter
                dismissal = apply_dismissal_filter(
                    db, job_title, j.get("description", ""), comp.name
                )
                dismissal_match = dismissal.match
                job_status = dismissal.status

                match_score = 0
                match_scored = False
                score_method = match_scored_at = match_band = apply_recommendation = (
                    None
                )
                strengths = []
                gaps = []
                if dismissal_match:
                    q_dismissed += 1
                    dismissed_filtered += 1
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
                        j.get("description") or "",
                        parsed_resume=parsed_resume,
                        job_title=j.get("title") or "",
                    )
                    if not match_analysis:
                        match_analysis = (
                            f"Direct Google Jobs match for alert: '{query_str}'."
                        )

                # Vector Embedding
                job_emb = generate_embeddings(
                    f"{job_title} {clean_job_description(j.get('description', '') or job_title)}"
                )

                new_job = Job(
                    company_id=comp.id,
                    title=job_title,
                    description=j.get(
                        "description",
                        f"Google for Jobs opening: {job_title} at {comp.name}",
                    ),
                    url=j.get("url", ""),
                    salary_range=j.get("salary_range", "Competitive"),
                    location=j.get("location", "Remote / Various"),
                    source=f"Google Jobs ({query_str})",
                    source_type=GOOGLE_JOBS_SOURCE_TYPE,
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
                    is_ghost_job=dd.get("is_ghost_job", False),
                    embedding=job_emb,
                )
                db.add(new_job)
                q_added += 1
                total_added += 1

            db.commit()

            per_query_breakdown.append(
                {
                    "query": query_str,
                    "discovered": q_discovered,
                    "added": q_added,
                    "existing": q_existing,
                    "dismissed": q_dismissed,
                    "filtered": q_filtered,
                    "outcome": outcome_info.get("outcome"),
                }
            )

        summary_msg = (
            f"Google Jobs discovery complete across {len(queries_to_run)} alert query(ies): "
            f"Discovered {total_discovered} posting(s). Added {total_added} new target(s), "
            f"{already_existing} already tracked, {dismissed_filtered} auto-dismissed, "
            f"{filtered_out_of_preference} outside the saved preferences."
        )

        # A per-query outcome was collected but only landed in the breakdown: the log always said
        # "Completed", so a scrape where every query failed looked like a clean run (finding 67).
        endpoint_outcomes: List[Dict[str, Any]] = []
        explicit_failures = {
            "error",
            "browser_busy",
            "robots_blocked",
            "dom_changed",
            "not_jobs_mode",
            "cooling_down",
        }
        query_failures = sum(
            1
            for entry in per_query_breakdown
            if entry.get("outcome") in explicit_failures
        )
        blocked_queries = [
            entry for entry in per_query_breakdown if entry.get("outcome") == "blocked"
        ]
        if blocked_queries:
            channel_status.record(
                endpoint_outcomes,
                "google_jobs",
                channel_status.ERROR,
                f"{len(blocked_queries)}/{len(per_query_breakdown)} queries blocked by Google",
            )
        elif per_query_breakdown and query_failures == len(per_query_breakdown):
            channel_status.record(
                endpoint_outcomes,
                "google_jobs",
                channel_status.ERROR,
                f"all {len(per_query_breakdown)} queries failed",
            )
        elif total_discovered == 0:
            channel_status.record(
                endpoint_outcomes,
                "google_jobs",
                channel_status.DEGRADED_ZERO,
                f"0 of {len(per_query_breakdown)} queries returned any posting",
            )
        else:
            channel_status.record(
                endpoint_outcomes,
                "google_jobs",
                channel_status.OK,
                f"{total_discovered} postings discovered across {len(per_query_breakdown)} queries",
            )
        endpoint_status = channel_status.aggregate_status(endpoint_outcomes)
        endpoint_note = channel_status.describe(endpoint_outcomes)
        log_summary = summary_msg
        if endpoint_note:
            log_summary = f"{summary_msg} Degraded — {endpoint_note}"

        # OperationLog
        try:
            end_tok = token_tracker.get_stats()
            p_used = max(0, end_tok["prompt_tokens"] - start_tok["prompt_tokens"])
            c_used = max(
                0, end_tok["completion_tokens"] - start_tok["completion_tokens"]
            )

            op_log = OperationLog(
                operation_type="google_jobs_scrape",
                status=endpoint_status,
                summary=log_summary,
                details_json=sanitize_log_details(
                    {
                        "queries_scanned": queries_to_run,
                        "location": search_loc,
                        "total_discovered": total_discovered,
                        "total_added": total_added,
                        "already_existing": already_existing,
                        "dismissed_filtered": dismissed_filtered,
                        "filtered_out_of_preference": filtered_out_of_preference,
                        "per_query_breakdown": per_query_breakdown,
                        "channels": endpoint_outcomes,
                    }
                ),
                jobs_count=total_added,
                prompt_tokens=p_used,
                completion_tokens=c_used,
            )
            db.add(op_log)
            db.commit()
        except Exception as log_err:
            logger.warning(f"Failed to log operation for Google Jobs scrape: {log_err}")

        return GoogleJobsScrapeResponse(
            total_discovered=total_discovered,
            total_added=total_added,
            already_existing=already_existing,
            dismissed_filtered=dismissed_filtered,
            filtered_out_of_preference=filtered_out_of_preference,
            queries_scanned=queries_to_run,
            per_query_breakdown=per_query_breakdown,
            message=summary_msg,
        )

    except Exception as e:
        logger.error(f"Error during Google Jobs scrape: {e}")
        # An outer failure previously returned 200 with no channel record at all (finding 67).
        failure_outcomes: List[Dict[str, Any]] = []
        channel_status.record(
            failure_outcomes, "google_jobs", channel_status.ERROR, str(e)
        )
        try:
            db.add(
                OperationLog(
                    operation_type="google_jobs_scrape",
                    status=channel_status.aggregate_status(failure_outcomes),
                    summary=f"Google Jobs scrape failed: {str(e)}",
                    details_json=sanitize_log_details(
                        {
                            "location": search_loc,
                            "per_query_breakdown": per_query_breakdown,
                            "channels": failure_outcomes,
                        }
                    ),
                    jobs_count=0,
                )
            )
            db.commit()
        except Exception as log_err:
            logger.warning(f"Failed to log Google Jobs scrape failure: {log_err}")
        return GoogleJobsScrapeResponse(
            total_discovered=total_discovered,
            total_added=total_added,
            already_existing=already_existing,
            dismissed_filtered=dismissed_filtered,
            queries_scanned=queries_to_run,
            per_query_breakdown=per_query_breakdown,
            message=f"Google Jobs scrape failed: {str(e)}",
        )


_GOOGLE_JOBS_MAX_LOCATIONS = 3
_GOOGLE_JOBS_MAX_PREF_QUERIES = 6


def _run_google_jobs_task_runner(
    queries: Optional[List[str]] = None,
    location: str = "India",
    use_playwright: bool = False,
    cancel_token: Optional[CancellationToken] = None,
    progress_cb: Optional[Callable] = None,
) -> Dict[str, Any]:
    """Background runner for Google for Jobs multi-query discovery scan."""
    db_gen = get_db()
    db = next(db_gen)
    total_discovered = 0
    errors_count = 0
    # How many queries actually returned raw postings. Zero across all queries means the
    # fetcher produced nothing, which is NOT the same as "no new roles to add".
    queries_with_results = 0
    blocked_count = 0
    # Per-query outcome: fetched | browser_busy | dom_changed | robots_blocked | zero_results.
    per_query_outcomes: List[Dict[str, Any]] = []
    # Route Google Jobs browser navigation through the shared HostPacer (delay + jitter + ceiling).
    from backend.ats.pacing import build_pacer

    google_pacer = build_pacer()

    try:
        from backend import google_jobs_state

        cooldown = google_jobs_state.cooldown_reason()
        if cooldown:
            gjo_outcomes = []
            channel_status.record(
                gjo_outcomes, "google_jobs", channel_status.COOLING_DOWN, cooldown
            )
            gjo_status = channel_status.aggregate_status(gjo_outcomes)
            if progress_cb:
                progress_cb(
                    step=1,
                    label=f"Skipped: {cooldown}",
                    items_found=0,
                    total_steps=1,
                )
            db.add(
                OperationLog(
                    operation_type="google_jobs_background_scan",
                    status=gjo_status,
                    summary=f"Google Jobs Scan: skipped — {cooldown}",
                    details_json=sanitize_log_details(
                        {"channels": gjo_outcomes, "cooldown_reason": cooldown}
                    ),
                    jobs_count=0,
                    prompt_tokens=0,
                    completion_tokens=0,
                )
            )
            db.commit()
            return {
                "queries_scanned": 0,
                "jobs_found": 0,
                "filtered_out_of_preference": 0,
                "errors_count": 0,
                "status": gjo_status,
                "skipped": "cooling_down",
            }

        if not queries or len(queries) == 0:
            # Derive queries from persisted user preferences first, then followed queries.
            pref = get_user_preferences(db)
            titles = [
                t.strip() for t in (pref.target_titles or "").split(",") if t.strip()
            ]
            # One query per title x location. Concatenating every city into a single phrase
            # ("jobs in A, B, C, ..., Z") is not a searchable query, so Google answers nothing.
            locs = [
                c.strip() for c in (pref.target_cities or "").split(",") if c.strip()
            ]
            if not locs and (pref.target_country or "").strip():
                locs = [pref.target_country.strip()]
            locs = list(dict.fromkeys(locs))[:_GOOGLE_JOBS_MAX_LOCATIONS]
            remote_suffix = (
                " remote" if (pref.work_mode or "").lower() == "remote" else ""
            )
            pref_queries: List[str] = []
            for title in titles:
                for loc in locs or [""]:
                    phrase = f" in {loc}" if loc else ""
                    pref_queries.append(
                        normalize_google_query(f"{title} jobs{phrase}{remote_suffix}")
                    )
                    if len(pref_queries) >= _GOOGLE_JOBS_MAX_PREF_QUERIES:
                        break
                if len(pref_queries) >= _GOOGLE_JOBS_MAX_PREF_QUERIES:
                    break
            queries = _normalize_google_queries(pref_queries) or load_google_searches()

        if not queries:
            queries = [
                "Senior Software Engineer jobs in India",
                "Staff Software Engineer remote",
            ]

        queries = _normalize_google_queries(queries)
        queries, total_queries = google_jobs_state.select_queries(queries)
        total_steps = len(queries)
        active_resume = db.query(Resume).filter(Resume.is_active == True).first()
        resume_text = (
            decrypt_data(active_resume.content_encrypted) if active_resume else ""
        )
        parsed_resume = _parsed_resume_of(active_resume)
        # Shared preference gate: never ingest roles/locations outside the user's criteria.
        pref_filter = PreferenceFilter.from_db(db)
        known_names = known_company_names(db)
        filtered_count = 0

        for idx, q_str in enumerate(queries):
            if cancel_token:
                cancel_token.check()

            if progress_cb:
                progress_cb(
                    step=idx + 1,
                    label=f"Query {idx + 1}/{total_steps}: '{q_str[:40]}'",
                    items_found=total_discovered,
                    errors=errors_count,
                    total_steps=total_steps,
                )

            try:
                raw_jobs = []
                q_outcome_info: Dict[str, Any] = {}
                try:
                    with sync_playwright() as p:
                        raw_jobs = fetch_google_jobs_playwright(
                            p, query=q_str, location=location, headless=True,
                            max_results=15, pacer=google_pacer, outcome=q_outcome_info,
                        )
                except Exception as p_err:
                    q_outcome = q_outcome_info.get("outcome") or (
                        "browser_busy"
                        if "BrowserProfileBusy" in type(p_err).__name__
                        else "error"
                    )
                    logger.warning(
                        f"[Google Jobs] Playwright fetch failed for '{q_str}' "
                        f"({q_outcome}): {p_err}"
                    )
                    per_query_outcomes.append(
                        {"query": q_str[:80], "outcome": q_outcome, "error": str(p_err)[:200]}
                    )
                    errors_count += 1
                else:
                    q_outcome = q_outcome_info.get("outcome") or (
                        "fetched" if raw_jobs else "zero_results"
                    )
                    if q_outcome in ("blocked", "not_jobs_mode"):
                        blocked_count += 1
                    per_query_outcomes.append(
                        {
                            "query": q_str[:80],
                            "outcome": q_outcome,
                            "count": len(raw_jobs),
                            "url": q_outcome_info.get("url"),
                            "browser_mode": q_outcome_info.get("browser_mode"),
                            "body_signature": q_outcome_info.get("body_signature"),
                        }
                    )
                    if q_outcome == "blocked":
                        logger.warning(
                            f"[Google Jobs] Blocked by Google for '{q_str}' "
                            "(consent wall or unusual-traffic interstitial)."
                        )
                        google_jobs_state.record_block(f"blocked on query '{q_str[:60]}'")
                        if google_jobs_state.last_save_error():
                            # The cooldown is running in-memory only; say so instead of implying
                            # the block survived (finding 49).
                            logger.error(
                                "Google Jobs cooldown could NOT be persisted: "
                                f"{google_jobs_state.last_save_error()}"
                            )
                            per_query_outcomes.append(
                                {
                                    "query": q_str[:80],
                                    "outcome": "blocked",
                                    "cooldown_persisted": False,
                                }
                            )
                        break
                    elif q_outcome == "not_jobs_mode":
                        logger.warning(
                            f"[Google Jobs] Redirect dropped the jobs mode for '{q_str}'; "
                            "landed on a plain web SERP."
                        )
                    elif q_outcome == "cooling_down":
                        logger.warning(
                            f"[Google Jobs] Pacer refused '{q_str}': google.com is cooling "
                            "down or over its per-run ceiling; stopping this run."
                        )
                        break
                    elif q_outcome == "dom_changed":
                        logger.warning(
                            f"[Google Jobs] DOM selector miss for '{q_str}' (dom_changed); "
                            "Google may have changed the jobs panel markup."
                        )

                if raw_jobs:
                    queries_with_results += 1

                for rj in raw_jobs:
                    if cancel_token:
                        cancel_token.check()

                    comp_name = (rj.get("company_name") or "").strip()
                    comp_name = (
                        resolve_company_name(comp_name, known_names) or comp_name
                    )
                    j_title = (rj.get("title") or "").strip()
                    j_url = (rj.get("apply_url") or rj.get("url") or "").strip()
                    j_location = rj.get("location") or location

                    # A posting with no URL is still a posting: the employer + title + location
                    # key identifies it, and the old `or url` fallback stored the SERP link.
                    if not comp_name or not j_title:
                        continue

                    # Preference gate (title / location / work-mode) before ingestion.
                    if pref_filter.rejection_reason(
                        j_title, j_location, rj.get("description", "")
                    ):
                        filtered_count += 1
                        continue

                    # Upsert company first: the duplicate key is employer-scoped, otherwise a
                    # posting at employer B is dropped because employer A has the same title.
                    comp_rec, _company_created = find_or_create_company(db, comp_name)
                    if comp_rec is None:
                        continue

                    key = (Job.company_id == comp_rec.id) & (Job.title == j_title) & (
                        Job.location == j_location
                    )
                    exists = (
                        db.query(Job).filter((Job.url == j_url) | key).first()
                        if j_url
                        else db.query(Job).filter(key).first()
                    )
                    if exists:
                        continue

                    # Same gate and scorer as the manual endpoint: a fabricated 75.0 made every
                    # Google row outrank genuinely scored ones, and dismissed roles were ingested
                    # because this path never ran the dismissal filter (finding 66).
                    dismissal = apply_dismissal_filter(
                        db, j_title, rj.get("description") or "", comp_rec.name
                    )
                    job_status = dismissal.status
                    match_score = 0
                    match_analysis = None
                    match_scored = False
                    score_method = match_scored_at = match_band = (
                        apply_recommendation
                    ) = None
                    strengths = []
                    gaps = []
                    partials = []
                    if dismissal.match:
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
                            rj.get("description") or "",
                            parsed_resume=parsed_resume,
                            job_title=j_title,
                        )
                        if not match_analysis:
                            match_analysis = (
                                f"Direct Google Jobs match for alert: '{q_str}'."
                            )

                    job_emb = generate_embeddings(
                        f"{j_title} {clean_job_description(rj.get('description') or j_title)}"
                    )
                    new_job = Job(
                        company_id=comp_rec.id,
                        title=j_title,
                        description=rj.get("description")
                        or f"Google for Jobs posting for {j_title} at {comp_name}.",
                        url=j_url,
                        location=j_location,
                        source="google_jobs",
                        source_type=GOOGLE_JOBS_SOURCE_TYPE,
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
                        embedding=job_emb,
                    )
                    db.add(new_job)
                    db.flush()
                    total_discovered += 1

                db.commit()
            except InterruptedError:
                raise
            except Exception as q_err:
                errors_count += 1
                logger.warning(f"Error scanning query '{q_str}': {q_err}")

        if progress_cb:
            progress_cb(
                step=total_steps,
                label=f"Completed {total_steps} queries. Discovered {total_discovered} roles ({filtered_count} filtered out).",
                items_found=total_discovered,
                errors=errors_count,
                total_steps=total_steps,
            )

        if not blocked_count:
            google_jobs_state.record_run(
                len(queries), total_queries, reached=total_steps - errors_count
            )

        gjo_outcomes = []
        if errors_count >= total_steps:
            channel_status.record(
                gjo_outcomes, "google_jobs", channel_status.ERROR,
                f"all {total_steps} queries raised",
            )
        elif blocked_count:
            until = google_jobs_state.cooling_down_until()
            deadline = (
                f"; cooling down until {until.strftime('%Y-%m-%d %H:%M UTC')}"
                if until
                else ""
            )
            channel_status.record(
                gjo_outcomes, "google_jobs", channel_status.ERROR,
                f"{blocked_count}/{total_steps} queries blocked by Google "
                f"(consent wall or unusual-traffic interstitial){deadline}",
            )
        elif queries_with_results == 0:
            channel_status.record(
                gjo_outcomes, "google_jobs", channel_status.DEGRADED_ZERO,
                f"0 of {total_steps} queries returned any posting",
            )
        else:
            channel_status.record(
                gjo_outcomes, "google_jobs", channel_status.OK,
                f"{queries_with_results}/{total_steps} queries returned postings",
            )
        gjo_status = channel_status.aggregate_status(gjo_outcomes)
        gjo_summary = (
            f"Google Jobs Scan: {total_steps} queries scanned, "
            f"{total_discovered} new roles discovered, {filtered_count} filtered by preferences."
        )
        gjo_note = channel_status.describe(gjo_outcomes)
        if gjo_note:
            gjo_summary = f"{gjo_summary} Degraded — {gjo_note}"
        db.add(
            OperationLog(
                operation_type="google_jobs_background_scan",
                status=gjo_status,
                summary=gjo_summary,
                details_json=sanitize_log_details(
                    {
                        "filtered_out_of_preference": filtered_count,
                        "queries_with_results": queries_with_results,
                        "queries_failed": errors_count,
                        "queries_available": total_queries,
                        "per_query_outcomes": per_query_outcomes,
                        "channels": gjo_outcomes,
                    }
                ),
                jobs_count=total_discovered,
                prompt_tokens=0,
                completion_tokens=0,
            )
        )
        db.commit()

        return {
            "queries_scanned": total_steps,
            "jobs_found": total_discovered,
            "filtered_out_of_preference": filtered_count,
            "errors_count": errors_count,
            "status": gjo_status,
        }
    finally:
        db.close()
