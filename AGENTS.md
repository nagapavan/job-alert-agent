# Agent & Developer Guidelines (LLM Tools & Coding Assistants)

## 0. Core Engineering Workflow & Ground Rules
Every feature request, enhancement, or non-trivial bugfix must follow the structured 5-phase lifecycle:
1. **Phase 1: Multi-Dimensional Research & Assessment**:
   - **Functionality**: Define exact inputs, expected outputs, state changes, edge cases, and boundary constraints.
   - **Viability**: Validate technical feasibility against existing codebase architecture, SDK/library capabilities, rate limits, performance/VRAM pacing, and privacy boundaries.
   - **Usability**: Evaluate end-user ergonomics, UI friction, visual feedback, clear error states, and intuitive interactions (across Frontend SPA, Browser Extension, or Chat Agent).
2. **Phase 2: Spec Generation & Planning**:
   - Formulate a clear technical specification / implementation plan only after Functionality, Viability, and Usability all check out.
   - Define exact data contracts, API payloads, schema additions, and component touchpoints.
3. **Phase 3: TDD-Based Incremental Implementation**:
   - Write / update unit and integration test assertions first (or alongside atomic steps) covering happy paths and boundary conditions.
   - Implement code incrementally, verifying passing status at each atomic step.
4. **Phase 4: Side-Effect & Regression Verification**:
   - Run the full test suite (`./.venv/bin/pytest -v tests/` and frontend/extension JS checks) to guarantee zero regressions or unintended side-effects.
5. **Phase 5: Walkthrough & User Sign-Off**:
   - Document changes made, validation results, and clear instructions on how to use/test the feature.

---

## 1. Core Architecture & Key Modules
- **Backend API (`backend/main.py`)**: FastAPI application exposing REST endpoints for jobs, companies, resumes, application tracker, Playwright automation, Q&A semantic memory, skills alignment, hiring teams, **structured resume↔job match analysis** (`POST /api/match/analyze`), **interview-question generation** (`POST /api/jobs/{job_id}/interview-questions`), and **one-shot sync** (`POST /api/tasks/sync/all`).
- **Background Tasks & Scheduler (`backend/task_engine.py`, `backend/scheduler.py`)**: Asynchronous non-blocking task engine with real-time SSE progress streaming (`/api/tasks/events`), cooperative cancellation (`CancellationToken`), and periodic in-process cron daemon.
- **LLM Engine & Worker Queue (`backend/ai_helper.py`, `backend/llm_queue.py`)**:
  - Local-first architecture: LM Studio (`:1234`), Unsloth (`:8008`), Ollama (`:11434`), and opt-in cloud fallback (OpenAI, Gemini, Anthropic Claude).
  - Dynamic Concurrency Dispatch: 1 worker (0.2s pacing) for local models to prevent VRAM thrashing; scales to 8 parallel workers (0.0s pacing) for cloud APIs.
  - Semantic Q&A Memory Bank & Zero-Token Filtering (`check_semantic_dismissal`, `search_qa_memory`, `extract_job_skills_and_alignment`).
- **Web Scraping & Due Diligence (`backend/scraper.py`)**: Scrapes Greenhouse, Lever, Ashby, Uber, and custom career portals. Performs due diligence to flag phantom/ghost jobs and unwrap shortened recruitment URLs.
- **Browser Automation (`backend/playwright_app.py`)**: Persistent browser context (`~/.job-alert-agent/chrome_profile/`), LinkedIn alerts/recommendations/saved jobs sync with auto-prune for closed roles, and assisted application overlay.
- **Observability Layer (`backend/observability.py`)**: Dual-sink structured logging (colored console + JSON file `logs/app.log`), `X-Request-ID` middleware, and real-time log inspection API (`/api/logs`). Logs rotate via `LOG_MAX_BYTES`/`LOG_BACKUP_COUNT` across the app sink and a dedicated `llm_audit.log` sink, and `OperationLog` rows are pruned by `prune_operation_logs()` (`OPERATION_LOG_MAX_ROWS`/`OPERATION_LOG_RETENTION_DAYS`).
- **Database Layer (`backend/database.py`)**: SQLAlchemy ORM models (`Job`, `Company`, `Resume`, `ApplicationEvent`, `OperationLog`, `ApplicationQuestionAnswer`, `DismissedJobPattern`, `ProcessedEmail`, `UserPreference`, `CustomFormField`). Supports SQLite and PostgreSQL (`pgvector`).
- **Preference Filter (`backend/preference_filter.py`)**: Reusable `PreferenceFilter`/`PreferenceCriteria` (title/location/work-mode, incl. broad-region rejection) shared by ATS, Google Jobs, LinkedIn, and Gmail discovery scans. `backend/scraper.py` loads optional, user-supplied employer aliases from `data/company_aliases.json` (empty by default).
- **Frontend SPA & Chrome Extension (`frontend/`, `extension/`)**: Vanilla JS SPA with real-time token & cost analytics, application lifecycle tracker, preferences dashboard, and Manifest V3 Chrome Extension side panel.

