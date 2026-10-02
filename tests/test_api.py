import io
import json
import pytest
from unittest.mock import patch

from backend.config import encrypt_data, decrypt_data
from backend.database import Base, Company, Resume, Job, ApplicationEvent
from conftest import test_client as client, TestingSessionLocal


# -------------------------------------------------------------
# Unit Tests: Security & Encryption
# -------------------------------------------------------------
def test_encryption_roundtrip():
    secret_text = "John Doe, Senior Software Engineer, contact@example.com"
    encrypted = encrypt_data(secret_text)
    assert encrypted != secret_text
    assert len(encrypted) > 20

    decrypted = decrypt_data(encrypted)
    assert decrypted == secret_text


def test_encryption_empty():
    assert encrypt_data("") == ""
    assert decrypt_data("") == ""


# -------------------------------------------------------------
# Integration Tests: Health Endpoint
# -------------------------------------------------------------
def test_health_check():
    res = client.get("/api/health")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "ok"
    assert data["database"] == "connected"
    assert "active_llm" in data
    assert "provider" in data["active_llm"]


def test_llm_status_endpoint():
    res = client.get("/api/llm/status")
    assert res.status_code == 200
    data = res.json()
    assert "provider" in data
    assert "status" in data


# -------------------------------------------------------------
# Integration Tests: Company CRUD & Catalog Pre-seeding
# -------------------------------------------------------------
def test_company_crud():
    # 1. Create company with explicit details
    res = client.post(
        "/api/companies",
        json={
            "name": "Stripe",
            "domain": "stripe.com",
            "careers_url": "https://stripe.com/jobs",
            "description": "Financial infrastructure for the internet.",
        },
    )
    assert res.status_code == 201
    created = res.json()
    assert created["id"] is not None
    assert created["name"] == "Stripe"

    # 2. Duplicate rejection
    res_dup = client.post("/api/companies", json={"name": "stripe"})
    assert res_dup.status_code == 400

    # 3. List companies
    res_list = client.get("/api/companies")
    assert res_list.status_code == 200
    assert len(res_list.json()) == 1

    # 4. Get company details
    company_id = created["id"]
    res_get = client.get(f"/api/companies/{company_id}")
    assert res_get.status_code == 200
    assert res_get.json()["domain"] == "stripe.com"

    # 5. Update company details (PUT)
    res_put = client.put(
        f"/api/companies/{company_id}",
        json={
            "careers_url": "https://stripe.com/jobs/search",
            "recent_news": "Processed record payments volume.",
        },
    )
    assert res_put.status_code == 200
    updated = res_put.json()
    assert updated["careers_url"] == "https://stripe.com/jobs/search"
    assert updated["recent_news"] == "Processed record payments volume."

    # 6. Delete company
    res_del = client.delete(f"/api/companies/{company_id}")
    assert res_del.status_code == 204
    assert len(client.get("/api/companies").json()) == 0


def test_company_catalog_and_seeding(monkeypatch):
    import backend.company_catalog as company_catalog

    # Hermetic: use the shipped generic example catalog, never a personal company list.
    monkeypatch.setattr(
        company_catalog, "CONFIG_FILE", company_catalog.EXAMPLE_CONFIG_FILE
    )

    # 1. Fetch catalog
    res_cat = client.get("/api/companies/catalog")
    assert res_cat.status_code == 200
    catalog = res_cat.json()
    assert len(catalog) >= 3
    assert any(c["name"] == "Acme Corp" for c in catalog)

    # 2. Auto-fill from catalog when adding just the name
    res_add = client.post("/api/companies", json={"name": "Acme Corp"})
    assert res_add.status_code == 201
    comp = res_add.json()
    assert comp["domain"] == "acme.com"
    assert "greenhouse" in comp["careers_url"]
    assert comp["salary_insights"] is not None

    # Verify catalog now reflects tracked status
    res_cat_after = client.get("/api/companies/catalog")
    acme_entry = next(c for c in res_cat_after.json() if c["name"] == "Acme Corp")
    assert acme_entry["is_tracked"] is True

    # 3. Pre-seed all catalog companies
    res_seed = client.post("/api/companies/seed")
    assert res_seed.status_code == 200
    assert "Successfully seeded" in res_seed.json()["message"]

    # Verify count increased
    all_comps = client.get("/api/companies").json()
    assert len(all_comps) >= 3

    # Cleanup seeded companies for test isolation
    for c in all_comps:
        client.delete(f"/api/companies/{c['id']}")


def test_company_file_import_csv():
    csv_content = (
        "Company Name,Domain,Careers URL\n"
        "Scale AI,scaleai.com,https://jobs.lever.co/scaleai\n"
        "Perplexity,perplexity.ai,https://jobs.ashbyhq.com/perplexity\n"
    )
    files = {"file": ("companies.csv", csv_content.encode("utf-8"), "text/csv")}
    res = client.post("/api/companies/import", files=files)
    assert res.status_code == 200
    data = res.json()
    assert data["added_count"] == 2
    assert "Scale AI" in data["added"]
    assert "Perplexity" in data["added"]

    # Verify in DB
    all_comps = client.get("/api/companies").json()
    scale = next(c for c in all_comps if c["name"] == "Scale AI")
    assert scale["domain"] == "scaleai.com"
    assert scale["careers_url"] == "https://jobs.lever.co/scaleai"

    # Cleanup
    for c in all_comps:
        client.delete(f"/api/companies/{c['id']}")


def test_company_file_import_json():
    json_content = json.dumps(
        [
            {
                "name": "Mistral AI",
                "domain": "mistral.ai",
                "careers_url": "https://jobs.lever.co/mistral",
            },
            {
                "name": "Cohere",
                "domain": "cohere.com",
                "careers_url": "https://jobs.lever.co/cohere",
            },
        ]
    )
    files = {
        "file": ("companies.json", json_content.encode("utf-8"), "application/json")
    }
    res = client.post("/api/companies/import", files=files)
    assert res.status_code == 200
    data = res.json()
    assert data["added_count"] == 2

    # Cleanup
    for c in client.get("/api/companies").json():
        client.delete(f"/api/companies/{c['id']}")


def test_company_file_import_markdown():
    md_content = (
        "# Target Companies\n"
        "| Company | Domain | Careers Portal |\n"
        "| --- | --- | --- |\n"
        "| Warp | warp.dev | https://jobs.ashbyhq.com/warp |\n"
        "| Linear | linear.app | https://linear.app/careers |\n"
    )
    files = {"file": ("targets.md", md_content.encode("utf-8"), "text/markdown")}
    res = client.post("/api/companies/import", files=files)
    assert res.status_code == 200
    data = res.json()
    assert data["added_count"] == 2

    # Cleanup
    for c in client.get("/api/companies").json():
        client.delete(f"/api/companies/{c['id']}")


# -------------------------------------------------------------
# Integration Tests: Job Lifecycle & Tracking
# -------------------------------------------------------------
def test_job_crud_and_status_tracking():
    # Setup company
    comp_res = client.post("/api/companies", json={"name": "Vercel"})
    comp_id = comp_res.json()["id"]

    # 1. Create Job
    job_res = client.post(
        "/api/jobs",
        json={
            "company_id": comp_id,
            "title": "Staff Backend Engineer",
            "description": "Build high-throughput edge systems.",
            "salary_range": "$180k - $240k",
            "source": "Direct Careers Page",
            "match_score": 92.5,
        },
    )
    assert job_res.status_code == 201
    job_data = job_res.json()
    job_id = job_data["id"]
    assert job_data["status"] == "To Apply"
    assert job_data["company_name"] == "Vercel"
    assert job_data["applied_at"] is None

    # 2. Update status to 'Shortlisted'
    shortlist_res = client.patch(
        f"/api/jobs/{job_id}/status", json={"status": "Shortlisted"}
    )
    assert shortlist_res.status_code == 200
    assert shortlist_res.json()["status"] == "Shortlisted"

    # 3. Update status to 'Applied'
    update_res = client.patch(
        f"/api/jobs/{job_id}/status",
        json={
            "status": "Applied",
            "note": "Applied via company portal using resume v2",
        },
    )
    assert update_res.status_code == 200
    updated_job = update_res.json()
    assert updated_job["status"] == "Applied"
    assert updated_job["applied_at"] is not None

    # 4. Check timeline events created automatically
    events_res = client.get(f"/api/jobs/{job_id}/events")
    assert events_res.status_code == 200
    events = events_res.json()
    assert len(events) >= 3  # created event + shortlist event + status change event
    assert any("Shortlisted" in ev["description"] for ev in events)
    assert any("Applied" in ev["description"] for ev in events)

    # 5. Filter jobs by in_progress_only
    in_progress = client.get("/api/jobs?in_progress_only=true").json()
    assert len(in_progress) == 1

    # 6. Move status to 'Not Interested'
    dismiss_res = client.patch(
        f"/api/jobs/{job_id}/status", json={"status": "Not Interested"}
    )
    assert dismiss_res.status_code == 200
    assert dismiss_res.json()["status"] == "Not Interested"

    # 7. Verify in_progress_only now hides the dismissed job
    in_progress_after = client.get("/api/jobs?in_progress_only=true").json()
    assert len(in_progress_after) == 0

    # 8. Move status to 'Rejected'
    reject_res = client.patch(f"/api/jobs/{job_id}/status", json={"status": "Rejected"})
    assert reject_res.status_code == 200
    assert reject_res.json()["rejected_at"] is not None


