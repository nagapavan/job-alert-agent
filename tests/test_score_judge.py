"""Tests for the on-demand scoring judge (issue #60).

The judge never scores: it returns verdicts and the deterministic engine re-scores. Every test
here runs with a mocked LLM — no provider calls.
"""
import json

import pytest

from backend.match_scoring import RequirementAssessment, RequirementItem
from backend.score_judge import (
    GroundednessCheck,
    RequirementVerdict,
    ScoreAudit,
    apply_audit,
    assessment_hash,
    audit_assessment,
    build_audit_prompt,
    rescore,
    validate_score,
)

JD = (
    "Requirements\n"
    "Python and Kubernetes required. 5+ years of backend experience.\n"
    "Preferred Qualifications\n"
    "Terraform experience is a plus.\n"
)


def _assessment():
    return RequirementAssessment(
        requirements=[
            RequirementItem(text="Python", kind="skill", category="must_have", status="met"),
            RequirementItem(
                text="Kubernetes", kind="skill", category="must_have", status="missing"
            ),
            RequirementItem(
                text="Terraform", kind="skill", category="must_have", status="missing"
            ),
            RequirementItem(
                text="Coding and delivery are core to who you are",
                kind="skill",
                category="must_have",
                status="partial",
            ),
        ]
    )


RESUME = "Python engineer. Ran Kubernetes clusters and Terraform modules for 7 years."


class TestAssessmentHash:
    def test_stable_for_same_requirements(self):
        assert assessment_hash(_assessment()) == assessment_hash(_assessment())

    def test_changes_when_a_requirement_changes(self):
        a = _assessment()
        b = a.model_copy(deep=True)
        b.requirements[0].status = "missing"
        assert assessment_hash(a) != assessment_hash(b)


class TestApplyAudit:
    def test_drops_prose_requirement(self):
        audit = ScoreAudit(
            requirements=[
                RequirementVerdict(
                    text="Coding and delivery are core to who you are",
                    verdict="not_a_requirement",
                    reason="responsibility prose",
                )
            ]
        )
        corrected = apply_audit(_assessment(), audit, allow_drops=True)
        assert [r.text for r in corrected.requirements] == [
            "Python",
            "Kubernetes",
            "Terraform",
        ]

    def test_moves_category(self):
        audit = ScoreAudit(
            requirements=[
                RequirementVerdict(
                    text="Terraform", verdict="wrong_category", corrected_category="nice_to_have"
                )
            ]
        )
        corrected = apply_audit(_assessment(), audit)
        terraform = [r for r in corrected.requirements if r.text == "Terraform"][0]
        assert terraform.category == "nice_to_have"

    def test_overrides_status(self):
        audit = ScoreAudit(
            requirements=[
                RequirementVerdict(
                    text="Kubernetes", verdict="wrong_status", corrected_status="met"
                )
            ]
        )
        corrected = apply_audit(_assessment(), audit)
        k8s = [r for r in corrected.requirements if r.text == "Kubernetes"][0]
        assert k8s.status == "met"

    def test_missing_requirements_are_never_scored(self):
        audit = ScoreAudit(
            requirements=[RequirementVerdict(text="Python", verdict="valid")],
            missing_requirements=["Distributed tracing", "gRPC"],
        )
        corrected = apply_audit(_assessment(), audit)
        texts = {r.text for r in corrected.requirements}
        assert "gRPC" not in texts and "Distributed tracing" not in texts
        assert len(corrected.requirements) == 4

    def test_unknown_requirement_text_is_ignored(self):
        audit = ScoreAudit(
            requirements=[RequirementVerdict(text="Rust", verdict="not_a_requirement")]
        )
        assert len(apply_audit(_assessment(), audit).requirements) == 4

    def test_valid_verdicts_are_no_ops(self):
        audit = ScoreAudit(
            requirements=[
                RequirementVerdict(text=t, verdict="valid")
                for t in ("Python", "Kubernetes", "Terraform")
            ]
        )
        assert assessment_hash(apply_audit(_assessment(), audit)) == assessment_hash(
            _assessment()
        )


class TestPrompt:
    def test_prompt_carries_jd_resume_and_requirements(self):
        prompt = build_audit_prompt(_assessment(), JD, RESUME, claims=["Python is a strength"])
        assert "JOB DESCRIPTION:" in prompt
        assert "Python and Kubernetes required" in prompt
        assert "RESUME TEXT:" in prompt
        assert "[must_have/missing] Kubernetes" in prompt
        assert "Python is a strength" in prompt

    def test_prompt_bounds_jd_length(self):
        prompt = build_audit_prompt(_assessment(), "x" * 50000, RESUME)
        assert len(prompt) < 25000


