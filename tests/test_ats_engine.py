"""Unit tests for the config-driven ATS portal engine (backend/ats/engine.py)."""

from unittest.mock import MagicMock, patch

import pytest

from backend.ats.engine import (
    PortalConfig,
    _resolve_path,
    load_portal_configs,
    run_portal,
)


@pytest.fixture(autouse=True)
def _no_permission_gate(monkeypatch):
    """Neutralise the robots/AI-use permission gate so these tests are offline + deterministic.

    The gate itself is covered by tests/test_ats_pacing.py and tests/test_scraper.py.
    """
    monkeypatch.setattr("backend.scraper.may_fetch", lambda url, *a, **k: True)


# -------------------------------------------------------------
# Config loading
# -------------------------------------------------------------
def test_load_portal_configs_reads_lever():
    configs = load_portal_configs()
    ids = {c.id for c in configs}
    assert "lever" in ids
    lever = next(c for c in configs if c.id == "lever")
    assert lever.shape == "simple_json"
    assert lever.label == "Lever Portal"
    assert lever.default_flag == "ats_lever"


def test_load_portal_configs_missing_file_returns_empty(tmp_path):
    assert load_portal_configs(tmp_path / "nope.json") == []


# -------------------------------------------------------------
# Dotted-path resolution
# -------------------------------------------------------------
def test_resolve_path_nested_and_missing():
    obj = {"a": {"b": {"c": "hit"}}}
    assert _resolve_path(obj, "a.b.c") == "hit"
    assert _resolve_path(obj, "a.b.missing") is None
    assert _resolve_path(obj, "a.b.c.deeper") is None
    assert _resolve_path(obj, "") is obj


# -------------------------------------------------------------
# Shape dispatcher
# -------------------------------------------------------------
def test_run_portal_unknown_shape_returns_empty():
    cfg = PortalConfig(
        id="ghost", label="Ghost", shape="does_not_exist", list_url="https://x/{slug}"
    )
    assert run_portal(cfg, company_name="Acme") == []


# -------------------------------------------------------------
# simple_json end-to-end (Lever shape)
# -------------------------------------------------------------
@patch("backend.ats.engine.requests.get")
def test_run_portal_lever_maps_fields(mock_get):
    mock_get.return_value = MagicMock(
        status_code=200,
        json=MagicMock(
            return_value=[
                {
                    "text": "Staff Software Engineer",
                    "hostedUrl": "https://jobs.lever.co/acme/host",
                    "applyUrl": "https://jobs.lever.co/acme/apply",
                    "descriptionPlain": "Build things.",
                    "categories": {"location": "Bengaluru, India"},
                }
            ]
        ),
    )
    cfg = next(c for c in load_portal_configs() if c.id == "lever")
    jobs = run_portal(cfg, company_name="Acme", careers_url="https://jobs.lever.co/acme")
    assert len(jobs) == 1
    j = jobs[0]
    assert j["title"] == "Staff Software Engineer"
    assert j["url"] == "https://jobs.lever.co/acme/apply"
    assert j["location"] == "Bengaluru, India"
    assert j["source"] == "Lever Portal"
    assert j["source_type"] == "Direct"


@patch("backend.ats.engine.requests.get")
def test_run_portal_lever_applies_title_filter(mock_get):
    mock_get.return_value = MagicMock(
        status_code=200,
        json=MagicMock(
            return_value=[
                {"text": "Backend Engineer", "categories": {"location": "India"}},
                {"text": "Product Designer", "categories": {"location": "India"}},
            ]
        ),
    )
    cfg = next(c for c in load_portal_configs() if c.id == "lever")
    jobs = run_portal(
        cfg,
        company_name="Acme",
        careers_url="https://jobs.lever.co/acme",
        title_filters=["backend engineer"],
    )
    assert [j["title"] for j in jobs] == ["Backend Engineer"]


@patch("backend.ats.engine.requests.get")
def test_run_portal_lever_description_fallback_when_missing(mock_get):
    mock_get.return_value = MagicMock(
        status_code=200,
        json=MagicMock(
            return_value=[{"text": "Backend Engineer", "categories": {"location": "India"}}]
        ),
    )
    cfg = next(c for c in load_portal_configs() if c.id == "lever")
    jobs = run_portal(cfg, company_name="Acme", careers_url="https://jobs.lever.co/acme")
    assert "opening at Acme" in jobs[0]["description"]


