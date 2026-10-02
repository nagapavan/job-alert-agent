"""Unit tests for deterministic resume-side signals."""
from backend.resume_signals import estimate_total_years, latest_seniority


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
