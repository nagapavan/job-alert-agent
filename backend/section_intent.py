"""Shared section-heading intent classification.

A single primitive that answers "given a heading line, what does this section mean?"
so three independent consumers stop maintaining their own hardcoded phrase lists:

- **JD matching** (``backend.match_classifier``) needs ``required`` vs ``preferred``
  vs ``intro`` to split must-haves from nice-to-haves.
- **Resume parsing** (``backend.parser``) needs ``experience``/``education``/``skills``
  buckets.
- **Preference filtering** (``backend.preference_filter``) needs to know whether a topic
  sits under a must-have heading.

The label sets stay **separate per domain** (separation of responsibilities); only the
detection primitive is shared. Detection is layered: a fast deterministic phrase match
first (covers the overwhelming majority), then an embedding nearest-neighbour fallback
for phrasings no phrase list anticipates (e.g. "What sets you apart"). It never calls an
LLM.
"""
from __future__ import annotations

import logging
import re
import threading
import unicodedata
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from backend.embeddings import cosine_similarity as _cosine

logger = logging.getLogger("section_intent")

BULLET_CHARS = ("•", "●", "*", "-", "–", "—", "▪", "◦", "►", "■", "·")

# Similarity floor for the embedding fallback. Deliberately high: an unsure heading
# should stay "unknown" rather than silently mislabel a section.
_EMBED_THRESHOLD = 0.74
_EMBED_MARGIN = 0.03


# ---------------------------------------------------------------------------
# Phrase sets (data)
# ---------------------------------------------------------------------------

# Job-description heading intents.
JD_HEADINGS: Dict[str, Tuple[str, ...]] = {
    "required": (
        "required experience and skills", "required experience", "required skills",
        "minimum qualifications", "minimum requirements", "minimum experience",
        "basic qualifications", "required qualifications", "requirements",
        "qualifications", "must have", "must-have", "must haves", "what you'll need",
        "what you will need", "what we're looking for", "what we are looking for",
        "who you are", "who you'll be", "what you bring", "what you'll bring",
        "key qualifications", "key requirements", "you have", "you'll need",
        "you will need", "your background", "required", "skills and experience",
        "experience and skills", "skills", "core skills", "essential skills",
        "the must haves", "about you",
    ),
    "preferred": (
        "preferred experience", "preferred qualifications", "preferred skills",
        "preferred", "nice to have", "nice-to-have", "good to have",
        "bonus points", "added advantage", "added bonus", "highly regarded",
        "even better if", "what sets you apart", "what will set you apart",
        "what would set you apart", "extra credit", "ideal candidate",
    ),
    "responsibilities": (
        "what you'll do", "what you will do", "what you'll be doing",
        "what you will be doing", "how you'll make an impact", "responsibilities",
        "key responsibilities", "the role", "role expectations", "your role",
        "the opportunity", "day to day", "day-to-day", "what you'll own",
        "about the role", "duties", "job description",
    ),
    "intro": (
        "about us", "about the company", "about the team", "about our company",
        "who we are", "company overview", "our story", "our mission", "our vision",
        "why join us", "life at", "welcome to", "about the job", "about this role",
        "about the position", "position overview", "job overview", "role overview",
        "position summary",
    ),
    # Company perks / offer prose. These headings introduce no candidate requirements,
    # so they are masked before the JD skill scan (see match_classifier). Without this,
    # ordinary words in a perks paragraph (e.g. "benefits") match HR-domain taxonomy
    # aliases and surface as false requirements.
    "boilerplate": (
        "what we offer", "what we can offer", "what we can offer you",
        "what we offer you", "what you can expect", "what you can expect from us",
        "benefits", "benefits & perks", "benefits and perks", "our benefits",
        "perks", "compensation", "compensation & benefits",
        "compensation and benefits", "total rewards",
        "health & wellbeing", "health and wellbeing", "wellbeing",
        "personal & professional development", "personal and professional development",
        "professional development", "unconditional inclusion", "inclusion",
        "accessibility", "let's stay connected", "let us stay connected",
        "stay connected",
    ),
}

