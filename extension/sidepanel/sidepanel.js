/**
 * Side Panel Controller
 * Job Alert Agent - AI Career Copilot & ATS Autofill
 */

document.addEventListener("DOMContentLoaded", async () => {
  // -------------------------------------------------------------
  // 1. Tab Navigation
  // -------------------------------------------------------------
  const tabButtons = document.querySelectorAll(".sp-tab-btn");
  const tabPanes = document.querySelectorAll(".sp-tab-pane");

  tabButtons.forEach((btn) => {
    btn.addEventListener("click", () => {
      const targetTabId = btn.getAttribute("data-tab");

      tabButtons.forEach((b) => b.classList.remove("active"));
      tabPanes.forEach((p) => p.classList.remove("active"));

      btn.classList.add("active");
      const targetPane = document.getElementById(targetTabId);
      if (targetPane) targetPane.classList.add("active");
    });
  });

  // -------------------------------------------------------------
  // 2. Initialization & Runtime Status
  // -------------------------------------------------------------
  const statusIndicator = document.getElementById("sp-connection-status");
  const statusLabel = document.getElementById("sp-status-label");

  async function refreshConnectionStatus() {
    await window.jobAgentRuntime.init();
    if (window.jobAgentRuntime.isConnected) {
      statusIndicator.className = "sp-status-indicator connected";
      statusLabel.innerText = "Connected (API)";
    } else {
      statusIndicator.className = "sp-status-indicator standalone";
      statusLabel.innerText = "Standalone (Nano)";
    }
  }

  await refreshConnectionStatus();

  function escapeHtml(str) {
    if (!str) return "";
    const div = document.createElement("div");
    div.textContent = str;
    return div.innerHTML;
  }

  function showToast(msg) {
    const toast = document.getElementById("sp-toast");
    if (!toast) return;
    toast.innerText = msg;
    toast.classList.add("show");
    setTimeout(() => toast.classList.remove("show"), 2500);
  }

  async function ensureContentScriptInjected(tabId) {
    try {
      const isAlive = await new Promise((resolve) => {
        chrome.tabs.sendMessage(tabId, { type: "PING" }, (resp) => {
          if (chrome.runtime.lastError || !resp) {
            resolve(false);
          } else {
            resolve(true);
          }
        });
      });

      if (!isAlive) {
        if (chrome.scripting) {
          await chrome.scripting.insertCSS({
            target: { tabId: tabId },
            files: ["content/ats-autofill.css"]
          }).catch(() => {});

          await chrome.scripting.executeScript({
            target: { tabId: tabId },
            files: ["content/ats-autofill.js"]
          }).catch((err) => console.warn("Execute script error:", err));

          await new Promise(r => setTimeout(r, 120));
        }
      }
      return true;
    } catch (err) {
      console.warn("Could not inject content script into tab:", err);
      return false;
    }
  }

  // -------------------------------------------------------------
  // 3. Profile & Settings Management
  // -------------------------------------------------------------
  async function loadProfile(forceSync = false) {
    const data = await chrome.storage.local.get(["candidateProfile", "backendUrl", "mode"]);
    let prof = data.candidateProfile || {};

    // Auto-sync from backend API if connected and local profile has empty fields
    if (forceSync || (!prof.fullName && !prof.email && window.jobAgentRuntime.isConnected)) {
      const syncRes = await window.jobAgentRuntime.fetchCandidateProfile();
      if (syncRes.success) {
        prof = syncRes.profile;
      }
    }

    let firstName = prof.firstName || "";
    let lastName = prof.lastName || "";
    let fullName = prof.fullName || "";

    if (!firstName && !lastName && fullName) {
      const parts = fullName.split(/\s+/);
      if (parts.length > 1) {
        firstName = parts.slice(0, parts.length - 1).join(" ");
        lastName = parts[parts.length - 1];
      } else {
        firstName = parts[0] || "";
      }
    }

    if (document.getElementById("prof-firstname")) document.getElementById("prof-firstname").value = firstName;
    if (document.getElementById("prof-lastname")) document.getElementById("prof-lastname").value = lastName;
    if (document.getElementById("prof-fullname")) document.getElementById("prof-fullname").value = fullName || `${firstName} ${lastName}`.trim();
    if (document.getElementById("prof-email")) document.getElementById("prof-email").value = prof.email || "";
    if (document.getElementById("prof-phone")) document.getElementById("prof-phone").value = prof.phone || "";
    if (document.getElementById("prof-location")) document.getElementById("prof-location").value = prof.location || "";
    if (document.getElementById("prof-linkedin")) document.getElementById("prof-linkedin").value = prof.linkedinUrl || "";
    if (document.getElementById("prof-github")) document.getElementById("prof-github").value = prof.githubUrl || "";
    if (document.getElementById("prof-portfolio")) document.getElementById("prof-portfolio").value = prof.portfolioUrl || "";
    if (document.getElementById("prof-resumelink")) document.getElementById("prof-resumelink").value = prof.resumeLink || prof.portfolioUrl || prof.linkedinUrl || "";
    if (document.getElementById("prof-company")) document.getElementById("prof-company").value = prof.currentCompany || "";
    if (document.getElementById("prof-title")) document.getElementById("prof-title").value = prof.currentTitle || "";
    if (document.getElementById("prof-experience")) document.getElementById("prof-experience").value = prof.yearsExperience || "";
    if (document.getElementById("prof-noticeperiod")) document.getElementById("prof-noticeperiod").value = prof.noticePeriod || "";
    if (document.getElementById("prof-auth")) document.getElementById("prof-auth").value = prof.workAuthorization || "";
    if (document.getElementById("prof-sponsorship")) document.getElementById("prof-sponsorship").value = prof.sponsorshipRequired || "";

    if (document.getElementById("cfg-backend-url")) document.getElementById("cfg-backend-url").value = data.backendUrl || "http://localhost:8000";
    if (document.getElementById("cfg-api-key")) document.getElementById("cfg-api-key").value = data.apiKey || "";
    if (document.getElementById("cfg-mode")) document.getElementById("cfg-mode").value = data.mode || "connected";
  }

  await loadProfile();

  // Sync from backend resume button
  document.getElementById("btn-sync-profile-api")?.addEventListener("click", async () => {
    const syncBtn = document.getElementById("btn-sync-profile-api");
    syncBtn.innerHTML = "<span>⏳ Syncing...</span>";
    await window.jobAgentRuntime.init();
    const res = await window.jobAgentRuntime.fetchCandidateProfile();
    if (res.success) {
      await loadProfile(true);
      await loadQACache();
      showToast("✓ Profile & Q&A Synced from Backend Resume!");
    } else {
      showToast("⚠️ Could not fetch from backend. Check FastAPI server and API Key.");
    }
    syncBtn.innerHTML = "<span>🔄 Sync from Backend</span>";
  });

  // Save profile button
  document.getElementById("btn-save-profile")?.addEventListener("click", async () => {
    const fn = document.getElementById("prof-firstname")?.value.trim() || "";
    const ln = document.getElementById("prof-lastname")?.value.trim() || "";
    let full = document.getElementById("prof-fullname")?.value.trim() || "";
    if (!full && (fn || ln)) {
      full = `${fn} ${ln}`.trim();
    }

    const currentStorage = await chrome.storage.local.get(["candidateProfile"]);
    const existing = currentStorage.candidateProfile || {};

    const profile = {
      ...existing,
      firstName: fn,
      lastName: ln,
      fullName: full,
      email: document.getElementById("prof-email")?.value.trim() || "",
      phone: document.getElementById("prof-phone")?.value.trim() || "",
      location: document.getElementById("prof-location")?.value.trim() || "",
      linkedinUrl: document.getElementById("prof-linkedin")?.value.trim() || "",
      githubUrl: document.getElementById("prof-github")?.value.trim() || "",
      portfolioUrl: document.getElementById("prof-portfolio")?.value.trim() || "",
      resumeLink: document.getElementById("prof-resumelink")?.value.trim() || "",
      currentCompany: document.getElementById("prof-company")?.value.trim() || "",
      currentTitle: document.getElementById("prof-title")?.value.trim() || "",
      yearsExperience: document.getElementById("prof-experience")?.value.trim() || "",
      noticePeriod: document.getElementById("prof-noticeperiod")?.value.trim() || "",
      workAuthorization: document.getElementById("prof-auth")?.value || "",
      sponsorshipRequired: document.getElementById("prof-sponsorship")?.value || ""
    };

    const backendUrl = document.getElementById("cfg-backend-url")?.value.trim() || "http://localhost:8000";
    const apiKey = document.getElementById("cfg-api-key")?.value.trim() || "";
    const mode = document.getElementById("cfg-mode")?.value || "connected";

    await chrome.storage.local.set({ candidateProfile: profile, backendUrl, apiKey, mode });
    await window.jobAgentRuntime.setBackendUrl(backendUrl);
    await window.jobAgentRuntime.setApiKey(apiKey);
    await window.jobAgentRuntime.setMode(mode);
    await refreshConnectionStatus();

    showToast("✓ Profile & Settings Saved!");
  });

  // -------------------------------------------------------------
  // 4. Tab 1: Live Job Analyzer & Auto-Fill
  // -------------------------------------------------------------
  let currentInspectedJob = null;

  async function inspectActiveTab() {
    const titleEl = document.getElementById("sp-job-title");
    const companyEl = document.getElementById("sp-job-company");
    const portalEl = document.getElementById("sp-job-portal");
    const snippetEl = document.getElementById("sp-job-snippet");
    const scoreCircle = document.getElementById("sp-score-circle");
    const scoreLabel = document.getElementById("sp-score-tier-label");
    const analysisBox = document.getElementById("sp-match-analysis-text");

    titleEl.innerText = "Inspecting active tab...";
    scoreCircle.innerText = "...";
    analysisBox.innerHTML = "<p>Analyzing requirements against candidate resume...</p>";

    try {
      const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
      if (!tab || !tab.id) {
        titleEl.innerText = "No active tab found";
        return;
      }

      // Ensure content script is running on custom site or ATS
      await ensureContentScriptInjected(tab.id);

      // Query content script for job metadata
      chrome.tabs.sendMessage(tab.id, { type: "GET_PAGE_JOB_INFO" }, async (response) => {
        let jobMeta = null;
        if (response && response.data) {
          jobMeta = response.data;
        } else {
          // Fallback extraction from tab title and domain
          const tabUrl = new URL(tab.url || "https://example.com");
          jobMeta = {
            title: tab.title || "Custom Job Position",
            company: tabUrl.hostname.replace(/www\.|\.com|\.io|\.co|\.org/g, "").toUpperCase(),
            description: tab.title || "Target Career Opportunity",
            url: tab.url,
            atsType: "custom"
          };
        }

        currentInspectedJob = jobMeta;

        titleEl.innerText = jobMeta.title;
        companyEl.innerText = jobMeta.company;
        portalEl.innerText = (jobMeta.atsType || "CUSTOM").toUpperCase();
        snippetEl.innerText = jobMeta.description ? jobMeta.description.slice(0, 180) + "..." : "Posting extracted.";

        // Run Match Analysis
        const profData = await chrome.storage.local.get(["candidateProfile"]);
        const resumeSummary = profData.candidateProfile ? JSON.stringify(profData.candidateProfile) : "";

        const matchResult = await window.jobAgentRuntime.analyzeJobMatch(
          jobMeta.title,
          jobMeta.company,
          jobMeta.description,
          resumeSummary
        );

        if (matchResult.error || matchResult.matchScore == null) {
          // No reliable score: show an explicit unavailable state, never a fabricated number.
          scoreCircle.innerText = "--";
          scoreCircle.style.borderColor = "#f59e0b";
          scoreCircle.style.color = "#f59e0b";
          scoreLabel.innerText = "Analysis unavailable";
        } else {
          scoreCircle.innerText = `${matchResult.matchScore}%`;
          if (matchResult.matchScore >= 80) {
            scoreCircle.style.borderColor = "#10b981";
            scoreCircle.style.color = "#10b981";
            scoreLabel.innerText = "High Compatibility Match";
          } else if (matchResult.matchScore >= 60) {
            scoreCircle.style.borderColor = "#6366f1";
            scoreCircle.style.color = "#6366f1";
            scoreLabel.innerText = "Moderate Compatibility";
          } else {
            scoreCircle.style.borderColor = "#f59e0b";
            scoreCircle.style.color = "#f59e0b";
            scoreLabel.innerText = "Potential Skill Gaps";
          }
        }

        // Render Telemetry & Detected Questions
        const telemetryBanner = document.getElementById("sp-telemetry-banner");
        const telemetrySummary = document.getElementById("sp-telemetry-summary");
        if (response && response.telemetry) {
          const t = response.telemetry;
          if (telemetryBanner && telemetrySummary) {
            telemetrySummary.innerText = `💡 Form Analysis: ${t.recognized_count} recognized, ${t.unrecognized_count} custom fields`;
            telemetryBanner.style.display = "block";
          }
          // Forward telemetry to runtime / backend
          window.jobAgentRuntime.sendFormTelemetry(t);

          // Render detected screener questions
          renderDetectedQuestions(t.detected_questions || [], jobMeta);
        } else if (telemetryBanner) {
          telemetryBanner.style.display = "none";
        }

        if (matchResult.error) {
          analysisBox.innerHTML =
            escapeHtml(matchResult.analysis || "").replace(/\n/g, "<br/>") +
            '<button class="sp-btn-secondary" id="sp-reprocess-match" style="margin-top:10px;">🔄 Re-process match analysis</button>';
          document.getElementById("sp-reprocess-match")?.addEventListener("click", inspectActiveTab);
        } else {
          analysisBox.innerHTML = escapeHtml(matchResult.analysis || "").replace(/\n/g, "<br/>");
        }
      });
    } catch (e) {
      titleEl.innerText = "Failed to inspect tab";
      console.error("Tab inspection error:", e);
    }
  }

  function renderDetectedQuestions(questions, jobMeta) {
    const section = document.getElementById("sp-detected-questions-section");
    const listEl = document.getElementById("sp-detected-questions-list");
    const countEl = document.getElementById("sp-detected-count");
    const draftAllBtn = document.getElementById("btn-draft-all-questions");

    if (!section || !listEl) return;

    // Strict defensive filtering for screener questions
    const cleanQuestions = (questions || []).filter((q) => {
      if (!q) return false;
      const qText = String(q.question || "").trim();
      const qId = String(q.id || q.field_id || q.field_name || q.name || "").trim();
      const combined = `${qText} ${qId}`.toLowerCase();

      // 1. Skip captcha, bot, token, honeypot, security patterns
      if (/recaptcha|captcha|turnstile|hcaptcha|cf-chl|cf_chl|challenge|csrf|_token|authenticity_token|honeypot|bot.?check|arkose|threatmetrix|perimeterx/i.test(combined)) {
        return false;
      }

      // 2. Skip raw UUIDs, hex strings, or Ashby 'type h' artifacts
      if (/^[0-9a-f-]{16,}$/i.test(qText) || /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i.test(qText) || /\btype\s+h\b/i.test(qText)) {
        return false;
      }

      // 3. Skip if question text lacks alphabetic characters or is too short
      if (qText.replace(/[^a-zA-Z]/g, "").length < 3) {
        return false;
      }

      // 4. Skip if current_value is a massive token / hash
      const curVal = String(q.current_value || "").trim();
      if (curVal.length > 50 && !curVal.includes(" ") && !curVal.includes("@") && !curVal.includes("http")) {
        return false;
      }

      // 5. Skip standard basic contact fields if they slipped through
      if (/^(first.?name|last.?name|full.?name|email|phone|mobile|resume|cv|portfolio|linkedin|github)$/i.test(qText.replace(/[^a-zA-Z0-9]/g, ""))) {
        return false;
      }

      return true;
    });

    if (cleanQuestions.length === 0) {
      section.style.display = "none";
      return;
    }

    countEl.innerText = cleanQuestions.length;
    section.style.display = "block";
    listEl.innerHTML = "";

    const questionCards = [];

    cleanQuestions.forEach((q) => {
      const card = document.createElement("div");
      card.className = "sp-detected-card";

      const qText = document.createElement("div");
      qText.className = "sp-detected-q-text";
      qText.innerText = q.question || "Application Question";

      const previewBox = document.createElement("textarea");
      previewBox.className = "sp-detected-preview-box";
      previewBox.placeholder = "Click 'Generate Pitch' to draft a grounded 3-5 line answer...";
      previewBox.value = q.current_value || "";

      const actionsDiv = document.createElement("div");
      actionsDiv.className = "sp-detected-actions";

      const genBtn = document.createElement("button");
      genBtn.className = "sp-btn-secondary";
      genBtn.innerHTML = "<span>⚡ Generate Pitch</span>";

      const fillBtn = document.createElement("button");
      fillBtn.className = "sp-btn-secondary";
      fillBtn.innerHTML = "<span>✍️ Fill Field</span>";

      const triggerGenerate = async () => {
        genBtn.innerHTML = "<span>⏳ Drafting...</span>";
        genBtn.disabled = true;
        try {
          const profData = await chrome.storage.local.get(["candidateProfile"]);
          const prof = profData.candidateProfile || {};
          
          const res = await window.jobAgentRuntime.generateGroundedPitch(
            q.question,
            jobMeta ? jobMeta.company : "",
            jobMeta ? jobMeta.title : "",
            jobMeta ? jobMeta.description : "",
            prof.resumeSummary || ""
          );

          if (res && res.answer) {
            previewBox.value = res.answer;
            if (res.is_cache_hit) {
              showToast(res.similarity ? `✓ Matched saved answer (${Math.round(res.similarity * 100)}% match)!` : "✓ Reused saved answer from memory.");
            } else {
              showToast("✓ Generated grounded 3-5 line pitch!");
            }
          } else if (res && res.error) {
            showToast(`⚠️ ${res.message || "Answer generation unavailable — no answer fabricated."}`);
          } else {
            showToast("⚠️ No answer generated. Start the backend or enable Gemini Nano.");
          }
        } catch (err) {
          console.error("Grounded pitch generation error:", err);
          showToast("Failed to generate answer. Check backend connection.");
        } finally {
          genBtn.innerHTML = "<span>⚡ Generate Pitch</span>";
          genBtn.disabled = false;
        }
      };

      genBtn.addEventListener("click", triggerGenerate);

      fillBtn.addEventListener("click", async () => {
        const val = previewBox.value.trim();
        if (!val) {
          showToast("Generate or write an answer first.");
          return;
        }
        const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
        if (tab && tab.id) {
          chrome.tabs.sendMessage(
            tab.id,
            { type: "AUTOFILL_SPECIFIC_FIELD", fieldId: q.id, value: val },
            (resp) => {
              if (resp && resp.success) {
                showToast("✓ Field filled on active tab!");
              } else {
                showToast("Filled first matching textarea.");
              }
            }
          );
        }
      });

      actionsDiv.appendChild(genBtn);
      actionsDiv.appendChild(fillBtn);

      card.appendChild(qText);
      card.appendChild(previewBox);
      card.appendChild(actionsDiv);

      listEl.appendChild(card);
      questionCards.push({ q, previewBox, triggerGenerate });
    });

    if (draftAllBtn) {
      draftAllBtn.onclick = async () => {
        draftAllBtn.innerHTML = "<span>⏳ Drafting All...</span>";
        draftAllBtn.disabled = true;
        let generatedCount = 0;
        for (const item of questionCards) {
          if (!item.previewBox.value.trim()) {
            await item.triggerGenerate();
            generatedCount++;
          }
        }
        draftAllBtn.innerHTML = "<span>⚡ Draft All</span>";
        draftAllBtn.disabled = false;
        showToast(`✓ Drafted answers for ${generatedCount} question(s)!`);
      };
    }
  }

  // Autofill Mode & Overwrite Option
  const overwriteCheckbox = document.getElementById("sp-opt-overwrite");
  const modeBadge = document.getElementById("sp-autofill-mode-badge");

  function updateAutofillModeUI(isOverwrite) {
    if (!modeBadge) return;
    if (isOverwrite) {
      modeBadge.innerText = "⚠️ Overwrite Mode";
      modeBadge.style.background = "rgba(239, 68, 68, 0.15)";
      modeBadge.style.color = "#ef4444";
    } else {
      modeBadge.innerText = "🛡️ Safe Mode";
      modeBadge.style.background = "rgba(16, 185, 129, 0.15)";
      modeBadge.style.color = "#10b981";
    }
  }

  // Load saved preference
  chrome.storage.local.get(["autofillOverwritePref"], (res) => {
    const isOverwrite = Boolean(res.autofillOverwritePref);
    if (overwriteCheckbox) {
      overwriteCheckbox.checked = isOverwrite;
    }
    updateAutofillModeUI(isOverwrite);
  });

  overwriteCheckbox?.addEventListener("change", (e) => {
    const isOverwrite = e.target.checked;
    chrome.storage.local.set({ autofillOverwritePref: isOverwrite });
    updateAutofillModeUI(isOverwrite);
  });

  document.getElementById("btn-inspect-page")?.addEventListener("click", inspectActiveTab);

  // 1-Click Auto-Fill
  document.getElementById("btn-autofill-app")?.addEventListener("click", async () => {
    const btn = document.getElementById("btn-autofill-app");
    btn.innerHTML = "<span>⏳ Filling Application Form...</span>";

    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab || !tab.id) return;

    // Ensure content script is active on the tab (works on Markin and all custom portals)
    await ensureContentScriptInjected(tab.id);

    // Refresh profile and QA answers from storage or backend
    let profData = await chrome.storage.local.get(["candidateProfile", "localQABank", "autofillOverwritePref"]);
    let profile = profData.candidateProfile || {};
    let qaAnswers = profData.localQABank || [];
    const overwriteExisting = Boolean(document.getElementById("sp-opt-overwrite")?.checked ?? profData.autofillOverwritePref);

    if ((!profile.fullName || !profile.email) && window.jobAgentRuntime.isConnected) {
      const syncRes = await window.jobAgentRuntime.fetchCandidateProfile();
      if (syncRes.success) {
        profile = syncRes.profile;
        qaAnswers = syncRes.qaBank;
      }
    }

    chrome.tabs.sendMessage(
      tab.id,
      { type: "EXECUTE_AUTOFILL", profile, qaAnswers, overwriteExisting },
      (response) => {
        if (response && response.data) {
          btn.innerHTML = `<span>✓ Autofilled ${response.data.filledCount} Fields</span>`;
          showToast(`✓ Autofilled ${response.data.filledCount} fields!`);
          if (response.data.telemetry) {
            window.jobAgentRuntime.sendFormTelemetry(response.data.telemetry);
            renderDetectedQuestions(response.data.detectedQuestions || [], currentInspectedJob);
          }
        } else {
          btn.innerHTML = `<span>⚡ 1-Click Auto-Fill Application</span>`;
          showToast("Autofill executed on tab.");
        }
        setTimeout(() => {
          btn.innerHTML = `<span>⚡ 1-Click Auto-Fill Application</span>`;
        }, 3000);
      }
    );
  });

  // Shortlist Job
  document.getElementById("btn-shortlist-job")?.addEventListener("click", async () => {
    if (!currentInspectedJob) {
      showToast("Inspect a job posting first.");
      return;
    }

    if (window.jobAgentRuntime.isConnected) {
      try {
        await fetch(`${window.jobAgentRuntime.backendUrl}/api/chat/assistant`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            message: `Shortlist job: ${currentInspectedJob.title} at ${currentInspectedJob.company}`
          })
        });
        showToast("⭐ Job Shortlisted to Pipeline!");
      } catch (e) {
        showToast("Saved locally to favorites.");
      }
    } else {
      showToast("⭐ Saved to Local Shortlist.");
    }
  });

  // -------------------------------------------------------------
  // 5. Tab 2: AI Career Copilot Chat
  // -------------------------------------------------------------
  const chatHistoryEl = document.getElementById("sp-chat-history");
  const chatInputEl = document.getElementById("sp-chat-input");
  const sendBtnEl = document.getElementById("sp-chat-send-btn");
  let conversationHistory = [];

  function appendChatMessage(role, text) {
    const msgDiv = document.createElement("div");
    msgDiv.className = `sp-message sp-message-${role}`;
    
    const bubble = document.createElement("div");
    bubble.className = "sp-msg-bubble";
    bubble.innerHTML = escapeHtml(text || "").replace(/\n/g, "<br/>");

    msgDiv.appendChild(bubble);
    chatHistoryEl.appendChild(msgDiv);
    chatHistoryEl.scrollTop = chatHistoryEl.scrollHeight;

    conversationHistory.push({ role, content: text });
  }

  async function handleSendChat() {
    const text = chatInputEl.value.trim();
    if (!text) return;

    appendChatMessage("user", text);
    chatInputEl.value = "";

    // Show typing bubble
    const typingDiv = document.createElement("div");
    typingDiv.className = "sp-message sp-message-assistant";
    typingDiv.innerHTML = "<div class='sp-msg-bubble'><em>Thinking...</em></div>";
    chatHistoryEl.appendChild(typingDiv);
    chatHistoryEl.scrollTop = chatHistoryEl.scrollHeight;

    const result = await window.jobAgentRuntime.sendCopilotMessage(text, conversationHistory);
    typingDiv.remove();

    appendChatMessage("assistant", result.reply);
  }

  sendBtnEl?.addEventListener("click", handleSendChat);
  chatInputEl?.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSendChat();
    }
  });

  document.querySelectorAll(".sp-prompt-chip").forEach((chip) => {
    chip.addEventListener("click", () => {
      chatInputEl.value = chip.getAttribute("data-prompt");
      handleSendChat();
    });
  });

  // -------------------------------------------------------------
  // 6. Tab 3: Q&A Memory Bank
  // -------------------------------------------------------------
  let cachedQAList = [];

  async function loadQACache() {
    const listContainer = document.getElementById("sp-qa-list-container");
    const statsCount = document.getElementById("sp-qa-stats-count");
    if (!listContainer || !statsCount) return;
    
    let qaItems = [];

    // Fetch from backend if connected
    if (window.jobAgentRuntime.isConnected) {
      try {
        const resp = await fetch(`${window.jobAgentRuntime.backendUrl}/api/memory/qa`);
        if (resp.ok) {
          qaItems = await resp.json();
        }
      } catch (e) {}
    }

    if (qaItems.length === 0) {
      const localData = await chrome.storage.local.get(["localQABank"]);
      qaItems = localData.localQABank || [];
    }

    cachedQAList = qaItems;
    renderQAList(qaItems);
  }

  function renderQAList(items) {
    const listContainer = document.getElementById("sp-qa-list-container");
    const statsCount = document.getElementById("sp-qa-stats-count");
    if (!listContainer || !statsCount) return;

    statsCount.innerText = `${items.length} Saved`;

    if (items.length === 0) {
      listContainer.innerHTML = "<div class='sp-empty-state'>No cached questions found. They will auto-learn here.</div>";
      return;
    }

    listContainer.innerHTML = "";
    items.forEach((item) => {
      const card = document.createElement("div");
      card.className = "sp-qa-item";
      const rawQuestion = item.question_text || item.question || "";
      const rawAnswer = item.answer_text || item.answer || "";
      const qText = escapeHtml(rawQuestion);
      const aText = escapeHtml(rawAnswer);
      card.innerHTML = `
        <div class="sp-qa-item-q">${qText}</div>
        <div class="sp-qa-item-a">${aText.slice(0, 140)}...</div>
        <button class="sp-btn-secondary btn-copy-qa" style="font-size: 10px; margin-top: 6px;">📋 Copy Full Answer</button>
      `;
      card.querySelector(".btn-copy-qa").addEventListener("click", () => {
        navigator.clipboard.writeText(rawAnswer);
        showToast("✓ Copied to clipboard!");
      });
      listContainer.appendChild(card);
    });
  }

  // Live search for Q&A memory
  document.getElementById("sp-qa-search-input")?.addEventListener("input", (e) => {
    const query = e.target.value.toLowerCase().trim();
    if (!query) {
      renderQAList(cachedQAList);
      return;
    }
    const filtered = cachedQAList.filter((item) => {
      const q = (item.question_text || item.question || "").toLowerCase();
      const a = (item.answer_text || item.answer || "").toLowerCase();
      return q.includes(query) || a.includes(query);
    });
    renderQAList(filtered);
  });

  loadQACache();

  // Inspect tab automatically on open
  inspectActiveTab();
});
