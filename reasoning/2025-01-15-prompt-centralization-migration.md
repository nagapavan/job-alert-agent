# Targets: `backend/ai_helper.py`, `backend/main.py`, `backend/scraper.py`, `backend/prompts.py`

# Problem

The codebase has 13 multi-line prompts scattered across 3 source files with inline string definitions. This creates maintenance and auditability issues:

- No centralized location for prompt review and updates
- Difficult to test prompts in isolation
- No version control/audit trail for prompt changes
- Inconsistent patterns across files
- Hard to A/B test different prompt variants

**Current state:**
- `backend/prompts.py` already exists with 4 centralized prompts (33% complete)
- 12 prompts still inline in `ai_helper.py`, `main.py`, and `scraper.py` (67% remaining)
- ~18,000+ characters of inline prompt text

# Decision

**Chosen approach:** Complete the migration of all remaining inline prompts to `backend/prompts.py`.

**Rationale:**

1. **Single source of truth:** All LLM instruction text lives in one module, making it reviewable and consistent
2. **Maintainability:** Prompt updates require editing only one file
3. **Auditability:** Changes to prompts are visible in git history of `prompts.py`
4. **Testability:** Prompts can be imported and unit tested independently
5. **A/B testing:** Easy to create prompt variants without changing production code
6. **Documentation:** Each prompt can have descriptive docstrings explaining its purpose
7. **Type safety:** Functions with parameters vs. static strings where needed

## Migration Status (COMPLETED 2026-10-15)

All remaining inline prompts have been migrated to `backend/prompts.py`. The following files were updated:

### `backend/prompts.py` (expanded)
Added new centralized functions/constants:
- `resume_ats_parser_system()` — Resume → structured JSON (ATS format)
- `requirement_extraction_system()` — Job requirements extraction/assessment
- `cover_letter_writer_system()` — Anti-slop, grounded 3-4 paragraph cover letters
- `resume_tailoring_suggestions_system()` — ATS-optimization resume coach
- `cold_outreach_message_system(max_chars)` — LinkedIn connection notes (char-limited)
- `application_essay_answer_system(...)` — ATS screener essay answers
- `consolidated_package_system(schema_str)` — Single-shot application package assessment
- `grounded_pitch_system()` — 3-5 sentence factually grounded pitches
- `pitch_grounding_judge_system()` — Pitch hallucination fact-check judge
- `cover_letter_grounding_judge_system(slop_phrases_list)` — Cover letter ground/slop judge
- `interview_prep_advisor_system(...)` — Per-job interview prep chat advisor
- `company_intelligence_synthesis_system()` — Company web-snippet synthesis

### `backend/ai_helper.py` (10 prompts removed)
- Imported centralized prompt functions from `backend.prompts`
- Replaced all inline `system_prompt = (...)` definitions with function calls
- Removed `_REQUIREMENT_EXTRACTION_PROMPT` constant
- `interview_prep_advisor_system` wired into chat flow

### `backend/main.py` (1 prompt removed)
- Added `from backend.prompts import interview_prep_advisor_system`
- Replaced inline interview prep advisor prompt with function call

### `backend/scraper.py` (1 prompt removed)
- Added `from backend.prompts import company_intelligence_synthesis_system`
- Replaced inline company intelligence synthesis prompt with function call

## Naming conventions
- Static prompts (no parameters): UPPERCASE constant (e.g., `JOB_MATCH_SCORING_SYSTEM`)
- Functions with parameters: snake_case function (e.g., `resume_ats_parser_system()`)

## Validation
- All backend modules import cleanly (`ai_helper`, `main`, `scraper`, `prompts`)
- 159 tests pass across `test_ai_helper`, `test_interview_questions`, `test_match_analysis`, `test_scraper`, `test_chat_agent`, `test_structured_outputs`, `test_cover_letter_evals`, `test_match_scoring`

This follows the existing pattern already established in `backend/prompts.py`.