# Project Structure & Reference

Reference material for this repository: what the modules are, what the database holds, which
packages are in play, and how to run, test and debug it. Section numbers are preserved from the
`AGENTS.md` they were split out of, so existing references (`PROJECT_STRUCTURE.md` §9, §12.6, …)
resolve to the same content.

- Behavioural rules for agents and contributors live in [`AGENTS.md`](AGENTS.md).
- Design rationale, policies and invariants live in [`DESIGN.md`](DESIGN.md).
- The file-by-file index with code examples is [`PROJECT_INDEX.md`](PROJECT_INDEX.md).
- Diagrams are in [`DESIGN_DIAGRAMS.md`](DESIGN_DIAGRAMS.md).

## Contents

| Section | Covers |
| :--- | :--- |
| §1 | Core architecture and key modules |
| §5 | Schema and migration conventions |
| §8 | Package and dependency inventory |
| §9 | Database schema reference |
| §12 | Developer onboarding: setup, configuration, tests, file map, troubleshooting |

---

## 1. Core Architecture & Key Modules

- **Backend API (`backend/main.py`)**: FastAPI application exposing REST endpoints for jobs, companies, resumes, application tracker, Playwright automation, Q&A semantic memory, skills alignment, hiring teams, **structured resume↔job match analysis** (`POST /api/match/analyze`), **interview-question generation** (`POST /api/jobs/{job_id}/interview-questions`), and **one-shot sync** (`POST /api/tasks/sync/all`).
- **Background Tasks & Scheduler (`backend/task_engine.py`, `backend/scheduler.py`)**: Asynchronous non-blocking task engine with real-time SSE progress streaming (`/api/tasks/events`), cooperative cancellation (`CancellationToken`), and periodic in-process cron daemon.
- **LLM Engine & Worker Queue (`backend/ai_helper.py`, `backend/llm_queue.py`)**:
  - Local-first architecture: LM Studio (`:1234`), Unsloth (`:8008`), Ollama (`:11434`), and opt-in cloud fallback (OpenAI, Gemini, Anthropic Claude).
  - Dynamic Concurrency Dispatch: 1 worker (0.2s pacing) for local models to prevent VRAM thrashing; scales to 8 parallel workers (0.0s pacing) for cloud APIs.
  - Semantic Q&A Memory Bank & Zero-Token Filtering (`check_semantic_dismissal`, `search_qa_memory`, `extract_job_skills_and_alignment`).
- **Deterministic Scoring Internals (`backend/match_classifier.py`, `matchers.py`, `match_scoring.py`)**: `build_deterministic_assessment` extracts JD requirements (skills/years/seniority/education) and scores them against the parsed resume with **no LLM call**; `band_and_recommendation` (`backend/match_scoring.py`, driven by the persisted `ScoringConfig`) owns band/recommendation so thresholds stay configurable.
  - **Embeddings (`backend/embeddings.py`)**: pluggable, fixed 384-d (`SafeVector(384)`) local-first chain — in-process ONNX `standalone` (`bge-small-en-v1.5`) → OpenAI-compatible `external` (`/v1/embeddings`: Ollama/LM Studio/Unsloth/vLLM/llama.cpp/custom) → deterministic `hash` last resort. Config `EMBEDDING_BACKEND` (`auto|standalone|external|ollama|hash`), `EMBEDDING_MODEL`, `EMBEDDING_SERVER_URL`/`EMBEDDING_SERVER_MODEL`, `EMBEDDING_ALLOW_DOWNLOAD`, `EMBEDDING_CACHE_DIR`; persisted in `data/embedding_settings.json` (env overrides). Changing the **model** keeps the dimension but requires re-embedding existing rows.
  - **Section intent (`backend/section_intent.py`)**: one heading/section classifier used by the scorer, parser, and preference filter — `classify_heading(line, domain="jd"|"resume"|"must_have")`, `jd_section_spans`/`label_at`/`mask_spans`, `is_must_have_heading`, `starts_with_preference_cue`. Word-boundary matching; preference cues are line-start-only.
  - **Skill taxonomy (`backend/skill_taxonomy.py` + `data/skill_taxonomy.json`)**: data-driven canonical skill → aliases + concepts (alias-aware JD matching); `parser.COMMON_TECH_SKILLS` is sourced from it. Unmatched phrases are recorded to `data/skill_taxonomy_candidates.json` for review and are **never scored**.
  - **Residual requirements (`backend/requirements_extractor.py`)**: mines requirement units the taxonomy missed and credits those the resume evidences (`extract_residual_requirements`).
  - **Resume RAG-lite (`backend/resume_index.py`)**: `chunk_resume`/`index_resume_chunks` persist skills/role/project chunks into `resume_chunks`; `top_evidence` supplies grounding excerpts. Scoring never depends on embeddings — they only ground the narrative.
  - **Graceful shutdown (`backend/shutdown.py`)**: bounded ASGI-lifecycle drain — see `DESIGN.md` §12.11.
