import json
from unittest.mock import MagicMock, patch

import pytest

from backend.matchers import check_title_match as main_check
from backend.matchers import ROLE_EXPANSIONS, check_title_match
from backend.scraper import (
    evaluate_job_due_diligence,
    find_talent_partners,
    gather_company_intelligence,
    scrape_ashby_jobs,
    scrape_custom_jobs,
    scrape_greenhouse_jobs,
    scrape_lever_jobs,
    scrape_smartrecruiters_jobs,
    scrape_uber_jobs,
    scrape_workable_jobs,
    scrape_workday_jobs,
    search_ddg,
    unwrap_shortened_url,
)


# -------------------------------------------------------------
# Unit Tests: Shortened URL Unwrapping
# -------------------------------------------------------------
def test_unwrap_shortened_url_regular_url():
    """Verifies that unwrap_shortened_url leaves regular URLs intact and strips tracking query parameters."""
    url = "https://boards.greenhouse.io/datadog/jobs/12345?utm_source=linkedin&utm_medium=post&trk=abc"
    clean = unwrap_shortened_url(url)
    assert "utm_source" not in clean
    assert "boards.greenhouse.io/datadog/jobs/12345" in clean


@patch("backend.scraper.requests.head")
def test_unwrap_shortened_url_lnkd_in(mock_head):
    """Verifies that unwrap_shortened_url follows HTTP redirects for lnkd.in links."""
    mock_resp = MagicMock()
    mock_resp.url = "https://jobs.lever.co/stripe/staff-backend-eng?utm_source=lnkd"
    mock_head.return_value = mock_resp

    short_url = "https://lnkd.in/eAbCd12"
    result = unwrap_shortened_url(short_url)
    assert "jobs.lever.co/stripe/staff-backend-eng" in result
    assert "utm_source" not in result


# -------------------------------------------------------------
# Unit Tests: DuckDuckGo HTML Search
# -------------------------------------------------------------
@patch("backend.scraper.requests.get")
def test_search_ddg(mock_get):
    sample_html = """
    <html>
      <body>
        <div class="result">
          <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fstripe.com%2Fabout&rut=1">Stripe - Financial Infrastructure</a>
          <a class="result__snippet">Stripe is a financial infrastructure platform for businesses.</a>
        </div>
        <div class="result">
          <a class="result__a" href="https://news.ycombinator.com">Hacker News</a>
          <a class="result__snippet">Tech news and discussions.</a>
        </div>
      </body>
    </html>
    """
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = sample_html
    mock_get.return_value = mock_resp

    results = search_ddg("Stripe financial products", max_results=2)
    assert len(results) == 2
    assert results[0]["title"] == "Stripe - Financial Infrastructure"
    assert results[0]["link"] == "https://stripe.com/about"
    assert "financial infrastructure" in results[0]["snippet"]


def test_search_ddg_empty():
    assert search_ddg("") == []
    assert search_ddg("   ") == []


# -------------------------------------------------------------
# Unit Tests: Greenhouse Careers Scraper
# -------------------------------------------------------------
@patch("backend.scraper.requests.get")
def test_scrape_greenhouse_jobs(mock_get):
    mock_json = {
        "jobs": [
            {
                "title": "Senior Infrastructure Engineer",
                "absolute_url": "https://boards.greenhouse.io/stripe/jobs/12345",
                "content": "<p>We are looking for a <strong>Senior Engineer</strong> to scale our cloud.</p>",
                "location": {"name": "Remote, US"},
            }
        ]
    }
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = mock_json
    mock_get.return_value = mock_resp

    jobs = scrape_greenhouse_jobs("Stripe")
    assert len(jobs) == 1
    assert jobs[0]["title"] == "Senior Infrastructure Engineer"
    assert jobs[0]["url"] == "https://boards.greenhouse.io/stripe/jobs/12345"
    assert "Senior Engineer" in jobs[0]["description"]
    assert "<p>" not in jobs[0]["description"]
    assert jobs[0]["source_type"] == "Direct"


def test_scrape_greenhouse_empty():
    assert scrape_greenhouse_jobs("") == []