def test_job_bulk_status():
    comp = client.post("/api/companies", json={"name": "BulkCorp"}).json()
    j1 = client.post(
        "/api/jobs", json={"company_id": comp["id"], "title": "Role 1"}
    ).json()
    j2 = client.post(
        "/api/jobs", json={"company_id": comp["id"], "title": "Role 2"}
    ).json()

    res = client.post(
        "/api/jobs/bulk-status",
        json={"job_ids": [j1["id"], j2["id"]], "status": "Applied"},
    )
    assert res.status_code == 200
    assert "Updated 2 job(s)" in res.json()["message"]

    # Verify updated in DB
    updated_j1 = client.get(f"/api/jobs/{j1['id']}").json()
    assert updated_j1["status"] == "Applied"
    assert updated_j1["applied_at"] is not None


# -------------------------------------------------------------
# Integration Tests: Resume Upload & Encrypted Retrieval
# -------------------------------------------------------------
def test_resume_upload_and_get():
    sample_resume = (
        "Alice Smith\n"
        "Senior Cloud Engineer\n"
        "Email: alice@example.com\n"
        "Skills: Python, FastAPI, Docker, Kubernetes, AWS"
    )

    # 1. Upload resume as markdown/text
    file_payload = {
        "file": (
            "resume.md",
            io.BytesIO(sample_resume.encode("utf-8")),
            "text/markdown",
        )
    }
    upload_res = client.post("/api/resume/upload", files=file_payload)
    assert upload_res.status_code == 201
    upload_data = upload_res.json()
    assert upload_data["filename"] == "resume.md"
    assert upload_data["is_active"] is True

    # 2. Check Database directly to verify content is encrypted at rest
    db = TestingSessionLocal()
    db_resume = db.query(Resume).filter(Resume.id == upload_data["id"]).first()
    assert db_resume.content_encrypted != sample_resume
    assert "Alice Smith" not in db_resume.content_encrypted
    db.close()

    # 3. Retrieve resume through API (should decrypt seamlessly)
    get_res = client.get("/api/resume")
    assert get_res.status_code == 200
    resume_out = get_res.json()
    assert "Alice Smith" in resume_out["raw_preview"]
    assert "Kubernetes" in resume_out["raw_preview"]


def test_resume_upload_validation_and_sanitization():
    # 1. Invalid extension rejection (.exe, .sh, .py)
    bad_payload = {
        "file": (
            "malicious.exe",
            io.BytesIO(b"MZ... executable"),
            "application/x-msdownload",
        )
    }
    bad_res = client.post("/api/resume/upload", files=bad_payload)
    assert bad_res.status_code == 400
    assert "Unsupported file extension" in bad_res.json()["detail"]

    # 2. Path traversal sanitization (../../resume.pdf -> resume.pdf)
    traversal_payload = {
        "file": (
            "../../secret_resume.pdf",
            io.BytesIO(b"%PDF-1.4 dummy valid text"),
            "application/pdf",
        )
    }
    with patch(
        "backend.main.parse_resume_document",
        return_value="Candidate with Python and AWS",
    ):
        trav_res = client.post("/api/resume/upload", files=traversal_payload)
        assert trav_res.status_code == 201
        assert trav_res.json()["filename"] == "secret_resume.pdf"


def test_multi_resume_switching_and_deletion():
    # Clear existing resumes
    client.delete("/api/resume")

    # 1. Upload Resume 1
    r1_payload = {
        "file": (
            "backend_resume.md",
            io.BytesIO(b"Candidate One\nPython, FastAPI"),
            "text/markdown",
        )
    }
    r1_res = client.post("/api/resume/upload", files=r1_payload)
    assert r1_res.status_code == 201
    r1_id = r1_res.json()["id"]

    # 2. Upload Resume 2
    r2_payload = {
        "file": (
            "ai_resume.md",
            io.BytesIO(b"Candidate Two\nPyTorch, LLMs"),
            "text/markdown",
        )
    }
    r2_res = client.post("/api/resume/upload", files=r2_payload)
    assert r2_res.status_code == 201
    r2_id = r2_res.json()["id"]

    # 3. List resumes (both should be present)
    list_res = client.get("/api/resumes")
    assert list_res.status_code == 200
    all_resumes = list_res.json()
    assert len(all_resumes) == 2

    # 4. Active should be r2 (latest)
    active_res = client.get("/api/resume")
    assert active_res.json()["id"] == r2_id

    # 5. Switch active to r1
    act_res = client.post(f"/api/resume/{r1_id}/activate")
    assert act_res.status_code == 200
    assert act_res.json()["is_active"] is True
    assert client.get("/api/resume").json()["id"] == r1_id

    # 6. Delete r1 (should activate r2 automatically)
    del_res = client.delete(f"/api/resume/{r1_id}")
    assert del_res.status_code == 200
    assert client.get("/api/resume").json()["id"] == r2_id

    # 7. Bulk Delete
    del_all_res = client.delete("/api/resume")
    assert del_all_res.status_code == 200
    assert client.get("/api/resume").status_code == 404


# -------------------------------------------------------------
# Integration Tests: Phase 5 Endpoints (Scrape, Apply, Chat)
# -------------------------------------------------------------
@patch("backend.main.gather_company_intelligence")
@patch("backend.main.scrape_greenhouse_jobs")
def test_trigger_jobs_scrape(mock_gh, mock_intel):
    mock_intel.return_value = {
        "description": "Leading payments infrastructure company.",
        "recent_news": "Launched Stripe Billing in 5 new countries.",
        "salary_insights": "$170k - $240k",
        "hiring_process": "Recruiter -> Coding -> System Design",
    }
    mock_gh.return_value = [
        {
            "title": "Staff Backend Engineer - Payments",
            "url": "https://boards.greenhouse.io/stripe/jobs/999",
            "description": "Looking for Staff Engineer with Python & distributed systems.",
            "location": "San Francisco, CA",
            "source": "Greenhouse",
            "source_type": "Direct",
        }
    ]

    # Create target company
    comp_res = client.post(
        "/api/companies", json={"name": "Stripe", "domain": "stripe.com"}
    )
    comp_id = comp_res.json()["id"]

    # Trigger scrape
    scrape_res = client.post("/api/jobs/scrape", json={"company_id": comp_id})
    assert scrape_res.status_code == 200
    data = scrape_res.json()
    assert data["jobs_found"] >= 1
    assert data["jobs_added"] >= 1

    # Fetch jobs to verify
    jobs_res = client.get(f"/api/jobs?company_id={comp_id}")
    assert jobs_res.status_code == 200
    jobs = jobs_res.json()
    assert len(jobs) >= 1

    # Test single-company scrape endpoint
    single_scrape = client.post(f"/api/companies/{comp_id}/scrape-jobs")
    assert single_scrape.status_code == 200

    # Test on-demand tailoring endpoint
    tailor_res = client.post(f"/api/jobs/{jobs[0]['id']}/tailor")
    # 200 on success; 400/502 when no resume / AI unavailable in isolated fixture
    assert tailor_res.status_code in [200, 400, 502]

    # Test stop scrape endpoint
    stop_res = client.post("/api/jobs/scrape/stop")
    assert stop_res.status_code == 200
    assert "stopped" in stop_res.json()["message"]

    # Test stop linkedin sync endpoint
    stop_li_res = client.post("/api/linkedin/sync/stop")
    assert stop_li_res.status_code == 200
    assert "stopped" in stop_li_res.json()["message"]


