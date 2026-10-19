# Documentation Index

Quick map of the project's documentation and what each covers.

## Core documents (repository root)

| Document | Path | What it covers |
| :--- | :--- | :--- |
| Master System Design & DFDs | [`DESIGN_DIAGRAMS.md`](../DESIGN_DIAGRAMS.md) | 20 sections & 9 Data Flow Diagrams (DFD 1–9): system topology, dynamic model routing, LLM priority queue, background task engine, hiring radar, multi-platform sync, PDF parsing, tech-stack matrix, C4 architectural models, plus **text-mode architecture, deployment & module dependency maps** (§19). |
| Agentic AI Architecture | [`AGENTIC_ARCHITECTURE.md`](../AGENTIC_ARCHITECTURE.md) | The 5 core pillars of the agentic execution harness, autonomy spectrum, sandboxing, safety guardrails, the **AI/ML/LLM architecture stack** (model topology, task tiering, the deterministic+LLM+verifier reasoning model, RAG/vector memory) and **prompt-engineering details** (§5–6). |
| Developer & Agent Guidelines | [`AGENTS.md`](../AGENTS.md) | 5-phase engineering lifecycle, canonical SQLAlchemy schema references, zero-token optimization rules, testing standards, plus **package inventory** (§8), **detailed database schema** (§9), **performance strategies** (§10), **security implementation** (§11), and a **developer onboarding guide** (§12). |
| Project Overview & Quickstart | [`README.md`](../README.md) | Feature directory, tech stack, setup commands, environment configuration, Gmail API setup, responsible-use notes. |
| Project File Index | [`PROJECT_INDEX.md`](../PROJECT_INDEX.md) | File-by-file responsibilities for locating where changes belong, plus **real code examples for every major component** (generation, queue, DB, scraper, Playwright, tasks, scheduler, observability, SPA, extension). |
| Chrome Extension Docs | [`extension/README.md`](../extension/README.md) | Manifest V3 architecture (side panel, content-script DOM telemetry, safe-mode autofill). |
| Store Listing & Privacy | [`CHROMEWEBSTORE.md`](../CHROMEWEBSTORE.md) | Chrome Web Store metadata, permission justifications, and privacy disclosures. |

## Topic → document map

Where to look for the deep-dive topics:

| Topic | Document / section |
| :--- | :--- |
| **AI/ML Portfolio Journey & Decision Making** | [`PORTFOLIO_JOURNEY.md`](PORTFOLIO_JOURNEY.md) (narrative arc from monolithic LLM → performance crisis → hybrid classifier breakthrough; why each decision was made, trade-offs analyzed, lessons learned; **for LinkedIn/interviews**) |
| **LLM Model Behavior & Agentic Platform Lessons** | [`LESSONS_LEARNED.md`](LESSONS_LEARNED.md) (Gemini hallucination patterns, Claude over-documentation, DeepSeek reliability, working with verbose models, context management) |
| **Fast classifier + on-demand LLM analysis** | [`HYBRID_MATCHER_GUIDE.md`](HYBRID_MATCHER_GUIDE.md) (complete walkthrough: how it works, API reference, architecture, feature extraction, benchmarks, training) |
| **Bulk match scoring (deterministic-first)** | [`BULK_MATCH_SCORING.md`](BULK_MATCH_SCORING.md) (implementation: two-tier routing, artifact guard, counters, testing, performance expectations) |
| Architecture diagrams (Mermaid + text/ASCII) | [`DESIGN_DIAGRAMS.md`](../DESIGN_DIAGRAMS.md) §1–20 (text topology in §19) |
| Code examples for every major component | [`PROJECT_INDEX.md`](../PROJECT_INDEX.md) → "Code Examples — Major Components" |
| Database schema + explanations | [`AGENTS.md`](../AGENTS.md) §5 & §9 |
| AI / ML / LLM architectures | [`AGENTIC_ARCHITECTURE.md`](../AGENTIC_ARCHITECTURE.md) §5 (model topology, tiering, reasoning model, RAG) |
| AI prompt engineering details | [`AGENTIC_ARCHITECTURE.md`](../AGENTIC_ARCHITECTURE.md) §6 |
| Packages / dependencies used | [`AGENTS.md`](../AGENTS.md) §8; [`requirements.txt`](../requirements.txt) |
| Performance optimization strategies | [`AGENTS.md`](../AGENTS.md) §10 |
| Security implementation details | [`AGENTS.md`](../AGENTS.md) §11; [`SECURITY_IMPLEMENTATION.md`](SECURITY_IMPLEMENTATION.md); [`DESIGN_DIAGRAMS.md`](../DESIGN_DIAGRAMS.md) §14 & DFD 5 |
| Developer onboarding & code editing rules | [`README.md`](../README.md) Quick Start; [`CONTRIBUTING.md`](../CONTRIBUTING.md); [`EngineeringAgentRules.md`](../EngineeringAgentRules.md) |
| Agentic reasoning / autonomy / harness | [`AGENTIC_ARCHITECTURE.md`](../AGENTIC_ARCHITECTURE.md) §1–5 |
| ATS portals, filters & requisition probing | [`ATS_PORTALS.md`](ATS_PORTALS.md) |
| Polite crawling (robots/AI files + pacing) & Workday | [`POLITE_CRAWLING_AND_WORKDAY.md`](POLITE_CRAWLING_AND_WORKDAY.md); [`ATS_GENERIC_ENGINE_PLAN.md`](ATS_GENERIC_ENGINE_PLAN.md) |
| Structured match analysis & interview prep | [`PROJECT_INDEX.md`](../PROJECT_INDEX.md); [`DESIGN_DIAGRAMS.md`](../DESIGN_DIAGRAMS.md) §20 |
| One-shot "Sync All" orchestration | [`SYNC_ALL_PLAN.md`](SYNC_ALL_PLAN.md) |
| Log rotation & operation-log retention | [`DESIGN_DIAGRAMS.md`](../DESIGN_DIAGRAMS.md) §1; [`README.md`](../README.md) §Observability |
| No-fabrication guarantees / "not AI-scored" | [`AGENTIC_ARCHITECTURE.md`](../AGENTIC_ARCHITECTURE.md) §5.5 |
| Embedding backends (pluggable, 384-d) | [`docs/README.md`](README.md) §Scoring & matching internals; `backend/embeddings.py` |
| Section-intent / skill taxonomy / residual requirements | [`docs/README.md`](README.md) §Scoring & matching internals; `backend/section_intent.py` |
| Self-learning labels & training readiness | [`docs/README.md`](README.md) §Scoring & matching internals; `GET /api/learning/readiness` |
| Graceful shutdown / drain order | [`AGENTS.md`](../AGENTS.md) §12.11; [`DESIGN_DIAGRAMS.md`](../DESIGN_DIAGRAMS.md) §19.3 |
| Security implementation & encryption | [`SECURITY_IMPLEMENTATION.md`](SECURITY_IMPLEMENTATION.md) (CORS, auth, secrets, encryption, browser isolation, prompt injection, opt-in guardrails, Gmail privacy) |

## Feature / design plans (`docs/`)

| Document | Path | What it covers |
| :--- | :--- | :--- |
| Gmail API Flow (opt-in, BYOK) | [`GMAIL_API.md`](GMAIL_API.md) | Implemented design: loopback OAuth, encrypted token storage, narrow-query metadata-only fetch, dedup, endpoints, UI. |
| ATS Portal Support & Probing | [`ATS_PORTALS.md`](ATS_PORTALS.md) | Design spec: supported portals (Greenhouse/Lever/Ashby/Uber/custom), scraper + filter contracts, requisition-active probing subsystem, and the SmartRecruiters/Workable/Workday roadmap with verified endpoints. |
| One-Shot "Sync All" Orchestrator | [`SYNC_ALL_PLAN.md`](SYNC_ALL_PLAN.md) | Implemented plan: single `POST /api/tasks/sync/all` orchestrating ATS portals, Google Jobs, LinkedIn, and application-status sync with per-stage skip/fail handling. |

## Scoring & matching internals (deterministic path)

The bulk scorer is split across several modules that are not obvious from the endpoint names. This
table says which file owns which concern; `AGENTS.md` §1 and §10.2 describe the surrounding design.

