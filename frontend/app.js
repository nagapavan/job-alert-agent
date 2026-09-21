// -------------------------------------------------------------
// Job Alert Agent - Single Page Application Engine
// -------------------------------------------------------------

const API_BASE = window.location.origin;

let state = {
    companies: [],
    catalog: [],
    jobs: [],
    selectedJobIds: new Set(),
    resume: null,
    resumes: [],
    activeJobModal: null,
    activeLLM: null,
    feedTab: "all",
    trackerFilter: "in_progress", // 'in_progress', 'shortlisted', 'all', 'rejected', 'not_interested'
    companyFilter: "all", // 'all', 'direct', 'network'
    companySearchQuery: "",
    companyLayout: localStorage.getItem("job_agent_company_layout") || "grid", // 'grid', 'list'
    targetRoles: JSON.parse(localStorage.getItem("job_agent_target_roles") || '["Software Engineer", "Backend", "Full Stack"]'),
    targetCities: JSON.parse(localStorage.getItem("job_agent_target_cities") || '["Bengaluru", "Hyderabad"]'),
    targetCountry: localStorage.getItem("job_agent_target_country") || "India",
    targetWorkMode: localStorage.getItem("job_agent_target_workmode") || "all",
    userTimezone: localStorage.getItem("job_agent_timezone") || (Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC"),
    criteriaFilterActive: localStorage.getItem("job_agent_criteria_filter_active") !== "false"
};

let configuredGoogleQueries = [];

document.addEventListener("DOMContentLoaded", () => {
    initNavigation();
    initEventListeners();
    initModal();
    initTargetPreferences();
    initConsentGate();
    loadPreferencesFromServer();
    fetchInitialData();
});

// -------------------------------------------------------------
// Navigation & Section Toggling
// -------------------------------------------------------------
function initNavigation() {
    const navItems = document.querySelectorAll(".nav-item");
    const sections = document.querySelectorAll(".content-section");
    const titles = {
        dashboard: { title: "Job Feed & Discovery", sub: "Real-time alerts, ATS match scores, due diligence checks, and assisted applications." },
        tracker: { title: "Application Lifecycle Tracker", sub: "Track application statuses, interview schedules, and regret responses." },
        companies: { title: "Target Companies & Portals", sub: "Configure and manage organizations monitored for opportunities." },
        resume: { title: "Applicant Resume & Profile", sub: "Encrypted profile storage and AI ATS extraction breakdown." },
        tasks: { title: "Background Tasks, Queue & Scheduled Automations", sub: "Track asynchronous discovery scans, LLM queue depth, and hands-free cron schedules." },
        settings: { title: "Settings", sub: "Discovery preferences, opt-in features, BYOK cloud models, and Gmail API." }
    };

    navItems.forEach(item => {
        item.addEventListener("click", () => {
            navItems.forEach(n => n.classList.remove("active"));
            item.classList.add("active");

            const targetSection = item.getAttribute("data-section");
            sections.forEach(sec => {
                if (sec.id === `${targetSection}-section`) {
                    sec.classList.remove("hidden");
                } else {
                    sec.classList.add("hidden");
                }
            });

            if (titles[targetSection]) {
                document.getElementById("page-title").innerText = titles[targetSection].title;
                document.getElementById("page-subtitle").innerText = titles[targetSection].sub;
            }

            if (targetSection === "tracker") {
                renderTrackerList();
            } else if (targetSection === "companies") {
                fetchCompanyCatalog();
                fetchCompanies();
            } else if (targetSection === "tasks") {
                fetchTasksStatus(true);
            } else if (targetSection === "settings") {
                populateSettingsPanel();
            }
        });
    });
}

// -------------------------------------------------------------
// Event Listeners Initialization
// -------------------------------------------------------------
function initEventListeners() {
    // 1. Scan & Scrape Button
    const scrapeBtn = document.getElementById("trigger-scrape-btn");
    if (scrapeBtn) {
        scrapeBtn.addEventListener("click", triggerScrape);
    }

    // 1b. LinkedIn Sync Button
    const linkedinSyncBtn = document.getElementById("trigger-linkedin-sync-btn");
    if (linkedinSyncBtn) {
        linkedinSyncBtn.addEventListener("click", triggerLinkedInSync);
    }

    // 2. Add Company Form & File Import
    const companyForm = document.getElementById("add-company-form");
    if (companyForm) {
        companyForm.addEventListener("submit", handleAddCompany);
    }
    const companyFileInput = document.getElementById("company-file-input");
    if (companyFileInput) {
        companyFileInput.addEventListener("change", handleCompanyFileImport);
    }
    const editCompanyForm = document.getElementById("edit-company-form");
    if (editCompanyForm) {
        editCompanyForm.addEventListener("submit", saveCompanyEdit);
    }

    // 3. Resume File Upload
    const uploadZone = document.getElementById("upload-zone");
    const fileInput = document.getElementById("resume-file-input");
    if (uploadZone && fileInput) {
        uploadZone.addEventListener("click", () => fileInput.click());
        fileInput.addEventListener("change", handleResumeUpload);

        uploadZone.addEventListener("dragover", (e) => {
            e.preventDefault();
            uploadZone.style.borderColor = "var(--primary)";
        });
        uploadZone.addEventListener("dragleave", () => {
            uploadZone.style.borderColor = "var(--border-color)";
        });
        uploadZone.addEventListener("drop", (e) => {
            e.preventDefault();
            uploadZone.style.borderColor = "var(--border-color)";
            if (e.dataTransfer.files.length) {
                fileInput.files = e.dataTransfer.files;
                handleResumeUpload({ target: fileInput });
            }
        });
    }

    const uploadAnotherBtn = document.getElementById("btn-upload-another-resume");
    if (uploadAnotherBtn && fileInput) {
        uploadAnotherBtn.addEventListener("click", () => fileInput.click());
    }

    const deleteResumeBtn = document.getElementById("btn-delete-active-resume");
    if (deleteResumeBtn) {
        deleteResumeBtn.addEventListener("click", deleteActiveResume);
    }

    const resumeDropdown = document.getElementById("resume-select-dropdown");
    if (resumeDropdown) {
        resumeDropdown.addEventListener("change", (e) => {
            if (e.target.value) activateResume(parseInt(e.target.value));
        });
    }

    // 4. Search, Preference and Filter Controls
    const searchInput = document.getElementById("job-search-input");
    const statusFilter = document.getElementById("status-filter");
    const scoreFilter = document.getElementById("score-filter");

    const savedTitles = localStorage.getItem("job_target_titles");
    if (savedTitles !== null && document.getElementById("target-titles-input")) {
        document.getElementById("target-titles-input").value = savedTitles;
    }
    const savedCountry = localStorage.getItem("job_target_country");
    if (savedCountry !== null && document.getElementById("target-country-select")) {
        document.getElementById("target-country-select").value = savedCountry;
    }

    const targetTitlesInput = document.getElementById("target-titles-input");
    if (targetTitlesInput) {
        targetTitlesInput.addEventListener("input", () => {
            localStorage.setItem("job_target_titles", targetTitlesInput.value);
            applyJobFilters();
        });
    }

    const targetCountrySelect = document.getElementById("target-country-select");
    if (targetCountrySelect) {
        targetCountrySelect.addEventListener("change", () => {
            localStorage.setItem("job_target_country", targetCountrySelect.value);
            applyJobFilters();
        });
    }

    if (searchInput) searchInput.addEventListener("input", applyJobFilters);
    if (statusFilter) statusFilter.addEventListener("change", applyJobFilters);
    if (scoreFilter) scoreFilter.addEventListener("change", applyJobFilters);

    // 5. Tracker Filter Tabs
    document.querySelectorAll("#tracker-section .tab-btn").forEach(btn => {
        btn.addEventListener("click", () => {
            document.querySelectorAll("#tracker-section .tab-btn").forEach(b => b.classList.remove("active"));
            btn.classList.add("active");
            setTrackerFilter(btn.getAttribute("data-filter"));
        });
    });

    // 6. Modal Drawer Clipboard Buttons
    const copyCoverBtn = document.getElementById("copy-cover-letter-btn");
    if (copyCoverBtn) {
        copyCoverBtn.addEventListener("click", () => {
            const text = document.getElementById("modal-cover-letter-text")?.value || "";
            copyToClipboard(text, "Cover letter copied to clipboard!");
        });
    }

    const copyColdBtn = document.getElementById("copy-cold-msg-btn");
    if (copyColdBtn) {
        copyColdBtn.addEventListener("click", () => {
            const text = document.getElementById("modal-cold-msg-text")?.value || "";
            copyToClipboard(text, "Outreach note copied to clipboard!");
        });
    }

    // 7. Modal Interview Chat Form
    const chatForm = document.getElementById("chat-input-form");
    if (chatForm) {
        chatForm.addEventListener("submit", handleChatSubmit);
    }

    // 8. Modal Launch Apply Button
    const modalApplyBtn = document.getElementById("modal-apply-browser-btn");
    if (modalApplyBtn) {
        modalApplyBtn.addEventListener("click", () => {
            if (state.activeJobModal) {
                launchPlaywrightApply(state.activeJobModal.id);
            }
        });
    }
}

// -------------------------------------------------------------
// Modal & Slide-Over Drawer
// -------------------------------------------------------------
function initModal() {
    const modal = document.getElementById("details-modal");
    const closeBtn = document.getElementById("modal-close-btn");
    const tabBtns = document.querySelectorAll(".modal-tab-btn");
    const tabContents = document.querySelectorAll(".modal-tab-content");

    if (closeBtn && modal) {
        closeBtn.addEventListener("click", () => modal.classList.add("hidden"));
    }
    if (modal) {
        modal.addEventListener("click", (e) => {
            if (e.target === modal) modal.classList.add("hidden");
        });
    }

    const editModal = document.getElementById("edit-company-modal");
    if (editModal) {
        editModal.addEventListener("click", (e) => {
            if (e.target === editModal) closeEditCompanyModal();
        });
    }

    tabBtns.forEach(btn => {
        btn.addEventListener("click", () => {
            tabBtns.forEach(b => b.classList.remove("active"));
            tabContents.forEach(c => c.classList.remove("active"));

            btn.classList.add("active");
            const target = btn.getAttribute("data-tab");
            document.getElementById(`tab-${target}`).classList.add("active");
        });
    });
}

// -------------------------------------------------------------
// Initial Data Fetching & Onboarding Triage
// -------------------------------------------------------------
async function fetchInitialData() {
    try {
        await Promise.all([
            fetchHealthAndLLMStatus(),
            fetchCompanyCatalog(),
            fetchCompanies(),
            fetchJobs(),
            fetchResume(),
            fetchQAMemory(),
            fetchDismissedPatterns(),
            fetchGoogleJobQueries()
        ]);

        if (!state.resume || state.resumes.length === 0) {
            activateOnboardingWizard();
        } else {
            showToast("System ready & synchronized.");
        }
    } catch (e) {
        console.error("Failed to load initial data", e);
    }
}

// -------------------------------------------------------------
// Global Countries & Tech Hubs Catalog (Deterministic, Zero-Token)
// -------------------------------------------------------------
const COUNTRIES_HUBS_DATA = {
    "all": {
        name: "All Countries (Global)",
        flag: "🌍",
        hubs: ["Bengaluru", "San Francisco", "London", "New York", "Berlin", "Singapore", "Toronto", "Sydney", "Amsterdam", "Seattle", "Dublin", "Paris", "Zurich", "Tokyo", "Dubai"]
    },
    "India": {
        name: "India",
        flag: "🇮🇳",
        hubs: ["Bengaluru", "Hyderabad", "Pune", "Delhi NCR", "Mumbai", "Chennai", "Noida", "Gurgaon", "Kolkata", "Ahmedabad"]
    },
    "United States": {
        name: "United States",
        flag: "🇺🇸",
        hubs: ["San Francisco", "Seattle", "New York", "Austin", "Boston", "Los Angeles", "Chicago", "Denver", "San Diego", "Atlanta", "Dallas"]
    },
    "United Kingdom": {
        name: "United Kingdom",
        flag: "🇬🇧",
        hubs: ["London", "Manchester", "Cambridge", "Oxford", "Edinburgh", "Bristol", "Birmingham", "Belfast"]
    },
    "Germany": {
        name: "Germany",
        flag: "🇩🇪",
        hubs: ["Berlin", "Munich", "Frankfurt", "Hamburg", "Cologne", "Stuttgart", "Düsseldorf"]
    },
    "Canada": {
        name: "Canada",
        flag: "🇨🇦",
        hubs: ["Toronto", "Vancouver", "Montreal", "Ottawa", "Waterloo", "Calgary", "Edmonton"]
    },
    "Singapore": {
        name: "Singapore",
        flag: "🇸🇬",
        hubs: ["Singapore"]
    },
    "Australia": {
        name: "Australia",
        flag: "🇦🇺",
        hubs: ["Sydney", "Melbourne", "Brisbane", "Perth", "Adelaide", "Canberra"]
    },
    "Netherlands": {
        name: "Netherlands",
        flag: "🇳🇱",
        hubs: ["Amsterdam", "Rotterdam", "Utrecht", "Eindhoven", "The Hague"]
    },
    "Ireland": {
        name: "Ireland",
        flag: "🇮🇪",
        hubs: ["Dublin", "Cork", "Galway", "Limerick"]
    },
    "France": {
        name: "France",
        flag: "🇫🇷",
        hubs: ["Paris", "Lyon", "Toulouse", "Nantes", "Bordeaux", "Lille"]
    },
    "Switzerland": {
        name: "Switzerland",
        flag: "🇨🇭",
        hubs: ["Zurich", "Geneva", "Lausanne", "Basel", "Bern"]
    },
    "United Arab Emirates": {
        name: "United Arab Emirates",
        flag: "🇦🇪",
        hubs: ["Dubai", "Abu Dhabi"]
    },
    "Japan": {
        name: "Japan",
        flag: "🇯🇵",
        hubs: ["Tokyo", "Osaka", "Fukuoka", "Kyoto", "Yokohama"]
    },
    "Sweden": {
        name: "Sweden",
        flag: "🇸🇪",
        hubs: ["Stockholm", "Gothenburg", "Malmo", "Uppsala"]
    },
    "Poland": {
        name: "Poland",
        flag: "🇵🇱",
        hubs: ["Warsaw", "Krakow", "Wroclaw", "Gdansk", "Poznan"]
    },
    "Spain": {
        name: "Spain",
        flag: "🇪🇸",
        hubs: ["Madrid", "Barcelona", "Valencia", "Malaga", "Seville"]
    },
    "Israel": {
        name: "Israel",
        flag: "🇮🇱",
        hubs: ["Tel Aviv", "Jerusalem", "Haifa", "Herzliya"]
    },
    "Brazil": {
        name: "Brazil",
        flag: "🇧🇷",
        hubs: ["Sao Paulo", "Rio de Janeiro", "Curitiba", "Belo Horizonte", "Florianopolis"]
    },
    "Estonia": {
        name: "Estonia",
        flag: "🇪🇪",
        hubs: ["Tallinn", "Tartu"]
    },
    "Denmark": {
        name: "Denmark",
        flag: "🇩🇰",
        hubs: ["Copenhagen", "Aarhus"]
    },
    "Norway": {
        name: "Norway",
        flag: "🇳🇴",
        hubs: ["Oslo", "Bergen", "Trondheim"]
    },
    "Finland": {
        name: "Finland",
        flag: "🇫🇮",
        hubs: ["Helsinki", "Espoo", "Tampere"]
    },
    "Austria": {
        name: "Austria",
        flag: "🇦🇹",
        hubs: ["Vienna", "Graz", "Linz", "Salzburg"]
    },
    "Belgium": {
        name: "Belgium",
        flag: "🇧🇪",
        hubs: ["Brussels", "Antwerp", "Ghent", "Leuven"]
    },
    "Portugal": {
        name: "Portugal",
        flag: "🇵🇹",
        hubs: ["Lisbon", "Porto", "Braga"]
    },
    "Italy": {
        name: "Italy",
        flag: "🇮🇹",
        hubs: ["Milan", "Rome", "Turin", "Bologna"]
    },
    "New Zealand": {
        name: "New Zealand",
        flag: "🇳🇿",
        hubs: ["Auckland", "Wellington", "Christchurch"]
    },
    "South Africa": {
        name: "South Africa",
        flag: "🇿🇦",
        hubs: ["Cape Town", "Johannesburg", "Pretoria", "Durban"]
    },
    "Mexico": {
        name: "Mexico",
        flag: "🇲🇽",
        hubs: ["Mexico City", "Guadalajara", "Monterrey"]
    },
    "Argentina": {
        name: "Argentina",
        flag: "🇦🇷",
        hubs: ["Buenos Aires", "Cordoba", "Rosario"]
    },
    "Chile": {
        name: "Chile",
        flag: "🇨🇱",
        hubs: ["Santiago", "Valparaiso"]
    },
    "Colombia": {
        name: "Colombia",
        flag: "🇨🇴",
        hubs: ["Bogota", "Medellin", "Cali"]
    },
    "South Korea": {
        name: "South Korea",
        flag: "🇰🇷",
        hubs: ["Seoul", "Pangyo", "Busan"]
    },
    "Taiwan": {
        name: "Taiwan",
        flag: "🇹🇼",
        hubs: ["Taipei", "Hsinchu", "Taichung"]
    },
    "Hong Kong": {
        name: "Hong Kong",
        flag: "🇭🇰",
        hubs: ["Hong Kong"]
    },
    "Malaysia": {
        name: "Malaysia",
        flag: "🇲🇾",
        hubs: ["Kuala Lumpur", "Penang", "Cyberjaya"]
    },
    "Indonesia": {
        name: "Indonesia",
        flag: "🇮🇩",
        hubs: ["Jakarta", "Bandung", "Bali"]
    },
    "Philippines": {
        name: "Philippines",
        flag: "🇵🇭",
        hubs: ["Manila", "Cebu", "Taguig"]
    },
    "Thailand": {
        name: "Thailand",
        flag: "🇹🇭",
        hubs: ["Bangkok", "Chiang Mai"]
    },
    "Vietnam": {
        name: "Vietnam",
        flag: "🇻🇳",
        hubs: ["Ho Chi Minh City", "Hanoi", "Da Nang"]
    },
    "Saudi Arabia": {
        name: "Saudi Arabia",
        flag: "🇸🇦",
        hubs: ["Riyadh", "Jeddah", "Dammam"]
    },
    "Egypt": {
        name: "Egypt",
        flag: "🇪🇬",
        hubs: ["Cairo", "Alexandria", "Giza"]
    },
    "Nigeria": {
        name: "Nigeria",
        flag: "🇳🇬",
        hubs: ["Lagos", "Abuja"]
    },
    "Kenya": {
        name: "Kenya",
        flag: "🇰🇪",
        hubs: ["Nairobi", "Mombasa"]
    },
    "Czech Republic": {
        name: "Czech Republic",
        flag: "🇨🇿",
        hubs: ["Prague", "Brno"]
    },
    "Romania": {
        name: "Romania",
        flag: "🇷🇴",
        hubs: ["Bucharest", "Cluj-Napoca", "Timisoara", "Iasi"]
    },
    "Hungary": {
        name: "Hungary",
        flag: "🇭🇺",
        hubs: ["Budapest", "Debrecen"]
    },
    "Greece": {
        name: "Greece",
        flag: "🇬🇷",
        hubs: ["Athens", "Thessaloniki"]
    },
    "Turkey": {
        name: "Turkey",
        flag: "🇹🇷",
        hubs: ["Istanbul", "Ankara", "Izmir"]
    }
};

// -------------------------------------------------------------
// Target Location Helpers (country scoping & legacy remote cleanup)
// -------------------------------------------------------------
// Resolves a city to its owning country entry, or null if unknown.
function countryInfoForCity(city) {
    if (!city) return null;
    const needle = city.trim().toLowerCase();
    if (!needle) return null;
    for (const key of Object.keys(COUNTRIES_HUBS_DATA)) {
        if (key === "all") continue;
        const entry = COUNTRIES_HUBS_DATA[key];
        if ((entry.hubs || []).some(h => h.toLowerCase() === needle)) {
            return { key, name: entry.name, flag: entry.flag };
        }
    }
    return null;
}

// Legacy cleanup: "Remote" used to be stored as a global pseudo-city. Remote eligibility
// is now owned by Work Mode, so drop it from any persisted city list.
function sanitizeTargetCities() {
    const cleaned = state.targetCities.filter(c => c && c.toLowerCase().trim() !== "remote");
    if (cleaned.length !== state.targetCities.length) {
        state.targetCities = cleaned;
        localStorage.setItem("job_agent_target_cities", JSON.stringify(state.targetCities));
        return true;
    }
    return false;
}

// Cities currently selected that do not belong to the active country selection.
function foreignSelectedCities(countryKey) {
    if (!countryKey || countryKey === "all") return [];
    return state.targetCities.filter(city => {
        const info = countryInfoForCity(city);
        return info && info.key !== countryKey;
    });
}

// -------------------------------------------------------------
// Target Preference & Role Keywords Multi-Select Handlers
// -------------------------------------------------------------
function populateCountrySelects() {
    const countryEl = document.getElementById("target-country-select");
    const obCountryEl = document.getElementById("onboarding-target-country");

    const optionsHtml = Object.keys(COUNTRIES_HUBS_DATA).map(key => {
        const item = COUNTRIES_HUBS_DATA[key];
        return `<option value="${escapeHTML(key)}">${item.flag} ${escapeHTML(item.name)}</option>`;
    }).join("");

    if (countryEl) countryEl.innerHTML = optionsHtml;
    if (obCountryEl) obCountryEl.innerHTML = optionsHtml;
}

function renderDynamicCityPills(countryKey) {
    const mainPills = document.getElementById("quick-cities-pills");
    const obPills = document.getElementById("onboarding-quick-cities-pills");

    const selectedData = COUNTRIES_HUBS_DATA[countryKey] || COUNTRIES_HUBS_DATA["all"];
    const hubs = selectedData.hubs || [];
    const selected = new Set(state.targetCities.map(c => c.toLowerCase()));

    const pillsHtml = `
        <span class="pills-label">Quick Add ${escapeHTML(selectedData.name)} Hubs:</span>
        ${hubs.map(hub => {
            const active = selected.has(hub.toLowerCase());
            const cls = active ? "city-pill active" : "city-pill";
            const label = active ? `✓ ${escapeHTML(hub)}` : `+ ${escapeHTML(hub)}`;
            return `<button type="button" class="${cls}" onclick="addCityKeyword('${escapeHTML(hub)}')"${active ? ' title="Already selected"' : ''}>${label}</button>`;
        }).join(" ")}
    `;

    if (mainPills) mainPills.innerHTML = pillsHtml;
    if (obPills) obPills.innerHTML = pillsHtml;
}

function updateCityDatalist(countryKey) {
    const datalist = document.getElementById("city-suggestions-list");
    if (!datalist) return;

    const selectedData = COUNTRIES_HUBS_DATA[countryKey] || COUNTRIES_HUBS_DATA["all"];
    const hubs = selectedData.hubs || [];

    datalist.innerHTML = hubs.map(hub => `<option value="${escapeHTML(hub)}">${escapeHTML(hub)}</option>`).join("");
}

function handleCountrySelectChange(countryVal) {
    state.targetCountry = countryVal;
    localStorage.setItem("job_agent_target_country", state.targetCountry);

    const countryEl = document.getElementById("target-country-select");
    const obCountryEl = document.getElementById("onboarding-target-country");
    if (countryEl && countryEl.value !== countryVal) countryEl.value = countryVal;
    if (obCountryEl && obCountryEl.value !== countryVal) obCountryEl.value = countryVal;

    renderDynamicCityPills(countryVal);
    updateCityDatalist(countryVal);
    renderTargetCitiesChips();
    updateCriteriaSummaryBar();
    schedulePreferencesPersist();
    applyJobFilters();
}

function initTargetPreferences() {
    sanitizeTargetCities();
    populateCountrySelects();

    const countryEl = document.getElementById("target-country-select");
    const workModeEl = document.getElementById("target-workmode-select");
    const obCountryEl = document.getElementById("onboarding-target-country");
    const obWorkModeEl = document.getElementById("onboarding-work-mode");

    if (countryEl) countryEl.value = state.targetCountry;
    if (workModeEl) workModeEl.value = state.targetWorkMode;
    if (obCountryEl) obCountryEl.value = state.targetCountry;
    if (obWorkModeEl) obWorkModeEl.value = state.targetWorkMode;

    renderTargetRolesChips();
    renderTargetCitiesChips();
    renderDynamicCityPills(state.targetCountry);
    updateCityDatalist(state.targetCountry);
    updateCriteriaSummaryBar();
}

function updateCriteriaSummaryBar() {
    const summaryContainer = document.getElementById("criteria-summary-tags");
    const toggleBtn = document.getElementById("btn-toggle-criteria-filter");
    const toggleText = document.getElementById("criteria-filter-btn-text");

    if (toggleBtn && toggleText) {
        if (state.criteriaFilterActive) {
            toggleBtn.className = "btn btn-sm criteria-btn active-filter";
            toggleText.innerText = "Filter: Active";
        } else {
            toggleBtn.className = "btn btn-sm criteria-btn inactive-filter";
            toggleText.innerText = "Show All Jobs";
        }
    }

    if (!summaryContainer) return;

    const rolesStr = state.targetRoles.length > 0 
        ? state.targetRoles.slice(0, 3).join(", ") + (state.targetRoles.length > 3 ? ` +${state.targetRoles.length - 3}` : '')
        : "All Roles";
        
    const countryData = COUNTRIES_HUBS_DATA[state.targetCountry] || { flag: "🌍", name: "Global" };
    const citiesStr = state.targetCities.length > 0
        ? state.targetCities.slice(0, 2).join(", ") + (state.targetCities.length > 2 ? ` +${state.targetCities.length - 2}` : '')
        : (state.targetCountry === "all" || !state.targetCountry ? "Global" : `${countryData.flag} ${countryData.name}`);

    const modeStr = (!state.targetWorkMode || state.targetWorkMode === "all")
        ? "Any Mode"
        : (state.targetWorkMode.charAt(0).toUpperCase() + state.targetWorkMode.slice(1));

    summaryContainer.innerHTML = `
        <span class="criteria-tag">🎯 <strong>Roles:</strong> ${escapeHTML(rolesStr)}</span>
        <span class="criteria-tag">🏙️ <strong>Location:</strong> ${escapeHTML(citiesStr)}</span>
        <span class="criteria-tag">💼 <strong>Mode:</strong> ${escapeHTML(modeStr)}</span>
    `;
}

function toggleCriteriaFilter() {
    state.criteriaFilterActive = !state.criteriaFilterActive;
    localStorage.setItem("job_agent_criteria_filter_active", state.criteriaFilterActive);
    updateCriteriaSummaryBar();
    applyJobFilters();
    showToast(state.criteriaFilterActive ? "🎯 Filter active: Showing jobs matching your target preferences." : "🌐 Showing all ingested opportunities across the feed.");
}

function navigateToSection(sectionId) {
    const navBtn = document.querySelector(`.nav-item[data-section="${sectionId}"]`);
    if (navBtn) {
        navBtn.click();
    }
}

function renderTargetRolesChips() {
    const mainContainer = document.getElementById("target-roles-chips");
    const onboardingContainer = document.getElementById("onboarding-roles-chips");
    const countBadge = document.getElementById("badge-roles-count");

    if (countBadge) {
        countBadge.innerText = `${state.targetRoles.length} Active`;
    }

    const chipsHtml = state.targetRoles.length === 0
        ? `<span style="font-size:12px; color:var(--text-dim); font-style:italic;">No roles selected yet. Add keywords or click quick pills below.</span>`
        : state.targetRoles.map((role, idx) => `
            <span class="role-chip">
                <span>${escapeHTML(role)}</span>
                <span class="role-chip-remove" onclick="removeRoleKeyword(${idx})" title="Remove role">✕</span>
            </span>
        `).join("");

    if (mainContainer) mainContainer.innerHTML = chipsHtml;
    if (onboardingContainer) onboardingContainer.innerHTML = chipsHtml;
    updateCriteriaSummaryBar();
}

function renderTargetCitiesChips() {
    const mainContainer = document.getElementById("target-cities-chips");
    const onboardingContainer = document.getElementById("onboarding-cities-chips");
    const countBadge = document.getElementById("badge-cities-count");

    if (countBadge) {
        countBadge.innerText = `${state.targetCities.length} Cities`;
    }

    const chipsHtml = state.targetCities.length === 0
        ? `<span style="font-size:12px; color:var(--text-dim); font-style:italic;">No cities specified (filtering by country/global). Add cities or click quick pills below.</span>`
        : state.targetCities.map((city, idx) => {
            const info = countryInfoForCity(city);
            const flag = info ? `${info.flag} ` : "";
            const countryTitle = info ? ` title="${escapeHTML(info.name)}"` : "";
            return `
            <span class="city-chip"${countryTitle}>
                <span>📍 ${flag}${escapeHTML(city)}</span>
                <span class="city-chip-remove" onclick="removeCityKeyword(${idx})" title="Remove city">✕</span>
            </span>
        `;
        }).join("");

    if (mainContainer) mainContainer.innerHTML = chipsHtml;
    if (onboardingContainer) onboardingContainer.innerHTML = chipsHtml;
    renderForeignCitiesNotice();
    updateCriteriaSummaryBar();
}

// Shows a non-destructive nudge when selected cities fall outside the active country.
function renderForeignCitiesNotice() {
    const foreign = foreignSelectedCities(state.targetCountry);
    const html = foreign.length === 0 ? "" : `
        <span class="foreign-cities-text">⚠️ <strong>${foreign.length}</strong> selected ${foreign.length === 1 ? "city is" : "cities are"} outside <strong>${escapeHTML(state.targetCountry)}</strong>: ${foreign.map(c => escapeHTML(c)).join(", ")}</span>
        <button type="button" class="btn btn-sm btn-outline" onclick="clearForeignCities()">Clear these</button>
    `;
    ["foreign-cities-notice", "onboarding-foreign-cities-notice"].forEach(id => {
        const el = document.getElementById(id);
        if (el) {
            el.innerHTML = html;
            el.style.display = foreign.length === 0 ? "none" : "";
        }
    });
}

function clearForeignCities() {
    const foreignSet = new Set(foreignSelectedCities(state.targetCountry).map(c => c.toLowerCase()));
    if (foreignSet.size === 0) return;
    state.targetCities = state.targetCities.filter(c => !foreignSet.has(c.toLowerCase()));
    localStorage.setItem("job_agent_target_cities", JSON.stringify(state.targetCities));
    schedulePreferencesPersist();
    renderTargetCitiesChips();
    applyJobFilters();
    showToast(`🧹 Removed ${foreignSet.size} city(ies) outside ${state.targetCountry}.`);
}

function clearAllCities() {
    if (state.targetCities.length === 0) return;
    if (!confirm("Remove all selected cities? Filtering will fall back to your country/region.")) return;
    state.targetCities = [];
    localStorage.setItem("job_agent_target_cities", JSON.stringify(state.targetCities));
    schedulePreferencesPersist();
    renderTargetCitiesChips();
    applyJobFilters();
    showToast("🧹 Cleared all target cities.");
}

let roleSuggestions = [];

async function suggestRelatedRoles() {
    const container = document.getElementById("role-suggestions");
    if (!container) return;
    if (!state.targetRoles.length) {
        showToast("⚠️ Add at least one target role first.");
        return;
    }
    container.innerHTML = `<span class="pills-label">✨ Finding related titles…</span>`;
    try {
        const res = await fetch(`${API_BASE}/api/roles/enrich`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ titles: state.targetRoles })
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "Failed to suggest roles");
        roleSuggestions = data.suggestions || [];
        if (!roleSuggestions.length) {
            container.innerHTML = `<span class="pills-label">Your target roles already cover the common variants.</span>`;
            return;
        }
        container.innerHTML = `<span class="pills-label">Suggested related titles (click to add):</span>` +
            roleSuggestions.map((s, i) =>
                `<button type="button" class="role-pill" data-idx="${i}" title="${escapeHTML(s.reason)}" onclick="addSuggestedRole(${i})">+ ${escapeHTML(s.title)}</button>`
            ).join(" ");
    } catch (err) {
        container.innerHTML = "";
        showToast(`⚠️ Could not suggest roles: ${err.message}`);
    }
}