@patch("backend.ats.engine.requests.get")
def test_run_portal_empty_company_name_short_circuits(mock_get):
    cfg = next(c for c in load_portal_configs() if c.id == "lever")
    assert run_portal(cfg, company_name="") == []
    mock_get.assert_not_called()


@patch("backend.ats.engine.requests.get")
def test_run_portal_non_200_returns_empty(mock_get):
    mock_get.return_value = MagicMock(status_code=404)
    cfg = next(c for c in load_portal_configs() if c.id == "lever")
    assert run_portal(cfg, company_name="Acme") == []


# -------------------------------------------------------------
# Ashby variant: multi-location OR filter
# -------------------------------------------------------------
def _ashby_cfg():
    return next(c for c in load_portal_configs() if c.id == "ashby")


@patch("backend.ats.engine.requests.get")
def test_ashby_secondary_location_satisfies_filter(mock_get):
    """A role is eligible if ANY of its locations matches (primary or secondary)."""
    mock_get.return_value = MagicMock(
        status_code=200,
        json=MagicMock(
            return_value={
                "jobs": [
                    {
                        "title": "Backend Engineer",
                        "jobUrl": "https://jobs.ashbyhq.com/acme/1",
                        "descriptionHtml": "<p>Scale systems.</p>",
                        "location": "San Francisco, CA",
                        "secondaryLocations": [{"location": "Bengaluru, India"}],
                    }
                ]
            }
        ),
    )
    jobs = run_portal(
        _ashby_cfg(),
        company_name="Acme",
        careers_url="https://jobs.ashbyhq.com/acme",
        location_filters=["india"],
    )
    assert len(jobs) == 1
    assert jobs[0]["location"] == "San Francisco, CA"
    assert "_secondary_locations" not in jobs[0]


@patch("backend.ats.engine.requests.get")
def test_ashby_no_location_match_excluded(mock_get):
    """Faithful to the original Ashby semantics, which OR'd location with work-mode.

    With no work-mode filter, ``check_workmode_match`` returns True (see preference_filter), so the
    OR gate admits every row regardless of location. This preserves the pre-engine behaviour
    exactly; the stricter AND gate applies only when a work-mode IS set.
    """
    mock_get.return_value = MagicMock(
        status_code=200,
        json=MagicMock(
            return_value={
                "jobs": [
                    {
                        "title": "Backend Engineer",
                        "jobUrl": "https://jobs.ashbyhq.com/acme/2",
                        "location": "London, UK",
                        "secondaryLocations": [{"location": "Berlin, DE"}],
                    }
                ]
            }
        ),
    )
    jobs = run_portal(
        _ashby_cfg(),
        company_name="Acme",
        careers_url="https://jobs.ashbyhq.com/acme",
        location_filters=["india"],
    )
    # Faithful to legacy: admitted because work_mode is unset.
    assert len(jobs) == 1


@patch("backend.ats.engine.requests.get")
def test_ashby_workmode_filter_applied_when_set(mock_get):
    """When a work-mode IS set, the OR gate can still admit a row on work-mode alone."""
    mock_get.return_value = MagicMock(
        status_code=200,
        json=MagicMock(
            return_value={
                "jobs": [
                    {
                        "title": "Backend Engineer",
                        "jobUrl": "https://jobs.ashbyhq.com/acme/3",
                        "location": "Bengaluru, India",
                    }
                ]
            }
        ),
    )
    jobs = run_portal(
        _ashby_cfg(),
        company_name="Acme",
        careers_url="https://jobs.ashbyhq.com/acme",
        work_mode="onsite",
    )
    assert len(jobs) == 1



