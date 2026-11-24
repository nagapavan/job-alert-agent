"""Employer requisition identifiers extracted from ATS URLs and application emails.

A requisition id names exactly one posting at one employer, which makes it the strongest
attribution key an application mail carries. The matcher used to work from company + title
alone, so a status mail for one requisition could be filed against a sibling role at the same
employer ("Principal Software Engineer (200045102)" landing on a different Principal SWE row).

False positives are harmless by construction: a bogus id only matches a job that happens to
store the same id, so a wrong extraction degrades to the next matching tier.
"""
from __future__ import annotations

import re
from typing import Tuple

# (source, pattern), strongest first. The captured group is the id.
_PATTERNS: Tuple[Tuple[str, "re.Pattern"], ...] = (
    # Greenhouse board link: boards.greenhouse.io/acme/jobs/8162316?gh_jid=8162316
    ("greenhouse", re.compile(r"\bgh_jid=(\d{5,12})\b", re.I)),
    # ADP career centre: ...recruitment.html?cid=...&jobId=589285
    ("adp", re.compile(r"\bjobId=(\d{4,12})\b", re.I)),
    # Workday junior/senior requisitions: JR100171, R262578, R-262578
    ("workday", re.compile(r"\b(JR\d{5,8})\b")),
    ("workday", re.compile(r"\b(R-?\d{5,8})\b")),
    # A bare requisition id in parentheses: "Principal Software Engineer (200045102)"
    ("generic", re.compile(r"\((\d{6,12})\)")),
)


def extract_requisition_id(*texts: str) -> Tuple[str, str]:
    """Returns ``(requisition_id, source)`` from any of the texts, or ``("", "")``.

    ``source`` names the ATS family the id shape belongs to (greenhouse / adp / workday /
    generic) so a later matcher can tell a Workday req from a Greenhouse job id.
    """
    haystack = " ".join(t for t in texts if t)
    if not haystack:
        return "", ""
    for source, pattern in _PATTERNS:
        match = pattern.search(haystack)
        if match:
            return match.group(1).upper(), source
    return "", ""
