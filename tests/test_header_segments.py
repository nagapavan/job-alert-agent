"""Deterministic typing of recruiter/alert email header segments.

Shapes are copied from real mail, names are invented (Acme/Globex/Initech).
"""

from backend.header_segments import (
    classify_segment,
    parse_pipe_card,
    split_company_location,
)
from backend.scraper import _employer_from_subject, extract_company_and_role_from_email_header


def test_pipe_card_reads_title_company_and_location():
    card = parse_pipe_card(
        "Message replied: Job Opportunity || Staff Software Engineer - Platform || Acme || Remote"
    )
    assert card == {
        "title": "Staff Software Engineer - Platform",
        "company": "Acme",
        "location": "Remote",
    }


def test_pipe_card_keeps_the_title_verbatim():
    card = parse_pipe_card(
        "Message replied: Re: opening || Senior Staff Software Engineer- Enterprise "
        "Product Architecture || Initech || Bengaluru, India"
    )
    assert card["title"] == "Senior Staff Software Engineer- Enterprise Product Architecture"
    assert "||" not in card["title"]
    assert card["location"] == "Bengaluru, India"


def test_work_mode_is_never_an_employer():
    for segment in ("Remote", "Hybrid", "Onsite", "Remote (India)", "Remote, India"):
        kind, _value = classify_segment(segment)
        assert kind in ("work_mode", "location"), segment

    # A card with no company segment yields nothing at all.
    assert parse_pipe_card("Message replied: Job Opportunity || Senior Engineer || Remote") is None


def test_city_is_never_an_employer():
    assert classify_segment("Bengaluru")[0] == "location"
    assert classify_segment("Bengaluru, India")[0] == "location"
    assert parse_pipe_card(
        "Message replied: Job Opportunity || Principal Engineer || Bengaluru || Hybrid"
    ) is None


def test_company_with_legal_suffix_resolves_to_the_known_name():
    kind, value = classify_segment("Acme Technologies Inc", known_companies=("Acme",))
    assert kind == "company"
    assert value == "Acme"


def test_employer_from_subject_never_returns_a_work_mode():
    assert _employer_from_subject(
        "Message replied: Job Opportunity || Senior Engineer || Remote"
    ) is None


def test_prose_subject_reads_the_employer_and_the_parenthetical_location():
    extracted = extract_company_and_role_from_email_header(
        "Message replied: Principal Engineer: Re-imagine Observability at Globex (Remote, India)",
        "",
        "Recruiter <hit-reply@linkedin.com>",
    )
    assert extracted["company"] == "Globex"
    assert extracted["role"] == "Principal Engineer"
    assert extracted["location"] == "Remote, India"


def test_split_company_location_only_peels_a_place():
    assert split_company_location("Globex (Remote, India)") == ("Globex", "Remote, India")
    assert split_company_location("Globex (Series B)") == ("Globex (Series B)", "")
