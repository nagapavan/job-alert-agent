# Security Implementation Details

This document outlines the layered security approach: origin firewall → API-key/same-origin auth → column-level encryption → isolated browser profile → prompt-injection boundary → opt-in flags + human-in-the-loop.

---

## 1. Origin Firewall & CORS

`backend/main.py` installs `CORSMiddleware` with a strict localhost allowlist and an
**empty-by-default** extension allowlist:

```python
ALLOWED_ORIGINS = [
    "http://localhost:8000",
    "http://127.0.0.1:8000",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
]  # + env ALLOWED_ORIGINS

# Extensions are trusted only when their exact ID is allowlisted via ALLOWED_EXTENSION_IDS.
# By default the list is empty, so no chrome-extension:// origin is accepted.
_allowed_extension_ids = [
    eid.strip()
    for eid in os.environ.get("ALLOWED_EXTENSION_IDS", "").split(",")
    if eid.strip()
]
_extension_origin_regex = (
    r"^chrome-extension://(" + "|".join(re.escape(eid) for eid in _allowed_extension_ids) + r")$"
    if _allowed_extension_ids
    else None
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_origin_regex=_extension_origin_regex,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
```

**Key principles**:

- **No wildcard extension origin.** A blanket `^chrome-extension://.*$` would trust *any* installed
  extension. The regex is built from the explicit `ALLOWED_EXTENSION_IDS` allowlist and is `None`
  when that list is empty, so extension origins are denied by default. The extension is expected to
  authenticate with the `X-API-Key` header instead.
- **Credentials are not ambient.** `allow_credentials=False` — the API key travels in a header or
  `?api_key=` query parameter, never in a cookie, so there is no credentialed-CORS surface to
  protect.

The local SPA still works because of the loopback/same-origin branch in the auth middleware (§2),
not because of CORS.

---

## 2. Authentication (`security_firewall_middleware`)

All routes except a small public set are gated by `security_firewall_middleware`, which accepts **any** of:
- `X-API-Key` header
- `Authorization: Bearer` token
- `?api_key=` query parameter
- Same-origin/localhost origin/referer
- `Sec-Fetch-Site: same-origin`
- Loopback client (`127.0.0.1`, `::1`, `testclient`)

Otherwise returns **401 Unauthorized**.

**Public paths** (no auth required):
- `/` (root)
- `/api/health`, `/api/auth/status`
- `/docs`, `/openapi.json`
- `/favicon.ico`
- `/api/gmail/oauth2callback` (OAuth loopback)
- `/static/*` (static assets)
- All `OPTIONS` preflights

**Special case**: `GET /api/auth/client-config` requires `verify_api_key`.

**Constant-time comparison** (timing attack prevention):

```python
def is_valid_api_key(provided_key: str) -> bool:
    if not provided_key or not API_KEY:
        return False
    return secrets.compare_digest(provided_key.strip(), API_KEY.strip())
```

---

## 3. Secret Storage & Keys

Three critical secrets are managed locally:

### `.key` — Fernet Encryption Key
- Fernet symmetric key (for AES-128-CBC + HMAC-SHA256)
- File: `.key` (auto-created on startup, `chmod 600`)
- Override: env var `JOB_ALERT_AGENT_SECRET_KEY`
- Loaded by: `backend/config.py::load_or_create_key()`

### `.api_key` — System API Token
- 32-character URL-safe token
- File: `.api_key` (auto-created on startup, `chmod 600`)
- Override: env var `JOB_ALERT_AGENT_API_KEY`
- Used by: Extension, REST clients, internal auth checks

### `data/llm_settings.json` & `data/gmail_settings.json` — Provider Credentials
- **Format**: JSON, `chmod 600`
- **Encryption**: Per-field Fernet encryption
- **Contents**:
  - LLM settings: OpenAI/Gemini/Anthropic API keys
  - Gmail settings: OAuth client secret, refresh token, PKCE verifier
- **Precedence**: Env vars always override persisted values

---

## 4. Encryption at Rest

`backend/config.py` exposes:
- `encrypt_data(plaintext: str) -> str` — returns Fernet ciphertext
- `decrypt_data(ciphertext: str) -> str` — returns plaintext

**Algorithm**: Fernet (symmetric AES-128-CBC + HMAC-SHA256)

**Encrypted columns**:

| Table | Column | Purpose |
| :--- | :--- | :--- |
| `resumes` | `content_encrypted` | Raw resume text (PDF or plain text) |
| `resumes` | `parsed_json_encrypted` | Parsed ATS JSON (skills, work history) |
| `application_question_answers` | `answer_text` | Q&A bank answers (via property getter/setter) |

**Auto-migration**: Legacy plaintext Q&A rows are encrypted in place during `init_db()` on startup.

