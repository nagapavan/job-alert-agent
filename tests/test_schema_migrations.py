"""Schema auto-migration and hot-path indexes (findings 8 / 9).

The migration helper runs at startup on every existing database, so a failure is only visible
through its log line. These tests pin the two properties that make it safe: a failing table cannot
stop the others, and the indexes the query paths need actually get created.
"""

import datetime
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text

from backend.database import Base, Company, Job, _auto_migrate
from backend.main import app

client = TestClient(app)


class _FailingInspector:
    """Delegates to the real inspector but blows up on one table's column list."""

    def __init__(self, inner, failing_table):
        self._inner = inner
        self._failing = failing_table

    def get_table_names(self):
        return self._inner.get_table_names()

    def get_columns(self, table_name):
        if table_name == self._failing:
            raise RuntimeError(f"cannot read columns of {table_name}")
        return self._inner.get_columns(table_name)


def test_job_hot_path_columns_are_indexed(tmp_path):
    """url (dedup), created_at (every list ORDER BY) and company_id are indexed (finding 9)."""
    engine = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}")
    Base.metadata.create_all(bind=engine)

    names = {ix["name"] for ix in inspect(engine).get_indexes("jobs")}

    assert {"ix_jobs_url", "ix_jobs_created_at", "ix_jobs_company_id"} <= names


def test_auto_migration_adds_the_same_indexes_to_an_existing_table(tmp_path):
    """create_all cannot add an index to a table that already exists, so the migration must."""
    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    Job.__table__.create(bind=engine)
    with engine.begin() as conn:
        for name in ("ix_jobs_url", "ix_jobs_created_at", "ix_jobs_company_id"):
            conn.execute(text(f"DROP INDEX {name};"))
    assert not (
        {"ix_jobs_url", "ix_jobs_created_at", "ix_jobs_company_id"}
        & {ix["name"] for ix in inspect(engine).get_indexes("jobs")}
    )

    _auto_migrate(engine)

    names = {ix["name"] for ix in inspect(engine).get_indexes("jobs")}
    assert {"ix_jobs_url", "ix_jobs_created_at", "ix_jobs_company_id"} <= names


def test_one_failing_table_does_not_skip_the_rest(tmp_path, caplog):
    """A broken table logs an ERROR naming it and the later tables still migrate (finding 8)."""
    engine = create_engine(f"sqlite:///{tmp_path / 'partial.db'}")
    with engine.begin() as conn:
        conn.execute(
            text("CREATE TABLE operation_logs (id INTEGER PRIMARY KEY, created_at DATETIME);")
        )
        conn.execute(text("CREATE TABLE companies (id INTEGER PRIMARY KEY);"))

    real_inspect = inspect
    with patch(
        "sqlalchemy.inspect",
        lambda eng: _FailingInspector(real_inspect(eng), "operation_logs"),
    ), caplog.at_level("ERROR"):
        _auto_migrate(engine)

    errors = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    assert any("operation_logs" in message for message in errors)

    # The table after the failing one still received its columns.
    columns = {c["name"] for c in real_inspect(engine).get_columns("companies")}
    assert {"market_position", "talking_points"} <= columns


def test_list_jobs_is_bounded_by_limit(db_session):
    """/api/jobs must not serialise every row the table has ever held (finding 9)."""
    company = Company(name="Acme")
    db_session.add(company)
    db_session.flush()
    base = datetime.datetime(2026, 1, 1)
    for index in range(5):
        db_session.add(
            Job(
                company_id=company.id,
                title=f"Engineer {index}",
                location="Remote",
                url=f"https://acme.example/jobs/{index}",
                created_at=base + datetime.timedelta(minutes=index),
            )
        )
    db_session.commit()

    res = client.get("/api/jobs", params={"limit": 2})

    assert res.status_code == 200
    assert len(res.json()) == 2
