"""Consent is checked at run time and the blocklist covers every ingestion channel.

Findings 42 and 43. Both were true only in the ATS path: a flag-only gate let a consent-version
bump keep scraping, and Google/LinkedIn/Gmail created an excluded employer afresh.
"""

from unittest.mock import patch

from fastapi.testclient import TestClient

import json

from backend.database import (
    DEFAULT_FEATURE_FLAGS,
    Company,
    Job,
    find_or_create_company,
    get_user_preferences,
    record_consent,
)
from backend.main import app

client = TestClient(app)


def _enable_flag(db, flag):
    pref = get_user_preferences(db)
    flags = dict(DEFAULT_FEATURE_FLAGS)
    flags.update(json.loads(pref.features_json or "{}"))
    flags[flag] = True
    pref.features_json = json.dumps(flags)
    db.commit()


def _exclude(db, names):
    pref = get_user_preferences(db)
    pref.excluded_companies = ", ".join(names)
    db.commit()


# ---------------------------------------------------------------- finding 43

def test_find_or_create_company_refuses_a_blocklisted_employer(db_session):
    _exclude(db_session, ["Acme"])

    company, created = find_or_create_company(db_session, "Acme Corp", "acme.example")

    assert company is None
    assert created is False
    assert db_session.query(Company).filter(Company.name == "Acme Corp").first() is None


def test_find_or_create_company_still_creates_an_allowed_employer(db_session):
    _exclude(db_session, ["Acme"])

    company, created = find_or_create_company(db_session, "Globex", "globex.example")

    assert created is True
    assert company.name == "Globex"


def test_google_endpoint_skips_a_blocklisted_employer(db_session):
    _exclude(db_session, ["Acme"])
    _enable_flag(db_session, "google_jobs")

    with patch(
        "backend.routers.google_jobs.fetch_google_jobs_playwright",
        return_value=[
            {
                "title": "Backend Engineer",
                "company": "Acme",
                "location": "Remote",
                "description": "Build things.",
                "url": "https://acme.example/jobs/1",
            }
        ],
    ):
        res = client.post("/api/jobs/google-jobs/scrape", json={"query": "backend jobs"})

    assert res.status_code == 200
    assert res.json()["total_added"] == 0
    assert db_session.query(Company).filter(Company.name == "Acme").first() is None


# ---------------------------------------------------------------- finding 42

def _seed_applied_smartrecruiters_job(db_session):
    company = Company(name="Globex")
    db_session.add(company)
    db_session.flush()
    db_session.add(
        Job(
            company_id=company.id,
            title="Backend Engineer",
            url="https://jobs.smartrecruiters.com/globex/123",
            status="Applied",
            source_type="Direct",
        )
    )
    db_session.commit()


def test_consent_required_portal_is_skipped_when_consent_is_missing(db_session):
    """The flag alone is not permission after a CONSENT_VERSION bump (finding 42)."""
    _enable_flag(db_session, "ats_smartrecruiters")
    _seed_applied_smartrecruiters_job(db_session)

    with patch("backend.main.probe_smartrecruiters_job_active") as probe:
        res = client.post(
            "/api/applications/sync-external",
            json={"sources": ["smartrecruiters"]},
        )

    assert res.status_code == 200
    probe.assert_not_called()
    assert "consent" in " ".join(res.json()["details"]).lower()


def test_consent_required_portal_runs_once_consent_is_recorded(db_session):
    _enable_flag(db_session, "ats_smartrecruiters")
    _seed_applied_smartrecruiters_job(db_session)
    record_consent(db_session, "ats_smartrecruiters")

    with patch(
        "backend.main.probe_smartrecruiters_job_active", return_value=True
    ) as probe:
        res = client.post(
            "/api/applications/sync-external",
            json={"sources": ["smartrecruiters"]},
        )

    assert res.status_code == 200
    probe.assert_called_once()


# ---------------------------------------------------------------- finding 57

def test_ingestion_canonicalises_an_alias_to_the_stored_employer(db_session):
    """'Uber Technologies' must reuse the stored 'Uber' row, not create a second employer."""
    from backend.routers.linkedin import _linkedin_upsert_company

    stored = Company(name="Uber")
    db_session.add(stored)
    db_session.commit()

    resolved = _linkedin_upsert_company(db_session, "Uber Technologies Inc")

    assert resolved.id == stored.id
    assert (
        db_session.query(Company)
        .filter(Company.name == "Uber Technologies Inc")
        .first()
        is None
    )
