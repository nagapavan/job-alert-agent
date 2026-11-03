import pytest
import json
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

from backend.main import app
from backend.database import Job, Company, Resume, OperationLog, ApplicationEvent
from backend.scraper import (
    fetch_google_jobs,
    parse_google_alerts_digest,
    unwrap_google_redirect_url,
)
from backend.playwright_app import fetch_google_jobs_playwright

client = TestClient(app)

# -------------------------------------------------------------
# Unit Tests: Google Redirect URL Unwrapping
# -------------------------------------------------------------


def test_unwrap_google_redirect_url():
    # Google redirect with url parameter
    redirect_url = "https://www.google.com/url?rct=j&sa=t&url=https%3A%2F%2Fcareers.stripe.com%2Fjobs%2F123&ct=ga&cd=CAEYACoT"
    assert (
        unwrap_google_redirect_url(redirect_url)
        == "https://careers.stripe.com/jobs/123"
    )

    # Google redirect with q parameter
    redirect_q = "https://www.google.com/url?q=https%3A%2F%2Fjobs.lever.co%2Fdatabricks%2F456&sa=U"
    assert (
        unwrap_google_redirect_url(redirect_q) == "https://jobs.lever.co/databricks/456"
    )

    # Clean URL
    direct_url = "https://boards.greenhouse.io/anthropic/jobs/789"
    assert unwrap_google_redirect_url(direct_url) == direct_url

    # Empty
    assert unwrap_google_redirect_url("") == ""


# -------------------------------------------------------------
# Unit Tests: Google Alerts Digest Parser
# -------------------------------------------------------------


def test_parse_google_alerts_digest():
    mock_email_html = """
    <html>
        <body>
            <table class="content">
                <tr>
                    <td>
                        <a href="https://www.google.com/url?rct=j&sa=t&url=https%3A%2F%2Fcareers.openai.com%2Fjobs%2Fsenior-distributed-systems-engineer">
                            Senior Distributed Systems Engineer
                        </a>
                        <div>OpenAI is seeking a Senior Distributed Systems Engineer for model inference infrastructure. Location: San Francisco, CA / Remote.</div>
                    </td>
                </tr>
                <tr>
                    <td>
                        <a href="https://www.google.com/url?rct=j&sa=t&url=https%3A%2F%2Fboards.greenhouse.io%2Ffigma%2Fjobs%2Fbackend-lead">
                            Backend Engineering Lead
                        </a>
                        <div>Figma is looking for a Backend Engineering Lead to scale collaborative canvas systems. Location: Remote.</div>
                    </td>
                </tr>
            </table>
            <a href="https://www.google.com/alerts/manage">Manage Alerts</a>
        </body>
    </html>
    """

    jobs = parse_google_alerts_digest(
        mock_email_html, email_subject="Google Alert - Distributed Systems Engineer"
    )
    assert len(jobs) == 2
    assert jobs[0]["title"] == "Senior Distributed Systems Engineer"
    assert (
        jobs[0]["url"]
        == "https://careers.openai.com/jobs/senior-distributed-systems-engineer"
    )
    assert jobs[0]["source"] == "Google Alerts"
    assert jobs[0]["source_type"] == "Google"

    assert jobs[1]["title"] == "Backend Engineering Lead"
    assert jobs[1]["url"] == "https://boards.greenhouse.io/figma/jobs/backend-lead"


def test_parse_google_alerts_digest_empty():
    assert parse_google_alerts_digest("") == []
    assert parse_google_alerts_digest(None) == []


# -------------------------------------------------------------
# Unit Tests: Google Jobs Direct HTTP Scraper
# -------------------------------------------------------------


