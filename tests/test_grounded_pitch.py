import json
import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

from backend.main import app
from backend.database import ApplicationQuestionAnswer, CustomFormField, Resume, Company
from backend.config import encrypt_data
from backend.ai_helper import (
    generate_grounded_anti_slop_pitch,
    generate_embeddings,
    BANNED_AI_SLOP_PHRASES
)

client = TestClient(app)

# -------------------------------------------------------------
# 1. Unit Tests: Grounded Anti-Slop Pitch Generator
# -------------------------------------------------------------

def test_banned_ai_slop_phrases_list():
    """Verifies that the anti-slop dictionary contains key cliché patterns."""
    assert len(BANNED_AI_SLOP_PHRASES) >= 15
    assert any("thrilled to apply" in phrase for phrase in BANNED_AI_SLOP_PHRASES)
    assert any("testament to" in phrase for phrase in BANNED_AI_SLOP_PHRASES)
    assert any("fast-paced" in phrase for phrase in BANNED_AI_SLOP_PHRASES)


@patch("backend.ai_helper.generate_text")
@patch("backend.ai_helper.gather_company_intelligence")
def test_generate_grounded_anti_slop_pitch_success(mock_intel, mock_gen, db_session):
    """Verifies generation of authentic, 3-5 sentence answer with company & candidate facts."""
    mock_intel.return_value = {
        "description": "Markin develops automated distributed ledger reconciliation software.",
        "recent_news": "Launched real-time high-throughput settlement engine in 2026.",
        "salary_insights": "$180k - $210k base",
        "hiring_process": "Recruiter screen, System Design, Coding"
    }
    
    clean_pitch = (
        "Markin's real-time settlement engine addresses one of the hardest scalability bottlenecks in fintech. "
        "Over the past 8 years, I have engineered high-concurrency backend services in Python and Go, recently scaling a Kafka payment pipeline to 40,000 RPS. "
        "I have led zero-downtime PostgreSQL schema migrations and containerized Kubernetes deployments under high load. "
        "I look forward to bringing this distributed reliability experience to Markin's platform engineering team."
    )
    mock_gen.return_value = clean_pitch

    # Active Resume in DB
    resume_payload = {
        "name": "Alex Mercer",
        "skills": ["Python", "Go", "Kafka", "PostgreSQL", "Kubernetes"],
        "experience": [
            {
                "company": "Fintech Corp",
                "title": "Senior Staff Engineer",
                "description": "Scaled payment engine to 40k RPS with zero downtime."
            }
        ],
        "summary": "Distributed backend systems architect."
    }
    db_session.add(Resume(
        filename="alex_resume.pdf",
        content_encrypted=encrypt_data("Alex Mercer. Senior Staff Engineer with Python, Go, Kafka, PostgreSQL."),
        parsed_json_encrypted=encrypt_data(json.dumps(resume_payload)),
        is_active=True
    ))
    db_session.commit()

    result = generate_grounded_anti_slop_pitch(
        question="Why are you interested in joining Markin as a Staff Backend Engineer?",
        company_name="Markin",
        job_title="Staff Backend Engineer",
        job_description="We are seeking a Staff Backend Engineer to scale our real-time ledger systems.",
        db=db_session,
        auto_cache=True
    )

    assert result is not None
    assert result["is_cache_hit"] is False
    assert "Markin" in result["answer"]
    assert "40,000 RPS" in result["answer"]
    
    # Assert sentence count constraint (3 to 5 sentences)
    sentences = [s.strip() for s in result["answer"].split(".") if s.strip()]
    assert 3 <= len(sentences) <= 5

    # Assert no banned phrases
    for banned in BANNED_AI_SLOP_PHRASES:
        assert banned.lower() not in result["answer"].lower()

    # Verify auto-cached in memory bank
    cached = db_session.query(ApplicationQuestionAnswer).filter(
        ApplicationQuestionAnswer.question_text.contains("Why are you interested in joining Markin")
    ).first()
    assert cached is not None
    assert cached.category in ["company_interest", "fit"]


# -------------------------------------------------------------
# 2. API Contract Tests: Grounded Pitch & Telemetry
# -------------------------------------------------------------

