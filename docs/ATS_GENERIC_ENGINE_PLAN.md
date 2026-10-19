# Config-Driven Generic ATS Portal Engine — Plan

**Status:** Proposed (not started). Related: `reasoning/2026-10-18-doc-drift-and-backlog-triage.md` (Item 1).

## Goal

Stop hand-writing one scraper per ATS portal. Classify portals by **feed shape**, implement **one
small scraper per shape** (a class hierarchy), and drive per-portal differences from
`data/ats_portals.json`. Adding a portal of an existing shape becomes a JSON entry; a new *shape*
becomes one class.

Non-goals: changing scoring, ghost-job logic, or the ingestion path. Behaviour of the existing
default portals (Greenhouse/Lever/Ashby) must be byte-for-byte preserved (same filters, same
`max_matches=35`, same `source` labels).

---

## 1. Portal categorization (by feed shape, verified against current code)

| Shape | Portals | List fetch | Response root | Pagination | Detail needed? |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **A. Simple JSON list** | Lever, Ashby, Workable | `GET`, no params | list (`lever`) / `jobs` / `jobs` | none | no |
| **B. Paginated JSON** | Greenhouse, SmartRecruiters | `GET` | `jobs` / `content` | none / `limit`+`offset` | **yes** (per-posting detail) |
| **C. POST search** | Workday | `POST` JSON body | `jobPostings` | `limit`+`offset` in body | **yes** |
| **D. Bespoke** | Uber, Custom HTML | internal API / scrape `<a>` | n/a | n/a | yes |

Key observation: **every portal already funnels through the same four shared steps** —
slug resolution → list fetch → normalize `{title, location, url, description}` from portal-specific
JSON paths → apply the shared `check_title_match` / `check_location_match` / `check_workmode_match`
gate → optional detail fetch. Only the *paths*, *pagination*, and *fetch method* differ. That is
exactly what a config + shape-class split can absorb.

Greenhouse/Lever/Ashby are the high-traffic defaults and have performance-sensitive early filtering
(metadata-only fetch, then detail only for matches). They can run through the engine too, but a
per-shape class must preserve their early-filter ordering so bandwidth behaviour doesn't regress.

---

## 2. Class abstraction

```python
# backend/ats/engine.py  (new package)

@dataclass(frozen=True)
class PortalConfig:
    id: str                      # "smartrecruiters"
    label: str                   # "SmartRecruiters Portal"  -> jobs[].source
    shape: str                   # "simple_json" | "paginated_json" | "post_json" | "bespoke"
    list_url: str                # format template, e.g. "https://api.lever.co/v0/postings/{slug}?mode=json"
    slug_from: str               # "careers_url" | "company_name" | "board_url"
    method: str = "GET"          # GET | POST
    body: dict | None = None     # POST template (workday)
    response_path: str = ""      # dotted path to the list, e.g. "jobs" | "content" | ""
    title_path: str = "title"
    location_path: str = "location"         # dotted; uses _first_of for candidates
    url_path: str = "absolute_url"
    description_path: str = ""
    location_join: list[str] = field(default_factory=list)  # e.g. ["city","region","country"]
    detail: dict | None = None   # {"url": tmpl, "map": {desc paths...}}
    transform: str | None = None # dotted path to a registered hook (XML/keyed APIs)
    risk_tier: str = "public"    # "public" | "opt_in"
    default_flag: str | None = None         # e.g. "ats_workable"
    max_pages: int = 5
    page_size: int = 100


class ShapeScraper(ABC):
    """One subclass per feed shape. Owns fetch + pagination + field mapping."""
    def fetch_list(self, cfg, slug, board) -> list[dict]: ...
    def map_item(self, cfg, item) -> dict | None: ...     # -> {title, location, url, description}
    def fetch_detail(self, cfg, item, ctx) -> str: ...    # optional

    def scrape(self, cfg, *, company_name, title_filters, city_filters,
               location_filters, work_mode, max_matches, careers_url) -> list[dict]:
        # template method: resolve slug -> fetch_list -> for each item:
        #   map_item -> shared filter gate -> (detail) -> emit canonical dict -> cap at max_matches
```