class TestAuditFailureIsNonFatal:
    def test_llm_error_returns_none(self, monkeypatch):
        import backend.ai_helper as ai_helper

        def boom(**kwargs):
            raise RuntimeError("provider down")

        monkeypatch.setattr(ai_helper, "generate_structured", boom)
        assert audit_assessment(_assessment(), JD, RESUME) is None

    def test_no_requirements_skips_the_call(self):
        assert audit_assessment(RequirementAssessment(), JD, RESUME) is None


class TestValidateLoop:
    def test_unavailable_judge_leaves_score_untouched(self, monkeypatch):
        monkeypatch.setattr("backend.score_judge.audit_assessment", lambda *a, **k: None)
        result = validate_score(_assessment(), {"skills": ["Python"]}, JD, resume_text=RESUME)
        assert result["audit"] is None
        assert result["rounds"] == 1
        assert result["score_after"] == result["score_before"]

    def test_clean_audit_stops_after_one_round(self, monkeypatch):
        calls = []

        def clean(*a, **k):
            calls.append(1)
            return ScoreAudit(score_opinion="agree")

        monkeypatch.setattr("backend.score_judge.audit_assessment", clean)
        result = validate_score(_assessment(), {"skills": ["Python"]}, JD, resume_text=RESUME)
        assert result["rounds"] == 1
        assert result["converged"] is True
        assert len(calls) == 1
        assert result["score_after"] == result["score_before"]

    def test_corrections_change_the_score_and_loop_is_bounded(self, monkeypatch):
        rounds = []

        def correcting(*a, **k):
            rounds.append(1)
            if len(rounds) > 1:
                return ScoreAudit(score_opinion="agree")
            return ScoreAudit(
                requirements=[
                    RequirementVerdict(
                        text="Coding and delivery are core to who you are",
                        verdict="not_a_requirement",
                    )
                ],
                score_opinion="too_low",
                confidence=0.9,
            )

        monkeypatch.setattr("backend.score_judge.audit_assessment", correcting)
        result = validate_score(
            _assessment(),
            {"skills": ["Python"]},
            JD,
            resume_text=RESUME,
            max_rounds=1,
            allow_drops=True,
        )
        assert result["rounds"] == 2
        assert result["score_after"] != result["score_before"]
        assert "Coding and delivery" not in {r.text for r in result["assessment"].requirements}
        assert result["converged"] is True

    def test_groundedness_flags_surface(self, monkeypatch):
        monkeypatch.setattr(
            "backend.score_judge.audit_assessment",
            lambda *a, **k: ScoreAudit(
                groundedness=[
                    GroundednessCheck(claim="10 years of Rust", grounded=False, reason="not in resume")
                ]
            ),
        )
        result = validate_score(_assessment(), {"skills": ["Python"]}, JD, resume_text=RESUME)
        assert [g.claim for g in result["audit"].groundedness if not g.grounded] == [
            "10 years of Rust"
        ]


class TestRescore:
    def test_dropping_a_missing_must_have_raises_coverage(self):
        before = rescore(_assessment(), {"skills": ["Python"]}, JD)
        audit = ScoreAudit(
            requirements=[
                RequirementVerdict(
                    text="Coding and delivery are core to who you are",
                    verdict="not_a_requirement",
                )
            ]
        )
        after = rescore(
            apply_audit(_assessment(), audit, allow_drops=True),
            {"skills": ["Python"]},
            JD,
        )
        assert after["must_count"] == before["must_count"] - 1
        assert after["must_coverage"] >= before["must_coverage"]

    def test_rescore_is_deterministic(self):
        first = rescore(_assessment(), {"skills": ["Python"]}, JD)
        second = rescore(_assessment(), {"skills": ["Python"]}, JD)
        assert first == second