def test_fetch_google_jobs_json_ld():
    mock_json_ld_html = """
    <html>
        <head>
            <script type="application/ld+json">
            {
                "@context": "https://schema.org/",
                "@type": "JobPosting",
                "title": "Staff Platform Engineer",
                "description": "Lead core infrastructure and Kubernetes service mesh orchestration.",
                "datePosted": "2026-09-01",
                "hiringOrganization": {
                    "@type": "Organization",
                    "name": "Cloudflare"
                },
                "jobLocation": {
                    "@type": "Place",
                    "address": {
                        "addressLocality": "San Francisco",
                        "addressRegion": "CA",
                        "addressCountry": "USA"
                    }
                },
                "baseSalary": {
                    "@type": "MonetaryAmount",
                    "currency": "USD",
                    "value": {
                        "@type": "QuantitativeValue",
                        "value": "210000 - 260000"
                    }
                },
                "url": "https://www.cloudflare.com/careers/jobs/staff-platform-eng"
            }
            </script>
        </head>
        <body>
        </body>
    </html>
    """

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = mock_json_ld_html

    with patch("backend.scraper.requests.get", return_value=mock_resp):
        jobs = fetch_google_jobs(
            "Staff Platform Engineer", location="Remote", max_results=5
        )
        assert len(jobs) == 1
        assert jobs[0]["title"] == "Staff Platform Engineer"
        assert jobs[0]["company"] == "Cloudflare"
        assert jobs[0]["location"] == "San Francisco, CA, USA"
        assert "$210000 - 260000 USD" in jobs[0]["salary_range"]
        assert jobs[0]["source"] == "Google Jobs"
        assert jobs[0]["source_type"] == "Google"


def test_fetch_google_jobs_dom_cards_fallback():
    mock_dom_html = """
    <html>
        <body>
            <div class="PwjeAc">
                <div class="BjJfJf">Principal AI Infrastructure Engineer</div>
                <div class="vNEEBe">Cohere</div>
                <div class="Qk80nd">Remote, USA</div>
                <div class="HBvxfe">Build large scale training clusters with high-throughput network topologies.</div>
                <a href="https://www.google.com/url?q=https%3A%2F%2Fjobs.ashbyhq.com%2Fcohere%2Fprincipal-ai-infra">Apply on Company Site</a>
            </div>
        </body>
    </html>
    """

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = mock_dom_html

    with patch("backend.scraper.requests.get", return_value=mock_resp):
        jobs = fetch_google_jobs("Principal AI Infrastructure", max_results=5)
        assert len(jobs) == 1
        assert jobs[0]["title"] == "Principal AI Infrastructure Engineer"
        assert jobs[0]["company"] == "Cohere"
        assert jobs[0]["location"] == "Remote, USA"
        assert jobs[0]["url"] == "https://jobs.ashbyhq.com/cohere/principal-ai-infra"


def test_fetch_google_jobs_empty():
    assert fetch_google_jobs("") == []
    assert fetch_google_jobs(None) == []


# -------------------------------------------------------------
# Unit Tests: Google Jobs Playwright Browser Fetcher
# -------------------------------------------------------------


def test_fetch_google_jobs_playwright():
    mock_p = MagicMock()
    mock_context = MagicMock()
    mock_page = MagicMock()
    mock_p.chromium.launch_persistent_context.return_value = mock_context
    mock_context.new_page.return_value = mock_page

    mock_card = MagicMock()
    mock_title = MagicMock()
    mock_title.inner_text.return_value = "Senior Rust Systems Architect"
    mock_comp = MagicMock()
    mock_comp.inner_text.return_value = "RustLab"
    mock_loc = MagicMock()
    mock_loc.inner_text.return_value = "Remote"
    mock_link = MagicMock()
    mock_link.get_attribute.return_value = "https://careers.rustlab.io/jobs/1"
    mock_snippet = MagicMock()
    mock_snippet.inner_text.return_value = (
        "Building async distributed storage engines in Rust."
    )

    mock_card.query_selector.side_effect = lambda sel: {
        "div.BjJfJf, div.PUpOsf, div[role='heading'], h2, a.title": mock_title,
        "div.vNEEBe, div.wvy6fc, span.company, div.company": mock_comp,
        "div.Qk80nd, div.oc1tfd, span.location": mock_loc,
        "a[href]": mock_link,
        "div.HBvxfe, span.WbZuDe, div.Yflw0c": mock_snippet,
    }.get(sel, None)

    mock_page.query_selector_all.return_value = [mock_card]

    with patch(
        "backend.playwright_app.get_persistent_browser_context",
        return_value=mock_context,
    ):
        jobs = fetch_google_jobs_playwright(
            mock_p, "Rust Systems Architect", headless=True, max_results=5
        )
        assert len(jobs) == 1
        assert jobs[0]["title"] == "Senior Rust Systems Architect"
        assert jobs[0]["company"] == "RustLab"
        assert jobs[0]["url"] == "https://careers.rustlab.io/jobs/1"


