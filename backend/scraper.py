import json
import logging
import re
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple  # noqa: UP035
from urllib import robotparser

import requests
from bs4 import BeautifulSoup

from backend.ai_helper import _clean_json_string, generate_text
from backend.config import AGENT_USER_AGENT, RESPECT_ROBOTS
from backend.matchers import check_title_match

# Optional dependency: open-source JobSpy aggregator (Google/Indeed/LinkedIn/...).
# Imported defensively so the app runs without it installed.
try:
    # pyrefly: ignore [missing-import]
    from jobspy import scrape_jobs as _jobspy_scrape_jobs
except Exception:  # pragma: no cover - only when the optional dep is absent
    _jobspy_scrape_jobs = None

logger = logging.getLogger("scraper")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}

# Workday board URLs embed a locale segment (e.g. /en-US/External) that is NOT the site name.
_WORKDAY_LOCALE_RE = re.compile(r"^[a-z]{2}-[A-Za-z]{2}$")

# Self-identifying headers for HTML/index scraping (custom portals, Google for Jobs). Public ATS
# JSON APIs keep the standard browser UA defined above.
AGENT_HEADERS = {**HEADERS, "User-Agent": AGENT_USER_AGENT}

_robots_cache: Dict[str, Any] = {}


def _fetch_robots(robots_url: str, timeout: int = 5) -> str:
    """Fetches a robots.txt body. Returns "" on any failure (fail-open)."""
    try:
        res = requests.get(robots_url, headers=AGENT_HEADERS, timeout=timeout)
        if res.status_code == 200:
            return res.text or ""
    except Exception:
        pass
    return ""


def robots_allowed(url: str, user_agent: str = AGENT_USER_AGENT) -> bool:
    """
    True when the agent may fetch `url` per the host's robots.txt.

    Fail-open: any error (network, parse, non-200) is treated as allowed so a robots outage
    never blocks discovery. Only applied to HTML/index scraping — not public ATS JSON APIs.
    """
    if not RESPECT_ROBOTS:
        return True
    try:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return True
        origin = f"{parsed.scheme}://{parsed.netloc}"
        rp = _robots_cache.get(origin)
        if rp is None:
            rp = robotparser.RobotFileParser()
            rp.parse(_fetch_robots(f"{origin}/robots.txt").splitlines())
            _robots_cache[origin] = rp
        return rp.can_fetch(user_agent, url)
    except Exception as e:
        logger.debug(f"robots.txt check failed for {url}: {e}")
        return True


# -------------------------------------------------------------
# DuckDuckGo HTML Search Utility
# -------------------------------------------------------------


def search_ddg(query: str, max_results: int = 5) -> List[Dict[str, str]]:
    """
    Searches DuckDuckGo HTML endpoint and extracts organic search results.
    Returns list of dicts: {'title': ..., 'link': ..., 'snippet': ...}
    """
    if not query or not query.strip():
        return []

    encoded = urllib.parse.quote(query.strip())
    url = f"https://html.duckduckgo.com/html/?q={encoded}"

    try:
        res = requests.get(url, headers=HEADERS, timeout=6)
        if res.status_code != 200:
            logger.debug(f"DuckDuckGo search returned status {res.status_code}")
            return []

        soup = BeautifulSoup(res.text, "html.parser")
        results = []

        cards = soup.find_all("div", class_="result")
        for card in cards:
            if len(results) >= max_results:
                break

            title_el = card.find("a", class_="result__a")
            snippet_el = card.find("a", class_="result__snippet")

            if not title_el:
                continue

            raw_link = title_el.get("href", "")
            # Clean DDG redirect wrapper: //duckduckgo.com/l/?uddg=https%3A%2F%2F...
            if "uddg=" in raw_link:
                try:
                    parts = raw_link.split("uddg=")
                    clean_link = urllib.parse.unquote(parts[1].split("&")[0])
                except Exception:
                    clean_link = raw_link
            else:
                clean_link = raw_link

            title_text = title_el.get_text(strip=True)
            snippet_text = snippet_el.get_text(strip=True) if snippet_el else ""

            if clean_link and title_text:
                results.append(
                    {"title": title_text, "link": clean_link, "snippet": snippet_text}
                )

        return results
    except Exception as e:
        logger.debug(f"DuckDuckGo search notice for query '{query}': {e}")
        return []


# -------------------------------------------------------------
# Dynamic ATS Portal & Careers URL Extractor
# -------------------------------------------------------------


def extract_slug_from_careers_url(url_or_name: str, ats: str) -> str:
    """Extracts clean board slug from career portal URL or company name."""
    if not url_or_name or not url_or_name.strip():
        return ""
    val = url_or_name.strip()
    known_domains = (
        "greenhouse.io",
        "lever.co",
        "ashbyhq.com",
        "smartrecruiters.com",
        "workable.com",
    )
    looks_like_url = "://" in val or any(d in val for d in known_domains)
    if looks_like_url:
        try:
            parsed = urllib.parse.urlparse(val if "://" in val else f"https://{val}")
            path = parsed.path
            host = (parsed.hostname or "").lower()
            if ats == "greenhouse":
                if "for=" in val:
                    params = urllib.parse.parse_qs(parsed.query)
                    if "for" in params and params["for"]:
                        return params["for"][0].strip().lower()
                parts = [
                    p
                    for p in path.split("/")
                    if p and p not in ("v1", "boards", "embed", "jobs")
                ]
                if parts:
                    return parts[0].strip().lower()
            elif ats == "lever":
                parts = [
                    p for p in path.split("/") if p and p not in ("v0", "postings")
                ]
                if parts:
                    return parts[0].strip().lower()
            elif ats == "ashby":
                parts = [
                    p
                    for p in path.split("/")
                    if p and p not in ("posting-api", "job-board")
                ]
                if parts:
                    return parts[0].strip().lower()
            elif ats == "smartrecruiters":
                # api.smartrecruiters.com/v1/companies/{id} | careers.smartrecruiters.com/{id}
                parts = [
                    p for p in path.split("/") if p and p not in ("v1", "companies")
                ]
                if parts:
                    return parts[0].strip().lower()
            elif ats == "workable":
                # www.workable.com/api/accounts/{slug}
                if "api" in path and "accounts" in path:
                    parts = [
                        p for p in path.split("/") if p and p not in ("api", "accounts")
                    ]
                    if parts:
                        return parts[0].strip().lower()
                # {slug}.workable.com  (slug lives in the hostname)
                first_label = host.split(".")[0]
                if first_label and first_label not in ("apply", "www", "jobs"):
                    return first_label
                # apply.workable.com/{slug}/...
                parts = [p for p in path.split("/") if p]
                if parts:
                    return parts[0].strip().lower()
        except Exception:
            pass
    return val.lower().replace(" ", "")


SHORT_DOMAINS = {
    "lnkd.in",
    "bit.ly",
    "tinyurl.com",
    "t.co",
    "ow.ly",
    "buff.ly",
    "ift.tt",
    "is.gd",
    "rebrand.ly",
    "cutt.ly",
}


def unwrap_shortened_url(raw_url: str, timeout: int = 5) -> str:
    """
    Follows HTTP redirects for shortened URLs (lnkd.in, bit.ly, t.co, etc.)
    and strips common tracking query parameters.
    """
    if not raw_url or not isinstance(raw_url, str) or not raw_url.startswith("http"):
        return raw_url or ""

    clean_input = raw_url.strip()
    try:
        parsed = urllib.parse.urlparse(clean_input)
        hostname = (parsed.hostname or "").lower().replace("www.", "")

        if hostname in SHORT_DOMAINS or "lnkd.in" in clean_input:
            try:
                resp = requests.head(
                    clean_input, headers=HEADERS, allow_redirects=True, timeout=timeout
                )
                if resp.url and resp.url != clean_input:
                    clean_input = resp.url
                elif resp.status_code in (405, 403, 400):
                    with requests.get(
                        clean_input,
                        headers=HEADERS,
                        allow_redirects=True,
                        stream=True,
                        timeout=timeout,
                    ) as get_resp:
                        if get_resp.url:
                            clean_input = get_resp.url
            except Exception as e:
                logger.debug(f"Could not unwrap shortened URL {raw_url}: {e}")
    except Exception:
        pass

    try:
        parsed = urllib.parse.urlparse(clean_input)
        if "linkedin.com" not in (parsed.hostname or ""):
            query_dict = urllib.parse.parse_qs(parsed.query)
            filtered_query = {
                k: v
                for k, v in query_dict.items()
                if not k.lower().startswith("utm_")
                and k.lower()
                not in ("trk", "refid", "trackingid", "ref", "fbclid", "gclid")
            }
            new_query = urllib.parse.urlencode(filtered_query, doseq=True)
            clean_input = urllib.parse.urlunparse(parsed._replace(query=new_query))
    except Exception:
        pass

    return clean_input


