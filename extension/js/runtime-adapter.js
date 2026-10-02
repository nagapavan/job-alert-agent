/**
 * Hybrid Runtime Adapter (Mode A: FastAPI vs Mode B: Gemini Nano Standalone)
 * Provides a unified API interface for the Side Panel and Extension UI.
 */

class JobAgentRuntimeAdapter {
  constructor() {
    this.backendUrl = "http://localhost:8000";
    this.apiKey = "";
    this.mode = "connected"; // "connected" or "standalone"
    this.isConnected = false;
    this.geminiNanoSession = null;
  }

  getAuthHeaders(customHeaders = {}) {
    const headers = {
      "Content-Type": "application/json",
      ...customHeaders
    };
    if (this.apiKey && this.apiKey.trim()) {
      headers["X-API-Key"] = this.apiKey.trim();
    }
    return headers;
  }

  async init() {
    const data = await chrome.storage.local.get(["backendUrl", "apiKey", "mode"]);
    if (data.backendUrl) this.backendUrl = data.backendUrl;
    if (data.apiKey) this.apiKey = data.apiKey;
    if (data.mode) this.mode = data.mode;

    // Check backend health
    await this.checkBackendStatus();

    // Check Chrome Built-in Gemini Nano availability
    await this.initGeminiNano();
  }

  async checkBackendStatus() {
    try {
      const resp = await fetch(`${this.backendUrl}/api/health`, {
        method: "GET",
        headers: this.getAuthHeaders()
      });
      if (resp.ok) {
        const data = await resp.json();
        this.isConnected = data.status === "ok";
      } else {
        this.isConnected = false;
      }
    } catch (e) {
      this.isConnected = false;
    }
    return this.isConnected;
  }

  async initGeminiNano() {
    try {
      if (typeof window !== "undefined" && window.ai && window.ai.languageModel) {
        const capabilities = await window.ai.languageModel.capabilities();
        if (capabilities.available === "readily") {
          this.geminiNanoSession = await window.ai.languageModel.create();
        }
      }
    } catch (e) {
      console.log("Gemini Nano not readily available in this Chrome environment:", e.message);
    }
  }

  async setMode(mode) {
    this.mode = mode;
    await chrome.storage.local.set({ mode });
  }

  async setApiKey(apiKey) {
    this.apiKey = apiKey || "";
    await chrome.storage.local.set({ apiKey: this.apiKey });
    await this.checkBackendStatus();
  }

  async setBackendUrl(url) {
    this.backendUrl = url;
    await chrome.storage.local.set({ backendUrl: url });
    await this.checkBackendStatus();
  }

  // -------------------------------------------------------------
  // Profile & Q&A Memory Synchronization
  // -------------------------------------------------------------
  async fetchCandidateProfile() {
    if (this.mode === "connected" && this.isConnected) {
      try {
        const resp = await fetch(`${this.backendUrl}/api/candidate/profile`, {
          method: "GET",
          headers: this.getAuthHeaders()
        });
        if (resp.ok) {
          const prof = await resp.json();
          // Fetch QA Memory Bank
          let qaBank = [];
          try {
            const qaResp = await fetch(`${this.backendUrl}/api/memory/qa`, {
              method: "GET",
              headers: this.getAuthHeaders()
            });
            if (qaResp.ok) {
              const rawQa = await qaResp.json();
              qaBank = rawQa.map((q) => ({
                question: q.question_text,
                answer: q.answer_text,
                category: q.category || "general"
              }));
            }
          } catch (qaErr) {
            console.debug("Could not fetch QA memory:", qaErr);
          }

          await chrome.storage.local.set({ candidateProfile: prof, localQABank: qaBank });
          return { success: true, profile: prof, qaBank };
        }
      } catch (e) {
        console.warn("Failed to fetch candidate profile from backend:", e);
      }
    }
    const data = await chrome.storage.local.get(["candidateProfile", "localQABank"]);
    return { success: false, profile: data.candidateProfile || {}, qaBank: data.localQABank || [] };
  }

