import os
import sys
import tempfile
import warnings
from pathlib import Path
import pytest

# Importing backend.main no longer touches the database: observability, init_db() and the
# scheduler now run on the ASGI startup lifecycle, so collection stays side-effect free. The
# NO_SCHEDULER guard still matters for every TestClient that starts the app. DATABASE_URL is set
# here so that startup handler opens a throwaway file instead of the operator's live database.
os.environ.setdefault("JOB_ALERT_AGENT_NO_SCHEDULER", "1")
os.environ.setdefault(
    "DATABASE_URL", f"sqlite:///{Path(tempfile.mkdtemp(prefix='jaa-test-')) / 'import-time.db'}"
)

# Suppress upstream Starlette/FastAPI TestClient httpx deprecation warning
warnings.filterwarnings(
    "ignore", message=".*Using `httpx` with `starlette.testclient`.*"
)
warnings.filterwarnings(
    "ignore", category=DeprecationWarning, module="fastapi.testclient"
)

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# Add project root to sys.path
root_dir = Path(__file__).resolve().parent
if str(root_dir) not in sys.path:
    sys.path.insert(0, str(root_dir))

from unittest.mock import MagicMock, patch
from backend.database import Base, get_db
from backend.main import app

# Shared In-Memory Test Database
TEST_DATABASE_URL = "sqlite:///:memory:"
test_engine = create_engine(
    TEST_DATABASE_URL, connect_args={"check_same_thread": False}, poolclass=StaticPool
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)


@pytest.fixture(autouse=True)
def setup_test_db():
    """Recreates tables cleanly before each test."""
    Base.metadata.create_all(bind=test_engine)
    yield
    Base.metadata.drop_all(bind=test_engine)


@pytest.fixture(autouse=True)
def isolate_token_tracker_stats(tmp_path, monkeypatch):
    """
    Isolates TokenTracker STATS_FILE to a temporary test directory
    to prevent test runs from resetting or modifying live workspace token stats.
    """
    test_stats_file = tmp_path / "test_token_stats.json"
    monkeypatch.setattr("backend.ai_helper.STATS_FILE", test_stats_file)
    from backend.ai_helper import token_tracker

    with token_tracker._lock:
        token_tracker._prompt_tokens = 0
        token_tracker._completion_tokens = 0
        token_tracker._call_count = 0
        token_tracker._last_aggregated_log_id = 0
    yield


@pytest.fixture(autouse=True)
def isolate_llm_settings_file(tmp_path, monkeypatch):
    """
    Isolates persisted LLM/BYOK settings to a temporary file so tests never read or
    write the real workspace settings.
    """
    monkeypatch.setattr(
        "backend.config.LLM_SETTINGS_FILE", tmp_path / "llm_settings.json"
    )
    monkeypatch.setattr(
        "backend.config.GMAIL_SETTINGS_FILE", tmp_path / "gmail_settings.json"
    )
    monkeypatch.setattr(
        "backend.config.EMBEDDING_SETTINGS_FILE", tmp_path / "embedding_settings.json"
    )
    # Never let the standalone ONNX backend attempt a network download during tests.
    monkeypatch.setattr("backend.config._ENV_EMBEDDING_ALLOW_DOWNLOAD", "false")
    monkeypatch.setattr("backend.config._ENV_EMBEDDING_BACKEND", None)
    monkeypatch.setattr("backend.config._ENV_EMBEDDING_MODEL", None)
    monkeypatch.setattr("backend.config._ENV_EMBEDDING_SERVER_URL", None)
    monkeypatch.setattr("backend.config._ENV_EMBEDDING_SERVER_MODEL", None)
    yield


@pytest.fixture(autouse=True)
def isolate_google_jobs_state(tmp_path, monkeypatch):
    """
    Keeps the Google Jobs cooldown/rotation state out of the workspace so a test that
    simulates a block cannot put the real instance into a week-long cooldown.
    """
    monkeypatch.setattr(
        "backend.google_jobs_state.STATE_PATH", tmp_path / "google_jobs_state.json"
    )
    yield


