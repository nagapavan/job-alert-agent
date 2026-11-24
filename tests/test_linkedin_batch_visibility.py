"""Tests for LinkedIn sync batch visibility & review queue.

Covers:
- list_jobs filters: source_type (linkedin/direct/email), sync_batch, new_from_sync.
- update_job_status / bulk_update_job_status clearing the review marker (sync_batch_id).
- GET /api/linkedin/last-sync returning the latest batch summary + pending review count.
"""
import json

from backend.database import Company, Job, OperationLog
from conftest import test_client as client, TestingSessionLocal

BATCH_A = "li-20260101T000000"
BATCH_B = "li-20260102T000000"


def _make_job(db, title, source_type="LinkedIn", source="LinkedIn Saved Jobs",
              sync_batch_id=None, status="Shortlisted"):
    comp = Company(name=f"Comp-{title}")
    db.add(comp)
    db.flush()
    job = Job(
        company_id=comp.id,
        title=title,
        url=f"https://example.com/{title}",
        source=source,
        source_type=source_type,
        status=status,
        sync_batch_id=sync_batch_id,
    )
    db.add(job)
    db.flush()
    return job


def test_list_jobs_filter_by_source_type(db_session):
    _make_job(db_session, "LinkedIn Role A", source_type="LinkedIn", source="LinkedIn Saved Jobs")
    _make_job(db_session, "LinkedIn Role B", source_type="LinkedIn", source="LinkedIn Job Alert")
    _make_job(db_session, "ATS Role C", source_type="Direct", source="Greenhouse Portal")
    _make_job(db_session, "Email Role D", source_type="Email", source="Gmail Opportunity")
    db_session.commit()

    li = client.get("/api/jobs", params={"source_type": "linkedin"}).json()
    assert {j["title"] for j in li} == {"LinkedIn Role A", "LinkedIn Role B"}

    direct = client.get("/api/jobs", params={"source_type": "direct"}).json()
    assert {j["title"] for j in direct} == {"ATS Role C"}

    email = client.get("/api/jobs", params={"source_type": "email"}).json()
    assert {j["title"] for j in email} == {"Email Role D"}

    all_rows = client.get("/api/jobs", params={"source_type": "all"}).json()
    assert len(all_rows) == 4


def test_list_jobs_filter_by_sync_batch(db_session):
    _make_job(db_session, "Batch A 1", sync_batch_id=BATCH_A)
    _make_job(db_session, "Batch A 2", sync_batch_id=BATCH_A)
    _make_job(db_session, "Batch B 1", sync_batch_id=BATCH_B)
    _make_job(db_session, "No Batch", sync_batch_id=None)
    db_session.commit()

    batch_a = client.get("/api/jobs", params={"sync_batch": BATCH_A}).json()
    assert {j["title"] for j in batch_a} == {"Batch A 1", "Batch A 2"}

    batch_b = client.get("/api/jobs", params={"sync_batch": BATCH_B}).json()
    assert {j["title"] for j in batch_b} == {"Batch B 1"}


def test_list_jobs_new_from_sync(db_session):
    _make_job(db_session, "Reviewed", sync_batch_id=None)
    _make_job(db_session, "Unreviewed 1", sync_batch_id=BATCH_A)
    _make_job(db_session, "Unreviewed 2", sync_batch_id=BATCH_B)
    db_session.commit()

    new_only = client.get("/api/jobs", params={"new_from_sync": "true"}).json()
    assert {j["title"] for j in new_only} == {"Unreviewed 1", "Unreviewed 2"}


def test_update_job_status_clears_sync_batch_id(db_session):
    job = _make_job(db_session, "To Review", sync_batch_id=BATCH_A)
    db_session.commit()
    job_id = job.id

    res = client.patch(f"/api/jobs/{job_id}/status", json={"status": "Applied"})
    assert res.status_code == 200
    assert res.json()["sync_batch_id"] is None

    with TestingSessionLocal() as db2:
        j = db2.query(Job).filter(Job.id == job_id).first()
        assert j.sync_batch_id is None


