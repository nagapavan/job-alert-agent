"""Per-host request pacing / rate-limiting for discovery scrapers.

One shared :class:`HostPacer` instance is created per scrape run and threaded into every shape
scraper, so a company whose board is hit by two portals still sees evenly spaced requests. The
pacer enforces two independent guardrails:

1. an inter-request **delay** per host (with jitter), and
2. a hard per-run **request ceiling** per host,

plus a short cooldown after a 429/503 so a rate-limited host is not retried immediately.

See ``docs/POLITE_CRAWLING_AND_WORKDAY.md`` §2.2. Thread-safe: keys are hostnames, the lock is
per-pacer (cheap; contention is negligible at our request rates).
"""

from __future__ import annotations

import logging
import random
import threading
import time
import urllib.parse
from typing import Dict, Optional

logger = logging.getLogger("scraper.ats.pacing")


class HostPacer:
    """Per-host inter-request delay + per-run request ceiling. Thread-safe."""

    def __init__(
        self,
        default_delay: float,
        *,
        jitter: float = 0.5,
        max_requests_per_run: int = 60,
        cooldown_seconds: float = 30.0,
        _sleep=time.sleep,
        _monotonic=time.monotonic,
    ):
        self.default_delay = max(0.0, float(default_delay))
        self.jitter = min(max(0.0, float(jitter)), 1.0)
        self.max_requests_per_run = max(1, int(max_requests_per_run))
        self.cooldown_seconds = max(0.0, float(cooldown_seconds))
        self._sleep = _sleep
        self._monotonic = _monotonic

        self._lock = threading.Lock()
        self._last_request: Dict[str, float] = {}
        self._counts: Dict[str, int] = {}
        self._block_until: Dict[str, float] = {}
        self._stopped: Dict[str, str] = {}
        self._per_host_delay: Dict[str, Optional[float]] = {}

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _host(url: str) -> str:
        try:
            netloc = urllib.parse.urlparse(url).netloc
            return netloc or url
        except Exception:  # noqa: BLE001
            return url

    def _delay_for(self, host: str) -> float:
        delay = self.default_delay
        override = self._per_host_delay.get(host)
        if override is not None:
            delay = override
        if delay <= 0:
            return 0.0
        return delay * (1.0 + self.jitter * random.random())

    # -- public API --------------------------------------------------------

    def allow(self, url: str, *, delay: Optional[float] = None) -> bool:
        """True when ``url``'s host may be requested now.

        Returns False once the host has hit the per-run ceiling or is inside a rate-limit cooldown.
        ``delay`` overrides the inter-request delay for this host (used by per-portal config).
        """
        host = self._host(url)
        if delay is not None:
            with self._lock:
                self._per_host_delay[host] = max(0.0, float(delay))
        with self._lock:
            if self._counts.get(host, 0) >= self.max_requests_per_run:
                if self._stopped.get(host) != "ceiling":
                    self._stopped[host] = "ceiling"
                    logger.info(
                        f"Pacing: host '{host}' reached the per-run ceiling "
                        f"({self.max_requests_per_run}); stopping this host for the run."
                    )
                return False
            if self._monotonic() < self._block_until.get(host, 0.0):
                return False
        return True

    def wait(self, url: str, *, delay: Optional[float] = None) -> float:
        """Blocks until the host's next request is due; returns the seconds actually slept."""
        if not self.allow(url, delay=delay):
            return 0.0
        host = self._host(url)
        with self._lock:
            last = self._last_request.get(host)
            self._last_request[host] = self._monotonic()
            self._counts[host] = self._counts.get(host, 0) + 1
        target = self._delay_for(host)
        if last is not None and target > 0:
            elapsed = self._monotonic() - last
            remaining = target - elapsed
            if remaining > 0:
                self._sleep(remaining)
                return remaining
        return 0.0

    def note_failure(self, url: str, retry_after: Optional[float] = None) -> None:
        """Records a 429/503 (or explicit ``Retry-After``) and cools the host down."""
        host = self._host(url)
        cooldown = self.cooldown_seconds
        if retry_after is not None and retry_after > 0:
            cooldown = max(cooldown, float(retry_after))
        if cooldown <= 0:
            return
        with self._lock:
            self._block_until[host] = self._monotonic() + cooldown
        logger.warning(
            f"Pacing: host '{host}' signalled rate-limiting; cooling down for "
            f"{cooldown:.0f}s."
        )

    def stats(self) -> Dict[str, int]:
        with self._lock:
            return dict(self._counts)


def build_pacer(
    *,
    default_delay: Optional[float] = None,
    jitter: Optional[float] = None,
    max_requests_per_run: Optional[int] = None,
    cooldown_seconds: Optional[float] = None,
    **kw,
) -> HostPacer:
    """Builds a :class:`HostPacer` from config defaults, allowing per-call overrides."""
    from backend import config

    return HostPacer(
        config.SCRAPE_PACING_SECONDS if default_delay is None else default_delay,
        jitter=config.SCRAPE_PACING_JITTER if jitter is None else jitter,
        max_requests_per_run=(
            config.SCRAPE_MAX_REQUESTS_PER_HOST_PER_RUN
            if max_requests_per_run is None
            else max_requests_per_run
        ),
        cooldown_seconds=(
            config.SCRAPE_HOST_COOLDOWN_SECONDS if cooldown_seconds is None else cooldown_seconds
        ),
        **kw,
    )
