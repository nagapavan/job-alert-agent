"""Similarity thresholds shared by the zero-token filters.

Named here because one concept had several literals across layers, and they had already drifted
(a cache threshold of 0.82 in one caller against 0.85 everywhere else, and SPA copy quoting 82%
for a filter whose real default was 0.80). Per-user tuning, if it is ever wanted, belongs in
``ScoringConfig``; these are the defaults.
"""

DISMISSAL_SIMILARITY_THRESHOLD = 0.80  # negative role memory: auto-filter a discovered posting
SEMANTIC_CACHE_THRESHOLD = 0.85        # Q&A / essay cache hit: reuse a previously verified answer
TITLE_JACCARD_MIN = 0.60               # symmetric overlap of distinguishing title tokens
DISTINGUISHING_COVERAGE_MIN = 0.60     # share of the rule's distinguishing tokens in the title
