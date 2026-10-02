import sys
import warnings
from pathlib import Path
import pytest

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


@pytest.fixture(autouse=True)
def prevent_real_browser_spawns(tmp_path, monkeypatch):
    """
    Globally mocks Playwright and BrowserSessionManager during test execution
    to prevent real Chromium/Node processes from spawning and crashing when pytest exits.

    Also isolates the persistent Chrome profile directory to a per-test temp path so the
    suite is hermetic even when a real browser currently holds the live profile lock
    (otherwise get_persistent_browser_context raises BrowserProfileBusy and spoils tests).
    """
    monkeypatch.setattr(
        "backend.playwright_app.CHROME_PROFILE_DIR", tmp_path / "chrome_profile"
    )

    mock_instance = MagicMock()
    mock_instance.open_job_tab = MagicMock(return_value=None)
    mock_instance._ensure_context = MagicMock(return_value=MagicMock())

    with (
        patch(
            "backend.playwright_app.BrowserSessionManager.get_instance",
            return_value=mock_instance,
        ),
        patch(
            "backend.playwright_app.launch_assisted_application_session",
            return_value=None,
        ),
        patch("backend.main.launch_assisted_application_session", return_value=None),
        patch("backend.main.sync_playwright", MagicMock()),
    ):
        yield


@pytest.fixture(autouse=True)
def stub_discovery_llm_scoring(monkeypatch):
    """
    Keep the suite hermetic: discovery ingestion (ATS/Google/LinkedIn/Hirist) scores jobs through
    the LLM-backed requirement engine. Stub it to an empty assessment so tests never call a real
    model. Tests that exercise scoring/endpoints patch ``assess_job_requirements`` themselves.
    """
    from backend import main
    from backend.match_scoring import RequirementAssessment, compute_match

    def _empty(*args, **kwargs):
        assessment = RequirementAssessment(requirements=[])
        return assessment, compute_match(assessment)

    if hasattr(main, "assess_job_requirements"):
        monkeypatch.setattr(main, "assess_job_requirements", _empty)


@pytest.fixture(autouse=True)
def override_get_db():
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
