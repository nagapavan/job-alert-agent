# Incremental Implementation Roadmap: Job Alert Agent

---

## 🏁 Phase 1: Foundation, Data Layer & Base APIs `[COMPLETED]`
- `[x]` **Task 1.1**: Virtual environment setup & dependency management (`requirements.txt`)
- `[x]` **Task 1.2**: Security & Fernet encryption utilities (`backend/config.py`)
- `[x]` **Task 1.3**: Database models & SafeVector compatibility (`backend/database.py`)
- `[x]` **Task 1.4**: Resume PDF / text extraction (`backend/parser.py`)
- `[x]` **Task 1.5**: Base FastAPI CRUD endpoints for Companies, Jobs, Events, and Resumes (`backend/main.py`)
- `[x]` **Task 1.6**: Automated test suite for data layer & endpoints (`tests/test_api.py` - 6/6 passed)

---

## 🧠 Phase 2: Local AI & NLP Engine (`backend/ai_helper.py`)
- `[x]` **Task 2.1**: **Unified LLM Client Router**
  - Implement routing to local Unsloth (`/v1/chat/completions`) & local Ollama (`/api/chat`) with fallback support for OpenAI, Gemini, or Anthropic.
  - Write unit test for mock LLM responses & provider routing.
- `[x]` **Task 2.2**: **Resume ATS Structured Parser**
  - Prompt LLM to parse extracted resume text into structured JSON (skills, experience, education, summary).
  - Write test verifying structured JSON parsing.
- `[x]` **Task 2.3**: **Job Match Analyzer & Scoring**
  - Prompt LLM to compare Resume vs. Job Description (outputs 0–100 match score, strengths, missing gaps, feedback).
  - Write test verifying score calculation and gap analysis.
- `[x]` **Task 2.4**: **Cover Letter & Resume Tailoring Generators**
  - Prompt LLM to write a personalized 250–300 word cover letter matching the target job description.
  - Prompt LLM to provide concrete keyword and bullet point tailoring recommendations.
  - Write tests verifying output format, input validation, and content personalization.
- `[x]` **Task 2.5**: **LinkedIn Cold Outreach Message Drafter**
  - Prompt LLM to write short connection invitation notes (<300 chars) tailored to the hiring manager.
  - Write test verifying length limit and tone.
- `[x]` **Task 2.6**: **Vector Embeddings Generator**
  - Implement embedding extraction (via Ollama `/api/embeddings` or local deterministic vectorizer).
  - Write test verifying vector dimensionality (384 dimensions).

---

## 🔍 Phase 3: Scraping & Intelligence Engine (`backend/scraper.py`)
- `[x]` **Task 3.1**: **DuckDuckGo HTML Search Utility**
  - Clean HTML scraper for DDG searches (extracts title, link, snippet).
  - Write unit test with mock HTML fixture.
- `[x]` **Task 3.2**: **Greenhouse Careers Board Scraper**
  - Fetch and parse open positions from Greenhouse public board API.
  - Write unit test with mock Greenhouse API response.
- `[x]` **Task 3.3**: **Lever Careers Postings Scraper**
  - Fetch and parse open positions from Lever public postings API.
  - Write unit test with mock Lever API response.
- `[x]` **Task 3.4**: **Company Intelligence Synthesizer**
  - Search news, salary ranges, and hiring process details via DDG and synthesize using LLM into structured company notes.
  - Write unit test.
- `[x]` **Task 3.5**: **Due Diligence & Ghost Job Detection**
  - Repost counter and flagging heuristic for jobs reposted across multiple cycles.
  - Write test for ghost job tagging.

---

## 🌐 Phase 4: Playwright Browser Automation (`backend/playwright_app.py`)
- `[x]` **Task 4.1**: **Persistent Context Launcher**
  - Initialize Playwright pointing to the isolated `.chrome_profile/` directory.
  - Write test verifying browser launch and context lifecycle.
- `[x]` **Task 4.2**: **Gmail Job Alerts & Response Email Scanner**
  - Browser-driven scanner for Gmail (extracts job alerts and flags rejection/interview emails).
