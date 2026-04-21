"""Deterministic study-summary builder from Moodle course materials.

The pipeline is:

1. **Section pick** — :mod:`worsaga.sections` scores/ranks sections to
   find the best match for a given teaching week, with smart fallback
   for reading weeks, revision weeks, exam periods, and weeks with no
   materials.
2. **Material filtering** — the most useful downloadable files,
   deduplicated and prioritised by format (PDF > PPTX > DOCX > TXT).
3. **Text extraction** — pull text from the selected files
   (:mod:`worsaga.extraction`).
4. **Summary generation** — deterministic bullet-point summaries from
   extracted content, using the line-level pipeline in
   :mod:`worsaga.summary_text`, with fallback canned bullets for reading
   weeks, exam periods, and sections with no usable content.

All operations are read-only. Nothing is written to Moodle.
"""

from __future__ import annotations

import re
from typing import Callable

from worsaga.extraction import clean_text, extract_file_text
from worsaga.sections import find_best_section, get_downloadable_files
from worsaga.summary_text import (
    _condense_line,
    _deduplicate_lines,
    _merge_fragments,
    _polish_bullet,
    _reject_final_bullet,
    _score_line,
    _select_diverse,
)


# ── Constants ────────────────────────────────────────────────────

MAX_BULLETS = 6
MIN_BULLET_SCORE = 18


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


# ── Deterministic extractive summary ─────────────────────────────

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


# ── High-level summary builder ───────────────────────────────────

def build_summary(
    file_texts: list[tuple[str, str]],
    *,
    section_type: str = "normal",
    max_bullets: int = MAX_BULLETS,
) -> dict:
    """Build a complete study summary for a set of extracted file texts.

    Returns a dict with:
        - ``bullets``: list of bullet-point strings
        - ``method``: ``'extractive'`` or ``'fallback'``
        - ``section_type``: the input section type
        - ``file_count``: number of files that contributed text

    Pipeline: deterministic extractive summary first; if extraction
    yields nothing, return deterministic fallback bullets suitable for
    the given *section_type*.
    """
    file_count = sum(1 for _, t in file_texts if t.strip())

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


# ── Shared weekly summary orchestration ──────────────────────────


def build_weekly_summary(
    client,
    course_id: int,
    week: int | str,
    *,
    sections: list[dict] | None = None,
    on_extract: Callable[[str], None] | None = None,
) -> dict:
    """End-to-end weekly summary: section pick → download → extract → bullets.

    Single entry point used by both the CLI and MCP server so the two
    surfaces share one orchestration path.

    Parameters
    ----------
    client : MoodleClient
        Used to fetch course contents (when *sections* is None) and to
        download each selected file.
    course_id : int
        Included in the returned dict for caller convenience; also used
        to fetch sections when they are not pre-supplied.
    week : int | str
        Week number or a name query (e.g. ``"revision"``).
    sections : list[dict], optional
        Pre-fetched course sections. When None, fetched via
        ``client.get_course_contents(course_id)``.
    on_extract : callable, optional
        Invoked as ``on_extract(filename)`` immediately before each file
        download. Useful for progress reporting from the CLI.

    Returns
    -------
    dict
        The result of :func:`build_summary` with ``section_name``,
        ``week`` and ``course_id`` fields added.
    """
    if sections is None:
        sections = client.get_course_contents(course_id)
    section, section_type, section_name = find_best_section(sections, week)

    file_texts: list[tuple[str, str]] = []
    if section and section.get("modules"):
        files = get_downloadable_files(section["modules"])
        for finfo in files:
            url = finfo["fileurl"]
            if not url:
                continue
            if on_extract is not None:
                on_extract(finfo["filename"])
            data = client.download_file(url)
            if data:
                text = extract_file_text(data, finfo["filename"], clean=True)
                if text:
                    file_texts.append((finfo["filename"], text))

    result = build_summary(file_texts, section_type=section_type)
    result["section_name"] = section_name
    result["week"] = week
    result["course_id"] = course_id
    return result
