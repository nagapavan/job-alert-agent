# Project Index — Files & Responsibilities

Quick-lookup reference for locating where changes should be made.

---

## Backend

### `backend/ai_helper.py`
**Responsibility:** Central LLM abstraction layer.
- `TokenTracker` — thread-safe token counting & cost estimation.
- `call_lmstudio` / `call_ollama` / `call_openai` / `call_gemini` / `call_anthropic` — per-provider inference wrappers.
- `get_model_for_tier` / `resolve_task_routing` — model selection by task complexity.
- `generate_text` / `generate_structured` — unified generation entry points (with priority queueing).
- `detect_active_llm_provider` — runtime provider discovery.
- `analyze_job_match` — resume↔JD matching; returns `match_score=None` (+ `error`) on failure — never fabricates a score.
- `generate_interview_questions` — JD-derived interview questions with STAR scaffolds & resume-grounded suggested answers.
- `generate_embeddings` / `cosine_similarity` — semantic similarity primitives.
- `adapt_answer_variables` / `classify_question_category` — answer tailoring & categorization.
- `resolve_semantic_essay_cache` / `generate_and_cache_essay_answer` — essay caching pipeline; cache hits record a measured `tokens_saved`.
- `gather_company_intelligence` — company research synthesis.
- `verify_and_judge_grounded_pitch` / `verify_and_judge_grounded_cover_letter` — hallucination/grounding verification.
- `generate_consolidated_application_package` — returns only score/strengths/gaps and delegates materials to the vetted generators (resume tailoring, cover letter, cold outreach).
- No-fabrication contract — match/package schemas use an optional (`None`) `match_score`; failures return `None`/`""` + `error`, not invented values.

### `backend/company_catalog.py`
**Responsibility:** Loads the static company catalog used for discovery/targeting.
- `load_company_catalog()` — returns list of company dicts; reads `backend/companies_config.json` else falls back to the shipped generic `backend/companies_config.example.json`.

### `backend/config.py`
**Responsibility:** Encryption & secret management.
- `load_or_create_key` / `encrypt_data` / `decrypt_data` — symmetric encryption for stored secrets.
- `load_or_create_api_key` — backend API key bootstrap.

### `backend/database.py`
**Responsibility:** SQLAlchemy models & DB schema.
- `SafeVector` — vector column type decorator.
- `ApplicationEvent` — application lifecycle event records.
- `ApplicationQuestionAnswer` — Q&A persistence for applications.
- `Job.match_scored` — Boolean flag set True only by a real AI analysis (discovery baselines stay False → UI "not AI-scored" cue); auto-migrated in `init_db()`.
- `prune_operation_logs` — caps `OperationLog` rows by count/age; `OperationLog.created_at` is indexed.

### `backend/gmail_client.py`
**Responsibility:** Opt-in BYOK Gmail API client (privacy-scoped).
- `build_auth_url` / `exchange_code_for_tokens` — loopback OAuth (BYOK client id/secret).
- `build_service` / `get_profile_email` — authenticated Gmail service.
- `list_message_ids` / `fetch_message_metadata` — narrow query, metadata+snippet only.
- `run_gmail_sync` — classifies decision emails; dedups via `ProcessedEmail`.
- `get_status` — masked connection status.

### `backend/llm_queue.py`
**Responsibility:** Dynamic-concurrency priority queue for LLM calls.
- `LLMQueueManager` — local models run at concurrency 1 with pacing; cloud models scale to `MAX_CONCURRENCY`.
- `set_concurrency` / `sync_provider_concurrency` — runtime tuning.

### `backend/main.py`
**Responsibility:** FastAPI app, routes, CORS/auth firewall, background-task endpoints.
- `POST /api/match/analyze` — stateless structured resume↔job match analysis (`match_score`, `strengths`, `gaps`, `feedback`); falls back to the active resume and never fabricates a score (`match_score=None` + `error` on failure).
- `POST /api/jobs/{job_id}/interview-questions` — JD-derived questions with STAR scaffolds & resume-grounded answers (400 without an active resume, 404 missing job, 502 on generation error).
- `POST /api/tasks/sync/all` — one-shot "Sync All" orchestrator (ATS portals → Google Jobs → LinkedIn → application-status sync) with per-stage skip/fail handling.

