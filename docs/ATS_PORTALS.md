# ATS Portal Support & Requisition Probing — Design Specification

**Status:** Living document. Sections marked ✅ are implemented; 🔜 is the agreed next step;
🗺️ is the roadmap.

**Scope:** how the agent discovers jobs from Applicant Tracking System (ATS) public feeds, how
candidate filters are applied, and how it verifies that a tracked requisition is still open.

**Related docs:** [`../DESIGN_DIAGRAMS.md`](../DESIGN_DIAGRAMS.md) (DFD 1 discovery, DFD 6
external sync), [`../AGENTS.md`](../AGENTS.md) §9–§11 (schema, performance, security),
[`../PROJECT_INDEX.md`](../PROJECT_INDEX.md) (file responsibilities).

---

## 1. Goals & non-goals

**Goals**

1. Discover jobs from an employer's own ATS feed (source of truth) without HTML scraping where a
   public API exists.
2. Apply the candidate's title / location / city / work-mode filters consistently — the same
   semantics in the scraper's early pre-filter and the endpoint's final matcher.
3. Keep the application tracker honest: automatically flag tracked requisitions that have closed.
4. Degrade gracefully when a portal is unreachable or changes shape.

**Non-goals**

- No candidate-side access to employer-authenticated ATS APIs (internal/draft/closed listings,
  webhooks) — those require the employer's API key and are out of scope.
- No automatic application submission. The agent prepares materials; a human confirms.
- No scraping of rendered HTML when a stable public JSON/XML feed exists (custom portals remain
  the fallback).

---

## 2. Current support matrix (implemented)

| Capability | Greenhouse | Lever | Ashby | SmartRecruiters | Workable | Uber | Custom | Workday |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| List/discovery scan | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ (needs board URL) |
| Portal/slug resolution (`extract_portal_info_from_job_url`) | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ | ✅ |
| Requisition-active probe | ✅ `probe_greenhouse_job_active` | ✅ `probe_lever_job_active` | 🗺️ via generic probe | ✅ `probe_smartrecruiters_job_active` | 🗺️ | n/a | n/a | 🗺️ |
| Tokenized application-status page | ✅ `check_greenhouse_application_status` | ❌ (not public) | ❌ (not public) | ❌ | ❌ | ❌ | ❌ | ❌ |
| External-sync source (`POST /api/applications/sync-external`) | ✅ | ✅ | 🗺️ | ✅ | 🗺️ | ❌ | ❌ | 🗺️ |

Scrapers live in `backend/scraper.py`; discovery is dispatched from
`backend/main.py::trigger_jobs_scrape` (`POST /api/jobs/scrape`); external status sync is
`backend/main.py::sync_external_applications` (`POST /api/applications/sync-external`).

### 2.1 Risk tiers, opt-in & consent

| Tier | Portals | Default | Rationale |
| :--- | :--- | :--- | :--- |
| **Low** (public, documented APIs) | Greenhouse, Lever, Ashby | **on** | Public/no-auth; Lever explicitly permits scraping published postings |
| **Medium** (unofficial/ambiguous) | SmartRecruiters, Workable, Workday, Uber, custom HTML | **off — opt-in with consent** | Unofficial endpoints, or ToS that restrict automated agents |
| **Session-driven** | LinkedIn, Gmail | **off** | Requires the user's logged-in session / inbox; ToS + account risk |
| **Blocklisted** | `UserPreference.excluded_companies` (default `amazon`) | **never scanned** | Amazon Conditions of Use + Agent Policy restrict automated/agent access |

Control surfaces: `UserPreference.features_json` (per-portal flags) and
`UserPreference.excluded_companies`, both exposed via `GET/PUT /api/preferences` and the SPA
**Settings → Discovery Features** panel. Gating is enforced **server-side** in
`trigger_jobs_scrape` and `sync_external_applications` — hiding the UI alone is not sufficient.

> **LinkedIn:** there is **no official, self-serve jobs API/SDK for candidates**. LinkedIn's
> official APIs (Job Posting, Apply Connect, Recruiter System Connect) are partner-only and
> *publish* jobs; they do not expose job search. Third-party "LinkedIn job APIs" are scraping /
> account-automation resellers. LinkedIn direct scan is therefore **off by default**; when
> disabled, LinkedIn-sourced roles still flow in via opt-in **Gmail job-alert emails** and
> **Google for Jobs**.

> **Not legal advice.** You are responsible for complying with each site's terms of service. The
> agent performs discovery only and never submits applications.

---

## 3. Scanning architecture

### 3.1 Dispatch

`trigger_jobs_scrape` iterates configured `Company` rows and, per company, tries portals in order
until one returns jobs:

