"""Unit tests for backend/ats/pacing.py (per-host request pacing)."""

from backend.ats.pacing import HostPacer, build_pacer


class _Clock:
    """Deterministic fake clock + sleep recorder."""

    def __init__(self):
        self.t = 1000.0
        self.slept = []

    def monotonic(self):
        return self.t

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.t += seconds


def _pacer(clock, **kw):
    """Builds a pacer with the test clock; ``default_delay`` defaults to 0.5 unless given."""
    delay = kw.pop("default_delay", 0.5)
    kw.setdefault("jitter", 0.0)
    return HostPacer(delay, _sleep=clock.sleep, _monotonic=clock.monotonic, **kw)


def test_wait_enforces_delay_between_same_host_requests():
    c = _Clock()
    p = _pacer(c)
    p.wait("https://a.example.com/jobs")
    p.wait("https://a.example.com/jobs")
    assert c.slept == [0.5]


def test_wait_is_independent_across_hosts():
    c = _Clock()
    p = _pacer(c)
    p.wait("https://a.example.com/jobs")
    p.wait("https://b.example.com/jobs")
    # Different hosts: no delay imposed.
    assert c.slept == []


def test_zero_delay_never_sleeps():
    c = _Clock()
    p = _pacer(c, default_delay=0.0)
    p.wait("https://a.example.com/x")
    p.wait("https://a.example.com/x")
    assert c.slept == []


def test_per_call_delay_override():
    c = _Clock()
    p = _pacer(c)
    p.wait("https://a.example.com/x", delay=2.0)
    p.wait("https://a.example.com/x")
    assert c.slept == [2.0]


def test_per_run_ceiling_stops_host_but_not_others():
    c = _Clock()
    p = _pacer(c, max_requests_per_run=2, default_delay=0.0)
    assert p.allow("https://a.example.com/1")
    p.wait("https://a.example.com/1")
    p.wait("https://a.example.com/2")
    # Third request to the same host is refused...
    assert p.allow("https://a.example.com/3") is False
    # ...but a different host is unaffected.
    assert p.allow("https://b.example.com/1") is True


def test_note_failure_cools_down_host():
    c = _Clock()
    p = _pacer(c, default_delay=0.0, cooldown_seconds=30.0)
    p.wait("https://a.example.com/1")
    p.note_failure("https://a.example.com/1")
    assert p.allow("https://a.example.com/2") is False
    # After the cooldown elapses the host is allowed again.
    c.t += 31.0
    assert p.allow("https://a.example.com/3") is True


def test_retry_after_extends_cooldown():
    c = _Clock()
    p = _pacer(c, default_delay=0.0, cooldown_seconds=5.0)
    p.note_failure("https://a.example.com/1", retry_after=60.0)
    c.t += 10.0
    assert p.allow("https://a.example.com/1") is False


def test_jitter_scales_delay():
    c = _Clock()
    p = HostPacer(
        1.0, jitter=0.5, _sleep=c.sleep, _monotonic=c.monotonic
    )
    # First request primes the clock; second request waits 1.0..1.5s.
    p.wait("https://a.example.com/x")
    p.wait("https://a.example.com/x")
    assert len(c.slept) == 1
    assert 1.0 <= c.slept[0] <= 1.5


def test_build_pacer_uses_config_defaults():
    p = build_pacer(default_delay=0.0, jitter=0.0, max_requests_per_run=7, cooldown_seconds=0.0)
    assert p.max_requests_per_run == 7
    assert p.jitter == 0.0
    assert p.default_delay == 0.0