@patch("backend.ats.engine.requests.get")
def test_ashby_description_html_cleaned(mock_get):
    mock_get.return_value = MagicMock(
        status_code=200,
        json=MagicMock(
            return_value={
                "jobs": [
                    {
                        "title": "Backend Engineer",
                        "jobUrl": "https://jobs.ashbyhq.com/acme/3",
                        "descriptionHtml": "<p>Build <b>reasoning</b> models.</p>",
                        "location": "Remote",
                    }
                ]
            }
        ),
    )
    jobs = run_portal(_ashby_cfg(), company_name="Acme", careers_url="https://jobs.ashbyhq.com/acme")
    assert "reasoning" in jobs[0]["description"]
    assert "<" not in jobs[0]["description"]


# -------------------------------------------------------------
# Workable variant: (Remote) suffix + full_description fallback
# -------------------------------------------------------------
def _workable_cfg():
    return next(c for c in load_portal_configs() if c.id == "workable")


@patch("backend.ats.engine.requests.get")
def test_workable_remote_suffix_and_city_join(mock_get):
    mock_get.return_value = MagicMock(
        status_code=200,
        json=MagicMock(
            return_value={
                "jobs": [
                    {
                        "title": "Platform Engineer",
                        "url": "https://apply.workable.com/acme/j/1",
                        "city": "Austin",
                        "state": "TX",
                        "country": "USA",
                        "telecommuting": True,
                        "description": "<p>Own the platform.</p>",
                    }
                ]
            }
        ),
    )
    jobs = run_portal(
        _workable_cfg(), company_name="Acme", careers_url="https://apply.workable.com/acme"
    )
    assert jobs[0]["location"] == "Austin, TX, USA (Remote)"
    assert jobs[0]["source"] == "Workable Portal"


@patch("backend.ats.engine.requests.get")
def test_workable_full_description_fallback(mock_get):
    mock_get.return_value = MagicMock(
        status_code=200,
        json=MagicMock(
            return_value={
                "jobs": [
                    {
                        "title": "Platform Engineer",
                        "url": "https://apply.workable.com/acme/j/2",
                        "city": "Austin",
                        "full_description": "<p>Full role detail.</p>",
                    }
                ]
            }
        ),
    )
    jobs = run_portal(
        _workable_cfg(), company_name="Acme", careers_url="https://apply.workable.com/acme"
    )
    assert "Full role detail" in jobs[0]["description"]


# -------------------------------------------------------------
# Greenhouse variant: embedded content, detail only when empty
# -------------------------------------------------------------
def _greenhouse_cfg():
    return next(c for c in load_portal_configs() if c.id == "greenhouse")


@patch("backend.ats.engine.requests.get")
def test_greenhouse_uses_embedded_content_without_detail_call(mock_get):
    mock_get.return_value = MagicMock(
        status_code=200,
        json=MagicMock(
            return_value={
                "jobs": [
                    {
                        "id": 1,
                        "title": "Platform Engineer",
                        "absolute_url": "https://boards.greenhouse.io/acme/jobs/1",
                        "location": {"name": "Remote, India"},
                        "content": "<p>Own <b>the</b> platform.</p>",
                    }
                ]
            }
        ),
    )
    jobs = run_portal(_greenhouse_cfg(), company_name="Acme")
    assert len(jobs) == 1
    assert "platform" in jobs[0]["description"]
    assert "<" not in jobs[0]["description"]
    assert jobs[0]["location"] == "Remote, India"
    # A single list call; the embedded content means no per-posting detail fetch.
    assert mock_get.call_count == 1


@patch("backend.ats.engine.requests.get")
def test_greenhouse_detail_fallback_when_content_empty(mock_get):
    calls = []

    def _fake_get(url, headers=None, timeout=None, params=None):
        calls.append(url)
        resp = MagicMock()
        resp.status_code = 200
        if url.rstrip("/").split("/")[-1].isdigit():
            resp.json.return_value = {"content": "<p>Detailed role body.</p>"}
        else:
            resp.json.return_value = {
                "jobs": [
                    {
                        "id": 7,
                        "title": "Platform Engineer",
                        "absolute_url": "https://boards.greenhouse.io/acme/jobs/7",
                        "location": {"name": "Remote"},
                        "content": "",
                    }
                ]
            }
        return resp

    mock_get.side_effect = _fake_get
    jobs = run_portal(_greenhouse_cfg(), company_name="Acme")
    assert "Detailed role body" in jobs[0]["description"]
    assert len(calls) == 2


