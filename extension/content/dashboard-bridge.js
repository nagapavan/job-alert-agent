/**
 * Dashboard Bridge (content script on the local SPA)
 *
 * The SPA cannot message the extension directly without knowing its ID, so it talks to
 * this bridge over window.postMessage. The bridge forwards to the service worker and
 * posts the result back. When this script is absent (extension not installed) the SPA
 * falls back to opening the tab itself.
 */

(() => {
  if (window.__jobAgentDashboardBridge) return;
  window.__jobAgentDashboardBridge = true;

  const PAGE_SOURCE = "job-alert-agent";
  const EXT_SOURCE = "job-alert-agent-extension";

  window.addEventListener("message", (event) => {
    if (event.source !== window) return;
    const data = event.data;
    if (!data || data.source !== PAGE_SOURCE) return;

    if (data.type === "OPEN_APPLY") {
      chrome.runtime.sendMessage(
        { type: "OPEN_APPLY", payload: data.payload || {} },
        (resp) => {
          const err = chrome.runtime.lastError;
          window.postMessage(
            {
              source: EXT_SOURCE,
              type: "OPEN_APPLY_RESULT",
              requestId: data.requestId,
              ok: !err && Boolean(resp && resp.ok),
              opened: (resp && resp.opened) || 0,
              error: (err && err.message) || (resp && resp.error) || null
            },
            "*"
          );
        }
      );
    } else if (data.type === "PING_EXTENSION") {
      window.postMessage({ source: EXT_SOURCE, type: "EXTENSION_READY" }, "*");
    }
  });

  // Let the SPA prefer the extension path from the first paint.
  window.postMessage({ source: EXT_SOURCE, type: "EXTENSION_READY" }, "*");

  // Submission is confirmed on the ATS page; relay it back to the SPA so the API call
  // is made same-origin (the ATS origin is not in the backend CORS allowlist).
  chrome.runtime.onMessage.addListener((message) => {
    if (message && message.type === "APPLY_SUBMITTED") {
      window.postMessage(
        {
          source: EXT_SOURCE,
          type: "APPLY_SUBMITTED",
          jobId: message.jobId
        },
        "*"
      );
    }
  });
})();
