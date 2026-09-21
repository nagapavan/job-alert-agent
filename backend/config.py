import os
import json
import shutil
import secrets
from pathlib import Path
from cryptography.fernet import Fernet

BASE_DIR = Path(__file__).resolve().parent.parent

# -------------------------------------------------------------
# Encryption Setup (Fernet)
# - Reads secret key from env var or generates/persists to .key file
# - Encrypts sensitive data (resumes, Q&A memory, notes) at rest
# -------------------------------------------------------------
KEY_FILE = BASE_DIR / ".key"
API_KEY_FILE = BASE_DIR / ".api_key"

def load_or_create_key() -> bytes:
    """Loads existing key or generates a new one securely."""
    env_key = os.environ.get("JOB_ALERT_AGENT_SECRET_KEY")
    if env_key:
        return env_key.encode()
    
    if KEY_FILE.exists():
        try:
            key_data = KEY_FILE.read_bytes().strip()
            # Validate Fernet key format (must be 32 urlsafe base64 bytes)
            Fernet(key_data)
            return key_data
        except Exception:
            pass
    
    new_key = Fernet.generate_key()
    KEY_FILE.write_bytes(new_key)
    # Restrict permissions on key file on Unix systems (chmod 600)
    try:
        os.chmod(KEY_FILE, 0o600)
    except Exception:
        pass
    return new_key

ENCRYPTION_KEY = load_or_create_key()
cipher = Fernet(ENCRYPTION_KEY)

def encrypt_data(data: str) -> str:
    """Encrypts a plaintext string and returns a base64 encoded string."""
    if not data:
        return ""
    return cipher.encrypt(data.encode("utf-8")).decode("utf-8")

def decrypt_data(token: str) -> str:
    """Decrypts a base64 encoded token and returns the plaintext string."""
    if not token:
        return ""
    try:
        return cipher.decrypt(token.encode("utf-8")).decode("utf-8")
    except Exception:
        # If not encrypted (e.g. legacy plaintext), return as-is for backward compatibility
        return token

# -------------------------------------------------------------
# API Key & Authentication Setup
# - Reads API key from env var or generates/persists to .api_key file
# - Protects all sensitive backend endpoints from unauthorized access
# -------------------------------------------------------------
def load_or_create_api_key() -> str:
    """Loads existing API key or generates a new cryptographically secure one."""
    env_key = os.environ.get("JOB_ALERT_AGENT_API_KEY")
    if env_key and env_key.strip():
        return env_key.strip()
    
    if API_KEY_FILE.exists():
        try:
            key_val = API_KEY_FILE.read_text("utf-8").strip()
            if len(key_val) >= 16:
                return key_val
        except Exception:
            pass
    
    new_api_key = secrets.token_urlsafe(32)
    API_KEY_FILE.write_text(new_api_key, encoding="utf-8")
    try:
        os.chmod(API_KEY_FILE, 0o600)
    except Exception:
        pass
    return new_api_key

API_KEY = load_or_create_api_key()

def is_valid_api_key(provided_key: str) -> bool:
    """Constant-time comparison against the active system API key."""
    if not provided_key or not API_KEY:
        return False
    return secrets.compare_digest(provided_key.strip(), API_KEY.strip())

# -------------------------------------------------------------
# Database Configuration
# - PostgreSQL with pgvector support
# - SQLite fallback if PostgreSQL is not reachable
# -------------------------------------------------------------
POSTGRES_USER = os.environ.get("DB_USER", "postgres")
POSTGRES_PASSWORD = os.environ.get("DB_PASSWORD", "postgres")
POSTGRES_HOST = os.environ.get("DB_HOST", "localhost")
POSTGRES_PORT = os.environ.get("DB_PORT", "5432")
POSTGRES_DB = os.environ.get("DB_NAME", "job_alert_agent")

