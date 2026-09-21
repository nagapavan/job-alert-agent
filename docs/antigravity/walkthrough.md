# Walkthrough: Multi-Platform Application Tracking (Hirist.tech & Greenhouse.io)

We have implemented multi-platform candidate application tracking across **Hirist.tech** (`hirist.tech` / `hirist.com`) and **Greenhouse.io** (`greenhouse.io` / `boards.greenhouse.io`), seamlessly synchronized with our **Gmail Decision Alerts** scanner.

---

## Key Changes & Architecture

### 1. Greenhouse Status & Requisition Verification ([`backend/scraper.py`](../../backend/scraper.py))
- **`check_greenhouse_application_status(status_url)`**: Parses tokenized candidate application status links (`boards.greenhouse.io/application_status?token=...`) to extract live stage information, submission date, role title, and company name.
- **`probe_greenhouse_job_active(job_url)`**: Queries the Greenhouse Public Board API (`boards-api.greenhouse.io/v1/boards/{slug}/jobs/{job_id}`) or performs an HTTP probe to detect if an applied requisition was closed/unlisted, auto-marking the application status as `Rejected` / closed.

### 2. Hirist Candidate Portal & Decision Email Scrapers ([`backend/playwright_app.py`](../../backend/playwright_app.py))
- **`scan_and_sync_hirist_applications(playwright_instance, headless=True, max_pages=10)`**: Uses the persistent Chrome profile (`.chrome_profile/`) to access authenticated candidate dashboards at `https://www.hirist.tech/candidate/applications`. Extracts all applied cards and normalizes statuses:
  - `"Interview Scheduled"` / `"Contacted"` $\rightarrow$ `Interview`
  - `"Not Shortlisted"` / `"Rejected"` / `"Closed"` $\rightarrow$ `Rejected`
  - `"Shortlisted"` $\rightarrow$ `Shortlisted`
  - `"Applied"` / `"Viewed by Recruiter"` $\rightarrow$ `Applied`
- **`sync_email_application_events(playwright_instance, headless=True)`**: Scans candidate Gmail for interview invitations and rejection notices, extracting the target company and role to transition `Job.status`.

### 3. Application Sync API & Database Orchestration ([`backend/main.py`](../../backend/main.py))
- **`POST /api/applications/sync-external`**:
  - Request schema: `ExternalSyncRequest(sources=['hirist', 'greenhouse', 'gmail'], max_pages=10, headless=True)`
  - Response schema: `ExternalSyncResponse(hirist_checked, hirist_updated, hirist_ingested, emails_checked, status_updates, details, message)`
  - Correlates scraped external applications against the local database:
    - **Existing Applications**: Updates `Job.status` (e.g. `Applied` $\rightarrow$ `Interview` / `Rejected`), sets timeline timestamps (`applied_at`, `interview_scheduled_at`, `rejected_at`), and logs `ApplicationEvent('external_status_sync')`.
    - **New Applications (submitted directly on Hirist)**: Ingests into the database with source `"Hirist Applications"`, computes initial match scores and embeddings, and logs `ApplicationEvent('external_application_ingested')`.
  - Protected by our zero-trust firewall (`X-API-Key` & same-origin verification).

### 4. Frontend SPA Controls ([`frontend/index.html`](../../frontend/index.html) & [`frontend/app.js`](../../frontend/app.js))
- Added **`🔄 Sync Portals`** button in the header actions bar.
- Added **`⚡ Launch Hirist.tech Login`** shortcut in the Dedicated Browser Profile Login modal (`.chrome_profile`).

### 5. Architectural Documentation ([`DESIGN_DIAGRAMS.md`](../../DESIGN_DIAGRAMS.md))
- Added **Section 17.6: DFD 6 - Multi-Platform Application Tracker & Lifecycle Synchronization Flow** detailing the end-to-end data flow across Playwright, ATS scrapers, Gmail alerts, and the database layer.

---

## Test Verification

