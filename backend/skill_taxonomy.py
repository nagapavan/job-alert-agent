"""Skill taxonomy as data (``data/skill_taxonomy.json``).

The taxonomy is intentionally **data, not code**: it can grow without a Python change,
and the same vocabulary serves the JD requirement scanner, the resume skill scan, and
concept-based partial credit. A small built-in fallback keeps the app working if the
JSON is missing or malformed.

Only *curated* skills live here. Unknown phrases mined from JDs are recorded to a
separate review queue (``data/skill_taxonomy_candidates.json``) rather than silently
becoming scoring inputs — see ``record_skill_candidate``.
"""
from __future__ import annotations

import json
import logging
import re
import threading
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

logger = logging.getLogger("skill_taxonomy")

BASE_DIR = Path(__file__).resolve().parent.parent
TAXONOMY_PATH = BASE_DIR / "data" / "skill_taxonomy.json"
CANDIDATES_PATH = BASE_DIR / "data" / "skill_taxonomy_candidates.json"

# Minimal built-in fallback (kept in sync with the common core of the JSON file).
_FALLBACK_SKILLS: List[dict] = [
    {"name": "Python", "aliases": [], "concepts": []},
    {"name": "FastAPI", "aliases": [], "concepts": []},
    {"name": "PostgreSQL", "aliases": ["postgres", "psql"], "concepts": ["sql"]},
    {"name": "Docker", "aliases": [], "concepts": ["containers"]},
    {"name": "Kubernetes", "aliases": ["k8s"], "concepts": ["containers"]},
    {"name": "AWS", "aliases": ["amazon web services"], "concepts": ["cloud"]},
    {"name": "GCP", "aliases": ["google cloud platform", "google cloud"], "concepts": ["cloud"]},
    {"name": "Azure", "aliases": ["microsoft azure"], "concepts": ["cloud"]},
    {"name": "CI/CD", "aliases": ["continuous integration", "continuous delivery"], "concepts": ["devops"]},
    {"name": "React", "aliases": ["react.js", "reactjs"], "concepts": ["javascript"]},
    {"name": "TypeScript", "aliases": ["ts"], "concepts": ["javascript", "node.js"]},
    {"name": "JavaScript", "aliases": ["js"], "concepts": ["node.js", "typescript"]},
    {"name": "Node.js", "aliases": ["nodejs", "node js"], "concepts": ["javascript", "typescript"]},
    {"name": "REST API", "aliases": ["rest", "restful api", "api integration"], "concepts": ["api"]},
    {"name": "Machine Learning", "aliases": ["ml"], "concepts": ["llm", "nlp"]},
    {"name": "LLM", "aliases": ["llms", "large language model", "genai", "generative ai"], "concepts": ["machine learning", "nlp"]},
    {"name": "NLP", "aliases": ["natural language processing"], "concepts": ["machine learning", "llm"]},
    {"name": "Go", "aliases": ["golang"], "concepts": []},
    {"name": "SQL", "aliases": [], "concepts": []},
]

_FALLBACK_CLOUD = {"aws", "gcp", "azure"}

# Single generic-English surfaces are rejected as aliases: they match ordinary JD prose
# (e.g. a perks paragraph mentioning "benefits") and surface false requirements. Canonical
# display names are unaffected — only the ambiguous short alias is dropped. This is a
# review-time guard so a future hand-edit of the data file cannot silently re-introduce
# the class of bug that made "benefits" score as "Benefits Administration".
_AMBIGUOUS_ALIASES = {
    "benefits", "employee benefits", "benefits systems",
    "case management", "leave management",
}


def _normalize(name: str) -> str:
    return (name or "").strip().lower()


class _TaxonomyData:
    def __init__(self) -> None:
        self.skills: List[dict] = []
        self.display_skills: List[str] = []
        self.canonical_by_surface: Dict[str, str] = {}
        self.concepts: Dict[str, Set[str]] = {}
        self.cloud_providers: Set[str] = set()


def _load() -> _TaxonomyData:
    data = _TaxonomyData()
    raw: Optional[dict] = None
    if TAXONOMY_PATH.exists():
        try:
            raw = json.loads(TAXONOMY_PATH.read_text("utf-8"))
        except Exception as exc:  # noqa: BLE001 - never fail import on bad data
            logger.warning("Failed to parse %s: %s", TAXONOMY_PATH, exc)
    skills = (raw or {}).get("skills")
    if not isinstance(skills, list) or not skills:
        skills = _FALLBACK_SKILLS
    cloud = (raw or {}).get("cloud_providers")
    if not isinstance(cloud, list) or not cloud:
        cloud = sorted(_FALLBACK_CLOUD)

    for entry in skills:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        if not name:
            continue
        canonical = _normalize(name)
        raw_aliases = [str(a).strip().lower() for a in (entry.get("aliases") or []) if str(a).strip()]
        dropped = [a for a in raw_aliases if a in _AMBIGUOUS_ALIASES]
        if dropped:
            logger.warning(
                "Ignoring ambiguous taxonomy alias(es) for %s: %s", name, ", ".join(dropped)
            )
        aliases = [a for a in raw_aliases if a not in _AMBIGUOUS_ALIASES]
        concepts = {str(c).strip().lower() for c in (entry.get("concepts") or []) if str(c).strip()}
        data.skills.append({"name": name, "canonical": canonical, "aliases": aliases, "concepts": concepts})
        data.display_skills.append(name)
        data.canonical_by_surface[canonical] = canonical
        for alias in aliases:
            data.canonical_by_surface[alias] = canonical
        data.concepts[canonical] = concepts
    data.concepts.setdefault("cloud", set())
    data.cloud_providers = {_normalize(c) for c in cloud}
    return data


