#!/usr/bin/env python
"""Run the scoring judge over a set of jobs (issue #60).

Audits each job's deterministic assessment, re-scores what survives, and prints the diff.
Nothing is written unless ``--apply`` is passed.

    ./.venv/bin/python scripts/audit_scores.py                     # default sample
    ./.venv/bin/python scripts/audit_scores.py --jobs 585 1015
    ./.venv/bin/python scripts/audit_scores.py --jobs 820 --apply

Requires a reachable LLM (local LM Studio/Ollama/Unsloth, or cloud fallback enabled).
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Validation set from the issue: score extremes, median, partial-heavy, prose-heavy.
DEFAULT_JOBS = [585, 1015, 820, 983, 339]


def _load(job_ids: List[int]):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import backend.database as dbm
    from backend.config import decrypt_data

    url = os.environ.get("DATABASE_URL") or "sqlite:///./job_alert_agent.db"
    db = sessionmaker(bind=create_engine(url))()
    resume = (
        db.query(dbm.Resume)
        .filter(dbm.Resume.is_active == True)  # noqa: E712 - ORM filter
        .order_by(dbm.Resume.created_at.desc())
        .first()
    )
    if not resume:
        raise SystemExit("No active resume; upload one first.")
    resume_text = decrypt_data(resume.content_encrypted)
    parsed_resume = None
    if resume.parsed_json_encrypted:
        import json

        parsed_resume = json.loads(decrypt_data(resume.parsed_json_encrypted))

    jobs = db.query(dbm.Job).filter(dbm.Job.id.in_(job_ids)).all()
    return db, jobs, resume_text, parsed_resume


def main() -> int:
    ap = argparse.ArgumentParser(description="Audit deterministic match scores with the judge.")
    ap.add_argument("--jobs", nargs="*", type=int, default=DEFAULT_JOBS)
    ap.add_argument("--apply", action="store_true", help="write the corrected score back")
    ap.add_argument(
        "--allow-drops",
        action="store_true",
        help="let the judge delete requirements it calls prose (inflation risk)",
    )
    ap.add_argument("--provider", default=None, help="force a provider (e.g. openai, ollama)")
    ap.add_argument("--max-rounds", type=int, default=None)
    args = ap.parse_args()

    from backend.match_classifier import build_deterministic_assessment
    from backend.score_judge import validate_score

    db, jobs, resume_text, parsed_resume = _load(args.jobs)
    if not jobs:
        raise SystemExit(f"No jobs found for ids {args.jobs}")

    for job in jobs:
        assessment = build_deterministic_assessment(
            job.description or "",
            parsed_resume,
            resume_text=resume_text,
            job_title=job.title or "",
        )
        if not assessment.requirements:
            print(f"\n#{job.id} {job.title[:50]}: no requirements extracted — skipped")
            continue
        result = validate_score(
            assessment,
            parsed_resume,
            job.description or "",
            resume_text=resume_text,
            job_title=job.title or "",
            max_rounds=args.max_rounds,
            provider=args.provider,
            allow_drops=args.allow_drops,
        )
        audit = result.get("audit")
        print(f"\n#{job.id} {job.title[:60]}")
        print(
            f"  score {result['score_before']:.1f} -> {result['score_after']:.1f} "
            f"({result['before']['band']} -> {result['after']['band']})  "
            f"must {result['before']['must_coverage']:.2f} -> {result['after']['must_coverage']:.2f}  "
            f"rounds={result['rounds']}"
        )
        if audit is None:
            print("  judge unavailable (no LLM responded)")
            continue
        # Report the *effective* diff, not the raw verdicts: apply_audit ignores verdicts
        # that would delete a signal-derived requirement (years/seniority/education).
        corrected = result["assessment"]
        after_texts = {r.text for r in corrected.requirements}
        before_by_text = {r.text: r for r in assessment.requirements}
        dropped = [r.text for r in assessment.requirements if r.text not in after_texts]
        moved = [
            f"{r.text} -> {r.category}/{r.status}"
            for r in corrected.requirements
            if (b := before_by_text.get(r.text)) is not None
            and (b.category != r.category or b.status != r.status)
        ]
        if dropped:
            print("  dropped (applied): " + "; ".join(dropped))
        flagged = [v.text for v in audit.requirements if v.verdict == "not_a_requirement"]
        reported = [t for t in flagged if t in after_texts]
        if reported:
            print("  flagged as prose (kept, not applied): " + "; ".join(reported))
        if moved:
            print("  relabelled: " + "; ".join(moved))
        if audit.missing_requirements:
            print("  missing from assessment: " + "; ".join(audit.missing_requirements))
        ungrounded = [g.claim for g in audit.groundedness if not g.grounded]
        if ungrounded:
            print("  ungrounded claims: " + "; ".join(ungrounded))
        print(f"  opinion={audit.score_opinion} confidence={audit.confidence:.2f} — {audit.critique}")

        if args.apply and result["score_after"] != result["score_before"]:
            after = result["after"]
            job.match_score = after["score"]
            job.match_band = after["band"]
            job.apply_recommendation = after["recommendation"]
            job.score_method = "classifier_v1+judge"
            db.commit()
            print("  applied")

    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
