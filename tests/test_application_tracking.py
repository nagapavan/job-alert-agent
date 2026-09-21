import json
import datetime
import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

from backend.main import app
from backend.config import API_KEY
from backend.database import get_db, Company, Job, Resume, ApplicationEvent, OperationLog
from backend.scraper import (
    check_greenhouse_application_status,
    probe_greenhouse_job_active,
    probe_job_url_active,
    probe_lever_job_active,
    probe_smartrecruiters_job_active,
)
from backend.playwright_app import (
    scan_and_sync_hirist_applications,
    sync_email_application_events
)

client = TestClient(app)

# -------------------------------------------------------------
# Unit Tests: Greenhouse Application Status & Active Probe
# -------------------------------------------------------------

@patch("backend.scraper.requests.get")
def test_check_greenhouse_application_status_active(mock_get):
    """Verifies that check_greenhouse_application_status parses candidate confirmation status pages."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = """
    <html>
      <head><title>Application Status | Datadog</title></head>
      <body>
        <div class="application-status">
          <h2>Application Status: In Review</h2>
          <div class="job-title">Staff Backend Engineer</div>
          <div class="company-name">Datadog</div>
          <span class="submitted-date">Submitted on September 12, 2026</span>
        </div>
      </body>
    </html>
    """
    mock_get.return_value = mock_resp

    res = check_greenhouse_application_status("https://boards.greenhouse.io/application_status?token=test_token_123")
    assert res["is_active"] is True
    assert "In Review" in res["status"]
    assert "Staff Backend Engineer" in res.get("job_title", "")
    assert "Datadog" in res.get("company", "")

@patch("backend.scraper.requests.get")
def test_check_greenhouse_application_status_closed(mock_get):
    """Verifies that check_greenhouse_application_status detects closed or rejected positions."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = """
    <html>
      <body>
        <div class="status-banner">
          <h1>This position is no longer accepting applications</h1>
          <p>Thank you for your interest in Airbnb.</p>
        </div>
      </body>
    </html>
    """
    mock_get.return_value = mock_resp

    res = check_greenhouse_application_status("https://boards.greenhouse.io/application_status?token=closed_token_456")
    assert res["is_active"] is False
    assert any(k in res["status"].lower() for k in ["closed", "no longer accepting", "inactive"])

@patch("backend.scraper.requests.get")
def test_probe_greenhouse_job_active_api_success(mock_get):
    """Verifies that probe_greenhouse_job_active confirms active requisition via Public Board API."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"id": 12345, "title": "Senior Infrastructure Engineer"}
    mock_get.return_value = mock_resp

    url = "https://boards.greenhouse.io/stripe/jobs/12345"
    assert probe_greenhouse_job_active(url) is True

@patch("backend.scraper.requests.get")
def test_probe_greenhouse_job_active_api_closed(mock_get):
    """Verifies that probe_greenhouse_job_active flags closed requisition when API returns 404."""
    mock_resp = MagicMock()
    mock_resp.status_code = 404
    mock_get.return_value = mock_resp

    url = "https://boards.greenhouse.io/stripe/jobs/99999"
    assert probe_greenhouse_job_active(url) is False

# -------------------------------------------------------------
# Unit Tests: Hirist Candidate Applications Scraping
# -------------------------------------------------------------

