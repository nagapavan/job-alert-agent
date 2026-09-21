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
