"""Channel outcome aggregation + honest discovery run status (issue #75).

The defect these guard against: every discovery run was recorded as ``success`` even when a
requested channel never executed (Gmail API disconnected, optional dependency missing, fetcher
returning nothing for an unknown reason). "Nothing to find" and "this channel is dead" must not
look identical in the Operations log.
"""
from unittest.mock import patch

from backend import channel_status
from backend.database import Company, Job, OperationLog, UserPreference, get_user_preferences


# -------------------------------------------------------------
# Pure aggregation
# -------------------------------------------------------------


def test_all_healthy_is_success():
    outcomes = [
        {"channel": "gmail", "outcome": channel_status.OK, "reason": "3 matched"},
        {"channel": "hirist", "outcome": channel_status.OK, "reason": ""},
    ]
    assert channel_status.aggregate_status(outcomes) == "success"


def test_operator_disabled_channel_is_not_a_degradation():
    """A flag the operator turned off must not make every run look broken."""
    outcomes = [
        {"channel": "gmail", "outcome": channel_status.SKIPPED_FLAG, "reason": "disabled"},
        {"channel": "hirist", "outcome": channel_status.OK, "reason": ""},
    ]
    assert channel_status.aggregate_status(outcomes) == "success"


def test_single_degraded_channel_is_partial():
    outcomes = [
        {"channel": "gmail", "outcome": channel_status.NOT_CONNECTED, "reason": "no token"},
        {"channel": "hirist", "outcome": channel_status.OK, "reason": ""},
    ]
    assert channel_status.aggregate_status(outcomes) == "Partial"


def test_every_requested_channel_errored_is_failed():
    outcomes = [
        {"channel": "gmail", "outcome": channel_status.ERROR, "reason": "boom"},
        {"channel": "hirist", "outcome": channel_status.ERROR, "reason": "boom"},
    ]
    assert channel_status.aggregate_status(outcomes) == "Failed"


def test_degraded_without_exception_is_partial_not_failed():
    """A channel that ran but produced a meaningless zero is degraded, not failed."""
    outcomes = [
        {"channel": "gmail", "outcome": channel_status.NOT_CONNECTED, "reason": "no token"},
        {"channel": "hirist", "outcome": channel_status.DEGRADED_ZERO, "reason": "0 of 6"},
    ]
    assert channel_status.aggregate_status(outcomes) == "Partial"


def test_nothing_requested_stays_success():
    assert channel_status.aggregate_status([]) == "success"
    assert channel_status.aggregate_status(None) == "success"


def test_describe_lists_only_degraded_channels():
    outcomes = [
        {"channel": "gmail", "outcome": channel_status.NOT_CONNECTED, "reason": "no token"},
        {"channel": "hirist", "outcome": channel_status.OK, "reason": ""},
    ]
    text = channel_status.describe(outcomes)
    assert "gmail" in text and "not_connected" in text
    assert "hirist" not in text


def test_record_truncates_and_ignores_none_sink():
    outcomes = []
    channel_status.record(outcomes, "gmail", channel_status.ERROR, "x" * 1000)
    assert len(outcomes[0]["reason"]) <= channel_status.MAX_REASON_CHARS
    channel_status.record(None, "gmail", channel_status.ERROR, "ignored")  # must not raise


# -------------------------------------------------------------
# Gmail connection diagnostics
# -------------------------------------------------------------


def test_gmail_status_reports_expiry_reason():
    """A revoked token must surface why, instead of a bare 'not connected'."""
    from backend.config import save_gmail_settings
    from backend.gmail_client import get_status

    save_gmail_settings(
        client_id="client-id.apps.googleusercontent.com",
        client_secret="GOCSPX-test",
        refresh_token="",
        token_expired_at="2026-10-23T12:46:13+00:00",
        last_error="Google rejected the stored refresh token (invalid_grant).",
    )
    status = get_status()
    assert status["connected"] is False
    assert status["needs_reconnect"] is True
    assert status["token_expired_at"].startswith("2026-10-23")
    assert "invalid_grant" in status["last_error"]


