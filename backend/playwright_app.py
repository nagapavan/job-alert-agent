import logging
import os
import re
import threading
import urllib.parse
from typing import List, Dict, Optional, Any

from backend.config import CHROME_PROFILE_DIR
from backend.parser import split_candidate_name

logger = logging.getLogger("playwright_app")

# Ensure profile directory exists with safe permissions
CHROME_PROFILE_DIR.mkdir(parents=True, exist_ok=True)

# -------------------------------------------------------------
# Persistent Context Launcher
# -------------------------------------------------------------

class BrowserProfileBusy(RuntimeError):
    """Raised when the persistent Chrome profile is held by a live browser session."""


# Serializes the profile-in-use check + launch so concurrent callers can't race.
_PROFILE_LAUNCH_LOCK = threading.RLock()


def _lock_owner_pid(lock_path: str) -> Optional[int]:
    """
    Chromium's SingletonLock is a symlink whose target is '<hostname>-<pid>'.
    Returns the owning PID, or None when it cannot be determined.
    """
    try:
        if not (os.path.islink(lock_path) or os.path.exists(lock_path)):
            return None
        target = os.readlink(lock_path) if os.path.islink(lock_path) else ""
        return int(str(target).rsplit("-", 1)[-1])
    except Exception:
        return None


def _pid_alive(pid: Optional[int]) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False


def is_profile_in_use() -> bool:
    """True when a live browser process currently owns the persistent profile lock."""
    lock_path = os.path.join(str(CHROME_PROFILE_DIR), "SingletonLock")
    return _pid_alive(_lock_owner_pid(lock_path))


def clean_stale_profile_locks() -> bool:
    """
    Removes leftover Chromium Singleton* lock files ONLY when no live process owns them.
    Returns True if cleanup was safe/performed, False if a live browser holds the profile.
    Never deletes a lock held by a running browser (which would risk profile corruption).
    """
    if is_profile_in_use():
        return False
    for lock_name in ["SingletonLock", "SingletonSocket", "SingletonCookie"]:
        lock_path = os.path.join(str(CHROME_PROFILE_DIR), lock_name)
        if os.path.islink(lock_path) or os.path.exists(lock_path):
            try:
                os.unlink(lock_path)
            except Exception:
                pass
    return True

def get_persistent_browser_context(playwright_instance, headless: bool = False, **kwargs):
    """
    Initializes a persistent Chromium browser context pointing to CHROME_PROFILE_DIR.
    Ensures user session cookies, logins, and preferences persist safely.

    Concurrency safety: the profile is single-owner. If a live browser session already
    holds it, raises BrowserProfileBusy instead of deleting live locks or falling back to
    an isolated (logged-out) context, which would silently defeat session persistence.
    """
    args = [
        "--no-default-browser-check",
        "--start-maximized",
        "--window-size=1680,1050"
    ]
    with _PROFILE_LAUNCH_LOCK:
        if is_profile_in_use():
            raise BrowserProfileBusy(
                "Persistent Chrome profile is in use by another live session. "
                "Close the other browser window or wait for the active task to finish."
            )
        clean_stale_profile_locks()
        try:
            return playwright_instance.chromium.launch_persistent_context(
                user_data_dir=str(CHROME_PROFILE_DIR),
                headless=headless,
                no_viewport=True,
                args=args,
                **kwargs
            )
        except Exception as e:
            err_msg = str(e)
            if "SingletonLock" in err_msg or "existing browser session" in err_msg or "ProcessSingleton" in err_msg:
                # A live session may have acquired the lock between our check and launch.
                if is_profile_in_use():
                    raise BrowserProfileBusy(
                        "Persistent Chrome profile became locked by another session."
                    )
                clean_stale_profile_locks()
                return playwright_instance.chromium.launch_persistent_context(
                    user_data_dir=str(CHROME_PROFILE_DIR),
                    headless=headless,
                    no_viewport=True,
                    args=args,
                    **kwargs
                )
            raise

# -------------------------------------------------------------
# Gmail Job Alerts & Response Email Scanner
# -------------------------------------------------------------

# Known ATS / job-board / recruiter sender domains. A decision email coming from one of
# these is a strong hiring signal (raises classification confidence).
EMAIL_ATS_SENDER_DOMAINS = (
    "greenhouse.io", "greenhouse-mail.io", "lever.co", "ashbyhq.com", "myworkdayjobs.com",
    "workday.com", "smartrecruiters.com", "icims.com", "linkedin.com", "jobs.lever.co",
)

EMAIL_REJECTION_PHRASES = [
    "not moving forward", "other candidates", "unfortunately",
    "decided to pursue", "regret to inform", "not selected",
    "won't be moving forward", "application status", "position has been filled",
    "will not be moving forward"
]

EMAIL_OFFER_PHRASES = [
    "offer letter", "job offer", "offer of employment", "pleased to offer",
    "delighted to offer", "would like to offer", "we are offering", "verbal offer",
    "compensation package", "offer for the position", "extend an offer",
]

EMAIL_INTERVIEW_PHRASES = [
    "interview", "schedule a time", "phone screen", "next steps",
    "invitation to interview", "take-home assessment", "technical screen",
    "recruiter screen", "hiring manager screen", "assessment invitation",
    "schedule your interview", "interview availability", "google meet interview",
    "virtual interview", "video interview", "phone screening", "technical phone screening",
    "virtual round", "self-schedule", "invitation to self-schedule", "interview confirmation",
    "technical interview", "loop interview", "call/invites"
]

EMAIL_SCREENING_PHRASES = [
    "initial screening", "screening call", "screening round", "screening process",
    "recruiter screening", "intro call", "introduction call", "initial call",
]

EMAIL_ALERT_PHRASES = [
    "google alert", "job alert", "new jobs", "open roles", "matching jobs",
    "recommended for you", "positions open", "careers at", "openings at",
    "job recommendations", "new opportunities", "jobs matching", "jobs/alerts",
    "sees you as a top applicant", "role at", "opportunities at", "opportunity at"
]

EMAIL_APPLICATION_PHRASES = [
    "thank you for applying", "application received", "we received your application",
    "application submitted", "jobs/applications"
]

# Ordered by specificity/priority; the first category with a matched phrase wins.
_EMAIL_CLASSIFICATION_ORDER = (
    ("rejection", EMAIL_REJECTION_PHRASES),
    ("offer", EMAIL_OFFER_PHRASES),
    ("interview", EMAIL_INTERVIEW_PHRASES),
    ("screening", EMAIL_SCREENING_PHRASES),
    ("job_alert", EMAIL_ALERT_PHRASES),
    ("application_received", EMAIL_APPLICATION_PHRASES),
)


def classify_email(subject: str, snippet: str = "", sender: str = "") -> Dict[str, Any]:
    """
    Heuristic email classifier with a confidence score (0.0–1.0).

    Confidence starts at a base value, grows with the number of matched phrases, and is
    boosted when the sender is a known ATS/recruiter domain. Callers should gate automatic
    DB writes on confidence so a single weak keyword cannot mutate application state.
    """
    text = f"{sender} {subject} {snippet}".lower()
    sender_lower = (sender or "").lower()
    ats_sender = any(domain in sender_lower for domain in EMAIL_ATS_SENDER_DOMAINS)

    for category, phrases in _EMAIL_CLASSIFICATION_ORDER:
        matched = [p for p in phrases if p in text]
        if matched:
            confidence = min(0.95, 0.45 + 0.2 * min(len(matched), 2))
            if ats_sender:
                confidence = min(0.98, confidence + 0.25)
            return {
                "category": category,
                "confidence": round(confidence, 2),
                "matched": matched[:5],
                "ats_sender": ats_sender,
            }

    return {"category": "general", "confidence": 0.0, "matched": [], "ats_sender": ats_sender}


def parse_email_classification(subject: str, snippet: str, sender: str = "") -> str:
    """
    Classifies email contents into 'rejection', 'offer', 'interview', 'screening',
    'job_alert', 'application_received', or 'general'.
    """
    return classify_email(subject, snippet, sender)["category"]


