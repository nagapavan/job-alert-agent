# Design & Policies

Why the system is built this way, and the invariants that must not be broken: the compute and
token policy, privacy and opt-in guardrails, performance strategy, the security model, the shutdown
drain, and the per-channel reliability policies. Section numbers are preserved from the `AGENTS.md`
they were split out of, so existing references (`DESIGN.md` §10.2, §12.11, …) resolve to the same
content.

- Behavioural rules for agents and contributors live in [`AGENTS.md`](AGENTS.md).
- File map, schema, dependencies and onboarding live in [`PROJECT_STRUCTURE.md`](PROJECT_STRUCTURE.md).
- Diagrams are in [`DESIGN_DIAGRAMS.md`](DESIGN_DIAGRAMS.md); the agentic layer is in
  [`AGENTIC_ARCHITECTURE.md`](AGENTIC_ARCHITECTURE.md).

## Contents

| Section | Covers |
| :--- | :--- |
| §3 | Token optimisation and LLM conventions |
| §7 | Privacy and opt-in guardrails |
| §10 | Performance optimisation strategies |
| §11 | Security implementation details |
| §12.11 | Graceful shutdown drain |
| §13 | Reliability policies (Google Jobs cooldown, embedding chain, extension mode) |

---

## 3. Token Optimization & LLM Conventions

