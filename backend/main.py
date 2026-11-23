import datetime
import json
import logging
import os
import re
import secrets
import threading
import time
import urllib.parse
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from fastapi import (
    Depends,
    FastAPI,
    File,
    HTTPException,
    Query,
    Request,
    Security,
    UploadFile,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import APIKeyHeader
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.orm import Session

from backend import channel_status
from backend import linkedin_scan_queue
from backend.job_scoring import (  # noqa: F401 - re-exported for tests
    ScoringResult,  # noqa: F401 - re-exported for tests
    _active_resume_row,
    _check_eligibility,
    _derive_strengths_gaps,
    _deterministic_analysis_text,
    _deterministic_match,
    _match_analysis_text,
    _merge_extra_skills,
    _normalize_extra_skills,
    _parsed_resume_of,
    _score_job_with_engine,
    _score_provenance,
    get_extra_skills,
)


def _resume_score_counts(db: Session) -> dict:
    """Map resume fingerprint -> number of stored scores produced by it (issue #58)."""
    from sqlalchemy import func

    rows = (
        db.query(Job.scored_resume_fingerprint, func.count(Job.id))
        .filter(Job.scored_resume_fingerprint.isnot(None))
        .group_by(Job.scored_resume_fingerprint)
        .all()
    )
    return {fingerprint: count for fingerprint, count in rows if fingerprint}


def _resume_provenance_context(db: Session) -> dict:
    """Lookup tables for deriving ``score_resume_state`` without a query per job."""
    active = _active_resume_row(db)
    known = {}
    for fingerprint, filename, created_at in db.query(
        Resume.fingerprint, Resume.filename, Resume.created_at
    ).all():
        if fingerprint:
            known.setdefault(fingerprint, (filename, created_at))
    return {
        "active_fingerprint": active.fingerprint if active else None,
        "known": known,
    }


def _apply_score_provenance(job_out: "JobOut", job: Job, ctx: dict) -> "JobOut":
    """Label a serialized job with the state of the resume version that produced its score.

    ``current`` also covers rows scored before provenance existed: unknown is treated as current
    (decision on #56) so the existing feed is not retroactively marked degraded.
    """
    fingerprint = getattr(job, "scored_resume_fingerprint", None)
    if not fingerprint:
        job_out.score_resume_state = "current"
        return job_out
    if fingerprint == ctx.get("active_fingerprint"):
        job_out.score_resume_state = "current"
    elif fingerprint in ctx.get("known", {}):
        job_out.score_resume_state = "stale"
    else:
        job_out.score_resume_state = "deleted"
    filename, created_at = ctx.get("known", {}).get(fingerprint, (None, None))
    job_out.score_resume_filename = filename
    job_out.score_resume_created_at = created_at
    return job_out


def _attach_outreach_guidance(db: Session, job: Job, job_out: "JobOut") -> "JobOut":
    """Attach human-in-the-loop talking points when the job has an outreach draft (issue #27).

    Derived on read from the JD and the active resume, so nothing is persisted and no score is
    affected. Best-effort: a failure here must never break serialization.
    """
    if not job.cold_message_draft:
        return job_out
    try:
        active = (
            db.query(Resume)
            .filter(Resume.is_active == True)
            .order_by(Resume.created_at.desc())
            .first()
        )
        if not active:
            return job_out
        from backend.ai_helper import build_outreach_guidance

        job_out.outreach_guidance = build_outreach_guidance(
            job.description or "",
            decrypt_data(active.content_encrypted),
            job.title or "",
        )
    except Exception as e:
        logger.debug(f"Outreach guidance unavailable for job {job.id}: {e}")
    return job_out


def _job_out(db: Session, job: Job, ctx: Optional[dict] = None) -> "JobOut":
    """Serialize a job with company name and score provenance attached."""
    job_out = JobOut.model_validate(job)
    job_out.company_name = job.company.name if job.company else None
    _apply_score_provenance(job_out, job, ctx or _resume_provenance_context(db))
    return _attach_outreach_guidance(db, job, job_out)

from backend.matchers import detect_foreign_restriction
from backend.playwright_app import search_linkedin_talent_partners
from backend.prompts import interview_prep_advisor_system

logger = logging.getLogger("api")

from playwright.sync_api import sync_playwright

from backend.ai_helper import (
    MIN_JOB_DESCRIPTION_CHARS,
    assess_job_requirements,
    cached_active_llm_provider,
    extract_job_skills_and_alignment,
    generate_consolidated_application_package,
    generate_embeddings,
    generate_interview_questions,
    generate_text,
    index_dismissed_job_pattern,
    parse_resume_to_json,
)
from backend.chat_agent import CareerChatAgent
from backend.company_catalog import (
    get_catalog_company,
    load_company_catalog,
    parse_company_import,
)
from backend.config import (
    API_KEY,
    BASE_DIR,
    INTERACTIVE_SESSION_HOLD_SECONDS,
    LLM_INTERACTIVE_TIMEOUT,
    clear_gmail_settings,
    decrypt_data,
    encrypt_data,
    get_gmail_settings,
    is_gmail_configured,
    is_valid_api_key,
    save_gmail_settings,
)
from backend.country_hubs import load_country_hubs
from backend.database import (
    CONSENT_REQUIRED_FEATURES,
    CONSENT_VERSION,
    DEFAULT_FEATURE_FLAGS,
    DEFAULT_EXCLUDED_COMPANIES,
    ApplicationEvent,
    Company,
    Job,
    OperationLog,
    Resume,
    ScoreAuditRecord,
    EmailApplicationLink,
    EmailSuppression,
    PendingEmailApplication,
    get_consents,
    get_db,
    get_feature_flags,
    get_scoring_config,
    get_user_preferences,
    has_consent,
    init_db,
    is_feature_enabled,
    record_consent,
)
from backend.llm_queue import LLMPriority
from backend.matchers import check_title_match
from backend.match_scoring import ScoringConfig
from backend.observability import sanitize_log_details, setup_observability  # noqa: F401 - re-exported for tests
from backend.parser import (
    RESUME_PARSER_VERSION,
    clean_job_description,
    parse_resume_document,
    resume_fingerprint,
    split_candidate_name,
)
from backend.scoring_constants import DISMISSAL_SIMILARITY_THRESHOLD
from backend.text_utils import SNIPPET_CHARS, truncate_preview
from backend.routers.discovery import (  # noqa: F401 - ScrapeResponse re-exported for tests
    ScrapeRequest,
    ScrapeResponse,
    _execute_ats_scrape,
    router as discovery_router,
)
from backend.routers.operations import router as operations_router
from backend.google_searches import (  # noqa: F401 - re-exported for the dispatch route and tests
    load_google_searches,
    normalize_google_query,
)
from backend.routers.google_jobs import (  # noqa: F401 - re-exported for the sync-all runner and tests
    _GOOGLE_JOBS_MAX_PREF_QUERIES,
    _run_google_jobs_task_runner,
    router as google_jobs_router,
)
from backend.routers.linkedin import (  # noqa: F401 - re-exported for the sync-all runner and tests
    LinkedInSyncResponse,
    _LINKEDIN_SCAN_GATE,
    _log_linkedin_automation_warning,
    _run_linkedin_sync_task_runner,
    _run_queued_linkedin_scan,
    _submit_queued_linkedin_scan,
    router as linkedin_router,
)
from backend.routers.memory_qa import router as memory_qa_router
from backend.routers.settings import router as settings_router
from backend.routers.tasks import router as tasks_router
from backend.playwright_app import (
    scan_and_sync_hirist_applications,
    sync_email_application_events,
)
from backend.preference_filter import (
    PreferenceFilter,
    check_location_match,
    check_workmode_match,
)
from backend.resume_index import index_resume_chunks, top_evidence
from backend.scheduler import scheduler
from backend.scraper import (
    extract_opportunity_company_and_title,
    find_talent_partners,
    gather_company_intelligence,
    probe_greenhouse_job_active,
    probe_lever_job_active,
    probe_smartrecruiters_job_active,
    _is_ats_or_freemail_domain,
    _is_freemail_domain,
)
from backend.requisition import extract_requisition_id
from backend.task_engine import CancellationToken, task_engine
from backend.title_parser import safe_company_name as _safe_company_name

# Graceful shutdown is driven entirely by the ASGI lifespan (see `_lifespan` below). We
# deliberately do NOT register our own signal handlers: uvicorn owns SIGTERM/SIGINT and calls the
# lifespan on every exit path (--reload child, --workers, standalone). See backend/shutdown.py for
# why.


@asynccontextmanager
async def _lifespan(application):
    """The one startup/shutdown hook (the three ``on_event`` handlers were deprecated).

    Order matters: observability and the schema must exist before the handlers that query the
    database, and the recovery step re-queues work only after the task engine is usable.
    """
    _bootstrap_runtime()
    _startup_operation_log_maintenance()
    _recover_queued_linkedin_scans()
    try:
        yield
    finally:
        _graceful_shutdown_drain()


app = FastAPI(
    title="Job Alert Agent API",
    description="Autonomous job hunting, application tracking, resume ATS tailoring, and browser assistance.",
    version="1.0.0",
    lifespan=_lifespan,
)

app.include_router(discovery_router)
app.include_router(google_jobs_router)
app.include_router(linkedin_router)
app.include_router(operations_router)
app.include_router(tasks_router)
app.include_router(memory_qa_router)
app.include_router(settings_router)


def _bootstrap_runtime():
    """Observability, schema and scheduler — once per process, on startup, not on import.

    These ran at module scope, so every ``import backend.main`` (including pytest collection
    and every maintenance script) opened the configured database, flipped it to WAL, ran
    ``create_all`` plus the migrations, and could start a discovery scheduler thread. Importing
    a module must not do that; a server starting must. Scripts therefore call
    ``backend.database.init_db()`` themselves.
    """
    setup_observability()
    init_db()
    # Maintenance scripts set JOB_ALERT_AGENT_NO_SCHEDULER=1 so importing the app never starts
    # a discovery thread in their process.
    if os.environ.get("JOB_ALERT_AGENT_NO_SCHEDULER") != "1":
        scheduler.start()


def _startup_operation_log_maintenance():
    """Rotates the operation_logs table once on startup so long-running DBs stay bounded."""
    try:
        from backend.database import SessionLocal, prune_operation_logs

        db = SessionLocal()
        try:
            prune_operation_logs(db)
        finally:
            db.close()
    except Exception as e:
        logger.debug(f"Startup operation-log rotation skipped: {e}")


def _recover_queued_linkedin_scans():
    """Re-queue LinkedIn scans that were still waiting when the previous process exited (#115).

    A row left 'queued' never started, so it is safe to run now; a row left 'running' is marked
    cancelled by the recovery helper because its outcome is unknown.
    """
    try:
        from backend.database import SessionLocal

        db = SessionLocal()
        try:
            request_ids = linkedin_scan_queue.recover_stale_requests(db)
        finally:
            db.close()
        for request_id in request_ids:
            logger.info(f"Re-queueing LinkedIn scan #{request_id} after restart.")
            _submit_queued_linkedin_scan(request_id)
    except Exception as e:  # noqa: BLE001 - recovery must never block startup
        logger.debug(f"Startup LinkedIn queue recovery skipped: {e}")


def _graceful_shutdown_drain():
    """Drain background work on server shutdown (SIGTERM/SIGINT/reload).

    The ASGI server drives this on its own graceful-exit path, so it fires identically
    for `--reload` children, `--workers`, and standalone runs. Doing the drain here
    (rather than inside a signal handler) keeps the event loop intact and lets the
    server/reloader parent survive and restart us.
    """
    try:
        from backend.shutdown import run_shutdown

        run_shutdown()
    except Exception as e:
        logger.warning(f"Graceful shutdown drain failed: {e}")


# -------------------------------------------------------------
# Zero-Trust CORS & Security Perimeter Configuration
# -------------------------------------------------------------
ALLOWED_ORIGINS = [
    "http://localhost:8000",
    "http://127.0.0.1:8000",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
]
custom_origins = os.environ.get("ALLOWED_ORIGINS", "")
if custom_origins:
    for org in custom_origins.split(","):
        clean_org = org.strip()
        if clean_org and clean_org not in ALLOWED_ORIGINS:
            ALLOWED_ORIGINS.append(clean_org)

# Browser extensions are trusted only when their exact extension ID is explicitly
# allowlisted via ALLOWED_EXTENSION_IDS (comma-separated). By default no extension
# origin is permitted; the extension authenticates with the X-API-Key header.
_allowed_extension_ids = [
    eid.strip()
    for eid in os.environ.get("ALLOWED_EXTENSION_IDS", "").split(",")
    if eid.strip()
]
_extension_origin_regex = (
    r"^chrome-extension://("
    + "|".join(re.escape(eid) for eid in _allowed_extension_ids)
    + r")$"
    if _allowed_extension_ids
    else None
)

def _origin_is_local(value: Optional[str]) -> bool:
    """True only when a URL's parsed host is exactly a loopback host.

    Uses exact host comparison (never substring matching), so origins such as
    ``http://localhost.evil.com`` are correctly rejected.
    """
    if not value:
        return False
    from urllib.parse import urlsplit

    try:
        host = (urlsplit(value).hostname or "").lower()
    except Exception:
        return False
    return host in ("localhost", "127.0.0.1", "::1", "0.0.0.0")


app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_origin_regex=_extension_origin_regex,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _log_safe_header(value: Optional[str], limit: int = 200) -> str:
    """Make a request header safe for a single log line (no CR/LF, bounded length)."""
    if not value:
        return "-"
    cleaned = "".join(ch for ch in str(value) if ch.isprintable())
    return cleaned[:limit] or "-"


@app.middleware("http")
async def security_firewall_middleware(request: Request, call_next):
    """
    Zero-Trust Security Firewall:
    - Automatically allows public documentation, health endpoints, and static assets.
    - Requires verified X-API-Key or verified same-origin session for all REST API endpoints.
    - Blocks cross-site unauthorized requests from arbitrary websites.

    Declared BEFORE ``request_observability_middleware`` on purpose: Starlette inserts each
    middleware at the front of the stack, so the last one declared is the outermost. The firewall
    rejects with a 401 without calling ``call_next``, so it must sit *inside* the observability
    middleware or the rejection produces no log line and no ``X-Request-ID`` (issue #92/#93).
    """
    path = request.url.path
    if (
        path
        in (
            "/",
            "/api/health",
            "/api/auth/status",
            "/docs",
            "/openapi.json",
            "/favicon.ico",
            "/api/gmail/oauth2callback",
        )
        or path.startswith("/static/")
        or request.method == "OPTIONS"
    ):
        return await call_next(request)

    try:
        # 1. Header Validation
        api_key = request.headers.get("X-API-Key")
        if api_key and is_valid_api_key(api_key):
            return await call_next(request)

        # 2. Bearer Token
        auth_header = request.headers.get("authorization")
        if auth_header and auth_header.lower().startswith("bearer "):
            if is_valid_api_key(auth_header[7:].strip()):
                return await call_next(request)

        # 3. Same-Origin Local Browser Session
        origin = request.headers.get("origin")
        referer = request.headers.get("referer")
        sec_fetch_site = request.headers.get("sec-fetch-site")

        if (
            _origin_is_local(origin)
            or _origin_is_local(referer)
            or (sec_fetch_site == "same-origin")
        ):
            return await call_next(request)

        # 4. Localhost direct script/testing fallback if loopback caller has no external origin
        client_host = request.client.host if request.client else ""
        if client_host in ("127.0.0.1", "::1", "testclient") and not origin:
            return await call_next(request)

        return JSONResponse(
            status_code=401,
            content={
                "detail": "Unauthorized: Valid X-API-Key header or same-origin session required."
            },
        )
    except Exception as e:
        logger.error(f"Security firewall error: {e}")
        return JSONResponse(status_code=401, content={"detail": "Unauthorized"})


@app.middleware("http")
async def request_observability_middleware(request: Request, call_next):
    """
    HTTP Request Observability & Correlation Middleware.
    Assigns unique X-Request-ID, logs request lifecycle, status codes, and latency in milliseconds.

    Outermost middleware, so it also sees the security firewall's own 401 responses.
    """
    req_id = (
        request.headers.get("X-Request-ID")
        or f"req_{int(time.time() * 1000) % 1000000:06d}"
    )
    request.state.request_id = req_id
    t0 = time.perf_counter()

    try:
        response = await call_next(request)
        latency_ms = (time.perf_counter() - t0) * 1000
        response.headers["X-Request-ID"] = req_id

        # Avoid noisy health check polling logs at INFO level
        if request.url.path not in ("/api/health", "/favicon.ico"):
            if response.status_code == 404:
                # An unknown path is usually a stale client build or an external probe. Without
                # caller identity a 404 storm cannot be attributed to anything (issue #93), so
                # record headers only - never bodies.
                logger.info(
                    f"{request.method} {request.url.path} -> 404 ({latency_ms:.1f}ms) "
                    f"ua={_log_safe_header(request.headers.get('user-agent'))} "
                    f"referer={_log_safe_header(request.headers.get('referer'))}",
                    extra={"request_id": req_id},
                )
            else:
                logger.info(
                    f"{request.method} {request.url.path} -> {response.status_code} ({latency_ms:.1f}ms)",
                    extra={"request_id": req_id},
                )
        return response
    except Exception as exc:
        latency_ms = (time.perf_counter() - t0) * 1000
        logger.error(
            f"{request.method} {request.url.path} -> ERROR: {exc} ({latency_ms:.1f}ms)",
            extra={"request_id": req_id},
        )
        raise


# -------------------------------------------------------------
# API Key Authentication & Verification Dependency
# -------------------------------------------------------------
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def verify_api_key(
    request: Request, api_key: Optional[str] = Security(api_key_header)
) -> bool:
    """
    Zero-Trust Security Dependency:
    - Validates X-API-Key header against active system key using constant-time comparison.
    - Also supports Authorization: Bearer <token>.
    - Automatically allows same-origin requests originating from local dashboard/SPA.
    - Rejects cross-site / unauthenticated requests with HTTP 401 Unauthorized.
    """
    # 1. Header Validation (Primary for Extension and external API callers)
    if api_key and is_valid_api_key(api_key):
        return True

    # 2. Bearer Token in Authorization header
    auth_header = request.headers.get("authorization")
    if auth_header and auth_header.lower().startswith("bearer "):
        token = auth_header[7:].strip()
        if is_valid_api_key(token):
            return True

    # 3. Same-Origin Local Browser Session (SPA loaded directly from this server)
    origin = request.headers.get("origin")
    referer = request.headers.get("referer")
    sec_fetch_site = request.headers.get("sec-fetch-site")

    if (
        _origin_is_local(origin)
        or _origin_is_local(referer)
        or (sec_fetch_site == "same-origin")
    ):
        return True

    # 4. Localhost direct script/testing fallback if loopback caller has no external origin
    client_host = request.client.host if request.client else ""
    if client_host in ("127.0.0.1", "::1", "testclient") and not origin:
        return True

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Unauthorized: Valid X-API-Key header or same-origin session required.",
        headers={"WWW-Authenticate": "ApiKey"},
    )


# -------------------------------------------------------------
# Auth Status & Client Config Endpoints
# -------------------------------------------------------------
@app.get("/api/auth/status")
def get_auth_status():
    """Public endpoint to check if API key protection is enabled and active."""
    return {
        "status": "online",
        "auth_required": True,
        "api_key_configured": bool(API_KEY),
        "allowed_origins": ALLOWED_ORIGINS,
    }


@app.get("/api/auth/client-config", dependencies=[Depends(verify_api_key)])
def get_client_config(request: Request):
    """Returns application client configuration and active API Key for local client handshake.

    Restricted to true loopback callers so that browser extensions / remote origins
    cannot read the raw API key.
    """
    client_host = request.client.host if request.client else ""
    if client_host not in ("127.0.0.1", "::1", "testclient"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Client config is only available to local loopback clients.",
        )
    return {"api_key": API_KEY, "version": "1.0.0"}


# -------------------------------------------------------------
# Pydantic Schemas
# -------------------------------------------------------------
class CompanyCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    domain: Optional[str] = None
    careers_url: Optional[str] = None
    description: Optional[str] = None
    market_position: Optional[str] = None
    talking_points: Optional[str] = None


class CompanyUpdate(BaseModel):
    name: Optional[str] = None
    domain: Optional[str] = None
    careers_url: Optional[str] = None
    description: Optional[str] = None
    recent_news: Optional[str] = None
    salary_insights: Optional[str] = None
    hiring_process: Optional[str] = None
    market_position: Optional[str] = None
    talking_points: Optional[str] = None


class CompanyOut(BaseModel):
    id: int
    name: str
    domain: Optional[str] = None
    careers_url: Optional[str] = None
    description: Optional[str] = None
    reviews_summary: Optional[str] = None
    salary_insights: Optional[str] = None
    hiring_process: Optional[str] = None
    recent_news: Optional[str] = None
    market_position: Optional[str] = None
    talking_points: Optional[str] = None
    created_at: Optional[datetime.datetime] = None

    model_config = ConfigDict(from_attributes=True)


JOB_STATUSES = [
    "To Apply",
    "Shortlisted",
    "Applied",
    "Screening",
    "Interview Scheduling",
    "Interview",
    "Offered",
    "Rejected",
    "Closed",
    "Withdrawn",
    "Not Interested",
]

# States that end an application. "Closed" is a posting that disappeared (filled, expired,
# unlisted) and "Withdrawn" is the operator backing out — neither is a rejection by the employer.
TERMINAL_JOB_STATUSES = ("Rejected", "Closed", "Withdrawn", "Not Interested")


def _canonical_job_status(value: str) -> str:
    canonical = next(
        (s for s in JOB_STATUSES if s.lower() == str(value).strip().lower()), None
    )
    if not canonical:
        raise ValueError(
            f"Invalid status '{value}'. Allowed: {', '.join(JOB_STATUSES)}"
        )
    return canonical


class JobCreate(BaseModel):
    company_id: int
    title: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    url: Optional[str] = None
    salary_range: Optional[str] = None
    location: Optional[str] = None
    source: Optional[str] = "Direct"
    source_type: Optional[str] = "Direct"
    status: Optional[str] = "To Apply"
    match_score: Optional[float] = None
    match_analysis: Optional[str] = None
    is_ghost_job: Optional[bool] = False

    @field_validator("status")
    @classmethod
    def _validate_status(cls, v):
        return _canonical_job_status(v) if v is not None else v


class JobStatusUpdate(BaseModel):
    status: str = Field(
        ...,
        description="To Apply, Shortlisted, Applied, Screening, Interview Scheduling, Interview, Offered, Rejected, Closed, Withdrawn, Not Interested",
    )
    note: Optional[str] = None

    @field_validator("status")
    @classmethod
    def _validate_status(cls, v):
        return _canonical_job_status(v)


class JobOut(BaseModel):
    id: int
    company_id: int
    company_name: Optional[str] = None
    title: str
    description: Optional[str] = None
    url: Optional[str] = None
    salary_range: Optional[str] = None
    location: Optional[str] = None
    source: Optional[str] = None
    source_type: Optional[str] = None
    status: str
    match_score: Optional[float] = None
    match_analysis: Optional[str] = None
    match_strengths: Optional[List[str]] = None
    match_gaps: Optional[List[str]] = None
    match_partials: Optional[List[str]] = None
    cover_letter_draft: Optional[str] = None
    tailored_resume_points: Optional[str] = None
    cold_message_draft: Optional[str] = None
    # Talking points for human-written fields (issue #27). Deterministic, never generated:
    # only JD must-haves the resume evidences. Populated when a draft exists.
    outreach_guidance: Optional[List[Dict[str, Any]]] = None
    is_ghost_job: bool
    repost_count: int
    submission_confirmed: bool = False
    sync_batch_id: Optional[str] = None
    match_scored: bool = False
    score_method: Optional[str] = None
    match_scored_at: Optional[datetime.datetime] = None
    match_band: Optional[str] = None
    apply_recommendation: Optional[str] = None
    # Which resume version produced this score (issue #56). "current" also covers rows scored
    # before provenance existed, per the decision that unknown is treated as current.
    score_resume_state: str = "current"
    score_resume_filename: Optional[str] = None
    score_resume_created_at: Optional[datetime.datetime] = None
    applied_at: Optional[datetime.datetime] = None
    interview_scheduled_at: Optional[datetime.datetime] = None
    rejected_at: Optional[datetime.datetime] = None
    created_at: Optional[datetime.datetime] = None
    updated_at: Optional[datetime.datetime] = None

    model_config = ConfigDict(from_attributes=True)


class ApplicationEventCreate(BaseModel):
    event_type: str
    description: Optional[str] = None


class ApplicationEventOut(BaseModel):
    id: int
    job_id: int
    event_type: str
    description: Optional[str] = None
    timestamp: datetime.datetime

    model_config = ConfigDict(from_attributes=True)


class ResumeOut(BaseModel):
    id: int
    filename: str
    is_active: bool
    created_at: datetime.datetime
    raw_preview: str
    parsed_json: Optional[dict] = None
    # Parser provenance (issue #54): a stored parse is a cache, so it must say which parser
    # produced it and whether that is still the current one.
    parser_version: Optional[int] = None
    parse_stale: bool = False
    # How many stored job scores were produced by this version (issue #58). Non-zero means
    # deleting it orphans those scores, which the UI warns about before the fact.
    score_count: int = 0


class PruneJobsRequest(BaseModel):
    target_titles: Optional[str] = None
    target_locations: Optional[str] = None
    target_cities: Optional[str] = None
    work_mode: Optional[str] = None


class ApplyMaterial(BaseModel):
    """Everything the browser needs to apply, returned instead of launching one server-side."""

    job_id: int
    title: str
    company: str = ""
    url: str
    cover_letter: str = ""
    tailored_points: str = ""
    cold_msg: str = ""


class ApplyResponse(BaseModel):
    job_id: int
    status: str
    message: str
    apply: ApplyMaterial


class BulkApplyRequest(BaseModel):
    job_ids: List[int] = Field(
        ..., description="List of job IDs to apply for in browser tabs"
    )


class BulkApplyResponse(BaseModel):
    message: str
    applied_job_ids: List[int]
    jobs: List[ApplyMaterial] = Field(default_factory=list)


class ExternalSyncRequest(BaseModel):
    sources: List[str] = ["hirist", "greenhouse", "lever"]
    max_pages: int = 10
    headless: bool = True


class ExternalSyncResponse(BaseModel):
    hirist_checked: int = 0
    hirist_updated: int = 0
    hirist_ingested: int = 0
    emails_checked: int = 0
    status_updates: int = 0
    opportunities_added: int = 0
    pending_email_applications: int = 0
    details: List[str] = []
    message: str = ""
    # "success" | "Partial" | "Failed" — see backend.channel_status.aggregate_status.
    status: str = "success"
    channels: List[dict] = []




