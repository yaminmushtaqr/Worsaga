"""Markdown study-pack export for a course week.

A study pack is a single self-contained Markdown document for one
teaching week: deterministic study notes, a materials overview, and the
full extracted per-page content of the section's supported files (up to
``MAX_PACK_FILES``; a larger section is included in listed order with
an explicit warning). It is built entirely from data fetched in memory
through the authenticated client — each file is downloaded once and
used for both the summary bullets and the content section.

``build_study_pack`` is the shared orchestrator used by the CLI
(``worsaga study-pack``) and the MCP server (``export_study_pack``).
The returned markdown and metadata contain **no tokens, no raw
``file_url`` values, and no authenticated URLs** — the entire response
(markdown, course names, bullets, file names, warnings) is passed
through :func:`worsaga.cache.sanitize_payload` as a belt-and-braces
redaction.

All operations are read-only. Nothing is written to Moodle.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from worsaga.cache import sanitize_payload
from worsaga.client import DownloadError
from worsaga.extraction import MAX_TEXT_PER_FILE, extract_file_structured
from worsaga.materials import _reserve_path, _sanitize_filename
from worsaga.sections import find_best_section, get_downloadable_files
from worsaga.summaries import build_summary

if TYPE_CHECKING:
    from worsaga.client import MoodleClient

#: Hard cap on files downloaded per study pack.
MAX_PACK_FILES = 8


def _format_size(size: int) -> str:
    """Return a compact human-readable size."""
    size = int(size or 0)
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    if size >= 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size} B"


def _page_label(file_type: str) -> str:
    return "Slide" if file_type == "pptx" else "Page"


def study_pack_filename(course_label: str, week: int | str) -> str:
    """Return a filesystem-safe study pack filename."""
    week_part = _sanitize_filename(str(week)) or "week"
    course_part = _sanitize_filename(str(course_label)) or "course"
    return f"{course_part}-week-{week_part}-study-pack.md"


def build_study_pack(
    client: "MoodleClient",
    course_id: int,
    week: int | str,
    *,
    sections: list[dict] | None = None,
    max_files: int = MAX_PACK_FILES,
    on_file: Callable[[str], None] | None = None,
    now: int | None = None,
) -> dict[str, Any]:
    """Build a Markdown study pack for one course week.

    Parameters
    ----------
    client : MoodleClient
        Used to fetch course contents (when *sections* is None) and to
        download each selected file in memory.
    course_id : int
        The Moodle course ID.
    week : int | str
        Week number or a name query (e.g. ``"revision"``).
    sections : list[dict], optional
        Pre-fetched course sections (fetched when None).
    max_files : int
        Cap on files downloaded for the pack.
    on_file : callable, optional
        Invoked as ``on_file(filename)`` before each download, for CLI
        progress reporting.
    now : int, optional
        Unix timestamp for the generation date (defaults to wall time).

    Returns
    -------
    dict
        ``markdown`` plus ``course_id``, ``course_shortname``,
        ``course_fullname``, ``week``, ``section_name``,
        ``section_type``, ``summary_method``, ``bullets``, ``files``
        (name/size/pages per included file), ``suggested_filename``,
        and ``warnings``.
    """
    generated_at = int(time.time()) if now is None else int(now)

    course_shortname = ""
    course_fullname = ""
    for course in client.get_courses():
        if course.get("id") == course_id:
            course_shortname = str(course.get("shortname", ""))
            course_fullname = str(course.get("fullname", ""))
            break

    if sections is None:
        sections = client.get_course_contents(course_id)
    section, section_type, section_name = find_best_section(sections, week)

    warnings: list[str] = []
    file_texts: list[tuple[str, str]] = []
    extracted: list[dict[str, Any]] = []

    files = []
    if section and section.get("modules"):
        # Enumerate everything supported (the helper's own default cap
        # is smaller than ours), then apply this pack's cap loudly.
        all_files = get_downloadable_files(
            section["modules"], max_files=10_000,
        )
        if len(all_files) > max_files:
            warnings.append(
                f"Including the first {max_files} of {len(all_files)} "
                "supported files in this section."
            )
        files = all_files[:max_files]
    for finfo in files:
        url = finfo.get("fileurl", "")
        name = finfo.get("filename", "")
        if not url or not name:
            continue
        if on_file is not None:
            on_file(name)
        try:
            data = client.download_file(url)
        except DownloadError as exc:
            warnings.append(f"Skipped {name}: {exc}")
            continue
        if not data:
            warnings.append(f"Skipped {name}: empty download")
            continue
        result = extract_file_structured(
            data, name, max_chars=MAX_TEXT_PER_FILE, clean=True,
        )
        result["file_size"] = finfo.get("filesize", 0)
        result["module_name"] = finfo.get("module_name", "")
        extracted.append(result)
        for warning in result.get("warnings", []):
            warnings.append(f"{name}: {warning}")
        text = "\n".join(
            page.get("text", "") for page in result.get("pages", [])
        ).strip()
        if text:
            file_texts.append((name, text))

    summary = build_summary(file_texts, section_type=section_type)

    date_str = time.strftime("%Y-%m-%d", time.localtime(generated_at))
    course_label = course_shortname or str(course_id)
    title_week = f"Week {week}" if str(week).isdigit() else str(week).title()

    lines: list[str] = []
    lines.append(f"# {course_label}: {title_week} — {section_name or 'Untitled'}")
    lines.append("")
    source = f'Moodle course "{course_fullname}"' if course_fullname else "Moodle"
    lines.append(f"> Generated by Worsaga from {source} on {date_str}.")
    lines.append("")
    lines.append("## Study notes")
    lines.append("")
    for bullet in summary["bullets"]:
        lines.append(f"- {bullet}")
    lines.append("")
    lines.append(f"_Notes built deterministically ({summary['method']})._")
    lines.append("")
    lines.append("## Materials")
    lines.append("")
    if extracted:
        lines.append("| File | Size | Pages |")
        lines.append("| --- | --- | --- |")
        for result in extracted:
            file_name = str(result.get("filename", "")).replace("|", "\\|")
            lines.append(
                f"| {file_name} "
                f"| {_format_size(result.get('file_size', 0))} "
                f"| {len(result.get('pages', []))} |"
            )
    else:
        lines.append("_No downloadable materials in this section._")
    lines.append("")

    for result in extracted:
        pages = result.get("pages", [])
        if not any(str(page.get("text", "")).strip() for page in pages):
            continue
        lines.append(f"## {result.get('filename', '')}")
        lines.append("")
        label = _page_label(str(result.get("file_type", "")))
        for page in pages:
            body = str(page.get("markdown") or page.get("text") or "").strip()
            if not body:
                continue
            if len(pages) > 1:
                lines.append(f"### {label} {page.get('page', 0)}")
                lines.append("")
            lines.append(body)
            lines.append("")

    markdown = "\n".join(lines).rstrip() + "\n"

    # Sanitize the whole response, not just the markdown: course names,
    # bullets, file names, and warnings all originate from Moodle data
    # and pass through the same redaction as the cache boundary.
    return sanitize_payload({
        "course_id": course_id,
        "course_shortname": course_shortname,
        "course_fullname": course_fullname,
        "week": week,
        "section_name": section_name,
        "section_type": section_type,
        "summary_method": summary["method"],
        "bullets": summary["bullets"],
        "files": [
            {
                "file_name": result.get("filename", ""),
                "file_size": result.get("file_size", 0),
                "page_count": len(result.get("pages", [])),
            }
            for result in extracted
        ],
        "markdown": markdown,
        "suggested_filename": study_pack_filename(course_label, week),
        "warnings": warnings,
    })


def write_study_pack(
    markdown: str,
    dest_dir: str | Path,
    filename: str,
) -> Path:
    """Write *markdown* into *dest_dir* and return the path.

    The destination name is collision-safe: an existing file is never
    overwritten — a numeric suffix is added instead. Content is always
    written UTF-8 so packs are portable regardless of the platform's
    legacy default encoding.
    """
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    path = _reserve_path(dest / _sanitize_filename(filename))
    try:
        path.write_text(markdown, encoding="utf-8")
    except OSError:
        path.unlink(missing_ok=True)
        raise
    return path


__all__ = [
    "MAX_PACK_FILES",
    "build_study_pack",
    "study_pack_filename",
    "write_study_pack",
]