function addSuggestedRole(index) {
    const s = roleSuggestions[index];
    if (!s) return;
    addRoleKeyword(s.title);
    const btn = document.querySelector(`#role-suggestions button[data-idx="${index}"]`);
    if (btn) btn.remove();
}

function addRoleKeyword(role) {
    if (!role) return;
    const clean = role.trim();
    if (!clean) return;
    
    if (!state.targetRoles.some(r => r.toLowerCase() === clean.toLowerCase())) {
        state.targetRoles.push(clean);
        localStorage.setItem("job_agent_target_roles", JSON.stringify(state.targetRoles));
        schedulePreferencesPersist();
        renderTargetRolesChips();
        applyJobFilters();
    }
}

function removeRoleKeyword(index) {
    if (index >= 0 && index < state.targetRoles.length) {
        state.targetRoles.splice(index, 1);
        localStorage.setItem("job_agent_target_roles", JSON.stringify(state.targetRoles));
        schedulePreferencesPersist();
        renderTargetRolesChips();
        applyJobFilters();
    }
}

function addCityKeyword(city) {
    if (!city) return;
    const clean = city.trim();
    if (!clean) return;

    // "Remote" is not a location: remote eligibility is owned by the Work Mode selector.
    if (clean.toLowerCase() === "remote") {
        showToast("🌐 Remote is set via the Work Mode dropdown, not as a city.");
        return;
    }

    if (!state.targetCities.some(c => c.toLowerCase() === clean.toLowerCase())) {
        state.targetCities.push(clean);
        localStorage.setItem("job_agent_target_cities", JSON.stringify(state.targetCities));
        schedulePreferencesPersist();
        renderTargetCitiesChips();
        renderDynamicCityPills(state.targetCountry);
        applyJobFilters();
    }
}

function removeCityKeyword(index) {
    if (index >= 0 && index < state.targetCities.length) {
        state.targetCities.splice(index, 1);
        localStorage.setItem("job_agent_target_cities", JSON.stringify(state.targetCities));
        schedulePreferencesPersist();
        renderTargetCitiesChips();
        renderDynamicCityPills(state.targetCountry);
        applyJobFilters();
    }
}

function handleAddRoleFromInput() {
    const input = document.getElementById("add-role-input");
    if (!input) return;
    const val = input.value.trim();
    if (val) {
        val.split(",").forEach(t => addRoleKeyword(t.trim()));
        input.value = "";
    }
}

function handleAddRoleFromOnboardingInput() {
    const input = document.getElementById("onboarding-role-input");
    if (!input) return;
    const val = input.value.trim();
    if (val) {
        val.split(",").forEach(t => addRoleKeyword(t.trim()));
        input.value = "";
    }
}

function handleAddCityFromInput() {
    const input = document.getElementById("add-city-input");
    if (!input) return;
    const val = input.value.trim();
    if (val) {
        val.split(",").forEach(t => addCityKeyword(t.trim()));
        input.value = "";
    }
}

function handleAddCityFromOnboardingInput() {
    const input = document.getElementById("onboarding-city-input");
    if (!input) return;
    const val = input.value.trim();
    if (val) {
        val.split(",").forEach(t => addCityKeyword(t.trim()));
        input.value = "";
    }
}

function handleTargetPrefChange() {
    const countryEl = document.getElementById("target-country-select");
    const workModeEl = document.getElementById("target-workmode-select");
    
    if (countryEl) {
        state.targetCountry = countryEl.value;
        localStorage.setItem("job_agent_target_country", state.targetCountry);
    }
    if (workModeEl) {
        state.targetWorkMode = workModeEl.value;
        localStorage.setItem("job_agent_target_workmode", state.targetWorkMode);
    }
    schedulePreferencesPersist();
    applyJobFilters();
}

// -------------------------------------------------------------
// Guided Onboarding Wizard
// -------------------------------------------------------------
let onboardingState = {
    step: 1,
    skills: [],
    candidateName: "",
    email: "",
    experience: "",
    selectedCompanies: new Set()
};

function activateOnboardingWizard() {
    document.querySelectorAll(".content-section").forEach(sec => sec.classList.add("hidden"));
    const obSec = document.getElementById("onboarding-section");
    if (obSec) obSec.classList.remove("hidden");

    document.getElementById("page-title").innerText = "Candidate Profile Setup";
    document.getElementById("page-subtitle").innerText = "Step-by-step profile confirmation, career preferences, and targeted setup.";

    initOnboardingDropZone();
    renderOnboardingCompanies();
    goToOnboardingStep(1);
}

function initOnboardingDropZone() {
    const zone = document.getElementById("onboarding-upload-zone");
    const input = document.getElementById("onboarding-resume-input");
    if (!zone || !input) return;

    zone.addEventListener("dragover", (e) => {
        e.preventDefault();
        zone.classList.add("dragover");
    });
    zone.addEventListener("dragleave", () => zone.classList.remove("dragover"));
    zone.addEventListener("drop", (e) => {
        e.preventDefault();
        zone.classList.remove("dragover");
        if (e.dataTransfer.files.length) {
            handleOnboardingFileUpload(e.dataTransfer.files[0]);
        }
    });

    input.addEventListener("change", (e) => {
        if (e.target.files.length) {
            handleOnboardingFileUpload(e.target.files[0]);
        }
    });
}

async function handleOnboardingFileUpload(file) {
    const loading = document.getElementById("onboarding-resume-loading");
    const preview = document.getElementById("onboarding-resume-preview");
    const zone = document.getElementById("onboarding-upload-zone");
    const nextBtn = document.getElementById("btn-onboarding-step1-next");

    if (loading) loading.classList.remove("hidden");
    if (zone) zone.classList.add("hidden");

    const formData = new FormData();
    formData.append("file", file);

    try {
        const res = await fetch(`${API_BASE}/api/resume/upload`, {
            method: "POST",
            body: formData
        });
        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || "Upload failed");
        }
        const data = await res.json();
        state.resume = data;
        if (!state.resumes.some(r => r.id === data.id)) state.resumes.push(data);

        document.getElementById("onboarding-resume-filename").innerText = data.filename;

        const parsed = data.parsed_json || {};
        onboardingState.candidateName = parsed.name || "";
        onboardingState.email = parsed.email || "";
        onboardingState.experience = parsed.summary || parsed.experience_years || "";
        onboardingState.skills = Array.isArray(parsed.skills) ? [...parsed.skills] : [];

        document.getElementById("onboarding-candidate-name").value = onboardingState.candidateName;
        document.getElementById("onboarding-candidate-email").value = onboardingState.email;
        document.getElementById("onboarding-candidate-exp").value = onboardingState.experience;

        renderOnboardingSkillChips();

        if (preview) preview.classList.remove("hidden");
        if (nextBtn) nextBtn.disabled = false;
        showToast("Resume parsed! Please confirm your profile details.");
    } catch (e) {
        alert("Failed to parse resume: " + e.message);
        if (zone) zone.classList.remove("hidden");
    } finally {
        if (loading) loading.classList.add("hidden");
    }
}

function renderOnboardingSkillChips() {
    const container = document.getElementById("onboarding-skill-chips");
    if (!container) return;

    if (onboardingState.skills.length === 0) {
        container.innerHTML = `<span style="font-size: 12.5px; color: var(--text-dim);">No skills extracted yet. Add key tools below.</span>`;
        return;
    }

    container.innerHTML = onboardingState.skills.map((skill, idx) => `
        <span class="skill-chip-removable">
            ${escapeHTML(skill)}
            <span class="chip-remove" onclick="removeOnboardingSkill(${idx})" title="Remove skill">&times;</span>
        </span>
    `).join("");
}

function removeOnboardingSkill(idx) {
    onboardingState.skills.splice(idx, 1);
    renderOnboardingSkillChips();
}

function addOnboardingSkill() {
    const input = document.getElementById("onboarding-new-skill-input");
    if (!input) return;
    const val = input.value.trim();
    if (val && !onboardingState.skills.includes(val)) {
        onboardingState.skills.push(val);
        input.value = "";
        renderOnboardingSkillChips();
    }
}

function goToOnboardingStep(stepNumber) {
    onboardingState.step = stepNumber;

    [1, 2, 3].forEach(s => {
        const pane = document.getElementById(`onboarding-step-${s}`);
        const node = document.getElementById(`step-node-${s}`);
        const line = document.getElementById(`step-line-${s}`);

        if (pane) {
            if (s === stepNumber) pane.classList.remove("hidden");
            else pane.classList.add("hidden");
        }

        if (node) {
            node.classList.remove("active", "completed");
            if (s < stepNumber) node.classList.add("completed");
            else if (s === stepNumber) node.classList.add("active");
        }

        if (line) {
            line.classList.toggle("active", s < stepNumber);
        }
    });
}

async function proceedToOnboardingStep2() {
    if (!state.resume) {
        alert("Please upload a resume first.");
        return;
    }

    // Save updated name/email/skills to backend
    onboardingState.candidateName = document.getElementById("onboarding-candidate-name")?.value.trim() || "";
    onboardingState.email = document.getElementById("onboarding-candidate-email")?.value.trim() || "";
    onboardingState.experience = document.getElementById("onboarding-candidate-exp")?.value.trim() || "";

    try {
        await fetch(`${API_BASE}/api/resume/${state.resume.id}/skills`, {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                skills: onboardingState.skills,
                candidate_name: onboardingState.candidateName,
                email: onboardingState.email,
                experience_years: onboardingState.experience
            })
        });
    } catch (e) {
        console.warn("Failed to persist skill edits", e);
    }

    goToOnboardingStep(2);
}

function proceedToOnboardingStep3() {
    const country = document.getElementById("onboarding-target-country")?.value || "India";
    const workMode = document.getElementById("onboarding-work-mode")?.value || "all";

    state.targetCountry = country;
    state.targetWorkMode = workMode;

    localStorage.setItem("job_agent_target_country", country);
    localStorage.setItem("job_agent_target_workmode", workMode);
    localStorage.setItem("job_agent_target_roles", JSON.stringify(state.targetRoles));
    // Persist onboarding choices to the server so scheduled scans honor them.
    persistPreferencesToServer();

    const countrySelect = document.getElementById("target-country-select");
    const workModeSelect = document.getElementById("target-workmode-select");
    if (countrySelect) countrySelect.value = country;
    if (workModeSelect) workModeSelect.value = workMode;

    renderTargetRolesChips();
    goToOnboardingStep(3);
}

function renderOnboardingCompanies() {
    const container = document.getElementById("onboarding-companies-list");
    if (!container) return;

    const catalog = state.catalog || [];
    const catalogNames = new Set(catalog.map(c => c.name));
    const extraSelected = [...onboardingState.selectedCompanies]
        .filter(name => !catalogNames.has(name))
        .map(name => ({ name, domain: "" }));
    const items = [...catalog, ...extraSelected];

    if (!items.length) {
        container.innerHTML = `<p class="onboarding-empty-hint">Add your 3&ndash;5 target companies below. You can add or import more anytime from the Target Companies tab.</p>`;
        return;
    }

    container.innerHTML = items.map(c => {
        const isChecked = onboardingState.selectedCompanies.has(c.name);
        return `
            <label class="onboarding-company-item">
                <input type="checkbox" ${isChecked ? 'checked' : ''} onchange="toggleOnboardingCompany('${escapeHTML(c.name)}', this.checked)">
                <span><strong>${escapeHTML(c.name)}</strong> <small style="color:var(--text-dim);">(${escapeHTML(c.domain || '')})</small></span>
            </label>
        `;
    }).join("");
}

function toggleOnboardingCompany(name, isChecked) {
    if (isChecked) onboardingState.selectedCompanies.add(name);
    else onboardingState.selectedCompanies.delete(name);
}

function addOnboardingCompanyFromInput() {
    const input = document.getElementById("onboarding-company-input");
    if (!input) return;
    const name = (input.value || "").trim();
    if (!name) return;
    onboardingState.selectedCompanies.add(name);
    input.value = "";
    renderOnboardingCompanies();
}

async function completeOnboardingAndLaunch() {
    const cleanSlate = document.getElementById("onboarding-clean-slate-chk")?.checked;
    showToast("🚀 Initializing your targeted discovery pipeline...");

    // 1. Clean old generic cache if requested
    if (cleanSlate) {
        try {
            await fetch(`${API_BASE}/api/system/clean-cache`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ clean_unapplied_jobs: true })
            });
        } catch (e) {
            console.warn("Failed to clean cache", e);
        }
    }

    // 2. Track selected organizations
    for (const compName of onboardingState.selectedCompanies) {
        try {
            await fetch(`${API_BASE}/api/companies`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ name: compName })
            });
        } catch (e) {}
    }

    await Promise.all([fetchCompanies(), fetchCompanyCatalog()]);

    // 3. Transition to Dashboard
    document.getElementById("onboarding-section")?.classList.add("hidden");
    document.getElementById("dashboard-section")?.classList.remove("hidden");
    document.querySelector(".nav-item[data-section='dashboard']")?.classList.add("active");
    document.getElementById("page-title").innerText = "Job Feed & Discovery";
    document.getElementById("page-subtitle").innerText = "Real-time alerts, ATS match scores, due diligence checks, and assisted applications.";

    // 4. Launch targeted discovery
    await triggerScrape();
}

const finishOnboardingAndScan = completeOnboardingAndLaunch;

async function cleanSystemFeedCache() {
    if (!confirm("Purge unapplied generic jobs from your feed and start with a clean slate?")) {
        return;
    }

    showToast("🧹 Purging cached generic jobs...");
    try {
        const res = await fetch(`${API_BASE}/api/system/clean-cache`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ clean_unapplied_jobs: true })
        });
        const data = await res.json();
        showToast(data.message);
        await fetchJobs();
    } catch (e) {
        alert("Failed to clean cache: " + e.message);
    }
}

async function fetchCompanyCatalog() {
    try {
        const res = await fetch(`${API_BASE}/api/companies/catalog`);
        if (res.ok) {
            state.catalog = await res.json();
            renderTrendingChips();
            renderOnboardingCompanies();
        } else {
            fallbackCatalog();
        }
    } catch (e) {
        console.warn("Failed to fetch catalog from API, using fallback", e);
        fallbackCatalog();
    }
}

function fallbackCatalog() {
    // No hardcoded company names: when the API is unreachable the catalog stays empty and
    // the user adds their own target organizations.
    state.catalog = [];
    renderTrendingChips();
    renderOnboardingCompanies();
}

let queuePollTimer = null;
let activeOperationsCount = 0;

function startFastQueuePolling() {
    activeOperationsCount++;
    fetchQueueStatus();
}

function stopFastQueuePolling() {
    activeOperationsCount = Math.max(0, activeOperationsCount - 1);
    fetchTokenUsage();
    fetchQueueStatus();
}

async function fetchQueueStatus() {
    if (queuePollTimer) {
        clearTimeout(queuePollTimer);
        queuePollTimer = null;
    }

    try {
        const res = await fetch(`${API_BASE}/api/llm/queue-status`);
        if (!res.ok) {
            queuePollTimer = setTimeout(fetchQueueStatus, 60000);
            return;
        }
        const data = await res.json();
        const qBadge = document.getElementById("llm-queue-badge");
        const hasActiveTasks = (data.queue_depth > 0) || Boolean(data.active_task);

        if (qBadge) {
            if (hasActiveTasks) {
                qBadge.style.display = "inline-block";
                const modeSuffix = data.mode === "cloud" ? ` (Cloud: ${data.max_concurrency}x parallel)` : ` (Local: 1x)`;
                const activeCount = data.active_workers || (data.active_task ? 1 : 0);

                if (activeCount > 1) {
                    qBadge.innerText = `⚡ ${activeCount} tasks parallel | +${data.queue_depth} queued${modeSuffix}`;
                } else if (data.active_task && data.queue_depth === 0) {
                    const elapsed = Math.round(data.active_task_elapsed_seconds || 0);
                    qBadge.innerText = `⚡ ${data.active_task} (${elapsed}s)${modeSuffix}`;
                } else if (data.active_task && data.queue_depth > 0) {
                    qBadge.innerText = `⚡ ${data.active_task} | +${data.queue_depth} queued${modeSuffix}`;
                } else if (data.queue_depth > 0) {
                    qBadge.innerText = `Queue: ${data.queue_depth} tasks (${data.estimated_wait_seconds || 0}s est.)${modeSuffix}`;
                }
                qBadge.style.background = data.is_backpressure_high ? "rgba(239, 68, 68, 0.2)" : "rgba(137, 180, 250, 0.15)";
                qBadge.style.color = data.is_backpressure_high ? "#f87171" : "#89b4fa";
            } else {
                qBadge.style.display = "none";
            }
        }

        // Adaptive polling schedule:
        // When active operations or queued tasks are present, poll frequently (2.5s).
        // When completely idle, poll with relaxed long heartbeat (60s).
        if (hasActiveTasks || activeOperationsCount > 0) {
            queuePollTimer = setTimeout(fetchQueueStatus, 2500);
        } else {
            queuePollTimer = setTimeout(fetchQueueStatus, 60000);
        }
    } catch (e) {
        queuePollTimer = setTimeout(fetchQueueStatus, 60000);
    }
}

async function fetchHealthAndLLMStatus() {
    try {
        const res = await fetch(`${API_BASE}/api/health`);
        const data = await res.json();
        if (data.active_llm) {
            state.activeLLM = data.active_llm;
            const badge = document.getElementById("llm-status-badge");
            if (badge) {
                if (data.active_llm.status === "online") {
                    badge.innerText = `AI: ${data.active_llm.provider} (${data.active_llm.model})`;
                    badge.className = "badge active";
                } else if (data.active_llm.status === "configured") {
                    badge.innerText = `AI: ${data.active_llm.provider}`;
                    badge.className = "badge verified";
                } else {
                    badge.innerText = `AI: Offline / Heuristics`;
                    badge.className = "badge warning";
                }
            }
        }
        await fetchQueueStatus();
    } catch (e) {
        console.warn("Could not retrieve LLM status", e);
    }
}

async function fetchTokenUsage(showToastFeedback = false) {
    try {
        const res = await fetch(`${API_BASE}/api/llm/token-usage`);
        if (res.ok) {
            const data = await res.json();
            updateTokenSidebar(data);
            if (showToastFeedback) {
                showToast(`⚡ Refreshed token counts: ${(data.total_tokens || 0).toLocaleString()} tokens used.`);
            }
        }
    } catch (e) {
        console.debug("Could not retrieve token usage", e);
    }
}

function updateTokenSidebar(data) {
    const tokenEl = document.getElementById("sidebar-token-count");
    const savingsEl = document.getElementById("sidebar-savings-usd");
    if (tokenEl) {
        const total = data.total_tokens || 0;
        const totalFormatted = total >= 1000 ? `${(total / 1000).toFixed(1)}k` : total.toLocaleString();
        const calls = data.total_inferences || data.call_count || 0;
        tokenEl.innerText = `${totalFormatted} (${calls} req${calls === 1 ? '' : 's'})`;
    }
    if (savingsEl) {
        const savings = data.total_savings_usd || 0;
        savingsEl.innerText = `+$${savings.toFixed(2)} saved`;
    }
}

async function openTokenAnalyticsModal(showToastFeedback = false) {
    const modal = document.getElementById("token-analytics-modal");
    if (!modal) return;
    modal.classList.remove("hidden");

    try {
        const res = await fetch(`${API_BASE}/api/llm/token-usage`);
        if (res.ok) {
            const data = await res.json();
            renderTokenAnalytics(data);
            updateTokenSidebar(data);
            if (showToastFeedback) {
                showToast("⚡ Token analytics refreshed.");
            }
        }
    } catch (e) {
        showToast("⚠️ Failed to load token analytics: " + e.message);
    }
}

function closeTokenAnalyticsModal() {
    const modal = document.getElementById("token-analytics-modal");
    if (modal) modal.classList.add("hidden");
}

function renderTokenAnalytics(data) {
    const totalTokensEl = document.getElementById("analytics-total-tokens");
    const splitEl = document.getElementById("analytics-token-split");
    const totalCallsEl = document.getElementById("analytics-total-calls");
    const savingsEl = document.getElementById("analytics-total-savings");

    const total = data.total_tokens || 0;
    const calls = data.total_inferences || 0;
    const savings = data.total_savings_usd || 0;

    if (totalTokensEl) totalTokensEl.innerText = total.toLocaleString();
    if (splitEl) splitEl.innerText = `${(data.prompt_tokens || 0).toLocaleString()} in / ${(data.completion_tokens || 0).toLocaleString()} out`;
    if (totalCallsEl) totalCallsEl.innerText = calls.toLocaleString();
    if (savingsEl) savingsEl.innerText = `+$${savings.toFixed(2)}`;

    const costs = data.estimated_cloud_costs_usd || {};

    const gpt4oEl = document.getElementById("cost-gpt4o");
    const gpt4oMiniEl = document.getElementById("cost-gpt4o-mini");
    const claudeEl = document.getElementById("cost-claude");
    const deepseekEl = document.getElementById("cost-deepseek");
    const geminiEl = document.getElementById("cost-gemini");

    if (gpt4oEl) gpt4oEl.innerText = `$${(costs.gpt_4o || 0).toFixed(4)}`;
    if (gpt4oMiniEl) gpt4oMiniEl.innerText = `$${(costs.gpt_4o_mini || 0).toFixed(4)}`;
    if (claudeEl) claudeEl.innerText = `$${(costs.claude_3_5_sonnet || 0).toFixed(4)}`;
    if (deepseekEl) deepseekEl.innerText = `$${(costs.deepseek_v3 || 0).toFixed(4)}`;
    if (geminiEl) geminiEl.innerText = `$${(costs.gemini_2_0_flash ?? costs.gemini_1_5_flash ?? 0).toFixed(4)}`;

    // Populate Trade-off Insight Callout
    const insightCallsEl = document.getElementById("insight-calls-count");
    const insightSavedEl = document.getElementById("insight-saved");
    const insightFlashCostEl = document.getElementById("insight-flash-cost");

    const flashCost = costs.gemini_2_0_flash ?? costs.gemini_1_5_flash ?? 0;

    if (insightCallsEl) insightCallsEl.innerText = calls.toLocaleString();
    if (insightSavedEl) insightSavedEl.innerText = `+$${savings.toFixed(2)}`;
    if (insightFlashCostEl) insightFlashCostEl.innerText = `$${flashCost.toFixed(4)}`;
    if (insightLocalTimeEl) insightLocalTimeEl.innerText = `~${localTimeMin} min`;
    if (insightCloudTimeEl) {
        if (flashTimeMin < 1.0) {
            const seconds = Math.max(1, Math.round(flashTimeMin * 60));
            insightCloudTimeEl.innerText = `~${seconds}s`;
        } else {
            insightCloudTimeEl.innerText = `~${flashTimeMin} min`;
        }
    }
}

async function resetTokenCounter() {
    if (!confirm("Reset the token usage counter back to zero for this session?")) return;
    try {
        const res = await fetch(`${API_BASE}/api/llm/token-usage`, { method: "DELETE" });
        const data = await res.json();
        showToast(data.message || "Token usage reset.");
        await openTokenAnalyticsModal();
        await fetchTokenUsage();
    } catch (e) {
        showToast("⚠️ Reset failed: " + e.message);
    }
}

