"""
CareerChatAgent: Conversational AI Career Assistant & Job Search Copilot.
Handles multi-turn career strategy dialogues, dynamic pipeline job retrieval,
live interview preparation (STAR method), on-demand application tailoring, and database actions.
"""
import json
import logging
import re
from typing import List, Dict, Any, Optional
from sqlalchemy.orm import Session

from backend.database import Job, Resume, ApplicationEvent
from backend.config import decrypt_data
from backend.ai_helper import (
    generate_text, 
    generate_consolidated_application_package,
    clean_job_description,
    generate_embeddings,
    resolve_semantic_essay_cache
)
from backend.company_catalog import compute_cosine_similarity

logger = logging.getLogger("chat_agent")

# A status change is only triggered by an EXPLICIT mutation command. Bare status nouns
# ("offer", "screen", "apply") must not mutate anything — that previously let any message
# containing the substring "offer" flip the top search hit to "Offered".
_STATUS_MUTATION_VERB_RE = re.compile(r"\b(?:mark|set|move|update|change|flag|put)\b")
_STATUS_IMPERATIVE_RE = re.compile(r"^\s*(?:please\s+)?(?:shortlist|reject|ignore)\b")
_STATUS_PATTERNS = (
    ("Shortlisted", re.compile(r"\bshort[\s-]?list(?:ed)?\b")),
    ("Screening", re.compile(r"\bscreen(?:ing|ed|s)?\b")),
    ("Offered", re.compile(r"\boffer(?:ed|ing|s)?\b")),
    ("Applied", re.compile(r"\bappl(?:y|ied|ies)\b")),
    ("Interview", re.compile(r"\binterview(?:ing|s)?\b")),
    ("Rejected", re.compile(r"\breject(?:ed|ion|s)?\b")),
    ("Not Interested", re.compile(r"\b(?:not\s+interested|ignore|no\s+longer\s+interested)\b")),
)

# Words that never identify a target job when a status change is requested.
_STATUS_TARGET_STOPWORDS = {
    "the", "and", "for", "with", "from", "into", "onto", "this", "that", "job", "jobs",
    "role", "roles", "position", "positions", "please", "status", "stage", "as", "to",
    "mark", "set", "move", "update", "change", "flag", "put", "shortlist", "reject",
    "ignore", "offer", "offered", "applied", "apply", "screening", "screen", "interview",
    "interviewing", "rejected", "rejection", "not", "interested", "longer", "candidate",
}


def _detect_status_intent(message_lower: str) -> Optional[str]:
    """Returns a canonical status only when the message is an explicit mutation command.

    Requires a mutation verb (mark/set/move/update/change/flag/put) or an imperative status
    word at the start (shortlist/reject/ignore). Bare nouns such as "offer" are ignored, and
    the target job must still be unambiguous before anything is mutated.
    """
    has_command = bool(
        _STATUS_MUTATION_VERB_RE.search(message_lower)
        or _STATUS_IMPERATIVE_RE.search(message_lower)
    )
    if not has_command:
        return None
    for status, pattern in _STATUS_PATTERNS:
        if pattern.search(message_lower):
            return status
    return None