# -------------------------------------------------------------
# SmartRecruiters variant: (Remote) suffix + dict detail (desc + applyUrl)
# -------------------------------------------------------------
def _smartrecruiters_cfg():
    return next(c for c in load_portal_configs() if c.id == "smartrecruiters")


def _smartrecruiters_fake_get(list_payload, detail_payload):
    def _fake_get(url, headers=None, timeout=None, params=None):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = (
            detail_payload if url.rstrip("/").split("/")[-1].isdigit() else list_payload
        )
        return resp

    return _fake_get


@patch("backend.ats.engine.requests.get")
def test_smartrecruiters_detail_overrides_description_and_url(mock_get):
    list_payload = {
        "totalFound": 1,
        "limit": 100,
        "offset": 0,
        "content": [
            {
                "id": "744000000000001",
                "name": "Staff Software Engineer",
                "ref": "https://api.smartrecruiters.com/v1/companies/Acme/postings/744000000000001",
                "location": {
                    "city": "Bengaluru",
                    "region": "Karnataka",
                    "country": "India",
                    "remote": True,
                },
            }
        ],
    }
    detail_payload = {
        "id": "744000000000001",
        "applyUrl": "https://jobs.smartrecruiters.com/Acme/744000000000001",
        "jobAd": {
            "sections": {"jobDescription": {"text": "<p>Build distributed systems.</p>"}}
        },
    }
    mock_get.side_effect = _smartrecruiters_fake_get(list_payload, detail_payload)

    jobs = run_portal(_smartrecruiters_cfg(), company_name="Acme")
    assert len(jobs) == 1
    assert jobs[0]["url"] == "https://jobs.smartrecruiters.com/Acme/744000000000001"
    assert "distributed systems" in jobs[0]["description"]
    assert "<" not in jobs[0]["description"]
    assert "Bengaluru" in jobs[0]["location"]
    assert jobs[0]["location"].endswith("(Remote)")
    assert jobs[0]["source"] == "SmartRecruiters Portal"
    # Pagination uses the API's limit/offset params.
    list_calls = [c for c in mock_get.call_args_list if (c.kwargs.get("params") or {}).get("limit")]
    assert list_calls
    list_params = list_calls[0].kwargs.get("params") or {}
    assert list_params.get("limit") == 100
    assert list_params.get("offset") == 0


# -------------------------------------------------------------
# Workday variant: post_json list + externalPath detail
# -------------------------------------------------------------
def _workday_cfg():
    return next(c for c in load_portal_configs() if c.id == "workday")


_WORKDAY_BOARD = "https://acme.wd5.myworkdayjobs.com/en-US/AcmeExternal"


def _workday_fake_post(list_payload):
    def _fake_post(url, headers=None, timeout=None, json=None):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = list_payload
        return resp

    return _fake_post


def _workday_fake_get(detail_payload):
    def _fake_get(url, headers=None, timeout=None):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = detail_payload
        return resp

    return _fake_get


@patch("backend.ats.engine.requests.get")
@patch("backend.ats.engine.requests.post")
def test_workday_list_maps_fields_and_detail(mock_post, mock_get):
    """Workday POSTs a JSON body and maps title/locationsText/externalPath; detail fills desc."""
    list_payload = {
        "total": 1,
        "jobPostings": [
            {
                "title": "Staff Platform Engineer",
                "externalPath": "/job/Bengaluru/Staff-Platform-Engineer_JR123",
                "locationsText": "2 Locations",
                "bulletFields": ["JR123"],
            }
        ],
    }
    detail_payload = {
        "jobPostingInfo": {
            "title": "Staff Platform Engineer",
            "location": "Bengaluru, India",
            "jobDescription": "<p>Own the platform at scale.</p>",
        }
    }
    mock_post.side_effect = _workday_fake_post(list_payload)
    mock_get.side_effect = _workday_fake_get(detail_payload)

    jobs = run_portal(_workday_cfg(), company_name="Acme", careers_url=_WORKDAY_BOARD)
    assert len(jobs) == 1
    j = jobs[0]
    assert j["title"] == "Staff Platform Engineer"
    # Host-relative externalPath is prefixed with the board host.
    assert j["url"] == (
        "https://acme.wd5.myworkdayjobs.com/job/Bengaluru/Staff-Platform-Engineer_JR123"
    )
    assert "platform at scale" in j["description"]
    assert "<" not in j["description"]
    assert j["source"] == "Workday Portal"
    assert j["source_type"] == "Direct"

    # The list call is a POST carrying the paginated JSON body.
    body = mock_post.call_args_list[0].kwargs.get("json") or {}
    assert body.get("limit") == 20
    assert body.get("offset") == 0


