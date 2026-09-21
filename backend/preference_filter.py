"""
Reusable discovery-preference gate.

Single source of truth for deciding whether a discovered posting is in-scope for the
candidate's persisted preferences (target roles, country/cities, work mode, foreign
region restrictions). Every discovery/ingestion path should call :class:`PreferenceFilter`
instead of re-implementing filter logic.

Explicit-intent sources (LinkedIn saved jobs, manual additions, already-applied Hirist
applications) intentionally bypass this gate.
"""

import logging
from dataclasses import dataclass, field
from typing import List, Optional  # noqa: UP035

from backend.matchers import check_title_match

logger = logging.getLogger("preference_filter")


# Region-group -> ISO-3166 alpha-2 membership. Used to reject region-scoped remote roles
# (e.g. "Americas - Remote", "EMEA remote", "APAC") when none of the target countries
# belong to that group. Country names are still resolved by backend.geo.
REGION_COUNTRY_GROUPS = {
    "north america": {"US", "CA", "MX", "BM", "GL", "PM"},
    "south america": {
        "AR",
        "BO",
        "BR",
        "CL",
        "CO",
        "EC",
        "GY",
        "PE",
        "PY",
        "SR",
        "UY",
        "VE",
    },
    "latam": {
        "AR",
        "BO",
        "BR",
        "CL",
        "CO",
        "CR",
        "CU",
        "DO",
        "EC",
        "GT",
        "HN",
        "MX",
        "NI",
        "PA",
        "PE",
        "PY",
        "SV",
        "UY",
        "VE",
        "PR",
    },
    "americas": {
        "US",
        "CA",
        "MX",
        "BR",
        "AR",
        "CL",
        "CO",
        "PE",
        "VE",
        "EC",
        "BO",
        "PY",
        "UY",
        "CR",
        "PA",
        "GT",
        "HN",
        "SV",
        "NI",
        "DO",
        "CU",
        "PR",
        "JM",
        "TT",
    },
    "emea": {
        "GB",
        "IE",
        "DE",
        "FR",
        "ES",
        "IT",
        "NL",
        "BE",
        "CH",
        "AT",
        "SE",
        "NO",
        "DK",
        "FI",
        "PL",
        "PT",
        "GR",
        "CZ",
        "RO",
        "HU",
        "UA",
        "TR",
        "IL",
        "AE",
        "SA",
        "QA",
        "KW",
        "BH",
        "OM",
        "ZA",
        "NG",
        "KE",
        "EG",
        "MA",
    },
    "europe": {
        "GB",
        "IE",
        "DE",
        "FR",
        "ES",
        "IT",
        "NL",
        "BE",
        "CH",
        "AT",
        "SE",
        "NO",
        "DK",
        "FI",
        "IS",
        "PL",
        "PT",
        "GR",
        "CZ",
        "SK",
        "HU",
        "RO",
        "BG",
        "HR",
        "SI",
        "RS",
        "UA",
        "EE",
        "LV",
        "LT",
        "LU",
        "MT",
        "CY",
    },
    "apac": {
        "IN",
        "CN",
        "JP",
        "KR",
        "SG",
        "MY",
        "ID",
        "TH",
        "VN",
        "PH",
        "AU",
        "NZ",
        "HK",
        "TW",
        "BD",
        "LK",
        "PK",
        "NP",
    },
    "anz": {"AU", "NZ"},
    "mena": {
        "AE",
        "SA",
        "QA",
        "KW",
        "BH",
        "OM",
        "JO",
        "LB",
        "EG",
        "MA",
        "TN",
        "DZ",
        "IL",
        "IQ",
        "IR",
    },
    "nordics": {"SE", "NO", "DK", "FI", "IS"},
    "asia": {
        "IN",
        "CN",
        "JP",
        "KR",
        "SG",
        "MY",
        "ID",
        "TH",
        "VN",
        "PH",
        "HK",
        "TW",
        "BD",
        "LK",
        "PK",
        "NP",
        "KH",
        "MM",
        "MN",
    },
    "africa": {
        "ZA",
        "NG",
        "KE",
        "EG",
        "GH",
        "MA",
        "TN",
        "DZ",
        "ET",
        "TZ",
        "UG",
        "RW",
        "SN",
        "CI",
        "CM",
    },
    "oceania": {"AU", "NZ", "FJ", "PG"},
}


def region_foreign_restriction(text: str, target_iso) -> bool:
    """True when ``text`` names a region group that contains NONE of the target countries."""
    targets = set(target_iso or set())
    if not targets:
        return False
    t = (text or "").lower()
    for region, isos in REGION_COUNTRY_GROUPS.items():
        if region in t and not (isos & targets):
            return True
    return False


def check_workmode_match(
    job_title: str, job_location: str, work_mode: Optional[str] = None
) -> bool:
    if not work_mode or work_mode.lower() == "all":
        return True
    wm = work_mode.strip().lower()
    text = f"{job_title} {job_location}".lower()
    if wm == "remote":
        return any(
            r in text
            for r in ["remote", "anywhere", "worldwide", "global", "distributed"]
        )
    elif wm == "hybrid":
        return "hybrid" in text or "remote" in text
    elif wm == "onsite":
        return "remote" not in text
    return True


