"""
Tests for the fast ML-based match classifier.
"""

import pytest
from backend.match_classifier import (
    MatchFeatures,
    SimpleHeuristicClassifier,
    extract_match_features,
    build_deterministic_assessment,
    build_jd_hints,
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


class TestDeterministicAssessment:
    """Deterministic (no-LLM) requirement extraction fed to the classifier."""

    @pytest.mark.parametrize(
        "jd, parsed, resume_text, skill, expected_status",
        [
            ("We need Python, Kubernetes, Terraform and AWS.", None,
             "Skills: Python, AWS, Kubernetes.", "Python", "met"),
            ("We need Python, Kubernetes, Terraform and AWS.", None,
             "Skills: Python, AWS, Kubernetes.", "Kubernetes", "met"),
            ("We need Python, Kubernetes, Terraform and AWS.", None,
             "Skills: Python, AWS, Kubernetes.", "Terraform", "missing"),
            ("Requires Python and Go.", None,
             "I have 5 years of Python experience.", "Python", "met"),
            ("Requires Python and Go.", None,
             "I have 5 years of Python experience.", "Go", "missing"),
            ("Must know Kubernetes and Docker.", {"skills": ["Kubernetes"]},
             "", "Kubernetes", "met"),
            ("Must know Kubernetes and Docker.", {"skills": ["Kubernetes"]},
             "", "Docker", "missing"),
        ],
    )
    def test_skill_status(self, jd, parsed, resume_text, skill, expected_status):
        a = build_deterministic_assessment(
            jd, parsed, resume_text=resume_text, job_title="Engineer"
        )
        by_text = {r.text: r.status for r in a.requirements}
        assert by_text[skill] == expected_status

    @pytest.mark.parametrize(
        "jd, parsed, resume_text, skill, expected_status",
        [
            # NodeJS in raw resume text is aliased to canonical node.js and matches a
            # Node.js requirement directly.
            ("Requires Node.js.", {"skills": ["Python"]},
             "Built services with NodeJS.", "Node.js", "met"),
            # A Node.js developer is partially (not fully) aligned with a JavaScript
            # requirement via the front-end language-family concept group.
            ("Requires JavaScript.", {"skills": ["Python"]},
             "Built services with NodeJS.", "JavaScript", "partial"),
            # TypeScript requirement is likewise partial for a Node developer.
            ("Requires TypeScript.", {"skills": ["Python"]},
             "Built services with NodeJS.", "TypeScript", "partial"),
            # A resume with neither Node nor JS stays missing.
            ("Requires JavaScript.", {"skills": ["Python"]},
             "Python and Docker.", "JavaScript", "missing"),
        ],
    )
    def test_nodejs_language_family_detection(self, jd, parsed, resume_text, skill, expected_status):
        a = build_deterministic_assessment(
            jd, parsed, resume_text=resume_text, job_title="Engineer"
        )
        by_text = {r.text: r.status for r in a.requirements}
        assert by_text[skill] == expected_status

    @pytest.mark.parametrize(
        "skill, expected_category",
        [("Python", "must_have"), ("Rust", "nice_to_have")],
    )
    def test_preferred_section_marks_nice_to_have(self, skill, expected_category):
        jd = (
            "Minimum Qualifications: Python required.\n"
            "Preferred Qualifications: Rust experience."
        )
        a = build_deterministic_assessment(jd, {"skills": ["Python", "Rust"]})
        by_text = {r.text: r.category for r in a.requirements}
        assert by_text[skill] == expected_category

    @pytest.mark.parametrize(
        "jd_degree, resume_degree, expected_status",
        [
            ("Bachelor's degree required.", "Master of Science in CS", "met"),
            ("Bachelor's degree required.", "PhD in Computer Science", "met"),
            ("Bachelor's degree required.", "B.Tech in Computer Science", "met"),
            ("Bachelor's degree required.", "Associate degree", "missing"),
            ("Master's degree required.", "Bachelor of Science", "missing"),
            ("Master's degree required.", "M.Tech in CS", "met"),
            ("Bachelor's or Master's degree required.", "B.Tech in CS", "met"),
            ("Bachelor's or Master's degree required.", "M.Tech in CS", "met"),
            ("Bachelor's degree required.", "", "unknown"),
        ],
    )
    def test_education_level_comparison(self, jd_degree, resume_degree, expected_status):
        parsed = {"education": [{"degree": resume_degree}]} if resume_degree else None
        a = build_deterministic_assessment(jd_degree, parsed, job_title="Engineer")
        edu = [r for r in a.requirements if r.kind == "education"]
        assert len(edu) == 1
        assert edu[0].status == expected_status

    @pytest.mark.parametrize(
        "resume_degree, expected_status",
        [
            ("B.Tech in Computer Science", "met"),
            ("Master's degree in CS", "met"),
            ("PhD in CS", "met"),
            ("Associate degree", "missing"),
        ],
    )
    def test_education_alternative_list_uses_lowest_level(self, resume_degree, expected_status):
        """A "BS/MS/PhD" list is alternatives: the effective requirement is the lowest (bachelor)."""
        jd = "BS/MS/PhD in Computer Science or a related field."
        parsed = {"education": [{"degree": resume_degree}]}
        a = build_deterministic_assessment(jd, parsed, job_title="Engineer")
        edu = [r for r in a.requirements if r.kind == "education"]
        assert len(edu) == 1
        assert edu[0].text == "Education: bachelor"
        assert edu[0].status == expected_status

    @pytest.mark.parametrize(
        "resume_skills, expected_status",
        [(["Golang"], "partial"), (["Python"], "missing")],
    )
    def test_technology_years_not_over_credited(self, resume_skills, expected_status):
        """A "N years <Tech>" requirement is partial (presence) or missing — never "met"."""
        jd = "What You Bring: At least 3-years experience in Golang."
        a = build_deterministic_assessment(jd, {"skills": resume_skills}, job_title="Engineer")
        years = [r for r in a.requirements if r.technology == "Golang"]
        assert len(years) == 1
        assert years[0].status == expected_status
        # Presence must not also be credited as a separate plain skill.
        assert not [r for r in a.requirements if r.kind == "skill" and r.text == "Golang"]

    def test_technology_years_met_when_dated_evidence_supports(self):
        jd = "What You Bring: 3+ years of Java experience."
        parsed = {
            "skills": ["Java"],
            "experience": [
                {"title": "Engineer", "dates": "2014 - 2020", "details": "Java services"}
            ],
        }
        a = build_deterministic_assessment(jd, parsed, job_title="Engineer")
        java = [r for r in a.requirements if r.technology == "Java"]
        assert len(java) == 1
        assert java[0].status == "met"
        assert "yrs" in java[0].evidence

    def test_skills_in_about_section_ignored(self):
        """Skills in company/marketing prose (the intro block) are ignored."""
        jd = (
            "About Us: we maintain 40-year old C++ code.\n"
            "What You Bring: Python experience."
        )
        a = build_deterministic_assessment(jd, {"skills": ["Python"]}, job_title="Engineer")
        skill_texts = {r.text for r in a.requirements if r.kind == "skill"}
        assert "Python" in skill_texts
        assert "C++" not in skill_texts

    def test_benefits_perks_prose_not_scored_as_skill(self):
        """Regression: a perks/offer paragraph mentioning "benefits" must not surface the
        HR skill "Benefits Administration" — the word is ordinary prose, not a requirement."""
        jd = (
            "Required Skills & Experience\n"
            "Deep expertise in C/C++, Linux internals, and RDMA.\n"
            "What We Can Offer You\n"
            "Health & Wellbeing\n"
            "We provide a comprehensive suite of benefits that supports your wellbeing.\n"
        )
        a = build_deterministic_assessment(
            jd, {"skills": ["C++", "Linux"]}, job_title="Engineer"
        )
        skill_texts = {r.text for r in a.requirements if r.kind == "skill"}
        assert "Benefits Administration" not in skill_texts
        assert {"C++", "Linux"} <= skill_texts

    def test_skills_before_requirements_heading_are_captured(self):
        """A skill list before a later "Requirements" heading must not be dropped."""
        jd = "We need Python and Go engineers.\n\nRequirements: strong communication."
        a = build_deterministic_assessment(jd, None, resume_text="Skills: Python, Go.", job_title="Engineer")
        skill_texts = {r.text for r in a.requirements if r.kind == "skill"}
        assert {"Python", "Go"} <= skill_texts

    def test_intro_footer_does_not_wipe_requirements(self):
        """A trailing "About Us" (nav/footer) must not cause the whole JD to be skipped."""
        jd = (
            "We need Python and Kubernetes engineers.\n\n"
            "About Us: we are a great company."
        )
        a = build_deterministic_assessment(jd, None, resume_text="Skills: Python.", job_title="Engineer")
        skill_texts = {r.text for r in a.requirements if r.kind == "skill"}
        assert {"Python", "Kubernetes"} <= skill_texts

    def test_ideally_section_marks_nice_to_have(self):
        jd = "What You Bring: Python required.\nIdeally, you'll also have Rust experience."
        a = build_deterministic_assessment(jd, {"skills": ["Python", "Rust"]})
        by_text = {r.text: r.category for r in a.requirements}
        assert by_text["Rust"] == "nice_to_have"

    def test_cloud_providers_collapse_to_one_requirement(self):
        a = build_deterministic_assessment(
            "Managing Cloud Infrastructure on one of the Hyperscalers: AWS, GCP, or Azure.",
            {"skills": ["AWS"]},
            job_title="Staff Engineer",
        )
        cloud = [r for r in a.requirements if r.text.startswith("Cloud (one of")]
        assert len(cloud) == 1
        assert cloud[0].status == "met"

    @pytest.mark.parametrize(
        "jd",
        [
            "We are a small team that values curiosity and craft.",
            "Join our mission to do great things.",
            "About us: we build tools for people.",
        ],
    )
    def test_signal_less_jd_yields_no_requirements(self, jd):
        a = build_deterministic_assessment(
            jd, None, resume_text="Alice.", job_title="Engineer"
        )
        assert a.requirements == []

    def test_prose_bullets_do_not_enter_must_have_denominator(self):
        """Residual mining must not turn responsibilities prose into must-haves (#52).

        Every prose bullet below is fully evidenced by the resume, which is exactly what made
        them score as `partial` must-haves and cap must coverage.
        """
        jd = (
            "Requirements:\n"
            "Python and Kubernetes required.\n"
            "- Lead the design and end-to-end delivery of complex platform systems.\n"
            "- Drive architectural decisions and system-level thinking across teams.\n"
            "- Mentor and coach other engineers through technical guidance and reviews.\n"
            "- Coding and delivery are core to who you are.\n"
        )
        resume = (
            "Python, Kubernetes. Led the design and end-to-end delivery of complex platform "
            "systems. Drove architectural decisions and system-level thinking across teams. "
            "Mentored and coached other engineers through technical guidance and reviews. "
            "Coding and delivery are core to who I am."
        )
        a = build_deterministic_assessment(jd, {"skills": ["Python", "Kubernetes"]}, resume_text=resume)
        must = [r for r in a.requirements if r.category == "must_have"]
        texts = " ".join(r.text.lower() for r in must)
        assert "lead the design" not in texts
        assert "coding and delivery" not in texts
        assert "mentor and coach" not in texts
        assert {r.text for r in must if r.kind == "skill"} == {"Python", "Kubernetes"}


class TestAmbiguousProductNames:
    """Curated skills that are also ordinary English words (issue #50).

    A case-insensitive scan reads JD prose as a requirement: "during the workday", "we go fast",
    "you react to change", "we express ourselves". Only canonical casing or an explicit alias
    counts.
    """

    PROSE_JD = (
        "Requirements\n"
        "You will have focus time during the workday. We go fast and express ourselves; "
        "you react to change.\n"
        "Must have Python.\n"
    )

    @pytest.mark.parametrize("skill", ["Workday", "Go", "React", "Express"])
    def test_prose_does_not_become_a_requirement(self, skill):
        a = build_deterministic_assessment(
            self.PROSE_JD, {"skills": ["Python"]}, resume_text="Python"
        )
        assert skill not in {r.text for r in a.requirements}

    def test_only_the_real_requirement_survives(self):
        a = build_deterministic_assessment(
            self.PROSE_JD, {"skills": ["Python"]}, resume_text="Python"
        )
        assert [r.text for r in a.requirements] == ["Python"]

    @pytest.mark.parametrize(
        "jd, skill",
        [
            ("Requirements\nGo developer with distributed systems experience.", "Go"),
            ("Requirements\nReact and TypeScript for the frontend.", "React"),
            ("Requirements\nExpress.js services and REST APIs.", "Express"),
            ("Requirements\nWorkday integration experience required.", "Workday"),
            ("Requirements\nExperience with HR systems such as Workday, Greenhouse.", "Workday"),
        ],
    )
    def test_canonical_casing_still_matches(self, jd, skill):
        a = build_deterministic_assessment(jd, {"skills": []}, resume_text="")
        assert skill in {r.text for r in a.requirements}

    @pytest.mark.parametrize(
        "jd, skill",
        [
            ("Requirements\nWe write golang services.", "Go"),
            ("Requirements\nreactjs and nextjs experience.", "React"),
            ("Requirements\nexpressjs middleware.", "Express"),
        ],
    )
    def test_unambiguous_aliases_still_match(self, jd, skill):
        a = build_deterministic_assessment(jd, {"skills": []}, resume_text="")
        assert skill in {r.text for r in a.requirements}

    def test_all_caps_still_matches(self):
        """An uppercase occurrence is a deliberate technology mention, not prose."""
        a = build_deterministic_assessment(
            "Requirements\nGO and REACT experience.", {"skills": []}, resume_text=""
        )
        texts = {r.text for r in a.requirements}
        assert {"Go", "React"} <= texts

    def test_lowercase_alias_is_unaffected_by_the_casing_rule(self):
        """Only the canonical surface needs casing; aliases are unambiguous by construction."""
        a = build_deterministic_assessment(
            "Requirements\nexperience with golang", {"skills": []}, resume_text=""
        )
        assert "Go" in {r.text for r in a.requirements}


class TestEmptyNiceToHaveSet:
    """A JD with no preferred section states no nice-to-haves (issue #70).

    ``nice_coverage`` stays 0.0 when the set is empty, so weighting it at 20/100 scored the
    missing set as "none met" and capped such JDs at 80.
    """

    @staticmethod
    def _features(must_cov, nice_cov, *, must_count, nice_count, seniority=1.0, education=1.0):
        return MatchFeatures(
            must_coverage=must_cov,
            nice_coverage=nice_cov,
            seniority_fit=seniority,
            education_match=education,
            experience_years_delta=0.0,
            over_qualified_flag=0.0,
            must_count=must_count,
            nice_count=nice_count,
        )

    def test_no_nice_section_is_scored_over_the_remaining_eighty_points(self):
        classifier = SimpleHeuristicClassifier()
        features = self._features(0.5, 0.0, must_count=8, nice_count=0)
        # (0.5*60 + 1.0*15 + 1.0*5) / 80 * 100 == 62.5, not the legacy 45.0.
        assert classifier.predict(features) == pytest.approx(62.5)

    def test_a_perfect_fit_without_a_preferred_section_can_reach_one_hundred(self):
        """The old formula capped such a JD at 80 no matter how well the candidate matched."""
        classifier = SimpleHeuristicClassifier()
        features = self._features(1.0, 0.0, must_count=8, nice_count=0)
        assert classifier.predict(features) == pytest.approx(100.0)

    def test_a_jd_with_nice_to_haves_keeps_its_legacy_score(self):
        """The renormalisation must be inert when the set is present."""
        classifier = SimpleHeuristicClassifier()
        features = self._features(0.6, 0.75, must_count=5, nice_count=4)
        legacy = 0.6 * 60 + 0.75 * 20 + 1.0 * 15 + 1.0 * 5
        assert classifier.predict(features) == pytest.approx(legacy)

    def test_partial_nice_coverage_still_costs_points(self):
        """An empty set is free; a present-but-unmet set is not."""
        classifier = SimpleHeuristicClassifier()
        unmet = self._features(0.6, 0.0, must_count=5, nice_count=4)
        met = self._features(0.6, 1.0, must_count=5, nice_count=4)
        absent = self._features(0.6, 0.0, must_count=5, nice_count=0)
        assert classifier.predict(unmet) < classifier.predict(absent) < classifier.predict(met)

    def test_no_requirements_at_all_keeps_the_legacy_full_scale(self):
        """With nothing to verify, the score stays low instead of renormalising over 20 points."""
        classifier = SimpleHeuristicClassifier()
        features = self._features(0.0, 0.0, must_count=0, nice_count=0)
        assert classifier.predict(features) == pytest.approx(20.0)

    def test_features_report_an_empty_nice_set_as_zero_count(self):
        assessment = build_deterministic_assessment(
            "Requirements\nMust have Python and Kubernetes.",
            {"skills": ["Python", "Kubernetes"]},
            resume_text="Python, Kubernetes",
        )
        features = extract_match_features(
            {"skills": ["Python", "Kubernetes"]},
            build_jd_hints("Requirements\nMust have Python and Kubernetes.", "Backend Engineer"),
            assessment=assessment,
        )
        assert features.nice_count == 0
        assert features.must_count > 0