def test_bulk_status_clears_sync_batch_id(db_session):
    j1 = _make_job(db_session, "Bulk 1", sync_batch_id=BATCH_A)
    j2 = _make_job(db_session, "Bulk 2", sync_batch_id=BATCH_B)
    db_session.commit()

    res = client.post(
        "/api/jobs/bulk-status",
        json={"job_ids": [j1.id, j2.id], "status": "Not Interested"},
    )
    assert res.status_code == 200

    with TestingSessionLocal() as db2:
        assert db2.query(Job).filter(Job.id == j1.id).first().sync_batch_id is None
        assert db2.query(Job).filter(Job.id == j2.id).first().sync_batch_id is None


def test_linkedin_last_sync_no_sync(db_session):
    res = client.get("/api/linkedin/last-sync")
    assert res.status_code == 200
    data = res.json()
    assert data["has_sync"] is False
    assert data["sync_batch_id"] is None


def test_linkedin_last_sync_returns_batch_and_pending(db_session):
    # Two jobs from the latest batch (pending review), one already reviewed.
    _make_job(db_session, "Pending 1", sync_batch_id=BATCH_A)
    _make_job(db_session, "Pending 2", sync_batch_id=BATCH_A)
    _make_job(db_session, "Reviewed", sync_batch_id=None)
    db_session.commit()

    details = {
        "sync_batch_id": BATCH_A,
        "total_added": 3,
        "saved_jobs_added": 2,
        "recommendations_added": 1,
        "already_existing": 5,
        "closed_jobs_pruned": 0,
    }
    db_session.add(OperationLog(
        operation_type="linkedin_sync",
        status="Completed",
        summary="LinkedIn sync complete",
        details_json=json.dumps(details),
        jobs_count=3,
    ))
    db_session.commit()

    res = client.get("/api/linkedin/last-sync")
    assert res.status_code == 200
    data = res.json()
    assert data["has_sync"] is True
    assert data["sync_batch_id"] == BATCH_A
    assert data["total_added"] == 3
    assert data["saved_jobs_added"] == 2
    assert data["recommendations_added"] == 1
    assert data["pending_review"] == 2


def test_linkedin_last_sync_latest_wins(db_session):
    old_details = {"sync_batch_id": BATCH_B, "total_added": 1, "saved_jobs_added": 1,
                   "recommendations_added": 0, "already_existing": 0, "closed_jobs_pruned": 0}
    new_details = {"sync_batch_id": BATCH_A, "total_added": 2, "saved_jobs_added": 2,
                   "recommendations_added": 0, "already_existing": 0, "closed_jobs_pruned": 0}
    db_session.add(OperationLog(
        operation_type="linkedin_sync", status="Completed", summary="older",
        details_json=json.dumps(old_details), jobs_count=1,
    ))
    db_session.add(OperationLog(
        operation_type="linkedin_sync", status="Completed", summary="newer",
        details_json=json.dumps(new_details), jobs_count=2,
    ))
    db_session.commit()

    data = client.get("/api/linkedin/last-sync").json()
    # The higher-id (newer) log should win.
    assert data["sync_batch_id"] == BATCH_A
    assert data["total_added"] == 2


def test_a_closed_saved_listing_only_touches_its_own_employer(db_session):
    """Title-only pruning closed another employer's row and wrote an event on it (finding 41)."""
    from backend.database import Company, Job
    from backend.routers.linkedin import _find_closed_saved_job

    alpha = Company(name="Alpha Corp")
    beta = Company(name="Beta Labs")
    db_session.add_all([alpha, beta])
    db_session.flush()
    alpha_job = Job(
        company_id=alpha.id,
        title="Backend Engineer",
        location="Remote",
        url="https://alpha.example/jobs/1",
        source_type="LinkedIn",
    )
    db_session.add(alpha_job)
    db_session.commit()

    other_employer = {
        "title": "Backend Engineer",
        "company": "Beta Labs",
        "url": "https://beta.example/jobs/9",
        "is_closed": True,
    }
    assert _find_closed_saved_job(db_session, other_employer) is None

    same_employer = dict(other_employer, company="Alpha Corp")
    assert _find_closed_saved_job(db_session, same_employer).id == alpha_job.id

    # With no employer on the card, only an exact URL may match.
    assert _find_closed_saved_job(db_session, {"title": "Backend Engineer"}) is None
    assert (
        _find_closed_saved_job(
            db_session, {"title": "Backend Engineer", "url": "https://alpha.example/jobs/1"}
        ).id
        == alpha_job.id
    )
