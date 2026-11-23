"""Title/employer parsing for job cards whose title and company arrived concatenated.

LinkedIn job cards are frequently rendered collapsed, so the scraper's ``innerText`` arrives as
one string:

    "Principal Core Infrastructure Engineer (Verified job)Principal Core Infrastructure Engineer Oracle"
    "Senior Staff EngineerArcesium"
    "Principal Engineer - Member Experience Platform Skipton Building Society"
    "Staff Backend Engineer - Python, FastAPI | AI FinTech | London HybridOwen Thomas | B Corp™"

Splitting that reliably is what keeps the employer out of the title and out of a catch-all bucket
company ("LinkedIn Network", "Unknown Company"). Every split carries a confidence so callers can
apply the trustworthy ones automatically and merely report the rest:

* ``high``   - an unambiguous boundary (duplicated role, a known employer).
* ``medium`` - a structural boundary (known name inside the tail, camel case, separator segment,
  team qualifier, corporate suffix).
* ``low``    - a guess that is reported but must not be written.
* ``none``   - no boundary found.

Employer names are additionally canonicalised through :func:`resolve_company_name`, which consults
``data/company_synonyms.json`` and the existing company table so "Uber Technologies Inc",
"Uber Technologies" and "Uber" resolve to one row instead of three.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

_SYNONYMS_FILE = Path(__file__).resolve().parent.parent / "data" / "company_synonyms.json"
_synonyms_cache: Optional[dict] = None

# Words that mark the end of a role and the start of an employer in a merged title.
_ROLE_WORDS = frozenset(
    {
        "engineer", "engineering", "architect", "developer", "manager", "analyst", "scientist",
        "lead", "designer", "consultant", "specialist", "director", "administrator", "programmer",
        "sre", "devops", "staff", "intern", "recruiter", "technician", "officer", "development",
    }
)

# Team/product qualifiers that belong to the role, not to the employer.
_TEAM_WORDS = frozenset(
    {
        "ai", "ml", "platform", "platforms", "team", "group", "labs", "lab", "cloud", "data",
        "infrastructure", "products", "product", "solutions", "architecture", "technology",
        "engineering", "member", "experience", "applied", "core", "systems", "services", "digital",
        "software", "backend", "frontend", "full", "stack", "security", "payments", "growth",
        "marketing", "sales", "enterprise", "customer", "commercial", "consumer", "retail",
        "finance", "operations", "internal", "tools", "quality", "automation", "observability",
    }
)

# Legal entity forms: dropped when canonicalising an employer name.
_LEGAL_SUFFIX_WORDS = frozenset(
    {
        "inc", "ltd", "llc", "llp", "corp", "corporation", "gmbh", "plc", "bv", "nv", "ag", "sa",
        "sas", "pty", "limited", "co", "company",
    }
)

# Descriptive tails an employer name may end with ("Skipton Building Society", "Fuse Energy").
# These are kept in the name but used to anchor where the employer starts.
_DESCRIPTOR_SUFFIX_WORDS = frozenset(
    {
        "technologies", "technology", "software", "solutions", "systems", "group", "health",
        "energy", "exchange", "networks", "network", "international", "society", "bank", "media",
        "analytics", "consulting", "partners", "holdings", "ventures", "capital", "studios",
        "interactive", "entertainment", "industries", "global", "worldwide", "associates",
        "advisory", "financial", "insurance", "pharma", "labs", "robotics", "motors", "airlines",
    }
)

_BUCKET_COMPANIES = frozenset(
    {"", "unknown company", "linkedin", "linkedin network", "linkedin job alert", "direct",
     "discovered employer", "open role"}
)

# Tokens that can never be an employer name on their own (junk company rows created by earlier
# parsers: "AI/ML", "Data", "Platform"). Matching one of these would re-create the same junk.
_NON_EMPLOYER_TOKENS = frozenset(
    {"ai", "ml", "data", "cloud", "platform", "platforms", "engineering", "software", "systems",
     "solutions", "products", "services", "team", "group", "labs", "and", "&", "of", "the"}
)

_VERIFIED = re.compile(r"\(\s*verified job\s*\)", re.I)
_WITH_VERIFICATION = re.compile(r"\s+with verification\b", re.I)
_TRAILING_LANGUAGE = re.compile(
    r"\s*[-–—|]\s*(english|hindi|german|french|spanish|portuguese)\s*$", re.I
)
_CAMEL = re.compile(r"(?<=[a-z\)])(?=[A-Z])")
_SEPARATORS = " \t·-–—|,:;"
_ROLE_RE = re.compile(r"\b(" + "|".join(sorted(_ROLE_WORDS)) + r")\b", re.I)
_MAX_EMPLOYER_WORDS = 4


def _load_synonyms() -> dict:
    """Loads ``data/company_synonyms.json`` as ``{canonical: [alias, ...]}``."""
    global _synonyms_cache
    if _synonyms_cache is not None:
        return _synonyms_cache
    data: dict = {}
    try:
        if _SYNONYMS_FILE.exists():
            raw = json.loads(_SYNONYMS_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                data = {
                    str(k).strip(): [str(a).strip() for a in v if str(a).strip()]
                    for k, v in raw.items()
                    if str(k).strip() and isinstance(v, list)
                }
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(f"Could not load company synonyms: {exc}")
    _synonyms_cache = data
    return data


def normalize_title(title: str) -> str:
    """Collapses card markup noise ("(Verified job)", language tags) into a plain title."""
    s = _VERIFIED.sub(" ", title or "")
    s = _WITH_VERIFICATION.sub("", s)
    s = re.sub(r"\s+", " ", s).strip()
    return _TRAILING_LANGUAGE.sub("", s).strip()


def _norm_key(name: str) -> str:
    """Lowercase alphanumeric key used for space/punctuation-insensitive comparison."""
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def _words(name: str) -> List[str]:
    return re.findall(r"[A-Za-z0-9&.+'’\-]+", name or "")


def _strip_legal_suffix(name: str) -> str:
    words = _words(name)
    while len(words) > 1 and words[-1].lower().strip(".") in _LEGAL_SUFFIX_WORDS:
        words.pop()
    return " ".join(words).strip(" .,")


def _is_employer_like(text: str) -> bool:
    return bool(text) and text[0].isupper() and len(text) >= 2


def resolve_company_name(name: str, known_companies: Iterable[str] = ()) -> str:
    """Canonicalises an employer name so synonyms do not create duplicate company rows.

    Resolution order: exact match against the known names, the synonym table, then a normalized
    comparison that ignores spaces, punctuation, legal suffixes and trailing descriptors
    ("Uber Technologies Inc" -> "Uber" when "Uber" is known).
    """
    candidate = (name or "").strip().strip(" .,;|")
    candidate = re.sub(r"\s*[|·]\s*.*$", "", candidate).strip()  # drop "| B Corp™" style brands
    candidate = candidate.replace("™", "").strip()
    if not candidate:
        return ""

    known = [k for k in known_companies if k and not is_bucket_company(k)]
    by_lower = {k.lower(): k for k in known}
    if candidate.lower() in by_lower:
        return by_lower[candidate.lower()]

    synonyms = _load_synonyms()
    cand_key = _norm_key(candidate)
    for canonical, aliases in synonyms.items():
        if cand_key in {_norm_key(a) for a in [canonical, *aliases]}:
            return by_lower.get(canonical.lower(), canonical)

    # Longest known name that prefixes the candidate on a word boundary.
    best: Tuple[int, str] = (0, "")
    for k in known:
        k_key = _norm_key(k)
        if len(k_key) < 3:
            continue
        if cand_key.startswith(k_key) and len(k_key) > best[0]:
            best = (len(k_key), k)
    if best[1]:
        return best[1]

    stripped = _strip_legal_suffix(candidate)
    stripped_key = _norm_key(stripped)
    for k in known:
        if stripped_key and _norm_key(k) == stripped_key:
            return k
    for canonical, aliases in synonyms.items():
        if stripped_key and stripped_key in {_norm_key(a) for a in [canonical, *aliases]}:
            return by_lower.get(canonical.lower(), canonical)
    return stripped or candidate


# Placeholder employer values that mean "no employer was parsed". Deliberately narrower than
# ``_BUCKET_COMPANIES``: this decides whether a *fallback* label is substituted, so widening it
# would relabel real bucket rows ("LinkedIn Network") at write time.
_UNKNOWN_COMPANY_NAMES = frozenset({"", "unknown company", "unknown", "n/a", "none", "null"})


def safe_company_name(name: Optional[str], fallback: str) -> str:
    """Returns a usable employer name, substituting ``fallback`` for empty/placeholder values."""
    cleaned = (name or "").strip()
    return cleaned if cleaned.lower() not in _UNKNOWN_COMPANY_NAMES else fallback


def is_bucket_company(name: str) -> bool:
    """True for the catch-all employer placeholders the scraper falls back to."""
    return (name or "").strip().lower() in _BUCKET_COMPANIES


def looks_like_role(text: str) -> bool:
    """True when the text names a role ("Senior Staff Engineer", "Technical Architect")."""
    return bool(_ROLE_RE.search((text or "").strip()))


def _known_name_inside(tail: str, known: Sequence[str]) -> str:
    """Longest known employer whose *word sequence* appears in ``tail`` (never a substring).

    Substring matching is wrong here: ``"Connect Tech+Talent"`` normalized contains ``"ttec"``,
    which would resolve the employer to TTEC. A match followed by a separator is rejected too:
    the employer is the final segment, not an acronym that appears earlier in the tail.
    """
    words = _words(tail)
    lower = [w.lower() for w in words]
    best: Tuple[int, int] = (0, -1)
    for name in known:
        name_words = [w.lower() for w in _words(name)]
        if not name_words or any(w in _NON_EMPLOYER_TOKENS for w in name_words):
            continue
        span = len(name_words)
        for start in range(len(lower) - span + 1):
            if lower[start:start + span] != name_words or span <= best[0]:
                continue
            remainder = " ".join(words[start + span:])
            if any(sep in remainder for sep in (" - ", " – ", " — ", "|")):
                continue
            best = (span, start)
    if best[1] < 0:
        return ""
    return " ".join(words[best[1]:])


def _camel_employer(tail: str) -> Tuple[str, str]:
    """(employer, consumed_left) after a lowercase->uppercase boundary in ``tail``."""
    parts = _CAMEL.split(tail)
    if len(parts) < 2:
        return "", ""
    # Rebuild the split offsets so the employer keeps its original spelling.
    offsets, pos = [], 0
    for part in parts:
        offsets.append(pos)
        pos += len(part)
    for idx in range(1, len(parts)):
        right = tail[offsets[idx]:].strip(_SEPARATORS)
        left = tail[: offsets[idx]].strip(_SEPARATORS)
        if len(right) < 3 or not _is_employer_like(right):
            continue
        # Only a boundary after role text (or after a team/comp token) is a real employer start.
        left_words = _words(left)
        last = left_words[-1].lower() if left_words else ""
        left_ok = bool(
            last and (
                _ROLE_RE.search(last)
                or last in _TEAM_WORDS
                or last.strip(".") in _DESCRIPTOR_SUFFIX_WORDS
            )
        )
        if left_ok or re.search(r"[£$€0-9]", left):
            return right, left
    return "", ""


def _strip_team_prefix(text: str) -> str:
    """Drops leading team qualifiers and connectors ("AI Platform Addepar" -> "Addepar")."""
    words = _words(text)
    idx = 0
    while idx < len(words) - 1 and words[idx].lower() in (_TEAM_WORDS | {"and", "&", "of", "for"}):
        idx += 1
    return " ".join(words[idx:])


def _suffix_anchor(text: str) -> str:
    """Cuts back to the last corporate/descriptor suffix word ("... Baxter International")."""
    words = _words(text)
    anchor = -1
    for idx, word in enumerate(words):
        if word.lower().strip(".") in _DESCRIPTOR_SUFFIX_WORDS:
            anchor = idx
    if anchor < 0:
        return ""
    start = anchor
    while start > 0 and anchor - start < 3:
        prev = words[start - 1].lower()
        if prev in _TEAM_WORDS or _ROLE_RE.search(prev):
            break
        start -= 1
    return " ".join(words[start:])


def _employer_from_tail(tail: str, known: Sequence[str]) -> Tuple[str, str, str]:
    """Returns (employer, confidence, camel_left) for the text after the last role word."""
    candidate = tail.strip(_SEPARATORS)
    if not candidate:
        return "", "none", ""

    inside = _known_name_inside(candidate, known)
    if inside:
        return inside, "medium", ""

    camel, left = _camel_employer(candidate)
    if camel:
        return camel, "medium", left

    for sep in ("|", " - ", " – ", " — "):
        if sep in candidate:
            candidate = candidate.rsplit(sep, 1)[-1].strip(_SEPARATORS)
            if not candidate:
                return "", "none", ""

    # "Software (Communication Platforms & Architecture), Baxter International":
    # peel team qualifiers, then anchor on the corporate/descriptor suffix.
    trimmed = _strip_team_prefix(candidate)
    if trimmed and _norm_key(trimmed) != _norm_key(candidate):
        candidate = trimmed
    anchored = _suffix_anchor(candidate)
    if anchored and _norm_key(anchored) != _norm_key(candidate):
        candidate = anchored

    words = _words(candidate)
    if not words or not _is_employer_like(candidate):
        return "", "none", ""
    # "Staff Software Engineer - Enterprise Platform": a tail made only of team/role qualifiers is a
    # role qualifier, not an employer.
    if all(
        w.lower() in (_TEAM_WORDS | _DESCRIPTOR_SUFFIX_WORDS | _ROLE_WORDS | {"and", "&", "of", "for"})
        for w in words
    ):
        return "", "none", ""
    # "Leadcrewlia": a role word glued to a lowercase name is not a boundary we can trust.
    single = candidate.lower()
    if len(words) == 1 and any(single.startswith(rw) and single != rw for rw in _ROLE_WORDS):
        return "", "none", ""
    if len(words) > 1 and _ROLE_RE.search(words[0]) and words[1][:1].islower():
        return "", "none", ""
    if len(words) <= _MAX_EMPLOYER_WORDS:
        return candidate, "medium", ""
    return candidate, "low", ""


def split_role_employer(
    title: str, known_companies: Iterable[str] = ()
) -> Tuple[str, str, str]:
    """Splits a merged "role + employer" card title.

    Returns ``(role, employer, confidence)`` where confidence is ``high``/``medium``/``low``/
    ``none``. Callers should persist ``high``/``medium`` and only report the rest.
    """
    s = normalize_title(title)
    if not s:
        return "", "", "none"
    known = [k for k in known_companies if k and not is_bucket_company(k)]

    # 1) The card duplicated the role: "<role> <role> <employer>".
    for k in range(len(s) // 2, 8, -1):
        head, tail = s[:k].strip(_SEPARATORS), s[k:].strip(_SEPARATORS)
        if tail.lower().startswith(head.lower()):
            rest = tail[len(head):].strip(_SEPARATORS)
            if rest and _ROLE_RE.search(head):
                return head, resolve_company_name(rest, known) or rest, "high"

    # 2) A known employer at the end of the title.
    s_key = _norm_key(s)
    for name in sorted(known, key=lambda n: -len(n)):
        key = _norm_key(name)
        if len(key) < 3 or not s_key.endswith(key) or s_key == key:
            continue
        if any(w in _NON_EMPLOYER_TOKENS for w in (w.lower() for w in _words(name))):
            continue
        role = s[: len(s) - len(name)].strip(_SEPARATORS)
        if role and _ROLE_RE.search(role):
            return role, name, "high"

    # 3) Split at the last role word and read the employer off the tail.
    for match in reversed(list(_ROLE_RE.finditer(s))):
        tail = s[match.end():]
        if not tail.strip(_SEPARATORS):
            continue
        role = s[: match.end()].strip(_SEPARATORS)
        employer, confidence, camel_left = _employer_from_tail(tail, known)
        if not employer or not role:
            continue
        if _norm_key(employer) and _norm_key(employer) in _norm_key(role):
            continue  # the "employer" is just the role repeated
        # "Senior Staff EngineerArcesium": the glued role text belongs to the role.
        if camel_left and "|" not in camel_left and len(_words(camel_left)) <= 6:
            role = f"{role} {camel_left}".strip(_SEPARATORS)
        return role, resolve_company_name(employer, known) or employer, confidence

    # 4) Camel-case concatenation anywhere ("Senior Staff EngineerArcesium").
    camel, left = _camel_employer(s)
    if camel and left and _ROLE_RE.search(left):
        return left, resolve_company_name(camel, known) or camel, "medium"

    return "", "", "none"