# Resume section intents (superset of the legacy parser.ALL_SECTION_HEADERS).
RESUME_SECTION_HEADERS: Dict[str, Tuple[str, ...]] = {
    "summary": (
        "summary", "professional summary", "executive summary", "profile",
        "about me", "objective", "career objective", "about",
    ),
    "experience": (
        "work experience", "professional experience", "experience",
        "employment history", "career history", "work history", "relevant experience",
    ),
    "education": (
        "education", "academic background", "academic history", "qualifications",
        "academics", "education & certifications", "education and certifications",
    ),
    "skills": (
        "skills", "technical skills", "technical skill set", "core competencies",
        "technologies", "skills & competencies", "tech stack", "technical expertise",
        "skills and interests",
    ),
    "projects": ("projects", "key projects", "personal projects", "open source"),
    "certifications": ("certifications", "licenses", "certificates", "awards"),
}

# Line-start phrases that flip the remainder of a posting to "preferred". These are
# deliberately only honored at the *start* of a line so mid-sentence uses
# ("Advanced degrees are a plus but not a substitute...") never open a preferred
# section — the exact false-positive this module was created to fix.
PREFERENCE_CUES: Tuple[str, ...] = (
    "ideally", "nice to have", "nice-to-have", "good to have", "bonus points",
    "even better if", "it'd be great if", "it would be great if", "we'd love",
    "a plus", "plus points", "would be a plus", "extra credit",
)

# Preference-filter must-have markers (heading or inline marker form).
MUST_HAVE_MARKERS: Tuple[str, ...] = (
    "must have", "must-have", "must:", "required:", "requirement:", "requirements:",
    "you must", "candidate must", "we require", "requires", "minimum qualifications",
)

# Domain -> label -> exemplar sentences for the embedding fallback.
_EMBED_EXEMPLARS: Dict[str, Dict[str, Tuple[str, ...]]] = {
    "jd": {
        "required": (
            "what you will need", "minimum qualifications required",
            "the requirements for this role",
        ),
        "preferred": (
            "nice to have additional experience", "bonus if you have this",
            "what would set you apart",
        ),
        "responsibilities": (
            "what you will be doing in this role", "your day to day responsibilities",
        ),
        "intro": ("about our company and team", "who we are as a company"),
        "boilerplate": (
            "what we offer you as an employee", "our benefits and perks",
            "compensation and total rewards",
        ),
    },
    "resume": {
        "experience": ("professional work experience",),
        "education": ("academic education background",),
        "skills": ("technical skills and technologies",),
        "summary": ("professional summary of the candidate",),
        "projects": ("personal and key projects",),
        "certifications": ("licenses and certifications",),
    },
}


# ---------------------------------------------------------------------------
# Normalization & phrase matching
# ---------------------------------------------------------------------------

_WS_RE = re.compile(r"\s+")
_TRAILING_PUNCT_RE = re.compile(r"[\s:•●*\-.–—]+$")

# Typographic punctuation JDs actually ship with. Unfolded, "What you’ll be doing" matches no
# heading phrase, the requirements body stays inside the preceding intro span and is masked
# out of scoring (issue #61).
_QUOTE_FOLD = str.maketrans(
    {
        "\u2018": "'",  # ‘
        "\u2019": "'",  # ’
        "\u201b": "'",  # ‛
        "\u201c": '"',  # “
        "\u201d": '"',  # ”
        "\u00a0": " ",  # nbsp
    }
)


def _strip_leading_symbols(text: str) -> str:
    """Drop leading emoji/variation selectors/control chars, keeping real punctuation.

    Only symbol (S*), mark (M*) and control (C*) code points are removed, so a leading dot or
    plus that is part of a name (".NET") survives.
    """
    i = 0
    while i < len(text) and unicodedata.category(text[i])[0] in ("S", "M", "C"):
        i += 1
    return text[i:]


def normalize_heading(line: str) -> str:
    """Lowercase, collapse whitespace, strip bullets/emoji and trailing punctuation.

    Typographic punctuation is folded to ASCII first: JDs routinely use ``’`` ("What you’ll
    be doing") and an unfolded quote defeats the whole heading vocabulary, which silently
    masks the requirements body as intro/boilerplate (issue #61). Leading emoji are dropped
    for the same reason (``💻 WHAT YOU'LL BE DOING``).
    """
    text = (line or "").translate(_QUOTE_FOLD).strip()
    # Markdown heading markers: ATS feeds (JSON-LD descriptions in particular) ship headings as
    # "## Minimum Qualifications" / "### Nice to Have". Left in place, the exact-match gate in
    # `_looks_like_heading` fails, every section collapses into the leading "required" span, and
    # nice-to-have items are scored as must-haves.
    text = re.sub(r"^#{1,6}\s*", "", text)
    text = text.lstrip("".join(BULLET_CHARS)).strip()
    text = _strip_leading_symbols(text).strip()
    text = _TRAILING_PUNCT_RE.sub("", text)
    text = _WS_RE.sub(" ", text)
    return text.lower()


