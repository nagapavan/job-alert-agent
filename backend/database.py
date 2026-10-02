import os
import json
import logging
import datetime
from sqlalchemy import (
    create_engine, Column, Integer, String, Text, 
    DateTime, Float, ForeignKey, Boolean, TypeDecorator
)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship, synonym
from backend.config import DATABASE_URL, SQLITE_URL, encrypt_data, decrypt_data, cipher

logger = logging.getLogger("database")

Base = declarative_base()

# Operation-log retention: bound table growth on long-running deployments.
# Rows older than the retention window are dropped, then the table is capped to max_rows (oldest first).
OPERATION_LOG_MAX_ROWS = int(os.environ.get("OPERATION_LOG_MAX_ROWS", "1000"))
OPERATION_LOG_RETENTION_DAYS = int(os.environ.get("OPERATION_LOG_RETENTION_DAYS", "30"))

# -------------------------------------------------------------
# SafeVector Type Decorator (PostgreSQL pgvector / SQLite JSON)
# -------------------------------------------------------------
class SafeVector(TypeDecorator):
    """
    Stores vector embeddings.
    In PostgreSQL: maps to pgvector.sqlalchemy.Vector.
    In SQLite: maps to Text (JSON serialized float list).
    """
    impl = Text
    cache_ok = True

    def __init__(self, dimensions=384):
        super().__init__()
        self.dimensions = dimensions

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            try:
                from pgvector.sqlalchemy import Vector
                return dialect.type_descriptor(Vector(self.dimensions))
            except ImportError:
                pass
        return dialect.type_descriptor(Text)

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if dialect.name == "postgresql":
            return value
        return json.dumps(value)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if dialect.name == "postgresql":
            return value
        try:
            return json.loads(value)
        except Exception:
            return []

# -------------------------------------------------------------
def utc_now():
    return datetime.datetime.now(datetime.timezone.utc)

# -------------------------------------------------------------
# Models
# -------------------------------------------------------------
class Company(Base):
    __tablename__ = "companies"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), unique=True, nullable=False, index=True)
    domain = Column(String(255), nullable=True)
    careers_url = Column(String(512), nullable=True)
    description = Column(Text, nullable=True)
    reviews_summary = Column(Text, nullable=True)
    salary_insights = Column(Text, nullable=True)
    hiring_process = Column(Text, nullable=True)
    recent_news = Column(Text, nullable=True)
    market_position = Column(Text, nullable=True)
    talking_points = Column(Text, nullable=True)
    created_at = Column(DateTime, default=utc_now)
    updated_at = Column(DateTime, default=utc_now, onupdate=utc_now)

    # Relationships
    jobs = relationship("Job", back_populates="company", cascade="all, delete-orphan")

class Resume(Base):
    __tablename__ = "resumes"

    id = Column(Integer, primary_key=True, index=True)
    filename = Column(String(255), nullable=False)
    content_encrypted = Column(Text, nullable=False)
    parsed_json_encrypted = Column(Text, nullable=True)
    embedding = Column(SafeVector(384), nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=utc_now)
    updated_at = Column(DateTime, default=utc_now, onupdate=utc_now)


class ResumeChunk(Base):
    """Embedded resume fragments (skills block, each role, each project) used for
    RAG-lite evidence retrieval during requirement assessment."""

    __tablename__ = "resume_chunks"

    id = Column(Integer, primary_key=True, index=True)
    resume_id = Column(
        Integer, ForeignKey("resumes.id", ondelete="CASCADE"), index=True, nullable=False
    )
    section = Column(String(50), default="other")   # skills | role | project | education
    text = Column(Text, nullable=False)
    embedding = Column(SafeVector(384), nullable=True)
    created_at = Column(DateTime, default=utc_now)


