"""Semantic Q&A memory bank, the essay-answer cache, and extension form telemetry.

These routes share one shape: they read or write the encrypted Q&A cache, the learned dismissal
patterns or the observed form fields, and the essay endpoints all resolve the active resume first.
No app-level state is involved, so they move out of ``main.py`` unchanged.
"""

import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from backend.ai_helper import (
    batch_autofill_essay_answers,
    generate_and_cache_essay_answer,
    generate_embeddings,
    generate_grounded_anti_slop_pitch,
    search_qa_memory,
)
from backend.config import decrypt_data
from backend.database import (
    ApplicationQuestionAnswer,
    CustomFormField,
    DismissedJobPattern,
    Resume,
    get_db,
)

router = APIRouter(tags=["memory_qa"])


class QAMemoryIn(BaseModel):
    question_text: str
    answer_text: str
    category: Optional[str] = "general"


class QAMemoryOut(BaseModel):
    id: int
    question_text: str
    answer_text: str
    category: str
    use_count: int
    created_at: datetime.datetime
    updated_at: datetime.datetime

    model_config = ConfigDict(from_attributes=True)


class QASearchRequest(BaseModel):
    query_question: str
    top_k: Optional[int] = 3
    min_similarity: Optional[float] = 0.65


class QAResolveRequest(BaseModel):
    question_text: str
    company_name: Optional[str] = None
    job_title: Optional[str] = None
    auto_cache: bool = True
    threshold: float = 0.85
    provider: Optional[str] = None


class QABatchAutofillRequest(BaseModel):
    questions: List[str]
    company_name: Optional[str] = None
    job_title: Optional[str] = None
    auto_cache: bool = True
    threshold: float = 0.85
    provider: Optional[str] = None


class GroundedPitchRequest(BaseModel):
    question_text: str
    company_name: Optional[str] = None
    job_title: Optional[str] = None
    job_description: Optional[str] = None
    auto_cache: bool = True
    threshold: float = 0.85
    provider: Optional[str] = None


class FormFieldItem(BaseModel):
    field_name: Optional[str] = None
    field_id: Optional[str] = None
    field_label: Optional[str] = None
    field_type: Optional[str] = "text"
    is_recognized: bool = False
    suggested_category: Optional[str] = "unknown"


class FormTelemetryRequest(BaseModel):
    domain: str
    ats_type: Optional[str] = "custom"
    url: Optional[str] = None
    fields: List[FormFieldItem]


class FormFieldOut(BaseModel):
    id: int
    ats_type: str
    domain: str
    field_name: Optional[str] = None
    field_id: Optional[str] = None
    field_label: Optional[str] = None
    field_type: str
    is_recognized: bool
    classified_category: str
    occurrence_count: int
    created_at: datetime.datetime

    model_config = ConfigDict(from_attributes=True)


class DismissedPatternOut(BaseModel):
    id: int
    title: str
    company_name: Optional[str] = None
    description: Optional[str] = None
    reason: Optional[str] = None
    created_at: datetime.datetime

    model_config = ConfigDict(from_attributes=True)

# -------------------------------------------------------------
# Semantic Memory & Question Bank Endpoints
# -------------------------------------------------------------
@router.get("/api/memory/qa", response_model=List[QAMemoryOut])
def get_qa_memory_items(category: Optional[str] = None, db: Session = Depends(get_db)):
    """Retrieves all saved application question-answer memory pairs."""
    query = db.query(ApplicationQuestionAnswer)
    if category and category != "all":
        query = query.filter(ApplicationQuestionAnswer.category == category)
    return query.order_by(
        ApplicationQuestionAnswer.use_count.desc(),
        ApplicationQuestionAnswer.updated_at.desc(),
    ).all()