def extract_portal_info_from_job_url(
    job_url: str, company_name: str = ""
) -> Optional[Dict[str, str]]:
    """
    Extracts canonical ATS apply link, career board root URL, portal type, and domain
    from a job application URL or LinkedIn externalApply redirect wrapper.
    """
    if not job_url or not job_url.strip():
        return None

    raw_url = unwrap_shortened_url(job_url.strip())

    # Unwrap LinkedIn redirect wrapper if present
    # e.g., https://www.linkedin.com/jobs/view/externalApply/12345?url=https%3A%2F%2Fboards.greenhouse.io%2Fstripe%2Fjobs%2F5892019&urlHash=abc
    if "externalApply" in raw_url and "url=" in raw_url:
        try:
            parsed = urllib.parse.urlparse(raw_url)
            params = urllib.parse.parse_qs(parsed.query)
            if "url" in params and params["url"]:
                raw_url = params["url"][0]
        except Exception:
            pass

    clean_apply_url = (
        raw_url.split("?")[0] if "linkedin.com" not in raw_url else raw_url
    )
    try:
        parsed = urllib.parse.urlparse(raw_url)
    except Exception:
        return None

    hostname = (parsed.hostname or "").lower()
    path = parsed.path

    careers_url = None
    portal_type = "Direct"
    domain = None

    # Greenhouse ATS
    if "greenhouse.io" in hostname:
        portal_type = "Greenhouse"
        slug = extract_slug_from_careers_url(raw_url, "greenhouse")
        if slug:
            careers_url = f"https://boards.greenhouse.io/{slug}"

    # Lever ATS
    elif "lever.co" in hostname:
        portal_type = "Lever"
        slug = extract_slug_from_careers_url(raw_url, "lever")
        if slug:
            careers_url = f"https://jobs.lever.co/{slug}"

    # Ashby ATS
    elif "ashbyhq.com" in hostname:
        portal_type = "Ashby"
        slug = extract_slug_from_careers_url(raw_url, "ashby")
        if slug:
            careers_url = f"https://jobs.ashbyhq.com/{slug}"

    # SmartRecruiters ATS
    elif "smartrecruiters.com" in hostname:
        portal_type = "SmartRecruiters"
        slug = extract_slug_from_careers_url(raw_url, "smartrecruiters")
        if slug:
            careers_url = f"https://careers.smartrecruiters.com/{slug}"

    # Workable ATS
    elif "workable.com" in hostname:
        portal_type = "Workable"
        slug = extract_slug_from_careers_url(raw_url, "workable")
        if slug:
            careers_url = f"https://apply.workable.com/{slug}/"

    # Workday ATS
    elif "myworkdayjobs.com" in hostname:
        portal_type = "Workday"
        parts = [p for p in path.split("/") if p]
        # Skip the locale segment (e.g. /en-US/) so we keep the real board/site name.
        site = next((p for p in parts if not _WORKDAY_LOCALE_RE.match(p)), None)
        if site:
            careers_url = f"{parsed.scheme}://{parsed.netloc}/{site}"
        else:
            careers_url = f"{parsed.scheme}://{parsed.netloc}"

    # General Direct Careers Portal
    elif "linkedin.com" not in hostname and hostname:
        portal_type = "Direct"
        if "careers" in path.lower() or "jobs" in path.lower():
            path_segments = [p for p in path.split("/") if p]
            if len(path_segments) > 1:
                careers_url = f"{parsed.scheme}://{parsed.netloc}/{path_segments[0]}"
            else:
                careers_url = f"{parsed.scheme}://{parsed.netloc}{path}"
        else:
            careers_url = f"{parsed.scheme}://{parsed.netloc}"
        domain = hostname.replace("www.", "")

    if not domain and company_name:
        domain = f"{company_name.lower().replace(' ', '')}.com"

    return {
        "canonical_apply_url": clean_apply_url,
        "careers_url": careers_url,
        "portal_type": portal_type,
        "domain": domain,
    }


# -------------------------------------------------------------
# Greenhouse Careers Board Scraper
# -------------------------------------------------------------


def scrape_greenhouse_jobs(
    company_name: str,
    title_filters: Optional[List[str]] = None,
    city_filters: Optional[List[str]] = None,
    location_filters: Optional[List[str]] = None,
    work_mode: Optional[str] = None,
    max_matches: int = 35,
    careers_url: Optional[str] = None,
) -> List[Dict[str, str]]:
    """
    Fetches job listings from Greenhouse Public Board API with high-efficiency early filtering.
    - Fetches lightweight metadata without heavy HTML content (~95% less bandwidth, sub-second response).
    - Applies title, location, and work-mode filters early on lightweight payloads.
    - Fetches full HTML descriptions on-demand ONLY for candidate-matching positions.
    """
    if not company_name or not company_name.strip():
        return []

    # Derive board slug from careers_url or company name
    slug = extract_slug_from_careers_url(careers_url or company_name, "greenhouse")
    if not slug:
        return []

    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"

    try:
        res = requests.get(url, headers=HEADERS, timeout=15)
        if res.status_code != 200:
            return []

        data = res.json()
        raw_jobs = data.get("jobs", [])
        jobs = []

        has_filters = bool(
            title_filters
            or city_filters
            or location_filters
            or (work_mode and work_mode != "all")
        )

        for item in raw_jobs:
            title = item.get("title", "Untitled Role").strip()
            loc_str = (item.get("location") or {}).get("name", "Remote / Various")
            job_id = item.get("id")
            abs_url = item.get("absolute_url", "")

            # Early filter before fetching description
            if has_filters:
                # Title filter (shared semantics with the final matcher)
                if title_filters and not check_title_match(title, title_filters):
                    continue

                from backend.preference_filter import (
                    check_location_match,
                    check_workmode_match,
                )

                # Location & Work Mode eligibility check
                location_check = check_location_match(
                    f"{title} {loc_str}", location_filters, city_filters
                )
                workmode_check = check_workmode_match(title, loc_str, work_mode)
                if not location_check or not workmode_check:
                    continue

            raw_content = item.get("content", "")
            embedded_desc = (
                BeautifulSoup(raw_content, "html.parser")
                .get_text(separator="\n")
                .strip()
                if raw_content
                else ""
            )

            jobs.append(
                {
                    "id": job_id,
                    "title": title,
                    "url": abs_url,
                    "description": embedded_desc,
                    "location": loc_str,
                    "source": "Greenhouse Portal",
                    "source_type": "Direct",
                }
            )

            if len(jobs) >= max_matches:
                break

        # Fetch detailed content only for matched positions if not already present
        final_jobs = []
        for j in jobs:
            clean_desc = (
                j.get("description")
                or f"{j['title']} opening at {company_name} ({j['location']})."
            )
            if not j.get("description") and j.get("id"):
                try:
                    detail_res = requests.get(
                        f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{j['id']}",
                        headers=HEADERS,
                        timeout=8,
                    )
                    if detail_res.status_code == 200:
                        raw_content = detail_res.json().get("content", "")
                        if raw_content:
                            clean_desc = (
                                BeautifulSoup(raw_content, "html.parser")
                                .get_text(separator="\n")
                                .strip()
                            )
                except Exception:
                    pass

            final_jobs.append(
                {
                    "title": j["title"],
                    "url": j["url"],
                    "description": clean_desc,
                    "location": j["location"],
                    "source": j["source"],
                    "source_type": j["source_type"],
                }
            )

        return final_jobs
    except Exception as e:
        logger.warning(f"Greenhouse scraping failed for '{company_name}': {e}")
        return []


# -------------------------------------------------------------
# Lever Careers Postings Scraper
# -------------------------------------------------------------


def scrape_lever_jobs(
    company_name: str,
    title_filters: Optional[List[str]] = None,
    city_filters: Optional[List[str]] = None,
    location_filters: Optional[List[str]] = None,
    work_mode: Optional[str] = None,
    max_matches: int = 35,
    careers_url: Optional[str] = None,
) -> List[Dict[str, str]]:
    """
    Fetches job listings from Lever Public Postings API with early filtering.
    Endpoint: https://api.lever.co/v0/postings/{slug}?mode=json
    """
    if not company_name or not company_name.strip():
        return []

    slug = extract_slug_from_careers_url(careers_url or company_name, "lever")
    if not slug:
        return []

    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"

    try:
        res = requests.get(url, headers=HEADERS, timeout=15)
        if res.status_code != 200:
            return []

        data = res.json()
        if not isinstance(data, list):
            return []

        jobs = []
        has_filters = bool(
            title_filters
            or city_filters
            or location_filters
            or (work_mode and work_mode != "all")
        )

        for item in data:
            title = item.get("text", "Untitled Role").strip()
            loc_str = (item.get("categories") or {}).get("location", "Remote / Various")

            if has_filters:
                if title_filters and not check_title_match(title, title_filters):
                    continue
                from backend.preference_filter import (
                    check_location_match,
                    check_workmode_match,
                )

                location_check = check_location_match(
                    f"{title} {loc_str}", location_filters, city_filters
                )
                workmode_check = check_workmode_match(title, loc_str, work_mode)
                if not location_check or not workmode_check:
                    continue

            desc = item.get("descriptionPlain") or item.get("description") or ""
            clean_desc = (
                BeautifulSoup(desc, "html.parser").get_text(separator="\n").strip()
                if desc
                else ""
            )

            jobs.append(
                {
                    "title": title,
                    "url": item.get("applyUrl") or item.get("hostedUrl", ""),
                    "description": clean_desc,
                    "location": loc_str,
                    "source": "Lever Portal",
                    "source_type": "Direct",
                }
            )

            if len(jobs) >= max_matches:
                break

        return jobs
    except Exception as e:
        logger.warning(f"Lever scraping failed for '{company_name}': {e}")
        return []