// Initial fetch on page load
fetchQueueStatus();
fetchTokenUsage();

async function fetchCompanies() {
    try {
        const res = await fetch(`${API_BASE}/api/companies`);
        state.companies = await res.json();
        renderCompanies();
    } catch (e) {
        console.error("Failed to fetch companies", e);
    }
}

async function fetchJobs() {
    try {
        const res = await fetch(`${API_BASE}/api/jobs`);
        state.jobs = await res.json();
        updateKPIStats();
        applyJobFilters();
    } catch (e) {
        console.error("Failed to fetch jobs", e);
    }
}

async function fetchResume() {
    try {
        const resAll = await fetch(`${API_BASE}/api/resumes`);
        if (resAll.ok) {
            state.resumes = await resAll.json();
        } else {
            state.resumes = [];
        }

        const resActive = await fetch(`${API_BASE}/api/resume`);
        if (resActive.ok) {
            state.resume = await resActive.json();
        } else {
            state.resume = null;
        }
        renderResumeDetails();
    } catch (e) {
        console.log("No active resume yet.");
        state.resume = null;
        renderResumeDetails();
    }
}

// -------------------------------------------------------------
// KPI Stats Calculation
// -------------------------------------------------------------
function updateKPIStats(filteredJobs) {
    const list = Array.isArray(filteredJobs) ? filteredJobs : state.jobs;
    const active = list.filter(j => ["To Apply", "Applied", "Screening", "Interview", "Offered"].includes(j.status)).length;
    const highMatches = list.filter(j => (j.match_score || 0) >= 70).length;
    const ghost = list.filter(j => j.is_ghost_job).length;
    const genuine = list.filter(j => !j.is_ghost_job).length;

    const elActive = document.getElementById("stat-active") || document.getElementById("stat-applied");
    const elActiveDesc = elActive?.parentElement?.querySelector(".stat-desc");
    const elHigh = document.getElementById("stat-high-match") || document.getElementById("stat-matches");
    const elGhost = document.getElementById("stat-ghost") || document.getElementById("stat-total");
    const elGen = document.getElementById("stat-genuine");

    if (elActive) elActive.innerText = active;
    if (elActiveDesc) {
        if (state.jobs.length > active) {
            elActiveDesc.innerText = `Showing ${active} of ${state.jobs.length} total in DB`;
        } else {
            elActiveDesc.innerText = `Applications in progress`;
        }
    }
    if (elHigh) elHigh.innerText = highMatches;
    if (elGhost) elGhost.innerText = ghost;
    if (elGen) elGen.innerText = genuine;
}

// -------------------------------------------------------------
// Target Role & Title Semantic Expansion Matching
// -------------------------------------------------------------
const ROLE_EXPANSIONS = {
    'principal engineer': ['principal software engineer', 'principal engineer', 'principal architect', 'principal staff engineer', 'principal systems engineer', 'principal backend engineer', 'principal sde', 'principal member of technical staff', 'principal mts', 'principal solutions architect', 'principal cloud engineer'],
    'staff software engineer': ['staff software engineer', 'staff engineer', 'staff backend engineer', 'staff systems engineer', 'staff infra engineer', 'staff developer', 'staff sde', 'senior staff engineer', 'senior staff software engineer', 'staff member of technical staff', 'staff mts', 'staff architect', 'staff platform engineer'],
    'senior staff software engineer': ['senior staff software engineer', 'senior staff engineer', 'senior staff developer', 'senior staff sde', 'senior staff architect'],
    'staff engineer': ['staff engineer', 'staff software engineer', 'staff backend engineer', 'staff systems engineer', 'senior staff engineer', 'staff platform engineer', 'staff infra'],
    'software engineer': ['software engineer', 'software development engineer', 'sde', 'developer', 'engineer', 'backend engineer', 'full stack engineer', 'frontend engineer', 'systems engineer', 'mts', 'member of technical staff'],
    'backend engineer': ['backend engineer', 'back-end engineer', 'backend developer', 'server engineer', 'systems engineer', 'software engineer, backend', 'backend software engineer', 'platform engineer', 'distributed systems'],
    'full stack': ['full stack', 'fullstack', 'full-stack', 'full stack engineer', 'full stack developer'],
    'lead engineer': ['lead engineer', 'lead software engineer', 'tech lead', 'technical lead', 'lead backend engineer', 'team lead', 'lead architect'],
    'technical lead': ['technical lead', 'tech lead', 'lead engineer', 'lead software engineer', 'lead developer', 'team lead']
};

function normalizeRoleText(text) {
    return (text || '').toLowerCase().replace(/[^a-z0-9]/g, ' ').replace(/\s+/g, ' ').trim();
}

function matchesTargetRole(jobTitle, jobDescription, targetRoles) {
    if (!targetRoles || targetRoles.length === 0) return true;
    
    const normTitle = normalizeRoleText(jobTitle);
    const normDesc = normalizeRoleText(jobDescription || '');
    const titleWords = new Set(normTitle.split(' '));

    for (const rawTarget of targetRoles) {
        const normTarget = normalizeRoleText(rawTarget);
        if (!normTarget) continue;

        // 1. Direct exact substring match
        if (normTitle.includes(normTarget)) return true;

        // 2. Synonyms / Role expansion match
        const expansions = ROLE_EXPANSIONS[normTarget] || [];
        for (const exp of expansions) {
            if (normTitle.includes(exp)) return true;
        }

        // 3. Token-based word set matching (all non-stop words present in title)
        const targetWords = normTarget.split(' ').filter(w => !['and', 'or', 'the', 'in', 'at', 'of', 'for'].includes(w));
        if (targetWords.length > 0 && targetWords.every(w => titleWords.has(w) || normTitle.includes(w))) {
            return true;
        }

        // 4. Role Level + Technical Discipline Match
        const levels = ['principal', 'senior staff', 'staff', 'lead', 'architect', 'director', 'head', 'vp', 'vice president', 'distinguished'];
        const disciplines = ['engineer', 'software', 'backend', 'developer', 'architect', 'systems', 'platform', 'infra', 'infrastructure', 'ai', 'ml', 'cloud', 'sre', 'devops'];

        const targetHasLevel = levels.find(lvl => normTarget.includes(lvl));
        const titleHasLevel = levels.find(lvl => normTitle.includes(lvl));

        if (targetHasLevel && titleHasLevel) {
            const levelMatches = (targetHasLevel === titleHasLevel) || 
                (targetHasLevel.includes('staff') && titleHasLevel.includes('staff')) ||
                (targetHasLevel.includes('principal') && titleHasLevel.includes('principal'));
            if (levelMatches) {
                const targetHasDisc = disciplines.some(fn => normTarget.includes(fn));
                const titleHasDisc = disciplines.some(fn => normTitle.includes(fn));
                if (!targetHasDisc || titleHasDisc) {
                    return true;
                }
            }
        }

        // 5. Description excerpt check
        if (normDesc && normDesc.includes(normTarget)) {
            return true;
        }
    }

    return false;
}

// -------------------------------------------------------------
// Job Feed & Filter Rendering
// -------------------------------------------------------------
function updateFeedTabCounts() {
    const allCountEl = document.getElementById("feed-count-all");
    const shortlistedCountEl = document.getElementById("feed-count-shortlisted");
    const toApplyCountEl = document.getElementById("feed-count-to-apply");
    const appliedCountEl = document.getElementById("feed-count-applied");
    const dismissedCountEl = document.getElementById("feed-count-dismissed");

    let cAll = 0, cShortlisted = 0, cToApply = 0, cApplied = 0, cDismissed = 0;
    let tInProgress = 0, tRejected = 0;

    const query = (document.getElementById("job-search-input")?.value || "").toLowerCase();
    const minScore = parseFloat(document.getElementById("score-filter")?.value || "0");
    const targetRoles = state.targetRoles.map(t => t.toLowerCase().trim()).filter(Boolean);
    const targetCountry = (state.targetCountry || "all").toLowerCase().trim();
    const targetWorkMode = (state.targetWorkMode || "all").toLowerCase().trim();

    state.jobs.forEach(j => {
        const s = j.status || "To Apply";

        // Global tracker metrics
        if (s === "Applied" || ["Screening", "Interview", "Offered"].includes(s)) tInProgress++;
        else if (s === "Rejected") tRejected++;

        // Feed tab count calculation synchronized with active search & criteria filters
        const matchesQuery = !query || 
            j.title.toLowerCase().includes(query) || 
            (j.company_name && j.company_name.toLowerCase().includes(query)) ||
            (j.description && j.description.toLowerCase().includes(query));

        const matchesScore = (j.match_score || 0) >= minScore;
        const matchesTitles = !state.criteriaFilterActive || matchesTargetRole(j.title, j.description, targetRoles);
        const matchesLocation = !state.criteriaFilterActive || isJobLocationMatch(j.location, j.title, targetCountry, state.targetCities, targetWorkMode);

        let matchesWorkMode = true;
        if (state.criteriaFilterActive && targetWorkMode !== "all") {
            const jLoc = (j.location || "").toLowerCase();
            const jTitle = j.title.toLowerCase();
            if (targetWorkMode === "remote") {
                matchesWorkMode = jLoc.includes("remote") || jTitle.includes("remote");
            } else if (targetWorkMode === "hybrid") {
                matchesWorkMode = jLoc.includes("hybrid");
            } else if (targetWorkMode === "onsite") {
                matchesWorkMode = !jLoc.includes("remote");
            }
        }

        if (matchesQuery && matchesScore && matchesTitles && matchesLocation && matchesWorkMode) {
            if (s === "Not Interested" || s === "Ignored") {
                cDismissed++;
            } else if (s === "Shortlisted" || s === "Saved") {
                cShortlisted++;
                cAll++;
            } else if (s === "To Apply") {
                cToApply++;
                cAll++;
            } else if (s === "Applied" || ["Screening", "Interview", "Offered"].includes(s)) {
                cApplied++;
                cAll++;
            }
        }
    });

    if (allCountEl) allCountEl.innerText = cAll;
    if (shortlistedCountEl) shortlistedCountEl.innerText = cShortlisted;
    if (toApplyCountEl) toApplyCountEl.innerText = cToApply;
    if (appliedCountEl) appliedCountEl.innerText = cApplied;
    if (dismissedCountEl) dismissedCountEl.innerText = cDismissed;

    const tInProgEl = document.getElementById("tracker-in-progress-count");
    const tShortlistEl = document.getElementById("tracker-shortlisted-count");
    const tToApplyEl = document.getElementById("tracker-to-apply-count");
    const tAllEl = document.getElementById("tracker-all-count");
    const tRejEl = document.getElementById("tracker-rejected-count");
    const tDismEl = document.getElementById("tracker-dismissed-count");

    if (tInProgEl) tInProgEl.innerText = tInProgress;
    if (tShortlistEl) tShortlistEl.innerText = state.jobs.filter(j => j.status === "Shortlisted" || j.status === "Saved").length;
    if (tToApplyEl) tToApplyEl.innerText = state.jobs.filter(j => (j.status || "To Apply") === "To Apply").length;
    if (tAllEl) tAllEl.innerText = state.jobs.length;
    if (tRejEl) tRejEl.innerText = tRejected;
    if (tDismEl) tDismEl.innerText = state.jobs.filter(j => j.status === "Not Interested" || j.status === "Ignored").length;
}

function setFeedTab(tabName) {
    state.feedTab = tabName;
    document.querySelectorAll(".feed-tab-btn").forEach(btn => {
        btn.classList.toggle("active", btn.getAttribute("data-feed-tab") === tabName);
    });
    applyJobFilters();
}

function isJobLocationMatch(jLoc, jTitle, targetCountry, targetCities, targetWorkMode) {
    const loc = (jLoc || "").toLowerCase().trim();
    const title = (jTitle || "").toLowerCase().trim();
    const combined = `${title} ${loc}`;

    const cities = (targetCities || []).map(c => c.toLowerCase().trim()).filter(Boolean);
    const countryKey = (targetCountry || "all").toLowerCase().trim();

    // 1. Direct city & regional tech hub check
    if (cities.length > 0) {
        for (const c of cities) {
            if (c === "remote") continue;
            if (loc.includes(c)) return true;
            if ((c === "bengaluru" || c === "bangalore") && (loc.includes("bengaluru") || loc.includes("bangalore") || loc.includes("karnataka") || loc.includes("blr"))) return true;
            if ((c === "hyderabad" || c === "hyd") && (loc.includes("hyderabad") || loc.includes("telangana") || loc.includes("hyd"))) return true;
            if ((c === "pune") && (loc.includes("pune") || loc.includes("maharashtra"))) return true;
            if ((c === "delhi ncr" || c === "delhi") && (loc.includes("delhi") || loc.includes("noida") || loc.includes("gurgaon") || loc.includes("gurugram") || loc.includes("ncr"))) return true;
            if ((c === "mumbai") && (loc.includes("mumbai") || loc.includes("maharashtra") || loc.includes("bombay"))) return true;
            if ((c === "chennai") && (loc.includes("chennai") || loc.includes("tamil nadu") || loc.includes("madras"))) return true;
            if ((c === "san francisco" || c === "sf") && (loc.includes("san francisco") || loc.includes("sf") || loc.includes("bay area") || loc.includes("california") || loc.includes("ca"))) return true;
            if ((c === "new york" || c === "nyc") && (loc.includes("new york") || loc.includes("nyc") || loc.includes("ny"))) return true;
            if ((c === "seattle") && (loc.includes("seattle") || loc.includes("washington") || loc.includes("wa"))) return true;
            if ((c === "austin") && (loc.includes("austin") || loc.includes("texas") || loc.includes("tx"))) return true;
            if ((c === "london") && (loc.includes("london") || loc.includes("uk") || loc.includes("united kingdom"))) return true;
        }
    }

    // 2. Direct country check
    if (countryKey && countryKey !== "all") {
        const countryEntry = COUNTRIES_HUBS_DATA[targetCountry] || COUNTRIES_HUBS_DATA[countryKey];
        const countryName = countryEntry ? countryEntry.name.toLowerCase() : countryKey;
        const hubs = countryEntry ? countryEntry.hubs.map(h => h.toLowerCase()) : [];

        if (loc.includes(countryName) || hubs.some(h => loc.includes(h))) {
            return true;
        }
        if (countryKey === "india" && (loc.includes("apac") || loc.includes("south asia") || loc.includes("india"))) {
            return true;
        }
    }

    // 3. Remote Handling
    const isRemote = combined.includes("remote") || combined.includes("worldwide") || combined.includes("global") || combined.includes("anywhere") || combined.includes("distributed") || combined.includes("all-remote");
    if (isRemote) {
        if (targetWorkMode === "onsite") return false;

        const isTrulyGlobal = combined.includes("worldwide") || combined.includes("global") || combined.includes("anywhere") || combined.includes("all-remote");

        // Foreign restriction check if country is set
        if (countryKey && countryKey !== "all") {
            const foreignIndicators = [
                { key: "united states", matches: ["united states", "usa", "amer", "north america", "san francisco", "seattle", "new york", "austin", "boston", "chicago", "los angeles", "california"] },
                { key: "canada", matches: ["canada", "toronto", "vancouver", "montreal", "ottawa", "calgary", "waterloo"] },
                { key: "united kingdom", matches: ["united kingdom", "uk", "u.k.", "london", "manchester", "scotland", "edinburgh"] },
                { key: "poland", matches: ["poland", "polska", "warsaw", "krakow", "wroclaw"] },
                { key: "germany", matches: ["germany", "deutschland", "berlin", "munich", "frankfurt"] },
                { key: "europe", matches: ["europe", "emea", "eu"] }
            ];

            let hasForeignRestriction = false;
            for (const item of foreignIndicators) {
                if (countryKey !== item.key && !countryKey.includes(item.key) && !item.key.includes(countryKey)) {
                    for (const m of item.matches) {
                        if (loc.includes(` ${m} `) || loc.includes(`, ${m}`) || loc.includes(`,${m}`) || loc.includes(`${m},`) || loc.includes(`(${m})`) || loc.includes(`- ${m}`) || loc.includes(`remote, ${m}`) || loc.startsWith(`${m} `) || loc.endsWith(` ${m}`)) {
                            hasForeignRestriction = true;
                            break;
                        }
                    }
                    if (hasForeignRestriction) break;
                }
            }

            if (hasForeignRestriction) {
                return false;
            }
        }

        const wantsRemote = (targetWorkMode === "remote" || targetWorkMode === "all" || !targetWorkMode) || cities.includes("remote");
        if (wantsRemote || isTrulyGlobal) {
            return true;
        }
    }

    if (cities.length === 0 && (countryKey === "all" || !countryKey)) {
        return true;
    }

    return false;
}

function applyJobFilters() {
    updateFeedTabCounts();

    const query = (document.getElementById("job-search-input")?.value || "").toLowerCase();
    const statusVal = document.getElementById("status-filter")?.value || "all";
    const minScore = parseFloat(document.getElementById("score-filter")?.value || "0");

    const targetRoles = state.targetRoles.map(t => t.toLowerCase().trim()).filter(Boolean);
    const targetCountry = (state.targetCountry || "all").toLowerCase().trim();
    const targetWorkMode = (state.targetWorkMode || "all").toLowerCase().trim();

    const filtered = state.jobs.filter(j => {
        const jStatus = j.status || "To Apply";

        // Tab & Status condition
        let matchesTab = true;
        if (state.feedTab === "all") {
            if (statusVal === "all") {
                // In Active Feed, show unapplied actionable opportunities (To Apply & Shortlisted)
                matchesTab = jStatus === "To Apply" || jStatus === "Shortlisted" || jStatus === "Saved";
            } else {
                matchesTab = jStatus === statusVal;
            }
        } else if (state.feedTab === "Shortlisted") {
            matchesTab = (jStatus === "Shortlisted" || jStatus === "Saved") && (statusVal === "all" || jStatus === statusVal);
        } else if (state.feedTab === "To Apply") {
            matchesTab = jStatus === "To Apply" && (statusVal === "all" || jStatus === statusVal);
        } else if (state.feedTab === "Applied") {
            matchesTab = (jStatus === "Applied" || jStatus === "Screening" || jStatus === "Interview" || jStatus === "Offered") && (statusVal === "all" || jStatus === statusVal);
        } else if (state.feedTab === "Not Interested") {
            matchesTab = (jStatus === "Not Interested" || jStatus === "Ignored") && (statusVal === "all" || jStatus === statusVal);
        }

        const matchesQuery = !query || 
            j.title.toLowerCase().includes(query) || 
            (j.company_name && j.company_name.toLowerCase().includes(query)) ||
            (j.description && j.description.toLowerCase().includes(query));

        const matchesScore = (j.match_score || 0) >= minScore;

        // Target Preferences (Roles, Location, Work Mode)
        const matchesTitles = !state.criteriaFilterActive || matchesTargetRole(j.title, j.description, targetRoles);
        const matchesLocation = !state.criteriaFilterActive || isJobLocationMatch(j.location, j.title, state.targetCountry, state.targetCities, targetWorkMode);

        let matchesWorkMode = true;
        if (state.criteriaFilterActive && targetWorkMode !== "all") {
            const jLoc = (j.location || "").toLowerCase();
            const jTitle = j.title.toLowerCase();
            if (targetWorkMode === "remote") {
                matchesWorkMode = jLoc.includes("remote") || jTitle.includes("remote");
            } else if (targetWorkMode === "hybrid") {
                matchesWorkMode = jLoc.includes("hybrid");
            } else if (targetWorkMode === "onsite") {
                matchesWorkMode = !jLoc.includes("remote");
            }
        }

        return matchesTab && matchesQuery && matchesScore && matchesTitles && matchesLocation && matchesWorkMode;
    });

    updateKPIStats(filtered);
    renderJobFeed(filtered);
}

function renderJobFeed(jobs) {
    const feed = document.getElementById("job-feed");
    if (!jobs || jobs.length === 0) {
        feed.innerHTML = `
            <div class="empty-state">
                <div class="empty-icon">🔍</div>
                <p>No matching jobs found in this view. Click "⚡ Scan Feed" or switch filter tabs to see more opportunities.</p>
            </div>
        `;
        return;
    }

    feed.innerHTML = jobs.map(job => {
        const hasScore = job.match_score !== null && job.match_score !== undefined;
        const score = hasScore ? Math.round(job.match_score) : null;
        let scoreClass = "low";
        if (score !== null && score >= 80) scoreClass = "high";
        else if (score !== null && score >= 60) scoreClass = "medium";
        const scoreTitle = !hasScore
            ? "Not yet analyzed"
            : (job.match_scored ? "AI match score" : "Source baseline — not AI match-scored");

        const compName = job.company_name || 'Direct';
        const initial = compName.trim().charAt(0).toUpperCase();

        const ghostBadge = job.is_ghost_job 
            ? `<span class="badge ghost" title="Flagged as potential ghost listing">⚠️ Ghost Alert</span>`
            : `<span class="badge verified" title="Verified active direct posting">🛡️ Genuine</span>`;

        const tailoredBadge = (job.cover_letter_draft && job.cover_letter_draft.length > 20)
            ? `<span class="tailored-badge" title="AI Tailored cover letter and resume points ready">⚡ Tailored</span>`
            : '';

        const isChecked = state.selectedJobIds.has(job.id);
        const postedDate = job.created_at ? formatDate(job.created_at) : 'Recently';
        const statusSlug = (job.status || 'To Apply').toLowerCase().replace(/\s+/g, '-');
        const isShortlisted = job.status === "Shortlisted" || job.status === "Saved";
        const isDismissed = job.status === "Not Interested" || job.status === "Ignored";
        const isApplied = ["Applied", "Screening", "Interview", "Offered", "Rejected"].includes(job.status);

        return `
            <div class="job-card ${isChecked ? 'selected' : ''} ${isShortlisted ? 'shortlisted' : ''} ${isDismissed ? 'not-interested' : ''}" data-job-id="${job.id}">
                <div class="job-card-select-wrapper">
                    <input type="checkbox" class="job-select-chk" ${isChecked ? 'checked' : ''} onchange="toggleJobSelection(${job.id}, this.checked)">
                </div>
                <div class="company-avatar-badge">${initial}</div>
                <div class="job-info-main">
                    <div class="job-title-row">
                        <h4>${escapeHTML(job.title)}</h4>
                        <span class="company-name-pill">${escapeHTML(compName)}</span>
                        ${isShortlisted ? `<span class="badge" style="background:rgba(234,179,8,0.2); color:#facc15; border:1px solid rgba(234,179,8,0.5);">⭐ Shortlisted</span>` : ''}
                        ${job.status === 'Applied' ? (job.submission_confirmed ? `<span class="badge" style="background:rgba(166,227,161,0.2); color:#a6e3a1; border:1px solid rgba(166,227,161,0.5);">✅ Submitted</span>` : `<span class="badge" style="background:rgba(250,179,135,0.15); color:#fab387; border:1px solid rgba(250,179,135,0.4);">🕓 Awaiting submit</span>`) : ''}
                        ${ghostBadge}
                        ${tailoredBadge}
                    </div>
                    <div class="job-meta-row">
                        <span class="job-meta-item">📍 ${escapeHTML(job.location || 'Remote / Various')}</span>
                        <span class="job-meta-item">💰 ${escapeHTML(job.salary_range || 'Competitive')}</span>
                        <span class="job-meta-item">📅 ${postedDate}</span>
                        <span class="job-meta-item">🔗 ${escapeHTML(job.source || 'Direct Portal')}</span>
                    </div>
                </div>

                <div class="job-actions-right">
                    <div class="match-pill ${scoreClass}" title="${scoreTitle}">
                        ${hasScore ? `🎯 ${score}% Match${job.match_scored ? '' : ' <span class="not-scored-cue">not AI-scored</span>'}` : '🎯 —'}
                    </div>
                    <select class="status-dropdown status-${statusSlug}" onchange="updateJobStatus(${job.id}, this.value, this)">
                        <option value="Shortlisted" ${isShortlisted ? 'selected' : ''}>⭐ Shortlisted</option>
                        <option value="To Apply" ${job.status === 'To Apply' ? 'selected' : ''}>⚪ To Apply</option>
                        <option value="Applied" ${job.status === 'Applied' ? 'selected' : ''}>🚀 Applied</option>
                        <option value="Screening" ${job.status === 'Screening' ? 'selected' : ''}>📋 Screening</option>
                        <option value="Interview" ${job.status === 'Interview' ? 'selected' : ''}>🎙️ Interview</option>
                        <option value="Offered" ${job.status === 'Offered' ? 'selected' : ''}>🎉 Offered</option>
                        <option value="Rejected" ${job.status === 'Rejected' ? 'selected' : ''}>📫 Rejected</option>
                        <option value="Not Interested" ${isDismissed ? 'selected' : ''}>🚫 Dismissed</option>
                    </select>

                    ${!isApplied ? `
                        <button class="btn-shortlist-action ${isShortlisted ? 'is-saved' : ''}" onclick="toggleShortlistJob(${job.id}, event)" title="${isShortlisted ? 'Saved in Shortlist (Click to remove)' : 'Save to Shortlist'}">
                            ${isShortlisted ? '⭐ Shortlisted' : '☆ Save'}
                        </button>

                        ${isDismissed ? `
                            <button class="btn btn-outline btn-sm" onclick="restoreJob(${job.id}, event)" title="Restore / un-dismiss position back to active feed">
                                ↺ Un-Dismiss
                            </button>
                        ` : `
                            <button class="btn-dismiss-action" onclick="dismissJob(${job.id}, event)" title="Dismiss and hide from active feed">
                                🚫 Dismiss
                            </button>
                        `}
                    ` : ''}

                    <button class="btn btn-outline btn-sm" onclick="tailorJobDirect(${job.id})" title="Generate tailored cover letter and resume points with AI">
                        ⚡ Tailor AI
                    </button>
                    <button class="btn btn-outline btn-sm" onclick="openJobDetailsModal(${job.id})">
                        📋 Intel & Prep
                    </button>
                    <button class="btn ${isApplied ? 'btn-outline' : 'btn-primary'} btn-sm" onclick="launchPlaywrightApply(${job.id})" title="${isApplied ? 'Re-open application portal' : 'Apply via assisted browser'}">
                        ${isApplied ? '🚀 Reopen ↗' : '🚀 Apply ↗'}
                    </button>
                </div>
            </div>
        `;
    }).join("");

    updateBatchBar();
}

function toggleJobSelection(jobId, isChecked) {
    if (isChecked) {
        state.selectedJobIds.add(jobId);
    } else {
        state.selectedJobIds.delete(jobId);
    }
    updateBatchBar();
}

function clearJobSelection() {
    state.selectedJobIds.clear();
    const chks = document.querySelectorAll(".job-select-chk, #select-all-jobs-chk");
    chks.forEach(c => c.checked = false);
    updateBatchBar();
}

const clearBatchSelection = clearJobSelection;

function updateBatchBar() {
    const bar = document.getElementById("batch-action-bar");
    const countEl = document.getElementById("batch-selected-count");
    const masterChk = document.getElementById("select-all-jobs-chk");
    if (!bar) return;

    const count = state.selectedJobIds.size;
    if (count > 0) {
        bar.classList.remove("hidden");
        if (countEl) countEl.innerHTML = `<strong>${count}</strong> selected`;
    } else {
        bar.classList.add("hidden");
        if (masterChk) masterChk.checked = false;
    }
}