### `backend/observability.py`
**Responsibility:** Structured logging & LLM telemetry.
- `JSONFormatter` / `ConsoleFormatter` — log formatting.
- `log_llm_event` — emits per-inference metrics (tokens, latency, cache hits).
- `setup_observability` — rotating sinks for `logs/app.log` + a dedicated `logs/llm_audit.log` sink; env `LOG_LEVEL`, `LOG_MAX_BYTES`, `LOG_BACKUP_COUNT`.
- Operation-log retention env: `OPERATION_LOG_MAX_ROWS`, `OPERATION_LOG_RETENTION_DAYS`, `OPERATION_LOG_PRUNE_INTERVAL_SECONDS`.

### `backend/parser.py`
**Responsibility:** Resume & job description text processing.
- `normalize_resume_text` / `clean_job_description` — text cleanup.
- `segment_resume_sections` — section splitting.
- `is_bullet_or_detail` / `clean_detail_line` — bullet normalization.
- `extract_timeline_entries` — work history extraction.
- `parse_open_source_resume` — structured resume parsing.
- `extract_text_from_pdf` — PDF ingestion.

### `backend/preference_filter.py`
**Responsibility:** Reusable discovery preference filtering.
- `PreferenceFilter` / `PreferenceCriteria` — shared title/location/work-mode matching across ATS, Google Jobs, LinkedIn, and Gmail discovery scans.
- `check_location_match` / `check_workmode_match` / `accepts` / `rejection_reason` — match checks including broad-region rejection.

### `backend/playwright_app.py`
**Responsibility:** Browser automation infrastructure.
- `clean_stale_profile_locks` — profile lock cleanup.
- `get_persistent_browser_context` — persistent Playwright context.
- `parse_email_classification` — email triage classification.

### `backend/scheduler.py`
**Responsibility:** In-process background job scheduling.
- `ScheduledJobConfig` — job configuration model.
- `PeriodicScheduler` — daemon thread running trigger-based jobs.
- `start` / `run_job_now` — lifecycle & manual trigger.

### `backend/scraper.py`
**Responsibility:** Job discovery & scraping.
- `scrape_greenhouse_jobs` / `scrape_lever_jobs` / `scrape_ashby_jobs` / `scrape_smartrecruiters_jobs` / `scrape_workable_jobs` / `scrape_workday_jobs` / `scrape_uber_jobs` / `scrape_custom_jobs` — ATS/public-feed scrapers (shared filter signature).
- `check_title_match` / `is_location_eligible` / `detect_foreign_restriction` — shared title & location filtering.
- `extract_slug_from_careers_url` — ATS slug extraction.
- `unwrap_shortened_url` / `unwrap_google_redirect_url` — URL resolution.
- `probe_job_url_active` — shared/portal-agnostic requisition-active fallback probe.
- `probe_greenhouse_job_active` / `probe_lever_job_active` / `probe_smartrecruiters_job_active` — per-portal active probes.
- `check_greenhouse_application_status` — tokenized Greenhouse candidate status page.
- `gather_company_intelligence` — scraping-side company research.
- `extract_company_and_role_from_email_header` — loads OPTIONAL employer aliases from `data/company_aliases.json` (empty default; regex heuristic fallback).
- Design & roadmap: [`docs/ATS_PORTALS.md`](docs/ATS_PORTALS.md).

### `backend/task_engine.py`
**Responsibility:** Long-running background task orchestration.
- `TaskProgress` — progress model.
- `CancellationToken` — cooperative cancellation.
- `BackgroundTask` — task wrapper.
- `BackgroundTaskEngine` — worker pool, step callbacks, SSE broadcasting.
- `submit_task` / `_broadcast_event` — submission & event emission.

