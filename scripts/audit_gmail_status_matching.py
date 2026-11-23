"""Audit older Gmail application emails against the current matcher (read-only by default).

`run_gmail_sync` skips any message already recorded in `processed_emails`, so an email that was
mis-matched once can never be re-evaluated by a normal sync. This re-fetches messages in a window
(default: older than 14 days, within the same 90-day scope the sync uses) and replays them through
the *current* `_match_job_for_email_event`, reporting:

  MISSED   the event would move an application the DB does not reflect
  OK       the DB already reflects the event
  AMBIG    several applications fit equally (correctly staged, nothing to fix)
  UNMATCH  no live application fits (would be staged / auto-created)
  NOTFOUND no tracked job for the parsed employer at all

Usage:
    ./.venv/bin/python scripts/audit_gmail_status_matching.py                  # audit only
    ./.venv/bin/python scripts/audit_gmail_status_matching.py --older-than 14
    ./.venv/bin/python scripts/audit_gmail_status_matching.py --apply          # write the fixes
"""

import argparse
import os
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# The app module is imported for the attribution helpers it re-exports; importing it no longer
# touches the database (that happens on server startup), so open it explicitly. The scheduler
# must stay off so a data-repair run cannot fire a live browser scan while it mutates rows.
os.environ.setdefault("JOB_ALERT_AGENT_NO_SCHEDULER", "1")
import backend.main as main  # noqa: F401 - attribution helpers
from backend.config import get_gmail_settings
from backend.database import ApplicationEvent, Job, SessionLocal, init_db

init_db()
from backend.gmail_client import (
    STATUS_EVENT_TYPES,
    build_service,
    fetch_message_metadata,
    list_message_ids,
)
from backend.playwright_app import classify_email
from backend.scraper import extract_company_and_role_from_email_header

# What each event type should make the job status become.
TARGET_STATUS = {
    "rejection": "Rejected",
    "offer": "Offered",
    "interview": "Interview",
    "screening": "Screening",
    "application_received": "Applied",
}


def main_cli() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--older-than", type=int, default=14, help="days; default 14")
    parser.add_argument("--within", type=int, default=90, help="upper bound in days; default 90")
    parser.add_argument("--apply", action="store_true", help="write the MISSED corrections")
    args = parser.parse_args()

    settings = get_gmail_settings()
    if not (settings["client_id"] and settings["client_secret"] and settings["refresh_token"]):
        print("Gmail is not connected; nothing to audit.")
        return 1
    service = build_service(
        settings["client_id"], settings["client_secret"], settings["refresh_token"]
    )
    query = f"older_than:{args.older_than}d newer_than:{args.within}d"
    message_ids = list_message_ids(service, query=query, max_results=100, max_pages=3)
    print(f"Scanning {len(message_ids)} message(s) matching: {query}\n")

    db = SessionLocal()
    rows = []
    try:
        for mid in message_ids:
            meta = fetch_message_metadata(service, mid)
            cls = classify_email(meta["subject"], meta["snippet"], meta["sender"])
            if cls["category"] not in STATUS_EVENT_TYPES:
                continue
            extracted = extract_company_and_role_from_email_header(
                meta["subject"], meta["snippet"], meta["sender"]
            )
            company = (extracted.get("company") or "").strip()
            role = (extracted.get("role") or "").strip()
            if not company:
                continue
            job, info = main._match_job_for_email_event(
                db, company, role, extracted.get("domain") or ""
            )
            target = TARGET_STATUS.get(cls["category"])
            if job is None:
                verdict = "AMBIG" if info["ambiguous"] else "UNMATCH"
            elif job.status == target:
                verdict = "OK"
            else:
                verdict = "MISSED"
            rows.append(
                {
                    "verdict": verdict,
                    "event": cls["category"],
                    "company": company,
                    "role": role,
                    "job": job,
                    "matched_by": info["matched_by"],
                    "subject": meta["subject"][:70],
                    "candidates": [c["job_id"] for c in info.get("candidates") or []],
                }
            )
    finally:
        pass

    order = {"MISSED": 0, "AMBIG": 1, "UNMATCH": 2, "OK": 3}
    rows.sort(key=lambda r: (order[r["verdict"]], r["event"]))
    for r in rows:
        job = r["job"]
        where = (
            f"job {job.id} {job.title!r} [{job.status}]" if job else f"candidates={r['candidates']}"
        )
        print(
            f"  {r['verdict']:<7} {r['event']:<20} {r['company'][:24]:<24} "
            f"{r['role'][:32]:<32} -> {where}  ({r['matched_by']})"
        )
        print(f"          subject: {r['subject']}")

    missed = [r for r in rows if r["verdict"] == "MISSED"]
    print(f"\n{len(rows)} status email(s): {len(missed)} missed, "
          f"{sum(1 for r in rows if r['verdict'] == 'AMBIG')} ambiguous, "
          f"{sum(1 for r in rows if r['verdict'] == 'UNMATCH')} unmatched, "
          f"{sum(1 for r in rows if r['verdict'] == 'OK')} already reflected.")

    if not args.apply:
        print("\nAudit only. Re-run with --apply to write the MISSED corrections.")
        db.close()
        return 0

    applied = 0
    for r in missed:
        job, target = r["job"], TARGET_STATUS[r["event"]]
        job.status = target
        if target == "Rejected":
            job.rejected_at = job.rejected_at or dt.datetime.now(dt.timezone.utc)
        db.add(
            ApplicationEvent(
                job_id=job.id,
                event_type=f"email_{r['event']}",
                description=f"Backfilled from Gmail audit: {r['subject']}",
            )
        )
        applied += 1
        print(f"  applied: job {job.id} {job.title!r} -> {target}")
    db.commit()
    print(f"\nApplied {applied} correction(s).")
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main_cli())
