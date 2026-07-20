"""Calendar event reader."""

from __future__ import annotations

import re
import time
from typing import Any

from worsaga.client import MoodleClient
from worsaga.materials import match_section
from worsaga.models import as_int, clean_text
from worsaga.time_utils import timestamp_to_display, timestamp_to_iso


_WEEK_EVENT_LABELS = (
    "week",
    "wk",
    "topic",
    "session",
    "lecture",
    "lec",
    "seminar",
    "class",
    "workshop",
)


def normalize_calendar_events(payload: dict[str, Any] | list[Any]) -> list[dict[str, Any]]:
    """Normalize Moodle calendar payloads."""
    events = payload.get("events", []) if isinstance(payload, dict) else payload
    records: list[dict[str, Any]] = []
    for event in events if isinstance(events, list) else []:
        if not isinstance(event, dict):
            continue
        start_at = as_int(event.get("timestart", event.get("timesort")))
        records.append({
            "id": as_int(event.get("id"), 0),
            "course_id": as_int(event.get("courseid")),
            "name": clean_text(event.get("name")),
            "description": clean_text(event.get("description"), limit=180),
            "event_type": str(event.get("eventtype") or event.get("type") or ""),
            "start_at": start_at,
            "start_iso": timestamp_to_iso(start_at),
            "start_str": timestamp_to_display(start_at),
            "duration": as_int(event.get("timeduration"), 0) or 0,
            "source": "calendar",
            "view_url": str(event.get("url") or event.get("viewurl") or ""),
        })
    records.sort(key=lambda r: r["start_at"] or 0)
    return records


def _week_int(value: int | str) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _match_text(value: Any) -> str:
    return clean_text(value).lower()


def _event_text(event: dict[str, Any]) -> str:
    return " ".join(
        _match_text(event.get(key))
        for key in ("name", "description", "event_type", "type")
    )


def _matches_week_query(text: str, week: int | str) -> bool:
    week_num = _week_int(week)
    if week_num is None:
        query = str(week).strip().lower()
        return bool(query) and query in text

    labels = "|".join(re.escape(label) for label in _WEEK_EVENT_LABELS)
    return re.search(rf"\b(?:{labels})\s*0*{week_num}\b", text) is not None


def _section_title_tail(name: str, week: int | str) -> str:
    week_num = _week_int(week)
    if week_num is None:
        return ""
    labels = "|".join(re.escape(label) for label in _WEEK_EVENT_LABELS)
    tail = re.sub(rf"^\s*(?:{labels})\s*0*{week_num}\b\W*", "", name, flags=re.I)
    return re.sub(rf"^\s*0*{week_num}\b\W*", "", tail).strip()


def _add_term(terms: list[str], value: Any) -> None:
    term = _match_text(value)
    if len(term) < 4 or not any(ch.isalpha() for ch in term):
        return
    if term not in terms:
        terms.append(term)


def _week_terms_from_sections(
    sections: list[dict[str, Any]] | None,
    week: int | str,
) -> list[str]:
    terms: list[str] = []
    if not sections:
        return terms

    for section in sections:
        if not isinstance(section, dict) or not match_section(section, week):
            continue
        name = clean_text(section.get("name"))
        _add_term(terms, name)
        _add_term(terms, _section_title_tail(name, week))
        for module in section.get("modules", []) or []:
            if isinstance(module, dict):
                _add_term(terms, module.get("name"))
    return terms


def filter_calendar_events_by_week(
    events: list[dict[str, Any]],
    week: int | str,
    *,
    sections: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Filter normalized calendar events to a teaching-week query.

    Moodle calendar events do not reliably carry section IDs, so this uses
    explicit week references in event text plus section/module names from the
    matching Moodle course section when available.
    """
    section_terms = _week_terms_from_sections(sections, week)
    filtered: list[dict[str, Any]] = []
    for event in events:
        text = _event_text(event)
        if _matches_week_query(text, week):
            filtered.append(event)
            continue
        if any(term in text for term in section_terms):
            filtered.append(event)
    return filtered


def get_calendar_events(
    client: MoodleClient,
    course_id: int | None = None,
    *,
    days: int = 30,
    week: int | str | None = None,
) -> list[dict[str, Any]]:
    """Return calendar events for one course or all courses."""
    now = int(time.time())
    end = now + days * 86400
    course_ids = [course_id] if course_id is not None else None
    payload = client.get_calendar_events(
        course_ids=course_ids,
        timestart=now,
        timeend=end,
    )
    events = normalize_calendar_events(payload)
    if week is None:
        return events
    sections = client.get_course_contents(course_id) if course_id is not None else None
    return filter_calendar_events_by_week(events, week, sections=sections)
