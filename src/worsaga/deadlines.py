"""Deadline aggregation for assignments and quizzes.

Fetches upcoming deadlines from the Moodle client, deduplicates,
normalises, and returns them sorted by due date.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from worsaga.client import MoodleClient, MoodleWriteAttemptError


def get_upcoming_deadlines(
    client: MoodleClient,
    lookahead_days: int = 14,
) -> list[dict]:
    """Return upcoming deadlines for assignments AND quizzes, sorted by due date.

    Each entry contains:
        name, type, course, due_ts, due_str, due_iso, days_left
    """
    now = time.time()
    cutoff = now + lookahead_days * 86400
    upcoming: list[dict] = []
    seen: set[tuple[str, int]] = set()

    courses = client.get_courses()
    course_map = {c["id"]: c["shortname"] for c in courses}

    # --- Assignments ---
    for course in courses:
        try:
            result = client.get_assignments(course["id"])
            for c in result.get("courses", []):
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
                                "course": course["shortname"],
                                "due_ts": due,
                                "due_str": due_dt.strftime("%b %d %H:%M UTC"),
                                "due_iso": due_dt.isoformat(),
                                "days_left": int((due - now) / 86400),
                            })
        except MoodleWriteAttemptError:
            raise
        except Exception:
            pass

    # --- Quizzes ---
    try:
        ids = list(course_map.keys())
        params = {f"courseids[{i}]": cid for i, cid in enumerate(ids)}
        result = client.call("mod_quiz_get_quizzes_by_courses", **params)
        for q in result.get("quizzes", []):
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
    except MoodleWriteAttemptError:
        raise
    except Exception:
        pass

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