---

## Extension

### `extension/content/ats-autofill.js`
**Responsibility:** Content script injected into ATS pages.
- Context/security gating (OAuth, auth, non-application filters).
- `setElementValue` — safe-mode form filling (preserves pre-filled data).
- `getFieldIdentifier` — field matching heuristics.

### `extension/js/runtime-adapter.js`
**Responsibility:** Bridge between extension and backend / standalone mode.
- `getAuthHeaders` / `checkBackendStatus` — connectivity & auth.
- `initGeminiNano` — on-device fallback model.
- `generateGroundedPitch` — grounded pitch generation with local fallback.

### `extension/sidepanel/sidepanel.js`
**Responsibility:** Side panel UI logic.
- Tab navigation.
- `showToast` — user notifications.

---

## Root

### `run.py`
**Responsibility:** CLI entry point.
- `print_help` / `check_dependencies` / `start_server`.

### `conftest.py`
**Responsibility:** Pytest fixtures.
- `setup_test_db` / `isolate_token_tracker_stats` / `prevent_real_browser_spawns` — autouse isolation.
- `override_get_db` / `db_session` — DB fixtures.

### `tests/test_e2e.py`
**Responsibility:** End-to-end application lifecycle test.
- `test_full_application_lifecycle_e2e` — full flow with mocked external services.

### Committed example configs
**Responsibility:** Generic, committable defaults for local-only configuration.
- `backend/companies_config.example.json` — fallback target-company catalog used when the personal `backend/companies_config.json` is absent (gitignored).
- `data/company_aliases.example.json` — template for the optional `data/company_aliases.json` employer aliases (empty default; gitignored).

---

## Docs

### `docs/README.md`
**Responsibility:** Documentation index / map of all project docs.

### `docs/GMAIL_API.md`
**Responsibility:** Opt-in BYOK Gmail API design & setup (implemented).

### `docs/antigravity/`
**Responsibility:** Original Antigravity IDE planning artifacts (requirements, plan, task, walkthrough) — provenance only.

---

## Code Examples — Major Components

Short, real snippets showing the public contract of each major component. See the source files
for the full implementations, and `AGENTIC_ARCHITECTURE.md` / `AGENTS.md` for the surrounding
design (tiers, prompts, schema, security).

### `backend/ai_helper.py` — unified generation & structured outputs

```python
from backend.ai_helper import generate_text, generate_structured, TaskComplexity

# Free-form generation with task-tier routing + priority queueing
reply = generate_text(
    system_prompt="You are ...", user_prompt="...",
    task_type="career_copilot_strategy", task_name="Copilot",
)

# Schema-enforced generation (Pydantic V2)
from backend.ai_helper import JobMatchResult
result: JobMatchResult = generate_structured(
    schema=JobMatchResult, system_prompt=..., user_prompt=...,
    task_type="job_match_scoring", task_name="Match-Scoring",
)
print(result.match_score, result.strengths, result.gaps, result.summary)
```

```python
# Task-complexity routing: (provider, model, temperature, max_tokens, complexity)
provider, model, temp, max_tokens, complexity = resolve_task_routing(task_type="cover_letter")
# -> ("auto", "llama3.3:70b", 0.8, 2500, TaskComplexity.DEEP_REASONING)
```

```python
# RAG / embeddings / semantic memory
vec = generate_embeddings("eBPF and high-throughput streams")   # 384-d, L2-normalized
sim = cosine_similarity(vec, job.embedding)
hit = resolve_semantic_essay_cache(question, db=db, company_name="Acme", threshold=0.85)
dismissed = check_semantic_dismissal(job_title, job_description, db)   # default 0.80
```

### `backend/llm_queue.py` — dynamic concurrency dispatcher

