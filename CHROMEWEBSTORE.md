# Chrome Web Store Listing & Publishing Blueprint

This document serves as the single source of truth for all Chrome Web Store metadata, permissions justifications, privacy disclosures, and version history for the **Job Alert Agent** Chrome Extension.

---

## 1. Store Listing Metadata

* **Extension Name**: Job Alert Agent - AI Career Copilot & ATS Autofill
* **Short Description** (max 132 chars): AI Career Copilot and 1-Click ATS Application Autofiller with dynamic job match scoring and STAR interview coaching.
* **Category**: Productivity / Tools
* **Default Language**: English (United States)
* **Website**: `https://github.com/nagapavan/job-alert-agent`

### Detailed Store Description
```
Job Alert Agent is your personal AI Career Copilot and 1-Click Application Autofiller designed to streamline your job search and boost interview conversions.

🌟 KEY FEATURES:
• Chrome Side Panel Copilot: Sits right alongside job postings on Greenhouse, Lever, Ashby, and Workday to analyze ATS match compatibility, highlight strengths, and reveal keyword gaps. LinkedIn analysis is available on demand (side panel triggers injection under activeTab; no passive access).
• 1-Click ATS Form Autofiller: Injects a floating helper badge on application pages to instantly autofill your candidate profile (contact info, links, work authorization) and tailored screener essay answers in <1 second.
• Multi-Turn STAR Interview Coach: Practice behavioral and technical interview questions directly in your browser sidebar with instant STAR-method feedback tailored to your actual resume background.
• Q&A Memory Bank: Automatically stores and re-adapts approved application essay answers with zero-token instant recall.
• Hybrid Dual-Mode Architecture: Works seamlessly with your local Job Agent FastAPI backend or runs 100% on-device in Chrome using built-in Gemini Nano.

🔒 PRIVACY-FIRST DESIGN:
All candidate resume data and application answers are stored locally on your device or in your private local backend. No third-party tracking or data monetization.
```

---

## 2. Permissions Justification

Every permission declared in `manifest.json` requires a clear plain-English justification for the Chrome Web Store review team:

| Permission | Justification |
| :--- | :--- |
| `sidePanel` | Required to provide a persistent, multi-tab AI Career Copilot and Match Analyzer alongside active job posting tabs. |
| `storage` | Required to save the candidate's local autofill profile, runtime connection preferences, and offline Q&A memory bank. |
| `tabs` | Required to detect the URL and title of the active job posting tab to provide relevant match analysis and autofill actions. |
| `scripting` | Required to inject the job-description extractor and the 1-click autofill overlay into the active job posting page (including any embedded ATS iframe) when the user clicks "Inspect Current Tab" or the autofill button. |
| `activeTab` | Retained as a secondary gesture grant; primary access is provided by the host permissions below. |

### Host Permissions Justification

| Host Pattern | Justification |
| :--- | :--- |
| `http://localhost:8000/*`, `http://127.0.0.1:8000/*` | Connects to the user's private local FastAPI backend server for AI model inference and SQLite/PostgreSQL synchronization. |
| `https://*/*`, `http://*/*` | The extension analyzes job postings and autofills applications on employer career portals, which cannot be enumerated in advance. Scripts are injected **on demand only**, when the user explicitly clicks Inspect/Autofill; no page content is read passively or at page load (the only declarative content scripts run on the four ATS domains above). No page data leaves the machine except to the user's own local backend. |

> **Broad host access is intentional.** Rather than prompting for permission on every new job site,
> the extension is granted host access once at install so analysis/autofill works seamlessly on any
> employer portal. Injection remains user-initiated; there is no background page scraping.

---

## 3. Privacy & Data Use Disclosures

* **Data Collected**:
  * User profile details (Name, Email, Phone, Portfolio URLs, Work Authorization status) stored strictly in `chrome.storage.local`.
  * Job description text extracted from active tabs for the sole purpose of computing compatibility scores and drafting application materials.
* **Data Transmission**:
  * In Connected Mode: Transmitted exclusively to the user's locally running server (`http://localhost:8000`).
  * In Standalone Mode: Processed 100% on-device inside Chrome via Gemini Nano (`window.ai`).
* **Data Monetization**: None. No data is sold, rented, or transferred to advertising networks.

---

## 4. Version History

* **v1.0.0** (2026-09-04): Initial release with Chrome Side Panel Copilot, In-DOM ATS Autofill (Greenhouse, Lever, Ashby, Workday; LinkedIn on demand), Hybrid Runtime Adapter (FastAPI + Gemini Nano), and Q&A Memory Bank.
