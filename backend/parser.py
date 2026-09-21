import io
import re
from typing import Dict
from pypdf import PdfReader

# -------------------------------------------------------------
# Lexicons & Regular Expressions (OpenResume / ATS Standard)
# -------------------------------------------------------------

COMMON_TECH_SKILLS = [
    "Python",
    "FastAPI",
    "Django",
    "Flask",
    "PostgreSQL",
    "MySQL",
    "SQLite",
    "MongoDB",
    "Redis",
    "Kafka",
    "RabbitMQ",
    "Docker",
    "Kubernetes",
    "AWS",
    "GCP",
    "Azure",
    "Terraform",
    "CI/CD",
    "Git",
    "GitHub",
    "Linux",
    "React",
    "Next.js",
    "TypeScript",
    "JavaScript",
    "HTML",
    "CSS",
    "Tailwind",
    "Node.js",
    "Express",
    "GraphQL",
    "REST API",
    "Microservices",
    "System Design",
    "Distributed Systems",
    "Machine Learning",
    "LLM",
    "LLMs",
    "NLP",
    "PyTorch",
    "TensorFlow",
    "Pandas",
    "NumPy",
    "Scikit-Learn",
    "Java",
    "Spring Boot",
    "C++",
    "C#",
    "Go",
    "Golang",
    "Rust",
    "SQL",
    "Elasticsearch",
    "Celery",
    "Airflow",
    "Apache Kafka",
    "gRPC",
]

MONTH_REGEX_STR = r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"

DATE_RANGE_REGEX = re.compile(
    rf"(?:(?:{MONTH_REGEX_STR}\.?,?\s+)?\d{{4}}|\d{{1,2}}/\d{{4}})\s*(?:[-–—to]+|\s+to\s+)\s*(?:Present|Current|Now|(?:{MONTH_REGEX_STR}\.?,?\s+)?\d{{4}}|\d{{1,2}}/\d{{4}})",
    re.IGNORECASE,
)

BULLET_PREFIXES = ("•", "●", "*", "-", "–", "—", "▪", "◦", "►", "■")

ACTION_VERBS = {
    "architected",
    "accelerated",
    "built",
    "created",
    "designed",
    "developed",
    "directed",
    "drove",
    "engineered",
    "executed",
    "implemented",
    "integrated",
    "led",
    "managed",
    "modernized",
    "orchestrated",
    "scaled",
    "spearheaded",
    "transformed",
    "contributed",
    "authored",
    "maintained",
    "delivered",
    "optimized",
    "championed",
    "deployed",
    "programmed",
    "conducted",
    "pioneered",
    "resolved",
}

TITLE_KEYWORDS = [
    "engineer",
    "developer",
    "architect",
    "lead",
    "manager",
    "director",
    "principal",
    "staff",
    "vp",
    "cto",
    "scientist",
    "consultant",
    "specialist",
    "founder",
    "analyst",
]

ALL_SECTION_HEADERS = {
    "summary": [
        "summary",
        "professional summary",
        "executive summary",
        "profile",
        "about me",
        "objective",
        "about",
    ],
    "experience": [
        "work experience",
        "professional experience",
        "experience",
        "employment history",
        "career history",
        "work history",
    ],
    "education": [
        "education",
        "academic background",
        "academic history",
        "qualifications",
        "academics",
        "education & certifications",
        "education and certifications",
    ],
    "skills": [
        "skills",
        "technical skills",
        "technical skill set",
        "core competencies",
        "technologies",
        "skills & competencies",
        "tech stack",
    ],
    "projects": ["projects", "key projects", "personal projects", "open source"],
    "certifications": ["certifications", "licenses", "certificates", "awards"],
}

# -------------------------------------------------------------
# Text Normalization & Cleaners
# -------------------------------------------------------------