# -------------------------------------------------------------
# Unit Tests: Lever Careers Scraper
# -------------------------------------------------------------
@patch("backend.scraper.requests.get")
def test_scrape_lever_jobs(mock_get):
    mock_json = [
        {
            "text": "Staff Software Engineer - Backend",
            "hostedUrl": "https://jobs.lever.co/posthog/abcde",
            "applyUrl": "https://jobs.lever.co/posthog/abcde/apply",
            "descriptionPlain": "Help us build open-source product analytics at high scale.",
            "categories": {"location": "San Francisco, CA"},
        }
    ]
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = mock_json
    mock_get.return_value = mock_resp

    jobs = scrape_lever_jobs("PostHog")
    assert len(jobs) == 1
    assert jobs[0]["title"] == "Staff Software Engineer - Backend"
    assert jobs[0]["url"] == "https://jobs.lever.co/posthog/abcde/apply"
    assert "open-source product analytics" in jobs[0]["description"]
    assert jobs[0]["location"] == "San Francisco, CA"


# -------------------------------------------------------------
# Unit Tests: Ashby Careers Scraper
# -------------------------------------------------------------
@patch("backend.scraper.requests.get")
def test_scrape_ashby_jobs(mock_get):
    mock_json = {
        "jobs": [
            {
                "title": "Member of Technical Staff",
                "jobUrl": "https://jobs.ashbyhq.com/openai/12345",
                "descriptionHtml": "<p>Build reasoning frontier models.</p>",
                "location": "San Francisco, CA",
            }
        ]
    }
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = mock_json
    mock_get.return_value = mock_resp

    jobs = scrape_ashby_jobs("OpenAI")
    assert len(jobs) == 1
    assert jobs[0]["title"] == "Member of Technical Staff"
    assert jobs[0]["url"] == "https://jobs.ashbyhq.com/openai/12345"
    assert "reasoning frontier models" in jobs[0]["description"]


# -------------------------------------------------------------
# Unit Tests: Uber Careers Scraper
# -------------------------------------------------------------
@patch("backend.scraper.requests.post")
def test_scrape_uber_jobs(mock_post):
    mock_json = {
        "data": {
            "results": [
                {
                    "id": "uber-job-999",
                    "title": "Software Engineer II - Backend",
                    "description": "Scale marketplace dispatch systems.",
                    "location": {"city": "Bengaluru", "countryName": "India"},
                }
            ]
        }
    }
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = mock_json
    mock_post.return_value = mock_resp

    jobs = scrape_uber_jobs()
    assert len(jobs) == 1
    assert jobs[0]["title"] == "Software Engineer II - Backend"
    assert "uber-job-999" in jobs[0]["url"]
    assert "Bengaluru, India" in jobs[0]["location"]


# -------------------------------------------------------------
# Unit Tests: Custom Careers Page Scraping
# -------------------------------------------------------------
@patch("backend.scraper._fetch_robots")
def test_robots_allowed_parses_disallow(mock_fetch):
    """robots.txt Disallow rules are honoured; allowed paths pass."""
    from backend.scraper import robots_allowed, _robots_cache

    _robots_cache.clear()
    mock_fetch.return_value = "User-agent: *\nDisallow: /private/"
    assert robots_allowed("https://example.com/private/jobs") is False
    assert robots_allowed("https://example.com/jobs") is True


@patch("backend.scraper.requests.get")
@patch("backend.scraper._fetch_robots", return_value="User-agent: *\nDisallow: /")
def test_scrape_custom_jobs_respects_robots(mock_fetch, mock_get):
    """A robots-disallowed custom portal is skipped without making the page request."""
    from backend.scraper import scrape_custom_jobs, _robots_cache

    _robots_cache.clear()
    assert scrape_custom_jobs("https://example.com/careers") == []
    mock_get.assert_not_called()


@patch("backend.scraper.requests.get")
def test_scrape_custom_jobs(mock_get):
    sample_html = """
    <html>
      <body>
        <a href="/jobs/senior-dev">Senior Backend Developer - Apply Now</a>
        <a href="/careers/lead-designer">Lead Product Designer (Details)</a>
        <a href="/about-us">About Us</a>
      </body>
    </html>
    """
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = sample_html
    mock_get.return_value = mock_resp

    jobs = scrape_custom_jobs("https://example.com/careers")
    assert len(jobs) == 2
    assert jobs[0]["title"] == "Senior Backend Developer - Apply Now"
    assert jobs[0]["url"] == "https://example.com/jobs/senior-dev"
    assert jobs[1]["url"] == "https://example.com/careers/lead-designer"