A dedicated test suite was added in [`tests/test_application_tracking.py`](../../tests/test_application_tracking.py):
- `test_check_greenhouse_application_status_active` (PASSED)
- `test_check_greenhouse_application_status_closed` (PASSED)
- `test_probe_greenhouse_job_active_api_success` (PASSED)
- `test_probe_greenhouse_job_active_api_closed` (PASSED)
- `test_scan_and_sync_hirist_applications_mocked_dom` (PASSED)
- `test_sync_email_application_events` (PASSED)
- `test_api_sync_external_applications_lifecycle` (PASSED)
- `test_api_sync_external_unauthorized` (PASSED)

### Full Test Suite Results:
```bash
$ ./.venv/bin/pytest -v tests/
======================== 180 passed, 5 skipped in 4.54s ========================
```
---

## 6. Active Tab Inspector & reCAPTCHA / Hidden Field Filtering Fix

### The Problem
When inspecting job applications on portals using Google reCAPTCHA v2/v3, Cloudflare Turnstile, hCaptcha, or custom ATS systems (e.g. Ashby), invisible `<textarea>` or hidden security input elements (e.g. `<textarea id="g-recaptcha-response" class="g-recaptcha-response" style="display: none;"></textarea>`) and internal UUID hash identifiers (e.g. `795f54cf-c23a-42e8-8f0b-b7191ea0ad3a ... type h`) were mistakenly detected as screener questions.

### The Solution: Dual-Layer Filtering Architecture
1. **Layer 1: Content Script DOM Inspection ([`extension/content/ats-autofill.js`](../../extension/content/ats-autofill.js))**:
   - Upgraded `isIgnoredOrHiddenField(input)` to exclude:
     - Bot / Captcha patterns: `recaptcha`, `g-recaptcha`, `grecaptcha`, `turnstile`, `hcaptcha`, `cf-turnstile`, `cf-chl`, `challenge`, `csrf`, `_token`, `authenticity_token`, `honeypot`, `botcheck`, `arkose`, `funcaptcha`, `threatmetrix`, `datadome`, `perimeterx`.
     - Container/ancestor checks (`.grecaptcha-badge`, `iframe[src*='recaptcha']`, `[aria-hidden='true']`, `[hidden]`, etc.).
     - Geometry & visibility checks (`getBoundingClientRect()`, `display: none`, `visibility: hidden`, `opacity: 0`, `clip: rect(0,0,0,0)`).
     - Token values (>40 characters with no whitespace or URL structure).
   - Upgraded `getFieldCleanLabel(input)` to sanitize and discard raw UUIDs, hex strings, and `type h` artifacts.
   - Upgraded `inspectFormTelemetry()` to discard security tokens, raw UUIDs, and standard contact fields from `detected_questions`.

2. **Layer 2: Sidepanel UI Defensive Sanitization ([`extension/sidepanel/sidepanel.js`](../../extension/sidepanel/sidepanel.js))**:
   - Filtered detected questions in `renderDetectedQuestions()`:
     - Drops any item matching security/captcha patterns or UUID formats.
     - Drops any question with fewer than 3 alphabetic characters.
     - Hides the "Custom Screener Questions" section completely when no genuine subjective questions exist.

---

## 7. UI Layout Reorganization (Context-Aware Sync & Scanners)

### The Problem
The global SPA header previously contained 8 action buttons in a single horizontal row (`Login to Portals`, `Operations Log`, `Google Jobs`, `Sync LinkedIn`, `Sync Portals`, `Hiring Posts`, `Clean Cache`, `Scan Portals`), causing visual congestion.

### The Solution: Contextual Section Architecture
1. **Global Header ([`frontend/index.html`](../../frontend/index.html))**:
   - Streamlined to essential global utilities: `📜 Operations Log` and `🔐 Login to Portals`.
2. **Job Feed & Discovery (`#dashboard-section`)**:
   - Added **⚡ Live Job Discovery Radar** (`.discovery-radar-bar` in [`frontend/styles.css`](../../frontend/styles.css)):
     - `⚡ Scan Portals` (Primary scraping trigger with stop support)
     - `🔗 Sync LinkedIn` (Subscribed alerts & saved jobs sync)
     - `🌐 Google Jobs` (Google for Jobs search modal)
     - `👥 Hiring Posts` (LinkedIn hiring managers / recruiter search modal)
     - `🧹 Clean Cache` (Purges non-matching unapplied roles)
