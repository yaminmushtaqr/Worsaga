"""Normalized JSON-friendly Worsaga record factories."""

from __future__ import annotations

import html
import re
from typing import Any

from worsaga.time_utils import timestamp_to_display, timestamp_to_iso


def as_int(value: Any, default: int | None = None) -> int | None:
    """Return *value* as an int when possible, otherwise *default*."""
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def as_float(value: Any, default: float | None = None) -> float | None:
    """Return *value* as a float when possible, otherwise *default*."""
    if value is None or value == "":
        return default
    if isinstance(value, str):
        value = value.strip().replace("%", "")
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def as_bool(value: Any, default: bool | None = None) -> bool | None:
    """Return *value* as a bool when Moodle gives common bool-like values."""
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "y", "submitted", "complete"}:
            return True
        if lowered in {"0", "false", "no", "n", "not submitted", "new"}:
            return False
    return default


def clean_text(value: Any, *, limit: int | None = None) -> str:
    """Return compact plain text from a Moodle string or HTML fragment."""
    text = html.unescape(str(value or ""))
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if limit is not None and len(text) > limit:
        return text[: max(0, limit - 1)].rstrip() + "..."
    return text


# Cap plain-text section summaries so a pathological inline-styled summary
# cannot dominate a compact course-contents response. Real summaries strip
# to a sentence or two; this is a guard, not the common case.
SECTION_SUMMARY_LIMIT = 500


def course_record(course: dict[str, Any]) -> dict[str, Any]:
    """Normalize a Moodle course dict into a compact, agent-useful record.

    Keeps identity (``id``, ``shortname``, ``fullname``) plus a few
    schedule fields (``category``, ``start_at``, ``end_at``) and drops the
    bulky, agent-irrelevant fields the raw ``core_enrol_get_users_courses``
    payload carries — HTML ``summary``, ``enrolledusercount``, the course
    image, progress, and so on. No HTML and no token-bearing URLs survive.
    """
    return {
        "id": as_int(course.get("id"), 0),
        "shortname": str(course.get("shortname") or ""),
        "fullname": str(course.get("fullname") or ""),
        "category": as_int(course.get("category")),
        "start_at": as_int(course.get("startdate")),
        "end_at": as_int(course.get("enddate")),
    }


def course_module_file_record(
    *,
    file_name: str,
    file_size: Any = 0,
    mime_type: str = "",
    time_modified: Any = 0,
    dedupe_key: str,
) -> dict[str, Any]:
    """Build a compact, token-free record for one file inside a module.

    Mirrors the file metadata ``get_week_materials`` exposes — name, size,
    type, modification time, and a token-free ``dedupe_key`` — but never a
    raw ``file_url``. Downloads always go through the authenticated path.
    """
    return {
        "file_name": str(file_name or ""),
        "file_size": as_int(file_size, 0),
        "mime_type": str(mime_type or ""),
        "time_modified": as_int(time_modified, 0),
        "dedupe_key": dedupe_key,
    }