async function bulkTailorSelectedJobs() {
    const ids = Array.from(state.selectedJobIds);
    if (ids.length === 0) {
        showToast("Please select at least one job first.");
        return;
    }

    const optResume = document.getElementById("batch-opt-resume")?.checked ?? true;
    const optCover = document.getElementById("batch-opt-cover")?.checked ?? true;

    showToast(`⚡ Generating custom application materials for ${ids.length} job(s)...`);
    try {
        const res = await fetch(`${API_BASE}/api/jobs/bulk-tailor`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ 
                job_ids: ids,
                include_resume_tailoring: optResume,
                include_cover_letter: optCover,
                include_cold_message: true
            })
        });
        if (!res.ok) throw new Error("Batch tailoring failed");
        const data = await res.json();
        showToast(data.message || "Jobs tailored successfully!");
        await fetchJobs();
        clearJobSelection();
    } catch (e) {
        alert("Failed to tailor selected jobs: " + e.message);
    }
}

async function tailorCurrentModalJob() {
    if (!state.activeJobModal) return;
    const btn = document.getElementById("modal-tailor-btn");
    if (btn) {
        btn.disabled = true;
        btn.innerHTML = `<span class="spinner" style="width:12px;height:12px;display:inline-block;"></span> Generating...`;
    }

    const optResume = document.getElementById("modal-opt-resume")?.checked ?? true;
    const optCover = document.getElementById("modal-opt-cover")?.checked ?? true;
    const optOutreach = document.getElementById("modal-opt-outreach")?.checked ?? true;

    startFastQueuePolling();
    try {
        const res = await fetch(`${API_BASE}/api/jobs/${state.activeJobModal.id}/tailor`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                include_resume_tailoring: optResume,
                include_cover_letter: optCover,
                include_cold_message: optOutreach
            })
        });
        if (!res.ok) {
            let detail = "Failed to generate application package";
            try { detail = (await res.json()).detail || detail; } catch (_) {}
            throw new Error(detail);
        }
        const updatedJob = await res.json();
        state.activeJobModal = updatedJob;
        
        // Update state.jobs list
        const idx = state.jobs.findIndex(j => j.id === updatedJob.id);
        if (idx !== -1) state.jobs[idx] = updatedJob;

        // Refresh modal content
        openJobModal(updatedJob);
        showToast("Application materials tailored successfully!");
        applyJobFilters();
    } catch (e) {
        alert("Tailoring failed: " + e.message);
    } finally {
        stopFastQueuePolling();
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = `⚡ Generate Materials`;
        }
    }
}

async function bulkApplySelectedJobs() {
    const ids = Array.from(state.selectedJobIds);
    if (ids.length === 0) {
        showToast("Please select at least one job to apply.");
        return;
    }

    const confirmed = confirm(`Launch single-window multi-tab application session for ${ids.length} selected job(s)?`);
    if (!confirmed) return;

    showToast(`🚀 Launching ${ids.length} job application tab(s) in Chrome...`);
    startFastQueuePolling();
    try {
        const res = await fetch(`${API_BASE}/api/jobs/bulk-apply`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ job_ids: ids })
        });
        const data = await res.json();
        if (res.ok) {
            showToast(data.message || `Launched ${ids.length} application tabs!`);
            await fetchJobs();
            clearJobSelection();
        } else {
            showToast(`⚠️ ${data.detail || data.message || "Failed to launch bulk application"}`);
        }
    } catch (e) {
        showToast("⚠️ Could not connect to agent backend.");
    } finally {
        stopFastQueuePolling();
    }
}

async function bulkSetStatus(newStatus) {
    if (!newStatus) return;
    const ids = Array.from(state.selectedJobIds);
    if (ids.length === 0) {
        showToast("Please select at least one job.");
        return;
    }

    try {
        const res = await fetch(`${API_BASE}/api/jobs/bulk-status`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ job_ids: ids, status: newStatus })
        });
        if (!res.ok) throw new Error("Batch status update failed");
        const data = await res.json();
        showToast(data.message);
        await fetchJobs();
        clearJobSelection();
    } catch (e) {
        alert("Failed to update status: " + e.message);
    }
}

async function tailorJobDirect(jobId) {
    showToast("⚡ Generating tailored cover letter & ATS points with AI...");
    try {
        const res = await fetch(`${API_BASE}/api/jobs/${jobId}/tailor`, { method: "POST" });
        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || "Tailoring failed");
        }
        const updated = await res.json();
        const idx = state.jobs.findIndex(j => j.id === jobId);
        if (idx !== -1) state.jobs[idx] = updated;
        showToast("Application materials tailored successfully!");
        applyJobFilters();
        openJobDetailsModal(jobId);
    } catch (e) {
        alert(e.message);
    }
}

// -------------------------------------------------------------
// Application Tracker View
// -------------------------------------------------------------
function setTrackerFilter(filterType) {
    state.trackerFilter = filterType;
    document.querySelectorAll("#tracker-section .tab-btn").forEach(btn => {
        btn.classList.toggle("active", btn.getAttribute("data-filter") === filterType);
    });
    renderTrackerList();
}

function renderTrackerList() {
    const listEl = document.getElementById("tracker-list");
    let filtered = state.jobs;

    if (state.trackerFilter === "in_progress") {
        filtered = state.jobs.filter(j => ["Applied", "Screening", "Interview", "Offered"].includes(j.status));
    } else if (state.trackerFilter === "shortlisted") {
        filtered = state.jobs.filter(j => j.status === "Shortlisted" || j.status === "Saved");
    } else if (state.trackerFilter === "to_apply") {
        filtered = state.jobs.filter(j => j.status === "To Apply");
    } else if (state.trackerFilter === "rejected") {
        filtered = state.jobs.filter(j => j.status === "Rejected");
    } else if (state.trackerFilter === "not_interested") {
        filtered = state.jobs.filter(j => j.status === "Not Interested" || j.status === "Ignored");
    } else if (state.trackerFilter === "all") {
        filtered = state.jobs.filter(j => j.status !== "Not Interested" && j.status !== "Ignored");
    }

    if (filtered.length === 0) {
        listEl.innerHTML = `
            <div class="empty-state">
                <div class="empty-icon">📋</div>
                <p>No applications match the selected filter category.</p>
            </div>
        `;
        return;
    }

    listEl.innerHTML = filtered.map(job => {
        const appliedDate = job.applied_at ? formatDate(job.applied_at) : 'N/A';
        const rejectedDate = job.rejected_at ? formatDate(job.rejected_at) : 'N/A';
        const statusSlug = (job.status || 'To Apply').toLowerCase().replace(/\s+/g, '-');
        const isShortlisted = job.status === "Shortlisted" || job.status === "Saved";
        const isDismissed = job.status === "Not Interested" || job.status === "Ignored";

        return `
            <div class="job-card ${isShortlisted ? 'shortlisted' : ''} ${isDismissed ? 'not-interested' : ''}">
                <div class="job-info-main">
                    <div class="job-title-row">
                        <h4>${escapeHTML(job.title)}</h4>
                        <span class="badge active">${escapeHTML(job.company_name || '')}</span>
                        ${isShortlisted ? `<span class="badge" style="background:rgba(234,179,8,0.2); color:#facc15; border:1px solid rgba(234,179,8,0.5);">⭐ Shortlisted</span>` : ''}
                        <span class="status-pill status-${statusSlug}">
                            ${job.status === 'Shortlisted' ? '⭐ Shortlisted' :
                              job.status === 'Applied' ? '🚀 Applied' : 
                              job.status === 'Interview' ? '🎙️ Interview' : 
                              job.status === 'Screening' ? '📋 Screening' : 
                              job.status === 'Offered' ? '🎉 Offered' : 
                              job.status === 'Rejected' ? '📫 Regret' : 
                              isDismissed ? '🚫 Dismissed' : '⚪ ' + job.status}
                        </span>
                    </div>
                    <div class="job-meta-row">
                        <span class="job-meta-item">📍 ${escapeHTML(job.location || 'Remote / Various')}</span>
                        <span class="job-meta-item">📅 Applied: ${appliedDate}</span>
                        ${job.status === 'Applied' && job.submission_confirmed ? `<span class="job-meta-item" style="color:#a6e3a1;">✅ Submission confirmed</span>` : ''}
                        ${job.status === 'Applied' && !job.submission_confirmed ? `<span class="job-meta-item" style="color:#fab387;">🕓 Awaiting submit confirmation</span>` : ''}
                        ${job.status === 'Rejected' ? `<span class="job-meta-item text-danger">🛑 Regret Date: ${rejectedDate}</span>` : ''}
                    </div>
                </div>
                <div class="job-actions-right">
                    <select class="status-dropdown status-${statusSlug}" onchange="updateJobStatus(${job.id}, this.value, this)">
                        <option value="Shortlisted" ${isShortlisted ? 'selected' : ''}>⭐ Shortlisted</option>
                        <option value="To Apply" ${job.status === 'To Apply' ? 'selected' : ''}>⚪ To Apply</option>
                        <option value="Applied" ${job.status === 'Applied' ? 'selected' : ''}>🚀 Applied</option>
                        <option value="Screening" ${job.status === 'Screening' ? 'selected' : ''}>📋 Screening</option>
                        <option value="Interview" ${job.status === 'Interview' ? 'selected' : ''}>🎙️ Interview</option>
                        <option value="Offered" ${job.status === 'Offered' ? 'selected' : ''}>🎉 Offered</option>
                        <option value="Rejected" ${job.status === 'Rejected' ? 'selected' : ''}>📫 Rejected</option>
                        <option value="Not Interested" ${isDismissed ? 'selected' : ''}>🚫 Dismissed</option>
                    </select>
                    ${isDismissed ? `
                        <button class="btn btn-outline btn-sm" onclick="restoreJob(${job.id}, event)" title="Restore position back to active opportunities">
                            ↺ Un-Dismiss
                        </button>
                    ` : `
                        <button class="btn btn-primary btn-sm" onclick="launchPlaywrightApply(${job.id})">
                            🚀 Apply ↗
                        </button>
                    `}
                    <button class="btn btn-outline btn-sm" onclick="openJobDetailsModal(${job.id})">
                        View Dossier
                    </button>
                </div>
            </div>
        `;
    }).join("");
}

async function toggleShortlistJob(jobId, e) {
    if (e) e.stopPropagation();
    const job = state.jobs.find(j => j.id === jobId);
    if (!job) return;

    const isCurrentlyShortlisted = job.status === "Shortlisted" || job.status === "Saved";
    const newStatus = isCurrentlyShortlisted ? "To Apply" : "Shortlisted";

    try {
        const res = await fetch(`${API_BASE}/api/jobs/${jobId}/status`, {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ status: newStatus })
        });
        if (!res.ok) throw new Error("Failed to update status");
        job.status = newStatus;
        showToast(newStatus === "Shortlisted" ? "⭐ Saved to Shortlist!" : "Removed from Shortlist");
        applyJobFilters();
    } catch (err) {
        showToast("⚠️ Could not update shortlist: " + err.message);
    }
}

async function dismissJob(jobId, e) {
    if (e) e.stopPropagation();
    const job = state.jobs.find(j => j.id === jobId);
    if (!job) return;

    try {
        const res = await fetch(`${API_BASE}/api/jobs/${jobId}/status`, {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ status: "Not Interested" })
        });
        if (!res.ok) throw new Error("Failed to dismiss job");
        job.status = "Not Interested";
        showToast(`🚫 Dismissed '${job.title}' (hidden from active feed)`);
        applyJobFilters();
        const trackerSection = document.getElementById("tracker-section");
        if (trackerSection && !trackerSection.classList.contains("hidden")) {
            renderTrackerList();
        }
        await fetchDismissedPatterns();
    } catch (err) {
        showToast("⚠️ Could not dismiss position: " + err.message);
    }
}

async function restoreJob(jobId, e) {
    if (e) e.stopPropagation();
    const job = state.jobs.find(j => j.id === jobId);
    if (!job) return;

    try {
        const res = await fetch(`${API_BASE}/api/jobs/${jobId}/status`, {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ status: "To Apply" })
        });
        if (!res.ok) throw new Error("Failed to restore job");
        job.status = "To Apply";
        showToast(`↺ Restored '${job.title}' to active feed`);
        applyJobFilters();
        const trackerSection = document.getElementById("tracker-section");
        if (trackerSection && !trackerSection.classList.contains("hidden")) {
            renderTrackerList();
        }
    } catch (err) {
        showToast("⚠️ Could not restore position: " + err.message);
    }
}

async function deleteJob(jobId) {
    if (!confirm("Are you sure you want to permanently delete this job record?")) return;
    try {
        const res = await fetch(`${API_BASE}/api/jobs/${jobId}`, { method: "DELETE" });
        if (res.ok) {
            showToast("Job record removed.");
            await fetchJobs();
        } else {
            showToast("Failed to delete job.");
        }
    } catch (e) {
        console.error("Error deleting job", e);
        showToast("⚠️ Could not delete job: " + e.message);
    }
}

// -------------------------------------------------------------
// Target Companies Management
// -------------------------------------------------------------
// Target Companies Management & Trending Catalog
// -------------------------------------------------------------
function renderTrendingChips() {
    const container = document.getElementById("catalog-chips-container") || document.getElementById("trending-chips-list");
    if (!container) return;

    if (!state.catalog || state.catalog.length === 0) {
        container.innerHTML = `<span style="font-size: 13px; color: var(--text-dim);">Loading recommended direct portals...</span>`;
        return;
    }

    const trackedNames = new Set(state.companies.map(c => c.name.toLowerCase()));

    container.innerHTML = state.catalog.map(item => {
        const isTracked = trackedNames.has(item.name.toLowerCase());
        if (isTracked) {
            return `<button type="button" class="chip-btn tracked" disabled title="Currently monitored">✓ ${escapeHTML(item.name)}</button>`;
        } else {
            return `<button type="button" class="chip-btn" onclick="quickAddCompany('${escapeHTML(item.name)}')">➕ ${escapeHTML(item.name)}</button>`;
        }
    }).join("");
}

async function quickAddCompany(name) {
    try {
        const res = await fetch(`${API_BASE}/api/companies`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ name })
        });

        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || "Failed to track company");
        }

        showToast(`Tracking ${name} with verified intelligence!`);
        await Promise.all([fetchCompanies(), fetchCompanyCatalog()]);
    } catch (e) {
        alert(e.message);
    }
}

async function seedCompanyCatalog() {
    const btn = document.getElementById("btn-seed-catalog") || document.getElementById("preseed-all-btn");
    if (btn) {
        btn.disabled = true;
        btn.innerText = "⏳ Seeding...";
    }

    try {
        const res = await fetch(`${API_BASE}/api/companies/seed`, { method: "POST" });
        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || "Failed to seed companies");
        }
        const data = await res.json();
        showToast(data.message || "Top tech companies seeded!");
        await Promise.all([fetchCompanies(), fetchCompanyCatalog()]);
    } catch (e) {
        alert("Failed to seed organizations: " + e.message);
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.innerText = "⭐ Seed Top Tech Orgs";
        }
    }
}
const preseedAllCompanies = seedCompanyCatalog;

async function handleCompanyFileImport(event) {
    const file = event.target.files[0];
    if (!file) return;

    const formData = new FormData();
    formData.append("file", file);

    showToast(`📁 Importing companies from ${file.name}...`);

    try {
        const res = await fetch(`${API_BASE}/api/companies/import`, {
            method: "POST",
            body: formData
        });

        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || "Import failed");
        }

        const data = await res.json();
        showToast(data.message || "Companies imported successfully!");
        await Promise.all([fetchCompanies(), fetchCompanyCatalog()]);
    } catch (e) {
        alert("Failed to import companies: " + e.message);
    } finally {
        event.target.value = "";
    }
}

function toggleAddCompanyForm() {
    const form = document.getElementById("add-company-form");
    const btn = document.getElementById("btn-toggle-add-company");
    if (!form) return;
    const isHidden = form.classList.contains("hidden");
    if (isHidden) {
        form.classList.remove("hidden");
        if (btn) btn.innerHTML = "✕ Close Form";
        const firstInput = document.getElementById("company-name");
        if (firstInput) firstInput.focus();
    } else {
        form.classList.add("hidden");
        if (btn) btn.innerHTML = "➕ Add Company";
    }
}

function setCompanyFilter(filter) {
    state.companyFilter = filter;
    ["all", "direct", "network"].forEach(f => {
        const tab = document.getElementById(`tab-comp-${f}`);
        if (tab) {
            if (f === filter) tab.classList.add("active");
            else tab.classList.remove("active");
        }
    });
    renderCompanies();
}

function setCompanyLayout(layout) {
    state.companyLayout = layout;
    localStorage.setItem("job_agent_company_layout", layout);
    const gridBtn = document.getElementById("btn-company-view-grid");
    const listBtn = document.getElementById("btn-company-view-list");
    if (gridBtn && listBtn) {
        if (layout === "grid") {
            gridBtn.classList.add("active");
            listBtn.classList.remove("active");
        } else {
            listBtn.classList.add("active");
            gridBtn.classList.remove("active");
        }
    }
    renderCompanies();
}

function onCompanySearchChange(e) {
    state.companySearchQuery = (e.target.value || "").trim().toLowerCase();
    renderCompanies();
}

function renderCompanies() {
    const listEl = document.getElementById("companies-list") || document.getElementById("company-list");
    const badgeEl = document.getElementById("companies-count-badge");
    const totalCount = state.companies.length;
    
    // Tab counters
    const directCount = state.companies.filter(c => c.careers_url && c.careers_url.trim().length > 0).length;
    const networkCount = totalCount - directCount;

    if (badgeEl) badgeEl.innerText = `${totalCount} Organizations`;
    const countAllEl = document.getElementById("company-filter-all-count");
    const countDirectEl = document.getElementById("company-filter-direct-count");
    const countNetworkEl = document.getElementById("company-filter-network-count");
    if (countAllEl) countAllEl.innerText = totalCount;
    if (countDirectEl) countDirectEl.innerText = directCount;
    if (countNetworkEl) countNetworkEl.innerText = networkCount;

    // View toggle active state
    const gridBtn = document.getElementById("btn-company-view-grid");
    const listBtn = document.getElementById("btn-company-view-list");
    if (gridBtn && listBtn) {
        if (state.companyLayout === "list") {
            listBtn.classList.add("active");
            gridBtn.classList.remove("active");
        } else {
            gridBtn.classList.add("active");
            listBtn.classList.remove("active");
        }
    }

    renderTrendingChips();

    if (!listEl) return;

    if (state.companyLayout === "list") {
        listEl.classList.add("list-view");
    } else {
        listEl.classList.remove("list-view");
    }

    if (totalCount === 0) {
        listEl.innerHTML = `
            <div class="empty-state">
                <div class="empty-icon">🏢</div>
                <p>No target companies added yet. Click any <strong>Recommended Direct Portal</strong> chip above or click <strong>⭐ Seed Top Tech Orgs</strong>!</p>
            </div>
        `;
        return;
    }

    // Apply Search and Category Filters
    let filtered = state.companies;
    if (state.companyFilter === "direct") {
        filtered = filtered.filter(c => c.careers_url && c.careers_url.trim().length > 0);
    } else if (state.companyFilter === "network") {
        filtered = filtered.filter(c => !c.careers_url || c.careers_url.trim().length === 0);
    }

    if (state.companySearchQuery) {
        filtered = filtered.filter(c => {
            const name = (c.name || "").toLowerCase();
            const dom = (c.domain || "").toLowerCase();
            const desc = (c.description || "").toLowerCase();
            return name.includes(state.companySearchQuery) || dom.includes(state.companySearchQuery) || desc.includes(state.companySearchQuery);
        });
    }

    if (filtered.length === 0) {
        listEl.innerHTML = `
            <div class="empty-state" style="grid-column: 1 / -1;">
                <div class="empty-icon">🔍</div>
                <p>No companies found matching <strong>"${escapeHTML(state.companySearchQuery || state.companyFilter)}"</strong>.</p>
            </div>
        `;
        return;
    }

    if (state.companyLayout === "list") {
        // Render Sleek Compact Row List View
        listEl.innerHTML = filtered.map(c => {
            const initial = (c.name || 'C').charAt(0).toUpperCase();
            const hasCareers = c.careers_url && c.careers_url.trim().length > 0;
            return `
            <div class="company-list-row">
                <div class="company-list-left">
                    <div class="company-logo-avatar">${initial}</div>
                    <div>
                        <div style="font-weight: 600; color: var(--text-main); font-size: 14px;">${escapeHTML(c.name)}</div>
                        <div class="company-domain-link">🌐 ${c.domain ? `<a href="https://${escapeHTML(c.domain)}" target="_blank">${escapeHTML(c.domain)}</a>` : 'Domain N/A'}</div>
                    </div>
                </div>
                <div class="company-list-mid">
                    <div>
                        ${hasCareers ? `<a href="${escapeHTML(c.careers_url)}" target="_blank" class="portal-badge" title="Open Careers Page">Careers Portal ↗</a>` : `<span class="portal-badge portal-missing">LinkedIn / Network</span>`}
                    </div>
                    <div class="company-list-meta-chip">
                        💰 ${escapeHTML(c.salary_insights ? c.salary_insights.split('.')[0] : 'Competitive')}
                    </div>
                </div>
                <div class="company-list-right">
                    <button class="btn btn-sm btn-primary" onclick="scrapeCompanyJobs(${c.id})" title="Scan this company's career portal for new jobs">⚡ Scan</button>
                    <button class="btn btn-sm btn-outline" onclick="scanAndOpenCompanyRadar(${c.id}, '${escapeHTML(c.name)}', this)" title="Scan ${escapeHTML(c.name)} and open Opportunity Radar">🛰️ Radar</button>
                    <button class="btn btn-sm btn-outline" onclick="openEditCompanyModal(${c.id})" title="Edit Careers Portal URL / Domain">✏️ Edit</button>
                    <button class="btn btn-sm btn-outline" onclick="refreshCompanyIntel(${c.id})" title="Refresh Web Intelligence">🔄</button>
                    <button class="btn btn-sm btn-danger" onclick="deleteCompany(${c.id})" title="Remove Company">🗑️</button>
                </div>
            </div>
            `;
        }).join("");
    } else {
        // Render Responsive Tile Grid View
        listEl.innerHTML = filtered.map(c => {
            const initial = (c.name || 'C').charAt(0).toUpperCase();
            const hasCareers = c.careers_url && c.careers_url.trim().length > 0;
            
            return `
            <div class="company-card enhanced-company-card">
                <div class="company-card-top">
                    <div class="company-brand">
                        <div class="company-logo-avatar">${initial}</div>
                        <div class="company-brand-info">
                            <div class="company-title-row">
                                <h5>${escapeHTML(c.name)}</h5>
                                ${hasCareers ? `<a href="${escapeHTML(c.careers_url)}" target="_blank" class="portal-badge" title="Open Careers Page">Careers Portal ↗</a>` : `<span class="portal-badge portal-missing">LinkedIn / Network</span>`}
                            </div>
                            <p class="company-domain-link">
                                🌐 ${c.domain ? `<a href="https://${escapeHTML(c.domain)}" target="_blank">${escapeHTML(c.domain)}</a>` : 'Domain N/A'}
                            </p>
                        </div>
                    </div>
                    <div class="company-card-actions">
                        <button class="btn btn-sm btn-primary" onclick="scrapeCompanyJobs(${c.id})" title="Scan this company's career portal for new jobs">⚡ Scan</button>
                        <button class="btn btn-sm btn-outline" onclick="scanAndOpenCompanyRadar(${c.id}, '${escapeHTML(c.name)}', this)" title="Scan ${escapeHTML(c.name)} and open Opportunity Radar">🛰️ Radar</button>
                        <button class="btn btn-sm btn-outline" onclick="openEditCompanyModal(${c.id})" title="Edit Careers Portal URL / Domain">✏️</button>
                        <button class="btn btn-sm btn-outline" onclick="refreshCompanyIntel(${c.id})" title="Refresh Web Intelligence">🔄</button>
                        <button class="btn btn-sm btn-danger" onclick="deleteCompany(${c.id})" title="Remove Company">🗑️</button>
                    </div>
                </div>

                ${c.description ? `
                <div class="company-desc-snippet">
                    ${escapeHTML(c.description)}
                </div>` : ''}

                <div class="company-intel-grid">
                    <div class="intel-block">
                        <span class="intel-label">📰 News & Products</span>
                        <p class="intel-val">${escapeHTML(c.recent_news || 'Intelligence updated on scan.')}</p>
                    </div>
                    <div class="intel-block">
                        <span class="intel-label">💰 Salary & Interview</span>
                        <p class="intel-val">${escapeHTML(c.salary_insights || 'Competitive')} • ${escapeHTML(c.hiring_process || 'Technical')}</p>
                    </div>
                    ${c.market_position ? `
                    <div class="intel-block">
                        <span class="intel-label">🏢 Market Position</span>
                        <p class="intel-val">${escapeHTML(c.market_position)}</p>
                    </div>` : ''}
                    ${c.talking_points ? `
                    <div class="intel-block">
                        <span class="intel-label">💡 Talking Points</span>
                        <p class="intel-val">${escapeHTML(c.talking_points)}</p>
                    </div>` : ''}
                </div>
            </div>
            `;
        }).join("");
    }
}

async function handleAddCompany(e) {
    if (e && e.preventDefault) e.preventDefault();
    const nameInput = document.getElementById("company-name") || document.getElementById("company-name-input");
    const domainInput = document.getElementById("company-domain") || document.getElementById("company-domain-input");
    const careersInput = document.getElementById("company-careers") || document.getElementById("company-careers-input");

    if (!nameInput || !nameInput.value.trim()) return;

    const payload = {
        name: nameInput.value.trim(),
        domain: domainInput ? domainInput.value.trim() || undefined : undefined,
        careers_url: careersInput ? careersInput.value.trim() || undefined : undefined
    };

    try {
        const res = await fetch(`${API_BASE}/api/companies`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload)
        });

        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || "Failed to add company");
        }

        nameInput.value = "";
        if (domainInput) domainInput.value = "";
        if (careersInput) careersInput.value = "";
        const form = document.getElementById("add-company-form");
        const toggleBtn = document.getElementById("btn-toggle-add-company");
        if (form) form.classList.add("hidden");
        if (toggleBtn) toggleBtn.innerHTML = "➕ Add Company";
        showToast("Company added and intelligence resolved!");
        await Promise.all([fetchCompanies(), fetchCompanyCatalog()]);
    } catch (e) {
        alert(e.message);
    }
}

function openEditCompanyModal(companyId) {
    const company = state.companies.find(c => c.id === companyId);
    if (!company) return;

    document.getElementById("edit-company-id").value = company.id;
    document.getElementById("edit-company-name").value = company.name || "";
    document.getElementById("edit-company-domain").value = company.domain || "";
    document.getElementById("edit-company-careers").value = company.careers_url || "";
    document.getElementById("edit-company-desc").value = company.description || "";

    const modal = document.getElementById("edit-company-modal");
    if (modal) modal.classList.remove("hidden");
}

function closeEditCompanyModal() {
    const modal = document.getElementById("edit-company-modal");
    if (modal) modal.classList.add("hidden");
}

async function saveCompanyEdit(e) {
    e.preventDefault();
    const id = document.getElementById("edit-company-id").value;
    const name = document.getElementById("edit-company-name").value.trim();
    const domain = document.getElementById("edit-company-domain").value.trim();
    const careers_url = document.getElementById("edit-company-careers").value.trim();
    const description = document.getElementById("edit-company-desc").value.trim();

    try {
        const res = await fetch(`${API_BASE}/api/companies/${id}`, {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ name, domain, careers_url, description })
        });

        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || "Failed to update company");
        }

        closeEditCompanyModal();
        showToast("Company details updated successfully!");
        fetchCompanies();
    } catch (err) {
        alert(err.message);
    }
}