```
Uber special-case (if name contains "uber")
  -> Greenhouse -> Lever -> Ashby -> SmartRecruiters -> Workable -> Workday -> custom HTML
```

Each portal scraper is independent and returns a list of job dicts. A company is not retried
against a later portal once an earlier one yields results.

### 3.2 Scraper contract

Every ATS scraper shares one signature so the dispatcher never branches on portal:

```python
def scrape_<portal>_jobs(
    company_name: str,
    title_filters: Optional[List[str]] = None,
    city_filters: Optional[List[str]] = None,
    location_filters: Optional[List[str]] = None,
    work_mode: Optional[str] = None,
    max_matches: int = 35,
    careers_url: Optional[str] = None,
) -> List[Dict[str, str]]:
    ...
```

Returned dict keys (all strings unless noted):

| Key | Meaning | Required |
| :--- | :--- | :---: |
| `title` | Role title | ✅ |
| `url` | Canonical apply URL | ✅ |
| `description` | Plain-text JD (HTML already stripped) | ✅ |
| `location` | Human-readable location | ✅ |
| `source` | e.g. `"Greenhouse Portal"`, `"Lever Portal"`, `"Ashby Portal"` | ✅ |
| `source_type` | `"Direct"` (portal-sourced) or `"LinkedIn"` | ✅ |
| `salary_range` | Optional, when the portal exposes it | ➖ |

### 3.3 Filter contract (important)

Filters are applied **twice** by design — early in the scraper (to avoid fetching descriptions
for rejected rows) and again in the endpoint's final matcher (authoritative):

- **Title:** `backend.scraper.check_title_match(...)` — the *same* function object is imported by
  `backend/main.py` (`check_title_match is main_check`, asserted by a regression test) so the
  early pre-filter can never starve the final matcher.
- **Location:** scrapers use `backend.scraper.is_location_eligible(loc, title, city_filters,
  location_filters, work_mode)`; the endpoint uses `check_location_match(...)` +
  `check_workmode_match(...)`. Both resolve countries via the offline `backend.geo` resolver
  (`geonamescache` + `pycountry`) and are foreign-restriction aware.
- **`max_matches`** caps rows *after* filtering.
- **Server-side filters:** most ATS public feeds return the whole board, so filtering is
  client-side. SmartRecruiters is **paginated server-side** via `limit`/`offset` (bounded to 5
  pages). Its `q`/`country`/`city` narrowing params are deliberately **not** used: they can
  starve expansion-aware title matching (e.g. `staff software engineer` → `Principal Engineer`)
  and global-remote eligibility (a `country=IN` filter would drop a "Remote – Worldwide" role).
  The client-side matchers remain the authoritative filter.

**Rule for new portals:** never silently drop a filter argument. If the API cannot filter
server-side, fetch and filter client-side; add a test proving the filter excludes a
non-matching row (see §8).

---

## 4. Requisition-active probing subsystem

Purpose: an application that is `Applied`/`Shortlisted` may reference a requisition that has since
closed. The probe detects this and transitions the job to `Rejected` with an audit event.

### 4.1 Shared generic probe

`backend.scraper.probe_job_url_active(job_url, timeout=10) -> bool`:

- HTTP **404/410** → `False` (closed).
- HTTP **200** containing any `CLOSED_JOB_MARKERS` phrase → `False`.
- Otherwise → `True`. Network/parse errors are treated as **active** (a transient failure must
  never auto-reject a real application).

This is the fallback for every portal-specific probe and is reusable for portals without a
dedicated endpoint (e.g. Ashby, which only 404s at the job URL).

### 4.2 Greenhouse probe

`probe_greenhouse_job_active(job_url)`:

1. Extract board slug + numeric job id (`/jobs/<id>` or `gh_jid=<id>`).
2. `GET https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{id}` → 200 active / 404–410 closed.
3. Fallback → `probe_job_url_active(job_url)`.

Greenhouse additionally exposes a **tokenized candidate status page**
(`check_greenhouse_application_status(status_url)`), which is unique among the supported portals.

### 4.3 Lever probe

`probe_lever_job_active(job_url)`:

1. Extract `(site_slug, posting_id)` from `/v0/postings/{slug}/{id}`, `jobs.lever.co/{slug}/{id}`,
   or `.../{id}/apply`.
2. `GET https://api.lever.co/v0/postings/{slug}/{id}` → 200 active / 404–410 closed.
3. Fallback → `probe_job_url_active(job_url)`.

Lever's public Postings API only exposes `published` postings; closed/unlisted ones 404. (The
authenticated Lever **v1 API** can list closed/internal postings, but requires the employer's key
— out of scope.) Lever has **no** public tokenized application-status page.