  // -------------------------------------------------------------
  // 1. Job Match Analysis
  // -------------------------------------------------------------
  async analyzeJobMatch(jobTitle, company, jobDescription, resumeText) {
    // Mode A: Connected FastAPI — structured JSON match analysis (single source of truth)
    if (this.mode === "connected" && this.isConnected) {
      try {
        // Only forward genuine resume text; ignore a JSON candidate-profile blob so the
        // backend uses its authoritative active resume instead.
        const rawResume = typeof resumeText === "string" ? resumeText.trim() : "";
        const looksLikeResume = rawResume.length > 200 && !rawResume.startsWith("{");
        const resp = await fetch(`${this.backendUrl}/api/match/analyze`, {
          method: "POST",
          headers: this.getAuthHeaders(),
          body: JSON.stringify({
            job_title: String(jobTitle || "").slice(0, 500),
            company: String(company || "").slice(0, 255),
            job_description: String(jobDescription || "").slice(0, 15000),
            resume_text: looksLikeResume ? rawResume.slice(0, 50000) : null
          })
        });
        if (resp.ok) {
          const res = await resp.json();
          if (res.match_score == null || res.analysis_source === "error") {
            console.warn("Backend could not produce a match score:", res.error || res.analysis_source);
          } else {
            const score = this._normalizeScore(res.match_score);
            return {
              matchScore: score,
              analysis: this._formatMatchAnalysis(score, res),
              mode: "connected"
            };
          }
        } else {
          console.warn(`Backend match analysis returned ${resp.status}; trying on-device.`);
        }
      } catch (e) {
        console.warn("Backend match analysis failed, trying on-device:", e);
      }
    }

    // Mode B: On-Device Gemini Nano — request structured JSON so score and narrative agree
    if (this.geminiNanoSession) {
      try {
        const prompt = `You are an ATS resume match evaluator. Compare the candidate resume against the job and return ONLY minified JSON with this exact shape: {"match_score": <integer 0-100>, "strengths": ["...", "..."], "gaps": ["...", "..."], "feedback": "one sentence recommendation"}. Do not include markdown fences or any text outside the JSON. Job title: "${jobTitle}" at ${company}. Job description: ${jobDescription.slice(0, 800)}. Candidate resume: ${resumeText.slice(0, 500)}`;
        const result = await this.geminiNanoSession.prompt(prompt);
        const parsed = this._parseNanoMatchResult(result);
        if (parsed) {
          const score = this._normalizeScore(parsed.match_score);
          return {
            matchScore: score,
            analysis: this._formatMatchAnalysis(score, parsed),
            mode: "standalone_nano"
          };
        }
        // Could not parse JSON — reuse the percentage the model itself stated (still genuine).
        const pctMatch = String(result).match(/(\d{1,3})\s*%/);
        if (pctMatch) {
          const score = this._normalizeScore(pctMatch[1]);
          return { matchScore: score, analysis: String(result), mode: "standalone_nano" };
        }
        console.warn("Gemini Nano match response could not be parsed.");
      } catch (e) {
        console.warn("Gemini Nano prompt failed:", e);
      }
    }

    // Never fabricate a score. Surface an explicit, retryable failure instead.
    return this._matchUnavailableResult(
      "No AI model was available to evaluate this role. Connect the backend or enable on-device AI, then re-process."
    );
  }

  _matchUnavailableResult(message) {
    return {
      matchScore: null,
      error: true,
      analysis: `Match analysis unavailable\n\n${message || "The analysis could not be completed."}\n\nNo score is shown because a reliable result could not be produced.`,
      mode: "error"
    };
  }

  _normalizeScore(value) {
    const n = Math.round(Number(value));
    if (!Number.isFinite(n)) return 0;
    return Math.max(0, Math.min(100, n));
  }

  _parseNanoMatchResult(text) {
    const raw = String(text || "").trim().replace(/^```(?:json)?/i, "").replace(/```$/, "").trim();
    const start = raw.indexOf("{");
    const end = raw.lastIndexOf("}");
    if (start === -1 || end === -1 || end <= start) return null;
    try {
      const obj = JSON.parse(raw.slice(start, end + 1));
      if (obj && obj.match_score != null) return obj;
    } catch (e) {
      // fall through to null
    }
    return null;
  }