3. **Application Tracker (`#tracker-section`)**:
   - Added `🔄 Sync Application Statuses` (`#trigger-external-sync-btn`) to the Application Pipeline header row, synchronizing candidate statuses across Hirist.tech, Greenhouse, and Gmail decision notices.
4. **Profile & Preferences (`#resume-section`)**:
   - Added **📡 Discovery Channels & Background Sync Engines** (`.sync-channels-grid`):
     - Interactive cards to manage, trigger, and inspect each sync engine directly from candidate settings.

---

## 8. Grounded LLM-Judge Verification Loop & ATS Company Slug Normalizer

### The Problem
1. **Hallucinated Resume Background**: Pitch generation previously hallucinated unlisted past employers (e.g., employers not present in the resume) and fictitious metrics ("processed a petabyte of data daily for fraud detection") when the candidate never held those roles.
2. **Generic Company Placeholders**: On Greenhouse, Lever, and Ashby portals, URL hostname fallback yielded `"Job boards"` or `"boards"`, resulting in outputs like `"Job boards's engineering approach..."`.

### The Solution
1. **ATS Path Slug Extraction ([`extension/content/ats-autofill.js`](../../extension/content/ats-autofill.js))**:
   - Parses company slug directly from path (`boards.greenhouse.io/:company/jobs/:id`, `jobs.lever.co/:company/:id`, `jobs.ashbyhq.com/:company/:id`, `myworkdayjobs.com`).
   - Filters out generic placeholders (`"Job boards"`, `"Greenhouse"`, `"Careers"`, `"Apply"`).
2. **Dynamic Ground-Truth Evidence Loading ([`backend/ai_helper.py`](../../backend/ai_helper.py))**:
   - Dynamically loads verified candidate employers, titles, skills, and project bullets from decrypted `Resume` records (zero hardcoded values).
   - Injects strict negative prompting forbidding the LLM from inventing employers or scale metrics.
3. **LLM-Judge & Self-Correction Verification Loop**:
   - Structured verification schema `PitchVerificationResult(is_grounded, hallucinated_entities, critique, corrected_pitch)`.
   - Compares drafted text against decrypted resume facts.
   - If hallucinations are detected, self-corrects the pitch before caching in the database or returning to the user.

---

## 9. Safe Mode & Overwrite Toggle for ATS Form Autofill (Greenhouse, Lever, Ashby, Workday)

### The Problem
When applying on Greenhouse (or Lever/Ashby/Workday), fields are often pre-populated when a candidate uploads a resume or loads an existing application profile. Previously, "1-Click Auto-Fill Application" unconditionally set `input.value` and triggered change events, wiping out any pre-filled or user-customized field values.

### The Solution: Non-Destructive Safe Mode + Explicit Overwrite Control
1. **Safe Non-Destructive Autofill as Default (`overwrite = false`)**:
   - In [`extension/content/ats-autofill.js`](../../extension/content/ats-autofill.js):
     - **`setElementValue(element, value, overwrite = false)`**: If `!overwrite && element.value && element.value.trim() !== ""`, existing text/values are safely preserved, and the function returns `false` without modifying the field.
     - **`selectDropdownOption(selectEl, query, overwrite = false)`**: If `!overwrite && selectEl.selectedIndex > 0 && selectEl.value !== ""`, existing dropdown selections are preserved.
     - **Radio / Checkbox fields**: Checkboxes (e.g. "Currently working here", Work Authorization) will not flip or overwrite already-selected choices unless `overwrite === true`.
     - **Work Experience & Education Lists**: All repeating card containers safely preserve already-filled company names, titles, dates, institutions, and degrees.
     - **`filledCount` Metric**: Accurately counts and reports only the fields that were actually populated with new data.

2. **Sidepanel UI Mode Toggle & Persistence**:
   - In [`extension/sidepanel/sidepanel.html`](../../extension/sidepanel/sidepanel.html) & [`extension/sidepanel/sidepanel.js`](../../extension/sidepanel/sidepanel.js):
     - Added `[ ] Overwrite pre-filled fields` checkbox directly above the **⚡ 1-Click Auto-Fill Application** button.
     - Dynamic status badge:
       - **🛡️ Safe Mode** (Green badge, default): Preserves any pre-filled data.
       - **⚠️ Overwrite Mode** (Red badge): Replaces all form fields with candidate profile data when explicitly checked by the user.
     - Preference is persisted locally in `chrome.storage.local` under `autofillOverwritePref`.
     - Both the Extension Sidepanel and the In-Page Floating Quick-Action Widget respect the user's preference.

