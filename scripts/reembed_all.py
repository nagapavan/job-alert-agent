#!/usr/bin/env python
"""Re-embed stored vectors into the current embedding space.

Vectors from different embedding backends are not comparable, so a model change (or a period
spent on the hash fallback) leaves rows stranded in a previous vector space. This script walks
every vector-bearing table, re-embeds the source text and stamps the row with the model that
produced it. Already-stamped rows are skipped, so the script is resumable.

    ./.venv/bin/python scripts/reembed_all.py --dry-run
    ./.venv/bin/python scripts/reembed_all.py
    ./.venv/bin/python scripts/reembed_all.py --tables jobs --batch-size 25
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import Dict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

VECTOR_TABLES = (
    "jobs",
    "resumes",
    "resume_chunks",
    "application_question_answers",
    "dismissed_job_patterns",
)


def _job_text(job) -> str:
    from backend.parser import clean_job_description

    description = job.description or ""
    return f"{job.title} {clean_job_description(description or job.title)}"


def _resume_text(resume) -> str:
    from backend.config import decrypt_data

    return decrypt_data(resume.content_encrypted) if resume.content_encrypted else ""


def _chunk_text(chunk) -> str:
    return chunk.text or ""


def _qa_text(item) -> str:
    return item.question_text or ""


def _dismissal_text(pattern) -> str:
    description = pattern.description[:400] if pattern.description else ""
    return f"{pattern.title} {description}"


def sources() -> Dict[str, tuple]:
    import backend.database as dbm

    return {
        "jobs": (dbm.Job, _job_text),
        "resumes": (dbm.Resume, _resume_text),
        "resume_chunks": (dbm.ResumeChunk, _chunk_text),
        "application_question_answers": (dbm.ApplicationQuestionAnswer, _qa_text),
        "dismissed_job_patterns": (dbm.DismissedJobPattern, _dismissal_text),
    }


def resolve_target(allow_hash: bool) -> tuple[str, str]:
    """(backend, model) that new vectors will belong to, proven by an actual embed."""
    from backend import embeddings

    embeddings.embed_text("reembed probe")
    stamp = embeddings.last_embed_stamp()
    if not stamp:
        raise SystemExit("Could not resolve the embedding backend (no stamp recorded).")
    backend, model = stamp
    if backend == "hash" and not allow_hash:
        raise SystemExit(
            "The embedding chain resolves to the hash fallback; refusing to overwrite vectors "
            "with lexical hashes. Provision a real backend first, or pass --allow-hash."
        )
    return backend, model


def reembed_table(
    db,
    model_cls,
    text_of,
    model: str,
    *,
    batch_size: int = 50,
    dry_run: bool = False,
    limit: int = 0,
    progress=None,
) -> dict:
    """Re-embed one table's rows that are not already stamped with ``model``."""
    from backend.embeddings import embed_text

    rows = (
        db.query(model_cls)
        .filter(model_cls.embedding.isnot(None))
        .order_by(model_cls.id)
        .all()
    )
    if limit:
        rows = rows[:limit]
    pending = [row for row in rows if row.embedding_model != model]
    stats = {
        "vectors": len(rows),
        "already": len(rows) - len(pending),
        "pending": len(pending),
        "reembedded": 0,
        "empty": 0,
    }
    if dry_run:
        return stats

    for row in pending:
        text = text_of(row)
        if not text.strip():
            stats["empty"] += 1
            continue
        vector = embed_text(text)
        row.embedding = vector
        row.embedding_model = model
        row.embedding_dim = len(vector)
        stats["reembedded"] += 1
        if stats["reembedded"] % max(1, batch_size) == 0:
            db.commit()
            if progress:
                progress(stats["reembedded"], len(pending))
    db.commit()
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description="Re-embed stored vectors into the current space.")
    ap.add_argument("--dry-run", action="store_true", help="report what would change, write nothing")
    ap.add_argument("--tables", nargs="*", choices=VECTOR_TABLES, default=list(VECTOR_TABLES))
    ap.add_argument("--batch-size", type=int, default=50, help="rows per commit (default 50)")
    ap.add_argument("--allow-hash", action="store_true", help="permit the non-semantic hash backend")
    ap.add_argument("--limit", type=int, default=0, help="max rows per table (0 = all)")
    args = ap.parse_args()

    from sqlalchemy.orm import sessionmaker

    from backend import database as dbm

    dbm.init_db()
    db = sessionmaker(bind=dbm.engine)()

    backend, model = resolve_target(args.allow_hash)
    print(f"target vector space: backend={backend} model={model}")

    table_sources = sources()
    total_reembedded = total_already = total_empty = 0
    for table in args.tables:
        model_cls, text_of = table_sources[table]
        stats = reembed_table(
            db,
            model_cls,
            text_of,
            model,
            batch_size=args.batch_size,
            dry_run=args.dry_run,
            limit=args.limit,
            progress=lambda done, total: print(f"  committed {done}/{total}"),
        )
        print(
            f"{table}: {stats['vectors']} vectors, {stats['already']} already stamped, "
            f"{stats['pending']} pending, re-embedded {stats['reembedded']}, "
            f"empty source {stats['empty']}"
        )
        total_reembedded += stats["reembedded"]
        total_already += stats["already"]
        total_empty += stats["empty"]

    db.close()
    print(
        f"\ntotal: re-embedded {total_reembedded}, already current {total_already}, "
        f"empty source {total_empty}" + (" (dry run)" if args.dry_run else "")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
