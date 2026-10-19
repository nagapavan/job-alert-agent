#!/usr/bin/env python
"""Drain noise from the skill-taxonomy candidate review queue.

The queue (``data/skill_taxonomy_candidates.json``) accumulated ~8.5k entries of raw HTML,
section headings and JD prose before ``record_skill_candidate`` gained a capture filter.
This re-applies that filter to the existing file so the historical pollution is removed
while genuine candidates (and their recurrence counts) are preserved.

Idempotent: safe to re-run; a second run removes nothing.

Usage:
    ./.venv/bin/python scripts/purge_taxonomy_candidates.py [--dry-run]

See reasoning/2026-10-19-taxonomy-candidate-queue-pollution.md (issue #8).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import skill_taxonomy as tax  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="report what would be removed without writing"
    )
    args = parser.parse_args()

    path = tax.CANDIDATES_PATH
    if not path.exists():
        print(f"No candidate queue at {path} - nothing to do.")
        return 0

    data = json.loads(path.read_text("utf-8")) or {}
    counts = data.get("counts") if isinstance(data.get("counts"), dict) else {}
    if not counts:
        print("Candidate queue is empty - nothing to do.")
        return 0

    noisy = {k: v for k, v in counts.items() if not tax.is_plausible_skill_phrase(k)}

    print(f"Queue:   {path}")
    print(f"Entries: {len(counts)}")
    print(f"Noise:   {len(noisy)}")
    print(f"Keep:    {len(counts) - len(noisy)}")
    if noisy:
        print("\nSample of noise being dropped:")
        for phrase, count in sorted(noisy.items(), key=lambda kv: -kv[1])[:10]:
            print(f"  x{count:<5} {phrase[:70]!r}")

    if args.dry_run:
        print("\n--dry-run: no changes written.")
        return 0

    removed, kept = tax.purge_candidates()
    print(f"\nPurged {removed} noise entries; {kept} remain.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