@pytest.fixture(autouse=True)
def isolate_database_session_local(monkeypatch):
    """
    Points the module-level SessionLocal at the in-memory test database.

    Background components (the scheduler's feature/consent gate, log pruning) open their own
    sessions instead of taking the ``get_db`` dependency, so without this they read the live
    workspace database — which made scheduler tests depend on the operator's real feature flags.
    """
    monkeypatch.setattr("backend.database.SessionLocal", TestingSessionLocal)
    yield


@pytest.fixture(autouse=True)
def neutralize_robots(monkeypatch):
    """
    Keeps tests hermetic and offline: robots.txt fetches return empty (allow-all) and the
    robots cache is reset, so HTML-scraper tests never make extra network calls.
    """
    monkeypatch.setattr("backend.scraper._fetch_robots", lambda url, timeout=5: "")
    monkeypatch.setattr("backend.scraper._robots_cache", {}, raising=False)
    yield


def _refuse_browser_spawn(*_args, **_kwargs):
    raise RuntimeError(
        "A test tried to launch a real browser. Mock the module the caller imports "
        "(e.g. patch('backend.routers.google_jobs.sync_playwright')) - a patch on a module "
        "that no longer owns the name lets the real browser run and hits the live site."
    )


@pytest.fixture(autouse=True)
def prevent_real_browser_spawns(tmp_path, monkeypatch):
    """
    Globally mocks Playwright during test execution to prevent real Chromium/Node
    processes from spawning and crashing when pytest exits.

    Also isolates the persistent Chrome profile directory to a per-test temp path so the
    suite is hermetic even when a real browser currently holds the live profile lock
    (otherwise get_persistent_browser_context raises BrowserProfileBusy and spoils tests).

    The launch guard is on ``BrowserType`` itself, not on one module's imported name: every
    module that does ``from playwright.sync_api import sync_playwright`` binds its own reference
    at import time, so patching a single module's name cannot protect the others. A stale
    ``patch("backend.main.sync_playwright")`` in a test whose code moved to a router used to let
    the real browser run and hit Google for Jobs (2026-11-18: seven live fetches, interstitial).
    """
    monkeypatch.setattr(
        "backend.playwright_app.CHROME_PROFILE_DIR", tmp_path / "chrome_profile"
    )
    for _target in (
        "playwright.sync_api.BrowserType.launch",
        "playwright.sync_api.BrowserType.launch_persistent_context",
    ):
        monkeypatch.setattr(_target, _refuse_browser_spawn)

    with patch("backend.main.sync_playwright", MagicMock()):
        yield


@pytest.fixture(autouse=True)
def stub_discovery_llm_scoring(monkeypatch):
    """
    Keep the suite hermetic: discovery ingestion (ATS/Google/LinkedIn/Hirist) scores jobs through
    the LLM-backed requirement engine. Stub it to an empty assessment so tests never call a real
    model. Tests that exercise scoring/endpoints patch ``assess_job_requirements`` themselves.

    Patched in every module that calls it: ``backend.job_scoring`` holds its own reference bound at
    import time and is what the ingestion paths reach, so patching ``backend.main`` alone was
    vacuous once the scoring pipeline moved out (2026-11-18).
    """
    from backend import job_scoring, main
    from backend.match_scoring import RequirementAssessment, compute_match

    def _empty(*args, **kwargs):
        assessment = RequirementAssessment(requirements=[])
        return assessment, compute_match(assessment)

    for module in (job_scoring, main):
        if hasattr(module, "assess_job_requirements"):
            monkeypatch.setattr(module, "assess_job_requirements", _empty)


def route_paths(app) -> list:
    """Flatten ``app.routes`` into a path list, included routers included.

    This FastAPI version keeps ``include_router`` as a single ``_IncludedRouter`` entry, so a plain
    ``getattr(route, "path")`` sweep silently misses every route that was extracted into a router.
    """
    paths = []
    for route in app.routes:
        included = getattr(route, "original_router", None)
        if included is not None:
            paths.extend(getattr(r, "path", None) for r in included.routes)
        else:
            paths.append(getattr(route, "path", None))
    return paths


def override_get_db():
    """Dependency override for FastAPI app testing (not a pytest fixture)."""
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def db_session():
    """Provides a fresh isolated database session for testing."""
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


app.dependency_overrides[get_db] = override_get_db
test_client = TestClient(app)
