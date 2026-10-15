# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **LLM transient-failure resilience layer** (`backend/llm_resilience.py`)
  - `with_retry()` exponential backoff (+ jitter) around provider calls in `_execute_generate_text`
  - Retries only 429 / 5xx / connection / timeout; 400 & other non-transient errors propagate
  - Per-task `max_retries` override in `TASK_PROFILES`, plus global `LLM_MAX_RETRIES`,
    `LLM_BACKOFF_BASE`, `LLM_BACKOFF_MAX`, `LLM_RETRY_JITTER`
  - `generate_text` / `generate_structured` now accept `max_retries`

- **Centralized prompts module** (`backend/prompts.py`)
  - `JOB_MATCH_SCORING_SYSTEM`, `JUDGE_SYSTEM`, `interview_questions_system()`, `career_copilot_system()`
  - Match-scoring, interview-question, copilot, and Judge prompts migrated off inline strings

- **Reusable quality Judge gate** (`ai_helper.JudgeVerdict` + `judge_output`)
  - Scores open-ended output 0-5 (correctness/completeness/groundedness/tone), bounded re-generation
  - Wired into Career Copilot replies (`chat_agent.process_message`) behind
    `COPILOT_JUDGE_ENABLED` / `COPILOT_JUDGE_MAX_REGEN`

- **LLM audit observability** (`observability.log_llm_event`)
  - `trace_id` correlation linking all LLM calls in one operation
  - Opt-in prompt/response content capture (`LLM_AUDIT_CAPTURE_CONTENT`, default OFF for privacy;
    capped by `LLM_AUDIT_MAX_CONTENT_CHARS`)

- **LinkedIn sync batch visibility & review queue**
  - New `Job.sync_batch_id` column (auto-migrated) tags every job newly ingested by a LinkedIn sync
  - `GET /api/linkedin/last-sync` returns the latest sync batch + pending-review count
  - SPA "N new from sync" review banner with a View button, a per-tile "New from sync" badge, and a
    source filter (LinkedIn / ATS-Direct / Email); `GET /api/jobs` gains `source_type`,
    `sync_batch`, and `new_from_sync` filters
  - The review marker clears automatically once the user changes a job's status (single or bulk)
  - Works for both the manual `POST /api/linkedin/sync` and the background/Sync-All runner

- **Resume Gap Insights** (requirement-level strengths & gaps surfaced in the UI)
  - New `Job.match_strengths_json` / `Job.match_gaps_json` columns (auto-migrated) persist the
    structured lists at scoring time — no fragile re-parsing of the flattened narrative
  - `ScoringResult` now carries `strengths` / `gaps`; written by every ingestion path, the batch
    rescore runner, the tailor package, and the detailed-analysis cache
  - `JobOut` (all `GET /api/jobs*` responses) exposes `match_strengths` / `match_gaps` lists
  - SPA job modal "Resume Gap Insights" panel: gap bullets + already-matched bullets; legacy rows
    (pre-feature) fall back to the narrative text. Toggled by the new `resume_gap_insights`
    feature flag (Settings → Discovery Features → UI insights; default ON)
  - Extension side panel already renders structured strengths/gaps from `/api/match/analyze`

### Changed
- **SQLite is now the default database** (`config.py`): zero-config out of the box. Set
  `DATABASE_URL` to a `postgresql://` URL to opt into PostgreSQL (with pgvector).
- **Ollama simplified to single model**: removed `FAST_TIER_MODEL` / `STANDARD_TIER_MODEL` /
  `DEEP_TIER_MODEL`; `get_model_for_tier("ollama", ...)` returns `OLLAMA_MODEL` for all tiers.
  Dropped the `tier_models` block from `GET /api/llm/routing-status` (kept `resolved_provider_tiers`).
- **Gmail scan now covers all mail, not just the active inbox** (both paths stay privacy-scoped)
  - Time window widened `newer_than:60d` → `newer_than:90d` (full application lifecycle)
  - Added major ATS senders: `workday.com`, `icims.com`, `taleo.net`, `workable.com`
  - Browser fallback navigates directly to Gmail's global `#search/` view (mailbox-wide, covering
    Updates/Promotions/Social + user labels; Gmail still excludes Spam/Trash) with the search box
    kept as fallback, and reads 2 result pages (≈80 emails) instead of 1
  - The Gmail API path already searched all labels (no `labelIds`); the all-mail scope is now
    documented in `gmail_client.py`
- **Source filter in the SPA now covers every real source type**: LinkedIn / Google / Email /
  Hirist / Direct (previously only LinkedIn / Email / Direct).

### Technical Details
- 524 tests passing, 13 skipped (live-marker excluded), 0 regressions
- New files: `backend/llm_resilience.py`, `backend/prompts.py`, `tests/test_llm_resilience.py`,
  `tests/test_judge_and_observability.py`, `tests/test_linkedin_batch_visibility.py`,
  `tests/test_resume_gap_insights.py`

---

## [1.0.0] - 2026-10-02

### Added
- **Hybrid fast classifier + on-demand LLM analysis**
  - Rule-based heuristic classifier: instant scoring (<100ms, 0 tokens)
  - Optional trained ML classifier (logistic regression) for improved accuracy
  - On-demand `/api/match/detailed-analysis` endpoint for LLM narrative (cached)
  - ~90% token savings vs. monolithic LLM approach
  - See [PORTFOLIO_JOURNEY.md](docs/PORTFOLIO_JOURNEY.md) for architecture + lessons learned

- **Match scoring improvements**
  - Zero-token semantic dismissal filter (skip obviously-rejected jobs)
  - Requirements-based assessment with structured evidence snippets
  - `match_method` field in API response (shows "heuristic" vs "classifier" vs "trained_classifier")

- **Documentation**
  - [PORTFOLIO_JOURNEY.md](docs/PORTFOLIO_JOURNEY.md): AI/ML design narrative (phases 1-5, decisions, lessons)
  - [HYBRID_MATCHER_GUIDE.md](docs/HYBRID_MATCHER_GUIDE.md): Technical deep-dive

### Changed
- `/api/match/analyze` now returns instant score from classifier (was full LLM extraction)
- Extension shows "💡 View Detailed Analysis" button for on-demand narrative

### Technical Details
- 434 tests passing (all previous + 4 new)
- 0 regressions
- New files: `backend/match_classifier.py`, `scripts/train_match_classifier.py`
- Performance: <100ms fast path, 5-15s on-demand detailed (vs 5-15s per job before)

---

## [0.9.0] - 2026-09-30

Initial release candidate (before hybrid matcher).