- **Web Scraping & Due Diligence (`backend/scraper.py`)**: Scrapes Greenhouse, Lever, Ashby, Uber, and custom career portals. Performs due diligence to flag phantom/ghost jobs and unwrap shortened recruitment URLs.
- **Browser Automation (`backend/playwright_app.py`)**: Persistent browser context (`~/.job-alert-agent/chrome_profile/`), LinkedIn alerts/recommendations/saved jobs sync with auto-prune for closed roles, and assisted application overlay.
- **Observability Layer (`backend/observability.py`)**: Dual-sink structured logging (colored console + JSON file `logs/app.log`), `X-Request-ID` middleware, and real-time log inspection API (`/api/logs`). Logs rotate via `LOG_MAX_BYTES`/`LOG_BACKUP_COUNT` across the app sink and a dedicated `llm_audit.log` sink, and `OperationLog` rows are pruned by `prune_operation_logs()` (`OPERATION_LOG_MAX_ROWS`/`OPERATION_LOG_RETENTION_DAYS`).
- **Database Layer (`backend/database.py`)**: SQLAlchemy ORM models (`Job`, `Company`, `Resume`, `ApplicationEvent`, `OperationLog`, `ApplicationQuestionAnswer`, `DismissedJobPattern`, `ProcessedEmail`, `EmailApplicationLink`, `PendingEmailApplication`, `UserPreference`, `CustomFormField`). SQLite is the default; PostgreSQL (`pgvector`) is opt-in via `DATABASE_URL`.
- **Preference Filter (`backend/preference_filter.py`)**: Reusable `PreferenceFilter`/`PreferenceCriteria` (title/location/work-mode, incl. broad-region rejection) shared by ATS, Google Jobs, LinkedIn, and Gmail discovery scans. `backend/scraper.py` loads optional, user-supplied employer aliases from `data/company_aliases.json` (empty by default).
- **Frontend SPA & Chrome Extension (`frontend/`, `extension/`)**: Vanilla JS SPA with real-time token & cost analytics, application lifecycle tracker, preferences dashboard, and Manifest V3 Chrome Extension side panel.

---

## 5. Database Schema & Migration Rules

- **Automatic Schema Migration**: When adding or updating columns in SQLAlchemy models, include lightweight auto-migration (`ALTER TABLE`) logic in `init_db()` in `backend/database.py` to synchronize existing SQLite/PostgreSQL databases without data loss.
- **ORM Querying First (No Blind Raw SQL)**: Always query via SQLAlchemy ORM models (`db.query(OperationLog).all()`, `db.query(Job)...`) or inspect `backend/database.py` rather than guessing raw column names in ad-hoc CLI scripts.
- **Canonical Model Schema Reference**:
  - `Job`: `id`, `company_id`, `title`, `description`, `url`, `salary_range`, `location`, `source`, `source_type`, `status` (`To Apply`, `Shortlisted`, `Applied`, `Screening`, `Interview`, `Offered`, `Rejected`, `Not Interested`), `match_score`, `score_method` (`classifier_v1` = deterministic fast path, `requirement_coverage_v1` = LLM fallback, `NULL` = unscored), `match_scored_at`, `match_band`, `apply_recommendation` (the band/recommendation trio stamped at scoring time; all serialized by `JobOut`), `match_analysis`, `match_strengths_json`, `match_gaps_json`, `match_partials_json` (structured Resume Gap Insights lists, NULL for legacy/unscored rows), `cover_letter_draft`, `tailored_resume_points`, `cold_message_draft`, `is_ghost_job`, `repost_count`, `match_scored`, `sync_batch_id` (LinkedIn sync review marker, cleared on any status change), `submission_confirmed`, `created_at`, `applied_at`, `rejected_at`, `interview_scheduled_at`, `embedding`.
  - `Company`: `id`, `name`, `domain`, `careers_url`, `description`, `reviews_summary`, `salary_insights`, `hiring_process`, `recent_news`, `created_at`, `updated_at`.
  - `Resume`: `id`, `filename`, `content_encrypted`, `parsed_json_encrypted`, `embedding`, `is_active`, `created_at`, `updated_at`.
  - `ApplicationEvent`: `id`, `job_id`, `event_type`, `description`, `timestamp` (aliased as `created_at`).
  - `OperationLog`: `id`, `operation_type`, `status`, `summary`, `details_json`, `jobs_count`, `prompt_tokens`, `completion_tokens`, `created_at` (aliased as `timestamp`).
  - `ApplicationQuestionAnswer`: `id`, `question_text`, `answer_text` (encrypted property), `category`, `embedding`, `use_count`, `created_at`, `updated_at`.
  - `DismissedJobPattern`: `id`, `title`, `description`, `reason`, `embedding`, `created_at`.
  - `CustomFormField`: `id`, `ats_type`, `domain`, `field_name`, `field_id`, `field_label`, `field_type`, `is_recognized`, `classified_category`, `occurrence_count`, `created_at`, `updated_at`.

