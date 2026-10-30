"""Resume version management: listing, score counts, per-version delete (issue #58)."""
import json

from backend.config import encrypt_data
from backend.database import Company, Job, Resume
from backend.parser import resume_fingerprint
from conftest import test_client as client

TEXT_A = "Alice\nPython engineer.\n"
TEXT_B = "Alice\nPython and Kubernetes engineer.\n"
TEXT_C = "Alice\nPython, Kubernetes and Go engineer.\n"


def _resume(db_session, text, *, filename="resume.md", active=False, version=2):
    resume = Resume(
        filename=filename,
        content_encrypted=encrypt_data(text),
        parsed_json_encrypted=encrypt_data(json.dumps({"skills": ["Python"]})),
        parser_version=version,
        fingerprint=resume_fingerprint(text),
        is_active=active,
    )
    db_session.add(resume)
    db_session.commit()
    db_session.refresh(resume)
    return resume


def _scored_job(db_session, url, fingerprint, score=70.0):
    company = db_session.query(Company).first()
    if not company:
        company = Company(name="VersionCorp", domain="versioncorp.example")
        db_session.add(company)
        db_session.flush()
    job = Job(
        company_id=company.id,
        title="Backend Engineer",
        description="Python required.",
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


class TestVersionList:
    def test_every_version_is_listed_with_its_score_count(self, db_session):
        old = _resume(db_session, TEXT_A, filename="same.md")
        current = _resume(db_session, TEXT_B, filename="same.md", active=True)
        _scored_job(db_session, "v1", old.fingerprint)
        _scored_job(db_session, "v2", old.fingerprint)
        _scored_job(db_session, "v3", current.fingerprint)

        rows = client.get("/api/resumes").json()
        by_id = {r["id"]: r for r in rows}
        assert by_id[old.id]["score_count"] == 2
        assert by_id[current.id]["score_count"] == 1
        # Duplicate filenames are expected; the list must still distinguish the rows by id.
        assert {r["filename"] for r in rows} == {"same.md"}
        assert len(by_id) == 2

    def test_unused_version_reports_zero_scores(self, db_session):
        _resume(db_session, TEXT_A, active=True)
        spare = _resume(db_session, TEXT_C)
        rows = {r["id"]: r for r in client.get("/api/resumes").json()}
        assert rows[spare.id]["score_count"] == 0

    def test_parse_staleness_is_reported_per_version(self, db_session):
        _resume(db_session, TEXT_A, active=True, version=1)
        fresh = _resume(db_session, TEXT_B, version=99)
        rows = {r["id"]: r for r in client.get("/api/resumes").json()}
        assert rows[fresh.id]["parse_stale"] is False


class TestPerVersionDelete:
    def test_inactive_version_can_be_deleted_without_touching_the_active_one(
        self, db_session
    ):
        active = _resume(db_session, TEXT_A, active=True)
        spare = _resume(db_session, TEXT_C, filename="old.md")

        res = client.delete(f"/api/resume/{spare.id}")
        assert res.status_code == 200
        body = res.json()
        assert body["deleted_id"] == spare.id
        assert body["active_resume_filename"] is None  # nothing was re-activated

        remaining = {r["id"] for r in client.get("/api/resumes").json()}
        assert remaining == {active.id}
        assert client.get("/api/resume").json()["id"] == active.id

    def test_deleting_the_active_version_activates_the_newest_remaining(self, db_session):
        newest = _resume(db_session, TEXT_A, active=True)
        older = _resume(db_session, TEXT_B, filename="older.md")

        res = client.delete(f"/api/resume/{newest.id}")
        assert res.status_code == 200
        assert res.json()["active_resume_filename"] == "older.md"
        assert client.get("/api/resume").json()["id"] == older.id

    def test_delete_reports_how_many_scores_it_orphans(self, db_session):
        active = _resume(db_session, TEXT_A, active=True)
        _scored_job(db_session, "o1", active.fingerprint)
        _scored_job(db_session, "o2", active.fingerprint)

        body = client.delete(f"/api/resume/{active.id}").json()
        assert body["orphaned_scores"] == 2
        assert "labelled 'deleted'" in body["message"]

    def test_deleting_a_version_turns_its_scores_into_deleted_state(self, db_session):
        active = _resume(db_session, TEXT_A, active=True)
        job = _scored_job(db_session, "d1", active.fingerprint)
        assert client.get(f"/api/jobs/{job.id}").json()["score_resume_state"] == "current"

        client.delete(f"/api/resume/{active.id}")

        # No resume row carries that fingerprint any more; the score keeps its value.
        body = client.get(f"/api/jobs/{job.id}").json()
        assert body["score_resume_state"] == "deleted"
        assert body["match_score"] == 70.0

    def test_404_for_unknown_version(self, db_session):
        assert client.delete("/api/resume/999999").status_code == 404
