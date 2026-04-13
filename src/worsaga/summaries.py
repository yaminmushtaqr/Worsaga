"""Deterministic study-summary builder with optional LLM enhancement.

This module generates weekly study notes from Moodle course materials.
The pipeline is:

1. **Section finding** — score/rank sections to find the best match for
   a given teaching week, with smart fallback for reading weeks, revision
   weeks, exam periods, and weeks with no materials.
2. **Material filtering** — pick the most useful downloadable files,
   deduplicated and prioritised by format (PDF > PPTX > DOCX > TXT).
3. **Text extraction** — pull text from the selected files.
4. **Summary generation** — deterministic bullet-point summaries from
   extracted content, with an *optional* LLM-assisted path that is
   never required and always clearly gated.

All operations are read-only.  Nothing is written to Moodle.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath

from worsaga.extraction import (
    FILE_PRIORITY,
    clean_text,
    extract_file_text,
    is_boilerplate,
)


# ── Constants ────────────────────────────────────────────────────

MAX_FILES_PER_SECTION = 5
MAX_BULLETS = 6
MIN_BULLET_SCORE = 18

# Keywords for detecting special week types
READING_WEEK_KW = (
    "reading week", "reading period", "independent study",
    "self-study", "consolidation week",
)
EXAM_KW = ("exam", "examination", "final exam", "assessment period")
REVISION_KW = ("revision", "review week", "recap", "review session")

# Keywords that suggest a section contains teaching materials
_MATERIAL_SECTION_KW = (
    "lecture", "seminar", "session", "topic", "week", "materials",
    "readings", "recordings", "slides", "class", "content",
)


# ── Section type detection ───────────────────────────────────────

def classify_section(name: str) -> str:
    """Return the section type: 'reading', 'exam', 'revision', or 'normal'.

    Based on keyword matching against the section name.
    """
    lower = name.lower().strip()
    if any(kw in lower for kw in READING_WEEK_KW):
        return "reading"
    if any(kw in lower for kw in EXAM_KW):
        return "exam"
    if any(kw in lower for kw in REVISION_KW):
        return "revision"
    return "normal"


# ── Section scoring ──────────────────────────────────────────────

def score_section_match(name: str, week: int) -> tuple[int, str]:
    """Score how well a section name matches the target *week*.

    Returns ``(score, section_type)``.
    ``score > 0`` means a match; higher is better.
    ``section_type`` is one of ``'normal'``, ``'reading'``, ``'exam'``,
    ``'revision'``, or ``'general'``.
    """
    name_lower = name.lower().strip()

    # Detect special week types
    stype = classify_section(name)
    if stype != "normal":
        if re.search(rf"\b{week}\b", name_lower):
            return (50, stype)
        return (0, stype)

    # Check numbered heading patterns: "Week 7", "Lecture 7:", etc.
    priority_map = {
        "week": 100, "lecture": 95, "seminar": 90,
        "session": 85, "class": 80, "topic": 75,
    }
    for keyword, base_score in priority_map.items():
        pattern = re.compile(rf"\b{keyword}\s+{week}\b", re.IGNORECASE)
        if pattern.search(name):
            return (base_score, "normal")

    # Check leading section number: "7. Topic Name" or "7 - Topic Name"
    num_match = re.match(r"^(\d+)[.\s\-:]+", name_lower)
    if num_match and int(num_match.group(1)) == week:
        return (70, "normal")

    return (0, "general")


# ── Downloadable file discovery ──────────────────────────────────

def get_downloadable_files(
    modules: list[dict],
    *,
    max_files: int = MAX_FILES_PER_SECTION,
) -> list[dict]:
    """Extract downloadable files from section modules, prioritised by type.

    Returns list of dicts with keys: ``filename``, ``fileurl``,
    ``filesize``, ``module_id``, ``module_name``, ``priority``.

    PDFs first, then PPTX, then other supported formats.
    Deduplicates by ``(fileurl, filename)``.
    """
    files: list[dict] = []
    seen_keys: set[str] = set()

    for mod in modules:
        modname = mod.get("modname", "")
        if modname in ("label", "lti", "forum", "quiz"):
            continue

        mod_id = mod.get("id", 0)
        mod_title = (mod.get("name") or "").strip()

        for content in mod.get("contents", []):
            if content.get("type") != "file":
                continue

            fname = (content.get("filename") or "").strip()
            fileurl = (content.get("fileurl") or "").strip()
            if not fname or not fileurl:
                continue

            ext = PurePosixPath(fname).suffix.lower()
            if ext not in FILE_PRIORITY:
                continue

            key = f"{fileurl}|{fname.lower()}"
            if key in seen_keys:
                continue
            seen_keys.add(key)

            files.append({
                "filename": fname,
                "fileurl": fileurl,
                "filesize": content.get("filesize", 0),
                "module_id": mod_id,
                "module_name": mod_title,
                "priority": FILE_PRIORITY[ext],
            })

    files.sort(key=lambda f: (f["priority"], f["module_id"]))
    return files[:max_files]


# ── Best-section finder ─────────────────────────────────────────

def find_best_section(
    sections: list[dict],
    week: int | str,
) -> tuple[dict | None, str, str]:
    """Find the best Moodle section for the given *week*.

    Returns ``(section, section_type, section_name)``.

    ``section_type`` is one of ``'normal'``, ``'reading'``, ``'exam'``,
    ``'revision'``, ``'fallback'``, or ``'general'`` (nothing found).

    *week* may be an ``int``, a numeric string (``"3"``), or a name
    substring (``"revision"``).  Numeric values use scored matching;
    non-numeric strings fall back to case-insensitive substring search.
    """
    # Normalise: try to treat as int for the numeric path.
    week_int: int | None = None
    if isinstance(week, int):
        week_int = week
    else:
        try:
            week_int = int(str(week).strip())
        except (ValueError, TypeError):
            pass

    # ── Non-numeric string path: substring match on section names ──
    if week_int is None:
        needle = str(week).strip().lower()
        matches: list[tuple[int, dict, str, str]] = []
        for section in sections:
            name = section.get("name", "")
            if needle in name.lower():
                stype = classify_section(name)
                modules = section.get("modules") or []
                files = get_downloadable_files(modules) if modules else []
                matches.append((len(files), section, stype, name))
        if matches:
            matches.sort(key=lambda x: -x[0])
            _, section, stype, name = matches[0]
            return (section, stype, name)
        return (None, "general", "")

    # ── Numeric path: original scored matching ─────────────────────
    scored: list[tuple[int, dict, str, str]] = []
    special_sections: list[tuple[dict, str, str]] = []
    material_candidates: list[tuple[int, dict, str]] = []

    for section in sections:
        name = section.get("name", "")
        name_lower = name.lower().strip()
        modules = section.get("modules") or []
        files = get_downloadable_files(modules) if modules else []

        score, stype = score_section_match(name, week_int)
        if score > 0:
            scored.append((score, section, stype, name))
        elif stype in ("reading", "exam", "revision"):
            special_sections.append((section, stype, name))

        if files:
            heuristic = 0
            if any(kw in name_lower for kw in _MATERIAL_SECTION_KW):
                heuristic += 30

            nums = [int(n) for n in re.findall(r"\b(\d{1,2})\b", name_lower)]
            if nums:
                heuristic += max(0, 20 - (min(abs(n - week_int) for n in nums) * 3))

            sec_idx = section.get("section")
            if isinstance(sec_idx, int):
                heuristic += max(0, 15 - abs(sec_idx - week_int))

            heuristic += min(len(files), MAX_FILES_PER_SECTION) * 4
            material_candidates.append((heuristic, section, name))

    scored.sort(key=lambda x: -x[0])

    # Best direct matches that have files
    for _, section, stype, name in scored:
        if section.get("modules") and get_downloadable_files(section["modules"]):
            return (section, stype, name)

    # Best direct match is a special week — return even without files
    if scored:
        _, section, stype, name = scored[0]
        if stype in ("reading", "exam", "revision"):
            return (section, stype, name)

    # Special week sections discovered outside strict week-number matching
    if special_sections:
        section, stype, name = special_sections[0]
        return (section, stype, name)

    # Adjacent week fallback
    for adj_week in (week_int - 1, week_int + 1):
        if adj_week < 1:
            continue
        for section in sections:
            name = section.get("name", "")
            adj_score, _ = score_section_match(name, adj_week)
            if adj_score > 0 and section.get("modules"):
                files = get_downloadable_files(section["modules"])
                if files:
                    return (section, "fallback", name)

    # Last resort: most material-rich section
    if material_candidates:
        material_candidates.sort(key=lambda x: -x[0])
        _, section, name = material_candidates[0]
        return (section, "fallback", name)

    return (None, "general", "")


# ── Module-level overview ────────────────────────────────────────

def summarize_modules(modules: list[dict]) -> str:
    """Build a concise overview string from a section's modules.

    Groups by type: slides/lectures, readings/cases, exercises/quizzes.
    Returns a short pipe-separated summary string.
    """
    slides: list[str] = []
    readings: list[str] = []
    exercises: list[str] = []

    for mod in modules:
        modname = mod.get("modname", "")
        name = mod.get("name", "").strip()
        if not name:
            continue
        if modname in ("label", "lti", "forum"):
            continue

        name_lower = name.lower()

        if any(kw in name_lower for kw in (
            "slide", "lecture", "deck", "presentation", "whiteboard",
        )):
            slides.append(name)
        elif any(kw in name_lower for kw in (
            "case", "reading", "chapter", "article", "note",
        )):
            readings.append(name)
        elif modname == "quiz" or any(kw in name_lower for kw in (
            "exercise", "quiz", "problem set", "problem_set",
        )):
            exercises.append(name)
        elif modname == "resource":
            readings.append(name)
        elif modname == "folder":
            slides.append(name)
        elif modname == "url":
            readings.append(name)

    parts: list[str] = []
    if slides:
        short = [s[:50] for s in slides[:3]]
        parts.append("Slides: " + ", ".join(short))
    if readings:
        short = [r[:50] for r in readings[:3]]
        extra = len(readings) - 3
        label = "Readings: " + ", ".join(short)
        if extra > 0:
            label += f" (+{extra} more)"
        parts.append(label)
    if exercises:
        parts.append("Exercises: " + ", ".join(e[:50] for e in exercises[:2]))

    return " | ".join(parts) if parts else ""


# ── Deterministic fallback bullets ───────────────────────────────

def fallback_bullets(section_type: str) -> list[str]:
    """Return deterministic fallback bullet strings for special or empty weeks.

    Parameters
    ----------
    section_type : str
        One of ``'reading'``, ``'exam'``, ``'revision'``, or anything
        else (treated as a generic no-material fallback).

    Returns a list of plain bullet strings (no leading marker).
    """
    if section_type == "reading":
        return [
            "Review and consolidate material from previous weeks",
            "Revisit key frameworks and models covered so far",
            "Catch up on any unfinished readings or cases",
            "Identify areas of uncertainty for targeted revision",
        ]
    if section_type == "exam":
        return [
            "Review past exam papers and marking criteria",
            "Consolidate core frameworks and their applications",
            "Practice applying theories to case scenarios",
            "Focus on areas with highest exam weighting",
        ]
    if section_type == "revision":
        return [
            "Revisit core concepts and key definitions",
            "Review lecture summaries and seminar discussion points",
            "Practice structuring answers using key frameworks",
            "Consolidate notes on applications and case studies",
        ]
    # Generic no-material fallback
    return [
        "Materials not yet available \u2014 check Moodle closer to class",
    ]


# ── Fragment merging & line-level synthesis ─────────────────────

# Words that signal a line is a continuation of the previous one.
_CONTINUATION_STARTS = frozenset({
    "and", "or", "but", "nor", "yet", "so", "for", "as", "if",
    "that", "which", "where", "when", "while", "because", "although",
    "though", "in", "on", "at", "by", "with", "from", "to", "than",
    "rather", "such", "not", "between", "versus", "vs", "vs.",
    "e.g.", "i.e.", "ie", "eg",
})

# Words that boost a line's informativeness score.
_SIGNAL_WORDS = (
    "means", "defined as", "refers to", "implies", "leads to",
    "because", "therefore", "however", "although", "whereas",
    "framework", "model", "theory", "explains", "determines",
    "suggests", "relationship between", "effect of", "caused by",
    "result of", "in contrast", "for example", "such as",
    "according to", "argument", "approach", "distinguish",
    "characteristic", "principle", "concept", "assumes",
    "predicts", "incentive", "trade-off", "equilibrium",
    "optimal", "constraint", "mechanism", "strategy",
)

# Light verb / copula indicators — lines containing these are more
# likely to express a proposition rather than being a bare label.
_VERB_INDICATORS = (
    " is ", " are ", " was ", " were ", " has ", " have ", " had ",
    " can ", " may ", " will ", " should ", " must ", " does ",
    " do ", " did ", " provide", " create", " determine",
    " affect", " influence", " lead", " cause", " result",
    " suggest", " show", " explain", " describe", " define",
    " argue", " claim", " increase", " decrease", " reduce",
    " improve", " depend", " require", " allow", " enable",
    " involve", " represent", " reflect", " assume", " predict",
    " maximize", " maximise", " minimize", " minimise",
    " equal", " generate", " produce", " emerge", " occur",
    " specialize", " specialise", " operate", " function",
    " tend", " drive", " shape", " promote", " restrict",
    " focus", " develop", " establish", " demonstrate",
    " indicate", " examine", " discuss", " identify",
)


# Words that, when trailing a line, signal the next line is a continuation.
_TRAILING_CONNECTORS = frozenset({
    "and", "or", "but", "nor", "yet", "so", "whereas", "while",
    "although", "because", "that", "which", "with", "from",
    "for", "in", "on", "at", "by", "to", "as", "of",
})


# Patterns indicating causal / explanatory / definitional structure —
# lines matching these are strong study-note candidates.
_CAUSAL_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in (
        r'\bbecause\b.{3,}',
        r'\bleads?\s+to\b',
        r'\bresults?\s+in\b',
        r'\bcauses?\b.{3,}\b(?:by|of|the|a|an|when)\b',
        r'\bimpl(?:y|ies)\s+that\b',
        r'\bmeans?\s+that\b',
        r'\bdefined\s+as\b',
        r'\brefers?\s+to\b',
        r'\bdistinguish\w*\s+between\b',
        r'\b(?:differs?|different)\s+from\b',
        r'\bin\s+contrast\s+(?:to|with)\b',
        r'\bwhereas\b.{10,}',
        r'\brather\s+than\b',
        r'\b(?:therefore|thus|hence|consequently)\b',
    )
]


def _looks_like_reference(line: str) -> bool:
    """Return True if *line* looks like a citation/reference entry."""
    lower = line.lower().strip()
    if not lower:
        return False

    if (re.search(r'[A-Z]\w+,\s+[A-Z]\.', line) and
            re.search(r'(?:\((?:19|20)\d{2}\w?\)|\b(?:19|20)\d{2}\b)', line)):
        return True

    if re.match(r'^\(?(?:19|20)\d{2}\)?\s*[:.\-]?\s*["“]?chapter\s+\d+', lower):
        return True

    if (re.search(r'\b(?:financial times|journal of|human resource management|'
                  r'harvard business review|academy of management|doi)\b', lower)
            and re.search(r'\b(?:19|20)\d{2}\b', lower)):
        return True

    return False


def _is_continuation(line: str) -> bool:
    """True if *line* looks like it continues the previous line."""
    if not line:
        return False
    # Starts with a lowercase letter → almost always a continuation.
    if line[0].islower():
        return True
    # Starts with a digit + comma/punctuation (e.g. "2, and ..." from a
    # page break splitting "Year\n2, and ...").
    if re.match(r"^\d+[,;)\s]", line):
        return True
    first_word = line.split()[0].lower().rstrip(".,;:")
    return first_word in _CONTINUATION_STARTS


def _ends_mid_thought(line: str) -> bool:
    """True if *line* ends with a connector, suggesting the next line continues it."""
    if not line:
        return False
    last_word = line.rstrip(".,;: ").rsplit(None, 1)[-1].lower()
    return last_word in _TRAILING_CONNECTORS


def _merge_fragments(text: str) -> list[str]:
    """Split *text* into logical content lines, merging broken fragments.

    Lecture-slide text often has hard line breaks mid-sentence.  This
    function joins continuation lines back together and returns a list
    of coherent phrases/sentences.
    """
    raw_lines = text.split("\n")
    merged: list[str] = []
    current: list[str] = []

    for raw in raw_lines:
        stripped = raw.strip()
        if not stripped:
            # Blank line → flush current accumulator.
            if current:
                merged.append(" ".join(current))
                current = []
            continue

        # Merge if this line is a continuation OR the previous line ended
        # mid-thought (trailing connector word).
        prev_continues = current and _ends_mid_thought(current[-1])
        if current and (_is_continuation(stripped) or prev_continues):
            current.append(stripped)
        else:
            if current:
                merged.append(" ".join(current))
            current = [stripped]

    if current:
        merged.append(" ".join(current))

    # For long merged lines (prose paragraphs), split on sentence
    # boundaries so each sentence can be scored independently.
    final: list[str] = []
    for line in merged:
        if len(line) > 200:
            sentences = re.split(r"(?<=[.!?])\s+", line)
            final.extend(s.strip() for s in sentences if s.strip())
        else:
            final.append(line)

    # Drop very short fragments (isolated labels/keywords).
    return [line for line in final if len(line) >= 15]


def _deduplicate_lines(lines: list[str]) -> list[str]:
    """Remove exact and near-duplicate lines (word-level overlap)."""
    result: list[str] = []
    seen_keys: set[str] = set()
    seen_word_sets: list[frozenset[str]] = []

    for line in lines:
        key = re.sub(r"[^\w\s]", "", line.lower())
        key = " ".join(key.split())

        if key in seen_keys:
            continue

        words = frozenset(key.split())
        if len(words) >= 3:
            is_dup = False
            for existing_words in seen_word_sets:
                if len(existing_words) < 3:
                    continue
                overlap = len(words & existing_words)
                threshold = min(len(words), len(existing_words)) * 0.8
                if overlap >= threshold:
                    is_dup = True
                    break
            if is_dup:
                continue
            seen_word_sets.append(words)

        seen_keys.add(key)
        result.append(line)

    return result


def _score_line(line: str) -> float:
    """Score a line for informativeness (higher = better study-note)."""
    score: float = 0.0
    length = len(line)
    lower = line.lower()

    # Length sweet-spot: 40–200 chars.
    if 40 <= length <= 200:
        score += 20
    elif 25 <= length < 40:
        score += 10
    elif length > 200:
        score += 12

    # Signal-word bonus.
    for sw in _SIGNAL_WORDS:
        if sw in lower:
            score += 8

    # Verb presence → propositional content, not a label.
    if any(v in lower for v in _VERB_INDICATORS):
        score += 15

    # Colon inside (likely a definition) — but not a trailing colon.
    if ":" in line and not line.rstrip().endswith(":"):
        colon_pos = line.index(":")
        after_colon = line[colon_pos + 1:].strip()
        # Stronger bonus when the part after : is a substantive explanation.
        if len(after_colon) > 30 and any(v in after_colon.lower() for v in _VERB_INDICATORS):
            score += 12
        else:
            score += 5

    # Causal/explanatory/definitional structure bonus.
    if any(pat.search(lower) for pat in _CAUSAL_PATTERNS):
        score += 12

    # ── Penalties ──
    # All-caps heading.
    if line.isupper() and length < 60:
        score -= 20

    # Trailing question mark (question heading / prompt, not content).
    if line.endswith("?") and length < 100:
        score -= 10

    # Very short → probably a heading/label.
    if length < 25:
        score -= 15

    # Trailing colon → section header, not content.
    if line.rstrip().endswith(":"):
        score -= 10

    # Administrative / logistical content — not study material.
    _ADMIN_SIGNALS = (
        "you are expected", "attendance", "attend all",
        "register", "sign up", "office hours", "email me",
        "submit by", "due date", "deadline", "submission",
        "assessment criteria", "marking scheme", "grading",
        "class participation", "seminar preparation",
        "lectures are", "not recorded", "in person lecture",
        "in person seminar", "virtual lecture", "hybrid",
        "unable to attend", "reasons for absence", "please email",
        "formative", "summative", "coursework worth",
        "essay title", "essay question", "choice of two",
        "receive an email", "receive email", "receive your",
        "instructions to", "more information in",
        "will be published", "will be marked", "will be graded",
        "complete your", "complete online", "complete the questionnaire",
        "see dates", "see moodle", "fortnightly",
        "meeting with", "attend meeting", "your allocated",
        "allocated day", "allocated time", "on campus",
        "share your", "your point of view",
        "unique strengths", "discussion opportunities",
        "group members", "your ideas and progress",
    )
    _admin_count = sum(1 for sig in _ADMIN_SIGNALS if sig in lower)
    if _admin_count >= 1:
        score -= 25
    if _admin_count >= 2:
        score -= 15  # Extra penalty for heavily admin content

    # Lines containing email addresses — contact info, not content.
    if re.search(r'\b[\w.+-]+@[\w.-]+\.\w+\b', lower):
        score -= 25

    # Student-directed instructions (logistics, not content).
    # "You will...", "We will..." without domain signal words
    # are almost always logistics/scheduling lines.
    if re.match(r"^(you will|you should|you need|you must|we will)\b", lower):
        if not any(sw in lower for sw in _SIGNAL_WORDS):
            score -= 20

    # High second-person pronoun density signals instructions to students.
    _you_count = len(re.findall(r'\byou(?:r|rs|rself)?\b', lower))
    if _you_count >= 2:
        score -= 20

    # Meta-instructional lines (questions/prompts to the reader).
    if re.match(r"^(consider|think about|discuss|reflect on|what)\b", lower):
        score -= 20
        if line.endswith("?"):
            score -= 10  # Extra penalty for question prompts

    # Instructional prompts after a heading / colon are not revision notes.
    if re.search(
        r':\s*(discuss|describe|identify|select|outline|create|list|'
        r'compare|consider|reflect|explain)\b',
        lower,
    ):
        score -= 30

    if re.match(r'^\(?multiple\s+choice\)?', lower):
        score -= 40

    # Reflective/meta question prompts: "How might...", "Why do...", etc.
    if re.match(
        r"^(how\s+(?:might|do|can|would|could|should|does|did)|"
        r"why\s+(?:do|does|did|might|would|could|should|is|are)|"
        r"in\s+what\s+way)",
        lower,
    ):
        score -= 20
        if line.endswith("?"):
            score -= 10

    # Direct quotation: starts with opening quote mark.
    if re.match(r'^["\u201c\u2018\u00ab]', line):
        score -= 15
        # Full wrapped quote (also ends with closing quote mark).
        if re.search(r'["\u201d\u2019\u00bb][.!?]?\s*$', line):
            score -= 15

    # Ellipsis suggests partial/attributed quote.
    if '\u2026' in line or re.search(r'(?<!\.)\.\.\.(?!\.)', line):
        score -= 10

    # Course-objective / learning-outcome language.
    _OBJECTIVE_SIGNALS = (
        "identify a range", "you will learn", "you will be able",
        "by the end of", "learning outcome", "module aim",
        "course objective", "aim of this", "goal of this",
        "able to explain", "able to describe", "able to apply",
        "able to identify", "able to analyse", "able to evaluate",
    )
    if any(sig in lower for sig in _OBJECTIVE_SIGNALS):
        score -= 25

    # Blog / group-work / team-admin language.
    _GROUP_ADMIN = (
        "your blog", "blog group", "your team", "team project",
        "group project", "your presentation", "allocated to",
        "will be allocated",
    )
    if any(sig in lower for sig in _GROUP_ADMIN):
        score -= 25

    # Promotional / motivational / slogan-like language.
    _PROMOTIONAL_SIGNALS = (
        "continuous journey", "are unique", "are special",
        "no right or wrong", "it's about", "is key to",
        "is critical to", "is essential to", "is important to",
        "is a journey", "is a process",
    )
    if any(sig in lower for sig in _PROMOTIONAL_SIGNALS):
        score -= 20

    # Platform consent / ToS / survey logistics language.
    _PLATFORM_ADMIN = (
        "in accordance with", "terms of service", "terms and conditions",
        "consent", "privacy policy", "survey will be",
        "proprietary", "commercially sensitive",
        "processed in accordance",
    )
    if any(sig in lower for sig in _PLATFORM_ADMIN):
        score -= 25

    # Weak pronoun-led continuations are often context-dependent fragments.
    if re.match(
        r'^(they|it|this|these|those)\s+'
        r'(have|has|had|are|is|were|was|can|could|may|might|would|will|should)\b',
        lower,
    ):
        score -= 18

    # Parenthetical fragments and example labels rarely make good study notes.
    if re.match(r'^\([^)]{1,140}\)$', line.strip()):
        score -= 45
    if re.match(r'^(example|for example)\s*:', lower):
        score -= 25

    # Time/period labels often introduce contextual narration rather than a concept.
    if re.match(
        r'^(?:\d{1,2}(?:st|nd|rd|th)\s+(?:and\s+\d{1,2}(?:st|nd|rd|th)\s+)?'
        r'c(?:entury)?|\d{4}s?)\s*:',
        lower,
    ):
        score -= 25
    if re.match(
        r'^[a-z][a-z/&\- ]{0,20}:\s+(?:in\s+(?:ancient|medieval|early|late)\b|'
        r'\d{1,2}(?:st|nd|rd|th)\b)',
        lower,
    ):
        score -= 20

    # Conversational first-person quote fragments read badly as study bullets.
    _first_person_count = len(re.findall(r'\b(?:i|we|our|us|my)\b', lower))
    if _first_person_count >= 2:
        score -= 20

    # Bibliography / citation-only lines are not useful bullets.
    if _looks_like_reference(line):
        score -= 45

    # Bare imperative learning objectives (Bloom's taxonomy verbs).
    # "Understand the principles..." is an objective, not study content.
    if re.match(
        r'^(understand|explain|describe|analyse|analyze|evaluate|apply|'
        r'demonstrate|compare|contrast|outline|summarise|summarize|'
        r'define|assess|examine|explore|investigate|recognise|recognize|'
        r'distinguish|classify|illustrate|identify|'
        r'critically\s+\w+)\s+'
        r'(the|how|what|why|key|various|different|a\b|an\b|your)',
        lower,
    ):
        score -= 30

    # Truncated lines ending with a trailing connector/preposition.
    _trail = line.rstrip(".,;: ")
    if _trail:
        _last_word = _trail.rsplit(None, 1)[-1].lower()
        if _last_word in _TRAILING_CONNECTORS or _last_word in {
            "the", "a", "an", "when", "where", "whether", "than",
        }:
            score -= 20

    # Inline bullet markers produce awkward output.
    if re.search(r'[\u2022\u2023\u25e6\u2043]', line):
        score -= 15

    # Quiz / worksheet prompts and assessment instructions.
    if re.search(
        r'\b(?:multiple choice|look(?:ing)?\s+through\s+the\s+list|'
        r'select\s+\d+\s*-\s*\d+|within\s+the\s+body\s+of\s+the\s+essay|'
        r'questionnaire)\b',
        lower,
    ):
        score -= 35

    # Quote-heavy or footnoted lines usually read badly as study notes.
    if re.search(r'["“”‘’]', line) and re.search(r'\d+\s*$', line.strip('”\" ')):
        score -= 25

    # Lines with no content indicators (no signal words AND no verb
    # indicators) are likely headings, agenda items, or structural text.
    if (not any(sw in lower for sw in _SIGNAL_WORDS) and
            not any(v in lower for v in _VERB_INDICATORS)):
        score -= 10

    # Short lines (25–50 chars) without clear verbs: likely agenda/outline items.
    # Uses word-boundary matching to avoid false positives from nouns
    # like "leaders" matching the substring " lead".
    if 25 <= length <= 50:
        _has_verb = bool(re.search(
            r'\b(is|are|was|were|has|have|had|can|may|will|should|must|'
            r'does|do|did|means?|explains?|determines?|suggests?|shows?|'
            r'leads?|causes?|results?|involves?|requires?|allows?|depends?|'
            r'affects?|influences?|creates?|provides?|represents?|reflects?|'
            r'assumes?|predicts?|produces?|tends?|drives?|shapes?|promotes?|'
            r'reduces?|improves?|increases?|decreases?|generates?|emerges?|'
            r'focus(?:es|sed)?|develops?|establishes?|demonstrates?|'
            r'indicates?|examines?|discusses?|identifies?)\b',
            lower,
        ))
        if not _has_verb:
            score -= 25

    # Comma-separated term lists (agenda/outline items, not propositions).
    comma_parts = [p.strip() for p in line.split(',') if p.strip()]
    if len(comma_parts) >= 5:
        score -= 25
    elif len(comma_parts) >= 3:
        if not any(v in lower for v in _VERB_INDICATORS):
            score -= 15

    return score


def _polish_bullet(line: str) -> str:
    """Clean up a line for use as a bullet point."""
    # Strip leading bullet markers the source text may contain.
    line = re.sub(r"^[\-\*\u2022\u2023\u25e6]+\s*", "", line)
    # Strip leading list numbering: "1. ", "2) ", etc.
    line = re.sub(r"^\d+[.\)]\s+", "", line)
    # Clean inline bullet markers into spaces for coherent sentences.
    line = re.sub(r':\s*[\-\*\u2022\u2023\u25e6\u2043]+\s*', ' ', line)
    line = re.sub(r'\s*[\u2022\u2023\u25e6\u2043]\s*', ' ', line)
    # Normalize capitalized helper verbs left behind by inline-bullet cleanup.
    line = re.sub(
        r'\b(which|that|and|but|or)\s+'
        r'(Don[\'’]t|Do|Does|Did|Is|Are|Was|Were|Have|Has|Had|Can|May|Will|Should|Must)\b',
        lambda m: f"{m.group(1)} {m.group(2).lower()}",
        line,
    )
    # Strip full-quote wrapping (opens and closes with quote marks).
    if (re.match(r'^["\u201c\u2018\u00ab]', line) and
            re.search(r'["\u201d\u2019\u00bb][.!?]?\s*$', line)):
        line = re.sub(r'^["\u201c\u2018\u00ab]+\s*', '', line)
        line = re.sub(r'\s*["\u201d\u2019\u00bb]+[.!?]?\d*\s*$', '', line)
    # Strip leading ellipsis with optional quote mark (partial attribution).
    line = re.sub(r'^["\u201c\u2018\u00ab]*[\u2026]+\s*', '', line)
    # Strip leading "For example, " (tangential reference).
    line = re.sub(r'^[Ff]or example,?\s+', '', line)
    line = line.strip()
    # Capitalize first letter.
    if line and line[0].islower():
        line = line[0].upper() + line[1:]
    # Remove trailing incomplete-thought markers.
    line = re.sub(r"[\-\u2013\u2014\u2026]+\s*$", "", line).strip()
    # Trim trailing dangling conjunctions/prepositions (broken continuation).
    line = re.sub(
        r",?\s+\b(and|or|but|whereas|while|although|because|that|which|"
        r"with|from|for|in|on|at|by|to|as|of)\s*$",
        "", line, flags=re.IGNORECASE,
    ).strip()
    # Trim unclosed trailing parenthetical.
    if '(' in line and ')' not in line[line.rindex('('):]:
        line = line[:line.rindex('(')].strip()
    # If the line is long, doesn't end with terminal punctuation, and
    # contains a clause boundary marker, trim to the last complete clause
    # rather than leaving a dangling fragment.
    if len(line) > 150 and not re.search(r'[.!?)]\s*$', line):
        clause_break = re.search(
            r',?\s+(?:whereas|while|although|however)\s+',
            line[80:],
        )
        if clause_break:
            trim_pos = 80 + clause_break.start()
            line = line[:trim_pos].rstrip(',; ')

    # Cap very long bullets at a natural sentence boundary.
    if len(line) > 200:
        m = re.search(r'[.!?]\s', line[:200])
        if m:
            line = line[:m.start() + 1].strip()
        else:
            line = line[:200].rsplit(' ', 1)[0].rstrip('.,;: ')
    return line


# ── Condensation ────────────────────────────────────────────────

# Leading filler phrases stripped case-insensitively from lines.
_LEADING_FILLER = [
    re.compile(p, re.IGNORECASE) for p in (
        r"^it\s+is\s+(?:important|essential|worth|useful)\s+to\s+"
        r"(?:note|understand|recogni[sz]e)\s+that\s+",
        r"^it\s+should\s+be\s+noted\s+that\s+",
        r"^(?:the\s+)?key\s+(?:point|takeaway|insight|message)\s+"
        r"(?:here\s+)?is\s+that\s+",
        r"^the\s+main\s+(?:point|takeaway|insight)\s+is\s+that\s+",
        r"^what\s+this\s+means\s+is\s+that\s+",
        r"^in\s+other\s+words,?\s+",
        r"^that\s+is\s+to\s+say,?\s+",
        r"^put\s+(?:simply|differently),?\s+",
        r"^(?:simply|essentially|basically|fundamentally),?\s+",
        r"^according\s+to\s+[\w\s.,']+?,\s+",
        r"^as\s+[\w\s.,']+?\s+(?:argues?|suggests?|notes?|"
        r"observes?|points?\s+out),?\s+",
        r"^(?:research|evidence)\s+(?:suggests?|shows?|indicates?|"
        r"demonstrates?)\s+that\s+",
        r"^studies\s+(?:have\s+)?(?:shown?|found|demonstrated?|"
        r"indicated?)\s+that\s+",
        r"^see\s+for\s+example:?\s+",
        r"^\(?multiple\s+choice\)?\s*",
    )
]

# Inline patterns stripped from lines (citations, cross-references).
_INLINE_STRIP = [
    re.compile(p) for p in (
        r"\s*\([\w\s.,&]+\d{4}\w?\)",
        r"\s*\[\d+(?:,\s*\d+)*\]",
        r"\s*\((?:see|cf\.?)\s+[^)]{1,40}\)",
        r"\s*\((?:ibid|op\.?\s*cit)\.?\)",
        r"^[A-Z][a-z]+\s+(?:et\s+al\.?\s*)?\(\d{4}\)\s*:\s*",
    )
]


def _condense_line(line: str) -> str:
    """Strip filler phrases, inline citations, and hedging from a line."""
    for pat in _LEADING_FILLER:
        line = pat.sub("", line, count=1)
    line = re.sub(
        r'^[A-Z][A-Za-z\'’.-]+\s*\((?:19|20)\d{2}\)\s*:\s+',
        '',
        line,
    )
    line = re.sub(r'^after\s+[^.]{0,140}\.\s*', '', line, flags=re.IGNORECASE)
    for pat in _INLINE_STRIP:
        line = pat.sub("", line)
    line = line.strip()
    if _looks_like_reference(line):
        return ""
    if line and line[0].islower():
        line = line[0].upper() + line[1:]
    return line


def _reject_final_bullet(line: str) -> bool:
    """Return True when a polished line is still not bullet-worthy."""
    lower = line.lower().strip()
    if not lower:
        return True

    if is_boilerplate(line) or _looks_like_reference(line):
        return True

    if re.match(r'^(week|lecture|seminar|session|topic)\s+\d+\s*[:\-–]', lower):
        return True

    if re.match(r'^\(?multiple\s+choice\)?', lower):
        return True

    if re.match(r'^\([^)]{1,140}\)$', line.strip()):
        return True

    if re.match(r'^(example|for example)\s*:', lower):
        return True

    if re.match(
        r'^(discuss|describe|identify|select|outline|create|list|'
        r'consider|reflect|look\b|understand|explain)\b',
        lower,
    ):
        return True

    if line.endswith('?'):
        return True

    # Trailing colon → section header / intro to a list, not content.
    if line.rstrip().endswith(':'):
        return True

    # Narrative openers that lack domain context (history fragments).
    if re.match(r'^It did\b', line):
        return True
    if re.match(r'^[A-Z][a-z]+ed\s+on\b.*:', line):
        return True
    if re.match(
        r'^(?:\d{1,2}(?:st|nd|rd|th)\s+(?:and\s+\d{1,2}(?:st|nd|rd|th)\s+)?'
        r'c(?:entury)?|\d{4}s?)\s*:',
        lower,
    ):
        return True
    if re.match(
        r'^[a-z][a-z/&\- ]{0,20}:\s+(?:in\s+(?:ancient|medieval|early|late)\b|'
        r'\d{1,2}(?:st|nd|rd|th)\b)',
        lower,
    ):
        return True

    if re.match(
        r'^(they|it|this|these|those)\s+'
        r'(have|has|had|are|is|were|was|can|could|may|might|would|will|should)\b',
        lower,
    ):
        return True

    if re.search(r'["“”‘’]', line) and re.search(r'\b(?:i|we|our|us|my)\b', lower):
        return True

    trail = line.rstrip('.,;: ')
    if trail:
        last_word = trail.rsplit(None, 1)[-1].lower()
        if last_word in _TRAILING_CONNECTORS | {'the', 'a', 'an', 'when', 'where', 'whether', 'than'}:
            return True

    return False


# ── Diverse bullet selection ───────────────────────────────────

# Stop words excluded from topic similarity calculations.
_STOP_WORDS = frozenset({
    "the", "a", "an", "is", "are", "was", "were", "be", "been",
    "being", "have", "has", "had", "do", "does", "did", "will",
    "would", "shall", "should", "may", "might", "must", "can",
    "could", "and", "but", "or", "nor", "not", "so", "yet",
    "for", "at", "by", "from", "in", "into", "of", "on", "to",
    "with", "as", "if", "that", "than", "this", "these", "those",
    "it", "its", "they", "them", "their", "he", "she", "his",
    "her", "we", "our", "you", "your", "more", "most", "also",
    "how", "what", "when", "where", "which", "who", "why",
    "about", "between", "through", "during", "before", "after",
})


def _content_words(text: str) -> set[str]:
    """Extract content words from text (lowered, stop words removed)."""
    words = set(re.sub(r'[^\w\s]', '', text.lower()).split())
    return words - _STOP_WORDS


def _select_diverse(
    scored_lines: list[tuple[str, float]],
    max_items: int,
) -> list[tuple[str, float]]:
    """Select items maximising quality and topic diversity (MMR-style).

    For each slot after the first, the candidate with the best combined
    score of (quality - similarity_penalty) is chosen.
    """
    if len(scored_lines) <= max_items:
        return list(scored_lines)

    selected: list[tuple[str, float]] = [scored_lines[0]]
    sel_words: list[set[str]] = [_content_words(scored_lines[0][0])]
    candidates = list(scored_lines[1:])

    while len(selected) < max_items and candidates:
        best_idx = 0
        best_combined = -float('inf')

        for i, (line, score) in enumerate(candidates):
            words = _content_words(line)
            max_sim = 0.0
            for sw in sel_words:
                if words and sw:
                    union = len(words | sw)
                    sim = len(words & sw) / union if union else 0.0
                    if sim > max_sim:
                        max_sim = sim

            combined = score - (max_sim * 40)
            if combined > best_combined:
                best_combined = combined
                best_idx = i

        line, score = candidates[best_idx]
        selected.append((line, score))
        sel_words.append(_content_words(line))
        candidates.pop(best_idx)

    return selected


def build_deterministic_summary(
    file_texts: list[tuple[str, str]],
    *,
    max_bullets: int = MAX_BULLETS,
) -> list[str]:
    """Build bullet-point study notes from extracted file texts.

    Pipeline:
    1. Clean and merge broken fragments into coherent lines.
    2. Deduplicate near-identical lines.
    3. Score each line for informativeness.
    4. Select the top-scoring, diverse set of bullets.
    5. Polish formatting.

    Parameters
    ----------
    file_texts : list[tuple[str, str]]
        Each entry is ``(filename, raw_text)``.
    max_bullets : int
        Maximum number of bullet points.

    Returns a list of plain bullet strings (no leading marker).
    Falls back to an empty list if nothing useful can be extracted.
    """
    all_lines: list[str] = []
    for _, raw_text in file_texts:
        cleaned = clean_text(raw_text)
        if cleaned:
            all_lines.extend(_merge_fragments(cleaned))

    if not all_lines:
        return []

    unique_lines = _deduplicate_lines(all_lines)
    if not unique_lines:
        return []

    # Score and rank.
    scored = [(line, _score_line(line)) for line in unique_lines]
    scored.sort(key=lambda x: -x[1])

    # Quality floor: exclude lines below minimum informativeness.
    scored = [(line, s) for line, s in scored if s >= MIN_BULLET_SCORE]

    # Diversity-aware selection (MMR-style). Keep a wider pool so we can
    # drop weak final bullets without running out of candidates.
    selected = _select_diverse(scored, max(max_bullets * 3, max_bullets))

    # Condense and polish each bullet.
    bullets: list[str] = []
    seen_keys: set[str] = set()
    for line, _ in selected:
        condensed = _condense_line(line)
        polished = _polish_bullet(condensed)
        if not polished or _reject_final_bullet(polished):
            continue
        key = re.sub(r'[^\w\s]', '', polished.lower())
        key = ' '.join(key.split())
        if not key or key in seen_keys:
            continue
        seen_keys.add(key)
        bullets.append(polished)
        if len(bullets) >= max_bullets:
            break
    return bullets


# ── Optional LLM-assisted summary ────────────────────────────────

def build_llm_summary(
    file_texts: list[tuple[str, str]],
    *,
    llm_callable: callable | None = None,
    max_bullets: int = MAX_BULLETS,
    max_input_chars: int = 6000,
) -> list[str] | None:
    """Attempt an LLM-assisted summary. Returns None if unavailable.

    Parameters
    ----------
    file_texts : list[tuple[str, str]]
        Each entry is ``(filename, raw_text)``.
    llm_callable : callable, optional
        A function ``f(prompt: str) -> str`` that returns the LLM's
        text response.  If None, this function is a no-op.
    max_bullets : int
        Requested number of bullets.
    max_input_chars : int
        Truncate combined text before sending to LLM.

    Returns
    -------
    list[str] | None
        Bullet strings, or None if the LLM path is unavailable or fails.
    """
    if llm_callable is None:
        return None

    combined_parts: list[str] = []
    for _, raw_text in file_texts:
        cleaned = clean_text(raw_text)
        if cleaned:
            combined_parts.append(cleaned)

    full_text = "\n".join(combined_parts)
    if not full_text.strip():
        return None

    truncated = full_text[:max_input_chars]

    prompt = (
        f"You are a study assistant. Given the following lecture/course material, "
        f"produce exactly {max_bullets} concise bullet points summarizing the key "
        f"insights a student should focus on. Each bullet should be a single sentence. "
        f"Output plain lines, one per bullet point.\n\n"
        f"Material:\n{truncated}"
    )

    try:
        response = llm_callable(prompt)
        if not response or not isinstance(response, str):
            return None

        raw_lines = [
            line.strip() for line in response.strip().splitlines()
            if line.strip()
        ]
        # Strip leading bullet markers the LLM may add
        cleaned_lines: list[str] = []
        for line in raw_lines:
            line = re.sub(r"^[\-\*\u2022\u2023\u25e6\d\.\)\s]+", "", line).strip()
            if line:
                cleaned_lines.append(line)
        if len(cleaned_lines) >= 2:
            return cleaned_lines[:max_bullets]
        return None
    except Exception:
        return None


# ── High-level summary builder ───────────────────────────────────

def build_summary(
    file_texts: list[tuple[str, str]],
    *,
    section_type: str = "normal",
    llm_callable: callable | None = None,
    max_bullets: int = MAX_BULLETS,
) -> dict:
    """Build a complete study summary for a set of extracted file texts.

    Returns a dict with:
        - ``bullets``: list of bullet-point strings
        - ``method``: ``'llm'``, ``'extractive'``, or ``'fallback'``
        - ``section_type``: the input section type
        - ``file_count``: number of files that contributed text

    The pipeline:
    1. If ``llm_callable`` is provided, try the LLM path first.
    2. Fall back to deterministic extractive summary.
    3. If extraction yields nothing, use deterministic fallback bullets.
    """
    file_count = sum(1 for _, t in file_texts if t.strip())

    # Try LLM path (optional, never required)
    if llm_callable is not None:
        llm_result = build_llm_summary(
            file_texts,
            llm_callable=llm_callable,
            max_bullets=max_bullets,
        )
        if llm_result:
            return {
                "bullets": llm_result,
                "method": "llm",
                "section_type": section_type,
                "file_count": file_count,
            }

    # Deterministic extractive summary
    det_result = build_deterministic_summary(file_texts, max_bullets=max_bullets)
    if det_result:
        return {
            "bullets": det_result,
            "method": "extractive",
            "section_type": section_type,
            "file_count": file_count,
        }

    # Fallback — differentiate "no files" from "files but no study content"
    if file_count > 0:
        return {
            "bullets": [
                "This section contains introductory or administrative material",
                "Core subject content likely begins in subsequent weeks",
            ],
            "method": "fallback",
            "section_type": section_type,
            "file_count": file_count,
        }
    return {
        "bullets": fallback_bullets(section_type),
        "method": "fallback",
        "section_type": section_type,
        "file_count": 0,
    }


def format_bullets(bullets: list[str], *, marker: str = "\u2022") -> str:
    """Format bullet strings into a display-ready multi-line string."""
    return "\n".join(f"  {marker} {b}" for b in bullets)
