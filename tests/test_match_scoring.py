"""Unit tests for the deterministic requirement-coverage scoring engine."""
from backend.match_scoring import (
    APPLY,
    APPLY_WITH_CAUTION,
    MUST_HAVE,
    NICE_TO_HAVE,
    SKIP,
    UNKNOWN_RECOMMENDATION,
    RequirementAssessment,
    RequirementItem,
    ScoringConfig,
    compute_match,
)


def _req(status, category=MUST_HAVE, text="r", **kw):
    return RequirementItem(text=text, category=category, status=status, **kw)


def _score(items, config=None):
    return compute_match(RequirementAssessment(requirements=items), config)


def test_all_must_and_nice_met_is_perfect_and_apply():
    res = _score([
        _req("met", text="Python"),
        _req("met", category=NICE_TO_HAVE, text="K8s"),
    ])
    assert res.score == 100.0
    assert res.band == "Strong"
    assert res.apply_recommendation == APPLY
    assert res.must_coverage == 1.0
    assert res.nice_coverage == 1.0


def test_all_missing_is_zero_and_skip():
    res = _score([_req("missing"), _req("missing")])
    assert res.score == 0.0
    assert res.band == "Poor"
    assert res.apply_recommendation == SKIP


def test_partial_credit():
    res = _score([_req("met"), _req("met"), _req("partial"), _req("missing")])
    # (1 + 1 + 0.5 + 0) / 4 = 0.625
    assert res.must_coverage == 0.625
    assert res.score == 62.5
    assert res.band == "Moderate"


def test_unknown_excluded_from_denominator():
    res = _score([
        _req("met", text="A"),
        _req("unknown", text="B"),
        _req("unknown", text="C"),
    ])
    assert res.must_coverage == 1.0
    assert res.score == 100.0
    assert res.unverified == ["B", "C"]


def test_unknown_only_yields_no_score():
    res = _score([_req("unknown"), _req("unknown", category=NICE_TO_HAVE)])
    assert res.score is None
    assert res.band == "Unknown"
    assert res.apply_recommendation == UNKNOWN_RECOMMENDATION


def test_must_have_floor_gates_to_skip():
    # 1 of 4 must-haves met (0.25 < 0.5 floor) even though every nice-to-have is met.
    items = [
        _req("met", text="m1"),
        _req("missing", text="m2"),
        _req("missing", text="m3"),
        _req("missing", text="m4"),
    ] + [_req("met", category=NICE_TO_HAVE, text=f"n{i}") for i in range(4)]
    res = _score(items)
    assert res.must_coverage == 0.25
    assert res.score == 47.5
    assert res.apply_recommendation == SKIP


def test_must_floor_caps_score_when_blend_would_exceed_cap():
    # 2 of 5 must-haves met (0.4, below floor) + all nice-haves met.
    items = [_req("met", text="m1"), _req("met", text="m2")] + [
        _req("missing", text=f"m{i}") for i in range(3)
    ] + [_req("met", category=NICE_TO_HAVE, text=f"n{i}") for i in range(5)]
    res = _score(items)
    assert res.must_coverage == 0.4
    # Ungated blend would be 0.7*0.4 + 0.3*1.0 = 58 -> capped at 55.
    assert res.score == ScoringConfig().cap_when_below_floor
    assert res.apply_recommendation == SKIP


def test_must_dominates_nice_via_category_weights():
    # All must-haves met, all nice-to-haves missing -> 0.7*1.0 + 0.3*0.0 = 70.
    items = [_req("met"), _req("met")] + [
        _req("missing", category=NICE_TO_HAVE) for _ in range(3)
    ]
    res = _score(items)
    assert res.score == 70.0
    assert res.band == "Good"
    assert res.apply_recommendation == APPLY


def test_explicit_weight_override():
    items = [
        _req("met", text="heavy", weight=9.0),
        _req("missing", text="light", weight=1.0),
    ]
    res = _score(items)
    assert res.must_coverage == 0.9
    assert res.score == 90.0


def test_over_qualified_is_advisory_only():
    plain = _score([_req("met"), _req("met")])
    over = _score([_req("met"), _req("met", over_qualified=True)])
    assert over.score == plain.score == 100.0
    assert over.over_qualified is True
    assert over.apply_recommendation == APPLY_WITH_CAUTION


def test_missing_must_haves_sorted_by_weight():
    items = [
        _req("missing", text="light", weight=1.0),
        _req("missing", text="heavy", weight=9.0),
        _req("met", text="ok"),
    ]
    res = _score(items)
    assert res.missing_must_haves == ["heavy", "light"]


def test_band_boundaries():
    cfg = ScoringConfig()
    assert _score([_req("unknown")], cfg).band == "Unknown"
    assert _score([_req("met")], cfg).band == "Strong"          # 100
    assert _score([_req("met")] * 3 + [_req("missing")]).band == "Good"      # 75
    assert _score([_req("met")] * 2 + [_req("partial"), _req("missing")]).band == "Moderate"  # 62.5
    assert _score([_req("met"), _req("missing")]).band == "Weak"  # 50 (not gated)
    assert _score([_req("met")] + [_req("missing")] * 3).band == "Poor"  # 25, gated


def test_config_from_mapping_applies_known_keys_only():
    cfg = ScoringConfig.from_mapping({"must_floor": 0.8, "bogus": 5, "good": 60})
    assert cfg.must_floor == 0.8
    assert cfg.good == 60.0
    assert cfg.must_weight == 3.0


def test_met_and_known_counts():
    res = _score([_req("met"), _req("partial"), _req("unknown")])
    assert res.met_count == 1
    assert res.known_count == 2