def normalize_resume_text(text: str) -> str:
    """
    Cleans and normalizes extracted resume text.
    Handles broken single-word line breaks (common in PDFs), extraneous whitespace,
    and formats section headers cleanly.
    """
    if not text:
        return ""

    text = text.replace("\x00", "")
    raw_lines = [l.strip() for l in text.splitlines()]

    known_section_names = set(
        h for headers in ALL_SECTION_HEADERS.values() for h in headers
    )

    cleaned_paragraphs = []
    current_paragraph = []

    for line in raw_lines:
        if not line:
            if current_paragraph:
                cleaned_paragraphs.append(" ".join(current_paragraph))
                current_paragraph = []
            continue

        clean_lower = line.strip(":").lower()
        is_bullet = line.startswith(BULLET_PREFIXES) or bool(
            re.match(r"^\d+[\.\)]\s+", line)
        )
        is_header = (
            bool(re.match(r"^[A-Z\s/&]{3,40}:?$", line)) and len(line.split()) <= 5
        ) or clean_lower in known_section_names

        if is_bullet or is_header:
            if current_paragraph:
                cleaned_paragraphs.append(" ".join(current_paragraph))
                current_paragraph = []
            cleaned_paragraphs.append(line)
        else:
            current_paragraph.append(line)

    if current_paragraph:
        cleaned_paragraphs.append(" ".join(current_paragraph))

    normalized = "\n".join(cleaned_paragraphs)
    normalized = re.sub(r"\s+\|\s+", " | ", normalized)
    normalized = re.sub(r"\n{3,}", "\n\n", normalized)
    normalized = re.sub(r"[ \t]+", " ", normalized)
    return normalized.strip()


def split_candidate_name(
    name: str, explicit_first: str = "", explicit_last: str = ""
) -> tuple[str, str]:
    """
    splits a candidate full name into (first_name, last_name).
    """
    first = (explicit_first or "").strip()
    last = (explicit_last or "").strip()
    if first and last:
        return first, last

    name_clean = (name or "").strip()
    if not name_clean:
        return first or "", last or ""

    # Check for "LastName, FirstName Middle" format
    if "," in name_clean:
        comma_parts = [p.strip() for p in name_clean.split(",", 1) if p.strip()]
        if len(comma_parts) == 2:
            return comma_parts[1], comma_parts[0]

    parts = name_clean.split()
    if len(parts) > 1:
        # Standard convention: last token is surname / family name, preceding tokens are compound given name
        return " ".join(parts[:-1]), parts[-1]
    elif len(parts) == 1:
        return parts[0], ""

    return "", ""


# -------------------------------------------------------------
# Job Description Cleaner & Boilerplate Stripper
# -------------------------------------------------------------

BOILERPLATE_SECTION_PATTERNS = [
    r"(?i)^(?:#{1,6}\s*)?(?:benefits(?:\s*(?:&|and)\s*perks)?|what\s+we\s+offer|perks(?:\s*(?:&|and)\s*benefits)?|our\s+benefits|compensation(?:\s*(?:&|and)\s*benefits)?|total\s+rewards)\b.*",
    r"(?i)^(?:#{1,6}\s*)?(?:equal\s+opportunity(?:\s+employer)?|eeo(?:\s+statement)?|diversity(?:\s*,\s*equity)?(?:\s*(?:&|and)\s*inclusion)?|affirmative\s+action|dei\s+statement)\b.*",
    r"(?i)^(?:#{1,6}\s*)?(?:pay\s+transparency(?:\s+notice)?|salary\s+transparency|compensation\s+range|expected\s+compensation|base\s+pay\s+range)\b.*",
    r"(?i)^(?:#{1,6}\s*)?(?:notice\s+to\s+(?:recruitment\s+)?agencies|unsolicited\s+agency\s+resumes|third\s+party\s+recruiters)\b.*",
    r"(?i)^(?:#{1,6}\s*)?(?:background\s+check(?:\s+policy)?|physical\s+demands|working\s+conditions|work\s+authorization)\b.*",
    r"(?i)^(?:#{1,6}\s*)?(?:about\s+(?:us|the\s+company|our\s+team|our\s+mission|our\s+values)|who\s+we\s+are)\b.*",
]

CORE_SECTION_PATTERNS = [
    r"(?i)^(?:#{1,6}\s*)?(?:about\s+the\s+role|role\s+overview|the\s+opportunity|position\s+overview|job\s+summary|what\s+you'll\s+do|responsibilities|key\s+responsibilities|duties|what\s+you\s+will\s+do)\b.*",
    r"(?i)^(?:#{1,6}\s*)?(?:requirements|qualifications|what\s+we're\s+looking\s+for|what\s+you\s+need|who\s+you\s+are|minimum\s+qualifications|basic\s+qualifications|preferred\s+qualifications|skills\s+(?:&|and)\s+experience|nice\s+to\s+have|bonus\s+points|tech\s+stack)\b.*",
]

