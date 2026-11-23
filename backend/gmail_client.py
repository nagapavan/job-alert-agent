"""
Gmail API client (BYOK OAuth, opt-in, privacy-scoped).

Design principles (see plan §/docs):
- Never enumerate the mailbox: every messages.list call carries a narrow `q` and `maxResults`.
- Fetch metadata + snippet only (never full message bodies).
- Read-only scope; refresh token stored encrypted via backend.config.
- Google API libraries are imported lazily so the app and tests run without them installed.
"""
import base64
import html
import json
import logging
import math
import os
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("gmail_client")

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
GMAIL_REDIRECT_URI = "http://localhost:8000/api/gmail/oauth2callback"
PAGE_SIZE = 50
MAX_PAGES = 2

# Code-owned narrow queries, split into two passes. Label scope: no labelIds are passed to
# messages().list, so Gmail searches ALL labels (Inbox, Updates, Promotions, Social, plus any
# user labels) except Spam/Trash. The narrow q keeps this privacy-scoped (no mailbox
# enumeration).
#
# The split exists because alert digests arrive 5+ times a day and used to occupy a single
# 100-message window, starving the status/outreach mail behind them.
GMAIL_STATUS_SENDERS = (
    "from:greenhouse.io OR from:lever.co OR from:ashbyhq.com "
    "OR from:myworkdayjobs.com OR from:workday.com OR from:smartrecruiters.com "
    "OR from:workable.com OR from:icims.com OR from:taleo.net "
    # LinkedIn *message* notifications (recruiter outreach), not the alert digests.
    "OR from:hit-reply@linkedin.com OR from:inmail-hit-reply@linkedin.com "
    "OR from:messages-noreply@linkedin.com OR from:messaging-digest-noreply@linkedin.com "
    "OR from:googlealerts-noreply@google.com"
)

GMAIL_STATUS_SUBJECT_CUES = (
    'OR subject:interview OR subject:offer OR subject:"next steps" '
    'OR subject:regret OR subject:"not moving forward" '
    'OR subject:applying OR subject:availability OR subject:scheduling '
    'OR subject:assessment OR subject:"your application" OR subject:"was viewed" '
    'OR subject:opportunity OR subject:application '
    # Employer career-portal digests: "New jobs at <Employer>", "New jobs posted from
    # careers.<employer>.com", "New job(s) match your search", "Jobs for you", "Top jobs".
    'OR subject:"new job" OR subject:"new jobs" OR subject:"job match" '
    'OR subject:"jobs for you" OR subject:"top jobs" OR subject:"match your search" '
    'OR subject:"open positions" OR subject:"career opportunities" OR subject:"job opportunity"'
)

# Alert senders never appear in the status pass, so their volume cannot push status mail out
# of the window.
GMAIL_ALERT_SENDERS = (
    "from:jobalerts-noreply@linkedin.com OR from:jobs-noreply@linkedin.com "
    "OR from:googlealerts-noreply@google.com"
)
GMAIL_STATUS_EXCLUDES = (
    "-from:jobalerts-noreply@linkedin.com -from:jobs-noreply@linkedin.com "
    "-from:googlealerts-noreply@google.com"
)

# Emails that map to an application-pipeline status change.
STATUS_EVENT_TYPES = ("interview", "rejection", "application_received", "offer", "screening")

# LinkedIn alert senders carry a card-per-posting digest rather than Google Alerts text.
LINKEDIN_ALERT_SENDERS = ("jobalerts-noreply@linkedin.com", "jobs-noreply@linkedin.com")

# LinkedIn *message* notifications relay a recruiter, not an employer decision.
LINKEDIN_MESSAGE_SENDERS = (
    "hit-reply@linkedin.com",
    "inmail-hit-reply@linkedin.com",
    "messages-noreply@linkedin.com",
    "messaging-digest-noreply@linkedin.com",
)

# One digest can list 30+ postings. Cap what a single mail may contribute; the rest is left
# for the operator to triage manually rather than flooding the feed.
ALERT_DIGEST_MAX_JOBS = 5

# Gmail messages.list accepts 500 per page; the cap is ours, and is per pass.
PAGE_SIZE_MAX = 500
STATUS_WINDOW_DAYS = int(os.environ.get("GMAIL_STATUS_WINDOW_DAYS", "90"))
ALERT_WINDOW_DAYS = int(os.environ.get("GMAIL_ALERT_WINDOW_DAYS", "7"))
STATUS_MAX_MESSAGES = int(os.environ.get("GMAIL_STATUS_MAX_MESSAGES", "500"))
ALERT_MAX_MESSAGES = int(os.environ.get("GMAIL_ALERT_MAX_MESSAGES", "200"))

