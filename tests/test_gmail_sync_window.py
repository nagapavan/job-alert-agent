"""Gmail sync window: two passes, a persisted watermark, and the outreach guard.

Mail shapes are synthetic (Acme/Globex/Initech); no real message content is used.
"""

from unittest.mock import MagicMock, patch

from backend import gmail_client
from backend.database import ProcessedEmail


def test_status_query_excludes_alert_senders():
    query = gmail_client.build_status_query()
    assert "from:greenhouse.io" in query
    assert "from:hit-reply@linkedin.com" in query
    assert "from:inmail-hit-reply@linkedin.com" in query
    assert "-from:jobalerts-noreply@linkedin.com" in query
    assert "-from:jobs-noreply@linkedin.com" in query
    # The alert senders must not appear in the positive group.
    positive = query.split("-from:jobalerts-noreply")[0]
    assert "from:jobalerts-noreply@linkedin.com" not in positive
    assert "label:" not in query


def test_alert_query_only_lists_alert_senders():
    query = gmail_client.build_alert_query()
    assert "from:jobalerts-noreply@linkedin.com" in query
    assert "from:jobs-noreply@linkedin.com" in query
    assert "from:googlealerts-noreply@google.com" in query
    assert "greenhouse" not in query
    assert "hit-reply" not in query


def test_window_uses_the_watermark_with_a_day_of_overlap():
    assert gmail_client._window_clause(None, 90) == "newer_than:90d"
    assert gmail_client._window_clause("2026-11-16", 90) == "after:2026/11/15"
    # A malformed watermark must not break the query.
    assert gmail_client._window_clause("not-a-date", 30) == "newer_than:30d"


def test_sync_state_roundtrip(tmp_path, monkeypatch):
    state_file = tmp_path / "gmail_sync_state.json"
    monkeypatch.setattr(gmail_client, "SYNC_STATE_FILE", state_file)
    gmail_client.save_sync_state({"status": {"after": "2026-11-16"}})
    assert gmail_client.load_sync_state()["status"]["after"] == "2026-11-16"


def test_list_message_ids_for_pages_up_to_the_cap():
    svc = MagicMock()
    svc.users().messages().list.return_value.execute.return_value = {"messages": [{"id": "m1"}]}
    ids = gmail_client.list_message_ids_for(svc, "q", 1200)
    assert ids == ["m1"]
    assert svc.users().messages().list.call_args.kwargs["maxResults"] == 500


def _service_for(subject, sender, snippet, thread_id="t1"):
    svc = MagicMock()
    svc.users().messages().list.return_value.execute.return_value = {"messages": [{"id": "m1"}]}
    svc.users().messages().get.return_value.execute.return_value = {
        "id": "m1",
        "threadId": thread_id,
        "snippet": snippet,
        "payload": {"headers": [
            {"name": "From", "value": sender},
            {"name": "Subject", "value": subject},
            {"name": "Date", "value": "today"},
        ]},
    }
    return svc


SETTINGS = {
    "client_id": "c", "client_secret": "s", "refresh_token": "r",
    "token_email": "me@example.com", "history_id": "", "connected_at": "",
}


@patch("backend.scraper.parse_google_alerts_digest", return_value=[])
@patch("backend.scraper.parse_linkedin_alert_cards", return_value=[])
@patch("backend.scraper.extract_company_and_role_from_email_header")
@patch("backend.gmail_client.build_service")
@patch("backend.config.get_gmail_settings")
def test_outreach_body_cue_does_not_become_an_interview_event(
    mock_settings, mock_build, mock_extract, _mock_cards, _mock_digest, db_session, tmp_path, monkeypatch
):
    monkeypatch.setattr(gmail_client, "SYNC_STATE_FILE", tmp_path / "state.json")
    mock_settings.return_value = dict(SETTINGS)
    mock_extract.return_value = {"company": None, "role": None}
    mock_build.return_value = _service_for(
        "Sumithra sent you a message",
        "Recruiter <hit-reply@linkedin.com>",
        "reply here to discuss next steps",
    )

    assert gmail_client.run_gmail_sync(db_session) == []
    assert db_session.query(ProcessedEmail).count() == 0


@patch("backend.scraper.extract_company_and_role_from_email_header")
@patch("backend.gmail_client.build_service")
@patch("backend.config.get_gmail_settings")
def test_outreach_with_a_subject_cue_stays_a_status_event(
    mock_settings, mock_build, mock_extract, db_session, tmp_path, monkeypatch
):
    monkeypatch.setattr(gmail_client, "SYNC_STATE_FILE", tmp_path / "state.json")
    mock_settings.return_value = dict(SETTINGS)
    mock_extract.return_value = {"company": "Globex", "role": "Staff Software Engineer"}
    mock_build.return_value = _service_for(
        "Interview invitation for Staff Software Engineer",
        "Recruiter <hit-reply@linkedin.com>",
        "Please pick a time",
    )

    events = gmail_client.run_gmail_sync(db_session)
    assert [e["event_type"] for e in events] == ["interview"]
    assert events[0]["company"] == "Globex"
