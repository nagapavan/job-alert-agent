"""Tests for Tier A/B residual requirement extraction + resume-evidence bridge (Phase 3)."""
from backend import requirements_extractor as rx
from backend import section_intent as si


class TestKeyPhrase:
    def test_strips_leadins(self):
        assert rx.key_phrase("Hands-on experience with Kubernetes") == "Kubernetes"

    def test_caps_words(self):
        phrase = rx.key_phrase("a b c d e f g h i j k")
        assert len(phrase.split()) <= 8

    def test_empty(self):
        assert rx.key_phrase("   ") == ""


class TestCoverageCheck:
    def test_taxonomy_skill_is_covered(self):
        assert rx.unit_covered_by_taxonomy("Strong Python and Kubernetes skills") is True

    def test_unknown_phrase_not_covered(self):
        assert rx.unit_covered_by_taxonomy("Demonstrated mentorship of engineers") is False


class TestResumeEvidence:
    def test_token_overlap_supported(self):
        supported, score, how = rx.resume_supports(
            "stakeholder management", "Led stakeholder management across teams."
        )
        assert supported is True
        assert how == "token_overlap"

    def test_no_evidence(self):
        supported, _s, how = rx.resume_supports(
            "payroll processing", "Built React frontends with TypeScript."
        )
        assert supported is False
        assert how == "none"

    def test_empty_phrase(self):
        assert rx.resume_supports("", "anything") == (False, 0.0, "none")


class TestResidualExtraction:
    def test_supported_residual_is_credited_and_unknown_is_queued(self, monkeypatch, tmp_path):
        monkeypatch.setattr("backend.skill_taxonomy.CANDIDATES_PATH", tmp_path / "c.json")
        jd = (
            "Requirements:\n"
            "- Demonstrated mentorship of less-experienced engineers.\n"
            "- Payroll and benefits domain depth.\n"
        )
        spans = si.jd_section_spans(jd)
        resume = "I have demonstrated mentorship of junior engineers throughout my career."
        results = rx.extract_residual_requirements(jd, spans, resume_text=resume)
        texts = [r["text"] for r in results]
        # The mentorship unit is evidenced by the resume -> credited.
        assert any("mentorship" in t.lower() for t in texts)
        # The payroll unit has no resume evidence -> queued, never scored.
        assert not any("payroll" in t.lower() for t in texts)

    def test_preferred_category_preserved(self, monkeypatch, tmp_path):
        monkeypatch.setattr("backend.skill_taxonomy.CANDIDATES_PATH", tmp_path / "c.json")
        jd = "Preferred:\n- Demonstrated mentorship of engineers.\n"
        spans = si.jd_section_spans(jd)
        results = rx.extract_residual_requirements(
            jd, spans, resume_text="demonstrated mentorship of engineers"
        )
        assert results and all(r["category"] == "nice_to_have" for r in results)
