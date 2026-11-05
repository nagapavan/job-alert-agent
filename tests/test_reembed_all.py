"""Tests for scripts/reembed_all.py (issue #100)."""
import importlib.util
from pathlib import Path

import pytest

from backend.database import Company, Job

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "reembed_all.py"


@pytest.fixture(scope="module")
def reembed():
    spec = importlib.util.spec_from_file_location("reembed_all_script", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _job(db, company_name, title, model):
    company = Company(name=company_name, domain=f"{company_name.lower()}.example")
    db.add(company)
    db.flush()
    job = Job(
        company_id=company.id,
        title=title,
        description="python services",
        embedding=[0.1] * 384,
        embedding_model=model,
        embedding_dim=384,
    )
    db.add(job)
    db.commit()
    return job


class TestReembedTable:
    def test_skips_rows_already_stamped_with_the_target_model(
        self, db_session, reembed, monkeypatch
    ):
        from backend import embeddings
        from backend.embeddings import HashEmbeddingBackend

        monkeypatch.setattr(
            embeddings, "resolve_backends", lambda settings=None: [HashEmbeddingBackend()]
        )
        current = _job(db_session, "Acme", "Engineer", "target-model")
        stale = _job(db_session, "Globex", "Analyst", "hash")
        before = list(current.embedding)

        stats = reembed.reembed_table(
            db_session, Job, reembed._job_text, "target-model", batch_size=10
        )

        assert stats["already"] == 1
        assert stats["pending"] == 1
        assert stats["reembedded"] == 1
        assert list(current.embedding) == before
        assert current.embedding_model == "target-model"
        assert stale.embedding_model == "target-model"

    def test_dry_run_writes_nothing(self, db_session, reembed):
        stale = _job(db_session, "Initech", "Developer", "hash")

        stats = reembed.reembed_table(
            db_session, Job, reembed._job_text, "new-model", dry_run=True
        )

        assert stats["pending"] == 1
        assert stats["reembedded"] == 0
        assert stale.embedding_model == "hash"

    def test_refuses_to_overwrite_vectors_with_hashes(self, reembed, monkeypatch):
        from backend import embeddings
        from backend.embeddings import HashEmbeddingBackend

        monkeypatch.setattr(
            embeddings, "resolve_backends", lambda settings=None: [HashEmbeddingBackend()]
        )

        with pytest.raises(SystemExit):
            reembed.resolve_target(allow_hash=False)
        assert reembed.resolve_target(allow_hash=True) == ("hash", "hash")