class ChatRequest(BaseModel):
    message: str


class ChatResponse(BaseModel):
    reply: str
    job_id: int


class ChatMessage(BaseModel):
    role: str = Field(..., description="Role: 'user', 'assistant', or 'system'")
    content: str
    timestamp: Optional[str] = None


class ChatAssistantRequest(BaseModel):
    message: str
    history: List[ChatMessage] = []
    job_id: Optional[int] = None


class ChatAssistantResponse(BaseModel):
    reply: str
    actions_taken: List[dict] = []
    embedded_jobs: List[dict] = []


class MatchAnalyzeRequest(BaseModel):
    job_title: str = Field(..., min_length=1, max_length=500)
    company: Optional[str] = Field(default="", max_length=255)
    job_description: Optional[str] = Field(default="", max_length=20000)
    location: Optional[str] = Field(default="", max_length=255)
    resume_text: Optional[str] = Field(default=None, max_length=60000)


class MatchAnalyzeResponse(BaseModel):
    match_score: Optional[float] = Field(
        default=None,
        description="Deterministic requirement-coverage score (0.0 to 100.0); None when analysis failed",
    )
    strengths: List[str] = Field(default_factory=list)
    gaps: List[str] = Field(default_factory=list)
    partials: List[str] = Field(default_factory=list, description="Must-have requirements met partially (some resume evidence, not full coverage)")
    feedback: str = ""
    resume_available: bool = True
    analysis_source: str = Field(
        default="llm",
        description="'llm' for AI analysis, 'classifier' for ML-based, 'error' when no reliable score could be produced",
    )
    error: Optional[str] = Field(
        default=None,
        description="Human-readable failure reason; no score is shown when set",
    )
    job_id: Optional[int] = Field(
        default=None,
        description="Job ID from database (if result came from saved job); None for ad-hoc analysis",
    )
    band: Optional[str] = None
    apply_recommendation: Optional[str] = None
    must_coverage: Optional[float] = None
    nice_coverage: Optional[float] = None
    over_qualified: bool = False
    eligible: bool = True
    ineligibility_reason: Optional[str] = None
    requirements: List[Dict[str, Any]] = Field(default_factory=list)
    unverified: List[str] = Field(default_factory=list)
    score_method: Optional[str] = None
    match_method: Optional[str] = Field(
        default=None,
        description="Scoring method used: 'classifier_v1' (fast ML), 'requirement_coverage_v1' (LLM-based)",
    )


class SkillItem(BaseModel):
    name: str
    status: str
    type: str


class SkillsAlignmentResponse(BaseModel):
    job_id: int
    compatibility_pct: Optional[int] = None
    total_stack_count: int
    matched_count: int
    gap_count: int
    matched_skills: List[SkillItem]
    gap_skills: List[SkillItem]
    core_stack: List[str]
    confidence: str = "medium"
    summary: str


class HiringContact(BaseModel):
    author_name: str
    author_headline: str
    author_profile_url: str
    post_url: Optional[str] = None
    cold_outreach_draft: Optional[str] = None
    opportunity_url: Optional[str] = None
    source: str = "LinkedIn"


class JobHiringTeamResponse(BaseModel):
    job_id: int
    company_name: str
    contacts_count: int
    contacts: List[HiringContact]


# -------------------------------------------------------------
# Health & Status Endpoint
# -------------------------------------------------------------
@app.get("/api/health")
def health_check(db: Session = Depends(get_db)):
    """Liveness check: database reachable, and whether a graceful drain is in progress.

    Reports ``shutting_down: true`` while a graceful drain is in progress, so a
    supervisor can distinguish "draining" from "dead" during a restart.

    Deliberately cheap and side-effect free. It used to probe every LLM provider (serial HTTP,
    1.5 s timeout each) and resize the worker pool on each call, on a public route that gets
    polled continuously. ``active_llm`` is now the last observed snapshot; ``/api/llm/status``
    is the route that probes, and the SPA asks for it separately.
    """
    try:
        db.query(Company).count()
        from backend.shutdown import is_shutting_down

        return {
            "status": "shutting_down" if is_shutting_down() else "ok",
            "database": "connected",
            "active_llm": cached_active_llm_provider(),
            "shutting_down": is_shutting_down(),
        }
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Database connectivity failed: {str(e)}",
        )


# -------------------------------------------------------------
# Gmail API (BYOK OAuth) — opt-in, privacy-scoped
# -------------------------------------------------------------
from backend import gmail_client


class GmailCredentialsIn(BaseModel):
    client_id: Optional[str] = None
    client_secret: Optional[str] = None


class GmailSyncResponse(BaseModel):
    status: str
    emails_scanned: int = 0
    status_updates: int = 0
    opportunities_added: int = 0
    message: str


@app.get("/api/gmail/status")
def gmail_status():
    """Masked Gmail connection status (never returns secrets)."""
    return gmail_client.get_status()


@app.post("/api/gmail/credentials")
def gmail_save_credentials(payload: GmailCredentialsIn):
    """Stores the user's BYOK Google OAuth client id/secret (encrypted at rest)."""
    client_id = (payload.client_id or "").strip()
    client_secret = (payload.client_secret or "").strip()
    # Guard against pasting the client ID into the secret field (a common, silent mistake
    # that later surfaces as OAuth `invalid_client`).
    if client_secret and (
        client_secret == client_id
        or client_secret.endswith(".apps.googleusercontent.com")
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "That looks like the Client ID, not the Client Secret. Copy the secret from "
                "Google Cloud → Credentials (it starts with 'GOCSPX-')."
            ),
        )
    save_gmail_settings(
        client_id=client_id or None,
        client_secret=client_secret or None,
    )
    return gmail_client.get_status()


@app.get("/api/gmail/connect")
def gmail_connect():
    """Returns the Google consent URL for the loopback OAuth flow."""
    s = get_gmail_settings()
    if not (s["client_id"] and s["client_secret"]):
        raise HTTPException(
            status_code=400, detail="Add your Google OAuth client ID and secret first."
        )
    try:
        result = gmail_client.build_auth_url(s["client_id"], s["client_secret"])
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Could not build Google auth URL: {e}"
        )
    # Persist the PKCE verifier and the OAuth nonce: the callback needs the first to finish the
    # exchange and the second to prove the redirect answers a Connect this app started.
    save_gmail_settings(
        code_verifier=result.get("code_verifier", ""),
        oauth_state=result.get("state", ""),
    )
    return {"auth_url": result["auth_url"]}


@app.get("/api/gmail/oauth2callback")
def gmail_oauth2callback(
    code: Optional[str] = None,
    error: Optional[str] = None,
    state: Optional[str] = None,
):
    """Google loopback redirect target: validates ``state``, exchanges the code, stores the token.

    The route is public (it is the OAuth redirect), so ``state`` is the only thing that ties a
    redirect to a Connect the operator started. Without it any cross-site navigation carrying a
    ``code`` could force-connect a mailbox.
    """
    settings = get_gmail_settings()
    expected_state = (settings.get("oauth_state") or "").strip()
    if not expected_state or not state or not secrets.compare_digest(
        state, expected_state
    ):
        logger.warning("Gmail OAuth callback rejected: state did not match a pending Connect.")
        return HTMLResponse(
            "<h3>OAuth state mismatch.</h3>"
            "<p>Start the connection from the app's Gmail panel and try again.</p>",
            status_code=400,
        )
    # Single use: consume the nonce before the exchange, so a replayed redirect cannot reuse it.
    save_gmail_settings(oauth_state="")

    if error:
        return HTMLResponse(
            f"<h3>Gmail connection failed:</h3><p>{error}</p>", status_code=400
        )
    if not code:
        return HTMLResponse("<h3>Missing authorization code.</h3>", status_code=400)
    s = get_gmail_settings()
    try:
        tokens = gmail_client.exchange_code_for_tokens(
            code,
            s["client_id"],
            s["client_secret"],
            code_verifier=s.get("code_verifier", ""),
        )
    except Exception as e:
        return HTMLResponse(
            f"<h3>Token exchange failed:</h3><p>{e}</p>", status_code=400
        )
    if not tokens.get("refresh_token"):
        return HTMLResponse(
            "<h3>No refresh token returned.</h3><p>Re-run Connect and approve offline access.</p>",
            status_code=400,
        )
    save_gmail_settings(
        refresh_token=tokens["refresh_token"],
        token_email=tokens.get("email", ""),
        connected_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        code_verifier="",
        # A fresh token clears the previous failure diagnostics.
        token_expired_at="",
        last_error="",
    )
    return HTMLResponse(
        "<h3>✅ Gmail connected.</h3><p>You can close this tab and return to Job Alert Agent.</p>"
    )


@app.post("/api/gmail/disconnect")
def gmail_disconnect():
    """Clears stored Gmail credentials/token."""
    clear_gmail_settings()
    return {"status": "disconnected"}


def _execute_gmail_sync(db: Session, progress_cb: Optional[Callable] = None) -> Dict[str, Any]:
    """Fetch → classify → apply one Gmail run, persisting an honest OperationLog.

    Shared by the synchronous endpoint and the background task runner so both paths record
    the same counts and the same status vocabulary (issue #84). Deterministic: no LLM.
    """
    def _step(step: int, label: str) -> None:
        if progress_cb:
            progress_cb(step, label)

    outcomes: List[Dict[str, Any]] = []
    stats: Dict[str, Any] = {}
    events: List[Dict[str, Any]] = []
    updates = 0
    opportunities = 0
    error: Optional[str] = None

    _step(1, "Fetching mail")
    try:
        events = gmail_client.run_gmail_sync(db, stats=stats)
        _step(2, "Classifying applications")
        updates = _apply_email_events_to_db(db, events)
        _step(3, "Recording opportunities")
        opportunities = _ingest_email_opportunities_to_db(db, events)
    except Exception as exc:  # noqa: BLE001 - the failure is the record we must keep
        error = str(exc)
        logger.error(f"Gmail sync failed: {exc}")
        # A failed run must not burn dedup marks on the messages it did process.
        db.rollback()

    messages_matched = int(stats.get("messages_matched") or 0)
    if error:
        channel_status.record(outcomes, "gmail", channel_status.ERROR, error)
    elif not events:
        # The API path ran but classified nothing: "nothing to find" is not a success signal
        # when the operator asked for a sync and got no outcome at all.
        channel_status.record(
            outcomes, "gmail", channel_status.DEGRADED_ZERO,
            f"{messages_matched} message(s) matched, none classified",
        )
    else:
        channel_status.record(
            outcomes, "gmail", channel_status.OK, f"{len(events)} event(s) classified"
        )

    status = channel_status.aggregate_status(outcomes)
    pending = (
        db.query(PendingEmailApplication)
        .filter(PendingEmailApplication.state == "pending")
        .count()
    )
    summary = (
        f"Gmail sync: {messages_matched} messages matched, {len(events)} events, "
        f"{updates} status updates, {opportunities} opportunities added."
    )
    note = channel_status.describe(outcomes)
    if note:
        summary = f"{summary} Degraded — {note}"
    db.add(
        OperationLog(
            operation_type="gmail_sync",
            status=status,
            summary=summary,
            details_json=sanitize_log_details(
                {
                    "emails_scanned": messages_matched,
                    "status_messages": int(stats.get("status_messages_matched") or 0),
                    "alert_messages": int(stats.get("alert_messages_matched") or 0),
                    "emails_new": int(stats.get("new_processed") or 0),
                    "events": len(events),
                    "status_updates": updates,
                    "opportunities_added": opportunities,
                    "pending_email_applications": pending,
                    "channels": outcomes,
                }
            ),
            jobs_count=opportunities,
            prompt_tokens=0,
            completion_tokens=0,
        )
    )
    db.commit()
    _step(4, "Done")
    return {
        "status": status,
        "emails_scanned": messages_matched,
        "status_updates": updates,
        "opportunities_added": opportunities,
        "pending_email_applications": pending,
        "error": error,
        "message": summary,
    }


@app.post("/api/gmail/sync", response_model=GmailSyncResponse)
def gmail_sync_now(db: Session = Depends(get_db)):
    """Runs the opt-in, narrowly-scoped Gmail API sync (synchronous, for API/CLI callers)."""
    if not is_feature_enabled(db, "gmail_sync"):
        return GmailSyncResponse(
            status="disabled",
            message="Enable 'Gmail sync' in Settings → Opt-in Discovery Features.",
        )
    if not is_gmail_configured():
        return GmailSyncResponse(
            status="not_connected", message="Connect Gmail in Settings first."
        )
    result = _execute_gmail_sync(db)
    if result["error"]:
        raise HTTPException(status_code=502, detail=f"Gmail sync failed: {result['error']}")
    return GmailSyncResponse(
        status="ok",
        emails_scanned=result["emails_scanned"],
        status_updates=result["status_updates"],
        opportunities_added=result["opportunities_added"],
        message=result["message"],
    )


# -------------------------------------------------------------
# Company Endpoints
# -------------------------------------------------------------
@app.get("/api/companies/catalog")
def get_companies_catalog(db: Session = Depends(get_db)):
    """
    Returns the pre-seeded trending organizations catalog,
    annotating whether each company is currently tracked.
    """
    tracked_names = {c.name.lower() for c in db.query(Company.name).all()}
    tracked_domains = {
        c.domain.lower()
        for c in db.query(Company.domain).filter(Company.domain != None).all()
    }

    catalog = load_company_catalog()
    results = []
    for item in catalog:
        is_tracked = (item["name"].lower() in tracked_names) or (
            item["domain"].lower() in tracked_domains
        )
        results.append({**item, "is_tracked": is_tracked})
    return results


@app.post("/api/companies/seed")
def seed_trending_companies(db: Session = Depends(get_db)):
    """
    Seeds trending tech and AI organizations from the pre-built catalog into the database.
    """
    catalog = load_company_catalog()
    seeded = []
    for item in catalog:
        existing = (
            db.query(Company)
            .filter(
                (Company.name.ilike(item["name"].strip()))
                | (Company.domain.ilike(item["domain"].strip()))
            )
            .first()
        )

        if not existing:
            comp = Company(
                name=item["name"],
                domain=item["domain"],
                careers_url=item["careers_url"],
                description=item["description"],
                recent_news=item["recent_news"],
                salary_insights=item["salary_insights"],
                hiring_process=item["hiring_process"],
                market_position=item.get("market_position"),
                talking_points=item.get("talking_points"),
            )
            db.add(comp)
            seeded.append(item["name"])

    db.commit()
    return {
        "message": f"Successfully seeded {len(seeded)} trending organizations.",
        "seeded": seeded,
    }


@app.get("/api/companies", response_model=List[CompanyOut])
def list_companies(db: Session = Depends(get_db)):
    """Lists all configured target companies."""
    return db.query(Company).order_by(Company.name.asc()).all()


@app.post(
    "/api/companies", response_model=CompanyOut, status_code=status.HTTP_201_CREATED
)
def create_company(payload: CompanyCreate, db: Session = Depends(get_db)):
    """
    Adds a new company to the tracking list.
    Auto-populates domain, careers portal, and intelligence from catalog if available.
    """
    name_clean = payload.name.strip()
    existing = db.query(Company).filter(Company.name.ilike(name_clean)).first()
    if existing:
        raise HTTPException(
            status_code=400, detail="A company with this name is already being tracked."
        )

    # 1. Check if matching entry exists in pre-seeded catalog
    catalog_match = get_catalog_company(name_clean)

    domain = (
        payload.domain.strip()
        if payload.domain
        else (catalog_match["domain"] if catalog_match else None)
    )
    careers_url = (
        payload.careers_url.strip()
        if payload.careers_url
        else (catalog_match["careers_url"] if catalog_match else None)
    )
    description = payload.description or (
        catalog_match["description"] if catalog_match else None
    )
    recent_news = catalog_match["recent_news"] if catalog_match else None
    salary_insights = catalog_match["salary_insights"] if catalog_match else None
    hiring_process = catalog_match["hiring_process"] if catalog_match else None
    market_position = payload.market_position or (
        catalog_match.get("market_position") if catalog_match else None
    )
    talking_points = payload.talking_points or (
        catalog_match.get("talking_points") if catalog_match else None
    )

    # Fallback auto-discovery if still missing
    if not domain:
        import re

        slug = re.sub(r"[^a-zA-Z0-9]", "", name_clean).lower()
        domain = f"{slug}.com"
    if not careers_url and domain:
        careers_url = f"https://www.{domain}/careers"

    company = Company(
        name=name_clean,
        domain=domain,
        careers_url=careers_url,
        description=description,
        recent_news=recent_news,
        salary_insights=salary_insights,
        hiring_process=hiring_process,
        market_position=market_position,
        talking_points=talking_points,
    )
    db.add(company)
    db.commit()
    db.refresh(company)
    return company


@app.post("/api/companies/import")
async def import_companies_file(
    file: UploadFile = File(...), db: Session = Depends(get_db)
):
    """
    Imports target companies from an uploaded CSV, JSON, Markdown (.md), or plain text file.
    Auto-populates verified domain, careers portal, and intelligence from the catalog when available.
    """
    content_bytes = await file.read()
    try:
        content_str = content_bytes.decode("utf-8")
    except UnicodeDecodeError:
        content_str = content_bytes.decode("latin-1", errors="ignore")

    parsed_entries = parse_company_import(content_str, filename=file.filename)
    if not parsed_entries:
        raise HTTPException(
            status_code=400,
            detail="No valid company entries could be extracted from the file.",
        )

    added_companies = []
    updated_companies = []

    for entry in parsed_entries:
        raw_name = entry.get("name", "").strip()
        if not raw_name:
            continue

        existing = (
            db.query(Company)
            .filter(
                (Company.name.ilike(raw_name))
                | (
                    (Company.domain != None)
                    & (Company.domain.ilike(entry.get("domain", "---").strip()))
                )
            )
            .first()
        )

        catalog_match = get_catalog_company(raw_name)

        domain = entry.get("domain") or (
            catalog_match["domain"] if catalog_match else None
        )
        careers_url = entry.get("careers_url") or (
            catalog_match["careers_url"] if catalog_match else None
        )
        description = entry.get("description") or (
            catalog_match["description"] if catalog_match else None
        )
        recent_news = catalog_match["recent_news"] if catalog_match else None
        salary_insights = catalog_match["salary_insights"] if catalog_match else None
        hiring_process = catalog_match["hiring_process"] if catalog_match else None
        market_position = entry.get("market_position") or (
            catalog_match.get("market_position") if catalog_match else None
        )
        talking_points = entry.get("talking_points") or (
            catalog_match.get("talking_points") if catalog_match else None
        )

        if not domain:
            import re

            slug = re.sub(r"[^a-zA-Z0-9]", "", raw_name).lower()
            domain = f"{slug}.com"
        if not careers_url and domain:
            careers_url = f"https://www.{domain}/careers"

        if existing:
            # Update missing attributes
            if not existing.domain and domain:
                existing.domain = domain
            if not existing.careers_url and careers_url:
                existing.careers_url = careers_url
            if not existing.description and description:
                existing.description = description
            updated_companies.append(existing.name)
        else:
            comp = Company(
                name=raw_name,
                domain=domain,
                careers_url=careers_url,
                description=description,
                recent_news=recent_news,
                salary_insights=salary_insights,
                hiring_process=hiring_process,
                market_position=market_position,
                talking_points=talking_points,
            )
            db.add(comp)
            added_companies.append(raw_name)

    db.commit()
    return {
        "message": f"Successfully imported {len(added_companies)} new company(ies) and updated {len(updated_companies)} existing.",
        "added_count": len(added_companies),
        "updated_count": len(updated_companies),
        "added": added_companies,
    }


@app.get("/api/companies/{company_id}", response_model=CompanyOut)
def get_company(company_id: int, db: Session = Depends(get_db)):
    """Retrieves a single company's details."""
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found.")
    return company


@app.put("/api/companies/{company_id}", response_model=CompanyOut)
def update_company(
    company_id: int, payload: CompanyUpdate, db: Session = Depends(get_db)
):
    """Updates careers page, domain, description, or intelligence for a target company."""
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found.")

    if payload.name is not None:
        company.name = payload.name.strip()
    if payload.domain is not None:
        company.domain = payload.domain.strip() if payload.domain.strip() else None
    if payload.careers_url is not None:
        company.careers_url = (
            payload.careers_url.strip() if payload.careers_url.strip() else None
        )
    if payload.description is not None:
        company.description = payload.description.strip()
    if payload.recent_news is not None:
        company.recent_news = payload.recent_news.strip()
    if payload.salary_insights is not None:
        company.salary_insights = payload.salary_insights.strip()
    if payload.hiring_process is not None:
        company.hiring_process = payload.hiring_process.strip()
    if payload.market_position is not None:
        company.market_position = payload.market_position.strip()
    if payload.talking_points is not None:
        company.talking_points = payload.talking_points.strip()

    db.commit()
    db.refresh(company)
    return company


@app.post("/api/companies/{company_id}/refresh-intel", response_model=CompanyOut)
def refresh_company_intel(company_id: int, db: Session = Depends(get_db)):
    """Fetches fresh web news, salary estimates, and interview processes for the company."""
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found.")

    intel = gather_company_intelligence(company.name)
    if intel.get("description"):
        company.description = intel["description"]
    if intel.get("recent_news"):
        company.recent_news = intel["recent_news"]
    if intel.get("salary_insights"):
        company.salary_insights = intel["salary_insights"]
    if intel.get("hiring_process"):
        company.hiring_process = intel["hiring_process"]
    if intel.get("market_position"):
        company.market_position = intel["market_position"]
    if intel.get("talking_points"):
        company.talking_points = intel["talking_points"]

    db.commit()
    db.refresh(company)
    return company