---

## 10. Pitch & Cover Letter Evals, Temperature Tuning (0.8) & Stale Cache Cleanout

### The Problem
- Application pitch generation and cover letter generation were susceptible to generic filler ("thrilled to apply", "unique blend", "passionate about").
- Stale hallucinated responses (e.g. referencing an unlisted employer instead of the target company) were cached in `ApplicationQuestionAnswer` and served upon subsequent requests for similar questions.

### The Solution
1. **Temperature Tuning to 0.8**:
   - Tuned `TASK_PROFILES["grounded_pitch"]`, `TASK_PROFILES["cover_letter"]`, and `TASK_PROFILES["consolidated_package"]` to `temperature = 0.8` to produce sharper, authentic, and accurate output rather than conversational fluff.
2. **Cover Letter LLM-Judge Guardrail**:
   - Added [`verify_and_judge_grounded_cover_letter()`](../../backend/ai_helper.py) to check cover letters against decrypted resume ground truth and eliminate hallucinated entities and banned AI cliches before delivery.
3. **Stale Cache Purge**:
   - Cleaned out previously cached pitch/screener records from the database table `ApplicationQuestionAnswer` (`id=9, 10`).
   - Fixed `adapt_answer_variables` and `generateGroundedPitch` fallback variable scoping in `runtime-adapter.js`.

---

## 11. Full-Stack Observability & Request Tracing Layer

### Implemented Capabilities ([`backend/observability.py`](../../backend/observability.py)):
1. **Dual-Sink Structured Logging**:
   - **Terminal**: Clean, color-coded output via `ConsoleFormatter` (`%H:%M:%S LEVEL [logger] [req_id] message`).
   - **Rotating File**: Single-line structured JSON logs in `logs/app.log` (`RotatingFileHandler`, 10MB x 5 backups).
2. **HTTP Request Correlation & Timing Middleware ([`backend/main.py`](../../backend/main.py))**:
   - Injects `X-Request-ID` header into every HTTP response.
   - Logs `METHOD /path -> STATUS (LATENCY_MS)` for all non-health requests with correlation IDs.
3. **LLM Invocation Audit Trail ([`backend/ai_helper.py`](../../backend/ai_helper.py))**:
   - `log_llm_event` emits structured metadata for all LLM calls (task name, provider, model, estimated prompt/completion tokens, temperature, latency, cache hit status).
4. **Log Streamer Endpoint (`GET /api/logs`)**:
   - Real-time endpoint to tail the active rotating JSON log file with optional log level filtering (`?level=INFO|WARNING|ERROR`) and text search (`?search=query`).

---

## 12. Non-Blocking Background Tasks Engine, Unified Queue Status & Periodic Scheduler

### The Problem
- Discovery scans (such as scanning 6+ followed Google for Jobs queries or synchronizing LinkedIn alerts) ran synchronously inside single HTTP requests, locking the UI modal for 20–45s, freezing navigation, and risking client timeouts.

### The Solution
1. **Async Background Task Engine ([`backend/task_engine.py`](../../backend/task_engine.py))**:
   - `BackgroundTaskEngine` dispatches decoupled asynchronous tasks, returns `202 Accepted` + `task_id` immediately.
   - Granular step-by-step progress tracking (`step_label`, `progress_percentage`, `items_discovered`, `elapsed_seconds`).
   - Re-entrant cooperative `CancellationToken` support (`POST /api/tasks/{task_id}/cancel`).
   - Server-Sent Events (SSE) channel (`GET /api/tasks/events`) streaming real-time updates directly to frontend clients.

2. **Periodic Automation Scheduler ([`backend/scheduler.py`](../../backend/scheduler.py))**:
   - In-process daemon executing periodic discovery automations:
     - Google for Jobs Multi-Query Scan (every 6 hours)
     - LinkedIn Saved Jobs & Due Diligence Sync (every 12 hours)
   - Auto-persistence in `data/scheduler_config.json`, toggle switches (`is_enabled`), interval adjustment, and on-demand trigger endpoints (`POST /api/scheduler/jobs/{id}/run`).