class Job(Base):
    __tablename__ = "jobs"

    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(Integer, ForeignKey("companies.id", ondelete="CASCADE"), nullable=False)
    title = Column(String(255), nullable=False, index=True)
    description = Column(Text, nullable=True)
    url = Column(String(512), nullable=True)
    salary_range = Column(String(255), nullable=True)
    location = Column(String(255), nullable=True)
    source = Column(String(100), nullable=True)  # e.g., "LinkedIn Job Alert", "Greenhouse", "Direct"
    source_type = Column(String(50), default="Direct")  # "LinkedIn" or "Direct"
    status = Column(String(50), default="To Apply", index=True)  # Canonical: To Apply, Shortlisted, Applied, Screening, Interview, Offered, Rejected, Not Interested
    
    # AI Scoring & Tailoring
    match_score = Column(Float, nullable=True)
    match_analysis = Column(Text, nullable=True)
    cover_letter_draft = Column(Text, nullable=True)
    tailored_resume_points = Column(Text, nullable=True)
    cold_message_draft = Column(Text, nullable=True)
    embedding = Column(SafeVector(384), nullable=True)
    
    # Due Diligence / Ghost Job Detection
    is_ghost_job = Column(Boolean, default=False)
    repost_count = Column(Integer, default=0)

    # Assisted application: True only once the candidate confirms actual submission
    submission_confirmed = Column(Boolean, default=False)

    # True only when match_score came from a real AI analysis (vs. a source/baseline/saved default)
    match_scored = Column(Boolean, default=False)
    # Which scoring engine produced match_score (null for legacy/baseline scores) + when.
    score_method = Column(String(50), nullable=True)
    match_scored_at = Column(DateTime, nullable=True)
    # Deterministic engine's qualitative result (for UI display).
    match_band = Column(String(20), nullable=True)
    apply_recommendation = Column(String(30), nullable=True)

    # Timeline Dates
    applied_at = Column(DateTime, nullable=True)
    interview_scheduled_at = Column(DateTime, nullable=True)
    rejected_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=utc_now)
    updated_at = Column(DateTime, default=utc_now, onupdate=utc_now)

    # Relationships
    company = relationship("Company", back_populates="jobs")
    events = relationship("ApplicationEvent", back_populates="job", cascade="all, delete-orphan")

class ApplicationEvent(Base):
    __tablename__ = "application_events"

    id = Column(Integer, primary_key=True, index=True)
    job_id = Column(Integer, ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False)
    event_type = Column(String(100), nullable=False)  # "status_change", "note", "interview", "email_received"
    description = Column(Text, nullable=True)
    timestamp = Column(DateTime, default=utc_now)
    created_at = synonym("timestamp")

    # Relationships
    job = relationship("Job", back_populates="events")

class OperationLog(Base):
    __tablename__ = "operation_logs"

    id = Column(Integer, primary_key=True, index=True)
    operation_type = Column(String(100), nullable=False, index=True)  # "linkedin_sync", "ats_scrape", "bulk_apply", "assisted_apply", "cache_clean"
    status = Column(String(50), default="Completed")  # "Completed", "Partial", "Failed"
    summary = Column(Text, nullable=False)
    details_json = Column(Text, nullable=True)
    jobs_count = Column(Integer, default=0)
    prompt_tokens = Column(Integer, default=0)
    completion_tokens = Column(Integer, default=0)
    created_at = Column(DateTime, default=utc_now, index=True)
    timestamp = synonym("created_at")

class ApplicationQuestionAnswer(Base):
    """
    Semantic Memory for application essay and screener questions (e.g. Workday, Greenhouse, Lever).
    Stores question-answer pairs with embeddings for 1-click retrieval and adaptation.
    Answer content is encrypted at rest using Fernet symmetric encryption.
    """
    __tablename__ = "application_question_answers"

    id = Column(Integer, primary_key=True, index=True)
    question_text = Column(Text, nullable=False)
    _answer_text = Column("answer_text", Text, nullable=False)
    category = Column(String(100), default="general")  # "technical", "behavioral", "experience", "salary", "logistics"
    embedding = Column(SafeVector(384), nullable=True)
    use_count = Column(Integer, default=1)
    created_at = Column(DateTime, default=utc_now)
    updated_at = Column(DateTime, default=utc_now, onupdate=utc_now)

    def _get_answer_text(self) -> str:
        return decrypt_data(self._answer_text) if self._answer_text else ""

    def _set_answer_text(self, value: str):
        if not value:
            self._answer_text = ""
        else:
            self._answer_text = encrypt_data(value)

    answer_text = synonym("_answer_text", descriptor=property(_get_answer_text, _set_answer_text))

class DismissedJobPattern(Base):
    """
    Negative Role Memory: stores embeddings of jobs marked as 'Not Interested' or 'Ignored'.
    Enables zero-token early semantic filtering for future discovery and scrapers.
    """
    __tablename__ = "dismissed_job_patterns"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(255), nullable=False)
    company_name = Column(String(255), nullable=True, index=True)  # scope the dismissal to one employer
    description = Column(Text, nullable=True)
    reason = Column(String(255), nullable=True)
    embedding = Column(SafeVector(384), nullable=True)
    created_at = Column(DateTime, default=utc_now)