@router.post(
    "/api/memory/qa", response_model=QAMemoryOut, status_code=status.HTTP_201_CREATED
)
def save_qa_memory_item(payload: QAMemoryIn, db: Session = Depends(get_db)):
    """Saves or updates an application question-answer pair with semantic embedding vector."""
    clean_q = payload.question_text.strip()
    clean_a = payload.answer_text.strip()
    if not clean_q or not clean_a:
        raise HTTPException(
            status_code=400, detail="Both question and answer text are required."
        )

    # Check if exact question already exists
    existing = (
        db.query(ApplicationQuestionAnswer)
        .filter(ApplicationQuestionAnswer.question_text.ilike(clean_q))
        .first()
    )

    emb = generate_embeddings(clean_q)

    if existing:
        existing.answer_text = clean_a
        existing.category = payload.category or existing.category
        existing.embedding = emb
        existing.use_count += 1
        db.commit()
        db.refresh(existing)
        return existing

    new_item = ApplicationQuestionAnswer(
        question_text=clean_q,
        answer_text=clean_a,
        category=payload.category or "general",
        embedding=emb,
        use_count=1,
    )
    db.add(new_item)
    db.commit()
    db.refresh(new_item)
    return new_item


@router.post("/api/memory/qa/search")
def search_qa_memory_endpoint(payload: QASearchRequest, db: Session = Depends(get_db)):
    """Searches the Q&A memory bank using semantic vector similarity."""
    results = search_qa_memory(
        query_question=payload.query_question,
        db=db,
        top_k=payload.top_k or 3,
        min_similarity=payload.min_similarity or 0.65,
    )
    return {"query": payload.query_question, "matches": results}


