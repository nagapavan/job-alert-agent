import pytest
import urllib.parse
from unittest.mock import patch, MagicMock

from backend.playwright_app import (
    get_persistent_browser_context,
    parse_email_classification,
    classify_email,
    scan_gmail_for_job_alerts,
    search_linkedin_talent_partners,
    generate_helper_panel_html,
    inject_helper_panel,
    scan_linkedin_job_recommendations,
    scan_and_sync_linkedin_saved_jobs
)

# -------------------------------------------------------------
# Unit Tests: Persistent Context Launcher
# -------------------------------------------------------------
def test_get_persistent_browser_context(tmp_path, monkeypatch):
    import backend.playwright_app as pw
    monkeypatch.setattr(pw, "CHROME_PROFILE_DIR", tmp_path)

    mock_p = MagicMock()
    mock_context = MagicMock()
    mock_p.chromium.launch_persistent_context.return_value = mock_context

    ctx = get_persistent_browser_context(mock_p, headless=True)
    assert ctx == mock_context
    called_args, called_kwargs = mock_p.chromium.launch_persistent_context.call_args
    assert called_kwargs["user_data_dir"] == str(tmp_path)
    assert called_kwargs["headless"] is True


def test_clean_stale_profile_locks_only_removes_dead_locks(tmp_path, monkeypatch):
    """Stale (dead-PID) locks are removed; a live owner is detected and left untouched."""
    import os
    import backend.playwright_app as pw

    monkeypatch.setattr(pw, "CHROME_PROFILE_DIR", tmp_path)
    lock = tmp_path / "SingletonLock"

    # Dead PID -> detected as stale and cleaned
    os.symlink("somehost-999999999", lock)
    assert pw.is_profile_in_use() is False
    assert pw.clean_stale_profile_locks() is True
    assert not lock.exists() and not lock.is_symlink()


def test_is_profile_in_use_detects_live_pid(tmp_path, monkeypatch):
    import os
    import backend.playwright_app as pw

    monkeypatch.setattr(pw, "CHROME_PROFILE_DIR", tmp_path)
    lock = tmp_path / "SingletonLock"

    # Live PID (our own) -> in use, and locks must NOT be removed
    os.symlink(f"host-{os.getpid()}", lock)
    assert pw.is_profile_in_use() is True
    assert pw.clean_stale_profile_locks() is False
    assert lock.is_symlink()


def test_get_persistent_browser_context_raises_when_profile_busy(tmp_path, monkeypatch):
    import os
    import pytest
    import backend.playwright_app as pw

    monkeypatch.setattr(pw, "CHROME_PROFILE_DIR", tmp_path)
    os.symlink(f"host-{os.getpid()}", tmp_path / "SingletonLock")

    mock_p = MagicMock()
    with pytest.raises(pw.BrowserProfileBusy):
        pw.get_persistent_browser_context(mock_p, headless=True)
    mock_p.chromium.launch_persistent_context.assert_not_called()

# -------------------------------------------------------------
# Unit Tests: Email Classifier & Gmail Scanner
# -------------------------------------------------------------
def test_parse_email_classification():
    # Rejection
    assert parse_email_classification("Update on your application", "Unfortunately, we are not moving forward.") == "rejection"
    assert parse_email_classification("Stripe Application", "We have decided to pursue other candidates.") == "rejection"
    
    # Interview
    assert parse_email_classification("Invitation to Interview", "Please schedule a time for a technical screen.") == "interview"
    assert parse_email_classification("Next steps with OpenAI", "We'd love to set up a phone screen.") == "interview"

    # Job Alert
    assert parse_email_classification("Daily Job Alert", "3 new jobs matching your profile at Google.") == "job_alert"

    # Offer
    assert parse_email_classification("Congratulations", "We are pleased to offer you the position.") == "offer"

    # Screening
    assert parse_email_classification("Your application", "We'd like to schedule an initial screening call.") == "screening"

    # General
    assert parse_email_classification("Welcome to the newsletter", "Weekly tech updates.") == "general"

def test_classify_email_confidence():
    """ATS senders and multiple phrase matches raise confidence; lone weak keywords stay low."""
    # Known ATS sender + explicit decision phrase -> high confidence
    high = classify_email("Your application", "Unfortunately, we are not moving forward.", "jobs@greenhouse.io")
    assert high["category"] == "rejection"
    assert high["confidence"] >= 0.75
    assert high["ats_sender"] is True

    # Lone weak keyword from an unknown sender -> below the auto-create threshold
    weak = classify_email("Update", "Unfortunately our store is closed.", "newsletter@shop.com")
    assert weak["category"] == "rejection"
    assert weak["confidence"] < 0.75

    # Unknown / generic email
    general = classify_email("Weekly digest", "Here are some articles.", "news@blog.com")
    assert general["category"] == "general"
    assert general["confidence"] == 0.0

