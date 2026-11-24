"""The user's Google-for-Jobs search queries: normalisation plus the JSON file they live in.

Extracted from ``main.py`` so the Google Jobs routes and the scheduled runner share one
implementation without importing the app module.
"""

import json
import logging
import re
from typing import List

from backend.config import BASE_DIR

logger = logging.getLogger(__name__)


GOOGLE_SEARCHES_FILE = BASE_DIR / "data" / "google_searches.json"
DEFAULT_GOOGLE_SEARCHES = [
    "Staff Software Engineer remote",
    "Senior Backend Engineer distributed systems",
    "Principal Software Engineer",
    "Senior Data Engineer Python SQL",
]


def normalize_google_query(query: str) -> str:
    """
    Cleans a Google Jobs search/alert query:
    - collapses consecutive duplicate words (e.g. "jobs in India India" -> "jobs in India")
    - drops a trailing word that already appeared earlier (e.g. "Acme india jobs India" -> "Acme india jobs")
    Never renames the user's alerts on Google; only what we store/search with.
    """
    if not query:
        return query
    s = " ".join(str(query).split())
    s = re.sub(r"\b(\w+)(?:\s+\1\b)+", r"\1", s, flags=re.IGNORECASE)
    words = s.split()
    if len(words) >= 2:
        earlier = {w.lower() for w in words[:-1]}
        if words[-1].lower() in earlier:
            words = words[:-1]
            s = " ".join(words)
    return s.strip()


def _normalize_google_queries(searches: List[str]) -> List[str]:
    clean: List[str] = []
    seen = set()
    for q in searches:
        n = normalize_google_query(str(q))
        key = n.lower()
        if n and key not in seen:
            seen.add(key)
            clean.append(n)
    return clean


def load_google_searches() -> List[str]:
    try:
        if GOOGLE_SEARCHES_FILE.exists():
            with open(GOOGLE_SEARCHES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list) and data:
                    return _normalize_google_queries(
                        [str(q) for q in data if str(q).strip()]
                    )
    except Exception as e:
        logger.debug(f"Could not load google_searches.json: {e}")
    return list(DEFAULT_GOOGLE_SEARCHES)


def save_google_searches(searches: List[str]) -> List[str]:
    try:
        GOOGLE_SEARCHES_FILE.parent.mkdir(parents=True, exist_ok=True)
        clean = _normalize_google_queries(searches)
        with open(GOOGLE_SEARCHES_FILE, "w", encoding="utf-8") as f:
            json.dump(clean, f, indent=2)
        return clean
    except Exception as e:
        logger.debug(f"Could not save google_searches.json: {e}")
        return searches
