"""Email → application attribution keys: requisition ids, thread inheritance, link records.

The defect class these guard against: attribution was inferred from company + title alone and
stored as prose, so a status mail for one requisition could be filed against a sibling role at
the same employer, and nothing could be re-checked afterwards.
"""
import datetime

import pytest

from backend.database import (
    ApplicationEvent,
    Company,
    EmailApplicationLink,
    Job,
    PendingEmailApplication,
)
from backend.requisition import extract_requisition_id


# -------------------------------------------------------------
# Requisition extraction
# -------------------------------------------------------------
@pytest.mark.parametrize(
    "text, expected_id, expected_source",
    [
        ("Thank you for applying - R262578 Principal System Software Architect", "R262578", "workday"),
        ("Information needed for your interviews: Principal Software Engineer (200045102)", "200045102", "generic"),
        ("Thank You For Applying! JR100171 AI Staff SW Systems Engineer", "JR100171", "workday"),
        ("https://boards.greenhouse.io/acme/jobs/8162316?gh_jid=8162316", "8162316", "greenhouse"),
        ("https://workforcenow.adp.com/x?cid=1&ccId=2&jobId=589285", "589285", "adp"),
        ("Thanks for your time", "", ""),
    ],
)
def test_extract_requisition_id(text, expected_id, expected_source):
    assert extract_requisition_id(text) == (expected_id, expected_source)


# -------------------------------------------------------------
# Matcher: strongest key first
# -------------------------------------------------------------
def _company_with_principal_rows(db):
    comp = Company(name="Initech", domain="initech.com")
    db.add(comp)
    db.flush()
    targeted = Job(
        company_id=comp.id,
        title="Principal Software Engineer",
        status="Shortlisted",
        requisition_id="200045102",
    )
    sibling = Job(
        company_id=comp.id,
        title="Principal Software Engineer",
        status="Shortlisted",
        requisition_id="200099999",
    )
    db.add_all([targeted, sibling])
    db.commit()
    return comp, targeted, sibling


def test_requisition_id_wins_over_an_ambiguous_title(db_session):
    from backend.main import _match_job_for_email_event

    comp, targeted, _sibling = _company_with_principal_rows(db_session)

    job, info = _match_job_for_email_event(
        db_session,
        "Initech",
        "Principal Software Engineer",
        "initech.com",
        requisition_id="200045102",
    )

    assert job is not None and job.id == targeted.id
    assert info["matched_by"] == "requisition"


def test_without_a_requisition_the_same_rows_are_ambiguous(db_session):
    """Regression: the ambiguity that let a requisition land on a sibling row."""
    from backend.main import _match_job_for_email_event

    _company_with_principal_rows(db_session)
    job, info = _match_job_for_email_event(
        db_session, "Initech", "Principal Software Engineer", "initech.com"
    )
    assert job is None
    assert info["ambiguous"] is True


def test_seniority_and_job_family_alone_are_not_title_evidence(db_session):
    """A decision mail for one requisition must not land on a sibling role.

    "Staff Backend Engineer" and "Staff Software Engineer - NLP" share exactly {staff, engineer};
    counting that as overlap attached an outcome to the wrong application at the same employer.
    """
    from backend.main import _match_job_for_email_event

    comp = Company(name="Hooli", domain="hooli.com")
    db_session.add(comp)
    db_session.flush()
    sibling = Job(
        company_id=comp.id,
        title="Staff Software Engineer - NLP",
        status="Applied",
        applied_at=datetime.datetime(2026, 9, 27, tzinfo=datetime.timezone.utc),
    )
    db_session.add(sibling)
    db_session.commit()

    job, info = _match_job_for_email_event(
        db_session, "Hooli", "Staff Backend Engineer", "hooli.com"
    )

    assert job is None
    assert info["matched_by"] == "no_title_evidence"


def test_a_shared_specialisation_still_matches(db_session):
    """The stricter overlap rule must not stop a real match on the specialisation."""
    from backend.main import _match_job_for_email_event

    comp = Company(name="Hooli", domain="hooli.com")
    db_session.add(comp)
    db_session.flush()
    backend_row = Job(
        company_id=comp.id,
        title="Backend Engineer, Payments",
        status="Applied",
        applied_at=datetime.datetime(2026, 9, 27, tzinfo=datetime.timezone.utc),
    )
    db_session.add(backend_row)
    db_session.commit()

    job, info = _match_job_for_email_event(
        db_session, "Hooli", "Staff Backend Engineer", "hooli.com"
    )

    assert job is not None and job.id == backend_row.id
    assert info["matched_by"] in ("title_contains", "title_overlap")


