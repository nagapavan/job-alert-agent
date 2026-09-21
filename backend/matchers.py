# -------------------------------------------------------------
# Shared role-title matching
# -------------------------------------------------------------
# Lives here (not in main.py) so the ATS scrapers' early pre-filter uses the SAME
# semantics as the final match check. A stricter pre-filter silently starves the
# interactive matcher and produces empty scans.
from typing import Optional

ROLE_EXPANSIONS = {
    "eng manager": [
        "engineering manager",
        "eng manager",
        "engineering lead",
        "em",
        "director of engineering",
        "engineering director",
        "manager",
    ],
    "engineering manager": [
        "engineering manager",
        "eng manager",
        "engineering lead",
        "em",
        "director of engineering",
        "engineering director",
        "manager",
    ],
    "staff engineer": [
        "staff",
        "staff engineer",
        "staff software",
        "principal",
        "lead",
        "senior staff",
    ],
    "staff software engineer": [
        "staff",
        "staff engineer",
        "staff software",
        "principal",
        "lead",
        "senior staff",
    ],
    "principal engineer": [
        "principal",
        "staff",
        "distinguished",
        "architect",
        "lead",
        "fellow",
    ],
    "backend": [
        "backend",
        "back-end",
        "server",
        "core",
        "infrastructure",
        "systems",
        "platform",
        "software engineer",
    ],
    "backend engineer": [
        "backend",
        "back-end",
        "server",
        "core",
        "infrastructure",
        "systems",
        "platform",
        "software engineer",
    ],
    "distributed systems": [
        "distributed",
        "systems",
        "backend",
        "infrastructure",
        "storage",
        "database",
        "platform",
        "cloud",
    ],
    "full stack": [
        "full stack",
        "fullstack",
        "full-stack",
        "software engineer",
        "frontend",
        "web",
        "application",
    ],
    "full stack engineer": [
        "full stack",
        "fullstack",
        "full-stack",
        "software engineer",
        "frontend",
        "web",
        "application",
    ],
    "platform engineer": [
        "platform",
        "infra",
        "infrastructure",
        "systems",
        "cloud",
        "reliability",
        "sre",
        "devops",
    ],
    "ai / ml": [
        "machine learning",
        "ml",
        "ai",
        "deep learning",
        "nlp",
        "computer vision",
        "research engineer",
        "technical staff",
        "mts",
        "scientist",
    ],
    "machine learning engineer": [
        "machine learning",
        "ml",
        "ai",
        "deep learning",
        "nlp",
        "computer vision",
        "research engineer",
        "technical staff",
        "mts",
        "scientist",
    ],
}


def check_title_match(job_title: str, title_filters: list[str] | None) -> bool:
    if not title_filters:
        return True
    jt_lower = (job_title or "").lower()

    for tf in title_filters:
        tf_clean = (tf or "").strip().lower()
        if not tf_clean:
            continue
        if tf_clean in jt_lower:
            return True
        if tf_clean in ROLE_EXPANSIONS:  # noqa: SIM102
            if any(syn in jt_lower for syn in ROLE_EXPANSIONS[tf_clean]):
                return True
        words = [
            w
            for w in tf_clean.split()
            if len(w) >= 4 and w not in ("software", "engineer")
        ]
        if words and any(w in jt_lower for w in words):
            return True
    return False


def detect_foreign_restriction(
    combined_text: str, clean_loc_filters: list[str]
) -> bool:
    """
    Detects if a job listing is restricted to a foreign country/region that excludes ALL of
    the candidate's target countries (e.g. "Remote US", "US-PA-Remote", "Remote Canada/US").

    Backed by the offline ``backend.geo`` resolver (geonamescache + pycountry), so it scales
    to every country/city instead of a hardcoded list.
    """
    if not clean_loc_filters:
        return False
    if not (combined_text or "").strip():
        return False

    from backend.geo import countries_for_terms, is_foreign_restricted

    target_iso = countries_for_terms(clean_loc_filters)
    if not target_iso:
        return False
    return is_foreign_restricted(combined_text, target_iso)