```python
from backend.llm_queue import llm_queue, LLMPriority

# Interactive work jumps ahead of background batches
letter = llm_queue.submit(generate_cover_letter, resume, title, company, jd,
                          priority=LLMPriority.BACKGROUND, task_name="CoverLetter")

llm_queue.sync_provider_concurrency(is_local=True)   # 1 worker / 0.2s pacing
status = llm_queue.get_status()                       # depth, backpressure, est_wait
llm_queue.clear_background_tasks()                    # drain only BACKGROUND items
```

### `backend/database.py` — session dependency & helpers

```python
from backend.database import get_db, init_db, get_user_preferences, get_feature_flags

@app.get("/api/jobs")
def list_jobs(db: Session = Depends(get_db)): ...

init_db()  # create tables + ALTER TABLE auto-migrations + encrypt legacy Q&A
flags = get_feature_flags(db)      # {"ats_portals": True, "gmail_sync": False, ...}
```

### `backend/config.py` — encryption, keys, provider/Gmail settings

```python
from backend.config import encrypt_data, decrypt_data, is_valid_api_key, is_cloud_fallback_allowed

cipher_text = encrypt_data("sensitive resume text")
plain = decrypt_data(cipher_text)
ok = is_valid_api_key(request.headers.get("X-API-Key"))   # secrets.compare_digest
```

### `backend/scraper.py` — ATS scraping, due diligence, geo

```python
from backend.scraper import (
    scrape_greenhouse_jobs, scrape_lever_jobs, scrape_ashby_jobs,
    evaluate_job_due_diligence, is_location_eligible, extract_portal_info_from_job_url,
    fetch_google_jobs,
)

jobs = scrape_greenhouse_jobs("stripe", title_filters=["Engineer"], location_filters=["India"])
portal = extract_portal_info_from_job_url(linkedin_job_url)   # {careers_url, portal_type, domain}
verdict = evaluate_job_due_diligence(title, source_type="LinkedIn", portal_jobs=jobs, repost_count=4)
# -> {"is_ghost_job": True, "status_label": "Probable Ghost Job (Repeated Repost)", ...}
```

### `backend/playwright_app.py` — persistent browser session & sync

```python
from playwright.sync_api import sync_playwright
from backend.playwright_app import (
    get_persistent_browser_context, BrowserProfileBusy,
    scan_linkedin_job_alerts, scan_and_sync_linkedin_saved_jobs, inject_helper_panel,
)

with sync_playwright() as p:
    try:
        ctx = get_persistent_browser_context(p, headless=True)   # raises BrowserProfileBusy if locked
    except BrowserProfileBusy:
        ...
    page = ctx.new_page()
    alerts = scan_linkedin_job_alerts(p, max_alerts=5, max_jobs_per_alert=10)
    results = scan_and_sync_linkedin_saved_jobs(p, auto_unsave_closed=True, max_pages=3)
    inject_helper_panel(page, resume_data, cover_letter, tailored_points, cold_msg, job_id=42)
```

### `backend/parser.py` — resume & JD parsing

```python
from backend.parser import (
    parse_resume_document, extract_text_from_pdf,
    normalize_resume_text, segment_resume_sections, clean_job_description, split_candidate_name,
)

text = parse_resume_document(file_bytes, "resume.pdf")   # pypdf
sections = segment_resume_sections(text)
jd = clean_job_description(raw_jd, max_chars=1200)       # strips EEO/benefits boilerplate
first, last = split_candidate_name("Doe, Jane")
```

### `backend/gmail_client.py` — opt-in BYOK Gmail API

```python
from backend.gmail_client import build_auth_url, exchange_code_for_tokens, run_gmail_sync

auth = build_auth_url(client_id, client_secret)          # {"auth_url": ..., "code_verifier": ...}
tokens = exchange_code_for_tokens(code, client_id, client_secret, code_verifier=auth["code_verifier"])
result = run_gmail_sync(db)   # narrow query, metadata+snippet only, ProcessedEmail dedup
```

### `backend/task_engine.py` — background tasks + SSE

