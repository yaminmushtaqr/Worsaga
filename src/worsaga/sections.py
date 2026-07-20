"""Section matching and downloadable-file discovery.

Helpers for picking the most relevant Moodle section for a given
teaching week and extracting the downloadable files attached to it.

The pipeline consumers (CLI and the summary builder) both rely on
these to answer "which section is this week, and what files does it
have?" before any text extraction runs.

All operations are read-only.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath

from worsaga.extraction import FILE_PRIORITY


# ── Constants ────────────────────────────────────────────────────

MAX_FILES_PER_SECTION = 5

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