def test_scan_and_sync_hirist_applications_mocked_dom():
    """Verifies that scan_and_sync_hirist_applications extracts applications and normalizes recruiter pipeline statuses."""
    mock_p = MagicMock()
    mock_context = MagicMock()
    mock_page = MagicMock()
    mock_p.chromium.launch_persistent_context.return_value = mock_context
    mock_context.new_page.return_value = mock_page
    mock_context.pages = [mock_page]

    # Mock cards on page
    card1 = MagicMock()
    card1.inner_text.return_value = "Senior Backend Engineer\nSwiggy\nBengaluru\nInterview Scheduled\nApplied on 10 Sep 2026"
    title_el1 = MagicMock()
    title_el1.inner_text.return_value = "Senior Backend Engineer"
    title_el1.get_attribute.return_value = "https://www.hirist.tech/j/swiggy-senior-backend-123.html"
    comp_el1 = MagicMock()
    comp_el1.inner_text.return_value = "Swiggy"
    status_el1 = MagicMock()
    status_el1.inner_text.return_value = "Interview Scheduled"
    loc_el1 = MagicMock()
    loc_el1.inner_text.return_value = "Bengaluru"
    card1.query_selector.side_effect = lambda sel: {
        "a[href*='/j/'], a.job-title, h2 a, a": title_el1,
        ".company-name, .employer-name, .comp-name, div[class*='company'], span[class*='company']": comp_el1,
        ".status-badge, .status-text, [class*='status'], [class*='badge'], span[class*='pipeline']": status_el1,
        ".location, .job-location, span[class*='location']": loc_el1
    }.get(sel, None)

    card2 = MagicMock()
    card2.inner_text.return_value = "Lead Platform Engineer\nRazorpay\nBengaluru\nNot Shortlisted\nApplied on 05 Sep 2026"
    title_el2 = MagicMock()
    title_el2.inner_text.return_value = "Lead Platform Engineer"
    title_el2.get_attribute.return_value = "https://www.hirist.tech/j/razorpay-lead-platform-456.html"
    comp_el2 = MagicMock()
    comp_el2.inner_text.return_value = "Razorpay"
    status_el2 = MagicMock()
    status_el2.inner_text.return_value = "Not Shortlisted"
    loc_el2 = MagicMock()
    loc_el2.inner_text.return_value = "Bengaluru"
    card2.query_selector.side_effect = lambda sel: {
        "a[href*='/j/'], a.job-title, h2 a, a": title_el2,
        ".company-name, .employer-name, .comp-name, div[class*='company'], span[class*='company']": comp_el2,
        ".status-badge, .status-text, [class*='status'], [class*='badge'], span[class*='pipeline']": status_el2,
        ".location, .job-location, span[class*='location']": loc_el2
    }.get(sel, None)

    mock_page.query_selector_all.return_value = [card1, card2]
    mock_page.query_selector.return_value = None  # No Next button to break pagination

    results = scan_and_sync_hirist_applications(mock_p, headless=True, max_pages=1, page=mock_page)
    assert len(results) == 2
    
    assert results[0]["title"] == "Senior Backend Engineer"
    assert results[0]["company"] == "Swiggy"
    assert results[0]["status"] == "Interview"
    assert "swiggy" in results[0]["url"]

    assert results[1]["title"] == "Lead Platform Engineer"
    assert results[1]["company"] == "Razorpay"
    assert results[1]["status"] == "Rejected"

# -------------------------------------------------------------
# Unit Tests: Email Status Events Sync
# -------------------------------------------------------------

@patch("backend.playwright_app.scan_gmail_for_job_alerts")
def test_sync_email_application_events(mock_scan_gmail):
    """Verifies that sync_email_application_events extracts recruiter decision events from parsed emails."""
    mock_scan_gmail.return_value = [
        {
            "sender": "no-reply@greenhouse.io",
            "subject": "Interview with Stripe for Senior Software Engineer",
            "snippet": "We would like to invite you to schedule your technical phone screen.",
            "classification": "interview",
            "date": "2026-09-18"
        },
        {
            "sender": "jobs@datadoghq.com",
            "subject": "Update regarding your application at Datadog",
            "snippet": "Unfortunately, we have decided to pursue other candidates at this time.",
            "classification": "rejection",
            "date": "2026-09-17"
        },
        {
            "sender": "alerts@google.com",
            "subject": "3 new jobs for Software Engineer",
            "snippet": "Daily digest of open jobs",
            "classification": "job_alert",
            "date": "2026-09-19"
        }
    ]

    mock_p = MagicMock()
    events = sync_email_application_events(mock_p, headless=True)
    assert len(events) == 2
    
    stripe_event = next(e for e in events if "Stripe" in e["company"])
    assert stripe_event["event_type"] == "interview"
    assert "Senior Software Engineer" in stripe_event["role"]

    datadog_event = next(e for e in events if "Datadog" in e["company"])
    assert datadog_event["event_type"] == "rejection"

# -------------------------------------------------------------
# Integration Tests: External Applications Sync API Endpoint
# -------------------------------------------------------------

from conftest import test_client as client, TestingSessionLocal

