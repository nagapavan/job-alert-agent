"""Tests for structured resume-gap/strength insights (match_strengths_json / match_gaps_json).

Covers:
- Job.match_strengths / Job.match_gaps properties (JSON parse, legacy-None, invalid JSON).
- JobOut exposing the parsed lists to the API.
- The resume_gap_insights feature flag defaulting to on.
"""
import json

from backend.database import (
    DEFAULT_FEATURE_FLAGS,
    Company,
    Job,
    get_feature_flags,
)
from conftest import test_client as client


def test_job_properties_parse_json(db_session):
    comp = Company(name="Acme")
    db_session.add(comp)
    db_session.flush()
    job = Job(
        company_id=comp.id,
        title="Backend Engineer",
        match_strengths_json=json.dumps(["Python", "8 years"]),
        match_gaps_json=json.dumps(["Kubernetes", "Public speaking"]),
    )
    db_session.add(job)
    db_session.commit()

    assert job.match_strengths == ["Python", "8 years"]
    assert job.match_gaps == ["Kubernetes", "Public speaking"]


def test_job_properties_none_for_legacy_rows(db_session):
    comp = Company(name="Legacy")
    db_session.add(comp)
    db_session.flush()
    job = Job(
        company_id=comp.id,
        title="Legacy Job",
        match_analysis="Strengths: python. Gaps: k8s",
        match_strengths_json=None,
        match_gaps_json=None,
    )
    db_session.add(job)
    db_session.commit()
    assert job.match_strengths is None
    assert job.match_gaps is None


def test_job_properties_invalid_json_returns_none(db_session):
    comp = Company(name="BadJson")
    db_session.add(comp)
    db_session.flush()
    job = Job(
        company_id=comp.id,
        title="Bad",
        match_strengths_json="{not valid json",
        match_gaps_json="a scalar string",
    )
    db_session.add(job)
    db_session.commit()
    assert job.match_strengths is None
    assert job.match_gaps is None


def test_job_out_exposes_lists(db_session):
    comp = Company(name="Exposed")
    db_session.add(comp)
    db_session.flush()
    db_session.add(
        Job(
            company_id=comp.id,
            title="Exposed Job",
            match_strengths_json=json.dumps(["Go"]),
            match_gaps_json=json.dumps(["React"]),
        )
    )
    db_session.commit()

    res = client.get("/api/jobs")
    assert res.status_code == 200
    row = next(j for j in res.json() if j["title"] == "Exposed Job")
    assert row["match_strengths"] == ["Go"]
    assert row["match_gaps"] == ["React"]


def test_resume_gap_insights_flag_defaults_on(db_session):
    assert DEFAULT_FEATURE_FLAGS["resume_gap_insights"] is True
    flags = get_feature_flags(db_session)
    assert flags.get("resume_gap_insights") is True


def test_job_partials_property_round_trip(db_session):
    """match_partials holds must-have requirements whose status is 'partial' (resume has
    some evidence but not full coverage). Persisted separately from gaps so the Skills
    Insights aggregation only counts truly-missing skills."""
    comp = Company(name="PartialsCo")
    db_session.add(comp)
    db_session.flush()
    job = Job(
        company_id=comp.id,
        title="Partial Role",
        match_partials_json=json.dumps(["Embeddings", "10+ years experience"]),
    )
    db_session.add(job)
    db_session.commit()
    assert job.match_partials == ["Embeddings", "10+ years experience"]


def test_job_partials_none_for_legacy_rows(db_session):
    comp = Company(name="Legacy2")
    db_session.add(comp)
    db_session.flush()
    job = Job(
        company_id=comp.id,
        title="Legacy2 Job",
        match_partials_json=None,
    )
    db_session.add(job)
    db_session.commit()
    assert job.match_partials is None


def test_job_out_exposes_partials(db_session):
    comp = Company(name="Exposed2")
    db_session.add(comp)
    db_session.flush()
    db_session.add(
        Job(
            company_id=comp.id,
            title="Exposed2 Job",
            match_strengths_json=json.dumps(["Go"]),
            match_gaps_json=json.dumps(["React"]),
            match_partials_json=json.dumps(["Kubernetes"]),
        )
    )
    db_session.commit()

    res = client.get("/api/jobs")
    assert res.status_code == 200
    row = next(j for j in res.json() if j["title"] == "Exposed2 Job")
    assert row["match_strengths"] == ["Go"]
    assert row["match_gaps"] == ["React"]
    assert row["match_partials"] == ["Kubernetes"]