### 4.4 SmartRecruiters probe

`probe_smartrecruiters_job_active(job_url)`:

1. Extract `(company_identifier, posting_id)` from `/v1/companies/{id}/postings/{pid}`,
   `jobs.smartrecruiters.com/{Company}/{pid}-{slug}`, or `careers.smartrecruiters.com/{id}/{pid}`.
2. `GET https://api.smartrecruiters.com/v1/companies/{id}/postings/{pid}` → the detail exposes an
   `active` boolean, so this is a direct open/closed signal (not a 404 heuristic).
3. Falls back to `probe_job_url_active(job_url)` on parse failure or if the tenant requires auth.

### 4.5 State transition

Applied in `sync_external_applications`:

```text
if "greenhouse" in sources or "all" in sources:
    for job in jobs(url ~ boards.greenhouse.io, status in [Applied, Shortlisted]):
        if not probe_greenhouse_job_active(job.url) and job.status != "Rejected":
            job.status = "Rejected"; job.rejected_at = now
            add ApplicationEvent("requisition_closed")

if "lever" in sources or "all" in sources:
    for job in jobs(url ~ jobs.lever.co, status in [Applied, Shortlisted]):
        if not probe_lever_job_active(job.url) and job.status != "Rejected":
            job.status = "Rejected"; job.rejected_at = now
            add ApplicationEvent("requisition_closed")
```

`ExternalSyncRequest.sources` defaults to `["hirist", "greenhouse", "lever"]`; the SPA sends
`["hirist", "greenhouse", "lever"]` (plus `"gmail"` when the opt-in flag is on). `"all"` runs
every branch.

---

## 5. Portal-by-portal design

Legend: **Auth** = needs a key; **Format** = response body; **Filters** = server-side capability.

### 5.1 Implemented

| Portal | List endpoint | Server filters used | Probe |
| :--- | :--- | :--- | :--- |
| Greenhouse | `GET boards-api.greenhouse.io/v1/boards/{slug}/jobs` (+ `/jobs/{id}` detail) | none | single-job 200/404 + tokenized status page |
| Lever | `GET api.lever.co/v0/postings/{site}?mode=json` (EU: `api.eu.lever.co`) | none | single-posting 200/404 |
| Ashby | `GET api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true` | none | 🗺️ generic URL probe |
| SmartRecruiters | `GET api.smartrecruiters.com/v1/companies/{id}/postings` (+ per-posting detail) | `limit`/`offset` pagination only | 🗺️ single-posting `active` |
| Workable | `GET apply.workable.com/api/v1/widget/accounts/{slug}?details=true` | none | 🗺️ |
| Workday | `POST {tenant}.{shard}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs` (+ detail) | `limit`/`offset` in the JSON body | 🗺️ |
| Uber | `POST uber.com/api/loadSearchJobsResults` | none | n/a |
| Custom | `GET {careers_url}` + `<a>` heuristics | n/a | n/a |

### 5.2 Next (🔜)

No committed next item — the agreed backlog is the roadmap below, in the order listed.

### 5.3 Roadmap (🗺️)

| Portal | List endpoint | Auth | Format | Key caveats |
| :--- | :--- | :---: | :--- | :--- |
| Recruitee | `GET {company}.recruitee.com/api/offers/` | none | JSON | published-only; includes `status` |
| Rippling | `GET api.rippling.com/platform/api/ats/v1/board/{company}/jobs` | none | JSON | lowercase slug |
| Breezy HR | `GET {company}.breezy.hr/json` | none | JSON | list omits descriptions |
| Pinpoint | `GET {company}.pinpointhq.com/postings.json` | none | JSON | — |
| BambooHR | `GET {slug}.bamboohr.com/careers/list` | none | JSON | no date/description in list (per-job `/careers/{id}/detail`); missing tenant returns **302**, not 404 |
| Personio | `GET {slug}.jobs.personio.de/search.json` (XML feed also exists) | none | JSON/XML | XML parsing path; redirect-not-404 |
| Teamtailor | Partner/Job Board API (beta) | Public Read key | JSON API | public list requires a key; lower priority |

**Sequencing rationale:** SmartRecruiters and Workable first (highest coverage per unit effort,
JSON, stable), then Workday (big enterprises, higher effort), then the long tail.

---

## 6. Integration points (touch these when adding a portal)

