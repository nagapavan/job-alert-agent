"""Tests for the self-learning label pipeline (backend/learning_signals.py).

Labels are derived from implicit behaviour. The critical invariant is that
**system-generated** states never supervise learning: a job auto-filtered at discovery
was never judged by the user, so treating it as a negative would teach the scorer that
unread discovery noise is a poor match.
"""
import datetime

import pytest

from backend import learning_signals as ls
from backend.database import ApplicationEvent, Company, Job


def _job(db, *, status, title="Engineer", analysis=None, applied_at=None):
    company = db.query(Company).filter(Company.name == "TestCo").first()
    if company is None:
        company = Company(name="TestCo")
        db.add(company)
        db.commit()
        db.refresh(company)
    job = Job(
        company_id=company.id,
        title=title,
        description="Requirements: Python and Kubernetes.",
        status=status,
        match_analysis=analysis,
        applied_at=applied_at,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def _event(db, job_id, description):
    db.add(ApplicationEvent(job_id=job_id, event_type="status_change", description=description))
    db.commit()


class TestLabelDerivation:
    def test_applied_is_positive(self, db_session):
        job = _job(db_session, status="Applied")
        d = ls.derive_decision(job, db_session)
        assert d.label == ls.POSITIVE
        assert d.is_trainable is True

    def test_applied_at_without_status_ts_is_positive(self, db_session):
        job = _job(db_session, status="To Apply", applied_at=datetime.datetime.now(datetime.timezone.utc))
        d = ls.derive_decision(job, db_session)
        assert d.label == ls.POSITIVE
        assert d.is_trainable is True

    @pytest.mark.parametrize("status", ["Interview", "Offered"])
    def test_employer_progress_is_strong_positive(self, db_session, status):
        job = _job(db_session, status=status)
        d = ls.derive_decision(job, db_session)
        assert d.label == ls.STRONG_POSITIVE
        assert d.is_trainable is True

    def test_rejection_after_applying_is_separate_label(self, db_session):
        job = _job(db_session, status="Rejected")
        d = ls.derive_decision(job, db_session)
        assert d.label == ls.NEGATIVE_OUTCOME
        assert d.is_trainable is True

    def test_user_dismissal_with_event_is_negative(self, db_session):
        job = _job(db_session, status="Not Interested")
        _event(db_session, job.id, "Status updated from 'To Apply' to 'Not Interested'")
        d = ls.derive_decision(job, db_session)
        assert d.label == ls.NEGATIVE
        assert d.provenance == ls.USER_INITIATED
        assert d.is_trainable is True

    def test_no_signal_returns_none(self, db_session):
        job = _job(db_session, status="Shortlisted")
        assert ls.derive_decision(job, db_session) is None


class TestSystemGeneratedExclusion:
    def test_auto_filtered_dismissal_is_not_trainable(self, db_session):
        """Discovery auto-filtering is NOT a user preference signal."""
        job = _job(
            db_session,
            status="Not Interested",
            analysis="Auto-filtered by semantic ignore rule (matched: 'X' with 91% similarity)",
        )
        d = ls.derive_decision(job, db_session)
        assert d.label == ls.NEGATIVE
        assert d.provenance == ls.SYSTEM_GENERATED
        assert d.is_trainable is False

    def test_dismissal_without_event_is_not_trainable(self, db_session):
        """Rows parked in a dismissed status at ingestion carry no decision."""
        job = _job(db_session, status="Not Interested")
        d = ls.derive_decision(job, db_session)
        assert d.provenance == ls.SYSTEM_GENERATED
        assert d.is_trainable is False

    def test_auto_filter_event_description_is_not_a_user_decision(self, db_session):
        job = _job(db_session, status="Not Interested")
        _event(
            db_session, job.id,
            "Auto-filtered by semantic ignore rule (matched: 'X' with 91% similarity)",
        )
        d = ls.derive_decision(job, db_session)
        assert d.provenance == ls.SYSTEM_GENERATED
        assert d.is_trainable is False


class TestAggregation:
    def test_label_counts_only_trainable(self, db_session):
        applied = _job(db_session, status="Applied")
        dismissed = _job(db_session, status="Not Interested")
        _event(db_session, dismissed.id, "Status updated from 'To Apply' to 'Not Interested'")
        auto = _job(db_session, status="Not Interested",
                    analysis="Auto-filtered by semantic ignore rule (matched: 'X')")

        decisions = ls.collect_decisions(db_session)
        counts = ls.label_counts(decisions)
        assert counts.get(ls.POSITIVE) == 1
        assert counts.get(ls.NEGATIVE) == 1  # only the user one
        assert len([d for d in decisions if not d.is_trainable]) == 1

    def test_readiness_reports_verdict_fields(self, db_session):
        _job(db_session, status="Applied")
        s = ls.learning_readiness(db_session)
        assert s["trainable"] == 1
        assert s["positives"] == 1
        assert "label_counts" in s
        assert "excluded_system_generated" in s


class TestTrainingFloor:
    def test_trainer_refuses_below_floor(self, db_session, monkeypatch, tmp_path):
        """A model fitted on a handful of outcomes must never be produced."""
        from backend import match_classifier as mc

        _job(db_session, status="Applied")
        monkeypatch.setattr(mc, "MIN_TRAINING_SAMPLES", 50)
        monkeypatch.setattr(mc, "MIN_PER_CLASS_SAMPLES", 15)
        out = tmp_path / "m.pkl"
        assert mc.train_classifier_from_db(db_session, output_path=out) is False
        assert not out.exists()

    def test_labels_are_not_derived_from_match_score(self, db_session):
        """Regression: the old trainer used `match_score >= 60` (its own output).

        A job with a high score but no user outcome must not become a training row.
        """
        job = _job(db_session, status="Shortlisted")
        job.match_score = 95.0
        job.match_scored = True
        db_session.commit()
        assert ls.derive_decision(job, db_session) is None