DATABASE_URL = os.environ.get(
    "DATABASE_URL", 
    f"postgresql://{POSTGRES_USER}:{POSTGRES_PASSWORD}@{POSTGRES_HOST}:{POSTGRES_PORT}/{POSTGRES_DB}"
)
SQLITE_URL = f"sqlite:///{BASE_DIR}/job_alert_agent.db"

# -------------------------------------------------------------
# AI / LLM Configurations (Local LM Studio / Unsloth / Ollama & Cloud Fallbacks)
# -------------------------------------------------------------
# Default provider priority: "auto", "lmstudio", "unsloth", "ollama", "openai", "gemini", "anthropic"
DEFAULT_LLM_PROVIDER = os.environ.get("DEFAULT_LLM_PROVIDER", "auto")
LLM_TIMEOUT = int(os.environ.get("LLM_TIMEOUT", "120"))

# LM Studio Local Server (OpenAI-compatible inference server)
LM_STUDIO_BASE_URL = os.environ.get("LM_STUDIO_BASE_URL", "http://localhost:1234/v1")
LM_STUDIO_MODEL = os.environ.get("LM_STUDIO_MODEL", "local-model")

# Optional per-tier LM Studio models. When set, the dynamic model router will request a
# different model id per task complexity tier. Requires LM Studio "Just-in-time model
# loading" to be enabled; otherwise requests for an unloaded model will fail and the
# client falls back to LM_STUDIO_MODEL.
LM_STUDIO_FAST_MODEL = os.environ.get("LM_STUDIO_FAST_MODEL", "")
LM_STUDIO_STANDARD_MODEL = os.environ.get("LM_STUDIO_STANDARD_MODEL", "")
LM_STUDIO_DEEP_MODEL = os.environ.get("LM_STUDIO_DEEP_MODEL", "")

# Unsloth Local Server (OpenAI-compatible inference server)
UNSLOTH_BASE_URL = os.environ.get("UNSLOTH_BASE_URL", "http://localhost:8008/v1")
UNSLOTH_MODEL = os.environ.get("UNSLOTH_MODEL", "unsloth/Llama-3-8B-Instruct")

# Ollama Local Server
OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3")

# Cloud API Keys & Policy (Strictly Opt-In; Default False to preserve local-first privacy)
ALLOW_CLOUD_FALLBACK = os.environ.get("ALLOW_CLOUD_FALLBACK", "false").lower() in ("true", "1", "yes")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# -------------------------------------------------------------
# Runtime LLM Settings (BYOK via UI) — persisted, encrypted at rest.
# Environment variables always take precedence over persisted settings.
# -------------------------------------------------------------
LLM_SETTINGS_FILE = BASE_DIR / "data" / "llm_settings.json"


def _bool(value) -> bool:
    return str(value).strip().lower() in ("true", "1", "yes", "on")


def _read_llm_settings() -> dict:
    """Loads persisted LLM settings, decrypting stored API keys."""
    defaults = {"openai_api_key": "", "gemini_api_key": "", "anthropic_api_key": "", "allow_cloud_fallback": False}
    if not LLM_SETTINGS_FILE.exists():
        return defaults
    try:
        raw = json.loads(LLM_SETTINGS_FILE.read_text("utf-8"))
        settings = dict(defaults)
        for key in ("openai_api_key", "gemini_api_key", "anthropic_api_key"):
            token = raw.get(key) or ""
            settings[key] = decrypt_data(token) if token else ""
        settings["allow_cloud_fallback"] = _bool(raw.get("allow_cloud_fallback", False))
        return settings
    except Exception:
        return defaults


def get_llm_credentials() -> dict:
    """Returns effective cloud credentials: environment overrides persisted settings."""
    stored = _read_llm_settings()
    return {
        "openai": OPENAI_API_KEY or stored["openai_api_key"],
        "gemini": GEMINI_API_KEY or stored["gemini_api_key"],
        "anthropic": ANTHROPIC_API_KEY or stored["anthropic_api_key"],
    }