- **Discovery Pacing & Token Saver**: During background scraping or LinkedIn sync, **never pre-generate full cover letters or cold outreach messages synchronously**. Only compute match alignment scores. Heavy application materials must be generated on-demand when the user clicks "⚡ Generate Materials" or launches "Apply".
- **Deterministic-First Bulk Scoring**: All job scoring (discovery & rescore) is two-tier: fast deterministic classifier (~93% coverage, ~0.1–0.3 s per posting, no LLM), with a bounded LLM fallback only for signal-less JDs. Discovery artifacts (LinkedIn snippets, Gmail recruiter lines) are identified and marked unscored without LLM spend. See [`docs/BULK_MATCH_SCORING.md`](docs/BULK_MATCH_SCORING.md) for implementation details.
- **Zero-Token Semantic Ignore Filter**: Always evaluate `check_semantic_dismissal` before running AI match prompts on newly discovered roles to skip unneeded LLM inference.
- **LinkedIn Saved Jobs Full-Fidelity Ingestion**: Saved jobs represent explicit user intent. Always ingest active saved jobs **as-is** with status `"Shortlisted"`, bypassing negative dismissal filters, and auto-upgrade `Company.careers_url` if an external ATS portal link is present.
- **No LinkedIn Network Calls in Rescore / Backfill / Migration Scripts**: Match rescoring, partial-coverage backfill, and any data-migration script must **never** open a Playwright browser or make a LinkedIn HTTP call. Score from the **cached** `jobs.description` only. LinkedIn is a session-driven, ToS-restricted channel (see `docs/ATS_PORTALS.md` §Risk tiers); re-hitting `linkedin.com/jobs/view/` from a maintenance script multiplies account-suspension risk. The only paths allowed to call `fetch_linkedin_job_details` are the **live sync flows** (`sync_external_applications`) and the one-shot `backend/scripts/repair_linkedin_saved_jobs.py` (which requires explicit `--fetch` and the user's awareness).

---

## 7. Privacy & Opt-in Guardrails

- **Server-persisted user preferences**: Discovery preferences live in the single-row `UserPreference` table (`GET/PUT /api/preferences`). Manual **and** background/scheduled runners must read these (not derive targets solely from the resume). A missing value falls back to resume-derived data only as a last resort.
- **Opt-in feature flags** (`UserPreference.features_json`, defaults in `backend/database.py::DEFAULT_FEATURE_FLAGS`): master channels `ats_portals`, `google_jobs` (**off**), `linkedin_sync` (**off**), `gmail_sync` (**off**); the `ats_hirist` candidate-portal status-sync flag (**on**); the UI-insight `resume_gap_insights` flag (**on** — per-job Resume Gap Insights panel in the job modal); per-portal ATS flags `ats_greenhouse`/`ats_lever`/`ats_ashby` (**on** — public, documented APIs) and `ats_smartrecruiters`/`ats_workable`/`ats_workday`/`ats_uber`/`ats_custom_html` (**off** — unofficial/ambiguous endpoints, require explicit opt-in consent). Disabled channels are hidden in the SPA and **skipped server-side** (`trigger_jobs_scrape`, `sync_external_applications`; scheduler automatic runs; explicit "Run now" passes `force=True`).
- **Employer/domain blocklist** (`UserPreference.excluded_companies`, seeded from `DEFAULT_EXCLUDED_COMPANIES`): a company whose name/domain matches any comma-separated token is never scanned (`backend.database.is_company_excluded`). The seed covers employers whose own terms forbid automated access — Amazon, Booking.com, eBay, Target, HEINEKEN. Common-word employers are seeded by domain (`target.com`), never by the bare word.
- **Per-portal risk tiers & disclaimers**: scanning/scraping follows each site's terms; see [`docs/ATS_PORTALS.md`](docs/ATS_PORTALS.md) §"Risk tiers, opt-in & consent". This is **not legal advice**; the user is responsible for compliance with each site's terms of service.
- **Opportunity Radar (`GET /api/opportunities/radar`)**: structured, ranked hiring-intent feed derived from already-ingested jobs (LinkedIn alerts/recommendations, ATS portals, Google Jobs, Gmail recruiter outreach). Scored deterministically by `compute_hiring_intent` in `backend/main.py` — no LLM calls and no LinkedIn content-post scraping (the unofficial `/api/linkedin/search-hiring-posts` path was removed).
- **Gmail is opt-in and privacy-scoped** (amends the original "no OAuth tokens" guardrail as an explicit opt-in exception):
  - Preferred path: **BYOK Gmail API** (`backend/gmail_client.py`) — narrow `q`, `metadata`+snippet only, `maxResults`/page caps, `messageId` dedup (`ProcessedEmail`), encrypted client secret + refresh token (`data/gmail_settings.json`, 0600). **Never enumerate the full mailbox.**
  - Fallback: restricted browser-session scanner (`scan_gmail_for_job_alerts` — targeted search only; no full-inbox / category scans).
- **BYOK secrets encrypted at rest**: LLM keys (`save_llm_settings`) and Gmail OAuth credentials (`save_gmail_settings`) are Fernet-encrypted; env vars always override persisted values. `/api/logs` requires auth; `/api/gmail/oauth2callback` is the only Gmail-related public path (loopback).
- **Single-owner browser profile**: `get_persistent_browser_context` raises `BrowserProfileBusy` rather than deleting live locks or launching a logged-out context; `clean_stale_profile_locks` is PID-aware. Background tasks skip when the profile is in use.
- **Prompt-injection boundary**: scraped/external text is wrapped with `wrap_untrusted`; generated text is passed through `sanitize_generated_text` before display/injection. The LLM never submits applications (human confirms via `submission_confirmed`).
- **Documentation map**: see `docs/README.md`; feature docs in `docs/` (e.g. `docs/GMAIL_API.md`).
- **Published artifacts**: issues, PR/commit messages, code, tests, fixtures, docs and reasoning logs must never carry the operator's personal or resume-derived data — the rule and its self-check live in `AGENTS.md` §7.9.

## 10. Performance Optimization Strategies

Performance work here is dominated by **avoiding unnecessary LLM calls and protecting local
GPU memory**, not by shaving CPU. The key strategies:

### 10.1 Tiered compute & dynamic concurrency

- **Task-complexity routing** sends trivial work to small/fast models (see `AGENTIC_ARCHITECTURE.md` §5.2).
- **`LLMQueueManager`** (`backend/llm_queue.py`) is a priority min-heap with adaptive concurrency:
  - `INTERACTIVE=1` < `ON_DEMAND=2` < `BACKGROUND=3` (lower runs first).
  - Local models: **1 worker, 0.2 s pacing** — prevents GPU/VRAM thrashing and KV-cache churn.
  - Cloud APIs: up to **8 parallel workers, 0.0 s pacing**.
  - `sync_provider_concurrency(is_local)` applies the config values; `set_concurrency()` scales
    live. Backpressure is signalled when `queue_depth >= max_workers * 4`.
- **Bulk operations pace themselves**: `bulk_tailor_jobs` sleeps `0.5 s` between jobs (KV-cache
  cycling); `bulk_apply` sleeps `0.4 s` between tabs; LinkedIn uses
  `LINKEDIN_AUTOMATION_PACING_SECONDS` (default 4.0 s).

### 10.2 Token elimination (the biggest win)

- **Zero-token semantic dismissal** (`check_semantic_dismissal`, default `0.80`) runs **before**
  any match prompt on new listings, skipping inference for ignored role patterns.
- **Cache-first screener answers** (`resolve_semantic_essay_cache`, `0.85`; chat `0.82`) return
  verified answers at ~0 tokens and increment `use_count`.
- **Grounding/anti-slop reruns** only occur for material generation (on-demand), never during
  discovery sync.
- **Discovery never pre-generates heavy materials**; it only computes match scores.
- **Deterministic match scoring first** (`POST /api/match/analyze`): `build_deterministic_assessment`
  (`backend/match_classifier.py`) extracts requirements from JD skills/years/seniority/education
  signals and the parsed resume with **no LLM call**, then the ML/heuristic classifier
  (`get_classifier().predict`) scores the features. Band/recommendation come from the shared
  `ScoringConfig` via `band_and_recommendation` (`backend/match_scoring.py`) — no hardcoded
  thresholds. The full LLM extractor (`assess_job_requirements`) runs **only** as a fallback when
  the deterministic path yields no requirements, so bulk ATS scoring never saturates the LLM queue.
  Technology-scoped year requirements (e.g. "3 years Golang") are evaluated against per-technology
  tenure inferred from the resume's dated experience entries (`resume_signals.estimate_technology_years`;
  overlapping projects merged so shared time is counted once). When tenure cannot be inferred, presence
  earns **partial** (never a full "met"), and the JD-vs-resume comparison is surfaced in the result
  feedback so the user can judge borderline cases rather than have the tool decide silently.

