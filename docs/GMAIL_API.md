# Gmail API Flow (opt-in, privacy-scoped)

Status: **IMPLEMENTED** (`backend/gmail_client.py`, endpoints `/api/gmail/*`, `gmail_sync` feature flag).
This document originated as a design plan kept in a local (uncommitted) planning scratchpad
and is reproduced here for project provenance.
Related existing work: restricted browser-session Gmail scanner
(`backend/playwright_app.py::scan_gmail_for_job_alerts`), opt-in `gmail_sync` flag (default off).

> **Setup quickstart:** `pip install -r requirements.txt` (installs the Google deps), then follow §12.2.
> **Troubleshooting:** see §13 (dependency errors, PKCE `Missing code verifier`, redirect URI).

---

## 1. Objective & privacy principles

Replace/augment the browser-session Gmail scan with the official **Gmail API**, under strict rules:

- **Never enumerate the mailbox.** Every `users().messages().list` call carries a narrow `q`
  and an explicit `maxResults`. (This is the failure mode of the earlier prototype that "read all mails".)
- **Fetch metadata + snippet only** — never `format="full"` message bodies.
- **Read-only** scope; OAuth token **encrypted at rest**; explicit connect/disconnect; opt-in feature flag.
- **Log counts only** (never email content / PII).

## 2. Auth model — BYOK OAuth (loopback)

- User brings their own Google Cloud **OAuth client** (client id + secret), consistent with the
  existing "BYOK" LLM-key philosophy. Avoids maintaining/verifying a shared Google app.
- Flow:
  1. `GET /api/gmail/connect` → backend builds the Google consent URL (state + offline access, `prompt=consent`).
  2. User approves; Google redirects to loopback `http://localhost:8000/api/gmail/oauth2callback?code=...`.
  3. Backend exchanges `code` for tokens; stores the **refresh token** encrypted.
- `POST /api/gmail/disconnect` → revoke token + clear storage.
- **Dependencies (install first):** `pip install -r requirements.txt` → `google-auth`,
  `google-auth-oauthlib`, `google-api-python-client`. The app lazy-imports them, so it starts
  without them, but connect/sync will fail with an actionable error until installed.
- **PKCE:** `authorization_url()` generates a `code_verifier`. Because `connect` and
  `oauth2callback` are **separate HTTP requests**, the verifier is persisted (encrypted) and
  supplied again at token exchange. Otherwise Google returns `invalid_grant: Missing code verifier`.
- New module: `backend/gmail_client.py` (isolates OAuth/API from browser automation).

## 3. Credential & token storage

- New file `data/gmail_settings.json` (chmod `0600`), values encrypted with the existing Fernet helper
  (mirrors `save_llm_settings` in `backend/config.py`):
  - `client_id`, `client_secret` (encrypted), `refresh_token` (encrypted),
    `code_verifier` (encrypted, transient PKCE state)
  - `token_email`, `history_id`, `connected_at`
- Status endpoints return **masked** data only — never the secrets.

## 4. Query & fetch strategy (anti-"read-all-mail" core)

- Single, code-owned query (not user free-form):
  ```
  newer_than:60d (from:greenhouse.io OR from:lever.co OR from:ashbyhq.com
   OR from:myworkdayjobs.com OR from:smartrecruiters.com
   OR subject:interview OR subject:offer OR subject:"next steps"
   OR subject:regret OR subject:"not moving forward")
  ```
- `users().messages().list(userId="me", q=QUERY, maxResults=50, fields="messages/id,nextPageToken")`,
  page-capped (e.g., max 2 pages).
- Per message: `messages().get(userId="me", id=..., format="metadata",
  metadataHeaders=["From","Subject","Date"])` → returns headers + `snippet`. Batch requests.
- Optional small "extra senders" allowlist (still combined into the narrow `q`).

## 5. Incremental sync & dedup

- Persist `history_id`; subsequent runs use `users().history().list(startHistoryId=...)` to fetch only
  changes. Fall back to the `newer_than:` query on first run or on 404 (history expired).
- Dedup by Gmail `messageId` via a small table `ProcessedEmail(id, message_id UNIQUE, processed_at)`
  (auto-migrated in `init_db()` per project conventions). Prevents duplicate events.

## 6. Classification & DB mapping (reuse existing logic)

- `parse_email_classification(subject, snippet, sender)` → `interview | rejection | application_received | job_alert`.
- `job_alert` → `parse_google_alerts_digest` → ingest new jobs (as today).
- `interview | rejection | application_received` → `extract_company_and_role_from_email_header` →
  match existing `Job`, update status, write `ApplicationEvent` (same behavior as
  `sync_email_application_events`, minus the browser).

## 7. Integration points

- New `POST /api/gmail/sync` for explicit sync.
- Also fold into `/api/applications/sync-external` when `"gmail"` is requested:
  - Gmail API connected → use API path.
  - Else if `gmail_sync` enabled and a browser session is available → restricted browser scraper (fallback).
  - Else → return guidance ("Connect Gmail or enable opt-in").
- `GET /api/gmail/status` → connected, email, last sync, last history id, counts.

## 8. UI (SPA: Settings → Gmail)

- `gmail_sync` toggle (already present).
- "Connect Gmail" button → opens auth URL; shows connected email + last sync; "Disconnect".
- Privacy note (metadata + snippet only, narrow query).

## 9. Security / errors / limits

- Handle token expiry (auto-refresh), revoked (mark disconnected), not configured (400 + guidance),
  429/403 quota (exponential backoff; record in `OperationLog`), 404 history (fallback query).
- Scope: `https://www.googleapis.com/auth/gmail.readonly` (metadata scope cannot return snippets).
- Observability: emit counts (messages listed/fetched, events written) — no content.

