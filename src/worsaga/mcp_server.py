"""MCP server for worsaga.

Worsaga is a study kit for university LMS systems.
Moodle is supported today. Blackboard and Canvas are planned next.

Exposes tools:
    - list_courses
    - get_deadlines
    - get_course_contents
    - get_week_materials
    - search_course_content
    - get_weekly_summary
    - download_material

Requires the ``mcp`` extra: pip install worsaga[mcp]
"""

from __future__ import annotations

import json

from mcp.server.fastmcp import FastMCP

from worsaga.client import MoodleClient
from worsaga.config import MoodleConfig
from worsaga.deadlines import get_upcoming_deadlines
from worsaga.extraction import extract_file_text
from worsaga.materials import (
    MaterialSelectionError,
    _candidate_summary,
    download_material as _download_material,
    get_section_materials,
    search_course_content as _search_content,
    select_material as _select_material,
)
from worsaga.summaries import (
    build_summary,
    find_best_section,
    format_bullets,
    get_downloadable_files,
)

mcp = FastMCP("worsaga")

# Lazily initialised so the server module can be imported without
# credentials (e.g. for tests or tooling introspection).
_client: MoodleClient | None = None


def _get_client() -> MoodleClient:
    global _client
    if _client is None:
        _client = MoodleClient(MoodleConfig.load())
    return _client


@mcp.tool()
def list_courses() -> str:
    """List all Moodle courses the authenticated user is enrolled in."""
    courses = _get_client().get_courses()
    return json.dumps(courses, indent=2)


@mcp.tool()
def get_deadlines(lookahead_days: int = 14) -> str:
    """Return upcoming assignment and quiz deadlines sorted by due date.

    Parameters
    ----------
    lookahead_days : int
        How many days ahead to look (default 14).
    """
    deadlines = get_upcoming_deadlines(_get_client(), lookahead_days=lookahead_days)
    return json.dumps(deadlines, indent=2)


@mcp.tool()
def get_course_contents(course_id: int) -> str:
    """Return all sections and modules for a specific course.

    Parameters
    ----------
    course_id : int
        The Moodle course ID.
    """
    contents = _get_client().get_course_contents(course_id)
    return json.dumps(contents, indent=2)


@mcp.tool()
def get_week_materials(course_id: int, week: str) -> str:
    """List downloadable materials for a specific teaching week (discovery only).

    Returns metadata about available files — file names, sizes, types, and
    sections — but does NOT download them. To fetch a file, pass the same
    course_id and week to ``download_material()``, which handles authentication
    internally.

    The ``file_url`` field in each record is the raw Moodle URL retained for
    provenance; do not fetch it directly (it requires token authentication).

    Parameters
    ----------
    course_id : int
        The Moodle course ID.
    week : str
        Week number (e.g. "1") or a substring to match against section names
        (e.g. "Revision"). Numeric matching is based on explicit week-like
        labels in section names, not Moodle's raw section slot number.
    """
    client = _get_client()
    sections = client.get_course_contents(course_id)
    materials = get_section_materials(sections, course_id, week, base_url=client.base_url)
    return json.dumps(materials, indent=2)


@mcp.tool()
def search_course_content(course_id: int, query: str) -> str:
    """Search section and module names within a course.

    Useful for finding where a topic lives without knowing the week number.

    Parameters
    ----------
    course_id : int
        The Moodle course ID.
    query : str
        Case-insensitive search term to match against section and module names.
    """
    sections = _get_client().get_course_contents(course_id)
    results = _search_content(sections, query)
    return json.dumps(results, indent=2)


@mcp.tool()
def get_weekly_summary(course_id: int, week: int) -> str:
    """Generate a study summary for a specific teaching week of a course.

    Finds the best matching section, extracts text from downloadable
    materials, and returns deterministic bullet-point study notes with
    appropriate fallbacks for reading weeks, revision weeks, exam periods,
    and weeks with no materials.

    Parameters
    ----------
    course_id : int
        The Moodle course ID.
    week : int
        The teaching week number.
    """
    client = _get_client()
    sections = client.get_course_contents(course_id)
    section, section_type, section_name = find_best_section(sections, week)

    file_texts: list[tuple[str, str]] = []
    if section and section.get("modules"):
        files = get_downloadable_files(section["modules"])
        for finfo in files:
            url = finfo["fileurl"]
            if not url:
                continue
            data = client.download_file(url)
            if data:
                text = extract_file_text(data, finfo["filename"], clean=True)
                if text:
                    file_texts.append((finfo["filename"], text))

    result = build_summary(file_texts, section_type=section_type)
    result["section_name"] = section_name
    result["week"] = week
    result["course_id"] = course_id
    result["formatted"] = format_bullets(result["bullets"])
    return json.dumps(result, indent=2)


@mcp.tool()
def download_material(
    course_id: int,
    week: str,
    match: str = "",
    index: int = -1,
    output_dir: str = "",
) -> str:
    """Download a material file from a teaching week (authenticated).

    This is the primary way to fetch files from Moodle. It discovers
    materials for the given week, selects one, and downloads it using
    authenticated credentials. The token is never exposed in the
    response.

    Typical workflow: call ``get_week_materials()`` first to see what
    is available, then call this tool with ``match`` or ``index`` to
    fetch a specific file.

    If multiple materials match, returns a structured error with a
    candidate list so the caller can refine with *match* or *index*.

    Parameters
    ----------
    course_id : int
        The Moodle course ID.
    week : str
        Week number (e.g. "3") or section name substring.
    match : str
        Optional substring to filter candidates by file or module name.
    index : int
        Zero-based index to pick from matching materials (-1 = auto).
    output_dir : str
        Directory to save the file (empty = current working directory).
    """
    client = _get_client()
    sections = client.get_course_contents(course_id)
    materials = get_section_materials(
        sections, course_id, week, base_url=client.base_url,
    )

    if not materials:
        return json.dumps({
            "error": f"No materials found for week '{week}'.",
            "candidates": [],
        }, indent=2)

    sel_match = match or None
    sel_index = index if index >= 0 else None

    try:
        chosen = _select_material(materials, match=sel_match, index=sel_index)
    except MaterialSelectionError as exc:
        candidates = [
            _candidate_summary({**c, "_index": i})
            for i, c in enumerate(exc.candidates)
        ]
        return json.dumps({
            "error": str(exc),
            "candidates": candidates,
        }, indent=2)

    try:
        result = _download_material(
            client, chosen,
            output_dir=output_dir or None,
        )
    except RuntimeError as exc:
        return json.dumps({"error": str(exc)}, indent=2)

    return json.dumps(result, indent=2)


def main() -> None:
    """Entry point when running ``python -m worsaga.mcp_server``."""
    mcp.run()


if __name__ == "__main__":
    main()