def test_scan_gmail_for_job_alerts():
    mock_p = MagicMock()
    mock_context = MagicMock()
    mock_page = MagicMock()
    mock_p.chromium.launch_persistent_context.return_value = mock_context
    mock_context.new_page.return_value = mock_page

    # Mock DOM email row elements
    mock_row = MagicMock()
    mock_sender = MagicMock()
    mock_sender.inner_text.return_value = "Stripe Careers"
    mock_subject = MagicMock()
    mock_subject.inner_text.return_value = "Thank you for applying"
    mock_snippet = MagicMock()
    mock_snippet.inner_text.return_value = "Unfortunately, we won't be moving forward at this time."
    mock_date = MagicMock()
    mock_date.inner_text.return_value = "May 12"

    mock_row.query_selector.side_effect = lambda sel: {
        ".yP, .zF": mock_sender,
        ".bog": mock_subject,
        ".y2": mock_snippet,
        ".xW": mock_date
    }.get(sel)

    mock_page.query_selector_all.return_value = [mock_row]

    emails = scan_gmail_for_job_alerts(mock_p, headless=True)
    assert len(emails) == 1
    assert emails[0]["sender"] == "Stripe Careers"
    assert emails[0]["category"] == "rejection"
    assert "moving forward" in emails[0]["snippet"]

    # All-mail widening: the scan navigates straight to the global #search/ view
    # (mailbox-wide), not the inbox, and widens the window to 90 days.
    goto_url = mock_page.goto.call_args[0][0]
    assert "#search/" in goto_url
    decoded_query = urllib.parse.unquote_plus(goto_url.split("#search/", 1)[1])
    assert "newer_than:90d" in decoded_query
    assert "from:workday.com" in decoded_query
    assert "#inbox" not in goto_url

# -------------------------------------------------------------
# Unit Tests: LinkedIn DOM Search
# -------------------------------------------------------------
def test_search_linkedin_talent_partners():
    mock_p = MagicMock()
    mock_context = MagicMock()
    mock_page = MagicMock()
    mock_p.chromium.launch_persistent_context.return_value = mock_context
    mock_context.new_page.return_value = mock_page

    # Mock LinkedIn search result card
    mock_card = MagicMock()
    mock_name = MagicMock()
    mock_name.inner_text.return_value = "Sarah Connor\nView Profile"
    mock_headline = MagicMock()
    mock_headline.inner_text.return_value = "Senior Technical Recruiter at Datadog"
    mock_link = MagicMock()
    mock_link.get_attribute.return_value = "https://www.linkedin.com/in/sarah-connor?miniProfile="

    mock_card.query_selector.side_effect = lambda sel: {
        ".entity-result__title-text a, .app-aware-link": mock_name,
        ".entity-result__primary-subtitle": mock_headline,
        "a.app-aware-link": mock_link
    }.get(sel)

    mock_page.query_selector_all.return_value = [mock_card]

    contacts = search_linkedin_talent_partners(mock_p, "Datadog", "Backend Engineer", headless=True)
    assert len(contacts) == 1
    assert contacts[0]["name"] == "Sarah Connor"
    assert "Datadog" in contacts[0]["headline"]
    assert contacts[0]["profile_url"] == "https://www.linkedin.com/in/sarah-connor"

# -------------------------------------------------------------
# Unit Tests: Assistant Floating UI Overlay Panel Injection
# -------------------------------------------------------------
def test_generate_helper_panel_html():
    resume_data = {
        "name": "Jane Developer",
        "email": "jane@example.com",
        "phone": "+1-555-0100",
        "skills": ["Python", "FastAPI", "React"]
    }
    cover_letter = "Dear Hiring Team, I am excited to apply..."
    tailored_points = "* Highlight Python scalability"
    cold_msg = "Hi Recruiter, let's connect!"

    html = generate_helper_panel_html(resume_data, cover_letter, tailored_points, cold_msg)
    assert "Jane Developer" in html
    assert "jane@example.com" in html
    assert "Dear Hiring Team" in html
    assert "Hi Recruiter" in html
    assert "job-agent-panel" in html

def test_inject_helper_panel():
    mock_page = MagicMock()
    resume_data = {"name": "Alex", "email": "alex@test.com", "phone": "123"}
    inject_helper_panel(mock_page, resume_data, "CL", "Tips", "Msg")
    assert mock_page.evaluate.called
    called_script = mock_page.evaluate.call_args[0][0]
    assert "job-agent-panel" in called_script
    assert "runDeepAutoFill" in called_script