# -------------------------------------------------------------
# Unit Tests: Company Intelligence & Talent Partners
# -------------------------------------------------------------
@patch("backend.scraper.generate_text")
@patch("backend.scraper.search_ddg")
def test_gather_company_intelligence(mock_search, mock_gen):
    mock_search.return_value = [
        {
            "title": "Company Launches AI Product",
            "link": "https://example.com/news",
            "snippet": "New generative AI tool launched.",
        }
    ]
    mock_gen.return_value = json.dumps(
        {
            "description": "Acme Corp is a leader in cloud developer tools.",
            "recent_news": "Launched new developer platform in Q1.",
            "salary_insights": "$160k - $210k for Senior roles.",
            "hiring_process": "1. Recruiter Screen -> 2. System Design -> 3. Values Alignment.",
            "market_position": "Acme Corp employs 5,000 staff, founded 2011, HQ in Austin, competing with Gitlab and Atlassian.",
            "talking_points": "Ask how the platform balances developer speed with governance; watch the AI tooling trend.",
        }
    )

    intel = gather_company_intelligence("Acme Corp", "Senior Engineer")
    assert set(intel.keys()) == {
        "description",
        "recent_news",
        "salary_insights",
        "hiring_process",
        "market_position",
        "talking_points",
    }
    assert "Acme Corp" in intel["description"]
    assert "$160k" in intel["salary_insights"]
    assert "System Design" in intel["hiring_process"]
    assert "5,000" in intel["market_position"]
    assert "AI tooling" in intel["talking_points"]


@patch("backend.scraper.generate_text")
@patch("backend.scraper.search_ddg")
def test_gather_company_intelligence_new_fields_fallback(mock_search, mock_gen):
    mock_search.return_value = [
        {
            "title": "Company News",
            "link": "https://example.com/news",
            "snippet": "Some snippet.",
        }
    ]
    # Model omits the two new fields entirely.
    mock_gen.return_value = json.dumps(
        {
            "description": "Acme Corp builds tools.",
            "recent_news": "No recent news available.",
            "salary_insights": "Compensation data unavailable.",
            "hiring_process": "Standard technical interview process.",
        }
    )

    intel = gather_company_intelligence("Acme Corp")
    assert set(intel.keys()) == {
        "description",
        "recent_news",
        "salary_insights",
        "hiring_process",
        "market_position",
        "talking_points",
    }
    assert intel["market_position"] == "Not found in sources."
    assert intel["talking_points"] == "Not found in sources."


@patch("backend.scraper.search_ddg")
def test_gather_company_intelligence_empty_name(mock_search):
    intel = gather_company_intelligence("")
    assert set(intel.keys()) == {
        "description",
        "recent_news",
        "salary_insights",
        "hiring_process",
        "market_position",
        "talking_points",
    }
    mock_search.assert_not_called()


@patch("backend.scraper.search_ddg")
def test_find_talent_partners(mock_search):
    mock_search.return_value = [
        {
            "title": "Jane Doe - Technical Recruiter - Stripe | LinkedIn",
            "link": "https://www.linkedin.com/in/janedoe-recruiter",
            "snippet": "Technical Recruiter hiring for infrastructure engineering.",
        },
        {
            "title": "Random Blog Post",
            "link": "https://medium.com/some-article",
            "snippet": "Article about recruiting.",
        },
    ]

    contacts = find_talent_partners("Stripe", "Senior Engineer")
    assert len(contacts) == 1
    assert contacts[0]["name"] == "Jane Doe"
    assert "linkedin.com/in/janedoe" in contacts[0]["profile_url"]


# -------------------------------------------------------------
# Unit Tests: Due Diligence & Ghost Job Detection
# -------------------------------------------------------------
def test_evaluate_job_due_diligence():
    portal_jobs = [
        {"title": "Staff Backend Engineer", "url": "https://portal.com/job/1"},
        {"title": "Product Designer", "url": "https://portal.com/job/2"},
    ]

    # Case 1: Direct genuine job
    res1 = evaluate_job_due_diligence("Staff Backend Engineer", "Direct", portal_jobs)
    assert res1["is_ghost_job"] is False
    assert res1["status_label"] == "Genuine Opportunity"

    # Case 2: LinkedIn alert job NOT on direct portal -> Ghost job
    res2 = evaluate_job_due_diligence(
        "Senior Machine Learning Engineer", "LinkedIn", portal_jobs
    )
    assert res2["is_ghost_job"] is True
    assert "not found on the active company careers portal" in res2["reason"]

    # Case 3: Repeated repost count >= 3 -> Ghost job
    res3 = evaluate_job_due_diligence(
        "Staff Backend Engineer", "Direct", portal_jobs, repost_count=4
    )
    assert res3["is_ghost_job"] is True
    assert "Repeated Repost" in res3["status_label"]