def check_location_match(
    job_location: str,
    loc_filters: List[str],
    city_filters: Optional[List[str]] = None,
) -> bool:
    """
    Checks whether a job listing's location matches the candidate's target cities,
    country preferences, and remote eligibility.
    - Prevents foreign-country-restricted remote positions (e.g. US-PA-Remote) from matching.
    - Rejects region-scoped remotes (e.g. "Americas - Remote") when the target country is elsewhere.
    - Matches global/worldwide/anywhere remote or target country remote.
    """
    jl_lower = (job_location or "").lower().strip()
    if not jl_lower:
        return True
    # If loc_filters is empty list or None, set to []
    loc_filters = loc_filters or []

    # 1. Check specific city filters first if provided (e.g. Bengaluru, Hyderabad, SF)
    if city_filters:
        for cf in city_filters:
            cf_clean = cf.strip().lower()
            if not cf_clean or cf_clean == "remote":
                continue
            if cf_clean in jl_lower:
                return True
            if cf_clean in ("bengaluru", "bangalore") and any(
                b in jl_lower for b in ["bengaluru", "bangalore"]
            ):
                return True
            if cf_clean in ("san francisco", "sf") and any(
                b in jl_lower for b in ["san francisco", "sf", "bay area"]
            ):
                return True
            if cf_clean in (
                "delhi ncr",
                "delhi",
                "gurgaon",
                "noida",
                "gurugram",
            ) and any(
                b in jl_lower for b in ["delhi", "noida", "gurgaon", "gurugram", "ncr"]
            ):
                return True
            if cf_clean in ("new york", "nyc") and any(
                b in jl_lower for b in ["new york", "nyc", "ny"]
            ):
                return True

    clean_loc_filters = [
        lf.strip().lower()
        for lf in loc_filters
        if lf.strip() and lf.strip().lower() != "all"
    ]

    # No location or city preference: any location is eligible. Work-mode filtering is
    # handled separately by check_workmode_match.
    if not clean_loc_filters and not city_filters:
        return True

    # Resolve target countries once (explicit countries + countries implied by cities) via the
    # offline geo resolver — no hardcoded country/city tables.
    from backend.geo import countries_for_terms, countries_for_cities, resolve_countries

    target_iso = countries_for_terms(clean_loc_filters) | countries_for_cities(
        city_filters
    )

    # Region-group restriction (e.g. "Americas - Remote" for an India target).
    if target_iso and region_foreign_restriction(jl_lower, target_iso):
        return False

    # Country / region match (only when the listing isn't ALSO foreign-restricted).
    from backend.matchers import detect_foreign_restriction

    if (
        target_iso
        and (resolve_countries(jl_lower) & target_iso)
        and not detect_foreign_restriction(jl_lower, list(target_iso))
    ):
        return True

    # Handle Remote jobs
    is_remote_job = any(
        r in jl_lower
        for r in [
            "remote",
            "anywhere",
            "worldwide",
            "global",
            "distributed",
            "all-remote",
        ]
    )

    if is_remote_job:
        # Reject foreign-restricted remotes (e.g. "Remote US") relative to the target
        # countries; otherwise bare/global remotes are eligible (work mode is separate).
        if target_iso and detect_foreign_restriction(jl_lower, list(target_iso)):
            return False
        return True

    return False


@dataclass
class PreferenceCriteria:
    """Plain, serializable view of the discovery preferences that gate ingestion."""

    titles: List[str] = field(default_factory=list)
    locations: List[str] = field(default_factory=list)
    cities: List[str] = field(default_factory=list)
    work_mode: str = ""
    country: str = ""

    @property
    def has_filters(self) -> bool:
        return bool(
            self.titles
            or self.locations
            or self.cities
            or (self.work_mode and self.work_mode.lower() != "all")
        )

    @classmethod
    def from_pref(cls, pref) -> "PreferenceCriteria":
        raw_locs = (
            getattr(pref, "target_locations", None)
            or getattr(pref, "target_country", "")
            or ""
        )
        return cls(
            titles=[
                t.strip().lower()
                for t in (getattr(pref, "target_titles", "") or "").split(",")
                if t.strip()
            ],
            locations=[l.strip().lower() for l in raw_locs.split(",") if l.strip()],
            cities=[
                c.strip().lower()
                for c in (getattr(pref, "target_cities", "") or "").split(",")
                if c.strip()
            ],
            work_mode=(getattr(pref, "work_mode", "") or "").strip().lower(),
            country=(getattr(pref, "target_country", "") or "").strip(),
        )


class PreferenceFilter:
    """
    Reusable, side-effect-free preference gate.

    Usage::
        pf = PreferenceFilter.from_db(db)
        if pf.accepts(title, location, description):
            ingest(...)
        elif reason := pf.rejection_reason(title, location):
            skipped[reason] += 1
    """

    def __init__(self, criteria: PreferenceCriteria):
        self.criteria = criteria

    @classmethod
    def from_db(cls, db) -> "PreferenceFilter":
        from backend.database import get_user_preferences

        return cls(PreferenceCriteria.from_pref(get_user_preferences(db)))

    @property
    def has_filters(self) -> bool:
        return self.criteria.has_filters

    def title_ok(self, title: str) -> bool:
        return check_title_match(title or "", self.criteria.titles)

    def location_ok(self, location: str) -> bool:
        return check_location_match(
            location or "",
            self.criteria.locations,
            self.criteria.cities,
        )

    def work_mode_ok(self, title: str, location: str) -> bool:
        return check_workmode_match(
            title or "", location or "", self.criteria.work_mode
        )

    def rejection_reason(self, title: str, location: str) -> Optional[str]:
        """Returns the first failing rule ('title' | 'location' | 'work_mode') or None."""
        if not self.has_filters:
            return None
        if not self.title_ok(title):
            return "title"
        if not self.location_ok(location):
            return "location"
        if not self.work_mode_ok(title, location):
            return "work_mode"
        return None

    def accepts(self, title: str, location: str, description: str = "") -> bool:
        return self.rejection_reason(title, location) is None