Concrete shapes: `SimpleJsonScraper`, `PaginatedJsonScraper`, `PostJsonScraper`. Uber/Custom remain
their own functions behind a `BespokeScraper` adapter (kept as-is).

**Canonical job dict** (unchanged, so ingestion is untouched):
`{"title", "url", "description", "location", "source": cfg.label, "source_type": "Direct"}` — with
the existing per-portal description fallback `f"{title} opening at {company_name} ({loc_str})."`.

**Where the shared filter gate lives:** one helper `apply_shared_filters(title, loc_str, ctx)` in the
engine, calling `check_title_match` + `check_location_match` + `check_workmode_match` (imported from
`backend.preference_filter`). This removes the ~6 duplicated blocks currently copy-pasted in
`scraper.py`.

**Transform hook registry** (for shapes that don't map cleanly): `register_transform("personio_xml")`,
`register_transform("bamboohr_detail")`, `register_transform("teamtailor_keyed")`. Only needed when
a portal is actually added.

---

## 3. Data file — `data/ats_portals.json`

```json
{
  "lever": {
    "shape": "simple_json", "label": "Lever Portal", "slug_from": "careers_url",
    "list_url": "https://api.lever.co/v0/postings/{slug}?mode=json",
    "response_path": "", "title_path": "text",
    "location_path": "categories.location", "url_path": "hostedUrl",
    "alternate_url_path": "applyUrl", "description_path": "descriptionPlain",
    "risk_tier": "public", "default_flag": "ats_lever", "enabled_by_default": true
  },
  "smartrecruiters": {
    "shape": "paginated_json", "label": "SmartRecruiters Portal", "slug_from": "careers_url",
    "list_url": "https://api.smartrecruiters.com/v1/companies/{slug}/postings",
    "response_path": "content", "title_path": "name",
    "location_join": ["location.city", "location.region", "location.country"],
    "url_path": "ref", "page_size": 100, "max_pages": 5,
    "detail": { "url": "{base}/{id}", "desc_paths": ["jobAd.sections.jobDescription.text",
                "jobAd.sections.qualifications.text"] },
    "risk_tier": "opt_in", "default_flag": "ats_smartrecruiters"
  }
}
```

Backward-compat: `enabled_by_default` + `risk_tier` reproduce today's on/off split (public =
default-on: Greenhouse/Lever/Ashby; opt_in = off: SmartRecruiters/Workable/Workday/Uber/custom).

---

## 4. Dispatch change (`backend/main.py::_execute_ats_scrape`)

Replace the 7 hand-written `if _flags.get("ats_*"): ...` blocks with:

```python
from backend.ats import load_portal_configs, run_portal
for cfg in load_portal_configs():
    if not _ats_on: break
    if cfg.default_flag and not _flags.get(cfg.default_flag): continue
    discovered_jobs.extend(run_portal(cfg, comp, filters...))
```

`run_portal` looks up the shape class and calls `.scrape(...)`. Slug resolution moves behind
`slug_from`; `extract_slug_from_careers_url` is extended with a metadata-driven variant rather than
the current `if ats == "greenhouse": ... elif ...` chain (kept as fallback).

---

## 5. Migration order (TDD, incremental — no big-bang)

1. **Engine + Lever** — ✅ **DONE (2026-10-18).** New package `backend/ats/` (`engine.py`: `PortalConfig`,
   `ShapeScraper`, `SimpleJsonScraper`, `PaginatedJsonScraper`, `load_portal_configs`, `run_portal`),
   config `data/ats_portals.json` (Lever entry), and `scrape_lever_jobs` reduced to a thin wrapper
   delegating to the engine. Slug resolution is reused from `extract_slug_from_careers_url` via a
   registered resolver. New tests: `tests/test_ats_engine.py`; existing `tests/test_scraper.py` +
   `tests/test_api.py` pass unchanged.
