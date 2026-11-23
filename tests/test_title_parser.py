"""Tests for the shared merged-title parser (role + employer in one card string)."""

import pytest

from backend.title_parser import (
    is_bucket_company,
    normalize_title,
    resolve_company_name,
    split_role_employer,
)


def test_duplicated_role_card_splits_at_high_confidence():
    title = (
        "Principal Core Infrastructure Engineer (Verified job)"
        "Principal Core Infrastructure Engineer Oracle"
    )
    role, employer, confidence = split_role_employer(title)
    assert role == "Principal Core Infrastructure Engineer"
    assert employer == "Oracle"
    assert confidence == "high"


def test_camel_case_concatenation_keeps_the_role_intact():
    assert split_role_employer("Senior Staff EngineerArcesium")[:2] == (
        "Senior Staff Engineer",
        "Arcesium",
    )
    assert split_role_employer("Senior Software Engineer, Backend & InfrastructureCoursera")[:2] == (
        "Senior Software Engineer Backend & Infrastructure",
        "Coursera",
    )


def test_team_qualifiers_are_stripped_from_the_employer():
    assert split_role_employer("Principal Architect - AI Anaplan")[:2] == (
        "Principal Architect",
        "Anaplan",
    )
    assert split_role_employer("Principal Engineer - Member Experience Platform Skipton Building Society")[:2] == (
        "Principal Engineer",
        "Skipton Building Society",
    )


def test_a_tail_made_only_of_team_words_is_not_an_employer():
    """"Staff Software Engineer - Enterprise Platform" has no employer in it."""
    role, employer, confidence = split_role_employer("Staff Software Engineer - Enterprise Platform")
    assert employer == ""
    assert confidence == "none"


def test_glued_lowercase_name_is_not_guessed():
    assert split_role_employer("Back-End Development Leadcrewlia")[2] == "none"


def test_trailing_language_tag_and_verification_marker_are_removed():
    assert normalize_title("Principal Agentic Engineer Eli Lilly - English") == (
        "Principal Agentic Engineer Eli Lilly"
    )
    assert normalize_title("Staff Engineer (Verified job)") == "Staff Engineer"


def test_known_company_inside_the_tail_wins_over_team_words():
    assert split_role_employer(
        "Senior Software Engineer – Agent Systems Kraken Digital Asset Exchange", ["Kraken"]
    )[:2] == ("Senior Software Engineer", "Kraken")


def test_known_name_matching_is_word_based_not_substring():
    """"Connect Tech+Talent" contains "ttec" as a substring; it must not resolve to TTEC."""
    role, employer, _ = split_role_employer(
        "Solution Architect & Lead SE Engineer Connect Tech+Talent", ["TTEC"]
    )
    assert employer == "Connect Tech+Talent"
    assert role == "Solution Architect & Lead SE Engineer"


def test_junk_known_names_are_ignored():
    """A company row literally named "AI/ML" must not swallow the employer that follows it."""
    role, employer, _ = split_role_employer(
        "Sr. Software Engineer - AI/ML - AI Platform Addepar", ["AI/ML"]
    )
    assert employer == "Addepar"


@pytest.mark.parametrize(
    "variant, expected",
    [
        ("Uber Technologies Inc", "Uber"),
        ("Uber Technologies, Inc.", "Uber"),
        ("Kainos Software Ltd.", "Kainos"),
        ("Kraken Digital Asset Exchange", "Kraken"),
        ("Owen Thomas | B Corp™", "Owen Thomas"),
    ],
)
def test_resolve_company_name_collapses_synonyms(variant, expected):
    known = ["Uber", "Kainos", "Kraken", "Owen Thomas"]
    assert resolve_company_name(variant, known) == expected


def test_resolve_company_name_uses_the_synonym_file():
    # "F5 Networks" is declared as an alias of F5 in data/company_synonyms.json.
    assert resolve_company_name("F5 Networks", ["F5"]) == "F5"


def test_bucket_companies_are_recognised():
    assert is_bucket_company("LinkedIn Network")
    assert is_bucket_company("Unknown Company")
    assert not is_bucket_company("Acme")