_DATA = _load()

# Public, import-friendly views (rebuilt only if the data is reloaded).
DISPLAY_SKILLS: List[str] = _DATA.display_skills
CANONICAL_BY_SURFACE: Dict[str, str] = _DATA.canonical_by_surface
CONCEPT_GROUPS: Dict[str, Set[str]] = _DATA.concepts
CLOUD_PROVIDERS: Set[str] = _DATA.cloud_providers


def reload() -> None:
    """Reload the taxonomy from disk (used after the file changes)."""
    global _DATA, DISPLAY_SKILLS, CANONICAL_BY_SURFACE, CONCEPT_GROUPS, CLOUD_PROVIDERS
    _DATA = _load()
    DISPLAY_SKILLS = _DATA.display_skills
    CANONICAL_BY_SURFACE = _DATA.canonical_by_surface
    CONCEPT_GROUPS = _DATA.concepts
    CLOUD_PROVIDERS = _DATA.cloud_providers


def normalize_skill(name: str) -> str:
    """Map a surface spelling to its canonical lower-case skill name."""
    key = _normalize(name)
    return CANONICAL_BY_SURFACE.get(key, key)


def alias_surfaces(canonical: str) -> Set[str]:
    """All surface spellings that map to a canonical skill."""
    canon = normalize_skill(canonical)
    return {surface for surface, c in CANONICAL_BY_SURFACE.items() if c == canon and surface != canon}


def concepts_for(canonical: str) -> Set[str]:
    return CONCEPT_GROUPS.get(normalize_skill(canonical), set())


# ---------------------------------------------------------------------------
# Candidate review queue (dynamic growth with a promotion gate)
# ---------------------------------------------------------------------------

_candidates_lock = threading.Lock()

# Markup that reached the queue historically because the stored JD text was raw HTML.
# Note: fragments arrive *split mid-tag* (e.g. "<li style=\"color: rgb(73, 73, 73)"), so a
# strict "well-formed tag" regex is not enough - any angle bracket disqualifies the phrase.
_HTML_TAG_RE = re.compile(r"[<>]|&[a-z]+;?|&#\d+;", re.I)
# CSS declarations / style leftovers ("font-family: helvetica, arial, sans-serif", "font-size: 10pt").
_CSS_RE = re.compile(
    r"[a-z-]+\s*:\s*[a-z0-9#(),.%\"' ]+;|\b\d+(pt|px|em|rem|%)\b|"
    r"\b(font-family|font-size|font-style|color|margin|padding|line-height|"
    r"background|display|text-align|border|width|height)\b",
    re.I,
)
# Sentence openings that mark prose, not a skill ("we are a small team that values…").
_PROSE_LEAD_RE = re.compile(
    r"^(we|our|you|your|they|the team|join|join us|come|help us|as a|this role|"
    r"this is|you will|you'll|there is|it is|is a|are a|be a|a |an )\b",
    re.I,
)
# Fragments left by truncation/joining ("… more", leading conjunction).
_FRAGMENT_RE = re.compile(r"^\s*(…|\.\.\.)|^(and|or|for|to|of|with|in|on|at|as|but)\b", re.I)
# Footer/legal/CTA phrases: never requirements in any JD we care about. Kept deliberately small;
# the queue is *reviewed by a human*, so a borderline phrase is cheap to ignore, and a long
# hand-enumerated list would be a maintenance liability.
_NOISE_PHRASE_RE = re.compile(
    r"(all rights reserved|equal opportunity|privacy policy|terms of use|cookie|©|"
    r"apply now|learn more|read more|find out more|get started|sign up|click here|contact us|"
    r"under license|export control|accommodation|"
    r"about (us|the company|our)|nice to have'?s?\b|additional details|job category)",
    re.I,
)
# Requisition ids / salary strings: identifiers and numbers, never skills.
_ID_RE = re.compile(r"^[a-z]{1,4}\d{4,}$|^[₹$€£]\s?\d", re.I)
# Common English function words. A phrase made only of these (or a single one) carries no
# skill signal, so it is not worth a reviewer's time.
_FUNCTION_WORDS = {
    "a", "an", "and", "as", "at", "be", "but", "by", "for", "from", "here", "if", "in", "is",
    "it", "more", "no", "not", "of", "on", "or", "our", "so", "that", "the", "their", "there",
    "these", "this", "those", "to", "us", "we", "with", "yes", "you", "your", "etc", "note",
    "please", "other", "others", "all", "any", "some", "such", "than", "then", "too", "very",
}

