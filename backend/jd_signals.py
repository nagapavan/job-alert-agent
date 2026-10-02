"""Deterministic parsers for structured job-description signals.

These cover the pattern-based requirements (years, seniority, education, certifications)
so the LLM is only needed for genuinely semantic extraction. English-only.
"""
from __future__ import annotations

import re
from typing import List, Optional, TypedDict

# A candidate is "over-qualified" when they exceed a *total* experience minimum by this margin.
OVER_QUALIFICATION_YEARS_MARGIN = 5.0

_YEARS_NUM = r"(\d{1,2}(?:\.\d)?)"
_YEARS_UNIT = r"(?:years?|yrs?)"

# Words that follow "N+ years of/in/with ..." but are not a specific technology.
_NON_TECHNOLOGY = {
    "experience", "professional", "industry", "software", "engineering",
    "related", "work", "hands-on", "handson", "relevant", "technical",
    "development", "product", "commercial", "production", "full", "total",
}

_EXPERIENCE_PATTERNS = [
    (re.compile(rf"{_YEARS_NUM}\s*(?:\+|plus)\s*{_YEARS_UNIT}", re.I), "min"),
    (re.compile(rf"{_YEARS_NUM}\s*(?:-|–|—|to)\s*{_YEARS_NUM}\s*{_YEARS_UNIT}", re.I), "range"),
    (re.compile(rf"(?:at least|minimum(?: of)?|min\.?)\s*{_YEARS_NUM}\s*{_YEARS_UNIT}", re.I), "min"),
    (re.compile(rf"{_YEARS_NUM}\s*{_YEARS_UNIT}\s+(?:of\s+)?(?:relevant\s+|professional\s+|industry\s+)?experience", re.I), "min"),
]

_SCOPE_FOLLOW = re.compile(r"\s*(?:(?:of|in|with)\s+)?([A-Za-z][\w+#/-]{1,29})")

_SENIORITY = [
    ("director", ("director", "head of", "vp", "vice president", "chief")),
    ("manager", ("manager", "management")),
    ("principal", ("principal", "distinguished", "fellow", "architect")),
    ("staff", ("staff",)),
    ("lead", ("lead", "leader", "tech lead", "team lead")),
    ("senior", ("senior", "sr.", "sr ")),
    ("mid", ("mid-level", "mid level", "intermediate")),
    ("junior", ("junior", "jr.", "entry", "entry-level", "graduate", "associate")),
    ("intern", ("intern", "trainee", "apprentice")),
]

_EDUCATION = [
    ("phd", ("phd", "ph.d", "doctorate", "doctoral")),
    ("master", ("master", "m.s.", "m.sc", "mba", "msc")),
    ("bachelor", ("bachelor", "b.s.", "b.sc", "bsc", "undergraduate")),
    ("associate", ("associate degree", "associate's")),
    ("high_school", ("high school", "diploma")),
]

CERT_CATALOG = (
    "AWS Certified", "AWS Solutions Architect", "Azure", "Google Cloud", "GCP",
    # Kubernetes / CNCF
    "CKA", "CKAD", "CKS", "KCNA", "KCSA",
    "Certified Kubernetes Administrator", "Certified Kubernetes Application Developer",
    "Certified Kubernetes Security Specialist", "Kubernetes and Cloud Native Associate",
    "Kubernetes and Cloud Native Security Associate", "CNCF",
    # Architecture / delivery / security
    "TOGAF", "Terraform", "PMP", "Scrum Master", "CISSP", "CCNA",
    "ITIL", "SnowPro", "Databricks", "Kafka", "Salesforce Certified",
)


class ExperienceHint(TypedDict):
    min_years: Optional[float]
    max_years: Optional[float]
    scope: str            # "total" | "technology"
    technology: Optional[str]
    snippet: str


def _to_float(value: str) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_experience(text: str) -> List[ExperienceHint]:
    """Find experience requirements, tagging each as total or technology-specific."""
    if not text:
        return []
    hints: List[ExperienceHint] = []
    seen = set()
    for pattern, kind in _EXPERIENCE_PATTERNS:
        for m in pattern.finditer(text):
            groups = m.groups()
            if kind == "range" and len(groups) >= 2 and groups[0] and groups[1]:
                lo, hi = _to_float(groups[0]), _to_float(groups[1])
            else:
                lo = _to_float(next((g for g in groups if g), ""))
                hi = None
            if lo is None:
                continue
            scope, technology = "total", None
            follow = _SCOPE_FOLLOW.match(text[m.end():])
            if follow:
                token = follow.group(1)
                if token.lower() not in _NON_TECHNOLOGY and token[:1].isupper():
                    scope, technology = "technology", token
            key = (lo, hi, scope, technology)
            if key in seen:
                continue
            seen.add(key)
            hints.append({
                "min_years": lo,
                "max_years": hi,
                "scope": scope,
                "technology": technology,
                "snippet": m.group(0).strip(),
            })
    return hints


def seniority_level(title: str) -> str:
    """Map a job title to a coarse seniority level, or "unknown"."""
    lowered = (title or "").lower()
    for level, tokens in _SENIORITY:
        if any(tok in lowered for tok in tokens):
            return level
    return "unknown"


def parse_education(text: str) -> List[str]:
    """Return the education levels referenced in the text (e.g. ["bachelor"])."""
    lowered = (text or "").lower()
    return [level for level, tokens in _EDUCATION if any(t in lowered for t in tokens)]


def parse_certifications(text: str) -> List[str]:
    """Return catalog certifications named in the text."""
    lowered = (text or "").lower()
    return [cert for cert in CERT_CATALOG if cert.lower() in lowered]


def is_over_qualified(candidate_years: Optional[float], hint: ExperienceHint) -> bool:
    """True only for a *total* experience minimum/range the candidate clearly exceeds.

    Technology-specific callouts never count as over-qualification (more years in a
    specific technology is a strength).
    """
    if candidate_years is None or hint.get("scope") == "technology":
        return False
    if hint.get("max_years") is not None:
        return candidate_years > float(hint["max_years"])
    minimum = hint.get("min_years")
    if minimum is None:
        return False
    return (candidate_years - float(minimum)) >= OVER_QUALIFICATION_YEARS_MARGIN