def _phrase_pattern(phrase: str) -> "re.Pattern":
    # Word-ish boundaries that tolerate symbols like "&", "/", "+".
    return re.compile(
        r"(?<![A-Za-z0-9])" + re.escape(phrase) + r"(?![A-Za-z0-9])", re.I
    )


_PHRASE_RE_CACHE: Dict[str, "re.Pattern"] = {}


def _phrase_re(phrase: str) -> "re.Pattern":
    pat = _PHRASE_RE_CACHE.get(phrase)
    if pat is None:
        pat = _phrase_pattern(phrase)
        _PHRASE_RE_CACHE[phrase] = pat
    return pat


def _match_phrase_set(line: str, phrase_map: Dict[str, Sequence[str]]) -> str:
    normalized = normalize_heading(line)
    if not normalized:
        return "unknown"
    best_label, best_len = "unknown", 0
    for label, phrases in phrase_map.items():
        for phrase in phrases:
            if phrase == normalized or _phrase_re(phrase).search(normalized):
                if len(phrase) > best_len:
                    best_label, best_len = label, len(phrase)
    return best_label


def _exact_phrase_label(line: str, phrase_map: Dict[str, Sequence[str]]) -> str:
    """Label only when the normalized line equals a known heading exactly."""
    normalized = normalize_heading(line)
    if not normalized:
        return "unknown"
    for label, phrases in phrase_map.items():
        if normalized in phrases:
            return label
    return "unknown"


def _prefix_phrase_label(line: str, phrase_map: Dict[str, Sequence[str]]) -> str:
    """Label when the normalized line equals a heading, or starts with one followed by
    a separator. Used for resume headings, where a whole line like "CloudScale
    Technologies" must NOT match the "technologies" heading keyword."""
    normalized = normalize_heading(line)
    if not normalized:
        return "unknown"
    best_label, best_len = "unknown", 0
    for label, phrases in phrase_map.items():
        for phrase in phrases:
            if normalized == phrase:
                candidate = phrase
            elif normalized.startswith(phrase):
                nxt = normalized[len(phrase):len(phrase) + 1]
                if nxt and nxt in (" ", "&", "/", ",", "-"):
                    candidate = phrase
                else:
                    continue
            else:
                continue
            if len(candidate) > best_len:
                best_label, best_len = label, len(candidate)
    return best_label


def _embedding_label(line: str, domain: str) -> str:
    """Embedding nearest-neighbour fallback; returns 'unknown' when unavailable.

    Memoised per thread (see ``_exemplar_vectors`` / ``_remember_label``): the 13 JD exemplars
    are embedded once per embedding backend, not once per heading line, and a heading text is
    classified once per backend. Section headings repeat across a corpus, so the fallback used
    to dominate scoring time (~5 s of a 6.8 s five-posting profile).
    """
    exemplars = _EMBED_EXEMPLARS.get(domain)
    if not exemplars:
        return "unknown"
    text = normalize_heading(line)
    if not text:
        return "unknown"
    try:
        from backend import embeddings
    except Exception as exc:  # noqa: BLE001 - fallback must never break extraction
        logger.debug("Embedding heading classification skipped: %s", exc)
        return "unknown"

    stamp = embeddings.last_embed_stamp()
    if stamp is not None:
        cached = _label_cache().get((stamp, domain, text))
        if cached is not None:
            return cached

    try:
        labels, vectors = _exemplar_vectors(domain, exemplars, embeddings, stamp)

        # A non-semantic backend (hash) would produce meaningless similarity. Decided from
        # the vector actually produced, because configuration alone cannot tell whether a
        # configured backend served the request.
        stamp = embeddings.last_embed_stamp()
        if not stamp or stamp[0] == "hash":
            if stamp is not None:
                _remember_label((stamp, domain, text), "unknown")
            return "unknown"

        query = embeddings.embed_text(text)
        scored = sorted(
            ((_cosine(query, v), labels[i]) for i, v in enumerate(vectors)),
            reverse=True,
        )
        if not scored:
            return "unknown"
        top_score, top_label = scored[0]
        second = scored[1][0] if len(scored) > 1 else 0.0
        label = "unknown"
        if top_score >= _EMBED_THRESHOLD and (top_score - second) >= _EMBED_MARGIN:
            label = top_label
        _remember_label((stamp, domain, text), label)
        return label
    except Exception as exc:  # noqa: BLE001 - fallback must never break extraction
        logger.debug("Embedding heading classification skipped: %s", exc)
    return "unknown"