---

## 8. Package & Dependency Inventory

### 8.1 Python runtime dependencies (`requirements.txt`)

| Package | Purpose in this project | Where it's used |
| :--- | :--- | :--- |
| `fastapi` | HTTP API framework, dependency injection, Pydantic models | `backend/main.py` |
| `uvicorn` | ASGI server (`uvicorn backend.main:app`); `uvicorn[standard]` adds the `watchfiles` watcher needed for safe `--reload` | CLI |
| `sqlalchemy` | ORM models, engine/session management, migrations | `backend/database.py`, all DB access |
| `psycopg2-binary` | PostgreSQL driver | PostgreSQL `DATABASE_URL` |
| `pgvector` | Native 384-d vector column + indexed similarity search | `SafeVector` when the URL is PostgreSQL |
| `playwright` | Persistent-context browser automation (LinkedIn/Gmail/ATS) | `backend/playwright_app.py` |
| `pypdf` | PDF text extraction (default, BSD-3 licensed) | `backend/parser.py::extract_text_from_pdf` |
| `ollama` | Declared client for local Ollama; current code talks to Ollama's HTTP API via `requests` | `backend/ai_helper.py` (`generate_embeddings`) |
| `cryptography` | Fernet (AES-128-CBC + HMAC) encryption at rest | `backend/config.py`, `backend/database.py` |
| `python-multipart` | Resume file upload parsing | `POST /api/resume/upload` |
| `requests` | Direct ATS/HTTP scraping, provider REST calls, short-URL unwrap | `backend/scraper.py`, `backend/ai_helper.py` |
| `beautifulsoup4` | HTML parsing for custom portals / search results | `backend/scraper.py` |
| `httpx` | HTTP client used by Starlette `TestClient` in tests | `tests/`, `conftest.py` |
| `pytest` | Test runner | `tests/`, `pytest.ini` |
| `google-auth`, `google-auth-oauthlib`, `google-api-python-client` | Opt-in BYOK Gmail API (OAuth + metadata fetch) | `backend/gmail_client.py` |
| `geonamescache` | Offline city gazetteer for location filtering | `backend/geo.py` |
| `pycountry` | ISO-3166 country resolution | `backend/geo.py` |

**Optional / lazy imports** (code degrades gracefully when absent): `pgvector` (falls back to SQLite JSON vectors), `google-*` (Gmail API unavailable → browser-session fallback), `geonamescache`/
`pycountry` (falls back to curated aliases in `data/location_aliases.json`).

**Notable stdlib building blocks**: `threading` (task engine, LLM queue, scheduler, profile lock),
`asyncio` (SSE queues), `queue.PriorityQueue` (LLM dispatch), `concurrent.futures` (bounded
batch matching), `secrets.compare_digest` (constant-time auth), `hashlib` (offline embeddings),
`logging.handlers.RotatingFileHandler` (log rotation).

### 8.2 Frontend & extension

There is **no build step**. The SPA is vanilla HTML/CSS/JS; the extension is Chrome Manifest V3.

| Technology | Role |
| :--- | :--- |
| Vanilla JS / HTML5 / CSS3 | Dashboard SPA (`frontend/`) — dark glassmorphism UI |
| Chrome MV3 APIs (`sidePanel`, `storage`, `tabs`, `scripting`, `activeTab`) | Extension side panel + content scripts |
| `chrome.storage.local` | Local candidate profile, Q&A bank, connection settings, form-field catalog |
| Chrome built-in **Gemini Nano** (`window.ai.languageModel`) | Standalone (zero-backend) on-device generation in `extension/js/runtime-adapter.js` |
| Browser `MutationObserver` / intervals | Live DOM autofill re-scan in `extension/content/ats-autofill.js` |

> Earlier design notes mentioned "Transformers.js / IndexedDB" for standalone mode; the
> **implemented** standalone path is Gemini Nano + `chrome.storage.local` (no Wasm embeddings,
> no IndexedDB).

---

## 9. Database Schema Reference (Detailed)