def test_extract_portal_info_from_job_url():
    from backend.scraper import extract_portal_info_from_job_url

    # Case 1: LinkedIn wrapped external apply link
    wrapped_gh = "https://www.linkedin.com/jobs/view/externalApply/4155123456?url=https%3A%2F%2Fboards.greenhouse.io%2Fstripe%2Fjobs%2F5892019&urlHash=abc"
    info1 = extract_portal_info_from_job_url(wrapped_gh, "Stripe")
    assert info1 is not None
    assert info1["portal_type"] == "Greenhouse"
    assert info1["careers_url"] == "https://boards.greenhouse.io/stripe"
    assert "boards.greenhouse.io/stripe/jobs/5892019" in info1["canonical_apply_url"]

    # Case 2: Lever link
    lever_url = "https://jobs.lever.co/netflix/634789ab"
    info2 = extract_portal_info_from_job_url(lever_url, "Netflix")
    assert info2["portal_type"] == "Lever"
    assert info2["careers_url"] == "https://jobs.lever.co/netflix"

    # Case 3: Ashby link
    ashby_url = "https://jobs.ashbyhq.com/openai/1298410"
    info3 = extract_portal_info_from_job_url(ashby_url, "OpenAI")
    assert info3["portal_type"] == "Ashby"
    assert info3["careers_url"] == "https://jobs.ashbyhq.com/openai"

    # Case 4: Workday link (locale segment must be dropped from the board URL)
    workday_url = "https://snowflake.wd1.myworkdayjobs.com/en-US/Snowflake_Careers/job/Bengaluru/Staff-Engineer_R1234"
    info4 = extract_portal_info_from_job_url(workday_url, "Snowflake")
    assert info4["portal_type"] == "Workday"
    assert (
        info4["careers_url"]
        == "https://snowflake.wd1.myworkdayjobs.com/Snowflake_Careers"
    )

    # Case 5: SmartRecruiters link
    sr_url = (
        "https://jobs.smartrecruiters.com/Acme/744000000000001-staff-software-engineer"
    )
    info5 = extract_portal_info_from_job_url(sr_url, "Acme")
    assert info5["portal_type"] == "SmartRecruiters"
    assert info5["careers_url"] == "https://careers.smartrecruiters.com/acme"

    # Case 6: Workable link
    wk_url = "https://apply.workable.com/acme/j/ABC001/"
    info6 = extract_portal_info_from_job_url(wk_url, "Acme")
    assert info6["portal_type"] == "Workable"
    assert info6["careers_url"] == "https://apply.workable.com/acme/"


# -------------------------------------------------------------
# Regression: early ATS pre-filter must match the semantic matcher
# -------------------------------------------------------------
def test_title_prefilter_is_consistent_with_final_matcher():
    """The ATS early filter and the endpoint's final check must use identical semantics."""
    assert check_title_match is main_check
    assert isinstance(ROLE_EXPANSIONS, dict) and ROLE_EXPANSIONS

    cases = [
        ("Senior Software Engineer", ["senior staff software engineer"]),
        ("Staff Engineer, Backend", ["staff software engineer"]),
        ("Principal Engineer", ["principal engineer"]),
        ("Lead Product Designer", ["technical lead"]),
        ("Product Designer", ["staff software engineer"]),
    ]
    for title, filters in cases:
        assert check_title_match(title, filters) == main_check(title, filters)


@patch("backend.scraper.requests.get")
def test_greenhouse_prefilter_keeps_semantic_matches(mock_get):
    """
    Regression: strict, substring-only early filters starved ATS scans.
    A title that matches semantically but is not a literal substring of the filter must survive.
    """
    mock_json = {
        "jobs": [
            {
                "title": "Senior Software Engineer",
                "absolute_url": "https://boards.greenhouse.io/acme/jobs/1",
                "content": "",
                "location": {"name": "Remote, India"},
            },
            {
                "title": "Product Designer",
                "absolute_url": "https://boards.greenhouse.io/acme/jobs/2",
                "content": "",
                "location": {"name": "Remote, India"},
            },
        ]
    }
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = mock_json
    mock_get.return_value = mock_resp

    jobs = scrape_greenhouse_jobs(
        "Acme",
        title_filters=["senior staff software engineer"],
        location_filters=["india"],
        work_mode="remote",
    )
    titles = [j["title"] for j in jobs]
    assert "Senior Software Engineer" in titles
    assert "Product Designer" not in titles


