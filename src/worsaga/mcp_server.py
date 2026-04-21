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

from typing import Any

from mcp.server.fastmcp import FastMCP

from worsaga.client import MoodleClient
from worsaga.config import MoodleConfig
from worsaga.deadlines import get_upcoming_deadlines
from worsaga.materials import (
    MaterialSelectionError,
    candidate_summary,
    download_material as _download_material,
    get_section_materials,
    search_course_content as _search_content,
    select_material as _select_material,
)
from worsaga.summaries import build_weekly_summary, format_bullets

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
def list_courses() -> list[dict[str, Any]]:
    """List all Moodle courses the authenticated user is enrolled in."""
    return _get_client().get_courses()


@mcp.tool()
def get_deadlines(lookahead_days: int = 14) -> list[dict[str, Any]]:
    """Return upcoming assignment and quiz deadlines sorted by due date.

    Parameters
    ----------
    lookahead_days : int
        How many days ahead to look (default 14).
    """
    return get_upcoming_deadlines(_get_client(), lookahead_days=lookahead_days)


@mcp.tool()
def get_course_contents(course_id: int) -> list[dict[str, Any]]:
    """Return all sections and modules for a specific course.

    Parameters
    ----------
    course_id : int
        The Moodle course ID.
    """
    return _get_client().get_course_contents(course_id)


@mcp.tool()
def get_week_materials(course_id: int, week: str) -> list[dict[str, Any]]:
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
    return get_section_materials(sections, course_id, week, base_url=client.base_url)


@mcp.tool()
def search_course_content(course_id: int, query: str) -> list[dict[str, Any]]:
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
    return _search_content(sections, query)


@mcp.tool()
def get_weekly_summary(course_id: int, week: int) -> dict[str, Any]:
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
    result = build_weekly_summary(_get_client(), course_id, week)
    result["formatted"] = format_bullets(result["bullets"])
    return result


@mcp.tool()
def download_material(
    course_id: int,
    week: str,
    match: str = "",
    index: int = -1,
    output_dir: str = "",
) -> dict[str, Any]:
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
        return {
            "error": f"No materials found for week '{week}'.",
            "candidates": [],
        }

    sel_match = match or None
    sel_index = index if index >= 0 else None

    try:
        chosen = _select_material(materials, match=sel_match, index=sel_index)
    except MaterialSelectionError as exc:
        candidates = [
            candidate_summary(c, i)
            for i, c in enumerate(exc.candidates)
        ]
        return {
            "error": str(exc),
            "candidates": candidates,
        }

    try:
        result = _download_material(
            client, chosen,
            output_dir=output_dir or None,
        )
    except RuntimeError as exc:
        return {"error": str(exc)}

    return result


def main() -> None:
    """Entry point when running ``python -m worsaga.mcp_server``."""
    mcp.run()


if __name__ == "__main__":
    main()