3. **Background Jobs & Queue Dashboard View ([`frontend/index.html`](../../frontend/index.html), [`frontend/app.js`](../../frontend/app.js), [`frontend/styles.css`](../../frontend/styles.css))**:
   - **Header Progress Pill**: Live indicator in navigation bar (`⚡ 1 Task Active (50%)`) with one-click direct jump.
   - **Active Tasks Card**: Real-time progress bars with step descriptions, items discovered counter, and **"⏹️ Stop Task"** action.
   - **LLM Priority Queue Card**: Real-time visibility into local GPU vs cloud concurrency, queue depth, active generation, and average inference latency.
   - **Automated Schedules Matrix**: Interactive switches, next-run timers, and **"▶️ Run Now"** triggers.
   - **Non-Blocking Modal Trigger**: Clicking "🚀 Run Discovery Scan" in the Google Jobs modal immediately closes the modal and executes in the background.

---

## 13. Work Experience ATS Extraction & Heuristic Fallback Robustness

### The Problem
- When reloading or uploading candidate resumes under conditions where the local LLM server timed out or returned a parsing error, the system fell back to the heuristic deterministic parser (`parse_open_source_resume` in [`backend/parser.py`](../../backend/parser.py)).
- PDF text extracted via `pypdf` frequently emits single tokens or words on individual lines (e.g., `Professional\nExperience\nAcme...`).
- Legacy normalization merged mixed-case headers and trailing bullet sentences into single paragraphs, missing section breaks and treating full role headers as accomplishment details because line lengths exceeded default thresholds.
- Furthermore, month variations with trailing characters (e.g. `Sept` vs `Sep`) were missed by `DATE_RANGE_REGEX`. As a result, work history extracted 0 experience items (`experience: []`).

### The Solution
1. **Enhanced Month & Date Regexes ([`backend/parser.py`](../../backend/parser.py))**:
   - Upgraded `MONTH_REGEX_STR` and `DATE_RANGE_REGEX` to support full variations including `Sept?`, `Jan` through `Dec`, full month names, numeric formats, and `Present` / `Current` / `Now`.
2. **Text Normalization & Header Preservation**:
   - Enhanced `normalize_resume_text` to recognize mixed-case section headers (`Professional Experience`, `Technical Skill Set`, `Core Competencies`, `Executive Summary`, `Education & Certifications`) across multi-line tokens.
   - Preserved bullet points (`●`, `•`, `▪`, `◦`, `►`, `■`) without collapsing section boundaries into preceding sentences.
3. **Multi-Pattern Timeline Extractor (`extract_timeline_entries`)**:
   - Added robust timeline extractor that segments candidate roles by date boundaries and company title patterns.
   - Handles title abbreviations (`Sr.`, `Jr.`) without improper sentence boundary truncation.
   - Extracts candidate name, contact info, summary, skills, all 8 historical roles, and education/certifications.
4. **Database Record Synchronization**:
   - Re-parsed the active database `Resume` record (`parsed_json_encrypted` and `skills_extracted`) to restore all 8 historical roles with full accomplishment bullets and contact details.

---

## 14. Background Task Engine Runner Fixes (LinkedIn Sync & Hiring Posts Scanners)

> **Superseded:** The LinkedIn Hiring Posts content-search scanner (`search_linkedin_hiring_posts`, `_run_hiring_posts_task_runner`, `HiringPost`, `POST /api/linkedin/search-hiring-posts`, the `hiring_posts_scan` schedule, and `generate_hiring_post_outreach`) was later **removed** after LinkedIn migrated to hashed CSS-module class names, which broke content-post parsing. It was replaced by the structured **Opportunity Radar** (`GET /api/opportunities/radar`). The notes below are retained as historical context.

### The Problem
In the Background Tasks & Schedules dashboard, automated tasks showed failures or 0 items:
1. `Hiring Posts Scheduled Scan (Auto)` failed immediately (`0.2s`, status `FAILED`) because `_run_hiring_posts_task_runner` passed `query`, `max_scrolls`, and `db` to `search_linkedin_hiring_posts`, which only accepted `company_name: str`.
2. `LinkedIn Scheduled Sync (Auto)` completed with 0 items and logged `TypeError: Object of type Session is not JSON serializable` because the SQLAlchemy `db` session was accidentally passed as the second argument (`headless: bool`) to `scan_and_sync_linkedin_saved_jobs(p, db, max_pages=3)`.

