/**
 * In-DOM ATS Scraper & 1-Click Form Autofiller (Content Script)
 * Supports Greenhouse, Lever, Ashby, Workday & custom portals (declarative injection),
 * plus LinkedIn (gesture-only: injected on-demand by the side panel under activeTab).
 *
 * Safety invariants — do NOT break these:
 *   - Runs in the isolated content-script world only (never MAIN world / never page context).
 *   - Never reads cookies / session tokens and never calls platform internal APIs.
 *   - Autofill is strictly user-initiated (explicit button click or side-panel message);
 *     the SPA DOM observer only refreshes the badge, never fills or submits forms.
 */

(function () {
  // Prevent duplicate execution
  if (window.__jobAgentContentScriptInjected) return;
  window.__jobAgentContentScriptInjected = true;

  // -------------------------------------------------------------
  // 1. Context & Security Gating (OAuth, Auth & Non-Application Filters)
  // -------------------------------------------------------------

  function isBlockedContext() {
    // 1. Check window dimensions (Small popup windows for OAuth / SSO / Captcha)
    if (window.innerWidth > 0 && window.innerHeight > 0) {
      if (window.innerWidth < 640 && window.innerHeight < 600) {
        return true;
      }
    }

    // 2. Check if running inside cross-origin or auth iframe
    try {
      if (window.self !== window.top) {
        const host = window.location.hostname.toLowerCase();
        // Allow only iframes on known primary ATS domains
        if (!host.includes("greenhouse.io") && !host.includes("lever.co") && !host.includes("ashbyhq.com")) {
          return true;
        }
      }
    } catch (e) {
      return true; // Sandboxed / cross-origin iframe
    }

    // 3. Strict exclusion for OAuth, Authentication, Login, Signup & Checkpoint URLs
    const url = window.location.href.toLowerCase();
    const pathname = window.location.pathname.toLowerCase();
    const blockedKeywords = [
      "/oauth", "/checkpoint", "/uas/login", "/login", "/signup", "/sign-in",
      "/auth", "/mfa", "/challenge", "/identity", "/saml", "/sso", "/verify",
      "accounts.google", "appleid.apple", "github.com/login/oauth"
    ];
    if (blockedKeywords.some(kw => pathname.includes(kw) || url.includes(kw))) {
      return true;
    }

    // 4. LinkedIn non-job pages (Feed, Messages, Profile, Network, Notifications)
    if (window.location.hostname.includes("linkedin.com")) {
      const nonJobPaths = [
        "/feed", "/messaging", "/mynetwork", "/notifications", "/in/",
        "/company/", "/pulse", "/learning", "/settings", "/premium"
      ];
      if (nonJobPaths.some(p => pathname.startsWith(p))) {
        return true;
      }
    }

    return false;
  }

  function isJobApplicationContext() {
    if (isBlockedContext()) return false;

    const host = window.location.hostname.toLowerCase();

    // A. LinkedIn Specific Gating
    if (host.includes("linkedin.com")) {
      // ONLY inject when on an active Easy Apply modal or apply form
      const easyApplyModal = document.querySelector(
        ".jobs-easy-apply-modal, .jobs-easy-apply-content, [data-easy-apply-modal], .jobs-apply-form, .jobs-easy-apply-form"
      );
      if (easyApplyModal) {
        const inputs = easyApplyModal.querySelectorAll("input:not([type='hidden']), select, textarea");
        return inputs.length > 0;
      }
      return false;
    }

    // B. Standard ATS Forms (Greenhouse, Lever, Ashby, Workday)
    const hasAtsForm = document.querySelector(
      "#app_form, #application_form, .application-form, [data-automation-id='applicationPage'], form[action*='apply'], [class*='ashby'] form, form#job-application-form"
    );
    if (hasAtsForm) {
      const inputs = hasAtsForm.querySelectorAll("input:not([type='hidden']):not([type='search']), textarea, select");
      if (inputs.length >= 2) return true;
    }

    // C. Generic ATS / Portal Application Page Detection
    const inputs = Array.from(document.querySelectorAll("input:not([type='hidden']):not([type='search']):not([type='submit']), textarea"));
    let applicationFieldMatches = 0;
    for (const el of inputs) {
      const idName = `${el.id} ${el.name} ${el.placeholder || ""} ${el.getAttribute("aria-label") || ""}`.toLowerCase();
      if (/resume|cv|first.?name|last.?name|email|phone|mobile|cover.?letter|linkedin|github|portfolio|sponsorship|authorized/i.test(idName)) {
        applicationFieldMatches++;
      }
      if (el.type === "file") applicationFieldMatches += 2;
    }

    return applicationFieldMatches >= 3;
  }

  // -------------------------------------------------------------
  // 2. Page Metadata & ATS Detection
  // -------------------------------------------------------------

  function detectAtsType() {
    const host = window.location.hostname.toLowerCase();

    if (host.includes("greenhouse.io") || document.querySelector("#app_form, #application_form, .greenhouse-job-board")) {
      return "greenhouse";
    }
    if (host.includes("lever.co") || document.querySelector(".application-form, .posting-headline")) {
      return "lever";
    }
    if (host.includes("ashbyhq.com") || document.querySelector("[class*='ashby']")) {
      return "ashby";
    }
    if (host.includes("myworkdayjobs.com") || document.querySelector("[data-automation-id='applicationPage']")) {
      return "workday";
    }
    if (host.includes("linkedin.com")) {
      return "linkedin";
    }
    return "custom";
  }

  function extractJobMetadata() {
    let title = "";
    let company = "";
    let description = "";

    const ats = detectAtsType();

    // 1. Title Extraction
    const titleSelectors = [
      "h1.app-title",
      "h1.job-title",
      ".posting-headline h2",
      "[data-automation-id='jobTitle']",
      "h1.top-card-layout__title",
      ".job-details-jobs-unified-top-card__job-title",
      "h1"
    ];
    for (const sel of titleSelectors) {
      const el = document.querySelector(sel);
      if (el && el.innerText.trim()) {
        title = el.innerText.trim();
        break;
      }
    }

    // 2. Company Extraction
    const isGenericCompanyName = (str) => {
      if (!str || typeof str !== "string") return true;
      const clean = str.trim().toLowerCase();
      return (
        !clean ||
        clean.length < 2 ||
        /^(job|jobs|career|careers|job boards?|boards?|greenhouse|greenhouse job board|lever|lever jobs|ashby|ashbyhq|workday|workday jobs|smartrecruiters|target company|company|posting|job posting|application|hiring|apply)$/i.test(clean)
      );
    };

    const companySelectors = [
      ".company-name",
      ".posting-category",
      "span.topcard__flavor--black-link",
      ".job-details-jobs-unified-top-card__company-name",
      "[data-automation-id='companyName']",
      "meta[property='og:site_name']"
    ];
    for (const sel of companySelectors) {
      const el = document.querySelector(sel);
      if (el) {
        let extracted = "";
        if (el.tagName === "META") {
          extracted = el.getAttribute("content") || "";
        } else if (el.innerText.trim()) {
          extracted = el.innerText.trim();
        }
        if (extracted && !isGenericCompanyName(extracted)) {
          company = extracted;
          break;
        }
      }
    }

    // Fallback: derive company from ATS URL pathname or hostname
    if (!company || isGenericCompanyName(company)) {
      const host = window.location.hostname.toLowerCase();
      const pathParts = window.location.pathname.split("/").filter(Boolean);

      if ((host.includes("greenhouse.io") || host.includes("lever.co") || host.includes("ashbyhq.com") || host.includes("smartrecruiters.com")) && pathParts.length > 0) {
        // e.g. boards.greenhouse.io/yipitdata/jobs/123 -> yipitdata
        // e.g. jobs.lever.co/stripe/123 -> stripe
        // e.g. jobs.ashbyhq.com/figma/123 -> figma
        const slug = pathParts[0].replace(/[-_]+/g, " ").trim();
        if (slug && !isGenericCompanyName(slug)) {
          company = slug.charAt(0).toUpperCase() + slug.slice(1);
        }
      } else if (host.includes("myworkdayjobs.com")) {
        // e.g. nvidia.wd5.myworkdayjobs.com -> nvidia
        const sub = host.split(".")[0].replace(/[-_]+/g, " ").trim();
        if (sub && !isGenericCompanyName(sub)) {
          company = sub.charAt(0).toUpperCase() + sub.slice(1);
        }
      } else {
        const parts = host.replace(/^www\./, "").split(".");
        if (parts.length >= 2) {
          const rootDomain = parts[0].replace(/jobs|careers|-/g, " ").trim();
          if (rootDomain && !isGenericCompanyName(rootDomain)) {
            company = rootDomain.charAt(0).toUpperCase() + rootDomain.slice(1);
          }
        }
      }
    }

    // 3. Description Extraction
    const descSelectors = [
      "#content",
      "#job-description",
      ".job-description",
      ".posting-description",
      ".section-wrapper",
      "[data-automation-id='jobPostingDescription']",
      ".jobs-description__content",
      ".show-more-less-html__markup"
    ];
    for (const sel of descSelectors) {
      const el = document.querySelector(sel);
      if (el && el.innerText.trim().length > 100) {
        description = el.innerText.trim();
        break;
      }
    }

    if (!description) {
      description = document.body ? document.body.innerText.slice(0, 4000) : "";
    }

    return {
      title: title || document.title || "Target Position",
      company: company || "Target Company",
      description: description,
      url: window.location.href,
      atsType: ats
    };
  }

  // -------------------------------------------------------------
  // 3. Form Autofill & Telemetry Logic
  // -------------------------------------------------------------

  function setElementValue(element, value, overwrite = false) {
    if (!element || value === undefined || value === null) return false;

    // Safe Mode: preserve pre-filled data if overwrite is false
    if (!overwrite) {
      const currentVal = (element.value || "").trim();
      if (currentVal.length > 0) {
        return false;
      }
    }

    element.focus();
    element.value = value;
    element.dispatchEvent(new Event("input", { bubbles: true }));
    element.dispatchEvent(new Event("change", { bubbles: true }));
    element.blur();

    element.classList.add("job-agent-autofilled-field");
    return true;
  }

  function isIgnoredOrHiddenField(input) {
    if (!input || !input.tagName) return true;
    
    // A. Type exclusion
    const type = (input.type || input.getAttribute("type") || "").toLowerCase();
    if (type === "hidden" || type === "submit" || type === "button" || type === "reset" || type === "image" || type === "password") {
      return true;
    }

    // B. Collect all identifying strings & attributes
    const id = String(input.id || input.getAttribute("id") || "").toLowerCase();
    const name = String(input.name || input.getAttribute("name") || "").toLowerCase();
    let cls = "";
    try {
      cls = (typeof input.className === "string" ? input.className : (input.getAttribute("class") || "")).toLowerCase();
    } catch (e) {}
    const ariaLabel = String(input.getAttribute("aria-label") || "").toLowerCase();
    const placeholder = String(input.placeholder || input.getAttribute("placeholder") || "").toLowerCase();
    const autocomplete = String(input.getAttribute("autocomplete") || "").toLowerCase();
    const combinedAttrs = `${id} ${name} ${cls} ${ariaLabel} ${placeholder} ${autocomplete}`;

    // C. Strict Captcha, Bot, Token, Honeypot, Security & Framework identifiers
    const securityRegex = /captcha|recaptcha|g-recaptcha|grecaptcha|hcaptcha|h-captcha|turnstile|cf-turnstile|cf-chl|challenge|csrf|xsrf|_token|authenticity_token|_wpnonce|honeypot|bot.?check|arkose|funcaptcha|threatmetrix|akamai|perimeterx|datadome/i;
    if (securityRegex.test(combinedAttrs)) {
      return true;
    }

    // D. Check for Captcha tokens or JWT in value (long continuous hash/token)
    const val = String(input.value || "").trim();
    if (val.length > 40 && !val.includes(" ") && !val.includes("@") && !val.includes("http")) {
      return true;
    }

    // E. Visually hidden / HTML5 hidden / aria-hidden
    if (input.hidden || input.hasAttribute("hidden") || input.getAttribute("aria-hidden") === "true") {
      return true;
    }

    // F. Container / Ancestor check
    try {
      if (input.closest("[hidden], [aria-hidden='true'], .grecaptcha-badge, .g-recaptcha, [class*='captcha'], [id*='captcha'], [class*='recaptcha'], [id*='recaptcha'], [class*='turnstile'], [id*='turnstile'], [class*='hcaptcha'], [id*='hcaptcha'], iframe[src*='recaptcha'], iframe[src*='turnstile'], iframe[src*='hcaptcha'], [style*='display: none'], [style*='display:none']")) {
        return true;
      }
    } catch (e) {}

    // G. Inline style & Computed Geometry check
    if (input.style.display === "none" || input.style.visibility === "hidden" || input.style.opacity === "0") {
      return true;
    }

    try {
      const style = window.getComputedStyle(input);
      if (
        style.display === "none" || 
        style.visibility === "hidden" || 
        style.opacity === "0" || 
        style.clip === "rect(0px, 0px, 0px, 0px)" ||
        (style.position === "absolute" && (parseInt(style.left, 10) < -1000 || parseInt(style.top, 10) < -1000))
      ) {
        return true;
      }
      
      const rect = input.getBoundingClientRect();
      if (rect.width === 0 && rect.height === 0) {
        return true;
      }
    } catch (e) {}

    return false;
  }

  function getFieldCleanLabel(input) {
    if (!input) return "";
    let labelText = "";

    // 1. Associated explicit label[for="..."]
    if (input.id) {
      try {
        const lbl = document.querySelector(`label[for="${CSS.escape(input.id)}"]`);
        if (lbl && lbl.innerText.trim()) {
          labelText = lbl.innerText.trim();
        }
      } catch (e) {}
    }

    // 2. Parent wrapping label
    if (!labelText) {
      const parentLbl = input.closest("label");
      if (parentLbl && parentLbl.innerText.trim()) {
        labelText = parentLbl.innerText.trim();
      }
    }

    // 3. Container label / legend / heading
    if (!labelText) {
      const container = input.closest(".form-group, .field, .form-field, .input-wrapper, fieldset, [data-automation-id], div");
      if (container) {
        const lblEl = container.querySelector("label, .label, .form-label, legend, h3, h4, h5, [class*='label'], [class*='question']");
        if (lblEl && lblEl !== input && !lblEl.contains(input) && lblEl.innerText.trim()) {
          labelText = lblEl.innerText.trim();
        }
      }
    }

    // 4. aria-label or placeholder
    if (!labelText) {
      const aria = (input.getAttribute("aria-label") || "").trim();
      if (aria) labelText = aria;
    }
    if (!labelText) {
      const placeholder = (input.placeholder || "").trim();
      if (placeholder && placeholder.length > 5) labelText = placeholder;
    }

    // 5. Fallback: humanize name/id only if not a system/UUID/captcha identifier
    if (!labelText) {
      const raw = (input.name || input.id || "").trim();
      if (raw && !/^(text|input|field|custom|question_\d+|[0-9a-f-]+|g-recaptcha.*|.*captcha.*)$/i.test(raw)) {
        labelText = raw.replace(/[_-]+/g, " ").replace(/([a-z])([A-Z])/g, "$1 $2").trim();
      }
    }

    // Clean asterisks, required notes, type annotations, and whitespace
    let cleaned = (labelText || "")
      .replace(/[\*\†\‡]/g, "")
      .replace(/\s*\(optional\)\s*/gi, "")
      .replace(/\s*\(required\)\s*/gi, "")
      .replace(/\s*required\s*$/gi, "")
      .replace(/\btype\s+h\b/gi, "")
      .replace(/\btype\s+hidden\b/gi, "")
      .replace(/\s+/g, " ")
      .trim();

    // Discard if cleaned label is just a raw UUID, hex string, or captcha name
    if (/^[0-9a-f-]{16,}$/i.test(cleaned) || /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i.test(cleaned)) {
      return "";
    }
    if (/captcha|recaptcha|turnstile|hcaptcha|csrf|_token/i.test(cleaned)) {
      return "";
    }

    return cleaned.slice(0, 300);
  }

  function getFieldIdentifier(input) {
    if (!input) return "";
    const id = (input.id || "").toLowerCase();
    const name = (input.name || "").toLowerCase();
    const placeholder = (input.placeholder || "").toLowerCase();
    const ariaLabel = (input.getAttribute("aria-label") || "").toLowerCase();
    const dataAutomationId = (input.getAttribute("data-automation-id") || "").toLowerCase();
    const cleanLabel = (getFieldCleanLabel(input) || "").toLowerCase();
    
    return `${id} ${name} ${placeholder} ${ariaLabel} ${dataAutomationId} ${cleanLabel}`.trim();
  }

  function selectDropdownOption(selectEl, query, overwrite = false) {
    if (!selectEl || !selectEl.options || selectEl.options.length === 0 || !query) return false;

    // Safe Mode: preserve existing selected option if overwrite is false
    if (!overwrite) {
      if (selectEl.selectedIndex > 0 && selectEl.value && selectEl.value.trim() !== "") {
        return false;
      }
    }

    const q = String(query).toLowerCase().trim();
    for (let i = 0; i < selectEl.options.length; i++) {
      const opt = selectEl.options[i];
      const optText = (opt.text || "").toLowerCase().trim();
      const optVal = (opt.value || "").toLowerCase().trim();
      if (optVal === q || optText === q || optVal.includes(q) || optText.includes(q)) {
        selectEl.selectedIndex = i;
        selectEl.value = opt.value;
        selectEl.dispatchEvent(new Event("input", { bubbles: true }));
        selectEl.dispatchEvent(new Event("change", { bubbles: true }));
        selectEl.classList.add("job-agent-autofilled-field");
        return true;
      }
    }
    return false;
  }

  function extractYear(str) {
    if (!str) return "";
    const match = String(str).match(/\b(19\d\d|20\d\d)\b/);
    return match ? match[1] : "";
  }

  const MONTH_NAMES = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"];
  const MONTH_ABBRS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"];

  function extractMonth(str) {
    if (!str) return "";
    const s = String(str).toLowerCase();
    for (let i = 0; i < MONTH_NAMES.length; i++) {
      if (s.includes(MONTH_NAMES[i]) || s.includes(MONTH_ABBRS[i])) {
        return MONTH_NAMES[i].charAt(0).toUpperCase() + MONTH_NAMES[i].slice(1);
      }
    }
    const numMatch = s.match(/\b(0?[1-9]|1[0-2])\b/);
    if (numMatch) {
      const idx = parseInt(numMatch[1], 10) - 1;
      return MONTH_NAMES[idx].charAt(0).toUpperCase() + MONTH_NAMES[idx].slice(1);
    }
    return "";
  }

  function inspectFormTelemetry() {
    const inputs = Array.from(document.querySelectorAll("input, textarea, select"))
      .filter(input => !isIgnoredOrHiddenField(input));
    const fields = [];
    const detectedQuestions = [];

    inputs.forEach((input) => {
      const id = (input.id || "").trim();
      const name = (input.name || "").trim();
      const placeholder = (input.placeholder || "").trim();
      const fieldType = (input.tagName === "TEXTAREA" ? "textarea" : input.tagName === "SELECT" ? "select" : (input.type || "text")).toLowerCase();

      const combinedText = getFieldIdentifier(input);
      const cleanLabel = getFieldCleanLabel(input) || name || id;
      let isRecognized = false;
      let category = "unknown";

      if (/first.?name|given.?name/i.test(combinedText)) { isRecognized = true; category = "first_name"; }
      else if (/last.?name|family.?name|surname/i.test(combinedText)) { isRecognized = true; category = "last_name"; }
      else if (/full.?name|candidate.?name/i.test(combinedText)) { isRecognized = true; category = "full_name"; }
      else if (/email/i.test(combinedText)) { isRecognized = true; category = "email"; }
      else if (/phone|mobile|tel/i.test(combinedText)) { isRecognized = true; category = "phone"; }
      else if (/location|city|address|based/i.test(combinedText)) { isRecognized = true; category = "location"; }
      else if (/linkedin/i.test(combinedText)) { isRecognized = true; category = "linkedin"; }
      else if (/github/i.test(combinedText)) { isRecognized = true; category = "github"; }
      else if (/portfolio|website/i.test(combinedText)) { isRecognized = true; category = "portfolio"; }
      else if (/resume|cv/i.test(combinedText)) { isRecognized = true; category = "resume"; }
      else if (/authorized|sponsorship|visa/i.test(combinedText)) { isRecognized = true; category = "work_auth"; }
      else if (/salary|compensation/i.test(combinedText)) { isRecognized = true; category = "salary"; }
      else if (/why|fit|interest|motivation/i.test(combinedText)) { isRecognized = true; category = "why_us"; }
      else if (/about.?you|tell.?me|summary/i.test(combinedText)) { isRecognized = true; category = "about_you"; }
      else if (/company|employer/i.test(combinedText)) { isRecognized = true; category = "company"; }
      else if (/title|role|position/i.test(combinedText)) { isRecognized = true; category = "job_title"; }

      const descriptor = {
        field_name: name || id || cleanLabel.slice(0, 50),
        field_id: id || name || "",
        field_label: cleanLabel,
        field_type: fieldType,
        is_recognized: isRecognized,
        suggested_category: category
      };

      if (descriptor.field_label || descriptor.field_id || descriptor.field_name) {
        fields.push(descriptor);
      }

      // Collect essay / screener questions (long textareas or explicit subjective questions)
      if (fieldType === "textarea" || /why|fit|interest|motivation|describe|tell.?me|challenge|rank.?top|looking.?for|skill.?set/i.test(combinedText)) {
        const isStandardContact = /location|city|address|phone|email|first.?name|last.?name|linkedin|github|portfolio|resume|work_auth/i.test(combinedText);
        const isSecurityToken = /recaptcha|captcha|turnstile|hcaptcha|csrf|token|challenge|honeypot|bot/i.test(`${id} ${name} ${cleanLabel}`);
        const isRawUuid = /^[0-9a-f-]{16,}$/i.test(cleanLabel.trim()) || /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i.test(cleanLabel.trim()) || /type\s+h/i.test(cleanLabel);
        const hasCaptchaValue = (input.value || "").length > 40 && !(input.value || "").includes(" ");

        if (!isStandardContact && !isSecurityToken && !isRawUuid && !hasCaptchaValue && cleanLabel && cleanLabel.replace(/[^a-zA-Z]/g, "").length >= 3) {
          detectedQuestions.push({
            id: id || name,
            question: cleanLabel,
            category: category,
            element_type: fieldType,
            current_value: input.value || ""
          });
        }
      }
    });

    return {
      domain: window.location.hostname,
      ats_type: detectAtsType(),
      url: window.location.href,
      total_fields: fields.length,
      recognized_count: fields.filter(f => f.is_recognized).length,
      unrecognized_count: fields.filter(f => !f.is_recognized).length,
      fields: fields,
      detected_questions: detectedQuestions
    };
  }

  function autofillWorkExperience(experienceList, handledElements, overwrite = false) {
    if (!Array.isArray(experienceList) || experienceList.length === 0) return 0;
    let filledCount = 0;

    // 1. Locate repeating experience block containers
    const containerSelectors = [
      '[data-automation-id*="workExperience"]',
      '[data-automation-id*="experience-"]',
      '[class*="experience-card"]',
      '[class*="experience-item"]',
      '[class*="experience-entry"]',
      '[class*="experience-section"]',
      '[class*="work-experience"]',
      '[class*="work_experience"]',
      '[class*="employment-history"]',
      '[class*="work-history"]',
      '[class*="job-history"]',
      '[class*="position-item"]',
      '[class*="job-entry"]',
      'fieldset[class*="experience"]',
      'fieldset[class*="work"]',
      'fieldset[class*="employment"]'
    ];

    let containers = Array.from(document.querySelectorAll(containerSelectors.join(", ")))
      .filter(el => el.querySelectorAll("input, textarea, select").length > 0);

    // Fallback: Group by repeating distinct ancestor containers for company inputs
    if (containers.length <= 1) {
      const allCompanyInputs = Array.from(document.querySelectorAll("input:not([type='hidden']):not([type='submit']):not([type='button'])"))
        .filter(input => {
          const idText = getFieldIdentifier(input);
          return /(?:^|\b)(?:company|employer|organization|company.?name)(?:\b|$)/i.test(idText) && !/why|summary|current.?salary/i.test(idText);
        });

      if (allCompanyInputs.length >= 2) {
        const structuralContainers = [];
        const seen = new Set();
        for (const input of allCompanyInputs) {
          const block = input.closest("fieldset, [class*='card'], [class*='row'], [class*='section'], [class*='group'], form > div, li");
          if (block && !seen.has(block)) {
            seen.add(block);
            structuralContainers.push(block);
          }
        }
        if (structuralContainers.length >= 2) {
          containers = structuralContainers;
        }
      }
    }

    // Process each container with corresponding experience entry
    if (containers.length > 0) {
      containers.forEach((container, idx) => {
        if (idx >= experienceList.length) return;
        const exp = experienceList[idx];
        if (!exp) return;

        const inputs = Array.from(container.querySelectorAll("input, textarea, select"));
        inputs.forEach(input => {
          if (handledElements.has(input)) return;
          if (input.type === "hidden" || input.type === "submit" || input.type === "button") return;

          const idText = getFieldIdentifier(input);

          // A. Company Name
          if (/(?:^|\b)(?:company|employer|organization|company.?name|organization.?name)(?:\b|$)/i.test(idText) && !/why|summary|salary/i.test(idText)) {
            const comp = exp.company || exp.employer || exp.organization || "";
            if (comp) {
              if (setElementValue(input, comp, overwrite)) filledCount++;
              handledElements.add(input);
            }
          }
          // B. Title / Role / Position
          else if (/(?:^|\b)(?:job.?title|title|role|position|designation|occupation)(?:\b|$)/i.test(idText) && !/target|applied|salary/i.test(idText)) {
            const title = exp.title || exp.role || exp.position || "";
            if (title) {
              if (setElementValue(input, title, overwrite)) filledCount++;
              handledElements.add(input);
            }
          }
          // C. Start Date / Month / Year
          else if (/start.?date|from.?date|begin.?date/i.test(idText) || (/(?:start|from|begin)/i.test(idText) && /date/i.test(idText))) {
            const sDate = exp.start_date || exp.startDate || (exp.duration ? exp.duration.split(/[-–—to]/)[0].trim() : "");
            if (sDate) {
              if (setElementValue(input, sDate, overwrite)) filledCount++;
              handledElements.add(input);
            }
          }
          else if (/start.?month|from.?month/i.test(idText)) {
            const sDate = exp.start_date || exp.startDate || (exp.duration ? exp.duration.split(/[-–—to]/)[0].trim() : "");
            const m = extractMonth(sDate);
            if (m) {
              if (input.tagName === "SELECT") {
                if (selectDropdownOption(input, m, overwrite)) filledCount++;
              } else {
                if (setElementValue(input, m, overwrite)) filledCount++;
              }
              handledElements.add(input);
            }
          }
          else if (/start.?year|from.?year/i.test(idText)) {
            const sDate = exp.start_date || exp.startDate || (exp.duration ? exp.duration.split(/[-–—to]/)[0].trim() : "");
            const y = extractYear(sDate);
            if (y) {
              if (input.tagName === "SELECT") {
                if (selectDropdownOption(input, y, overwrite)) filledCount++;
              } else {
                if (setElementValue(input, y, overwrite)) filledCount++;
              }
              handledElements.add(input);
            }
          }
          // D. End Date / Month / Year / "Currently working here"
          else if (/current.?role|currently.?work|present.?role|is.?current/i.test(idText) && (input.type === "checkbox" || input.type === "radio")) {
            const isCurrent = exp.is_current || /present|current/i.test(exp.end_date || exp.endDate || exp.duration || "");
            if (isCurrent) {
              if (overwrite || !input.checked) {
                input.checked = true;
                input.dispatchEvent(new Event("change", { bubbles: true }));
                filledCount++;
              }
              handledElements.add(input);
            }
          }
          else if (/end.?date|to.?date|until.?date|finish.?date/i.test(idText) || (/(?:end|to|until)/i.test(idText) && /date/i.test(idText))) {
            const eDate = exp.end_date || exp.endDate || (exp.duration && exp.duration.includes("-") ? exp.duration.split(/[-–—to]/)[1].trim() : "");
            if (eDate && !/present|current/i.test(eDate)) {
              if (setElementValue(input, eDate, overwrite)) filledCount++;
              handledElements.add(input);
            }
          }
          else if (/end.?month|to.?month/i.test(idText)) {
            const eDate = exp.end_date || exp.endDate || (exp.duration && exp.duration.includes("-") ? exp.duration.split(/[-–—to]/)[1].trim() : "");
            if (eDate && !/present|current/i.test(eDate)) {
              const m = extractMonth(eDate);
              if (m) {
                if (input.tagName === "SELECT") {
                  if (selectDropdownOption(input, m, overwrite)) filledCount++;
                } else {
                  if (setElementValue(input, m, overwrite)) filledCount++;
                }
                handledElements.add(input);
              }
            }
          }
          else if (/end.?year|to.?year/i.test(idText)) {
            const eDate = exp.end_date || exp.endDate || (exp.duration && exp.duration.includes("-") ? exp.duration.split(/[-–—to]/)[1].trim() : "");
            if (eDate && !/present|current/i.test(eDate)) {
              const y = extractYear(eDate);
              if (y) {
                if (input.tagName === "SELECT") {
                  if (selectDropdownOption(input, y, overwrite)) filledCount++;
                } else {
                  if (setElementValue(input, y, overwrite)) filledCount++;
                }
                handledElements.add(input);
              }
            }
          }
          // E. Location
          else if (/(?:^|\b)(?:location|city|work.?location)(?:\b|$)/i.test(idText) && !/relocat|current.?address/i.test(idText)) {
            if (exp.location) {
              if (setElementValue(input, exp.location, overwrite)) filledCount++;
              handledElements.add(input);
            }
          }
          // F. Description / Summary / Details
          else if (/(?:description|responsibilities|summary|achievements|duties|bullet|details)/i.test(idText) && (input.tagName === "TEXTAREA" || input.type === "text")) {
            const desc = exp.description || exp.details || exp.summary || (Array.isArray(exp.highlights) ? exp.highlights.join("\n") : "");
            if (desc) {
              if (setElementValue(input, desc, overwrite)) filledCount++;
              handledElements.add(input);
            }
          }
          // G. Skills
          else if (/(?:skills|technologies|tools|keywords)/i.test(idText)) {
            const skl = Array.isArray(exp.skills) ? exp.skills.join(", ") : (exp.skills || "");
            if (skl) {
              if (setElementValue(input, skl, overwrite)) filledCount++;
              handledElements.add(input);
            }
          }
        });
      });
    }

    // Fallback: Sibling / Sequential mapping for remaining unhandled company and title inputs
    const unhandledCompanyInputs = Array.from(document.querySelectorAll("input:not([type='hidden']):not([type='submit']):not([type='button'])"))
      .filter(input => {
        if (handledElements.has(input)) return false;
        const idText = getFieldIdentifier(input);
        return /(?:^|\b)(?:company|employer|organization|company.?name)(?:\b|$)/i.test(idText) && !/why|summary|current.?salary/i.test(idText);
      });

    if (unhandledCompanyInputs.length >= 2) {
      unhandledCompanyInputs.forEach((input, idx) => {
        if (idx < experienceList.length && experienceList[idx].company) {
          if (setElementValue(input, experienceList[idx].company, overwrite)) filledCount++;
          handledElements.add(input);
        }
      });
    }

    const unhandledTitleInputs = Array.from(document.querySelectorAll("input:not([type='hidden']):not([type='submit']):not([type='button'])"))
      .filter(input => {
        if (handledElements.has(input)) return false;
        const idText = getFieldIdentifier(input);
        return /(?:^|\b)(?:job.?title|title|role|position|designation)(?:\b|$)/i.test(idText) && !/target|applied|current.?salary/i.test(idText);
      });

    if (unhandledTitleInputs.length >= 2) {
      unhandledTitleInputs.forEach((input, idx) => {
        if (idx < experienceList.length && experienceList[idx].title) {
          if (setElementValue(input, experienceList[idx].title, overwrite)) filledCount++;
          handledElements.add(input);
        }
      });
    }

    return filledCount;
  }

  function autofillEducation(educationList, handledElements, overwrite = false) {
    if (!Array.isArray(educationList) || educationList.length === 0) return 0;
    let filledCount = 0;

    const eduContainerSelectors = [
      '[data-automation-id*="education"]',
      '[class*="education-card"]',
      '[class*="education-item"]',
      '[class*="education-entry"]',
      '[class*="school-item"]',
      'fieldset[class*="education"]',
      'fieldset[class*="school"]'
    ];

    let containers = Array.from(document.querySelectorAll(eduContainerSelectors.join(", ")))
      .filter(el => el.querySelectorAll("input, textarea, select").length > 0);

    if (containers.length <= 1) {
      const allSchoolInputs = Array.from(document.querySelectorAll("input:not([type='hidden']):not([type='submit']):not([type='button'])"))
        .filter(input => {
          if (handledElements.has(input)) return false;
          const idText = getFieldIdentifier(input);
          return /(?:^|\b)(?:school|university|institution|college)(?:\b|$)/i.test(idText);
        });

      if (allSchoolInputs.length >= 2) {
        const structuralContainers = [];
        const seen = new Set();
        for (const input of allSchoolInputs) {
          const block = input.closest("fieldset, [class*='card'], [class*='row'], [class*='section'], [class*='group'], form > div, li");
          if (block && !seen.has(block)) {
            seen.add(block);
            structuralContainers.push(block);
          }
        }
        if (structuralContainers.length >= 2) {
          containers = structuralContainers;
        }
      }
    }

    if (containers.length > 0) {
      containers.forEach((container, idx) => {
        if (idx >= educationList.length) return;
        const edu = educationList[idx];
        if (!edu) return;

        const inputs = Array.from(container.querySelectorAll("input, textarea, select"));
        inputs.forEach(input => {
          if (handledElements.has(input)) return;
          if (input.type === "hidden" || input.type === "submit" || input.type === "button") return;

          const idText = getFieldIdentifier(input);

          // A. School / University
          if (/(?:^|\b)(?:school|university|institution|college|academy)(?:\b|$)/i.test(idText)) {
            const inst = edu.institution || edu.school || edu.university || "";
            if (inst) {
              if (setElementValue(input, inst, overwrite)) filledCount++;
              handledElements.add(input);
            }
          }
          // B. Degree
          else if (/(?:^|\b)(?:degree|qualification|program)(?:\b|$)/i.test(idText)) {
            const deg = edu.degree || "";
            if (deg) {
              if (input.tagName === "SELECT") {
                if (selectDropdownOption(input, deg, overwrite)) filledCount++;
              } else {
                if (setElementValue(input, deg, overwrite)) filledCount++;
              }
              handledElements.add(input);
            }
          }
          // C. Major / Field of Study
          else if (/(?:^|\b)(?:major|field.?of.?study|discipline|specialization|branch)(?:\b|$)/i.test(idText)) {
            const maj = edu.major || edu.field_of_study || edu.area || "";
            if (maj) {
              if (setElementValue(input, maj, overwrite)) filledCount++;
              handledElements.add(input);
            }
          }
          // D. Graduation Year / Date
          else if (/graduat|year|end.?date|to.?date/i.test(idText)) {
            const y = edu.year || edu.graduation_date || edu.end_date || "";
            if (y) {
              const yr = extractYear(y) || y;
              if (setElementValue(input, yr, overwrite)) filledCount++;
              handledElements.add(input);
            }
          }
          // E. GPA / Grade
          else if (/gpa|grade|score/i.test(idText)) {
            if (edu.gpa) {
              if (setElementValue(input, edu.gpa, overwrite)) filledCount++;
              handledElements.add(input);
            }
          }
        });
      });
    }

    // Fallback: Sibling / Sequential mapping for school inputs
    const unhandledSchoolInputs = Array.from(document.querySelectorAll("input:not([type='hidden']):not([type='submit']):not([type='button'])"))
      .filter(input => {
        if (handledElements.has(input)) return false;
        const idText = getFieldIdentifier(input);
        return /(?:^|\b)(?:school|university|institution|college)(?:\b|$)/i.test(idText);
      });

    if (unhandledSchoolInputs.length >= 2) {
      unhandledSchoolInputs.forEach((input, idx) => {
        if (idx < educationList.length) {
          const inst = educationList[idx].institution || educationList[idx].school || educationList[idx].university || "";
          if (inst) {
            if (setElementValue(input, inst, overwrite)) filledCount++;
            handledElements.add(input);
          }
        }
      });
    }

    return filledCount;
  }

  function autofillForm(profile, qaAnswers = [], overwrite = false) {
    if (!profile) return { filledCount: 0 };

    let filledCount = 0;
    const handledElements = new Set();

    // Stage 1: Work Experience Multi-Entry Section Fill
    const expList = Array.isArray(profile.experience) && profile.experience.length > 0
      ? profile.experience
      : (profile.currentCompany || profile.currentTitle ? [{ company: profile.currentCompany, title: profile.currentTitle }] : []);
    
    filledCount += autofillWorkExperience(expList, handledElements, overwrite);

    // Stage 2: Education Multi-Entry Section Fill
    const eduList = Array.isArray(profile.education) ? profile.education : [];
    filledCount += autofillEducation(eduList, handledElements, overwrite);

    // Count how many unhandled company and title inputs exist across the entire document
    const remainingCompanyInputs = Array.from(document.querySelectorAll("input:not([type='hidden']):not([type='submit']):not([type='button'])"))
      .filter(input => !handledElements.has(input) && /(?:^|\b)(?:company|employer|organization)(?:\b|$)/i.test(getFieldIdentifier(input)));
    const isSingleCompanyField = remainingCompanyInputs.length <= 1;

    const remainingTitleInputs = Array.from(document.querySelectorAll("input:not([type='hidden']):not([type='submit']):not([type='button'])"))
      .filter(input => !handledElements.has(input) && /(?:^|\b)(?:title|role|position|designation)(?:\b|$)/i.test(getFieldIdentifier(input)));
    const isSingleTitleField = remainingTitleInputs.length <= 1;

    // Stage 3: General Profile Fields Pass
    const inputs = Array.from(document.querySelectorAll("input, textarea, select"));

    let firstName = (profile.firstName || "").trim();
    let lastName = (profile.lastName || "").trim();
    let fullName = (profile.fullName || "").trim();

    if (!firstName && !lastName && fullName) {
      const parts = fullName.split(/\s+/);
      if (parts.length > 1) {
        firstName = parts.slice(0, parts.length - 1).join(" ");
        lastName = parts[parts.length - 1];
      } else {
        firstName = parts[0] || "";
      }
    }
    if (!fullName && (firstName || lastName)) {
      fullName = `${firstName} ${lastName}`.trim();
    }

    inputs.forEach((input) => {
      if (handledElements.has(input)) return;
      // Ignore hidden, captcha, token, submit, or reset inputs
      if (isIgnoredOrHiddenField(input)) return;

      const fieldIdentifier = getFieldIdentifier(input);

      // A. Standard Personal Information & Link Fields
      // 1. First Name / Given Name
      if (/(?:^|\b)(?:first.?name|given.?name|fname|forename)(?:\b|$)/i.test(fieldIdentifier) && !/last|sur|family/i.test(fieldIdentifier)) {
        if (firstName) {
          if (setElementValue(input, firstName, overwrite)) filledCount++;
          handledElements.add(input);
        }
      }
      // 2. Last Name / Family Name / Surname
      else if (/(?:^|\b)(?:last.?name|family.?name|surname|lname)(?:\b|$)/i.test(fieldIdentifier) && !/first|given/i.test(fieldIdentifier)) {
        if (lastName) {
          if (setElementValue(input, lastName, overwrite)) filledCount++;
          handledElements.add(input);
        }
      }
      // 3. Full Name
      else if (fullName && /(?:^|\b)(?:full.?name|your.?name|candidate.?name)(?:\b|$)/i.test(fieldIdentifier) && !/first|last|company|school|file|resume/i.test(fieldIdentifier)) {
        if (setElementValue(input, fullName, overwrite)) filledCount++;
        handledElements.add(input);
      }
      // 4. Email Address
      else if (profile.email && /email|e-mail/i.test(fieldIdentifier)) {
        if (setElementValue(input, profile.email, overwrite)) filledCount++;
        handledElements.add(input);
      }
      // 5. Phone / Mobile
      else if (profile.phone && /phone|mobile|tel|contact/i.test(fieldIdentifier)) {
        if (setElementValue(input, profile.phone, overwrite)) filledCount++;
        handledElements.add(input);
      }
      // 6. Current Location / City / Address (e.g. Hyderabad, London, etc.)
      else if ((profile.location || profile.city) && /location|city|address|based|where are you located|residence/i.test(fieldIdentifier) && !/relocat/i.test(fieldIdentifier)) {
        if (setElementValue(input, profile.location || profile.city, overwrite)) filledCount++;
        handledElements.add(input);
      }
      // 7. Combined LinkedIn or Portfolio
      else if (/(?:linkedin.*portfolio|portfolio.*linkedin|linkedin.?or.?portfolio|social.?link)/i.test(fieldIdentifier)) {
        const val = profile.linkedinUrl || profile.portfolioUrl || profile.githubUrl;
        if (val) {
          if (setElementValue(input, val, overwrite)) filledCount++;
          handledElements.add(input);
        }
      }
      // 8. CV / Resume Link / Cloud Storage Link
      else if (/(?:resume.?link|cv.?link|resume.?url|cv.?url|drive|dropbox|link.?to.?resume|link.?to.?cv|link.?to.*resume|resume.*drive|personal.?site)/i.test(fieldIdentifier)) {
        const val = profile.resumeLink || profile.portfolioUrl || profile.linkedinUrl || profile.githubUrl;
        if (val) {
          if (setElementValue(input, val, overwrite)) filledCount++;
          handledElements.add(input);
        }
      }
      // 9. LinkedIn URL
      else if (profile.linkedinUrl && /linkedin/i.test(fieldIdentifier)) {
        if (setElementValue(input, profile.linkedinUrl, overwrite)) filledCount++;
        handledElements.add(input);
      }
      // 10. GitHub URL
      else if (profile.githubUrl && /github|git/i.test(fieldIdentifier)) {
        if (setElementValue(input, profile.githubUrl, overwrite)) filledCount++;
        handledElements.add(input);
      }
      // 11. Portfolio / Personal Website
      else if ((profile.portfolioUrl || profile.websiteUrl) && /portfolio|website|site|personal.?url|blog/i.test(fieldIdentifier)) {
        if (setElementValue(input, profile.portfolioUrl || profile.websiteUrl, overwrite)) filledCount++;
        handledElements.add(input);
      }
      // 12. Current Company / Employer (Only if explicit Current Company or single standalone company field)
      else if (profile.currentCompany && (/(?:^|\b)(?:current.?company|current.?employer|present.?company|present.?employer)(?:\b|$)/i.test(fieldIdentifier) || (isSingleCompanyField && /(?:^|\b)(?:company|employer|organization)(?:\b|$)/i.test(fieldIdentifier))) && !/why/i.test(fieldIdentifier)) {
        if (setElementValue(input, profile.currentCompany, overwrite)) filledCount++;
        handledElements.add(input);
      }
      // 13. Current Title / Role (Only if explicit Current Title or single standalone title field)
      else if ((profile.currentTitle || profile.title) && (/(?:^|\b)(?:current.?title|current.?role|current.?designation|present.?title)(?:\b|$)/i.test(fieldIdentifier) || (isSingleTitleField && /(?:^|\b)(?:job.?title|title|role|designation)(?:\b|$)/i.test(fieldIdentifier))) && !/target|applied/i.test(fieldIdentifier)) {
        if (setElementValue(input, profile.currentTitle || profile.title, overwrite)) filledCount++;
        handledElements.add(input);
      }
      // 14. Years of Experience
      else if ((profile.yearsExperience || profile.experience) && /years.?of.?experience|years.?experience|yoe|total.?experience/i.test(fieldIdentifier)) {
        if (setElementValue(input, profile.yearsExperience || profile.experience, overwrite)) filledCount++;
        handledElements.add(input);
      }
      // 15. Notice Period / Availability
      else if (profile.noticePeriod && /notice.?period|availability|how.?soon|start.?date/i.test(fieldIdentifier)) {
        if (setElementValue(input, profile.noticePeriod, overwrite)) filledCount++;
        handledElements.add(input);
      }

      // B. Work Authorization & Sponsorship Radios / Selects
      // Privacy: these are personal facts. Only fill when the candidate has explicitly
      // provided a value in their profile — never assume "Yes"/"No".
      if (/authorized|sponsorship|visa/i.test(fieldIdentifier)) {
        const desiredAuth = (profile.workAuthorization || "").trim();
        const desiredSponsorship = (profile.sponsorshipRequired || "").trim();

        if (/authorized/i.test(fieldIdentifier) && desiredAuth) {
          if (input.type === "radio") {
            const wantYes = /^yes$/i.test(desiredAuth);
            if ((wantYes ? /yes|authorized/i : /no|not authorized/i).test(fieldIdentifier)) {
              if (overwrite || !input.checked) {
                input.checked = true;
                input.dispatchEvent(new Event("change", { bubbles: true }));
                filledCount++;
              }
              handledElements.add(input);
            }
          } else if (input.tagName === "SELECT") {
            if (selectDropdownOption(input, desiredAuth, overwrite) || setElementValue(input, desiredAuth, overwrite)) filledCount++;
            handledElements.add(input);
          }
        }
        if (/sponsorship/i.test(fieldIdentifier) && desiredSponsorship) {
          if (input.type === "radio") {
            const wantNo = /^no$/i.test(desiredSponsorship);
            if ((wantNo ? /no|will not/i : /yes|require/i).test(fieldIdentifier)) {
              if (overwrite || !input.checked) {
                input.checked = true;
                input.dispatchEvent(new Event("change", { bubbles: true }));
                filledCount++;
              }
              handledElements.add(input);
            }
          } else if (input.tagName === "SELECT") {
            if (selectDropdownOption(input, desiredSponsorship, overwrite) || setElementValue(input, desiredSponsorship, overwrite)) filledCount++;
            handledElements.add(input);
          }
        }
      }

      // C. Long-form Essay / Screener Questions (Textareas)
      if (input.tagName === "TEXTAREA") {
        let textareaFilled = false;

        // 1. Check QA Memory Bank for semantic matches
        if (qaAnswers && qaAnswers.length > 0) {
          for (const qa of qaAnswers) {
            const qTokens = (qa.question || "").toLowerCase().split(" ").filter(t => t.length > 3);
            let matchCount = 0;
            for (const tok of qTokens) {
              if (fieldIdentifier.includes(tok)) matchCount++;
            }
            if (matchCount >= 2 && qa.answer) {
              const detectedComp = extractJobMetadata().company || "your team";
              const adaptedAns = qa.answer.replace(/\[Company\]/g, detectedComp);
              if (setElementValue(input, adaptedAns, overwrite)) {
                filledCount++;
              }
              handledElements.add(input);
              textareaFilled = true;
              break;
            }
          }
        }

        // 2. Subjective "Why company / Why role / About yourself" questions are intentionally NOT
        //    auto-filled here. The content script has no access to the AI backend, so any text it
        //    could inject would be fabricated. These questions are surfaced to the side panel via
        //    detected_questions, where the user triggers a real generation request
        //    (POST /api/qa/grounded-pitch) and reviews the answer before filling the field.
      }
    });

    const telemetry = inspectFormTelemetry();
    return { 
      filledCount,
      telemetry,
      detectedQuestions: telemetry.detected_questions
    };
  }

  // -------------------------------------------------------------
  // 4. Floating Quick-Action Overlay Widget (Draggable & Non-Blocking)
  // -------------------------------------------------------------

  function makeDraggable(element, handle) {
    let isDragging = false;
    let startX = 0;
    let startY = 0;
    let initialLeft = 0;
    let initialTop = 0;

    handle.addEventListener("mousedown", startDrag);
    handle.addEventListener("touchstart", startDrag, { passive: false });

    function startDrag(e) {
      if (e.target.closest("button")) return;
      isDragging = true;
      element.classList.add("is-dragging");

      const clientX = e.type.startsWith("touch") ? e.touches[0].clientX : e.clientX;
      const clientY = e.type.startsWith("touch") ? e.touches[0].clientY : e.clientY;

      const rect = element.getBoundingClientRect();
      startX = clientX;
      startY = clientY;
      initialLeft = rect.left;
      initialTop = rect.top;

      // Switch from bottom/right positioning to explicit top/left
      element.style.bottom = "auto";
      element.style.right = "auto";
      element.style.left = `${initialLeft}px`;
      element.style.top = `${initialTop}px`;

      document.addEventListener("mousemove", onDrag);
      document.addEventListener("mouseup", stopDrag);
      document.addEventListener("touchmove", onDrag, { passive: false });
      document.addEventListener("touchend", stopDrag);

      e.preventDefault();
    }

    function onDrag(e) {
      if (!isDragging) return;
      const clientX = e.type.startsWith("touch") ? e.touches[0].clientX : e.clientX;
      const clientY = e.type.startsWith("touch") ? e.touches[0].clientY : e.clientY;

      const deltaX = clientX - startX;
      const deltaY = clientY - startY;

      const maxLeft = Math.max(10, window.innerWidth - element.offsetWidth - 10);
      const maxTop = Math.max(10, window.innerHeight - element.offsetHeight - 10);

      const newLeft = Math.min(maxLeft, Math.max(10, initialLeft + deltaX));
      const newTop = Math.min(maxTop, Math.max(10, initialTop + deltaY));

      element.style.left = `${newLeft}px`;
      element.style.top = `${newTop}px`;

      if (e.cancelable) e.preventDefault();
    }

    function stopDrag() {
      if (!isDragging) return;
      isDragging = false;
      element.classList.remove("is-dragging");

      document.removeEventListener("mousemove", onDrag);
      document.removeEventListener("mouseup", stopDrag);
      document.removeEventListener("touchmove", onDrag);
      document.removeEventListener("touchend", stopDrag);

      // Persist custom user position in session
      try {
        const rect = element.getBoundingClientRect();
        sessionStorage.setItem("job_agent_badge_pos", JSON.stringify({ left: rect.left, top: rect.top }));
      } catch (e) {}
    }
  }

  function injectFloatingBadge() {
    if (!isJobApplicationContext()) return;
    if (sessionStorage.getItem("job_agent_badge_dismissed") === "true") return;

    let badge = document.querySelector(".job-agent-floating-badge");
    if (badge) return;

    badge = document.createElement("div");
    badge.className = "job-agent-floating-badge";

    const isMinimized = sessionStorage.getItem("job_agent_badge_minimized") === "true";
    if (isMinimized) {
      badge.classList.add("is-minimized");
    }

    badge.innerHTML = `
      <div class="job-agent-badge-drag-handle" title="Drag to reposition">
        <div class="job-agent-pulse-dot"></div>
        <span class="job-agent-badge-title-text">⚡ Job Agent</span>
      </div>
      <div class="job-agent-badge-actions">
        <button class="job-agent-autofill-btn" id="job-agent-quick-fill-btn" title="1-Click Auto-Fill Application Form">
          <span>Auto-Fill Form</span>
        </button>
        <button class="job-agent-minimize-btn" id="job-agent-min-badge-btn" title="Minimize / Collapse">—</button>
        <button class="job-agent-close-btn" id="job-agent-close-badge-btn" title="Dismiss for this session">✕</button>
      </div>
      <div class="job-agent-minimized-icon" title="Click to expand Job Agent">⚡</div>
    `;

    // Restore user saved position if available
    try {
      const savedPos = sessionStorage.getItem("job_agent_badge_pos");
      if (savedPos) {
        const { left, top } = JSON.parse(savedPos);
        if (left >= 0 && top >= 0 && left < window.innerWidth && top < window.innerHeight) {
          badge.style.bottom = "auto";
          badge.style.right = "auto";
          badge.style.left = `${left}px`;
          badge.style.top = `${top}px`;
        }
      }
    } catch (e) {}

    document.body.appendChild(badge);

    const dragHandle = badge.querySelector(".job-agent-badge-drag-handle");
    makeDraggable(badge, dragHandle);
    makeDraggable(badge, badge.querySelector(".job-agent-minimized-icon"));

    const fillBtn = badge.querySelector("#job-agent-quick-fill-btn");
    const minBtn = badge.querySelector("#job-agent-min-badge-btn");
    const closeBtn = badge.querySelector("#job-agent-close-badge-btn");
    const minIcon = badge.querySelector(".job-agent-minimized-icon");

    // Auto-fill button click
    fillBtn.addEventListener("click", async (e) => {
      e.stopPropagation();
      fillBtn.innerText = "Filling...";
      fillBtn.disabled = true;

      chrome.storage.local.get(["candidateProfile", "localQABank", "autofillOverwritePref"], (result) => {
        const profile = result.candidateProfile || {};
        const qaAnswers = result.localQABank || [];
        const overwrite = Boolean(result.autofillOverwritePref);
        const fillResult = autofillForm(profile, qaAnswers, overwrite);
        fillBtn.innerText = `✓ Filled ${fillResult.filledCount} fields`;
        setTimeout(() => {
          fillBtn.innerText = "Auto-Fill Form";
          fillBtn.disabled = false;
        }, 3000);
      });
    });

    // Minimize button click
    minBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      badge.classList.add("is-minimized");
      sessionStorage.setItem("job_agent_badge_minimized", "true");
    });

    // Expand when clicking minimized icon
    minIcon.addEventListener("click", () => {
      badge.classList.remove("is-minimized");
      sessionStorage.setItem("job_agent_badge_minimized", "false");
    });

    // Close button click
    closeBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      badge.remove();
      sessionStorage.setItem("job_agent_badge_dismissed", "true");
    });
  }

  function updateBadgeLifecycle() {
    const badge = document.querySelector(".job-agent-floating-badge");
    const shouldShow = isJobApplicationContext();

    if (shouldShow && !badge) {
      injectFloatingBadge();
    } else if (!shouldShow && badge) {
      badge.remove();
    }
  }

  // -------------------------------------------------------------
  // 5. Runtime Message Listener (From Side Panel & Background)
  // -------------------------------------------------------------

  chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    if (message.type === "PING") {
      sendResponse({ status: "alive", url: window.location.href });
    } else if (message.type === "GET_PAGE_JOB_INFO") {
      const meta = extractJobMetadata();
      const telemetry = inspectFormTelemetry();
      sendResponse({ success: true, data: meta, telemetry: telemetry });
    } else if (message.type === "GET_PAGE_TELEMETRY") {
      const telemetry = inspectFormTelemetry();
      sendResponse({ success: true, data: telemetry });
    } else if (message.type === "AUTOFILL_SPECIFIC_FIELD") {
      const fieldId = message.fieldId;
      const value = message.value;
      let matchedEl = null;
      if (fieldId) {
        matchedEl = document.getElementById(fieldId) || document.querySelector(`[name="${CSS.escape(fieldId)}"]`);
      }
      if (matchedEl) {
        setElementValue(matchedEl, value, true);
        sendResponse({ success: true, fieldId });
      } else {
        // Fallback: search textarea matching text
        const textareas = Array.from(document.querySelectorAll("textarea"));
        if (textareas.length > 0) {
          setElementValue(textareas[0], value, true);
          sendResponse({ success: true, fieldId: textareas[0].id || "textarea-0" });
        } else {
          sendResponse({ success: false, error: "Field not found" });
        }
      }
    } else if (message.type === "EXECUTE_AUTOFILL") {
      const profile = message.profile || {};
      const qaAnswers = message.qaAnswers || [];
      const overwrite = Boolean(message.overwriteExisting);
      const res = autofillForm(profile, qaAnswers, overwrite);
      sendResponse({ success: true, data: res });
    }
    return true;
  });

  // -------------------------------------------------------------
  // 6. SPA Dynamic DOM Observer
  // -------------------------------------------------------------
  // Invariant: this observer MUST only refresh the badge lifecycle. It must never call
  // autofillForm()/autofill*, so pages (incl. LinkedIn) are never auto-filled or submitted.

  let debounceTimer = null;
  const observer = new MutationObserver(() => {
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(updateBadgeLifecycle, 350);
  });

  if (document.body) {
    observer.observe(document.body, { childList: true, subtree: true });
  }

  // Initial check
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", updateBadgeLifecycle);
  } else {
    updateBadgeLifecycle();
  }
})();

