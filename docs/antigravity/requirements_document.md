# Requirements Document: Job Alert Agent (Open Source)

## 1. Project Overview
The **Job Alert Agent** is an open-source, self-hosted tool designed to automate, enrich, and assist in the job application lifecycle. It helps job seekers monitor job alert emails, research companies, prepare personalized application materials (resumes, cover letters, cold outreach messages), track application progress, and assist in applying through automated browser sessions.

---

## 2. Functional Requirements

### 2.1 Job Alert & Email Integration
* **Gmail Scanning**: Access Gmail via a dedicated persistent Chrome profile in Playwright to scan and retrieve job alert emails (e.g., from LinkedIn, direct company alerts). This eliminates the need for OAuth API credentials or storing refresh tokens.
* **Due Diligence & Verification**:
  * Identify if the alert is from LinkedIn or a direct company portal.
  * If it's a LinkedIn Job Alert, verify if the position exists on the direct company portal.
  * **Ghost Job / Repost Detection**: Track if a job is repeatedly reposted over a threshold period. If so, flag it as a *"Probable Ghost/Gas-lighting Position"*.
  * Mark direct company portal alerts as *"Genuine"*.

### 2.2 Company & Job Research (Intelligence Engine)
* **Direct Company Portal Search**: For a configured list of companies, periodically search their careers pages for open positions matching user preferences.
* **Talent Partner & Employee Lookup**:
  * Identify the hiring manager/talent partner or 2-3 relevant employees in similar roles.
  * Use search engines/LinkedIn queries to locate profiles.
* **LinkedIn Outreach Drafts**: Suggest the best cold message to send to the identified contact based on the job description and user profile.
* **Company Insights**: Retrieve and categorize:
  * Products, services, and core business.
  * Recent news and press releases relevant to the role.
  * Salary ranges for the target position (e.g., from open web, reviews).
  * Employer reviews and ratings.
  * Known hiring process details.
* **Information Summarization**: Present a clean, categorized summary of all gathered data per job position.

### 2.3 Company List Enrichment
* **Similar Company Recommendations**: Discover similar companies based on products, services, recent news, pay ranges, and employer reviews.
* **Outside Opportunities**: Look up opportunities and hiring managers at these recommended companies.

### 2.4 Assisted Application Submission
* **Resume Parsing & Vectorization**: Use an open-source parser to extract skills, experience, and education, storing them in a vectorized format.
* **Resume Tailoring**: Automatically suggest changes or generate an updated resume tailored to the specific job description and company context.
* **Cover Letter Generation**: Draft a highly personalized cover letter matching the job description.
* **Playwright Automation**:
  * Direct submissions directly on the company's portal (strongly preferred over LinkedIn Easy Apply).
  * Auto-fill forms where possible.
  * **Interactivity & Guardrails**:
    * If signup or MFA/login is required, pause and wait for the user to complete it in the browser session.
    * Once logged in, resume auto-filling and submit or hand over to the user.
    * No applications must be submitted automatically without user confirmation (unless explicitly enabled).

### 2.5 Applied Job Tracking & Response Handling
* **Automated Response Scanning**: Scan Gmail for incoming updates related to submitted applications, searching by company name or job title.
* **Email Classification**: Automatically parse email bodies (detecting keywords/intent for interview requests, technical screens, or regret/rejection notices) and update the job status in the DB.
* **Timelines & Dates**: Track explicit lifecycle dates including `applied_at`, `interview_scheduled_at`, and `rejected_at`.
* **State Archiving**: Allow the dashboard to filter/show only "In Progress" applications, archiving/hiding rejections but displaying metrics like total days in process.

### 2.6 Interview Preparation Chat (Post-Application)
* If an application moves to the "Screening" or "Interview" phase, allow the user to chat with a local RAG agent about the specific role and company.
* Fetch and store company engineering handbooks, engineering blogs, core values, and typical interview processes.
* Persist this info in a local vector DB for fast, offline querying.

---

## 3. Technical Stack

* **Backend**: Python (selected for its rich AI, scraping, NLP, and Playwright libraries).
* **AI/LLM**: Local Unsloth (OpenAI-compatible inference server) & local Ollama, with optional configuration for OpenAI, Anthropic, or Google Gemini APIs.
* **Database**: PostgreSQL with `pgvector` extension (allows relational tracking of jobs/applications and vector embeddings for resume/company docs in a single DB).
* **Web Interface**: A modern, responsive dashboard built with FastAPI (backend) and React/Vite (frontend) or a streamlined Python-native UI like Streamlit/NiceGUI (to keep deployment simple for self-hosting).
* **Automation & Browser Session**: Playwright for Python using a **dedicated, isolated persistent Chrome profile** (e.g. `~/.job-alert-agent/chrome-profile`). Allows authenticating once to Gmail/LinkedIn without exposing personal browser tabs or storing raw OAuth tokens.
* **APIs / Search**: Structured web search (DuckDuckGo HTML), ATS board endpoints (Greenhouse, Lever), and browser-driven session queries.

---

## 4. Guardrails & Safety
1. **Browser Profile Isolation**: The agent runs in its own dedicated Chrome user data directory (`~/.job-alert-agent/chrome-profile`), fully isolated from the user's daily personal browser, bookmarks, cookies, and password managers. No remote debugging ports are exposed across the network.
2. **Zero-Token Credential Storage**: No passwords, Google OAuth client secrets, or LinkedIn API keys are persisted on disk or in the database. Authentication state is handled natively through standard browser cookies within the dedicated profile.
3. **No Duplicates**: Do not apply to positions that are already marked as "Applied" or "Ignored".
4. **No Unapproved Communication**: The agent *shall not* send emails, LinkedIn messages, or submit applications without explicit user approval. It only generates drafts.
5. **Data Security (Encryption)**:
   * Encrypt sensitive data (emails, resumes, notes) at rest and in transit using Fernet symmetric encryption.
6. **Local First**: Prioritize local processing (Ollama, local databases) to maintain user privacy.

---

## 5. Proposed MVP (Minimum Viable Product) Scope
To establish a solid foundation, we will build the MVP focusing on the core pipeline:

1. **Database & Core Models**: PostgreSQL schema with `pgvector` for storing resumes, companies, jobs, and application status.
2. **Job Ingestion & Parsing**:
   * CLI or simple Web UI to input a list of companies.
   * Local resume parsing (PDF/Docx extraction to Markdown/JSON) and vectorization.
3. **Company & Job Intelligence**:
   * Scrape careers pages of 2-3 sample target companies.
   * Retrieve basic company description and open jobs.
   * Use local Ollama to draft a cover letter and tailored resume points.
4. **Assisted Apply (Playwright)**:
   * Launch a Playwright browser session that navigates to a job's application page and displays a side panel or helper terminal containing the generated resume details and cover letter for easy copy-pasting or basic auto-fill.
5. **Basic Dashboard**:
   * A premium, clean single-page web dashboard using FastAPI and a modern CSS front-end.
   * Displays the list of fetched jobs, categorized intelligence summaries, and status tracking (To Apply, Applied, Interviewing, Rejected).