# Section headings are reviewed centrally in section_intent; reuse them rather than
# duplicating a second heading vocabulary here (they would drift).
_HEADING_PHRASES: Optional[Set[str]] = None


def _heading_phrases() -> Set[str]:
    global _HEADING_PHRASES
    if _HEADING_PHRASES is None:
        try:
            from backend import section_intent

            _HEADING_PHRASES = {
                phrase.replace("\u2019", "'").replace("\u2018", "'")
                for phrases in section_intent.JD_HEADINGS.values()
                for phrase in phrases
            }
        except Exception:  # noqa: BLE001 - the filter degrades, capture still works
            _HEADING_PHRASES = set()
    return _HEADING_PHRASES


def is_plausible_skill_phrase(phrase: str) -> bool:
    """True when ``phrase`` is worth human review as a candidate skill.

    The review queue is only useful if it holds *skills*, so this rejects the four
    classes of noise that historically flooded it: raw HTML/CSS, section headings,
    footer/CTA boilerplate, and ordinary prose. Defence in depth for issue #8 — see
    reasoning/2026-10-19-taxonomy-candidate-queue-pollution.md.
    """
    cleaned = re.sub(r"\s+", " ", (phrase or "").strip().lower())
    # Normalise curly apostrophes so heading/boilerplate lookups match ("what you’ll do").
    cleaned = cleaned.replace("\u2019", "'").replace("\u2018", "'")
    if len(cleaned) < 3 or len(cleaned) > 60:
        return False
    if not re.search(r"[a-z]", cleaned):
        return False
    # 1. Raw markup (tags, split fragments, entities) is never a skill.
    if _HTML_TAG_RE.search(cleaned):
        return False
    # 2. CSS/style leftovers.
    if _CSS_RE.search(cleaned):
        return False
    bare = cleaned.strip(" .:;,-–—?!")
    # 3. Bare section headings and footer/CTA boilerplate are structure, not requirements.
    if bare in _heading_phrases():
        return False
    if _NOISE_PHRASE_RE.search(bare):
        return False
    if _ID_RE.match(bare):
        return False
    # 4. Prose, fragments, and lexically empty phrases.
    if len(cleaned.split()) > 6:
        return False
    if _PROSE_LEAD_RE.match(cleaned) or _FRAGMENT_RE.match(cleaned):
        return False
    words = bare.split()
    if not words or all(w in _FUNCTION_WORDS for w in words):
        return False
    if len(words) == 1 and len(words[0]) < 3:
        return False
    return True


def record_skill_candidate(phrase: str) -> None:
    """Record a JD phrase that matched neither the taxonomy nor the resume.

    This is a *review queue*, not a scoring input: candidates graduate into the
    taxonomy only when a human approves them (or a future promotion rule fires).
    Noise (markup, headings, prose) is rejected up front — see
    :func:`is_plausible_skill_phrase`.
    """
    cleaned = re.sub(r"\s+", " ", (phrase or "").strip().lower())
    if not is_plausible_skill_phrase(cleaned):
        return
    with _candidates_lock:
        try:
            data = {}
            if CANDIDATES_PATH.exists():
                data = json.loads(CANDIDATES_PATH.read_text("utf-8")) or {}
            if not isinstance(data, dict):
                data = {}
            counts = data.get("counts") if isinstance(data.get("counts"), dict) else {}
            counts[cleaned] = int(counts.get(cleaned, 0)) + 1
            data["version"] = 1
            data["counts"] = counts
            CANDIDATES_PATH.parent.mkdir(parents=True, exist_ok=True)
            CANDIDATES_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001 - telemetry must never break scoring
            logger.debug("Could not record skill candidate %r: %s", phrase, exc)


def purge_candidates(path: Optional[Path] = None) -> "Tuple[int, int]":
    """Drop entries that no longer pass :func:`is_plausible_skill_phrase`.

    One-off drain for the historical queue, which was written before the capture filter
    existed (see reasoning/2026-10-19-taxonomy-candidate-queue-pollution.md). Survivors
    keep their counts, so re-running is safe and loses nothing.

    Returns ``(removed, kept)``; ``(0, 0)`` when there is no queue file.
    """
    target = path or CANDIDATES_PATH
    with _candidates_lock:
        try:
            if not target.exists():
                return (0, 0)
            data = json.loads(target.read_text("utf-8")) or {}
            counts = data.get("counts") if isinstance(data.get("counts"), dict) else {}
            if not counts:
                return (0, 0)
            kept = {k: int(v) for k, v in counts.items() if is_plausible_skill_phrase(k)}
            removed = len(counts) - len(kept)
            data["version"] = 1
            data["counts"] = kept
            target.write_text(json.dumps(data, indent=2), encoding="utf-8")
            return (removed, len(kept))
        except Exception as exc:  # noqa: BLE001 - maintenance must never raise at import
            logger.debug("Could not purge skill candidates: %s", exc)
            return (0, 0)
