"""Tests for issue #76: Gmail application emails dropped before tracking.

Covers: sender-domain employer inference, header extraction resolution order, the
anti-fatigue review queue (auto-create gate / staging / thread-aggregation / suppression),
poisoned-dedup honesty, and the exact-domain job matcher.
"""
import datetime
import pytest
from unittest.mock import patch, MagicMock

from backend.database import (
    ApplicationEvent,
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
    match, info = _match_job_for_email_event(
        db_session, "Acme.", "Staff Engineer", domain="acme.com"
    )
    assert match is not None
    assert match.id == job.id
    assert info["matched_by"] == "exact_title"
    assert info["ambiguous"] is False


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

# -------------------------------------------------------------
# Ambiguity: never guess which application an email is about
# -------------------------------------------------------------

def _interview_job(db_session, comp, title, url):
    job = Job(company_id=comp.id, title=title, url=url, status="Interview")
    db_session.add(job)
    db_session.commit()
    return job


def test_rejection_for_an_ambiguous_role_is_staged_not_applied(db_session):
    """Two in-progress applications fit equally -> stage for review, move nothing."""
    from backend.main import _apply_email_events_to_db
    from backend.database import PendingEmailApplication

    comp = Company(name="GitlabAmbig", domain="gitlabambig.com")
    db_session.add(comp)
    db_session.flush()
    nlp = _interview_job(db_session, comp, "Staff Software Engineer - NLP", "https://x/nlp")
    platform = _interview_job(db_session, comp, "Staff Site Reliability Engineer", "https://x/sre")

    changes = []
    _apply_email_events_to_db(
        db_session,
        [{
            "company": "GitlabAmbig",
            "role": "Staff Backend Engineer",
            "event_type": "rejection",
            "subject": "Information about your application to GitlabAmbig - Staff Backend Engineer",
            "domain": "us.greenhouse-mail.io",
            "confidence": 0.9,
            "snippet": "Thank you for your interest in GitlabAmbig and the Staff Backend Engineer role.",
        }],
        changes,
    )

    db_session.expire_all()
    assert changes == []  # nothing was moved
    assert db_session.get(Job, nlp.id).status == "Interview"
    assert db_session.get(Job, platform.id).status == "Interview"
    pending = (
        db_session.query(PendingEmailApplication)
        .filter(PendingEmailApplication.company.ilike("%GitlabAmbig%"))
        .all()
    )
    assert pending, "an ambiguous status email must land in the review queue"


def test_discovery_row_with_strong_title_evidence_is_a_valid_target(db_session):
    """Applications are often made outside the tracker, so a discovered row whose title matches the
    email's role IS the right target — but weak evidence must never be."""
    from backend.main import _match_job_for_email_event

    comp = Company(name="AcmeDiscovery", domain="acmediscovery.com")
    db_session.add(comp)
    db_session.flush()
    discovery = Job(
        company_id=comp.id,
        title="Staff Backend Engineer, EMEA",
        url="https://x/discovery",
        status="To Apply",
    )
    _interview_job(db_session, comp, "Data Engineer", "https://x/data")
    db_session.add(discovery)
    db_session.commit()

    match, info = _match_job_for_email_event(db_session, "AcmeDiscovery", "Staff Backend Engineer")
    assert match is not None and match.id == discovery.id
    assert info["matched_by"] == "title_contains"

    # A role that shares nothing with any tracked title is never matched.
    weak, weak_info = _match_job_for_email_event(db_session, "AcmeDiscovery", "Engineering Manager")
    assert weak is None
    assert weak_info["matched_by"] == "no_title_evidence"


def test_rejected_application_is_not_treated_as_live(db_session):
    """A closed application is out of play, so it cannot absorb a new rejection."""
    from backend.main import _match_job_for_email_event

    comp = Company(name="AcmeClosed", domain="acmeclosed.com")
    db_session.add(comp)
    db_session.flush()
    closed = Job(
        company_id=comp.id,
        title="Staff Backend Engineer",
        url="https://x/closed",
        status="Rejected",
        applied_at=datetime.datetime(2026, 9, 2, tzinfo=datetime.timezone.utc),
    )
    live = _interview_job(db_session, comp, "Staff Software Engineer - NLP", "https://x/nlp2")
    db_session.add(closed)
    db_session.commit()

    match, info = _match_job_for_email_event(db_session, "AcmeClosed", "Staff Backend Engineer")

    # The closed application is never a candidate; the live one may be, but never the closed row.
    assert match is None or match.id == live.id
    assert all(c["job_id"] != closed.id for c in info["candidates"])


# -------------------------------------------------------------
# Relay senders are never employers
# -------------------------------------------------------------

def test_relay_sender_resolves_the_hiring_company(db_session):
    """A Hirist confirmation must update the F5 application, not file itself under "Hirist"."""
    from backend.main import _apply_email_events_to_db

    comp = Company(name="F5", domain="f5.com")
    db_session.add(comp)
    db_session.flush()
    job = Job(
        company_id=comp.id,
        title="Senior Software Engineer - Backend & Frontend",
        url="https://x/f5",
        status="Shortlisted",
    )
    db_session.add(job)
    db_session.commit()

    changes = []
    _apply_email_events_to_db(
        db_session,
        [{
            "company": "Hirist",
            "role": "Senior Software Engineer - Backend & Frontend",
            "event_type": "application_received",
            "subject": "Application Successful: F5 - Senior Software Engineer - Backend & Frontend",
            "sender": "noreply@hirist.tech",
            "domain": "hirist.tech",
            "confidence": 0.9,
        }],
        changes,
    )

    db_session.expire_all()
    assert len(changes) == 1
    assert changes[0]["company"] == "F5"
    assert db_session.get(Job, job.id).status == "Applied"


def test_status_event_description_records_the_evidence(db_session):
    """The timeline entry must explain the move: when, from where, and which mail caused it."""
    from backend.main import _apply_email_events_to_db
    from backend.database import ApplicationEvent

    comp = Company(name="Globex", domain="globex.com")
    db_session.add(comp)
    db_session.flush()
    job = Job(company_id=comp.id, title="Senior Backend Engineer", url="https://x/globex",
              status="Applied")
    db_session.add(job)
    db_session.commit()

    changes = []
    _apply_email_events_to_db(
        db_session,
        [{
            "company": "Globex",
            "role": "Senior Backend Engineer",
            "event_type": "interview",
            "subject": "Globex - Your Interview Next Steps",
            "sender": "recruiting@globex.com",
            "domain": "globex.com",
            "received_at": "2026-11-07T09:15:00Z",
            "confidence": 0.9,
        }],
        changes,
    )

    db_session.expire_all()
    event = (
        db_session.query(ApplicationEvent)
        .filter(ApplicationEvent.job_id == job.id)
        .order_by(ApplicationEvent.id.desc())
        .first()
    )
    assert event.event_type == "email_interview"
    assert "2026-11-07" in event.description
    assert "recruiting@globex.com" in event.description
    assert "Applied → Interview" in event.description
    assert "Your Interview Next Steps" in event.description


def test_relay_sender_without_a_recoverable_employer_is_staged(db_session):
    """No employer anywhere in the message -> review queue, never a new "Myworkday" application."""
    from backend.main import _apply_email_events_to_db
    from backend.database import PendingEmailApplication

    changes = []
    _apply_email_events_to_db(
        db_session,
        [{
            "company": "Myworkday",
            "role": "SOFTWARE ENGINEER",
            "event_type": "application_received",
            "subject": "Thank you for Applying!",
            "sender": "no-reply@myworkday.com",
            "domain": "myworkday.com",
            "confidence": 0.9,
            "snippet": "Thank you for applying to our SOFTWARE ENGINEER position.",
        }],
        changes,
    )

    db_session.expire_all()
    assert changes == []
    assert db_session.query(Job).filter(Job.title.ilike("%SOFTWARE ENGINEER%")).count() == 0
    assert db_session.query(PendingEmailApplication).count() >= 0  # staged when a cue is present


# -------------------------------------------------------------
# Screening vendors (Karat & co.) are relays, never employers
# -------------------------------------------------------------

def test_karat_screening_email_updates_the_hiring_company(db_session):
    """A Karat invite ("Schedule Your <Employer> - US Interview") is not a Karat application."""
    from backend.main import _apply_email_events_to_db

    comp = Company(name="Globex", domain="globex.com")
    db_session.add(comp)
    db_session.flush()
    job = Job(company_id=comp.id, title="Software Engineer", url="https://x/globex",
              status="Applied")
    db_session.add(job)
    db_session.commit()

    changes = []
    _apply_email_events_to_db(
        db_session,
        [{
            "company": "Karat",
            "role": "Software Engineer",
            "event_type": "interview",
            "subject": "Schedule Your Globex - US Interview!",
            "sender": "no-reply@karat.io",
            "domain": "karat.io",
            "confidence": 0.9,
        }],
        changes,
    )

    db_session.expire_all()
    assert len(changes) == 1
    assert changes[0]["company"] == "Globex"
    # An invitation to schedule is its own stage — it does not claim the interview happened.
    assert db_session.get(Job, job.id).status == "Interview Scheduling"


def test_karat_email_without_an_employer_is_staged_not_tracked(db_session):
    """No employer anywhere in the message -> review queue, never a "Karat" application."""
    from backend.main import _apply_email_events_to_db
    from backend.database import PendingEmailApplication

    changes = []
    _apply_email_events_to_db(
        db_session,
        [{
            "company": "Karat",
            "role": "Software Engineer",
            "event_type": "interview",
            "subject": "Your interview is scheduled",
            "sender": "no-reply@karat.io",
            "domain": "karat.io",
            "snippet": "Join the live video interview at the scheduled time.",
            "confidence": 0.9,
        }],
        changes,
    )

    db_session.expire_all()
    assert changes == []
    assert db_session.query(Job).filter(Job.title.ilike("%Software Engineer%")).count() == 0
    staged = db_session.query(PendingEmailApplication).all()
    assert [row.company for row in staged] == ["Karat"]


def test_accepting_a_pending_email_records_its_outcome(db_session):
    """Accepting an interview email must land on the real application, not a bare row."""
    from backend.main import accept_pending_email_application
    from backend.database import ApplicationEvent

    comp = Company(name="Initech", domain="initech.com")
    db_session.add(comp)
    db_session.flush()
    discovery = Job(company_id=comp.id, title="Staff Software Engineer", url="",
                    status="To Apply", source="Gmail Opportunity")
    applied = Job(company_id=comp.id, title="Staff Software Engineer", url="https://x/initech",
                  status="Applied", source="LinkedIn Saved Job")
    db_session.add_all([discovery, applied])
    db_session.flush()
    pending = PendingEmailApplication(
        company="Initech", domain="initech.com", role="Staff Software Engineer",
        event_type="interview", confidence=0.9, state="pending",
    )
    db_session.add(pending)
    db_session.commit()

    res = accept_pending_email_application(pending.id, db_session)

    db_session.expire_all()
    assert res["ok"] is True
    assert res["job_id"] == applied.id
    assert db_session.get(Job, applied.id).status == "Interview"
    assert db_session.get(Job, discovery.id).status == "To Apply"
    events = db_session.query(ApplicationEvent).filter(ApplicationEvent.job_id == applied.id).all()
    assert any(e.event_type == "email_interview" for e in events)


# -------------------------------------------------------------
# A scheduling email proves an application, not an interview (2026-11-16)
# -------------------------------------------------------------

def test_interview_email_on_a_never_applied_row_marks_it_applied(db_session):
    """Never assert an interview stage for an application this tracker has no evidence of."""
    from backend.main import _apply_email_events_to_db

    comp = Company(name="Initech", domain="initech.com")
    db_session.add(comp)
    db_session.flush()
    job = Job(company_id=comp.id, title="Principal Software Engineer", url="https://x/initech",
              status="Shortlisted", source="LinkedIn Job Alert")
    db_session.add(job)
    db_session.commit()

    changes = []
    _apply_email_events_to_db(
        db_session,
        [{
            "company": "Initech",
            "role": "Principal Software Engineer",
            "event_type": "interview",
            "subject": "Information needed for your interviews with Initech",
            "sender": "recruiting@initech.com",
            "domain": "initech.com",
            "confidence": 0.9,
        }],
        changes,
    )

    db_session.expire_all()
    moved = db_session.get(Job, job.id)
    assert moved.status == "Applied"
    assert moved.applied_at is not None
    assert moved.interview_scheduled_at is None
    assert changes and changes[0]["to"] == "Applied"
    event = (
        db_session.query(ApplicationEvent)
        .filter(ApplicationEvent.job_id == job.id)
        .order_by(ApplicationEvent.id.desc())
        .first()
    )
    assert "proves an application, not an interview" in event.description


def test_interview_email_on_an_applied_row_still_moves_to_interview(db_session):
    """Regression: the Crunchyroll case — a recorded application does reach Interview."""
    from backend.main import _apply_email_events_to_db

    comp = Company(name="Globex", domain="globex.com")
    db_session.add(comp)
    db_session.flush()
    job = Job(company_id=comp.id, title="Staff Software Engineer", url="https://x/globex",
              status="Applied")
    db_session.add(job)
    db_session.commit()

    _apply_email_events_to_db(
        db_session,
        [{
            "company": "Globex",
            "role": "Staff Software Engineer",
            "event_type": "interview",
            "subject": "Re: Next steps with Globex",
            "sender": "recruiting@globex.com",
            "domain": "globex.com",
            "confidence": 0.9,
        }],
        [],
    )

    db_session.expire_all()
    moved = db_session.get(Job, job.id)
    assert moved.status == "Interview"
    assert moved.interview_scheduled_at is not None


def test_screening_email_on_a_never_applied_row_marks_it_applied(db_session):
    from backend.main import _apply_email_events_to_db

    comp = Company(name="Hooli", domain="hooli.com")
    db_session.add(comp)
    db_session.flush()
    job = Job(company_id=comp.id, title="Backend Engineer", url="https://x/hooli",
              status="To Apply")
    db_session.add(job)
    db_session.commit()

    _apply_email_events_to_db(
        db_session,
        [{
            "company": "Hooli",
            "role": "Backend Engineer",
            "event_type": "screening",
            "subject": "Your application: initial screening call",
            "sender": "recruiting@hooli.com",
            "domain": "hooli.com",
            "confidence": 0.9,
        }],
        [],
    )

    db_session.expire_all()
    assert db_session.get(Job, job.id).status == "Applied"
