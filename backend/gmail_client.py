"""
Gmail API client (BYOK OAuth, opt-in, privacy-scoped).

Design principles (see plan §/docs):
- Never enumerate the mailbox: every messages.list call carries a narrow `q` and `maxResults`.
- Fetch metadata + snippet only (never full message bodies).
- Read-only scope; refresh token stored encrypted via backend.config.
- Google API libraries are imported lazily so the app and tests run without them installed.
"""
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger("gmail_client")

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
GMAIL_REDIRECT_URI = "http://localhost:8000/api/gmail/oauth2callback"
PAGE_SIZE = 50
MAX_PAGES = 2

# Code-owned narrow query: job-board senders + decision/interview subjects only.
GMAIL_QUERY = (
    'newer_than:60d (from:greenhouse.io OR from:lever.co OR from:ashbyhq.com '
    'OR from:myworkdayjobs.com OR from:smartrecruiters.com OR from:linkedin.com '
    'OR subject:interview OR subject:offer OR subject:"next steps" '
    'OR subject:regret OR subject:"not moving forward" '
    'OR subject:"job alert" OR subject:opportunity)'
)

# Emails that map to an application-pipeline status change.
STATUS_EVENT_TYPES = ("interview", "rejection", "application_received", "offer", "screening")


def _require_google_libs():
    """Raises an actionable error if the optional Google dependencies are missing."""
    try:
        import google_auth_oauthlib  # noqa: F401
        import googleapiclient  # noqa: F401
        import google.oauth2  # noqa: F401
    except ImportError as e:
        raise RuntimeError(
            "Gmail dependencies are not installed. Run: "
            "pip install -r requirements.txt "
            "(google-auth, google-auth-oauthlib, google-api-python-client)."
        ) from e


def _flow(client_id: str, client_secret: str, redirect_uri: str):
    _require_google_libs()
    from google_auth_oauthlib.flow import Flow

    client_config = {
        "web": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [redirect_uri],
        }
    }
    return Flow.from_client_config(client_config, scopes=GMAIL_SCOPES, redirect_uri=redirect_uri)


def build_auth_url(client_id: str, client_secret: str, redirect_uri: str = GMAIL_REDIRECT_URI,
                   state: str = "job-alert-agent") -> Dict[str, str]:
    """
    Builds the Google consent URL for the loopback OAuth flow.
    Returns the URL plus the PKCE code_verifier, which MUST be persisted and supplied
    again at token exchange (the flow spans two separate HTTP requests).
    """
    flow = _flow(client_id, client_secret, redirect_uri)
    url, _ = flow.authorization_url(
        access_type="offline", include_granted_scopes="true", prompt="consent", state=state
    )
    return {"auth_url": url, "code_verifier": getattr(flow, "code_verifier", "") or ""}


def exchange_code_for_tokens(code: str, client_id: str, client_secret: str,
                             redirect_uri: str = GMAIL_REDIRECT_URI,
                             code_verifier: str = "") -> Dict[str, str]:
    """Exchanges the authorization code for tokens and returns the refresh token + email."""
    flow = _flow(client_id, client_secret, redirect_uri)
    fetch_kwargs: Dict[str, str] = {"code": code}
    if code_verifier:
        fetch_kwargs["code_verifier"] = code_verifier
    flow.fetch_token(**fetch_kwargs)
    creds = flow.credentials
    email = ""
    try:
        service = _service_from_credentials(creds)
        email = get_profile_email(service)
    except Exception:
        pass
    return {"refresh_token": creds.refresh_token or "", "email": email}


def _credentials(client_id: str, client_secret: str, refresh_token: str):
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request

    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        client_id=client_id,
        client_secret=client_secret,
        token_uri="https://oauth2.googleapis.com/token",
        scopes=GMAIL_SCOPES,
    )
    if not creds.valid:
        creds.refresh(Request())
    return creds


def _service_from_credentials(creds):
    from googleapiclient.discovery import build

    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def build_service(client_id: str, client_secret: str, refresh_token: str):
    """Builds an authenticated Gmail service (refreshing the access token as needed)."""
    _require_google_libs()
    return _service_from_credentials(_credentials(client_id, client_secret, refresh_token))


def get_profile_email(service) -> str:
    profile = service.users().getProfile(userId="me").execute()
    return profile.get("emailAddress", "")


def list_message_ids(service, query: str = GMAIL_QUERY, max_results: int = PAGE_SIZE,
                     max_pages: int = MAX_PAGES) -> List[str]:
    """Lists message ids for the narrow query only, bounded by pages."""
    ids: List[str] = []
    token: Optional[str] = None
    for _ in range(max(1, max_pages)):
        resp = service.users().messages().list(
            userId="me", q=query, maxResults=max_results, pageToken=token,
            fields="messages/id,nextPageToken",
        ).execute()
        ids.extend(m.get("id") for m in (resp.get("messages") or []) if m.get("id"))
        token = resp.get("nextPageToken")
        if not token:
            break
    return ids