def scan_gmail_for_job_alerts(playwright_instance, headless: bool = True) -> List[Dict[str, Any]]:
    """
    Navigates to Gmail using the persistent Chrome session and runs a single TARGETED search
    (job-board senders + interview/offer/rejection subjects) to catch job alerts, interview
    calls, and status updates. Deliberately does NOT scan the full inbox or category tabs.
    """
    emails = []
    seen_keys = set()

    try:
        context = get_persistent_browser_context(playwright_instance, headless=headless)
        page = context.new_page()

        # Step 1: Navigate to Gmail Inbox and wait for SPA to load
        page.goto("https://mail.google.com/mail/u/0/#inbox", timeout=45000, wait_until="domcontentloaded")

        if "accounts.google.com" in page.url or "signin" in page.url.lower():
            logger.warning("Gmail scan: User is not logged in to Google in persistent Chrome profile. Launch interactive browser session to sign in.")
            context.close()
            return []

        # Wait for search box or email row table to be interactive
        try:
            page.wait_for_selector("input[aria-label='Search mail'], input[name='q'], tr.zA", timeout=15000)
        except Exception:
            pass

        # Helper to parse visible rows on current page view
        def parse_visible_rows():
            found_rows = page.query_selector_all("tr.zA")
            for row in found_rows[:40]:
                try:
                    sender_el = row.query_selector(".yP, .zF") or row.query_selector("span[email], span.bA4 span, span[name], span[email]")
                    subject_el = row.query_selector(".bog") or row.query_selector(".bqe, span[data-thread-id], span.bqe, span.bog")
                    snippet_el = row.query_selector(".y2") or row.query_selector("span.y2, .mG")
                    date_el = row.query_selector(".xW") or row.query_selector("td.xW, span.b3, .xW span")
                    label_els = row.query_selector_all(".ar, .av, .at, div[role='gridcell'] .ar")

                    sender = sender_el.inner_text().strip() if sender_el else "Unknown Sender"
                    subject = subject_el.inner_text().strip() if subject_el else "No Subject"
                    snippet = snippet_el.inner_text().strip() if snippet_el else ""
                    date_str = date_el.inner_text().strip() if date_el else ""
                    
                    labels = [l.inner_text().strip() for l in label_els if l.inner_text().strip()]
                    if labels and not any(f"[{lbl}]" in subject for lbl in labels):
                        subject = f"{' '.join(['[' + lbl + ']' for lbl in labels])} {subject}"

                    dedup_key = f"{sender}:{subject}:{date_str}"
                    if dedup_key in seen_keys:
                        continue
                    seen_keys.add(dedup_key)

                    category = parse_email_classification(subject, snippet, sender)
                    extracted_jobs = []
                    if category == "job_alert" or "alert" in sender.lower() or "alert" in subject.lower() or "[jobs/alerts]" in subject.lower():
                        from backend.scraper import parse_google_alerts_digest
                        extracted_jobs = parse_google_alerts_digest(snippet, subject)

                    emails.append({
                        "sender": sender,
                        "subject": subject,
                        "snippet": snippet,
                        "date": date_str,
                        "category": category,
                        "extracted_jobs": extracted_jobs
                    })
                except Exception as row_err:
                    logger.debug(f"Error parsing email row: {row_err}")

        # Helper to parse visible rows on current page view and paginate through older pages
        def scan_view_with_pagination(max_pages: int = 1):
            for p_idx in range(max_pages):
                parse_visible_rows()
                if p_idx < max_pages - 1:
                    next_btn = page.query_selector("div[aria-label='Older'], div[data-tooltip='Older'], button[aria-label='Older'], span[aria-label='Older'], div[role='button'][aria-label='Older']")
                    if next_btn and next_btn.get_attribute("aria-disabled") != "true" and next_btn.is_visible():
                        try:
                            next_btn.click()
                            page.wait_for_timeout(2000)
                            try:
                                page.wait_for_selector("tr.zA", timeout=6000)
                            except Exception:
                                pass
                        except Exception:
                            break
                    else:
                        break

        # Privacy: run ONLY a tight, targeted search. We deliberately do NOT scan the whole
        # inbox or the Category "Updates" tabs, which would read unrelated personal mail.
        search_query = (
            "newer_than:60d "
            "(from:greenhouse.io OR from:lever.co OR from:ashbyhq.com OR from:myworkdayjobs.com "
            "OR from:smartrecruiters.com OR subject:interview OR subject:offer OR subject:\"next steps\" "
            "OR subject:regret OR subject:\"not moving forward\" OR subject:\"application\" OR subject:\"job alert\")"
        )
        try:
            search_input = page.query_selector("input[aria-label='Search mail'], input[name='q']")
            if search_input:
                search_input.click()
                search_input.fill(search_query)
                search_input.press("Enter")
                page.wait_for_timeout(2500)
                try:
                    page.wait_for_selector("tr.zA", timeout=8000)
                except Exception:
                    pass
                scan_view_with_pagination(max_pages=1)
        except Exception as search_err:
            logger.debug(f"Gmail targeted search notice: {search_err}")

        context.close()
    except Exception as e:
        logger.warning(f"Gmail scan failed (may require initial login in browser): {e}")

    return emails

# -------------------------------------------------------------
# LinkedIn Talent Partner DOM Search
# -------------------------------------------------------------

def search_linkedin_talent_partners(
    playwright_instance, 
    company_name: str, 
    role_title: str = "", 
    headless: bool = True
) -> List[Dict[str, str]]:
    """
    Performs a LinkedIn people search for recruiters/talent partners within the active session.
    """
    if not company_name or not company_name.strip():
        return []

    contacts = []
    query = f"{company_name} recruiter {role_title}".strip()
    encoded = urllib.parse.quote(query)
    search_url = f"https://www.linkedin.com/search/results/people/?keywords={encoded}&origin=SWITCH_SEARCH_VERTICAL"

    try:
        context = get_persistent_browser_context(playwright_instance, headless=headless)
        page = context.new_page()
        page.goto(search_url, timeout=30000, wait_until="domcontentloaded")

        # Wait for search results container
        page.wait_for_selector(".reusable-search__result-container, .search-results-container", timeout=8000)

        cards = page.query_selector_all(".reusable-search__result-container")
        for card in cards[:6]:
            try:
                name_el = card.query_selector(".entity-result__title-text a, .app-aware-link")
                headline_el = card.query_selector(".entity-result__primary-subtitle")
                link_el = card.query_selector("a.app-aware-link")

                name = name_el.inner_text().split("\n")[0].strip() if name_el else ""
                headline = headline_el.inner_text().strip() if headline_el else "Recruiter / Talent Partner"
                link = link_el.get_attribute("href") if link_el else ""

                if name and link:
                    clean_link = link.split("?")[0]
                    contacts.append({
                        "name": name,
                        "headline": headline,
                        "profile_url": clean_link
                    })
            except Exception as e:
                logger.debug(f"Error parsing LinkedIn card: {e}")

        context.close()
    except Exception as e:
        logger.warning(f"LinkedIn DOM search failed: {e}")

    return contacts

# -------------------------------------------------------------
# Targeted LinkedIn Hiring Post & Manager Discovery
# -------------------------------------------------------------

# -------------------------------------------------------------
# LinkedIn Recommendations & Saved Jobs Sync with Auto-Cleaner
# -------------------------------------------------------------

def scan_linkedin_job_recommendations(
    playwright_instance,
    headless: bool = True,
    limit: int = 15,
    page=None
) -> List[Dict[str, Any]]:
    """
    Scans LinkedIn active recommended jobs and alert collections in the persistent session.
    Navigates to the personalized recommended collections feed.
    """
    jobs = []
    own_context = False
    context = None
    try:
        if page is None:
            context = get_persistent_browser_context(playwright_instance, headless=headless)
            page = context.pages[0] if (hasattr(context, "pages") and isinstance(context.pages, list) and len(context.pages) > 0) else context.new_page()
            own_context = True

        page.goto("https://www.linkedin.com/jobs/collections/recommended/", timeout=35000, wait_until="domcontentloaded")
        page.wait_for_timeout(3500)

        cards = page.query_selector_all(
            "li.artdeco-list__item, li[data-occludable-job-id], div.job-card-container, div[data-view-name*='job'], li:has(a[href*='/jobs/view/'])"
        )
        for card in cards[:limit * 2]:
            try:
                title_el = card.query_selector(".job-card-list__title, a.job-card-container__link, .artdeco-entity-lockup__title a") or card.query_selector("a[href*='/jobs/view/'], strong, a")
                if not title_el:
                    continue

                raw_url = title_el.get_attribute("href") or ""
                clean_url = urllib.parse.urljoin("https://www.linkedin.com", raw_url).split("?")[0] if raw_url else ""

                # Extract company, location
                comp_el = card.query_selector(".job-card-container__company-name, .artdeco-entity-lockup__subtitle") or card.query_selector(".job-card-container__primary-description")
                loc_el = card.query_selector(".job-card-container__metadata-item, .artdeco-entity-lockup__caption")

                text_lines = [l.strip() for l in card.inner_text().split("\n") if l.strip() and not l.strip().startswith("Easy Apply") and not l.strip().startswith("Promoted") and not l.strip().startswith("Actively recruiting") and "verification" not in l.lower()]

                title = title_el.inner_text().strip() if title_el else (text_lines[0] if text_lines else "Untitled Role")
                company = comp_el.inner_text().strip() if comp_el else (text_lines[1] if len(text_lines) > 1 else "LinkedIn Network")
                location = loc_el.inner_text().strip() if loc_el else (text_lines[2] if len(text_lines) > 2 else "Remote / Various")

                # Clean any trailing badge text or nested duplicate lines
                title = title.split("\n")[0].split(" with verification")[0].strip()
                company = company.split("\n")[0].split(" · ")[0].strip()
                location = location.split("\n")[0].strip()

                if title and clean_url and not any(j["url"] == clean_url for j in jobs):
                    jobs.append({
                        "title": title,
                        "company": company,
                        "location": location,
                        "url": clean_url,
                        "description": f"LinkedIn Recommended opening for {title} at {company} in {location}.",
                        "source": "LinkedIn Recommendations",
                        "source_type": "LinkedIn"
                    })

                if len(jobs) >= limit:
                    break
            except Exception as e:
                logger.debug(f"Error parsing recommended job card: {e}")

    except Exception as e:
        logger.warning(f"LinkedIn recommendations scan failed (ensure user is logged in): {e}")
    finally:
        if own_context and context:
            try:
                context.close()
            except Exception:
                pass

    return jobs