2. **Ashby + Workable** — ✅ **DONE (2026-10-18).** `AshbyScraper` (multi-location OR gate, secondary
   locations stashed on the row) and `WorkableScraper` (`(Remote)` suffix + `full_description`
   fallback) variants registered in `_VARIANT_REGISTRY`; config entries added.
3. **Greenhouse + SmartRecruiters** — ✅ **DONE (2026-10-18).** Greenhouse delegates to the
   `paginated_json` / `greenhouse` variant (embedded `content`; per-posting detail only when the
   embedded description is empty). SmartRecruiters delegates to the `paginated_json` /
   `smartrecruiters` variant; its apply URL lives only on the detail payload, so `fetch_detail`
   returns a **dict** `{"description", "url"}` and the base `scrape()` accepts a `str` **or** `dict`
   from `fetch_detail` (dict overrides both description and URL). The shared detail-fetch template
   moved up into `ShapeScraper.fetch_detail` (Greenhouse's `detail.url` needs `{slug}`; the template
   is formatted with `base`/`slug`/`id`).
4. **Workday** — **deferred / re-scoped.** There is no single global Workday feed; each employer has
   its own tenant board (`https://{tenant}.{shard}.myworkdayjobs.com/{locale}/{site}`), so this is a
   *collection of per-employer boards*, not one portal. Re-evaluate as its own `post_json` shape
   where the "portal" is keyed by the tenant/shard/site parsed from `Company.careers_url`. Keep
   `_parse_workday_board` + `_normalize_workday_external_path`.
   → **Now specified:** see [`POLITE_CRAWLING_AND_WORKDAY.md`](POLITE_CRAWLING_AND_WORKDAY.md)
   (robots/AI-file gate + configurable pacing + the `post_json` shape). Awaiting sign-off.
5. **Swap `main.py` dispatch to the loop** — ✅ **DONE (2026-10-18).** `_execute_ats_scrape` now
   loops over `_ATS_PORTALS` (built from `load_portal_configs()`), gating each portal by its
   `default_flag`. Scraper functions are resolved by **attribute name** at dispatch time so tests can
   patch `backend.main.scrape_*`. Uber/Custom/Workday stay bespoke.
6. **Delete the superseded per-portal bodies** — ✅ **DONE (2026-10-18).** All five ATS scrapers are
   now thin wrappers; `backend/scraper.py` went **2363 → 1972 LOC** (−391) and the new engine is
   `backend/ats/engine.py` (563 LOC). Net ≈ −390 LOC in `scraper.py`.

Each step: extend `tests/test_scraper.py` for that shape with a mocked payload + filter assertion,
run `./.venv/bin/pytest -q tests/test_scraper.py tests/test_api.py`, then the affected modules.

---

## 6. Risks & mitigations

| Risk | Mitigation |
| :--- | :--- |
| Behaviour drift in default portals | Wrapper delegation + existing tests as the contract; compare output dicts on a fixture payload before/after. |
| Field mapping not expressive enough | `transform` hook registry; escalate only the non-conforming portal to a bespoke class. |
| ToS/opt-in regressions | `risk_tier` + `default_flag` reproduce the current gating; consent flow (`CONSENT_REQUIRED_FEATURES`) untouched. |
| Over-abstraction | Bespoke stays bespoke (Uber/Custom/Workday if needed); add a shape only when ≥2 portals share it. |

## 7. Expected outcome

- ~**−300 to −500 LOC** in `backend/scraper.py` + `backend/main.py`.
- Adding a long-tail portal (Recruitee/Rippling/Breezy/…) = one `data/ats_portals.json` entry.
- One place to fix pagination/filtering bugs instead of six.
