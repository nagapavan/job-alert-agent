# Targets: 
- `backend/ai_helper.py`: `check_semantic_dismissal` function
- `backend/ai_helper.py`: `_token_overlap_similarity` and `_distinguishing_token_coverage` functions  
- `backend/database.py`: `UserPreference.skip_topics` column addition
- `backend/preference_filter.py`: `PreferenceCriteria.skip_topics` and `rejection_reason` method

# Problem:
The current dismissal rules are blocking opportunities too aggressively. Specifically:
1. Title-only overlap at ≥0.75 similarity triggers dismissal even when only generic role words (like "senior", "engineer") match, causing false positives
2. GitLab "Not Interested" patterns block any GitLab role with token overlap ≥0.75
3. No way to specify explicit skill/topic avoidance ("skip topics") that should only filter jobs mentioning those skills in must-have requirements or titles

# Decision:
1. **Fix overly broad title matching**: Added `_distinguishing_token_coverage` helper to identify non-generic tokens from pattern titles. Title-only dismissal now requires both:
   - Title overlap ≥0.75
   - Distinguishing token coverage ≥0.5 (or pattern has no distinguishing tokens)
   Generic tokens list includes common role/seniority words that don't distinguish specific roles

2. **Add precise skip topics**: 
   - Added `skip_topics` column to `UserPreference` (comma-separated)
   - Topics are checked in titles via word-boundary regex matching
   - For descriptions, topics are only checked in must-have requirement sections (looking for markers like "must have", "required:", "candidate must" within 500 chars)
   - Skip topics are the first check in `rejection_reason` (explicit user avoidance trumps other filters)

3. **Implementation details**:
   - Added database migration in `init_db()` for `skip_topics` column
   - Updated `PreferenceCriteria.from_pref()` to parse skip topics
   - Updated method signatures to accept `description` parameter for must-have requirement checking
   - Added `re` import for regex word-boundary matching

# Why:
- The current system was hiding genuine opportunities due to overly broad matching
- Users need precise control over what to avoid, not blanket blocking based on generic title words
- Skip topics should be surgical: avoid specific skills mentioned as requirements, not casual mentions
- The company-scoping already exists and prevents cross-company blocking; this fix addresses intra-company overblocking