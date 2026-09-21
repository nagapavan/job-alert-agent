import os
import pytest
import requests
from backend.config import LM_STUDIO_BASE_URL
from backend.ai_helper import (
    detect_active_llm_provider,
    call_lmstudio,
    parse_resume_to_json,
    analyze_job_match,
    generate_cover_letter,
    generate_cold_outreach_message
)

def is_lmstudio_running() -> bool:
    try:
        base = LM_STUDIO_BASE_URL.rstrip("/")
        r = requests.get(f"{base}/models", timeout=2.0)
        return r.status_code == 200
    except Exception:
        return False

# Live tests are opt-in: they run only when RUN_LIVE_LLM_TESTS=1 AND LM Studio is up.
# This keeps the default `pytest tests/` suite deterministic and network-free.
RUN_LIVE_LLM_TESTS = os.environ.get("RUN_LIVE_LLM_TESTS", "").lower() in ("1", "true", "yes")
pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not (RUN_LIVE_LLM_TESTS and is_lmstudio_running()),
        reason="Live LLM tests are opt-in. Set RUN_LIVE_LLM_TESTS=1 with LM Studio running on localhost:1234.",
    ),
]

SAMPLE_RESUME = """Alex Jordan Morgan
Principal Software Engineer
San Francisco, CA | +1 555 019 2834 | alex.morgan@example.com

SUMMARY
Experienced Principal Software Engineer with 12+ years building high-throughput distributed systems and cloud infrastructure.

WORK EXPERIENCE
Apex Distributed Cloud
Principal Software Engineer | July 2024 - Present
• Designed distributed microservices platform processing 100k requests per second with 99.99% uptime.
• Automated deployment workflows across Kubernetes clusters reducing deployment lead time by 40%.

CloudScale Technologies
Staff Software Engineer | Jan 2021 - June 2024
• Designed real-time event streaming pipeline processing 50M events daily using Kafka and FastAPI.

EDUCATION
B.S. in Computer Science | Example University | 2012

SKILLS
Python, FastAPI, Docker, Kubernetes, AWS, PostgreSQL, Kafka, Distributed Systems
"""

SAMPLE_JOB_DESC = """
Role: Staff Backend Engineer
Company: Acme Systems
Location: Remote / San Francisco, CA

Requirements:
- 8+ years of experience with Python, FastAPI, Distributed Systems, and Kafka.
- Experience with Kubernetes, Docker, and AWS cloud architectures.
- Track record of leading architecture for high-throughput, fault-tolerant distributed services.
- Strong knowledge of relational databases and SQL performance tuning.
"""

def test_live_lmstudio_health_and_model_detection():
    """Verifies that LM Studio /v1/models is live and auto-detects the loaded model."""
    status = detect_active_llm_provider()
    assert status["provider"] == "LM Studio"
    assert status["status"] == "online"
    assert len(status["model"]) > 0
    print(f"\n[LIVE LM STUDIO] Detected Active Model: {status['model']}")

def test_live_lmstudio_basic_chat():
    """Verifies basic text completion without timeout."""
    resp = call_lmstudio(
        system_prompt="You are a helpful assistant.",
        user_prompt="Say 'LM Studio is ready for job alert agent!' in 1 sentence.",
        json_mode=False
    )
    assert len(resp.strip()) > 0
    print(f"\n[LIVE LM STUDIO] Basic Chat Response: {resp[:100]}")

def test_live_lmstudio_resume_parsing():
    """Verifies that LM Studio parses resume text into clean structured JSON."""
    parsed = parse_resume_to_json(SAMPLE_RESUME, provider="lmstudio")
    assert "Alex Jordan" in parsed["name"] or "Morgan" in parsed["name"]
    assert "alex.morgan@example.com" in parsed["email"]
    assert len(parsed["skills"]) >= 3
    assert len(parsed["experience"]) >= 1
    assert "Principal" in parsed["experience"][0]["title"]
    print(f"\n[LIVE LM STUDIO] Parsed Candidate: {parsed['name']} ({len(parsed['skills'])} skills detected)")

def test_live_lmstudio_job_match_scoring():
    """Verifies that LM Studio performs match scoring and gap analysis."""
    match = analyze_job_match(SAMPLE_RESUME, SAMPLE_JOB_DESC, provider="lmstudio")
    assert 0 <= match["match_score"] <= 100
    assert len(match["strengths"]) > 0 or len(match["feedback"]) > 0
    print(f"\n[LIVE LM STUDIO] Match Score: {match['match_score']}% | Strengths: {match['strengths']}")

def test_live_lmstudio_cover_letter_and_outreach():
    """Verifies that LM Studio generates personalized cover letters and cold messages."""
    letter = generate_cover_letter(SAMPLE_RESUME, "Acme Systems", "Staff Backend Engineer", SAMPLE_JOB_DESC, provider="lmstudio")
    assert len(letter) > 100
    assert "Acme" in letter or "Backend" in letter

    outreach = generate_cold_outreach_message(SAMPLE_RESUME, "Jane Doe", "Acme Systems", "Staff Backend Engineer", provider="lmstudio")
    assert len(outreach) > 30
    print(f"\n[LIVE LM STUDIO] Outreach Snippet:\n{outreach[:150]}...")