INLINE_BOILERPLATE_REGEX = re.compile(
    r"(?i)(?:"
    r"we\s+are\s+(?:an?\s+)?equal\s+opportunity\s+employer[^\n\.]*|"
    r"all\s+qualified\s+applicants\s+will\s+receive\s+consideration[^\n\.]*|"
    r"pursuant\s+to\s+the\s+.*fair\s+chance\s+ordinance[^\n\.]*|"
    r"we\s+do\s+not\s+accept\s+unsolicited\s+(?:agency\s+)?resumes[^\n\.]*|"
    r"must\s+be\s+(?:legally\s+)?authorized\s+to\s+work\s+in\s+the\s+(?:us|united states|uk|eu)[^\n\.]*|"
    r"the\s+base\s+salary\s+range\s+for\s+this\s+position\s+is[^\n\.]*"
    r")[\.\n]?",
    re.MULTILINE,
)


def clean_job_description(text: str, max_chars: int = 2500) -> str:
    """
    Strips legal EEO statements, benefits/perks listings, salary transparency disclosures,
    and generic HR boilerplate from job descriptions.
    Preserves core responsibilities, qualifications, and role overviews.
    """
    if not text:
        return ""

    # Remove HTML tags if present
    clean = re.sub(r"<[^>]+>", " ", text)
    clean = re.sub(r"&[a-zA-Z0-9#]+;", " ", clean)

    lines = clean.splitlines()
    filtered_lines = []
    in_boilerplate_section = False

    for line in lines:
        stripped = line.strip()
        if not stripped:
            if (
                not in_boilerplate_section
                and filtered_lines
                and filtered_lines[-1] != ""
            ):
                filtered_lines.append("")
            continue

        # Check if line starts a core section
        is_core = any(re.match(p, stripped) for p in CORE_SECTION_PATTERNS)
        if is_core:
            in_boilerplate_section = False
            filtered_lines.append(stripped)
            continue

        # Check if line starts a boilerplate section
        is_boilerplate = any(
            re.match(p, stripped) for p in BOILERPLATE_SECTION_PATTERNS
        )
        if is_boilerplate:
            in_boilerplate_section = True
            continue

        if in_boilerplate_section:
            continue

        filtered_lines.append(stripped)

    result = "\n".join(filtered_lines)
    # Strip any remaining inline boilerplate fragments
    result = INLINE_BOILERPLATE_REGEX.sub("", result)
    result = re.sub(r"\n{3,}", "\n\n", result).strip()

    # Fallback to original text if over-cleaned
    if len(result) < 50 and len(text) >= 50:
        result = text[:max_chars].strip()

    return result[:max_chars]


def segment_resume_sections(text: str) -> Dict[str, str]:
    """
    Segments a plain resume text into recognized standard ATS sections.
    """
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    sections = {
        "header": [],
        "summary": [],
        "experience": [],
        "education": [],
        "skills": [],
        "projects": [],
        "certifications": [],
    }

    current_section = "header"

    for line in lines:
        cleaned_line = line.strip(":").strip().lower()
        matched_section = None

        if len(line.split()) <= 4:
            for sec_name, header_keywords in ALL_SECTION_HEADERS.items():
                if any(
                    cleaned_line == kw or cleaned_line.startswith(kw)
                    for kw in header_keywords
                ):
                    matched_section = sec_name
                    break

        if matched_section:
            current_section = matched_section
            continue

        sections[current_section].append(line)

    return {k: "\n".join(v) for k, v in sections.items()}


def is_bullet_or_detail(line: str) -> bool:
    """Detects if a line is an accomplishment bullet or detail sentence rather than a job header."""
    if line.startswith(BULLET_PREFIXES) or bool(re.match(r"^\d+[\.\)]\s+", line)):
        return True
    first_word = line.split()[0].lower().strip(":,.-") if line.split() else ""
    if first_word in ACTION_VERBS:
        return True
    if len(line.split()) > 12:
        return True
    return False


def clean_detail_line(line: str) -> str:
    return line.lstrip("•*-–—▪◦► ").strip()


TITLE_START_REGEX = re.compile(
    r"\b((?:Principal|Senior|Sr\.\s+Staff|Sr\.|Staff|Lead|Member\s+of\s+Technical\s+Staff|Software\s+Development\s+Engineer|Software\s+Engineer|Application\s+Developer|Developer|Architect|Engineer|Manager|Director|Analyst|Scientist)(?:[^\n\|]*))\b",
    re.IGNORECASE,
)


