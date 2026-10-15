"""Deterministic resume-side signals derived from the parsed resume JSON."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from backend.jd_signals import seniority_level

_YEAR = re.compile(r"(?:19|20)\d{2}")
_PRESENT = re.compile(r"present|current|now|ongoing|today", re.I)


def estimate_total_years(
    parsed_json: Optional[Dict[str, Any]], current_year: Optional[int] = None
) -> Optional[float]:
    """Approximate total experience as the career span from the earliest start year to
    now (or the latest end year). Returns None when no years are parseable."""
    entries = (parsed_json or {}).get("experience") or []
    today = current_year or datetime.now(timezone.utc).year
    starts, ends = [], []
    for entry in entries:
        dates = str(entry.get("dates") or "")
        years = [int(m.group(0)) for m in _YEAR.finditer(dates)]
        if years:
            starts.append(min(years))
            ends.append(max(years))
        if _PRESENT.search(dates):
            ends.append(today)
    if not starts:
        return None
    end = max(ends) if ends else today
    return float(max(0, end - min(starts)))


def latest_seniority(parsed_json: Optional[Dict[str, Any]]) -> str:
    """Coarse seniority level of the most recent role, or "unknown"."""
    entries = (parsed_json or {}).get("experience") or []
    if not entries:
        return "unknown"
    return seniority_level(str(entries[0].get("title") or ""))


def _entry_interval(dates: str, current_year: int):
    """Return (start_year, end_year) for a date range, or None when unparseable."""
    years = [int(m.group(0)) for m in _YEAR.finditer(dates or "")]
    if not years:
        return None
    start = min(years)
    end = current_year if _PRESENT.search(dates or "") else max(years)
    if end < start:
        start, end = end, start
    return (start, end)


def _merge_intervals(intervals):
    """Merge overlapping/adjacent year intervals so shared time is counted once."""
    merged = []
    for s, e in sorted(intervals):
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def estimate_technology_years(
    parsed_json: Optional[Dict[str, Any]],
    technology: str,
    current_year: Optional[int] = None,
) -> Optional[float]:
    """Estimate years of hands-on experience with a specific technology.

    Sums the tenures of experience entries that mention the technology, merging
    overlapping periods so concurrent projects are not double-counted. Returns None
    when no dated entry mentions it (tenure cannot be inferred).
    """
    tech = (technology or "").strip().lower()
    if not tech:
        return None
    today = current_year or datetime.now(timezone.utc).year
    entries = (parsed_json or {}).get("experience") or []
    token = re.compile(r"(?<![a-z0-9])" + re.escape(tech) + r"(?![a-z0-9])")
    intervals = []
    for entry in entries:
        blob = f"{entry.get('title') or ''} {entry.get('details') or ''}".lower()
        if token.search(blob):
            interval = _entry_interval(str(entry.get("dates") or ""), today)
            if interval:
                intervals.append(interval)
    if not intervals:
        return None
    return float(sum(e - s for s, e in _merge_intervals(intervals)))
