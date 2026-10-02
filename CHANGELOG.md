# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-10-02

### Added
- **Hybrid fast classifier + on-demand LLM analysis**
  - Rule-based heuristic classifier: instant scoring (<100ms, 0 tokens)
  - Optional trained ML classifier (logistic regression) for improved accuracy
  - On-demand `/api/match/detailed-analysis` endpoint for LLM narrative (cached)
  - ~90% token savings vs. monolithic LLM approach
  - See [PORTFOLIO_JOURNEY.md](docs/PORTFOLIO_JOURNEY.md) for architecture + lessons learned

- **Match scoring improvements**
  - Zero-token semantic dismissal filter (skip obviously-rejected jobs)
  - Requirements-based assessment with structured evidence snippets
  - `match_method` field in API response (shows "heuristic" vs "classifier" vs "trained_classifier")

- **Documentation**
  - [PORTFOLIO_JOURNEY.md](docs/PORTFOLIO_JOURNEY.md): AI/ML design narrative (phases 1-5, decisions, lessons)
  - [HYBRID_MATCHER_GUIDE.md](docs/HYBRID_MATCHER_GUIDE.md): Technical deep-dive

### Changed
- `/api/match/analyze` now returns instant score from classifier (was full LLM extraction)
- Extension shows "💡 View Detailed Analysis" button for on-demand narrative

### Technical Details
- 434 tests passing (all previous + 4 new)
- 0 regressions
- New files: `backend/match_classifier.py`, `scripts/train_match_classifier.py`
- Performance: <100ms fast path, 5-15s on-demand detailed (vs 5-15s per job before)

---

## [0.9.0] - 2026-09-30

Initial release candidate (before hybrid matcher).