# -------------------------------------------------------------
# Unit Tests: LinkedIn Recommendations & Saved Jobs Sync
# -------------------------------------------------------------
def test_scan_linkedin_job_recommendations():
    mock_p = MagicMock()
    mock_context = MagicMock()
    mock_page = MagicMock()
    mock_p.chromium.launch_persistent_context.return_value = mock_context
    mock_context.new_page.return_value = mock_page

    mock_card = MagicMock()
    mock_title = MagicMock()
    mock_title.inner_text.return_value = "Staff AI Engineer"
    mock_title.get_attribute.return_value = "/jobs/view/987654321/"
    mock_card.inner_text.return_value = "Staff AI Engineer\nAnthropic · San Francisco, CA"

    mock_card.query_selector.side_effect = lambda sel: {
        ".job-card-list__title, a.job-card-container__link, .artdeco-entity-lockup__title a": mock_title,
    }.get(sel)

    mock_page.query_selector_all.return_value = [mock_card]

    recs = scan_linkedin_job_recommendations(mock_p, headless=True)
    assert len(recs) == 1
    assert recs[0]["title"] == "Staff AI Engineer"
    assert recs[0]["company"] == "Anthropic"
    assert "987654321" in recs[0]["url"]
    assert recs[0]["source_type"] == "LinkedIn"

def test_scan_and_sync_linkedin_saved_jobs_prunes_closed():
    mock_p = MagicMock()
    mock_context = MagicMock()
    mock_page = MagicMock()
    mock_p.chromium.launch_persistent_context.return_value = mock_context
    mock_context.new_page.return_value = mock_page
    mock_page.query_selector.return_value = None  # no pagination control

    # Anchor 1: Active job (container text provides title/company/location)
    anchor_active = MagicMock()
    anchor_active.get_attribute.return_value = "https://www.linkedin.com/jobs/view/111"
    anchor_active.evaluate.return_value = "Senior Python Developer\nStripe\nBengaluru, India\nApply"

    # Anchor 2: Closed job ("No longer accepting applications")
    anchor_closed = MagicMock()
    anchor_closed.get_attribute.return_value = "/jobs/view/222"
    anchor_closed.evaluate.return_value = "Staff Backend Engineer\nOpenAI\nRemote, India\nNo longer accepting applications"
    unsave_btn = MagicMock()
    handle = MagicMock()
    handle.as_element.return_value = unsave_btn
    anchor_closed.evaluate_handle.return_value = handle

    mock_page.query_selector_all.return_value = [anchor_active, anchor_closed]

    saved = scan_and_sync_linkedin_saved_jobs(mock_p, headless=True, auto_unsave_closed=True)
    assert len(saved) == 2
    assert saved[0]["title"] == "Senior Python Developer"
    assert saved[0]["company"] == "Stripe"
    assert saved[0]["is_closed"] is False
    assert saved[0]["unsaved"] is False

    assert saved[1]["title"] == "Staff Backend Engineer"
    assert saved[1]["is_closed"] is True
    assert saved[1]["unsaved"] is True
    assert unsave_btn.click.called


# -------------------------------------------------------------
# Regression: LinkedIn saved-job cards can collapse to a single line
# -------------------------------------------------------------
def test_parse_linkedin_card_multiline_strips_recency():
    from backend.playwright_app import _parse_linkedin_job_card_text
    res = _parse_linkedin_job_card_text("Staff Engineer\nAcme · Bengaluru, India\nPosted 2w ago")
    assert res == {"title": "Staff Engineer", "company": "Acme", "location": "Bengaluru, India"}


def test_parse_linkedin_card_collapsed_single_line_uses_title_hint():
    """A collapsed card must not fold the company/location/recency into the title."""
    from backend.playwright_app import _parse_linkedin_job_card_text
    res = _parse_linkedin_job_card_text(
        "Solutions Architect-3FedEx ACC · HyderabadReposted 1w ago",
        title_hint="Solutions Architect-3",
    )
    assert res["title"] == "Solutions Architect-3"
    assert res["company"] == "FedEx ACC"
    assert res["location"] == "Hyderabad"


def test_extract_linkedin_job_from_anchor_collapsed_card():
    """The /jobs/view/ anchor text recovers the title from a single-line card."""
    from backend.playwright_app import _extract_linkedin_job_from_anchor
    anchor = MagicMock()
    anchor.get_attribute.return_value = "/jobs/view/333"
    anchor.evaluate.return_value = "Lead Engineer – AI PlatformWaters · BengaluruReposted 3w ago"
    anchor.inner_text.return_value = "Lead Engineer – AI Platform"

    rec = _extract_linkedin_job_from_anchor(anchor)
    assert rec["title"] == "Lead Engineer – AI Platform"
    assert rec["company"] == "Waters"
    assert rec["location"] == "Bengaluru"
    assert rec["is_closed"] is False
    assert "333" in rec["url"]


