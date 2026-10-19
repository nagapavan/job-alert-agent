"""Data-driven ATS portal engine.

Portals are described in ``data/ats_portals.json`` and executed by one small scraper per feed
*shape* (see :class:`backend.ats.engine.ShapeScraper`). Adding a portal of an existing shape is a
JSON entry; only a genuinely new feed shape needs a new class.

See ``docs/ATS_GENERIC_ENGINE_PLAN.md`` for the design and migration order.
"""

from backend.ats.engine import (
    PortalConfig,
    ShapeScraper,
    get_shape_scraper,
    load_portal_configs,
    run_portal,
)

__all__ = [
    "PortalConfig",
    "ShapeScraper",
    "get_shape_scraper",
    "load_portal_configs",
    "run_portal",
]
