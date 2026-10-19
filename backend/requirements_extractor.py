"""Tier A/B requirement extraction (deterministic, no LLM by default).

Rather than scoring only the intersection of a JD with a fixed skill list, this module
mines requirement *units* from the JD (bullets / sentences) and:

- **Tier A (deterministic):** units already covered by the curated taxonomy are handled
  by ``match_classifier``. Residual units are reduced to a key phrase and checked against
  the candidate's *own resume* — the "derive from the candidate" half of the design.
- **Tier B (embeddings):** when a semantic embedding backend is active (not the offline
  hash fallback), a residual phrase can still be credited if it is semantically close to
  a resume chunk.

Unmatched residuals are **recorded to a review queue** (``skill_taxonomy``) instead of
becoming scored requirements, so the vocabulary can grow without inventing requirements
from JD prose.
"""
from __future__ import annotations

import logging
import re
from typing import Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger("requirements_extractor")

# Leading phrasings that introduce a requirement but carry no signal themselves.
_LEADINS = (
    "hands-on experience with", "hands-on experience in", "hands on experience with",
    "experience with", "experience in", "experience of", "working knowledge of",
    "solid understanding of", "strong understanding of", "understanding of",
    "knowledge of", "familiarity with", "expertise in", "proficiency in",
    "background in", "skilled in", "proven", "solid", "strong", "demonstrated",
    "deep", "extensive", "ability to", "capable of", "comfortable with",
)

# Only these section labels contribute requirement units.
_CONTRIBUTING_LABELS = ("required", "preferred")

_STOPWORDS = {
    "a", "an", "the", "and", "or", "of", "in", "on", "to", "with", "for", "at", "by",
    "as", "is", "are", "be", "been", "being", "that", "this", "these", "those", "it",
    "its", "from", "into", "about", "how", "what", "who", "when", "where", "which",
    "you", "your", "we", "our", "they", "their", "will", "would", "can", "could",
    "should", "may", "might", "must", "have", "has", "had", "do", "does", "did",
    "not", "no", "so", "than", "then", "such", "including", "include", "includes",
    "etc", "via", "per", "plus", "using", "use", "used", "well", "also", "more",
    "most", "other", "others", "both", "each", "any", "all", "some", "very", "own",
    "record", "demonstrated", "experience", "years", "year", "work", "working",
}

_BULLET_RE = re.compile(r"^\s*(?:[•●*\-–—▪◦►■·]|\d+[\.\)])\s+")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?;])\s+")
_WS_RE = re.compile(r"\s+")

_pattern_cache: Optional[List["re.Pattern"]] = None


def _surface_patterns() -> List["re.Pattern"]:
    global _pattern_cache
    if _pattern_cache is None:
        from backend.skill_taxonomy import CANONICAL_BY_SURFACE

        _pattern_cache = [
            re.compile(r"(?<![A-Za-z0-9])" + re.escape(s) + r"(?![A-Za-z0-9])", re.I)
            for s in CANONICAL_BY_SURFACE
        ]
    return _pattern_cache


def unit_covered_by_taxonomy(unit_text: str) -> bool:
    """True when the unit already contains a known taxonomy skill surface."""
    text = unit_text or ""
    return any(p.search(text) for p in _surface_patterns())


def _strip_markup(text: str) -> str:
    """Remove HTML tags/entities so requirement units never carry markup.

    Some ATS feeds store the JD as HTML; without this, tags and style fragments become
    "requirement units" and pollute the taxonomy review queue (issue #8).
    """
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text or "", flags=re.I | re.S)
    text = re.sub(r"<[^>]*>", " ", text)
    text = re.sub(r"&[a-z]+;|&#\d+;", " ", text, flags=re.I)
    return text


def _split_units(text: str) -> List[str]:
    text = _strip_markup(text)
    units: List[str] = []
    for raw_line in (text or "").splitlines():
        line = _BULLET_RE.sub("", raw_line).strip()
        if not line:
            continue
        for sentence in _SENTENCE_SPLIT_RE.split(line):
            sentence = sentence.strip()
            if sentence:
                units.append(sentence)
    return units


