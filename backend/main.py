import asyncio
import datetime
import json
import logging
import os
import re
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

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
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.security import APIKeyHeader
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.orm import Session

from backend.ai_helper import TASK_PROFILES, TaskComplexity, get_model_for_tier
from backend.config import (
    DEEP_TIER_MODEL,
    DEFAULT_LLM_PROVIDER,
    ENABLE_DYNAMIC_MODEL_ROUTING,
    FAST_TIER_MODEL,
    STANDARD_TIER_MODEL,
)
from backend.matchers import detect_foreign_restriction
from backend.playwright_app import BrowserProfileBusy, search_linkedin_talent_partners

logger = logging.getLogger("api")

from playwright.sync_api import sync_playwright

from backend.ai_helper import (
    analyze_job_match,
    batch_autofill_essay_answers,
    check_semantic_dismissal,
    detect_active_llm_provider,
    extract_job_skills_and_alignment,
    generate_and_cache_essay_answer,
    generate_consolidated_application_package,
    generate_embeddings,
    generate_grounded_anti_slop_pitch,
    generate_interview_questions,
    generate_text,
    index_dismissed_job_pattern,
    parse_resume_to_json,
    search_qa_memory,
    token_tracker,
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
    LINKEDIN_AUTOMATION_PACING_SECONDS,
    clear_gmail_settings,
    decrypt_data,
    encrypt_data,
    get_gmail_settings,
    get_llm_credentials,
    is_cloud_fallback_allowed,
    is_gmail_configured,
    is_valid_api_key,
    save_gmail_settings,
    save_llm_settings,
)
from backend.database import (
    CONSENT_REQUIRED_FEATURES,
    CONSENT_VERSION,
    DEFAULT_FEATURE_FLAGS,
    ApplicationEvent,
    ApplicationQuestionAnswer,
    Company,
    CustomFormField,
    DismissedJobPattern,
    Job,
    OperationLog,
    Resume,
    get_consents,
    get_db,
    get_feature_flags,
    get_user_preferences,
    has_consent,
    init_db,
    is_company_excluded,
    is_feature_enabled,
    record_consent,
)
from backend.llm_queue import LLMPriority, llm_queue
from backend.matchers import check_title_match
from backend.observability import setup_observability, tail_log_file
from backend.parser import (
    clean_job_description,
    parse_resume_document,
    split_candidate_name,
)
from backend.playwright_app import (
    fetch_google_jobs_followed_queries_playwright,
    fetch_google_jobs_playwright,
    get_persistent_browser_context,
    launch_assisted_application_session,
    scan_and_sync_hirist_applications,
    scan_and_sync_linkedin_saved_jobs,
    scan_linkedin_job_alerts,
    scan_linkedin_job_recommendations,
    sync_email_application_events,
)
from backend.preference_filter import (
    PreferenceCriteria,
    PreferenceFilter,
    check_location_match,
    check_workmode_match,
)
from backend.scheduler import ScheduledJobConfig, scheduler
from backend.scraper import (
    evaluate_job_due_diligence,
    extract_opportunity_company_and_title,
    extract_portal_info_from_job_url,
    fetch_google_jobs,
    fetch_jobs_via_jobspy,
    find_talent_partners,
    gather_company_intelligence,
    probe_greenhouse_job_active,
    probe_lever_job_active,
    probe_smartrecruiters_job_active,
    scrape_ashby_jobs,
    scrape_custom_jobs,
    scrape_greenhouse_jobs,
    scrape_lever_jobs,
    scrape_smartrecruiters_jobs,
    scrape_uber_jobs,
    scrape_workable_jobs,
    scrape_workday_jobs,
)
from backend.task_engine import CancellationToken, TaskProgress, task_engine

# Initialize Observability, DB tables, and Periodic Scheduler on startup
setup_observability()
init_db()
scheduler.start()

app = FastAPI(
    title="Job Alert Agent API",
    description="Autonomous job hunting, application tracking, resume ATS tailoring, and browser assistance.",
    version="1.0.0",
)


@app.on_event("startup")
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


@app.middleware("http")
async def request_observability_middleware(request: Request, call_next):
    """
    HTTP Request Observability & Correlation Middleware.
    Assigns unique X-Request-ID, logs request lifecycle, status codes, and latency in milliseconds.
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


@app.middleware("http")
async def security_firewall_middleware(request: Request, call_next):
    """
    Zero-Trust Security Firewall:
    - Automatically allows public documentation, health endpoints, and static assets.
    - Requires verified X-API-Key or verified same-origin session for all REST API endpoints.
    - Blocks cross-site unauthorized requests from arbitrary websites.
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


def sanitize_log_details(details: Optional[Union[str, dict, list]]) -> Optional[str]:
    """
    Redacts PII/credentials before persisting to OperationLog. Always returns valid JSON text
    (or None), so callers can safely ``json.loads`` the stored value.
    """
    if not details:
        return None
    try:
        raw = details if isinstance(details, str) else json.dumps(details)
        # Redact common credentials/tokens if present
        raw = re.sub(
            r'("?(?:api_key|token|password|secret|refresh_token|client_secret|access_token)"?\s*:\s*)"[^"]+"',
            r'\1"[REDACTED]"',
            raw,
            flags=re.IGNORECASE,
        )
        # Redact email addresses in logs
        raw = re.sub(
            r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+", "[REDACTED_EMAIL]", raw
        )
        return raw
    except Exception:
        try:
            return json.dumps(str(details))
        except Exception:
            return None


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
    "Interview",
    "Offered",
    "Rejected",
    "Not Interested",
]


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
        description="To Apply, Shortlisted, Applied, Screening, Interview, Offered, Rejected, Not Interested",
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
    cover_letter_draft: Optional[str] = None
    tailored_resume_points: Optional[str] = None
    cold_message_draft: Optional[str] = None
    is_ghost_job: bool
    repost_count: int
    submission_confirmed: bool = False
    match_scored: bool = False
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


class ScrapeRequest(BaseModel):
    company_id: Optional[int] = None
    target_titles: Optional[str] = None
    target_locations: Optional[str] = None
    target_cities: Optional[str] = None
    work_mode: Optional[str] = None


class PruneJobsRequest(BaseModel):
    target_titles: Optional[str] = None
    target_locations: Optional[str] = None
    target_cities: Optional[str] = None
    work_mode: Optional[str] = None


class ScrapeResponse(BaseModel):
    jobs_found: int
    jobs_added: int
    companies_updated: int
    message: str


class ApplyResponse(BaseModel):
    job_id: int
    status: str
    message: str


class BulkApplyRequest(BaseModel):
    job_ids: List[int] = Field(
        ..., description="List of job IDs to apply for in browser tabs"
    )


class BulkApplyResponse(BaseModel):
    message: str
    applied_job_ids: List[int]


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
    details: List[str] = []
    message: str = ""


LATEST_SCRAPE_AUDIT: List[dict] = []


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
    resume_text: Optional[str] = Field(default=None, max_length=60000)


class MatchAnalyzeResponse(BaseModel):
    match_score: Optional[float] = Field(
        default=None,
        description="ATS alignment confidence score (0.0 to 100.0); None when the analysis failed",
    )
    strengths: List[str] = Field(default_factory=list)
    gaps: List[str] = Field(default_factory=list)
    feedback: str = ""
    resume_available: bool = True
    analysis_source: str = Field(
        default="llm",
        description="'llm' for AI analysis, 'error' when no reliable score could be produced",
    )
    error: Optional[str] = Field(
        default=None,
        description="Human-readable failure reason; no score is shown when set",
    )


GOOGLE_SEARCHES_FILE = BASE_DIR / "data" / "google_searches.json"
DEFAULT_GOOGLE_SEARCHES = [
    "Staff Software Engineer remote",
    "Senior Backend Engineer distributed systems",
    "Principal Software Engineer",
    "Senior Data Engineer Python SQL",
]


def normalize_google_query(query: str) -> str:
    """
    Cleans a Google Jobs search/alert query:
    - collapses consecutive duplicate words (e.g. "jobs in India India" -> "jobs in India")
    - drops a trailing word that already appeared earlier (e.g. "Acme india jobs India" -> "Acme india jobs")
    Never renames the user's alerts on Google; only what we store/search with.
    """
    if not query:
        return query
    s = " ".join(str(query).split())
    s = re.sub(r"\b(\w+)(?:\s+\1\b)+", r"\1", s, flags=re.IGNORECASE)
    words = s.split()
    if len(words) >= 2:
        earlier = {w.lower() for w in words[:-1]}
        if words[-1].lower() in earlier:
            words = words[:-1]
            s = " ".join(words)
    return s.strip()


def _normalize_google_queries(searches: List[str]) -> List[str]:
    clean: List[str] = []
    seen = set()
    for q in searches:
        n = normalize_google_query(str(q))
        key = n.lower()
        if n and key not in seen:
            seen.add(key)
            clean.append(n)
    return clean


def load_google_searches() -> List[str]:
    try:
        if GOOGLE_SEARCHES_FILE.exists():
            with open(GOOGLE_SEARCHES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list) and data:
                    return _normalize_google_queries(
                        [str(q) for q in data if str(q).strip()]
                    )
    except Exception as e:
        logger.debug(f"Could not load google_searches.json: {e}")
    return list(DEFAULT_GOOGLE_SEARCHES)


def save_google_searches(searches: List[str]) -> List[str]:
    try:
        GOOGLE_SEARCHES_FILE.parent.mkdir(parents=True, exist_ok=True)
        clean = _normalize_google_queries(searches)
        with open(GOOGLE_SEARCHES_FILE, "w", encoding="utf-8") as f:
            json.dump(clean, f, indent=2)
        return clean
    except Exception as e:
        logger.debug(f"Could not save google_searches.json: {e}")
        return searches


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
    dismissed_filtered: int = 0
    queries_scanned: List[str] = []
    per_query_breakdown: List[dict] = []
    message: str


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


class OperationLogOut(BaseModel):
    id: int
    operation_type: str
    status: str
    summary: str
    details_json: Optional[str] = None
    jobs_count: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    created_at: datetime.datetime

    model_config = ConfigDict(from_attributes=True)


class QAMemoryIn(BaseModel):
    question_text: str
    answer_text: str
    category: Optional[str] = "general"


class QAMemoryOut(BaseModel):
    id: int
    question_text: str
    answer_text: str
    category: str
    use_count: int
    created_at: datetime.datetime
    updated_at: datetime.datetime

    model_config = ConfigDict(from_attributes=True)


