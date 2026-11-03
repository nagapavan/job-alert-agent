"""Tests for issue #76: Gmail application emails dropped before tracking.

Covers: sender-domain employer inference, header extraction resolution order, the
anti-fatigue review queue (auto-create gate / staging / thread-aggregation / suppression),
poisoned-dedup honesty, and the exact-domain job matcher.
"""
import datetime
import pytest
from unittest.mock import patch, MagicMock

from backend.database import (
    Company,
    Job,
    EmailSuppression,
    PendingEmailApplication,
)
from backend.scraper import (
    infer_company_from_sender_domain,
    extract_company_and_role_from_email_header,
    _is_ats_or_freemail_domain,
    _sender_domain,
)


# -------------------------------------------------------------
# Sender-domain inference
# -------------------------------------------------------------

def test_infer_company_from_sender_domain_basic():
    assert infer_company_from_sender_domain("Varsha <varsha@acme.com>") == "Acme"
    assert infer_company_from_sender_domain("varsha@acme.co.uk") == "Acme"
    # +tag / subdomain stripping
    assert infer_company_from_sender_domain("jobs+alert@globex.com") == "Globex"
    assert infer_company_from_sender_domain("noreply@engineering.initech.com") == "Initech"


def test_infer_company_from_sender_domain_rejects_ats_and_freemail():
    assert infer_company_from_sender_domain("jobs@greenhouse.io") is None
    assert infer_company_from_sender_domain("noreply@linkedin.com") is None
    assert infer_company_from_sender_domain("noreply@jobs.lever.co") is None
    assert infer_company_from_sender_domain("me@gmail.com") is None
    assert infer_company_from_sender_domain("me@outlook.com") is None
    assert infer_company_from_sender_domain("me@yahoo.com") is None
    assert infer_company_from_sender_domain("me@proton.me") is None


def test_is_ats_or_freemail_domain_subdomain_blocked():
    assert _is_ats_or_freemail_domain("jobs.lever.co") is True
    assert _is_ats_or_freemail_domain("mail.ashbyhq.com") is True
    assert _is_ats_or_freemail_domain("acme.com") is False


def test_sender_domain_helper():
    assert _sender_domain("jobs@acme.com") == "acme.com"
    assert _sender_domain("noreply@mail.initech.io") == "initech.io"
    assert _sender_domain("no-email") == ""


# -------------------------------------------------------------
# Header extraction: resolution order + role vocabulary
# -------------------------------------------------------------

def test_extract_company_prefers_sender_domain_when_subject_is_ambiguous(monkeypatch):
    monkeypatch.setattr("backend.scraper._company_aliases_cache", [])
    res = extract_company_and_role_from_email_header(
        subject="Thank you for applying",
        snippet="",
        sender="Acme Talent <talent@acme.com>",
    )
    assert res["company"] == "Acme"


def test_extract_company_resolves_pipe_and_about_patterns(monkeypatch):
    monkeypatch.setattr("backend.scraper._company_aliases_cache", [])
    # "| Acme" trailing (freemail sender → no sender-domain inference interference)
    res = extract_company_and_role_from_email_header(
        subject="Regarding your job application - Lead Engineer - Full Stack (AI) | Acme",
        snippet="",
        sender="recruiter@gmail.com",
    )
    assert res["company"] == "Acme"
    # role resolves as the full qualifier-bearing title, not a generic collapse
    assert "Lead Engineer" in res["role"]


def test_extract_role_resolves_full_stack_qualifier(monkeypatch):
    monkeypatch.setattr("backend.scraper._company_aliases_cache", [])
    res = extract_company_and_role_from_email_header(
        subject="Unable to reach you | Lead Engineer - Full Stack (AI) Acme",
        snippet="",
        sender="recruiter@gmail.com",
    )
    assert "Lead Engineer" in res["role"]


def test_extract_company_rejects_prepositional_junk(monkeypatch):
    monkeypatch.setattr("backend.scraper._company_aliases_cache", [])
    # "Applying to Harvey" must not become the company name.
    res = extract_company_and_role_from_email_header(
        subject="Applying to Harvey",
        snippet="",
        sender="recruiter@gmail.com",
    )
    assert res["company"] is None


def test_extract_company_strips_trailing_punctuation(monkeypatch):
    monkeypatch.setattr("backend.scraper._company_aliases_cache", [])
    res = extract_company_and_role_from_email_header(
        subject="Interview Availability at Acme.",
        snippet="",
        sender="recruiter@gmail.com",
    )
    assert res["company"] == "Acme"


def test_extract_aliases_still_win(monkeypatch):
    monkeypatch.setattr(
        "backend.scraper._company_aliases_cache", ["Enterpert"]
    )
    res = extract_company_and_role_from_email_header(
        subject="Principal Engineer role at Enterpert",
        snippet="",
        sender="Adarsh <adarsh@gmail.com>",
    )
    assert res["company"] == "Enterpert"


# -------------------------------------------------------------
# Review queue: auto-create gate + staging (via _apply_email_events_to_db)
# -------------------------------------------------------------

