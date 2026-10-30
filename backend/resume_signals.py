"""Deterministic resume-side signals derived from the parsed resume JSON.

Tenure is computed in **months**, never by subtracting year numbers (issue #55). Year arithmetic
discarded months and ignored employment gaps, so the same timeline could report 15.0 or 18.0
depending on which year it happened to end in.

Gap policy (agreed on #55):

- a gap of **<= 1 month** between roles is a month-granularity artifact (a handover), so it counts
  as contiguous;
- a gap of **>= 2 months** is real unemployment and is subtracted;
- an open role (``Present`` / ``Current``) runs to today.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from backend.jd_signals import seniority_level

_YEAR = re.compile(r"(?:19|20)\d{2}")
_PRESENT = re.compile(r"present|current|now|ongoing|today", re.I)

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_MONTH_YEAR_RE = re.compile(
    r"(?P<mon>jan\w*|feb\w*|mar\w*|apr\w*|may|jun\w*|jul\w*|aug\w*|sep\w*|oct\w*|nov\w*|dec\w*)"
    r"[\s.,\-/]{0,3}(?P<year>(?:19|20)\d{2})",
    re.I,
)
_NUMERIC_MY_RE = re.compile(r"(?P<mon>0?[1-9]|1[0-2])\s*/\s*(?P<year>(?:19|20)\d{2})")

# A gap this long is a month-granularity artifact, not unemployment.
_MATERIAL_GAP_MONTHS = 2

# "17 years 6 months" / "17y 6m" / "17.5"
_YEARS_MONTHS_RE = re.compile(
    r"(?P<y>\d+(?:\.\d+)?)\s*(?:years?|yrs?|y)\b(?:\s*(?:and\s*)?(?P<m>\d+)\s*(?:months?|mos?|m)\b)?",
    re.I,
)
_PLAIN_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")


def _now_months() -> int:
    """Current month as a month index (year * 12 + month - 1)."""
    now = datetime.now(timezone.utc)
    return now.year * 12 + (now.month - 1)


def _month_points(dates: str) -> List[int]:
    """Absolute month indexes found in a date range, in order of appearance.

    Month precision is used when present ("Sept 2022"); a bare year is anchored to January
    (``year * 12``), which keeps "2016 - 2020" exactly 4 years instead of inflating it to 5.
    """
    text = dates or ""
    points = [
        int(m.group("year")) * 12 + int(m.group("mon")) - 1
        for m in _NUMERIC_MY_RE.finditer(text)
    ]
    if not points:
        points = [
            int(m.group("year")) * 12 + _MONTHS[m.group("mon")[:3].lower()] - 1
            for m in _MONTH_YEAR_RE.finditer(text)
        ]
    if points:
        return points
    years = [int(m.group(0)) for m in _YEAR.finditer(text)]
    if not years:
        return []
    return [min(years) * 12, max(years) * 12]


def _entry_interval(dates: str, now_months: int) -> Optional[Tuple[int, int]]:
    """Return ``(start_month, end_month)`` for a date range, or None when unparseable."""
    text = dates or ""
    points = _month_points(text)
    if not points:
        return None
    start = min(points)
    if _PRESENT.search(text):
        return (start, max(start, now_months))
    end = max(points)
    if end < start:
        start, end = end, start
    return (start, end)


def _merge_intervals(intervals: Sequence[Tuple[int, int]]) -> List[Tuple[int, int]]:
    """Merge overlapping/adjacent intervals so shared time is counted once."""
    merged: List[Tuple[int, int]] = []
    for s, e in sorted(intervals):
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def _career_span_months(intervals: Sequence[Tuple[int, int]]) -> int:
    """Career length in months: first start to last end, minus gaps that are real."""
    merged = _merge_intervals(intervals)
    if not merged:
        return 0
    span = merged[-1][1] - merged[0][0]
    gaps = sum(
        later[0] - earlier[1]
        for earlier, later in zip(merged, merged[1:])
        if later[0] - earlier[1] >= _MATERIAL_GAP_MONTHS
    )
    return max(0, span - gaps)


def _override_years(parsed_json: Optional[Dict[str, Any]]) -> Optional[float]:
    """The user's own tenure figure, when they entered one.

    ``PUT /api/resume/{id}/skills`` stores ``experience_years``; it used to be written and never
    read, so entering the correct tenure changed nothing (issue #55).
    """
    raw = (parsed_json or {}).get("experience_years")
    if raw is None or raw == "":
        return None
    if isinstance(raw, (int, float)):
        return float(raw) if raw > 0 else None
    text = str(raw)
    match = _YEARS_MONTHS_RE.search(text)
    if match:
        years = float(match.group("y"))
        months = float(match.group("m") or 0)
        total = years + months / 12.0
        return round(total, 2) if total > 0 else None
    match = _PLAIN_NUMBER_RE.search(text)
    if not match:
        return None
    value = float(match.group(0))
    return value if value > 0 else None


def estimate_total_years(
    parsed_json: Optional[Dict[str, Any]], current_year: Optional[int] = None
) -> Optional[float]:
    """Total experience in fractional years.

    An explicit ``experience_years`` override wins over the derived timeline. Otherwise the span
    from the earliest start to the latest end (or today for an open role) is measured in months,
    with gaps of two months or more subtracted.

    ``current_year`` pins "now" to January of that year; it exists for tests and deterministic
    callers, not for production scoring.
    """
    override = _override_years(parsed_json)
    if override is not None:
        return override

    now_months = current_year * 12 if current_year else _now_months()
    entries = (parsed_json or {}).get("experience") or []
    intervals = [
        interval
        for entry in entries
        if (interval := _entry_interval(str(entry.get("dates") or ""), now_months))
    ]
    if not intervals:
        return None
    return round(_career_span_months(intervals) / 12.0, 2)


def latest_seniority(parsed_json: Optional[Dict[str, Any]]) -> str:
    """Coarse seniority level of the most recent role, or "unknown"."""
    entries = (parsed_json or {}).get("experience") or []
    if not entries:
        return "unknown"
    return seniority_level(str(entries[0].get("title") or ""))


def estimate_technology_years(
    parsed_json: Optional[Dict[str, Any]],
    technology: str,
    current_year: Optional[int] = None,
) -> Optional[float]:
    """Estimate years of hands-on experience with a specific technology.

    Sums the tenures of experience entries that mention the technology, merging overlapping
    periods so concurrent projects are not double-counted. Returns None when no dated entry
    mentions it (tenure cannot be inferred).
    """
    tech = (technology or "").strip().lower()
    if not tech:
        return None
    now_months = current_year * 12 if current_year else _now_months()
    entries = (parsed_json or {}).get("experience") or []
    token = re.compile(r"(?<![a-z0-9])" + re.escape(tech) + r"(?![a-z0-9])")
    intervals = []
    for entry in entries:
        blob = f"{entry.get('title') or ''} {entry.get('details') or ''}".lower()
        if token.search(blob):
            interval = _entry_interval(str(entry.get("dates") or ""), now_months)
            if interval:
                intervals.append(interval)
    if not intervals:
        return None
    months = sum(e - s for s, e in _merge_intervals(intervals))
    return round(months / 12.0, 2)
