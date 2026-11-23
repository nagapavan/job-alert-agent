"""Deterministic classification of the segments a recruiter/alert email header is made of.

LinkedIn renders recruiter outreach and alert cards as pipe-delimited strings, and the only
structured place the employer appears is one of those segments::

    "Message replied: Job Opportunity || Senior Staff Engineer || Sprinto || Remote"

Reading the *last* pipe segment as the employer is how "Remote" became a company row. Every
segment is therefore typed before it is used:

* ``work_mode``  - remote / hybrid / onsite / anywhere
* ``location``   - a city, country or "City, Country" pair (``backend.geo``)
* ``title``      - contains a role word (``backend.title_parser``)
* ``company``    - employer-like, not a bucket placeholder, not a role, not a place
* ``unknown``    - anything else; never guessed at

The classifier is pure string work (no embeddings, no LLM), so callers can run it on every
mail without spending anything.
"""

from __future__ import annotations

import re
from typing import Iterable, Optional, Tuple

from backend.title_parser import (
    is_bucket_company,
    looks_like_role,
    resolve_company_name,
)

# Work-mode markers are locations, never employers. Kept here (not in the scraper) so the
# classifier stays importable without pulling the scraping stack in.
WORK_MODE_TOKENS = frozenset(
    {
        "remote", "hybrid", "onsite", "on-site", "on site", "anywhere", "remote-first",
        "work from home", "wfh", "fully remote", "remote (india)", "remote india",
    }
)

# Words that can never be an employer name on their own.
_NON_EMPLOYER_TOKENS = frozenset(
    {
        "ai", "ml", "data", "cloud", "platform", "platforms", "engineering", "software",
        "systems", "solutions", "products", "services", "team", "group", "labs", "and",
        "&", "of", "the", "job", "role", "opportunity", "hiring", "apply",
    }
)

_TRAILING_PUNCT = re.compile(r"[\s.,;:|\-–—]+$")
_VERIFIED = re.compile(r"\(\s*verified job\s*\)", re.I)
_PIPE_SPLIT = re.compile(r"\s*\|\|?\s*")
_NOTIFICATION_PREFIX = re.compile(r"^\s*(?:message replied|re|fwd|fw)\s*:\s*", re.I)


def _clean(segment: str) -> str:
    text = _VERIFIED.sub(" ", segment or "")
    text = re.sub(r"\s+", " ", text).strip()
    return _TRAILING_PUNCT.sub("", text).strip()


def _is_work_mode(text: str) -> bool:
    lowered = _clean(text).lower()
    if not lowered:
        return False
    if lowered in WORK_MODE_TOKENS:
        return True
    # "Remote, India" / "Remote - India" / "Remote (US)"
    head = re.split(r"[,/(\-–—]", lowered, 1)[0].strip()
    return head in WORK_MODE_TOKENS


_LOCATION_AREA_SUFFIX = re.compile(r"\s+(?:area|region|metro|metropolitan)$", re.I)
_URL_OR_CTA = re.compile(r"://|^(?:view|see|show|browse|apply)\b", re.I)
# Words that may follow a place inside a location phrase ("London Area United Kingdom").
_LOCATION_FILLER = frozenset(
    {
        "area", "region", "metro", "metropolitan", "greater", "united", "kingdom",
        "states", "state", "province", "county", "city", "and", "the", "of", "republic",
    }
)


def _is_location(text: str) -> bool:
    candidate = _clean(text)
    if not candidate:
        return False
    try:
        from backend.geo import is_place_name
    except Exception:  # pragma: no cover - geo is a hard dependency in practice
        return False
    # "London Area United Kingdom" carries no comma: the leading word(s) must be a place and
    # everything after it only location filler. Country-level fuzzy matching is deliberately
    # not used — "Inc" resolves to CN and "Architecture" to US.
    parts = [candidate, *[p.strip() for p in re.split(r"[,/]", candidate)]]
    for part in parts:
        if not part:
            continue
        words = part.split()
        for count in range(min(3, len(words)), 0, -1):
            head = " ".join(words[:count])
            rest = [w.lower().strip(".,") for w in words[count:]]
            if rest and not all(word in _LOCATION_FILLER for word in rest):
                continue
            try:
                if is_place_name(head) or is_place_name(_LOCATION_AREA_SUFFIX.sub("", head)):
                    return True
            except Exception:
                continue
    return False


def _company_value(text: str, known_companies: Iterable[str] = ()) -> str:
    candidate = _clean(text)
    if not candidate or len(candidate) > 60 or len(candidate.split()) > 5:
        return ""
    if _URL_OR_CTA.search(candidate):
        return ""
    if is_bucket_company(candidate):
        return ""
    if any(word.lower().strip(".,") in _NON_EMPLOYER_TOKENS for word in candidate.split()):
        return ""
    if looks_like_role(candidate) or _is_location(candidate):
        return ""
    # Conservative: an unknown lower-case phrase is more likely prose than a brand.
    if not candidate[0].isupper():
        return ""
    return resolve_company_name(candidate, known_companies) or candidate


def classify_segment(
    segment: str, known_companies: Iterable[str] = ()
) -> Tuple[str, str]:
    """Types one header segment. Returns ``(kind, value)``.

    ``kind`` is one of ``work_mode`` / ``location`` / ``title`` / ``company`` / ``unknown``.
    """
    text = _clean(segment)
    if not text:
        return "unknown", ""
    if _is_work_mode(text):
        return "work_mode", text
    if _is_location(text):
        return "location", text
    if looks_like_role(text):
        return "title", text
    company = _company_value(text, known_companies)
    if company:
        return "company", company
    return "unknown", text


def parse_pipe_card(
    subject: str, known_companies: Iterable[str] = ()
) -> Optional[dict]:
    """Parses a ``<subject> || <Title> || <Company> || <Location>`` header.

    Segments are typed first and then read positionally: the title is the first segment that
    is a role *and* is followed by a company segment, the company is the first company segment
    after it, and the location is the first location/work-mode segment after the company.
    Anything before the title is the recruiter's own subject and is discarded.

    Returns ``None`` when the header is not a pipe card or no company can be typed — callers
    must leave such a mail unprocessed rather than invent an employer.
    """
    header = (subject or "").strip()
    if header.count("|") < 2:
        return None
    segments = [seg for seg in _PIPE_SPLIT.split(header) if _clean(seg)]
    if len(segments) < 2:
        return None

    kinds = [classify_segment(seg, known_companies) for seg in segments]
    for index, (kind, _value) in enumerate(kinds):
        if kind != "title":
            continue
        company = ""
        location = ""
        for later in range(index + 1, len(segments)):
            later_kind, later_value = kinds[later]
            if not company and later_kind == "company":
                company = later_value
                continue
            if company and not location and later_kind in ("location", "work_mode"):
                location = later_value
                break
        if company:
            return {
                "title": _clean(segments[index]),
                "company": company,
                "location": location or None,
            }
    return None


def split_company_location(text: str) -> Tuple[str, str]:
    """Splits ``"Globex (Remote, India)"`` into ``("Globex", "Remote, India")``."""
    candidate = _clean(text)
    match = re.match(r"^(.*?)\s*\(([^()]{2,60})\)\s*$", candidate)
    if not match:
        return candidate, ""
    head, tail = match.group(1).strip(), match.group(2).strip()
    if not head or not (_is_location(tail) or _is_work_mode(tail)):
        return candidate, ""
    return head, tail


def strip_notification_prefix(text: str) -> str:
    """Drops a leading ``Message replied:`` / ``Re:`` / ``Fwd:`` marker."""
    return _NOTIFICATION_PREFIX.sub("", text or "").strip()