def test_fetch_google_jobs_playwright_reads_detail_pane():
    """The JD pane (not the one-line snippet) becomes `description` (issue #77)."""
    mock_p = MagicMock()
    mock_context = MagicMock()
    mock_page = MagicMock()
    mock_p.chromium.launch_persistent_context.return_value = mock_context
    mock_context.new_page.return_value = mock_page

    mock_card = MagicMock()
    mock_title = MagicMock()
    mock_title.inner_text.return_value = "Principal Engineer"
    mock_comp = MagicMock()
    mock_comp.inner_text.return_value = "Acme"
    mock_loc = MagicMock()
    mock_loc.inner_text.return_value = "Hyderabad"
    mock_link = MagicMock()
    mock_link.get_attribute.return_value = "https://acme.example/jobs/1"
    mock_card.query_selector.side_effect = lambda sel: {
        "div.BjJfJf, div.PUpOsf, div[role='heading'], h2, a.title": mock_title,
        "div.vNEEBe, div.wvy6fc, span.company, div.company": mock_comp,
        "div.Qk80nd, div.oc1tfd, span.location": mock_loc,
        "a[href]": mock_link,
    }.get(sel, None)

    mock_page.query_selector_all.return_value = [mock_card]

    detail = MagicMock()
    detail.inner_text.return_value = (
        "Full JD body: responsibilities, requirements, and qualifications. " * 3
    )
    mock_page.query_selector.side_effect = lambda sel: (
        detail if sel in ("div.KLsYvd", "div[jsname='bN97Pc']") else None
    )

    with patch(
        "backend.playwright_app.get_persistent_browser_context",
        return_value=mock_context,
    ):
        jobs = fetch_google_jobs_playwright(
            mock_p, "Principal Engineer", headless=True, max_results=5
        )
    assert len(jobs) == 1
    assert jobs[0]["description"].startswith("Full JD body:")


def test_fetch_google_jobs_playwright_pagination_is_bounded(monkeypatch):
    """Pagination scrolls at most _GOOGLE_JOBS_MAX_SCROLLS times (issue #77)."""
    import backend.playwright_app as pw

    monkeypatch.setattr(pw, "_GOOGLE_JOBS_MAX_SCROLLS", 2)

    mock_p = MagicMock()
    mock_context = MagicMock()
    mock_page = MagicMock()
    mock_p.chromium.launch_persistent_context.return_value = mock_context
    mock_context.new_page.return_value = mock_page

    counter = {"n": 0}

    def make_card(title):
        card = MagicMock()
        t = MagicMock()
        t.inner_text.return_value = title
        c = MagicMock()
        c.inner_text.return_value = "Acme"
        l = MagicMock()
        l.inner_text.return_value = "Remote"
        link = MagicMock()
        link.get_attribute.return_value = "https://acme.example/jobs/1"
        card.query_selector.side_effect = lambda sel: {
            "div.BjJfJf, div.PUpOsf, div[role='heading'], h2, a.title": t,
            "div.vNEEBe, div.wvy6fc, span.company, div.company": c,
            "div.Qk80nd, div.oc1tfd, span.location": l,
            "a[href]": link,
        }.get(sel, None)
        return card

    def fresh_cards(_sel):
        # Each scroll reveals one new card (distinct title) so the list keeps growing.
        counter["n"] += 1
        return [make_card(f"Engineer {counter['n']}")]

    mock_page.query_selector_all.side_effect = fresh_cards

    with patch(
        "backend.playwright_app.get_persistent_browser_context",
        return_value=mock_context,
    ):
        jobs = fetch_google_jobs_playwright(
            mock_p, "Engineer", headless=True, max_results=15
        )
    # With the cap at 2 scrolls, at most 2 pages of cards are read.
    assert len(jobs) <= 2
    assert mock_page.mouse.wheel.call_count <= 2


