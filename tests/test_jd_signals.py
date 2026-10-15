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


def test_parse_experience_more_than():
    hints = parse_experience("More than 10 years applied experience.")
    assert hints and hints[0]["min_years"] == 10
    assert hints[0]["scope"] == "total"


def test_parse_experience_technology_specific():
    hints = parse_experience("3+ years of Kubernetes in production.")
    assert hints[0]["scope"] == "technology"
    assert hints[0]["technology"] == "Kubernetes"


def test_parse_experience_dedupes():
    hints = parse_experience("3+ years required. 3+ years minimum.")
    assert len(hints) == 1


def test_parse_experience_hyphenated_technology():
    hints = parse_experience("At least 3-years experience in Golang programming.")
    tech = [h for h in hints if h["scope"] == "technology"]
    assert len(tech) == 1
    assert tech[0]["technology"] == "Golang"
    assert tech[0]["min_years"] == 3


def test_parse_education_abbreviations():
    assert parse_education("B.Tech in Computer Science") == ["bachelor"]
    assert parse_education("M.Tech / M.E. preferred") == ["master"]


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


def test_parse_education_no_false_positive_on_ordinary_words():
    # "master" / "bachelor" as adjectives must not be read as a degree.
    assert parse_education("Master debugger with deep systems knowledge.") == []
    assert parse_education("We hire master engineers who ship.") == []
    assert parse_education("hacker bachelor pad") == []
    # Standalone abbreviations still detected.
    assert parse_education("Ph.D. preferred") == ["phd"]
    # "X or Y" chaining: both levels detected when a degree word follows.
    assert parse_education("Bachelor's or Master's degree required.") == ["master", "bachelor"]
    assert parse_education("No formal degree required.") == []


def test_parse_education_slash_joined_degree_cluster():
    # The common "BS/MS/PhD in <field>" alternative list must yield all three levels so the
    # comparison can pick the lowest (bachelor) as the effective requirement.
    levels = parse_education(
        "BS/MS/PhD in Computer Engineering, Electrical Engineering, or a related field"
    )
    assert set(levels) == {"bachelor", "master", "phd"}
    assert parse_education("B.S./M.S. in Computer Science") == ["master", "bachelor"]
    # Dotless acronyms alone still need a degree context (avoid "MS Teams"/"BS detector").
    assert parse_education("MS Teams integration experience") == []
    assert parse_education("BS detector") == []
    assert parse_education("MS in Computer Science") == ["master"]


def test_parse_certifications():
    assert "AWS Certified" in parse_certifications("Must hold AWS Certified Solutions Architect.")
    assert parse_certifications("No certifications required.") == []


def test_parse_certifications_kubernetes_togaf_cncf():
    certs = parse_certifications("TOGAF certified; CKA and Certified Kubernetes Security Specialist; CNCF member.")
    assert "TOGAF" in certs
    assert "CKA" in certs
    assert "Certified Kubernetes Security Specialist" in certs
    assert "CNCF" in certs


def test_parse_certifications_no_substring_acronym_matches():
    # Short acronyms like CKA/CKS must not match inside ordinary longer words.
    assert "CKA" not in parse_certifications("I hold a CKAD certification.")
    assert "CKS" not in parse_certifications("We fixed production bottlenecks and shipped.")
    assert "CKS" not in parse_certifications("Three specialization tracks in the platform.")
    # Standalone occurrences still match.
    certs = parse_certifications("Certified Kubernetes Administrator (CKA); CKS certified.")
    assert "CKA" in certs
    assert "CKS" in certs


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