def fetch_message_metadata(service, message_id: str) -> Dict[str, str]:
    """Fetches only headers + snippet (metadata format) for a message."""
    msg = service.users().messages().get(
        userId="me", id=message_id, format="metadata",
        metadataHeaders=["From", "Subject", "Date"],
        fields="id,snippet,payload/headers",
    ).execute()
    headers = {h.get("name", "").lower(): h.get("value", "")
               for h in (msg.get("payload", {}) or {}).get("headers", [])}
    return {
        "id": msg.get("id", message_id),
        "sender": headers.get("from", ""),
        "subject": headers.get("subject", ""),
        "date": headers.get("date", ""),
        "snippet": msg.get("snippet", ""),
    }


def run_gmail_sync(db) -> List[Dict[str, Any]]:
    """
    Runs the opt-in Gmail API sync. Returns classified application events
    (interview/rejection/application_received) for the caller to map to the DB.
    Marks each processed message id to guarantee dedup.
    """
    from backend.config import get_gmail_settings
    from backend.database import ProcessedEmail
    from backend.playwright_app import classify_email
    from backend.scraper import (
        extract_company_and_role_from_email_header,
        parse_google_alerts_digest,
    )

    settings = get_gmail_settings()
    if not (settings["client_id"] and settings["client_secret"] and settings["refresh_token"]):
        raise RuntimeError("Gmail is not connected. Add your OAuth client and connect first.")

    service = build_service(settings["client_id"], settings["client_secret"], settings["refresh_token"])
    message_ids = list_message_ids(service)

    events: List[Dict[str, Any]] = []
    new_count = 0
    for mid in message_ids:
        if db.query(ProcessedEmail).filter(ProcessedEmail.message_id == mid).first():
            continue
        meta = fetch_message_metadata(service, mid)
        cls = classify_email(meta["subject"], meta["snippet"], meta["sender"])
        classification = cls["category"]
        if classification in STATUS_EVENT_TYPES:
            extracted = extract_company_and_role_from_email_header(
                meta["subject"], meta["snippet"], meta["sender"]
            )
            company = extracted.get("company")
            if company:
                events.append({
                    "company": company,
                    "role": extracted.get("role") or "Software Engineer",
                    "event_type": classification,
                    "confidence": cls["confidence"],
                    "ats_sender": cls["ats_sender"],
                    "subject": meta["subject"],
                    "snippet": meta["snippet"],
                    "sender": meta["sender"],
                    "date": meta["date"],
                })
        elif classification == "job_alert":
            # Opportunities from alerts/recruiter digests — tracked even without an application.
            opportunities = parse_google_alerts_digest(meta["snippet"], meta["subject"])
            if not opportunities:
                extracted = extract_company_and_role_from_email_header(
                    meta["subject"], meta["snippet"], meta["sender"]
                )
                if extracted.get("company"):
                    opportunities = [{
                        "title": extracted.get("role") or "Open Role",
                        "company": extracted["company"],
                        "url": "",
                    }]
            for oj in opportunities:
                title = (oj.get("title") or "").strip()
                company = (oj.get("company") or "").strip()
                if not (title and company):
                    continue
                events.append({
                    "company": company,
                    "role": title,
                    "event_type": "opportunity",
                    "confidence": cls["confidence"],
                    "ats_sender": cls["ats_sender"],
                    "url": oj.get("url", ""),
                    "subject": meta["subject"],
                    "snippet": meta["snippet"],
                    "sender": meta["sender"],
                    "date": meta["date"],
                })
        db.add(ProcessedEmail(message_id=mid))
        new_count += 1

    db.flush()
    logger.info(f"Gmail API sync: {len(message_ids)} messages matched, {new_count} new processed, {len(events)} events.")
    return events


def get_status() -> Dict[str, Any]:
    """Returns masked Gmail connection status (never secrets)."""
    from backend.config import get_gmail_settings, is_gmail_configured

    s = get_gmail_settings()
    return {
        "client_id_set": bool(s["client_id"]),
        "client_secret_set": bool(s["client_secret"]),
        "connected": bool(s["client_id"] and s["client_secret"] and s["refresh_token"]),
        "configured": is_gmail_configured(),
        "email": s["token_email"] or "",
        "connected_at": s["connected_at"] or "",
        "scopes": GMAIL_SCOPES,
        "redirect_uri": GMAIL_REDIRECT_URI,
    }
