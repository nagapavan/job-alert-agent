import os
import json
import pytest
from fastapi.testclient import TestClient
from backend.main import app
from backend.config import API_KEY, encrypt_data, decrypt_data, is_valid_api_key, cipher
from backend.database import ApplicationQuestionAnswer

def test_api_key_validity_check():
    """Validates constant-time API key verification."""
    assert is_valid_api_key(API_KEY) is True
    assert is_valid_api_key("wrong_key_1234567890123456") is False
    assert is_valid_api_key("") is False
    assert is_valid_api_key(None) is False

def test_public_endpoints_accessible_without_auth():
    """Public endpoints should be accessible without any API key or session token."""
    client = TestClient(app)
    
    health_res = client.get("/api/health")
    assert health_res.status_code == 200
    assert health_res.json()["status"] == "ok"

    auth_status_res = client.get("/api/auth/status")
    assert auth_status_res.status_code == 200
    data = auth_status_res.json()
    assert data["auth_required"] is True
    assert data["api_key_configured"] is True

def test_protected_endpoint_rejects_unauthorized_cross_origin():
    """Protected endpoints should reject requests from external origins without a valid API key."""
    client = TestClient(app)
    
    # Simulate cross-origin request from an arbitrary malicious site
    headers = {
        "Origin": "https://malicious-website.com",
        "Sec-Fetch-Site": "cross-site"
    }
    
    res = client.get("/api/candidate/profile", headers=headers)
    assert res.status_code == 401
    assert "Unauthorized" in res.json().get("detail", "")

def test_protected_endpoint_accepts_valid_api_key_header():
    """Protected endpoints should succeed when valid X-API-Key is provided."""
    client = TestClient(app)
    
    headers = {
        "X-API-Key": API_KEY,
        "Origin": "chrome-extension://abcdefghijklmnopqrstuvwxyz"
    }
    
    res = client.get("/api/auth/client-config", headers=headers)
    assert res.status_code == 200
    assert res.json()["api_key"] == API_KEY

def test_protected_endpoint_accepts_bearer_token():
    """Protected endpoints should accept Bearer token authorization."""
    client = TestClient(app)
    
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Origin": "https://external-client.com"
    }
    
    res = client.get("/api/auth/client-config", headers=headers)
    assert res.status_code == 200

def test_protected_endpoint_rejects_query_param_key():
    """Auth is header/Bearer/same-origin only; ?api_key= must NOT authenticate (keys leak into logs/referrers)."""
    client = TestClient(app)

    res = client.get(f"/api/auth/client-config?api_key={API_KEY}", headers={"Origin": "https://external-client.com"})
    assert res.status_code == 401

def test_qa_memory_transparent_encryption_at_rest(db_session):
    """Verifies that ApplicationQuestionAnswer encrypts answer_text in the database and transparently decrypts on read."""
    question_text = "What is your desired salary range for this role?"
    plain_answer = "$180,000 - $210,000 USD base salary"
    
    item = ApplicationQuestionAnswer(
        question_text=question_text,
        answer_text=plain_answer,
        category="salary"
    )
    db_session.add(item)
    db_session.commit()
    db_session.refresh(item)
    
    # 1. ORM level reading should return decrypted plaintext
    assert item.answer_text == plain_answer
    
    # 2. Raw underlying column _answer_text should be encrypted ciphertext (not plaintext)
    assert item._answer_text != plain_answer
    decrypted_raw = cipher.decrypt(item._answer_text.encode("utf-8")).decode("utf-8")
    assert decrypted_raw == plain_answer

def test_qa_legacy_plaintext_backward_compatibility():
    """Verifies that legacy plaintext answers in database decrypt gracefully without crashing."""
    legacy_plain = "Legacy plaintext answer"
    decrypted = decrypt_data(legacy_plain)
    assert decrypted == legacy_plain

def test_candidate_profile_has_no_personal_defaults():
    """Personal application facts must never be pre-filled with assumed defaults."""
    client = TestClient(app)
    res = client.get("/api/candidate/profile")
    assert res.status_code == 200
    data = res.json()
    assert data["workAuthorization"] == ""
    assert data["sponsorshipRequired"] == ""
    assert data["noticePeriod"] == ""

def test_init_db_does_not_seed_personal_answers(tmp_path):
    """init_db must not seed assumed work-authorization/sponsorship/notice-period answers."""
    from sqlalchemy.orm import sessionmaker
    from backend import database as db_module

    db_file = tmp_path / "seed_check.db"
    engine = db_module.init_db(custom_url=f"sqlite:///{db_file}")
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        assert session.query(db_module.ApplicationQuestionAnswer).count() == 0
    finally:
        session.close()
        # Restore the module's global session factory to the real database
        db_module.init_db()