Defined in `backend/database.py` (SQLAlchemy 2.x declarative). Engine selection happens in
`init_db()`: try `DATABASE_URL` (PostgreSQL), else fall back to SQLite
(`job_alert_agent.db`). On PostgreSQL it attempts `CREATE EXTENSION IF NOT EXISTS vector`.
Lightweight `ALTER TABLE` auto-migrations sync older databases without data loss, and legacy
plaintext Q&A answers are encrypted in place on startup. Sessions are provided to routes via the
`get_db()` dependency.

`init_db()` runs once on the app's **startup lifecycle**, never at module import — importing
`backend.main` must not touch a database. Maintenance scripts that need `SessionLocal` call
`backend.database.init_db()` themselves.

### 9.1 `SafeVector` type — cross-dialect embeddings

```python
class SafeVector(TypeDecorator):
    impl = Text
    cache_ok = True

    def __init__(self, dimensions=384): ...

    # PostgreSQL -> pgvector.sqlalchemy.Vector(384); SQLite -> JSON-serialized Text
```

All embeddings are 384-d. On SQLite they are stored as JSON text and compared in Python
(O(n)); on PostgreSQL they map to native `vector` columns for indexed search.

### 9.2 Tables

**`companies`** — one row per employer.

| Column | Type | Notes |
| :--- | :--- | :--- |
| `id` | Integer PK, indexed | |
| `name` | String(255), **unique**, indexed, not null | Join key / dedup key |
| `domain` | String(255) | Auto-derived from portal URLs |
| `careers_url` | String(512) | Auto-upgraded when a direct ATS link is discovered |
| `description`, `reviews_summary`, `salary_insights`, `hiring_process`, `recent_news` | Text | Company-intelligence synthesis |
| `created_at`, `updated_at` | DateTime (UTC) | `updated_at` auto-onupdate |
| **Relationship** | | `jobs` → `Job` (cascade delete-orphan) |

**`jobs`** — the pipeline record and the richest table.

| Column | Type | Notes |
| :--- | :--- | :--- |
| `id` | Integer PK, indexed | |
| `company_id` | FK → `companies.id` (ON DELETE CASCADE), not null | |
| `title` | String(255), indexed, not null | |
| `description` | Text | Raw JD (cleaned at prompt time) |
| `url` | String(512) | Canonical listing/apply URL (dedup key) |
| `salary_range`, `location` | String | |
| `source` | String(100) | e.g. `LinkedIn Job Alert`, `Greenhouse Portal`, `Google Jobs (query)` |
| `source_type` | String(50), default `Direct` | `LinkedIn` triggers ghost-job logic |
| `status` | String(50), indexed, default `To Apply` | Canonical set below |
| `match_score` | Float | 0–100 ATS alignment |
| `match_analysis` | Text | Strengths/gaps/feedback (flattened narrative) |
| `match_strengths_json`, `match_gaps_json`, `match_partials_json` | Text (JSON) | Structured Resume Gap Insights lists, persisted at scoring time; NULL for legacy/unscored rows (exposed as `Job.match_strengths` / `Job.match_gaps` / `Job.match_partials` properties). `match_partials_json` holds must-have requirements met partially (some resume evidence, not full coverage) — surfaced as a dedicated "Partial Coverage" panel. |
| `cover_letter_draft`, `tailored_resume_points`, `cold_message_draft` | Text | On-demand materials |
| `embedding` | SafeVector(384) | Semantic search |
| `is_ghost_job` | Boolean, default False | Due-diligence flag |
| `repost_count` | Integer, default 0 | ≥3 flags a probable ghost job |
| `match_scored_at` | DateTime | When the score was last (re)computed; `NULL` for unscored rows |
| `match_band` | String(50) | Band label from the shared `ScoringConfig` (`band_and_recommendation`) — not hardcoded thresholds |
| `apply_recommendation` | String(50) | e.g. `apply` / `apply_with_caution` / `skip`, from the same `ScoringConfig` |
| `sync_batch_id` | String(64) | Set on jobs newly ingested by a LinkedIn sync run; cleared on any status change (drives the "new from sync" review banner) |
| `submission_confirmed` | Boolean, default False | **Human confirmation gate** |
| `match_scored` | Boolean, default False | True only when a real AI analysis produced `match_score`; discovery baselines stay False and the UI shows “not AI-scored” |
| `applied_at`, `interview_scheduled_at`, `rejected_at` | DateTime | Lifecycle timeline |
| `created_at`, `updated_at` | DateTime (UTC) | |
| **Relationship** | | `company` → `Company`; `events` → `ApplicationEvent` (cascade) |

