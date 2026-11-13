"""Regression tests for the deterministic scoring contract (issue #108).

The fast classifier path and the requirement-coverage path had drifted apart: the classifier
counted only `met` (partial = 0), scored education/seniority as constants even when the JD
defined neither, and could not read headings out of an HTML-stripped posting. These tests pin
the shared contract.
"""

import pytest

from backend.match_classifier import (
    MatchFeatures,
    SimpleHeuristicClassifier,
    _is_title_phrase,
    build_deterministic_assessment,
    extract_match_features,
)
from backend.match_scoring import (
    RequirementAssessment,
    RequirementItem,
    _coverage,
    ScoringConfig,
)


def _reqs(statuses, category="must_have", kind="skill"):
    return [
        RequirementItem(
            text=f"req-{i}-{status}",
            kind=kind,
            category=category,
            status=status,
        )
        for i, status in enumerate(statuses)
    ]


def test_partial_credit_matches_requirement_coverage():
    """`partial` earns the same CREDIT on both engines (0.5), not zero."""
    assessment = RequirementAssessment(requirements=_reqs(["met", "met", "partial", "missing"]))

    features = extract_match_features(None, {"seniority_level": "unknown"}, assessment)
    expected = _coverage(assessment.requirements, ScoringConfig())

    assert expected == pytest.approx(0.625)
    assert features.must_coverage == pytest.approx(expected)


def test_unknown_requirements_leave_the_denominator():
    """A requirement that cannot be judged is excluded, not counted as a miss."""
    assessment = RequirementAssessment(requirements=_reqs(["met", "unknown"]))

    features = extract_match_features(None, {"seniority_level": "unknown"}, assessment)

    assert features.must_coverage == pytest.approx(1.0)


def test_nice_coverage_uses_partial_credit():
    assessment = RequirementAssessment(
        requirements=_reqs(["partial", "met"], category="nice_to_have")
    )

    features = extract_match_features(None, {"seniority_level": "unknown"}, assessment)

    assert features.nice_coverage == pytest.approx(0.75)


def test_education_component_is_excluded_when_the_jd_states_none():
    """No education requirement means the component leaves the scale entirely."""
    assessment = RequirementAssessment(requirements=_reqs(["met", "met"]))

    features = extract_match_features(None, {"seniority_level": "unknown"}, assessment)

    assert features.education_mentioned is False
    # must 60 + seniority excluded + education excluded -> perfect coverage scores 100.
    score = SimpleHeuristicClassifier().predict(
        MatchFeatures(
            must_coverage=features.must_coverage,
            nice_coverage=0.0,
            seniority_fit=0.0,
            education_match=0.0,
            experience_years_delta=0.0,
            over_qualified_flag=0.0,
            must_count=2,
            nice_count=0,
            seniority_known=False,
            education_mentioned=False,
        )
    )
    assert score == pytest.approx(100.0)


def test_seniority_uses_a_rank_gradient():
    """One level short is a near miss, not the same as a large gap."""
    from backend import resume_signals

    original = resume_signals.latest_seniority
    try:
        resume_signals.latest_seniority = lambda _parsed: "senior"
        features = extract_match_features(
            {"roles": []}, {"seniority_level": "staff"}, assessment=None
        )
        assert features.seniority_known is True
        # senior (3) -> staff (5): two levels short.
        assert features.seniority_fit == pytest.approx(0.7)

        resume_signals.latest_seniority = lambda _parsed: "junior"
        features = extract_match_features(
            {"roles": []}, {"seniority_level": "staff"}, assessment=None
        )
        assert features.seniority_fit == pytest.approx(0.4)
    finally:
        resume_signals.latest_seniority = original


def test_unknown_seniority_is_excluded_from_scoring():
    from backend import resume_signals

    original = resume_signals.latest_seniority
    try:
        resume_signals.latest_seniority = lambda _parsed: "unknown"
        features = extract_match_features(
            {"roles": []}, {"seniority_level": "staff"}, assessment=None
        )
        assert features.seniority_known is False
    finally:
        resume_signals.latest_seniority = original


@pytest.mark.parametrize(
    "phrase,title,expected",
    [
        ("Senior Staff Engineer", "Senior Staff Engineer", True),
        ("Staff Engineer", "", True),
        ("Lead Developer", "", True),
        ("Agentic AI", "", False),
        ("Distributed Systems", "", False),
        ("Python", "", False),
        ("Kubernetes", "", False),
    ],
)
def test_title_phrases_are_not_requirements(phrase, title, expected):
    assert _is_title_phrase(phrase, title) is expected


def test_the_posting_title_never_becomes_a_requirement():
    """The title is not a skill, even when the resume repeats it verbatim."""
    jd = (
        "Senior Staff Engineer\n"
        "Qualifications\n"
        "Must have skills:\n"
        "Python and Kubernetes experience.\n"
    )
    resume = "Senior Staff Engineer. Skills: Python, Kubernetes, Distributed Systems."
    assessment = build_deterministic_assessment(
        jd, {"skills": ["Python", "Kubernetes"]}, resume_text=resume,
        job_title="Senior Staff Engineer",
    )
    texts = [r.text.lower() for r in assessment.requirements]
    assert "senior staff engineer" not in texts


def test_an_html_stripped_jd_still_yields_sections():
    """A scraped posting (few short metadata lines + paragraph-sized lines) is segmented.

    Without this the whole document is one `required` span, so company boilerplate is scored
    as a requirement and `nice_count` is always 0.
    """
    from backend import section_intent

    body = "We are a global data company. " * 40
    qualifications = "Python required. " * 40
    benefits = "We care about people. " * 40
    jd = (
        "Senior Staff Engineer\n"
        "Full-time\n"
        "Hybrid\n"
        f"{body}Qualifications {qualifications}Benefits {benefits}"
    )

    spans = section_intent.jd_section_spans(jd)
    labels = [s.label for s in spans]

    assert len(spans) > 1
    assert "boilerplate" in labels
