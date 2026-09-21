import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

from backend.main import app
from backend.database import ApplicationQuestionAnswer, Resume
from backend.config import encrypt_data
from backend.ai_helper import (
    adapt_answer_variables,
    classify_question_category,
    resolve_semantic_essay_cache,
    generate_and_cache_essay_answer,
    batch_autofill_essay_answers,
    generate_embeddings,
)

client = TestClient(app)

# -------------------------------------------------------------
# 1. Unit Tests: Variable Adaptation & Categorization
# -------------------------------------------------------------


def test_adapt_answer_variables():
    # Test standard placeholder substitution
    ans = (
        "I am excited to join [Company] as a [Role] because of its innovative product."
    )
    adapted = adapt_answer_variables(
        ans, "Why work here?", target_company="Datadog", target_role="Staff Engineer"
    )
    assert "Datadog" in adapted
    assert "Staff Engineer" in adapted
    assert "[Company]" not in adapted

    # Test dynamic company name replacement from cached question
    ans2 = "Stripe has built the world's most resilient payments infrastructure."
    adapted2 = adapt_answer_variables(ans2, "Why Stripe?", target_company="Uber")
    assert "Uber" in adapted2


def test_classify_question_category():
    assert (
        classify_question_category("Why do you want to work at Google?")
        == "company_interest"
    )
    assert (
        classify_question_category(
            "Describe your experience with distributed databases and Python"
        )
        == "technical"
    )
    assert (
        classify_question_category(
            "Tell me about a time you had a conflict with a coworker"
        )
        == "behavioral"
    )
    assert (
        classify_question_category("What are your expected salary requirements?")
        == "salary"
    )
    assert (
        classify_question_category("Will you require visa sponsorship in the US?")
        == "logistics"
    )
    assert (
        classify_question_category("Walk me through your resume and background")
        == "experience"
    )


# -------------------------------------------------------------
# 2. Integration Tests: Semantic Cache Resolver & Auto-Learning
# -------------------------------------------------------------


def test_resolve_semantic_essay_cache_hit_and_miss(db_session):
    # 1. Seed cached question
    q_text = "Why do you want to join our engineering team?"
    a_text = "I have spent 7 years building high-throughput microservices and admire [Company]'s engineering culture."
    emb = generate_embeddings(q_text)

    item = ApplicationQuestionAnswer(
        question_text=q_text,
        answer_text=a_text,
        category="company_interest",
        embedding=emb,
        use_count=1,
    )
    db_session.add(item)
    db_session.commit()

    # 2. Query similar question (cache hit)
    hit = resolve_semantic_essay_cache(
        question="Why do you want to join our team?",
        db=db_session,
        company_name="Netflix",
        threshold=0.75,
    )
    assert hit is not None
    assert hit["is_cache_hit"] is True
    assert "Netflix" in hit["answer"]
    assert hit["similarity"] >= 0.75
    assert hit["tokens_saved"] > 0

    # Verify use_count was incremented in DB
    refreshed = (
        db_session.query(ApplicationQuestionAnswer).filter_by(id=item.id).first()
    )
    assert refreshed.use_count == 2

    # 3. Query completely unrelated question (cache miss)
    miss = resolve_semantic_essay_cache(
        question="What is your current US work authorization status?",
        db=db_session,
        threshold=0.90,
    )
    assert miss is None