class CareerChatAgent:
    """
    Career assistant that understands the candidate's active profile,
    manages pipeline jobs, drafts tailored materials, and conducts mock interviews.
    """

    def __init__(self, db: Session):
        self.db = db

    def _build_candidate_context(self) -> Dict[str, Any]:
        """Loads decrypted active candidate resume profile and core skills."""
        active_resume = (
            self.db.query(Resume)
            .filter(Resume.is_active == True)
            .order_by(Resume.created_at.desc())
            .first()
        )
        if not active_resume:
            return {
                "has_resume": False,
                "resume_text": "",
                "skills": [],
                "name": "Candidate",
                "experience": "N/A"
            }

        resume_text = decrypt_data(active_resume.content_encrypted) if active_resume.content_encrypted else ""
        skills = []
        name = "Candidate"
        experience = "Experienced Engineer"

        if active_resume.parsed_json_encrypted:
            try:
                parsed = json.loads(decrypt_data(active_resume.parsed_json_encrypted))
                skills = parsed.get("skills", [])
                name = parsed.get("name") or parsed.get("full_name") or name
                experience = parsed.get("total_experience") or parsed.get("experience_summary") or experience
            except Exception:
                pass

        return {
            "has_resume": True,
            "resume_text": resume_text,
            "skills": skills,
            "name": name,
            "experience": experience
        }

    def _build_pipeline_snapshot(self) -> Dict[str, Any]:
        """Gathers aggregated statistics and top candidate jobs."""
        total_jobs = self.db.query(Job).count()
        to_apply_count = self.db.query(Job).filter(Job.status == "To Apply").count()
        shortlisted_count = self.db.query(Job).filter(Job.status == "Shortlisted").count()
        applied_count = self.db.query(Job).filter(Job.status == "Applied").count()
        interview_count = self.db.query(Job).filter(Job.status == "Interview").count()

        top_matches = (
            self.db.query(Job)
            .filter(Job.status.in_(["To Apply", "Shortlisted"]))
            .order_by(Job.match_score.desc())
            .limit(5)
            .all()
        )

        top_jobs_summary = []
        for j in top_matches:
            comp_name = j.company.name if j.company else "Company"
            top_jobs_summary.append({
                "id": j.id,
                "title": j.title,
                "company": comp_name,
                "location": j.location,
                "status": j.status,
                "match_score": int(j.match_score or 0),
                "url": j.url
            })

        return {
            "total_jobs": total_jobs,
            "to_apply_count": to_apply_count,
            "shortlisted_count": shortlisted_count,
            "applied_count": applied_count,
            "interview_count": interview_count,
            "top_jobs": top_jobs_summary
        }

    def _execute_job_search(self, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        """Queries the active pipeline for jobs matching keywords, company, status, 
        or semantic vector similarity."""
        q_lower = query.lower().strip()
        all_jobs = self.db.query(Job).all()
        if not all_jobs:
            return []

        # Generate query vector embedding for semantic search
        q_emb = None
        try:
            q_emb = generate_embeddings(query)
        except Exception:
            pass

        scored_jobs = []
        for j in all_jobs:
            comp_name = (j.company.name if j.company else "").lower()
            t_lower = (j.title or "").lower()
            loc_lower = (j.location or "").lower()
            desc_lower = (j.description or "").lower()
            status_lower = (j.status or "").lower()

            # Baseline score from match score
            score = (j.match_score or 50.0) * 0.2

            # Exact / substring keyword boosts
            if q_lower in comp_name:
                score += 50
            if q_lower in t_lower:
                score += 40
            if q_lower in loc_lower:
                score += 30
            if q_lower in status_lower:
                score += 30
            if q_lower in desc_lower:
                score += 15

            # Semantic Vector Similarity Boost (up to +40 points)
            if q_emb and j.embedding:
                try:
                    sim = compute_cosine_similarity(q_emb, j.embedding)
                    if sim > 0.4:
                        score += sim * 40.0
                except Exception:
                    pass

            # Check individual query tokens
            tokens = [w for w in q_lower.split() if len(w) > 2]
            for tok in tokens:
                if tok in comp_name or tok in t_lower or tok in loc_lower or tok in desc_lower:
                    score += 8

            scored_jobs.append((score, j))

        scored_jobs.sort(key=lambda x: x[0], reverse=True)

        matches = []
        for _, j in scored_jobs[:limit]:
            matches.append({
                "id": j.id,
                "title": j.title,
                "company": j.company.name if j.company else "Company",
                "location": j.location,
                "status": j.status,
                "match_score": int(j.match_score or 0),
                "url": j.url,
                "cover_letter_draft": bool(j.cover_letter_draft),
                "tailored_points": bool(j.tailored_resume_points)
            })

        return matches

    def _resolve_status_target(
        self, message: str, focused_job: Optional[Job]
    ) -> tuple[Optional[Job], List[Dict[str, Any]]]:
        """Resolves which job a status-change command refers to.

        Returns ``(job, ambiguous_candidates)``. Never falls back to the top fuzzy search
        hit: an explicit numeric id, the focused job, or a unique company/title token match
        is required. When several jobs match equally the request is reported as ambiguous
        rather than mutating a job on the user's behalf.
        """
        m_lower = message.lower()

        # 1. Explicit numeric reference: "job 5", "id 5", "#5".
        id_match = re.search(r"#(\d+)\b", m_lower) or re.search(r"\b(?:job|id)\s*#?(\d+)\b", m_lower)
        if id_match:
            job = self.db.query(Job).filter(Job.id == int(id_match.group(1))).first()
            if job:
                return job, []

        # 2. The job the user currently has open.
        if focused_job:
            return focused_job, []

        # 3. Unique company/title token match.
        tokens = {
            t for t in re.findall(r"[a-z0-9]{3,}", m_lower)
            if t not in _STATUS_TARGET_STOPWORDS
        }
        if not tokens:
            return None, []

        scored = []
        for j in self.db.query(Job).all():
            haystack = f"{j.company.name if j.company else ''} {j.title or ''}".lower()
            matches = sum(1 for t in tokens if t in haystack)
            if matches:
                scored.append((matches, j))
        if not scored:
            return None, []

        scored.sort(key=lambda pair: pair[0], reverse=True)
        top_score = scored[0][0]
        top = [j for score, j in scored if score == top_score]
        if len(top) == 1:
            return top[0], []
        return None, [
            {
                "id": j.id,
                "title": j.title,
                "company": j.company.name if j.company else "Company",
            }
            for j in top[:5]
        ]

    def _execute_status_update(self, job_id: int, new_status: str) -> Optional[Dict[str, Any]]:
        """Updates a job's status and logs an application event."""
        valid_statuses = ["To Apply", "Shortlisted", "Applied", "Screening", "Interview", "Offered", "Rejected", "Not Interested"]
        canonical_status = next((s for s in valid_statuses if s.lower() == new_status.lower()), None)
        if not canonical_status:
            return None

        job = self.db.query(Job).filter(Job.id == job_id).first()
        if not job:
            return None

        old_status = job.status
        job.status = canonical_status
        comp_name = job.company.name if job.company else "Company"

        event = ApplicationEvent(
            job_id=job.id,
            event_type="status_change",
            description=f"Status changed from '{old_status}' to '{canonical_status}' via AI Copilot."
        )
        self.db.add(event)
        self.db.commit()
        self.db.refresh(job)

        return {
            "job_id": job.id,
            "title": job.title,
            "company": comp_name,
            "old_status": old_status,
            "new_status": canonical_status
        }

    def _execute_material_generation(self, job_id: int) -> Optional[Dict[str, Any]]:
        """Generates tailored resume points, cover letter, and cold outreach note."""
        job = self.db.query(Job).filter(Job.id == job_id).first()
        if not job:
            return None

        candidate_info = self._build_candidate_context()
        if not candidate_info["resume_text"]:
            return None

        comp_name = job.company.name if job.company else "Company"
        pkg = generate_consolidated_application_package(
            resume_text=candidate_info["resume_text"],
            job_title=job.title,
            company_name=comp_name,
            job_description=job.description or job.title,
            include_resume_tailoring=True,
            include_cover_letter=True,
            include_cold_message=True,
            db=self.db
        )

        # Surface a real failure instead of persisting empty/None materials as success.
        if pkg.get("error"):
            return {
                "job_id": job.id,
                "title": job.title,
                "company": comp_name,
                "error": pkg["error"]
            }

        job.cover_letter_draft = pkg.get("cover_letter")
        job.tailored_resume_points = pkg.get("tailored_resume_points")
        job.cold_message_draft = pkg.get("cold_message")
        if pkg.get("match_score") is not None:
            job.match_score = pkg.get("match_score")

        self.db.commit()
        self.db.refresh(job)

        return {
            "job_id": job.id,
            "title": job.title,
            "company": comp_name,
            "cover_letter": job.cover_letter_draft,
            "tailored_resume_points": job.tailored_resume_points,
            "cold_message": job.cold_message_draft,
            "match_score": job.match_score
        }

    def _generate_offline_fallback_reply(
        self,
        message: str,
        candidate: Dict[str, Any],
        pipeline: Dict[str, Any],
        focused_job: Optional[Job] = None,
        actions_taken: Optional[List[Dict[str, Any]]] = None,
        embedded_jobs: Optional[List[Dict[str, Any]]] = None
    ) -> str:
        """
        Generates a clean, structured offline fallback response when local AI models are offline or restarting.
        Provides high-signal database pipeline insights and action summaries without crashing.
        """
        m_lower = message.lower()
        actions_taken = actions_taken or []
        embedded_jobs = embedded_jobs or []

        # 1. Action confirmation if database updates occurred
        action_summaries = []
        for a in actions_taken:
            if a.get("type") == "status_update":
                d = a.get("data", {})
                action_summaries.append(f"✅ Updated status for **{d.get('title')}** at **{d.get('company')}** to **{d.get('new_status')}**.")
            elif a.get("type") == "material_generation":
                d = a.get("data", {})
                if d.get("error"):
                    action_summaries.append(f"⚠️ Could not generate application materials for **{d.get('title')}** at **{d.get('company')}**: {d.get('error')}")
                else:
                    score_note = f" (Match Score: {d.get('match_score')}%)." if d.get("match_score") is not None else "."
                    action_summaries.append(f"⚡ Tailored application package generated for **{d.get('title')}** at **{d.get('company')}**{score_note}")
            elif a.get("type") == "job_search":
                action_summaries.append(f"🔍 Found **{a.get('results_count')}** matching role(s) in your pipeline.")

        action_header = "\n".join(action_summaries) + "\n\n" if action_summaries else ""

        # 2. Gmail inquiry (opt-in notice)
        if any(w in m_lower for w in ["rescan gmail", "scan gmail", "sync gmail", "check gmail", "gmail"]):
            return (
                f"{action_header}### 🔒 Gmail Inbox Reading Is Opt-In\n\n"
                f"Gmail inbox scanning is disabled by default for candidate privacy. Enable "
                f"**Settings → Opt-in Discovery Features → Gmail sync** to use it via "
                f"**Sync External Applications**.\n\n"
                f"**Alternative Discovery Channels:**\n"
                f"- 🔗 **LinkedIn Sync**: Sync live alerts and saved jobs directly from LinkedIn.\n"
                f"- 🌐 **Google Jobs**: Run live searches across millions of indexed postings.\n"
                f"- ⚡ **Scan Portals**: Scrape Greenhouse, Lever, Ashby, and custom ATS portals directly.\n\n"
                f"**Current Pipeline Status:**\n"
                f"- **Total Monitored**: {pipeline['total_jobs']} roles\n"
                f"- **To Apply**: {pipeline['to_apply_count']} | **Interviewing**: {pipeline['interview_count']}"
            )

        if any(w in m_lower for w in ["summary", "pipeline", "overview", "status", "stats", "active"]):
            lines = [
                f"{action_header}### 📊 Active Pipeline Overview",
                f"- **Total Monitored Roles**: {pipeline['total_jobs']}",
                f"- **To Apply**: {pipeline['to_apply_count']} | **Shortlisted**: {pipeline['shortlisted_count']}",
                f"- **Applied**: {pipeline['applied_count']} | **Interviewing**: {pipeline['interview_count']}",
                "",
                "#### 🎯 Top Matched Roles:"
            ]
            if pipeline["top_jobs"]:
                for j in pipeline["top_jobs"][:5]:
                    lines.append(f"- **{j['title']}** at **{j['company']}** ({j['location']}) — Match: `{j['match_score']}%` [{j['status']}]")
            else:
                lines.append("- *No high-match roles currently in To Apply/Shortlisted queue.*")
            
            lines.append("\n*(Generated via local database snapshot. Ensure LM Studio or Ollama is running for conversational reasoning.)*")
            return "\n".join(lines)

        if any(w in m_lower for w in ["top match", "best match", "recommend", "show job", "find job", "search", "matches"]):
            lines = [f"{action_header}### 🎯 Top Job Matches in Your Pipeline:"]
            jobs_to_show = embedded_jobs if embedded_jobs else pipeline.get("top_jobs", [])[:5]
            if jobs_to_show:
                for j in jobs_to_show:
                    lines.append(f"- **{j['title']}** at **{j['company']}** (`{j.get('match_score', 0)}% Match`) — 📍 {j.get('location', 'Remote')} | Status: `{j.get('status', 'To Apply')}`")
            else:
                lines.append("- *No matching roles found. Run a job scan or add new target companies.*")
            
            lines.append("\n*(Generated via local database snapshot. Ensure LM Studio or Ollama is running for conversational reasoning.)*")
            return "\n".join(lines)

        lines = [
            f"{action_header}👋 I have received your request: *\"{message}\"*",
            "",
            f"- **Candidate Profile**: {candidate['name']} ({candidate['experience']})",
            f"- **Active Pipeline**: {pipeline['total_jobs']} total jobs tracked across {pipeline['interview_count']} interview(s) and {pipeline['to_apply_count']} ready to apply.",
            "",
            "ℹ️ *Local AI model is currently offline or unreachable. Please ensure LM Studio (`localhost:1234`) or Ollama (`localhost:11434`) is active to enable conversational responses and STAR interview simulation.*"
        ]
        return "\n".join(lines)

    def process_message(
        self,
        message: str,
        history: Optional[List[Dict[str, str]]] = None,
        job_id: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Processes candidate prompt, evaluates intent, executes actions, and returns rich reply.
        """
        candidate = self._build_candidate_context()
        pipeline = self._build_pipeline_snapshot()
        history = history or []

        actions_taken = []
        embedded_jobs = []

        # 1. Check for specific focused job context
        focused_job = None
        if job_id:
            focused_job = self.db.query(Job).filter(Job.id == job_id).first()

        # 2. Heuristic Intent Detection for direct actions
        m_lower = message.lower()

        # A. Status Update Intent (e.g. "Move job 5 to Applied", "Shortlist Acme", "Mark Acme as rejected")
        target_status = _detect_status_intent(m_lower)
        if target_status:
            target_job, ambiguous_candidates = self._resolve_status_target(message, focused_job)
            if target_job:
                res = self._execute_status_update(target_job.id, target_status)
                if res:
                    actions_taken.append({
                        "type": "status_update",
                        "data": res
                    })
            elif ambiguous_candidates:
                # Do not guess which job the user meant — surface the candidates instead.
                actions_taken.append({
                    "type": "status_update_ambiguous",
                    "data": {
                        "requested_status": target_status,
                        "candidates": ambiguous_candidates,
                    }
                })

        # B. Material Generation Intent (e.g. "Tailor my resume for job 10", "Generate cover letter for Acme")
        if any(w in m_lower for w in ["tailor resume", "generate cover letter", "draft outreach", "create application package", "tailor for"]):
            target_job = focused_job
            if not target_job:
                searched = self._execute_job_search(message, limit=1)
                if searched:
                    target_job = self.db.query(Job).filter(Job.id == searched[0]["id"]).first()

            if target_job:
                res = self._execute_material_generation(target_job.id)
                if res:
                    actions_taken.append({
                        "type": "material_generation",
                        "data": res
                    })
                    embedded_jobs.append({
                        "id": target_job.id,
                        "title": target_job.title,
                        "company": target_job.company.name if target_job.company else "Company",
                        "match_score": int(target_job.match_score or 0),
                        "status": target_job.status,
                        "url": target_job.url
                    })

        # C. Job Search / Recommendation Intent (e.g. "Show top matches", "Find remote roles", "Search python jobs")
        if any(w in m_lower for w in ["top match", "best match", "show jobs", "find jobs", "search jobs", "recommendations", "opportunities", "matches today", "matches right now"]):
            found = self._execute_job_search(message, limit=4)
            if found:
                embedded_jobs.extend(found)
                actions_taken.append({
                    "type": "job_search",
                    "results_count": len(found)
                })

        # D. Semantic Essay / Screener Question Intent
        if any(w in m_lower for w in ["how should i answer", "answer this question", "application question", "why work here", "essay question"]):
            focused_comp = focused_job.company.name if (focused_job and focused_job.company) else None
            focused_role = focused_job.title if focused_job else None
            cached_hit = resolve_semantic_essay_cache(
                question=message,
                db=self.db,
                company_name=focused_comp,
                job_title=focused_role,
                threshold=0.82
            )
            if cached_hit:
                actions_taken.append({
                    "type": "semantic_cache_hit",
                    "matched_question": cached_hit["matched_question"],
                    "similarity": cached_hit["similarity"],
                    "tokens_saved": cached_hit["tokens_saved"],
                    "answer_preview": cached_hit["answer"]
                })

        # 3. Build Prompt for LLM Synthesizer
        history_snippet = ""
        for h in history[-6:]:
            role = h.get("role", "user").capitalize()
            content = h.get("content", "")
            history_snippet += f"{role}: {content}\n"

        focused_job_snippet = ""
        if focused_job:
            comp_name = focused_job.company.name if focused_job.company else "Company"
            focused_job_snippet = (
                f"\nFocused Job In Context:\n"
                f"- ID: {focused_job.id}\n"
                f"- Title: {focused_job.title} at {comp_name}\n"
                f"- Location: {focused_job.location}\n"
                f"- Status: {focused_job.status} (Match Score: {focused_job.match_score}%)\n"
                f"- Description Excerpt: {clean_job_description(focused_job.description or focused_job.title, max_chars=500)}\n"
            )

        top_matches_summary = ", ".join([f"{j['title']} @ {j['company']} ({j['match_score']}%)" for j in pipeline['top_jobs'][:3]]) if pipeline['top_jobs'] else "None currently in queue"
        skills_summary = ", ".join(candidate['skills'][:15]) if candidate['skills'] else "Engineering / Tech"

        system_prompt = (
            "You are the candidate's personal AI Career Copilot and Senior Executive Job Search Strategist.\n"
            "You have direct access to their active resume, job discovery pipeline, application tracker, and target company intelligence.\n\n"
            f"Candidate Profile:\n"
            f"- Name: {candidate['name']}\n"
            f"- Experience: {candidate['experience']}\n"
            f"- Key Skills: {skills_summary}\n\n"
            f"Pipeline Snapshot:\n"
            f"- Total Tracked: {pipeline['total_jobs']} roles\n"
            f"- To Apply: {pipeline['to_apply_count']}, Shortlisted: {pipeline['shortlisted_count']}, Applied: {pipeline['applied_count']}, Interviewing: {pipeline['interview_count']}\n"
            f"- Top Matches: {top_matches_summary}\n"
            f"{focused_job_snippet}\n"
            "Instructions:\n"
            "1. Be direct, empowering, proactive, and concise. Use clean GitHub markdown formatting.\n"
            "2. When discussing interview prep, use the STAR method (Situation, Task, Action, Result) referencing the candidate's real skills.\n"
            "3. If any database actions were taken (status changes, material generation), clearly confirm what was updated.\n"
            "4. Keep responses high-signal and structured with bold highlights and bullet points."
        )

        user_prompt = f"Conversation History:\n{history_snippet}\nCandidate Message: {message}"

        # If action was taken, note it in prompt
        if actions_taken:
            user_prompt += f"\n\nSystem Notice: The following real-time actions were already executed on the database: {json.dumps(actions_taken)}"

        copilot_task_type = "star_interview_prep" if any(w in m_lower for w in ["interview", "mock", "star", "behavioral", "question", "prep"]) else "career_copilot_strategy"

        try:
            ai_reply = generate_text(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                json_mode=False,
                task_type=copilot_task_type,
                task_name=f"Copilot-{copilot_task_type}"
            )
        except Exception as e:
            logger.warning(f"CareerCopilot AI generation offline fallback: {e}")
            ai_reply = self._generate_offline_fallback_reply(
                message=message,
                candidate=candidate,
                pipeline=pipeline,
                focused_job=focused_job,
                actions_taken=actions_taken,
                embedded_jobs=embedded_jobs
            )
            offline_banner = "⚠️ **Local AI offline** — showing stored pipeline data only; no AI analysis was generated.\n\n"
            if not ai_reply.startswith("⚠️ **Local AI offline**"):
                ai_reply = offline_banner + ai_reply

        return {
            "reply": ai_reply,
            "actions_taken": actions_taken,
            "embedded_jobs": embedded_jobs
        }
