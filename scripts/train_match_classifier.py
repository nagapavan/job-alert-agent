#!/usr/bin/env python3
"""
Train the match classifier from historical job assessments.

Usage:
    python scripts/train_match_classifier.py

This script:
1. Connects to the job database
2. Extracts features from jobs with match_scored=True
3. Trains a logistic regression classifier
4. Saves the model to data/match_classifier.pkl

Run this whenever you have accumulated enough historical assessments
(recommended: >100 scored jobs).
"""

import sys
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backend import database
from backend.match_classifier import train_classifier_from_db


def main():
    print("🚀 Training match classifier from historical assessments...")
    print()

    # Initialize database
    database.init_db()
    db = database.SessionLocal()

    try:
        success = train_classifier_from_db(db)
        if success:
            print()
            print("✅ Classifier training complete!")
            print("   Fast scoring is now enabled in the extension.")
            print("   Next: Restart the backend for changes to take effect.")
        else:
            print()
            print(
                "⚠️  Classifier training skipped (insufficient data or training failed)."
            )
            print("   You need >10 jobs with match_scored=True to train the model.")
            print("   Heuristic fallback is still in use for fast scoring.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