---

## 5. Browser Profile Isolation

All browser automation (LinkedIn, Gmail, ATS portals) uses a **single persistent profile**:

- **Path**: `~/.job-alert-agent/chrome_profile/` (default; override via env)
- **Permissions**: `0700` (user-only read/write/execute)
- **Purpose**: Maintain login state without repeatedly authenticating

**Concurrency safety**:

- `get_persistent_browser_context()` **raises `BrowserProfileBusy`** rather than deleting live locks or launching a logged-out context
- `clean_stale_profile_locks()` is **PID-aware**: reads `SingletonLock` files and verifies processes are alive (`os.kill(pid, 0)`)
- Background tasks **skip entirely** when the profile is in active use

This prevents race conditions where two browser instances collide on the same cookies/cache.

---

## 6. Prompt-Injection & Output Safety

Three layers protect against prompt injection and XSS:

### 6.1 Input Boundary (`wrap_untrusted`, `backend/ai_helper.py`)

`wrap_untrusted(text, label="untrusted")` wraps external text in `<<label>>` delimiters so the model treats it strictly as data; it strips any pre-existing boundary tags before wrapping.

Scraped JD descriptions, company websites, Gmail snippets, etc. are wrapped before being passed to prompts. This isolates untrusted content from prompt instructions.

### 6.2 Output Sanitization (`sanitize_generated_text`, `backend/ai_helper.py`)

`sanitize_generated_text(text, max_len=4000)` strips `<script>`/`<style>` blocks, HTML tags, and control characters, and caps length.

All LLM-generated text (cover letters, interview answers, match analysis) is sanitized before display or injection into forms.

### 6.3 Human-in-the-Loop for Applications

- `Job.submission_confirmed` (Boolean, default False) gates job applications
- The LLM **never** submits applications autonomously
- Human must explicitly confirm before form submission occurs

### 6.4 Extension HTML-Escaping

The extension escapes all text values before DOM insertion, preventing runtime XSS from untrusted data.

---

## 7. Opt-In Guardrails

Discovery channels carry varying trust/TOS risk. Opt-in feature flags in `UserPreference.features_json` (defaults in `backend/database.py::DEFAULT_FEATURE_FLAGS`) control channel availability:

### Public API Channels (On by default)

| Flag | Channel | Risk | Default |
| :--- | :--- | :--- | :--- |
| `ats_portals` | Greenhouse, Lever, Ashby ATS scraping | Low (public APIs) | **on** |
| `google_jobs` | Google Jobs API | Low (public API) | **on** |
| `ats_greenhouse`, `ats_lever`, `ats_ashby` | Per-portal toggles | Low (documented APIs) | **on** |

### Unofficial/Session-Driven Channels (Off by default)

| Flag | Channel | Risk | Default |
| :--- | :--- | :--- | :--- |
| `linkedin_sync` | LinkedIn DOM automation | High (session-driven, TOS ambiguous) | **off** |
| `gmail_sync` | Gmail inbox reading | Medium (OAuth, privacy-scoped) | **off** |
| `jobspy_google` | Unofficial Google Jobs aggregator | Medium (third-party API) | **off** |
| `ats_smartrecruiters`, `ats_workable`, `ats_workday`, `ats_uber`, `ats_custom_html` | Unofficial portals | High (reverse-engineered endpoints) | **off** |

**Enforcement**:
- Disabled channels are **hidden in the SPA UI** (Settings → Discovery Features)
- Disabled channels are **skipped server-side** in:
  - `trigger_jobs_scrape()` (discovery sync)
  - `sync_external_applications()` (status sync)
  - Scheduler automatic runs
- **Manual override**: Explicit `force=True` bypasses the gate for a single manual run (e.g., user clicks "Run now")

**UI disclosure**: LinkedIn/Gmail/JobSpy channels show explicit risk warnings when enabled, explaining account limitations and TOS implications.

---

## 8. Gmail Privacy Scoping

Gmail inbox reading is **opt-in** (`UserPreference.features_json["gmail_sync"]`, default **off**) and uses a **narrow, privacy-respecting** flow:

### Preferred Path: BYOK Gmail API (`backend/gmail_client.py`)

- **Scope**: Read-only `https://www.googleapis.com/auth/gmail.readonly`
- **Query**: Narrow search — only job-board senders + interview/offer/rejection keywords (never full-mailbox enumeration)
- **Fetch mode**: Metadata + snippet only (never downloads full message bodies)
- **Pagination**: `PAGE_SIZE=50`, `MAX_PAGES=2` (~100 messages max)
- **Dedup**: `ProcessedEmail` table tracks `message_id` to prevent re-processing
- **Credentials**: Encrypted at rest (`data/gmail_settings.json`)
  - Client secret (Fernet-encrypted)
  - Refresh token (Fernet-encrypted, survives logout)
  - PKCE verifier (Fernet-encrypted)
