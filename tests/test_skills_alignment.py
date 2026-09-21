import pytest
from unittest.mock import patch
from fastapi.testclient import TestClient
from backend.main import app
from backend.ai_helper import extract_job_skills_and_alignment
from backend.database import Job, Company, Resume, encrypt_data
import json

client = TestClient(app)

def test_extract_job_skills_and_alignment_comprehensive():
    job_desc = """
    We are seeking a Senior Backend Engineer to build high-throughput distributed microservices.
    Requirements:
    - 5+ years of experience with Python, FastAPI, and PostgreSQL
    - Hands-on experience with Docker, Kubernetes, and AWS (ECS/EKS)
    - Experience with Kafka, Redis, and IBM DB2 is a plus
    - Strong understanding of System Design and CI/CD
    """
    candidate_skills = [
        "Python", "FastAPI", "PostgreSQL", "Docker", "AWS", "Kubernetes", "Redis", "Distributed Systems"
    ]

    result = extract_job_skills_and_alignment(job_desc, candidate_skills)

    assert result["total_stack_count"] > 0
    assert result["compatibility_pct"] >= 50
    assert result["matched_count"] >= 5

    matched_names = [s["name"] for s in result["matched_skills"]]
    assert "Python" in matched_names
    assert "FastAPI" in matched_names
    assert "PostgreSQL" in matched_names
    assert "Kubernetes" in matched_names

    gap_names = [s["name"] for s in result["gap_skills"]]
    assert "IBM DB2" in gap_names
    assert "Kafka" in gap_names
    assert result["confidence"] in ("high", "medium")

def test_extract_job_skills_and_alignment_empty():
    res = extract_job_skills_and_alignment("", [])
    assert res["compatibility_pct"] is None
    assert res["total_stack_count"] == 0
    assert len(res["matched_skills"]) == 0
    assert res["confidence"] == "low"

def test_api_get_job_skills_alignment(db_session):
    comp = Company(name="Acme Tech", domain="acme.com")
    db_session.add(comp)
    db_session.flush()

    job = Job(
        company_id=comp.id,
        title="Senior Python Backend Engineer",
        description="Looking for an engineer skilled in Python, FastAPI, AWS, and Snowflake.",
        url="https://acme.com/jobs/1",
        status="To Apply"
    )
    db_session.add(job)

    # Add active resume
    resume_payload = {
        "name": "Alex Candidate",
        "skills": ["Python", "FastAPI", "AWS", "Docker", "PostgreSQL"]
    }
    resume = Resume(
        filename="Alex_Resume.pdf",
        content_encrypted=encrypt_data("Alex Candidate Resume Content"),
        is_active=True,
        parsed_json_encrypted=encrypt_data(json.dumps(resume_payload))
    )
    db_session.add(resume)
    db_session.commit()

    response = client.get(f"/api/jobs/{job.id}/skills-alignment")
    assert response.status_code == 200
    data = response.json()
    assert data["job_id"] == job.id
    assert data["compatibility_pct"] >= 70
    assert data["confidence"] in ("high", "medium")
    matched = [s["name"] for s in data["matched_skills"]]
    assert "Python" in matched
    assert "FastAPI" in matched
    assert "AWS" in matched
    gaps = [s["name"] for s in data["gap_skills"]]
    assert "Snowflake" in gaps

@patch("backend.main.find_talent_partners", return_value=[])
def test_api_get_job_hiring_team(mock_partners, db_session):
    comp = Company(name="Nexus Systems", domain="nexus.com")
    db_session.add(comp)
    db_session.flush()

    job = Job(
        company_id=comp.id,
        title="Principal Infrastructure Engineer",
        description="Lead our distributed platform.",
        url="https://nexus.com/jobs/2",
        status="To Apply"
    )
    db_session.add(job)
    db_session.commit()

    response = client.get(f"/api/jobs/{job.id}/hiring-team")
    assert response.status_code == 200
    data = response.json()
    assert data["job_id"] == job.id
    assert data["company_name"] == "Nexus Systems"
    # Always includes a general company talent-directory contact.
    assert data["contacts_count"] >= 1
    directory_links = [
        c for c in data["contacts"] if "Nexus Systems" in c["author_name"]
    ]
    assert directory_links