### 10.3 Vector search scaling

- Default SQLite path computes cosine similarity in Python → **O(n) per query**. For large job
  sets set `DATABASE_URL` to PostgreSQL with `pgvector` so `SafeVector` maps to a native indexed
  `vector` column.
- Radar caps its candidate scan at 500 rows before ranking and slices to the requested `limit`
  (`ge=1, le=200`, default 30).

### 10.4 Request & I/O bounds

- Scraper timeouts are tight and per-endpoint (DDG 6 s; ATS list 15 s; ATS detail 8 s; Gmail
  status 12 s; active probe 10 s).
- Page/result caps: ATS `max_matches=35`, Google Jobs `max_results=15`, Gmail API `PAGE_SIZE=50`
  × `MAX_PAGES=2` (~100 messages), `/api/logs` lines ≤1000, operation logs default limit 50.
- Playwright scroll/pagination is bounded per flow (e.g. saved jobs `max_pages=3`, 5 scrolls per
  page, 450 ms waits). Detail-page fetches are a separate budget: `LINKEDIN_SAVED_JOB_DESC_FETCH_LIMIT`
  (default 25, env-overridable) caps how many saved jobs get their full "About the job" text.

### 10.5 Non-blocking execution & streaming

- Long discovery/sync work runs on daemon threads via `BackgroundTaskEngine` and returns HTTP
  **202** immediately. Apply is *not* in that category: `/api/jobs/{id}/apply` returns
  `ApplyMaterial` synchronously and never launches a browser — the user's own browser applies, via
  the extension (`extension/content/dashboard-bridge.js`).
- Progress streams over SSE with a **15 s heartbeat** and `X-Accel-Buffering: no`.
- Token accounting is **O(1) in memory** (`TokenTracker.get_stats()`), with incremental
  DB-only-delta sync (`id > last_aggregated_log_id`) instead of full recalculation.

### 10.6 Known opportunities (be careful)