| Concern | Location |
| :--- | :--- |
| Add scraper `scrape_<portal>_jobs(...)` | `backend/scraper.py` |
| Slug extraction | `backend/scraper.py::extract_slug_from_careers_url` |
| Portal/slug recognition for tracked URLs | `backend/scraper.py::extract_portal_info_from_job_url` |
| Dispatch in discovery | `backend/main.py::trigger_jobs_scrape` (~Greenhouse/Lever/Ashby/custom blocks) |
| Requisition probe branch | `backend/main.py::sync_external_applications` (add a `source in [...]` block) |
| Hiring-intent source weight | `backend/main.py::HIRING_INTENT_SOURCE_WEIGHTS` |
| SPA sync sources | `frontend/app.js` (`["hirist", "greenhouse", "lever"]`) |
| Catalog / target companies | `backend/companies_config.json`, `backend/company_catalog.py` |
| Tests | `tests/test_scraper.py`, `tests/test_application_tracking.py` |

---

## 7. Data contracts

**External sync request** (`ExternalSyncRequest`):

```json
{ "sources": ["hirist", "greenhouse", "lever", "gmail", "all"],
  "max_pages": 10, "headless": true }
```

**External sync response** (`ExternalSyncResponse`): `hirist_checked`, `hirist_updated`,
`hirist_ingested`, `emails_checked`, `status_updates`, `opportunities_added`, `details`, `message`.

Closed-requisition transitions emit `ApplicationEvent.event_type == "requisition_closed"`.

---

## 8. Testing strategy

- **Filter forwarding:** mock `requests.get` with a mixed payload and assert a scraper with
  `title_filters`/`location_filters` returns only matching rows
  (`tests/test_scraper.py::test_scrape_lever_jobs_applies_title_and_location_filters`,
  `test_scrape_smartrecruiters_jobs_applies_filters`,
  `test_scrape_workable_jobs_applies_title_and_work_mode_filters`).
- **Filter plumbing:** assert `/api/jobs/scrape` forwards title/location/work-mode to the
  SmartRecruiters/Workable scrapers unchanged
  (`tests/test_api.py::test_trigger_jobs_scrape_forwards_filters_to_new_portals`).
- **Pagination:** assert SmartRecruiters sends `limit`/`offset` on the list call and fetches the
  per-posting detail for the apply URL + description.
- **Probe semantics:** 200 open, 404/410 closed, closed-marker phrase, and network-error-assumes-
  active (`tests/test_application_tracking.py`).
- **Probe URL parsing:** assert the Lever probe calls
  `https://api.lever.co/v0/postings/{slug}/{id}` for `.../{id}/apply` and unparseable URLs fall
  back to the raw-URL probe.
- **Wiring:** patch `backend.main.probe_lever_job_active` and assert a `Shortlisted` Lever job
  becomes `Rejected` with a `requisition_closed` event.
- **Regression:** the early title pre-filter must remain `is` the endpoint's matcher
  (`test_title_prefilter_is_consistent_with_final_matcher`).

Run: `./.venv/bin/pytest -q tests/test_scraper.py tests/test_application_tracking.py`.

---

## 9. Compliance & privacy

- Prefer employer-published public feeds over scraping; respect `robots`/ToS and use a
  descriptive User-Agent with conservative timeouts (existing scrapers use 8–15 s).
- **Self-identifying User-Agent & robots.txt (HTML/index scraping only).** Custom-portal and
  Google-for-Jobs HTML fetches send a self-identifying agent UA
  (`config.AGENT_USER_AGENT`, env `JOB_ALERT_AGENT_USER_AGENT`) and consult the host's
  `robots.txt` via `scraper.robots_allowed()` (fail-open on network errors; disable with
  `JOB_ALERT_AGENT_RESPECT_ROBOTS=false` at your own risk). Public ATS JSON APIs keep a standard
  UA because they are documented, machine-facing endpoints.
  - Note: Google's `robots.txt` disallows `/search`, so the direct Google-for-Jobs HTML fetch is
    skipped when robots is respected. Use the opt-in JobSpy source or the browser-session fetcher
    as alternatives.
- **Legacy repair:** `python -m backend.scripts.repair_linkedin_saved_jobs [--apply]` repairs
  malformed LinkedIn saved-job rows ingested before the collapsed-card parser fix (dry-run by
  default).
- Probes run only for jobs already in the user's tracker (not bulk external enumeration) and only
  for `Applied`/`Shortlisted` rows.
- Probe results are conservative: ambiguity (network error, non-404) resolves to **active**, so
  the agent never auto-rejects on weak evidence.
- No credentials, OAuth tokens, or employer-side API keys are used for scanning.

---

## 10. Open questions

1. Should the generic probe also be wired for Ashby/other ATS URLs in external sync (risk:
   JS-shell pages return 200 for closed roles, giving false "active")?
2. Rate-limit/back-off policy for the long tail of small portals.
3. Workday: enrich board discovery (some tenants expose multiple sites) and confirm the CXS
   detail path across shards/locales.
