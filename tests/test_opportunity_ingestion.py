"""Tests for issue #82: recruiter/JD opportunity ingestion.

Covers: outreach classification, employer/agency/location extraction, full-body fetch,
agency+employer dedup identity, expanded rejection patterns, and the no-fabrication rule.
"""
import base64
from unittest.mock import MagicMock, patch

from backend.database import Company, Job
from backend.playwright_app import classify_email, EMAIL_REJECTION_PHRASES
from backend.scraper import (
    extract_company_and_role_from_email_header,
    _resolve_role_from_email,
    _load_role_titles,
)


TARGET_SUBJECT = "Job Opportunity with Redwood- Principal Engineer - Hyderabad"
TARGET_BODY = (
    "Greetings from Recruise India Consulting Pvt. Ltd! "
    "Please find below the JD for Principal Engineer"
)


# -------------------------------------------------------------
# A. Recruiter / JD outreach classification
# -------------------------------------------------------------

def test_recruiter_outreach_classifies_as_job_alert():
    cls = classify_email(TARGET_SUBJECT, TARGET_BODY, "TA <recruiter@recruise.in>")
    assert cls["category"] == "job_alert"
    assert cls["confidence"] >= 0.6


def test_status_email_wins_over_alert_cue():
    # A rejection email that also says "on behalf of" must still classify as rejection.
    cls = classify_email(
        "Update on your application",
        "On behalf of the team, unfortunately we will not be proceeding.",
        "jobs@acme.com",
    )
    assert cls["category"] == "rejection"


# -------------------------------------------------------------
# B. Employer / agency / role / location extraction
# -------------------------------------------------------------

def test_extract_employer_agency_role_location(monkeypatch):
    monkeypatch.setattr("backend.scraper._company_aliases_cache", [])
    res = extract_company_and_role_from_email_header(
        TARGET_SUBJECT, TARGET_BODY, "TA <recruiter@recruise.in>"
    )
    assert res["employer"] == "Redwood"
    assert res["agency"] == "Recruise"
    assert res["company"] == "Redwood"          # employer preferred for dedup
    assert res["role"] == "Principal Engineer"  # location peeled off
    assert res["location"] == "Hyderabad"


def test_extract_falls_back_to_agency_when_no_employer(monkeypatch):
    monkeypatch.setattr("backend.scraper._company_aliases_cache", [])
    res = extract_company_and_role_from_email_header(
        "Interview Confirmation", "Schedule a time", "TA <talent@globex.com>"
    )
    assert res["employer"] is None
    assert res["agency"] == "Globex"
    assert res["company"] == "Globex"


def test_role_not_fabricated_when_unresolved(monkeypatch):
    monkeypatch.setattr("backend.scraper._company_aliases_cache", [])
    # No role token anywhere -> role must be None, never a fabricated "Software Engineer".
    assert _resolve_role_from_email("Weekly newsletter", "Here are some articles.") is None
    res = extract_company_and_role_from_email_header(
        "Weekly newsletter", "Here are some articles.", "news@blog.com"
    )
    assert res["role"] is None


def test_role_titles_are_data_driven():
    # The ladder comes from data/role_titles.json, not hardcoded source.
    titles = _load_role_titles()
    assert "Solutions Architect" in titles
    assert "Software Engineer" in titles


# -------------------------------------------------------------
# C. Full-body fetch (issue #82 B)
# -------------------------------------------------------------

def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def test_fetch_message_body_prefers_plain_text():
    from backend.gmail_client import fetch_message_body

    svc = MagicMock()
    svc.users().messages().get.return_value.execute.return_value = {
        "payload": {
            "mimeType": "multipart/alternative",
            "parts": [
                {"mimeType": "text/plain", "body": {"data": _b64("Plain JD body text")}},
                {"mimeType": "text/html", "body": {"data": _b64("<p>HTML JD body</p>")}},
            ],
        }
    }
    assert fetch_message_body(svc, "m1") == "Plain JD body text"