@patch("backend.main.scrape_workday_jobs")
@patch("backend.main.scrape_workable_jobs")
@patch("backend.main.scrape_smartrecruiters_jobs")
@patch("backend.main.scrape_ashby_jobs")
@patch("backend.main.scrape_lever_jobs")
@patch("backend.main.scrape_greenhouse_jobs")
def test_trigger_jobs_scrape_forwards_filters_to_new_portals(
    mock_gh, mock_lev, mock_ashby, mock_sr, mock_wk, mock_wd
):
    """SmartRecruiters/Workable are dispatched after the earlier portals miss, and receive the
    candidate's filters (title/location/work-mode) unchanged."""
    mock_gh.return_value = []
    mock_lev.return_value = []
    mock_ashby.return_value = []
    mock_sr.return_value = []
    mock_wk.return_value = []
    mock_wd.return_value = []
    # Medium-risk portals are opt-in with consent: enable + acknowledge so this dispatch test exercises the chain.
    client.put(
        "/api/preferences",
        json={
            "features": {
                "ats_smartrecruiters": True,
                "ats_workable": True,
                "ats_workday": True,
            },
            "consents": {
                "ats_smartrecruiters": {"ack": True},
                "ats_workable": {"ack": True},
                "ats_workday": {"ack": True},
            },
        },
    )
    with patch("backend.main.gather_company_intelligence"):
        comp = client.post(
            "/api/companies",
            json={
                "name": "AcmePortalTest",
                "careers_url": "https://apply.workable.com/acmeportaltest/",
            },
        ).json()
        res = client.post(
            "/api/jobs/scrape",
            json={
                "company_id": comp["id"],
                "target_titles": "backend engineer",
                "target_locations": "india",
                "work_mode": "remote",
            },
        )
    assert res.status_code == 200

    assert mock_sr.called and mock_wk.called and mock_wd.called
    for mock in (mock_sr, mock_wk, mock_wd):
        kwargs = mock.call_args.kwargs
        assert kwargs["title_filters"] == ["backend engineer"]
        assert kwargs["location_filters"] == ["india"]
        assert kwargs["work_mode"] == "remote"
        assert kwargs["max_matches"] == 35


@patch("backend.main.launch_assisted_application_session")
def test_trigger_assisted_apply(mock_launch):
    comp = client.post("/api/companies", json={"name": "ApplyCorp"}).json()
    job = client.post(
        "/api/jobs",
        json={
            "company_id": comp["id"],
            "title": "Lead Software Engineer",
            "url": "https://careers.applycorp.com/job/1",
        },
    ).json()

    apply_res = client.post(f"/api/jobs/{job['id']}/apply")
    assert apply_res.status_code == 200
    assert apply_res.json()["status"] == "Applied"

    # Verify job status in DB is updated to Applied
    get_job = client.get(f"/api/jobs/{job['id']}").json()
    assert get_job["status"] == "Applied"
    assert get_job["applied_at"] is not None


@patch("backend.playwright_app.BrowserSessionManager.open_job_tab")
def test_bulk_apply(mock_open_tab):
    comp = client.post("/api/companies", json={"name": "BulkCorp"}).json()
    j1 = client.post(
        "/api/jobs",
        json={
            "company_id": comp["id"],
            "title": "Dev 1",
            "url": "https://example.com/1",
        },
    ).json()
    j2 = client.post(
        "/api/jobs",
        json={
            "company_id": comp["id"],
            "title": "Dev 2",
            "url": "https://example.com/2",
        },
    ).json()

    bulk_res = client.post(
        "/api/jobs/bulk-apply", json={"job_ids": [j1["id"], j2["id"]]}
    )
    assert bulk_res.status_code == 200
    data = bulk_res.json()
    assert len(data["applied_job_ids"]) == 2

    # Verify both jobs are marked Applied
    assert client.get(f"/api/jobs/{j1['id']}").json()["status"] == "Applied"
    assert client.get(f"/api/jobs/{j2['id']}").json()["status"] == "Applied"


def test_scrape_audit_endpoint():
    res = client.get("/api/jobs/scrape-audit")
    assert res.status_code == 200
    data = res.json()
    assert "total_companies" in data
    assert "audit" in data


@patch("backend.main.generate_text")
def test_interview_prep_chat(mock_gen):
    mock_gen.return_value = "To prepare for Stripe, focus on idempotency, rate limiting, and distributed ledger consistency."

    comp = client.post("/api/companies", json={"name": "Stripe"}).json()
    job = client.post(
        "/api/jobs",
        json={
            "company_id": comp["id"],
            "title": "Staff Backend Engineer",
            "description": "Distributed systems scale",
        },
    ).json()

    chat_res = client.post(
        f"/api/jobs/{job['id']}/chat",
        json={"message": "What system design topics should I prepare?"},
    )
    assert chat_res.status_code == 200
    data = chat_res.json()
    assert "idempotency" in data["reply"]
    assert data["job_id"] == job["id"]


def test_frontend_static_serving():
    res = client.get("/")
    assert res.status_code == 200
    assert "Job Alert Agent" in res.text


@patch("backend.main.sync_playwright")
@patch("backend.main.scan_and_sync_linkedin_saved_jobs")
@patch("backend.main.scan_linkedin_job_alerts")
@patch("backend.main.scan_linkedin_job_recommendations")
def test_linkedin_sync_endpoint(mock_recs, mock_alerts, mock_saved, mock_pw):
    mock_alerts.return_value = []
    # Setup mock recommendations
    mock_recs.return_value = [
        {
            "title": "Staff ML Infrastructure Engineer",
            "company": "Scale AI",
            "location": "San Francisco, CA",
            "url": "https://www.linkedin.com/jobs/view/123456",
            "description": "Build ML infra at Scale AI.",
            "source": "LinkedIn Recommendation",
            "source_type": "LinkedIn",
        }
    ]

    # Pre-create a job in DB that is saved on LinkedIn
    comp = client.post("/api/companies", json={"name": "OpenAI"}).json()
    job = client.post(
        "/api/jobs",
        json={
            "company_id": comp["id"],
            "title": "Research Engineer",
            "url": "https://www.linkedin.com/jobs/view/999999",
            "status": "To Apply",
        },
    ).json()

    # Mock saved jobs response indicating the job is now closed on LinkedIn
    mock_saved.return_value = [
        {
            "title": "Research Engineer",
            "company": "OpenAI",
            "url": "https://www.linkedin.com/jobs/view/999999",
            "is_closed": True,
            "unsaved": True,
        }
    ]

    # LinkedIn automation is opt-in AND consent-gated; acknowledge it for this sync test.
    client.put(
        "/api/preferences",
        json={
            "features": {"linkedin_sync": True},
            "consents": {"linkedin_sync": {"ack": True}},
        },
    )

    sync_res = client.post("/api/linkedin/sync")
    assert sync_res.status_code == 200
    data = sync_res.json()
    assert data["recommendations_added"] >= 1
    assert data["closed_jobs_pruned"] >= 1

    # Verify new recommended job exists in DB
    rec_jobs = client.get("/api/jobs").json()
    assert any(j["title"] == "Staff ML Infrastructure Engineer" for j in rec_jobs)

    # Verify closed job was marked as Rejected and logged
    closed_job = client.get(f"/api/jobs/{job['id']}").json()
    assert closed_job["status"] == "Rejected"
    events = client.get(f"/api/jobs/{job['id']}/events").json()
    assert any("Closed on LinkedIn" in e["description"] for e in events)


@patch("backend.main.sync_playwright")
@patch("backend.main.scan_and_sync_linkedin_saved_jobs")
@patch("backend.main.scan_linkedin_job_alerts")
@patch("backend.main.scan_linkedin_job_recommendations")
def test_linkedin_saved_job_promotes_not_interested_to_shortlisted(
    mock_recs, mock_alerts, mock_saved, mock_pw
):
    """Saved jobs are explicit user intent: a previously dismissed ('Not Interested') job must become 'Shortlisted'."""
    mock_recs.return_value = []
    mock_alerts.return_value = []

    comp = client.post("/api/companies", json={"name": "Cohere"}).json()
    job = client.post(
        "/api/jobs",
        json={
            "company_id": comp["id"],
            "title": "Senior Backend Engineer",
            "url": "https://www.linkedin.com/jobs/view/555555",
            "status": "Not Interested",
        },
    ).json()

    mock_saved.return_value = [
        {
            "title": "Senior Backend Engineer",
            "company": "Cohere",
            "location": "Bengaluru, India",
            "url": "https://www.linkedin.com/jobs/view/555555",
            "is_closed": False,
            "unsaved": False,
        }
    ]

    client.put(
        "/api/preferences",
        json={
            "features": {"linkedin_sync": True},
            "consents": {"linkedin_sync": {"ack": True}},
        },
    )
    res = client.post("/api/linkedin/sync")
    assert res.status_code == 200

    updated = client.get(f"/api/jobs/{job['id']}").json()
    assert updated["status"] == "Shortlisted"