async function refreshCompanyIntel(companyId) {
    showToast("Refreshing company intelligence from live web...");
    try {
        const res = await fetch(`${API_BASE}/api/companies/${companyId}/refresh-intel`, { method: "POST" });
        if (res.ok) {
            showToast("Company intelligence updated!");
            fetchCompanies();
        }
    } catch (e) {
        console.error("Failed to refresh intel", e);
    }
}

async function scrapeCompanyJobs(companyId) {
    const comp = state.companies.find(c => c.id === companyId);
    showToast(`⚡ Scanning career postings for ${comp ? comp.name : 'company'}...`);
    const prefs = getTargetPreferences();
    const params = new URLSearchParams();
    if (prefs.target_titles) params.append("target_titles", prefs.target_titles);
    if (prefs.target_locations) params.append("target_locations", prefs.target_locations);
    if (prefs.target_cities) params.append("target_cities", prefs.target_cities);
    if (prefs.work_mode) params.append("work_mode", prefs.work_mode);
    const qs = params.toString() ? `?${params.toString()}` : '';

    try {
        const res = await fetch(`${API_BASE}/api/companies/${companyId}/scrape-jobs${qs}`, { method: "POST" });
        if (!res.ok) throw new Error("Scraping failed");
        const data = await res.json();
        showToast(data.message || "Scrape completed!");
        fetchJobs();
    } catch (e) {
        alert("Failed to scan company jobs: " + e.message);
    }
}

// Radar from the companies list: scan that company's portal FIRST (fresh discovery),
// then open the Opportunity Radar so the ranked feed reflects the new postings.
async function scanAndOpenCompanyRadar(companyId, companyName, btnEl) {
    const label = btnEl ? btnEl.innerHTML : "";
    if (btnEl) {
        btnEl.disabled = true;
        btnEl.innerHTML = `<span class="spinner" style="width:12px;height:12px;display:inline-block;"></span> Scanning...`;
    }
    showToast(`⚡ Scanning ${companyName} — Radar will open when the scan finishes...`);

    try {
        const prefs = getTargetPreferences();
        const params = new URLSearchParams();
        if (prefs.target_titles) params.append("target_titles", prefs.target_titles);
        if (prefs.target_locations) params.append("target_locations", prefs.target_locations);
        if (prefs.target_cities) params.append("target_cities", prefs.target_cities);
        if (prefs.work_mode) params.append("work_mode", prefs.work_mode);
        const qs = params.toString() ? `?${params.toString()}` : "";

        const res = await fetch(`${API_BASE}/api/companies/${companyId}/scrape-jobs${qs}`, { method: "POST" });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.detail || "Scan failed");
        showToast(data.message || "Scan completed.");
        await fetchJobs();
        if (typeof fetchCompanies === "function") await fetchCompanies();
    } catch (e) {
        showToast(`⚠️ Scan failed before Radar: ${e.message}`);
    } finally {
        if (btnEl) {
            btnEl.disabled = false;
            btnEl.innerHTML = label || "🛰️ Radar";
        }
    }

    // Open the Radar regardless, so the user still sees whatever is ranked.
    openOpportunityRadarModal(companyName);
}

async function deleteCompany(companyId) {
    if (!confirm("Are you sure you want to remove this company?")) return;
    try {
        await fetch(`${API_BASE}/api/companies/${companyId}`, { method: "DELETE" });
        showToast("Company removed.");
        await Promise.all([fetchCompanies(), fetchCompanyCatalog()]);
        fetchJobs();
    } catch (e) {
        console.error("Delete company error", e);
    }
}

// -------------------------------------------------------------
// Resume Upload & ATS Viewer
// -------------------------------------------------------------
async function handleResumeUpload(e) {
    const file = e.target.files[0];
    if (!file) return;

    const formData = new FormData();
    formData.append("file", file);

    const loadingEl = document.getElementById("resume-loading");
    loadingEl.classList.remove("hidden");

    try {
        const res = await fetch(`${API_BASE}/api/resume/upload`, {
            method: "POST",
            body: formData
        });

        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || "Resume upload failed");
        }

        state.resume = await res.json();
        renderResumeDetails();
        showToast("Resume uploaded & parsed successfully!");
    } catch (err) {
        alert(err.message);
    } finally {
        loadingEl.classList.add("hidden");
    }
}

function normalizeDisplayText(value) {
    // Collapses per-word line breaks from PDF extractors into readable single-line text
    // and normalizes bullet glyphs. Returns "" for empty input.
    if (value === null || value === undefined) return "";
    if (Array.isArray(value)) value = value.map(v => String(v)).join(" ");
    return String(value)
        .replace(/\s+/g, " ")
        .replace(/\s*[●•▪◦]\s*/g, " • ")
        .replace(/^•\s*/, "")
        .trim();
}

function renderResumeDetails() {
    const resumeDetailsEl = document.getElementById("resume-details");
    const uploadZone = document.getElementById("upload-zone");
    const selectorWrapper = document.getElementById("resume-selector-wrapper");
    const selectDropdown = document.getElementById("resume-select-dropdown");

    if (!state.resume) {
        if (resumeDetailsEl) resumeDetailsEl.classList.add("hidden");
        if (uploadZone) uploadZone.classList.remove("hidden");
        return;
    }

    if (resumeDetailsEl) resumeDetailsEl.classList.remove("hidden");
    if (uploadZone) uploadZone.classList.add("hidden");

    // Populate multi-resume selector if > 1 resume exists
    if (state.resumes && state.resumes.length > 1) {
        if (selectorWrapper) selectorWrapper.classList.remove("hidden");
        if (selectDropdown) {
            selectDropdown.innerHTML = state.resumes.map(r => `
                <option value="${r.id}" ${r.id === state.resume.id ? 'selected' : ''}>
                    ${escapeHTML(r.filename)} (${formatDate(r.created_at)}) ${r.is_active ? '★ Active' : ''}
                </option>
            `).join("");
        }
    } else {
        if (selectorWrapper) selectorWrapper.classList.add("hidden");
    }

    document.getElementById("resume-filename").innerText = state.resume.filename;
    document.getElementById("resume-upload-date").innerText = `📅 Uploaded ${formatDate(state.resume.created_at)}`;

    const parsed = state.resume.parsed_json || {};
    document.getElementById("parsed-name").innerText = parsed.name || "-";
    document.getElementById("parsed-email").innerText = parsed.email || "-";
    document.getElementById("parsed-phone").innerText = parsed.phone || "-";
    const summaryText = normalizeDisplayText(parsed.summary)
        || (state.resume.raw_preview ? state.resume.raw_preview.slice(0, 300).replace(/\s+/g, " ").trim() : "-");
    // Use textContent (not innerText) so embedded newlines are never turned into <br>.
    document.getElementById("parsed-summary").textContent = summaryText;

    const skillsContainer = document.getElementById("parsed-skills");
    if (parsed.skills && parsed.skills.length) {
        skillsContainer.innerHTML = parsed.skills.map(s => `<span class="skill-tag">${escapeHTML(s)}</span>`).join("");
    } else {
        skillsContainer.innerHTML = `<span class="text-dim">No skills detected.</span>`;
    }

    const expContainer = document.getElementById("parsed-experience");
    if (parsed.experience && parsed.experience.length) {
        expContainer.innerHTML = parsed.experience.map(exp => {
            const rawDetails = exp.details || '';
            const bullets = rawDetails.includes(' • ') 
                ? rawDetails.split(' • ').map(b => b.trim()).filter(Boolean)
                : rawDetails.includes('\n')
                ? rawDetails.split('\n').map(b => b.trim()).filter(Boolean)
                : [rawDetails].filter(Boolean);

            const bulletsHTML = bullets.length 
                ? `<ul style="margin: 8px 0 0 18px; padding: 0; font-size: 13px; color: var(--text-muted); line-height: 1.6;">
                    ${bullets.map(b => `<li style="margin-bottom: 6px;">${escapeHTML(b)}</li>`).join("")}
                   </ul>`
                : '';

            return `
                <div style="margin-bottom: 16px; background: rgba(255,255,255,0.02); border: 1px solid var(--border-color); border-radius: 10px; padding: 16px 18px;">
                    <div style="display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 6px; flex-wrap: wrap; gap: 8px;">
                        <div>
                            <strong style="font-size: 15px; color: var(--text-main); font-weight: 700;">${escapeHTML(exp.title || 'Role')}</strong>
                            <span style="color: var(--primary); font-weight: 600; margin-left: 6px;">@ ${escapeHTML(exp.company || 'Company')}</span>
                        </div>
                        <span class="date-tag" style="font-size: 12px; padding: 3px 8px;">📅 ${escapeHTML(exp.dates || 'Dates not specified')}</span>
                    </div>
                    ${bulletsHTML}
                </div>
            `;
        }).join("");
    } else {
        expContainer.innerHTML = `<p class="text-dim" style="font-size:12px;">Work experience parsed in profile summary.</p>`;
    }
}

async function activateResume(resumeId) {
    try {
        const res = await fetch(`${API_BASE}/api/resume/${resumeId}/activate`, { method: "POST" });
        if (!res.ok) throw new Error("Failed to switch active resume");
        state.resume = await res.json();
        await fetchResume();
        showToast("Switched active resume profile!");
    } catch (e) {
        alert(e.message);
    }
}

async function deleteActiveResume() {
    if (!state.resume) return;
    const confirmed = confirm(`Are you sure you want to delete '${state.resume.filename}'?\n\nThis will remove all associated database entries and allow a clean re-parse.`);
    if (!confirmed) return;

    try {
        const res = await fetch(`${API_BASE}/api/resume/${state.resume.id}`, { method: "DELETE" });
        if (!res.ok) throw new Error("Failed to delete resume");
        showToast("Resume deleted cleanly.");
        state.resume = null;
        await fetchResume();
    } catch (e) {
        alert(e.message);
    }
}

async function addResumeSkill(newSkill) {
    if (!state.resume) return;
    const skill = (newSkill || "").trim();
    if (!skill) return;
    const currentSkills = (state.resume.parsed_json?.skills || []).slice();
    if (currentSkills.includes(skill)) return;
    currentSkills.push(skill);
    try {
        const res = await fetch(`${API_BASE}/api/resume/${state.resume.id}/skills`, {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ skills: currentSkills })
        });
        if (res.ok) {
            state.resume = await res.json();
            renderResumeDetails();
            showToast("Added skill to profile.");
        }
    } catch (e) {
        console.error("Error adding resume skill", e);
    }
}

async function removeResumeSkill(skillToRemove) {
    if (!state.resume) return;
    const currentSkills = (state.resume.parsed_json?.skills || []).filter(s => s !== skillToRemove);
    try {
        const res = await fetch(`${API_BASE}/api/resume/${state.resume.id}/skills`, {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ skills: currentSkills })
        });
        if (res.ok) {
            state.resume = await res.json();
            renderResumeDetails();
            showToast("Skill removed from profile.");
        }
    } catch (e) {
        console.error("Error removing resume skill", e);
    }
}

// -------------------------------------------------------------
// Job Details Modal & Actions
// -------------------------------------------------------------
function openJobDetails(jobId) {
    return openJobDetailsModal(jobId);
}

function openJobModal(job) {
    if (typeof job === "object" && job && job.id) {
        return openJobDetailsModal(job.id);
    }
    return openJobDetailsModal(job);
}

function openJobDetailsModal(jobId) {
    const job = state.jobs.find(j => j.id === jobId);
    if (!job) return;

    state.activeJobModal = job;

    document.getElementById("modal-job-title").innerText = job.title;
    document.getElementById("modal-company-name").innerText = `${job.company_name || 'Direct'} • ${job.location || 'Remote'}`;

    const llmBadge = document.getElementById("modal-llm-badge");
    if (llmBadge) {
        if (state.activeLLM && state.activeLLM.status === "online") {
            llmBadge.innerText = `⚡ AI: ${state.activeLLM.provider} (${state.activeLLM.model})`;
            llmBadge.className = "badge active";
        } else if (state.activeLLM && state.activeLLM.status === "configured") {
            llmBadge.innerText = `⚡ AI: ${state.activeLLM.provider}`;
            llmBadge.className = "badge verified";
        } else {
            llmBadge.innerText = `⚡ AI: Offline Heuristics`;
            llmBadge.className = "badge warning";
        }
    }

    // Score & Analysis
    const hasScore = job.match_score !== null && job.match_score !== undefined;
    const score = hasScore ? Math.round(job.match_score) : null;
    const circleEl = document.getElementById("modal-score-circle");
    if (hasScore) {
        circleEl.innerText = `${score}%`;
        circleEl.title = job.match_scored ? "AI match score" : "Source baseline — not AI match-scored";
    } else {
        circleEl.innerText = "—";
        circleEl.title = "Not yet analyzed";
    }
    const unscoredCue = (!hasScore || !job.match_scored)
        ? "\n\nℹ️ This role has not been AI match-scored. Run '⚡ Tailor AI' to generate a real ATS score."
        : "";
    document.getElementById("modal-analysis-details").innerText = (job.match_analysis || "Comparison summary available.") + unscoredCue;

    // Cover Letter
    document.getElementById("modal-cover-letter-text").value = job.cover_letter_draft || "Click '⚡ Tailor AI' to generate a tailored cover letter for this role.";

    // Tailoring
    document.getElementById("modal-tailoring-text").innerText = job.tailored_resume_points || "Click '⚡ Tailor AI' to generate ATS keyword & bullet point tailoring suggestions.";

    // Cold note
    document.getElementById("modal-cold-msg-text").value = job.cold_message_draft || "Click '⚡ Tailor AI' to generate a concise LinkedIn connection note.";

    // Reset Chat
    document.getElementById("chat-messages").innerHTML = `
        <div class="chat-msg coach">
            Hello! I am your AI interview coach for <b>${escapeHTML(job.title)}</b> at <b>${escapeHTML(job.company_name || 'the company')}</b>.
            Ask me anything about interview stages, system design questions, or how to pitch your background!
        </div>
    `;

    // Load Tech Stack Alignment Matrix & Hiring Team
    loadJobSkillsAlignment(job.id);
    loadJobHiringTeam(job.id);
    loadJobActivity(job.id);

    // Reset active tab to Match
    const firstTabBtn = document.querySelector('.modal-tab-btn[data-tab="match"]');
    if (firstTabBtn) firstTabBtn.click();

    document.getElementById("details-modal").classList.remove("hidden");
}

async function loadJobSkillsAlignment(jobId) {
    const matchedContainer = document.getElementById("modal-matched-skills-grid");
    const gapContainer = document.getElementById("modal-gap-skills-grid");
    const badge = document.getElementById("modal-stack-badge");
    const fill = document.getElementById("modal-stack-progress-fill");
    const summary = document.getElementById("modal-stack-summary");
    const matchedCount = document.getElementById("modal-stack-count-matched");
    const gapCount = document.getElementById("modal-stack-count-gaps");

    if (!matchedContainer || !gapContainer) return;
    matchedContainer.innerHTML = '<span style="font-size:12px; color:var(--text-dim);">Scanning technical stack...</span>';
    gapContainer.innerHTML = '';

    try {
        const res = await fetch(`${API_BASE}/api/jobs/${jobId}/skills-alignment`);
        if (!res.ok) throw new Error("Failed to load skills alignment");
        const data = await res.json();

        const hasStackScore = data.compatibility_pct !== null && data.compatibility_pct !== undefined;
        if (badge) badge.innerText = hasStackScore ? `${data.compatibility_pct}% Stack Match` : "Not analyzed";
        if (fill) fill.style.width = hasStackScore ? `${data.compatibility_pct}%` : "0%";
        if (summary) summary.innerText = data.summary;
        if (matchedCount) matchedCount.innerText = `${data.matched_count} Matched in Resume`;
        if (gapCount) gapCount.innerText = `${data.gap_count} Growth Areas`;

        if (data.matched_skills && data.matched_skills.length > 0) {
            matchedContainer.innerHTML = data.matched_skills.map(s => `
                <span class="skill-chip-matched">✓ ${escapeHTML(s.name)}</span>
            `).join("");
        } else {
            matchedContainer.innerHTML = '<span style="font-size:12px; color:var(--text-dim); font-style:italic;">No direct stack overlap parsed yet.</span>';
        }

        if (data.gap_skills && data.gap_skills.length > 0) {
            gapContainer.innerHTML = data.gap_skills.map(s => `
                <span class="skill-chip-gap" onclick="bridgeSkillGapInTailoring('${escapeHTML(s.name)}')" title="Click to include ${escapeHTML(s.name)} in resume tailoring tips">
                    ⚡ ${escapeHTML(s.name)} <span style="font-size:10px; opacity:0.7;">+ Tailor</span>
                </span>
            `).join("");
        } else {
            gapContainer.innerHTML = '<span style="font-size:12px; color:#a6e3a1;">🎉 Full stack alignment! All detected key requirements exist in your profile.</span>';
        }
    } catch (e) {
        matchedContainer.innerHTML = '<span style="font-size:12px; color:var(--text-dim);">Could not compute stack alignment.</span>';
    }
}

async function loadJobActivity(jobId) {
    const container = document.getElementById("modal-activity-timeline");
    if (!container || !jobId) return;
    container.innerHTML = '<span style="font-size:12px; color:var(--text-dim);">Loading activity…</span>';

    try {
        const res = await fetch(`${API_BASE}/api/jobs/${jobId}/events`);
        if (!res.ok) throw new Error("Failed to load activity");
        const events = await res.json();
        if (!events.length) {
            container.innerHTML = '<span style="font-size:12px; color:var(--text-dim);">No recorded activity for this role yet.</span>';
            return;
        }
        const icons = {
            status_change: "🔀",
            note: "📝",
            interview: "🗓️",
            email_received: "📧",
            created: "🆕"
        };
        container.innerHTML = events.map(ev => {
            const icon = icons[ev.event_type] || "•";
            const when = ev.timestamp ? new Date(ev.timestamp).toLocaleString() : "";
            const label = (ev.event_type || "").replace(/_/g, " ");
            return `
                <div class="activity-item">
                    <div class="activity-icon">${icon}</div>
                    <div class="activity-body">
                        <div class="activity-meta">
                            <span class="activity-type">${escapeHTML(label)}</span>
                            <span class="activity-time">${escapeHTML(when)}</span>
                        </div>
                        <div class="activity-desc">${escapeHTML(ev.description || "")}</div>
                    </div>
                </div>
            `;
        }).join("");
    } catch (e) {
        container.innerHTML = '<span style="font-size:12px; color:var(--text-dim);">Could not load activity history.</span>';
    }
}

async function loadJobHiringTeam(jobId) {
    const container = document.getElementById("modal-hiring-team-list");
    if (!container || !jobId) return;
    container.innerHTML = '<div style="padding:15px; text-align:center; color:var(--text-dim);"><div class="spinner" style="margin:0 auto 8px auto;"></div>Locating hiring team and recruiters...</div>';

    try {
        const res = await fetch(`${API_BASE}/api/jobs/${jobId}/hiring-team`);
        if (!res.ok) throw new Error("Failed to load hiring team");
        const data = await res.json();

        if (!data.contacts || data.contacts.length === 0) {
            container.innerHTML = `
                <div style="background:rgba(255,255,255,0.03); border:1px solid var(--border-color); border-radius:8px; padding:15px; text-align:center; color:var(--text-dim);">
                    No verified contacts recorded yet for ${escapeHTML(data.company_name)}.
                </div>
            `;
            return;
        }

        container.innerHTML = data.contacts.map(c => {
            const initials = (c.author_name || "H L").split(" ").map(p => p[0]).join("").slice(0, 2).toUpperCase();
            return `
                <div class="hiring-contact-card">
                    <div class="hiring-contact-info">
                        <div class="hiring-contact-avatar">${escapeHTML(initials)}</div>
                        <div class="hiring-contact-details">
                            <h5>${escapeHTML(c.author_name)}</h5>
                            <p>${escapeHTML(c.author_headline)}</p>
                            <span style="font-size:10px; color:#89b4fa; text-transform:uppercase; font-weight:600; margin-top:2px; display:inline-block;">📡 ${escapeHTML(c.source)}</span>
                        </div>
                    </div>
                    <div class="hiring-contact-actions">
                        ${c.author_profile_url ? `
                            <a href="${c.author_profile_url}" target="_blank" class="btn btn-sm btn-outline" style="font-size:11px; padding:4px 8px;" title="View LinkedIn Profile">
                                👤 Profile ↗
                            </a>
                        ` : ''}
                        <button class="btn btn-sm btn-primary" style="font-size:11px; padding:4px 8px;" onclick="draftRecruiterOutreachFromContact('${escapeHTML(c.author_name)}', '${escapeHTML(state.activeJobModal?.title || '')}', '${escapeHTML(data.company_name)}')" title="Draft personalized recruiter outreach note">
                            💬 Draft Note
                        </button>
                    </div>
                </div>
            `;
        }).join("");
    } catch (e) {
        container.innerHTML = `<div style="color:var(--text-dim); padding:10px;">Could not load hiring contacts.</div>`;
    }
}

function bridgeSkillGapInTailoring(skillName) {
    if (!state.activeJobModal) return;
    const tabTailorBtn = document.querySelector('.modal-tab-btn[data-tab="tailoring"]');
    if (tabTailorBtn) tabTailorBtn.click();
    const tailoringBox = document.getElementById("modal-tailoring-text");
    if (tailoringBox) {
        const existing = tailoringBox.innerText.replace(/Click '⚡ Tailor AI'.*/, '').trim();
        const bridgeTip = `🎯 **Target Skill Bridge (${skillName})**: Highlight your experience architecting data pipelines and query performance, and emphasize quick adaptability with ${skillName} in your project bullet points.\n\n`;
        tailoringBox.innerText = bridgeTip + existing;
        showToast(`⚡ Added tailoring tip for '${skillName}'`);
    }
}

function draftRecruiterOutreachFromContact(recruiterName, roleTitle, compName) {
    const tabOutreachBtn = document.querySelector('.modal-tab-btn[data-tab="outreach"]');
    if (tabOutreachBtn) tabOutreachBtn.click();
    const coldMsgEl = document.getElementById("modal-cold-msg-text");
    if (coldMsgEl) {
        const currentMsg = coldMsgEl.value;
        const personalized = `Hi ${recruiterName}, I noticed you are leading talent acquisition for ${compName}. With my backend and distributed systems experience, I'm excited about the ${roleTitle} role. Would love to connect!`;
        coldMsgEl.value = personalized;
        showToast(`💬 Personalized outreach note for ${recruiterName}!`);
    }
}

async function handleChatSubmit(e) {
    e.preventDefault();
    if (!state.activeJobModal) return;

    const input = document.getElementById("chat-user-input");
    const msg = input.value.trim();
    if (!msg) return;

    const container = document.getElementById("chat-messages");
    container.innerHTML += `<div class="chat-msg user">${escapeHTML(msg)}</div>`;
    input.value = "";
    container.scrollTop = container.scrollHeight;

    try {
        const res = await fetch(`${API_BASE}/api/jobs/${state.activeJobModal.id}/chat`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ message: msg })
        });
        const data = await res.json();
        container.innerHTML += `<div class="chat-msg coach">${escapeHTML(data.reply)}</div>`;
        container.scrollTop = container.scrollHeight;
    } catch (err) {
        container.innerHTML += `<div class="chat-msg coach text-danger">Error fetching coach advice.</div>`;
    }
}

async function updateJobStatus(jobId, newStatus, selectEl) {
    try {
        const res = await fetch(`${API_BASE}/api/jobs/${jobId}/status`, {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ status: newStatus })
        });
        if (res.ok) {
            const updated = await res.json();
            const idx = state.jobs.findIndex(j => j.id === jobId);
            if (idx !== -1) state.jobs[idx] = updated;
            if (selectEl) {
                const slug = newStatus.toLowerCase().replace(/\s+/g, '-');
                selectEl.className = `status-dropdown status-${slug}`;
            }
            updateKPIStats();
            const trackerSection = document.getElementById("tracker-section");
            if (trackerSection && !trackerSection.classList.contains("hidden")) {
                renderTrackerList();
            }
            showToast(`Status updated to '${newStatus}'`);
        }
    } catch (e) {
        console.error("Failed to update job status", e);
    }
}

let scrapeAbortController = null;
let linkedinSyncAbortController = null;

function getTargetPreferences() {
    const countryVal = state.targetCountry === "all" ? "" : state.targetCountry;
    const workModeVal = state.targetWorkMode === "all" ? "" : state.targetWorkMode;
    return {
        target_titles: state.targetRoles.join(", "),
        target_locations: countryVal,
        target_cities: state.targetCities.join(", "),
        work_mode: workModeVal,
        timezone: state.userTimezone || ""
    };
}

function populateTimezoneSelect() {
    const el = document.getElementById("set-timezone");
    if (!el) return;
    let zones = [];
    try { zones = Intl.supportedValuesOf("timeZone"); } catch (e) { zones = []; }
    if (!zones || !zones.length) {
        zones = ["UTC", "Asia/Kolkata", "Asia/Dubai", "Asia/Singapore", "Europe/London",
                 "Europe/Dublin", "Europe/Berlin", "America/New_York", "America/Los_Angeles", "Australia/Sydney"];
    }
    if (!zones.includes("UTC")) zones = ["UTC", ...zones];
    el.innerHTML = zones.map(z => `<option value="${escapeHTML(z)}">${escapeHTML(z)}</option>`).join("");
    if (state.userTimezone && !zones.includes(state.userTimezone)) {
        el.insertAdjacentHTML("afterbegin", `<option value="${escapeHTML(state.userTimezone)}">${escapeHTML(state.userTimezone)}</option>`);
    }
    el.value = state.userTimezone || "UTC";
}

function handleTimezoneChange(value) {
    state.userTimezone = value || "UTC";
    localStorage.setItem("job_agent_timezone", state.userTimezone);
    schedulePreferencesPersist();
    // Refresh any time-bearing views that are already rendered.
    try { if (typeof fetchOperationLogs === "function") fetchOperationLogs(); } catch (e) {}
    try { if (typeof fetchTasksStatus === "function") fetchTasksStatus(); } catch (e) {}
    try { if (typeof applyJobFilters === "function") applyJobFilters(); } catch (e) {}
    showToast(`🕒 Timezone set to ${state.userTimezone}`);
}

// -------------------------------------------------------------
// Server-persisted preferences, opt-in feature flags & BYOK settings
// -------------------------------------------------------------
let featureFlags = {};
let availableFeatures = {};

const ALL_FEATURE_KEYS = [
    "ats_portals", "google_jobs", "jobspy_google", "linkedin_sync", "gmail_sync",
    "ats_greenhouse", "ats_lever", "ats_ashby", "ats_hirist",
    "ats_smartrecruiters", "ats_workable", "ats_workday", "ats_uber", "ats_custom_html"
];