def test_a_reply_inherits_its_threads_application(db_session):
    from backend.main import _match_job_for_email_event

    comp = Company(name="Globex", domain="globex.com")
    db_session.add(comp)
    db_session.flush()
    job = Job(company_id=comp.id, title="Staff Software Engineer", status="Applied")
    db_session.add(job)
    db_session.flush()
    db_session.add(
        EmailApplicationLink(message_id="m-1", thread_id="thread-9", job_id=job.id,
                             method="exact_title")
    )
    db_session.commit()

    # A follow-up with no company and no role: the thread is the only evidence.
    matched, info = _match_job_for_email_event(db_session, "", "", "", thread_id="thread-9")

    assert matched is not None and matched.id == job.id
    assert info["matched_by"] == "thread"


def test_a_thread_pointing_at_a_closed_row_is_not_inherited(db_session):
    from backend.main import _match_job_for_email_event

    comp = Company(name="Hooli", domain="hooli.com")
    db_session.add(comp)
    db_session.flush()
    job = Job(company_id=comp.id, title="Backend Engineer", status="Rejected")
    db_session.add(job)
    db_session.flush()
    db_session.add(EmailApplicationLink(message_id="m-2", thread_id="thread-10", job_id=job.id))
    db_session.commit()

    matched, _info = _match_job_for_email_event(db_session, "", "", "", thread_id="thread-10")
    assert matched is None


# -------------------------------------------------------------
# Link persistence
# -------------------------------------------------------------
def _applied_job(db, title="Staff Software Engineer", req=None):
    comp = Company(name="Globex", domain="globex.com")
    db.add(comp)
    db.flush()
    job = Job(company_id=comp.id, title=title, url="https://x/globex", status="Applied",
              requisition_id=req)
    db.add(job)
    db.commit()
    return job


def test_applying_an_email_records_the_link(db_session):
    from backend.main import _apply_email_events_to_db

    job = _applied_job(db_session)
    _apply_email_events_to_db(
        db_session,
        [{
            "company": "Globex",
            "role": "Staff Software Engineer",
            "event_type": "interview",
            "subject": "Next steps with Globex",
            "sender": "recruiting@globex.com",
            "domain": "globex.com",
            "confidence": 0.9,
            "message_id": "msg-abc",
            "thread_id": "thread-abc",
            "date": "Tue, 10 Nov 2026 11:56:38 +0000",
        }],
        [],
    )

    links = db_session.query(EmailApplicationLink).all()
    assert len(links) == 1
    link = links[0]
    assert link.message_id == "msg-abc"
    assert link.thread_id == "thread-abc"
    assert link.job_id == job.id
    assert link.method == "exact_title"
    assert link.sender == "recruiting@globex.com"
    assert "Next steps" in link.subject


def test_the_same_message_can_only_be_linked_once(db_session):
    """A re-sync (or a duplicated event) must not create a second link row."""
    from backend.main import _apply_email_events_to_db

    _applied_job(db_session)
    event = {
        "company": "Globex",
        "role": "Staff Software Engineer",
        "event_type": "interview",
        "subject": "Next steps with Globex",
        "sender": "recruiting@globex.com",
        "domain": "globex.com",
        "confidence": 0.9,
        "message_id": "msg-dup",
    }
    _apply_email_events_to_db(db_session, [event], [])
    _apply_email_events_to_db(db_session, [event], [])

    assert db_session.query(EmailApplicationLink).count() == 1


def test_a_strong_match_teaches_the_row_its_requisition_id(db_session):
    from backend.main import _apply_email_events_to_db

    job = _applied_job(db_session, req=None)
    _apply_email_events_to_db(
        db_session,
        [{
            "company": "Globex",
            "role": "Staff Software Engineer",
            "event_type": "interview",
            "subject": "Interview for requisition R123456",
            "sender": "recruiting@globex.com",
            "domain": "globex.com",
            "confidence": 0.9,
            "message_id": "msg-learn",
        }],
        [],
    )

    db_session.expire_all()
    learned = db_session.get(Job, job.id)
    assert learned.requisition_id == "R123456"
    assert learned.requisition_source == "workday"