@patch("backend.main.evaluate_job_due_diligence")
@patch("backend.main.generate_embeddings")
@patch("backend.main.check_semantic_dismissal")
@patch("backend.main.get_db")
@patch("backend.main.scan_and_sync_linkedin_saved_jobs")
@patch("backend.main.scan_linkedin_job_alerts")
@patch("backend.playwright_app.is_profile_in_use", return_value=False)
def test_scheduled_linkedin_sync_alerts_use_valid_kwargs(
    mock_in_use,
    mock_alerts,
    mock_saved,
    mock_get_db,
    mock_dismissal,
    mock_emb,
    mock_dd,
    db_session,
):
    """Regression: the scheduled runner must call scan_linkedin_job_alerts with its real signature.

    Previously it passed ``limit=10`` (TypeError swallowed by a broad except), so scheduled
    LinkedIn syncs always reported 0 items.
    """
    from backend.main import _run_linkedin_sync_task_runner

    mock_get_db.return_value = iter([db_session])
    mock_saved.return_value = []
    mock_dismissal.return_value = False
    mock_emb.return_value = [0.0] * 8
    mock_dd.return_value = {"is_ghost_job": False}

    # Real signature: a bogus kwarg (e.g. limit=) raises TypeError here.
    def fake_alerts(
        playwright_instance,
        headless=True,
        max_alerts=5,
        max_jobs_per_alert=15,
        target_roles=None,
        target_location=None,
        page=None,
    ):
        return [
            {
                "title": "Staff Software Engineer",
                "company": "GoDaddy",
                "location": "Remote, India",
                "url": "https://www.linkedin.com/jobs/view/123",
                "description": "GoDaddy is hiring staff engineers.",
                "source": "LinkedIn Job Alert",
                "source_type": "LinkedIn",
            }
        ]

    mock_alerts.side_effect = fake_alerts

    # The runner self-gates on feature flag + explicit consent; satisfy both on this session.
    from backend.database import get_user_preferences, record_consent

    _pref = get_user_preferences(db_session)
    _pref.features_json = '{"linkedin_sync": true}'
    db_session.commit()
    record_consent(db_session, "linkedin_sync")

    result = _run_linkedin_sync_task_runner()
    assert result["alerts_ingested"] == 1
    _, kwargs = mock_alerts.call_args
    assert "max_alerts" in kwargs


def test_job_target_filtering_and_prune():
    comp = client.post("/api/companies", json={"name": "FilterCorp"}).json()
    j1 = client.post(
        "/api/jobs",
        json={
            "company_id": comp["id"],
            "title": "Staff Backend Engineer",
            "location": "San Francisco, CA (Remote)",
        },
    ).json()
    j2 = client.post(
        "/api/jobs",
        json={
            "company_id": comp["id"],
            "title": "Senior Product Marketing Manager",
            "location": "New York, NY (Onsite)",
        },
    ).json()
    j3 = client.post(
        "/api/jobs",
        json={
            "company_id": comp["id"],
            "title": "Frontend Engineer",
            "location": "London, UK (Hybrid)",
        },
    ).json()

    # 1. Test filtering by title
    backend_jobs = client.get("/api/jobs?title_query=backend,staff").json()
    assert any(j["id"] == j1["id"] for j in backend_jobs)
    assert not any(j["id"] == j2["id"] for j in backend_jobs)

    # 2. Test filtering by location
    uk_jobs = client.get("/api/jobs?location_query=london,uk").json()
    assert any(j["id"] == j3["id"] for j in uk_jobs)
    assert not any(j["id"] == j1["id"] for j in uk_jobs)

    # 3. Test filtering by work_mode
    remote_jobs = client.get("/api/jobs?work_mode=remote").json()
    assert any(j["id"] == j1["id"] for j in remote_jobs)
    assert not any(j["id"] == j2["id"] for j in remote_jobs)

    # 4. Test pruning non-matching jobs in To Apply with target_cities
    prune_res = client.post(
        "/api/jobs/prune",
        json={
            "target_titles": "Backend, Engineer",
            "target_locations": "United States",
            "target_cities": "San Francisco",
            "work_mode": "remote",
        },
    )
    assert prune_res.status_code == 200
    assert prune_res.json()["pruned_count"] >= 1


def test_resume_skills_update_endpoint():
    sample_resume = "Alex Johnson\nBackend Developer\nSkills: Python, Django"
    file_payload = {
        "file": (
            "alex_resume.md",
            io.BytesIO(sample_resume.encode("utf-8")),
            "text/markdown",
        )
    }
    res = client.post("/api/resume/upload", files=file_payload).json()

    update_res = client.put(
        f"/api/resume/{res['id']}/skills",
        json={
            "skills": ["Python", "FastAPI", "PostgreSQL", "Docker"],
            "candidate_name": "Alexander Johnson",
            "email": "alex.j@example.com",
            "experience_years": "8+ Years",
        },
    )
    assert update_res.status_code == 200
    data = update_res.json()
    assert "FastAPI" in data["parsed_json"]["skills"]
    assert data["parsed_json"]["name"] == "Alexander Johnson"
    assert data["parsed_json"]["email"] == "alex.j@example.com"


def test_clean_system_cache_endpoint():
    comp = client.post("/api/companies", json={"name": "CacheCorp"}).json()
    client.post(
        "/api/jobs",
        json={
            "company_id": comp["id"],
            "title": "Unapplied Role 1",
            "status": "To Apply",
        },
    )
    client.post(
        "/api/jobs",
        json={
            "company_id": comp["id"],
            "title": "Dismissed Role 2",
            "status": "Not Interested",
        },
    )
    client.post(
        "/api/jobs",
        json={"company_id": comp["id"], "title": "Applied Role 3", "status": "Applied"},
    )
    client.post(
        "/api/jobs",
        json={
            "company_id": comp["id"],
            "title": "Shortlisted Role 4",
            "status": "Shortlisted",
        },
    )
    client.post(
        "/api/jobs",
        json={
            "company_id": comp["id"],
            "title": "Interview Role 5",
            "status": "Interview",
        },
    )

    # Clean cache should ONLY delete 'To Apply' jobs
    res = client.post("/api/system/clean-cache")
    assert res.status_code == 200
    assert res.json()["deleted_count"] == 1  # Only To Apply deleted

    # Verify Applied, Shortlisted, Interview, and Dismissed jobs are preserved
    all_remaining = client.get("/api/jobs").json()
    remaining_titles = [j["title"] for j in all_remaining]
    assert "Applied Role 3" in remaining_titles
    assert "Shortlisted Role 4" in remaining_titles
    assert "Interview Role 5" in remaining_titles
    assert "Dismissed Role 2" in remaining_titles
    assert "Unapplied Role 1" not in remaining_titles


def test_location_matching_prevents_foreign_restricted_remote_jobs():
    from backend.main import check_location_match

    # Candidate profile: India + Bengaluru/Hyderabad + Remote
    loc_filters = ["India"]
    city_filters = ["Bengaluru", "Hyderabad", "Remote"]

    # 1. Foreign-restricted remote positions should NOT match (including Ashby formats)
    assert (
        check_location_match(
            "Remote, Canada; Remote, United States", loc_filters, city_filters
        )
        is False
    )
    assert check_location_match("Remote, Poland", loc_filters, city_filters) is False
    assert (
        check_location_match("Remote, United Kingdom", loc_filters, city_filters)
        is False
    )
    assert (
        check_location_match("Toronto, Canada Remote", loc_filters, city_filters)
        is False
    )
    assert (
        check_location_match(
            "Senior Backend Engineer (AMER) - Remote, US", loc_filters, city_filters
        )
        is False
    )
    assert check_location_match("London, UK", loc_filters, city_filters) is False
    assert check_location_match("US-PA-Remote", loc_filters, city_filters) is False
    assert (
        check_location_match("US-NY-New York City-Remote", loc_filters, city_filters)
        is False
    )
    assert check_location_match("US-WA-Remote", loc_filters, city_filters) is False
    assert check_location_match("US-NY-Remote", loc_filters, city_filters) is False
    assert check_location_match("US-Remote", loc_filters, city_filters) is False
    assert check_location_match("Remote - US", loc_filters, city_filters) is False
    assert check_location_match("Remote (US)", loc_filters, city_filters) is False
    assert check_location_match("CA-Toronto-Remote", loc_filters, city_filters) is False
    assert check_location_match("DE-Berlin-Remote", loc_filters, city_filters) is False

    # 2. India & Target Hubs positions SHOULD match
    assert check_location_match("Bangalore, India", loc_filters, city_filters) is True
    assert (
        check_location_match("Bengaluru, Karnataka", loc_filters, city_filters) is True
    )
    assert check_location_match("Hyderabad, India", loc_filters, city_filters) is True
    assert check_location_match("Remote, India", loc_filters, city_filters) is True
    assert check_location_match("IN-Bengaluru", loc_filters, city_filters) is True
    assert check_location_match("IN-Remote", loc_filters, city_filters) is True

    # 3. Truly Global/Worldwide Remote SHOULD match
    assert check_location_match("Worldwide Remote", loc_filters, city_filters) is True
    assert check_location_match("Global Remote", loc_filters, city_filters) is True
    assert check_location_match("All-Remote", loc_filters, city_filters) is True


