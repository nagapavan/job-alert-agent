"""
Tests for the fast ML-based match classifier.
"""

import pytest
from backend.match_classifier import (
    MatchFeatures,
    SimpleHeuristicClassifier,
    extract_match_features,
)


class TestSimpleHeuristicClassifier:
    """Test fallback heuristic classifier (always available, no sklearn required)."""

    def test_strong_match(self):
        """Strong must-have + seniority + experience coverage."""
        classifier = SimpleHeuristicClassifier()
        features = MatchFeatures(
            must_coverage=1.0,
            nice_coverage=0.8,
            seniority_fit=1.0,
            education_match=1.0,
            experience_years_delta=0.0,
            over_qualified_flag=0.0,
            must_count=5,
            nice_count=3,
        )
        score = classifier.predict(features)
        assert score >= 80  # Should be in "Strong" band

    def test_moderate_match(self):
        """Partial must-haves, good nice-to-haves."""
        classifier = SimpleHeuristicClassifier()
        features = MatchFeatures(
            must_coverage=0.6,
            nice_coverage=0.9,
            seniority_fit=0.8,
            education_match=0.5,
            experience_years_delta=0.1,
            over_qualified_flag=0.0,
            must_count=5,
            nice_count=4,
        )
        score = classifier.predict(features)
        assert 55 <= score < 75  # Should be in "Moderate" or "Good" band

    def test_weak_match(self):
        """Low must-have coverage, experience mismatch."""
        classifier = SimpleHeuristicClassifier()
        features = MatchFeatures(
            must_coverage=0.2,
            nice_coverage=0.3,
            seniority_fit=0.3,
            education_match=0.0,
            experience_years_delta=-0.8,  # Too junior
            over_qualified_flag=0.0,
            must_count=10,
            nice_count=5,
        )
        score = classifier.predict(features)
        assert score < 50  # Should be in "Weak" or "Poor" band

    def test_score_bounds(self):
        """Scores are always 0-100."""
        classifier = SimpleHeuristicClassifier()
        for must_cov in [0.0, 0.5, 1.0]:
            for nice_cov in [0.0, 0.5, 1.0]:
                features = MatchFeatures(
                    must_coverage=must_cov,
                    nice_coverage=nice_cov,
                    seniority_fit=0.5,
                    education_match=0.5,
                    experience_years_delta=0.0,
                    over_qualified_flag=0.0,
                    must_count=5,
                    nice_count=5,
                )
                score = classifier.predict(features)
                assert 0 <= score <= 100


class TestExtractMatchFeatures:
    """Test feature extraction from resume signals and JD hints."""

    def test_basic_feature_extraction(self):
        """Extract features from empty parsed resume and hints."""
        jd_hints = {
            "min_years": 3,
            "max_years": 10,
            "seniority_level": "senior",
            "education_mentioned": True,
        }
        features = extract_match_features(None, jd_hints, assessment=None)

        assert isinstance(features, MatchFeatures)
        assert 0 <= features.must_coverage <= 1
        assert 0 <= features.nice_coverage <= 1
        assert -1 <= features.experience_years_delta <= 1

    def test_experience_delta_too_junior(self):
        """Candidate has less experience than job requires."""
        jd_hints = {"min_years": 10, "max_years": 20}
        parsed_resume = {"experience_years": 2}

        # Mock resume signals
        import backend.resume_signals as rs
        original_estimate = rs.estimate_total_years

        rs.estimate_total_years = lambda x: 2
        try:
            features = extract_match_features(parsed_resume, jd_hints, assessment=None)
            assert features.experience_years_delta < 0  # Too junior
        finally:
            rs.estimate_total_years = original_estimate

    def test_seniority_match(self):
        """Candidate seniority matches job level."""
        jd_hints = {"seniority_level": "senior"}
        parsed_resume = {"roles": [{"level": "senior"}]}

        import backend.resume_signals as rs
        original_seniority = rs.latest_seniority

        rs.latest_seniority = lambda x: "senior"
        try:
            features = extract_match_features(parsed_resume, jd_hints, assessment=None)
            assert features.seniority_fit == 1.0
        finally:
            rs.latest_seniority = original_seniority

    def test_education_match(self):
        """Education mentioned in JD affects feature."""
        jd_hints = {"education_mentioned": True}
        features = extract_match_features(None, jd_hints, assessment=None)
        assert features.education_match == 1.0

        jd_hints = {"education_mentioned": False}
        features = extract_match_features(None, jd_hints, assessment=None)
        assert features.education_match == 0.5