@patch("backend.ats.engine.requests.post")
def test_workday_without_board_url_is_skipped(mock_post):
    """A non-Workday careers_url yields no board context → no request at all."""
    jobs = run_portal(_workday_cfg(), company_name="Acme", careers_url="https://acme.com/careers")
    assert jobs == []
    mock_post.assert_not_called()


@patch("backend.ats.engine.requests.post")
def test_workday_applies_title_filter(mock_post):
    list_payload = {
        "total": 2,
        "jobPostings": [
            {"title": "Backend Engineer", "externalPath": "/job/x/Backend_JR1", "locationsText": "India"},
            {"title": "Product Designer", "externalPath": "/job/y/Designer_JR2", "locationsText": "India"},
        ],
    }
    mock_post.side_effect = _workday_fake_post(list_payload)
    jobs = run_portal(
        _workday_cfg(),
        company_name="Acme",
        careers_url=_WORKDAY_BOARD,
        title_filters=["backend engineer"],
    )
    assert [j["title"] for j in jobs] == ["Backend Engineer"]


def test_workday_map_item_uses_passed_ctx_not_instance_state():
    """Regression: map_item must not depend on per-request state stashed on the shared singleton.

    Interleaving two boards must not let one board's host leak into the other's URLs.
    """
    from backend.ats.engine import WorkdayScraper

    scraper = WorkdayScraper()
    cfg = _workday_cfg()
    item = {"title": "Engineer", "externalPath": "/job/X/Eng_JR1", "locationsText": "India"}
    ctx_a = {"host": "a.wd1.myworkdayjobs.com", "site": "SiteA"}
    ctx_b = {"host": "b.wd5.myworkdayjobs.com", "site": "SiteB"}

    url_a = scraper.map_item(cfg, item, ctx_a)["url"]
    url_b = scraper.map_item(cfg, item, ctx_b)["url"]
    # Re-map with A again: B's context must not have persisted on the instance.
    url_a2 = scraper.map_item(cfg, item, ctx_a)["url"]

    assert "a.wd1.myworkdayjobs.com" in url_a
    assert "b.wd5.myworkdayjobs.com" in url_b
    assert url_a == url_a2
    assert not hasattr(scraper, "_ctx")


# -------------------------------------------------------------
# Permission gate reporting
# -------------------------------------------------------------
def test_blocked_host_is_reported_once_per_run(monkeypatch, caplog):
    """A robots-blocked host must not emit one INFO line per company in a run."""
    import logging as _logging

    from backend.ats.engine import _BLOCKED_HOSTS_REPORTED, _http, clear_active_pacer

    monkeypatch.setattr("backend.scraper.may_fetch", lambda url, *a, **k: False)
    _BLOCKED_HOSTS_REPORTED.clear()
    cfg = PortalConfig(id="smartrecruiters", label="SmartRecruiters", shape="paginated_json", list_url="")

    with caplog.at_level(_logging.INFO, logger="scraper.ats"):
        for slug in ("stripe", "gitlab", "openai"):
            assert _http("GET", f"https://api.smartrecruiters.com/v1/companies/{slug}/postings", cfg=cfg) is None

    infos = [r for r in caplog.records if r.levelno == _logging.INFO]
    assert len(infos) == 1
    assert "api.smartrecruiters.com" in infos[0].getMessage()

    # A new run reports the host again.
    caplog.clear()
    clear_active_pacer()
    with caplog.at_level(_logging.INFO, logger="scraper.ats"):
        _http("GET", "https://api.smartrecruiters.com/v1/companies/stripe/postings", cfg=cfg)
    assert len([r for r in caplog.records if r.levelno == _logging.INFO]) == 1