- `[x]` **Task 4.3**: **LinkedIn Recruiter / Talent Partner Lookup**
  - Search LinkedIn within the persistent session for talent partners/recruiters.
- `[x]` **Task 4.4**: **Assistant Floating UI Overlay Injection**
  - Injects copyable candidate details, tailored cover letter, and resume bullet points into active application pages.
- `[x]` **Task 4.5**: **Assisted Application Session Runner**
  - Spawns background thread launching the target job portal with the assistant overlay panel.

---

## ⚡ Phase 5: API Integration & Background Tasks
- `[x]` **Task 5.1**: Connect background scraping & intelligence tasks to FastAPI (`POST /api/jobs/scrape`).
- `[x]` **Task 5.2**: Connect Playwright assisted apply trigger endpoint (`POST /api/jobs/{id}/apply`).
- `[x]` **Task 5.3**: Implement interview prep RAG chat endpoint (`POST /api/jobs/{id}/chat`).

---

## 🎨 Phase 6: Frontend Dashboard (`frontend/`)
- `[x]` **Task 6.1**: Sidebar navigation, dark theme layout, and KPI stats cards.
- `[x]` **Task 6.2**: Company management tab (add, view, remove target companies).
- `[x]` **Task 6.3**: Resume profile tab (drag-and-drop PDF upload, parsed ATS details preview).
- `[x]` **Task 6.4**: Job Feed tab (match score badges, due-diligence tags, in-progress vs. archived filters).
- `[x]` **Task 6.5**: "Apply via Playwright" trigger action and copyable drawer.

---

## ✅ Phase 7: Verification & Final Polish
- `[x]` **Task 7.1**: End-to-end workflow verification test.
- `[x]` **Task 7.2**: Complete README setup & user guide.
- `[x]` **Task 7.3**: **Option A: Dedicated Candidate Profile & Preferences Tab + Job Feed Visual Overhaul**
  - Consolidated Resume, Target Roles, Cities, Preset Pills, and Token policies into Tab 4 (`👤 Profile & Preferences`).
  - Replaced oversized Job Feed banner with compact 1-line active criteria bar.
  - Complete visual overhaul of Job Cards with company avatar icons, glowing match badges, structured metadata tags, and uncrowded action clusters.

---

## 🔮 Phase 8: Advanced Intelligent Copilot & Scalability
- `[x]` **Task 8.1**: **Chat Agent Style AI Assistant (Conversational Interface)**
  - Multi-turn conversational assistant with full pipeline visibility, dynamic candidate resume context, and live company intelligence.
  - Autonomous pipeline actions: Status updates (`Shortlisted`, `Applied`, `Interview`), on-demand JIT cover letter / tailoring generation, search with interactive `[⚡ Apply]` and `[⭐ Shortlist]` cards.
  - Live STAR-method mock interview coaching, salary negotiation advice, and strategic job search Q&A.
- `[x]` **Task 8.2**: **Strict Structured Outputs (`json_schema` with Pydantic V2)**
  - **Canonical Pydantic V2 Schemas**: Implemented `JobMatchResult`, `ParsedResumeSchema`, `ConsolidatedApplicationPackageSchema`, and `BatchJobMatchResult` with alias flexibility and validation bounds.
  - **Native Provider-Level Schema Envelopes**: Integrated native wire protocols for OpenAI (`response_format.json_schema`), Gemini (`responseSchema`), Ollama (`format: json_schema`), Anthropic (`tools: input_schema`), and local servers (`generate_structured`).
  - **Type-Strict Validation & Resilience**: 100% type-strict parsing via `generate_structured` with automated JSON repair fallback.
- `[x]` **Task 8.3**: **Google Jobs & Google Alerts Ingestion Engine**
  - **Google for Jobs Direct Fetcher**: Dual-mode ingestion (`fetch_google_jobs` direct Schema.org JSON-LD & DOM parser + `fetch_google_jobs_playwright` persistent session).
  - **Google Alerts Email Digest Parser**: Parses incoming digests from `googlealerts-noreply@google.com` unwrapping Google tracking redirect URLs.
  - **Cross-Source Deduplication & Ingestion Pipeline**: Ingests into `Job` table with ATS portal auto-upgrade, zero-token negative filter, and 384-d vector embeddings (`POST /api/jobs/google-jobs/scrape`, `POST /api/jobs/gmail/sync-alerts`).