Canonical `status` values (`JOB_STATUSES`): `To Apply`, `Shortlisted`, `Applied`, `Screening`,
`Interview`, `Offered`, `Rejected`, `Not Interested`.

**`resumes`** — encrypted candidate documents.

| Column | Type | Notes |
| :--- | :--- | :--- |
| `id` | Integer PK | |
| `filename` | String(255), not null | |
| `content_encrypted` | Text, not null | Fernet-encrypted raw resume text |
| `parsed_json_encrypted` | Text | Fernet-encrypted ATS JSON (skills, history) |
| `embedding` | SafeVector(384) | Resume vector |
| `is_active` | Boolean, default True | Only one active profile is used |
| `created_at`, `updated_at` | DateTime | |

**`resume_chunks`** — RAG-lite embedded resume fragments.

| Column | Type | Notes |
| :--- | :--- | :--- |
| `id` | Integer PK, indexed | |
| `resume_id` | FK → `resumes.id` (CASCADE), indexed, not null | |
| `section` | String(50), default `other` | `skills` \| `role` \| `project` \| `education` |
| `text` | Text, not null | Chunk body (skills block, one role, or one project) |
| `embedding` | SafeVector(384), nullable | Used by `resume_index.top_evidence` for grounding excerpts; scoring never depends on it |
| `created_at` | DateTime (UTC) | |

Written by `backend/resume_index.py` (`index_resume_chunks`); (re)indexed on resume upload and on
skill edits. These are derived data — safe to delete and rebuild.

**`application_events`** — append-only audit trail per job.

| Column | Type | Notes |
| :--- | :--- | :--- |
| `id` | Integer PK | |
| `job_id` | FK → `jobs.id` (CASCADE), not null | |
| `event_type` | String(100), not null | e.g. `status_change`, `note`, `interview`, `email_received` |
| `description` | Text | Human-readable detail |
| `timestamp` | DateTime, default now | **also aliased as `created_at`** (`synonym`) |

**`operation_logs`** — operation audit + token accounting.

| Column | Type | Notes |
| :--- | :--- | :--- |
| `id` | Integer PK | |
| `operation_type` | String(100), indexed, not null | e.g. `linkedin_sync`, `ats_scrape`, `bulk_apply` |
| `status` | String(50), default `Completed` | `Completed` / `Partial` / `Failed` |
| `summary` | Text, not null | |
| `details_json` | Text | Sanitized structured diagnostics |
| `jobs_count` | Integer, default 0 | |
| `prompt_tokens`, `completion_tokens` | Integer, default 0 | Per-operation LLM cost |
| `created_at` | DateTime | **also aliased as `timestamp`** |

**`application_question_answers`** — semantic Q&A memory bank.

| Column | Type | Notes |
| :--- | :--- | :--- |
| `id` | Integer PK | |
| `question_text` | Text, not null | |
| `_answer_text` (DB column `answer_text`) | Text, not null | Stores **Fernet ciphertext** |
| `answer_text` | synonym property | Transparent encrypt-on-set / decrypt-on-get |
| `category` | String(100), default `general` | e.g. `technical`, `behavioral`, `salary` |
| `embedding` | SafeVector(384) | For cache lookup |
| `use_count` | Integer, default 1 | Incremented on cache hits |
| `created_at`, `updated_at` | DateTime | |

**`dismissed_job_patterns`** — negative role memory (zero-token ignore filter).

| Column | Type | Notes |
| :--- | :--- | :--- |
| `id` | Integer PK | |
| `title` | String(255), not null | |
| `description` | Text | |
| `reason` | String(255) | e.g. `User marked as Not Interested` |
| `embedding` | SafeVector(384) | Dismissal matching |
| `created_at` | DateTime | |

**`custom_form_fields`** — extension form-field telemetry.

| Column | Type | Notes |
| :--- | :--- | :--- |
| `id` | Integer PK | |
| `ats_type` | String(100), indexed, default `custom` | |
| `domain` | String(255), indexed, not null | |
| `field_name`, `field_id`, `field_label`, `field_type` | String/Text | Observed field metadata |
| `is_recognized` | Boolean, default False | Filled automatically? |
| `classified_category` | String(100), default `unknown` | |
| `occurrence_count` | Integer, default 1 | Aggregate telemetry counter |
| `created_at`, `updated_at` | DateTime | |

**`processed_emails`** — Gmail API dedup.

| Column | Type | Notes |
| :--- | :--- | :--- |
| `id` | Integer PK | |
| `message_id` | String(255), unique, indexed, not null | Gmail message id |
| `processed_at` | DateTime, default now | |

**`user_preferences`** — single-row discovery preferences + feature flags.