def scan_linkedin_job_alerts(
    playwright_instance,
    headless: bool = True,
    max_alerts: int = 5,
    max_jobs_per_alert: int = 15,
    target_roles: Optional[List[str]] = None,
    target_location: Optional[str] = None,
    page=None
) -> List[Dict[str, Any]]:
    """
    Scans LinkedIn Subscribed Job Alerts directly from the user's active session.
    1. Navigates to LinkedIn Job Alerts manager (https://www.linkedin.com/my-items/job-alerts/ or https://www.linkedin.com/jobs/tracker/alerts/).
    2. Collects subscribed alert search URLs. If none found, generates live alert searches for candidate's target roles.
    3. Crawls each alert feed (filtered to newly posted positions: f_TPR=r604800) and extracts live opportunities.
    """
    alert_jobs = []
    own_context = False
    context = None
    try:
        if page is None:
            context = get_persistent_browser_context(playwright_instance, headless=headless)
            page = context.pages[0] if (hasattr(context, "pages") and isinstance(context.pages, list) and len(context.pages) > 0) else context.new_page()
            own_context = True

        # 1. Discover configured alert search URLs
        alert_urls = []
        try:
            page.goto("https://www.linkedin.com/my-items/job-alerts/", timeout=35000, wait_until="domcontentloaded")
            page.wait_for_timeout(3000)

            alert_links = page.query_selector_all("a[href*='/jobs/search/']")
            for link in alert_links:
                href = link.get_attribute("href") or ""
                if href and "/jobs/search/" in href:
                    clean = urllib.parse.urljoin("https://www.linkedin.com", href)
                    if clean not in alert_urls:
                        alert_urls.append(clean)
        except Exception as e:
            logger.debug(f"Could not read direct job alerts page: {e}")

        # Fallback: If no alert URLs found, generate search alert URLs from target_roles
        if not alert_urls and target_roles:
            loc_param = urllib.parse.quote(target_location or "India")
            for role in target_roles[:max_alerts]:
                role_param = urllib.parse.quote(role)
                alert_urls.append(f"https://www.linkedin.com/jobs/search/?keywords={role_param}&location={loc_param}&f_TPR=r604800&sortBy=DD")

        # Default fallback if still empty
        if not alert_urls:
            alert_urls.append("https://www.linkedin.com/jobs/collections/recommended/")

        # 2. Iterate through alert feeds and extract live postings
        for alert_url in alert_urls[:max_alerts]:
            try:
                target_url = alert_url
                if "f_TPR=" not in target_url and "/jobs/search" in target_url:
                    sep = "&" if "?" in target_url else "?"
                    target_url += f"{sep}f_TPR=r604800&sortBy=DD"

                page.goto(target_url, timeout=35000, wait_until="domcontentloaded")
                page.wait_for_timeout(3000)

                for _ in range(3):
                    try:
                        page.evaluate("window.scrollBy(0, 800);")
                        page.wait_for_timeout(400)
                    except Exception:
                        pass

                cards = page.query_selector_all(
                    "li.artdeco-list__item, li[data-occludable-job-id], div.job-card-container, div.entity-result, li:has(a[href*='/jobs/view/'])"
                )

                extracted_for_alert = 0
                for card in cards:
                    try:
                        title_el = (
                            card.query_selector(".job-card-list__title, a.job-card-container__link, .entity-result__title-text a") or
                            card.query_selector("a[href*='/jobs/view/'], strong, a")
                        )
                        comp_el = (
                            card.query_selector(".job-card-container__company-name, .entity-result__primary-subtitle") or
                            card.query_selector(".job-card-container__primary-description")
                        )
                        loc_el = (
                            card.query_selector(".job-card-container__metadata-item, .entity-result__secondary-subtitle") or
                            card.query_selector(".artdeco-entity-lockup__caption")
                        )

                        if not title_el:
                            continue

                        raw_url = title_el.get_attribute("href") or ""
                        clean_url = urllib.parse.urljoin("https://www.linkedin.com", raw_url).split("?")[0] if raw_url else ""

                        if "/jobs/view/" not in clean_url:
                            continue

                        text_lines = [l.strip() for l in card.inner_text().split("\n") if l.strip() and not l.strip().startswith("Easy Apply") and not l.strip().startswith("Promoted") and not l.strip().startswith("Actively recruiting") and "verification" not in l.lower()]
                        
                        title = title_el.inner_text().strip() if title_el else (text_lines[0] if text_lines else "Untitled Role")
                        company = comp_el.inner_text().strip() if comp_el else (text_lines[1] if len(text_lines) > 1 else "LinkedIn Network")
                        location = loc_el.inner_text().strip() if loc_el else (text_lines[2] if len(text_lines) > 2 else "Remote / Various")

                        title = title.split("\n")[0].split(" with verification")[0].strip()
                        company = company.split("\n")[0].split(" · ")[0].strip()
                        location = location.split("\n")[0].strip()

                        # Prevent job titles or alert search queries from being treated as company names
                        c_lower = company.lower()
                        if (
                            company == title
                            or len(company) > 60
                            or any(c_lower.startswith(w) for w in ["senior ", "staff ", "principal ", "lead ", "software ", "technical lead", "back-end", "front-end", "gen ai", "platform engineer"])
                        ):
                            company = "LinkedIn Network"

                        if title and clean_url and not any(j["url"] == clean_url for j in alert_jobs):
                            alert_jobs.append({
                                "title": title,
                                "company": company,
                                "location": location,
                                "url": clean_url,
                                "description": f"LinkedIn Alert opening for {title} at {company} in {location}.",
                                "source": "LinkedIn Job Alert",
                                "source_type": "LinkedIn"
                            })
                            extracted_for_alert += 1

                        if extracted_for_alert >= max_jobs_per_alert:
                            break
                    except Exception as card_err:
                        logger.debug(f"Error parsing alert job card: {card_err}")

            except Exception as alert_err:
                logger.debug(f"Error crawling alert URL {alert_url}: {alert_err}")

    except Exception as e:
        logger.warning(f"LinkedIn job alerts scan failed: {e}")
    finally:
        if own_context and context:
            try:
                context.close()
            except Exception:
                pass

    return alert_jobs

_LINKEDIN_CLOSED_MARKERS = (
    "no longer accepting applications",
    "position closed",
    "job is no longer available",
    "this job has expired",
)

# LinkedIn appends a recency label ("Reposted 1w ago", "Posted 2d ago") that must never be
# folded into the title/location, and its saved-job cards sometimes collapse to a single line.
# No leading \b: LinkedIn's collapsed innerText often concatenates the label directly onto the
# previous word (e.g. "...HyderabadReposted 1w ago").
_LINKEDIN_RECENCY_RE = re.compile(
    r"(?:Reposted|Posted|Updated)\b"
    r"(?:\s+\d+\s*(?:min|mins|minute|minutes|m|h|hr|hrs|hour|hours|d|day|days|w|wk|wks|week|weeks"
    r"|mo|month|months|y|yr|yrs|year|years)\s+ago|\s+just now|\s+today)?",
    re.IGNORECASE,
)

_LINKEDIN_CARD_NOISE = (
    "notification", "skip to", "easy apply", "actively recruiting",
    "with verification", "promoted",
)
_LINKEDIN_CARD_ACTIONS = {
    "apply", "save", "saved", "unsave", "follow", "dismiss", "see more", "show more",
}


def _clean_linkedin_card_segments(container_text: str) -> List[str]:
    """Splits a LinkedIn job card's innerText into meaningful lines, stripping recency/noise."""
    text = _LINKEDIN_RECENCY_RE.sub("\n", container_text or "")
    segments: List[str] = []
    for raw in text.split("\n"):
        line = raw.strip()
        if not line:
            continue
        low = line.lower()
        if any(skip in low for skip in _LINKEDIN_CARD_NOISE):
            continue
        if low in _LINKEDIN_CARD_ACTIONS:
            continue
        segments.append(line)
    return segments


def _parse_linkedin_job_card_text(container_text: str, title_hint: str = "") -> Dict[str, str]:
    """
    Best-effort title/company/location extraction from a LinkedIn job card's raw innerText.

    Handles both the classic multi-line layout
    ("Title\\nCompany · Location\\nReposted 1w ago") and LinkedIn's collapsed single-line
    variant ("TitleCompany · LocationReposted 1w ago"). When the card text is collapsed,
    ``title_hint`` (the /jobs/view/ anchor's own text) recovers the title and lets us split the
    merged "TitleCompany" segment.
    """
    segments = _clean_linkedin_card_segments(container_text)

    hint = title_hint.strip() if isinstance(title_hint, str) else ""
    if hint and " · " not in hint and not _LINKEDIN_RECENCY_RE.search(hint) and len(hint) <= 160:
        title = hint.split(" with verification")[0].strip()
    else:
        title = segments[0].split(" · ")[0].strip() if segments else ""

    company = "Unknown Company"
    location = "Remote / Various"

    for seg in segments:
        if " · " in seg:
            left, _, right = seg.partition(" · ")
            left = left.strip()
            # Collapsed "TitleCompany · Location": strip the recovered title prefix.
            if title and left.startswith(title):
                left = left[len(title):].strip()
            company = left.strip(" ·-") or company
            location = right.strip(" ·-") or location
            break

    if company == "Unknown Company" and len(segments) > 1:
        company = segments[1].split(" · ")[0].strip() or company
    if location == "Remote / Various" and len(segments) > 2:
        location = segments[2].strip() or location

    if not title:
        return {"title": "", "company": company, "location": location}
    return {"title": title, "company": company, "location": location}

def _extract_linkedin_job_from_anchor(anchor) -> Optional[Dict[str, Any]]:
    """
    Avoids depending on LinkedIn's frequently-changing card class names.
    """
    try:
        raw_url = anchor.get_attribute("href") or ""
    except Exception:
        return None
    if "/jobs/view/" not in raw_url:
        return None

    clean_url = urllib.parse.urljoin("https://www.linkedin.com", raw_url).split("?")[0]

    container_text = ""
    try:
        container_text = anchor.evaluate(
            "el => { const c = el.closest('li, div[data-occludable-job-id], "
            "div.job-card-container, div.entity-result, div[data-view-name]'); "
            "return (c || el).innerText; }"
        ) or ""
    except Exception:
        pass
    if not container_text:
        try:
            container_text = anchor.inner_text()
        except Exception:
            container_text = ""

    # The /jobs/view/ anchor usually wraps just the role title — a reliable title source even
    # when the surrounding card's innerText collapses to a single line.
    anchor_text = ""
    try:
        anchor_text = anchor.inner_text()
    except Exception:
        anchor_text = ""
    if not isinstance(anchor_text, str):
        anchor_text = ""

    parsed = _parse_linkedin_job_card_text(container_text, title_hint=anchor_text)
    if not parsed.get("title"):
        return None

    low = container_text.lower()
    return {
        "title": parsed["title"],
        "company": parsed["company"] or "Unknown Company",
        "location": parsed["location"] or "Remote / Various",
        "url": clean_url,
        "is_closed": any(m in low for m in _LINKEDIN_CLOSED_MARKERS),
        "unsaved": False,
    }