function applyFeatureFlags(flags) {
    featureFlags = flags || {};
    const map = {
        ats_portals: "trigger-scrape-btn",
        google_jobs: "trigger-google-jobs-btn",
        linkedin_sync: "trigger-linkedin-sync-btn"
    };
    Object.entries(map).forEach(([feat, id]) => {
        const el = document.getElementById(id);
        if (el) el.style.display = featureFlags[feat] ? "" : "none";
    });
    // (b) LinkedIn-free fallback hint: reassure that LinkedIn roles are still discoverable.
    const liHint = document.getElementById("linkedin-free-hint");
    if (liHint) {
        liHint.innerText = featureFlags.linkedin_sync
            ? ""
            : "LinkedIn automation is off — LinkedIn roles still flow in via Gmail job-alert emails and Google for Jobs.";
    }
    // (b2) One-time runtime warning per session when authenticated LinkedIn automation is active.
    if (featureFlags.linkedin_sync && !sessionStorage.getItem("job_agent_linkedin_warned")) {
        sessionStorage.setItem("job_agent_linkedin_warned", "1");
        showToast("⚠️ LinkedIn automation is ON — automated session use may risk account limits. Use at your own risk.");
    }
    // (c) Sync All: reflect how many stages the one-shot will run (mirrors backend stage plan).
    const statusEnabled = !!(featureFlags.ats_hirist || featureFlags.ats_greenhouse
        || featureFlags.ats_lever || featureFlags.ats_smartrecruiters || featureFlags.gmail_sync);
    const enabled = (featureFlags.ats_portals ? 1 : 0)
        + (featureFlags.google_jobs ? 1 : 0)
        + (featureFlags.linkedin_sync ? 1 : 0)
        + (statusEnabled ? 1 : 0);
    const syncAllBtn = document.getElementById("trigger-sync-all-btn");
    const countEl = document.getElementById("sync-all-enabled-count");
    if (countEl) countEl.textContent = enabled;
    if (syncAllBtn) {
        syncAllBtn.disabled = enabled === 0;
        syncAllBtn.title = enabled === 0
            ? "No channels enabled — enable discovery features in Settings → Discovery Features."
            : "Run every enabled discovery & application-status channel in one background task";
    }
}

// -------------------------------------------------------------
// Risk-feature consent gate (explicit acknowledgment required)
// -------------------------------------------------------------
// The backend is the source of truth for which features need consent
// (consent_required_features, mirroring CONSENT_REQUIRED_FEATURES); this fallback only
// covers the first render before preferences load.
const CONSENT_REQUIRED_KEYS_FALLBACK = [
    "linkedin_sync", "jobspy_google",
    "ats_smartrecruiters", "ats_workable", "ats_workday", "ats_uber", "ats_custom_html"
];
let consentRequiredKeys = CONSENT_REQUIRED_KEYS_FALLBACK.slice();
let featureConsents = {};
let consentVersion = 1;
let _pendingConsentKey = null;
let _resumeSaveAfterConsent = false;

function hasConsentFor(key) {
    const rec = featureConsents[key];
    return !!(rec && rec.ack && Number(rec.version || 0) === Number(consentVersion));
}

function initConsentGate() {
    consentRequiredKeys.forEach(key => {
        const el = document.getElementById(`set-feat-${key}`);
        if (!el || el.dataset.consentBound === "1") return;
        el.dataset.consentBound = "1";
        el.addEventListener("change", (e) => {
            // Revert an unacknowledged risk-feature toggle and ask for consent instead.
            if (e.target.checked && !hasConsentFor(key)) {
                e.target.checked = false;
                openConsentModal(key);
            }
        });
    });
    const ack = document.getElementById("consent-ack");
    if (ack && ack.dataset.consentBound !== "1") {
        ack.dataset.consentBound = "1";
        ack.addEventListener("change", () => {
            const btn = document.getElementById("consent-enable-btn");
            if (btn) btn.disabled = !ack.checked;
        });
    }
}

function openConsentModal(key, resumeSave = false) {
    _pendingConsentKey = key;
    _resumeSaveAfterConsent = resumeSave;
    const ack = document.getElementById("consent-ack");
    if (ack) ack.checked = false;
    const btn = document.getElementById("consent-enable-btn");
    if (btn) btn.disabled = true;
    const modal = document.getElementById("consent-modal");
    if (modal) modal.classList.remove("hidden");
}

function closeConsentModal() {
    // If dismissed without acknowledging, restore the toggle to its real (unconsented) state.
    const key = _pendingConsentKey;
    if (key && !hasConsentFor(key)) {
        const el = document.getElementById(`set-feat-${key}`);
        if (el) el.checked = false;
    }
    _pendingConsentKey = null;
    _resumeSaveAfterConsent = false;
    const modal = document.getElementById("consent-modal");
    if (modal) modal.classList.add("hidden");
}

async function confirmConsent() {
    const key = _pendingConsentKey;
    if (!key) { closeConsentModal(); return; }
    const resumeSave = _resumeSaveAfterConsent;
    try {
        const res = await fetch(`${API_BASE}/api/preferences`, {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ consents: { [key]: { ack: true } } }),
        });
        if (!res.ok) throw new Error("Failed to record consent");
        const data = await res.json();
        featureConsents = data.consents || featureConsents;
        consentVersion = data.consent_version || consentVersion;
        const el = document.getElementById(`set-feat-${key}`);
        if (el) el.checked = true;
    } catch (e) {
        showToast("⚠️ " + e.message);
    }
    closeConsentModal();
    // When consent was requested from the Save flow, retry the save now that the gate is satisfied.
    if (resumeSave && hasConsentFor(key)) {
        saveSettingsModal();
    }
}

async function loadPreferencesFromServer() {
    try {
        const res = await fetch(`${API_BASE}/api/preferences`);
        if (!res.ok) return;
        const data = await res.json();
        if (data.target_titles) state.targetRoles = data.target_titles.split(",").map(s => s.trim()).filter(Boolean);
        if (data.target_cities) state.targetCities = data.target_cities.split(",").map(s => s.trim()).filter(Boolean);
        sanitizeTargetCities();
        // Empty server values mean "all" — honor them instead of keeping stale localStorage defaults.
        if (data.target_country !== undefined && data.target_country !== null) {
            state.targetCountry = data.target_country || "all";
        }
        if (data.work_mode !== undefined && data.work_mode !== null) {
            state.targetWorkMode = data.work_mode || "all";
        }
        if (data.timezone) {
            state.userTimezone = data.timezone;
            localStorage.setItem("job_agent_timezone", data.timezone);
        }
        availableFeatures = data.available_features || {};
        featureConsents = data.consents || {};
        consentVersion = data.consent_version || 1;
        if (Array.isArray(data.consent_required_features) && data.consent_required_features.length) {
            consentRequiredKeys = data.consent_required_features;
            initConsentGate();
        }
        if (data.excluded_companies !== undefined && data.excluded_companies !== null) {
            state.excludedCompanies = data.excluded_companies;
        }
        applyFeatureFlags(data.features || {});
        initTargetPreferences();
    } catch (e) {
        console.debug("Server preferences load skipped:", e);
    }
}

async function persistPreferencesToServer(featuresOverride, excludedOverride) {
    try {
        const prefs = getTargetPreferences();
        const payload = { ...prefs, features: featuresOverride || featureFlags };
        const excluded = excludedOverride !== undefined ? excludedOverride : state.excludedCompanies;
        if (excluded !== undefined && excluded !== null) payload.excluded_companies = excluded;
        await fetch(`${API_BASE}/api/preferences`, {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload)
        });
    } catch (e) {
        console.debug("Server preferences save skipped:", e);
    }
}

let _prefsPersistTimer = null;
function schedulePreferencesPersist() {
    // Debounced: persist preference edits so background/scheduled scans stay in sync.
    if (_prefsPersistTimer) clearTimeout(_prefsPersistTimer);
    _prefsPersistTimer = setTimeout(() => { persistPreferencesToServer(); }, 700);
}

function switchSettingsTab(name) {
    document.querySelectorAll("#settings-section [data-settings-tab]").forEach(btn => {
        btn.classList.toggle("active", btn.getAttribute("data-settings-tab") === name);
    });
    ["preferences", "sources", "features", "models", "gmail"].forEach(t => {
        const el = document.getElementById(`settings-tab-${t}`);
        if (el) el.style.display = (t === name) ? "block" : "none";
    });
}

async function populateSettingsPanel() {
    renderTargetRolesChips();
    renderTargetCitiesChips();
    populateTimezoneSelect();
    ALL_FEATURE_KEYS.forEach(f => {
        const el = document.getElementById(`set-feat-${f}`);
        if (el) el.checked = !!featureFlags[f];
    });
    const exclEl = document.getElementById("set-excluded-companies");
    if (exclEl) exclEl.value = (state.excludedCompanies !== undefined && state.excludedCompanies !== null)
        ? state.excludedCompanies : "amazon";
    try {
        const res = await fetch(`${API_BASE}/api/settings/llm`);
        if (res.ok) {
            const s = await res.json();
            document.getElementById("set-allow-cloud").checked = !!s.allow_cloud_fallback;
            document.getElementById("set-openai-status").innerText = s.openai_configured ? "(configured)" : "(not set)";
            document.getElementById("set-gemini-status").innerText = s.gemini_configured ? "(configured)" : "(not set)";
            document.getElementById("set-anthropic-status").innerText = s.anthropic_configured ? "(configured)" : "(not set)";
        }
    } catch (e) { /* settings endpoint optional */ }
    loadGmailSection();
    switchSettingsTab("preferences");
}

async function loadGmailSection() {
    const statusEl = document.getElementById("gmail-status");
    if (!statusEl) return;
    try {
        const res = await fetch(`${API_BASE}/api/gmail/status`);
        if (!res.ok) { statusEl.innerText = "Gmail status unavailable."; return; }
        const s = await res.json();
        statusEl.innerText = s.connected
            ? `✅ Connected${s.email ? " as " + s.email : ""}.`
            : (s.client_id_set && s.client_secret_set ? "Client saved — not connected." : "Not configured.");
    } catch (e) {
        statusEl.innerText = "Gmail status unavailable.";
    }
}

async function saveGmailCredentials() {
    const clientId = document.getElementById("gmail-client-id").value.trim();
    const clientSecret = document.getElementById("gmail-client-secret").value.trim();
    if (!clientId && !clientSecret) { showToast("Enter a client ID or secret first."); return; }
    try {
        const res = await fetch(`${API_BASE}/api/gmail/credentials`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ client_id: clientId, client_secret: clientSecret })
        });
        if (!res.ok) throw new Error((await res.json()).detail || "Failed to save client");
        showToast("🔐 Gmail OAuth client saved.");
        document.getElementById("gmail-client-secret").value = "";
        loadGmailSection();
    } catch (e) {
        showToast("⚠️ " + e.message);
    }
}

async function connectGmail() {
    try {
        const res = await fetch(`${API_BASE}/api/gmail/connect`);
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "Connect failed");
        window.open(data.auth_url, "_blank");
        showToast("Approve access in the Google tab, then reopen Settings to refresh status.");
    } catch (e) {
        showToast("⚠️ " + e.message);
    }
}

async function disconnectGmail() {
    try {
        await fetch(`${API_BASE}/api/gmail/disconnect`, { method: "POST" });
        showToast("Gmail disconnected.");
        loadGmailSection();
    } catch (e) {
        showToast("⚠️ " + e.message);
    }
}

async function saveSettingsModal() {
    // Discovery preferences are edited via the rich chips and auto-persisted; here we
    // persist features + BYOK settings and re-commit the current preferences.
    const features = {};
    ALL_FEATURE_KEYS.forEach(f => {
        const el = document.getElementById(`set-feat-${f}`);
        if (el) features[f] = el.checked;
    });
    // A consent-required feature cannot be saved as enabled until it has been acknowledged.
    const unconsented = consentRequiredKeys.find(k => features[k] && !hasConsentFor(k));
    if (unconsented) {
        openConsentModal(unconsented, true);
        return;
    }
    const exclEl = document.getElementById("set-excluded-companies");
    const excluded = exclEl ? exclEl.value : undefined;
    await persistPreferencesToServer(features, excluded);
    applyFeatureFlags(features);

    const body = { allow_cloud_fallback: document.getElementById("set-allow-cloud").checked };
    const oa = document.getElementById("set-openai-key").value.trim();
    const ge = document.getElementById("set-gemini-key").value.trim();
    const an = document.getElementById("set-anthropic-key").value.trim();
    if (oa) body.openai_api_key = oa;
    if (ge) body.gemini_api_key = ge;
    if (an) body.anthropic_api_key = an;
    try {
        await fetch(`${API_BASE}/api/settings/llm`, {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body)
        });
    } catch (e) { /* ignore */ }

    initTargetPreferences();
    showToast("⚙️ Settings saved.");
}

async function saveAndScanTargetedJobs() {
    const prefs = getTargetPreferences();
    localStorage.setItem("job_agent_target_roles", JSON.stringify(state.targetRoles));
    localStorage.setItem("job_agent_target_cities", JSON.stringify(state.targetCities));
    localStorage.setItem("job_agent_target_country", state.targetCountry);
    localStorage.setItem("job_agent_target_workmode", state.targetWorkMode);
    persistPreferencesToServer();
    
    const roleStr = state.targetRoles.length ? state.targetRoles.join(", ") : "All Roles";
    const cityStr = state.targetCities.length ? state.targetCities.join(", ") : (state.targetCountry || "Global");
    const wmStr = state.targetWorkMode || "Any Mode";

    showToast(`🎯 Scanning career boards for: "${roleStr}" | 🏙️ "${cityStr}" | 💼 "${wmStr}"...`);
    await triggerScrape();
}

async function pruneNonMatchingJobs() {
    const prefs = getTargetPreferences();
    const roleStr = state.targetRoles.length ? state.targetRoles.join(", ") : "All Roles";
    const cityStr = state.targetCities.length ? state.targetCities.join(", ") : (state.targetCountry || "Global");
    const wmStr = state.targetWorkMode || "Any Mode";

    if (!confirm(`Prune unapplied jobs that do not match:\n• Roles: "${roleStr}"\n• Cities/Location: "${cityStr}"\n• Work Mode: "${wmStr}"\n\nOnly 'To Apply' jobs are removed. Shortlisted, Applied, Screening, Interview and Offered jobs are preserved.`)) {
        return;
    }

    showToast("🧹 Pruning non-matching job listings...");
    try {
        const res = await fetch(`${API_BASE}/api/jobs/prune`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(prefs)
        });
        if (!res.ok) throw new Error("Prune failed");
        const data = await res.json();
        showToast(data.message);
        await fetchJobs();
    } catch (e) {
        alert("Failed to prune jobs: " + e.message);
    }
}

async function triggerScrape() {
    const btn = document.getElementById("trigger-scrape-btn");
    const stopBtn = document.getElementById("btn-stop-scrape");
    
    scrapeAbortController = new AbortController();
    if (btn) {
        btn.disabled = true;
        btn.innerHTML = `<span class="spinner" style="width:14px;height:14px;display:inline-block;"></span> Scanning...`;
    }
    if (stopBtn) stopBtn.classList.remove("hidden");

    const prefs = getTargetPreferences();

    startFastQueuePolling();
    try {
        const res = await fetch(`${API_BASE}/api/jobs/scrape`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(prefs),
            signal: scrapeAbortController.signal
        });
        if (!res.ok) {
            const errData = await res.json().catch(() => ({}));
            throw new Error(errData.detail || `Server returned status ${res.status}`);
        }
        const data = await res.json();
        showToast(data.message || "Discovery scan completed.");
        await fetchJobs();
        await fetchCompanies();
    } catch (e) {
        if (e.name === "AbortError") {
            showToast("Job scan stopped by user.");
        } else {
            showToast(`Scan Notice: ${e.message || "Scan interrupted."}`);
        }
    } finally {
        stopFastQueuePolling();
        scrapeAbortController = null;
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = `<span class="btn-icon">⚡</span> Scan & Analyze Jobs`;
        }
        if (stopBtn) stopBtn.classList.add("hidden");
    }
}

async function stopScrape() {
    const stopBtn = document.getElementById("btn-stop-scrape");
    if (stopBtn) {
        stopBtn.disabled = true;
        stopBtn.innerText = "Stopping...";
    }
    
    if (scrapeAbortController) {
        scrapeAbortController.abort();
    }

    try {
        await fetch(`${API_BASE}/api/jobs/scrape/stop`, { method: "POST" });
        showToast("Job scanning process stopped.");
        fetchJobs();
        fetchCompanies();
    } catch (e) {
        console.error("Failed to signal stop scrape", e);
    } finally {
        stopFastQueuePolling();
        if (stopBtn) {
            stopBtn.disabled = false;
            stopBtn.innerText = "🛑 Stop Scan";
            stopBtn.classList.add("hidden");
        }
    }
}

async function triggerLinkedInSync() {
    const btn = document.getElementById("trigger-linkedin-sync-btn");
    const stopBtn = document.getElementById("btn-stop-linkedin-sync");

    linkedinSyncAbortController = new AbortController();
    btn.disabled = true;
    btn.innerHTML = `<span class="spinner" style="width:14px;height:14px;display:inline-block;"></span> Syncing LinkedIn...`;
    if (stopBtn) stopBtn.classList.remove("hidden");

    startFastQueuePolling();
    try {
        showToast("🔗 Syncing LinkedIn live alerts & saved jobs...");
        const res = await fetch(`${API_BASE}/api/linkedin/sync`, {
            method: "POST",
            signal: linkedinSyncAbortController.signal
        });
        const data = await res.json();
        if (!res.ok) {
            throw new Error(data.detail || data.message || "Failed to sync LinkedIn");
        }
        showToast(data.message);
        await fetchJobs();
        await fetchCompanies();
    } catch (e) {
        if (e.name === "AbortError") {
            showToast("LinkedIn sync cancelled by user.");
        } else {
            showToast("⚠️ " + e.message);
            alert("LinkedIn sync notice:\n" + e.message);
        }
    } finally {
        stopFastQueuePolling();
        linkedinSyncAbortController = null;
        btn.disabled = false;
        btn.innerHTML = `<span class="btn-icon">🔗</span> Sync LinkedIn (Alerts & Saved)`;
        if (stopBtn) stopBtn.classList.add("hidden");
    }
}

async function stopLinkedInSync() {
    const stopBtn = document.getElementById("btn-stop-linkedin-sync");
    if (stopBtn) {
        stopBtn.disabled = true;
        stopBtn.innerText = "Stopping...";
    }

    if (linkedinSyncAbortController) {
        linkedinSyncAbortController.abort();
    }

    try {
        await fetch(`${API_BASE}/api/linkedin/sync/stop`, { method: "POST" });
        showToast("LinkedIn sync process stopped.");
    } catch (e) {
        console.error("Failed to signal stop linkedin sync", e);
    } finally {
        stopFastQueuePolling();
        if (stopBtn) {
            stopBtn.disabled = false;
            stopBtn.innerText = "🛑 Stop Sync";
            stopBtn.classList.add("hidden");
        }
    }
}

async function triggerExternalSync() {
    const btn = document.getElementById("trigger-external-sync-btn");
    if (btn) {
        btn.disabled = true;
        btn.innerHTML = `<span class="spinner" style="width:14px;height:14px;display:inline-block;"></span> Syncing Portals...`;
    }

    try {
        // Only request portals the user has explicitly enabled (per-portal consent).
        const sources = [];
        if (featureFlags.ats_hirist) sources.push("hirist");
        if (featureFlags.ats_greenhouse) sources.push("greenhouse");
        if (featureFlags.ats_lever) sources.push("lever");
        if (featureFlags.ats_smartrecruiters) sources.push("smartrecruiters");
        if (featureFlags.gmail_sync) sources.push("gmail");
        if (sources.length === 0) {
            showToast("No application-status sources enabled. Enable them in Settings → Discovery Features.");
            return;
        }
        showToast(`🔄 Syncing candidate applications from ${sources.join(" & ")}...`);
        const res = await fetch(`${API_BASE}/api/applications/sync-external`, {
            method: "POST",
            headers: {
                "Content-Type": "application/json"
            },
            body: JSON.stringify({
                sources,
                max_pages: 10,
                headless: true
            })
        });
        const data = await res.json();
        if (!res.ok) {
            throw new Error(data.detail || data.message || "Failed to sync external applications");
        }
        showToast(data.message || `Synced: ${data.hirist_updated + data.status_updates} updates, ${data.hirist_ingested} new applications.`);
        await fetchJobs();
        await fetchCompanies();
    } catch (e) {
        showToast("⚠️ " + e.message);
        console.error("External sync error:", e);
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = `<span class="btn-icon">🔄</span> Sync Application Statuses`;
        }
    }
}

async function triggerSyncAll() {
    const btn = document.getElementById("trigger-sync-all-btn");
    if (btn) {
        btn.disabled = true;
        btn.innerHTML = `<span class="spinner" style="width:14px;height:14px;display:inline-block;"></span> Queueing Sync All...`;
    }
    try {
        const res = await fetch(`${API_BASE}/api/tasks/sync/all`, { method: "POST" });
        const data = await res.json();
        if (!res.ok) {
            throw new Error(data.detail || data.message || `Server returned ${res.status}`);
        }
        showToast(`🔁 ${data.message || "Sync All started in background."} Track it in Background Jobs & Queue.`);
        fetchTasksStatus(true);
    } catch (e) {
        showToast(`⚠️ Could not start Sync All: ${e.message}`, 5000);
    } finally {
        if (btn) {
            btn.innerHTML = `<span class="btn-icon">🔁</span> Sync All (<span id="sync-all-enabled-count">0</span> enabled)`;
            btn.disabled = false;
            applyFeatureFlags(featureFlags);
        }
    }
}

let isApplyingJob = false;
async function launchPlaywrightApply(jobId) {
    if (isApplyingJob) {
        showToast("⏳ Opening application session, please wait...");
        return;
    }
    isApplyingJob = true;
    startFastQueuePolling();
    try {
        showToast("🚀 Launching Chrome assisted application window...");
        const res = await fetch(`${API_BASE}/api/jobs/${jobId}/apply`, { method: "POST" });
        const data = await res.json();
        if (res.ok) {
            showToast(data.message || "Assisted application window opened and marked as 'Applied'!");
            await fetchJobs();
        } else {
            showToast(`⚠️ ${data.detail || data.message || "Could not launch application window"}`);
        }
    } catch (e) {
        showToast("⚠️ Could not connect to agent backend.");
    } finally {
        stopFastQueuePolling();
        setTimeout(() => { isApplyingJob = false; }, 2500);
    }
}



// -------------------------------------------------------------
// Utilities
// -------------------------------------------------------------
function copyToClipboard(text, successMsg) {
    navigator.clipboard.writeText(text).then(() => {
        showToast(successMsg);
    });
}

function showToast(message) {
    const toast = document.getElementById("toast");
    toast.innerText = message;
    toast.classList.remove("hidden");
    setTimeout(() => {
        toast.classList.add("hidden");
    }, 4000);
}

function escapeHTML(str) {
    if (!str) return "";
    return String(str)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#039;");
}
const escapeHtml = escapeHTML;

// -------------------------------------------------------------
// Timezone-aware timestamp formatting
// -------------------------------------------------------------
// The backend stores naive UTC datetimes, so their ISO strings carry no offset.
// Append 'Z' when missing so the browser anchors them to UTC before converting
// to the user's chosen timezone instead of misreading them as local time.
function parseUtcTimestamp(iso) {
    if (!iso) return null;
    const s = String(iso);
    const hasTz = /[zZ]$|[+-]\d{2}:?\d{2}$/.test(s);
    const d = new Date(hasTz ? s : `${s}Z`);
    return isNaN(d.getTime()) ? null : d;
}

function formatDateTime(iso) {
    const d = parseUtcTimestamp(iso);
    if (!d) return "—";
    return new Intl.DateTimeFormat(undefined, {
        timeZone: state.userTimezone || undefined,
        year: "numeric", month: "2-digit", day: "2-digit",
        hour: "2-digit", minute: "2-digit", second: "2-digit",
        hour12: false
    }).format(d);
}

function formatDate(iso) {
    const d = parseUtcTimestamp(iso);
    if (!d) return "—";
    return new Intl.DateTimeFormat(undefined, {
        timeZone: state.userTimezone || undefined,
        year: "numeric", month: "2-digit", day: "2-digit"
    }).format(d);
}

function formatTime(iso) {
    const d = parseUtcTimestamp(iso);
    if (!d) return "—";
    return new Intl.DateTimeFormat(undefined, {
        timeZone: state.userTimezone || undefined,
        hour: "2-digit", minute: "2-digit", hour12: false
    }).format(d);
}

// Expose handlers globally to window
// -------------------------------------------------------------
// Browser Authentication & Persistent Profile Launcher
// -------------------------------------------------------------
function openBrowserAuthModal() {
    const modal = document.getElementById("browser-auth-modal");
    if (modal) modal.classList.remove("hidden");
}

function closeBrowserAuthModal() {
    const modal = document.getElementById("browser-auth-modal");
    if (modal) modal.classList.add("hidden");
}

async function launchBrowserAuth(url) {
    showToast("🚀 Opening interactive browser for login...");
    try {
        const res = await fetch(`${API_BASE}/api/browser/auth-session`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ url: url || "https://www.linkedin.com/login" })
        });
        const data = await res.json();
        showToast(data.message || "Interactive browser session ready.");
        closeBrowserAuthModal();
    } catch (e) {
        showToast("⚠️ Failed to launch browser authentication session: " + e.message);
    }
}

function launchCustomBrowserAuth() {
    const input = document.getElementById("custom-auth-url");
    const val = input ? input.value.trim() : "";
    if (!val) {
        alert("Please enter a valid URL (e.g. https://www.linkedin.com/login)");
        return;
    }
    launchBrowserAuth(val);
}

// -------------------------------------------------------------
// Operations Log & Persistent Audit History
// -------------------------------------------------------------
let allOperationLogs = [];
let currentOpFilter = 'all';

async function openOperationsModal() {
    const modal = document.getElementById("operations-log-modal");
    if (modal) modal.classList.remove("hidden");
    await fetchOperationLogs();
}

function closeOperationsModal() {
    const modal = document.getElementById("operations-log-modal");
    if (modal) modal.classList.add("hidden");
}

async function fetchOperationLogs(showToastMsg = false) {
    const loading = document.getElementById("operations-loading");
    const emptyState = document.getElementById("operations-empty");
    const listContainer = document.getElementById("operations-log-list");

    if (loading) loading.classList.remove("hidden");
    if (emptyState) emptyState.classList.add("hidden");

    try {
        const res = await fetch(`${API_BASE}/api/operations/logs?limit=100`);
        if (!res.ok) throw new Error("Failed to load logs");
        allOperationLogs = await res.json();
        
        updateOperationCounts();
        renderOperationLogs();

        if (showToastMsg) {
            showToast(`Refreshed ${allOperationLogs.length} operation log record(s).`);
        }
    } catch (e) {
        showToast("⚠️ Could not load operation logs: " + e.message);
    } finally {
        if (loading) loading.classList.add("hidden");
    }
}

function updateOperationCounts() {
    const allCount = allOperationLogs.length;
    const liCount = allOperationLogs.filter(l => l.operation_type === 'linkedin_sync').length;
    const atsCount = allOperationLogs.filter(l => l.operation_type === 'ats_scrape').length;
    const applyCount = allOperationLogs.filter(l => l.operation_type && l.operation_type.includes('apply')).length;

    const elAll = document.getElementById("op-count-all");
    const elLi = document.getElementById("op-count-li");
    const elAts = document.getElementById("op-count-ats");
    const elApply = document.getElementById("op-count-apply");

    if (elAll) elAll.innerText = allCount;
    if (elLi) elLi.innerText = liCount;
    if (elAts) elAts.innerText = atsCount;
    if (elApply) elApply.innerText = applyCount;
}

function filterOperationLogs(filterType) {
    currentOpFilter = filterType;
    document.querySelectorAll(".filter-tab-btn[data-op-filter]").forEach(btn => {
        btn.classList.toggle("active", btn.getAttribute("data-op-filter") === filterType);
    });
    renderOperationLogs();
}

