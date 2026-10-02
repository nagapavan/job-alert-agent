"""Unit tests for deterministic job-description signal parsers."""
from backend.jd_signals import (
    is_over_qualified,
    parse_certifications,
    parse_education,
    parse_experience,
    seniority_level,
)


def test_parse_experience_plus_years_is_total():
    hints = parse_experience("We need 3+ years of experience building APIs.")
    assert hints and hints[0]["min_years"] == 3
    assert hints[0]["scope"] == "total"


def test_parse_experience_range():
    hints = parse_experience("Requires 3-5 years in backend engineering.")
    assert hints[0]["min_years"] == 3
    assert hints[0]["max_years"] == 5
    assert hints[0]["scope"] == "total"


def test_parse_experience_at_least():
    hints = parse_experience("At least 7 years of professional experience.")
    assert hints[0]["min_years"] == 7
    assert hints[0]["scope"] == "total"  # "professional" is not a technology


def test_parse_experience_technology_specific():
    hints = parse_experience("3+ years of Kubernetes in production.")
    assert hints[0]["scope"] == "technology"
    assert hints[0]["technology"] == "Kubernetes"


def test_parse_experience_dedupes():
    hints = parse_experience("3+ years required. 3+ years minimum.")
    assert len(hints) == 1


def test_seniority_levels():
    assert seniority_level("Senior Software Engineer") == "senior"
    assert seniority_level("Staff Backend Engineer") == "staff"
    assert seniority_level("Principal Architect") == "principal"
    assert seniority_level("Engineering Manager") == "manager"
    assert seniority_level("Head of Platform") == "director"
    assert seniority_level("Junior Developer") == "junior"
    assert seniority_level("Software Engineer") == "unknown"


def test_parse_education():
    assert parse_education("Bachelor's degree or equivalent experience.") == ["bachelor"]
    assert parse_education("Master's degree preferred.") == ["master"]
    assert parse_education("No formal degree required.") == []


def test_parse_certifications():
    assert "AWS Certified" in parse_certifications("Must hold AWS Certified Solutions Architect.")
    assert parse_certifications("No certifications required.") == []


def test_parse_certifications_kubernetes_togaf_cncf():
    certs = parse_certifications("TOGAF certified; CKA and Certified Kubernetes Security Specialist; CNCF member.")
    assert "TOGAF" in certs
    assert "CKA" in certs
    assert "Certified Kubernetes Security Specialist" in certs
    assert "CNCF" in certs


def test_over_qualified_total_minimum():
    hint = {"min_years": 3, "max_years": None, "scope": "total", "technology": None, "snippet": ""}
    assert is_over_qualified(10, hint) is True
    assert is_over_qualified(4, hint) is False   # within margin
    assert is_over_qualified(None, hint) is False


def test_over_qualified_technology_specific_is_exempt():
    hint = {"min_years": 3, "max_years": None, "scope": "technology", "technology": "Python", "snippet": ""}
    assert is_over_qualified(12, hint) is False


def test_over_qualified_range_upper_bound():
    hint = {"min_years": 3, "max_years": 5, "scope": "total", "technology": None, "snippet": ""}
    assert is_over_qualified(8, hint) is True
    assert is_over_qualified(5, hint) is False