def test_detect_foreign_restriction_us_abbreviations():
    """US/metro abbreviations (SF, NY, TX) must be recognised as foreign when not targeted."""
    from backend.matchers import detect_foreign_restriction

    targets = ["india", "united kingdom", "singapore", "ireland"]
    assert detect_foreign_restriction("sf, ny, remote", targets) is True
    assert detect_foreign_restriction("austin, tx, remote", targets) is True
    assert detect_foreign_restriction("remote, us", targets) is True
    assert detect_foreign_restriction("remote, canada", targets) is True
    assert detect_foreign_restriction("remote, london", targets) is False
    assert detect_foreign_restriction("remote, singapore", targets) is False
    assert detect_foreign_restriction("remote, india", targets) is False


# -------------------------------------------------------------
# Regression: ATS scrapers must forward the candidate's filters to the API payload
# -------------------------------------------------------------
@patch("backend.scraper.requests.get")
def test_scrape_lever_jobs_applies_title_and_location_filters(mock_get):
    """Passed filters must actually exclude non-matching API rows (not be silently dropped)."""
    mock_json = [
        {
            "text": "Staff Software Engineer - Backend",
            "hostedUrl": "https://jobs.lever.co/posthog/1",
            "applyUrl": "https://jobs.lever.co/posthog/1/apply",
            "descriptionPlain": "Backend at scale.",
            "categories": {"location": "Bengaluru, India"},
        },
        {
            "text": "Product Designer",
            "hostedUrl": "https://jobs.lever.co/posthog/2",
            "applyUrl": "https://jobs.lever.co/posthog/2/apply",
            "descriptionPlain": "Design systems.",
            "categories": {"location": "Bengaluru, India"},
        },
        {
            "text": "Staff Software Engineer - Backend",
            "hostedUrl": "https://jobs.lever.co/posthog/3",
            "applyUrl": "https://jobs.lever.co/posthog/3/apply",
            "descriptionPlain": "Backend in the US.",
            "categories": {"location": "San Francisco, CA"},
        },
    ]
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = mock_json
    mock_get.return_value = mock_resp

    jobs = scrape_lever_jobs(
        "PostHog",
        title_filters=["staff software engineer"],
        location_filters=["india"],
    )
    assert len(jobs) == 1
    assert jobs[0]["url"].endswith("/1/apply")
    assert all("Designer" not in j["title"] for j in jobs)


# -------------------------------------------------------------
# Unit Tests: SmartRecruiters Posting API
# -------------------------------------------------------------
def _smartrecruiters_fake_get(list_payload, detail_payload):
    """Returns a requests.get side_effect that distinguishes list vs detail calls."""

    def _fake_get(url, headers=None, timeout=None, params=None):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = (
            detail_payload if url.rstrip("/").split("/")[-1].isdigit() else list_payload
        )
        return resp

    return _fake_get


@patch("backend.scraper.requests.get")
def test_scrape_smartrecruiters_jobs(mock_get):
    list_payload = {
        "totalFound": 1,
        "limit": 100,
        "offset": 0,
        "content": [
            {
                "id": "744000000000001",
                "name": "Staff Software Engineer",
                "ref": "https://api.smartrecruiters.com/v1/companies/Acme/postings/744000000000001",
                "location": {
                    "city": "Bengaluru",
                    "region": "Karnataka",
                    "country": "India",
                    "remote": False,
                },
                "department": {"label": "Engineering"},
            }
        ],
    }
    detail_payload = {
        "id": "744000000000001",
        "name": "Staff Software Engineer",
        "applyUrl": "https://jobs.smartrecruiters.com/Acme/744000000000001-staff-software-engineer",
        "jobAd": {
            "sections": {
                "jobDescription": {
                    "title": "Job Description",
                    "text": "<p>Build distributed systems.</p>",
                }
            }
        },
    }
    mock_get.side_effect = _smartrecruiters_fake_get(list_payload, detail_payload)

    jobs = scrape_smartrecruiters_jobs("Acme")
    assert len(jobs) == 1
    assert jobs[0]["title"] == "Staff Software Engineer"
    assert (
        jobs[0]["url"]
        == "https://jobs.smartrecruiters.com/Acme/744000000000001-staff-software-engineer"
    )
    assert "distributed systems" in jobs[0]["description"]
    assert "<p>" not in jobs[0]["description"]
    assert "Bengaluru" in jobs[0]["location"]
    assert jobs[0]["source"] == "SmartRecruiters Portal"

    # The list call must paginate via the API's `limit`/`offset` parameters.
    list_params = mock_get.call_args_list[0].kwargs.get("params") or {}
    assert list_params.get("limit") == 100
    assert list_params.get("offset") == 0


