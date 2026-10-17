"""Derive ground-truth labels from implicit user behaviour (no LLM, no prompts).

Labels are *derived*, never asked for: applying and dismissing are byproducts of using the
app, whereas explicit thumbs-up/down is a near-dead feature in practice.

The distinction that matters most is **who decided**:

- **user-initiated** outcomes (the user applied, or deliberately marked a job Not
  Interested) are genuine preference signal and become training labels;
- **system-generated** states (a job auto-filtered by the semantic ignore rule at
  discovery, or rows pre-set at ingestion with no decision event) are *not* preference
  signal. Labelling them would teach the scorer that unread discovery noise is a poor
  match, which is exactly the bias that makes such models useless.

Features are intentionally NOT read from the job row (``match_strengths_json`` etc. are
NULL for progressed rows). Callers re-derive features via the same code path the live
scorer uses, so train/serve features cannot drift.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional

logger = logging.getLogger("learning_signals")

# Label vocabulary.
POSITIVE = "positive"                  # user applied — real intent
STRONG_POSITIVE = "strong_positive"    # employer progressed them (interview/offer)
NEGATIVE = "negative"                  # user deliberately rejected the role
NEGATIVE_OUTCOME = "negative_outcome"  # employer rejected after the user applied

# Provenance.
USER_INITIATED = "user_initiated"
SYSTEM_GENERATED = "system_generated"

# Statuses that mean "the user decided to act on this".
APPLIED_STATUSES = ("Applied", "Screening", "Interview", "Offered", "Rejected")
# Statuses where the employer responded.
PROGRESSED_STATUSES = ("Interview", "Offered")
DISMISSED_STATUSES = ("Not Interested", "Ignored")

# Marker written by the discovery path when a job is auto-filtered by the semantic ignore
# rule (see main.py). Such rows were never shown to, or judged by, the user.
_AUTO_FILTER_MARKER = "auto-filtered by semantic ignore rule"

# Schema constants used by :func:`record_decision`.
DECISION_EVENT_TYPE = "status_change"


@dataclass(frozen=True)
class Decision:
    """One job's derived label plus the provenance that justifies it."""

    job_id: int
    label: str
    provenance: str
    reason: str

    @property
    def is_trainable(self) -> bool:
        """Only user-initiated decisions may supervise learning."""
        return self.provenance == USER_INITIATED


def _is_auto_filtered(job) -> bool:
    """True when the row was set to a dismissed status by the ignore filter."""
    analysis = (getattr(job, "match_analysis", None) or "").lower()
    return _AUTO_FILTER_MARKER in analysis


def _has_user_decision_event(db, job_id: int) -> bool:
    """True when a user-initiated status_change event exists for this job."""
    try:
        from backend.database import ApplicationEvent

        events = (
            db.query(ApplicationEvent)
            .filter(
                ApplicationEvent.job_id == job_id,
                ApplicationEvent.event_type == DECISION_EVENT_TYPE,
            )
            .all()
        )
    except Exception as exc:  # noqa: BLE001 - label derivation must not explode
        logger.debug("Could not read decision events for job %s: %s", job_id, exc)
        return False
    for ev in events:
        desc = (ev.description or "").lower()
        if _AUTO_FILTER_MARKER not in desc:
            return True
    return False


def derive_decision(job, db) -> Optional[Decision]:
    """Derive a labelled decision for one job, or None when there is nothing to learn from.

    Applying is always user-initiated (the app never applies on the user's behalf), so it
    is trusted without an event lookup. Dismissals require a genuine user decision event,
    because discovery auto-filtering also parks rows in a dismissed status.
    """
    status = (getattr(job, "status", None) or "").strip()
    job_id = getattr(job, "id", None)

    # --- Employer responded: the strongest signal we have. ---
    if status in PROGRESSED_STATUSES:
        return Decision(
            job_id=job_id,
            label=STRONG_POSITIVE,
            provenance=USER_INITIATED,
            reason=f"Employer progressed to '{status}'",
        )

    # --- The user applied. ---
    if status in APPLIED_STATUSES or getattr(job, "applied_at", None) is not None:
        # A rejection after applying is still a positive *intent* example, but we keep the
        # outcome separate so calibration can use the distinction.
        if status == "Rejected":
            return Decision(
                job_id=job_id,
                label=NEGATIVE_OUTCOME,
                provenance=USER_INITIATED,
                reason="Employer rejected after the user applied",
            )
        return Decision(
            job_id=job_id,
            label=POSITIVE,
            provenance=USER_INITIATED,
            reason=f"User applied (status '{status}')",
        )

    # --- The user dismissed it. Only genuine user decisions count. ---
    if status in DISMISSED_STATUSES:
        if _is_auto_filtered(job):
            return Decision(
                job_id=job_id,
                label=NEGATIVE,
                provenance=SYSTEM_GENERATED,
                reason="Auto-filtered by the semantic ignore rule at discovery",
            )
        if not _has_user_decision_event(db, job_id):
            return Decision(
                job_id=job_id,
                label=NEGATIVE,
                provenance=SYSTEM_GENERATED,
                reason="Dismissed status with no user decision event (pre-set at ingestion)",
            )
        return Decision(
            job_id=job_id,
            label=NEGATIVE,
            provenance=USER_INITIATED,
            reason="User deliberately marked Not Interested",
        )

    return None


def collect_decisions(db, limit: Optional[int] = None) -> List[Decision]:
    """Derive decisions for every job that carries an outcome signal."""
    from backend.database import Job

    query = db.query(Job)
    if limit:
        query = query.limit(limit)
    decisions: List[Decision] = []
    for job in query.all():
        d = derive_decision(job, db)
        if d is not None:
            decisions.append(d)
    return decisions


def label_counts(decisions: List[Decision]) -> Dict[str, int]:
    """Counts by label, trainable (user-initiated) only."""
    counts: Dict[str, int] = {}
    for d in decisions:
        if d.is_trainable:
            counts[d.label] = counts.get(d.label, 0) + 1
    return counts


def learning_readiness(db) -> Dict[str, object]:
    """Summarise whether there is enough signal to train yet.

    Returns trainable label counts, how many rows were excluded as system-generated, and
    a plain verdict — so "not enough data yet" is a visible outcome rather than a silent
    bad model.
    """
    decisions = collect_decisions(db)
    trainable = [d for d in decisions if d.is_trainable]
    excluded = [d for d in decisions if not d.is_trainable]
    return {
        "total_jobs_with_signal": len(decisions),
        "trainable": len(trainable),
        "excluded_system_generated": len(excluded),
        "label_counts": label_counts(trainable),
        "positives": sum(
            1 for d in trainable if d.label in (POSITIVE, STRONG_POSITIVE)
        ),
        "negatives": sum(
            1 for d in trainable if d.label in (NEGATIVE, NEGATIVE_OUTCOME)
        ),
    }