def scan_and_sync_linkedin_saved_jobs(
    playwright_instance,
    headless: bool = True,
    auto_unsave_closed: bool = True,
    max_pages: int = 25,
    page=None
) -> List[Dict[str, Any]]:
    """
    Scans the entire library of saved jobs on LinkedIn across all pagination pages.
    Detects if any saved job is closed or no longer accepting applications.
    If closed and auto_unsave_closed is True, automatically unsaves/prunes it from LinkedIn.

    Uses an anchor-first extraction strategy (`/jobs/view/` links) so it does not break
    when LinkedIn reshuffles its job-card CSS class names.
    """
    saved_jobs = []
    own_context = False
    context = None
    seen_urls = set()
    try:
        if page is None:
            context = get_persistent_browser_context(playwright_instance, headless=headless)
            page = context.pages[0] if (hasattr(context, "pages") and isinstance(context.pages, list) and len(context.pages) > 0) else context.new_page()
            own_context = True

        # Support both modern my-items/saved-jobs and jobs/tracker/saved URLs
        target_urls = [
            "https://www.linkedin.com/my-items/saved-jobs/",
            "https://www.linkedin.com/jobs/tracker/saved/"
        ]

        for target_url in target_urls:
            try:
                page.goto(target_url, timeout=40000, wait_until="domcontentloaded")
                page.wait_for_timeout(3500)
            except Exception as nav_err:
                logger.debug(f"Saved jobs nav to {target_url} notice: {nav_err}")
                continue

            for page_idx in range(1, max_pages + 1):
                # 1. Scroll to trigger lazy DOM rendering
                for _ in range(5):
                    try:
                        page.evaluate("window.scrollBy(0, 1200);")
                        page.wait_for_timeout(450)
                    except Exception:
                        pass

                # 2. Collect every job link on the page (class-agnostic), dedupe by URL
                page_found_count = 0
                try:
                    anchors = page.query_selector_all("a[href*='/jobs/view/']")
                except Exception:
                    anchors = []

                for anchor in anchors:
                    try:
                        rec = _extract_linkedin_job_from_anchor(anchor)
                        if not rec or rec["url"] in seen_urls:
                            continue
                        seen_urls.add(rec["url"])

                        if rec["is_closed"] and auto_unsave_closed:
                            try:
                                unsave_btn = anchor.evaluate_handle(
                                    "el => { const c = el.closest('li, div[data-occludable-job-id], "
                                    "div.job-card-container, div.entity-result, div[data-view-name]') || el.parentElement; "
                                    "return c ? c.querySelector(\"button[aria-label*='Unsave' i], button[aria-label*='Saved' i]\") : null; }"
                                ).as_element()
                                if unsave_btn:
                                    unsave_btn.click()
                                    rec["unsaved"] = True
                                    page.wait_for_timeout(250)
                            except Exception:
                                pass

                        saved_jobs.append(rec)
                        page_found_count += 1
                    except Exception as card_err:
                        logger.debug(f"Error parsing saved job card: {card_err}")

                logger.info(f"LinkedIn Saved Jobs: page {page_idx} yielded {page_found_count} postings (Total accumulated: {len(saved_jobs)}).")

                if page_found_count == 0 and page_idx > 1:
                    break

                # 3. Advance to the next page if a pagination control exists
                next_btn = (
                    page.query_selector("button.artdeco-pagination__button--next:not([disabled])") or
                    page.query_selector("button[aria-label='Next']:not([disabled])") or
                    page.query_selector("button[aria-label='Next page']:not([disabled])") or
                    page.query_selector(f"button[aria-label='Page {page_idx + 1}']:not([disabled])") or
                    page.query_selector(f"li[data-test-pagination-page-btn='{page_idx + 1}'] button")
                )

                if next_btn and hasattr(next_btn, "is_enabled") and next_btn.is_enabled():
                    try:
                        next_btn.click()
                        page.wait_for_timeout(2500)
                    except Exception as click_err:
                        logger.debug(f"Next page click notice: {click_err}")
                        break
                else:
                    break

    except Exception as e:
        logger.warning(f"LinkedIn saved jobs scan failed: {e}")
    finally:
        if own_context and context:
            try:
                context.close()
            except Exception:
                pass

    return saved_jobs

# -------------------------------------------------------------
# Assistant Floating UI Overlay Panel Injection
# -------------------------------------------------------------

import html as html_lib
import threading

def generate_helper_panel_html(
    resume_data: dict, 
    cover_letter: str, 
    tailored_points: str,
    cold_msg: str = "",
    job_id: Optional[int] = None
) -> str:
    """
    Generates inline HTML & CSS for a floating, draggable application assistant widget with 1-click status sync & autofill.
    """
    name = html_lib.escape(str(resume_data.get("name", "") or ""))
    email = html_lib.escape(str(resume_data.get("email", "") or ""))
    phone = html_lib.escape(str(resume_data.get("phone", "") or ""))
    
    cl_escaped = html_lib.escape(cover_letter or "")
    msg_escaped = html_lib.escape(cold_msg or "")
    tp_escaped = html_lib.escape(tailored_points or "")

    panel_html = f"""
    <div id="job-agent-panel" style="
        position: fixed;
        bottom: 24px;
        right: 24px;
        width: 390px;
        min-width: 300px;
        max-width: 90vw;
        height: auto;
        min-height: 180px;
        max-height: 85vh;
        background: #1e1e2e;
        color: #cdd6f4;
        border-radius: 12px;
        box-shadow: 0 12px 36px rgba(0,0,0,0.6);
        font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
        font-size: 13px;
        z-index: 2147483647;
        display: flex;
        flex-direction: column;
        overflow: hidden;
        border: 1px solid #45475a;
        resize: both;
        box-sizing: border-box;
    ">
        <!-- Header -->
        <div style="background: #313244; padding: 10px 14px; display: flex; justify-content: space-between; align-items: center; cursor: grab; user-select: none; border-bottom: 1px solid #45475a;" id="job-agent-header">
            <div style="display:flex; align-items:center; gap:8px;">
                <span style="font-size:12px; opacity:0.6; cursor:grab;" title="Drag to move panel">⠿</span>
                <span style="font-weight: 700; font-size: 13px; color: #89b4fa;">🚀 Job Alert Assistant</span>
            </div>
            <div style="display:flex; align-items:center; gap:6px;">
                {f'<button id="job-agent-mark-applied-btn" style="background:#a6e3a1; color:#11111b; font-weight:700; border:none; border-radius:4px; padding:3px 8px; cursor:pointer; font-size:11px;">Mark Applied</button>' if job_id else ''}
                <button id="job-agent-toggle-btn" title="Collapse / Expand" style="background:#45475a; border:none; color:#cdd6f4; font-size:13px; font-weight:bold; cursor:pointer; padding:2px 7px; border-radius:4px; line-height:1;">−</button>
                <button id="job-agent-close-btn" title="Close" onclick="document.getElementById('job-agent-panel').style.display='none'" style="background:none; border:none; color:#f38ba8; font-size:16px; cursor:pointer; padding:0 4px; line-height:1;">✕</button>
            </div>
        </div>

        <!-- Quick Action Toolbar -->
        <div id="job-agent-toolbar" style="background: #24273a; padding: 8px 14px; display: flex; gap: 8px; align-items: center; border-bottom: 1px solid #363a4f;">
            <button id="job-agent-autofill-btn" style="flex:1; background:#89b4fa; color:#11111b; font-weight:700; border:none; border-radius:6px; padding:6px 10px; cursor:pointer; font-size:12px; display:flex; align-items:center; justify-content:center; gap:4px;">
                ⚡ Auto-Fill All Form Fields
            </button>
        </div>

        <!-- Content Body -->
        <div id="job-agent-body" style="padding: 12px 14px; overflow-y: auto; flex: 1; display: flex; flex-direction: column; gap: 10px;">
            <!-- Candidate Quick Info -->
            <div style="background: #181825; padding: 10px; border-radius: 8px;">
                <div style="font-weight: 600; color: #a6e3a1; margin-bottom: 6px;">👤 Quick Fill Details</div>
                <div style="display:flex; justify-content:space-between; margin-bottom:4px;">
                    <span><b>Name:</b> {name}</span>
                    <button class="job-agent-copy-btn" data-copy="{name}" style="background:#45475a; color:#cdd6f4; border:none; border-radius:4px; padding:2px 6px; cursor:pointer; font-size:11px;">Copy</button>
                </div>
                <div style="display:flex; justify-content:space-between; margin-bottom:4px;">
                    <span><b>Email:</b> {email}</span>
                    <button class="job-agent-copy-btn" data-copy="{email}" style="background:#45475a; color:#cdd6f4; border:none; border-radius:4px; padding:2px 6px; cursor:pointer; font-size:11px;">Copy</button>
                </div>
                <div style="display:flex; justify-content:space-between;">
                    <span><b>Phone:</b> {phone}</span>
                    <button class="job-agent-copy-btn" data-copy="{phone}" style="background:#45475a; color:#cdd6f4; border:none; border-radius:4px; padding:2px 6px; cursor:pointer; font-size:11px;">Copy</button>
                </div>
            </div>

            <!-- Cover Letter Draft -->
            <div style="background: #181825; padding: 10px; border-radius: 8px;">
                <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom: 6px;">
                    <span style="font-weight: 600; color: #f9e2af;">📝 Tailored Cover Letter</span>
                    <button class="job-agent-copy-btn" data-copy="{cl_escaped}" style="background:#89b4fa; color:#11111b; font-weight:600; border:none; border-radius:4px; padding:3px 8px; cursor:pointer; font-size:11px;">Copy All</button>
                </div>
                <div style="max-height: 90px; overflow-y:auto; background:#11111b; padding:8px; border-radius:6px; font-size:11px; white-space:pre-wrap;">{cl_escaped or 'No cover letter generated yet.'}</div>
            </div>

            <!-- Cold Outreach Note -->
            {f'''
            <div style="background: #181825; padding: 10px; border-radius: 8px;">
                <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom: 6px;">
                    <span style="font-weight: 600; color: #cba6f7;">💬 LinkedIn Recruiter Note</span>
                    <button class="job-agent-copy-btn" data-copy="{msg_escaped}" style="background:#cba6f7; color:#11111b; font-weight:600; border:none; border-radius:4px; padding:3px 8px; cursor:pointer; font-size:11px;">Copy Note</button>
                </div>
                <div style="max-height: 70px; overflow-y:auto; background:#11111b; padding:8px; border-radius:6px; font-size:11px;">{msg_escaped}</div>
            </div>
            ''' if cold_msg else ''}

            <!-- Tailoring Tips -->
            {f'''
            <div style="background: #181825; padding: 10px; border-radius: 8px;">
                <div style="font-weight: 600; color: #fab387; margin-bottom: 6px;">🎯 Resume Tailoring Tips</div>
                <div style="max-height: 80px; overflow-y:auto; font-size:11px;">{tp_escaped}</div>
            </div>
            ''' if tailored_points else ''}
        </div>
    </div>
    """
    return panel_html

