"""One-time migration: re-derive stored match data for ALL scored jobs.

Repairs rows corrupted by since-fixed scorer bugs (phantom "Benefits Administration"
gaps, "N+ years <word>" badge gaps). Uses ONLY the deterministic engine on CACHED
descriptions: no LLM, no Playwright, no LinkedIn/Gmail/network calls.

- Rows with a usable description are rescored through the same path as live scoring
  (user extra-skills merged), including LLM-scored rows when the deterministic path
  returns a result. Job status is never touched.
- Rows whose description is below MIN_JOB_DESCRIPTION_CHARS are unscored
  (match_scored=0, match fields nulled); live sync re-processes them later.
- Apply mode first copies the SQLite DB and writes a per-row audit file (old values).

Usage:
    ./.venv/bin/python -m backend.scripts.rescore_all_matches --dry-run
    ./.venv/bin/python -m backend.scripts.rescore_all_matches            # apply (stop the server first)
"""
import datetime
import json
import os
import shutil
import sqlite3
import sys
from collections import Counter

os.environ.setdefault("JOB_ALERT_AGENT_NO_SCHEDULER", "1")

from sqlalchemy.orm import sessionmaker

from backend.ai_helper import MIN_JOB_DESCRIPTION_CHARS
from backend.database import init_db, Job, Resume

_OLD_FIELDS = (
    "match_score", "match_band", "apply_recommendation", "score_method", "match_scored",
    "match_scored_at", "match_analysis", "match_strengths_json", "match_gaps_json",
    "match_partials_json",
)
_PHANTOM = ("Benefits Administration",)


def _backup_db(engine, stamp: str) -> str:
    path = engine.url.database
    if not path or engine.url.get_backend_name() != "sqlite":
        raise SystemExit("[abort] automatic backup supports SQLite only; back up the database manually.")
    dest = f"{path}.bak-{stamp}"
    src = sqlite3.connect(path)
    try:
        dst = sqlite3.connect(dest)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return dest


def _phantom_count(rows) -> int:
    return sum(1 for j in rows if any(p in (j.match_gaps_json or "") for p in _PHANTOM))


def run(apply: bool) -> dict:
    engine = init_db()
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    summary = Counter()
    deltas = []
    band_flips = Counter()

    from backend.config import decrypt_data
    from backend.main import (
        _parsed_resume_of, _deterministic_match, _deterministic_analysis_text,
        _merge_extra_skills, get_extra_skills, get_user_preferences,
    )

    with sessionmaker(bind=engine)() as db:
        active = (
            db.query(Resume).filter(Resume.is_active == True)
            .order_by(Resume.created_at.desc()).first()
        )
        if not active:
            raise SystemExit("[abort] no active resume")
        resume_text = decrypt_data(active.content_encrypted)
        parsed_resume = _parsed_resume_of(active)
        try:
            extra = get_extra_skills(get_user_preferences(db))
        except Exception:
            extra = []
        if extra:
            parsed_resume = _merge_extra_skills(parsed_resume, extra)

        jobs = db.query(Job).filter(Job.match_scored == True).order_by(Job.id.asc()).all()
        summary["scanned"] = len(jobs)
        before_phantom = _phantom_count(jobs)

        audit = None
        if apply:
            backup = _backup_db(engine, stamp)
            audit_path = f"{engine.url.database}.rescore-audit-{stamp}.jsonl"
            audit = open(audit_path, "w", encoding="utf-8")
            print(f"[backup] {backup}\n[audit]  {audit_path}")

        for job in jobs:
            desc = (job.description or "").strip()
            old = {f: (str(getattr(job, f)) if f == "match_scored_at" else getattr(job, f)) for f in _OLD_FIELDS}
            if len(desc) < MIN_JOB_DESCRIPTION_CHARS:
                summary["unscored_short_desc"] += 1
                new = None
            else:
                try:
                    new = _deterministic_match(
                        db, resume_text, desc, parsed_resume=parsed_resume, job_title=job.title or ""
                    )
                except Exception as exc:
                    summary["errors"] += 1
                    print(f"  [error] job {job.id}: {exc}")
                    continue
                if not new:
                    summary["skipped_no_det"] += 1
                    continue
                if job.score_method == "requirement_coverage_v1":
                    summary["llm_rows_replaced"] += 1
                summary["rescored"] += 1
                if job.match_score is not None:
                    deltas.append(abs(new["score"] - job.match_score))
                if job.match_band != new.get("band"):
                    band_flips[f"{job.match_band} -> {new.get('band')}"] += 1

            if audit:
                audit.write(json.dumps({"id": job.id, "old": old, "action": "unscore" if new is None else "rescore"}) + "\n")
            if not apply:
                continue
            if new is None:
                job.match_scored = False
                job.match_score = None
                job.match_band = None
                job.apply_recommendation = None
                job.score_method = None
                job.match_scored_at = None
                job.match_analysis = None
                job.match_strengths_json = None
                job.match_gaps_json = None
                job.match_partials_json = None
            else:
                job.match_strengths_json = json.dumps(list(new.get("strengths") or []))
                job.match_gaps_json = json.dumps(list(new.get("gaps") or []))
                job.match_partials_json = json.dumps(list(new.get("partials") or []))
                job.match_analysis = _deterministic_analysis_text(new)
                job.match_score = new["score"]
                job.match_band = new.get("band")
                job.apply_recommendation = new.get("recommendation")
                job.score_method = new.get("method", "classifier_v1")
                job.match_scored = True
                job.match_scored_at = datetime.datetime.now(datetime.timezone.utc)

        if apply:
            audit.close()
            db.commit()

        after = db.query(Job).filter(Job.match_scored == True).all()
        summary["phantom_before"] = before_phantom
        summary["phantom_after"] = _phantom_count(after) if apply else None

    if deltas:
        deltas.sort()
        print(f"[drift] rows={len(deltas)} mean_abs={sum(deltas)/len(deltas):.1f} "
              f"median={deltas[len(deltas)//2]:.1f} max={deltas[-1]:.1f}")
    if band_flips:
        print("[band changes]", dict(band_flips.most_common(10)))
    return dict(summary)


if __name__ == "__main__":
    apply = "--dry-run" not in sys.argv
    result = run(apply=apply)
    print(f"[{'APPLIED' if apply else 'DRY-RUN'}] {result}")
