"""Assignment status retrieval and normalization."""

from __future__ import annotations

import logging
import time
from typing import Any

from worsaga.client import (
    AssignmentNotFoundError,
    CourseNotFoundError,
    MoodleClient,
    MoodleWriteAttemptError,
)
from worsaga.concurrency import ProgressCallback, run_parallel
from worsaga.models import as_bool, as_int, assignment_record, clean_text
from worsaga.time_utils import calculate_days_left, timestamp_to_display

logger = logging.getLogger(__name__)


SUBMITTED_STATUSES = {"submitted", "graded", "released"}


def _course_targets(
    client: MoodleClient,
    course_id: int | None,
    courses: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Return the enrolled course records this request covers.

    An id outside the enrolment list is a not-found failure, never a
    synthesised ``{"id": course_id, "shortname": str(course_id)}`` target:
    fabricating one used to send an unknown id to Moodle and, if it
    answered, present a record for a course this account is not in.

    *courses* reuses an enrolled-course list the caller already has.
    """
    if courses is None:
        courses = client.get_courses()
    if course_id is None:
        return courses
    for course in courses:
        if as_int(course.get("id")) == course_id:
            return [course]
    raise CourseNotFoundError(course_id)


def _submission_from_status(status_payload: dict[str, Any] | None) -> dict[str, Any]:
    empty = {"submission": {}, "last_attempt": {}, "feedback": {}, "raw": {}}
    if not isinstance(status_payload, dict):
        return empty
    last_attempt = status_payload.get("lastattempt") or {}
    submission = last_attempt.get("submission") or status_payload.get("submission") or {}
    if not isinstance(submission, dict):
        submission = {}
    feedback = status_payload.get("feedback") or {}
    return {
        "submission": submission,
        "last_attempt": last_attempt if isinstance(last_attempt, dict) else {},
        "feedback": feedback if isinstance(feedback, dict) else {},
        "raw": status_payload,
    }


def _submitted(submission_status: str, status_parts: dict[str, Any]) -> bool | None:
    lowered = submission_status.lower()
    if lowered in SUBMITTED_STATUSES:
        return True
    if lowered in {"new", "draft", "not submitted", "no attempt"}:
        return False
    explicit = as_bool(status_parts.get("raw", {}).get("submitted"), None)
    if explicit is not None:
        return explicit
    return None


def _feedback_available(status_parts: dict[str, Any]) -> bool | None:
    feedback = status_parts.get("feedback") or {}
    if feedback.get("grade") or feedback.get("plugins"):
        return True
    raw = status_parts.get("raw") or {}
    if raw.get("feedback"):
        return True
    return None


def _graded(status_parts: dict[str, Any]) -> bool | None:
    feedback = status_parts.get("feedback") or {}
    grade = feedback.get("grade")
    if isinstance(grade, dict) and grade.get("grade") not in (None, "", "-"):
        return True
    raw = status_parts.get("raw") or {}
    explicit = as_bool(raw.get("graded"), None)
    if explicit is not None:
        return explicit
    return None


def _late(status_parts: dict[str, Any]) -> bool | None:
    raw = status_parts.get("raw") or {}
    last_attempt = status_parts.get("last_attempt") or {}
    for source in (raw, last_attempt, status_parts.get("submission") or {}):
        parsed = as_bool(source.get("late"), None)
        if parsed is not None:
            return parsed
    return None


def _status_label(
    *,
    submitted: bool | None,
    overdue: bool,
    graded: bool | None,
    days_left: int | None,
) -> str:
    if graded is True:
        return "graded"
    if submitted is True:
        return "submitted"
    if overdue:
        return "missing"
    if submitted is False:
        return "not_submitted"
    return "unknown"


def normalize_assignment(
    assignment: dict[str, Any],
    *,
    course_id: int,
    course_shortname: str,
    base_url: str = "",
    status_payload: dict[str, Any] | None = None,
    now: int | float | None = None,
) -> dict[str, Any]:
    """Normalize one Moodle assignment with optional submission status."""
    base_now = time.time() if now is None else now
    status_parts = _submission_from_status(status_payload)
    submission = status_parts.get("submission") or {}
    submission_status = str(
        submission.get("status")
        or status_parts.get("last_attempt", {}).get("submissionstatus")
        or status_parts.get("raw", {}).get("status")
        or ""
    )
    submitted = _submitted(submission_status, status_parts)
    due_at = as_int(assignment.get("duedate"))
    overdue = bool(due_at and due_at < base_now and submitted is not True)
    days_left = calculate_days_left(due_at, now=base_now)
    cmid = as_int(assignment.get("cmid"), 0) or 0
    view_url = f"{base_url}/mod/assign/view.php?id={cmid}" if base_url and cmid else ""

    record = assignment_record(
        course_id=course_id,
        course_shortname=course_shortname,
        assignment=assignment,
        submitted=submitted,
        submission_status=submission_status,
        late=_late(status_parts),
        overdue=overdue,
        graded=_graded(status_parts),
        feedback_available=_feedback_available(status_parts),
        days_left=days_left,
        view_url=view_url,
    )
    for timestamp_key in ("due_at", "cutoff_at", "allows_submissions_from"):
        if record[timestamp_key] == 0:
            record[timestamp_key] = None
    record["status"] = _status_label(
        submitted=record["submitted"],
        overdue=record["overdue"],
        graded=record["graded"],
        days_left=record["days_left"],
    )
    record["due_str"] = timestamp_to_display(record["due_at"])
    return record


def normalize_assignments(
    payload: dict[str, Any],
    *,
    course_map: dict[int, str],
    base_url: str = "",
    statuses: dict[int, dict[str, Any]] | None = None,
    now: int | float | None = None,
) -> list[dict[str, Any]]:
    """Normalize a Moodle ``mod_assign_get_assignments`` payload."""
    records: list[dict[str, Any]] = []
    statuses = statuses or {}
    for course in payload.get("courses", []) if isinstance(payload, dict) else []:
        if not isinstance(course, dict):
            continue
        course_id = as_int(course.get("id"), 0) or 0
        shortname = str(course.get("shortname") or course_map.get(course_id, course_id))
        assignments = course.get("assignments", [])
        if not isinstance(assignments, list):
            continue
        for assignment in assignments:
            if not isinstance(assignment, dict):
                continue
            assignment_id = as_int(assignment.get("id"), 0) or 0
            records.append(
                normalize_assignment(
                    assignment,
                    course_id=course_id,
                    course_shortname=shortname,
                    base_url=base_url,
                    status_payload=statuses.get(assignment_id),
                    now=now,
                )
            )
    records.sort(key=lambda r: (r["due_at"] or 2**31, r["course_shortname"], r["name"]))
    return records


def get_assignments(
    client: MoodleClient,
    course_id: int | None = None,
    *,
    include_feedback: bool = False,
    now: int | float | None = None,
    on_progress: ProgressCallback | None = None,
    courses: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Return normalized assignments for one course or all enrolled courses.

    Assignment definitions come from one batched call; each assignment's own
    submission status is then fetched concurrently (see
    :func:`worsaga.concurrency.run_parallel`), which is the slow part on a
    real account. A per-assignment status failure stays a logged warning and
    that assignment simply carries no submission detail — the record is still
    returned. ``on_progress`` (default silent) reports one completed
    assignment at a time.

    *courses* reuses an enrolled-course list the caller already fetched
    (the digest does) instead of listing them again.
    """
    courses = _course_targets(client, course_id, courses)
    course_ids = [as_int(course.get("id"), 0) or 0 for course in courses]
    course_map = {
        as_int(course.get("id"), 0) or 0: str(course.get("shortname") or course.get("id"))
        for course in courses
    }
    try:
        payload = client.get_assignments_by_courses(course_ids)
    except MoodleWriteAttemptError:
        raise
    except Exception as exc:
        logger.warning("Moodle assignment fetch failed: %s", exc)
        raise

    assignments = []
    for course in payload.get("courses", []) if isinstance(payload, dict) else []:
        assignments.extend(course.get("assignments", []) if isinstance(course, dict) else [])

    with_ids = [a for a in assignments if (as_int(a.get("id"), 0) or 0)]

    def _fetch_status(assignment: dict[str, Any]) -> tuple[int, dict[str, Any] | None]:
        assignment_id = as_int(assignment.get("id"), 0) or 0
        try:
            return assignment_id, client.get_assignment_submission_status(assignment_id)
        except MoodleWriteAttemptError:
            raise
        except Exception as exc:
            logger.warning(
                "Moodle assignment status fetch failed for assignment %s: %s",
                assignment_id,
                exc,
            )
            return assignment_id, None

    statuses: dict[int, dict[str, Any]] = {
        assignment_id: status
        for assignment_id, status in run_parallel(
            with_ids,
            _fetch_status,
            # clean_text so the live label matches the final record (Moodle
            # names arrive HTML-escaped, e.g. "Group 1 &amp; 2 ...").
            label_fn=lambda a: clean_text(a.get("name")) or str(a.get("id") or ""),
            on_progress=on_progress,
        )
        if status is not None
    }

    # ``include_feedback`` is accepted for compatibility but is a no-op:
    # grade and feedback fields always derive from the per-user
    # submission-status payloads fetched above. The former broad
    # ``mod_assign_get_grades`` call was removed — its response (which
    # can include other students' grades for teacher-capable tokens)
    # was never used, so the request only widened data exposure.
    del include_feedback

    return normalize_assignments(
        payload if isinstance(payload, dict) else {},
        course_map=course_map,
        base_url=client.base_url,
        statuses=statuses,
        now=now,
    )


def get_assignment_status(
    client: MoodleClient,
    course_id: int,
    assignment_id: int,
) -> dict[str, Any]:
    """Return one normalized assignment status record."""
    for assignment in get_assignments(client, course_id=course_id, include_feedback=True):
        if assignment.get("id") == assignment_id:
            return assignment
    # AssignmentNotFoundError subclasses ValueError, so existing callers and
    # the CLI's top-level handler are unaffected; MCP maps it to the
    # structured ``assignment_not_found`` error dict.
    raise AssignmentNotFoundError(assignment_id, course_id=course_id)
