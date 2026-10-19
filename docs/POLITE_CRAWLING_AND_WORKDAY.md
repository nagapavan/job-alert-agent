# Design Note — Polite-Crawling Provision (robots/AI files + pacing)

**Date:** 2026-10-18
**Status:** ✅ implemented (2026-10-18)
**Scope:** the config-driven ATS engine (`backend/ats/engine.py`) and every HTML/JSON discovery path
that shares its helpers. First consumer: the Workday `post_json` shape.

> **Sign-off recorded (2026-10-18):** default pacing 0.5 s/host ✅; AI-use files advisory ✅;
> implement Workday now (flag stays off) ✅; UI knob design-now/implement-later ✅.

## 0. What shipped

| Piece | Where |
| :--- | :--- |
| Pacing config (`SCRAPE_PACING_SECONDS`, `_JITTER`, `MAX_REQUESTS_PER_HOST_PER_RUN`, `HOST_COOLDOWN_SECONDS`) | `backend/config.py` |
| `HostPacer` + `build_pacer` (per-host delay, jitter, per-run ceiling, 429/503 cooldown, `Retry-After`) | `backend/ats/pacing.py` (new) |
| AI-use-file gate (`llms.txt`/`ai.txt`/`.well-known/ai.txt`) + `may_fetch` | `backend/scraper.py` (`ai_use_files_allowed`, `_fetch_ai_use_file`, `may_fetch`) |
| Every engine request routed through `_http()` (permission gate + pacing + rate-limit back-off) | `backend/ats/engine.py` |
| Multi-placeholder templating (`{host}`/`{tenant}`/`{site}`/`{slug}`/`{base}`/`{id}`) + dict resolvers | `backend/ats/engine.py` (`_resolve_context`, `_format_template`) |
| `post_json` shape + `WorkdayScraper` variant | `backend/ats/engine.py` |
| `workday_board` slug resolver | `backend/scraper.py` |
| Workday config entry (`post_json`, `pacing_seconds: 1.0`, `risk_tier: opt_in`) | `data/ats_portals.json` |
| `scrape_workday_jobs` → thin wrapper; dispatch loop now includes Workday | `backend/scraper.py`, `backend/main.py` |
| Tests | `tests/test_ats_pacing.py` (9), +Workday engine tests, +AI-file gate tests |

Verified live (read-only, 1 page, 1.0 s pacing) against NVIDIA's board: 2 jobs returned with
refined locations, full descriptions and correct public URLs. Full suite: **191 passed**.


---

## 1. Motivation

Two commitments, both from the user:

1. **Respect machine-readable permission files.** Check `robots.txt` — and, where present, emerging
   *AI-use* files (`llms.txt`, `ai.txt`, `.well-known/ai.txt`) — and honour them where possible.
   Do not silently ignore a host's stated wishes.
2. **Never overload a host.** Even with no explicit restriction, we must not burst-request a site
   into unavailability. Pacing must be a **configurable** provision (env first), designed so it can
   later be surfaced in the SPA as a user-tunable "request delay" per channel.

This is a *cross-cutting* concern: it applies to the ATS engine, the custom HTML scraper, Google
Jobs/DDG, Gmail status probes, and (already handled separately) LinkedIn. It belongs in one place,
not copy-pasted per portal.

### What we already have (reuse, don't reinvent)

- `backend/config.py` → `AGENT_USER_AGENT` (self-identifying UA), `RESPECT_ROBOTS`
  (`JOB_ALERT_AGENT_RESPECT_ROBOTS`, default true), `LINKEDIN_AUTOMATION_PACING_SECONDS`.
- `backend/scraper.py` → `_fetch_robots()` / `robots_allowed(url, user_agent)` — fail-open,
  per-origin cached via `robotparser.RobotFileParser`.
- Live check (2026-10-18): Workday tenants (`nvidia.wd5`, `salesforce.wd12`, `workday.wd5`) return
  **200 for `robots.txt`** and **404 for `llms.txt` / `ai.txt` / `.well-known/ai.txt`**. Their
  robots files `Allow` the career-site paths and `Disallow` only `/refreshFacet/`,
  `/talentcommunity/`, etc.

Missing today: (a) an **AI-file check**, (b) **pacing** for ATS/HTML discovery. The LLM queue already
paces itself; scrapers do not.

---

## 2. Design

### 2.1 New config (`backend/config.py`)

