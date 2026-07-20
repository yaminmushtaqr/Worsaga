"""MCP server for worsaga.

Worsaga is an open-source, local-first, read-only study toolkit for Moodle.
Moodle is the only supported LMS provider today.

Exposes tools:
    - list_courses
    - get_deadlines
    - get_course_contents
    - get_week_materials
    - search_course_content
    - get_weekly_summary
    - download_material
    - extract_material
    - get_grades
    - get_grade_summary
    - get_assignments
    - get_assignment_status
    - get_course_forums
    - get_forum_discussions
    - get_latest_updates
    - get_notifications
    - get_messages
    - get_digest
    - get_calendar_events
    - sync_now
    - get_changes
    - build_search_index
    - search_text
    - export_study_pack
    - get_autosync_status

Requires the ``mcp`` extra: pip install worsaga[mcp]

Demo mode: run with ``WORSAGA_DEMO=1`` to serve built-in fake course
data without Moodle credentials or network access.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from worsaga.assignments import get_assignment_status as _get_assignment_status
from worsaga.assignments import get_assignments as _get_assignments
from worsaga.calendar import get_calendar_events as _get_calendar_events
from pathlib import Path

from worsaga.client import DownloadError, MoodleClient
from worsaga.config import MoodleConfig, default_downloads_dir
from worsaga.deadlines import get_upcoming_deadlines
from worsaga.demo import DemoMoodleClient, demo_mode_enabled
from worsaga.digest import get_digest as _get_digest
from worsaga.forums import get_course_forums as _get_course_forums
from worsaga.forums import get_forum_discussions as _get_forum_discussions
from worsaga.forums import get_latest_updates as _get_latest_updates
from worsaga.grades import get_grade_summary as _get_grade_summary
from worsaga.grades import get_grades as _get_grades
from worsaga.extraction import MAX_TEXT_PER_FILE
from worsaga.materials import (
    MaterialSelectionError,
    candidate_summary,
    download_material as _download_material,
    extract_material_content as _extract_material_content,
    get_section_materials,
    search_course_content as _search_content,
    select_material as _select_material,
    strip_file_urls,
)
from worsaga.autosync import autosync_status as _autosync_status
from worsaga.studypack import build_study_pack as _build_study_pack
from worsaga.studypack import write_study_pack as _write_study_pack
from worsaga.summaries import build_weekly_summary, format_bullets
from worsaga.textindex import (
    INDEX_MAX_FILES_PER_RUN,
    TextIndexError,
    build_text_index as _build_text_index,
    search_text_index as _search_text_index,
)
from worsaga.sync import (
    SYNC_LOOKAHEAD_DAYS,
    get_recent_changes as _get_recent_changes,
    run_sync as _run_sync,
)
from worsaga.messages import get_messages as _get_messages
from worsaga.messages import get_notifications as _get_notifications

mcp = FastMCP("worsaga")

# Lazily initialised so the server module can be imported without
# credentials (e.g. for tests or tooling introspection).
_client: MoodleClient | None = None


def _get_client() -> MoodleClient:
    global _client
    if _client is None:
        if demo_mode_enabled():
            # WORSAGA_DEMO=1: serve built-in fake data with no
            # credentials and no network access.
            _client = DemoMoodleClient()
        else:
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
def get_grades(course_id: int | None = None) -> list[dict[str, Any]]:
    """Return normalized grade items for one course or all enrolled courses."""
    return _get_grades(_get_client(), course_id=course_id)


@mcp.tool()
def get_grade_summary(course_id: int | None = None) -> dict[str, Any]:
    """Return aggregate grade status counts for one course or all courses."""
    return _get_grade_summary(_get_client(), course_id=course_id)


@mcp.tool()
def get_assignments(course_id: int | None = None) -> list[dict[str, Any]]:
    """Return normalized assignment statuses for one course or all courses."""
    return _get_assignments(_get_client(), course_id=course_id)


@mcp.tool()
def get_assignment_status(course_id: int, assignment_id: int) -> dict[str, Any]:
    """Return one normalized assignment status record."""
    return _get_assignment_status(
        _get_client(),
        course_id=course_id,
        assignment_id=assignment_id,
    )


@mcp.tool()
def get_course_forums(course_id: int) -> list[dict[str, Any]]:
    """Return forum containers for a course."""
    return _get_course_forums(_get_client(), course_id=course_id)


@mcp.tool()
def get_forum_discussions(
    course_id: int,
    forum_id: int | None = None,
) -> list[dict[str, Any]]:
    """Return forum discussions for one forum or all forums in a course."""
    return _get_forum_discussions(
        _get_client(),
        course_id=course_id,
        forum_id=forum_id,
    )


@mcp.tool()
def get_latest_updates(
    course_id: int | None = None,
    since_days: int = 7,
) -> list[dict[str, Any]]:
    """Return recent forum updates."""
    return _get_latest_updates(
        _get_client(),
        course_id=course_id,
        since_days=since_days,
    )


@mcp.tool()
def get_notifications(unread_only: bool = False) -> list[dict[str, Any]]:
    """Return popup notifications without marking them read."""
    return _get_notifications(_get_client(), unread_only=unread_only)


@mcp.tool()
def get_messages(since_days: int | None = None) -> list[dict[str, Any]]:
    """Return messages without marking them read."""
    return _get_messages(_get_client(), since_days=since_days)


@mcp.tool()
def get_digest(since_days: int = 1) -> dict[str, Any]:
    """Return a live study digest with partial-failure warnings."""
    return _get_digest(_get_client(), since_days=since_days)


@mcp.tool()
def get_calendar_events(
    course_id: int | None = None,
    days: int = 30,
    week: str | None = None,
) -> list[dict[str, Any]]:
    """Return calendar events for one course or all courses, optionally by week."""
    return _get_calendar_events(
        _get_client(),
        course_id=course_id,
        days=days,
        week=week,
    )


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

    Raw Moodle ``file_url`` values are not included; downloads always go
    through ``download_material()``, which authenticates internally.

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
    return strip_file_urls(
        get_section_materials(sections, course_id, week, base_url=client.base_url)
    )


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
def get_weekly_summary(course_id: int, week: str) -> dict[str, Any]:
    """Generate a study summary for a specific teaching week of a course.

    Finds the best matching section, extracts text from downloadable
    materials, and returns deterministic bullet-point study notes with
    appropriate fallbacks for reading weeks, revision weeks, exam periods,
    and weeks with no materials.

    Parameters
    ----------
    course_id : int
        The Moodle course ID.
    week : str
        Teaching week number or name query, such as "3", "revision", or
        "reading".
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
        Optional subdirectory (relative path) inside Worsaga's own
        downloads directory. Files are always saved under that
        directory — absolute paths and path traversal are rejected.
    """
    downloads_root = default_downloads_dir()
    if output_dir:
        candidate = (downloads_root / output_dir).resolve()
        if not candidate.is_relative_to(downloads_root.resolve()):
            return {
                "error": (
                    "output_dir must be a relative path inside the Worsaga "
                    f"downloads directory ({downloads_root})."
                ),
                "error_code": "invalid_output_dir",
            }
        dest_dir: Path = candidate
    else:
        dest_dir = downloads_root

    client = _get_client()
    chosen = _select_week_material(client, course_id, week, match, index)
    if "error" in chosen:
        return chosen

    try:
        result = _download_material(client, chosen, output_dir=dest_dir)
    except DownloadError as exc:
        return {"error": str(exc), "error_code": exc.code}
    except RuntimeError as exc:
        return {"error": str(exc)}

    return result


def _select_week_material(
    client: MoodleClient,
    course_id: int,
    week: str,
    match: str,
    index: int,
) -> dict[str, Any]:
    """Discover materials for *week* and select exactly one.

    Returns the chosen material record, or a structured error dict
    (with a ``candidates`` list where applicable) that the calling tool
    passes straight back to the agent.
    """
    sections = client.get_course_contents(course_id)
    materials = get_section_materials(
        sections, course_id, week, base_url=client.base_url,
    )

    if not materials:
        return {
            "error": f"No materials found for week '{week}'.",
            "candidates": [],
        }

    try:
        return _select_material(
            materials,
            match=match or None,
            index=index if index >= 0 else None,
        )
    except MaterialSelectionError as exc:
        candidates = [
            candidate_summary(c, i)
            for i, c in enumerate(exc.candidates)
        ]
        return {
            "error": str(exc),
            "candidates": candidates,
        }


@mcp.tool()
def extract_material(
    course_id: int,
    week: str,
    match: str = "",
    index: int = -1,
    max_chars: int = 0,
    clean: bool = True,
) -> dict[str, Any]:
    """Extract per-page structured text from a material (in memory).

    Fetches the file with authenticated credentials and returns its
    text page by page (slide by slide for PPTX) — each page carries
    ``text``, ``markdown``, ``image_count``, ``has_low_text_density``,
    and ``warnings``. Nothing is written to disk; use
    ``download_material()`` when you need the file itself.

    Light cleaning is applied by default and preserves educational
    content — figure captions, learning objectives, references. Pages
    dominated by images are flagged rather than silently empty.

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
    max_chars : int
        Cap on total extracted text across pages (0 = default cap).
    clean : bool
        Strip boilerplate lines (page numbers, copyright footers,
        repeated headers). Set False for the raw extractor output.
    """
    client = _get_client()
    chosen = _select_week_material(client, course_id, week, match, index)
    if "error" in chosen:
        return chosen

    try:
        return _extract_material_content(
            client, chosen,
            max_chars=max_chars if max_chars > 0 else MAX_TEXT_PER_FILE,
            clean=clean,
        )
    except DownloadError as exc:
        return {"error": str(exc), "error_code": exc.code}
    except RuntimeError as exc:
        return {"error": str(exc)}


@mcp.tool()
def sync_now(lookahead_days: int = SYNC_LOOKAHEAD_DAYS) -> dict[str, Any]:
    """Sync metadata into the local cache and return detected changes.

    Fetches metadata-only snapshots — deadlines, file metadata, grades,
    and forum discussions; never file contents — into the local SQLite
    cache and diffs them against the previous sync. Detected changes
    (new deadlines, new files, grade updates, forum updates) are
    returned and recorded so ``get_changes()`` can replay them later.

    The first sync for a site establishes a baseline and reports no
    changes. Tokens and authenticated URLs are never stored in the
    cache.

    Parameters
    ----------
    lookahead_days : int
        Deadline look-ahead window in days (default 60).
    """
    return _run_sync(_get_client(), lookahead_days=lookahead_days)


@mcp.tool()
def get_changes(
    since_days: int = 7,
    category: str = "",
) -> list[dict[str, Any]]:
    """Return change events recorded by previous syncs (no network).

    Reads the local cache only; run ``sync_now()`` first to detect new
    changes. Each event has ``kind`` (``new_deadline``,
    ``deadline_changed``, ``new_file``, ``file_updated``,
    ``grade_updated``, ``new_forum_discussion``,
    ``forum_discussion_updated``), course context, a ``title``, compact
    ``before``/``after`` views, and ``detected_at``.

    Parameters
    ----------
    since_days : int
        Lookback window in days (default 7).
    category : str
        Optional filter: ``deadlines``, ``files``, ``grades``, or
        ``forums``.
    """
    try:
        return _get_recent_changes(
            _get_client().base_url,
            since_days=since_days,
            category=category or None,
        )
    except ValueError as exc:
        return [{"error": str(exc)}]


@mcp.tool()
def build_search_index(
    course_id: int = 0,
    week: str = "",
    max_files: int = INDEX_MAX_FILES_PER_RUN,
) -> dict[str, Any]:
    """Build or update the local full-text search index.

    Fetches supported course materials (PDF, PPTX, DOCX, TXT) in memory
    with authenticated credentials, extracts their text, and stores it
    page by page in a local SQLite full-text index for ``search_text()``.
    Files unchanged since the last build are skipped without a fetch, so
    repeated calls are cheap and resume where the previous run's file
    budget stopped. Tokens and authenticated URLs are never stored.

    Parameters
    ----------
    course_id : int
        Limit indexing to one course (0 = all enrolled courses).
    week : str
        Only index sections matching this week number or name
        (empty = the whole course).
    max_files : int
        Cap on files fetched this run (default 100).
    """
    try:
        return _build_text_index(
            _get_client(),
            course_id=course_id or None,
            week=week or None,
            max_files=max_files,
        )
    except TextIndexError as exc:
        return {"error": str(exc), "error_code": "index_unavailable"}
    except ValueError as exc:
        return {"error": str(exc)}


@mcp.tool()
def search_text(
    query: str,
    course_id: int = 0,
    limit: int = 20,
) -> dict[str, Any]:
    """Full-text search over indexed material text (no network).

    Searches the local index built by ``build_search_index()`` and
    returns the best-matching pages — each hit carries course, section,
    file, 1-based ``page``, a bracket-highlighted ``snippet``, and a
    relevance ``score`` (higher is better). The ``index`` stats in the
    result distinguish "no match" from "nothing indexed yet"; run
    ``build_search_index()`` first in the latter case.

    Parameters
    ----------
    query : str
        Search terms; all terms must match (AND).
    course_id : int
        Limit hits to one course (0 = all courses).
    limit : int
        Maximum hits to return (default 20).
    """
    try:
        return _search_text_index(
            _get_client().base_url,
            query,
            course_id=course_id or None,
            limit=limit,
        )
    except TextIndexError as exc:
        return {"error": str(exc), "error_code": "index_unavailable"}


@mcp.tool()
def export_study_pack(
    course_id: int,
    week: str,
    output_dir: str = "",
    include_markdown: bool = False,
) -> dict[str, Any]:
    """Export a Markdown study pack for a course week.

    Builds a single Markdown document — study notes, a materials
    overview, and the extracted per-page content of every supported
    file in the week's section — and writes it inside Worsaga's own
    downloads directory. The response reports the written ``path`` and
    pack metadata; set ``include_markdown=True`` to also return the
    full Markdown inline. No tokens or authenticated URLs appear in
    the pack or the response.

    Parameters
    ----------
    course_id : int
        The Moodle course ID.
    week : str
        Week number (e.g. "3") or section name query.
    output_dir : str
        Optional subdirectory (relative path) inside Worsaga's own
        downloads directory. Absolute paths and path traversal are
        rejected.
    include_markdown : bool
        Also return the full Markdown content inline (default False).
    """
    downloads_root = default_downloads_dir()
    if output_dir:
        candidate = (downloads_root / output_dir).resolve()
        if not candidate.is_relative_to(downloads_root.resolve()):
            return {
                "error": (
                    "output_dir must be a relative path inside the Worsaga "
                    f"downloads directory ({downloads_root})."
                ),
                "error_code": "invalid_output_dir",
            }
        dest_dir: Path = candidate
    else:
        dest_dir = downloads_root

    try:
        result = _build_study_pack(_get_client(), course_id, week)
    except DownloadError as exc:
        return {"error": str(exc), "error_code": exc.code}
    except RuntimeError as exc:
        return {"error": str(exc)}

    path = _write_study_pack(
        result["markdown"], dest_dir, result["suggested_filename"],
    )
    if not include_markdown:
        result = {
            key: value for key, value in result.items() if key != "markdown"
        }
    result["path"] = str(path)
    return result


@mcp.tool()
def get_autosync_status() -> dict[str, Any]:
    """Report whether a scheduled background sync is registered.

    Read-only: inspects the platform scheduler (Task Scheduler on
    Windows, launchd on macOS, a systemd user timer on Linux) and the
    local install record, and changes nothing. Installing or removing
    the scheduled sync modifies system state and is deliberately CLI
    only — direct the user to ``worsaga auto-sync install`` /
    ``worsaga auto-sync remove`` (both support ``--dry-run``).
    """
    return _autosync_status()


def main() -> None:
    """Entry point when running ``python -m worsaga.mcp_server``."""
    mcp.run()


if __name__ == "__main__":
    main()
