"""One failing portal must not abort the scan, and a pacer refusal is not "0 postings".

Findings 46 and 47.
"""

from unittest.mock import patch

from backend.ats.engine import active_pacer
from backend.ats.pacing import HostPacer
from backend.database import Company, Job
from backend.routers.discovery import ScrapeRequest, _execute_ats_scrape


def _company(db, name, careers_url):
    comp = Company(name=name, careers_url=careers_url)
    db.add(comp)
    db.commit()
    return comp


def test_a_raising_portal_is_recorded_and_the_scan_continues(db_session):
    broken = _company(db_session, "Acme", "https://boards.greenhouse.io/acme")
    healthy = _company(db_session, "Globex", "https://jobs.lever.co/globex")

    job = {
        "title": "Backend Engineer",
        "url": "https://jobs.lever.co/globex/1",
        "location": "Remote",
        "description": "Build APIs.",
        "source_type": "Direct",
    }

    with patch(
        "backend.scraper.scrape_greenhouse_jobs",
        side_effect=RuntimeError("board exploded"),
    ), patch("backend.scraper.scrape_lever_jobs", return_value=[job]) as lever:
        response = _execute_ats_scrape(ScrapeRequest(company_id=None), db_session)

    # The scan completed, the healthy company was still scraped, and the pacer was dropped.
    lever.assert_called()
    assert response.jobs_found >= 1
    assert active_pacer() is None

    from backend.routers.discovery import LATEST_SCRAPE_AUDIT

    by_company = {row["company"]: row for row in LATEST_SCRAPE_AUDIT}
    assert by_company["Acme"]["status"] == "Failed"
    assert "board exploded" in by_company["Acme"]["diagnostics"]
    assert by_company["Globex"]["status"] in ("Success", "Filtered", "0 Postings Found")


def test_a_pacer_refusal_is_reported_as_rate_limited(db_session):
    _company(db_session, "Acme", "https://boards.greenhouse.io/acme")
    pacer = HostPacer(0.0, max_requests_per_run=1, _sleep=lambda s: None)

    def refusing_portal(*_args, **_kwargs):
        # Consume the single request of budget, then be refused by the ceiling.
        pacer.allow("https://boards-api.greenhouse.io/v1/boards/acme/jobs")
        pacer.wait("https://boards-api.greenhouse.io/v1/boards/acme/jobs")
        pacer.allow("https://boards-api.greenhouse.io/v1/boards/acme/jobs")
        return []

    with patch("backend.ats.pacing.build_pacer", return_value=pacer), patch(
        "backend.scraper.scrape_greenhouse_jobs", side_effect=refusing_portal
    ):
        _execute_ats_scrape(ScrapeRequest(company_id=None), db_session)

    from backend.routers.discovery import LATEST_SCRAPE_AUDIT

    row = next(r for r in LATEST_SCRAPE_AUDIT if r["company"] == "Acme")
    assert row["status"] == "Rate limited"
    assert "ceiling" in row["diagnostics"]


def _greenhouse_jobs(*urls):
    return [
        {
            "title": f"Backend Engineer {i}",
            "url": url,
            "location": "Remote",
            "description": "Build APIs.",
            "source_type": "Direct",
        }
        for i, url in enumerate(urls)
    ]


def test_duplicate_urls_differing_only_by_tracking_count_once(db_session):
    _company(db_session, "Acme", "https://boards.greenhouse.io/acme")

    with patch(
        "backend.scraper.scrape_greenhouse_jobs",
        return_value=_greenhouse_jobs(
            "https://boards.greenhouse.io/acme/jobs/1",
            "https://www.boards.greenhouse.io/acme/jobs/1/?utm_source=alert",
        ),
    ):
        _execute_ats_scrape(ScrapeRequest(company_id=None), db_session)

    from backend.routers.discovery import LATEST_SCRAPE_AUDIT

    row = next(r for r in LATEST_SCRAPE_AUDIT if r["company"] == "Acme")
    # raw_found is the post-dedupe count, so the audit no longer inflates it.
    assert row["raw_found"] == 1


def test_a_retitled_posting_is_deduped_by_requisition_id(db_session):
    comp = _company(db_session, "Acme", "https://boards.greenhouse.io/acme")
    db_session.add(
        Job(
            company_id=comp.id,
            title="Backend Engineer (old title)",
            url="https://boards.greenhouse.io/acme/jobs/123456",
            requisition_id="123456",
            source_type="Direct",
        )
    )
    db_session.commit()

    with patch(
        "backend.scraper.scrape_greenhouse_jobs",
        return_value=[
            {
                "title": "Senior Backend Engineer (retitled)",
                "url": "https://boards.greenhouse.io/acme/jobs/123456?gh_jid=123456",
                "location": "Remote",
                "description": "Build APIs.",
                "source_type": "Direct",
            }
        ],
    ):
        response = _execute_ats_scrape(ScrapeRequest(company_id=None), db_session)

    assert response.jobs_added == 0
    assert (
        db_session.query(Job)
        .filter(Job.company_id == comp.id)
        .count()
        == 1
    )