```python
# Inter-request pacing for discovery scrapers (ATS + HTML). Seconds between requests to the SAME
# host; 0 disables. Applied with 0-50% jitter so bursts don't align. The per-portal value in
# data/ats_portals.json overrides this default. Designed to become a UI-tunable knob later.
SCRAPE_PACING_SECONDS = float(os.environ.get("SCRAPE_PACING_SECONDS", "0.5"))
SCRAPE_PACING_JITTER = float(os.environ.get("SCRAPE_PACING_JITTER", "0.5"))   # fraction, 0..1
SCRAPE_MAX_REQUESTS_PER_HOST_PER_RUN = int(os.environ.get("SCRAPE_MAX_REQUESTS_PER_HOST_PER_RUN", "60"))

# Machine-readable AI-use files, checked (fail-open) in addition to robots.txt for HTML scraping.
# Absent files are simply skipped. Set to "false" to check robots.txt only.
RESPECT_AI_USE_FILES = os.environ.get("JOB_ALERT_AGENT_RESPECT_AI_FILES", "true").lower() in ("true", "1", "yes")
```

Defaults rationale: **0.5 s/host** is deliberately slow enough to be visibly polite on a
sub-second ATS board, and small enough not to make a 5-page scan feel broken. The
**60-requests/host/run** cap is a hard ceiling independent of pacing, so a pathological board
(e.g. thousands of postings) can never turn into an accidental flood.

### 2.2 Shared politeness helper (new, in `backend/ats/pacing.py`)

One small module the engine and `scraper.py` both use:

```python
class HostPacer:
    """Per-host inter-request delay + per-run request ceiling. Thread-safe."""
    def __init__(self, default_delay: float, jitter: float, max_requests: int): ...
    def wait(self, url: str) -> None: ...          # sleeps (delay * (1 + jitter*U)) if needed
    def allow(self, url: str) -> bool: ...          # False once a host hits the per-run ceiling
    def note_failure(self, url: str) -> None: ...   # 429/503 -> back off this host hard
```

Semantics:
- **Delay is per host**, not per portal, so a company whose board is hit by two portals still sees
  evenly spaced requests.
- **Honour `Retry-After`** on 429/503 and set a temporary host cooldown (e.g. 3× delay, min 30 s).
- **Per-run ceiling** returns `allow() == False`; the caller stops that host for the run and logs
  it (partial results, never a stall).
- No global lock across hosts (different hosts stay parallel); lock is per host.

### 2.3 Permission gate (extend `scraper.robots_allowed`)

Rename intent to a single `may_fetch(url)` that checks, in order:
1. `RESPECT_ROBOTS` → `robots_allowed(url)`.
2. `RESPECT_AI_USE_FILES` → fetch `llms.txt`, then `ai.txt`, then `.well-known/ai.txt`
   (first that exists wins), parse it for a *disallow* signal, cache per origin.
   **Fail-open** on any error, matching robots behaviour.

AI-file parsing stays deliberately simple: look for explicit deny directives (a `Disallow:`-style
line, `User-agent: *` + `Disallow: /`, or a prose "no training/crawling" marker) → treat as
disallow; anything unparseable → allow. We do **not** attempt to be clever with unspecified formats.

> Note: these AI files are not standardised. Treating them as **advisory** (honour an explicit deny,
> otherwise proceed) is the honest position; the code comment will say so.

### 2.4 `PortalConfig` additions (`backend/ats/engine.py`)

```python
pacing_seconds: Optional[float] = None      # per-portal override of SCRAPE_PACING_SECONDS
respect_robots: bool = True                 # opt a portal out only with justification
// (existing) timeout, max_pages, page_size ...
```

`run_portal()` threads a shared `HostPacer` (created once per scrape call in `main.py`) into the
shape scrapers. Every `requests.get/post` inside the engine goes through
`pacer.wait(url)` + `pacer.allow(url)` first. 409/429/503 → `pacer.note_failure(url)`.

### 2.5 Per-portal back-off on errors

Any non-200 that is *not* 404 (i.e. rate-limit/outage) already breaks the pagination loop. We add:
`pacer.note_failure()` so a single bad host does not get retried on the next page/portal in the
same run.

### 2.6 UI surface (later, not this change)

`SCRAPE_PACING_SECONDS` / `SCRAPE_MAX_REQUESTS_PER_HOST_PER_RUN` are chosen so they can be exposed
in **Settings → Performance** as a numeric "Delay between requests (seconds)" per channel
(ATS / Google Jobs / Custom HTML), persisted in `UserPreference` alongside existing settings. The env
values seed the default; the DB value wins at runtime. **Design now, implement later** — but the
config layer is shaped so no refactor is needed when we do it.

---

## 3. Workday specifics (first consumer)

Live behaviour verified 2026-10-18 (NVIDIA/Salesforce/Workday tenants):

| Surface | Auth | Result |
| :--- | :--- | :--- |
| `POST /wday/cxs/{tenant}/{site}/jobs` | none | 200 JSON (`total`, `jobPostings`) |
| `GET /wday/cxs/{tenant}/{site}{externalPath}` | none | 200 JSON (`jobPostingInfo.jobDescription`) |
| Public job page | none | 200 HTML |
| `robots.txt` | — | 200, **Allows** career-site paths, disallows `/refreshFacet/` etc. |
| `llms.txt` / `ai.txt` | — | 404 (none today) |

