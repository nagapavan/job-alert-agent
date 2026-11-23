# Hybrid Fast Classifier + On-Demand LLM Analysis Guide

## Overview

The job-alert-agent now uses a **two-tier matching system**:

1. **Fast Classifier Tier** (deterministic, <100ms): Returns instant match score + recommendation
2. **On-Demand LLM Tier** (full analysis, 5-15s): Returns detailed strengths/gaps/narrative when requested

This architecture delivers **50-150x faster initial response** while preserving rich narrative analysis for users who want it.

---

## How It Works

### 1. User Opens a Job in Extension

```
User navigates to LinkedIn/ATS job page
↓
Extension triggers: GET_PAGE_JOB_INFO
↓
Extension calls: POST /api/match/analyze (with job title, company, description)
↓
Backend extracts JD signals deterministically (regex, no ML)
↓
Backend loads classifier (heuristic or trained ML model)
↓
Classifier predicts score in <100ms
↓
Response includes: score, band, recommendation, analysis_source="classifier", job_id (if available)
↓
Extension displays score circle + "💡 View Detailed Analysis" button
```

### 2. User Clicks "View Detailed Analysis" (Optional)

```
User clicks button
↓
Extension calls: POST /api/match/{job_id}/detailed-analysis
↓
Backend checks: is result cached in Job.match_analysis?
  ├─ YES: Return cached result immediately
  └─ NO: Run full LLM extraction
        ↓
        LLM generates detailed assessment (5-15s)
        ↓
        Save to Job.match_analysis
        ↓
        Return result with cached=false
↓
Extension displays detailed analysis in side panel
```

---

## API Reference

### Fast Match Analysis

```http
POST /api/match/analyze
Content-Type: application/json
X-API-Key: <your-api-key>

{
  "job_title": "Senior Python Engineer",
  "company": "TechCorp",
  "job_description": "We seek a Senior Python Engineer with 5+ years FastAPI experience...",
  "location": "San Francisco, CA",
  "resume_text": null  // or provide raw resume text; backend uses active resume if null
}
```

**Response** (134ms):
```json
{
  "match_score": 78,
  "band": "Good",
  "apply_recommendation": "apply",
  "analysis_source": "classifier",
  "match_method": "classifier_v1",
  "job_id": null,
  "strengths": [
    "5+ years Python backend experience",
    "Familiar with FastAPI and SQLAlchemy"
  ],
  "gaps": [],
  "feedback": "Score: 78% (Good match). Based on requirement coverage and job signals.",
  "must_coverage": 0.9,
  "nice_coverage": 0.6,
  "over_qualified": false,
  "eligible": true
}
```

**Fields:**
- `match_score`: 0-100 percentile
- `band`: "Strong", "Good", "Moderate", "Weak", "Poor" (or "Unknown" when no score could be produced)
- `apply_recommendation`: "apply", "apply_with_caution", "skip" (or "unknown" when unscored)
- `analysis_source`: "classifier" (fast), "llm" (full extraction), or "error"
- `match_method`: "classifier_v1" or "requirement_coverage_v1"
- `job_id`: Database ID if this is a saved job; null for ad-hoc analyses
- `strengths`: Evidence snippets from resume
- `gaps`: Missing requirements (if any)

### Detailed Analysis (On-Demand)

```http
POST /api/match/{job_id}/detailed-analysis
Content-Type: application/json
X-API-Key: <your-api-key>
```

**Response (cached, <1ms):**
```json
{
  "job_id": 42,
  "cached": true,
  "analysis": "Strengths: 6+ years Python, strong async/await patterns, PostgreSQL expertise. Gaps: No FastAPI REST API design experience mentioned, limited real-time systems background. Recommendation: Apply—strong fundamentals; gaps are learnable.",
  "strengths": ["6+ years Python experience", "PostgreSQL expertise"],
  "gaps": ["Limited FastAPI REST API design"],
  "feedback": "Strong fit with minor gaps",
  "method": "llm_detailed"
}
```

**Response (generated, 8-12s):**
```json
{
  "job_id": 42,
  "cached": false,
  "analysis": "...",
  "method": "llm_detailed"
}
```

---

## Architecture

### Data Flow

