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
    # Portal UI chrome/actions that can trail a standalone "N+ years" badge
    # (e.g. "... 3+ Years  View job", "3+ years  Apply  Save").
    "view", "apply", "save", "share", "bookmark", "follow", "login", "sign",
    "register", "more", "details", "description", "overview", "print", "copy",
}

_EXPERIENCE_PATTERNS = [
    (re.compile(rf"{_YEARS_NUM}\s*(?:\+|plus)\s*{_YEARS_UNIT}", re.I), "min"),
    (re.compile(rf"{_YEARS_NUM}\s*(?:-|–|—|to)\s*{_YEARS_NUM}\s*(?:or\s+more\s+)?{_YEARS_UNIT}", re.I), "range"),
    (re.compile(rf"(?:at least|minimum(?: of)?|min\.?)\s*{_YEARS_NUM}\s*-?\s*{_YEARS_UNIT}", re.I), "min"),
    (re.compile(rf"(?:more than|over)\s*{_YEARS_NUM}\s*-?\s*{_YEARS_UNIT}", re.I), "min"),
    (re.compile(rf"{_YEARS_NUM}\s*-?\s*{_YEARS_UNIT}\s+(?:of\s+)?(?:relevant\s+|professional\s+|industry\s+)?experience", re.I), "min"),
]

# Horizontal whitespace only: a technology reference is inline. A standalone
# "N+ years" badge lives on its own line, so it must NOT reach across a newline
# to grab the next capitalized word (e.g. "3+ years\nApply" or "...3+ Years View job").
_SCOPE_FOLLOW = re.compile(r"[ \t]*(?:(?:of|in|with)[ \t]+)?([A-Za-z][\w+#/-]{1,29})")

# "N years experience in/with <Tech>" — skip the connector and capture the technology.
_EXPERIENCE_CONNECTOR = re.compile(
    r"\s*(?:of\s+)?(?:relevant\s+|professional\s+|industry\s+|hands-on\s+)?"
    r"experience\s+(?:in|with|of)?\s*([A-Za-z][\w+#/.-]{1,29})",
    re.I,
)

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
    ("master", ("master", "m.s.", "m.s", "m.sc", "m.sc.", "mba", "msc", "ms", "ma", "m.tech", "mtech", "m.e.", "m.eng")),
    ("bachelor", ("bachelor", "b.s.", "b.s", "b.sc", "b.sc.", "bsc", "bs", "ba", "undergraduate", "b.tech", "btech", "b.e.", "b.eng")),
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

# Precompiled word-boundary patterns per cert (order preserved for stable output).
_CERT_RES = {
    cert: re.compile(r"(?<![A-Za-z0-9])" + re.escape(cert.lower()) + r"(?![A-Za-z0-9])")
    for cert in CERT_CATALOG
}


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
            tail = text[m.end():]
            follow = _EXPERIENCE_CONNECTOR.match(tail) or _SCOPE_FOLLOW.match(tail)
            if follow:
                token = follow.group(1).rstrip(".-")
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


# Words that disambiguate a bare degree term from its ordinary use (e.g. "master
# debugger", "MS Teams"). A context-gated token only counts when one of these appears
# within a few following tokens, or when it sits in a slash/comma-joined degree cluster
# (e.g. "BS/MS/PhD in Computer Science").
_EDU_CONTEXT_WORDS = (
    "degree", "of", "in", "science", "engineering", "technology",
    "administration", "arts", "business", "computer", "sciences", "management",
    "undergraduate", "graduate", "bachelor", "master", "bs", "ba", "ms", "ma",
    "bsc", "msc", "phd",
)

# Tokens that are also ordinary words/acronyms and therefore require a nearby degree
# context word (or another degree token) before counting. Maps token -> education level.
_EDU_CONTEXT_GATED = {
    "master": "master", "bachelor": "bachelor",
    "bs": "bachelor",  # BS = Bachelor of Science
    "ba": "bachelor",
    "ms": "master",    # MS = Master of Science
    "ma": "master",
}


def _edu_word(tok: str) -> "re.Pattern":
    return re.compile(
        r"(?<![A-Za-z0-9])" + re.escape(tok.lower()) + r"(?![A-Za-z0-9])", re.I
    )


# A context-gated token followed (within ~5 tokens, crossing "/", ",", "&") by a degree
# context word. This matches "master's degree", "bachelor's or master's degree", and
# "BS/MS/PhD in Computer Science", while rejecting "master debugger" / "MS Teams".
_EDU_CONTEXT_ALT = "|".join(re.escape(w) for w in _EDU_CONTEXT_WORDS)
_EDU_SEP = r"[\s/,&]+"
_EDU_CONTEXT_RE = {
    term: re.compile(
        rf"(?<![A-Za-z0-9])(?:{term})(?:'s|s)?(?![A-Za-z0-9])"
        rf"(?=(?:{_EDU_SEP}(?:\w+(?:'s)?|\d{{4}})){{0,5}}{_EDU_SEP}(?:{_EDU_CONTEXT_ALT})\b)",
        re.I,
    )
    for term in _EDU_CONTEXT_GATED
}


def _edu_token_re(level: str, tok: str) -> "re.Pattern":
    """Pattern for one (level, token): context-window for ambiguous tokens, else boundary."""
    if _EDU_CONTEXT_GATED.get(tok) == level:
        return _EDU_CONTEXT_RE[tok]
    return _edu_word(tok)


def parse_education(text: str) -> List[str]:
    """Return the education levels referenced in the text (e.g. ["bachelor"]).

    Uses word-boundary matching so ordinary words like "master debugger" do not get
    misread as a degree. Bare degree terms/acronyms ("master", "bachelor", "BS", "MS")
    only count when a degree-context word (or another degree token) appears within a few
    following tokens — e.g. "master's degree", "bachelor's or master's degree", or
    "BS/MS/PhD in Computer Science".
    """
    lowered = (text or "").lower()
    levels = []
    for level, tokens in _EDUCATION:
        if any(_edu_token_re(level, tok).search(lowered) for tok in tokens):
            levels.append(level)
    return levels


def parse_certifications(text: str) -> List[str]:
    """Return catalog certifications named in the text.

    Uses word-boundary matching so a short acronym like ``CKA`` is not matched inside
    a longer word (e.g. ``CKAD``), while standalone ``CKA``/``CKS`` and multi-word
    names like ``AWS Certified Solutions Architect`` still match.
    """
    lowered = (text or "").lower()
    return [cert for cert, pat in _CERT_RES.items() if pat.search(lowered)]


def cert_in_text(cert: str, text: str) -> bool:
    """True if ``cert`` appears as a standalone token in ``text`` (word-boundary)."""
    pat = _CERT_RES.get(cert)
    if pat is None:
        pat = re.compile(r"(?<![A-Za-z0-9])" + re.escape((cert or "").lower()) + r"(?![A-Za-z0-9])")
    return bool(pat.search((text or "").lower()))


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
