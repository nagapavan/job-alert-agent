"""Recall regression tests for the skill taxonomy vocabulary (issue #111).

Requirements are only created for taxonomy skills that appear in the JD, so a thin vocabulary
caps recall: the extractor then only ever sees requirements the resume already satisfies, and
`must_coverage` sits around 0.88 with almost nothing missing. These tests pin the vocabulary
coverage that makes `missing` possible.
"""

import json
from pathlib import Path

import pytest

TAXONOMY = Path(__file__).resolve().parent.parent / "data" / "skill_taxonomy.json"


def _skills():
    return json.loads(TAXONOMY.read_text())["skills"]


def test_canonical_names_are_unique():
    names = [entry["name"] for entry in _skills()]
    assert len(names) == len(set(names))


@pytest.mark.parametrize(
    "name",
    [
        "Scala",
        "OAuth",
        "Event-Driven Architecture",
        "Workflow Orchestration",
        "Multi-Agent Systems",
        "Helm",
        "Prometheus",
        "Snowflake",
    ],
)
def test_previously_missing_technologies_are_covered(name):
    """Terms JDs ask for that the vocabulary omitted, so they can now score as missing."""
    names = {entry["name"] for entry in _skills()}
    assert name in names


def test_aliases_are_not_duplicated_across_entries():
    seen = {}
    for entry in _skills():
        for surface in [entry["name"], *entry.get("aliases", [])]:
            key = surface.strip().lower()
            assert key not in seen or seen[key] == entry["name"], (
                f"{surface!r} maps to both {seen.get(key)!r} and {entry['name']!r}"
            )
            seen[key] = entry["name"]


def test_a_jd_naming_new_technologies_yields_them_as_requirements():
    """The end-to-end effect: coverage input stops being an echo of the resume."""
    from backend.match_classifier import build_deterministic_assessment

    jd = (
        "Qualifications\n"
        "Must have skills:\n"
        "Scala and OAuth experience.\n"
        "Event-Driven Architecture and Workflow Orchestration.\n"
        "Multi-Agent Systems, Helm, Prometheus, Snowflake.\n"
    )
    assessment = build_deterministic_assessment(
        jd, {"skills": ["Python"]}, resume_text="Python engineer."
    )
    texts = {r.text for r in assessment.requirements}

    for expected in (
        "Scala",
        "OAuth",
        "Event-Driven Architecture",
        "Workflow Orchestration",
        "Multi-Agent Systems",
        "Helm",
        "Prometheus",
        "Snowflake",
    ):
        assert expected in texts, f"{expected} was not extracted as a requirement"

    # And they are genuinely missing, which is the point of the recall fix.
    assert any(
        r.text == "Scala" and r.status == "missing" for r in assessment.requirements
    )