| Column | Type | Notes |
| :--- | :--- | :--- |
| `id` | Integer PK | Single logical row |
| `target_titles`, `target_locations`, `target_cities` | Text | Comma-separated |
| `work_mode` | String(50), default `all` | `all`/`remote`/`onsite`/`hybrid` |
| `target_country` | String(100) | |
| `timezone` | String(64) | IANA tz; `""` = browser default |
| `features_json` | Text, default `{}` | Opt-in flags (JSON) |
| `scoring_config_json` | Text (JSON) | Persisted `ScoringConfig` overrides (band/recommendation thresholds, weights). `get_scoring_config` merges over defaults; `ScoringConfig.from_mapping` is how scorers read it |
| `consents_json` | Text (JSON) | Risk/ToS acknowledgments keyed by feature. Stamped server-side at `CONSENT_VERSION` (`record_consent`); `CONSENT_REQUIRED_FEATURES` cannot be enabled without a valid ack (`has_consent`) |
| `extra_skills_json` | Text (JSON) | User-owned extra skills merged on top of resume-derived skills for **every** scoring path (`_merge_extra_skills`); case-insensitively deduped |
| `skip_topics` | Text | Comma-separated topic blocklist used by the dismissal filter |
| `excluded_companies` | Text, default `DEFAULT_EXCLUDED_COMPANIES` | Comma-separated employer/domain blocklist (never scanned) |
| `updated_at` | DateTime | |

`DEFAULT_FEATURE_FLAGS` (opt-in; unknown keys ignored): `ats_portals=True`, `google_jobs=False`,
`linkedin_sync=False`, `gmail_sync=False`, `ats_hirist=True`,
`resume_gap_insights=True` (UI insight surface, not a discovery channel). Helpers:
`get_user_preferences`, `get_feature_flags`, `is_feature_enabled`.

### 9.3 Entity relationships (text)

```text
Company 1 ────< Job 1 ────< ApplicationEvent
                   │
                   ├── embedding : SafeVector(384)
                   └── matches DismissedJobPattern (by embedding similarity, not FK)
ApplicationQuestionAnswer : standalone semantic cache (embedding-searched)
Resume : standalone active profile (encrypted content + embedding)
ResumeChunk : derived resume fragments (FK → Resume; rebuildable)
ProcessedEmail : standalone Gmail dedup (message_id only — it does NOT link to a job)
EmailApplicationLink : message_id (unique) → Job, + thread_id / method / confidence / sender /
                       subject. The attribution record: one mail can never attach to two
                       applications, and correcting a mistake is an update, not an appended note.
CustomFormField : standalone telemetry (aggregated by domain/field)
UserPreference : singleton config + feature flags
```

---

## 12. Developer Onboarding Guide

### 12.1 Prerequisites

- Python 3.10+ and `git`.
- Optional but recommended: a local inference server (LM Studio `:1234`, Unsloth `:8008`, or
  Ollama `:11434`). The app also runs without one (deterministic fallbacks + zero-token filters).
- Optional: PostgreSQL + `pgvector` for large databases.
- Google Chrome only if you want to load the extension.

### 12.2 Setup

```bash
git clone <repo-url> && cd job-alert-agent

python3 -m venv .venv
source .venv/bin/activate                 # macOS/Linux
pip install -r requirements.txt
playwright install chromium               # required for browser automation

# Run the server (plain, recommended — stable across long sessions)
uvicorn backend.main:app --host 0.0.0.0 --port 8000

# Optional hot reload (development only). uvicorn's default fallback reloader
# (StatReload) can hang on startup for this repo: without `watchfiles` it stat-polls
# every .py under the cwd (≈6k files in .venv alone) and the child worker can stall
# before the port is released — symptoms: dashboard freezes, `GET /api/health`
# times out, console shows "Started reloader process" but never "Application startup
# complete". To use --reload safely:
#   1. pip install "uvicorn[standard]"          # adds the watchfiles watcher
#   2. Scope the watcher to app code only:
uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload --reload-dir backend
```

Open `http://localhost:8000`. Verify readiness with `GET /api/health` and provider detection with
`GET /api/llm/status`. If a `--reload` server ever hangs (health times out but the process is
alive), kill it and restart with the plain command above.

### 12.3 Configuration (all optional)

Copy the keys you need into a root `.env` (defaults work out of the box):