@app.delete("/api/companies/{company_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_company(company_id: int, db: Session = Depends(get_db)):
    """Deletes a company and all its associated jobs."""
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found.")
    db.delete(company)
    db.commit()
    return None


# -------------------------------------------------------------
# Job Endpoints
# -------------------------------------------------------------
@app.get("/api/jobs", response_model=List[JobOut])
def list_jobs(
    status_filter: Optional[str] = Query(None, alias="status"),
    in_progress_only: bool = Query(False),
    company_id: Optional[int] = None,
    min_score: Optional[float] = None,
    title_query: Optional[str] = None,
    location_query: Optional[str] = None,
    work_mode: Optional[str] = None,
    source_type: Optional[str] = None,
    sync_batch: Optional[str] = None,
    new_from_sync: bool = Query(False),
    limit: int = Query(1000, ge=1, le=5000),
    db: Session = Depends(get_db),
):
    """
    Lists tracked jobs with optional filters for status, in-progress state, company, score, title, location, work mode,
    source type (LinkedIn / Direct / Email), a specific sync batch, or "new from sync" review items.

    Bounded by ``limit``: the ORDER BY is on the indexed ``created_at`` now, but an unbounded
    ``.all()`` still serialises every row the table has ever held.
    """
    query = db.query(Job)

    if in_progress_only:
        query = query.filter(
            Job.status.in_(
                [
                    "To Apply",
                    "Shortlisted",
                    "Applied",
                    "Screening",
                    "Interview",
                    "Offered",
                ]
            )
        )
    elif status_filter:
        query = query.filter(Job.status == status_filter)

    if company_id:
        query = query.filter(Job.company_id == company_id)

    if min_score is not None:
        query = query.filter(Job.match_score >= min_score)

    if source_type and source_type.lower() != "all":
        st = source_type.strip()
        if st.lower() == "linkedin":
            query = query.filter(Job.source_type == "LinkedIn")
        elif st.lower() == "google":
            query = query.filter(Job.source_type == "Google")
        elif st.lower() == "email":
            query = query.filter(Job.source_type == "Email")
        elif st.lower() == "hirist":
            query = query.filter(Job.source_type == "Hirist")
        elif st.lower() == "direct":
            query = query.filter(Job.source_type == "Direct")

    if sync_batch and sync_batch.strip():
        query = query.filter(Job.sync_batch_id == sync_batch.strip())

    if new_from_sync:
        query = query.filter(Job.sync_batch_id.isnot(None))

    if title_query and title_query.strip():
        terms = [t.strip() for t in title_query.split(",") if t.strip()]
        if terms:
            from sqlalchemy import or_

            query = query.filter(or_(*[Job.title.ilike(f"%{term}%") for term in terms]))

    if location_query and location_query.strip():
        terms = [t.strip() for t in location_query.split(",") if t.strip()]
        if terms:
            from sqlalchemy import or_

            query = query.filter(
                or_(*[Job.location.ilike(f"%{term}%") for term in terms])
            )

    if work_mode and work_mode.strip() and work_mode.lower() != "all":
        wm = work_mode.strip().lower()
        if wm == "remote":
            query = query.filter(Job.location.ilike("%remote%"))
        elif wm == "hybrid":
            query = query.filter(Job.location.ilike("%hybrid%"))
        elif wm == "onsite":
            query = query.filter(~Job.location.ilike("%remote%"))

    jobs = query.order_by(Job.created_at.desc()).limit(limit).all()

    # Attach company_name helper
    prov_ctx = _resume_provenance_context(db)
    results = []
    for j in jobs:
        j_out = JobOut.model_validate(j)
        j_out.company_name = j.company.name if j.company else None
        results.append(_apply_score_provenance(j_out, j, prov_ctx))
    return results


# -------------------------------------------------------------
# Opportunity Radar (structured hiring-intent feed)
# -------------------------------------------------------------
# Ranks already-ingested, structured opportunities (LinkedIn alerts/recommendations,
# ATS portals, Google Jobs, recruiter outreach) by deterministic hiring-intent signals.
# Deliberately does NOT call an LLM, per the token-optimization guardrails.
HIRING_INTENT_SOURCE_WEIGHTS = {
    "gmail recruiter outreach": 40,
    "recruiter outreach": 40,
    "linkedin job alert": 28,
    "linkedin saved job": 24,
    "linkedin recommendations": 24,
    "linkedin recommendation": 24,
    "linkedin": 20,
    "greenhouse portal": 18,
    "lever portal": 18,
    "ashby portal": 18,
    "workday portal": 18,
    "smartrecruiters portal": 18,
    "workable portal": 16,
    "greenhouse": 16,
    "lever": 16,
    "ashby": 16,
    "smartrecruiters": 16,
    "workable": 14,
    "google jobs": 8,
}

HIRING_INTENT_KEYWORDS = (
    "we're hiring",
    "we are hiring",
    "join my team",
    "join our team",
    "my team is hiring",
    "team is hiring",
    "now hiring",
    "hiring for",
    "open role",
    "open position",
    "looking for",
    "immediate joiners",
    "urgent",
    "headcount",
    "growing team",
)

RADAR_ACTIVE_STATUSES = ("To Apply", "Shortlisted", "Saved")
RADAR_APPLIED_STATUSES = ("Applied", "Screening", "Interview", "Offered")
RADAR_EXCLUDED_STATUSES = ("Rejected", "Closed", "Withdrawn", "Not Interested", "Ignored")


class OpportunityRadarItem(BaseModel):
    id: int
    company_id: int
    company_name: Optional[str] = None
    title: str
    description: Optional[str] = None
    location: Optional[str] = None
    url: Optional[str] = None
    salary_range: Optional[str] = None
    source: Optional[str] = None
    source_type: Optional[str] = None
    status: str
    match_score: Optional[float] = None
    match_analysis: Optional[str] = None
    cover_letter_draft: Optional[str] = None
    tailored_resume_points: Optional[str] = None
    cold_message_draft: Optional[str] = None
    is_ghost_job: bool = False
    repost_count: int = 0
    created_at: Optional[datetime.datetime] = None
    has_materials: bool = False
    intent_score: int = 0
    intent_reasons: List[str] = []



def compute_hiring_intent(job, now: Optional[datetime.datetime] = None):
    """
    Deterministic hiring-intent score (0-100ish) for a structured job listing.
    Returns (score, reasons). Pure function so it is cheap to unit test.
    """
    now = now or datetime.datetime.now(datetime.timezone.utc)
    score = 0
    reasons: List[str] = []

    source_lower = (job.source or "").strip().lower()
    if source_lower:
        for needle, weight in HIRING_INTENT_SOURCE_WEIGHTS.items():
            if needle in source_lower:
                score += weight
                reasons.append(f"Source: {job.source}")
                break

    created = getattr(job, "created_at", None)
    if created is not None:
        if created.tzinfo is None:
            created = created.replace(tzinfo=datetime.timezone.utc)
        age_days = (now - created).total_seconds() / 86400
        if age_days <= 7:
            score += 15
            reasons.append("Fresh (≤7d)")
        elif age_days <= 14:
            score += 8
            reasons.append("Recent (≤14d)")
        elif age_days <= 30:
            score += 3
            reasons.append("This month")

    text = f"{job.title or ''} {(job.description or '')[:600]}".lower()
    matched = [k for k in HIRING_INTENT_KEYWORDS if k in text]
    if matched:
        score += min(len(matched) * 5, 15)
        reasons.append("Hiring language: " + ", ".join(matched[:3]))

    if job.match_score:
        score += int(job.match_score * 0.2)
        reasons.append(f"Match {job.match_score:.0f}%")

    if job.is_ghost_job:
        score -= 20
        reasons.append("⚠️ Possible ghost job")

    return max(score, 0), reasons


def radar_location_matches(
    job_location: str, loc_filters: List[str], city_filters: List[str], work_mode: str
) -> bool:
    """
    City-precise location gate for the radar.

    Unlike the feed/prune matcher (which treats any listing in the target country as a
    match), the radar requires an explicit selected-city hit when cities are configured,
    while still allowing remote postings when the work mode permits them.
    """
    jl = (job_location or "").lower()
    if city_filters:
        if any(cf in jl for cf in city_filters):
            return True
        is_remote = any(
            tok in jl
            for tok in ("remote", "anywhere", "worldwide", "global", "distributed")
        )
        if not is_remote or work_mode == "onsite":
            return False
        # Remote listings must not be restricted to a foreign country relative to the
        # regions implied by the selected cities (e.g. "Remote, US" for India/UK/SG targets).
        from backend.geo import countries_for_terms, countries_for_cities

        target_iso = countries_for_terms(loc_filters) | countries_for_cities(
            city_filters
        )
        if target_iso:
            if detect_foreign_restriction(jl, list(target_iso)):
                return False
        return True
    return check_location_match(job_location or "", loc_filters, city_filters)


class SkillGapInsight(BaseModel):
    skill: str
    count: int
    share: float
    sample_job_ids: List[int] = []


class SkillsInsightsOut(BaseModel):
    total_jobs: int
    total_scored: int
    rows_with_gaps: int
    gaps: List[SkillGapInsight] = []


_INSIGHTS_SCAN_CAP = 2000


@app.get("/api/insights/skills", response_model=SkillsInsightsOut)
def get_skills_insights(
    limit: int = Query(8, ge=1, le=30),
    db: Session = Depends(get_db),
):
    """Aggregates persisted per-job Resume Gap lists into ranked skill-demand metrics.

    Deterministic only (no LLM): counts each skill's frequency across the structured
    ``match_gaps_json`` payloads that scoring persisted, so the user can see which missing
    skills block the most opportunities. Legacy rows without a gap payload are counted in
    the totals but contribute no per-skill data.
    """
    rows = (
        db.query(Job.id, Job.match_gaps_json)
        .filter(Job.match_scored == True)
        .order_by(Job.created_at.desc())
        .limit(_INSIGHTS_SCAN_CAP)
        .all()
    )
    total_scored = len(rows)
    counts: Dict[str, int] = {}
    samples: Dict[str, List[int]] = {}
    rows_with_gaps = 0
    for job_id, payload in rows:
        if not payload:
            continue
        try:
            gaps = json.loads(payload)
        except Exception:
            continue
        if not isinstance(gaps, list) or not gaps:
            continue
        rows_with_gaps += 1
        for raw in gaps:
            skill = str(raw).strip()
            if not skill:
                continue
            counts[skill] = counts.get(skill, 0) + 1
            bucket = samples.setdefault(skill, [])
            if len(bucket) < 5 and job_id not in bucket:
                bucket.append(job_id)
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
    denom = rows_with_gaps or 1
    return SkillsInsightsOut(
        total_jobs=db.query(Job).count(),
        total_scored=total_scored,
        rows_with_gaps=rows_with_gaps,
        gaps=[
            SkillGapInsight(
                skill=skill,
                count=count,
                share=round(count / denom, 3),
                sample_job_ids=samples.get(skill, []),
            )
            for skill, count in ranked
        ],
    )


@app.get("/api/opportunities/radar", response_model=List[OpportunityRadarItem])
def get_opportunity_radar(
    company_id: Optional[int] = None,
    min_intent: float = Query(0),
    include_applied: bool = Query(False),
    respect_preferences: bool = Query(True),
    limit: int = Query(30, ge=1, le=200),
    db: Session = Depends(get_db),
):
    """
    Returns ranked, structured hiring-intent opportunities from already-ingested jobs.

    When ``respect_preferences`` is true (default) results are limited to the persisted
    discovery preferences (target roles, country/cities, and work mode) exactly like the
    active-feed criteria filter, so the radar never shows roles/locations the user didn't ask for.
    """
    statuses = list(RADAR_ACTIVE_STATUSES)
    if include_applied:
        statuses += list(RADAR_APPLIED_STATUSES)

    # Persisted discovery preferences (server-side source of truth).
    pref = get_user_preferences(db)
    title_filters = [
        t.strip().lower() for t in (pref.target_titles or "").split(",") if t.strip()
    ]
    loc_filters = [
        l.strip().lower()
        for l in (pref.target_locations or pref.target_country or "").split(",")
        if l.strip() and l.strip().lower() != "all"
    ]
    city_filters = [
        c.strip().lower() for c in (pref.target_cities or "").split(",") if c.strip()
    ]
    wm_filter = (pref.work_mode or "").strip().lower()
    has_pref_filters = bool(
        title_filters
        or loc_filters
        or city_filters
        or (wm_filter and wm_filter != "all")
    )

    query = db.query(Job).filter(Job.status.in_(statuses))
    if company_id:
        query = query.filter(Job.company_id == company_id)

    jobs = query.order_by(Job.created_at.desc()).limit(500).all()

    items: List[OpportunityRadarItem] = []
    for j in jobs:
        if respect_preferences and has_pref_filters:
            if not check_title_match(j.title or "", title_filters):
                continue
            if not radar_location_matches(
                j.location or "", loc_filters, city_filters, wm_filter
            ):
                continue
            if not check_workmode_match(j.title or "", j.location or "", wm_filter):
                continue

        score, reasons = compute_hiring_intent(j)
        if score < min_intent:
            continue
        items.append(
            OpportunityRadarItem(
                id=j.id,
                company_id=j.company_id,
                company_name=j.company.name if j.company else None,
                title=j.title,
                description=j.description,
                location=j.location,
                url=j.url,
                salary_range=j.salary_range,
                source=j.source,
                source_type=j.source_type,
                status=j.status,
                match_score=j.match_score,
                match_analysis=j.match_analysis,
                cover_letter_draft=j.cover_letter_draft,
                tailored_resume_points=j.tailored_resume_points,
                cold_message_draft=j.cold_message_draft,
                is_ghost_job=bool(j.is_ghost_job),
                repost_count=j.repost_count or 0,
                created_at=j.created_at,
                has_materials=bool(
                    j.cover_letter_draft
                    or j.cold_message_draft
                    or j.tailored_resume_points
                ),
                intent_score=score,
                intent_reasons=reasons,
            )
        )

    items.sort(key=lambda i: (i.intent_score, i.match_score or 0), reverse=True)
    return items[:limit]


@app.post("/api/jobs", response_model=JobOut, status_code=status.HTTP_201_CREATED)
def create_job(payload: JobCreate, db: Session = Depends(get_db)):
    """Manually creates or records a job opportunity."""
    company = db.query(Company).filter(Company.id == payload.company_id).first()
    if not company:
        raise HTTPException(
            status_code=404, detail="Referenced company does not exist."
        )

    job = Job(
        company_id=payload.company_id,
        title=payload.title.strip(),
        description=payload.description,
        url=payload.url,
        salary_range=payload.salary_range,
        location=payload.location,
        source=payload.source,
        source_type=payload.source_type,
        status=payload.status or "To Apply",
        match_score=payload.match_score,
        match_analysis=payload.match_analysis,
        is_ghost_job=payload.is_ghost_job or False,
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    # Log creation event
    event = ApplicationEvent(
        job_id=job.id,
        event_type="created",
        description=f"Job opportunity recorded: {job.title} at {company.name}",
    )
    db.add(event)
    db.commit()

    j_out = JobOut.model_validate(job)
    j_out.company_name = company.name
    return _apply_score_provenance(j_out, job, _resume_provenance_context(db))


@app.get("/api/jobs/{job_id}", response_model=JobOut)
def get_job(job_id: int, db: Session = Depends(get_db)):
    """Retrieves a single job's details."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    return _job_out(db, job)


@app.patch("/api/jobs/{job_id}/status", response_model=JobOut)
def update_job_status(
    job_id: int, payload: JobStatusUpdate, db: Session = Depends(get_db)
):
    """
    Updates the application lifecycle status and sets corresponding timestamps.
    Automatically logs an ApplicationEvent.
    """
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")

    old_status = job.status
    if payload.status == old_status:
        # The SPA's status <select> fires `change` even when the same option is re-picked, and a
        # no-op write appended a "Shortlisted -> Shortlisted" event to the dossier (job 238).
        # Nothing changed, so nothing is written.
        j_out = JobOut.model_validate(job)
        j_out.company_name = job.company.name if job.company else None
        return _apply_score_provenance(j_out, job, _resume_provenance_context(db))
    job.status = payload.status
    # The user acted on this job, so it's no longer a "new from sync" review item.
    job.sync_batch_id = None
    now = datetime.datetime.now(datetime.timezone.utc)

    # Update date tracking
    if payload.status == "Applied" and not job.applied_at:
        job.applied_at = now
    elif (
        payload.status in ["Screening", "Interview"] and not job.interview_scheduled_at
    ):
        job.interview_scheduled_at = now
    elif payload.status == "Rejected" and not job.rejected_at:
        job.rejected_at = now

    # Index into Negative Role Memory for zero-token ignore filtering
    if payload.status in ["Not Interested", "Ignored"]:
        index_dismissed_job_pattern(
            job_title=job.title,
            job_description=job.description or "",
            db=db,
            reason=f"User marked as {payload.status}",
            company_name=job.company.name if job.company else None,
        )

    # Add timeline event
    desc = f"Status updated from '{old_status}' to '{payload.status}'"
    if payload.note:
        desc += f". Note: {payload.note}"

    event = ApplicationEvent(
        job_id=job.id, event_type="status_change", description=desc, timestamp=now
    )
    db.add(event)
    db.commit()
    db.refresh(job)

    j_out = JobOut.model_validate(job)
    j_out.company_name = job.company.name if job.company else None
    return _apply_score_provenance(j_out, job, _resume_provenance_context(db))


@app.delete("/api/jobs/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_job(job_id: int, db: Session = Depends(get_db)):
    """Deletes a job opportunity."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    db.delete(job)
    db.commit()
    return None


# -------------------------------------------------------------
# Application Events Endpoints
# -------------------------------------------------------------
@app.get("/api/jobs/{job_id}/events", response_model=List[ApplicationEventOut])
def get_job_events(job_id: int, db: Session = Depends(get_db)):
    """Retrieves timeline history / audit log for a job."""
    return (
        db.query(ApplicationEvent)
        .filter(ApplicationEvent.job_id == job_id)
        .order_by(ApplicationEvent.timestamp.desc())
        .all()
    )


@app.post(
    "/api/jobs/{job_id}/events",
    response_model=ApplicationEventOut,
    status_code=status.HTTP_201_CREATED,
)
def add_job_event(
    job_id: int, payload: ApplicationEventCreate, db: Session = Depends(get_db)
):
    """Manually adds a custom event / note to a job timeline."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    event = ApplicationEvent(
        job_id=job_id, event_type=payload.event_type, description=payload.description
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    return event


@app.get("/api/jobs/{job_id}/skills-alignment", response_model=SkillsAlignmentResponse)
def get_job_skills_alignment(job_id: int, db: Session = Depends(get_db)):
    """Computes real-time tech stack compatibility and requirement alignment between candidate resume and job."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")

    candidate_skills = []
    active_resume = (
        db.query(Resume)
        .filter(Resume.is_active == True)
        .order_by(Resume.created_at.desc())
        .first()
    )
    if active_resume and active_resume.parsed_json_encrypted:
        try:
            pj = json.loads(decrypt_data(active_resume.parsed_json_encrypted))
            candidate_skills = pj.get("skills", [])
        except Exception:
            pass

    job_text = f"{job.title} {job.description or ''}"
    alignment = extract_job_skills_and_alignment(job_text, candidate_skills)

    return SkillsAlignmentResponse(
        job_id=job.id,
        compatibility_pct=alignment["compatibility_pct"],
        total_stack_count=alignment["total_stack_count"],
        matched_count=alignment["matched_count"],
        gap_count=alignment["gap_count"],
        matched_skills=[SkillItem(**s) for s in alignment["matched_skills"]],
        gap_skills=[SkillItem(**s) for s in alignment["gap_skills"]],
        core_stack=alignment["core_stack"],
        confidence=alignment.get("confidence", "medium"),
        summary=alignment["summary"],
    )


@app.get("/api/jobs/{job_id}/hiring-team", response_model=JobHiringTeamResponse)
def get_job_hiring_team(job_id: int, db: Session = Depends(get_db)):
    """Use this endpoint to find the relevant people, i.e people marked
    as technical recruiter at the company."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")

    comp_name = job.company.name if job.company else "Company"
    contacts: List[HiringContact] = []

    if is_feature_enabled(db, "linkedin_sync") and has_consent(db, "linkedin_sync"):
        _log_linkedin_automation_warning()
        with sync_playwright() as p:
            contacts = search_linkedin_talent_partners(p, comp_name, job.title, True)

    # DDG-based talent-partner discovery (no logged-in browser session required).
    if not contacts:
        for partner in find_talent_partners(comp_name, job.title):
            contacts.append(
                HiringContact(
                    author_name=partner["name"],
                    author_headline=partner["headline"],
                    author_profile_url=partner["profile_url"],
                    source="Talent partner search",
                )
            )

    # Provide a general talent search link for the company
    if job.company:
        contacts.append(
            HiringContact(
                author_name=f"Search LinkedIn for recruiters at {comp_name}",
                author_headline=f"Recruiting & Technical Hiring Team at {comp_name}",
                author_profile_url=f"https://www.linkedin.com/search/results/people/?keywords={urllib.parse.quote(comp_name + ' technical recruiter')}",
                source="LinkedIn talent search link",
            )
        )

    return JobHiringTeamResponse(
        job_id=job.id,
        company_name=comp_name,
        contacts_count=len(contacts),
        contacts=contacts,
    )


# -------------------------------------------------------------
# Resume Endpoints (Encrypted Storage & Document Attachment)
# -------------------------------------------------------------


@app.post(
    "/api/resume/upload", response_model=ResumeOut, status_code=status.HTTP_201_CREATED
)
async def upload_resume(file: UploadFile = File(...), db: Session = Depends(get_db)):
    """
    Extracts text from an uploaded resume (PDF/Text/Markdown), encrypts it,
    and stores it securely in the database. Also persists active resume document for auto-attachment.
    """
    ALLOWED_RESUME_EXTENSIONS = {"pdf", "txt", "md"}
    safe_filename = Path(file.filename or "resume.pdf").name
    ext = safe_filename.rsplit(".", 1)[-1].lower() if "." in safe_filename else ""

    if ext not in ALLOWED_RESUME_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file extension '.{ext}'. Allowed extensions are: {', '.join(sorted(ALLOWED_RESUME_EXTENSIONS))}",
        )

    file_bytes = await file.read()
    if not file_bytes:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")

    try:
        raw_text = parse_resume_document(file_bytes, safe_filename)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to extract text: {str(e)}")

    if not raw_text.strip():
        raise HTTPException(
            status_code=400,
            detail="No readable text could be extracted from this resume.",
        )

    # Save active document to data folder for automated attachment
    resumes_dir = BASE_DIR / "data" / "resumes"
    resumes_dir.mkdir(parents=True, exist_ok=True)
    active_resume_file = resumes_dir / f"active_resume.{ext}"
    try:
        with open(active_resume_file, "wb") as f:
            f.write(file_bytes)
    except Exception as e:
        logger.warning(f"Failed to persist resume document to disk: {e}")

    # Encrypt raw content before persisting
    encrypted_content = encrypt_data(raw_text)

    # Parse ATS structure via LLM
    parsed_json = parse_resume_to_json(raw_text)
    encrypted_parsed_json = (
        encrypt_data(json.dumps(parsed_json)) if parsed_json else None
    )

    # Deactivate older resumes
    db.query(Resume).filter(Resume.is_active == True).update({"is_active": False})

    new_resume = Resume(
        filename=safe_filename,
        content_encrypted=encrypted_content,
        parsed_json_encrypted=encrypted_parsed_json,
        parser_version=RESUME_PARSER_VERSION,
        fingerprint=resume_fingerprint(raw_text),
        is_active=True,
    )
    db.add(new_resume)
    db.commit()
    db.refresh(new_resume)

    try:
        index_resume_chunks(db, new_resume)
    except Exception as idx_err:
        logger.debug(f"Resume chunk indexing skipped: {idx_err}")

    return ResumeOut(
        id=new_resume.id,
        filename=new_resume.filename,
        is_active=new_resume.is_active,
        created_at=new_resume.created_at,
        raw_preview=truncate_preview(raw_text),
        parsed_json=parsed_json,
        parser_version=new_resume.parser_version,
        parse_stale=(new_resume.parser_version or 0) < RESUME_PARSER_VERSION,
    )


@app.get("/api/resume", response_model=ResumeOut)
def get_active_resume(db: Session = Depends(get_db)):
    """Retrieves the active encrypted resume, decrypts it, and returns it."""
    resume = (
        db.query(Resume)
        .filter(Resume.is_active == True)
        .order_by(Resume.created_at.desc())
        .first()
    )
    if not resume:
        # Fallback to the latest resume if none marked active
        resume = db.query(Resume).order_by(Resume.created_at.desc()).first()
        if not resume:
            raise HTTPException(
                status_code=404, detail="No active resume found. Please upload one."
            )
        resume.is_active = True
        db.commit()

    decrypted_content = decrypt_data(resume.content_encrypted)
    parsed_json = None
    if resume.parsed_json_encrypted:
        try:
            parsed_json = json.loads(decrypt_data(resume.parsed_json_encrypted))
        except Exception:
            pass

    return ResumeOut(
        id=resume.id,
        filename=resume.filename,
        is_active=resume.is_active,
        created_at=resume.created_at,
        raw_preview=decrypted_content,
        parsed_json=parsed_json,
        parser_version=resume.parser_version,
        parse_stale=(resume.parser_version or 0) < RESUME_PARSER_VERSION,
    )


@app.get("/api/candidate/profile")
def get_candidate_profile(db: Session = Depends(get_db)):
    """
    Returns a unified candidate profile extracted from the active resume and Q&A bank,
    formatted specifically for Chrome extension ATS 1-click autofill.
    """
    resume = (
        db.query(Resume)
        .filter(Resume.is_active == True)
        .order_by(Resume.created_at.desc())
        .first()
    )
    if not resume:
        resume = db.query(Resume).order_by(Resume.created_at.desc()).first()

    profile_data = {
        "fullName": "",
        "firstName": "",
        "lastName": "",
        "email": "",
        "phone": "",
        "location": "",
        "linkedinUrl": "",
        "githubUrl": "",
        "portfolioUrl": "",
        "resumeLink": "",
        "currentCompany": "",
        "currentTitle": "",
        "yearsExperience": "",
        "workAuthorization": "",
        "sponsorshipRequired": "",
        "noticePeriod": "",
        "resumeSummary": "",
        "skills": [],
        "experience": [],
        "education": [],
    }

    if resume:
        decrypted_raw = decrypt_data(resume.content_encrypted)
        parsed_json = {}
        if resume.parsed_json_encrypted:
            try:
                parsed_json = json.loads(decrypt_data(resume.parsed_json_encrypted))
            except Exception:
                pass

        name = parsed_json.get("name") or ""
        explicit_first = (
            parsed_json.get("firstName") or parsed_json.get("first_name") or ""
        )
        explicit_last = (
            parsed_json.get("lastName") or parsed_json.get("last_name") or ""
        )
        first_name, last_name = split_candidate_name(
            name, explicit_first=explicit_first, explicit_last=explicit_last
        )

        links = (
            parsed_json.get("links", {})
            if isinstance(parsed_json.get("links"), dict)
            else {}
        )
        exp_list = (
            parsed_json.get("experience") or parsed_json.get("work_history") or []
        )
        if not isinstance(exp_list, list):
            exp_list = []
        edu_list = parsed_json.get("education") or []
        if not isinstance(edu_list, list):
            edu_list = []
        latest_exp = exp_list[0] if exp_list else {}

        profile_data.update(
            {
                "fullName": name,
                "firstName": first_name,
                "lastName": last_name,
                "email": parsed_json.get("email") or "",
                "phone": parsed_json.get("phone") or "",
                "location": parsed_json.get("location") or "",
                "linkedinUrl": links.get("linkedin") or links.get("LinkedIn") or "",
                "githubUrl": links.get("github") or links.get("GitHub") or "",
                "portfolioUrl": links.get("portfolio") or links.get("website") or "",
                "resumeLink": links.get("resume")
                or links.get("drive")
                or links.get("linkedin")
                or "",
                "currentCompany": latest_exp.get("company", "")
                if isinstance(latest_exp, dict)
                else "",
                "currentTitle": latest_exp.get("title", "")
                if isinstance(latest_exp, dict)
                else "",
                "resumeSummary": parsed_json.get("summary") or decrypted_raw[:1000],
                "skills": parsed_json.get("skills") or [],
                "experience": exp_list,
                "education": edu_list,
            }
        )

    return profile_data


# -------------------------------------------------------------
# User Preferences & Opt-in Feature Flags
# -------------------------------------------------------------
class UserPreferenceIn(BaseModel):
    target_titles: Optional[str] = None
    target_locations: Optional[str] = None
    target_cities: Optional[str] = None
    work_mode: Optional[str] = None
    target_country: Optional[str] = None
    timezone: Optional[str] = None
    features: Optional[Dict[str, bool]] = None
    consents: Optional[Dict[str, Any]] = None
    scoring_config: Optional[Dict[str, float]] = None
    excluded_companies: Optional[str] = None
    skip_topics: Optional[str] = None
    extra_skills: Optional[List[str]] = None


class UserPreferenceOut(BaseModel):
    target_titles: str = ""
    target_locations: str = ""
    target_cities: str = ""
    work_mode: str = "all"
    target_country: str = ""
    timezone: str = ""
    features: Dict[str, bool] = {}
    available_features: Dict[str, bool] = {}
    consents: Dict[str, Any] = {}
    consent_version: int = CONSENT_VERSION
    consent_required_features: List[str] = []
    scoring_config: Dict[str, float] = {}
    similarity_thresholds: Dict[str, float] = {}
    country_hubs: Dict[str, Any] = {}
    excluded_companies: str = ""
    skip_topics: str = ""
    extra_skills: List[str] = []
    pending_email_applications: int = 0


def normalize_target_cities(raw: Optional[str]) -> str:
    """
    Drops the legacy global 'Remote' pseudo-city from a comma-separated city list.

    Remote eligibility is governed solely by ``work_mode`` (and the backend location
    matchers already treat a 'Remote' city token as redundant). Normalizing here keeps
    stale server rows / other clients from resurrecting the confusing global chip.
    """
    if not raw:
        return ""
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    return ", ".join(p for p in parts if p.lower() != "remote")


@app.get("/api/preferences", response_model=UserPreferenceOut)
def get_preferences(db: Session = Depends(get_db)):
    """Returns server-persisted candidate discovery preferences and opt-in feature flags."""
    pref = get_user_preferences(db)
    return UserPreferenceOut(
        target_titles=pref.target_titles or "",
        target_locations=pref.target_locations or "",
        target_cities=normalize_target_cities(pref.target_cities),
        work_mode=pref.work_mode or "all",
        target_country=pref.target_country or "",
        timezone=pref.timezone or "",
        features=get_feature_flags(db),
        available_features=DEFAULT_FEATURE_FLAGS,
        consents=get_consents(db),
        consent_version=CONSENT_VERSION,
        consent_required_features=list(CONSENT_REQUIRED_FEATURES),
        scoring_config=get_scoring_config(db),
        similarity_thresholds={
            "dismissal": DISMISSAL_SIMILARITY_THRESHOLD,
        },
        country_hubs=load_country_hubs(),
        excluded_companies=pref.excluded_companies
        if pref.excluded_companies is not None
        else DEFAULT_EXCLUDED_COMPANIES,
        skip_topics=pref.skip_topics or "",
        extra_skills=get_extra_skills(pref),
        pending_email_applications=(
            db.query(PendingEmailApplication)
            .filter(PendingEmailApplication.state == "pending")
            .count()
        ),
    )


@app.put("/api/preferences", response_model=UserPreferenceOut)
def update_preferences(payload: UserPreferenceIn, db: Session = Depends(get_db)):
    """Persists candidate discovery preferences and opt-in feature flags server-side."""
    pref = get_user_preferences(db)
    if payload.target_titles is not None:
        pref.target_titles = payload.target_titles
    if payload.target_locations is not None:
        pref.target_locations = payload.target_locations
    if payload.target_cities is not None:
        pref.target_cities = normalize_target_cities(payload.target_cities)
    if payload.work_mode is not None:
        pref.work_mode = payload.work_mode
    if payload.target_country is not None:
        pref.target_country = payload.target_country
    if payload.timezone is not None:
        pref.timezone = payload.timezone
    # Consent records are stamped server-side at the current version (never trust a client version).
    if payload.consents is not None:
        for key, rec in payload.consents.items():
            if (
                key in CONSENT_REQUIRED_FEATURES
                and isinstance(rec, dict)
                and rec.get("ack")
            ):
                # Defer the commit to the single transaction below so a later gate failure
                # rolls back cleanly instead of persisting a partial update.
                record_consent(db, key, commit=False)
    if payload.features is not None:
        current = get_feature_flags(db)
        for key, value in payload.features.items():
            if key in DEFAULT_FEATURE_FLAGS:
                current[key] = bool(value)
        # Hard gate: a risk/ToS feature cannot be enabled without a valid acknowledgment.
        unacknowledged = [
            key
            for key in CONSENT_REQUIRED_FEATURES
            if current.get(key) and not has_consent(db, key)
        ]
        if unacknowledged:
            raise HTTPException(
                status_code=403,
                detail=(
                    "Consent required to enable: "
                    + ", ".join(f"'{k}'" for k in unacknowledged)
                    + ". Acknowledge the associated risk in Settings → Discovery Features first."
                ),
            )
        pref.features_json = json.dumps(current)
    if payload.scoring_config is not None:
        # Keep only known keys; merge over existing so partial updates are supported.
        current = get_scoring_config(db)
        for key, value in payload.scoring_config.items():
            if key in current and value is not None:
                current[key] = float(value)
        pref.scoring_config_json = json.dumps(current)
    if payload.excluded_companies is not None:
        # Normalize to a clean comma-separated, lowercased blocklist.
        tokens = [
            t.strip().lower()
            for t in payload.excluded_companies.split(",")
            if t.strip()
        ]
        pref.excluded_companies = ", ".join(dict.fromkeys(tokens))
    if payload.skip_topics is not None:
        # Normalize skip topics similar to excluded companies
        tokens = [
            t.strip().lower()
            for t in payload.skip_topics.split(",")
            if t.strip()
        ]
        pref.skip_topics = ", ".join(dict.fromkeys(tokens))
    if payload.extra_skills is not None:
        pref.extra_skills_json = json.dumps(_normalize_extra_skills(payload.extra_skills))
    db.commit()
    db.refresh(pref)
    return get_preferences(db=db)


@app.get("/api/resumes", response_model=List[ResumeOut])
def list_resumes(db: Session = Depends(get_db)):
    """Lists all uploaded resumes with decryption and ATS status."""
    resumes = db.query(Resume).order_by(Resume.created_at.desc()).all()
    score_counts = _resume_score_counts(db)
    results = []
    for r in resumes:
        decrypted_content = decrypt_data(r.content_encrypted)
        parsed_json = None
        if r.parsed_json_encrypted:
            try:
                parsed_json = json.loads(decrypt_data(r.parsed_json_encrypted))
            except Exception:
                pass
        results.append(
            ResumeOut(
                id=r.id,
                filename=r.filename,
                is_active=r.is_active,
                created_at=r.created_at,
                raw_preview=truncate_preview(decrypted_content),
                parsed_json=parsed_json,
                parser_version=r.parser_version,
                parse_stale=(r.parser_version or 0) < RESUME_PARSER_VERSION,
                score_count=score_counts.get(r.fingerprint, 0),
            )
        )
    return results


@app.post("/api/resume/{resume_id}/activate", response_model=ResumeOut)
def activate_resume(resume_id: int, db: Session = Depends(get_db)):
    """Switches the active resume."""
    target = db.query(Resume).filter(Resume.id == resume_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="Resume not found.")

    db.query(Resume).update({"is_active": False})
    target.is_active = True
    db.commit()
    db.refresh(target)

    decrypted_content = decrypt_data(target.content_encrypted)
    parsed_json = None
    if target.parsed_json_encrypted:
        try:
            parsed_json = json.loads(decrypt_data(target.parsed_json_encrypted))
        except Exception:
            pass

    return ResumeOut(
        id=target.id,
        filename=target.filename,
        is_active=target.is_active,
        created_at=target.created_at,
        raw_preview=decrypted_content,
        parsed_json=parsed_json,
        parser_version=target.parser_version,
        parse_stale=(target.parser_version or 0) < RESUME_PARSER_VERSION,
    )


@app.delete("/api/resume/{resume_id}", status_code=status.HTTP_200_OK)
def delete_single_resume(resume_id: int, db: Session = Depends(get_db)):
    """Deletes a specific resume and activates the latest remaining one if needed."""
    target = db.query(Resume).filter(Resume.id == resume_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="Resume not found.")

    filename = target.filename
    fingerprint = target.fingerprint
    was_active = target.is_active
    orphaned = 0
    if fingerprint:
        orphaned = (
            db.query(Job).filter(Job.scored_resume_fingerprint == fingerprint).count()
        )
    db.delete(target)
    db.commit()

    activated = None
    if was_active:
        latest = db.query(Resume).order_by(Resume.created_at.desc()).first()
        if latest:
            latest.is_active = True
            db.commit()
            activated = latest.filename

    message = f"Resume '{filename}' deleted successfully."
    if activated:
        message += f" '{activated}' is now the active profile."
    if orphaned:
        message += (
            f" {orphaned} stored score(s) keep their value and are now labelled 'deleted'."
        )
    return {
        "message": message,
        "deleted_id": resume_id,
        "orphaned_scores": orphaned,
        "active_resume_filename": activated,
    }


@app.delete("/api/resume", status_code=status.HTTP_200_OK)
def delete_all_resumes(db: Session = Depends(get_db)):
    """Deletes ALL resumes in the database, allowing a 100% clean slate."""
    deleted_count = db.query(Resume).delete()
    db.commit()
    return {"message": f"All {deleted_count} resume profile(s) deleted successfully."}


class ResumeSkillsUpdate(BaseModel):
    skills: List[str]
    experience_years: Optional[str] = None
    candidate_name: Optional[str] = None
    email: Optional[str] = None


@app.put("/api/resume/{resume_id}/skills", response_model=ResumeOut)
def update_resume_skills(
    resume_id: int, payload: ResumeSkillsUpdate, db: Session = Depends(get_db)
):
    """Allows user to confirm or edit parsed skills and profile details during onboarding."""
    target = db.query(Resume).filter(Resume.id == resume_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="Resume not found.")

    parsed_json = {}
    if target.parsed_json_encrypted:
        try:
            parsed_json = json.loads(decrypt_data(target.parsed_json_encrypted))
        except Exception:
            pass

    parsed_json["skills"] = payload.skills
    if payload.candidate_name:
        parsed_json["name"] = payload.candidate_name
    if payload.email:
        parsed_json["email"] = payload.email
    if payload.experience_years:
        parsed_json["experience_years"] = payload.experience_years

    target.parsed_json_encrypted = encrypt_data(json.dumps(parsed_json))
    db.commit()
    db.refresh(target)

    try:
        index_resume_chunks(db, target)
    except Exception as idx_err:
        logger.debug(f"Resume chunk indexing skipped: {idx_err}")

    decrypted_content = decrypt_data(target.content_encrypted)
    return ResumeOut(
        id=target.id,
        filename=target.filename,
        is_active=target.is_active,
        created_at=target.created_at,
        raw_preview=decrypted_content,
        parsed_json=parsed_json,
        parser_version=target.parser_version,
        parse_stale=(target.parser_version or 0) < RESUME_PARSER_VERSION,
    )


class CleanCacheRequest(BaseModel):
    clean_unapplied_jobs: bool = True
    clean_all_jobs: bool = False


@app.post("/api/resume/{resume_id}/reparse", response_model=ResumeOut)
def reparse_resume(resume_id: int, db: Session = Depends(get_db)):
    """Re-derive ``parsed_json`` from the stored resume text (issue #54).

    ``parsed_json_encrypted`` is a cache: it is written on upload and never re-derived, so a
    parser improvement (or an earlier parse that dropped a role) keeps feeding every score.
    This re-parses the encrypted text in place and stamps ``parser_version``.

    User-owned edits win: ``skills``, ``name``, ``email`` and ``experience_years`` are carried
    over from the stored parse rather than overwritten by the fresh one.
    """
    target = db.query(Resume).filter(Resume.id == resume_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="Resume not found.")

    raw_text = decrypt_data(target.content_encrypted)
    previous: dict = {}
    if target.parsed_json_encrypted:
        try:
            previous = json.loads(decrypt_data(target.parsed_json_encrypted)) or {}
        except Exception:
            previous = {}

    fresh = parse_resume_to_json(raw_text)
    if not fresh:
        raise HTTPException(
            status_code=502,
            detail="The resume parser returned nothing; the stored parse was kept.",
        )

    for field in ("skills", "name", "email", "experience_years"):
        if previous.get(field):
            fresh[field] = previous[field]

    target.parsed_json_encrypted = encrypt_data(json.dumps(fresh))
    target.parser_version = RESUME_PARSER_VERSION
    target.fingerprint = resume_fingerprint(raw_text)
    db.commit()
    db.refresh(target)

    try:
        index_resume_chunks(db, target)
    except Exception as idx_err:
        logger.debug(f"Resume chunk indexing skipped: {idx_err}")

    return ResumeOut(
        id=target.id,
        filename=target.filename,
        is_active=target.is_active,
        created_at=target.created_at,
        raw_preview=raw_text,
        parsed_json=fresh,
        parser_version=target.parser_version,
        parse_stale=False,
    )


@app.post("/api/system/clean-cache")
def clean_system_cache(
    payload: CleanCacheRequest = CleanCacheRequest(), db: Session = Depends(get_db)
):
    """
    Purges generic unapplied jobs from cache/database while strictly preserving all
    Applied, Shortlisted, Screening, Interview, Offered, and other tracked positions.
    Only jobs in 'To Apply' status are cleared.
    """
    deleted_jobs = (
        db.query(Job).filter(Job.status == "To Apply").delete(synchronize_session=False)
    )
    db.commit()

    # Persist Operation Audit Log
    try:
        op_log = OperationLog(
            operation_type="cache_clean",
            status="Completed",
            summary=f"Purged {deleted_jobs} unapplied feed job listing(s) in 'To Apply' status. Applied and Shortlisted jobs remain protected.",
            details_json=sanitize_log_details(
                {
                    "deleted_count": deleted_jobs,
                    "protected_statuses": [
                        "Applied",
                        "Shortlisted",
                        "Screening",
                        "Interview",
                        "Offered",
                    ],
                }
            ),
            jobs_count=deleted_jobs,
        )
        db.add(op_log)
        db.commit()
    except Exception:
        pass

    return {
        "message": f"Successfully purged {deleted_jobs} unapplied cached job listing(s). All Applied and Shortlisted jobs remain protected.",
        "deleted_count": deleted_jobs,
    }


# -------------------------------------------------------------
# Phase 5: Intelligence Scraping, Assisted Apply & Interview Chat
# -------------------------------------------------------------

# Global cancellation events for background scraping & sync tasks


class SingleTailorRequest(BaseModel):
    include_resume_tailoring: bool = True
    include_cover_letter: bool = True
    include_cold_message: bool = True


class BulkTailorRequest(BaseModel):
    job_ids: List[int]
    include_resume_tailoring: bool = True
    include_cover_letter: bool = True
    include_cold_message: bool = True


# -------------------------------------------------------------
# Role Title Enrichment (suggest related titles to broaden matching)
# -------------------------------------------------------------
# Deterministic (no LLM): strips seniority to find the core role, then suggests the
# base title plus curated adjacent titles, so users can add them explicitly.
ROLE_SENIORITY_TOKENS = (
    "staff",
    "principal",
    "senior",
    "lead",
    "distinguished",
    "associate",
    "junior",
    "sr",
    "jr",
    "head of",
    "vp",
    "chief",
)

ROLE_BASE_VARIANTS = {
    "engineer": [
        "Software Engineer",
        "Backend Engineer",
        "Platform Engineer",
        "Systems Engineer",
    ],
    "software engineer": [
        "Software Engineer",
        "Backend Engineer",
        "Platform Engineer",
        "Systems Engineer",
    ],
    "backend engineer": [
        "Backend Engineer",
        "Software Engineer",
        "Server Engineer",
        "Platform Engineer",
    ],
    "frontend engineer": ["Frontend Engineer", "Software Engineer", "Web Engineer"],
    "full stack engineer": [
        "Full Stack Engineer",
        "Software Engineer",
        "Application Engineer",
    ],
    "platform engineer": [
        "Platform Engineer",
        "Infrastructure Engineer",
        "Site Reliability Engineer",
        "DevOps Engineer",
    ],
    "devops engineer": [
        "DevOps Engineer",
        "Platform Engineer",
        "Site Reliability Engineer",
        "Infrastructure Engineer",
    ],
    "site reliability engineer": [
        "Site Reliability Engineer",
        "Platform Engineer",
        "Infrastructure Engineer",
    ],
    "data engineer": ["Data Engineer", "Analytics Engineer", "Data Platform Engineer"],
    "machine learning engineer": [
        "Machine Learning Engineer",
        "ML Engineer",
        "AI Engineer",
        "Applied Scientist",
    ],
    "engineering manager": [
        "Engineering Manager",
        "Engineering Lead",
        "Director of Engineering",
    ],
    "technical architect": [
        "Technical Architect",
        "Software Architect",
        "Solutions Architect",
    ],
    "architect": ["Software Architect", "Technical Architect", "Solutions Architect"],
    "technical lead": ["Technical Lead", "Engineering Lead", "Staff Software Engineer"],
}


def _role_base_title(title: str) -> str:
    """Strips leading seniority tokens to yield the core role (e.g. 'Staff Software Engineer' -> 'software engineer')."""
    t = re.sub(r"[^a-z0-9/ ]+", " ", (title or "").lower()).strip()
    words = t.split()
    changed = True
    while words and changed:
        changed = False
        for tok in ROLE_SENIORITY_TOKENS:
            parts = tok.split()
            if words[: len(parts)] == parts:
                words = words[len(parts) :]
                changed = True
                break
    return " ".join(words).strip()


class RoleEnrichRequest(BaseModel):
    titles: List[str] = []


class RoleSuggestion(BaseModel):
    title: str
    reason: str


class RoleEnrichResponse(BaseModel):
    suggestions: List[RoleSuggestion] = []


@app.post("/api/roles/enrich", response_model=RoleEnrichResponse)
def enrich_role_titles(payload: RoleEnrichRequest):
    """
    Suggests related role titles for the selected target roles so ATS matching is less strict.
    Purely deterministic — the user chooses which suggestions to add (precision stays in their hands).
    """
    selected = [t.strip() for t in (payload.titles or []) if t and t.strip()]
    seen = {t.lower() for t in selected}
    suggestions: List[RoleSuggestion] = []

    def add(title: str, reason: str):
        key = (title or "").strip().lower()
        if key and key not in seen:
            seen.add(key)
            suggestions.append(RoleSuggestion(title=title.strip(), reason=reason))

    for title in selected:
        base = _role_base_title(title)
        if not base:
            continue
        add(base.title(), f"Core role for '{title}'")
        for variant in ROLE_BASE_VARIANTS.get(base, []):
            add(variant, f"Related to '{title}'")

    return RoleEnrichResponse(suggestions=suggestions[:15])


@app.post("/api/jobs/prune")
def prune_jobs(payload: PruneJobsRequest, db: Session = Depends(get_db)):
    """
    Prunes/deletes non-matching jobs in 'To Apply' status based on target titles, locations, and work modes.
    Preserves all jobs with active progress (Applied / Interview / Screening).
    """
    query = db.query(Job).filter(Job.status == "To Apply")
    all_to_apply = query.all()

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

    total_scanned = len(all_to_apply)
    pruned_count = 0
    for job in all_to_apply:
        matches_title = check_title_match(job.title, title_filters)
        matches_loc = check_location_match(
            job.location or "", loc_filters, city_filters
        )
        matches_wm = check_workmode_match(job.title, job.location or "", wm_filter)

        if not (matches_title and matches_loc and matches_wm):
            db.delete(job)
            pruned_count += 1

    db.commit()
    return {
        "message": (
            f"Pruned {pruned_count} of {total_scanned} unapplied listing(s) that didn't match your filters. "
            "Shortlisted, Applied, Screening, Interview, Offered and other tracked jobs were preserved."
        ),
        "pruned_count": pruned_count,
        "scanned_count": total_scanned,
    }


class BulkStatusRequest(BaseModel):
    job_ids: List[int]
    status: str


def _apply_generated_package(
    job: Job, pkg: dict, *, overwrite_drafts: bool = True
) -> None:
    """
    Persist a generated application package onto a Job with consistent semantics:

    - `match_analysis` is always refreshed from the package's strengths/gaps.
    - `match_scored` becomes True only when the package carries a real AI score.
    - Drafts are written when the package provides a non-empty value; existing drafts are
      preserved unless `overwrite_drafts` is True (explicit (re)generation).
    """
    if pkg.get("error"):
        return
    if pkg.get("match_score") is not None:
        job.match_score = pkg["match_score"]
        job.match_scored = True
    job.match_analysis = (
        f"Strengths: {', '.join(pkg.get('strengths', []))}. "
        f"Gaps: {', '.join(pkg.get('gaps', []))}"
    )
    # Persist the structured lists alongside the narrative so the UI can render clean bullets.
    if pkg.get("strengths"):
        job.match_strengths_json = json.dumps([str(s) for s in pkg["strengths"]])
    if pkg.get("gaps"):
        job.match_gaps_json = json.dumps([str(g) for g in pkg["gaps"]])
    if pkg.get("partials"):
        job.match_partials_json = json.dumps([str(p) for p in pkg["partials"]])
    for attr, key in (
        ("cover_letter_draft", "cover_letter"),
        ("tailored_resume_points", "tailored_resume_points"),
        ("cold_message_draft", "cold_message"),
    ):
        value = pkg.get(key)
        if value and (overwrite_drafts or not getattr(job, attr)):
            setattr(job, attr, value)


@app.post("/api/jobs/{job_id}/tailor", response_model=JobOut)
def tailor_single_job(
    job_id: int,
    payload: SingleTailorRequest = SingleTailorRequest(),
    db: Session = Depends(get_db),
):
    """
    On-demand 1-Pass AI customization for a specific job with selective token optimization.
    Generates match analysis, tailored resume points, cover letter, and outreach based on requested options.
    """
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")

    active_resume = (
        db.query(Resume)
        .filter(Resume.is_active == True)
        .order_by(Resume.created_at.desc())
        .first()
    )
    if not active_resume:
        raise HTTPException(
            status_code=400,
            detail="Please upload a resume first to generate tailored application materials.",
        )

    resume_text = decrypt_data(active_resume.content_encrypted)
    comp_name = job.company.name if job.company else "the company"

    pkg = generate_consolidated_application_package(
        resume_text=resume_text,
        job_title=job.title,
        company_name=comp_name,
        job_description=job.description or job.title,
        include_resume_tailoring=payload.include_resume_tailoring,
        include_cover_letter=payload.include_cover_letter,
        include_cold_message=payload.include_cold_message,
        db=db,
    )

    if pkg.get("error"):
        raise HTTPException(status_code=502, detail=pkg["error"])

    _apply_generated_package(job, pkg, overwrite_drafts=True)

    db.commit()
    db.refresh(job)
    return _job_out(db, job)


@app.post("/api/jobs/bulk-tailor")
def bulk_tailor_jobs(payload: BulkTailorRequest, db: Session = Depends(get_db)):
    """Generates AI application materials in batch for the selected job IDs with fine-grained token controls."""
    active_resume = (
        db.query(Resume)
        .filter(Resume.is_active == True)
        .order_by(Resume.created_at.desc())
        .first()
    )
    if not active_resume:
        raise HTTPException(status_code=400, detail="Please upload a resume first.")

    resume_text = decrypt_data(active_resume.content_encrypted)
    tailored = []
    failed = []

    for idx, jid in enumerate(payload.job_ids):
        job = db.query(Job).filter(Job.id == jid).first()
        if not job:
            failed.append(jid)
            continue
        comp_name = job.company.name if job.company else "the company"
        pkg = generate_consolidated_application_package(
            resume_text=resume_text,
            job_title=job.title,
            company_name=comp_name,
            job_description=job.description or job.title,
            include_resume_tailoring=payload.include_resume_tailoring,
            include_cover_letter=payload.include_cover_letter,
            include_cold_message=payload.include_cold_message,
            priority=LLMPriority.BACKGROUND,
            db=db,
        )
        if pkg.get("error"):
            logger.warning(f"Bulk tailor failed for job {jid}: {pkg['error']}")
            failed.append(jid)
            continue
        _apply_generated_package(job, pkg, overwrite_drafts=True)
        tailored.append(job.id)

        # Batch pacing delay between jobs to allow local LLM KV cache/GPU to cycle
        if idx < len(payload.job_ids) - 1:
            time.sleep(0.5)

    db.commit()
    msg = f"Successfully prepared {len(tailored)} job application(s)."
    if failed:
        msg += f" {len(failed)} could not be analyzed (AI unavailable)."
    return {"message": msg, "job_ids": tailored, "failed": failed}


@app.post("/api/jobs/bulk-status")
def bulk_update_job_status(payload: BulkStatusRequest, db: Session = Depends(get_db)):
    """Batch updates status across selected jobs (e.g. mark as Applied, Screening, Rejected)."""
    updated = []
    for jid in payload.job_ids:
        job = db.query(Job).filter(Job.id == jid).first()
        if not job:
            continue
        job.status = payload.status
        # User acted on the job -> no longer a pending "new from sync" review item.
        job.sync_batch_id = None
        if payload.status == "Applied" and not job.applied_at:
            job.applied_at = datetime.datetime.now(datetime.timezone.utc)
        elif payload.status == "Rejected" and not job.rejected_at:
            job.rejected_at = datetime.datetime.now(datetime.timezone.utc)
        elif payload.status in ["Not Interested", "Ignored"]:
            index_dismissed_job_pattern(
                job_title=job.title,
                job_description=job.description or "",
                db=db,
                reason=f"Bulk marked as {payload.status}",
                company_name=job.company.name if job.company else None,
            )
        updated.append(job.id)

    db.commit()
    return {
        "message": f"Updated {len(updated)} job(s) to '{payload.status}'.",
        "job_ids": updated,
    }


@app.post("/api/jobs/{job_id}/apply", response_model=ApplyResponse)
def trigger_assisted_apply(job_id: int, db: Session = Depends(get_db)):
    """
    Prepares an assisted application and returns the materials the browser needs.

    The application itself opens in the browser the user is already working in (see
    frontend/app.js and extension/content/dashboard-bridge.js); this endpoint never
    launches a browser, so it cannot contend with the discovery profile lock.
    Implements Just-In-Time (JIT) material generation if cover letter/tailored points are missing.
    """
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")

    if not job.url:
        raise HTTPException(
            status_code=400, detail="Job does not have an application URL."
        )

    # Load active resume text (materials generation only; form fields are filled in-browser)
    active_resume = (
        db.query(Resume)
        .filter(Resume.is_active == True)
        .order_by(Resume.created_at.desc())
        .first()
    )
    resume_text = ""
    if active_resume and active_resume.parsed_json_encrypted:
        try:
            resume_text = decrypt_data(active_resume.content_encrypted)
        except Exception:
            pass

    # JIT Material Generation: If materials not yet generated, generate them on-demand for this specific job
    if resume_text and (
        not job.cover_letter_draft
        or not job.tailored_resume_points
        or not job.cold_message_draft
    ):
        try:
            comp_name = job.company.name if job.company else "the company"
            pkg = generate_consolidated_application_package(
                resume_text=resume_text,
                job_title=job.title,
                company_name=comp_name,
                job_description=job.description or job.title,
                include_resume_tailoring=True,
                include_cover_letter=True,
                include_cold_message=True,
                db=db,
            )
            if pkg.get("error"):
                logger.warning(
                    f"JIT application material generation skipped: {pkg['error']}"
                )
            else:
                _apply_generated_package(job, pkg, overwrite_drafts=False)
                db.commit()
                db.refresh(job)
        except Exception as e:
            logger.warning(f"JIT application material generation fallback: {e}")

    # Automatically update job status to 'Applied' and log event
    job.status = "Applied"
    if not job.applied_at:
        job.applied_at = datetime.datetime.now(datetime.timezone.utc)

    db.add(
        ApplicationEvent(
            job_id=job.id,
            event_type="applied",
            description=f"Applied via Assisted Application for '{job.title}' at {job.company.name if job.company else 'Company'}",
        )
    )

    # Persist Operation Audit Log
    try:
        op_log = OperationLog(
            operation_type="assisted_apply",
            status="Completed",
            summary=f"Assisted Apply: Auto-filled & launched application for '{job.title}' at {job.company.name if job.company else 'Company'}.",
            details_json=sanitize_log_details(
                {
                    "job_id": job.id,
                    "title": job.title,
                    "company": job.company.name if job.company else "",
                    "url": job.url,
                }
            ),
            jobs_count=1,
        )
        db.add(op_log)
    except Exception as e:
        pass

    db.commit()
    db.refresh(job)

    return ApplyResponse(
        job_id=job.id,
        status="Applied",
        message=f"Application materials ready for '{job.title}'. Apply in your browser.",
        apply=ApplyMaterial(
            job_id=job.id,
            title=job.title,
            company=job.company.name if job.company else "",
            url=job.url,
            cover_letter=job.cover_letter_draft or "",
            tailored_points=job.tailored_resume_points or "",
            cold_msg=job.cold_message_draft or "",
        ),
    )


@app.post("/api/jobs/{job_id}/confirm-submission", response_model=JobOut)
def confirm_job_submission(job_id: int, db: Session = Depends(get_db)):
    """
    Records that the candidate actually submitted an assisted application.
    The job is optimistically moved to 'Applied' when the application tab opens; this
    endpoint sets submission_confirmed=True so the tracker remains auditable.
    """
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")

    now = datetime.datetime.now(datetime.timezone.utc)
    job.submission_confirmed = True
    if job.status != "Applied":
        job.status = "Applied"
    if not job.applied_at:
        job.applied_at = now
    db.add(
        ApplicationEvent(
            job_id=job.id,
            event_type="submitted",
            description=f"Candidate confirmed application submission for '{job.title}'.",
        )
    )
    db.commit()
    db.refresh(job)
    j_out = JobOut.model_validate(job)
    j_out.company_name = job.company.name if job.company else None
    return _apply_score_provenance(j_out, job, _resume_provenance_context(db))


@app.post("/api/jobs/bulk-apply", response_model=BulkApplyResponse)
def trigger_bulk_apply(req: BulkApplyRequest, db: Session = Depends(get_db)):
    """
    Prepares materials for multiple selected job postings and marks them 'Applied'.
    The browser opens them as tabs in one window (extension) or the user opens them
    manually (fallback); no browser is launched server-side.
    """
    if not req.job_ids:
        raise HTTPException(status_code=400, detail="No job IDs provided.")

    jobs = db.query(Job).filter(Job.id.in_(req.job_ids)).all()
    if not jobs:
        raise HTTPException(status_code=404, detail="No matching jobs found.")

    applied_ids = []
    jobs_to_launch = []

    for job in jobs:
        if not job.url:
            continue

        job.status = "Applied"
        if not job.applied_at:
            job.applied_at = datetime.datetime.now(datetime.timezone.utc)

        db.add(
            ApplicationEvent(
                job_id=job.id,
                event_type="applied",
                description=f"Applied via Multi-Tab Assisted Application for '{job.title}' at {job.company.name if job.company else 'Company'}",
            )
        )
        applied_ids.append(job.id)
        jobs_to_launch.append(
            {
                "id": job.id,
                "title": job.title,
                "company": job.company.name if job.company else "",
                "url": job.url,
                "cover_letter": job.cover_letter_draft or "",
                "tailored_points": job.tailored_resume_points or "",
                "cold_msg": job.cold_message_draft or "",
            }
        )

    # Persist Operation Audit Log
    try:
        op_log = OperationLog(
            operation_type="bulk_apply",
            status="Completed",
            summary=f"Bulk Assisted Apply: Opened and auto-filled {len(applied_ids)} application tab(s).",
            details_json=sanitize_log_details({"applied_jobs": jobs_to_launch}),
            jobs_count=len(applied_ids),
        )
        db.add(op_log)
    except Exception as e:
        pass

    db.commit()

    return BulkApplyResponse(
        message=f"Application materials ready for {len(applied_ids)} job(s). Apply in your browser.",
        applied_job_ids=applied_ids,
        jobs=[
            ApplyMaterial(
                job_id=j["id"],
                title=j["title"],
                company=j["company"],
                url=j["url"],
                cover_letter=j["cover_letter"],
                tailored_points=j["tailored_points"],
                cold_msg=j["cold_msg"],
            )
            for j in jobs_to_launch
        ],
    )


@app.post("/api/jobs/{job_id}/chat", response_model=ChatResponse)
def interview_prep_chat(job_id: int, req: ChatRequest, db: Session = Depends(get_db)):
    """
    RAG-powered interview preparation chatbot tailored to the specific role and company.
    """
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")

    comp = db.query(Company).filter(Company.id == job.company_id).first()
    comp_name = comp.name if comp else "the target company"

    # Load active resume
    active_resume = (
        db.query(Resume)
        .filter(Resume.is_active == True)
        .order_by(Resume.created_at.desc())
        .first()
    )
    resume_text = (
        decrypt_data(active_resume.content_encrypted)
        if active_resume
        else "No resume uploaded."
    )

    cleaned_jd = clean_job_description(
        job.description or "Standard requirements for " + job.title, max_chars=800
    )
    system_prompt = interview_prep_advisor_system(
        job_title=job.title,
        company_name=comp_name,
        recent_news=(comp.recent_news if comp else "N/A"),
        hiring_process=(comp.hiring_process if comp else "N/A"),
        salary_insights=(comp.salary_insights if comp else "N/A"),
        cleaned_jd=cleaned_jd,
        resume_text=resume_text,
    )

    reply = generate_text(
        system_prompt=system_prompt,
        user_prompt=req.message,
        json_mode=False,
        temperature=0.5,
        priority=LLMPriority.INTERACTIVE,
        task_name=f"Chat-{comp_name}",
    )

    return ChatResponse(reply=reply, job_id=job.id)


class InterviewQuestionOut(BaseModel):
    question: str
    category: str = ""
    star_situation: str = ""
    star_action: str = ""
    star_result: str = ""
    suggested_answer: str = ""


class InterviewQuestionsRequest(BaseModel):
    num_questions: int = Field(default=6, ge=1, le=15)


class InterviewQuestionsResponse(BaseModel):
    job_id: int
    title: str
    company: str = ""
    questions: List[InterviewQuestionOut] = []
    error: Optional[str] = None


@app.post(
    "/api/jobs/{job_id}/interview-questions", response_model=InterviewQuestionsResponse
)
def generate_job_interview_questions(
    job_id: int,
    req: Optional[InterviewQuestionsRequest] = None,
    db: Session = Depends(get_db),
):
    """
    Structured interview coaching for a specific role: derives likely questions from the job
    description with STAR scaffolds and resume-grounded suggested answers. No fabricated content.
    """
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")

    active_resume = (
        db.query(Resume)
        .filter(Resume.is_active == True)
        .order_by(Resume.created_at.desc())
        .first()
    )
    if not active_resume:
        raise HTTPException(
            status_code=400,
            detail="Please upload a resume before generating interview questions.",
        )
    resume_text = decrypt_data(active_resume.content_encrypted)

    comp = db.query(Company).filter(Company.id == job.company_id).first()
    comp_name = comp.name if comp else ""

    result = generate_interview_questions(
        resume_text=resume_text,
        job_title=job.title,
        job_description=job.description or "",
        company_name=comp_name,
        num_questions=req.num_questions if req else 6,
    )
    if result.get("error"):
        raise HTTPException(status_code=502, detail=result["error"])

    return InterviewQuestionsResponse(
        job_id=job.id,
        title=job.title,
        company=comp_name,
        questions=[InterviewQuestionOut(**q) for q in result.get("questions", [])],
    )


@app.post("/api/chat/assistant", response_model=ChatAssistantResponse)
def career_copilot_chat(req: ChatAssistantRequest, db: Session = Depends(get_db)):
    """
    Multi-turn Conversational AI Career Copilot with database action execution,
    pipeline search, on-demand application tailoring, and interview preparation.
    """
    try:
        agent = CareerChatAgent(db)
        history_dicts = (
            [{"role": h.role, "content": h.content} for h in req.history]
            if req.history
            else []
        )

        result = agent.process_message(
            message=req.message, history=history_dicts, job_id=req.job_id
        )

        return ChatAssistantResponse(
            reply=result.get("reply", ""),
            actions_taken=result.get("actions_taken", []),
            embedded_jobs=result.get("embedded_jobs", []),
        )
    except Exception as e:
        logger.error(f"Error in career_copilot_chat: {e}")
        return ChatAssistantResponse(
            reply=f"⚠️ Career Copilot Notice: {str(e)}",
            actions_taken=[],
            embedded_jobs=[],
        )


@app.post("/api/match/analyze", response_model=MatchAnalyzeResponse)
def analyze_job_match_endpoint(
    payload: MatchAnalyzeRequest, db: Session = Depends(get_db)
):
    """
    Stateless structured resume↔job match evaluation used by the browser extension side panel.

    The returned match_score and the strengths/gaps/feedback narrative are derived from the same
    structured result, so the displayed score circle and the analysis text can never diverge.
    If no resume_text is supplied, the active resume stored in the backend is used.
    """
    active_resume = (
        db.query(Resume)
        .filter(Resume.is_active == True)
        .order_by(Resume.created_at.desc())
        .first()
    )
    resume_text = (payload.resume_text or "").strip()
    if not resume_text and active_resume:
        resume_text = decrypt_data(active_resume.content_encrypted)

    if not resume_text:
        return MatchAnalyzeResponse(
            match_score=None,
            strengths=[],
            gaps=[],
            feedback="",
            resume_available=False,
            analysis_source="error",
            error="No resume available. Upload a resume in the dashboard to enable ATS match scoring.",
        )

    parsed_resume = _parsed_resume_of(active_resume)

    # Lazily index the active resume's chunks so RAG-lite evidence retrieval works
    # for resumes uploaded before chunking existed.
    if active_resume and parsed_resume:
        try:
            from backend.database import ResumeChunk

            if (
                db.query(ResumeChunk)
                .filter(ResumeChunk.resume_id == active_resume.id)
                .count()
                == 0
            ):
                index_resume_chunks(db, active_resume)
        except Exception as idx_err:
            logger.debug(f"Resume chunk indexing skipped: {idx_err}")

    description = (payload.job_description or "").strip()
    if len(description) < MIN_JOB_DESCRIPTION_CHARS:
        return MatchAnalyzeResponse(
            match_score=None,
            strengths=[],
            gaps=[],
            feedback="",
            analysis_source="error",
            error="Job description is too short to score reliably. Open the full posting and retry.",
        )

    evidence = []
    if active_resume:
        try:
            evidence = top_evidence(db, active_resume.id, description, k=4)  # pyright: ignore[reportArgumentType]
        except Exception as ev_err:
            logger.debug(f"Resume evidence retrieval skipped: {ev_err}")

    det_match_details = _deterministic_match(
        db,
        resume_text,
        description,
        parsed_resume,
        job_title=payload.job_title or "",
        location=payload.location or "",
    )
    if det_match_details:
        return MatchAnalyzeResponse(
            match_score=det_match_details["score"],
            band=det_match_details["band"],
            apply_recommendation=det_match_details["recommendation"],
            eligible=det_match_details["eligible"],
            ineligibility_reason=det_match_details["ineligibility_reason"],
            strengths=det_match_details["strengths"],
            gaps=det_match_details["gaps"],
            partials=det_match_details["partials"],
            feedback=det_match_details["feedback"],
            requirements=[
                r.model_dump() for r in det_match_details["assessment"].requirements
            ],
            unverified=det_match_details["unverified"],
            must_coverage=det_match_details["must_coverage"],
            nice_coverage=det_match_details["nice_coverage"],
            over_qualified=det_match_details["over_qualified"],
            analysis_source="classifier",
            resume_available=True,
            match_method=det_match_details["method"],
            score_method=det_match_details["score_method"],
        )
    logger.debug(
        "Deterministic classifier did not produce a match; falling back to LLM analysis."
    )

    # FALLBACK PATH: Full LLM extraction (only if classifier fails). Bounded so an
    # offline/slow local model cannot block the interactive request for the full timeout.
    try:
        assessment, match = assess_job_requirements(
            resume_text,
            description,
            parsed_resume=parsed_resume,
            scoring_config=ScoringConfig.from_mapping(get_scoring_config(db)),
            evidence=evidence,
            timeout=LLM_INTERACTIVE_TIMEOUT,
        )
    except Exception as e:
        logger.warning(f"Requirement-based match analysis failed: {e}")
        assessment, match = None, None

    if assessment is None or not assessment.requirements:
        # Never fabricate a score. Surface the failure explicitly so the UI can offer a re-process.
        logger.warning(
            "Match analysis produced no usable result; returning an error state."
        )
        return MatchAnalyzeResponse(
            match_score=None,
            strengths=[],
            gaps=[],
            feedback="",
            analysis_source="error",
            error=(
                "Match analysis could not be completed. Please verify the job description "
                "is complete and your resume is uploaded, then re-process."
            ),
        )

    strengths, gaps, partials = _derive_strengths_gaps(assessment, match)

    eligible, ineligibility_reason = _check_eligibility(
        db, payload.job_title, payload.location, description
    )

    return MatchAnalyzeResponse(
        match_score=match.score,
        strengths=strengths,
        gaps=gaps,
        partials=partials,
        feedback=assessment.summary or "",
        analysis_source="llm",
        band=match.band,
        apply_recommendation=match.apply_recommendation,
        must_coverage=match.must_coverage,
        nice_coverage=match.nice_coverage,
        over_qualified=match.over_qualified,
        eligible=eligible,
        ineligibility_reason=ineligibility_reason,
        requirements=[r.model_dump() for r in assessment.requirements],
        unverified=match.unverified,
        score_method=match.method,
    )


@app.post("/api/match/{job_id}/detailed-analysis")
def generate_detailed_match_analysis(job_id: int, db: Session = Depends(get_db)):
    """
    On-demand detailed match analysis for a job.

    Called when user clicks "Explain" or "View Details" in the extension.
    Returns full LLM-generated narrative (strengths/gaps/feedback) and caches it in Job.match_analysis.

    Fast-path classifier returns score instantly; this endpoint provides narrative context.
    """
    from backend.database import Job, Resume
    from backend.config import decrypt_data

    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        return {"error": f"Job {job_id} not found", "analysis": None}

    if job.match_analysis:
        # Already cached; return immediately
        return {
            "analysis": job.match_analysis,
            "cached": True,
            "job_id": job_id,
        }

    # Fetch active resume
    active_resume = (
        db.query(Resume)
        .filter(Resume.is_active == True)
        .order_by(Resume.created_at.desc())
        .first()
    )

    if not active_resume:
        return {
            "error": "No active resume found",
            "analysis": None,
            "job_id": job_id,
        }

    resume_text = decrypt_data(active_resume.content_encrypted)
    parsed_resume = parse_resume_to_json(resume_text)

    try:
        # Run full LLM extraction
        assessment, match = assess_job_requirements(
            resume_text,
            job.description or "",
            parsed_resume=parsed_resume,
            scoring_config=ScoringConfig.from_mapping(get_scoring_config(db)),
            evidence=[],  # On-demand, no pre-fetched evidence
        )
    except Exception as e:
        logger.warning(f"LLM analysis for job {job_id} failed: {e}")
        return {
            "error": f"Analysis failed: {str(e)}",
            "analysis": None,
            "job_id": job_id,
        }

    if not assessment:
        return {
            "error": "No assessment produced",
            "analysis": None,
            "job_id": job_id,
        }

    # Format narrative
    strengths, gaps, partials = _derive_strengths_gaps(assessment, match)
    narrative = _match_analysis_text(assessment, match)

    # Cache in database (narrative + structured lists for the gap-insights UI)
    job.match_analysis = narrative
    job.match_strengths_json = json.dumps(list(strengths))
    job.match_gaps_json = json.dumps(list(gaps))
    job.match_partials_json = json.dumps(list(partials))
    db.commit()

    logger.info(f"Cached detailed analysis for job {job_id}")

    return {
        "analysis": narrative,
        "strengths": strengths,
        "gaps": gaps,
        "partials": partials,
        "feedback": assessment.summary or "",
        "cached": False,
        "job_id": job_id,
        "method": "llm_detailed",
    }


class AuthSessionRequest(BaseModel):
    url: Optional[str] = "https://www.linkedin.com/login"


@app.post("/api/browser/auth-session")
@app.post("/api/browser/interactive-session")
def launch_interactive_browser_session(
    payload: Optional[AuthSessionRequest] = None, target_url: Optional[str] = None
):
    """
    Launches an interactive (headed) Chromium browser session using the persistent Chrome profile (.chrome_profile).
    Allows user to sign in to LinkedIn and career portals once so cookies/tokens persist for all future agent operations.
    """
    from backend.playwright_app import run_interactive_session

    final_url = "https://www.linkedin.com/login"
    if payload and payload.url:
        final_url = payload.url.strip()
    elif target_url:
        final_url = target_url.strip()

    parsed_url = urllib.parse.urlparse(final_url)
    if parsed_url.scheme not in ("http", "https"):
        raise HTTPException(
            status_code=400,
            detail="Invalid URL scheme. Only 'http://' and 'https://' URLs are permitted for interactive sessions.",
        )

    task_id = task_engine.submit_task(
        "browser_session",
        "Interactive browser login",
        run_interactive_session,
        final_url,
    )

    return {
        "status": "Launched",
        "task_id": task_id,
        "hold_seconds": INTERACTIVE_SESSION_HOLD_SECONDS,
        "message": (
            f"Interactive browser launched for '{final_url}'. Sign in to your account, "
            f"and your session will persist automatically in .chrome_profile. The window "
            f"closes itself after {INTERACTIVE_SESSION_HOLD_SECONDS // 60} minutes, or "
            f"cancel it from the task list."
        ),
    }


# -------------------------------------------------------------
# External Applications Dashboard Sync (Hirist / Greenhouse / Gmail)
# -------------------------------------------------------------

# Confidence gates for automatic email-driven DB writes.
# Events without an explicit confidence (e.g. legacy/manual callers) are treated as trusted.
EMAIL_STATUS_MIN_CONFIDENCE = 0.6  # to mutate an existing job's status
EMAIL_AUTO_CREATE_MIN_CONFIDENCE = 0.75  # to create a brand-new job from an email
EMAIL_OPPORTUNITY_MIN_CONFIDENCE = 0.6  # to create a job from an email opportunity


# One spelling per concept: "Software Engineering PMTS" and "Software Engineer" describe the
# same role, and the mismatch used to rank the correct application below a sibling title.
_TITLE_TOKEN_SYNONYMS = {
    "engineering": "engineer",
    "development": "developer",
    "technologies": "technology",
    "applications": "application",
    "systems": "system",
    "platforms": "platform",
}


def _title_tokens(title: str) -> set:
    """Normalised token set for title comparison (case/punctuation/seniority-agnostic)."""
    return {
        _TITLE_TOKEN_SYNONYMS.get(t, t)
        for t in re.split(r"[^a-z0-9+#]+", (title or "").lower())
        if t and t not in _TITLE_NOISE_TOKENS
    }


# Words that carry no discriminative value when comparing a parsed email role to a tracked title.
_TITLE_NOISE_TOKENS = {"the", "and", "for", "with", "role", "position", "opening", "job", "a", "an"}

# Attribution keys strong enough to *create* an application from a scheduling mail. A title
# guess must never do that (2026-11-16: someone else's invite turned a Shortlisted row Applied).
_STRONG_MATCH_SOURCES = ("requisition", "thread", "exact_title", "auto_created")

# A role made only of these says nothing about WHICH application the mail is about. A generic
# "Software Engineer" is a subset of every sibling engineering title, so it used to win the
# subset tier against the row the mail actually named (2026-11-16 Salesforce poisoning).
_GENERIC_ROLE_TOKENS = {
    "engineer", "developer", "programmer", "software", "sre", "devops", "architect",
    "manager", "analyst", "scientist", "designer", "specialist", "consultant", "director",
    "lead", "senior", "staff", "principal", "junior", "associate", "intern", "head",
    "officer", "technical", "technology", "it", "member", "generalist", "ic",
}


def _is_live_application(job) -> bool:
    """True when this row is an application still in play.

    An application counts as live once the operator has applied (``applied_at`` or a status past
    applied) **and** it has not reached a terminal state. A discovery row in 'To Apply' /
    'Shortlisted' is not an application, and a 'Rejected' / 'Withdrawn' / 'Not Interested' row is
    no longer in play — an email must not be attributed to either when a live application exists.
    'Closed' stays eligible: the posting disappeared, which says nothing about the application.
    """
    if job.status in ("Rejected", "Withdrawn", "Not Interested"):
        return False
    return bool(job.applied_at) or job.status in (
        "Applied",
        "Screening",
        "Interview Scheduling",
        "Interview",
        "Offered",
    )


def _candidate_brief(job) -> Dict[str, Any]:
    return {
        "job_id": job.id,
        "title": job.title,
        "company": job.company.name if job.company else None,
        "status": job.status,
        "applied_at": job.applied_at.isoformat() if job.applied_at else None,
    }


def _company_looks_like_a_sentence(name: str) -> bool:
    """True for a company row created from a mangled subject fragment ("GitLab was submitted").

    Such rows exist (the email parser can promote a whole subject to the company) and they widen a
    name search for free, so they are skipped when resolving a company by name.
    """
    n = (name or "").strip().lower()
    if not n:
        return True
    return any(
        marker in n
        for marker in (
            " was ",
            " has ",
            " is ",
            " your ",
            " was submitted",
            " submitted",
            " application",
            " interview",
            " update",
        )
    )


def _match_job_for_email_event(
    db,
    comp_name: str,
    role: str = "",
    domain: str = "",
    requisition_id: str = "",
    thread_id: str = "",
):
    """Finds the tracked application an email is about.

    Returns ``(job, info)``. ``info`` describes HOW it matched and whether the evidence was
    ambiguous, so the caller can stage the email for review instead of guessing:

    - ``matched_by``: ``requisition`` | ``thread`` | ``exact_domain`` | ``exact_title``
      | ``title_contains`` | ``title_overlap`` | ``ambiguous`` | ``no_company``
      | ``no_candidate`` | ``no_title_evidence``
    - ``ambiguous``: True when several applications fit equally well (or a live application fits
      no title evidence at all) - the operator picks in that case.
    - ``candidates``: brief rows for the review queue / log.

    Identification runs strongest key first, and only falls through when a key is absent:

    1. **requisition id** - names exactly one posting at one employer.
    2. **thread** - a reply inherits the application its thread is already linked to.
    3. company + title evidence (exact > contains > token overlap).

    An **applied** application outranks a discovery row at the same title evidence; ``Job.id`` is
    only a final tie-break.
    """
    role = (role or "").strip()
    domain = (domain or "").strip().lower()
    requisition_id = (requisition_id or "").strip()
    thread_id = (thread_id or "").strip()
    role_tokens = _title_tokens(role)

    companies = []
    if domain:
        comp_domain = db.query(Company).filter(Company.domain.ilike(domain)).first()
        if comp_domain:
            companies.append(comp_domain)
    if not companies and comp_name:
        companies = db.query(Company).filter(Company.name.ilike(f"%{comp_name}%")).all()

    if requisition_id:
        by_req = [
            j
            for j in db.query(Job).filter(Job.requisition_id.ilike(requisition_id)).all()
            if j.status not in ("Rejected", "Not Interested")
        ]
        if len(by_req) > 1 and companies:
            # A generic id shape can collide across employers; prefer the resolved company.
            scoped = [j for j in by_req if j.company_id in {c.id for c in companies}]
            if scoped:
                by_req = scoped
        if len(by_req) == 1:
            return by_req[0], {
                "matched_by": "requisition",
                "ambiguous": False,
                "candidates": [_candidate_brief(by_req[0])],
            }
        if len(by_req) > 1:
            return None, {
                "matched_by": "ambiguous",
                "ambiguous": True,
                "candidates": [_candidate_brief(j) for j in by_req[:5]],
            }

    if thread_id:
        link = (
            db.query(EmailApplicationLink)
            .filter(EmailApplicationLink.thread_id == thread_id)
            .order_by(EmailApplicationLink.id.desc())
            .first()
        )
        if link is not None and link.job is not None and link.job.status not in (
            "Rejected",
            "Not Interested",
        ):
            return link.job, {
                "matched_by": "thread",
                "ambiguous": False,
                "candidates": [_candidate_brief(link.job)],
            }

    if not companies:
        return None, {"matched_by": "no_company", "ambiguous": False, "candidates": []}

    jobs = (
        db.query(Job)
        .filter(Job.company_id.in_([c.id for c in companies]))
        .order_by(Job.id.desc())
        .all()
    )
    # Rows in a terminal state are out of play: an application that is already Rejected or marked
    # Not Interested cannot absorb a new outcome email. Everything else stays eligible, including
    # a discovery row — applications are often made outside the tracker, so a row with no
    # ``applied_at`` can still be the one the email is about.
    candidates = [j for j in jobs if j.status not in ("Rejected", "Not Interested")]
    if not candidates:
        return None, {"matched_by": "no_candidate", "ambiguous": False, "candidates": []}

    def _title_tier(job) -> int:
        title_tokens = _title_tokens(job.title)
        if not role_tokens or not title_tokens:
            return 4
        if title_tokens == role_tokens:
            return 1
        # A role that is nothing but role nouns ("Software Engineer") is a subset of every
        # sibling engineering title, so it must not win the subset tier by accident. It still
        # matches its own title exactly (tier 1 above).
        if role_tokens <= _GENERIC_ROLE_TOKENS:
            return 4
        if role_tokens <= title_tokens or title_tokens <= role_tokens:
            return 2
        shared = role_tokens & title_tokens
        # A single shared word ("engineer") is not evidence: every engineering title shares one.
        # Neither is a pair that only states seniority and the job family: "Staff Backend
        # Engineer" and "Staff Software Engineer - NLP" share exactly {staff, engineer}, which at
        # an employer with several applications is how an outcome lands on a sibling requisition.
        # Overlap needs at least one token that actually names the specialisation.
        distinguishing = shared - _GENERIC_ROLE_TOKENS
        return 3 if len(shared) >= 2 and distinguishing else 4

    def _rank(job):
        # The role named in the email is the strongest signal; an application the operator has
        # actually recorded breaks a tie at the same title evidence; newest row last.
        return (_title_tier(job), 0 if _is_live_application(job) else 1, -job.id)

    ranked = sorted(candidates, key=_rank)
    best_rank = _rank(ranked[0])
    best_tier = best_rank[0]
    group = [j for j in ranked if _rank(j)[:2] == best_rank[:2]]

    labels = {1: "exact_title", 2: "title_contains", 3: "title_overlap", 4: "no_title_evidence"}
    label = labels[best_tier]

    if len(group) > 1:
        return None, {
            "matched_by": "ambiguous",
            "ambiguous": True,
            "candidates": [_candidate_brief(j) for j in group[:5]],
        }

    if best_tier == 4:
        # Not one shared token between the email's role and any tracked application: never guess,
        # even when only one application exists at the company (a rejection for a different role
        # would otherwise flip it).
        return None, {
            "matched_by": "no_title_evidence",
            "ambiguous": False,
            "candidates": [_candidate_brief(j) for j in candidates[:5]],
        }

    winner = group[0]
    return winner, {
        "matched_by": label,
        "ambiguous": False,
        "candidates": [_candidate_brief(winner)],
    }



def _ensure_tracked_job_for_email_event(db, ev):
    """
    Creates a tracked job for an application email when no existing job matches,
    so manual applications and their outcomes are captured even if never discovered
    by a portal/LinkedIn/Google scan.
    """
    comp_name = (ev.get("company") or "").strip()
    role = (ev.get("role") or "").strip() or "Open Role"
    if not comp_name:
        return None
    if _company_looks_like_a_sentence(comp_name):
        # A mangled subject fragment is not an employer; stage it for the operator instead of
        # minting a company like "GitLab was submitted".
        return None
    comp = db.query(Company).filter(Company.name.ilike(comp_name)).first()
    if not comp:
        comp = Company(
            name=comp_name, domain=f"{comp_name.lower().replace(' ', '')}.com"
        )
        db.add(comp)
        db.flush()
    exists = (
        db.query(Job).filter(Job.company_id == comp.id, Job.title.ilike(role)).all()
    )
    if exists:
        # The same title can exist twice (a discovery row and the real application): attach to the
        # application the operator has actually recorded, not the oldest row.
        exists.sort(key=lambda j: (0 if _is_live_application(j) else 1, -j.id))
        return exists[0]
    job = Job(
        company_id=comp.id,
        title=role,
        description=ev.get("snippet")
        or f"Application email for {role} at {comp_name}.",
        url=ev.get("url") or "",
        location="Remote / Various",
        source="Gmail Application",
        source_type="Email",
        status="To Apply",
        match_score=0,
        match_scored=False,
        match_analysis=f"Tracked from Gmail: '{ev.get('subject', '')}'.",
    )
    db.add(job)
    db.flush()
    return job


def _email_has_application_cue(ev) -> bool:
    """True when the email clearly references the candidate's own application process.

    Distinguishes real status emails ("your application", "interview", "assessment", "offer")
    from recruiter cold outreach or generic notifications that share a confidence boost but
    must never auto-create a tracked job.
    """
    text = f"{ev.get('subject', '')} {ev.get('snippet', '')}".lower()
    cues = (
        "your application", "application received", "you applied", "your job application",
        "application confirmation", "thank you for applying", "thanks for applying",
        "applying", "applied for", "interview", "interview confirmation", "phone screen",
        "recruiter screen", "assessment", "offer", "offer letter", "next steps",
        "not moving forward", "regret", "was viewed", "screening", "scheduling",
        "availability", "code challenge", "technical screen",
    )
    return any(cue in text for cue in cues)


# A mail can report that the *posting* is gone without the employer rejecting the candidate
# (requisition filled, expired, unlisted). That is "Closed", not "Rejected".
_EMAIL_POSTING_CLOSURE_CUES = (
    "position has been closed", "posting has been closed", "job posting has closed",
    "position is closed", "position closed", "requisition has been closed",
    "requisition closed", "this role has been closed", "role has been closed",
    "no longer accepting applications", "posting is no longer available",
    "position is no longer available", "job has been closed", "application closed",
    "posting has closed", "has been filled and is no longer",
)

# Invitation-to-schedule wording. A confirmed "your interview is on <date>" mail is not here:
# that is the interview stage, not the scheduling stage.
_EMAIL_INTERVIEW_SCHEDULING_CUES = (
    "schedule your interview", "schedule a time", "schedule time", "self-schedule",
    "self schedule", "pick a time", "select a time", "book a time", "find a time",
    "interview availability", "your availability", "invitation to interview",
    "schedule your", "scheduling link", "choose a time slot", "select a slot",
)


def _email_text(ev) -> str:
    return f"{ev.get('subject', '')} {ev.get('snippet', '')}".lower()


def _email_reports_posting_closure(ev) -> bool:
    """True when the mail says the posting closed, rather than the employer rejecting."""
    text = _email_text(ev)
    return any(cue in text for cue in _EMAIL_POSTING_CLOSURE_CUES)


def _email_is_interview_invite(ev) -> bool:
    """True when the mail invites the candidate to schedule an interview."""
    text = _email_text(ev)
    return any(cue in text for cue in _EMAIL_INTERVIEW_SCHEDULING_CUES)


def _email_auto_create_eligible(ev) -> bool:
    """Unambiguous case → safe to auto-create a tracked job from an email (issue #76).

    Auto-create only when ALL hold: it is a status event, confidence is at or above the
    auto-create threshold, the sender domain is a real employer (not ATS/aggregator/freemail),
    an application-reference cue is present, and a role resolved. Anything else is staged for
    the operator instead.
    """
    if ev.get("event_type") not in (
        "application_received", "screening", "interview", "offer", "rejection",
    ):
        return False
    if float(ev.get("confidence", 1.0) or 0.0) < EMAIL_AUTO_CREATE_MIN_CONFIDENCE:
        return False
    if not (ev.get("company") or "").strip():
        return False
    role = (ev.get("role") or "").strip()
    # A placeholder role ("Open Role") is not a resolved role → not unambiguous.
    if not role or role.lower() == "open role":
        return False
    domain = (ev.get("domain") or "").strip()
    if not domain or _is_ats_or_freemail_domain(domain):
        return False
    return _email_has_application_cue(ev)


def _email_is_suppressed(db, ev) -> bool:
    """True when the operator has ignored this employer+role or the whole employer."""
    domain = (ev.get("domain") or "").strip().lower()
    role = (ev.get("role") or "").strip()
    comp = (ev.get("company") or "").strip().lower()
    if not (domain or comp):
        return False
    # Suppressions are keyed by domain (when known), else employer name.
    key = domain or comp
    return bool(
        db.query(EmailSuppression).filter(
            EmailSuppression.domain.ilike(key),
            (EmailSuppression.role.is_(None)) | (EmailSuppression.role.ilike(role)),
        ).first()
    )


def _stage_email_application(db, ev) -> bool:
    """Stages a non-auto-create application email into the anti-fatigue review queue.

    Thread-aggregated: one row per (normalized employer, normalized role). A repeat email
    for the same employer+role bumps ``last_seen``/``email_count`` instead of creating a new
    card. Returns True if a pending row exists or was created.

    Only genuine application-status signals are staged: a freemail sender or a message with
    no application-reference cue (cold outreach, "unable to reach you") queues nothing.
    """
    comp = (ev.get("company") or "").strip()
    role = (ev.get("role") or "").strip() or "Software Engineer"
    if not comp:
        return False
    domain = (ev.get("domain") or "").strip()
    if domain and _is_freemail_domain(domain):
        return False
    if not _email_has_application_cue(ev):
        return False
    if _email_is_suppressed(db, ev):
        return False
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    pending = (
        db.query(PendingEmailApplication)
        .filter(
            PendingEmailApplication.company.ilike(comp),
            PendingEmailApplication.role.ilike(role),
            PendingEmailApplication.state == "pending",
        )
        .first()
    )
    if pending:
        pending.last_seen = now_utc
        pending.email_count = (pending.email_count or 0) + 1
        if ev.get("message_id"):
            pending.message_id = ev["message_id"]
        if ev.get("thread_id"):
            pending.thread_id = ev["thread_id"]
        if ev.get("event_type"):
            pending.event_type = ev["event_type"]
        if ev.get("snippet"):
            pending.snippet = (ev["snippet"] or "")[:SNIPPET_CHARS]
        return True
    db.add(
        PendingEmailApplication(
            company=comp,
            domain=(ev.get("domain") or "").strip(),
            role=role,
            event_type=ev.get("event_type") or "application_received",
            confidence=float(ev.get("confidence", 0.0) or 0.0),
            first_seen=now_utc,
            last_seen=now_utc,
            email_count=1,
            state="pending",
            snippet=(ev.get("snippet") or "")[:SNIPPET_CHARS],
            message_id=ev.get("message_id") or None,
            thread_id=ev.get("thread_id") or None,
        )
    )
    return True


_RELAY_EMPLOYER_PATTERNS = (
    re.compile(r"application successful:\s*([^-\u2013\u2014]{2,40})\s*[-\u2013\u2014]", re.I),
    re.compile(r"thank you for applying to\s+([^!.,\n]{2,40})", re.I),
    re.compile(r"applying to\s+([^.,\n]{2,40}?)\s+for the role", re.I),
    re.compile(r"application (?:to|for)\s+([^.,\n]{2,40}?)\s+(?:-|for|position)", re.I),
    # Screening vendors ("Schedule Your <Employer> - US Interview!", "<Employer> - Karat Interview")
    re.compile(r"schedule your\s+([^-\u2013\u2014!\n]{2,40})\s*[-\u2013\u2014]", re.I),
    re.compile(r"^\s*([^-\u2013\u2014!\n]{2,40})\s*[-\u2013\u2014]\s*(?:karat|hackerrank|codility)", re.I),
    re.compile(r"(?:karat|hackerrank|codility)[^.!?\n]{0,20}\b(?:with|for|at)\s+([^.,!\n]{2,40})", re.I),
    # Scheduling vendors (GoodTime/ModernLoop): "Please Confirm: Your <Employer> Virtual
    # Interview", "Reminder - <Employer> Virtual Interview - <candidate> for <role>".
    re.compile(
        r"\byour\s+([^.!?\n]{2,40}?)\s+(?:virtual|phone|technical|video|on-?site)\s+interview",
        re.I,
    ),
    re.compile(
        r"^\s*(?:reminder\s*[-\u2013\u2014]\s*)?([^-\u2013\u2014!\n]{2,40}?)\s+"
        r"(?:virtual|phone|technical|video|on-?site)\s+interview",
        re.I,
    ),
    # Career-portal digests relayed by an ATS ("MyGreenhouse: New Jobs at Airbnb").
    re.compile(r"new\s+jobs?\s+at\s+([^-\u2013\u2014!\n.,]{2,40})", re.I),
)


def _is_relay_sender(ev) -> bool:
    """True when the parsed employer is really an ATS/relay, never the hiring company.

    Relays (`myworkday.com`, `hirist.tech`, `karat.com`, `greenhouse-mail.io`, ...) hide the
    employer in their sender domain, so using them as the company key files every application from
    that vendor under one bogus row (one row ended up holding three unrelated applications).
    """
    company = (ev.get("company") or "").strip()
    domain = (ev.get("domain") or "").strip()
    if company and _is_aggregator_company(company):
        return True
    return bool(domain) and _is_ats_or_freemail_domain(domain)


_RELAY_CANDIDATE_TAIL = re.compile(
    r"\s+(?:is|are|was|has|have|will|would|interview|interviews|screening|invitation|confirmed|"
    r"scheduled|assessment|test)\b.*$",
    re.I,
)


def _clean_relay_candidate(candidate: str) -> str:
    """Trims sentence residue from a subject-derived employer ("Initech is confirmed" -> "Initech")."""
    text = _RELAY_CANDIDATE_TAIL.sub("", candidate or "").strip(" .,-\u2013\u2014:;'’")
    return text


def _employer_from_relay_email(db, ev) -> Optional[str]:
    """Best-effort recovery of the hiring company from a relay-sent email.

    Order: explicit subject patterns ("Application Successful: F5 - ..."), the relay's per-employer
    local part (`salesforce@myworkday.com`), then any tracked company name mentioned in the email.
    """
    subject = ev.get("subject") or ""
    for pattern in _RELAY_EMPLOYER_PATTERNS:
        match = pattern.search(subject)
        if match:
            candidate = _clean_relay_candidate(match.group(1))
            if candidate and len(candidate) <= 40 and not _is_aggregator_company(candidate):
                return candidate

    from sqlalchemy import func

    sender = ev.get("sender") or ""
    if "@" in sender:
        local = sender.split("@", 1)[0].split("+")[0].strip()
        if len(local) >= 4 and not _is_aggregator_company(local):
            # Exact name first, then the tenant prefix: "walmart@myworkday.com" is
            # "Walmart Global Tech", and "salesforce@myworkday.com" is "Salesforce".
            hit = (
                db.query(Company)
                .filter(Company.name.ilike(local))
                .first()
                or db.query(Company)
                .filter(Company.name.ilike(f"{local}%"))
                .order_by(func.length(Company.name))
                .first()
            )
            if hit:
                return hit.name

    best = None
    haystack = f"{subject}\n{ev.get('snippet') or ''}"
    for comp in db.query(Company).all():
        name = (comp.name or "").strip()
        if len(name) < 4 or _is_aggregator_company(name):
            continue
        if re.search(rf"\b{re.escape(name)}\b", haystack, re.I):
            if best is None or len(name) > len(best):
                best = name
    return best


# Match methods strong enough to teach a job its own requisition id. A weak/ambiguous match must
# never bind an id to a row: the next mail carrying that id would follow the same mistake.
_STRONG_MATCH_METHODS = frozenset(
    {"requisition", "exact_title", "title_contains", "accepted_pending", "auto_created"}
)


def _record_email_application_link(db, ev, job, info, requisition_id: str = "") -> None:
    """Persist which message was attributed to which application (upsert by ``message_id``)."""
    if job is None:
        return
    if requisition_id and not job.requisition_id and (info or {}).get("matched_by") in _STRONG_MATCH_METHODS:
        job.requisition_id = requisition_id
        job.requisition_source = _req_source_of(ev)
    message_id = (ev.get("message_id") or "").strip()
    if not message_id:
        return
    link = (
        db.query(EmailApplicationLink)
        .filter(EmailApplicationLink.message_id == message_id)
        .first()
    )
    if link is None:
        link = EmailApplicationLink(message_id=message_id, job_id=job.id)
        db.add(link)
    elif link.job_id != job.id:
        logger.info(
            "Email %s re-pointed from job %s to %s (%s)",
            message_id,
            link.job_id,
            job.id,
            (info or {}).get("matched_by") or "?",
        )
        link.job_id = job.id
    link.thread_id = ev.get("thread_id") or link.thread_id
    link.method = (info or {}).get("matched_by") or link.method or ""
    link.confidence = float(ev.get("confidence", 0.0) or 0.0)
    link.sender = (ev.get("sender") or "")[:255]
    link.subject = (ev.get("subject") or "")[:2000]
    link.received_at = (ev.get("date") or "")[:64]


def _req_source_of(ev) -> str:
    from backend.requisition import extract_requisition_id

    return extract_requisition_id(ev.get("subject") or "", ev.get("snippet") or "")[1]


def _apply_email_events_to_db(
    db, email_events, out_changes: Optional[List[Dict[str, Any]]] = None
) -> int:
    """
    Maps classified email events to Job status updates and ApplicationEvents:
      application_received -> Applied, screening -> Screening, interview -> Interview,
      offer -> Offered, rejection -> Rejected.
    Returns the number of status updates applied. 'opportunity' events are ignored here
    (see _ingest_email_opportunities_to_db).

    Confidence guard: events below EMAIL_STATUS_MIN_CONFIDENCE are ignored, and new jobs
    are only created when confidence >= EMAIL_AUTO_CREATE_MIN_CONFIDENCE. This prevents a
    single weak keyword (e.g. a marketing email containing "unfortunately") from mutating
    application state or creating phantom Rejected jobs.
    """
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    status_updates = 0
    skipped_low_confidence = 0
    ambiguous_staged = 0
    relay_staged = 0
    weak_staged = 0
    changes = out_changes if out_changes is not None else []
    for ev in email_events or []:
        ev_type = ev.get("event_type", "")
        if ev_type == "opportunity":
            continue
        comp_name = ev.get("company", "")
        role = ev.get("role", "")
        subj = ev.get("subject", "")
        if _is_relay_sender(ev):
            # A relay is not an employer: resolve the hiring company, or stage the email so it is
            # never filed as its own application.
            recovered = _employer_from_relay_email(db, ev)
            if not recovered:
                if _stage_email_application(db, ev):
                    relay_staged += 1
                logger.info(
                    "Gmail status email came via a relay with no recoverable employer "
                    "(%s): staged for review.",
                    (ev.get("domain") or ev.get("company") or "?"),
                )
                continue
            comp_name = recovered
        if not comp_name:
            continue
        confidence = float(ev.get("confidence", 1.0) or 0.0)
        if confidence < EMAIL_STATUS_MIN_CONFIDENCE:
            skipped_low_confidence += 1
            logger.info(
                f"Gmail email event ignored (confidence {confidence:.2f} < "
                f"{EMAIL_STATUS_MIN_CONFIDENCE}): '{subj[:60]}'"
            )
            continue
        # The strongest keys first: a requisition id names exactly one posting, and a reply
        # inherits its thread's application. Both are absent for most legacy mails, in which
        # case the company + title tiers decide.
        requisition_id, _req_source = extract_requisition_id(
            subj, ev.get("snippet") or ""
        )
        matched_job, match_info = _match_job_for_email_event(
            db,
            comp_name,
            role,
            ev.get("domain", ""),
            requisition_id=requisition_id,
            thread_id=ev.get("thread_id", ""),
        )
        if not matched_job:
            # Ambiguous: several applications fit the email equally well, or the only fitting rows
            # are ones the operator never applied to. Guessing here flips the wrong application, so
            # the email goes to the review queue with its candidates instead (issue: GitLab
            # "Staff Backend Engineer" rejection landing on a discovered posting).
            if match_info.get("ambiguous"):
                if _stage_email_application(db, ev):
                    ambiguous_staged += 1
                logger.info(
                    "Gmail email event is ambiguous (%s): %s candidates at '%s' — staged for review.",
                    match_info.get("matched_by"),
                    len(match_info.get("candidates") or []),
                    comp_name,
                )
                continue
            # The company already has tracked applications, but none of them shares a usable
            # title with the mail: auto-creating another row here is how a generic role minted a
            # phantom application beside the real one. Stage it and let the operator choose.
            if match_info.get("matched_by") == "no_title_evidence" and match_info.get("candidates"):
                if _stage_email_application(db, ev):
                    weak_staged += 1
                logger.info(
                    "Gmail email event has no title evidence (%s candidates at '%s') — staged.",
                    len(match_info.get("candidates") or []),
                    comp_name,
                )
                continue
            # Capture applications/outcomes for roles never discovered by a scan — but ONLY
            # for the unambiguous case (issue #76). Otherwise stage it for the operator so a
            # real application is never silently dropped or auto-created as a phantom job.
            if _email_auto_create_eligible(ev):
                matched_job = _ensure_tracked_job_for_email_event(db, ev)
            else:
                _stage_email_application(db, ev)
                continue
            if not matched_job:
                continue
            match_info = {"matched_by": "auto_created", "ambiguous": False}

        new_status = None
        previous_status = matched_job.status
        desc = ""
        note = ""
        if ev_type in ("interview", "screening") and not _is_live_application(matched_job):
            # A scheduling/interview mail proves an *application*, not that an interview happened
            # for this requisition. Asserting "Interview" here is how rows the operator never
            # applied to ended up in the interview stage (2026-11-16 cleanup: Microsoft, FIRY,
            # Amazon, InfoVision, Meazure, DeepMind). Mark the application, leave the stage alone.
            #
            # Only with a strong attribution: a title-overlap guess must not *create* an
            # application, which is how a sibling row was poisoned by someone else's invite.
            if match_info.get("matched_by") not in _STRONG_MATCH_SOURCES:
                if _stage_email_application(db, ev):
                    weak_staged += 1
                logger.info(
                    "Gmail scheduling mail matched only by '%s' at '%s' — staged.",
                    match_info.get("matched_by"),
                    comp_name,
                )
                continue
            new_status = "Applied"
            note = "a scheduling email proves an application, not an interview"
            if not matched_job.applied_at:
                matched_job.applied_at = now_utc
        elif ev_type == "rejection":
            if _email_reports_posting_closure(ev):
                # The posting is gone, the candidate was not turned down: keep those apart so a
                # closure count never reads as a rejection count.
                if matched_job.status != "Closed":
                    new_status = "Closed"
                    note = "the mail reports the posting closed, not an employer rejection"
            elif matched_job.status != "Rejected":
                new_status = "Rejected"
                matched_job.rejected_at = now_utc
        elif ev_type == "offer":
            if matched_job.status != "Offered":
                new_status = "Offered"
        elif ev_type == "interview":
            if matched_job.status not in ("Interview Scheduling", "Interview", "Offered"):
                if _email_is_interview_invite(ev):
                    new_status = "Interview Scheduling"
                    note = "an invitation to schedule, not a confirmed interview"
                else:
                    new_status = "Interview"
                    matched_job.interview_scheduled_at = now_utc
        elif ev_type == "screening":
            if matched_job.status in ("To Apply", "Shortlisted", "Applied"):
                new_status = "Screening"
        elif ev_type == "application_received":
            if matched_job.status in ("To Apply", "Shortlisted", "Not Interested"):
                new_status = "Applied"
                matched_job.applied_at = now_utc

        if new_status:
            matched_job.status = new_status
            # The event is the evidence trail: say WHEN the mail arrived, WHAT moved, and WHICH
            # message caused it, so the dossier explains the status instead of asserting it.
            received = (ev.get("received_at") or "").strip()
            when = received[:10] if received else now_utc.strftime("%Y-%m-%d")
            source = (ev.get("sender") or ev.get("domain") or "email").strip()
            desc = (
                f"Mail {when} from {source}: {previous_status or 'unknown'} → {new_status} "
                f"— '{subj}'"
            )
            if note:
                desc = f"{desc} ({note})"
            db.add(
                ApplicationEvent(
                    job_id=matched_job.id,
                    event_type=f"email_{ev_type}",
                    description=desc,
                )
            )
            status_updates += 1
            # The operator must be able to tell WHICH application moved, not just how many.
            changes.append(
                {
                    "job_id": matched_job.id,
                    "title": matched_job.title,
                    "company": (
                        matched_job.company.name
                        if matched_job.company
                        else comp_name
                    ),
                    "from": previous_status,
                    "to": new_status,
                    "event": ev_type,
                    "subject": (subj or "")[:200],
                    "matched_by": match_info.get("matched_by") or "email",
                }
            )

        # Attribution is a record, not a sentence: one row per message (unique), so the same mail
        # can never be attached to two applications and a correction is an update.
        _record_email_application_link(db, ev, matched_job, match_info, requisition_id)

    if skipped_low_confidence:
        logger.info(
            f"Gmail sync: {skipped_low_confidence} low-confidence status event(s) skipped."
        )
    if ambiguous_staged:
        logger.info(
            f"Gmail sync: {ambiguous_staged} ambiguous status event(s) staged for review."
        )
    if relay_staged:
        logger.info(
            f"Gmail sync: {relay_staged} relay-sent status event(s) staged (no employer resolved)."
        )
    if weak_staged:
        logger.info(
            f"Gmail sync: {weak_staged} weakly-matched status event(s) staged for review."
        )
    db.commit()
    return status_updates


# Sender/aggregator names that are NOT the real employer. When an opportunity event's
# company is one of these, the true employer is recovered from the alert snippet.
_AGGREGATOR_COMPANY_NAMES = {
    "",
    "linkedin",
    "linkedin network",
    "linkedin job alert",
    "indeed",
    "glassdoor",
    "google",
    "google alerts",
    "ziprecruiter",
    "naukri",
    "wellfound",
    "angel.co",
    "monster",
    "dice",
    "builtin",
    "hirist",
    "foundit",
    "myworkday",
    "workday",
    "kekamail",
    # Interview scheduling / assessment vendors (they mail on behalf of the employer).
    "goodtime",
    "goodtime.io",
    "modernloop",
    "calendly",
    "hirevue",
    "sparkhire",
    "vidcruiter",
    "paradox",
    "karat",
    "recruiterflow",
    "recruiterflowmail",
    "jobgether",
    "greenhouse",
    "lever",
    "ashby",
    "discovered employer",
    "discovered via google alert",
    "direct",
    "unknown company",
    "open role",
}

# Pipeline progress ranking used when merging duplicate opportunity rows. The most
# advanced row wins so shortlisted/applied state is never lost.
_JOB_STATUS_RANK = {
    "Not Interested": -1,
    "Rejected": 0,
    "Closed": 0,
    "Withdrawn": 0,
    "To Apply": 0,
    "Shortlisted": 1,
    "Applied": 2,
    "Screening": 3,
    "Interview Scheduling": 4,
    "Interview": 5,
    "Offered": 6,
}


def _is_aggregator_company(name: str) -> bool:
    """True if the name is an alert aggregator/sender rather than the real employer."""
    return (name or "").strip().lower() in _AGGREGATOR_COMPANY_NAMES


def _normalize_opportunity_fingerprint(text: str, limit: int = 180) -> str:
    """Lower-cased, whitespace/padding-stripped fingerprint of an alert snippet."""
    if not text:
        return ""
    cleaned = re.sub(r"[\s\u200b\u200c\u200d\ufeff\u034f\u00ad]+", " ", text)
    return cleaned.strip().lower()[:limit]


def _find_existing_opportunity_job(db, company_id: int, title: str, url: str = ""):
    """Finds a tracked job matching an incoming opportunity by URL, else employer + title."""
    url = (url or "").strip()
    if url:
        hit = db.query(Job).filter(Job.url.in_([url, url.rstrip("/")])).first()
        if hit:
            return hit
    if not title:
        return None
    return (
        db.query(Job)
        .filter(Job.company_id == company_id, Job.title.ilike(title))
        .first()
    )


def dedupe_gmail_opportunity_jobs(db) -> int:
    """
    Collapses duplicate Gmail-opportunity jobs sharing the same identity: normalized URL,
    or alert-snippet fingerprint, or employer + title. The most advanced pipeline row is
    kept (tie-break: earliest id) and its siblings' ApplicationEvents are re-pointed to it
    before deletion.

    This guards against historical duplicates created before identity-aware ingestion
    (e.g. when autoflush=False let same-batch rows slip past the dedup query). Returns the
    number of rows removed. Safe to run repeatedly.
    """
    jobs = (
        db.query(Job)
        .filter(Job.source == "Gmail Opportunity")
        .order_by(Job.id.asc())
        .all()
    )
    groups = {}
    for job in jobs:
        groups.setdefault(_opportunity_identity_key(job), []).append(job)

    removed = 0
    for group in groups.values():
        if len(group) < 2:
            continue
        keep = max(group, key=lambda j: (_JOB_STATUS_RANK.get(j.status, 0), -j.id))
        for dup in group:
            if dup.id == keep.id:
                continue
            for event in list(dup.events):
                event.job_id = keep.id
            if not (keep.url or "").strip() and (dup.url or "").strip():
                keep.url = dup.url
            db.delete(dup)
            removed += 1
    if removed:
        db.flush()
        logger.info(f"Deduplicated {removed} duplicate Gmail opportunity job(s).")
    return removed


def _opportunity_identity_key(job):
    """Identity used to group legacy Gmail-opportunity rows for deduplication."""
    url = (job.url or "").strip().rstrip("/").lower()
    if url:
        return ("url", url)
    fingerprint = _normalize_opportunity_fingerprint(job.description)
    if fingerprint:
        return ("desc", fingerprint)
    return ("title", job.company_id, (job.title or "").strip().lower())


def _ingest_email_opportunities_to_db(
    db, email_events, out_records: Optional[List[Dict[str, Any]]] = None
) -> int:
    """
    Ingests opportunities discovered in Gmail (job alerts / recruiter outreach) as
    'To Apply' jobs, so roles surfaced by email are tracked even without an application.

    Identity: aggregator senders (LinkedIn, Indeed, ...) are resolved to the real employer
    + role from the alert snippet, then deduplicated by URL (when present) or employer +
    title. Duplicates within a single batch are suppressed too, since the session runs
    with autoflush=False and pending rows are not yet query-visible.
    """
    added = 0
    seen = set()
    added_records = out_records if out_records is not None else []
    pref_filter = PreferenceFilter.from_db(db)
    filtered_out = 0
    for ev in email_events or []:
        if ev.get("event_type") != "opportunity":
            continue
        fallback_company = (ev.get("company") or "").strip()
        fallback_role = (ev.get("role") or "").strip() or "Open Role"
        snippet = (ev.get("snippet") or "").strip()
        subject = ev.get("subject", "")
        url = (ev.get("url") or "").strip()
        # Agency-relayed JD (issue #82): track under the *employer* (dedup with a direct posting)
        # while preserving the agency on the source string for context.
        employer = (ev.get("employer") or "").strip()
        agency = (ev.get("agency") or "").strip()
        ev_location = (ev.get("location") or "").strip()

        comp_name, role = fallback_company, fallback_role
        # Aggregators hide the real employer in the sender header; recover it from the
        # alert snippet so distinct roles at different employers don't collapse together.
        if _is_aggregator_company(fallback_company):
            parsed_company, parsed_role = extract_opportunity_company_and_title(
                snippet, subject, fallback_company, fallback_role
            )
            if parsed_company and not _is_aggregator_company(parsed_company):
                comp_name = parsed_company
            if parsed_role:
                role = parsed_role
        if employer:
            comp_name = employer
        if not comp_name:
            continue

        confidence = float(ev.get("confidence", 1.0) or 0.0)
        if confidence < EMAIL_OPPORTUNITY_MIN_CONFIDENCE:
            continue

        # Preference gate: Gmail roles carry no structured location, so verify against the
        # alert text. When location filters exist, an unverifiable location is rejected.
        if pref_filter.has_filters:
            loc_text = f"{ev_location} {snippet} {subject}".strip()
            if not pref_filter.title_ok(role):
                filtered_out += 1
                continue
            if pref_filter.criteria.locations or pref_filter.criteria.cities:
                if not loc_text or not pref_filter.location_ok(loc_text):
                    filtered_out += 1
                    continue
            if not pref_filter.work_mode_ok(role, loc_text):
                filtered_out += 1
                continue

        # In-batch dedup key (pending rows aren't query-visible with autoflush disabled).
        batch_key = (
            ("url", url.rstrip("/").lower())
            if url
            else ("ct", comp_name.lower(), role.lower())
        )
        if batch_key in seen:
            continue

        comp = db.query(Company).filter(Company.name.ilike(comp_name)).first()
        if not comp:
            comp = Company(
                name=comp_name, domain=f"{comp_name.lower().replace(' ', '')}.com"
            )
            db.add(comp)
            db.flush()

        if _find_existing_opportunity_job(db, comp.id, role, url):
            seen.add(batch_key)
            continue

        source = "Gmail Opportunity"
        if agency and agency.lower() != comp_name.lower():
            source = f"Gmail Opportunity (via {agency})"

        db.add(
            Job(
                company_id=comp.id,
                title=role,
                description=snippet
                or f"Opportunity from Gmail for {role} at {comp_name}.",
                url=url,
                location=ev_location or "Remote / Various",
                source=source,
                source_type="Email",
                status="To Apply",
                match_score=55.0,
                match_analysis=f"Opportunity detected in Gmail: '{ev.get('subject', '')}'.",
            )
        )
        db.flush()
        seen.add(batch_key)
        added += 1
        added_records.append(
            {
                "title": role,
                "company": comp_name,
                "url": url,
                "subject": (subject or "")[:200],
                "source": source,
            }
        )
    dedupe_gmail_opportunity_jobs(db)
    db.commit()
    if filtered_out:
        logger.info(
            f"[PreferenceFilter] Gmail opportunities: {added} ingested, {filtered_out} filtered out of preference."
        )
    return added


@app.post("/api/applications/sync-external", response_model=ExternalSyncResponse)
def sync_external_applications(
    payload: ExternalSyncRequest, db: Session = Depends(get_db)
):
    """Synchronous candidate application-status sync (request path)."""
    return _execute_external_sync(payload, db)


# -------------------------------------------------------------
# Email review queue (anti-fatigue, issue #76)
# -------------------------------------------------------------

# Age after which a still-unresolved pending review item is dropped.
EMAIL_PENDING_EXPIRY_DAYS = 14


def _expire_stale_email_pending(db) -> int:
    """Marks pending review items older than EMAIL_PENDING_EXPIRY_DAYS as expired."""
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
        days=EMAIL_PENDING_EXPIRY_DAYS
    )
    stale = (
        db.query(PendingEmailApplication)
        .filter(
            PendingEmailApplication.state == "pending",
            PendingEmailApplication.last_seen < cutoff,
        )
        .all()
    )
    for row in stale:
        row.state = "expired"
    if stale:
        db.commit()
    return len(stale)


@app.get("/api/email/applications/pending")
def list_pending_email_applications(
    db: Session = Depends(get_db),
):
    """Lists staged, unresolved application emails for the operator review banner.

    Expired items are dropped silently first, so the count never shows stale nag cards.
    """
    _expire_stale_email_pending(db)
    rows = (
        db.query(PendingEmailApplication)
        .filter(PendingEmailApplication.state == "pending")
        .order_by(PendingEmailApplication.last_seen.desc())
        .all()
    )
    return {
        "count": len(rows),
        "items": [
            {
                "id": r.id,
                "company": r.company,
                "domain": r.domain or "",
                "role": r.role,
                "event_type": r.event_type,
                "confidence": r.confidence,
                "first_seen": r.first_seen.isoformat() if r.first_seen else "",
                "last_seen": r.last_seen.isoformat() if r.last_seen else "",
                "email_count": r.email_count or 1,
                "snippet": r.snippet or "",
            }
            for r in rows
        ],
    }


@app.post("/api/email/applications/pending/{pending_id}/accept")
def accept_pending_email_application(pending_id: int, db: Session = Depends(get_db)):
    """Tracks the staged application: creates/updates Company + Job and marks it accepted."""
    pending = db.query(PendingEmailApplication).get(pending_id)
    if not pending or pending.state != "pending":
        return {"ok": False, "detail": "Not found or already resolved."}
    ev = {
        "company": pending.company,
        "domain": pending.domain or "",
        "role": pending.role,
        "event_type": pending.event_type,
        "snippet": pending.snippet or "",
        "subject": f"{pending.company} {pending.role}",
        "confidence": pending.confidence,
        # The staged card keeps the message identity, so accepting it records the link and the
        # next mail in that thread inherits this application.
        "message_id": pending.message_id or "",
        "thread_id": pending.thread_id or "",
    }
    job = _ensure_tracked_job_for_email_event(db, ev)
    if not job:
        return {"ok": False, "detail": "Could not create a tracked job."}
    # Tracking an application is not just "make a row": the email's outcome (interview, rejection,
    # offer) has to land on the application as evidence, otherwise the dossier says nothing.
    _apply_email_events_to_db(db, [ev], [])
    pending.state = "accepted"
    db.commit()
    return {"ok": True, "job_id": job.id, "company": pending.company, "role": pending.role}


@app.post("/api/email/applications/pending/{pending_id}/ignore")
def ignore_pending_email_application(
    pending_id: int, db: Session = Depends(get_db)
):
    """Ignores one staged application (employer+role) and suppresses it from ever resurfacing."""
    pending = db.query(PendingEmailApplication).get(pending_id)
    if not pending or pending.state != "pending":
        return {"ok": False, "detail": "Not found or already resolved."}
    key = (pending.domain or "").strip().lower() or (pending.company or "").strip().lower()
    role = (pending.role or "").strip()
    if key:
        existing = (
            db.query(EmailSuppression)
            .filter(
                EmailSuppression.domain.ilike(key),
                EmailSuppression.role.ilike(role),
            )
            .first()
        )
        if not existing:
            db.add(EmailSuppression(domain=key, role=role or None))
    pending.state = "accepted"
    db.commit()
    return {"ok": True, "ignored": key, "role": role}


@app.post("/api/email/applications/pending/{pending_id}/ignore-domain")
def ignore_pending_email_domain(
    pending_id: int, db: Session = Depends(get_db)
):
    """Ignores everything from the staged application's employer (domain-wide suppression)."""
    pending = db.query(PendingEmailApplication).get(pending_id)
    if not pending or pending.state != "pending":
        return {"ok": False, "detail": "Not found or already resolved."}
    key = (pending.domain or "").strip().lower() or (pending.company or "").strip().lower()
    if key:
        existing = (
            db.query(EmailSuppression)
            .filter(
                EmailSuppression.domain.ilike(key),
                EmailSuppression.role.is_(None),
            )
            .first()
        )
        if not existing:
            db.add(EmailSuppression(domain=key, role=None))
    pending.state = "accepted"
    db.commit()
    return {"ok": True, "ignored_domain": key}


def _execute_external_sync(
    payload: ExternalSyncRequest,
    db: Session,
    cancel_token: Optional[CancellationToken] = None,
    progress_cb: Optional[Callable] = None,
) -> ExternalSyncResponse:
    """
    Synchronizes candidate applications across external platforms:
    - Hirist (`hirist.tech` / `hirist.com`): Candidate dashboard scraping, status synchronization, and auto-ingestion.
    - Greenhouse (`greenhouse.io` / `boards.greenhouse.io`): Requisition availability probe.
    - Lever (`jobs.lever.co`): Requisition availability probe via the public Postings API.
    - SmartRecruiters (`jobs.smartrecruiters.com`): Requisition availability probe via the public Posting API.
    - Gmail (`mail.google.com`): Interview invites and rejection status synchronization.
    """
    hirist_checked = 0
    hirist_updated = 0
    hirist_ingested = 0
    emails_checked = 0
    status_updates = 0
    opportunities_added = 0
    email_changes: List[Dict[str, Any]] = []
    email_opportunities: List[Dict[str, Any]] = []
    details = []
    # Per-channel outcomes so the run status can be honest (see backend.channel_status).
    outcomes = []

    def _source_enabled(source: str, flag: str) -> bool:
        """Probing runs only when the source is requested AND its portal flag is enabled."""
        if source not in payload.sources and "all" not in payload.sources:
            return False
        if not is_feature_enabled(db, flag):
            details.append(
                f"{source} probing skipped: enable '{flag}' in Settings → Discovery Features."
            )
            channel_status.record(
                outcomes, source, channel_status.SKIPPED_FLAG, f"'{flag}' disabled"
            )
            return False
        return True

    active_resume = db.query(Resume).filter(Resume.is_active == True).first()
    resume_text = (
        decrypt_data(active_resume.content_encrypted)
        if (active_resume and active_resume.content_encrypted)
        else ""
    )
    parsed_resume = _parsed_resume_of(active_resume)

    if cancel_token:
        cancel_token.check()

    # 1. Hirist Applications Dashboard Sync (opt-in, default enabled)
    if _source_enabled("hirist", "ats_hirist"):
        try:
            with sync_playwright() as p:
                hirist_apps = scan_and_sync_hirist_applications(
                    p, headless=payload.headless, max_pages=payload.max_pages
                )
                hirist_checked = len(hirist_apps)

                for item in hirist_apps:
                    title = item.get("title", "").strip()
                    comp_name = _safe_company_name(
                        item.get("company"), "Hirist Employer"
                    )
                    target_status = item.get("status", "Applied")
                    raw_status = item.get("raw_status", target_status)
                    item_url = item.get("url", "")

                    if not title:
                        continue

                    # Search matching job in DB
                    matched_job = None
                    if item_url:
                        matched_job = db.query(Job).filter(Job.url == item_url).first()

                    if not matched_job:
                        matched_job = (
                            db.query(Job)
                            .filter(
                                (Job.title.ilike(title))
                                & (Job.company.has(Company.name.ilike(comp_name)))
                            )
                            .first()
                        )

                    if not matched_job:
                        matched_job = (
                            db.query(Job)
                            .filter(
                                (Job.title.ilike(title))
                                & (Job.source_type.in_(["Hirist", "Direct"]))
                            )
                            .first()
                        )

                    now_utc = datetime.datetime.now(datetime.timezone.utc)

                    if matched_job:
                        # Existing job in DB -> Check if status changed
                        if matched_job.status != target_status:
                            old_status = matched_job.status
                            matched_job.status = target_status
                            if (
                                target_status == "Interview"
                                and not matched_job.interview_scheduled_at
                            ):
                                matched_job.interview_scheduled_at = now_utc
                            elif (
                                target_status == "Rejected"
                                and not matched_job.rejected_at
                            ):
                                matched_job.rejected_at = now_utc

                            event = ApplicationEvent(
                                job_id=matched_job.id,
                                event_type="external_status_sync",
                                description=f"Status transitioned from '{old_status}' to '{target_status}' on Hirist (raw: '{raw_status}').",
                            )
                            db.add(event)
                            hirist_updated += 1
                            details.append(
                                f"Updated '{title}' at {comp_name}: {old_status} -> {target_status}"
                            )
                    else:
                        # New application on Hirist -> Ingest into DB
                        comp = (
                            db.query(Company)
                            .filter(Company.name.ilike(comp_name))
                            .first()
                        )
                        if not comp:
                            comp = Company(
                                name=comp_name,
                                domain=f"{comp_name.lower().replace(' ', '')}.com",
                            )
                            db.add(comp)
                            db.flush()

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
                            item.get("description") or "",
                            parsed_resume=parsed_resume,
                            job_title=item.get("title") or "",
                        )
                        if not match_analysis:
                            match_analysis = f"Imported from Hirist candidate applications dashboard. Current Status: {raw_status}."

                        job_emb = generate_embeddings(
                            f"{title} {comp.name} {item.get('location', '')}"
                        )
                        new_job = Job(
                            company_id=comp.id,
                            title=title,
                            description=f"Candidate application for {title} at {comp.name} submitted via Hirist. Status: {raw_status}.",
                            url=item_url
                            or f"https://www.hirist.tech/candidate/applications",
                            location=item.get("location", "India / Remote"),
                            source="Hirist Applications",
                            source_type="Hirist",
                            status=target_status,
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
                            applied_at=now_utc
                            if target_status in ["Applied", "Interview", "Rejected"]
                            else None,
                            rejected_at=now_utc
                            if target_status == "Rejected"
                            else None,
                            interview_scheduled_at=now_utc
                            if target_status == "Interview"
                            else None,
                            embedding=job_emb,
                        )
                        db.add(new_job)
                        db.flush()

                        event = ApplicationEvent(
                            job_id=new_job.id,
                            event_type="external_application_ingested",
                            description=f"Ingested application from Hirist portal (Status: {raw_status}).",
                        )
                        db.add(event)
                        hirist_ingested += 1
                        details.append(
                            f"Ingested '{title}' at {comp_name} (Status: {target_status})"
                        )

                db.commit()
            channel_status.record(
                outcomes, "hirist", channel_status.OK, f"{hirist_checked} application(s) checked"
            )
        except Exception as e:
            logger.error(f"Error during Hirist application sync: {e}")
            details.append(f"Hirist sync error: {str(e)}")
            channel_status.record(outcomes, "hirist", channel_status.ERROR, str(e))

    if cancel_token:
        cancel_token.check()

    # 2. Gmail Decision & Interview Emails Sync (OPT-IN)
    if "gmail" in payload.sources and not is_feature_enabled(db, "gmail_sync"):
        details.append(
            "Gmail sync skipped: enable 'Gmail sync' in Settings → Opt-in Discovery Features."
        )
        channel_status.record(
            outcomes, "gmail", channel_status.SKIPPED_FLAG, "'gmail_sync' disabled"
        )

    if "gmail" in payload.sources and is_feature_enabled(db, "gmail_sync"):
        email_events = []
        used_api = False
        api_error = None
        fallback_error = None
        # API-first when connected; otherwise restricted browser fallback.
        if is_gmail_configured():
            api_stats: Dict[str, Any] = {}
            try:
                from backend.gmail_client import run_gmail_sync

                email_events = run_gmail_sync(db, stats=api_stats)
                used_api = True
                # A *first* alert pass that matches nothing means the query or the granted
                # scope is wrong — not that the mailbox is quiet — so fall back once.
                if (
                    int(api_stats.get("alert_messages_matched") or 0) == 0
                    and not api_stats.get("alerts_had_watermark")
                ):
                    api_error = "Gmail API matched no alert mail on a first run"
                    used_api = False
            except Exception as api_err:
                api_error = str(api_err)
                logger.warning(
                    f"Gmail API path failed, using browser fallback: {api_err}"
                )

        if not used_api:
            details.append(
                "Gmail API not connected; using restricted browser fallback."
                if not api_error
                else f"Gmail API notice: {api_error}. Using restricted browser fallback."
            )
            try:
                with sync_playwright() as p:
                    email_events = sync_email_application_events(
                        p, headless=payload.headless
                    )
            except Exception as e:
                fallback_error = str(e)
                logger.error(f"Error during browser email status sync: {e}")
                details.append(f"Email sync notice: {str(e)}")

        emails_checked = len(email_events)
        status_updates += _apply_email_events_to_db(
            db, email_events, email_changes
        )
        for change in email_changes:
            details.append(
                f"Email status change: '{change['title']}' at {change['company']} — "
                f"{change['from'] or 'unknown'} → {change['to']} (subject: '{change['subject'][:80]}')."
            )
        opportunities_added += _ingest_email_opportunities_to_db(
            db, email_events, email_opportunities
        )
        for opp in email_opportunities:
            details.append(
                f"Gmail opportunity tracked: '{opp['title']}' at {opp['company']}."
            )

        # Honest channel outcome: "0 emails" is only meaningful when the API path ran.
        if used_api:
            channel_status.record(
                outcomes, "gmail", channel_status.OK, f"{emails_checked} message(s) matched"
            )
        elif fallback_error:
            channel_status.record(
                outcomes, "gmail", channel_status.ERROR, fallback_error
            )
        elif api_error:
            channel_status.record(
                outcomes, "gmail", channel_status.NOT_CONNECTED, api_error
            )
        elif emails_checked == 0:
            channel_status.record(
                outcomes,
                "gmail",
                channel_status.DEGRADED_ZERO,
                "Gmail API not connected and the browser fallback found 0 messages; "
                "reconnect Gmail in Settings to use the API path.",
            )
        else:
            channel_status.record(
                outcomes,
                "gmail",
                channel_status.OK,
                f"restricted browser fallback, {emails_checked} message(s) matched",
            )

    if cancel_token:
        cancel_token.check()

    # 3. Greenhouse Active Requisition Probing
    if _source_enabled("greenhouse", "ats_greenhouse"):
        try:
            greenhouse_jobs = (
                db.query(Job)
                .filter(
                    (
                        Job.url.ilike("%boards.greenhouse.io%")
                        | Job.url.ilike("%job-boards.greenhouse.io%")
                    )
                    & (Job.status.in_(["Applied", "Shortlisted"]))
                )
                .all()
            )

            for gh_job in greenhouse_jobs:
                is_active = probe_greenhouse_job_active(gh_job.url)
                if not is_active and gh_job.status not in ("Rejected", "Closed"):
                    # A vanished requisition is a closure, not an employer rejection.
                    gh_job.status = "Closed"
                    db.add(
                        ApplicationEvent(
                            job_id=gh_job.id,
                            event_type="requisition_closed",
                            description="Greenhouse job requisition has been closed or unlisted.",
                        )
                    )
                    status_updates += 1
                    details.append(
                        f"Greenhouse requisition closed for '{gh_job.title}'"
                    )

            channel_status.record(
                outcomes, "greenhouse", channel_status.OK,
                f"{len(greenhouse_jobs)} requisition(s) probed",
            )
            db.commit()
        except Exception as e:
            logger.debug(f"Error during Greenhouse requisition probe: {e}")
            channel_status.record(outcomes, "greenhouse", channel_status.ERROR, str(e))

    if cancel_token:
        cancel_token.check()

    # 4. Lever Active Requisition Probing
    if _source_enabled("lever", "ats_lever"):
        try:
            lever_jobs = (
                db.query(Job)
                .filter(
                    Job.url.ilike("%jobs.lever.co%")
                    & (Job.status.in_(["Applied", "Shortlisted"]))
                )
                .all()
            )

            for lv_job in lever_jobs:
                if (
                    not probe_lever_job_active(lv_job.url)
                    and lv_job.status not in ("Rejected", "Closed")
                ):
                    lv_job.status = "Closed"
                    db.add(
                        ApplicationEvent(
                            job_id=lv_job.id,
                            event_type="requisition_closed",
                            description="Lever job posting has been closed or unlisted.",
                        )
                    )
                    status_updates += 1
                    details.append(f"Lever requisition closed for '{lv_job.title}'")

            channel_status.record(
                outcomes, "lever", channel_status.OK,
                f"{len(lever_jobs)} requisition(s) probed",
            )
            db.commit()
        except Exception as e:
            logger.debug(f"Error during Lever requisition probe: {e}")
            channel_status.record(outcomes, "lever", channel_status.ERROR, str(e))

    if cancel_token:
        cancel_token.check()

    # 5. SmartRecruiters Active Posting Probing
    if _source_enabled("smartrecruiters", "ats_smartrecruiters"):
        try:
            sr_jobs = (
                db.query(Job)
                .filter(
                    Job.url.ilike("%smartrecruiters.com%")
                    & (Job.status.in_(["Applied", "Shortlisted"]))
                )
                .all()
            )

            for sr_job in sr_jobs:
                if (
                    not probe_smartrecruiters_job_active(sr_job.url)
                    and sr_job.status not in ("Rejected", "Closed")
                ):
                    sr_job.status = "Closed"
                    db.add(
                        ApplicationEvent(
                            job_id=sr_job.id,
                            event_type="requisition_closed",
                            description="SmartRecruiters job posting has been closed or unlisted.",
                        )
                    )
                    status_updates += 1
                    details.append(
                        f"SmartRecruiters requisition closed for '{sr_job.title}'"
                    )

            channel_status.record(
                outcomes, "smartrecruiters", channel_status.OK,
                f"{len(sr_jobs)} posting(s) probed",
            )
            db.commit()
        except Exception as e:
            logger.debug(f"Error during SmartRecruiters requisition probe: {e}")
            channel_status.record(outcomes, "smartrecruiters", channel_status.ERROR, str(e))

    # Log operation telemetry. Status reflects what actually happened: a run where a
    # requested channel never executed is not a success.
    run_status = channel_status.aggregate_status(outcomes)
    degraded_note = channel_status.describe(outcomes)
    summary = (
        f"External Sync: {hirist_checked} Hirist checked ({hirist_updated} updated, "
        f"{hirist_ingested} ingested), {emails_checked} emails scanned "
        f"({status_updates} status updates, {opportunities_added} opportunities)."
    )
    if degraded_note:
        summary = f"{summary} Degraded — {degraded_note}"
    db.add(
        OperationLog(
            operation_type="external_application_sync",
            status=run_status,
            summary=summary,
            details_json=sanitize_log_details(
                {
                    "details": details,
                    "channels": outcomes,
                    "status_changes": email_changes,
                    "opportunities": email_opportunities,
                }
            ),
            jobs_count=hirist_checked + emails_checked,
            prompt_tokens=0,
            completion_tokens=0,
        )
    )
    db.commit()

    if progress_cb:
        progress_cb(
            step=1,
            label="Application statuses synced.",
            items_found=hirist_checked + emails_checked,
            total_steps=1,
        )

    return ExternalSyncResponse(
        hirist_checked=hirist_checked,
        hirist_updated=hirist_updated,
        hirist_ingested=hirist_ingested,
        emails_checked=emails_checked,
        status_updates=status_updates,
        opportunities_added=opportunities_added,
        pending_email_applications=(
            db.query(PendingEmailApplication)
            .filter(PendingEmailApplication.state == "pending")
            .count()
        ),
        details=details,
        message=f"Synced successfully: {hirist_updated + status_updates} status updates, {hirist_ingested} new applications tracked.",
        status=run_status,
        channels=outcomes,
    )


# -------------------------------------------------------------
# Background Task Runners (Decoupled Long-Running Execution)
# -------------------------------------------------------------


# Preference-derived Google Jobs queries fan out one-per-location; both caps keep a scheduled
# run bounded (requests are paced and Google rate-limits aggressively).
def _run_gmail_sync_task_runner(
    cancel_token: Optional[CancellationToken] = None,
    progress_cb: Optional[Callable] = None,
) -> Dict[str, Any]:
    """Background Gmail sync runner (issue #84): fetch → classify → apply → OperationLog."""
    db_gen = get_db()
    db = next(db_gen)
    try:
        if cancel_token:
            cancel_token.check()
        result = _execute_gmail_sync(db, progress_cb=progress_cb)
        return {
            "status": result["status"],
            "emails_scanned": result["emails_scanned"],
            "status_updates": result["status_updates"],
            "jobs_found": result["opportunities_added"],
            "pending_email_applications": result["pending_email_applications"],
            "error": result["error"],
        }
    finally:
        db.close()


def _run_sync_all_task_runner(
    cancel_token: Optional[CancellationToken] = None,
    progress_cb: Optional[Callable] = None,
) -> Dict[str, Any]:
    """
    Runs every enabled discovery/status stage serially (single persistent browser profile),
    tolerating partial failures. Disabled channels are skipped entirely.
    """
    from backend.playwright_app import BrowserProfileBusy

    db_gen = get_db()
    db = next(db_gen)
    summary: List[Dict[str, Any]] = []
    try:
        flags = get_feature_flags(db)
        pref = get_user_preferences(db)

        stages: List[tuple] = []
        if flags.get("ats_portals", True):
            stages.append(
                (
                    "ats_portals",
                    "ATS portal scan",
                    lambda cb: _execute_ats_scrape(
                        ScrapeRequest(
                            target_titles=pref.target_titles or None,
                            target_locations=pref.target_country or None,
                            target_cities=pref.target_cities or None,
                            work_mode=pref.work_mode or None,
                        ),
                        db,
                        cancel_token,
                        cb,
                    ),
                )
            )
        if flags.get("google_jobs"):
            stages.append(
                (
                    "google_jobs",
                    "Google Jobs scan",
                    lambda cb: _run_google_jobs_task_runner(
                        cancel_token=cancel_token, progress_cb=cb
                    ),
                )
            )
        if flags.get("linkedin_sync") and has_consent(db, "linkedin_sync"):
            stages.append(
                (
                    "linkedin_sync",
                    "LinkedIn sync",
                    lambda cb: _run_linkedin_sync_task_runner(
                        cancel_token=cancel_token, progress_cb=cb
                    ),
                )
            )

        # Application status sync: only the explicitly enabled candidate-portal sources.
        ext_sources = []
        if is_feature_enabled(db, "ats_hirist"):
            ext_sources.append("hirist")
        if is_feature_enabled(db, "ats_greenhouse"):
            ext_sources.append("greenhouse")
        if is_feature_enabled(db, "ats_lever"):
            ext_sources.append("lever")
        if is_feature_enabled(db, "ats_smartrecruiters"):
            ext_sources.append("smartrecruiters")
        if is_feature_enabled(db, "gmail_sync"):
            ext_sources.append("gmail")
        if ext_sources:
            stages.append(
                (
                    "application_status",
                    "Application status sync",
                    lambda cb: _execute_external_sync(
                        ExternalSyncRequest(
                            sources=ext_sources, max_pages=10, headless=True
                        ),
                        db,
                        cancel_token,
                        cb,
                    ),
                )
            )

        total = max(1, len(stages))

        def _make_adapter(idx: int, stage_label: str):
            def _cb(
                step=None,
                label=None,
                items_found: int = 0,
                errors: int = 0,
                total_steps=None,
                **_,
            ):
                if progress_cb:
                    progress_cb(
                        step=idx,
                        label=label or stage_label,
                        items_found=items_found,
                        errors=errors,
                        total_steps=total,
                    )

            return _cb

        logger.info(
            f"[SyncAll] Starting one-shot sync with {len(stages)} enabled stage(s)."
        )
        for i, (key, label, fn) in enumerate(stages, start=1):
            if cancel_token:
                cancel_token.check()
            if progress_cb:
                progress_cb(step=i, label=f"Starting: {label}", total_steps=total)
            try:
                result = fn(_make_adapter(i, label))
                # Runners report "I did not run" through a `skipped` key rather than by raising
                # (consent missing, cooldown active, profile busy, scan already in progress).
                # Counting those as completed made a green run out of channels that never ran.
                skipped_reason = result.get("skipped") if isinstance(result, dict) else None
                if skipped_reason:
                    summary.append(
                        {
                            "stage": key,
                            "status": "skipped",
                            "detail": f"skipped: {skipped_reason}",
                        }
                    )
                    logger.info(f"[SyncAll] Stage '{key}' skipped: {skipped_reason}")
                else:
                    summary.append(
                        {"stage": key, "status": "completed", "detail": str(result)[:300]}
                    )
                    logger.info(f"[SyncAll] Stage '{key}' completed: {str(result)[:200]}")
            except BrowserProfileBusy as bpb:
                summary.append(
                    {
                        "stage": key,
                        "status": "skipped",
                        "detail": f"browser profile busy: {bpb}",
                    }
                )
                logger.info(f"[SyncAll] Stage '{key}' skipped: {bpb}")
            except InterruptedError:
                raise
            except Exception as stage_err:
                summary.append(
                    {"stage": key, "status": "failed", "detail": str(stage_err)[:300]}
                )
                logger.warning(f"[SyncAll] Stage '{key}' failed: {stage_err}")
                # A stage that tripped a flush leaves the shared session in PendingRollbackError
                # state, which would fail the run's own bookkeeping and report the whole task as
                # FAILED even though the other stages ran.
                try:
                    db.rollback()
                except Exception as rollback_err:  # noqa: BLE001 - recovery is best-effort
                    logger.warning(
                        f"[SyncAll] Rollback after stage '{key}' failed: {rollback_err}"
                    )

        completed = sum(1 for s in summary if s["status"] == "completed")
        skipped = sum(1 for s in summary if s["status"] == "skipped")
        failed = sum(1 for s in summary if s["status"] == "failed")
        status = "success" if failed == 0 else "partial"
        op_status = "Completed" if failed == 0 else "Partial"

        db.add(
            OperationLog(
                operation_type="sync_all",
                status=op_status,
                summary=f"Sync All: {completed} completed, {skipped} skipped, {failed} failed (of {len(stages)} enabled stages).",
                details_json=sanitize_log_details(summary),
                jobs_count=completed,
                prompt_tokens=0,
                completion_tokens=0,
            )
        )
        try:
            db.commit()
        except Exception as log_err:  # noqa: BLE001 - the stage summary is the deliverable
            logger.warning(f"[SyncAll] Failed to persist sync_all OperationLog: {log_err}")
            try:
                db.rollback()
            except Exception:  # noqa: BLE001 - recovery is best-effort
                pass

        if progress_cb:
            progress_cb(
                step=total,
                label=f"Sync All finished: {completed} completed, {skipped} skipped, {failed} failed.",
                total_steps=total,
            )

        return {
            "status": status,
            "completed": completed,
            "skipped": skipped,
            "failed": failed,
            "stages": summary,
        }
    finally:
        db.close()


# -------------------------------------------------------------
# Background Tasks & Queue API Endpoints
# -------------------------------------------------------------


class SubmitTaskRequest(BaseModel):
    queries: Optional[List[str]] = None
    location: Optional[str] = "India"
    use_playwright: Optional[bool] = False


class TaskResponse(BaseModel):
    task_id: str
    status: str
    message: str


class RescoreRequest(BaseModel):
    limit: int = Field(default=200, ge=1, le=2000)
    # Scope (issue #57): only rows scored against a different resume version, or exactly these ids.
    stale_only: bool = False
    job_ids: Optional[List[int]] = None


class ScoreAuditRequest(BaseModel):
    apply: bool = False
    allow_drops: bool = False
    provider: Optional[str] = None


class ScoreAuditRequirementOut(BaseModel):
    text: str
    category: str
    status: str


class ScoreAuditResponse(BaseModel):
    job_id: int
    cached: bool = False
    applied: bool = False
    score_before: float = 0.0
    score_after: float = 0.0
    band_before: str = ""
    band_after: str = ""
    must_coverage_before: float = 0.0
    must_coverage_after: float = 0.0
    rounds: int = 0
    converged: bool = False
    requirements: List[ScoreAuditRequirementOut] = []
    removed: List[str] = []
    flagged: List[str] = []
    relabelled: List[str] = []
    missing_requirements: List[str] = []
    ungrounded_claims: List[str] = []
    score_opinion: str = "agree"
    critique: str = ""
    confidence: float = 0.0


def _score_audit_response(job_id, original, result, cached, applied) -> ScoreAuditResponse:
    """Shape a validate_score() result for the API, diffing before/after requirements."""
    corrected = result["assessment"]
    after_texts = {r.text for r in corrected.requirements}
    removed = [r.text for r in original.requirements if r.text not in after_texts]
    flagged = [
        v.text
        for v in (getattr(result.get("audit"), "requirements", None) or [])
        if v.verdict == "not_a_requirement"
    ]
    before_by_text = {r.text: r for r in original.requirements}
    relabelled = [
        r.text
        for r in corrected.requirements
        if (b := before_by_text.get(r.text)) is not None
        and (b.category != r.category or b.status != r.status)
    ]
    audit = result.get("audit")
    return ScoreAuditResponse(
        job_id=job_id,
        cached=cached,
        applied=applied,
        score_before=result["score_before"],
        score_after=result["score_after"],
        band_before=result["before"]["band"],
        band_after=result["after"]["band"],
        must_coverage_before=result["before"]["must_coverage"],
        must_coverage_after=result["after"]["must_coverage"],
        rounds=result.get("rounds", 0),
        converged=bool(result.get("converged")),
        requirements=[
            ScoreAuditRequirementOut(text=r.text, category=r.category, status=r.status)
            for r in corrected.requirements
        ],
        removed=removed,
        flagged=flagged,
        relabelled=relabelled,
        missing_requirements=list(getattr(audit, "missing_requirements", []) or []),
        ungrounded_claims=[
            g.claim for g in (getattr(audit, "groundedness", []) or []) if not g.grounded
        ],
        score_opinion=getattr(audit, "score_opinion", "agree"),
        critique=getattr(audit, "critique", ""),
        confidence=float(getattr(audit, "confidence", 0.0) or 0.0),
    )


@app.post("/api/jobs/{job_id}/score-audit", response_model=ScoreAuditResponse)
def audit_job_score(
    job_id: int,
    req: Optional[ScoreAuditRequest] = None,
    db: Session = Depends(get_db),
):
    """On-demand judge audit of a job's deterministic score (issue #60).

    The judge never scores: it validates the requirement set and the narrative claims, and the
    deterministic engine re-scores whatever survives. ``apply=false`` (the default) reports the
    corrected score without touching the stored one.
    """
    if not is_feature_enabled(db, "score_judge"):
        raise HTTPException(
            status_code=403,
            detail="The scoring judge is off. Enable 'score_judge' in Settings -> Features.",
        )
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    active_resume = (
        db.query(Resume)
        .filter(Resume.is_active == True)
        .order_by(Resume.created_at.desc())
        .first()
    )
    if not active_resume:
        raise HTTPException(
            status_code=400, detail="Upload a resume before auditing a score."
        )

    from backend.match_classifier import build_deterministic_assessment
    from backend.score_judge import (
        ScoreAudit,
        apply_audit,
        assessment_hash,
        rescore,
        validate_score,
    )

    description = job.description or ""
    resume_text = decrypt_data(active_resume.content_encrypted)
    parsed_resume = _parsed_resume_of(active_resume)
    assessment = build_deterministic_assessment(
        description, parsed_resume, resume_text=resume_text, job_title=job.title or ""
    )
    if not assessment.requirements:
        raise HTTPException(
            status_code=422,
            detail="No requirements could be extracted from this job description, so there is nothing to audit.",
        )

    config = ScoringConfig.from_mapping(get_scoring_config(db))
    ahash = assessment_hash(assessment)
    record = (
        db.query(ScoreAuditRecord)
        .filter(
            ScoreAuditRecord.job_id == job.id,
            ScoreAuditRecord.assessment_hash == ahash,
        )
        .first()
    )
    apply_changes = bool(req and req.apply)
    cached = False
    result = None

    if record and record.verdict_json:
        try:
            audit = ScoreAudit.model_validate(json.loads(record.verdict_json))
        except Exception:
            audit = None
        if audit is not None:
            corrected = apply_audit(assessment, audit)
            before = rescore(assessment, parsed_resume, description, job.title or "", config)
            after = rescore(corrected, parsed_resume, description, job.title or "", config)
            result = {
                "audit": audit,
                "assessment": corrected,
                "score_before": before["score"],
                "score_after": after["score"],
                "before": before,
                "after": after,
                "rounds": 0,
                "converged": True,
            }
            cached = True

    if result is None:
        strengths, gaps, partials = _derive_strengths_gaps(assessment)
        claims = (strengths[:4] + partials[:4] + gaps[:4])[:12]
        result = validate_score(
            assessment,
            parsed_resume,
            description,
            resume_text=resume_text,
            job_title=job.title or "",
            claims=claims,
            provider=req.provider if req else None,
            config=config,
            allow_drops=bool(req and req.allow_drops),
        )
        audit = result.get("audit")
        if audit is None:
            raise HTTPException(
                status_code=502,
                detail="The scoring judge could not run (no LLM responded). The stored score is unchanged.",
            )
        if record is None:
            record = ScoreAuditRecord(
                job_id=job.id,
                assessment_hash=ahash,
                verdict_json=audit.model_dump_json(),
                score_before=result["score_before"],
                score_after=result["score_after"],
                applied=False,
            )
            db.add(record)
        else:
            record.verdict_json = audit.model_dump_json()
            record.score_before = result["score_before"]
            record.score_after = result["score_after"]
        db.commit()

    if apply_changes:
        after = result["after"]
        job.match_score = after["score"]
        job.match_band = after["band"]
        job.apply_recommendation = after["recommendation"]
        job.score_method = "classifier_v1+judge"
        job.match_scored = True
        job.match_scored_at = datetime.datetime.now(datetime.timezone.utc)
        for _field, _value in _score_provenance(db).items():
            setattr(job, _field, _value)
        s, g, p = _derive_strengths_gaps(result["assessment"])
        job.match_strengths_json = json.dumps(s)
        job.match_gaps_json = json.dumps(g)
        job.match_partials_json = json.dumps(p)
        job.match_analysis = _match_analysis_text(result["assessment"], None)
        if record:
            record.applied = True
        db.commit()

    return _score_audit_response(job.id, assessment, result, cached, apply_changes)


def _apply_scoring_result(job: Job, result, provenance: dict) -> None:
    """Write a ScoringResult onto a job.

    ``provenance`` must be the resume version the result was computed against, captured when the
    resume text was loaded. Re-reading the active resume here would stamp a score with a different
    version if the user switches resumes during a long bulk run (issue #57 review).
    """
    job.match_score = result.score
    job.match_analysis = result.analysis
    job.match_scored = True
    job.score_method = result.method
    job.match_scored_at = result.timestamp
    for field, value in provenance.items():
        setattr(job, field, value)
    job.match_band = result.band
    job.apply_recommendation = result.recommendation
    job.match_strengths_json = json.dumps(result.strengths)
    job.match_gaps_json = json.dumps(result.gaps)
    job.match_partials_json = json.dumps(result.partials)


@app.post("/api/jobs/{job_id}/rescore", response_model=JobOut)
def rescore_job(job_id: int, db: Session = Depends(get_db)):
    """Re-score a single job synchronously (issue #57).

    This is the primitive lazy re-score needs: deterministic engine first, with the same bounded
    LLM fallback discovery uses only when the deterministic path yields no requirements. Idempotent
    — the engine is deterministic, so two tabs re-scoring the same job write the same values.
    """
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    active = _active_resume_row(db)
    if not active:
        raise HTTPException(
            status_code=400, detail="Upload a resume before re-scoring a job."
        )

    resume_text = decrypt_data(active.content_encrypted)
    provenance = _score_provenance(db)
    parsed_resume = _parsed_resume_of(active)
    description = (job.description or job.title or "").strip()
    result = _score_job_with_engine(
        db,
        resume_text,
        description,
        parsed_resume=parsed_resume,
        job_title=job.title or "",
    )
    if not result.scored:
        raise HTTPException(
            status_code=409,
            detail="This job could not be scored (no usable description or no extractable requirements).",
        )

    _apply_scoring_result(job, result, provenance)
    db.commit()
    db.refresh(job)
    return _job_out(db, job)


RESCORE_COMMIT_BATCH = 25


def _commit_rescore_batch(db: Session, batch: List[tuple]) -> tuple:
    """Commit one batch of re-scored jobs, tolerating rows deleted mid-run (issue #74).

    ``batch`` is a list of ``(job_id, method)`` for jobs mutated in this session but not yet
    committed. A job deleted by another writer (job pruning, the LinkedIn sync's auto-prune) would
    make its ``UPDATE ... WHERE id=?`` match zero rows and raise ``StaleDataError``, rolling back
    the whole run. Those rows are detected *before* the flush and expunged instead.

    Returns ``(committed, deterministic, llm_fallback, deleted, failed)``.
    """
    pending_ids = [job_id for job_id, _method in batch]
    # no_autoflush: the existence check must not flush the pending UPDATEs first, or the stale row
    # raises exactly the error this helper exists to avoid.
    with db.no_autoflush:
        existing = {
            row[0]
            for row in db.query(Job.id).filter(Job.id.in_(pending_ids)).all()
        }

    deleted = 0
    for job_id, _method in batch:
        if job_id in existing:
            continue
        deleted += 1
        obj = db.identity_map.get((Job, job_id))
        if obj is not None:
            db.expunge(obj)

    try:
        db.commit()
    except Exception as commit_err:
        # Bounded loss: roll back this batch only; the caller keeps going with the next one.
        db.rollback()
        logger.warning(
            f"match_rescore: batch commit failed for {len(batch)} job(s), continuing: {commit_err}"
        )
        return 0, 0, 0, deleted, len(batch) - deleted

    committed = len(batch) - deleted
    deterministic = sum(1 for job_id, method in batch if job_id in existing and method == "classifier_v1")
    llm_fallback = committed - deterministic
    return committed, deterministic, llm_fallback, deleted, 0


def _run_match_rescore_task_runner(
    cancel_token: Optional[CancellationToken] = None,
    progress_cb: Optional[Callable] = None,
    limit: int = 200,
    stale_only: bool = False,
    job_ids: Optional[List[int]] = None,
) -> Dict[str, Any]:
    """Background re-score of existing jobs (deterministic engine first, bounded LLM fallback)."""
    db = next(get_db())
    # Batched commits (issue #74): expire-on-commit would re-query every loaded row after each
    # batch, and a row deleted by another writer would then raise on attribute access.
    db.expire_on_commit = False
    rescored = failed = deleted_skipped = 0
    deterministic = llm_fallback = 0
    started = datetime.datetime.now(datetime.timezone.utc)
    try:
        active = (
            db.query(Resume)
            .filter(Resume.is_active == True)
            .order_by(Resume.created_at.desc())
            .first()
        )
        if not active:
            return {"rescored": 0, "failed": 0, "error": "no_active_resume"}

        resume_text = decrypt_data(active.content_encrypted)
        provenance = _score_provenance(db)
        parsed_resume = None
        if active.parsed_json_encrypted:
            try:
                parsed_resume = json.loads(decrypt_data(active.parsed_json_encrypted))
            except Exception:
                parsed_resume = None

        # Ensure the active resume is chunk-indexed so evidence retrieval has data.
        from backend.database import ResumeChunk

        if (
            db.query(ResumeChunk).filter(ResumeChunk.resume_id == active.id).count()
            == 0
        ):
            try:
                index_resume_chunks(db, active)
            except Exception as idx_err:
                logger.debug(f"Resume chunk indexing skipped: {idx_err}")

        query = db.query(Job).filter(Job.status.notin_(["Rejected", "Not Interested"]))
        if job_ids is not None:
            query = query.filter(Job.id.in_(job_ids))
        if stale_only:
            # Rows scored against a different resume version: stale (that version still exists)
            # and deleted (it does not). NULL provenance is "unknown -> current" (#56) and is
            # deliberately left alone.
            query = query.filter(
                Job.scored_resume_fingerprint.isnot(None),
                Job.scored_resume_fingerprint != active.fingerprint,
            )
        jobs = query.order_by(Job.created_at.desc()).limit(limit).all()

        batch: List[tuple] = []

        def _flush_batch() -> None:
            nonlocal rescored, failed, deleted_skipped, deterministic, llm_fallback
            if not batch:
                return
            committed, det, llm, deleted, batch_failed = _commit_rescore_batch(db, batch)
            rescored += committed
            deterministic += det
            llm_fallback += llm
            deleted_skipped += deleted
            failed += batch_failed
            batch.clear()

        for idx, job in enumerate(jobs):
            if cancel_token:
                cancel_token.check()
            description = (job.description or job.title or "").strip()
            try:
                # no_autoflush: the engine issues read queries; autoflushing here would flush the
                # pending score updates mid-batch and resurrect the StaleDataError this fixes.
                with db.no_autoflush:
                    result = _score_job_with_engine(
                        db,
                        resume_text,
                        description,
                        parsed_resume=parsed_resume,
                        job_title=job.title or "",
                    )
                if not result.scored:
                    failed += 1
                else:
                    _apply_scoring_result(job, result, provenance)
                    batch.append((job.id, result.method))
            except Exception as job_err:
                failed += 1
                logger.debug(f"Re-score skipped for job {job.id}: {job_err}")

            if len(batch) >= RESCORE_COMMIT_BATCH:
                _flush_batch()

            if progress_cb:
                progress_cb(
                    step=idx + 1,
                    label=(
                        f"Re-scored {rescored} job(s), {failed} skipped, "
                        f"{deleted_skipped} removed."
                    ),
                    items_found=rescored,
                    total_steps=len(jobs),
                )
            if idx < len(jobs) - 1:
                time.sleep(0.3)

        _flush_batch()

        logger.info(
            f"match_rescore: deterministic={deterministic} llm_fallback={llm_fallback} "
            f"unscored={failed} deleted={deleted_skipped} total={len(jobs)}"
        )

        try:
            db.add(
                OperationLog(
                    operation_type="match_rescore",
                    status="Completed" if not failed else "Partial",
                    summary=(
                        f"Re-scored {rescored} job(s): {deterministic} deterministic, "
                        f"{llm_fallback} LLM fallback; {failed} skipped, "
                        f"{deleted_skipped} removed during the run."
                    ),
                    details_json=sanitize_log_details(
                        {
                            "rescored": rescored,
                            "deterministic": deterministic,
                            "llm_fallback": llm_fallback,
                            "failed": failed,
                            "deleted_skipped": deleted_skipped,
                            "batch_size": RESCORE_COMMIT_BATCH,
                            "limit": limit,
                            "started_at": started.isoformat(),
                        }
                    ),
                    jobs_count=rescored,
                )
            )
            db.commit()
        except Exception as op_err:
            logger.warning(
                f"Failed to persist OperationLog for match re-score: {op_err}"
            )

        return {
            "rescored": rescored,
            "failed": failed,
            "jobs_found": rescored,
            "deterministic": deterministic,
            "llm_fallback": llm_fallback,
            "deleted_skipped": deleted_skipped,
        }
    finally:
        db.close()


@app.post(
    "/api/match/rescore",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=TaskResponse,
)
def dispatch_match_rescore_task(payload: Optional[RescoreRequest] = None):
    """Dispatches a non-blocking re-score of existing jobs with the current engine."""
    limit = payload.limit if payload else 200
    stale_only = bool(payload and payload.stale_only)
    job_ids = list(payload.job_ids) if payload and payload.job_ids is not None else None
    if stale_only and job_ids is not None:
        raise HTTPException(
            status_code=400,
            detail="Choose either stale_only or job_ids, not both.",
        )
    if job_ids is not None:
        if not job_ids:
            raise HTTPException(status_code=400, detail="job_ids is empty; nothing to re-score.")
        if len(job_ids) > 2000:
            raise HTTPException(status_code=400, detail="At most 2000 job_ids per request.")
        # The explicit selection is the whole scope: do not let the default limit truncate it.
        limit = len(job_ids)
    scope = (
        f"{len(job_ids)} selected job(s)"
        if job_ids is not None
        else ("stale job(s)" if stale_only else f"up to {limit} job(s)")
    )
    task_id = task_engine.submit_task(
        task_type="match_rescore",
        task_name=f"Re-score {scope}",
        fn=_run_match_rescore_task_runner,
        total_steps=1,
        limit=limit,
        stale_only=stale_only,
        job_ids=job_ids,
    )
    return TaskResponse(
        task_id=task_id,
        status="QUEUED",
        message=f"Match re-score queued for {scope}.",
    )


@app.post(
    "/api/tasks/discovery/google-jobs",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=TaskResponse,
)
def dispatch_google_jobs_task(payload: Optional[SubmitTaskRequest] = None):
    """Dispatches a non-blocking Google for Jobs multi-query discovery scan in the background."""
    payload = payload or SubmitTaskRequest()
    queries_list = payload.queries or load_google_searches()
    total = len(queries_list) if queries_list else 6

    task_id = task_engine.submit_task(
        task_type="google_jobs_scan",
        task_name=f"Google for Jobs Scan ({total} queries)",
        fn=_run_google_jobs_task_runner,
        queries=payload.queries,
        location=payload.location or "India",
        use_playwright=payload.use_playwright or False,
        total_steps=total,
    )

    return TaskResponse(
        task_id=task_id,
        status="QUEUED",
        message=f"Google Jobs discovery scan queued with {total} followed queries.",
    )


@app.post(
    "/api/tasks/sync/linkedin",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=TaskResponse,
)
def dispatch_linkedin_sync_task(db: Session = Depends(get_db)):
    """Dispatches a non-blocking LinkedIn saved jobs & alerts synchronization."""
    if not is_feature_enabled(db, "linkedin_sync"):
        raise HTTPException(
            status_code=403,
            detail="LinkedIn automation is disabled. Enable it under Settings → Discovery Features.",
        )
    if not has_consent(db, "linkedin_sync"):
        raise HTTPException(
            status_code=403,
            detail="Consent required for LinkedIn automation. Acknowledge the risk in Settings → Discovery Features.",
        )
    task_id = task_engine.submit_task(
        task_type="linkedin_sync",
        task_name="LinkedIn Saved Jobs & Alerts Sync",
        fn=_run_linkedin_sync_task_runner,
        total_steps=3,
    )
    return TaskResponse(
        task_id=task_id,
        status="QUEUED",
        message="LinkedIn sync task dispatched in background.",
    )


@app.post(
    "/api/tasks/sync/gmail",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=TaskResponse,
)
def dispatch_gmail_sync_task(db: Session = Depends(get_db)):
    """Dispatches a non-blocking Gmail sync so it shows up in the task queue and SSE feed."""
    if not is_feature_enabled(db, "gmail_sync"):
        raise HTTPException(
            status_code=403,
            detail="Gmail sync is disabled. Enable it under Settings → Discovery Features.",
        )
    if not is_gmail_configured():
        raise HTTPException(
            status_code=400,
            detail="Gmail is not connected. Connect it in Settings first.",
        )
    task_id = task_engine.submit_task(
        task_type="gmail_sync",
        task_name="Gmail Sync",
        fn=_run_gmail_sync_task_runner,
        total_steps=4,
    )
    return TaskResponse(
        task_id=task_id,
        status="QUEUED",
        message="Gmail sync task dispatched in background.",
    )


@app.post(
    "/api/tasks/sync/all",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=TaskResponse,
)
def dispatch_sync_all_task(db: Session = Depends(get_db)):
    """Dispatches a one-shot, feature-flag-aware sync across all enabled discovery & status channels."""
    flags = get_feature_flags(db)
    enabled: List[str] = []
    if flags.get("ats_portals", True):
        enabled.append("ATS portals")
    if flags.get("google_jobs"):
        enabled.append("Google Jobs")
    if flags.get("linkedin_sync") and has_consent(db, "linkedin_sync"):
        enabled.append("LinkedIn")
    if (
        flags.get("ats_hirist")
        or flags.get("ats_greenhouse")
        or flags.get("ats_lever")
        or flags.get("ats_smartrecruiters")
        or flags.get("gmail_sync")
    ):
        enabled.append("Application statuses")

    task_id = task_engine.submit_task(
        task_type="sync_all",
        task_name=f"Sync All ({len(enabled)} enabled)",
        fn=_run_sync_all_task_runner,
        total_steps=max(1, len(enabled)),
    )
    return TaskResponse(
        task_id=task_id,
        status="QUEUED",
        message=f"Sync All queued for {len(enabled)} channel(s): {', '.join(enabled)}.",
    )


# -------------------------------------------------------------
# Automation Scheduler Endpoints
# -------------------------------------------------------------


# -------------------------------------------------------------
# Frontend Static Files Mount
# -------------------------------------------------------------
frontend_dir = BASE_DIR / "frontend"

if frontend_dir.exists():
    app.mount("/", StaticFiles(directory=str(frontend_dir), html=True), name="static")