def test_location_matching_uses_cities_when_country_is_all():
    """With 'All Countries' (empty country) + specific target cities, foreign-restricted remotes are still excluded."""
    from backend.main import check_location_match, radar_location_matches

    loc_filters = []
    city_filters = ["Bengaluru", "Hyderabad", "Singapore", "London", "Dublin"]

    # Remote roles restricted to countries the user did NOT target -> excluded
    assert (
        check_location_match(
            "Remote, Canada; Remote, United States", loc_filters, city_filters
        )
        is False
    )
    assert check_location_match("Remote, US", loc_filters, city_filters) is False
    assert check_location_match("Remote, Canada", loc_filters, city_filters) is False
    assert check_location_match("Remote, Germany", loc_filters, city_filters) is False
    # US abbreviations must be caught too (regression: "SF, NY, Remote" survived a prune)
    assert check_location_match("SF, NY, Remote", loc_filters, city_filters) is False
    assert (
        check_location_match("Austin, TX, Remote", loc_filters, city_filters) is False
    )
    # ...and a US location that coincidentally contains a target-country substring ("york" -> GB)
    assert (
        check_location_match("US-NY-New York City-Remote", loc_filters, city_filters)
        is False
    )

    # Remote roles in the user's regions / truly global -> allowed
    assert check_location_match("Remote, India", loc_filters, city_filters) is True
    assert (
        check_location_match("Remote, United Kingdom", loc_filters, city_filters)
        is True
    )
    assert check_location_match("Remote, Singapore", loc_filters, city_filters) is True
    assert check_location_match("Worldwide Remote", loc_filters, city_filters) is True
    assert check_location_match("Remote", loc_filters, city_filters) is True

    # Opportunity Radar applies the same guard
    assert (
        radar_location_matches("Remote, United States", [], city_filters, "remote")
        is False
    )
    assert radar_location_matches("Remote, London", [], city_filters, "remote") is True
    assert radar_location_matches("Remote", [], city_filters, "remote") is True


def test_operation_logs_lifecycle():
    # 1. Query initial logs
    res = client.get("/api/operations/logs")
    assert res.status_code == 200
    initial_logs = res.json()

    # 2. Trigger an operation (bulk apply)
    comp = client.post("/api/companies", json={"name": "LogCorp"}).json()
    j1 = client.post(
        "/api/jobs",
        json={
            "company_id": comp["id"],
            "title": "Dev 1",
            "url": "https://example.com/log1",
        },
    ).json()
    client.post("/api/jobs/bulk-apply", json={"job_ids": [j1["id"]]})

    # 3. Verify operation log was persisted
    res2 = client.get("/api/operations/logs")
    assert res2.status_code == 200
    logs = res2.json()
    assert len(logs) >= len(initial_logs) + 1
    latest = logs[0]
    assert latest["operation_type"] == "bulk_apply"
    assert latest["jobs_count"] == 1
    assert "Bulk Assisted Apply" in latest["summary"]
    assert latest["details_json"] is not None

    # 4. Clear operation logs
    del_res = client.delete("/api/operations/logs")
    assert del_res.status_code == 200
    assert "Successfully cleared" in del_res.json()["message"]

    # 5. Verify empty
    res3 = client.get("/api/operations/logs")
    assert res3.status_code == 200
    assert len(res3.json()) == 0


def test_llm_token_usage_lifecycle(tmp_path, monkeypatch):
    from backend.ai_helper import token_tracker

    test_file = tmp_path / "token_stats.json"
    monkeypatch.setattr("backend.ai_helper.STATS_FILE", test_file)

    token_tracker.reset()
    token_tracker.record(500, 150)

    # 1. Fetch token usage stats
    res = client.get("/api/llm/token-usage")
    assert res.status_code == 200
    data = res.json()
    assert data["prompt_tokens"] == 500
    assert data["completion_tokens"] == 150
    assert data["total_tokens"] == 650
    assert data["total_inferences"] == 1
    assert data["local_cost_usd"] == 0.0
    assert "gpt_4o" in data["estimated_cloud_costs_usd"]
    assert data["total_savings_usd"] > 0

    # 2. Reset token stats
    del_res = client.delete("/api/llm/token-usage")
    assert del_res.status_code == 200
    assert "reset" in del_res.json()["message"]

    # 3. Verify reset
    res2 = client.get("/api/llm/token-usage")
    assert res2.json()["total_tokens"] == 0
    assert res2.json()["total_inferences"] == 0

    # 4. Test incremental sync endpoint
    from backend.database import OperationLog

    db = next(
        client.app.dependency_overrides[
            client.app.dependency_overrides.__iter__().__next__()
        ]()
    )
    try:
        log = OperationLog(
            operation_type="scraper",
            status="Completed",
            summary="Scraped 2 jobs",
            prompt_tokens=300,
            completion_tokens=75,
        )
        db.add(log)
        db.commit()
    finally:
        db.close()

    sync_res = client.post("/api/llm/token-usage/sync")
    assert sync_res.status_code == 200
    assert sync_res.json()["prompt_tokens"] >= 300
    assert sync_res.json()["completion_tokens"] >= 75


@patch("backend.main.threading.Thread")
def test_browser_auth_session_endpoints(mock_thread):
    # Test POST /api/browser/auth-session
    res1 = client.post(
        "/api/browser/auth-session", json={"url": "https://www.linkedin.com/login"}
    )
    assert res1.status_code == 200
    assert res1.json()["status"] == "Launched"
    assert "linkedin.com/login" in res1.json()["message"]

    # Test POST /api/browser/interactive-session
    res2 = client.post(
        "/api/browser/interactive-session",
        params={"target_url": "https://mail.google.com"},
    )
    assert res2.status_code == 200
    assert res2.json()["status"] == "Launched"
    assert "mail.google.com" in res2.json()["message"]

    # Test scheme rejection (file:// and javascript:)
    bad_res1 = client.post(
        "/api/browser/auth-session", json={"url": "file:///etc/passwd"}
    )
    assert bad_res1.status_code == 400
    assert "Invalid URL scheme" in bad_res1.json()["detail"]

    bad_res2 = client.post(
        "/api/browser/interactive-session", params={"target_url": "javascript:alert(1)"}
    )
    assert bad_res2.status_code == 400
    assert "Invalid URL scheme" in bad_res2.json()["detail"]


def test_qa_memory_and_dismissed_patterns_api():
    # 1. Create Q&A memory item
    qa_res = client.post(
        "/api/memory/qa",
        json={
            "question_text": "What are your salary expectations for this role?",
            "answer_text": "$220,000 - $260,000 base + equity based on overall scope.",
            "category": "salary",
        },
    )
    assert qa_res.status_code == 201
    qa_id = qa_res.json()["id"]

    # 2. Get list of Q&A items
    list_res = client.get("/api/memory/qa")
    assert list_res.status_code == 200
    assert any(i["id"] == qa_id for i in list_res.json())

    # 3. Search Q&A memory
    search_res = client.post(
        "/api/memory/qa/search",
        json={
            "query_question": "Desired compensation and salary expectation range",
            "top_k": 2,
            "min_similarity": 0.3,
        },
    )
    assert search_res.status_code == 200
    assert len(search_res.json()["matches"]) > 0

    # 4. Delete Q&A item
    del_qa = client.delete(f"/api/memory/qa/{qa_id}")
    assert del_qa.status_code == 204

    # 5. Dismissed Pattern lifecycle via job status
    comp = client.post("/api/companies", json={"name": "PatternCo"}).json()
    job = client.post(
        "/api/jobs",
        json={
            "company_id": comp["id"],
            "title": "Legacy Perl Systems Developer",
            "description": "Maintain legacy scripts.",
        },
    ).json()

    # Marking as Not Interested indexes the pattern
    patch_res = client.patch(
        f"/api/jobs/{job['id']}/status", json={"status": "Not Interested"}
    )
    assert patch_res.status_code == 200

    patterns_res = client.get("/api/memory/dismissed-patterns")
    assert patterns_res.status_code == 200
    patterns = patterns_res.json()
    matched_pat = next((p for p in patterns if "Legacy Perl" in p["title"]), None)
    assert matched_pat is not None
    # Dismissals are stored company-scoped
    assert matched_pat.get("company_name") == "PatternCo"

    # Delete pattern
    del_pat = client.delete(f"/api/memory/dismissed-patterns/{matched_pat['id']}")
    assert del_pat.status_code == 204