# Heading classification runs on background scoring threads, so the memo is thread-local: the
# embedding stamp it is keyed on is itself per-thread, and no lock is needed.
_HEADING_CACHE = threading.local()
# Cap per thread so a long rescore of unique headings cannot grow the memo without bound.
_HEADING_CACHE_MAX = 4096


def _label_cache() -> Dict[Tuple[Tuple[str, str], str, str], str]:
    cache = getattr(_HEADING_CACHE, "labels", None)
    if cache is None:
        cache = {}
        _HEADING_CACHE.labels = cache
    return cache


def _exemplar_cache() -> Dict[Tuple[Tuple[str, str], str], Tuple[List[str], List[List[float]]]]:
    cache = getattr(_HEADING_CACHE, "exemplars", None)
    if cache is None:
        cache = {}
        _HEADING_CACHE.exemplars = cache
    return cache


def _remember_label(key: Tuple[Tuple[str, str], str, str], label: str) -> None:
    cache = _label_cache()
    if len(cache) >= _HEADING_CACHE_MAX:
        cache.clear()
    cache[key] = label


def _exemplar_vectors(
    domain: str,
    exemplars: Dict[str, Sequence[str]],
    embeddings,
    stamp: Optional[Tuple[str, str]],
) -> Tuple[List[str], List[List[float]]]:
    """Exemplar labels + vectors for one domain, embedded once per embedding backend."""
    cache = _exemplar_cache()
    if stamp is not None:
        cached = cache.get((stamp, domain))
        if cached is not None:
            return cached
    labels: List[str] = []
    vectors: List[List[float]] = []
    for label, samples in exemplars.items():
        for sample in samples:
            labels.append(label)
            vectors.append(embeddings.embed_text(sample))
    stamp = embeddings.last_embed_stamp()
    if stamp is not None:
        # A backend change makes every earlier exemplar vector incomparable.
        for stale in [k for k in cache if k[0] != stamp]:
            cache.pop(stale, None)
        cache[(stamp, domain)] = (labels, vectors)
    return labels, vectors


def reset_heading_cache() -> None:
    """Drops this thread's heading memo (used by tests and after a backend switch)."""
    _HEADING_CACHE.labels = {}
    _HEADING_CACHE.exemplars = {}


def classify_heading(line: str, domain: str = "jd") -> str:
    """Return the intent label for a heading line, or 'unknown'."""
    if domain == "resume":
        return _prefix_phrase_label(line, RESUME_SECTION_HEADERS)
    if domain in ("jd", "must_have"):
        label = _match_phrase_set(line, JD_HEADINGS)
        if label != "unknown":
            return label
        if domain == "jd":
            return _embedding_label(line, "jd")
        return "unknown"
    return "unknown"


def starts_with_preference_cue(line: str) -> bool:
    """True when a line begins with an inline preference cue (line-start only)."""
    normalized = normalize_heading(line)
    if not normalized:
        return False
    for cue in PREFERENCE_CUES:
        pat = re.compile(r"^" + re.escape(cue) + r"(?![A-Za-z0-9])", re.I)
        if pat.search(normalized):
            return True
    return False


def is_must_have_heading(line: str, domain: str = "must_have") -> bool:
    """True when a line is a must-have/required heading (preference-filter policy)."""
    normalized = normalize_heading(line)
    if not normalized or len(normalized.split()) > 8:
        return False
    if classify_heading(line, "must_have") == "required":
        return True
    return any(marker in normalized for marker in MUST_HAVE_MARKERS)


# ---------------------------------------------------------------------------
# JD segmentation
# ---------------------------------------------------------------------------

