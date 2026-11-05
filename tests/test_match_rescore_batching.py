"""Batched re-score commits: a row deleted mid-run must not roll back the run (issue #74)."""
import json
from unittest.mock import patch

from sqlalchemy import text

from backend.config import encrypt_data
from backend.database import Company, Job, OperationLog, Resume
from backend.main import ScoringResult, _run_match_rescore_task_runner
from backend.parser import resume_fingerprint

JD = "We need a Python engineer to build distributed APIs and services."


def _scored(method="classifier_v1"):
    return ScoringResult(
        82.0, "Strengths: Python", True, method, None, "Strong", "apply", ["Python"], [], []
    )


def _resume(db_session):
    resume = Resume(
        filename="r.md",
        content_encrypted=encrypt_data("resume text"),
        parsed_json_encrypted=encrypt_data(json.dumps({"skills": ["Python"]})),
        fingerprint=resume_fingerprint("resume text"),
        is_active=True,
    )
    db_session.add(resume)
    db_session.commit()
    # Pre-seeded chunk: the runner indexes an unindexed resume, and that extra commit would
    # otherwise be counted as a batch commit.
    from backend.database import ResumeChunk

    db_session.add(ResumeChunk(resume_id=resume.id, section="skills", text="Python"))
    db_session.commit()
    return resume


def _jobs(db_session, count):
    company = Company(name="BatchCorp", domain="batch.example")
    db_session.add(company)
    db_session.flush()
    jobs = [
        Job(
            company_id=company.id,
            title=f"Backend {i}",
            description=JD,
            url=f"batch-{i}",
            status="To Apply",
            match_scored=False,
        )
        for i in range(count)
    ]
    db_session.add_all(jobs)
    db_session.commit()
    for job in jobs:
        db_session.refresh(job)
    return jobs


class TestDeletedMidRun:
    def test_deleted_row_is_skipped_and_the_rest_of_the_run_commits(self, db_session):
        _resume(db_session)
        jobs = _jobs(db_session, 3)
        victim_id = jobs[0].id
        survivor_ids = [j.id for j in jobs if j.id != victim_id]
        state = {"deleted": False}

        def fake_score(db, resume_text, description, **kwargs):
            # Simulate another writer (job prune / LinkedIn auto-prune) removing a loaded row.
            if not state["deleted"]:
                state["deleted"] = True
                with db.no_autoflush:
                    db.execute(text("DELETE FROM jobs WHERE id = :id"), {"id": victim_id})
            return _scored()

        with patch("backend.main.RESCORE_COMMIT_BATCH", 2), patch(
            "backend.main._score_job_with_engine", side_effect=fake_score
        ), patch("backend.main.get_db", return_value=iter([db_session])):
            out = _run_match_rescore_task_runner(limit=10)

        assert out["deleted_skipped"] == 1
        assert out["rescored"] == 2
        assert out["failed"] == 0

        for job_id in survivor_ids:
            row = db_session.query(Job).filter(Job.id == job_id).first()
            assert row is not None
            assert row.match_scored is True
            assert row.match_score == 82.0

        # The run still records its outcome.
        op = (
            db_session.query(OperationLog)
            .filter(OperationLog.operation_type == "match_rescore")
            .order_by(OperationLog.id.desc())
            .first()
        )
        assert op is not None
        assert "1 removed during the run" in op.summary


class TestBatching:
    def test_commits_in_batches_not_once_at_the_end(self, db_session):
        _resume(db_session)
        _jobs(db_session, 5)
        commits = {"n": 0}
        real_commit = db_session.commit

        def counting_commit():
            commits["n"] += 1
            return real_commit()

        with patch("backend.main.RESCORE_COMMIT_BATCH", 2), patch(
            "backend.main._score_job_with_engine", return_value=_scored()
        ), patch.object(db_session, "commit", side_effect=counting_commit), patch(
            "backend.main.get_db", return_value=iter([db_session])
        ):
            out = _run_match_rescore_task_runner(limit=10)

        assert out["rescored"] == 5
        # ceil(5/2) = 3 batch commits + 1 OperationLog commit.
        assert commits["n"] == 4

    def test_a_failed_batch_does_not_drop_the_remaining_batches(self, db_session):
        _resume(db_session)
        _jobs(db_session, 4)
        real_commit = db_session.commit
        state = {"failed_once": False}

        def flaky_commit():
            if not state["failed_once"]:
                state["failed_once"] = True
                raise RuntimeError("database is locked")
            return real_commit()

        with patch("backend.main.RESCORE_COMMIT_BATCH", 2), patch(
            "backend.main._score_job_with_engine", return_value=_scored()
        ), patch.object(db_session, "commit", side_effect=flaky_commit), patch(
            "backend.main.get_db", return_value=iter([db_session])
        ):
            out = _run_match_rescore_task_runner(limit=10)

        # First batch is lost and reported; the second one still commits.
        assert out["failed"] == 2
        assert out["rescored"] == 2
        persisted = db_session.query(Job).filter(Job.match_scored == True).count()
        assert persisted == 2


class TestSqliteConcurrency:
    def test_init_db_enables_wal_and_a_busy_timeout(self, tmp_path):
        from sqlalchemy import text as sql_text

        import backend.database as dbm

        saved = (dbm.engine, dbm.SessionLocal)
        try:
            dbm.init_db(custom_url=f"sqlite:///{tmp_path / 'wal.db'}")
            with dbm.engine.connect() as conn:
                assert conn.execute(sql_text("PRAGMA journal_mode")).scalar().lower() == "wal"
                assert conn.execute(sql_text("PRAGMA busy_timeout")).scalar() == 30000
        finally:
            dbm.engine.dispose()
            dbm.engine, dbm.SessionLocal = saved
