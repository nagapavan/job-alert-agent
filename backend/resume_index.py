"""RAG-lite resume chunking, indexing, and evidence retrieval.

Chunks are embedded once per resume (skills block + each role + each project) and used to
ground requirement assessment. Scoring itself never depends on embeddings.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional


def chunk_resume(parsed_json: Optional[Dict[str, Any]]) -> List[Dict[str, str]]:
    """Split a parsed resume into embeddable chunks (skills, each role, each project)."""
    parsed = parsed_json or {}
    chunks: List[Dict[str, str]] = []

    skills = [str(s).strip() for s in (parsed.get("skills") or []) if str(s).strip()]
    if skills:
        chunks.append({"section": "skills", "text": "Skills: " + ", ".join(skills)})

    for exp in parsed.get("experience") or []:
        header = " | ".join(
            str(exp.get(key)).strip()
            for key in ("title", "company", "dates")
            if exp.get(key)
        )
        detail = str(exp.get("description") or exp.get("summary") or "").strip()
        text = f"{header}\n{detail}".strip() if detail else header
        if text:
            chunks.append({"section": "role", "text": text})

    for project in parsed.get("projects") or []:
        name = str(project.get("name") or project.get("title") or "").strip()
        detail = str(project.get("description") or "").strip()
        text = f"{name}\n{detail}".strip() if detail else name
        if text:
            chunks.append({"section": "project", "text": text})

    return chunks


def index_resume_chunks(db, resume) -> int:
    """(Re)index a resume's chunks. Returns the number of chunks written."""
    from backend.ai_helper import generate_embeddings
    from backend.config import decrypt_data
    from backend.database import ResumeChunk

    parsed = {}
    if getattr(resume, "parsed_json_encrypted", None):
        try:
            parsed = json.loads(decrypt_data(resume.parsed_json_encrypted))
        except Exception:
            parsed = {}

    chunks = chunk_resume(parsed)
    db.query(ResumeChunk).filter(ResumeChunk.resume_id == resume.id).delete()
    for chunk in chunks:
        db.add(
            ResumeChunk(
                resume_id=resume.id,
                section=chunk["section"],
                text=chunk["text"],
                embedding=generate_embeddings(chunk["text"]),
            )
        )
    db.commit()
    return len(chunks)


def top_evidence(db, resume_id: int, query: str, k: int = 3) -> List[str]:
    """Return the resume chunks most similar to ``query`` (best-effort; [] when none)."""
    from backend.company_catalog import compute_cosine_similarity
    from backend.database import ResumeChunk

    chunks = (
        db.query(ResumeChunk).filter(ResumeChunk.resume_id == resume_id).all()
    )
    if not chunks or not (query or "").strip():
        return []

    from backend.ai_helper import generate_embeddings

    q_vec = generate_embeddings(query)
    scored = [
        (compute_cosine_similarity(q_vec, chunk.embedding), chunk.text)
        for chunk in chunks
        if chunk.embedding
    ]
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [text for _, text in scored[:k]]
