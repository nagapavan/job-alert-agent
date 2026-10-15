# Bulk Match Scoring: Deterministic-First Architecture

## Overview

All job scoring in the agent uses a **two-tier approach** to maximize token efficiency and response speed:

1. **Fast Path** (deterministic, <100ms, no LLM cost)
2. **Bounded Fallback** (LLM, capped at `LLM_INTERACTIVE_TIMEOUT`, only when needed)

This reduces LLM usage by ~93% during bulk discovery and rescoring operations.

---

## Architecture

### Tier 1: Deterministic Classification

**Entry point**: `_score_job_with_engine()` (main.py)

```
Job + Resume → deterministic extraction (regex, parsing) → classifier → score
  ├─ build_deterministic_assessment(jd, resume)
  │   ├─ JD signals: years, skills, degree, seniority, specialty
  │   ├─ Resume signals: experience, tenure, skills, education
  │   └─ Skill/year matching: tech-specific tenure vs. requirement
  │
  └─ classifier (sklearn-like heuristic or ML model)
      └─ score_method="classifier_v1"
```

**Output** (if successful):

- `match_score` (0–100)
- `band` (Below/Beginner/Intermediate/Advanced/Expert)
- `recommendation` (Pass/Investigate/Reach/Strong)
- `score_method="classifier_v1"`
- Strengths, gaps, feedback (no LLM)
- `match_scored=True`

**Observability**: Deterministic counters tracked in rescore tasks.

---

### Tier 2: Bounded LLM Fallback

**Trigger**: Deterministic path encounters an error (e.g., resume parse failure), **not** "no requirements found".

**Guard: Artifact Detection** (`_looks_like_job_description()`)

Before attempting LLM fallback, the system checks whether the description text looks like an actual job posting. The guard matches JD structural cues (e.g. "experience", "skills", "looking for", "requirements", "qualifications", "years of", "degree") via a compiled regex (`_JD_CUE_RE` in `backend/main.py`) and returns `False` for LinkedIn snippets, Gmail recruiter lines, and truncated link previews.

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
- Fallback on error only; not triggered by "no requirements" from artifacts

---

## Implementation Details

### Files Involved

| File                          | Function                           | Role                                             |
| :---------------------------- | :--------------------------------- | :----------------------------------------------- |
| `backend/main.py`             | `_score_job_with_engine()`         | Two-tier routing with artifact guard             |
| `backend/main.py`             | `_deterministic_match()`           | Deterministic requirement + classifier fast path |
| `backend/main.py`             | `_looks_like_job_description()`    | Non-JD artifact guard                            |
| `backend/match_classifier.py` | `build_deterministic_assessment()` | Fast JD+resume requirement extraction            |
| `backend/resume_signals.py`   | `estimate_technology_years()`      | Tech tenure inference                            |
| `backend/jd_signals.py`       | Requirement parsing                | Year/skill/degree extraction                     |
| `backend/main.py`             | `_run_match_rescore_task_runner()` | Bulk rescore with method counters                |

For the exact signatures and control flow, see `backend/main.py` — the code is the source of truth.

---

## Observability

### Database

**New `Job` field**: `score_method` (String, nullable)

- `classifier_v1` → deterministic path succeeded
- `requirement_coverage_v1` → LLM fallback succeeded
- `NULL` → unscored (artifact or error)

### Logs

**OperationLog** (`POST /api/match/rescore` response):

```json
{
  "operation_type": "match_rescore",
  "status": "Completed",
  "summary": "Rescored 390 jobs: 364 deterministic, 0 LLM fallback, 26 unscored",
  "details_json": {
    "deterministic": 364,
    "llm_fallback": 0,
    "unscored": 26,
    "total": 390
  },
  "jobs_count": 390,
  "prompt_tokens": 0,
  "completion_tokens": 0
}
```

---

## Configuration

### Environment Variables

| Var                          | Default        | Purpose                                                               |
| :--------------------------- | :------------- | :-------------------------------------------------------------------- |
| `DETERMINISTIC_BULK_SCORING` | `true`         | Enable two-tier approach; `false` disables and falls back to full LLM |
| `LLM_INTERACTIVE_TIMEOUT`    | `30` (seconds) | Max time for LLM fallback call                                        |

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
| Unscored (artifacts)                   | 9     | 2.3%  | <10ms  | 0 tokens       |

**Total LLM tokens for bulk rescore**: ~850–2550 tokens (vs. ~50k+ without two-tier approach).

---

## Migration & Rollback

### Enabling (already done)

1. Column added to `Job` model: `score_method = Column(String(50), nullable=True)`
2. Auto-migration in `init_db()`: `ALTER TABLE jobs ADD COLUMN score_method VARCHAR(50)`
3. Flag `DETERMINISTIC_BULK_SCORING=true` (default)
4. Discovery calls updated to pass `job_title`

### Disabling (if needed)

Set `DETERMINISTIC_BULK_SCORING=false`:

- All scoring calls fall back to full LLM for every job
- `score_method` remains in DB but unused
- No code changes needed; env var only

---

## Testing

### Unit Tests

- `test_score_job_with_engine_shapes` (3 cases): deterministic, LLM fallback, LLM error
- `test_score_job_with_engine_skips_non_jd_snippet`: Artifact guard prevents LLM
- `test_looks_like_job_description`: Cue regex verification
- `test_rescore_updates_existing_jobs`: Mixed method counters in OperationLog

### Integration Tests

- `test_rescore_operations_endpoint`: Full rescore flow with counter assertions

---

## Future Enhancements

1. **Learned artifact detector** (ML): Replace regex with classifier trained on artifact patterns.
2. **Classifier retraining**: Periodically retrain heuristic/ML model on user-confirmed matches.
3. **Score confidence interval**: Attach uncertainty bounds from classifier.
4. **Cache Tier 2 results**: Reuse LLM fallback results for duplicate JDs (e.g., LinkedIn repost).
