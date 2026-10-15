# Targets:
- `extension/content/ats-autofill.js`: `extractJobMetadata` (description tiers), new helpers
  `isLinkedInChallengePage`, `extractLinkedInJobDescription`, `extractJobInfoAsync`
- `extension/sidepanel/sidepanel.js`: `collectFrameData`, `handleInspection`
- `tests/test_extension_integration.py`: static-invariant tests

# Problem:
The extension side panel intermittently showed "random data" / wrong requirements on LinkedIn
(e.g. `reCAPTCHA` in the Active Page Inspector), while other times it worked — with no code change.

Live DOM probe on a LinkedIn job page (`linkedin.com/jobs/view/<id>`) returned:
- `jsonldCount: 0`  -> LinkedIn no longer emits `schema.org/JobPosting` JSON-LD on the client render
- `jobDescFound: false` -> the legacy `.jobs-description__content` / `.show-more-less-html__markup`
  classes no longer exist
- The page's `main#workspace` / `section` blocks contain feed content: "people you may know",
  company posts ("That's a wrap on Ottawa…"), recruiter names, "similar jobs", etc.
- `chrome-extension://invalid/ net::ERR_FAILED` noise plus reCAPTCHA -> LinkedIn is also serving
  anti-bot / auth challenge pages to automated contexts.

Root cause: with Tier 0 (JSON-LD) and Tier 1 (legacy classes) both dead, extraction fell through
to Tier 2 (`extractMainContentText` — bare `main`/`article`/`section`) and Tier 3
(`deepRootsText` = whole body). On LinkedIn those grab unrelated page text. Whether the correct
job text won was a race against LinkedIn's client-side render, so it was intermittent.

# Decision:
LinkedIn-specific fixes only (other ATS portals keep the existing tiered behavior):
1. **Job-scoped container selector**: the only stable hook is the id prefix
   `JobDetails_AboutTheJob_<jobId>` (verified in the probe; length 8561, content = "About the job…").
   Added `[id^="JobDetails_AboutTheJob_"]` to the Tier-1 list and a dedicated
   `extractLinkedInJobDescription()` helper. Kept legacy classes as fallbacks in case LinkedIn
   restores them.
2. **No whole-page fallback for LinkedIn**: Tier 2 and Tier 3 are now gated on `!isLinkedIn`.
   If the job container is absent, the description stays empty rather than filling with feed text.
3. **Challenge-page detection**: `isChallengeOrBlockedPage()` is checked for **every host**
   (a LinkedIn email job link can redirect through a google.com reCAPTCHA interstitial).
   It only flags an *actual* challenge surface:
   - URL: `/sorry/`, `/recaptcha/`, `/checkpoint`, `/authwall`, `/challenge`, or `google.com` +
     recaptcha/sorry.
   - A **rendered** reCAPTCHA challenge iframe (`iframe[src*="google.com/recaptcha"]` with a
     visible size ≥250×100).
   - Visible page text **dominated** by verification language ("unusual activity", "verify
     you're a human", "complete the captcha", "I'm not a robot", …).
   `extractJobMetadata` returns `{description: "", challenge: true}` on a hit.

   *False-positive fix (2026-10-15)*: the first version also flagged `[data-sitekey]`,
   `.g-recaptcha`, and any `script` referencing `recaptcha/api.js`. Those appear on ordinary
   application pages that embed an **invisible** reCAPTCHA badge (e.g. `kake.co/jobs/...`),
   causing "Verification required" on a perfectly normal page. Removed those signals; only a
   rendered challenge iframe / challenge URL / verification-dominated text counts now.
4. **Bounded render wait**: new async `extractJobInfoAsync()` polls (200ms, up to 4s) for the
   job container before extracting, exposed as `window.__jobAgentExtractJobInfoAsync`. The side
   panel's `collectFrameData` now prefers and awaits it (falling back to the sync entry point).
   This removes the render race that caused the intermittent behavior.
5. **Actionable UI state**: when a challenge page is detected (any host), or a LinkedIn
   description is < 200 chars, the side panel shows "Verification required" / "Job not detected"
   with a Re-process button — never a fabricated score from unrelated text. The side panel's
   tab-title fallback (used when the content script cannot be injected, e.g. no host permission)
   also flags challenge URLs/titles instead of treating the title as a description.

# Why:
- The scoring layer was already clean (deterministic assessment on the Spreetail JD returned only
  real requirements: Distributed Systems, AWS, 8+ years, staff level). The bug was purely input:
  wrong/absent text fed into a correct scorer.
- Node/element-agnostic whole-page text is fundamentally unsafe on a social feed; scoping to the
  job container is the correct source of truth.
- The earlier substring/cert/education and node.js fixes remain unchanged and green.
