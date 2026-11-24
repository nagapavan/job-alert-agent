"""Country -> suggested hiring hubs, served with the discovery preferences.

This is a UI suggestion list (which cities to offer once a country is chosen), not a country
resolver — ``geo.py`` owns resolution. The data lives in ``data/country_hubs.json`` so it can be
extended without a code change, and it is shipped to the SPA inside ``GET /api/preferences`` so the
dashboard needs no second request.
"""

import json
import logging
from pathlib import Path
from typing import Dict

logger = logging.getLogger(__name__)

_HUBS_FILE = Path(__file__).resolve().parent.parent / "data" / "country_hubs.json"

_cache: Dict[str, dict] | None = None


def load_country_hubs() -> Dict[str, dict]:
    """Return the ``{country: {name, flag, hubs}}`` map, or ``{}`` when the file is unusable."""
    global _cache
    if _cache is None:
        try:
            data = json.loads(_HUBS_FILE.read_text(encoding="utf-8"))
            _cache = data if isinstance(data, dict) else {}
        except Exception as err:
            logger.warning(f"Country hub suggestions unavailable: {err}")
            _cache = {}
    return _cache
