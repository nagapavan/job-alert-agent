"""
Unit tests for the offline geo resolver (geonamescache + pycountry).
"""
import json

from backend.geo import (
    countries_for_cities,
    countries_for_terms,
    resolve_countries,
    is_foreign_restricted,
)


def test_countries_for_cities_maps_major_hubs():
    assert countries_for_cities(["Bengaluru", "Hyderabad"]) == {"IN"}
    assert countries_for_cities(["London", "Manchester", "Belfast"]) == {"GB"}
    assert countries_for_cities(["Dublin", "Limerick"]) == {"IE"}
    assert countries_for_cities(["Singapore"]) == {"SG"}


def test_countries_for_terms_accepts_names_and_codes():
    assert countries_for_terms(["india", "uk"]) == {"IN", "GB"}
    assert countries_for_terms(["united states"]) == {"US"}
    assert countries_for_terms(["all"]) == set()


def test_resolve_countries_handles_names_cities_and_prefixes():
    assert resolve_countries("Remote, Canada; Remote, United States") == {"CA", "US"}
    assert resolve_countries("SF, NY, Remote") == {"US"}
    assert resolve_countries("US-PA-Remote") == {"US"}
    assert resolve_countries("IN-Bengaluru") == {"IN"}
    # Broad / generic regions must not resolve to a country.
    assert resolve_countries("Remote") == set()
    assert resolve_countries("Worldwide Remote") == set()
    assert resolve_countries("APAC") == set()


def test_is_foreign_restricted():
    targets = {"IN", "SG", "GB", "IE"}
    assert is_foreign_restricted("Remote, Canada", targets) is True
    assert is_foreign_restricted("SF, NY, Remote", targets) is True
    assert is_foreign_restricted("Remote, India", targets) is False
    assert is_foreign_restricted("Remote, London", targets) is False
    # No recognisable country -> never foreign.
    assert is_foreign_restricted("Remote", targets) is False


def test_alias_file_is_loaded():
    from backend.geo import _aliases

    aliases = _aliases()
    assert aliases["city"]["sf"] == "US"
    assert "apac" in aliases["broad"]


def test_custom_alias_file_is_merged(tmp_path, monkeypatch):
    """Users can add aliases via data/location_aliases.json without code changes."""
    import backend.geo as geo

    custom = tmp_path / "location_aliases.json"
    custom.write_text(
        json.dumps({
            "city_aliases": {"springfieldx": "US"},
            "country_aliases": {"freedonia": "US"},
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(geo, "_ALIASES_FILE", custom)
    geo._aliases.cache_clear()
    geo._city_index.cache_clear()
    geo._country_index.cache_clear()
    geo.resolve_countries.cache_clear()
    try:
        assert "US" in geo.resolve_countries("springfieldx")
        assert geo.countries_for_terms(["freedonia"]) == {"US"}
    finally:
        for fn in (geo._aliases, geo._city_index, geo._country_index, geo.resolve_countries):
            fn.cache_clear()
