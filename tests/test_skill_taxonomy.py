"""Tests for the data-driven skill taxonomy + candidate review queue (Phase 3)."""
import json

from backend import skill_taxonomy as tax


class TestTaxonomyData:
    def test_loads_extended_vocabulary(self):
        names = set(tax.DISPLAY_SKILLS)
        for expected in ("LLM", "NLU", "Prompt Engineering", "RAG", "Embeddings", "Agentic AI"):
            assert expected in names

    def test_normalize_aliases(self):
        assert tax.normalize_skill("genai") == "llm"
        assert tax.normalize_skill("Generative AI") == "llm"
        assert tax.normalize_skill("golang") == "go"
        assert tax.normalize_skill("nodejs") == "node.js"
        assert tax.normalize_skill("Postgres") == "postgresql"

    def test_over_generic_alias_is_ignored(self):
        """Bare "benefits" must not alias the HR skill (it matched perks prose in any JD)."""
        assert tax.normalize_skill("benefits") == "benefits"
        assert tax.normalize_skill("employee benefits") == "benefits administration"

    def test_alias_surfaces(self):
        assert "nodejs" in tax.alias_surfaces("node.js")
        assert "genai" in tax.alias_surfaces("llm")

    def test_concepts(self):
        assert "node.js" in tax.concepts_for("javascript")
        assert "nlp" in tax.concepts_for("machine learning")

    def test_cloud_providers(self):
        assert {"aws", "gcp", "azure"} <= tax.CLOUD_PROVIDERS


class TestCandidateQueue:
    def test_records_incrementing_counts(self, monkeypatch, tmp_path):
        monkeypatch.setattr(tax, "CANDIDATES_PATH", tmp_path / "candidates.json")
        tax.record_skill_candidate("stakeholder wrangling")
        tax.record_skill_candidate("Stakeholder   Wrangling")
        data = json.loads((tmp_path / "candidates.json").read_text())
        assert data["counts"]["stakeholder wrangling"] == 2

    def test_ignores_junk(self, monkeypatch, tmp_path):
        monkeypatch.setattr(tax, "CANDIDATES_PATH", tmp_path / "candidates.json")
        tax.record_skill_candidate("ab")
        tax.record_skill_candidate("12345")
        assert not (tmp_path / "candidates.json").exists()
