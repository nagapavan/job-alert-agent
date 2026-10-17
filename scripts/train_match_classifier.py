#!/usr/bin/env python3
"""
Train the match classifier from implicit user outcomes.

Usage:
    python scripts/train_match_classifier.py

Labels are derived from real behaviour (the user applied, or deliberately dismissed a
role) via backend/learning_signals.py — never from the model's own previous output.
System-generated states (discovery auto-filtering, rows pre-set at ingestion) are
excluded because they carry no preference signal.

Training is REFUSED below a minimum sample floor so a model fitted on a handful of
outcomes cannot silently degrade scoring. Run with --status to see whether there is
enough signal yet.
"""

import sys
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backend import database
from backend.learning_signals import learning_readiness
from backend.match_classifier import (
    MIN_PER_CLASS_SAMPLES,
    MIN_TRAINING_SAMPLES,
    train_classifier_from_db,
)


def _print_status(db) -> None:
    status = learning_readiness(db)
    print("Learning signal status")
    print("----------------------")
    print(f"  jobs with an outcome signal : {status['total_jobs_with_signal']}")
    print(f"  trainable (user-initiated)  : {status['trainable']}")
    print(f"  excluded (system-generated) : {status['excluded_system_generated']}")
    print(f"  positives                   : {status['positives']}")
    print(f"  negatives                   : {status['negatives']}")
    print(f"  label counts                : {status['label_counts']}")
    print()
    print(f"  training floor              : {MIN_TRAINING_SAMPLES} total, "
          f"{MIN_PER_CLASS_SAMPLES} per class")
    ready = (
        status["trainable"] >= MIN_TRAINING_SAMPLES
        and status["positives"] >= MIN_PER_CLASS_SAMPLES
        and status["negatives"] >= MIN_PER_CLASS_SAMPLES
    )
    print(f"  verdict                     : {'READY to train' if ready else 'NOT ENOUGH DATA yet'}")
    if not ready:
        print()
        print("  More labelled decisions are needed. Keep applying / dismissing jobs —")
        print("  the pipeline derives labels from that automatically, no extra input required.")


def main():
    show_status_only = "--status" in sys.argv

    database.init_db()
    db = database.SessionLocal()

    try:
        if show_status_only:
            _print_status(db)
            return

        print("🚀 Training match classifier from implicit user outcomes...")
        print()
        _print_status(db)
        print()

        success = train_classifier_from_db(db)
        if success:
            print("✅ Classifier training complete!")
            print("   Restart the backend for the new model to take effect.")
        else:
            print("⚠️  Classifier training skipped (see the reason above).")
            print("   The deterministic heuristic fallback remains in use.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