class QASearchRequest(BaseModel):
    query_question: str
    top_k: Optional[int] = 3
    min_similarity: Optional[float] = 0.65


class QAResolveRequest(BaseModel):
    question_text: str
    company_name: Optional[str] = None
    job_title: Optional[str] = None
    auto_cache: bool = True
    threshold: float = 0.85
    provider: Optional[str] = None


class QABatchAutofillRequest(BaseModel):
    questions: List[str]
    company_name: Optional[str] = None
    job_title: Optional[str] = None
    auto_cache: bool = True
    threshold: float = 0.85
    provider: Optional[str] = None


class GroundedPitchRequest(BaseModel):
    question_text: str
    company_name: Optional[str] = None
    job_title: Optional[str] = None
    job_description: Optional[str] = None
    auto_cache: bool = True
    threshold: float = 0.85
    provider: Optional[str] = None


class FormFieldItem(BaseModel):
    field_name: Optional[str] = None
    field_id: Optional[str] = None
    field_label: Optional[str] = None
    field_type: Optional[str] = "text"
    is_recognized: bool = False
    suggested_category: Optional[str] = "unknown"


class FormTelemetryRequest(BaseModel):
    domain: str
    ats_type: Optional[str] = "custom"
    url: Optional[str] = None
    fields: List[FormFieldItem]


class FormFieldOut(BaseModel):
    id: int
    ats_type: str
    domain: str
    field_name: Optional[str] = None
    field_id: Optional[str] = None
    field_label: Optional[str] = None
    field_type: str
    is_recognized: bool
    classified_category: str
    occurrence_count: int
    created_at: datetime.datetime

    model_config = ConfigDict(from_attributes=True)


class DismissedPatternOut(BaseModel):
    id: int
    title: str
    company_name: Optional[str] = None
    description: Optional[str] = None
    reason: Optional[str] = None
    created_at: datetime.datetime

    model_config = ConfigDict(from_attributes=True)


@app.get("/api/operations/logs", response_model=List[OperationLogOut])
def get_operation_logs(limit: int = 50, db: Session = Depends(get_db)):
    """Retrieves persistent audit history of all sync, scrape, and application operations."""
    return (
        db.query(OperationLog)
        .order_by(OperationLog.created_at.desc())
        .limit(limit)
        .all()
    )


@app.delete("/api/operations/logs")
def clear_operation_logs(db: Session = Depends(get_db)):
    """Clears past operation history logs."""
    deleted = db.query(OperationLog).delete()
    db.commit()
    return {"message": f"Successfully cleared {deleted} operation log record(s)."}


@app.get("/api/logs")
def get_application_logs(
    lines: int = Query(default=100, ge=1, le=1000),
    level: Optional[str] = Query(default=None),
    search: Optional[str] = Query(default=None),
):
    """
    Observability Log Streamer.
    Tails the active rotating structured JSON log file (logs/app.log).
    Supports optional log level filtering (INFO, WARNING, ERROR) and substring search.
    """
    raw_logs = tail_log_file(lines=lines * 2 if (level or search) else lines)
    filtered = raw_logs
    if level:
        lvl_upper = level.upper()
        filtered = [l for l in filtered if l.get("level", "").upper() == lvl_upper]
    if search:
        s_lower = search.lower()
        filtered = [l for l in filtered if s_lower in json.dumps(l).lower()]

    return {"count": len(filtered[:lines]), "logs": filtered[:lines]}


# -------------------------------------------------------------
# Semantic Memory & Question Bank Endpoints
# -------------------------------------------------------------
@app.get("/api/memory/qa", response_model=List[QAMemoryOut])
def get_qa_memory_items(category: Optional[str] = None, db: Session = Depends(get_db)):
    """Retrieves all saved application question-answer memory pairs."""
    query = db.query(ApplicationQuestionAnswer)
    if category and category != "all":
        query = query.filter(ApplicationQuestionAnswer.category == category)
    return query.order_by(
        ApplicationQuestionAnswer.use_count.desc(),
        ApplicationQuestionAnswer.updated_at.desc(),
    ).all()


@app.post(
    "/api/memory/qa", response_model=QAMemoryOut, status_code=status.HTTP_201_CREATED
)
def save_qa_memory_item(payload: QAMemoryIn, db: Session = Depends(get_db)):
    """Saves or updates an application question-answer pair with semantic embedding vector."""
    clean_q = payload.question_text.strip()
    clean_a = payload.answer_text.strip()
    if not clean_q or not clean_a:
        raise HTTPException(
            status_code=400, detail="Both question and answer text are required."
        )

    # Check if exact question already exists
    existing = (
        db.query(ApplicationQuestionAnswer)
        .filter(ApplicationQuestionAnswer.question_text.ilike(clean_q))
        .first()
    )

    emb = generate_embeddings(clean_q)

    if existing:
        existing.answer_text = clean_a
        existing.category = payload.category or existing.category
        existing.embedding = emb
        existing.use_count += 1
        db.commit()
        db.refresh(existing)
        return existing

    new_item = ApplicationQuestionAnswer(
        question_text=clean_q,
        answer_text=clean_a,
        category=payload.category or "general",
        embedding=emb,
        use_count=1,
    )
    db.add(new_item)
    db.commit()
    db.refresh(new_item)
    return new_item


@app.post("/api/memory/qa/search")
def search_qa_memory_endpoint(payload: QASearchRequest, db: Session = Depends(get_db)):
    """Searches the Q&A memory bank using semantic vector similarity."""
    results = search_qa_memory(
        query_question=payload.query_question,
        db=db,
        top_k=payload.top_k or 3,
        min_similarity=payload.min_similarity or 0.65,
    )
    return {"query": payload.query_question, "matches": results}