def test_email_auto_create_eligible_requires_real_domain(monkeypatch):
    from backend.main import _email_auto_create_eligible
    # ATS/freemail sender must NOT auto-create (goes to the review queue instead).
    assert _email_auto_create_eligible({
        "company": "Acme", "role": "Software Engineer", "domain": "gmail.com",
        "event_type": "interview", "confidence": 0.9,
        "subject": "Interview Confirmation", "snippet": "",
    }) is False
    # Real employer domain + app cue + high confidence → eligible.
    assert _email_auto_create_eligible({
        "company": "Acme", "role": "Software Engineer", "domain": "acme.com",
        "event_type": "interview", "confidence": 0.9,
        "subject": "Interview Confirmation", "snippet": "",
    }) is True


def test_email_auto_create_eligible_rejects_cold_outreach():
    from backend.main import _email_auto_create_eligible
    # Recruiter cold outreach ("opportunity", no app cue) must never auto-create.
    assert _email_auto_create_eligible({
        "company": "Acme", "role": "Software Engineer", "domain": "acme.com",
        "event_type": "interview", "confidence": 0.9,
        "subject": "Great opportunity to join our team", "snippet": "",
    }) is False


def test_apply_email_events_stages_when_not_auto_created(db_session):
    from backend.main import _apply_email_events_to_db
    # A valid status event from a real domain but an ATS sender domain (no company match)
    # is staged, not auto-created as a phantom job.
    _apply_email_events_to_db(db_session, [
        {
            "company": "Acme", "domain": "greenhouse.io", "role": "Staff Engineer",
            "event_type": "interview", "confidence": 0.9,
            "subject": "Interview Confirmation", "snippet": "Schedule a time",
        }
    ])
    db_session.commit()
    assert db_session.query(Job).count() == 0
    assert db_session.query(PendingEmailApplication).count() == 1


def test_apply_email_events_auto_creates_unambiguous(db_session):
    from backend.main import _apply_email_events_to_db
    _apply_email_events_to_db(db_session, [
        {
            "company": "Acme", "domain": "acme.com", "role": "Staff Engineer",
            "event_type": "interview", "confidence": 0.9,
            "subject": "Interview Confirmation", "snippet": "Schedule a time",
        }
    ])
    db_session.commit()
    job = db_session.query(Job).first()
    assert job is not None
    assert job.title == "Staff Engineer"
    assert job.company.name == "Acme"
    # No review-card for an auto-created job.
    assert db_session.query(PendingEmailApplication).count() == 0


def test_apply_email_events_thread_aggregates_repeat_emails(db_session):
    from backend.main import _apply_email_events_to_db
    for _ in range(3):
        _apply_email_events_to_db(db_session, [
            {
                "company": "Acme", "domain": "greenhouse.io", "role": "Staff Engineer",
                "event_type": "interview", "confidence": 0.9,
                "subject": "Interview Confirmation", "snippet": "Schedule a time",
            }
        ])
    db_session.commit()
    rows = db_session.query(PendingEmailApplication).all()
    assert len(rows) == 1
    assert rows[0].email_count == 3


def test_apply_email_events_honors_suppression(db_session):
    from backend.main import _apply_email_events_to_db
    db_session.add(EmailSuppression(domain="acme.com", role="Staff Engineer"))
    db_session.commit()
    _apply_email_events_to_db(db_session, [
        {
            "company": "Acme", "domain": "acme.com", "role": "Staff Engineer",
            "event_type": "interview", "confidence": 0.6,
            "subject": "Interview Confirmation", "snippet": "Schedule a time",
        }
    ])
    db_session.commit()
    # Suppressed: nothing staged, nothing auto-created.
    assert db_session.query(PendingEmailApplication).count() == 0
    assert db_session.query(Job).count() == 0


def test_unable_to_reach_queues_nothing(db_session):
    from backend.main import _apply_email_events_to_db
    # "unable to reach you" carries no application-reference cue → not staged.
    _apply_email_events_to_db(db_session, [
        {
            "company": "Acme", "domain": "acme.com", "role": "Lead Engineer",
            "event_type": "rejection", "confidence": 0.8,
            "subject": "Unable to reach you", "snippet": "",
        }
    ])
    db_session.commit()
    assert db_session.query(PendingEmailApplication).count() == 0
    assert db_session.query(Job).count() == 0


def test_freemail_sender_creates_nothing(db_session):
    from backend.main import _apply_email_events_to_db
    _apply_email_events_to_db(db_session, [
        {
            "company": "Acme", "domain": "gmail.com", "role": "Staff Engineer",
            "event_type": "interview", "confidence": 0.9,
            "subject": "Interview Confirmation", "snippet": "",
        }
    ])
    db_session.commit()
    assert db_session.query(Job).count() == 0
    assert db_session.query(PendingEmailApplication).count() == 0


# -------------------------------------------------------------
# Exact-domain job matcher
# -------------------------------------------------------------