**Access:** positions are public, no login — Workday's docs state external career sites are "made
publicly available for job seekers". The blocker has never been auth; it is resolving
`{tenant}/{shard}/{site}`, which is per-employer.

**ToS:** Workday's corporate Site Terms at `workday.com/legal/site-terms.html` explicitly ban scraping
and unauthorised third-party applications, **but** they scope to `www.workday.com`, pages referencing
those terms, the Community, and "the Workday APIs". Customer-hosted `*.myworkdayjobs.com` boards are
not clearly in scope; the operative control there is the tenant's `robots.txt` (permissive). There is
**no public job-seeker API**; the per-tenant OAuth REST/SOAP APIs are company-scoped and unusable for
third-party discovery. US precedent (*hiQ v. LinkedIn*, 9th Cir.) means login-free public scraping is
generally not a **CFAA** issue, but ToS remains a **contract** matter and JD text carries **copyright**
risk.

**Therefore Workday stays `risk_tier: opt_in`** (flag off by default) + consent ack + robots/pacing
gate, and is **read-only** (no auto-submit; the human `submission_confirmed` gate already enforces
this).

### Proposed config entry

```json
{
  "id": "workday",
  "label": "Workday Portal",
  "shape": "post_json",
  "slug_from": "workday_board",       // parse {tenant, shard, site, host} from Company.careers_url
  "list_url": "https://{host}/wday/cxs/{tenant}/{site}/jobs",
  "method": "POST",
  "body": {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": ""},
  "response_path": "jobPostings",
  "title_path": "title",
  "location_path": "locationsText",
  "url_path": "externalPath",         // joined to https://{host}{externalPath} by the variant
  "url_prefix": "https://{host}",
  "detail": {"url": "https://{host}/wday/cxs/{tenant}/{site}{externalPath}",
             "desc_paths": ["jobPostingInfo.jobDescription"], "html": true},
  "pacing_seconds": 1.0,              // be extra gentle on Workday tenants
  "risk_tier": "opt_in",
  "default_flag": "ats_workday",
  "enabled_by_default": false
}
```

New pieces this requires (all small, all reusable):
- **`post_json` shape** — list via `POST` with a JSON body + `limit`/`offset` pagination + total
  from `response_path`'s sibling (`total`). The engine has no POST shape yet; Workday is the
  justification (≥2 portals would use it if we later add more enterprise boards).
- **`workday_board` slug resolver** — reuses the existing `_parse_workday_board()` to yield
  `tenant/shard/site/host` from a board URL; returns `""` when the URL is not a Workday board.
  **Never infer tenants from a company name.**
- **Multi-placeholder templating** — `list_url`/`detail.url` need `{host}`, `{tenant}`, `{site}`,
  not just `{slug}`/`{base}`/`{id}`. Extend the formatter to pass the whole resolver context.

---

## 4. Test plan (TDD)

`tests/test_ats_pacing.py` (new):
- `HostPacer.wait` sleeps ≥ delay between two calls to the *same* host and not across hosts (freeze
  time / inject a fake sleep).
- Per-run ceiling: after N calls to a host, `allow()` is False and the scraper stops that host.
- 429/503 → `note_failure` sets a cooldown; next `allow()` within the window is False.

`tests/test_ats_engine.py` (extend):
- Portal-level `pacing_seconds` override is honoured.
- A host hitting the ceiling still returns the jobs collected so far (partial, no exception).

`tests/test_scraper.py` (extend):
- `may_fetch` honours an explicit `llms.txt`/`ai.txt` deny; absent files → allowed (fail-open).
- Robots `Disallow` on a career path → discovery for that URL is skipped.

All network mocked (`prevent_real_browser_spawns`, `requests` patched). No live calls.

---

## 5. Rollout

1. **Pacing config + `HostPacer` + tests** (no behaviour change when delay=0) — mergeable alone.
2. **AI-file gate + `may_fetch` + tests** — small, independent.
3. **Workday `post_json` shape + resolver + templating + config entry** (flag still off) + tests.
4. *(Later)* UI "request delay" knob in Settings.

Each step runs: `./.venv/bin/pytest -q tests/test_ats_engine.py tests/test_ats_pacing.py tests/test_scraper.py tests/test_api.py`.

---

## 6. Open questions for sign-off

1. **Default pacing** — 0.5 s/host (above) or slower? Workday entry proposes 1.0 s.
2. **AI-file semantics** — treat as advisory (honour explicit deny, else proceed) — agreed?
3. **Workday scope** — do we implement the `post_json` shape now (flag off), or leave the existing
   inline `scrape_workday_jobs` until a target employer needs it?
4. **UI knob** — confirm "design now, implement later" is acceptable.

**All four resolved 2026-10-18** — see the sign-off note at the top; implementation is complete.
