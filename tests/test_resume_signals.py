"""Unit tests for deterministic resume-side signals."""
from backend.resume_signals import (
    estimate_total_years,
    estimate_technology_years,
    latest_seniority,
)


def test_estimate_total_years_span():
    parsed = {
        "experience": [
            {"title": "Engineer", "dates": "2015 - 2019"},
            {"title": "Senior Engineer", "dates": "2019 - Present"},
        ]
    }
    # Earliest start 2015, ongoing -> current_year 2026 => 11 years.
    assert estimate_total_years(parsed, current_year=2026) == 11.0


def test_estimate_total_years_latest_end_when_not_present():
    parsed = {"experience": [{"dates": "2016 - 2020"}, {"dates": "2020 - 2022"}]}
    assert estimate_total_years(parsed, current_year=2026) == 6.0


def test_estimate_total_years_none_without_years():
    assert estimate_total_years({"experience": [{"dates": "No dates"}]}) is None
    assert estimate_total_years({}) is None
    assert estimate_total_years(None) is None


def test_latest_seniority():
    parsed = {"experience": [{"title": "Senior Backend Engineer"}, {"title": "Engineer"}]}
    assert latest_seniority(parsed) == "senior"
    assert latest_seniority({"experience": []}) == "unknown"
    assert latest_seniority(None) == "unknown"


def test_estimate_technology_years_sums_projects():
    parsed = {
        "experience": [
            {"title": "Engineer", "dates": "2014 - 2016", "details": "Java services"},
            {"title": "Senior Engineer", "dates": "2016 - 2021", "details": "Java and Kafka"},
        ]
    }
    assert estimate_technology_years(parsed, "Java", current_year=2026) == 7.0


def test_estimate_technology_years_merges_overlaps():
    parsed = {
        "experience": [
            {"title": "A", "dates": "2016 - 2019", "details": "Java microservices"},
            {"title": "B", "dates": "2018 - 2021", "details": "Java and Kafka"},
        ]
    }
    # Overlap 2018-2019 counted once -> union 2016-2021 = 5, not 6.
    assert estimate_technology_years(parsed, "Java", current_year=2026) == 5.0


def test_estimate_technology_years_none_when_unmentioned_or_undated():
    parsed = {"experience": [{"title": "Engineer", "dates": "2014 - 2020", "details": "Python"}]}
    assert estimate_technology_years(parsed, "Java") is None
    parsed_undated = {"experience": [{"title": "Engineer", "details": "Java"}]}
    assert estimate_technology_years(parsed_undated, "Java") is None


# ---------------------------------------------------------------------------
# Tenure in months, gap policy, and the experience_years override (issue #55)
# ---------------------------------------------------------------------------
TIMELINE = [
    {"dates": "July 2024 - July 2026"},
    {"dates": "Sept 2022 - March 2024"},
    {"dates": "Sept 2020 - Aug 2022"},
    {"dates": "Dec 2019 - Sept 2020"},
    {"dates": "Feb 2016 - Sept 2019"},
    {"dates": "June 2014 - Feb 2016"},
    {"dates": "Jan 2011 - June 2014"},
    {"dates": "Aug 2008 - Jan 2011"},
]


def test_real_timeline_closed_role_is_17y4m():
    """Aug 2008 -> Jul 2026 = 215 months, minus the 3- and 4-month gaps = 208 = 17.33.

    The sub-week PayPal -> Atlassian handover (Aug 2022 -> Sept 2022) is one month at this
    granularity and must NOT be penalised.
    """
    assert estimate_total_years({"experience": TIMELINE}) == 17.33


def test_real_timeline_open_role_is_17y7m():
    """Same timeline with the current role open runs to today: 211 months = 17.58."""
    open_timeline = [dict(e) for e in TIMELINE]
    open_timeline[0]["dates"] = "July 2024 - Present"
    assert estimate_total_years({"experience": open_timeline}) == 17.58


def test_single_long_role_is_17y11m():
    assert estimate_total_years({"experience": [{"dates": "Aug 2008 - Jul 2026"}]}) == 17.92


def test_material_gaps_are_subtracted():
    """A 2-month gap is real; a 1-month gap is a handover and counts as contiguous."""
    one_month = {"experience": [{"dates": "Jan 2010 - Jan 2020"}, {"dates": "Feb 2020 - Jan 2021"}]}
    two_months = {"experience": [{"dates": "Jan 2010 - Jan 2020"}, {"dates": "Mar 2020 - Jan 2021"}]}
    assert estimate_total_years(one_month, current_year=2026) == 11.0
    assert estimate_total_years(two_months, current_year=2026) == 10.83


def test_month_precision_beats_year_subtraction():
    """'Mar 2020 - Nov 2020' is 8 months, not 0 years."""
    assert estimate_total_years(
        {"experience": [{"dates": "Mar 2020 - Nov 2020"}]}, current_year=2026
    ) == 0.67


class TestExperienceYearsOverride:
    def test_override_wins_over_the_derived_timeline(self):
        parsed = {"experience": TIMELINE, "experience_years": "18"}
        assert estimate_total_years(parsed) == 18.0

    def test_override_accepts_years_and_months(self):
        parsed = {"experience": TIMELINE, "experience_years": "17 years 7 months"}
        assert estimate_total_years(parsed) == 17.58

    def test_override_accepts_a_number(self):
        assert estimate_total_years({"experience": [], "experience_years": 12.5}) == 12.5

    def test_unusable_override_falls_back_to_the_timeline(self):
        parsed = {"experience": TIMELINE, "experience_years": "n/a"}
        assert estimate_total_years(parsed) == 17.33

    def test_no_override_still_derives(self):
        assert estimate_total_years({"experience": TIMELINE}) == 17.33
