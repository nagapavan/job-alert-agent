import json
import pytest
from unittest.mock import patch
from sqlalchemy.orm import Session

from backend.database import Company, Resume, Job, ApplicationEvent
from backend.config import encrypt_data
from backend.chat_agent import CareerChatAgent
from conftest import test_client as client, TestingSessionLocal

@pytest.fixture
def chat_seed_db():
    db = TestingSessionLocal()
    # Clean tables
    db.query(ApplicationEvent).delete()
    db.query(Job).delete()
    db.query(Company).delete()
    db.query(Resume).delete()

    # Seed Company
    comp = Company(
        name="Databricks",
        domain="databricks.com",
        careers_url="https://boards.greenhouse.io/databricks",
        recent_news="Launched Apache Spark 4.0 and AI gateway.",
        hiring_process="1. Recruiter Call -> 2. Distributed Systems Design -> 3. Coding & Values."
    )
    db.add(comp)
    db.commit()
    db.refresh(comp)

    # Seed Resume
    parsed_json = json.dumps({
        "full_name": "Jane Developer",
        "total_experience": "9+ Years",
        "skills": ["Python", "Distributed Systems", "Kubernetes", "FastAPI", "PostgreSQL", "Go"]
    })
    resume = Resume(
        filename="resume.pdf",
        content_encrypted=encrypt_data("Jane Developer - Staff Engineer with 9+ years experience in Distributed Systems, Python, and cloud infrastructure."),
        parsed_json_encrypted=encrypt_data(parsed_json),
        is_active=True
    )
    db.add(resume)
    db.commit()

    # Seed Job
    job = Job(
        company_id=comp.id,
        title="Staff Software Engineer, Distributed Systems",
        description="We are looking for a Staff Engineer to lead high-throughput streaming systems.",
        url="https://boards.greenhouse.io/databricks/jobs/12345",
        location="Bengaluru, India",
        source="Greenhouse",
        source_type="Direct",
        status="To Apply",
        match_score=88.0
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    yield db, comp, job

    db.close()

@patch("backend.chat_agent.generate_text")
def test_chat_agent_search_jobs_intent(mock_gen_text, chat_seed_db):
    db, comp, job = chat_seed_db
    mock_gen_text.return_value = "Here are your top matching opportunities including Databricks."

    agent = CareerChatAgent(db)
    res = agent.process_message("Show my top matches for distributed systems")

    assert res["reply"] is not None
    assert len(res["embedded_jobs"]) >= 1
    assert res["embedded_jobs"][0]["company"] == "Databricks"
    assert any(a["type"] == "job_search" for a in res["actions_taken"])

@patch("backend.chat_agent.generate_text")
def test_chat_agent_status_update_intent(mock_gen_text, chat_seed_db):
    db, comp, job = chat_seed_db
    mock_gen_text.return_value = "I've shortlisted the Databricks Staff Engineer role for you."

    agent = CareerChatAgent(db)
    res = agent.process_message(f"Shortlist job {job.id}")

    assert res["reply"] is not None
    assert any(a["type"] == "status_update" for a in res["actions_taken"])
    
    # Verify DB was updated
    updated_job = db.query(Job).filter(Job.id == job.id).first()
    assert updated_job.status == "Shortlisted"

@patch("backend.chat_agent.generate_consolidated_application_package")
@patch("backend.chat_agent.generate_text")
def test_chat_agent_material_generation_intent(mock_gen_text, mock_pkg, chat_seed_db):
    db, comp, job = chat_seed_db
    mock_pkg.return_value = {
        "cover_letter": "Dear Databricks Hiring Team,\nI am excited to apply...",
        "tailored_resume_points": "- Optimized distributed stream processing by 40%.",
        "cold_message": "Hi, I noticed the Staff Engineer opening and would love to connect!",
        "match_score": 92.0
    }
    mock_gen_text.return_value = "I have drafted a tailored cover letter and resume bullet points for Databricks."

    agent = CareerChatAgent(db)
    res = agent.process_message(f"Tailor resume for Databricks", job_id=job.id)

    assert res["reply"] is not None
    assert any(a["type"] == "material_generation" for a in res["actions_taken"])
    
    # Verify job record has drafts
    db.refresh(job)
    assert job.cover_letter_draft is not None
    assert "Databricks" in job.cover_letter_draft
    assert job.tailored_resume_points is not None

@patch("backend.chat_agent.generate_text")
def test_chat_agent_interview_prep(mock_gen_text, chat_seed_db):
    db, comp, job = chat_seed_db
    mock_gen_text.return_value = "### System Design Mock Question\n**Question**: How would you design a distributed log replication service?\n**STAR Framework Advice**:\n- **Situation**: Mention your Kubernetes clustering experience."

    agent = CareerChatAgent(db)
    res = agent.process_message("How should I prepare for the Databricks system design interview?", job_id=job.id)

    assert "System Design Mock Question" in res["reply"]
    assert mock_gen_text.called

@patch("backend.chat_agent.generate_text")
def test_api_chat_assistant_endpoint(mock_gen_text, chat_seed_db):
    db, comp, job = chat_seed_db
    mock_gen_text.return_value = "Hello! I am your AI Career Copilot. How can I help with your job search today?"

    payload = {
        "message": "What is the status of my pipeline?",
        "history": [
            {"role": "user", "content": "Hi copilot"},
            {"role": "assistant", "content": "Hello! How can I assist you?"}
        ],
        "job_id": job.id
    }

    res = client.post("/api/chat/assistant", json=payload)
    assert res.status_code == 200
    data = res.json()
    assert "reply" in data
    assert "actions_taken" in data
    assert "embedded_jobs" in data
    assert data["reply"] == "Hello! I am your AI Career Copilot. How can I help with your job search today?"


def test_chat_agent_gmail_inquiry_offline_fallback(chat_seed_db):
    db, comp, job = chat_seed_db
    agent = CareerChatAgent(db)
    
    # When AI is offline, process_message falls back to offline reply
    with patch("backend.chat_agent.generate_text", side_effect=RuntimeError("AI offline")):
        res = agent.process_message("Rescan gmail now")
        assert "Gmail Inbox Reading Is Opt-In" in res["reply"]
        assert "LinkedIn Sync" in res["reply"]
        assert "Google Jobs" in res["reply"]


@patch("backend.chat_agent.generate_text")
def test_bare_offer_noun_does_not_mutate_status(mock_gen_text, chat_seed_db):
    """A question that merely contains the word 'offer' must not change any job's status."""
    db, comp, job = chat_seed_db
    mock_gen_text.return_value = "Databricks does offer visa sponsorship for this role."

    agent = CareerChatAgent(db)
    res = agent.process_message("Does Databricks offer sponsorship?")

    assert not any(a["type"] == "status_update" for a in res["actions_taken"])
    db.refresh(job)
    assert job.status == "To Apply"


@patch("backend.chat_agent.generate_text")
def test_explicit_offer_command_updates_status(mock_gen_text, chat_seed_db):
    """An explicit mutation command with a job id still works."""
    db, comp, job = chat_seed_db
    mock_gen_text.return_value = "Marked as Offered."

    agent = CareerChatAgent(db)
    res = agent.process_message(f"Mark job {job.id} as offered")

    assert any(a["type"] == "status_update" for a in res["actions_taken"])
    db.refresh(job)
    assert job.status == "Offered"


@patch("backend.chat_agent.generate_text")
def test_ambiguous_status_target_does_not_mutate(mock_gen_text, chat_seed_db):
    """When several jobs equally match, the copilot must ask instead of guessing."""
    db, comp, job = chat_seed_db
    job2 = Job(
        company_id=comp.id,
        title="Senior Solutions Architect",
        description="Design distributed platforms.",
        url="https://boards.greenhouse.io/databricks/jobs/99999",
        location="Remote",
        source="Greenhouse",
        source_type="Direct",
        status="To Apply",
        match_score=80.0
    )
    db.add(job2)
    db.commit()

    mock_gen_text.return_value = "Which role did you mean?"

    agent = CareerChatAgent(db)
    res = agent.process_message("Mark Databricks as rejected")

    assert not any(a["type"] == "status_update" for a in res["actions_taken"])
    assert any(a["type"] == "status_update_ambiguous" for a in res["actions_taken"])
    db.refresh(job)
    assert job.status == "To Apply"