def inject_helper_panel(
    page, 
    resume_data: dict, 
    cover_letter: str, 
    tailored_points: str, 
    cold_msg: str = "",
    job_id: Optional[int] = None
):
    """
    Injects the assistant UI panel and activates real-time automated deep form-filling across portals.
    """
    qa_items = []
    try:
        from backend.database import init_db, ApplicationQuestionAnswer
        import backend.database as db_mod
        if db_mod.SessionLocal is None:
            init_db()
        db = db_mod.SessionLocal()
        try:
            records = db.query(ApplicationQuestionAnswer).all()
            for r in records:
                qa_items.append({
                    "question": r.question_text,
                    "answer": r.answer_text,
                    "category": r.category
                })
        finally:
            db.close()
    except Exception as e:
        logger.debug(f"Could not load QA memory for helper panel: {e}")

    name = str(resume_data.get("name", "") or "")
    email = str(resume_data.get("email", "") or "")
    phone = str(resume_data.get("phone", "") or "")
    explicit_first = str(resume_data.get("firstName", "") or resume_data.get("first_name", "") or "")
    explicit_last = str(resume_data.get("lastName", "") or resume_data.get("last_name", "") or "")
    first_name, last_name = split_candidate_name(name, explicit_first=explicit_first, explicit_last=explicit_last)

    links = resume_data.get("links", {}) if isinstance(resume_data.get("links"), dict) else {}
    linkedin_url = links.get("linkedin") or links.get("LinkedIn") or ""
    github_url = links.get("github") or links.get("GitHub") or ""
    portfolio_url = links.get("portfolio") or links.get("website") or ""

    exp_list = resume_data.get("experience", [])
    latest_exp = exp_list[0] if (isinstance(exp_list, list) and exp_list) else {}
    current_company = latest_exp.get("company", "") if isinstance(latest_exp, dict) else ""
    current_title = latest_exp.get("title", "") if isinstance(latest_exp, dict) else ""
    location = resume_data.get("location", "") or ""

    panel_html = generate_helper_panel_html(resume_data, cover_letter, tailored_points, cold_msg, job_id=job_id)

    payload = {
        "html": panel_html,
        "jobId": job_id,
        "name": name,
        "firstName": first_name,
        "lastName": last_name,
        "email": email,
        "phone": phone,
        "linkedinUrl": linkedin_url,
        "githubUrl": github_url,
        "portfolioUrl": portfolio_url,
        "currentCompany": current_company,
        "currentTitle": current_title,
        "location": location,
        "coverLetter": cover_letter,
        "qaMemory": qa_items
    }

    script = """
    (payload) => {
        const existing = document.getElementById('job-agent-panel');
        if (existing) existing.remove();

        const container = document.createElement('div');
        container.innerHTML = payload.html;
        if (container.firstElementChild && document.body) {
            const panel = container.firstElementChild;
            document.body.appendChild(panel);

            // 1. Draggable movement logic
            const header = panel.querySelector('#job-agent-header');
            if (header) {
                let isDragging = false;
                let startX = 0, startY = 0;
                let startLeft = 0, startTop = 0;

                header.addEventListener('mousedown', (e) => {
                    if (e.target.tagName === 'BUTTON' || e.target.closest('button')) return;
                    isDragging = true;
                    header.style.cursor = 'grabbing';
                    startX = e.clientX;
                    startY = e.clientY;

                    const rect = panel.getBoundingClientRect();
                    startLeft = rect.left;
                    startTop = rect.top;

                    panel.style.bottom = 'auto';
                    panel.style.right = 'auto';
                    panel.style.left = `${startLeft}px`;
                    panel.style.top = `${startTop}px`;
                    panel.style.userSelect = 'none';
                    e.preventDefault();
                });

                document.addEventListener('mousemove', (e) => {
                    if (!isDragging) return;
                    const dx = e.clientX - startX;
                    const dy = e.clientY - startY;

                    let newLeft = startLeft + dx;
                    let newTop = startTop + dy;

                    const maxLeft = window.innerWidth - panel.offsetWidth - 5;
                    const maxTop = window.innerHeight - panel.offsetHeight - 5;
                    newLeft = Math.max(5, Math.min(newLeft, maxLeft));
                    newTop = Math.max(5, Math.min(newTop, maxTop));

                    panel.style.left = `${newLeft}px`;
                    panel.style.top = `${newTop}px`;
                });

                document.addEventListener('mouseup', () => {
                    if (isDragging) {
                        isDragging = false;
                        header.style.cursor = 'grab';
                        panel.style.userSelect = 'auto';
                    }
                });
            }

            // 2. Collapse / Expand toggle logic
            const toggleBtn = panel.querySelector('#job-agent-toggle-btn');
            const toolbar = panel.querySelector('#job-agent-toolbar');
            const body = panel.querySelector('#job-agent-body');
            if (toggleBtn && toolbar && body) {
                let isCollapsed = false;
                let preCollapseHeight = '';
                toggleBtn.addEventListener('click', () => {
                    isCollapsed = !isCollapsed;
                    if (isCollapsed) {
                        preCollapseHeight = panel.style.height;
                        toolbar.style.display = 'none';
                        body.style.display = 'none';
                        panel.style.height = 'auto';
                        panel.style.minHeight = '0';
                        panel.style.resize = 'none';
                        toggleBtn.innerText = '+';
                    } else {
                        toolbar.style.display = 'flex';
                        body.style.display = 'flex';
                        panel.style.height = preCollapseHeight || 'auto';
                        panel.style.minHeight = '180px';
                        panel.style.resize = 'both';
                        toggleBtn.innerText = '−';
                    }
                });
            }

            // 3. Copy button handlers
            panel.querySelectorAll('.job-agent-copy-btn').forEach(btn => {
                btn.addEventListener('click', (e) => {
                    const text = e.target.getAttribute('data-copy') || '';
                    navigator.clipboard.writeText(text);
                    const original = e.target.innerText;
                    e.target.innerText = 'Copied!';
                    setTimeout(() => { e.target.innerText = original; }, 1500);
                });
            });

            // 4. Confirm Submission Status Sync (records that the candidate actually submitted)
            const markBtn = panel.querySelector('#job-agent-mark-applied-btn');
            if (markBtn && payload.jobId) {
                markBtn.addEventListener('click', async () => {
                    markBtn.innerText = 'Syncing...';
                    try {
                        await fetch(`http://127.0.0.1:8000/api/jobs/${payload.jobId}/confirm-submission`, {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' }
                        });
                        markBtn.innerText = '✅ Submitted';
                        markBtn.style.background = '#a6e3a1';
                        markBtn.style.color = '#11111b';
                    } catch (e) {
                        markBtn.innerText = '✅ Applied';
                    }
                });
            }

            // 5. Auto-Fill Button Handler
            const fillBtn = panel.querySelector('#job-agent-autofill-btn');
            if (fillBtn) {
                fillBtn.addEventListener('click', () => {
                    const count = runDeepAutoFill();
                    fillBtn.innerText = `⚡ Auto-Filled (${count} Fields)`;
                    setTimeout(() => { fillBtn.innerText = '⚡ Auto-Fill All Form Fields'; }, 2500);
                });
            }
        }

        // Deep Form Auto-Filler Logic with React-Select & Custom Question Support
        const runDeepAutoFill = () => {
            let count = 0;

            const setInputVal = (el, val) => {
                if (!el || val === undefined || val === null || val === '') return false;
                if (el.value && el.value.trim().length > 0) return false;
                el.focus();
                el.value = val;
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
                el.dispatchEvent(new Event('blur', { bubbles: true }));
                return true;
            };

            const fillMatches = (selectors, val) => {
                if (!val) return;
                for (const sel of selectors) {
                    const els = document.querySelectorAll(sel);
                    els.forEach(el => {
                        if (setInputVal(el, val)) count++;
                    });
                }
            };

            // Semantic Field Finder (finds inputs by surrounding label text)
            const fillByLabelMatch = (keywords, val) => {
                if (!val) return;
                const allInputs = document.querySelectorAll('input:not([type="hidden"]):not([type="radio"]):not([type="checkbox"]):not([type="file"]), textarea');
                allInputs.forEach(input => {
                    if (input.value && input.value.trim().length > 0) return;
                    const attrs = `${input.name || ''} ${input.id || ''} ${input.placeholder || ''} ${input.getAttribute('aria-label') || ''}`.toLowerCase();
                    let labelText = '';
                    const label = input.closest('label') || (input.id ? document.querySelector(`label[for="${input.id}"]`) : null) || input.closest('.field, .form-group, div[data-key]');
                    if (label) {
                        labelText = (label.innerText || label.textContent || '').toLowerCase();
                    }
                    const fullContext = `${attrs} ${labelText}`;
                    const matched = keywords.some(k => fullContext.includes(k.toLowerCase()));
                    if (matched) {
                        if (setInputVal(input, val)) count++;
                    }
                });
            };

            // Dropdown & React-Select / Combobox Selector
            const selectDropdownByQuestion = (questionKeywords, desiredOptionKeywords) => {
                const groups = document.querySelectorAll('.field, .form-group, div[data-key], fieldset, label, div[class*="question"], div[class*="field"], div[class*="custom-question"]');
                groups.forEach(group => {
                    const groupText = (group.innerText || group.textContent || '').toLowerCase();
                    const matchesQuestion = questionKeywords.some(qk => groupText.includes(qk.toLowerCase()));
                    if (!matchesQuestion) return;

                    // A. Standard HTML <select>
                    const selectEl = group.querySelector('select');
                    if (selectEl) {
                        for (let i = 0; i < selectEl.options.length; i++) {
                            const optText = (selectEl.options[i].text || '').toLowerCase().trim();
                            const optVal = (selectEl.options[i].value || '').toLowerCase().trim();
                            const matchesOpt = desiredOptionKeywords.some(dk => optText === dk.toLowerCase() || optText.startsWith(dk.toLowerCase()) || optVal === dk.toLowerCase());
                            if (matchesOpt) {
                                if (selectEl.selectedIndex !== i) {
                                    selectEl.selectedIndex = i;
                                    selectEl.dispatchEvent(new Event('change', { bubbles: true }));
                                    count++;
                                }
                                break;
                            }
                        }
                    }

                    // B. React-Select / Custom Combobox / ARIA Listbox (e.g. Greenhouse Modern React UI)
                    const reactSelectControl = group.querySelector('[class*="-control"], .select__control, div[role="combobox"], [class*="control"]');
                    if (reactSelectControl) {
                        const currentVal = (reactSelectControl.innerText || reactSelectControl.textContent || '').toLowerCase().trim();
                        const isAlreadySelected = desiredOptionKeywords.some(dk => currentVal.includes(dk.toLowerCase()) && !currentVal.includes('select'));
                        if (!isAlreadySelected) {
                            reactSelectControl.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true }));
                            reactSelectControl.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
                            
                            setTimeout(() => {
                                const options = document.querySelectorAll('[class*="-option"], .select__option, div[role="option"], li[role="option"], [id*="react-select"][role="option"]');
                                for (const opt of options) {
                                    const optText = (opt.innerText || opt.textContent || '').toLowerCase().trim();
                                    const matchesOpt = desiredOptionKeywords.some(dk => optText === dk.toLowerCase() || optText.startsWith(dk.toLowerCase()));
                                    if (matchesOpt) {
                                        opt.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true }));
                                        opt.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
                                        count++;
                                        break;
                                    }
                                }
                            }, 100);
                        }
                    }

                    // C. Radio Buttons inside the group
                    const radios = group.querySelectorAll('input[type="radio"]');
                    radios.forEach(radio => {
                        const rLabel = radio.closest('label') || (radio.id ? document.querySelector(`label[for="${radio.id}"]`) : null);
                        const rText = `${rLabel ? rLabel.innerText : ''} ${radio.value || ''}`.toLowerCase().trim();
                        const matchesOpt = desiredOptionKeywords.some(dk => rText === dk.toLowerCase() || rText.startsWith(dk.toLowerCase()));
                        if (matchesOpt && !radio.checked) {
                            radio.checked = true;
                            radio.dispatchEvent(new Event('change', { bubbles: true }));
                            count++;
                        }
                    });
                });
            };

            // Checkbox Selector inside Question Group
            const checkCheckboxInQuestionGroup = (questionKeywords, desiredCheckboxKeywords) => {
                const groups = document.querySelectorAll('.field, .form-group, div[data-key], fieldset, div[class*="question"], div[class*="field"], form, div[id*="sanction"], div[id*="compliance"]');
                groups.forEach(group => {
                    const groupText = (group.innerText || group.textContent || '').toLowerCase();
                    const matchesQuestion = questionKeywords.some(qk => groupText.includes(qk.toLowerCase()));
                    if (!matchesQuestion) return;

                    const checkboxes = group.querySelectorAll('input[type="checkbox"]');
                    checkboxes.forEach(chk => {
                        const chkLabel = chk.closest('label') || (chk.id ? document.querySelector(`label[for="${chk.id}"]`) : null) || chk.parentElement;
                        const chkText = (chkLabel ? (chkLabel.innerText || chkLabel.textContent || '') : (chk.value || '')).toLowerCase().trim();
                        const matchesDesired = desiredCheckboxKeywords.some(dk => chkText.includes(dk.toLowerCase()));
                        if (matchesDesired && !chk.checked) {
                            chk.checked = true;
                            chk.dispatchEvent(new Event('change', { bubbles: true }));
                            chk.dispatchEvent(new Event('input', { bubbles: true }));
                            count++;
                        }
                    });
                });
            };

            // 1. Standard Personal Details
            fillMatches(['input[name*="first_name" i]', 'input[id*="first_name" i]', 'input[name*="firstname" i]', 'input[id*="firstname" i]', 'input[autocomplete="given-name"]', 'input[aria-label*="first name" i]', 'input[placeholder*="first name" i]'], payload.firstName);
            fillMatches(['input[name*="last_name" i]', 'input[id*="last_name" i]', 'input[name*="lastname" i]', 'input[id*="lastname" i]', 'input[autocomplete="family-name"]', 'input[aria-label*="last name" i]', 'input[placeholder*="last name" i]'], payload.lastName);
            fillMatches(['input[name="name" i]', 'input[id="name" i]', 'input[name*="full_name" i]', 'input[id*="full_name" i]', 'input[name*="fullname" i]', 'input[id*="fullname" i]', 'input[autocomplete="name"]', 'input[placeholder*="full name" i]'], payload.name);
            fillMatches(['input[type="email"]', 'input[name*="email" i]', 'input[id*="email" i]', 'input[autocomplete="email"]', 'input[placeholder*="email" i]'], payload.email);
            fillMatches(['input[type="tel"]', 'input[name*="phone" i]', 'input[id*="phone" i]', 'input[autocomplete="tel"]', 'input[name*="mobile" i]', 'input[placeholder*="phone" i]'], payload.phone);

            // 2. Profiles & Social URLs
            fillMatches(['input[name*="linkedin" i]', 'input[id*="linkedin" i]', 'input[placeholder*="linkedin.com" i]', 'input[aria-label*="linkedin" i]'], payload.linkedinUrl);
            fillMatches(['input[name*="github" i]', 'input[id*="github" i]', 'input[placeholder*="github.com" i]'], payload.githubUrl);
            fillMatches(['input[name*="website" i]', 'input[id*="website" i]', 'input[name*="portfolio" i]', 'input[placeholder*="portfolio" i]'], payload.portfolioUrl);

            // 3. Current Firm / Company (Handles Greenhouse "Name of the current firm you're part of? *")
            fillMatches(['input[name*="current_company" i]', 'input[id*="current_company" i]', 'input[name*="org" i]', 'input[placeholder*="company" i]'], payload.currentCompany);
            fillByLabelMatch(['current firm', "firm you're part of", 'name of the current firm', 'current company', 'current employer', 'employer'], payload.currentCompany);

            // 4. Current Title & Location
            fillMatches(['input[name*="current_title" i]', 'input[id*="current_title" i]', 'input[name*="headline" i]', 'input[placeholder*="title" i]'], payload.currentTitle);
            fillMatches(['input[name*="location" i]', 'input[id*="location" i]', 'input[name*="city" i]', 'input[id*="city" i]', 'input[placeholder*="city" i]', 'input[placeholder*="location" i]'], payload.location);
            fillByLabelMatch(['city', 'current location', 'where are you located'], payload.location);

            // 5-11. Personal answers (how did you hear, work authorization, visa sponsorship,
            // prior employment, sanctions screening, notice period) are intentionally NOT
            // hardcoded. They are private candidate facts and are only filled from the user's
            // stored Q&A memory bank (section 13 below) when an answer was explicitly provided.

            // 12. Cover Letter
            if (payload.coverLetter) {
                const textareas = document.querySelectorAll('textarea');
                textareas.forEach(ta => {
                    const attrs = `${ta.name || ''} ${ta.id || ''} ${ta.placeholder || ''} ${ta.getAttribute('aria-label') || ''}`.toLowerCase();
                    if (attrs.includes('cover') || attrs.includes('message') || attrs.includes('letter') || attrs.includes('comments') || attrs.includes('additional')) {
                        if (setInputVal(ta, payload.coverLetter)) count++;
                    }
                });
            }

            // 13. Application Q&A Memory Bank Semantic Evaluation
            if (payload.qaMemory && Array.isArray(payload.qaMemory)) {
                payload.qaMemory.forEach(qa => {
                    const qText = (qa.question || '').toLowerCase();
                    const ansText = (qa.answer || '').trim();
                    if (!qText || !ansText) return;

                    const keywords = qText.split(/[^a-z0-9]/).filter(w => w.length > 3 && !['what', 'your', 'have', 'with', 'from', 'this', 'that', 'please', 'select', 'which', 'about'].includes(w));
                    if (keywords.length >= 2) {
                        fillByLabelMatch(keywords, ansText);
                        if (ansText.toLowerCase() === 'yes' || ansText.toLowerCase() === 'no' || ansText.length < 25) {
                            selectDropdownByQuestion(keywords, [ansText]);
                        }
                    }
                });
            }

            // 14. EEO / Demographic Dropdowns -> Decline / Prefer Not To Say
            document.querySelectorAll('select').forEach(sel => {
                const selName = (sel.name || '' + ' ' + sel.id || '').toLowerCase();
                if (selName.includes('gender') || selName.includes('race') || selName.includes('veteran') || selName.includes('disability') || selName.includes('eeo')) {
                    for (let i = 0; i < sel.options.length; i++) {
                        const optText = sel.options[i].text.toLowerCase();
                        if (optText.includes('decline') || optText.includes('prefer not') || optText.includes('choose not') || optText.includes('do not wish')) {
                            if (sel.selectedIndex !== i) {
                                sel.selectedIndex = i;
                                sel.dispatchEvent(new Event('change', { bubbles: true }));
                                count++;
                            }
                            break;
                        }
                    }
                }
            });

            return count;
        };

        // Run immediately
        runDeepAutoFill();

        // MutationObserver to auto-fill dynamic application modals (e.g. clicking "Apply Now")
        try {
            const observer = new MutationObserver(() => {
                runDeepAutoFill();
            });
            observer.observe(document.body, { childList: true, subtree: true });
        } catch (e) {}

        // Periodic check for 20 seconds after page load
        let attempts = 0;
        const interval = setInterval(() => {
            attempts++;
            runDeepAutoFill();
            if (attempts > 12) clearInterval(interval);
        }, 1500);
    }
    """
    try:
        page.evaluate(script, payload)
    except Exception as e:
        logger.debug(f"Error evaluating helper overlay injection: {e}")