@router.delete("/api/memory/qa/{qa_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_qa_memory_item(qa_id: int, db: Session = Depends(get_db)):
    """Deletes a saved question-answer memory pair."""
    item = (
        db.query(ApplicationQuestionAnswer)
        .filter(ApplicationQuestionAnswer.id == qa_id)
        .first()
    )
    if not item:
        raise HTTPException(status_code=404, detail="Q&A item not found.")
    db.delete(item)
    db.commit()
    return None


@router.get("/api/memory/dismissed-patterns", response_model=List[DismissedPatternOut])
def get_dismissed_patterns(db: Session = Depends(get_db)):
    """Retrieves all learned negative job patterns for zero-token ignore filtering."""
    return (
        db.query(DismissedJobPattern)
        .order_by(DismissedJobPattern.created_at.desc())
        .all()
    )


@router.delete(
    "/api/memory/dismissed-patterns/{pattern_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_dismissed_pattern(pattern_id: int, db: Session = Depends(get_db)):
    """Removes a learned ignore rule so future matching jobs are no longer auto-filtered."""
    pattern = (
        db.query(DismissedJobPattern)
        .filter(DismissedJobPattern.id == pattern_id)
        .first()
    )
    if not pattern:
        raise HTTPException(status_code=404, detail="Pattern not found.")
    db.delete(pattern)
    db.commit()
    return None


# -------------------------------------------------------------
# Semantic Essay Prompt & Response Caching Endpoints
# -------------------------------------------------------------
@router.post("/api/qa/grounded-pitch")
def generate_grounded_pitch_endpoint(
    payload: GroundedPitchRequest, db: Session = Depends(get_db)
):
    """
    Generates a crisp, authentic, 3-5 sentence application essay / screener pitch.
    Eliminates all AI clichés, grounding the answer in company intelligence and candidate resume evidence.
    """
    active_resume = db.query(Resume).filter(Resume.is_active == True).first()
    resume_text = decrypt_data(active_resume.content_encrypted) if active_resume else ""

    return generate_grounded_anti_slop_pitch(
        question=payload.question_text,
        company_name=payload.company_name or "",
        job_title=payload.job_title or "",
        job_description=payload.job_description or "",
        resume_text=resume_text,
        db=db,
        auto_cache=payload.auto_cache,
        threshold=payload.threshold,
        provider=payload.provider,
    )


@router.post("/api/forms/telemetry")
def ingest_form_telemetry(payload: FormTelemetryRequest, db: Session = Depends(get_db)):
    """
    Ingests and aggregates field detection telemetry from the Chrome extension,
    learning custom form fields across career portals.
    """
    processed = 0
    clean_domain = payload.domain.strip().lower()
    for field in payload.fields:
        existing = (
            db.query(CustomFormField)
            .filter(
                CustomFormField.domain == clean_domain,
                (CustomFormField.field_id == field.field_id)
                | (CustomFormField.field_name == field.field_name)
                | (CustomFormField.field_label == field.field_label),
            )
            .first()
        )

        if existing:
            existing.occurrence_count += 1
            existing.is_recognized = field.is_recognized or existing.is_recognized
            if field.suggested_category and field.suggested_category != "unknown":
                existing.classified_category = field.suggested_category
            existing.updated_at = datetime.datetime.now(datetime.timezone.utc)
        else:
            new_field = CustomFormField(
                ats_type=payload.ats_type or "custom",
                domain=clean_domain,
                field_name=field.field_name,
                field_id=field.field_id,
                field_label=field.field_label,
                field_type=field.field_type or "text",
                is_recognized=field.is_recognized,
                classified_category=field.suggested_category or "unknown",
                occurrence_count=1,
            )
            db.add(new_field)
        processed += 1

    db.commit()
    return {"status": "ok", "processed_count": processed}


@router.get("/api/forms/telemetry", response_model=List[FormFieldOut])
def get_form_telemetry(
    domain: Optional[str] = None,
    unrecognized_only: bool = False,
    db: Session = Depends(get_db),
):
    """
    Returns aggregated custom form fields and learned portal patterns.
    """
    query = db.query(CustomFormField)
    if domain:
        query = query.filter(CustomFormField.domain == domain.strip().lower())
    if unrecognized_only:
        query = query.filter(CustomFormField.is_recognized == False)
    return query.order_by(CustomFormField.occurrence_count.desc()).all()


@router.post("/api/qa/resolve-or-generate")
def resolve_or_generate_qa_endpoint(
    payload: QAResolveRequest, db: Session = Depends(get_db)
):
    """
    Evaluates semantic question against memory cache first (0 tokens if >= threshold).
    Falls back to JIT LLM generation and auto-indexes fresh answer into database.
    """
    active_resume = db.query(Resume).filter(Resume.is_active == True).first()
    resume_text = decrypt_data(active_resume.content_encrypted) if active_resume else ""

    return generate_and_cache_essay_answer(
        question=payload.question_text,
        resume_text=resume_text,
        db=db,
        company_name=payload.company_name or "",
        job_title=payload.job_title or "",
        auto_cache=payload.auto_cache,
        threshold=payload.threshold,
        provider=payload.provider,
    )


@router.post("/api/qa/batch-autofill")
def batch_autofill_qa_endpoint(
    payload: QABatchAutofillRequest, db: Session = Depends(get_db)
):
    """
    Resolves a list of application form essay questions in a single call with mixed cache hits/misses.
    """
    active_resume = db.query(Resume).filter(Resume.is_active == True).first()
    resume_text = decrypt_data(active_resume.content_encrypted) if active_resume else ""

    return batch_autofill_essay_answers(
        questions=payload.questions,
        resume_text=resume_text,
        db=db,
        company_name=payload.company_name or "",
        job_title=payload.job_title or "",
        auto_cache=payload.auto_cache,
        threshold=payload.threshold,
        provider=payload.provider,
    )


@router.get("/api/qa/cache-stats")
def get_qa_cache_stats(db: Session = Depends(get_db)):
    """
    Returns observability metrics for the Semantic Q&A Cache.
    """
    all_qa = db.query(ApplicationQuestionAnswer).all()
    total_cached = len(all_qa)
    reused_count = sum(item.use_count - 1 for item in all_qa if item.use_count > 1)

    categories_breakdown = {}
    for item in all_qa:
        cat = item.category or "general"
        categories_breakdown[cat] = categories_breakdown.get(cat, 0) + 1

    top_reused = sorted(all_qa, key=lambda x: x.use_count, reverse=True)[:5]
    top_reused_data = [
        {
            "id": item.id,
            "question": item.question_text,
            "category": item.category,
            "use_count": item.use_count,
            "updated_at": item.updated_at,
        }
        for item in top_reused
    ]

    return {
        "total_cached_pairs": total_cached,
        "total_inferences_saved": reused_count,
        "categories_breakdown": categories_breakdown,
        "top_reused_questions": top_reused_data,
    }


