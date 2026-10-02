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
