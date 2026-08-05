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
    - get_connection_info

Requires the ``mcp`` extra: pip install worsaga[mcp]

Demo mode: run with ``WORSAGA_DEMO=1`` to serve built-in fake course
data without Moodle credentials or network access.

Structured domain errors
-------------------------
Tools return an agent-branchable ``{"error", "error_code", ...}`` dict for
expected domain failures rather than raising (which FastMCP would surface
as an ``isError`` string built from raw Moodle DB wording). The
``error_code`` values form a small, stable vocabulary — see
:data:`ERROR_CODES`. Genuinely unexpected failures still raise.
"""

from __future__ import annotations

import functools
import json
from typing import Any, Callable, get_origin

from mcp.server.fastmcp import FastMCP

from worsaga.assignments import get_assignment_status as _get_assignment_status
from worsaga.assignments import get_assignments as _get_assignments
from worsaga.cache import sanitize_payload
from worsaga.calendar import get_calendar_events as _get_calendar_events
from pathlib import Path

from worsaga.client import (
    AssignmentNotFoundError,
    CourseNotFoundError,
    DownloadError,
    ForumNotFoundError,
    MoodleClient,
    MoodleRateLimitedError,
)
from worsaga.config import MoodleConfig, default_downloads_dir
from worsaga.courses import (
    CourseAmbiguousError,
    CourseResolutionError,
    resolve_course_id,
)
from worsaga.deadlines import get_upcoming_deadlines
from worsaga.demo import DemoMoodleClient, demo_mode_enabled
from worsaga.doctor import ConnectionCheckError, build_connection_info
from worsaga.digest import get_digest as _get_digest
from worsaga.forums import get_course_forums as _get_course_forums
from worsaga.forums import get_forum_discussions as _get_forum_discussions
from worsaga.forums import get_latest_updates as _get_latest_updates
from worsaga.grades import get_grade_summary as _get_grade_summary
from worsaga.grades import get_grades as _get_grades
from worsaga.extraction import MAX_TEXT_PER_FILE
from worsaga.materials import (
    MaterialSelectionError,
    build_course_contents,
    candidate_summary,
    download_material as _download_material,
    extract_material_content as _extract_material_content,
    get_section_materials,
    search_course_content as _search_content,
    sections_matching_week,
    select_material as _select_material,
    strip_file_urls,
)
from worsaga.models import as_int, course_record
from worsaga.sections import (
    WeekNotFoundError,
    section_names,
    week_not_found_message,
)
from worsaga.autosync import autosync_status as _autosync_status
from worsaga.principal import PrincipalMismatchError, known_principal
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


# The complete, stable vocabulary of ``error_code`` values a tool may return
# in a structured ``{"error", "error_code"}`` dict. Documented here so agents
# can branch on a small closed set. The download/extraction codes come from
# :class:`worsaga.client.DownloadError` and are surfaced verbatim.
ERROR_CODES = (
    "course_not_found",     # course id/name not enrolled or does not exist
    "course_ambiguous",     # course name/prefix matched more than one course
    "assignment_not_found",  # assignment id does not exist / not accessible
    "forum_not_found",      # forum id is not one of the course's forums
    "week_not_found",       # week query matched no section
    "invalid_output_dir",   # output_dir escaped the downloads directory
    "index_unavailable",    # local search index could not be opened
    # a local store (sync cache / search index) belongs to a different
    # Moodle account than the one this server is authenticated as
    "principal_mismatch",
    # another Worsaga process (a watch loop, the scheduled auto-sync, a
    # second agent) already held this site's sync lock, so sync_now made
    # no requests at all. Retrying immediately will hit the same lock;
    # read the cache with get_changes(), or try again shortly.
    "sync_in_progress",
    # DownloadError.code values (download_material / extract_material) and
    # get_connection_info auth/network failures:
    "auth", "not_found", "network", "oversize", "invalid_url", "empty",
    # the Moodle site asked for fewer requests (HTTP 429/503) and the
    # retries allowed for one request ran out. Not a bad token and not an
    # unreachable site: wait and try again.
    "rate_limited",
)

# Deterministic upper bound on the serialized ``extract_material`` response.
# The per-page ``text`` cap alone did not bound the response, because each
# page also carries a same-size ``markdown`` field — a 150-page PDF could
# reach ~240k chars. This caps the whole payload.
MAX_EXTRACT_RESPONSE_CHARS = 130_000


def _returns_a_list(fn: Callable[..., Any]) -> bool:
    """Whether *fn* is annotated as returning a list.

    A tool declared ``-> list[...]`` has to answer with a list even when
    it is answering with an error, which is the shape ``get_changes``
    already established.
    """
    annotation = fn.__annotations__.get("return")
    if isinstance(annotation, str):
        return annotation.lstrip().startswith("list")
    return get_origin(annotation) is list


def tool(*decorator_args: Any, **decorator_kwargs: Any):
    """Register an MCP tool that reports rate limiting in the usual shape.

    Every tool that touches the network can meet HTTP 429/503, and
    ``rate_limited`` is in :data:`ERROR_CODES` precisely so an agent can
    branch on it. Wrapping registration in one place is what makes that
    promise true for all 26 tools instead of the two that happened to
    catch it — hand-wrapping each body would guarantee the next tool
    added forgets.

    Only :class:`~worsaga.client.MoodleRateLimitedError` is translated
    here. Every other failure keeps whatever handling its own tool
    already has, so nothing else changes shape.
    """

    def decorate(fn: Callable[..., Any]) -> Callable[..., Any]:
        as_list = _returns_a_list(fn)

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return fn(*args, **kwargs)
            except MoodleRateLimitedError as exc:
                payload = {"error": str(exc), "error_code": "rate_limited"}
                return [payload] if as_list else payload

        return mcp.tool(*decorator_args, **decorator_kwargs)(wrapper)

    return decorate


def _course_not_found(exc: CourseNotFoundError) -> dict[str, Any]:
    """Return the structured error dict for a missing course."""
    return {"error": str(exc), "error_code": "course_not_found"}


def _assignment_not_found(exc: AssignmentNotFoundError) -> dict[str, Any]:
    """Return the structured error dict for a missing assignment."""
    return {"error": str(exc), "error_code": "assignment_not_found"}


def _forum_not_found(exc: ForumNotFoundError) -> dict[str, Any]:
    """Return the structured error dict for a forum outside the course."""
    return {"error": str(exc), "error_code": "forum_not_found"}


def _course_ambiguous(exc: CourseAmbiguousError) -> dict[str, Any]:
    """Return the structured error dict for an ambiguous course-name prefix.

    Mirrors ``download_material``'s candidate precedent: the ``candidates``
    list lets the agent pick the intended course by ``id`` or exact
    ``shortname`` without a separate ``list_courses`` round-trip.
    """
    return {
        "error": str(exc),
        "error_code": "course_ambiguous",
        "candidates": [
            {
                "id": as_int(course.get("id"), 0),
                "shortname": str(course.get("shortname") or ""),
                "fullname": str(course.get("fullname") or ""),
            }
            for course in exc.candidates
        ],
    }


def _resolve_course_arg(
    client: MoodleClient, course_id: int | str | None,
) -> tuple[int | None, dict[str, Any] | None]:
    """Resolve an MCP ``course_id`` argument to an int id or a structured error.

    Accepts what every course-taking tool now accepts: ``None`` (all
    enrolled courses, where the tool supports it), an ``int`` or digit
    string, or a course short-code — an exact case-insensitive match or an
    unambiguous prefix. All of them go through
    :func:`worsaga.courses.resolve_course_id`, so a numeric id is confirmed
    against the enrolled-course list rather than used verbatim.

    Returns ``(resolved_id, None)`` on success, or ``(None, error_dict)``
    where the error dict carries ``error_code`` ``"course_not_found"`` (no
    match, including an id outside the enrolment list) or
    ``"course_ambiguous"`` (a prefix matched several courses, with a
    ``candidates`` list).
    """
    if course_id is None:
        return None, None
    try:
        return resolve_course_id(client, course_id), None
    except CourseAmbiguousError as exc:
        return None, _course_ambiguous(exc)
    except CourseResolutionError as exc:
        return None, {"error": str(exc), "error_code": "course_not_found"}


def _numeric_course_id(course_id: int | str | None) -> int | None:
    """Return *course_id* as an int when it is already numeric, else None.

    ``0`` and ``""`` are the "all courses" sentinels and read as None. Used
    only by the offline search tool, which must not turn a numeric filter
    into a network round-trip.
    """
    if course_id is None or isinstance(course_id, bool):
        return None
    if isinstance(course_id, int):
        return course_id or None
    try:
        return int(str(course_id).strip()) or None
    except ValueError:
        return None


@tool()
def list_courses() -> list[dict[str, Any]]:
    """List all Moodle courses the authenticated user is enrolled in.

    Returns one compact record per course — ``id``, ``shortname``,
    ``fullname``, ``category``, ``start_at``, ``end_at`` — normalized
    through Worsaga's record layer. The bulky, agent-irrelevant fields the
    raw Moodle payload carries (HTML course ``summary``, the course image,
    ``enrolledusercount``, progress) are dropped; no HTML and no
    token-bearing URLs appear in the response.
    """
    return [course_record(course) for course in _get_client().get_courses()]


@tool()
def get_deadlines(lookahead_days: int = 14) -> list[dict[str, Any]]:
    """Return upcoming assignment and quiz deadlines sorted by due date.

    Parameters
    ----------
    lookahead_days : int
        How many days ahead to look (default 14).
    """
    return get_upcoming_deadlines(_get_client(), lookahead_days=lookahead_days)


@tool()
def get_grades(
    course_id: int | str | None = None,
) -> list[dict[str, Any]] | dict[str, Any]:
    """Return normalized grade items for one course or all enrolled courses.

    *course_id* accepts a numeric id **or** a course short-code such as
    ``"ECON101"`` (case-insensitive; an unambiguous prefix like ``"ECON"``
    also resolves) — omit it for all courses. An unknown name returns a
    structured ``{"error", "error_code": "course_not_found"}`` dict; an
    ambiguous prefix returns ``{"error", "error_code": "course_ambiguous",
    "candidates"}`` so you can pick the intended course.

    An empty list can mean either "no grade items" or "gradebook access
    denied" for some enrollments (common for non-academic containers) —
    per-course access warnings are not part of this list; call
    ``get_grade_summary()`` to see them alongside the status counts.
    """
    client = _get_client()
    resolved, error = _resolve_course_arg(client, course_id)
    if error is not None:
        return error
    try:
        return _get_grades(client, course_id=resolved)
    except CourseNotFoundError as exc:
        return _course_not_found(exc)


@tool()
def get_grade_summary(course_id: int | str | None = None) -> dict[str, Any]:
    """Return aggregate grade status counts for one course or all courses.

    *course_id* accepts a numeric id or a course short-code (exact or an
    unambiguous prefix); omit it for all courses. An unknown name returns
    ``{"error", "error_code": "course_not_found"}``; an ambiguous prefix
    returns ``{"error", "error_code": "course_ambiguous", "candidates"}``.
    """
    client = _get_client()
    resolved, error = _resolve_course_arg(client, course_id)
    if error is not None:
        return error
    try:
        return _get_grade_summary(client, course_id=resolved)
    except CourseNotFoundError as exc:
        return _course_not_found(exc)


@tool()
def get_assignments(
    course_id: int | str | None = None,
) -> list[dict[str, Any]] | dict[str, Any]:
    """Return normalized assignment statuses for one course or all courses.

    *course_id* accepts a numeric id or a course short-code (exact or an
    unambiguous prefix); omit it for all courses. An unknown name returns
    ``{"error", "error_code": "course_not_found"}``; an ambiguous prefix
    returns ``{"error", "error_code": "course_ambiguous", "candidates"}``.
    """
    client = _get_client()
    resolved, error = _resolve_course_arg(client, course_id)
    if error is not None:
        return error
    try:
        return _get_assignments(client, course_id=resolved)
    except CourseNotFoundError as exc:
        return _course_not_found(exc)


@tool()
def get_assignment_status(
    course_id: int | str, assignment_id: int,
) -> dict[str, Any]:
    """Return one normalized assignment status record.

    *course_id* accepts a numeric id or a course short-code (exact or an
    unambiguous prefix). Returns a structured ``{"error", "error_code"}``
    dict when the course name is ambiguous (``course_ambiguous``), the
    course is not found (``course_not_found``), or the assignment id is not
    in the course (``assignment_not_found``).
    """
    client = _get_client()
    resolved, error = _resolve_course_arg(client, course_id)
    if error is not None:
        return error
    try:
        return _get_assignment_status(
            client,
            course_id=resolved,
            assignment_id=assignment_id,
        )
    except CourseNotFoundError as exc:
        return _course_not_found(exc)
    except AssignmentNotFoundError as exc:
        return _assignment_not_found(exc)


@tool()
def get_course_forums(
    course_id: int | str,
) -> list[dict[str, Any]] | dict[str, Any]:
    """Return forum containers for a course.

    *course_id* accepts a numeric id or a course short-code (exact or an
    unambiguous prefix). An unknown name returns ``{"error", "error_code":
    "course_not_found"}``; an ambiguous prefix returns ``{"error",
    "error_code": "course_ambiguous", "candidates"}``.
    """
    client = _get_client()
    resolved, error = _resolve_course_arg(client, course_id)
    if error is not None:
        return error
    try:
        return _get_course_forums(client, course_id=resolved)
    except CourseNotFoundError as exc:
        return _course_not_found(exc)


@tool()
def get_forum_discussions(
    course_id: int | str,
    forum_id: int | None = None,
) -> list[dict[str, Any]] | dict[str, Any]:
    """Return forum discussions for one forum or all forums in a course.

    *course_id* accepts a numeric id or a course short-code (exact or an
    unambiguous prefix). An unknown name returns ``{"error", "error_code":
    "course_not_found"}``; an ambiguous prefix returns ``{"error",
    "error_code": "course_ambiguous", "candidates"}``. A *forum_id* that is
    not one of that course's forums returns ``{"error", "error_code":
    "forum_not_found"}``.
    """
    client = _get_client()
    resolved, error = _resolve_course_arg(client, course_id)
    if error is not None:
        return error
    try:
        return _get_forum_discussions(
            client,
            course_id=resolved,
            forum_id=forum_id,
        )
    except CourseNotFoundError as exc:
        return _course_not_found(exc)
    except ForumNotFoundError as exc:
        return _forum_not_found(exc)


@tool()
def get_latest_updates(
    course_id: int | str | None = None,
    since_days: int = 7,
) -> list[dict[str, Any]] | dict[str, Any]:
    """Return recent forum updates.

    *course_id* accepts a numeric id or a course short-code (exact or an
    unambiguous prefix); omit it for all courses. An unknown name returns
    ``{"error", "error_code": "course_not_found"}``; an ambiguous prefix
    returns ``{"error", "error_code": "course_ambiguous", "candidates"}``.
    """
    client = _get_client()
    resolved, error = _resolve_course_arg(client, course_id)
    if error is not None:
        return error
    try:
        return _get_latest_updates(
            client,
            course_id=resolved,
            since_days=since_days,
        )
    except CourseNotFoundError as exc:
        return _course_not_found(exc)


@tool()
def get_notifications(unread_only: bool = False) -> list[dict[str, Any]]:
    """Return popup notifications without marking them read."""
    return _get_notifications(_get_client(), unread_only=unread_only)


@tool()
def get_messages(since_days: int | None = None) -> list[dict[str, Any]]:
    """Return messages without marking them read."""
    return _get_messages(_get_client(), since_days=since_days)


@tool()
def get_digest(since_days: int = 1) -> dict[str, Any]:
    """Return a live study digest with partial-failure warnings."""
    return _get_digest(_get_client(), since_days=since_days)


@tool()
def get_calendar_events(
    course_id: int | str | None = None,
    days: int = 30,
    week: str | None = None,
) -> list[dict[str, Any]] | dict[str, Any]:
    """Return calendar events for one course or all courses, optionally by week.

    *course_id* accepts a numeric id or a course short-code (exact or an
    unambiguous prefix); omit it for all courses. An unknown name returns
    ``{"error", "error_code": "course_not_found"}``; an ambiguous prefix
    returns ``{"error", "error_code": "course_ambiguous", "candidates"}``.
    """
    client = _get_client()
    resolved, error = _resolve_course_arg(client, course_id)
    if error is not None:
        return error
    try:
        return _get_calendar_events(
            client,
            course_id=resolved,
            days=days,
            week=week,
        )
    except CourseNotFoundError as exc:
        return _course_not_found(exc)


@tool()
def get_course_contents(
    course_id: int | str,
) -> list[dict[str, Any]] | dict[str, Any]:
    """Return a compact, sanitized map of a course's sections and modules.

    One record per section — ``section_id``, ``section_num``,
    ``section_name``, a plain-text ``summary`` (section HTML stripped), and
    ``modules`` — where each module carries ``module_id``, ``module_name``,
    ``module_type``, a human ``view_url``, and (for file resources) a
    ``files`` list of token-free metadata (``file_name``, ``file_size``,
    ``mime_type``, ``time_modified``, ``dedupe_key``). This replaces the
    verbatim Moodle payload, which is far larger (inline-styled HTML
    summaries and per-file authenticated URLs).

    Raw ``file_url`` values are never included — to fetch a file, pass the
    course and week to ``download_material()`` (the ``dedupe_key`` here
    matches ``get_week_materials``). An unknown course name returns
    ``{"error", "error_code": "course_not_found"}``; an ambiguous prefix
    returns ``{"error", "error_code": "course_ambiguous", "candidates"}``.

    Parameters
    ----------
    course_id : int | str
        The Moodle course ID, or a course short-code (exact match or an
        unambiguous prefix, e.g. ``"ECON101"``).
    """
    client = _get_client()
    resolved, error = _resolve_course_arg(client, course_id)
    if error is not None:
        return error
    try:
        sections = client.get_course_contents(resolved)
    except CourseNotFoundError as exc:
        return _course_not_found(exc)
    # sanitize_payload is a belt-and-braces pass over the compact shape:
    # even though build_course_contents omits file_url, this guarantees no
    # token-bearing key or value can survive to the response.
    return sanitize_payload(
        build_course_contents(sections, resolved, base_url=client.base_url)
    )


@tool()
def get_week_materials(
    course_id: int | str, week: str,
) -> list[dict[str, Any]] | dict[str, Any]:
    """List downloadable materials for a specific teaching week (discovery only).

    Returns metadata about available files — file names, sizes, types, and
    sections — but does NOT download them. To fetch a file, pass the same
    course_id and week to ``download_material()``, which handles authentication
    internally.

    Raw Moodle ``file_url`` values are not included; downloads always go
    through ``download_material()``, which authenticates internally.

    If *week* matches no section at all, returns a structured error dict
    (``error``, ``error_code="week_not_found"``, ``available_sections``)
    instead of a silently empty list. A section that matches but has no
    downloadable files is a valid empty state and returns ``[]``.

    Parameters
    ----------
    course_id : int | str
        The Moodle course ID, or a course short-code (exact match or an
        unambiguous prefix, e.g. ``"ECON101"``).
    week : str
        Week number (e.g. "1") or a substring to match against section names
        (e.g. "Revision"). Numeric matching is based on explicit week-like
        labels in section names, not Moodle's raw section slot number.

    An unknown course name returns ``{"error", "error_code":
    "course_not_found"}``; an ambiguous prefix returns ``{"error",
    "error_code": "course_ambiguous", "candidates"}``.
    """
    client = _get_client()
    resolved, error = _resolve_course_arg(client, course_id)
    if error is not None:
        return error
    try:
        sections = client.get_course_contents(resolved)
    except CourseNotFoundError as exc:
        return _course_not_found(exc)
    if not sections_matching_week(sections, week):
        return {
            "error": week_not_found_message(week, resolved),
            "error_code": "week_not_found",
            "available_sections": section_names(sections),
        }
    return strip_file_urls(
        get_section_materials(sections, resolved, week, base_url=client.base_url)
    )


@tool()
def search_course_content(
    course_id: int | str, query: str,
) -> list[dict[str, Any]] | dict[str, Any]:
    """Search section and module names within a course.

    Useful for finding where a topic lives without knowing the week number.
    An unknown course name returns ``{"error", "error_code":
    "course_not_found"}``; an ambiguous prefix returns ``{"error",
    "error_code": "course_ambiguous", "candidates"}``.

    Parameters
    ----------
    course_id : int | str
        The Moodle course ID, or a course short-code (exact match or an
        unambiguous prefix, e.g. ``"ECON101"``).
    query : str
        Case-insensitive search term to match against section and module names.
    """
    client = _get_client()
    resolved, error = _resolve_course_arg(client, course_id)
    if error is not None:
        return error
    try:
        sections = client.get_course_contents(resolved)
    except CourseNotFoundError as exc:
        return _course_not_found(exc)
    return _search_content(sections, query)


@tool()
def get_weekly_summary(course_id: int | str, week: str) -> dict[str, Any]:
    """Generate a study summary for a specific teaching week of a course.

    Finds the best matching section, extracts text from downloadable
    materials, and returns deterministic bullet-point study notes with
    appropriate fallbacks for reading weeks, revision weeks, exam periods,
    and weeks with no materials.

    Parameters
    ----------
    course_id : int | str
        The Moodle course ID, or a course short-code (exact match or an
        unambiguous prefix, e.g. ``"ECON101"``).
    week : str
        Teaching week number or name query, such as "3", "revision", or
        "reading".

    An unknown course name returns ``{"error", "error_code":
    "course_not_found"}`` and an ambiguous prefix returns ``{"error",
    "error_code": "course_ambiguous", "candidates"}``. If *week* matches no
    section at all, returns a structured error dict (``error``,
    ``error_code="week_not_found"``, ``available_sections``) instead of
    fabricating fallback notes. A section that matches but has no materials
    is a valid empty state and returns normal fallback notes.
    """
    client = _get_client()
    resolved, error = _resolve_course_arg(client, course_id)
    if error is not None:
        return error
    try:
        result = build_weekly_summary(client, resolved, week)
    except CourseNotFoundError as exc:
        return _course_not_found(exc)
    except WeekNotFoundError as exc:
        return {
            "error": str(exc),
            "error_code": "week_not_found",
            "available_sections": exc.available_sections,
        }
    result["formatted"] = format_bullets(result["bullets"])
    return result


@tool()
def download_material(
    course_id: int | str,
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
    course_id : int | str
        The Moodle course ID, or a course short-code (exact match or an
        unambiguous prefix, e.g. ``"ECON101"``). An unknown name returns
        ``course_not_found``; an ambiguous prefix returns
        ``course_ambiguous`` with a ``candidates`` list.
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
    resolved, error = _resolve_course_arg(client, course_id)
    if error is not None:
        return error
    chosen = _select_week_material(client, resolved, week, match, index)
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
    (``course_not_found``, or a selection error with a ``candidates`` list
    where applicable) that the calling tool passes straight back to the
    agent.
    """
    try:
        sections = client.get_course_contents(course_id)
    except CourseNotFoundError as exc:
        return _course_not_found(exc)
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


