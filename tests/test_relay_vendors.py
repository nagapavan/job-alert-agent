"""Interview-scheduling vendors are relays, never employers.

Shapes are copied from real scheduler mail; employers/candidates are invented.
"""

from backend.database import Company, Job
from backend.main import _employer_from_relay_email, _is_relay_sender
from backend.scraper import _resolve_role_from_email


class _NoDB:
    def query(self, *_args, **_kwargs):  # pragma: no cover - only hit on the DB fallback
        raise AssertionError("the subject patterns must resolve the employer")


def _event(subject, company="Goodtime", domain="goodtime.io"):
    return {
        "company": company,
        "domain": domain,
        "subject": subject,
        "sender": "Recruiter (via GoodTime.io) <scheduler@goodtime.io>",
    }


def test_scheduling_vendor_is_a_relay():
    assert _is_relay_sender(_event("Please Confirm: Your Acme Virtual Interview"))
    assert _is_relay_sender(_event("x", company="Modernloop", domain="modernloop.io"))
    # A real employer domain is not a relay.
    assert not _is_relay_sender(_event("Update", company="Acme", domain="acme.com"))


def test_employer_is_recovered_from_scheduler_subjects():
    assert _employer_from_relay_email(
        _NoDB(), _event("Please Confirm: Your Globex Virtual Interview")
    ) == "Globex"
    assert _employer_from_relay_email(
        _NoDB(),
        _event("Reminder - Initech Virtual Interview - Alex Doe for Staff Engineer: Alex Doe"),
    ) == "Initech"


def test_interview_subject_yields_the_full_role_not_a_generic_one():
    assert _resolve_role_from_email(
        "Reminder - Acme Virtual Interview - Alex Doe for Staff Software Engineer "
        "(Platform, Kafka): Alex Doe",
        "",
    ) == "Staff Software Engineer (Platform, Kafka)"
    assert _resolve_role_from_email(
        "Invitation from an unknown sender: Staff Software Engineer (Platform) with Acme",
        "",
    ) == "Staff Software Engineer (Platform)"
    # A plain scheduling confirmation names no role.
    assert _resolve_role_from_email("Please Confirm: Your Acme Virtual Interview", "") is None


def test_scheduler_mail_updates_the_real_application_not_the_vendor(db_session):
    from backend.main import _apply_email_events_to_db

    comp = Company(name="GlobexRelayTest", domain="globexrelaytest.com")
    db_session.add(comp)
    db_session.flush()
    job = Job(
        company_id=comp.id,
        title="Staff Software Engineer (Platform)",
        url="https://x/globex",
        status="Applied",
    )
    db_session.add(job)
    db_session.commit()

    _apply_email_events_to_db(db_session, [{
        "company": "Goodtime",
        "domain": "goodtime.io",
        "sender": "Recruiter (via GoodTime.io) <scheduler@goodtime.io>",
        "role": "Staff Software Engineer (Platform)",
        "event_type": "interview",
        "subject": "Please Confirm: Your GlobexRelayTest Virtual Interview",
        "confidence": 0.9,
    }])

    db_session.expire_all()
    # No "Goodtime" company row, and the tracked application carries the stage change.
    assert db_session.query(Company).filter(Company.name.ilike("Goodtime")).first() is None
    # A "Please Confirm" mail is a booked interview, not an invitation to schedule.
    assert db_session.get(Job, job.id).status == "Interview"