def scrape_ashby_jobs(
    company_name: str,
    title_filters: Optional[List[str]] = None,
    city_filters: Optional[List[str]] = None,
    location_filters: Optional[List[str]] = None,
    work_mode: Optional[str] = None,
    max_matches: int = 35,
    careers_url: Optional[str] = None,
) -> List[Dict[str, str]]:
    """
    Fetches job listings from Ashby Public Board API with early filtering.
    Endpoint: https://api.ashbyhq.com/posting-api/job-board/{board_slug}
    """
    if not company_name or not company_name.strip():
        return []

    slug = extract_slug_from_careers_url(careers_url or company_name, "ashby")
    if not slug:
        return []

    url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"

    try:
        res = requests.get(url, headers=HEADERS, timeout=15)
        if res.status_code != 200:
            return []

        data = res.json()
        raw_jobs = data.get("jobs", [])
        jobs = []
        has_filters = bool(
            title_filters
            or city_filters
            or location_filters
            or (work_mode and work_mode != "all")
        )

        for item in raw_jobs:
            title = item.get("title", "Untitled Role").strip()
            loc_str = item.get("location") or "Remote / Various"
            secondary_locs = item.get("secondaryLocations") or []
            all_loc_strs = [loc_str]
            for sec in secondary_locs:
                if isinstance(sec, dict) and sec.get("location"):
                    all_loc_strs.append(sec.get("location"))
                elif isinstance(sec, str) and sec:
                    all_loc_strs.append(sec)

            if has_filters:
                if title_filters and not check_title_match(title, title_filters):
                    continue
                from backend.preference_filter import (
                    check_location_match,
                    check_workmode_match,
                )

                if not any(
                    check_location_match(
                        job_location=l,
                        city_filters=city_filters,
                        loc_filters=location_filters,
                    )
                    or check_workmode_match(title, l, work_mode)
                    for l in all_loc_strs
                ):
                    continue

            desc_html = item.get("descriptionHtml") or ""
            clean_desc = (
                BeautifulSoup(desc_html, "html.parser").get_text(separator="\n").strip()
                if desc_html
                else ""
            )

            jobs.append(
                {
                    "title": title,
                    "url": item.get("jobUrl") or item.get("applyUrl", ""),
                    "description": clean_desc,
                    "location": loc_str,
                    "source": "Ashby Portal",
                    "source_type": "Direct",
                }
            )

            if len(jobs) >= max_matches:
                break

        return jobs
    except Exception as e:
        logger.warning(f"Ashby scraping failed for '{company_name}': {e}")
        return []


def scrape_smartrecruiters_jobs(
    company_name: str,
    title_filters: Optional[List[str]] = None,
    city_filters: Optional[List[str]] = None,
    location_filters: Optional[List[str]] = None,
    work_mode: Optional[str] = None,
    max_matches: int = 35,
    careers_url: Optional[str] = None,
) -> List[Dict[str, str]]:
    """
    Fetches job postings from the SmartRecruiters public Posting API.

    Endpoint: GET https://api.smartrecruiters.com/v1/companies/{companyIdentifier}/postings
    List rows omit the apply URL and description; a per-posting detail call supplies them on
    demand (mirrors the Greenhouse detail fetch). Pagination uses the API's `limit`/`offset`
    parameters. The shared client-side title/location matchers remain authoritative so server
    behaviour can never starve the final match (see docs/ATS_PORTALS.md §3.3).
    """
    if not company_name or not company_name.strip():
        return []

    slug = extract_slug_from_careers_url(careers_url or company_name, "smartrecruiters")
    if not slug:
        return []

    base = f"https://api.smartrecruiters.com/v1/companies/{slug}/postings"
    has_filters = bool(
        title_filters
        or city_filters
        or location_filters
        or (work_mode and work_mode != "all")
    )

    raw_items: List[Dict[str, Any]] = []
    try:
        offset = 0
        page_size = 100
        max_pages = 5  # bound list requests for large enterprise boards
        for _ in range(max_pages):
            res = requests.get(
                base,
                headers=HEADERS,
                params={"limit": page_size, "offset": offset},
                timeout=15,
            )
            if res.status_code != 200:
                break
            data = res.json()
            content = data.get("content") or []
            raw_items.extend(content)
            total = data.get("totalFound")
            offset += data.get("limit") or page_size
            if not content or (isinstance(total, int) and offset >= total):
                break
    except Exception as e:
        logger.warning(f"SmartRecruiters scraping failed for '{company_name}': {e}")
        return []

    jobs = []
    for item in raw_items:
        title = (item.get("name") or "Untitled Role").strip()
        loc = item.get("location") or {}
        loc_str = (
            ", ".join(
                [
                    p
                    for p in (loc.get("city"), loc.get("region"), loc.get("country"))
                    if p
                ]
            )
            or "Remote / Various"
        )
        if loc.get("remote") and "remote" not in loc_str.lower():
            loc_str = f"{loc_str} (Remote)"

        if has_filters:
            if title_filters and not check_title_match(title, title_filters):
                continue
            from backend.preference_filter import (
                check_location_match,
                check_workmode_match,
            )

            location_check = check_location_match(
                f"{title} {loc_str}", location_filters, city_filters
            )
            workmode_check = check_workmode_match(title, loc_str, work_mode)
            if not location_check or not workmode_check:
                continue

        apply_url = (
            item.get("applyUrl") or item.get("postingUrl") or item.get("ref") or ""
        )
        description = ""
        detail_url = item.get("ref") or (
            f"{base}/{item.get('id')}" if item.get("id") else ""
        )
        if detail_url:
            try:
                dres = requests.get(detail_url, headers=HEADERS, timeout=8)
                if dres.status_code == 200:
                    detail = dres.json()
                    apply_url = (
                        detail.get("applyUrl") or detail.get("postingUrl") or apply_url
                    )
                    sections = (detail.get("jobAd") or {}).get("sections") or {}
                    parts = []
                    for key in (
                        "companyDescription",
                        "jobDescription",
                        "qualifications",
                        "additionalInformation",
                    ):
                        sec = sections.get(key) or {}
                        txt = sec.get("text") if isinstance(sec, dict) else None
                        if txt:
                            parts.append(
                                BeautifulSoup(txt, "html.parser")
                                .get_text(separator="\n")
                                .strip()
                            )
                    description = "\n\n".join([p for p in parts if p]).strip()
            except Exception:
                pass

        jobs.append(
            {
                "title": title,
                "url": apply_url,
                "description": description
                or f"{title} opening at {company_name} ({loc_str}).",
                "location": loc_str,
                "source": "SmartRecruiters Portal",
                "source_type": "Direct",
            }
        )

        if len(jobs) >= max_matches:
            break

    return jobs


def scrape_workable_jobs(
    company_name: str,
    title_filters: Optional[List[str]] = None,
    city_filters: Optional[List[str]] = None,
    location_filters: Optional[List[str]] = None,
    work_mode: Optional[str] = None,
    max_matches: int = 35,
    careers_url: Optional[str] = None,
) -> List[Dict[str, str]]:
    """
    Fetches published job postings from Workable's public widget endpoint.

    Endpoint: GET https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true
    (Workable redirects www.workable.com/api/accounts/{slug} to this widget URL.) The public
    feed exposes only published roles and has no server-side filters, so the shared client-side
    title/location matchers are applied here (see docs/ATS_PORTALS.md §3.3).
    """
    if not company_name or not company_name.strip():
        return []

    slug = extract_slug_from_careers_url(careers_url or company_name, "workable")
    if not slug:
        return []

    url = f"https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true"

    try:
        res = requests.get(url, headers=HEADERS, timeout=15)
        if res.status_code != 200:
            return []

        data = res.json()
        raw_jobs = data.get("jobs") or []
        has_filters = bool(
            title_filters
            or city_filters
            or location_filters
            or (work_mode and work_mode != "all")
        )
        jobs = []

        for item in raw_jobs:
            title = (item.get("title") or "Untitled Role").strip()
            loc_str = (
                ", ".join(
                    [
                        p
                        for p in (
                            item.get("city"),
                            item.get("state"),
                            item.get("country"),
                        )
                        if p
                    ]
                )
                or "Remote / Various"
            )
            is_remote = (
                bool(item.get("telecommuting"))
                or str(item.get("workplace_type") or "").lower() == "remote"
            )
            if is_remote and "remote" not in loc_str.lower():
                loc_str = f"{loc_str} (Remote)"

            if has_filters:
                if title_filters and not check_title_match(title, title_filters):
                    continue
                from backend.preference_filter import (
                    check_location_match,
                    check_workmode_match,
                )

                location_check = check_location_match(
                    f"{title} {loc_str}", location_filters, city_filters
                )
                workmode_check = check_workmode_match(title, loc_str, work_mode)
                if not location_check or not workmode_check:
                    continue

            desc = item.get("description") or item.get("full_description") or ""
            clean_desc = (
                BeautifulSoup(desc, "html.parser").get_text(separator="\n").strip()
                if desc
                else ""
            )

            jobs.append(
                {
                    "title": title,
                    "url": item.get("url")
                    or item.get("application_url")
                    or item.get("shortlink")
                    or "",
                    "description": clean_desc
                    or f"{title} opening at {company_name} ({loc_str}).",
                    "location": loc_str,
                    "source": "Workable Portal",
                    "source_type": "Direct",
                }
            )

            if len(jobs) >= max_matches:
                break

        return jobs
    except Exception as e:
        logger.warning(f"Workable scraping failed for '{company_name}': {e}")
        return []


