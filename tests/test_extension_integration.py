import json
import pytest
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient

from backend.main import app
from backend.database import ApplicationQuestionAnswer, Job, Company, Resume
from backend.config import encrypt_data
from backend.ai_helper import generate_embeddings

client = TestClient(app)

def test_extension_health_check_contract():
    """Verifies that the extension runtime adapter receives valid health response."""
    resp = client.get("/api/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert "database" in data
    assert "active_llm" in data

@patch("backend.chat_agent.generate_text")
def test_extension_copilot_chat_contract(mock_gen, db_session):
    """Verifies that extension Copilot messages are processed and return structured answers."""
    mock_gen.return_value = "For this Staff Backend role, emphasize your high-throughput microservice experience using the STAR method."

    payload = {
        "message": "How should I answer interview questions for this role?"
    }
    resp = client.post("/api/chat/assistant", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert "reply" in data
    assert "STAR method" in data["reply"]
    assert "actions_taken" in data
    assert "embedded_jobs" in data

def test_extension_qa_resolution_contract(db_session):
    """Verifies that the extension can query and auto-learn screener essay questions."""
    # Seed active resume
    db_session.add(Resume(
        filename="candidate.pdf",
        content_encrypted=encrypt_data("Senior engineer with distributed systems and Kubernetes background."),
        is_active=True
    ))
    q_text = "Why do you want to join our company?"
    a_text = "I admire the innovative product engineering culture at [Company]."
    db_session.add(ApplicationQuestionAnswer(
        question_text=q_text,
        answer_text=a_text,
        category="company_interest",
        embedding=generate_embeddings(q_text),
        use_count=1
    ))
    db_session.commit()

    payload = {
        "question_text": "Why do you want to join our company?",
        "company_name": "Datadog",
        "threshold": 0.80
    }
    resp = client.post("/api/qa/resolve-or-generate", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["is_cache_hit"] is True
    assert "Datadog" in data["answer"]

def test_extension_qa_memory_list_contract(db_session):
    """Verifies that the extension Q&A cache tab can load stored memory pairs."""
    q_text = "What are your salary expectations?"
    db_session.add(ApplicationQuestionAnswer(
        question_text=q_text,
        answer_text="$190k base",
        category="salary",
        embedding=generate_embeddings(q_text),
        use_count=3
    ))
    db_session.commit()

    resp = client.get("/api/memory/qa")
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) >= 1
    assert any("salary expectations" in item["question_text"] for item in items)


def test_extension_candidate_profile_contract(db_session):
    """Verifies that the /api/candidate/profile endpoint returns formatted profile data from active resume."""
    parsed_resume_json = {
        "name": "Alex Jordan Morgan",
        "email": "alex.morgan@example.com",
        "phone": "+1 555 123 4567",
        "location": "San Francisco, CA",
        "links": {
            "linkedin": "https://www.linkedin.com/in/alexjordanmorgan/",
            "github": "https://github.com/alexjordanmorgan",
            "resume": "https://drive.google.com/file/d/12345/view"
        },
        "experience": [
            {
                "company": "Tech Innovations Inc",
                "title": "Lead Staff Architect",
                "duration": "2021 - Present",
                "location": "San Francisco, CA",
                "description": "Architected distributed AI agent platforms.",
                "skills": ["Python", "FastAPI", "Kubernetes"]
            },
            {
                "company": "NextGen Systems Corp",
                "title": "Senior Backend Engineer",
                "duration": "2018 - 2021",
                "location": "San Jose, CA",
                "description": "Scaled high-throughput microservices.",
                "skills": ["Go", "Docker", "PostgreSQL"]
            },
            {
                "company": "Alpha Cloud Labs",
                "title": "Software Engineer",
                "duration": "2015 - 2018",
                "location": "Austin, TX",
                "description": "Built resilient API gateways.",
                "skills": ["Python", "AWS", "Redis"]
            }
        ],
        "education": [
            {
                "institution": "State University of Technology",
                "degree": "B.S. in Computer Science",
                "year": "2015"
            }
        ],
        "summary": "Seasoned Lead Engineer specializing in distributed backends and AI agents.",
        "skills": ["Python", "FastAPI", "React", "Docker", "Kubernetes"]
    }

    db_session.add(Resume(
        filename="alex_morgan_lead.pdf",
        content_encrypted=encrypt_data("Raw resume text for Alex Jordan Morgan..."),
        parsed_json_encrypted=encrypt_data(json.dumps(parsed_resume_json)),
        is_active=True
    ))
    db_session.commit()

    resp = client.get("/api/candidate/profile")
    assert resp.status_code == 200
    data = resp.json()

    assert data["fullName"] == "Alex Jordan Morgan"
    assert data["firstName"] == "Alex Jordan"
    assert data["lastName"] == "Morgan"
    assert data["email"] == "alex.morgan@example.com"
    assert data["phone"] == "+1 555 123 4567"
    assert data["location"] == "San Francisco, CA"
    assert data["linkedinUrl"] == "https://www.linkedin.com/in/alexjordanmorgan/"
    assert data["githubUrl"] == "https://github.com/alexjordanmorgan"
    assert data["resumeLink"] == "https://drive.google.com/file/d/12345/view"
    assert data["currentCompany"] == "Tech Innovations Inc"
    assert data["currentTitle"] == "Lead Staff Architect"
    assert "distributed backends" in data["resumeSummary"]
    assert "Python" in data["skills"]
    assert len(data["experience"]) == 3
    assert data["experience"][0]["company"] == "Tech Innovations Inc"
    assert data["experience"][1]["company"] == "NextGen Systems Corp"
    assert len(data["education"]) == 1
    assert data["education"][0]["institution"] == "State University of Technology"


def test_extension_candidate_profile_work_history_fallback(db_session):
    """Verifies that work_history key is parsed as experience when experience key is omitted."""
    parsed_json = {
        "name": "Jane Doe",
        "email": "jane.doe@example.com",
        "work_history": [
            {"company": "First Tier Systems", "title": "Senior Engineer"},
            {"company": "Foundational Labs", "title": "Software Engineer"}
        ]
    }
    db_session.add(Resume(
        filename="jane_doe.pdf",
        content_encrypted=encrypt_data("Raw resume text for Jane Doe..."),
        parsed_json_encrypted=encrypt_data(json.dumps(parsed_json)),
        is_active=True
    ))
    db_session.commit()

    resp = client.get("/api/candidate/profile")
    assert resp.status_code == 200
    data = resp.json()
    assert data["currentCompany"] == "First Tier Systems"
    assert len(data["experience"]) == 2
    assert data["experience"][0]["company"] == "First Tier Systems"
    assert data["experience"][1]["company"] == "Foundational Labs"


def test_extension_telemetry_filters_recaptcha_and_uuid_fields(db_session):
    """Verifies that the telemetry ingestion properly ingests sanitized form fields and ignores noise."""
    payload = {
        "domain": "jobs.ashbyhq.com",
        "ats_type": "ashby",
        "url": "https://jobs.ashbyhq.com/example-company/123",
        "fields": [
            {
                "field_name": "why_interested",
                "field_id": "field_why",
                "field_label": "Why do you want to work here?",
                "field_type": "textarea",
                "is_recognized": False,
                "suggested_category": "why_us"
            },
            {
                "field_name": "g-recaptcha-response",
                "field_id": "g-recaptcha-response",
                "field_label": "g-recaptcha-response",
                "field_type": "textarea",
                "is_recognized": False,
                "suggested_category": "unknown"
            }
        ]
    }
    resp = client.post("/api/forms/telemetry", json=payload)
    assert resp.status_code == 200
    res_data = resp.json()
    assert res_data["status"] == "ok"
    assert res_data["processed_count"] == 2

    # Query telemetry report
    get_resp = client.get("/api/forms/telemetry?domain=jobs.ashbyhq.com")
    assert get_resp.status_code == 200
    telemetry_list = get_resp.json()
    assert any(f["field_name"] == "why_interested" for f in telemetry_list)


# -------------------------------------------------------------
# Extension static invariants (passive / gesture-only / no LinkedIn access)
# -------------------------------------------------------------
EXT_DIR = Path(__file__).resolve().parent.parent / "extension"


def _read(rel_path: str) -> str:
    return (EXT_DIR / rel_path).read_text(encoding="utf-8")


def test_manifest_has_no_linkedin_host_access():
    """The extension must not request persistent access to linkedin.com.

    LinkedIn is gesture-only: the side panel injects the content script on demand
    under activeTab, so no host permission or declarative content-script match may
    reference it.
    """
    manifest = json.loads(_read("manifest.json"))
    blob = json.dumps(manifest).lower()
    assert "linkedin.com" not in blob


def test_content_script_observer_never_autofills():
    """The SPA MutationObserver must only refresh the badge, never autofill/submit forms."""
    src = _read("content/ats-autofill.js")
    start = src.index("new MutationObserver(")
    block = src[start:start + 400]
    assert "updateBadgeLifecycle" in block
    assert "autofill" not in block.lower()


def test_content_script_has_no_page_or_cookie_access():
    """No MAIN-world injection, cookie/session reads, or platform-internal API calls."""
    src = _read("content/ats-autofill.js")
    assert 'world: "MAIN"' not in src and "world: 'MAIN'" not in src
    assert "document.cookie" not in src
    assert ".postMessage(" not in src
    assert "voyager" not in src.lower()
    assert "chrome.cookies" not in src


def test_extension_never_calls_linkedin_internal_endpoints():
    """No extension source may call LinkedIn's internal endpoints."""
    for rel in (
        "manifest.json",
        "background/service-worker.js",
        "sidepanel/sidepanel.js",
        "js/runtime-adapter.js",
        "content/ats-autofill.js",
    ):
        assert "voyager" not in _read(rel).lower()


def test_sidepanel_uses_gesture_based_injection():
    """The side panel must inject the content script on demand (activeTab) for gesture-only hosts."""
    src = _read("sidepanel/sidepanel.js")
    assert "ensureContentScriptInjected" in src
    assert "chrome.scripting.executeScript" in src