# Where the last successful pass left off, so a run does not re-walk the whole window.
SYNC_STATE_FILE = Path(__file__).resolve().parent.parent / "data" / "gmail_sync_state.json"


def is_linkedin_alert_sender(sender: str) -> bool:
    """True for the LinkedIn alert-digest senders (never recruiter message notifications)."""
    lowered = (sender or "").lower()
    return any(addr in lowered for addr in LINKEDIN_ALERT_SENDERS)


def is_linkedin_message_sender(sender: str) -> bool:
    """True for the LinkedIn message-notification senders (recruiter outreach)."""
    lowered = (sender or "").lower()
    return any(addr in lowered for addr in LINKEDIN_MESSAGE_SENDERS)


def is_google_alerts_sender(sender: str) -> bool:
    """True for Google Alerts digests (the only sender whose body the Google parser reads)."""
    return "googlealerts-noreply@google.com" in (sender or "").lower()


def _window_clause(after: Optional[str], window_days: int) -> str:
    """``after:`` from the stored watermark (one day of overlap), else a rolling window.

    Gmail's ``after:`` is day-granular, so the previous day is re-scanned; ``ProcessedEmail``
    dedup makes the overlap free.
    """
    if after:
        try:
            since = datetime.strptime(str(after)[:10], "%Y-%m-%d").date() - timedelta(days=1)
            return f"after:{since:%Y/%m/%d}"
        except ValueError:
            logger.warning(f"Gmail watermark '{after}' is not a date; using a rolling window.")
    return f"newer_than:{max(1, window_days)}d"


def build_status_query(after: Optional[str] = None, window_days: int = STATUS_WINDOW_DAYS) -> str:
    """Narrow query for application status mail and recruiter outreach."""
    window = _window_clause(after, window_days)
    return (
        f"{window} ({GMAIL_STATUS_SENDERS} {GMAIL_STATUS_SUBJECT_CUES}) "
        f"{GMAIL_STATUS_EXCLUDES}"
    )


def build_alert_query(after: Optional[str] = None, window_days: int = ALERT_WINDOW_DAYS) -> str:
    """Narrow query for alert digests only (their own window and cap)."""
    window = _window_clause(after, window_days)
    return f"{window} ({GMAIL_ALERT_SENDERS})"


# Kept for callers that just want "the" query; the sync itself uses both passes.
GMAIL_QUERY = build_status_query()


def load_sync_state() -> Dict[str, Any]:
    """Reads the per-pass watermarks (``{"status": {"after": "YYYY-MM-DD"}, ...}``)."""
    try:
        if SYNC_STATE_FILE.exists():
            data = json.loads(SYNC_STATE_FILE.read_text("utf-8"))
            if isinstance(data, dict):
                return data
    except Exception as exc:  # pragma: no cover - a corrupt state file is not fatal
        logger.warning(f"Could not read Gmail sync state: {exc}")
    return {}


def save_sync_state(state: Dict[str, Any]) -> None:
    """Persists the watermarks; failure only costs a re-scan, never a missed mail."""
    try:
        SYNC_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        SYNC_STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception as exc:  # pragma: no cover - best effort
        logger.warning(f"Could not persist Gmail sync state: {exc}")


def list_message_ids_for(service, query: str, max_messages: int) -> List[str]:
    """Lists up to ``max_messages`` ids, paging at the API maximum."""
    cap = max(1, int(max_messages))
    page = min(PAGE_SIZE_MAX, cap)
    pages = max(1, math.ceil(cap / page))
    return list_message_ids(service, query=query, max_results=page, max_pages=pages)[:cap]


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


