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
from typing import Dict, List, Optional, Set

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
    "benefits", "case management", "leave management",
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


def record_skill_candidate(phrase: str) -> None:
    """Record a JD phrase that matched neither the taxonomy nor the resume.

    This is a *review queue*, not a scoring input: candidates graduate into the
    taxonomy only when a human approves them (or a future promotion rule fires).
    """
    cleaned = re.sub(r"\s+", " ", (phrase or "").strip().lower())
    if len(cleaned) < 3 or len(cleaned) > 60:
        return
    if not re.search(r"[a-z]", cleaned):
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