def _parse_workday_board(url: str) -> Optional[Dict[str, str]]:
    """
    Parses a Workday board/job URL into {tenant, shard, site, host}.

    Workday encodes three independent values that cannot be inferred from a company name:
      https://{tenant}.{shard}.myworkdayjobs.com/{locale?}/{site}/...
    """
    if not url or "myworkdayjobs.com" not in url:
        return None
    try:
        parsed = urllib.parse.urlparse(url if "://" in url else f"https://{url}")
        host = (parsed.hostname or "").lower()
        labels = host.split(".")
        if len(labels) < 3:
            return None
        tenant, shard = labels[0], labels[1]
        parts = [p for p in parsed.path.split("/") if p]
        site = next((p for p in parts if not _WORKDAY_LOCALE_RE.match(p)), "")
        if not tenant or not shard or not site:
            return None
        return {"tenant": tenant, "shard": shard, "site": site, "host": host}
    except Exception:
        return None


def _normalize_workday_external_path(external_path: str, site: str) -> str:
    """Strips a leading locale/site from Workday's externalPath for the CXS detail call."""
    parts = [p for p in (external_path or "").split("/") if p]
    if parts and _WORKDAY_LOCALE_RE.match(parts[0]):
        parts.pop(0)
    if parts and site and parts[0] == site:
        parts.pop(0)
    return "/" + "/".join(parts) if parts else ""


def scrape_workday_jobs(
    company_name: str,
    title_filters: Optional[List[str]] = None,
    city_filters: Optional[List[str]] = None,
    location_filters: Optional[List[str]] = None,
    work_mode: Optional[str] = None,
    max_matches: int = 35,
    careers_url: Optional[str] = None,
) -> List[Dict[str, str]]:
    """
    Fetches job postings from a Workday tenant's public CXS API.

    Board URL: https://{tenant}.{shard}.myworkdayjobs.com/{locale}/{site}
    List:   POST /wday/cxs/{tenant}/{site}/jobs   (JSON body: limit/offset/searchText)
    Detail: GET  /wday/cxs/{tenant}/{site}{externalPath}

    Tenant/shard/site cannot be guessed from a bare company name, so this scraper requires a
    Workday board URL (usually Company.careers_url). The shared client-side title/location
    matchers remain authoritative (see docs/ATS_PORTALS.md §3.3).
    """
    board = _parse_workday_board(careers_url or company_name or "")
    if not board:
        return []

    host, tenant, site = board["host"], board["tenant"], board["site"]
    api_url = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    list_headers = {
        **HEADERS,
        "content-type": "application/json",
        "accept": "application/json",
    }
    has_filters = bool(
        title_filters
        or city_filters
        or location_filters
        or (work_mode and work_mode != "all")
    )

    postings: List[Dict[str, Any]] = []
    try:
        offset = 0
        page_size = 20
        max_pages = 5  # bound requests (~100 postings)
        for _ in range(max_pages):
            res = requests.post(
                api_url,
                headers=list_headers,
                json={
                    "appliedFacets": {},
                    "limit": page_size,
                    "offset": offset,
                    "searchText": "",
                },
                timeout=15,
            )
            if res.status_code != 200:
                break
            data = res.json() or {}
            page = data.get("jobPostings") or []
            postings.extend(page)
            total = data.get("total")
            offset += page_size
            if not page or (isinstance(total, int) and offset >= total):
                break
    except Exception as e:
        logger.warning(f"Workday scraping failed for '{company_name}': {e}")
        return []

    jobs = []
    for item in postings:
        title = (item.get("title") or "Untitled Role").strip()
        loc_str = (item.get("locationsText") or "Remote / Various").strip()
        external_path = item.get("externalPath") or ""

        if has_filters:
            if title_filters and not check_title_match(title, title_filters):
                continue
            from backend.preference_filter import (
                check_location_match,
                check_workmode_match,
            )

            location_check = check_location_match(
                f"{title} {loc_str}", location_filters, city_filters
            )
            workmode_check = check_workmode_match(title, loc_str, work_mode)
            if not location_check or not workmode_check:
                continue

        public_url = (
            f"https://{host}{external_path}"
            if external_path.startswith("/")
            else (
                f"https://{host}/{site}/{external_path}"
                if external_path
                else f"https://{host}/{site}"
            )
        )

        description = ""
        detail_path = _normalize_workday_external_path(external_path, site)
        if detail_path:
            try:
                dres = requests.get(
                    f"https://{host}/wday/cxs/{tenant}/{site}{detail_path}",
                    headers=HEADERS,
                    timeout=8,
                )
                if dres.status_code == 200:
                    info = (dres.json() or {}).get("jobPostingInfo") or {}
                    if info.get("location"):
                        loc_str = info["location"] or loc_str
                    desc_html = info.get("jobDescription") or ""
                    if desc_html:
                        description = (
                            BeautifulSoup(desc_html, "html.parser")
                            .get_text(separator="\n")
                            .strip()
                        )
            except Exception:
                pass

        jobs.append(
            {
                "title": title,
                "url": public_url,
                "description": description
                or f"{title} opening at {company_name} ({loc_str}).",
                "location": loc_str,
                "source": "Workday Portal",
                "source_type": "Direct",
            }
        )

        if len(jobs) >= max_matches:
            break

    return jobs


def scrape_uber_jobs() -> List[Dict[str, str]]:
    """
    Fetches live job listings from Uber's public careers search API.
    """
    url = "https://www.uber.com/api/loadSearchJobsResults"
    headers = {**HEADERS, "content-type": "application/json", "x-csrf-token": "x"}
    payload = {"params": {"query": "", "limit": 40}}
    try:
        res = requests.post(url, headers=headers, json=payload, timeout=15)
        if res.status_code == 200:
            data = res.json()
            results = (data.get("data") or {}).get("results", [])
            jobs = []
            for item in results:
                title = item.get("title", "").strip()
                job_id = item.get("id")
                loc_obj = item.get("location") or {}
                city = loc_obj.get("city", "")
                country = loc_obj.get("countryName", "")
                loc_str = f"{city}, {country}".strip(", ") or "Remote / Various"

                if title and job_id:
                    jobs.append(
                        {
                            "title": title,
                            "url": f"https://www.uber.com/global/en/careers/list/{job_id}/",
                            "description": item.get("description")
                            or f"Uber opening for {title} in {loc_str}.",
                            "location": loc_str,
                            "source": "Uber Careers",
                            "source_type": "Direct",
                        }
                    )
            if jobs:
                return jobs
    except Exception as e:
        logger.warning(f"Uber API scraping failed: {e}")
    return []


def scrape_custom_jobs(portal_url: str) -> List[Dict[str, str]]:
    """
    Scrapes generic career websites for open position links using heuristics.
    """
    if not portal_url or not portal_url.strip():
        return []

    if not robots_allowed(portal_url):
        logger.warning(
            f"robots.txt disallows scraping '{portal_url}'; skipping custom portal scan."
        )
        return []

    try:
        res = requests.get(portal_url, headers=AGENT_HEADERS, timeout=15)
        if res.status_code != 200:
            return []

        soup = BeautifulSoup(res.text, "html.parser")
        jobs = []
        seen_urls = set()

        for a in soup.find_all("a", href=True):
            href = a["href"]
            text = a.get_text(strip=True)
            href_lower = href.lower()
            text_lower = text.lower()

            # Heuristics for job posting URLs
            is_job_link = any(
                k in href_lower
                for k in ["/jobs/", "/job/", "/careers/", "/opening/", "/position/"]
            ) or any(
                k in text_lower
                for k in ["apply", "view opening", "view role", "details"]
            )

            if is_job_link:
                full_url = urllib.parse.urljoin(portal_url, href)
                if full_url not in seen_urls and len(text) > 3:
                    seen_urls.add(full_url)
                    jobs.append(
                        {
                            "title": text,
                            "url": full_url,
                            "description": "Click apply to view full role details on company portal.",
                            "location": "See portal",
                            "source": "Direct Careers Page",
                            "source_type": "Direct",
                        }
                    )

        return jobs
    except Exception as e:
        logger.warning(f"Custom portal scraping failed for '{portal_url}': {e}")
        return []