class CustomFormField(Base):
    """
    Form Field Telemetry: Tracks recognized and unique custom form fields across company career portals
    to improve autofill heuristics and learn portal-specific screener patterns.
    """
    __tablename__ = "custom_form_fields"

    id = Column(Integer, primary_key=True, index=True)
    ats_type = Column(String(100), default="custom", index=True)
    domain = Column(String(255), nullable=False, index=True)
    field_name = Column(String(255), nullable=True)
    field_id = Column(String(255), nullable=True)
    field_label = Column(Text, nullable=True)
    field_type = Column(String(50), default="text")
    is_recognized = Column(Boolean, default=False)
    classified_category = Column(String(100), default="unknown")
    occurrence_count = Column(Integer, default=1)
    created_at = Column(DateTime, default=utc_now)
    updated_at = Column(DateTime, default=utc_now, onupdate=utc_now)

class ProcessedEmail(Base):
    """
    Tracks Gmail API message ids already ingested, so the targeted Gmail sync never
    reprocesses the same email (dedup for the opt-in Gmail API flow).
    """
    __tablename__ = "processed_emails"

    id = Column(Integer, primary_key=True, index=True)
    message_id = Column(String(255), unique=True, index=True, nullable=False)
    processed_at = Column(DateTime, default=utc_now)


class UserPreference(Base):
    """
    Single-row candidate search preferences + opt-in discovery feature flags.
    Persisted server-side so manual, scheduled, and background discovery scans all honor
    the same user-configured criteria (instead of deriving targets from the resume).
    """
    __tablename__ = "user_preferences"

    id = Column(Integer, primary_key=True, index=True)
    target_titles = Column(Text, default="")       # comma-separated roles
    target_locations = Column(Text, default="")    # comma-separated countries/regions
    target_cities = Column(Text, default="")       # comma-separated cities
    work_mode = Column(String(50), default="all")  # all | remote | onsite | hybrid
    target_country = Column(String(100), default="")
    timezone = Column(String(64), default="")      # IANA tz (e.g. Asia/Kolkata); "" = browser default
    features_json = Column(Text, default="{}")     # opt-in feature flags (JSON)
    scoring_config_json = Column(Text, default="{}")  # tunable match-scoring weights/cutoffs (JSON)
    consents_json = Column(Text, default="{}")     # explicit per-feature consent records (JSON, versioned)
    excluded_companies = Column(Text, default="amazon")  # comma-separated name/domain blocklist
    updated_at = Column(DateTime, default=utc_now, onupdate=utc_now)


# Default employer/domain blocklist (substring match, lowercased). Companies whose name or
# domain contains any token are never scanned. Seeded with Amazon: its Conditions of Use and
# Agent Policy explicitly restrict automated/agent access, so it must not be scraped by default.
DEFAULT_EXCLUDED_COMPANIES = "amazon"


# Opt-in discovery features. Disabled channels are hidden in the UI and skipped by the scheduler.
#
# Risk tiers drive the defaults:
#   * Public, documented ATS APIs (Greenhouse/Lever/Ashby) -> on.
#   * Unofficial/ambiguous endpoints (SmartRecruiters/Workable/Workday/Uber/custom HTML) and
#     session-driven channels (LinkedIn/Gmail) -> OFF; require explicit opt-in consent.
DEFAULT_FEATURE_FLAGS = {
    # ---- Master discovery channels ----
    "ats_portals": True,
    "google_jobs": True,
    "linkedin_sync": False,   # session-based LinkedIn DOM automation (ToS / account risk)
    "gmail_sync": False,      # reads Gmail (opt-in, privacy-scoped)
    "jobspy_google": False,   # unofficial Google Jobs aggregator (off by default)
    # ---- Per-portal ATS scanners (public, documented APIs) ----
    "ats_greenhouse": True,
    "ats_lever": True,
    "ats_ashby": True,
    # ---- Candidate-portal application-status sync (browser session) ----
    "ats_hirist": True,
    # ---- Unofficial / ambiguous endpoints: opt-in with consent ----
    "ats_smartrecruiters": False,
    "ats_workable": False,
    "ats_workday": False,
    "ats_uber": False,
    "ats_custom_html": False,
}


# -------------------------------------------------------------
# Explicit consent records (versioned)
# -------------------------------------------------------------
# Enabling these features is not just an opt-in toggle: the user must explicitly
# acknowledge the associated ToS / account / privacy risk. Consent is stored separately
# from the feature flags and versioned, so changing the terms can force re-acknowledgment
# (a stale version no longer counts as consent).
CONSENT_VERSION = 1