| Module | Responsibility |
| :--- | :--- |
| [`backend/embeddings.py`](../backend/embeddings.py) | Pluggable embedding backends behind one 384-d contract: in-process ONNX `standalone` → OpenAI-compatible `external` server → deterministic `hash` last resort. `resolve_backends` / `embed_texts` / `active_backend_name` / `local_model_cached`. |
| [`backend/section_intent.py`](../backend/section_intent.py) | Single heading/section classifier. `classify_heading(line, domain=jd\|resume\|must_have)`, `jd_section_spans` / `label_at` / `mask_spans`, `is_must_have_heading`, `starts_with_preference_cue`. Replaced three duplicated detectors. |
| [`backend/skill_taxonomy.py`](../backend/skill_taxonomy.py) + [`data/skill_taxonomy.json`](../data/skill_taxonomy.json) | Data-driven skill taxonomy: canonical name → aliases + concepts. `normalize_skill` / `alias_surfaces` / `concepts_for`; unmatched phrases land in `data/skill_taxonomy_candidates.json` via `record_skill_candidate`. |
| [`backend/requirements_extractor.py`](../backend/requirements_extractor.py) | Mines residual JD requirement units the taxonomy did not cover and credits the ones the resume evidences (`extract_residual_requirements`). Unsupported residuals go to the review queue, never into a score. |
| [`backend/resume_index.py`](../backend/resume_index.py) | RAG-lite resume chunking. `chunk_resume` splits parsed JSON into skills/role/project chunks; `index_resume_chunks` persists them into `resume_chunks`; `top_evidence` returns the chunks most similar to a query. |
| [`backend/learning_signals.py`](../backend/learning_signals.py) | Implicit-signal labelling for classifier training. `derive_decision` maps tracked outcomes to `positive`/`strong_positive`/`negative`/`negative_outcome` with `user_initiated` vs `system_generated` provenance; `learning_readiness` reports the trainable counts. |
| [`backend/shutdown.py`](../backend/shutdown.py) | Bounded graceful drain (`run_shutdown`, `is_shutting_down`) invoked from the ASGI shutdown lifecycle. Order: scheduler → task engine → LLM queue → browser-profile locks. See `AGENTS.md` §12.11. |

Supporting data/config:

- `data/embedding_settings.json` — persisted backend/model choice (env vars override).
  Endpoints: `GET/PUT /api/settings/embeddings`, `POST /api/settings/embeddings/download`.
- `data/skill_taxonomy_candidates.json` — review queue for unmatched skill phrases (never scored).
- Changing the embedding **model** does not change the 384-d dimension, but existing rows must be
  re-embedded before similarity comparisons stay meaningful.

## Location filtering & aliases

Scan/radar location filtering is backed by the offline `backend/geo.py` resolver
(`geonamescache` city gazetteer + `pycountry` ISO data), so countries/cities are recognised
from data rather than hardcoded lists. To teach it an abbreviation or alternate name without a
code change, edit [`data/location_aliases.json`](../data/location_aliases.json):

- `country_aliases` — token → ISO alpha-2 (e.g. `"uae": "AE"`).
- `city_aliases` — token → ISO alpha-2 (e.g. `"sf": "US"`, `"bangalore": "IN"`).
- `broad_regions` — tokens that must **not** resolve to a country (e.g. `apac`, `emea`).

Values are ISO 3166-1 alpha-2 codes; keys are lowercased. A missing file falls back to built-in
defaults. The resolver is offline and cached (no network calls).

## Related operational artifacts (not committed)

These live outside version control by design:

- `~/.job-alert-agent/chrome_profile/` — isolated persistent browser profile (session cookies).
- `data/` — user preferences, scheduler config, token stats, Gmail/LLM settings (encrypted secrets). Committed examples: [`data/location_aliases.json`](../data/location_aliases.json) and [`data/company_aliases.example.json`](../data/company_aliases.example.json). User-supplied `data/company_aliases.json` and `backend/companies_config.json` stay local/gitignored (the repo ships `backend/companies_config.example.json` as the generic default).
- `logs/app.log` — structured JSON logs.