- `GET /api/jobs` returns all matching rows with **no LIMIT** and attaches `company_name` in a
  Python loop; there is **no `joinedload`/`selectinload`** anywhere. Add pagination + eager
  loading before scaling to very large job tables.
- `top_p` in `TASK_PROFILES` is not yet forwarded to providers.

---

## 11. Security Implementation Details

Security is layered: origin firewall → API-key/same-origin auth → column-level encryption →
isolated browser profile → prompt-injection boundary → opt-in flags + human-in-the-loop.

### 11.1 Origin firewall & CORS

`backend/main.py` installs `CORSMiddleware` with a strict localhost allowlist and an **empty-by-default** extension allowlist:

```python
ALLOWED_ORIGINS = [
    "http://localhost:8000",
    "http://127.0.0.1:8000",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
]  # + env ALLOWED_ORIGINS

# Extensions are trusted only when their exact ID is allowlisted via ALLOWED_EXTENSION_IDS.
# By default the list is empty, so no chrome-extension:// origin is accepted at all.
_allowed_extension_ids = [eid.strip() for eid in os.environ.get("ALLOWED_EXTENSION_IDS", "").split(",") if eid.strip()]
_extension_origin_regex = (
    r"^chrome-extension://(" + "|".join(re.escape(eid) for eid in _allowed_extension_ids) + r")$"
    if _allowed_extension_ids else None
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

Two properties matter here and both differ from a naive reading:

- **No wildcard extension regex.** A blanket `^chrome-extension://.*$` would let *any* installed
  browser extension call the API. Instead the regex matches only explicitly allowlisted IDs, and is
  `None` when the list is empty. Extensions authenticate with the `X-API-Key` header instead of
  relying on origin trust.
- **`allow_credentials=False`.** Credentials are passed explicitly (header / `?api_key=`), not via
  ambient cookies, so the credentialed-CORS hazard of combining wildcard origins with
  `allow_credentials=True` does not arise.

Note the loopback auth fallback in §11.2 is what actually makes the local SPA work: same-origin
requests from `localhost:8000` are accepted without an API key.

### 11.2 Authentication (`security_firewall_middleware`)

All routes except a small public set are gated by `security_firewall_middleware`, which accepts
**any** of: `X-API-Key` header, `Authorization: Bearer`, `?api_key=`, a same-origin/localhost
origin/referer, `Sec-Fetch-Site: same-origin`, or a loopback client (`127.0.0.1`, `::1`,
`testclient`). Otherwise it returns **401**.

- **Public paths**: `/`, `/api/health`, `/api/auth/status`, `/docs`, `/openapi.json`,
  `/favicon.ico`, `/api/gmail/oauth2callback`, `/static/*`, and all `OPTIONS` preflights.
- `verify_api_key` is also applied to `GET /api/auth/client-config`.
- Key validation uses **constant-time** comparison to prevent timing attacks:

```python
def is_valid_api_key(provided_key: str) -> bool:
    if not provided_key or not API_KEY:
        return False
    return secrets.compare_digest(provided_key.strip(), API_KEY.strip())
```

### 11.3 Secret storage & keys

- `.key` — Fernet key (`load_or_create_key`, `chmod 600`), env `JOB_ALERT_AGENT_SECRET_KEY` overrides.
- `.api_key` — system API token (`secrets.token_urlsafe(32)`, `chmod 600`), env
  `JOB_ALERT_AGENT_API_KEY` overrides.
- `data/llm_settings.json` and `data/gmail_settings.json` are `chmod 600`; API keys / OAuth
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

- Read-only scope: `https://www.googleapis.com/auth/gmail.readonly`.
- **All-mail, narrow query**: the scan searches ALL labels (Inbox, Updates, Promotions, Social,
  user labels — Gmail itself excludes Spam/Trash) but the `q` stays narrow (job-board senders +
  interview/offer/rejection subjects, `newer_than:90d`), so there is still **no mailbox
  enumeration**; metadata + snippet only; `PAGE_SIZE=50`, `MAX_PAGES=2`; dedup via `ProcessedEmail`.
  The browser fallback navigates straight to the global `#search/` view (mailbox-wide) with the
  search box as fallback, and reads 2 result pages.