_HEADING_WORD_LIMIT = 8
_HEADING_CHAR_LIMIT = 90


def _looks_like_heading(line: str, prefix: str) -> bool:
    """A line is heading-shaped when it is short and either:
    - a prefix followed by a colon matches a known heading phrase, or
    - the whole line exactly equals a known heading phrase.
    """
    stripped = line.strip()
    if not prefix or len(prefix.split()) > _HEADING_WORD_LIMIT:
        return False
    if len(stripped) > _HEADING_CHAR_LIMIT and prefix == stripped:
        return False
    if ":" in stripped:
        head = stripped.split(":", 1)[0].strip()
        return normalize_heading(head) == normalize_heading(prefix)
    # No colon -> only an exact, short phrase counts as a heading.
    if normalize_heading(stripped) != normalize_heading(prefix):
        return False
    return _exact_phrase_label(stripped, JD_HEADINGS) != "unknown"


@dataclass(frozen=True)
class SectionSpan:
    label: str
    start: int
    end: int


def _line_heading_label(line: str) -> Optional[str]:
    stripped = line.strip()
    if not stripped:
        return None
    if stripped[0] in BULLET_CHARS or re.match(r"^\d+[\.\)]\s", stripped):
        # A bullet is content; still, an inline preference cue can switch sections.
        if starts_with_preference_cue(stripped.lstrip("".join(BULLET_CHARS))):
            return "preferred"
        return None
    # Candidate heading: consider the text up to the first colon (if any).
    prefix = stripped.split(":", 1)[0].strip() if ":" in stripped else stripped
    label = classify_heading(prefix, "jd")
    if label != "unknown" and _looks_like_heading(stripped, prefix):
        return label
    if starts_with_preference_cue(stripped):
        return "preferred"
    return None


_SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+")


def _line_spans(text: str) -> List[Tuple[int, int]]:
    """Return (start, end) offsets of every line, keeping its terminator."""
    units: List[Tuple[int, int]] = []
    offset = 0
    for line in text.splitlines(keepends=True):
        units.append((offset, offset + len(line)))
        offset += len(line)
    return units


def _sentence_spans(text: str) -> List[Tuple[int, int]]:
    """Return (start, end) offsets of every sentence in a text with no line breaks."""
    units: List[Tuple[int, int]] = []
    pos = 0
    for match in _SENTENCE_BREAK.finditer(text):
        if match.start() > pos:
            units.append((pos, match.start()))
        pos = match.end()
    if pos < len(text):
        units.append((pos, len(text)))
    return units


# -------------------------------------------------------------
# Inline (sentence-trailing) preference markers — issue #98
# -------------------------------------------------------------
# A posting often marks one requirement nice-to-have without opening a section:
# "Experience with NATS.IO, Jetstream, or Kafka preferred". Honouring the cue only at the
# *end* of a unit keeps the mid-sentence case ("Advanced degrees are a plus but not a
# substitute for shipping") from switching sections.
_TRAILING_PREFERENCE_RE = re.compile(
    r"(?:\bis\s+)?\b(?:preferred|a\s+plus|nice\s+to\s+have|nice-to-have|good\s+to\s+have)"
    r"\s*[.!]?\s*$",
    re.I,
)


def ends_with_preference_cue(unit: str) -> bool:
    """True when a requirement unit is marked nice-to-have by a trailing cue."""
    return bool(_TRAILING_PREFERENCE_RE.search((unit or "").strip()))


# -------------------------------------------------------------
# Leading ATS header chrome — issue #98
# -------------------------------------------------------------
# Portal pages render a metadata block above the JD: title, department, location, worker
# category, job class, posted-date badge, requisition number and the Apply/Save CTAs. It
# sits before the first heading, which `jd_section_spans` labels `required`, so it used to
# be mined as requirements ("Full Time", "Professional", "24 days ago", "Apply").
#
# The guard is deliberately positive: a leading block is chrome only when at least two of
# its lines match a metadata shape. A flat JD that opens with a bare skills list matches
# none of them, so its leading span stays `required` — the reason this is not implemented
# by demoting all pre-heading text.
_CHROME_LINE_PATTERNS: Tuple["re.Pattern", ...] = (
    re.compile(r"^\d+\s+(?:second|minute|hour|day|week|month)s?\s+ago$", re.I),
    re.compile(r"^(?:posted|updated|published)\b.{0,40}$", re.I),
    re.compile(
        r"^(?:requisition|req|job|position|reference|ref)\s*"
        r"(?:id|no\.?|number|#)?\s*[:#]\s*\S+$",
        re.I,
    ),
    re.compile(
        r"^(?:apply(?: now)?|easy apply|save(?: job)?|share|bookmark|view job|back to results)$",
        re.I,
    ),
    re.compile(
        r"^(?:full[- ]?time|part[- ]?time|contract(?:or)?|internship|temporary|permanent|"
        r"freelance|seasonal)$",
        re.I,
    ),
    re.compile(
        r"^(?:professional|management|executive|entry[- ]level|mid[- ]level|senior[- ]level|"
        r"student|graduate|experienced)$",
        re.I,
    ),
    re.compile(r"^(?:remote|on[- ]?site|hybrid)(?:\s*\([^)]*\))?$", re.I),
)