@tool()
def extract_material(
    course_id: int | str,
    week: str,
    match: str = "",
    index: int = -1,
    max_chars: int = 0,
    clean: bool = True,
    include_markdown: bool = False,
) -> dict[str, Any]:
    """Extract per-page structured text from a material (in memory).

    Fetches the file with authenticated credentials and returns its
    text page by page (slide by slide for PPTX) — each page carries
    ``text``, ``image_count``, ``has_low_text_density``, and
    ``warnings``. Nothing is written to disk; use ``download_material()``
    when you need the file itself.

    The whole response is deterministically bounded to about
    130,000 characters. To achieve that without discarding content, the
    per-page ``markdown`` rendering (which duplicates ``text``) is omitted
    by default — the ``text`` field carries the full content. Pass
    ``include_markdown=True`` for the light Markdown view; the text budget
    is reduced accordingly so the combined response stays within the
    bound. If a file is too large to fit, trailing pages are truncated and
    an explicit ``warnings`` entry says so and how to get the rest
    (re-extract a specific page, or narrow with ``match``/``index``).

    Light cleaning is applied by default and preserves educational
    content — figure captions, learning objectives, references. Pages
    dominated by images are flagged rather than silently empty.

    If multiple materials match, returns a structured error with a
    candidate list so the caller can refine with *match* or *index*. An
    unknown course name returns ``{"error", "error_code":
    "course_not_found"}``; an ambiguous prefix returns ``{"error",
    "error_code": "course_ambiguous", "candidates"}``.

    Parameters
    ----------
    course_id : int | str
        The Moodle course ID, or a course short-code (exact match or an
        unambiguous prefix, e.g. ``"ECON101"``).
    week : str
        Week number (e.g. "3") or section name substring.
    match : str
        Optional substring to filter candidates by file or module name.
    index : int
        Zero-based index to pick from matching materials (-1 = auto).
    max_chars : int
        Cap on total extracted text across pages (0 = default cap). Values
        above the default per-file cap are clamped to keep the response
        bounded.
    clean : bool
        Strip boilerplate lines (page numbers, copyright footers,
        repeated headers). Set False for the raw extractor output.
    include_markdown : bool
        Also return the per-page ``markdown`` rendering (default False).
    """
    client = _get_client()
    resolved, error = _resolve_course_arg(client, course_id)
    if error is not None:
        return error
    chosen = _select_week_material(client, resolved, week, match, index)
    if "error" in chosen:
        return chosen

    requested = max_chars if max_chars > 0 else MAX_TEXT_PER_FILE
    # The MCP response is bounded, so never extract more text than the
    # per-file cap regardless of the requested value. When markdown is
    # also returned it roughly doubles the size, so halve the text budget.
    budget = min(requested, MAX_TEXT_PER_FILE)
    if include_markdown:
        budget = min(budget, MAX_EXTRACT_RESPONSE_CHARS // 2)

    try:
        result = _extract_material_content(
            client, chosen, max_chars=budget, clean=clean,
        )
    except DownloadError as exc:
        return {"error": str(exc), "error_code": exc.code}
    except RuntimeError as exc:
        return {"error": str(exc)}

    return _bound_extract_response(result, include_markdown=include_markdown)


def _bound_extract_response(
    result: dict[str, Any], *, include_markdown: bool,
) -> dict[str, Any]:
    """Shape and hard-cap an ``extract_material`` result for MCP.

    Drops the per-page ``markdown`` field unless the caller opted in, then
    enforces :data:`MAX_EXTRACT_RESPONSE_CHARS` on the serialized payload:
    markdown on trailing pages is dropped first, then trailing page text is
    truncated, and an explicit truncation warning is appended. The result
    is deterministic — the same input always yields the same bounded
    output.
    """
    pages = result.get("pages", [])
    if not include_markdown:
        for page in pages:
            page.pop("markdown", None)

    def _size() -> int:
        return len(json.dumps(result, default=str))

    if _size() <= MAX_EXTRACT_RESPONSE_CHARS:
        return result

    truncated = False
    # Markdown duplicates text; shed it from the tail first.
    for page in reversed(pages):
        if _size() <= MAX_EXTRACT_RESPONSE_CHARS:
            break
        if page.get("markdown"):
            page["markdown"] = ""
            truncated = True
    # Still over budget: truncate trailing page text.
    idx = len(pages) - 1
    while idx >= 0 and _size() > MAX_EXTRACT_RESPONSE_CHARS:
        page = pages[idx]
        text = page.get("text", "")
        if len(text) > 200:
            page["text"] = text[: len(text) // 2]
            truncated = True
        else:
            page["text"] = ""
            if "markdown" in page:
                page["markdown"] = ""
            idx -= 1
            truncated = True

    if truncated:
        result.setdefault("warnings", []).append(
            "Response truncated to stay within the "
            f"~{MAX_EXTRACT_RESPONSE_CHARS}-character MCP limit; re-extract a "
            "specific page, or narrow with match/index, for the full text."
        )
    return result


@tool()
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

    The result carries an ``outcome``: ``"success"`` (every category
    synced), ``"partial"`` (some did — see ``warnings``), or ``"failed"``
    (none did, so an empty ``changes`` list means "nothing was fetched",
    not "nothing changed"). A failed run also carries ``failure_class``
    (``auth``, ``network``, ``rate_limited``, ``other``).

    While another Worsaga process is already syncing this site, this
    returns ``{"error", "error_code": "sync_in_progress"}`` and makes no
    requests, rather than fetching every course a second time.

    Parameters
    ----------
    lookahead_days : int
        Deadline look-ahead window in days (default 60).
    """
    try:
        result = _run_sync(_get_client(), lookahead_days=lookahead_days)
    except PrincipalMismatchError as exc:
        return {"error": str(exc), "error_code": "principal_mismatch"}
    if result.get("outcome") == "skipped":
        return {
            "error": (result.get("warnings") or ["another sync is running"])[0],
            "error_code": "sync_in_progress",
            "site": result.get("site", ""),
        }
    return result


@tool()
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


@tool()
def build_search_index(
    course_id: int | str = 0,
    week: str = "",
    max_files: int = INDEX_MAX_FILES_PER_RUN,
) -> dict[str, Any]:
    """Build or update the local full-text search index.

    Fetches supported course materials (PDF, PPTX, DOCX, TXT) in memory
    with authenticated credentials, extracts their text, and stores it
    page by page in a local SQLite full-text index for ``search_text()``.
    Files unchanged since the last build are skipped without a fetch, so
    repeated calls are cheap and resume where the previous run's file
    budget stopped. Full-course builds also drop index entries for files
    deleted or renamed on Moodle (reported as ``files_removed``; never
    on failed fetches, and never from week-scoped builds). Tokens and
    authenticated URLs are never stored.

    Parameters
    ----------
    course_id : int | str
        Limit indexing to one course — a numeric id or a course short-code
        (exact match or an unambiguous prefix); ``0`` = all enrolled
        courses. This tool fetches from Moodle, so the id is confirmed
        against your enrolled courses: an id or name that is not among them
        returns ``course_not_found``, and an ambiguous prefix returns
        ``course_ambiguous`` with a ``candidates`` list.
    week : str
        Only index sections matching this week number or name
        (empty = the whole course).
    max_files : int
        Cap on files fetched this run (default 100).
    """
    client = _get_client()
    # ``0`` is this tool's documented "all enrolled courses" sentinel, not a
    # course id to resolve.
    resolved, error = _resolve_course_arg(client, course_id or None)
    if error is not None:
        return error
    try:
        return _build_text_index(
            client,
            course_id=resolved or None,
            week=week or None,
            max_files=max_files,
        )
    except PrincipalMismatchError as exc:
        return {"error": str(exc), "error_code": "principal_mismatch"}
    except TextIndexError as exc:
        return {"error": str(exc), "error_code": "index_unavailable"}
    except ValueError as exc:
        return {"error": str(exc)}


@tool()
def search_text(
    query: str,
    course_id: int | str = 0,
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
    course_id : int | str
        Limit hits to one course — a numeric id or a course short-code
        (exact match or an unambiguous prefix); ``0`` = all courses. A
        numeric id filters the local index directly and keeps this tool
        offline; an id that was never indexed simply yields no hits. A
        short-code has to be matched against your enrolled courses, which
        is the one case that contacts Moodle: an unknown name then returns
        ``course_not_found`` and an ambiguous prefix ``course_ambiguous``
        with a ``candidates`` list.
    limit : int
        Maximum hits to return (default 20).
    """
    client = _get_client()
    # A numeric id goes straight to the local index filter. The index is
    # built enrolment-scoped, so filtering by id cannot widen what it holds,
    # and resolving the id would break this tool's no-network contract.
    resolved = _numeric_course_id(course_id)
    if resolved is None:
        resolved, error = _resolve_course_arg(client, course_id or None)
        if error is not None:
            return error
    try:
        return _search_text_index(
            client.base_url,
            query,
            course_id=resolved or None,
            limit=limit,
            # Only an identity this server already verified, so the
            # no-network contract above still holds.
            principal=known_principal(client),
        )
    except PrincipalMismatchError as exc:
        return {"error": str(exc), "error_code": "principal_mismatch"}
    except TextIndexError as exc:
        return {"error": str(exc), "error_code": "index_unavailable"}


@tool()
def export_study_pack(
    course_id: int | str,
    week: str,
    output_dir: str = "",
    include_markdown: bool = False,
) -> dict[str, Any]:
    """Export a Markdown study pack for a course week.

    Builds a single Markdown document — study notes, a materials
    overview, and the extracted per-page content of the week's
    supported files (up to 8; larger sections are included in listed
    order with a warning) — and writes it inside Worsaga's own
    downloads directory. The response reports the written ``path`` and
    pack metadata; set ``include_markdown=True`` to also return the
    full Markdown inline. No tokens or authenticated URLs appear in
    the pack or the response.

    Parameters
    ----------
    course_id : int | str
        The Moodle course ID, or a course short-code (exact match or an
        unambiguous prefix, e.g. ``"ECON101"``). An unknown name returns
        ``course_not_found``; an ambiguous prefix returns
        ``course_ambiguous`` with a ``candidates`` list.
    week : str
        Week number (e.g. "3") or section name query.
    output_dir : str
        Optional subdirectory (relative path) inside Worsaga's own
        downloads directory. Absolute paths and path traversal are
        rejected.
    include_markdown : bool
        Also return the full Markdown content inline (default False).

    If *week* matches no section at all, returns a structured error dict
    (``error``, ``error_code="week_not_found"``, ``available_sections``)
    instead of writing a fabricated pack. A section that matches but has
    no materials is a valid empty state and produces a coherent pack.
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
    resolved, error = _resolve_course_arg(client, course_id)
    if error is not None:
        return error
    try:
        result = _build_study_pack(client, resolved, week)
    except CourseNotFoundError as exc:
        return _course_not_found(exc)
    except WeekNotFoundError as exc:
        return {
            "error": str(exc),
            "error_code": "week_not_found",
            "available_sections": exc.available_sections,
        }
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


@tool()
def get_autosync_status() -> dict[str, Any]:
    """Report whether a scheduled background sync is registered.

    Read-only: inspects the platform scheduler (Task Scheduler on
    Windows, launchd on macOS, a systemd user timer on Linux) and the
    local install record, and changes nothing. ``last_sync_at``, when
    present, is the site's most recent sync from **any** trigger —
    manual or scheduled; sync provenance is not recorded. Installing
    or removing the scheduled sync modifies system state and is
    deliberately CLI only — direct the user to
    ``worsaga auto-sync install`` / ``worsaga auto-sync remove``
    (both support ``--dry-run``).
    """
    return _autosync_status()


@tool()
def get_connection_info() -> dict[str, Any]:
    """Report authentication and site identity without fetching any data.

    A cheap, read-only "am I connected?" check: it makes at most one Moodle
    web-service call (``core_webservice_get_site_info``) and returns

    - ``authenticated`` — ``True`` when the site answered;
    - ``demo_mode`` — ``True`` when serving the offline demo dataset;
    - ``site_url`` — the Moodle **base** URL only (never the token or any
      ``/webservice`` path);
    - ``site_name`` — the site's display name;
    - ``user_id`` and ``user_display_name`` — the authenticated user;
    - ``worsaga_version`` — the running Worsaga version;
    - ``config_source`` — where credentials came from: ``"env"``,
      ``"file"``, ``"demo"``, or ``"unset"``;
    - ``config_path`` — for a file-backed config, the file *path* only
      (never its contents); ``None`` otherwise.

    Use this before other tools to confirm the server is pointed at the
    right Moodle and account. On an authentication or network failure it
    returns a structured ``{"error", "error_code"}`` dict with
    ``error_code`` ``"auth"`` (credentials missing or rejected) or
    ``"network"`` (the site was unreachable). The token never appears in
    any field.
    """
    demo = demo_mode_enabled()
    try:
        client = _get_client()
        return build_connection_info(client, demo_mode=demo)
    except ConnectionCheckError as exc:
        return {"error": str(exc), "error_code": exc.code}
    except ValueError as exc:
        # Credentials are not configured (MoodleConfig.load); the server is
        # importable but cannot authenticate — an auth-state answer, not a
        # crash. _get_client() does not cache a client on this path.
        return {"error": str(exc), "error_code": "auth"}


def main() -> None:
    """Entry point when running ``python -m worsaga.mcp_server``."""
    mcp.run()


if __name__ == "__main__":
    main()