function renderOperationLogs() {
    const listContainer = document.getElementById("operations-log-list");
    const emptyState = document.getElementById("operations-empty");
    if (!listContainer) return;

    listContainer.innerHTML = "";

    let filtered = allOperationLogs;
    if (currentOpFilter === 'linkedin_sync') {
        filtered = allOperationLogs.filter(l => l.operation_type === 'linkedin_sync');
    } else if (currentOpFilter === 'ats_scrape') {
        filtered = allOperationLogs.filter(l => l.operation_type === 'ats_scrape');
    } else if (currentOpFilter === 'assisted_apply') {
        filtered = allOperationLogs.filter(l => l.operation_type && l.operation_type.includes('apply'));
    }

    if (!filtered || filtered.length === 0) {
        if (emptyState) emptyState.classList.remove("hidden");
        return;
    }
    if (emptyState) emptyState.classList.add("hidden");

    filtered.forEach(log => {
        const card = document.createElement("div");
        card.className = "op-log-card";

        const badgeClass = `type-${log.operation_type}`;
        let badgeLabel = log.operation_type.replace(/_/g, ' ').toUpperCase();
        if (log.operation_type === 'linkedin_sync') badgeLabel = "LINKEDIN SYNC";
        else if (log.operation_type === 'gmail_sync') badgeLabel = "GMAIL SYNC";
        else if (log.operation_type === 'google_jobs_scrape') badgeLabel = "GOOGLE JOBS";
        else if (log.operation_type === 'ats_scrape') badgeLabel = "ATS DISCOVERY";
        else if (log.operation_type === 'assisted_apply') badgeLabel = "ASSISTED APPLY";
        else if (log.operation_type === 'bulk_apply') badgeLabel = "BULK APPLY";

        const dateStr = formatDateTime(log.created_at);

        let detailsHtml = "";
        if (log.details_json) {
            try {
                const parsed = JSON.parse(log.details_json);
                detailsHtml = `
                    <div id="op-details-${log.id}" class="op-log-details-content hidden">
                        ${escapeHTML(JSON.stringify(parsed, null, 2))}
                    </div>
                `;
            } catch (e) {
                detailsHtml = `<div id="op-details-${log.id}" class="op-log-details-content hidden">${escapeHTML(log.details_json)}</div>`;
            }
        }

        card.innerHTML = `
            <div class="op-log-header">
                <span class="op-log-badge ${badgeClass}">${badgeLabel}</span>
                <span class="op-log-time">${dateStr}</span>
            </div>
            <div class="op-log-summary">${escapeHTML(log.summary)}</div>
            <div class="op-log-meta">
                <span>Jobs Processed: <strong>${log.jobs_count || 0}</strong></span>
                ${((log.prompt_tokens || 0) + (log.completion_tokens || 0) > 0) ? `<span>Tokens: <strong>${((log.prompt_tokens || 0) + (log.completion_tokens || 0)).toLocaleString()}</strong> (${(log.prompt_tokens || 0).toLocaleString()} in / ${(log.completion_tokens || 0).toLocaleString()} out)</span>` : ''}
                ${log.details_json ? `<button class="op-log-details-toggle" onclick="toggleOpLogDetails(${log.id})">Toggle Breakdown</button>` : ''}
            </div>
            ${detailsHtml}
        `;
        listContainer.appendChild(card);
    });
}

function toggleOpLogDetails(id) {
    const el = document.getElementById(`op-details-${id}`);
    if (el) {
        el.classList.toggle("hidden");
    }
}

async function clearOperationHistory() {
    if (!confirm("Are you sure you want to clear all operation history records?")) return;
    try {
        const res = await fetch(`${API_BASE}/api/operations/logs`, { method: "DELETE" });
        const data = await res.json();
        showToast(data.message || "Operation logs cleared.");
        await fetchOperationLogs();
    } catch (e) {
        showToast("⚠️ Failed to clear operation logs: " + e.message);
    }
}

window.openOperationsModal = openOperationsModal;
window.closeOperationsModal = closeOperationsModal;
window.fetchOperationLogs = fetchOperationLogs;
window.filterOperationLogs = filterOperationLogs;
window.clearOperationHistory = clearOperationHistory;
window.toggleOpLogDetails = toggleOpLogDetails;
window.openTokenAnalyticsModal = openTokenAnalyticsModal;
window.closeTokenAnalyticsModal = closeTokenAnalyticsModal;
window.resetTokenCounter = resetTokenCounter;
window.fetchTokenUsage = fetchTokenUsage;
window.openBrowserAuthModal = openBrowserAuthModal;
window.closeBrowserAuthModal = closeBrowserAuthModal;
window.launchBrowserAuth = launchBrowserAuth;
window.launchCustomBrowserAuth = launchCustomBrowserAuth;
window.addRoleKeyword = addRoleKeyword;
window.removeRoleKeyword = removeRoleKeyword;
window.addCityKeyword = addCityKeyword;
window.removeCityKeyword = removeCityKeyword;
window.handleAddRoleFromInput = handleAddRoleFromInput;
// -------------------------------------------------------------
// Application Q&A Memory Bank (Semantic RAG) & Learned Ignore Rules
// -------------------------------------------------------------
let qaMemoryItems = [];
let qaSearchTimer = null;

async function fetchQAMemory() {
    try {
        const res = await fetch(`${API_BASE}/api/memory/qa`);
        if (res.ok) {
            qaMemoryItems = await res.json();
            renderQAMemory(qaMemoryItems);
            const badge = document.getElementById("badge-qa-count");
            if (badge) badge.innerText = `${qaMemoryItems.length} Answers Saved`;
        }
    } catch (e) {
        console.error("Failed to fetch Q&A memory", e);
    }
}

function renderQAMemory(items) {
    const container = document.getElementById("qa-memory-list");
    if (!container) return;

    if (!items || items.length === 0) {
        container.innerHTML = `
            <div style="text-align:center; padding: 20px; color: var(--text-dim); font-size: 13px;">
                No saved Q&A memory items yet. Add questions like "Work Authorization", "Why this company", or "Major outage experience" to enable 1-click semantic retrieval.
            </div>
        `;
        return;
    }

    container.innerHTML = items.map(item => {
        const catBadgeColors = {
            technical: "background:rgba(99,102,241,0.15); color:#818cf8; border:1px solid rgba(99,102,241,0.4);",
            behavioral: "background:rgba(168,85,247,0.15); color:#c084fc; border:1px solid rgba(168,85,247,0.4);",
            experience: "background:rgba(59,130,246,0.15); color:#60a5fa; border:1px solid rgba(59,130,246,0.4);",
            salary: "background:rgba(34,197,94,0.15); color:#4ade80; border:1px solid rgba(34,197,94,0.4);",
            logistics: "background:rgba(234,179,8,0.15); color:#facc15; border:1px solid rgba(234,179,8,0.4);"
        };
        const catStyle = catBadgeColors[item.category] || "background:rgba(255,255,255,0.08); color:var(--text-main); border:1px solid var(--border-color);";
        const simBadge = item.similarity !== undefined ? `
            <span class="badge" style="background:rgba(16,185,129,0.2); color:#34d399; border:1px solid rgba(16,185,129,0.4);">
                ⚡ ${Math.round(item.similarity * 100)}% Semantic Match
            </span>
        ` : '';

        return `
            <div style="background: rgba(255,255,255,0.02); border: 1px solid var(--border-color); border-radius: 10px; padding: 12px 14px; display: flex; flex-direction: column; gap: 6px;">
                <div style="display: flex; justify-content: space-between; align-items: flex-start; gap: 8px;">
                    <div style="display: flex; align-items: center; gap: 8px; flex-wrap: wrap;">
                        <span class="badge" style="${catStyle}; font-size: 11px; text-transform: capitalize;">${escapeHTML(item.category || 'general')}</span>
                        <strong style="font-size: 13px; color: var(--text-main);">${escapeHTML(item.question_text || item.question)}</strong>
                        ${simBadge}
                    </div>
                    <div style="display: flex; gap: 6px;">
                        <button type="button" class="btn btn-sm btn-outline" style="padding: 2px 8px; font-size: 11px;" onclick="copyToClipboard('${escapeHTML((item.answer_text || item.answer || '').replace(/'/g, "\\'"))}')">📋 Copy</button>
                        <button type="button" class="btn btn-sm btn-outline text-danger" style="padding: 2px 8px; font-size: 11px;" onclick="deleteQAMemoryItem(${item.id})">✕</button>
                    </div>
                </div>
                <div style="background: rgba(0,0,0,0.2); border-radius: 6px; padding: 8px 10px; font-size: 12px; color: var(--text-dim); line-height: 1.5; white-space: pre-wrap;">${escapeHTML(item.answer_text || item.answer)}</div>
            </div>
        `;
    }).join("");
}

function toggleAddQAModal() {
    const form = document.getElementById("qa-add-form");
    if (form) form.classList.toggle("hidden");
}

async function submitNewQAMemory() {
    const qEl = document.getElementById("qa-new-question");
    const aEl = document.getElementById("qa-new-answer");
    const cEl = document.getElementById("qa-new-category");

    const question_text = qEl ? qEl.value.trim() : "";
    const answer_text = aEl ? aEl.value.trim() : "";
    const category = cEl ? cEl.value : "general";

    if (!question_text || !answer_text) {
        showToast("Please provide both a question and an answer.");
        return;
    }

    try {
        const res = await fetch(`${API_BASE}/api/memory/qa`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ question_text, answer_text, category })
        });
        if (res.ok) {
            showToast("Saved to Q&A Memory Bank.");
            if (qEl) qEl.value = "";
            if (aEl) aEl.value = "";
            toggleAddQAModal();
            await fetchQAMemory();
        } else {
            showToast("Failed to save Q&A memory.");
        }
    } catch (e) {
        console.error("Error saving Q&A item", e);
    }
}

async function deleteQAMemoryItem(qaId) {
    if (!confirm("Remove this question & answer from your memory bank?")) return;
    try {
        const res = await fetch(`${API_BASE}/api/memory/qa/${qaId}`, { method: "DELETE" });
        if (res.ok) {
            showToast("Removed from memory.");
            await fetchQAMemory();
        }
    } catch (e) {
        console.error("Error deleting Q&A item", e);
    }
}

function debounceSearchQAMemory(query) {
    clearTimeout(qaSearchTimer);
    if (!query || !query.trim()) {
        renderQAMemory(qaMemoryItems);
        return;
    }
    qaSearchTimer = setTimeout(async () => {
        try {
            const res = await fetch(`${API_BASE}/api/memory/qa/search`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ query_question: query.trim(), top_k: 5, min_similarity: 0.50 })
            });
            if (res.ok) {
                const data = await res.json();
                renderQAMemory(data.matches || []);
            }
        } catch (e) {
            console.error("Semantic search failed", e);
        }
    }, 250);
}

async function fetchDismissedPatterns() {
    try {
        const res = await fetch(`${API_BASE}/api/memory/dismissed-patterns`);
        if (res.ok) {
            const patterns = await res.json();
            renderDismissedPatterns(patterns);
            const badge = document.getElementById("badge-dismissed-count");
            if (badge) badge.innerText = `${patterns.length} Learned Rules`;
        }
    } catch (e) {
        console.error("Failed to fetch dismissed patterns", e);
    }
}

function renderDismissedPatterns(patterns) {
    const container = document.getElementById("dismissed-patterns-list");
    if (!container) return;

    if (!patterns || patterns.length === 0) {
        container.innerHTML = `
            <div style="padding: 10px 0; color: var(--text-dim); font-size: 13px;">
                No ignore rules learned yet. Marking any unwanted role as 'Not Interested' will automatically memorize it here to save tokens on future scans.
            </div>
        `;
        return;
    }

    container.innerHTML = patterns.map(p => `
        <div style="background: rgba(239,68,68,0.1); border: 1px solid rgba(239,68,68,0.3); border-radius: 20px; padding: 4px 10px; display: inline-flex; align-items: center; gap: 8px; font-size: 12px; color: #fca5a5;">
            <span>🚫 ${p.company_name ? escapeHTML(p.company_name) + " — " : ""}${escapeHTML(p.title)}</span>
            <button type="button" style="background:none; border:none; color:#f87171; cursor:pointer; font-size:11px; padding:0;" onclick="deleteDismissedPattern(${p.id})">✕</button>
        </div>
    `).join("");
}

async function deleteDismissedPattern(patternId) {
    try {
        const res = await fetch(`${API_BASE}/api/memory/dismissed-patterns/${patternId}`, { method: "DELETE" });
        if (res.ok) {
            showToast("Ignore rule removed.");
            await fetchDismissedPatterns();
        }
    } catch (e) {
        console.error("Error deleting dismissed pattern", e);
    }
}

window.fetchQAMemory = fetchQAMemory;
window.renderQAMemory = renderQAMemory;
window.toggleAddQAModal = toggleAddQAModal;
window.submitNewQAMemory = submitNewQAMemory;
window.deleteQAMemoryItem = deleteQAMemoryItem;
window.debounceSearchQAMemory = debounceSearchQAMemory;
window.fetchDismissedPatterns = fetchDismissedPatterns;
window.renderDismissedPatterns = renderDismissedPatterns;
window.deleteDismissedPattern = deleteDismissedPattern;
window.handleAddRoleFromOnboardingInput = handleAddRoleFromOnboardingInput;
window.handleAddCityFromInput = handleAddCityFromInput;
window.handleAddCityFromOnboardingInput = handleAddCityFromOnboardingInput;
window.handleTargetPrefChange = handleTargetPrefChange;
window.handleCountrySelectChange = handleCountrySelectChange;
window.saveAndScanTargetedJobs = saveAndScanTargetedJobs;
window.pruneNonMatchingJobs = pruneNonMatchingJobs;
window.cleanSystemFeedCache = cleanSystemFeedCache;
window.bulkTailorSelectedJobs = bulkTailorSelectedJobs;
window.tailorCurrentModalJob = tailorCurrentModalJob;
window.bulkApplySelectedJobs = bulkApplySelectedJobs;
window.proceedToOnboardingStep2 = proceedToOnboardingStep2;
window.proceedToOnboardingStep3 = proceedToOnboardingStep3;
window.goToOnboardingStep = goToOnboardingStep;
window.finishOnboardingAndScan = finishOnboardingAndScan;
window.addOnboardingSkill = addOnboardingSkill;
window.updateCriteriaSummaryBar = updateCriteriaSummaryBar;
window.seedCompanyCatalog = seedCompanyCatalog;
window.preseedAllCompanies = preseedAllCompanies;
window.quickAddCompany = quickAddCompany;
window.scrapeCompanyJobs = scrapeCompanyJobs;
window.scanAndOpenCompanyRadar = scanAndOpenCompanyRadar;
window.openEditCompanyModal = openEditCompanyModal;
window.closeEditCompanyModal = closeEditCompanyModal;
window.saveCompanyEdit = saveCompanyEdit;
window.refreshCompanyIntel = refreshCompanyIntel;
window.deleteCompany = deleteCompany;
window.handleCompanyFileImport = handleCompanyFileImport;
window.handleAddCompany = handleAddCompany;
window.stopLinkedInSync = stopLinkedInSync;
window.triggerExternalSync = triggerExternalSync;
window.stopScrape = stopScrape;
window.toggleJobSelection = toggleJobSelection;
window.clearBatchSelection = clearBatchSelection;
window.openJobDetails = openJobDetails;
window.updateJobStatus = updateJobStatus;
window.deleteJob = deleteJob;
window.launchPlaywrightApply = launchPlaywrightApply;
window.setFeedTab = setFeedTab;
window.toggleShortlistJob = toggleShortlistJob;
window.dismissJob = dismissJob;
window.restoreJob = restoreJob;
window.bulkSetStatus = bulkSetStatus;
window.removeResumeSkill = removeResumeSkill;
window.addResumeSkill = addResumeSkill;
window.copyToClipboard = copyToClipboard;
window.showToast = showToast;
window.escapeHTML = escapeHTML;
window.escapeHtml = escapeHTML;

// ==========================================================================
// AI Career Copilot Controller
// ==========================================================================

let copilotHistory = [];
let copilotIsOpen = false;
let copilotFocusedJobId = null;
let copilotFocusedJobTitle = null;

function loadCopilotHistory() {
    try {
        const saved = localStorage.getItem("job_agent_copilot_history");
        if (saved) {
            copilotHistory = JSON.parse(saved);
        }
    } catch (e) {
        copilotHistory = [];
    }
    if (!copilotHistory || copilotHistory.length === 0) {
        copilotHistory = [
            {
                role: "assistant",
                content: "👋 Hi! I am your **AI Career Copilot**. I have live access to your active resume, monitored companies, and job pipeline.\n\nAsk me to **search opportunities**, **tailor application materials**, **run mock interview prep**, or **manage your pipeline**!"
            }
        ];
    }
}

function saveCopilotHistory() {
    try {
        localStorage.setItem("job_agent_copilot_history", JSON.stringify(copilotHistory.slice(-25)));
    } catch (e) {
        console.error("Failed to save copilot history:", e);
    }
}

function toggleCopilotDrawer(focusJobId = null, focusJobTitle = null) {
    const drawer = document.getElementById("copilot-drawer");
    const backdrop = document.getElementById("copilot-backdrop");
    if (!drawer) return;

    if (focusJobId) {
        copilotFocusedJobId = focusJobId;
        copilotFocusedJobTitle = focusJobTitle || `Job #${focusJobId}`;
        const focusBar = document.getElementById("copilot-focus-job-bar");
        const focusTitleEl = document.getElementById("copilot-focus-job-title");
        if (focusBar && focusTitleEl) {
            focusTitleEl.innerText = `Context: ${copilotFocusedJobTitle}`;
            focusBar.classList.remove("hidden");
        }
    }

    copilotIsOpen = !copilotIsOpen;
    if (copilotIsOpen) {
        drawer.classList.remove("hidden");
        if (backdrop) backdrop.classList.remove("hidden");
        renderCopilotMessages();
        const input = document.getElementById("copilot-input");
        if (input) setTimeout(() => input.focus(), 150);
    } else {
        drawer.classList.add("hidden");
        if (backdrop) backdrop.classList.add("hidden");
    }
}

function clearCopilotJobFocus() {
    copilotFocusedJobId = null;
    copilotFocusedJobTitle = null;
    const focusBar = document.getElementById("copilot-focus-job-bar");
    if (focusBar) focusBar.classList.add("hidden");
}

function openCopilotForJob(jobId, jobTitle) {
    toggleCopilotDrawer(jobId, jobTitle);
    const input = document.getElementById("copilot-input");
    if (input) {
        input.value = `How should I tailor my application for ${jobTitle || 'this role'}?`;
        input.focus();
    }
}

function handleCopilotQuickPrompt(text) {
    const input = document.getElementById("copilot-input");
    if (input) {
        input.value = text;
        sendCopilotMessage();
    }
}

function handleCopilotInputKey(e) {
    if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        sendCopilotMessage();
    }
}

function formatCopilotMarkdown(raw) {
    if (!raw) return "";
    let formatted = escapeHTML(raw);
    
    // Headers: ### Header
    formatted = formatted.replace(/^### (.*$)/gim, '<h4 style="color:#f8fafc; margin: 6px 0 2px 0;">$1</h4>');
    formatted = formatted.replace(/^## (.*$)/gim, '<h3 style="color:#f8fafc; margin: 8px 0 3px 0;">$1</h3>');
    formatted = formatted.replace(/^# (.*$)/gim, '<h2 style="color:#f8fafc; margin: 10px 0 4px 0;">$1</h2>');

    // Bold: **text**
    formatted = formatted.replace(/\*\*(.*?)\*\*/gim, '<strong style="color:#f8fafc;">$1</strong>');
    // Italics: *text*
    formatted = formatted.replace(/\*(.*?)\*/gim, '<em>$1</em>');
    // Inline code: `code`
    formatted = formatted.replace(/`([^`]+)`/gim, '<code>$1</code>');

    // Bullet points: - item
    formatted = formatted.replace(/^\s*-\s+(.*$)/gim, '<li>$1</li>');
    formatted = formatted.replace(/(<li>.*<\/li>)/gims, '<ul style="margin: 4px 0 4px 16px; padding: 0;">$1</ul>');

    // Line breaks
    formatted = formatted.replace(/\n\n/g, '<div style="height:6px;"></div>');
    formatted = formatted.replace(/\n/g, '<br>');

    return formatted;
}

function renderCopilotMessages() {
    const container = document.getElementById("copilot-messages-container");
    if (!container) return;

    container.innerHTML = copilotHistory.map((m, idx) => {
        const isUser = m.role === "user";
        let actionsHtml = "";
        if (m.actions_taken && m.actions_taken.length > 0) {
            actionsHtml = m.actions_taken.map(a => {
                if (a.type === "status_update") {
                    return `<div class="copilot-action-badge">✓ Updated status for <b>${escapeHTML(a.data.company)}</b> to <b>${escapeHTML(a.data.new_status)}</b></div>`;
                }
                if (a.type === "material_generation") {
                    return `<div class="copilot-action-badge">⚡ Application package generated for <b>${escapeHTML(a.data.company)}</b></div>`;
                }
                return "";
            }).join("");
        }

        let embeddedJobsHtml = "";
        if (m.embedded_jobs && m.embedded_jobs.length > 0) {
            embeddedJobsHtml = m.embedded_jobs.map(j => `
                <div class="copilot-job-card">
                    <div class="copilot-job-card-header">
                        <div>
                            <div class="copilot-job-card-title">${escapeHTML(j.title)}</div>
                            <div class="copilot-job-card-company">${escapeHTML(j.company)} • ${escapeHTML(j.location || 'Remote')}</div>
                        </div>
                        <span class="badge ${(j.match_score || 0) >= 80 ? 'active' : 'verified'}" style="font-size: 10px;">${j.match_score != null ? Math.round(j.match_score) + '% Match' : 'Not scored'}</span>
                    </div>
                    <div class="copilot-job-card-actions">
                        <button class="copilot-card-btn" onclick="copilotApplyJob(${j.id})">⚡ Apply</button>
                        <button class="copilot-card-btn" onclick="copilotShortlistJob(${j.id})">⭐ Shortlist</button>
                        <button class="copilot-card-btn" onclick="openJobDetailsModal(${j.id})">👁️ Details</button>
                    </div>
                </div>
            `).join("");
        }

        return `
            <div class="chat-bubble ${isUser ? 'user' : 'assistant'}">
                ${actionsHtml}
                <div>${formatCopilotMarkdown(m.content)}</div>
                ${embeddedJobsHtml}
            </div>
        `;
    }).join("");

    container.scrollTop = container.scrollHeight;
}

async function sendCopilotMessage() {
    const input = document.getElementById("copilot-input");
    const sendBtn = document.getElementById("btn-copilot-send");
    if (!input) return;

    const message = input.value.trim();
    if (!message) return;

    // Append User Message
    copilotHistory.push({
        role: "user",
        content: message
    });
    input.value = "";
    renderCopilotMessages();

    // Show Typing Indicator
    const container = document.getElementById("copilot-messages-container");
    const typingIndicator = document.createElement("div");
    typingIndicator.className = "copilot-typing-indicator";
    typingIndicator.id = "copilot-typing";
    typingIndicator.innerHTML = `
        <div class="copilot-typing-dot"></div>
        <div class="copilot-typing-dot"></div>
        <div class="copilot-typing-dot"></div>
    `;
    container.appendChild(typingIndicator);
    container.scrollTop = container.scrollHeight;

    if (sendBtn) sendBtn.disabled = true;

    try {
        const historyPayload = copilotHistory
            .filter(h => h.role === "user" || h.role === "assistant")
            .slice(-8)
            .map(h => ({ role: h.role, content: h.content }));

        const res = await fetch(`${API_BASE}/api/chat/assistant`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                message: message,
                history: historyPayload,
                job_id: copilotFocusedJobId
            })
        });

        if (!res.ok) {
            const errData = await res.json().catch(() => ({}));
            throw new Error(errData.detail || `Server returned ${res.status}`);
        }

        const data = await res.json();

        // Remove typing indicator
        const el = document.getElementById("copilot-typing");
        if (el) el.remove();

        // Append Assistant Reply
        copilotHistory.push({
            role: "assistant",
            content: data.reply || "I am ready to assist with your search.",
            actions_taken: data.actions_taken || [],
            embedded_jobs: data.embedded_jobs || []
        });

        saveCopilotHistory();
        renderCopilotMessages();

        // If actions were taken that alter jobs/status, refresh feed in background
        if (data.actions_taken && data.actions_taken.length > 0) {
            fetchJobs();
            fetchTokenUsage();
            fetchOperationLogs();
        }
    } catch (err) {
        const el = document.getElementById("copilot-typing");
        if (el) el.remove();

        copilotHistory.push({
            role: "assistant",
            content: `⚠️ **Notice**: Could not fetch AI response (${err.message}). Ensure your local AI server is active.`
        });
        renderCopilotMessages();
    } finally {
        if (sendBtn) sendBtn.disabled = false;
        setTimeout(() => {
            const inp = document.getElementById("copilot-input");
            if (inp) inp.focus();
        }, 100);
    }
}

function clearCopilotHistory() {
    if (!confirm("Clear AI Copilot chat history?")) return;
    copilotHistory = [
        {
            role: "assistant",
            content: "✨ Chat history cleared. How can I help you take the next step in your career today?"
        }
    ];
    saveCopilotHistory();
    renderCopilotMessages();
}

async function copilotApplyJob(jobId) {
    showToast(`⚡ Launching assisted application session...`);
    await launchPlaywrightApply(jobId);
    copilotHistory.push({
        role: "assistant",
        content: `🚀 Launched Assisted Application session in Chrome for job #${jobId}. Status updated to **Applied**.`
    });
    saveCopilotHistory();
    renderCopilotMessages();
}

async function copilotShortlistJob(jobId) {
    await updateJobStatus(jobId, "Shortlisted");
    showToast(`⭐ Job #${jobId} moved to Shortlisted.`);
    copilotHistory.push({
        role: "assistant",
        content: `⭐ Job #${jobId} has been successfully added to your **Shortlisted** pipeline.`
    });
    saveCopilotHistory();
    renderCopilotMessages();
}

// Window bindings
window.toggleCopilotDrawer = toggleCopilotDrawer;
window.clearCopilotJobFocus = clearCopilotJobFocus;
window.openCopilotForJob = openCopilotForJob;
window.handleCopilotQuickPrompt = handleCopilotQuickPrompt;
window.handleCopilotInputKey = handleCopilotInputKey;
window.sendCopilotMessage = sendCopilotMessage;
window.clearCopilotHistory = clearCopilotHistory;
window.copilotApplyJob = copilotApplyJob;
window.copilotShortlistJob = copilotShortlistJob;

// ==========================================================================
// Google for Jobs & Gmail Alerts Sync Handlers
// ==========================================================================

async function fetchGoogleJobQueries() {
    try {
        const res = await fetch(`${API_BASE}/api/jobs/google-jobs/queries`);
        if (res.ok) {
            const data = await res.json();
            configuredGoogleQueries = data.queries || [];
            renderGoogleSearchesList();
        }
    } catch (err) {
        console.error("Failed to fetch Google Job queries:", err);
    }
}

function renderGoogleSearchesList() {
    // 1. In Google Discovery Modal
    const modalContainer = document.getElementById("google-searches-list");
    const modalBadge = document.getElementById("google-searches-count-badge");
    if (modalBadge) {
        modalBadge.innerText = `${configuredGoogleQueries.length} ${configuredGoogleQueries.length === 1 ? 'query' : 'queries'}`;
    }
    if (modalContainer) {
        if (configuredGoogleQueries.length === 0) {
            modalContainer.innerHTML = `<span style="color: #6c7086; font-size: 12px; font-style: italic;">No followed queries saved yet. Add one below or sync from Google!</span>`;
        } else {
            modalContainer.innerHTML = configuredGoogleQueries.map((q, idx) => `
                <span class="google-search-chip">
                    <span>${escapeHTML(q)}</span>
                    <button type="button" onclick="deleteGoogleJobQuery(${idx})" class="google-search-chip-remove" title="Remove this search query">&times;</button>
                </span>
            `).join("");
        }
    }

    // 2. In Profile Preferences Tab
    const profileContainer = document.getElementById("profile-google-searches-chips");
    const profileBadge = document.getElementById("badge-profile-google-searches-count");
    if (profileBadge) {
        profileBadge.innerText = `${configuredGoogleQueries.length} ${configuredGoogleQueries.length === 1 ? 'Alert' : 'Alerts'}`;
    }
    if (profileContainer) {
        if (configuredGoogleQueries.length === 0) {
            profileContainer.innerHTML = `<span style="color: #6c7086; font-size: 12px; font-style: italic;">No followed queries saved yet. Add one below or sync from Google!</span>`;
        } else {
            profileContainer.innerHTML = configuredGoogleQueries.map((q, idx) => `
                <span class="google-search-chip">
                    <span>${escapeHTML(q)}</span>
                    <button type="button" onclick="deleteGoogleJobQuery(${idx})" class="google-search-chip-remove" title="Remove this search query">&times;</button>
                </span>
            `).join("");
        }
    }
}

