"""Tests for the one-off LinkedIn saved-job repair script."""
from backend.database import Company, Job, ApplicationEvent
from backend.scripts.repair_linkedin_saved_jobs import (
    repair_linkedin_saved_jobs,
    _parse_linkedin_slug,
    _titleize,
)


def test_parse_linkedin_slug_splits_on_last_at():
    assert _parse_linkedin_slug(
        "https://www.linkedin.com/jobs/view/solutions-architect-3-at-fedex-acc-4416423134"
    ) == ("solutions-architect-3", "fedex-acc")
    # "data-at-scale" must not fool the split: use the LAST "-at-".
    assert _parse_linkedin_slug(
        "https://www.linkedin.com/jobs/view/data-at-scale-engineer-at-acme-4416423999"
    ) == ("data-at-scale-engineer", "acme")


def test_titleize_keeps_acronyms():
    assert _titleize("staff-software-engineer-ai-platform") == "Staff Software Engineer AI Platform"
    assert _titleize("solutions-architect-3") == "Solutions Architect 3"


def test_repair_linkedin_saved_jobs_applies(db_session):
    comp = Company(name="Unknown Company", domain="unknown.com")
    db_session.add(comp)
    db_session.flush()

    job = Job(
        company_id=comp.id,
        title="Solutions Architect-3FedEx ACC · HyderabadReposted 1w ago",
        url="https://www.linkedin.com/jobs/view/solutions-architect-3-at-fedex-acc-4416423134",
        location="Remote / Various",
        source="LinkedIn Saved Jobs",
        source_type="LinkedIn",
        status="Shortlisted",
    )
    db_session.add(job)
    db_session.commit()

    summary = repair_linkedin_saved_jobs(db_session, apply=True)
    db_session.refresh(job)

    assert summary["repaired"] >= 1
    assert "Reposted" not in job.title
    assert " · " not in job.title
    assert job.title.lower().startswith("solutions architect")
    assert job.location == "Hyderabad"
    assert job.company.name.lower() == "fedex acc"
    events = db_session.query(ApplicationEvent).filter(ApplicationEvent.job_id == job.id).all()
    assert any(e.event_type == "maintenance_repair" for e in events)


def test_repair_linkedin_saved_jobs_dry_run_leaves_untouched(db_session):
    comp = Company(name="Unknown Company", domain="unknown.com")
    db_session.add(comp)
    db_session.flush()

    job = Job(
        company_id=comp.id,
        title="Lead Engineer – AI PlatformWaters · BengaluruReposted 3w ago",
        url="https://www.linkedin.com/jobs/view/lead-engineer-ai-platform-at-waters-4416423999",
        location="Remote / Various",
        source="LinkedIn Saved Jobs",
        source_type="LinkedIn",
        status="Shortlisted",
    )
    db_session.add(job)
    db_session.commit()

    summary = repair_linkedin_saved_jobs(db_session, apply=False)
    db_session.refresh(job)

    assert summary["repaired"] >= 1
    # Dry-run must not modify anything.
    assert "Reposted" in job.title
    assert job.location == "Remote / Various"
