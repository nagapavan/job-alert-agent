# Job Alert Agent: AI/ML Portfolio Journey

**Audience:** ML engineers, AI enthusiasts  
**Focus:** Decision-making, architectural trade-offs, AI/ML innovations, and lessons learned  
**Format:** Narrative + code examples + performance data

---

## Table of Contents

1. [Executive Summary](#executive-summary)
2. [Project Vision & Problem Statement](#project-vision--problem-statement)
3. [Phase 1: Discovery & Architectural Research](#phase-1-discovery--architectural-research)
4. [Phase 2: MVP - Synchronous Monolithic Match Analysis](#phase-2-mvp---synchronous-monolithic-match-analysis)
5. [Phase 3: The Performance Crisis & Rearchitecture](#phase-3-the-performance-crisis--rearchitecture)
6. [Phase 4: Hybrid Classifier Breakthrough](#phase-4-hybrid-classifier-breakthrough)
7. [Phase 5: On-Demand Narrative Generation](#phase-5-on-demand-narrative-generation)
8. [Key AI/ML Decisions & Trade-offs](#key-aiml-decisions--trade-offs)
9. [Performance Evolution & Optimization Strategies](#performance-evolution--optimization-strategies)
10. [Open Source & Lessons Learned](#open-source--lessons-learned)
11. [Future Enhancements](#future-enhancements)

---

## Executive Summary

**Job Alert Agent** is a local-first job discovery and application assistant. Built with LLM coding apps in co-pilot mode.  
I have started with N8N with Gmail API plug-in integration, and realized that the approach will not be enough to track all the job applications and possible opportunities (from LinkedIn application mails and direct-mails from the hiring teams).  
Though the setup worked, I was not happy with the limited options - which gave me the idea to write a stand-alone application with the idea of LocalLLM based JobAgent. Expanded the discovery options to tracking not just direct emails and the LinkedIn easy-apply replies, but also the public job-boards like Greenhouse and etc.

I have started of with the goal to track GMail application status, the journey evolved the application into something bigger with great learning experience.

The application combines:

- **Rule-based heuristics** (instant, zero-token classification)
- **Trained ML classifiers** (logistic regression, optional scaling)
- **Large language models** (on-demand narrative generation, with semantic caching)
- **Vector embeddings** (semantic similarity search and dismissal pattern matching)
- **Browser automation** (LinkedIn, ATS portal, Gmail integration)

**Key innovation:** Hybrid scoring architecture that separates fast deterministic classification from expensive LLM-based narrative generation, reducing token consumption by ~90% while maintaining <100ms latency for the critical path.

**Why this matters for AI/ML hiring:**

- Demonstrates practical **cost optimization** in LLM-driven applications
- Shows **architectural thinking** for balancing determinism vs. learned representations
- Illustrates **full-stack AI/ML systems design** (not just model training)
- Proves ability to **measure and iterate** on production performance metrics

---

## Project Vision & Problem Statement

### The Pain Point

Job hunting is a high-volume, low-signal process:

- 100+ emails per week from recruiters (most irrelevant)
- Manual screening of job boards = repetitive, error-prone
- Tailoring materials (resume, cover letter, answers) for each application takes 20–30 minutes
- No structured tracking of past applications or reasoning

### The Vision

**Automate discovery, rank by fit, and assist application — without leaving your computer.**

Key constraints:

- **Privacy-first:** No cloud data; no OAuth tokens logged
- **Local-first inference:** Prefer local models to avoid cloud costs + latency
- **Semantic understanding:** Match candidates to roles holistically (not just keyword matching)
- **User-in-the-loop:** Never auto-submit; always show reasoning and require confirmation

### Why This Is AI/ML-Interesting

This isn't "just a scraper." The core challenge is:

> Given a resume and a job description, predict whether this role is a good fit **and explain why** — fast, cheap, and accurately.

That requires:

- Feature engineering (what makes a "match"?)
- Model selection (heuristics vs. trained vs. LLM?)
- Cost-benefit analysis (token spend vs. quality?)
- Observability (token accounting, latency tracking)
- User experience (when to show automation, when to ask for human input?)

---

**Learnings from agentic coding platforms and LLM model behavior:** See [`docs/LESSONS_LEARNED.md`](LESSONS_LEARNED.md) for detailed insights on Gemini, Claude, and DeepSeek model patterns observed while building this project.

---

## Design Phases & Architectural Pivots

The features and major changes/decisions are listed below:

- researching Local Open weight model selection to provide optimal peformance on my low-resource M3 (18GB) machine.
  - Settled with `qwen/qwen3.5-9b` for best token generation performance for my local.
    - *Note: this is a machine-specific personal override. The app's config defaults are `qwen2.5:3b` (fast tier), `llama3:8b` (standard tier), and `llama3.3:70b` (deep tier) for the Ollama provider, and per-provider models for OpenAI/Gemini/Anthropic. See `backend/config.py`.*
- research and implement best practices
  - Added langchain to streamline the workflow and later fallback to python-code first approach
    - this is to reduce calls to local API to avoid performance bottlenecs and to reduce token cost even when the user switched to cloud LLMs
- Added ATS scoring for the user Resume
  - Researched the best practices from available libraries and hardened the logic over the time.
- Job Dashboard to view opportunities: short-list, apply and dismiss the found opportunities
  - User preferences added to fetch only matching job postings.
    - Job Title, location preferences were added with multiple value options.
    - Helps in reducing the data we are pulling as well as to keep focus on the areas interested only.
    - Added one-click pruning of jobs that were not falling into the user's preferences.
  - added more portals: greenhouse, ashbyhq APIs. Opt-in mechanism for LinkedIn sync (disabled by default), Google job search alerts.
  - On-demand and scheduled scanning for user-selected organization portals.
  - BE Queue based processing for each portal, and each job parsing and ATS scoring match.
    - Avoids overwhelming the resource-constrained LocalLLM instance
- The initial approach was to use a seperate Chrome profile, isolated for the job application tracking and job discovery
  - This allowed to avoid taking control of user's active browser.
  - Provided sand-box experience, to separate regular usage and job search profile separate.
  - Allows to batch apply selected job listings from the Jobs page.
  - There is no auto-apply here, user need to handle the application filling and logins for job portals like workday, etc
- Added cloud LLM fallback, if the user prefers speed over privacy-first LocalLLM approach.
  - OpenAI, Gemini and Anthropic are supported at the moment
- While working on the application filling, I realized a gap with the application.
  - We do not have a dynamic way to verify the ATS score for the opportunity.
  - Also, there is no way to auto-fill the page (not auto-apply) with known user profile information.
  - This led to the addition of a Chrome extension.
- Chrome extension - assistant and co-pilot:
  - User needs to load the custom extension from the app folder
  - Extension connects to the live API. Also, have an offline option to work with the API and also with the Gemini Nano (on Chrome browser)
  - Set the user profile information, if already not available from API (from Resume)
  - Co-Pilot provides a chat agent. The chat gets the context of the active JD in the active chrome tab.
    - Provides Interview Prep, Resume customization and also helps with an outreach note for the JD.
  - Have an option to train and add Q&A cache - if user requires help with most frequent questions on Job application pages.
- ATS scoring
  - Started with a full LLM based scoring for job description match against the user's Resume.
  - To reduce the time-taken to process, added vectorized Resume information to be passed to the LLM requests.
  - Added caching of request and results, to avoid reprocessing of the same JD
  - Deterministic ML algorithms
    - Even with the caching, vector data filtering per job description, the ATS scoring started taking longer and started hallucinating by adding random scores.
    - Adding a judge validation did not help much.
      - This resulted in iterative re-evaluations to get better scoring.
      - Only ended up with increasing the time to process and load on LocalLLM
    - While verifying the code flows and retrospecting the code, it occured to me I might be un-necessarily using LLM where a standard ML heuristic classifiers with cosine similarity check of JD and user profile elements would help.
      - Moved a piece of JD match scoring with the heuristic model, which improved the speed of scoring.
      - This gave me confidence that this approach is correct and moved rest of the scoring too.
      - Here is where I realized the previous LLM based matcher have too many hard-coded responses (missed in Gemini code) and also the matcher scoring was not comprehensive.
      - Started off with adding more information dimensions to the matcher algorithms with individual heuristing scoring:
        - the skills - must-haves, good to haves
        - experience ranges
        - location and work mode
        - job role specific responsibilities
  - Added retry with for LLM failures to handle 429s and network issues

---

## Phase 1: Discovery & Architectural Research

### Goals

- Understand the problem domain (what makes a job a good fit?)
- Design the data pipeline (where do jobs come from? how do we ingest them?)
- Plan the AI/ML architecture (what models? when to invoke them?)

### Key Decisions

#### 1.1 Where Do Jobs Come From?

**Candidates Considered:**

- A. LinkedIn API (official) — deprecated, high rate limits
- B. LinkedIn scraping (Playwright) — violates ToS, but data-rich
- C. Google Jobs API (unofficial) — `jobspy` library, public aggregator
- D. ATS portals (Greenhouse, Lever, Ashby) — direct, high-quality, documented
- E. Gmail recruiter emails — opt-in, low-volume but high-intent

**Decision:** Multi-source ingestion

- Primary: **ATS portals** (documented, company-direct, high quality)
- Secondary: **LinkedIn alerts + recommendations** (Playwright automation, despite ToS ambiguity)
- Tertiary: **Google Jobs** (via jobspy library, optional feature flag)
- Fallback: **Gmail recruiter emails** (BYOK Gmail API, strict opt-in)

**Why:** Reduces single-source bias; LinkedIn gives discovery, ATS gives certainty.

#### 1.2 Data Model: What's a "Job"?

**Core entity design:**

```python
class Job(Base):
    id: int                              # Primary key
    company_id: ForeignKey              # Denormalization target
    title: str                          # Searchable
    description: Text                   # Full JD (for LLM)
    url: str                            # Canonical apply URL (dedup key)
    salary_range: str                   # Structured (optional)
    location: str                       # For geo-filtering
    source: str                         # "LinkedIn Job Alert", "Greenhouse Portal", etc.
    source_type: str                    # Categorical: LinkedIn | Direct | Email
    status: str                         # Lifecycle: "To Apply" → "Applied" → "Interview" → "Offered"

    # AI/ML fields
    match_score: float                  # 0–100 (the prediction we care about)
    match_analysis: Text               # Why? (strengths, gaps, feedback)
    cover_letter_draft: Text           # Tailored for this role
    tailored_resume_points: Text       # Which skills to highlight
    cold_message_draft: Text           # For cold outreach
    embedding: SafeVector(384)         # Semantic search

    # Metadata
    is_ghost_job: bool                 # Did-diligence flag (repost count)
    submission_confirmed: bool         # Human confirmation gate
    match_scored: bool                 # Was this AI-analyzed (not just discovery baseline)?
    created_at: DateTime
    applied_at: DateTime (nullable)
    updated_at: DateTime
```

**Trade-off:** Denormalized `company_id` alongside embedded company name in description for search convenience, but maintains referential integrity.

#### 1.3 AI/ML Pipeline: The Initial Monolithic Approach

**First concept (Phase 1 brainstorm):**

```
User opens app
  ↓
Scrape jobs from ATS / LinkedIn
  ↓
For each job:
  ├─ Extract from JD: title, skills, experience, salary
  ├─ Load resume
  ├─ Call LLM: "Does this match the candidate?"
  └─ Store: match_score, match_analysis, embedding
  ↓
Rank by match_score
  ↓
Display to user
```

**Why it looked good:** Simple, single responsibility (LLM does all the reasoning).

**Why it was naive:** Every job = one full LLM call (5–15s). Discovering 50 jobs = 4–10 minutes blocked.

---

## Phase 2: MVP - Synchronous Monolithic Match Analysis

### Implementation (Sept 2026)

Built the straightforward version:

1. **Job discovery:** Playwright scraper for LinkedIn/ATS, Google Jobs via jobspy
2. **Match analysis:** FastAPI endpoint `/api/match/analyze` that called Claude 3.5 Sonnet
3. **Database:** SQLAlchemy ORM with Job, Company, Resume, ApplicationEvent tables
4. **UX:** Vanilla JS SPA + Chrome extension side panel
5. **Safety:** Encryption at rest (Fernet), API key auth, human confirmation for submissions

**Code example (Phase 2 `/api/match/analyze`, simplified to the essential flow):**

```python
@app.post("/api/match/analyze", response_model=MatchAnalyzeResponse)
def analyze_job_match_endpoint(payload: MatchAnalyzeRequest, db: Session = Depends(get_db)):
    # Load the active resume; resume text is Fernet-encrypted in the DB, so decrypt it.
    active_resume = (
        db.query(Resume)
        .filter(Resume.is_active == True)
        .order_by(Resume.created_at.desc())
        .first()
    )
    resume_text = (payload.resume_text or "").strip()
    if not resume_text and active_resume:
        resume_text = decrypt_data(active_resume.content_encrypted)

    if not resume_text:
        return MatchAnalyzeResponse(match_score=None, analysis_source="error",
                                    error="No resume available.")

    description = (payload.job_description or "").strip()
    if len(description) < MIN_JOB_DESCRIPTION_CHARS:
        return MatchAnalyzeResponse(match_score=None, analysis_source="error",
                                    error="Job description is too short to score reliably.")

    # Deterministic fast path first (no LLM); see "Phase 4" below for the classifier.
    det = _deterministic_match(db, resume_text, description,
                               parsed_resume=_parsed_resume_of(active_resume),
                               job_title=payload.job_title or "")
    if det:
        return MatchAnalyzeResponse(
            match_score=det["score"], strengths=det["strengths"], gaps=det["gaps"],
            feedback=det["feedback"], analysis_source="classifier", match_method="classifier_v1",
        )

    # Bounded LLM fallback — one structured generation call, capped by LLM_INTERACTIVE_TIMEOUT.
    assessment, match = assess_job_requirements(
        resume_text, description,
        parsed_resume=_parsed_resume_of(active_resume),
        scoring_config=ScoringConfig.from_mapping(get_scoring_config(db)),
        timeout=LLM_INTERACTIVE_TIMEOUT,
    )
    # Assessment drives the score, narrative, and match_method="requirement_coverage_v1".
```

*Note: the current production endpoint also uses `top_evidence(...)` for RAG-lite evidence and `_parsed_resume_of(...)` for the structured parsed resume; the fragment above captures the core flow. See `backend/main.py::analyze_job_match_endpoint` for the full implementation.*

**Performance:** 5–15 seconds per job (latency of LLM).

**UX Impact:** Acceptable for on-demand (user clicks "Analyze"), but **killed the discovery workflow**:

- User discovers 50 jobs
- User clicks "Reprocess all" in dashboard
- System attempts to score all 50 jobs
- UX is blocked for 4–10 minutes with no progress feedback

**Metrics captured:**

- `prompt_tokens` / `completion_tokens` per call (~500–1500 tokens per job)
- Request latency (5–15s)
- Token cost ($$$ to run discovery at scale)

**Lessons:**

1. ✓ LLM-based reasoning works (accurate, explains itself)
2. ✗ **LLM is expensive + slow for high-volume screening**
3. ✗ Users expect discovery (scan 50 jobs) to feel instant or show progress
4. ✗ No way to differentiate "quick check" vs. "detailed analysis"

---

## Phase 3: The Performance Crisis & Rearchitecture

### Problem: September 30, 2026

User complaint: "Why does 'Reprocess All' take 10 minutes?"

### Root Cause Analysis

Every single `/api/match/analyze` call triggered **full LLM extraction:**

- Feature extraction (title, skills, years exp, salary) — wasted tokens
- Requirements analysis (what does the job want?) — same as previous 49 jobs
- Reasoning (does resume match?) — the only part that changes per job

**Hypothesis:** 80–90% of tokens are redundant overhead; 10–20% is the actual match reasoning.

### Architectural Options Considered

**Option A: Batch Processing**

- Call LLM with 5 jobs at once, get 5 scores
- Pros: 20% token savings
- Cons: Context window thrashing, worse quality for heterogeneous jobs

**Option B: Hybrid Classifier (Fast + On-Demand) ← CHOSEN**

- Fast path: Rule-based heuristic + optional trained ML classifier (~<100ms, 0 tokens)
- Slow path: LLM-generated narrative (only when user asks, ~5-15s, full tokens)
- Pros: 90% token savings, instant feedback, better UX
- Cons: Need to train classifier, maintain two models

**Option C: Semantic Cache**

- Cache LLM responses for job descriptions (avoid re-analyzing same JD)
- Pros: Simple, token savings for duplicate jobs
- Cons: Only helps with reposts; doesn't solve cold-start

### Decision: Option B (Hybrid Classifier)

**Rationale:**

1. Addresses **cold-start discovery** (most common case)
2. Enables **progressive disclosure** (show score instantly, explain on-demand)
3. Measurable **token efficiency** (~90% savings typical)
4. Extensible (start with heuristics, add ML classifier later)

---

## Phase 4: Hybrid Classifier Breakthrough

### Implementation (Oct 2026, Days 1–2)

#### 4.1 Rule-Based Heuristic Classifier

**Philosophy:** Extract deterministic signals from resume + JD; no ML needed.

```python
# backend/match_classifier.py
from dataclasses import dataclass

@dataclass
class MatchFeatures:
    """Extracted signals for match scoring."""
    # From resume
    years_experience: float
    key_skills: List[str]
    education_level: str  # "HS", "BS", "MS", "PhD"
    certifications: List[str]

    # From job description
    years_required: float
    required_skills: List[str]
    required_education: str
    nice_to_have_skills: List[str]
    salary_range: tuple[float, float]  # (min, max) in thousands
    location: str

    # Computed
    location_match: bool
    work_mode: str  # "remote", "hybrid", "onsite"

class SimpleHeuristicClassifier:
    """Zero-dependencies, rule-based match scorer."""

    def score(self, features: MatchFeatures) -> float:
        """Return 0-100 match score."""
        score = 0.0
        evidence = []

        # Skill overlap (40% weight)
        overlap = set(features.key_skills) & set(features.required_skills)
        skill_match = len(overlap) / max(len(features.required_skills), 1)
        score += skill_match * 40
        evidence.append(f"Skill overlap: {len(overlap)}/{len(features.required_skills)}")

        # Experience level (30% weight)
        if features.years_experience >= features.years_required:
            score += 30
            evidence.append(f"Experience: {features.years_experience:.1f} years (required {features.years_required:.1f})")
        elif features.years_experience >= features.years_required * 0.7:
            score += 20  # Close enough
            evidence.append(f"Experience: slightly junior ({features.years_experience:.1f} vs {features.years_required:.1f})")
        else:
            evidence.append(f"Experience gap: {features.years_experience:.1f} < {features.years_required:.1f}")

        # Education (15% weight)
        education_hierarchy = {"HS": 0, "BS": 1, "MS": 2, "PhD": 3}
        if education_hierarchy.get(features.education_level, 0) >= education_hierarchy.get(features.required_education, 0):
            score += 15
            evidence.append(f"Education: {features.education_level} (meets {features.required_education})")

        # Location (10% weight)
        if features.location_match:
            score += 10
            evidence.append("Location: match")
        else:
            evidence.append("Location: mismatch (remote? visa?)")

        # Nice-to-have bonus (5% weight)
        nice_overlap = set(features.key_skills) & set(features.nice_to_have_skills)
        bonus = min(len(nice_overlap), 2) * 2.5
        score += bonus
        if nice_overlap:
            evidence.append(f"Nice-to-have: {nice_overlap}")

        return min(score, 100.0), evidence

def extract_match_features(resume_text: str, jd_text: str, job: Job) -> MatchFeatures:
    """Extract deterministic features from unstructured text."""
    # Regex + keyword matching (no LLM)
    years_exp = extract_years_of_experience(resume_text)  # Regex: "10 years"
    skills = extract_skills(resume_text, SKILL_TAXONOMY)  # Against known list
    education = extract_education_level(resume_text)  # Regex: "BS CS", "PhD"

    years_req = extract_requirement_years(jd_text)
    req_skills = extract_required_skills(jd_text, SKILL_TAXONOMY)
    req_education = extract_required_education(jd_text)

    return MatchFeatures(
        years_experience=years_exp,
        key_skills=skills,
        education_level=education,
        required_skills=req_skills,
        years_required=years_req,
        required_education=req_education,
        location_match=check_location_match(job.location, user_prefs),
        # ... etc
    )
```

**Trade-off:** Rules are brittle (regex mistakes, domain knowledge baked in), but:

- ✓ Zero dependencies (no sklearn, no API calls)
- ✓ Instant execution (<10ms)
- ✓ Explainable (clear evidence)
- ✓ Serves as fallback when anything breaks

**Accuracy:** ~70% baseline (will improve with ML classifier, but usable as-is).

#### 4.2 Fast API Endpoint: `/api/match/analyze` Redesign

```python
@app.post("/api/match/analyze", response_model=MatchAnalyzeResponse)
async def analyze_match(
    request: MatchAnalyzeRequest,
    db: Session = Depends(get_db),
):
    job = db.query(Job).filter(Job.id == request.job_id).first()
    resume = db.query(Resume).filter(Resume.is_active).first()

    if not job or not resume:
        raise HTTPException(status_code=404)

    # Extract features deterministically
    features = extract_match_features(resume.content, job.description, job)

    # Try trained classifier (if available)
    classifier = load_classifier()
    if classifier and classifier.is_trained:
        try:
            score, confidence = classifier.predict_with_confidence(features)
            evidence = classifier.get_evidence(features)
            method = "trained_classifier"
        except Exception:
            # Fallback to heuristic
            score, evidence = SimpleHeuristicClassifier().score(features)
            method = "heuristic_fallback"
    else:
        # Use heuristic
        score, evidence = SimpleHeuristicClassifier().score(features)
        method = "heuristic"

    # Store result (no LLM yet)
    job.match_score = score
    job.match_scored = True  # Marked as scored, but no narrative yet
    job.match_analysis = None  # Narrative deferred
    db.commit()

    return MatchAnalyzeResponse(
        job_id=job.id,
        match_score=score,
        match_score_band=categorize_score(score),  # "Strong Fit", "Good Fit", "Consider"
        evidence=evidence,
        analysis_source=method,
        recommendation="Apply" if score > 75 else "Review" if score > 50 else "Skip"
        # NOTE: Detailed analysis is NOT included; user can request via separate endpoint
    )
```

**Performance:** <100ms (local feature extraction only, no network).

**UX:** User discovers 50 jobs → scores all in <5 seconds → can see ranked list immediately.

#### 4.3 Optional: Trained ML Classifier

```python
class TrainedClassifier:
    """Scikit-learn wrapper; trained from historical match scores."""

    def __init__(self, model_path: str = "data/match_classifier.pkl"):
        self.model = self._load_or_none(model_path)
        self.is_trained = self.model is not None

    def predict_with_confidence(self, features: MatchFeatures) -> Tuple[float, float]:
        """Return (score, confidence)."""
        if not self.is_trained:
            return None

        X = self.featurize(features)  # Convert dataclass to feature vector
        score = self.model.predict(X)[0]  # 0-100
        confidence = self.model.predict_proba(X)[0].max()  # 0-1
        return score * 100, confidence

    @staticmethod
    def train_from_db(db: Session, model_path: str):
        """Train on all jobs where match_scored=True (manual assessment)."""
        jobs = db.query(Job).filter(Job.match_scored).all()

        if len(jobs) < 10:
            logger.info(f"Insufficient data: {len(jobs)} < 10")
            return False

        # Build training set
        X = []
        y = []
        for job in jobs:
            features = extract_match_features(job.resume.content, job.description, job)
            X.append(classifier.featurize(features))
            y.append(job.match_score)

        # Train logistic regression
        from sklearn.linear_model import LogisticRegression
        model = LogisticRegression().fit(X, y)

        # Save
        with open(model_path, "wb") as f:
            pickle.dump(model, f)

        logger.info(f"Trained classifier on {len(jobs)} samples")
        return True
```

**Training:** Run `python scripts/train_match_classifier.py` after 10+ historical judgments.

**Why logistic regression?**

- Simple, interpretable (can explain which features matter)
- Fast inference
- Works on small datasets (unlike deep learning)
- Consistent with "deterministic" philosophy (no black-box)

### Results (End of Phase 4)

**Before (Phase 2):**

- Discover 50 jobs: 4–10 minutes (blocked on LLM)
- Token cost: ~50 × 1000 tokens = 50K tokens (~$0.50)

**After (Phase 4):**

- Discover 50 jobs: <5 seconds (local classifier)
- Token cost: $0.00 (no LLM)
- User satisfaction: Instant feedback → ranked list → pick what to analyze deeply

**Metrics:**

- Heuristic classifier accuracy: ~70% (compared to historical LLM scores)
- Trained classifier accuracy: ~85% (after 20+ training samples)
- Latency: <100ms (vs. 5–15s)
- Token savings: 99% vs. monolithic approach

*Note: the ~70% heuristic / ~85% ML accuracy figures above are from practical measurement — custom tests and bulk sync runs executed from the app — not an externally audited benchmark.*

---

## Phase 5: On-Demand Narrative Generation

### Problem: Phase 4 Shortcoming

Fast classifier gives a score + evidence, but users want **why** in detail:

- "What specific skills am I missing?"
- "Why did it score 72 instead of 80?"
- "Should I apply or keep looking?"

### Solution: Deferred LLM + Caching

New endpoint: `POST /api/match/{job_id}/detailed-analysis`

```python
@app.post("/api/match/{job_id}/detailed-analysis")
async def get_detailed_analysis(
    job_id: int,
    db: Session = Depends(get_db),
) -> DetailedAnalysisResponse:
    """
    Generate or retrieve detailed match narrative (LLM-backed).

    Flow:
    1. Check if Job.match_analysis already cached → return cached
    2. Otherwise, call LLM (full extraction) → cache in DB → return

    This defers expensive LLM work to user request, not discovery scan.
    """
    job = db.query(Job).filter(Job.id == job_id).first()
    resume = db.query(Resume).filter(Resume.is_active).first()

    # Cache hit
    if job.match_analysis:
        return DetailedAnalysisResponse(
            job_id=job_id,
            analysis=job.match_analysis,
            generated_at=job.updated_at,
            cached=True
        )

    # Cache miss: run full LLM extraction (bounded by LLM_INTERACTIVE_TIMEOUT)
    resume_text = decrypt_data(active_resume.content_encrypted)
    parsed_resume = parse_resume_json(resume_text)

    assessment, match = assess_job_requirements(
        resume_text,
        job.description or "",
        parsed_resume=parsed_resume,
        scoring_config=ScoringConfig.from_mapping(get_scoring_config(db)),
        evidence=[],  # On-demand, no pre-fetched evidence
    )
    strengths, gaps = _derive_strengths_gaps(assessment, match)
    narrative = _match_analysis_text(assessment, match)

    # Cache in DB
    job.match_analysis = narrative
    db.commit()

    return DetailedAnalysisResponse(
        job_id=job_id,
        analysis=narrative,
        generated_at=job.updated_at,
        cached=False
    )
```

**UX Integration (Extension):**

```javascript
// extension/sidepanel/sidepanel.js
if (analysisSource === "classifier" && jobId) {
  // Show button instead of full text
  const explainButton = document.createElement("button");
  explainButton.textContent = "💡 View Detailed Analysis";
  explainButton.onclick = async () => {
    const response = await fetch(`/api/match/${jobId}/detailed-analysis`, {
      method: "POST",
      headers: { "X-API-Key": apiKey },
    });
    const data = await response.json();
    showAnalysisModal(data.analysis);
  };
  container.appendChild(explainButton);
}
```

**Performance & Cost:**

- Discovery scan 50 jobs: <5 seconds, $0.00 token cost
- User clicks "Explain" on 5 jobs: 5 × (5s + $0.05) = 25s total, $0.25 cost
- Net savings: 90% vs. scanning all 50 with full LLM

---

## Key AI/ML Decisions & Trade-offs

### 1. Determinism vs. Learned Representations

**Decision:** Start deterministic (heuristics), layer ML optionally.

**Why:**

- ✓ Reproducible (same input → same output, no randomness)
- ✓ Debuggable (can trace which feature caused low score)
- ✓ Fast (no model loading overhead)
- ✓ Safe (no risk of model drift or OOD failure)
- ✗ Less accurate initially (~70% vs. LLM's 85%+)

**Trade-off:** Traded initial accuracy for reliability. Accuracy improved via optional trained classifier.

### 2. Rule-Based vs. ML vs. LLM

| Model Type               | Speed | Accuracy | Cost | Interpretability          | Maintenance            |
| ------------------------ | ----- | -------- | ---- | ------------------------- | ---------------------- |
| Heuristics               | <10ms | 70%      | $0   | Perfect                   | Manual rule tuning     |
| ML (Logistic Regression) | <10ms | 85%      | $0   | Good (feature importance) | Retraining on new data |
| LLM (Claude 3.5)         | 5-15s | 95%+     | $$$  | Good (narrative)          | Prompt engineering     |

**Chosen:** Hybrid (heuristic + optional ML for fast path, LLM for detailed path)

**Why not pure LLM?** Cost and latency for discovery (80% of use cases). Why not pure ML? Limited training data initially, less explainability.

### 3. Feature Extraction Approach

**Decision:** Regex + keyword matching (no LLM-as-extractor).

**Candidates:**

- A. LLM extraction: `"Extract years of experience from: {resume}"` → "10 years"
  - Pros: Flexible, handles variations
  - Cons: Slow, expensive, introduces latency into feature pipeline
- B. Regex + curated skill taxonomy (chosen)
  - Pros: Fast, deterministic, zero-cost
  - Cons: Fragile (misses variations, new skills)

**Hybrid approach:** Use regex + taxonomy for 90% of cases; fall back to pattern-matching if detection fails.

### 4. Caching Strategy

**Decision:** Cache at the **analysis level**, not the feature level.

```
Job → Feature extraction (cheap) → Classifier (cheap) → Cache hit
                                       ↓
                         (User requests details)
                                       ↓
                         LLM extraction (expensive) → Cache in DB
```

**Why not cache features?** Jobs are immutable; if you've cached features for a JD, you'll likely need them again for different candidates.

**Why cache analysis?** Same job viewed by multiple candidates (rare, but no loss). More importantly: simplifies expiry logic (analysis valid forever unless job description changes).

### 5. Multi-Source Discovery

**Decision:** Ingest from multiple sources (LinkedIn, ATS, Gmail), deduplicate, rank by match.

**Rationale:**

- No single source is complete (LinkedIn misses private companies; ATS portals miss fresh listings)
- Redundancy improves robustness (if LinkedIn scraping breaks, ATS portals still work)
- User control (can disable channels via feature flags)

**Trade-off:** Added complexity (source-specific parsers, dedup logic) vs. robustness.

---

## Performance Evolution & Optimization Strategies

### Iteration 1: Monolithic LLM (Phase 2)

```
Scan job → Extract score + narrative + resume points + cover letter
           (single LLM call, ~1500 tokens)
           (latency: 5-15s per job)
```

**Problem:** Linear scaling with job count (50 jobs = 4–10 min).

### Iteration 2: Hybrid Classifier (Phase 4)

```
Scan job → Extract features (0.01s)
         → Classifier score (0.01s)
         → Cache score + evidence
         → (User optionally requests narrative: 5-15s)
```

**Result:** 50-150x speedup for typical discovery.

### Iteration 3: Token Accounting & Backpressure (Phase 3+)

Realized token spend is **the bottleneck**, not request latency.

**Strategy:**

1. **Explicit token tracking:** Every LLM call records `prompt_tokens` + `completion_tokens` → `OperationLog`
2. **Per-priority concurrency:** Limit parallel workers (1 for local, 8 for cloud) to prevent VRAM thrashing / API rate limits
3. **Cost-aware routing:** Direct routine work to fast models (local Llama 2), complex reasoning to Claude 3.5 (only on-demand)

**Code example (llm_queue.py):**

```python
class LLMQueueManager:
    def __init__(self, is_local: bool):
        self.max_workers = 1 if is_local else 8
        self.pacing_seconds = 0.2 if is_local else 0.0
        self.queue = PriorityQueue()  # Min-heap by (priority, timestamp)

    async def enqueue(self, prompt, model, priority="background"):
        """
        priority: "interactive" (1) < "on_demand" (2) < "background" (3)

        Returns immediately; actual execution happens on worker thread.
        """
        job = LLMJob(prompt=prompt, model=model, priority=priority)
        self.queue.put((priority, time.time(), job))

        if self.queue.qsize() > self.max_workers * 4:
            signal_backpressure()  # Warn UX: queue is deep

    def run_worker(self):
        while True:
            _, _, job = self.queue.get()

            # Respect pacing (e.g., 0.2s for local to avoid VRAM thrashing)
            time.sleep(self.pacing_seconds)

            result = invoke_llm(job.model, job.prompt)
            job.set_result(result)

            # Record token usage
            track_tokens(result.usage.prompt_tokens, result.usage.completion_tokens)
```

### Iteration 4: Semantic Dismissal Filter

**Problem:** Some jobs match immediately-obvious negative patterns:

- "Java-only shop" (candidate does Python)
- "On-site in NYC" (candidate remote-only in SF)
- "Startup pre-seed" (candidate wants stability)

**Solution:** Zero-token filter before classifier.

```python
async def check_semantic_dismissal(job: Job, user_prefs) -> bool:
    """
    Check if job matches any dismissed patterns (embeddings).

    If match confidence > 0.80, return True (skip job).
    Zero LLM calls.
    """
    dismissal_patterns = db.query(DismissedJobPattern).all()

    for pattern in dismissal_patterns:
        similarity = cosine_sim(job.embedding, pattern.embedding)
        if similarity > 0.80:
            logger.info(f"Dismissed: {job.title} matches {pattern.title}")
            return True

    return False
```

**Benefit:** Prevents wasted classifier calls on roles the user will definitely reject.

---

## Open Source & Lessons Learned

### What Went Well

1. **Modular architecture:** Each component (scraper, classifier, LLM queue) lives in its own file with clear interfaces. Easy to test, extend, or swap implementations.

2. **Measured optimization:** Every decision backed by metrics (latency, token count, accuracy). Didn't optimize prematurely; profiled before redesigning.

3. **User in the loop:** Never auto-submit; always show reasoning. Builds trust and allows user to override when the system is wrong.

4. **Local-first with cloud fallback:** Runs on potato hardware (LM Studio), scales to cloud when needed. Respects privacy but doesn't require users to run complex infra.

### What Was Hard

1. **Feature engineering:** Extracting semantic meaning from unstructured resume + JD via regex is brittle. Would benefit from a small pretrained embedding model (but adds dependency). Tradeoff accepted for MVP.

2. **Deduplication across sources:** LinkedIn + ATS + Gmail can list the same job. Matching by URL + title + company is 95% effective, but edge cases remain. Full solution requires canonical job ID (impossible without coordination).

3. **Scraper maintenance:** Website changes break parsers. Greenhouse/Lever have stable APIs (documented), but LinkedIn requires Playwright + constant monitoring. ATS vendor changes force rewrites.

4. **Model staleness:** Trained classifier decays over time (candidate profile evolves, market changes). Needs retraining. Heuristics are more stable but less accurate.

### Lessons for Production ML Systems

1. **Start simple, measure everything.** Heuristics were 70% accurate but taught us what matters. ML improved accuracy to 85% only after we understood feature importance.

2. **Separate the fast path from the slow path.** Discovery (scan 50 jobs) ≠ analysis (deep dive on 1 job). Design for both; don't force one path for both use cases.

3. **Cache is king.** Caching JD analysis saved ~90% of tokens. The most expensive computation is the one you never do.

4. **Interpretability matters for user trust.** A black-box model says "score 72"; interpretable heuristics say "score 72 because skill overlap is 80% but experience gap is 10 years." User prefers the latter for hiring decisions.

5. **Local first, cloud fallback.** Latency + cost + privacy all favor local inference. Cloud is backup for complex reasoning only.

---

## Future Enhancements

### Short-term (Next Quarter)

1. **Confidence scoring:**
   - Classifier returns `(score, confidence)` where confidence = agreement between heuristic + ML + semantic features
   - Flag jobs where score is uncertain for manual review

2. **Feature importance:**
   - Show which resume features → matched the job
   - "Your 5 years Python experience matched the 3+ years requirement"
   - Builds user trust, enables optimization (e.g., "get certified in React")

3. **Skill-gap recommendations:**
   - Extract gaps: `required_skills - resume_skills`
   - Rank by frequency (show most-hiring skill first)
   - Link to learning resources

### Medium-term (6–12 Months)

1. **Fine-tuned embedding model:**
   - Current: generic embeddings (no domain knowledge)
   - Future: embed a small domain-specific model trained on jobs + resumes
   - Improves semantic search + dismissal pattern matching

2. **Candidate clustering:**
   - "Jobs similar to ones you've applied to" (similarity search)
   - "Companies hiring for your profile" (invert the match)

3. **Interview prep:**
   - Extract common questions per company/role
   - Generate practice Q&A based on job description
   - Track interview performance over time

### Long-term (1+ Year)

1. **Multi-candidate support:**
   - Current: single resume (one user)
   - Future: teams / multiple candidates → shared job database → ranked by best fit

2. **Recruiter outreach assistant:**
   - User uploads recruiter email
   - Auto-flag suspicious (phantom jobs, sketchy companies)
   - Draft response or silence

3. **Market intelligence:**
   - Aggregate anonymized data: skills in demand, salary trends, company hiring velocity
   - Show "React is up 15% in last month" (without exposing individual data)

---

## Conclusion: Why This Matters for AI/ML Engineering

**Job Alert Agent** demonstrates:

1. **Systems thinking:** Not just "train a model," but orchestrating multiple models (heuristic, ML, LLM) for different purposes.

2. **Cost optimization:** Reducing token spend by 90% through architectural choices (fast path + deferred LLM), not model tricks.

3. **User-centric AI:** Designing around latency expectations and trust (show reasoning, don't auto-submit).

4. **Full-stack competence:** Scraping → parsing → feature extraction → model training → API design → UX integration.

5. **Iteration discipline:** Measuring (profiling), analyzing (root cause of slow discovery), and redesigning (hybrid classifier).

The key insight: **Don't use an LLM when a heuristic or lightweight ML classifier will do.** LLMs are powerful for reasoning; use them only for the 10% of work that truly needs semantic depth. Everything else should be fast and cheap.

---

## For Hiring Managers / AI/ML Interviewers

**What to ask in a technical interview:**

1. "Walk me through the performance crisis in Phase 3. Why did you choose hybrid classifier over alternatives?"
2. "How would you measure classifier accuracy without ground truth labels?"
3. "If you had 10x more data, how would your architecture change?"
4. "How do you prevent model drift in the trained classifier? What's your retraining strategy?"
5. "What's the most expensive bottleneck today? How would you attack it?"

**My answers:**

1. Scanning 50 jobs took 4–10 minutes; root cause was LLM for every job. Hybrid classifier defers LLM to user request (progressive disclosure).
2. Compare to historical manual scores (match_scored=True jobs). A/B test heuristic vs. ML vs. LLM on new jobs, then get user feedback.
3. With 10x data, I'd explore gradient boosted trees or a small fine-tuned transformer. Right now, logistic regression is the right level.
4. Retraining monthly on recent judgments; versioning models in `data/match_classifier_vX.pkl`. Rollback if accuracy drops.
5. Semantic dismissal filter (skip 20% of jobs instantly). Next is parallelizing classifier over multiple jobs (already done in discovery scan).

---

## Repository Structure for This Document

- **docs/PORTFOLIO_JOURNEY.md** ← You are here
- **docs/HYBRID_MATCHER_GUIDE.md** ← Technical deep-dive (for developers)
- **docs/AGENTIC_ARCHITECTURE.md** ← AI/LLM routing (for ML engineers)
- **DESIGN_DIAGRAMS.md** ← System architecture, data flows (for architects)
- **README.md** ← Quickstart and features (for everyone)
- **CHANGELOG.md** ← Version history (for users)

**To learn more:**

- Code walkthrough: `backend/match_classifier.py` (rule-based heuristic)
- Training script: `scripts/train_match_classifier.py`
- Tests: `tests/test_match_classifier.py`, `tests/test_detailed_analysis.py`

---

_Last updated: Oct 2, 2026_
_AI/ML Portfolio | Job Alert Agent v1.0_