@patch("backend.main.sync_playwright")
@patch("backend.main.sync_email_application_events")
@patch("backend.main.scan_and_sync_hirist_applications")
@patch("backend.main.probe_greenhouse_job_active")
def test_api_sync_external_applications_lifecycle(mock_probe_gh, mock_scan_hirist, mock_sync_email, mock_sync_pw):
    """Verifies end-to-end POST /api/applications/sync-external endpoint lifecycle and DB updates."""
    mock_p_ctx = MagicMock()
    mock_sync_pw.return_value.__enter__.return_value = mock_p_ctx
    # Setup test DB entities
    db = TestingSessionLocal()
    comp = None
    job = None
    razorpay_job = None
    try:
        # Create test company and applied job
        comp = Company(name="Swiggy", domain="swiggy.com", careers_url="https://careers.swiggy.com")
        db.add(comp)
        db.flush()

        job = Job(
            company_id=comp.id,
            title="Senior Backend Engineer",
            description="Swiggy backend opening",
            url="https://www.hirist.tech/j/swiggy-senior-backend-123.html",
            location="Bengaluru",
            source="Direct",
            source_type="Hirist",
            status="Applied",
            match_score=85.0
        )
        db.add(job)
        db.commit()

        # Mock Hirist returning updated status for Swiggy, plus a new job for Razorpay
        mock_scan_hirist.return_value = [
            {
                "title": "Senior Backend Engineer",
                "company": "Swiggy",
                "status": "Interview",
                "raw_status": "Interview Scheduled",
                "applied_date": "2026-09-10",
                "url": "https://www.hirist.tech/j/swiggy-senior-backend-123.html",
                "location": "Bengaluru"
            },
            {
                "title": "Staff Platform Engineer",
                "company": "Razorpay",
                "status": "Applied",
                "raw_status": "Applied",
                "applied_date": "2026-09-14",
                "url": "https://www.hirist.tech/j/razorpay-staff-platform-999.html",
                "location": "Bengaluru"
            }
        ]

        # Mock email events
        mock_sync_email.return_value = []
        mock_probe_gh.return_value = True

        headers = {
            "X-API-Key": API_KEY,
            "Origin": "chrome-extension://abcdefghijklmnopqrstuvwxyz"
        }
        payload = {
            "sources": ["hirist", "greenhouse", "gmail"],
            "max_pages": 5,
            "headless": True
        }

        res = client.post("/api/applications/sync-external", json=payload, headers=headers)
        assert res.status_code == 200
        data = res.json()
        assert data["hirist_checked"] == 2
        assert data["hirist_updated"] >= 1
        assert data["hirist_ingested"] >= 1

        # Verify DB status update on existing Swiggy job
        db.refresh(job)
        assert job.status == "Interview"
        
        # Verify ApplicationEvent log exists
        events = db.query(ApplicationEvent).filter(ApplicationEvent.job_id == job.id).all()
        assert any("Hirist" in e.description for e in events)

        # Verify Razorpay job was newly ingested
        razorpay_job = db.query(Job).filter(Job.title.ilike("%Staff Platform Engineer%")).first()
        assert razorpay_job is not None
        assert razorpay_job.status == "Applied"
        assert razorpay_job.source_type == "Hirist"

    finally:
        # Cleanup
        job_ids = []
        if job and job.id:
            job_ids.append(job.id)
        if razorpay_job and razorpay_job.id:
            job_ids.append(razorpay_job.id)

        if job_ids:
            db.query(ApplicationEvent).filter(ApplicationEvent.job_id.in_(job_ids)).delete(synchronize_session=False)
            db.query(Job).filter(Job.id.in_(job_ids)).delete(synchronize_session=False)

        db.query(Company).filter(Company.name.in_(["Swiggy", "Razorpay"])).delete(synchronize_session=False)
        db.commit()

@patch("backend.main.sync_playwright")
@patch("backend.main.sync_email_application_events")
def test_gmail_sync_is_opt_in(mock_sync_email, mock_sync_pw):
    """Gmail inbox scanning only runs when explicitly requested AND the opt-in flag is enabled."""
    mock_sync_pw.return_value.__enter__.return_value = MagicMock()
    mock_sync_email.return_value = []

    # Default: requested but flag disabled -> never scans Gmail
    res = client.post("/api/applications/sync-external", json={"sources": ["gmail"]})
    assert res.status_code == 200
    mock_sync_email.assert_not_called()

    # Enable the opt-in feature -> Gmail scan runs
    client.put("/api/preferences", json={"features": {"gmail_sync": True}})
    res = client.post("/api/applications/sync-external", json={"sources": ["gmail"]})
    assert res.status_code == 200
    mock_sync_email.assert_called_once()


def test_gmail_query_is_narrow():
    from backend.gmail_client import GMAIL_QUERY
    assert "newer_than:" in GMAIL_QUERY
    assert "from:greenhouse.io" in GMAIL_QUERY
    assert "subject:interview" in GMAIL_QUERY
    # Opportunity signals (LinkedIn / job alerts) are included for opportunity tracking.
    assert "from:linkedin.com" in GMAIL_QUERY
    assert "job alert" in GMAIL_QUERY


def test_gmail_list_message_ids_uses_narrow_query_and_caps_pages():
    from backend.gmail_client import list_message_ids, GMAIL_QUERY
    svc = MagicMock()
    svc.users().messages().list.return_value.execute.return_value = {"messages": [{"id": "m1"}, {"id": "m2"}]}
    ids = list_message_ids(svc, max_pages=2)
    assert ids == ["m1", "m2"]
    kwargs = svc.users().messages().list.call_args.kwargs
    assert kwargs["q"] == GMAIL_QUERY
    assert kwargs["maxResults"] > 0