@patch("backend.ai_helper.generate_text")
def test_generate_and_cache_essay_answer_auto_learning(mock_gen, db_session):
    mock_gen.return_value = "I led the migration of a legacy monolithic service to gRPC, reducing latency by 35%."

    # Initial query: Cache miss triggers LLM generation and auto-indexing
    res = generate_and_cache_essay_answer(
        question="Describe a difficult performance optimization project you delivered",
        resume_text="Senior Backend Engineer with Python and Go experience",
        db=db_session,
        company_name="Stripe",
        auto_cache=True,
        threshold=0.85,
    )

    assert res["is_cache_hit"] is False
    assert "gRPC" in res["answer"]
    assert res["cache_id"] is not None

    # Verify newly generated Q&A was indexed in database
    cached_db_item = (
        db_session.query(ApplicationQuestionAnswer)
        .filter_by(id=res["cache_id"])
        .first()
    )
    assert cached_db_item is not None
    assert "performance optimization" in cached_db_item.question_text

    # Second query for identical/similar question: Instant Cache Hit (0 LLM calls)
    mock_gen.reset_mock()
    second_res = generate_and_cache_essay_answer(
        question="Describe a difficult performance optimization project you delivered",
        resume_text="Senior Backend Engineer",
        db=db_session,
        company_name="Airbnb",
        threshold=0.85,
    )

    assert second_res["is_cache_hit"] is True
    assert second_res["tokens_saved"] > 0
    mock_gen.assert_not_called()


@patch("backend.ai_helper.generate_text")
def test_batch_autofill_essay_answers(mock_gen, db_session):
    # Seed 1 answer in cache
    q1 = "What is your target compensation?"
    a1 = "$180,000 - $210,000 base salary plus equity."
    db_session.add(
        ApplicationQuestionAnswer(
            question_text=q1,
            answer_text=a1,
            category="salary",
            embedding=generate_embeddings(q1),
            use_count=1,
        )
    )
    db_session.commit()

    mock_gen.return_value = "I am a US Citizen and do not require visa sponsorship."

    questions = [
        "What is your target compensation?",
        "Will you now or in the future require visa sponsorship?",
    ]

    batch_res = batch_autofill_essay_answers(
        questions=questions,
        resume_text="Candidate resume",
        db=db_session,
        company_name="Uber",
    )

    assert batch_res["total_questions"] == 2
    assert batch_res["cache_hits"] == 1
    assert batch_res["cache_misses"] == 1
    assert len(batch_res["answers"]) == 2


# -------------------------------------------------------------
# 3. REST API Endpoint Tests
# -------------------------------------------------------------


def test_api_resolve_or_generate_endpoint(db_session):
    # Seed active resume
    db_session.add(
        Resume(
            filename="candidate_resume.pdf",
            content_encrypted=encrypt_data(
                "Senior engineer with FastAPI and PostgreSQL expertise."
            ),
            is_active=True,
        )
    )
    # Seed Q&A
    q = "Why are you interested in our mission?"
    db_session.add(
        ApplicationQuestionAnswer(
            question_text=q,
            answer_text="I am passionate about building developer tools at [Company].",
            category="company_interest",
            embedding=generate_embeddings(q),
            use_count=1,
        )
    )
    db_session.commit()

    res = client.post(
        "/api/qa/resolve-or-generate",
        json={
            "question_text": "Why are you interested in our mission?",
            "company_name": "GitHub",
            "threshold": 0.80,
        },
    )

    assert res.status_code == 200
    data = res.json()
    assert data["is_cache_hit"] is True
    assert "GitHub" in data["answer"]


def test_api_batch_autofill_endpoint(db_session):
    q = "What is your notice period?"
    db_session.add(
        ApplicationQuestionAnswer(
            question_text=q,
            answer_text="2 weeks standard notice.",
            category="logistics",
            embedding=generate_embeddings(q),
            use_count=1,
        )
    )
    db_session.commit()

    res = client.post(
        "/api/qa/batch-autofill",
        json={"questions": ["What is your notice period?"], "company_name": "Stripe"},
    )

    assert res.status_code == 200
    data = res.json()
    assert data["total_questions"] == 1
    assert data["cache_hits"] == 1


def test_api_cache_stats_endpoint(db_session):
    q = "Why this role?"
    db_session.add(
        ApplicationQuestionAnswer(
            question_text=q,
            answer_text="Great alignment.",
            category="company_interest",
            embedding=generate_embeddings(q),
            use_count=5,
        )
    )
    db_session.commit()

    res = client.get("/api/qa/cache-stats")
    assert res.status_code == 200
    data = res.json()
    assert data["total_cached_pairs"] >= 1
    assert data["total_inferences_saved"] >= 4
    assert "company_interest" in data["categories_breakdown"]