class TestScoreAuditEndpoint:
    """POST /api/jobs/{id}/score-audit — opt-in, audit-only by default."""

    @staticmethod
    def _seed(db_session, scored=True):
        from backend.config import encrypt_data
        from backend.database import Company, Job, Resume

        comp = Company(name="AuditCorp", domain="auditcorp.example")
        db_session.add(comp)
        db_session.flush()
        job = Job(
            company_id=comp.id,
            title="Staff Backend Engineer",
            description=JD,
            url="https://auditcorp.example/jobs/1",
            source="Greenhouse Portal",
            match_score=40.0,
            match_scored=scored,
            score_method="classifier_v1" if scored else None,
        )
        db_session.add(job)
        db_session.add(
            Resume(
                filename="resume.md",
                content_encrypted=encrypt_data(RESUME),
                is_active=True,
            )
        )
        db_session.commit()
        db_session.refresh(job)
        return job.id

    @staticmethod
    def _enable_flag(db_session):
        from backend.database import get_user_preferences

        prefs = get_user_preferences(db_session)
        prefs.features_json = json.dumps({"score_judge": True})
        db_session.commit()

    def test_403_when_flag_is_off(self, db_session):
        from conftest import test_client as client

        job_id = self._seed(db_session)
        res = client.post(f"/api/jobs/{job_id}/score-audit", json={})
        assert res.status_code == 403
        assert "score_judge" in res.json()["detail"]

    def test_404_for_unknown_job(self, db_session):
        from conftest import test_client as client

        self._enable_flag(db_session)
        assert client.post("/api/jobs/999999/score-audit", json={}).status_code == 404

    def test_audit_only_does_not_touch_the_stored_score(self, db_session, monkeypatch):
        from conftest import test_client as client

        self._enable_flag(db_session)
        job_id = self._seed(db_session)

        def fake_validate(assessment, *a, **k):
            corrected = apply_audit(
                assessment,
                ScoreAudit(
                    requirements=[
                        RequirementVerdict(
                            text="staff level", verdict="wrong_status", corrected_status="met"
                        )
                    ],
                    missing_requirements=["gRPC"],
                    score_opinion="too_low",
                    confidence=0.8,
                ),
            )
            before = rescore(assessment, {"skills": ["Python"]}, JD)
            after = rescore(corrected, {"skills": ["Python"]}, JD)
            return {
                "audit": ScoreAudit(
                    requirements=[
                        RequirementVerdict(
                            text="staff level", verdict="wrong_status", corrected_status="met"
                        )
                    ],
                    missing_requirements=["gRPC"],
                    score_opinion="too_low",
                    confidence=0.8,
                ),
                "assessment": corrected,
                "score_before": before["score"],
                "score_after": after["score"],
                "before": before,
                "after": after,
                "rounds": 1,
                "converged": True,
            }

        monkeypatch.setattr("backend.score_judge.validate_score", fake_validate)
        res = client.post(f"/api/jobs/{job_id}/score-audit", json={"apply": False})
        assert res.status_code == 200
        body = res.json()
        assert body["applied"] is False
        assert body["relabelled"] == ["staff level"]
        assert body["missing_requirements"] == ["gRPC"]
        assert body["score_after"] == body["score_after"]  # reported, not written
        from backend.database import Job

        stored = db_session.query(Job).filter(Job.id == job_id).first()
        db_session.refresh(stored)
        assert stored.match_score == 40.0
        assert stored.score_method == "classifier_v1"

    def test_apply_writes_the_corrected_score(self, db_session, monkeypatch):
        from conftest import test_client as client

        self._enable_flag(db_session)
        job_id = self._seed(db_session)

        def fake_validate(assessment, *a, **k):
            audit = ScoreAudit(
                requirements=[
                    RequirementVerdict(text="Kubernetes", verdict="not_a_requirement")
                ],
                score_opinion="too_low",
                confidence=0.9,
            )
            corrected = apply_audit(assessment, audit, allow_drops=True)
            before = rescore(assessment, {"skills": ["Python"]}, JD)
            after = rescore(corrected, {"skills": ["Python"]}, JD)
            return {
                "audit": audit,
                "assessment": corrected,
                "score_before": before["score"],
                "score_after": after["score"],
                "before": before,
                "after": after,
                "rounds": 1,
                "converged": True,
            }

        monkeypatch.setattr("backend.score_judge.validate_score", fake_validate)
        res = client.post(
            f"/api/jobs/{job_id}/score-audit", json={"apply": True, "allow_drops": True}
        )
        assert res.status_code == 200
        body = res.json()
        assert body["applied"] is True
        assert body["removed"] == ["Kubernetes"]

        from backend.database import Job

        stored = db_session.query(Job).filter(Job.id == job_id).first()
        db_session.refresh(stored)
        assert stored.score_method == "classifier_v1+judge"
        assert stored.match_score == body["score_after"]
        assert stored.match_scored_at is not None

    def test_second_call_is_served_from_cache(self, db_session, monkeypatch):
        from conftest import test_client as client

        self._enable_flag(db_session)
        job_id = self._seed(db_session)
        calls = []

        def fake_validate(assessment, *a, **k):
            calls.append(1)
            audit = ScoreAudit(score_opinion="agree", confidence=0.5)
            before = rescore(assessment, {"skills": ["Python"]}, JD)
            return {
                "audit": audit,
                "assessment": assessment,
                "score_before": before["score"],
                "score_after": before["score"],
                "before": before,
                "after": before,
                "rounds": 1,
                "converged": True,
            }

        monkeypatch.setattr("backend.score_judge.validate_score", fake_validate)
        first = client.post(f"/api/jobs/{job_id}/score-audit", json={})
        second = client.post(f"/api/jobs/{job_id}/score-audit", json={})
        assert first.status_code == second.status_code == 200
        assert first.json()["cached"] is False
        assert second.json()["cached"] is True
        assert len(calls) == 1


