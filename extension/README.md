# Job Alert Agent: Chrome Extension (Manifest V3)

A **Single Unified Hybrid Chrome Extension** that gives candidates an in-browser **AI Career Copilot**, **Live ATS Match Analyzer**, and **1-Click Application Autofiller** for Greenhouse, Lever, Ashby, Workday, and custom portals. LinkedIn is supported **on demand only** (injected by the side panel under `activeTab`, never preloaded).

---

## 🚀 Quickstart: Load into Google Chrome

1. Open Google Chrome and navigate to `chrome://extensions/`.
2. Toggle **Developer mode** in the top right corner.
3. Click **Load unpacked** in the top left corner.
4. Select the `extension/` folder inside this repository:
   ```
   path/to/job-alert-agent/extension
   ```
5. Pin the **Job Alert Agent** extension in your Chrome toolbar.
6. Click the extension icon on any job posting (e.g. Greenhouse, Lever, Ashby, Workday) to open the **Chrome Side Panel**. On LinkedIn, the content script is injected only after you open the panel on the job page.

---

## 🌟 Key Features

### 1. Persistent Chrome Side Panel (`chrome.sidePanel`)
- Sits right beside any job posting on your screen.
- **Job Analyzer**: Computes real-time ATS match scores, highlights candidate strengths, and detects missing keyword gaps.
- **AI Career Copilot**: Multi-turn conversational interview coach (STAR method), salary negotiation advisor, and outreach drafter.
- **Q&A Memory Bank**: 1-click retrieval and copying of stored application essay answers.
- **Candidate Profile**: Configures candidate name, email, LinkedIn, GitHub, portfolio, and work authorization.

### 2. In-DOM 1-Click Form Autofiller
- Injects a floating helper badge on supported ATS portals.
- Automatically maps and fills standard form fields (First Name, Last Name, Email, Phone, LinkedIn, GitHub, Portfolio, Work Authorization).
- Intelligently detects essay textareas and pulls tailored answers from the Q&A memory bank.

### 3. Hybrid Dual-Mode Runtime
- **Mode A: Connected Agent Mode**: Connects to the local FastAPI backend (`http://localhost:8000`), leveraging PostgreSQL/SQLite, dynamic model routing, and the semantic cache.
- **Mode B: Standalone Zero-Backend Mode**: Runs 100% on-device inside Chrome using built-in Gemini Nano (`window.ai.languageModel`) and `chrome.storage.local` without needing the backend server running.

---

## ⚖️ Responsible Use

* Autofill only writes values **you** have provided (profile fields / saved Q&A). Nothing is assumed on your behalf.
* The extension never submits an application — it pre-fills fields and you click submit.
* Job-description text is treated as untrusted data; generated answers are sanitized before display/fill.
* You are responsible for complying with the terms of the sites you visit.

## 📁 File Structure

```
extension/
├── manifest.json              # Chrome Manifest V3 configuration
├── background/
│   └── service-worker.js      # Background service worker & sidePanel trigger
├── content/
│   ├── ats-autofill.js        # In-DOM scraper & form filler
│   └── ats-autofill.css       # Floating badge & field highlight styles
├── js/
│   └── runtime-adapter.js     # Hybrid runtime adapter (FastAPI vs Gemini Nano)
├── sidepanel/
│   ├── sidepanel.html         # Multi-tab side panel UI
│   ├── sidepanel.css          # Glassmorphic dark-theme styles
│   └── sidepanel.js           # Interactive controller
└── icons/
    ├── icon-16.png            # 16x16 icon
    ├── icon-48.png            # 48x48 icon
    └── icon-128.png           # 128x128 icon
```
