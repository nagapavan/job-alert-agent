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
| Architecture diagrams (Mermaid + text/ASCII) | [`DESIGN_DIAGRAMS.md`](../DESIGN_DIAGRAMS.md) §1–20 (text topology in §19) |
| Code examples for every major component | [`PROJECT_INDEX.md`](../PROJECT_INDEX.md) → "Code Examples — Major Components" |
| Database schema + explanations | [`AGENTS.md`](../AGENTS.md) §5 & §9 |
| AI / ML / LLM architectures | [`AGENTIC_ARCHITECTURE.md`](../AGENTIC_ARCHITECTURE.md) §5 (model topology, tiering, reasoning model, RAG) |
| AI prompt engineering details | [`AGENTIC_ARCHITECTURE.md`](../AGENTIC_ARCHITECTURE.md) §6 |
| Packages / dependencies used | [`AGENTS.md`](../AGENTS.md) §8; [`requirements.txt`](../requirements.txt) |
| Performance optimization strategies | [`AGENTS.md`](../AGENTS.md) §10 |
| Security implementation details | [`AGENTS.md`](../AGENTS.md) §11; [`DESIGN_DIAGRAMS.md`](../DESIGN_DIAGRAMS.md) §14 & DFD 5 |
| Developer onboarding guide | [`AGENTS.md`](../AGENTS.md) §12; [`README.md`](../README.md) Quick Start |
| Agentic reasoning / autonomy / harness | [`AGENTIC_ARCHITECTURE.md`](../AGENTIC_ARCHITECTURE.md) §1–5 |
| ATS portals, filters & requisition probing | [`ATS_PORTALS.md`](ATS_PORTALS.md) |
| Structured match analysis & interview prep | [`AGENTS.md`](../AGENTS.md) §1; [`PROJECT_INDEX.md`](../PROJECT_INDEX.md); [`DESIGN_DIAGRAMS.md`](../DESIGN_DIAGRAMS.md) §20 |
| One-shot "Sync All" orchestration | [`SYNC_ALL_PLAN.md`](SYNC_ALL_PLAN.md) |
| Log rotation & operation-log retention | [`AGENTS.md`](../AGENTS.md) §1 & §12.3 |
| No-fabrication guarantees / "not AI-scored" | [`AGENTIC_ARCHITECTURE.md`](../AGENTIC_ARCHITECTURE.md) §5.5; [`AGENTS.md`](../AGENTS.md) §1 |

## Feature / design plans (`docs/`)

| Document | Path | What it covers |
| :--- | :--- | :--- |
| Gmail API Flow (opt-in, BYOK) | [`GMAIL_API.md`](GMAIL_API.md) | Implemented design: loopback OAuth, encrypted token storage, narrow-query metadata-only fetch, dedup, endpoints, UI. |
| ATS Portal Support & Probing | [`ATS_PORTALS.md`](ATS_PORTALS.md) | Design spec: supported portals (Greenhouse/Lever/Ashby/Uber/custom), scraper + filter contracts, requisition-active probing subsystem, and the SmartRecruiters/Workable/Workday roadmap with verified endpoints. |
| One-Shot "Sync All" Orchestrator | [`SYNC_ALL_PLAN.md`](SYNC_ALL_PLAN.md) | Implemented plan: single `POST /api/tasks/sync/all` orchestrating ATS portals, Google Jobs, LinkedIn, and application-status sync with per-stage skip/fail handling. |
| Antigravity planning artifacts | [`antigravity/`](antigravity/README.md) | Original requirements, implementation plan, task list, and walkthrough from the Antigravity IDE session (provenance). |

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