  _formatMatchAnalysis(score, data) {
    const strengths = (data.strengths || []).map(s => String(s).trim()).filter(Boolean).slice(0, 6);
    const gaps = (data.gaps || []).map(g => String(g).trim()).filter(Boolean).slice(0, 6);
    const feedback = String(data.feedback || data.summary || "").trim();
    const band = String(data.band || "").trim();
    const rec = String(data.apply_recommendation || "").trim();
    const missingMust = (data.requirements || [])
      .filter(r => r && r.category === "must_have" && r.status === "missing")
      .map(r => String(r.text || "").trim())
      .filter(Boolean);
    // The side panel escapes HTML and converts newlines, so emit plain text (no markdown syntax).
    const lines = [];
    if (rec) {
      const label = rec === "apply" ? "✅ Apply"
        : rec === "apply_with_caution" ? "⚠️ Apply with caution"
        : rec === "skip" ? "⛔ Skip"
        : "Unverified";
      lines.push(`Recommendation: ${label}${band ? ` (${band} match)` : ""}`);
      lines.push("");
    }
    if (data.eligible === false && data.ineligibility_reason) {
      lines.push(`⛔ Not eligible: ${String(data.ineligibility_reason).trim()}`);
      lines.push("");
    }
    lines.push(`Match Alignment: ${score}%`, "");
    lines.push("Key Strengths Detected:");
    lines.push(strengths.length ? strengths.map(s => `• ${s}`).join("\n") : "• No direct strengths detected.");
    lines.push("");
    lines.push("Missing Keywords / Gaps:");
    lines.push(gaps.length ? gaps.map(g => `• ${g}`).join("\n") : "• None detected.");
    if (missingMust.length) {
      lines.push("");
      lines.push("Missing Must-Haves:");
      lines.push(missingMust.slice(0, 3).map(m => `• ${m}`).join("\n"));
    }
    if (data.over_qualified) {
      lines.push("");
      lines.push("Note: you appear over-qualified for this role (advisory — your call whether to apply).");
    }
    if (feedback) {
      lines.push("");
      lines.push("Summary:");
      lines.push(feedback);
    }
    return lines.join("\n");
  }

  async generateGroundedPitch(question, company, jobTitle, jobDescription = "", resumeText = "") {
    // Load local state first — bank and prof are needed for fallback paths
    const localData = await chrome.storage.local.get(["localQABank", "candidateProfile"]);
    const bank = localData.localQABank || [];
    const prof = localData.candidateProfile || {};

    // Sanitize company name — reject generic ATS placeholders as target company
    const isGenericCompany = !company || /^(job boards?|boards?|greenhouse|lever|ashby|workday|target company|company|careers|apply)$/i.test(company.trim());
    const targetCompany = isGenericCompany ? "" : company.trim();

    // Mode A: Connected FastAPI backend (grounded anti-slop with LLM-Judge)
    if (this.mode === "connected" && this.isConnected) {
      try {
        const resp = await fetch(`${this.backendUrl}/api/qa/grounded-pitch`, {
          method: "POST",
          headers: this.getAuthHeaders(),
          body: JSON.stringify({
            question_text: question,
            company_name: targetCompany,
            job_title: jobTitle,
            job_description: jobDescription,
            auto_cache: true,
            threshold: 0.85
          })
        });
        if (resp.ok) {
          const result = await resp.json();

          // Sanity-check: if the answer incorrectly names a company other than the target,
          // log a warning but still return (LLM-Judge should have corrected it already)
          if (result.answer && targetCompany) {
            const knownPhantomCompanies = ["splunk", "cisco", "google", "meta", "amazon", "microsoft"];
            const answerLower = result.answer.toLowerCase();
            const targetLower = targetCompany.toLowerCase();
            for (const phantom of knownPhantomCompanies) {
              if (phantom !== targetLower && answerLower.includes(phantom)) {
                console.warn(`[GroundedPitch] Possible hallucinated company "${phantom}" in answer for target "${targetCompany}". LLM-Judge may not have corrected it.`);
              }
            }
          }

          return result;
        } else {
          const errText = await resp.text().catch(() => "");
          console.warn(`[GroundedPitch] Backend returned ${resp.status}: ${errText.slice(0, 200)}`);
        }
      } catch (e) {
        console.warn("[GroundedPitch] Connected backend call failed, falling back to standalone:", e);
      }
    }

    // Mode B: Local QA bank match (only returns answers the user previously saved)
    for (const item of bank) {
      const qSlice = question.toLowerCase().slice(0, 15);
      const iSlice = (item.question || "").toLowerCase().slice(0, 15);
      if (iSlice && (item.question.toLowerCase().includes(qSlice) || question.toLowerCase().includes(iSlice))) {
        // Replace any [Company] placeholder with target company
        const ans = (item.answer || "").replace(/\[Company\]/g, targetCompany || "your company");
        if (ans.trim()) {
          return {
            is_cache_hit: true,
            answer: ans,
            matched_question: item.question,
            similarity: 0.0,
            tokens_saved: 0
          };
        }
      }
    }

    // Mode C: On-Device Gemini Nano — a real model invocation
    if (this.geminiNanoSession) {
      try {
        const displayCompany = targetCompany || "your team";
        const candidateBg = resumeText || prof.resumeSummary || "Senior software engineer with backend and distributed systems experience";
        const antiSlopPrompt = `Draft a crisp, authentic, strictly 3 to 4 sentence application pitch for the ${jobTitle || "Engineer"} role at ${displayCompany}. Question: "${question}". Candidate background: ${candidateBg}. Rules: No generic praise, no conversational fluff (never use "thrilled", "passionate", "fast-paced world"). State 1 concrete observation on ${displayCompany}, 1-2 quantified past engineering achievements, and direct fit.`;
        const freshAnswer = await this.geminiNanoSession.prompt(antiSlopPrompt);
        if (freshAnswer && freshAnswer.trim()) {
          const answer = freshAnswer.trim();
          bank.push({
            question: question,
            answer: answer,
            category: "screener_pitch",
            created_at: new Date().toISOString()
          });
          await chrome.storage.local.set({ localQABank: bank });
          return {
            is_cache_hit: false,
            answer: answer,
            matched_question: question,
            similarity: 0.0,
            tokens_saved: 0
          };
        }
      } catch (e) {
        console.warn("[GroundedPitch] Gemini Nano pitch error:", e);
      }
    }

    // No backend, no Gemini Nano, and no saved answer — surface an explicit error instead of
    // fabricating a pitch (the side panel shows this to the user and offers a retry).
    return this._pitchUnavailableResult();
  }

