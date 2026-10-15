# Lessons Learned: LLM Models & Agentic Coding Platforms

**Date**: October 2026  
**Context**: Building Job Alert Agent with LLM coding assistants (Gemini, Claude, DeepSeek)  
**Audience**: Developers using AI code assistants, ML engineers evaluating LLM models

---

## Lesson 1: Gemini 3.x Model Hallucinations in Domain-Specific Tasks

### Observation

A lot of people in my network kept mentioning Gemini hallucinates a lot. But till this point, it provided grounded code and user-consented changes. But for ATS scoring, it started drifting away from the hard-set rules and started adding cooked up information in code.

### What Happened

Fortunately I kept verifying the code and realized the issues. Which made me review the code in detail and identify some 15% hard coding in the ATS code.

### Root Cause

When given domain-specific constraints (e.g., "score a job match between 0–100 based on resume vs JD, using only these rules"), Gemini would sometimes:
- Add scoring logic not in the original requirements
- Hallucinate "reasonable defaults" for edge cases
- Deviate from strict rule-based evaluation toward more "natural" reasoning

### Impact

This prompted me to move to pay-as-you-go Anthropic models and DeepSeek for critical scoring logic.

### Takeaway

**Gemini works well for open-ended code generation, but for rule-based deterministic systems, you need stronger verification and stricter prompt boundaries.**

---

## Lesson 2: Anthropic Claude — Chattiness & Over-Documentation

### Observation

Claude models are chatty. They tend to provide too much explanation on the context.

### Specific Patterns

They tend to write new code for every small thing:
- Be it functions, classes and files — they prefer to add new content
- Adds too much documentation: for plans, explanations and even the test-result summarization
- Annoying at first glance, but these also cost tokens

### Why This Matters

Over-verbosity in:
- Generated code means more to review and maintain
- Documentation means more to keep in sync
- Explanations means more context bloat (and higher token costs when the session grows)

### How to Mitigate

We have to handle this with proper ground rules and ask the agent to re-read the AGENTS.md file. Yes, they keep forgetting the rules as context in the session grows. So repeatedly compact the conversation and ask the agent: claude-code or opencode (my personal choice) to read the AGENTS.md instructions again.

You need a lot of patience to figure out optimal ways to tether the Claude models to only do what was asked and provide the concise information.

### Takeaway

**Claude's depth is an asset for complex reasoning, but requires strict guardrails (like AGENTS.md rules) and regular compression to stay focused and cost-effective.**

---

## Lesson 3: DeepSeek V3 Flash — Reliability & Privacy Trade-Off

### Observation

Personal experience: it was a very good model for coding. Rarely hallucinates, tries to auto-compact as context grows (not sure if it's some opencode configuration).

### Strengths

- **Reliability**: Produces grounded, verifiable code
- **Cost-awareness**: Naturally tries to compact context without being asked
- **Coding quality**: Comparable to Claude, with fewer hallucinations

### Caveat

Unfortunately, the concerns related to Chinese models privacy were always there.

### Takeaway

**DeepSeek is technically excellent for coding but carries geopolitical/privacy concerns that may influence your choice depending on use case and organizational policy.**

---

## Cross-References

- **Related in PORTFOLIO_JOURNEY.md**: How these model choices influenced the job-alert-agent architecture
- **Related in AGENTIC_ARCHITECTURE.md**: Model tier selection and provider routing logic
- **Related in AGENTS.md**: Guardrails for working with verbose/creative models

---

## Future Observations

Add more learnings here as you work with new models or discover patterns with existing ones.
