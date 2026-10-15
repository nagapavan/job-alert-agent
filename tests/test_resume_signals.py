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
