"""Tests for the Google Jobs run policy (issue #104): cooldown + query rotation."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from backend import google_jobs_state as gjs


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(gjs, "STATE_PATH", tmp_path / "google_jobs_state.json")
    return tmp_path


class TestCooldown:
    def test_no_state_means_no_cooldown(self):
        assert gjs.cooling_down_until() is None
        assert gjs.cooldown_reason() == ""

    def test_block_starts_cooldown_with_a_deadline(self, monkeypatch):
        monkeypatch.setattr(gjs, "COOLDOWN_HOURS", 24.0)

        until = gjs.record_block("blocked on query 'staff engineer'")

        assert until > datetime.now(timezone.utc)
        assert gjs.cooling_down_until() is not None
        reason = gjs.cooldown_reason()
        assert "cooling down until" in reason
        assert until.strftime("%Y-%m-%d") in reason

    def test_expired_cooldown_lets_the_channel_run_again(self):
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        gjs.STATE_PATH.write_text(
            json.dumps({"blocked_until": past.isoformat()}), encoding="utf-8"
        )

        assert gjs.cooling_down_until() is None
        assert gjs.cooldown_reason() == ""

    def test_successful_run_clears_the_cooldown(self):
        gjs.record_block("blocked")
        assert gjs.cooldown_reason()

        gjs.record_run(queries_used=2, total=5)

        assert gjs.cooldown_reason() == ""
        state = gjs.load_state()
        assert "blocked_until" not in state

    def test_corrupt_state_is_ignored(self):
        gjs.STATE_PATH.write_text("{not json", encoding="utf-8")
        assert gjs.load_state() == {}
        assert gjs.cooldown_reason() == ""


class TestQueryRotation:
    def test_subset_rotates_across_runs(self, monkeypatch):
        monkeypatch.setattr(gjs, "QUERIES_PER_RUN", 2)
        queries = ["q1", "q2", "q3", "q4"]

        first, total = gjs.select_queries(queries)
        gjs.record_run(queries_used=len(first), total=total)
        second, _ = gjs.select_queries(queries)
        gjs.record_run(queries_used=len(second), total=total)
        third, _ = gjs.select_queries(queries)

        assert (first, second, third) == (["q1", "q2"], ["q3", "q4"], ["q1", "q2"])

    def test_limit_never_exceeds_the_query_set(self, monkeypatch):
        monkeypatch.setattr(gjs, "QUERIES_PER_RUN", 10)
        picked, total = gjs.select_queries(["a", "b", "c"])
        assert total == 3
        assert picked == ["a", "b", "c"]

    def test_empty_query_set(self):
        assert gjs.select_queries([]) == ([], 0)

    def test_cursor_is_bounded_by_the_query_count(self, monkeypatch):
        monkeypatch.setattr(gjs, "QUERIES_PER_RUN", 1)
        gjs.STATE_PATH.write_text(json.dumps({"query_cursor": 97}), encoding="utf-8")
        picked, _ = gjs.select_queries(["a", "b", "c"])
        assert picked == ["b"]


class TestChannelDefault:
    def test_google_jobs_is_opt_in(self):
        from backend.database import DEFAULT_FEATURE_FLAGS

        assert DEFAULT_FEATURE_FLAGS["google_jobs"] is False