---

## 2. Code Editing & File Integrity Rules
- **Never Push Without Explicit Approval**: Do NOT run `git push` or otherwise publish to any remote unless the user explicitly asks. Local commits and branches are fine.
- **Active File Targeting**: If changes belong to the active file, modify only that file. Otherwise, update the existing relevant file.
- **No Unnecessary Files**: Do not create new files or folders unless explicitly requested or approved by the user.
- **No Over-Engineering Documentation**: Do NOT auto-generate explanatory docs, checklists, alternative analyses, or reference files unless explicitly requested. Make the code changes only. If the user asks "should we do X?", wait for their decision—do not create docs exploring options they didn't ask for.
- **Concise Communication**: Provide only what was requested. Avoid verbose summaries, unnecessary walkthroughs, or "for future reference" guides.
- **Pydantic V2 Syntax**: Always use Pydantic V2 conventions (e.g. `model_config = ConfigDict(from_attributes=True)` instead of v1 `class Config`).
- **Preserve Comments & Docstrings**: Maintain existing docstrings, type annotations, and code styling patterns.

---

## 3. Token Optimization & LLM Conventions
- **Discovery Pacing & Token Saver**: During background scraping or LinkedIn sync, **never pre-generate full cover letters or cold outreach messages synchronously**. Only compute match alignment scores. Heavy application materials must be generated on-demand when the user clicks "⚡ Generate Materials" or launches "Apply".
- **Zero-Token Semantic Ignore Filter**: Always evaluate `check_semantic_dismissal` before running AI match prompts on newly discovered roles to skip unneeded LLM inference.
- **LinkedIn Saved Jobs Full-Fidelity Ingestion**: Saved jobs represent explicit user intent. Always ingest active saved jobs **as-is** with status `"Shortlisted"`, bypassing negative dismissal filters, and auto-upgrade `Company.careers_url` if an external ATS portal link is present.

---

## 4. Third-Party Libraries & Context7 MCP
- **Context7 Documentation Querying**: Always use Context7 MCP (`resolve-library-id` and `query-docs`) whenever writing code involving third-party frameworks, SDKs, or APIs (e.g., Playwright, SQLAlchemy, FastAPI, Pydantic, Requests) to verify current API signatures and prevent version deprecation bugs.

---