def _is_invalid_grant(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "invalid_grant" in msg or "token has been expired or revoked" in msg


def build_service(client_id: str, client_secret: str, refresh_token: str):
    """Builds an authenticated Gmail service (refreshing the access token as needed).

    On ``invalid_grant`` (expired/revoked refresh token) the dead token is cleared so the app
    stops retrying and the UI can prompt a reconnect; the client id/secret are kept.
    """
    _require_google_libs()
    try:
        return _service_from_credentials(_credentials(client_id, client_secret, refresh_token))
    except Exception as e:
        if _is_invalid_grant(e):
            try:
                from backend.config import save_gmail_settings

                # Persist *why* the connection is unusable so the UI can say
                # "Needs reconnect (expired <date>)" instead of "not connected".
                save_gmail_settings(
                    refresh_token="",
                    token_expired_at=datetime.now(timezone.utc).isoformat(),
                    last_error=(
                        "Google rejected the stored refresh token (invalid_grant). "
                        "Reconnect Gmail in Settings."
                    ),
                )
            except Exception:
                pass
            logger.warning("Gmail refresh token is expired/revoked; cleared it (reconnect required).")
            raise RuntimeError(
                "Gmail authorization expired or was revoked (invalid_grant). "
                "Reconnect Gmail in Settings. If this recurs roughly every 7 days, publish the "
                "Google OAuth consent screen to 'In production' — Testing-mode refresh tokens "
                "expire after 7 days."
            ) from e
        raise


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
        fields="id,threadId,snippet,payload/headers",
    ).execute()
    headers = {h.get("name", "").lower(): h.get("value", "")
               for h in (msg.get("payload", {}) or {}).get("headers", [])}
    return {
        "id": msg.get("id", message_id),
        "thread_id": msg.get("threadId", ""),
        "sender": headers.get("from", ""),
        "subject": headers.get("subject", ""),
        "date": headers.get("date", ""),
        "snippet": msg.get("snippet", ""),
    }


def _b64url_decode(data: str) -> str:
    """Decodes a Gmail base64url body part to text (best-effort)."""
    if not data:
        return ""
    try:
        padded = data + "=" * (-len(data) % 4)
        return base64.urlsafe_b64decode(padded.encode("utf-8")).decode("utf-8", "replace")
    except Exception:
        return ""


def _html_to_text(raw_html: str) -> str:
    """Cheap HTML→text: drop script/style, strip tags, unescape entities."""
    if not raw_html:
        return ""
    cleaned = re.sub(r"(?is)<(script|style).*?</\1>", " ", raw_html)
    cleaned = re.sub(r"(?i)<br\s*/?>", "\n", cleaned)
    cleaned = re.sub(r"(?i)</p>", "\n", cleaned)
    cleaned = re.sub(r"(?s)<[^>]+>", " ", cleaned)
    return html.unescape(cleaned)


def _extract_payload_text(payload: dict) -> str:
    """Walks a Gmail message payload, preferring text/plain over text/html."""
    plain_parts: List[str] = []
    html_parts: List[str] = []

    def _walk(part: dict) -> None:
        if not part:
            return
        mime = (part.get("mimeType") or "").lower()
        body = part.get("body", {}) or {}
        data = body.get("data")
        if data:
            decoded = _b64url_decode(data)
            if mime == "text/plain":
                plain_parts.append(decoded)
            elif mime == "text/html":
                html_parts.append(decoded)
        for child in part.get("parts", []) or []:
            _walk(child)

    _walk(payload)
    if plain_parts:
        return "\n".join(p for p in plain_parts if p).strip()
    if html_parts:
        return _html_to_text("\n".join(html_parts)).strip()
    return ""


def fetch_message_body(service, message_id: str, max_chars: int = 6000) -> str:
    """Fetches the full decoded text body of a message (plain preferred, HTML fallback).

    Used only for opportunity-classified mail (issue #82) so a recruiter's JD can be scored
    against the resume. Bounded to ``max_chars`` and never stores raw HTML.
    """
    msg = service.users().messages().get(
        userId="me", id=message_id, format="full", fields="payload",
    ).execute()
    text = _extract_payload_text(msg.get("payload", {}) or {})
    # Collapse the trailing quoted reply history / signature clutter where cheaply possible.
    text = re.split(r"(?i)\n(?:on .{0,120}wrote:|-----original message-----|from:\s)", text)[0]
    return text[:max_chars].strip()


_STATUS_SUBJECT_CUE_RE = re.compile(
    r"\b(?:interview|screening|screen|rejection|rejected|regret|not\s+moving\s+forward|"
    r"offer|application|next\s+steps|schedul\w*|availability|assessment)\b",
    re.I,
)


def _thread_is_linked(db, thread_id: str) -> bool:
    """True when this thread already names a tracked application."""
    if not thread_id:
        return False
    try:
        from backend.database import EmailApplicationLink

        return (
            db.query(EmailApplicationLink)
            .filter(EmailApplicationLink.thread_id == thread_id)
            .first()
            is not None
        )
    except Exception:  # pragma: no cover - never let attribution break the sync
        return False