def iter_requirement_units(jd: str, spans: Sequence) -> List[Tuple[str, str]]:
    """Yield ``(unit_text, category)`` for contributing (required/preferred) sections."""
    from backend.match_scoring import MUST_HAVE, NICE_TO_HAVE

    units: List[Tuple[str, str]] = []
    for span in spans:
        if span.label not in _CONTRIBUTING_LABELS:
            continue
        category = NICE_TO_HAVE if span.label == "preferred" else MUST_HAVE
        for unit in _split_units(jd[span.start:span.end]):
            units.append((unit, category))
    return units


def key_phrase(unit_text: str, max_words: int = 8) -> str:
    """Reduce a requirement unit to a short, de-fluffed phrase."""
    text = (unit_text or "").strip()
    if not text:
        return ""
    lowered = text.lower()
    changed = True
    while changed:
        changed = False
        for lead in _LEADINS:
            if lowered.startswith(lead + " "):
                text = text[len(lead):].strip()
                lowered = text.lower()
                changed = True
    text = text.strip(" .:;,-–—")
    words = text.split()
    if not words:
        return ""
    phrase = " ".join(words[:max_words]).strip()
    return phrase if len(phrase) >= 3 else ""


def _content_tokens(text: str) -> List[str]:
    tokens = re.findall(r"[a-z0-9][a-z0-9+#./\-]*", (text or "").lower())
    return [t for t in tokens if t not in _STOPWORDS and len(t) > 1]


def _token_match(token: str, corpus: set) -> bool:
    if token in corpus:
        return True
    if len(token) >= 4:
        return any(token in c or c in token for c in corpus if len(c) >= 4)
    return False


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _resume_chunks(resume_text: str, limit: int = 30) -> List[str]:
    chunks = [c.strip() for c in _SENTENCE_SPLIT_RE.split(resume_text or "") if c.strip()]
    return chunks[:limit]


def resume_supports(
    phrase: str,
    resume_text: str,
    resume_chunks: Optional[Sequence[str]] = None,
    token_threshold: float = 0.6,
    embedding_threshold: float = 0.80,
) -> Tuple[bool, float, str]:
    """Does the candidate's own resume evidence this phrase?

    Returns ``(supported, score, method)``. Token overlap runs first (deterministic);
    the embedding path is only attempted with a *semantic* backend.
    """
    phrase = (phrase or "").strip()
    if not phrase:
        return False, 0.0, "none"
    p_tokens = _content_tokens(phrase)
    if p_tokens:
        corpus = set(_content_tokens(resume_text or ""))
        if corpus:
            matched = sum(1 for t in p_tokens if _token_match(t, corpus))
            overlap = matched / len(p_tokens)
            if overlap >= token_threshold:
                return True, overlap, "token_overlap"
    # Embedding fallback (Tier B); skipped for the non-semantic hash backend.
    try:
        from backend import embeddings

        if embeddings.active_backend_name() == "hash":
            return False, 0.0, "none"
        chunks = list(resume_chunks) if resume_chunks is not None else _resume_chunks(resume_text)
        if not chunks:
            return False, 0.0, "none"
        query = embeddings.embed_text(phrase)
        best = max((_cosine(query, embeddings.embed_text(c)) for c in chunks), default=0.0)
        if best >= embedding_threshold:
            return True, best, "embedding"
    except Exception as exc:  # noqa: BLE001 - inference is best-effort
        logger.debug("Embedding resume-evidence check skipped: %s", exc)
    return False, 0.0, "none"


def extract_residual_requirements(
    jd: str,
    spans: Sequence,
    resume_text: str,
    resume_chunks: Optional[Sequence[str]] = None,
) -> List[Dict[str, str]]:
    """Mine residual JD units and credit the ones the resume evidences.

    Unsupported residuals are recorded to the taxonomy review queue, never scored.
    """
    from backend import skill_taxonomy

    results: List[Dict[str, str]] = []
    for unit_text, category in iter_requirement_units(jd, spans):
        if unit_covered_by_taxonomy(unit_text):
            continue
        phrase = key_phrase(unit_text)
        if not phrase:
            continue
        supported, _score, how = resume_supports(phrase, resume_text, resume_chunks)
        if supported:
            results.append(
                {"text": phrase, "category": category, "evidence": f"resume:{how}"}
            )
        else:
            skill_taxonomy.record_skill_candidate(phrase)
    return results