def test_a_weak_match_does_not_bind_a_requisition_id(db_session):
    """Only an unambiguous match may teach an id — otherwise the next mail follows the mistake."""
    from backend.main import _apply_email_events_to_db

    job = _applied_job(db_session, title="Principal Software Engineer", req=None)
    _apply_email_events_to_db(
        db_session,
        [{
            "company": "Globex",
            # Shares two tokens with the title but is not a subset either way -> tier 3.
            "role": "Principal Engineer Backend",
            "event_type": "interview",
            "subject": "Interview for requisition R654321",
            "sender": "recruiting@globex.com",
            "domain": "globex.com",
            "confidence": 0.9,
            "message_id": "msg-weak",
        }],
        [],
    )

    db_session.expire_all()
    assert db_session.get(Job, job.id).requisition_id is None


# -------------------------------------------------------------
# Staged emails keep their identity through acceptance
# -------------------------------------------------------------
def test_accepting_a_staged_email_records_the_link(db_session):
    from backend.main import accept_pending_email_application

    pending = PendingEmailApplication(
        company="Initech",
        domain="initech.com",
        role="Staff Software Engineer",
        event_type="application_received",
        confidence=0.9,
        state="pending",
        message_id="msg-pending",
        thread_id="thread-pending",
    )
    db_session.add(pending)
    db_session.commit()

    result = accept_pending_email_application(pending.id, db_session)

    db_session.expire_all()
    assert result["ok"] is True
    link = db_session.query(EmailApplicationLink).filter(
        EmailApplicationLink.message_id == "msg-pending"
    ).one()
    assert link.job_id == result["job_id"]
    assert link.thread_id == "thread-pending"


# -------------------------------------------------------------
# Relay sender tenant
# -------------------------------------------------------------
def test_relay_local_part_resolves_a_company_prefix(db_session):
    """`walmart@myworkday.com` is Walmart Global Tech; the tenant used to be discarded."""
    from backend.main import _employer_from_relay_email

    db_session.add(Company(name="Walmart Global Tech", domain="walmart.com"))
    db_session.add(Company(name="Salesforce", domain="salesforce.com"))
    db_session.commit()

    assert (
        _employer_from_relay_email(
            db_session,
            {
                "company": "Myworkday",
                "domain": "myworkday.com",
                "sender": "walmart@myworkday.com",
                "subject": "Thank you for Applying!",
                "snippet": "",
            },
        )
        == "Walmart Global Tech"
    )
    assert (
        _employer_from_relay_email(
            db_session,
            {
                "company": "Myworkday",
                "domain": "myworkday.com",
                "sender": "salesforce@myworkday.com",
                "subject": "Thank you for applying",
                "snippet": "",
            },
        )
        == "Salesforce"
    )


def test_a_generic_requisition_id_is_scoped_to_the_resolved_company(db_session):
    """A generic id shape can collide across employers; the resolved company decides."""
    from backend.main import _match_job_for_email_event

    initech = Company(name="Initech", domain="initech.com")
    hooli = Company(name="Hooli", domain="hooli.com")
    db_session.add_all([initech, hooli])
    db_session.flush()
    at_initech = Job(company_id=initech.id, title="Engineer", requisition_id="123456789")
    at_hooli = Job(company_id=hooli.id, title="Engineer", requisition_id="123456789")
    db_session.add_all([at_initech, at_hooli])
    db_session.commit()

    job, info = _match_job_for_email_event(
        db_session, "Hooli", "", "hooli.com", requisition_id="123456789"
    )

    assert job is not None and job.id == at_hooli.id
    assert info["matched_by"] == "requisition"


def test_a_substring_company_does_not_match_a_different_employer(db_session):
    """`Acme` must not resolve to `Acmetools` (finding 64)."""
    from backend.main import _match_job_for_email_event

    other = Company(name="Acmetools")
    db_session.add(other)
    db_session.flush()
    db_session.add(Job(company_id=other.id, title="Engineer"))
    db_session.commit()

    job, info = _match_job_for_email_event(db_session, "Acme", "Engineer")
    assert job is None
    assert info["matched_by"] == "no_company"


def test_a_legal_suffix_still_matches(db_session):
    """Word-boundary matching must keep the 'Acme' -> 'Acme Corporation' case (finding 64)."""
    from backend.main import _match_job_for_email_event

    company = Company(name="Acme Corporation")
    db_session.add(company)
    db_session.flush()
    job = Job(company_id=company.id, title="Engineer")
    db_session.add(job)
    db_session.commit()

    matched, info = _match_job_for_email_event(db_session, "Acme", "Engineer")
    assert matched is not None and matched.id == job.id
    assert info["matched_by"] == "exact_title"