### The Solution
1. **Playwright App Signature Modernization ([`backend/playwright_app.py`](../../backend/playwright_app.py))**:
   - Updated `search_linkedin_hiring_posts(playwright_instance, company_name="", role_title="", query="", headless=True, limit=10, page=None, max_scrolls=3, db=None)` to accept custom search queries, role titles, and max scrolls with intelligent fallback query composition.
2. **Background Task Runners Ingestion ([`backend/main.py`](../../backend/main.py))**:
   - In `_run_linkedin_sync_task_runner`: Fixed calls to `scan_and_sync_linkedin_saved_jobs(p, headless=True, auto_unsave_closed=True, max_pages=3)` and `scan_linkedin_job_alerts(p, headless=True, limit=10)`. Ingests active saved jobs as `"Shortlisted"`, prunes closed positions as `"Rejected"`, and records `ApplicationEvent` logs.
   - In `_run_hiring_posts_task_runner`: Properly calls `search_linkedin_hiring_posts`, ingests discovered posts into `HiringPost` records in the database, and reports granular progress back to the Task Engine and dashboard.

---

## 15. Application Status Clean-Up & Job Dossier Tech Stack / Hiring Team Upgrades

### 1. Application Status Clean-Up ([`backend/scripts/cleanup_interview_statuses.py`](../../backend/scripts/cleanup_interview_statuses.py))
- Cleaned up legacy misattributed email scan entries:
  - **Job 106 (large retail tech employer)**: Reset from `Interview` $\rightarrow$ `To Apply`.
  - **Job 162 (early-stage startup)**: Reset from `Interview` $\rightarrow$ `To Apply`.
  - **Job 181 (product-analytics startup)**: Re-linked from a generic company label to the proper company name.
  - **Job 102 (AI research lab)**: Verified and preserved in `Interview` status.
- Recorded timeline audit `ApplicationEvent` logs for each correction.

### 2. Tech Stack & Requirements Compatibility Matrix ([`backend/ai_helper.py`](../../backend/ai_helper.py), [`frontend/index.html`](../../frontend/index.html), [`frontend/styles.css`](../../frontend/styles.css), [`frontend/app.js`](../../frontend/app.js))
- **`extract_job_skills_and_alignment(job_desc, candidate_skills)`**: Performs zero-token deterministic extraction across languages, databases, cloud, pipelines, and architecture paradigms, cross-referencing against the candidate's active decrypted resume skills.
- **Dossier UI Visual Stack Compatibility**:
  - **Progress Fill Meter**: Displays `% Stack Match` (e.g., `80% Stack Match • 4 Matched • 1 Growth Area`).
  - **Matched Stack Badges (`✓`)**: Glowing green badges for verified skills present on the resume.
  - **Growth / Gap Stack Badges (`⚡`)**: Amber badges with 1-click **"⚡ Bridge Gap in Resume"** that auto-populates tailoring bullet suggestions into the Resume Tips tab.

### 3. Embedded Hiring Team & Recruiter Dossier ([`backend/main.py`](../../backend/main.py), [`frontend/index.html`](../../frontend/index.html))
- **`GET /api/jobs/{job_id}/hiring-team`**: Aggregates a talent-acquisition / engineering-manager LinkedIn directory contact for the company.
- **Hiring Team Tab**: Displays contact avatar, name, title, source, 1-click **"👤 Profile ↗"** shortcut, and **"💬 Draft Note"** which pre-populates a personalized recruiter connection note directly in the Cold Note tab.

---

## Validation & Test Results

```bash
$ ./.venv/bin/pytest -v tests/
======================== 231 passed, 5 skipped in 4.90s ========================
```
- **JavaScript Syntax Validation**:
  ```bash
  $ node -c extension/content/ats-autofill.js && node -c extension/sidepanel/sidepanel.js && node -c extension/js/runtime-adapter.js && node -c frontend/app.js
  # Clean exit (code 0)
  ```
- **Total Tests Passing**: 231 (100% pass rate)
