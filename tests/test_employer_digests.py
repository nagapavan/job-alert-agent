"""Employer career-portal digests ("New jobs at …", "New jobs posted from careers.…").

Subjects are copied from real mail; employers are invented where the real ones are not needed.
"""

from unittest.mock import MagicMock, patch

from backend.gmail_client import build_status_query, is_google_alerts_sender
from backend.main import _employer_from_relay_email
from backend.playwright_app import classify_email
from backend.scraper import extract_company_and_role_from_email_header

DIGEST_SUBJECTS = (
    ("Microsoft Careers <careers@microsoft.com>", "New jobs at Microsoft that match your profile"),
    ("EYJobAlerts <eyjobalerts@ey.com>", "New jobs posted from careers.ey.com"),
    ("MyGreenhouse <no-reply@us.greenhouse-mail.io>", "New Jobs at Acme - Here's your weekly update"),
    ("bellmicros-jobnotif <jobnotif@bell-integration.com>",
     "New jobs posted from careers.bell-integration.com"),
    ("Google Careers <careers-noreply@google.com>", "New job(s) match your search on Google Careers"),
)


def test_career_portal_digests_classify_as_job_alerts():
    for sender, subject in DIGEST_SUBJECTS:
        cls = classify_email(subject, "We've found exciting new opportunities tailored for you!", sender)
        assert cls["category"] == "job_alert", (subject, cls)


def test_status_query_covers_digest_subjects():
    query = build_status_query()
    for cue in (
        'subject:"new job"',
        'subject:"new jobs"',
        'subject:"match your search"',
        'subject:"jobs for you"',
        'subject:"open positions"',
    ):
        assert cue in query, cue


def test_relay_digest_resolves_the_employer_from_the_subject():
    class _NoDB:
        def query(self, *_args, **_kwargs):  # pragma: no cover - subject patterns must resolve
            raise AssertionError("no DB lookup expected")

    employer = _employer_from_relay_email(
        _NoDB(),
        {
            "subject": "New Jobs at Acme - Here's your weekly update",
            "sender": "no-reply@us.greenhouse-mail.io",
            "company": "Mygreenhouse",
            "domain": "greenhouse-mail.io",
        },
    )
    assert employer == "Acme"


def test_role_extraction_drops_markup_from_alert_bodies():
    extracted = extract_company_and_role_from_email_header(
        "New job(s) match your search on Google Careers",
        "Staff  \r\nSoftware Engineer, Search Intelligence</u></a></h5><span style='x'>",
        "Google Careers <careers-noreply@google.com>",
    )
    assert extracted["company"] == "Google"
    role = extracted["role"] or ""
    assert "<" not in role and ">" not in role


def test_google_parser_is_only_used_for_google_alerts(tmp_path, monkeypatch):
    assert is_google_alerts_sender("Google Alerts <googlealerts-noreply@google.com>")
    assert not is_google_alerts_sender("EYJobAlerts <eyjobalerts@ey.com>")

    from backend import gmail_client

    monkeypatch.setattr(gmail_client, "SYNC_STATE_FILE", tmp_path / "state.json")

    settings = {
        "client_id": "c", "client_secret": "s", "refresh_token": "r",
        "token_email": "me@example.com", "history_id": "", "connected_at": "",
    }
    svc = MagicMock()
    svc.users().messages().list.return_value.execute.return_value = {"messages": [{"id": "m1"}]}
    svc.users().messages().get.return_value.execute.return_value = {
        "id": "m1",
        "snippet": "New jobs match your profile",
        "payload": {"headers": [
            {"name": "From", "value": "EYJobAlerts <eyjobalerts@ey.com>"},
            {"name": "Subject", "value": "New jobs posted from careers.ey.com"},
            {"name": "Date", "value": "today"},
        ]},
    }

    with patch("backend.config.get_gmail_settings", return_value=settings), \
         patch("backend.gmail_client.build_service", return_value=svc), \
         patch("backend.scraper.parse_google_alerts_digest") as google_parser, \
         patch("backend.scraper.extract_company_and_role_from_email_header",
               return_value={"company": None, "role": None}):
        gmail_client.run_gmail_sync(MagicMock())

    google_parser.assert_not_called()
