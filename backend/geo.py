"""
Offline location resolution for target/foreign filtering.

Backed by the offline ``geonamescache`` gazetteer (cities + US states, name -> country)
and ``pycountry`` (country names / ISO alpha-2). A small curated alias layer covers the
handful of tokens gazetteers miss or that are ambiguous (e.g. "sf", "nyc", "uk", "uae").

All lookups are offline (no network) and cached. If either package is missing the module
falls back to the curated aliases only, so the app still boots.
"""
import functools
import json
import logging
import re
from pathlib import Path
from typing import Dict, Iterable, Set

logger = logging.getLogger("geo")

# Editable alias file (data, not code). Add city/country aliases without touching Python.
# A missing file simply falls back to the built-in defaults below.
_ALIASES_FILE = Path(__file__).resolve().parent.parent / "data" / "location_aliases.json"

# Tokens that gazetteers miss, or are short/ambiguous. "in"/"ca" are deliberately NOT bare
# 2-letter country codes (they collide with Indiana/California and India).
_DEFAULT_COUNTRY_ALIASES: Dict[str, str] = {
    "us": "US", "usa": "US", "u.s.": "US", "u.s.a.": "US", "amer": "US",
    "uk": "GB", "u.k.": "GB", "britain": "GB", "great britain": "GB",
    "england": "GB", "scotland": "GB", "wales": "GB", "northern ireland": "GB",
    "uae": "AE", "emirates": "AE",
    "south korea": "KR", "north korea": "KP",
    "czech republic": "CZ", "ivory coast": "CI",
    "hong kong": "HK", "taiwan": "TW",
}

_DEFAULT_CITY_ALIASES: Dict[str, str] = {
    "sf": "US", "ny": "US", "nyc": "US", "dc": "US",
    "bombay": "IN", "bangalore": "IN", "madras": "IN", "calcutta": "IN",
    "saigon": "VN", "peking": "CN",
}

# Broad regions are intentionally NOT resolved to a country (avoids false restrictions).
_DEFAULT_BROAD_REGIONS = {
    "apac", "emea", "latam", "mena", "nordics", "baltics", "anz", "sea", "row",
    "global", "worldwide", "anywhere", "remote", "hybrid", "onsite", "various",
}

_ALT_MIN_LEN = 3
_ALT_MIN_POP = 100000
_TOKEN_SPLIT = re.compile(r'[\s,\(\)\[\];_\-\|\/]+')
_PREFIX_RE = re.compile(r'^\s*([a-z]{2})[-/]')


@functools.lru_cache(maxsize=1)
def _aliases() -> Dict[str, object]:
    """
    Loads editable aliases from ``data/location_aliases.json`` (if present), merged over the
    built-in defaults so the file can extend or override them without code changes.

    Expected shape::
        {
          "country_aliases": {"uae": "AE"},
          "city_aliases": {"sf": "US", "bangalore": "IN"},
          "broad_regions": ["apac", "emea"]
        }
    """
    country = dict(_DEFAULT_COUNTRY_ALIASES)
    city = dict(_DEFAULT_CITY_ALIASES)
    broad = set(_DEFAULT_BROAD_REGIONS)
    try:
        if _ALIASES_FILE.exists():
            data = json.loads(_ALIASES_FILE.read_text(encoding="utf-8"))
            for k, v in (data.get("country_aliases") or {}).items():
                if k and v:
                    country[str(k).strip().lower()] = str(v).strip().upper()
            for k, v in (data.get("city_aliases") or {}).items():
                if k and v:
                    city[str(k).strip().lower()] = str(v).strip().upper()
            for r in (data.get("broad_regions") or []):
                if r:
                    broad.add(str(r).strip().lower())
    except Exception as e:
        logger.warning("geo: could not load %s: %s", _ALIASES_FILE, e)
    return {"country": country, "city": city, "broad": broad}


def _country_aliases() -> Dict[str, str]:
    return _aliases()["country"]  # type: ignore[return-value]


def _city_aliases() -> Dict[str, str]:
    return _aliases()["city"]  # type: ignore[return-value]


def _broad_regions() -> Set[str]:
    return _aliases()["broad"]  # type: ignore[return-value]