```env
# Database: SQLite is the default (zero-config). Set DATABASE_URL to a postgresql:// URL
# to opt into PostgreSQL (with pgvector) for large job sets.
DATABASE_URL=sqlite:///./job_alert_agent.db   # or postgresql://user:pass@host:5432/job_alert_agent
DEFAULT_LLM_PROVIDER=auto
LM_STUDIO_BASE_URL=http://localhost:1234/v1
OLLAMA_BASE_URL=http://localhost:11434
ALLOW_CLOUD_FALLBACK=false                    # cloud inference stays off unless enabled
# OPENAI_API_KEY= / GEMINI_API_KEY= / ANTHROPIC_API_KEY= are also overridable via Settings (encrypted)

# LLM transient-failure retry policy (429/5xx/conn/timeout). Overridable per task
# via TASK_PROFILES["<task>"]["max_retries"] in backend/ai_helper.py.
LLM_MAX_RETRIES=3
LLM_BACKOFF_BASE=0.5
LLM_BACKOFF_MAX=5.0
LLM_RETRY_JITTER=true

# LLM audit-trail observability (LangSmith-analogue, no external dependency).
# trace_id links all LLM calls in one operation in logs/llm_audit.log. Content capture
# is OFF by default (prompts may contain PII such as resume text).
LLM_AUDIT_CAPTURE_CONTENT=false
LLM_AUDIT_MAX_CONTENT_CHARS=500

# Career Copilot quality Judge gate (open-ended reply scoring + bounded regeneration).
# Adds one extra LLM call per copilot turn, so it is skippable for strict token budgets.
COPILOT_JUDGE_ENABLED=true
COPILOT_JUDGE_MAX_REGEN=1

# Google for Jobs browser mode. Google serves an empty Jobs vertical to chrome-headless-shell,
# so the default is a real browser window; "new_headless" opts into full Chromium headless.
GOOGLE_JOBS_BROWSER_MODE=headful

# Structured log & operation-log retention (optional)
LOG_LEVEL=INFO
LOG_MAX_BYTES=10485760
LOG_BACKUP_COUNT=5
OPERATION_LOG_MAX_ROWS=1000
OPERATION_LOG_RETENTION_DAYS=30
OPERATION_LOG_PRUNE_INTERVAL_SECONDS=3600
```

Runtime files created on first run (not committed): `.key`, `.api_key`, `data/*.json` (prefs,
token stats, scheduler config, encrypted BYOK/Gmail settings), `logs/app.log`,
`job_alert_agent.db`, and `~/.job-alert-agent/chrome_profile/`.

### 12.4 Load the Chrome extension (optional)

1. `chrome://extensions/` → enable **Developer mode** → **Load unpacked** → select `extension/`.
2. Pin it, then open the side panel on an ATS/LinkedIn job page. It auto-detects whether the
   backend is running (Connected Mode) or falls back to on-device Gemini Nano (Standalone Mode).

### 12.5 Verification & tests

```bash
./.venv/bin/pytest -v tests/          # full suite
./.venv/bin/pytest -v tests/test_api.py   # single module
```

`pytest.ini` defines a `live` marker for tests that call a real local LLM
(`RUN_LIVE_LLM_TESTS=1`), excluded from the normal run. `conftest.py` provides autouse isolation:

- `setup_test_db` — in-memory SQLite, tables created/dropped per test.
- `isolate_token_tracker_stats` — redirects token stats to a temp file.
- `isolate_llm_settings_file` — redirects LLM/Gmail settings to temp files.
- `prevent_real_browser_spawns` — mocks Playwright and isolates the Chrome profile dir.
- `db_session` — a fresh session for DB-backed tests.
- `override_get_db` wires the test DB into the FastAPI app; `test_client` is a ready `TestClient`.

**Never** spawn real browsers or hit live external networks in unit tests.

### 12.6 Where things live