def extract_timeline_entries(text: str) -> list[dict]:
    """
    Extracts structured timeline roles using pipe and date range delimiter patterns.
    Handles dense and PDF-extracted formats.
    """
    flat_text = " ".join(text.split())
    DATE_EXPR = rf"{MONTH_REGEX_STR}\.?\s+\d{{4}}\s*(?:-|–|—|to)\s*(?:Present|Current|Now|{MONTH_REGEX_STR}\.?\s+\d{{4}})"
    matches = list(re.finditer(rf"\|\s*({DATE_EXPR})", flat_text, re.IGNORECASE))

    entries = []
    for idx, m in enumerate(matches):
        dates = m.group(1)
        start_pos = m.start()
        end_pos = m.end()

        preceding = flat_text[max(0, start_pos - 120) : start_pos].strip()
        masked = re.sub(
            r"\b(Sr|Jr|Inc|Corp|Ltd|Dr|Mr|Ms)\.\s+", r"\1_DOT_ ", preceding, flags=re.I
        )
        chunks = re.split(r"\.\s+|\b[●•]\b|\n", masked)
        header_chunk = chunks[-1].replace("_DOT_", ".").strip() if chunks else preceding
        header_chunk = re.sub(
            r"^(?:Professional\s+Experience|Work\s+Experience|Experience)\s*",
            "",
            header_chunk,
            flags=re.I,
        ).strip()

        if any(
            k in header_chunk.lower()
            for k in [
                "master",
                "bachelor",
                "degree",
                "university",
                "certification",
                "ielts",
                "academics",
            ]
        ):
            continue

        t_match = TITLE_START_REGEX.search(header_chunk)
        if t_match:
            company = header_chunk[: t_match.start()].strip(" ,|-")
            title = header_chunk[t_match.start() :].strip(" ,|-")
        else:
            company = header_chunk
            title = "Software Engineer"

        next_start = (
            matches[idx + 1].start() if idx + 1 < len(matches) else len(flat_text)
        )
        details_chunk = flat_text[end_pos:next_start]

        if idx + 1 < len(matches):
            next_preceding = flat_text[max(0, next_start - 120) : next_start]
            next_masked = re.sub(
                r"\b(Sr|Jr|Inc|Corp|Ltd|Dr|Mr|Ms)\.\s+",
                r"\1_DOT_ ",
                next_preceding,
                flags=re.I,
            )
            next_chunks = re.split(r"\.\s+|\b[●•]\b|\n", next_masked)
            if next_chunks:
                last_piece = next_chunks[-1].replace("_DOT_", ".").strip()
                if last_piece and details_chunk.endswith(last_piece):
                    details_chunk = details_chunk[: -len(last_piece)].strip()

        details_chunk = re.sub(
            r"(?:Education\s*(?:&|and)\s*Certifications|Education|Certifications).*$",
            "",
            details_chunk,
            flags=re.I,
        )
        bullets = [b.strip() for b in re.split(r"[●•]", details_chunk) if b.strip()]
        details = " • ".join(bullets)

        entries.append(
            {
                "title": title,
                "company": company or "Direct Employer",
                "dates": dates,
                "details": details,
            }
        )
    return entries