```python
from backend.task_engine import task_engine

task_id = task_engine.submit_task(
    task_type="google_jobs_scan", task_name="Google for Jobs Scan",
    fn=_run_google_jobs_task_runner, queries=[...], location="India", total_steps=6,
)                                  # returns immediately; runner gets cancel_token + progress_cb
task_engine.cancel_task(task_id)   # cooperative cancellation
task_engine.get_active_tasks()     # initial SSE snapshot
```

```python
# Runner signature the engine injects into
def runner(*args, cancel_token, progress_cb, **kwargs):
    progress_cb(1, "Scanning queries", items_found=0)
    cancel_token.check()   # raises InterruptedError when cancelled
    ...
```

### `backend/scheduler.py` — periodic automation

```python
from backend.scheduler import scheduler

scheduler.start()
scheduler.update_job("sched_linkedin_sync", is_enabled=True, interval_hours=12)
scheduler.run_job_now("sched_google_jobs", force=True)   # force bypasses opt-in flag gate
scheduler.get_all_jobs()                                  # next_run_at, last_status, last_task_id
```

### `backend/observability.py` — dual-sink logging & LLM audit

```python
from backend.observability import setup_observability, log_llm_event, tail_log_file

setup_observability()                       # console + rotating JSON (logs/app.log)
log_llm_event(task_name="CoverLetter", model="gpt-4o", provider="openai",
              prompt_tokens=850, completion_tokens=350, latency_ms=1800, temperature=0.8)
entries = tail_log_file(lines=100)          # parsed JSON log records
```

### `backend/chat_agent.py` — copilot dialogue & action dispatch

```python
from backend.chat_agent import CareerChatAgent

agent = CareerChatAgent(db)
out = agent.process_message("Shortlist the Stripe role and tailor my resume", history=[...])
# -> {"reply": "...", "actions_taken": [{"type": "status_update", ...}], "embedded_jobs": [...]}
```

### `backend/main.py` — app wiring, auth & deterministic scoring

```python
app = FastAPI(title="Job Alert Agent API", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=ALLOWED_ORIGINS,
                   allow_origin_regex=r"^chrome-extension://.*$",
                   allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

@app.middleware("http")   # security_firewall_middleware gates all non-public paths
async def security_firewall_middleware(request, call_next): ...

score, reasons = compute_hiring_intent(job)   # deterministic 0-100 hiring-intent score
```

```python
# Background task submission returns HTTP 202 + task_id immediately
@app.post("/api/tasks/discovery/google-jobs", status_code=202, response_model=TaskResponse)
def start_google_jobs_task(payload: SubmitTaskRequest) -> TaskResponse: ...
```

### `backend/geo.py` & `backend/company_catalog.py` — offline resolvers

```python
from backend.geo import resolve_countries, countries_for_terms, is_foreign_restricted

iso = resolve_countries("bangalore, india")  # -> {"IN"}  (geonamescache + pycountry, cached)
from backend.company_catalog import load_company_catalog
catalog = load_company_catalog()                                 # list[dict] of target companies
```

### `frontend/` — SPA (no build step)

```javascript
// app.js: talk to the same-origin API; X-Request-ID is echoed by the server
const res = await fetch("/api/jobs?status=Shortlisted");
const jobs = await res.json();

// Trigger a background scan and stream progress
await fetch("/api/tasks/discovery/google-jobs", {
  method: "POST", headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ queries: ["Staff Engineer India"], location: "India" }),
});
const es = new EventSource("/api/tasks/events");   // SSE progress
es.onmessage = (e) => updateProgressBar(JSON.parse(e.data));
```

### `extension/` — hybrid runtime adapter

```javascript
// runtime-adapter.js: Connected Mode (backend) vs Standalone (Gemini Nano)
const adapter = new RuntimeAdapter();
await adapter.initGeminiNano();                    // window.ai.languageModel (on-device fallback)
const pitch = await adapter.generateGroundedPitch(question, company, role);

// content/ats-autofill.js: safe-mode fill + form telemetry
chrome.storage.local.get(["candidateProfile", "localQABank"], (res) => { /* fill fields */ });
```

