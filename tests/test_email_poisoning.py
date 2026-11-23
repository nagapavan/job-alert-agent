"""Email attribution must not poison a sibling application.

The shapes are the ones that actually misfired on 2026-11-16 (generic role, two roles at one
employer); the employers and titles are invented.
"""

from backend.database import Job, PendingEmailApplication, Company
from backend.main import _apply_email_events_to_db, _title_tokens


def _company(db, name="GlobexAttribution", domain="globexattribution.com"):
    comp = Company(name=name, domain=domain)
    db.add(comp)
    db.flush()
    return comp


def test_engineering_and_engineer_are_the_same_token():
    assert _title_tokens("Software Engineering PMTS") == _title_tokens("Software Engineer PMTS")


def test_generic_role_does_not_apply_to_a_sibling_application(db_session):
    """A mail whose role is just "Software Engineer" must not pick a sibling title."""
    comp = _company(db_session)
    real = Job(
        company_id=comp.id,
        title="Software Engineering PMTS (Apache, Kafka, Terraform)",
        url="https://x/real",
        status="Applied",
    )
    sibling = Job(
        company_id=comp.id,
        title="Lead Software Engineer, Python/Flask",
        url="https://x/sibling",
        status="Shortlisted",
    )
    db_session.add_all([real, sibling])
    db_session.commit()

    _apply_email_events_to_db(db_session, [{
        "company": "GlobexAttribution",
        "domain": "globexattribution.com",
        "sender": "Recruiter <recruiting@globexattribution.com>",
        "role": "Software Engineer",
        "event_type": "interview",
        "subject": "Information needed for your interviews with GlobexAttribution",
        "confidence": 0.9,
    }])

    db_session.expire_all()
    assert db_session.get(Job, sibling.id).status == "Shortlisted"
    assert db_session.get(Job, sibling.id).applied_at is None
    assert db_session.get(Job, real.id).status == "Applied"
    # Nothing was guessed: the mail waits in the review queue instead.
    assert db_session.query(PendingEmailApplication).count() == 1


def test_the_role_named_in_the_mail_wins(db_session):
    """With the real title in the mail, the matching application moves — not the sibling."""
    comp = _company(db_session, name="InitechAttribution", domain="initechattribution.com")
    real = Job(
        company_id=comp.id,
        title="Software Engineering PMTS (Apache, Kafka, Terraform)",
        url="https://x/real2",
        status="Applied",
    )
    sibling = Job(
        company_id=comp.id,
        title="Lead Software Engineer, Python/Flask",
        url="https://x/sibling2",
        status="Shortlisted",
    )
    db_session.add_all([real, sibling])
    db_session.commit()

    _apply_email_events_to_db(db_session, [{
        "company": "InitechAttribution",
        "domain": "initechattribution.com",
        "sender": "Recruiter <recruiting@initechattribution.com>",
        "role": "Software Engineering PMTS (Apache, Kafka, Terraform)",
        "event_type": "interview",
        "subject": "Please Confirm: Your InitechAttribution Virtual Interview",
        "confidence": 0.9,
    }])

    db_session.expire_all()
    assert db_session.get(Job, real.id).status == "Interview"
    assert db_session.get(Job, sibling.id).status == "Shortlisted"


def test_weak_title_overlap_does_not_create_an_application(db_session):
    """Two shared words are not proof: a Shortlisted row must not become Applied on a guess."""
    comp = _company(db_session, name="UmbrellaAttribution", domain="umbrellaattribution.com")
    sibling = Job(
        company_id=comp.id,
        title="Staff Software Engineer, Payments",
        url="https://x/pay",
        status="Shortlisted",
    )
    db_session.add(sibling)
    db_session.commit()

    _apply_email_events_to_db(db_session, [{
        "company": "UmbrellaAttribution",
        "domain": "umbrellaattribution.com",
        "sender": "Recruiter <recruiting@umbrellaattribution.com>",
        "role": "Staff Software Engineer, Growth",
        "event_type": "interview",
        "subject": "Invitation to interview",
        "confidence": 0.9,
    }])

    db_session.expire_all()
    assert db_session.get(Job, sibling.id).status == "Shortlisted"
    assert db_session.query(PendingEmailApplication).count() == 1