def test_match_job_for_email_event_prefers_exact_domain(db_session):
    from backend.main import _match_job_for_email_event
    comp = Company(name="Acme", domain="acme.com")
    db_session.add(comp)
    db_session.flush()
    job = Job(
        company_id=comp.id, title="Staff Engineer", url="https://acme.com/se",
        status="Shortlisted",
    )
    db_session.add(job)
    db_session.commit()
    # Sender domain acme.com (from a cleaned address) finds the tracked job even when the
    # parsed company name carries trailing punctuation / differs.
    match = _match_job_for_email_event(
        db_session, "Acme.", "Staff Engineer", domain="acme.com"
    )
    assert match is not None
    assert match.id == job.id


# -------------------------------------------------------------
# Review queue endpoints
# -------------------------------------------------------------

def test_pending_endpoints_accept_and_ignore(db_session):
    from backend.main import app
    from conftest import test_client as client

    pending = PendingEmailApplication(
        company="Acme", domain="acme.com", role="Staff Engineer",
        event_type="interview", confidence=0.6, email_count=1, state="pending",
    )
    db_session.add(pending)
    db_session.commit()
    pid = pending.id

    listing = client.get("/api/email/applications/pending")
    assert listing.status_code == 200
    assert listing.json()["count"] == 1

    accept = client.post(f"/api/email/applications/pending/{pid}/accept")
    assert accept.status_code == 200
    assert accept.json()["ok"] is True
    db_session.commit()
    assert db_session.query(Job).count() == 1
    assert db_session.query(PendingEmailApplication).first().state == "accepted"


def test_pending_ignore_creates_suppression(db_session):
    from conftest import test_client as client

    pending = PendingEmailApplication(
        company="Acme", domain="acme.com", role="Staff Engineer",
        event_type="interview", confidence=0.6, email_count=1, state="pending",
    )
    db_session.add(pending)
    db_session.commit()
    pid = pending.id

    res = client.post(f"/api/email/applications/pending/{pid}/ignore")
    assert res.json()["ok"] is True
    db_session.commit()
    assert db_session.query(EmailSuppression).count() == 1


# -------------------------------------------------------------
# Poisoned-dedup honesty (issue #76: unresolved mail is NOT burned)
# -------------------------------------------------------------

@patch("backend.scraper.extract_company_and_role_from_email_header", return_value={"company": None, "role": None})
@patch("backend.gmail_client.build_service")
@patch("backend.config.get_gmail_settings")
def test_run_gmail_sync_does_not_burn_unresolvable_mail(mock_settings, mock_build_service, mock_extract, db_session):
    """A status email whose employer can't be resolved must NOT be marked ProcessedEmail."""
    from backend.gmail_client import run_gmail_sync
    from backend.database import ProcessedEmail

    mock_settings.return_value = {
        "client_id": "c", "client_secret": "s", "refresh_token": "r",
        "token_email": "me@example.com", "history_id": "", "connected_at": "",
    }
    svc = MagicMock()
    svc.users().messages().list.return_value.execute.return_value = {"messages": [{"id": "m1"}]}
    svc.users().messages().get.return_value.execute.return_value = {
        "id": "m1", "snippet": "Unfortunately, we won't be moving forward",
        "payload": {"headers": [
            {"name": "From", "value": "noreply@example.com"},
            {"name": "Subject", "value": "Your application"},
            {"name": "Date", "value": "today"},
        ]},
    }
    mock_build_service.return_value = svc

    events = run_gmail_sync(db_session)
    db_session.commit()
    # No company resolved -> no event, and NOT marked processed (so a pattern fix can re-read it).
    assert events == []
    assert db_session.query(ProcessedEmail).count() == 0


@patch("backend.scraper.extract_company_and_role_from_email_header")
@patch("backend.gmail_client.build_service")
@patch("backend.config.get_gmail_settings")
def test_run_gmail_sync_marks_only_resolved_mail(mock_settings, mock_build_service, mock_extract, db_session):
    """A resolvable status email IS marked processed; dedup holds across runs."""
    from backend.gmail_client import run_gmail_sync
    from backend.database import ProcessedEmail

    mock_settings.return_value = {
        "client_id": "c", "client_secret": "s", "refresh_token": "r",
        "token_email": "me@example.com", "history_id": "", "connected_at": "",
    }
    mock_extract.return_value = {"company": "Acme", "role": "Staff Engineer"}
    svc = MagicMock()
    svc.users().messages().list.return_value.execute.return_value = {"messages": [{"id": "m1"}]}
    svc.users().messages().get.return_value.execute.return_value = {
        "id": "m1", "snippet": "Interview Confirmation",
        "payload": {"headers": [
            {"name": "From", "value": "jobs@acme.com"},
            {"name": "Subject", "value": "Your application"},
            {"name": "Date", "value": "today"},
        ]},
    }
    mock_build_service.return_value = svc

    events = run_gmail_sync(db_session)
    db_session.commit()
    assert len(events) == 1
    assert db_session.query(ProcessedEmail).count() == 1
    assert run_gmail_sync(db_session) == []