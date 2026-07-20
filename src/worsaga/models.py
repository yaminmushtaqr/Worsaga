"""Normalized JSON-friendly Worsaga record factories."""

from __future__ import annotations

import html
import re
from typing import Any


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


def course_record(course: dict[str, Any]) -> dict[str, Any]:
    """Normalize a Moodle course dict."""
    return {
        "id": as_int(course.get("id"), 0),
        "shortname": str(course.get("shortname") or ""),
        "fullname": str(course.get("fullname") or ""),
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
    hidden: bool | None = None,
    contributes_to_total: bool | None = None,
    status: str = "unknown",
) -> dict[str, Any]:
    """Build a normalized grade record."""
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
    """Build a normalized forum discussion record."""
    return {
        "course_id": course_id,
        "forum_id": forum_id,
        "forum_name": clean_text(forum_name),
        "discussion_id": discussion_id,
        "name": clean_text(name),
        "author": clean_text(author),
        "created_at": created_at,
        "modified_at": modified_at,
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
    """Build a normalized notification or message record."""
    return {
        "id": notification_id,
        "type": notification_type,
        "subject": clean_text(subject),
        "body_preview": clean_text(body, limit=180),
        "sender": clean_text(sender),
        "course_id": course_id,
        "created_at": created_at,
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
