# 🚀 Job Alert Agent & Auto-Apply Assistant

An autonomous, security-first AI agent that tracks job alerts from target companies, screens career portals (Greenhouse, Lever, Custom boards), detects ghost jobs, scores ATS match compatibility, generates STAR-scaffolded interview-question sets, drafts tailored cover letters and outreach notes, and assists with one-click browser-based job applications.

---

## 🌟 Key Features

1. **Local AI Engine (LM Studio + Unsloth + Ollama + Cloud Fallback)**
   * Native support for local **LM Studio** (`http://localhost:1234/v1`), **Unsloth** (`http://localhost:8008/v1`), and **Ollama** (`http://localhost:11434`).
   * Fallback support for OpenAI, Google Gemini, and Anthropic Claude.
   * Full JSON mode extraction for resume ATS parsing and qualification scoring.

2. **Isolated Browser Profile (Zero OAuth Security Risk)**
   * Uses a dedicated persistent browser profile directory (`~/.job-alert-agent/chrome_profile/`).
   * No risky OAuth tokens or CDP security exposures; persistent session cookies for Gmail and LinkedIn.

3. **Due Diligence & Ghost Job Detection**
   * Verifies LinkedIn and third-party job alerts against direct company career portals.
   * Detects repeated reposts and flags probable phantom/ghost job postings.

4. **Tailored Application Generator**
   * Generates 250–300 word personalized cover letters tailored to company mission and role requirements.
   * Provides concrete keyword & metric tailoring suggestions for ATS optimization.
   * Drafts concise, high-converting LinkedIn recruiter connection messages (<300 chars).

5. **Assisted Browser Application Assistant**
   * Injects a floating, draggable assistant overlay widget into job portals.
   * Automatically pre-fills form fields (Name, Email, Phone) while giving you one-click copy buttons for tailored cover letters and cold notes.

6. **Application Lifecycle Tracker & RAG Interview Coach**
   * Tracks full application lifecycle: `To Apply` $\rightarrow$ `Applied` $\rightarrow$ `Screening` $\rightarrow$ `Interview` $\rightarrow$ `Offered` $\rightarrow$ `Rejected`.
   * Automatically records dates and audit trail history.
   * Interactive RAG interview preparation chatbot tailored to role and company intelligence.

7. **Application Q&A Memory Bank (Semantic RAG) & Zero-Token Ignore Filtering**
   * **Q&A Memory**: Semantically vectorizes and retrieves past screener/essay answers for 1-click reuse across application portals.
   * **Zero-Token Ignore Filter**: Automatically memorizes dismissed job patterns to auto-filter unwanted roles with 0 LLM tokens.

8. **Dynamic Concurrency Dispatch & In-Prompt Batch Analyzer**
   * Automatically scales from 1 worker (with pacing delay) on local GPU models to 8 parallel workers for cloud providers.
   * In-prompt batching analyzes up to 10 jobs in a single prompt pack, saving 70%+ prompt tokens.

9. **LinkedIn Recommendations & Saved Jobs Auto-Cleaner**
   * Automatically syncs active recommended job alerts from `https://www.linkedin.com/jobs/`.
   * Scans your saved jobs; detects expired or closed postings ("No longer accepting applications") and **automatically unsaves/removes them from LinkedIn** while archiving them in your application tracker.

10. **Non-Blocking Background Tasks Engine & Periodic Scheduler**
    * Dispatches long discovery scans in background worker threads with HTTP 202 immediately.
    * Real-time Server-Sent Events (SSE) progress streaming (`/api/tasks/events`) with cooperative cancellation.
    * In-process automation scheduler daemon (`PeriodicAutomationScheduler`) with interval customization and disk persistence.

11. **Tech Stack & Requirements Alignment Matrix**
    * Zero-token deterministic requirement extraction across languages, databases, cloud, messaging, and architecture.
    * Interactive segmented compatibility progress bar with glowing green `✓` matched resume skill badges and amber `⚡` gap badges with 1-click tailoring integration.

12. **Opportunity Radar & Embedded Hiring Team Dossier**
    * **Opportunity Radar** ranks hiring-intent opportunities from structured, already-ingested sources (LinkedIn job alerts/recommendations, ATS portals, Google Jobs, Gmail recruiter outreach) with deterministic scoring — no LLM calls and no LinkedIn content-post scraping.
    * Sources talent acquisition specialists and engineering-manager contacts for target employers, with 1-click personalized recruiter cold outreach note pre-population.