## 5. Database Schema & Migration Rules
- **Automatic Schema Migration**: When adding or updating columns in SQLAlchemy models, include lightweight auto-migration (`ALTER TABLE`) logic in `init_db()` in `backend/database.py` to synchronize existing SQLite/PostgreSQL databases without data loss.
- **ORM Querying First (No Blind Raw SQL)**: Always query via SQLAlchemy ORM models (`db.query(OperationLog).all()`, `db.query(Job)...`) or inspect `backend/database.py` rather than guessing raw column names in ad-hoc CLI scripts.
- **Canonical Model Schema Reference**:
  - `Job`: `id`, `company_id`, `title`, `description`, `url`, `salary_range`, `location`, `source`, `source_type`, `status` (`To Apply`, `Shortlisted`, `Applied`, `Screening`, `Interview`, `Offered`, `Rejected`, `Not Interested`), `match_score`, `match_analysis`, `cover_letter_draft`, `tailored_resume_points`, `cold_message_draft`, `is_ghost_job`, `repost_count`, `match_scored`, `created_at`, `applied_at`, `rejected_at`, `interview_scheduled_at`, `embedding`.
  - `Company`: `id`, `name`, `domain`, `careers_url`, `description`, `reviews_summary`, `salary_insights`, `hiring_process`, `recent_news`, `created_at`, `updated_at`.
  - `Resume`: `id`, `filename`, `content_encrypted`, `parsed_json_encrypted`, `embedding`, `is_active`, `created_at`, `updated_at`.
  - `ApplicationEvent`: `id`, `job_id`, `event_type`, `description`, `timestamp` (aliased as `created_at`).
  - `OperationLog`: `id`, `operation_type`, `status`, `summary`, `details_json`, `jobs_count`, `prompt_tokens`, `completion_tokens`, `created_at` (aliased as `timestamp`).
  - `ApplicationQuestionAnswer`: `id`, `question_text`, `answer_text` (encrypted property), `category`, `embedding`, `use_count`, `created_at`, `updated_at`.
  - `DismissedJobPattern`: `id`, `title`, `description`, `reason`, `embedding`, `created_at`.
  - `CustomFormField`: `id`, `ats_type`, `domain`, `field_name`, `field_id`, `field_label`, `field_type`, `is_recognized`, `classified_category`, `occurrence_count`, `created_at`, `updated_at`.

---

## 6. Code Execution & Automated Testing
- **Virtual Environment**: Use the activated `.venv` environment (`./.venv/bin/python`, `./.venv/bin/pytest`).
- **Always Run Pytest**: Run `./.venv/bin/pytest -v tests/` after modifying code to verify that all unit, API, and integration tests pass.
- **Database Test Isolation**: Use the `db_session` fixture from `conftest.py` for any database-backed unit tests.
- **Mock Browser & Network Calls in Tests**: Never spawn un-mocked Playwright browser instances or make live external network calls during unit tests (rely on `prevent_real_browser_spawns` and mock responses).

---

## 7. Privacy & Opt-in Guardrails

- **Server-persisted user preferences**: Discovery preferences live in the single-row `UserPreference` table (`GET/PUT /api/preferences`). Manual **and** background/scheduled runners must read these (not derive targets solely from the resume). A missing value falls back to resume-derived data only as a last resort.
- **Opt-in feature flags** (`UserPreference.features_json`, defaults in `backend/database.py::DEFAULT_FEATURE_FLAGS`): master channels `ats_portals`, `google_jobs`, `linkedin_sync` (**off**), `gmail_sync` (**off**), `jobspy_google` (**off**); the `ats_hirist` candidate-portal status-sync flag (**on**); per-portal ATS flags `ats_greenhouse`/`ats_lever`/`ats_ashby` (**on** — public, documented APIs) and `ats_smartrecruiters`/`ats_workable`/`ats_workday`/`ats_uber`/`ats_custom_html` (**off** — unofficial/ambiguous endpoints, require explicit opt-in consent). Disabled channels are hidden in the SPA and **skipped server-side** (`trigger_jobs_scrape`, `sync_external_applications`; scheduler automatic runs; explicit "Run now" passes `force=True`).
- **Employer/domain blocklist** (`UserPreference.excluded_companies`, default `"amazon"`): a company whose name/domain matches any comma-separated token is never scanned (`backend.database.is_company_excluded`). Amazon is seeded because its Conditions of Use and Agent Policy explicitly restrict automated/agent access.
- **Per-portal risk tiers & disclaimers**: scanning/scraping follows each site's terms; see [`docs/ATS_PORTALS.md`](docs/ATS_PORTALS.md) §"Risk tiers, opt-in & consent". This is **not legal advice**; the user is responsible for compliance with each site's terms of service.
- **Opportunity Radar (`GET /api/opportunities/radar`)**: structured, ranked hiring-intent feed derived from already-ingested jobs (LinkedIn alerts/recommendations, ATS portals, Google Jobs, Gmail recruiter outreach). Scored deterministically by `compute_hiring_intent` in `backend/main.py` — no LLM calls and no LinkedIn content-post scraping (the unofficial `/api/linkedin/search-hiring-posts` path was removed).
- **Gmail is opt-in and privacy-scoped** (amends the original "no OAuth tokens" guardrail as an explicit opt-in exception):
  - Preferred path: **BYOK Gmail API** (`backend/gmail_client.py`) — narrow `q`, `metadata`+snippet only, `maxResults`/page caps, `messageId` dedup (`ProcessedEmail`), encrypted client secret + refresh token (`data/gmail_settings.json`, 0600). **Never enumerate the full mailbox.**
  - Fallback: restricted browser-session scanner (`scan_gmail_for_job_alerts` — targeted search only; no full-inbox / category scans).
