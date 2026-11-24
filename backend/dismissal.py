"""One decision point for the zero-token negative-role filter.

Every discovery path needs the same three things from a dismissal check: the decision, the status
it implies, and the explanation shown to the operator. What differs per path is the surrounding
field resets (score, band, strengths, counters), so only the shared part lives here.
"""

from dataclasses import dataclass
from typing import Optional

from backend.ai_helper import check_semantic_dismissal
from backend.scoring_constants import DISMISSAL_SIMILARITY_THRESHOLD

AUTO_FILTERED_STATUS = "Not Interested"
DEFAULT_STATUS = "To Apply"


@dataclass(frozen=True)
class DismissalOutcome:
    """Result of the negative-role filter for one posting."""

    status: str
    analysis: Optional[str]
    match: Optional[dict]

    @property
    def dismissed(self) -> bool:
        return self.match is not None


def apply_dismissal_filter(
    db,
    title: str,
    description: Optional[str],
    company_name: Optional[str],
    threshold: float = DISMISSAL_SIMILARITY_THRESHOLD,
) -> DismissalOutcome:
    """Decide whether a posting matches a learned (company, role) dismissal rule."""
    match = check_semantic_dismissal(
        title,
        description or "",
        db,
        threshold=threshold,
        company_name=company_name,
    )
    if not match:
        return DismissalOutcome(DEFAULT_STATUS, None, None)
    analysis = (
        "Auto-filtered by semantic ignore rule "
        f"(matched: '{match['matched_title']}' with {int(match['similarity'] * 100)}% similarity)"
    )
    return DismissalOutcome(AUTO_FILTERED_STATUS, analysis, match)