```
┌─────────────────────────────────────────────────────────────┐
│ POST /api/match/analyze (extension side panel)              │
└─────────────────────────────────────────────────────────────┘
                             ↓
     ┌───────────────────────────────────────────────┐
     │ Backend: analyze_job_match_endpoint()          │
     └───────────────────────────────────────────────┘
                             ↓
         ┌─────────────────────────────────────────┐
         │ Load active resume (encrypted)           │
         │ Parse resume JSON (skills, experience)   │
         └─────────────────────────────────────────┘
                             ↓
         ┌─────────────────────────────────────────┐
         │ Extract JD signals (deterministic)       │
         │ - regex for years of experience          │
         │ - skill keyword matching                 │
         │ - education requirement parsing          │
         │ No LLM, no ML                            │
         └─────────────────────────────────────────┘
                             ↓
         ┌─────────────────────────────────────────┐
         │ Extract match features                   │
         │ - must-have skill coverage (0-1)        │
         │ - nice-to-have coverage (0-1)           │
         │ - seniority fit (score delta)           │
         │ - education match (bool)                │
         │ - overqualification flag                │
         │ Deterministic computation only           │
         └─────────────────────────────────────────┘
                             ↓
   ┌─────────────────────────────────────────────────────┐
   │ DeterministicClassifier (one scorer, no model file) │
   ├─────────────────────────────────────────────────────┤
   │ Weighted: 60 must-coverage / 20 nice-coverage /     │
   │           15 seniority / 5 education                │
   │ Renormalised over the components the JD defines     │
   └─────────────────────────────────────────────────────┘
                             ↓
         ┌─────────────────────────────────────────┐
         │ classifier.predict(features)             │
         │ Returns: 0-100 score (deterministic)    │
         │ <100ms latency                          │
         └─────────────────────────────────────────┘
                             ↓
         ┌─────────────────────────────────────────┐
         │ Determine band + recommendation from    │
         │ score (shared ScoringConfig, same       │
         │ logic as the LLM path)                  │
         │ - ≥85:  "Strong"  → "apply"            │
         │ - ≥70:  "Good"    → "apply"            │
         │ - ≥55:  "Moderate"→ "apply_with_caution"│
         │ - ≥40:  "Weak"    → "skip"             │
         │ - <40:  "Poor"    → "skip"             │
         │ - None: "Unknown" → "unknown"          │
         └─────────────────────────────────────────┘
                             ↓
         ┌─────────────────────────────────────────┐
         │ Return MatchAnalyzeResponse              │
         │ - match_score: 0-100                    │
         │ - analysis_source: "classifier"         │
         │ - job_id: null (ad-hoc) or int (saved) │
         │ - Evidence snippets (first 2)           │
         │ Total latency: <100ms                   │
         └─────────────────────────────────────────┘
                             ↓
     ┌─────────────────────────────────────────────┐
     │ Extension receives response                 │
     │ - Displays score circle (green/blue/orange) │
     │ - Shows recommendation tier label           │
     │ - Displays evidence snippets               │
     │ - Shows "💡 View Detailed Analysis" button │
     └─────────────────────────────────────────────┘
```

### On-Demand Detailed Analysis

```
┌─────────────────────────────────────────────────────────────┐
│ POST /api/match/{job_id}/detailed-analysis (user-triggered)│
└─────────────────────────────────────────────────────────────┘
                             ↓
     ┌───────────────────────────────────────────┐
     │ Backend: generate_detailed_match_analysis()│
     └───────────────────────────────────────────┘
                             ↓
         ┌─────────────────────────────────────────┐
         │ Query: SELECT * FROM jobs WHERE id=?    │
         │ Check: if job.match_analysis is set:   │
         │   ├─ YES: Return cached result (<1ms)  │
         │   └─ NO: Continue to LLM generation    │
         └─────────────────────────────────────────┘
                             ↓
         ┌─────────────────────────────────────────┐
         │ Load active resume + decrypt            │
         │ Fetch job description from DB           │
         └─────────────────────────────────────────┘
                             ↓
         ┌─────────────────────────────────────────┐
         │ Call assess_job_requirements()          │
         │ (Full LLM extraction)                   │
         │ - Parses requirements into structured  │
         │ - Evaluates candidate alignment        │
         │ - Generates narrative summary          │
         │ Latency: 5-15s (local) or 2-8s (cloud)│
         └─────────────────────────────────────────┘
                             ↓
         ┌─────────────────────────────────────────┐
         │ Format response:                        │
         │ - Extract strengths/gaps from assessment
         │ - Generate _match_analysis_text()      │
         │ (same formatting as LLM path)          │
         └─────────────────────────────────────────┘
                             ↓
         ┌─────────────────────────────────────────┐
         │ Cache result:                           │
         │ UPDATE jobs SET match_analysis = ?      │
         │ WHERE id = ?                            │
         └─────────────────────────────────────────┘
                             ↓
     ┌─────────────────────────────────────────────┐
     │ Return response {analysis, strengths, gaps} │
     │ with cached=false (first time) or true      │
     └─────────────────────────────────────────────┘
                             ↓
     ┌─────────────────────────────────────────────┐
     │ Extension displays detailed narrative       │
     │ in side panel (replaces quick summary)      │
     └─────────────────────────────────────────────┘
```