def test_gmail_fetch_metadata_is_metadata_format():
    from backend.gmail_client import fetch_message_metadata
    svc = MagicMock()
    svc.users().messages().get.return_value.execute.return_value = {
        "id": "m1", "snippet": "Unfortunately, we won't be moving forward",
        "payload": {"headers": [
            {"name": "From", "value": "Stripe <jobs@stripe.com>"},
            {"name": "Subject", "value": "Your application"},
            {"name": "Date", "value": "Mon, 1 Sep"},
        ]},
    }
    meta = fetch_message_metadata(svc, "m1")
    assert meta["subject"] == "Your application"
    assert "stripe.com" in meta["sender"]
    assert svc.users().messages().get.call_args.kwargs["format"] == "metadata"


@patch("backend.scraper.extract_company_and_role_from_email_header")
@patch("backend.gmail_client.build_service")
@patch("backend.config.get_gmail_settings")
def test_run_gmail_sync_classifies_and_dedups(mock_settings, mock_build_service, mock_extract, db_session):
    from backend.gmail_client import run_gmail_sync
    from backend.database import ProcessedEmail

    mock_settings.return_value = {
        "client_id": "c", "client_secret": "s", "refresh_token": "r",
        "token_email": "me@example.com", "history_id": "", "connected_at": "",
    }
    mock_extract.return_value = {"company": "Stripe", "role": "Staff Engineer"}

    svc = MagicMock()
    svc.users().messages().list.return_value.execute.return_value = {"messages": [{"id": "m1"}]}
    svc.users().messages().get.return_value.execute.return_value = {
        "id": "m1", "snippet": "Unfortunately, we won't be moving forward",
        "payload": {"headers": [
            {"name": "From", "value": "Stripe <jobs@stripe.com>"},
            {"name": "Subject", "value": "Your application"},
            {"name": "Date", "value": "today"},
        ]},
    }
    mock_build_service.return_value = svc

    events = run_gmail_sync(db_session)
    db_session.commit()
    assert len(events) == 1
    assert events[0]["event_type"] == "rejection"
    assert events[0]["company"] == "Stripe"
    assert db_session.query(ProcessedEmail).count() == 1

    # Second run must dedup (message already processed)
    assert run_gmail_sync(db_session) == []


def test_api_sync_external_unauthorized():
    """Verifies that unauthorized requests without an API key are rejected with 401."""
    headers = {
        "Origin": "https://unauthorized-domain.com",
        "Sec-Fetch-Site": "cross-site"
    }
    payload = {"sources": ["hirist"]}
    res = client.post("/api/applications/sync-external", json=payload, headers=headers)
    assert res.status_code == 401


# -------------------------------------------------------------
# Unit Tests: Gmail opportunities + full application status mapping
# -------------------------------------------------------------

@patch("backend.scraper.parse_google_alerts_digest", return_value=[])
@patch("backend.scraper.extract_company_and_role_from_email_header")
@patch("backend.gmail_client.build_service")
@patch("backend.config.get_gmail_settings")
def test_run_gmail_sync_returns_opportunities(mock_settings, mock_build_service, mock_extract, mock_digest, db_session):
    """Job-alert emails without an application must surface as 'opportunity' events."""
    from backend.gmail_client import run_gmail_sync

    mock_settings.return_value = {
        "client_id": "c", "client_secret": "s", "refresh_token": "r",
        "token_email": "me@example.com", "history_id": "", "connected_at": "",
    }
    mock_extract.return_value = {"company": "Google", "role": "Staff Software Engineer"}

    svc = MagicMock()
    svc.users().messages().list.return_value.execute.return_value = {"messages": [{"id": "m1"}]}
    svc.users().messages().get.return_value.execute.return_value = {
        "id": "m1", "snippet": "3 new jobs matching your profile at Google.",
        "payload": {"headers": [
            {"name": "From", "value": "Google Alerts <googlealerts-noreply@google.com>"},
            {"name": "Subject", "value": "Daily Job Alert"},
            {"name": "Date", "value": "today"},
        ]},
    }
    mock_build_service.return_value = svc

    events = run_gmail_sync(db_session)
    assert len(events) == 1
    assert events[0]["event_type"] == "opportunity"
    assert events[0]["company"] == "Google"
    assert events[0]["role"] == "Staff Software Engineer"


