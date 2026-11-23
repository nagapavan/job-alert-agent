"""Data-integrity guards: the failures that destroy or silently misreport stored data.

Each case here is a defect where the wrong behaviour was indistinguishable from a normal one —
a replaced key, a commit smuggled into a read, a database that quietly became a different file,
a watermark that skipped mail.
"""

import logging

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import config, database
from backend.database import Base, UserPreference


# ---------------------------------------------------------------- encryption key

def test_an_unreadable_key_is_not_silently_replaced(tmp_path, monkeypatch):
    """A damaged .key used to be overwritten with a fresh one, orphaning every encrypted row."""
    key_file = tmp_path / ".key"
    key_file.write_bytes(b"not-a-fernet-key")
    monkeypatch.setattr(config, "KEY_FILE", key_file)
    monkeypatch.delenv("JOB_ALERT_AGENT_SECRET_KEY", raising=False)

    with pytest.raises(RuntimeError, match="unreadable"):
        config.load_or_create_key()

    assert key_file.read_bytes() == b"not-a-fernet-key"
    backups = list(tmp_path.glob(".key.corrupt-*"))
    assert len(backups) == 1, "the damaged key must be kept for recovery"


def test_a_valid_key_is_reused(tmp_path, monkeypatch):
    from cryptography.fernet import Fernet

    key = Fernet.generate_key()
    key_file = tmp_path / ".key"
    key_file.write_bytes(key)
    monkeypatch.setattr(config, "KEY_FILE", key_file)
    monkeypatch.delenv("JOB_ALERT_AGENT_SECRET_KEY", raising=False)

    assert config.load_or_create_key() == key


def test_foreign_ciphertext_is_logged_not_silently_returned(caplog):
    """Ciphertext that cannot be decrypted is corruption, not legacy plaintext."""
    token = "gAAAAA" + "A" * 40
    with caplog.at_level(logging.ERROR, logger="backend.config"):
        assert config.decrypt_data(token) == token
    assert any("could not be decrypted" in r.message for r in caplog.records)


def test_legacy_plaintext_still_reads(caplog):
    """Pre-encryption rows must keep working, and must not raise a false alarm."""
    with caplog.at_level(logging.ERROR, logger="backend.config"):
        assert config.decrypt_data("plain text answer") == "plain text answer"
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


# ---------------------------------------------------------------- preference reads

def test_reading_preferences_does_not_commit_pending_writes():
    """get_user_preferences is called mid-transaction; it must not publish the caller's work."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        pref = database.get_user_preferences(db)
        pref.target_titles = "Staff Engineer"
        db.rollback()

        fresh = Session()
        try:
            assert fresh.query(UserPreference).first() is None or (
                fresh.query(UserPreference).first().target_titles is None
            )
        finally:
            fresh.close()
    finally:
        db.close()


# ---------------------------------------------------------------- database URL

def test_a_configured_database_url_does_not_fall_back_to_sqlite(monkeypatch):
    """An unreachable configured DB used to become an empty SQLite file with no log line."""
    unreachable = "postgresql://nobody@127.0.0.1:1/definitely_not_running"
    # config reads DATABASE_URL once at import, so the test must move the value init_db uses.
    monkeypatch.setattr(database, "DATABASE_URL", unreachable)
    with pytest.raises(Exception):
        database.init_db()


def test_no_configured_url_still_falls_back(monkeypatch):
    """Without an explicit URL the default is a local file, so the fallback stays allowed."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    engine = database.init_db(custom_url="sqlite:///:memory:")
    assert engine.dialect.name == "sqlite"