# -------------------------------------------------------------
# Singleton Browser Session & Multi-Tab Apply Manager
# -------------------------------------------------------------

class BrowserSessionManager:
    """
    Thread-safe Singleton Playwright Browser Context Manager.
    Maintains a single persistent Chromium window across all applications.
    Opens new jobs in separate tabs (pages) within the same window and automatically
    attaches the assistant overlay to newly opened tabs/popups (e.g. LinkedIn redirects).
    """
    _instance = None
    _lock = threading.Lock()

    def __init__(self):
        self._playwright = None
        self._context = None
        self._loop_lock = threading.Lock()
        self._last_application_context = {}

    @classmethod
    def get_instance(cls) -> "BrowserSessionManager":
        with cls._lock:
            if cls._instance is None:
                cls._instance = BrowserSessionManager()
            return cls._instance

    def _setup_context_listeners(self, context):
        try:
            context.on("page", self._handle_new_tab)
        except Exception as e:
            logger.debug(f"Could not attach context page listener: {e}")

    def _handle_new_tab(self, page):
        """Automatically hooks into any new tab/popup spawned by the candidate (e.g. LinkedIn Apply redirect)."""
        def on_dom_loaded():
            try:
                if page.url.startswith("chrome://") or page.url.startswith("about:"):
                    return
                ctx = self._last_application_context
                if ctx and ctx.get("resume_data"):
                    inject_helper_panel(
                        page=page,
                        resume_data=ctx.get("resume_data", {}),
                        cover_letter=ctx.get("cover_letter", ""),
                        tailored_points=ctx.get("tailored_points", ""),
                        cold_msg=ctx.get("cold_msg", ""),
                        job_id=ctx.get("job_id")
                    )
                    resume_file_path = ctx.get("resume_file_path")
                    if resume_file_path and os.path.exists(resume_file_path):
                        try:
                            file_inputs = page.locator('input[type="file"]')
                            if file_inputs.count() > 0:
                                file_inputs.first.set_input_files(resume_file_path)
                        except Exception:
                            pass
            except Exception as e:
                logger.debug(f"New tab auto-injection notice: {e}")

        try:
            page.on("domcontentloaded", on_dom_loaded)
        except Exception:
            pass

    def _ensure_context(self, headless: bool = False):
        with self._loop_lock:
            if self._playwright is None:
                from playwright.sync_api import sync_playwright
                self._playwright = sync_playwright().start()
            
            if self._context is None:
                self._context = get_persistent_browser_context(self._playwright, headless=headless)
                self._setup_context_listeners(self._context)
            else:
                try:
                    # Probe if context is still active
                    _ = self._context.pages
                except Exception:
                    clean_stale_profile_locks()
                    self._context = get_persistent_browser_context(self._playwright, headless=headless)
                    self._setup_context_listeners(self._context)
            return self._context

    def open_job_tab(
        self,
        job_id: Optional[int],
        url: str,
        resume_data: dict,
        cover_letter: str,
        tailored_points: str,
        cold_msg: str = "",
        resume_file_path: Optional[str] = None,
        headless: bool = False
    ):
        """
        Opens a new tab in the single shared browser window, navigates to the job URL,
        injects the deep form auto-filler and assistant overlay, and attaches the resume document.
        """
        import subprocess
        context = self._ensure_context(headless=headless)
        
        # Save context for newly spawned tabs/popups
        self._last_application_context = {
            "job_id": job_id,
            "url": url,
            "resume_data": resume_data,
            "cover_letter": cover_letter,
            "tailored_points": tailored_points,
            "cold_msg": cold_msg,
            "resume_file_path": resume_file_path
        }

        # Reuse initial blank tab if available, otherwise open new tab
        existing_pages = context.pages
        if len(existing_pages) == 1 and existing_pages[0].url in ("about:blank", "chrome://newtab/"):
            page = existing_pages[0]
        else:
            page = context.new_page()

        # Listen for internal SPA/redirect navigations on this page as well
        self._handle_new_tab(page)

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.bring_to_front()

            # Bring Chrome to foreground on macOS
            for app_name in ["Google Chrome for Testing", "Google Chrome", "Chromium"]:
                subprocess.run(
                    ["osascript", "-e", f'tell application "{app_name}" to activate'], 
                    check=False, 
                    stdout=subprocess.DEVNULL, 
                    stderr=subprocess.DEVNULL
                )

            # Inject enhanced assistant panel & automated form-filler
            inject_helper_panel(
                page=page,
                resume_data=resume_data,
                cover_letter=cover_letter,
                tailored_points=tailored_points,
                cold_msg=cold_msg,
                job_id=job_id
            )

            # Auto-attach resume file if present
            if resume_file_path and os.path.exists(resume_file_path):
                try:
                    file_inputs = page.locator('input[type="file"]')
                    if file_inputs.count() > 0:
                        file_inputs.first.set_input_files(resume_file_path)
                        logger.info(f"Auto-attached resume file '{resume_file_path}' to application form.")
                except Exception as file_err:
                    logger.debug(f"Resume auto-attachment skipped: {file_err}")

        except Exception as e:
            logger.error(f"Error opening job tab for job {job_id} ({url}): {e}")

