/**
 * Backend origin defaults, shared by the service worker and the side panel.
 *
 * The manifest's host_permissions must stay literal — Chrome does not expand variables there — so
 * this file is the single source for every script. The live value still comes from
 * chrome.storage.local.backendUrl, which the side panel writes when the user edits the field.
 */

const DEFAULT_BACKEND_URL = "http://localhost:8000";

// Loopback origins the dashboard may be served from; the second covers 127.0.0.1 aliases.
const DASHBOARD_ORIGINS = ["http://localhost:8000", "http://127.0.0.1:8000"];
const DASHBOARD_URLS = DASHBOARD_ORIGINS.map((origin) => `${origin}/*`);