def test_fetch_message_body_html_fallback_strips_tags():
    from backend.gmail_client import fetch_message_body

    svc = MagicMock()
    svc.users().messages().get.return_value.execute.return_value = {
        "payload": {
            "mimeType": "text/html",
            "body": {"data": _b64("<html><body><p>Hello <b>World</b></p></body></html>")},
        }
    }
    body = fetch_message_body(svc, "m1")
    assert "Hello" in body and "World" in body
    assert "<" not in body


def test_fetch_message_body_is_bounded():
    from backend.gmail_client import fetch_message_body

    svc = MagicMock()
    long_text = "x" * 10000
    svc.users().messages().get.return_value.execute.return_value = {
        "payload": {"mimeType": "text/plain", "body": {"data": _b64(long_text)}},
    }
    assert len(fetch_message_body(svc, "m1", max_chars=500)) <= 500


# -------------------------------------------------------------
# D. Agency + employer identity on ingestion (issue #82 C)
# -------------------------------------------------------------

def test_ingest_opportunity_uses_employer_and_preserves_agency(db_session):
    from backend.main import _ingest_email_opportunities_to_db

    added = _ingest_email_opportunities_to_db(db_session, [{
        "company": "Redwood", "employer": "Redwood", "agency": "Recruise",
        "role": "Principal Engineer", "location": "Hyderabad",
        "event_type": "opportunity", "confidence": 0.85,
        "subject": TARGET_SUBJECT, "snippet": TARGET_BODY,
    }])
    assert added == 1
    job = db_session.query(Job).first()
    assert job.company.name == "Redwood"            # employer, not the agency
    assert job.title == "Principal Engineer"
    assert job.location == "Hyderabad"
    assert "Recruise" in job.source                 # agency preserved for context


def test_ingest_dedups_agency_jd_with_direct_posting(db_session):
    from backend.main import _ingest_email_opportunities_to_db

    comp = Company(name="Redwood", domain="redwood.com")
    db_session.add(comp)
    db_session.flush()
    direct = Job(company_id=comp.id, title="Principal Engineer",
                 url="https://redwood.com/jobs/1", status="Shortlisted")
    db_session.add(direct)
    db_session.commit()

    # An agency JD for the same employer+role must NOT create a duplicate.
    added = _ingest_email_opportunities_to_db(db_session, [{
        "company": "Redwood", "employer": "Redwood", "agency": "Recruise",
        "role": "Principal Engineer", "event_type": "opportunity", "confidence": 0.85,
        "subject": TARGET_SUBJECT, "snippet": TARGET_BODY,
    }])
    assert added == 0
    assert db_session.query(Job).count() == 1


# -------------------------------------------------------------
# E. Rejection / regret patterns (issue #82 D)
# -------------------------------------------------------------

def test_expanded_rejection_patterns_classify():
    samples = [
        "We have decided to move forward with other candidates.",
        "Unfortunately, we will not be proceeding with your application.",
        "We decided to go in a different direction.",
        "We regret to inform you that you were not successful.",
        "We have gone with another candidate.",
        "We will not be extending an offer at this time.",
    ]
    for body in samples:
        cls = classify_email("Your application", body, "jobs@acme.com")
        assert cls["category"] == "rejection", body


def test_thank_you_for_interest_alone_is_not_rejection():
    # Neutral opener on its own must NOT be classified as rejection.
    cls = classify_email("Welcome", "Thank you for your interest in Acme.", "hello@acme.com")
    assert cls["category"] != "rejection"


def test_thank_you_for_interest_with_rejection_cue_is_rejection():
    cls = classify_email(
        "Your application",
        "Thank you for your interest. Unfortunately we will not be moving forward.",
        "jobs@acme.com",
    )
    assert cls["category"] == "rejection"


def test_over_broad_application_status_phrase_removed():
    assert "application status" not in EMAIL_REJECTION_PHRASES