async function handleAddGoogleSearchFromProfile() {
    const input = document.getElementById("add-profile-google-search-input");
    if (!input) return;
    const query = input.value.trim();
    if (!query) {
        showToast("⚠️ Please enter a valid search query string.");
        return;
    }
    if (configuredGoogleQueries.includes(query)) {
        showToast("ℹ️ This search query is already in your list.");
        input.value = "";
        return;
    }
    const updated = [...configuredGoogleQueries, query];
    try {
        const res = await fetch(`${API_BASE}/api/jobs/google-jobs/queries`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ queries: updated })
        });
        if (!res.ok) throw new Error("Failed to save search query");
        const data = await res.json();
        configuredGoogleQueries = data.queries || updated;
        renderGoogleSearchesList();
        input.value = "";
        showToast("✓ Added followed search query.");
    } catch (err) {
        showToast(`⚠️ Could not save query: ${err.message}`);
    }
}

async function addGoogleJobQuery() {
    const input = document.getElementById("google-new-query-input");
    if (!input) return;
    const query = input.value.trim();
    if (!query) {
        showToast("⚠️ Please enter a valid search query string.");
        return;
    }

    if (configuredGoogleQueries.includes(query)) {
        showToast("ℹ️ This search query is already in your list.");
        input.value = "";
        return;
    }

    const updated = [...configuredGoogleQueries, query];
    try {
        const res = await fetch(`${API_BASE}/api/jobs/google-jobs/queries`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ queries: updated })
        });
        if (!res.ok) throw new Error("Failed to save search query");
        const data = await res.json();
        configuredGoogleQueries = data.queries || updated;
        renderGoogleSearchesList();
        input.value = "";
        showToast("✓ Added followed search query.");
    } catch (err) {
        showToast(`⚠️ Could not save query: ${err.message}`);
    }
}

async function deleteGoogleJobQuery(index) {
    if (index < 0 || index >= configuredGoogleQueries.length) return;
    const updated = configuredGoogleQueries.filter((_, i) => i !== index);
    try {
        const res = await fetch(`${API_BASE}/api/jobs/google-jobs/queries`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ queries: updated })
        });
        if (!res.ok) throw new Error("Failed to delete search query");
        const data = await res.json();
        configuredGoogleQueries = data.queries || updated;
        renderGoogleSearchesList();
        showToast("✓ Search query removed.");
    } catch (err) {
        showToast(`⚠️ Could not delete query: ${err.message}`);
    }
}

async function syncGoogleFollowedQueries() {
    const btn = document.getElementById("btn-sync-google-tabs");
    if (btn) {
        btn.disabled = true;
        btn.innerText = "⏳ Syncing...";
    }
    showToast("🔍 Connecting to Google for Jobs 'Following' tab...");

    try {
        const res = await fetch(`${API_BASE}/api/jobs/google-jobs/sync-followed`, {
            method: "POST"
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || data.message || "Failed to sync followed queries");
        
        configuredGoogleQueries = data.queries || [];
        renderGoogleSearchesList();
        showToast(`✓ ${data.message || `Discovered ${data.new_queries_found || 0} new alert search(es)!`}`);
    } catch (err) {
        console.error("Sync followed queries failed:", err);
        showToast(`⚠️ Google sync notice: ${err.message}`, 4000);
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.innerText = "🔄 Sync from Google";
        }
    }
}

function toggleGoogleJobsSingleQueryMode() {
    const check = document.getElementById("google-jobs-scan-all-check");
    const singleGroup = document.getElementById("google-jobs-single-query-group");
    if (!check || !singleGroup) return;

    if (check.checked) {
        singleGroup.classList.add("hidden");
    } else {
        singleGroup.classList.remove("hidden");
        const singleInput = document.getElementById("google-jobs-query-input");
        if (singleInput) singleInput.focus();
    }
}

function openGoogleJobsModal() {
    const modal = document.getElementById("google-jobs-modal");
    if (modal) {
        modal.classList.remove("hidden");
        fetchGoogleJobQueries();
        const check = document.getElementById("google-jobs-scan-all-check");
        if (check) check.checked = true;
        toggleGoogleJobsSingleQueryMode();
    }
}

function closeGoogleJobsModal() {
    const modal = document.getElementById("google-jobs-modal");
    if (modal) modal.classList.add("hidden");
}

async function submitGoogleJobsScrape() {
    const scanAllCheck = document.getElementById("google-jobs-scan-all-check");
    const queryInput = document.getElementById("google-jobs-query-input");
    const locInput = document.getElementById("google-jobs-location-input");
    const browserCheck = document.getElementById("google-jobs-browser-mode");
    const submitBtn = document.getElementById("btn-submit-google-jobs-scrape");

    const scanAll = scanAllCheck ? scanAllCheck.checked : true;
    const singleQuery = queryInput ? queryInput.value.trim() : "";
    const location = locInput ? locInput.value.trim() : "India";
    const useBrowser = browserCheck ? browserCheck.checked : false;

    try {
        const payload = {
            location: location || "India",
            use_playwright: useBrowser
        };

        if (scanAll) {
            payload.queries = configuredGoogleQueries.length > 0 ? configuredGoogleQueries : null;
        } else {
            payload.queries = singleQuery ? [singleQuery] : null;
        }

        const countQueries = payload.queries ? payload.queries.length : (configuredGoogleQueries.length || 6);

        // Dispatch non-blocking background task
        const res = await fetch(`${API_BASE}/api/tasks/discovery/google-jobs`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload)
        });

        const data = await res.json();
        if (!res.ok) {
            throw new Error(data.detail || data.message || `Server returned ${res.status}`);
        }

        // Close modal immediately and show non-blocking toast
        closeGoogleJobsModal();
        showToast(`🚀 Google Jobs scan running in background (${countQueries} queries)... Click '⚡ Background Jobs' to view progress.`);
        
        // Refresh active tasks and pill
        fetchTasksStatus();
    } catch (err) {
        console.error("Google Jobs task dispatch error:", err);
        showToast(`⚠️ Could not start background scan: ${err.message}`, 5000);
    }
}

// -------------------------------------------------------------
// Opportunity Radar (structured hiring-intent feed)
// -------------------------------------------------------------
// Ranks already-ingested, structured opportunities (LinkedIn alerts/recommendations,
// ATS portals, Google Jobs, recruiter outreach). This replaces the fragile LinkedIn
// content-post scraping with a structured-only path; the backend scores hires
// deterministically (no LLM calls).

let radarItems = [];
let radarCompanyFilter = "";

function openOpportunityRadarModal(companyName = "") {
    const modal = document.getElementById("opportunity-radar-modal");
    if (!modal) return;

    modal.classList.remove("hidden");
    radarCompanyFilter = (companyName || "").trim();

    const scopeEl = document.getElementById("radar-scope");
    const resultsList = document.getElementById("radar-results");
    const emptyState = document.getElementById("radar-empty");
    if (scopeEl) scopeEl.value = "active";
    if (resultsList) resultsList.innerHTML = "";
    if (emptyState) emptyState.classList.remove("hidden");

    fetchOpportunityRadar();
}

function closeOpportunityRadarModal() {
    const modal = document.getElementById("opportunity-radar-modal");
    if (modal) modal.classList.add("hidden");
}

async function fetchOpportunityRadar() {
    const minIntentEl = document.getElementById("radar-min-intent");
    const scopeEl = document.getElementById("radar-scope");
    const loading = document.getElementById("radar-loading");
    const emptyState = document.getElementById("radar-empty");
    const resultsList = document.getElementById("radar-results");

    const minIntent = minIntentEl ? parseInt(minIntentEl.value, 10) || 0 : 0;
    const includeApplied = !!(scopeEl && scopeEl.value === "all");
    const respectPrefsEl = document.getElementById("radar-respect-prefs");
    const respectPrefs = respectPrefsEl ? respectPrefsEl.checked : true;

    if (loading) loading.classList.remove("hidden");
    if (emptyState) emptyState.classList.add("hidden");
    if (resultsList) resultsList.innerHTML = "";

    try {
        const params = new URLSearchParams({
            min_intent: String(minIntent),
            include_applied: String(includeApplied),
            respect_preferences: String(respectPrefs),
            limit: "100"
        });
        const res = await fetch(`${API_BASE}/api/opportunities/radar?${params.toString()}`);
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || `Server returned status ${res.status}`);
        radarItems = Array.isArray(data) ? data : [];
        renderOpportunityRadar();
    } catch (err) {
        console.error("Error fetching opportunity radar:", err);
        const emptyText = document.getElementById("radar-empty-text");
        if (emptyState) emptyState.classList.remove("hidden");
        if (emptyText) emptyText.innerText = `Radar notice: ${err.message}`;
    } finally {
        if (loading) loading.classList.add("hidden");
    }
}

function renderOpportunityRadar() {
    const resultsList = document.getElementById("radar-results");
    const emptyState = document.getElementById("radar-empty");
    const emptyText = document.getElementById("radar-empty-text");
    const sourceFilterEl = document.getElementById("radar-source-filter");
    const countEl = document.getElementById("radar-count");
    if (!resultsList) return;

    const sourceFilter = (sourceFilterEl ? sourceFilterEl.value : "").trim().toLowerCase();
    let items = radarItems;
    if (radarCompanyFilter) {
        const cf = radarCompanyFilter.toLowerCase();
        items = items.filter(i => (i.company_name || "").toLowerCase().includes(cf));
    }
    if (sourceFilter) {
        items = items.filter(i => (i.source || "").toLowerCase().includes(sourceFilter));
    }

    if (countEl) countEl.innerText = `${items.length} opportunit${items.length === 1 ? "y" : "ies"}`;

    if (items.length === 0) {
        resultsList.innerHTML = "";
        if (emptyState) emptyState.classList.remove("hidden");
        if (emptyText) {
            emptyText.innerText = radarCompanyFilter
                ? `No ranked opportunities found for ${radarCompanyFilter} in your structured sources.`
                : "No ranked opportunities found. Run a LinkedIn sync or an ATS scan to populate structured sources.";
        }
        return;
    }

    if (emptyState) emptyState.classList.add("hidden");
    resultsList.innerHTML = items.map(item => {
        const reasons = (item.intent_reasons || [])
            .map(r => `<span class="radar-reason">${escapeHTML(r)}</span>`)
            .join("");
        const ghost = item.is_ghost_job ? `<span class="badge badge-warning" style="font-size:10px;">👻 Ghost?</span>` : "";
        const age = item.created_at ? formatDate(item.created_at) : "";
        return `
        <div class="radar-card radar-card-clickable" onclick="openRadarJob(${item.id})" title="Open job details">
            <div class="radar-card-main">
                <div class="radar-card-title-row">
                    <span class="badge badge-primary radar-score">🎯 ${item.intent_score}</span>
                    <strong class="radar-title">${escapeHTML(item.title)}</strong>
                    ${ghost}
                </div>
                <div class="radar-card-meta">
                    🏢 ${escapeHTML(item.company_name || "Unknown")} · 📍 ${escapeHTML(item.location || "Remote / Various")}${age ? ` · 🗓️ ${escapeHTML(age)}` : ""}
                </div>
                <div class="radar-reasons">${reasons}</div>
            </div>
            <div class="radar-card-actions">
                <span class="badge badge-info radar-source">${escapeHTML(item.source || "Unknown")}</span>
                ${item.match_score ? `<span class="radar-match">Match ${Math.round(item.match_score)}%</span>` : ""}
                <div class="radar-action-buttons">
                    <button class="btn btn-sm btn-outline radar-act" onclick="event.stopPropagation(); radarUpdateStatus(${item.id}, 'Shortlisted')" title="Move to Shortlisted">⭐ Shortlist</button>
                    <button class="btn btn-sm btn-outline radar-act" onclick="event.stopPropagation(); radarUpdateStatus(${item.id}, 'Not Interested')" title="Dismiss this opportunity">🚫 Dismiss</button>
                    ${item.url ? `<a class="btn btn-sm btn-outline radar-act" href="${escapeHTML(item.url)}" target="_blank" onclick="event.stopPropagation()" style="font-size:11px;">🔗 Open</a>` : ""}
                </div>
                ${item.has_materials ? `<span class="badge badge-success" style="font-size:10px;">✨ Materials ready</span>` : ""}
            </div>
        </div>`;
    }).join("");
}

// Opens the standard job details modal for a radar row (clicking anywhere on the card).
function openRadarJob(id) {
    const item = radarItems.find(i => i.id === id);
    if (!item) return;
    const idx = state.jobs.findIndex(j => j.id === id);
    if (idx === -1) {
        state.jobs.push(item);
    } else {
        state.jobs[idx] = { ...state.jobs[idx], ...item };
    }
    closeOpportunityRadarModal();
    openJobDetailsModal(id);
}

// Quick status actions from a radar row, then keep the list in sync.
async function radarUpdateStatus(id, newStatus) {
    await updateJobStatus(id, newStatus);
    if (newStatus === "Not Interested" || newStatus === "Rejected") {
        radarItems = radarItems.filter(i => i.id !== id);
    }
    renderOpportunityRadar();
}

// -------------------------------------------------------------
// Background Tasks, LLM Queue Status & Automation Schedules
// -------------------------------------------------------------

let currentActiveTasks = [];
let taskEventSource = null;

async function fetchTasksStatus(showToastFeedback = false) {
    try {
        const [sysRes, schedRes] = await Promise.all([
            fetch(`${API_BASE}/api/system/unified-status`),
            fetch(`${API_BASE}/api/scheduler/jobs`)
        ]);

        if (sysRes.ok) {
            const sysData = await sysRes.json();
            currentActiveTasks = sysData.active_background_tasks || [];
            renderActiveTasks(currentActiveTasks);
            renderLLMQueueStatus(sysData.llm_queue || {});
            renderTasksHistory(sysData.recent_background_tasks || []);
            updateHeaderTaskPill(currentActiveTasks);
        }

        if (schedRes.ok) {
            const schedData = await schedRes.json();
            renderSchedules(schedData || []);
        }

        if (showToastFeedback) {
            showToast("✓ Background tasks and queue status refreshed.");
        }
    } catch (err) {
        console.warn("Could not fetch tasks status:", err);
    }
}

function updateHeaderTaskPill(activeTasks) {
    const pill = document.getElementById("header-task-pill");
    const textEl = document.getElementById("header-task-pill-text");
    if (!pill || !textEl) return;

    if (activeTasks && activeTasks.length > 0) {
        const first = activeTasks[0];
        const pct = Math.round(first.progress_percentage || 0);
        const count = activeTasks.length;
        textEl.innerText = count > 1 ? `${count} Tasks Active (${pct}%)` : `${first.task_name} (${pct}%)`;
        pill.classList.remove("hidden");
    } else {
        pill.classList.add("hidden");
    }
}

function renderActiveTasks(tasks) {
    const container = document.getElementById("active-tasks-list");
    const badge = document.getElementById("tasks-active-badge");
    if (!container) return;

    if (badge) {
        badge.innerText = `${tasks.length} Running`;
        badge.style.color = tasks.length > 0 ? "#89b4fa" : "#a6adc8";
    }

    if (!tasks || tasks.length === 0) {
        container.innerHTML = `
            <div class="empty-state" style="padding: 30px 10px; text-align: center;">
                <span style="font-size: 28px;">☕</span>
                <p style="color: #6c7086; font-size: 13px; margin-top: 6px;">No active background tasks running right now.</p>
            </div>
        `;
        return;
    }

    container.innerHTML = tasks.map(t => {
        const pct = Math.round(t.progress_percentage || 0);
        return `
            <div class="task-progress-card status-${t.status}" id="task-card-${t.task_id}">
                <div style="display: flex; justify-content: space-between; align-items: flex-start;">
                    <div>
                        <div style="display: flex; align-items: center; gap: 8px;">
                            <h5 style="font-size: 14px; color: #cdd6f4; font-weight: 600; margin: 0;">${escapeHTML(t.task_name)}</h5>
                            <span class="badge" style="font-size: 10px; background: rgba(137,180,250,0.15); color: #89b4fa;">#${t.task_id}</span>
                        </div>
                        <p style="font-size: 12px; color: #a6adc8; margin: 4px 0 0 0;" id="task-step-label-${t.task_id}">
                            ${escapeHTML(t.step_label || 'In progress...')}
                        </p>
                    </div>
                    <button type="button" class="btn btn-xs btn-outline" onclick="cancelTask('${t.task_id}')" style="color: #f38ba8; border-color: rgba(243,139,168,0.35); font-size: 11px;">
                        ⏹️ Stop Task
                    </button>
                </div>

                <div class="task-progress-track">
                    <div class="task-progress-fill" id="task-fill-${t.task_id}" style="width: ${pct}%;"></div>
                </div>

                <div style="display: flex; justify-content: space-between; font-size: 11px; color: #6c7086;">
                    <span>📥 <strong style="color: #a6e3a1;">${t.items_discovered || 0}</strong> items found</span>
                    <span id="task-pct-${t.task_id}"><strong>${pct}%</strong> (${t.current_step}/${t.total_steps} steps)</span>
                    <span>⏱️ ${t.elapsed_seconds || 0}s elapsed</span>
                </div>
            </div>
        `;
    }).join("");
}

function renderLLMQueueStatus(llm) {
    const depthEl = document.getElementById("queue-depth-metric");
    const workersEl = document.getElementById("queue-workers-metric");
    const activeTaskEl = document.getElementById("queue-active-task-name");
    const avgLatEl = document.getElementById("queue-avg-latency");
    const modeBadge = document.getElementById("llm-workers-badge");

    if (depthEl) depthEl.innerText = `${llm.queue_depth || 0} tasks`;
    if (workersEl) workersEl.innerText = `${llm.max_concurrency || 1} Worker${(llm.max_concurrency || 1) > 1 ? 's' : ''}`;
    if (activeTaskEl) activeTaskEl.innerText = llm.active_task || "Idle";
    if (avgLatEl) avgLatEl.innerText = `${llm.avg_latency_ms || 0} ms`;
    if (modeBadge) {
        modeBadge.innerText = llm.mode === "cloud" ? "Cloud Dynamic (8 Workers)" : "Local Paced (1 Worker)";
        modeBadge.style.color = llm.mode === "cloud" ? "#89b4fa" : "#a6e3a1";
    }
}

function renderSchedules(schedules) {
    const container = document.getElementById("schedules-list-container");
    if (!container) return;

    if (!schedules || schedules.length === 0) {
        container.innerHTML = `<p style="color: #6c7086; font-size: 12px;">No automated schedules configured.</p>`;
        return;
    }

    container.innerHTML = schedules.map(s => {
        const nextRun = s.next_run_at ? formatTime(s.next_run_at) : "Pending";
        const lastRun = s.last_run_at ? formatTime(s.last_run_at) : "Never";

        return `
            <div class="schedule-row-card">
                <div style="flex: 1;">
                    <div style="display: flex; align-items: center; gap: 8px;">
                        <h5 style="font-size: 13px; color: #cdd6f4; font-weight: 600; margin: 0;">${escapeHTML(s.name)}</h5>
                        <span class="badge" style="font-size: 10px; background: rgba(203,166,247,0.15); color: #cba6f7;">Every ${s.interval_hours}h</span>
                    </div>
                    <p style="font-size: 11px; color: #a6adc8; margin: 3px 0 0 0;">${escapeHTML(s.description)}</p>
                    <div style="display: flex; gap: 14px; font-size: 11px; color: #6c7086; margin-top: 4px;">
                        <span>Last Run: <strong>${lastRun}</strong> (${s.last_status || 'IDLE'})</span>
                        <span>Next Trigger: <strong style="color: #89b4fa;">${nextRun}</strong></span>
                    </div>
                </div>

                <div style="display: flex; align-items: center; gap: 12px;">
                    <label class="schedule-toggle-switch" title="Toggle automatic execution">
                        <input type="checkbox" ${s.is_enabled ? 'checked' : ''} onchange="toggleSchedule('${s.id}', this.checked)">
                        <span class="schedule-toggle-slider"></span>
                    </label>
                    <button type="button" class="btn btn-xs btn-primary" onclick="runScheduledJobNow('${s.id}')" title="Run now on-demand">
                        ▶️ Run Now
                    </button>
                </div>
            </div>
        `;
    }).join("");
}

function renderTasksHistory(tasks) {
    const tbody = document.getElementById("tasks-history-tbody");
    const countEl = document.getElementById("tasks-history-count");
    if (!tbody) return;

    const completedTasks = tasks.filter(t => t.status !== "RUNNING" && t.status !== "QUEUED");
    if (countEl) countEl.innerText = `${completedTasks.length} tasks`;

    if (completedTasks.length === 0) {
        tbody.innerHTML = `<tr><td colspan="5" style="text-align: center; padding: 20px; color: #6c7086;">No completed tasks recorded yet.</td></tr>`;
        return;
    }

    tbody.innerHTML = completedTasks.map(t => {
        const completedTime = t.completed_at ? formatTime(t.completed_at) : "-";
        const statusColor = t.status === "COMPLETED" ? "#a6e3a1" : (t.status === "FAILED" ? "#f38ba8" : "#f9e2af");
        return `
            <tr style="border-bottom: 1px solid #28293d;">
                <td style="padding: 8px 10px; color: #cdd6f4; font-weight: 500;">${escapeHTML(t.task_name)}</td>
                <td style="padding: 8px 10px;"><span class="badge" style="color: ${statusColor}; border-color: ${statusColor}; font-size: 10px;">${t.status}</span></td>
                <td style="padding: 8px 10px; color: #a6adc8;">${t.items_discovered || 0} items</td>
                <td style="padding: 8px 10px; color: #6c7086;">${t.elapsed_seconds || 0}s</td>
                <td style="padding: 8px 10px; color: #6c7086;">${completedTime}</td>
            </tr>
        `;
    }).join("");
}

async function cancelTask(taskId) {
    try {
        const res = await fetch(`${API_BASE}/api/tasks/${taskId}/cancel`, { method: "POST" });
        if (res.ok) {
            showToast(`⏹️ Requested cancellation for task #${taskId}.`);
            fetchTasksStatus();
        } else {
            showToast("Could not cancel task or task already completed.");
        }
    } catch (err) {
        console.error("Cancel task error:", err);
    }
}

async function clearCompletedTasks() {
    try {
        const res = await fetch(`${API_BASE}/api/tasks/completed`, { method: "DELETE" });
        if (res.ok) {
            const data = await res.json();
            showToast(`🧹 Cleared ${data.cleared_count || 0} completed task records.`);
            fetchTasksStatus();
        }
    } catch (err) {
        console.error("Clear completed tasks error:", err);
    }
}

async function toggleSchedule(jobId, enabled) {
    try {
        const res = await fetch(`${API_BASE}/api/scheduler/jobs/${jobId}`, {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ is_enabled: enabled })
        });
        if (res.ok) {
            showToast(enabled ? "✓ Schedule enabled." : "Schedule disabled.");
            fetchTasksStatus();
        }
    } catch (err) {
        console.error("Toggle schedule error:", err);
    }
}

async function runScheduledJobNow(jobId) {
    try {
        const res = await fetch(`${API_BASE}/api/scheduler/jobs/${jobId}/run`, { method: "POST" });
        if (res.ok) {
            const data = await res.json();
            showToast(`🚀 Dispatched background job: ${data.message || 'Running in background...'}`);
            fetchTasksStatus();
        } else {
            showToast("⚠️ Could not dispatch scheduled job.");
        }
    } catch (err) {
        console.error("Run scheduled job error:", err);
    }
}

function initTaskSSE() {
    try {
        if (taskEventSource) {
            taskEventSource.close();
        }

        taskEventSource = new EventSource(`${API_BASE}/api/tasks/events`);

        taskEventSource.onmessage = (event) => {
            if (!event.data || event.data === ": ping") return;
            try {
                const prog = JSON.parse(event.data);
                
                // Update specific task card if present
                const fillEl = document.getElementById(`task-fill-${prog.task_id}`);
                const labelEl = document.getElementById(`task-step-label-${prog.task_id}`);
                const pctEl = document.getElementById(`task-pct-${prog.task_id}`);

                if (fillEl && labelEl && pctEl) {
                    const pct = Math.round(prog.progress_percentage || 0);
                    fillEl.style.width = `${pct}%`;
                    labelEl.innerText = prog.step_label || "";
                    pctEl.innerHTML = `<strong>${pct}%</strong> (${prog.current_step}/${prog.total_steps} steps)`;
                }

                // If completed, toast and refresh jobs
                if (prog.status === "COMPLETED") {
                    showToast(`🎉 Task '${prog.task_name}' complete! Discovered ${prog.items_discovered || 0} items.`);
                    fetchJobs();
                    fetchTasksStatus();
                } else if (prog.status === "FAILED" || prog.status === "CANCELLED") {
                    fetchTasksStatus();
                }
            } catch (e) {}
        };

        taskEventSource.onerror = () => {
            // Reconnect automatically handled by browser EventSource
        };
    } catch (err) {
        console.debug("SSE initialization notice:", err);
    }
}

// Global Exports
window.navigateToSection = navigateToSection;
window.fetchTasksStatus = fetchTasksStatus;
window.cancelTask = cancelTask;
window.clearCompletedTasks = clearCompletedTasks;
window.toggleSchedule = toggleSchedule;
window.runScheduledJobNow = runScheduledJobNow;

window.fetchGoogleJobQueries = fetchGoogleJobQueries;
window.renderGoogleSearchesList = renderGoogleSearchesList;
window.addGoogleJobQuery = addGoogleJobQuery;
window.deleteGoogleJobQuery = deleteGoogleJobQuery;
window.handleAddGoogleSearchFromProfile = handleAddGoogleSearchFromProfile;
window.syncGoogleFollowedQueries = syncGoogleFollowedQueries;
window.toggleGoogleJobsSingleQueryMode = toggleGoogleJobsSingleQueryMode;
window.openGoogleJobsModal = openGoogleJobsModal;
window.closeGoogleJobsModal = closeGoogleJobsModal;
window.submitGoogleJobsScrape = submitGoogleJobsScrape;
window.toggleCriteriaFilter = toggleCriteriaFilter;

window.openOpportunityRadarModal = openOpportunityRadarModal;
window.closeOpportunityRadarModal = closeOpportunityRadarModal;
window.fetchOpportunityRadar = fetchOpportunityRadar;
window.renderOpportunityRadar = renderOpportunityRadar;
window.openRadarJob = openRadarJob;
window.radarUpdateStatus = radarUpdateStatus;
window.handleTimezoneChange = handleTimezoneChange;
window.populateTimezoneSelect = populateTimezoneSelect;
window.suggestRelatedRoles = suggestRelatedRoles;
window.addSuggestedRole = addSuggestedRole;

loadCopilotHistory();
initTaskSSE();
fetchTasksStatus();

document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && copilotIsOpen) {
        toggleCopilotDrawer();
    }
});