- **BYOK secrets encrypted at rest**: LLM keys (`save_llm_settings`) and Gmail OAuth credentials (`save_gmail_settings`) are Fernet-encrypted; env vars always override persisted values. `/api/logs` requires auth; `/api/gmail/oauth2callback` is the only Gmail-related public path (loopback).
- **Single-owner browser profile**: `get_persistent_browser_context` raises `BrowserProfileBusy` rather than deleting live locks or launching a logged-out context; `clean_stale_profile_locks` is PID-aware. Background tasks skip when the profile is in use.
- **Prompt-injection boundary**: scraped/external text is wrapped with `wrap_untrusted`; generated text is passed through `sanitize_generated_text` before display/injection. The LLM never submits applications (human confirms via `submission_confirmed`).
- **Documentation map**: see `docs/README.md`; feature plans in `docs/` (e.g. `docs/GMAIL_API.md`, `docs/antigravity/`).

---

## 8. Package & Dependency Inventory

### 8.1 Python runtime dependencies (`requirements.txt`)

| Package | Purpose in this project | Where it's used |
| :--- | :--- | :--- |
| `fastapi` | HTTP API framework, dependency injection, Pydantic models | `backend/main.py` |
| `uvicorn` | ASGI server (`uvicorn backend.main:app`) | `run.py` / CLI |
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
| `python-jobspy` | Optional unofficial Google Jobs aggregator (off by default, feature flag `jobspy_google`) | `backend/scraper.py::fetch_jobs_via_jobspy` |
| `geonamescache` | Offline city gazetteer for location filtering | `backend/geo.py` |
| `pycountry` | ISO-3166 country resolution | `backend/geo.py` |

**Optional / lazy imports** (code degrades gracefully when absent): `pgvector` (falls back to SQLite JSON vectors), `jobspy` (feature
disabled), `google-*` (Gmail API unavailable → browser-session fallback), `geonamescache`/
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
| `match_analysis` | Text | Strengths/gaps/feedback |
| `cover_letter_draft`, `tailored_resume_points`, `cold_message_draft` | Text | On-demand materials |
| `embedding` | SafeVector(384) | Semantic search |
| `is_ghost_job` | Boolean, default False | Due-diligence flag |
| `repost_count` | Integer, default 0 | ≥3 flags a probable ghost job |
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
| `excluded_companies` | Text, default `amazon` | Comma-separated employer/domain blocklist (never scanned) |
| `updated_at` | DateTime | |

`DEFAULT_FEATURE_FLAGS` (opt-in; unknown keys ignored): `ats_portals=True`, `google_jobs=True`,
`linkedin_sync=False`, `gmail_sync=False`, `jobspy_google=False`, `ats_hirist=True`. Helpers:
`get_user_preferences`, `get_feature_flags`, `is_feature_enabled`.

### 9.3 Entity relationships (text)

```text
Company 1 ────< Job 1 ────< ApplicationEvent
                   │
                   ├── embedding : SafeVector(384)
                   └── matches DismissedJobPattern (by embedding similarity, not FK)
ApplicationQuestionAnswer : standalone semantic cache (embedding-searched)
Resume : standalone active profile (encrypted content + embedding)
ProcessedEmail : standalone Gmail dedup
CustomFormField : standalone telemetry (aggregated by domain/field)
UserPreference : singleton config + feature flags
```