# -------------------------------------------------------------
# User Preferences, Opt-in Feature Flags & BYOK Settings
# -------------------------------------------------------------
def test_user_preferences_roundtrip_and_feature_flags():
    # Defaults: ATS + Google on, LinkedIn/Gmail off (opt-in)
    res = client.get("/api/preferences")
    assert res.status_code == 200
    data = res.json()
    assert data["features"]["ats_portals"] is True
    assert data["features"]["google_jobs"] is True
    assert data["features"]["linkedin_sync"] is False
    assert "linkedin_sync" in data["available_features"]
    # High-comfort public-API portals on; unofficial/ambiguous portals opt-in (off).
    assert data["features"]["ats_greenhouse"] is True
    assert data["features"]["ats_lever"] is True
    assert data["features"]["ats_ashby"] is True
    assert data["features"]["ats_smartrecruiters"] is False
    assert data["features"]["ats_workable"] is False
    assert data["features"]["ats_workday"] is False
    # Amazon is blocklisted by default.
    assert "amazon" in (data["excluded_companies"] or "").lower()

    upd = client.put(
        "/api/preferences",
        json={
            "target_titles": "Staff Engineer, Backend",
            "target_country": "India",
            "target_cities": "Bengaluru, Hyderabad",
            "work_mode": "remote",
            "timezone": "Asia/Kolkata",
            "features": {
                "linkedin_sync": True,
                "gmail_sync": True,
                "ats_workable": True,
            },
            "consents": {
                "linkedin_sync": {"ack": True},
                "ats_workable": {"ack": True},
            },
            "excluded_companies": "Amazon,  acme.com ,Amazon",
        },
    )
    assert upd.status_code == 200
    data = upd.json()
    assert data["target_titles"] == "Staff Engineer, Backend"
    assert data["target_cities"] == "Bengaluru, Hyderabad"
    assert data["work_mode"] == "remote"
    assert data["timezone"] == "Asia/Kolkata"
    assert data["features"]["linkedin_sync"] is True
    assert data["features"]["gmail_sync"] is True
    assert data["features"]["ats_workable"] is True
    # Consent records round-trip and are stamped at the current server-side version.
    assert data["consents"]["linkedin_sync"]["ack"] is True
    assert data["consents"]["linkedin_sync"]["version"] == data["consent_version"]
    # Blocklist is normalized + de-duplicated.
    assert data["excluded_companies"] == "amazon, acme.com"
    # Persisted
    assert (
        client.get("/api/preferences").json()["target_titles"]
        == "Staff Engineer, Backend"
    )


@patch("backend.main.scrape_greenhouse_jobs")
def test_trigger_jobs_scrape_skips_disabled_portal(mock_gh):
    """A disabled portal flag must skip that scanner server-side."""
    mock_gh.return_value = []
    client.put("/api/preferences", json={"features": {"ats_greenhouse": False}})
    with patch("backend.main.gather_company_intelligence"):
        comp = client.post(
            "/api/companies",
            json={
                "name": "GateDisabledTest",
                "careers_url": "https://boards.greenhouse.io/gatedisabledtest",
            },
        ).json()
        res = client.post("/api/jobs/scrape", json={"company_id": comp["id"]})
    assert res.status_code == 200
    mock_gh.assert_not_called()


@patch("backend.main.scrape_greenhouse_jobs")
def test_trigger_jobs_scrape_master_switch_disables_all_ats(mock_gh):
    mock_gh.return_value = []
    client.put("/api/preferences", json={"features": {"ats_portals": False}})
    with patch("backend.main.gather_company_intelligence"):
        comp = client.post(
            "/api/companies",
            json={
                "name": "MasterOffTest",
                "careers_url": "https://boards.greenhouse.io/masterofftest",
            },
        ).json()
        res = client.post("/api/jobs/scrape", json={"company_id": comp["id"]})
    assert res.status_code == 200
    mock_gh.assert_not_called()


@patch("backend.main.scrape_greenhouse_jobs")
def test_trigger_jobs_scrape_skips_excluded_company(mock_gh):
    """Companies on the blocklist (default includes 'amazon') are never scanned."""
    mock_gh.return_value = []
    with patch("backend.main.gather_company_intelligence"):
        comp = client.post(
            "/api/companies",
            json={
                "name": "AmazonExcludedTest",
                "domain": "amazon-excluded.test",
                "careers_url": "https://boards.greenhouse.io/amazonexcludedtest",
            },
        ).json()
        res = client.post("/api/jobs/scrape", json={"company_id": comp["id"]})
    assert res.status_code == 200
    mock_gh.assert_not_called()
    audit = client.get("/api/jobs/scrape-audit").json()
    assert any(a.get("status") == "Excluded" for a in audit.get("audit", []))


def test_preferences_strips_legacy_remote_pseudo_city(db_session):
    """The legacy global 'Remote' city token is dropped; work_mode owns remote eligibility."""
    upd = client.put(
        "/api/preferences",
        json={
            "target_country": "Ireland",
            "target_cities": "Dublin, Remote, Cork",
            "work_mode": "all",
        },
    )
    assert upd.status_code == 200
    data = upd.json()
    assert data["target_cities"] == "Dublin, Cork"
    # Persisted normalized value is returned on subsequent reads too
    assert client.get("/api/preferences").json()["target_cities"] == "Dublin, Cork"


def test_confirm_submission_endpoint(db_session):
    comp = Company(name="Acme Confirm", domain="acme-confirm.com")
    db_session.add(comp)
    db_session.commit()
    db_session.refresh(comp)

    job = Job(company_id=comp.id, title="Staff Engineer", status="Applied")
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)
    assert job.submission_confirmed is False

    res = client.post(f"/api/jobs/{job.id}/confirm-submission")
    assert res.status_code == 200
    data = res.json()
    assert data["submission_confirmed"] is True

    db_session.refresh(job)
    assert job.submission_confirmed is True
    events = (
        db_session.query(ApplicationEvent)
        .filter(ApplicationEvent.job_id == job.id)
        .all()
    )
    assert any(e.event_type == "submitted" for e in events)


def test_job_status_validation_canonicalizes_and_rejects_invalid():
    comp = client.post("/api/companies", json={"name": "StatusCo"}).json()

    bad = client.post(
        "/api/jobs", json={"company_id": comp["id"], "title": "X", "status": "Bogus"}
    )
    assert bad.status_code == 422

    ok = client.post(
        "/api/jobs", json={"company_id": comp["id"], "title": "Y", "status": "applied"}
    )
    assert ok.status_code == 201
    assert ok.json()["status"] == "Applied"

    bad_patch = client.patch(
        f"/api/jobs/{ok.json()['id']}/status", json={"status": "Nonsense"}
    )
    assert bad_patch.status_code == 422


def test_linkedin_sync_disabled_by_default():
    """The manual LinkedIn sync must respect the opt-in feature flag."""
    res = client.post("/api/linkedin/sync")
    assert res.status_code == 200
    data = res.json()
    assert "disabled" in data["message"].lower()


def test_preferences_cannot_enable_risk_feature_without_consent():
    """Turning on a consent-required feature without acknowledgment is rejected server-side."""
    res = client.put("/api/preferences", json={"features": {"linkedin_sync": True}})
    assert res.status_code == 403
    # The flag must remain off.
    assert client.get("/api/preferences").json()["features"]["linkedin_sync"] is False


def test_consent_only_does_not_enable_feature():
    """Recording consent is independent of enabling the flag."""
    res = client.put("/api/preferences", json={"consents": {"linkedin_sync": {"ack": True}}})
    assert res.status_code == 200
    data = res.json()
    assert data["consents"]["linkedin_sync"]["ack"] is True
    assert data["features"]["linkedin_sync"] is False


def test_linkedin_sync_route_requires_consent():
    """With the flag on but consent revoked, the manual LinkedIn route is blocked (403)."""
    client.put(
        "/api/preferences",
        json={"features": {"linkedin_sync": True}, "consents": {"linkedin_sync": {"ack": True}}},
    )
    with patch("backend.main.has_consent", return_value=False):
        res = client.post("/api/linkedin/sync")
    assert res.status_code == 403