# -------------------------------------------------------------
# Company Intelligence & Talent Partners
# -------------------------------------------------------------


def gather_company_intelligence(
    company_name: str, job_title: str = "", provider: Optional[str] = None
) -> Dict[str, str]:
    """
    Gathers and synthesizes company news, products, salary ranges, and hiring processes.
    Uses DuckDuckGo searches + LLM summarization.
    """
    if not company_name or not company_name.strip():
        return {
            "description": "Company name not provided.",
            "recent_news": "",
            "salary_insights": "",
            "hiring_process": "",
            "market_position": "",
            "talking_points": "",
        }

    # 1. Search recent news & products
    news_results = search_ddg(
        f"{company_name} recent news products services 2026", max_results=3
    )
    news_ctx = "\n".join([f"- {r['title']}: {r['snippet']}" for r in news_results])

    # 2. Search salary ranges & levels
    salary_query = (
        f"{company_name} {job_title} salary range levels.fyi glassdoor"
        if job_title
        else f"{company_name} engineering salary ranges"
    )
    salary_results = search_ddg(salary_query, max_results=3)
    salary_ctx = "\n".join([f"- {r['title']}: {r['snippet']}" for r in salary_results])

    # 3. Search interview & hiring process
    interview_results = search_ddg(
        f"{company_name} interview process technical screening steps", max_results=3
    )
    interview_ctx = "\n".join(
        [f"- {r['title']}: {r['snippet']}" for r in interview_results]
    )

    # 4. Search company background, size, HQ & competitors
    market_results = search_ddg(
        f"{company_name} company size employees founded headquarters competitors",
        max_results=3,
    )
    market_ctx = "\n".join([f"- {r['title']}: {r['snippet']}" for r in market_results])

    system_prompt = (
        "You are an executive research analyst. Synthesize the provided raw web snippets into a clean, "
        "structured JSON object with the following schema:\n"
        "{\n"
        '  "description": "1-2 concise sentences summarizing the core products, business model, and mission.",\n'
        '  "market_position": "Background and positioning: company size (employees/revenue if available), founded date, HQ location, target customers, value proposition, key competitors, and differentiators.",\n'
        '  "talking_points": "What makes this company interesting; questions a smart candidate would ask; relevant industry trends.",\n'
        '  "recent_news": "Bulleted summary of recent developments, product launches, leadership changes, funding rounds, or announcements.",\n'
        '  "salary_insights": "Estimated salary ranges or compensation structure for the role.",\n'
        '  "hiring_process": "Overview of typical interview stages (e.g. Recruiter screen, Technical, Onsite)."\n'
        "}\n"
        "Rules:\n"
        "- Use ONLY the information present in the provided raw snippets.\n"
        "- If a field is not supported by the snippets, write exactly: Not found in sources.\n"
        "- Use sentence case and simple sentences.\n"
        "- Output ONLY valid JSON without conversational wrapper."
    )

    user_prompt = (
        f"Target Company: {company_name}\nTarget Role: {job_title}\n\n"
        f"--- RAW NEWS / PRODUCTS DATA ---\n{news_ctx or 'No news results found.'}\n\n"
        f"--- RAW SALARY DATA ---\n{salary_ctx or 'No specific salary results found.'}\n\n"
        f"--- RAW HIRING PROCESS DATA ---\n{interview_ctx or 'No hiring process results found.'}\n\n"
        f"--- RAW BACKGROUND / MARKET DATA ---\n{market_ctx or 'No company background results found.'}"
    )

    try:
        raw_output = generate_text(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            json_mode=True,
            provider=provider,
            temperature=0.2,
            task_name=f"Intel-{company_name}",
        )
        cleaned = _clean_json_string(raw_output)
        parsed = json.loads(cleaned)

        return {
            "description": str(
                parsed.get("description", f"{company_name} is a technology company.")
            ),
            "recent_news": str(parsed.get("recent_news", "No recent news available.")),
            "salary_insights": str(
                parsed.get("salary_insights", "Compensation data unavailable.")
            ),
            "hiring_process": str(
                parsed.get("hiring_process", "Standard technical interview process.")
            ),
            "market_position": str(
                parsed.get("market_position", "Not found in sources.")
            ),
            "talking_points": str(
                parsed.get("talking_points", "Not found in sources.")
            ),
        }
    except Exception as e:
        logger.warning(f"Failed to synthesize company intelligence: {e}")
        return {
            "description": f"{company_name} is an active technology organization.",
            "recent_news": news_ctx[:300] if news_ctx else "No news available.",
            "salary_insights": salary_ctx[:300]
            if salary_ctx
            else "Competitive salary.",
            "hiring_process": interview_ctx[:300]
            if interview_ctx
            else "Standard screening process.",
            "market_position": market_ctx[:300]
            if market_ctx
            else "Not found in sources.",
            "talking_points": "Not found in sources.",
        }


def find_talent_partners(
    company_name: str, job_title: str = ""
) -> List[Dict[str, str]]:
    """
    Searches for technical recruiters or hiring managers for the target company on LinkedIn.
    """
    if not company_name or not company_name.strip():
        return []

    query = f"{company_name} recruiter OR talent partner {job_title} linkedin.com/in/"
    results = search_ddg(query, max_results=6)
    contacts = []

    for r in results:
        link = r.get("link", "")
        if "linkedin.com/in/" in link:
            # Clean name from title (e.g. "Jane Doe - Senior Recruiter - Acme | LinkedIn")
            raw_title = r.get("title", "")
            name_part = raw_title.split("-")[0].split("|")[0].split("–")[0].strip()

            contacts.append(
                {
                    "name": name_part or "Talent Partner",
                    "headline": r.get("snippet", "Recruiter / Talent Partner"),
                    "profile_url": link,
                }
            )

    return contacts


# -------------------------------------------------------------
# Due Diligence & Ghost Job Detection
# -------------------------------------------------------------


def evaluate_job_due_diligence(
    job_title: str,
    source_type: str,
    portal_jobs: List[Dict[str, str]],
    repost_count: int = 0,
) -> Dict[str, any]:
    """
    Performs due diligence on a job opportunity:
    1. If LinkedIn Job Alert: Checks if position actively exists on the direct company portal.
    2. Flags repeated reposts (repost_count >= 3) or phantom listings as a Probable Ghost Job.
    3. Returns {'is_ghost_job': bool, 'status_label': str, 'reason': str}
    """
    title_lower = job_title.lower()

    # Check if job title roughly matches any open job on direct portal
    found_on_portal = any(
        title_lower in (j.get("title", "").lower())
        or (j.get("title", "").lower()) in title_lower
        for j in portal_jobs
    )

    if source_type == "LinkedIn" and not found_on_portal and len(portal_jobs) > 0:
        return {
            "is_ghost_job": True,
            "status_label": "Probable Ghost Job",
            "reason": "Listed on LinkedIn alert, but not found on the active company careers portal.",
        }

    if repost_count >= 3:
        return {
            "is_ghost_job": True,
            "status_label": "Probable Ghost Job (Repeated Repost)",
            "reason": f"Position has been reposted {repost_count} times without being filled.",
        }

    if source_type == "Direct" or found_on_portal:
        return {
            "is_ghost_job": False,
            "status_label": "Genuine Opportunity",
            "reason": "Verified on direct company careers portal.",
        }

    return {
        "is_ghost_job": False,
        "status_label": "Unverified Listing",
        "reason": "Direct portal listings could not be independently queried.",
    }


# -------------------------------------------------------------
# Google for Jobs & Google Alerts Ingestion Engine
# -------------------------------------------------------------


def unwrap_google_redirect_url(url: str) -> str:
    """Unwraps Google redirect tracking links (google.com/url?...) to return the canonical destination URL."""
    if not url:
        return ""
    if "google.com/url" in url:
        try:
            parsed = urllib.parse.urlparse(url)
            params = urllib.parse.parse_qs(parsed.query)
            target = params.get("url") or params.get("q")
            if target and target[0]:
                return target[0]
        except Exception:
            pass
    return url