---

## 10. Performance Optimization Strategies

Performance work here is dominated by **avoiding unnecessary LLM calls and protecting local
GPU memory**, not by shaving CPU. The key strategies:

### 10.1 Tiered compute & dynamic concurrency

* **Task-complexity routing** sends trivial work to small/fast models (see `AGENTIC_ARCHITECTURE.md` §5.2).
* **`LLMQueueManager`** (`backend/llm_queue.py`) is a priority min-heap with adaptive concurrency:
  * `INTERACTIVE=1` < `ON_DEMAND=2` < `BACKGROUND=3` (lower runs first).
  * Local models: **1 worker, 0.2 s pacing** — prevents GPU/VRAM thrashing and KV-cache churn.
  * Cloud APIs: up to **8 parallel workers, 0.0 s pacing**.
  * `sync_provider_concurrency(is_local)` applies the config values; `set_concurrency()` scales
    live. Backpressure is signalled when `queue_depth >= max_workers * 4`.
* **Bulk operations pace themselves**: `bulk_tailor_jobs` sleeps `0.5 s` between jobs (KV-cache
  cycling); `bulk_apply` sleeps `0.4 s` between tabs; LinkedIn uses
  `LINKEDIN_AUTOMATION_PACING_SECONDS` (default 2.5 s).

### 10.2 Token elimination (the biggest win)

* **Zero-token semantic dismissal** (`check_semantic_dismissal`, default `0.80`) runs **before**
  any match prompt on new listings, skipping inference for ignored role patterns.
* **Cache-first screener answers** (`resolve_semantic_essay_cache`, `0.85`; chat `0.82`) return
  verified answers at ~0 tokens and increment `use_count`.
* **Grounding/anti-slop reruns** only occur for material generation (on-demand), never during
  discovery sync.
* **Discovery never pre-generates heavy materials**; it only computes match scores.

### 10.3 Vector search scaling

* Default SQLite path computes cosine similarity in Python → **O(n) per query**. For large job
  sets set `DATABASE_URL` to PostgreSQL with `pgvector` so `SafeVector` maps to a native indexed
  `vector` column.
* Radar caps its candidate scan at 500 rows before ranking and slices to the requested `limit`
  (`ge=1, le=200`, default 30).

### 10.4 Request & I/O bounds

* Scraper timeouts are tight and per-endpoint (DDG 6 s; ATS list 15 s; ATS detail 8 s; Gmail
  status 12 s; active probe 10 s).
* Page/result caps: ATS `max_matches=35`, Google Jobs `max_results=15`, Gmail API `PAGE_SIZE=50`
  × `MAX_PAGES=2` (~100 messages), `/api/logs` lines ≤1000, operation logs default limit 50.
* Playwright scroll/pagination is bounded per flow (e.g. saved jobs `max_pages=25`, 5 scrolls per
  page, 450 ms waits).

### 10.5 Non-blocking execution & streaming

* Long discovery/sync work runs on daemon threads via `BackgroundTaskEngine` and returns HTTP
  **202** immediately; assisted-apply Playwright work is offloaded to dedicated daemon threads so
  request handlers stay responsive.
* Progress streams over SSE with a **15 s heartbeat** and `X-Accel-Buffering: no`.
* Token accounting is **O(1) in memory** (`TokenTracker.get_stats()`), with incremental
  DB-only-delta sync (`id > last_aggregated_log_id`) instead of full recalculation.

### 10.6 Known opportunities (be careful)

* `GET /api/jobs` returns all matching rows with **no LIMIT** and attaches `company_name` in a
  Python loop; there is **no `joinedload`/`selectinload`** anywhere. Add pagination + eager
  loading before scaling to very large job tables.
* `top_p` in `TASK_PROFILES` is not yet forwarded to providers.

---

## 11. Security Implementation Details

Security is layered: origin firewall → API-key/same-origin auth → column-level encryption →
isolated browser profile → prompt-injection boundary → opt-in flags + human-in-the-loop.