| Need to change… | File |
| :--- | :--- |
| API routes, CORS, auth, task endpoints | `backend/main.py` (app wiring) + `backend/routers/*.py` (extracted domains: `operations`, `tasks`, `memory_qa`, `settings`, `discovery`, `linkedin`, `google_jobs`). Task *dispatch* endpoints (`/api/tasks/...`) deliberately stay in `main`. |
| LLM providers, routing, embeddings, caching | `backend/ai_helper.py` |
| Central LLM system prompts (match, interview, judge, copilot) | `backend/prompts.py` |
| Transient-failure retry/backoff (429/5xx/conn/timeout) | `backend/llm_resilience.py` (wired via `ai_helper._execute_generate_text`) |
| Quality Judge gate (`judge_output`) + `JudgeVerdict` | `backend/ai_helper.py` (wired into `chat_agent.py`) |
| Queue concurrency/pacing | `backend/llm_queue.py` |
| ORM models / migrations | `backend/database.py` |
| Secrets, encryption, provider/Gmail config | `backend/config.py` |
| Scraping, ghost detection, geo/URL helpers | `backend/scraper.py`, `backend/geo.py` |
| Browser automation, LinkedIn/Gmail sync, autofill overlay | `backend/playwright_app.py` |
| Resume/PDF/JD parsing | `backend/parser.py` |
| Background tasks / SSE | `backend/task_engine.py` |
| Periodic scheduler | `backend/scheduler.py` |
| Logging / telemetry (rotation + `OperationLog` pruning) | `backend/observability.py`, `backend/database.py` |
| Embedding backends (pluggable, 384-d contract) | `backend/embeddings.py`; config in `backend/config.py`; `data/embedding_settings.json` |
| Section-intent classification (headings/sections, ATS header chrome, inline "preferred") | `backend/section_intent.py` |
| Merged-title/employer parsing (LinkedIn cards, repair scripts) | `backend/title_parser.py`, `data/company_synonyms.json` |
| Requisition-id extraction (ATS URLs + application mails) | `backend/requisition.py` |
| Per-channel discovery outcomes + run status honesty | `backend/channel_status.py` |
| Config-driven ATS portals (shapes + per-portal variants) | `backend/ats/engine.py`, `data/ats_portals.json` |
| Skill taxonomy + review queue | `backend/skill_taxonomy.py`, `data/skill_taxonomy.json`, `data/skill_taxonomy_candidates.json` |
| Country → city hub suggestions (SPA) | `backend/country_hubs.py`, `data/country_hubs.json` (served in `GET /api/preferences`) |
| Residual JD requirement mining | `backend/requirements_extractor.py` |
| Resume chunking / RAG-lite evidence | `backend/resume_index.py` |
| Graceful shutdown / drain order | `backend/shutdown.py` (wired in `backend/main.py`) |
| Structured match analysis & interview prep | `backend/main.py` (`/api/match/analyze`, `/api/jobs/{job_id}/interview-questions`), `backend/ai_helper.py` |
| Shared preference filtering | `backend/preference_filter.py` |
| Copilot dialogue + action dispatch | `backend/chat_agent.py` |
| SPA dashboard | `frontend/app.js`, `index.html`, `styles.css` |
| Extension runtime / autofill / side panel | `extension/js/runtime-adapter.js`, `content/ats-autofill.js`, `sidepanel/sidepanel.js` |

See [`PROJECT_INDEX.md`](PROJECT_INDEX.md) for the full file-by-file index with code examples.

### 12.7 Your first change (TDD walkthrough)

1. **Research** the touchpoints (grep for the function; check `PROJECT_INDEX.md`).
2. **Write/extend a test** in `tests/` using `db_session`/`test_client`; assert happy path + a boundary.
3. **Implement** incrementally; keep changes in the owning module.
4. **Run the full suite**: `./.venv/bin/pytest -v tests/`. Zero regressions required.
5. If you touched a model, add an `ALTER TABLE` step in `init_db()`.
6. Update the relevant doc (`DESIGN_DIAGRAMS.md`, `AGENTIC_ARCHITECTURE.md`, or this file) and
   `docs/README.md` if you add a concept.

### 12.8 Debugging & observability

- `GET /api/logs?level=ERROR&search=foo&limit=200` tails the JSON log; `GET /api/operations/logs`
  returns persisted `OperationLog` history.
- `GET /api/llm/queue-status` shows queue depth, concurrency, backpressure, and estimated wait.
- `GET /api/llm/token-usage` reports O(1) token/cost stats; `POST .../sync` and `.../recalculate`
  reconcile with the DB.
- Every request gets an `X-Request-ID` echoed in logs (console + JSON).

### 12.9 Troubleshooting

| Symptom | Likely cause / fix |
| :--- | :--- |
| "Local AI model is offline" copilot replies | No local server running; start LM Studio/Ollama or enable cloud fallback |
| Server hangs after a reload (health times out, console shows "Started reloader process" but no "Application startup complete") | `--reload` without `watchfiles` (StatReload fallback) stalls the worker, often with a stale socket still bound to :8000. Kill the process (`pkill -f "uvicorn backend.main:app"`), verify the port is free (`lsof -nP -iTCP:8000 -sTCP:LISTEN`), and restart plain or with `uvicorn[standard]` + `--reload-dir backend` (see §12.2) |
| `BrowserProfileBusy` | Another task/process holds the profile lock; wait or stop the other task |
| `Unauthorized: Valid X-API-Key...` | Extension/REST caller missing `X-API-Key`; use the key from `.api_key` (or same-origin) |
| Semantic results look weak | No Ollama embeddings running; the deterministic hash fallback is lower quality |
| Slow semantic queries | SQLite O(n) vectors; switch to PostgreSQL + `pgvector` |
| Gmail sync does nothing | Gmail is opt-in and/or not connected; see `docs/GMAIL_API.md` |
