"""Scoped re-scoring: per-job re-score, stale-only bulk scope (issue #57)."""
import json
from unittest.mock import patch

from backend.config import encrypt_data
from backend.database import Company, Job, Resume
from backend.parser import resume_fingerprint
from conftest import test_client as client

JD = "We need a Python engineer to build distributed APIs and services."


def _resume(db_session, text="resume text", active=True):
    resume = Resume(
        filename="r.md",
        content_encrypted=encrypt_data(text),
        parsed_json_encrypted=encrypt_data(json.dumps({"skills": ["Python"]})),
        fingerprint=resume_fingerprint(text),
        is_active=active,
    )
    db_session.add(resume)
    db_session.commit()
    db_session.refresh(resume)
    return resume


def _job(db_session, *, url, fingerprint=None, score=50.0):
    company = db_session.query(Company).first()
    if not company:
        company = Company(name="RescoreCorp", domain="rescore.example")
        db_session.add(company)
        db_session.flush()
    job = Job(
        company_id=company.id,
        title="Backend Engineer",
        description=JD,
        url=url,
        status="To Apply",
        match_score=score,
        match_scored=True,
        score_method="classifier_v1",
        scored_resume_fingerprint=fingerprint,
    )
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)
    return job


class TestPerJobRescore:
    def test_rescore_returns_the_updated_job_with_current_provenance(self, db_session):
        resume = _resume(db_session)
        job = _job(db_session, url="u1", fingerprint="0" * 64)

        res = client.post(f"/api/jobs/{job.id}/rescore")
        assert res.status_code == 200
        body = res.json()
        assert body["match_scored"] is True
        assert body["score_method"] == "classifier_v1"
        assert body["score_resume_state"] == "current"

        db_session.refresh(job)
        assert job.scored_resume_fingerprint == resume.fingerprint
        assert job.scored_resume_id == resume.id
        assert job.match_scored_at is not None

    def test_rescore_is_idempotent(self, db_session):
        _resume(db_session)
        job = _job(db_session, url="u2")
        first = client.post(f"/api/jobs/{job.id}/rescore").json()
        second = client.post(f"/api/jobs/{job.id}/rescore").json()
        assert first["match_score"] == second["match_score"]
        assert first["match_band"] == second["match_band"]

    def test_404_for_unknown_job(self, db_session):
        _resume(db_session)
        assert client.post("/api/jobs/999999/rescore").status_code == 404

    def test_400_without_a_resume(self, db_session):
        job = _job(db_session, url="u3")
        res = client.post(f"/api/jobs/{job.id}/rescore")
        assert res.status_code == 400
        assert "resume" in res.json()["detail"].lower()

    def test_409_when_the_job_cannot_be_scored(self, db_session):
        _resume(db_session)
        job = _job(db_session, url="u4")
        job.description = "short"
        db_session.commit()
        res = client.post(f"/api/jobs/{job.id}/rescore")
        assert res.status_code == 409


class TestScopedBulkRescore:
    def _seed_three(self, db_session):
        active = _resume(db_session, text="current resume")
        current = _job(db_session, url="c1", fingerprint=active.fingerprint, score=10.0)
        stale = _job(db_session, url="s1", fingerprint="a" * 64, score=20.0)
        unknown = _job(db_session, url="u1", fingerprint=None, score=30.0)
        return active, current, stale, unknown

    def test_stale_only_skips_current_and_unknown_rows(self, db_session):
        active, current, stale, unknown = self._seed_three(db_session)
        current_id, stale_id, unknown_id = current.id, stale.id, unknown.id
        active_fp = active.fingerprint
        with patch("backend.main.get_db", return_value=iter([db_session])):
            from backend.main import _run_match_rescore_task_runner

            out = _run_match_rescore_task_runner(limit=50, stale_only=True)

        assert out["rescored"] == 1
        # The runner closes its session, so re-read instead of refreshing detached instances.
        rows = {
            j.id: j
            for j in db_session.query(Job)
            .filter(Job.id.in_([current_id, stale_id, unknown_id]))
            .all()
        }
        assert rows[current_id].match_score == 10.0  # untouched
        assert rows[unknown_id].match_score == 30.0  # unknown provenance is "current" (#56)
        assert rows[stale_id].scored_resume_fingerprint == active_fp  # re-scored

    def test_job_ids_touches_exactly_those_rows(self, db_session):
        _active, current, stale, unknown = self._seed_three(db_session)
        current_id, stale_id, unknown_id = current.id, stale.id, unknown.id
        with patch("backend.main.get_db", return_value=iter([db_session])):
            from backend.main import _run_match_rescore_task_runner

            out = _run_match_rescore_task_runner(limit=50, job_ids=[unknown_id])

        assert out["rescored"] == 1
        rows = {
            j.id: j
            for j in db_session.query(Job)
            .filter(Job.id.in_([current_id, stale_id, unknown_id]))
            .all()
        }
        assert rows[current_id].match_score == 10.0
        assert rows[stale_id].match_score == 20.0
        assert rows[unknown_id].match_scored_at is not None

    def test_limit_only_behaviour_is_unchanged(self, db_session):
        _active, current, stale, unknown = self._seed_three(db_session)
        with patch("backend.main.get_db", return_value=iter([db_session])):
            from backend.main import _run_match_rescore_task_runner

            out = _run_match_rescore_task_runner(limit=50)
        assert out["rescored"] == 3

    def test_endpoint_rejects_both_scopes(self, db_session):
        res = client.post(
            "/api/match/rescore", json={"stale_only": True, "job_ids": [1, 2]}
        )
        assert res.status_code == 400

    def test_endpoint_forwards_the_scope(self, db_session):
        with patch("backend.main.task_engine.submit_task") as submit:
            submit.return_value = "tsk_test"
            res = client.post("/api/match/rescore", json={"stale_only": True, "limit": 10})
        assert res.status_code == 202
        assert submit.call_args.kwargs["stale_only"] is True
        assert "stale" in res.json()["message"]
