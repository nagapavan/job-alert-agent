# Targets: 
- `backend/jd_signals.py`: `parse_education`, `parse_certifications`
- `backend/match_classifier.py`: certification status check in `build_deterministic_assessment`, and (if needed) education-from-resume parsing

# Problem:
The deterministic extraction used PLAIN SUBSTRING matching for certifications and education, which causes short acronyms/words to match inside unrelated words. Confirmed cases:
- `CKA` (cert) falsely matched from `CKAD` (Certified Kubernetes Application Developer) in the resume
- `CKS` (cert) falsely matched from `bottlenecks` / `stacks` in the resume, and from `tracks` in the JD
- `Master` (degree) falsely parsed from `Master debugger(s)` in the JD
- The user also saw `Education: phd` with no `phd` in the JD/resume (likely the same substring bug on another posting, or LLM hallucination)

The skill matcher (`_skill_token`) already uses word-boundary regex and is correct; only certifications + education used raw substring.

# Decision:
1. **Certifications** (`parse_certifications` + cert status check): use word-boundary matching (the same `(?<![A-Za-z0-9])...(?![A-Za-z0-9])` boundary used by skills) so `CKAD`/`bottlenecks`/`stacks`/`tracks` no longer trigger `CKA`/`CKS`, while standalone `CKA`/`CKS`/`AWS Certified Solutions Architect` still match.
2. **Education** (`parse_education`): word-boundary alone is NOT enough for the ambiguous bare words `master`/`bachelor` (they legitimately appear as "master debugger"). Fix:
   - Unambiguous tokens (`phd`, `ph.d`, `doctorate`, `doctoral`, `mba`, `m.s.`, `m.sc`, `m.tech`, `b.s.`, `b.sc`, `b.tech`, etc.) match via word boundary.
   - Ambiguous bare words (`master`, `bachelor`, with optional possessive `'s`) only count as an education signal when followed by degree-context words (degree, of, in, science, engineering, technology, administration, arts, business, computer).
3. Apply the same normalized matching on the resume side (`cert.lower() in resume_blob`) so a resume's `CKAD` doesn't credit `CKA`.

No schema change. Pure deterministic-layer precision fix.

# Why:
- Raw substring matching is fundamentally unsound for short acronyms/words shared with ordinary English.
- The skill layer already demonstrates the correct approach (word-boundary); certifications/education should be consistent.
- The extension side panel surfaces these strengths/gaps directly, so false positives mislead the user's apply/no-apply decision.

# Implementation note (final):
- The first education attempt used separate "definite abbreviation" + "bare context" regex tables and
  a narrow lookahead. It caused a regression: `"Bachelor's or Master's degree"` no longer detected both
  levels (the first term was followed by "or" rather than a context word), and it added complexity that
  did not earn its keep. Simplify: keep the ORIGINAL `_EDUCATION` token lists (backward compatible),
  match every token with word-boundary (`(?<![A-Za-z0-9])...(?![A-Za-z0-9])`), and apply an extra
  degree-context window ONLY to the genuinely ambiguous bare words `master`/`bachelor`. The window allows
  up to 5 tokens + a context/degree word ahead, so "bachelor's or master's degree" chains work while
  "master debugger" / "master engineers" / "hacker bachelor pad" still return nothing.
- Note: bare "MS"/"BS" (no dots) are NOT matched by any token — this matches the original code's behavior
  (original tokens required a dot, e.g. "m.s."), so it is not a regression.
- Verified end-to-end: the JD's spurious `CKS` cert and `Education: master` are gone; legitimate
  requirements (Python, Kafka, LLM, Go, 7+ years, senior, GCP-missing) remain. `GCP` stays a real gap.
- Test coverage added in `tests/test_jd_signals.py`: ordinary-word false positives ("Master debugger",
  "hacker bachelor pad"), "X or Y" chaining, and no-substring-acronym cert matches.

# Follow-up fix (2026-10-15): "BS/MS/PhD" alternative lists
Reported: a JD line `Education: BS/MS/PhD in Computer Engineering, ... or a related field` produced a
"Missing Must-Haves: Education: phd" gap for a candidate with a bachelor's/master's.

Root cause: `parse_education` only found `phd` in the cluster — dotless `BS`/`MS` were not recognized
as bachelor/master (the old tokens required periods). So the alternatives list collapsed to the highest
level, and `min(rank)` = phd became the requirement.
Fix:
- Added dotless `bs`/`ba`/`ms`/`ma` to `_EDUCATION` (bachelor/master) and made them CONTEXT-GATED
  (same degree-context window as `master`/`bachelor`) so "MS Teams"/"BS detector" stay unmatched.
- The context window now crosses `/`, `,`, `&` separators and treats other degree tokens as context, so
  `BS/MS/PhD in ...` yields {bachelor, master, phd}.
- `build_deterministic_assessment` now displays the *effective minimum* level
  (`Education: bachelor`) instead of the raw list, matching the `min(rank)` comparison.
- Precompiled the education + certification patterns at module level (removed per-call recompilation);
  `parse_certifications` and the new `jd_signals.cert_in_text()` share `_CERT_RES`.
- Tests: `test_jd_signals.py::test_parse_education_slash_joined_degree_cluster`,
  `test_match_classifier.py::test_education_alternative_list_uses_lowest_level`.