def parse_open_source_resume(text: str) -> dict:
    """
    ATS parser based on OSS - OpenResume / Resume-Matcher patterns.
    Extracts contact info, summary, experience timeline, education, and skills.
    """
    sections = segment_resume_sections(text)
    header_text = sections["header"] or text[:400]

    # 1. Email
    email_match = re.search(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+", text)
    email = email_match.group(0).strip(".,;") if email_match else ""

    # 2. Phone
    phone_match = re.search(
        r"(?:\+?\d{1,3}[-.\s]?)?\(?\d{2,4}\)?[-.\s]?\d{3,5}[-.\s]?\d{3,5}", text
    )
    phone = (
        re.sub(r"\s+", " ", phone_match.group(0)).strip(".,;") if phone_match else ""
    )

    # 3. Candidate Name
    name = ""
    header_lines = [l.strip() for l in header_text.splitlines() if l.strip()]
    for l in header_lines[:4]:
        if "@" in l or "http" in l or "linkedin" in l.lower() or "github" in l.lower():
            continue
        if "|" in l:
            candidate = l.split("|")[0].strip()
            if 2 <= len(candidate.split()) <= 4 and not any(
                k in candidate.lower() for k in ["email", "phone", "mobile", "resume"]
            ):
                name = candidate
                break
        words = l.split()
        if 2 <= len(words) <= 5 and not any(char in l for char in [":", "@", "/", "="]):
            name = l
            break
    if not name and header_lines:
        first = header_lines[0].split("|")[0].strip()
        if 2 <= len(first.split()) <= 5:
            name = first

    if not name or len(name.split()) < 2:
        flat_start = " ".join(text[:400].split())
        title_pos = re.search(
            r"\b(Principal|Senior|Lead|Staff|Software\s+Engineer|Developer|Architect|Mobile:|Mail:|Phone:|Email:|Links:)\b",
            flat_start,
            re.I,
        )
        if title_pos:
            cand = flat_start[: title_pos.start()].strip(" ,|-")
            if 2 <= len(cand.split()) <= 5 and not any(
                k in cand.lower()
                for k in ["resume", "curriculum", "email", "phone", "mobile"]
            ):
                name = cand

    # 4. Links
    linkedin_match = re.search(
        r"(?:https?://)?(?:www\.)?linkedin\.com/in/[a-zA-Z0-9_-]+", text
    )
    github_match = re.search(
        r"(?:https?://)?(?:www\.)?github\.com/[a-zA-Z0-9_-]+", text
    )
    links = {}
    if linkedin_match:
        links["linkedin"] = linkedin_match.group(0)
    if github_match:
        links["github"] = github_match.group(0)

    # 5. Summary
    summary = sections["summary"]
    if not summary:
        sum_match = re.search(
            r"(?:Executive\s+Summary|Professional\s+Summary|Summary)\s*(?:[●•]\s*)?(.+?)(?=(?:Core\s+Competencies|Technical\s+Skills|Professional\s+Experience|Work\s+Experience))",
            " ".join(text.split()),
            re.I,
        )
        if sum_match:
            sum_blob = sum_match.group(1).strip()
            summary = " ".join(
                [b.strip() for b in re.split(r"[●•]", sum_blob) if b.strip()]
            )
        else:
            paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
            for p in paragraphs:
                if len(p) > 40 and "@" not in p and "http" not in p:
                    summary = re.sub(r"\s+", " ", p)
                    break
            if not summary:
                summary = re.sub(r"\s+", " ", text[:300])
    if isinstance(summary, list):
        summary = " ".join(str(s) for s in summary)
    # Some PDF extractors emit one word per line; collapse all whitespace into a single line.
    summary = re.sub(r"\s+", " ", summary).strip()
    # Normalize bullet glyphs into readable separators.
    summary = re.sub(r"\s*[●•▪◦]\s*", " • ", summary).strip()
    summary = re.sub(r"^•\s*", "", summary)

    # 6. Skills
    skills_text = (sections["skills"] + " " + text).lower()
    skills_found = []
    for skill in COMMON_TECH_SKILLS:
        pattern = r"\b" + re.escape(skill.lower()) + r"\b"
        if re.search(pattern, skills_text):
            skills_found.append(skill)
    skills_found = list(dict.fromkeys(skills_found))

    # 7. Experience Timeline
    exp_text = sections["experience"] or text
    exp_lines = [l.strip() for l in exp_text.splitlines() if l.strip()]
    entries = []
    current_entry = None
    header_buffer = []

    for line in exp_lines:
        date_match = DATE_RANGE_REGEX.search(line)
        if date_match and not is_bullet_or_detail(line):
            dates = date_match.group(0)
            line_without_date = DATE_RANGE_REGEX.sub("", line).strip(" |,-–—()")
            combined_parts = []
            for b in header_buffer:
                if not is_bullet_or_detail(b) and len(b.split()) <= 8:
                    combined_parts.append(b)
            if line_without_date:
                combined_parts.append(line_without_date)

            header_buffer = []
            title = ""
            company = ""
            if len(combined_parts) >= 2:
                p0, p1 = combined_parts[0], combined_parts[1]
                if any(k in p0.lower() for k in TITLE_KEYWORDS):
                    title, company = p0, p1
                else:
                    company, title = p0, p1
            elif len(combined_parts) == 1:
                hdr = combined_parts[0]
                for sep in [" | ", " at ", " @ ", " - ", " – ", ", "]:
                    if sep in hdr:
                        parts = hdr.split(sep, 1)
                        p0, p1 = parts[0].strip(), parts[1].strip()
                        if any(k in p0.lower() for k in TITLE_KEYWORDS):
                            title, company = p0, p1
                        else:
                            company, title = p0, p1
                        break
                if not title:
                    title = hdr
                    company = "Direct Employer"
            else:
                title = "Software Engineer"
                company = "Organization"

            if current_entry:
                entries.append(current_entry)
            current_entry = {
                "title": title,
                "company": company,
                "dates": dates,
                "details": [],
            }
        elif is_bullet_or_detail(line):
            header_buffer = []
            if current_entry:
                detail = clean_detail_line(line)
                if detail:
                    current_entry["details"].append(detail)
        else:
            if len(line.split()) <= 8:
                header_buffer.append(line)

    if current_entry:
        entries.append(current_entry)

    for e in entries:
        e["details"] = (
            " • ".join(e["details"]) if isinstance(e["details"], list) else e["details"]
        )

    # If section-based extraction produced fewer entries than timeline pattern matching, use timeline matches
    timeline_entries = extract_timeline_entries(text)
    if len(timeline_entries) > len(entries):
        entries = timeline_entries

    # 8. Education & Certifications
    edu_text = sections["education"]
    edu_entries = []
    if edu_text:
        edu_bullets = [
            re.sub(r"\s+", " ", b).strip()
            for b in re.split(r"[●•\n]{2,}|[●•]", edu_text)
            if b.strip()
        ]
        for b in edu_bullets[:8]:
            clean_b = b.lstrip("•*-–—▪◦► &").strip()
            if not clean_b or clean_b.lower() in [
                "certification",
                "certifications",
                "education",
                "academics",
                "education & certifications",
            ]:
                continue
            if "|" in clean_b:
                parts = [p.strip() for p in clean_b.split("|") if p.strip()]
                degree = parts[0]
                school = parts[1] if len(parts) > 1 else "University / Institution"
                dates = parts[2] if len(parts) > 2 else ""
                edu_entries.append({"degree": degree, "school": school, "dates": dates})
            elif len(clean_b) >= 4:
                edu_entries.append(
                    {
                        "degree": clean_b,
                        "school": "University / Institution",
                        "dates": "",
                    }
                )

    if not edu_entries:
        flat_all = " ".join(text.split())
        edu_match = re.search(
            r"(?:Education\s*(?:&|and)\s*Certifications|Education)\s*(?:[●•]\s*)?(.+?)$",
            flat_all,
            re.I,
        )
        if edu_match:
            edu_blob = edu_match.group(1).strip()
            bullets = [b.strip() for b in re.split(r"[●•]", edu_blob) if b.strip()]
            for b in bullets:
                clean_b = b.lstrip("•*-–—▪◦► &").strip()
                if not clean_b or clean_b.lower() in [
                    "certification",
                    "certifications",
                    "education",
                    "academics",
                    "education & certifications",
                ]:
                    continue
                if "|" in clean_b:
                    parts = [p.strip() for p in clean_b.split("|") if p.strip()]
                    degree = parts[0]
                    school = parts[1] if len(parts) > 1 else "University / Institution"
                    dates = parts[2] if len(parts) > 2 else ""
                    edu_entries.append(
                        {"degree": degree, "school": school, "dates": dates}
                    )
                elif len(clean_b) >= 4:
                    edu_entries.append(
                        {
                            "degree": clean_b,
                            "school": "University / Institution",
                            "dates": "",
                        }
                    )

    return {
        "name": name,
        "email": email,
        "phone": phone,
        "links": links,
        "summary": summary,
        "skills": skills_found,
        "experience": entries[:12],
        "education": edu_entries,
    }


def extract_experience_heuristics(resume_text: str) -> list[dict]:
    """Extracts structured work history entries from resume text."""
    parsed = parse_open_source_resume(resume_text)
    return parsed.get("experience", [])


# -------------------------------------------------------------
# Document Extractors
# -------------------------------------------------------------


def extract_text_from_pdf(pdf_bytes: bytes) -> str:
    """
    Reads PDF bytes
    """
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        full_text = []
        for page in reader.pages:
            text = page.extract_text(extraction_mode="layout")
            if text:
                full_text.append(text)
        raw_combined = "\n".join(full_text).strip()
        return normalize_resume_text(raw_combined)
    except Exception as e:
        raise ValueError(f"Error parsing PDF file: {str(e)}")


def parse_resume_document(file_bytes: bytes, filename: str) -> str:
    """Detects file extension (PDF, TXT, MD) and extracts normalized raw text."""
    ext = filename.split(".")[-1].lower() if "." in filename else ""
    if ext == "pdf":
        return extract_text_from_pdf(file_bytes)
    elif ext in ["txt", "md"]:
        raw = file_bytes.decode("utf-8", errors="ignore").strip()
        return normalize_resume_text(raw)
    else:
        try:
            raw = file_bytes.decode("utf-8").strip()
            return normalize_resume_text(raw)
        except Exception:
            raise ValueError(
                f"Unsupported file format: '.{ext}'. Supported formats are PDF, TXT, and MD."
            )
