# Antigravity IDE — Original Planning Artifacts (provenance)

These files are the original planning artifacts for the **Job Alert Agent**, created in a local
Antigravity IDE session and copied here verbatim for project provenance.

| File | Origin / purpose |
| :--- | :--- |
| `requirements_document.md` | Original spec: functional requirements, tech stack, guardrails, MVP scope. |
| `implementation_plan.md` | Status clean-up + Torre-style dossier (stack compatibility matrix, hiring team). |
| `task.md` | Task breakdown used during implementation. |
| `walkthrough.md` | Implementation walkthrough / verification notes. |

Source location: the local Antigravity IDE session workspace (not part of this repo, and not
reproduced here).

> **Note on guardrail drift:** the original `requirements_document.md` (§2.1, §4.2) explicitly
> chose **browser-session Gmail scanning over OAuth** and declared *"Zero-Token Credential
> Storage: No … Google OAuth client secrets … persisted on disk."* The project has since added an
> **opt-in BYOK Gmail API** flow (see [`../GMAIL_API.md`](../GMAIL_API.md)) which stores an
> encrypted OAuth client secret + refresh token. This is a deliberate, opt-in reversal and is
> recorded in `AGENTS.md` and `README.md`. Treat these artifacts as historical design input, not
> the current contract.