@functools.lru_cache(maxsize=1)
def _city_index() -> Dict[str, str]:
    """name/alias -> ISO alpha-2 country code. Name collisions resolved by population."""
    idx: Dict[str, str] = dict(_city_aliases())
    try:
        import geonamescache

        gc = geonamescache.GeonamesCache()
        best: Dict[str, tuple] = {}
        for c in gc.get_cities().values():
            name = (c.get("name") or "").lower()
            cc = c.get("countrycode") or ""
            if not name or not cc:
                continue
            if len(name) < 3 and name not in _city_aliases():
                continue  # avoid ambiguous 2-letter city names (e.g. "Pa" in Burkina Faso)
            pop = c.get("population") or 0
            cur = best.get(name)
            if cur is None or pop > cur[0]:
                best[name] = (pop, cc)
        for name, (_pop, cc) in best.items():
            idx.setdefault(name, cc)

        # Alternate names for major cities (e.g. "Bangalore" -> Bengaluru -> IN).
        for c in gc.get_cities().values():
            if (c.get("population") or 0) < _ALT_MIN_POP:
                continue
            cc = c.get("countrycode") or ""
            if not cc:
                continue
            for alt in (c.get("alternatenames") or []):
                a = (alt or "").strip().lower()
                if _ALT_MIN_LEN <= len(a) <= 40 and a.isalpha():
                    idx.setdefault(a, cc)

        for st in gc.get_us_states().values():
            nm = (st.get("name") or "").lower()
            if nm:
                idx.setdefault(nm, "US")
    except Exception as e:  # pragma: no cover - only when the optional dep is absent
        logger.warning("geo: geonamescache unavailable; using curated aliases only: %s", e)
    return idx


@functools.lru_cache(maxsize=1)
def _country_index() -> Dict[str, str]:
    """country name/official name -> ISO alpha-2."""
    idx: Dict[str, str] = dict(_country_aliases())
    try:
        import pycountry

        for c in pycountry.countries:
            for nm in (getattr(c, "name", ""), getattr(c, "official_name", ""), getattr(c, "common_name", "")):
                if nm:
                    idx.setdefault(nm.lower(), c.alpha_2)
            if getattr(c, "alpha_2", None):
                idx.setdefault(c.alpha_2.lower(), c.alpha_2)
    except Exception as e:  # pragma: no cover
        logger.warning("geo: pycountry unavailable; using curated aliases only: %s", e)
    return idx


@functools.lru_cache(maxsize=1)
def _iso_codes() -> Set[str]:
    return {iso for iso in _country_index().values() if iso}


@functools.lru_cache(maxsize=8192)
def resolve_countries(text: str) -> frozenset:
    """
    Returns the set of ISO alpha-2 country codes referenced by a free-text location/title.

    Handles ISO prefixes ("US-PA-Remote", "IN-Bengaluru"), country names ("Remote, United
    States"), and city names/aliases ("Bengaluru", "SF", "NY"). Broad regions ("APAC",
    "Remote") resolve to nothing so they can't trigger a false foreign restriction.
    """
    if not text:
        return frozenset()
    low = str(text).lower()
    out: Set[str] = set()

    prefix = _PREFIX_RE.match(low)
    if prefix and prefix.group(1).upper() in _iso_codes():
        out.add(prefix.group(1).upper())

    for name, iso in _country_index().items():
        if len(name) < 3:
            continue
        if re.search(r'(?<![a-z])' + re.escape(name) + r'(?![a-z])', low):
            out.add(iso)

    tokens = {t for t in _TOKEN_SPLIT.split(low) if t}
    country_aliases = _country_aliases()
    for key in ("us", "uk"):
        if key in tokens and key in country_aliases:
            out.add(country_aliases[key])

    city_idx = _city_index()
    broad = _broad_regions()
    for t in tokens:
        if t in broad:
            continue
        iso = city_idx.get(t)
        if iso:
            out.add(iso)

    return frozenset(out)


def countries_for_terms(terms: Iterable[str]) -> Set[str]:
    """Resolves a list of country names / codes / city names to ISO alpha-2 codes."""
    out: Set[str] = set()
    country_idx = _country_index()
    city_idx = _city_index()
    country_aliases = _country_aliases()
    broad = _broad_regions()
    for raw in (terms or []):
        t = (raw or "").strip().lower()
        if not t or t == "all" or t in broad:
            continue
        if t in country_aliases:
            out.add(country_aliases[t])
        elif t in country_idx:
            out.add(country_idx[t])
        elif t in city_idx:
            out.add(city_idx[t])
        else:
            out |= set(resolve_countries(t))
    return out


def countries_for_cities(cities: Iterable[str]) -> Set[str]:
    """Maps configured target cities to ISO alpha-2 codes (Bengaluru -> IN, London -> GB)."""
    return countries_for_terms(cities)


def is_foreign_restricted(text: str, target_countries: Iterable[str]) -> bool:
    """
    True when the location references a country outside the target set. A location with no
    recognisable country (e.g. generic "Remote") is never considered foreign.
    """
    mentioned = resolve_countries(text)
    if not mentioned:
        return False
    targets = set(target_countries or set())
    return not mentioned.issubset(targets)
