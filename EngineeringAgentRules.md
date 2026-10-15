# Engineering Agent Rules

Engineering principles and workflow rules for the job-alert-agent project. These apply to all contributors. Detailed reference material lives in the linked docs; this file captures the rules that must be followed.

---

## 0. Core Engineering Workflow & Ground Rules

Every feature request, enhancement, or non-trivial bugfix follows a 5-phase lifecycle:

1. **Phase 1: Multi-Dimensional Research & Assessment** — define Functionality (inputs, outputs, edge cases), Viability (feasibility vs. existing architecture, SDK limits, rate limits, VRAM pacing, privacy), and Usability (ergonomics, UI friction, error states).
2. **Phase 2: Spec Generation & Planning** — technical spec + data contracts + component touchpoints, only after all three dimensions pass.
3. **Phase 3: TDD-Based Incremental Implementation** — write test assertions first, implement incrementally, verify passing at each step.
4. **Phase 4: Side-Effect & Regression Verification** — run the full test suite (`./.venv/bin/pytest -v tests/`) plus frontend/extension JS checks; zero regressions.
5. **Phase 5: Walkthrough & User Sign-Off** — document changes, validation results, and test instructions.

**Ground rules:**
- No slop-comments in code; put rationale in design docs instead.
- Do not over-engineer or add unnecessary abstraction layers.
- Add files/functions only when necessary; reuse existing code first.
- Concise answers before elaboration; provide only what is requested.

---

## 1. Code Editing & File Integrity

- **Never push without explicit approval.** Local commits and branches are fine; no `git push` unless asked.
- **Active file targeting** — modify only the owning file; if a change belongs elsewhere, update that file.
- **No unnecessary files** — no new files/folders unless requested or approved.
- **No over-engineering documentation** — do not auto-generate docs, checklists, or analyses unless explicitly requested. If asked "should we do X?", wait for a decision.
- **Pydantic V2** conventions (e.g. `model_config = ConfigDict(from_attributes=True)`).
- **Preserve existing docstrings, type annotations, and code styling.**

---

## 2. Database Schema & Migration

- **Automatic schema migration** — when adding/updating SQLAlchemy columns, add a lightweight `ALTER TABLE` step in `init_db()` (`backend/database.py`) so existing DBs migrate without data loss.
- **ORM first, no blind raw SQL** — query via SQLAlchemy ORM models or inspect `backend/database.py`, never guess raw column names.
- Canonical schema reference lives in `DESIGN_DIAGRAMS.md` and `PROJECT_INDEX.md`.

---

## 3. Code Execution & Automated Testing

- Use the activated venv: `./.venv/bin/python`, `./.venv/bin/pytest`.
- **Run tests judiciously** — only the modules covering the affected code (e.g. `./.venv/bin/pytest tests/test_match_classifier.py tests/test_match_scoring.py`). Escalate to the full suite only for cross-cutting, regression-prone changes (`database.py`, `config.py`, auth/CORS, schema migrations) or when blast radius is unbounded. State which tests ran and why.
- **Database isolation** — use the `db_session` fixture from `conftest.py`.
- **No live network/browser in unit tests** — rely on `prevent_real_browser_spawns` and mocked responses.

---

## 4. Token Optimization & LLM Conventions

- **Discovery never pre-generates heavy materials** — only compute match scores; generate cover letters / cold outreach / tailored points on-demand.
- **Zero-token semantic dismissal** — always run `check_semantic_dismissal` before match prompts on new listings.
- **Deterministic-first scoring** — use the deterministic classifier fast path first, with a bounded LLM fallback only for signal-less JDs. Non-JD discovery artifacts are marked unscored (no LLM). See [`docs/BULK_MATCH_SCORING.md`](docs/BULK_MATCH_SCORING.md).
- **LinkedIn saved jobs full-fidelity** — ingest active saved jobs as-is with `Shortlisted` status, bypassing negative filters.

---

## 5. Privacy & Opt-in Guardrails

- **Server-persisted preferences** — discovery reads `UserPreference`; manual *and* scheduled runners must use it (resume-derived data only as last resort).
- **Opt-in feature flags** (`UserPreference.features_json`) — `linkedin_sync`, `gmail_sync`, `jobspy_google` default **off**; per-portal ATS flags must be explicitly enabled for unofficial endpoints. Disabled channels are hidden in the UI and skipped server-side (`force=True` bypasses for a single manual run).
- **Employer blocklist** (`excluded_companies`, default `amazon`) — matching companies are never scanned.
- **Gmail opt-in/privacy-scoped** — BYOK Gmail API with narrow query, metadata+snippet only, page caps, `messageId` dedup; **never enumerate the full mailbox**.
- **Single-owner browser profile** — `BrowserProfileBusy` is raised rather than deleting live locks; background tasks skip when the profile is in use.
- **Prompt-injection boundary** — external text wrapped via `wrap_untrusted`; generated text via `sanitize_generated_text`; the LLM never submits applications (human confirms via `submission_confirmed`).

---

## 6. Where Things Live

| Need to change…                                           | File                                                                                   |
| :-------------------------------------------------------- | :------------------------------------------------------------------------------------- |
| API routes, CORS, auth, task endpoints                    | `backend/main.py`                                                                      |
| LLM providers, prompts, routing, embeddings, caching      | `backend/ai_helper.py`                                                                 |
| Queue concurrency/pacing                                  | `backend/llm_queue.py`                                                                 |
| ORM models / migrations                                   | `backend/database.py`                                                                  |
| Secrets, encryption, provider/Gmail config                | `backend/config.py`                                                                    |
| Scraping, ghost detection, geo/URL helpers                | `backend/scraper.py`, `backend/geo.py`                                                 |
| Browser automation, LinkedIn/Gmail sync, autofill overlay | `backend/playwright_app.py`                                                            |
| Resume/PDF/JD parsing                                     | `backend/parser.py`                                                                    |
| Background tasks / SSE                                    | `backend/task_engine.py`                                                               |
| Periodic scheduler                                        | `backend/scheduler.py`                                                                 |
| Logging / telemetry                                       | `backend/observability.py`, `backend/database.py`                                      |
| Structured match analysis & interview prep                | `backend/main.py`, `backend/ai_helper.py`                                              |
| Copilot dialogue + action dispatch                        | `backend/chat_agent.py`                                                                |
| SPA dashboard                                             | `frontend/app.js`, `index.html`, `styles.css`                                          |
| Extension runtime / autofill / side panel                 | `extension/js/runtime-adapter.js`, `content/ats-autofill.js`, `sidepanel/sidepanel.js` |

Full file-by-file index with code examples: [`PROJECT_INDEX.md`](PROJECT_INDEX.md).

---

## 7. Reference Docs

- **Dependency inventory** — `requirements.txt` (pinned) and `AGENTS.md` §8.
- **Detailed database schema** — `DESIGN_DIAGRAMS.md`, `PROJECT_INDEX.md`.
- **Performance optimization strategies** — `AGENTS.md` §10, `AGENTIC_ARCHITECTURE.md`.
- **Security implementation** — [`docs/SECURITY_IMPLEMENTATION.md`](docs/SECURITY_IMPLEMENTATION.md).
- **Development setup & onboarding** — `README.md` Quick Start, `CONTRIBUTING.md`.
- **Prompt & architecture** — `AGENTIC_ARCHITECTURE.md`, `DESIGN_DIAGRAMS.md`.