- `/api/gmail/oauth2callback` is the only public Gmail path (loopback).

### 11.7 Prompt-injection & output safety

External text is wrapped with `wrap_untrusted` (which strips pre-existing boundary tags), generated
text is passed through `sanitize_generated_text` (strips `<script>`/`<style>`/HTML/control chars),
and the extension HTML-escapes values before DOM insertion. The LLM **never** submits
applications — a human confirms (`submission_confirmed`).

### 11.8 Opt-in guardrails

Opt-in feature flags in `UserPreference.features_json` (`linkedin_sync`, `gmail_sync`,
`google_jobs` default **off**) hide channels in the UI and are **skipped** by the scheduler.
Explicit "Run now" passes `force=True` to bypass the gate for a single manual run.

---

### 12.11 Graceful shutdown (`backend/shutdown.py`)

The app runs several daemon threads (scheduler, task engine, LLM queue) and owns a single-user
Playwright profile. Without a drain, an abrupt `kill`/`Ctrl-C` abandoned in-flight work and often
left Chromium's `SingletonLock` behind, so the next launch raised `BrowserProfileBusy`.

```python
# backend/main.py
@app.on_event("shutdown")
def _graceful_shutdown_drain():
    from backend.shutdown import run_shutdown
    run_shutdown()
```

**Drain order** (`run_shutdown` → `_drain`, bounded by `DRAIN_DEADLINE_SECONDS = 10.0`):

1. `scheduler.stop()` — stop triggering new runs.
2. `task_engine.cancel_all()` — cooperatively cancel queued/running tasks, then wait (bounded) for
   them to observe cancellation.
3. `llm_queue.stop()` — stop dispatching new provider calls.
4. `clean_stale_profile_locks()` — release the browser profile **last**, and only when no live
   process owns it.

`run_shutdown()` is idempotent (first caller drains; later calls no-op) and every step is wrapped so
teardown can never raise. During a drain, `GET /api/health` reports `status: "shutting_down"` and
`shutting_down: true` so a supervisor can tell "draining" from "dead".

> **Do NOT register your own signal handlers.** Uvicorn owns `SIGTERM`/`SIGINT` and fires the ASGI
> shutdown lifecycle on every graceful path (`--reload` child, `--workers`, standalone). Because the
> ASGI app is imported during `Server._serve`, anything we registered would clobber uvicorn's
> handler, and both prior alternatives broke `--reload`:
>
> - raising out of the handler aborted uvicorn's event loop and killed the reloader parent;
> - swallowing the signal left the child alive, so the reloader blocked forever in `process.join()`.
>
> Keep the drain in the lifecycle. See `reasoning/2026-10-16-graceful-shutdown-on-signals.md`.

**Gotcha:** `LLMQueue` is a `PriorityQueue` — never push a `None` sentinel, because `heappop` cannot
compare `None` against the tuple keys.

## 13. Reliability policies (2026-11-06)

### 13.1 Google Jobs: block cooldown & query rotation (issue #104)

Google answers automated traffic with `/sorry/index?continue=...`. The block is neither
query-specific nor retryable, so it is treated as a circuit breaker, not a log line:

- **First `blocked` outcome stops the run.** The remaining queries are not attempted.
- **Cooldown is persisted** in `data/google_jobs_state.json` (`blocked_until`), length
  `GOOGLE_JOBS_COOLDOWN_HOURS` (default 168 h).
- **While cooling down the runner exits before opening a browser** and records the channel outcome
  `cooling_down` with the deadline — the Operations log reads "cooling down until `<UTC>`".
- **Queries rotate:** each run scans `GOOGLE_JOBS_QUERIES_PER_RUN` (default 3) starting at a persisted
  cursor that wraps, so the set is covered across runs instead of in one burst.
- **A clean run clears the cooldown** and advances the cursor.
- **`google_jobs` is opt-in** (`DEFAULT_FEATURE_FLAGS`), and the SPA hides the discovery tile (not
  just the toolbar button) when the flag is off.