def test_apply_email_events_updates_all_application_statuses(db_session):
    """application_received/screening/offer/rejection must map to the pipeline, role-aware."""
    from backend.main import _apply_email_events_to_db

    comp = Company(name="AcmeStatusTest", domain="acmestatustest.com")
    db_session.add(comp)
    db_session.flush()
    backend_job = Job(company_id=comp.id, title="Senior Backend Engineer", url="https://x/se", status="Shortlisted")
    other_job = Job(company_id=comp.id, title="Product Designer", url="https://x/pd", status="Shortlisted")
    db_session.add_all([backend_job, other_job])
    db_session.commit()

    _apply_email_events_to_db(db_session, [
        {"company": "AcmeStatusTest", "role": "Senior Backend Engineer",
         "event_type": "application_received", "subject": "Thanks for applying"}
    ])
    db_session.refresh(backend_job)
    db_session.refresh(other_job)
    assert backend_job.status == "Applied"
    assert other_job.status == "Shortlisted"  # role-aware: the other role is untouched

    _apply_email_events_to_db(db_session, [
        {"company": "AcmeStatusTest", "role": "Senior Backend Engineer",
         "event_type": "screening", "subject": "Initial screening"}
    ])
    db_session.refresh(backend_job)
    assert backend_job.status == "Screening"

    _apply_email_events_to_db(db_session, [
        {"company": "AcmeStatusTest", "role": "Senior Backend Engineer",
         "event_type": "offer", "subject": "Your offer"}
    ])
    db_session.refresh(backend_job)
    assert backend_job.status == "Offered"

    _apply_email_events_to_db(db_session, [
        {"company": "AcmeStatusTest", "role": "Senior Backend Engineer",
         "event_type": "rejection", "subject": "Update"}
    ])
    db_session.refresh(backend_job)
    assert backend_job.status == "Rejected"


def test_apply_email_events_creates_untracked_application(db_session):
    """An application email for a role we never discovered still creates a tracked job."""
    from backend.main import _apply_email_events_to_db

    n = _apply_email_events_to_db(db_session, [{
        "company": "UntrackedCoTest", "role": "Principal Engineer",
        "event_type": "application_received", "subject": "Thanks for applying",
    }])
    assert n == 1
    job = db_session.query(Job).filter(
        Job.company.has(Company.name.ilike("UntrackedCoTest")),
        Job.title == "Principal Engineer",
    ).first()
    assert job is not None
    assert job.status == "Applied"
    assert job.source_type == "Email"


def test_low_confidence_email_events_are_guarded(db_session):
    """A weak keyword signal must not create a phantom job or mutate state."""
    from backend.main import _apply_email_events_to_db, _ingest_email_opportunities_to_db

    # Low-confidence rejection for an untracked role -> no job created
    n = _apply_email_events_to_db(db_session, [{
        "company": "LowConfCoTest", "role": "Data Scientist",
        "event_type": "rejection", "confidence": 0.3, "subject": "Weekly newsletter",
    }])
    assert n == 0
    assert db_session.query(Job).filter(Job.company.has(Company.name.ilike("LowConfCoTest"))).count() == 0

    # Low-confidence opportunity -> not ingested
    assert _ingest_email_opportunities_to_db(db_session, [{
        "company": "WeakOppCoTest", "role": "ML Engineer",
        "event_type": "opportunity", "confidence": 0.2,
    }]) == 0


def test_confidence_gate_blocks_weak_update_but_allows_strong(db_session):
    """Existing jobs are only mutated by sufficiently confident email events."""
    from backend.main import _apply_email_events_to_db

    comp = Company(name="GateCoTest", domain="gateco.com")
    db_session.add(comp)
    db_session.flush()
    job = Job(company_id=comp.id, title="Backend Engineer", url="https://x/gate", status="Shortlisted")
    db_session.add(job)
    db_session.commit()

    _apply_email_events_to_db(db_session, [{
        "company": "GateCoTest", "role": "Backend Engineer",
        "event_type": "rejection", "confidence": 0.2, "subject": "weak",
    }])
    db_session.refresh(job)
    assert job.status == "Shortlisted"

    _apply_email_events_to_db(db_session, [{
        "company": "GateCoTest", "role": "Backend Engineer",
        "event_type": "rejection", "confidence": 0.95, "subject": "strong",
    }])
    db_session.refresh(job)
    assert job.status == "Rejected"


