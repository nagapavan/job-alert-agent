# Bulk Match Scoring: Deterministic-First Architecture

## Overview

All job scoring in the agent uses a **two-tier approach** to maximize token efficiency and response speed:

1. **Fast Path** (deterministic, <100ms, no LLM cost)
2. **Bounded Fallback** (LLM, capped at `LLM_INTERACTIVE_TIMEOUT`, only when needed)

Rescoring is deliberately **not** LinkedIn-blocking: it never opens a browser and scores only from
the cached `jobs.description` (see `AGENTS.md` §3).

This reduces LLM usage by ~93% during bulk discovery and rescoring operations.

---

## Architecture

### Tier 1: Deterministic Classification

**Entry point**: `_score_job_with_engine()` (`backend/main.py`), which delegates to
`_deterministic_match()` for the fast path.

```
Job + Resume → deterministic extraction (regex, parsing) → classifier → score
  ├─ _deterministic_match(jd, resume, job_title)
  │   └─ build_deterministic_assessment(...)  [backend/match_classifier.py]
  │       ├─ JD signals (backend/jd_signals.py): years, degree, certifications, seniority
  │       ├─ Resume signals (backend/resume_signals.py): tenure per technology, education
  │       └─ Tenure matching: `estimate_technology_years` (overlapping periods merged so
  │          concurrent projects are not double-counted)
  │
  └─ get_classifier().predict(features)   [heuristic or trained ML model]
      └─ band_and_recommendation(score, ScoringConfig)  [backend/match_scoring.py]
```

**Output** (if successful):

- `match_score` (0–100)
- `band` — `Strong` (≥85) / `Good` (≥70) / `Moderate` (≥55) / `Weak` (≥40) / `Poor`, from the
  shared `ScoringConfig` (not hardcoded), or `Unknown` when unscored
- `recommendation` — `apply` / `apply_with_caution` / `skip` (or `unknown`), from the same config
- `score_method="classifier_v1"`
- Strengths, gaps, partials, feedback (no LLM)
- `match_scored=True`

**Observability**: `deterministic` / `llm_fallback` counters tracked in the rescore task.

---

### Tier 2: Bounded LLM Fallback

**Trigger** — the LLM runs when *either*:

1. the deterministic path **raised an error** (fell through to `det_errored`), **or**
2. the deterministic path produced **no usable requirements** *and* the text looks like a real JD.

A clean "no requirements" result on a non-JD discovery artifact does **not** trigger it.

**Guard: Artifact Detection** (`_looks_like_job_description()`)

Before spending an LLM call, the system checks whether the description text looks like an actual job posting. The guard matches JD structural cues (e.g. "experience", "responsibilities", "looking for", "requirements", "qualifications", "years of", "degree") via a compiled regex (`_JD_CUE_RE` in `backend/main.py`) and returns `False` for LinkedIn snippets, Gmail recruiter lines, and truncated link previews.

**Examples of artifacts that skip LLM**:

- `"LinkedIn Alert opening for Software Engineer in San Francisco"`
- `"Gmail: Hi, we're recruiting for a role at Acme. Apply here: [link]"`
- `"(Truncated description) ... salary $80k-120k"`

These are marked `unscored` without spending an LLM call.

**Examples that trigger LLM fallback**:

- Real job descriptions with poor signal structure (no requirements section, but present)
- Niche roles where the deterministic parser finds no structured clues

**LLM Call Behavior**:

- Timeout: `LLM_INTERACTIVE_TIMEOUT` (default 30s)
- Method: `requirement_coverage_v1` (full analysis via LLM)
- Runs on deterministic error, or on a signal-less real JD; never on a non-JD artifact

---

## Implementation Details

### Files Involved

| File                          | Function                           | Role                                             |
| :---------------------------- | :--------------------------------- | :----------------------------------------------- |
| `backend/main.py`             | `_score_job_with_engine()`         | Two-tier routing with artifact guard             |
| `backend/main.py`             | `_deterministic_match()`           | Deterministic requirement + classifier fast path |
| `backend/main.py`             | `_looks_like_job_description()`    | Non-JD artifact guard (`_JD_CUE_RE`)             |
| `backend/match_classifier.py` | `build_deterministic_assessment()` | Fast JD+resume requirement extraction            |
| `backend/match_scoring.py`    | `band_and_recommendation()`        | Shared band/recommendation from `ScoringConfig`  |
| `backend/resume_signals.py`   | `estimate_technology_years()`      | Tech tenure inference (overlaps merged)          |
| `backend/jd_signals.py`       | `parse_experience` / `parse_education` / `parse_certifications` / `seniority_level` | JD signal extraction |
| `backend/requirements_extractor.py` | `extract_residual_requirements()` | Credits JD units the taxonomy missed (no LLM) |
| `backend/main.py`             | `_run_match_rescore_task_runner()` | Bulk rescore with method counters                |

For the exact signatures and control flow, see `backend/main.py` — the code is the source of truth.

---

## Observability

### Database

`Job` fields written by the scorer:

| Field | Values |
| :--- | :--- |
| `score_method` | `classifier_v1` → deterministic path succeeded; `requirement_coverage_v1` → LLM fallback succeeded; `NULL` → unscored (artifact or error) |
| `match_scored_at` | Timestamp of the (re)score; `NULL` for unscored rows |
| `match_band` | Band from the shared `ScoringConfig` (`Strong`/`Good`/`Moderate`/`Weak`/`Poor`) |
| `apply_recommendation` | `apply` / `apply_with_caution` / `skip` |
| `match_strengths_json` / `match_gaps_json` / `match_partials_json` | Structured lists persisted at scoring time |