def parse_google_alerts_digest(
    email_html_or_text: str, email_subject: str = ""
) -> List[Dict[str, Any]]:
    """
    Parses Google Alerts digest emails (from googlealerts-noreply@google.com).
    Decodes tracking redirects and extracts job title, source employer/site, and snippet.
    """
    if not email_html_or_text or not email_html_or_text.strip():
        return []

    jobs = []
    soup = BeautifulSoup(email_html_or_text, "html.parser")

    # Try finding structured link blocks inside the email
    links = soup.find_all("a", href=True)
    for a in links:
        href = a["href"]
        text = a.get_text(strip=True)
        if not text or len(text) < 4:
            continue

        # Skip Google footer/settings links
        if any(
            skip in href.lower()
            for skip in [
                "google.com/alerts/manage",
                "google.com/alerts/remove",
                "support.google.com",
                "policies.google.com",
            ]
        ):
            continue

        canonical_url = unwrap_google_redirect_url(href)
        if not canonical_url or "google.com" in canonical_url:
            continue

        # Find surrounding snippet / parent container
        parent = a.find_parent(["td", "div", "li", "p", "tr"])
        snippet = ""
        company = "Discovered Employer"
        if parent:
            snippet = parent.get_text(" ", strip=True)
            # Try to identify company or site from URL
            try:
                domain = urllib.parse.urlparse(canonical_url).netloc.replace("www.", "")
                company = domain.split(".")[0].capitalize()
            except Exception:
                company = "Discovered via Google Alert"

        jobs.append(
            {
                "title": text,
                "company": company,
                "location": "Remote / Various",
                "salary_range": "Competitive",
                "description": snippet
                or f"Discovered via Google Alert: {email_subject}",
                "url": canonical_url,
                "posted_at": "Recently Discovered",
                "source": "Google Alerts",
                "source_type": "Google",
            }
        )

    # Deduplicate by URL
    seen_urls = set()
    unique_jobs = []
    for j in jobs:
        if j["url"] not in seen_urls:
            seen_urls.add(j["url"])
            unique_jobs.append(j)

    return unique_jobs


# Optional user-supplied employer aliases used to recognise an employer inside an email
# header. Ships empty; the heuristic regex below is the default path, so no real employer
# names are hardcoded in source.
_COMPANY_ALIASES_FILE = (
    Path(__file__).resolve().parent.parent / "data" / "company_aliases.json"
)
_company_aliases_cache: Optional[List[str]] = None


def _load_company_aliases() -> List[str]:
    """Loads the optional employer-alias list from data/company_aliases.json (a JSON array)."""
    global _company_aliases_cache
    if _company_aliases_cache is not None:
        return _company_aliases_cache
    aliases: List[str] = []
    try:
        if _COMPANY_ALIASES_FILE.exists():
            with open(_COMPANY_ALIASES_FILE, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, list):
                aliases = [str(x).strip() for x in data if str(x).strip()]
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(f"Could not load company aliases: {exc}")
    _company_aliases_cache = aliases
    return aliases


def extract_company_and_role_from_email_header(
    subject: str, snippet: str = "", sender: str = ""
) -> Dict[str, Optional[str]]:
    """
    Extracts employer company name and candidate role title from recruiter and interview emails.
    Handles major tech firms, startup patterns, and standard engineering hierarchies.
    """
    full_text = f"{subject} {snippet}".strip()
    company = None
    role = None

    # Optional user-configured employer aliases (data/company_aliases.json); never hardcoded.
    for comp in _load_company_aliases():
        pattern = r"\b" + re.escape(comp) + r"\b"
        if re.search(pattern, full_text, re.IGNORECASE) or re.search(
            pattern, sender, re.IGNORECASE
        ):
            company = comp
            break

    if not company:
        # Regex heuristics: 'with X', 'at X', 'from X', 'for X'
        comp_match = re.search(
            r"(?:with|at|from|for)\s+([A-Z][A-Za-z0-9\.\s]{2,25}?)(?:\s*[:|\-,]|\s+for|\s+regarding|\s+team|\s*$)",
            subject,
        )
        if comp_match:
            candidate_comp = comp_match.group(1).strip()
            if candidate_comp.lower() not in [
                "your",
                "the",
                "our",
                "an",
                "interview",
                "interviews",
                "application",
                "process",
                "next",
            ]:
                company = candidate_comp

    # Clean subject for standard role extraction
    clean_subj = subject
    for prefix in [
        "Information needed for your interviews with",
        "Virtual Interview for",
        "Video interview -",
        "Interview with",
        "Update from",
        "Re:",
        "Fwd:",
    ]:
        if prefix.lower() in clean_subj.lower():
            clean_subj = re.sub(re.escape(prefix), "", clean_subj, flags=re.IGNORECASE)

    standard_roles = [
        "Principal Software Engineer",
        "Staff Software Engineer",
        "Senior Software Engineer",
        "Software Development Engineer",
        "Principal Engineer",
        "Staff Engineer",
        "Senior Engineer",
        "Lead Software Engineer",
        "Engineering Manager",
        "Solutions Architect",
        "Principal Architect",
        "Staff Architect",
        "Data Engineer",
        "Machine Learning Engineer",
        "AI Engineer",
        "Software Engineer II",
        "Software Engineer 2",
        "Software Engineer",
    ]
    for r in standard_roles:
        if re.search(r"\b" + re.escape(r) + r"\b", clean_subj, re.IGNORECASE):
            role = r
            break
        elif re.search(r"\b" + re.escape(r) + r"\b", snippet, re.IGNORECASE):
            role = r
            break

    return {"company": company, "role": role or "Software Engineer"}


# Role phrases commonly embedded as the header of a job-alert snippet. Aggregators such as
# LinkedIn render their alert cards as "<Employer> <Role>: <blurb>", which lets us recover
# the real employer/title that the sender header alone cannot provide.
_OPPORTUNITY_ROLE_PHRASE_RE = re.compile(
    r"\b((?:(?:Principal|Staff|Senior|Sr\.?|Lead|Junior|Associate|Chief)\s+)?"
    r"(?:Software|Backend|Back[- ]?end|Frontend|Front[- ]?end|Full[- ]?Stack|Fullstack|"
    r"Data|Machine\s+Learning|ML|AI|Platform|Cloud|DevOps|Security|Mobile|Android|iOS|"
    r"QA|Test|Site\s+Reliability|SRE|Systems?|Embedded|Product|Project|Program|Engineering)"
    r"\s+(?:Engineer|Developer|Architect|Manager|Lead|Scientist|Analyst|Designer))",
    re.IGNORECASE,
)

# Leading words that betray a blurb/sentence prefix rather than an employer name.
_OPPORTUNITY_COMPANY_STOPWORDS = {
    "we",
    "our",
    "the",
    "this",
    "these",
    "those",
    "join",
    "looking",
    "seeking",
    "hiring",
    "come",
    "are",
    "is",
    "a",
    "an",
    "new",
    "great",
    "exciting",
    "position",
    "role",
    "job",
    "opportunity",
    "apply",
    "now",
    "click",
    "see",
    "find",
    "your",
    "about",
    "at",
    "for",
    "with",
    "and",
    "or",
    "to",
    "be",
    "you",
    "they",
    "it",
}


def extract_opportunity_company_and_title(
    snippet: str,
    subject: str = "",
    fallback_company: str = "",
    fallback_role: str = "",
) -> tuple:
    """
    Recovers the *real* employer and role from a job-alert snippet body.

    Aggregator senders (LinkedIn, Indeed, ...) put their own name in the email header, so
    `extract_company_and_role_from_email_header` yields the aggregator as the company and a
    generic role. Their alert cards, however, render as "<Employer> <Role>: <blurb>". This
    parses that leading header so distinct roles at different employers stay
    distinguishable. Falls back to the supplied values when no trustworthy header is found.
    """
    fallback_company = (fallback_company or "").strip()
    fallback_role = (fallback_role or "").strip()

    text = (snippet or "").strip()
    if not text:
        return fallback_company, fallback_role

    match = _OPPORTUNITY_ROLE_PHRASE_RE.search(text)
    if not match:
        return fallback_company, fallback_role

    company = text[: match.start()].strip(" \t\r\n-\u2013\u2014:|•·[]()")
    # Reject sentence fragments / blurb-like prefixes so we never invent an employer.
    words = company.split()
    if (
        not company
        or len(company) > 60
        or len(words) > 4
        or any(ch in company for ch in ".!?\n")
        or words[0].lower().strip("'\"") in _OPPORTUNITY_COMPANY_STOPWORDS
    ):
        return fallback_company, fallback_role

    title = match.group(1).strip()
    # Keep an immediately-following qualifier (" (Integrations)", " - AI Platform",
    # ", AI Email App", "/ Architect- AI") but stop at the blurb delimiter.
    qualifier = re.match(r"(\s*[(\-,\/][^:.\n]{1,50})", text[match.end() :])
    if qualifier:
        title = f"{title}{qualifier.group(1).rstrip(' ,')}".strip()

    return company, title or fallback_role