- **Public paths**: Only `/api/gmail/oauth2callback` is public (loopback)

### Fallback Path: Browser-Session Scanner

If Gmail API isn't connected, fall back to a **restricted browser-session scanner** (`scan_gmail_for_job_alerts`):
- Targeted search only (no full-inbox / category scans)
- Reads visible DOM content (no API direct access)
- No token refresh needed; uses existing browser session

**Caveat**: Google Console expires refresh tokens after **7 days** if the app is in "Testing" mode. Move to "In production" (unverified, skip app verification) to avoid reconnecting frequently.

---

## 9. Employer/Domain Blocklist

`UserPreference.excluded_companies` (default: `"amazon"`) is a comma-separated blocklist of company names/domains. Function signature: `is_company_excluded(name: str, domain: str, db)` in `backend/database.py` — it matches the company name/domain against blocklist tokens case-insensitively.

Companies matching the blocklist are **never scanned** by any discovery channel.

**Rationale for default "amazon"**: Amazon's Conditions of Use and Agent Policy explicitly restrict automated/agent access.

---

## 10. Opportunity Radar (Deterministic Scoring, No LLM Scraping)

`GET /api/opportunities/radar` returns a structured, ranked hiring-intent feed derived from **already-ingested** jobs:
- LinkedIn alerts/recommendations
- ATS portals
- Google Jobs
- Gmail recruiter outreach

**Scoring method**: `compute_hiring_intent()` in `backend/main.py`
- **No LLM calls** (deterministic heuristic)
- **No LinkedIn post scraping** (the unofficial `/api/linkedin/search-hiring-posts` path was removed)
- Ranks jobs by recency, match score, and user engagement signals

---

## 11. Database Schema & Access Control

### ORM-Only Queries (No Blind Raw SQL)

Always query via SQLAlchemy ORM models:
```python
# Good
db.query(OperationLog).all()
db.query(Job).filter(Job.status == "Applied").first()

# Bad: avoid raw SQL unless absolutely necessary
```

This prevents SQL injection and keeps schema changes in one place.

### Auto-Migrations for Schema Changes

When adding/updating columns in SQLAlchemy models, include lightweight auto-migration logic in `init_db()` (`backend/database.py`). It inspects existing table columns (`PRAGMA` for SQLite / `information_schema` for PostgreSQL) and issues `ALTER TABLE ... ADD COLUMN` for missing ones, so existing databases upgrade without data loss (e.g. `score_method`, `is_active`).

This synchronizes existing SQLite/PostgreSQL databases without data loss.

---

## 12. Logging & Observability

### Structured Logging

- **Console output**: Colored, human-readable (dev-friendly)
- **File output**: JSON format (`logs/app.log`), machine-parseable
- **Rotation**: `LOG_MAX_BYTES` (default 10 MB), `LOG_BACKUP_COUNT` (default 5 files)
- **LLM audit trail**: Separate `logs/llm_audit.log` sink

### Request Tracking

Every request gets an `X-Request-ID` (UUID) echoed in:
- Response headers
- Structured logs
- Database `OperationLog` (if applicable)

Enables correlation of related log entries across multiple sources.

### Operation Audit Trail

`OperationLog` table tracks:
- `operation_type` (e.g., `linkedin_sync`, `ats_scrape`, `match_rescore`)
- `status` (`Completed`, `Partial`, `Failed`)
- `summary` (human-readable summary)
- `details_json` (sanitized structured diagnostics)
- `prompt_tokens`, `completion_tokens` (LLM cost accounting)
- `timestamp` (creation time)

Retention: `OPERATION_LOG_MAX_ROWS` (default 1000), `OPERATION_LOG_RETENTION_DAYS` (default 30)
Pruning: `prune_operation_logs()` runs every `OPERATION_LOG_PRUNE_INTERVAL_SECONDS` (default 3600)

### Real-Time Log Inspection

- `GET /api/logs?level=ERROR&search=foo&limit=200` — tail structured logs
- `GET /api/operations/logs` — query `OperationLog` history
- `/api/logs` **requires auth** (not public)

---

## 13. Responsible Disclosure

If you discover a security issue:

1. **Do NOT open a public GitHub issue** (discloses the vulnerability)
2. Check for `SECURITY.md` in the repository root
3. Contact the maintainer directly

---

## See Also

- [`docs/GMAIL_API.md`](GMAIL_API.md) — opt-in BYOK Gmail API flow
- [`docs/ATS_PORTALS.md`](ATS_PORTALS.md) — ATS portal scraping risk tiers & consent
- [`README.md`](../README.md) — Account security risks & terms-of-service caveats
