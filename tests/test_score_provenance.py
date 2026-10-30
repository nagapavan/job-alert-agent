"""Score provenance: which resume version produced a stored score (issue #56).

Decisions recorded on the ticket:

- do not re-score on resume change; label the score with the version that produced it;
- ``unknown`` (rows scored before provenance existed) is treated as ``current``;
- re-scoring happens lazily on open, which is #57.
"""
import io
import json

import pytest

from backend.config import decrypt_data, encrypt_data
from backend.parser import resume_fingerprint

RESUME_A = "Alice\nPython, Kubernetes, 8 years.\n"
RESUME_B = "Alice\nPython, Kubernetes, Terraform, 9 years.\n"


def _seed(db_session, *, active_text=RESUME_A, other_texts=()):
    from backend.database import Resume

    active = Resume(
        filename="active.md",
        content_encrypted=encrypt_data(active_text),
        parsed_json_encrypted=encrypt_data(json.dumps({"skills": ["Python"]})),
        fingerprint=resume_fingerprint(active_text),
        is_active=True,
    )
    db_session.add(active)
    for index, text in enumerate(other_texts):
        db_session.add(
            Resume(
                filename=f"old-{index}.md",
                content_encrypted=encrypt_data(text),
                fingerprint=resume_fingerprint(text),
                is_active=False,
            )
        )
    db_session.commit()
    db_session.refresh(active)
    return active


def _seed_job(db_session, *, fingerprint, resume_id=None, score=70.0):
    from backend.database import Company, Job

    company = db_session.query(Company).first()
    if not company:
        company = Company(name="ProvCorp", domain="provcorp.example")
        db_session.add(company)
        db_session.flush()
    job = Job(
        company_id=company.id,
        title="Staff Engineer",
        description="Python and Kubernetes required.",
        url=f"https://provcorp.example/{fingerprint or 'none'}-{score}",
        source="Greenhouse Portal",
        status="To Apply",
        match_score=score,
        match_scored=True,
        score_method="classifier_v1",
        scored_resume_id=resume_id,
        scored_resume_fingerprint=fingerprint,
    )
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)
    return job


class TestFingerprint:
    def test_identical_text_is_the_same_version(self):
        assert resume_fingerprint(RESUME_A) == resume_fingerprint(RESUME_A)

    def test_edited_text_is_a_new_version(self):
        assert resume_fingerprint(RESUME_A) != resume_fingerprint(RESUME_B)

    def test_upload_stamps_the_fingerprint(self, monkeypatch):
        """The fingerprint is the hash of the stored text, not of the uploaded bytes."""
        from conftest import TestingSessionLocal, test_client as client
        from backend.database import Resume

        monkeypatch.setattr(
            "backend.main.parse_resume_to_json", lambda text: {"skills": ["Python"]}
        )
        res = client.post(
            "/api/resume/upload",
            files={"file": ("resume.md", io.BytesIO(RESUME_A.encode()), "text/markdown")},
        )
        assert res.status_code == 201
        db = TestingSessionLocal()
        try:
            row = db.query(Resume).order_by(Resume.id.desc()).first()
            stored_text = decrypt_data(row.content_encrypted)
            assert row.fingerprint == resume_fingerprint(stored_text)
        finally:
            db.close()


class TestScoreResumeState:
    def test_matching_fingerprint_is_current(self, db_session):
        from conftest import test_client as client

        resume = _seed(db_session)
        job = _seed_job(db_session, fingerprint=resume.fingerprint, resume_id=resume.id)
        body = client.get(f"/api/jobs/{job.id}").json()
        assert body["score_resume_state"] == "current"
        assert body["score_resume_filename"] == "active.md"

    def test_different_fingerprint_that_still_exists_is_stale(self, db_session):
        from conftest import test_client as client

        _seed(db_session, active_text=RESUME_B, other_texts=(RESUME_A,))
        job = _seed_job(db_session, fingerprint=resume_fingerprint(RESUME_A))
        assert client.get(f"/api/jobs/{job.id}").json()["score_resume_state"] == "stale"

    def test_fingerprint_with_no_row_is_deleted(self, db_session):
        from conftest import test_client as client

        _seed(db_session, active_text=RESUME_B)
        job = _seed_job(db_session, fingerprint=resume_fingerprint(RESUME_A))
        assert client.get(f"/api/jobs/{job.id}").json()["score_resume_state"] == "deleted"

    def test_rows_without_provenance_are_treated_as_current(self, db_session):
        from conftest import test_client as client

        _seed(db_session, active_text=RESUME_B)
        job = _seed_job(db_session, fingerprint=None)
        body = client.get(f"/api/jobs/{job.id}").json()
        assert body["score_resume_state"] == "current"
        assert body["score_resume_filename"] is None

    def test_feed_labels_every_job(self, db_session):
        from conftest import test_client as client

        resume = _seed(db_session, active_text=RESUME_B, other_texts=(RESUME_A,))
        _seed_job(db_session, fingerprint=resume.fingerprint, resume_id=resume.id, score=80.0)
        _seed_job(db_session, fingerprint=resume_fingerprint(RESUME_A), score=60.0)
        _seed_job(db_session, fingerprint="f" * 64, score=40.0)
        states = [j["score_resume_state"] for j in client.get("/api/jobs").json()]
        assert sorted(states) == ["current", "deleted", "stale"]


class TestMigrationBackfillsFingerprints:
    def test_backfill_computes_the_fingerprint_for_legacy_rows(self, tmp_path):
        """A row written before fingerprinting existed must get one on the next start."""
        import sqlite3

        from backend.database import init_db

        db_path = tmp_path / "legacy.db"
        init_db(f"sqlite:///{db_path}")
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO resumes (filename, content_encrypted, is_active) VALUES (?, ?, 1)",
            ("legacy.md", encrypt_data(RESUME_A)),
        )
        conn.execute("UPDATE resumes SET fingerprint = NULL")
        conn.commit()
        conn.close()

        init_db(f"sqlite:///{db_path}")

        conn = sqlite3.connect(db_path)
        stored = conn.execute("SELECT fingerprint FROM resumes").fetchone()[0]
        conn.close()
        assert stored == resume_fingerprint(RESUME_A)