_CHROME_LOCATION_RE = re.compile(r"^[A-Za-z][A-Za-z .'-]{1,30}(?:,\s*[A-Za-z][A-Za-z .'-]{1,30}){1,3}$")
_CHROME_REGION_CODE_RE = re.compile(r"^[A-Z]{2,3}$")


def _is_location_line(line: str) -> bool:
    """`City, ST` / `Remote India, IN` — a location, not a comma-separated skill list."""
    if not _CHROME_LOCATION_RE.match(line):
        return False
    parts = [p.strip() for p in line.split(",") if p.strip()]
    return bool(parts) and bool(_CHROME_REGION_CODE_RE.match(parts[-1]))


def _is_chrome_line(line: str) -> bool:
    stripped = (line or "").strip()
    if not stripped:
        return False
    if any(p.match(stripped) for p in _CHROME_LINE_PATTERNS):
        return True
    return _is_location_line(stripped)


def _is_prose_line(line: str) -> bool:
    """A line that opens real JD content (a sentence or a bullet)."""
    stripped = (line or "").strip()
    if not stripped:
        return False
    if stripped[0] in BULLET_CHARS or re.match(r"^\d+[\.\)]\s", stripped):
        return True
    words = stripped.split()
    if len(words) >= 8:
        return True
    return stripped[-1] in ".!?" and len(words) >= 4


def leading_header_chrome_end(text: str, max_lines: int = 15, max_filler: int = 2) -> int:
    """Offset at which the leading ATS metadata block ends (0 when there is none).

    Walks the leading lines until the first heading, prose line, `max_lines`, or the third
    non-chrome line, and returns the end offset only when the block carries at least two
    chrome signals. The filler allowance is what lets a department or title line sit inside
    the block without letting a terse skills list (`Kubernetes` / `Docker` / `AWS`) be read as
    chrome and stripped.
    """
    if not text or "\n" not in text:
        return 0
    block_end = 0
    chrome_signals = 0
    fillers = 0
    offset = 0
    for index, raw in enumerate(text.splitlines(keepends=True)):
        if index >= max_lines:
            break
        offset += len(raw)
        stripped = raw.strip()
        if not stripped:
            block_end = offset
            continue
        if _line_heading_label(stripped) is not None or _is_prose_line(stripped):
            break
        if _is_chrome_line(stripped):
            chrome_signals += 1
        else:
            fillers += 1
            if fillers > max_filler:
                break
        block_end = offset
    if chrome_signals < 2 or block_end <= 0 or block_end >= len(text):
        return 0
    return block_end


def _apply_header_chrome(spans: Sequence[SectionSpan], end: int) -> List[SectionSpan]:
    """Relabel the leading chrome region as `intro` (already masked by the scorer)."""
    if end <= 0:
        return list(spans)
    result: List[SectionSpan] = [SectionSpan("intro", 0, end)]
    for span in spans:
        start = max(span.start, end)
        if span.end <= start:
            continue
        if result[-1].label == span.label:
            result[-1] = SectionSpan(span.label, result[-1].start, span.end)
        else:
            result.append(SectionSpan(span.label, start, span.end))
    return result