def test_fetch_google_jobs_followed_queries_playwright():
    from backend.playwright_app import fetch_google_jobs_followed_queries_playwright

    mock_p = MagicMock()
    mock_context = MagicMock()
    mock_page = MagicMock()
    mock_p.chromium.launch_persistent_context.return_value = mock_context
    mock_context.new_page.return_value = mock_page

    mock_tab = MagicMock()
    mock_tab.inner_text.return_value = "Following"
    mock_page.query_selector_all.side_effect = lambda sel: (
        [mock_tab]
        if "tab" in sel
        else [
            MagicMock(
                inner_text=lambda: (
                    "Product companies Senior Staff Software Engineer jobs in India\nEmail to: user@example.com\nDaily"
                )
            ),
            MagicMock(inner_text=lambda: "Notion india jobs\nWeekly"),
        ]
    )

    with patch(
        "backend.playwright_app.get_persistent_browser_context",
        return_value=mock_context,
    ):
        queries = fetch_google_jobs_followed_queries_playwright(mock_p, headless=True)
        assert len(queries) == 2
        assert (
            "Product companies Senior Staff Software Engineer jobs in India" in queries
        )
        assert "Notion india jobs" in queries


# -------------------------------------------------------------
# Integration Tests: Google Jobs Queries & Followed Tab Sync
# -------------------------------------------------------------


def test_api_google_jobs_queries_crud():
    # GET queries
    res = client.get("/api/jobs/google-jobs/queries")
    assert res.status_code == 200
    data = res.json()
    assert "queries" in data
    assert isinstance(data["queries"], list)

    # POST queries
    test_queries = [
        "Product companies Senior Staff Software Engineer jobs in India",
        "Principal Engineer jobs Bangalore",
    ]
    res_update = client.post(
        "/api/jobs/google-jobs/queries", json={"queries": test_queries}
    )
    assert res_update.status_code == 200
    assert res_update.json()["queries"] == test_queries

    # Verify updated via GET
    res_get = client.get("/api/jobs/google-jobs/queries")
    assert res_get.json()["queries"] == test_queries


def test_api_google_jobs_sync_followed():
    mock_followed = [
        "Product companies Senior Staff Software Engineer jobs in India",
        "Senior Data Analyst jobs in Hyderabad",
        "Notion india jobs",
    ]
    with patch(
        "backend.main.fetch_google_jobs_followed_queries_playwright",
        return_value=mock_followed,
    ):
        res = client.post("/api/jobs/google-jobs/sync-followed")
        assert res.status_code == 200
        data = res.json()
        assert "queries" in data
        assert "new_queries_found" in data
        for q in mock_followed:
            assert q in data["queries"]


# -------------------------------------------------------------
# Integration Tests: Google Jobs Scrape API Endpoint
# -------------------------------------------------------------


def test_api_google_jobs_scrape_endpoint(db_session):
    mock_jobs = [
        {
            "title": "Lead Site Reliability Engineer",
            "company": "Fastly",
            "location": "Remote, Global",
            "salary_range": "$190k - $240k",
            "description": "Scale global edge network and DNS infrastructure.",
            "url": "https://boards.greenhouse.io/fastly/jobs/lead-sre",
            "source": "Google Jobs",
            "source_type": "Google",
        }
    ]

    with patch("backend.main.fetch_google_jobs_playwright", return_value=mock_jobs):
        res = client.post(
            "/api/jobs/google-jobs/scrape",
            json={
                "query": "Lead Site Reliability Engineer",
                "location": "Remote",
                "limit": 5,
                "use_browser": False,
            },
        )
        assert res.status_code == 200
        data = res.json()
        assert data["total_discovered"] == 1
        assert data["total_added"] == 1
        assert data["already_existing"] == 0
        assert len(data["queries_scanned"]) == 1
        assert len(data["per_query_breakdown"]) == 1

        # Verify DB ingestion & company portal elevation
        job_in_db = (
            db_session.query(Job)
            .filter(Job.title == "Lead Site Reliability Engineer")
            .first()
        )
        assert job_in_db is not None
        assert job_in_db.source_type == "Google"
        assert job_in_db.embedding is not None  # Vector embedding populated

        comp_in_db = db_session.query(Company).filter(Company.name == "Fastly").first()
        assert comp_in_db is not None
        assert "greenhouse.io" in comp_in_db.careers_url

        # Test deduplication on re-scrape
        res2 = client.post(
            "/api/jobs/google-jobs/scrape",
            json={"query": "Lead Site Reliability Engineer", "limit": 5},
        )
        assert res2.status_code == 200
        data2 = res2.json()
        assert data2["total_discovered"] == 1
        assert data2["total_added"] == 0
        assert data2["already_existing"] == 1