@app.delete("/api/memory/qa/{qa_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_qa_memory_item(qa_id: int, db: Session = Depends(get_db)):
    """Deletes a saved question-answer memory pair."""
    item = (
        db.query(ApplicationQuestionAnswer)
        .filter(ApplicationQuestionAnswer.id == qa_id)
        .first()
    )
    if not item:
        raise HTTPException(status_code=404, detail="Q&A item not found.")
    db.delete(item)
    db.commit()
    return None


@app.get("/api/memory/dismissed-patterns", response_model=List[DismissedPatternOut])
def get_dismissed_patterns(db: Session = Depends(get_db)):
    """Retrieves all learned negative job patterns for zero-token ignore filtering."""
    return (
        db.query(DismissedJobPattern)
        .order_by(DismissedJobPattern.created_at.desc())
        .all()
    )


@app.delete(
    "/api/memory/dismissed-patterns/{pattern_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_dismissed_pattern(pattern_id: int, db: Session = Depends(get_db)):
    """Removes a learned ignore rule so future matching jobs are no longer auto-filtered."""
    pattern = (
        db.query(DismissedJobPattern)
        .filter(DismissedJobPattern.id == pattern_id)
        .first()
    )
    if not pattern:
        raise HTTPException(status_code=404, detail="Pattern not found.")
    db.delete(pattern)
    db.commit()
    return None


# -------------------------------------------------------------
# Semantic Essay Prompt & Response Caching Endpoints
# -------------------------------------------------------------
@app.post("/api/qa/grounded-pitch")
def generate_grounded_pitch_endpoint(
    payload: GroundedPitchRequest, db: Session = Depends(get_db)
):
    """
    Generates a crisp, authentic, 3-5 sentence application essay / screener pitch.
    Eliminates all AI clichés, grounding the answer in company intelligence and candidate resume evidence.
    """
    active_resume = db.query(Resume).filter(Resume.is_active == True).first()
    resume_text = decrypt_data(active_resume.content_encrypted) if active_resume else ""

    return generate_grounded_anti_slop_pitch(
        question=payload.question_text,
        company_name=payload.company_name or "",
        job_title=payload.job_title or "",
        job_description=payload.job_description or "",
        resume_text=resume_text,
        db=db,
        auto_cache=payload.auto_cache,
        threshold=payload.threshold,
        provider=payload.provider,
    )


@app.post("/api/forms/telemetry")
def ingest_form_telemetry(payload: FormTelemetryRequest, db: Session = Depends(get_db)):
    """
    Ingests and aggregates field detection telemetry from the Chrome extension,
    learning custom form fields across career portals.
    """
    processed = 0
    clean_domain = payload.domain.strip().lower()
    for field in payload.fields:
        existing = (
            db.query(CustomFormField)
            .filter(
                CustomFormField.domain == clean_domain,
                (CustomFormField.field_id == field.field_id)
                | (CustomFormField.field_name == field.field_name)
                | (CustomFormField.field_label == field.field_label),
            )
            .first()
        )

        if existing:
            existing.occurrence_count += 1
            existing.is_recognized = field.is_recognized or existing.is_recognized
            if field.suggested_category and field.suggested_category != "unknown":
                existing.classified_category = field.suggested_category
            existing.updated_at = datetime.datetime.now(datetime.timezone.utc)
        else:
            new_field = CustomFormField(
                ats_type=payload.ats_type or "custom",
                domain=clean_domain,
                field_name=field.field_name,
                field_id=field.field_id,
                field_label=field.field_label,
                field_type=field.field_type or "text",
                is_recognized=field.is_recognized,
                classified_category=field.suggested_category or "unknown",
                occurrence_count=1,
            )
            db.add(new_field)
        processed += 1

    db.commit()
    return {"status": "ok", "processed_count": processed}


@app.get("/api/forms/telemetry", response_model=List[FormFieldOut])
def get_form_telemetry(
    domain: Optional[str] = None,
    unrecognized_only: bool = False,
    db: Session = Depends(get_db),
):
    """
    Returns aggregated custom form fields and learned portal patterns.
    """
    query = db.query(CustomFormField)
    if domain:
        query = query.filter(CustomFormField.domain == domain.strip().lower())
    if unrecognized_only:
        query = query.filter(CustomFormField.is_recognized == False)
    return query.order_by(CustomFormField.occurrence_count.desc()).all()


@app.post("/api/qa/resolve-or-generate")
def resolve_or_generate_qa_endpoint(
    payload: QAResolveRequest, db: Session = Depends(get_db)
):
    """
    Evaluates semantic question against memory cache first (0 tokens if >= threshold).
    Falls back to JIT LLM generation and auto-indexes fresh answer into database.
    """
    active_resume = db.query(Resume).filter(Resume.is_active == True).first()
    resume_text = decrypt_data(active_resume.content_encrypted) if active_resume else ""

    return generate_and_cache_essay_answer(
        question=payload.question_text,
        resume_text=resume_text,
        db=db,
        company_name=payload.company_name or "",
        job_title=payload.job_title or "",
        auto_cache=payload.auto_cache,
        threshold=payload.threshold,
        provider=payload.provider,
    )


@app.post("/api/qa/batch-autofill")
def batch_autofill_qa_endpoint(
    payload: QABatchAutofillRequest, db: Session = Depends(get_db)
):
    """
    Resolves a list of application form essay questions in a single call with mixed cache hits/misses.
    """
    active_resume = db.query(Resume).filter(Resume.is_active == True).first()
    resume_text = decrypt_data(active_resume.content_encrypted) if active_resume else ""

    return batch_autofill_essay_answers(
        questions=payload.questions,
        resume_text=resume_text,
        db=db,
        company_name=payload.company_name or "",
        job_title=payload.job_title or "",
        auto_cache=payload.auto_cache,
        threshold=payload.threshold,
        provider=payload.provider,
    )


@app.get("/api/qa/cache-stats")
def get_qa_cache_stats(db: Session = Depends(get_db)):
    """
    Returns observability metrics for the Semantic Q&A Cache.
    """
    all_qa = db.query(ApplicationQuestionAnswer).all()
    total_cached = len(all_qa)
    reused_count = sum(item.use_count - 1 for item in all_qa if item.use_count > 1)

    categories_breakdown = {}
    for item in all_qa:
        cat = item.category or "general"
        categories_breakdown[cat] = categories_breakdown.get(cat, 0) + 1

    top_reused = sorted(all_qa, key=lambda x: x.use_count, reverse=True)[:5]
    top_reused_data = [
        {
            "id": item.id,
            "question": item.question_text,
            "category": item.category,
            "use_count": item.use_count,
            "updated_at": item.updated_at,
        }
        for item in top_reused
    ]

    return {
        "total_cached_pairs": total_cached,
        "total_inferences_saved": reused_count,
        "categories_breakdown": categories_breakdown,
        "top_reused_questions": top_reused_data,
    }


# -------------------------------------------------------------
# Health & Status Endpoint
# -------------------------------------------------------------
@app.get("/api/health")
def health_check(db: Session = Depends(get_db)):
    """Health check verifying API, Database, and Active LLM Engine readiness."""
    try:
        db.query(Company).count()
        active_llm = detect_active_llm_provider()
        return {"status": "ok", "database": "connected", "active_llm": active_llm}
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Database connectivity failed: {str(e)}",
        )


@app.get("/api/llm/status")
def get_llm_status():
    """Returns detailed status and metadata of the currently active LLM provider."""
    return detect_active_llm_provider()


@app.get("/api/llm/routing-status")
def get_llm_routing_status():
    """Returns dynamic model routing configuration, tier assignments, and task complexity profiles."""

    active_provider = detect_active_llm_provider()
    provider_id = active_provider.get("provider", DEFAULT_LLM_PROVIDER).lower()

    return {
        "dynamic_routing_enabled": ENABLE_DYNAMIC_MODEL_ROUTING,
        "active_provider": active_provider,
        "tier_models": {
            "fast_tier": FAST_TIER_MODEL,
            "standard_tier": STANDARD_TIER_MODEL,
            "deep_tier": DEEP_TIER_MODEL,
        },
        "resolved_provider_tiers": {
            "fast": get_model_for_tier(provider_id, TaskComplexity.FAST),
            "standard": get_model_for_tier(provider_id, TaskComplexity.STANDARD),
            "deep_reasoning": get_model_for_tier(
                provider_id, TaskComplexity.DEEP_REASONING
            ),
        },
        "task_profiles": {
            k: {
                "complexity": v["complexity"].value
                if hasattr(v["complexity"], "value")
                else str(v["complexity"]),
                "temperature": v["temperature"],
                "max_tokens": v["max_tokens"],
                "description": v["description"],
                "top_p": v["top_p"],
            }
            for k, v in TASK_PROFILES.items()
        },
    }


@app.get("/api/llm/queue-status")
def get_llm_queue_status():
    """Returns live metrics, queue depth, active task, and backpressure alerts for the local LLM worker queue."""
    return llm_queue.get_status()


@app.get("/api/llm/token-usage")
def get_llm_token_usage():
    """Returns total prompt tokens, completion tokens, inference counts, and cost/savings analytics in O(1) time."""
    from backend.ai_helper import token_tracker

    return token_tracker.get_stats()


@app.post("/api/llm/token-usage/sync")
def sync_llm_token_usage(db: Session = Depends(get_db)):
    """Incrementally aggregates delta token usage from newer operation logs."""
    from backend.ai_helper import token_tracker

    token_tracker.sync_incremental_db_tokens(db=db)
    return token_tracker.get_stats()


@app.post("/api/llm/token-usage/recalculate")
def recalculate_llm_token_usage(db: Session = Depends(get_db)):
    """Reconstructs lifetime token counters from all historical database records."""
    from backend.ai_helper import token_tracker

    return token_tracker.recalculate_from_database(db=db)


@app.delete("/api/llm/token-usage")
def reset_llm_token_usage():
    """Resets the session token usage counter."""
    from backend.ai_helper import token_tracker

    token_tracker.reset()
    return {"message": "Token usage counter successfully reset."}


# -------------------------------------------------------------
# BYOK LLM Settings (persisted, encrypted; env vars override)
# -------------------------------------------------------------
class LlmSettingsIn(BaseModel):
    openai_api_key: Optional[str] = None
    gemini_api_key: Optional[str] = None
    anthropic_api_key: Optional[str] = None
    allow_cloud_fallback: Optional[bool] = None


class LlmSettingsOut(BaseModel):
    openai_configured: bool
    gemini_configured: bool
    anthropic_configured: bool
    allow_cloud_fallback: bool


def _masked_llm_settings() -> LlmSettingsOut:
    creds = get_llm_credentials()
    return LlmSettingsOut(
        openai_configured=bool(creds["openai"]),
        gemini_configured=bool(creds["gemini"]),
        anthropic_configured=bool(creds["anthropic"]),
        allow_cloud_fallback=is_cloud_fallback_allowed(),
    )


@app.get("/api/settings/llm", response_model=LlmSettingsOut)
def get_llm_settings():
    """Returns masked BYOK credential status and the cloud-fallback toggle (never raw keys)."""
    return _masked_llm_settings()


@app.put("/api/settings/llm", response_model=LlmSettingsOut)
def update_llm_settings(payload: LlmSettingsIn):
    """
    Persists BYOK credentials (encrypted at rest) and the cloud-fallback toggle.
    Pass an empty string to clear a key; omit a field (null) to leave it unchanged.
    """
    save_llm_settings(
        openai_api_key=payload.openai_api_key,
        gemini_api_key=payload.gemini_api_key,
        anthropic_api_key=payload.anthropic_api_key,
        allow_cloud_fallback=payload.allow_cloud_fallback,
    )
    # Re-evaluate the active provider so LLM queue concurrency/pacing follows the new config.
    try:
        detect_active_llm_provider()
    except Exception:
        pass
    return _masked_llm_settings()


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
    save_gmail_settings(
        client_id=(payload.client_id or "").strip() or None,
        client_secret=(payload.client_secret or "").strip() or None,
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
    # Persist the PKCE verifier so the callback can complete the token exchange.
    save_gmail_settings(code_verifier=result.get("code_verifier", ""))
    return {"auth_url": result["auth_url"]}


@app.get("/api/gmail/oauth2callback")
def gmail_oauth2callback(code: Optional[str] = None, error: Optional[str] = None):
    """Google loopback redirect target: exchanges the code and stores the refresh token."""
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
    )
    return HTMLResponse(
        "<h3>✅ Gmail connected.</h3><p>You can close this tab and return to Job Alert Agent.</p>"
    )


@app.post("/api/gmail/disconnect")
def gmail_disconnect():
    """Clears stored Gmail credentials/token."""
    clear_gmail_settings()
    return {"status": "disconnected"}


@app.post("/api/gmail/sync", response_model=GmailSyncResponse)
def gmail_sync_now(db: Session = Depends(get_db)):
    """Runs the opt-in, narrowly-scoped Gmail API sync."""
    if not is_feature_enabled(db, "gmail_sync"):
        return GmailSyncResponse(
            status="disabled",
            message="Enable 'Gmail sync' in Settings → Opt-in Discovery Features.",
        )
    if not is_gmail_configured():
        return GmailSyncResponse(
            status="not_connected", message="Connect Gmail in Settings first."
        )
    try:
        events = gmail_client.run_gmail_sync(db)
        updates = _apply_email_events_to_db(db, events)
        opportunities = _ingest_email_opportunities_to_db(db, events)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Gmail sync failed: {e}")
    return GmailSyncResponse(
        status="ok",
        emails_scanned=len(events),
        status_updates=updates,
        opportunities_added=opportunities,
        message=f"Gmail sync complete: {len(events)} events, {updates} status updates, {opportunities} opportunities added.",
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
    db: Session = Depends(get_db),
):
    """
    Lists tracked jobs with optional filters for status, in-progress state, company, score, title, location, or work mode.
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

    jobs = query.order_by(Job.created_at.desc()).all()

    # Attach company_name helper
    results = []
    for j in jobs:
        j_out = JobOut.model_validate(j)
        j_out.company_name = j.company.name if j.company else None
        results.append(j_out)
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
RADAR_EXCLUDED_STATUSES = ("Rejected", "Not Interested", "Ignored")


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


_UNKNOWN_COMPANY_NAMES = {"", "unknown company", "unknown", "n/a", "none", "null"}


def _safe_company_name(name: Optional[str], fallback: str) -> str:
    """Returns a usable employer name, substituting ``fallback`` for empty/placeholder values."""
    cleaned = (name or "").strip()
    return cleaned if cleaned.lower() not in _UNKNOWN_COMPANY_NAMES else fallback


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
    return j_out


@app.get("/api/jobs/scrape-audit")
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


@app.get("/api/jobs/{job_id}", response_model=JobOut)
def get_job(job_id: int, db: Session = Depends(get_db)):
    """Retrieves a single job's details."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    j_out = JobOut.model_validate(job)
    j_out.company_name = job.company.name if job.company else None
    return j_out


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
    job.status = payload.status
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
    return j_out


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


@app.post("/api/llm/queue/clear-background")
def clear_background_tasks():
    """Clears all background tasks from the LLM queue."""
    llm_queue.clear_background_tasks()
    return {"message": "Background tasks cleared successfully."}


# -------------------------------------------------------------
# Resume Endpoints (Encrypted Storage & Document Attachment)
# -------------------------------------------------------------


def get_active_resume_file_path() -> Optional[str]:
    """Finds the local active resume document on disk for automated form attachment."""
    resumes_dir = BASE_DIR / "data" / "resumes"
    if not resumes_dir.exists():
        return None
    for ext in ["pdf", "txt", "md"]:
        f = resumes_dir / f"active_resume.{ext}"
        if f.exists():
            return str(f)
    return None


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
        is_active=True,
    )
    db.add(new_resume)
    db.commit()
    db.refresh(new_resume)

    return ResumeOut(
        id=new_resume.id,
        filename=new_resume.filename,
        is_active=new_resume.is_active,
        created_at=new_resume.created_at,
        raw_preview=raw_text[:400] + ("..." if len(raw_text) > 400 else ""),
        parsed_json=parsed_json,
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
    excluded_companies: Optional[str] = None


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
    excluded_companies: str = ""


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
        excluded_companies=pref.excluded_companies
        if pref.excluded_companies is not None
        else "amazon",
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
    if payload.excluded_companies is not None:
        # Normalize to a clean comma-separated, lowercased blocklist.
        tokens = [
            t.strip().lower()
            for t in payload.excluded_companies.split(",")
            if t.strip()
        ]
        pref.excluded_companies = ", ".join(dict.fromkeys(tokens))
    db.commit()
    db.refresh(pref)
    return get_preferences(db=db)


@app.get("/api/resumes", response_model=List[ResumeOut])
def list_resumes(db: Session = Depends(get_db)):
    """Lists all uploaded resumes with decryption and ATS status."""
    resumes = db.query(Resume).order_by(Resume.created_at.desc()).all()
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
                raw_preview=decrypted_content[:400]
                + ("..." if len(decrypted_content) > 400 else ""),
                parsed_json=parsed_json,
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
    )