def launch_assisted_application_session(
    url: str,
    resume_data: dict,
    cover_letter: str,
    tailored_points: str,
    cold_msg: str = "",
    job_id: Optional[int] = None,
    resume_file_path: Optional[str] = None,
    headless: bool = False
):
    """
    Delegates to BrowserSessionManager to open the role in a tab inside the single shared browser window.
    """
    manager = BrowserSessionManager.get_instance()
    manager.open_job_tab(
        job_id=job_id,
        url=url,
        resume_data=resume_data,
        cover_letter=cover_letter,
        tailored_points=tailored_points,
        cold_msg=cold_msg,
        resume_file_path=resume_file_path,
        headless=headless
    )

# -------------------------------------------------------------
# Google for Jobs Playwright Browser Fetcher
# -------------------------------------------------------------

def fetch_google_jobs_playwright(
    playwright_instance, 
    query: str, 
    location: Optional[str] = None, 
    headless: bool = True, 
    max_results: int = 15
) -> List[Dict[str, Any]]:
    """
    Automates navigation to Google for Jobs (ibp=htl;jobs) within the persistent browser session.
    Extracts deep job details, employer names, locations, and direct external application URLs.
    """
    if not query or not query.strip():
        return []

    search_terms = query.strip()
    if location and location.strip():
        search_terms = f"{search_terms} in {location.strip()}"

    encoded_query = urllib.parse.quote(search_terms)
    url = f"https://www.google.com/search?q={encoded_query}&ibp=htl;jobs"

    jobs = []
    try:
        context = get_persistent_browser_context(playwright_instance, headless=headless)
        page = context.new_page()
        page.goto(url, timeout=30000, wait_until="domcontentloaded")

        # Auto-dismiss Google cookie/terms consent dialogs if present
        try:
            consent_btn = page.query_selector("button:has-text('Accept all'), button:has-text('I agree'), button:has-text('Accept'), form[action*='consent'] button")
            if consent_btn and consent_btn.is_visible():
                consent_btn.click()
                page.wait_for_timeout(2000)
        except Exception:
            pass

        try:
            page.wait_for_selector("li.iFjolb, div.PwjeAc, div.gWSBe, div[data-encoded-docid], div.d5F0ib, div.KLsYvd, div[jsname='bN97Pc'], div.sp-c, div.s8bAkb, div[data-docid]", timeout=8000)
        except Exception:
            logger.debug("Google Jobs main list selector timeout, continuing with available DOM")

        card_elements = page.query_selector_all("li.iFjolb, div.PwjeAc, div.gWSBe, div[data-encoded-docid]") or page.query_selector_all("div.d5F0ib, div.KLsYvd, div[jsname='bN97Pc'], div.sp-c, div.s8bAkb, div[data-docid]")
        for card in card_elements[:max_results]:
            try:
                title_el = card.query_selector("div.BjJfJf, div.PUpOsf, div[role='heading'], h2, a.title") or card.query_selector("h3, .t-bold")
                title = title_el.inner_text().strip() if title_el else ""
                if not title or len(title) < 3:
                    continue

                company_el = card.query_selector("div.vNEEBe, div.wvy6fc, span.company, div.company") or card.query_selector(".s8bAkb, .MK9DJe")
                company = company_el.inner_text().strip() if company_el else "Target Employer"

                loc_el = card.query_selector("div.Qk80nd, div.oc1tfd, span.location") or card.query_selector(".Qk80nd")
                location_str = loc_el.inner_text().strip() if loc_el else "Remote / Various"

                link_el = card.query_selector("a[href]")
                apply_url = link_el.get_attribute("href") if link_el else url
                if apply_url and not apply_url.startswith("http"):
                    apply_url = f"https://www.google.com{apply_url}"

                snippet_el = card.query_selector("div.HBvxfe, span.WbZuDe, div.Yflw0c") or card.query_selector("div.Vb0Rqe")
                snippet = snippet_el.inner_text().strip() if snippet_el else f"Google for Jobs opening: {title} at {company}"

                jobs.append({
                    "title": title,
                    "company": company,
                    "location": location_str,
                    "salary_range": "Competitive",
                    "description": snippet,
                    "url": apply_url,
                    "posted_at": "Discovered via Google Jobs",
                    "source": "Google Jobs",
                    "source_type": "Google"
                })
            except Exception as e:
                logger.debug(f"Error parsing Google Jobs card element: {e}")

        # If zero jobs found via direct Google for Jobs widget, check organic search results on page
        if not jobs:
            organic_cards = page.query_selector_all("div.g, div.MjjYud, div[data-hveid]")
            for org in organic_cards[:max_results]:
                try:
                    title_el = org.query_selector("h3, a h3")
                    link_el = org.query_selector("a[href]")
                    snippet_el = org.query_selector("div[data-sncf], div.VwiC3b")

                    title_text = title_el.inner_text().strip() if title_el else ""
                    href = link_el.get_attribute("href") if link_el else ""
                    snip = snippet_el.inner_text().strip() if snippet_el else ""

                    if title_text and href and "google.com" not in href:
                        comp = "Direct Employer"
                        if " - " in title_text:
                            comp = title_text.split(" - ")[-1].strip()
                        elif " | " in title_text:
                            comp = title_text.split(" | ")[-1].strip()

                        clean_t = title_text.split(" - ")[0].split(" | ")[0].strip()
                        jobs.append({
                            "title": clean_t,
                            "company": comp,
                            "location": location or "Remote / Various",
                            "salary_range": "Competitive",
                            "description": snip or f"Opportunity for {clean_t} at {comp}",
                            "url": href,
                            "posted_at": "Discovered via Google Search",
                            "source": "Google Jobs",
                            "source_type": "Google"
                        })
                except Exception:
                    pass

        context.close()
    except Exception as e:
        logger.warning(f"Google Jobs Playwright extraction failed: {e}")

    # Deduplicate by title + company
    seen = set()
    deduped = []
    for j in jobs:
        key = (j["title"].lower().strip(), j["company"].lower().strip())
        if key not in seen:
            seen.add(key)
            deduped.append(j)

    return deduped[:max_results]


def fetch_google_jobs_followed_queries_playwright(
    playwright_instance, 
    headless: bool = True
) -> List[str]:
    """
    Navigates to Google for Jobs 'Following' tab in the persistent browser context.
    Extracts all saved/followed alert search queries created by the user.
    """
    url = "https://www.google.com/search?q=jobs&ibp=htl;jobs"
    followed_queries = []

    try:
        context = get_persistent_browser_context(playwright_instance, headless=headless)
        page = context.new_page()
        page.goto(url, timeout=30000, wait_until="domcontentloaded")
        page.wait_for_timeout(2000)

        # Look for the 'Following' tab
        following_tabs = page.query_selector_all("div[role='tab'], button[role='tab'], span, a")
        for tab in following_tabs:
            if "following" in (tab.inner_text() or "").lower():
                try:
                    tab.click()
                    page.wait_for_timeout(1500)
                    break
                except Exception:
                    pass

        # Extract followed search headings
        query_elements = page.query_selector_all("div.rPq8ae, div[data-query], div.M5l6Xe")
        for q_el in query_elements:
            raw = q_el.inner_text().strip()
            lines = [l.strip() for l in raw.split("\n") if l.strip() and not l.startswith("Email to:") and not l.startswith("Daily") and not l.startswith("Weekly") and not l == "Manage your updates"]
            for l in lines:
                if len(l) > 5 and not l.lower().startswith("manage") and l not in followed_queries:
                    clean_q = l.split("Email to:")[0].strip()
                    followed_queries.append(clean_q)

        page.close()
    except Exception as e:
        logger.warning(f"Could not extract followed Google Jobs queries via Playwright: {e}")

    return followed_queries