def test_parse_linkedin_view_slug_splits_title_and_company_on_last_at():
    from backend.playwright_app import _parse_linkedin_view_slug

    assert _parse_linkedin_view_slug(
        "https://www.linkedin.com/jobs/view/staff-software-engineer-at-databricks-4416423111"
    ) == ("Staff Software Engineer", "Databricks")
    # "data-at-scale-engineer" must not fool the split: use the LAST "-at-".
    assert _parse_linkedin_view_slug(
        "https://www.linkedin.com/jobs/view/data-at-scale-engineer-at-acme-4416423999"
    ) == ("Data At Scale Engineer", "Acme")
    # A bare numeric id carries no slug.
    assert _parse_linkedin_view_slug("https://www.linkedin.com/jobs/view/4469681606/") == ("", "")


def test_extract_linkedin_job_from_anchor_reads_semantic_children():
    """The employer is read from its own element, never inferred from the role text."""
    from backend.playwright_app import _extract_linkedin_job_from_anchor

    anchor = MagicMock()
    anchor.get_attribute.return_value = "/jobs/view/4461133182/?refId=abc"

    def _evaluate(arg, *_):
        if "job-card-list__title" in arg:
            return {"title": "Staff Software Engineer", "company": "Databricks", "location": "Bengaluru"}
        return "Staff Software EngineerDatabricks\nBengaluru\nSave"

    anchor.evaluate.side_effect = _evaluate

    rec = _extract_linkedin_job_from_anchor(anchor)
    assert rec["title"] == "Staff Software Engineer"
    assert rec["company"] == "Databricks"
    assert rec["location"] == "Bengaluru"


def test_extract_linkedin_job_from_anchor_strips_employer_glued_to_title():
    """Slug carries the employer: a merged "TitleCompany" title must not duplicate it."""
    from backend.playwright_app import _extract_linkedin_job_from_anchor

    anchor = MagicMock()
    anchor.get_attribute.return_value = (
        "/jobs/view/principal-software-engineer-at-autodesk-4469681606/"
    )
    anchor.evaluate.return_value = (
        "Principal Software EngineerAutodesk\nBengaluru, Karnataka, India (On-site)\nSave"
    )
    anchor.inner_text.return_value = "Principal Software EngineerAutodesk"

    rec = _extract_linkedin_job_from_anchor(anchor)
    assert rec["company"] == "Autodesk"
    assert rec["title"] == "Principal Software Engineer"


def test_extract_linkedin_job_never_mistakes_location_for_company():
    """Merged title + numeric-only URL: the location row must not become the employer."""
    from backend.playwright_app import _extract_linkedin_job_from_anchor

    anchor = MagicMock()
    anchor.get_attribute.return_value = "/jobs/view/4469681606/"
    anchor.evaluate.return_value = (
        "Principal Software Engineer\nBengaluru, Karnataka, India (On-site)\nSave"
    )
    anchor.inner_text.return_value = "Principal Software Engineer"

    rec = _extract_linkedin_job_from_anchor(anchor)
    assert rec["title"] == "Principal Software Engineer"
    assert rec["company"] == "Unknown Company"
    assert "Bengaluru" in rec["location"]


def test_fetch_linkedin_job_details_reads_about_the_job_and_topcard():
    from backend.playwright_app import fetch_linkedin_job_details

    page = MagicMock()
    page.evaluate.return_value = {
        "description": "About the job\nWe build distributed systems in Go.",
        "title": "Staff Software Engineer",
        "company": "Databricks",
        "canonical_url": "https://www.linkedin.com/jobs/view/staff-software-engineer-at-databricks-4461133182/",
    }

    details = fetch_linkedin_job_details(page, "https://www.linkedin.com/jobs/view/4461133182/")
    assert details["description"].startswith("About the job")
    assert details["title"] == "Staff Software Engineer"
    assert details["company"] == "Databricks"
    assert "staff-software-engineer-at-databricks" in details["canonical_url"]
    page.goto.assert_called_once()


def test_fetch_linkedin_job_details_returns_empty_on_non_job_url():
    from backend.playwright_app import fetch_linkedin_job_details

    page = MagicMock()
    assert fetch_linkedin_job_details(page, "https://www.linkedin.com/feed/") == {
        "description": "", "title": "", "company": "", "canonical_url": "",
    }
    page.goto.assert_not_called()


def test_fetch_linkedin_job_details_degrades_on_navigation_error():
    from backend.playwright_app import fetch_linkedin_job_details

    page = MagicMock()
    page.goto.side_effect = RuntimeError("net::ERR_TIMED_OUT")
    assert fetch_linkedin_job_details(page, "https://www.linkedin.com/jobs/view/123/")["description"] == ""





