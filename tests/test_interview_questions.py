"""
Tests for I2 structured interview coaching:
`generate_interview_questions` (ai_helper) and
`POST /api/jobs/{job_id}/interview-questions` (main).
"""

from unittest.mock import patch

from conftest import test_client as client
from backend.database import Job, Company, Resume
from backend.config import encrypt_data


def _make_job(db_session):
    comp = Company(name="Acme Corp", domain="acme.com")
    db_session.add(comp)
    db_session.commit()
    db_session.refresh(comp)
    job = Job(
        company_id=comp.id,
        title="Senior Backend Engineer",
        description="Build distributed systems with Python and Kubernetes.",
        url="https://acme.example/jobs/1",
        source="Manual",
        status="To Apply",
    )
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)
    return comp, job


def _add_resume(db_session, text="Alice — Python engineer."):
    db_session.add(Resume(filename="resume.md", content_encrypted=encrypt_data(text), is_active=True))
    db_session.commit()


@patch("backend.main.generate_interview_questions")
def test_interview_questions_endpoint_success(mock_gen, db_session):
    comp, job = _make_job(db_session)
    _add_resume(db_session)
    mock_gen.return_value = {
        "questions": [
            {
                "question": "Tell me about a distributed system you built.",
                "category": "behavioral",
                "star_situation": "Led a payments service migration.",
                "star_action": "Designed the sharding and failover strategy.",
                "star_result": "Cut p99 latency by 40%.",
                "suggested_answer": "I led the migration by ...",
            }
        ]
    }

    res = client.post(f"/api/jobs/{job.id}/interview-questions", json={"num_questions": 3})

    assert res.status_code == 200
    data = res.json()
    assert data["job_id"] == job.id
    assert data["title"] == "Senior Backend Engineer"
    assert data["company"] == "Acme Corp"
    assert len(data["questions"]) == 1
    assert data["questions"][0]["question"].startswith("Tell me")
    assert data["questions"][0]["star_result"] == "Cut p99 latency by 40%."

    kwargs = mock_gen.call_args.kwargs
    assert kwargs["num_questions"] == 3
    assert "Alice" in kwargs["resume_text"]
    assert kwargs["company_name"] == "Acme Corp"


@patch("backend.main.generate_interview_questions")
def test_interview_questions_defaults_to_six(mock_gen, db_session):
    comp, job = _make_job(db_session)
    _add_resume(db_session)
    mock_gen.return_value = {"questions": []}

    res = client.post(f"/api/jobs/{job.id}/interview-questions")

    assert res.status_code == 200
    assert mock_gen.call_args.kwargs["num_questions"] == 6


@patch("backend.main.generate_interview_questions")
def test_interview_questions_requires_active_resume(mock_gen, db_session):
    comp, job = _make_job(db_session)

    res = client.post(f"/api/jobs/{job.id}/interview-questions", json={"num_questions": 3})

    assert res.status_code == 400
    assert "resume" in res.json()["detail"].lower()
    mock_gen.assert_not_called()


@patch("backend.main.generate_interview_questions")
def test_interview_questions_surfaces_generation_error(mock_gen, db_session):
    comp, job = _make_job(db_session)
    _add_resume(db_session)
    mock_gen.return_value = {"questions": [], "error": "AI unavailable"}

    res = client.post(f"/api/jobs/{job.id}/interview-questions", json={"num_questions": 3})

    assert res.status_code == 502
    assert "AI unavailable" in res.json()["detail"]


def test_interview_questions_job_not_found(db_session):
    res = client.post("/api/jobs/999999/interview-questions", json={"num_questions": 3})
    assert res.status_code == 404