def is_cloud_fallback_allowed() -> bool:
    """True when cloud fallback is enabled via env or persisted UI setting."""
    if ALLOW_CLOUD_FALLBACK:
        return True
    return bool(_read_llm_settings().get("allow_cloud_fallback", False))


# -------------------------------------------------------------
# Gmail API (BYOK OAuth) Settings — persisted, encrypted at rest.
# -------------------------------------------------------------
GMAIL_SETTINGS_FILE = BASE_DIR / "data" / "gmail_settings.json"

_GMAIL_DEFAULTS = {
    "client_id": "", "client_secret": "", "refresh_token": "",
    "token_email": "", "history_id": "", "connected_at": "", "code_verifier": "",
}


def _read_gmail_settings() -> dict:
    settings = dict(_GMAIL_DEFAULTS)
    if not GMAIL_SETTINGS_FILE.exists():
        return settings
    try:
        raw = json.loads(GMAIL_SETTINGS_FILE.read_text("utf-8"))
        for key in ("client_id", "token_email", "history_id", "connected_at"):
            settings[key] = raw.get(key) or ""
        for key in ("client_secret", "refresh_token", "code_verifier"):
            token = raw.get(key) or ""
            settings[key] = decrypt_data(token) if token else ""
    except Exception:
        pass
    return settings


def get_gmail_settings() -> dict:
    """Returns decrypted Gmail settings (client id/secret, refresh token, history id)."""
    return _read_gmail_settings()


def is_gmail_configured() -> bool:
    s = _read_gmail_settings()
    return bool(s["client_id"] and s["client_secret"] and s["refresh_token"])


def save_gmail_settings(client_id=None, client_secret=None, refresh_token=None,
                        token_email=None, history_id=None, connected_at=None,
                        code_verifier=None) -> dict:
    """Persists Gmail settings, encrypting sensitive fields. None leaves a value unchanged."""
    current = _read_gmail_settings()
    for key, value in (
        ("client_id", client_id), ("client_secret", client_secret),
        ("refresh_token", refresh_token), ("token_email", token_email),
        ("history_id", history_id), ("connected_at", connected_at),
        ("code_verifier", code_verifier),
    ):
        if value is not None:
            current[key] = value

    payload = {
        "client_id": current["client_id"],
        "client_secret": encrypt_data(current["client_secret"]) if current["client_secret"] else "",
        "refresh_token": encrypt_data(current["refresh_token"]) if current["refresh_token"] else "",
        "token_email": current["token_email"],
        "history_id": current["history_id"],
        "connected_at": current["connected_at"],
        "code_verifier": encrypt_data(current["code_verifier"]) if current["code_verifier"] else "",
    }
    GMAIL_SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    GMAIL_SETTINGS_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    try:
        os.chmod(GMAIL_SETTINGS_FILE, 0o600)
    except Exception:
        pass
    return current


def clear_gmail_settings() -> None:
    """Removes persisted Gmail credentials/token."""
    try:
        if GMAIL_SETTINGS_FILE.exists():
            GMAIL_SETTINGS_FILE.unlink()
    except Exception:
        pass


def save_llm_settings(openai_api_key=None, gemini_api_key=None, anthropic_api_key=None, allow_cloud_fallback=None) -> dict:
    """
    Persists LLM settings. API keys are encrypted at rest with the local Fernet key.
    Passing None leaves a value unchanged; passing an empty string clears it.
    """
    current = _read_llm_settings()
    if openai_api_key is not None:
        current["openai_api_key"] = openai_api_key
    if gemini_api_key is not None:
        current["gemini_api_key"] = gemini_api_key
    if anthropic_api_key is not None:
        current["anthropic_api_key"] = anthropic_api_key
    if allow_cloud_fallback is not None:
        current["allow_cloud_fallback"] = _bool(allow_cloud_fallback)

    payload = {
        "openai_api_key": encrypt_data(current["openai_api_key"]) if current["openai_api_key"] else "",
        "gemini_api_key": encrypt_data(current["gemini_api_key"]) if current["gemini_api_key"] else "",
        "anthropic_api_key": encrypt_data(current["anthropic_api_key"]) if current["anthropic_api_key"] else "",
        "allow_cloud_fallback": bool(current["allow_cloud_fallback"]),
    }
    LLM_SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    LLM_SETTINGS_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    try:
        os.chmod(LLM_SETTINGS_FILE, 0o600)
    except Exception:
        pass
    return current

