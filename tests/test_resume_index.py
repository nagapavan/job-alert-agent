"""Unit tests for RAG-lite resume chunking, indexing, and evidence retrieval."""
import json
from unittest.mock import patch

from backend.config import encrypt_data
from backend.database import Resume, ResumeChunk
from backend.resume_index import chunk_resume, index_resume_chunks, top_evidence

PARSED = {
    "skills": ["Python", "AWS", "Kubernetes"],
    "experience": [
        {"title": "Senior Engineer", "company": "Acme", "dates": "2019 - Present"},
        {"title": "Engineer", "company": "Globex", "dates": "2016 - 2019", "description": "Built APIs"},
    ],
    "projects": [{"name": "Data Pipeline", "description": "Kafka + Spark"}],
}


def test_chunk_resume_sections():
    chunks = chunk_resume(PARSED)
    sections = [c["section"] for c in chunks]
    assert sections == ["skills", "role", "role", "project"]
    assert chunks[0]["text"].startswith("Skills: Python")
    assert "Built APIs" in chunks[2]["text"]


def test_chunk_resume_empty():
    assert chunk_resume({}) == []
    assert chunk_resume(None) == []


def test_chunk_resume_uses_parser_details_and_ai_bullets():
    """The heuristic parser writes `details`; the AI parser writes `bullets`. Neither is
    `description`, so a role chunk must not collapse to its header line (finding 79)."""
    parsed = {
        "experience": [
            {"title": "Engineer", "company": "Acme", "dates": "2020", "details": "Built APIs"},
            {"title": "Lead", "company": "Globex", "dates": "2022", "bullets": ["Led team", "Shipped"]},
        ]
    }
    chunks = chunk_resume(parsed)
    role_texts = [c["text"] for c in chunks if c["section"] == "role"]
    assert "Built APIs" in role_texts[0]
    assert "Led team" in role_texts[1] and "Shipped" in role_texts[1]


def _fake_embed(text):
    vec = [0.0] * 384
    vec[0 if "python" in text.lower() else 1] = 1.0
    return vec


@patch("backend.ai_helper.generate_embeddings", side_effect=_fake_embed)
def test_index_and_retrieve_prefers_matching_chunk(mock_emb, db_session):
    resume = Resume(
        filename="r.md",
        content_encrypted=encrypt_data("resume text"),
        parsed_json_encrypted=encrypt_data(json.dumps(PARSED)),
        is_active=True,
    )
    db_session.add(resume)
    db_session.commit()
    db_session.refresh(resume)

    count = index_resume_chunks(db_session, resume)
    assert count == 4
    assert db_session.query(ResumeChunk).filter_by(resume_id=resume.id).count() == 4

    evidence = top_evidence(db_session, resume.id, "python developer", k=1)
    assert evidence and evidence[0].startswith("Skills: Python")


@patch("backend.ai_helper.generate_embeddings", side_effect=_fake_embed)
def test_reindex_replaces_old_chunks(mock_emb, db_session):
    resume = Resume(
        filename="r.md",
        content_encrypted=encrypt_data("x"),
        parsed_json_encrypted=encrypt_data(json.dumps(PARSED)),
        is_active=True,
    )
    db_session.add(resume)
    db_session.commit()
    db_session.refresh(resume)

    index_resume_chunks(db_session, resume)
    index_resume_chunks(db_session, resume)
    assert db_session.query(ResumeChunk).filter_by(resume_id=resume.id).count() == 4


def test_top_evidence_empty_without_chunks(db_session):
    # No chunks -> returns [] without touching the network.
    assert top_evidence(db_session, 999, "python") == []