def course_module_record(
    *,
    module_id: Any,
    module_name: str,
    module_type: str,
    view_url: str = "",
    files: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a compact module record for course-contents output.

    Carries the module identity (``module_id``, ``module_name``,
    ``module_type``), an optional human ``view_url``, and an optional
    ``files`` list of token-free file records. No raw ``file_url`` values.
    """
    record: dict[str, Any] = {
        "module_id": as_int(module_id, 0),
        "module_name": clean_text(module_name),
        "module_type": str(module_type or ""),
    }
    if view_url:
        record["view_url"] = view_url
    if files:
        record["files"] = files
    return record


def course_section_record(
    *,
    section_id: Any,
    section_num: Any,
    section_name: str,
    summary: str = "",
    modules: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a compact section record with an HTML-stripped summary.

    The Moodle section ``summary`` is often inline-styled HTML; it is
    reduced to plain text (capped at :data:`SECTION_SUMMARY_LIMIT`) so the
    response stays small and carries no markup or token-bearing URLs.
    """
    return {
        "section_id": as_int(section_id, 0),
        "section_num": as_int(section_num, 0),
        "section_name": clean_text(section_name),
        "summary": clean_text(summary, limit=SECTION_SUMMARY_LIMIT),
        "modules": modules or [],
    }


def assignment_record(
    *,
    course_id: int,
    course_shortname: str,
    assignment: dict[str, Any],
    submitted: bool | None = None,
    submission_status: str = "",
    late: bool | None = None,
    overdue: bool = False,
    graded: bool | None = None,
    feedback_available: bool | None = None,
    days_left: int | None = None,
    view_url: str = "",
) -> dict[str, Any]:
    """Build a normalized assignment record."""
    return {
        "course_id": course_id,
        "course_shortname": course_shortname,
        "id": as_int(assignment.get("id"), 0),
        "cmid": as_int(assignment.get("cmid"), 0),
        "name": clean_text(assignment.get("name")),
        "due_at": as_int(assignment.get("duedate")),
        "cutoff_at": as_int(assignment.get("cutoffdate")),
        "allows_submissions_from": as_int(assignment.get("allowsubmissionsfromdate")),
        "submitted": submitted,
        "submission_status": submission_status,
        "late": late,
        "overdue": overdue,
        "graded": graded,
        "feedback_available": feedback_available,
        "days_left": days_left,
        "view_url": view_url,
    }


def grade_record(
    *,
    course_id: int,
    course_shortname: str,
    item_id: int | None,
    item_name: str,
    category: str = "",
    grade_raw: str = "",
    grade_display: str = "",
    percentage: float | None = None,
    weight: float | None = None,
    range_text: str = "",
    feedback: str = "",
    graded: bool = False,
    graded_at: int | None = None,
    hidden: bool | None = None,
    contributes_to_total: bool | None = None,
    status: str = "unknown",
) -> dict[str, Any]:
    """Build a normalized grade record.

    ``graded_at`` is the Unix epoch when the item was graded (Moodle's
    ``gradedategraded``), kept for machine use; ``graded_iso`` (UTC
    ISO-8601) and ``graded_str`` (local display) are derived from it,
    mirroring the ``due_*`` / ``start_*`` fields on deadlines and calendar
    events. All three are empty/``None`` when Moodle reports no grade date.
    """
    return {
        "course_id": course_id,
        "course_shortname": course_shortname,
        "item_id": item_id,
        "item_name": clean_text(item_name),
        "category": clean_text(category),
        "grade_raw": clean_text(grade_raw),
        "grade_display": clean_text(grade_display),
        "percentage": percentage,
        "weight": weight,
        "range": clean_text(range_text),
        "feedback": clean_text(feedback),
        "graded": graded,
        "graded_at": graded_at,
        "graded_iso": timestamp_to_iso(graded_at),
        "graded_str": timestamp_to_display(graded_at),
        "hidden": hidden,
        "contributes_to_total": contributes_to_total,
        "status": status,
    }


def forum_discussion_record(
    *,
    course_id: int,
    forum_id: int,
    forum_name: str,
    discussion_id: int,
    name: str,
    author: str = "",
    created_at: int | None = None,
    modified_at: int | None = None,
    unread_count: int | None = None,
    pinned: bool | None = None,
    locked: bool | None = None,
    view_url: str = "",
) -> dict[str, Any]:
    """Build a normalized forum discussion record.

    The raw Unix epochs (``created_at``, ``modified_at``) are kept for
    machine use; ``created_iso`` / ``modified_iso`` (UTC ISO-8601) and
    ``created_str`` / ``modified_str`` (local display) are derived
    alongside them, mirroring the ``due_*`` / ``start_*`` fields on
    deadlines and calendar events.
    """
    return {
        "course_id": course_id,
        "forum_id": forum_id,
        "forum_name": clean_text(forum_name),
        "discussion_id": discussion_id,
        "name": clean_text(name),
        "author": clean_text(author),
        "created_at": created_at,
        "created_iso": timestamp_to_iso(created_at),
        "created_str": timestamp_to_display(created_at),
        "modified_at": modified_at,
        "modified_iso": timestamp_to_iso(modified_at),
        "modified_str": timestamp_to_display(modified_at),
        "unread_count": unread_count,
        "pinned": pinned,
        "locked": locked,
        "view_url": view_url,
    }


def notification_record(
    *,
    notification_id: int | str,
    notification_type: str,
    subject: str,
    body: str = "",
    sender: str = "",
    course_id: int | None = None,
    created_at: int | None = None,
    read: bool | None = None,
    view_url: str = "",
) -> dict[str, Any]:
    """Build a normalized notification or message record.

    The raw Unix epoch (``created_at``) is kept for machine use;
    ``created_iso`` (UTC ISO-8601) and ``created_str`` (local display) are
    derived alongside it, mirroring the ``due_*`` / ``start_*`` fields on
    deadlines and calendar events.
    """
    return {
        "id": notification_id,
        "type": notification_type,
        "subject": clean_text(subject),
        "body_preview": clean_text(body, limit=180),
        "sender": clean_text(sender),
        "course_id": course_id,
        "created_at": created_at,
        "created_iso": timestamp_to_iso(created_at),
        "created_str": timestamp_to_display(created_at),
        "read": read,
        "view_url": view_url,
    }


def change_record(
    *,
    kind: str,
    title: str,
    detected_at: int,
    course_id: int | None = None,
    course_shortname: str = "",
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a normalized change record."""
    return {
        "kind": kind,
        "course_id": course_id,
        "course_shortname": course_shortname,
        "title": clean_text(title),
        "before": before,
        "after": after,
        "detected_at": detected_at,
    }
