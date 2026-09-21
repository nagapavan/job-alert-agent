import io
import json
import pytest
from unittest.mock import patch, MagicMock

from conftest import test_client as client

@patch("backend.main.launch_assisted_application_session")
@patch("backend.main.generate_text")
@patch("backend.main.gather_company_intelligence")
@patch("backend.main.scrape_greenhouse_jobs")
def test_full_application_lifecycle_e2e(mock_gh, mock_intel, mock_gen, mock_launch):
    """
    End-to-End Test covering the full user flow:
    1. Resume Upload & ATS Parsing
    2. Target Company Configuration
    3. Intelligence Gathering & Board Scraping
    4. Due Diligence & Match Scoring
    5. Assisted Apply Trigger
    6. Post-Application RAG Chat Coaching
    7. Application Status Transitions & Event History Audit
    """
    # Mocks
    mock_intel.return_value = {
        "description": "Leading global financial infrastructure platform.",
        "recent_news": "Announced support for instant global payouts.",
        "salary_insights": "$180k - $240k Base",
        "hiring_process": "1. Recruiter Screen -> 2. Technical Coding -> 3. System Architecture"
    }
    mock_gh.return_value = [
        {
            "title": "Principal Distributed Systems Engineer",
            "url": "https://boards.greenhouse.io/stripe/jobs/4242",
            "description": "Architect high-throughput payment ledger pipelines.",
            "location": "Remote / San Francisco",
            "source": "Greenhouse Board",
            "source_type": "Direct"
        }
    ]
    mock_gen.return_value = json.dumps({
        "name": "Jane Developer",
        "email": "jane@example.com",
        "phone": "+1 555-0199",
        "summary": "Distributed systems architect with 8 years experience.",
        "skills": ["Python", "FastAPI", "Distributed Systems", "PostgreSQL", "Kafka"],
        "match_score": 92,
        "strengths": ["Direct ledger scaling expertise", "Strong Python proficiency"],
        "gaps": ["None identified"],
        "feedback": "Outstanding match. Emphasize transaction throughput numbers."
    })

    # Step 1: Upload Candidate Resume
    resume_content = "Jane Developer\njane@example.com\nSkills: Python, FastAPI, Distributed Systems, Kafka"
    upload_res = client.post("/api/resume/upload", files={
        "file": ("jane_resume.txt", io.BytesIO(resume_content.encode("utf-8")), "text/plain")
    })
    assert upload_res.status_code == 201
    resume_data = upload_res.json()
    assert resume_data["filename"] == "jane_resume.txt"

    # Step 2: Add Target Company
    comp_res = client.post("/api/companies", json={
        "name": "Stripe",
        "domain": "stripe.com",
        "careers_url": "https://stripe.com/jobs"
    })
    assert comp_res.status_code == 201
    comp_id = comp_res.json()["id"]

    # Step 3: Trigger Career Scraping & Intelligence Gathering
    scrape_res = client.post("/api/jobs/scrape", json={"company_id": comp_id})
    assert scrape_res.status_code == 200
    scrape_data = scrape_res.json()
    assert scrape_data["jobs_added"] == 1

    # Step 4: Verify Scraped Job & AI Match Analysis
    jobs = client.get(f"/api/jobs?company_id={comp_id}").json()
    assert len(jobs) == 1
    job = jobs[0]
    job_id = job["id"]
    assert job["title"] == "Principal Distributed Systems Engineer"
    assert job["status"] == "To Apply"
    assert job["is_ghost_job"] is False

    # Step 5: Trigger Assisted Apply via Browser Profile
    apply_res = client.post(f"/api/jobs/{job_id}/apply")
    assert apply_res.status_code == 200
    assert apply_res.json()["status"] == "Applied"

    # Step 6: Interview Prep RAG Chat
    mock_gen.return_value = "Highlight your experience with two-phase commit protocols and idempotency keys."
    chat_res = client.post(f"/api/jobs/{job_id}/chat", json={
        "message": "How do I best demonstrate ledger consistency in the interview?"
    })
    assert chat_res.status_code == 200
    assert "two-phase commit" in chat_res.json()["reply"]

    # Step 7: Application Status Lifecycle & Audit Log
    # Move to 'Applied'
    client.patch(f"/api/jobs/{job_id}/status", json={"status": "Applied", "note": "Submitted via company portal"})
    job_applied = client.get(f"/api/jobs/{job_id}").json()
    assert job_applied["status"] == "Applied"
    assert job_applied["applied_at"] is not None

    # Move to 'Interview'
    client.patch(f"/api/jobs/{job_id}/status", json={"status": "Interview", "note": "Recruiter screen scheduled"})
    job_interview = client.get(f"/api/jobs/{job_id}").json()
    assert job_interview["status"] == "Interview"
    assert job_interview["interview_scheduled_at"] is not None

    # Verify event timeline
    events = client.get(f"/api/jobs/{job_id}/events").json()
    assert len(events) >= 2  # Applied event + Interview event