@patch("backend.ai_helper.generate_text")
@patch("backend.ai_helper.gather_company_intelligence")
def test_api_grounded_pitch_endpoint(mock_intel, mock_gen, db_session):
    """Verifies POST /api/qa/grounded-pitch endpoint."""
    mock_intel.return_value = {"description": "Datadog observability platform."}
    mock_gen.return_value = (
        "Datadog's distributed tracing infrastructure sets the industry standard for cloud observability. "
        "In my previous role, I optimized APM telemetry pipelines processing 2TB daily log volume using Rust and Python. "
        "I look forward to contributing to Datadog's high-throughput ingestion engine."
    )

    db_session.add(Resume(
        filename="candidate.pdf",
        content_encrypted=encrypt_data("Senior engineer with observability and telemetry background."),
        is_active=True
    ))
    db_session.commit()

    payload = {
        "question_text": "Why do you want to work at Datadog?",
        "company_name": "Datadog",
        "job_title": "Senior Infrastructure Engineer",
        "job_description": "Work on high-throughput tracing pipelines.",
        "auto_cache": True
    }

    resp = client.post("/api/qa/grounded-pitch", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert "answer" in data
    assert "Datadog" in data["answer"]
    assert data["category"] in ["company_interest", "general", "fit"]


def test_api_forms_telemetry_lifecycle(db_session):
    """Verifies POST and GET /api/forms/telemetry for custom form field tracking."""
    payload = {
        "domain": "careers.markin.com",
        "ats_type": "custom",
        "url": "https://careers.markin.com/jobs/123",
        "fields": [
            {
                "field_name": "why_us_essay",
                "field_id": "field-why-markin",
                "field_label": "WHY ARE YOU A GOOD FIT FOR MARKIN?",
                "field_type": "textarea",
                "is_recognized": False,
                "suggested_category": "why_us"
            },
            {
                "field_name": "portfolio_link",
                "field_id": "field-portfolio",
                "field_label": "LINKEDIN OR PORTFOLIO",
                "field_type": "text",
                "is_recognized": True,
                "suggested_category": "portfolio_link"
            }
        ]
    }

    # 1. Ingest telemetry batch
    resp = client.post("/api/forms/telemetry", json=payload)
    assert resp.status_code == 200
    res_data = resp.json()
    assert res_data["status"] == "ok"
    assert res_data["processed_count"] == 2

    # 2. Query telemetry report
    get_resp = client.get("/api/forms/telemetry?domain=careers.markin.com")
    assert get_resp.status_code == 200
    telemetry_data = get_resp.json()
    assert len(telemetry_data) >= 2
    
    unique_field = next(f for f in telemetry_data if f["field_id"] == "field-why-markin")
    assert unique_field["domain"] == "careers.markin.com"
    assert unique_field["ats_type"] == "custom"
    assert unique_field["field_type"] == "textarea"
    assert unique_field["occurrence_count"] >= 1


@patch("backend.ai_helper.generate_structured")
def test_llm_judge_catches_hallucinated_employer_and_metrics(mock_structured):
    """Verifies that the LLM Judge detects ungrounded claims and returns corrected text."""
    from backend.ai_helper import verify_and_judge_grounded_pitch, PitchVerificationResult

    # Mock judge returning a correction for a hallucinated company
    mock_structured.return_value = PitchVerificationResult(
        is_grounded=False,
        hallucinated_entities=["Splunk", "Cisco", "petabyte data daily"],
        critique="The pitch claims past employment at Splunk/Cisco and petabyte metrics not found in candidate resume.",
        corrected_pitch="YipitData's mission to transform raw data into actionable insights aligns with my background in high-concurrency backend architecture. Over the past several years, I have engineered scalable distributed pipelines in Python and Go, optimizing data throughput with high uptime. I look forward to contributing this reliable systems experience to YipitData's platform engineering team."
    )

    hallucinated_draft = (
        "YipitData's mission aligns with my expertise. As Principal Engineer at Splunk/Cisco, "
        "I led petabyte fraud detection processing for millions of users."
    )

    verified = verify_and_judge_grounded_pitch(
        pitch_text=hallucinated_draft,
        candidate_employers=["Fintech Corp", "Foundational Labs"],
        candidate_titles=["Senior Staff Engineer", "Backend Architect"],
        candidate_skills=["Python", "Go", "Distributed Systems", "Kafka"],
        candidate_highlights="Scaled payment engine to 40k RPS with zero downtime.",
        target_company="YipitData",
        job_title="Senior Staff Software Engineer"
    )

    assert "Splunk" not in verified
    assert "Cisco" not in verified
    assert "petabyte" not in verified
    assert "YipitData" in verified
    assert "distributed pipelines" in verified


@patch("backend.ai_helper.generate_structured")
def test_llm_judge_rejects_correction_that_drops_target_company(mock_structured):
    """Regression: the pitch judge must not accept a 'correction' that strips the target company."""
    from backend.ai_helper import verify_and_judge_grounded_pitch, PitchVerificationResult

    original_pitch = (
        "YipitData's data platform approach aligns directly with my backend background. "
        "I have scaled distributed pipelines in Python and Go. "
        "I look forward to contributing this systems experience to your team."
    )
    generic_pitch = (
        "I have a strong backend background in distributed systems and scalable services. "
        "I look forward to contributing to your team."
    )
    mock_structured.return_value = PitchVerificationResult(
        is_grounded=False,
        hallucinated_entities=["YipitData"],
        critique="Incorrectly flags the provided target company.",
        corrected_pitch=generic_pitch,
    )

    verified = verify_and_judge_grounded_pitch(
        pitch_text=original_pitch,
        candidate_employers=["Fintech Corp"],
        candidate_titles=["Senior Staff Engineer"],
        candidate_skills=["Python", "Go"],
        candidate_highlights="Scaled payment engine to 40k RPS with zero downtime.",
        target_company="YipitData",
        job_title="Senior Staff Software Engineer"
    )

    assert verified == original_pitch
    assert "YipitData" in verified


@patch("backend.ai_helper.generate_text")
def test_generate_grounded_pitch_normalizes_generic_company_names(mock_gen, db_session):
    """Verifies that generic placeholders like 'Job boards' or 'Greenhouse' are normalized."""
    mock_gen.return_value = (
        "Your team's scalable infrastructure approach addresses core engineering challenges. "
        "I have engineered high-throughput services and reliable APIs under heavy load. "
        "I look forward to contributing this systems experience to the Staff Engineer role."
    )

    res = generate_grounded_anti_slop_pitch(
        question="Why do you want this role?",
        company_name="Job boards",
        job_title="Staff Engineer",
        db=db_session,
        auto_cache=False
    )

    assert res["answer"] is not None
    assert "Job boards" not in res["answer"]