def test_ingest_email_opportunities_creates_and_dedups(db_session):
    """Opportunities from Gmail are tracked as 'To Apply' jobs and deduplicated."""
    from backend.main import _ingest_email_opportunities_to_db

    events = [{
        "company": "OpportunityCoTest", "role": "Staff Platform Engineer",
        "event_type": "opportunity", "subject": "Job alert",
        "snippet": "A great role", "url": "https://example.com/job/1",
    }]
    assert _ingest_email_opportunities_to_db(db_session, events) == 1
    job = db_session.query(Job).filter(
        Job.company.has(Company.name.ilike("OpportunityCoTest")),
        Job.title == "Staff Platform Engineer",
    ).first()
    assert job is not None
    assert job.status == "To Apply"
    assert job.source_type == "Email"
    assert job.source == "Gmail Opportunity"

    # Same opportunity again -> deduped
    assert _ingest_email_opportunities_to_db(db_session, events) == 0


class TestOpportunityIdentityParsing:
    """Aggregator alert snippets must resolve to the real employer + specific role."""

    def test_extract_parses_linkedin_alert_header(self):
        from backend.scraper import extract_opportunity_company_and_title

        cases = [
            (
                "Avalara APAC Principal Software Engineer/ Architect- AI: What You'll DoAvalara...",
                ("Avalara APAC", "Principal Software Engineer/ Architect- AI"),
            ),
            (
                "Columbia Sportswear Company Principal Software Engineer (Integrations): About...",
                ("Columbia Sportswear Company", "Principal Software Engineer (Integrations)"),
            ),
            (
                "Marks and Spencer Principal Software Engineer - International: All the details...",
                ("Marks and Spencer", "Principal Software Engineer - International"),
            ),
            (
                "GP Principal Software Engineer - AI Platform: About UsOur leading SaaS...",
                ("GP", "Principal Software Engineer - AI Platform"),
            ),
            (
                "BJAK Principal Software Engineer: The RoleBJAK builds technology products...",
                ("BJAK", "Principal Software Engineer"),
            ),
            (
                "ActAI Principal Software Engineer, AI Email App: About the RoleThere are...",
                ("ActAI", "Principal Software Engineer, AI Email App"),
            ),
        ]
        for snippet, expected in cases:
            assert extract_opportunity_company_and_title(snippet) == expected

    def test_extract_falls_back_when_header_is_untrustworthy(self):
        from backend.scraper import extract_opportunity_company_and_title

        # A blurb-like prefix must not be mistaken for an employer.
        assert extract_opportunity_company_and_title(
            "We are hiring for a Software Engineer role. Join our team!",
            fallback_company="LinkedIn",
            fallback_role="Software Engineer",
        ) == ("LinkedIn", "Software Engineer")
        # No role phrase at all -> untouched fallback.
        assert extract_opportunity_company_and_title(
            "3 new jobs matching your profile.",
            fallback_company="Google",
            fallback_role="Staff Software Engineer",
        ) == ("Google", "Staff Software Engineer")


def test_ingest_opportunities_splits_distinct_roles_and_dedups_batch(db_session):
    """
    Regression for the 'six identical Principal Software Engineer cards' bug:
    aggregator alert digests must resolve to their real employers, and same-batch
    duplicate roles must be collapsed (autoflush=False previously let them slip through).
    """
    from backend.main import _ingest_email_opportunities_to_db

    events = [
        {"company": "LinkedIn", "role": "Principal Software Engineer", "event_type": "opportunity",
         "subject": "Job alert", "snippet": "Avalara APAC Principal Software Engineer: What You'll Do", "url": ""},
        {"company": "LinkedIn", "role": "Principal Software Engineer", "event_type": "opportunity",
         "subject": "Job alert", "snippet": "BJAK Principal Software Engineer: The RoleBJAK builds", "url": ""},
        # Same underlying BJAK role again in the SAME batch -> must collapse.
        {"company": "LinkedIn", "role": "Principal Software Engineer", "event_type": "opportunity",
         "subject": "Job alert", "snippet": "BJAK Principal Software Engineer: The RoleBJAK builds", "url": ""},
    ]
    assert _ingest_email_opportunities_to_db(db_session, events) == 2

    companies = {
        j.company.name for j in db_session.query(Job).filter(Job.source == "Gmail Opportunity").all()
    }
    assert companies == {"Avalara APAC", "BJAK"}
    assert db_session.query(Job).filter(
        Job.source == "Gmail Opportunity", Job.company.has(Company.name == "BJAK")
    ).count() == 1