13. **Multi-Platform Application Status Synchronization**
    * Synchronizes application lifecycle statuses across **Hirist.tech**, **Greenhouse.io**, **Lever**, and **SmartRecruiters**, plus optional **Gmail decision/interview emails** (opt-in; disabled by default — enable under Settings → Opt-in Discovery Features).
    * Auto-prunes closed requisitions via per-portal active probes (`probe_greenhouse_job_active`, `probe_lever_job_active`, `probe_smartrecruiters_job_active`) with a shared generic fallback (`probe_job_url_active`). See [`docs/ATS_PORTALS.md`](docs/ATS_PORTALS.md).

14. **Full-Stack Dual-Sink Observability Layer**
    * Dual-sink structured logging (clean colorized console output + structured JSON log rotation at `logs/app.log`).
    * End-to-end `X-Request-ID` HTTP header correlation and real-time log tailing endpoint (`/api/logs`).
    * Log files and `OperationLog` history auto-rotate/prune (`LOG_MAX_BYTES`, `LOG_BACKUP_COUNT`, `OPERATION_LOG_MAX_ROWS`, `OPERATION_LOG_RETENTION_DAYS`).

15. **Structured Match Analysis & Interview Prep (JSON APIs)**
    * `POST /api/match/analyze` returns a structured resume↔job evaluation (score, strengths, gaps, feedback) from a single prompt; the extension side panel renders its score circle and analysis from the same result so they can never disagree.
    * `POST /api/jobs/{job_id}/interview-questions` derives likely interview questions from the job description with STAR scaffolds and resume-grounded suggested answers.

16. **One-Shot "Sync All"**
    * `POST /api/tasks/sync/all` runs every enabled discovery + application-status channel as one background task with per-stage progress, skip-on-`BrowserProfileBusy`, and an aggregated `sync_all` audit entry.

17. **No-Fabrication Guarantee**
    * If the AI is unreachable, analysis/material endpoints surface an explicit error instead of inventing content: tailor → HTTP 502, bulk-tailor → `failed` list, match analysis → "Not analyzed"/error state, cold outreach → empty (never a canned template).

---

## 🛠️ Architecture & Tech Stack

* **Backend**: FastAPI, SQLAlchemy, Pydantic V2, SQLite (default) or PostgreSQL (`pgvector`, optional), PyPDF, Cryptography (Fernet).

> **Storage & scaling note:** The default SQLite backend stores 384-d embeddings as JSON (`SafeVector`) and computes cosine similarity in Python, which is **O(n) per semantic query**. For large job databases, configure PostgreSQL with the `pgvector` extension (set `DATABASE_URL`) to use indexed vector search.
* **AI & NLP**: Local Unsloth / Ollama / LM Studio router, L2-normalized deterministic vectorizer, LLM Priority Dispatch Queue.
* **Scraping & Search**: BeautifulSoup4, DuckDuckGo HTML search, Greenhouse / Lever / Ashby / SmartRecruiters / Workable / Workday public job APIs, Google for Jobs scraper. Optional user-supplied employer aliases load from `data/company_aliases.json` (empty by default).
* **Automation**: Playwright with dedicated persistent Chromium profile (`~/.job-alert-agent/chrome_profile/`).
* **Observability**: Structured JSON logging, Request Correlation Middleware (`X-Request-ID`), Token & Cost Analytics Tracker.
* **Frontend**: Vanilla JS SPA, Chrome Extension (Manifest V3 Side Panel & Content Scripts), Glassmorphism Dark Mode UI.

---

## 📦 Quick Start & Installation

### 1. Clone & Set Up Python Environment

```bash
git clone https://github.com/nagapavan/job-alert-agent.git
cd job-alert-agent

# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt
playwright install chromium
```

### 2. Configure Environment Variables (Optional)

Create a `.env` file in the root directory (defaults work out of the box with local Unsloth/Ollama):

```env
# Database (defaults to SQLite: job_alert_agent.db)
DATABASE_URL=sqlite:///./job_alert_agent.db

# LLM Providers (auto-detects local LM Studio -> Unsloth -> Ollama)
DEFAULT_LLM_PROVIDER=auto
LM_STUDIO_BASE_URL=http://localhost:1234/v1
LM_STUDIO_MODEL=local-model
UNSLOTH_BASE_URL=http://localhost:8008/v1
UNSLOTH_MODEL=unsloth/Llama-3-8B-Instruct
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=llama3

# Optional per-tier models (dynamic routing). For Ollama these defaults apply:
#   FAST_TIER_MODEL=qwen2.5:3b  STANDARD_TIER_MODEL=llama3:8b  DEEP_TIER_MODEL=llama3.3:70b
# For LM Studio, set the LM_STUDIO_*_MODEL vars to enable per-tier JIT model switching;
# if unset, the single LM_STUDIO_MODEL above is used for all tiers.
# LM_STUDIO_FAST_MODEL=
# LM_STUDIO_STANDARD_MODEL=
# LM_STUDIO_DEEP_MODEL=

# Optional Cloud API Fallbacks
OPENAI_API_KEY=
GEMINI_API_KEY=
ANTHROPIC_API_KEY=

# Optional structured-log & operation-log retention
LOG_LEVEL=INFO
LOG_MAX_BYTES=10485760
LOG_BACKUP_COUNT=5
OPERATION_LOG_MAX_ROWS=1000
OPERATION_LOG_RETENTION_DAYS=30
OPERATION_LOG_PRUNE_INTERVAL_SECONDS=3600
```