  _pitchUnavailableResult() {
    return {
      is_cache_hit: false,
      answer: "",
      error: true,
      matched_question: "",
      similarity: 0.0,
      tokens_saved: 0,
      message: "Answer generation is unavailable. Start the local backend or enable Chrome Gemini Nano, then try again. No answer was fabricated."
    };
  }

  async resolveOrGenerateEssay(question, company, jobTitle, resumeText) {
    return this.generateGroundedPitch(question, company, jobTitle, "", resumeText);
  }

  async sendFormTelemetry(telemetryData) {
    if (!telemetryData || !telemetryData.fields) return;

    // Send to backend if connected
    if (this.mode === "connected" && this.isConnected) {
      try {
        await fetch(`${this.backendUrl}/api/forms/telemetry`, {
          method: "POST",
          headers: this.getAuthHeaders(),
          body: JSON.stringify(telemetryData)
        });
      } catch (e) {
        console.debug("Telemetry forwarding to backend failed:", e);
      }
    }

    // Also update local custom field catalog
    try {
      const stored = await chrome.storage.local.get(["customFieldsCatalog"]);
      const catalog = stored.customFieldsCatalog || {};
      const domain = telemetryData.domain || "unknown";
      if (!catalog[domain]) catalog[domain] = [];
      
      const newFields = telemetryData.fields.filter(f => !f.is_recognized);
      for (const nf of newFields) {
        if (!catalog[domain].some(existing => existing.field_id === nf.field_id)) {
          catalog[domain].push(nf);
        }
      }
      await chrome.storage.local.set({ customFieldsCatalog: catalog });
    } catch (e) {}
  }

  // -------------------------------------------------------------
  // 3. AI Career Copilot Chat
  // -------------------------------------------------------------
  async sendCopilotMessage(message, history = []) {
    if (this.mode === "connected" && this.isConnected) {
      try {
        const resp = await fetch(`${this.backendUrl}/api/chat/assistant`, {
          method: "POST",
          headers: this.getAuthHeaders(),
          body: JSON.stringify({
            message: message,
            history: history
          })
        });
        if (resp.ok) {
          const data = await resp.json();
          return {
            reply: data.reply,
            actions: data.actions_taken || [],
            jobs: data.embedded_jobs || []
          };
        }
      } catch (e) {
        console.warn("Connected copilot chat failed:", e);
      }
    }

    // Mode B: Standalone Copilot
    if (this.geminiNanoSession) {
      try {
        const prompt = `You are a Career Copilot and Job Application Strategist. Answer this candidate question concisely and helpfully:\n${message}`;
        const reply = await this.geminiNanoSession.prompt(prompt);
        return { reply, actions: [], jobs: [] };
      } catch (e) {
        console.warn("Nano copilot failed:", e);
      }
    }

    return {
      reply: `I am currently operating in **Standalone Offline Mode**. To unlock the full AI Copilot with multi-turn STAR interview coaching, dynamic model routing, and automatic database tracking, start your backend server (\`uvicorn backend.main:app\`) or ensure Chrome Gemini Nano is enabled.`,
      actions: [],
      jobs: []
    };
  }
}

// Export singleton instance
window.jobAgentRuntime = new JobAgentRuntimeAdapter();