@patch("backend.scraper.requests.get")
def test_scrape_smartrecruiters_jobs_applies_filters(mock_get):
    list_payload = {
        "totalFound": 2,
        "limit": 100,
        "offset": 0,
        "content": [
            {
                "id": "1",
                "name": "Staff Software Engineer",
                "location": {
                    "city": "Bengaluru",
                    "region": "Karnataka",
                    "country": "India",
                },
            },
            {
                "id": "2",
                "name": "Product Designer",
                "location": {
                    "city": "San Francisco",
                    "region": "CA",
                    "country": "United States",
                },
            },
        ],
    }
    detail_payload = {
        "id": "1",
        "applyUrl": "https://jobs.smartrecruiters.com/Acme/1",
        "jobAd": {"sections": {}},
    }
    mock_get.side_effect = _smartrecruiters_fake_get(list_payload, detail_payload)

    jobs = scrape_smartrecruiters_jobs(
        "Acme", title_filters=["staff software engineer"], location_filters=["india"]
    )
    assert len(jobs) == 1
    assert jobs[0]["title"] == "Staff Software Engineer"
    assert all("Designer" not in j["title"] for j in jobs)


# -------------------------------------------------------------
# Unit Tests: Workable Public Jobs API
# -------------------------------------------------------------
@patch("backend.scraper.requests.get")
def test_scrape_workable_jobs(mock_get):
    payload = {
        "name": "Acme",
        "jobs": [
            {
                "title": "Staff Backend Engineer",
                "shortcode": "ABC001",
                "url": "https://apply.workable.com/acme/j/ABC001/",
                "city": "Bengaluru",
                "state": "Karnataka",
                "country": "India",
                "telecommuting": False,
                "description": "<p>Build APIs at scale.</p>",
            },
            {
                "title": "Remote Platform Engineer",
                "shortcode": "ABC003",
                "url": "https://apply.workable.com/acme/j/ABC003/",
                "city": "",
                "state": "",
                "country": "",
                "telecommuting": True,
                "description": "Fully remote role.",
            },
        ],
    }
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = payload
    mock_get.return_value = mock_resp

    jobs = scrape_workable_jobs("Acme")
    assert len(jobs) == 2
    assert jobs[0]["title"] == "Staff Backend Engineer"
    assert "Build APIs at scale" in jobs[0]["description"]
    assert "<p>" not in jobs[0]["description"]
    assert "Bengaluru" in jobs[0]["location"]
    assert "Remote" in jobs[1]["location"]
    assert jobs[0]["source"] == "Workable Portal"


@patch("backend.scraper.requests.get")
def test_scrape_workable_jobs_applies_title_and_work_mode_filters(mock_get):
    payload = {
        "name": "Acme",
        "jobs": [
            {
                "title": "Staff Backend Engineer",
                "url": "https://apply.workable.com/acme/j/1/",
                "city": "Bengaluru",
                "state": "Karnataka",
                "country": "India",
                "telecommuting": False,
                "description": "x",
            },
            {
                "title": "Product Designer",
                "url": "https://apply.workable.com/acme/j/2/",
                "city": "Bengaluru",
                "state": "Karnataka",
                "country": "India",
                "telecommuting": False,
                "description": "y",
            },
            {
                "title": "Staff Backend Engineer (Remote)",
                "url": "https://apply.workable.com/acme/j/3/",
                "city": "",
                "state": "",
                "country": "",
                "telecommuting": True,
                "description": "z",
            },
        ],
    }
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = payload
    mock_get.return_value = mock_resp

    jobs = scrape_workable_jobs(
        "Acme", title_filters=["backend engineer"], work_mode="remote"
    )
    assert len(jobs) == 1
    assert jobs[0]["url"].endswith("/3/")