- **Never attempt to defeat the interstitial.** CAPTCHA solving and IP rotation are out of scope
  (ToS + escalation). Supported alternatives: Gemini API grounding with Google Search (billed per
  query, returns grounded answers not a SERP), or a third-party SERP API. The Custom Search JSON API
  is closed to new customers. No model — Gemma included — changes this: the block happens before
  content is served.

### 13.2 Embedding chain: report, stamp, re-embed (issue #100)

- `active_backend_name()` **probes** (real 1-token embed, 2 s timeout, 30 s cache). It must never
  report a backend merely because `available()` says so — the external backend reports itself
  available without contacting the server.
- `last_embed_stamp()` records the `(backend, model)` that produced the newest vector on the calling
  thread. Hot paths (`requirements_extractor.resume_supports`, `section_intent._embedding_label`)
  decide whether the semantic tier is trustworthy from that stamp, never from configuration.
- Every vector-bearing table (`jobs`, `resumes`, `resume_chunks`, `application_question_answers`,
  `dismissed_job_patterns`) carries `embedding_model` / `embedding_dim`, written by one
  `before_flush` listener from the backend that actually served the embed.
- The hash fallback logs **once per process with the reason**.
- `scripts/reembed_all.py` rebuilds vectors into the current space (batched, resumable, skips rows
  already stamped with the target model, refuses to write hashes unless `--allow-hash`).
- Provisioning the ONNX model is manual by design (`EMBEDDING_ALLOW_DOWNLOAD=false`): use
  `POST /api/settings/embeddings/download` or the Settings button. The endpoint now returns **502**
  with the error instead of 200 + `model_cached: false`.

### 13.2.1 ONNX session lifecycle & telemetry (issue #106)

- **One session per model, process-wide.** `_ONNX_SESSIONS[(model_id, cache_dir, allow_download)]`
  holds `(tokenizer, InferenceSession, input_names)`. Previously `resolve_backends()` built new
  backends per call, so every embed re-parsed the model (~85 ms) and churned native thread pools.
  Now: first call ~350 ms (load), subsequent ~2 ms.
- **`resolve_backends()` memoizes the chain** per settings tuple and returns a fresh list, so the
  backend objects are singletons but callers cannot corrupt the cache.
- **`encode_batch` + `session.run` are serialized by `_EMBED_LOCK`** — the tokenizer and session are
  shared across threads.
- **Telemetry is disabled before the first session** (`onnxruntime.disable_telemetry_events()`).
  ORT 1.21+ ships Microsoft's 1DS client: it makes an outbound call nobody configured and aborts the
  process at exit on macOS (`recursive_mutex lock failed`, microsoft/onnxruntime#24579).
- **`requirements.txt` pins `onnxruntime==1.24.1`** — its macOS arm64 wheel has **no**
  `PosixTelemetry` symbols (verified with `nm`), while 1.30.0 has 75. It is also the oldest release
  with a CPython 3.14 wheel (1.20–1.23 have none, so the commonly cited 1.20.1 pin is not
  installable here). Re-check the wheel's symbols before raising the pin.

### 13.3 Extension execution mode (issue #101)

- `_backendUsable()` = `isConnected && (mode === "connected" || no Nano session)`; used by match
  analysis, profile sync, grounded pitch, form telemetry and copilot chat.
- `analyzeJobMatch` awaits the in-flight `init()` **and** the bounded on-device probe before
  concluding Nano is unavailable (cold-panel race).
- A persisted `mode: "standalone"` with no session is reverted to `connected`; the SPA disables the
  On-Device switch while `nanoState` is `unsupported` / `unavailable` / `timeout`.
- Failure text names the cause (backend unreachable vs connected-but-unscorable).
- **Extension origins always need the API key.** The firewall's loopback fallback requires
  `not origin`, and a side panel always sends `Origin: chrome-extension://<id>`; paste `.api_key`
  into Profile → API key (or allowlist the ID via `ALLOWED_EXTENSION_IDS`). `/api/health` stays
  public, so "Connected (API)" can coexist with 401s on every authenticated call.