def fetch_jobs_via_jobspy(
    search_terms: List[str], location: Optional[str] = None, results_wanted: int = 25
) -> List[Dict[str, Any]]:
    """
    Optional best-effort Google Jobs source via the open-source JobSpy library.

    JobSpy is an unofficial aggregator (same ToS/robustness class as the LinkedIn path),
    so this is opt-in and returns [] when the library is missing or scraping fails.
    Google requires the natural-language ``google_search_term``.
    """
    if _jobspy_scrape_jobs is None:
        logger.info("JobSpy is not installed; Google Jobs fallback unavailable.")
        return []

    results: List[Dict[str, Any]] = []
    for term in search_terms or []:
        term = (term or "").strip()
        if not term:
            continue
        try:
            df = _jobspy_scrape_jobs(
                site_name="google",
                google_search_term=term,
                location=location or "",
                results_wanted=results_wanted,
                verbose=0,
            )
        except Exception as e:
            logger.warning(f"JobSpy Google search failed for '{term}': {e}")
            continue

        if df is None:
            continue
        try:
            rows = list(df.iterrows())
        except Exception:
            continue
        for _, row in rows:
            title = str(row.get("title") or "").strip()
            url = str(row.get("job_url") or "").strip()
            if not title or not url:
                continue
            results.append(
                {
                    "title": title,
                    "company_name": str(row.get("company") or "Unknown").strip(),
                    "url": url,
                    "location": str(
                        row.get("location") or location or "Remote / Various"
                    ),
                    "description": str(row.get("description") or ""),
                    "salary_range": row.get("min_amount")
                    and str(row.get("min_amount"))
                    or None,
                    "source": "Google Jobs (JobSpy)",
                    "source_type": "Google",
                }
            )
    return results


def fetch_google_jobs(
    query: str, location: Optional[str] = None, max_results: int = 15
) -> List[Dict[str, Any]]:
    """
    Direct HTTP scraper for Google for Jobs (ibp=htl;jobs) and SERP Schema.org JobPosting JSON-LD.
    Extracts high-signal job postings with title, company, location, salary, and direct URLs.
    """
    if not query or not query.strip():
        return []

    search_terms = query.strip()
    if location and location.strip():
        search_terms = f"{search_terms} in {location.strip()}"

    encoded_query = urllib.parse.quote(search_terms)
    # Primary URL with Google for Jobs intent
    url = f"https://www.google.com/search?q={encoded_query}&ibp=htl;jobs"

    if not robots_allowed(url):
        logger.warning(
            "robots.txt disallows Google search scraping; skipping direct Google Jobs fetch."
        )
        return []

    jobs = []
    try:
        res = requests.get(url, headers=AGENT_HEADERS, timeout=8)
        if res.status_code == 200:
            soup = BeautifulSoup(res.text, "html.parser")

            # 1. Parse Schema.org JobPosting JSON-LD script blocks
            json_scripts = soup.find_all("script", type="application/ld+json")
            for script in json_scripts:
                try:
                    data = json.loads(script.string or "{}")
                    items = []
                    if isinstance(data, list):
                        items = data
                    elif isinstance(data, dict):
                        if data.get("@type") == "JobPosting":
                            items = [data]
                        elif "@graph" in data:
                            items = [
                                g
                                for g in data["@graph"]
                                if g.get("@type") == "JobPosting"
                            ]

                    for item in items:
                        if len(jobs) >= max_results:
                            break

                        org = item.get("hiringOrganization")
                        company_name = (
                            org.get("name")
                            if isinstance(org, dict)
                            else (str(org) if org else "Google Jobs Partner")
                        )

                        loc = item.get("jobLocation", {})
                        loc_str = "Remote / Various"
                        if isinstance(loc, dict):
                            addr = loc.get("address", {})
                            if isinstance(addr, dict):
                                loc_parts = [
                                    addr.get("addressLocality"),
                                    addr.get("addressRegion"),
                                    addr.get("addressCountry"),
                                ]
                                loc_str = (
                                    ", ".join(p for p in loc_parts if p) or loc_str
                                )
                        elif isinstance(loc, str):
                            loc_str = loc

                        salary_info = "Competitive"
                        sal = item.get("baseSalary")
                        if isinstance(sal, dict):
                            val = sal.get("value", {})
                            if isinstance(val, dict) and val.get("value"):
                                salary_info = (
                                    f"${val.get('value')} {sal.get('currency', 'USD')}"
                                )

                        apply_url = item.get("url") or item.get("sameAs") or url

                        jobs.append(
                            {
                                "title": item.get("title", "Open Opportunity"),
                                "company": company_name,
                                "location": loc_str,
                                "salary_range": salary_info,
                                "description": item.get("description", ""),
                                "url": unwrap_google_redirect_url(apply_url),
                                "posted_at": item.get("datePosted", "Recently posted"),
                                "source": "Google Jobs",
                                "source_type": "Google",
                            }
                        )
                except Exception as e:
                    logger.debug(f"JSON-LD parse error: {e}")

            # 2. Fallback / supplementary DOM parsing for standard Google Job cards
            if len(jobs) < max_results:
                # Target Google Jobs card containers
                cards = soup.find_all(
                    ["div", "li"],
                    class_=lambda c: (
                        c
                        and any(
                            k in str(c)
                            for k in [
                                "PwjeAc",
                                "gWSBe",
                                "iFjolb",
                                "pE8eif",
                                "sp-c",
                                "KLsYvd",
                                "BjJfJf",
                            ]
                        )
                    ),
                )
                for card in cards:
                    if len(jobs) >= max_results:
                        break

                    # Extract title
                    title_el = card.find(
                        ["div", "h2", "a", "span"],
                        class_=lambda c: (
                            c
                            and any(
                                k in str(c)
                                for k in [
                                    "BjJfJf",
                                    "PUpOsf",
                                    "title",
                                    "heading",
                                    "t-bold",
                                ]
                            )
                        ),
                    )
                    title = title_el.get_text(strip=True) if title_el else ""
                    if (
                        not title
                        or len(title) < 3
                        or any(j["title"].lower() == title.lower() for j in jobs)
                    ):
                        continue

                    # Extract company
                    company_el = card.find(
                        ["div", "span"],
                        class_=lambda c: (
                            c
                            and any(
                                k in str(c)
                                for k in ["vNEEBe", "wvy6fc", "company", "employer"]
                            )
                        ),
                    )
                    company = (
                        company_el.get_text(strip=True)
                        if company_el
                        else "Target Employer"
                    )

                    # Extract location
                    loc_el = card.find(
                        ["div", "span"],
                        class_=lambda c: (
                            c
                            and any(
                                k in str(c) for k in ["Qk80nd", "oc1tfd", "location"]
                            )
                        ),
                    )
                    location_str = (
                        loc_el.get_text(strip=True) if loc_el else "Remote / Various"
                    )

                    # Extract link
                    link_el = card.find("a", href=True)
                    job_url = (
                        unwrap_google_redirect_url(link_el["href"])
                        if link_el
                        else f"https://www.google.com/search?q={encoded_query}&ibp=htl;jobs"
                    )

                    # Extract snippet / description
                    snippet_el = card.find(
                        ["div", "span"],
                        class_=lambda c: (
                            c
                            and any(
                                k in str(c)
                                for k in ["HBvxfe", "WbZuDe", "Yflw0c", "snippet"]
                            )
                        ),
                    )
                    snippet = (
                        snippet_el.get_text(" ", strip=True)
                        if snippet_el
                        else f"Opening for {title} at {company}."
                    )

                    jobs.append(
                        {
                            "title": title,
                            "company": company,
                            "location": location_str,
                            "salary_range": "Competitive",
                            "description": snippet,
                            "url": job_url,
                            "posted_at": "Discovered via Google Jobs",
                            "source": "Google Jobs",
                            "source_type": "Google",
                        }
                    )

    except Exception as e:
        logger.warning(f"Direct Google Jobs HTTP scrape encountered: {e}")

    # 3. If Google anti-bot / HTML blocks returned 0 results, query DuckDuckGo
    if not jobs:
        try:
            ddg_query = f"{search_terms} jobs apply site:greenhouse.io OR site:lever.co OR site:ashbyhq.com OR site:workday.com OR careers"
            ddg_results = search_ddg(ddg_query, max_results=max_results)
            for r in ddg_results:
                raw_title = r.get("title", "").strip()
                title_clean = raw_title.split(" - ")[0].split(" | ")[0].strip()
                company_guess = "Direct Employer"
                if " - " in raw_title:
                    company_guess = raw_title.split(" - ")[-1].strip()
                elif " | " in raw_title:
                    company_guess = raw_title.split(" | ")[-1].strip()

                if title_clean and len(title_clean) > 3:
                    jobs.append(
                        {
                            "title": title_clean,
                            "company": company_guess,
                            "location": location or "Remote / Various",
                            "salary_range": "Competitive",
                            "description": r.get(
                                "snippet",
                                f"Opportunity for {title_clean} at {company_guess}",
                            ),
                            "url": r.get("link", ""),
                            "posted_at": "Recently Discovered",
                            "source": "Google Jobs",
                            "source_type": "Google",
                        }
                    )
        except Exception as ddg_err:
            logger.debug(f"DDG fallback for Google Jobs search notice: {ddg_err}")

    # Deduplicate by title + company
    seen = set()
    deduped = []
    for j in jobs:
        key = (j["title"].lower().strip(), j["company"].lower().strip())
        if key not in seen:
            seen.add(key)
            deduped.append(j)

    return deduped[:max_results]