def run_gmail_sync(db, stats: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """
    Runs the opt-in Gmail API sync in two passes (status/outreach, then alert digests).
    Returns classified application events (interview/rejection/application_received plus
    opportunity) for the caller to map to the DB. Marks each processed message id to
    guarantee dedup.

    ``stats`` (optional) is filled with ``messages_matched`` / ``new_processed`` (and the
    per-pass counts) so the caller can persist an honest operation record instead of
    guessing from the event count.
    """
    from backend.config import get_gmail_settings
    from backend.database import ProcessedEmail
    from backend.playwright_app import classify_email
    from backend.scraper import (
        extract_company_and_role_from_email_header,
        parse_google_alerts_digest,
        parse_linkedin_alert_cards,
        _sender_domain,
    )

    settings = get_gmail_settings()
    if not (settings["client_id"] and settings["client_secret"] and settings["refresh_token"]):
        raise RuntimeError("Gmail is not connected. Add your OAuth client and connect first.")

    service = build_service(settings["client_id"], settings["client_secret"], settings["refresh_token"])

    events: List[Dict[str, Any]] = []
    new_count = 0
    seen_ids: set = set()

    def process(mid: str) -> None:
        nonlocal new_count
        # A message can match both passes; pending rows are not query-visible with
        # autoflush off, so dedupe within the run as well as against ProcessedEmail.
        if mid in seen_ids:
            return
        seen_ids.add(mid)
        if db.query(ProcessedEmail).filter(ProcessedEmail.message_id == mid).first():
            return
        meta = fetch_message_metadata(service, mid)
        cls = classify_email(meta["subject"], meta["snippet"], meta["sender"])
        classification = cls["category"]
        # A LinkedIn *message* notification relays a recruiter: a body-borne "next steps" must
        # not become an interview event. Require the cue in the subject, or an existing link.
        if (
            classification in STATUS_EVENT_TYPES
            and is_linkedin_message_sender(meta["sender"])
            and not _STATUS_SUBJECT_CUE_RE.search(meta["subject"] or "")
            and not _thread_is_linked(db, meta.get("thread_id") or "")
        ):
            classification = "job_alert"
        # A status event only becomes actionable once we can attach an employer. If no company
        # resolves (or it's not a status/job-alert email at all), do NOT mark it processed: a
        # later pattern fix should be able to re-evaluate it (issue #76 poisoned-dedup fix).
        processed = False
        if classification in STATUS_EVENT_TYPES:
            extracted = extract_company_and_role_from_email_header(
                meta["subject"], meta["snippet"], meta["sender"]
            )
            company = extracted.get("company")
            if company:
                events.append({
                    "company": company,
                    "domain": _sender_domain(meta["sender"]),
                    "role": extracted.get("role"),
                    "location": extracted.get("location"),
                    "event_type": classification,
                    "confidence": cls["confidence"],
                    "ats_sender": cls["ats_sender"],
                    "subject": meta["subject"],
                    "snippet": meta["snippet"],
                    "sender": meta["sender"],
                    "date": meta["date"],
                    # Attribution keys: the message id makes the decision a record, and the
                    # thread id lets a follow-up inherit this application.
                    "message_id": meta.get("id") or mid,
                    "thread_id": meta.get("thread_id") or "",
                })
                processed = True
        elif classification == "job_alert":
            # Opportunities from alerts/recruiter digests — tracked even without an application.
            # Recruiter/JD outreach is body-borne, so fetch the full body (bounded) to score it.
            body = fetch_message_body(service, mid)
            opportunities = []
            if is_linkedin_alert_sender(meta["sender"]):
                opportunities = parse_linkedin_alert_cards(
                    body or meta["snippet"], meta["subject"], max_jobs=ALERT_DIGEST_MAX_JOBS
                )
            # The Google Alerts text parser only understands Google's own block format. Running
            # it on an employer digest ("New jobs posted from careers.ey.com") invents rows from
            # prose, so it is gated to the sender it was written for.
            if not opportunities and is_google_alerts_sender(meta["sender"]):
                opportunities = parse_google_alerts_digest(
                    body or meta["snippet"], meta["subject"]
                )
            if not opportunities:
                extracted = extract_company_and_role_from_email_header(
                    meta["subject"], body or meta["snippet"], meta["sender"]
                )
                if extracted.get("company"):
                    opportunities = [{
                        "title": extracted.get("role") or "Open Role",
                        "company": extracted["company"],
                        "employer": extracted.get("employer"),
                        "agency": extracted.get("agency"),
                        "location": extracted.get("location"),
                        "url": "",
                    }]
            for oj in opportunities:
                title = (oj.get("title") or "").strip()
                company = (oj.get("company") or "").strip()
                if not (title and company):
                    continue
                events.append({
                    "company": company,
                    "employer": oj.get("employer"),
                    "agency": oj.get("agency"),
                    "location": oj.get("location"),
                    "role": title,
                    "event_type": "opportunity",
                    "confidence": cls["confidence"],
                    "ats_sender": cls["ats_sender"],
                    "url": oj.get("url", ""),
                    "subject": meta["subject"],
                    "snippet": (body or meta["snippet"])[:4000],
                    "sender": meta["sender"],
                    "date": meta["date"],
                    # Attribution keys: the message id makes the decision a record, and the
                    # thread id lets a follow-up inherit this application.
                    "message_id": meta.get("id") or mid,
                    "thread_id": meta.get("thread_id") or "",
                })
                processed = True
        if processed:
            db.add(ProcessedEmail(message_id=mid))
            new_count += 1

    state = load_sync_state()
    alerts_had_watermark = bool((state.get("alerts") or {}).get("after"))
    matched = 0
    per_pass: Dict[str, int] = {}
    for pass_name, query, cap in (
        ("status", build_status_query((state.get("status") or {}).get("after")), STATUS_MAX_MESSAGES),
        ("alerts", build_alert_query((state.get("alerts") or {}).get("after")), ALERT_MAX_MESSAGES),
    ):
        ids = list_message_ids_for(service, query, cap)
        per_pass[pass_name] = len(ids)
        matched += len(ids)
        for mid in ids:
            process(mid)
        # Only advance the watermark for a pass that ran to completion; a failure above
        # raises before this point, so the next run re-reads the same window.
        now = datetime.now(timezone.utc)
        state[pass_name] = {"after": now.date().isoformat(), "synced_at": now.isoformat()}
        save_sync_state(state)

    db.flush()
    if stats is not None:
        stats["messages_matched"] = matched
        stats["new_processed"] = new_count
        stats["status_messages_matched"] = per_pass.get("status", 0)
        stats["alert_messages_matched"] = per_pass.get("alerts", 0)
        # A first run that matches no alert mail means the query/scope is wrong, not that
        # the mailbox is quiet: the caller uses this to decide on the browser fallback.
        stats["alerts_had_watermark"] = alerts_had_watermark
    logger.info(
        f"Gmail API sync: {matched} messages matched "
        f"({per_pass.get('status', 0)} status/outreach, {per_pass.get('alerts', 0)} alerts), "
        f"{new_count} new processed, {len(events)} events."
    )
    return events


def get_status() -> Dict[str, Any]:
    """Returns masked Gmail connection status (never secrets)."""
    from backend.config import get_gmail_settings, is_gmail_configured

    s = get_gmail_settings()
    secret = s["client_secret"] or ""
    # Common paste mistake: the client ID (or a *.apps.googleusercontent.com URL) stored as the
    # secret. Surfacing it turns a later opaque `invalid_client` into an actionable message.
    secret_looks_invalid = bool(secret) and (
        secret == s["client_id"] or secret.endswith(".apps.googleusercontent.com")
    )
    return {
        "client_id": s["client_id"] or "",
        "client_id_set": bool(s["client_id"]),
        "client_secret_set": bool(secret),
        "secret_looks_invalid": secret_looks_invalid,
        "connected": bool(s["client_id"] and s["client_secret"] and s["refresh_token"]),
        "configured": is_gmail_configured(),
        "email": s["token_email"] or "",
        "connected_at": s["connected_at"] or "",
        "token_expired_at": s["token_expired_at"] or "",
        "last_error": s["last_error"] or "",
        # True when credentials exist but Google rejected the token: the operator must
        # reconnect. Distinguishes "expired" from "never configured".
        "needs_reconnect": bool(
            s["client_id"] and s["client_secret"] and not s["refresh_token"]
            and s["token_expired_at"]
        ),
        "scopes": GMAIL_SCOPES,
        "redirect_uri": GMAIL_REDIRECT_URI,
    }
