"""Human-in-the-loop outreach guidance (issue #27).

Some portals explicitly require human-written text, so the tool must not fill those fields. The
guidance surface lists the JD must-have requirements the resume genuinely evidences, derived
deterministically, so nothing can be fabricated.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backend.ai_helper import build_outreach_guidance
from backend.config import encrypt_data
from backend.database import Company, Job, Resume
from conftest import test_client as client

JD = (
    "Requirements\n"
    "Must have Python and Kubernetes experience.\n"
    "Must have Rust.\n"
)
RESUME = "Backend engineer. Python and Kubernetes in production."


class TestGuidanceContent:
    def test_lists_only_requirements_the_resume_evidences(self):
        points = build_outreach_guidance(JD, RESUME, "Backend Engineer")
        assert [p["requirement"] for p in points] == ["Python", "Kubernetes"]
        assert all(p["status"] == "met" for p in points)

    def test_a_resume_lacking_the_stack_yields_no_fabricated_talking_points(self):
        assert build_outreach_guidance(JD, "Marketing manager.", "Backend Engineer") == []

    def test_unmet_requirements_are_never_suggested(self):
        points = build_outreach_guidance(JD, RESUME, "Backend Engineer")
        assert "Rust" not in {p["requirement"] for p in points}

    def test_no_job_description_yields_nothing(self):
        assert build_outreach_guidance("", RESUME, "Backend Engineer") == []

    def test_each_point_carries_an_angle(self):
        for point in build_outreach_guidance(JD, RESUME, "Backend Engineer"):
            assert point["angle"].strip()
            assert point["requirement"] in point["angle"]

    def test_results_are_capped(self):
        points = build_outreach_guidance(JD, RESUME, "Backend Engineer", limit=1)
        assert len(points) == 1

    def test_points_are_deduplicated(self):
        jd = "Requirements\nMust have Python. Must have Python.\n"
        points = build_outreach_guidance(jd, "Python", "Engineer")
        assert len([p for p in points if p["requirement"] == "Python"]) == 1

    def test_guidance_is_deterministic(self):
        assert build_outreach_guidance(JD, RESUME, "Backend Engineer") == (
            build_outreach_guidance(JD, RESUME, "Backend Engineer")
        )


def _active_resume(db_session, text):
    resume = Resume(
        filename="resume.md",
        content_encrypted=encrypt_data(text),
        parsed_json_encrypted=encrypt_data(json.dumps({"skills": ["Python", "Kubernetes"]})),
        is_active=True,
    )
    db_session.add(resume)
    db_session.commit()
    return resume


def _job(db_session, *, draft=None):
    company = Company(name="GuidanceCorp", domain="guidance.example")
    db_session.add(company)
    db_session.flush()
    job = Job(
        company_id=company.id,
        title="Backend Engineer",
        description=JD,
        url="guidance-1",
        status="To Apply",
        cold_message_draft=draft,
    )
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)
    return job


class TestApiSurface:
    def test_guidance_is_returned_alongside_the_draft(self, db_session):
        _active_resume(db_session, RESUME)
        job = _job(db_session, draft="Hi, I build Python services.")

        data = client.get(f"/api/jobs/{job.id}").json()
        assert data["cold_message_draft"] == "Hi, I build Python services."
        assert [p["requirement"] for p in data["outreach_guidance"]] == ["Python", "Kubernetes"]

    def test_no_draft_means_no_guidance_payload(self, db_session):
        _active_resume(db_session, RESUME)
        job = _job(db_session, draft=None)

        data = client.get(f"/api/jobs/{job.id}").json()
        assert data["outreach_guidance"] is None

    def test_guidance_never_breaks_serialization_without_a_resume(self, db_session):
        job = _job(db_session, draft="Hi.")
        res = client.get(f"/api/jobs/{job.id}")
        assert res.status_code == 200
        assert res.json()["outreach_guidance"] is None

    def test_the_feed_does_not_carry_guidance(self, db_session):
        """The list endpoint must stay cheap: guidance is single-job only."""
        _active_resume(db_session, RESUME)
        _job(db_session, draft="Hi.")
        rows = client.get("/api/jobs").json()
        assert all(row.get("outreach_guidance") is None for row in rows)