### 3. Run the Server

```bash
uvicorn backend.main:app --host 127.0.0.1 --port 8000 --reload
# Binds to loopback only (single-user local tool). To expose it on your LAN, use
# --host 0.0.0.0 and set ALLOWED_ORIGINS / ALLOWED_EXTENSION_IDS for your clients.
```

Open your browser and navigate to: **`http://localhost:8000`**

---

## 🧪 Running Automated Tests

Run the complete automated verification suite:

```bash
./.venv/bin/pytest -v tests/
```

---

## 📧 Optional: Gmail API (BYOK)

Gmail status sync can use the official Gmail API instead of browser scanning. It is **opt-in** and **BYOK** (you supply your own Google OAuth client):

0. **Install dependencies**: `./.venv/bin/pip install -r requirements.txt` (adds `google-auth`, `google-auth-oauthlib`, `google-api-python-client`).
1. **Google Cloud Console → APIs & Services → Library**: create/select a project and enable the **Gmail API**.
2. **OAuth consent screen**: External → add your account as a **Test user** → add scope `.../auth/gmail.readonly`.
3. **Credentials → Create OAuth client ID**: type **Desktop app** (or Web app with redirect `http://localhost:8000/api/gmail/oauth2callback`).
4. In **Settings → Gmail**, paste the **Client ID/Secret**, click **Save Client**, then **Connect Gmail**.
5. Enable the **Gmail sync** feature toggle.

Privacy notes: the API path uses a **narrow query** (job-board senders + interview/offer/rejection subjects) and fetches **metadata + snippet only** — it never enumerates or downloads your whole mailbox. Refresh tokens are encrypted at rest. While the consent screen is in **Testing**, Google expires refresh tokens after **7 days**; set it to **In production** (unverified; *Advanced → Continue*) to avoid reconnecting. If the API isn't connected, the app falls back to the restricted browser-session scanner.

## ⚖️ Responsible Use & Compliance

Some discovery channels are **opt-in** and carry terms-of-service / account-risk considerations:

* **LinkedIn automation is opt-in and disabled by default.** It drives your logged-in session and reads its DOM, which may conflict with LinkedIn's User Agreement and can risk account limitations. Enable it under **Settings → Opt-in Discovery Features** only if you accept that risk.
* **Conservative pacing** is applied (`LINKEDIN_AUTOMATION_PACING_SECONDS`, default 2.5s) with low request volumes to reduce load.
* **Per-portal opt-in & consent:** high-comfort public-API portals (Greenhouse/Lever/Ashby) are on; unofficial/ambiguous endpoints (SmartRecruiters/Workable/Workday/Uber/custom HTML) and session-driven channels (LinkedIn/Gmail) are **off by default** and must be explicitly enabled in **Settings → Discovery Features**.
* **Employer blocklist:** companies/domains on `excluded_companies` (default includes **Amazon**, whose Conditions of Use + Agent Policy restrict automated access) are never scanned.
* **There is no official, self-serve LinkedIn jobs API for candidates** (LinkedIn's official APIs are partner-only and *publish* jobs). With LinkedIn direct scan off, LinkedIn roles remain discoverable via Gmail job-alert emails and Google for Jobs. See [`docs/ATS_PORTALS.md`](docs/ATS_PORTALS.md). This is **not legal advice** — you are responsible for each site's terms.
* **Self-identifying User-Agent + `robots.txt`:** HTML/custom scraping sends a descriptive agent UA and honours `robots.txt` (fail-open; override with `JOB_ALERT_AGENT_RESPECT_ROBOTS=false`). Public ATS JSON APIs keep a standard UA.
* **Legacy data repair:** `python -m backend.scripts.repair_linkedin_saved_jobs [--apply]` fixes malformed LinkedIn saved-job rows ingested before the parser fix (dry-run by default).
* **Gmail inbox reading is opt-in and off by default** — when enabled it prefers the **Gmail API** (narrow query, metadata + snippet only) and falls back to the restricted browser-session scanner if the API isn't connected. See [`docs/GMAIL_API.md`](docs/GMAIL_API.md).
* **Prefer structured sources** where available: Greenhouse, Lever, Ashby, Workday, and Google for Jobs use stable APIs/schema data; LinkedIn is a best-effort, lower-frequency channel.
* **Human-in-the-loop:** the agent pre-fills applications but never submits for you — you confirm submission (`submission_confirmed`).
* **Selector fragility:** DOM-based scraping can break when a site changes; failures are logged rather than forced.
* You are responsible for complying with each site's terms of service and applicable law.

## ⚖️ Legal & Terms of Service Disclaimer

This project is an open-source, AI-assisted job-search agent distributed under the [MIT License](LICENSE).

* **Terms of Service compliance:** automating interactions with, or programmatic browsing/scraping of, third-party platforms (including, but not limited to, LinkedIn) while authenticated may violate those platforms' Terms of Service or User Agreements (e.g. Section 8 of the LinkedIn User Agreement).
* **Account risk:** third-party services deploy bot detection, behavioural telemetry, and rate limiting. Using this software with personal or business accounts carries an inherent risk of **CAPTCHA challenges, security checkpoints, temporary lockouts, or permanent account termination**. The authenticated LinkedIn automation feature is opt-in, **off by default**, and gated behind an explicit risk acknowledgment; it is intended for personal/educational testing use only.
* **No platform affiliation:** this project is not affiliated with, authorised, endorsed by, or connected to LinkedIn Corporation, Microsoft Corporation, or any of their subsidiaries or affiliates. All product and company names are trademarks of their respective holders.
* **Limitation of liability:** the software is provided "as is", without warranty of any kind. In accordance with the MIT License, the authors, contributors, and maintainers accept no responsibility or liability for account restrictions, suspensions or bans, loss of data or reputation, or any violation of local laws, platform rules, or contractual obligations incurred by end users.
* **Sole responsibility:** you are solely responsible for ensuring your use complies with all applicable laws, regulations, and third-party terms of service.

This is **not legal advice**.

## 🔒 Security & Privacy

* **Encryption at Rest**: Candidate resume text, parsed ATS profiles, and private application notes are encrypted using Fernet symmetric encryption with a local key file (`.key`, `chmod 600`).
* **Profile Isolation**: All browser operations execute inside `~/.job-alert-agent/chrome_profile/`, keeping your primary browser accounts isolated and secure.
* **Local-First Execution**: Works 100% offline and private when paired with local Unsloth or Ollama.
* **No Fabrication**: match analysis and application-material generation never invent scores, cover letters, or outreach notes when a model is unavailable — they return an explicit error/empty result.
* **User-Supplied Target List**: the repo ships only a generic `backend/companies_config.example.json`; your real target-company list (`backend/companies_config.json`) and optional `data/company_aliases.json` are local and gitignored.

## 📚 Documentation

Full documentation map: [`docs/README.md`](docs/README.md). Highlights:

* [`DESIGN_DIAGRAMS.md`](DESIGN_DIAGRAMS.md) — system design, C4 models, 9 data-flow diagrams, and text/ASCII architecture + deployment topology (§19).
* [`AGENTIC_ARCHITECTURE.md`](AGENTIC_ARCHITECTURE.md) — agentic harness & autonomy model, AI/ML/LLM architecture (model topology, task tiering, reasoning/verifier loop, RAG/vector memory), and prompt-engineering details.
* [`AGENTS.md`](AGENTS.md) — developer/agent guidelines: package inventory, detailed database schema, performance strategies, security implementation, and a developer onboarding guide.
* [`PROJECT_INDEX.md`](PROJECT_INDEX.md) — file-by-file responsibility index with code examples for every major component.
* [`docs/GMAIL_API.md`](docs/GMAIL_API.md) — opt-in BYOK Gmail API design.
* [`docs/ATS_PORTALS.md`](docs/ATS_PORTALS.md) — ATS portal support, filter contract, requisition probing, and roadmap.
* [`docs/SYNC_ALL_PLAN.md`](docs/SYNC_ALL_PLAN.md) — one-shot "Sync All" orchestrator plan and task tracker.
* [`docs/antigravity/`](docs/antigravity/README.md) — original planning artifacts (provenance).

A compact topic→document map (architecture, code examples, schema, AI/prompt engineering, packages, performance, security, onboarding) is at the top of [`docs/README.md`](docs/README.md).