### 11.1 Origin firewall & CORS

`backend/main.py` installs `CORSMiddleware` with a strict allowlist:

```python
ALLOWED_ORIGINS = [
    "http://localhost:8000",
    "http://127.0.0.1:8000",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
]  # + env ALLOWED_ORIGINS
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_origin_regex=r"^chrome-extension://.*$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
```

Wildcard origins are deliberately avoided while credentials are enabled.

### 11.2 Authentication (`security_firewall_middleware`)

All routes except a small public set are gated by `security_firewall_middleware`, which accepts
**any** of: `X-API-Key` header, `Authorization: Bearer`, `?api_key=`, a same-origin/localhost
origin/referer, `Sec-Fetch-Site: same-origin`, or a loopback client (`127.0.0.1`, `::1`,
`testclient`). Otherwise it returns **401**.

* **Public paths**: `/`, `/api/health`, `/api/auth/status`, `/docs`, `/openapi.json`,
  `/favicon.ico`, `/api/gmail/oauth2callback`, `/static/*`, and all `OPTIONS` preflights.
* `verify_api_key` is also applied to `GET /api/auth/client-config`.
* Key validation uses **constant-time** comparison to prevent timing attacks:

```python
def is_valid_api_key(provided_key: str) -> bool:
    if not provided_key or not API_KEY:
        return False
    return secrets.compare_digest(provided_key.strip(), API_KEY.strip())
```

### 11.3 Secret storage & keys

* `.key` — Fernet key (`load_or_create_key`, `chmod 600`), env `JOB_ALERT_AGENT_SECRET_KEY` overrides.
* `.api_key` — system API token (`secrets.token_urlsafe(32)`, `chmod 600`), env
  `JOB_ALERT_AGENT_API_KEY` overrides.
* `data/llm_settings.json` and `data/gmail_settings.json` are `chmod 600`; API keys / OAuth
  client secret / refresh token / PKCE verifier are Fernet-encrypted field-by-field.

### 11.4 Encryption at rest

`backend/config.py` exposes `encrypt_data`/`decrypt_data` (Fernet = AES-128-CBC + HMAC-SHA256).
Encrypted columns: `resumes.content_encrypted`, `resumes.parsed_json_encrypted`,
`application_question_answers.answer_text` (via the `answer_text` property). Legacy plaintext
Q&A rows are auto-encrypted during `init_db()`.

### 11.5 Browser profile isolation

All automation uses a single persistent profile at `~/.job-alert-agent/chrome_profile/` (default
`0700`, env-overridable). `get_persistent_browser_context()` raises `BrowserProfileBusy` rather
than deleting live locks; `clean_stale_profile_locks()` is PID-aware (reads `SingletonLock` and
checks `os.kill(pid, 0)`). Background tasks skip when the profile is in use.

### 11.6 Gmail privacy scoping (opt-in)

* Read-only scope: `https://www.googleapis.com/auth/gmail.readonly`.
* Narrow query (no full-mailbox enumeration); metadata + snippet only; `PAGE_SIZE=50`,
  `MAX_PAGES=2`; dedup via `ProcessedEmail`.
* `/api/gmail/oauth2callback` is the only public Gmail path (loopback). Fallback browser scanner
  also uses a targeted search query only.

### 11.7 Prompt-injection & output safety

External text is wrapped with `wrap_untrusted` (which strips pre-existing boundary tags), generated
text is passed through `sanitize_generated_text` (strips `<script>`/`<style>`/HTML/control chars),
and the extension HTML-escapes values before DOM insertion. The LLM **never** submits
applications — a human confirms (`submission_confirmed`).

### 11.8 Opt-in guardrails

Opt-in feature flags in `UserPreference.features_json` (`linkedin_sync`, `gmail_sync`,
`jobspy_google` default **off**) hide channels in the UI and are **skipped** by the scheduler.
Explicit "Run now" passes `force=True` to bypass the gate for a single manual run.

---

## 12. Developer Onboarding Guide

### 12.1 Prerequisites