def test_api_google_jobs_scrape_multi_query(db_session):
    def mock_fetch(p, query=None, location="India", max_results=15, **kwargs):
        if "Principal" in (query or ""):
            return [
                {
                    "title": "Principal Architect",
                    "company": "Notion",
                    "location": "Hyderabad, India",
                    "salary_range": "₹60L - ₹90L",
                    "description": "Architect distributed document collaborative systems.",
                    "url": "https://jobs.lever.co/notion/principal-arch",
                    "source": "Google Jobs",
                    "source_type": "Google",
                }
            ]
        else:
            return [
                {
                    "title": "Senior Staff Engineer",
                    "company": "Stripe",
                    "location": "Bangalore, India",
                    "salary_range": "₹70L - ₹1Cr",
                    "description": "Payments settlement ledger engine.",
                    "url": "https://careers.stripe.com/jobs/staff-eng-ledger",
                    "source": "Google Jobs",
                    "source_type": "Google",
                }
            ]

    with patch("backend.main.fetch_google_jobs_playwright", side_effect=mock_fetch):
        res = client.post(
            "/api/jobs/google-jobs/scrape",
            json={
                "queries": [
                    "Product companies Principal Software Engineer jobs in India",
                    "Product companies Senior Staff Software Engineer jobs in India",
                ],
                "location": "India",
                "limit": 5,
                "use_browser": False,
            },
        )
        assert res.status_code == 200
        data = res.json()
        assert data["total_discovered"] == 2
        assert data["total_added"] == 2
        assert len(data["queries_scanned"]) == 2
        assert len(data["per_query_breakdown"]) == 2

        # Check OperationLog
        latest_op = (
            db_session.query(OperationLog)
            .filter(OperationLog.operation_type == "google_jobs_scrape")
            .order_by(OperationLog.created_at.desc())
            .first()
        )
        assert latest_op is not None
        assert latest_op.status == "Completed"
        details = json.loads(latest_op.details_json)
        assert len(details["queries_scanned"]) == 2
        assert len(details["per_query_breakdown"]) == 2


def test_normalize_google_query_dedupes_location():
    """Google alert/query names with duplicated location tokens are normalized."""
    from backend.main import normalize_google_query

    assert (
        normalize_google_query(
            "Product companies Senior Staff Software Engineer jobs in India India"
        )
        == "Product companies Senior Staff Software Engineer jobs in India"
    )
    assert normalize_google_query("Notion india jobs India") == "Notion india jobs"
    assert (
        normalize_google_query("Staff Software Engineer remote")
        == "Staff Software Engineer remote"
    )
    assert (
        normalize_google_query("  Senior   Data   Engineer  ") == "Senior Data Engineer"
    )
    assert normalize_google_query("") == ""


@patch("backend.main.fetch_google_jobs_playwright")
@patch("backend.main.get_db")
def test_google_jobs_task_runner_uses_playwright(mock_get_db, mock_fetch_pw, db_session):
    """The scheduled runner fetches via Playwright (the sole fetcher) with max_results."""
    from backend.main import _run_google_jobs_task_runner

    mock_get_db.return_value = iter([db_session])

    def fake_fetch(p, query=None, location=None, headless=True, max_results=15,
                   pacer=None, outcome=None, **kwargs):
        if outcome is not None:
            outcome["outcome"] = "fetched"
        return [
            {
                "company_name": "Acme",
                "title": "Backend Engineer",
                "url": "https://acme.example/jobs/1",
                "location": "Remote, India",
                "description": "We are hiring backend engineers.",
            }
        ]

    mock_fetch_pw.side_effect = fake_fetch

    result = _run_google_jobs_task_runner(
        queries=["Backend Engineer jobs"], location="India"
    )
    assert result["jobs_found"] == 1
    mock_fetch_pw.assert_called_once()
    _, kwargs = mock_fetch_pw.call_args
    assert "max_results" in kwargs