def test_dedupe_gmail_opportunity_jobs_merges_and_preserves_status(db_session):
    """Legacy duplicate rows are merged, keeping the most advanced row + its events."""
    from backend.main import dedupe_gmail_opportunity_jobs

    comp = Company(name="LinkedIn", domain="linkedin.com")
    db_session.add(comp)
    db_session.flush()

    def add_legacy(status, desc):
        j = Job(company_id=comp.id, title="Principal Software Engineer", description=desc,
                location="Remote / Various", source="Gmail Opportunity", source_type="Email",
                status=status)
        db_session.add(j)
        db_session.flush()
        return j

    gp_a = add_legacy("To Apply", "GP Principal Software Engineer - AI Platform: About Us")
    gp_b = add_legacy("To Apply", "GP Principal Software Engineer - AI Platform: About Us")
    bjak_apply = add_legacy("To Apply", "BJAK Principal Software Engineer: The RoleBJAK builds")
    bjak_short = add_legacy("Shortlisted", "BJAK Principal Software Engineer: The RoleBJAK builds")
    distinct = add_legacy("To Apply", "ActAI Principal Software Engineer, AI Email App: About")
    db_session.add(ApplicationEvent(job_id=bjak_short.id, event_type="status_change",
                                    description="Shortlisted"))
    db_session.commit()

    removed = dedupe_gmail_opportunity_jobs(db_session)
    db_session.commit()

    assert removed == 2  # gp_b and one BJAK row

    remaining_ids = {j.id for j in db_session.query(Job).filter(
        Job.source == "Gmail Opportunity"
    ).all()}
    assert gp_b.id not in remaining_ids
    assert distinct.id in remaining_ids

    kept_bjak = db_session.query(Job).filter(Job.id == bjak_short.id).first()
    assert kept_bjak is not None and kept_bjak.status == "Shortlisted"
    assert db_session.query(ApplicationEvent).filter(
        ApplicationEvent.job_id == bjak_short.id
    ).count() == 1

    # Idempotent: a second pass removes nothing.
    assert dedupe_gmail_opportunity_jobs(db_session) == 0


def test_task_events_sse_asyncio_is_available():
    """Regression: main.py must import asyncio (the SSE endpoint uses it)."""
    import backend.main as main
    assert hasattr(main, "asyncio")


# -------------------------------------------------------------
# Unit Tests: Shared & Lever Requisition-Active Probes
# -------------------------------------------------------------

@patch("backend.scraper.requests.get")
def test_probe_job_url_active_closed_marker(mock_get):
    """A 200 page carrying a known closing phrase must be flagged inactive."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = "<html><body>This position has been closed.</body></html>"
    mock_get.return_value = mock_resp
    assert probe_job_url_active("https://example.com/jobs/1") is False


@patch("backend.scraper.requests.get")
def test_probe_job_url_active_missing_returns_false(mock_get):
    mock_resp = MagicMock()
    mock_resp.status_code = 404
    mock_get.return_value = mock_resp
    assert probe_job_url_active("https://example.com/jobs/1") is False


@patch("backend.scraper.requests.get")
def test_probe_job_url_active_error_assumes_active(mock_get):
    """Transient network errors must never auto-reject a real application."""
    mock_get.side_effect = RuntimeError("network down")
    assert probe_job_url_active("https://example.com/jobs/1") is True


@patch("backend.scraper.requests.get")
def test_probe_lever_job_active_uses_public_posting_api(mock_get):
    """Lever probe must hit the single-posting endpoint with the extracted slug + id."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_get.return_value = mock_resp

    assert probe_lever_job_active("https://jobs.lever.co/stripe/abc-123-def/apply") is True
    called_url = mock_get.call_args[0][0]
    assert called_url == "https://api.lever.co/v0/postings/stripe/abc-123-def"


@patch("backend.scraper.requests.get")
def test_probe_lever_job_active_closed_returns_false(mock_get):
    """A 404 from the public Postings API means the requisition is closed/unlisted."""
    mock_resp = MagicMock()
    mock_resp.status_code = 404
    mock_get.return_value = mock_resp
    assert probe_lever_job_active("https://jobs.lever.co/stripe/abc-123-def") is False


@patch("backend.scraper.requests.get")
def test_probe_lever_job_active_falls_back_when_url_unparseable(mock_get):
    """Without a posting id the probe falls back to a generic HTTP check on the raw URL."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = "Open role"
    mock_get.return_value = mock_resp
    assert probe_lever_job_active("https://jobs.lever.co/stripe") is True
    assert mock_get.call_args[0][0] == "https://jobs.lever.co/stripe"


# -------------------------------------------------------------
# Integration: Lever requisition probing in the external sync endpoint
# -------------------------------------------------------------

@patch("backend.scraper.requests.get")
def test_probe_smartrecruiters_job_active_flag(mock_get):
    """The SmartRecruiters detail endpoint exposes a direct `active` boolean."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"active": True}
    mock_get.return_value = mock_resp

    assert probe_smartrecruiters_job_active(
        "https://jobs.smartrecruiters.com/Acme/744000000000001-staff-engineer"
    ) is True
    assert mock_get.call_args[0][0] == (
        "https://api.smartrecruiters.com/v1/companies/acme/postings/744000000000001"
    )