- `[x]` **Task 8.4**: **Task-Complexity-Based Dynamic Model Routing**
  - **Tiered Model Routing Engine**: Classifies inference requests into `fast_tier` (3B/7B, Gemini Flash, GPT-4o-mini), `standard_tier` (8B, balanced APIs), and `deep_tier` (70B+, Claude 3.5 Sonnet, GPT-4o, Gemini Pro).
  - **Dynamic Hyperparameter Tuning**: Dynamically applies task-calibrated temperatures (0.0 to 0.6), top_p, and token ceilings per task (`resolve_task_routing`).
  - **Observability**: Added `GET /api/llm/routing-status` endpoint exposing active provider mappings and task complexity profiles.
- `[x]` **Task 8.5**: **Semantic Prompt & Response Caching**
  - **Zero-Token Semantic Cache Engine**: Implemented `resolve_semantic_essay_cache` with 384-d cosine similarity and dynamic placeholder adaptation (`adapt_answer_variables`).
  - **Multi-Tier Resolver & Continuous Auto-Learning**: Implemented `generate_and_cache_essay_answer` and `batch_autofill_essay_answers` which automatically index newly generated responses.
  - **REST API Endpoints**: Exposed `POST /api/qa/resolve-or-generate`, `POST /api/qa/batch-autofill`, and `GET /api/qa/cache-stats`.
  - **Copilot Integration**: Connected real-time semantic caching into `CareerChatAgent.process_message`.
- `[x]` **Task 8.6**: **Single Unified Hybrid Chrome Extension (Side Panel & Full Tab App)**
  - **Manifest V3 Specification**: Configured `chrome.sidePanel` with `openPanelOnActionClick: true`, scoped permissions (`sidePanel`, `storage`, `tabs`, `scripting`, `activeTab`), and multi-resolution PNG icons.
  - **Hybrid Runtime Adapter (`js/runtime-adapter.js`)**: Seamless auto-switching between Connected Mode (FastAPI + local LLM router) and Standalone Zero-Backend Mode (Chrome Gemini Nano on-device + local storage).
  - **In-DOM ATS Scraper & Autofiller (`content/ats-autofill.js`)**: Injects floating quick-action widget and autofills standard form fields and screener essay textareas on Greenhouse, Lever, Ashby, Workday, and LinkedIn.
  - **Glassmorphic Chrome Side Panel (`sidepanel/`)**: 4-tab sidebar covering Live Job Analyzer, AI Career Copilot, Q&A Memory Bank, and Candidate Settings.
  - **Store Blueprint & Tests**: Created `CHROMEWEBSTORE.md`, `extension/README.md`, and integration tests in `tests/test_extension_integration.py`.

---

## 🚀 Phase 9: Resume-Driven Target Discovery & STAR Portfolio Coach `[QUEUED / FUTURE]`
- `[ ]` **Task 9.1**: **Resume-Driven Target Company Discovery & Suitability Engine**
  - Analyze active resume domain keywords, engineering archetype, tech stack, and scale maturity (e.g., distributed systems, fintech, developer infra, B2B AI).
  - Dynamically discover and recommend matching companies/startups via live web search and ATS indexing instead of relying on pre-seeded catalogs.
  - One-click "Add to Monitored Companies" with automatic portal URL detection.
- `[ ]` **Task 9.2**: **Interactive Project STAR Deep-Dive & Resume Content Enhancer**
  - Interactive conversational quizzer where the AI Copilot asks targeted probing questions about candidate projects (*"What was the system scale / QPS?", "What trade-offs did you make?", "What business impact or latency improvement did you deliver?"*).
  - Synthesize user answers into high-impact STAR project summaries and quantified bullet points.
  - Re-evaluate and suggest live upgrades to the candidate's active resume profile and exportable PDF/Markdown.