@patch("backend.main.fetch_google_jobs_playwright")
@patch("backend.main.get_db")
def test_google_jobs_runner_reports_partial_when_no_query_returns_postings(
    mock_get_db, mock_fetch_pw, db_session
):
    """A scan where every query came back empty is degraded, not a success (issue #75)."""
    from backend.database import OperationLog
    from backend.main import _run_google_jobs_task_runner

    mock_get_db.return_value = iter([db_session])
    mock_fetch_pw.return_value = []

    result = _run_google_jobs_task_runner(
        queries=["Backend Engineer jobs"], location="India"
    )

    assert result["jobs_found"] == 0
    assert result["status"] == "Partial"

    log = (
        db_session.query(OperationLog)
        .filter(OperationLog.operation_type == "google_jobs_background_scan")
        .order_by(OperationLog.id.desc())
        .first()
    )
    assert log is not None and log.status == "Partial"
    assert "Degraded" in log.summary
    # Per-query outcome is recorded for observability (issue #77).
    details = json.loads(log.details_json)
    assert details["per_query_outcomes"][0]["outcome"] == "zero_results"


@patch("backend.main.fetch_google_jobs_playwright")
@patch("backend.main.get_db")
def test_google_jobs_runner_reports_browser_busy(mock_get_db, mock_fetch_pw, db_session):
    """A BrowserProfileBusy fetch is reported as browser_busy, not silent success."""
    from backend.database import OperationLog
    from backend.main import _run_google_jobs_task_runner

    class BrowserProfileBusy(Exception):
        pass

    mock_get_db.return_value = iter([db_session])

    def raise_busy(*args, **kwargs):
        outcome = kwargs.get("outcome")
        if outcome is not None:
            outcome["outcome"] = "browser_busy"
        raise BrowserProfileBusy("profile locked")

    mock_fetch_pw.side_effect = raise_busy

    result = _run_google_jobs_task_runner(
        queries=["Backend Engineer jobs"], location="India"
    )
    assert result["jobs_found"] == 0

    log = (
        db_session.query(OperationLog)
        .filter(OperationLog.operation_type == "google_jobs_background_scan")
        .order_by(OperationLog.id.desc())
        .first()
    )
    details = json.loads(log.details_json)
    assert details["per_query_outcomes"][0]["outcome"] == "browser_busy"


# -------------------------------------------------------------
# Integration Tests: Gmail Alerts Sync Endpoint (Deprecated)
# -------------------------------------------------------------


def test_extract_company_and_role_from_email_header(monkeypatch):
    from backend.scraper import extract_company_and_role_from_email_header

    # Employer aliases are user-supplied (data/company_aliases.json), so inject them here
    # rather than depending on any hardcoded employer names in source.
    monkeypatch.setattr(
        "backend.scraper._company_aliases_cache",
        ["Microsoft", "Amazon", "Enterpert", "Google"],
    )

    res1 = extract_company_and_role_from_email_header(
        subject="Information needed for your interviews with Microsoft: Principal Software Engineer",
        snippet="Please provide your interview availability",
        sender="Varsha",
    )
    assert res1["company"] == "Microsoft"
    assert res1["role"] == "Principal Software Engineer"

    res2 = extract_company_and_role_from_email_header(
        subject="[Jobs/Alerts] Amazon Virtual Interview Confirmation - Software Development Engineer",
        snippet="Confirming your technical loop interview",
        sender="rs-rcindialoops",
    )
    assert res2["company"] == "Amazon"
    assert res2["role"] == "Software Development Engineer"

    res3 = extract_company_and_role_from_email_header(
        subject="Principal Engineer role at Enterpert | A Series A funded AI Startup",
        snippet="Hiring a Principal Engineer",
        sender="Adarsh",
    )
    assert res3["company"] == "Enterpert"
    assert res3["role"] == "Principal Engineer"
