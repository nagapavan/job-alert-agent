#!/usr/bin/env python
"""Print the deterministic score distribution over the stored JDs (issue #98).

Nothing is written and no network call is made: every posting is re-scored from its cached
description with the active resume, using the same deterministic path as bulk scoring.

    ./.venv/bin/python scripts/score_distribution.py            # current code
    ./.venv/bin/python scripts/score_distribution.py --compare  # before/after the #98 guards

`--compare` disables the header-chrome guard, the inline preference cue, the seniority-role
word guard and the generic-phrase guard in-process, so the shift caused by those four rules is
visible without needing the pre-change tree.
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("JOB_ALERT_AGENT_NO_SCHEDULER", "1")


def _load_rows():
    from backend.database import Job, SessionLocal, init_db

    init_db()

    db = SessionLocal()
    try:
        rows = (
            db.query(Job)
            .filter(Job.description != None)  # noqa: E711
            .filter(Job.description != "")
            .all()
        )
        return [(j.id, j.title or "", j.description or "") for j in rows]
    finally:
        db.close()


def _score_all(rows):
    from backend.database import Resume, SessionLocal
    from backend.match_classifier import build_deterministic_assessment
    from backend.match_scoring import band_and_recommendation, ScoringConfig

    db = SessionLocal()
    try:
        resume = (
            db.query(Resume)
            .filter(Resume.is_active == True)  # noqa: E712
            .order_by(Resume.created_at.desc())
            .first()
        )
        resume_text = ""
        parsed = {}
        if resume:
            from backend.config import decrypt_data

            resume_text = decrypt_data(resume.content_encrypted) or ""
            if resume.parsed_json_encrypted:
                try:
                    parsed = json.loads(decrypt_data(resume.parsed_json_encrypted) or "{}")
                except (TypeError, ValueError):
                    parsed = {}
    finally:
        db.close()

    config = ScoringConfig()
    scores, bands, recommendations = [], collections.Counter(), collections.Counter()
    must = []
    for _job_id, title, description in rows:
        assessment = build_deterministic_assessment(
            description, parsed, resume_text=resume_text, job_title=title
        )
        if not assessment.requirements:
            continue
        known = [r for r in assessment.requirements if r.status in ("met", "partial", "missing")]
        if not known:
            continue
        must_items = [r for r in known if r.category == "must_have"]
        if must_items:
            covered = sum(1 for r in must_items if r.status in ("met", "partial"))
            must.append(covered / len(must_items))
        score = _predict(assessment, parsed, title, description)
        band, rec = band_and_recommendation(score, config)
        scores.append(score)
        bands[band] += 1
        recommendations[rec] += 1
    return scores, bands, recommendations, must


def _predict(assessment, parsed, title, description):
    from backend.match_classifier import (
        CLASSIFIER, build_jd_hints, extract_match_features,
    )
    from backend.match_scoring import ScoringConfig

    hints = build_jd_hints(description, title)
    features = extract_match_features(parsed, hints, assessment=assessment)
    score = CLASSIFIER.predict(features)
    config = ScoringConfig()
    if features.must_count > 0 and features.must_coverage < config.must_floor:
        score = min(score, config.cap_when_below_floor)
    return score


def _summary(label, scores, bands, recommendations, must):
    if not scores:
        print(f"{label}: no scorable postings")
        return
    ordered = sorted(scores)
    def pct(p):
        return ordered[min(len(ordered) - 1, int(len(ordered) * p))]
    print(f"\n{label}: {len(scores)} postings")
    print(f"  mean {sum(scores)/len(scores):.1f} | p10 {pct(0.10):.1f} | "
          f"median {pct(0.50):.1f} | p90 {pct(0.90):.1f}")
    print("  bands: " + ", ".join(f"{b} {n}" for b, n in bands.most_common()))
    print("  recommendations: " + ", ".join(f"{r} {n}" for r, n in recommendations.most_common()))
    if must:
        ms = sorted(must)
        print(f"  must-coverage median {ms[len(ms)//2]:.2f}")


def _disable_issue_98_guards():
    from backend import jd_signals, requirements_extractor, section_intent

    section_intent.leading_header_chrome_end = lambda *a, **k: 0
    section_intent.ends_with_preference_cue = lambda *a, **k: False
    jd_signals.SENIORITY_ROLE_WORDS = set()
    requirements_extractor.is_generic_phrase = lambda *a, **k: False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compare", action="store_true",
                        help="also print the distribution with the #98 guards disabled")
    parser.add_argument("--limit", type=int, default=200,
                        help="even-stride sample size (each posting costs ~1s of embedding)")
    args = parser.parse_args()

    rows = _load_rows()
    if args.limit and len(rows) > args.limit:
        stride = len(rows) / args.limit
        rows = [rows[int(i * stride)] for i in range(args.limit)]
    _summary("current", *_score_all(rows))
    if args.compare:
        _disable_issue_98_guards()
        _summary("without #98 guards", *_score_all(rows))


if __name__ == "__main__":
    main()