* Python 3.10+ and `git`.
* Optional but recommended: a local inference server (LM Studio `:1234`, Unsloth `:8008`, or
  Ollama `:11434`). The app also runs without one (deterministic fallbacks + zero-token filters).
* Optional: PostgreSQL + `pgvector` for large databases.
* Google Chrome only if you want to load the extension.

### 12.2 Setup

```bash
git clone <repo-url> && cd job-alert-agent

python3 -m venv .venv
source .venv/bin/activate                 # macOS/Linux
pip install -r requirements.txt
playwright install chromium               # required for browser automation

# Run the server
uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload
# ...or the helper launcher:
python run.py
```

Open `http://localhost:8000`. Verify readiness with `GET /api/health` and provider detection with
`GET /api/llm/status`.

### 12.3 Configuration (all optional)

Copy the keys you need into a root `.env` (defaults work out of the box):

```env
DATABASE_URL=sqlite:///./job_alert_agent.db   # or postgresql://.../job_alert_agent
DEFAULT_LLM_PROVIDER=auto
LM_STUDIO_BASE_URL=http://localhost:1234/v1
OLLAMA_BASE_URL=http://localhost:11434
ALLOW_CLOUD_FALLBACK=false                    # cloud inference stays off unless enabled
# OPENAI_API_KEY= / GEMINI_API_KEY= / ANTHROPIC_API_KEY= are also overridable via Settings (encrypted)

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

* `setup_test_db` — in-memory SQLite, tables created/dropped per test.
* `isolate_token_tracker_stats` — redirects token stats to a temp file.
* `isolate_llm_settings_file` — redirects LLM/Gmail settings to temp files.
* `prevent_real_browser_spawns` — mocks Playwright and isolates the Chrome profile dir.
* `db_session` — a fresh session for DB-backed tests.
* `override_get_db` wires the test DB into the FastAPI app; `test_client` is a ready `TestClient`.

**Never** spawn real browsers or hit live external networks in unit tests.

### 12.6 Where things live

| Need to change… | File |
| :--- | :--- |
| API routes, CORS, auth, task endpoints | `backend/main.py` |
| LLM providers, prompts, routing, embeddings, caching | `backend/ai_helper.py` |
| Queue concurrency/pacing | `backend/llm_queue.py` |
| ORM models / migrations | `backend/database.py` |
| Secrets, encryption, provider/Gmail config | `backend/config.py` |
| Scraping, ghost detection, geo/URL helpers | `backend/scraper.py`, `backend/geo.py` |
| Browser automation, LinkedIn/Gmail sync, autofill overlay | `backend/playwright_app.py` |
| Resume/PDF/JD parsing | `backend/parser.py` |
| Background tasks / SSE | `backend/task_engine.py` |
| Periodic scheduler | `backend/scheduler.py` |
| Logging / telemetry (rotation + `OperationLog` pruning) | `backend/observability.py`, `backend/database.py` |
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

* `GET /api/logs?level=ERROR&search=foo&limit=200` tails the JSON log; `GET /api/operations/logs`
  returns persisted `OperationLog` history.
* `GET /api/llm/queue-status` shows queue depth, concurrency, backpressure, and estimated wait.
* `GET /api/llm/token-usage` reports O(1) token/cost stats; `POST .../sync` and `.../recalculate`
  reconcile with the DB.
* Every request gets an `X-Request-ID` echoed in logs (console + JSON).

### 12.9 Troubleshooting

| Symptom | Likely cause / fix |
| :--- | :--- |
| "Local AI model is offline" copilot replies | No local server running; start LM Studio/Ollama or enable cloud fallback |
| `BrowserProfileBusy` | Another task/process holds the profile lock; wait or stop the other task |
| `Unauthorized: Valid X-API-Key...` | Extension/REST caller missing `X-API-Key`; use the key from `.api_key` (or same-origin) |
| Semantic results look weak | No Ollama embeddings running; the deterministic hash fallback is lower quality |
| Slow semantic queries | SQLite O(n) vectors; switch to PostgreSQL + `pgvector` |
| Gmail sync does nothing | Gmail is opt-in and/or not connected; see `docs/GMAIL_API.md` |