def _leading_heading_phrase(unit: str) -> Optional[str]:
    """Label when a sentence *starts* with a known JD heading phrase followed by content.

    Only reachable for a description that lost its line breaks (issue #53): the heading sits
    mid-line, so "Preferred Qualifications Kubernetes is a plus." has to be recognised by
    prefix. Longest phrase wins, so "Preferred Qualifications" is not read as a bare
    "Preferred". A description that still has line breaks never takes this path, so its spans
    stay exactly as they were.
    """
    words = unit.split()
    for count in range(min(len(words), _HEADING_WORD_LIMIT), 0, -1):
        label = _exact_phrase_label(" ".join(words[:count]), JD_HEADINGS)
        if label != "unknown":
            return label
    return None


def _build_spans(
    text: str, units: Sequence[Tuple[int, int]], allow_leading_phrase: bool
) -> List[SectionSpan]:
    """Label a span list from pre-computed units."""
    spans: List[SectionSpan] = []
    current_label = "required"
    current_start = 0
    for start, end in units:
        unit = text[start:end]
        label = _line_heading_label(unit)
        if label is None and allow_leading_phrase and start > 0:
            label = _leading_heading_phrase(unit.strip())
        if label is None and current_label != "preferred" and ends_with_preference_cue(unit):
            # A single requirement marked nice-to-have inline: its own span, then the
            # surrounding section label resumes.
            if start > current_start:
                spans.append(SectionSpan(current_label, current_start, start))
            spans.append(SectionSpan("preferred", start, end))
            current_start = end
            continue
        if label is not None:
            if start > current_start:
                spans.append(SectionSpan(current_label, current_start, start))
            current_label = label
            current_start = start
    spans.append(SectionSpan(current_label, current_start, len(text)))
    return spans


def _merge_adjacent(spans: Sequence[SectionSpan]) -> List[SectionSpan]:
    """Coalesce consecutive spans with the same label (a JD can restate a heading)."""
    merged: List[SectionSpan] = []
    for span in spans:
        if span.end <= span.start:
            continue
        if merged and merged[-1].label == span.label:
            merged[-1] = SectionSpan(span.label, merged[-1].start, span.end)
        else:
            merged.append(span)
    return merged


def _long_lines_dominate(text: str) -> bool:
    """True when most characters sit in lines too long to be headings.

    Scraped JDs are frequently HTML-stripped into a few enormous "lines" (a whole paragraph
    each) plus a handful of short metadata lines. `_looks_like_heading` rejects the long
    ones, so the line pass cannot see the headings buried inside them.
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    total = sum(len(line) for line in lines)
    if not total:
        return False
    long_chars = sum(len(line) for line in lines if len(line) > _HEADING_CHAR_LIMIT)
    return (long_chars / total) > 0.5


def jd_section_spans(text: str) -> List[SectionSpan]:
    """Split a JD into labelled spans. Text before the first heading is 'required'.

    Line structure is used whenever it can detect headings. A description with no line
    breaks is segmented by sentence (issue #53). A description whose lines are mostly too
    long to be headings — the shape produced by HTML-stripping a posting — is segmented by
    sentence too, but only when the line pass found nothing, so well-structured descriptions
    keep their existing spans untouched (issue #108).
    """
    text = text or ""
    chrome_end = leading_header_chrome_end(text)
    if "\n" in text:
        line_spans = _build_spans(text, _line_spans(text), allow_leading_phrase=False)
        if len(line_spans) > 1 or not _long_lines_dominate(text):
            kept = [s for s in line_spans if s.end > s.start or not line_spans]
            return _apply_header_chrome(kept, chrome_end)

    sentence_spans = _build_spans(
        text, _sentence_spans(text), allow_leading_phrase=True
    )
    spans = _merge_adjacent(sentence_spans) or [SectionSpan("required", 0, len(text))]
    return _apply_header_chrome(spans, chrome_end)


def label_at(spans: Sequence[SectionSpan], offset: int) -> str:
    for span in spans:
        if span.start <= offset < span.end:
            return span.label
    return "required"


def mask_spans(text: str, spans: Sequence[SectionSpan]) -> str:
    """Replace span ranges with spaces, preserving length and offsets."""
    if not spans:
        return text
    chars = list(text)
    for span in spans:
        for i in range(max(0, span.start), min(len(chars), span.end)):
            if chars[i] != "\n":
                chars[i] = " "
    return "".join(chars)


def first_preferred_offset(spans: Sequence[SectionSpan]) -> Optional[int]:
    for span in spans:
        if span.label == "preferred":
            return span.start
    return None