# Dynamic Model Routing
ENABLE_DYNAMIC_MODEL_ROUTING = os.environ.get("ENABLE_DYNAMIC_MODEL_ROUTING", "true").lower() in ("true", "1", "yes")
FAST_TIER_MODEL = os.environ.get("FAST_TIER_MODEL", "qwen2.5:3b")
STANDARD_TIER_MODEL = os.environ.get("STANDARD_TIER_MODEL", "llama3:8b")
DEEP_TIER_MODEL = os.environ.get("DEEP_TIER_MODEL", "llama3.3:70b")

# LinkedIn automation is opt-in and rate-limited. Conservative inter-action pause (seconds)
# to reduce load/risk on the user's LinkedIn session.
LINKEDIN_AUTOMATION_PACING_SECONDS = float(os.environ.get("LINKEDIN_AUTOMATION_PACING_SECONDS", "2.5"))

# Self-identifying User-Agent for HTML/index scraping (custom careers pages, Google for Jobs).
# Public ATS JSON APIs keep the standard browser UA; identifying crawlers is the compliance-
# relevant part (e.g. Amazon's Agent Policy requires agents to self-identify).
AGENT_USER_AGENT = os.environ.get(
    "JOB_ALERT_AGENT_USER_AGENT",
    "JobAlertAgent/1.0 (+personal job-search assistant; respects robots.txt)",
)

# Respect robots.txt for HTML/index scraping. Fail-open on network errors. Set to "false" only
# if you understand and accept the ToS implications.
RESPECT_ROBOTS = os.environ.get("JOB_ALERT_AGENT_RESPECT_ROBOTS", "true").lower() in ("true", "1", "yes")

# Concurrency & Pacing Limits (Local single-worker vs Cloud parallel dispatch)
LLM_LOCAL_MAX_CONCURRENCY = int(os.environ.get("LLM_LOCAL_MAX_CONCURRENCY", "1"))
LLM_CLOUD_MAX_CONCURRENCY = int(os.environ.get("LLM_CLOUD_MAX_CONCURRENCY", "8"))
LLM_LOCAL_PACING_SECONDS = float(os.environ.get("LLM_LOCAL_PACING_SECONDS", "0.2"))
LLM_CLOUD_PACING_SECONDS = float(os.environ.get("LLM_CLOUD_PACING_SECONDS", "0.0"))

# -------------------------------------------------------------
# Dedicated Chrome Profile Configuration
# - Path to the isolated Chrome profile for Playwright session persistence.
# - Defaults to a stable per-user location so logins survive repo reinstall/moves.
# -------------------------------------------------------------
LEGACY_CHROME_PROFILE_DIR = BASE_DIR / ".chrome_profile"
CHROME_PROFILE_DIR = Path(os.environ.get(
    "CHROME_PROFILE_DIR",
    str(Path.home() / ".job-alert-agent" / "chrome_profile")
))


def _migrate_legacy_chrome_profile():
    """
    One-time migration: move an existing in-repo .chrome_profile to the stable
    per-user location so persisted LinkedIn/portal logins are not lost.
    """
    try:
        if os.environ.get("CHROME_PROFILE_DIR"):
            return
        if CHROME_PROFILE_DIR.exists():
            return
        if LEGACY_CHROME_PROFILE_DIR.exists():
            CHROME_PROFILE_DIR.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(LEGACY_CHROME_PROFILE_DIR), str(CHROME_PROFILE_DIR))
    except Exception:
        pass


_migrate_legacy_chrome_profile()

try:
    CHROME_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
except Exception:
    pass

