# Implementation Plan: Status Clean-Up & Job Dossier Visual Stack/Hiring Team Enhancements

This plan encompasses two primary deliverables:
1. **Database Application Status Clean-Up**: Correcting legacy misattributed email/sync statuses for a few affected employers and related jobs.
2. **Torre.ai-Inspired Dossier Upgrades**:
   - **Tech Stack & Requirements Compatibility Matrix**: Modern, distinctive stack breakdown (Segmented Compatibility Bar, Matched Resume Skills `✓`, and Growth/Gap Areas `⚡` with 1-click tailoring integration).
   - **Embedded Hiring Team & Talent Partners Card**: Direct visibility into recruiters, engineering managers, and talent partners for the target company with 1-click personalized outreach.

---

## User Review Required

> [!NOTE]
> - **Job 102 (AI research lab)** has a verified candidate application and valid interview confirmation and will **remain in `Interview` status**.
> - **Job 106 (large retail tech employer)** and **Job 162 (early-stage startup)** were misattributed by a legacy email regex and will be **reset to `To Apply`**.
> - **Job 181 (product-analytics startup)** will be corrected from a generic company label to the proper company name.

---

## Proposed Changes

### 1. Database Status Clean-Up & Verification

#### [NEW] [`backend/scripts/cleanup_interview_statuses.py`](../../backend/scripts/cleanup_interview_statuses.py)
- Programmatic clean-up script that:
  - Identifies jobs in `Interview` status without genuine application records or with misattributed email events.
  - Safely resets `Job 106` (large retail tech employer) and `Job 162` (early-stage startup) back to `To Apply`.
  - Re-links `Job 181` to the proper company name.
  - Records an audit `ApplicationEvent` for each correction.

---

### 2. Backend Tech Stack & Hiring Team Endpoints

#### [MODIFY] [`backend/ai_helper.py`](../../backend/ai_helper.py)
- **`extract_job_skills_and_alignment(job_description: str, candidate_skills: List[str]) -> Dict[str, Any]`**:
  - Extracts required technical skills from the job description.
  - Cross-references against candidate's active decrypted resume skills.
  - Returns:
    - `compatibility_pct`: percentage of required skills matched.
    - `matched_skills`: list of matched skill tokens.
    - `gap_skills`: list of missing/growth skills.
    - `core_stack`: all primary technologies detected.

#### [MODIFY] [`backend/main.py`](../../backend/main.py)
- Add `GET /api/jobs/{job_id}/skills-alignment`:
  - Returns the structured stack compatibility breakdown.
- Enhance `GET /api/companies/{company_id}/talent-partners` or `GET /api/jobs/{job_id}/hiring-team`:
  - Returns associated hiring team leads / recruiters and a talent-acquisition directory for that specific company with outreach templates.

---

### 3. Frontend SPA UI & Dossier Visualizations

#### [MODIFY] [`frontend/index.html`](../../frontend/index.html)
- In `#details-modal`:
  - **Stack Compatibility Card (inside Tab 1 - Match)**:
    - Segmented progress bar with compatibility badge (`e.g. 80% Tech Stack Match`).
    - Matched Skills section with glowing green `✓` pills.
    - Gap / Growth Skills section with amber `⚡` pills and a 1-click **"⚡ Bridge Gap in Resume"** button.
  - **New Tab / Card: 👥 Hiring Team & Key Contacts**:
    - Renders talent partners, hiring managers, and recruiters for the company with photo/avatar, role, profile link, and **"💬 Draft Recruiter Note"** action.

#### [MODIFY] [`frontend/app.js`](../../frontend/app.js)
- Update `openJobDetailsModal(jobId)`:
  - Dynamically computes and renders the **Stack Compatibility Matrix**.
  - Fetches and displays **Hiring Team & Recruiter Contacts** for that company.
  - Wires up the **"💬 Draft Recruiter Note"** shortcut to pre-populate the recruiter's name in Tab 4 (Cold Note).

#### [MODIFY] [`frontend/styles.css`](../../frontend/styles.css)
- Add styles for:
  - `.stack-compatibility-card`, `.stack-progress-bar`, `.stack-segment-matched`
  - `.skill-chip-matched` (green theme with glow) and `.skill-chip-gap` (amber/violet theme with action trigger)
  - `.hiring-team-grid`, `.hiring-contact-card`, `.hiring-contact-avatar`

---

## Verification Plan

### Automated Tests
- Create [`tests/test_skills_alignment.py`](../../tests/test_skills_alignment.py):
  - Test skill extraction and alignment with candidate resume skills.
  - Test `GET /api/jobs/{id}/skills-alignment` endpoint response schema.
  - Test hiring team resolution for jobs and companies.
- Run full pytest test suite:
  ```bash
  ./.venv/bin/pytest -v tests/
  ```

### Manual Verification
1. Run the clean-up script and verify in the Tracker that the reset roles return to **To Apply** while the verified interview remains **Interview**.
2. Open the Job Details Dossier on any job card:
   - Verify the **Tech Stack Compatibility Bar** and color-coded **Matched vs. Gap Skill Chips**.
   - Open the **Hiring Team** tab and verify recruiter / talent partner cards appear with 1-click outreach.