# -------------------------------------------------------------
# Hirist & External Application Tracker Sync
# -------------------------------------------------------------

def normalize_hirist_status(raw_status: str) -> str:
    """Normalizes raw status text from Hirist dashboard to internal Job status."""
    st = raw_status.lower()
    if any(k in st for k in ["not shortlisted", "rejected", "declined", "closed", "position closed", "not selected", "expired"]):
        return "Rejected"
    if any(k in st for k in ["interview", "interview scheduled", "contacted", "call scheduled", "telephonic screen"]):
        return "Interview"
    if any(k in st for k in ["shortlisted", "candidate shortlisted"]):
        return "Shortlisted"
    if any(k in st for k in ["viewed", "viewed by recruiter", "recruiter viewed"]):
        return "Applied"
    if any(k in st for k in ["applied", "application sent", "submitted", "awaiting"]):
        return "Applied"
    return "Applied"

def scan_and_sync_hirist_applications(
    playwright_instance,
    headless: bool = True,
    max_pages: int = 10,
    page=None
) -> List[Dict[str, Any]]:
    """
    Navigates to the candidate application dashboard on Hirist (hirist.tech / hirist.com)
    using the persistent browser context and extracts all tracked job applications with live recruiter statuses.
    """
    applications = []
    own_context = False
    context = None
    try:
        if page is None:
            context = get_persistent_browser_context(playwright_instance, headless=headless)
            page = context.pages[0] if (hasattr(context, "pages") and isinstance(context.pages, list) and len(context.pages) > 0) else context.new_page()
            own_context = True

        target_urls = [
            "https://www.hirist.tech/candidate/applications",
            "https://www.hirist.com/candidate/applications",
            "https://www.hirist.tech/candidate/applied-jobs"
        ]

        navigated = False
        for target_url in target_urls:
            try:
                page.goto(target_url, timeout=35000, wait_until="domcontentloaded")
                page.wait_for_timeout(3000)
                if "login" not in page.url.lower() and "signin" not in page.url.lower():
                    navigated = True
                    break
            except Exception as e:
                logger.debug(f"Hirist navigation notice for {target_url}: {e}")
                continue

        if not navigated and ("login" in page.url.lower() or "signin" in page.url.lower()):
            logger.warning("Hirist sync: User is not logged in to Hirist in persistent Chrome profile.")
            return []

        for current_page_idx in range(1, max_pages + 1):
            # Smooth scroll down to trigger dynamic card rendering
            for _ in range(3):
                try:
                    page.evaluate("window.scrollBy(0, 800);")
                    page.wait_for_timeout(400)
                except Exception:
                    pass

            cards = page.query_selector_all(
                ".application-card, .applied-job-card, div[class*='application-card'], div[class*='jobCard'], .app-card, div[class*='applied-job'], div.candidate-application-item, li:has(a[href*='/j/'])"
            )

            if not cards:
                # Fallback to generic cards containing job links
                cards = page.query_selector_all("div:has(a[href*='/j/']), li:has(a[href*='/j/'])")

            page_found_count = 0
            for card in cards:
                try:
                    title_el = (
                        card.query_selector("a[href*='/j/'], a.job-title, h2 a, a")
                    )
                    comp_el = (
                        card.query_selector(".company-name, .employer-name, .comp-name, div[class*='company'], span[class*='company']")
                    )
                    status_el = (
                        card.query_selector(".status-badge, .status-text, [class*='status'], [class*='badge'], span[class*='pipeline']")
                    )
                    loc_el = (
                        card.query_selector(".location, .job-location, span[class*='location']")
                    )
                    date_el = (
                        card.query_selector(".applied-date, span[class*='date'], div[class*='date'], small")
                    )

                    card_text = card.inner_text().strip() if hasattr(card, "inner_text") else ""
                    lines = [l.strip() for l in card_text.split("\n") if l.strip()]

                    title = ""
                    if title_el:
                        title = title_el.inner_text().strip()
                    elif lines:
                        title = lines[0]

                    company = ""
                    if comp_el:
                        company = comp_el.inner_text().strip()
                    elif len(lines) > 1:
                        company = lines[1]

                    raw_status = "Applied"
                    if status_el:
                        raw_status = status_el.inner_text().strip()
                    else:
                        for s_candidate in ["not shortlisted", "rejected", "closed", "position closed", "not selected", "interview scheduled", "interview", "shortlisted", "viewed", "applied"]:
                            if s_candidate in card_text.lower():
                                raw_status = s_candidate.capitalize()
                                break

                    normalized_status = normalize_hirist_status(raw_status)

                    raw_url = title_el.get_attribute("href") if title_el else ""
                    clean_url = urllib.parse.urljoin("https://www.hirist.tech", raw_url) if raw_url else ""

                    applied_date = ""
                    if date_el:
                        applied_date = date_el.inner_text().strip()
                    else:
                        date_match = re.search(r'applied(?:\s+on)?\s+([A-Za-z0-9,\s]{4,20})', card_text, re.IGNORECASE)
                        if date_match:
                            applied_date = date_match.group(0).strip()

                    location = loc_el.inner_text().strip() if loc_el else "India / Remote"

                    if title and not any(a["title"].lower() == title.lower() and a["company"].lower() == company.lower() for a in applications):
                        applications.append({
                            "title": title,
                            "company": company or "Hirist Employer",
                            "status": normalized_status,
                            "raw_status": raw_status,
                            "applied_date": applied_date,
                            "url": clean_url,
                            "location": location
                        })
                        page_found_count += 1
                except Exception as card_err:
                    logger.debug(f"Error parsing Hirist application card: {card_err}")

            logger.info(f"Hirist Application Sync: page {current_page_idx} found {page_found_count} applications (Total: {len(applications)}).")

            if page_found_count == 0 and current_page_idx > 1:
                break

            # Check next page button
            next_btn = (
                page.query_selector("button.pagination-next:not([disabled])") or
                page.query_selector("a[aria-label='Next']:not([disabled])") or
                page.query_selector("li.next a:not([disabled])") or
                page.query_selector(f"button[aria-label='Page {current_page_idx + 1}']")
            )
            if next_btn and hasattr(next_btn, "is_enabled") and next_btn.is_enabled():
                try:
                    next_btn.click()
                    page.wait_for_timeout(2500)
                except Exception:
                    break
            else:
                break

    except Exception as e:
        logger.warning(f"Hirist application sync failed: {e}")
    finally:
        if own_context and context:
            try:
                context.close()
            except Exception:
                pass

    return applications

def sync_email_application_events(playwright_instance, headless: bool = True) -> List[Dict[str, Any]]:
    """
    Scans candidate Gmail inbox via Playwright and extracts high-signal application status updates
    (interview invitations, rejection updates, and application submissions) matching them to employers.
    """
    from backend.scraper import extract_company_and_role_from_email_header

    raw_emails = scan_gmail_for_job_alerts(playwright_instance, headless=headless)
    events = []

    for email in raw_emails:
        cls = classify_email(
            email.get("subject", ""), email.get("snippet", ""), email.get("sender", "")
        )
        classification = email.get("classification") or cls["category"]
        confidence = cls["confidence"]

        if classification in ["interview", "rejection", "application_received", "offer", "screening"]:
            extracted = extract_company_and_role_from_email_header(
                email.get("subject", ""), email.get("snippet", ""), email.get("sender", "")
            )

            company = extracted.get("company")
            role = extracted.get("role") or "Software Engineer"

            if company:
                events.append({
                    "company": company,
                    "role": role,
                    "event_type": classification,
                    "confidence": confidence,
                    "ats_sender": cls["ats_sender"],
                    "subject": email.get("subject", ""),
                    "snippet": email.get("snippet", ""),
                    "sender": email.get("sender", ""),
                    "date": email.get("date", "")
                })
        elif classification == "job_alert":
            # Opportunities from alerts/recruiter digests — tracked even without an application.
            for oj in (email.get("extracted_jobs") or []):
                title = (oj.get("title") or "").strip()
                company = (oj.get("company") or "").strip()
                if not (title and company):
                    continue
                events.append({
                    "company": company,
                    "role": title,
                    "event_type": "opportunity",
                    "confidence": confidence,
                    "ats_sender": cls["ats_sender"],
                    "url": oj.get("url", ""),
                    "subject": email.get("subject", ""),
                    "snippet": email.get("snippet", ""),
                    "sender": email.get("sender", ""),
                    "date": email.get("date", "")
                })

    return events


if __name__ == "__main__":

    import sys
    import subprocess
    from playwright.sync_api import sync_playwright

    target = sys.argv[1] if len(sys.argv) > 1 else "https://www.linkedin.com/login"
    print(f"\n=======================================================")
    print(f"🌐 Launching Dedicated Browser Session for Login")
    print(f"Target URL: {target}")
    print(f"Profile:    {CHROME_PROFILE_DIR}")
    print(f"=======================================================")
    print("Log in to your accounts in the open window.")
    print("Press Ctrl+C in this terminal when you are done.\n")

    with sync_playwright() as p:
        context = get_persistent_browser_context(p, headless=False)
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(target, wait_until="domcontentloaded")
        page.bring_to_front()

        for app_name in ["Google Chrome for Testing", "Google Chrome", "Chromium"]:
            subprocess.run(
                ["osascript", "-e", f'tell application "{app_name}" to activate'], 
                check=False, 
                stdout=subprocess.DEVNULL, 
                stderr=subprocess.DEVNULL
            )

        try:
            page.wait_for_timeout(3600000)  # 1 hour
        except KeyboardInterrupt:
            print("\nClosing browser...")
        finally:
            context.close()
            print("✅ Session saved successfully in .chrome_profile!")