CONSENT_REQUIRED_FEATURES = (
    "linkedin_sync",        # authenticated LinkedIn DOM automation (ToS / account-ban risk)
    "jobspy_google",        # unofficial Google Jobs aggregator
    "ats_smartrecruiters",  # unofficial / ambiguous ATS endpoints
    "ats_workable",
    "ats_workday",
    "ats_uber",
    "ats_custom_html",
)


def get_user_preferences(db) -> "UserPreference":
    """Returns the singleton preference record, creating it if absent."""
    pref = db.query(UserPreference).first()
    if not pref:
        pref = UserPreference()
        db.add(pref)
        db.commit()
        db.refresh(pref)
    return pref


def get_feature_flags(db) -> dict:
    """Returns the effective opt-in feature flags (unknown keys ignored)."""
    pref = get_user_preferences(db)
    try:
        stored = json.loads(pref.features_json or "{}")
    except Exception:
        stored = {}
    flags = dict(DEFAULT_FEATURE_FLAGS)
    for key in DEFAULT_FEATURE_FLAGS:
        if key in stored:
            flags[key] = bool(stored[key])
    return flags


def is_feature_enabled(db, feature: str) -> bool:
    """True when an opt-in discovery feature is enabled."""
    return bool(get_feature_flags(db).get(feature, False))


# Tunable match-scoring parameters (mirrors backend.match_scoring.ScoringConfig defaults).
DEFAULT_SCORING_CONFIG = {
    "must_weight": 3.0,
    "nice_weight": 1.0,
    "must_floor": 0.5,
    "cap_when_below_floor": 55.0,
    "must_blend": 0.7,
    "nice_blend": 0.3,
    "strong": 85.0,
    "good": 70.0,
    "moderate": 55.0,
    "weak": 40.0,
}


def get_scoring_config(db) -> dict:
    """Returns the effective match-scoring config (stored overrides merged over defaults)."""
    pref = get_user_preferences(db)
    try:
        stored = json.loads(pref.scoring_config_json or "{}")
    except Exception:
        stored = {}
    config = dict(DEFAULT_SCORING_CONFIG)
    for key in DEFAULT_SCORING_CONFIG:
        if key in stored:
            try:
                config[key] = float(stored[key])
            except (TypeError, ValueError):
                pass
    return config


def get_consents(db) -> dict:
    """Returns the raw per-feature consent records (JSON-decoded)."""
    pref = get_user_preferences(db)
    try:
        return json.loads(pref.consents_json or "{}")
    except Exception:
        return {}


def has_consent(db, key: str) -> bool:
    """
    True only when the user explicitly acknowledged the current consent version for a feature.

    A record with a missing/older ``version`` does not count, so bumping ``CONSENT_VERSION``
    forces re-acknowledgment.
    """
    record = get_consents(db).get(key)
    if not isinstance(record, dict):
        return False
    try:
        version = int(record.get("version", 0) or 0)
    except (TypeError, ValueError):
        return False
    return bool(record.get("ack")) and version == CONSENT_VERSION


