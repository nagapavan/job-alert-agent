"""Tests for resume parse provenance and re-derivation (issue #54).

`parsed_json_encrypted` is a cache: it is written on upload and was never re-derived, so a
parser improvement (or a parse that dropped a role) kept feeding every score.
"""
import io
import json

import pytest

from backend.config import decrypt_data, encrypt_data
from backend.parser import RESUME_PARSER_VERSION, parse_open_source_resume

RESUME_TEXT = """Naga Test
Principal Software Engineer | Hyderabad
Email: naga@example.com

Technical Skill Set
- Java, Python, Go, Kubernetes

Professional Experience
Cisco – Principal Software Engineer                        July 2024 - July 2026
- Led platform architecture for observability.
- Drove migration to Kubernetes.

Oracle – Senior Member Technical Staff                     Jan 2011 - June 2014
- Built distributed storage services.

IBM – Application Developer                                Aug 2008 - Jan 2011
- Delivered internal tooling.

Education
B.Tech, Computer Science, 2008
"""


def _seed_resume(db_session, parsed=None, parser_version=None, text=RESUME_TEXT):
    from backend.database import Resume

    resume = Resume(
        filename="resume.md",
        content_encrypted=encrypt_data(text),
        parsed_json_encrypted=encrypt_data(json.dumps(parsed)) if parsed is not None else None,
        parser_version=parser_version,
        is_active=True,
    )
    db_session.add(resume)
    db_session.commit()
    db_session.refresh(resume)
    return resume


class TestParserCapturesEveryRole:
    def test_last_role_after_another_roles_bullets_is_captured(self):
        """The IBM-style role sits directly after Oracle's bullets — it must not be merged."""
        parsed = parse_open_source_resume(RESUME_TEXT)
        companies = [e.get("company") for e in parsed.get("experience") or []]
        assert len(companies) == 3
        assert companies[-1] == "IBM"


class TestParseProvenance:
    def test_upload_stamps_the_parser_version(self):
        from conftest import test_client as client

        res = client.post(
            "/api/resume/upload",
            files={
                "file": (
                    "resume.md",
                    io.BytesIO(RESUME_TEXT.encode("utf-8")),
                    "text/markdown",
                )
            },
        )
        assert res.status_code == 201
        body = res.json()
        assert body["parser_version"] == RESUME_PARSER_VERSION
        assert body["parse_stale"] is False

    def test_legacy_row_is_reported_stale(self, db_session):
        from conftest import test_client as client

        _seed_resume(db_session, parsed={"skills": ["Python"]}, parser_version=None)
        body = client.get("/api/resume").json()
        assert body["parser_version"] is None
        assert body["parse_stale"] is True

    def test_current_version_is_not_stale(self, db_session):
        from conftest import test_client as client

        _seed_resume(
            db_session, parsed={"skills": ["Python"]}, parser_version=RESUME_PARSER_VERSION
        )
        assert client.get("/api/resume").json()["parse_stale"] is False


class TestReparse:
    def test_reparse_recovers_a_dropped_role(self, db_session, monkeypatch):
        """The stored parse is missing IBM; re-parsing must bring it back."""
        from conftest import test_client as client

        stale = parse_open_source_resume(RESUME_TEXT)
        stale["experience"] = [
            e for e in stale["experience"] if e.get("company") != "IBM"
        ]
        resume = _seed_resume(db_session, parsed=stale, parser_version=1)
        assert len(stale["experience"]) == 2

        monkeypatch.setattr(
            "backend.main.parse_resume_to_json", lambda text: parse_open_source_resume(text)
        )
        res = client.post(f"/api/resume/{resume.id}/reparse")
        assert res.status_code == 200
        body = res.json()
        assert len(body["parsed_json"]["experience"]) == 3
        assert body["parsed_json"]["experience"][-1]["company"] == "IBM"
        assert body["parser_version"] == RESUME_PARSER_VERSION
        assert body["parse_stale"] is False

        db_session.refresh(resume)
        stored = json.loads(decrypt_data(resume.parsed_json_encrypted))
        assert len(stored["experience"]) == 3

    def test_reparse_preserves_user_owned_edits(self, db_session, monkeypatch):
        from conftest import test_client as client

        resume = _seed_resume(
            db_session,
            parsed={
                "skills": ["Hand-curated Skill"],
                "name": "Edited Name",
                "email": "edited@example.com",
                "experience_years": "18",
            },
            parser_version=1,
        )
        monkeypatch.setattr(
            "backend.main.parse_resume_to_json", lambda text: parse_open_source_resume(text)
        )
        body = client.post(f"/api/resume/{resume.id}/reparse").json()
        parsed = body["parsed_json"]
        assert parsed["skills"] == ["Hand-curated Skill"]
        assert parsed["name"] == "Edited Name"
        assert parsed["email"] == "edited@example.com"
        assert parsed["experience_years"] == "18"

    def test_reparse_keeps_the_stored_parse_when_the_parser_fails(
        self, db_session, monkeypatch
    ):
        from conftest import test_client as client

        resume = _seed_resume(db_session, parsed={"skills": ["Python"]}, parser_version=1)
        monkeypatch.setattr("backend.main.parse_resume_to_json", lambda text: {})
        res = client.post(f"/api/resume/{resume.id}/reparse")
        assert res.status_code == 502
        db_session.refresh(resume)
        assert json.loads(decrypt_data(resume.parsed_json_encrypted)) == {"skills": ["Python"]}
        assert resume.parser_version == 1

    def test_reparse_404_for_unknown_resume(self):
        from conftest import test_client as client

        assert client.post("/api/resume/999999/reparse").status_code == 404