# -------------------------------------------------------------
# Greenhouse Application Status & Active Probe
# -------------------------------------------------------------


def check_greenhouse_application_status(status_url: str) -> Dict[str, Any]:
    """
    Checks the status of an application via Greenhouse tokenized status URLs.
    Extracts status (e.g. In Review, Closed, Interviewing), submission date, job title, and company.
    """
    if not status_url or not status_url.strip():
        return {
            "status": "Unknown",
            "is_active": False,
            "submitted_date": "",
            "job_title": "",
            "company": "",
            "raw_details": "No status URL provided.",
        }

    try:
        res = requests.get(status_url, headers=HEADERS, timeout=12)
        if res.status_code != 200:
            return {
                "status": "Inactive / Not Found",
                "is_active": False,
                "submitted_date": "",
                "job_title": "",
                "company": "",
                "raw_details": f"HTTP {res.status_code}",
            }

        soup = BeautifulSoup(res.text, "html.parser")
        full_text = soup.get_text(" ", strip=True)

        is_closed = any(
            k in full_text.lower()
            for k in [
                "no longer accepting applications",
                "position has been filled",
                "position is closed",
                "job has been closed",
                "application closed",
                "unsuccessful",
                "not moving forward",
            ]
        )

        status_text = "In Review"
        if is_closed:
            status_text = "Closed / Not Selected"
        elif "interview" in full_text.lower():
            status_text = "Interviewing"
        elif "offer" in full_text.lower():
            status_text = "Offer Stage"

        # Extract title from h1, h2, or page title
        job_title = ""
        company = ""

        title_el = soup.find(
            ["h1", "h2", "div", "span"],
            class_=lambda c: (
                c
                and any(
                    k in str(c).lower()
                    for k in ["job-title", "title", "role", "heading"]
                )
            ),
        )
        if title_el:
            job_title = title_el.get_text(strip=True)

        comp_el = soup.find(
            ["div", "span", "h3"],
            class_=lambda c: (
                c
                and any(
                    k in str(c).lower() for k in ["company", "employer", "organization"]
                )
            ),
        )
        if comp_el:
            company = comp_el.get_text(strip=True)

        if not company and soup.title:
            title_parts = soup.title.get_text(strip=True).split("|")
            if len(title_parts) > 1:
                company = title_parts[-1].strip()

        submitted_date = ""
        date_match = re.search(
            r"submitted on\s+([A-Za-z]+\s+\d{1,2},?\s+\d{4})", full_text, re.IGNORECASE
        )
        if date_match:
            submitted_date = date_match.group(1).strip()

        return {
            "status": status_text,
            "is_active": not is_closed,
            "submitted_date": submitted_date,
            "job_title": job_title,
            "company": company,
            "raw_details": full_text[:400],
        }
    except Exception as e:
        logger.warning(f"Error checking Greenhouse application status: {e}")
        return {
            "status": "Error Checking Status",
            "is_active": False,
            "submitted_date": "",
            "job_title": "",
            "company": "",
            "raw_details": str(e),
        }


# Closing phrases used by the generic HTTP probe. Kept lowercase for substring matching.
CLOSED_JOB_MARKERS = (
    "this job is no longer available",
    "position has been closed",
    "job posting has closed",
    "no longer accepting applications",
    "this job has expired",
    "position is closed",
)


def probe_job_url_active(job_url: str, timeout: int = 10) -> bool:
    """
    Portal-agnostic requisition probe, shared as the fallback for every ATS probe.

    Returns True when a listing still looks open and False only when it is clearly closed
    (HTTP 404/410, or a known closing phrase on an otherwise-200 page). Network/parse errors
    are treated as "assume active" so a transient failure never auto-rejects a real application.
    """
    if not job_url or not job_url.strip():
        return False
    try:
        res = requests.get(job_url, headers=HEADERS, timeout=timeout)
        if res.status_code in (404, 410):
            return False
        if res.status_code == 200:
            text = (res.text or "").lower()
            if any(marker in text for marker in CLOSED_JOB_MARKERS):
                return False
            return True
    except Exception as e:
        logger.debug(f"Generic job-active probe failed for {job_url}: {e}")
    return True


def _extract_lever_slug_and_posting_id(job_url: str) -> Tuple[str, str]:
    """Extracts (site_slug, posting_id) from a Lever job/apply or Public API URL."""
    if not job_url or not job_url.strip():
        return "", ""
    try:
        parsed = urllib.parse.urlparse(job_url.strip())
        parts = [p for p in parsed.path.split("/") if p and p.lower() != "apply"]
        slug = parts[0].strip().lower() if parts else ""
        posting_id = parts[1].strip() if len(parts) > 1 else ""
        return slug, posting_id
    except Exception:
        return "", ""


def probe_lever_job_active(job_url: str) -> bool:
    """
    Probes a Lever posting via the Public Postings API.

    Lever's public Postings API only exposes `published` postings, so a closed or unlisted
    requisition returns HTTP 404. Internal/closed postings are invisible to the public API by
    design. Falls back to the shared generic HTTP probe when the site slug or posting id cannot
    be parsed from the URL.
    """
    if not job_url or not job_url.strip():
        return False

    slug, posting_id = _extract_lever_slug_and_posting_id(job_url)
    if slug and posting_id:
        api_url = f"https://api.lever.co/v0/postings/{slug}/{posting_id}"
        try:
            res = requests.get(api_url, headers=HEADERS, timeout=10)
            if res.status_code == 200:
                return True
            if res.status_code in (404, 410):
                return False
        except Exception as e:
            logger.debug(f"Lever API probe failed for {api_url}: {e}")

    return probe_job_url_active(job_url)


def _extract_smartrecruiters_slug_and_posting_id(job_url: str) -> Tuple[str, str]:
    """Extracts (company_identifier, posting_id) from a SmartRecruiters job or API URL."""
    if not job_url or not job_url.strip():
        return "", ""
    slug = extract_slug_from_careers_url(job_url, "smartrecruiters")
    posting_id = ""
    try:
        parsed = urllib.parse.urlparse(
            job_url if "://" in job_url else f"https://{job_url}"
        )
        parts = [p for p in parsed.path.split("/") if p]
        if "postings" in parts:
            idx = parts.index("postings")
            if len(parts) > idx + 1:
                posting_id = parts[idx + 1]
        elif slug:
            for i, p in enumerate(parts):
                if p.lower() == slug.lower() and i + 1 < len(parts):
                    posting_id = parts[i + 1]
                    break
    except Exception:
        pass
    # Public URLs use "<numericId>-<slug>"; keep just the numeric posting id.
    m = re.match(r"^(\d+)-", posting_id)
    if m:
        posting_id = m.group(1)
    return slug, posting_id


def probe_smartrecruiters_job_active(job_url: str) -> bool:
    """
    Probes a SmartRecruiters posting via the public Posting API.

    The posting detail exposes an `active` boolean, so this is a direct open/closed signal
    rather than a 404 heuristic. Falls back to the shared generic HTTP probe when the company
    id or posting id cannot be parsed (or when the API requires auth for that tenant).
    """
    if not job_url or not job_url.strip():
        return False

    slug, posting_id = _extract_smartrecruiters_slug_and_posting_id(job_url)
    if slug and posting_id:
        api_url = (
            f"https://api.smartrecruiters.com/v1/companies/{slug}/postings/{posting_id}"
        )
        try:
            res = requests.get(api_url, headers=HEADERS, timeout=10)
            if res.status_code == 200:
                data = res.json() or {}
                active = data.get("active")
                return True if active is None else bool(active)
            if res.status_code in (404, 410):
                return False
        except Exception as e:
            logger.debug(f"SmartRecruiters API probe failed for {api_url}: {e}")

    return probe_job_url_active(job_url)


def probe_greenhouse_job_active(job_url: str) -> bool:
    """
    Probes if a Greenhouse job requisition remains actively open via Public API or HTTP probe.
    """
    if not job_url or not job_url.strip():
        return False

    slug = extract_slug_from_careers_url(job_url, "greenhouse")

    # Try to extract job ID
    job_id = None
    id_match = re.search(r"/jobs/(\d+)", job_url)
    if not id_match:
        id_match = re.search(r"gh_jid=(\d+)", job_url)
    if id_match:
        job_id = id_match.group(1)

    if slug and job_id:
        api_url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{job_id}"
        try:
            res = requests.get(api_url, headers=HEADERS, timeout=10)
            if res.status_code == 200:
                return True
            elif res.status_code in [404, 410]:
                return False
        except Exception as e:
            logger.debug(f"Greenhouse API probe failed for {api_url}: {e}")

    # Fallback to the shared, portal-agnostic HTTP probe
    return probe_job_url_active(job_url)