def record_consent(db, key: str, commit: bool = True) -> None:
    """
    Stamps an acknowledged consent record for a feature at the current version.

    Pass ``commit=False`` when recording as part of a larger transaction (e.g. the
    preferences update) so a later validation failure rolls the whole request back
    instead of persisting a partial write.
    """
    pref = get_user_preferences(db)
    consents = get_consents(db)
    consents[key] = {
        "ack": True,
        "version": CONSENT_VERSION,
        "at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    pref.consents_json = json.dumps(consents)
    if commit:
        db.commit()
    else:
        db.flush()


def get_excluded_companies(db) -> list:
    """Returns the normalized (lowercased) employer/domain blocklist tokens."""
    pref = get_user_preferences(db)
    raw = pref.excluded_companies
    if raw is None:
        raw = DEFAULT_EXCLUDED_COMPANIES
    return [c.strip().lower() for c in str(raw).split(",") if c.strip()]


def is_company_excluded(name: str, domain: str, db) -> bool:
    """True when a company's name/domain matches any blocklist token (substring, case-insensitive)."""
    tokens = get_excluded_companies(db)
    if not tokens:
        return False
    haystack = f"{(name or '').lower()} {(domain or '').lower()}"
    return any(tok in haystack for tok in tokens)


engine = None
SessionLocal = None

def init_db(custom_url: str = None):
    global engine, SessionLocal
    target_url = custom_url or DATABASE_URL
    
    if not custom_url:
        try:
            test_engine = create_engine(target_url, pool_pre_ping=True)
            with test_engine.connect() as conn:
                pass
            engine = test_engine
        except Exception:
            engine = create_engine(SQLITE_URL, connect_args={"check_same_thread": False})
    else:
        connect_args = {"check_same_thread": False} if "sqlite" in target_url else {}
        engine = create_engine(target_url, connect_args=connect_args)

    # Try creating vector extension if PostgreSQL
    if engine.dialect.name == "postgresql":
        try:
            from sqlalchemy import text
            with engine.begin() as conn:
                conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector;"))
        except Exception:
            pass

    Base.metadata.create_all(bind=engine)

    # Lightweight schema auto-migration for existing SQLite/PostgreSQL databases
    try:
        from sqlalchemy import inspect, text
        inspector = inspect(engine)
        existing_tables = inspector.get_table_names()

        if "operation_logs" in existing_tables:
            col_names = {c["name"] for c in inspector.get_columns("operation_logs")}
            with engine.begin() as conn:
                if "prompt_tokens" not in col_names:
                    conn.execute(text("ALTER TABLE operation_logs ADD COLUMN prompt_tokens INTEGER DEFAULT 0;"))
                if "completion_tokens" not in col_names:
                    conn.execute(text("ALTER TABLE operation_logs ADD COLUMN completion_tokens INTEGER DEFAULT 0;"))
                if "timestamp" not in col_names and "created_at" in col_names:
                    conn.execute(text("ALTER TABLE operation_logs ADD COLUMN timestamp DATETIME;"))
                    conn.execute(text("UPDATE operation_logs SET timestamp = created_at WHERE timestamp IS NULL;"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_operation_logs_created_at ON operation_logs (created_at);"))

        if "application_events" in existing_tables:
            col_names = {c["name"] for c in inspector.get_columns("application_events")}
            with engine.begin() as conn:
                if "created_at" not in col_names and "timestamp" in col_names:
                    conn.execute(text("ALTER TABLE application_events ADD COLUMN created_at DATETIME;"))
                    conn.execute(text("UPDATE application_events SET created_at = timestamp WHERE created_at IS NULL;"))

        if "jobs" in existing_tables:
            col_names = {c["name"] for c in inspector.get_columns("jobs")}
            with engine.begin() as conn:
                if "is_ghost_job" not in col_names:
                    conn.execute(text("ALTER TABLE jobs ADD COLUMN is_ghost_job BOOLEAN DEFAULT 0;"))
                if "repost_count" not in col_names:
                    conn.execute(text("ALTER TABLE jobs ADD COLUMN repost_count INTEGER DEFAULT 0;"))
                if "source_type" not in col_names:
                    conn.execute(text("ALTER TABLE jobs ADD COLUMN source_type VARCHAR(50) DEFAULT 'Direct';"))
                if "cold_message_draft" not in col_names:
                    conn.execute(text("ALTER TABLE jobs ADD COLUMN cold_message_draft TEXT;"))
                if "embedding" not in col_names:
                    conn.execute(text("ALTER TABLE jobs ADD COLUMN embedding BLOB;"))
                if "submission_confirmed" not in col_names:
                    conn.execute(text("ALTER TABLE jobs ADD COLUMN submission_confirmed BOOLEAN DEFAULT 0;"))
                if "match_scored" not in col_names:
                    conn.execute(text("ALTER TABLE jobs ADD COLUMN match_scored BOOLEAN DEFAULT 0;"))
                    # Backfill legacy rows whose score/analysis came from the AI analyzer
                    conn.execute(text("UPDATE jobs SET match_scored = 1 WHERE match_analysis LIKE 'Strengths:%';"))
                if "score_method" not in col_names:
                    conn.execute(text("ALTER TABLE jobs ADD COLUMN score_method VARCHAR(50);"))
                if "match_scored_at" not in col_names:
                    conn.execute(text("ALTER TABLE jobs ADD COLUMN match_scored_at DATETIME;"))
                if "match_band" not in col_names:
                    conn.execute(text("ALTER TABLE jobs ADD COLUMN match_band VARCHAR(20);"))
                if "apply_recommendation" not in col_names:
                    conn.execute(text("ALTER TABLE jobs ADD COLUMN apply_recommendation VARCHAR(30);"))

        if "companies" in existing_tables:
            col_names = {c["name"] for c in inspector.get_columns("companies")}
            with engine.begin() as conn:
                if "market_position" not in col_names:
                    conn.execute(text("ALTER TABLE companies ADD COLUMN market_position TEXT;"))
                if "talking_points" not in col_names:
                    conn.execute(text("ALTER TABLE companies ADD COLUMN talking_points TEXT;"))

        if "user_preferences" in existing_tables:
            col_names = {c["name"] for c in inspector.get_columns("user_preferences")}
            with engine.begin() as conn:
                if "timezone" not in col_names:
                    conn.execute(text("ALTER TABLE user_preferences ADD COLUMN timezone VARCHAR(64) DEFAULT '';"))
                if "excluded_companies" not in col_names:
                    conn.execute(text("ALTER TABLE user_preferences ADD COLUMN excluded_companies TEXT DEFAULT 'amazon';"))
                if "consents_json" not in col_names:
                    conn.execute(text("ALTER TABLE user_preferences ADD COLUMN consents_json TEXT DEFAULT '{}';"))
                if "scoring_config_json" not in col_names:
                    conn.execute(text("ALTER TABLE user_preferences ADD COLUMN scoring_config_json TEXT DEFAULT '{}';"))

        if "dismissed_job_patterns" in existing_tables:
            col_names = {c["name"] for c in inspector.get_columns("dismissed_job_patterns")}
            with engine.begin() as conn:
                if "company_name" not in col_names:
                    conn.execute(text("ALTER TABLE dismissed_job_patterns ADD COLUMN company_name VARCHAR(255);"))
    except Exception as mig_err:
        logger.warning(f"Schema auto-migration skipped: {mig_err}")

    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    # Secure file permissions on browser profile directory
    try:
        from backend.config import CHROME_PROFILE_DIR as profile_dir
        if profile_dir.exists():
            import os
            os.chmod(profile_dir, 0o700)
    except Exception:
        pass

    # Encrypt legacy plaintext Q&A answers.
    # NOTE: No candidate-specific application answers are seeded by default. Personal facts
    # such as work authorization, visa sponsorship, current employment, and notice period
    # are private and must be captured from the user (onboarding / profile / Q&A bank);
    # the application must never assert them on the user's behalf.
    try:
        db = SessionLocal()
        try:
            existing_qas = db.query(ApplicationQuestionAnswer).all()
            updated_count = 0
            for qa in existing_qas:
                raw_val = qa._answer_text
                if raw_val:
                    try:
                        cipher.decrypt(raw_val.encode("utf-8"))
                    except Exception:
                        # It's plaintext, encrypt it
                        qa.answer_text = raw_val
                        updated_count += 1
            if updated_count > 0:
                db.commit()
        finally:
            db.close()
    except Exception:
        pass

    return engine

def get_db():
    """FastAPI database session dependency."""
    global SessionLocal
    if SessionLocal is None:
        init_db()
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def prune_operation_logs(db, max_rows: int = None, retention_days: int = None) -> int:
    """
    Rotates the operation_logs table so it never grows unbounded:
      1. Deletes rows older than the retention window.
      2. Caps the table to max_rows, deleting the oldest rows beyond the cap.

    Safe to call periodically. It removes only the OLDEST rows (lowest ids) which are already
    reflected in the persisted cumulative token counters, so token stats are not lost.
    Returns the number of pruned rows.
    """
    max_rows = OPERATION_LOG_MAX_ROWS if max_rows is None else max_rows
    retention_days = OPERATION_LOG_RETENTION_DAYS if retention_days is None else retention_days
    deleted = 0
    try:
        if retention_days and retention_days > 0:
            cutoff = utc_now() - datetime.timedelta(days=retention_days)
            deleted += db.query(OperationLog).filter(
                OperationLog.created_at < cutoff
            ).delete(synchronize_session=False)

        if max_rows and max_rows > 0:
            total = db.query(OperationLog).count()
            if total > max_rows:
                excess = total - max_rows
                old_ids = [
                    row[0] for row in
                    db.query(OperationLog.id)
                      .order_by(OperationLog.created_at.asc())
                      .limit(excess).all()
                ]
                if old_ids:
                    deleted += db.query(OperationLog).filter(
                        OperationLog.id.in_(old_ids)
                    ).delete(synchronize_session=False)

        if deleted:
            db.commit()
            logger.info(
                f"Operation-log rotation pruned {deleted} row(s) "
                f"(max_rows={max_rows}, retention_days={retention_days})."
            )
        else:
            db.rollback()
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        logger.warning(f"Operation-log rotation failed: {e}")
    return deleted