---

## Feature Extraction

### Deterministic Features (No ML)

The classifier receives pre-computed features:

```python
@dataclass
class MatchFeatures:
    must_coverage: float        # 0-1, % of must-have skills candidate has
    nice_coverage: float        # 0-1, % of nice-to-have skills
    seniority_fit: float        # -1 to 1, how aligned experience is with role
    education_match: bool       # True if education requirements met
    over_qualified_flag: float  # 0-1, degree of overqualification
```

**Extraction is deterministic:**
- No LLM calls
- Reuses `jd_signals.parse_experience()` (regex)
- Reuses `resume_signals.*` parsers
- Combined via `extract_match_features()`

---

## The Scorer

There is exactly one scorer: `backend.match_classifier.DeterministicClassifier`. It is a weighted
rule-based model over the deterministic features above — no model file, no training step, no
per-user state — so the same JD and resume always produce the same score, which is what makes a
stored score reproducible and lets the audit re-derive it.

```text
score = (60·must_coverage + 20·nice_coverage + 15·seniority_fit + 5·education_match)
        / (sum of the components the JD defines) × 100
```

- Weights are renormalised over the components the **posting** defines (issue #70, #108): a JD with
  no education requirement or unparseable seniority drops that term instead of scoring a constant.
- Must-haves dominate. A JD that states no requirement set at all is scored on the full scale and
  left low rather than renormalised over 20 points.
- `experience_years_delta < -0.3` (candidate clearly too junior) scales the score by 0.8.
  Over-qualification is advisory: it is surfaced, never used to lower the score.
- The must-have floor (`ScoringConfig.must_floor`) caps the score afterwards — see below.

**Performance:** <1ms, no dependencies.

### Band thresholds & recommendation (`ScoringConfig`)

Band and apply-recommendation are **not hardcoded** in the classifier. Both paths call
`band_and_recommendation` (`backend/match_scoring.py`), which reads the shared `ScoringConfig`
dataclass. Defaults — overridable per user via `scoring_config_json` in `GET/PUT /api/preferences`:

| Field | Default | Meaning |
| :--- | :--- | :--- |
| `strong` | `85.0` | ≥ this → "Strong" |
| `good` | `70.0` | ≥ this → "Good" |
| `moderate` | `55.0` | ≥ this → "Moderate" |
| `weak` | `40.0` | ≥ this → "Weak"; below → "Poor" |

Two rules the band table alone does not capture:

- `score is None` → band `"Unknown"`, recommendation `"unknown"` (no fabrication on failure).
- `gated=True` (a must-have floor failure) → `"skip"` **regardless of band**, and
  `over_qualified=True` → `"apply_with_caution"` even in the Strong/Good bands.

A must-have floor (`must_floor=0.5`) caps the score at `cap_when_below_floor=55.0`, so passing the
floor gates the upper bands.

---

## Configuration

### Real knobs

The classifier itself has **no environment variables**. What is tunable:

| Knob | Where | Effect |
| :--- | :--- | :--- |
| Band cutoffs (`strong`/`good`/`moderate`/`weak`) & coverage weights | `UserPreference.scoring_config_json` via `GET/PUT /api/preferences` → `scoring_config` | Changes band/recommendation boundaries and must/nice weighting for both scoring paths |
| Embedding backend | `EMBEDDING_*` env vars, or `GET/PUT /api/settings/embeddings` | Semantics only — does not change the score thresholds |

`ScoringConfig` is the single source of truth: `ScoringConfig.from_mapping(get_scoring_config(db))`
is how every scorer reads it, so editing preferences changes bands without a code change.

### Feature Flags

Classifier is always enabled. No feature flag to disable.

---

## Usage Examples

### Extension (JavaScript)

```javascript
// Fetch fast match analysis
const result = await fetch(`${backendUrl}/api/match/analyze`, {
  method: "POST",
  headers: {
    "Content-Type": "application/json",
    "X-API-Key": apiKey
  },
  body: JSON.stringify({
    job_title: "Senior Python Engineer",
    company: "TechCorp",
    job_description: jobDesc,
    location: null
  })
});

const data = await result.json();

// Check if score came from classifier
if (data.analysis_source === "classifier" && data.job_id) {
  // Show "View Detailed Analysis" button
  showButton("💡 View Detailed Analysis", async () => {
    const detailed = await fetch(
      `${backendUrl}/api/match/${data.job_id}/detailed-analysis`,
      { method: "POST", headers: {...} }
    );
    const narrative = await detailed.json();
    displayAnalysis(narrative.analysis);
  });
}
```

### Dashboard (REST)

```bash
# Analyze a job posting without saving
curl -X POST http://localhost:8000/api/match/analyze \
  -H "X-API-Key: your-key" \
  -H "Content-Type: application/json" \
  -d '{
    "job_title": "Senior Python Engineer",
    "company": "TechCorp",
    "job_description": "5+ years Python...",
    "resume_text": null
  }'

# Later: fetch detailed analysis for saved job #42
curl -X POST http://localhost:8000/api/match/42/detailed-analysis \
  -H "X-API-Key: your-key"
```

### Python Backend

```python
from backend.main import analyze_job_match_endpoint
from backend.database import Session

# Get score instantly
response = analyze_job_match_endpoint(
    payload=MatchAnalyzeRequest(
        job_title="Senior Python Engineer",
        company="TechCorp",
        job_description="...",
        location="SF"
    ),
    db=session
)

print(f"Score: {response.match_score}%")
print(f"Source: {response.analysis_source}")  # "classifier" or "llm"
print(f"Method: {response.match_method}")    # "classifier_v1" or "requirement_coverage_v1"
```

---

## Performance Benchmarks

### Discovery (Classify Only)

| Phase | Latency | Tokens | Notes |
|-------|---------|--------|-------|
| Feature extraction | <1ms | 0 | Regex + parsing |
| Classifier inference | <2ms | 0 | Deterministic weights |
| **Total** | **<100ms** | **0** | No LLM calls |

### On-Demand Detailed Analysis (First Time)

| Phase | Latency | Tokens |
|-------|---------|--------|
| Resume + JD fetch | <5ms | 0 |
| LLM assessment | 8-12s (cloud) / 5-15s (local) | 600-2000 |
| Response formatting | <10ms | 0 |
| DB cache write | <5ms | 0 |
| **Total** | **5-15s** | **600-2000** |

### On-Demand Detailed Analysis (Cached)

| Phase | Latency | Tokens |
|-------|---------|--------|
| DB lookup | <1ms | 0 |
| Return cached | <1ms | 0 |
| **Total** | **<1ms** | **0** |

### Token Savings

**Per 100 jobs discovered:**
- **Before:** 100 × 800 = 80,000 tokens
- **After:** 0 tokens (unless users click "View Details")
- **Typical user behavior:** ~5-10% click rate → 4,000-8,000 tokens total
- **Savings: 90-95%**

---

## Why there is no training step

Scoring is deliberately not learned per user. A fitted model was removed because:

- It could only be trained locally, on the operator's own apply/dismiss history, and that artifact
  is personal data that must never be committed — so a second installation could never have one.
- It needed scikit-learn, which was never installed by default, so the path was inert.
- Every training-capable installation would score the *same* posting differently, which breaks the
  reproducibility the audit depends on.

Selection between alternative *postings* is expressed through preferences, dismissals and the
application workflow, not by retraining the scorer.

## Troubleshooting

### Q: Score takes 5-15s instead of <100ms

**A:** Classifier fallback to LLM. Likely causes:
- Classifier crashed (check logs for exceptions)
- Resume parsing failed (check resume format)
- JD signal extraction failed (check job description is complete)

**Fix:**
```bash
grep "Classifier-based scoring failed" logs/app.log
```

### Q: "View Detailed Analysis" button doesn't appear

**A:** `analysis_source` is not "classifier" or `job_id` is null.

**Verify:**
- Check response field: `"analysis_source": "classifier"`
- Check response field: `"job_id": 42` (not null)

**Fix:** For extension, ensure ad-hoc analysis (from side panel) isn't trying to fetch detailed analysis (requires saved job).

### Q: Detailed analysis is slow

**A:** First request (cache miss) runs LLM (5-15s). Subsequent requests are <1ms.

**Verify cache is working:**
```sql
SELECT id, title, match_analysis FROM jobs WHERE id=42 LIMIT 1;
```

Should show non-null `match_analysis` after first request.

## Future Enhancements

### Possible Improvements

1. **Confidence scoring:** Return confidence interval (0.5-0.95) alongside score
2. **A/B testing:** Compare the deterministic score against the LLM analysis; surface divergence
3. **Feature importance:** Return which features most influenced the score ("60% seniority fit")
4. **Fast narrative:** Return 1-2 sentence summary from classifier (no LLM)
5. **Streaming analysis:** Stream LLM narrative as it's generated

### Not Planned (Out of Scope)

- Multi-model ensemble (classifier + heuristic + LLM voting) — added complexity without clear benefit

---

## Summary

✅ **Fast classifier** delivers scores in <100ms  
✅ **On-demand LLM** generates narratives only when requested  
✅ **Caching** avoids re-computation  
✅ **Token savings** ~90% reduction in typical usage  
✅ **Graceful degradation** works without ML (heuristic always available)  
✅ **Zero configuration** required; works out of the box
