# One-Shot "Sync All" — Plan & Task List

Backend-orchestrated, feature-flag-aware one-shot sync. Single background task via `task_engine`
(unified progress/SSE/cancel). Stages run **serially** (single persistent browser profile).
Scope: discovery (ATS + Google Jobs + optional LinkedIn) **and** application status sync.
JobSpy stays an automatic fallback (with explicit log entries). UI: one button on Job Feed only.

## Decisions
- Orchestrator reads flags server-side (`get_feature_flags`); frontend only displays the enabled count.
- "0 enabled" → button disabled, label `Sync All (0 enabled)`.
- Partial failures tolerated; browser-profile-busy stage is skipped with a reason and the run continues.
- No duplicate button in Background Tasks / Discovery Engines panel.

## Tasks
| # | Task | Status |
|---|------|--------|
| T1 | Backend: extract `_execute_ats_scrape()` helper; add optional `cancel_token`/`progress_cb` | Done |
| T2 | Backend: extract `_execute_external_sync()` helper; add optional `cancel_token`/`progress_cb` | Done |
| T3 | Backend: JobSpy fallback log entries (info/debug) | Done |
| T4 | Backend: add `_run_sync_all_task_runner()` orchestrator | Done |
| T5 | Backend: add `POST /api/tasks/sync/all` endpoint | Done |
| T6 | Frontend: Sync All button on Job Feed radar bar + enabled-count state | Done |
| T7 | Frontend: `triggerSyncAll()` dispatcher | Done |
| T8 | Tests: `tests/test_sync_all.py` (skip disabled, run enabled, continue on failure) | Done |
| T9 | Verify: full pytest suite + JS syntax check | Done |
| T10 | Flag `ats_hirist` (default **on**): gate Hirist in external sync, Sync All stage + count, UI, settings | Done |

## Preference filtering at ingestion (proper filter flows)
Problem: preference filters are applied inconsistently — ATS scrape filters, but Google Jobs /
LinkedIn alerts / Gmail opportunities ingest out-of-scope roles (US, Pakistan, etc.). Deletion is
"after damage"; fix at ingestion with one reusable component.

| # | Task | Status |
|---|------|--------|
| P1 | Add reusable `backend/preference_filter.py` (`PreferenceCriteria` + `PreferenceFilter`) | Pending |
| P2 | Centralize location/work-mode matchers + region-aware foreign restriction | Pending |
| P3 | Adopt in Google Jobs runner (filter before insert) | Pending |
| P4 | Adopt in LinkedIn alerts/recommendations (saved jobs bypass) | Pending |
| P5 | Adopt in Gmail opportunity ingestion (per policy) | Pending |
| P6 | Filter telemetry (skipped counts in OperationLog) + tests | Pending |

## Log rotation & retention (sustainability)
| # | Task | Status |
|---|------|--------|
| L1 | App logs: env-configurable `LOG_MAX_BYTES` / `LOG_BACKUP_COUNT` in `setup_observability` | Done |
| L2 | App logs: dedicated rotating `llm_audit.log` sink (no longer interleaved in app.log) | Done |
| L3 | DB: `prune_operation_logs()` (retention days + row cap) + `created_at` index | Done |
| L4 | DB: rotate on startup (`@app.on_event("startup")`) and hourly in scheduler daemon | Done |
| L5 | Tests: `tests/test_log_rotation.py` | Done |