Only rows with a real score get these persisted; unscored rows keep `match_scored=False` and the UI
shows "not AI-scored".

### Logs

Rescore runs in the background and records one `OperationLog`
(`operation_type="match_rescore"`) when the task finishes. `status` is `Completed` when nothing was
skipped, otherwise `Partial`. The real `details_json` shape is:

```json
{
  "operation_type": "match_rescore",
  "status": "Completed",
  "summary": "Re-scored 364 job(s): 364 deterministic, 0 LLM fallback; 0 skipped.",
  "details_json": {
    "rescored": 364,
    "deterministic": 364,
    "llm_fallback": 0,
    "failed": 0,
    "limit": 200,
    "started_at": "2026-10-18T09:12:44.183920+00:00"
  },
  "jobs_count": 364,
  "prompt_tokens": 0,
  "completion_tokens": 0
}
```

Note `details_json` counts **`rescored`** (scored rows) and **`failed`** (skipped/unscored), not
`unscored`/`total`.

---

## Configuration

### Environment Variables

| Var                          | Default        | Purpose                                                               |
| :--------------------------- | :------------- | :-------------------------------------------------------------------- |
| `DETERMINISTIC_BULK_SCORING` | `true`         | Enable the two-tier approach; `false` skips the fast path and always uses the LLM extractor (still artifact-guarded) |
| `LLM_INTERACTIVE_TIMEOUT`    | `30` (seconds) | Max time for the LLM fallback call                                    |

### Rescore endpoint

`POST /api/match/rescore` dispatches a **background task** (`202` + `task_id`) — it is not a
synchronous response, so read the counters from the resulting `OperationLog`, not the HTTP body.
It scores at most `limit` jobs (default `200`), newest first, skipping `Rejected` / `Not Interested`
rows, and paces itself with a 0.3 s sleep between jobs.

### Discovery Calls

All discovery sites pass the `job_title` param to `_score_job_with_engine()` to help deterministic parsing:

- LinkedIn job alert sync
- LinkedIn recommendations
- LinkedIn saved jobs
- ATS portal scraping
- Google Jobs API
- Gmail recruiter outreach

---

## Performance Expectations

**Baseline measurement** (390 scoreable jobs):

| Method                                 | Count | %     | Speed  | LLM Cost       |
| :------------------------------------- | :---- | :---- | :----- | :------------- |
| Deterministic (classifier_v1)          | 364   | 93.3% | <100ms | 0 tokens       |
| LLM Fallback (requirement_coverage_v1) | 17    | 4.4%  | 5–15s  | ~50–150 tokens |
| Unscored (artifacts / errors)          | 9     | 2.3%  | <10ms  | 0 tokens       |

**Total LLM tokens for bulk rescore**: ~850–2550 tokens (vs. ~50k+ without the two-tier approach).

> These are measured from practical runs (custom tests + bulk syncs), not an externally audited
> benchmark. Treat them as indicative.

---

## Migration & Rollback

### Enabling (already done)

1. `Job.score_method` (nullable) added, alongside `match_scored_at` / `match_band` /
   `apply_recommendation` and the structured `match_*_json` lists
2. Auto-migration in `init_db()` issues the `ALTER TABLE jobs ADD COLUMN ...` steps for existing DBs
3. Flag `DETERMINISTIC_BULK_SCORING=true` (default)
4. Discovery calls updated to pass `job_title`

### Disabling (if needed)

Set `DETERMINISTIC_BULK_SCORING=false`:

- The fast path is skipped, so every scoreable job goes through the LLM extractor
- The artifact guard still applies, so discovery snippets are still marked unscored rather than scored
- `score_method` still records `requirement_coverage_v1` (it is not "unused")
- No code changes needed; env var only

---

## Testing

### Unit tests (`tests/test_match_rescore.py`)

- `test_score_job_with_engine_deterministic` — fast path succeeds
- `test_score_job_with_engine_llm_fallback` — falls back when the deterministic path yields nothing
- `test_score_job_with_engine_llm_failure_unscored` — LLM failure degrades to unscored, never fabricates
- `test_score_job_with_engine_skips_non_jd_snippet` — artifact guard prevents the LLM call
- `test_score_job_with_engine_falls_back_on_deterministic_error` — deterministic error → LLM
- `test_score_job_with_engine_flag_disables_deterministic` — `DETERMINISTIC_BULK_SCORING=false` path
- `test_rescore_updates_existing_jobs` — per-method counters land in the `OperationLog`
- `test_rescore_skips_when_no_resume` — no active resume is handled
- `test_rescore_endpoint_dispatches` — the route dispatches a background task

Run: `./.venv/bin/pytest -q tests/test_match_rescore.py`

---

## Future Enhancements

1. **Learned artifact detector** (ML): Replace the `_JD_CUE_RE` regex with a classifier trained on artifact patterns.
2. **Classifier retraining**: Shipped — `scripts/train_match_classifier.py` trains from implicit user
   outcomes (excluded system-generated rows), gated at ≥50 trainable / ≥15 per class. Still open:
   scheduling it periodically and versioning the model artifact.
3. **Score confidence interval**: Attach uncertainty bounds from the classifier.
4. **Cache Tier 2 results**: Reuse LLM fallback results for duplicate JDs (e.g., LinkedIn repost).