def test_dispatch_linkedin_task_requires_consent():
    """The background dispatch endpoint is hard-gated on consent."""
    client.put(
        "/api/preferences",
        json={"features": {"linkedin_sync": True}, "consents": {"linkedin_sync": {"ack": True}}},
    )
    with patch("backend.main.has_consent", return_value=False):
        res = client.post("/api/tasks/sync/linkedin")
    assert res.status_code == 403


def test_consent_required_features_exposed():
    """The API advertises which features require consent (single source of truth for the UI)."""
    data = client.get("/api/preferences").json()
    assert "linkedin_sync" in data["consent_required_features"]
    assert data["consent_version"] >= 1


def test_preferences_403_does_not_persist_unrelated_fields():
    """A consent-gate rejection must roll back the whole request, not persist partial edits."""
    resp = client.put(
        "/api/preferences",
        json={"target_titles": "Staff Engineer", "features": {"linkedin_sync": True}},
    )
    assert resp.status_code == 403
    data = client.get("/api/preferences").json()
    assert data["features"]["linkedin_sync"] is False
    assert "Staff Engineer" not in (data["target_titles"] or "")


@patch("backend.main.find_talent_partners", return_value=[])
@patch("backend.main.search_linkedin_talent_partners")
@patch("backend.main.has_consent", return_value=False)
@patch("backend.main.is_feature_enabled", return_value=True)
def test_hiring_team_skips_linkedin_automation_without_consent(
    mock_feat, mock_consent, mock_search, mock_partners
):
    """Authenticated LinkedIn talent search must not run when consent is absent."""
    comp = client.post("/api/companies", json={"name": "NoConsent Co"}).json()
    db = TestingSessionLocal()
    job = Job(
        company_id=comp["id"],
        title="Staff Engineer",
        description="Build things",
        url="https://noconsent.example/1",
        status="To Apply",
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    jid = job.id
    db.close()

    res = client.get(f"/api/jobs/{jid}/hiring-team")
    assert res.status_code == 200
    mock_search.assert_not_called()


def test_llm_settings_never_exposes_raw_keys():
    res = client.get("/api/settings/llm")
    assert res.status_code == 200
    data = res.json()
    assert "openai_api_key" not in data
    assert "gemini_api_key" not in data
    assert "anthropic_api_key" not in data

    put = client.put(
        "/api/settings/llm",
        json={"openai_api_key": "sk-test-1234567890", "allow_cloud_fallback": True},
    )
    assert put.status_code == 200
    data = put.json()
    assert data["openai_configured"] is True
    assert data["allow_cloud_fallback"] is True


def test_gmail_credentials_connect_and_disconnect_flow():
    from unittest.mock import patch as _patch

    assert client.get("/api/gmail/status").json()["connected"] is False

    saved = client.post(
        "/api/gmail/credentials",
        json={
            "client_id": "cid.apps.googleusercontent.com",
            "client_secret": "csecret",
        },
    )
    assert saved.status_code == 200
    status = client.get("/api/gmail/status").json()
    assert status["client_id_set"] is True
    assert status["client_secret_set"] is True
    assert status["connected"] is False

    with _patch(
        "backend.gmail_client.build_auth_url",
        return_value={
            "auth_url": "https://accounts.google.com/o/oauth2/auth?client_id=cid",
            "code_verifier": "verifier123",
        },
    ):
        res = client.get("/api/gmail/connect")
        assert res.status_code == 200
        assert res.json()["auth_url"].startswith("https://accounts.google.com")
        # PKCE verifier is persisted for the callback
        from backend.config import get_gmail_settings

        assert get_gmail_settings()["code_verifier"] == "verifier123"

    client.post("/api/gmail/disconnect")
    assert client.get("/api/gmail/status").json()["client_id_set"] is False


def test_gmail_sync_respects_opt_in_flag():
    # gmail_sync is disabled by default
    res = client.post("/api/gmail/sync")
    assert res.status_code == 200
    assert res.json()["status"] == "disabled"


# -------------------------------------------------------------
# Opportunity Radar (structured hiring-intent feed)
# -------------------------------------------------------------
def test_compute_hiring_intent_signals():
    """Deterministic scoring: source weight, freshness, hiring language, match, ghost penalty."""
    import datetime as dt

    from backend.main import compute_hiring_intent

    now = dt.datetime(2026, 1, 15, tzinfo=dt.timezone.utc)

    class _J:
        pass

    strong = _J()
    strong.source = "Gmail Recruiter Outreach"
    strong.title = "Staff Backend Engineer"
    strong.description = "We are hiring! Join our team to build distributed systems."
    strong.match_score = 90.0
    strong.is_ghost_job = False
    strong.created_at = now - dt.timedelta(days=2)

    score, reasons = compute_hiring_intent(strong, now=now)
    assert score >= 40 + 15
    assert any("Source" in r for r in reasons)
    assert any("Fresh" in r for r in reasons)
    assert any("Hiring language" in r for r in reasons)

    weak = _J()
    weak.source = "Google Jobs"
    weak.title = "Marketing Manager"
    weak.description = "Generic listing."
    weak.match_score = 5.0
    weak.is_ghost_job = False
    weak.created_at = now - dt.timedelta(days=400)
    weak_score, _ = compute_hiring_intent(weak, now=now)
    assert weak_score < score

    ghost = _J()
    ghost.source = "Google Jobs"
    ghost.title = "Engineer"
    ghost.description = "x"
    ghost.match_score = 5.0
    ghost.is_ghost_job = True
    ghost.created_at = now - dt.timedelta(days=400)
    ghost_score, ghost_reasons = compute_hiring_intent(ghost, now=now)
    assert ghost_score == 0  # clamped, never negative
    assert any("ghost" in r.lower() for r in ghost_reasons)


def test_opportunity_radar_endpoint_ranks_and_filters(db_session):
    import datetime as dt

    comp = client.post("/api/companies", json={"name": "RadarCorp"}).json()
    now = dt.datetime.now(dt.timezone.utc)

    db_session.add(
        Job(
            company_id=comp["id"],
            title="Staff Backend Engineer",
            description="We are hiring. Join our team!",
            location="Remote, India",
            source="Gmail Recruiter Outreach",
            source_type="Direct",
            status="To Apply",
            match_score=88.0,
            url="https://example.com/strong",
            created_at=now,
        )
    )
    db_session.add(
        Job(
            company_id=comp["id"],
            title="Warehouse Associate",
            description="Generic listing.",
            location="Onsite",
            source="Google Jobs",
            source_type="Direct",
            status="To Apply",
            match_score=5.0,
            url="https://example.com/weak",
            created_at=now - dt.timedelta(days=60),
        )
    )
    db_session.add(
        Job(
            company_id=comp["id"],
            title="Already Applied Role",
            description="We are hiring!",
            location="Remote",
            source="LinkedIn Job Alert",
            source_type="LinkedIn",
            status="Applied",
            match_score=50.0,
            url="https://example.com/applied",
            created_at=now,
        )
    )
    db_session.add(
        Job(
            company_id=comp["id"],
            title="Dismissed Role",
            description="We are hiring!",
            location="Remote",
            source="LinkedIn Job Alert",
            source_type="LinkedIn",
            status="Not Interested",
            match_score=50.0,
            url="https://example.com/dismissed",
            created_at=now,
        )
    )
    db_session.commit()

    items = client.get("/api/opportunities/radar").json()
    titles = [i["title"] for i in items]
    assert "Staff Backend Engineer" in titles
    # Applied is excluded by default; dismissed is always excluded
    assert "Already Applied Role" not in titles
    assert "Dismissed Role" not in titles
    # Ranked by intent: recruiter outreach above generic Google Jobs
    assert items[0]["title"] == "Staff Backend Engineer"
    assert items[0]["intent_score"] > items[-1]["intent_score"]
    assert items[0]["intent_reasons"]

    # include_applied surfaces applied roles
    applied_items = client.get("/api/opportunities/radar?include_applied=true").json()
    assert "Already Applied Role" in [i["title"] for i in applied_items]

    # company filter + min_intent
    filtered = client.get(
        f"/api/opportunities/radar?company_id={comp['id']}&min_intent=25"
    ).json()
    assert all(i["company_id"] == comp["id"] for i in filtered)
    assert all(i["intent_score"] >= 25 for i in filtered)


def test_opportunity_radar_respects_preferences(db_session):
    """Radar limits results to persisted roles/cities/work mode (city-precise)."""
    import datetime as dt

    comp = client.post("/api/companies", json={"name": "RadarPrefs"}).json()
    now = dt.datetime.now(dt.timezone.utc)

    def mk(title, location, url):
        return Job(
            company_id=comp["id"],
            title=title,
            description="We are hiring. Join our team!",
            location=location,
            source="LinkedIn Job Alert",
            source_type="LinkedIn",
            status="To Apply",
            match_score=80.0,
            url=url,
            created_at=now,
        )

    db_session.add(
        mk("Staff Backend Engineer", "Bengaluru, India (Hybrid)", "https://x/1")
    )
    db_session.add(
        mk("Staff Backend Engineer", "Chennai, India (On-site)", "https://x/2")
    )
    db_session.add(
        mk(
            "Mergers and Acquisitions Director",
            "Bengaluru, India (On-site)",
            "https://x/3",
        )
    )
    db_session.add(mk("Staff Backend Engineer", "Remote, India", "https://x/4"))
    db_session.commit()

    client.put(
        "/api/preferences",
        json={
            "target_titles": "Backend",
            "target_country": "India",
            "target_cities": "Bengaluru",
            "work_mode": "all",
        },
    )

    items = client.get("/api/opportunities/radar").json()
    titles = [i["title"] for i in items]
    locations = " ".join(i["location"] or "" for i in items)
    assert "Staff Backend Engineer" in titles
    assert "Mergers and Acquisitions Director" not in titles  # role filter
    assert "Chennai" not in locations  # city-precise filter
    assert any("Remote" in (i["location"] or "") for i in items)  # remote allowed

    # respect_preferences=false bypasses the filters
    unfiltered = client.get("/api/opportunities/radar?respect_preferences=false").json()
    assert any(i["title"] == "Mergers and Acquisitions Director" for i in unfiltered)

    # onsite work mode drops the remote listing
    client.put(
        "/api/preferences",
        json={
            "target_titles": "Backend",
            "target_country": "India",
            "target_cities": "Bengaluru",
            "work_mode": "onsite",
        },
    )
    onsite = client.get("/api/opportunities/radar").json()
    assert all("Remote" not in (i["location"] or "") for i in onsite)


def test_enrich_role_titles_suggests_related():
    """Role enrichment strips seniority and suggests adjacent titles (excluding selected ones)."""
    res = client.post("/api/roles/enrich", json={"titles": ["Staff Software Engineer"]})
    assert res.status_code == 200
    titles = [s["title"] for s in res.json()["suggestions"]]
    assert "Software Engineer" in titles
    assert "Backend Engineer" in titles
    assert "Staff Software Engineer" not in titles  # already selected

    res2 = client.post("/api/roles/enrich", json={"titles": ["Principal Engineer"]})
    assert res2.status_code == 200
    titles2 = [s["title"] for s in res2.json()["suggestions"]]
    assert "Software Engineer" in titles2


@patch("backend.main.generate_consolidated_application_package")
def test_tailor_surfaces_error_instead_of_fabricating(mock_pkg):
    """When the AI cannot produce materials, tailoring must surface an error and
    must NOT invent a match score, cover letter, or resume points."""
    comp = client.post("/api/companies", json={"name": "TailorErr Co"}).json()
    db = TestingSessionLocal()
    job = Job(
        company_id=comp["id"],
        title="Backend Engineer",
        description="Build APIs",
        url="https://tailorerr.example/1",
        status="To Apply",
    )
    db.add(job)
    db.add(
        Resume(
            filename="resume.md",
            content_encrypted=encrypt_data("Alice — Python engineer."),
            is_active=True,
        )
    )
    db.commit()
    db.refresh(job)
    jid = job.id
    db.close()

    mock_pkg.return_value = {
        "error": "AI unavailable",
        "match_score": None,
        "strengths": [],
        "gaps": ["Unable to generate application materials."],
        "tailored_resume_points": None,
        "cover_letter": None,
        "cold_message": None,
    }

    # Single tailor: explicit 502, no DB write.
    res = client.post(f"/api/jobs/{jid}/tailor")
    assert res.status_code == 502
    assert "AI unavailable" in res.json()["detail"]

    jdb = TestingSessionLocal()
    stored = jdb.query(Job).filter(Job.id == jid).first()
    assert stored.match_score is None
    assert stored.cover_letter_draft is None
    assert stored.tailored_resume_points is None
    jdb.close()

    # Bulk tailor: failure is reported, not fabricated.
    bulk = client.post("/api/jobs/bulk-tailor", json={"job_ids": [jid]})
    assert bulk.status_code == 200
    body = bulk.json()
    assert jid in body.get("failed", [])


# -------------------------------------------------------------
# Unit Tests: Generated package application semantics
# -------------------------------------------------------------
def test_apply_generated_package_overwrite_semantics():
    """Explicit regeneration overwrites drafts; JIT fill only fills gaps and only sets
    match_scored when a real AI score is present."""
    from backend.main import _apply_generated_package

    job = Job(
        company_id=1,
        title="Engineer",
        description="d",
        url="https://pkg.example/1",
        cover_letter_draft="old letter",
        tailored_resume_points="old points",
        cold_message_draft=None,
        match_score=None,
        match_scored=False,
    )
    pkg = {
        "match_score": 88.0,
        "strengths": ["Python"],
        "gaps": ["K8s"],
        "cover_letter": "new letter",
        "tailored_resume_points": "new points",
        "cold_message": "new message",
    }

    # JIT fill: existing drafts preserved, only the empty one is filled.
    _apply_generated_package(job, pkg, overwrite_drafts=False)
    assert job.cover_letter_draft == "old letter"
    assert job.tailored_resume_points == "old points"
    assert job.cold_message_draft == "new message"
    assert job.match_score == 88.0
    assert job.match_scored is True
    assert "Python" in job.match_analysis and "K8s" in job.match_analysis

    # Explicit regeneration: drafts replaced.
    _apply_generated_package(job, pkg, overwrite_drafts=True)
    assert job.cover_letter_draft == "new letter"
    assert job.tailored_resume_points == "new points"


def test_apply_generated_package_no_score_keeps_unscored():
    """A package without an AI score must not fabricate match_scored=True."""
    from backend.main import _apply_generated_package

    job = Job(
        company_id=1,
        title="Engineer",
        description="d",
        url="https://pkg.example/2",
        match_score=None,
        match_scored=False,
    )
    _apply_generated_package(
        job,
        {"match_score": None, "strengths": [], "gaps": ["none"], "cover_letter": None},
        overwrite_drafts=True,
    )
    assert job.match_score is None
    assert job.match_scored is False


# -------------------------------------------------------------
# Integration Tests: Hiring-team contacts via DDG talent search
# -------------------------------------------------------------
@patch("backend.main.find_talent_partners")
def test_hiring_team_uses_ddg_talent_partners_when_linkedin_disabled(mock_partners):
    """With LinkedIn sync off (default), contacts are populated from the DDG talent-partner
    search instead of requiring a logged-in browser session."""
    mock_partners.return_value = [
        {
            "name": "Jane Recruiter",
            "headline": "Technical Recruiter at Acme",
            "profile_url": "https://linkedin.com/in/jane-recruiter",
        }
    ]
    comp = client.post("/api/companies", json={"name": "TalentSearch Co"}).json()
    db = TestingSessionLocal()
    job = Job(
        company_id=comp["id"],
        title="Platform Engineer",
        description="Build platforms",
        url="https://talentsearch.example/1",
        status="To Apply",
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    jid = job.id
    db.close()

    res = client.get(f"/api/jobs/{jid}/hiring-team")
    assert res.status_code == 200
    data = res.json()
    urls = [c["author_profile_url"] for c in data["contacts"]]
    assert "https://linkedin.com/in/jane-recruiter" in urls
    mock_partners.assert_called_once()


# -------------------------------------------------------------
# Match-scoring configuration (user-tunable thresholds)
# -------------------------------------------------------------
def test_scoring_config_defaults_and_roundtrip():
    data = client.get("/api/preferences").json()
    assert data["scoring_config"]["must_weight"] == 3.0
    assert data["scoring_config"]["good"] == 70.0

    res = client.put(
        "/api/preferences",
        json={"scoring_config": {"must_floor": 0.2, "good": 60.0, "bogus": 9}},
    )
    assert res.status_code == 200
    cfg = res.json()["scoring_config"]
    assert cfg["must_floor"] == 0.2
    assert cfg["good"] == 60.0
    assert cfg["must_weight"] == 3.0   # untouched default preserved
    assert "bogus" not in cfg          # unknown keys ignored

    # Persisted across requests.
    assert client.get("/api/preferences").json()["scoring_config"]["good"] == 60.0


