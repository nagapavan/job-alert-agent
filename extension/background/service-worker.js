/**
 * Service Worker (Manifest V3)
 * Job Alert Agent - AI Career Copilot & ATS Autofill
 */

// Enable side panel to open automatically when user clicks the extension action icon
chrome.sidePanel
  .setPanelBehavior({ openPanelOnActionClick: true })
  .catch((error) => console.error("Failed to set panel behavior:", error));

// Listen for installation
chrome.runtime.onInstalled.addListener(async (details) => {
  if (details.reason === "install") {
    // Seed default settings into chrome.storage.local
    const existing = await chrome.storage.local.get([
      "backendUrl",
      "mode",
      "candidateProfile"
    ]);

    if (!existing.backendUrl) {
      await chrome.storage.local.set({
        backendUrl: "http://localhost:8000",
        mode: "connected", // "connected" (FastAPI) or "standalone" (Gemini Nano)
        candidateProfile: {
          fullName: "",
          email: "",
          phone: "",
          linkedinUrl: "",
          githubUrl: "",
          portfolioUrl: "",
          currentCompany: "",
          yearsExperience: "",
          workAuthorization: "",
          sponsorshipRequired: "",
          resumeSummary: ""
        }
      });
    }
  }
});

// Centralized message router for side panel, content scripts, and popups
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  (async () => {
    try {
      if (message.type === "GET_ACTIVE_TAB") {
        const [activeTab] = await chrome.tabs.query({ active: true, currentWindow: true });
        sendResponse({ success: true, tab: activeTab });
      } else if (message.type === "RELAY_TO_ACTIVE_TAB") {
        const [activeTab] = await chrome.tabs.query({ active: true, currentWindow: true });
        if (!activeTab || !activeTab.id) {
          sendResponse({ success: false, error: "No active tab found" });
          return;
        }
        const response = await chrome.tabs.sendMessage(activeTab.id, message.payload);
        sendResponse({ success: true, data: response });
      } else if (message.type === "UPDATE_BADGE") {
        if (message.text) {
          await chrome.action.setBadgeText({ text: message.text });
          if (message.color) {
            await chrome.action.setBadgeBackgroundColor({ color: message.color });
          }
        } else {
          await chrome.action.setBadgeText({ text: "" });
        }
        sendResponse({ success: true });
      } else if (message.type === "OPEN_APPLY") {
        sendResponse(await openApplyTabs(message.payload || {}));
      } else if (message.type === "GET_APPLY_DATA") {
        const tabId = sender.tab && sender.tab.id;
        if (!tabId) {
          sendResponse({ success: false, error: "No tab id" });
          return;
        }
        const key = `apply_${tabId}`;
        const stored = await chrome.storage.session.get(key);
        sendResponse({ success: true, data: stored[key] || null });
      } else if (message.type === "APPLY_SUBMITTED") {
        sendResponse(await relaySubmission(message.jobId));
      } else {
        sendResponse({ success: false, error: `Unknown message type: ${message.type}` });
      }
    } catch (err) {
      console.error("Service worker message error:", err);
      sendResponse({ success: false, error: err.message });
    }
  })();
  return true; // Keep message channel open for async response
});

// -------------------------------------------------------------
// Assisted apply: open the user's own browser instead of a server-side one
// -------------------------------------------------------------

const DASHBOARD_URLS = ["http://localhost:8000/*", "http://127.0.0.1:8000/*"];

async function stashApplyData(tabId, job) {
  if (!tabId) return;
  await chrome.storage.session.set({ [`apply_${tabId}`]: job });
}

/**
 * Open apply tabs in the browser the user is already working in.
 * A single job opens one tab; a batch opens ONE window with the tabs queued in
 * sequence so the user works through them one at a time.
 */
async function openApplyTabs(payload) {
  const jobs = (payload.jobs || []).filter((j) => j && j.url);
  if (!jobs.length) {
    return { ok: false, error: "No application URLs provided" };
  }

  if (jobs.length === 1) {
    const tab = await chrome.tabs.create({ url: jobs[0].url, active: true });
    await stashApplyData(tab.id, jobs[0]);
    return { ok: true, opened: 1 };
  }

  const win = await chrome.windows.create({ url: jobs[0].url, focused: true });
  const firstTab = win.tabs && win.tabs[0];
  await stashApplyData(firstTab && firstTab.id, jobs[0]);

  for (let i = 1; i < jobs.length; i++) {
    const tab = await chrome.tabs.create({
      windowId: win.id,
      url: jobs[i].url,
      active: false
    });
    await stashApplyData(tab.id, jobs[i]);
  }
  return { ok: true, opened: jobs.length };
}

/**
 * "Mark as submitted" happens on the ATS page, which is cross-origin to the API.
 * Relay it to the dashboard tab so the API call is made same-origin.
 */
async function relaySubmission(jobId) {
  const tabs = await chrome.tabs.query({ url: DASHBOARD_URLS });
  if (!tabs.length) {
    return { ok: false, error: "Dashboard tab is not open" };
  }
  for (const tab of tabs) {
    try {
      await chrome.tabs.sendMessage(tab.id, { type: "APPLY_SUBMITTED", jobId });
      return { ok: true };
    } catch (err) {
      // Try the next dashboard tab.
    }
  }
  return { ok: false, error: "Could not reach the dashboard tab" };
}

