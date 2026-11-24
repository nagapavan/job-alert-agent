"""Gmail OAuth `state` is generated, persisted and single-use (finding 23).

`/api/gmail/oauth2callback` is public (it is the redirect target), so `state` is the only thing
that ties a redirect to a Connect the operator actually started.
"""

from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.main import app

client = TestClient(app)


def _save_credentials():
    res = client.post(
        "/api/gmail/credentials",
        json={"client_id": "cid", "client_secret": "csecret"},
    )
    assert res.status_code == 200


def test_connect_persists_the_state_it_put_in_the_url():
    from backend.config import get_gmail_settings

    _save_credentials()
    with patch(
        "backend.gmail_client.build_auth_url",
        return_value={
            "auth_url": "https://accounts.google.com/o/oauth2/auth?state=nonce123",
            "code_verifier": "verifier123",
            "state": "nonce123",
        },
    ):
        res = client.get("/api/gmail/connect")

    assert res.status_code == 200
    settings = get_gmail_settings()
    assert settings["oauth_state"] == "nonce123"
    assert settings["code_verifier"] == "verifier123"


def test_build_auth_url_generates_a_random_state_by_default():
    from backend.gmail_client import build_auth_url

    with patch("backend.gmail_client._flow") as flow_factory:
        flow = flow_factory.return_value
        flow.authorization_url.return_value = ("https://accounts.google.com/x", "state")
        flow.code_verifier = "v"
        first = build_auth_url("cid", "csecret")
        second = build_auth_url("cid", "csecret")

    assert first["state"] and second["state"]
    assert first["state"] != second["state"]
    assert flow.authorization_url.call_args.kwargs["state"] == second["state"]


def test_callback_rejects_a_missing_or_mismatched_state():
    from backend.config import save_gmail_settings

    _save_credentials()
    save_gmail_settings(oauth_state="expected-nonce", code_verifier="v")

    with patch("backend.gmail_client.exchange_code_for_tokens") as exchange:
        missing = client.get("/api/gmail/oauth2callback", params={"code": "abc"})
        wrong = client.get(
            "/api/gmail/oauth2callback", params={"code": "abc", "state": "other"}
        )

    assert missing.status_code == 400
    assert "state" in missing.text.lower()
    assert wrong.status_code == 400
    exchange.assert_not_called()


def test_callback_consumes_the_state_so_a_replay_is_refused():
    from backend.config import get_gmail_settings, save_gmail_settings

    _save_credentials()
    save_gmail_settings(oauth_state="nonce", code_verifier="v")

    with patch(
        "backend.gmail_client.exchange_code_for_tokens",
        return_value={"refresh_token": "rt", "email": "someone@example.test"},
    ) as exchange:
        first = client.get(
            "/api/gmail/oauth2callback", params={"code": "abc", "state": "nonce"}
        )
        replay = client.get(
            "/api/gmail/oauth2callback", params={"code": "abc", "state": "nonce"}
        )

    assert first.status_code == 200
    assert exchange.call_count == 1
    assert replay.status_code == 400
    assert get_gmail_settings()["oauth_state"] == ""