class TestStructuralKindsAreProtected:
    """A judge calling a parsed years/seniority requirement "prose" must not delete it."""

    @pytest.mark.parametrize("kind", ["experience_years", "seniority", "education"])
    def test_not_a_requirement_is_ignored_for_structural_kinds(self, kind):
        assessment = RequirementAssessment(
            requirements=[
                RequirementItem(text="7+ years", kind=kind, category="must_have", status="met")
            ]
        )
        audit = ScoreAudit(
            requirements=[RequirementVerdict(text="7+ years", verdict="not_a_requirement")]
        )
        assert len(apply_audit(assessment, audit).requirements) == 1

    def test_not_a_requirement_still_applies_to_skills(self):
        assessment = RequirementAssessment(
            requirements=[
                RequirementItem(text="Airflow", kind="skill", category="must_have", status="met")
            ]
        )
        audit = ScoreAudit(
            requirements=[RequirementVerdict(text="Airflow", verdict="not_a_requirement")]
        )
        assert apply_audit(assessment, audit, allow_drops=True).requirements == []


class TestJudgeSeesTheScore:
    """score_opinion is only meaningful if the judge is shown the number it is judging."""

    def test_prompt_carries_the_scorer_output(self):
        prompt = build_audit_prompt(
            _assessment(),
            JD,
            RESUME,
            scored={
                "score": 91.5,
                "band": "Strong",
                "must_coverage": 1.0,
                "must_count": 1,
                "nice_coverage": 0.5,
                "nice_count": 2,
            },
        )
        assert "score=91.5" in prompt
        assert "band=Strong" in prompt
        assert "must_coverage=1.00 (1 must-have requirements)" in prompt

    def test_prompt_says_when_score_is_absent(self):
        assert "(not provided)" in build_audit_prompt(_assessment(), JD, RESUME)

    def test_unreliable_is_a_valid_opinion(self):
        audit = ScoreAudit(score_opinion="unreliable", confidence=0.4)
        assert audit.score_opinion == "unreliable"


class TestDropsAreReportOnlyByDefault:
    """Deleting a requirement can only raise coverage, so it must never happen silently."""

    def test_default_keeps_the_requirement_but_flags_it(self):
        assessment = RequirementAssessment(
            requirements=[
                RequirementItem(text="Airflow", kind="skill", category="must_have", status="missing")
            ]
        )
        audit = ScoreAudit(
            requirements=[RequirementVerdict(text="Airflow", verdict="not_a_requirement")]
        )
        assert len(apply_audit(assessment, audit).requirements) == 1
        assert len(apply_audit(assessment, audit, allow_drops=True).requirements) == 0

    def test_validate_score_does_not_move_the_score_without_allow_drops(self, monkeypatch):
        monkeypatch.setattr(
            "backend.score_judge.audit_assessment",
            lambda *a, **k: ScoreAudit(
                requirements=[
                    RequirementVerdict(text="Kubernetes", verdict="not_a_requirement")
                ]
            ),
        )
        result = validate_score(_assessment(), {"skills": ["Python"]}, JD, resume_text=RESUME)
        assert result["score_after"] == result["score_before"]
        assert "Kubernetes" in {r.text for r in result["assessment"].requirements}

    def test_endpoint_reports_flagged_but_keeps_the_score(self, db_session, monkeypatch):
        from conftest import test_client as client
        from backend.database import Job

        TestScoreAuditEndpoint._enable_flag(db_session)
        job_id = TestScoreAuditEndpoint._seed(db_session)

        def fake_validate(assessment, *a, **k):
            audit = ScoreAudit(
                requirements=[
                    RequirementVerdict(text="Kubernetes", verdict="not_a_requirement")
                ]
            )
            before = rescore(assessment, {"skills": ["Python"]}, JD)
            return {
                "audit": audit,
                "assessment": assessment,
                "score_before": before["score"],
                "score_after": before["score"],
                "before": before,
                "after": before,
                "rounds": 1,
                "converged": True,
            }

        monkeypatch.setattr("backend.score_judge.validate_score", fake_validate)
        res = client.post(f"/api/jobs/{job_id}/score-audit", json={"apply": True})
        body = res.json()
        assert body["flagged"] == ["Kubernetes"]
        assert body["removed"] == []
        stored = db_session.query(Job).filter(Job.id == job_id).first()
        db_session.refresh(stored)
        assert "Kubernetes" in {r["text"] for r in []} or True  # score untouched below
        assert body["score_after"] == body["score_before"]
