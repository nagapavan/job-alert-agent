"""Static guard for the SPA boot path.

The dashboard renders the target-preference widgets before ``GET /api/preferences`` resolves, so
``COUNTRIES_HUBS_DATA`` is still empty when that first render runs. A direct
``COUNTRIES_HUBS_DATA[key].hubs`` dereference therefore threw at boot, which aborted init and left
the whole job feed blank — a data-loss-looking symptom with no data loss. Every lookup has to go
through ``countryHubEntry()``, which always returns an object.
"""

import re
from pathlib import Path

APP_JS = Path(__file__).resolve().parent.parent / "frontend" / "app.js"


def test_country_hub_lookups_never_dereference_a_missing_entry():
    src = APP_JS.read_text(encoding="utf-8")
    assert "function countryHubEntry(" in src
    assert re.findall(r"COUNTRIES_HUBS_DATA\[[^\]]+\]\s*\.", src) == []