## 10. Testing

- Mock `googleapiclient.discovery.build`; no live Google calls.
- Unit: query builder, metadata parsing, classification→event mapping, dedup, token encryption.
- Endpoint: connect URL generation, callback token exchange (mocked), sync with mocked service,
  disabled-flag path.

## 11. Rollout

- Ship behind the existing `gmail_sync` flag (default off). Browser scraper remains a fallback.
- Update README / DESIGN docs to describe API-first with browser fallback.

---

## 12. BYOK Google OAuth client — setup guide & in-app help

Because the Gmail API path is BYOK, users must create their own Google Cloud OAuth client.
This guidance must be surfaced **in the UI, in help, and in documentation**.

### 12.1 What "shared Google app" means (and why we are NOT doing it)

A *shared Google app* = the project registers ONE Google Cloud OAuth client and ships its
`client_id`/`client_secret` so all users click "Connect" with no setup. Downsides:

- `gmail.readonly` is a **restricted scope**; an external-use app requires Google **verification +
  CASA security assessment**.
- A distributed local app cannot keep a `client_secret` secret.
- Gmail API **quota is shared** across all users; the project becomes **data controller** for
  everyone's mailbox access.

BYOK gives each user their own client, their own quota, and no project-level verification burden.

### 12.2 Step-by-step instructions to display (verbatim content)

1. Open **Google Cloud Console → APIs & Services → Library**, create/select a project.
2. **Enable the Gmail API** for the project.
3. **OAuth consent screen**: choose *External*; add your own Google account as a **Test user**;
   add scope `https://www.googleapis.com/auth/gmail.readonly`.
4. **APIs & Services → Credentials → Create credentials → OAuth client ID**.
   - Type: **Desktop app** (loopback is allowed), OR **Web application** with redirect URI
     `http://localhost:8000/api/gmail/oauth2callback`.
5. Copy the **Client ID** and **Client secret** into **Settings → Gmail** in this app, then click **Connect**.
6. **Refresh-token expiry note:** while the consent screen is in **Testing**, Google expires refresh
   tokens after **7 days** (you'll need to reconnect). To avoid this, set the app to **In production**
   (acceptable for personal use; you'll see an "unverified app" warning → *Advanced → Continue*).

### 12.3 Where the guidance lives

Implemented locations:
- **UI:** `frontend/index.html` Settings → **Gmail API** section — Client ID/Secret inputs,
  Save Client / Connect Gmail / Disconnect buttons, a collapsible "How to get your Google OAuth
  client ID" panel (the numbered steps above), and a live masked status line. Controller in
  `frontend/app.js` (`loadGmailSection`, `saveGmailCredentials`, `connectGmail`, `disconnectGmail`).
- **Docs:** this file (§12.2) and the README subsection **"Optional: Gmail API (BYOK)"**.
  (No separate `docs/GMAIL_SETUP.md` — this document is the canonical guide.)
- **Error states:** status line shows "Not configured" / "Client saved — not connected" / "Connected as <email>";
  `POST /api/gmail/sync` returns `disabled` (flag off) or `not_connected` with guidance.

---

## 13. Implementation notes & troubleshooting

### Endpoints (implemented)
| Method | Path | Purpose |
| :--- | :--- | :--- |
| GET | `/api/gmail/status` | Masked connection status (`connected`, `email`, `client_id_set`, …). |
| POST | `/api/gmail/credentials` | Store BYOK client ID/secret (encrypted). Empty field = keep current. |
| GET | `/api/gmail/connect` | Returns `{auth_url}`; persists the PKCE `code_verifier`. |
| GET | `/api/gmail/oauth2callback` | Google loopback redirect; exchanges code, stores refresh token. Public path (loopback). |
| POST | `/api/gmail/disconnect` | Clears stored credentials/token. |
| POST | `/api/gmail/sync` | Runs the narrow sync (requires `gmail_sync` flag + connected). |

### Troubleshooting (issues already hit — do not re-debug)
1. **`No module named 'google_auth_oauthlib'`** → dependencies not installed. Run
   `./.venv/bin/pip install -r requirements.txt`. (The client now raises an actionable message.)
2. **`Token exchange failed: (invalid_grant) Missing code verifier`** → PKCE verifier not carried across
   the two requests. Fixed by persisting `code_verifier` between `/api/gmail/connect` and
   `/api/gmail/oauth2callback`; do not call `exchange_code_for_tokens` without it.
3. **`redirect_uri_mismatch`** → the OAuth client's registered redirect must be exactly
   `http://localhost:8000/api/gmail/oauth2callback` (Web app type), or use a **Desktop app** client.
4. **Reconnect every 7 days** → consent screen in **Testing**; set to **In production** (see §12.2 step 6).
5. **`status: disabled`** from `/api/gmail/sync` → enable the **Gmail sync** opt-in flag.

### Decisions (resolved)
1. OAuth client: **BYOK** (user supplies id/secret) — see §12.1.
2. Scope: **`gmail.readonly`** (snippets required for classification), fetched as `metadata` only.
3. Entry point: **both** dedicated `/api/gmail/sync` and integration into `sync-external`.
4. Fallback: **yes** — restricted browser scraper when the API isn't connected.
5. Dedup: **new `ProcessedEmail` table**.

### Status
Implemented and covered by tests: `test_gmail_query_is_narrow`,
`test_gmail_list_message_ids_uses_narrow_query_and_caps_pages`,
`test_gmail_fetch_metadata_is_metadata_format`, `test_run_gmail_sync_classifies_and_dedups`,
`test_gmail_credentials_connect_and_disconnect_flow`, `test_gmail_sync_respects_opt_in_flag`,
`test_gmail_sync_is_opt_in`.