@patch("backend.scraper.requests.get")
def test_probe_smartrecruiters_job_closed_flag(mock_get):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"active": False}
    mock_get.return_value = mock_resp
    assert probe_smartrecruiters_job_active(
        "https://jobs.smartrecruiters.com/Acme/744000000000001-staff-engineer"
    ) is False


@patch("backend.scraper.requests.get")
def test_probe_smartrecruiters_job_missing_returns_false(mock_get):
    mock_resp = MagicMock()
    mock_resp.status_code = 404
    mock_get.return_value = mock_resp
    assert probe_smartrecruiters_job_active(
        "https://jobs.smartrecruiters.com/Acme/744000000000001-staff-engineer"
    ) is False


@patch("backend.main.probe_smartrecruiters_job_active")
def test_api_sync_external_smartrecruiters_probe_closes_closed_posting(mock_probe_sr):
    """Applied/Shortlisted SmartRecruiters jobs are auto-rejected when `active` is false."""
    mock_probe_sr.return_value = False
    db = TestingSessionLocal()
    comp = None
    job = None
    try:
        comp = Company(name="SRCoTest", domain="srcotest.com")
        db.add(comp)
        db.flush()
        job = Job(
            company_id=comp.id,
            title="Staff Engineer",
            description="SmartRecruiters test posting",
            url="https://jobs.smartrecruiters.com/SRCoTest/744000000000001-staff-engineer",
            location="Bengaluru",
            source="SmartRecruiters Portal",
            source_type="Direct",
            status="Applied",
        )
        db.add(job)
        db.commit()

        # SmartRecruiters probing is opt-in with consent; enable + acknowledge it before syncing.
        client.put(
            "/api/preferences",
            json={
                "features": {"ats_smartrecruiters": True},
                "consents": {"ats_smartrecruiters": {"ack": True}},
            },
        )
        res = client.post("/api/applications/sync-external", json={"sources": ["smartrecruiters"]})
        assert res.status_code == 200
        data = res.json()
        assert data["status_updates"] >= 1
        assert any("SmartRecruiters" in d for d in data["details"])

        db.refresh(job)
        assert job.status == "Rejected"
        assert job.rejected_at is not None
        events = db.query(ApplicationEvent).filter(ApplicationEvent.job_id == job.id).all()
        assert any(e.event_type == "requisition_closed" for e in events)
    finally:
        if job and job.id:
            db.query(ApplicationEvent).filter(ApplicationEvent.job_id == job.id).delete(synchronize_session=False)
            db.query(Job).filter(Job.id == job.id).delete(synchronize_session=False)
        if comp and comp.id:
            db.query(Company).filter(Company.id == comp.id).delete(synchronize_session=False)
        db.commit()


@patch("backend.main.probe_lever_job_active")
def test_api_sync_external_lever_probe_closes_closed_requisition(mock_probe_lever):
    """Applied/Shortlisted Lever jobs are auto-rejected when the public API reports closed."""
    mock_probe_lever.return_value = False
    db = TestingSessionLocal()
    comp = None
    job = None
    try:
        comp = Company(name="LeverCoTest", domain="levercotest.com")
        db.add(comp)
        db.flush()

        job = Job(
            company_id=comp.id,
            title="Staff Backend Engineer",
            description="Lever test posting",
            url="https://jobs.lever.co/levercotest/abc-123-def",
            location="Bengaluru",
            source="Lever Portal",
            source_type="Direct",
            status="Shortlisted",
        )
        db.add(job)
        db.commit()

        res = client.post("/api/applications/sync-external", json={"sources": ["lever"]})
        assert res.status_code == 200
        data = res.json()
        assert data["status_updates"] >= 1
        assert any("Lever" in d for d in data["details"])

        db.refresh(job)
        assert job.status == "Rejected"
        assert job.rejected_at is not None

        events = db.query(ApplicationEvent).filter(ApplicationEvent.job_id == job.id).all()
        assert any(e.event_type == "requisition_closed" for e in events)
    finally:
        if job and job.id:
            db.query(ApplicationEvent).filter(ApplicationEvent.job_id == job.id).delete(synchronize_session=False)
            db.query(Job).filter(Job.id == job.id).delete(synchronize_session=False)
        if comp and comp.id:
            db.query(Company).filter(Company.id == comp.id).delete(synchronize_session=False)
        db.commit()