@app.delete("/api/resume/{resume_id}", status_code=status.HTTP_200_OK)
def delete_single_resume(resume_id: int, db: Session = Depends(get_db)):
    """Deletes a specific resume and activates the latest remaining one if needed."""
    target = db.query(Resume).filter(Resume.id == resume_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="Resume not found.")

    filename = target.filename
    was_active = target.is_active
    db.delete(target)
    db.commit()

    if was_active:
        latest = db.query(Resume).order_by(Resume.created_at.desc()).first()
        if latest:
            latest.is_active = True
            db.commit()

    return {"message": f"Resume '{filename}' deleted successfully."}


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

    decrypted_content = decrypt_data(target.content_encrypted)
    return ResumeOut(
        id=target.id,
        filename=target.filename,
        is_active=target.is_active,
        created_at=target.created_at,
        raw_preview=decrypted_content,
        parsed_json=parsed_json,
    )


class CleanCacheRequest(BaseModel):
    clean_unapplied_jobs: bool = True
    clean_all_jobs: bool = False


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
scrape_cancel_event = threading.Event()
linkedin_cancel_event = threading.Event()


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


@app.post("/api/companies/{company_id}/scrape-jobs", response_model=ScrapeResponse)
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


@app.post("/api/jobs/scrape", response_model=ScrapeResponse)
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
    resume_skills = []
    if active_resume and active_resume.parsed_json_encrypted:
        try:
            parsed = json.loads(decrypt_data(active_resume.parsed_json_encrypted))
            resume_skills = [s.lower() for s in parsed.get("skills", [])]
        except Exception:
            pass

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

    global LATEST_SCRAPE_AUDIT
    audit_records = []

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

        # A. Specialized company APIs (e.g. Uber) — opt-in (unofficial internal API)
        if _ats_on and _flags.get("ats_uber") and "uber" in comp_lower:
            discovered_jobs.extend(scrape_uber_jobs()[:35])

        # B. Greenhouse ATS (Lightweight metadata fetch + early filtering + on-demand detail)
        if _ats_on and _flags.get("ats_greenhouse"):
            gh_jobs = scrape_greenhouse_jobs(
                comp.name,
                title_filters=title_filters,
                city_filters=city_filters,
                location_filters=loc_filters,
                work_mode=wm_filter,
                max_matches=35,
                careers_url=comp.careers_url,
            )
            if gh_jobs:
                discovered_jobs.extend(gh_jobs)

        # C. Lever ATS
        if _ats_on and _flags.get("ats_lever"):
            lev_jobs = scrape_lever_jobs(
                comp.name,
                title_filters=title_filters,
                city_filters=city_filters,
                location_filters=loc_filters,
                work_mode=wm_filter,
                max_matches=35,
                careers_url=comp.careers_url,
            )
            if lev_jobs:
                discovered_jobs.extend(lev_jobs)

        # D. Ashby ATS (companies hosting their job boards on Ashby)
        if _ats_on and _flags.get("ats_ashby"):
            ashby_jobs = scrape_ashby_jobs(
                comp.name,
                title_filters=title_filters,
                city_filters=city_filters,
                location_filters=loc_filters,
                work_mode=wm_filter,
                max_matches=35,
                careers_url=comp.careers_url,
            )
            if ashby_jobs:
                discovered_jobs.extend(ashby_jobs)

        # D2. SmartRecruiters ATS — opt-in (ambiguous ToS; see docs/ATS_PORTALS.md)
        if _ats_on and _flags.get("ats_smartrecruiters"):
            sr_jobs = scrape_smartrecruiters_jobs(
                comp.name,
                title_filters=title_filters,
                city_filters=city_filters,
                location_filters=loc_filters,
                work_mode=wm_filter,
                max_matches=35,
                careers_url=comp.careers_url,
            )
            if sr_jobs:
                discovered_jobs.extend(sr_jobs)

        # D3. Workable ATS — opt-in (unofficial widget endpoint)
        if _ats_on and _flags.get("ats_workable"):
            wk_jobs = scrape_workable_jobs(
                comp.name,
                title_filters=title_filters,
                city_filters=city_filters,
                location_filters=loc_filters,
                work_mode=wm_filter,
                max_matches=35,
                careers_url=comp.careers_url,
            )
            if wk_jobs:
                discovered_jobs.extend(wk_jobs)

        # D4. Workday ATS (requires a Workday board URL) — opt-in (unofficial CXS API)
        if _ats_on and _flags.get("ats_workday"):
            wd_jobs = scrape_workday_jobs(
                comp.name,
                title_filters=title_filters,
                city_filters=city_filters,
                location_filters=loc_filters,
                work_mode=wm_filter,
                max_matches=35,
                careers_url=comp.careers_url,
            )
            if wd_jobs:
                discovered_jobs.extend(wd_jobs)

        # E. Custom HTML portal scraper — opt-in (arbitrary HTML scraping)
        if _ats_on and _flags.get("ats_custom_html") and comp.careers_url:
            custom_jobs = scrape_custom_jobs(comp.careers_url)
            if custom_jobs:
                discovered_jobs.extend(custom_jobs[:35])

        comp_raw_count = len(discovered_jobs)
        total_found += comp_raw_count
        comp_added = 0

        # remove duplicates from discovered jobs based on url
        unique_jobs = []
        seen_urls = set()
        for job in discovered_jobs:
            if job["url"] not in seen_urls:
                seen_urls.add(job["url"])
                unique_jobs.append(job)
        discovered_jobs = unique_jobs

        # 3. Process each discovered job
        for j in discovered_jobs:
            if scrape_cancel_event.is_set():
                logger.info("Job scraping cancelled by user.")
                break

            # Semantic & Synonym Filtering via the shared preference gate
            if not pref_filter.accepts(j.get("title", ""), j.get("location", "")):
                continue

            existing = (
                db.query(Job)
                .filter(Job.company_id == comp.id, Job.title == j["title"])
                .first()
            )

            if existing:
                continue

            # Evaluate due diligence / ghost job flags
            dd = evaluate_job_due_diligence(
                job_title=j["title"],
                source_type=j.get("source_type", "Direct"),
                portal_jobs=discovered_jobs,
            )

            # Negative Role Filter
            dismissal_match = check_semantic_dismissal(
                j["title"], j.get("description", ""), db, company_name=comp.name
            )
            job_status = "Not Interested" if dismissal_match else "To Apply"
            match_analysis = (
                f"Auto-filtered by semantic ignore rule (matched: '{dismissal_match['matched_title']}' with {int(dismissal_match['similarity'] * 100)}% similarity)"
                if dismissal_match
                else None
            )

            # Fast keyword/semantic match score
            match_score = 0
            if resume_skills and not dismissal_match:
                job_text_lower = f"{j['title']} {j.get('description', '')}".lower()
                matched_count = sum(1 for s in resume_skills if s in job_text_lower)
                if resume_skills:
                    ratio = matched_count / max(len(resume_skills), 1)
                    match_score = round(min(98.0, max(50.0, 50.0 + (ratio * 50.0))), 1)

            job_emb = generate_embeddings(
                f"{j['title']} {clean_job_description(j.get('description', '') or j['title'])}"
            )
            new_job = Job(
                company_id=comp.id,
                title=j["title"],
                description=j.get("description", ""),
                url=j.get("url", comp.careers_url or ""),
                salary_range=j.get("salary_range")
                or comp.salary_insights
                or "Competitive",
                location=j.get("location", "Remote / Various"),
                source=j.get("source", "Careers Portal"),
                source_type=j.get("source_type", "Direct"),
                status=job_status,
                match_score=match_score,
                match_analysis=match_analysis,
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

        status_text = (
            "Success"
            if comp_added > 0
            else ("Filtered" if comp_raw_count > 0 else "0 Postings Found")
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
    return job


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


@app.post("/api/jobs/scrape/stop")
def stop_jobs_scrape():
    """Signals the active job scraping background loop to cancel immediately."""
    scrape_cancel_event.set()
    return {"message": "Job scraping process stopped."}


@app.post("/api/linkedin/sync/stop")
def stop_linkedin_sync():
    """Signals the active LinkedIn sync background task to abort."""
    linkedin_cancel_event.set()
    return {"message": "LinkedIn sync process stopped."}


@app.post("/api/jobs/{job_id}/apply", response_model=ApplyResponse)
def trigger_assisted_apply(job_id: int, db: Session = Depends(get_db)):
    """
    Launches an interactive Playwright session with the dedicated Chrome profile
    to assist with application filling and display the helper overlay panel.
    Implements Just-In-Time (JIT) material generation if cover letter/tailored points are missing.
    """
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")

    if not job.url:
        raise HTTPException(
            status_code=400, detail="Job does not have an application URL."
        )

    # Load active resume parsed data
    active_resume = (
        db.query(Resume)
        .filter(Resume.is_active == True)
        .order_by(Resume.created_at.desc())
        .first()
    )
    resume_data = {}
    resume_text = ""
    if active_resume and active_resume.parsed_json_encrypted:
        try:
            resume_data = json.loads(decrypt_data(active_resume.parsed_json_encrypted))
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

    resume_file_path = get_active_resume_file_path()

    # Launch Playwright session in background thread
    def run_browser():
        try:
            launch_assisted_application_session(
                url=job.url,
                resume_data=resume_data,
                cover_letter=job.cover_letter_draft or "",
                tailored_points=job.tailored_resume_points or "",
                cold_msg=job.cold_message_draft or "",
                job_id=job.id,
                resume_file_path=resume_file_path,
                headless=False,
            )
        except Exception as e:
            logger.error(f"Error running assisted apply browser session: {e}")

    thread = threading.Thread(target=run_browser, daemon=True)
    thread.start()

    return ApplyResponse(
        job_id=job.id,
        status="Applied",
        message=f"Assisted application browser tab opened for '{job.title}' and marked as 'Applied'.",
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
    return j_out


@app.post("/api/jobs/bulk-apply", response_model=BulkApplyResponse)
def trigger_bulk_apply(req: BulkApplyRequest, db: Session = Depends(get_db)):
    """
    Opens multiple selected job postings as tabs inside the single Chrome window,
    triggers automated deep form-filling on each, and marks all selected jobs as 'Applied'.
    """
    if not req.job_ids:
        raise HTTPException(status_code=400, detail="No job IDs provided.")

    jobs = db.query(Job).filter(Job.id.in_(req.job_ids)).all()
    if not jobs:
        raise HTTPException(status_code=404, detail="No matching jobs found.")

    active_resume = (
        db.query(Resume)
        .filter(Resume.is_active == True)
        .order_by(Resume.created_at.desc())
        .first()
    )
    resume_data = {}
    resume_text = ""
    if active_resume and active_resume.parsed_json_encrypted:
        try:
            resume_data = json.loads(decrypt_data(active_resume.parsed_json_encrypted))
            resume_text = decrypt_data(active_resume.content_encrypted)
        except Exception:
            pass

    resume_file_path = get_active_resume_file_path()

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

    def run_bulk_browser():
        from backend.playwright_app import BrowserSessionManager

        manager = BrowserSessionManager.get_instance()
        for j in jobs_to_launch:
            try:
                manager.open_job_tab(
                    job_id=j["id"],
                    url=j["url"],
                    resume_data=resume_data,
                    cover_letter=j["cover_letter"],
                    tailored_points=j["tailored_points"],
                    cold_msg=j["cold_msg"],
                    resume_file_path=resume_file_path,
                    headless=False,
                )
                time.sleep(0.4)
            except Exception as e:
                pass

    threading.Thread(target=run_bulk_browser, daemon=True).start()

    return BulkApplyResponse(
        message=f"Successfully opened {len(applied_ids)} job application tab(s) in Chrome and updated status to 'Applied'.",
        applied_job_ids=applied_ids,
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
    system_prompt = (
        f"You are an elite executive career coach and interview preparation advisor. "
        f"You are coaching the candidate for the role of '{job.title}' at '{comp_name}'.\n\n"
        f"Context Details:\n"
        f"- Target Position: {job.title}\n"
        f"- Company: {comp_name}\n"
        f"- Company News & Products: {comp.recent_news if comp else 'N/A'}\n"
        f"- Company Hiring Process: {comp.hiring_process if comp else 'N/A'}\n"
        f"- Compensation Insights: {comp.salary_insights if comp else 'N/A'}\n"
        f"- Job Description: {cleaned_jd}\n"
        f"- Candidate Profile: {resume_text[:1200]}\n\n"
        "Provide tailored, actionable, high-impact advice. Answer directly and structure clearly with markdown."
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


def _match_result_lacks_detail(result: dict) -> bool:
    """
    True when the analyzer produced no usable detail: either the empty JobMatchResult default,
    or one of its generic error sentinels when the model was unreachable. We surface this as an
    explicit error rather than fabricating a score.
    """
    if result.get("match_score") is None:
        return True
    if [s for s in (result.get("strengths") or []) if s]:
        return False
    gaps = [str(g).strip() for g in (result.get("gaps") or []) if g]
    feedback = str(result.get("feedback") or "").strip()
    if feedback.lower().startswith("analysis encountered an error"):
        return True
    if gaps and all(g.lower() == "unable to complete ai comparison." for g in gaps):
        return True
    return not gaps and not feedback


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
    resume_text = (payload.resume_text or "").strip()
    if not resume_text:
        active_resume = (
            db.query(Resume)
            .filter(Resume.is_active == True)
            .order_by(Resume.created_at.desc())
            .first()
        )
        resume_text = (
            decrypt_data(active_resume.content_encrypted) if active_resume else ""
        )

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

    description = (payload.job_description or "").strip() or payload.job_title
    try:
        result = analyze_job_match(resume_text, description)
    except Exception as e:
        logger.warning(f"Structured match analysis failed: {e}")
        result = {}

    if _match_result_lacks_detail(result):
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
                "Match analysis could not be completed because the AI model was unreachable "
                "or returned no usable result."
            ),
        )

    return MatchAnalyzeResponse(
        match_score=(
            float(result["match_score"])
            if result.get("match_score") is not None
            else None
        ),
        strengths=[str(s) for s in (result.get("strengths") or []) if s],
        gaps=[str(g) for g in (result.get("gaps") or []) if g],
        feedback=str(result.get("feedback") or ""),
        analysis_source="llm",
    )


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
    from backend.playwright_app import clean_stale_profile_locks
    import subprocess

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

    def run_browser():
        try:
            clean_stale_profile_locks()
            with sync_playwright() as p:
                context = get_persistent_browser_context(p, headless=False)
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(final_url, wait_until="domcontentloaded", timeout=60000)
                page.bring_to_front()

                # Force foreground focus on macOS
                for app_name in [
                    "Google Chrome for Testing",
                    "Google Chrome",
                    "Chromium",
                ]:
                    subprocess.run(
                        [
                            "osascript",
                            "-e",
                            f'tell application "{app_name}" to activate',
                        ],
                        check=False,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )

                # Keep session open for user to log in
                page.wait_for_timeout(600000)
                context.close()
        except Exception as e:
            logger.error(f"Interactive browser session error: {e}")

    thread = threading.Thread(target=run_browser, daemon=True)
    thread.start()

    return {
        "status": "Launched",
        "message": f"Interactive browser launched for '{final_url}'. Sign in to your account, and your session will persist automatically in .chrome_profile.",
    }


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


@app.post("/api/linkedin/sync", response_model=LinkedInSyncResponse)
def sync_linkedin_alerts_and_saved(db: Session = Depends(get_db)):
    """
    Syncs live LinkedIn subscribed job alerts, recommended openings, and saved jobs.
    Prunes stale unacted 'To Apply' alert jobs (>7 days old) while strictly preserving Shortlisted & Applied jobs.
    Prunes/unsaves any closed or expired jobs from the LinkedIn saved list and updates DB status.
    """
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
                # 2. Scan subscribed job alert feeds + recommendations on shared page
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

                        # Zero-Token Negative Role Filter
                        dismissal_match = check_semantic_dismissal(
                            rec["title"],
                            rec.get("description", ""),
                            db,
                            company_name=comp.name,
                        )
                        job_status = "Not Interested" if dismissal_match else "To Apply"

                        match_scored = False
                        match_score = 0
                        if dismissal_match:
                            dismissed_filtered += 1
                            match_analysis = f"Auto-filtered by semantic ignore rule (matched: '{dismissal_match['matched_title']}' with {int(dismissal_match['similarity'] * 100)}% similarity)"
                        else:
                            match_analysis = "Direct LinkedIn Job Alert match."
                            if resume_text:
                                analysis = analyze_job_match(
                                    resume_text, rec.get("description", rec["title"])
                                )
                                if analysis.get("match_score") is not None:
                                    match_score = analysis["match_score"]
                                    match_analysis = f"Strengths: {', '.join(analysis.get('strengths', []))}. Gaps: {', '.join(analysis.get('gaps', []))}"
                                    match_scored = True

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
                            match_score=match_score,
                            match_analysis=match_analysis,
                            match_scored=match_scored,
                            cover_letter_draft=None,
                            tailored_resume_points=None,
                            cold_message_draft=None,
                            is_ghost_job=dd.get("is_ghost_job", False),
                            embedding=job_emb,
                        )
                        db.add(new_job)
                        recs_added += 1

                # 3. Scan saved jobs across all pages on shared page, ingest active ones as Shortlisted, and prune closed listings
                saved_jobs = scan_and_sync_linkedin_saved_jobs(
                    p, headless=True, auto_unsave_closed=True, page=page
                )
                saved_checked = len(saved_jobs)
                saved_added = 0

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
                            # Only auto-reject pre-application states; never clobber active progress.
                            if matched_job.status in (
                                "To Apply",
                                "Shortlisted",
                                "Not Interested",
                            ):
                                matched_job.status = "Rejected"
                                matched_job.rejected_at = datetime.datetime.now(
                                    datetime.timezone.utc
                                )
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
                            if existing_job.status in ("To Apply", "Not Interested"):
                                existing_job.status = "Shortlisted"
                        else:
                            # Ingest new active saved job
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

                            match_analysis = "Saved directly on LinkedIn."
                            match_scored = False
                            match_score = 0

                            if resume_text:
                                analysis = analyze_job_match(resume_text, item["title"])
                                if analysis.get("match_score") is not None:
                                    match_score = analysis["match_score"]
                                    match_analysis = f"Strengths: {', '.join(analysis.get('strengths', []))}. Gaps: {', '.join(analysis.get('gaps', []))}"
                                    match_scored = True

                            job_emb = generate_embeddings(
                                f"{item['title']} {clean_job_description(item.get('description', '') or item['title'])}"
                            )
                            new_saved_job = Job(
                                company_id=comp.id,
                                title=item["title"],
                                description=f"LinkedIn Saved Job opening for {item['title']} at {comp.name}.",
                                url=item.get("url", ""),
                                location=item.get("location", "Remote / Various"),
                                source="LinkedIn Saved Jobs",
                                source_type="LinkedIn",
                                status="Shortlisted",
                                match_score=match_score,
                                match_analysis=match_analysis,
                                match_scored=match_scored,
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
        summary_msg = f"LinkedIn sync complete: Scanned {total_processed} total posting(s) ({recs_found} alerts/recommendations, {saved_checked} saved). {total_added} new added, {already_existing} already tracked, {closed_pruned} closed job(s) pruned."

        # Persist Operation Audit Log
        try:
            end_tok = token_tracker.get_stats()
            p_used = max(0, end_tok["prompt_tokens"] - start_tok["prompt_tokens"])
            c_used = max(
                0, end_tok["completion_tokens"] - start_tok["completion_tokens"]
            )

            op_log = OperationLog(
                operation_type="linkedin_sync",
                status="Completed",
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


@app.get("/api/jobs/google-jobs/queries")
def get_google_jobs_queries():
    """Returns the list of followed / configured Google Jobs search queries."""
    return {"queries": load_google_searches()}


@app.post("/api/jobs/google-jobs/queries")
def update_google_jobs_queries(payload: GoogleSearchesUpdate):
    """Updates and saves the configured Google Jobs search queries."""
    saved = save_google_searches(payload.queries)
    return {
        "queries": saved,
        "message": f"Successfully saved {len(saved)} search queries.",
    }


@app.post("/api/jobs/google-jobs/sync-followed")
def sync_google_jobs_followed_queries():
    """
    Spawns Playwright to navigate to Google for Jobs 'Following' tab,
    extracts followed alert queries, merges them into saved searches, and returns the updated list.
    """
    discovered = []
    try:
        with sync_playwright() as p:
            discovered = fetch_google_jobs_followed_queries_playwright(p, headless=True)
    except Exception as e:
        logger.warning(f"Failed to extract followed queries via Playwright: {e}")

    current = load_google_searches()
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


@app.post("/api/jobs/google-jobs/scrape", response_model=GoogleJobsScrapeResponse)
def scrape_google_jobs_endpoint(
    req: Optional[GoogleJobsScrapeRequest] = None, db: Session = Depends(get_db)
):
    """
    Scrapes Google for Jobs (ibp=htl;jobs) and SERP Schema.org JobPosting listings
    across multiple configured followed queries.
    Performs cross-source deduplication, ATS portal learning, zero-token filtering,
    match alignment scoring, and automated 384-d vector embeddings indexing.
    """
    req = req or GoogleJobsScrapeRequest()
    start_tok = token_tracker.get_stats()

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
    resume_skills = []
    if active_resume:
        try:
            parsed_data = (
                json.loads(decrypt_data(active_resume.parsed_json_encrypted))
                if active_resume.parsed_json_encrypted
                else {}
            )
            resume_skills = parsed_data.get("skills", [])
        except Exception:
            pass

    if not queries_to_run:
        default_q = "Software Engineer"
        if resume_skills:
            default_q = f"{resume_skills[0]} Software Engineer"
        queries_to_run = [default_q]

    total_discovered = 0
    total_added = 0
    already_existing = 0
    dismissed_filtered = 0
    per_query_breakdown = []
    seen_in_batch_urls = set()

    search_loc = req.location or "India"

    try:
        for query_str in queries_to_run:
            q_discovered = 0
            q_added = 0
            q_existing = 0
            q_dismissed = 0
            discovered_jobs = []

            try:
                if req.use_browser:
                    with sync_playwright() as p:
                        discovered_jobs = fetch_google_jobs_playwright(
                            p,
                            query=query_str,
                            location=search_loc,
                            headless=True,
                            max_results=req.limit,
                        )
                else:
                    discovered_jobs = fetch_google_jobs(
                        query=query_str, location=search_loc, max_results=req.limit
                    )
                    if not discovered_jobs:
                        try:
                            with sync_playwright() as p:
                                discovered_jobs = fetch_google_jobs_playwright(
                                    p,
                                    query=query_str,
                                    location=search_loc,
                                    headless=True,
                                    max_results=req.limit,
                                )
                        except Exception as p_err:
                            logger.debug(
                                f"Playwright fallback for Google Jobs failed for query '{query_str}': {p_err}"
                            )
            except Exception as q_err:
                logger.error(f"Error fetching jobs for query '{query_str}': {q_err}")
                discovered_jobs = []

            q_discovered = len(discovered_jobs)
            total_discovered += q_discovered

            for j in discovered_jobs:
                job_url = j.get("url", "").strip()
                job_title = j.get("title", "").strip()
                comp_name = _safe_company_name(j.get("company"), "Google Jobs Employer")

                # Batch deduplication check
                batch_key = (
                    job_url if job_url else f"{job_title}::{comp_name}"
                ).lower()
                if batch_key in seen_in_batch_urls:
                    q_existing += 1
                    already_existing += 1
                    continue
                seen_in_batch_urls.add(batch_key)

                # Database deduplication check
                existing = None
                if job_url:
                    existing = db.query(Job).filter(Job.url == job_url).first()
                if not existing:
                    existing = (
                        db.query(Job)
                        .filter(
                            (Job.title.ilike(job_title)) & (Job.source_type == "Google")
                        )
                        .first()
                    )

                if existing:
                    q_existing += 1
                    already_existing += 1
                    continue

                # Employer resolution & ATS portal learning
                comp = db.query(Company).filter(Company.name.ilike(comp_name)).first()
                if not comp:
                    comp = Company(
                        name=comp_name,
                        domain=f"{comp_name.lower().replace(' ', '')}.com",
                    )
                    db.add(comp)
                    db.flush()

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

                # Due diligence
                dd = evaluate_job_due_diligence(
                    job_title=job_title, source_type="Direct", portal_jobs=[]
                )

                # Zero-token negative role filter
                dismissal_match = check_semantic_dismissal(
                    job_title, j.get("description", ""), db, company_name=comp.name
                )
                job_status = "Not Interested" if dismissal_match else "To Apply"

                match_score = 0
                if dismissal_match:
                    q_dismissed += 1
                    dismissed_filtered += 1
                    match_analysis = f"Auto-filtered by semantic ignore rule (matched: '{dismissal_match['matched_title']}' with {int(dismissal_match['similarity'] * 100)}% similarity)"
                else:
                    match_analysis = (
                        f"Direct Google Jobs match for alert: '{query_str}'."
                    )
                    if resume_skills:
                        job_text_lower = (
                            f"{job_title} {j.get('description', '')}".lower()
                        )
                        matched_count = sum(
                            1 for s in resume_skills if s.lower() in job_text_lower
                        )
                        ratio = matched_count / max(len(resume_skills), 1)
                        match_score = round(
                            min(98.0, max(50.0, 50.0 + (ratio * 50.0))), 1
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
                    source_type="Google",
                    status=job_status,
                    match_score=match_score,
                    match_analysis=match_analysis,
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
                }
            )

        summary_msg = f"Google Jobs discovery complete across {len(queries_to_run)} alert query(ies): Discovered {total_discovered} posting(s). Added {total_added} new target(s), {already_existing} already tracked, {dismissed_filtered} auto-dismissed."

        # OperationLog
        try:
            end_tok = token_tracker.get_stats()
            p_used = max(0, end_tok["prompt_tokens"] - start_tok["prompt_tokens"])
            c_used = max(
                0, end_tok["completion_tokens"] - start_tok["completion_tokens"]
            )

            op_log = OperationLog(
                operation_type="google_jobs_scrape",
                status="Completed",
                summary=summary_msg,
                details_json=sanitize_log_details(
                    {
                        "queries_scanned": queries_to_run,
                        "location": search_loc,
                        "total_discovered": total_discovered,
                        "total_added": total_added,
                        "already_existing": already_existing,
                        "dismissed_filtered": dismissed_filtered,
                        "per_query_breakdown": per_query_breakdown,
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
            queries_scanned=queries_to_run,
            per_query_breakdown=per_query_breakdown,
            message=summary_msg,
        )

    except Exception as e:
        logger.error(f"Error during Google Jobs scrape: {e}")
        return GoogleJobsScrapeResponse(
            total_discovered=total_discovered,
            total_added=total_added,
            already_existing=already_existing,
            dismissed_filtered=dismissed_filtered,
            queries_scanned=queries_to_run,
            per_query_breakdown=per_query_breakdown,
            message=f"Google Jobs scrape encountered notice: {str(e)}",
        )


# -------------------------------------------------------------
# External Applications Dashboard Sync (Hirist / Greenhouse / Gmail)
# -------------------------------------------------------------

# Confidence gates for automatic email-driven DB writes.
# Events without an explicit confidence (e.g. legacy/manual callers) are treated as trusted.
EMAIL_STATUS_MIN_CONFIDENCE = 0.6  # to mutate an existing job's status
EMAIL_AUTO_CREATE_MIN_CONFIDENCE = 0.75  # to create a brand-new job from an email
EMAIL_OPPORTUNITY_MIN_CONFIDENCE = 0.6  # to create a job from an email opportunity


def _match_job_for_email_event(db, comp_name: str, role: str = ""):
    """
    Finds the best matching Job for an email event.
    Prefers an explicit title match at the company (among jobs the user has shortlisted/applied
    to), then a unique company match, then the most recent in-progress job at that company
    (avoids cross-role mixups when a company has many tracked roles).
    """
    pipeline_statuses = ["Applied", "Screening", "Interview", "Offered", "Shortlisted"]
    comp_query = db.query(Job).filter(
        Job.company.has(Company.name.ilike(f"%{comp_name}%"))
    )
    role = (role or "").strip()
    if role:
        match = (
            comp_query.filter(Job.title.ilike(f"%{role}%"))
            .filter(Job.status.in_(pipeline_statuses))
            .order_by(Job.id.desc())
            .first()
        )
        if not match:
            match = (
                comp_query.filter(Job.title.ilike(f"%{role}%"))
                .order_by(Job.id.desc())
                .first()
            )
        if match:
            return match
    candidates = comp_query.order_by(Job.id.desc()).limit(2).all()
    if len(candidates) == 1:
        return candidates[0]
    return (
        comp_query.filter(Job.status.in_(pipeline_statuses))
        .order_by(Job.id.desc())
        .first()
    )


def _ensure_tracked_job_for_email_event(db, ev):
    """
    Creates a tracked job for an application email when no existing job matches,
    so manual applications and their outcomes are captured even if never discovered
    by a portal/LinkedIn/Google scan.
    """
    comp_name = (ev.get("company") or "").strip()
    role = (ev.get("role") or "").strip() or "Software Engineer"
    if not comp_name:
        return None
    comp = db.query(Company).filter(Company.name.ilike(comp_name)).first()
    if not comp:
        comp = Company(
            name=comp_name, domain=f"{comp_name.lower().replace(' ', '')}.com"
        )
        db.add(comp)
        db.flush()
    exists = (
        db.query(Job).filter(Job.company_id == comp.id, Job.title.ilike(role)).first()
    )
    if exists:
        return exists
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


def _apply_email_events_to_db(db, email_events) -> int:
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
    for ev in email_events or []:
        ev_type = ev.get("event_type", "")
        if ev_type == "opportunity":
            continue
        comp_name = ev.get("company", "")
        role = ev.get("role", "")
        subj = ev.get("subject", "")
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
        matched_job = _match_job_for_email_event(db, comp_name, role)
        if not matched_job:
            # Capture applications/outcomes for roles never discovered by a scan — but only
            # for high-confidence signals, so weak matches don't create phantom jobs.
            if (
                ev_type
                in (
                    "application_received",
                    "screening",
                    "interview",
                    "offer",
                    "rejection",
                )
                and confidence >= EMAIL_AUTO_CREATE_MIN_CONFIDENCE
            ):
                matched_job = _ensure_tracked_job_for_email_event(db, ev)
            if not matched_job:
                continue

        new_status = None
        desc = ""
        if ev_type == "rejection":
            if matched_job.status != "Rejected":
                new_status = "Rejected"
                matched_job.rejected_at = now_utc
                desc = f"Decision email received: '{subj}'."
        elif ev_type == "offer":
            if matched_job.status != "Offered":
                new_status = "Offered"
                desc = f"Offer email received: '{subj}'."
        elif ev_type == "interview":
            if matched_job.status not in ("Interview", "Offered"):
                new_status = "Interview"
                matched_job.interview_scheduled_at = now_utc
                desc = f"Interview invite email received: '{subj}'."
        elif ev_type == "screening":
            if matched_job.status in ("To Apply", "Shortlisted", "Applied"):
                new_status = "Screening"
                desc = f"Screening email received: '{subj}'."
        elif ev_type == "application_received":
            if matched_job.status in ("To Apply", "Shortlisted", "Not Interested"):
                new_status = "Applied"
                matched_job.applied_at = now_utc
                desc = f"Application confirmation email received: '{subj}'."

        if new_status:
            matched_job.status = new_status
            db.add(
                ApplicationEvent(
                    job_id=matched_job.id,
                    event_type=f"email_{ev_type}",
                    description=desc,
                )
            )
            status_updates += 1

    if skipped_low_confidence:
        logger.info(
            f"Gmail sync: {skipped_low_confidence} low-confidence status event(s) skipped."
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
    "To Apply": 0,
    "Shortlisted": 1,
    "Applied": 2,
    "Screening": 3,
    "Interview": 4,
    "Offered": 5,
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


def _ingest_email_opportunities_to_db(db, email_events) -> int:
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
        if not comp_name:
            continue

        confidence = float(ev.get("confidence", 1.0) or 0.0)
        if confidence < EMAIL_OPPORTUNITY_MIN_CONFIDENCE:
            continue

        # Preference gate: Gmail roles carry no structured location, so verify against the
        # alert text. When location filters exist, an unverifiable location is rejected.
        if pref_filter.has_filters:
            loc_text = f"{snippet} {subject}".strip()
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

        db.add(
            Job(
                company_id=comp.id,
                title=role,
                description=snippet
                or f"Opportunity from Gmail for {role} at {comp_name}.",
                url=url,
                location="Remote / Various",
                source="Gmail Opportunity",
                source_type="Email",
                status="To Apply",
                match_score=55.0,
                match_analysis=f"Opportunity detected in Gmail: '{ev.get('subject', '')}'.",
            )
        )
        db.flush()
        seen.add(batch_key)
        added += 1
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
    details = []

    def _source_enabled(source: str, flag: str) -> bool:
        """Probing runs only when the source is requested AND its portal flag is enabled."""
        if source not in payload.sources and "all" not in payload.sources:
            return False
        if not is_feature_enabled(db, flag):
            details.append(
                f"{source} probing skipped: enable '{flag}' in Settings → Discovery Features."
            )
            return False
        return True

    active_resume = db.query(Resume).filter(Resume.is_active == True).first()
    resume_text = (
        decrypt_data(active_resume.content_encrypted)
        if (active_resume and active_resume.content_encrypted)
        else ""
    )

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

                        match_analysis = f"Imported from Hirist candidate applications dashboard. Current Status: {raw_status}."
                        match_scored = False
                        match_score = 0
                        if resume_text:
                            try:
                                analysis = analyze_job_match(resume_text, title)
                                if analysis.get("match_score") is not None:
                                    match_score = analysis["match_score"]
                                    match_scored = True
                            except Exception as mat_err:
                                logger.debug(f"Hirist match analysis skipped: {mat_err}")

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
        except Exception as e:
            logger.error(f"Error during Hirist application sync: {e}")
            details.append(f"Hirist sync error: {str(e)}")

    if cancel_token:
        cancel_token.check()

    # 2. Gmail Decision & Interview Emails Sync (OPT-IN)
    if "gmail" in payload.sources and not is_feature_enabled(db, "gmail_sync"):
        details.append(
            "Gmail sync skipped: enable 'Gmail sync' in Settings → Opt-in Discovery Features."
        )

    if "gmail" in payload.sources and is_feature_enabled(db, "gmail_sync"):
        email_events = []
        used_api = False
        api_error = None
        # API-first when connected; otherwise restricted browser fallback.
        if is_gmail_configured():
            try:
                from backend.gmail_client import run_gmail_sync

                email_events = run_gmail_sync(db)
                used_api = True
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
                logger.error(f"Error during browser email status sync: {e}")
                details.append(f"Email sync notice: {str(e)}")

        emails_checked = len(email_events)
        status_updates += _apply_email_events_to_db(db, email_events)
        opportunities_added = _ingest_email_opportunities_to_db(db, email_events)
        if opportunities_added:
            details.append(
                f"Gmail opportunities tracked: {opportunities_added} new role(s)."
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
                if not is_active and gh_job.status != "Rejected":
                    gh_job.status = "Rejected"
                    gh_job.rejected_at = datetime.datetime.now(datetime.timezone.utc)
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

            db.commit()
        except Exception as e:
            logger.debug(f"Error during Greenhouse requisition probe: {e}")

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
                    and lv_job.status != "Rejected"
                ):
                    lv_job.status = "Rejected"
                    lv_job.rejected_at = datetime.datetime.now(datetime.timezone.utc)
                    db.add(
                        ApplicationEvent(
                            job_id=lv_job.id,
                            event_type="requisition_closed",
                            description="Lever job posting has been closed or unlisted.",
                        )
                    )
                    status_updates += 1
                    details.append(f"Lever requisition closed for '{lv_job.title}'")

            db.commit()
        except Exception as e:
            logger.debug(f"Error during Lever requisition probe: {e}")

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
                    and sr_job.status != "Rejected"
                ):
                    sr_job.status = "Rejected"
                    sr_job.rejected_at = datetime.datetime.now(datetime.timezone.utc)
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

            db.commit()
        except Exception as e:
            logger.debug(f"Error during SmartRecruiters requisition probe: {e}")

    # Log operation telemetry
    db.add(
        OperationLog(
            operation_type="external_application_sync",
            status="success",
            summary=f"External Sync: {hirist_checked} Hirist checked ({hirist_updated} updated, {hirist_ingested} ingested), {emails_checked} emails scanned ({status_updates} status updates, {opportunities_added} opportunities).",
            details_json=sanitize_log_details(details),
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
        details=details,
        message=f"Synced successfully: {hirist_updated + status_updates} status updates, {hirist_ingested} new applications tracked.",
    )


# -------------------------------------------------------------
# Background Task Runners (Decoupled Long-Running Execution)
# -------------------------------------------------------------


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

    try:
        if not queries or len(queries) == 0:
            # Derive queries from persisted user preferences first, then followed queries.
            pref = get_user_preferences(db)
            titles = [
                t.strip() for t in (pref.target_titles or "").split(",") if t.strip()
            ]
            locs = [
                c.strip()
                for c in f"{pref.target_country or ''},{pref.target_cities or ''}".split(
                    ","
                )
                if c.strip()
            ]
            loc_phrase = f" in {', '.join(dict.fromkeys(locs))}" if locs else ""
            remote_suffix = (
                " remote" if (pref.work_mode or "").lower() == "remote" else ""
            )
            pref_queries = [
                normalize_google_query(f"{t} jobs{loc_phrase}{remote_suffix}")
                for t in titles
            ]
            queries = _normalize_google_queries(pref_queries) or load_google_searches()

        if not queries:
            queries = [
                "Senior Software Engineer jobs in India",
                "Staff Software Engineer remote",
            ]

        queries = _normalize_google_queries(queries)
        total_steps = len(queries)
        active_resume = db.query(Resume).filter(Resume.is_active == True).first()
        resume_text = (
            decrypt_data(active_resume.content_encrypted) if active_resume else ""
        )
        # Shared preference gate: never ingest roles/locations outside the user's criteria.
        pref_filter = PreferenceFilter.from_db(db)
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
                if use_playwright:
                    try:
                        with sync_playwright() as p:
                            raw_jobs = fetch_google_jobs_playwright(
                                p, query=q_str, location=location, headless=True
                            )
                    except Exception as p_err:
                        logger.debug(
                            f"Playwright fallback to direct SERP for '{q_str}': {p_err}"
                        )
                        raw_jobs = fetch_google_jobs(
                            query=q_str, location=location, max_results=10
                        )
                else:
                    raw_jobs = fetch_google_jobs(
                        query=q_str, location=location, max_results=10
                    )

                # Opt-in fallback: the direct SERP is often blocked, so try the open-source
                # JobSpy aggregator when the flag is enabled and the direct path found nothing.
                if not raw_jobs and is_feature_enabled(db, "jobspy_google"):
                    logger.info(
                        f"[JobSpy] Direct SERP empty for '{q_str}'; using enabled JobSpy fallback."
                    )
                    raw_jobs = fetch_jobs_via_jobspy(
                        [q_str], location=location, results_wanted=15
                    )
                    logger.info(
                        f"[JobSpy] Fallback returned {len(raw_jobs or [])} result(s) for '{q_str}'."
                    )
                elif not raw_jobs:
                    logger.debug(
                        f"[JobSpy] Direct SERP empty for '{q_str}'; JobSpy fallback disabled (jobspy_google off)."
                    )

                for rj in raw_jobs:
                    if cancel_token:
                        cancel_token.check()

                    comp_name = (rj.get("company_name") or "").strip()
                    j_title = (rj.get("title") or "").strip()
                    j_url = (rj.get("apply_url") or rj.get("url") or "").strip()

                    if not comp_name or not j_title or not j_url:
                        continue

                    # Preference gate (title / location / work-mode) before ingestion.
                    if pref_filter.rejection_reason(
                        j_title, rj.get("location") or location
                    ):
                        filtered_count += 1
                        continue

                    # Check for duplicate
                    exists = (
                        db.query(Job)
                        .filter(
                            (Job.url == j_url)
                            | (
                                (Job.title == j_title)
                                & (Job.location == rj.get("location"))
                            )
                        )
                        .first()
                    )
                    if exists:
                        continue

                    # Upsert company
                    comp_rec = (
                        db.query(Company).filter(Company.name.ilike(comp_name)).first()
                    )
                    if not comp_rec:
                        comp_rec = Company(
                            name=comp_name,
                            domain=comp_name.lower().replace(" ", "") + ".com",
                        )
                        db.add(comp_rec)
                        db.flush()

                    new_job = Job(
                        company_id=comp_rec.id,
                        title=j_title,
                        description=rj.get("description")
                        or f"Google for Jobs posting for {j_title} at {comp_name}.",
                        url=j_url,
                        location=rj.get("location") or location,
                        source="google_jobs",
                        source_type="Direct",
                        status="To Apply",
                        match_score=75.0,
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

        db.add(
            OperationLog(
                operation_type="google_jobs_background_scan",
                status="success",
                summary=f"Google Jobs Scan: {total_steps} queries scanned, {total_discovered} new roles discovered, {filtered_count} filtered by preferences.",
                details_json=sanitize_log_details(
                    {"filtered_out_of_preference": filtered_count}
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
        }
    finally:
        db.close()


def _run_linkedin_sync_task_runner(
    cancel_token: Optional[CancellationToken] = None,
    progress_cb: Optional[Callable] = None,
) -> Dict[str, Any]:
    """Background runner for LinkedIn Saved Jobs and Job Alerts synchronization."""
    db_gen = get_db()
    db = next(db_gen)
    total_found = 0
    # Saved jobs are explicit user intent and bypass the preference gate; alerts/recommendations do not.
    pref_filter = PreferenceFilter.from_db(db)
    alerts_filtered = 0

    try:
        # Defense in depth: the runner gates itself, so no caller (including a forced "Run now")
        # can dispatch authenticated LinkedIn automation without an explicit acknowledgment.
        if not is_feature_enabled(db, "linkedin_sync") or not has_consent(db, "linkedin_sync"):
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

        saved_count = 0
        try:
            with sync_playwright() as p:
                saved_jobs = scan_and_sync_linkedin_saved_jobs(
                    p, headless=True, auto_unsave_closed=True, max_pages=3
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
                            "Not Interested",
                        ):
                            matched_job.status = "Rejected"
                            matched_job.rejected_at = datetime.datetime.now(
                                datetime.timezone.utc
                            )
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
                            if existing.status in ("To Apply", "Not Interested"):
                                existing.status = "Shortlisted"
                        else:
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
                            job_emb = generate_embeddings(
                                f"{item.get('title', '')} {clean_job_description(item.get('description', '') or item.get('title', ''))}"
                            )
                            new_job = Job(
                                company_id=comp.id,
                                title=item.get("title", ""),
                                description=item.get("description", ""),
                                url=item.get("url", ""),
                                location=item.get("location", "Remote / Various"),
                                source="LinkedIn Saved Job",
                                source_type="LinkedIn",
                                status="Shortlisted",
                                match_score=0,
                                match_scored=False,
                                match_analysis="Saved on LinkedIn (High user interest).",
                                is_ghost_job=dd.get("is_ghost_job", False),
                                embedding=job_emb,
                            )
                            db.add(new_job)
                            saved_count += 1
                db.commit()
                total_found += saved_count
        except Exception as s_err:
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
                alerts_items = scan_linkedin_job_alerts(
                    p, headless=True, max_alerts=10, max_jobs_per_alert=10
                )
                for rec in alerts_items or []:
                    # Preference gate: alert/recommendation feeds are discovery, not explicit intent.
                    if pref_filter.rejection_reason(
                        rec.get("title", ""), rec.get("location") or ""
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
                        dismissal_match = check_semantic_dismissal(
                            rec.get("title", ""),
                            rec.get("description", ""),
                            db,
                            company_name=comp.name,
                        )
                        job_status = "Not Interested" if dismissal_match else "To Apply"
                        match_score = 0
                        match_analysis = (
                            f"Auto-filtered by semantic ignore rule"
                            if dismissal_match
                            else "Direct LinkedIn Job Alert match."
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
            logger.warning(f"LinkedIn alerts scan notice: {a_err}")

        if progress_cb:
            progress_cb(
                step=3,
                label=f"Sync completed. Ingested {total_found} postings ({alerts_filtered} filtered out).",
                items_found=total_found,
                total_steps=3,
            )

        return {
            "saved_jobs_ingested": saved_count,
            "alerts_ingested": alerts_count,
            "alerts_filtered_out_of_preference": alerts_filtered,
            "jobs_found": total_found,
        }
    finally:
        db.close()


def _run_sync_all_task_runner(
    cancel_token: Optional[CancellationToken] = None,
    progress_cb: Optional[Callable] = None,
) -> Dict[str, Any]:
    """
    Runs every enabled discovery/status stage serially (single persistent browser profile),
    tolerating partial failures. Disabled channels are skipped entirely; JobSpy stays an
    automatic fallback.
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
        db.commit()

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


@app.get("/api/tasks", response_model=List[TaskProgress])
def list_all_background_tasks(limit: int = 50):
    """Returns list of recent and active background tasks."""
    return task_engine.get_all_tasks(limit=limit)


@app.get("/api/tasks/active", response_model=List[TaskProgress])
def list_active_background_tasks():
    """Returns all currently running or queued background tasks."""
    return task_engine.get_active_tasks()


@app.get("/api/tasks/{task_id}", response_model=TaskProgress)
def get_task_status(task_id: str):
    """Retrieves status and progress of a specific background task."""
    progress = task_engine.get_task(task_id)
    if not progress:
        raise HTTPException(status_code=404, detail="Task not found")
    return progress


@app.post("/api/tasks/{task_id}/cancel")
def cancel_background_task(task_id: str):
    """Cooperatively cancels a running or queued background task."""
    success = task_engine.cancel_task(task_id)
    if not success:
        raise HTTPException(
            status_code=400,
            detail="Task could not be cancelled or has already completed.",
        )
    return {"status": "ok", "message": f"Cancellation requested for task {task_id}."}


@app.delete("/api/tasks/completed")
def clear_completed_tasks_endpoint():
    """Clears completed and cancelled background tasks from memory."""
    cleared = task_engine.clear_completed_tasks()
    return {"status": "ok", "cleared_count": cleared}


@app.get("/api/tasks/events")
async def task_events_sse(request: Request):
    """
    Server-Sent Events (SSE) stream for real-time background task progress updates.
    """
    # Bind the running loop so worker threads broadcast thread-safely.
    task_engine.set_event_loop(asyncio.get_running_loop())
    q = asyncio.Queue()
    task_engine.register_subscriber(q)

    async def event_generator():
        try:
            # Yield initial state of active tasks
            for task_prog in task_engine.get_active_tasks():
                yield f"data: {task_prog.model_dump_json()}\n\n"

            while True:
                if await request.is_disconnected():
                    break
                try:
                    # Wait for next event or heartbeat
                    event_data = await asyncio.wait_for(q.get(), timeout=15.0)
                    yield f"data: {event_data}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            task_engine.unregister_subscriber(q)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/system/unified-status")
def get_unified_system_status():
    """
    Returns unified status across Background Scraping Tasks, LLM Inference Queue, and Token Cache.
    """
    llm_status = llm_queue.get_status()
    active_tasks = task_engine.get_active_tasks()
    all_tasks = task_engine.get_all_tasks(limit=10)

    return {
        "active_background_tasks_count": len(active_tasks),
        "active_background_tasks": active_tasks,
        "recent_background_tasks": all_tasks,
        "llm_queue": llm_status,
        "server_time": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }


# -------------------------------------------------------------
# Automation Scheduler Endpoints
# -------------------------------------------------------------


class UpdateScheduleRequest(BaseModel):
    is_enabled: Optional[bool] = None
    interval_hours: Optional[float] = None


@app.get("/api/scheduler/jobs", response_model=List[ScheduledJobConfig])
def list_scheduled_jobs():
    """Returns all configured periodic automation jobs and their next scheduled run times."""
    return scheduler.get_all_jobs()


@app.patch("/api/scheduler/jobs/{job_id}", response_model=ScheduledJobConfig)
def update_scheduled_job(job_id: str, req: UpdateScheduleRequest):
    """Updates a periodic job schedule (e.g. enable/disable, change interval)."""
    updated = scheduler.update_job(
        job_id, is_enabled=req.is_enabled, interval_hours=req.interval_hours
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Scheduled job not found")
    return updated


@app.post("/api/scheduler/jobs/{job_id}/run")
def trigger_scheduled_job_now(job_id: str):
    """Triggers immediate on-demand background execution of a scheduled job."""
    task_id = scheduler.run_job_now(job_id, force=True)
    if not task_id:
        reason = scheduler.last_skip_reason()
        if reason:
            # Policy skip (e.g. consent not given) — the job exists but is intentionally blocked.
            raise HTTPException(
                status_code=403,
                detail=f"Cannot run '{job_id}': {reason}.",
            )
        raise HTTPException(
            status_code=404, detail="Scheduled job not found or cannot be triggered."
        )
    return {
        "status": "ok",
        "job_id": job_id,
        "task_id": task_id,
        "message": f"Triggered scheduled job {job_id} as background task {task_id}.",
    }


# -------------------------------------------------------------
# Frontend Static Files Mount
# -------------------------------------------------------------
frontend_dir = BASE_DIR / "frontend"

if frontend_dir.exists():
    app.mount("/", StaticFiles(directory=str(frontend_dir), html=True), name="static")