def test_gmail_status_needs_reconnect_false_without_expiry():
    from backend.config import save_gmail_settings
    from backend.gmail_client import get_status

    save_gmail_settings(
        client_id="client-id.apps.googleusercontent.com",
        client_secret="GOCSPX-test",
        refresh_token="",
        token_expired_at="",
        last_error="",
    )
    status = get_status()
    assert status["connected"] is False
    assert status["needs_reconnect"] is False


def test_saving_new_refresh_token_clears_diagnostics():
    from backend.config import get_gmail_settings, save_gmail_settings

    save_gmail_settings(
        client_id="cid", client_secret="GOCSPX-test", refresh_token="",
        token_expired_at="2026-10-23T12:46:13+00:00", last_error="expired",
    )
    save_gmail_settings(refresh_token="fresh-token", token_expired_at="", last_error="")
    s = get_gmail_settings()
    assert s["refresh_token"] == "fresh-token"
    assert s["token_expired_at"] == ""
    assert s["last_error"] == ""


# -------------------------------------------------------------
# External sync run status
# -------------------------------------------------------------


def _enable_gmail(db_session):
    pref = get_user_preferences(db_session)
    if not pref:
        pref = UserPreference()
        db_session.add(pref)
    import json as _json

    flags = _json.loads(pref.features_json or "{}")
    flags["gmail_sync"] = True
    pref.features_json = _json.dumps(flags)
    db_session.commit()


def test_external_sync_records_partial_when_gmail_api_down_and_fallback_empty(db_session, monkeypatch):
    """Reproduces the reported symptom: API not connected, fallback found 0 -> not 'success'."""
    from backend.main import ExternalSyncRequest, _execute_external_sync

    _enable_gmail(db_session)

    with patch("backend.main.is_gmail_configured", return_value=False), patch(
        "backend.main.sync_email_application_events", return_value=[]
    ), patch("backend.main.sync_playwright") as fake_pw:
        fake_pw.return_value.__enter__.return_value = object()
        fake_pw.return_value.__exit__.return_value = False
        result = _execute_external_sync(
            ExternalSyncRequest(sources=["gmail"]), db_session
        )

    assert result.status == "Partial"
    gmail = [c for c in result.channels if c["channel"] == "gmail"]
    assert gmail and gmail[0]["outcome"] == channel_status.DEGRADED_ZERO
    assert "reconnect" in gmail[0]["reason"].lower()

    log = (
        db_session.query(OperationLog)
        .filter(OperationLog.operation_type == "external_application_sync")
        .order_by(OperationLog.id.desc())
        .first()
    )
    assert log is not None
    assert log.status == "Partial"
    assert "Degraded" in log.summary
    # details_json must stay valid JSON and carry the per-channel breakdown
    import json as _json

    parsed = _json.loads(log.details_json)
    assert parsed["channels"][0]["channel"] == "gmail"


def test_external_sync_records_success_when_api_path_runs(db_session):
    from backend.main import ExternalSyncRequest, _execute_external_sync

    _enable_gmail(db_session)

    with patch("backend.main.is_gmail_configured", return_value=True), patch(
        "backend.gmail_client.run_gmail_sync", return_value=[]
    ):
        result = _execute_external_sync(
            ExternalSyncRequest(sources=["gmail"]), db_session
        )

    assert result.status == "success"
    gmail = [c for c in result.channels if c["channel"] == "gmail"]
    assert gmail and gmail[0]["outcome"] == channel_status.OK


def test_external_sync_marks_disabled_channel_as_skipped(db_session):
    """gmail_sync off -> skipped, and the run is not marked degraded for it."""
    from backend.main import ExternalSyncRequest, _execute_external_sync

    result = _execute_external_sync(ExternalSyncRequest(sources=["gmail"]), db_session)
    gmail = [c for c in result.channels if c["channel"] == "gmail"]
    assert gmail and gmail[0]["outcome"] == channel_status.SKIPPED_FLAG
    assert result.status == "success"
