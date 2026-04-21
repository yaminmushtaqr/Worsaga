"""Deadline aggregation for assignments and quizzes.

Fetches upcoming deadlines from the Moodle client, deduplicates,
normalises, and returns them sorted by due date.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from worsaga.client import MoodleClient, MoodleWriteAttemptError

logger = logging.getLogger(__name__)


def get_upcoming_deadlines(
    client: MoodleClient,
    lookahead_days: int = 14,
) -> list[dict]:
    """Return upcoming deadlines for assignments AND quizzes, sorted by due date.

    Assignments and quizzes are fetched in one batched call each. If either
    batched fetch fails, a warning is logged via ``worsaga.deadlines`` and that
    category is omitted from the result; the other category still returns.
    :class:`MoodleWriteAttemptError` always propagates so the read-only
    guarantee remains observable to callers.

    Each entry contains:
        name, type, course, due_ts, due_str, due_iso, days_left
    """
    now = time.time()
    cutoff = now + lookahead_days * 86400
    upcoming: list[dict] = []
    seen: set[tuple[str, int]] = set()

    courses = client.get_courses()
    course_map = {c["id"]: c["shortname"] for c in courses}
    course_ids = list(course_map.keys())

    # --- Assignments (batched) ---
    try:
        assign_result = client.get_assignments_by_courses(course_ids)
    except MoodleWriteAttemptError:
        raise
    except Exception as exc:
        logger.warning(
            "Moodle assignment fetch failed for %d course(s); "
            "deadline list will omit assignments: %s",
            len(course_ids), exc,
        )
        assign_result = {"courses": []}

    for c in assign_result.get("courses", []):
        course_shortname = course_map.get(c.get("id"), "Unknown")
        for a in c.get("assignments", []):
            due = a.get("duedate", 0)
            if due and now < due < cutoff:
                key = ("assign", a["id"])
                if key not in seen:
                    seen.add(key)
                    due_dt = datetime.fromtimestamp(due, tz=timezone.utc)
                    upcoming.append({
                        "name": a["name"],
                        "type": "assignment",
                        "course": course_shortname,
                        "due_ts": due,
                        "due_str": due_dt.strftime("%b %d %H:%M UTC"),
                        "due_iso": due_dt.isoformat(),
                        "days_left": int((due - now) / 86400),
                    })

    # --- Quizzes (batched) ---
    try:
        quiz_result = client.get_quizzes(course_ids)
    except MoodleWriteAttemptError:
        raise
    except Exception as exc:
        logger.warning(
            "Moodle quiz fetch failed for %d course(s); "
            "deadline list will omit quizzes: %s",
            len(course_ids), exc,
        )
        quiz_result = {"quizzes": []}

    for q in quiz_result.get("quizzes", []):
        close = q.get("timeclose", 0)
        if close and now < close < cutoff:
            key = ("quiz", q["id"])
            if key not in seen:
                seen.add(key)
                due_dt = datetime.fromtimestamp(close, tz=timezone.utc)
                course_name = course_map.get(q.get("course", 0), "Unknown")
                upcoming.append({
                    "name": q["name"],
                    "type": "quiz",
                    "course": course_name,
                    "due_ts": close,
                    "due_str": due_dt.strftime("%b %d %H:%M UTC"),
                    "due_iso": due_dt.isoformat(),
                    "days_left": int((close - now) / 86400),
                })

    upcoming.sort(key=lambda x: x["due_ts"])
    return upcoming


def normalize_deadlines(deadlines: list[dict]) -> list[dict]:
    """Convert raw deadline dicts into a simplified form for storage/display."""
    import html as _html

    return [
        {
            "title": _html.unescape(str(d.get("name", "Untitled"))),
            "module": str(d.get("course", "Unknown")),
            "due_date": str(d.get("due_iso", "")),
            "type": str(d.get("type", "deadline")),
        }
        for d in deadlines
    ]
