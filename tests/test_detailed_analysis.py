"""
Tests for the on-demand detailed match analysis endpoint.
"""

import pytest
from backend.database import Job, Company, Resume
from backend.config import encrypt_data
from conftest import test_client


@pytest.fixture
def setup_job_with_resume(db_session):
    """Create a test job and active resume."""
    company = Company(name="TechCorp", domain="techcorp.com")
    db_session.add(company)
    db_session.flush()

    job = Job(
        company_id=company.id,
        title="Senior Python Engineer",
        description="We seek a Senior Python Engineer with 5+ years of experience. "
                   "Must know FastAPI, SQLAlchemy, PostgreSQL. Nice-to-have: Playwright automation.",
        url="https://techcorp.com/jobs/1",
        location="Remote",
        source="test",
        status="To Apply"
    )
    db_session.add(job)
    db_session.flush()

    resume_text = """
    John Doe
    Senior Software Engineer
    
    EXPERIENCE
    - 6 years Python backend development
    - FastAPI, SQLAlchemy, PostgreSQL
    - Playwright automation for testing
    
    SKILLS
    Python, FastAPI, SQLAlchemy, PostgreSQL, Playwright
    """

    resume = Resume(
        filename="test_resume.txt",
        content_encrypted=encrypt_data(resume_text),
        is_active=True
    )
    db_session.add(resume)
    db_session.commit()

    return job, resume


def test_detailed_analysis_cached(db_session, setup_job_with_resume):
    """Test that cached analysis is returned immediately."""
    job, resume = setup_job_with_resume

    # Pre-cache analysis
    cached_analysis = "This is a cached detailed analysis."
    job.match_analysis = cached_analysis
    db_session.commit()

    resp = test_client.post(f"/api/match/{job.id}/detailed-analysis")
    assert resp.status_code == 200
    data = resp.json()
    assert data["cached"] is True
    assert data["analysis"] == cached_analysis
    assert data["job_id"] == job.id


def test_detailed_analysis_not_found():
    """Test that missing job returns error."""
    resp = test_client.post("/api/match/99999/detailed-analysis")
    assert resp.status_code == 200
    data = resp.json()
    assert "error" in data
    assert data["analysis"] is None


def test_detailed_analysis_no_resume(db_session):
    """Test that missing active resume returns error."""
    company = Company(name="NoResumeCorp", domain="noresume.com")
    db_session.add(company)
    db_session.flush()

    job = Job(
        company_id=company.id,
        title="Python Engineer",
        description="Senior Python Engineer with 5+ years",
        url="https://noresume.com/jobs/1",
        location="Remote",
        source="test",
        status="To Apply"
    )
    db_session.add(job)
    db_session.commit()

    resp = test_client.post(f"/api/match/{job.id}/detailed-analysis")
    assert resp.status_code == 200
    data = resp.json()
    assert "error" in data
    assert data["analysis"] is None


def test_match_analyze_response_includes_job_id(db_session):
    """Test that /api/match/analyze response includes optional job_id field."""
    resume_text = """
    John Doe
    Senior Software Engineer
    6 years Python experience
    FastAPI, SQLAlchemy, PostgreSQL
    """

    resume = Resume(
        filename="test_resume.txt",
        content_encrypted=encrypt_data(resume_text),
        is_active=True
    )
    db_session.add(resume)
    db_session.commit()

    resp = test_client.post(
        "/api/match/analyze",
        json={
            "job_title": "Python Engineer",
            "company": "TechCorp",
            "job_description": "Seeking Senior Python Engineer with 5+ years FastAPI and PostgreSQL experience",
            "resume_text": None
        }
    )

    assert resp.status_code == 200
    data = resp.json()
    assert "job_id" in data
    assert data["job_id"] is None  # Ad-hoc analysis has no job_id
    assert "analysis_source" in data
    assert data["analysis_source"] in ["classifier", "llm", "error"]
