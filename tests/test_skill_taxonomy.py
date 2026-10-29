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
        """Perks-prose surfaces must not alias the HR skill."""
        assert tax.normalize_skill("benefits") == "benefits"
        assert tax.normalize_skill("employee benefits") == "employee benefits"
        assert tax.normalize_skill("benefits systems") == "benefits systems"
        assert tax.normalize_skill("benefits administration") == "benefits administration"

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

    def test_rejects_html_markup(self, monkeypatch, tmp_path):
        """Raw JD HTML must never reach the review queue (issue #8)."""
        monkeypatch.setattr(tax, "CANDIDATES_PATH", tmp_path / "candidates.json")
        for phrase in ("<ul>", "<li>solve real business needs</li>", "<p>p-1385</p>", "&nbsp;"):
            tax.record_skill_candidate(phrase)
        assert not (tmp_path / "candidates.json").exists()

    def test_rejects_section_headings(self, monkeypatch, tmp_path):
        """Headings are structure, not candidate skills — reuse section_intent's vocabulary."""
        monkeypatch.setattr(tax, "CANDIDATES_PATH", tmp_path / "candidates.json")
        for phrase in ("about the job", "minimum qualifications", "what we offer", "requirements"):
            tax.record_skill_candidate(phrase)
        assert not (tmp_path / "candidates.json").exists()

    def test_rejects_prose_sentences(self, monkeypatch, tmp_path):
        """JD prose is not a skill phrase."""
        monkeypatch.setattr(tax, "CANDIDATES_PATH", tmp_path / "candidates.json")
        tax.record_skill_candidate("we are a small team that values curiosity")
        tax.record_skill_candidate("join our mission to do great things")
        assert not (tmp_path / "candidates.json").exists()

    def test_accepts_real_skill_phrases(self, monkeypatch, tmp_path):
        """Positive control: genuine candidates still record, so the filter is not over-broad."""
        monkeypatch.setattr(tax, "CANDIDATES_PATH", tmp_path / "candidates.json")
        for phrase in ("stakeholder wrangling", "event-driven architecture", "terraform"):
            tax.record_skill_candidate(phrase)
        counts = json.loads((tmp_path / "candidates.json").read_text())["counts"]
        assert set(counts) == {"stakeholder wrangling", "event-driven architecture", "terraform"}

    def test_purge_removes_existing_pollution(self, monkeypatch, tmp_path):
        """purge_candidates drops historical noise while preserving real candidates + counts."""
        path = tmp_path / "candidates.json"
        path.write_text(
            json.dumps({
                "version": 1,
                "counts": {
                    "<ul>": 12, "about the job": 9,
                    "we are a small team that values curiosity": 4,
                    "stakeholder wrangling": 7, "terraform": 3,
                },
            })
        )
        monkeypatch.setattr(tax, "CANDIDATES_PATH", path)
        removed, kept = tax.purge_candidates()
        assert removed == 3 and kept == 2
        counts = json.loads(path.read_text())["counts"]
        assert counts == {"stakeholder wrangling": 7, "terraform": 3}