# -------------------------------------------------------------
# Unit Tests: Workday CXS API
# -------------------------------------------------------------
def test_parse_workday_board_skips_locale_and_reads_tenant_shard():
    from backend.scraper import _parse_workday_board

    parsed = _parse_workday_board(
        "https://snowflake.wd1.myworkdayjobs.com/en-US/Snowflake_Careers/job/Bengaluru/Staff_R1"
    )
    assert parsed == {
        "tenant": "snowflake",
        "shard": "wd1",
        "site": "Snowflake_Careers",
        "host": "snowflake.wd1.myworkdayjobs.com",
    }
    assert (
        _parse_workday_board("https://intel.wd1.myworkdayjobs.com/External")["site"]
        == "External"
    )
    assert _parse_workday_board("Acme Corp") is None


@patch("backend.scraper.requests.get")
@patch("backend.scraper.requests.post")
def test_scrape_workday_jobs(mock_post, mock_get):
    list_resp = MagicMock(status_code=200)
    list_resp.json.return_value = {
        "total": 1,
        "jobPostings": [
            {
                "title": "Staff Software Engineer",
                "externalPath": "/en-US/Snowflake_Careers/job/Bengaluru/Staff-Engineer_R123",
                "locationsText": "Bengaluru, India",
                "postedOn": "Posted 5 Days Ago",
            }
        ],
    }
    mock_post.return_value = list_resp

    detail_resp = MagicMock(status_code=200)
    detail_resp.json.return_value = {
        "jobPostingInfo": {
            "title": "Staff Software Engineer",
            "location": "Bengaluru, India",
            "jobDescription": "<p>Build the data platform.</p>",
        }
    }
    mock_get.return_value = detail_resp

    jobs = scrape_workday_jobs(
        "Snowflake",
        careers_url="https://snowflake.wd1.myworkdayjobs.com/Snowflake_Careers",
    )
    assert len(jobs) == 1
    assert jobs[0]["title"] == "Staff Software Engineer"
    assert jobs[0]["url"] == (
        "https://snowflake.wd1.myworkdayjobs.com/en-US/Snowflake_Careers/job/Bengaluru/Staff-Engineer_R123"
    )
    assert "data platform" in jobs[0]["description"]
    assert "<p>" not in jobs[0]["description"]
    assert jobs[0]["source"] == "Workday Portal"

    # Pagination goes through the CXS API body, not the URL.
    body = mock_post.call_args.kwargs["json"]
    assert body["limit"] == 20 and body["offset"] == 0 and body["searchText"] == ""
    assert mock_post.call_args.args[0] == (
        "https://snowflake.wd1.myworkdayjobs.com/wday/cxs/snowflake/Snowflake_Careers/jobs"
    )
    # Detail path must not duplicate the locale/site segment.
    assert mock_get.call_args.args[0] == (
        "https://snowflake.wd1.myworkdayjobs.com/wday/cxs/snowflake/Snowflake_Careers/job/Bengaluru/Staff-Engineer_R123"
    )


@patch("backend.scraper.requests.get")
@patch("backend.scraper.requests.post")
def test_scrape_workday_jobs_applies_filters(mock_post, mock_get):
    list_resp = MagicMock(status_code=200)
    list_resp.json.return_value = {
        "total": 2,
        "jobPostings": [
            {
                "title": "Staff Software Engineer",
                "externalPath": "/job/1",
                "locationsText": "Bengaluru, India",
            },
            {
                "title": "Product Designer",
                "externalPath": "/job/2",
                "locationsText": "San Francisco, CA",
            },
        ],
    }
    mock_post.return_value = list_resp
    mock_get.return_value = MagicMock(
        status_code=200, json=MagicMock(return_value={"jobPostingInfo": {}})
    )

    jobs = scrape_workday_jobs(
        "Snowflake",
        title_filters=["staff software engineer"],
        location_filters=["india"],
        careers_url="https://snowflake.wd1.myworkdayjobs.com/Snowflake_Careers",
    )
    assert len(jobs) == 1
    assert jobs[0]["title"] == "Staff Software Engineer"
    assert all("Designer" not in j["title"] for j in jobs)
