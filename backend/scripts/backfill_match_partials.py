"""One-time backfill: populate match_partials_json on rows that were scored
before the partial-coverage panel landed (2026-10-17).

Uses ONLY the deterministic engine (`_deterministic_match`) on CACHED job
descriptions. No LLM calls, no Playwright browser, no LinkedIn or any other
network requests. Safe to re-run (idempotent — only touches rows where
match_partials_json is NULL/empty).

Usage:
    ./.venv/bin/python -m backend.scripts.backfill_match_partials            # 100-row slice (default)
    ./.venv/bin/python -m backend.scripts.backfill_match_partials --limit 0 # full run
    ./.venv/bin/python -m backend.scripts.backfill_match_partials --limit 100
    ./.venv/bin/python -m backend.scripts.backfill_match_partials --force --limit 0  # re-derive ALL rows (dedupe fix)
    ./.venv/bin/python -m backend.scripts.backfill_match_partials --dry-run --limit 100
"""
import json
import os
import sys
import time
from collections import Counter

# Prevent the discovery daemon from starting when this script is invoked.
os.environ.setdefault("JOB_ALERT_AGENT_NO_SCHEDULER", "1")

from sqlalchemy.orm import sessionmaker

from backend.database import init_db, Job, Resume, Company

MIN_DESC_CHARS = 30
BATCH_SLEEP_S = 0.0  # No sleep needed; deterministic scoring is CPU-bound, not I/O-bound


def run(apply: bool = True, limit: int = 0, force: bool = False) -> dict:
    engine = init_db()
    db_factory = sessionmaker(bind=engine)
    summary = {"scanned": 0, "updated": 0, "skipped_no_desc": 0, "skipped_no_det": 0, "errors": 0}

    from backend.config import decrypt_data
    from backend.main import _parsed_resume_of, _deterministic_match

    with db_factory() as db:
        active = (
            db.query(Resume)
            .filter(Resume.is_active == True)
            .order_by(Resume.created_at.desc())
            .first()
        )
        if not active:
            print("[error] no active resume")
            return summary
        resume_text = decrypt_data(active.content_encrypted)
        parsed_resume = _parsed_resume_of(active)

        query = db.query(Job).filter(Job.match_scored == True).filter(Job.description.isnot(None))
        if not force:
            query = query.filter(Job.match_partials_json.is_(None) | (Job.match_partials_json == ""))
        query = query.order_by(Job.id.asc())
        if limit > 0:
            query = query.limit(limit)
        jobs = query.all()

        if not apply:
            summary["scanned"] = len(jobs)
            for j in jobs:
                if not (j.description or "").strip() or len(j.description.strip()) < MIN_DESC_CHARS:
                    summary["skipped_no_desc"] += 1
            print(f"[DRY-RUN] scanned={len(jobs)} would_rescore={len(jobs) - summary['skipped_no_desc']}")
            return summary

        for idx, job in enumerate(jobs, start=1):
            summary["scanned"] += 1
            desc = (job.description or "").strip()
            if not desc or len(desc) < MIN_DESC_CHARS:
                summary["skipped_no_desc"] += 1
                continue
            try:
                det = _deterministic_match(
                    db, resume_text, desc, parsed_resume=parsed_resume, job_title=job.title or ""
                )
            except Exception as exc:
                summary["errors"] += 1
                print(f"  [error] job {job.id}: {exc}")
                continue

            if det is None:
                summary["skipped_no_det"] += 1
                continue

            strengths = list(det.get("strengths") or [])
            gaps = list(det.get("gaps") or [])
            partials = list(det.get("partials") or [])

            job.match_strengths_json = json.dumps(strengths)
            job.match_gaps_json = json.dumps(gaps)
            job.match_partials_json = json.dumps(partials)
            # Update the analysis narrative so it reflects the new breakdown.
            from backend.main import _deterministic_analysis_text
            job.match_analysis = _deterministic_analysis_text(det)
            # Update feedback (deterministic path)
            job.match_score = det["score"]
            job.match_scored = True
            job.score_method = det.get("method", "classifier_v1")
            job.match_band = det.get("band")
            job.apply_recommendation = det.get("recommendation")

            summary["updated"] += 1
            if idx % 25 == 0:
                print(f"  [{idx}/{len(jobs)}] updated={summary['updated']} errors={summary['errors']}", flush=True)
            if BATCH_SLEEP_S > 0:
                time.sleep(BATCH_SLEEP_S)

        db.commit()
    return summary


if __name__ == "__main__":
    apply = "--dry-run" not in sys.argv
    force = "--force" in sys.argv
    # Default to a bounded 100-row slice: each invocation commits at its end, so an
    # interrupted run only loses that slice's work. Pass --limit 0 for a full run.
    limit = 100
    if "--limit" in sys.argv:
        try:
            limit = int(sys.argv[sys.argv.index("--limit") + 1])
        except (IndexError, ValueError):
            print("[warn] --limit needs an integer; using default 100")
    summary = run(apply=apply, limit=limit, force=force)
    prefix = "DRY-RUN" if not apply else "APPLIED"
    print(f"[{prefix}] scanned={summary['scanned']} updated={summary['updated']} "
          f"skipped_no_desc={summary['skipped_no_desc']} skipped_no_det={summary['skipped_no_det']} "
          f"errors={summary['errors']}")
